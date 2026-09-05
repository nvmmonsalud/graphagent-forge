# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

GraphAgent Forge is a hackathon project (Daytona HackSprint Tokyo 2026): a FastAPI-based agent that ingests URLs/text, extracts entities with Kimi AI (Moonshot), stores them as a knowledge graph in Neo4j, and answers questions via GraphRAG.

## Commands

```bash
pip install -r requirements.txt      # runtime deps
pip install -r requirements-dev.txt  # + pytest, pytest-asyncio, ruff, streamlit
cp .env.example .env                 # configure API keys (app boots with none, degraded)

python -m src.main                   # run FastAPI server on :8000 (uvicorn; reload opt-in, see DEV_RELOAD)
./run.sh                             # alternative: creates .venv, installs deps, starts server
docker compose up                    # app + Neo4j 5.26
streamlit run frontend/app.py        # optional Streamlit dashboard (API base via GRAPHAGENT_API)
python -m scripts.seed_graph         # loads seed/graph.json (a few sample documents) for demos

ruff check src/ tests/               # lint (config in pyproject.toml: E,F,I,UP,B, line 100)
pytest                               # offline suite — no keys/Neo4j needed (integration deselected)
pytest -m integration                # extra tests against a live Neo4j (reads NEO4J_* env, else skips)
```

There is no build step. The primary UI is `frontend/index.html`, served by FastAPI at `/`. CI (`.github/workflows/ci.yml`) runs ruff + the offline pytest suite.

## Architecture

Two pipelines share state initialized in `src/main.py`'s lifespan hook (`app.state.neo4j`, `app.state.agent`, `app.state.ws_manager`, `app.state.neo4j_available`). Startup tolerates an unreachable Neo4j: the server boots degraded, `/api/health` reports `{"status": "degraded", "neo4j": false}`, and graph routes return 503.

**Ingestion** (`GraphAgent.ingest_url` / `ingest_text` / `ingest_file` in `src/agent/core.py`):
`src/ingestion/extractor.py` (fetch + clean HTML; SSRF guard: http/https only, all DNS records checked, manual redirect re-validation, 3MB streamed cap, content-type gate) — or, for uploads, `src/ingestion/file_extractor.py` (PDF/TXT/MD from raw bytes; see the file-upload subsection below) — → `src/ingestion/graph_writer.py` (orchestrates; prefixes node IDs with an md5 hash of `source_doc` to avoid cross-document collisions; drops edges referencing unknown IDs, reports `dropped_edges`) → `src/ingestion/entity_parser.py` (Kimi LLM extracts nodes/edges as JSON; structured error when `KIMI_API_KEY` is missing) → `src/graph/neo4j_client.py` (writes `:Entity` nodes and `:RELATES_TO` edges in one transaction). After a successful write, `_post_ingest` (shared tail): stores per-entity embeddings (`label: summary` per node via `set_node_embeddings`), optionally auto-merges exact-label duplicates (`AUTO_MERGE=exact`), broadcasts that source's nodes/edges over WebSocket `/ws/graph`, and runs integrity verification (scoped to the ingested source) in a Daytona sandbox. Progress stages reported to the job queue, in order: `fetching` (URL only) / `parsing` (file only) → `extracting` → `embedding` → `merging` (only when `AUTO_MERGE=exact`) → `broadcasting` → `verifying`.

**Query** (`GraphRAGEngine.query` in `src/graph/graphrag.py`):
vector search via `db.index.vector.queryNodes` (only if a real Nosana embedding was produced) → keyword search (terms batched with `asyncio.gather`) → fetch 2-hop neighborhood context for top-5 candidates (also gathered) → Kimi answers using only that graph context (`answer_query` in `entity_parser.py`). The response carries a `status` field: `"no_context"` (nothing matched, no LLM call made), `"llm_unavailable"` (matched, but `answer_query`'s return started with the `LLM_UNAVAILABLE_PREFIX` fixed string — missing key or a failed request), or `"answered"`. Classify on `status`, not on parsing the answer text.

HTTP endpoints live in `src/api/routes.py` (mounted at `/api`); they pull `agent`/`neo4j` off `request.app.state`. Beyond the core routes there are: `POST /ingest/file` (multipart upload; only registered when `HAS_MULTIPART` is true — see file-upload subsection below), `GET /jobs?limit=`, `GET /jobs/{id}`, `GET /sources`, `DELETE /sources?source_doc=`, `POST /graph/clear`, `GET /graph/data?source_doc=&limit=`, `GET /graph/export?format=json|csv`, `GET /graph/duplicates?limit=`, `POST /graph/merge`. Security middleware/deps: optional `X-API-Key` auth on mutating routes (only when env `API_KEY` is set), in-memory per-IP rate limiting (`RATE_LIMIT_MAX`, default 10/min, read once at import time — not per-request — shared across `/ingest/*` and `/ask`, per-process), CORS origins from `ALLOWED_ORIGINS`, 500s log server-side (`log.exception`) and return only `"internal error"` to clients. `GET /graph/data` without `source_doc` accepts `?limit=` (1–5000, default 500) capping the all-sources node set; `get_all_graph_data` returns `{"nodes", "edges", "truncated", "total_nodes", "total_edges", "limit"}` — `truncated` is true whenever the real counts exceed what was returned. The **boundary-node invariant still holds under the cap**: the edge query is constrained to the node ids actually returned (`WHERE a.id IN $ids AND b.id IN $ids`), so a capped read never emits an edge to a node the client wasn't given — edges are truncated independently and more generously, since an orphaned node (no edge) renders fine in D3 but a dangling edge endpoint does not. `get_graph_data_by_source` returns the same shape with `truncated: false` (per-source reads are never capped). New per-source or all-graph read queries must preserve both the shape and the invariant.

### Ingest job queue

Ingests run through a submit-then-track queue (`src/agent/jobs.py`, `JobManager` on `app.state.jobs`, created and `start()`ed in the lifespan hook right after `ws_manager`). A route calls `jobs.submit(kind, params, run)` and returns the queued job record immediately; 2 workers pull from an `asyncio.Queue` and execute the injected async `run(progress_cb)`, which reports stages back. Every transition (queued → running → each stage → terminal) is broadcast over `/ws/graph` as `{"type": "job_update", "job": {...}}`; clients can also poll `GET /api/jobs/{id}` or pass `?wait=true` on submit as a synchronous escape hatch. `INGEST_JOB_TIMEOUT` (default 600s) caps a single job.

Status semantics matter: **`failed` means the job crashed — unhandled exception, timeout, or shutdown, nothing else.** A business failure (bad URL, missing `KIMI_API_KEY`, 0 entities) is a *`succeeded`* job whose `result.success` is false. `error` is only ever one of three literals — `"internal error"`, `"timed out"`, `"server shutdown"`; raw exception text is logged via `log.exception` and must never reach the record. `jobs.py` stays generic: it imports neither `src.main` nor `src.agent.core` (the broadcast sink is injected), and treats `params`/`result` as opaque dicts.

The registry is in-memory and per-process — jobs vanish on restart. Uvicorn's `reload=True` restarts the process on every file save, which kills any in-flight job the same way, so `reload` is **opt-in**: `python -m src.main` only passes `reload=True` when `DEV_RELOAD=1` is set (default off — leave it unset for demos/rehearsals, set it while iterating on code). `shutdown()` runs first in the lifespan `finally` (before the Neo4j driver and shared httpx clients close) so in-flight jobs can drain; whatever is still queued/running is failed with `"server shutdown"` and its waiters unblocked. Finished jobs beyond `history_limit` (100) are evicted oldest-first; `submit` raises `JobQueueFull` once queued+running hits `max_active` (20).

### Graceful-degradation pattern

Every external sponsor service has a local fallback, so the app runs with zero configured keys:

- **Daytona** (`src/agent/daytona_exec.py`): sandboxed code execution → falls back to local subprocess (30s timeout) if SDK/key missing. The verify path pipes the graph JSON via stdin locally (argv is capped at ARG_MAX; do not embed large payloads in `-c` scripts). `verify_graph` returns exactly one of two shapes: **ran** — `{"ok": true, "valid": bool, ..., "method": "daytona"|"local", "duration_ms": int}` (plus `boot_ms` and `sandbox_id` on the Daytona path); **failed** — `{"ok": false, "error": <literal>, "method": ..., "duration_ms": int}` with no `valid` key at all, so a failed-to-run result can never be mistaken for a real integrity verdict. `error` is one of two client-facing literals (`VERIFY_ERROR_UNAVAILABLE` = `"verification unavailable"`, `VERIFY_ERROR_TIMEOUT` = `"verification timed out"`); the real exception goes to `log.exception`/`log.error`, never to the client. `duration_ms` and `boot_ms` are measured with `time.perf_counter()` at call time — never hardcode or assume a boot time.
- **Nosana** (`src/agent/nosana_client.py`): GPU embeddings via `NOSANA_EMBEDDING_URL` → falls back to a deterministic uint32-derived hash pseudo-embedding (384-dim, unit-norm, always finite; non-finite remote vectors are rejected and fall back too). `last_embedding_method` records which path ran; GraphRAG skips vector search when it's the hash fallback (hash embeddings aren't semantic). One `NosanaClient` instance is shared between `GraphAgent` and `GraphRAGEngine` — keep it that way.
- **Kimi** has no fallback — a missing `KIMI_API_KEY` returns structured errors (`{"error": "KIMI_API_KEY not configured"}`), never an SDK exception. The model comes from `KIMI_MODEL` env (resolved in `entity_parser._resolve_model`; don't hardcode it elsewhere). Moonshot's current Kimi models are reasoning models with two live-API constraints (verified 2026-09): `temperature` must be 1 (anything else is a 400), so **never pass `temperature`**; and thinking cannot be disabled, with reasoning tokens counting against `max_tokens` — budgets live in `EXTRACT_MAX_TOKENS` (16384, env `KIMI_EXTRACT_MAX_TOKENS`) and `ANSWER_MAX_TOKENS` (4096, env `KIMI_ANSWER_MAX_TOKENS`), and a `finish_reason == "length"` extraction returns the structured `TRUNCATED_ERROR` rather than a JSON parse error. Expect ~1 minute per ingest of a few thousand characters.
- **File upload** (`src/ingestion/file_extractor.py`, `POST /api/ingest/file`): PDF/TXT/MD uploads, capped at `MAX_UPLOAD_BYTES` (3MB) and read in bounded chunks so the handler never materializes more than that regardless of what Starlette already buffered. Two independent optional-import degradations, both import-time probes that must never raise: `pypdf` missing → `HAS_PYPDF = False`, PDF uploads return `ERR_NO_PYPDF` while TXT/MD keep working; `python-multipart` missing → `HAS_MULTIPART = False` in `src/api/routes.py`, and the whole `POST /ingest/file` route is conditionally **not registered at all** (`if HAS_MULTIPART: @router.post(...)`) rather than registered and failing per-request. Before trusting file content, `check_upload_shape`/`safe_filename` enforce: filename has no path separators/control chars and is under `MAX_FILENAME_LEN`; extension is one of `SUPPORTED_EXTENSIONS` (`.pdf`/`.txt`/`.md`); and **magic bytes win over the declared/extension type** — a leading `%PDF-`/`PK\x03\x04`/`MZ`/`\x7fELF` signature (or other binary sniff) on a file claiming to be `.txt`/`.md` is rejected as a mismatch even though the extension looked fine. A declared `Content-Type` that conflicts with the extension is also rejected, except for the meaningless values browsers send constantly (`""`, `application/octet-stream`, `binary/octet-stream`, and `None`), which are never treated as a conflict.

When touching these clients, preserve the fallback behavior and the `method` / `last_embedding_method` reporting fields.

### Graph schema

Single node label `:Entity` with properties `id` (unique constraint), `label`, `type`, `summary`, `norm_label` (indexed; from `src/graph/normalize.py:normalize_label` — compute it for any new node write), `source_docs` (LIST of provenance strings — membership tests use `$source_doc IN coalesce(n.source_docs, [])`; edges keep a scalar `r.source_doc` because an edge is a per-document assertion), `aliases` (list, set by merges), `embedding` + `embedding_method` (384-dim vector index, cosine; only `embedding_method = 'nosana'` vectors are semantic). All relationships are stored with the fixed label `:RELATES_TO`; the LLM's semantic relationship name lives in the `r.type` property, and every read path returns `coalesce(r.type, type(r))` — new queries must do the same or the UI/LLM will see the literal string `RELATES_TO`. Schema/indexes are created idempotently on startup by `Neo4jClient.init_schema`, which also runs the idempotent scalar-`source_doc` → `source_docs` migration and `norm_label` backfill (rollback recipe: `MATCH (n:Entity) WHERE n.source_docs IS NOT NULL SET n.source_doc = head(n.source_docs) REMOVE n.source_docs`).

### Entity dedup/merge

Suggest-only by design: `GET /api/graph/duplicates` returns tier-1 (exact `norm_label` match across differing source sets) and tier-2 (embedding cosine ≥0.9, only over `embedding_method='nosana'` nodes; disabled with a `tier2_reason` under the hash fallback) candidate groups; `POST /api/graph/merge` merges explicitly listed node ids via `Neo4jClient.merge_nodes` (plain-Cypher transactional: repoint edges both directions preserving `r.source_doc`/`context`, union `source_docs`, absorb labels into `aliases`, longest summary wins, canonical = longest label → newest → smallest id). `AUTO_MERGE=exact` env opts into automatic tier-1 merging at ingest (off by default). `delete_source` removes membership + that doc's edges and deletes a node only when its membership empties. `get_graph_data_by_source` includes boundary nodes so every returned edge endpoint is present — the frontend's D3 forceLink hard-crashes otherwise; preserve that invariant in new per-source queries.

### WebSocket coupling

`GraphAgent._get_ws_manager` lazily imports `ws_manager` from `src.main` to avoid a circular import — the agent module must not import `src.main` at top level. Broadcast payloads: `{"type": "graph_update", "nodes": [...], "edges": [...]}` (append-only incremental merge) and `{"type": "graph_merge", "removed_ids": [...], "canonical": {...}, "edges": [...]}` (frontend removes merged nodes in place, falling back to a full redraw on any error — never leave the graph blank). Unknown message types are ignored; the frontend only refetches when the socket is closed.

## Notes

- Async throughout: Neo4j uses the async driver, all client calls are `await`ed. Blocking work (DNS, HTML parsing) goes through `asyncio.to_thread` / the loop's `getaddrinfo`. Keep new code async.
- `load_dotenv()` runs in `src/main.py` *before* the app-module imports — keep it there so import-time env reads see `.env`.
- The frontend escapes all dynamic content through its `esc()` helper before any `innerHTML` write — route new dynamic values through it too (scraped/LLM content is untrusted).
- Tests are offline by default (mocked LLM/Neo4j); Neo4j-dependent tests are marked `integration` and skip without `NEO4J_URI`/`NEO4J_USER`/`NEO4J_PASSWORD`.
- The md5 doc-prefix still namespaces node ids per document; cross-document duplicates are handled by the suggest-only merge feature (see Entity dedup/merge above).
- Known issue: `source_doc` identity for URL ingests is the HTML `<title>` (URL fallback) — two different pages with the same title share an identity and md5 prefix. Unchanged for now.
