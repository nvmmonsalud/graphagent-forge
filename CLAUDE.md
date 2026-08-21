# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

GraphAgent Forge is a hackathon project (Daytona HackSprint Tokyo 2026): a FastAPI-based agent that ingests URLs/text, extracts entities with Kimi AI (Moonshot), stores them as a knowledge graph in Neo4j, and answers questions via GraphRAG.

## Commands

```bash
pip install -r requirements.txt      # runtime deps
pip install -r requirements-dev.txt  # + pytest, pytest-asyncio, ruff, streamlit
cp .env.example .env                 # configure API keys (app boots with none, degraded)

python -m src.main                   # run FastAPI server on :8000 (uvicorn, reload=True)
./run.sh                             # alternative: creates .venv, installs deps, starts server
docker compose up                    # app + Neo4j 5.26
streamlit run frontend/app.py        # optional Streamlit dashboard (API base via GRAPHAGENT_API)

ruff check src/ tests/               # lint (config in pyproject.toml: E,F,I,UP,B, line 100)
pytest                               # offline suite — no keys/Neo4j needed (integration deselected)
pytest -m integration                # extra tests against a live Neo4j (reads NEO4J_* env, else skips)
```

There is no build step. The primary UI is `frontend/index.html`, served by FastAPI at `/`. CI (`.github/workflows/ci.yml`) runs ruff + the offline pytest suite.

## Architecture

Two pipelines share state initialized in `src/main.py`'s lifespan hook (`app.state.neo4j`, `app.state.agent`, `app.state.ws_manager`, `app.state.neo4j_available`). Startup tolerates an unreachable Neo4j: the server boots degraded, `/api/health` reports `{"status": "degraded", "neo4j": false}`, and graph routes return 503.

**Ingestion** (`GraphAgent.ingest_url` / `ingest_text` in `src/agent/core.py`):
`src/ingestion/extractor.py` (fetch + clean HTML; SSRF guard: http/https only, all DNS records checked, manual redirect re-validation, 3MB streamed cap, content-type gate) → `src/ingestion/graph_writer.py` (orchestrates; prefixes node IDs with an md5 hash of `source_doc` to avoid cross-document collisions; drops edges referencing unknown IDs, reports `dropped_edges`) → `src/ingestion/entity_parser.py` (Kimi LLM extracts nodes/edges as JSON; structured error when `KIMI_API_KEY` is missing) → `src/graph/neo4j_client.py` (writes `:Entity` nodes and `:RELATES_TO` edges in one transaction). After a successful write, `_post_ingest` (shared tail): stores per-entity embeddings (`label: summary` per node via `set_node_embeddings`), broadcasts that source's nodes/edges over WebSocket `/ws/graph`, and runs integrity verification (scoped to the ingested source) in a Daytona sandbox.

**Query** (`GraphRAGEngine.query` in `src/graph/graphrag.py`):
vector search via `db.index.vector.queryNodes` (only if a real Nosana embedding was produced) → keyword search (terms batched with `asyncio.gather`) → fetch 2-hop neighborhood context for top-5 candidates (also gathered) → Kimi answers using only that graph context (`answer_query` in `entity_parser.py`).

HTTP endpoints live in `src/api/routes.py` (mounted at `/api`); they pull `agent`/`neo4j` off `request.app.state`. Beyond the core routes there are: `GET /sources`, `DELETE /sources?source_doc=`, `POST /graph/clear`, `GET /graph/data?source_doc=`, `GET /graph/export?format=json|csv`. Security middleware/deps: optional `X-API-Key` auth on mutating routes (only when env `API_KEY` is set), in-memory per-IP rate limiting (10/min, shared across `/ingest/*` and `/ask`, per-process), CORS origins from `ALLOWED_ORIGINS`, 500s log server-side (`log.exception`) and return only `"internal error"` to clients.

### Graceful-degradation pattern

Every external sponsor service has a local fallback, so the app runs with zero configured keys:

- **Daytona** (`src/agent/daytona_exec.py`): sandboxed code execution → falls back to local subprocess (30s timeout) if SDK/key missing. The verify path pipes the graph JSON via stdin locally (argv is capped at ARG_MAX; do not embed large payloads in `-c` scripts).
- **Nosana** (`src/agent/nosana_client.py`): GPU embeddings via `NOSANA_EMBEDDING_URL` → falls back to a deterministic uint32-derived hash pseudo-embedding (384-dim, unit-norm, always finite; non-finite remote vectors are rejected and fall back too). `last_embedding_method` records which path ran; GraphRAG skips vector search when it's the hash fallback (hash embeddings aren't semantic). One `NosanaClient` instance is shared between `GraphAgent` and `GraphRAGEngine` — keep it that way.
- **Kimi** has no fallback — a missing `KIMI_API_KEY` returns structured errors (`{"error": "KIMI_API_KEY not configured"}`), never an SDK exception. The model comes from `KIMI_MODEL` env (resolved in `entity_parser._resolve_model`; don't hardcode it elsewhere).

When touching these clients, preserve the fallback behavior and the `method` / `last_embedding_method` reporting fields.

### Graph schema

Single node label `:Entity` with properties `id` (unique constraint), `label`, `type`, `summary`, `source_doc`, `embedding` (384-dim vector index, cosine). All relationships are stored with the fixed label `:RELATES_TO`; the LLM's semantic relationship name lives in the `r.type` property, and every read path returns `coalesce(r.type, type(r))` — new queries must do the same or the UI/LLM will see the literal string `RELATES_TO`. Schema/indexes are created idempotently on startup by `Neo4jClient.init_schema`.

### WebSocket coupling

`GraphAgent._get_ws_manager` lazily imports `ws_manager` from `src.main` to avoid a circular import — the agent module must not import `src.main` at top level. Broadcast payloads are `{"type": "graph_update", "nodes": [...], "edges": [...]}`; the frontend merges them incrementally and only refetches when the socket is closed.

## Notes

- Async throughout: Neo4j uses the async driver, all client calls are `await`ed. Blocking work (DNS, HTML parsing) goes through `asyncio.to_thread` / the loop's `getaddrinfo`. Keep new code async.
- `load_dotenv()` runs in `src/main.py` *before* the app-module imports — keep it there so import-time env reads see `.env`.
- The frontend escapes all dynamic content through its `esc()` helper before any `innerHTML` write — route new dynamic values through it too (scraped/LLM content is untrusted).
- Tests are offline by default (mocked LLM/Neo4j); Neo4j-dependent tests are marked `integration` and skip without `NEO4J_URI`/`NEO4J_USER`/`NEO4J_PASSWORD`.
- Known limitation (deliberate): the md5 doc-prefix means the same real-world entity appearing in two documents becomes two nodes — entity resolution/merge is future work.
