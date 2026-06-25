"""Telegram connector: normalization, idempotency, retry mapping, cursor.

Telegram is mocked with ``httpx.MockTransport`` so these run without a bot token
or network. Persistence goes through the real Postgres to exercise the same
idempotent upsert the GitHub slice uses.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.connectors.base import ConnectorNotConfigured, RetryableError
from app.connectors.telegram import CURSOR_KEY, TelegramConnector, resolve_file_url
from app.db.models import Message
from app.db.session import SessionLocal, session_scope
from app.schemas.message import SourceSystem, UnifiedAttachment, UnifiedMessage
from app.services.cursor import InMemoryCursorStore
from app.services.ingest import upsert_messages
from app.tasks.attachments import download_attachments
from tests.conftest import count_messages

UPDATES_PAYLOAD = {
    "ok": True,
    "result": [
        {
            "update_id": 101,
            "message": {
                "message_id": 11,
                "date": 1700000000,
                "chat": {"id": -100, "type": "group", "title": "Team"},
                "from": {"id": 1, "username": "alice"},
                "text": "deploy failed on staging",
            },
        },
        {
            "update_id": 102,
            "message": {
                "message_id": 12,
                "date": 1700000100,
                "chat": {"id": -100, "type": "group", "title": "Team"},
                "from": {"id": 2, "first_name": "Bob"},
                "text": "on it",
                "document": {
                    "file_id": "DOC1",
                    "file_name": "log.txt",
                    "mime_type": "text/plain",
                    "file_size": 1234,
                },
            },
        },
        # Non-message update: skipped, but the offset must still advance past it.
        {"update_id": 103, "callback_query": {"id": "cb1"}},
    ],
}


def make_connector(payload, *, status=200, cursor=None) -> TelegramConnector:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    transport = httpx.MockTransport(handler)
    conn = TelegramConnector(token="TESTTOKEN", cursor=cursor or InMemoryCursorStore())
    conn._build_client = lambda: httpx.Client(transport=transport)  # type: ignore[method-assign]
    return conn


def test_normalizes_and_upserts():
    conn = make_connector(UPDATES_PAYLOAD)
    with session_scope() as s:
        result = upsert_messages(s, conn.fetch())

    assert (result.inserted, result.updated) == (2, 0)  # callback_query skipped

    with SessionLocal() as s:
        msg = s.execute(
            select(Message).where(Message.external_id == "-100:11")
        ).scalar_one()
        assert msg.author == "alice"
        assert msg.body == "deploy failed on staging"
        assert msg.thread_external_id == "-100"
        assert msg.title == "Team"

        with_doc = s.execute(
            select(Message).where(Message.external_id == "-100:12")
        ).scalar_one()
        assert with_doc.author == "Bob"
        assert len(with_doc.attachments) == 1
        assert with_doc.attachments[0].filename == "log.txt"


def test_cursor_advances_past_all_updates_after_commit():
    cursor = InMemoryCursorStore()
    conn = make_connector(UPDATES_PAYLOAD, cursor=cursor)

    with session_scope() as s:
        upsert_messages(s, conn.fetch())

    # Not advanced until the task confirms the commit.
    assert cursor.get_int(CURSOR_KEY) == 0
    conn.on_sync_committed()
    # max update_id is 103 (the skipped callback) -> next offset 104.
    assert cursor.get_int(CURSOR_KEY) == 104


def test_idempotent_redelivery():
    cursor = InMemoryCursorStore()

    conn1 = make_connector(UPDATES_PAYLOAD, cursor=cursor)
    with session_scope() as s:
        upsert_messages(s, conn1.fetch())
    conn1.on_sync_committed()

    # Telegram redelivering the same updates must not create duplicates.
    conn2 = make_connector(UPDATES_PAYLOAD, cursor=cursor)
    with session_scope() as s:
        again = upsert_messages(s, conn2.fetch())

    assert again.inserted == 0
    assert count_messages() == 2


def test_server_error_is_retryable():
    conn = make_connector({}, status=500)
    with pytest.raises(RetryableError):
        list(conn.fetch())


def test_rate_limit_is_retryable():
    conn = make_connector({"ok": False, "parameters": {"retry_after": 5}}, status=429)
    with pytest.raises(RetryableError):
        list(conn.fetch())


def test_bad_token_is_permanent():
    conn = make_connector({"ok": False, "error_code": 401}, status=401)
    with pytest.raises(httpx.HTTPStatusError):  # not RetryableError
        list(conn.fetch())


def test_not_configured():
    conn = TelegramConnector(token=None, cursor=InMemoryCursorStore())
    with pytest.raises(ConnectorNotConfigured):
        list(conn.fetch())


# --------------------------------------------------------------------------- #
# Attachments: getFile -> S3
# --------------------------------------------------------------------------- #
def test_resolve_file_url_builds_download_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getFile")
        assert request.url.params["file_id"] == "FILEID1"
        return httpx.Response(
            200,
            json={"ok": True, "result": {"file_id": "FILEID1", "file_path": "documents/f_5.bin"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    url = resolve_file_url("FILEID1", token="TESTTOKEN", client=client)
    assert url == "https://api.telegram.org/file/botTESTTOKEN/documents/f_5.bin"


def test_download_telegram_attachment_to_s3(monkeypatch):
    message = UnifiedMessage(
        source_system=SourceSystem.telegram,
        external_id="-100:55",
        thread_external_id="-100",
        body="see file",
        attachments=[
            UnifiedAttachment(
                external_id="FILEID1", filename="doc.bin", content_type="application/pdf"
            )
        ],
    )
    with session_scope() as s:
        upsert_messages(s, [message])
    with SessionLocal() as s:
        mid = s.execute(
            select(Message).where(Message.external_id == "-100:55")
        ).scalar_one().id

    uploads: list = []
    monkeypatch.setattr(
        "app.tasks.attachments.resolve_file_url", lambda file_id: f"http://tg-file/{file_id}"
    )
    monkeypatch.setattr("app.tasks.attachments.ensure_bucket", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.tasks.attachments.upload_bytes",
        lambda key, data, content_type=None, bucket=None: (
            uploads.append((key, len(data))) or ("attachments", key)
        ),
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"PDF-BYTES"))
    monkeypatch.setattr(
        "app.tasks.attachments._build_client", lambda: httpx.Client(transport=transport)
    )

    result = download_attachments(mid)
    assert result["downloaded"] == 1

    with SessionLocal() as s:
        att = s.get(Message, mid).attachments[0]
        assert att.s3_bucket == "attachments"
        assert att.s3_key == "telegram/-100:55/doc.bin"
        assert att.size_bytes == len(b"PDF-BYTES")
