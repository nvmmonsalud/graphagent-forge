# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

GraphAgent Forge is a hackathon project (Daytona HackSprint Tokyo 2026): a FastAPI-based agent that ingests URLs/text, extracts entities with Kimi AI (Moonshot), stores them as a knowledge graph in Neo4j, and answers questions via GraphRAG.

## Commands

```bash
pip install -r requirements.txt      # install deps
cp .env.example .env                 # configure API keys (required before running)

python -m src.main                   # run FastAPI server on :8000 (uvicorn, reload=True)
./run.sh                             # alternative: creates .venv, installs deps, starts server
streamlit run frontend/app.py        # optional Streamlit dashboard (expects API on :8000)

ruff check src/                      # lint
pytest                               # tests (pytest is a dependency; no tests/ dir exists yet)
```

There is no build step. The primary UI is `frontend/index.html`, served by FastAPI at `/`.

## Architecture

Two pipelines share state initialized in `src/main.py`'s lifespan hook (`app.state.neo4j`, `app.state.agent`, `app.state.ws_manager`):

**Ingestion** (`GraphAgent.ingest_url` / `ingest_text` in `src/agent/core.py`):
`src/ingestion/extractor.py` (fetch + clean HTML, with SSRF guard blocking private IPs) → `src/ingestion/graph_writer.py` (orchestrates; prefixes node IDs with an md5 hash of `source_doc` to avoid cross-document collisions) → `src/ingestion/entity_parser.py` (Kimi LLM extracts nodes/edges as JSON) → `src/graph/neo4j_client.py` (writes `:Entity` nodes). After a successful write, the agent: stores a content embedding, broadcasts new nodes/edges over WebSocket `/ws/graph`, and runs integrity verification in a Daytona sandbox.

**Query** (`GraphRAGEngine.query` in `src/graph/graphrag.py`):
vector search (only if a real Nosana embedding was produced) → keyword search fallback → fetch 2-hop neighborhood context for top-5 candidates → Kimi answers using only that graph context (`answer_query` in `entity_parser.py`).

HTTP endpoints live in `src/api/routes.py` (mounted at `/api`); they pull `agent`/`neo4j` off `request.app.state`.

### Graceful-degradation pattern

Every external sponsor service has a local fallback, so the app runs with zero configured keys:

- **Daytona** (`src/agent/daytona_exec.py`): sandboxed code execution → falls back to local subprocess (30s timeout) if SDK/key missing.
- **Nosana** (`src/agent/nosana_client.py`): GPU embeddings via `NOSANA_EMBEDDING_URL` → falls back to a deterministic SHA-256 hash pseudo-embedding. `last_embedding_method` records which path ran; GraphRAG skips vector search when it's the hash fallback (hash embeddings aren't semantic).
- **Kimi** has no fallback — `KIMI_API_KEY` is effectively required for entity extraction and question answering.

When touching these clients, preserve the fallback behavior and the `method` / `last_embedding_method` reporting fields.

### Graph schema

Single node label `:Entity` with properties `id` (unique constraint), `label`, `type`, `summary`, `source_doc`, `embedding` (384-dim vector index, cosine). Relationship types are dynamic UPPERCASE strings produced by the LLM. Schema/indexes are created idempotently on startup by `Neo4jClient.init_schema`.

### WebSocket coupling

`GraphAgent._get_ws_manager` lazily imports `ws_manager` from `src.main` to avoid a circular import — the agent module must not import `src.main` at top level.

## Notes

- The README's project-structure listing is partly aspirational (`prompts.py`, `schema.py`, `tests/`, `static/` don't exist; `nosana_client.py` does).
- Async throughout: Neo4j uses the async driver, all client calls are `await`ed. Keep new code async.
- Default Kimi model is `kimi-k2.7-code-highspeed`, set both in `entity_parser.py` and `graph_writer.py`.
