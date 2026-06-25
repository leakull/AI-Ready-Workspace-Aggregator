"""GitHub Issues connector — the fully implemented vertical slice.

Incremental: the first sync of a repo backfills (newest-created first); every
later sync passes ``since=<last updated_at>`` and walks updates oldest-first, so
only changed issues are fetched. The per-repo cursor is advanced to the newest
``updated_at`` seen, but only after the sync task commits (``on_sync_committed``)
— a failed commit re-fetches the same window. Pull requests are skipped (the
issues endpoint returns them too); rate-limit / server errors map to
:class:`RetryableError`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import httpx

from app.connectors.base import BaseConnector, RetryableError
from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.message import SourceSystem, UnifiedMessage
from app.services.cursor import CursorStore, RedisCursorStore

log = get_logger(__name__)

GITHUB_API = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 10  # safety cap per sync


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _cursor_key(repo: str) -> str:
    return f"connector:github:{repo}:since"


class GitHubConnector(BaseConnector):
    source_system = SourceSystem.github.value

    def __init__(
        self,
        repos: list[str] | None = None,
        token: str | None = None,
        cursor: CursorStore | None = None,
    ):
        self.repos = repos if repos is not None else settings.github_repo_list
        self.token = token if token is not None else settings.github_token
        self.cursor = cursor if cursor is not None else RedisCursorStore()
        self._next_since: dict[str, str] = {}

    def _build_client(self) -> httpx.Client:
        return httpx.Client(timeout=30.0)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _check_response(self, resp: httpx.Response) -> None:
        # Rate limit: GitHub uses 403 (or 429) with a zeroed remaining counter.
        if resp.status_code in (403, 429) and resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = resp.headers.get("X-RateLimit-Reset", "?")
            raise RetryableError(f"github rate limited, resets at {reset}")
        if resp.status_code >= 500:
            raise RetryableError(f"github server error {resp.status_code}")
        resp.raise_for_status()

    def _params(self, repo: str, page: int) -> dict:
        since = self.cursor.get_str(_cursor_key(repo))
        params = {"state": "all", "per_page": PER_PAGE, "page": page}
        if since:
            # Incremental: only issues updated since the cursor, oldest-first so
            # the MAX_PAGES cap still makes forward progress every run.
            params |= {"sort": "updated", "direction": "asc", "since": since}
        else:
            # First run: backfill the most recently created issues.
            params |= {"sort": "created", "direction": "desc"}
        return params

    def _fetch_repo(self, client: httpx.Client, repo: str) -> Iterator[UnifiedMessage]:
        max_updated: datetime | None = None
        for page in range(1, MAX_PAGES + 1):
            try:
                resp = client.get(
                    f"{GITHUB_API}/repos/{repo}/issues",
                    headers=self._headers(),
                    params=self._params(repo, page),
                )
            except httpx.TransportError as exc:
                raise RetryableError(f"github transport error: {exc}") from exc

            self._check_response(resp)
            issues = resp.json()
            if not issues:
                break

            for issue in issues:
                # Advance the cursor past every item (PRs included) so they are
                # not re-fetched next run.
                updated = _parse_dt(issue.get("updated_at"))
                if updated and (max_updated is None or updated > max_updated):
                    max_updated = updated
                if "pull_request" in issue:
                    continue
                yield self._normalize(repo, issue)

            if len(issues) < PER_PAGE:
                break

        if max_updated is not None:
            self._next_since[repo] = max_updated.isoformat()

    def _normalize(self, repo: str, issue: dict) -> UnifiedMessage:
        return UnifiedMessage(
            source_system=SourceSystem.github,
            external_id=f"{repo}#{issue['number']}",
            thread_external_id=repo,
            author=(issue.get("user") or {}).get("login"),
            title=issue.get("title"),
            body=issue.get("body") or "",
            url=issue.get("html_url"),
            source_created_at=_parse_dt(issue.get("created_at")),
            raw=issue,
        )

    def fetch(self) -> Iterator[UnifiedMessage]:
        if not self.repos:
            log.warning("github.no_repos_configured")
            return
        self._next_since = {}
        with self._build_client() as client:
            for repo in self.repos:
                incremental = self.cursor.get_str(_cursor_key(repo)) is not None
                log.info("github.fetch_repo", repo=repo, incremental=incremental)
                yield from self._fetch_repo(client, repo)

    def on_sync_committed(self) -> None:
        for repo, since in self._next_since.items():
            self.cursor.set_str(_cursor_key(repo), since)
            log.info("github.cursor_advanced", repo=repo, since=since)
        self._next_since = {}
