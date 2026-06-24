"""Embedding task — OPTIONAL, behind the ``VECTOR_ENABLED`` feature flag.

When enabled, this is where a message's text would be turned into a vector and
upserted into Qdrant so an AI agent can do semantic retrieval. The embedding
provider (local SentenceTransformers vs. a hosted API) is deliberately left
undecided — only the wiring exists. With the flag off the task short-circuits,
so it never sits in the critical ingest path.
"""

from __future__ import annotations

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Message
from app.db.session import session_scope

log = get_logger(__name__)


@celery_app.task(name="app.tasks.embeddings.embed_message")
def embed_message(message_id: int) -> dict:
    if not settings.vector_enabled:
        return {"message_id": message_id, "status": "disabled"}

    with session_scope() as session:
        message = session.get(Message, message_id)
        if message is None:
            return {"message_id": message_id, "status": "missing"}

        text = f"{message.title or ''}\n\n{message.body}".strip()

    # TODO: provider not chosen yet (local SentenceTransformers vs hosted API).
    #   vector = embed(text)
    #   qdrant_client.upsert(collection="messages", points=[(message_id, vector, payload)])
    log.info("embeddings.todo", message_id=message_id, chars=len(text), qdrant=settings.qdrant_url)
    return {"message_id": message_id, "status": "todo"}
