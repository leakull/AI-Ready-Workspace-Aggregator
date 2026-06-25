"""Qdrant vector store wrapper.

Thin layer over ``qdrant-client``: lazily create the collection (sized to the
active provider's vector dimension) and upsert / search points. A message's
Postgres ``id`` is reused as the Qdrant point id so search hits map straight back
to rows.
"""

from __future__ import annotations

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.core.config import settings
from app.core.logging import get_logger
from app.vector.embeddings import get_embedding_provider

log = get_logger(__name__)


class QdrantStore:
    def __init__(self, url: str, collection: str, dim: int):
        self.client = QdrantClient(url=url)
        self.collection = collection
        self.dim = dim
        self._ensured = False

    def ensure_collection(self) -> None:
        if self._ensured:
            return
        names = {c.name for c in self.client.get_collections().collections}
        if self.collection not in names:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
            )
            log.info("qdrant.collection_created", collection=self.collection, dim=self.dim)
        self._ensured = True

    def upsert(self, point_id: int, vector: list[float], payload: dict) -> None:
        self.ensure_collection()
        self.client.upsert(
            collection_name=self.collection,
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
        )

    def search(self, vector: list[float], limit: int = 10):
        self.ensure_collection()
        return self.client.search(
            collection_name=self.collection, query_vector=vector, limit=limit
        )


def get_vector_store() -> QdrantStore:
    # Collection size must match the active provider's output dimension.
    provider = get_embedding_provider()
    return QdrantStore(
        url=settings.qdrant_url, collection=settings.qdrant_collection, dim=provider.dim
    )
