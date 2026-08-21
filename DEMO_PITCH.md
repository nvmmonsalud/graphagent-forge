# 🎤 GraphAgent Forge — 2-Minute Demo Pitch

## The Hook (10s)
> "What if you could paste any URL and instantly build a knowledge graph you could reason over? That's GraphAgent Forge."

## The Problem (15s)
> "Today's AI chatbots are stateless — they forget everything between sessions. And scattered information across articles, docs, and notes stays disconnected. You can't ask 'how do these things relate?' because nothing is connected."

## The Solution (20s)
> "GraphAgent Forge ingests any URL or text, uses Kimi AI to extract entities and relationships, and stores them in a Neo4j knowledge graph. Then you can ask questions that require multi-hop reasoning across all that data."

## LIVE DEMO (60s)
**Step 1 — Ingest (15s)**
- Open http://localhost:8000
- Paste a URL (e.g., a tech article, company page, or research paper)
- Click "Ingest URL"
- Show the node/edge count animating up
- "Kimi AI just extracted 48 entities and 59 relationships in 3 seconds"

**Step 2 — Explore the Graph (15s)**
- Scroll to the knowledge graph section
- Show the force-directed visualization: "485 nodes, 878 edges — all connected"
- Hover over a node to show the tooltip and neighbor highlighting
- "Every entity is color-coded by type — people, orgs, tech, events"

**Step 3 — Ask a Question (15s)**
- Type: "Who are the sponsors and what does each one provide?"
- Click Ask
- Show the answer with context entities
- "GraphRAG searched the graph, found relevant context, and Kimi reasoned over it"

**Step 4 — Sponsor Integration (15s)**
- "Here's how we used every sponsor's technology:"
  - **Kimi AI**: Entity extraction + reasoning (kimi-k2.7-code-highspeed)
  - **Neo4j**: Knowledge graph storage + GraphRAG queries
  - **Daytona**: Isolated sandbox execution (0.5s boot time!)
  - **Nosana**: GPU compute for embeddings and scoring

## The Vision (10s)
> "Imagine feeding this every article, every internal doc, every meeting note — and having an AI that knows how everything connects. That's the future of knowledge work."

## The Ask (5s)
> "GraphAgent Forge. Built with Kimi AI, Neo4j, Daytona, and Nosana. Thank you."

---

## 🎯 Tips for Delivery
1. **Start with the problem** — judges relate to pain points
2. **Show, don't tell** — the force graph is the visual wow moment
3. **Name-drop sponsors naturally** — don't force it
4. **End with vision** — what could this become?
5. **Practice the 60s demo** — timing is everything
6. **Have a backup plan** — if API is slow, show screenshots

## 🔗 URLs to Have Ready
- Dashboard: http://localhost:8000
- GitHub: (set up before demo day)
- Neo4j Aura console: https://console.neo4j.io
