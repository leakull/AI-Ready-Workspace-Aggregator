"""Test fixtures.

These tests run against a real Postgres (the dockerized one) because the
idempotency contract relies on Postgres-specific features — ``ON CONFLICT`` and
the ``xmax = 0`` insert/update detection — which SQLite cannot emulate. The
schema is created from the ORM metadata and every test starts from a truncated
database.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select, text

from app.core.config import settings
from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base
from app.db.models import Message
from app.db.session import SessionLocal, engine
from app.schemas.message import SourceSystem, UnifiedAttachment, UnifiedMessage


def _ensure_test_database() -> None:
    """Create the configured database if it does not exist.

    Tests are meant to run against a dedicated DB (``POSTGRES_DB=aggregator_test``)
    so they never touch development data. We connect to the maintenance
    ``postgres`` database to issue ``CREATE DATABASE``.
    """
    if settings.postgres_db == "aggregator":
        raise RuntimeError(
            "Refusing to run tests against the 'aggregator' dev database. "
            "Run with POSTGRES_DB=aggregator_test (see `make test`)."
        )

    admin_url = (
        f"postgresql+psycopg2://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/postgres"
    )
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": settings.postgres_db},
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{settings.postgres_db}"'))
    admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    _ensure_test_database()
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def clean_db(_create_schema):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE attachments, messages RESTART IDENTITY CASCADE"))
    yield


def make_message(
    n: int,
    *,
    source: SourceSystem = SourceSystem.github,
    body: str = "hello world",
    title: str | None = None,
    attachments: list[UnifiedAttachment] | None = None,
) -> UnifiedMessage:
    return UnifiedMessage(
        source_system=source,
        external_id=f"acme/repo#{n}",
        thread_external_id="acme/repo",
        author="octocat",
        title=title or f"Issue {n}",
        body=body,
        url=f"https://example.test/{n}",
        source_created_at=datetime(2026, 1, n % 28 + 1, tzinfo=UTC),
        raw={"number": n},
        attachments=attachments or [],
    )


def count_messages() -> int:
    with SessionLocal() as s:
        return s.execute(select(func.count()).select_from(Message)).scalar_one()
