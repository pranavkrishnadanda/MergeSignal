"""Extension -> tree-sitter grammar registry. **Owned by Agent C.**

Backed by ``tree-sitter-language-pack``: one dependency, every grammar. The
registry is intentionally a plain dict so adding a language is a one-line
change, and every lookup miss is a *supported* outcome (``None`` -> the caller
falls back to textual analysis), never an exception (NFR-3).

Minimum coverage required by DESIGN.md: ``.py .js .ts .tsx .jsx .go .rs .java``.

Two distinct notions of "supported" live in this package and must not be
confused:

``language_for_path`` / :data:`EXTENSION_TO_LANGUAGE`
    "We recognise this extension and can name a grammar for it." This is what
    fills :attr:`~mergesignal.models.DiffFile.language`.
:data:`mergesignal.analysis.symbols.SUPPORTED_LANGUAGES`
    "We have symbol-extraction rules for this grammar." A file may be
    recognised (``language='yaml'``) yet yield no symbols; that is a normal,
    non-error degradation.

Grammar loading is lazy and cached: nothing is loaded at import time, so
importing MergeSignal stays cheap for the CLI's ``--help`` path.
"""

from __future__ import annotations

import os
from functools import cache
from typing import Any

#: Extension (lowercase, with leading dot) -> tree-sitter language id.
#: Agent C owns the contents; the keys here are the required minimum.
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    # --- required by DESIGN.md ------------------------------------------
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    # --- recognised, parsed, but no symbol rules (textual-ish fallback) --
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".lua": "lua",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
}

#: Filenames without a useful extension that still map to a grammar.
FILENAME_TO_LANGUAGE: dict[str, str] = {
    "Dockerfile": "dockerfile",
    "Makefile": "make",
    "makefile": "make",
    "GNUmakefile": "make",
}


class LanguageUnavailableError(LookupError):
    """The language pack cannot supply a grammar for this language id.

    A :class:`LookupError` subclass so callers can keep catching the documented
    ``LookupError`` while still telling "we never heard of it" apart from "the
    pack refused to load it" via the message.
    """


def _basename(path: str) -> str:
    """Final path component, tolerating both POSIX and Windows separators."""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def language_for_path(path: str) -> str | None:
    """Return the tree-sitter language id for ``path``, or ``None``.

    Dispatches on the lowercased final extension, then on the bare filename.
    ``None`` means "unsupported" and routes the caller to the textual fallback —
    it is never an error.

    Dotfiles without a further extension (``.gitignore``) have no extension as
    far as :func:`os.path.splitext` is concerned, which is the behaviour we
    want: they fall through to the filename table and then to ``None``.
    """
    name = _basename(path)
    if not name:
        return None
    _, ext = os.path.splitext(name)
    if ext:
        language = EXTENSION_TO_LANGUAGE.get(ext.lower())
        if language is not None:
            return language
    return FILENAME_TO_LANGUAGE.get(name)


def is_supported(path: str) -> bool:
    """``True`` when :func:`language_for_path` would return a language."""
    return language_for_path(path) is not None


@cache
def get_language(language: str) -> Any:
    """Return the cached ``tree_sitter.Language`` object for ``language``.

    :raises LookupError: unknown language id, or the pack cannot provide the
        grammar in this environment (some pack builds fetch grammars on demand
        and have no network).
    """
    try:
        from tree_sitter_language_pack import get_language as _pack_get_language
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise LanguageUnavailableError(f"tree-sitter-language-pack is unavailable: {exc}") from exc
    try:
        return _pack_get_language(language)  # type: ignore[arg-type]
    except Exception as exc:
        raise LanguageUnavailableError(f"no tree-sitter grammar for {language!r}: {exc}") from exc


@cache
def get_parser(language: str) -> Any:
    """Return a cached ``tree_sitter.Parser`` configured for ``language``.

    Parsers are cached per language because constructing one is comparatively
    expensive and analysis touches many files of the same language.

    :raises LookupError: the language pack does not ship that grammar.
    """
    import tree_sitter

    return tree_sitter.Parser(get_language(language))


def parse_source(source: str | bytes, language: str) -> Any:
    """Parse ``source`` and return the ``tree_sitter.Tree``.

    Tree-sitter is error-tolerant: a file with syntax errors still yields a tree
    containing ``ERROR`` nodes, and callers extract whatever is parseable rather
    than giving up on the file.

    :raises LookupError: unknown language.
    """
    payload = source.encode("utf-8", errors="replace") if isinstance(source, str) else source
    return get_parser(language).parse(payload)


def supported_languages() -> list[str]:
    """Sorted list of distinct language ids the registry can produce."""
    return sorted(set(EXTENSION_TO_LANGUAGE.values()) | set(FILENAME_TO_LANGUAGE.values()))


def extensions_for(language: str) -> list[str]:
    """Every registered extension mapping to ``language``, sorted.

    Handy for tests and for ``scan`` style commands that want a pathspec.
    """
    return sorted(ext for ext, lang in EXTENSION_TO_LANGUAGE.items() if lang == language)
