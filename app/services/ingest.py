"""Idempotent persistence of normalized messages.

The whole pipeline's reliability rests on this module: messages are written with
``INSERT ... ON CONFLICT (source_system, external_id) DO UPDATE`` so that
replaying a sync over an already-seen window updates rows in place instead of
duplicating them. Whether a row was inserted or updated is detected with the
Postgres ``xmax = 0`` trick, which lets a sync report honest counts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Attachment, Message
from app.schemas.message import UnifiedAttachment, UnifiedMessage

log = get_logger(__name__)


@dataclass
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    message_ids: list[int] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.inserted + self.updated


# Columns that may legitimately change between syncs of the same item.
_MUTABLE_MESSAGE_FIELDS = (
    "thread_external_id",
    "author",
    "title",
    "body",
    "url",
    "source_created_at",
    "raw",
    "processing_status",
)


def _upsert_message(session: Session, msg: UnifiedMessage) -> tuple[int, bool]:
    """Upsert one message. Returns (id, inserted)."""
    values = msg.model_dump(exclude={"attachments"})

    stmt = insert(Message).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_messages_source_external",
        set_={field: getattr(stmt.excluded, field) for field in _MUTABLE_MESSAGE_FIELDS}
        | {"fetched_at": stmt.excluded.fetched_at, "updated_at": literal_column("now()")},
    ).returning(
        Message.id,
        # xmax = 0 means the row was freshly inserted (no prior tuple version).
        literal_column("(xmax = 0)").label("inserted"),
    )

    row = session.execute(stmt).one()
    return row.id, bool(row.inserted)


def _upsert_attachments(
    session: Session, message_id: int, attachments: Iterable[UnifiedAttachment]
) -> None:
    for att in attachments:
        # Stable dedup key per message; fall back to filename when the source
        # gives no id. The S3 columns are intentionally NOT in the update set so
        # a later download task can fill them without being clobbered.
        external_id = att.external_id or att.filename
        stmt = insert(Attachment).values(
            message_id=message_id,
            external_id=external_id,
            filename=att.filename,
            content_type=att.content_type,
            size_bytes=att.size_bytes,
            source_url=att.source_url,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_attachments_message_external",
            set_={
                "filename": stmt.excluded.filename,
                "content_type": stmt.excluded.content_type,
                "size_bytes": stmt.excluded.size_bytes,
                "source_url": stmt.excluded.source_url,
            },
        )
        session.execute(stmt)


def upsert_messages(session: Session, messages: Iterable[UnifiedMessage]) -> UpsertResult:
    """Persist a batch of normalized messages idempotently.

    The caller owns the transaction boundary (Celery task / request scope).
    """
    result = UpsertResult()
    for msg in messages:
        message_id, inserted = _upsert_message(session, msg)
        _upsert_attachments(session, message_id, msg.attachments)

        result.message_ids.append(message_id)
        if inserted:
            result.inserted += 1
        else:
            result.updated += 1

    session.flush()
    log.info(
        "messages.upserted",
        inserted=result.inserted,
        updated=result.updated,
        total=result.total,
    )
    return result
