"""Unit tests for :class:`SymbolIndex` and the symbol differ."""

from __future__ import annotations

import pytest

from mergesignal.analysis.index import (
    BINARY_SNIFF_BYTES,
    SymbolIndex,
    diff_symbols,
    index_paths,
    looks_binary,
    rename_candidates,
)
from mergesignal.models import Diff, DiffFile, Symbol, SymbolChange

BASE_SOURCES = {
    "lib.py": (
        "def old_name(x):\n"
        "    return x\n"
        "\n"
        "def compute(a):\n"
        "    return a\n"
        "\n"
        "def doomed(z):\n"
        "    return z\n"
    ),
    "caller.py": "from lib import compute\n\ncompute(1)\n",
}

HEAD_SOURCES = {
    "lib.py": (
        "def new_name(x):\n"
        "    return x\n"
        "\n"
        "def compute(a, b, c):\n"
        "    return a + b + c\n"
    ),
    "caller.py": "from lib import compute\n\ncompute(1, 2, 3)\n",
}


@pytest.fixture
def base_index() -> SymbolIndex:
    return SymbolIndex.from_sources("base", BASE_SOURCES)


@pytest.fixture
def head_index() -> SymbolIndex:
    return SymbolIndex.from_sources("head", HEAD_SOURCES)


def kinds(changes: list[SymbolChange]) -> dict[str, list[str]]:
    """``{change kind: [symbol names]}`` — readable assertions."""
    out: dict[str, list[str]] = {}
    for change in changes:
        out.setdefault(change.kind, []).append(change.symbol.name)
    return out


# ------------------------------------------------------------------- index


def test_index_records_the_ref_and_sorts_content(base_index: SymbolIndex) -> None:
    assert base_index.ref == "base"
    symbols = base_index.all_symbols
    assert [(s.file, s.line) for s in symbols] == sorted((s.file, s.line) for s in symbols)


def test_definitions_and_references_lookup(base_index: SymbolIndex) -> None:
    (definition,) = base_index.definitions("old_name")
    assert (definition.file, definition.line, definition.signature) == ("lib.py", 1, "(x)")
    assert base_index.definitions("nope") == []
    assert [r.line for r in base_index.references("compute")] == [1, 3]
    assert base_index.references("nope") == []


def test_defines_and_names(base_index: SymbolIndex) -> None:
    assert base_index.defines("compute") is True
    assert base_index.defines("nope") is False
    assert {"old_name", "compute", "doomed"} <= base_index.names


def test_per_file_views(base_index: SymbolIndex) -> None:
    assert [s.name for s in base_index.symbols_in("lib.py")] == [
        "old_name",
        "compute",
        "doomed",
    ]
    assert base_index.symbols_in("missing.py") == []
    assert [r.name for r in base_index.references_in("caller.py")] == ["compute", "compute"]
    assert base_index.files == {"lib.py", "caller.py"}


def test_unsupported_and_ruleless_files_are_reported_as_skipped() -> None:
    index = SymbolIndex.from_sources(
        "head", {"a.py": "def f():\n    pass\n", "notes.zzz": "text\n", "conf.yaml": "k: v\n"}
    )
    assert index.skipped_paths == ["conf.yaml", "notes.zzz"]
    assert [s.name for s in index.all_symbols] == ["f"]


def test_mutating_returned_lists_does_not_corrupt_the_index(base_index: SymbolIndex) -> None:
    base_index.all_symbols.clear()
    base_index.definitions("compute").clear()
    base_index.skipped_paths.append("bogus")
    assert base_index.definitions("compute")
    assert base_index.skipped_paths == []


def test_empty_index_is_usable() -> None:
    index = SymbolIndex("head", [], [])
    assert index.names == set()
    assert index.all_symbols == []
    assert index.all_references == []
    assert index.skipped_paths == []
    assert "SymbolIndex" in repr(index)


# ------------------------------------------------------------- binary sniff


def test_looks_binary() -> None:
    assert looks_binary(b"\x00\x01\x02") is True
    assert looks_binary(b"def f():\n    pass\n") is False
    assert looks_binary(b"a" * BINARY_SNIFF_BYTES + b"\x00") is False, "sniff is bounded"


# --------------------------------------------------------------- index_paths


def test_index_paths_covers_both_sides_of_a_rename_and_skips_binaries() -> None:
    diff = Diff(
        base="main",
        head="feature",
        files=[
            DiffFile(path="new/name.py", old_path="old/name.py"),
            DiffFile(path="logo.png", is_binary=True),
            DiffFile(path="app.py"),
            DiffFile(path="app.py"),
        ],
    )
    assert index_paths(diff) == ["new/name.py", "old/name.py", "app.py"]


# ------------------------------------------------------------- diff_symbols


def test_diff_symbols_classifies_every_flavour(
    base_index: SymbolIndex, head_index: SymbolIndex
) -> None:
    grouped = kinds(diff_symbols(base_index, head_index))
    assert grouped["renamed"] == ["new_name"]
    assert grouped["signature_changed"] == ["compute"]
    assert grouped["removed"] == ["doomed"]
    assert "added" not in grouped


def test_signature_change_carries_both_signatures(
    base_index: SymbolIndex, head_index: SymbolIndex
) -> None:
    change = next(
        c for c in diff_symbols(base_index, head_index) if c.kind == "signature_changed"
    )
    assert change.old_signature == "(a)"
    assert change.new_signature == "(a, b, c)"
    assert change.confidence == "high"
    assert change.symbol.file == "lib.py"


def test_rename_detection_is_a_heuristic_never_high_confidence(
    base_index: SymbolIndex, head_index: SymbolIndex
) -> None:
    change = next(c for c in diff_symbols(base_index, head_index) if c.kind == "renamed")
    assert change.old_name == "old_name"
    assert change.symbol.name == "new_name"
    assert change.confidence in {"low", "medium"}


def test_rename_detection_can_be_disabled(
    base_index: SymbolIndex, head_index: SymbolIndex
) -> None:
    grouped = kinds(diff_symbols(base_index, head_index, detect_renames=False))
    assert "renamed" not in grouped
    assert sorted(grouped["removed"]) == ["doomed", "old_name"]
    assert grouped["added"] == ["new_name"]


def test_added_symbol_is_reported_with_its_new_signature() -> None:
    base = SymbolIndex.from_sources("base", {"a.py": "x = 1\n"})
    head = SymbolIndex.from_sources("head", {"a.py": "x = 1\n\ndef fresh(a, b):\n    return a\n"})
    (change,) = [c for c in diff_symbols(base, head) if c.symbol.name == "fresh"]
    assert change.kind == "added"
    assert change.new_signature == "(a, b)"
    assert change.old_signature is None


def test_identical_refs_produce_no_changes(base_index: SymbolIndex) -> None:
    assert diff_symbols(base_index, SymbolIndex.from_sources("head", BASE_SOURCES)) == []


def test_reformatting_is_not_a_signature_change() -> None:
    base = SymbolIndex.from_sources("base", {"a.py": "def f(a,b=1):\n    return a\n"})
    head = SymbolIndex.from_sources("head", {"a.py": "def f(a, b = 1):\n    return a\n"})
    assert [c.kind for c in diff_symbols(base, head)] == []


def test_declaration_moving_file_is_modified_not_removed_plus_added() -> None:
    base = SymbolIndex.from_sources("base", {"old.py": "def moved(a):\n    return a\n"})
    head = SymbolIndex.from_sources("head", {"new.py": "def moved(a):\n    return a\n"})
    (change,) = diff_symbols(base, head)
    assert change.kind == "modified"
    assert change.old_file == "old.py"
    assert change.symbol.file == "new.py"


def test_body_growth_is_reported_as_modified_with_low_confidence() -> None:
    base = SymbolIndex.from_sources("base", {"a.py": "def f(a):\n    return a\n"})
    head = SymbolIndex.from_sources(
        "head", {"a.py": "def f(a):\n    log(a)\n    log(a)\n    return a\n"}
    )
    (change,) = [c for c in diff_symbols(base, head) if c.symbol.name == "f"]
    assert change.kind == "modified"
    assert change.confidence == "low"


def test_methods_are_scoped_by_their_class() -> None:
    base = SymbolIndex.from_sources(
        "base", {"a.py": "class A:\n    def go(self, x):\n        return x\n"}
    )
    head = SymbolIndex.from_sources(
        "head", {"a.py": "class B:\n    def go(self, x):\n        return x\n"}
    )
    grouped = kinds(diff_symbols(base, head, detect_renames=False))
    assert grouped["removed"] == ["A", "go"]
    assert grouped["added"] == ["B", "go"]


def test_changes_are_sorted_deterministically(
    base_index: SymbolIndex, head_index: SymbolIndex
) -> None:
    changes = diff_symbols(base_index, head_index)
    keys = [(c.symbol.file, c.symbol.line, c.symbol.name, c.kind) for c in changes]
    assert keys == sorted(keys)
    assert diff_symbols(base_index, head_index) == changes


# --------------------------------------------------------- rename candidates


def _symbol(name: str, *, file: str = "lib.py", signature: str | None = "(x)", line: int = 1) -> Symbol:
    return Symbol(name=name, kind="function", file=file, line=line, signature=signature)


def test_rename_candidates_pairs_similar_names() -> None:
    matches = rename_candidates([_symbol("old_name")], [_symbol("new_name")])
    assert [(o.name, n.name) for o, n, _ in matches] == [("old_name", "new_name")]
    assert matches[0][2] >= 0.6


def test_rename_candidates_accepts_an_unambiguous_swap_with_the_same_signature() -> None:
    """One removal, one addition, same scope and signature: that is a rename."""
    matches = rename_candidates([_symbol("foo")], [_symbol("bar")])
    assert [(o.name, n.name) for o, n, _ in matches] == [("foo", "bar")]


def test_rename_candidates_refuse_to_cross_files() -> None:
    assert rename_candidates([_symbol("old_name")], [_symbol("new_name", file="other.py")]) == []


def test_rename_candidates_refuse_ambiguous_dissimilar_pairs() -> None:
    removed = [_symbol("alpha"), _symbol("beta", line=5)]
    added = [_symbol("zulu", signature="(a, b, c)"), _symbol("yankee", signature="(q)", line=5)]
    assert rename_candidates(removed, added) == []


def test_rename_candidates_use_each_symbol_at_most_once() -> None:
    removed = [_symbol("old_name"), _symbol("old_named", line=5)]
    added = [_symbol("new_name"), _symbol("new_named", line=5)]
    matches = rename_candidates(removed, added)
    assert len({id(o) for o, _, _ in matches}) == len(matches)
    assert len({id(n) for _, n, _ in matches}) == len(matches)


def test_rename_candidates_respect_the_threshold() -> None:
    assert rename_candidates([_symbol("old_name")], [_symbol("new_name")], threshold=0.99) == []


def test_rename_candidates_are_best_first() -> None:
    removed = [_symbol("handler"), _symbol("unrelated", line=5)]
    added = [_symbol("handler2"), _symbol("totallyother", line=5)]
    matches = rename_candidates(removed, added, threshold=0.0)
    scores = [score for _, _, score in matches]
    assert scores == sorted(scores, reverse=True)


def test_empty_inputs_are_fine() -> None:
    assert rename_candidates([], []) == []
    assert rename_candidates([_symbol("a")], []) == []
    assert rename_candidates([], [_symbol("a")]) == []
