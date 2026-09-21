"""Symbol indexes over git refs, and diffing between them. **Owned by Agent C.**

:class:`SymbolIndex` answers "what does this ref define, and where is each name
used?". :func:`diff_symbols` turns two indexes into a list of
:class:`~mergesignal.models.SymbolChange` — the input the semantic signal
reasons over (FR-4).

Indexes are built lazily and bounded: by default only the files touched by a
diff are parsed, because parsing a whole monorepo would blow NFR-1. A
whole-tree scan is available but must be opt-in and capped.
"""

from __future__ import annotations

from mergesignal.git.repo import Repo
from mergesignal.models import Diff, Reference, Symbol, SymbolChange


class SymbolIndex:
    """Definitions and references for a set of files at one git ref.

    Construct via :meth:`from_ref` or :meth:`from_sources`. Instances are
    immutable once built; the analysis pipeline builds two (base and head) and
    hands them to :func:`diff_symbols`.
    """

    def __init__(self, ref: str, symbols: list[Symbol], references: list[Reference], *, skipped: list[str] | None = None) -> None:
        """Store the extracted data.

        :param skipped: paths that were not parsed (binary, too large,
            unsupported language) — surfaced so signals can lower confidence
            rather than silently assuming a symbol is unused.
        """
        raise NotImplementedError

    @classmethod
    def from_ref(cls, repo: Repo, ref: str, paths: list[str], *, max_files: int | None = None, max_file_bytes: int = 1_000_000) -> SymbolIndex:
        """Build an index for ``paths`` as they exist at ``ref``.

        Paths absent at that ref (added on the other side) are simply skipped,
        not an error.
        """
        raise NotImplementedError

    @classmethod
    def from_sources(cls, ref: str, sources: dict[str, str]) -> SymbolIndex:
        """Build an index from in-memory ``{path: source}`` — the unit-test door."""
        raise NotImplementedError

    def definitions(self, name: str) -> list[Symbol]:
        """Every definition of ``name`` in this index (may be several)."""
        raise NotImplementedError

    def references(self, name: str) -> list[Reference]:
        """Every usage of ``name`` in this index."""
        raise NotImplementedError

    def symbols_in(self, path: str) -> list[Symbol]:
        """Definitions made by one file."""
        raise NotImplementedError

    def references_in(self, path: str) -> list[Reference]:
        """Usages appearing in one file."""
        raise NotImplementedError

    @property
    def names(self) -> set[str]:
        """Every defined name in the index."""
        raise NotImplementedError

    @property
    def all_symbols(self) -> list[Symbol]:
        """Every definition in the index, sorted by (file, line, name)."""
        raise NotImplementedError

    @property
    def all_references(self) -> list[Reference]:
        """Every usage in the index, sorted by (file, line, name)."""
        raise NotImplementedError

    @property
    def skipped_paths(self) -> list[str]:
        """Paths that were not parsed (binary, oversized, unsupported language)."""
        raise NotImplementedError


def build_indexes(repo: Repo, base: str, head: str, diff: Diff, *, max_files: int | None = None, max_file_bytes: int = 1_000_000) -> tuple[SymbolIndex, SymbolIndex]:
    """Build the base-side and head-side indexes for a diff's touched files.

    Both indexes cover the *union* of paths, including renames' old and new
    paths, so a symbol that moved files is still comparable.
    """
    raise NotImplementedError


def diff_symbols(base_index: SymbolIndex, head_index: SymbolIndex, *, detect_renames: bool = True, rename_similarity: float = 0.6) -> list[SymbolChange]:
    """Compare two indexes and classify what happened to each symbol.

    Classification rules:

    ``removed``
        Defined in base, absent in head, no rename candidate.
    ``added``
        Absent in base, defined in head.
    ``renamed``
        A removal and an addition in the same file/parent whose signatures and
        bodies are similar enough (``rename_similarity``). Heuristic, so the
        resulting change carries ``confidence`` below ``"high"``.
    ``signature_changed``
        Same name and scope, different normalised signature.
    ``modified``
        Same name and signature, body lines changed.

    :param detect_renames: disable to treat every rename as removed + added,
        which is cheaper and produces no false rename claims.
    """
    raise NotImplementedError


def rename_candidates(removed: list[Symbol], added: list[Symbol], *, threshold: float = 0.6) -> list[tuple[Symbol, Symbol, float]]:
    """Pair removed with added symbols that plausibly represent a rename.

    :returns: ``(old, new, similarity)`` triples above ``threshold``, each
        symbol used at most once, best matches first.
    """
    raise NotImplementedError
