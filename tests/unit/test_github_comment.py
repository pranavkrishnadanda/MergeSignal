"""Unit tests for the idempotent GitHub PR comment. **Owned by Agent D.**

The important property here is not prettiness but *round-tripping*: whatever we
render must be findable again by :func:`find_existing_comment`, including after
truncation, or the service starts spamming a new comment on every push.
"""

from __future__ import annotations

from typing import Any

import pytest

from mergesignal.models import Finding, Report, RiskScore, Signal
from mergesignal.report.github_comment import (
    COMMENT_MARKER,
    MAX_COMMENT_CHARS,
    details_section,
    find_existing_comment,
    render_comment,
    risk_badge,
    summary_section,
    truncate_body,
)
from tests.unit.test_render import FIXED_TIME, build_report


@pytest.fixture
def report() -> Report:
    return build_report()


def comment(
    cid: int, body: str, login: str = "mergesignal[bot]", created: str | None = None
) -> dict[str, Any]:
    """A minimal GitHub issue-comment object."""
    payload: dict[str, Any] = {"id": cid, "body": body, "user": {"login": login}}
    if created is not None:
        payload["created_at"] = created
    return payload


EXPECTED_COMMENT = """\
## MergeSignal

Merging `feature/x` into `main` for acme/widgets#12.

| Signal | Status | Findings | Summary |
| --- | --- | --- | --- |
| `conflicts` | `[!!]` findings | 2 | 1 file, 2 regions conflict |
| `semantic` | `[!!]` findings | 1 | 1 potential break |
| `overlap` | `[--]` skipped | 0 | no other branches supplied |
| `risk` | `[ok]` ok | 0 | score 62/100 |

**Risk:** 62/100 (high)

<details>
<summary>conflicts — findings, 2 findings</summary>

1 file, 2 regions conflict

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

</details>

<details>
<summary>semantic — findings, 1 finding</summary>

1 potential break

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

</details>

<details>
<summary>overlap — skipped</summary>

no other branches supplied

</details>

---

<sub>MergeSignal · 3 findings (critical 1, high 1, medium 1, low 0) · report schema 1 · generated 2024-05-04 12:30:00 UTC</sub>

<!-- mergesignal:v1 -->
"""


# ------------------------------------------------------------------ rendering


def test_render_comment_snapshot(report: Report) -> None:
    assert render_comment(report, repo_slug="acme/widgets", pr_number=12) == EXPECTED_COMMENT


def test_render_comment_carries_the_marker_exactly_once(report: Report) -> None:
    body = render_comment(report)
    assert body.count(COMMENT_MARKER) == 1


def test_render_comment_is_deterministic(report: Report) -> None:
    assert render_comment(report) == render_comment(report)


def test_render_comment_target_label_variants(report: Report) -> None:
    assert "for acme/widgets#12." in render_comment(report, repo_slug="acme/widgets", pr_number=12)
    assert "in acme/widgets." in render_comment(report, repo_slug="acme/widgets")
    assert "for #7." in render_comment(report, pr_number=7)
    assert "into `main`.\n" in render_comment(report)


def test_render_comment_has_one_details_block_per_noisy_signal(report: Report) -> None:
    body = render_comment(report)
    # conflicts + semantic + overlap(skipped) sections, plus two evidence blocks.
    assert body.count("<summary>conflicts") == 1
    assert body.count("<summary>semantic") == 1
    assert body.count("<summary>overlap") == 1
    assert "<summary>risk" not in body  # clean signal stays quiet
    assert body.count("<details>") == body.count("</details>")


def test_render_comment_is_emoji_free_and_has_no_remote_images(report: Report) -> None:
    body = render_comment(report)
    assert "![" not in body
    assert "img.shields.io" not in body
    assert not any(ord(ch) >= 0x1F000 for ch in body)  # no emoji planes


# --------------------------------------------------------------- marker hunt


def test_find_existing_comment_round_trips_what_we_render(report: Report) -> None:
    posted = comment(1, render_comment(report))
    assert find_existing_comment([comment(0, "unrelated chatter"), posted]) is posted


def test_find_existing_comment_returns_none_without_a_match() -> None:
    assert find_existing_comment([]) is None
    assert find_existing_comment([comment(1, "just a human talking")]) is None


def test_find_existing_comment_prefers_the_oldest_by_created_at() -> None:
    newer = comment(9, f"new {COMMENT_MARKER}", created="2024-05-04T00:00:00Z")
    older = comment(2, f"old {COMMENT_MARKER}", created="2024-01-01T00:00:00Z")
    assert find_existing_comment([newer, older]) is older


def test_find_existing_comment_falls_back_to_lowest_id() -> None:
    newer = comment(9, f"new {COMMENT_MARKER}")
    older = comment(2, f"old {COMMENT_MARKER}")
    assert find_existing_comment([newer, older]) is older


def test_find_existing_comment_honours_bot_login() -> None:
    human = comment(1, f"quoting you: {COMMENT_MARKER}", login="carol")
    ours = comment(2, f"ours {COMMENT_MARKER}", login="mergesignal[bot]")
    assert find_existing_comment([human, ours], bot_login="mergesignal[bot]") is ours
    assert find_existing_comment([human, ours], bot_login="MergeSignal[bot]") is ours
    assert find_existing_comment([human], bot_login="mergesignal[bot]") is None
    assert find_existing_comment([human]) is human  # no login guard: first marker wins


def test_find_existing_comment_survives_malformed_entries() -> None:
    ours = comment(5, f"ours {COMMENT_MARKER}")
    junk: list[Any] = [None, "string", {"id": 1}, {"body": None}, {"body": 42, "id": 2}, ours]
    assert find_existing_comment(junk) is ours


def test_find_existing_comment_accepts_a_custom_marker() -> None:
    legacy = comment(1, "body <!-- mergesignal:v0 -->")
    assert find_existing_comment([legacy]) is None
    assert find_existing_comment([legacy], marker="<!-- mergesignal:v0 -->") is legacy


# ------------------------------------------------------------------ sections


def test_summary_section_is_a_four_row_table(report: Report) -> None:
    lines = summary_section(report).splitlines()
    assert lines[0] == "| Signal | Status | Findings | Summary |"
    assert lines[1] == "| --- | --- | --- | --- |"
    assert len(lines) == 6
    assert lines[2].startswith("| `conflicts` |")
    assert lines[-1].startswith("| `risk` |")


def test_risk_badge_formats_a_score(report: Report) -> None:
    assert risk_badge(report) == "**Risk:** 62/100 (high)"


def test_risk_badge_is_explicit_when_the_score_is_missing(report: Report) -> None:
    without = report.model_copy(update={"risk_score": None})
    badge = risk_badge(without)
    assert "not computed" in badge
    assert "/100" not in badge


def test_risk_badge_rounds_deterministically() -> None:
    rpt = Report(
        base="a",
        head="b",
        risk_score=RiskScore(score=0.0, level="low"),
        generated_at=FIXED_TIME,
    )
    assert risk_badge(rpt) == "**Risk:** 0/100 (low)"


def test_details_section_is_empty_for_a_clean_signal(report: Report) -> None:
    assert details_section(report, "risk") == ""


def test_details_section_is_empty_for_an_absent_signal(report: Report) -> None:
    assert details_section(report, "nope") == ""


def test_details_section_explains_skipped_and_errored_signals(report: Report) -> None:
    skipped = details_section(report, "overlap")
    assert skipped.startswith("<details>")
    assert "no other branches supplied" in skipped

    broken = report.model_copy(update={"signals": [Signal.error("risk", "boom")]})
    errored = details_section(broken, "risk")
    assert "risk — error" in errored
    assert "boom" in errored


def test_details_section_lists_findings_worst_first(report: Report) -> None:
    section = details_section(report, "conflicts")
    assert "conflicts — findings, 2 findings" in section
    critical = section.index("| critical |")
    high = section.index("| high |")
    assert critical < high


def test_details_section_verbose_keeps_full_evidence() -> None:
    finding = Finding(
        signal="conflicts",
        severity="high",
        title="big conflict",
        file="a.py",
        line=1,
        evidence={"lines": [f"line {i}" for i in range(40)]},
    )
    rpt = Report(
        base="a",
        head="b",
        signals=[Signal(name="conflicts", status="findings", findings=[finding])],
        generated_at=FIXED_TIME,
    )
    assert "more lines" in details_section(rpt, "conflicts")
    assert "more lines" not in details_section(rpt, "conflicts", verbose=True)


# ---------------------------------------------------------------- truncation


def test_truncate_body_leaves_short_bodies_alone() -> None:
    body = f"hello\n{COMMENT_MARKER}\n"
    assert truncate_body(body) == body


def test_truncate_body_cuts_at_a_line_boundary_and_keeps_the_marker() -> None:
    body = "\n".join(f"line {i}" for i in range(5000)) + f"\n{COMMENT_MARKER}\n"
    cut = truncate_body(body, limit=500)
    assert len(cut) <= 500
    assert cut.endswith(f"{COMMENT_MARKER}\n")
    assert "Output truncated" in cut
    content = [ln for ln in cut.splitlines() if ln.startswith("line ")]
    assert content[0] == "line 0"
    assert content[-1] == f"line {len(content) - 1}"  # never a half-written line


def test_truncated_body_is_still_findable() -> None:
    body = truncate_body("x\n" * 50_000 + COMMENT_MARKER, limit=1000)
    assert find_existing_comment([comment(1, body)]) is not None


def test_render_comment_never_exceeds_the_github_limit() -> None:
    findings = [
        Finding(
            signal="conflicts",
            severity="high",
            title=f"conflict {i}",
            file=f"file{i:04d}.py",
            line=i + 1,
            detail="x" * 200,
            evidence={"text": "y" * 500},
        )
        for i in range(400)
    ]
    rpt = Report(
        base="main",
        head="feature",
        signals=[Signal(name="conflicts", status="findings", findings=findings)],
        generated_at=FIXED_TIME,
    )
    body = render_comment(rpt, verbose=True)
    assert len(body) <= MAX_COMMENT_CHARS
    assert "Output truncated" in body
    assert body.count(COMMENT_MARKER) == 1
    assert find_existing_comment([comment(1, body)]) is not None
