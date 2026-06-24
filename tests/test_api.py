"""API surface: health, message read endpoints, connector listing, and the
POST sync path exercised end-to-end with Celery in eager mode (no broker)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.celery_app import celery_app
from app.db.session import session_scope
from app.main import app
from app.schemas.message import SourceSystem
from app.services.ingest import upsert_messages
from tests.conftest import count_messages, make_message

client = TestClient(app)


def _seed(messages):
    with session_scope() as s:
        upsert_messages(s, messages)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_list_and_filter_messages():
    _seed(
        [
            make_message(1, source=SourceSystem.github, body="deploy failed on staging"),
            make_message(2, source=SourceSystem.github, body="please review the PR"),
        ]
    )

    resp = client.get("/api/v1/messages", params={"source": "github"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 2
    assert len(payload["items"]) == 2

    # Case-insensitive search hits the body.
    resp = client.get("/api/v1/messages", params={"q": "DEPLOY"})
    assert resp.json()["total"] == 1

    # Filtering by a source with no rows yields nothing.
    resp = client.get("/api/v1/messages", params={"source": "telegram"})
    assert resp.json()["total"] == 0


def test_get_message_by_id():
    _seed([make_message(1)])
    listing = client.get("/api/v1/messages").json()
    message_id = listing["items"][0]["id"]

    resp = client.get(f"/api/v1/messages/{message_id}")
    assert resp.status_code == 200
    assert resp.json()["external_id"] == "acme/repo#1"

    assert client.get("/api/v1/messages/999999").status_code == 404


def test_list_connectors():
    resp = client.get("/api/v1/connectors")
    assert resp.status_code == 200
    sources = {c["source"] for c in resp.json()}
    assert {"github", "telegram", "email"} <= sources


def test_trigger_sync_runs_pipeline(monkeypatch):
    """POST /connectors/{source}/sync -> Celery task -> idempotent upsert."""

    class FakeConnector:
        def is_configured(self):
            return True

        def fetch(self):
            yield from [make_message(10), make_message(11)]

    monkeypatch.setattr("app.tasks.sync.get_connector", lambda source: FakeConnector())
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", True)

    resp = client.post("/api/v1/connectors/github/sync")
    assert resp.status_code == 202
    assert resp.json()["source"] == "github"
    assert resp.json()["task_id"]

    assert count_messages() == 2


def test_trigger_sync_unknown_source():
    assert client.post("/api/v1/connectors/nope/sync").status_code == 404
