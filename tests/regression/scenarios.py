"""The golden regression corpus: one scripted repository per scenario.

Each :class:`Scenario` owns three things:

* a **RepoBuilder script** that builds a real git repository from scratch,
* the **analyze arguments** (base, head, overlap branches, config tweaks) that
  MergeSignal should be run with, and
* a **name**, which is also the filename of its JSON snapshot under
  ``tests/regression/snapshots/``.

Why snapshots at all? The four signal engines are heuristics stitched across
five subsystems (git, diff, symbols, signals, render). Unit tests pin each
subsystem's behaviour in isolation; nothing else pins what the *combination*
produces. A refactor that silently stops populating ``base_references`` — which
is precisely the bug this corpus was written after finding — breaks no unit
test and quietly guts the semantic signal. The snapshots catch exactly that.

Determinism
-----------

:class:`~tests.helpers.repo_builder.RepoBuilder` fixes author identity and
commit timestamps, so everything in a report is reproducible except commit
shas, the wall-clock ``generated_at`` and the temp-directory ``repo_path``.
:func:`normalize_report` erases those three and nothing else — see its
docstring for why each is unavoidable rather than merely inconvenient.

Regenerating
------------

Snapshots are *deliberately* tedious to regenerate, so a behaviour change has to
be an explicit decision::

    pytest tests/regression --snapshot-update

Then read ``git diff tests/regression/snapshots`` line by line. Every changed
line is a change in what MergeSignal tells its users.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mergesignal.cli import build_context, collect_others, run_pipeline
from mergesignal.config import Config
from mergesignal.git.repo import Repo
from mergesignal.models import Report
from tests.helpers.repo_builder import RepoBuilder

#: Directory holding the committed golden files.
SNAPSHOT_DIR = Path(__file__).parent / "snapshots"

#: Stand-in written over every commit sha. Shas depend on the repository path
#: and on git's own version, so they cannot be pinned.
SHA_PLACEHOLDER = "<sha>"

#: Stand-in written over the temp-directory repository path.
PATH_PLACEHOLDER = "<repo>"

#: Stand-in written over ``generated_at``.
TIME_PLACEHOLDER = "<generated-at>"

#: Matches a full or abbreviated git object name. Seven characters is git's own
#: minimum abbreviation, so anything shorter is a word, not a sha.
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


# --------------------------------------------------------------- the scenario


@dataclass(frozen=True)
class Scenario:
    """One golden corpus entry: how to build the repo and how to analyse it."""

    name: str
    """Snapshot filename stem and pytest parameter id."""

    description: str
    """What this scenario is meant to prove, shown when the snapshot mismatches."""

    script: Callable[[RepoBuilder], None]
    """Builds the repository. Receives a fresh builder in an empty directory."""

    base: str = "main"
    """Ref to merge into."""

    head: str = "feature"
    """Ref to merge from."""

    branches: list[str] = field(default_factory=list)
    """Overlap (S3) inputs — local branch names passed as ``--branches``."""

    config_overrides: dict[str, Any] = field(default_factory=dict)
    """Fields set on the default :class:`~mergesignal.config.Config`."""

    @property
    def snapshot_path(self) -> Path:
        """Location of this scenario's committed golden JSON."""
        return SNAPSHOT_DIR / f"{self.name}.json"

    def build(self, path: Path) -> Path:
        """Run the RepoBuilder script in ``path`` and return the repository."""
        builder = RepoBuilder(path)
        self.script(builder)
        return builder.build()

    def config(self) -> Config:
        """The configuration to analyse with.

        Built from :class:`Config` defaults rather than :func:`load_config` so
        that a stray ``.mergesignal.yaml` anywhere above the temp directory
        cannot reach into the corpus and change a snapshot.
        """
        config = Config()
        for key, value in self.config_overrides.items():
            setattr(config, key, value)
        return config

    def run(self, repo_path: Path) -> Report:
        """Analyse the built repository exactly as ``mergesignal analyze`` would."""
        config = self.config()
        repo = Repo(str(repo_path), timeout=config.analysis.git_timeout_seconds)
        others = collect_others(repo, config, branches=self.branches or None, prs=None, base=self.base)
        ctx = build_context(repo, self.base, self.head, config, others=others)
        return run_pipeline(ctx, config)


# ------------------------------------------------------------- normalisation


def normalize_report(report: Report, repo_path: Path) -> dict[str, Any]:
    """Strip the three genuinely non-reproducible parts of a report.

    ``repo_path``
        An absolute temp directory, different on every run and every machine.
    ``generated_at``
        Wall-clock time.
    commit shas
        RepoBuilder pins commit *content* and *timestamps*, but a commit's sha
        still varies: some git versions fold the repository path into the
        initial commit, and the object format itself may differ (sha1 vs
        sha256). Shas are therefore blanked wherever they appear — including
        inside finding titles, details and evidence, where they leak as part of
        ref names.

    Everything else — statuses, summaries, severities, confidences, counts,
    conflict text, evidence structure — is compared verbatim. ``duration_ms`` is
    dropped rather than blanked because it is pure timing noise that would
    otherwise add a meaningless line to every signal in every snapshot.
    """
    payload = json.loads(report.model_dump_json())
    payload["repo_path"] = PATH_PLACEHOLDER
    payload["generated_at"] = TIME_PLACEHOLDER
    return _scrub(payload, repo_path=str(Path(repo_path).resolve()))


def _scrub(value: Any, *, repo_path: str) -> Any:
    """Recursively replace temp paths and shas, and drop ``duration_ms`` keys."""
    if isinstance(value, dict):
        return {
            key: _scrub(item, repo_path=repo_path)
            for key, item in value.items()
            if key != "duration_ms"
        }
    if isinstance(value, list):
        return [_scrub(item, repo_path=repo_path) for item in value]
    if isinstance(value, str):
        return scrub_text(value, repo_path=repo_path)
    return value


def scrub_text(text: str, *, repo_path: str) -> str:
    """Replace the temp repository path and any git object name in ``text``.

    Exported because the CLI end-to-end test needs the same treatment on raw
    stdout, where the report has not been parsed into a model yet.
    """
    if repo_path:
        text = text.replace(repo_path, PATH_PLACEHOLDER)
    return _SHA_RE.sub(SHA_PLACEHOLDER, text)


def write_snapshot(scenario: Scenario, payload: dict[str, Any]) -> None:
    """Write ``payload`` as this scenario's golden file (``--snapshot-update``)."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    scenario.snapshot_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_snapshot(scenario: Scenario) -> dict[str, Any]:
    """Load this scenario's golden file.

    :raises FileNotFoundError: the snapshot has never been generated.
    """
    return json.loads(scenario.snapshot_path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------- scripts
#
# Each script below is the *specification* of its scenario. Keep them small and
# literal: a reader debugging a snapshot diff must be able to see the whole
# repository at a glance.


def _clean_merge(builder: RepoBuilder) -> None:
    """Two branches touching disjoint files — the happy path.

    Proves a clean run stays clean: conflicts ``ok``, semantic ``ok``, and no
    engine inventing a finding out of an ordinary merge.
    """
    builder.file("alpha.py", "def alpha():\n    return 1\n")
    builder.file("tests/test_alpha.py", "from alpha import alpha\n\n\ndef test_alpha():\n    assert alpha() == 1\n")
    builder.commit("initial")
    builder.branch("feature")
    builder.file("beta.py", "def beta():\n    return 2\n")
    builder.file("tests/test_beta.py", "from beta import beta\n\n\ndef test_beta():\n    assert beta() == 2\n")
    builder.commit("add beta")
    builder.checkout("main")
    builder.file("alpha.py", "def alpha():\n    return 1\n\n\ndef alpha_extra():\n    return 10\n")
    builder.commit("extend alpha")


def _textual_conflict(builder: RepoBuilder) -> None:
    """Both branches rewriting the same two regions of one file.

    Two edits far enough apart that git cannot coalesce them, so S1 must report
    exactly two :class:`~mergesignal.models.ConflictRegion` findings rather than
    one whole-file conflict.
    """
    original = "\n".join(f"line {index}" for index in range(1, 41)) + "\n"
    builder.file("settings.conf", original).commit("initial settings")

    builder.branch("feature")
    feature = original.replace("line 3\n", "line 3 FEATURE\n").replace("line 30\n", "line 30 FEATURE\n")
    builder.file("settings.conf", feature).commit("feature tweaks")

    builder.checkout("main")
    main = original.replace("line 3\n", "line 3 MAIN\n").replace("line 30\n", "line 30 MAIN\n")
    builder.file("settings.conf", main).commit("main tweaks")


def _rename_vs_new_caller(builder: RepoBuilder) -> None:
    """The classic clean-but-broken merge (FR-4, renamed-old-name-referenced).

    ``feature`` renames ``load_settings`` to ``read_settings``; ``main``
    meanwhile adds a brand-new module that imports and calls the *old* name.
    Neither side touches the other's file, so git merges without a murmur and
    the result raises ``ImportError`` on the first run.
    """
    builder.file(
        "settings.py",
        "def load_settings(path):\n"
        '    """Read settings from path."""\n'
        "    return {'path': path}\n",
    )
    builder.commit("add settings")

    builder.branch("feature")
    builder.file(
        "settings.py",
        "def read_settings(path):\n"
        '    """Read settings from path."""\n'
        "    return {'path': path}\n",
    )
    builder.commit("rename load_settings to read_settings")

    builder.checkout("main")
    builder.file(
        "startup.py",
        "from settings import load_settings\n\n\ndef boot():\n    return load_settings('app.ini')\n",
    )
    builder.commit("add startup calling load_settings")


def _signature_change_new_callers(builder: RepoBuilder) -> None:
    """Signature widened on one side, new call sites added on the other (FR-4).

    ``feature`` gives ``render`` two extra required parameters; ``main`` adds
    two fresh single-argument call sites. Again textually clean, again broken.
    """
    builder.file(
        "render.py",
        "def render(template):\n    return template.upper()\n",
    )
    builder.commit("add render")

    builder.branch("feature")
    builder.file(
        "render.py",
        "def render(template, context, strict):\n    return template.upper()\n",
    )
    builder.commit("require context and strict")

    builder.checkout("main")
    builder.file(
        "pages.py",
        "from render import render\n\n\ndef home():\n    return render('home')\n\n\ndef about():\n    return render('about')\n",
    )
    builder.commit("add pages calling render")


def _overlap_three_branches(builder: RepoBuilder) -> None:
    """One candidate against three peers, one per collision granularity (FR-5).

    The candidate re-signatures ``alpha``. Against that:

    * ``peer-symbol`` **also re-signatures** ``alpha`` — the sharpest collision
      there is, reported at ``symbol`` granularity.
    * ``peer-hunk`` edits ``beta``'s body, close enough to the candidate's hunk
      to matter but a different declaration — ``hunk`` granularity.
    * ``peer-file`` edits an unrelated file and must produce **no finding**.

    Note what "symbol overlap" needs: a symbol *change*, not merely an edit
    inside a declaration. ``diff_symbols`` infers ``modified`` from a
    declaration's line span, so an equal-length body rewrite is invisible to it
    by design (the index does not retain bodies). Two branches whose edits are
    both body-only therefore collide at hunk level, which is the honest answer.
    """
    core = (
        "def alpha(x):\n"
        "    return x\n"
        "\n"
        "\n"
        "def beta(y):\n"
        "    return y\n"
        "\n"
        "\n"
        "def gamma(z):\n"
        "    return z\n"
    )
    builder.file("core.py", core)
    builder.file("docs.md", "# docs\n")
    builder.commit("initial core")

    builder.branch("feature")
    builder.file("core.py", core.replace("def alpha(x):", "def alpha(x, verbose):"))
    builder.commit("candidate adds verbose to alpha")

    builder.checkout("main")
    builder.branch("peer-symbol")
    builder.file("core.py", core.replace("def alpha(x):", "def alpha(x, retries):"))
    builder.commit("peer-symbol adds retries to alpha")

    builder.checkout("main")
    builder.branch("peer-hunk")
    builder.file("core.py", core.replace("    return y\n", "    return y * 2\n"))
    builder.commit("peer-hunk rewrites beta's body")

    builder.checkout("main")
    builder.branch("peer-file")
    builder.file("docs.md", "# docs\n\nnow with words\n")
    builder.commit("peer-file changes docs only")

    builder.checkout("main")


def _unsupported_language(builder: RepoBuilder) -> None:
    """A language with no tree-sitter grammar — must degrade, never crash.

    S2 has to report ``skipped`` with a reason. Reporting ``ok`` would be a
    lie: MergeSignal did not look, so it cannot say there is nothing there.
    """
    builder.file("pipeline.zzz", "BEGIN\n  step one\n  step two\nEND\n").commit("add pipeline")
    builder.branch("feature")
    builder.file("pipeline.zzz", "BEGIN\n  step one\n  step two improved\nEND\n").commit("improve step two")
    builder.checkout("main")
    builder.file("notes.zzz", "BEGIN\n  unrelated\nEND\n").commit("add notes")


def _binary_file(builder: RepoBuilder) -> None:
    """A binary blob rewritten on both sides.

    The conflict must be reported as a whole-file binary conflict with no line
    detail, and no engine may try to parse the bytes as source (NFR-3).
    """
    builder.binary("logo.png", bytes(range(256)) * 4).commit("add logo")
    builder.branch("feature")
    builder.binary("logo.png", bytes(range(255, -1, -1)) * 4).commit("feature logo")
    builder.checkout("main")
    builder.binary("logo.png", bytes([17]) * 1024).commit("main logo")


def _empty_diff(builder: RepoBuilder) -> None:
    """base == head: there is no change, so there is nothing to say.

    Every engine must report ``skipped`` (or a clean ``ok`` for conflicts, which
    can honestly answer "already merged") and the CLI must exit 0.
    """
    builder.file("only.py", "def only():\n    return 1\n").commit("the only commit")


#: The corpus. Order is the order snapshots are listed and tests are run.
SCENARIOS: list[Scenario] = [
    Scenario(
        name="clean_merge",
        description="disjoint files on both sides; merges cleanly with no conflict or semantic findings",
        script=_clean_merge,
    ),
    Scenario(
        name="textual_conflict",
        description="same file edited in two places on both sides; exactly two conflict regions",
        script=_textual_conflict,
    ),
    Scenario(
        name="rename_vs_new_caller",
        description="feature renames a function while main adds a caller of the old name",
        script=_rename_vs_new_caller,
    ),
    Scenario(
        name="signature_change_new_callers",
        description="feature widens a signature while main adds call sites using the old arity",
        script=_signature_change_new_callers,
    ),
    Scenario(
        name="overlap_three_branches",
        description="candidate versus three peer branches: one symbol collision, one hunk collision, one miss",
        script=_overlap_three_branches,
        branches=["peer-symbol", "peer-hunk", "peer-file"],
    ),
    Scenario(
        name="unsupported_language",
        description="only .zzz files change; semantic analysis degrades to skipped",
        script=_unsupported_language,
    ),
    Scenario(
        name="binary_file",
        description="a binary blob conflicts; reported without line detail and never parsed",
        script=_binary_file,
    ),
    Scenario(
        name="empty_diff",
        description="base == head; every engine skips and the CLI exits 0",
        script=_empty_diff,
        base="main",
        head="main",
    ),
]

#: ``name -> Scenario`` for tests that want one specific entry.
BY_NAME: dict[str, Scenario] = {scenario.name: scenario for scenario in SCENARIOS}
