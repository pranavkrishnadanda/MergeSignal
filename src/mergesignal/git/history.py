"""Repository history statistics feeding the risk signal. **Owned by Agent B.**

All three statistics are *bounded*: they never walk the entire history of a
large repository. Every function takes ``days`` and ``max_commits`` limits
(defaulting to :class:`~mergesignal.config.AnalysisConfig` values) and the
returned :class:`HistoryStats` records which limit actually bit, so the risk
signal can lower its confidence when the sample was truncated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mergesignal.git.repo import Repo

#: Default lookback window in days for churn and co-change statistics.
DEFAULT_HISTORY_DAYS = 90

#: Default hard cap on commits traversed by any single ``git log`` invocation.
DEFAULT_MAX_COMMITS = 2000


@dataclass(frozen=True)
class HistoryStats:
    """Bounded history summary for a set of paths.

    ``truncated`` is ``True`` when the traversal hit ``max_commits`` before
    exhausting the window, meaning the numbers are a lower bound.
    """

    churn: dict[str, int] = field(default_factory=dict)
    """Path -> number of commits touching it within the window."""

    co_change: dict[str, dict[str, int]] = field(default_factory=dict)
    """Path -> (other path -> number of commits that touched both)."""

    authors: dict[str, dict[str, int]] = field(default_factory=dict)
    """Path -> (author email -> number of commits by that author)."""

    commits_scanned: int = 0
    """How many commits were actually examined."""

    truncated: bool = False
    """Whether ``max_commits`` cut the traversal short."""

    days: int = DEFAULT_HISTORY_DAYS
    """The lookback window actually used."""


def collect_history(repo: Repo, paths: list[str] | None = None, *, ref: str = "HEAD", days: int = DEFAULT_HISTORY_DAYS, max_commits: int = DEFAULT_MAX_COMMITS) -> HistoryStats:
    """Walk bounded history once and derive churn, co-change and author stats.

    A single ``git log --name-only -z --since=<days>.days.ago -n <max_commits>``
    traversal feeds all three statistics, because three separate walks would
    blow NFR-1 on large repositories.

    :param paths: restrict co-change/churn keys to these paths (the diff's
        touched files). ``None`` means "every path seen in the window", which is
        only appropriate for small repositories.
    :param ref: tip to walk back from; an unborn branch yields empty stats
        rather than raising.
    :returns: :class:`HistoryStats`, possibly empty. Never raises for a
        repository with no commits in the window.

    Merge commits are skipped (``--no-merges``) so that a merge touching 300
    files does not manufacture spurious coupling between all of them.
    """
    raise NotImplementedError


def churn(repo: Repo, paths: list[str], *, ref: str = "HEAD", days: int = DEFAULT_HISTORY_DAYS, max_commits: int = DEFAULT_MAX_COMMITS) -> dict[str, int]:
    """Commits touching each path within the window.

    Paths with no commits in the window are present with value ``0`` so callers
    can rely on every requested key existing.
    """
    raise NotImplementedError


def co_change(repo: Repo, paths: list[str], *, ref: str = "HEAD", days: int = DEFAULT_HISTORY_DAYS, max_commits: int = DEFAULT_MAX_COMMITS, min_support: int = 2) -> dict[str, dict[str, int]]:
    """Files historically committed together with each of ``paths``.

    :param min_support: ignore pairs seen fewer than this many times — one
        coincidental co-commit is noise, not coupling.
    :returns: mapping of path -> {coupled path: count}, self-pairs excluded.

    Commits touching an implausibly large number of files (mass renames,
    reformatting sweeps) should be excluded by the implementation, since they
    couple everything to everything; document the threshold chosen.
    """
    raise NotImplementedError


def author_share(repo: Repo, paths: list[str], *, ref: str = "HEAD", days: int = DEFAULT_HISTORY_DAYS, max_commits: int = DEFAULT_MAX_COMMITS) -> dict[str, dict[str, float]]:
    """Fraction of commits per author for each path.

    :returns: path -> {author email: share in 0.0-1.0}. A path with a single
        dominant author ("bus factor 1") is a risk amplifier; a path with no
        commits in the window maps to an empty dict.
    """
    raise NotImplementedError


def last_modified(repo: Repo, paths: list[str], *, ref: str = "HEAD") -> dict[str, str | None]:
    """ISO-8601 timestamp of the most recent commit touching each path.

    ``None`` for paths with no commits (newly added in the working tree).
    """
    raise NotImplementedError
