"""Vector module: hash embedder, embed task, and the /search endpoint.

Qdrant and any hosted provider are stubbed, so these run with no vector server
and no API keys. The hash provider is exercised for real (it has no deps).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.db.models import Message
from app.db.session import SessionLocal, session_scope
from app.main import app
from app.services.ingest import upsert_messages
from app.tasks.embeddings import embed_message
from app.vector.embeddings import HashEmbeddingProvider
from tests.conftest import make_message

client = TestClient(app)


def _seed(messages) -> dict[str, int]:
    with session_scope() as s:
        upsert_messages(s, messages)
    with SessionLocal() as s:
        rows = s.execute(select(Message)).scalars().all()
        return {m.external_id: m.id for m in rows}


# --------------------------------------------------------------------------- #
# Hash provider
# --------------------------------------------------------------------------- #
def test_hash_embedding_is_deterministic_and_normalized():
    provider = HashEmbeddingProvider(dim=64)
    v1 = provider.embed(["deploy failed on staging"])[0]
    v2 = provider.embed(["deploy failed on staging"])[0]

    assert v1 == v2
    assert len(v1) == 64
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-9  # unit length


def test_hash_embedding_overlap_scores_higher():
    p = HashEmbeddingProvider(dim=512)
    base = p.embed(["database connection error on staging"])[0]
    near = p.embed(["database connection error on production"])[0]
    far = p.embed(["lunch menu for friday"])[0]

    def dot(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert dot(base, near) > dot(base, far)


# --------------------------------------------------------------------------- #
# embed_message task
# --------------------------------------------------------------------------- #
class FakeStore:
    def __init__(self):
        self.points: list[tuple[int, list[float], dict]] = []

    def upsert(self, point_id, vector, payload):
        self.points.append((point_id, vector, payload))

    def search(self, vector, limit=10):
        return []


def test_embed_message_disabled(monkeypatch):
    monkeypatch.setattr(settings, "vector_enabled", False)
    assert embed_message(123)["status"] == "disabled"


def test_embed_message_upserts_vector(monkeypatch):
    ids = _seed([make_message(1, body="deploy failed")])
    message_id = ids["acme/repo#1"]

    store = FakeStore()
    monkeypatch.setattr(settings, "vector_enabled", True)
    monkeypatch.setattr("app.tasks.embeddings.get_vector_store", lambda: store)
    monkeypatch.setattr(
        "app.tasks.embeddings.get_embedding_provider", lambda: HashEmbeddingProvider(dim=64)
    )

    result = embed_message(message_id)
    assert result["status"] == "ok"
    assert len(store.points) == 1
    pid, vector, payload = store.points[0]
    assert pid == message_id
    assert len(vector) == 64
    assert payload["source_system"] == "github"
    assert payload["external_id"] == "acme/repo#1"


# --------------------------------------------------------------------------- #
# /search endpoint
# --------------------------------------------------------------------------- #
def test_search_disabled_returns_503(monkeypatch):
    monkeypatch.setattr(settings, "vector_enabled", False)
    assert client.get("/api/v1/search", params={"q": "anything"}).status_code == 503


def test_search_returns_ranked_messages(monkeypatch):
    ids = _seed([make_message(1, body="deploy failed"), make_message(2, body="please review")])
    id1, id2 = ids["acme/repo#1"], ids["acme/repo#2"]

    class SearchStore(FakeStore):
        def search(self, vector, limit=10):
            return [SimpleNamespace(id=id1, score=0.95), SimpleNamespace(id=id2, score=0.40)]

    monkeypatch.setattr(settings, "vector_enabled", True)
    monkeypatch.setattr("app.api.v1.search.get_vector_store", lambda: SearchStore())
    monkeypatch.setattr(
        "app.api.v1.search.get_embedding_provider", lambda: HashEmbeddingProvider(dim=64)
    )

    resp = client.get("/api/v1/search", params={"q": "deploy"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "hash"
    assert [r["message"]["external_id"] for r in body["results"]] == ["acme/repo#1", "acme/repo#2"]
    assert body["results"][0]["score"] == pytest.approx(0.95)
