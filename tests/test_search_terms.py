"""Tests for GraphRAGEngine._extract_search_terms."""
from __future__ import annotations

from unittest.mock import AsyncMock

from src.graph.graphrag import GraphRAGEngine


def _engine() -> GraphRAGEngine:
    # neo4j/nosana are never called by _extract_search_terms; mocks are enough.
    return GraphRAGEngine(neo4j=AsyncMock(), nosana=AsyncMock())


def test_stopwords_are_dropped() -> None:
    engine = _engine()
    terms = engine._extract_search_terms("What is the capital of France?")
    assert "what" not in terms
    assert "is" not in terms
    assert "the" not in terms
    assert "of" not in terms
    assert "capital" in terms
    assert "france" in terms


def test_punctuation_is_stripped() -> None:
    engine = _engine()
    terms = engine._extract_search_terms("Who founded OpenAI, and when?")
    assert "openai," not in terms
    assert "openai" in terms
    assert "when?" not in terms


def test_capped_at_six_terms() -> None:
    engine = _engine()
    question = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    terms = engine._extract_search_terms(question)
    assert len(terms) <= 6


def test_full_question_not_among_terms() -> None:
    engine = _engine()
    question = "What technologies does GraphAgent Forge use for embeddings?"
    terms = engine._extract_search_terms(question)
    assert question not in terms
    assert question.lower() not in terms


def test_short_words_dropped() -> None:
    engine = _engine()
    terms = engine._extract_search_terms("is it up on at to")
    assert terms == []


def test_duplicate_terms_deduplicated() -> None:
    engine = _engine()
    terms = engine._extract_search_terms("graph graph graph knowledge")
    assert terms.count("graph") == 1
