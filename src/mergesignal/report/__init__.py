"""Rendering a :class:`~mergesignal.models.Report` for humans and for GitHub.

Determinism is the contract here: the regression corpus snapshots renderer
output, so findings are always sorted by
:attr:`mergesignal.models.Finding.sort_key` and JSON keys follow model field
order. Any change to output shape must be a deliberate snapshot update.
"""

from mergesignal.report.render import render

__all__ = ["render"]
