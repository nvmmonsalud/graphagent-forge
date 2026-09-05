# 🎤 GraphAgent Forge — Demo Pitch

## The Hook (10s)
> "What if you could paste any URL and instantly build a knowledge graph you could reason over? That's GraphAgent Forge."

## The Problem (15s)
> "Today's AI chatbots are stateless — they forget everything between sessions. And scattered information across articles, docs, and notes stays disconnected. You can't ask 'how do these things relate?' because nothing is connected."

## The Solution (20s)
> "GraphAgent Forge ingests a URL, pasted text, or an uploaded file, uses Kimi AI to extract entities and relationships, and stores them in a Neo4j knowledge graph. Then you can ask questions that require multi-hop reasoning across all that data — and every step is visible live, not a black box."

## Pre-flight checklist (do this before you're on stage)
- [ ] **Seed the graph**: `python -m scripts.seed_graph` against a running Neo4j. This loads a small multi-document graph (`seed/graph.json`) so the "Explore the Graph" beat has real content even with zero API keys — don't rely on a live ingest finishing in time.
- [ ] **Run without `DEV_RELOAD`** (leave it unset, i.e. don't run with `DEV_RELOAD=1`). Uvicorn's `--reload` restarts the whole process on any file change, which silently kills whatever ingest job is in flight — deadly mid-demo. `DEV_RELOAD=1` is for editing code, not presenting.
- [ ] **Raise `RATE_LIMIT_MAX`** if you're going to rehearse the demo and then present it back-to-back. The 10-requests/60s bucket is *shared* across every `/ingest/*` call **and** every `/ask` call, per IP, per process — a rehearsal run can leave you rate-limited (`429`) five minutes later when it actually matters. Set `RATE_LIMIT_MAX=50` or higher in `.env` for demo day.
- [ ] **Confirm the connection badge reads `live`** (bottom-left of the graph panel) before you start talking — if it says `reconnecting`, the WebSocket hasn't connected and node updates won't animate in.
- [ ] **Budget a full minute per live ingest.** Kimi's current models are reasoning models: a ~3k-character page took 55–60s of extraction end to end in rehearsal. Submit the URL early in the talk (it returns a job id instantly) and keep talking; don't stand there waiting for it.
- [ ] **Don't paste a Wikipedia URL from a cloud/datacenter box.** Wikimedia's bot policy 403s the fetch from datacenter egress even with a browser User-Agent; from a laptop on normal wifi it's fine. Safe rehearsed example: `https://www.python.org/about/` (47 nodes / 49 edges).
- [ ] Have `KIMI_API_KEY` configured if you want the "Ask" beat to produce a real written answer — everything else on the page (graph, sources, duplicates, verify, export, path finder) works with zero keys configured.

## LIVE DEMO

**Step 1 — Ingest, live (20s)**
- Open http://localhost:8000 (the web UI) — start on the "02 — Ingest" section
- Paste a URL (e.g., a tech article, company page, or research paper) and click **🚀 Ingest URL**
- The button spins and the result box shows **"Queued…"**, then flips to **"Running — {stage}"** as the job moves through its stages: `fetching` → `extracting` → `embedding` → `broadcasting` → `verifying` — all pushed live over the `/ws/graph` WebSocket, no polling
- New nodes and edges stream into the force-directed graph as `broadcasting` fires — the count in the graph badge climbs in real time while you're still talking
- "This isn't a spinner — that's an actual async job queue. Ingest returns instantly with a job id, two background workers run the pipeline, and every stage is broadcast to every connected browser."
- On success the result box shows entity/relationship counts and — if `KIMI_API_KEY` isn't set — the app still succeeds gracefully rather than crashing, because every sponsor integration has a local fallback

**Step 2 — Explore the Graph (15s)**
- Scroll to "01 — Knowledge Graph" (or start here if you seeded beforehand)
- Show the force-directed visualization: seeded from `scripts.seed_graph`, the fixture (`seed/graph.json`) loads 3 sample documents totaling 56 nodes and 84 edges before any dedup/merge runs
- Hover over a node to show the tooltip and neighbor highlighting
- "Every entity is color-coded by type — people, orgs, tech, events. And the connection badge here reads `live` — the graph updates in place as new data lands, no refresh needed."

**Step 3 — Ask a Question (15s)**
- Scroll to "03 — Query"
- Type the placeholder question verbatim: `Who are the sponsors and what do they provide?`
- Click **🧠 Ask**
- Show the answer with its context entities
- "GraphRAG found the relevant nodes — vector search when we have real embeddings, keyword search otherwise — pulled their 2-hop neighborhood, and Kimi reasoned over only that graph context, not the open web."

**Step 4 — The features judges don't see if you stop at "ask a question" (30s)**
Pick 3–4 of these based on time — each is a real, working panel, not a mockup:
- **📄 File/PDF upload** ("02 — Ingest", From File): drag in a PDF, .txt, or .md — parsed in memory, magic-byte checked against its declared type, never written to disk
- **🛡️ Verify graph integrity**: runs an actual integrity check in a Daytona sandbox (or a local subprocess when no `DAYTONA_API_KEY` is set) and reports which path ran plus the measured time it took — not a hardcoded number
- **⬇ Export**: one click to JSON or CSV of the whole graph
- **🧬 Duplicates / 🔀 Merge**: "Scan for duplicates" surfaces entities that likely refer to the same thing (exact-label matches always; semantic matches too, once real embeddings are configured) — merge them with one click and watch the graph update live
- **📚 Sources panel**: every ingested document, with the ability to focus the graph on just one source or delete it
- **05 — Path Finder** (🔍 Find Path): type two entity labels and highlight the shortest connecting path through the graph
- **The connection badge** itself: `live` vs `reconnecting` is a real WebSocket health indicator, not decoration — point at it after a merge or a new ingest lands to show the graph updated without a page reload

## Sponsor Integration (15s)
> "Here's how we used every sponsor's technology — and the graceful-degradation story is the real engineering flex: every one of these has a working local fallback, so the whole app boots and demos with zero keys configured."
- **Kimi AI**: entity extraction + reasoning (`kimi-k2.7-code-highspeed` by default, configurable via `KIMI_MODEL`) — no fallback, but fails with a structured error, never a crash
- **Neo4j**: knowledge graph storage + GraphRAG queries, vector index for semantic search
- **Daytona**: isolated sandbox execution of the graph-integrity check — a real sandbox when `DAYTONA_API_KEY` is set, a local subprocess otherwise, with the verify response reporting which path ran and the measured wall time
- **Nosana**: GPU embeddings via a configurable endpoint, with a deterministic hash-based fallback so the app runs keyless; semantic duplicate detection turns itself on only when real embeddings exist

## The Vision (10s)
> "Imagine feeding this every article, every internal doc, every meeting note — and having an AI that knows how everything connects. That's the future of knowledge work."

## The Ask (5s)
> "GraphAgent Forge. Built with Kimi AI, Neo4j, Daytona, and Nosana. Thank you."

---

## 🎯 Tips for Delivery
1. **Start with the problem** — judges relate to pain points
2. **Show, don't tell** — the live job queue and the force graph filling in are the visual wow moment
3. **Name-drop sponsors naturally** — don't force it
4. **End with vision** — what could this become?
5. **Practice the full run** — timing is everything, and the job-queue stages take real seconds each
6. **Have a backup plan**: seed the graph beforehand (`python -m scripts.seed_graph`). Every panel except the written LLM answer — graph view, sources, verify, export, duplicates/merge, path finder — works with zero API keys configured, so a slow or missing `KIMI_API_KEY` never blanks the demo.

## 🔗 URLs to Have Ready
- Web UI: http://localhost:8000
- GitHub: https://github.com/nvmmonsalud/graphagent-forge
- Neo4j Aura console: https://console.neo4j.io
