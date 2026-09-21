"""Unit tests for S1 — conflict prediction (:mod:`mergesignal.signals.conflicts`).

The merge oracle itself belongs to Agent B and has its own tests; here the
simulation is stubbed so we can assert exactly how a :class:`MergeSimulation`
becomes a :class:`Signal`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from mergesignal.git.repo import GitError
from mergesignal.models import AnalysisContext, ConflictRegion, LineRange, MergeSimulation
from mergesignal.signals import conflicts


def region(
    file: str = "a.py",
    *,
    ours: tuple[int, int] = (10, 12),
    theirs: tuple[int, int] = (10, 13),
    ours_text: str | None = "ours line\n",
    theirs_text: str | None = "theirs line\n",
    base_text: str | None = None,
    is_binary: bool = False,
) -> ConflictRegion:
    """Build a :class:`~mergesignal.models.ConflictRegion` for the stubbed oracle."""
    return ConflictRegion(
        file=file,
        ours_range=LineRange(start=ours[0], end=ours[1]),
        theirs_range=LineRange(start=theirs[0], end=theirs[1]),
        ours_text=ours_text,
        theirs_text=theirs_text,
        base_text=base_text,
        is_binary=is_binary,
    )


def simulation(**overrides: Any) -> MergeSimulation:
    """Build a :class:`~mergesignal.models.MergeSimulation` with sane defaults."""
    kwargs: dict[str, Any] = {"base": "main", "head": "feature", "merge_base": "0" * 40, "clean": True}
    kwargs.update(overrides)
    return MergeSimulation(**kwargs)


@pytest.fixture
def stub_simulation(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Replace :func:`mergesignal.git.merge_sim.simulate_merge` with a stub."""

    def _stub(result: MergeSimulation | Exception) -> None:
        def fake(*_args: object, **_kwargs: object) -> MergeSimulation:
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr("mergesignal.git.merge_sim.simulate_merge", fake)

    return _stub


# ------------------------------------------------------------------ statuses


def test_clean_merge_is_ok(make_context: Callable[..., AnalysisContext], stub_simulation: Callable[..., None]) -> None:
    stub_simulation(simulation(clean=True, strategy="merge-tree"))

    signal = conflicts.analyze(make_context())

    assert signal.name == "conflicts"
    assert signal.status == "ok"
    assert signal.findings == []
    assert "cleanly" in signal.summary
    assert signal.metadata == {
        "conflicted_files": 0,
        "regions": 0,
        "strategy": "merge-tree",
        "clean": True,
        "up_to_date": False,
    }


def test_up_to_date_is_ok_not_error(make_context: Callable[..., AnalysisContext], stub_simulation: Callable[..., None]) -> None:
    stub_simulation(simulation(clean=True, up_to_date=True))

    signal = conflicts.analyze(make_context())

    assert signal.status == "ok"
    assert "already merged" in signal.summary
    assert signal.metadata["up_to_date"] is True


def test_git_failure_becomes_error_signal(make_context: Callable[..., AnalysisContext], stub_simulation: Callable[..., None]) -> None:
    stub_simulation(GitError("unknown revision 'nope'"))

    signal = conflicts.analyze(make_context())

    assert signal.status == "error"
    assert "GitError" in signal.summary
    assert signal.findings == []


def test_unexpected_exception_becomes_error_signal(make_context: Callable[..., AnalysisContext], stub_simulation: Callable[..., None]) -> None:
    stub_simulation(RuntimeError("boom"))

    signal = conflicts.analyze(make_context())

    assert signal.status == "error"
    assert "RuntimeError: boom" in signal.summary


# ------------------------------------------------------------------ findings


def test_one_finding_per_region(make_context: Callable[..., AnalysisContext], stub_simulation: Callable[..., None]) -> None:
    stub_simulation(
        simulation(
            clean=False,
            conflicted_files=["a.py", "b.py"],
            regions=[region("a.py"), region("a.py", ours=(30, 33)), region("b.py")],
        )
    )

    signal = conflicts.analyze(make_context())

    assert signal.status == "findings"
    assert len(signal.findings) == 3
    assert {f.file for f in signal.findings} == {"a.py", "b.py"}
    assert all(f.severity == "high" for f in signal.findings)
    assert all(f.confidence == "high" for f in signal.findings)
    assert signal.metadata["conflicted_files"] == 2
    assert signal.metadata["regions"] == 3
    assert signal.summary == "3 conflicted regions across 2 files"


def test_binary_region_is_critical_and_carries_no_excerpt(make_context: Callable[..., AnalysisContext], stub_simulation: Callable[..., None]) -> None:
    binary = region("asset.bin", ours=(0, 0), theirs=(0, 0), ours_text=None, theirs_text=None, is_binary=True)
    stub_simulation(simulation(clean=False, conflicted_files=["asset.bin"], regions=[binary]))

    signal = conflicts.analyze(make_context())

    (finding,) = signal.findings
    assert finding.severity == "critical"
    assert finding.line is None
    assert finding.evidence["binary"] is True
    assert "ours_excerpt" not in finding.evidence
    assert "binary conflict" in signal.summary


def test_conflicted_file_without_region_is_still_reported(make_context: Callable[..., AnalysisContext], stub_simulation: Callable[..., None]) -> None:
    stub_simulation(simulation(clean=False, conflicted_files=["a.py", "gone.py"], regions=[region("a.py")]))

    signal = conflicts.analyze(make_context())

    unrepresented = [f for f in signal.findings if f.file == "gone.py"]
    assert len(unrepresented) == 1
    assert unrepresented[0].severity == "critical"
    assert unrepresented[0].evidence == {"conflicted_file": "gone.py", "region_available": False}
    assert signal.metadata["files_without_regions"] == 1
    assert "no extractable region" in signal.summary


def test_evidence_carries_ranges_refs_and_excerpts() -> None:
    finding = conflicts.finding_for_region(
        region("a.py", ours=(4, 6), theirs=(4, 7), base_text="common\n"),
        base="main",
        head="feature",
    )

    assert finding.file == "a.py"
    assert finding.line == 4
    assert finding.evidence["ours_range"] == [4, 6]
    assert finding.evidence["theirs_range"] == [4, 7]
    assert finding.evidence["ours_ref"] == "main"
    assert finding.evidence["theirs_ref"] == "feature"
    assert finding.evidence["ours_excerpt"] == "ours line"
    assert finding.evidence["theirs_excerpt"] == "theirs line"
    assert finding.evidence["base_excerpt"] == "common"


def test_excerpts_are_truncated() -> None:
    huge = "\n".join(f"line {i}" for i in range(500))
    finding = conflicts.finding_for_region(region("a.py", ours_text=huge, theirs_text=huge), base="main", head="feature")

    excerpt = finding.evidence["ours_excerpt"]
    assert excerpt.endswith(conflicts.TRUNCATION_MARKER)
    assert len(excerpt.splitlines()) == conflicts.MAX_EXCERPT_LINES + 1


def test_region_without_line_numbers_omits_line() -> None:
    finding = conflicts.finding_for_region(region("a.py", ours=(0, 0), theirs=(0, 0)), base="main", head="feature")

    assert finding.line is None
    assert finding.file == "a.py"


# ----------------------------------------------------------------- summarize


@pytest.mark.parametrize(
    ("regions", "files", "expected"),
    [
        ([], [], "no textual conflicts"),
        ([region("a.py")], ["a.py"], "1 conflicted region across 1 file"),
        ([region("a.py"), region("b.py")], ["a.py", "b.py"], "2 conflicted regions across 2 files"),
    ],
)
def test_summarize_pluralises(regions: list[ConflictRegion], files: list[str], expected: str) -> None:
    assert conflicts.summarize(regions, files) == expected


def test_summarize_counts_files_without_regions_separately() -> None:
    summary = conflicts.summarize([region("a.py")], ["a.py", "b.py"])

    assert summary.startswith("1 conflicted region across 2 files")
    assert "1 file with no extractable region" in summary
