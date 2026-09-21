"""Integration tests for merge simulation against real git repositories.

Every test here builds an actual repository with :class:`RepoBuilder` and runs
real git. Mocking git would only test our assumptions about ``merge-tree``,
which is precisely the thing that has to be right.

Both strategies are exercised on the same fixtures and asserted to agree:

* ``merge-tree`` runs whenever the host git is >= 2.38 (``requires_merge_tree``).
* ``worktree-fallback`` is forced with ``prefer_merge_tree=False``, so the
  fallback is covered by real git on every host regardless of version. A
  separate mocked test proves :func:`simulate_merge` *routes* to it when
  ``Repo.supports_merge_tree()`` reports an old git.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mergesignal.git.merge_sim import (
    STRATEGY_MERGE_TREE,
    STRATEGY_WORKTREE,
    UNRELATED_HISTORIES_PATH,
    conflicted_paths,
    merge_tree_simulate,
    simulate_merge,
    temporary_worktree,
    worktree_simulate,
)
from mergesignal.git.repo import GitError, Repo
from mergesignal.models import MergeSimulation
from tests.conftest import requires_merge_tree
from tests.helpers.repo_builder import RepoBuilder

pytestmark = pytest.mark.integration

#: Run the same assertions through both code paths.
BOTH_STRATEGIES = pytest.mark.parametrize("prefer_merge_tree", [pytest.param(True, marks=requires_merge_tree), False])


def _strategy(prefer_merge_tree: bool) -> str:
    return STRATEGY_MERGE_TREE if prefer_merge_tree else STRATEGY_WORKTREE


def _snapshot(repo: Repo) -> tuple[str, str, str]:
    """Everything a simulation is forbidden to change."""
    return (
        repo.run(["status", "--porcelain"]),
        repo.run(["rev-parse", "HEAD"]),
        repo.run(["worktree", "list"]),
    )


# --------------------------------------------------- (a) clean merge, no conflicts


@BOTH_STRATEGIES
def test_clean_merge_reports_no_conflicts(two_branch_repo: Path, prefer_merge_tree: bool) -> None:
    repo = Repo(two_branch_repo)

    sim = simulate_merge(repo, "main", "feature", prefer_merge_tree=prefer_merge_tree)

    assert isinstance(sim, MergeSimulation)
    assert sim.clean is True
    assert sim.conflicted_files == []
    assert sim.regions == []
    assert sim.up_to_date is False
    assert sim.merge_base is not None
    assert sim.strategy == _strategy(prefer_merge_tree)


@BOTH_STRATEGIES
def test_simulation_never_mutates_the_repository(conflict_repo: Path, prefer_merge_tree: bool) -> None:
    """NFR-2: no working-tree, HEAD or worktree-list change, even on conflict."""
    repo = Repo(conflict_repo)
    before = _snapshot(repo)

    simulate_merge(repo, "main", "feature", prefer_merge_tree=prefer_merge_tree)

    assert _snapshot(repo) == before


# ------------------------------------------- (b) same-line edits, real regions


@BOTH_STRATEGIES
def test_same_line_edits_produce_conflict_regions(conflict_repo: Path, prefer_merge_tree: bool) -> None:
    repo = Repo(conflict_repo)

    sim = simulate_merge(repo, "main", "feature", prefer_merge_tree=prefer_merge_tree)

    assert sim.clean is False
    assert sim.conflicted_files == ["conflict.txt"]
    assert len(sim.regions) == 2

    first, second = sim.regions
    assert first.file == "conflict.txt"
    assert first.ours_text == "line 1 MAIN\n"
    assert first.theirs_text == "line 1 FEATURE\n"
    assert first.base_text == "line 1\n"
    assert first.is_binary is False
    assert second.ours_text == "line 21 MAIN\n"
    assert second.theirs_text == "line 21 FEATURE\n"

    # Ranges are ordered, non-overlapping and point past the marker lines.
    assert first.ours_range.start < first.theirs_range.start < second.ours_range.start
    assert first.ours_range.length == 1
    assert first.theirs_range.length == 1


@requires_merge_tree
def test_both_strategies_agree_on_the_same_conflict(conflict_repo: Path) -> None:
    repo = Repo(conflict_repo)

    fast = merge_tree_simulate(repo, "main", "feature")
    slow = worktree_simulate(repo, "main", "feature")

    assert fast.conflicted_files == slow.conflicted_files
    assert [r.model_dump(exclude={"is_binary"}) for r in fast.regions] == [
        r.model_dump(exclude={"is_binary"}) for r in slow.regions
    ]
    assert fast.tree_sha is not None
    assert slow.tree_sha is None


@requires_merge_tree
def test_merge_tree_records_the_written_tree(conflict_repo: Path) -> None:
    repo = Repo(conflict_repo)

    sim = merge_tree_simulate(repo, "main", "feature")

    assert sim.tree_sha is not None
    # The recorded tree really exists in the object database.
    assert repo.run(["cat-file", "-t", sim.tree_sha]) == "tree"


@BOTH_STRATEGIES
def test_regions_can_be_extraction_free(conflict_repo: Path, prefer_merge_tree: bool) -> None:
    repo = Repo(conflict_repo)

    sim = simulate_merge(repo, "main", "feature", prefer_merge_tree=prefer_merge_tree, extract_regions=False)

    assert sim.conflicted_files == ["conflict.txt"]
    assert sim.regions == []


def test_conflicted_paths_helper_names_the_conflict(conflict_repo: Path) -> None:
    assert conflicted_paths(Repo(conflict_repo), "main", "feature") == ["conflict.txt"]


def test_conflicted_paths_helper_is_empty_for_a_clean_merge(two_branch_repo: Path) -> None:
    assert conflicted_paths(Repo(two_branch_repo), "main", "feature") == []


# ------------------------------------------- (c) fallback routing and cleanup


def test_simulate_merge_routes_to_worktree_on_old_git(conflict_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts with git < 2.38 must transparently take the fallback path.

    We cannot install an old git in CI, so the *routing decision* is mocked while
    the fallback itself runs for real.
    """
    repo = Repo(conflict_repo)
    monkeypatch.setattr(Repo, "supports_merge_tree", lambda self: False)

    sim = simulate_merge(repo, "main", "feature")

    assert sim.strategy == STRATEGY_WORKTREE
    assert sim.tree_sha is None
    assert sim.conflicted_files == ["conflict.txt"]


def test_simulate_merge_uses_merge_tree_when_supported(conflict_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Repo(conflict_repo)
    monkeypatch.setattr(Repo, "supports_merge_tree", lambda self: True)

    assert simulate_merge(repo, "main", "feature").strategy == STRATEGY_MERGE_TREE


def test_temporary_worktree_is_removed_on_success(simple_repo: Path) -> None:
    repo = Repo(simple_repo)

    with temporary_worktree(repo, "main") as workdir:
        assert (workdir / "README.md").is_file()
        assert str(workdir) in repo.run(["worktree", "list"])
        parent = workdir.parent

    assert not workdir.exists()
    assert not parent.exists()
    assert str(workdir) not in repo.run(["worktree", "list"])


def test_temporary_worktree_is_removed_on_exception(simple_repo: Path) -> None:
    """Cleanup must not be skipped — and must not mask the caller's error."""
    repo = Repo(simple_repo)
    captured: list[Path] = []

    with pytest.raises(RuntimeError, match="boom"), temporary_worktree(repo, "main") as workdir:
        captured.append(workdir)
        raise RuntimeError("boom")

    (workdir,) = captured
    assert not workdir.exists()
    assert str(workdir) not in repo.run(["worktree", "list"])


def test_temporary_worktree_on_bad_ref_leaves_nothing_behind(simple_repo: Path) -> None:
    repo = Repo(simple_repo)
    before = repo.run(["worktree", "list"])

    with pytest.raises(GitError), temporary_worktree(repo, "no-such-ref"):
        pytest.fail("worktree add should have raised")

    assert repo.run(["worktree", "list"]) == before


def test_worktree_fallback_survives_repeated_runs(conflict_repo: Path) -> None:
    """Back-to-back fallbacks must not collide on a leftover scratch directory."""
    repo = Repo(conflict_repo)

    first = worktree_simulate(repo, "main", "feature")
    second = worktree_simulate(repo, "main", "feature")

    assert first.conflicted_files == second.conflicted_files
    assert repo.run(["worktree", "list"]).count("\n") == 0  # only the main worktree


# ------------------------------------------------------------- edge cases


@BOTH_STRATEGIES
def test_already_up_to_date_is_clean_not_an_error(builder: RepoBuilder, prefer_merge_tree: bool) -> None:
    builder.file("a.txt", "1\n").commit("base").branch("feature").checkout("main")
    builder.file("b.txt", "2\n").commit("main moves on")
    repo = Repo(builder.build())

    sim = simulate_merge(repo, "main", "feature", prefer_merge_tree=prefer_merge_tree)

    assert sim.up_to_date is True
    assert sim.clean is True
    assert sim.conflicted_files == []
    assert sim.merge_base is not None


@BOTH_STRATEGIES
def test_merging_a_ref_into_itself_is_up_to_date(simple_repo: Path, prefer_merge_tree: bool) -> None:
    sim = simulate_merge(Repo(simple_repo), "main", "main", prefer_merge_tree=prefer_merge_tree)

    assert (sim.up_to_date, sim.clean) == (True, True)


@BOTH_STRATEGIES
def test_binary_conflict_is_marked_not_decoded(builder: RepoBuilder, prefer_merge_tree: bool) -> None:
    builder.scenario_binary_file()
    repo = Repo(builder.build())

    sim = simulate_merge(repo, "main", "feature", prefer_merge_tree=prefer_merge_tree)

    assert sim.conflicted_files == ["asset.bin"]
    (region,) = sim.regions
    assert region.is_binary is True
    assert region.ours_text is None
    assert region.theirs_text is None
    assert region.ours_range.length == 0
    assert region.theirs_range.length == 0


@BOTH_STRATEGIES
def test_paths_with_spaces_and_unicode_survive_parsing(builder: RepoBuilder, prefer_merge_tree: bool) -> None:
    path = "dir with space/ünïcodé — file.txt"
    builder.file(path, "a\nb\nc\n").commit("base")
    builder.branch("feature").file(path, "FEATURE\nb\nc\n").commit("feature edit")
    builder.checkout("main").file(path, "MAIN\nb\nc\n").commit("main edit")
    repo = Repo(builder.build())

    sim = simulate_merge(repo, "main", "feature", prefer_merge_tree=prefer_merge_tree)

    assert sim.conflicted_files == [path]
    (region,) = sim.regions
    assert region.file == path
    assert region.ours_text == "MAIN\n"
    assert region.theirs_text == "FEATURE\n"


@BOTH_STRATEGIES
def test_modify_delete_conflict_keeps_the_path(builder: RepoBuilder, prefer_merge_tree: bool) -> None:
    """A conflict with no markers must still name the file (not be dropped)."""
    builder.file("doomed.py", "a\nb\nc\n").commit("base")
    builder.branch("feature").remove("doomed.py").commit("delete it")
    builder.checkout("main").file("doomed.py", "a\nb\nCHANGED\n").commit("edit it")
    repo = Repo(builder.build())

    sim = simulate_merge(repo, "main", "feature", prefer_merge_tree=prefer_merge_tree)

    assert sim.clean is False
    assert sim.conflicted_files == ["doomed.py"]
    (region,) = sim.regions
    assert region.file == "doomed.py"
    assert region.ours_range.length > 0  # whole-file extent, never an empty shell


@BOTH_STRATEGIES
def test_add_add_conflict_produces_a_region(builder: RepoBuilder, prefer_merge_tree: bool) -> None:
    builder.file("seed.txt", "seed\n").commit("base")
    builder.branch("feature").file("new.txt", "FEATURE\n").commit("feature adds")
    builder.checkout("main").file("new.txt", "MAIN\n").commit("main adds")
    repo = Repo(builder.build())

    sim = simulate_merge(repo, "main", "feature", prefer_merge_tree=prefer_merge_tree)

    assert sim.conflicted_files == ["new.txt"]
    (region,) = sim.regions
    assert region.ours_text == "MAIN\n"
    assert region.theirs_text == "FEATURE\n"


@BOTH_STRATEGIES
def test_several_conflicted_files_are_sorted(builder: RepoBuilder, prefer_merge_tree: bool) -> None:
    for name in ("zeta.txt", "alpha.txt", "mid.txt"):
        builder.file(name, "base\n")
    builder.commit("base")
    builder.branch("feature")
    for name in ("zeta.txt", "alpha.txt", "mid.txt"):
        builder.file(name, "FEATURE\n")
    builder.commit("feature edits").checkout("main")
    for name in ("zeta.txt", "alpha.txt", "mid.txt"):
        builder.file(name, "MAIN\n")
    builder.commit("main edits")
    repo = Repo(builder.build())

    sim = simulate_merge(repo, "main", "feature", prefer_merge_tree=prefer_merge_tree)

    assert sim.conflicted_files == ["alpha.txt", "mid.txt", "zeta.txt"]
    assert [r.file for r in sim.regions] == ["alpha.txt", "mid.txt", "zeta.txt"]


@BOTH_STRATEGIES
def test_unrelated_histories_are_reported_not_raised(builder: RepoBuilder, prefer_merge_tree: bool) -> None:
    builder.file("a.txt", "1\n").commit("main root")
    builder.git("checkout", "--orphan", "other")
    builder.git("rm", "-rf", "-q", ".", check=False)
    builder.file("z.txt", "z\n").commit("unrelated root")
    repo = Repo(builder.build())

    sim = simulate_merge(repo, "main", "other", prefer_merge_tree=prefer_merge_tree)

    assert sim.merge_base is None
    assert sim.clean is False
    assert sim.conflicted_files == [UNRELATED_HISTORIES_PATH]
    (region,) = sim.regions
    assert region.file == UNRELATED_HISTORIES_PATH
    assert region.is_binary is True


@BOTH_STRATEGIES
def test_unknown_ref_raises_git_error(two_branch_repo: Path, prefer_merge_tree: bool) -> None:
    repo = Repo(two_branch_repo)

    with pytest.raises(GitError, match="unknown or unborn ref"):
        simulate_merge(repo, "main", "no-such-branch", prefer_merge_tree=prefer_merge_tree)

    with pytest.raises(GitError, match="unknown or unborn ref"):
        simulate_merge(repo, "no-such-branch", "main", prefer_merge_tree=prefer_merge_tree)


@BOTH_STRATEGIES
def test_unborn_branch_raises_git_error(builder: RepoBuilder, prefer_merge_tree: bool) -> None:
    """A freshly initialised repo has no commit on HEAD; GitError must escape."""
    repo = Repo(builder.build())

    with pytest.raises(GitError, match="unknown or unborn ref"):
        simulate_merge(repo, "main", "main", prefer_merge_tree=prefer_merge_tree)


@BOTH_STRATEGIES
def test_detached_head_refs_are_simulatable(conflict_repo: Path, prefer_merge_tree: bool) -> None:
    """NFR-3: refs may be raw shas on a detached HEAD."""
    repo = Repo(conflict_repo)
    base_sha = repo.rev_parse("main")
    head_sha = repo.rev_parse("feature")
    repo.run(["checkout", "--detach", "--quiet", base_sha])

    sim = simulate_merge(repo, base_sha, head_sha, prefer_merge_tree=prefer_merge_tree)

    assert sim.conflicted_files == ["conflict.txt"]


@BOTH_STRATEGIES
def test_tags_work_as_refs(conflict_repo: Path, prefer_merge_tree: bool) -> None:
    repo = Repo(conflict_repo)
    repo.run(["tag", "-a", "-m", "release", "v1", "feature"])

    sim = simulate_merge(repo, "main", "v1", prefer_merge_tree=prefer_merge_tree)

    assert sim.conflicted_files == ["conflict.txt"]


# ----------------------------------------------- history over the same repos


def test_history_stats_over_a_built_repo(tmp_path: Path) -> None:
    """End-to-end sanity check that history reads the repos merge_sim simulates."""
    from mergesignal.git.history import collect_history

    now = datetime.now(UTC)
    builder = RepoBuilder(tmp_path / "joint", start_time=now - timedelta(days=4))
    builder.file("api.py", "1\n").file("api_test.py", "1\n").commit("feature one")
    builder.file("api.py", "2\n").file("api_test.py", "2\n").commit("feature two")
    repo = Repo(builder.build())

    stats = collect_history(repo, ["api.py"])

    assert stats.churn == {"api.py": 2}
    assert stats.co_change == {"api.py": {"api_test.py": 2}}
    assert stats.authors["api.py"] == {"author@example.com": 2}
    assert stats.truncated is False
