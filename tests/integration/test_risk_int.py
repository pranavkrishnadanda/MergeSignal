"""Integration tests for S4 against real repositories with real git history.

The history-derived factors (churn, co-change) need commits inside the config's
lookback window, so these repositories are built with a *recent* clock rather
than RepoBuilder's default fixed date.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mergesignal.analysis.diff import diff_refs
from mergesignal.config import AnalysisConfig, Config, RiskWeights
from mergesignal.git.repo import Repo
from mergesignal.models import AnalysisContext, RiskScore
from tests.helpers.repo_builder import RepoBuilder

from mergesignal.signals import risk  # isort: skip

pytestmark = pytest.mark.integration

#: Recent enough that every commit falls inside the default 90-day window.
RECENT = datetime.now(UTC) - timedelta(days=10)


def context(
    path: Path, *, base: str = "main", head: str = "feature", config: Config | None = None
) -> AnalysisContext:
    """Candidate context: S4 needs the head diff plus the repository itself."""
    config = config or Config()
    repo = Repo(path)
    merge_base = repo.merge_base(base, head)
    return AnalysisContext(
        repo_path=str(path),
        base=base,
        head=head,
        merge_base=merge_base,
        head_diff=diff_refs(repo, merge_base or base, head, max_files=config.analysis.max_files),
        config=config,
    )


@pytest.fixture
def recent_builder(tmp_path: Path) -> RepoBuilder:
    """A builder whose commits land inside the history window."""
    return RepoBuilder(tmp_path / "recent", start_time=RECENT)


def test_churn_factor_uses_real_history(recent_builder: RepoBuilder) -> None:
    recent_builder.file("src/hot.py", "def f():\n    return 0\n").commit("seed")
    for i in range(1, 7):
        recent_builder.file("src/hot.py", f"def f():\n    return {i}\n").commit(f"churn {i}")
    recent_builder.branch("feature")
    path = (
        recent_builder.file("src/hot.py", "def f():\n    return 99\n").commit("candidate").build()
    )

    signal = risk.analyze(context(path))

    assert "churn" not in signal.metadata["unavailable_factors"]
    assert signal.metadata["factors"]["churn"] > 0
    (finding,) = [f for f in signal.findings if f.evidence["factor"] == "churn"]
    assert finding.evidence["per_path"]["src/hot.py"] >= 6
    assert finding.evidence["window_days"] == 90


def test_co_change_flags_a_coupled_file_missing_from_the_diff(recent_builder: RepoBuilder) -> None:
    recent_builder.file("src/a.py", "A = 0\n").file("src/b.py", "B = 0\n").commit("seed")
    for i in range(1, 4):
        recent_builder.file("src/a.py", f"A = {i}\n").file("src/b.py", f"B = {i}\n").commit(
            f"pair {i}"
        )
    recent_builder.branch("feature")
    path = recent_builder.file("src/a.py", "A = 99\n").commit("touch only a").build()

    signal = risk.analyze(context(path))

    assert signal.metadata["factors"]["co_change"] == 1.0
    (finding,) = [f for f in signal.findings if f.evidence["factor"] == "co_change"]
    assert finding.evidence["missing_partners"]["src/a.py"] == ["src/b.py"]


def test_history_outside_the_window_leaves_factors_unavailable(builder: RepoBuilder) -> None:
    """RepoBuilder's default clock is years old: no data, so no score, not zero."""
    path = builder.scenario_clean_merge().build()

    signal = risk.analyze(context(path))

    assert {"churn", "co_change"} <= set(signal.metadata["unavailable_factors"])


def test_existing_but_untouched_test_file_is_a_partial_penalty(recent_builder: RepoBuilder) -> None:
    recent_builder.file("src/app.py", "def run_it():\n    return 1\n")
    recent_builder.file("tests/test_app.py", "def test_run_it():\n    assert True\n").commit("seed")
    recent_builder.branch("feature")
    path = (
        recent_builder.file("src/app.py", "def run_it():\n    return 2\n")
        .commit("change app only")
        .build()
    )

    signal = risk.analyze(context(path))

    factor = signal.metadata["factors"]["test_coverage"]
    assert factor == risk.UNTOUCHED_TEST_PENALTY
    (finding,) = [f for f in signal.findings if f.evidence["factor"] == "test_coverage"]
    assert finding.evidence["tree_scanned"] is True
    assert finding.evidence["verdicts"]["src/app.py"] == "test exists but untouched"


def test_touching_the_test_file_clears_the_penalty(recent_builder: RepoBuilder) -> None:
    recent_builder.file("src/app.py", "def run_it():\n    return 1\n")
    recent_builder.file("tests/test_app.py", "def test_run_it():\n    assert True\n").commit("seed")
    recent_builder.branch("feature")
    recent_builder.file("src/app.py", "def run_it():\n    return 2\n")
    path = (
        recent_builder.file("tests/test_app.py", "def test_run_it():\n    assert True  # updated\n")
        .commit("app and test")
        .build()
    )

    signal = risk.analyze(context(path))

    assert signal.metadata["factors"]["test_coverage"] == 0.0


def test_hot_paths_from_config(recent_builder: RepoBuilder) -> None:
    recent_builder.file("src/pkg/models.py", "X = 1\n").file("src/pkg/util.py", "Y = 1\n").commit(
        "seed"
    )
    recent_builder.branch("feature")
    recent_builder.file("src/pkg/models.py", "X = 2\n")
    path = recent_builder.file("src/pkg/util.py", "Y = 2\n").commit("touch both").build()

    config = Config(hot_paths=["src/**/models.py"])
    signal = risk.analyze(context(path, config=config))

    assert signal.metadata["factors"]["hot_paths"] == 0.5
    (finding,) = [f for f in signal.findings if f.evidence["factor"] == "hot_paths"]
    assert finding.evidence["matched"] == ["src/pkg/models.py"]


def test_empty_diff_is_skipped(simple_repo: Path) -> None:
    signal = risk.analyze(context(simple_repo, base="main", head="main"))

    assert signal.status == "skipped"
    assert "empty diff" in signal.summary


def test_score_is_reproducible_and_explainable(recent_builder: RepoBuilder) -> None:
    recent_builder.file("src/app.py", "def run_it():\n    return 1\n").commit("seed")
    recent_builder.branch("feature")
    path = (
        recent_builder.file("src/app.py", "def run_it():\n    return 2\n").commit("change").build()
    )

    ctx = context(path)
    first = risk.analyze(ctx)
    second = risk.analyze(ctx)

    assert first.metadata["risk_score"] == second.metadata["risk_score"]
    score = RiskScore.model_validate(first.metadata["risk_score"])
    assert score.level == risk.level_for(score.score)
    assert set(score.factors) == set(score.weights)
    # Every scored factor with a non-zero value is explained by exactly one finding.
    explained = {f.evidence["factor"] for f in first.findings}
    assert explained == {name for name, value in score.factors.items() if value > 0}


def test_history_window_is_configurable(recent_builder: RepoBuilder) -> None:
    recent_builder.file("src/a.py", "A = 0\n").commit("seed")
    recent_builder.branch("feature")
    path = recent_builder.file("src/a.py", "A = 1\n").commit("change").build()

    config = Config(analysis=AnalysisConfig(history_days=1), risk_weights=RiskWeights())
    signal = risk.analyze(context(path, config=config))

    assert "churn" in signal.metadata["unavailable_factors"]
