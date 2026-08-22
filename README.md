# 🧠 GraphAgent Forge

> Turn scattered information into connected intelligence.

GraphAgent Forge is an autonomous AI agent that ingests unstructured data,
builds knowledge graphs, and reasons over them using GraphRAG. Built for the
Daytona HackSprint Tokyo (Sept 12, 2026).

## 🏗️ Architecture

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   User URL   │───▶│  Ingestion   │───▶│  Kimi (LLM)  │
│  or Document │    │  Pipeline    │    │  (Extract)   │
└──────────────┘    └──────────────┘    └──────┬───────┘
                                               │
                                               ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   Web UI     │◀──▶│  GraphRAG    │◀──▶│   Neo4j      │
│ (D3/FastAPI) │    │  Query Eng.  │    │  (Graph DB)  │
└──────────────┘    └──────────────┘    └──────────────┘
                           │
                           ▼
                    ┌──────────────┐    ┌──────────────┐
                    │   Daytona    │    │    Nosana    │
                    │  (Sandboxes) │    │  (GPU Jobs)  │
                    └──────────────┘    └──────────────┘
```

## 🔧 Sponsor Integration

| Sponsor | Role | Integration |
|---------|------|-------------|
| **Kimi AI** | Reasoning & extraction LLM | Kimi (`kimi-k2.7-code-highspeed` by default; override with `KIMI_MODEL`) — OpenAI-compatible API; inputs are clipped to 50,000 characters (`MAX_CHARS`) before extraction |
| **Neo4j** | Knowledge graph storage & GraphRAG | Cypher queries, vector search |
| **Daytona** | Isolated agent execution sandboxes | Isolated sandbox execution of the graph-integrity check — a Daytona sandbox when `DAYTONA_API_KEY` is set, a local subprocess otherwise. The verify response reports which path ran (`method`) and the measured wall time (`duration_ms`, plus `boot_ms` for sandbox creation) |
| **Nosana** | Decentralized GPU compute | GPU embeddings via a configurable `NOSANA_EMBEDDING_URL`, with a deterministic 384-dim hash fallback so the app runs keyless. Semantic (tier-2) duplicate detection turns itself on only when real embeddings exist |

## 🚀 Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env with your API keys (the app boots with none configured,
# in a degraded mode — KIMI_API_KEY is needed for extraction/answers)

# 3. Run the app
python -m src.main
# alternative: ./run.sh  (creates .venv, installs deps, starts the server)

# 4. Open the web UI
open http://localhost:8000

# Optional: seed a demo graph (3 sample documents) instead of ingesting live
python -m scripts.seed_graph
```

Or with Docker (bundles Neo4j):

```bash
docker compose up
```

Optional Streamlit dashboard (a secondary view, separate from the web UI above):

```bash
streamlit run frontend/app.py    # API base via GRAPHAGENT_API env var
```

Development:

```bash
pip install -r requirements-dev.txt
ruff check src/ tests/     # lint (configured in pyproject.toml)
pytest                     # offline test suite — no keys or Neo4j needed
pytest -m integration      # extra tests against a live Neo4j (reads NEO4J_* env)
```

## 📁 Project Structure

```
graphagent-forge/
├── src/
│   ├── main.py              # FastAPI app entry point, WebSocket manager, lifespan
│   ├── agent/
│   │   ├── core.py          # Main agent loop (ingest/ask orchestration, progress stages)
│   │   ├── jobs.py          # Async ingest job queue (JobManager, submit/wait/list)
│   │   ├── daytona_exec.py  # Daytona sandbox execution (local subprocess fallback)
│   │   └── nosana_client.py # Nosana GPU embeddings (hash fallback)
│   ├── graph/
│   │   ├── neo4j_client.py  # Neo4j connection, schema & queries
│   │   ├── graphrag.py      # GraphRAG query engine
│   │   └── normalize.py     # normalize_label() for norm_label / dedup matching
│   ├── ingestion/
│   │   ├── extractor.py     # URL/document content extraction (SSRF-guarded)
│   │   ├── file_extractor.py# Uploaded-file extraction (PDF/TXT/MD, magic-byte checks)
│   │   ├── entity_parser.py # Kimi-powered entity extraction & answering
│   │   └── graph_writer.py  # Write entities to Neo4j
│   └── api/
│       └── routes.py        # FastAPI endpoints (/api/*)
├── frontend/
│   ├── index.html           # Primary web UI (D3 graph, served at /)
│   └── app.py               # Optional Streamlit dashboard
├── scripts/
│   └── seed_graph.py        # Loads seed/graph.json into Neo4j for demos
├── seed/
│   └── graph.json           # Sample multi-document graph fixture
├── tests/                   # Offline unit/route tests + Neo4j integration tests
├── .github/                 # CI workflow + PR template
├── .env.example             # Environment template
├── .dockerignore            # Docker build context excludes
├── requirements.txt         # Runtime dependencies
├── requirements-dev.txt     # Dev/test dependencies
├── pyproject.toml           # ruff + pytest configuration
├── Dockerfile               # App image
├── docker-compose.yml       # App + Neo4j
├── run.sh                   # Convenience script: venv + deps + start server
├── CLAUDE.md                # Guidance for Claude Code when working in this repo
├── DEMO_PITCH.md            # 2-minute demo script
├── LICENSE                  # MIT
└── README.md                # This file
```

## 🔌 API Highlights

All endpoints under `/api`.

**Ingest is async — a job queue, not a synchronous call.** `POST /ingest/url`,
`POST /ingest/text`, and `POST /ingest/file` (multipart upload; PDF/TXT/MD, 3MB
cap) all return `202` immediately with `{"job_id", "status"}`; two background
workers pull from an in-memory queue and run the pipeline, reporting stage
transitions (`fetching`/`parsing` → `extracting` → `embedding` → `merging`
→ `broadcasting` → `verifying`) live over `WS /ws/graph` as
`{"type": "job_update", "job": {...}}`. Poll `GET /api/jobs/{id}` or list
recent jobs with `GET /api/jobs?limit=`, or pass `?wait=true` on any ingest
route to block and get the final result synchronously instead (still subject
to `INGEST_JOB_TIMEOUT`, default 600s, which caps how long a single job may
run before it's failed with `"timed out"`).

Query: `POST /ask`, `POST /graph/search`, `POST /graph/path`. Graph data:
`GET /graph/data[?source_doc=][&limit=]` (node cap 1–5000, default 500, on
the all-sources view only — a per-source read is never truncated), `GET
/graph/stats`, `GET /graph/verify`, `GET /graph/export?format=json|csv`.
Source management: `GET /sources`, `DELETE /sources?source_doc=`, `POST
/graph/clear`. Duplicate entities across sources are surfaced by `GET
/graph/duplicates` and merged via `POST /graph/merge` (suggest-only; set
`AUTO_MERGE=exact` to auto-merge exact matches at ingest). Live updates
stream over `WS /ws/graph`. Optional hardening via env: `API_KEY`
(X-API-Key auth on mutating routes), `ALLOWED_ORIGINS` (CORS). Ingest and
ask share one rate-limit bucket per IP, `RATE_LIMIT_MAX` requests (default
10) per 60s.

## 🎯 Judging Criteria Alignment

| Criterion | How We Win |
|-----------|-----------|
| **Completeness** | Full pipeline: ingest → extract → graph → query → web UI |
| **Innovation** | GraphRAG + autonomous agent = novel reasoning over connected data |
| **Real problem** | "Make sense of scattered info" — universal pain point |
| **Sponsor usage** | All 4 sponsors integrated naturally |

## 📝 License

MIT — built with ❤️ for Daytona HackSprint Tokyo 2026
