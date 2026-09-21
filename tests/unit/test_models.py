"""Contract tests for the pydantic models — the shape every agent codes against."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mergesignal.models import (
    SEVERITY_ORDER,
    SIGNAL_NAMES,
    ConflictRegion,
    DiffFile,
    Finding,
    Hunk,
    LineRange,
    MergeSimulation,
    Report,
    Signal,
    Symbol,
    SymbolChange,
    severity_at_least,
)


class TestLineRange:
    def test_rejects_inverted_range(self) -> None:
        with pytest.raises(ValidationError):
            LineRange(start=10, end=3)

    def test_length_and_empty_range(self) -> None:
        assert LineRange(start=1, end=4).length == 3
        assert LineRange(start=5, end=5).length == 0

    def test_overlaps(self) -> None:
        assert LineRange(start=1, end=5).overlaps(LineRange(start=4, end=9))
        assert not LineRange(start=1, end=4).overlaps(LineRange(start=4, end=9))
        assert not LineRange(start=3, end=3).overlaps(LineRange(start=3, end=3))


class TestDiffFile:
    def test_binary_file_may_not_carry_hunks(self) -> None:
        hunk = Hunk(file_path="a.bin", base_range=LineRange(start=1, end=2), head_range=LineRange(start=1, end=2))
        with pytest.raises(ValidationError, match="binary"):
            DiffFile(path="a.bin", is_binary=True, hunks=[hunk])

    def test_cannot_be_new_and_deleted(self) -> None:
        with pytest.raises(ValidationError, match="new and deleted"):
            DiffFile(path="a.py", is_new=True, is_deleted=True)

    def test_rename_and_churn(self) -> None:
        f = DiffFile(path="new.py", old_path="old.py", additions=3, deletions=2)
        assert f.is_rename
        assert f.churn == 5
        assert not DiffFile(path="a.py", old_path="a.py").is_rename


class TestSymbolChange:
    def test_renamed_requires_old_name(self) -> None:
        symbol = Symbol(name="f", kind="function", file="a.py", line=1)
        with pytest.raises(ValidationError, match="old_name"):
            SymbolChange(symbol=symbol, kind="renamed")

    def test_signature_change_requires_difference(self) -> None:
        symbol = Symbol(name="f", kind="function", file="a.py", line=1)
        with pytest.raises(ValidationError, match="differing signatures"):
            SymbolChange(symbol=symbol, kind="signature_changed", old_signature="(a)", new_signature="(a)")

    def test_qualified_name(self) -> None:
        assert Symbol(name="run", kind="method", file="a.py", line=2, parent="Job").qualified_name == "Job.run"
        assert Symbol(name="run", kind="function", file="a.py", line=2).qualified_name == "run"


class TestFinding:
    def test_line_requires_file(self) -> None:
        with pytest.raises(ValidationError, match="requires"):
            Finding(signal="semantic", severity="high", title="t", line=3)

    def test_sort_key_orders_worst_first(self) -> None:
        low = Finding(signal="risk", severity="low", title="l", file="b.py", line=1)
        critical = Finding(signal="risk", severity="critical", title="c", file="z.py", line=1)
        assert sorted([low, critical], key=lambda f: f.sort_key)[0] is critical


class TestSignal:
    def test_ok_with_findings_is_rejected(self, make_finding) -> None:
        with pytest.raises(ValidationError, match="status 'ok'"):
            Signal(name="semantic", status="ok", findings=[make_finding()])

    def test_findings_status_requires_findings(self) -> None:
        with pytest.raises(ValidationError, match="no findings"):
            Signal(name="semantic", status="findings")

    def test_factories(self) -> None:
        assert Signal.error("risk", "boom").status == "error"
        assert Signal.skipped("overlap", "nothing to compare").status == "skipped"
        assert Signal.from_findings("risk", []).status == "ok"

    def test_from_findings_sets_findings_status(self, make_finding) -> None:
        signal = Signal.from_findings("semantic", [make_finding()])
        assert signal.status == "findings"

    def test_max_severity(self, make_finding) -> None:
        signal = Signal.from_findings("risk", [make_finding(severity="low"), make_finding(severity="critical")])
        assert signal.max_severity == "critical"
        assert Signal(name="risk", status="ok").max_severity is None


class TestMergeSimulation:
    def test_clean_merge_cannot_carry_conflicts(self) -> None:
        region = ConflictRegion(file="a.py", ours_range=LineRange(start=1, end=2), theirs_range=LineRange(start=1, end=2))
        with pytest.raises(ValidationError, match="clean"):
            MergeSimulation(base="main", head="feature", clean=True, conflicted_files=["a.py"], regions=[region])

    def test_conflicted_simulation(self) -> None:
        sim = MergeSimulation(base="main", head="feature", clean=False, conflicted_files=["a.py"])
        assert not sim.clean


class TestReport:
    def test_defaults_and_serialisation(self, make_report) -> None:
        report = make_report()
        payload = json.loads(report.model_dump_json())
        assert payload["version"] == "1"
        assert [s["name"] for s in payload["signals"]] == list(SIGNAL_NAMES)
        assert report.generated_at.tzinfo is not None

    def test_lookup_and_aggregates(self, make_report) -> None:
        report = make_report()
        assert report.signal("semantic") is not None
        assert report.signal("nope") is None
        assert report.has_errors
        assert report.max_severity == "high"
        assert len(report.all_findings) == 1

    def test_exceeds_threshold(self, make_report) -> None:
        report = make_report()
        assert report.exceeds("high")
        assert not report.exceeds("critical")

    def test_clean_report_does_not_exceed(self) -> None:
        report = Report(base="main", head="main", signals=[Signal(name="conflicts", status="ok")])
        assert not report.exceeds("low")
        assert report.max_severity is None
        assert not report.has_errors


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Finding(signal="risk", severity="low", title="t", typo_field=1)


def test_severity_helpers() -> None:
    assert SEVERITY_ORDER["critical"] > SEVERITY_ORDER["low"]
    assert severity_at_least("high", "medium")
    assert severity_at_least("high", "high")
    assert not severity_at_least("medium", "high")
