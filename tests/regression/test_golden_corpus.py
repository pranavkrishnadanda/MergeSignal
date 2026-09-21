"""Golden-snapshot regression tests over the scenario corpus.

Each test builds a real git repository from a
:class:`~tests.regression.scenarios.Scenario` script, runs the full pipeline,
normalises the report and compares it to the committed JSON snapshot. A diff
means MergeSignal's *output* changed — which is either a bug or a decision, but
never an accident.

Run ``pytest tests/regression --snapshot-update`` to regenerate, then review
``git diff tests/regression/snapshots`` line by line.

Alongside the snapshot comparison, each scenario carries a handful of explicit
assertions about the thing it exists to prove. That redundancy is deliberate: a
snapshot tells you *something* moved, while the explicit assertion tells you
*what* was supposed to hold. Regenerating a snapshot cannot silently erase the
intent the way it can erase the value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mergesignal.cli import build_context, collect_others
from mergesignal.git.repo import Repo
from mergesignal.models import SIGNAL_NAMES, Report
from tests.regression.scenarios import (
    BY_NAME,
    SCENARIOS,
    Scenario,
    normalize_report,
    read_snapshot,
    write_snapshot,
)

pytestmark = [pytest.mark.regression, pytest.mark.integration]


@pytest.fixture
def run_scenario(tmp_path: Path):
    """Build and analyse a scenario, returning ``(report, normalised, repo_path)``."""

    def _run(scenario: Scenario) -> tuple[Report, dict, Path]:
        repo_path = scenario.build(tmp_path / scenario.name)
        report = scenario.run(repo_path)
        return report, normalize_report(report, repo_path), repo_path

    return _run


# ---------------------------------------------------------------- the corpus


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_report_matches_snapshot(scenario: Scenario, run_scenario, snapshot_update: bool) -> None:
    """The normalised report equals its committed golden file."""
    _, actual, _ = run_scenario(scenario)

    if snapshot_update:
        write_snapshot(scenario, actual)
        pytest.skip(f"snapshot for {scenario.name!r} regenerated")

    try:
        expected = read_snapshot(scenario)
    except FileNotFoundError:  # pragma: no cover - only before first generation
        pytest.fail(
            f"no snapshot for {scenario.name!r}; run: pytest tests/regression --snapshot-update"
        )

    assert actual == expected, (
        f"{scenario.name}: {scenario.description}\n"
        "The report no longer matches its golden snapshot. If the change is intended, run\n"
        "  pytest tests/regression --snapshot-update\n"
        "and review the diff."
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_normalisation_is_stable(scenario: Scenario, tmp_path: Path) -> None:
    """Building and analysing the same scenario twice normalises identically.

    Guards the snapshots themselves: if any residual non-determinism leaked past
    :func:`~tests.regression.scenarios.normalize_report`, the corpus would
    flake in CI instead of catching real regressions. Two *separate* repository
    directories are used so that anything path- or sha-derived shows up here.
    """
    first_repo = scenario.build(tmp_path / "first")
    second_repo = scenario.build(tmp_path / "second")
    first = normalize_report(scenario.run(first_repo), first_repo)
    second = normalize_report(scenario.run(second_repo), second_repo)
    assert first == second


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_no_engine_errored(scenario: Scenario, run_scenario) -> None:
    """No scenario in the corpus may leave an engine in ``status='error'``.

    An ``error`` row means an exception escaped into
    :func:`~mergesignal.cli.run_signal`'s trap. The corpus is the integration
    surface, so a crash anywhere in the five subsystems lands here.
    """
    report, _, _ = run_scenario(scenario)
    errored = {s.name: s.summary for s in report.signals if s.status == "error"}
    assert not errored, f"{scenario.name}: engines errored: {errored}"
    assert [s.name for s in report.signals] == list(SIGNAL_NAMES)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_snapshot_is_scrubbed(scenario: Scenario, run_scenario) -> None:
    """The committed snapshot leaks neither the temp path nor a real sha."""
    _, actual, repo_path = run_scenario(scenario)
    blob = json.dumps(actual)
    assert str(repo_path) not in blob
    assert "generated_at" not in blob or "<generated-at>" in blob


# ------------------------------------------------------------ context wiring


def test_build_context_populates_both_sides(tmp_path: Path) -> None:
    """``AnalysisContext`` carries each side's *own* symbols, refs and changes.

    This is the seam the whole corpus rests on, pinned directly so a failure
    says "the context is half-empty" instead of "eight snapshots changed".

    The original wiring indexed both sides over the *head* diff's paths, which
    left ``base_changes`` empty and filled ``base_references`` with merge-base
    references. Every engine kept working and every unit test kept passing; S2
    simply stopped finding anything in one of its two directions.
    """
    scenario = BY_NAME["rename_vs_new_caller"]
    repo_path = scenario.build(tmp_path / "ctx")
    config = scenario.config()
    repo = Repo(str(repo_path), timeout=config.analysis.git_timeout_seconds)
    ctx = build_context(repo, scenario.base, scenario.head, config)

    # head renamed load_settings -> read_settings in settings.py
    assert {c.symbol.file for c in ctx.head_changes} == {"settings.py"}
    assert any(c.kind == "renamed" and c.old_name == "load_settings" for c in ctx.head_changes)

    # base added startup.py, which references the old name
    assert {c.symbol.file for c in ctx.base_changes} == {"startup.py"}
    assert "load_settings" in {r.name for r in ctx.base_references}

    # Each side's symbols describe that side's tree, not the merge base's.
    assert "read_settings" in {s.name for s in ctx.head_symbols}
    assert "read_settings" not in {s.name for s in ctx.base_symbols}


def test_branch_diffs_carry_changed_symbols_only(tmp_path: Path) -> None:
    """``BranchDiff.symbols`` holds what a peer *changed*, not all it declares.

    Returning every declaration in the touched files would make any two edits to
    one module look like a collision on every symbol in it, collapsing S3's
    symbol > hunk > file ranking into "symbol, always".
    """
    scenario = BY_NAME["overlap_three_branches"]
    repo_path = scenario.build(tmp_path / "branches")
    config = scenario.config()
    repo = Repo(str(repo_path), timeout=config.analysis.git_timeout_seconds)
    others = collect_others(repo, config, branches=scenario.branches, prs=None, base=scenario.base)

    by_name = {other.name: other for other in others}
    # peer-symbol re-signatured alpha: exactly that one symbol, not beta/gamma.
    assert {s.name for s in by_name["peer-symbol"].symbols} == {"alpha"}
    # peer-hunk only rewrote a body of equal length, which is no symbol change.
    assert by_name["peer-hunk"].symbols == []
    # peer-file touched no source file at all.
    assert by_name["peer-file"].symbols == []


# ------------------------------------------------- per-scenario expectations


def test_clean_merge_finds_nothing_alarming(run_scenario) -> None:
    report, _, _ = run_scenario(BY_NAME["clean_merge"])
    assert report.signal("conflicts").status == "ok"
    assert report.signal("semantic").status == "ok"
    # Risk always has something to say about diff size; it must not be critical.
    assert report.max_severity != "critical"


def test_textual_conflict_reports_exactly_two_regions(run_scenario) -> None:
    """Two separated edits on both sides produce two regions, not one file."""
    report, _, _ = run_scenario(BY_NAME["textual_conflict"])
    conflicts = report.signal("conflicts")
    assert conflicts.status == "findings"
    assert len(conflicts.findings) == 2
    assert {f.file for f in conflicts.findings} == {"settings.conf"}
    # Distinct regions, so distinct primary lines.
    assert len({f.line for f in conflicts.findings}) == 2


def test_rename_vs_new_caller_is_a_clean_but_broken_merge(run_scenario) -> None:
    """git is happy; MergeSignal is not. This is the product's whole thesis."""
    report, _, _ = run_scenario(BY_NAME["rename_vs_new_caller"])
    assert report.signal("conflicts").status == "ok", "the merge must be textually clean"

    semantic = report.signal("semantic")
    assert semantic.status == "findings"
    patterns = {f.evidence.get("pattern") for f in semantic.findings}
    assert "renamed_old_name_referenced" in patterns

    finding = next(
        f for f in semantic.findings if f.evidence.get("pattern") == "renamed_old_name_referenced"
    )
    assert finding.evidence["old_name"] == "load_settings"
    assert finding.evidence["new_name"] == "read_settings"
    assert finding.file == "startup.py"
    # Evidence must carry *both* sides, per FR-4.
    assert finding.evidence["definition"]["file"] == "settings.py"
    assert finding.evidence["references"]


def test_signature_change_flags_the_new_callers(run_scenario) -> None:
    report, _, _ = run_scenario(BY_NAME["signature_change_new_callers"])
    assert report.signal("conflicts").status == "ok"

    semantic = report.signal("semantic")
    assert semantic.status == "findings"
    finding = next(
        f for f in semantic.findings if f.evidence.get("pattern") == "signature_changed_new_callers"
    )
    assert finding.evidence["symbol"] == "render"
    assert finding.evidence["signature_breakage"] == "breaking"
    assert finding.severity == "high"
    # Every newly added usage is evidence: the import plus both call sites.
    assert {ref["file"] for ref in finding.evidence["references"]} == {"pages.py"}
    call_lines = {
        ref["line"] for ref in finding.evidence["references"] if "render(" in ref["context"]
    }
    assert len(call_lines) == 2, finding.evidence["references"]
    assert finding.evidence["reference_count"] == len(finding.evidence["references"])
    # Only lines the *other* side added count as new callers.
    assert finding.evidence["new_callers_only"] is True


def test_overlap_ranks_colliding_branches_and_ignores_the_rest(run_scenario) -> None:
    """FR-5: symbol > hunk > file, and a branch sharing no file stays silent."""
    report, _, _ = run_scenario(BY_NAME["overlap_three_branches"])
    overlap = report.signal("overlap")
    assert overlap.status == "findings"

    by_branch = {f.evidence["branch"]: f for f in overlap.findings}
    assert "peer-file" not in by_branch, "a branch sharing no file must not be reported"
    assert set(by_branch) == {"peer-symbol", "peer-hunk"}

    # Both branches touch core.py, but only one touches the same declaration.
    assert by_branch["peer-symbol"].evidence["granularity"] == "symbol"
    assert by_branch["peer-symbol"].evidence["symbols"] == ["alpha"]
    assert by_branch["peer-hunk"].evidence["granularity"] == "hunk"
    assert by_branch["peer-hunk"].evidence["symbols"] == []

    # Ranking puts the sharper collision first, and the summary says so.
    assert [f.evidence["branch"] for f in overlap.findings] == ["peer-symbol", "peer-hunk"]
    assert "symbol level" in overlap.summary

    from mergesignal.models import SEVERITY_ORDER

    assert (
        SEVERITY_ORDER[by_branch["peer-symbol"].severity]
        >= SEVERITY_ORDER[by_branch["peer-hunk"].severity]
    )


def test_unsupported_language_skips_rather_than_claiming_ok(run_scenario) -> None:
    """NFR-3: degrade, never crash — and never pretend to have looked."""
    report, _, _ = run_scenario(BY_NAME["unsupported_language"])
    semantic = report.signal("semantic")
    assert semantic.status == "skipped"
    assert semantic.summary, "a skip must explain itself"
    assert report.signal("conflicts").status == "ok"


def test_binary_conflict_has_no_line_detail(run_scenario) -> None:
    report, _, _ = run_scenario(BY_NAME["binary_file"])
    conflicts = report.signal("conflicts")
    assert conflicts.status == "findings"
    finding = conflicts.findings[0]
    assert finding.file == "logo.png"
    assert finding.evidence["binary"] is True
    # No text and no line detail invented for bytes git itself refuses to diff.
    assert finding.line is None
    assert "ours_text" not in finding.evidence
    assert "theirs_text" not in finding.evidence
    assert finding.evidence["ours_range"] == [0, 0]
    assert report.signal("semantic").status == "skipped"


def test_empty_diff_skips_everything(run_scenario) -> None:
    report, _, _ = run_scenario(BY_NAME["empty_diff"])
    assert report.max_severity is None
    assert not report.all_findings
    for name in ("semantic", "overlap", "risk"):
        assert report.signal(name).status == "skipped", name
    # Conflicts can honestly answer "already merged" rather than skipping.
    assert report.signal("conflicts").status in ("ok", "skipped")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_history_factors_are_measured(scenario: Scenario, run_scenario) -> None:
    """churn and co_change must be *measured* in every scored scenario.

    This is the corpus-wide tripwire for the blind spot it was written to fix:
    every scenario repo originally started at a fixed 2024-01-01, which aged
    out of the risk engine's ``--since=<history_days>`` window, so both
    history-derived factors reported ``unavailable`` in all eight snapshots —
    a regression in :func:`mergesignal.git.history.collect_history` would have
    produced green tests. ``Scenario.start_time`` now defaults to inside the
    window, and this test fails loudly if the corpus ever drifts out again.
    """
    report, _, _ = run_scenario(scenario)
    risk = report.signal("risk")
    if risk.status == "skipped":
        pytest.skip("an empty diff carries no history question")
    assert risk.status != "error", risk.summary
    assert "churn" in risk.metadata["factors"], f"{scenario.name}: churn unavailable"
    assert "co_change" in risk.metadata["factors"], f"{scenario.name}: co_change unavailable"


def test_optional_param_widening_produces_no_finding(run_scenario) -> None:
    """A compatible signature change must not be reported as breakage.

    This is the false-positive class that dominated the first real-repo audit:
    ``render(template) -> render(template, context=None)`` breaks no caller.
    """
    report, _, _ = run_scenario(BY_NAME["optional_param_widening"])
    semantic = report.signal("semantic")
    assert semantic.status != "error", semantic.summary
    assert semantic.findings == []


def test_same_name_in_another_module_is_suppressed(run_scenario) -> None:
    """The merged tree still defines ``helper`` — the reference resolves."""
    report, _, _ = run_scenario(BY_NAME["same_name_different_module"])
    semantic = report.signal("semantic")
    assert semantic.status != "error", semantic.summary
    assert semantic.findings == []
    suppressed = semantic.metadata["suppressed"]
    assert suppressed, "the match should be visible in the audit trail"
    assert any("merged tree" in entry["reason"] for entry in suppressed)


def test_merge_erased_reference_produces_no_finding(run_scenario) -> None:
    """Both the definition *and* the referencing file are merged away."""
    report, _, _ = run_scenario(BY_NAME["merge_erased_reference"])
    semantic = report.signal("semantic")
    assert semantic.status != "error", semantic.summary
    assert semantic.findings == []


def test_untouched_caller_still_breaks(run_scenario) -> None:
    """Recall: the merged-tree pass finds callers neither diff indexed.

    ``old_caller.py`` is untouched since the merge base, so no side index saw
    the reference — but the merged tree has a call to a name nothing defines.
    """
    report, _, _ = run_scenario(BY_NAME["untouched_caller_breaks"])
    assert report.signal("conflicts").status == "ok", "the merge must be textually clean"

    semantic = report.signal("semantic")
    (finding,) = semantic.findings
    assert finding.evidence["symbol"] == "helper"
    assert finding.evidence["merge_verified"] is True
    assert finding.evidence["merged_tree_only"] is True
    assert {r["file"] for r in finding.evidence["references"]} == {"old_caller.py"}
    assert finding.severity == "critical"


def test_hot_history_scores_real_churn_and_coupling(run_scenario) -> None:
    """The history factors must produce *nonzero* values, not merely exist.

    ``hot_history`` commits ``api.py`` and ``api_client.py`` together three
    times, then changes ``api.py`` alone: churn registers ~3 commits/file and
    co_change must flag ``api_client.py`` as a coupled partner missing from the
    diff — the exact "you forgot its partner" evidence S4 exists to carry.
    Factors are score metadata, not findings: a churned file is context, not a
    defect.
    """
    report, _, _ = run_scenario(BY_NAME["hot_history"])
    risk = report.signal("risk")
    assert risk.status != "error", risk.summary
    # No risk_threshold is configured, so the score must not masquerade as findings.
    assert risk.findings == []

    factors = risk.metadata["factors"]
    assert factors["churn"] > 0, "in-window history must register"
    assert factors["co_change"] > 0, "the coupled partner must be flagged"

    co_change = risk.metadata["factor_evidence"]["co_change"]
    assert co_change["missing_partners"] == {"api.py": ["api_client.py"]}
