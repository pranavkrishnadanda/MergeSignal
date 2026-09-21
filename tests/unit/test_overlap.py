"""Unit tests for S3 — cross-branch overlap (:mod:`mergesignal.signals.overlap`)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from mergesignal.models import (
    AnalysisContext,
    BranchDiff,
    Diff,
    DiffFile,
    Hunk,
    LineRange,
    Symbol,
    SymbolChange,
)
from mergesignal.signals import overlap


def hunk(path: str, base: tuple[int, int], head: tuple[int, int] | None = None) -> Hunk:
    """A hunk whose base-side range is what overlap math compares."""
    head = head or base
    return Hunk(
        file_path=path,
        base_range=LineRange(start=base[0], end=base[1]),
        head_range=LineRange(start=head[0], end=head[1]),
        added_lines=["+"] * max(head[1] - head[0], 0),
        removed_lines=["-"] * max(base[1] - base[0], 0),
    )


def diff(*files: tuple[str, list[tuple[int, int]]], base: str = "main", head: str = "feature", old_paths: dict[str, str] | None = None) -> Diff:
    """Build a :class:`~mergesignal.models.Diff` from ``(path, [(start, end), ...])``."""
    old_paths = old_paths or {}
    return Diff(
        base=base,
        head=head,
        files=[
            DiffFile(
                path=path,
                old_path=old_paths.get(path),
                language="python",
                hunks=[hunk(path, span) for span in spans],
                additions=sum(end - start for start, end in spans),
            )
            for path, spans in files
        ],
    )


def branch(name: str, changed: Diff, *, symbols: list[str] | None = None, **kwargs: object) -> BranchDiff:
    """Build a :class:`~mergesignal.models.BranchDiff` for ``ctx.others``."""
    return BranchDiff(
        name=name,
        head=kwargs.pop("head", name),  # type: ignore[arg-type]
        base="main",
        diff=changed,
        symbols=[Symbol(name=n, kind="function", file="lib.py", line=1) for n in (symbols or [])],
        **kwargs,  # type: ignore[arg-type]
    )


def change(name: str, *, file: str = "lib.py") -> SymbolChange:
    """A candidate-side symbol change, the sharpest overlap key we have."""
    return SymbolChange(symbol=Symbol(name=name, kind="function", file=file, line=1), kind="modified")


# ------------------------------------------------------------------ skipping


def test_no_others_is_skipped(make_context: Callable[..., AnalysisContext]) -> None:
    signal = overlap.analyze(make_context(head_diff=diff(("a.py", [(1, 5)]))))

    assert signal.status == "skipped"
    assert "no other branches" in signal.summary
    assert signal.metadata["others"] == 0


def test_missing_candidate_diff_is_skipped(make_context: Callable[..., AnalysisContext]) -> None:
    signal = overlap.analyze(make_context(others=[branch("other", diff(("a.py", [(1, 5)])))]))

    assert signal.status == "skipped"
    assert "no diff available" in signal.summary


def test_empty_candidate_diff_is_skipped(make_context: Callable[..., AnalysisContext]) -> None:
    signal = overlap.analyze(
        make_context(head_diff=Diff(base="main", head="feature", files=[]), others=[branch("other", diff(("a.py", [(1, 5)])))])
    )

    assert signal.status == "skipped"
    assert "empty diff" in signal.summary


def test_candidate_itself_is_filtered_out(make_context: Callable[..., AnalysisContext]) -> None:
    ctx = make_context(head="feature", head_diff=diff(("a.py", [(1, 5)])), others=[branch("feature", diff(("a.py", [(1, 5)])))])

    signal = overlap.analyze(ctx)

    assert signal.status == "skipped"
    assert signal.metadata["compared"] == 0


def test_never_raises(monkeypatch: pytest.MonkeyPatch, make_context: Callable[..., AnalysisContext]) -> None:
    monkeypatch.setattr(overlap, "_analyze", lambda _ctx: (_ for _ in ()).throw(RuntimeError("boom")))

    signal = overlap.analyze(make_context())

    assert signal.status == "error"
    assert "RuntimeError: boom" in signal.summary


# --------------------------------------------------------------- granularity


def test_disjoint_branches_produce_no_findings(make_context: Callable[..., AnalysisContext]) -> None:
    ctx = make_context(head_diff=diff(("a.py", [(1, 5)])), others=[branch("other", diff(("z.py", [(1, 5)])))])

    signal = overlap.analyze(ctx)

    assert signal.status == "ok"
    assert signal.metadata["compared"] == 1
    assert signal.metadata["collisions"] == 0


def test_same_file_far_apart_is_file_granularity(make_context: Callable[..., AnalysisContext]) -> None:
    ctx = make_context(head_diff=diff(("a.py", [(1, 5)])), others=[branch("other", diff(("a.py", [(500, 510)])))])

    (finding,) = overlap.analyze(ctx).findings

    assert finding.evidence["granularity"] == "file"
    assert finding.severity == "medium"
    assert finding.file == "a.py"
    assert finding.evidence["files"] == ["a.py"]


def test_intersecting_hunks_are_high(make_context: Callable[..., AnalysisContext]) -> None:
    ctx = make_context(head_diff=diff(("a.py", [(10, 20)])), others=[branch("other", diff(("a.py", [(15, 25)])))])

    (finding,) = overlap.analyze(ctx).findings

    assert finding.evidence["granularity"] == "hunk"
    assert finding.severity == "high"
    assert finding.evidence["direct_hunk_count"] == 1
    assert finding.evidence["hunks"][0] == {"file": "a.py", "candidate": [10, 20], "other": [15, 25], "direct": True}


def test_adjacent_hunks_are_medium(make_context: Callable[..., AnalysisContext]) -> None:
    ctx = make_context(head_diff=diff(("a.py", [(10, 12)])), others=[branch("other", diff(("a.py", [(16, 18)])))])

    (finding,) = overlap.analyze(ctx).findings

    assert finding.evidence["granularity"] == "hunk"
    assert finding.severity == "medium"
    assert finding.evidence["direct_hunk_count"] == 0
    assert finding.evidence["hunk_count"] == 1


def test_shared_symbol_beats_hunk_and_file(make_context: Callable[..., AnalysisContext]) -> None:
    ctx = make_context(
        head_diff=diff(("lib.py", [(1, 5)])),
        head_changes=[change("shared"), change("only_mine")],
        others=[branch("other", diff(("lib.py", [(1, 5)])), symbols=["shared", "theirs"])],
    )

    (finding,) = overlap.analyze(ctx).findings

    assert finding.evidence["granularity"] == "symbol"
    assert finding.severity == "high"
    assert finding.confidence == "high"
    assert finding.evidence["symbols"] == ["shared"]


def test_symbol_match_without_file_overlap_is_not_reported(make_context: Callable[..., AnalysisContext]) -> None:
    """A coincidental name match in an unrelated file must not manufacture a collision."""
    ctx = make_context(
        head_diff=diff(("mine.py", [(1, 5)])),
        head_changes=[change("shared")],
        others=[branch("other", diff(("theirs.py", [(1, 5)])), symbols=["shared"])],
    )

    assert overlap.analyze(ctx).findings == []


# -------------------------------------------------------------- one per branch


def test_one_finding_per_branch_not_per_file(make_context: Callable[..., AnalysisContext]) -> None:
    shared = [(f"m{i}.py", [(1, 5)]) for i in range(8)]
    ctx = make_context(head_diff=diff(*shared), others=[branch("other", diff(*shared))])

    signal = overlap.analyze(ctx)

    assert len(signal.findings) == 1
    assert signal.findings[0].evidence["file_count"] == 8
    assert signal.findings[0].file is None


def test_findings_are_ranked_by_granularity(make_context: Callable[..., AnalysisContext]) -> None:
    ctx = make_context(
        head_diff=diff(("lib.py", [(10, 20)]), ("other.py", [(1, 3)])),
        head_changes=[change("shared")],
        others=[
            branch("file-level", diff(("other.py", [(900, 910)]))),
            branch("symbol-level", diff(("lib.py", [(10, 20)])), symbols=["shared"]),
            branch("hunk-level", diff(("lib.py", [(12, 18)]))),
        ],
    )

    signal = overlap.analyze(ctx)

    assert [f.evidence["branch"] for f in signal.findings] == ["symbol-level", "hunk-level", "file-level"]
    assert "sharpest collision at symbol level" in signal.summary
    assert signal.metadata["collisions"] == 3


def test_pr_metadata_is_carried_into_evidence(make_context: Callable[..., AnalysisContext]) -> None:
    other = branch(
        "PR #7",
        diff(("a.py", [(1, 5)])),
        head="refs/pull/7/head",
        pr_number=7,
        url="https://example.invalid/pull/7",
        author="octocat",
    )
    ctx = make_context(head_diff=diff(("a.py", [(1, 5)])), others=[other])

    (finding,) = overlap.analyze(ctx).findings

    assert finding.evidence["pr_number"] == 7
    assert finding.evidence["url"] == "https://example.invalid/pull/7"
    assert finding.evidence["author"] == "octocat"


# ------------------------------------------------------------------- helpers


def test_file_overlap_is_rename_aware() -> None:
    candidate = diff(("new.py", [(1, 5)]), old_paths={"new.py": "old.py"})
    other = diff(("old.py", [(1, 5)]))

    assert overlap.file_overlap(candidate, other) == {"old.py"}


def test_hunk_overlap_uses_base_side_ranges() -> None:
    candidate = diff(("a.py", [(10, 20)]))
    other = diff(("a.py", [(18, 30), (400, 410)]))

    assert overlap.hunk_overlap(candidate, other) == [("a.py", (10, 20), (18, 30))]


def test_hunk_overlap_adjacency_is_configurable() -> None:
    candidate = diff(("a.py", [(10, 12)]))
    other = diff(("a.py", [(30, 32)]))

    assert overlap.hunk_overlap(candidate, other, adjacency=0) == []
    assert overlap.hunk_overlap(candidate, other, adjacency=25) == [("a.py", (10, 12), (30, 32))]


def test_pure_insertions_at_the_same_offset_are_adjacent_not_intersecting() -> None:
    candidate = diff(("a.py", [(7, 7)]))
    other = diff(("a.py", [(7, 7)]))

    pairs = overlap.hunk_overlap(candidate, other)

    assert pairs == [("a.py", (7, 7), (7, 7))]
    assert overlap._ranges_intersect((7, 7), (7, 7)) is False


def test_symbol_overlap_intersects_names() -> None:
    assert overlap.symbol_overlap(["a", "b", ""], ["b", "c"]) == {"b"}
    assert overlap.symbol_overlap([], ["b"]) == set()


def test_compare_returns_none_when_disjoint() -> None:
    assert overlap.compare(diff(("a.py", [(1, 5)])), branch("other", diff(("b.py", [(1, 5)])))) is None


def test_rank_is_stable_for_equal_granularity() -> None:
    findings = [
        overlap.compare(diff(("a.py", [(1, 5)])), branch(name, diff(("a.py", [(900, 905)]))))
        for name in ("zulu", "alpha")
    ]

    assert [f.evidence["branch"] for f in overlap.rank([f for f in findings if f])] == ["alpha", "zulu"]
