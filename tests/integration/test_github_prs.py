"""The ``--prs`` cross-PR overlap source (S3 input) wired into the CLI.

These exercise :func:`mergesignal.cli._collect_pr_diffs` against a real
repository with a mocked GitHub API. The rule under test is NFR-2: listing PRs
must never fetch or otherwise mutate the user's repository, so a PR whose head
is not already local is skipped with a warning instead.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from mergesignal import cli
from mergesignal.config import Config
from mergesignal.git.repo import GitError, Repo
from mergesignal.github import client as client_module
from tests.helpers.repo_builder import RepoBuilder

pytestmark = pytest.mark.integration

SLUG = "acme/widgets"


def pull_json(number: int, head_ref: str, head_sha: str = "") -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Pull {number}",
        "draft": False,
        "html_url": f"https://github.com/{SLUG}/pull/{number}",
        "user": {"login": "octocat"},
        "base": {"ref": "main"},
        "head": {
            "ref": head_ref,
            "sha": head_sha,
            "repo": {"full_name": SLUG, "clone_url": "https://github.com/acme/widgets.git"},
        },
    }


@pytest.fixture
def mock_github(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[dict[str, Any]]], None]:
    """Install a mock transport into every client the CLI builds."""
    original = client_module.GitHubClient

    def install(pulls: list[dict[str, Any]]) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/pulls"):
                return httpx.Response(200, json=pulls)
            for pull in pulls:
                if request.url.path.endswith(f"/pulls/{pull['number']}"):
                    return httpx.Response(200, json=pull)
            return httpx.Response(404, json={"message": "Not Found"})

        def factory(slug: str, **kwargs: Any) -> Any:
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            return original(slug, **kwargs)

        monkeypatch.setattr(client_module, "GitHubClient", factory)

    return install


@pytest.fixture
def repo_with_feature(builder: RepoBuilder) -> Path:
    """``main`` plus a local ``feature`` branch — the PR head, already fetched."""
    return builder.scenario_clean_merge().build()


def config_for(slug: str | None = SLUG) -> Config:
    config = Config()
    config.github.repo = slug
    return config


def test_collect_others_builds_branch_diffs_for_open_prs(
    repo_with_feature: Path, mock_github: Callable[..., None]
) -> None:
    mock_github([pull_json(11, "feature")])
    repo = Repo(repo_with_feature)

    others = cli.collect_others(repo, config_for(), branches=None, prs=["open"], base="main")

    assert len(others) == 1
    other = others[0]
    assert other.name == "PR #11"
    assert other.pr_number == 11
    assert other.author == "octocat"
    assert other.url == f"https://github.com/{SLUG}/pull/11"
    assert other.diff.paths == {"b.py"}


def test_specific_pr_numbers_are_fetched_individually(
    repo_with_feature: Path, mock_github: Callable[..., None]
) -> None:
    mock_github([pull_json(11, "feature")])
    repo = Repo(repo_with_feature)

    others = cli.collect_others(repo, config_for(), branches=None, prs=["#11"], base="main")

    assert [o.pr_number for o in others] == [11]


def test_pr_head_uses_the_sha_when_it_is_local(
    repo_with_feature: Path, mock_github: Callable[..., None]
) -> None:
    repo = Repo(repo_with_feature)
    sha = repo.rev_parse("feature")
    mock_github([pull_json(12, "some-remote-name", head_sha=sha)])

    others = cli.collect_others(repo, config_for(), branches=None, prs=["open"], base="main")

    assert [o.head for o in others] == [sha]


def test_pr_without_local_objects_is_skipped(
    repo_with_feature: Path, mock_github: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """NFR-2: we do not fetch on the user's behalf, we tell them to."""
    mock_github([pull_json(13, "never-fetched")])
    repo = Repo(repo_with_feature)

    others = cli.collect_others(repo, config_for(), branches=None, prs=["open"], base="main")

    assert others == []
    assert "git fetch" in capsys.readouterr().err


def test_unknown_pr_value_warns_and_continues(
    repo_with_feature: Path, mock_github: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    mock_github([pull_json(11, "feature")])
    repo = Repo(repo_with_feature)

    others = cli.collect_others(
        repo, config_for(), branches=None, prs=["not-a-number", "open"], base="main"
    )

    assert [o.pr_number for o in others] == [11]
    assert "unrecognised --prs value" in capsys.readouterr().err


def test_api_failure_degrades_to_no_overlap_input(
    repo_with_feature: Path, mock_github: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """FR-8: GitHub being unreachable must never fail the whole analysis."""
    mock_github([])  # /pulls/99 -> 404
    repo = Repo(repo_with_feature)

    others = cli.collect_others(repo, config_for(), branches=None, prs=["99"], base="main")

    assert others == []
    assert "cross-PR overlap unavailable" in capsys.readouterr().err


def test_slug_comes_from_the_origin_remote(repo_with_feature: Path) -> None:
    repo = Repo(repo_with_feature)
    repo.run(["remote", "add", "origin", "git@github.com:acme/widgets.git"], check=True)

    assert cli._resolve_repo_slug(repo, config_for(None)) == SLUG


def test_slug_config_wins_over_the_remote(repo_with_feature: Path) -> None:
    repo = Repo(repo_with_feature)
    repo.run(["remote", "add", "origin", "git@github.com:someone/else.git"], check=True)

    assert cli._resolve_repo_slug(repo, config_for("acme/widgets")) == SLUG


def test_missing_slug_raises_a_clear_error(repo_with_feature: Path) -> None:
    repo = Repo(repo_with_feature)
    with pytest.raises(GitError, match=r"github\.repo"):
        cli._resolve_repo_slug(repo, config_for(None))
