"""Tests for src.graph.normalize.normalize_label — pure stdlib, offline."""
from __future__ import annotations

from src.graph.normalize import normalize_label


def test_case_folding() -> None:
    assert normalize_label("Kimi AI") == "kimi ai"
    assert normalize_label("KIMI ai") == "kimi ai"


def test_punctuation_stripped() -> None:
    assert normalize_label("Kimi, AI!") == "kimi ai"
    assert normalize_label("O'Brien & Sons, Inc.") == "obrien sons inc"


def test_whitespace_collapsed() -> None:
    assert normalize_label("Kimi   AI") == "kimi ai"
    assert normalize_label("\tKimi\nAI\t") == "kimi ai"


def test_contract_example() -> None:
    assert normalize_label("  Kimi, AI!  ") == "kimi ai"


def test_empty_string() -> None:
    assert normalize_label("") == ""


def test_none_like_empty_is_tolerated() -> None:
    # Docstring promises '' for empty/None-ish input so callers can treat
    # "no normal form" uniformly.
    assert normalize_label("") == ""


def test_unicode_letters_preserved() -> None:
    assert normalize_label("Café Kimi") == "café kimi"
    assert normalize_label("東京, Tokyo!") == "東京 tokyo"


def test_only_punctuation_yields_empty() -> None:
    assert normalize_label("!!! ,,, ...") == ""


def test_idempotent() -> None:
    once = normalize_label("  Kimi, AI!  ")
    assert normalize_label(once) == once
