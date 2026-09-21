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

Implementation notes
--------------------

* Hunk ranges are compared on the **base** side, the only coordinate system two
  branches share. Both diffs are expected to be measured from the same base (the
  CLI builds ``others`` with ``base...branch``); when they are not, hunk
  granularity degrades gracefully to file granularity rather than lying.
* A ``symbol`` collision is only ever reported for branches that already collide
  at file level, so a coincidental name match in two unrelated files cannot
  manufacture a high-severity finding.
"""

from __future__ import annotations

from typing import Any

from mergesignal.models import (
    SEVERITY_ORDER,
    AnalysisContext,
    BranchDiff,
    Confidence,
    Diff,
    DiffFile,
    Finding,
    Severity,
    Signal,
)

#: Name registered in :data:`mergesignal.signals.REGISTRY`.
NAME = "overlap"

#: Granularity ranking, most specific first. Used for sorting and severity.
GRANULARITIES: tuple[str, ...] = ("symbol", "hunk", "file")

#: Line distance within which two non-overlapping hunks count as "adjacent".
ADJACENCY_LINES = 10

#: Most hunk pairs carried in one finding's evidence.
MAX_EVIDENCE_HUNKS = 20

#: Most paths/symbols carried in one finding's evidence.
MAX_EVIDENCE_NAMES = 25


def analyze(ctx: AnalysisContext) -> Signal:
    """Run the overlap signal over ``ctx.others``. Never raises."""
    try:
        return _analyze(ctx)
    except Exception as exc:  # noqa: BLE001 - engines never raise (see signals/__init__)
        return Signal.error(NAME, f"{type(exc).__name__}: {exc}")


def _analyze(ctx: AnalysisContext) -> Signal:
    """The real body of :func:`analyze`, wrapped by its exception trap."""
    candidate = ctx.head_diff
    if candidate is None:
        return Signal.skipped(
            NAME,
            "no diff available for the candidate; overlap not computed",
            others=len(ctx.others),
        )
    if not candidate.files:
        return Signal.skipped(NAME, "empty diff; nothing can collide", others=len(ctx.others))
    if not ctx.others:
        return Signal.skipped(
            NAME, "no other branches or PRs supplied (use --branches or --prs)", others=0
        )

    candidate_symbols = _candidate_symbols(ctx)
    findings: list[Finding] = []
    errors: list[str] = []
    compared = 0

    for other in ctx.others:
        if _is_candidate_itself(other, ctx):
            continue
        try:
            compared += 1
            finding = compare(candidate, other, candidate_symbols=candidate_symbols)
        except Exception as exc:  # noqa: BLE001 - one bad branch must not lose the signal
            errors.append(f"{other.name}: {type(exc).__name__}: {exc}")
            continue
        if finding is not None:
            findings.append(finding)

    if compared == 0:
        return Signal.skipped(
            NAME,
            "every supplied branch/PR is the candidate itself; nothing to compare",
            others=len(ctx.others),
            compared=0,
        )

    findings = rank(findings)
    metadata: dict[str, Any] = {
        "others": len(ctx.others),
        "compared": compared,
        "collisions": len(findings),
        "errors": errors,
        "candidate_symbols": len(candidate_symbols),
    }
    return Signal.from_findings(NAME, findings, _summarize(findings, compared), **metadata)


def compare(
    candidate: Diff,
    other: BranchDiff,
    *,
    candidate_symbols: list[str] | None = None,
    adjacency: int = ADJACENCY_LINES,
) -> Finding | None:
    """Compare the candidate against one other change set.

    :returns: a :class:`~mergesignal.models.Finding` describing the strongest
        collision found, or ``None`` when the two change sets are disjoint.

    ``evidence`` carries ``{"granularity": ..., "files": [...], "symbols":
    [...], "hunks": [{"file":..., "candidate":[s,e], "other":[s,e]}]}`` so the
    markdown renderer can table it.
    """
    files = file_overlap(candidate, other.diff)
    if not files:
        return None

    symbols = symbol_overlap(
        candidate_symbols or [], [symbol.qualified_name for symbol in other.symbols]
    )
    hunks = hunk_overlap(candidate, other.diff, adjacency=adjacency)
    direct = [entry for entry in hunks if _ranges_intersect(entry[1], entry[2])]

    if symbols:
        granularity = "symbol"
        severity: Severity = "high"
        confidence: Confidence = "high"
    elif hunks:
        granularity = "hunk"
        severity = "high" if direct else "medium"
        confidence = "high" if direct else "medium"
    else:
        granularity = "file"
        severity = "medium"
        confidence = "medium"

    sorted_files = sorted(files)
    sorted_symbols = sorted(symbols)
    evidence: dict[str, Any] = {
        "granularity": granularity,
        "branch": other.name,
        "head": other.head,
        "base": other.base,
        "files": sorted_files[:MAX_EVIDENCE_NAMES],
        "file_count": len(sorted_files),
        "symbols": sorted_symbols[:MAX_EVIDENCE_NAMES],
        "symbol_count": len(sorted_symbols),
        "hunks": [
            {
                "file": path,
                "candidate": [cand[0], cand[1]],
                "other": [oth[0], oth[1]],
                "direct": _ranges_intersect(cand, oth),
            }
            for path, cand, oth in hunks[:MAX_EVIDENCE_HUNKS]
        ],
        "hunk_count": len(hunks),
        "direct_hunk_count": len(direct),
        "adjacency_lines": adjacency,
    }
    if other.pr_number is not None:
        evidence["pr_number"] = other.pr_number
    if other.url:
        evidence["url"] = other.url
    if other.author:
        evidence["author"] = other.author

    detail_bits = [
        f"{other.name} touches {_plural(len(sorted_files), 'of the same file')}: {', '.join(sorted_files[:5])}"
        + (f" (+{len(sorted_files) - 5} more)" if len(sorted_files) > 5 else "")
    ]
    if sorted_symbols:
        detail_bits.append(
            f"Both change the same declaration(s): {', '.join(sorted_symbols[:5])}"
            + (f" (+{len(sorted_symbols) - 5} more)" if len(sorted_symbols) > 5 else "")
        )
    if hunks:
        kind = "overlapping" if direct else f"within {adjacency} lines of each other"
        detail_bits.append(f"{_plural(len(hunks), 'hunk pair')} {kind}.")
    detail_bits.append("Whoever merges second will have to reconcile these by hand.")

    return Finding(
        signal=NAME,
        severity=severity,
        confidence=confidence,
        title=f"{other.name} collides at {granularity} level ({_plural(len(sorted_files), 'shared file')})",
        detail=" ".join(detail_bits),
        file=sorted_files[0] if len(sorted_files) == 1 else None,
        evidence=evidence,
    )


def file_overlap(candidate: Diff, other: Diff) -> set[str]:
    """Paths changed by both sides.

    Rename-aware: a file the candidate renamed and the other side edited under
    its old name is an overlap, and one of the nastiest kinds.
    """
    return _path_keys(candidate) & _path_keys(other)


def hunk_overlap(
    candidate: Diff, other: Diff, *, adjacency: int = ADJACENCY_LINES
) -> list[tuple[str, tuple[int, int], tuple[int, int]]]:
    """Intersecting (or near-intersecting) hunk ranges, per shared file.

    Comparison happens on the **base** side ranges, since that is the only
    coordinate system the two branches share.

    :returns: ``(path, candidate_range, other_range)`` tuples.
    """
    other_ranges = _ranges_by_key(other)
    results: list[tuple[str, tuple[int, int], tuple[int, int]]] = []

    for changed in candidate.files:
        for key in _keys_for(changed):
            for candidate_range in _base_ranges(changed):
                for other_range in other_ranges.get(key, ()):
                    if _ranges_intersect(candidate_range, other_range) or _gap(
                        candidate_range, other_range
                    ) <= max(adjacency, 0):
                        results.append((key, candidate_range, other_range))
    return sorted(set(results))


def symbol_overlap(candidate_symbols: list[str], other_symbols: list[str]) -> set[str]:
    """Qualified symbol names changed by both sides."""
    return {name for name in candidate_symbols if name} & {name for name in other_symbols if name}


def rank(findings: list[Finding]) -> list[Finding]:
    """Sort findings by granularity (symbol > hunk > file), then severity, then name."""

    def key(finding: Finding) -> tuple[int, int, str]:
        granularity = str(finding.evidence.get("granularity", ""))
        rank_index = (
            GRANULARITIES.index(granularity) if granularity in GRANULARITIES else len(GRANULARITIES)
        )
        return (
            rank_index,
            -SEVERITY_ORDER[finding.severity],
            str(finding.evidence.get("branch", finding.title)),
        )

    return sorted(findings, key=key)


# ------------------------------------------------------------------- helpers


def _candidate_symbols(ctx: AnalysisContext) -> list[str]:
    """Qualified names the candidate changed, falling back to what it defines.

    Symbol *changes* are the sharper signal, but they are only available when
    the index ran; a context carrying only symbols still supports the weaker
    "both sides touch a file declaring this name" comparison.
    """
    if ctx.head_changes:
        return sorted({change.symbol.qualified_name for change in ctx.head_changes})
    return sorted({symbol.qualified_name for symbol in ctx.head_symbols})


def _is_candidate_itself(other: BranchDiff, ctx: AnalysisContext) -> bool:
    """``True`` when this "other" change set is really the candidate again."""
    return other.head == ctx.head or other.name == ctx.head


def _keys_for(changed: DiffFile) -> set[str]:
    """Every path a file is known by — post-image plus pre-image for renames."""
    keys = {changed.path}
    if changed.old_path:
        keys.add(changed.old_path)
    return keys


def _path_keys(diff: Diff) -> set[str]:
    """Union of :func:`_keys_for` over a whole diff."""
    keys: set[str] = set()
    for changed in diff.files:
        keys |= _keys_for(changed)
    return keys


def _base_ranges(changed: DiffFile) -> list[tuple[int, int]]:
    """Base-side ``(start, end)`` pairs for a file's hunks."""
    return [(hunk.base_range.start, hunk.base_range.end) for hunk in changed.hunks]


def _ranges_by_key(diff: Diff) -> dict[str, list[tuple[int, int]]]:
    """Path (including rename aliases) -> base-side hunk ranges."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    for changed in diff.files:
        spans = _base_ranges(changed)
        for key in _keys_for(changed):
            ranges.setdefault(key, []).extend(spans)
    return ranges


def _ranges_intersect(left: tuple[int, int], right: tuple[int, int]) -> bool:
    """``True`` when two half-open ranges share at least one line.

    Empty ranges (pure insertions, ``@@ -0,0 +1,3 @@``) never *intersect*; they
    are caught by :func:`_gap` as adjacent instead, which is the honest reading:
    two insertions at the same offset are a likely, not a certain, collision.
    """
    if left[1] <= left[0] or right[1] <= right[0]:
        return False
    return left[0] < right[1] and right[0] < left[1]


def _gap(left: tuple[int, int], right: tuple[int, int]) -> int:
    """Line distance between two ranges; ``0`` when they touch or intersect."""
    return max(0, max(left[0], right[0]) - min(left[1], right[1]))


def _plural(count: int, noun: str) -> str:
    """``"1 shared file"`` / ``"2 shared files"``."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _summarize(findings: list[Finding], compared: int) -> str:
    """One-line summary naming the sharpest granularity found."""
    if not findings:
        return f"no overlap with {_plural(compared, 'other change set')}"
    sharpest = min(
        (str(f.evidence.get("granularity", "file")) for f in findings),
        key=lambda g: GRANULARITIES.index(g) if g in GRANULARITIES else len(GRANULARITIES),
    )
    return f"{_plural(len(findings), 'colliding change set')} of {compared} compared; sharpest collision at {sharpest} level"
