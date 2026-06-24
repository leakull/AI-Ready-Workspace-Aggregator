"""Celery application: broker, result backend, beat schedule and a task base
that wires every task into the structured-logging ``trace_id``.
"""

from __future__ import annotations

from celery import Celery, Task
from celery.signals import setup_logging

from app.core.config import settings
from app.core.logging import bind_trace_id, configure_logging


class TraceTask(Task):
    """Base task that binds the Celery task id as the ``trace_id`` so logs from
    fetching through persistence share one correlation id."""

    def __call__(self, *args, **kwargs):
        if self.request.id:
            bind_trace_id(self.request.id)
        return super().__call__(*args, **kwargs)


celery_app = Celery(
    "workspace_aggregator",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.sync", "app.tasks.attachments", "app.tasks.embeddings"],
)

celery_app.Task = TraceTask

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_max_tasks_per_child=200,
    result_expires=3600,
    timezone="UTC",
    enable_utc=True,
    # Periodic sync of every configured connector.
    beat_schedule={
        "sync-all-connectors-every-5-min": {
            "task": "app.tasks.sync.sync_all_connectors",
            "schedule": 300.0,
        },
    },
)


@setup_logging.connect
def _on_setup_logging(**_kwargs):
    # Replace Celery's default logging with our structlog config.
    configure_logging()
