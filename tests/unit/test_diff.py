"""Unit tests for structured diff parsing (``git diff`` text -> models)."""

from __future__ import annotations

import pytest

from mergesignal.analysis.diff import (
    annotate_languages,
    changed_line_numbers,
    parse_diff,
    parse_file_diff,
    parse_hunk,
    parse_hunk_header,
    unquote_path,
)
from mergesignal.models import Diff, DiffFile

MODIFY_DIFF = """diff --git a/src/app.py b/src/app.py
index 1234567..89abcde 100644
--- a/src/app.py
+++ b/src/app.py
@@ -3,2 +3,3 @@ def greet(name):
-    return "hi"
-    # old
+    return "hello"
+    # new
+    # extra
"""

NEW_FILE_DIFF = """diff --git a/pkg/new.py b/pkg/new.py
new file mode 100644
index 0000000..3e75765
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1,2 @@
+def added():
+    return 1
"""

DELETED_FILE_DIFF = """diff --git a/pkg/gone.py b/pkg/gone.py
deleted file mode 100644
index 3e75765..0000000
--- a/pkg/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def gone():
-    return 1
"""

RENAME_DIFF = """diff --git a/old/name.py b/new/name.py
similarity index 92%
rename from old/name.py
rename to new/name.py
index 1234567..89abcde 100644
--- a/old/name.py
+++ b/new/name.py
@@ -1 +1 @@
-def a(): ...
+def b(): ...
"""

PURE_RENAME_DIFF = """diff --git a/old.py b/moved.py
similarity index 100%
rename from old.py
rename to moved.py
"""

BINARY_DIFF = """diff --git a/assets/logo.png b/assets/logo.png
index 0f49c4a..2e63df1 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
"""

MODE_ONLY_DIFF = """diff --git a/run.sh b/run.sh
old mode 100644
new mode 100755
"""

NO_NEWLINE_DIFF = """diff --git a/tail.txt b/tail.txt
index 1234567..89abcde 100644
--- a/tail.txt
+++ b/tail.txt
@@ -1 +1 @@
-one
\\ No newline at end of file
+two
\\ No newline at end of file
"""

SPACED_PATH_DIFF = """diff --git a/sp ace.txt b/sp ace.txt
index c0d0fb4..83db48f 100644
--- a/sp ace.txt\t
+++ b/sp ace.txt\t
@@ -2,0 +3 @@ line2
+line3
"""

QUOTED_PATH_DIFF = """diff --git "a/src/caf\\303\\251.py" "b/src/caf\\303\\251.py"
index c0d0fb4..83db48f 100644
--- "a/src/caf\\303\\251.py"
+++ "b/src/caf\\303\\251.py"
@@ -1 +1 @@
-a = 1
+a = 2
"""

CONTEXT_DIFF = """diff --git a/ctx.py b/ctx.py
index 1234567..89abcde 100644
--- a/ctx.py
+++ b/ctx.py
@@ -1,5 +1,5 @@
 unchanged one
 unchanged two
-removed
+added
 unchanged three
 unchanged four
"""


# ------------------------------------------------------------- hunk headers


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("@@ -3,2 +3,3 @@ def greet(name):", (3, 2, 3, 3)),
        ("@@ -1 +1 @@", (1, 1, 1, 1)),
        ("@@ -0,0 +1,2 @@", (0, 0, 1, 2)),
        ("@@ -1,2 +0,0 @@", (1, 2, 0, 0)),
        ("@@ -2,0 +3 @@ line2", (2, 0, 3, 1)),
    ],
)
def test_parse_hunk_header(header: str, expected: tuple[int, int, int, int]) -> None:
    assert parse_hunk_header(header) == expected


@pytest.mark.parametrize("header", ["", "not a hunk", "@@ -a,b +c,d @@", "@@ -1,2 @@"])
def test_parse_hunk_header_rejects_garbage(header: str) -> None:
    with pytest.raises(ValueError, match="malformed hunk header"):
        parse_hunk_header(header)


def test_parse_hunk_splits_sides_and_keeps_the_header() -> None:
    hunk = parse_hunk("@@ -3,2 +3,3 @@ ctx", ["-old", "+new", "+extra", " same"], "a.py")
    assert hunk.file_path == "a.py"
    assert (hunk.base_range.start, hunk.base_range.end) == (3, 5)
    assert (hunk.head_range.start, hunk.head_range.end) == (3, 6)
    assert hunk.removed_lines == ["old"]
    assert hunk.added_lines == ["new", "extra"]
    assert hunk.header == "@@ -3,2 +3,3 @@ ctx"


# -------------------------------------------------------------- path quoting


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain/path.py", "plain/path.py"),
        ('"src/caf\\303\\251.py"', "src/café.py"),
        ('"sp ace.py"', "sp ace.py"),
        ('"tab\\there.py"', "tab\there.py"),
        ('"quote\\".py"', 'quote".py'),
        ('"back\\\\slash.py"', "back\\slash.py"),
    ],
)
def test_unquote_path(raw: str, expected: str) -> None:
    assert unquote_path(raw) == expected


# ------------------------------------------------------------- whole sections


def test_parse_modification() -> None:
    diff = parse_diff(MODIFY_DIFF, base="main", head="feature")
    assert diff.base == "main" and diff.head == "feature"
    assert len(diff.files) == 1
    changed = diff.files[0]
    assert changed.path == "src/app.py"
    assert changed.old_path is None
    assert not changed.is_new and not changed.is_deleted and not changed.is_binary
    assert changed.additions == 3
    assert changed.deletions == 2
    assert changed.churn == 5
    assert len(changed.hunks) == 1
    assert changed.hunks[0].added_lines == ['    return "hello"', "    # new", "    # extra"]


def test_parse_new_file() -> None:
    changed = parse_diff(NEW_FILE_DIFF).files[0]
    assert changed.path == "pkg/new.py"
    assert changed.is_new is True
    assert changed.is_deleted is False
    assert changed.old_path is None
    assert changed.hunks[0].base_range.length == 0
    assert changed.hunks[0].is_pure_addition


def test_parse_deleted_file() -> None:
    changed = parse_diff(DELETED_FILE_DIFF).files[0]
    assert changed.path == "pkg/gone.py"
    assert changed.is_deleted is True
    assert changed.is_new is False
    assert changed.hunks[0].head_range.length == 0
    assert changed.hunks[0].is_pure_deletion


def test_parse_rename_with_edits() -> None:
    changed = parse_diff(RENAME_DIFF).files[0]
    assert changed.path == "new/name.py"
    assert changed.old_path == "old/name.py"
    assert changed.is_rename is True
    assert changed.additions == 1 and changed.deletions == 1


def test_parse_pure_rename_has_no_hunks() -> None:
    changed = parse_diff(PURE_RENAME_DIFF).files[0]
    assert (changed.path, changed.old_path) == ("moved.py", "old.py")
    assert changed.hunks == []
    assert changed.churn == 0


def test_parse_binary_file_carries_no_hunks() -> None:
    changed = parse_diff(BINARY_DIFF).files[0]
    assert changed.path == "assets/logo.png"
    assert changed.is_binary is True
    assert changed.hunks == []
    assert changed.additions == 0 and changed.deletions == 0


def test_mode_only_change_is_a_file_with_no_hunks() -> None:
    changed = parse_diff(MODE_ONLY_DIFF).files[0]
    assert changed.path == "run.sh"
    assert changed.hunks == []
    assert changed.is_binary is False


def test_no_newline_marker_counts_for_neither_side() -> None:
    changed = parse_diff(NO_NEWLINE_DIFF).files[0]
    assert changed.additions == 1
    assert changed.deletions == 1
    assert changed.hunks[0].added_lines == ["two"]
    assert changed.hunks[0].removed_lines == ["one"]


def test_paths_with_spaces_survive_the_ambiguous_header() -> None:
    changed = parse_diff(SPACED_PATH_DIFF).files[0]
    assert changed.path == "sp ace.txt"
    assert changed.old_path is None


def test_c_quoted_unicode_paths_are_decoded() -> None:
    changed = parse_diff(QUOTED_PATH_DIFF).files[0]
    assert changed.path == "src/café.py"


def test_context_lines_are_not_counted_as_changes() -> None:
    changed = parse_diff(CONTEXT_DIFF).files[0]
    assert changed.additions == 1
    assert changed.deletions == 1
    assert changed.hunks[0].added_lines == ["added"]


def test_multiple_files_in_one_diff_keep_git_order() -> None:
    diff = parse_diff(NEW_FILE_DIFF + BINARY_DIFF + DELETED_FILE_DIFF)
    assert [f.path for f in diff.files] == ["pkg/new.py", "assets/logo.png", "pkg/gone.py"]
    assert diff.paths == {"pkg/new.py", "assets/logo.png", "pkg/gone.py"}
    assert diff.total_churn == 4
    assert diff.by_path("pkg/new.py") is not None
    assert diff.by_path("nope.py") is None


def test_empty_input_yields_an_empty_diff() -> None:
    assert parse_diff("").files == []
    assert parse_diff("\n\n").files == []


def test_preamble_before_the_first_section_is_ignored() -> None:
    show_output = "commit deadbeef\nAuthor: A <a@b.c>\nDate: today\n\n    message\n\n" + MODIFY_DIFF
    assert [f.path for f in parse_diff(show_output).files] == ["src/app.py"]


def test_hunk_bodies_never_confuse_the_section_splitter() -> None:
    """A removed line that *looks* like a header is still a removed line."""
    tricky = (
        "diff --git a/meta.txt b/meta.txt\n"
        "index 1..2 100644\n"
        "--- a/meta.txt\n"
        "+++ b/meta.txt\n"
        "@@ -1,2 +1,2 @@\n"
        "-diff --git a/fake b/fake\n"
        "+@@ -9,9 +9,9 @@\n"
    )
    diff = parse_diff(tricky)
    assert len(diff.files) == 1
    assert diff.files[0].hunks[0].removed_lines == ["diff --git a/fake b/fake"]
    assert diff.files[0].hunks[0].added_lines == ["@@ -9,9 +9,9 @@"]


def test_parse_file_diff_requires_a_header() -> None:
    with pytest.raises(ValueError, match="diff --git"):
        parse_file_diff([])


# --------------------------------------------------------------- derived data


def test_changed_line_numbers_per_side() -> None:
    changed = parse_diff(MODIFY_DIFF).files[0]
    assert changed_line_numbers(changed, side="head") == {3, 4, 5}
    assert changed_line_numbers(changed, side="base") == {3, 4}


def test_changed_line_numbers_of_a_pure_insertion_touch_no_base_line() -> None:
    changed = parse_diff(NEW_FILE_DIFF).files[0]
    assert changed_line_numbers(changed, side="base") == set()
    assert changed_line_numbers(changed, side="head") == {1, 2}


def test_changed_line_numbers_rejects_a_bogus_side() -> None:
    changed = parse_diff(MODIFY_DIFF).files[0]
    with pytest.raises(ValueError, match="side must be"):
        changed_line_numbers(changed, side="theirs")


def test_annotate_languages_fills_known_extensions_only() -> None:
    diff = Diff(
        base="a",
        head="b",
        files=[DiffFile(path="src/app.py"), DiffFile(path="notes.zzz"), DiffFile(path="m.go")],
    )
    annotated = annotate_languages(diff)
    assert [f.language for f in annotated.files] == ["python", None, "go"]
    assert [f.language for f in diff.files] == [None, None, None], "input must not be mutated"
