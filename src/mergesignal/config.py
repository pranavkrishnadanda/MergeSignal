"""``.mergesignal.yaml`` loading and validation.

Configuration is entirely optional: a repository with no config file gets
:func:`default_config`, which is tuned to be useful out of the box. Unknown keys
are rejected loudly — a typo like ``hot_path:`` silently doing nothing is worse
than a startup error.

Example ``.mergesignal.yaml``::

    enabled_signals: [conflicts, semantic, risk]
    severity_threshold: high
    risk_weights:
      churn: 0.3
      co_change: 0.2
      hot_paths: 0.3
      test_coverage: 0.1
      diff_size: 0.1
    hot_paths:
      - "src/mergesignal/models.py"
      - "src/**/migrations/**"
    github:
      repo: owner/name
      comment: true
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from mergesignal.models import SIGNAL_NAMES, Severity

#: Filename looked for when walking up from the repository root.
CONFIG_FILENAME = ".mergesignal.yaml"

#: Also accepted, for people who spell YAML the other way.
ALT_CONFIG_FILENAMES = (".mergesignal.yml",)

#: Risk factors recognised by :mod:`mergesignal.signals.risk`.
RISK_FACTORS: tuple[str, ...] = ("churn", "co_change", "hot_paths", "test_coverage", "diff_size")


class ConfigError(ValueError):
    """Raised when a config file exists but cannot be read or validated.

    The CLI turns this into exit code 2 with the file path in the message; a
    *missing* file is never an error.
    """


class RiskWeights(BaseModel):
    """Relative weights for the five risk factors (FR-6).

    Weights need not sum to 1 — :mod:`mergesignal.signals.risk` normalises by the
    sum of the weights of the factors it could actually compute, so disabling a
    factor by setting it to ``0`` does not deflate the whole score.
    """

    model_config = ConfigDict(extra="forbid")

    churn: float = Field(
        default=0.25, ge=0.0, description="Weight of recent commit churn on the touched paths."
    )
    co_change: float = Field(
        default=0.20,
        ge=0.0,
        description="Weight of historical co-change coupling to files not in this diff.",
    )
    hot_paths: float = Field(
        default=0.25, ge=0.0, description="Weight of matching a configured hot path glob."
    )
    test_coverage: float = Field(
        default=0.15,
        ge=0.0,
        description="Weight of the test-coverage proxy (changed file lacking a sibling test file).",
    )
    diff_size: float = Field(
        default=0.15,
        ge=0.0,
        description="Weight of raw diff size (files touched and lines churned).",
    )

    @model_validator(mode="after")
    def _check_nonzero(self) -> RiskWeights:
        """At least one factor must carry weight, else the score is meaningless."""
        if self.total <= 0:
            raise ValueError("risk_weights must not all be zero")
        return self

    @property
    def total(self) -> float:
        """Sum of all weights, used as the normalisation denominator."""
        return sum(self.as_dict().values())

    def as_dict(self) -> dict[str, float]:
        """Weights keyed by :data:`RISK_FACTORS` name."""
        return {name: float(getattr(self, name)) for name in RISK_FACTORS}


class GitHubConfig(BaseModel):
    """GitHub integration settings. Entirely optional — MergeSignal works offline.

    Secrets are **never** read from this file (NFR-5): tokens and private keys
    come from the environment only. The fields here name *which* environment
    variables to read, so a repo can point at its own naming scheme.
    """

    model_config = ConfigDict(extra="forbid")

    repo: str | None = Field(
        default=None,
        description="'owner/name' slug used by --prs when it cannot be inferred from the remote.",
    )
    api_url: str = Field(
        default="https://api.github.com",
        description="REST API base URL; override for GitHub Enterprise.",
    )
    comment: bool = Field(
        default=True, description="Whether the service posts/updates a PR comment after analysis."
    )
    check_run: bool = Field(
        default=False, description="Whether to additionally publish a check run."
    )
    token_env: str = Field(
        default="GITHUB_TOKEN",
        description="Environment variable holding a PAT, when not using App auth.",
    )
    app_id_env: str = Field(
        default="MERGESIGNAL_APP_ID", description="Environment variable holding the GitHub App id."
    )
    private_key_env: str = Field(
        default="MERGESIGNAL_PRIVATE_KEY",
        description="Environment variable holding the App PEM (contents or path).",
    )
    webhook_secret_env: str = Field(
        default="MERGESIGNAL_WEBHOOK_SECRET",
        description="Environment variable holding the webhook HMAC secret.",
    )
    max_prs: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Cap on open PRs fetched for cross-PR overlap, keeping NFR-1 honest.",
    )

    @model_validator(mode="after")
    def _check_repo_slug(self) -> GitHubConfig:
        """``repo`` must look like ``owner/name`` when present."""
        if self.repo is not None and self.repo.count("/") != 1:
            raise ValueError(f"github.repo must be 'owner/name', got {self.repo!r}")
        return self

    def token(self) -> str | None:
        """Read the PAT from the configured environment variable, or ``None``."""
        return os.environ.get(self.token_env) or None


class AnalysisConfig(BaseModel):
    """Bounds that keep analysis within NFR-1 (typical PR under 10 seconds)."""

    model_config = ConfigDict(extra="forbid")

    max_files: int = Field(
        default=500,
        ge=1,
        description="Stop indexing symbols past this many changed files; the diff is still reported.",
    )
    max_file_bytes: int = Field(
        default=1_000_000,
        ge=1024,
        description="Skip tree-sitter parsing of files larger than this; they degrade to textual handling.",
    )
    history_days: int = Field(
        default=90, ge=1, description="How far back churn/co-change statistics look."
    )
    history_max_commits: int = Field(
        default=2000,
        ge=1,
        description="Hard cap on commits traversed by `git log` for history stats.",
    )
    git_timeout_seconds: float = Field(
        default=30.0, gt=0, description="Per-invocation timeout for every git subprocess."
    )


class Config(BaseModel):
    """Validated ``.mergesignal.yaml`` contents.

    Construct via :func:`load_config`; instantiate directly only in tests.
    """

    model_config = ConfigDict(extra="forbid")

    enabled_signals: list[str] = Field(
        default_factory=lambda: list(SIGNAL_NAMES),
        description="Which signal engines to run, in SIGNAL_NAMES order regardless of listed order.",
    )
    severity_threshold: Severity = Field(
        default="high", description="Findings at or above this severity make the CLI exit 1 (FR-7)."
    )
    risk_weights: RiskWeights = Field(
        default_factory=RiskWeights, description="Weights for the risk score factors."
    )
    hot_paths: list[str] = Field(
        default_factory=list,
        description="Glob patterns (fnmatch/pathlib style, '**' supported) marking high-blast-radius files.",
    )
    ignore_paths: list[str] = Field(
        default_factory=lambda: [".git/**"],
        description="Glob patterns excluded from all analysis, e.g. vendored or generated code.",
    )
    github: GitHubConfig = Field(
        default_factory=GitHubConfig, description="GitHub integration settings."
    )
    analysis: AnalysisConfig = Field(
        default_factory=AnalysisConfig, description="Performance and traversal bounds."
    )
    source_path: str | None = Field(
        default=None,
        description="Absolute path the config was loaded from; None when defaults were used.",
    )

    @model_validator(mode="after")
    def _check_signals(self) -> Config:
        """Reject unknown signal names and normalise to canonical order."""
        unknown = [s for s in self.enabled_signals if s not in SIGNAL_NAMES]
        if unknown:
            raise ValueError(f"unknown signals {unknown!r}; valid names are {list(SIGNAL_NAMES)}")
        self.enabled_signals = [s for s in SIGNAL_NAMES if s in set(self.enabled_signals)]
        return self

    def is_enabled(self, signal_name: str) -> bool:
        """``True`` when engine ``signal_name`` should run."""
        return signal_name in self.enabled_signals

    def is_hot(self, path: str) -> bool:
        """``True`` when ``path`` matches any :attr:`hot_paths` glob.

        Matching is done with :meth:`pathlib.PurePosixPath.full_match` semantics
        via :func:`match_any`, so ``src/**/models.py`` behaves as expected.
        """
        return match_any(path, self.hot_paths)

    def is_ignored(self, path: str) -> bool:
        """``True`` when ``path`` matches any :attr:`ignore_paths` glob."""
        return match_any(path, self.ignore_paths)


def match_any(path: str, patterns: list[str]) -> bool:
    """Return ``True`` when POSIX ``path`` matches any glob in ``patterns``.

    Supports ``*``, ``?``, ``[...]`` and recursive ``**``. A pattern with no
    slash also matches on basename alone (so ``*.sql`` matches
    ``db/migrations/001.sql``), which is what people expect from a hot-path list.
    Invalid patterns are ignored rather than raising.
    """
    from fnmatch import fnmatch
    from pathlib import PurePosixPath

    if not patterns:
        return False
    candidate = PurePosixPath(path)
    for pattern in patterns:
        if not pattern:
            continue
        try:
            if candidate.full_match(pattern):  # type: ignore[attr-defined]
                return True
        except (AttributeError, ValueError):
            if fnmatch(path, pattern):
                return True
        if "/" not in pattern and fnmatch(candidate.name, pattern):
            return True
    return False


def default_config() -> Config:
    """The configuration used when no ``.mergesignal.yaml`` exists."""
    return Config()


def find_config_file(start: str | os.PathLike[str]) -> Path | None:
    """Walk up from ``start`` looking for a config file.

    Stops at the filesystem root or at a directory containing ``.git`` (the
    repository root) — whichever comes first, inclusive. Returns ``None`` when
    nothing is found, which is a normal, non-error outcome.
    """
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        for name in (CONFIG_FILENAME, *ALT_CONFIG_FILENAMES):
            candidate = directory / name
            if candidate.is_file():
                return candidate
        if (directory / ".git").exists():
            break
    return None


def load_config(
    path: str | os.PathLike[str] | None = None, *, repo_path: str | os.PathLike[str] | None = None
) -> Config:
    """Load configuration, falling back to defaults.

    :param path: explicit config file (``--config``). When given and missing,
        that *is* an error — the user asked for a specific file.
    :param repo_path: directory to search upward from when ``path`` is ``None``.
        Defaults to the current working directory.
    :raises ConfigError: the file is unreadable, is not a YAML mapping, or fails
        validation. A merely absent file never raises.

    An empty file (or one containing only ``null``) is treated as "all defaults".
    """
    if path is not None:
        config_file = Path(path).expanduser()
        if not config_file.is_file():
            raise ConfigError(f"config file not found: {config_file}")
    else:
        found = find_config_file(repo_path or Path.cwd())
        if found is None:
            return default_config()
        config_file = found

    try:
        raw_text = config_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {config_file}: {exc}") from exc

    return config_from_text(raw_text, source=str(config_file.resolve()))


def config_from_text(text: str, *, source: str = "<config>") -> Config:
    """Validate config YAML supplied as a string rather than a file.

    Used by the webhook service, whose fetch-only clone has no working tree to
    search: the config blob is read straight out of the base ref via
    ``git show`` and handed here.

    :param source: label used in error messages (a path or ``ref:path``).
    :raises ConfigError: the text is not valid YAML, is not a mapping, or fails
        validation.
    """
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {source}: {exc}") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{source} must contain a YAML mapping, got {type(data).__name__}")

    data = dict(data)
    data["source_path"] = source
    try:
        return Config.model_validate(data)
    except ValueError as exc:
        raise ConfigError(f"invalid configuration in {source}: {exc}") from exc
