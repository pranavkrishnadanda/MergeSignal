"""S2 — semantic breakage detection. **Owned by Agent E.** The product's heart.

Catches the dangerous case: the merge is textually *clean*, so git is happy, but
the result does not work. Three patterns (FR-4), each cross-referencing what one
side did against what the other side added:

``removed-symbol-still-referenced``
    Side A deleted a definition; side B added (or kept) references to it.
    Confidence ``high`` when the reference is in a file A also touched or in the
    same module; ``medium`` cross-file; ``low`` when the name is common
    (``run``, ``get``) and could resolve elsewhere.

``renamed-old-name-referenced``
    Side A renamed a definition; side B references the old name. Inherits the
    rename detection's confidence, capped at ``medium`` — rename detection is
    itself a heuristic.

``signature_changed-with-new-callers``
    Side A changed a signature; side B added call sites. Confidence depends on
    whether the arity actually changed (``high``) or only names/defaults did
    (``low``/``medium``).

Hard rule: **never fabricate**. Only report what the two indexes actually
support; if a language is unsupported, the index is empty and the signal must
report ``skipped``, not "no problems found".

Severity policy: removed-still-referenced ``critical`` at high confidence and
``high`` otherwise; renamed-old-name ``high``; signature change ``high`` when
arity changed, ``medium`` otherwise.

Implementation notes
--------------------

* The analysis runs **both ways**: ``base_changes`` against ``head_references``
  and ``head_changes`` against ``base_references``. ``changed_side`` /
  ``reference_side`` in every finding's evidence say which way round it was, so
  a reader can tell who has to fix what.
* A pattern is suppressed when the *referencing* side re-declares the same name
  in the same file the declaration was removed from (excluding ``import``
  declarations, which are usages dressed as declarations): after the merge the
  name still resolves, so reporting it would be a fabrication.
* "Added by the other side" is decided from that side's diff
  (:func:`mergesignal.analysis.diff.changed_line_numbers`). When the diff is not
  available at all the filter cannot run; findings that depend on it then carry
  ``evidence["new_callers_only"] = False`` and a capped confidence rather than
  being silently dropped or silently trusted.
"""

from __future__ import annotations

import posixpath
from typing import Any

from mergesignal.models import (
    CONFIDENCE_ORDER,
    AnalysisContext,
    Confidence,
    Diff,
    Finding,
    Reference,
    Severity,
    Signal,
    SymbolChange,
)

#: Name registered in :data:`mergesignal.signals.REGISTRY`.
NAME = "semantic"

#: Names too generic to attribute confidently across files.
COMMON_NAMES: frozenset[str] = frozenset({"run", "get", "set", "main", "init", "test", "handle", "name", "value", "data"})

#: Most reference locations carried in one finding's evidence.
MAX_EVIDENCE_REFERENCES = 10

#: Evidence ``pattern`` values, also used as metadata counter keys.
PATTERN_REMOVED = "removed_symbol_still_referenced"
PATTERN_RENAMED = "renamed_old_name_referenced"
PATTERN_SIGNATURE = "signature_changed_new_callers"


# --------------------------------------------------------------------- entry


def analyze(ctx: AnalysisContext) -> Signal:
    """Run the semantic signal in both directions. Never raises.

    Checks base-side changes against head-side references *and* head-side
    changes against base-side references, because breakage is symmetric: either
    party can be the one who pulled the rug.

    Returns ``skipped`` when neither side produced any symbols (no supported
    language in the diff) — reporting "ok" there would be a lie.
    """
    try:
        return _analyze(ctx)
    except Exception as exc:  # noqa: BLE001 - engines never raise (see signals/__init__)
        return Signal.error(NAME, f"{type(exc).__name__}: {exc}")


def _analyze(ctx: AnalysisContext) -> Signal:
    """The real body of :func:`analyze`, wrapped by its exception trap."""
    diffs = [d for d in (ctx.base_diff, ctx.head_diff) if d is not None]
    changed_files = [f for d in diffs for f in d.files]

    if diffs and not changed_files:
        return Signal.skipped(NAME, "empty diff; nothing to analyse", changed_files=0)

    languages = sorted({f.language for f in changed_files if f.language})
    if changed_files and not languages:
        return Signal.skipped(
            NAME,
            "no supported language among the changed files; semantic analysis unavailable",
            changed_files=len(changed_files),
            languages=[],
        )

    if not ctx.base_symbols and not ctx.head_symbols:
        return Signal.skipped(
            NAME,
            "no symbols were indexed for the changed files; semantic analysis unavailable",
            changed_files=len(changed_files),
            languages=languages,
        )

    base_added = added_reference_lines(ctx.base_diff)
    head_added = added_reference_lines(ctx.head_diff)
    base_declares = _redeclared_names(ctx.base_changes)
    head_declares = _redeclared_names(ctx.head_changes)

    findings: list[Finding] = []
    directions = (
        # (changes made here, references observed there, labels, ...)
        (ctx.base_changes, ctx.head_references, "base", "head", head_added, head_declares),
        (ctx.head_changes, ctx.base_references, "head", "base", base_added, base_declares),
    )
    for changes, references, changed_side, reference_side, added, declared in directions:
        if not changes or not references:
            continue
        kwargs: dict[str, Any] = {
            "changed_side": changed_side,
            "reference_side": reference_side,
            "redeclared": declared,
        }
        findings.extend(find_removed_still_referenced(changes, references, **kwargs))
        findings.extend(find_renamed_old_name_referenced(changes, references, **kwargs))
        findings.extend(
            find_signature_change_new_callers(changes, references, added_lines=added, **kwargs)
        )

    findings.sort(key=lambda f: (f.sort_key, f.title))

    counts = dict.fromkeys((PATTERN_REMOVED, PATTERN_RENAMED, PATTERN_SIGNATURE), 0)
    for finding in findings:
        pattern = finding.evidence.get("pattern")
        if pattern in counts:
            counts[pattern] += 1

    metadata: dict[str, Any] = {
        "changes_considered": len(ctx.base_changes) + len(ctx.head_changes),
        "references_considered": len(ctx.base_references) + len(ctx.head_references),
        "languages": languages,
        "patterns": counts,
    }
    summary = _summarize(findings, counts)
    return Signal.from_findings(NAME, findings, summary, **metadata)


# ------------------------------------------------------------------ patterns


def find_removed_still_referenced(
    changes: list[SymbolChange],
    references: list[Reference],
    *,
    changed_side: str,
    reference_side: str,
    redeclared: dict[str, set[str]] | None = None,
) -> list[Finding]:
    """Definitions removed on one side that the other side still references.

    References located inside the removed definition's own body are excluded —
    deleting a function deletes its internal recursion too.

    :param redeclared: name -> files in which the *referencing* side declares
        that name itself (from :func:`_redeclared_names`). A name the other side
        re-declares in the same file still resolves after the merge, so it is
        not reported.
    """
    by_name = _references_by_name(references)
    findings: list[Finding] = []

    for change in changes:
        if change.kind != "removed":
            continue
        symbol = change.symbol
        if _is_redeclared(change, redeclared):
            continue
        candidates = [
            reference
            for reference in by_name.get(symbol.name, ())
            if not _inside_definition(reference, change)
            and (symbol.kind != "import" or reference.file == symbol.file)
        ]
        if not candidates:
            continue

        confidence = _best_confidence(candidates, symbol.file, symbol.name)
        severity: Severity = "critical" if confidence == "high" else "high"
        findings.append(
            _build_finding(
                change,
                candidates,
                pattern=PATTERN_REMOVED,
                severity=severity,
                confidence=confidence,
                title=f"{symbol.kind} '{symbol.name}' removed on {changed_side} is still referenced on {reference_side}",
                detail=(
                    f"{changed_side} deletes {symbol.kind} '{symbol.name}' "
                    f"({symbol.file}:{symbol.line}), but {reference_side} still references it at "
                    f"{_locations_text(candidates)}. The merge is textually clean; the result will fail at "
                    "import or call time."
                ),
                changed_side=changed_side,
                reference_side=reference_side,
            )
        )
    return findings


def find_renamed_old_name_referenced(
    changes: list[SymbolChange],
    references: list[Reference],
    *,
    changed_side: str,
    reference_side: str,
    redeclared: dict[str, set[str]] | None = None,
) -> list[Finding]:
    """Renamed definitions whose *old* name is still referenced elsewhere."""
    by_name = _references_by_name(references)
    findings: list[Finding] = []

    for change in changes:
        if change.kind != "renamed" or not change.old_name:
            continue
        symbol = change.symbol
        old_name = change.old_name
        old_file = change.old_file or symbol.file
        if redeclared and old_file in redeclared.get(old_name, set()):
            continue
        candidates = list(by_name.get(old_name, ()))
        if not candidates:
            continue

        # Rename detection is itself a heuristic: never claim more than medium.
        confidence = _min_confidence(
            _best_confidence(candidates, old_file, old_name),
            change.confidence,
            "medium",
        )
        findings.append(
            _build_finding(
                change,
                candidates,
                pattern=PATTERN_RENAMED,
                severity="high",
                confidence=confidence,
                title=f"'{old_name}' renamed to '{symbol.name}' on {changed_side} but referenced on {reference_side}",
                detail=(
                    f"{changed_side} renames {symbol.kind} '{old_name}' to '{symbol.name}' "
                    f"({symbol.file}:{symbol.line}), while {reference_side} references the old name at "
                    f"{_locations_text(candidates)}. Rename detection is a heuristic, so confirm before acting."
                ),
                changed_side=changed_side,
                reference_side=reference_side,
                extra={"old_name": old_name, "old_file": old_file, "new_name": symbol.name},
            )
        )
    return findings


def find_signature_change_new_callers(
    changes: list[SymbolChange],
    references: list[Reference],
    *,
    changed_side: str,
    reference_side: str,
    added_lines: dict[str, set[int]] | None = None,
    redeclared: dict[str, set[str]] | None = None,
) -> list[Finding]:
    """Signature changes on one side with call sites added on the other.

    Only references that the other side *added* count: a pre-existing call site
    is the author's own problem and would already be visible in their tests.

    :param added_lines: path -> line numbers the referencing side added, from
        :func:`added_reference_lines`. ``None`` means the diff was unavailable,
        in which case every reference is considered (with reduced confidence and
        ``evidence["new_callers_only"] = False``), because silently reporting
        nothing would hide a real break.
    """
    by_name = _references_by_name(references)
    findings: list[Finding] = []
    known_added = added_lines is not None

    for change in changes:
        if change.kind != "signature_changed":
            continue
        symbol = change.symbol
        candidates = [
            reference
            for reference in by_name.get(symbol.name, ())
            if not _inside_definition(reference, change)
            and (not known_added or reference.line in (added_lines or {}).get(reference.file, set()))
        ]
        if not candidates:
            continue

        arity = arity_changed(change.old_signature, change.new_signature)
        severity: Severity = "high" if arity else "medium"
        confidence: Confidence = "high" if arity else ("medium" if arity is None else "low")
        if symbol.name in COMMON_NAMES:
            confidence = _min_confidence(confidence, "low")
        if not known_added:
            confidence = _min_confidence(confidence, "medium")

        change_kind = "arity" if arity else ("unknown" if arity is None else "names/defaults only")
        findings.append(
            _build_finding(
                change,
                candidates,
                pattern=PATTERN_SIGNATURE,
                severity=severity,
                confidence=confidence,
                title=f"signature of '{symbol.name}' changed on {changed_side} with new callers on {reference_side}",
                detail=(
                    f"{changed_side} changes '{symbol.name}' from {change.old_signature or '?'} to "
                    f"{change.new_signature or '?'} ({symbol.file}:{symbol.line}) while {reference_side} "
                    f"{'adds' if known_added else 'has'} call sites at {_locations_text(candidates)}. "
                    f"Changed: {change_kind}."
                ),
                changed_side=changed_side,
                reference_side=reference_side,
                extra={
                    "old_signature": change.old_signature,
                    "new_signature": change.new_signature,
                    "arity_changed": arity,
                    "new_callers_only": known_added,
                },
            )
        )
    return findings


# ------------------------------------------------------------------- helpers


def arity_changed(old_signature: str | None, new_signature: str | None) -> bool | None:
    """Whether the parameter *count* changed between two signatures.

    :returns: ``True``/``False``, or ``None`` when either signature is unknown
        or unparseable — callers must treat ``None`` as "lower the confidence",
        not as ``False``.
    """
    old_params = parameter_list(old_signature)
    new_params = parameter_list(new_signature)
    if old_params is None or new_params is None:
        return None
    return len(old_params) != len(new_params)


def parameter_list(signature: str | None) -> list[str] | None:
    """Split a signature's outermost parenthesised group into parameters.

    :returns: the (possibly empty) parameter list, or ``None`` when the
        signature is missing or has no balanced parameter group — languages
        whose grammar exposes no signature land here, which is why
        :func:`arity_changed` can answer "unknown".
    """
    if signature is None:
        return None
    text = signature.strip()
    start = text.find("(")
    if start < 0:
        return None

    depth = 0
    end = -1
    for index in range(start, len(text)):
        char = text[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end < 0:
        return None

    inner = text[start + 1 : end].strip()
    if not inner:
        return []

    params: list[str] = []
    current: list[str] = []
    depth = 0
    for char in inner:
        if char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth -= 1
        if char == "," and depth == 0:
            params.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    params.append("".join(current).strip())
    return [param for param in params if param]


def confidence_for(reference: Reference, symbol_file: str, name: str) -> str:
    """Grade how sure we are that ``reference`` really resolves to ``name``.

    Same file -> ``high``; same package/directory -> ``medium``; elsewhere or a
    name in :data:`COMMON_NAMES` -> ``low``.
    """
    if name in COMMON_NAMES:
        return "low"
    if reference.file == symbol_file:
        return "high"
    if posixpath.dirname(reference.file) == posixpath.dirname(symbol_file):
        return "medium"
    return "low"


def added_reference_lines(diff: Diff | None) -> dict[str, set[int]] | None:
    """Post-image line numbers each file gained in ``diff``.

    :returns: path -> added line numbers, or ``None`` when ``diff`` is ``None``
        (the caller must then treat "was this reference added?" as unknown).
    """
    if diff is None:
        return None
    from mergesignal.analysis.diff import changed_line_numbers

    return {changed.path: changed_line_numbers(changed, side="head") for changed in diff.files}


def _redeclared_names(changes: list[SymbolChange]) -> dict[str, set[str]]:
    """Names a side declares itself: name -> files it declares them in.

    ``import`` declarations are excluded: importing a name is a *usage*, not a
    definition that would make a removal on the other side harmless.
    """
    declared: dict[str, set[str]] = {}
    for change in changes:
        if change.kind not in ("added", "renamed") or change.symbol.kind == "import":
            continue
        declared.setdefault(change.symbol.name, set()).add(change.symbol.file)
    return declared


def _is_redeclared(change: SymbolChange, redeclared: dict[str, set[str]] | None) -> bool:
    """``True`` when the referencing side re-declares this name in the same file."""
    if not redeclared:
        return False
    return change.symbol.file in redeclared.get(change.symbol.name, set())


def _inside_definition(reference: Reference, change: SymbolChange) -> bool:
    """``True`` when ``reference`` sits inside the changed declaration's own body.

    Line numbers come from two different trees, so this is a heuristic — but a
    conservative one: it only ever suppresses findings in the declaration's own
    file.
    """
    symbol = change.symbol
    if reference.file != symbol.file:
        return False
    end = symbol.end_line if symbol.end_line is not None else symbol.line
    return symbol.line <= reference.line <= end


def _references_by_name(references: list[Reference]) -> dict[str, list[Reference]]:
    """Group references by name, preserving file/line order within each name."""
    grouped: dict[str, list[Reference]] = {}
    for reference in sorted(references, key=lambda r: (r.file, r.line, r.name)):
        grouped.setdefault(reference.name, []).append(reference)
    return grouped


def _best_confidence(references: list[Reference], symbol_file: str, name: str) -> Confidence:
    """Highest per-reference confidence in the group — the worst case for the user."""
    best = "low"
    for reference in references:
        graded = confidence_for(reference, symbol_file, name)
        if CONFIDENCE_ORDER[graded] > CONFIDENCE_ORDER[best]:
            best = graded
    return best  # type: ignore[return-value]


def _min_confidence(*values: str) -> Confidence:
    """The least confident of several grades."""
    return min(values, key=lambda value: CONFIDENCE_ORDER[value])  # type: ignore[return-value]


def _locations_text(references: list[Reference], limit: int = 3) -> str:
    """``"a.py:3, b.py:9 (+2 more)"`` — compact locations for a detail string."""
    shown = [f"{reference.file}:{reference.line}" for reference in references[:limit]]
    extra = len(references) - len(shown)
    return ", ".join(shown) + (f" (+{extra} more)" if extra > 0 else "")


def _build_finding(
    change: SymbolChange,
    references: list[Reference],
    *,
    pattern: str,
    severity: Severity,
    confidence: Confidence,
    title: str,
    detail: str,
    changed_side: str,
    reference_side: str,
    extra: dict[str, Any] | None = None,
) -> Finding:
    """Assemble one finding whose evidence carries **both** sides' locations."""
    symbol = change.symbol
    ordered = sorted(references, key=lambda r: (r.file, r.line))
    evidence: dict[str, Any] = {
        "pattern": pattern,
        "symbol": symbol.name,
        "symbol_kind": symbol.kind,
        "change": change.kind,
        "change_confidence": change.confidence,
        "changed_side": changed_side,
        "reference_side": reference_side,
        "definition": {
            "file": symbol.file,
            "line": symbol.line,
            "end_line": symbol.end_line,
            "signature": symbol.signature,
            "parent": symbol.parent,
        },
        "references": [
            {"file": reference.file, "line": reference.line, "context": reference.context}
            for reference in ordered[:MAX_EVIDENCE_REFERENCES]
        ],
        "reference_count": len(ordered),
        "references_truncated": len(ordered) > MAX_EVIDENCE_REFERENCES,
    }
    if extra:
        evidence.update(extra)

    primary = ordered[0]
    return Finding(
        signal=NAME,
        severity=severity,
        confidence=confidence,
        title=title,
        detail=detail,
        file=primary.file,
        line=primary.line,
        evidence=evidence,
    )


def _summarize(findings: list[Finding], counts: dict[str, int]) -> str:
    """One-line summary naming the patterns that fired."""
    if not findings:
        return "no semantic breakage detected"
    labels = {
        PATTERN_REMOVED: "removed-still-referenced",
        PATTERN_RENAMED: "renamed-old-name-referenced",
        PATTERN_SIGNATURE: "signature-changed-with-new-callers",
    }
    parts = [f"{count} {labels[pattern]}" for pattern, count in counts.items() if count]
    noun = "issue" if len(findings) == 1 else "issues"
    return f"{len(findings)} potential semantic {noun}: " + ", ".join(parts)
