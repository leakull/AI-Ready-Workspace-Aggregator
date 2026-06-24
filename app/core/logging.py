"""Structured logging via structlog.

Every log line is emitted as JSON (in non-local environments) and carries a
``trace_id`` taken from a context variable. The HTTP middleware and the Celery
task base both bind a ``trace_id`` so a single message can be followed from the
moment it is fetched until it is persisted.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

from app.core.config import settings

# Holds the current trace id for the running request / task.
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def bind_trace_id(trace_id: str) -> None:
    trace_id_var.set(trace_id)


def _add_trace_id(_logger, _method, event_dict):
    trace_id = trace_id_var.get()
    if trace_id is not None:
        event_dict["trace_id"] = trace_id
    return event_dict


def configure_logging() -> None:
    """Idempotently configure stdlib logging + structlog processors."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    renderer = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _add_trace_id,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
