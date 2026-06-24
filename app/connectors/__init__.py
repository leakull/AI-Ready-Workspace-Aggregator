"""Connector registry.

Each connector knows how to read one source system and yield ``UnifiedMessage``
objects. The registry lets tasks and the API resolve a connector by its source
name without importing every module explicitly.
"""

from __future__ import annotations

from app.connectors.base import BaseConnector
from app.connectors.email import EmailConnector
from app.connectors.github import GitHubConnector
from app.connectors.telegram import TelegramConnector

_REGISTRY: dict[str, type[BaseConnector]] = {
    GitHubConnector.source_system: GitHubConnector,
    TelegramConnector.source_system: TelegramConnector,
    EmailConnector.source_system: EmailConnector,
}


def get_connector(source: str) -> BaseConnector:
    try:
        connector_cls = _REGISTRY[source]
    except KeyError as exc:
        raise ValueError(f"unknown connector source: {source!r}") from exc
    return connector_cls()


def available_sources() -> list[str]:
    return list(_REGISTRY)
