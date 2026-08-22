# GraphAgent Forge Architecture Notes

*Source document for the seed corpus. Fixture `source_doc`: `GraphAgent Forge Architecture Notes`.*

**GraphAgent Forge** is a knowledge-graph agent. It ingests URLs, pasted text and uploaded
files, extracts entities into a graph, and answers questions over that graph instead of over
raw text.

## Service shell

The whole service is built on **FastAPI**, an async Python web framework, with a lifespan hook
that wires shared state at startup. FastAPI serves the REST API under `/api`, the WebSocket
endpoint at `/ws/graph`, and the single-page frontend at the root path. A **Rate Limiter** — a
per-IP in-memory limit of ten requests a minute, shared across the ingest and ask routes — sits
in front of everything expensive and guards the ingest queue.

Extracted entities and relationships are stored in **Neo4j**: every entity is an `:Entity` node
and every relationship a `:RELATES_TO` edge, with the semantic relationship name kept in a
property. Neo4j defines the **Entity Schema** — one node label carrying an id, a label, a type,
a summary, a normalised label, a list of source documents, aliases, and an embedding with the
method that produced it. Constraints and indexes are created idempotently at startup. The schema
includes a **Vector Index**: a 384-dimension cosine index over entity embeddings, where only
genuinely semantic vectors are trusted for retrieval and deterministic stand-in vectors are
excluded.

## Ingestion

The **Ingest Job Queue** is a submit-then-track queue: a request returns a queued job record
immediately while two background workers execute the ingest and report each stage back. The
queue is managed by **JobManager**, the in-memory registry that tracks job state, caps
concurrency, evicts old finished jobs, and drains everything still in flight at shutdown.
JobManager broadcasts every state transition over the **WebSocket Broadcast Channel** at
`/ws/graph`, which emits `job_update`, `graph_update` and `graph_merge` messages so every
connected browser sees the graph change as it is written.

A URL ingest job drives the **URL Extractor**, which fetches a page, strips scripts, navigation
and boilerplate, and hands clean text plus a title downstream; the title becomes the document
identity for provenance. The extractor is protected by the **SSRF Guard**: only `http` and
`https` schemes, every DNS record checked against private ranges, redirects re-validated by
hand, a streamed size cap and a content-type gate. No URL is fetched until the guard has cleared
every resolved address.

A file ingest job drives the **File Upload Pipeline**, which accepts an uploaded document,
parses the bytes into plain text, rejects unparseable files up front, and then joins the same
extract-and-write tail as URL ingestion.

Both feed the **Entity Parser**, which asks the language model for a strict JSON envelope of
nodes and edges using a fixed entity vocabulary of Person, Org, Concept, Event, Place and
Technology. Its output goes straight to the **Graph Writer**, which prefixes every node id with
a hash of its source document so two documents never collide, drops edges pointing at ids that
were never declared, applies label normalisation, and writes nodes and edges in one transaction.

## Retrieval

The **GraphRAG Engine** queries Neo4j directly. Vector search runs first when a semantic
embedding is available, using the vector index; then keyword search; then a two-hop
neighbourhood walk; and finally an LLM answer grounded only in that graph context — the model
is never given the raw source text.

The **Keyword Search Fallback** is the retrieval path that works with no embedding service at
all: the question is stripped of stopwords and each surviving term is matched as a substring
against entity labels, aliases and summaries.

**Nosana**, the decentralised GPU network, powers the embeddings written into the vector index.
When no endpoint is configured the embedding step degrades to a deterministic local vector
instead of failing.

## Cross-cutting

**Graceful Degradation** is the rule that every external service has a local fallback, so the
whole application boots and demos correctly with zero configured keys and reports which path
actually ran. It governs both the Nosana client and the keyword retrieval path.

**Label Normalization** — lowercase, strip punctuation, collapse whitespace — is what turns
spelling variants of the same entity into a detectable duplicate group, and so enables the
**Duplicate Merge Workflow**. That workflow is suggest-only by design: exact normalised-label
matches across different documents are proposed, and a human confirms the merge that repoints
edges and unions provenance. A confirmed merge is announced on the WebSocket channel as its own
message so the canvas updates in place.

## Frontend

The **Frontend Dashboard** is the single-page UI: ingest controls, a question box, a source
list, and the graph canvas. Every dynamic value is escaped before it reaches the DOM because
scraped content is untrusted. Its centrepiece is the **D3 Force Graph**, a force-directed
visualisation where nodes are coloured by entity type and merged nodes are removed in place
rather than through a full redraw. The dashboard's duplicates panel is where a human confirms
a merge.
