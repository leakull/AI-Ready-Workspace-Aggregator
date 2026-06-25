"""Semantic search API — the AI agent's retrieval entrypoint.

Embeds the query with the active provider, asks Qdrant for the nearest message
vectors, then hydrates the hits from Postgres. Gated by ``VECTOR_ENABLED``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Message
from app.db.session import get_db
from app.schemas.message import MessageRead, SearchHit, SearchResponse
from app.vector.embeddings import get_embedding_provider
from app.vector.qdrant import get_vector_store

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchResponse)
def search(
    q: str = Query(min_length=1, description="Natural-language query"),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
) -> SearchResponse:
    if not settings.vector_enabled:
        raise HTTPException(
            status_code=503, detail="vector search disabled (set VECTOR_ENABLED=true)"
        )

    provider = get_embedding_provider()
    vector = provider.embed([q])[0]
    hits = get_vector_store().search(vector, limit=limit)

    scores = {int(h.id): h.score for h in hits}
    if not scores:
        return SearchResponse(query=q, provider=provider.name, results=[])

    rows = db.execute(select(Message).where(Message.id.in_(scores.keys()))).scalars().all()
    by_id = {m.id: m for m in rows}

    # Preserve Qdrant's relevance order.
    results = [
        SearchHit(score=scores[mid], message=MessageRead.model_validate(by_id[mid]))
        for mid in sorted(scores, key=lambda m: scores[m], reverse=True)
        if mid in by_id
    ]
    return SearchResponse(query=q, provider=provider.name, results=results)
