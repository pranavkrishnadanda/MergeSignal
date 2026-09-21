"""MergeSignal — pre-merge intelligence for git repositories.

MergeSignal answers one question before code lands: *what happens if this merges?*
It produces a :class:`~mergesignal.models.Report` containing four signals:

``conflicts``
    S1 — textual merge conflicts, predicted via ``git merge-tree`` simulation.
``semantic``
    S2 — changes that merge cleanly but break meaning (deleted/renamed/re-signatured
    symbols that the other side still references).
``overlap``
    S3 — which other open PRs / branches collide with this change.
``risk``
    S4 — a 0-100 weighted blast-radius score.

The public contract lives in :mod:`mergesignal.models`; everything else is an
implementation detail that may change between releases.
"""

from mergesignal.models import (
    REPORT_SCHEMA_VERSION,
    AnalysisContext,
    Report,
    Signal,
)

#: Distribution version; keep in sync with ``pyproject.toml``.
__version__ = "0.1.0"

__all__ = ["REPORT_SCHEMA_VERSION", "AnalysisContext", "Report", "Signal", "__version__"]
