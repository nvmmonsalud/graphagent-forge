# 🧠 GraphAgent Forge

> Turn scattered information into connected intelligence.

GraphAgent Forge is an autonomous AI agent that ingests unstructured data,
builds knowledge graphs, and reasons over them using GraphRAG. Built for the
Daytona HackSprint Tokyo (Sept 12, 2026).

## 🏗️ Architecture

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   User URL   │───▶│  Ingestion   │───▶│  Kimi AI K3  │
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
| **Kimi AI** | Reasoning & extraction LLM | OpenAI-compatible API, 1M context |
| **Neo4j** | Knowledge graph storage & GraphRAG | Cypher queries, vector search |
| **Daytona** | Isolated agent execution sandboxes | Python SDK, sub-second boot |
| **Nosana** | Decentralized GPU compute | Embedding/scoring workloads |

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

# 4. Open the web UI
open http://localhost:8000
```

Or with Docker (bundles Neo4j):

```bash
docker compose up
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
│   ├── main.py              # FastAPI app entry point, WebSocket manager
│   ├── agent/
│   │   ├── core.py          # Main agent loop
│   │   ├── daytona_exec.py  # Daytona sandbox execution (local fallback)
│   │   └── nosana_client.py # Nosana GPU embeddings (hash fallback)
│   ├── graph/
│   │   ├── neo4j_client.py  # Neo4j connection, schema & queries
│   │   └── graphrag.py      # GraphRAG query engine
│   ├── ingestion/
│   │   ├── extractor.py     # URL/document content extraction (SSRF-guarded)
│   │   ├── entity_parser.py # Kimi-powered entity extraction & answering
│   │   └── graph_writer.py  # Write entities to Neo4j
│   └── api/
│       └── routes.py        # FastAPI endpoints (/api/*)
├── frontend/
│   ├── index.html           # Primary web UI (D3 graph, served at /)
│   └── app.py               # Optional Streamlit dashboard
├── tests/                   # Offline unit/route tests + Neo4j integration tests
├── .env.example             # Environment template
├── requirements.txt         # Runtime dependencies
├── requirements-dev.txt     # Dev/test dependencies
├── pyproject.toml           # ruff + pytest configuration
├── Dockerfile               # App image
├── docker-compose.yml       # App + Neo4j
└── README.md                # This file
```

## 🔌 API Highlights

All endpoints under `/api`: ingest (`POST /ingest/url`, `POST /ingest/text`),
query (`POST /ask`, `POST /graph/search`, `POST /graph/path`), graph data
(`GET /graph/data[?source_doc=]`, `GET /graph/stats`, `GET /graph/verify`,
`GET /graph/export?format=json|csv`), and source management (`GET /sources`,
`DELETE /sources?source_doc=`, `POST /graph/clear`). Duplicate entities across sources are surfaced by `GET /graph/duplicates`
and merged via `POST /graph/merge` (suggest-only; set `AUTO_MERGE=exact` to
auto-merge exact matches at ingest). Live updates stream over
`WS /ws/graph`. Optional hardening via env: `API_KEY` (X-API-Key auth on
mutating routes), `ALLOWED_ORIGINS` (CORS). Ingest and ask are rate-limited
per IP.

## 🎯 Judging Criteria Alignment

| Criterion | How We Win |
|-----------|-----------|
| **Completeness** | Full pipeline: ingest → extract → graph → query → dashboard |
| **Innovation** | GraphRAG + autonomous agent = novel reasoning over connected data |
| **Real problem** | "Make sense of scattered info" — universal pain point |
| **Sponsor usage** | All 4 sponsors integrated naturally |

## 📝 License

MIT — built with ❤️ for Daytona HackSprint Tokyo 2026
