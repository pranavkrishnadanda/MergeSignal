"""Merge simulation — S1's oracle. **Owned by Agent B.**

Answers "would ``git merge head`` conflict, and exactly where?" without touching
the user's working tree, index or refs (FR-1, NFR-2).

Two strategies:

``merge-tree`` (preferred, git >= 2.38)
    ``git merge-tree --write-tree --name-only <base> <head>`` performs a real
    three-way merge entirely in the object database, writes the resulting tree,
    and lists conflicted paths. Conflict regions are recovered by reading the
    merged blobs out of that tree (they contain ``<<<<<<<``/``=======``/
    ``>>>>>>>`` markers) with :func:`parse_conflict_markers`.

``worktree-fallback`` (git < 2.38)
    ``git worktree add --detach`` a throwaway directory, ``git merge --no-commit
    --no-ff``, collect ``git diff --name-only --diff-filter=U``, read the
    conflicted files, then **always** ``git merge --abort`` and ``git worktree
    remove --force``, including on exception. The user's checkout is untouched
    because the merge happens in the scratch worktree.

Edge cases every implementation must handle:

* ``head`` already an ancestor of ``base`` — "Already up to date", zero
  conflicts, ``up_to_date=True``; this is **not** an error.
* Unrelated histories (``merge_base`` is ``None``) — report the failure as an
  error-free simulation with ``clean=False`` and a synthetic whole-tree note,
  never a crash.
* Unborn branch / missing ref — let :class:`~mergesignal.git.repo.GitError`
  propagate; the calling signal converts it to ``status="error"``.
* Binary conflicts — ``ConflictRegion(is_binary=True)`` with empty ranges and
  ``None`` text; never attempt to decode.
* Paths with spaces/unicode — all parsing must use ``-z`` NUL-separated output.
* Add/add, modify/delete and rename/rename conflicts — ``merge-tree`` reports
  these without marker text in the blob; emit a whole-file region rather than
  dropping the path.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mergesignal.git.repo import Repo
from mergesignal.models import ConflictRegion, MergeSimulation


def simulate_merge(repo: Repo, base: str, head: str, *, prefer_merge_tree: bool = True, extract_regions: bool = True) -> MergeSimulation:
    """Simulate merging ``head`` into ``base`` and report what would conflict.

    :param repo: read-only repository handle.
    :param base: ref merged *into* ("ours").
    :param head: ref merged *from* ("theirs").
    :param prefer_merge_tree: use the ``merge-tree`` fast path when available;
        set ``False`` to force the worktree fallback (used by tests).
    :param extract_regions: when ``False``, only conflicted *paths* are
        collected — much cheaper, used when the caller just needs a yes/no.
    :returns: a :class:`~mergesignal.models.MergeSimulation` whose ``strategy``
        records which path ran.
    :raises GitError: a ref is unknown or git itself failed.

    Must never mutate the caller's repository state.
    """
    raise NotImplementedError


def merge_tree_simulate(repo: Repo, base: str, head: str, *, extract_regions: bool = True) -> MergeSimulation:
    """``git merge-tree --write-tree`` path (git >= 2.38).

    Exit status 0 means a clean merge, 1 means conflicts; any other status is a
    real failure and must raise :class:`~mergesignal.git.repo.GitError`.
    """
    raise NotImplementedError


def worktree_simulate(repo: Repo, base: str, head: str, *, extract_regions: bool = True) -> MergeSimulation:
    """Fallback path for git < 2.38 using a disposable detached worktree.

    Guarantees cleanup of the scratch worktree even when the merge or the
    parsing raises; leaking a worktree would pollute ``git worktree list`` in
    the user's repository, which counts as mutating their state.
    """
    raise NotImplementedError


@contextmanager
def temporary_worktree(repo: Repo, ref: str, *, prefix: str = "mergesignal-") -> Iterator[Path]:
    """Context manager yielding a detached scratch worktree checked out at ``ref``.

    Removes the worktree (``git worktree remove --force``) and prunes the
    administrative entry on exit, swallowing cleanup errors so they cannot mask
    the original exception.
    """
    raise NotImplementedError


def parse_conflict_markers(text: str, file_path: str) -> list[ConflictRegion]:
    """Split merged file ``text`` into :class:`ConflictRegion` objects.

    Recognises both the default marker style::

        <<<<<<< ours
        ours lines
        =======
        theirs lines
        >>>>>>> theirs

    and diff3 style, where a ``||||||| base`` section sits between the ours
    section and ``=======``; its content becomes ``base_text``.

    Ranges are 1-based, half-open, and refer to line numbers **in the merged
    buffer** (marker lines excluded). Nested or unterminated markers are
    tolerated: an unterminated conflict yields one region running to EOF rather
    than raising. Returns ``[]`` when the text contains no markers.
    """
    raise NotImplementedError


def parse_merge_tree_output(stdout: str) -> tuple[str | None, list[str]]:
    """Parse ``git merge-tree --write-tree`` stdout.

    The format is: the written tree sha on the first line, then (on conflict) a
    NUL- or newline-separated list of conflicted paths, then an informational
    messages block separated by a blank line. Returns ``(tree_sha, paths)``;
    ``tree_sha`` is ``None`` when git produced none.
    """
    raise NotImplementedError


def conflicted_paths(repo: Repo, base: str, head: str) -> list[str]:
    """Cheap "which files conflict" query with no region extraction.

    Equivalent to ``simulate_merge(..., extract_regions=False).conflicted_files``
    but allowed to take shortcuts. Returns ``[]`` for a clean or up-to-date merge.
    """
    raise NotImplementedError
