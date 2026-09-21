"""Unit tests for the three report renderers. **Owned by Agent D.**

Snapshot style: the expected output is spelled out in full, so a change in
renderer shape shows up as a readable diff rather than a boolean. If a snapshot
here changes, that is a deliberate output change — update it knowingly, the
regression corpus depends on this shape.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest

from mergesignal.models import Finding, Report, RiskScore, Signal
from mergesignal.report.render import (
    format_evidence,
    location,
    ordered_signals,
    render,
    render_json,
    render_markdown,
    render_text,
    severity_style,
    sorted_findings,
    status_symbol,
    summary_table,
)

FIXED_TIME = datetime(2024, 5, 4, 12, 30, tzinfo=UTC)


def build_report() -> Report:
    """The fixture report every snapshot in this file (and the comment tests) uses.

    It deliberately covers all four signal statuses, three severities, findings
    supplied *out of order*, multi-line detail text, multi-line evidence and a
    populated risk score. Signals are listed in the wrong order too, so the
    renderers' canonical re-ordering is exercised.
    """
    conflicts = Signal(
        name="conflicts",
        status="findings",
        summary="1 file, 2 regions conflict",
        findings=[
            Finding(
                signal="conflicts",
                severity="high",
                confidence="high",
                title="Conflict in src/app.py",
                detail="Both sides edited the same lines.",
                file="src/app.py",
                line=12,
                evidence={"ours": "return 1\nreturn 2", "theirs": "return 3", "regions": 2},
            ),
            Finding(
                signal="conflicts",
                severity="critical",
                confidence="high",
                title="Conflict in README.md",
                file="README.md",
                line=3,
            ),
        ],
        metadata={"strategy": "merge-tree"},
    )
    semantic = Signal(
        name="semantic",
        status="findings",
        summary="1 potential break",
        findings=[
            Finding(
                signal="semantic",
                severity="medium",
                confidence="low",
                title="removed symbol still referenced",
                detail="`helper` was removed on head.",
                file="lib/util.py",
                line=42,
                evidence={"symbol": "helper", "refs": ["lib/a.py:3", "lib/b.py:9"]},
            )
        ],
    )
    overlap = Signal(name="overlap", status="skipped", summary="no other branches supplied")
    risk = Signal(name="risk", status="ok", summary="score 62/100")
    return Report(
        base="main",
        head="feature/x",
        merge_base="a" * 40,
        signals=[risk, overlap, semantic, conflicts],
        risk_score=RiskScore(
            score=61.7, level="high", factors={"churn": 0.8}, weights={"churn": 1.0}
        ),
        generated_at=FIXED_TIME,
    )


@pytest.fixture
def report() -> Report:
    return build_report()


EXPECTED_TEXT = """\
MergeSignal report
base: main -> head: feature/x
merge-base: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
generated: 2024-05-04 12:30:00 UTC

Summary
  [!!] conflicts    2 findings  1 file, 2 regions conflict
  [!!] semantic     1 finding   1 potential break
  [--] overlap      0 findings  no other branches supplied
  [ok] risk         0 findings  score 62/100

conflicts: findings - 1 file, 2 regions conflict
  CRITICAL (high confidence) Conflict in README.md
      at README.md:3
  HIGH (high confidence) Conflict in src/app.py
      at src/app.py:12
      Both sides edited the same lines.
      evidence:
        ours:
          return 1
          return 2
        regions: 2
        theirs: return 3

semantic: findings - 1 potential break
  MEDIUM (low confidence) removed symbol still referenced
      at lib/util.py:42
      `helper` was removed on head.
      evidence:
        refs:
          - lib/a.py:3
          - lib/b.py:9
        symbol: helper

overlap: skipped - no other branches supplied

risk: ok - score 62/100
  no findings

Risk: 62/100 (high)
3 findings (critical 1, high 1, medium 1, low 0)
worst severity: critical
"""


EXPECTED_MARKDOWN = """\
# MergeSignal report

**Base:** `main` &rarr; **Head:** `feature/x`

**Merge base:** `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`

**Generated:** 2024-05-04 12:30:00 UTC

## Summary

| Signal | Status | Findings | Summary |
| --- | --- | --- | --- |
| `conflicts` | `[!!]` findings | 2 | 1 file, 2 regions conflict |
| `semantic` | `[!!]` findings | 1 | 1 potential break |
| `overlap` | `[--]` skipped | 0 | no other branches supplied |
| `risk` | `[ok]` ok | 0 | score 62/100 |

**Risk: 62/100 (high)**

## conflicts

_Status: findings — 1 file, 2 regions conflict_

| Severity | Confidence | Location | Finding |
| --- | --- | --- | --- |
| critical | high | `README.md:3` | Conflict in README.md |
| high | high | `src/app.py:12` | Conflict in src/app.py |

<details>
<summary>Conflict in src/app.py — src/app.py:12</summary>

Both sides edited the same lines.

```text
ours:
  return 1
  return 2
regions: 2
theirs: return 3
```

</details>

## semantic

_Status: findings — 1 potential break_

| Severity | Confidence | Location | Finding |
| --- | --- | --- | --- |
| medium | low | `lib/util.py:42` | removed symbol still referenced |

<details>
<summary>removed symbol still referenced — lib/util.py:42</summary>

`helper` was removed on head.

```text
refs:
  - lib/a.py:3
  - lib/b.py:9
symbol: helper
```

</details>

## overlap

_Status: skipped — no other branches supplied_

No findings.

## risk

_Status: ok — score 62/100_

No findings.

---

3 findings (critical 1, high 1, medium 1, low 0) · report schema 1
"""


# ---------------------------------------------------------------- dispatching


def test_render_dispatches_to_each_format(report: Report) -> None:
    assert render(report, "text", color=False) == render_text(report, color=False)
    assert render(report, "json") == render_json(report)
    assert render(report, "md") == render_markdown(report)


def test_render_rejects_unknown_format(report: Report) -> None:
    with pytest.raises(ValueError, match="unknown output format"):
        render(report, "yaml")  # type: ignore[arg-type]


# ---------------------------------------------------------------------- text


def test_render_text_snapshot(report: Report) -> None:
    assert render_text(report, color=False) == EXPECTED_TEXT


def test_render_text_is_ascii_and_ansi_free_without_color(report: Report) -> None:
    out = render_text(report, color=False)
    assert "\x1b[" not in out
    assert all(line == line.rstrip() for line in out.splitlines())


def test_render_text_emits_ansi_when_colour_forced(report: Report) -> None:
    assert "\x1b[" in render_text(report, color=True)


def test_render_text_honours_no_color_env(report: Report, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdout", _Tty())
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b[" not in render_text(report)
    monkeypatch.delenv("NO_COLOR")
    assert "\x1b[" in render_text(report)


def test_render_text_without_tty_is_plain(report: Report, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert "\x1b[" not in render_text(report)


def test_render_text_reports_engine_failure(report: Report) -> None:
    broken = report.model_copy(
        update={"signals": [Signal.error("risk", "tree-sitter exploded")], "risk_score": None}
    )
    out = render_text(broken, color=False)
    assert "risk: error - tree-sitter exploded" in out
    assert "Risk: not computed" in out
    assert "one or more signals failed to run" in out


def test_render_text_empty_report_is_still_renderable() -> None:
    empty = Report(base="main", head="main", generated_at=FIXED_TIME)
    out = render_text(empty, color=False)
    assert "(no signals ran)" in out
    assert "0 findings (critical 0, high 0, medium 0, low 0)" in out


# ---------------------------------------------------------------------- json


def test_render_json_is_parseable_and_sorted(report: Report) -> None:
    payload = json.loads(render_json(report))
    assert [s["name"] for s in payload["signals"]] == ["conflicts", "semantic", "overlap", "risk"]
    conflicts = payload["signals"][0]
    assert [f["severity"] for f in conflicts["findings"]] == ["critical", "high"]
    assert payload["generated_at"] == "2024-05-04T12:30:00Z"
    assert payload["version"] == "1"
    assert payload["risk_score"]["score"] == pytest.approx(61.7)


def test_render_json_key_order_is_model_field_order(report: Report) -> None:
    payload = json.loads(render_json(report), object_pairs_hook=list)
    top_level = [key for key, _ in payload]
    assert top_level == [
        "base",
        "head",
        "merge_base",
        "signals",
        "risk_score",
        "repo_path",
        "generated_at",
        "version",
    ]


def test_render_json_compact_mode_is_one_line(report: Report) -> None:
    compact = render_json(report, indent=None)
    assert "\n" not in compact
    assert json.loads(compact) == json.loads(render_json(report))


def test_render_json_does_not_mutate_the_report(report: Report) -> None:
    before = [f.severity for f in report.signals[-1].findings]
    render_json(report)
    assert [f.severity for f in report.signals[-1].findings] == before


# ------------------------------------------------------------------ markdown


def test_render_markdown_snapshot(report: Report) -> None:
    assert render_markdown(report) == EXPECTED_MARKDOWN


def test_render_markdown_has_collapsible_evidence(report: Report) -> None:
    out = render_markdown(report)
    assert out.count("<details>") == out.count("</details>") == 2
    assert "```text" in out


def test_render_markdown_escapes_table_pipes() -> None:
    finding = Finding(signal="semantic", severity="low", title="a | b", file="x|y.py")
    rpt = Report(
        base="main",
        head="f",
        signals=[Signal(name="semantic", status="findings", findings=[finding])],
        generated_at=FIXED_TIME,
    )
    rows = [line for line in render_markdown(rpt).splitlines() if line.startswith("| low ")]
    assert rows == ["| low | medium | `x\\|y.py` | a \\| b |"]


# ------------------------------------------------------------------- helpers


def test_sorted_findings_orders_by_severity_then_file_then_line() -> None:
    def make(severity: str, file: str, line: int) -> Finding:
        return Finding(signal="s", severity=severity, title=f"{file}:{line}", file=file, line=line)

    signal = Signal(
        name="semantic",
        status="findings",
        findings=[
            make("low", "a.py", 1),
            make("critical", "z.py", 9),
            make("high", "b.py", 4),
            make("high", "a.py", 7),
            make("high", "a.py", 2),
        ],
    )
    assert [f.title for f in sorted_findings(signal)] == [
        "z.py:9",
        "a.py:2",
        "a.py:7",
        "b.py:4",
        "a.py:1",
    ]


def test_summary_table_uses_canonical_signal_order(report: Report) -> None:
    assert summary_table(report) == [
        ("conflicts", "findings", 2, "1 file, 2 regions conflict"),
        ("semantic", "findings", 1, "1 potential break"),
        ("overlap", "skipped", 0, "no other branches supplied"),
        ("risk", "ok", 0, "score 62/100"),
    ]


def test_summary_table_omits_absent_signals_and_appends_unknown_ones() -> None:
    rpt = Report(
        base="b",
        head="h",
        signals=[
            Signal(name="custom", status="ok", summary="plugin"),
            Signal(name="risk", status="ok", summary="cheap"),
        ],
        generated_at=FIXED_TIME,
    )
    assert [row[0] for row in summary_table(rpt)] == ["risk", "custom"]


def test_status_symbols_are_ascii_and_distinct() -> None:
    symbols = [status_symbol(s) for s in ("ok", "findings", "skipped", "error")]
    assert len(set(symbols)) == 4
    assert all(s.isascii() for s in symbols)
    assert status_symbol("who-knows").isascii()


def test_severity_style_covers_every_severity() -> None:
    styles = {severity_style(s) for s in ("low", "medium", "high", "critical")}
    assert len(styles) == 4
    assert severity_style("critical") == "bold red"
    assert severity_style("nonsense")  # never empty


def test_location_formats_file_line_and_missing_file() -> None:
    assert location(Finding(signal="s", severity="low", title="t", file="a.py", line=3)) == "a.py:3"
    assert location(Finding(signal="s", severity="low", title="t", file="a.py")) == "a.py"
    assert location(Finding(signal="s", severity="low", title="t")) == "-"


def test_format_evidence_is_empty_for_empty_dict() -> None:
    assert format_evidence({}) == ""


def test_format_evidence_renders_nested_structures_sorted() -> None:
    out = format_evidence({"b": {"y": 2, "x": 1}, "a": [1, 2], "c": True, "d": None, "e": []})
    assert out == "a:\n  - 1\n  - 2\nb:\n  x: 1\n  y: 2\nc: true\nd: null\ne: []"


def test_format_evidence_truncates_unless_verbose() -> None:
    evidence = {"lines": [str(i) for i in range(50)]}
    truncated = format_evidence(evidence, max_lines=5)
    assert truncated.splitlines()[-1] == "... 46 more lines"
    assert len(truncated.splitlines()) == 6
    assert "... more" not in format_evidence(evidence, verbose=True)
    assert len(format_evidence(evidence, verbose=True).splitlines()) == 51


def test_ordered_signals_is_stable_for_shuffled_input(report: Report) -> None:
    shuffled = report.model_copy(update={"signals": list(reversed(report.signals))})
    assert [s.name for s in ordered_signals(shuffled)] == [
        s.name for s in ordered_signals(report)
    ]


# -------------------------------------------------------------- determinism


@pytest.mark.parametrize("fmt", ["text", "json", "md"])
def test_output_is_byte_identical_across_runs(report: Report, fmt: str) -> None:
    assert render(report, fmt, color=False) == render(report, fmt, color=False)  # type: ignore[arg-type]


@pytest.mark.parametrize("fmt", ["text", "json", "md"])
def test_output_is_independent_of_input_ordering(report: Report, fmt: str) -> None:
    shuffled_signals = list(reversed(report.signals))
    shuffled = report.model_copy(
        update={
            "signals": [
                s.model_copy(update={"findings": list(reversed(s.findings))})
                for s in shuffled_signals
            ]
        }
    )
    assert render(shuffled, fmt, color=False) == render(report, fmt, color=False)  # type: ignore[arg-type]
