"""ORM models.

The core design decision lives here: every ingested item is a ``Message`` made
unique by ``(source_system, external_id)``. That pair backs the idempotent
upsert, so re-running a sync over the same window never creates duplicates.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class SourceSystem(str, enum.Enum):
    github = "github"
    telegram = "telegram"
    email = "email"


class ProcessingStatus(str, enum.Enum):
    raw = "raw"
    normalized = "normalized"
    error = "error"


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # The dedup contract: one row per (source, external id).
        UniqueConstraint("source_system", "external_id", name="uq_messages_source_external"),
        Index("ix_messages_status", "processing_status"),
        Index("ix_messages_source_created_at", "source_system", "source_created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    source_system: Mapped[SourceSystem] = mapped_column(
        Enum(SourceSystem, name="source_system"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    thread_external_id: Mapped[str | None] = mapped_column(String(255))

    author: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(1024))
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(String(2048))

    # When the item was created in the source system.
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Original payload, kept for traceability / re-normalization.
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)

    processing_status: Mapped[ProcessingStatus] = mapped_column(
        Enum(ProcessingStatus, name="processing_status"),
        default=ProcessingStatus.normalized,
        nullable=False,
    )
    error_log: Mapped[str | None] = mapped_column(Text)

    # Audit trail.
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        UniqueConstraint("message_id", "external_id", name="uq_attachments_message_external"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )

    external_id: Mapped[str | None] = mapped_column(String(255))
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)

    # Original URL at the source; downloaded lazily by a Celery task.
    source_url: Mapped[str | None] = mapped_column(String(2048))

    # Populated once stored in S3/MinIO.
    s3_bucket: Mapped[str | None] = mapped_column(String(255))
    s3_key: Mapped[str | None] = mapped_column(String(1024))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    message: Mapped[Message] = relationship(back_populates="attachments")
