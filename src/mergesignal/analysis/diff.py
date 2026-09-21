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

Line-number conventions follow :class:`~mergesignal.models.LineRange`: 1-based,
start inclusive, end exclusive. A pure insertion reported by git as
``@@ -0,0 +1,3 @@`` becomes ``base_range=[0, 0)`` (empty, at the top of the
file) and ``head_range=[1, 4)``.
"""

from __future__ import annotations

import re

from mergesignal.analysis.languages import language_for_path
from mergesignal.git.repo import Repo
from mergesignal.models import Diff, DiffFile, Hunk, LineRange

#: ``@@ -old_start[,old_count] +new_start[,new_count] @@[ context]``
_HUNK_HEADER_RE = re.compile(r"^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@+")

#: Marker git emits for a file whose last line has no newline. Belongs to
#: neither side and must never be counted as content.
_NO_NEWLINE_PREFIX = "\\"

#: Single-character escapes git's C-style quoting can emit.
_C_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    '"': '"',
}


def diff_refs(
    repo: Repo,
    base: str,
    head: str,
    *,
    merge_base: bool = False,
    context_lines: int = 0,
    detect_renames: bool = True,
    max_files: int | None = None,
) -> Diff:
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
    args = [
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        f"-U{max(int(context_lines), 0)}",
        "-M" if detect_renames else "--no-renames",
        f"{base}...{head}" if merge_base else f"{base}",
    ]
    if not merge_base:
        args.append(head)
    text = repo.run(args, check=True)
    diff = parse_diff(text, base=base, head=head)
    if max_files is not None and len(diff.files) > max_files:
        diff = Diff(base=diff.base, head=diff.head, files=diff.files[:max_files])
    return annotate_languages(diff)


def parse_diff(diff_text: str, *, base: str = "", head: str = "") -> Diff:
    """Parse unified ``git diff`` output into a :class:`~mergesignal.models.Diff`.

    Pure function — the workhorse that unit tests drive directly with recorded
    diff text. Empty input yields a ``Diff`` with no files.

    Any preamble before the first ``diff --git`` line (commit headers from
    ``git show``, for instance) is ignored, so the same parser handles
    ``git diff``, ``git show`` and ``git format-patch`` output.
    """
    files: list[DiffFile] = []
    for section in _split_sections(diff_text):
        parsed = parse_file_diff(section)
        if parsed is not None:
            files.append(parsed)
    return Diff(base=base, head=head, files=files)


def _split_sections(diff_text: str) -> list[list[str]]:
    """Split raw diff text into one list of lines per ``diff --git`` section.

    Safe because every line inside a hunk body carries a ``+``/``-``/space/``\\``
    prefix, so a content line can never be mistaken for a section header.
    """
    if not diff_text:
        return []
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in diff_text.replace("\r\n", "\n").split("\n"):
        if line.startswith("diff --git "):
            if current is not None:
                sections.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        sections.append(current)
    return sections


def parse_file_diff(lines: list[str]) -> DiffFile:
    """Parse the lines of a single ``diff --git`` section into a ``DiffFile``."""
    if not lines:
        raise ValueError("parse_file_diff requires at least the 'diff --git' header line")

    header_a, header_b = _parse_diff_git_header(lines[0])
    minus_path: str | None = None
    plus_path: str | None = None
    rename_from: str | None = None
    rename_to: str | None = None
    is_binary = False
    is_new = False
    is_deleted = False

    index = 1
    while index < len(lines):
        line = lines[index]
        if line.startswith("@@"):
            break
        if line.startswith("new file mode"):
            is_new = True
        elif line.startswith("deleted file mode"):
            is_deleted = True
        elif line.startswith("rename from "):
            rename_from = unquote_path(line[len("rename from ") :])
        elif line.startswith("rename to "):
            rename_to = unquote_path(line[len("rename to ") :])
        elif line.startswith("copy from "):
            rename_from = unquote_path(line[len("copy from ") :])
        elif line.startswith("copy to "):
            rename_to = unquote_path(line[len("copy to ") :])
        elif line.startswith("--- "):
            minus_path = _strip_side_prefix(line[4:])
        elif line.startswith("+++ "):
            plus_path = _strip_side_prefix(line[4:])
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            is_binary = True
        index += 1

    old_candidate = rename_from or minus_path or header_a
    new_candidate = rename_to or plus_path or header_b

    if is_deleted:
        path = old_candidate or new_candidate or ""
    else:
        path = new_candidate or old_candidate or ""

    old_path: str | None = None
    if old_candidate and old_candidate != path and not is_new:
        old_path = old_candidate

    hunks: list[Hunk] = []
    if not is_binary:
        for header, body in _split_hunks(lines[index:]):
            hunks.append(parse_hunk(header, body, path))

    return DiffFile(
        path=path,
        old_path=old_path,
        is_binary=is_binary,
        is_new=is_new,
        is_deleted=is_deleted,
        hunks=hunks,
        language=None,
        additions=sum(len(h.added_lines) for h in hunks),
        deletions=sum(len(h.removed_lines) for h in hunks),
    )


def _split_hunks(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Group the tail of a file section into ``(header, body_lines)`` pairs."""
    hunks: list[tuple[str, list[str]]] = []
    header: str | None = None
    body: list[str] = []
    for line in lines:
        if line.startswith("@@") and _HUNK_HEADER_RE.match(line):
            if header is not None:
                hunks.append((header, body))
            header, body = line, []
        elif header is not None:
            body.append(line)
    if header is not None:
        hunks.append((header, body))
    return hunks


def _parse_diff_git_header(line: str) -> tuple[str | None, str | None]:
    """Extract the ``a/`` and ``b/`` paths from a ``diff --git`` line.

    The line is genuinely ambiguous for unquoted paths containing spaces, so
    three strategies are tried in order: C-quoted fields, the "both halves are
    the same length" split git's own output guarantees for non-renames, and
    finally a naive split on the ``b/`` prefix. The ``---``/``+++`` headers are
    authoritative when present and override whatever this returns.
    """
    if not line.startswith("diff --git "):
        return (None, None)
    rest = line[len("diff --git ") :]

    fields = _split_quoted_pair(rest)
    if fields is not None:
        return (_strip_side_prefix(fields[0]), _strip_side_prefix(fields[1]))

    midpoint, remainder = divmod(len(rest), 2)
    if remainder == 1 and rest[midpoint] == " ":
        left, right = rest[:midpoint], rest[midpoint + 1 :]
        if left[:2] in {"a/", "b/"} and right[:2] in {"a/", "b/"}:
            return (_strip_side_prefix(left), _strip_side_prefix(right))

    marker = rest.find(" b/")
    if marker > 0:
        return (_strip_side_prefix(rest[:marker]), _strip_side_prefix(rest[marker + 1 :]))
    return (None, None)


def _split_quoted_pair(rest: str) -> tuple[str, str] | None:
    """Split ``"a/x" "b/y"`` (or a half-quoted pair) into two unquoted fields."""
    if '"' not in rest:
        return None
    fields: list[str] = []
    position = 0
    while position < len(rest) and len(fields) < 2:
        if rest[position] == " ":
            position += 1
            continue
        if rest[position] == '"':
            end = position + 1
            while end < len(rest):
                if rest[end] == "\\":
                    end += 2
                    continue
                if rest[end] == '"':
                    break
                end += 1
            if end >= len(rest):
                return None
            fields.append(unquote_path(rest[position : end + 1]))
            position = end + 1
        else:
            end = rest.find(" ", position)
            if end == -1:
                end = len(rest)
            fields.append(rest[position:end])
            position = end
    return (fields[0], fields[1]) if len(fields) == 2 else None


def _strip_side_prefix(raw: str) -> str | None:
    """Turn a ``--- a/x`` / ``+++ b/x`` payload into a plain path or ``None``.

    Git appends a tab plus a timestamp in some configurations; everything from
    the tab onwards is metadata, not the path.
    """
    value = unquote_path(raw.split("\t", 1)[0].strip())
    if not value or value == "/dev/null":
        return None
    if value[:2] in {"a/", "b/"}:
        return value[2:]
    return value


def parse_hunk_header(header: str) -> tuple[int, int, int, int]:
    """Parse ``@@ -old_start,old_count +new_start,new_count @@`` .

    The counts are optional in git's output (``@@ -1 +1 @@`` means one line on
    each side). Returns ``(old_start, old_count, new_start, new_count)``.

    :raises ValueError: the header is malformed.
    """
    match = _HUNK_HEADER_RE.match(header.strip())
    if match is None:
        raise ValueError(f"malformed hunk header: {header!r}")
    old_start = int(match.group(1))
    old_count = 1 if match.group(2) is None else int(match.group(2))
    new_start = int(match.group(3))
    new_count = 1 if match.group(4) is None else int(match.group(4))
    return (old_start, old_count, new_start, new_count)


def parse_hunk(header: str, body_lines: list[str], file_path: str) -> Hunk:
    """Build a :class:`~mergesignal.models.Hunk` from a header plus its body."""
    old_start, old_count, new_start, new_count = parse_hunk_header(header)

    added: list[str] = []
    removed: list[str] = []
    for line in body_lines:
        if not line:
            # An empty string is an empty *context* line: git writes a bare
            # space that trailing-whitespace-stripping pipelines often eat.
            continue
        marker, content = line[0], line[1:]
        if marker == "+":
            added.append(content)
        elif marker == "-":
            removed.append(content)
        elif marker == _NO_NEWLINE_PREFIX:
            continue  # "\ No newline at end of file" — belongs to neither side
        # anything else (' ') is context and contributes to no side

    return Hunk(
        file_path=file_path,
        base_range=LineRange(start=old_start, end=old_start + old_count),
        head_range=LineRange(start=new_start, end=new_start + new_count),
        added_lines=added,
        removed_lines=removed,
        header=header.rstrip("\n"),
    )


def unquote_path(raw: str) -> str:
    """Undo git's C-style quoting of paths containing spaces or non-ASCII bytes.

    ``"src/\\303\\251.py"`` -> ``src/é.py``. Unquoted input is returned as-is.

    Octal escapes are decoded as *bytes* and the accumulated byte string is
    decoded as UTF-8 at the end, because a multi-byte character arrives as
    several independent octal escapes.
    """
    value = raw.strip()
    if len(value) < 2 or not value.startswith('"') or not value.endswith('"'):
        return raw
    inner = value[1:-1]
    out = bytearray()
    index = 0
    while index < len(inner):
        char = inner[index]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(inner):
            out.extend(b"\\")
            break
        escape = inner[index]
        if escape in _C_ESCAPES:
            out.extend(_C_ESCAPES[escape].encode("utf-8"))
            index += 1
        elif escape.isdigit():
            digits = inner[index : index + 3]
            try:
                out.append(int(digits, 8))
            except ValueError:
                out.extend(escape.encode("utf-8"))
                index += 1
                continue
            index += len(digits)
        else:
            out.extend(escape.encode("utf-8"))
            index += 1
    return out.decode("utf-8", errors="replace")


def changed_line_numbers(diff_file: DiffFile, *, side: str = "head") -> set[int]:
    """Every 1-based line number touched on one side of a file.

    :param side: ``"head"`` or ``"base"``.
    :raises ValueError: ``side`` is neither.

    Used by the semantic signal to ask "did this change land inside the body of
    the symbol I care about?" and by overlap to compute line intersections.
    Pure insertions contribute nothing on the base side (the range is empty),
    which is exactly right: no base line was touched.
    """
    if side not in {"head", "base"}:
        raise ValueError(f"side must be 'head' or 'base', got {side!r}")
    numbers: set[int] = set()
    for hunk in diff_file.hunks:
        span = hunk.head_range if side == "head" else hunk.base_range
        numbers.update(range(max(span.start, 1), span.end))
    return numbers


def annotate_languages(diff: Diff) -> Diff:
    """Fill in ``DiffFile.language`` for every file using the language registry.

    Returns a new :class:`~mergesignal.models.Diff`; does not mutate the input.
    Files with unknown extensions keep ``language=None``.
    """
    return Diff(
        base=diff.base,
        head=diff.head,
        files=[f.model_copy(update={"language": language_for_path(f.path)}) for f in diff.files],
    )
