"""Report renderers: text, json, markdown. **Owned by Agent D.**

Three formats, one rule: **deterministic output**. Findings are sorted by
:attr:`mergesignal.models.Finding.sort_key` (severity desc, then file, then
line) before rendering, so two runs over the same repository produce
byte-identical output and the golden corpus can diff it.

``text``
    Rich console output for a terminal: one section per signal, severity-coloured
    finding lines, a summary footer with the risk score. Honours ``NO_COLOR`` and
    non-TTY stdout by degrading to plain text.
``json``
    ``Report.model_dump_json`` — the machine contract. Stable key order, ISO-8601
    timestamps, no ANSI.
``md``
    Markdown for pasting into a PR: a heading per signal, a table of findings,
    and ``<details>`` blocks for verbose evidence.
"""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import UTC
from typing import Any

from rich.console import Console
from rich.markup import escape

from mergesignal.models import (
    SEVERITY_ORDER,
    SIGNAL_NAMES,
    Finding,
    OutputFormat,
    Report,
    Signal,
)

__all__ = [
    "evidence_details_md",
    "findings_line",
    "findings_table_md",
    "format_evidence",
    "format_timestamp",
    "location",
    "md_escape",
    "ordered_signals",
    "render",
    "render_json",
    "render_markdown",
    "render_text",
    "severity_counts",
    "severity_style",
    "sorted_findings",
    "status_symbol",
    "summary_rows_md",
    "summary_table",
]

#: Fixed console width — renderer output must not depend on the terminal size,
#: or two runs of the same report would produce different bytes.
CONSOLE_WIDTH = 100

_SEVERITY_STYLES: dict[str, str] = {
    "critical": "bold red",
    "high": "red",
    "medium": "yellow",
    "low": "cyan",
}
_DEFAULT_SEVERITY_STYLE = "white"

_STATUS_SYMBOLS: dict[str, str] = {
    "ok": "[ok]",
    "findings": "[!!]",
    "skipped": "[--]",
    "error": "[XX]",
}
_UNKNOWN_STATUS_SYMBOL = "[??]"

_STATUS_STYLES: dict[str, str] = {
    "ok": "green",
    "findings": "yellow",
    "skipped": "dim",
    "error": "bold red",
}

#: Severities in the order the summary footer counts them (worst first).
_SEVERITIES_WORST_FIRST: tuple[str, ...] = tuple(
    sorted(SEVERITY_ORDER, key=lambda s: -SEVERITY_ORDER[s])
)


# --------------------------------------------------------------- entry point


def render(
    report: Report,
    fmt: OutputFormat = "text",
    *,
    color: bool | None = None,
    verbose: bool = False,
) -> str:
    """Render ``report`` in the requested format and return the string.

    :param color: force colour on/off for the ``text`` format; ``None`` means
        auto-detect (TTY and ``NO_COLOR``).
    :param verbose: include full evidence dumps rather than summaries.
    :raises ValueError: unknown format.

    The CLI prints the returned string; renderers must not write to stdout
    themselves, so that the service can reuse them.
    """
    if fmt == "text":
        return render_text(report, color=color, verbose=verbose)
    if fmt == "json":
        return render_json(report)
    if fmt in ("md", "markdown"):
        return render_markdown(report, verbose=verbose)
    raise ValueError(f"unknown output format {fmt!r}; expected one of 'text', 'json', 'md'")


# -------------------------------------------------------------- shared parts


def sorted_findings(signal: Signal) -> list[Finding]:
    """Findings sorted deterministically — every renderer must go through this."""
    return sorted(signal.findings, key=lambda f: f.sort_key)


def severity_style(severity: str) -> str:
    """Rich style string for a severity ('bold red' for critical, ...)."""
    return _SEVERITY_STYLES.get(severity, _DEFAULT_SEVERITY_STYLE)


def status_symbol(status: str) -> str:
    """Short ASCII marker for a signal status, used in summary tables.

    ASCII only — CI logs and Windows terminals mangle box-drawing and emoji.
    """
    return _STATUS_SYMBOLS.get(status, _UNKNOWN_STATUS_SYMBOL)


def summary_table(report: Report) -> list[tuple[str, str, int, str]]:
    """Rows of ``(signal, status, finding_count, summary)`` in SIGNAL_NAMES order.

    Shared by the text, markdown and GitHub-comment renderers so the three can
    never drift apart. Signals absent from the report (engine disabled in
    config) are omitted rather than invented; unknown signal names are appended
    after the canonical four in report order.
    """
    return [(s.name, s.status, len(s.findings), s.summary) for s in ordered_signals(report)]


def ordered_signals(report: Report) -> list[Signal]:
    """Report signals in canonical :data:`SIGNAL_NAMES` order, extras last."""
    by_name = {s.name: s for s in report.signals}
    ordered = [by_name[name] for name in SIGNAL_NAMES if name in by_name]
    ordered.extend(s for s in report.signals if s.name not in SIGNAL_NAMES)
    return ordered


def severity_counts(report: Report) -> dict[str, int]:
    """Count of findings per severity, worst first, zeros included."""
    counts = dict.fromkeys(_SEVERITIES_WORST_FIRST, 0)
    for finding in report.all_findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts


def format_evidence(evidence: dict[str, Any], *, verbose: bool = False, max_lines: int = 20) -> str:
    """Render a finding's evidence dict as readable indented text.

    Truncates to ``max_lines`` with an explicit "... N more" marker unless
    ``verbose``; a conflict region's text can be thousands of lines long.
    """
    if not evidence:
        return ""
    lines: list[str] = []
    for key in sorted(evidence, key=str):
        lines.extend(_evidence_lines(str(key), evidence[key]))
    if not verbose and len(lines) > max_lines:
        hidden = len(lines) - max_lines
        suffix = "" if hidden == 1 else "s"
        lines = [*lines[:max_lines], f"... {hidden} more line{suffix}"]
    return "\n".join(lines)


def _evidence_lines(key: str, value: Any, indent: int = 0) -> list[str]:
    """One evidence entry as indented ``key: value`` lines (recursive)."""
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{pad}{key}: {{}}"]
        lines = [f"{pad}{key}:"]
        for sub in sorted(value, key=str):
            lines.extend(_evidence_lines(str(sub), value[sub], indent + 2))
        return lines
    if isinstance(value, (list, tuple)):
        if not value:
            return [f"{pad}{key}: []"]
        lines = [f"{pad}{key}:"]
        for item in value:
            if isinstance(item, (dict, list, tuple)):
                lines.append(f"{pad}  - {json.dumps(item, sort_keys=True, default=str)}")
            else:
                lines.append(f"{pad}  - {_scalar(item)}")
        return lines
    if isinstance(value, str) and "\n" in value:
        lines = [f"{pad}{key}:"]
        lines.extend(f"{pad}  {line}" for line in value.rstrip("\n").split("\n"))
        return lines
    return [f"{pad}{key}: {_scalar(value)}"]


def _scalar(value: Any) -> str:
    """Stable single-line rendering of a JSON-primitive evidence value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def location(finding: Finding) -> str:
    """``file:line`` / ``file`` / ``-`` for a finding's primary location."""
    if finding.file is None:
        return "-"
    if finding.line is None:
        return finding.file
    return f"{finding.file}:{finding.line}"


def format_timestamp(report: Report) -> str:
    """UTC timestamp of the report, formatted for humans."""
    return report.generated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _risk_line(report: Report) -> str:
    """Plain-text risk summary used by the text and markdown footers."""
    risk = report.risk_score
    if risk is None:
        return "Risk: not computed"
    return f"Risk: {risk.score:.0f}/100 ({risk.level})"


def findings_line(report: Report) -> str:
    """``3 findings (critical 0, high 1, ...)`` footer line."""
    counts = severity_counts(report)
    total = sum(counts.values())
    noun = "finding" if total == 1 else "findings"
    breakdown = ", ".join(f"{sev} {counts[sev]}" for sev in _SEVERITIES_WORST_FIRST)
    return f"{total} {noun} ({breakdown})"


# ---------------------------------------------------------------------- text


def _want_color(color: bool | None) -> bool:
    """Resolve the colour tri-state: explicit wins, else TTY and no ``NO_COLOR``."""
    if color is not None:
        return color
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def render_text(report: Report, *, color: bool | None = None, verbose: bool = False) -> str:
    """Rich-formatted console report."""
    use_color = _want_color(color)
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        width=CONSOLE_WIDTH,
        force_terminal=use_color,
        no_color=not use_color,
        color_system="truecolor" if use_color else None,
        highlight=False,
        emoji=False,
        soft_wrap=True,
        legacy_windows=False,
    )

    console.print("[bold]MergeSignal report[/bold]")
    console.print(f"base: {escape(report.base)} -> head: {escape(report.head)}")
    console.print(f"merge-base: {escape(report.merge_base or '(none)')}")
    console.print(f"generated: {format_timestamp(report)}")

    rows = summary_table(report)
    console.print("")
    console.print("[bold]Summary[/bold]")
    if not rows:
        console.print("  (no signals ran)")
    else:
        name_width = max(len(name) for name, _, _, _ in rows)
        for name, status, count, summary in rows:
            noun = "finding " if count == 1 else "findings"
            marker = escape(status_symbol(status))
            style = _STATUS_STYLES.get(status, "white")
            text = f"  [{style}]{marker}[/{style}] {escape(name):<{name_width}}  {count:>3} {noun}"
            if summary:
                text += f"  {escape(summary)}"
            console.print(text)

    for signal in ordered_signals(report):
        console.print("")
        style = _STATUS_STYLES.get(signal.status, "white")
        header = f"[bold]{escape(signal.name)}[/bold]: [{style}]{signal.status}[/{style}]"
        if signal.summary:
            header += f" - {escape(signal.summary)}"
        console.print(header)
        findings = sorted_findings(signal)
        if not findings:
            if signal.status == "ok":
                console.print("  no findings")
            continue
        for finding in findings:
            sev_style = severity_style(finding.severity)
            console.print(
                f"  [{sev_style}]{finding.severity.upper()}[/{sev_style}]"
                f" ({finding.confidence} confidence) {escape(finding.title)}"
            )
            if finding.file is not None:
                console.print(f"      at {escape(location(finding))}")
            for line in finding.detail.splitlines():
                console.print(f"      {escape(line)}")
            evidence = format_evidence(finding.evidence, verbose=verbose)
            if evidence:
                console.print("      evidence:")
                for line in evidence.split("\n"):
                    console.print(f"        {escape(line)}")

    console.print("")
    console.print(f"[bold]{_risk_line(report)}[/bold]")
    console.print(findings_line(report))
    worst = report.max_severity
    if worst is not None:
        console.print(f"worst severity: [{severity_style(worst)}]{worst}[/{severity_style(worst)}]")
    if report.has_errors:
        console.print("[bold red]one or more signals failed to run[/bold red]")

    body = "\n".join(line.rstrip() for line in buffer.getvalue().split("\n"))
    return body.strip("\n") + "\n"


# ---------------------------------------------------------------------- json


def render_json(report: Report, *, indent: int | None = 2) -> str:
    """Canonical JSON. ``indent=None`` produces one compact line for piping."""
    findings_sorted = report.model_copy(
        update={
            "signals": [
                signal.model_copy(update={"findings": sorted_findings(signal)})
                for signal in ordered_signals(report)
            ]
        }
    )
    return findings_sorted.model_dump_json(indent=indent)


# ------------------------------------------------------------------ markdown


def md_escape(text: str) -> str:
    """Escape a string for use inside a markdown **table cell**."""
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def _md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """A GitHub-flavoured markdown table as a list of lines."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def summary_rows_md(report: Report) -> list[str]:
    """The shared four-column summary table, as markdown lines."""
    rows = [
        [
            f"`{md_escape(name)}`",
            f"`{status_symbol(status)}` {status}",
            str(count),
            md_escape(summary) or "-",
        ]
        for name, status, count, summary in summary_table(report)
    ]
    if not rows:
        return ["_No signals ran._"]
    return _md_table(["Signal", "Status", "Findings", "Summary"], rows)


def findings_table_md(signal: Signal) -> list[str]:
    """Markdown table of a signal's findings, worst first."""
    rows = [
        [
            finding.severity,
            finding.confidence,
            f"`{md_escape(location(finding))}`",
            md_escape(finding.title),
        ]
        for finding in sorted_findings(signal)
    ]
    return _md_table(["Severity", "Confidence", "Location", "Finding"], rows)


def evidence_details_md(finding: Finding, *, verbose: bool = False) -> list[str]:
    """Collapsible ``<details>`` block with a finding's detail text + evidence."""
    evidence = format_evidence(finding.evidence, verbose=verbose)
    if not finding.detail and not evidence:
        return []
    lines = [
        "<details>",
        f"<summary>{md_escape(finding.title)} — {md_escape(location(finding))}</summary>",
        "",
    ]
    if finding.detail:
        lines.extend(finding.detail.rstrip("\n").split("\n"))
        lines.append("")
    if evidence:
        lines.extend(["```text", *evidence.split("\n"), "```", ""])
    lines.extend(["</details>", ""])
    return lines


def render_markdown(report: Report, *, verbose: bool = False) -> str:
    """Markdown report suitable for a PR description or a comment."""
    lines: list[str] = [
        "# MergeSignal report",
        "",
        f"**Base:** `{md_escape(report.base)}` &rarr; **Head:** `{md_escape(report.head)}`",
        "",
        f"**Merge base:** `{md_escape(report.merge_base or '(none)')}`",
        "",
        f"**Generated:** {format_timestamp(report)}",
        "",
        "## Summary",
        "",
        *summary_rows_md(report),
        "",
        f"**{_risk_line(report)}**",
        "",
    ]

    for signal in ordered_signals(report):
        lines.extend([f"## {md_escape(signal.name)}", ""])
        status_line = f"_Status: {signal.status}_"
        if signal.summary:
            status_line = f"_Status: {signal.status} — {md_escape(signal.summary)}_"
        lines.extend([status_line, ""])
        findings = sorted_findings(signal)
        if not findings:
            lines.extend(["No findings.", ""])
            continue
        lines.extend(findings_table_md(signal))
        lines.append("")
        for finding in findings:
            lines.extend(evidence_details_md(finding, verbose=verbose))

    lines.extend(
        [
            "---",
            "",
            f"{findings_line(report)} · report schema {report.version}",
            "",
        ]
    )
    return "\n".join(lines)
