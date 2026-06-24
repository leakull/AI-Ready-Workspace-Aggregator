"""Sync tasks.

``sync_connector`` is the workhorse: pull from one source, upsert idempotently,
optionally fan out attachment/embedding work. It retries with exponential
backoff on :class:`RetryableError` (rate limits / 5xx / network), and because the
upsert is idempotent a retry can safely re-process the same window.

``sync_all_connectors`` is the Beat entrypoint that fans out to every configured
connector.
"""

from __future__ import annotations

from app.connectors import available_sources, get_connector
from app.connectors.base import ConnectorNotConfigured, RetryableError
from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import session_scope
from app.services.ingest import upsert_messages

log = get_logger(__name__)

RETRY_BACKOFF_MAX = 600  # cap exponential backoff at 10 minutes
MAX_RETRIES = 5


@celery_app.task(
    bind=True,
    name="app.tasks.sync.sync_connector",
    autoretry_for=(RetryableError,),
    retry_backoff=True,            # 1s, 2s, 4s, ... exponential
    retry_backoff_max=RETRY_BACKOFF_MAX,
    retry_jitter=True,            # avoid thundering herd on shared rate limits
    max_retries=MAX_RETRIES,
)
def sync_connector(self, source: str) -> dict:
    """Sync a single source system into the message store."""
    log.info("sync.start", source=source, attempt=self.request.retries)

    connector = get_connector(source)
    if not connector.is_configured():
        log.warning("sync.skipped_not_configured", source=source)
        return {"source": source, "status": "skipped", "reason": "not configured"}

    try:
        with session_scope() as session:
            result = upsert_messages(session, connector.fetch())
            message_ids = list(result.message_ids)
    except ConnectorNotConfigured as exc:
        log.warning("sync.skipped_not_configured", source=source, error=str(exc))
        return {"source": source, "status": "skipped", "reason": str(exc)}
    # RetryableError propagates to Celery's autoretry machinery.

    # Fan out optional post-processing (each is a no-op unless enabled/needed).
    _enqueue_followups(message_ids)

    log.info(
        "sync.done",
        source=source,
        inserted=result.inserted,
        updated=result.updated,
        total=result.total,
    )
    return {
        "source": source,
        "status": "ok",
        "inserted": result.inserted,
        "updated": result.updated,
        "total": result.total,
    }


def _enqueue_followups(message_ids: list[int]) -> None:
    """Kick off attachment download and (optional) embedding per message."""
    from app.tasks.attachments import download_attachments
    from app.tasks.embeddings import embed_message

    for message_id in message_ids:
        download_attachments.delay(message_id)
        if settings.vector_enabled:
            embed_message.delay(message_id)


@celery_app.task(name="app.tasks.sync.sync_all_connectors")
def sync_all_connectors() -> dict:
    """Beat entrypoint: enqueue a sync for every registered connector."""
    enqueued = []
    for source in available_sources():
        sync_connector.delay(source)
        enqueued.append(source)
    log.info("sync.all_enqueued", sources=enqueued)
    return {"enqueued": enqueued}
