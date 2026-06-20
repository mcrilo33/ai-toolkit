"""Small, dependency-free text helpers."""

from __future__ import annotations


def is_blank(value: str | None) -> bool:
    """Return True when *value* is None, empty, or only whitespace."""
    return value is None or value.strip() == ""
