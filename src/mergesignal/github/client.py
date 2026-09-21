"""httpx REST client for the GitHub API. **Owned by Agent F.**

Only the handful of endpoints MergeSignal needs. Everything is synchronous
(``httpx.Client``) because the CLI is synchronous and the service runs the
pipeline in a worker thread anyway.

Auth precedence: an explicit ``token`` argument, then
:class:`~mergesignal.github.app.GitHubAppAuth`, then ``$GITHUB_TOKEN``.
Unauthenticated requests are allowed for public repositories but rate-limit
fast — the client surfaces that as a clear :class:`GitHubError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mergesignal.models import Report


class GitHubError(RuntimeError):
    """A GitHub API call failed.

    Carries ``status_code`` and the parsed error body so callers can tell a 404
    (repo not found / no access) from a 403 (rate limit) from a 422.
    """

    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass(frozen=True)
class PullRequest:
    """The subset of a GitHub PR that MergeSignal cares about."""

    number: int
    title: str
    base_ref: str
    head_ref: str
    head_sha: str
    author: str
    url: str
    draft: bool = False
    head_repo_clone_url: str | None = None
    """Populated for fork PRs, whose head ref is not in the base repository."""


class GitHubClient:
    """Synchronous GitHub REST client.

    :param repo_slug: ``owner/name``.
    :param token: explicit token; omit to fall back to App auth / env.
    :param api_url: REST base URL.
    :param timeout: per-request timeout in seconds.
    """

    def __init__(self, repo_slug: str, *, token: str | None = None, api_url: str = "https://api.github.com", timeout: float = 20.0, transport: Any = None) -> None:
        """``transport`` exists so tests can inject an ``httpx.MockTransport``."""
        raise NotImplementedError

    def close(self) -> None:
        """Close the underlying httpx client."""
        raise NotImplementedError

    def __enter__(self) -> GitHubClient:
        raise NotImplementedError

    def __exit__(self, *exc_info: object) -> None:
        raise NotImplementedError

    def list_open_pulls(self, *, limit: int = 20, base: str | None = None, include_drafts: bool = False) -> list[PullRequest]:
        """List open PRs, newest first, following pagination up to ``limit``.

        :param base: only PRs targeting this base branch.
        :raises GitHubError: non-2xx response.
        """
        raise NotImplementedError

    def get_pull(self, number: int) -> PullRequest:
        """Fetch one PR.

        :raises GitHubError: 404 when the PR does not exist or is invisible.
        """
        raise NotImplementedError

    def list_issue_comments(self, number: int, *, limit: int = 100) -> list[dict[str, Any]]:
        """Raw issue comments on a PR, oldest first — input to
        :func:`~mergesignal.report.github_comment.find_existing_comment`."""
        raise NotImplementedError

    def create_comment(self, number: int, body: str) -> dict[str, Any]:
        """Post a new issue comment on a PR."""
        raise NotImplementedError

    def update_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        """Edit an existing issue comment in place."""
        raise NotImplementedError

    def upsert_report_comment(self, number: int, report: Report, *, bot_login: str | None = None, verbose: bool = False) -> dict[str, Any]:
        """Render ``report`` and create-or-update the single MergeSignal comment.

        This is the idempotency guarantee of FR-8: find the marker comment via
        :func:`~mergesignal.report.github_comment.find_existing_comment` and
        ``PATCH`` it when present, ``POST`` only when absent.
        """
        raise NotImplementedError

    def create_check_run(self, head_sha: str, report: Report, *, name: str = "MergeSignal") -> dict[str, Any]:
        """Optionally publish a check run summarising the report.

        Conclusion is ``failure`` when findings exceed the configured threshold,
        ``neutral`` when any signal errored, ``success`` otherwise.
        """
        raise NotImplementedError

    def repo_clone_url(self, *, use_token: bool = True) -> str:
        """HTTPS clone URL, with the token embedded when ``use_token``.

        Tokens must never be logged; callers that print commands are responsible
        for redacting.
        """
        raise NotImplementedError


def slug_from_remote(remote_url: str) -> str | None:
    """Extract ``owner/name`` from an SSH or HTTPS git remote URL.

    Handles ``git@github.com:owner/name.git``,
    ``https://github.com/owner/name.git`` and Enterprise hostnames. Returns
    ``None`` when the URL is not a recognisable GitHub remote.
    """
    raise NotImplementedError
