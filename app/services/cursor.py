"""Polling cursors for connectors.

Some sources (Telegram's ``getUpdates``) are consumed incrementally and need a
small piece of persistent state — the next offset to request. That state is kept
in Redis (already a dependency), deliberately *separate* from the message store
so connectors never reach into the ORM. An in-memory implementation is provided
for tests.
"""

from __future__ import annotations

from typing import Protocol

import redis

from app.core.config import settings


class CursorStore(Protocol):
    def get_int(self, key: str, default: int = 0) -> int: ...
    def set_int(self, key: str, value: int) -> None: ...
    def get_str(self, key: str, default: str | None = None) -> str | None: ...
    def set_str(self, key: str, value: str) -> None: ...


class RedisCursorStore:
    def __init__(self, client: redis.Redis | None = None):
        self._client = client or redis.Redis.from_url(settings.redis_url, decode_responses=True)

    def get_int(self, key: str, default: int = 0) -> int:
        value = self._client.get(key)
        return int(value) if value is not None else default

    def set_int(self, key: str, value: int) -> None:
        self._client.set(key, int(value))

    def get_str(self, key: str, default: str | None = None) -> str | None:
        value = self._client.get(key)
        return value if value is not None else default

    def set_str(self, key: str, value: str) -> None:
        self._client.set(key, value)


class InMemoryCursorStore:
    """Non-persistent store for tests."""

    def __init__(self) -> None:
        self._data: dict[str, object] = {}

    def get_int(self, key: str, default: int = 0) -> int:
        return int(self._data.get(key, default))

    def set_int(self, key: str, value: int) -> None:
        self._data[key] = int(value)

    def get_str(self, key: str, default: str | None = None) -> str | None:
        value = self._data.get(key)
        return str(value) if value is not None else default

    def set_str(self, key: str, value: str) -> None:
        self._data[key] = value
