"""S4 — risk / blast-radius score. **Owned by Agent E.**

Produces a 0-100 :class:`~mergesignal.models.RiskScore` (FR-6) from five
configurable factors, each normalised to 0.0-1.0 before weighting so that the
weights in ``.mergesignal.yaml`` mean what they look like they mean:

``churn``
    How much the touched paths have been changing lately (hot code breaks).
``co_change``
    How many files historically move together with the touched ones but are
    *not* in this diff — the "you forgot to update its partner" factor.
``hot_paths``
    Whether the diff touches a path matching ``config.hot_paths``.
``test_coverage``
    Proxy only: does each changed source file have a plausible sibling or
    mirrored test file, and did the diff touch it? Absence is a risk signal,
    never a claim about real coverage.
``diff_size``
    Files touched and lines churned, saturating (a 400-file diff is not twice as
    risky as a 200-file one).

Every factor emits its own :class:`~mergesignal.models.Finding` so the score is
**explainable**: a number nobody can decompose is a number nobody trusts.

Severity of the overall score is bucketed by :data:`RISK_LEVELS`.
"""

from __future__ import annotations

from mergesignal.models import AnalysisContext, Diff, Finding, RiskScore, Severity, Signal

#: Name registered in :data:`mergesignal.signals.REGISTRY`.
NAME = "risk"

#: Score thresholds (inclusive lower bound) -> severity bucket.
RISK_LEVELS: tuple[tuple[float, Severity], ...] = (
    (80.0, "critical"),
    (60.0, "high"),
    (30.0, "medium"),
    (0.0, "low"),
)

#: Diff size above which the diff_size factor saturates at 1.0.
DIFF_SIZE_SATURATION_LINES = 2000

#: File count above which the diff_size factor saturates at 1.0.
DIFF_SIZE_SATURATION_FILES = 50


def analyze(ctx: AnalysisContext) -> Signal:
    """Compute the risk score and its explanation. Never raises.

    Populates ``metadata["risk_score"]`` with the
    :class:`~mergesignal.models.RiskScore` dump; the CLI lifts it onto
    ``Report.risk_score``. Returns ``skipped`` for an empty diff.

    Factors whose inputs are unavailable (no git history, history walk timed
    out) are **omitted** from the weighted average rather than scored zero, so a
    shallow clone does not silently make everything look safe.
    """
    raise NotImplementedError


def score_churn(ctx: AnalysisContext) -> float | None:
    """Normalised recent-churn factor, or ``None`` when history is unavailable."""
    raise NotImplementedError


def score_co_change(ctx: AnalysisContext) -> float | None:
    """Normalised coupling factor: coupled files *missing* from the diff."""
    raise NotImplementedError


def score_hot_paths(ctx: AnalysisContext) -> float | None:
    """Fraction of changed files matching ``config.hot_paths`` globs.

    Returns ``None`` when no hot paths are configured, so an unconfigured repo
    is not penalised or rewarded.
    """
    raise NotImplementedError


def score_test_coverage(ctx: AnalysisContext) -> float | None:
    """Proxy factor: changed source files with no corresponding test change.

    Uses :func:`test_path_candidates`. Returns ``None`` when the diff contains no
    source files at all (docs-only change).
    """
    raise NotImplementedError


def score_diff_size(diff: Diff) -> float:
    """Saturating size factor from file count and churned lines."""
    raise NotImplementedError


def test_path_candidates(path: str) -> list[str]:
    """Plausible test-file paths for a source path.

    Covers the common conventions: ``tests/test_<name>.py``, ``<dir>/test_<name>.py``,
    ``<name>_test.go``, ``<name>.test.ts``, ``<name>.spec.ts``, and the mirrored
    ``src/x/y.py`` -> ``tests/x/test_y.py`` layout. Returns an ordered list of
    candidates; the caller checks which exist in the repository or the diff.
    """
    raise NotImplementedError


def level_for(score: float) -> Severity:
    """Bucket a 0-100 score into a severity using :data:`RISK_LEVELS`."""
    raise NotImplementedError


def build_score(factors: dict[str, float], weights: dict[str, float]) -> RiskScore:
    """Combine normalised factors and weights into a :class:`RiskScore`.

    Normalises by the sum of the weights of the factors actually present, so a
    missing factor rescales rather than deflates the result.
    """
    raise NotImplementedError


def finding_for_factor(name: str, value: float, weight: float, detail: str, evidence: dict) -> Finding:
    """One explainability finding per contributing factor.

    Severity is ``low`` below 0.5, ``medium`` below 0.8, ``high`` above — a
    factor's finding describes *that factor*, not the overall score.
    """
    raise NotImplementedError
