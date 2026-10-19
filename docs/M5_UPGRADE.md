# M5 research and discovery

M5 delivers ranked evidence search, bounded bulk/refresh workflows and saved
investigations on the existing M3/M4 storage and authorization boundaries. It
adds no reranker, paid evaluator or optional community/global synthesis. Use one
API process on local disk; the remaining M3 monetary/shared-worker scope is still
tracked separately. Private mode retains reader/editor server enforcement.

## Ranked evidence and source-specific questions

Neo4j5.26 schema initialization adds an English fulltext EvidenceChunk index.
Private index labels/names remain workspace-specific. Search constructs up to12
safe quoted terms (64 characters each), retrieves at most40 indexed lexical and
40 entity-linked candidates, and combines ranks with reciprocal rank fusion
(k=60). Returned scores/ranks describe retrieval order, not factual confidence.
Hash fallback vectors never become semantic matches. Answers retain the M2
three-excerpt/24,000-character limit and current-version citation checks.

When the index is unavailable, search reports fallback and scores a bounded
window of the1,000 newest eligible chunks. That fallback can miss older evidence;
inspect retrieval diagnostics rather than assuming it searched the whole corpus.
Fulltext index population may be asynchronous immediately after upgrade. No
reingestion or embedding backfill is required for existing EvidenceChunk text.

The Ask panel can select a source. POST `/api/ask` accepts optional `source_id`;
its source-specific path skips global semantic/context retrieval and filters
current excerpts BEFORE ranking/provider input. Missing/deleted/out-of-scope
sources produce explicit no-context/insufficient-evidence outcomes. Source scope
is additional to, and cannot expand, workspace membership.

The fixed English inflection fixture found2/4 expected sources with the old
substring predicate and4/4 with the index. It includes distractions and bounded
ranking; this is a small source-recall comparison, not a general quality,
precision, multilingual or factual-entailment guarantee. The prior current-source
and Japanese/Unicode fixtures remain covered. No live-provider judge was used.

## Bulk imports and refresh schedules

The Research panel accepts URL lines and named text; advanced API clients may
also include file commands. POST `/api/research/bulk` accepts `commands` with the
existing version1 URL/text/file JSON shapes. Limits are10 commands,4MB serialized
aggregate/request,200,000 text characters and3MB decoded file bytes in aggregate.
Every command validates before the first submission. Queue capacity can still
partially accept a manifest: inspect each item's accepted/rejected outcome. No
all-or-nothing bulk transaction is promised. Retain up to50 batch metadata records;
GET `/api/research/batches` shows status and POST `/batches/{id}/cancel` requests
cancellation. Private payloads remain only in the existing job command store.

`Idempotency-Key` protects each item through durable job keys; memory mode rejects
keys before submission instead of silently promising deduplication. Batch records
are status views, so repeated requests can have distinct batch IDs referencing
the same retained jobs. Idempotency only lasts while those jobs are retained.
Durable cancellation uses the existing cleanup/lease protocol. Memory cancellation
is cooperative and returns an explicit cancelled result while preserving workers.

URL refresh is opt-in: POST `/api/research/refreshes` with `url`,
`interval_seconds` (1hour–30days) and optional verified `source_id`. A workspace
may retain20 schedules. URL query values persist privately so the original URL
can be fetched again; list/job params use redacted display URIs. URL identity
still includes query bytes, and credentialed userinfo URLs remain forbidden.
The ordinary SSRF/DNS/redirect/byte checks run on EVERY fetch. Uploaded files need
an explicit reupload; file bytes are not retained for recurring schedules.

GET `/refreshes` shows next due time, last job/error and previous/current version
metadata. POST `/refreshes/{id}/run` requests an immediate run; DELETE removes the
schedule/private URL and cancels its pending job. Deleting a source alone does
not disable a schedule: delete the schedule too to prevent intentional later
reingestion. An already committed publication is not rolled back by cancellation.

Claims and completion use persisted generation/lease fences; expired/missed
intervals coalesce into one run, with no catchup burst. Active jobs, timeouts,
attempt budgets, revocation and source CAS remain enforced. Schedules retain
submitter identity and require current editor membership before submitting work.
In private mode, startup now opens each configured workspace with memberships
(max8), so persisted queues/schedules resume without waiting for browser access.
No schedule is enabled by default. Restarting opted-in state may call configured
providers. Local memory schedules disappear on restart; configure STATE_DB_PATH
for persistence. Failed providers/queues are reported explicitly; attempt budgets
are not token or dollar spend accounting.

The freshness display compares version fingerprints, not historical document
text. It cannot reconstruct a full text/assertion diff because source replacement
removes obsolete excerpts. Missing/evicted jobs yield unavailable metadata rather
than invented change counts.

## Saved investigations and progressive graph browsing

Use the Research panel to save/restore the current question, source, selected
entity IDs and evidence notes. Private workspace storage is shared with authorized
members; it is not a per-principal secret notebook. Up to50 views are stored,
with200-character titles,4,000-character queries, five selected entity IDs and
20 notes of2,000 characters each. With durable state they survive restart; memory
mode remains ephemeral. POST/PUT/DELETE `/api/research/investigations` require
editor, GET permits reader. Updates carry the current revision; stale edits
return409 rather than overwriting another edit.

Notes bind server-validated original evidence metadata, not copied excerpts.
Opening a view rechecks notes and marks deleted/replaced citations stale. Editing
a stale note preserves its original source/version/offset identity. Notes never
become answer evidence by themselves. Text is escaped in the UI; no HTML executes.
All new panels/inputs are cleared and old requests fenced on private auth/workspace
changes; no new browser localStorage cache is introduced.

POST `/api/research/compare` accepts up to five exact entity IDs and returns
bounded one-hop contexts plus missing IDs. It does not merge entities or infer
that same labels are the same entity. Readers may compare. GET `/api/research/browse`
uses source-bound opaque cursors, pages of at most100 nodes and300 edges, with
endpoint closure. Cursors are filters, not credentials; the workspace graph client
always enforces namespace. Only page-internal edges are returned; cross-page
relationships are not drawn in accumulated browsing. The primary UI caps accumulated
rendering at500 nodes; browse smaller/source-filtered views for further inspection.

## Operations and validation

New schedules/batches/views live in the same application/workspace SQLite file,
so existing backup and offline workspace purge procedures include them. Logical
deletion is not secure erasure of WAL/free pages/backups. Provider URL queries
and stored queries/notes can be sensitive; protect volumes and backups. Old app
workers must stop before upgrading; rollback requires coordinated app/state/graph
backup, not running old writers against new state. Read M3_UPGRADE and M4_UPGRADE.

Tests cover real SQLite reopen, competing schedule claims, expired-lease/stale
completion and delete-during-submit races, partial manifests, private API roles,
source filtering before provider calls, saved-note CAS/staleness, bounded cursors
and actual Neo4j fulltext/namespace/browse behavior. Chromium exercises all three
flows and private workspace reset. The PR records full-suite and independent
code/security review results. No paid APIs or production databases validate this
release.
