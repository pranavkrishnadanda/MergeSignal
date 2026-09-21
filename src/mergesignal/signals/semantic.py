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
"""

from __future__ import annotations

from mergesignal.models import (
    AnalysisContext,
    Finding,
    Reference,
    Signal,
    SymbolChange,
)

#: Name registered in :data:`mergesignal.signals.REGISTRY`.
NAME = "semantic"

#: Names too generic to attribute confidently across files.
COMMON_NAMES: frozenset[str] = frozenset({"run", "get", "set", "main", "init", "test", "handle", "name", "value", "data"})


def analyze(ctx: AnalysisContext) -> Signal:
    """Run the semantic signal in both directions. Never raises.

    Checks base-side changes against head-side references *and* head-side
    changes against base-side references, because breakage is symmetric: either
    party can be the one who pulled the rug.

    Returns ``skipped`` when neither side produced any symbols (no supported
    language in the diff) — reporting "ok" there would be a lie.
    """
    raise NotImplementedError


def find_removed_still_referenced(changes: list[SymbolChange], references: list[Reference], *, changed_side: str, reference_side: str) -> list[Finding]:
    """Definitions removed on one side that the other side still references.

    References located inside the removed definition's own body are excluded —
    deleting a function deletes its internal recursion too.
    """
    raise NotImplementedError


def find_renamed_old_name_referenced(changes: list[SymbolChange], references: list[Reference], *, changed_side: str, reference_side: str) -> list[Finding]:
    """Renamed definitions whose *old* name is still referenced elsewhere."""
    raise NotImplementedError


def find_signature_change_new_callers(changes: list[SymbolChange], references: list[Reference], *, changed_side: str, reference_side: str) -> list[Finding]:
    """Signature changes on one side with call sites added on the other.

    Only references that the other side *added* count: a pre-existing call site
    is the author's own problem and would already be visible in their tests.
    """
    raise NotImplementedError


def arity_changed(old_signature: str | None, new_signature: str | None) -> bool | None:
    """Whether the parameter *count* changed between two signatures.

    :returns: ``True``/``False``, or ``None`` when either signature is unknown
        or unparseable — callers must treat ``None`` as "lower the confidence",
        not as ``False``.
    """
    raise NotImplementedError


def confidence_for(reference: Reference, symbol_file: str, name: str) -> str:
    """Grade how sure we are that ``reference`` really resolves to ``name``.

    Same file -> ``high``; same package/directory -> ``medium``; elsewhere or a
    name in :data:`COMMON_NAMES` -> ``low``.
    """
    raise NotImplementedError
