"""Pydantic schemas.

``UnifiedMessage`` is the single shape every connector must produce: whatever a
source looks like on the wire, it is normalized into this before it touches the
database. The ``*Read`` schemas are the API's serialization contract.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import ProcessingStatus, SourceSystem


class UnifiedAttachment(BaseModel):
    """Normalized attachment, not yet downloaded to S3."""

    external_id: str | None = None
    filename: str
    content_type: str | None = None
    size_bytes: int | None = None
    source_url: str | None = None


class UnifiedMessage(BaseModel):
    """The canonical, source-agnostic message produced by every connector."""

    source_system: SourceSystem
    external_id: str
    thread_external_id: str | None = None

    author: str | None = None
    title: str | None = None
    body: str = ""
    url: str | None = None

    source_created_at: datetime | None = None

    raw: dict = Field(default_factory=dict)
    attachments: list[UnifiedAttachment] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# API read models
# --------------------------------------------------------------------------- #
class AttachmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    content_type: str | None
    size_bytes: int | None
    s3_bucket: str | None
    s3_key: str | None
    source_url: str | None


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_system: SourceSystem
    external_id: str
    thread_external_id: str | None
    author: str | None
    title: str | None
    body: str
    url: str | None
    source_created_at: datetime | None
    processing_status: ProcessingStatus
    fetched_at: datetime
    created_at: datetime
    updated_at: datetime
    attachments: list[AttachmentRead] = Field(default_factory=list)


class MessageList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[MessageRead]


class SyncResponse(BaseModel):
    task_id: str
    source: str
    detail: str = "sync enqueued"
