"""GitHub Issues connector — the fully implemented vertical slice.

Reads issues from one or more repositories via the REST API, paginates, maps
rate-limit / server errors to :class:`RetryableError`, and normalizes each issue
into a :class:`UnifiedMessage`. Pull requests are skipped (the issues endpoint
returns them too).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import httpx

from app.connectors.base import BaseConnector, RetryableError
from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.message import SourceSystem, UnifiedMessage

log = get_logger(__name__)

GITHUB_API = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 10  # safety cap for the demo


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHubConnector(BaseConnector):
    source_system = SourceSystem.github.value

    def __init__(self, repos: list[str] | None = None, token: str | None = None):
        self.repos = repos if repos is not None else settings.github_repo_list
        self.token = token if token is not None else settings.github_token

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

    def _fetch_repo(self, client: httpx.Client, repo: str) -> Iterator[UnifiedMessage]:
        for page in range(1, MAX_PAGES + 1):
            try:
                resp = client.get(
                    f"{GITHUB_API}/repos/{repo}/issues",
                    headers=self._headers(),
                    params={
                        "state": "all",
                        # Sort by creation: "updated" surfaces bot-touched PRs
                        # first on active repos, starving real issues.
                        "sort": "created",
                        "direction": "desc",
                        "per_page": PER_PAGE,
                        "page": page,
                    },
                )
            except httpx.TransportError as exc:  # connect/read timeouts, DNS, etc.
                raise RetryableError(f"github transport error: {exc}") from exc

            self._check_response(resp)
            issues = resp.json()
            if not issues:
                break

            for issue in issues:
                if "pull_request" in issue:  # the issues endpoint also lists PRs
                    continue
                yield self._normalize(repo, issue)

            if len(issues) < PER_PAGE:
                break

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
        with httpx.Client(timeout=30.0) as client:
            for repo in self.repos:
                log.info("github.fetch_repo", repo=repo)
                yield from self._fetch_repo(client, repo)
