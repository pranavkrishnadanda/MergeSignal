"""Unit tests for S2 — semantic breakage (:mod:`mergesignal.signals.semantic`).

Contexts are hand-built here so each heuristic is exercised in isolation; the
end-to-end "real repo, real tree-sitter index" path lives in
``tests/integration/test_semantic_int.py``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from mergesignal.models import (
    AnalysisContext,
    Diff,
    DiffFile,
    Hunk,
    LineRange,
    Reference,
    Symbol,
    SymbolChange,
)
from mergesignal.signals import semantic


def symbol(
    name: str = "helper",
    *,
    file: str = "lib.py",
    line: int = 10,
    end_line: int | None = 14,
    kind: str = "function",
    signature: str | None = "(a)",
    parent: str | None = None,
) -> Symbol:
    """Build a :class:`~mergesignal.models.Symbol`."""
    return Symbol(
        name=name,
        kind=kind,
        file=file,
        line=line,
        end_line=end_line,
        signature=signature,
        parent=parent,
    )  # type: ignore[arg-type]


def removed(name: str = "helper", **kwargs: object) -> SymbolChange:
    """A ``removed`` :class:`~mergesignal.models.SymbolChange`."""
    sym = symbol(name, **kwargs)  # type: ignore[arg-type]
    return SymbolChange(symbol=sym, kind="removed", old_signature=sym.signature)


def renamed(new: str = "new_name", old: str = "old_name", **kwargs: object) -> SymbolChange:
    """A ``renamed`` :class:`~mergesignal.models.SymbolChange`."""
    return SymbolChange(
        symbol=symbol(new, **kwargs), kind="renamed", old_name=old, confidence="medium"
    )  # type: ignore[arg-type]


def signature_changed(
    name: str = "compute", *, old: str = "(a)", new: str = "(a, b)", **kwargs: object
) -> SymbolChange:
    """A ``signature_changed`` :class:`~mergesignal.models.SymbolChange`."""
    return SymbolChange(
        symbol=symbol(name, signature=new, **kwargs),
        kind="signature_changed",
        old_signature=old,
        new_signature=new,
    )  # type: ignore[arg-type]


def reference(
    name: str = "helper", *, file: str = "caller.py", line: int = 3, context: str = "helper(1)"
) -> Reference:
    """Build a :class:`~mergesignal.models.Reference`."""
    return Reference(name=name, file=file, line=line, context=context)


def diff_with(*paths_and_lines: tuple[str, int, int], language: str | None = "python") -> Diff:
    """A diff whose files each gained one hunk covering ``[start, end)`` on head."""
    files = [
        DiffFile(
            path=path,
            language=language,
            hunks=[
                Hunk(
                    file_path=path,
                    base_range=LineRange(start=0, end=0),
                    head_range=LineRange(start=start, end=end),
                    added_lines=["x"] * (end - start),
                )
            ],
            additions=end - start,
        )
        for path, start, end in paths_and_lines
    ]
    return Diff(base="main", head="feature", files=files)


# ------------------------------------------------------------------ skipping


def test_skips_when_no_symbols_indexed(make_context: Callable[..., AnalysisContext]) -> None:
    signal = semantic.analyze(make_context(head_diff=diff_with(("a.py", 1, 3))))

    assert signal.status == "skipped"
    assert "no symbols" in signal.summary


def test_skips_unsupported_language_rather_than_claiming_ok(
    make_context: Callable[..., AnalysisContext],
) -> None:
    ctx = make_context(head_diff=diff_with(("script.zzz", 1, 3), language=None))

    signal = semantic.analyze(ctx)

    assert signal.status == "skipped"
    assert "no supported language" in signal.summary
    assert signal.metadata["languages"] == []


def test_skips_empty_diff(make_context: Callable[..., AnalysisContext]) -> None:
    ctx = make_context(
        base_diff=Diff(base="main", head="main", files=[]),
        head_diff=Diff(base="main", head="main", files=[]),
    )

    signal = semantic.analyze(ctx)

    assert signal.status == "skipped"
    assert "empty diff" in signal.summary


def test_reports_ok_when_indexed_but_nothing_breaks(
    make_context: Callable[..., AnalysisContext],
) -> None:
    ctx = make_context(
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("helper")],
        base_references=[reference("something_else")],
        head_changes=[removed("gone")],
    )

    signal = semantic.analyze(ctx)

    assert signal.status == "ok"
    assert signal.summary == "no semantic breakage detected"


def test_never_raises(
    monkeypatch: pytest.MonkeyPatch, make_context: Callable[..., AnalysisContext]
) -> None:
    monkeypatch.setattr(
        semantic, "_analyze", lambda _ctx: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    signal = semantic.analyze(make_context())

    assert signal.status == "error"
    assert "RuntimeError: boom" in signal.summary


# ------------------------------------------- removed symbol still referenced


def test_removed_same_file_reference_is_critical(
    make_context: Callable[..., AnalysisContext],
) -> None:
    ctx = make_context(
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("helper")],
        head_changes=[removed("helper", file="lib.py")],
        base_references=[reference("helper", file="lib.py", line=40)],
    )

    signal = semantic.analyze(ctx)

    (finding,) = signal.findings
    assert finding.severity == "critical"
    assert finding.confidence == "high"
    assert finding.evidence["pattern"] == semantic.PATTERN_REMOVED
    assert finding.evidence["changed_side"] == "head"
    assert finding.evidence["reference_side"] == "base"
    assert finding.evidence["definition"] == {
        "file": "lib.py",
        "line": 10,
        "end_line": 14,
        "signature": "(a)",
        "parent": None,
    }
    assert finding.evidence["references"] == [
        {"file": "lib.py", "line": 40, "context": "helper(1)"}
    ]
    assert finding.file == "lib.py"
    assert finding.line == 40


def test_removed_cross_file_same_package_is_medium_confidence(
    make_context: Callable[..., AnalysisContext],
) -> None:
    ctx = make_context(
        head_diff=diff_with(("pkg/lib.py", 1, 3)),
        head_symbols=[symbol("helper")],
        head_changes=[removed("helper", file="pkg/lib.py")],
        base_references=[reference("helper", file="pkg/caller.py")],
    )

    (finding,) = semantic.analyze(ctx).findings

    assert finding.confidence == "medium"
    assert finding.severity == "high"


def test_removed_far_away_reference_is_low_confidence(
    make_context: Callable[..., AnalysisContext],
) -> None:
    ctx = make_context(
        head_diff=diff_with(("pkg/lib.py", 1, 3)),
        head_symbols=[symbol("helper")],
        head_changes=[removed("helper", file="pkg/lib.py")],
        base_references=[reference("helper", file="elsewhere/other.py")],
    )

    (finding,) = semantic.analyze(ctx).findings

    assert finding.confidence == "low"
    assert finding.severity == "high"


def test_common_names_never_claim_high_confidence(
    make_context: Callable[..., AnalysisContext],
) -> None:
    ctx = make_context(
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("run")],
        head_changes=[removed("run", file="lib.py")],
        base_references=[reference("run", file="lib.py", line=40)],
    )

    (finding,) = semantic.analyze(ctx).findings

    assert finding.confidence == "low"


def test_references_inside_the_removed_body_are_ignored(
    make_context: Callable[..., AnalysisContext],
) -> None:
    ctx = make_context(
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("helper")],
        head_changes=[removed("helper", file="lib.py", line=10, end_line=14)],
        base_references=[
            reference("helper", file="lib.py", line=12, context="return helper(n - 1)")
        ],
    )

    assert semantic.analyze(ctx).findings == []


def test_reference_side_redeclaring_the_name_suppresses_the_finding(
    make_context: Callable[..., AnalysisContext],
) -> None:
    ctx = make_context(
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("helper")],
        head_changes=[removed("helper", file="lib.py")],
        base_references=[reference("helper", file="caller.py")],
        base_changes=[SymbolChange(symbol=symbol("helper", file="lib.py"), kind="added")],
    )

    assert semantic.analyze(ctx).findings == []


def test_import_declarations_do_not_count_as_a_redeclaration(
    make_context: Callable[..., AnalysisContext],
) -> None:
    """Importing a name is a usage; it cannot rescue a definition removed elsewhere."""
    ctx = make_context(
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("helper")],
        head_changes=[removed("helper", file="lib.py")],
        base_references=[
            reference("helper", file="caller.py", line=1, context="from lib import helper")
        ],
        base_changes=[
            SymbolChange(
                symbol=symbol("helper", file="lib.py", kind="import", signature=None), kind="added"
            )
        ],
    )

    assert len(semantic.analyze(ctx).findings) == 1


def test_removed_import_only_matters_in_its_own_file(
    make_context: Callable[..., AnalysisContext],
) -> None:
    ctx = make_context(
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("helper")],
        head_changes=[removed("os", file="lib.py", kind="import", signature=None, end_line=None)],
        base_references=[
            reference("os", file="other.py", line=5),
            reference("os", file="lib.py", line=99),
        ],
    )

    (finding,) = semantic.analyze(ctx).findings

    assert [ref["file"] for ref in finding.evidence["references"]] == ["lib.py"]


def test_both_directions_are_checked(make_context: Callable[..., AnalysisContext]) -> None:
    """Either side can be the one who pulled the rug."""
    ctx = make_context(
        base_diff=diff_with(("a.py", 1, 3)),
        head_diff=diff_with(("b.py", 1, 3)),
        head_symbols=[symbol("helper")],
        base_changes=[removed("from_base", file="a.py")],
        head_changes=[removed("from_head", file="b.py")],
        head_references=[reference("from_base", file="a.py", line=40)],
        base_references=[reference("from_head", file="b.py", line=40)],
    )

    signal = semantic.analyze(ctx)

    sides = {(f.evidence["changed_side"], f.evidence["reference_side"]) for f in signal.findings}
    assert sides == {("base", "head"), ("head", "base")}


# ----------------------------------------------- renamed old name referenced


def test_renamed_old_name_referenced(make_context: Callable[..., AnalysisContext]) -> None:
    ctx = make_context(
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("new_name")],
        head_changes=[renamed("new_name", "old_name", file="lib.py")],
        base_references=[reference("old_name", file="lib.py", line=40)],
    )

    (finding,) = semantic.analyze(ctx).findings

    assert finding.severity == "high"
    assert finding.evidence["pattern"] == semantic.PATTERN_RENAMED
    assert finding.evidence["old_name"] == "old_name"
    assert finding.evidence["new_name"] == "new_name"


def test_rename_confidence_is_capped_at_medium(
    make_context: Callable[..., AnalysisContext],
) -> None:
    """Even a same-file reference cannot make a heuristic rename claim 'high'."""
    ctx = make_context(
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("new_name")],
        head_changes=[renamed("new_name", "old_name", file="lib.py")],
        base_references=[reference("old_name", file="lib.py", line=40)],
    )

    (finding,) = semantic.analyze(ctx).findings

    assert finding.confidence == "medium"


def test_rename_with_low_confidence_detection_stays_low(
    make_context: Callable[..., AnalysisContext],
) -> None:
    change = SymbolChange(
        symbol=symbol("new_name", file="lib.py"),
        kind="renamed",
        old_name="old_name",
        confidence="low",
    )
    ctx = make_context(
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("new_name")],
        head_changes=[change],
        base_references=[reference("old_name", file="lib.py", line=40)],
    )

    (finding,) = semantic.analyze(ctx).findings

    assert finding.confidence == "low"


def test_rename_without_references_to_the_old_name_is_quiet(
    make_context: Callable[..., AnalysisContext],
) -> None:
    ctx = make_context(
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("new_name")],
        head_changes=[renamed("new_name", "old_name", file="lib.py")],
        base_references=[reference("new_name", file="caller.py")],
    )

    assert semantic.analyze(ctx).findings == []


# --------------------------------------------- signature change new callers


def test_signature_change_with_added_caller_is_high(
    make_context: Callable[..., AnalysisContext],
) -> None:
    ctx = make_context(
        base_diff=diff_with(("caller.py", 1, 5)),
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("compute")],
        head_changes=[signature_changed("compute", old="(a)", new="(a, b)", file="lib.py")],
        base_references=[reference("compute", file="caller.py", line=3, context="compute(1)")],
    )

    (finding,) = semantic.analyze(ctx).findings

    assert finding.severity == "high"
    assert finding.confidence == "high"
    assert finding.evidence["pattern"] == semantic.PATTERN_SIGNATURE
    assert finding.evidence["arity_changed"] is True
    assert finding.evidence["new_callers_only"] is True
    assert finding.evidence["old_signature"] == "(a)"
    assert finding.evidence["new_signature"] == "(a, b)"


def test_pre_existing_callers_are_not_reported(
    make_context: Callable[..., AnalysisContext],
) -> None:
    """Line 99 is outside the lines the referencing side added, so it is theirs to own."""
    ctx = make_context(
        base_diff=diff_with(("caller.py", 1, 5)),
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("compute")],
        head_changes=[signature_changed("compute", file="lib.py")],
        base_references=[reference("compute", file="caller.py", line=99)],
    )

    assert semantic.analyze(ctx).findings == []


def test_signature_change_without_arity_change_is_medium(
    make_context: Callable[..., AnalysisContext],
) -> None:
    ctx = make_context(
        base_diff=diff_with(("caller.py", 1, 5)),
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("compute")],
        head_changes=[signature_changed("compute", old="(a)", new="(a: int)", file="lib.py")],
        base_references=[reference("compute", file="caller.py", line=3)],
    )

    (finding,) = semantic.analyze(ctx).findings

    assert finding.severity == "medium"
    assert finding.confidence == "low"
    assert finding.evidence["arity_changed"] is False


def test_unknown_signatures_lower_confidence_rather_than_claiming_no_change(
    make_context: Callable[..., AnalysisContext],
) -> None:
    change = SymbolChange(
        symbol=symbol("compute", file="lib.java", signature=None),
        kind="signature_changed",
        old_signature="unparseable",
        new_signature="also unparseable",
    )
    ctx = make_context(
        base_diff=diff_with(("caller.java", 1, 5), language="java"),
        head_diff=diff_with(("lib.java", 1, 3), language="java"),
        head_symbols=[symbol("compute", file="lib.java")],
        head_changes=[change],
        base_references=[reference("compute", file="caller.java", line=3)],
    )

    (finding,) = semantic.analyze(ctx).findings

    assert finding.evidence["arity_changed"] is None
    assert finding.confidence == "medium"
    assert finding.severity == "medium"


def test_missing_diff_means_added_callers_cannot_be_distinguished(
    make_context: Callable[..., AnalysisContext],
) -> None:
    """Without the referencing side's diff we report, but say so and cap confidence."""
    ctx = make_context(
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("compute")],
        head_changes=[signature_changed("compute", old="(a)", new="(a, b)", file="lib.py")],
        base_references=[reference("compute", file="caller.py", line=3)],
    )

    (finding,) = semantic.analyze(ctx).findings

    assert finding.evidence["new_callers_only"] is False
    assert finding.confidence == "medium"


# --------------------------------------------------------- grouping & helpers


def test_many_references_collapse_into_one_finding(
    make_context: Callable[..., AnalysisContext],
) -> None:
    refs = [reference("helper", file=f"m{i}.py", line=i + 1) for i in range(15)]
    ctx = make_context(
        head_diff=diff_with(("lib.py", 1, 3)),
        head_symbols=[symbol("helper")],
        head_changes=[removed("helper", file="lib.py")],
        base_references=refs,
    )

    (finding,) = semantic.analyze(ctx).findings

    assert finding.evidence["reference_count"] == 15
    assert len(finding.evidence["references"]) == semantic.MAX_EVIDENCE_REFERENCES
    assert finding.evidence["references_truncated"] is True


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("(a)", "(a, b)", True),
        ("(a, b)", "(a, b)", False),
        ("(a: int = 1)", "(a: str = 'x')", False),
        ("()", "(a)", True),
        (None, "(a)", None),
        ("(a)", None, None),
        ("no parens", "(a)", None),
        ("(a, b: Dict[str, int])", "(a, b: Dict[str, int], c)", True),
    ],
)
def test_arity_changed(old: str | None, new: str | None, expected: bool | None) -> None:
    assert semantic.arity_changed(old, new) is expected


@pytest.mark.parametrize(
    ("signature", "expected"),
    [
        ("()", []),
        ("(a)", ["a"]),
        ("(a, b)", ["a", "b"]),
        ("(self, x=1)", ["self", "x=1"]),
        (None, None),
        ("x", None),
    ],
)
def test_parameter_list(signature: str | None, expected: list[str] | None) -> None:
    assert semantic.parameter_list(signature) == expected


@pytest.mark.parametrize(
    ("ref_file", "symbol_file", "name", "expected"),
    [
        ("lib.py", "lib.py", "helper", "high"),
        ("pkg/a.py", "pkg/b.py", "helper", "medium"),
        ("far/a.py", "pkg/b.py", "helper", "low"),
        ("lib.py", "lib.py", "run", "low"),
    ],
)
def test_confidence_for(ref_file: str, symbol_file: str, name: str, expected: str) -> None:
    assert semantic.confidence_for(reference(name, file=ref_file), symbol_file, name) == expected


def test_added_reference_lines_handles_missing_diff() -> None:
    assert semantic.added_reference_lines(None) is None
    assert semantic.added_reference_lines(diff_with(("a.py", 3, 6))) == {"a.py": {3, 4, 5}}
