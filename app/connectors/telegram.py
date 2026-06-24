"""Telegram connector — polling via ``getUpdates``.

Pulls bot updates with an offset cursor (no public URL needed, unlike webhooks),
normalizes each message into a :class:`UnifiedMessage`, and captures document /
photo attachments as metadata. The offset is advanced only after the sync task
commits (see :meth:`on_sync_committed`), so a failed commit re-fetches the same
window instead of losing updates — Telegram keeps undelivered updates for ~24h.

``external_id`` is ``"{chat_id}:{message_id}"`` because ``message_id`` is only
unique within a chat. Attachment bytes are not downloaded here: Telegram requires
a separate ``getFile`` call to resolve a download path, left as a follow-up.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx

from app.connectors.base import BaseConnector, ConnectorNotConfigured, RetryableError
from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.message import SourceSystem, UnifiedAttachment, UnifiedMessage
from app.services.cursor import CursorStore, RedisCursorStore

log = get_logger(__name__)

TELEGRAM_API = "https://api.telegram.org"
CURSOR_KEY = "connector:telegram:offset"
ALLOWED_UPDATES = ["message", "edited_message"]


class TelegramConnector(BaseConnector):
    source_system = SourceSystem.telegram.value

    def __init__(self, token: str | None = None, cursor: CursorStore | None = None):
        self.token = token if token is not None else settings.telegram_bot_token
        self.cursor = cursor if cursor is not None else RedisCursorStore()
        self._next_offset: int | None = None

    def is_configured(self) -> bool:
        return bool(self.token)

    # -- HTTP (overridable in tests) ----------------------------------------
    def _build_client(self) -> httpx.Client:
        return httpx.Client(timeout=30.0)

    @property
    def _base(self) -> str:
        return f"{TELEGRAM_API}/bot{self.token}"

    def _check_response(self, resp: httpx.Response) -> None:
        if resp.status_code == 429:
            retry_after = (resp.json().get("parameters") or {}).get("retry_after", "?")
            raise RetryableError(f"telegram rate limited, retry_after={retry_after}")
        if resp.status_code >= 500:
            raise RetryableError(f"telegram server error {resp.status_code}")
        resp.raise_for_status()  # 4xx (e.g. 401 bad token) => permanent

    # -- fetch --------------------------------------------------------------
    def fetch(self) -> Iterator[UnifiedMessage]:
        if not self.is_configured():
            raise ConnectorNotConfigured("TELEGRAM_BOT_TOKEN is not set")

        offset = self.cursor.get_int(CURSOR_KEY, 0)
        params: dict = {
            "timeout": 0,  # scheduled short poll, do not block the worker
            "allowed_updates": json.dumps(ALLOWED_UPDATES),
        }
        if offset:
            params["offset"] = offset

        with self._build_client() as client:
            try:
                resp = client.get(f"{self._base}/getUpdates", params=params)
            except httpx.TransportError as exc:
                raise RetryableError(f"telegram transport error: {exc}") from exc
            self._check_response(resp)
            payload = resp.json()

        if not payload.get("ok"):
            raise RetryableError(f"telegram api not ok: {payload.get('description')}")

        updates = payload.get("result", [])
        log.info("telegram.fetched_updates", count=len(updates), offset=offset)

        max_update_id: int | None = None
        for update in updates:
            update_id = update.get("update_id")
            if update_id is not None:
                max_update_id = (
                    update_id if max_update_id is None else max(max_update_id, update_id)
                )
            message = update.get("message") or update.get("edited_message")
            if message:
                yield self._normalize(message)

        # Stage the next offset; committed for real in on_sync_committed().
        if max_update_id is not None:
            self._next_offset = max_update_id + 1

    def on_sync_committed(self) -> None:
        if self._next_offset is not None:
            self.cursor.set_int(CURSOR_KEY, self._next_offset)
            log.info("telegram.cursor_advanced", next_offset=self._next_offset)
            self._next_offset = None

    # -- normalization ------------------------------------------------------
    def _normalize(self, message: dict) -> UnifiedMessage:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        date = message.get("date")

        return UnifiedMessage(
            source_system=SourceSystem.telegram,
            external_id=f"{chat_id}:{message_id}",
            thread_external_id=str(chat_id) if chat_id is not None else None,
            author=sender.get("username") or sender.get("first_name") or _maybe_id(sender),
            title=chat.get("title"),
            body=message.get("text") or message.get("caption") or "",
            url=None,
            source_created_at=datetime.fromtimestamp(date, tz=UTC) if date else None,
            raw=message,
            attachments=_extract_attachments(message),
        )


def _maybe_id(sender: dict) -> str | None:
    return str(sender["id"]) if sender.get("id") is not None else None


def _extract_attachments(message: dict) -> list[UnifiedAttachment]:
    attachments: list[UnifiedAttachment] = []

    document = message.get("document")
    if document:
        attachments.append(
            UnifiedAttachment(
                external_id=document.get("file_id"),
                filename=document.get("file_name") or document.get("file_unique_id") or "document",
                content_type=document.get("mime_type"),
                size_bytes=document.get("file_size"),
            )
        )

    photos = message.get("photo")  # array of progressively larger sizes
    if photos:
        largest = max(photos, key=lambda p: p.get("file_size") or 0)
        attachments.append(
            UnifiedAttachment(
                external_id=largest.get("file_id"),
                filename=f"{largest.get('file_unique_id', 'photo')}.jpg",
                content_type="image/jpeg",
                size_bytes=largest.get("file_size"),
            )
        )

    return attachments
