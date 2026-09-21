"""The four signal engines.

Every engine exposes exactly one entry point::

    def analyze(ctx: AnalysisContext) -> Signal

and obeys two invariants:

1. **It never raises.** Any exception — including the ``NotImplementedError``
   raised by an unfinished engine — is caught by the engine itself (or, as a
   backstop, by :func:`mergesignal.cli.run_signal`) and converted into
   ``Signal(status="error")``. One broken engine must never lose the report.
2. **Missing inputs mean "skipped", not "error".** No other branches to compare
   against, an unsupported language, an empty diff: return
   ``Signal.skipped(name, reason)``.
"""

from mergesignal.signals import conflicts, overlap, risk, semantic

#: Registry consumed by the CLI pipeline. Keys match
#: :data:`mergesignal.models.SIGNAL_NAMES` and the ``enabled_signals`` config.
REGISTRY = {
    "conflicts": conflicts.analyze,
    "semantic": semantic.analyze,
    "overlap": overlap.analyze,
    "risk": risk.analyze,
}

__all__ = ["REGISTRY", "conflicts", "overlap", "risk", "semantic"]
