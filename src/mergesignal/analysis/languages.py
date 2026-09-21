"""Extension -> tree-sitter grammar registry. **Owned by Agent C.**

Backed by ``tree-sitter-language-pack``: one dependency, every grammar. The
registry is intentionally a plain dict so adding a language is a one-line
change, and every lookup miss is a *supported* outcome (``None`` -> the caller
falls back to textual analysis), never an exception (NFR-3).

Minimum coverage required by DESIGN.md: ``.py .js .ts .tsx .jsx .go .rs .java``.
"""

from __future__ import annotations

from typing import Any

#: Extension (lowercase, with leading dot) -> tree-sitter language id.
#: Agent C owns the contents; the keys here are the required minimum.
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}

#: Filenames without a useful extension that still map to a grammar.
FILENAME_TO_LANGUAGE: dict[str, str] = {
    "Dockerfile": "dockerfile",
    "Makefile": "make",
}


def language_for_path(path: str) -> str | None:
    """Return the tree-sitter language id for ``path``, or ``None``.

    Dispatches on the lowercased final extension, then on the bare filename.
    ``None`` means "unsupported" and routes the caller to the textual fallback —
    it is never an error.
    """
    raise NotImplementedError


def is_supported(path: str) -> bool:
    """``True`` when :func:`language_for_path` would return a language."""
    raise NotImplementedError


def get_parser(language: str) -> Any:
    """Return a cached ``tree_sitter.Parser`` configured for ``language``.

    Parsers are cached per language because constructing one is comparatively
    expensive and analysis touches many files of the same language.

    :raises LookupError: the language pack does not ship that grammar.
    """
    raise NotImplementedError


def get_language(language: str) -> Any:
    """Return the cached ``tree_sitter.Language`` object for ``language``.

    :raises LookupError: unknown language id.
    """
    raise NotImplementedError


def parse_source(source: str | bytes, language: str) -> Any:
    """Parse ``source`` and return the ``tree_sitter.Tree``.

    Tree-sitter is error-tolerant: a file with syntax errors still yields a tree
    containing ``ERROR`` nodes, and callers extract whatever is parseable rather
    than giving up on the file.

    :raises LookupError: unknown language.
    """
    raise NotImplementedError


def supported_languages() -> list[str]:
    """Sorted list of distinct language ids the registry can produce."""
    raise NotImplementedError
