"""Integration tests: real repositories -> real ``git diff`` -> parsed models."""

from __future__ import annotations

import pytest

from mergesignal.analysis.diff import changed_line_numbers, diff_refs
from mergesignal.git.repo import GitError, Repo
from tests.helpers.repo_builder import RepoBuilder

pytestmark = pytest.mark.integration


def test_identical_refs_produce_an_empty_diff(builder: RepoBuilder) -> None:
    builder.file("a.py", "def a():\n    return 1\n").commit("one")
    repo = Repo(builder.build())
    diff = diff_refs(repo, "HEAD", "HEAD")
    assert diff.files == []
    assert diff.total_churn == 0


def test_modification_across_two_commits(builder: RepoBuilder) -> None:
    builder.file("app.py", "def greet(name):\n    return name\n").commit("one")
    builder.file("app.py", "def greet(name, loud=False):\n    return name\n").commit("two")
    repo = Repo(builder.build())
    diff = diff_refs(repo, "HEAD~1", "HEAD")
    (changed,) = diff.files
    assert changed.path == "app.py"
    assert changed.language == "python"
    assert changed.additions == 1 and changed.deletions == 1
    assert changed_line_numbers(changed, side="head") == {1}


def test_new_and_deleted_files(builder: RepoBuilder) -> None:
    builder.file("gone.py", "x = 1\n").commit("one")
    builder.remove("gone.py").file("fresh.py", "y = 2\n").commit("two")
    repo = Repo(builder.build())
    diff = diff_refs(repo, "HEAD~1", "HEAD")
    by_path = {f.path: f for f in diff.files}
    assert by_path["gone.py"].is_deleted is True
    assert by_path["fresh.py"].is_new is True
    assert by_path["fresh.py"].old_path is None


def test_rename_is_detected_and_can_be_disabled(builder: RepoBuilder) -> None:
    body = "".join(f"line {i}\n" for i in range(40))
    builder.file("old/name.txt", body).commit("one")
    builder.move("old/name.txt", "new/name.txt").commit("two")
    repo = Repo(builder.build())

    (renamed,) = diff_refs(repo, "HEAD~1", "HEAD").files
    assert (renamed.path, renamed.old_path) == ("new/name.txt", "old/name.txt")
    assert renamed.is_rename is True

    split = diff_refs(repo, "HEAD~1", "HEAD", detect_renames=False)
    assert {f.path for f in split.files} == {"old/name.txt", "new/name.txt"}
    assert all(f.old_path is None for f in split.files)


def test_binary_files_are_flagged_and_carry_no_hunks(builder: RepoBuilder) -> None:
    builder.scenario_binary_file()
    repo = Repo(builder.build())
    diff = diff_refs(repo, "main", "feature")
    (changed,) = diff.files
    assert changed.path == "asset.bin"
    assert changed.is_binary is True
    assert changed.hunks == []
    assert changed.language is None


def test_unsupported_language_still_produces_hunks(builder: RepoBuilder) -> None:
    builder.scenario_unsupported_language()
    repo = Repo(builder.build())
    (changed,) = diff_refs(repo, "main", "feature").files
    assert changed.path == "script.zzz"
    assert changed.language is None, "textual fallback path"
    assert changed.hunks, "a diff is still a diff without a grammar"


def test_paths_with_spaces_and_unicode(builder: RepoBuilder) -> None:
    builder.file("dir with space/café.py", "a = 1\n").commit("one")
    builder.file("dir with space/café.py", "a = 2\n").commit("two")
    repo = Repo(builder.build())
    (changed,) = diff_refs(repo, "HEAD~1", "HEAD").files
    assert changed.path == "dir with space/café.py"
    assert changed.language == "python"


def test_merge_base_mode_reports_only_the_head_side(builder: RepoBuilder) -> None:
    builder.file("shared.py", "x = 1\n").commit("base")
    builder.branch("feature")
    builder.file("feature_only.py", "f = 1\n").commit("feature work")
    builder.checkout("main")
    builder.file("main_only.py", "m = 1\n").commit("main work")
    repo = Repo(builder.build())

    three_dot = diff_refs(repo, "main", "feature", merge_base=True)
    assert three_dot.paths == {"feature_only.py"}

    two_dot = diff_refs(repo, "main", "feature")
    assert two_dot.paths == {"feature_only.py", "main_only.py"}


def test_context_lines_do_not_change_hunk_ranges(builder: RepoBuilder) -> None:
    body = "".join(f"line {i}\n" for i in range(20))
    builder.file("ctx.txt", body).commit("one")
    builder.file("ctx.txt", body.replace("line 10", "line TEN")).commit("two")
    repo = Repo(builder.build())
    tight = diff_refs(repo, "HEAD~1", "HEAD", context_lines=0).files[0]
    loose = diff_refs(repo, "HEAD~1", "HEAD", context_lines=3).files[0]
    assert changed_line_numbers(tight, side="head") == {11}
    assert tight.additions == 1 and tight.deletions == 1
    assert loose.additions == 1 and loose.deletions == 1


def test_max_files_truncates(builder: RepoBuilder) -> None:
    for index in range(5):
        builder.file(f"f{index}.py", "x = 1\n")
    builder.commit("one")
    for index in range(5):
        builder.file(f"f{index}.py", "x = 2\n")
    builder.commit("two")
    repo = Repo(builder.build())
    assert len(diff_refs(repo, "HEAD~1", "HEAD").files) == 5
    assert len(diff_refs(repo, "HEAD~1", "HEAD", max_files=2).files) == 2


def test_unknown_ref_raises_git_error(builder: RepoBuilder) -> None:
    builder.file("a.py", "x = 1\n").commit("one")
    repo = Repo(builder.build())
    with pytest.raises(GitError):
        diff_refs(repo, "HEAD", "no-such-ref")
