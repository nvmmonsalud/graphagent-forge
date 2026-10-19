# M3 durable workflow contract

Baseline main2679780. This delivery adds opt-in single-host durable workflows;
provider token/dollar accounting and multi-API-worker WebSocket/rate limits remain
tracked follow-up work. Do not claim a complete hosted/multi-tenant platform.

## State and compatibility

`STATE_DB_PATH` enables SQLite; unset preserves existing memory adapters exactly.
Only local filesystem storage is supported (not NFS). Use WAL, busy timeout,
foreign keys, explicit transactions, private directory/database permissions,
reject symlink paths, fail startup on unsupported schema/corrupt state; never
silently fall back and lose durable work. Blocking SQLite work runs in threads.
`src/agent/state.py`: `StateDatabase(path)`, async `initialize()`,
async `run(callback, *, write=False)`; callback gets sqlite3.Connection with
Row factory, returns plain result, runs in a short transaction. No external I/O
inside callbacks. Connections per operation, initialize application_id/user_version
checks. Each adapter initializes its tables idempotently through run.

SQLiteQueryHistory(db) in durable_history.py has async initialize, record, list,
get, set_saved, delete, counts. Retain QueryHistory nine-field public schema and
existing bounds/eviction semantics. History failures must not fail an answer.
Methods may be awaited through API helper supporting legacy synchronous adapters.

## Commands and jobs

`commands.py`: versioned JSON commands `{version:1,kind:'url'|'text'|'file',...}`;
validate exact fields/types, URL via existing guarded extractor at execution,
text<=200000, upload<=3MB decoded. File data base64 private; no pickle/functions.
`command_handler(agent)` returns async handler(command, progress, operation_id).
It calls agent ingest methods with operation_id=stable job ID. Unknown/malformed
commands are permanent failures. RetryableJobError is an explicit typed opt-in;
ordinary exceptions and unsuccessful results fail permanently, never blind retry.

DurableJobManager(db, handler, broadcast=None, workers=2,max_active=20,
history_limit=100,job_timeout=existing,lease_seconds=30,max_attempts=2,
attempt_budget=100,budget_window_seconds=3600). Async initialize/start/shutdown,
submit_command(kind, public_params, command, idempotency_key=None), get/list/wait,
cancel(job_id), events(job_id, after=0,limit=100). submit_command matches existing
public record/202 envelopes; get/list may be async and API adapts. Handler receives
private command; public params only existing URL/source/filename/length hints.
Private command JSON cap ~4.3MB, result cap bounded (<=1MB), no raw exception text.
Job public record keeps legacy fields; optional attempts metadata separately in
bounded event API, never command contents/keys/tokens. Key max128 printable chars,
canonical full command hash; same key+same command returns retained job, changed
command=>IdempotencyConflict=>409. Idempotency only within retained job lifetime.

SQLite atomically enforces active cap, claim with fresh token, shared running
permit (default2 across managers), attempt budget reservation per fixed window,
lease heartbeats and fenced progress/finalize transitions. Max attempts includes
lease-expired attempts; no paid-work refund. Budget exhausted work stays queued.
All active commands expire after24h; timeout<=600s by default. Stage/events persist
with monotonic IDs; bounded per-job events plus terminal history eviction cascades
payload/events/key associations. Terminal transitions immediately remove private
command. Cancellation: queued=>cancelled; running records request, owner cancels
and awaits task cleanup before terminal cancelled/releasing permit. Expired owner
cannot write progress/results; worker detecting lost lease cancels/awaits handler.
Shutdown preserves queued work and relinquishes owned running leases only AFTER
cancelling/awaiting their execution; recovery is bounded at-least-once.

## Publication recovery

Neo4j `:IngestOperation` receipt unique id, written inside SAME transaction as
source graph/excerpt publication (also unchanged publication). It binds stable
job ID to result source/version/counts and created time; `get_ingest_operation(id)`
returns minimal `{success:true,source,version,nodes,edges,unchanged:true}` or None.
Core/command preflight checks receipt BEFORE fetching/extracting on replay;
publish transaction rechecks under graph mutation lock and returns prior result
instead of publishing again, even after an intervening refresh/delete/clear.
Receipts survive source deletion/clear (no excerpts/text) and are bounded to a
7-day TTL; commands expire24h, so valid replay always fits receipt horizon.
No receipt on failed/conflicting extraction. Receipt id private/internal, not
exposed in public graph data. Publication receipt is authoritative checkpoint;
post-publication embedding/verification may not rerun after crash. No exactly-once
provider billing or atomic transaction across SQLite and Neo4j is promised.

## HTTP/lifecycle and UI

Use configured-key auth on new cancel/events APIs; durable-mode jobs/history GETs
also enforce configured key, keeping legacy memory read behavior compatible.
`Idempotency-Key` accepted by ingest endpoints in durable mode. Mutating cancel
has no graph dependency; event replay bounded. Existing wait=true preserved.
Start durable state/history/jobs after constructing agent; startup failure closes
created resources; drain jobs before Neo4j/HTTP clients. Health adds storage mode
only if compatibility tests permit; otherwise no new envelope required.
Primary UI recognizes cancelled terminal jobs, exposes cancel for active jobs,
uses polling to recover persisted progress and wording that applies to either
mode. History is historical; rerun required for current evidence. Explain file
bytes stored privately while queued/running in durable mode then removed at
terminal state; WAL/backups are not secure erasure. Compose optional named volume
and env example, gitignore local DB files. No secrets in docs or tests.

## Acceptance

Real on-disk reopen retains saved answers, queued/terminal jobs and events;
two managers race claims/capacity/budget/idempotency safely; crash expiry recovery
is bounded, stale tokens rejected; cancel awaits cleanup; unknown commands never
retry; terminal eviction/purge clears payloads/events. Simulate crash AFTER graph
publication BEFORE SQLite terminal result, refresh source, then replay old job:
receipt must prevent rollback/resurrection and paid extraction. Offline, disposable
Neo4j, browser and independent code/security exact-commit reviews required.
