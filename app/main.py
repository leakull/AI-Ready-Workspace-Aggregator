"""FastAPI application entrypoint.

Wires the v1 router, configures structured logging, and installs a middleware
that assigns every request a ``trace_id`` (honoring an inbound
``X-Request-ID``) so API logs correlate the same way task logs do.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import bind_trace_id, configure_logging, get_logger

configure_logging()
log = get_logger(__name__)

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    summary="Aggregates corporate communications and tasks into one store for AI agents.",
)


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    bind_trace_id(trace_id)
    log.info("http.request", method=request.method, path=request.url.path)
    response = await call_next(request)
    response.headers["X-Request-ID"] = trace_id
    return response


@app.exception_handler(ValueError)
async def value_error_handler(_request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}


app.include_router(api_router)
