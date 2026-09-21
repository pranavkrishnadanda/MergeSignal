"""Integration tests for S2 against real repositories and the real symbol index.

These are the DESIGN.md §4 semantic regression scenarios: rename-vs-new-caller,
signature-change-with-new-callers, removed-symbol-still-referenced, plus the
degradations (unsupported language, binary file, clean merge).

:func:`build_full_context` assembles the *symmetric* context the engine is
specified against — both sides' diffs, symbols, references and symbol changes,
all measured from the merge base.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mergesignal.analysis.diff import diff_refs
from mergesignal.analysis.index import SymbolIndex, diff_symbols, index_paths
from mergesignal.config import Config
from mergesignal.git.repo import Repo
from mergesignal.models import AnalysisContext
from mergesignal.signals import semantic
from tests.helpers.repo_builder import RepoBuilder

pytestmark = pytest.mark.integration


def build_full_context(
    path: Path, *, base: str = "main", head: str = "feature", config: Config | None = None
) -> AnalysisContext:
    """Build a two-sided :class:`~mergesignal.models.AnalysisContext` for a repo.

    Indexes the union of both sides' touched paths so that a change on one side
    can be compared against references on the other — the whole point of S2.
    """
    config = config or Config()
    repo = Repo(path)
    merge_base = repo.merge_base(base, head)
    anchor = merge_base or base

    base_diff = diff_refs(repo, anchor, base, max_files=config.analysis.max_files)
    head_diff = diff_refs(repo, anchor, head, max_files=config.analysis.max_files)
    paths = sorted(set(index_paths(base_diff)) | set(index_paths(head_diff)))

    anchor_index = SymbolIndex.from_ref(repo, anchor, paths)
    base_index = SymbolIndex.from_ref(repo, base, paths)
    head_index = SymbolIndex.from_ref(repo, head, paths)

    return AnalysisContext(
        repo_path=str(path),
        base=base,
        head=head,
        merge_base=merge_base,
        base_diff=base_diff,
        head_diff=head_diff,
        base_symbols=base_index.all_symbols,
        head_symbols=head_index.all_symbols,
        base_references=base_index.all_references,
        head_references=head_index.all_references,
        base_changes=diff_symbols(anchor_index, base_index),
        head_changes=diff_symbols(anchor_index, head_index),
        config=config,
    )


def test_rename_vs_new_caller(builder: RepoBuilder) -> None:
    """feature renames lib.old_name; main adds a caller of the old name."""
    path = builder.scenario_rename_vs_new_caller().build()

    signal = semantic.analyze(build_full_context(path))

    assert signal.status == "findings"
    (finding,) = [f for f in signal.findings if f.evidence["pattern"] == semantic.PATTERN_RENAMED]
    assert finding.severity == "high"
    assert finding.confidence in ("low", "medium")  # rename detection is a heuristic
    assert finding.evidence["old_name"] == "old_name"
    assert finding.evidence["symbol"] == "new_name"
    assert finding.evidence["changed_side"] == "head"
    assert finding.evidence["reference_side"] == "base"
    assert finding.evidence["definition"]["file"] == "lib.py"
    assert {ref["file"] for ref in finding.evidence["references"]} == {"caller.py"}
    assert finding.file == "caller.py"


def test_signature_change_with_new_callers(builder: RepoBuilder) -> None:
    """feature widens compute(a) to compute(a, b, c); main adds compute(1)."""
    path = builder.scenario_signature_change().build()

    signal = semantic.analyze(build_full_context(path))

    (finding,) = [f for f in signal.findings if f.evidence["pattern"] == semantic.PATTERN_SIGNATURE]
    assert finding.severity == "high"
    assert finding.confidence == "high"
    assert finding.evidence["signature_breakage"] == "breaking"
    assert finding.evidence["old_signature"] == "(a)"
    assert finding.evidence["new_signature"] == "(a, b, c)"
    assert finding.evidence["new_callers_only"] is True
    assert any(ref["line"] == 3 for ref in finding.evidence["references"])


def test_removed_symbol_still_referenced(builder: RepoBuilder) -> None:
    """feature deletes a helper that main starts calling from another module."""
    builder.file("lib.py", "def helper(x):\n    return x\n\n\ndef keep(y):\n    return y\n").commit(
        "add lib"
    )
    builder.branch("feature")
    builder.file("lib.py", "def keep(y):\n    return y\n").commit("drop helper")
    builder.checkout("main")
    path = (
        builder.file("app.py", "from lib import helper\n\nhelper(3)\n")
        .commit("call helper")
        .build()
    )

    signal = semantic.analyze(build_full_context(path))

    (finding,) = [f for f in signal.findings if f.evidence["pattern"] == semantic.PATTERN_REMOVED]
    assert finding.severity == "high"  # cross-file, same package -> medium confidence
    assert finding.confidence == "medium"
    assert finding.evidence["symbol"] == "helper"
    assert finding.evidence["change"] == "removed"
    assert finding.evidence["definition"]["file"] == "lib.py"
    assert sorted(ref["line"] for ref in finding.evidence["references"]) == [1, 3]
    assert finding.evidence["reference_count"] == 2


def test_removed_symbol_referenced_in_its_own_file_is_critical(builder: RepoBuilder) -> None:
    builder.file(
        "lib.py", "def helper(x):\n    return x\n\n\ndef caller(y):\n    return y\n"
    ).commit("add lib")
    builder.branch("feature")
    builder.file("lib.py", "def caller(y):\n    return y\n").commit("drop helper")
    builder.checkout("main")
    path = (
        builder.file(
            "lib.py", "def helper(x):\n    return x\n\n\ndef caller(y):\n    return helper(y)\n"
        )
        .commit("use helper")
        .build()
    )

    signal = semantic.analyze(build_full_context(path))

    (finding,) = [f for f in signal.findings if f.evidence["pattern"] == semantic.PATTERN_REMOVED]
    assert finding.severity == "critical"
    assert finding.confidence == "high"
    assert finding.file == "lib.py"


def test_clean_merge_with_symbols_reports_ok(two_branch_repo: Path) -> None:
    signal = semantic.analyze(build_full_context(two_branch_repo))

    assert signal.status == "ok"
    assert signal.findings == []
    assert signal.metadata["languages"] == ["python"]


def test_unsupported_language_is_skipped_not_ok(builder: RepoBuilder) -> None:
    path = builder.scenario_unsupported_language().build()

    signal = semantic.analyze(build_full_context(path))

    assert signal.status == "skipped"
    assert "no supported language" in signal.summary


def test_binary_diff_is_skipped(builder: RepoBuilder) -> None:
    path = builder.scenario_binary_file().build()

    signal = semantic.analyze(build_full_context(path))

    assert signal.status == "skipped"


def test_base_equals_head_is_skipped(simple_repo: Path) -> None:
    signal = semantic.analyze(build_full_context(simple_repo, base="main", head="main"))

    assert signal.status == "skipped"
    assert "empty diff" in signal.summary


def test_renaming_a_file_and_calling_it_from_the_other_side(builder: RepoBuilder) -> None:
    """A declaration that moves file is not a removal — do not fabricate one."""
    builder.file("lib.py", "def helper(x):\n    return x\n").commit("add lib")
    builder.branch("feature")
    builder.move("lib.py", "util.py")
    builder.commit("move lib to util")
    builder.checkout("main")
    path = (
        builder.file("app.py", "from lib import helper\n\nhelper(1)\n")
        .commit("call helper")
        .build()
    )

    signal = semantic.analyze(build_full_context(path))

    removals = [f for f in signal.findings if f.evidence["pattern"] == semantic.PATTERN_REMOVED]
    assert removals == []


def test_typescript_signature_change_is_detected(builder: RepoBuilder) -> None:
    """Semantic analysis is not python-only."""
    builder.file("lib.ts", "export function compute(a: number) {\n  return a;\n}\n").commit(
        "add lib"
    )
    builder.branch("feature")
    builder.file(
        "lib.ts", "export function compute(a: number, b: number) {\n  return a + b;\n}\n"
    ).commit("widen")
    builder.checkout("main")
    path = (
        builder.file("app.ts", "import { compute } from './lib';\n\ncompute(1);\n")
        .commit("call it")
        .build()
    )

    signal = semantic.analyze(build_full_context(path))

    assert signal.status == "findings"
    assert any(f.evidence["symbol"] == "compute" for f in signal.findings)
