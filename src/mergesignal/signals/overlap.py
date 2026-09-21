"""S3 — cross-PR / cross-branch overlap. **Owned by Agent E.**

Answers "who else is standing on this code right now?" (FR-5). Inputs are the
candidate change (``ctx.head_diff``) and ``ctx.others`` — a list of
:class:`~mergesignal.models.BranchDiff` built either from local branches
(``--branches``) or from open GitHub PRs (``--prs``).

Collisions are ranked by granularity, most specific first:

``symbol``
    Both change the *same declaration*. Severity ``high``.
``hunk``
    Their changed line ranges intersect within a file. Severity ``high`` when
    the ranges overlap directly, ``medium`` when they are merely adjacent.
``file``
    They touch the same file in disjoint places. Severity ``medium``.

One :class:`~mergesignal.models.Finding` per colliding branch (not per file), so
a PR touching 40 shared files produces one actionable item with the detail in
``evidence`` rather than 40 rows of noise.

Edge cases: ``ctx.others`` empty -> ``skipped`` with reason; a branch whose diff
failed to compute -> noted in ``metadata["errors"]``, not fatal; the candidate
branch appearing in ``others`` -> filtered out by ref equality.
"""

from __future__ import annotations

from mergesignal.models import AnalysisContext, BranchDiff, Diff, Finding, Signal

#: Name registered in :data:`mergesignal.signals.REGISTRY`.
NAME = "overlap"

#: Granularity ranking, most specific first. Used for sorting and severity.
GRANULARITIES: tuple[str, ...] = ("symbol", "hunk", "file")

#: Line distance within which two non-overlapping hunks count as "adjacent".
ADJACENCY_LINES = 10


def analyze(ctx: AnalysisContext) -> Signal:
    """Run the overlap signal over ``ctx.others``. Never raises."""
    raise NotImplementedError


def compare(candidate: Diff, other: BranchDiff, *, candidate_symbols: list[str] | None = None, adjacency: int = ADJACENCY_LINES) -> Finding | None:
    """Compare the candidate against one other change set.

    :returns: a :class:`~mergesignal.models.Finding` describing the strongest
        collision found, or ``None`` when the two change sets are disjoint.

    ``evidence`` carries ``{"granularity": ..., "files": [...], "symbols":
    [...], "hunks": [{"file":..., "candidate":[s,e], "other":[s,e]}]}`` so the
    markdown renderer can table it.
    """
    raise NotImplementedError


def file_overlap(candidate: Diff, other: Diff) -> set[str]:
    """Paths changed by both sides.

    Rename-aware: a file the candidate renamed and the other side edited under
    its old name is an overlap, and one of the nastiest kinds.
    """
    raise NotImplementedError


def hunk_overlap(candidate: Diff, other: Diff, *, adjacency: int = ADJACENCY_LINES) -> list[tuple[str, tuple[int, int], tuple[int, int]]]:
    """Intersecting (or near-intersecting) hunk ranges, per shared file.

    Comparison happens on the **base** side ranges, since that is the only
    coordinate system the two branches share.

    :returns: ``(path, candidate_range, other_range)`` tuples.
    """
    raise NotImplementedError


def symbol_overlap(candidate_symbols: list[str], other_symbols: list[str]) -> set[str]:
    """Qualified symbol names changed by both sides."""
    raise NotImplementedError


def rank(findings: list[Finding]) -> list[Finding]:
    """Sort findings by granularity (symbol > hunk > file), then severity, then name."""
    raise NotImplementedError
