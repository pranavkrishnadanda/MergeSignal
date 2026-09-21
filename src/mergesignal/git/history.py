"""Repository history statistics feeding the risk signal. **Owned by Agent B.**

All three statistics are *bounded*: they never walk the entire history of a
large repository. Every function takes ``days`` and ``max_commits`` limits
(defaulting to :class:`~mergesignal.config.AnalysisConfig` values) and the
returned :class:`HistoryStats` records which limit actually bit, so the risk
signal can lower its confidence when the sample was truncated.

Documented bounds and deliberate omissions
------------------------------------------

* **One traversal.** ``collect_history`` runs a single
  ``git log --no-merges --name-only -z`` and derives churn, co-change and author
  share from it. Three separate walks would cost three times as much on the
  repositories where NFR-1 actually bites.
* **``--since=<days>.days.ago``** bounds the window; **``-n <max_commits>``**
  bounds the work even when the window is dense. When the cap bites,
  :attr:`HistoryStats.truncated` is ``True`` and every number is a lower bound.
* **Merge commits are skipped.** A merge touching 300 files would otherwise
  manufacture coupling between all 44 850 pairs of them.
* **Mass commits are skipped for co-change only** — see
  :data:`MAX_FILES_PER_COMMIT_FOR_COUPLING`. They still count towards churn and
  author share, because "this file was touched" remains true.
* **No pathspec is passed to ``git log``.** Restricting the traversal to the
  changed paths would make ``--name-only`` list *only* those paths, destroying
  the co-change signal, which is precisely about the files you did *not* change.
  Filtering happens in Python instead.
* **No rename following.** ``--follow`` only works for a single path and would
  force one traversal per file; history before a rename is simply not counted.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from mergesignal.git.repo import GitError, Repo

#: Default lookback window in days for churn and co-change statistics.
DEFAULT_HISTORY_DAYS = 90

#: Default hard cap on commits traversed by any single ``git log`` invocation.
DEFAULT_MAX_COMMITS = 2000

#: Commits touching more than this many files are ignored when computing
#: co-change coupling. Mass renames, dependency bumps and reformatting sweeps
#: touch hundreds of unrelated files and would couple everything to everything;
#: 50 is comfortably above a large but genuine feature commit.
MAX_FILES_PER_COMMIT_FOR_COUPLING = 50

#: Record separator injected into ``git log --format``. ``\x01`` cannot appear in
#: a commit sha, an email or an ISO timestamp, and git will not emit it
#: unescaped from any of the placeholders we use.
_RECORD_SEP = "\x01"

#: ``git log`` pretty format: sha, author email, author date, NUL-separated.
_LOG_FORMAT = "%x01%H%x00%ae%x00%aI"


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

    Co-change counts here are *raw*; :func:`co_change` applies ``min_support``.
    """
    wanted = set(paths) if paths is not None else None
    churn_counts: Counter[str] = Counter()
    author_counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    coupling: defaultdict[str, Counter[str]] = defaultdict(Counter)
    commits_scanned = 0

    for _sha, email, _when, files in _walk(repo, ref=ref, days=days, max_commits=max_commits):
        commits_scanned += 1
        tracked = [f for f in files if wanted is None or f in wanted]
        for path in tracked:
            churn_counts[path] += 1
            author_counts[path][email] += 1
        if 1 < len(files) <= MAX_FILES_PER_COMMIT_FOR_COUPLING:
            for path in tracked:
                for other in files:
                    if other != path:
                        coupling[path][other] += 1

    churn_result = dict.fromkeys(paths or [], 0)
    churn_result.update(churn_counts)
    authors_result: dict[str, dict[str, int]] = {path: {} for path in (paths or [])}
    authors_result.update({path: dict(counts) for path, counts in author_counts.items()})
    co_result: dict[str, dict[str, int]] = {path: {} for path in (paths or [])}
    co_result.update({path: dict(counts) for path, counts in coupling.items()})

    return HistoryStats(
        churn=churn_result,
        co_change=co_result,
        authors=authors_result,
        commits_scanned=commits_scanned,
        truncated=commits_scanned >= max_commits,
        days=days,
    )


def churn(repo: Repo, paths: list[str], *, ref: str = "HEAD", days: int = DEFAULT_HISTORY_DAYS, max_commits: int = DEFAULT_MAX_COMMITS) -> dict[str, int]:
    """Commits touching each path within the window.

    Paths with no commits in the window are present with value ``0`` so callers
    can rely on every requested key existing.
    """
    return collect_history(repo, paths, ref=ref, days=days, max_commits=max_commits).churn


def co_change(repo: Repo, paths: list[str], *, ref: str = "HEAD", days: int = DEFAULT_HISTORY_DAYS, max_commits: int = DEFAULT_MAX_COMMITS, min_support: int = 2) -> dict[str, dict[str, int]]:
    """Files historically committed together with each of ``paths``.

    :param min_support: ignore pairs seen fewer than this many times — one
        coincidental co-commit is noise, not coupling.
    :returns: mapping of path -> {coupled path: count}, self-pairs excluded.

    Commits touching an implausibly large number of files (mass renames,
    reformatting sweeps) should be excluded by the implementation, since they
    couple everything to everything; document the threshold chosen. Ours is
    :data:`MAX_FILES_PER_COMMIT_FOR_COUPLING`.

    Paths whose coupling is entirely below ``min_support`` are dropped from the
    result rather than mapping to an empty dict, so ``if path in result`` is a
    meaningful question.
    """
    stats = collect_history(repo, paths, ref=ref, days=days, max_commits=max_commits)
    result: dict[str, dict[str, int]] = {}
    for path, counts in stats.co_change.items():
        supported = {other: count for other, count in counts.items() if count >= min_support}
        if supported:
            result[path] = supported
    return result


def author_share(repo: Repo, paths: list[str], *, ref: str = "HEAD", days: int = DEFAULT_HISTORY_DAYS, max_commits: int = DEFAULT_MAX_COMMITS) -> dict[str, dict[str, float]]:
    """Fraction of commits per author for each path.

    :returns: path -> {author email: share in 0.0-1.0}. A path with a single
        dominant author ("bus factor 1") is a risk amplifier; a path with no
        commits in the window maps to an empty dict.
    """
    stats = collect_history(repo, paths, ref=ref, days=days, max_commits=max_commits)
    shares: dict[str, dict[str, float]] = {}
    for path, counts in stats.authors.items():
        total = sum(counts.values())
        shares[path] = {email: count / total for email, count in counts.items()} if total else {}
    return shares


def last_modified(repo: Repo, paths: list[str], *, ref: str = "HEAD") -> dict[str, str | None]:
    """ISO-8601 timestamp of the most recent commit touching each path.

    ``None`` for paths with no commits (newly added in the working tree).

    Costs one ``git log -1`` per path — bounded by the size of the diff, not by
    the size of the repository, which is why it is not folded into the single
    traversal in :func:`collect_history` (that one is limited to ``days``).
    """
    result: dict[str, str | None] = {}
    if not repo.ref_exists(ref):
        return dict.fromkeys(paths)
    for path in paths:
        try:
            out = repo.run(["log", "-1", "--format=%cI", ref, "--", path], check=True)
        except GitError:
            result[path] = None
            continue
        result[path] = out.strip() or None
    return result


# --------------------------------------------------------------------- internals


def _walk(repo: Repo, *, ref: str, days: int, max_commits: int) -> list[tuple[str, str, str, list[str]]]:
    """Run the one bounded ``git log`` and return parsed commit records.

    :returns: ``(sha, author_email, author_date, files)`` per commit, newest
        first. An unborn branch, an unknown ref or an empty window all yield
        ``[]`` — history statistics are advisory, never fatal.
    """
    if not repo.ref_exists(ref):
        return []
    args = [
        "-c",
        "core.quotepath=false",
        "log",
        ref,
        "--no-merges",
        "--name-only",
        "-z",
        f"--format={_LOG_FORMAT}",
        f"-n{max(int(max_commits), 1)}",
        f"--since={max(int(days), 1)}.days.ago",
    ]
    result = repo.run_result(args)
    if not result.ok:
        return []
    return _parse_log(result.stdout)


def _parse_log(payload: str) -> list[tuple[str, str, str, list[str]]]:
    """Parse ``git log --name-only -z --format=%x01%H%x00%ae%x00%aI`` output.

    Each record looks like::

        \\x01<sha>\\0<email>\\0<date>\\0\\n<path>\\0<path>\\0

    The stray ``\\n`` is git's separator between the commit header and the file
    list; only the *first* path of each commit carries it. A commit that touched
    no files (an empty commit) simply has no path fields.
    """
    records: list[tuple[str, str, str, list[str]]] = []
    for chunk in payload.split(_RECORD_SEP):
        if not chunk:
            continue
        fields = chunk.split("\0")
        if len(fields) < 3:
            continue
        sha, email, when = fields[0], fields[1], fields[2]
        files: list[str] = []
        for raw in fields[3:]:
            path = raw[1:] if raw.startswith("\n") else raw
            if path:
                files.append(path)
        records.append((sha.strip(), email, when, files))
    return records
