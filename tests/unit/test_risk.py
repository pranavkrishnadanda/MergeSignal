"""Unit tests for S4 — risk scoring (:mod:`mergesignal.signals.risk`).

``make_context`` points at a plain ``tmp_path`` that is *not* a repository, so
the history-derived factors are unavailable here by construction — which is
itself one of the behaviours under test (missing factors are omitted, never
scored zero). The history path is covered in
``tests/integration/test_risk_int.py``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from mergesignal.config import Config, RiskWeights
from mergesignal.models import AnalysisContext, Diff, DiffFile, Hunk, LineRange, RiskScore
from mergesignal.signals import risk


def changed(
    path: str,
    *,
    added: int = 5,
    removed: int = 0,
    language: str | None = "python",
    binary: bool = False,
    deleted: bool = False,
) -> DiffFile:
    """Build a :class:`~mergesignal.models.DiffFile` with the given churn."""
    hunks = (
        []
        if binary
        else [
            Hunk(
                file_path=path,
                base_range=LineRange(start=1, end=1 + removed),
                head_range=LineRange(start=1, end=1 + added),
                added_lines=["+"] * added,
                removed_lines=["-"] * removed,
            )
        ]
    )
    return DiffFile(
        path=path,
        language=language,
        is_binary=binary,
        is_deleted=deleted,
        hunks=hunks,
        additions=added,
        deletions=removed,
    )


def diff(*files: DiffFile) -> Diff:
    """Build a :class:`~mergesignal.models.Diff` from ready-made files."""
    return Diff(base="main", head="feature", files=list(files))


# ------------------------------------------------------------------ skipping


def test_missing_diff_is_skipped(make_context: Callable[..., AnalysisContext]) -> None:
    signal = risk.analyze(make_context())

    assert signal.status == "skipped"
    assert "no diff" in signal.summary


def test_empty_diff_is_skipped(make_context: Callable[..., AnalysisContext]) -> None:
    signal = risk.analyze(make_context(head_diff=Diff(base="main", head="main", files=[])))

    assert signal.status == "skipped"
    assert "empty diff" in signal.summary


def test_never_raises(
    monkeypatch: pytest.MonkeyPatch, make_context: Callable[..., AnalysisContext]
) -> None:
    monkeypatch.setattr(risk, "_analyze", lambda _ctx: (_ for _ in ()).throw(RuntimeError("boom")))

    signal = risk.analyze(make_context())

    assert signal.status == "error"
    assert "RuntimeError: boom" in signal.summary


# -------------------------------------------------------------------- score


def test_score_is_reported_and_explained(make_context: Callable[..., AnalysisContext]) -> None:
    signal = risk.analyze(make_context(head_diff=diff(changed("src/a.py"))))

    score = RiskScore.model_validate(signal.metadata["risk_score"])
    assert 0.0 <= score.score <= 100.0
    assert score.level in ("low", "medium", "high", "critical")
    assert set(score.factors) == set(score.weights)
    # Factors are explainability metadata, not findings — they are risk
    # indicators, not defects, and must not trip the severity threshold.
    assert signal.findings == []
    assert set(signal.metadata["factor_evidence"]) == set(score.factors)


def test_unavailable_factors_are_omitted_not_zeroed(
    make_context: Callable[..., AnalysisContext],
) -> None:
    """A repo with no history must not silently look safe."""
    signal = risk.analyze(make_context(head_diff=diff(changed("src/a.py"))))

    assert "churn" in signal.metadata["unavailable_factors"]
    assert "co_change" in signal.metadata["unavailable_factors"]
    assert "churn" not in signal.metadata["factors"]
    assert "churn unavailable" in signal.summary or "churn," in signal.summary


def test_only_contributing_factors_get_evidence(
    make_context: Callable[..., AnalysisContext],
) -> None:
    """Every measured factor carries its value, weight and explanation."""
    ctx = make_context(head_diff=diff(changed("tests/test_a.py")))

    signal = risk.analyze(ctx)

    evidence = signal.metadata["factor_evidence"]
    assert set(evidence) == set(signal.metadata["factors"])
    assert all(entry["detail"] for entry in evidence.values())


def test_hot_path_match_raises_the_score(make_context: Callable[..., AnalysisContext]) -> None:
    hot = Config(hot_paths=["src/**/models.py"])
    cold = risk.analyze(make_context(head_diff=diff(changed("src/pkg/models.py")), config=Config()))
    warm = risk.analyze(make_context(head_diff=diff(changed("src/pkg/models.py")), config=hot))

    assert warm.metadata["factors"]["hot_paths"] == 1.0
    assert "hot_paths" not in cold.metadata["factors"]
    assert warm.metadata["score"] > cold.metadata["score"]
    assert warm.metadata["factor_evidence"]["hot_paths"]["matched"] == ["src/pkg/models.py"]


def test_risk_threshold_emits_a_single_gate_finding(
    make_context: Callable[..., AnalysisContext],
) -> None:
    """The opt-in CI gate: score >= risk_threshold produces exactly one finding."""
    gated = Config(risk_threshold=10)
    signal = risk.analyze(make_context(head_diff=diff(changed("src/a.py")), config=gated))

    (finding,) = signal.findings
    assert finding.evidence["threshold"] == 10
    assert finding.evidence["score"] >= 10
    assert finding.confidence == "high"

    open_gate = Config(risk_threshold=101)
    quiet = risk.analyze(make_context(head_diff=diff(changed("src/a.py")), config=open_gate))
    assert quiet.findings == []


def test_unconfigured_hot_paths_are_neutral(make_context: Callable[..., AnalysisContext]) -> None:
    signal = risk.analyze(make_context(head_diff=diff(changed("src/a.py"))))

    assert "hot_paths" in signal.metadata["unavailable_factors"]


def test_weights_come_from_config(make_context: Callable[..., AnalysisContext]) -> None:
    weights = RiskWeights(churn=0, co_change=0, hot_paths=0, test_coverage=1, diff_size=0)
    ctx = make_context(head_diff=diff(changed("src/a.py")), config=Config(risk_weights=weights))

    signal = risk.analyze(ctx)

    assert signal.metadata["score"] == 100.0
    assert signal.metadata["level"] == "critical"


# ------------------------------------------------------------ test proxy


def test_touching_the_test_file_removes_the_penalty(
    make_context: Callable[..., AnalysisContext],
) -> None:
    with_test = risk.analyze(
        make_context(head_diff=diff(changed("src/pkg/a.py"), changed("tests/pkg/test_a.py")))
    )
    without_test = risk.analyze(make_context(head_diff=diff(changed("src/pkg/a.py"))))

    assert with_test.metadata["factors"]["test_coverage"] == 0.0
    assert without_test.metadata["factors"]["test_coverage"] == 1.0
    assert with_test.metadata["score"] < without_test.metadata["score"]


def test_test_file_matched_by_normalised_stem(
    make_context: Callable[..., AnalysisContext],
) -> None:
    """``tests/test_oov1916.py`` covers ``src/oov_1916.py`` — separators don't matter.

    Exact-path candidate matching missed real repos where test naming differs
    by underscore/casing; stems are normalised before comparison.
    """
    signal = risk.analyze(
        make_context(
            head_diff=diff(
                changed("src/oov_1916.py"),
                changed("tests/test_oov1916.py"),
            )
        )
    )

    factor = signal.metadata["factor_evidence"]["test_coverage"]
    assert factor["value"] == 0.0
    assert factor["verdicts"] == {"src/oov_1916.py": "test changed"}


def test_test_files_are_not_their_own_denominator(
    make_context: Callable[..., AnalysisContext],
) -> None:
    signal = risk.analyze(make_context(head_diff=diff(changed("tests/unit/test_a.py"))))

    assert "test_coverage" in signal.metadata["unavailable_factors"]


def test_docs_only_change_has_no_test_factor(make_context: Callable[..., AnalysisContext]) -> None:
    signal = risk.analyze(make_context(head_diff=diff(changed("README.md", language=None))))

    assert "test_coverage" in signal.metadata["unavailable_factors"]


def test_binary_and_deleted_files_are_not_expected_to_have_tests(
    make_context: Callable[..., AnalysisContext],
) -> None:
    ctx = make_context(
        head_diff=diff(
            changed("asset.bin", language=None, binary=True), changed("src/gone.py", deleted=True)
        )
    )

    signal = risk.analyze(ctx)

    assert "test_coverage" in signal.metadata["unavailable_factors"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/pkg/mod.py", "src/pkg/test_mod.py"),
        ("src/pkg/mod.py", "tests/pkg/test_mod.py"),
        ("pkg/server.go", "pkg/server_test.go"),
        ("web/button.ts", "web/button.test.ts"),
        ("web/button.ts", "web/button.spec.ts"),
        ("src/main/java/x/Thing.java", "src/test/java/x/ThingTest.java"),
    ],
)
def test_test_path_candidates(path: str, expected: str) -> None:
    assert expected in risk.test_path_candidates(path)


def test_test_path_candidates_never_returns_the_source_itself() -> None:
    assert "tests/test_a.py" not in risk.test_path_candidates("tests/test_a.py")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/unit/test_a.py", True),
        ("src/a_test.go", True),
        ("web/button.spec.ts", True),
        ("src/main/java/x/ThingTest.java", True),
        ("__tests__/button.ts", True),
        ("src/a.py", False),
        ("src/contest.py", False),
    ],
)
def test_is_test_path(path: str, expected: bool) -> None:
    assert risk.is_test_path(path) is expected


# ------------------------------------------------------------------- size


def test_diff_size_saturates() -> None:
    small = diff(changed("a.py", added=1))
    huge_lines = diff(changed("a.py", added=risk.DIFF_SIZE_SATURATION_LINES * 2))
    many_files = diff(
        *[changed(f"m{i}.py", added=1) for i in range(risk.DIFF_SIZE_SATURATION_FILES + 10)]
    )

    assert risk.score_diff_size(small) < 0.05
    assert risk.score_diff_size(huge_lines) == 1.0
    assert risk.score_diff_size(many_files) == 1.0


def test_diff_size_components_do_not_dilute_each_other() -> None:
    """One 3000-line file is as risky by size as 60 one-line files."""
    assert risk.score_diff_size(diff(changed("a.py", added=3000))) == 1.0


# ---------------------------------------------------------------- arithmetic


@pytest.mark.parametrize(
    ("score", "level"),
    [
        (0.0, "low"),
        (29.9, "low"),
        (30.0, "medium"),
        (59.9, "medium"),
        (60.0, "high"),
        (80.0, "critical"),
        (100.0, "critical"),
    ],
)
def test_level_for(score: float, level: str) -> None:
    assert risk.level_for(score) == level


def test_build_score_rescales_around_missing_factors() -> None:
    weights = {
        "churn": 0.25,
        "co_change": 0.2,
        "hot_paths": 0.25,
        "test_coverage": 0.15,
        "diff_size": 0.15,
    }

    full = risk.build_score({"churn": 1.0, "diff_size": 1.0}, weights)
    partial = risk.build_score({"churn": 1.0, "diff_size": 0.0}, weights)

    assert full.score == 100.0
    assert partial.score == pytest.approx(100 * 0.25 / 0.40, abs=0.05)
    assert partial.weights == {"churn": 0.25, "diff_size": 0.15}


def test_build_score_with_zero_weights_is_zero() -> None:
    score = risk.build_score({"churn": 1.0}, {"churn": 0.0})

    assert score.score == 0.0
    assert score.level == "low"


def test_build_score_with_no_factors_is_zero() -> None:
    assert risk.build_score({}, {"churn": 1.0}).score == 0.0


@pytest.mark.parametrize(
    ("value", "severity"),
    [(0.1, "low"), (0.49, "low"), (0.5, "medium"), (0.79, "medium"), (0.8, "high"), (1.0, "high")],
)
def test_finding_for_factor_severity(value: float, severity: str) -> None:
    finding = risk.finding_for_factor("churn", value, 0.25, "because", {"window_days": 90})

    assert finding.severity == severity
    assert finding.signal == "risk"
    assert finding.evidence["factor"] == "churn"
    assert finding.evidence["weight"] == 0.25
    assert finding.evidence["window_days"] == 90
    assert finding.detail == "because"
