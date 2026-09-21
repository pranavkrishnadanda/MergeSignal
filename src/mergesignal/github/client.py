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

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from mergesignal import __version__
from mergesignal.models import Report, Severity

#: Sent on every request so GitHub pins the response schema.
API_VERSION = "2022-11-28"

#: Page size used for every list endpoint (GitHub's maximum).
PAGE_SIZE = 100

#: Username half of the HTTPS clone URL for App installation tokens.
CLONE_USERNAME = "x-access-token"


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

    @property
    def label(self) -> str:
        """Human-readable name used in overlap findings, e.g. ``PR #12``."""
        return f"PR #{self.number}"


class GitHubClient:
    """Synchronous GitHub REST client.

    :param repo_slug: ``owner/name``.
    :param token: explicit token; omit to fall back to App auth / env.
    :param api_url: REST base URL.
    :param timeout: per-request timeout in seconds.
    """

    def __init__(
        self,
        repo_slug: str,
        *,
        token: str | None = None,
        api_url: str = "https://api.github.com",
        timeout: float = 20.0,
        transport: Any = None,
        token_env: str = "GITHUB_TOKEN",
        app_auth: Any = None,
        clone_url: str | None = None,
    ) -> None:
        """``transport`` exists so tests can inject an ``httpx.MockTransport``.

        ``clone_url`` overrides :meth:`repo_clone_url` for mirrors, GitHub
        Enterprise setups whose clone host differs from the API host, and tests
        that clone from a local path.
        """
        if not isinstance(repo_slug, str) or repo_slug.count("/") != 1 or not all(repo_slug.split("/")):
            raise ValueError(f"repo_slug must be 'owner/name', got {repo_slug!r}")
        self.repo_slug = repo_slug
        self.api_url = api_url.rstrip("/")
        self.token_env = token_env
        self._explicit_token = token or None
        self._app_auth = app_auth
        self._app_checked = app_auth is not None
        self._clone_url_override = clone_url
        self._client = httpx.Client(
            base_url=self.api_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": f"mergesignal/{__version__}",
            },
            follow_redirects=True,
        )

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - trivial; never print the token
        return f"GitHubClient({self.repo_slug!r}, api_url={self.api_url!r})"

    # ----------------------------------------------------------------- auth

    def token(self) -> str | None:
        """Resolve a token: explicit, then App auth, then the environment.

        App tokens are re-requested on every call; :class:`GitHubAppAuth` caches
        them internally and mints a fresh one shortly before expiry, so a
        long-lived client never sends a dead token.
        """
        if self._explicit_token:
            return self._explicit_token
        app = self._app()
        if app is not None:
            return app.token_for_repo(self.repo_slug)
        return os.environ.get(self.token_env) or None

    def _app(self) -> Any:
        """Lazily build :class:`GitHubAppAuth` from the environment, once."""
        if not self._app_checked:
            self._app_checked = True
            from mergesignal.github.app import GitHubAppAuth

            self._app_auth = GitHubAppAuth.from_env(api_url=self.api_url)
        return self._app_auth

    # -------------------------------------------------------------- plumbing

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Issue one authenticated request, translating failures to GitHubError."""
        headers = dict(kwargs.pop("headers", None) or {})
        token = self.token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self._client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub request failed: {method} {path}: {exc}") from exc
        if response.status_code >= 400:
            raise self._error(response, method, path)
        return response

    def _error(self, response: httpx.Response, method: str, path: str) -> GitHubError:
        """Build a :class:`GitHubError` explaining a non-2xx response."""
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text
        message = body.get("message") if isinstance(body, dict) else None
        detail = message or (str(body)[:200] if body else response.reason_phrase)
        if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
            detail = f"rate limited ({detail}); set {self.token_env} to raise the limit"
        return GitHubError(
            f"GitHub API {method} {path} failed with {response.status_code}: {detail}",
            status_code=response.status_code,
            body=body,
        )

    def _get_json(self, path: str, **kwargs: Any) -> Any:
        """GET and decode JSON."""
        return _json(self._request("GET", path, **kwargs))

    def _paginate(self, path: str, *, params: dict[str, Any], limit: int) -> list[Any]:
        """Follow ``Link: rel="next"`` until ``limit`` items are collected."""
        items: list[Any] = []
        next_url: str | None = path
        next_params: dict[str, Any] | None = {**params, "per_page": min(PAGE_SIZE, max(limit, 1))}
        while next_url and len(items) < limit:
            response = self._request("GET", next_url, params=next_params)
            page = _json(response)
            if not isinstance(page, list):
                raise GitHubError(f"expected a JSON array from {path}", body=page)
            items.extend(page)
            link = response.links.get("next") or {}
            next_url = link.get("url")
            next_params = None  # the Link URL already carries the query string
            if not page:
                break
        return items[:limit]

    # ------------------------------------------------------------------ PRs

    def list_open_pulls(self, *, limit: int = 20, base: str | None = None, include_drafts: bool = False) -> list[PullRequest]:
        """List open PRs, newest first, following pagination up to ``limit``.

        :param base: only PRs targeting this base branch.
        :raises GitHubError: non-2xx response.

        Drafts are filtered *after* fetching (the API has no draft filter), so a
        page full of drafts can yield fewer than ``limit`` results; that is
        preferable to paginating the whole repository looking for more.
        """
        params: dict[str, Any] = {"state": "open", "sort": "created", "direction": "desc"}
        if base:
            params["base"] = base
        raw = self._paginate(f"/repos/{self.repo_slug}/pulls", params=params, limit=max(limit, 0))
        pulls = [_pull_from_json(item, self.repo_slug) for item in raw if isinstance(item, dict)]
        if not include_drafts:
            pulls = [p for p in pulls if not p.draft]
        return pulls[:limit]

    def get_pull(self, number: int) -> PullRequest:
        """Fetch one PR.

        :raises GitHubError: 404 when the PR does not exist or is invisible.
        """
        data = self._get_json(f"/repos/{self.repo_slug}/pulls/{int(number)}")
        if not isinstance(data, dict):
            raise GitHubError(f"unexpected pull request payload for #{number}", body=data)
        return _pull_from_json(data, self.repo_slug)

    # ------------------------------------------------------------- comments

    def list_issue_comments(self, number: int, *, limit: int = 100) -> list[dict[str, Any]]:
        """Raw issue comments on a PR, oldest first — input to
        :func:`~mergesignal.report.github_comment.find_existing_comment`."""
        raw = self._paginate(f"/repos/{self.repo_slug}/issues/{int(number)}/comments", params={}, limit=max(limit, 0))
        return [item for item in raw if isinstance(item, dict)]

    def create_comment(self, number: int, body: str) -> dict[str, Any]:
        """Post a new issue comment on a PR."""
        data = _json(self._request("POST", f"/repos/{self.repo_slug}/issues/{int(number)}/comments", json={"body": body}))
        return data if isinstance(data, dict) else {}

    def update_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        """Edit an existing issue comment in place."""
        data = _json(self._request("PATCH", f"/repos/{self.repo_slug}/issues/comments/{int(comment_id)}", json={"body": body}))
        return data if isinstance(data, dict) else {}

    def upsert_comment(self, number: int, body: str, *, bot_login: str | None = None) -> dict[str, Any]:
        """Create-or-update the single MergeSignal comment carrying ``body``.

        ``body`` must already contain
        :data:`~mergesignal.report.github_comment.COMMENT_MARKER`, or the next
        run will not find it and will post a duplicate.
        """
        from mergesignal.report.github_comment import COMMENT_MARKER, find_existing_comment

        if COMMENT_MARKER not in body:
            raise ValueError("comment body is missing the MergeSignal marker; it would not be idempotent")
        existing = find_existing_comment(self.list_issue_comments(number), bot_login=bot_login)
        if existing is not None and isinstance(existing.get("id"), int):
            return self.update_comment(existing["id"], body)
        return self.create_comment(number, body)

    def upsert_report_comment(self, number: int, report: Report, *, bot_login: str | None = None, verbose: bool = False) -> dict[str, Any]:
        """Render ``report`` and create-or-update the single MergeSignal comment.

        This is the idempotency guarantee of FR-8: find the marker comment via
        :func:`~mergesignal.report.github_comment.find_existing_comment` and
        ``PATCH`` it when present, ``POST`` only when absent.
        """
        from mergesignal.report.github_comment import render_comment

        body = render_comment(report, repo_slug=self.repo_slug, pr_number=number, verbose=verbose)
        return self.upsert_comment(number, body, bot_login=bot_login)

    # ------------------------------------------------------------ check runs

    def create_check_run(self, head_sha: str, report: Report, *, name: str = "MergeSignal", threshold: Severity = "high") -> dict[str, Any]:
        """Optionally publish a check run summarising the report.

        Conclusion is ``failure`` when findings exceed the configured threshold,
        ``neutral`` when any signal errored, ``success`` otherwise.
        """
        from mergesignal.report.github_comment import render_comment

        conclusion = check_run_conclusion(report, threshold)
        summary = render_comment(report, repo_slug=self.repo_slug)
        payload = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": _check_run_title(report),
                "summary": summary[:65000],
            },
        }
        data = _json(self._request("POST", f"/repos/{self.repo_slug}/check-runs", json=payload))
        return data if isinstance(data, dict) else {}

    # ----------------------------------------------------------------- git

    def repo_clone_url(self, *, use_token: bool = True) -> str:
        """HTTPS clone URL, with the token embedded when ``use_token``.

        Tokens must never be logged; callers that print commands are responsible
        for redacting.
        """
        if self._clone_url_override:
            return self._clone_url_override
        host = clone_host(self.api_url)
        if use_token:
            token = self.token()
            if token:
                return f"https://{CLONE_USERNAME}:{token}@{host}/{self.repo_slug}.git"
        return f"https://{host}/{self.repo_slug}.git"


def check_run_conclusion(report: Report, threshold: Severity = "high") -> str:
    """Map a report onto a check-run conclusion.

    Findings win over errors: a report that found a real problem should fail the
    check even if some *other* engine also blew up.
    """
    if report.exceeds(threshold):
        return "failure"
    if report.has_errors:
        return "neutral"
    return "success"


def _check_run_title(report: Report) -> str:
    """One-line check-run title summarising the worst thing found."""
    worst = report.max_severity
    count = len(report.all_findings)
    if worst is None:
        return "No findings"
    noun = "finding" if count == 1 else "findings"
    return f"{count} {noun}, worst severity {worst}"


def clone_host(api_url: str) -> str:
    """Derive the git host from a REST base URL.

    ``https://api.github.com`` -> ``github.com``;
    ``https://ghe.example.com/api/v3`` -> ``ghe.example.com``.
    """
    host = urlsplit(api_url).netloc or urlsplit(f"https://{api_url}").netloc
    if host.startswith("api."):
        return host[4:]
    return host


def _json(response: httpx.Response) -> Any:
    """Decode a response body as JSON, or raise a clear :class:`GitHubError`."""
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubError(f"GitHub returned a non-JSON body for {response.url}") from exc


def _pull_from_json(data: dict[str, Any], repo_slug: str) -> PullRequest:
    """Project a GitHub pull-request object onto :class:`PullRequest`.

    Missing nested objects are tolerated (GitHub omits ``head.repo`` when the
    fork has been deleted) — a PR we cannot fetch is still worth listing.
    """
    head = data.get("head") or {}
    base = data.get("base") or {}
    head_repo = head.get("repo") or {}
    user = data.get("user") or {}

    clone_url = None
    full_name = head_repo.get("full_name")
    if isinstance(full_name, str) and full_name != repo_slug:
        raw = head_repo.get("clone_url")
        clone_url = raw if isinstance(raw, str) and raw else None

    return PullRequest(
        number=int(data.get("number") or 0),
        title=str(data.get("title") or ""),
        base_ref=str(base.get("ref") or ""),
        head_ref=str(head.get("ref") or ""),
        head_sha=str(head.get("sha") or ""),
        author=str(user.get("login") or ""),
        url=str(data.get("html_url") or ""),
        draft=bool(data.get("draft", False)),
        head_repo_clone_url=clone_url,
    )


#: ``git@host:owner/name(.git)`` — the scp-like SSH form.
_SCP_REMOTE = re.compile(r"^(?:[\w.+-]+@)?(?P<host>[\w.-]+):(?P<path>[\w.~/-]+?)(?:\.git)?/?$")


def slug_from_remote(remote_url: str) -> str | None:
    """Extract ``owner/name`` from an SSH or HTTPS git remote URL.

    Handles ``git@github.com:owner/name.git``,
    ``https://github.com/owner/name.git`` and Enterprise hostnames. Returns
    ``None`` when the URL is not a recognisable GitHub remote.
    """
    if not isinstance(remote_url, str) or not remote_url.strip():
        return None
    url = remote_url.strip()

    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http", "ssh", "git"):
            return None
        path = parts.path
    else:
        match = _SCP_REMOTE.match(url)
        if match is None:
            return None
        path = match.group("path")

    segments = [s for s in path.strip("/").removesuffix(".git").split("/") if s]
    if len(segments) != 2:
        return None
    owner, name = segments
    return f"{owner}/{name}"
