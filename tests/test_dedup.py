"""The central guarantee: replaying a sync never duplicates rows."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Message
from app.db.session import SessionLocal, session_scope
from app.schemas.message import UnifiedAttachment
from app.services.ingest import upsert_messages
from tests.conftest import count_messages, make_message


def test_upsert_is_idempotent():
    messages = [make_message(1), make_message(2), make_message(3)]

    with session_scope() as s:
        first = upsert_messages(s, messages)
    assert (first.inserted, first.updated) == (3, 0)

    # Re-run the exact same batch: everything is an update, nothing new.
    with session_scope() as s:
        second = upsert_messages(s, messages)
    assert (second.inserted, second.updated) == (0, 3)

    assert count_messages() == 3


def test_upsert_updates_mutable_fields():
    with session_scope() as s:
        upsert_messages(s, [make_message(1, body="old body", title="old")])
    with session_scope() as s:
        upsert_messages(s, [make_message(1, body="new body", title="new")])

    with SessionLocal() as s:
        msg = s.execute(
            select(Message).where(Message.external_id == "acme/repo#1")
        ).scalar_one()
        assert msg.body == "new body"
        assert msg.title == "new"

    assert count_messages() == 1


def test_attachments_dedup_and_preserve_s3_key():
    att = UnifiedAttachment(external_id="file-1", filename="report.pdf", source_url="http://x/y")
    with session_scope() as s:
        upsert_messages(s, [make_message(1, attachments=[att])])

    # Simulate a download task having stored the object in S3.
    with session_scope() as s:
        msg = s.execute(
            select(Message).where(Message.external_id == "acme/repo#1")
        ).scalar_one()
        msg.attachments[0].s3_bucket = "attachments"
        msg.attachments[0].s3_key = "github/acme/repo#1/report.pdf"

    # Re-sync the same message: attachment must not be duplicated and the
    # previously stored S3 key must survive.
    with session_scope() as s:
        upsert_messages(s, [make_message(1, attachments=[att])])

    with SessionLocal() as s:
        msg = s.execute(
            select(Message).where(Message.external_id == "acme/repo#1")
        ).scalar_one()
        assert len(msg.attachments) == 1
        assert msg.attachments[0].s3_key == "github/acme/repo#1/report.pdf"
