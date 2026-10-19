# Agent guidance

Applies to the whole repository. Read this before changing code. Consult
`CLAUDE.md` for detailed pipeline and response contracts, `README.md` for user
setup, and `.env.example` for configuration. Check the implementation and tests
when documentation disagrees; keep guidance aligned when behavior changes.

## Project and code map

GraphAgent Forge uses Python 3.11, FastAPI, Neo4j, and Kimi to ingest documents,
build a knowledge graph, and answer questions through GraphRAG. Daytona provides
sandboxed verification/analytics; Nosana provides optional semantic embeddings.

- `src/main.py`: app lifespan, shared resources, WebSocket manager, static UI.
- `src/api/routes.py`: request validation, dependencies, HTTP response contracts,
  ingestion submission, graph tools, analytics, and query-history endpoints.
- `src/agent/core.py`: ingestion/query orchestration and progress reporting.
- `src/agent/jobs.py`: bounded async job queue; `history.py`: bounded synchronous
  query-history store. These remain the default memory adapters. Optional
  `state.py`, `durable_jobs.py`, `durable_history.py` and `commands.py` implement
  SQLite-backed state when STATE_DB_PATH is set; see docs/M3_CONTRACT.md.
- `src/agent/daytona_exec.py`: verification, fan-out, sweep, and structural
  analytics execution; `nosana_client.py`: embeddings and fallback reporting.
- `src/ingestion/`: guarded URL fetching, file validation/extraction, Kimi calls,
  and document-scoped graph writes.
- `src/graph/neo4j_client.py`: schema, Cypher, provenance, merges, graph reads;
  `graphrag.py`: retrieval and grounded answers; `normalize.py`: label matching.
- `frontend/index.html`: primary UI, inline CSS/JavaScript, D3 visualization.
  `frontend/app.py` is a separate optional Streamlit client.
- `frontend/vendor/`: vendored D3 and attribution; preserve local asset loading.
- `tests/`: offline unit, route, frontend-contract, docs, and integration tests.
- `scripts/`, `seed/`, `assets/ui/`: demo seeding, capture tooling, sample data,
  and README images. Seeding writes directly to Neo4j without an LLM.

## Development commands

Run from the repository root. Use the existing `.venv` when available; otherwise
create one with `python3.11 -m venv .venv`.

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m src.main                  # FastAPI on port 8000
.venv/bin/ruff check src/ scripts/ tests/     # make lint; CI checks src/ tests/
.venv/bin/python -m pytest -q                # make test; offline suite
.venv/bin/python -m pytest -q tests/test_jobs.py  # focused example
.venv/bin/python -m pytest -q -m integration  # live Neo4j; explicit opt-in
.venv/bin/python -m scripts.seed_graph --dry-run  # validate without DB writes
```

`requirements-dev.txt` includes runtime requirements. Ruff targets Python 3.11,
100-character lines, and rules E/F/I/UP/B (`pyproject.toml`). There is no frontend
build step or JavaScript package manager. `./run.sh` creates a venv, installs
runtime dependencies, and starts the server. `docker compose up` runs app + Neo4j.
Use `streamlit run frontend/app.py` for the optional dashboard after activating
the venv; its API base is `GRAPHAGENT_API`.

Configure local settings using `.env.example`; never overwrite an existing `.env`
or commit credentials. The app boots without keys or a reachable Neo4j, but graph
operations need Neo4j and live extraction/answers need Kimi. `DEV_RELOAD=1` opts
into reload; memory state is lost, while durable mode recovers bounded jobs/history.

## Implementation contracts

- Keep I/O async. Offload blocking extraction/DNS/subprocess work as the existing
  code does. Load dotenv before app-module imports, since some settings are read
  at import time. Drain jobs before closing the driver and outbound clients.
- Route handlers use `request.app.state`. Keep jobs, history, and executor logic
  independent of HTTP/WebSocket transport; inject event sinks. Do not introduce
  top-level imports of `src.main` into agent modules; the current WebSocket
  accessor imports lazily to avoid a cycle.
- Preserve degraded operation: Neo4j outages yield degraded health and graph
  503s; Daytona can run locally; Nosana can return deterministic hash vectors;
  missing Kimi keys produce structured errors. Hash vectors are not semantic:
  skip vector retrieval and semantic duplicate matching for them. Preserve
  `method`, `last_embedding_method`, and query `status` reporting.
- Distinguish successful execution from a valid graph. Verification failure has
  `ok: false` and no `valid`; failed fan-out/sweep omit `sources`/`results`.
  Analytics structural execution may degrade within an otherwise useful response.
  Measure timings; never fabricate performance or count claims. Sweep sizes run
  sequentially; fan-out within a size is bounded by concurrency limits.
- Nodes use `:Entity`, unique `id`, normalized `norm_label`, and list-valued
  `source_docs`. Relationships use `:RELATES_TO`, scalar `source_doc`, and the
  semantic name in `r.type`; read it with `coalesce(r.type, type(r))`.
- Preserve document-scoped IDs and provenance through ingestion, merge, and
  deletion. Exact duplicate auto-merge is opt-in (`AUTO_MERGE=exact`); otherwise
  duplicate suggestions require an explicit merge action. New sources use stable
  IDs separate from titles. Publication, merge, clear and deletion share a graph
  mutation lock; Source version compare-and-swap prevents conflicting refreshes.
  Retain tombstones against stale work. Legacy provenance strings remain valid
  keys; see docs/M1_UPGRADE.md for explicit migration and rollback.
- M2 original excerpts use `:EvidenceChunk`, linked to current Source versions;
  they are separate from graph summaries. Preserve server-assigned `evidence_ids`,
  atomic replacement/deletion, and exact sent/current citation validation. Bound
  extraction to 12 chunks with concurrency two and final answers to three chunks.
  See docs/M2_CONTRACT.md and docs/M2_UPGRADE.md for offsets, coverage and privacy.
  Never include raw chunk text in job/WebSocket/public graph projections.
- Every returned edge must reference nodes included in the same graph response,
  including capped reads and per-source boundary nodes. Preserve graph metadata
  (`truncated`, totals, `limit`) and the distinction between incremental graph
  updates, merge updates, and job/verification telemetry on `/ws/graph`.
- Preserve bounded queue/history behavior and public schemas. History recording
  must not break an answer. Durable adapters preserve the public schemas, fence
  lease writes, and remove private commands on terminal transitions. Receipt and
  source publication must stay in the same Neo4j transaction; replay receipts
  survive refresh/delete/clear. Do not claim multi-API-worker or monetary-budget
  support; see docs/M3_UPGRADE.md for deployment and remaining M3 scope.
- Keep Kimi model selection in `_resolve_model`, preserve reasoning-token budgets
  and structured truncation errors, and do not send a `temperature` parameter.

## Private workspace contracts

- AUTH_MODE=private must fail closed without valid identity configuration and
  durable state. Local/unscoped resources must never be selected in private mode.
- Route resources come from request.state.resources after membership/role checks.
  Preserve all-label/type/index namespace enforcement and bounded traversal.
- Inject workspace broadcast managers; authenticate/recheck every private socket
  send. Recheck queued command submitter before work/chunk launch/publication.
- Keep opaque cookies, CSRF/Origin checks, credential rotation and browser cache
  fencing. Never put private credentials/history in localStorage or token URLs.
- Read docs/M4_CONTRACT.md and docs/M4_UPGRADE.md before auth/scoping changes.

## Research workflow contracts

- New fulltext indexes/procedure literals must be included in the workspace
  schema allowlist. Source-specific questions filter before retrieval/provider
  input; rankings are diagnostics, never confidence or entailment scores.
- Research services are per-resource, initialized before HTTP work and stopped
  before jobs/drivers. Private startup reopens enabled workspaces for schedules.
- Keep schedule URLs private, revalidate SSRF every fetch, fence claims/completion,
  and preserve server-added actor identity. Bulk acceptance may be partial.
- Investigation updates use revision CAS and server-validated evidence metadata;
  stale notes keep original provenance. Browse cursors cannot grant access.
- See docs/M5_CONTRACT.md and docs/M5_UPGRADE.md for bounds and page-local edges.

## Security and frontend changes

- Preserve URL SSRF defenses: HTTP(S) only, all DNS records validated, redirects
  revalidated, bounded streaming, and content-type checks.
- Keep upload size/filename/type/magic-byte checks. Optional `pypdf` and multipart
  imports must degrade safely; without multipart, omit the file route entirely.
- Reuse route dependency groups for graph availability, optional API-key auth,
  and per-IP rate limiting. Ask and sandbox routes also require the configured
  API key; sandbox routes use a separate rate-limit bucket. Return sanitized
  client errors and log exception details server-side.
- Treat scraped, uploaded, LLM, and graph text as untrusted. Escape dynamic values
  through the UI's `esc()` helper before HTML interpolation. Preserve graph
  rendering, merge recovery, and WebSocket message dispatch when changing panels.
- Keep vendored assets and their attribution intact. UI contract tests inspect
  HTML/JavaScript text; supplement them with a browser check for visual or
  interaction changes when a browser is available.

## Verification and collaboration

Choose existing focused tests for the changed subsystem, then run lint and the
offline suite for code changes. Add regression coverage for meaningful behavior
changes. Default pytest configuration excludes `integration`; those tests require
`NEO4J_URI`, `NEO4J_USER`, and `NEO4J_PASSWORD` and may skip without them. Offline
tests mock external services; preserve environment isolation in `tests/conftest.py`.
Run integration tests only against a disposable database: the suite includes a
test that clears the entire graph.
Do not call paid APIs, spawn remote sandboxes, or mutate demo databases just to
validate a local change. `make demo`, `seed-reset`, and `seed-clear` mutate data;
prefer `seed-dry` when checking seed fixtures. Report skipped/unavailable checks.

For substantial work, delegate independent reviews or implementation areas when
the task permits it: graph/schema and provenance; API/jobs/service clients;
frontend and WebSocket behavior; tests and docs. Give each agent a clear scope
and file ownership, avoid concurrent edits to shared files, and have one agent
integrate and validate the result. Small changes need no additional agents.
Keep changes focused and preserve unrelated working-tree edits.
