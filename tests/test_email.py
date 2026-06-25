"""Email connector: MIME parsing, attachment→S3 (mocked), idempotency,
thread linking, and the mark-seen-after-commit flow.

IMAP and S3 are stubbed, so these run without a mail server or MinIO. Real MIME
bytes are built with the stdlib so the parser is genuinely exercised.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest
from sqlalchemy import select

from app.connectors.base import ConnectorNotConfigured, RetryableError
from app.connectors.email import EmailConnector
from app.db.models import Message
from app.db.session import SessionLocal, session_scope
from app.services.ingest import upsert_messages
from tests.conftest import count_messages


def build_email(
    message_id: str,
    subject: str,
    from_: str,
    body: str,
    *,
    attachments=None,
    in_reply_to: str | None = None,
) -> bytes:
    m = EmailMessage()
    m["Message-ID"] = message_id
    m["Subject"] = subject
    m["From"] = from_
    m["To"] = "team@example.test"
    m["Date"] = "Tue, 24 Jun 2026 10:00:00 +0000"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    m.set_content(body)
    for filename, data, content_type in attachments or []:
        maintype, subtype = content_type.split("/", 1)
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return m.as_bytes()


def make_connector(raw_messages, monkeypatch, *, uploads=None) -> EmailConnector:
    conn = EmailConnector(host="imap.test", port=993, user="u", password="p")
    monkeypatch.setattr(conn, "_fetch_raw_messages", lambda: list(raw_messages))

    seen: list[str] = []
    monkeypatch.setattr(conn, "_mark_seen", seen.extend)
    conn.seen = seen  # type: ignore[attr-defined]  (test handle)

    def fake_upload(key, data, content_type=None, bucket=None):
        if uploads is not None:
            uploads.append((key, len(data), content_type))
        return ("attachments", key)

    monkeypatch.setattr("app.connectors.email.upload_bytes", fake_upload)
    monkeypatch.setattr("app.connectors.email.ensure_bucket", lambda *a, **k: None)
    return conn


def test_parses_and_uploads_attachment(monkeypatch):
    raw = build_email(
        "<m1@example.test>",
        "Deploy failed",
        "Alice <alice@example.test>",
        "see attached log",
        attachments=[("log.txt", b"boom", "text/plain")],
    )
    uploads: list = []
    conn = make_connector([("1", raw)], monkeypatch, uploads=uploads)

    with session_scope() as s:
        result = upsert_messages(s, conn.fetch())
    assert result.inserted == 1

    assert len(uploads) == 1
    key, size, content_type = uploads[0]
    assert key == "email/<m1@example.test>/log.txt"
    assert size == 4

    with SessionLocal() as s:
        msg = s.execute(
            select(Message).where(Message.external_id == "<m1@example.test>")
        ).scalar_one()
        assert msg.title == "Deploy failed"
        assert "alice@example.test" in msg.author
        assert "see attached log" in msg.body
        assert len(msg.attachments) == 1
        att = msg.attachments[0]
        assert att.filename == "log.txt"
        assert att.s3_bucket == "attachments"
        assert att.s3_key == "email/<m1@example.test>/log.txt"


def test_seen_only_after_commit(monkeypatch):
    raw = build_email("<m2@example.test>", "Hi", "bob@example.test", "hello")
    conn = make_connector([("7", raw)], monkeypatch)

    with session_scope() as s:
        upsert_messages(s, conn.fetch())

    assert conn.seen == []  # not marked seen yet
    conn.on_sync_committed()
    assert conn.seen == ["7"]


def test_idempotent(monkeypatch):
    raw = build_email("<m3@example.test>", "Repeat", "a@example.test", "body")

    conn1 = make_connector([("1", raw)], monkeypatch)
    with session_scope() as s:
        upsert_messages(s, conn1.fetch())

    conn2 = make_connector([("1", raw)], monkeypatch)
    with session_scope() as s:
        again = upsert_messages(s, conn2.fetch())

    assert again.inserted == 0
    assert count_messages() == 1


def test_thread_linking(monkeypatch):
    reply = build_email(
        "<reply@example.test>",
        "Re: Deploy failed",
        "carol@example.test",
        "fixed it",
        in_reply_to="<m1@example.test>",
    )
    conn = make_connector([("1", reply)], monkeypatch)
    with session_scope() as s:
        upsert_messages(s, conn.fetch())

    with SessionLocal() as s:
        msg = s.execute(
            select(Message).where(Message.external_id == "<reply@example.test>")
        ).scalar_one()
        assert msg.thread_external_id == "<m1@example.test>"


def test_not_configured():
    conn = EmailConnector(host=None, user=None, password=None)
    with pytest.raises(ConnectorNotConfigured):
        list(conn.fetch())


def test_imap_error_is_retryable(monkeypatch):
    conn = EmailConnector(host="imap.test", user="u", password="p")

    def boom():
        raise OSError("connection refused")

    monkeypatch.setattr(conn, "_fetch_raw_messages", boom)
    with pytest.raises(RetryableError):
        list(conn.fetch())
