"""``mergesignal`` command line interface and the signal pipeline entry point.

Commands
--------

``analyze``
    The main event: analyse one base/head pair and print a
    :class:`~mergesignal.models.Report`.
``scan``
    Analyse many change sets at once — every open PR (``--prs``) or every local
    branch (``--branches``) against a base — and report which of them collide.
``serve``
    Run the webhook service.

Exit codes (FR-7), decided by :func:`exit_code_for`:

``0``
    Clean: no finding reached the severity threshold.
``1``
    Findings at or above the threshold.
``2``
    Error: bad arguments, git failure, unreadable config, or any signal engine
    reporting ``status="error"``.

Pipeline
--------

:func:`build_context` gathers the shared inputs once and :func:`run_pipeline`
runs each enabled engine through :func:`run_signal`, which traps **every**
exception — including the ``NotImplementedError`` raised by engines that are not
written yet — into ``Signal(status="error")``. That is why the CLI works end to
end from the very first commit of this project: an unimplemented engine degrades
to an error row instead of a traceback.

Other subsystems (notably :mod:`mergesignal.service.worker`) must call
:func:`run_pipeline` rather than reimplementing the orchestration.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from mergesignal import __version__
from mergesignal.config import Config, ConfigError, load_config
from mergesignal.git.repo import GitError, Repo
from mergesignal.models import (
    SIGNAL_NAMES,
    AnalysisContext,
    BranchDiff,
    OutputFormat,
    Report,
    RiskScore,
    Severity,
    Signal,
)

#: Exit status: no findings at or above the threshold.
EXIT_CLEAN = 0

#: Exit status: findings at or above the threshold.
EXIT_FINDINGS = 1

#: Exit status: something went wrong (bad input, git failure, engine error).
EXIT_ERROR = 2

app = typer.Typer(
    name="mergesignal",
    help="Pre-merge intelligence: conflicts, semantic breakage, cross-PR overlap and risk.",
    add_completion=False,
    no_args_is_help=True,
)


# --------------------------------------------------------------------- shared


def _echo_err(message: str) -> None:
    """Write a message to stderr so it never pollutes ``--format json`` stdout."""
    typer.echo(message, err=True)


def _load_config_or_exit(config_path: str | None, repo_path: str) -> Config:
    """Load configuration, exiting with :data:`EXIT_ERROR` on a bad file."""
    try:
        return load_config(config_path, repo_path=repo_path)
    except ConfigError as exc:
        _echo_err(f"error: {exc}")
        raise typer.Exit(EXIT_ERROR) from exc


def _open_repo_or_exit(repo_path: str, timeout: float) -> Repo:
    """Open the repository, exiting with :data:`EXIT_ERROR` when it is not one."""
    try:
        repo = Repo(repo_path, timeout=timeout)
        if not repo.is_repository():
            raise GitError(f"not a git repository: {repo_path}")
    except GitError as exc:
        _echo_err(f"error: {exc}")
        raise typer.Exit(EXIT_ERROR) from exc
    return repo


# ------------------------------------------------------------------- pipeline


def build_context(repo: Repo, base: str, head: str, config: Config, *, others: list[BranchDiff] | None = None) -> AnalysisContext:
    """Assemble the inputs every signal engine shares.

    Resolves the merge base, computes the two structured diffs (merge-base to
    base and merge-base to head) and builds the symbol indexes for the touched
    files.

    Each of those steps is *optional* in the sense that a failure leaves the
    corresponding field empty rather than aborting: an engine whose inputs are
    missing reports ``skipped`` or ``error`` on its own. In particular
    ``NotImplementedError`` from the not-yet-written analysis layer is caught
    here so the CLI stays usable while the project is being built out.

    :raises GitError: ``base`` or ``head`` does not resolve — that is fatal,
        because nothing downstream can proceed without both refs.
    """
    repo.rev_parse(base)
    repo.rev_parse(head)
    merge_base = repo.merge_base(base, head)

    ctx_kwargs: dict[str, Any] = {
        "repo_path": str(Path(repo.path).resolve()),
        "base": base,
        "head": head,
        "merge_base": merge_base,
        "others": list(others or []),
        "config": config,
    }

    diff_base = merge_base or base
    try:
        from mergesignal.analysis.diff import diff_refs

        ctx_kwargs["base_diff"] = diff_refs(repo, diff_base, base, max_files=config.analysis.max_files)
        ctx_kwargs["head_diff"] = diff_refs(repo, diff_base, head, max_files=config.analysis.max_files)
    except (NotImplementedError, GitError, ImportError):
        ctx_kwargs["base_diff"] = None
        ctx_kwargs["head_diff"] = None

    ctx = AnalysisContext(**ctx_kwargs)

    head_diff = ctx.head_diff
    if head_diff is not None:
        try:
            from mergesignal.analysis.index import build_indexes, diff_symbols

            base_index, head_index = build_indexes(
                repo,
                diff_base,
                head,
                head_diff,
                max_files=config.analysis.max_files,
                max_file_bytes=config.analysis.max_file_bytes,
            )
            ctx.base_symbols = list(base_index.all_symbols)
            ctx.head_symbols = list(head_index.all_symbols)
            ctx.base_references = list(base_index.all_references)
            ctx.head_references = list(head_index.all_references)
            ctx.head_changes = diff_symbols(base_index, head_index)
        except (NotImplementedError, GitError, ImportError):
            pass

    return ctx


def run_signal(name: str, ctx: AnalysisContext) -> Signal:
    """Run one engine, converting any exception into ``Signal(status="error")``.

    This is the safety net that makes the "engines never raise" invariant true
    even for engines that have not been written yet: an unimplemented engine
    surfaces as ``status="error"`` with ``summary="not implemented"``.
    """
    from mergesignal.signals import REGISTRY

    started = time.perf_counter()
    try:
        analyzer = REGISTRY[name]
    except KeyError:
        return Signal.error(name, f"unknown signal: {name!r}")

    try:
        signal = analyzer(ctx)
    except NotImplementedError:
        signal = Signal.error(name, "not implemented")
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all; one bad engine must not lose the report
        signal = Signal.error(name, f"{type(exc).__name__}: {exc}")

    if signal.duration_ms is None:
        signal.duration_ms = round((time.perf_counter() - started) * 1000, 3)
    return signal


def run_pipeline(ctx: AnalysisContext, config: Config) -> Report:
    """Run every enabled signal engine and assemble the :class:`Report`.

    Signals appear in :data:`~mergesignal.models.SIGNAL_NAMES` order regardless
    of the order they are listed in the config, so output is deterministic.
    Disabled engines are omitted entirely rather than reported as skipped.

    The risk engine's ``metadata["risk_score"]`` is lifted onto
    :attr:`Report.risk_score` when present and well-formed.
    """
    signals = [run_signal(name, ctx) for name in SIGNAL_NAMES if config.is_enabled(name)]

    report = Report(
        base=ctx.base,
        head=ctx.head,
        merge_base=ctx.merge_base,
        signals=signals,
        repo_path=ctx.repo_path,
    )

    risk_signal = report.signal("risk")
    if risk_signal is not None:
        raw = risk_signal.metadata.get("risk_score")
        if isinstance(raw, dict):
            with contextlib.suppress(ValueError):
                report.risk_score = RiskScore.model_validate(raw)
        elif isinstance(raw, RiskScore):
            report.risk_score = raw
    return report


def exit_code_for(report: Report, threshold: Severity) -> int:
    """Map a report onto the FR-7 exit-code contract.

    Errors win over findings: a run where an engine blew up cannot honestly
    claim the merge is clean, so it exits 2 even if the engines that did run
    found nothing.
    """
    if report.has_errors:
        return EXIT_ERROR
    if report.exceeds(threshold):
        return EXIT_FINDINGS
    return EXIT_CLEAN


# ------------------------------------------------------------------ rendering


def render_report(report: Report, fmt: OutputFormat, *, verbose: bool = False) -> str:
    """Render via :mod:`mergesignal.report.render`, with a bootstrap fallback.

    While the reporting layer is unimplemented this falls back to a minimal
    built-in renderer so that ``mergesignal analyze`` produces usable output
    from day one. Once :func:`mergesignal.report.render.render` exists this
    function is a thin delegation and the fallback is dead code.
    """
    try:
        from mergesignal.report.render import render

        return render(report, fmt, verbose=verbose)
    except (NotImplementedError, ImportError):
        return _fallback_render(report, fmt)


def _fallback_render(report: Report, fmt: OutputFormat) -> str:
    """Minimal dependency-free renderer used until Agent D's renderers land."""
    if fmt == "json":
        return report.model_dump_json(indent=2)

    lines = [
        f"MergeSignal {__version__}",
        f"base: {report.base}",
        f"head: {report.head}",
        f"merge-base: {report.merge_base or '(none)'}",
        "",
    ]
    for signal in report.signals:
        lines.append(f"[{signal.status}] {signal.name}: {signal.summary or '-'} ({len(signal.findings)} findings)")
        for finding in sorted(signal.findings, key=lambda f: f.sort_key):
            location = f" {finding.file}:{finding.line}" if finding.file else ""
            lines.append(f"    - ({finding.severity}/{finding.confidence}){location} {finding.title}")
    if report.risk_score is not None:
        lines.extend(["", f"risk: {report.risk_score.score:.0f}/100 ({report.risk_score.level})"])
    if fmt == "md":
        body = "\n".join(f"    {line}" if line else "" for line in lines)
        return f"## MergeSignal report\n\n```\n{body}\n```\n"
    return "\n".join(lines)


# ------------------------------------------------------------------- commands


def _version_callback(value: bool) -> None:
    """``--version`` handler: print and exit cleanly."""
    if value:
        typer.echo(f"mergesignal {__version__}")
        raise typer.Exit(EXIT_CLEAN)


@app.callback()
def main(
    version: Annotated[bool, typer.Option("--version", callback=_version_callback, is_eager=True, help="Print the version and exit.")] = False,
) -> None:
    """MergeSignal — answer "what happens if this merges?" before it merges."""


@app.command()
def analyze(
    base: Annotated[str, typer.Option("--base", "-b", help="Base ref to merge into (branch, tag or sha).")] = "HEAD",
    head: Annotated[str, typer.Option("--head", "-H", help="Head ref to merge from (branch, tag or sha).")] = "HEAD",
    fmt: Annotated[str, typer.Option("--format", "-f", help="Output format: text, json or md.")] = "text",
    repo_path: Annotated[str, typer.Option("--repo", "-C", help="Repository to analyse. Defaults to the current directory.")] = ".",
    config_path: Annotated[str | None, typer.Option("--config", help="Path to .mergesignal.yaml. Defaults to searching upward from the repo.")] = None,
    branches: Annotated[list[str] | None, typer.Option("--branches", help="Local branches to check for cross-branch overlap. Repeat or comma-separate; 'all' uses every local branch.")] = None,
    prs: Annotated[list[str] | None, typer.Option("--prs", help="GitHub PR numbers to check for overlap. Repeat or comma-separate; 'open' uses every open PR.")] = None,
    threshold: Annotated[str | None, typer.Option("--threshold", help="Severity at or above which the command exits 1. Overrides the config.")] = None,
    signals: Annotated[list[str] | None, typer.Option("--signal", help="Run only these signals. Repeat the flag; defaults to the config's enabled_signals.")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Include full evidence in the output.")] = False,
) -> None:
    """Analyse merging --head into --base and report the four signals.

    Exits 0 when clean, 1 when findings reach the severity threshold, and 2 on
    any error (including a signal engine failing).
    """
    fmt_value = _parse_format(fmt)
    config = _load_config_or_exit(config_path, repo_path)
    if threshold is not None:
        config.severity_threshold = _parse_severity(threshold)
    if signals:
        config.enabled_signals = _parse_signal_names(signals)

    repo = _open_repo_or_exit(repo_path, config.analysis.git_timeout_seconds)

    try:
        others = collect_others(repo, config, branches=branches, prs=prs, base=base)
        ctx = build_context(repo, base, head, config, others=others)
    except GitError as exc:
        _echo_err(f"error: {exc}")
        raise typer.Exit(EXIT_ERROR) from exc

    report = run_pipeline(ctx, config)
    typer.echo(render_report(report, fmt_value, verbose=verbose))
    raise typer.Exit(exit_code_for(report, config.severity_threshold))


@app.command()
def scan(
    base: Annotated[str, typer.Option("--base", "-b", help="Base ref every candidate is compared against.")] = "HEAD",
    repo_path: Annotated[str, typer.Option("--repo", "-C", help="Repository to scan. Defaults to the current directory.")] = ".",
    fmt: Annotated[str, typer.Option("--format", "-f", help="Output format: text, json or md.")] = "text",
    config_path: Annotated[str | None, typer.Option("--config", help="Path to .mergesignal.yaml.")] = None,
    branches: Annotated[list[str] | None, typer.Option("--branches", help="Local branches to scan. Repeat, comma-separate, or pass 'all'.")] = None,
    prs: Annotated[list[str] | None, typer.Option("--prs", help="GitHub PRs to scan. Repeat, comma-separate, or pass 'open'.")] = None,
    threshold: Annotated[str | None, typer.Option("--threshold", help="Severity at or above which the command exits 1.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum candidates to scan.")] = 20,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Include full evidence in the output.")] = False,
) -> None:
    """Scan many branches or PRs against a base and report which ones collide.

    Each candidate is analysed against --base with every *other* candidate
    supplied as overlap input, so the output answers "which of these open
    changes are fighting each other?".

    Neither --branches nor --prs given defaults to ``--branches all``.
    """
    fmt_value = _parse_format(fmt)
    config = _load_config_or_exit(config_path, repo_path)
    if threshold is not None:
        config.severity_threshold = _parse_severity(threshold)
    repo = _open_repo_or_exit(repo_path, config.analysis.git_timeout_seconds)

    if not branches and not prs:
        branches = ["all"]

    try:
        others = collect_others(repo, config, branches=branches, prs=prs, base=base)
    except GitError as exc:
        _echo_err(f"error: {exc}")
        raise typer.Exit(EXIT_ERROR) from exc
    candidates = [o.head for o in others][:limit]

    reports: list[Report] = []
    worst = EXIT_CLEAN
    for candidate in candidates:
        peers = [o for o in others if o.head != candidate]
        try:
            ctx = build_context(repo, base, candidate, config, others=peers)
        except GitError as exc:
            _echo_err(f"error: {candidate}: {exc}")
            worst = EXIT_ERROR
            continue
        report = run_pipeline(ctx, config)
        reports.append(report)
        worst = max(worst, exit_code_for(report, config.severity_threshold))

    if fmt_value == "json":
        typer.echo(json.dumps([json.loads(r.model_dump_json()) for r in reports], indent=2))
    else:
        for report in reports:
            typer.echo(render_report(report, fmt_value, verbose=verbose))
            typer.echo("")
    raise typer.Exit(worst)


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to bind.")] = 8000,
    config_path: Annotated[str | None, typer.Option("--config", help="Path to .mergesignal.yaml.")] = None,
    reload: Annotated[bool, typer.Option("--reload", help="Auto-reload on code changes (development only).")] = False,
    log_level: Annotated[str, typer.Option("--log-level", help="uvicorn log level.")] = "info",
) -> None:
    """Run the GitHub webhook service.

    Requires MERGESIGNAL_WEBHOOK_SECRET, plus either MERGESIGNAL_APP_ID with
    MERGESIGNAL_PRIVATE_KEY, or GITHUB_TOKEN. Exits 2 when the service cannot
    start. **Owned by Agent F** — the body below is a placeholder delegation.
    """
    _load_config_or_exit(config_path, ".")
    try:
        from mergesignal.service.server import run as run_server

        run_server(host=host, port=port, reload=reload, log_level=log_level)
    except NotImplementedError as exc:
        _echo_err("error: the webhook service is not implemented yet")
        raise typer.Exit(EXIT_ERROR) from exc
    except (ImportError, RuntimeError) as exc:
        _echo_err(f"error: {exc}")
        raise typer.Exit(EXIT_ERROR) from exc


# ------------------------------------------------------------------- helpers


def _parse_format(value: str) -> OutputFormat:
    """Validate ``--format``, exiting 2 on an unknown value."""
    normalised = value.strip().lower()
    aliases = {"markdown": "md", "text": "text", "json": "json", "md": "md", "txt": "text"}
    if normalised not in aliases:
        _echo_err(f"error: unknown format {value!r}; expected text, json or md")
        raise typer.Exit(EXIT_ERROR)
    return aliases[normalised]  # type: ignore[return-value]


def _parse_severity(value: str) -> Severity:
    """Validate a severity name, exiting 2 on an unknown value."""
    normalised = value.strip().lower()
    if normalised not in ("low", "medium", "high", "critical"):
        _echo_err(f"error: unknown severity {value!r}; expected low, medium, high or critical")
        raise typer.Exit(EXIT_ERROR)
    return normalised  # type: ignore[return-value]


def _parse_signal_names(values: list[str]) -> list[str]:
    """Split, validate and canonically order ``--signal`` values."""
    requested = {item.strip() for value in values for item in value.split(",") if item.strip()}
    unknown = sorted(requested - set(SIGNAL_NAMES))
    if unknown:
        _echo_err(f"error: unknown signal(s) {unknown}; expected any of {list(SIGNAL_NAMES)}")
        raise typer.Exit(EXIT_ERROR)
    return [name for name in SIGNAL_NAMES if name in requested]


def _split_option(values: list[str] | None) -> list[str]:
    """Flatten repeated and comma-separated option values into a clean list."""
    if not values:
        return []
    return [item.strip() for value in values for item in value.split(",") if item.strip()]


def resolve_branch_refs(repo: Repo, branches: list[str] | None, *, base: str) -> list[str]:
    """Expand a ``--branches`` option into concrete branch names.

    ``all`` (or ``*``) expands to every local branch except ``base`` itself.
    Unknown branch names are dropped with a warning rather than aborting the run,
    because a stale name in a script should not cost the user their whole report.
    """
    requested = _split_option(branches)
    if not requested:
        return []
    wants_all = any(item in ("all", "*") for item in requested)
    names = repo.list_branches() if wants_all else requested
    base_sha = repo.rev_parse(base) if repo.ref_exists(base) else None
    resolved: list[str] = []
    for name in names:
        if not repo.ref_exists(name):
            _echo_err(f"warning: skipping unknown branch {name!r}")
            continue
        if base_sha is not None and repo.rev_parse(name) == base_sha:
            continue
        resolved.append(name)
    return resolved


def collect_others(repo: Repo, config: Config, *, branches: list[str] | None, prs: list[str] | None, base: str) -> list[BranchDiff]:
    """Build the overlap inputs (S3) from ``--branches`` and/or ``--prs``.

    Local branches are diffed directly; PRs are fetched through
    :mod:`mergesignal.github.client`, which requires network access and a token.
    Either source failing degrades to an empty list plus a stderr warning — the
    overlap signal then reports ``skipped`` rather than the whole run failing,
    keeping MergeSignal usable offline (FR-8).
    """
    others: list[BranchDiff] = []
    branch_names = resolve_branch_refs(repo, branches, base=base)
    if branch_names:
        try:
            from mergesignal.analysis.diff import diff_refs

            for name in branch_names:
                others.append(
                    BranchDiff(
                        name=name,
                        head=name,
                        base=base,
                        diff=diff_refs(repo, base, name, merge_base=True, max_files=config.analysis.max_files),
                    )
                )
        except (NotImplementedError, GitError, ImportError) as exc:
            _echo_err(f"warning: cross-branch overlap unavailable ({type(exc).__name__})")
            others = []

    pr_values = _split_option(prs)
    if pr_values:
        try:
            others.extend(_collect_pr_diffs(repo, config, pr_values, base=base))
        except Exception as exc:  # noqa: BLE001 - network/auth failures must not kill the run
            _echo_err(f"warning: cross-PR overlap unavailable ({type(exc).__name__}: {exc})")
    return others


def _collect_pr_diffs(repo: Repo, config: Config, pr_values: list[str], *, base: str) -> list[BranchDiff]:
    """Fetch open PRs and diff each against ``base``. **Owned by Agent F.**

    ``open`` expands to every open PR up to ``config.github.max_prs``; otherwise
    the values are PR numbers.
    """
    raise NotImplementedError


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(app())
