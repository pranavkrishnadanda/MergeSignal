"""Thin, read-only subprocess wrapper over the ``git`` binary.

Why subprocess and not libgit2: ``git merge-tree --write-tree`` is the only
accurate conflict oracle, and it only exists in the real git binary. Since we
need the binary anyway, every other operation goes through it too.

**This class never mutates repository state.** It runs plumbing and read-only
porcelain with ``--no-optional-locks`` and never touches the index, the working
tree or refs. Modules that genuinely need a scratch workspace (see
:mod:`mergesignal.git.merge_sim`) create a *separate* worktree and clean it up.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Default per-invocation timeout, seconds. Overridable per :class:`Repo`.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Environment forced onto every git invocation so output is stable and git
#: never blocks waiting for a human (credential prompts, pagers, editors).
_STABLE_ENV: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_OPTIONAL_LOCKS": "0",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
}


class GitError(RuntimeError):
    """A git invocation failed, or the repository is unusable.

    Carries the argv, exit status and captured stderr so callers can decide
    whether a non-zero exit is expected (``git merge-tree`` exits 1 on conflict)
    or genuinely broken.
    """

    def __init__(
        self,
        message: str,
        *,
        args: list[str] | None = None,
        returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.git_args = list(args or [])
        self.returncode = returncode
        self.stderr = stderr

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        parts = [self.message]
        if self.git_args:
            parts.append(f"(git {' '.join(self.git_args)})")
        if self.returncode is not None:
            parts.append(f"exit={self.returncode}")
        if self.stderr.strip():
            parts.append(f"stderr={self.stderr.strip()[:500]}")
        return " ".join(parts)


class GitTimeoutError(GitError):
    """A git invocation exceeded the configured timeout."""


@dataclass(frozen=True)
class GitResult:
    """Captured output of one git invocation."""

    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """``True`` when git exited zero."""
        return self.returncode == 0


@dataclass(frozen=True)
class GitVersion:
    """Parsed ``git --version``.

    Comparison is tuple-wise, so ``repo.git_version() >= GitVersion(2, 38, 0)``
    is the supported way to gate on ``merge-tree --write-tree``.
    """

    major: int
    minor: int
    patch: int
    raw: str = ""

    def __ge__(self, other: GitVersion) -> bool:
        return (self.major, self.minor, self.patch) >= (other.major, other.minor, other.patch)

    def __gt__(self, other: GitVersion) -> bool:
        return (self.major, self.minor, self.patch) > (other.major, other.minor, other.patch)

    def __lt__(self, other: GitVersion) -> bool:
        return not self >= other

    def __le__(self, other: GitVersion) -> bool:
        return not self > other

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


#: Minimum version providing ``git merge-tree --write-tree`` (the fast path).
MERGE_TREE_MIN_VERSION = GitVersion(2, 38, 0)


class Repo:
    """Read-only handle on a git repository.

    :param path: any path inside the repository (working tree or ``.git`` dir).
    :param timeout: per-invocation timeout in seconds.
    :param git_binary: override the executable, mostly for tests.
    :raises GitError: ``path`` does not exist, or git is not installed.

    The constructor deliberately does **not** verify that ``path`` is a git
    repository — a bare path is allowed so that callers can build a ``Repo``
    before cloning into it. Use :meth:`is_repository` to check.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        git_binary: str = "git",
    ) -> None:
        self.path = Path(path).expanduser()
        self.timeout = float(timeout)
        self.git_binary = git_binary
        if shutil.which(git_binary) is None:
            raise GitError(f"git binary not found on PATH: {git_binary!r}")
        if not self.path.exists():
            raise GitError(f"repository path does not exist: {self.path}")

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Repo({str(self.path)!r})"

    # ------------------------------------------------------------------ core

    def run(
        self,
        args: list[str],
        *,
        check: bool = True,
        timeout: float | None = None,
        cwd: str | os.PathLike[str] | None = None,
        input_text: str | None = None,
    ) -> str:
        """Run ``git <args>`` in the repository and return stdout.

        :param args: git arguments, *not* including the ``git`` binary itself.
        :param check: raise :class:`GitError` on non-zero exit. Pass ``False``
            for commands whose non-zero exit is meaningful (``merge-tree``
            returns 1 on conflict, ``rev-parse --verify`` returns 128 for an
            unknown ref) and use :meth:`run_result` if you also need the code.
        :param timeout: override the per-repo timeout for this call.
        :param cwd: run somewhere else (used for temporary worktrees); defaults
            to :attr:`path`.
        :param input_text: text piped to git's stdin.
        :returns: stdout decoded as UTF-8 with ``errors="replace"`` — git paths
            can contain arbitrary bytes and we must never raise on decoding.
        :raises GitTimeoutError: the call exceeded the timeout.
        :raises GitError: git exited non-zero and ``check`` is true.

        Trailing newlines are stripped, since virtually every caller wants that.
        """
        return self.run_result(
            args, check=check, timeout=timeout, cwd=cwd, input_text=input_text
        ).stdout

    def run_result(
        self,
        args: list[str],
        *,
        check: bool = False,
        timeout: float | None = None,
        cwd: str | os.PathLike[str] | None = None,
        input_text: str | None = None,
    ) -> GitResult:
        """Like :meth:`run` but returns the full :class:`GitResult`.

        Defaults to ``check=False`` because the whole point of reaching for this
        method is to inspect a non-zero exit code yourself.
        """
        argv = [self.git_binary, "--no-optional-locks", *args]
        env = {**os.environ, **_STABLE_ENV}
        try:
            completed = subprocess.run(  # argv is built from a fixed binary plus caller args, never a shell string
                argv,
                cwd=str(cwd or self.path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout if timeout is not None else self.timeout,
                input=input_text,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitTimeoutError(
                f"git timed out after {exc.timeout:g}s",
                args=list(args),
                stderr=_as_text(exc.stderr),
            ) from exc
        except OSError as exc:
            raise GitError(f"failed to execute git: {exc}", args=list(args)) from exc

        result = GitResult(
            args=list(args),
            returncode=completed.returncode,
            stdout=(completed.stdout or "").rstrip("\n"),
            stderr=completed.stderr or "",
        )
        if check and not result.ok:
            raise GitError(
                "git command failed",
                args=result.args,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return result

    # ------------------------------------------------------------ inspection

    def is_repository(self) -> bool:
        """``True`` when :attr:`path` is inside a git repository (bare or not)."""
        return self.run_result(["rev-parse", "--git-dir"]).ok

    def git_version(self) -> GitVersion:
        """Parse ``git --version``.

        Vendor suffixes are tolerated (``2.54.0 (Apple Git-157)``). Missing
        components default to 0, so ``git version 3`` parses as ``3.0.0``.

        :raises GitError: git is unusable or its output is unparseable.
        """
        raw = self.run(["--version"], check=True)
        parts = raw.split()
        for token in parts:
            if token and token[0].isdigit():
                numbers = []
                for chunk in token.split(".")[:3]:
                    digits = "".join(c for c in chunk if c.isdigit())
                    numbers.append(int(digits) if digits else 0)
                while len(numbers) < 3:
                    numbers.append(0)
                return GitVersion(numbers[0], numbers[1], numbers[2], raw=raw)
        raise GitError(f"could not parse git version from {raw!r}")

    def supports_merge_tree(self) -> bool:
        """``True`` when this git provides ``merge-tree --write-tree`` (>= 2.38)."""
        try:
            return self.git_version() >= MERGE_TREE_MIN_VERSION
        except GitError:
            return False

    def toplevel(self) -> str:
        """Absolute path of the working-tree root.

        :raises GitError: not a working tree (bare repo or outside a repo).
        """
        return self.run(["rev-parse", "--show-toplevel"], check=True)

    def git_dir(self) -> str:
        """Absolute path of the ``.git`` directory."""
        return self.run(["rev-parse", "--absolute-git-dir"], check=True)

    # ------------------------------------------------------------------ refs

    def rev_parse(self, ref: str) -> str:
        """Resolve ``ref`` to a full 40-character commit sha.

        ``^{commit}`` is appended so annotated tags resolve to their commit.

        :raises GitError: the ref does not exist, is ambiguous, or the branch is
            unborn (a fresh repo's ``HEAD`` has no commit yet).
        """
        result = self.run_result(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
        if not result.ok or not result.stdout:
            raise GitError(
                f"unknown or unborn ref: {ref!r}",
                args=result.args,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return result.stdout.strip()

    def ref_exists(self, ref: str) -> bool:
        """``True`` when ``ref`` resolves to a commit; never raises."""
        try:
            self.rev_parse(ref)
        except GitError:
            return False
        return True

    def merge_base(self, base: str, head: str) -> str | None:
        """Best common ancestor of ``base`` and ``head``.

        :returns: the merge-base sha, or ``None`` when the histories are
            unrelated (git exits 1 with no output) — that is a legitimate state,
            not an error.
        :raises GitError: either ref is unknown.
        """
        self.rev_parse(base)
        self.rev_parse(head)
        result = self.run_result(["merge-base", base, head])
        if result.ok and result.stdout.strip():
            return result.stdout.strip()
        if result.returncode == 1:
            return None
        raise GitError(
            "git merge-base failed",
            args=result.args,
            returncode=result.returncode,
            stderr=result.stderr,
        )

    def is_ancestor(self, maybe_ancestor: str, descendant: str) -> bool:
        """``True`` when ``maybe_ancestor`` is reachable from ``descendant``.

        Used to detect the "already up to date" merge case, which must report
        zero conflicts rather than an error.
        """
        result = self.run_result(["merge-base", "--is-ancestor", maybe_ancestor, descendant])
        if result.returncode in (0, 1):
            return result.ok
        raise GitError(
            "git merge-base --is-ancestor failed",
            args=result.args,
            returncode=result.returncode,
            stderr=result.stderr,
        )

    def current_branch(self) -> str | None:
        """Name of the checked-out branch, or ``None`` on a detached HEAD.

        Also returns the branch name for an *unborn* branch (fresh repo with no
        commits), because ``symbolic-ref`` works before the first commit.
        """
        result = self.run_result(["symbolic-ref", "--quiet", "--short", "HEAD"])
        if result.ok and result.stdout.strip():
            return result.stdout.strip()
        return None

    def list_branches(self, *, remote: bool = False, pattern: str | None = None) -> list[str]:
        """List branch names, sorted by most recent commit first.

        :param remote: include remote-tracking branches instead of local ones.
        :param pattern: optional ``git for-each-ref`` glob, e.g. ``feature/*``.
        :returns: short names (``main``, ``origin/feature/x``). Symbolic refs
            such as ``origin/HEAD`` are filtered out because they are aliases,
            not branches. An empty repository yields ``[]``, not an error.
        """
        prefix = "refs/remotes/" if remote else "refs/heads/"
        ref_pattern = f"{prefix}{pattern}" if pattern else prefix
        result = self.run_result(
            [
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short) %(symref)",
                ref_pattern,
            ]
        )
        if not result.ok:
            raise GitError(
                "git for-each-ref failed",
                args=result.args,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        branches: list[str] = []
        for line in result.stdout.splitlines():
            line = line.rstrip()
            if not line:
                continue
            name, _, symref = line.partition(" ")
            if symref.strip():
                continue
            branches.append(name)
        return branches

    # ----------------------------------------------------------------- trees

    def diff_names(
        self, base: str, head: str, *, find_renames: bool = True, merge_base: bool = False
    ) -> list[str]:
        """Paths changed between two refs.

        :param find_renames: pass ``-M`` so renames are detected; the returned
            list then contains **both** the old and the new path for a rename,
            since either may matter to a caller doing path intersection.
        :param merge_base: compare ``head`` against the merge base of the two
            refs (``git diff base...head``) rather than against ``base`` itself.
        :returns: repository-relative POSIX paths, deduplicated, sorted.

        ``-z`` is used so paths containing spaces, quotes or non-UTF-8 bytes
        come through verbatim rather than in git's quoted form.
        """
        args = ["diff", "--name-status", "-z", "--no-color"]
        if find_renames:
            args.append("-M")
        args.extend([f"{base}...{head}"] if merge_base else [base, head])
        result = self.run_result(args)
        if not result.ok:
            raise GitError(
                "git diff --name-status failed",
                args=result.args,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return sorted(_parse_name_status_z(result.stdout))

    def file_content_at(self, ref: str, path: str) -> str | None:
        """Read the text of ``path`` as it exists at ``ref``.

        :returns: the file's contents, or ``None`` when the path does not exist
            at that ref (deleted on one side, added on the other) — an extremely
            common, non-exceptional case.
        :raises GitError: ``ref`` itself is unknown.

        Content is decoded as UTF-8 with ``errors="replace"``; binary blobs come
        back as mojibake rather than raising, so callers must consult the diff's
        ``is_binary`` flag before treating the result as source code. Use
        :meth:`file_bytes_at` when the raw bytes matter.
        """
        blob = self.file_bytes_at(ref, path)
        return None if blob is None else blob.decode("utf-8", errors="replace")

    def file_bytes_at(self, ref: str, path: str) -> bytes | None:
        """Raw bytes of ``path`` at ``ref``, or ``None`` when absent.

        Bypasses text decoding, which matters for binary detection and for
        hashing blobs.
        """
        argv = [self.git_binary, "--no-optional-locks", "show", f"{ref}:{path}"]
        try:
            completed = subprocess.run(  # fixed binary, structured args, no shell
                argv,
                cwd=str(self.path),
                capture_output=True,
                timeout=self.timeout,
                env={**os.environ, **_STABLE_ENV},
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitTimeoutError(
                f"git show timed out after {exc.timeout:g}s", args=argv[1:]
            ) from exc
        except OSError as exc:
            raise GitError(f"failed to execute git: {exc}", args=argv[1:]) from exc
        if completed.returncode == 0:
            return completed.stdout
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
        # git show fails the same way for "bad ref" and "path absent at ref", so
        # disambiguate by asking whether the ref itself resolves.
        if not self.ref_exists(ref):
            raise GitError(
                f"unknown ref: {ref!r}",
                args=argv[1:],
                returncode=completed.returncode,
                stderr=stderr,
            )
        return None

    def list_files_at(self, ref: str, *, pattern: str | None = None) -> list[str]:
        """List every tracked path present at ``ref``.

        Bounded only by repository size — callers doing whole-tree scans should
        pass ``pattern`` or cap the result themselves (see
        ``AnalysisConfig.max_files``).
        """
        args = ["ls-tree", "-r", "--name-only", "-z", ref]
        if pattern:
            args.extend(["--", pattern])
        result = self.run_result(args)
        if not result.ok:
            raise GitError(
                "git ls-tree failed",
                args=result.args,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return [p for p in result.stdout.split("\0") if p]

    def show_ref_message(self, ref: str) -> str:
        """Subject line of the commit ``ref`` points at, for report headers."""
        return self.run(["log", "-1", "--format=%s", ref], check=True)


def _parse_name_status_z(payload: str) -> set[str]:
    """Parse ``git diff --name-status -z`` output into a set of paths.

    The format is NUL-separated with a quirk: a rename/copy status (``R100``,
    ``C75``) is followed by *two* path fields instead of one. Both are returned.
    """
    fields = [f for f in payload.split("\0") if f != ""]
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        takes_two = status[:1] in {"R", "C"}
        for _ in range(2 if takes_two else 1):
            if index < len(fields):
                paths.add(fields[index])
                index += 1
    return paths


def _as_text(value: str | bytes | None) -> str:
    """Coerce subprocess output of unknown type to text, never raising."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
