"""Unit tests for the GitHub REST client, driven by ``httpx.MockTransport``.

Nothing here touches the network. The important behaviours are pagination,
auth precedence, error translation and — above all — the comment *upsert* path
that keeps MergeSignal from spamming a PR (FR-8).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from mergesignal.github.client import (
    GitHubClient,
    GitHubError,
    PullRequest,
    check_run_conclusion,
    clone_host,
    slug_from_remote,
)
from mergesignal.models import Finding, Report, Signal
from mergesignal.report.github_comment import COMMENT_MARKER

SLUG = "acme/widgets"


def pull_json(number: int = 7, **overrides: Any) -> dict[str, Any]:
    """A trimmed-down GitHub pull-request object."""
    data: dict[str, Any] = {
        "number": number,
        "title": f"Pull {number}",
        "draft": False,
        "html_url": f"https://github.com/{SLUG}/pull/{number}",
        "user": {"login": "octocat"},
        "base": {"ref": "main"},
        "head": {
            "ref": f"feature-{number}",
            "sha": "a" * 40,
            "repo": {"full_name": SLUG, "clone_url": f"https://github.com/{SLUG}.git"},
        },
    }
    data.update(overrides)
    return data


def client_for(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> GitHubClient:
    """Build a client wired to a mock transport."""
    return GitHubClient(SLUG, transport=httpx.MockTransport(handler), **kwargs)


# ------------------------------------------------------------ construction


@pytest.mark.parametrize("slug", ["acme", "acme/widgets/extra", "/widgets", "acme/", ""])
def test_rejects_a_bad_slug(slug: str) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        GitHubClient(slug)


def test_client_is_a_context_manager() -> None:
    with client_for(lambda r: httpx.Response(200, json={})) as client:
        assert client.repo_slug == SLUG


# -------------------------------------------------------------------- auth


def test_explicit_token_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json=pull_json())

    with client_for(handler, token="explicit") as client:
        client.get_pull(7)
    assert seen == ["Bearer explicit"]


def test_env_token_is_used_when_no_explicit_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json=pull_json())

    with client_for(handler) as client:
        client.get_pull(7)
    assert seen == ["Bearer from-env"]


def test_unauthenticated_requests_send_no_authorization_header() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json=pull_json())

    with client_for(handler) as client:
        client.get_pull(7)
    assert seen == [None]


def test_app_auth_supplies_the_token() -> None:
    class FakeApp:
        def token_for_repo(self, slug: str) -> str:
            assert slug == SLUG
            return "ghs_installation"

    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json=pull_json())

    with client_for(handler, app_auth=FakeApp()) as client:
        client.get_pull(7)
    assert seen == ["Bearer ghs_installation"]


def test_api_version_header_is_pinned() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-GitHub-Api-Version"))
        return httpx.Response(200, json=pull_json())

    with client_for(handler) as client:
        client.get_pull(7)
    assert seen == ["2022-11-28"]


# --------------------------------------------------------------------- PRs


def test_get_pull_maps_the_fields() -> None:
    with client_for(lambda r: httpx.Response(200, json=pull_json(12))) as client:
        pull = client.get_pull(12)

    assert pull == PullRequest(
        number=12,
        title="Pull 12",
        base_ref="main",
        head_ref="feature-12",
        head_sha="a" * 40,
        author="octocat",
        url=f"https://github.com/{SLUG}/pull/12",
        draft=False,
        head_repo_clone_url=None,
    )
    assert pull.label == "PR #12"


def test_get_pull_records_a_fork_clone_url() -> None:
    forked = pull_json(
        3,
        head={
            "ref": "main",
            "sha": "b" * 40,
            "repo": {
                "full_name": "someone/widgets",
                "clone_url": "https://github.com/someone/widgets.git",
            },
        },
    )
    with client_for(lambda r: httpx.Response(200, json=forked)) as client:
        pull = client.get_pull(3)
    assert pull.head_repo_clone_url == "https://github.com/someone/widgets.git"


def test_get_pull_survives_a_deleted_fork() -> None:
    orphan = pull_json(4, head={"ref": "gone", "sha": "c" * 40, "repo": None})
    with client_for(lambda r: httpx.Response(200, json=orphan)) as client:
        pull = client.get_pull(4)
    assert pull.head_repo_clone_url is None
    assert pull.head_ref == "gone"


def test_get_pull_404_raises_github_error() -> None:
    with (
        client_for(lambda r: httpx.Response(404, json={"message": "Not Found"})) as client,
        pytest.raises(GitHubError) as exc_info,
    ):
        client.get_pull(99)
    assert exc_info.value.status_code == 404
    assert "Not Found" in str(exc_info.value)


def test_rate_limit_403_explains_itself() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"message": "API rate limit exceeded"}, headers={"X-RateLimit-Remaining": "0"}
        )

    with client_for(handler) as client, pytest.raises(GitHubError, match="rate limited"):
        client.get_pull(1)


def test_list_open_pulls_filters_drafts_and_passes_base() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=[pull_json(1), pull_json(2, draft=True), pull_json(3)])

    with client_for(handler) as client:
        pulls = client.list_open_pulls(base="main")

    assert [p.number for p in pulls] == [1, 3]
    assert seen[0].params["state"] == "open"
    assert seen[0].params["base"] == "main"


def test_list_open_pulls_can_include_drafts() -> None:
    with client_for(
        lambda r: httpx.Response(200, json=[pull_json(1), pull_json(2, draft=True)])
    ) as client:
        pulls = client.list_open_pulls(include_drafts=True)
    assert [p.number for p in pulls] == [1, 2]


def test_list_open_pulls_follows_pagination() -> None:
    page_two = "https://api.github.com/repos/acme/widgets/pulls?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[pull_json(3)])
        return httpx.Response(
            200, json=[pull_json(1), pull_json(2)], headers={"Link": f'<{page_two}>; rel="next"'}
        )

    with client_for(handler) as client:
        pulls = client.list_open_pulls(limit=10)
    assert [p.number for p in pulls] == [1, 2, 3]


def test_list_open_pulls_honours_the_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[pull_json(n) for n in range(1, 6)])

    with client_for(handler) as client:
        pulls = client.list_open_pulls(limit=2)
    assert len(pulls) == 2


# ---------------------------------------------------------------- comments


def _comment(comment_id: int, body: str, login: str = "mergesignal[bot]") -> dict[str, Any]:
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": login},
        "created_at": f"2024-01-0{comment_id}T00:00:00Z",
    }


def test_upsert_report_comment_creates_when_absent(make_report: Callable[..., Report]) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": 555})

    with client_for(handler) as client:
        result = client.upsert_report_comment(7, make_report())

    assert result["id"] == 555
    assert calls == [
        ("GET", f"/repos/{SLUG}/issues/7/comments"),
        ("POST", f"/repos/{SLUG}/issues/7/comments"),
    ]


def test_upsert_report_comment_updates_the_marked_comment(
    make_report: Callable[..., Report],
) -> None:
    bodies: list[str] = []
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    _comment(1, "unrelated chatter", "human"),
                    _comment(42, f"old report\n{COMMENT_MARKER}\n"),
                ],
            )
        bodies.append(request.read().decode())
        return httpx.Response(200, json={"id": 42})

    with client_for(handler) as client:
        result = client.upsert_report_comment(7, make_report())

    assert result["id"] == 42
    assert calls[-1] == ("PATCH", f"/repos/{SLUG}/issues/comments/42"), (
        "an existing report must be PATCHed, never duplicated"
    )
    assert COMMENT_MARKER in bodies[0], "the marker must survive the update or idempotency breaks"


def test_upsert_report_comment_respects_bot_login(make_report: Callable[..., Report]) -> None:
    """A human quoting our marker must not hijack the update target."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200, json=[_comment(9, f"> quoting you\n{COMMENT_MARKER}", login="impostor")]
            )
        return httpx.Response(201, json={"id": 10})

    with client_for(handler) as client:
        client.upsert_report_comment(7, make_report(), bot_login="mergesignal[bot]")

    assert calls[-1][0] == "POST"


def test_upsert_comment_refuses_an_unmarked_body() -> None:
    with (
        client_for(lambda r: httpx.Response(200, json=[])) as client,
        pytest.raises(ValueError, match="marker"),
    ):
        client.upsert_comment(7, "no marker here")


def test_list_issue_comments_returns_raw_dicts() -> None:
    with client_for(lambda r: httpx.Response(200, json=[_comment(1, "hi")])) as client:
        comments = client.list_issue_comments(7)
    assert comments[0]["id"] == 1


# -------------------------------------------------------------- check runs


def test_check_run_conclusion_mapping(make_finding: Callable[..., Finding]) -> None:
    clean = Report(base="main", head="feature", signals=[Signal(name="conflicts", status="ok")])
    errored = Report(base="main", head="feature", signals=[Signal.error("risk", "boom")])
    bad = Report(
        base="main",
        head="feature",
        signals=[
            Signal(name="semantic", status="findings", findings=[make_finding(severity="critical")])
        ],
    )

    assert check_run_conclusion(clean) == "success"
    assert check_run_conclusion(errored) == "neutral"
    assert check_run_conclusion(bad) == "failure"
    assert check_run_conclusion(bad, "critical") == "failure"


def test_create_check_run_posts_the_expected_payload(make_report: Callable[..., Report]) -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.append(json.loads(request.read()))
        return httpx.Response(201, json={"id": 1})

    with client_for(handler) as client:
        client.create_check_run("f" * 40, make_report())

    payload = captured[0]
    assert payload["head_sha"] == "f" * 40
    assert payload["name"] == "MergeSignal"
    assert payload["status"] == "completed"
    assert payload["conclusion"] in {"success", "failure", "neutral"}
    assert COMMENT_MARKER in payload["output"]["summary"]


# ------------------------------------------------------------- clone URLs


def test_repo_clone_url_embeds_the_token() -> None:
    with client_for(lambda r: httpx.Response(200), token="ghs_secret") as client:
        assert client.repo_clone_url() == f"https://x-access-token:ghs_secret@github.com/{SLUG}.git"
        assert client.repo_clone_url(use_token=False) == f"https://github.com/{SLUG}.git"


def test_repo_clone_url_override_wins() -> None:
    with client_for(lambda r: httpx.Response(200), clone_url="/tmp/local.git") as client:
        assert client.repo_clone_url() == "/tmp/local.git"


@pytest.mark.parametrize(
    ("api_url", "host"),
    [
        ("https://api.github.com", "github.com"),
        ("https://ghe.example.com/api/v3", "ghe.example.com"),
        ("https://api.ghe.example.com", "ghe.example.com"),
    ],
)
def test_clone_host(api_url: str, host: str) -> None:
    assert clone_host(api_url) == host


# ------------------------------------------------------------ slug parsing


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:acme/widgets.git", "acme/widgets"),
        ("git@github.com:acme/widgets", "acme/widgets"),
        ("https://github.com/acme/widgets.git", "acme/widgets"),
        ("https://github.com/acme/widgets", "acme/widgets"),
        ("https://user:token@github.com/acme/widgets.git", "acme/widgets"),
        ("ssh://git@ghe.example.com:22/acme/widgets.git", "acme/widgets"),
        ("git@ghe.example.com:acme/widgets.git", "acme/widgets"),
        ("  https://github.com/acme/widgets.git  ", "acme/widgets"),
        ("https://github.com/acme/widgets/", "acme/widgets"),
    ],
)
def test_slug_from_remote(remote: str, expected: str) -> None:
    assert slug_from_remote(remote) == expected


@pytest.mark.parametrize(
    "remote",
    [
        "",
        "   ",
        "not a url",
        "https://github.com/acme",
        "https://github.com/a/b/c",
        "/local/path/repo.git",
        "file:///tmp/repo",
    ],
)
def test_slug_from_remote_rejects_the_unrecognisable(remote: str) -> None:
    assert slug_from_remote(remote) is None
