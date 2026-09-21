"""Analysis pipeline for webhook-triggered runs. **Owned by Agent F.**

One job: given a PR, produce a report and update its comment.

Steps, all inside an isolated temporary directory that is removed in a
``finally`` block no matter what:

1. Shallow-clone (or fetch into a cached bare mirror) just the base and head
   refs. Fork PRs need the head fetched from the fork's clone URL.
2. Build an :class:`~mergesignal.models.AnalysisContext` and run the signal
   pipeline — reuse :func:`mergesignal.cli.run_pipeline`, never reimplement it.
3. Render via :mod:`mergesignal.report.github_comment` and upsert the comment.

A per-run timeout bounds the whole thing; exceeding it posts a comment saying
the analysis timed out rather than leaving the PR with a stale report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mergesignal.models import Report

#: Depth used for shallow fetches; enough history for merge-base resolution in
#: the common case, with a documented fallback to deepening when it is not.
DEFAULT_FETCH_DEPTH = 50


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


def analyze_pull_request(repo_slug: str, pr_number: int, *, token: str | None = None, api_url: str = "https://api.github.com", work_dir: str | None = None, timeout_seconds: float = 300.0, post_comment: bool = True) -> WorkerResult:
    """Full clone -> analyse -> comment pipeline for one PR.

    Never raises: every failure (clone error, timeout, GitHub 403) is captured
    in :attr:`WorkerResult.error` so the webhook handler can log it and return a
    2xx rather than triggering GitHub's redelivery storm.
    """
    raise NotImplementedError


def prepare_checkout(repo_slug: str, pr: Any, work_dir: str, *, token: str | None = None, depth: int = DEFAULT_FETCH_DEPTH) -> str:
    """Fetch base and head refs into a fresh repository under ``work_dir``.

    :returns: path to the prepared repository.
    :raises GitError: the fetch failed.

    Deepens the fetch automatically when the merge base is not reachable at the
    requested depth; a PR branched from an old commit must still analyse.
    """
    raise NotImplementedError


def cleanup(path: str) -> None:
    """Remove a temporary checkout, ignoring errors.

    Cleanup failures must never mask the real exception; they are logged only.
    """
    raise NotImplementedError
