"""Tests for src.ingestion.entity_parser — behavior without a configured Kimi key."""
from __future__ import annotations

import pytest

from src.ingestion import entity_parser


@pytest.fixture(autouse=True)
def _no_kimi_key(monkeypatch):
    """Every test in this module runs with KIMI_API_KEY unset."""
    monkeypatch.delenv("KIMI_API_KEY", raising=False)


@pytest.mark.asyncio
async def test_extract_entities_returns_structured_error_without_key() -> None:
    result = await entity_parser.extract_entities("some text to extract from")

    assert result == {
        "nodes": [],
        "edges": [],
        "error": entity_parser.MISSING_KEY_ERROR,
    }


@pytest.mark.asyncio
async def test_answer_query_returns_cannot_answer_without_key() -> None:
    answer = await entity_parser.answer_query("What is this?", context="some context")

    assert answer.startswith("Cannot answer:")
    assert entity_parser.MISSING_KEY_ERROR in answer


def test_resolve_model_arg_takes_precedence(monkeypatch) -> None:
    monkeypatch.setenv("KIMI_MODEL", "env-model")
    assert entity_parser._resolve_model("explicit-model") == "explicit-model"


def test_resolve_model_env_used_when_no_arg(monkeypatch) -> None:
    monkeypatch.setenv("KIMI_MODEL", "env-model")
    assert entity_parser._resolve_model(None) == "env-model"


def test_resolve_model_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.delenv("KIMI_MODEL", raising=False)
    assert entity_parser._resolve_model(None) == entity_parser.DEFAULT_KIMI_MODEL
