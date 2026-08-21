"""Kimi AI (Moonshot) client — OpenAI-compatible wrapper."""
from __future__ import annotations

import os
from typing import Any

from openai import AsyncOpenAI

_kimi_client: AsyncOpenAI | None = None


def get_kimi_client() -> AsyncOpenAI:
    """Return a singleton AsyncOpenAI client pointed at Kimi/Moonshot."""
    global _kimi_client
    if _kimi_client is None:
        _kimi_client = AsyncOpenAI(
            api_key=os.getenv("KIMI_API_KEY", "«redacted:sk-…»"),
            base_url=os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1"),
        )
    return _kimi_client


async def extract_entities(text: str, model: str = "kimi-k2.7-code-highspeed") -> dict[str, Any]:
    """Ask Kimi to extract entities and relationships from text.

    Returns structured JSON with nodes and edges for graph insertion.
    """
    client = get_kimi_client()

    system_prompt = """You are an expert knowledge graph builder.
Given text, extract ALL entities and their relationships.

Return valid JSON with this exact structure:
{
  "nodes": [
    {"id": "unique_id", "label": "EntityName", "type": "Person|Org|Concept|Event|Place|Technology", "properties": {"summary": "one-line description"}}
  ],
  "edges": [
    {"source": "source_id", "target": "target_id", "relationship": "RELATES_TO", "properties": {"context": "brief context"}}
  ]
}

Rules:
- Each node gets a unique string ID
- Use UPPERCASE for relationship types (e.g., FOUNDED_BY, WORKS_AT, USES_TECHNOLOGY)
- Include ALL meaningful entities, even minor ones
- Capture temporal relationships when present
- Be specific, not generic"""

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Extract knowledge graph from:\n\n{text}"},
        ],
        temperature=1,
        response_format={"type": "json_object"},
    )

    import json

    try:
        return json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError as e:
        return {"nodes": [], "edges": [], "error": f"Failed to parse LLM JSON output: {e}"}


async def answer_query(question: str, context: str, model: str = "kimi-k2.7-code-highspeed") -> str:
    """Answer a question using graph context (GraphRAG style)."""
    client = get_kimi_client()

    response = await client.chat.completions.create(
        model=model,
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
        temperature=1,
    )

    return response.choices[0].message.content or "No response generated."
