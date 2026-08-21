"""Tests for src.agent.nosana_client — hash-embedding fallback."""
from __future__ import annotations

import math
import string

import pytest

from src.agent.nosana_client import EMBEDDING_DIM, NosanaClient, _is_finite_embedding


def _varied_inputs() -> list[str]:
    inputs = ["", " ", "a", "hello world", "GraphAgent Forge" * 5]
    inputs += [str(i) for i in range(200)]
    inputs += [c * 3 for c in string.printable[:80]]
    inputs += [f"entity-{i}: some summary text here {i}" for i in range(220)]
    return inputs


@pytest.mark.parametrize("text", _varied_inputs())
def test_hash_embedding_shape_and_finiteness(text: str) -> None:
    vec = NosanaClient._hash_embedding(text)
    assert len(vec) == EMBEDDING_DIM
    assert all(isinstance(v, float) and math.isfinite(v) for v in vec)


@pytest.mark.parametrize("text", _varied_inputs())
def test_hash_embedding_unit_norm(text: str) -> None:
    vec = NosanaClient._hash_embedding(text)
    norm = math.sqrt(sum(v * v for v in vec))
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_hash_embedding_deterministic() -> None:
    a = NosanaClient._hash_embedding("deterministic input")
    b = NosanaClient._hash_embedding("deterministic input")
    assert a == b


def test_hash_embedding_differs_for_different_input() -> None:
    a = NosanaClient._hash_embedding("input one")
    b = NosanaClient._hash_embedding("input two")
    assert a != b


def test_is_finite_embedding_accepts_valid_vector() -> None:
    vec = [0.1] * EMBEDDING_DIM
    assert _is_finite_embedding(vec) is True


def test_is_finite_embedding_rejects_nan() -> None:
    vec = [0.1] * (EMBEDDING_DIM - 1) + [float("nan")]
    assert _is_finite_embedding(vec) is False


def test_is_finite_embedding_rejects_inf() -> None:
    vec = [0.1] * (EMBEDDING_DIM - 1) + [float("inf")]
    assert _is_finite_embedding(vec) is False


def test_is_finite_embedding_rejects_wrong_length() -> None:
    assert _is_finite_embedding([0.1] * (EMBEDDING_DIM - 1)) is False


def test_is_finite_embedding_rejects_non_list() -> None:
    assert _is_finite_embedding("not a list") is False
    assert _is_finite_embedding(None) is False


def test_is_finite_embedding_rejects_bools() -> None:
    vec = [0.1] * (EMBEDDING_DIM - 1) + [True]
    assert _is_finite_embedding(vec) is False


@pytest.mark.asyncio
async def test_get_embedding_no_config_uses_local_hash_fallback(monkeypatch) -> None:
    monkeypatch.delenv("NOSANA_API_KEY", raising=False)
    monkeypatch.delenv("NOSANA_EMBEDDING_URL", raising=False)
    client = NosanaClient()

    result = await client.get_embedding("some text to embed")

    assert len(result) == EMBEDDING_DIM
    assert all(math.isfinite(v) for v in result)
    assert client.last_embedding_method == "local_hash_fallback"
    await client.aclose()
