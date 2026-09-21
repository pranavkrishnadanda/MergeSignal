"""Tests for the shared test infrastructure itself.

If RepoBuilder is wrong, every integration and regression test downstream is
wrong in the same way, so it gets its own coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.repo_builder import (
    COMMIT_INTERVAL_SECONDS,
    RepoBuilder,
    RepoBuilderError,
    build_repo,
)


def test_builder_creates_repository_on_default_branch(builder: RepoBuilder) -> None:
    assert (builder.path / ".git").exists()
    assert builder.current_branch() == "main"


def test_file_commit_chain_is_fluent(builder: RepoBuilder) -> None:
    path = builder.file("a.py", "x = 1").commit("first").file("b.py", "y = 2").commit("second").build()
    assert isinstance(path, Path)
    assert [c.message for c in builder.commits] == ["first", "second"]
    assert builder.git("ls-tree", "-r", "--name-only", "HEAD").split() == ["a.py", "b.py"]


def test_file_adds_trailing_newline(builder: RepoBuilder) -> None:
    builder.file("a.py", "no newline").commit("c")
    assert builder.read("a.py") == "no newline\n"


def test_commit_timestamps_advance_deterministically(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("one").file("a", "2").commit("two")
    first, second = builder.commits
    assert (second.timestamp - first.timestamp).total_seconds() == COMMIT_INTERVAL_SECONDS
    dates = builder.git("log", "--format=%ad", "--date=iso-strict").splitlines()
    assert len(set(dates)) == 2


def test_author_override_per_commit(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("mine")
    builder.file("a", "2").commit("theirs", author=("Other", "other@example.com"))
    emails = builder.git("log", "--format=%ae").splitlines()
    assert emails == ["other@example.com", "author@example.com"]


def test_branch_and_checkout(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("init").branch("feature")
    assert builder.current_branch() == "feature"
    builder.checkout("main")
    assert builder.current_branch() == "main"


def test_detached_head_reports_none(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("init").checkout("HEAD", detach=True)
    assert builder.current_branch() is None


def test_move_is_detected_as_rename(builder: RepoBuilder) -> None:
    builder.file("old.py", "def f():\n    return 1\n").commit("add")
    builder.move("old.py", "new.py").commit("rename")
    status = builder.git("diff", "--name-status", "-M", "HEAD~1", "HEAD")
    assert status.startswith("R")
    assert "old.py" in status and "new.py" in status


def test_remove_deletes_file(builder: RepoBuilder) -> None:
    builder.file("a.py", "1").commit("add").remove("a.py").commit("drop")
    assert not (builder.path / "a.py").exists()


def test_binary_file_is_recorded_as_binary(builder: RepoBuilder) -> None:
    builder.binary("blob.bin", bytes(range(256))).commit("add binary")
    builder.binary("blob.bin", bytes(range(255, -1, -1))).commit("change binary")
    assert "Binary files" in builder.git("diff", "HEAD~1", "HEAD")


def test_append_requires_existing_file(builder: RepoBuilder) -> None:
    with pytest.raises(RepoBuilderError):
        builder.append("missing.py", "more")


def test_merge_of_diverged_branches(builder: RepoBuilder) -> None:
    builder.scenario_clean_merge()
    builder.merge("feature")
    assert (builder.path / "b.py").exists()


def test_conflicting_merge_raises_and_aborts(builder: RepoBuilder) -> None:
    builder.scenario_textual_conflict()
    with pytest.raises(RepoBuilderError, match="conflicted"):
        builder.merge("feature")
    assert "<<<<<<<" not in builder.read("conflict.txt")


def test_conflicting_merge_can_be_kept(builder: RepoBuilder) -> None:
    builder.scenario_textual_conflict()
    builder.merge("feature", allow_conflict=True)
    assert "<<<<<<<" in builder.read("conflict.txt")
    builder.git("merge", "--abort", check=False)


def test_tag_creation(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("init").tag("v1", message="release")
    assert builder.git("tag", "--list") == "v1"


@pytest.mark.parametrize(
    "scenario",
    [
        "scenario_clean_merge",
        "scenario_textual_conflict",
        "scenario_rename_vs_new_caller",
        "scenario_signature_change",
        "scenario_binary_file",
        "scenario_unsupported_language",
    ],
)
def test_scenarios_produce_two_branches(builder: RepoBuilder, scenario: str) -> None:
    getattr(builder, scenario)()
    branches = builder.git("branch", "--format=%(refname:short)").split()
    assert set(branches) == {"main", "feature"}
    assert builder.current_branch() == "main"


def test_build_repo_shortcut(tmp_path: Path) -> None:
    path = build_repo(tmp_path / "quick", {"x.py": "x = 1"})
    assert (path / "x.py").exists()
