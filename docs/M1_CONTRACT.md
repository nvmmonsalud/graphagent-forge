# M1 implementation contract

Baseline: `bcc1858`. Preserve existing HTTP result envelopes and legacy provenance
keys. Changes add metadata; they do not claim M2 excerpt/claim citations or M4 auth.

## Sources

New ingestion uses a Source map: `id`, `title`, `kind`, `uri`, `content_hash`,
`version`, `extraction_version`. IDs use `src_` + SHA-256 of kind and canonical
identity. URL identity preserves query parameters and only normalizes scheme/host,
default port and fragment. Named text/upload identities are scoped by kind and
their explicit source name (default text `manual`; upload filename otherwise).
Durable/display URI omits query values; full queries remain only in hashed identity.
Version fingerprints cleaned content plus extraction model/schema configuration;
the content hash is separate. A title is never an identity for a URL.

`Neo4jClient.get_source(source_id)` returns a map or None.
`publish_source_graph(graph_data, *, source, expected_version)` publishes atomically:
lock the Source record, compare the current version with the pre-extraction
expected version, remove only this source's old memberships/assertions, write the
new graph and Source metadata in the same transaction. Same version is an
unchanged result; a different concurrent version is a conflict without mutation.
Return `nodes_written`, `edges_written`, `unchanged` and `conflict` booleans.
Graph ingestion returns a business failure on conflict. Failures/cancellation
rollback; no partially published version. Publication, deletion, clear and merge
acquire the same persistent graph mutation lock before source locks. Source reads
reserve absent identities as placeholders; deletion/clear retain minimal tombstones
with changed versions so in-flight refreshes cannot resurrect removed data. Listing
excludes placeholders/tombstones. New deliberate ingestion can replace a tombstone.
IDs for new versioned entities use a
SHA-256 source/version namespace; legacy seeded IDs retain their historical prefix.

Before extraction, skip matching versions to avoid paid extraction. Skip paid
post-ingest work on unchanged results. Source refresh broadcasts require a full
graph refetch, since incremental appends cannot remove superseded nodes.

Migration is explicit, idempotent and reversible: legacy `source_docs` strings
become Source IDs verbatim; no rekeying of merged nodes or edges. Legacy title
collisions cannot be reconstructed and must be reported, not guessed. Source
listing keeps `source_doc` as key and adds `title`, `kind`, `uri`, `version`.
Deletion/clear remove descriptive Source metadata and graph membership consistently,
retaining only tombstone identity/version state. Shared merged entities losing a
source conservatively lose summaries, aliases and embeddings: legacy data cannot
recover which source supplied those fields, and stale evidence must not survive.
Relationships are distinct per endpoint/type/source, including during merges.
Document backup, dry-run, application and rollback commands before rollout.

## Retrieval

`get_entity_context(entity_id, depth=2)` returns
`{text, source_docs, entity_ids, nodes, edges}`. Center summary is always present,
even with zero edges. Traversal is bounded and selected by exact ID. Structured
nodes/edges carry their provenance; `source_docs` unions all included evidence.
`GraphRAGEngine.query` uses this method, preserving status/source/context_nodes
keys and adding `evidence`. Do not substitute a label after selecting an ID.

## Embeddings

`NosanaClient.get_embedding_result(text)` returns
`{embedding, method, model, version, dimension}` per call. `get_embedding(text)`
remains a list-returning compatibility wrapper. `last_embedding_method` is only
diagnostic; concurrent writes and retrieval never derive metadata from it.

A shared `configured_embedding_space()` helper in `src/graph/embedding_space.py`
defines active `model`, `version`, `dimension` (384). Model is explicit env
`NOSANA_EMBEDDING_MODEL`, otherwise a non-secret endpoint fingerprint; version is
`NOSANA_EMBEDDING_VERSION` (default `1`). Embedding results use these values.

Only valid active-space semantic results populate `semantic_embedding` and the
`:SemanticEntity` label. New index `entity_semantic_embedding` covers that label
and property. Legacy/fallback vectors remain outside it. Startup quarantines
incompatible semantic properties/labels; no blind migration of old mislabeled
vectors. `vector_search(..., model=None, version=None)` verifies the active space;
semantic duplicate suggestions also use only the active compatible space.
`set_node_embeddings(items, method=...)` accepts per-item metadata; the legacy
method argument is retained for explicit compatibility callers. Core ingestion
and merged-node refresh must supply per-item results.

## Validation and ownership

- Graph agent: `neo4j_client.py`, graph/migration tests and migration CLI.
- Retrieval agent: `nosana_client.py`, `graphrag.py`, embedding-space helper,
  retrieval/embedding tests. Coordinate test ownership with workflow agent.
- Workflow agent: `core.py`, `graph_writer.py`, source identity helper and tests.
- Integrator: HTTP/frontend/seeder compatibility, docs, disposable Neo4j harness.
- Separate code and security reviewers check final integrated diff and migrations.

Required cases: equal-title URLs, isolated summaries, same-label distinct IDs,
traversed source provenance, mixed embedding batches, unchanged skip, conflicting
concurrent publication, merged-source deletion, legacy migration/rollback, and
failed-refresh rollback on a disposable Neo4j 5.26 instance.
