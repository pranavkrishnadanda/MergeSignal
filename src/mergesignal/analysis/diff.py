"""Structured diff parsing: ``git diff`` text -> models. **Owned by Agent C.**

Produces :class:`~mergesignal.models.Diff` /
:class:`~mergesignal.models.DiffFile` / :class:`~mergesignal.models.Hunk`
(FR-2). Everything downstream — semantic analysis, overlap math, risk scoring —
reads these models rather than raw diff text, so the parser is the single place
that has to understand git's output quirks.

Quirks the parser must handle:

* ``--no-color`` and ``-U0``-vs-``-U3`` context: hunk ranges are taken from the
  ``@@`` header, which is authoritative regardless of context size.
* Renames and copies: ``similarity index`` / ``rename from`` / ``rename to``
  headers, which set ``old_path``. ``-M`` must be passed to get them.
* ``Binary files a/x and b/x differ`` — set ``is_binary``, emit no hunks.
* ``new file mode`` / ``deleted file mode`` — set ``is_new`` / ``is_deleted``.
* Pure mode changes and empty-file creation produce a ``DiffFile`` with zero
  hunks, which is legal, not an error.
* ``\\ No newline at end of file`` lines belong to neither side and must not be
  counted as added or removed content.
* Paths are unquoted from git's C-style quoting (``"a/sp ace.py"``) and the
  ``a/``/``b/`` prefixes stripped; ``/dev/null`` maps to ``None``.
"""

from __future__ import annotations

from mergesignal.git.repo import Repo
from mergesignal.models import Diff, DiffFile, Hunk


def diff_refs(repo: Repo, base: str, head: str, *, merge_base: bool = False, context_lines: int = 0, detect_renames: bool = True, max_files: int | None = None) -> Diff:
    """Compute and parse the diff between two refs.

    :param merge_base: use ``base...head`` (changes on the head side only)
        rather than ``base head`` (the two-dot symmetric difference).
    :param context_lines: ``-U`` value; ``0`` keeps hunks tight, which makes
        hunk-overlap scoring in S3 far less noisy.
    :param max_files: stop after this many files, leaving the rest unparsed;
        callers should surface the truncation to the user.
    :returns: a populated :class:`~mergesignal.models.Diff`; an empty file list
        when the refs are identical.
    :raises GitError: a ref is unknown.
    """
    raise NotImplementedError


def parse_diff(diff_text: str, *, base: str = "", head: str = "") -> Diff:
    """Parse unified ``git diff`` output into a :class:`~mergesignal.models.Diff`.

    Pure function — the workhorse that unit tests drive directly with recorded
    diff text. Empty input yields a ``Diff`` with no files.
    """
    raise NotImplementedError


def parse_file_diff(lines: list[str]) -> DiffFile:
    """Parse the lines of a single ``diff --git`` section into a ``DiffFile``."""
    raise NotImplementedError


def parse_hunk_header(header: str) -> tuple[int, int, int, int]:
    """Parse ``@@ -old_start,old_count +new_start,new_count @@`` .

    The counts are optional in git's output (``@@ -1 +1 @@`` means one line on
    each side). Returns ``(old_start, old_count, new_start, new_count)``.

    :raises ValueError: the header is malformed.
    """
    raise NotImplementedError


def parse_hunk(header: str, body_lines: list[str], file_path: str) -> Hunk:
    """Build a :class:`~mergesignal.models.Hunk` from a header plus its body."""
    raise NotImplementedError


def unquote_path(raw: str) -> str:
    """Undo git's C-style quoting of paths containing spaces or non-ASCII bytes.

    ``"src/\\303\\251.py"`` -> ``src/é.py``. Unquoted input is returned as-is.
    """
    raise NotImplementedError


def changed_line_numbers(diff_file: DiffFile, *, side: str = "head") -> set[int]:
    """Every 1-based line number touched on one side of a file.

    :param side: ``"head"`` or ``"base"``.

    Used by the semantic signal to ask "did this change land inside the body of
    the symbol I care about?" and by overlap to compute line intersections.
    """
    raise NotImplementedError


def annotate_languages(diff: Diff) -> Diff:
    """Fill in ``DiffFile.language`` for every file using the language registry.

    Returns a new :class:`~mergesignal.models.Diff`; does not mutate the input.
    Files with unknown extensions keep ``language=None``.
    """
    raise NotImplementedError
