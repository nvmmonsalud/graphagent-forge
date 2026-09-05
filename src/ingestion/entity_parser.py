"""Kimi AI (Moonshot) client — OpenAI-compatible wrapper."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import AsyncOpenAI

log = logging.getLogger(__name__)

DEFAULT_KIMI_MODEL = os.getenv("KIMI_MODEL", "kimi-k2.7-code-highspeed")
DEFAULT_KIMI_BASE_URL = "https://api.moonshot.ai/v1"
MISSING_KEY_ERROR = "KIMI_API_KEY not configured"

# Every `answer_query` string that is a failure notice rather than a real answer
# starts with this prefix.  Callers (GraphRAGEngine.query) classify on it instead
# of on the message text, so the return type stays a plain `str`.
LLM_UNAVAILABLE_PREFIX = "Cannot answer:"

# Moonshot's current Kimi models (k2.6 / k2.7-code / k2.7-code-highspeed / k3)
# are reasoning models with two hard constraints, both verified against the
# live API:
#   * `temperature` must be exactly 1 (any other value is a 400
#     "invalid temperature: only 1 is allowed for this model"), so we never
#     send the parameter and let the provider default apply;
#   * thinking cannot be disabled, and the reasoning tokens COUNT AGAINST
#     `max_tokens`.  A 4k budget was routinely eaten by ~3.5k reasoning tokens,
#     leaving a truncated (unparseable) JSON body.  Budgets below are sized so
#     the visible output survives a long think.
EXTRACT_MAX_TOKENS = int(os.getenv("KIMI_EXTRACT_MAX_TOKENS", "16384"))
ANSWER_MAX_TOKENS = int(os.getenv("KIMI_ANSWER_MAX_TOKENS", "4096"))
TRUNCATED_ERROR = "LLM output truncated: raise KIMI_EXTRACT_MAX_TOKENS"

_kimi_client: AsyncOpenAI | None = None


def _api_key() -> str:
    """Return the configured Kimi API key, or '' if unset/blank."""
    return (os.getenv("KIMI_API_KEY") or "").strip()


def _resolve_model(model: str | None) -> str:
    """Explicit argument > live env > module default.

    The live env re-read matters because src/main.py calls load_dotenv() *after*
    importing this module, so KIMI_MODEL can land in os.environ only after
    DEFAULT_KIMI_MODEL was computed at import time.
    """
    return model or os.environ.get("KIMI_MODEL") or DEFAULT_KIMI_MODEL


def get_kimi_client() -> AsyncOpenAI:
    """Return a singleton AsyncOpenAI client pointed at Kimi/Moonshot."""
    global _kimi_client
    if _kimi_client is None:
        _kimi_client = AsyncOpenAI(
            api_key=_api_key(),
            base_url=os.getenv("KIMI_BASE_URL", DEFAULT_KIMI_BASE_URL),
        )
    return _kimi_client


async def extract_entities(text: str, model: str | None = None) -> dict[str, Any]:
    """Ask Kimi to extract entities and relationships from text.

    Returns structured JSON with nodes and edges for graph insertion.
    """
    if not _api_key():
        log.warning("extract_entities called without KIMI_API_KEY configured")
        return {"nodes": [], "edges": [], "error": MISSING_KEY_ERROR}

    client = get_kimi_client()

    system_prompt = """You are an expert knowledge graph builder.
Given text, extract ALL entities and their relationships.

Return valid JSON with this exact structure:
{
  "nodes": [
    {"id": "unique_id", "label": "EntityName",
     "type": "Person|Org|Concept|Event|Place|Technology",
     "properties": {"summary": "one-line description"}}
  ],
  "edges": [
    {"source": "source_id", "target": "target_id", "relationship": "RELATES_TO",
     "properties": {"context": "brief context"}}
  ]
}

Rules:
- Each node gets a unique string ID
- Use UPPERCASE for relationship types (e.g., FOUNDED_BY, WORKS_AT, USES_TECHNOLOGY)
- Include ALL meaningful entities, even minor ones
- Capture temporal relationships when present
- Be specific, not generic"""

    try:
        response = await client.chat.completions.create(
            model=_resolve_model(model),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Extract knowledge graph from:\n\n{text}"},
            ],
            max_tokens=EXTRACT_MAX_TOKENS,
            timeout=120,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        log.error("Kimi extract_entities request failed: %s", e)
        return {"nodes": [], "edges": [], "error": f"LLM request failed: {type(e).__name__}"}

    choice = response.choices[0]
    if choice.finish_reason == "length":
        # Reasoning + output overran the budget; the JSON is cut mid-stream and
        # would fail to parse anyway — report the real cause instead.
        log.error(
            "Kimi extract_entities hit max_tokens=%s (finish_reason=length)", EXTRACT_MAX_TOKENS
        )
        return {"nodes": [], "edges": [], "error": TRUNCATED_ERROR}

    try:
        return json.loads(choice.message.content or "{}")
    except json.JSONDecodeError as e:
        return {"nodes": [], "edges": [], "error": f"Failed to parse LLM JSON output: {e}"}


async def answer_query(question: str, context: str, model: str | None = None) -> str:
    """Answer a question using graph context (GraphRAG style)."""
    if not _api_key():
        log.warning("answer_query called without KIMI_API_KEY configured")
        return f"{LLM_UNAVAILABLE_PREFIX} {MISSING_KEY_ERROR}."

    client = get_kimi_client()

    try:
        response = await client.chat.completions.create(
            model=_resolve_model(model),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise research assistant. Answer questions using ONLY "
                        "the provided knowledge graph context. Cite specific entities and "
                        "relationships. If the context doesn't contain enough info, say so."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Knowledge Graph Context:\n{context}\n\nQuestion: {question}",
                },
            ],
            max_tokens=ANSWER_MAX_TOKENS,
            timeout=120,
        )
    except Exception as e:
        log.error("Kimi answer_query request failed: %s", e)
        return f"{LLM_UNAVAILABLE_PREFIX} LLM request failed ({type(e).__name__})."

    choice = response.choices[0]
    content = choice.message.content or ""
    if not content:
        # Typically finish_reason == "length" with the whole budget spent on
        # reasoning.  An empty string is not an answer; classify it as
        # unavailable so GraphRAG reports `llm_unavailable`, not `answered`.
        reason = choice.finish_reason
        log.error("Kimi answer_query returned no content (finish_reason=%s)", reason)
        return f"{LLM_UNAVAILABLE_PREFIX} LLM returned no content (finish_reason={reason})."
    return content
