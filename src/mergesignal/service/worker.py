"""Analysis pipeline for webhook-triggered runs. **Owned by Agent F.**

One job: given a PR, produce a report and update its comment.

Steps, all inside an isolated temporary directory that is removed in a
``finally`` block no matter what:

1. Shallow-clone (or fetch into a cached bare mirror) just the base and head
   refs. Fork PRs need the head fetched from the fork's clone URL.
2. Read ``.mergesignal.yaml`` out of the **base** ref — the clone has no
   working tree, and head's copy must never be trusted: the config controls
   what MergeSignal does to the repo, so a fork PR cannot silence it by
   editing the file in its own branch.
3. Build an :class:`~mergesignal.models.AnalysisContext` and run the signal
   pipeline — reuse :func:`mergesignal.cli.run_pipeline`, never reimplement it.
4. Render via :mod:`mergesignal.report.github_comment` and upsert the comment.

A per-run timeout bounds the whole thing; exceeding it posts a comment saying
the analysis timed out rather than leaving the PR with a stale report.

The timeout is a *deadline*, checked between steps and pushed down into every
git subprocess, rather than a hard kill: the pipeline is pure Python and git
subprocesses, both of which honour it, and killing a thread mid-analysis would
leak the checkout we are careful to clean up.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mergesignal.git.repo import GitError, Repo
from mergesignal.models import Report

#: Depth used for shallow fetches; enough history for merge-base resolution in
#: the common case, with a documented fallback to deepening when it is not.
DEFAULT_FETCH_DEPTH = 50

#: How many times a fetch is deepened (each time by 4x) before giving up and
#: unshallowing outright. A PR branched from an ancient commit still analyses.
MAX_DEEPEN_ATTEMPTS = 2

#: Prefix for every temporary checkout, so stray directories are identifiable.
TMP_PREFIX = "mergesignal-"

#: HEAD is pointed at this (never created) branch so that fetching into
#: ``refs/heads/<base branch>`` is not refused as "checked out".
IDLE_BRANCH = "mergesignal-idle"

logger = logging.getLogger("mergesignal.service.worker")


class RunTimeout(TimeoutError):
    """The per-run deadline passed before the analysis finished."""


@dataclass(frozen=True)
class WorkerResult:
    """Outcome of one webhook-triggered analysis run."""

    repo_slug: str
    pr_number: int
    report: Report | None
    comment_id: int | None = None
    error: str | None = None
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """``True`` when a report was produced and no error was recorded."""
        return self.report is not None and self.error is None


def base_ref_name(pr: Any) -> str:
    """Local ref the PR's base branch is fetched into."""
    return str(getattr(pr, "base_ref", "") or "base")


def head_ref_name(pr: Any) -> str:
    """Local ref the PR's head is fetched into.

    Deliberately *not* the head branch name: a fork PR's head branch is often
    called ``main`` too, and fetching both into ``refs/heads/main`` would make
    the head clobber the base.
    """
    return f"pr-{int(getattr(pr, 'number', 0) or 0)}"


def analyze_pull_request(
    repo_slug: str,
    pr_number: int,
    *,
    token: str | None = None,
    api_url: str = "https://api.github.com",
    work_dir: str | None = None,
    timeout_seconds: float = 300.0,
    post_comment: bool = True,
    client: Any = None,
    transport: Any = None,
    clone_url: str | None = None,
    config: Any = None,
    verbose: bool = False,
) -> WorkerResult:
    """Full clone -> analyse -> comment pipeline for one PR.

    Never raises: every failure (clone error, timeout, GitHub 403) is captured
    in :attr:`WorkerResult.error` so the webhook handler can log it and return a
    2xx rather than triggering GitHub's redelivery storm.

    :param client: pre-built :class:`~mergesignal.github.client.GitHubClient`;
        when omitted one is constructed and closed by this function.
    :param transport: ``httpx`` transport for the client we build (tests).
    :param clone_url: override the URL both refs are fetched from.
    """
    from mergesignal.github.client import GitHubClient

    started = time.monotonic()
    deadline = started + max(float(timeout_seconds), 0.0)
    owns_client = client is None
    checkout: str | None = None
    report: Report | None = None
    comment_id: int | None = None

    try:
        if client is None:
            client = GitHubClient(repo_slug, token=token, api_url=api_url, transport=transport, clone_url=clone_url)

        pr = client.get_pull(pr_number)
        _check_deadline(deadline)

        checkout = tempfile.mkdtemp(prefix=TMP_PREFIX, dir=work_dir)
        repo_path = prepare_checkout(
            repo_slug,
            pr,
            checkout,
            token=token,
            base_url=clone_url or client.repo_clone_url(),
            head_url=clone_url or pr.head_repo_clone_url,
            timeout=_remaining(deadline),
        )
        _check_deadline(deadline)

        if config is None:
            config = _service_config(repo_path, base_ref_name(pr), timeout=_remaining(deadline))
        report = _run_analysis(
            repo_path,
            pr,
            config,
            client=client,
            base_url=clone_url or client.repo_clone_url(),
            deadline=deadline,
            timeout=_remaining(deadline),
        )
        _check_deadline(deadline)

        if post_comment and getattr(config.github, "comment", True):
            data = client.upsert_report_comment(pr.number, report, verbose=verbose)
            raw_id = data.get("id") if isinstance(data, dict) else None
            comment_id = raw_id if isinstance(raw_id, int) else None

        if getattr(config.github, "check_run", False) and pr.head_sha:
            try:
                client.create_check_run(pr.head_sha, report, threshold=config.severity_threshold)
            except Exception as exc:  # noqa: BLE001 - a check run is a bonus, never the point
                logger.warning("check run failed for %s#%s: %s", repo_slug, pr_number, exc)

        return WorkerResult(
            repo_slug=repo_slug,
            pr_number=pr_number,
            report=report,
            comment_id=comment_id,
            duration_seconds=time.monotonic() - started,
        )

    except RunTimeout as exc:
        logger.warning("analysis of %s#%s timed out: %s", repo_slug, pr_number, exc)
        if post_comment and client is not None:
            _post_failure_comment(client, pr_number, f"Analysis timed out after {timeout_seconds:.0f}s.")
        return WorkerResult(
            repo_slug=repo_slug,
            pr_number=pr_number,
            report=None,
            error=f"timeout: {exc}",
            duration_seconds=time.monotonic() - started,
        )
    except Exception as exc:
        logger.exception("analysis of %s#%s failed", repo_slug, pr_number)
        return WorkerResult(
            repo_slug=repo_slug,
            pr_number=pr_number,
            report=None,
            error=f"{type(exc).__name__}: {exc}",
            duration_seconds=time.monotonic() - started,
        )
    finally:
        if checkout is not None:
            cleanup(checkout)
        if owns_client and client is not None:
            client.close()


def _service_config(repo_path: str, base_ref: str, *, timeout: float) -> Any:
    """The analysed repo's own ``.mergesignal.yaml``, read from the *base* ref.

    The service clone has no working tree, so the file is read straight from
    the git object store via :meth:`Repo.file_content_at`.

    Base, never head: the config controls what MergeSignal does — enabled
    signals, thresholds, whether to comment — so it must come from the trusted
    side of the merge. Letting head supply it would let any fork PR silence the
    tool by editing the file in its own branch.

    A missing file yields defaults; an unreadable or invalid one is a logged
    warning plus defaults — a broken config in the repo must not take the
    service down with it.
    """
    from mergesignal.config import (
        ALT_CONFIG_FILENAMES,
        CONFIG_FILENAME,
        ConfigError,
        config_from_text,
        default_config,
    )

    repo = Repo(repo_path, timeout=max(timeout, 1.0))
    for name in (CONFIG_FILENAME, *ALT_CONFIG_FILENAMES):
        try:
            text = repo.file_content_at(base_ref, name)
        except GitError:
            text = None
        if text is None:
            continue
        try:
            return config_from_text(text, source=f"{base_ref}:{name}")
        except ConfigError as exc:
            logger.warning("ignoring invalid %s in repo config: %s", name, exc)
            return default_config()
    return default_config()


def _run_analysis(
    repo_path: str,
    pr: Any,
    config: Any,
    *,
    client: Any = None,
    base_url: str | None = None,
    deadline: float | None = None,
    timeout: float,
) -> Report:
    """Build the context and run the shared pipeline from :mod:`mergesignal.cli`.

    ``client`` + ``base_url`` enable S3: the other open PRs targeting the same
    base are fetched into the temporary checkout and diffed, so the overlap
    signal fires on webhook runs exactly as it does for ``analyze --prs``.
    Without a client the overlap engine simply reports ``skipped``.
    """
    from mergesignal.cli import build_context, run_pipeline

    git_timeout = min(float(config.analysis.git_timeout_seconds), max(timeout, 1.0))
    repo = Repo(repo_path, timeout=git_timeout)
    others = _collect_peer_diffs(repo, client, pr, config, base_url=base_url, deadline=deadline)
    ctx = build_context(repo, base_ref_name(pr), head_ref_name(pr), config, others=others)
    return run_pipeline(ctx, config)


#: Namespace inside the temporary checkout that peer PR heads are fetched into.
#: Not ``refs/heads/`` — a peer branch named like the base must never collide
#: with it, and the whole namespace disappears with the checkout anyway.
PEER_REF_PREFIX = "refs/mergesignal/others"


def _collect_peer_diffs(repo: Repo, client: Any, pr: Any, config: Any, *, base_url: str | None, deadline: float | None) -> list[Any]:
    """Fetch other open PRs' heads and diff each against the base — S3 input.

    Unlike :func:`mergesignal.cli.collect_others`, which is forbidden from
    fetching (NFR-2 protects the *user's* repository), the service clone is a
    throwaway we own, so peers are fetched into ``refs/mergesignal/others/*``.

    Skipped entirely when the overlap engine is disabled or no client is
    available. Otherwise every failure is per-PR: an unfetchable peer (deleted
    fork, missing ref) is skipped with a warning, and a failed listing degrades
    to no peers — overlap reports ``skipped`` rather than sinking the run.
    """
    if client is None or not config.is_enabled("overlap"):
        return []

    base = base_ref_name(pr)
    try:
        pulls = client.list_open_pulls(limit=config.github.max_prs, base=base)
    except Exception as exc:  # noqa: BLE001 - overlap is advisory, never fatal
        logger.warning("could not list open PRs for overlap on %s#%s: %s", client.repo_slug, getattr(pr, "number", "?"), exc)
        return []

    others: list[Any] = []
    for pull in pulls:
        if deadline is not None:
            _check_deadline(deadline)
        if pull.number == getattr(pr, "number", None) or not pull.head_ref:
            continue
        local_ref = f"{PEER_REF_PREFIX}/pr-{pull.number}"
        url = pull.head_repo_clone_url or base_url
        if not url:
            continue
        try:
            _fetch(repo, url, [f"+refs/heads/{pull.head_ref}:{local_ref}"], depth=DEFAULT_FETCH_DEPTH)
        except GitError as exc:
            logger.warning("skipping peer %s for overlap: %s", pull.label, exc)
            continue
        try:
            others.append(_branch_diff_for_peer(repo, pull, local_ref, base, config))
        except GitError as exc:
            logger.warning("could not diff peer %s: %s", pull.label, exc)
    return others


def _branch_diff_for_peer(repo: Repo, pull: Any, local_ref: str, base: str, config: Any) -> Any:
    """One :class:`BranchDiff` for a fetched peer head.

    A shallow fetch can leave the peer's merge base with ``base`` unreachable;
    ``git diff base...ref`` then fails, and the caller skips the peer rather
    than reporting a diff computed against the wrong commits.
    """
    from mergesignal.analysis.diff import diff_refs
    from mergesignal.cli import index_branch_symbols
    from mergesignal.models import BranchDiff

    diff = diff_refs(repo, base, local_ref, merge_base=True, max_files=config.analysis.max_files)
    return BranchDiff(
        name=pull.label,
        head=local_ref,
        base=base,
        diff=diff,
        symbols=index_branch_symbols(repo, local_ref, diff, config, base=base),
        pr_number=pull.number,
        url=pull.url or None,
        author=pull.author or None,
    )


def prepare_checkout(
    repo_slug: str,
    pr: Any,
    work_dir: str,
    *,
    token: str | None = None,
    depth: int = DEFAULT_FETCH_DEPTH,
    base_url: str | None = None,
    head_url: str | None = None,
    timeout: float = 120.0,
) -> str:
    """Fetch base and head refs into a fresh repository under ``work_dir``.

    :returns: path to the prepared repository.
    :raises GitError: the fetch failed.

    Deepens the fetch automatically when the merge base is not reachable at the
    requested depth; a PR branched from an old commit must still analyse.

    ``base_url`` defaults to the anonymous HTTPS URL for ``repo_slug`` (with
    ``token`` embedded when given); ``head_url`` is only needed for fork PRs and
    defaults to ``base_url``.
    """
    from mergesignal.github.client import CLONE_USERNAME, clone_host

    base_ref = base_ref_name(pr)
    head_ref_remote = str(getattr(pr, "head_ref", "") or "")
    if not base_ref or not head_ref_remote:
        raise GitError(f"pull request {getattr(pr, 'number', '?')} is missing base/head refs")

    if base_url is None:
        host = clone_host("https://api.github.com")
        credentials = f"{CLONE_USERNAME}:{token}@" if token else ""
        base_url = f"https://{credentials}{host}/{repo_slug}.git"
    head_url = head_url or base_url

    path = Path(work_dir) / "repo"
    path.mkdir(parents=True, exist_ok=True)

    repo = Repo(path, timeout=timeout)
    repo.run(["init", "--quiet"], check=True)
    # Park HEAD on a branch nobody fetches into, so fetching the base branch is
    # not refused with "refusing to fetch into branch checked out at ...".
    repo.run(["symbolic-ref", "HEAD", f"refs/heads/{IDLE_BRANCH}"], check=True)
    repo.run(["config", "gc.auto", "0"], check=True)

    local_head = head_ref_name(pr)
    base_spec = f"+refs/heads/{base_ref}:refs/heads/{base_ref}"
    head_spec = f"+refs/heads/{head_ref_remote}:refs/heads/{local_head}"

    same_origin = head_url == base_url
    if same_origin:
        _fetch(repo, base_url, [base_spec, head_spec], depth=depth)
    else:
        _fetch(repo, base_url, [base_spec], depth=depth)
        _fetch(repo, head_url, [head_spec], depth=depth)

    _ensure_merge_base(repo, base_ref, local_head, base_url, head_url, depth=depth, same_origin=same_origin)
    return str(path)


def _fetch(repo: Repo, url: str, refspecs: list[str], *, depth: int | None) -> None:
    """Run one ``git fetch``, retrying without ``--depth`` when the remote refuses.

    Some servers (and every ``file://``-less local path) reject shallow fetches;
    a deep fetch is slower but correct, so it is worth the retry.
    """
    args = ["fetch", "--quiet", "--no-tags"]
    if depth:
        args.append(f"--depth={depth}")
    args.extend([url, *refspecs])
    result = repo.run_result(args)
    if result.ok:
        return
    if depth:
        retry = repo.run_result(["fetch", "--quiet", "--no-tags", url, *refspecs])
        if retry.ok:
            return
        result = retry
    raise GitError(
        f"failed to fetch {' '.join(refspecs)}",
        args=result.args,
        returncode=result.returncode,
        stderr=_redact(result.stderr, url),
    )


def _ensure_merge_base(repo: Repo, base_ref: str, head_ref: str, base_url: str, head_url: str, *, depth: int, same_origin: bool) -> None:
    """Deepen the history until the two refs share an ancestor, or give up.

    Giving up is not fatal: unrelated histories are a legitimate (if weird)
    state that the signals report on, so this only *tries* to make the merge
    base reachable.
    """
    attempts = 0
    current = depth
    while attempts < MAX_DEEPEN_ATTEMPTS:
        try:
            if repo.merge_base(base_ref, head_ref) is not None:
                return
        except GitError:
            return
        if not (repo.path / ".git" / "shallow").exists():
            return  # not a shallow clone; deepening cannot help
        current *= 4
        attempts += 1
        for url, refspec in _deepen_targets(base_ref, head_ref, base_url, head_url, same_origin):
            repo.run_result(["fetch", "--quiet", "--no-tags", f"--depth={current}", url, refspec])
    repo.run_result(["fetch", "--quiet", "--no-tags", "--unshallow", base_url])


def _deepen_targets(base_ref: str, head_ref: str, base_url: str, head_url: str, same_origin: bool) -> list[tuple[str, str]]:
    """``(url, refspec)`` pairs to re-fetch when deepening."""
    base = (base_url, f"+refs/heads/{base_ref}:refs/heads/{base_ref}")
    if same_origin:
        return [base]
    return [base, (head_url, f"+refs/heads/{head_ref}:refs/heads/{head_ref}")]


def cleanup(path: str) -> None:
    """Remove a temporary checkout, ignoring errors.

    Cleanup failures must never mask the real exception; they are logged only.
    """
    if not path:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 - defensive; rmtree already swallows most
        logger.warning("could not clean up %s: %s", path, exc)


def _post_failure_comment(client: Any, pr_number: int, message: str) -> None:
    """Replace the PR comment with a short failure note, best effort.

    Uses the same marker as a real report so it *updates* the existing comment
    instead of leaving a stale, now-wrong report next to a new complaint.
    """
    from mergesignal.report.github_comment import COMMENT_MARKER

    body = f"## MergeSignal\n\n{message}\n\nNo report was produced for this push.\n\n{COMMENT_MARKER}\n"
    try:
        client.upsert_comment(pr_number, body)
    except Exception as exc:  # noqa: BLE001 - we are already on the failure path
        logger.warning("could not post failure comment on #%s: %s", pr_number, exc)


def _remaining(deadline: float) -> float:
    """Seconds left before ``deadline``, never negative."""
    return max(deadline - time.monotonic(), 0.0)


def _check_deadline(deadline: float) -> None:
    """Raise :class:`RunTimeout` when the per-run budget is spent."""
    if time.monotonic() >= deadline:
        raise RunTimeout("per-run timeout exceeded")


def _redact(text: str, url: str) -> str:
    """Strip credentials out of git's stderr before it reaches a log."""
    if "@" in url:
        scheme, _, rest = url.partition("://")
        credentials, at, host = rest.rpartition("@")
        if at and credentials:
            text = text.replace(f"{scheme}://{credentials}@{host}", f"{scheme}://***@{host}")
            secret = credentials.partition(":")[2]
            if secret:
                text = text.replace(secret, "***")
    return text
