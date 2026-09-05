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


# ------------------------------------------------------------------
# Live-API constraints of Moonshot's reasoning models (see entity_parser.py):
# no `temperature` may be sent, budgets must survive reasoning tokens, and a
# truncated body is reported as such instead of as a JSON parse failure.
# ------------------------------------------------------------------
class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content, finish_reason="stop"):
        self.message = _Msg(content)
        self.finish_reason = finish_reason


class _Resp:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [_Choice(content, finish_reason)]


class _FakeKimi:
    """Records the kwargs of the single chat.completions.create call."""

    def __init__(self, content, finish_reason="stop"):
        self.kwargs: dict = {}
        self._resp = _Resp(content, finish_reason)
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.kwargs = kwargs
                return outer._resp

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


@pytest.fixture
def fake_kimi(monkeypatch):
    def _install(content, finish_reason="stop"):
        fake = _FakeKimi(content, finish_reason)
        monkeypatch.setenv("KIMI_API_KEY", "test-key")
        monkeypatch.setattr(entity_parser, "get_kimi_client", lambda: fake)
        return fake

    return _install


@pytest.mark.asyncio
async def test_extract_entities_never_sends_temperature(fake_kimi) -> None:
    fake = fake_kimi('{"nodes": [], "edges": []}')
    await entity_parser.extract_entities("text")
    assert "temperature" not in fake.kwargs
    assert fake.kwargs["max_tokens"] == entity_parser.EXTRACT_MAX_TOKENS
    assert fake.kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_answer_query_never_sends_temperature(fake_kimi) -> None:
    fake = fake_kimi("An answer.")
    out = await entity_parser.answer_query("q", context="ctx")
    assert out == "An answer."
    assert "temperature" not in fake.kwargs
    assert fake.kwargs["max_tokens"] == entity_parser.ANSWER_MAX_TOKENS


def test_default_budgets_cover_reasoning_overhead() -> None:
    # A 4k budget was observed being eaten by ~3.5k reasoning tokens on
    # kimi-k2.7-code-highspeed; anything at or under that regresses the demo.
    assert entity_parser.EXTRACT_MAX_TOKENS > 4096
    assert entity_parser.ANSWER_MAX_TOKENS > 1024


@pytest.mark.asyncio
async def test_extract_entities_reports_truncation_not_json_error(fake_kimi) -> None:
    fake_kimi('{"nodes": [{"id": "a", "lab', finish_reason="length")
    result = await entity_parser.extract_entities("text")
    assert result == {"nodes": [], "edges": [], "error": entity_parser.TRUNCATED_ERROR}


@pytest.mark.asyncio
async def test_answer_query_empty_content_is_unavailable(fake_kimi) -> None:
    fake_kimi("", finish_reason="length")
    out = await entity_parser.answer_query("q", context="ctx")
    assert out.startswith(entity_parser.LLM_UNAVAILABLE_PREFIX)
