"""S1 — textual conflict prediction. **Owned by Agent E.**

Wraps :mod:`mergesignal.git.merge_sim`: simulate the merge, then emit one
:class:`~mergesignal.models.Finding` per conflicted region.

Severity policy (the contract renderers and the exit code depend on):

* a conflicted region in a text file -> ``high``
* a binary / add-add / modify-delete whole-file conflict -> ``critical``
  (a human cannot resolve it by reading a diff)
* a clean merge -> ``status="ok"``, no findings
* already up to date -> ``status="ok"`` with ``summary`` saying so

Confidence is always ``"high"``: unlike the semantic signal, this is git's own
verdict, not a heuristic.
"""

from __future__ import annotations

from mergesignal.models import AnalysisContext, ConflictRegion, Finding, Signal

#: Name registered in :data:`mergesignal.signals.REGISTRY`.
NAME = "conflicts"


def analyze(ctx: AnalysisContext) -> Signal:
    """Run the conflict signal. Never raises.

    ``metadata`` must carry at least ``conflicted_files`` (int),
    ``regions`` (int) and ``strategy`` (which simulation path ran), because the
    regression snapshots assert on them.
    """
    raise NotImplementedError


def finding_for_region(region: ConflictRegion, *, base: str, head: str) -> Finding:
    """Turn one :class:`~mergesignal.models.ConflictRegion` into a Finding.

    ``evidence`` carries the ours/theirs line ranges and a truncated excerpt of
    each side's text (capped so a 5000-line conflict does not produce a 5000-line
    JSON report). Binary regions carry ``{"binary": true}`` and no excerpt.
    """
    raise NotImplementedError


def summarize(regions: list[ConflictRegion], files: list[str]) -> str:
    """One-line human summary, e.g. ``"3 conflicted regions across 2 files"``.

    Pluralises correctly and reports files with no extractable regions
    separately, so a binary conflict is never silently dropped from the count.
    """
    raise NotImplementedError
