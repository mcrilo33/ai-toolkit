"""Unit tests for the is_blank text helper."""

from scripts.telemetry._textutil import is_blank


def test_is_blank_none():
    assert is_blank(None) is True


def test_is_blank_empty():
    assert is_blank("") is True


def test_is_blank_whitespace_only():
    assert is_blank("  ") is True


def test_is_blank_non_blank():
    assert is_blank("x") is False
