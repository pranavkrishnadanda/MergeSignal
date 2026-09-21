"""Integration tests for S1 against real git repositories built by RepoBuilder.

Covers the DESIGN.md §4 regression scenarios that S1 owns: clean merge, textual
conflict, binary file in the diff, and an empty diff (base == head).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mergesignal.config import Config
from mergesignal.models import AnalysisContext
from mergesignal.signals import conflicts
from tests.helpers.repo_builder import RepoBuilder

pytestmark = pytest.mark.integration


def context(path: Path, *, base: str = "main", head: str = "feature") -> AnalysisContext:
    """Minimal context — S1 only needs a repository and two refs."""
    return AnalysisContext(repo_path=str(path), base=base, head=head, config=Config())


def test_clean_merge_reports_ok(two_branch_repo: Path) -> None:
    signal = conflicts.analyze(context(two_branch_repo))

    assert signal.status == "ok"
    assert signal.findings == []
    assert signal.metadata["conflicted_files"] == 0
    assert signal.metadata["strategy"] in ("merge-tree", "worktree-fallback")


def test_textual_conflict_reports_both_regions(conflict_repo: Path) -> None:
    signal = conflicts.analyze(context(conflict_repo))

    assert signal.status == "findings"
    assert signal.metadata["conflicted_files"] == 1
    assert signal.metadata["regions"] == 2
    assert len(signal.findings) == 2
    assert all(f.file == "conflict.txt" for f in signal.findings)
    assert all(f.severity == "high" and f.confidence == "high" for f in signal.findings)

    lines = sorted(f.line for f in signal.findings)
    assert lines == sorted(set(lines))
    first = signal.findings[0]
    assert "MAIN" in first.evidence["ours_excerpt"]
    assert "FEATURE" in first.evidence["theirs_excerpt"]


def test_binary_conflict_is_critical_and_never_parsed(builder: RepoBuilder) -> None:
    path = builder.scenario_binary_file().build()

    signal = conflicts.analyze(context(path))

    assert signal.status == "findings"
    (finding,) = [f for f in signal.findings if f.file == "asset.bin"]
    assert finding.severity == "critical"
    assert finding.evidence["binary"] is True
    assert "ours_excerpt" not in finding.evidence


def test_base_equals_head_is_up_to_date(simple_repo: Path) -> None:
    signal = conflicts.analyze(context(simple_repo, base="main", head="main"))

    assert signal.status == "ok"
    assert signal.metadata["up_to_date"] is True
    assert signal.findings == []


def test_already_merged_branch_is_not_an_error(builder: RepoBuilder) -> None:
    path = builder.scenario_clean_merge().merge("feature").build()

    signal = conflicts.analyze(context(path))

    assert signal.status == "ok"
    assert signal.metadata["up_to_date"] is True


def test_unknown_ref_becomes_an_error_signal(simple_repo: Path) -> None:
    signal = conflicts.analyze(context(simple_repo, head="does-not-exist"))

    assert signal.status == "error"
    assert "GitError" in signal.summary


def test_not_a_repository_becomes_an_error_signal(tmp_path: Path) -> None:
    signal = conflicts.analyze(context(tmp_path / "nowhere"))

    assert signal.status == "error"
