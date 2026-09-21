"""RepoBuilder — script real git repositories in a temp directory.

Shared test infrastructure for every agent. Integration and regression tests
build genuine repositories rather than mocking git, because the whole product
rests on git's actual behaviour (``merge-tree`` conflict output, rename
detection, ``--name-status`` quirks) and a mock would only test our own
assumptions.

Everything is fluent and deterministic::

    repo = (
        RepoBuilder(tmp_path)
        .file("app.py", "def greet(name):\\n    return f'hi {name}'\\n")
        .commit("initial")
        .branch("feature")
        .file("app.py", "def greet(name, loud=False):\\n    return 'HI'\\n")
        .commit("add loud flag")
        .checkout("main")
        .file("caller.py", "from app import greet\\ngreet('ada')\\n")
        .commit("add caller")
        .build()
    )

Determinism guarantees (so golden snapshots do not churn):

* Fixed author/committer name, email and timestamp unless overridden. Each
  commit advances the clock by :data:`COMMIT_INTERVAL_SECONDS`, so history order
  is stable and ``--since`` windows behave predictably.
* ``init.defaultBranch=main``, no GPG signing, no global config
  (``GIT_CONFIG_NOSYSTEM``), ``core.autocrlf=false``.
* Commit shas are *not* deterministic across runs (tree content plus fixed time
  would make them so, but the initial commit picks up the repo path in some git
  versions); assert on content, not on shas.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: Default identity for every commit, keeping author-based stats reproducible.
DEFAULT_AUTHOR_NAME = "Test Author"
DEFAULT_AUTHOR_EMAIL = "author@example.com"

#: Base timestamp for the first commit; later commits advance from here.
DEFAULT_START_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

#: Seconds added to the clock for each successive commit.
COMMIT_INTERVAL_SECONDS = 3600

#: Branch created by ``git init``.
DEFAULT_BRANCH = "main"


class RepoBuilderError(RuntimeError):
    """A builder operation failed — almost always a bug in the test itself."""


@dataclass
class CommitRecord:
    """One commit made by the builder, for tests that need to assert on history."""

    sha: str
    message: str
    branch: str
    paths: list[str] = field(default_factory=list)
    timestamp: datetime = DEFAULT_START_TIME


class RepoBuilder:
    """Fluent builder for real git repositories in a temporary directory.

    :param path: directory to create the repository in (typically pytest's
        ``tmp_path``). Created if missing; must be empty or not yet a repo.
    :param bare: initialise a bare repository (for clone/fetch tests).
    :param default_branch: name of the initial branch.
    :param start_time: timestamp of the first commit.
    :param author: ``(name, email)`` used when a commit does not override it.

    Every mutating method returns ``self`` so calls chain. :meth:`build` returns
    the repository path, which is what most tests actually want to pass to
    :class:`~mergesignal.git.repo.Repo`.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        bare: bool = False,
        default_branch: str = DEFAULT_BRANCH,
        start_time: datetime = DEFAULT_START_TIME,
        author: tuple[str, str] = (DEFAULT_AUTHOR_NAME, DEFAULT_AUTHOR_EMAIL),
    ) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.default_branch = default_branch
        self.author_name, self.author_email = author
        self._clock = start_time
        self._commits: list[CommitRecord] = []
        self._staged: set[str] = set()
        self._init(bare=bare)

    # ----------------------------------------------------------------- setup

    def _init(self, *, bare: bool) -> None:
        """Run ``git init`` and apply the deterministic local configuration."""
        args = ["init", "--quiet", f"--initial-branch={self.default_branch}"]
        if bare:
            args.append("--bare")
        self.git(*args)
        for key, value in (
            ("user.name", self.author_name),
            ("user.email", self.author_email),
            ("commit.gpgsign", "false"),
            ("tag.gpgsign", "false"),
            ("core.autocrlf", "false"),
            ("core.quotepath", "false"),
            ("merge.conflictstyle", "merge"),
            ("advice.detachedHead", "false"),
            ("gc.auto", "0"),
        ):
            self.git("config", key, value)

    def _env(self, *, timestamp: datetime | None = None, name: str | None = None, email: str | None = None) -> dict[str, str]:
        """Environment forcing a deterministic identity and clock onto git."""
        when = (timestamp or self._clock).isoformat()
        author_name = name or self.author_name
        author_email = email or self.author_email
        return {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
            "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_DATE": when,
        }

    def git(self, *args: str, check: bool = True, env: dict[str, str] | None = None, input_text: str | None = None) -> str:
        """Run a raw git command in the repository and return stripped stdout.

        Exposed so tests can reach for anything the builder does not wrap.

        :raises RepoBuilderError: git exited non-zero and ``check`` is true.
        """
        completed = subprocess.run(  # test helper: fixed binary, no shell
            ["git", *args],
            cwd=str(self.path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env or self._env(),
            input=input_text,
            check=False,
        )
        if check and completed.returncode != 0:
            raise RepoBuilderError(f"git {' '.join(args)} failed ({completed.returncode}): {completed.stderr.strip()}")
        return (completed.stdout or "").strip()

    # ------------------------------------------------------------- authoring

    def file(self, path: str, content: str, *, mode: int | None = None) -> RepoBuilder:
        """Write a text file (creating parent directories) and stage it.

        Content is written verbatim with ``\\n`` line endings; a trailing newline
        is added when missing, because a file without one makes every diff
        against it noisy in a way that hides the change under test.
        """
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if content and not content.endswith("\n"):
            content += "\n"
        target.write_text(content, encoding="utf-8", newline="\n")
        if mode is not None:
            target.chmod(mode)
        self.git("add", "--", path)
        self._staged.add(path)
        return self

    def binary(self, path: str, data: bytes) -> RepoBuilder:
        """Write and stage a binary file — the "must not crash" fixture."""
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.git("add", "--", path)
        self._staged.add(path)
        return self

    def append(self, path: str, content: str) -> RepoBuilder:
        """Append to an existing file and stage it.

        :raises RepoBuilderError: the file does not exist.
        """
        target = self.path / path
        if not target.exists():
            raise RepoBuilderError(f"cannot append to missing file: {path}")
        existing = target.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        return self.file(path, existing + content)

    def remove(self, path: str) -> RepoBuilder:
        """Delete a tracked file and stage the deletion."""
        self.git("rm", "--quiet", "-f", "--", path)
        self._staged.add(path)
        return self

    def move(self, src: str, dst: str) -> RepoBuilder:
        """Rename a tracked file and stage the rename — the FR-4 rename fixture."""
        (self.path / dst).parent.mkdir(parents=True, exist_ok=True)
        self.git("mv", "--", src, dst)
        self._staged.update({src, dst})
        return self

    def commit(self, message: str = "commit", *, author: tuple[str, str] | None = None, timestamp: datetime | None = None, allow_empty: bool = False) -> RepoBuilder:
        """Commit everything staged so far and advance the deterministic clock.

        :param author: ``(name, email)`` override for this commit only, so
            author-share tests can produce multiple contributors.
        :param timestamp: explicit commit time; otherwise the internal clock is
            used and then advanced by :data:`COMMIT_INTERVAL_SECONDS`.
        :param allow_empty: permit a commit with no staged changes.
        :raises RepoBuilderError: nothing to commit and ``allow_empty`` is false.
        """
        when = timestamp or self._clock
        name, email = author or (self.author_name, self.author_email)
        args = ["commit", "--quiet", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        self.git(*args, env=self._env(timestamp=when, name=name, email=email))
        sha = self.git("rev-parse", "HEAD")
        self._commits.append(
            CommitRecord(sha=sha, message=message, branch=self.current_branch() or "", paths=sorted(self._staged), timestamp=when)
        )
        self._staged.clear()
        if timestamp is None:
            self._clock += timedelta(seconds=COMMIT_INTERVAL_SECONDS)
        return self

    def commit_file(self, path: str, content: str, message: str | None = None) -> RepoBuilder:
        """Shorthand for :meth:`file` followed by :meth:`commit`."""
        return self.file(path, content).commit(message or f"update {path}")

    # -------------------------------------------------------------- branches

    def branch(self, name: str, *, checkout: bool = True, start_point: str | None = None) -> RepoBuilder:
        """Create a branch and (by default) switch to it.

        :param start_point: branch from this ref instead of ``HEAD``.
        :raises RepoBuilderError: the branch already exists.
        """
        args = ["branch", name]
        if start_point:
            args.append(start_point)
        self.git(*args)
        return self.checkout(name) if checkout else self

    def checkout(self, ref: str, *, create: bool = False, detach: bool = False) -> RepoBuilder:
        """Switch the working tree to ``ref``.

        :param create: create the branch if it does not exist.
        :param detach: check out in detached-HEAD state — the NFR-3 fixture.
        """
        args = ["checkout", "--quiet"]
        if create:
            args.append("-b")
        if detach:
            args.append("--detach")
        args.append(ref)
        self.git(*args)
        return self

    def tag(self, name: str, *, ref: str = "HEAD", message: str | None = None) -> RepoBuilder:
        """Create a lightweight tag, or an annotated one when ``message`` is given."""
        args = ["tag"]
        if message:
            args.extend(["-a", "-m", message])
        args.extend([name, ref])
        self.git(*args, env=self._env())
        return self

    def merge(self, ref: str, *, message: str | None = None, allow_conflict: bool = False, no_ff: bool = True) -> RepoBuilder:
        """Merge ``ref`` into the current branch.

        :param allow_conflict: when true, a conflicting merge leaves the
            conflicted working tree in place instead of raising — useful for
            building "what a conflict looks like" fixtures. When false (the
            default) a conflict aborts the merge and raises, so a test never
            silently continues from a half-merged state.
        :raises RepoBuilderError: the merge conflicted and ``allow_conflict``
            is false.
        """
        args = ["merge", "--no-edit"]
        if no_ff:
            args.append("--no-ff")
        if message:
            args.extend(["-m", message])
        args.append(ref)
        completed = subprocess.run(  # test helper: fixed binary, no shell
            ["git", *args],
            cwd=str(self.path),
            capture_output=True,
            text=True,
            env=self._env(),
            check=False,
        )
        if completed.returncode != 0:
            if not allow_conflict:
                self.git("merge", "--abort", check=False)
                raise RepoBuilderError(f"merge of {ref} conflicted: {completed.stdout.strip()}")
            return self
        self._clock += timedelta(seconds=COMMIT_INTERVAL_SECONDS)
        return self

    # ------------------------------------------------------------ inspection

    def current_branch(self) -> str | None:
        """Checked-out branch name, or ``None`` when HEAD is detached."""
        result = self.git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        return result or None

    def sha(self, ref: str = "HEAD") -> str:
        """Resolve ``ref`` to a full commit sha."""
        return self.git("rev-parse", ref)

    def read(self, path: str) -> str:
        """Read a file from the working tree."""
        return (self.path / path).read_text(encoding="utf-8")

    @property
    def commits(self) -> list[CommitRecord]:
        """Every commit this builder made, oldest first."""
        return list(self._commits)

    def build(self) -> Path:
        """Finish and return the repository path.

        Purely for readability at the end of a chain — the repository is usable
        at every point, so calling this is optional.
        """
        return self.path

    # ------------------------------------------------------------- shortcuts

    def scenario_clean_merge(self) -> RepoBuilder:
        """Two branches editing different files: merges cleanly, no findings."""
        self.file("a.py", "def a():\n    return 1\n").commit("add a")
        self.branch("feature")
        self.file("b.py", "def b():\n    return 2\n").commit("add b")
        return self.checkout(self.default_branch)

    def scenario_textual_conflict(self, *, regions: int = 1) -> RepoBuilder:
        """Both branches editing the same lines: ``regions`` conflicting hunks."""
        body = "\n".join(f"line {i}" for i in range(1, 40 * max(regions, 1)))
        self.file("conflict.txt", body).commit("base content")
        self.branch("feature")
        feature = body.replace("line 1\n", "line 1 FEATURE\n").replace("line 21", "line 21 FEATURE")
        self.file("conflict.txt", feature).commit("feature edits")
        self.checkout(self.default_branch)
        main = body.replace("line 1\n", "line 1 MAIN\n").replace("line 21", "line 21 MAIN")
        return self.file("conflict.txt", main).commit("main edits")

    def scenario_rename_vs_new_caller(self) -> RepoBuilder:
        """One branch renames a function; the other adds a call to the old name."""
        self.file("lib.py", "def old_name(x):\n    return x\n").commit("add lib")
        self.branch("feature")
        self.file("lib.py", "def new_name(x):\n    return x\n").commit("rename function")
        self.checkout(self.default_branch)
        return self.file("caller.py", "from lib import old_name\n\nold_name(1)\n").commit("add caller")

    def scenario_signature_change(self) -> RepoBuilder:
        """One branch changes a signature; the other adds callers of the old one."""
        self.file("lib.py", "def compute(a):\n    return a\n").commit("add lib")
        self.branch("feature")
        self.file("lib.py", "def compute(a, b, c):\n    return a + b + c\n").commit("widen signature")
        self.checkout(self.default_branch)
        return self.file("caller.py", "from lib import compute\n\ncompute(1)\n").commit("add caller")

    def scenario_binary_file(self) -> RepoBuilder:
        """A binary blob changed on both sides — must be reported, never parsed."""
        self.binary("asset.bin", bytes(range(256))).commit("add binary")
        self.branch("feature")
        self.binary("asset.bin", bytes(range(255, -1, -1))).commit("feature binary")
        self.checkout(self.default_branch)
        return self.binary("asset.bin", bytes([7]) * 256).commit("main binary")

    def scenario_unsupported_language(self) -> RepoBuilder:
        """Changes confined to a language with no grammar: textual fallback only."""
        self.file("script.zzz", "BEGIN\n  do thing\nEND\n").commit("add script")
        self.branch("feature")
        return self.file("script.zzz", "BEGIN\n  do other thing\nEND\n").commit("edit script").checkout(self.default_branch)


def build_repo(path: str | os.PathLike[str], files: dict[str, str] | None = None, *, message: str = "initial commit") -> Path:
    """One-liner for the common "repo with some files and one commit" case."""
    builder = RepoBuilder(path)
    for file_path, content in (files or {"README.md": "# test\n"}).items():
        builder.file(file_path, content)
    return builder.commit(message).build()


def commit_all(builder: RepoBuilder, files: Iterable[tuple[str, str]], message: str) -> RepoBuilder:
    """Stage several ``(path, content)`` pairs and commit them together."""
    for path, content in files:
        builder.file(path, content)
    return builder.commit(message)
