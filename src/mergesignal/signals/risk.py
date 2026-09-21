"""S4 — risk / blast-radius score. **Owned by Agent E.**

Produces a 0-100 :class:`~mergesignal.models.RiskScore` (FR-6) from five
configurable factors, each normalised to 0.0-1.0 before weighting so that the
weights in ``.mergesignal.yaml`` mean what they look like they mean:

``churn``
    How much the touched paths have been changing lately (hot code breaks).
``co_change``
    How many files historically move together with the touched ones but are
    *not* in this diff — the "you forgot to update its partner" factor.
``hot_paths``
    Whether the diff touches a path matching ``config.hot_paths``.
``test_coverage``
    Proxy only: does each changed source file have a plausible sibling or
    mirrored test file, and did the diff touch it? Absence is a risk signal,
    never a claim about real coverage.
``diff_size``
    Files touched and lines churned, saturating (a 400-file diff is not twice as
    risky as a 200-file one).

Every factor emits its own :class:`~mergesignal.models.Finding` so the score is
**explainable**: a number nobody can decompose is a number nobody trusts.

Severity of the overall score is bucketed by :data:`RISK_LEVELS`.
"""

from __future__ import annotations

import posixpath
from typing import Any

from mergesignal.models import AnalysisContext, Diff, Finding, RiskScore, Severity, Signal

#: Name registered in :data:`mergesignal.signals.REGISTRY`.
NAME = "risk"

#: Score thresholds (inclusive lower bound) -> severity bucket.
RISK_LEVELS: tuple[tuple[float, Severity], ...] = (
    (80.0, "critical"),
    (60.0, "high"),
    (30.0, "medium"),
    (0.0, "low"),
)

#: Diff size above which the diff_size factor saturates at 1.0.
DIFF_SIZE_SATURATION_LINES = 2000

#: File count above which the diff_size factor saturates at 1.0.
DIFF_SIZE_SATURATION_FILES = 50

#: Commits-per-file within the history window at which churn saturates at 1.0.
CHURN_SATURATION_COMMITS = 10

#: Minimum number of shared commits before two paths count as coupled.
CO_CHANGE_MIN_SUPPORT = 2

#: Penalty applied to a changed source file whose test exists but was not touched.
UNTOUCHED_TEST_PENALTY = 0.5

#: How sure each factor's own finding is. Hot paths and diff size are facts;
#: churn and coupling are history-derived; the test factor is an explicit proxy.
FACTOR_CONFIDENCE: dict[str, str] = {
    "churn": "medium",
    "co_change": "medium",
    "hot_paths": "high",
    "test_coverage": "low",
    "diff_size": "high",
}

#: Human labels used in factor findings.
FACTOR_LABELS: dict[str, str] = {
    "churn": "recent churn on the touched paths",
    "co_change": "historically coupled files missing from this diff",
    "hot_paths": "hot paths touched",
    "test_coverage": "changed source files without a touched test",
    "diff_size": "diff size",
}

#: Directory names that mark a path as test code.
TEST_DIR_NAMES = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing"})


# --------------------------------------------------------------------- entry


def analyze(ctx: AnalysisContext) -> Signal:
    """Compute the risk score and its explanation. Never raises.

    Populates ``metadata["risk_score"]`` with the
    :class:`~mergesignal.models.RiskScore` dump; the CLI lifts it onto
    ``Report.risk_score``. Returns ``skipped`` for an empty diff.

    Factors whose inputs are unavailable (no git history, history walk timed
    out) are **omitted** from the weighted average rather than scored zero, so a
    shallow clone does not silently make everything look safe.
    """
    try:
        return _analyze(ctx)
    except Exception as exc:  # noqa: BLE001 - engines never raise (see signals/__init__)
        return Signal.error(NAME, f"{type(exc).__name__}: {exc}")


def _analyze(ctx: AnalysisContext) -> Signal:
    """The real body of :func:`analyze`, wrapped by its exception trap."""
    diff = ctx.head_diff
    if diff is None:
        return Signal.skipped(NAME, "no diff available; risk not scored")
    if not diff.files:
        return Signal.skipped(NAME, "empty diff; nothing to score", files=0)

    weights = _weights(ctx)
    measured: dict[str, tuple[float, dict[str, Any]]] = {}
    unavailable: list[str] = []

    stats = _history(ctx)
    tree = _tree_files(ctx)
    for name, (value, evidence) in (
        ("churn", _churn_factor(ctx, stats)),
        ("co_change", _co_change_factor(ctx, stats)),
        ("hot_paths", _hot_paths_factor(ctx)),
        ("test_coverage", _test_coverage_factor(ctx, tree)),
        ("diff_size", (score_diff_size(diff), _diff_size_evidence(diff))),
    ):
        if value is None:
            unavailable.append(name)
            continue
        measured[name] = (round(float(value), 4), evidence)

    factors = {name: value for name, (value, _evidence) in measured.items()}
    risk = build_score(factors, weights)

    findings = [
        finding_for_factor(
            name,
            value,
            weights.get(name, 0.0),
            _factor_detail(name, value, evidence),
            evidence,
        )
        for name, (value, evidence) in sorted(measured.items())
        if value > 0
    ]

    metadata: dict[str, Any] = {
        "risk_score": risk.model_dump(),
        "score": risk.score,
        "level": risk.level,
        "factors": risk.factors,
        "unavailable_factors": unavailable,
        "files": len(diff.files),
        "churned_lines": diff.total_churn,
    }
    summary = f"risk {risk.score:.0f}/100 ({risk.level})"
    if unavailable:
        summary += f"; {', '.join(unavailable)} unavailable"
    return Signal.from_findings(NAME, findings, summary, **metadata)


# -------------------------------------------------------------------- factors


def score_churn(ctx: AnalysisContext) -> float | None:
    """Normalised recent-churn factor, or ``None`` when history is unavailable."""
    return _churn_factor(ctx, _history(ctx))[0]


def score_co_change(ctx: AnalysisContext) -> float | None:
    """Normalised coupling factor: coupled files *missing* from the diff."""
    return _co_change_factor(ctx, _history(ctx))[0]


def score_hot_paths(ctx: AnalysisContext) -> float | None:
    """Fraction of changed files matching ``config.hot_paths`` globs.

    Returns ``None`` when no hot paths are configured, so an unconfigured repo
    is not penalised or rewarded.
    """
    return _hot_paths_factor(ctx)[0]


def score_test_coverage(ctx: AnalysisContext) -> float | None:
    """Proxy factor: changed source files with no corresponding test change.

    Uses :func:`test_path_candidates`. Returns ``None`` when the diff contains no
    source files at all (docs-only change).
    """
    return _test_coverage_factor(ctx, _tree_files(ctx))[0]


def score_diff_size(diff: Diff) -> float:
    """Saturating size factor from file count and churned lines.

    The two components are combined with ``max``: a 60-file one-line-each diff
    and a single 3000-line rewrite are both maximally risky by size, and neither
    should be able to dilute the other.
    """
    files = len(diff.files) / DIFF_SIZE_SATURATION_FILES
    lines = diff.total_churn / DIFF_SIZE_SATURATION_LINES
    return min(1.0, max(files, lines))


def _churn_factor(ctx: AnalysisContext, stats: Any | None) -> tuple[float | None, dict[str, Any]]:
    """Churn factor plus its evidence: per-path commit counts in the window."""
    if stats is None or stats.commits_scanned == 0:
        return (None, {})
    paths = _touched_paths(ctx)
    if not paths:
        return (None, {})
    counts = {path: stats.churn.get(path, 0) for path in paths}
    mean = sum(counts.values()) / len(counts)
    value = min(1.0, mean / CHURN_SATURATION_COMMITS)
    evidence = {
        "window_days": stats.days,
        "commits_scanned": stats.commits_scanned,
        "truncated": stats.truncated,
        "mean_commits_per_file": round(mean, 3),
        "saturation_commits": CHURN_SATURATION_COMMITS,
        "per_path": dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]),
    }
    return (value, evidence)


def _co_change_factor(ctx: AnalysisContext, stats: Any | None) -> tuple[float | None, dict[str, Any]]:
    """Coupling factor plus the coupled-but-absent partners it found."""
    if stats is None or stats.commits_scanned == 0:
        return (None, {})
    paths = _touched_paths(ctx)
    if not paths:
        return (None, {})

    in_diff = set(paths)
    missing: dict[str, list[str]] = {}
    for path in paths:
        partners = sorted(
            other
            for other, count in stats.co_change.get(path, {}).items()
            if count >= CO_CHANGE_MIN_SUPPORT and other not in in_diff
        )
        if partners:
            missing[path] = partners[:10]

    value = len(missing) / len(paths)
    evidence = {
        "window_days": stats.days,
        "min_support": CO_CHANGE_MIN_SUPPORT,
        "paths_with_missing_partners": len(missing),
        "paths_considered": len(paths),
        "missing_partners": dict(sorted(missing.items())[:10]),
    }
    return (value, evidence)


def _hot_paths_factor(ctx: AnalysisContext) -> tuple[float | None, dict[str, Any]]:
    """Hot-path factor plus the matching paths."""
    config = getattr(ctx, "config", None)
    patterns = list(getattr(config, "hot_paths", []) or [])
    diff = ctx.head_diff
    if not patterns or diff is None or not diff.files:
        return (None, {})

    matcher = getattr(config, "is_hot", None)
    if not callable(matcher):
        from mergesignal.config import match_any

        def matcher(path: str) -> bool:  # type: ignore[misc]
            return match_any(path, patterns)

    matched = sorted(changed.path for changed in diff.files if matcher(changed.path))
    value = len(matched) / len(diff.files)
    evidence = {
        "patterns": patterns,
        "matched": matched[:25],
        "matched_count": len(matched),
        "files": len(diff.files),
    }
    return (value, evidence)


def _test_coverage_factor(ctx: AnalysisContext, tree: set[str] | None) -> tuple[float | None, dict[str, Any]]:
    """Test-proxy factor plus per-file verdicts.

    Each changed source file scores ``0.0`` when a plausible test path is in the
    diff, :data:`UNTOUCHED_TEST_PENALTY` when one merely exists in the tree, and
    ``1.0`` when none can be found at all.
    """
    diff = ctx.head_diff
    if diff is None:
        return (None, {})

    sources = [
        changed
        for changed in diff.files
        if not changed.is_binary and not changed.is_deleted and changed.language and not is_test_path(changed.path)
    ]
    if not sources:
        return (None, {})

    touched = {changed.path for changed in diff.files} | {
        changed.old_path for changed in diff.files if changed.old_path
    }

    penalties: dict[str, float] = {}
    verdicts: dict[str, str] = {}
    for changed in sources:
        candidates = test_path_candidates(changed.path)
        if any(candidate in touched for candidate in candidates):
            penalties[changed.path] = 0.0
            verdicts[changed.path] = "test changed"
        elif tree is not None and any(candidate in tree for candidate in candidates):
            penalties[changed.path] = UNTOUCHED_TEST_PENALTY
            verdicts[changed.path] = "test exists but untouched"
        else:
            penalties[changed.path] = 1.0
            verdicts[changed.path] = "no test found"

    value = sum(penalties.values()) / len(penalties)
    evidence = {
        "source_files": len(sources),
        "without_tests": sum(1 for penalty in penalties.values() if penalty >= 1.0),
        "untouched_tests": sum(1 for penalty in penalties.values() if 0 < penalty < 1.0),
        "tree_scanned": tree is not None,
        "verdicts": dict(sorted(verdicts.items())[:25]),
    }
    return (value, evidence)


def _diff_size_evidence(diff: Diff) -> dict[str, Any]:
    """Evidence for the diff-size factor: the raw counts behind the ratio."""
    return {
        "files": len(diff.files),
        "churned_lines": diff.total_churn,
        "saturation_files": DIFF_SIZE_SATURATION_FILES,
        "saturation_lines": DIFF_SIZE_SATURATION_LINES,
    }


# ------------------------------------------------------------------ scoring


def level_for(score: float) -> Severity:
    """Bucket a 0-100 score into a severity using :data:`RISK_LEVELS`."""
    for threshold, level in RISK_LEVELS:
        if score >= threshold:
            return level
    return RISK_LEVELS[-1][1]


def build_score(factors: dict[str, float], weights: dict[str, float]) -> RiskScore:
    """Combine normalised factors and weights into a :class:`RiskScore`.

    Normalises by the sum of the weights of the factors actually present, so a
    missing factor rescales rather than deflates the result.
    """
    applied = {name: float(weights.get(name, 0.0)) for name in factors}
    denominator = sum(applied.values())
    if denominator <= 0:
        score = 0.0
    else:
        weighted = sum(value * applied[name] for name, value in factors.items())
        score = max(0.0, min(100.0, 100.0 * weighted / denominator))
    score = round(score, 1)
    return RiskScore(
        score=score,
        level=level_for(score),
        factors={name: round(float(value), 4) for name, value in sorted(factors.items())},
        weights={name: round(weight, 4) for name, weight in sorted(applied.items())},
    )


def finding_for_factor(name: str, value: float, weight: float, detail: str, evidence: dict) -> Finding:
    """One explainability finding per contributing factor.

    Severity is ``low`` below 0.5, ``medium`` below 0.8, ``high`` above — a
    factor's finding describes *that factor*, not the overall score.
    """
    severity: Severity = "low" if value < 0.5 else ("medium" if value < 0.8 else "high")
    payload = {"factor": name, "value": round(float(value), 4), "weight": round(float(weight), 4)}
    payload.update(evidence or {})
    return Finding(
        signal=NAME,
        severity=severity,
        confidence=FACTOR_CONFIDENCE.get(name, "medium"),  # type: ignore[arg-type]
        title=f"risk factor {name}: {value:.0%} (weight {weight:g})",
        detail=detail,
        evidence=payload,
    )


# -------------------------------------------------------------- test mapping


def test_path_candidates(path: str) -> list[str]:
    """Plausible test-file paths for a source path.

    Covers the common conventions: ``tests/test_<name>.py``, ``<dir>/test_<name>.py``,
    ``<name>_test.go``, ``<name>.test.ts``, ``<name>.spec.ts``, and the mirrored
    ``src/x/y.py`` -> ``tests/x/test_y.py`` layout. Returns an ordered list of
    candidates; the caller checks which exist in the repository or the diff.
    """
    directory, filename = posixpath.split(path)
    stem, extension = posixpath.splitext(filename)
    if not stem:
        return []

    def joined(*parts: str) -> str:
        return posixpath.normpath(posixpath.join(*[part for part in parts if part]))

    candidates: list[str] = []

    if extension == ".py":
        candidates += [
            joined(directory, f"test_{stem}.py"),
            joined(directory, f"{stem}_test.py"),
            joined(directory, "tests", f"test_{stem}.py"),
        ]
    elif extension == ".go":
        candidates += [joined(directory, f"{stem}_test.go")]
    elif extension in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        candidates += [
            joined(directory, f"{stem}.test{extension}"),
            joined(directory, f"{stem}.spec{extension}"),
            joined(directory, "__tests__", f"{stem}.test{extension}"),
            joined(directory, "__tests__", f"{stem}{extension}"),
        ]
    elif extension == ".java":
        candidates += [joined(directory, f"{stem}Test.java")]
        if "src/main/java" in path:
            candidates.append(path.replace("src/main/java", "src/test/java").replace(f"{stem}.java", f"{stem}Test.java"))
    elif extension == ".rs":
        candidates += [joined(directory, f"{stem}_test.rs"), joined("tests", f"{stem}.rs")]
    else:
        candidates += [joined(directory, f"test_{stem}{extension}"), joined(directory, f"{stem}_test{extension}")]

    if extension not in (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        return _unique_candidates(candidates, path)

    # Mirrored layouts: src/pkg/mod.py -> tests/pkg/test_mod.py, tests/test_mod.py, ...
    mirrored_dir = directory
    for prefix in ("src/", "lib/", "app/"):
        if mirrored_dir == prefix.rstrip("/"):
            mirrored_dir = ""
        elif mirrored_dir.startswith(prefix):
            mirrored_dir = mirrored_dir[len(prefix) :]

    test_name = f"test_{stem}{extension}" if extension == ".py" else f"{stem}.test{extension}"
    for root in ("tests", "test"):
        candidates += [
            joined(root, mirrored_dir, test_name),
            joined(root, test_name),
            joined(root, "unit", test_name),
            joined(root, "integration", test_name),
        ]
        if mirrored_dir != directory:
            candidates.append(joined(root, directory, test_name))

    return _unique_candidates(candidates, path)


def _unique_candidates(candidates: list[str], path: str) -> list[str]:
    """De-duplicate candidate paths, preserving order and dropping ``path`` itself."""
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate and candidate != path and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def is_test_path(path: str) -> bool:
    """``True`` when ``path`` itself looks like test code.

    Test files are excluded from the denominator of the test-coverage proxy:
    "this test file has no test" is not a useful risk signal.
    """
    parts = path.split("/")
    if any(part.lower() in TEST_DIR_NAMES for part in parts[:-1]):
        return True
    filename = parts[-1]
    stem, extension = posixpath.splitext(filename)
    lowered = stem.lower()
    return (
        lowered.startswith("test_")
        or lowered.startswith("test.")
        or lowered.endswith("_test")
        or lowered.endswith(".test")
        or lowered.endswith(".spec")
        or (extension == ".java" and stem.endswith("Test"))
    )


# ------------------------------------------------------------------- helpers


def _weights(ctx: AnalysisContext) -> dict[str, float]:
    """Risk weights from the config, or the built-in defaults."""
    risk_weights = getattr(getattr(ctx, "config", None), "risk_weights", None)
    as_dict = getattr(risk_weights, "as_dict", None)
    if callable(as_dict):
        return {name: float(weight) for name, weight in as_dict().items()}

    from mergesignal.config import RiskWeights

    return RiskWeights().as_dict()


def _touched_paths(ctx: AnalysisContext) -> list[str]:
    """Post-image paths of the candidate diff, plus pre-image paths of renames."""
    diff = ctx.head_diff
    if diff is None:
        return []
    paths: list[str] = []
    for changed in diff.files:
        paths.append(changed.path)
        if changed.old_path and changed.old_path != changed.path:
            paths.append(changed.old_path)
    return sorted(set(paths))


def _repo(ctx: AnalysisContext) -> Any | None:
    """Open the analysed repository, or ``None`` when that is not possible."""
    try:
        from mergesignal.git.repo import Repo

        timeout = getattr(getattr(ctx, "config", None), "analysis", None)
        seconds = getattr(timeout, "git_timeout_seconds", None)
        repo = Repo(ctx.repo_path, timeout=float(seconds)) if isinstance(seconds, (int, float)) else Repo(ctx.repo_path)
        return repo if repo.is_repository() else None
    except Exception:  # noqa: BLE001 - history is optional; absence is not an error
        return None


def _history(ctx: AnalysisContext) -> Any | None:
    """Bounded history stats for the touched paths, cached on the context.

    Returns ``None`` when the repository or its history is unavailable, which is
    how the churn and co-change factors signal "omit me" rather than "zero".

    Walking history costs one bounded ``git log``; :func:`_analyze` does it once
    and threads the result into both factors, so this is only re-run when a
    caller uses the single-factor helpers directly.
    """
    stats = None
    paths = _touched_paths(ctx)
    repo = _repo(ctx) if paths else None
    if repo is not None:
        from mergesignal.git.history import collect_history

        analysis = getattr(getattr(ctx, "config", None), "analysis", None)
        days = int(getattr(analysis, "history_days", 90) or 90)
        max_commits = int(getattr(analysis, "history_max_commits", 2000) or 2000)
        for ref in _history_refs(ctx, repo):
            try:
                stats = collect_history(repo, paths, ref=ref, days=days, max_commits=max_commits)
            except Exception:  # noqa: BLE001 - a shallow clone or unborn branch is not an error
                stats = None
                continue
            break

    return stats


def _history_refs(ctx: AnalysisContext, repo: Any) -> list[str]:
    """Refs to try for the history walk, base first.

    The base branch is preferred over the head: the candidate's own commits are
    the change being judged, not evidence of how volatile the code already is.
    """
    refs = [ref for ref in (ctx.merge_base, ctx.base, "HEAD") if ref]
    try:
        return [ref for ref in refs if repo.ref_exists(ref)] or ["HEAD"]
    except Exception:  # noqa: BLE001
        return ["HEAD"]


def _tree_files(ctx: AnalysisContext) -> set[str] | None:
    """Every tracked path at the head ref, or ``None`` when unavailable.

    One ``git ls-tree`` answers "does a test file for this exist?" for the whole
    diff; failing to get it downgrades the factor to diff-only evidence rather
    than failing the signal.
    """
    files: set[str] | None = None
    repo = _repo(ctx)
    if repo is not None:
        try:
            files = set(repo.list_files_at(ctx.head))
        except Exception:  # noqa: BLE001 - optional enrichment only
            files = None

    return files


def _factor_detail(name: str, value: float, evidence: dict[str, Any]) -> str:
    """Human explanation of one factor's contribution."""
    label = FACTOR_LABELS.get(name, name)
    head = f"{label}: {value:.0%} of the maximum."
    if name == "churn":
        return f"{head} {evidence.get('mean_commits_per_file', 0)} commits per touched file in the last {evidence.get('window_days')} days."
    if name == "co_change":
        return (
            f"{head} {evidence.get('paths_with_missing_partners', 0)} of {evidence.get('paths_considered', 0)} "
            "touched files usually change together with files this diff does not touch."
        )
    if name == "hot_paths":
        return f"{head} {evidence.get('matched_count', 0)} of {evidence.get('files', 0)} changed files match a configured hot path."
    if name == "test_coverage":
        return (
            f"{head} {evidence.get('without_tests', 0)} changed source file(s) have no test file at all and "
            f"{evidence.get('untouched_tests', 0)} have one that this change does not touch. This is a proxy, not real coverage."
        )
    if name == "diff_size":
        return f"{head} {evidence.get('files', 0)} files and {evidence.get('churned_lines', 0)} churned lines."
    return head
