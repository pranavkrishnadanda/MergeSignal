"""Integration tests for the webhook worker against real git repositories.

The GitHub API is mocked with ``httpx.MockTransport``; git is not mocked at all
— :func:`prepare_checkout` fetches from a genuine repository built by
:class:`~tests.helpers.repo_builder.RepoBuilder`, which is the only way to prove
the fetch refspecs and ref naming actually work.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from mergesignal.config import Config
from mergesignal.git.repo import GitError, Repo
from mergesignal.github.client import GitHubClient, PullRequest
from mergesignal.report.github_comment import COMMENT_MARKER
from mergesignal.service.worker import (
    analyze_pull_request,
    base_ref_name,
    cleanup,
    head_ref_name,
    prepare_checkout,
)
from tests.helpers.repo_builder import RepoBuilder

pytestmark = pytest.mark.integration

SLUG = "acme/widgets"


@pytest.fixture
def upstream(builder: RepoBuilder) -> Path:
    """``main`` and ``feature`` diverged, with a semantic break between them."""
    return builder.scenario_rename_vs_new_caller().build()


@pytest.fixture
def pull() -> PullRequest:
    """A PR merging ``feature`` into ``main`` of the upstream repository."""
    return PullRequest(
        number=7,
        title="Rename old_name",
        base_ref="main",
        head_ref="feature",
        head_sha="",
        author="octocat",
        url=f"https://github.com/{SLUG}/pull/7",
    )


class Recorder:
    """A mock GitHub API that records every request it serves."""

    def __init__(self, *, existing_comments: list[dict[str, Any]] | None = None, pull_payload: dict[str, Any] | None = None, pulls: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict[str, Any]] = []
        self.existing = existing_comments or []
        self.pull_payload = pull_payload or {
            "number": 7,
            "title": "Rename old_name",
            "draft": False,
            "html_url": f"https://github.com/{SLUG}/pull/7",
            "user": {"login": "octocat"},
            "base": {"ref": "main"},
            "head": {"ref": "feature", "sha": "", "repo": {"full_name": SLUG, "clone_url": "unused"}},
        }
        self.pulls = pulls if pulls is not None else []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.content:
            self.bodies.append(json.loads(request.read()))
        if path.endswith("/pulls/7"):
            return httpx.Response(200, json=self.pull_payload)
        if path.endswith("/pulls"):
            return httpx.Response(200, json=self.pulls)
        if path.endswith("/issues/7/comments") and request.method == "GET":
            return httpx.Response(200, json=self.existing)
        if request.method == "POST":
            return httpx.Response(201, json={"id": 101})
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": 42})
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def client(self, **kwargs: Any) -> GitHubClient:
        return GitHubClient(SLUG, transport=httpx.MockTransport(self), **kwargs)


# ------------------------------------------------------------ ref naming


def test_ref_names_keep_base_and_head_apart(pull: PullRequest) -> None:
    """A fork PR from a branch also called ``main`` must not clobber the base."""
    fork_pull = PullRequest(number=9, title="t", base_ref="main", head_ref="main", head_sha="", author="a", url="")
    assert base_ref_name(fork_pull) == "main"
    assert head_ref_name(fork_pull) == "pr-9"
    assert base_ref_name(pull) != head_ref_name(pull)


# --------------------------------------------------------- prepare_checkout


def test_prepare_checkout_fetches_both_refs(tmp_path: Path, upstream: Path, pull: PullRequest) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    path = prepare_checkout(SLUG, pull, str(work_dir), base_url=str(upstream))

    repo = Repo(path)
    assert repo.is_repository()
    assert repo.ref_exists("main")
    assert repo.ref_exists("pr-7")
    assert repo.merge_base("main", "pr-7") is not None, "the merge base must be reachable after the fetch"


def test_prepare_checkout_fetches_a_fork_head(tmp_path: Path, make_builder: Any, pull: PullRequest) -> None:
    """A fork PR's head lives in a different repository and needs its own fetch."""
    base = make_builder("upstream")
    base.file("lib.py", "def f():\n    return 1\n").commit("initial")
    base_path = base.build()

    fork_path = tmp_path / "fork"
    Repo(tmp_path).run(["clone", "--quiet", str(base_path), str(fork_path)], cwd=tmp_path, check=True)
    fork = Repo(fork_path)
    fork.run(["checkout", "--quiet", "-b", "feature"], check=True)
    (fork_path / "lib.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    fork.run(["-c", "user.email=f@x", "-c", "user.name=f", "commit", "--quiet", "-am", "fork edit"], check=True)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    path = prepare_checkout(SLUG, pull, str(work_dir), base_url=str(base_path), head_url=str(fork_path))

    repo = Repo(path)
    assert repo.file_content_at("main", "lib.py") == "def f():\n    return 1\n"
    assert repo.file_content_at("pr-7", "lib.py") == "def f():\n    return 2\n"


def test_prepare_checkout_raises_on_a_missing_ref(tmp_path: Path, upstream: Path) -> None:
    missing = PullRequest(number=1, title="t", base_ref="main", head_ref="nope", head_sha="", author="a", url="")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    with pytest.raises(GitError, match="failed to fetch"):
        prepare_checkout(SLUG, missing, str(work_dir), base_url=str(upstream))


def test_prepare_checkout_rejects_a_pr_without_refs(tmp_path: Path) -> None:
    empty = PullRequest(number=1, title="t", base_ref="", head_ref="", head_sha="", author="a", url="")
    with pytest.raises(GitError, match="missing base/head refs"):
        prepare_checkout(SLUG, empty, str(tmp_path), base_url="unused")


# ------------------------------------------------------- analyze_pull_request


def test_analyze_pull_request_reports_and_comments(tmp_path: Path, upstream: Path) -> None:
    recorder = Recorder()
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = analyze_pull_request(
        SLUG,
        7,
        client=recorder.client(),
        clone_url=str(upstream),
        work_dir=str(work_dir),
    )

    assert result.ok, result.error
    assert result.report is not None
    assert result.comment_id == 101
    assert result.duration_seconds >= 0
    assert [s.name for s in result.report.signals] == ["conflicts", "semantic", "overlap", "risk"]
    assert result.report.base == "main"
    assert result.report.head == "pr-7"

    assert recorder.methods == ["GET", "GET", "GET", "POST"], "fetch PR, list open PRs for overlap, list comments, create comment"
    assert COMMENT_MARKER in recorder.bodies[-1]["body"]


def test_analyze_pull_request_reports_peer_overlap(tmp_path: Path, builder: RepoBuilder) -> None:
    """S3 must fire in the service path — the gap this test pins.

    ``worker`` used to call ``build_context`` without ``others``, so PR
    comments could never report cross-PR overlap even though ``analyze
    --prs`` could. A second open PR on a colliding branch must now surface
    in the webhook-produced report.
    """
    core = (
        "def alpha(x):\n    return x\n"
        "\n\n"
        "def beta(y):\n    return y\n"
    )
    builder.file("core.py", core).commit("initial core")
    builder.branch("feature")
    builder.file("core.py", core.replace("def alpha(x):", "def alpha(x, verbose):")).commit("candidate widens alpha")
    builder.checkout("main")
    builder.branch("peer-work")
    builder.file("core.py", core.replace("def alpha(x):", "def alpha(x, retries):")).commit("peer widens alpha")
    builder.checkout("main")
    upstream = builder.build()

    peer_payload = {
        "number": 3,
        "title": "Also touch alpha",
        "draft": False,
        "html_url": f"https://github.com/{SLUG}/pull/3",
        "user": {"login": "teammate"},
        "base": {"ref": "main"},
        "head": {"ref": "peer-work", "sha": "", "repo": {"full_name": SLUG, "clone_url": "unused"}},
    }
    candidate_payload = {
        "number": 7,
        "title": "Widen alpha",
        "draft": False,
        "html_url": f"https://github.com/{SLUG}/pull/7",
        "user": {"login": "octocat"},
        "base": {"ref": "main"},
        "head": {"ref": "feature", "sha": "", "repo": {"full_name": SLUG, "clone_url": "unused"}},
    }
    recorder = Recorder(pulls=[candidate_payload, peer_payload])
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = analyze_pull_request(SLUG, 7, client=recorder.client(), clone_url=str(upstream), work_dir=str(work_dir))

    assert result.ok, result.error
    assert result.report is not None
    overlap = result.report.signal("overlap")
    assert overlap.status == "findings", overlap.summary
    branches = {f.evidence["branch"] for f in overlap.findings}
    assert branches == {"PR #3"}, "the candidate PR itself must not appear as its own peer"
    finding = overlap.findings[0]
    assert finding.evidence["granularity"] == "symbol"
    assert finding.evidence["symbols"] == ["alpha"]


def test_analyze_pull_request_survives_a_peer_listing_failure(tmp_path: Path, upstream: Path) -> None:
    """A failed open-PR listing degrades overlap to skipped, never to a failed run."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/7"):
            return httpx.Response(200, json=Recorder().pull_payload)
        if path.endswith("/pulls"):
            return httpx.Response(500, json={"message": "boom"})
        return httpx.Response(200, json=[])

    client = GitHubClient(SLUG, transport=httpx.MockTransport(handler))
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = analyze_pull_request(SLUG, 7, client=client, clone_url=str(upstream), work_dir=str(work_dir), post_comment=False)

    assert result.ok, result.error
    assert result.report is not None
    assert result.report.signal("overlap").status == "skipped"


def test_analyze_pull_request_updates_an_existing_comment(tmp_path: Path, upstream: Path) -> None:
    existing = [{"id": 42, "body": f"stale report\n{COMMENT_MARKER}", "user": {"login": "bot"}, "created_at": "2024-01-01T00:00:00Z"}]
    recorder = Recorder(existing_comments=existing)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = analyze_pull_request(SLUG, 7, client=recorder.client(), clone_url=str(upstream), work_dir=str(work_dir))

    assert result.ok, result.error
    assert result.comment_id == 42
    assert recorder.calls[-1] == ("PATCH", f"/repos/{SLUG}/issues/comments/42")


def test_analyze_pull_request_cleans_up_its_checkout(tmp_path: Path, upstream: Path) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    analyze_pull_request(SLUG, 7, client=Recorder().client(), clone_url=str(upstream), work_dir=str(work_dir))

    assert list(work_dir.iterdir()) == [], "the temporary checkout must be removed even on success"


def test_analyze_pull_request_can_skip_commenting(tmp_path: Path, upstream: Path) -> None:
    recorder = Recorder()
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = analyze_pull_request(SLUG, 7, client=recorder.client(), clone_url=str(upstream), work_dir=str(work_dir), post_comment=False)

    assert result.ok, result.error
    assert result.comment_id is None
    assert recorder.methods == ["GET", "GET"], "the PR fetch and the open-PR listing; nothing was written"


def test_config_can_disable_the_comment(tmp_path: Path, upstream: Path) -> None:
    recorder = Recorder()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    config = Config()
    config.github.comment = False

    result = analyze_pull_request(SLUG, 7, client=recorder.client(), clone_url=str(upstream), work_dir=str(work_dir), config=config)

    assert result.ok, result.error
    assert recorder.methods == ["GET", "GET"]


def test_analyze_pull_request_captures_a_clone_failure(tmp_path: Path) -> None:
    recorder = Recorder()
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = analyze_pull_request(
        SLUG,
        7,
        client=recorder.client(),
        clone_url=str(tmp_path / "does-not-exist"),
        work_dir=str(work_dir),
    )

    assert not result.ok
    assert result.error is not None
    assert "GitError" in result.error
    assert list(work_dir.iterdir()) == [], "a failed run must still clean up"


def test_analyze_pull_request_captures_an_api_failure(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404, json={"message": "Not Found"}))
    result = analyze_pull_request(SLUG, 7, client=GitHubClient(SLUG, transport=transport), work_dir=str(tmp_path))

    assert not result.ok
    assert "GitHubError" in (result.error or "")


def test_timeout_posts_a_failure_comment(tmp_path: Path, upstream: Path) -> None:
    recorder = Recorder()
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = analyze_pull_request(
        SLUG,
        7,
        client=recorder.client(),
        clone_url=str(upstream),
        work_dir=str(work_dir),
        timeout_seconds=0.0,
    )

    assert not result.ok
    assert result.error is not None
    assert result.error.startswith("timeout:")
    assert recorder.methods[-1] == "POST"
    body = recorder.bodies[-1]["body"]
    assert "timed out" in body
    assert COMMENT_MARKER in body, "the failure note must carry the marker so the next run replaces it"


# ------------------------------------------------------------------ cleanup


def test_cleanup_removes_a_directory(tmp_path: Path) -> None:
    victim = tmp_path / "scratch"
    (victim / "nested").mkdir(parents=True)
    (victim / "nested" / "f.txt").write_text("x", encoding="utf-8")

    cleanup(str(victim))

    assert not victim.exists()


@pytest.mark.parametrize("path", ["", "/nonexistent/mergesignal-does-not-exist"])
def test_cleanup_never_raises(path: str) -> None:
    cleanup(path)
