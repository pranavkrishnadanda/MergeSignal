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

Implementation notes
--------------------

* Both strategies force ``merge.conflictstyle=diff3`` for the simulation only
  (via ``git -c``, never a config write), so :attr:`ConflictRegion.base_text` is
  populated regardless of how the user has configured their own checkout. This
  also makes region output deterministic across machines.
* ``merge-tree`` is invoked **without** ``--name-only`` so the conflicted-path
  list comes from the stage-entry block (``<mode> <sha> <stage>\\t<path>``),
  which every git that has ``--write-tree`` emits. Paths named only in the
  informational message block (rename/rename, directory/file) are unioned in.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from mergesignal.git.repo import GitError, Repo
from mergesignal.models import ConflictRegion, LineRange, MergeSimulation

#: Strategy name recorded on a :class:`MergeSimulation` produced by ``merge-tree``.
STRATEGY_MERGE_TREE = "merge-tree"

#: Strategy name recorded on a :class:`MergeSimulation` produced by a scratch worktree.
STRATEGY_WORKTREE = "worktree-fallback"

#: Pseudo-path used for the synthetic whole-tree note emitted when the two refs
#: have no common ancestor. It is not a real path and never exists on disk; it
#: is deliberately unrepresentable as a filename so downstream consumers cannot
#: confuse it with one.
UNRELATED_HISTORIES_PATH = "<unrelated histories>"

#: Conflict-marker prefixes. git writes exactly seven characters followed by
#: either end-of-line or a space and a label.
_MARKER_OURS = "<<<<<<<"
_MARKER_BASE = "|||||||"
_MARKER_SEP = "======="
_MARKER_THEIRS = ">>>>>>>"

#: Forced config for the simulation only — never written to any config file.
_DIFF3 = ["-c", "merge.conflictstyle=diff3"]


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
    use_merge_tree = prefer_merge_tree and repo.supports_merge_tree()
    if use_merge_tree:
        return merge_tree_simulate(repo, base, head, extract_regions=extract_regions)
    return worktree_simulate(repo, base, head, extract_regions=extract_regions)


def merge_tree_simulate(repo: Repo, base: str, head: str, *, extract_regions: bool = True) -> MergeSimulation:
    """``git merge-tree --write-tree`` path (git >= 2.38).

    Exit status 0 means a clean merge, 1 means conflicts; any other status is a
    real failure and must raise :class:`~mergesignal.git.repo.GitError`.
    """
    merge_base, short_circuit = _prepare(repo, base, head, STRATEGY_MERGE_TREE)
    if short_circuit is not None:
        return short_circuit

    result = repo.run_result([*_DIFF3, "merge-tree", "--write-tree", "-z", base, head])
    if result.returncode not in (0, 1):
        raise GitError(
            "git merge-tree failed",
            args=result.args,
            returncode=result.returncode,
            stderr=result.stderr,
        )

    tree_sha, paths = parse_merge_tree_output(result.stdout)
    info = _parse_merge_tree_info(result.stdout)
    for path in info:
        if path not in paths:
            paths.append(path)
    paths = sorted(set(paths))

    regions: list[ConflictRegion] = []
    if extract_regions and tree_sha:
        for path in paths:
            regions.extend(_regions_from_blob(repo, tree_sha, path, binary_hint=_is_binary_conflict(info.get(path, ()))))

    return MergeSimulation(
        base=base,
        head=head,
        merge_base=merge_base,
        clean=not paths,
        conflicted_files=paths,
        regions=regions,
        tree_sha=tree_sha,
        strategy=STRATEGY_MERGE_TREE,
        up_to_date=False,
    )


def worktree_simulate(repo: Repo, base: str, head: str, *, extract_regions: bool = True) -> MergeSimulation:
    """Fallback path for git < 2.38 using a disposable detached worktree.

    Guarantees cleanup of the scratch worktree even when the merge or the
    parsing raises; leaking a worktree would pollute ``git worktree list`` in
    the user's repository, which counts as mutating their state.
    """
    merge_base, short_circuit = _prepare(repo, base, head, STRATEGY_WORKTREE)
    if short_circuit is not None:
        return short_circuit

    with temporary_worktree(repo, base) as workdir:
        try:
            merge = repo.run_result(
                [*_DIFF3, "merge", "--no-commit", "--no-ff", head],
                cwd=workdir,
            )
            listing = repo.run_result(["diff", "--name-only", "--diff-filter=U", "-z"], cwd=workdir)
            if not listing.ok:
                raise GitError(
                    "git diff --diff-filter=U failed in scratch worktree",
                    args=listing.args,
                    returncode=listing.returncode,
                    stderr=listing.stderr,
                )
            paths = sorted({p for p in listing.stdout.split("\0") if p})
            if merge.returncode != 0 and not paths:
                # Non-zero exit with nothing marked unmerged is a genuine
                # failure (bad ref, unrelated histories, refusal), not a
                # conflict we can describe.
                raise GitError(
                    "git merge failed in scratch worktree",
                    args=merge.args,
                    returncode=merge.returncode,
                    stderr=merge.stderr or merge.stdout,
                )

            regions: list[ConflictRegion] = []
            if extract_regions:
                # The working-tree copy of a binary conflict is simply one
                # side's bytes, which may look perfectly textual on its own
                # (git calls a merge binary when *any* of the three stages is).
                # Ask git which paths it considers binary instead of guessing
                # from the leftover file.
                binary = _binary_paths(repo, base, head)
                for path in paths:
                    regions.extend(_regions_from_file(workdir / path, path, binary_hint=path in binary))
        finally:
            # Leave the scratch worktree in a sane state before it is removed;
            # `worktree remove --force` copes either way, but aborting first
            # keeps the administrative files tidy on git versions that refuse
            # to remove a worktree mid-merge.
            repo.run_result(["merge", "--abort"], cwd=workdir)

    return MergeSimulation(
        base=base,
        head=head,
        merge_base=merge_base,
        clean=not paths,
        conflicted_files=paths,
        regions=regions,
        tree_sha=None,
        strategy=STRATEGY_WORKTREE,
        up_to_date=False,
    )


@contextmanager
def temporary_worktree(repo: Repo, ref: str, *, prefix: str = "mergesignal-") -> Iterator[Path]:
    """Context manager yielding a detached scratch worktree checked out at ``ref``.

    Removes the worktree (``git worktree remove --force``) and prunes the
    administrative entry on exit, swallowing cleanup errors so they cannot mask
    the original exception.
    """
    parent = Path(tempfile.mkdtemp(prefix=prefix))
    # `git worktree add` insists on creating the directory itself.
    workdir = parent / "worktree"
    try:
        repo.run(["worktree", "add", "--detach", "--quiet", str(workdir), ref], check=True)
    except BaseException:
        shutil.rmtree(parent, ignore_errors=True)
        raise
    try:
        yield workdir
    finally:
        # Cleanup must never raise: it would replace the caller's real
        # exception with a cosmetic one.
        with suppress(GitError):
            repo.run_result(["worktree", "remove", "--force", str(workdir)])
        with suppress(GitError):
            repo.run_result(["worktree", "prune"])
        shutil.rmtree(parent, ignore_errors=True)


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
    if not text:
        return []

    lines = text.splitlines(keepends=True)
    regions: list[ConflictRegion] = []

    # Per-region accumulator state. `side` is None when outside a conflict.
    side: str | None = None
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    bodies: dict[str, list[str]] = {}

    def open_region(line_number: int) -> None:
        nonlocal side
        side = "ours"
        starts.clear()
        ends.clear()
        bodies.clear()
        starts["ours"] = line_number
        ends["ours"] = line_number
        bodies["ours"] = []

    def switch(new_side: str, line_number: int) -> None:
        nonlocal side
        side = new_side
        starts[new_side] = line_number
        ends[new_side] = line_number
        bodies[new_side] = []

    def close_region() -> None:
        nonlocal side
        regions.append(
            ConflictRegion(
                file=file_path,
                ours_range=LineRange(start=starts.get("ours", 0), end=ends.get("ours", 0)),
                theirs_range=LineRange(start=starts.get("theirs", 0), end=ends.get("theirs", 0)),
                ours_text="".join(bodies.get("ours", [])) or None,
                theirs_text="".join(bodies.get("theirs", [])) or None,
                base_text="".join(bodies["base"]) if "base" in bodies else None,
            )
        )
        side = None

    for index, raw in enumerate(lines):
        line_number = index + 1
        stripped = raw.rstrip("\r\n")
        if side is None:
            if _is_marker(stripped, _MARKER_OURS):
                open_region(line_number + 1)
            continue
        if _is_marker(stripped, _MARKER_BASE):
            switch("base", line_number + 1)
            continue
        if _is_marker(stripped, _MARKER_SEP):
            switch("theirs", line_number + 1)
            continue
        if _is_marker(stripped, _MARKER_THEIRS):
            close_region()
            continue
        # Anything else — including a nested `<<<<<<<` — is content of the
        # current side. Tolerating it is what keeps malformed merges from
        # blowing up the whole report.
        bodies[side].append(raw)
        ends[side] = line_number + 1

    if side is not None:
        # Unterminated conflict: the open side runs to EOF.
        close_region()
    return regions


def parse_merge_tree_output(stdout: str) -> tuple[str | None, list[str]]:
    """Parse ``git merge-tree --write-tree`` stdout.

    The format is: the written tree sha on the first line, then (on conflict) a
    NUL- or newline-separated list of conflicted paths, then an informational
    messages block separated by a blank line. Returns ``(tree_sha, paths)``;
    ``tree_sha`` is ``None`` when git produced none.

    Both the ``--name-only`` form (bare paths) and the default stage-entry form
    (``<mode> <sha> <stage>\\t<path>``) are accepted; stage entries for the same
    path collapse to a single entry. Order is preserved and duplicates removed.
    """
    if not stdout:
        return (None, [])

    fields = stdout.split("\0") if "\0" in stdout else stdout.split("\n")

    tree_sha = fields[0].strip() or None
    paths: list[str] = []
    seen: set[str] = set()
    for field in fields[1:]:
        if field == "" or field.strip() == "":
            # Empty field terminates the conflicted-path block; the remainder is
            # the informational message block, parsed separately.
            break
        path = _strip_stage_entry(field)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return (tree_sha, paths)


def conflicted_paths(repo: Repo, base: str, head: str) -> list[str]:
    """Cheap "which files conflict" query with no region extraction.

    Equivalent to ``simulate_merge(..., extract_regions=False).conflicted_files``
    but allowed to take shortcuts. Returns ``[]`` for a clean or up-to-date merge.
    """
    return simulate_merge(repo, base, head, extract_regions=False).conflicted_files


# --------------------------------------------------------------------- internals


def _prepare(repo: Repo, base: str, head: str, strategy: str) -> tuple[str | None, MergeSimulation | None]:
    """Resolve refs and short-circuit the two no-work cases.

    :returns: ``(merge_base, simulation)``. ``simulation`` is a finished
        :class:`MergeSimulation` when the merge is already up to date or the
        histories are unrelated, and ``None`` when the caller should go ahead
        and simulate. ``merge_base`` is handed back so the caller does not pay
        for a second ``git merge-base``.
    :raises GitError: either ref is unknown or unborn — the caller converts it
        to ``status="error"``.
    """
    repo.rev_parse(base)
    repo.rev_parse(head)
    merge_base = repo.merge_base(base, head)

    if merge_base is None:
        # No common ancestor: git refuses to merge at all. Report it as a
        # conflicted (but error-free) simulation carrying one whole-tree note,
        # so the caller can explain the situation instead of crashing.
        return None, MergeSimulation(
            base=base,
            head=head,
            merge_base=None,
            clean=False,
            conflicted_files=[UNRELATED_HISTORIES_PATH],
            regions=[
                ConflictRegion(
                    file=UNRELATED_HISTORIES_PATH,
                    ours_range=LineRange(start=0, end=0),
                    theirs_range=LineRange(start=0, end=0),
                    is_binary=True,
                )
            ],
            strategy=strategy,
            up_to_date=False,
        )

    if repo.is_ancestor(head, base):
        return merge_base, MergeSimulation(
            base=base,
            head=head,
            merge_base=merge_base,
            clean=True,
            conflicted_files=[],
            regions=[],
            strategy=strategy,
            up_to_date=True,
        )
    return merge_base, None


def _is_marker(line: str, marker: str) -> bool:
    """``True`` when ``line`` is a git conflict marker of the given kind.

    git emits exactly seven marker characters, optionally followed by a space
    and a label. Requiring the boundary keeps a line of ``========`` in a
    reStructuredText document from being mistaken for a separator.
    """
    if not line.startswith(marker):
        return False
    rest = line[len(marker) :]
    return rest == "" or rest[0] in " \t"


def _strip_stage_entry(field: str) -> str:
    """Turn one ``merge-tree`` conflicted-file field into a bare path.

    Accepts both ``--name-only`` output (already a bare path) and the default
    ``<mode> <sha> <stage>\\t<path>`` stage entry.
    """
    head, tab, tail = field.partition("\t")
    if tab and len(head.split(" ")) == 3:
        return tail
    return field


def _parse_merge_tree_info(stdout: str) -> dict[str, tuple[str, ...]]:
    """Map path -> conflict types from ``merge-tree``'s informational block.

    With ``-z`` the block is a sequence of records::

        <path count> NUL <path> [NUL <path>...] NUL <type> NUL <message> NUL

    Only records whose type names a conflict are kept; "Auto-merging" notices
    are noise. Returns ``{}`` for a clean merge or a non-``-z`` payload.
    """
    if "\0" not in stdout:
        return {}
    fields = stdout.split("\0")
    # Skip the tree sha and the conflicted-path block up to its empty terminator.
    index = 1
    while index < len(fields) and fields[index] != "":
        index += 1
    index += 1

    info: dict[str, list[str]] = {}
    while index < len(fields):
        count_field = fields[index]
        if not count_field.strip().isdigit():
            break
        count = int(count_field.strip())
        index += 1
        paths = fields[index : index + count]
        index += count
        if index >= len(fields):
            break
        kind = fields[index]
        index += 2  # skip the human-readable message too
        if "CONFLICT" not in kind.upper():
            continue
        for path in paths:
            info.setdefault(path, []).append(kind)
    return {path: tuple(kinds) for path, kinds in info.items()}


def _is_binary_conflict(kinds: tuple[str, ...] | list[str]) -> bool:
    """``True`` when git itself described the conflict as binary."""
    return any("binary" in kind.lower() for kind in kinds)


def _binary_region(path: str) -> ConflictRegion:
    """Whole-file marker for a conflict with no line detail to offer."""
    return ConflictRegion(
        file=path,
        ours_range=LineRange(start=0, end=0),
        theirs_range=LineRange(start=0, end=0),
        ours_text=None,
        theirs_text=None,
        is_binary=True,
    )


def _whole_file_region(path: str, text: str) -> ConflictRegion:
    """Region for a conflict git recorded without markers.

    Add/add, modify/delete and rename/rename conflicts leave a blob that is
    simply one side's content. We report the whole file as the "ours" extent so
    the path is never silently dropped, with ``theirs_range`` empty to signal
    that only one side's text is recoverable.
    """
    line_count = len(text.splitlines())
    return ConflictRegion(
        file=path,
        ours_range=LineRange(start=1, end=line_count + 1),
        theirs_range=LineRange(start=0, end=0),
        ours_text=text or None,
        theirs_text=None,
    )


def _regions_from_text(text: str, path: str) -> list[ConflictRegion]:
    """Marker regions for ``text``, falling back to a whole-file region."""
    regions = parse_conflict_markers(text, path)
    return regions or [_whole_file_region(path, text)]


def _regions_from_blob(repo: Repo, tree_sha: str, path: str, *, binary_hint: bool) -> list[ConflictRegion]:
    """Read one conflicted blob out of the written tree and describe it.

    A blob that cannot be read (deleted on both sides, or an exotic conflict
    with no entry in the tree) yields a binary-style whole-file marker rather
    than an exception: the path is known to conflict either way.
    """
    if binary_hint:
        return [_binary_region(path)]
    try:
        blob = repo.file_bytes_at(tree_sha, path)
    except GitError:
        blob = None
    if blob is None or b"\0" in blob:
        return [_binary_region(path)]
    return _regions_from_text(blob.decode("utf-8", errors="replace"), path)


def _regions_from_file(target: Path, path: str, *, binary_hint: bool = False) -> list[ConflictRegion]:
    """Same as :func:`_regions_from_blob` but reading the scratch worktree."""
    if binary_hint:
        return [_binary_region(path)]
    try:
        blob = target.read_bytes()
    except OSError:
        return [_binary_region(path)]
    if b"\0" in blob:
        return [_binary_region(path)]
    return _regions_from_text(blob.decode("utf-8", errors="replace"), path)


def _binary_paths(repo: Repo, base: str, head: str) -> set[str]:
    """Paths git reports as binary in ``git diff base head``.

    ``--numstat`` prints ``-`` for both counts on a binary file. ``--no-renames``
    keeps the ``-z`` record shape fixed at ``added\\tremoved\\tpath`` so a rename
    cannot desynchronise the parse. A failure here is non-fatal: worst case a
    binary conflict is described as a textual whole-file region.
    """
    result = repo.run_result(["diff", "--numstat", "-z", "--no-renames", base, head])
    if not result.ok:
        return set()
    binary: set[str] = set()
    for record in result.stdout.split("\0"):
        if not record:
            continue
        parts = record.split("\t")
        if len(parts) >= 3 and parts[0] == "-" and parts[1] == "-":
            binary.add("\t".join(parts[2:]))
    return binary
