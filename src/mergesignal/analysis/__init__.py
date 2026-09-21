"""Code analysis: structured diffs, language detection, symbols and indexes.

This subpackage is pure-ish: it takes text (from :mod:`mergesignal.git.repo`)
and produces models. It never shells out to git itself except through a passed
:class:`~mergesignal.git.repo.Repo`.

The guiding rule is **degrade, never crash** (NFR-3): an unsupported language
yields no symbols and ``language=None``; a file with syntax errors yields
whatever tree-sitter could parse; a binary file is skipped entirely.
"""
