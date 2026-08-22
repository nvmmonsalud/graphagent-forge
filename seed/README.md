# Seed corpus — a demo that needs zero API keys

`seed/graph.json` is a pre-extracted knowledge graph, plus the three human-readable
documents it corresponds to in `seed/docs/`. Load it with:

```bash
python -m scripts.seed_graph --dry-run     # validate + print the write plan, no connection
python -m scripts.seed_graph               # idempotent upsert into Neo4j
python -m scripts.seed_graph --reset       # drop these three sources first, then seed
python -m scripts.seed_graph --clear       # drop these three sources and exit
```

Only Neo4j is required. No `KIMI_API_KEY`, no `NOSANA_EMBEDDING_URL`, no `DAYTONA_API_KEY`.

---

## Provenance

The fixture is **hand-authored to match the `extract_entities` envelope** — the same
`{"nodes": [...], "edges": [...]}` shape, the same `Person|Org|Concept|Event|Place|Technology`
type vocabulary, the same `properties.summary` / `properties.context` fields. **No LLM produced
it and it was not captured from a live extraction run.** It is a deterministic stand-in written
so the demo is rehearsable and repeatable; treat it as fixture data, not as evidence of what a
particular model returns.

The documents in `seed/docs/` were written alongside the fixture and say the same things in
prose. They are the file-upload demo payload for when keys *are* configured (see
[Re-ingesting on top of the seed](#re-ingesting-on-top-of-the-seed)).

## What's in it

| Document (`source_doc`) | Id prefix | Nodes | Edges | Subject |
| --- | --- | --- | --- | --- |
| `Daytona HackSprint Tokyo 2026` | `2c7796b6_` | 18 | 27 | the event, venue, schedule, judges, rubric, prize |
| `GraphAgent Forge Architecture Notes` | `ec8c4227_` | 22 | 33 | FastAPI, Neo4j, job queue, WebSocket, D3, GraphRAG, SSRF guard, upload |
| `GraphAgent Forge Sponsor Stack` | `3199c1b4_` | 16 | 24 | the four sponsors and each one's fallback |
| **Total** | | **56** | **84** | |

Node ids are unprefixed inside the fixture. The seeder runs them through
`src.ingestion.graph_writer.prefix_and_validate` — the *same* function the live ingest path
uses — so a seeded id is exactly `md5(source_doc)[:8] + "_" + <raw id>`. That is deliberate:
it means `--reset` followed by a live re-ingest of the same document writes byte-identical ids
and collides (MERGE) instead of duplicating.

## Three islands, on purpose

Edges are per-document assertions, so a freshly seeded graph is **three weakly-connected
components** of 18 / 22 / 16 nodes. Nothing joins them. That is the setup for the best beat in
the demo: the graph only becomes one graph when a human merges a duplicate.

The fixture plants exactly **three cross-document tier-1 duplicate pairs** — spelling variants
that collapse to the same string under `normalize_label` (lowercase, strip punctuation, collapse
whitespace). Each pair spans two different documents, which is what `find_duplicate_groups`
requires: it drops any group whose members all share an identical provenance set.

| Normalized | Node A | Node B | Bridges |
| --- | --- | --- | --- |
| `daytona` | `Daytona` — `2c7796b6_daytona_org` | `Daytona.` — `3199c1b4_daytona_sponsor` | HackSprint ↔ Sponsor Stack |
| `neo4j` | `Neo4j` — `ec8c4227_neo4j` | `Neo4J` — `3199c1b4_neo4j_sponsor` | Architecture ↔ Sponsor Stack |
| `nosana` | `Nosana` — `ec8c4227_nosana` | `NOSANA` — `3199c1b4_nosana_sponsor` | Architecture ↔ Sponsor Stack |

## Path finding

Two pairs are guaranteed for the `POST /api/graph/path` demo (shortest path, undirected,
bounded to six hops):

**(a) Long path in the fresh, pre-merge graph — no merge needed.**

> From `SSRF Guard` → To `D3 Force Graph` — **5 hops**, entirely inside *Architecture Notes*:
> `SSRF Guard → URL Extractor → Ingest Job Queue → JobManager → WebSocket Broadcast Channel →
> D3 Force Graph`.

(Backup inside *HackSprint*, if you want a second one: `Tokyo` → `Judging Rubric`, 4 hops.)

**(b) Pair that only resolves AFTER the Nosana merge.**

> From `Vector Index` → To `Solana` — **no path at all** in the fresh graph (`Vector Index` is
> in *Architecture Notes*, `Solana` is in *Sponsor Stack*). After merging `Nosana` with
> `NOSANA` it becomes **2 hops**: `Vector Index → Nosana → Solana`.

Run (b) *before* the merge to show the empty result, merge, then run it again. That single
merge is what visibly welds two islands together on the canvas.

## Keyless retrieval — why the sponsor summaries read the way they do

With no Nosana embedding endpoint, `GraphRAGEngine` skips vector search entirely (hash
pseudo-embeddings carry no semantic signal) and falls back to keyword search: the question is
stripped of stopwords by `_extract_search_terms`, capped at six terms, and each surviving term
is matched with a Cypher `CONTAINS` against node label, summary and aliases.

For the scripted question **"Who are the sponsors and what do they provide?"** the surviving
terms are exactly `['sponsors', 'they', 'provide']`.

The fixture is tuned for that: **exactly five nodes** contain the substrings `sponsor` *and*
`provide` in their summary — and, more to the point, contain the literal search terms
`sponsors` and `provide`:

`Sponsor Stack` · `Daytona.` · `NOSANA` · `Neo4J` · `Kimi AI`

Five is under the per-term result limit of eight and matches the top-5 candidate cut exactly, so
all five land in the context window with none crowded out. No other node in the corpus uses the
plural "sponsors", deliberately — `Sponsor Integration Criterion` in the HackSprint document says
"sponsor" singular so it cannot displace a real sponsor node. **If you edit summaries, re-run the
self-check before the demo.**

## Demo cheatsheet

| # | Beat | What to do | What the audience sees |
| --- | --- | --- | --- |
| 0 | Setup | `python -m scripts.seed_graph --reset` then start the server | `Seeded 3 documents · 56 nodes · 84 edges · 3 tier-1 duplicate groups pending review` |
| 1 | The graph exists | Open `/` | 56 nodes in three coloured clusters, no keys configured |
| 2 | Ask it something | Ask **"Who are the sponsors and what do they provide?"** | Grounded answer over all four sponsors *and their fallbacks* — retrieved by keyword only |
| 3 | Provenance | Source list / `GET /api/sources` | Three documents, each answer citing which it came from |
| 4 | Reach | `POST /api/graph/path` `SSRF Guard` → `D3 Force Graph` | A real 5-hop traversal, not a lookup |
| 5 | The gap | `POST /api/graph/path` `Vector Index` → `Solana` | Empty — two disconnected islands |
| 6 | **The merge (best beat)** | `GET /api/graph/duplicates`, then merge `ec8c4227_nosana` + `3199c1b4_nosana_sponsor` | Two nodes become one *in place* over the WebSocket; the architecture island and the sponsor island snap together |
| 7 | Prove it | Re-run beat 5 | `Vector Index → Nosana → Solana` |
| 8 | Live ingest (only with a Kimi key) | Upload `seed/docs/*.md` or ingest a URL | New nodes stream in over `/ws/graph` alongside the seeded ones |

Two more merges (`daytona`, `neo4j`) are left on the table if a judge asks "does it do it again?".

## Re-ingesting on top of the seed

The seed and a live ingest share ids only when they share a `source_doc`. For an exact
collision, upload `seed/docs/graphagent-forge-sponsor-stack.md` with the `source` field set to
`GraphAgent Forge Sponsor Stack` — the node ids the LLM produces will be prefixed with the same
`3199c1b4_` hash, and matching ids will MERGE rather than duplicate. Without the `source`
override, the upload lands as its own document (filename identity) and shows up as a fourth
island — also a fine demo, just a different one.

`--reset` deletes only these three `source_doc`s via `delete_source`. It never calls
`clear_graph()`, so anything a judge ingested themselves survives a re-seed.

## Self-check

The invariants above are worth re-verifying after any edit to the fixture:

- 3 documents; node ids unique within each document; every edge endpoint declared as a node in
  the *same* document (the seeder enforces this and exits 1 with the failing rule).
- 56 node rows, 84 edges; every `type` inside `Person|Org|Concept|Event|Place|Technology`.
- Exactly 3 cross-document `normalize_label` collisions, each spanning two different documents.
- At least 4 nodes whose summary contains both `sponsor` and `provide`.

`python -m scripts.seed_graph --dry-run` checks the structural half and prints the duplicate
groups the fixture will create.
