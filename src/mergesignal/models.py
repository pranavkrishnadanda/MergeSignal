"""Pydantic v2 data contracts — **the** contract between every MergeSignal module.

This module is deliberately dependency-free (beyond pydantic) so that it can be
imported by the CLI, the analysis engines, the renderers and the web service
without pulling in git, tree-sitter or httpx.

Design rules for everything in here:

* Models are **frozen-ish by convention**, not enforced: engines build them once
  and pass them around. Do not mutate a model you did not construct.
* Every field is typed and documented. ``model_dump()`` output is the JSON that
  users, golden snapshots and the GitHub comment renderer all consume, so field
  names are part of the public API — renaming one is a breaking change.
* Collections default to empty, never ``None``, so consumers can iterate blindly.
* Line numbers are **1-based** and ranges are **inclusive of start, exclusive of
  end** (see :class:`LineRange`), matching ``git``'s own hunk semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Schema version stamped onto every :class:`Report`. Bump on breaking JSON changes.
REPORT_SCHEMA_VERSION = "1"

Severity = Literal["low", "medium", "high", "critical"]
"""How much a finding should worry the reader, ascending."""

Confidence = Literal["low", "medium", "high"]
"""How sure the heuristic is. Semantic findings are heuristics, never proofs."""

ChangeKind = Literal["added", "removed", "modified", "renamed", "signature_changed"]
"""What happened to a symbol between two refs."""

SymbolKind = Literal["function", "class", "method", "variable", "import"]
"""The sort of declaration a :class:`Symbol` represents."""

SignalStatus = Literal["ok", "findings", "skipped", "error"]
"""Outcome of running one signal engine.

``ok``
    Ran to completion, found nothing worth reporting.
``findings``
    Ran to completion and produced at least one :class:`Finding`.
``skipped``
    Deliberately not run (disabled in config, unsupported language, no inputs).
``error``
    Blew up. ``summary`` carries the reason; the CLI still renders a report.
"""

OutputFormat = Literal["text", "json", "md"]
"""Renderer selected by ``--format``."""

#: Ordering used whenever severities must be sorted or compared numerically.
SEVERITY_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}

#: Ordering used whenever confidences must be sorted or compared numerically.
CONFIDENCE_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

#: Canonical signal names, in the order they are rendered in reports.
SIGNAL_NAMES: tuple[str, ...] = ("conflicts", "semantic", "overlap", "risk")


def severity_at_least(severity: Severity, threshold: Severity) -> bool:
    """Return ``True`` when ``severity`` is as bad as, or worse than, ``threshold``.

    Used by the CLI exit-code contract (FR-7) and by config filtering.
    """
    return SEVERITY_ORDER[severity] >= SEVERITY_ORDER[threshold]


def _utcnow() -> datetime:
    """Timezone-aware ``now`` — reports must never carry naive timestamps."""
    return datetime.now(UTC)


class MergeSignalModel(BaseModel):
    """Base class applying the shared pydantic configuration.

    ``extra="forbid"`` is intentional: a typo in a field name from a downstream
    engine should fail at construction time, not silently vanish from the JSON.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class LineRange(MergeSignalModel):
    """A half-open range of 1-based line numbers: ``[start, end)``.

    An *empty* range (``start == end``) is legal and meaningful: it marks an
    insertion point in a file where the side in question contributed no lines
    (exactly how ``git diff`` reports ``@@ -0,0 +1,3 @@``).
    """

    start: int = Field(
        ...,
        ge=0,
        description="First line of the range, 1-based inclusive. 0 only for empty ranges at the start of a file.",
    )
    end: int = Field(
        ..., ge=0, description="One past the last line of the range, 1-based exclusive."
    )

    @model_validator(mode="after")
    def _check_ordering(self) -> LineRange:
        """Reject inverted ranges — they are always a bug in the caller's parser."""
        if self.end < self.start:
            raise ValueError(f"LineRange end ({self.end}) must be >= start ({self.start})")
        return self

    @property
    def length(self) -> int:
        """Number of lines covered by this range (0 for an insertion point)."""
        return self.end - self.start

    def overlaps(self, other: LineRange) -> bool:
        """Return ``True`` if the two ranges share at least one line.

        Two empty ranges at the same offset do *not* overlap (no shared line),
        which keeps hunk-overlap scoring in :mod:`mergesignal.signals.overlap`
        from flagging two unrelated pure insertions at the same offset.
        """
        if self.length == 0 or other.length == 0:
            return False
        return self.start < other.end and other.start < self.end


class Hunk(MergeSignalModel):
    """One contiguous changed region of one file, as emitted by ``git diff``.

    ``base_range`` describes lines as they exist in the *old* (pre-image) file and
    ``head_range`` the *new* (post-image) file. For a pure addition ``base_range``
    is empty; for a pure deletion ``head_range`` is empty.
    """

    file_path: str = Field(
        ...,
        description="Repository-relative POSIX path of the file this hunk belongs to (the post-image path for renames).",
    )
    base_range: LineRange = Field(..., description="Lines touched on the pre-image (base) side.")
    head_range: LineRange = Field(..., description="Lines touched on the post-image (head) side.")
    added_lines: list[str] = Field(
        default_factory=list,
        description="Raw text of lines added by this hunk, without the leading '+' and without trailing newline.",
    )
    removed_lines: list[str] = Field(
        default_factory=list,
        description="Raw text of lines removed by this hunk, without the leading '-' and without trailing newline.",
    )
    header: str | None = Field(
        default=None,
        description="The literal '@@ ... @@' header line, including any trailing function-context hint git supplies.",
    )

    @property
    def is_pure_addition(self) -> bool:
        """``True`` when nothing was removed (base side empty)."""
        return not self.removed_lines

    @property
    def is_pure_deletion(self) -> bool:
        """``True`` when nothing was added (head side empty)."""
        return not self.added_lines


class DiffFile(MergeSignalModel):
    """All changes to a single file between two refs.

    Edge cases downstream code must respect:

    * ``is_binary`` files carry no hunks — never attempt to parse or index them.
    * A deleted file has ``head_range``-empty hunks and ``is_deleted`` set.
    * A rename sets ``old_path``; ``path`` is always the post-image path.
    * ``language`` is ``None`` for unsupported/unknown extensions, which routes
      the file to the textual fallback path rather than tree-sitter.
    """

    path: str = Field(
        ...,
        description="Repository-relative POSIX path in the head (post-image) tree; for deletions, the path it had in base.",
    )
    old_path: str | None = Field(
        default=None, description="Pre-image path when the file was renamed or copied, else None."
    )
    is_binary: bool = Field(
        default=False, description="True when git reported the file as binary; hunks will be empty."
    )
    is_new: bool = Field(
        default=False, description="True when the file did not exist in the base tree."
    )
    is_deleted: bool = Field(
        default=False, description="True when the file does not exist in the head tree."
    )
    hunks: list[Hunk] = Field(
        default_factory=list,
        description="Changed regions, in file order. Empty for binary files and pure mode changes.",
    )
    language: str | None = Field(
        default=None,
        description="tree-sitter language id ('python', 'javascript', ...) or None when unsupported.",
    )
    additions: int = Field(default=0, ge=0, description="Total added line count across all hunks.")
    deletions: int = Field(
        default=0, ge=0, description="Total removed line count across all hunks."
    )

    @model_validator(mode="after")
    def _check_flags(self) -> DiffFile:
        """A file cannot be both new and deleted, and binaries carry no hunks."""
        if self.is_new and self.is_deleted:
            raise ValueError(f"DiffFile {self.path!r} cannot be both new and deleted")
        if self.is_binary and self.hunks:
            raise ValueError(f"binary DiffFile {self.path!r} must not carry hunks")
        return self

    @property
    def is_rename(self) -> bool:
        """``True`` when the file moved, i.e. ``old_path`` differs from ``path``."""
        return self.old_path is not None and self.old_path != self.path

    @property
    def churn(self) -> int:
        """Added + deleted lines — the cheap "how big is this change" proxy."""
        return self.additions + self.deletions


class Diff(MergeSignalModel):
    """A whole structured diff between two refs.

    Produced by :mod:`mergesignal.analysis.diff`; carried around in
    :class:`AnalysisContext` so every signal shares one parse.
    """

    base: str = Field(..., description="Ref (or commit sha) the diff is measured from.")
    head: str = Field(..., description="Ref (or commit sha) the diff is measured to.")
    files: list[DiffFile] = Field(
        default_factory=list, description="Changed files, in git's output order."
    )

    @property
    def paths(self) -> set[str]:
        """Post-image paths of every changed file — the usual overlap key."""
        return {f.path for f in self.files}

    @property
    def total_churn(self) -> int:
        """Sum of added + deleted lines over all files."""
        return sum(f.churn for f in self.files)

    def by_path(self, path: str) -> DiffFile | None:
        """Return the :class:`DiffFile` for ``path``, or ``None`` when untouched."""
        for f in self.files:
            if f.path == path:
                return f
        return None


class Symbol(MergeSignalModel):
    """A declaration extracted from a source file by tree-sitter.

    ``signature`` is best-effort and language-dependent: for Python it is the
    parameter list as written; for Go the full func signature; for languages
    whose grammar does not expose one, ``None``. Callers must treat a ``None``
    signature as "unknown", never as "no parameters".
    """

    name: str = Field(..., description="Identifier as written in source (unqualified).")
    kind: SymbolKind = Field(..., description="Declaration flavour.")
    file: str = Field(..., description="Repository-relative POSIX path the declaration lives in.")
    line: int = Field(..., ge=1, description="1-based line of the declaration's name token.")
    end_line: int | None = Field(
        default=None,
        ge=1,
        description="1-based last line of the declaration's body, when the grammar gives it.",
    )
    signature: str | None = Field(
        default=None,
        description="Normalised signature text, or None when the grammar exposes none.",
    )
    parent: str | None = Field(
        default=None,
        description="Enclosing scope name (class for a method, module for a top-level function), or None.",
    )

    @property
    def qualified_name(self) -> str:
        """``Parent.name`` when nested, else just ``name`` — the index key for methods."""
        return f"{self.parent}.{self.name}" if self.parent else self.name


class SymbolChange(MergeSignalModel):
    """One symbol's fate between two refs.

    ``symbol`` always describes the symbol in its *most recent* observed state:
    for ``removed`` that is the base-side declaration (it no longer exists in
    head), for everything else the head-side declaration.
    """

    symbol: Symbol = Field(..., description="The symbol being described.")
    kind: ChangeKind = Field(..., description="What happened to it.")
    old_signature: str | None = Field(
        default=None, description="Signature on the base side, when known."
    )
    new_signature: str | None = Field(
        default=None, description="Signature on the head side, when known."
    )
    old_name: str | None = Field(
        default=None, description="Previous identifier when kind == 'renamed'."
    )
    old_file: str | None = Field(
        default=None, description="Previous file path when the declaration moved."
    )
    confidence: Confidence = Field(
        default="high",
        description="How sure the differ is; rename detection is a heuristic, so it is rarely 'high'.",
    )

    @model_validator(mode="after")
    def _check_kind_fields(self) -> SymbolChange:
        """Enforce the fields each ``kind`` is required to carry."""
        if self.kind == "renamed" and not self.old_name:
            raise ValueError("SymbolChange(kind='renamed') requires old_name")
        if self.kind == "signature_changed" and self.old_signature == self.new_signature:
            raise ValueError("SymbolChange(kind='signature_changed') requires differing signatures")
        return self


class Reference(MergeSignalModel):
    """A *usage* of a name (call, attribute access, import, type annotation).

    References are the other half of the semantic signal: a removed definition
    only matters if something still references it.
    """

    name: str = Field(..., description="Identifier being referenced, unqualified.")
    file: str = Field(..., description="Repository-relative POSIX path containing the usage.")
    line: int = Field(..., ge=1, description="1-based line of the usage.")
    context: str = Field(
        default="", description="Source line text (trimmed) for human-readable evidence."
    )
    column: int | None = Field(
        default=None, ge=0, description="0-based column of the usage when available."
    )


class ConflictRegion(MergeSignalModel):
    """One conflicted region produced by simulating a merge.

    ``ours`` is the base side, ``theirs`` the head side, matching git's own
    labelling during ``git merge <head>`` performed while on ``base``.

    For binary conflicts the ranges are empty and both text fields are ``None`` —
    engines must mark the file as conflicted without pretending to have content.
    """

    file: str = Field(..., description="Repository-relative POSIX path of the conflicted file.")
    ours_range: LineRange = Field(
        ..., description="Lines contributed by the base ('ours') side in the merged buffer."
    )
    theirs_range: LineRange = Field(
        ..., description="Lines contributed by the head ('theirs') side in the merged buffer."
    )
    ours_text: str | None = Field(
        default=None,
        description="Base-side text of the region; None for binary or unreadable blobs.",
    )
    theirs_text: str | None = Field(
        default=None,
        description="Head-side text of the region; None for binary or unreadable blobs.",
    )
    base_text: str | None = Field(
        default=None, description="Merge-base text of the region when the diff3 style is available."
    )
    is_binary: bool = Field(
        default=False,
        description="True when the conflict is a binary/whole-file conflict with no line detail.",
    )


class MergeSimulation(MergeSignalModel):
    """Result of simulating ``merge head into base`` without touching user state."""

    base: str = Field(..., description="Ref merged into.")
    head: str = Field(..., description="Ref merged from.")
    merge_base: str | None = Field(
        default=None, description="Sha of the merge base, or None for unrelated histories."
    )
    clean: bool = Field(..., description="True when the merge would apply without conflicts.")
    conflicted_files: list[str] = Field(
        default_factory=list, description="Repository-relative paths git reported as conflicted."
    )
    regions: list[ConflictRegion] = Field(
        default_factory=list,
        description="Per-region conflict detail; may be shorter than conflicted_files for binary conflicts.",
    )
    tree_sha: str | None = Field(
        default=None,
        description="Sha of the tree written by `git merge-tree --write-tree`, when that path was used.",
    )
    strategy: str = Field(
        default="merge-tree",
        description="Which simulation path ran: 'merge-tree' or 'worktree-fallback'.",
    )
    up_to_date: bool = Field(
        default=False,
        description="True when head is already an ancestor of base — nothing to merge, not an error.",
    )

    @model_validator(mode="after")
    def _check_clean(self) -> MergeSimulation:
        """A clean merge cannot name conflicted files, and vice versa."""
        if self.clean and (self.conflicted_files or self.regions):
            raise ValueError("MergeSimulation marked clean but carries conflicts")
        return self


class Finding(MergeSignalModel):
    """One actionable observation produced by a signal engine.

    ``evidence`` is a free-form dict serialised verbatim into JSON output. Keep
    it JSON-primitive (str/int/float/bool/list/dict) so snapshots stay stable;
    put model dumps in it, not models.
    """

    signal: str = Field(
        ...,
        description="Name of the producing signal ('conflicts', 'semantic', 'overlap', 'risk').",
    )
    severity: Severity = Field(..., description="How bad this is.")
    confidence: Confidence = Field(
        default="medium", description="How sure the heuristic is that this is real."
    )
    title: str = Field(
        ..., min_length=1, description="One-line headline, rendered as-is in every format."
    )
    detail: str = Field(
        default="",
        description="Multi-line explanation, including what the reader should do about it.",
    )
    file: str | None = Field(
        default=None,
        description="Primary repository-relative path this finding points at, when there is one.",
    )
    line: int | None = Field(
        default=None, ge=1, description="Primary 1-based line within ``file``."
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict, description="Structured supporting data; JSON-primitive values only."
    )

    @model_validator(mode="after")
    def _check_line_needs_file(self) -> Finding:
        """A line number without a file is meaningless to the reader."""
        if self.line is not None and self.file is None:
            raise ValueError("Finding.line requires Finding.file")
        return self

    @property
    def sort_key(self) -> tuple[int, str, int]:
        """Deterministic ordering key: worst severity first, then file, then line.

        Renderers must sort with ``key=lambda f: f.sort_key`` so that JSON
        snapshots never churn on dict ordering.
        """
        return (-SEVERITY_ORDER[self.severity], self.file or "", self.line or 0)


class Signal(MergeSignalModel):
    """The output of one signal engine — always produced, even on failure.

    Every engine exposes ``analyze(ctx: AnalysisContext) -> Signal`` and **never
    raises**: exceptions are converted into ``status="error"`` with the reason in
    ``summary``, so a single broken engine cannot take down the report.
    """

    name: str = Field(..., description="Engine name; one of SIGNAL_NAMES.")
    status: SignalStatus = Field(..., description="Outcome — ok | findings | skipped | error.")
    findings: list[Finding] = Field(
        default_factory=list,
        description="Observations, unordered; renderers sort by Finding.sort_key.",
    )
    summary: str = Field(
        default="",
        description="One-line human summary; the failure reason when status == 'error', the skip reason when 'skipped'.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Engine-specific extras (counts, timings, strategy used). JSON-primitive values only.",
    )
    duration_ms: float | None = Field(
        default=None, ge=0, description="Wall-clock time the engine took, milliseconds."
    )

    @model_validator(mode="after")
    def _check_status_consistency(self) -> Signal:
        """Keep ``status`` and ``findings`` honest with one another."""
        if self.status == "ok" and self.findings:
            raise ValueError(f"Signal {self.name!r} has findings but status 'ok'; use 'findings'")
        if self.status == "findings" and not self.findings:
            raise ValueError(f"Signal {self.name!r} has status 'findings' but no findings")
        return self

    @property
    def max_severity(self) -> Severity | None:
        """Worst severity among findings, or ``None`` when there are none."""
        if not self.findings:
            return None
        return max((f.severity for f in self.findings), key=lambda s: SEVERITY_ORDER[s])

    @classmethod
    def error(cls, name: str, reason: str, **metadata: Any) -> Signal:
        """Build the canonical failure :class:`Signal` for engine ``name``.

        Used by the CLI pipeline to trap engine exceptions (including the
        ``NotImplementedError`` raised by not-yet-written engines) so that the
        report still renders end to end.
        """
        return cls(name=name, status="error", summary=reason, metadata=dict(metadata))

    @classmethod
    def skipped(cls, name: str, reason: str, **metadata: Any) -> Signal:
        """Build the canonical ``skipped`` :class:`Signal` for engine ``name``."""
        return cls(name=name, status="skipped", summary=reason, metadata=dict(metadata))

    @classmethod
    def from_findings(
        cls, name: str, findings: list[Finding], summary: str = "", **metadata: Any
    ) -> Signal:
        """Build a completed signal, choosing ``ok``/``findings`` from the list."""
        return cls(
            name=name,
            status="findings" if findings else "ok",
            findings=findings,
            summary=summary,
            metadata=dict(metadata),
        )


class RiskScore(MergeSignalModel):
    """Explainable 0-100 blast-radius score (FR-6).

    ``factors`` maps a factor name (``"churn"``, ``"co_change"``, ``"hot_paths"``,
    ``"test_coverage"``, ``"diff_size"``) to its normalised 0.0-1.0 contribution
    *before* weighting, so the renderer can show the arithmetic.
    """

    score: float = Field(
        ..., ge=0.0, le=100.0, description="Final weighted score, 0 (trivial) to 100 (terrifying)."
    )
    level: Severity = Field(..., description="Bucketed score for at-a-glance reading.")
    factors: dict[str, float] = Field(
        default_factory=dict,
        description="Normalised 0-1 contribution per factor, before weighting.",
    )
    weights: dict[str, float] = Field(
        default_factory=dict,
        description="Weights actually applied, echoed from config for explainability.",
    )


class Report(MergeSignalModel):
    """The complete answer to "what happens if this merges?".

    This is what ``--format json`` serialises and what the regression corpus
    snapshots. ``generated_at`` and absolute paths are the only non-deterministic
    parts; golden tests normalise them.
    """

    base: str = Field(..., description="Base ref as the user supplied it.")
    head: str = Field(..., description="Head ref as the user supplied it.")
    merge_base: str | None = Field(
        default=None,
        description="Resolved merge-base sha, or None for unrelated histories / unborn branches.",
    )
    signals: list[Signal] = Field(
        default_factory=list, description="One entry per enabled engine, in SIGNAL_NAMES order."
    )
    risk_score: RiskScore | None = Field(
        default=None, description="Populated when the risk engine ran successfully."
    )
    repo_path: str | None = Field(
        default=None,
        description="Absolute path of the analysed repository; normalised away in snapshots.",
    )
    generated_at: datetime = Field(
        default_factory=_utcnow, description="UTC timestamp the report was produced."
    )
    version: str = Field(
        default=REPORT_SCHEMA_VERSION,
        description="Report schema version; bumped on breaking JSON changes.",
    )

    @property
    def all_findings(self) -> list[Finding]:
        """Every finding from every signal, sorted deterministically."""
        return sorted((f for s in self.signals for f in s.findings), key=lambda f: f.sort_key)

    @property
    def max_severity(self) -> Severity | None:
        """Worst severity anywhere in the report, or ``None`` when clean."""
        severities = [s.max_severity for s in self.signals if s.max_severity]
        if not severities:
            return None
        return max(severities, key=lambda s: SEVERITY_ORDER[s])

    @property
    def has_errors(self) -> bool:
        """``True`` when any engine failed — drives CLI exit code 2."""
        return any(s.status == "error" for s in self.signals)

    def signal(self, name: str) -> Signal | None:
        """Return the signal named ``name``, or ``None`` when it did not run."""
        for s in self.signals:
            if s.name == name:
                return s
        return None

    def exceeds(self, threshold: Severity) -> bool:
        """``True`` when any finding is at or above ``threshold`` (FR-7, exit 1)."""
        worst = self.max_severity
        return worst is not None and severity_at_least(worst, threshold)


class BranchDiff(MergeSignalModel):
    """One *other* change set that the candidate may collide with (S3 input).

    Produced from either a local branch (``--branches``) or a GitHub PR
    (``--prs``); ``pr_number``/``url`` are populated only in the latter case.
    """

    name: str = Field(..., description="Branch name or 'PR #123' label used in findings.")
    head: str = Field(..., description="Head ref/sha of this change set.")
    base: str = Field(
        ...,
        description="Base ref/sha it is measured against (usually the same base as the candidate).",
    )
    diff: Diff = Field(..., description="Structured diff of this change set.")
    symbols: list[Symbol] = Field(
        default_factory=list,
        description="Symbols this change set *changed* (added, removed, renamed or re-signatured) since its merge base, when indexed. Not every symbol in the touched files: symbol-level overlap means 'we both edited this declaration', and the whole-file reading would make any two edits to one module look like a collision on all of it.",
    )
    pr_number: int | None = Field(
        default=None, ge=1, description="GitHub PR number when sourced from --prs."
    )
    url: str | None = Field(
        default=None, description="Web URL of the PR/branch for linking in findings."
    )
    author: str | None = Field(
        default=None, description="Author login/name, for human-readable collision findings."
    )


class AnalysisContext(MergeSignalModel):
    """Everything the four engines need, computed once and shared.

    Built by :func:`mergesignal.cli.build_context`. Engines treat it as
    read-only. Fields are populated best-effort: an engine whose inputs are
    missing must return ``status="skipped"`` with a reason rather than raising.

    ``base_diff`` is *merge-base → base* and ``head_diff`` is *merge-base → head*,
    so the two sides can be compared symmetrically (what base did vs what head
    did since they parted ways). The plain *base → head* diff of the change under
    review is ``head_diff`` whenever base is itself the merge base, which is the
    common PR case.
    """

    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, arbitrary_types_allowed=True
    )

    repo_path: str = Field(..., description="Absolute path to the repository working directory.")
    base: str = Field(..., description="Base ref as supplied by the user.")
    head: str = Field(..., description="Head ref as supplied by the user.")
    merge_base: str | None = Field(
        default=None, description="Resolved merge-base sha, None for unrelated/unborn histories."
    )
    base_diff: Diff | None = Field(
        default=None, description="Structured diff merge-base -> base (what the base side changed)."
    )
    head_diff: Diff | None = Field(
        default=None, description="Structured diff merge-base -> head (what the head side changed)."
    )
    base_symbols: list[Symbol] = Field(
        default_factory=list, description="Symbols defined on the base side of the touched files."
    )
    head_symbols: list[Symbol] = Field(
        default_factory=list, description="Symbols defined on the head side of the touched files."
    )
    base_references: list[Reference] = Field(
        default_factory=list, description="Name usages observed on the base side."
    )
    head_references: list[Reference] = Field(
        default_factory=list, description="Name usages observed on the head side."
    )
    base_changes: list[SymbolChange] = Field(
        default_factory=list, description="Symbol changes merge-base -> base."
    )
    head_changes: list[SymbolChange] = Field(
        default_factory=list, description="Symbol changes merge-base -> head."
    )
    others: list[BranchDiff] = Field(
        default_factory=list,
        description="Other branches/PRs to check for collisions (S3). Empty means 'skip overlap'.",
    )
    config: Any = Field(
        default=None,
        description="The loaded mergesignal.config.Config; typed as Any to keep this module import-free.",
    )

    @property
    def touched_paths(self) -> set[str]:
        """Union of paths changed on either side — the usual analysis frontier."""
        paths: set[str] = set()
        for d in (self.base_diff, self.head_diff):
            if d is not None:
                paths |= d.paths
        return paths
