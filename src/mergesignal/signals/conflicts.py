"""S1 — textual conflict prediction. **Owned by Agent E.**

Wraps :mod:`mergesignal.git.merge_sim`: simulate the merge, then emit one
:class:`~mergesignal.models.Finding` per conflicted region.

Severity policy (the contract renderers and the exit code depend on):

* a conflicted region in a text file -> ``high``
* a binary / add-add / modify-delete whole-file conflict -> ``critical``
  (a human cannot resolve it by reading a diff)
* a clean merge -> ``status="ok"``, no findings
* already up to date -> ``status="ok"`` with ``summary`` saying so

Confidence is always ``"high"``: unlike the semantic signal, this is git's own
verdict, not a heuristic.
"""

from __future__ import annotations

from typing import Any

from mergesignal.models import AnalysisContext, ConflictRegion, Finding, Signal

#: Name registered in :data:`mergesignal.signals.REGISTRY`.
NAME = "conflicts"

#: Most lines of either side's text kept in a finding's evidence excerpt.
MAX_EXCERPT_LINES = 20

#: Hard character cap on an excerpt, for files with pathologically long lines.
MAX_EXCERPT_CHARS = 2000

#: Appended to an excerpt that was cut short, so readers know there is more.
TRUNCATION_MARKER = "..."


def analyze(ctx: AnalysisContext) -> Signal:
    """Run the conflict signal. Never raises.

    ``metadata`` must carry at least ``conflicted_files`` (int),
    ``regions`` (int) and ``strategy`` (which simulation path ran), because the
    regression snapshots assert on them.
    """
    try:
        from mergesignal.git.merge_sim import simulate_merge
        from mergesignal.git.repo import Repo

        timeout = _git_timeout(ctx)
        repo = Repo(ctx.repo_path, timeout=timeout) if timeout else Repo(ctx.repo_path)
        simulation = simulate_merge(repo, ctx.base, ctx.head)
    except Exception as exc:  # noqa: BLE001 - engines never raise (see signals/__init__)
        return Signal.error(NAME, f"{type(exc).__name__}: {exc}")

    metadata: dict[str, Any] = {
        "conflicted_files": len(simulation.conflicted_files),
        "regions": len(simulation.regions),
        "strategy": simulation.strategy,
        "clean": simulation.clean,
        "up_to_date": simulation.up_to_date,
    }

    if simulation.up_to_date:
        return Signal(
            name=NAME,
            status="ok",
            summary=f"{ctx.head} is already merged into {ctx.base}; nothing to do",
            metadata=metadata,
        )
    if simulation.clean:
        return Signal(
            name=NAME,
            status="ok",
            summary="merges cleanly; no textual conflicts",
            metadata=metadata,
        )

    findings = [
        finding_for_region(region, base=ctx.base, head=ctx.head) for region in simulation.regions
    ]

    covered = {region.file for region in simulation.regions}
    unrepresented = [path for path in simulation.conflicted_files if path not in covered]
    for path in unrepresented:
        findings.append(
            Finding(
                signal=NAME,
                severity="critical",
                confidence="high",
                title=f"{path} conflicts with no recoverable region",
                detail=(
                    f"git reports {path} as conflicted when merging {ctx.head} into {ctx.base}, but no "
                    "conflict region could be extracted (add/add, modify/delete or an unreadable blob). "
                    "Resolve it by hand."
                ),
                file=path,
                evidence={"conflicted_file": path, "region_available": False},
            )
        )
    metadata["files_without_regions"] = len(unrepresented)

    return Signal(
        name=NAME,
        status="findings",
        findings=findings,
        summary=summarize(simulation.regions, simulation.conflicted_files),
        metadata=metadata,
    )


def finding_for_region(region: ConflictRegion, *, base: str, head: str) -> Finding:
    """Turn one :class:`~mergesignal.models.ConflictRegion` into a Finding.

    ``evidence`` carries the ours/theirs line ranges and a truncated excerpt of
    each side's text (capped so a 5000-line conflict does not produce a 5000-line
    JSON report). Binary regions carry ``{"binary": true}`` and no excerpt.
    """
    evidence: dict[str, Any] = {
        "file": region.file,
        "ours_range": [region.ours_range.start, region.ours_range.end],
        "theirs_range": [region.theirs_range.start, region.theirs_range.end],
        "ours_ref": base,
        "theirs_ref": head,
        "binary": region.is_binary,
    }

    if region.is_binary:
        return Finding(
            signal=NAME,
            severity="critical",
            confidence="high",
            title=f"Binary conflict in {region.file}",
            detail=(
                f"{region.file} changed on both sides and cannot be merged line by line. "
                f"Pick one side's version of the file deliberately when merging {head} into {base}."
            ),
            file=region.file,
            evidence=evidence,
        )

    ours_excerpt = _excerpt(region.ours_text)
    theirs_excerpt = _excerpt(region.theirs_text)
    if ours_excerpt is not None:
        evidence["ours_excerpt"] = ours_excerpt
    if theirs_excerpt is not None:
        evidence["theirs_excerpt"] = theirs_excerpt
    if region.base_text is not None:
        base_excerpt = _excerpt(region.base_text)
        if base_excerpt is not None:
            evidence["base_excerpt"] = base_excerpt

    line = region.ours_range.start if region.ours_range.start >= 1 else None
    if line is None and region.theirs_range.start >= 1:
        line = region.theirs_range.start

    location = f"{region.file}:{line}" if line is not None else region.file
    return Finding(
        signal=NAME,
        severity="high",
        confidence="high",
        title=f"Merge conflict in {location}",
        detail=(
            f"Merging {head} into {base} conflicts at {location}: both sides changed the same lines "
            f"({region.ours_range.length} line(s) from {base}, {region.theirs_range.length} from {head})."
        ),
        file=region.file,
        line=line,
        evidence=evidence,
    )


def summarize(regions: list[ConflictRegion], files: list[str]) -> str:
    """One-line human summary, e.g. ``"3 conflicted regions across 2 files"``.

    Pluralises correctly and reports files with no extractable regions
    separately, so a binary conflict is never silently dropped from the count.
    """
    if not files and not regions:
        return "no textual conflicts"

    covered = {region.file for region in regions}
    file_count = len(set(files) | covered)
    parts = [f"{_plural(len(regions), 'conflicted region')} across {_plural(file_count, 'file')}"]

    binary = sum(1 for region in regions if region.is_binary)
    if binary:
        parts.append(f"{_plural(binary, 'binary conflict')}")

    missing = len([path for path in files if path not in covered])
    if missing:
        parts.append(f"{_plural(missing, 'file')} with no extractable region")
    return ", ".join(parts)


def _plural(count: int, noun: str) -> str:
    """``"1 file"`` / ``"2 files"`` — naive but correct for the nouns used here."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _excerpt(text: str | None) -> str | None:
    """Cap ``text`` at :data:`MAX_EXCERPT_LINES` / :data:`MAX_EXCERPT_CHARS`.

    Returns ``None`` for missing or empty text, so callers can omit the key
    entirely rather than emitting a null into the JSON report.
    """
    if not text:
        return None
    lines = text.splitlines()
    truncated = len(lines) > MAX_EXCERPT_LINES
    body = "\n".join(lines[:MAX_EXCERPT_LINES])
    if len(body) > MAX_EXCERPT_CHARS:
        body = body[:MAX_EXCERPT_CHARS]
        truncated = True
    return f"{body}\n{TRUNCATION_MARKER}" if truncated else body


def _git_timeout(ctx: AnalysisContext) -> float | None:
    """Per-invocation git timeout from the config, or ``None`` when unset."""
    analysis = getattr(getattr(ctx, "config", None), "analysis", None)
    timeout = getattr(analysis, "git_timeout_seconds", None)
    return float(timeout) if isinstance(timeout, (int, float)) else None
