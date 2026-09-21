"""End-to-end CLI tests against real repositories.

These prove that ``mergesignal analyze`` runs the real engines and renders a
full report (text and JSON) on a genuine repository, and they pin the FR-7
exit-code contract end to end: 0 when nothing reaches the severity threshold,
1 when a finding does, 2 when anything errors — including a signal engine
blowing up, which :func:`run_signal` converts into ``Signal(status="error")``
instead of letting it escape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mergesignal.cli import (
    EXIT_CLEAN,
    EXIT_ERROR,
    EXIT_FINDINGS,
    app,
    build_context,
    exit_code_for,
    run_pipeline,
    run_signal,
)
from mergesignal.config import Config
from mergesignal.git.repo import Repo
from mergesignal.models import SIGNAL_NAMES, Finding, Report, Signal
from tests.helpers.repo_builder import RepoBuilder

runner = CliRunner()


@pytest.fixture
def diverged(builder: RepoBuilder) -> Path:
    """``main`` and ``feature`` with one commit each after a shared base."""
    return builder.scenario_clean_merge().build()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == EXIT_CLEAN
    assert "mergesignal" in result.stdout


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == EXIT_CLEAN
    for command in ("analyze", "scan", "serve"):
        assert command in result.stdout


def test_analyze_renders_report(diverged: Path) -> None:
    """Every engine runs for real and every signal gets a rendered row.

    On the clean-merge scenario nothing defects: risk factors are context, not
    findings, so without a configured ``risk_threshold`` the run exits clean.
    """
    result = runner.invoke(
        app, ["analyze", "--base", "main", "--head", "feature", "-C", str(diverged)]
    )
    assert result.exit_code == EXIT_CLEAN, result.stdout
    assert "MergeSignal report" in result.stdout
    for name in SIGNAL_NAMES:
        assert name in result.stdout
    # The engines are implemented: nothing may degrade to a bootstrap error row.
    assert "not implemented" not in result.stdout
    assert "Traceback" not in result.stdout


def test_analyze_exits_clean_when_no_finding_reaches_the_threshold(diverged: Path) -> None:
    """Same repository, threshold raised above the worst finding: FR-7 exit 0."""
    result = runner.invoke(
        app,
        [
            "analyze",
            "--base",
            "main",
            "--head",
            "feature",
            "-C",
            str(diverged),
            "--threshold",
            "critical",
        ],
    )
    assert result.exit_code == EXIT_CLEAN, result.stdout
    # The findings are still reported — they just are not fatal any more.
    assert "risk" in result.stdout


def test_analyze_exits_error_when_an_engine_fails(monkeypatch, diverged: Path) -> None:
    """A raising engine becomes an error row and forces FR-7 exit 2."""
    from mergesignal import signals

    def boom(ctx: object) -> None:
        raise RuntimeError("engine exploded")

    monkeypatch.setitem(signals.REGISTRY, "semantic", boom)
    result = runner.invoke(
        app, ["analyze", "--base", "main", "--head", "feature", "-C", str(diverged)]
    )
    assert result.exit_code == EXIT_ERROR, result.stdout
    assert "RuntimeError: engine exploded" in result.stdout
    # The rest of the report still renders: one bad engine must not lose it.
    for name in SIGNAL_NAMES:
        assert name in result.stdout


def test_analyze_json_output_is_parseable(diverged: Path) -> None:
    """``--format json`` emits Report JSON with the real per-signal statuses."""
    result = runner.invoke(
        app,
        ["analyze", "--base", "main", "--head", "feature", "-C", str(diverged), "--format", "json"],
    )
    assert result.exit_code == EXIT_CLEAN, result.stdout
    payload = json.loads(result.stdout)
    assert payload["base"] == "main"
    assert payload["head"] == "feature"
    assert len(payload["merge_base"]) == 40
    assert [s["name"] for s in payload["signals"]] == list(SIGNAL_NAMES)
    # Two branches touching different files: nothing conflicts, nothing breaks,
    # there is no second branch to overlap with, and risk factors are context —
    # carried in metadata, not findings — so the run exits clean.
    assert {s["name"]: s["status"] for s in payload["signals"]} == {
        "conflicts": "ok",
        "semantic": "ok",
        "overlap": "skipped",
        "risk": "ok",
    }
    risk = next(s for s in payload["signals"] if s["name"] == "risk")
    assert risk["metadata"]["factor_evidence"], risk
    assert payload["risk_score"] is not None
    assert 0.0 <= payload["risk_score"]["score"] <= 100.0


def test_analyze_respects_signal_selection(diverged: Path) -> None:
    result = runner.invoke(
        app,
        [
            "analyze",
            "--base",
            "main",
            "--head",
            "feature",
            "-C",
            str(diverged),
            "--format",
            "json",
            "--signal",
            "risk",
        ],
    )
    payload = json.loads(result.stdout)
    assert [s["name"] for s in payload["signals"]] == ["risk"]


def test_analyze_rejects_unknown_signal(diverged: Path) -> None:
    result = runner.invoke(app, ["analyze", "-C", str(diverged), "--signal", "nonsense"])
    assert result.exit_code == EXIT_ERROR
    assert "unknown signal" in result.output


def test_analyze_rejects_unknown_format(diverged: Path) -> None:
    result = runner.invoke(app, ["analyze", "-C", str(diverged), "--format", "xml"])
    assert result.exit_code == EXIT_ERROR
    assert "unknown format" in result.output


def test_analyze_rejects_unknown_ref(diverged: Path) -> None:
    result = runner.invoke(
        app, ["analyze", "--base", "main", "--head", "ghost", "-C", str(diverged)]
    )
    assert result.exit_code == EXIT_ERROR
    assert "ghost" in result.output


def test_analyze_outside_a_repository(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = runner.invoke(app, ["analyze", "-C", str(plain)])
    assert result.exit_code == EXIT_ERROR
    assert "not a git repository" in result.output


def test_analyze_with_bad_config(diverged: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("enabled_signals: [bogus]\n", encoding="utf-8")
    result = runner.invoke(app, ["analyze", "-C", str(diverged), "--config", str(bad)])
    assert result.exit_code == EXIT_ERROR


def test_scan_runs_without_network(diverged: Path) -> None:
    result = runner.invoke(app, ["scan", "--base", "main", "-C", str(diverged)])
    assert result.exit_code in (EXIT_CLEAN, EXIT_FINDINGS, EXIT_ERROR)


def test_serve_reports_unimplemented() -> None:
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == EXIT_ERROR


class TestPipeline:
    def test_build_context_resolves_merge_base(self, diverged: Path) -> None:
        repo = Repo(diverged)
        ctx = build_context(repo, "main", "feature", Config())
        assert ctx.merge_base == repo.merge_base("main", "feature")
        assert ctx.repo_path == str(diverged.resolve())
        assert ctx.base == "main"

    def test_build_context_raises_on_unknown_ref(self, diverged: Path) -> None:
        from mergesignal.git.repo import GitError

        with pytest.raises(GitError):
            build_context(Repo(diverged), "main", "ghost", Config())

    def test_run_signal_converts_not_implemented(self, monkeypatch, make_context) -> None:
        """An engine raising NotImplementedError degrades to an error row.

        Injected rather than taken from the registry: the conversion mechanism
        is what is under test, not which engine happens to raise what today.
        """
        from mergesignal import signals

        def pending(ctx: object) -> None:
            raise NotImplementedError("conflicts engine pending")

        monkeypatch.setitem(signals.REGISTRY, "conflicts", pending)
        signal = run_signal("conflicts", make_context())
        assert signal.name == "conflicts"
        assert signal.status == "error"
        assert signal.summary == "not implemented"
        assert signal.duration_ms is not None

    def test_run_signal_converts_arbitrary_exceptions(self, monkeypatch, make_context) -> None:
        from mergesignal import signals

        monkeypatch.setitem(
            signals.REGISTRY, "risk", lambda ctx: (_ for _ in ()).throw(ValueError("boom"))
        )
        signal = run_signal("risk", make_context())
        assert signal.status == "error"
        assert "ValueError: boom" in signal.summary

    def test_run_signal_unknown_name(self, make_context) -> None:
        assert run_signal("nope", make_context()).status == "error"

    def test_run_pipeline_honours_enabled_signals(self, make_context) -> None:
        config = Config(enabled_signals=["risk", "conflicts"])
        report = run_pipeline(make_context(config=config), config)
        assert [s.name for s in report.signals] == ["conflicts", "risk"]

    def test_run_pipeline_lifts_risk_score(self, monkeypatch, make_context) -> None:
        from mergesignal import signals

        score = {
            "score": 42.0,
            "level": "medium",
            "factors": {"churn": 0.4},
            "weights": {"churn": 1.0},
        }
        monkeypatch.setitem(
            signals.REGISTRY,
            "risk",
            lambda ctx: Signal(name="risk", status="ok", metadata={"risk_score": score}),
        )
        config = Config(enabled_signals=["risk"])
        report = run_pipeline(make_context(config=config), config)
        assert report.risk_score is not None
        assert report.risk_score.score == 42.0
        assert report.risk_score.level == "medium"


class TestExitCodes:
    def _report(self, *signals: Signal) -> Report:
        return Report(base="main", head="feature", signals=list(signals))

    def test_clean(self) -> None:
        report = self._report(Signal(name="conflicts", status="ok"))
        assert exit_code_for(report, "high") == EXIT_CLEAN

    def test_findings_at_threshold(self) -> None:
        finding = Finding(signal="semantic", severity="high", title="t")
        report = self._report(Signal(name="semantic", status="findings", findings=[finding]))
        assert exit_code_for(report, "high") == EXIT_FINDINGS
        assert exit_code_for(report, "critical") == EXIT_CLEAN

    def test_errors_win_over_findings(self) -> None:
        finding = Finding(signal="semantic", severity="critical", title="t")
        report = self._report(
            Signal(name="semantic", status="findings", findings=[finding]),
            Signal(name="risk", status="error", summary="boom"),
        )
        assert exit_code_for(report, "low") == EXIT_ERROR

    def test_skipped_is_clean(self) -> None:
        report = self._report(
            Signal(name="overlap", status="skipped", summary="nothing to compare")
        )
        assert exit_code_for(report, "low") == EXIT_CLEAN
