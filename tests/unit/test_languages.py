"""Unit tests for the extension -> tree-sitter grammar registry."""

from __future__ import annotations

import pytest

from mergesignal.analysis import languages
from mergesignal.analysis.languages import (
    EXTENSION_TO_LANGUAGE,
    LanguageUnavailableError,
    extensions_for,
    get_language,
    get_parser,
    is_supported,
    language_for_path,
    parse_source,
    supported_languages,
)

#: Coverage DESIGN.md requires, extension -> expected grammar id.
REQUIRED = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}


@pytest.mark.parametrize(("extension", "expected"), sorted(REQUIRED.items()))
def test_required_extensions_are_registered(extension: str, expected: str) -> None:
    assert EXTENSION_TO_LANGUAGE[extension] == expected
    assert language_for_path(f"src/pkg/module{extension}") == expected


def test_language_lookup_is_case_insensitive_on_the_extension() -> None:
    assert language_for_path("SRC/Module.PY") == "python"


def test_unknown_extension_is_none_not_an_error() -> None:
    assert language_for_path("script.zzz") is None
    assert language_for_path("data/blob.unknownext") is None
    assert is_supported("script.zzz") is False


def test_extensionless_and_dotfiles_fall_through_to_the_filename_table() -> None:
    assert language_for_path("LICENSE") is None
    assert language_for_path(".gitignore") is None
    assert language_for_path("deploy/Dockerfile") == "dockerfile"
    assert language_for_path("Makefile") == "make"


def test_windows_separators_are_tolerated() -> None:
    assert language_for_path(r"src\pkg\module.py") == "python"


def test_empty_path_is_none() -> None:
    assert language_for_path("") is None
    assert language_for_path("src/") is None


def test_supported_languages_is_sorted_and_deduplicated() -> None:
    result = supported_languages()
    assert result == sorted(set(result))
    for expected in REQUIRED.values():
        assert expected in result


def test_extensions_for_groups_aliases() -> None:
    assert ".js" in extensions_for("javascript")
    assert ".jsx" in extensions_for("javascript")
    assert extensions_for("javascript") == sorted(extensions_for("javascript"))
    assert extensions_for("no-such-language") == []


def test_parsers_and_languages_are_cached() -> None:
    assert get_parser("python") is get_parser("python")
    assert get_language("python") is get_language("python")


def test_unknown_language_raises_lookup_error() -> None:
    with pytest.raises(LookupError):
        get_language("definitely-not-a-grammar")
    with pytest.raises(LookupError):
        parse_source("x = 1", "definitely-not-a-grammar")


def test_language_unavailable_error_is_a_lookup_error() -> None:
    assert issubclass(LanguageUnavailableError, LookupError)


@pytest.mark.parametrize("language", sorted(set(REQUIRED.values())))
def test_every_required_grammar_parses(language: str) -> None:
    tree = parse_source("", language)
    assert tree.root_node is not None


def test_parse_source_accepts_bytes_and_str() -> None:
    from_text = parse_source("def f():\n    pass\n", "python")
    from_bytes = parse_source(b"def f():\n    pass\n", "python")
    assert str(from_text.root_node) == str(from_bytes.root_node)


def test_syntax_errors_still_produce_a_tree() -> None:
    tree = parse_source("def broken(:::\n", "python")
    assert tree.root_node.type == "module"
    assert tree.root_node.has_error


def test_registry_is_a_plain_mutable_dict() -> None:
    """Adding a language must stay a one-line change (module docstring promise)."""
    assert isinstance(languages.EXTENSION_TO_LANGUAGE, dict)
    assert isinstance(languages.FILENAME_TO_LANGUAGE, dict)
