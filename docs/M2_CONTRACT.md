# M2 evidence/citation implementation contract

Baseline `1624cca`; stack on M1 draft #20. Do not merge/change M1 to begin M2.
M2 retains public API envelopes, adds `insufficient_evidence` status, and does not
claim tenant isolation (M4) or durable jobs/history (M3).

## Bounded ingestion

Core requests normalized extraction up to 200,000 characters via optional
`max_chars` on URL/text/file extractors; keep old 50,000 default compatibility.
Do not append synthetic truncation text in the M2 path. Preserve byte/SSRF/PDF
limits and report input truncation explicitly. Offsets count Unicode characters
in normalized extracted text, not original HTML/file bytes. PDF page references
are not claimed; text offsets work across formats.

New `src/ingestion/chunks.py` owns constants/helpers: CHUNK_SIZE=8000,
CHUNK_OVERLAP=400, MAX_CHUNKS=12, MAX_DOCUMENT_CHARS=200000. Plan at most 12
extraction calls, concurrency <=2 per document; model output budgets remain
EXTRACT_MAX_TOKENS per call. Source extraction version includes policy/schema so
M1 sources are re-extracted once, then unchanged versions skip all paid work.

Each chunk: `{id, source_id, version, ordinal, start, end, text}`. ID is `ev_` +
SHA-256(source_id/version/ordinal/text digest); helper `chunk_id(...)` supplies one
definition. Offsets satisfy end-start=len(text). ID provenance is assigned by the
server, never trusted from the LLM. Namespace per-chunk local entity IDs before
version prefixing; attach `evidence_ids` to nodes/edges extracted from that chunk.
Global graph caps 500 nodes/1000 edges still apply with endpoint closure; report
graph capping separately from document coverage. Empty-entity chunks can still
publish useful source excerpts. Any failed planned chunk aborts publication and
preserves the previous version (no partial successful-version on provider errors).

Coverage: `{captured_chars, processed_chars, skipped_chars, planned_chunks,
completed_chunks, failed_chunks, input_truncated, partial, graph_capped}`. Report
unique covered chars (overlap not double-counted). Limits can deliberately publish
partial coverage with clear reporting; errors do not publish. Source metadata
receives coverage; graph_data receives chunks. Job result contains counts, not raw
document text. Actual original length may be unknown when byte/character-capped.

## Atomic storage / graph agent ownership

Extend `publish_source_graph` to validate and write `graph_data.chunks` and node/
edge evidence_ids in the same transaction as Source version and graph. Store
`:EvidenceChunk` nodes and Source HAS_CHUNK linkage (not Entity nodes). Metadata
coverage can use JSON internally; external methods return a dict. Source deletion,
clear and version replacement remove chunks. Legacy direct source writes must
invalidate old chunks before republishing; legacy graphs cannot invent excerpts.
Merge unions evidence IDs, preserves per-source assertions, and removes deleted
chunk references on source removal. Always check current Source version.

Methods:
- `get_evidence(evidence_id)` -> current chunk map + title/uri, or None.
- `get_source_evidence(source_id)` -> bounded current chunk list (max12).
- `search_evidence(question, entity_ids=None, limit=6)` -> ranked current chunks;
  lexical matches plus evidence referenced by selected entities. Must work when
  no graph entities matched, so long-document facts remain searchable.
- `validate_evidence_ids(ids)` -> current chunk maps for exact supplied IDs.

Ranking should prefer lexical relevance over merely linked chunks. Query inputs
are parameters; never enumerate unbounded source text into application memory.
Use graph mutation lock for consistent multi-step reads where needed. Current
state validation occurs again after answer generation to reject deletion/refresh
races. All new tests use disposable databases; no production/paid APIs.

## Cited answers / retrieval agent ownership

Keep `answer_query` compatibility; add
`answer_with_citations(question, evidence, model=None)` -> dict containing
`answer`, `citations` (IDs), `insufficient_evidence` bool, `conflicts` (list of
{description,evidence_ids}), optional sanitized `error`. Use JSON mode, no
temperature. Prompt uses only original retrieved excerpts for factual support,
with explicit untrusted-data fencing and instructions to expose disagreements.

GraphRAG may retain graph context as diagnostics, but final cited generation uses
at most 3 chunks /24,000 text chars. Validate every ID against those actually sent,
types, length/count bounds, nonempty citations for answered results, and current
source versions after generation. Any invented/stale/malformed citations discard
the generated answer and return insufficient_evidence. Conflicts need >=2 distinct
valid evidence IDs. Existence validation is not entailment verification; do not
invent confidence/support scores. Return citations as bounded metadata objects
{id,source_id,version,title,uri,start,end}, conflicts with IDs, existing source_docs/
context_nodes/sources and optional retrieved evidence diagnostics. Missing provider
returns llm_unavailable; no evidence returns insufficient_evidence (or no_context
when nothing matched anywhere). Do not let legacy graph summaries masquerade as
original source excerpts. Preserve offline compatibility where explicitly tested.

## Integrator ownership

Add authenticated-if-configured GET /api/evidence/{id} and
GET /api/sources/{id}/evidence; use API key + graph deps and bounded responses.
UI displays citation buttons, source/version/offset drawer, coverage/limits,
insufficient-evidence state, and conflicts. Escapes all values; clicks refetch
current evidence (404 stale), no generated URLs or injected HTML. Saved history
remains its existing schema and must tell users to rerun to validate evidence.
Original upload bytes remain unretained; disclose persisted extracted excerpts.

## File ownership / gates

- Ingestion: extractors, chunks.py, sources.py, graph_writer.py, core.py and owned
  ingestion tests. Coordinate stable chunk_id helper with graph agent.
- Graph: neo4j_client.py and new M2 graph/integration tests.
- Retrieval: entity_parser.py, graphrag.py, citations.py and answer/retrieval tests.
- Integrator: routes/UI/history presentation, quality fixtures, docs and harnesses.
- Independent code/security reviewers check final integrated commit.

Acceptance: fact after char50000 retrieved; exact excerpt offset roundtrip; failed
chunk rollback; deletion/refresh stale citation 404; citation fabrication rejected;
conflicts shown; malicious document instructions remain data; bounded calls/memory;
curated retrieval/citation evaluation with limitations published. Set fixture
expectations before implementation; no paid LLM judges or claimed live quality.
