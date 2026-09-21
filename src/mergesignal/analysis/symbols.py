"""Symbol and reference extraction via tree-sitter. **Owned by Agent C.**

Given a file's text and its language, produce the declarations it makes
(:class:`~mergesignal.models.Symbol`) and the names it uses
(:class:`~mergesignal.models.Reference`) — FR-3.

Definitions vs references is the whole game for the semantic signal: a deleted
definition only matters because something still *references* it.

Extraction contract:

* Symbols carry 1-based lines and, where the grammar exposes one, a normalised
  ``signature`` (parameter list, whitespace collapsed) so signature changes are
  detectable without false positives from reformatting.
* Methods carry ``parent`` set to the enclosing class; nested functions carry
  their enclosing function.
* Imports are symbols of kind ``import`` whose ``name`` is the *bound local
  name* (what other code will reference), not the module path.
* References exclude the definition site itself, and exclude attribute access on
  an obviously unrelated receiver where the grammar makes that clear.

Edge cases:

* Unsupported language -> ``([], [])``, never an exception.
* Binary content -> skipped by the caller; this module may assume text.
* Files above ``AnalysisConfig.max_file_bytes`` -> caller skips; if called
  anyway, implementations should still bound their work.
* Syntax errors -> extract from the non-``ERROR`` parts of the tree.
"""

from __future__ import annotations

from typing import Any

from mergesignal.models import Reference, Symbol

#: Files larger than this are not parsed; extraction returns empty lists.
MAX_PARSE_BYTES = 1_000_000


def extract(source: str, path: str, language: str | None) -> tuple[list[Symbol], list[Reference]]:
    """Extract every definition and reference from one file.

    The primary entry point; :func:`extract_symbols` and
    :func:`extract_references` are the halves, exposed for targeted tests.

    :param language: ``None`` (unsupported) yields ``([], [])``.
    :returns: ``(symbols, references)``, both sorted by line then name so that
        snapshots are stable.
    """
    raise NotImplementedError


def extract_symbols(source: str, path: str, language: str) -> list[Symbol]:
    """Extract declarations (functions, classes, methods, variables, imports)."""
    raise NotImplementedError


def extract_references(source: str, path: str, language: str) -> list[Reference]:
    """Extract name usages: calls, attribute access, type names, import targets.

    ``Reference.context`` is the source line, stripped, capped at a readable
    length so it can be embedded in findings verbatim.
    """
    raise NotImplementedError


def normalize_signature(signature: str | None) -> str | None:
    """Canonicalise a signature so formatting changes are not false positives.

    Collapses runs of whitespace, strips comments where trivially possible, and
    removes the trailing/leading parentheses padding. ``None`` passes through.
    Two signatures are considered "the same" iff their normalised forms are
    equal, so this function defines what ``signature_changed`` means.
    """
    raise NotImplementedError


def node_text(node: Any, source_bytes: bytes) -> str:
    """Decode a tree-sitter node's byte span back to text.

    Uses ``errors="replace"`` — a file with mixed encodings must not crash the
    extractor.
    """
    raise NotImplementedError


def walk(node: Any) -> Any:
    """Depth-first iterator over a tree-sitter node and all its descendants.

    Uses a ``TreeCursor`` rather than recursion so deeply nested files (minified
    JS, generated code) do not blow the Python stack.
    """
    raise NotImplementedError


def symbols_by_name(symbols: list[Symbol]) -> dict[str, list[Symbol]]:
    """Group symbols by unqualified name; a name may have several definitions."""
    raise NotImplementedError


def references_by_name(references: list[Reference]) -> dict[str, list[Reference]]:
    """Group references by name — the lookup the semantic signal performs."""
    raise NotImplementedError
