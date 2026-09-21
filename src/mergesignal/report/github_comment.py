"""Idempotent GitHub PR comment rendering. **Owned by Agent D.**

The service posts one comment per PR and *updates* it on every push rather than
piling up a new comment each time. That hinges on the hidden marker
:data:`COMMENT_MARKER`, which is embedded in every body we generate and located
again by :func:`find_existing_comment`.

Body layout:

1. Heading with base...head.
2. Summary table: four signals, status, finding count.
3. Risk score line (plain text — no emoji, no external shields image, so the
   comment renders identically on GitHub Enterprise and in email digests).
4. One ``<details>`` block per signal with findings, collapsed by default so a
   noisy report does not bury the conversation.
5. Footer with the tool version and the hidden marker.
"""

from __future__ import annotations

from typing import Any

from mergesignal.models import Report
from mergesignal.report.render import (
    evidence_details_md,
    findings_line,
    findings_table_md,
    format_timestamp,
    md_escape,
    ordered_signals,
    summary_rows_md,
)

__all__ = [
    "COMMENT_MARKER",
    "MAX_COMMENT_CHARS",
    "details_section",
    "find_existing_comment",
    "render_comment",
    "risk_badge",
    "summary_section",
    "truncate_body",
]

#: Hidden HTML comment identifying our comment. Must appear verbatim in every
#: generated body and must never change — changing it orphans every existing
#: comment and the service starts double-posting.
COMMENT_MARKER = "<!-- mergesignal:v1 -->"

#: GitHub rejects issue comment bodies above 65536 characters.
MAX_COMMENT_CHARS = 65_000

#: Note appended when a body had to be cut down to fit.
TRUNCATION_NOTE = "_Output truncated — run `mergesignal analyze` locally for the full report._"


def render_comment(
    report: Report,
    *,
    repo_slug: str | None = None,
    pr_number: int | None = None,
    verbose: bool = False,
) -> str:
    """Render the full PR comment body, marker included.

    Output is truncated to :data:`MAX_COMMENT_CHARS` at a block boundary with an
    explicit "output truncated" note, and the marker is re-appended after any
    truncation so idempotency survives.
    """
    target = _target_label(repo_slug, pr_number)
    lines: list[str] = [
        "## MergeSignal",
        "",
        f"Merging `{md_escape(report.head)}` into `{md_escape(report.base)}`{target}.",
        "",
        *summary_rows_md(report),
        "",
        risk_badge(report),
        "",
    ]

    for signal in ordered_signals(report):
        section = details_section(report, signal.name, verbose=verbose)
        if section:
            lines.extend([section, ""])

    lines.extend(
        [
            "---",
            "",
            f"<sub>MergeSignal · {findings_line(report)} · report schema {report.version}"
            f" · generated {format_timestamp(report)}</sub>",
            "",
            COMMENT_MARKER,
            "",
        ]
    )
    return truncate_body("\n".join(lines))


def _target_label(repo_slug: str | None, pr_number: int | None) -> str:
    """`` for owner/repo#12`` when we know where we are posting, else ``""``."""
    if repo_slug and pr_number is not None:
        return f" for {md_escape(repo_slug)}#{pr_number}"
    if repo_slug:
        return f" in {md_escape(repo_slug)}"
    if pr_number is not None:
        return f" for #{pr_number}"
    return ""


def find_existing_comment(
    comments: list[dict[str, Any]],
    *,
    marker: str = COMMENT_MARKER,
    bot_login: str | None = None,
) -> dict[str, Any] | None:
    """Locate our previous comment in a PR's comment list.

    :param comments: raw GitHub API issue-comment objects (each with ``id`` and
        ``body``).
    :param bot_login: when given, additionally require ``user.login`` to match,
        which guards against a human quoting our comment (marker included) in a
        reply and hijacking the update target.
    :returns: the matching comment dict, or ``None``. When several match, the
        **oldest** wins, so the comment people already linked to stays the one
        that gets updated.
    """
    matches = [c for c in comments or [] if _is_ours(c, marker, bot_login)]
    if not matches:
        return None
    if all(isinstance(c.get("created_at"), str) for c in matches):
        return min(matches, key=lambda c: c["created_at"])
    if all(isinstance(c.get("id"), int) for c in matches):
        return min(matches, key=lambda c: c["id"])
    return matches[0]


def _is_ours(comment: Any, marker: str, bot_login: str | None) -> bool:
    """``True`` when ``comment`` carries our marker and passes the author guard."""
    if not isinstance(comment, dict):
        return False
    body = comment.get("body")
    if not isinstance(body, str) or marker not in body:
        return False
    if bot_login is None:
        return True
    user = comment.get("user") or {}
    login = user.get("login") if isinstance(user, dict) else None
    return isinstance(login, str) and login.casefold() == bot_login.casefold()


def summary_section(report: Report) -> str:
    """The markdown table of the four signal statuses."""
    return "\n".join(summary_rows_md(report))


def risk_badge(report: Report) -> str:
    """Plain-text risk line, e.g. ``**Risk:** 62/100 (high)``.

    Returns an explicit "not computed" line when the risk engine did not run, so
    a missing score is never mistaken for a low one.
    """
    risk = report.risk_score
    if risk is None:
        return "**Risk:** not computed (the risk signal did not produce a score)"
    return f"**Risk:** {risk.score:.0f}/100 ({risk.level})"


def details_section(report: Report, signal_name: str, *, verbose: bool = False) -> str:
    """One collapsible ``<details>`` block for a signal.

    Returns an empty string for a signal with nothing to say, so clean reports
    stay short.
    """
    signal = report.signal(signal_name)
    if signal is None:
        return ""
    if signal.status == "ok" and not signal.findings:
        return ""

    count = len(signal.findings)
    noun = "finding" if count == 1 else "findings"
    heading = f"{md_escape(signal.name)} — {signal.status}"
    if count:
        heading += f", {count} {noun}"
    lines = ["<details>", f"<summary>{heading}</summary>", ""]
    if signal.summary:
        lines.extend([md_escape(signal.summary), ""])
    if signal.findings:
        lines.extend(findings_table_md(signal))
        lines.append("")
        for finding in sorted(signal.findings, key=lambda f: f.sort_key):
            lines.extend(evidence_details_md(finding, verbose=verbose))
    lines.append("</details>")
    return "\n".join(lines)


def truncate_body(body: str, *, limit: int = MAX_COMMENT_CHARS, marker: str = COMMENT_MARKER) -> str:
    """Cut a body to ``limit`` characters at a line boundary, keeping the marker."""
    if len(body) <= limit:
        return body
    footer = f"\n\n{TRUNCATION_NOTE}\n\n{marker}\n"
    budget = max(limit - len(footer), 0)
    head = body[:budget]
    cut = head.rfind("\n")
    if cut > 0:
        head = head[:cut]
    return head.rstrip("\n") + footer
