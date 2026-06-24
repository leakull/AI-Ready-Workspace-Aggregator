"""Read API consumed by the AI agent.

Exposes the normalized message store with filtering and pagination — the context
an agent queries when composing a reply.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Message, ProcessingStatus, SourceSystem
from app.db.session import get_db
from app.schemas.message import MessageList, MessageRead

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("", response_model=MessageList)
def list_messages(
    db: Session = Depends(get_db),
    source: SourceSystem | None = Query(default=None, description="Filter by source system"),
    status: ProcessingStatus | None = Query(
        default=None, description="Filter by processing status"
    ),
    q: str | None = Query(default=None, description="Case-insensitive search in title/body"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> MessageList:
    filters = []
    if source is not None:
        filters.append(Message.source_system == source)
    if status is not None:
        filters.append(Message.processing_status == status)
    if q:
        pattern = f"%{q}%"
        filters.append(Message.title.ilike(pattern) | Message.body.ilike(pattern))

    total = db.execute(select(func.count()).select_from(Message).where(*filters)).scalar_one()

    rows = (
        db.execute(
            select(Message)
            .where(*filters)
            .order_by(Message.source_created_at.desc().nullslast(), Message.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )

    return MessageList(
        total=total,
        limit=limit,
        offset=offset,
        items=[MessageRead.model_validate(r) for r in rows],
    )


@router.get("/{message_id}", response_model=MessageRead)
def get_message(message_id: int, db: Session = Depends(get_db)) -> MessageRead:
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    return MessageRead.model_validate(message)
