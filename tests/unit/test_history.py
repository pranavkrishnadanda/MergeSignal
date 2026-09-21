"""Tests for :mod:`mergesignal.git.history`.

The parsing layer (:func:`_parse_log`) is exercised against captured ``git log``
payloads; the aggregation layer is exercised against small real repositories,
because the value of these statistics is entirely in matching git's actual
``--name-only`` behaviour.

Note on dates: :class:`RepoBuilder` defaults to a commit clock in 2024, which
falls outside the default 90-day window. Every repository built here therefore
pins ``start_time`` relative to *now* so the ``--since`` bound is meaningful.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mergesignal.git.history import (
    DEFAULT_HISTORY_DAYS,
    DEFAULT_MAX_COMMITS,
    MAX_FILES_PER_COMMIT_FOR_COUPLING,
    HistoryStats,
    _parse_log,
    author_share,
    churn,
    co_change,
    collect_history,
    last_modified,
)
from mergesignal.git.repo import Repo
from tests.helpers.repo_builder import RepoBuilder

NOW = datetime.now(UTC)

# Captured `git log --no-merges --name-only -z --format=%x01%H%x00%ae%x00%aI`.
LOG_Z = (
    "\x01" + "a" * 40 + "\0z@example.com\x002024-05-01T10:00:00Z\0\na.py\0b.py\0"
    "\x01" + "b" * 40 + "\0author@example.com\x002024-04-30T10:00:00Z\0\na.py\0"
    "\x01"
    + "c" * 40
    + "\0author@example.com\x002024-04-29T10:00:00Z\0\ndir with space/ünïcodé.txt\0"
)


@pytest.fixture
def history_repo(tmp_path: Path) -> Repo:
    """Four commits inside the window plus one deliberately outside it.

    * ``a.py`` touched by three commits, one of them by a second author.
    * ``b.py`` touched by two commits, always alongside ``a.py``.
    * ``stale.py`` touched only 200 days ago — outside the default window.
    """
    builder = RepoBuilder(tmp_path / "hist", start_time=NOW - timedelta(days=5))
    builder.file("stale.py", "0\n").commit("stale", timestamp=NOW - timedelta(days=200))
    builder.file("a.py", "1\n").file("b.py", "1\n").commit("pair one")
    builder.file("a.py", "2\n").file("b.py", "2\n").commit("pair two")
    builder.file("a.py", "3\n").commit("solo", author=("Other", "other@example.com"))
    return Repo(builder.build())


# ------------------------------------------------------------- log parsing


def test_parse_log_empty_payload() -> None:
    assert _parse_log("") == []


def test_parse_log_splits_records_and_strips_header_newline() -> None:
    records = _parse_log(LOG_Z)

    assert [sha for sha, _, _, _ in records] == ["a" * 40, "b" * 40, "c" * 40]
    assert records[0][1] == "z@example.com"
    assert records[0][3] == ["a.py", "b.py"]
    assert records[1][3] == ["a.py"]


def test_parse_log_keeps_paths_with_spaces_and_unicode() -> None:
    records = _parse_log(LOG_Z)

    assert records[2][3] == ["dir with space/ünïcodé.txt"]


def test_parse_log_tolerates_a_commit_that_touched_nothing() -> None:
    payload = "\x01" + "d" * 40 + "\0me@example.com\x002024-05-01T10:00:00Z\0"
    ((_sha, email, _when, files),) = _parse_log(payload)

    assert email == "me@example.com"
    assert files == []


def test_parse_log_skips_truncated_records() -> None:
    assert _parse_log("\x01abc\0only-two-fields") == []


# -------------------------------------------------------------- aggregation


def test_collect_history_counts_churn_within_the_window(history_repo: Repo) -> None:
    stats = collect_history(history_repo, ["a.py", "b.py", "stale.py"])

    assert stats.churn == {"a.py": 3, "b.py": 2, "stale.py": 0}
    assert stats.commits_scanned == 3
    assert stats.days == DEFAULT_HISTORY_DAYS


def test_collect_history_widening_the_window_reaches_the_stale_commit(history_repo: Repo) -> None:
    stats = collect_history(history_repo, ["stale.py"], days=365)

    assert stats.churn["stale.py"] == 1
    assert stats.commits_scanned == 4


def test_collect_history_requested_paths_always_present(history_repo: Repo) -> None:
    stats = collect_history(history_repo, ["never/touched.py"])

    assert stats.churn == {"never/touched.py": 0}
    assert stats.authors == {"never/touched.py": {}}
    assert stats.co_change == {"never/touched.py": {}}


def test_collect_history_without_paths_reports_everything_seen(history_repo: Repo) -> None:
    stats = collect_history(history_repo, None)

    assert stats.churn == {"a.py": 3, "b.py": 2}


def test_collect_history_marks_truncation_at_the_commit_cap(history_repo: Repo) -> None:
    stats = collect_history(history_repo, None, max_commits=2)

    assert stats.commits_scanned == 2
    assert stats.truncated is True


def test_collect_history_untruncated_below_the_cap(history_repo: Repo) -> None:
    assert collect_history(history_repo, None).truncated is False


def test_collect_history_on_unborn_branch_is_empty_not_an_error(tmp_path: Path) -> None:
    repo = Repo(RepoBuilder(tmp_path / "unborn").build())

    stats = collect_history(repo, ["a.py"])

    assert stats == HistoryStats(churn={"a.py": 0}, co_change={"a.py": {}}, authors={"a.py": {}})


def test_collect_history_on_unknown_ref_is_empty_not_an_error(history_repo: Repo) -> None:
    assert collect_history(history_repo, None, ref="no-such-branch").commits_scanned == 0


def test_churn_returns_zero_for_every_requested_path(history_repo: Repo) -> None:
    assert churn(history_repo, ["a.py", "ghost.py"]) == {"a.py": 3, "ghost.py": 0}


# ---------------------------------------------------------------- co-change


def test_co_change_pairs_files_committed_together(history_repo: Repo) -> None:
    assert co_change(history_repo, ["a.py", "b.py"]) == {"a.py": {"b.py": 2}, "b.py": {"a.py": 2}}


def test_co_change_min_support_filters_weak_coupling(history_repo: Repo) -> None:
    assert co_change(history_repo, ["a.py", "b.py"], min_support=3) == {}


def test_co_change_excludes_self_pairs(history_repo: Repo) -> None:
    for path, coupled in co_change(history_repo, ["a.py", "b.py"]).items():
        assert path not in coupled


def test_co_change_ignores_mass_commits(tmp_path: Path) -> None:
    """A sweeping commit must not couple every file to every other file."""
    builder = RepoBuilder(tmp_path / "sweep", start_time=NOW - timedelta(days=2))
    for index in range(MAX_FILES_PER_COMMIT_FOR_COUPLING + 5):
        builder.file(f"mod{index}.py", "x\n")
    builder.commit("the great reformatting")
    repo = Repo(builder.build())

    assert co_change(repo, ["mod0.py"], min_support=1) == {}
    # Churn still counts it: the file really was touched.
    assert churn(repo, ["mod0.py"]) == {"mod0.py": 1}


def test_co_change_ignores_merge_commits(tmp_path: Path) -> None:
    """A merge touching both files must not manufacture coupling."""
    builder = RepoBuilder(tmp_path / "merged", start_time=NOW - timedelta(days=3))
    # Seeded separately so the only commit touching both is the merge itself.
    builder.file("left.py", "1\n").commit("seed left")
    builder.file("right.py", "1\n").commit("seed right")
    builder.branch("feature").file("left.py", "2\n").commit("left edit")
    builder.checkout("main").file("right.py", "2\n").commit("right edit")
    builder.merge("feature")
    repo = Repo(builder.build())

    assert co_change(repo, ["left.py", "right.py"], min_support=1) == {}


# ------------------------------------------------------------ author share


def test_author_share_normalises_to_fractions(history_repo: Repo) -> None:
    shares = author_share(history_repo, ["a.py"])

    assert shares["a.py"] == pytest.approx(
        {"author@example.com": 2 / 3, "other@example.com": 1 / 3}
    )
    assert sum(shares["a.py"].values()) == pytest.approx(1.0)


def test_author_share_bus_factor_one(history_repo: Repo) -> None:
    assert author_share(history_repo, ["b.py"]) == {"b.py": {"author@example.com": 1.0}}


def test_author_share_of_untouched_path_is_empty(history_repo: Repo) -> None:
    assert author_share(history_repo, ["ghost.py"]) == {"ghost.py": {}}


# ----------------------------------------------------------- last modified


def test_last_modified_returns_iso_timestamps(history_repo: Repo) -> None:
    stamps = last_modified(history_repo, ["a.py", "stale.py"])

    assert stamps["a.py"] is not None
    # Not bounded by the history window: the stale file still reports a date.
    assert stamps["stale.py"] is not None
    assert stamps["a.py"] > stamps["stale.py"]  # type: ignore[operator]


def test_last_modified_of_unknown_path_is_none(history_repo: Repo) -> None:
    assert last_modified(history_repo, ["ghost.py"]) == {"ghost.py": None}


def test_last_modified_on_unborn_branch_is_all_none(tmp_path: Path) -> None:
    repo = Repo(RepoBuilder(tmp_path / "unborn2").build())

    assert last_modified(repo, ["a.py", "b.py"]) == {"a.py": None, "b.py": None}


# -------------------------------------------------------------- boundedness


def test_defaults_are_documented_bounds() -> None:
    """The risk signal reads these; changing one is a behavioural change."""
    assert (DEFAULT_HISTORY_DAYS, DEFAULT_MAX_COMMITS) == (90, 2000)


def test_history_stats_defaults_are_empty_not_none() -> None:
    stats = HistoryStats()

    assert (stats.churn, stats.co_change, stats.authors) == ({}, {}, {})
    assert stats.commits_scanned == 0
    assert stats.truncated is False
