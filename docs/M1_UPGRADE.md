# M1: source integrity and retrieval upgrade

This release separates source IDs from titles, replaces source graphs atomically,
retrieves graph evidence by entity ID, and isolates compatible semantic vectors.
It does not add excerpt citations, persistent jobs, or private access.

## Behavior and compatibility

- New URL IDs use SHA-256 of a conservatively canonical URL: scheme/host case,
  default port and fragment normalize; paths/query parameters remain meaningful.
  Two pages with equal titles stay separate. Durable display URLs omit queries
  to avoid persisting signed URL/access-token values; IDs still distinguish queries. Named text sources refresh by name;
  uploads by explicit source name or filename, within their respective kinds.
  Reusing a name replaces that source; use different names for separate documents.
- Version fingerprints cover cleaned text and extraction model/configuration.
  Unchanged versions skip extraction and post-ingest compute. This covers the
  current 50,000-character extraction window; full-document chunks remain M2.
- Extraction precedes atomic publication. Conflicting concurrent refreshes return
  a retryable business failure; failed extraction/write preserves the old graph.
- `/api/sources` retains `source_doc` as the key, adding display metadata. The UI
  escapes titles and handles `graph_reset` by refetching, removing obsolete IDs.
- Hidden source reservations and minimal deletion tombstones prevent stale work
  from reviving a deleted/cleared graph. Later deliberate ingestion can replace
  a tombstone. M3 will address long-term state retention.
- Shared merged entities survive removal of one source, but conservatively lose
  summaries, aliases and embeddings: legacy fields have no per-source attribution.
  This prevents stale evidence, at the expense of some surviving evidence until
  a changed/new source version is extracted (unchanged ingestion is skipped). Full assertion lineage belongs to M2.
- GraphRAG preserves existing result/status keys and adds structured `evidence`:
  selected IDs, bounded nodes/edges, text and complete traversed source provenance.
  Isolated summaries now reach the prompt. These are graph-derived evidence
  records, not validated claim-level citations.

## Explicit legacy migration

Take a database backup/snapshot using the deployment's established procedure and
stop ingestion/seed scripts/old workers during upgrade. Set `NEO4J_URI`,
`NEO4J_USER`, `NEO4J_PASSWORD` in the shell or local `.env` for the intended DB.

```bash
python -m scripts.migrate_sources --dry-run
python -m scripts.migrate_sources --apply
python -m scripts.migrate_sources --dry-run
```

The metadata migration discovers scalar/list entity provenance and relationship
sources. Legacy strings become Source IDs verbatim; it changes no entity IDs,
memberships or assertions and does not downgrade existing Source records. Dry-run
rolls back its inspection transaction. Normal schema initialization still applies
the preexisting scalar-to-list provenance migration.

Old equal-title collisions cannot be reconstructed. Legacy title records are not
automatically associated with new canonical URLs. Reingest desired URLs, verify
the new sources, then deliberately remove obsolete legacy entries.

```bash
python -m scripts.migrate_sources --rollback --dry-run
python -m scripts.migrate_sources --rollback --apply
```

Rollback removes only unchanged metadata created by this migration. Modified/newly
published Sources and graph data survive. This does not roll back later graph
publication or replace a database snapshot restore; retain the backup.

## Rebuild semantic embeddings

New `entity_semantic_embedding` indexes active compatible `SemanticEntity`
vectors. Old mixed-vector properties/indexes may remain for compatibility but are
never queried for semantic search. Legacy vectors lacking trustworthy per-call
model/version metadata are quarantined. Hash fallbacks stay outside semantic
retrieval and semantic duplicate suggestions.

Configure `NOSANA_EMBEDDING_MODEL` and `NOSANA_EMBEDDING_VERSION` (default `1`).
Without a model name, an endpoint fingerprint is used. Bump the version if the
model behind the same endpoint changes. Dimension remains 384. Restart all
workers together: one active space is supported per graph, with GraphState guards
rejecting old-space writes/queries from stale workers.

```bash
python -m scripts.reembed_graph --dry-run
# Explicit provider calls: may incur cost and send entity labels/summaries.
python -m scripts.reembed_graph --apply
python -m scripts.reembed_graph --source SOURCE_ID --dry-run
python -m scripts.reembed_graph --source SOURCE_ID --apply
```

Apply requires `NOSANA_API_KEY`/`NOSANA_EMBEDDING_URL` and initializes the active
schema/index. It does not rerun extraction; writes require unchanged node text and
skip provider fallback results. Reports distinguish stored, unavailable, rejected,
and capped items. Rerun rejected work after concurrent mutation settles. Each run
is capped at 5,000 entities; split large graphs by source. Single larger sources
need future pagination. A capped/failed run is not a complete semantic rebuild.

## Validation

Offline tests cover caller contracts. M1 integration tests additionally require
an explicitly disposable Neo4j 5.26 database and `M1_DISPOSABLE_NEO4J=1`:

```bash
M1_DISPOSABLE_NEO4J=1 python -m pytest -m integration tests/test_m1_integration.py
```

These tests perform destructive cleanup. They exercise concurrent publication,
failed-refresh rollback, tombstones, merged-source deletion, stale embedding
rejection, active-space changes and migration/idempotency/rollback. See the PR for
executed results and review limits. Never point integration tests at production.
