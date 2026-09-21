"""Integration tests: index two real git refs and diff their symbols.

These cover the DESIGN.md §4 scenarios the semantic signal depends on —
rename-vs-new-caller and signature-change-with-new-callers — end to end, from
``git diff`` through tree-sitter to :class:`~mergesignal.models.SymbolChange`.
"""

from __future__ import annotations

import pytest

from mergesignal.analysis.diff import diff_refs
from mergesignal.analysis.index import SymbolIndex, build_indexes, diff_symbols
from mergesignal.git.repo import Repo
from mergesignal.models import SymbolChange
from tests.helpers.repo_builder import RepoBuilder

pytestmark = pytest.mark.integration


def changes_between(
    builder: RepoBuilder, base: str, head: str, **kwargs: object
) -> list[SymbolChange]:
    """Full pipeline: diff two refs, index both sides, classify the symbols."""
    repo = Repo(builder.build())
    diff = diff_refs(repo, base, head, merge_base=True)
    base_index, head_index = build_indexes(repo, base, head, diff)
    return diff_symbols(base_index, head_index, **kwargs)  # type: ignore[arg-type]


# ----------------------------------------------- DESIGN.md regression scenarios


def test_rename_vs_new_caller_scenario(builder: RepoBuilder) -> None:
    """``feature`` renames ``lib.old_name``; ``main`` adds a call to the old name."""
    builder.scenario_rename_vs_new_caller()
    repo = Repo(builder.build())
    merge_base = repo.merge_base("main", "feature")
    assert merge_base is not None

    head_changes = changes_between(builder, merge_base, "feature")
    (renamed,) = [c for c in head_changes if c.kind == "renamed"]
    assert renamed.old_name == "old_name"
    assert renamed.symbol.name == "new_name"
    assert renamed.symbol.file == "lib.py"
    assert renamed.confidence != "high", "rename detection is a heuristic (FR-4)"

    # The other side added a caller that still names the old symbol: exactly the
    # evidence the semantic signal pairs with the rename above.
    diff = diff_refs(repo, merge_base, "main", merge_base=True)
    _, main_index = build_indexes(repo, merge_base, "main", diff)
    assert [r.file for r in main_index.references("old_name")] == ["caller.py", "caller.py"]


def test_signature_change_with_new_callers_scenario(builder: RepoBuilder) -> None:
    builder.scenario_signature_change()
    repo = Repo(builder.build())
    merge_base = repo.merge_base("main", "feature")
    assert merge_base is not None

    (changed,) = [
        c for c in changes_between(builder, merge_base, "feature") if c.kind == "signature_changed"
    ]
    assert changed.symbol.name == "compute"
    assert changed.old_signature == "(a)"
    assert changed.new_signature == "(a, b, c)"

    diff = diff_refs(repo, merge_base, "main", merge_base=True)
    _, main_index = build_indexes(repo, merge_base, "main", diff)
    calls = main_index.references("compute")
    assert [(r.file, r.line) for r in calls] == [("caller.py", 1), ("caller.py", 3)]
    assert calls[-1].context == "compute(1)"


def test_clean_merge_scenario_only_adds_symbols(builder: RepoBuilder) -> None:
    builder.scenario_clean_merge()
    repo = Repo(builder.build())
    merge_base = repo.merge_base("main", "feature")
    assert merge_base is not None
    changes = changes_between(builder, merge_base, "feature")
    assert [(c.kind, c.symbol.name) for c in changes] == [("added", "b")]


def test_binary_files_are_skipped_not_parsed(builder: RepoBuilder) -> None:
    builder.scenario_binary_file()
    repo = Repo(builder.build())
    diff = diff_refs(repo, "main", "feature")
    base_index, head_index = build_indexes(repo, "main", "feature", diff)
    assert base_index.all_symbols == []
    assert "asset.bin" in base_index.skipped_paths
    assert "asset.bin" in head_index.skipped_paths
    assert diff_symbols(base_index, head_index) == []


def test_unsupported_language_yields_no_symbols_and_is_recorded(builder: RepoBuilder) -> None:
    builder.scenario_unsupported_language()
    repo = Repo(builder.build())
    diff = diff_refs(repo, "main", "feature")
    base_index, head_index = build_indexes(repo, "main", "feature", diff)
    assert base_index.all_symbols == []
    assert base_index.skipped_paths == ["script.zzz"]
    assert diff_symbols(base_index, head_index) == []


# ------------------------------------------------------------------ mechanics


def test_a_file_deleted_on_one_side_reports_removals(builder: RepoBuilder) -> None:
    builder.file("lib.py", "def gone(a):\n    return a\n").commit("add lib")
    builder.remove("lib.py").commit("drop lib")
    changes = changes_between(builder, "HEAD~1", "HEAD")
    assert [(c.kind, c.symbol.name, c.symbol.file) for c in changes] == [
        ("removed", "gone", "lib.py")
    ]


def test_a_file_added_on_one_side_reports_additions(builder: RepoBuilder) -> None:
    builder.file("seed.py", "x = 1\n").commit("seed")
    builder.file("lib.py", "def fresh(a, b):\n    return a\n").commit("add lib")
    (change,) = [c for c in changes_between(builder, "HEAD~1", "HEAD") if c.symbol.name == "fresh"]
    assert change.kind == "added"
    assert change.new_signature == "(a, b)"


def test_a_symbol_moved_between_files_is_modified_not_removed(builder: RepoBuilder) -> None:
    body = "def moved(a):\n    return a\n"
    builder.file("old.py", body).commit("one")
    builder.move("old.py", "new.py").commit("two")
    (change,) = changes_between(builder, "HEAD~1", "HEAD")
    assert change.kind == "modified"
    assert (change.old_file, change.symbol.file) == ("old.py", "new.py")


def test_indexes_cover_both_sides_of_a_rename(builder: RepoBuilder) -> None:
    builder.file("old.py", "def moved(a):\n    return a\n").commit("one")
    builder.move("old.py", "new.py").commit("two")
    repo = Repo(builder.build())
    diff = diff_refs(repo, "HEAD~1", "HEAD")
    base_index, head_index = build_indexes(repo, "HEAD~1", "HEAD", diff)
    assert [s.file for s in base_index.all_symbols] == ["old.py"]
    assert [s.file for s in head_index.all_symbols] == ["new.py"]


def test_cross_language_repository(builder: RepoBuilder) -> None:
    builder.file("app.py", "def py_fn(a):\n    return a\n")
    builder.file("app.go", "package main\n\nfunc GoFn(a int) int {\n\treturn a\n}\n")
    builder.file("app.ts", "export function tsFn(a: number): number { return a; }\n")
    builder.commit("polyglot")
    builder.file("app.py", "def py_fn(a, b):\n    return a\n")
    builder.file("app.go", "package main\n\nfunc GoFn(a int, b int) int {\n\treturn a\n}\n")
    builder.file("app.ts", "export function tsFn(a: number, b: number): number { return a; }\n")
    builder.commit("widen everything")
    changes = changes_between(builder, "HEAD~1", "HEAD")
    assert {(c.kind, c.symbol.name) for c in changes} == {
        ("signature_changed", "py_fn"),
        ("signature_changed", "GoFn"),
        ("signature_changed", "tsFn"),
    }


def test_max_files_bound_records_the_remainder_as_skipped(builder: RepoBuilder) -> None:
    for index in range(4):
        builder.file(f"m{index}.py", f"def f{index}():\n    return {index}\n")
    builder.commit("many")
    repo = Repo(builder.build())
    paths = [f"m{i}.py" for i in range(4)]
    bounded = SymbolIndex.from_ref(repo, "HEAD", paths, max_files=2)
    assert len(bounded.symbols_in("m0.py")) == 1
    assert bounded.skipped_paths == ["m2.py", "m3.py"]


def test_max_file_bytes_bound_skips_oversized_blobs(builder: RepoBuilder) -> None:
    builder.file("big.py", "def f():\n    return 1\n" + "# pad\n" * 200).commit("big")
    repo = Repo(builder.build())
    index = SymbolIndex.from_ref(repo, "HEAD", ["big.py"], max_file_bytes=64)
    assert index.all_symbols == []
    assert index.skipped_paths == ["big.py"]


def test_paths_absent_at_a_ref_are_not_reported_as_skipped(builder: RepoBuilder) -> None:
    builder.file("present.py", "def f():\n    return 1\n").commit("one")
    repo = Repo(builder.build())
    index = SymbolIndex.from_ref(repo, "HEAD", ["present.py", "never_existed.py"])
    assert [s.name for s in index.all_symbols] == ["f"]
    assert index.skipped_paths == []


def test_whole_tree_scan_is_opt_in_and_capped(builder: RepoBuilder) -> None:
    builder.file("a.py", "def a():\n    return 1\n")
    builder.file("pkg/b.py", "def b():\n    return 2\n")
    builder.file("notes.zzz", "not code\n")
    builder.commit("tree")
    repo = Repo(builder.build())
    index = SymbolIndex.from_tree(repo, "HEAD")
    assert {s.name for s in index.all_symbols} == {"a", "b"}
    assert "notes.zzz" not in index.files

    capped = SymbolIndex.from_tree(repo, "HEAD", max_files=1)
    assert len(capped.skipped_paths) == 1


def test_syntax_error_on_one_side_degrades_instead_of_crashing(builder: RepoBuilder) -> None:
    builder.file("lib.py", "def ok(a):\n    return a\n\ndef also(b):\n    return b\n").commit("one")
    builder.file("lib.py", "def ok(a, b):\n    return a\n\ndef broken(:::\n").commit("two")
    changes = changes_between(builder, "HEAD~1", "HEAD")
    kinds = {(c.kind, c.symbol.name) for c in changes}
    assert ("signature_changed", "ok") in kinds
    assert ("removed", "also") in kinds
