# M2: inspectable source excerpts and cited answers

M2 builds on the M1 source/version contract and delivers bounded excerpt
ingestion, current-evidence lookup and cited answers. The release PR records
validation and independent code/security reviews. M1 remains a prerequisite.

## Extraction and coverage limits

The agent requests up to 200,000 characters of normalized extracted text. URL
fetches and uploads retain the existing 3 MB byte caps, SSRF checks, file format
checks and resource-limited PDF parsing. Extractor helpers retain their historical
50,000-character default for callers that omit `max_chars`; the agent explicitly
uses the new limit. The M2 path never appends a synthetic truncation marker to
stored source text.

A source produces at most 12 chunks of 8,000 characters with 400-character
overlap. Extraction runs at most two calls concurrently per document, with the
existing per-call model output budget. Consequently the current policy processes
only the first **91,600 normalized characters**, even when 200,000 were captured.
Facts later than that window are outside this release's evidence coverage. M2
can retrieve facts beyond the old 50,000-character limit, but does not offer
complete arbitrary-length document ingestion.

Source/job coverage reports `captured_chars`, `processed_chars`, `skipped_chars`,
`planned_chunks`, `completed_chunks`, `failed_chunks`, `input_truncated`, `partial`
and `graph_capped`. Processed characters count overlap once; graph caps of 500
nodes and 1,000 edges are separate from text coverage. A captured 200,000-character
source normally reports 91,600 processed and 108,400 skipped characters. Input
truncation means the actual original length may be unknown. Planned extraction
failures abort publication; successful chunks never replace the previous source
version partially. Failed attempts may report completed extraction work, but none
of those excerpts are published.

Offsets are Unicode character offsets in the normalized extracted text, with an
exclusive `end`: `text[start:end]` is the excerpt and `end-start` is its length.
They are not offsets into uploaded bytes or original HTML. PDF extraction uses
text offsets across pages; no page number or OCR guarantee is made. Chunk IDs
bind source ID, source version, ordinal and exact excerpt text.

## Existing graphs and upgrade

Stop old application workers before upgrading. Follow the deployment's database
backup procedure first; retain the M1 source metadata migration where necessary.
Normal schema initialization adds evidence storage/index support. There is no
automatic excerpt backfill: the existing graph and generated summaries cannot
reconstruct original source text.

Deliberately reingest each original URL, named text source or upload that needs
citations. This can call the configured extraction/embedding providers and incur
costs. Reuse the same M1 source identity to replace it; different filenames/names
or URL identities create separate sources. The extraction version now includes
the M2 schema and chunk policy, so the first reingestion re-extracts even when
captured text is unchanged from M1. Subsequent identical versions skip provider
extraction. No bulk migration or paid backfill runs on application startup.

Publication atomically replaces the source's graph and excerpts. Deletion, clear,
refresh and legacy direct source writes must invalidate obsolete excerpts. A
citation opened after refresh/deletion may return 404; rerun the question to get
current evidence. Reverting application code alone does not undo the new data
model; restore a coordinated application/database snapshot when rollback needs
the old state. Running old and new writers together is unsupported.

## Citation semantics and privacy

Generated factual answers use at most three original excerpts (24,000 text
characters). Citations must name excerpts actually supplied to the model and
still current after generation. Invalid, stale or fabricated IDs discard the
answer and produce insufficient evidence. Conflicts reference at least two valid
excerpts. These checks verify existence and version consistency; they do not
prove that every claim is entailed by its citation or assign confidence scores.
Legacy graph summaries remain useful diagnostics but cannot masquerade as source
excerpts. Saved query history is still in memory and should be rerun to validate
current evidence.

M2 persists extracted excerpts in Neo4j. Original upload bytes are not retained,
but document text in those excerpts can contain sensitive information. Existing
API-key settings protect the new evidence endpoints when configured; M2 does not
provide tenant isolation or close all existing public-read/WebSocket access.
M4 remains required for a private hosted workspace. Provider extraction receives
the planned chunks, and answer generation receives selected excerpts. Document
instructions are treated as untrusted data; no document can grant tools or
permission to execute code.

## Verification

Offline extraction tests use mocked providers and cover exact Unicode offsets,
late-window facts, bounded calls/concurrency, empty entity graphs, graph caps with
endpoint closure, malformed output, failed-chunk publication rollback and
unchanged-version skipping. Database tests must use an explicitly disposable
Neo4j database; never run destructive integration checks against production.

Curated retrieval/citation evaluation uses fixed expectations and mocked answer
responses. No paid LLM judge or live provider evaluation is implied. Passing
those tests establishes the tested contracts, not general answer accuracy.


The disposable Neo4j evaluation declares eight synthetic sources and five questions
before querying real `search_evidence` ranking (no mocked candidates). All six
expected source hits appear among the top three chunks: fixture source recall@3
is 1.000. Cases include a fact after character 50,000, Japanese text and opposing
source assertions. Some distractors also rank, so this is a recall measurement,
not a precision, entailment or general quality score. Six additional offline
fixtures test citation orchestration with mocked provider responses. Human review
of real-provider claim support remains a follow-up before an answer-quality claim.

To run all destructive integration checks against a disposable Neo4j 5.26, set
`NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `M1_DISPOSABLE_NEO4J=1` and
`M2_DISPOSABLE_NEO4J=1`, then run `python -m pytest -m integration`.
