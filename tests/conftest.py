"""Shared pytest fixtures for the whole suite.

Every fixture here is available to unit, integration and regression tests
without importing anything. The repository-building fixtures create real git
repositories under pytest's ``tmp_path``, which is cleaned up automatically.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from mergesignal.config import Config, GitHubConfig, RiskWeights
from mergesignal.git.repo import Repo
from mergesignal.models import (
    AnalysisContext,
    Diff,
    DiffFile,
    Finding,
    Hunk,
    LineRange,
    Report,
    Signal,
)
from tests.helpers.repo_builder import RepoBuilder


def _git_version() -> tuple[int, int, int]:
    """Best-effort ``git --version`` tuple; ``(0, 0, 0)`` when git is missing."""
    if shutil.which("git") is None:
        return (0, 0, 0)
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False).stdout
    for token in out.split():
        if token[:1].isdigit():
            parts = [*token.split("."), "0", "0"][:3]
            return tuple(int("".join(c for c in p if c.isdigit()) or 0) for p in parts)  # type: ignore[return-value]
    return (0, 0, 0)


GIT_VERSION = _git_version()

#: Skip marker for tests that require ``git merge-tree --write-tree``.
requires_merge_tree = pytest.mark.skipif(
    GIT_VERSION < (2, 38, 0),
    reason=f"git >= 2.38 required for merge-tree --write-tree (found {GIT_VERSION})",
)


def pytest_configure(config: pytest.Config) -> None:
    """Register markers so ``-W error`` runs do not warn about unknown marks."""
    config.addinivalue_line("markers", "integration: builds real git repositories on disk (slower)")
    config.addinivalue_line("markers", "regression: golden-snapshot corpus tests")


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add ``--snapshot-update`` for regenerating the golden regression corpus.

    Kept deliberately explicit rather than, say, auto-writing a missing
    snapshot: a golden file that regenerates itself on demand stops being a
    regression test. CI never passes this flag, so a behaviour change in CI is
    a failure, not a silent rewrite.
    """
    parser.addoption(
        "--snapshot-update",
        action="store_true",
        default=False,
        help="Rewrite tests/regression/snapshots/*.json from the current behaviour, then skip.",
    )


@pytest.fixture
def snapshot_update(request: pytest.FixtureRequest) -> bool:
    """Whether ``--snapshot-update`` was passed on the command line."""
    return bool(request.config.getoption("--snapshot-update"))


# ------------------------------------------------------------- repositories


@pytest.fixture
def builder(tmp_path: Path) -> RepoBuilder:
    """A fresh :class:`RepoBuilder` rooted at ``tmp_path/repo``.

    The repository exists but has no commits yet — the "unborn branch" state
    NFR-3 requires us to survive. Call ``.file(...).commit(...)`` to populate it.
    """
    return RepoBuilder(tmp_path / "repo")


@pytest.fixture
def make_builder(tmp_path: Path) -> Callable[..., RepoBuilder]:
    """Factory for tests needing several repositories (clone/fetch scenarios).

    Usage: ``upstream = make_builder("upstream"); fork = make_builder("fork")``.
    """
    created: dict[str, RepoBuilder] = {}

    def _make(name: str = "repo", **kwargs: object) -> RepoBuilder:
        if name in created:
            raise AssertionError(f"builder {name!r} already created")
        created[name] = RepoBuilder(tmp_path / name, **kwargs)  # type: ignore[arg-type]
        return created[name]

    return _make


@pytest.fixture
def simple_repo(builder: RepoBuilder) -> Path:
    """A repository with one commit on ``main`` — the minimum viable repo."""
    return builder.file("README.md", "# test\n").commit("initial").build()


@pytest.fixture
def two_branch_repo(builder: RepoBuilder) -> Path:
    """``main`` and ``feature`` diverged over different files — a clean merge."""
    return builder.scenario_clean_merge().build()


@pytest.fixture
def conflict_repo(builder: RepoBuilder) -> Path:
    """``main`` and ``feature`` editing the same lines — a textual conflict."""
    return builder.scenario_textual_conflict().build()


@pytest.fixture
def repo(simple_repo: Path) -> Repo:
    """A :class:`~mergesignal.git.repo.Repo` over :func:`simple_repo`."""
    return Repo(simple_repo)


@pytest.fixture
def repo_factory() -> Callable[[Path], Repo]:
    """Turn any path into a :class:`~mergesignal.git.repo.Repo`."""
    return lambda path: Repo(path)


# ------------------------------------------------------------------- config


@pytest.fixture
def config() -> Config:
    """Default configuration — what a repo with no ``.mergesignal.yaml`` gets."""
    return Config()


@pytest.fixture
def strict_config() -> Config:
    """Configuration that flags everything: threshold ``low``, all signals on."""
    return Config(severity_threshold="low", risk_weights=RiskWeights(), github=GitHubConfig())


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[str], Path]:
    """Write a ``.mergesignal.yaml`` into ``tmp_path`` and return its path."""

    def _write(contents: str, *, name: str = ".mergesignal.yaml") -> Path:
        target = tmp_path / name
        target.write_text(contents, encoding="utf-8")
        return target

    return _write


# ------------------------------------------------------------ model factories


@pytest.fixture
def make_hunk() -> Callable[..., Hunk]:
    """Build a :class:`~mergesignal.models.Hunk` with sensible defaults."""

    def _make(file_path: str = "a.py", base: tuple[int, int] = (1, 2), head: tuple[int, int] = (1, 2), added: list[str] | None = None, removed: list[str] | None = None) -> Hunk:
        return Hunk(
            file_path=file_path,
            base_range=LineRange(start=base[0], end=base[1]),
            head_range=LineRange(start=head[0], end=head[1]),
            added_lines=added if added is not None else ["new"],
            removed_lines=removed if removed is not None else ["old"],
        )

    return _make


@pytest.fixture
def make_diff(make_hunk: Callable[..., Hunk]) -> Callable[..., Diff]:
    """Build a one-file :class:`~mergesignal.models.Diff`."""

    def _make(path: str = "a.py", *, base: str = "main", head: str = "feature", language: str | None = "python") -> Diff:
        hunk = make_hunk(file_path=path)
        return Diff(
            base=base,
            head=head,
            files=[DiffFile(path=path, hunks=[hunk], language=language, additions=len(hunk.added_lines), deletions=len(hunk.removed_lines))],
        )

    return _make


@pytest.fixture
def make_finding() -> Callable[..., Finding]:
    """Build a :class:`~mergesignal.models.Finding` with sensible defaults."""

    def _make(signal: str = "semantic", severity: str = "high", title: str = "example finding", **kwargs: object) -> Finding:
        return Finding(signal=signal, severity=severity, title=title, **kwargs)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def make_report(make_finding: Callable[..., Finding]) -> Callable[..., Report]:
    """Build a :class:`~mergesignal.models.Report` covering every signal status.

    The default report has one ``ok``, one ``findings``, one ``skipped`` and one
    ``error`` signal, which is exactly what renderer snapshot tests need.
    """

    def _make(*, base: str = "main", head: str = "feature", signals: list[Signal] | None = None) -> Report:
        if signals is None:
            signals = [
                Signal(name="conflicts", status="ok", summary="merges cleanly"),
                Signal(name="semantic", status="findings", summary="1 potential break", findings=[make_finding(file="lib.py", line=3)]),
                Signal(name="overlap", status="skipped", summary="no other branches supplied"),
                Signal(name="risk", status="error", summary="not implemented"),
            ]
        return Report(base=base, head=head, merge_base="0" * 40, signals=signals)

    return _make


@pytest.fixture
def make_context(config: Config, tmp_path: Path) -> Callable[..., AnalysisContext]:
    """Build an :class:`~mergesignal.models.AnalysisContext` for signal tests."""

    def _make(**overrides: object) -> AnalysisContext:
        kwargs: dict[str, object] = {
            "repo_path": str(tmp_path),
            "base": "main",
            "head": "feature",
            "merge_base": "0" * 40,
            "config": config,
        }
        kwargs.update(overrides)
        return AnalysisContext(**kwargs)  # type: ignore[arg-type]

    return _make


# --------------------------------------------------------------- environment


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip GitHub credentials from the environment for every test.

    Prevents a developer's real ``GITHUB_TOKEN`` from leaking into a test that
    accidentally makes a network call, and keeps offline behaviour (FR-8) as the
    default the suite exercises.
    """
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "MERGESIGNAL_APP_ID", "MERGESIGNAL_PRIVATE_KEY", "MERGESIGNAL_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    yield
