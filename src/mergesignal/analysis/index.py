"""Symbol indexes over git refs, and diffing between them. **Owned by Agent C.**

:class:`SymbolIndex` answers "what does this ref define, and where is each name
used?". :func:`diff_symbols` turns two indexes into a list of
:class:`~mergesignal.models.SymbolChange` — the input the semantic signal
reasons over (FR-4).

Indexes are built lazily and bounded: by default only the files touched by a
diff are parsed, because parsing a whole monorepo would blow NFR-1. A
whole-tree scan is available but must be opt-in and capped.

Bounds and degradations (all deliberate, none of them errors)
-------------------------------------------------------------

* ``max_file_bytes`` (default 1 MB): larger blobs are not parsed. Their paths
  land in :attr:`SymbolIndex.skipped_paths`.
* ``max_files``: indexing stops after this many paths; the remainder is
  recorded in ``skipped_paths`` so a signal can lower its confidence instead of
  concluding "nothing references this".
* Binary blobs (a NUL byte in the first :data:`BINARY_SNIFF_BYTES`) are skipped.
* Unsupported extensions are skipped — ``language_for_path`` returned ``None``.
* A path absent at the ref (added on the other side, deleted on this one) is
  simply not indexed and is *not* reported as skipped: there was nothing to
  parse, which is normal for every rename and every added file.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable

from mergesignal.analysis.languages import language_for_path
from mergesignal.analysis.symbols import extract, normalize_signature
from mergesignal.git.repo import Repo
from mergesignal.models import Diff, Reference, Symbol, SymbolChange

#: Default ceiling on a single file's size before tree-sitter is skipped.
DEFAULT_MAX_FILE_BYTES = 1_000_000

#: How many leading bytes are sniffed for a NUL when deciding "is this binary?".
BINARY_SNIFF_BYTES = 8192

#: Minimum similarity for :func:`rename_candidates` to claim a rename.
DEFAULT_RENAME_SIMILARITY = 0.6

#: Similarity floor above which a rename claim is reported with medium rather
#: than low confidence. Rename detection is a heuristic, never ``"high"``.
RENAME_MEDIUM_CONFIDENCE = 0.75


def looks_binary(payload: bytes) -> bool:
    """``True`` when a blob should never be handed to a parser.

    The same NUL-byte heuristic git itself uses, applied to a bounded prefix.
    """
    return b"\0" in payload[:BINARY_SNIFF_BYTES]


class SymbolIndex:
    """Definitions and references for a set of files at one git ref.

    Construct via :meth:`from_ref` or :meth:`from_sources`. Instances are
    immutable once built; the analysis pipeline builds two (base and head) and
    hands them to :func:`diff_symbols`.
    """

    def __init__(
        self,
        ref: str,
        symbols: list[Symbol],
        references: list[Reference],
        *,
        skipped: list[str] | None = None,
    ) -> None:
        """Store the extracted data.

        :param skipped: paths that were not parsed (binary, too large,
            unsupported language) — surfaced so signals can lower confidence
            rather than silently assuming a symbol is unused.
        """
        self.ref = ref
        self._symbols: list[Symbol] = sorted(
            symbols, key=lambda s: (s.file, s.line, s.name, s.kind)
        )
        self._references: list[Reference] = sorted(
            references,
            key=lambda r: (r.file, r.line, r.column if r.column is not None else -1, r.name),
        )
        self._skipped: list[str] = sorted(set(skipped or []))

        self._defs_by_name: dict[str, list[Symbol]] = {}
        self._defs_by_file: dict[str, list[Symbol]] = {}
        for symbol in self._symbols:
            self._defs_by_name.setdefault(symbol.name, []).append(symbol)
            self._defs_by_file.setdefault(symbol.file, []).append(symbol)

        self._refs_by_name: dict[str, list[Reference]] = {}
        self._refs_by_file: dict[str, list[Reference]] = {}
        for reference in self._references:
            self._refs_by_name.setdefault(reference.name, []).append(reference)
            self._refs_by_file.setdefault(reference.file, []).append(reference)

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return (
            f"SymbolIndex(ref={self.ref!r}, symbols={len(self._symbols)}, "
            f"references={len(self._references)}, skipped={len(self._skipped)})"
        )

    # ------------------------------------------------------------ construction

    @classmethod
    def from_ref(
        cls,
        repo: Repo,
        ref: str,
        paths: list[str],
        *,
        max_files: int | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> SymbolIndex:
        """Build an index for ``paths`` as they exist at ``ref``.

        Paths absent at that ref (added on the other side) are simply skipped,
        not an error.
        """
        ordered = _unique(paths)
        skipped: list[str] = []
        if max_files is not None and len(ordered) > max_files:
            skipped.extend(ordered[max_files:])
            ordered = ordered[:max_files]

        symbols: list[Symbol] = []
        references: list[Reference] = []
        for path in ordered:
            language = language_for_path(path)
            if language is None:
                skipped.append(path)
                continue
            payload = repo.file_bytes_at(ref, path)
            if payload is None:
                continue  # absent at this ref — normal, not a skip
            if looks_binary(payload) or len(payload) > max_file_bytes:
                skipped.append(path)
                continue
            source = payload.decode("utf-8", errors="replace")
            file_symbols, file_references = extract(source, path, language)
            # A recognised extension with no extraction rules for its grammar:
            # record it so callers know the absence of symbols is ignorance.
            if not file_symbols and not file_references and not _has_rules(language):
                skipped.append(path)
            symbols.extend(file_symbols)
            references.extend(file_references)
        return cls(ref, symbols, references, skipped=skipped)

    @classmethod
    def from_sources(cls, ref: str, sources: dict[str, str]) -> SymbolIndex:
        """Build an index from in-memory ``{path: source}`` — the unit-test door."""
        symbols: list[Symbol] = []
        references: list[Reference] = []
        skipped: list[str] = []
        for path in sorted(sources):
            language = language_for_path(path)
            if language is None or not _has_rules(language):
                skipped.append(path)
                if language is None:
                    continue
            file_symbols, file_references = extract(sources[path], path, language)
            symbols.extend(file_symbols)
            references.extend(file_references)
        return cls(ref, symbols, references, skipped=skipped)

    @classmethod
    def from_tree(
        cls,
        repo: Repo,
        ref: str,
        *,
        pattern: str | None = None,
        max_files: int | None = 2000,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> SymbolIndex:
        """Shallow whole-tree scan — opt-in and capped (see module docstring).

        Used by ``scan``-style callers that need "who references this name
        anywhere?" rather than only within the diff frontier. The default cap
        exists so that pointing MergeSignal at a monorepo degrades to a partial
        index instead of a ten-minute stall.
        """
        paths = [p for p in repo.list_files_at(ref, pattern=pattern) if language_for_path(p)]
        return cls.from_ref(repo, ref, paths, max_files=max_files, max_file_bytes=max_file_bytes)

    # --------------------------------------------------------------- lookups

    def definitions(self, name: str) -> list[Symbol]:
        """Every definition of ``name`` in this index (may be several)."""
        return list(self._defs_by_name.get(name, ()))

    def references(self, name: str) -> list[Reference]:
        """Every usage of ``name`` in this index."""
        return list(self._refs_by_name.get(name, ()))

    def symbols_in(self, path: str) -> list[Symbol]:
        """Definitions made by one file."""
        return list(self._defs_by_file.get(path, ()))

    def references_in(self, path: str) -> list[Reference]:
        """Usages appearing in one file."""
        return list(self._refs_by_file.get(path, ()))

    def defines(self, name: str) -> bool:
        """``True`` when ``name`` has at least one definition here."""
        return name in self._defs_by_name

    @property
    def names(self) -> set[str]:
        """Every defined name in the index."""
        return set(self._defs_by_name)

    @property
    def all_symbols(self) -> list[Symbol]:
        """Every definition in the index, sorted by (file, line, name)."""
        return list(self._symbols)

    @property
    def all_references(self) -> list[Reference]:
        """Every usage in the index, sorted by (file, line, name)."""
        return list(self._references)

    @property
    def skipped_paths(self) -> list[str]:
        """Paths that were not parsed (binary, oversized, unsupported language)."""
        return list(self._skipped)

    @property
    def files(self) -> set[str]:
        """Every path that contributed a definition or a reference."""
        return set(self._defs_by_file) | set(self._refs_by_file)


def _has_rules(language: str) -> bool:
    """``True`` when :mod:`mergesignal.analysis.symbols` can extract this grammar."""
    from mergesignal.analysis.symbols import SPECS

    return language in SPECS


def _unique(values: Iterable[str]) -> list[str]:
    """Stable de-duplication, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def index_paths(diff: Diff) -> list[str]:
    """Paths worth indexing for a diff: both sides of every non-binary file.

    A rename contributes its old *and* new path, so a symbol that moved files is
    still comparable across the two indexes.
    """
    paths: list[str] = []
    for changed in diff.files:
        if changed.is_binary:
            continue
        paths.append(changed.path)
        if changed.old_path:
            paths.append(changed.old_path)
    return _unique(paths)


def build_indexes(
    repo: Repo,
    base: str,
    head: str,
    diff: Diff,
    *,
    max_files: int | None = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> tuple[SymbolIndex, SymbolIndex]:
    """Build the base-side and head-side indexes for a diff's touched files.

    Both indexes cover the *union* of paths, including renames' old and new
    paths, so a symbol that moved files is still comparable.

    Binary files in the diff are recorded as skipped on both sides rather than
    silently dropped.
    """
    paths = index_paths(diff)
    binaries = [f.path for f in diff.files if f.is_binary]
    base_index = SymbolIndex.from_ref(
        repo, base, paths, max_files=max_files, max_file_bytes=max_file_bytes
    )
    head_index = SymbolIndex.from_ref(
        repo, head, paths, max_files=max_files, max_file_bytes=max_file_bytes
    )
    if binaries:
        base_index = SymbolIndex(
            base_index.ref,
            base_index.all_symbols,
            base_index.all_references,
            skipped=base_index.skipped_paths + binaries,
        )
        head_index = SymbolIndex(
            head_index.ref,
            head_index.all_symbols,
            head_index.all_references,
            skipped=head_index.skipped_paths + binaries,
        )
    return (base_index, head_index)


# ------------------------------------------------------------------ diffing


def _identity(symbol: Symbol) -> tuple[str, str, str]:
    """Within-file identity: file, qualified name, kind."""
    return (symbol.file, symbol.qualified_name, symbol.kind)


def _loose_identity(symbol: Symbol) -> tuple[str, str]:
    """Cross-file identity: qualified name and kind, so moves are matchable."""
    return (symbol.qualified_name, symbol.kind)


def _sig(symbol: Symbol) -> str | None:
    """Normalised signature, the only thing ``signature_changed`` compares."""
    return normalize_signature(symbol.signature)


def _span(symbol: Symbol) -> int:
    """Declaration height in lines — a cheap "did the body change?" proxy."""
    if symbol.end_line is None:
        return 0
    return max(symbol.end_line - symbol.line, 0)


def diff_symbols(
    base_index: SymbolIndex,
    head_index: SymbolIndex,
    *,
    detect_renames: bool = True,
    rename_similarity: float = DEFAULT_RENAME_SIMILARITY,
) -> list[SymbolChange]:
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

    Matching happens in two passes: exact ``(file, qualified_name, kind)``
    first, then ``(qualified_name, kind)`` across files so a declaration that
    moved file is reported as ``modified`` with ``old_file`` set rather than as
    a bogus remove/add pair.

    ``modified`` is inferred from the declaration's line span, because the index
    deliberately does not retain bodies (NFR-1). That makes it a *proxy*, so
    those changes carry ``confidence="low"``.
    """
    base_symbols = list(base_index.all_symbols)
    head_symbols = list(head_index.all_symbols)

    pairs, unmatched_base, unmatched_head = _match(base_symbols, head_symbols)

    changes: list[SymbolChange] = []
    for old, new in pairs:
        old_signature, new_signature = _sig(old), _sig(new)
        moved = old.file != new.file
        if (
            old_signature is not None
            and new_signature is not None
            and old_signature != new_signature
        ):
            changes.append(
                SymbolChange(
                    symbol=new,
                    kind="signature_changed",
                    old_signature=old_signature,
                    new_signature=new_signature,
                    old_file=old.file if moved else None,
                    confidence="high",
                )
            )
        elif moved or _span(old) != _span(new):
            changes.append(
                SymbolChange(
                    symbol=new,
                    kind="modified",
                    old_signature=old_signature,
                    new_signature=new_signature,
                    old_file=old.file if moved else None,
                    confidence="medium" if moved else "low",
                )
            )

    renamed_old: set[int] = set()
    renamed_new: set[int] = set()
    if detect_renames:
        for old, new, similarity in rename_candidates(
            unmatched_base, unmatched_head, threshold=rename_similarity
        ):
            renamed_old.add(id(old))
            renamed_new.add(id(new))
            changes.append(
                SymbolChange(
                    symbol=new,
                    kind="renamed",
                    old_name=old.name,
                    old_signature=_sig(old),
                    new_signature=_sig(new),
                    old_file=old.file if old.file != new.file else None,
                    confidence="medium" if similarity >= RENAME_MEDIUM_CONFIDENCE else "low",
                )
            )

    for symbol in unmatched_base:
        if id(symbol) in renamed_old:
            continue
        changes.append(
            SymbolChange(
                symbol=symbol, kind="removed", old_signature=_sig(symbol), confidence="high"
            )
        )
    for symbol in unmatched_head:
        if id(symbol) in renamed_new:
            continue
        changes.append(
            SymbolChange(symbol=symbol, kind="added", new_signature=_sig(symbol), confidence="high")
        )

    return sorted(
        changes,
        key=lambda c: (c.symbol.file, c.symbol.line, c.symbol.name, c.kind),
    )


def _match(
    base_symbols: list[Symbol], head_symbols: list[Symbol]
) -> tuple[list[tuple[Symbol, Symbol]], list[Symbol], list[Symbol]]:
    """Pair base and head declarations, exact identity first, then cross-file."""
    pairs: list[tuple[Symbol, Symbol]] = []
    head_by_identity: dict[tuple[str, str, str], list[Symbol]] = {}
    for symbol in head_symbols:
        head_by_identity.setdefault(_identity(symbol), []).append(symbol)

    remaining_base: list[Symbol] = []
    claimed: set[int] = set()
    for symbol in base_symbols:
        bucket = head_by_identity.get(_identity(symbol), [])
        partner = next((c for c in bucket if id(c) not in claimed), None)
        if partner is None:
            remaining_base.append(symbol)
            continue
        claimed.add(id(partner))
        pairs.append((symbol, partner))

    head_by_loose: dict[tuple[str, str], list[Symbol]] = {}
    for symbol in head_symbols:
        if id(symbol) not in claimed:
            head_by_loose.setdefault(_loose_identity(symbol), []).append(symbol)

    unmatched_base: list[Symbol] = []
    for symbol in remaining_base:
        bucket = head_by_loose.get(_loose_identity(symbol), [])
        partner = next((c for c in bucket if id(c) not in claimed), None)
        if partner is None:
            unmatched_base.append(symbol)
            continue
        claimed.add(id(partner))
        pairs.append((symbol, partner))

    unmatched_head = [s for s in head_symbols if id(s) not in claimed]
    return (pairs, unmatched_base, unmatched_head)


def _rename_group(symbol: Symbol) -> tuple[str, str, str]:
    """Renames are only considered within one file, scope and declaration kind."""
    return (symbol.file, symbol.parent or "", symbol.kind)


def _similarity(old: Symbol, new: Symbol, *, sole_candidate: bool) -> float:
    """Blend name similarity with signature similarity into a 0-1 score.

    A pure rename (``old_name`` -> ``new_name``, identical parameters) scores
    high on both halves. A genuinely unrelated pair scores near zero on the name
    half, which is what keeps this from inventing renames.

    ``sole_candidate`` lifts the floor for the unambiguous case — exactly one
    declaration disappeared and exactly one appeared in the same scope, with the
    same signature — because that is a rename far more often than not, even when
    the two names share no letters.
    """
    name_score = difflib.SequenceMatcher(None, old.name, new.name).ratio()
    old_signature, new_signature = _sig(old), _sig(new)
    if old_signature == new_signature:
        signature_score = 1.0
    elif old_signature is None or new_signature is None:
        signature_score = 0.0
    else:
        signature_score = difflib.SequenceMatcher(None, old_signature, new_signature).ratio()
    score = 0.5 * name_score + 0.5 * signature_score
    if sole_candidate and signature_score == 1.0:
        score = max(score, RENAME_MEDIUM_CONFIDENCE)
    return score


def rename_candidates(
    removed: list[Symbol], added: list[Symbol], *, threshold: float = DEFAULT_RENAME_SIMILARITY
) -> list[tuple[Symbol, Symbol, float]]:
    """Pair removed with added symbols that plausibly represent a rename.

    :returns: ``(old, new, similarity)`` triples above ``threshold``, each
        symbol used at most once, best matches first.

    Candidates are only drawn from the same ``(file, parent, kind)`` group:
    claiming that a function in one file was "renamed" into a class in another
    would be a fabrication, and FR-4 findings must be supportable by the index.
    """
    groups: dict[tuple[str, str, str], tuple[list[Symbol], list[Symbol]]] = {}
    for symbol in removed:
        groups.setdefault(_rename_group(symbol), ([], []))[0].append(symbol)
    for symbol in added:
        groups.setdefault(_rename_group(symbol), ([], []))[1].append(symbol)

    scored: list[tuple[float, Symbol, Symbol]] = []
    for group_removed, group_added in groups.values():
        if not group_removed or not group_added:
            continue
        sole = len(group_removed) == 1 and len(group_added) == 1
        for old in group_removed:
            for new in group_added:
                score = _similarity(old, new, sole_candidate=sole)
                if score >= threshold:
                    scored.append((score, old, new))

    scored.sort(key=lambda item: (-item[0], item[1].name, item[2].name))
    used_old: set[int] = set()
    used_new: set[int] = set()
    matches: list[tuple[Symbol, Symbol, float]] = []
    for score, old, new in scored:
        if id(old) in used_old or id(new) in used_new:
            continue
        used_old.add(id(old))
        used_new.add(id(new))
        matches.append((old, new, score))
    return matches
