"""Unit tests for the pure parsing half of :mod:`mergesignal.git.merge_sim`.

Everything here runs without touching a repository: conflict-marker parsing,
``merge-tree`` output parsing and path extraction. Anything that needs real git
lives in ``tests/integration/test_merge_sim_int.py``.
"""

from __future__ import annotations

import pytest

from mergesignal.git.merge_sim import (
    _binary_region,
    _is_binary_conflict,
    _is_marker,
    _parse_merge_tree_info,
    _strip_stage_entry,
    _whole_file_region,
    parse_conflict_markers,
    parse_merge_tree_output,
)

# A faithful capture of `git merge-tree --write-tree -z main feature` for a repo
# with one text conflict in a filename containing a space.
MERGE_TREE_Z = (
    "f3f7bb565fb7fe07e40788bed4e5273425e8e9d4\0"
    "100644 83db48f84ec878fbfb30b46d16630e944e34f205 1\tmy file.txt\0"
    "100644 73f7c4b0b0a25d671499f02ffc179ab2b452aeec 2\tmy file.txt\0"
    "100644 8e2988c56e93bdc0c1f75c154e7dfab1e84e410a 3\tmy file.txt\0"
    "\0"
    "1\0my file.txt\0Auto-merging\0Auto-merging my file.txt\n\0"
    "1\0my file.txt\0CONFLICT (contents)\0CONFLICT (content): Merge conflict in my file.txt\n\0"
)

# The same command against a repo with a binary conflict and a modify/delete.
MERGE_TREE_Z_MIXED = (
    "80854f9c4e0d28d4560f8628609fd7a685cda30a\0"
    "addadd.txt\0asset.bin\0keep.txt\0"
    "\0"
    "1\0addadd.txt\0CONFLICT (contents)\0CONFLICT (add/add): Merge conflict in addadd.txt\n\0"
    "1\0asset.bin\0CONFLICT (binary)\0warning: Cannot merge binary files: asset.bin\n\0"
    "1\0keep.txt\0CONFLICT (modify/delete)\0CONFLICT (modify/delete): keep.txt deleted in feature\n\0"
)


# ------------------------------------------------------------ marker parsing


def test_parse_conflict_markers_returns_nothing_without_markers() -> None:
    assert parse_conflict_markers("a\nb\nc\n", "a.txt") == []
    assert parse_conflict_markers("", "a.txt") == []


def test_parse_conflict_markers_default_style() -> None:
    text = "head\n<<<<<<< main\nours\n=======\ntheirs\n>>>>>>> feature\ntail\n"
    (region,) = parse_conflict_markers(text, "a.txt")

    assert region.file == "a.txt"
    assert region.ours_text == "ours\n"
    assert region.theirs_text == "theirs\n"
    assert region.base_text is None
    assert region.is_binary is False
    # Ranges are 1-based half-open over the merged buffer, markers excluded.
    assert (region.ours_range.start, region.ours_range.end) == (3, 4)
    assert (region.theirs_range.start, region.theirs_range.end) == (5, 6)


def test_parse_conflict_markers_diff3_style_captures_base() -> None:
    text = "<<<<<<< main\nours\n||||||| abc1234\nbase\n=======\ntheirs\n>>>>>>> feature\n"
    (region,) = parse_conflict_markers(text, "a.txt")

    assert region.ours_text == "ours\n"
    assert region.base_text == "base\n"
    assert region.theirs_text == "theirs\n"
    assert (region.ours_range.start, region.ours_range.end) == (2, 3)
    assert (region.theirs_range.start, region.theirs_range.end) == (6, 7)


def test_parse_conflict_markers_multiple_regions_keep_absolute_line_numbers() -> None:
    text = (
        "<<<<<<< main\nA\n=======\nB\n>>>>>>> feature\n"  # lines 1-5
        "middle\n"  # line 6
        "<<<<<<< main\nC\n=======\nD\n>>>>>>> feature\n"  # lines 7-11
    )
    first, second = parse_conflict_markers(text, "a.txt")

    assert (first.ours_range.start, first.theirs_range.start) == (2, 4)
    assert (second.ours_range.start, second.theirs_range.start) == (8, 10)
    assert second.ours_text == "C\n"
    assert second.theirs_text == "D\n"


def test_parse_conflict_markers_empty_side_yields_empty_range() -> None:
    """A pure deletion on one side contributes no lines, not a bogus range."""
    text = "<<<<<<< main\n=======\ntheirs\n>>>>>>> feature\n"
    (region,) = parse_conflict_markers(text, "a.txt")

    assert region.ours_text is None
    assert region.ours_range.length == 0
    assert region.theirs_text == "theirs\n"


def test_parse_conflict_markers_unterminated_runs_to_eof() -> None:
    """A truncated conflict must not raise — we report what is there."""
    text = "<<<<<<< main\nours\n=======\ntheirs but no closing marker\n"
    (region,) = parse_conflict_markers(text, "a.txt")

    assert region.ours_text == "ours\n"
    assert region.theirs_text == "theirs but no closing marker\n"
    assert region.theirs_range.end == 5


def test_parse_conflict_markers_tolerates_nested_open_marker() -> None:
    text = "<<<<<<< main\nours\n<<<<<<< nested\n=======\ntheirs\n>>>>>>> feature\n"
    (region,) = parse_conflict_markers(text, "a.txt")

    assert region.ours_text == "ours\n<<<<<<< nested\n"
    assert region.theirs_text == "theirs\n"


def test_parse_conflict_markers_ignores_separator_outside_conflict() -> None:
    """A reStructuredText underline is not a conflict separator."""
    assert parse_conflict_markers("Title\n=======\nbody\n", "doc.rst") == []


def test_parse_conflict_markers_preserves_missing_trailing_newline() -> None:
    text = "<<<<<<< main\nours\n=======\ntheirs"
    (region,) = parse_conflict_markers(text, "a.txt")

    assert region.theirs_text == "theirs"


def test_parse_conflict_markers_handles_crlf() -> None:
    text = "<<<<<<< main\r\nours\r\n=======\r\ntheirs\r\n>>>>>>> feature\r\n"
    (region,) = parse_conflict_markers(text, "a.txt")

    assert region.ours_text == "ours\r\n"
    assert region.theirs_text == "theirs\r\n"


@pytest.mark.parametrize(
    ("line", "marker", "expected"),
    [
        ("<<<<<<<", "<<<<<<<", True),
        ("<<<<<<< main", "<<<<<<<", True),
        ("<<<<<<<\tmain", "<<<<<<<", True),
        ("<<<<<<<<", "<<<<<<<", False),  # eight chars: not a git marker
        ("<<<<<<", "<<<<<<<", False),
        ("=======", "=======", True),
        ("=========", "=======", False),
        ("", "<<<<<<<", False),
    ],
)
def test_is_marker_requires_exact_run_length(line: str, marker: str, expected: bool) -> None:
    assert _is_marker(line, marker) is expected


# -------------------------------------------------------- merge-tree parsing


def test_parse_merge_tree_output_empty() -> None:
    assert parse_merge_tree_output("") == (None, [])


def test_parse_merge_tree_output_clean_merge_has_tree_and_no_paths() -> None:
    tree, paths = parse_merge_tree_output("afc7654a4982883e4c9cbd7975de39165a61b14d\0")

    assert tree == "afc7654a4982883e4c9cbd7975de39165a61b14d"
    assert paths == []


def test_parse_merge_tree_output_collapses_stage_entries() -> None:
    """Three stage entries for one path must yield exactly one path."""
    tree, paths = parse_merge_tree_output(MERGE_TREE_Z)

    assert tree == "f3f7bb565fb7fe07e40788bed4e5273425e8e9d4"
    assert paths == ["my file.txt"]


def test_parse_merge_tree_output_accepts_name_only_form() -> None:
    tree, paths = parse_merge_tree_output(MERGE_TREE_Z_MIXED)

    assert tree == "80854f9c4e0d28d4560f8628609fd7a685cda30a"
    assert paths == ["addadd.txt", "asset.bin", "keep.txt"]


def test_parse_merge_tree_output_stops_at_message_block() -> None:
    """The informational block must never leak into the path list."""
    _tree, paths = parse_merge_tree_output(MERGE_TREE_Z)

    assert not any("CONFLICT" in path or "Auto-merging" in path for path in paths)


def test_parse_merge_tree_output_newline_form() -> None:
    stdout = "abc123\nsrc/a.py\nsrc/b.py\n\nAuto-merging src/a.py\n"
    tree, paths = parse_merge_tree_output(stdout)

    assert tree == "abc123"
    assert paths == ["src/a.py", "src/b.py"]


def test_parse_merge_tree_output_preserves_unicode_and_spaces() -> None:
    stdout = "abc123\0dir with space/ünïcodé.txt\0\0"
    _tree, paths = parse_merge_tree_output(stdout)

    assert paths == ["dir with space/ünïcodé.txt"]


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("100644 83db48f 1\tmy file.txt", "my file.txt"),
        ("bare/path.py", "bare/path.py"),
        ("weird\tname.txt", "weird\tname.txt"),  # not a stage entry: kept whole
        ("a/b.py", "a/b.py"),
    ],
)
def test_strip_stage_entry(field: str, expected: str) -> None:
    assert _strip_stage_entry(field) == expected


def test_parse_merge_tree_info_maps_paths_to_conflict_kinds() -> None:
    info = _parse_merge_tree_info(MERGE_TREE_Z_MIXED)

    assert set(info) == {"addadd.txt", "asset.bin", "keep.txt"}
    assert info["asset.bin"] == ("CONFLICT (binary)",)
    assert info["keep.txt"] == ("CONFLICT (modify/delete)",)


def test_parse_merge_tree_info_drops_auto_merging_noise() -> None:
    info = _parse_merge_tree_info(MERGE_TREE_Z)

    assert info == {"my file.txt": ("CONFLICT (contents)",)}


def test_parse_merge_tree_info_is_empty_without_nul_payload() -> None:
    assert _parse_merge_tree_info("abc123\nsrc/a.py\n\nAuto-merging src/a.py\n") == {}


def test_is_binary_conflict() -> None:
    assert _is_binary_conflict(("CONFLICT (binary)",)) is True
    assert _is_binary_conflict(("CONFLICT (contents)",)) is False
    assert _is_binary_conflict(()) is False


# --------------------------------------------------------- region synthesis


def test_binary_region_carries_no_text_or_ranges() -> None:
    region = _binary_region("asset.bin")

    assert region.is_binary is True
    assert region.ours_text is None and region.theirs_text is None
    assert region.ours_range.length == 0 and region.theirs_range.length == 0


def test_whole_file_region_spans_the_file() -> None:
    region = _whole_file_region("keep.txt", "a\nb\nc\n")

    assert (region.ours_range.start, region.ours_range.end) == (1, 4)
    assert region.ours_text == "a\nb\nc\n"
    assert region.theirs_text is None
    assert region.is_binary is False


def test_whole_file_region_of_empty_file_is_legal() -> None:
    region = _whole_file_region("empty.txt", "")

    assert (region.ours_range.start, region.ours_range.end) == (1, 1)
    assert region.ours_text is None
