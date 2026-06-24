from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import connectors, messages

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(messages.router)
api_router.include_router(connectors.router)
