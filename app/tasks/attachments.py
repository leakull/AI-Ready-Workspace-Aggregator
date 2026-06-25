"""Attachment download task.

For attachments not yet in S3, fetch the bytes and store them in MinIO, then
record the bucket+key on the row. The byte source depends on the message's
origin: a ``source_url`` is downloaded directly, while a Telegram attachment is
resolved from its ``file_id`` via ``getFile`` at download time (so the bot token
never lands in the DB). Idempotent: an attachment already pointing at S3 is
skipped. Email attachments are uploaded inline by the connector, so they never
reach this task; GitHub issues have none.

Failure handling mirrors the connectors: transient problems (network, 429, 5xx)
raise :class:`RetryableError` and the task retries with backoff; a permanent
failure (4xx) is recorded on the message as ``processing_status = error`` with an
``error_log`` (surfaced via the API) instead of failing the whole task.
"""

from __future__ import annotations

import httpx

from app.connectors.base import RetryableError
from app.connectors.telegram import resolve_file_url
from app.core.celery_app import celery_app
from app.core.logging import get_logger
from app.db.models import Attachment, Message, ProcessingStatus, SourceSystem
from app.db.session import session_scope
from app.services.storage import build_object_key, ensure_bucket, upload_bytes

log = get_logger(__name__)


def _build_client() -> httpx.Client:
    return httpx.Client(timeout=60.0, follow_redirects=True)


@celery_app.task(
    bind=True,
    name="app.tasks.attachments.download_attachments",
    autoretry_for=(httpx.TransportError, RetryableError),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def download_attachments(self, message_id: int) -> dict:
    with session_scope() as session:
        message = session.get(Message, message_id)
        if message is None:
            return {"message_id": message_id, "status": "missing"}

        pending = [a for a in message.attachments if _downloadable(message, a)]
        if not pending:
            return {"message_id": message_id, "downloaded": 0}

        ensure_bucket()
        downloaded = 0
        errors: list[str] = []
        for att in pending:
            try:
                downloaded += _download_one(session, message, att)
            except httpx.HTTPStatusError as exc:  # 4xx — permanent for this attachment
                errors.append(f"{att.filename}: HTTP {exc.response.status_code}")

        if errors:
            message.processing_status = ProcessingStatus.error
            message.error_log = "; ".join(f"attachment download failed: {e}" for e in errors)
            session.add(message)

    log.info("attachments.done", message_id=message_id, downloaded=downloaded, errors=len(errors))
    return {"message_id": message_id, "downloaded": downloaded, "errors": len(errors)}


def _downloadable(message: Message, att: Attachment) -> bool:
    """Whether this task can fetch the attachment's bytes."""
    if att.s3_key:
        return False
    if att.source_url:
        return True
    return message.source_system == SourceSystem.telegram and bool(att.external_id)


def _resolve_url(message: Message, att: Attachment) -> str:
    if att.source_url:
        return att.source_url
    # Telegram: turn the file_id into a temporary, token-bearing download URL.
    return resolve_file_url(att.external_id)


def _download_one(session, message: Message, att: Attachment) -> int:
    url = _resolve_url(message, att)
    with _build_client() as client:
        try:
            resp = client.get(url)
        except httpx.TransportError as exc:
            raise RetryableError(f"attachment transport error: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableError(f"attachment server error {resp.status_code}")
        resp.raise_for_status()  # 4xx => HTTPStatusError (permanent)
        data = resp.content

    key = build_object_key(message.source_system.value, message.external_id, att.filename)
    bucket, key = upload_bytes(key, data, content_type=att.content_type)

    att.s3_bucket = bucket
    att.s3_key = key
    att.size_bytes = att.size_bytes or len(data)
    session.add(att)
    return 1
