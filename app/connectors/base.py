"""Connector interface.

A connector is a thin adapter: talk to one source system, normalize whatever it
returns into ``UnifiedMessage``. It must NOT touch the database — persistence is
the ingest service's job. This keeps connectors independently testable and makes
adding a new source a matter of implementing ``fetch``.

``RetryableError`` is the contract between a connector and the Celery layer:
raise it for transient failures (rate limits, 5xx, network blips) and the task
will back off and retry; raise anything else and it is treated as permanent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from app.schemas.message import UnifiedMessage


class RetryableError(Exception):
    """Transient failure that should trigger a Celery retry."""


class ConnectorNotConfigured(Exception):
    """Raised when required credentials/config are missing."""


class BaseConnector(ABC):
    #: source system identifier, must match ``SourceSystem`` enum values
    source_system: str

    def is_configured(self) -> bool:
        """Whether the connector has everything it needs to run."""
        return True

    @abstractmethod
    def fetch(self) -> Iterator[UnifiedMessage]:
        """Yield normalized messages from the source.

        Implementations should raise :class:`RetryableError` for transient
        problems so the Celery task can retry with backoff.
        """
        raise NotImplementedError
