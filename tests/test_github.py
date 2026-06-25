"""GitHub connector: incremental `since` cursor, PR filtering, retry mapping.

The API is mocked with ``httpx.MockTransport`` (captures request params), so these
run without network. Persistence isn't exercised here — see test_dedup / test_api.
"""

from __future__ import annotations

import httpx
import pytest

from app.connectors.base import RetryableError
from app.connectors.github import GitHubConnector
from app.services.cursor import InMemoryCursorStore

CURSOR_KEY = "connector:github:acme/repo:since"

ISSUES = [
    {
        "number": 1,
        "title": "Bug A",
        "body": "boom",
        "user": {"login": "alice"},
        "html_url": "https://x/1",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-05T10:00:00Z",
    },
    {  # a PR — skipped, but its updated_at is the newest and must advance the cursor
        "number": 2,
        "title": "PR B",
        "pull_request": {"url": "https://x/pull/2"},
        "created_at": "2026-01-02T00:00:00Z",
        "updated_at": "2026-01-06T12:00:00Z",
    },
    {
        "number": 3,
        "title": "Bug C",
        "body": "kaboom",
        "user": {"login": "bob"},
        "html_url": "https://x/3",
        "created_at": "2026-01-03T00:00:00Z",
        "updated_at": "2026-01-04T09:00:00Z",
    },
]


def make_connector(cursor, captured, *, status=200, json=ISSUES) -> GitHubConnector:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        page = int(request.url.params.get("page", "1"))
        body = json if page == 1 else []
        return httpx.Response(status, json=body, headers=_HEADERS.get(status, {}))

    transport = httpx.MockTransport(handler)
    conn = GitHubConnector(repos=["acme/repo"], token="T", cursor=cursor)
    conn._build_client = lambda: httpx.Client(transport=transport)  # type: ignore[method-assign]
    return conn


_HEADERS = {403: {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "999"}}


def test_first_run_backfills_and_stages_cursor():
    cursor = InMemoryCursorStore()
    captured: list = []
    conn = make_connector(cursor, captured)

    msgs = list(conn.fetch())
    assert [m.external_id for m in msgs] == ["acme/repo#1", "acme/repo#3"]  # PR skipped
    assert "since" not in captured[0]
    assert captured[0]["sort"] == "created"

    # Cursor advances only after the sync task commits.
    assert cursor.get_str(CURSOR_KEY) is None
    conn.on_sync_committed()
    assert cursor.get_str(CURSOR_KEY) == "2026-01-06T12:00:00+00:00"  # max across all, incl. PR


def test_incremental_run_sends_since():
    cursor = InMemoryCursorStore()
    cursor.set_str(CURSOR_KEY, "2026-01-06T12:00:00+00:00")
    captured: list = []
    conn = make_connector(cursor, captured)

    list(conn.fetch())
    assert captured[0]["since"] == "2026-01-06T12:00:00+00:00"
    assert captured[0]["sort"] == "updated"
    assert captured[0]["direction"] == "asc"


def test_server_error_is_retryable():
    conn = make_connector(InMemoryCursorStore(), [], status=500, json={})
    with pytest.raises(RetryableError):
        list(conn.fetch())


def test_rate_limit_is_retryable():
    conn = make_connector(InMemoryCursorStore(), [], status=403, json={})
    with pytest.raises(RetryableError):
        list(conn.fetch())
