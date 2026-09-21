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

from typing import Any

from mergesignal.models import Finding, OutputFormat, Report, Signal


def render(report: Report, fmt: OutputFormat = "text", *, color: bool | None = None, verbose: bool = False) -> str:
    """Render ``report`` in the requested format and return the string.

    :param color: force colour on/off for the ``text`` format; ``None`` means
        auto-detect (TTY and ``NO_COLOR``).
    :param verbose: include full evidence dumps rather than summaries.
    :raises ValueError: unknown format.

    The CLI prints the returned string; renderers must not write to stdout
    themselves, so that the service can reuse them.
    """
    raise NotImplementedError


def render_text(report: Report, *, color: bool | None = None, verbose: bool = False) -> str:
    """Rich-formatted console report."""
    raise NotImplementedError


def render_json(report: Report, *, indent: int | None = 2) -> str:
    """Canonical JSON. ``indent=None`` produces one compact line for piping."""
    raise NotImplementedError


def render_markdown(report: Report, *, verbose: bool = False) -> str:
    """Markdown report suitable for a PR description or a comment."""
    raise NotImplementedError


def sorted_findings(signal: Signal) -> list[Finding]:
    """Findings sorted deterministically — every renderer must go through this."""
    raise NotImplementedError


def severity_style(severity: str) -> str:
    """Rich style string for a severity ('bold red' for critical, ...)."""
    raise NotImplementedError


def status_symbol(status: str) -> str:
    """Short ASCII marker for a signal status, used in summary tables.

    ASCII only — CI logs and Windows terminals mangle box-drawing and emoji.
    """
    raise NotImplementedError


def format_evidence(evidence: dict[str, Any], *, verbose: bool = False, max_lines: int = 20) -> str:
    """Render a finding's evidence dict as readable indented text.

    Truncates to ``max_lines`` with an explicit "... N more" marker unless
    ``verbose``; a conflict region's text can be thousands of lines long.
    """
    raise NotImplementedError


def summary_table(report: Report) -> list[tuple[str, str, int, str]]:
    """Rows of ``(signal, status, finding_count, summary)`` in SIGNAL_NAMES order.

    Shared by the text, markdown and GitHub-comment renderers so the three can
    never drift apart.
    """
    raise NotImplementedError
