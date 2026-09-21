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
    Side A changed a signature; side B added call sites. Only reported when the
    change can actually break a caller (:func:`signature_breakage`): required
    parameters added, removed or reordered are ``breaking``; arity-stable name
    swaps are ``renamed`` (keyword callers break); optional-parameter additions
    and default changes are ``compatible`` and produce **no finding**.

Hard rule: **never fabricate**. Only report what the two indexes actually
support; if a language is unsupported, the index is empty and the signal must
report ``skipped``, not "no problems found".

Two filters sit between pattern matching and the findings list, both built
because an earlier version of this engine reported plausibly-shaped findings
that were false on the real merged result:

* **Merged-tree verification.** When ``git merge-tree --write-tree`` produced a
  tree, every candidate is checked against it: a finding is suppressed when the
  flagged references no longer exist in the merged blobs (the merge itself
  rewrote them away), or when the merged tree still defines the name (the
  reference side re-declares it, so it resolves). Conflicted files contain
  conflict markers and are treated as unverifiable — never as proof either way.
* **Confidence floor.** Findings below :data:`REPORT_CONFIDENCE` — a bare name
  match across distant directories, or a name too common to attribute — go to
  ``metadata["suppressed"]`` with their reason rather than the findings list.
  Precision over recall: a suppressed finding is still auditable in JSON and
  ``--verbose`` output, but it cannot drive the exit code.

Severity policy: removed-still-referenced ``critical`` at high confidence and
``high`` otherwise; renamed-old-name ``high``; signature change ``high`` when
the change is breaking, ``medium`` for renames and unknown signatures.

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
import re
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
COMMON_NAMES: frozenset[str] = frozenset(
    {"run", "get", "set", "main", "init", "test", "handle", "name", "value", "data"}
)

#: Most reference locations carried in one finding's evidence.
MAX_EVIDENCE_REFERENCES = 10

#: Evidence ``pattern`` values, also used as metadata counter keys.
PATTERN_REMOVED = "removed_symbol_still_referenced"
PATTERN_RENAMED = "renamed_old_name_referenced"
PATTERN_SIGNATURE = "signature_changed_new_callers"

#: Findings must reach this confidence to be reported. ``low`` means "the name
#: matched, but nothing proves the reference resolves to *this* symbol" — that
#: tier produced the worst false positives in real-repo testing, so it now
#: lands in ``metadata["suppressed"]`` instead of the findings list.
REPORT_CONFIDENCE: Confidence = "medium"


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

    if (
        not ctx.base_symbols
        and not ctx.head_symbols
        and not ctx.base_changes
        and not ctx.head_changes
    ):
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

    findings, suppressed, verification = _resolve_against_merged_tree(ctx, findings)

    kept: list[Finding] = []
    for finding in findings:
        if CONFIDENCE_ORDER[finding.confidence] < CONFIDENCE_ORDER[REPORT_CONFIDENCE]:
            suppressed.append(_suppressed_entry(finding, "below confidence floor"))
        else:
            kept.append(finding)
    findings = kept

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
        "merge_verification": verification,
        "suppressed": suppressed,
        "suppressed_count": len(suppressed),
    }
    summary = _summarize(findings, counts)
    if suppressed:
        summary += f"; {len(suppressed)} low-confidence match{'es' if len(suppressed) != 1 else ''} suppressed"
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
            and (
                not known_added or reference.line in (added_lines or {}).get(reference.file, set())
            )
        ]
        if not candidates:
            continue

        breakage = signature_breakage(change.old_signature, change.new_signature)
        if breakage == "compatible":
            # Adding an optional parameter or changing a default cannot break a
            # caller that was written against the old signature.
            continue
        severity: Severity = "high" if breakage == "breaking" else "medium"
        confidence: Confidence = "high" if breakage == "breaking" else "medium"
        if symbol.name in COMMON_NAMES:
            confidence = _min_confidence(confidence, "low")
        if not known_added:
            confidence = _min_confidence(confidence, "medium")

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
                    f"Changed: {breakage}."
                ),
                changed_side=changed_side,
                reference_side=reference_side,
                extra={
                    "old_signature": change.old_signature,
                    "new_signature": change.new_signature,
                    "signature_breakage": breakage,
                    "new_callers_only": known_added,
                },
            )
        )
    return findings


# ------------------------------------------------------------------- helpers


def signature_breakage(old_signature: str | None, new_signature: str | None) -> str:
    """Whether a signature change can break callers written against the old one.

    :returns: one of

        ``"breaking"``
            A caller written against the old signature fails outright: a
            required parameter was added or removed, or required parameters
            changed order (positional callers now pass the wrong values).
        ``"renamed"``
            Arity is preserved but a parameter name changed — keyword callers
            break, positional callers survive. Worth reporting, not fatal.
        ``"compatible"``
            Optional parameters added, defaults changed, or annotations moved:
            nothing an existing caller wrote can break.
        ``"unknown"``
            Either signature is missing or unparseable — languages whose
            grammar exposes no signature land here.
    """
    old = parameter_list(old_signature)
    new = parameter_list(new_signature)
    if old is None or new is None:
        return "unknown"

    old_params = [_param_info(p) for p in old]
    new_params = [_param_info(p) for p in new]
    old_names = [name for name, _required, variadic in old_params if not variadic]
    new_names = [name for name, _required, variadic in new_params if not variadic]
    new_variadic = any(variadic for _name, _required, variadic in new_params)

    removed = [name for name in old_names if name not in new_names]
    # Params that are required on the new side but were not required before —
    # either brand-new or flipped optional -> required (callers that relied on
    # the default now break).
    old_optional = {
        name for name, required, variadic in old_params if not required and not variadic
    }
    added_required = [
        name
        for name, required, variadic in new_params
        if required and not variadic and (name not in old_names or name in old_optional)
    ]
    if removed:
        # A name that vanished is "breaking" unless the new side soaks it up
        # (variadic accepts positionally; an arity-stable swap is a rename).
        return "renamed" if (added_required or new_variadic) else "breaking"
    if added_required:
        return "breaking"

    old_required = [name for name, required, variadic in old_params if required and not variadic]
    new_required = [name for name, required, variadic in new_params if required and not variadic]
    shared_old = [name for name in old_required if name in new_names]
    shared_new = [name for name in new_required if name in old_names]
    if shared_old != shared_new:
        return "breaking"
    return "compatible"


def _param_info(param: str) -> tuple[str, bool, bool]:
    """One parameter as ``(name, required, variadic)``.

    ``name`` is the leading identifier (``x`` for ``x: int = 1``); ``required``
    means no top-level ``=`` default; ``variadic`` covers ``*args``/``**kwargs``
    (and a bare ``*``/``/`` separator, which behaves like neither but must not
    be mistaken for a named parameter).
    """
    text = param.strip()
    if text.startswith("*") or text == "/" or not text:
        return (text.lstrip("*").split(":")[0].split("=")[0].strip(), False, True)
    name = re.split(r"[:=]", text, maxsplit=1)[0].strip()
    return (name, "=" not in text, False)


def parameter_list(signature: str | None) -> list[str] | None:
    """Split a signature's outermost parenthesised group into parameters.

    :returns: the (possibly empty) parameter list, or ``None`` when the
        signature is missing or has no balanced parameter group — languages
        whose grammar exposes no signature land here, which is why
        :func:`signature_breakage` can answer "unknown".
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


# ------------------------------------------------------- merged-tree check


def _resolve_against_merged_tree(
    ctx: AnalysisContext, findings: list[Finding]
) -> tuple[list[Finding], list[dict[str, Any]], str]:
    """Ask the actual merged tree whether each candidate finding still holds.

    The pattern finders compare the two sides' diffs — they answer "head
    removed what base references". The question that matters is what the
    *merged result* contains, and ``git merge-tree --write-tree`` already
    produces that tree for S1.

    For ``removed``/``renamed`` changes the merged tree is authoritative, not
    advisory: ``git grep`` the merged result for the name, then re-parse each
    hit file. That yields the *verified* set of dangling references —

    * references that do not survive the merge are dropped (the merge itself
      rewrote them away — the classic false positive),
    * if any merged file still *defines* the name, every reference to it
      resolves and the whole finding is suppressed,
    * and references in files **untouched by either diff** are discovered here
      too — the side indexes only cover touched paths, so without this pass a
      caller in an untouched file was invisible (the recall hole the diff-only
      design shipped with).

    Signature-change findings are verified per-reference instead: a flagged
    call site must still appear in the merged blob of its file.

    Anything unverifiable (no tree produced, conflict markers in the file)
    keeps the diff-based finding as-is: verification can remove or confirm a
    finding, never invent doubt.
    """
    needs_tree = findings or any(
        change.kind in ("removed", "renamed") for change in (*ctx.base_changes, *ctx.head_changes)
    )
    if not needs_tree:
        return findings, [], "no findings to verify"

    repo = _repo(ctx)
    if repo is None:
        return findings, [], "unavailable: repository not openable"

    from mergesignal.git.merge_sim import simulate_merge

    try:
        simulation = simulate_merge(repo, ctx.base, ctx.head, extract_regions=False)
    except Exception:  # noqa: BLE001 - verification is advisory, never fatal
        return findings, [], "unavailable: merge simulation failed"
    if not simulation.tree_sha:
        return findings, [], "unavailable: no merged tree produced"

    tree = simulation.tree_sha
    conflicted = set(simulation.conflicted_files)
    cache: dict[str, str | None] = {}

    def blob(path: str) -> str | None:
        if path not in cache:
            try:
                cache[path] = repo.file_content_at(tree, path)
            except Exception:  # noqa: BLE001
                cache[path] = None
        return cache[path]

    def merged_references(name: str) -> tuple[list[dict[str, Any]], bool]:
        """Parse-verified references to ``name`` in the merged tree.

        ``git grep`` gives candidate files cheaply; each is then re-parsed so
        comments and string literals do not count, and a hit file that still
        *defines* the name flips ``defined`` — the reference resolves.
        """
        from mergesignal.analysis.languages import language_for_path
        from mergesignal.analysis.symbols import extract_references, extract_symbols

        result = repo.run_result(["grep", "-l", "-w", "-F", name, tree])
        if result.returncode != 0:
            return [], False

        refs: list[dict[str, Any]] = []
        defined = False
        for line in result.stdout.splitlines():
            # ``<tree>:<path>`` per hit; the path may itself contain ':' — split once.
            _sha, _colon, path = line.partition(":")
            if not path or path in conflicted:
                if path in conflicted:
                    refs.append(
                        {"file": path, "line": 0, "context": "(unverifiable: file is conflicted)"}
                    )
                continue
            language = language_for_path(path)
            content = blob(path)
            if language is None or content is None:
                continue
            if any(
                symbol.name == name and symbol.kind != "import"
                for symbol in extract_symbols(content, path, language)
            ):
                defined = True
            refs.extend(
                {"file": reference.file, "line": reference.line, "context": reference.context}
                for reference in extract_references(content, path, language)
                if reference.name == name
            )
        return refs, defined

    def change_key(evidence: dict[str, Any]) -> tuple[str, str, str, str]:
        """The (kind, name, file, side) identity a removed/renamed finding claims."""
        pattern = str(evidence.get("pattern"))
        kind = "renamed" if pattern == PATTERN_RENAMED else "removed"
        name = str(evidence.get("old_name") or evidence.get("symbol") or "")
        definition = evidence.get("definition") or {}
        file = str(evidence.get("old_file") or definition.get("file") or "")
        return (kind, name, file, str(evidence.get("changed_side", "")))

    # Group the diff-based removed/renamed findings by the change they claim,
    # then resolve every such change against the merged tree — including
    # changes that produced no candidate (untouched-file callers).
    by_change: dict[tuple[str, str, str, str], list[Finding]] = {}
    kept: list[Finding] = []
    suppressed: list[dict[str, Any]] = []
    for finding in findings:
        if finding.evidence.get("pattern") == PATTERN_SIGNATURE:
            continue
        by_change.setdefault(change_key(finding.evidence), []).append(finding)

    resolved: list[Finding] = []
    for side, changes in (("base", ctx.base_changes), ("head", ctx.head_changes)):
        for change in changes:
            if change.kind not in ("removed", "renamed"):
                continue
            name = (
                change.old_name if change.kind == "renamed" else change.symbol.name
            ) or change.symbol.name
            file = change.old_file or change.symbol.file
            key = (change.kind, name, file, side)
            candidates = by_change.pop(key, [])

            refs, defined = merged_references(name)
            if defined:
                for finding in candidates:
                    suppressed.append(
                        _suppressed_entry(finding, "name still defined in the merged tree")
                    )
                continue
            if not refs:
                for finding in candidates:
                    suppressed.append(
                        _suppressed_entry(
                            finding, "no flagged reference survives in the merged tree"
                        )
                    )
                continue

            refs = refs[:MAX_EVIDENCE_REFERENCES]
            if candidates:
                for finding in candidates:
                    update = {
                        "merge_verified": True,
                        "references": refs,
                        "reference_count": len(refs),
                        "references_truncated": False,
                    }
                    kept.append(
                        finding.model_copy(update={"evidence": {**finding.evidence, **update}})
                    )
            else:
                resolved.append(_merged_tree_finding(change, name, side, refs))
    # Candidates whose change key never matched (shouldn't happen, but never
    # drop a finding on a bookkeeping miss): carry them through unverified.
    for leftover in by_change.values():
        kept.extend(leftover)

    # Signature findings: each flagged call site must still appear in the
    # merged blob of its file.
    for finding in findings:
        if finding.evidence.get("pattern") != PATTERN_SIGNATURE:
            continue
        name = str(finding.evidence.get("symbol") or "")
        references = finding.evidence.get("references") or []
        surviving = []
        for reference in references:
            file = str(reference.get("file", ""))
            if file in conflicted:
                surviving.append(reference)  # marker text is unverifiable
                continue
            content = blob(file)
            if content is not None and re.search(rf"\b{re.escape(name)}\b", content):
                surviving.append(reference)
        if references and not surviving and not finding.evidence.get("references_truncated"):
            suppressed.append(
                _suppressed_entry(finding, "no flagged reference survives in the merged tree")
            )
            continue
        kept.append(
            finding.model_copy(update={"evidence": {**finding.evidence, "merge_verified": True}})
        )

    return kept + resolved, suppressed, "ran"


def _merged_tree_finding(
    change: SymbolChange, name: str, changed_side: str, refs: list[dict[str, Any]]
) -> Finding:
    """A finding generated purely from merged-tree evidence — the recall win.

    No diff-side candidate existed (the referencing file was untouched by the
    other side's diff), but the merged tree contains a real, parse-verified
    reference to a name no file defines.
    """
    symbol = change.symbol
    pattern = PATTERN_RENAMED if change.kind == "renamed" else PATTERN_REMOVED
    if change.kind == "renamed":
        title = f"'{name}' renamed to '{symbol.name}' on {changed_side} but still referenced in the merged result"
        severity: Severity = "high"
        confidence: Confidence = "medium"
    else:
        title = f"{symbol.kind} '{name}' removed on {changed_side} is still referenced in the merged result"
        severity = "critical"
        confidence = "high"
    locations = ", ".join(f"{r['file']}:{r['line']}" for r in refs[:3])
    first_line = int(refs[0]["line"]) or None
    return Finding(
        signal=NAME,
        severity=severity,
        confidence=confidence,
        title=title,
        detail=(
            f"{changed_side} {change.kind} {symbol.kind} '{name}' ({symbol.file}:{symbol.line}) and the "
            f"merged tree still references it at {locations}. These call sites were not in the other "
            "side's diff — they exist in files neither branch touched."
        ),
        file=str(refs[0]["file"]),
        line=first_line,
        evidence={
            "pattern": pattern,
            "symbol": symbol.name,
            "symbol_kind": symbol.kind,
            "change": change.kind,
            "change_confidence": change.confidence,
            "changed_side": changed_side,
            "reference_side": "merged",
            "definition": {
                "file": symbol.file,
                "line": symbol.line,
                "end_line": symbol.end_line,
                "signature": symbol.signature,
                "parent": symbol.parent,
            },
            "references": refs,
            "reference_count": len(refs),
            "references_truncated": False,
            "merge_verified": True,
            "merged_tree_only": True,
            **({"old_name": name, "new_name": symbol.name} if change.kind == "renamed" else {}),
        },
    )


def _suppressed_entry(finding: Finding, reason: str) -> dict[str, Any]:
    """Compact record of a finding that was held back, for audit output."""
    return {
        "title": finding.title,
        "reason": reason,
        "severity": finding.severity,
        "confidence": finding.confidence,
        "pattern": finding.evidence.get("pattern"),
        "file": finding.file,
        "line": finding.line,
    }


def _repo(ctx: AnalysisContext) -> Any | None:
    """Open the analysed repository, or ``None`` when that is not possible."""
    try:
        from mergesignal.git.repo import Repo

        timeout = getattr(getattr(ctx, "config", None), "analysis", None)
        seconds = getattr(timeout, "git_timeout_seconds", None)
        repo = (
            Repo(ctx.repo_path, timeout=float(seconds))
            if isinstance(seconds, (int, float))
            else Repo(ctx.repo_path)
        )
        return repo if repo.is_repository() else None
    except Exception:  # noqa: BLE001 - verification is optional enrichment
        return None


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
