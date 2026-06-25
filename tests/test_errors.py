"""Per-message error capture: a permanent attachment failure marks the message
``error`` with an ``error_log`` surfaced through the API; a re-sync clears it."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.connectors.base import RetryableError
from app.db.models import Message, ProcessingStatus
from app.db.session import SessionLocal, session_scope
from app.main import app
from app.schemas.message import UnifiedAttachment
from app.services.ingest import upsert_messages
from app.tasks.attachments import _download_one, download_attachments
from tests.conftest import make_message

client = TestClient(app)


def _seed_with_attachment() -> int:
    att = UnifiedAttachment(
        external_id="a1", filename="trace.bin", source_url="http://files.test/trace.bin"
    )
    with session_scope() as s:
        upsert_messages(s, [make_message(1, attachments=[att])])
    with SessionLocal() as s:
        return s.execute(
            select(Message).where(Message.external_id == "acme/repo#1")
        ).scalar_one().id


def test_attachment_404_records_error_log(monkeypatch):
    message_id = _seed_with_attachment()

    transport = httpx.MockTransport(lambda r: httpx.Response(404))
    monkeypatch.setattr(
        "app.tasks.attachments._build_client", lambda: httpx.Client(transport=transport)
    )
    monkeypatch.setattr("app.tasks.attachments.ensure_bucket", lambda *a, **k: None)

    result = download_attachments(message_id)
    assert result["errors"] == 1

    with SessionLocal() as s:
        msg = s.get(Message, message_id)
        assert msg.processing_status == ProcessingStatus.error
        assert "404" in msg.error_log

    # Surfaced through the API (per-source: source + status filters compose).
    items = client.get("/api/v1/messages", params={"status": "error"}).json()["items"]
    hit = next(i for i in items if i["id"] == message_id)
    assert "404" in hit["error_log"]


def test_resync_clears_error():
    message_id = _seed_with_attachment()
    with session_scope() as s:
        msg = s.get(Message, message_id)
        msg.processing_status = ProcessingStatus.error
        msg.error_log = "attachment download failed: trace.bin: HTTP 404"

    att = UnifiedAttachment(
        external_id="a1", filename="trace.bin", source_url="http://files.test/trace.bin"
    )
    with session_scope() as s:
        upsert_messages(s, [make_message(1, attachments=[att])])

    with SessionLocal() as s:
        msg = s.get(Message, message_id)
        assert msg.processing_status == ProcessingStatus.normalized
        assert msg.error_log is None


def test_download_one_5xx_is_retryable(monkeypatch):
    transport = httpx.MockTransport(lambda r: httpx.Response(503))
    monkeypatch.setattr(
        "app.tasks.attachments._build_client", lambda: httpx.Client(transport=transport)
    )

    att = SimpleNamespace(source_url="http://files.test/x", filename="x.bin", content_type=None)
    with pytest.raises(RetryableError):
        _download_one(None, SimpleNamespace(), att)
