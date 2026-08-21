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
│  Dashboard   │◀──▶│  GraphRAG    │◀──▶│   Neo4j      │
│  (Streamlit) │    │  Query Eng.  │    │  (Graph DB)  │
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
# Edit .env with your API keys

# 3. Run the app
python -m src.main

# 4. Open dashboard
open http://localhost:8000
```

## 📁 Project Structure

```
graphagent-forge/
├── src/
│   ├── main.py              # FastAPI app entry point
│   ├── agent/
│   │   ├── core.py          # Main agent loop
│   │   ├── prompts.py       # LLM prompt templates
│   │   └── daytona_exec.py  # Daytona sandbox execution
│   ├── graph/
│   │   ├── neo4j_client.py  # Neo4j connection & queries
│   │   ├── schema.py        # Graph schema definitions
│   │   └── graphrag.py      # GraphRAG query engine
│   ├── ingestion/
│   │   ├── extractor.py     # URL/document content extraction
│   │   ├── entity_parser.py # Kimi-powered entity extraction
│   │   └── graph_writer.py  # Write entities to Neo4j
│   └── api/
│       └── routes.py        # FastAPI endpoints
├── frontend/
│   └── app.py               # Streamlit dashboard
├── static/                  # Assets
├── tests/                   # Tests
├── .env.example             # Environment template
├── requirements.txt         # Python dependencies
└── README.md                # This file
```

## 🎯 Judging Criteria Alignment

| Criterion | How We Win |
|-----------|-----------|
| **Completeness** | Full pipeline: ingest → extract → graph → query → dashboard |
| **Innovation** | GraphRAG + autonomous agent = novel reasoning over connected data |
| **Real problem** | "Make sense of scattered info" — universal pain point |
| **Sponsor usage** | All 4 sponsors integrated naturally |

## 📝 License

MIT — built with ❤️ for Daytona HackSprint Tokyo 2026
