"""Connector management API.

Lets an operator (or the agent) list configured connectors and force a sync,
which is dispatched to Celery rather than run inline so the request returns
immediately with a task id.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.connectors import available_sources, get_connector
from app.schemas.message import SyncResponse
from app.tasks.sync import sync_connector

router = APIRouter(prefix="/connectors", tags=["connectors"])


class ConnectorInfo(BaseModel):
    source: str
    configured: bool


@router.get("", response_model=list[ConnectorInfo])
def list_connectors() -> list[ConnectorInfo]:
    infos = []
    for source in available_sources():
        connector = get_connector(source)
        infos.append(ConnectorInfo(source=source, configured=connector.is_configured()))
    return infos


@router.post("/{source}/sync", response_model=SyncResponse, status_code=202)
def trigger_sync(source: str) -> SyncResponse:
    if source not in available_sources():
        raise HTTPException(status_code=404, detail=f"unknown connector: {source}")
    task = sync_connector.delay(source)
    return SyncResponse(task_id=task.id, source=source)
