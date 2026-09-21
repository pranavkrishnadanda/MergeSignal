"""Tests for the fully implemented git wrapper."""

from __future__ import annotations

from pathlib import Path

import pytest

from mergesignal.git.repo import (
    MERGE_TREE_MIN_VERSION,
    GitError,
    GitVersion,
    Repo,
    _parse_name_status_z,
)
from tests.helpers.repo_builder import RepoBuilder


def test_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="does not exist"):
        Repo(tmp_path / "nope")


def test_is_repository(tmp_path: Path, simple_repo: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert Repo(simple_repo).is_repository()
    assert not Repo(plain).is_repository()


def test_git_version_parses_vendor_suffix(repo: Repo) -> None:
    version = repo.git_version()
    assert version.major >= 2
    assert str(version).count(".") == 2
    assert version >= GitVersion(1, 0, 0)


def test_version_ordering() -> None:
    assert GitVersion(2, 38, 0) >= MERGE_TREE_MIN_VERSION
    assert GitVersion(2, 37, 9) < MERGE_TREE_MIN_VERSION
    assert GitVersion(3, 0, 0) > GitVersion(2, 99, 99)


def test_rev_parse_and_ref_exists(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("init").branch("feature")
    repo = Repo(builder.path)
    sha = repo.rev_parse("feature")
    assert len(sha) == 40
    assert repo.rev_parse("HEAD") == sha
    assert repo.ref_exists("main")
    assert not repo.ref_exists("nope")


def test_rev_parse_unknown_ref_raises(repo: Repo) -> None:
    with pytest.raises(GitError, match="unknown or unborn ref"):
        repo.rev_parse("does-not-exist")


def test_rev_parse_unborn_branch_raises(tmp_path: Path) -> None:
    builder = RepoBuilder(tmp_path / "empty")
    repo = Repo(builder.path)
    assert repo.is_repository()
    with pytest.raises(GitError):
        repo.rev_parse("HEAD")


def test_annotated_tag_resolves_to_commit(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("init").tag("v1", message="release")
    repo = Repo(builder.path)
    assert repo.rev_parse("v1") == repo.rev_parse("HEAD")


def test_merge_base(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("base")
    base_sha = builder.sha()
    builder.branch("feature").file("b", "2").commit("feature work")
    builder.checkout("main").file("c", "3").commit("main work")
    repo = Repo(builder.path)
    assert repo.merge_base("main", "feature") == base_sha


def test_merge_base_unrelated_histories_returns_none(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("main work")
    builder.git("checkout", "--quiet", "--orphan", "orphan")
    builder.git("rm", "-rf", "--quiet", ".")
    builder.file("z", "9").commit("orphan work")
    repo = Repo(builder.path)
    assert repo.merge_base("main", "orphan") is None


def test_is_ancestor(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("one")
    first = builder.sha()
    builder.file("a", "2").commit("two")
    repo = Repo(builder.path)
    assert repo.is_ancestor(first, "HEAD")
    assert not repo.is_ancestor("HEAD", first)


def test_current_branch_and_detached_head(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("init")
    repo = Repo(builder.path)
    assert repo.current_branch() == "main"
    builder.checkout("HEAD", detach=True)
    assert repo.current_branch() is None


def test_list_branches(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("init").branch("feature").checkout("main")
    repo = Repo(builder.path)
    assert sorted(repo.list_branches()) == ["feature", "main"]
    assert repo.list_branches(pattern="feat*") == ["feature"]
    assert repo.list_branches(remote=True) == []


def test_list_branches_empty_repo(tmp_path: Path) -> None:
    builder = RepoBuilder(tmp_path / "empty")
    assert Repo(builder.path).list_branches() == []


def test_diff_names_includes_both_rename_paths(builder: RepoBuilder) -> None:
    builder.file("old.py", "def f():\n    return 1\n").commit("add")
    builder.move("old.py", "new.py").commit("rename")
    repo = Repo(builder.path)
    assert repo.diff_names("HEAD~1", "HEAD") == ["new.py", "old.py"]


def test_diff_names_handles_paths_with_spaces_and_unicode(builder: RepoBuilder) -> None:
    builder.file("dir with space/ünïcode.py", "x = 1\n").commit("odd path")
    builder.file("dir with space/ünïcode.py", "x = 2\n").commit("edit")
    repo = Repo(builder.path)
    assert repo.diff_names("HEAD~1", "HEAD") == ["dir with space/ünïcode.py"]


def test_diff_names_merge_base_mode(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("base")
    builder.branch("feature").file("feature-only", "f").commit("feature work")
    builder.checkout("main").file("main-only", "m").commit("main work")
    repo = Repo(builder.path)
    assert repo.diff_names("main", "feature", merge_base=True) == ["feature-only"]
    assert repo.diff_names("main", "feature") == ["feature-only", "main-only"]


def test_file_content_at(builder: RepoBuilder) -> None:
    builder.file("a.py", "first\n").commit("one")
    builder.file("a.py", "second\n").commit("two")
    repo = Repo(builder.path)
    assert repo.file_content_at("HEAD~1", "a.py") == "first\n"
    assert repo.file_content_at("HEAD", "a.py") == "second\n"


def test_file_content_at_missing_path_returns_none(builder: RepoBuilder) -> None:
    builder.file("a.py", "1\n").commit("one")
    repo = Repo(builder.path)
    assert repo.file_content_at("HEAD", "nope.py") is None


def test_file_content_at_unknown_ref_raises(builder: RepoBuilder) -> None:
    builder.file("a.py", "1\n").commit("one")
    repo = Repo(builder.path)
    with pytest.raises(GitError, match="unknown ref"):
        repo.file_content_at("no-such-ref", "a.py")


def test_file_bytes_at_survives_binary(builder: RepoBuilder) -> None:
    payload = bytes(range(256))
    builder.binary("blob.bin", payload).commit("binary")
    repo = Repo(builder.path)
    assert repo.file_bytes_at("HEAD", "blob.bin") == payload
    assert repo.file_content_at("HEAD", "blob.bin") is not None


def test_list_files_at(builder: RepoBuilder) -> None:
    builder.file("a.py", "1").file("pkg/b.py", "2").commit("init")
    repo = Repo(builder.path)
    assert repo.list_files_at("HEAD") == ["a.py", "pkg/b.py"]


def test_run_check_false_returns_stdout_on_failure(repo: Repo) -> None:
    assert repo.run(["rev-parse", "--verify", "--quiet", "nope"], check=False) == ""


def test_run_check_true_raises(repo: Repo) -> None:
    with pytest.raises(GitError):
        repo.run(["cat-file", "-p", "deadbeef"], check=True)


def test_run_does_not_mutate_repository(builder: RepoBuilder) -> None:
    builder.file("a", "1").commit("init")
    repo = Repo(builder.path)
    before = builder.git("status", "--porcelain=v1")
    repo.diff_names("HEAD", "HEAD")
    repo.list_branches()
    repo.file_content_at("HEAD", "a")
    assert builder.git("status", "--porcelain=v1") == before


def test_parse_name_status_z_handles_renames() -> None:
    payload = "M\0a.py\0R100\0old.py\0new.py\0A\0c.py\0"
    assert _parse_name_status_z(payload) == {"a.py", "old.py", "new.py", "c.py"}


def test_supports_merge_tree_matches_version(repo: Repo) -> None:
    assert repo.supports_merge_tree() == (repo.git_version() >= MERGE_TREE_MIN_VERSION)
