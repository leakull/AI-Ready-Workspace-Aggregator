"""Embedding task — OPTIONAL, behind the ``VECTOR_ENABLED`` feature flag.

When enabled, a message's text is embedded and upserted into Qdrant so an AI
agent can do semantic retrieval (see ``GET /api/v1/search``). The point id is the
message's Postgres id, so a search hit maps straight back to the row. With the
flag off the task short-circuits, keeping it out of the critical ingest path.
"""

from __future__ import annotations

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Message
from app.db.session import session_scope
from app.vector.embeddings import TransientEmbeddingError, get_embedding_provider
from app.vector.qdrant import get_vector_store

log = get_logger(__name__)


@celery_app.task(
    bind=True,
    name="app.tasks.embeddings.embed_message",
    autoretry_for=(TransientEmbeddingError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def embed_message(self, message_id: int) -> dict:
    if not settings.vector_enabled:
        return {"message_id": message_id, "status": "disabled"}

    with session_scope() as session:
        message = session.get(Message, message_id)
        if message is None:
            return {"message_id": message_id, "status": "missing"}
        text = f"{message.title or ''}\n\n{message.body}".strip()
        payload = {
            "message_id": message.id,
            "source_system": message.source_system.value,
            "external_id": message.external_id,
            "title": message.title,
            "url": message.url,
        }

    provider = get_embedding_provider()
    vector = provider.embed([text])[0]
    get_vector_store().upsert(message_id, vector, payload)

    log.info("embeddings.upserted", message_id=message_id, provider=provider.name, dim=len(vector))
    return {"message_id": message_id, "status": "ok", "provider": provider.name}
