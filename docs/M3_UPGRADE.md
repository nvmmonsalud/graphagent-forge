# M3 durable jobs and research history

This delivery adds the durable workflow foundation of M3. Provider token usage,
dollar spend ceilings, shared quotas for ask/sandbox routes and multi-process
WebSocket delivery remain follow-up work; the full M3 roadmap remains in progress.

## Enable and deploy

Unset `STATE_DB_PATH` preserves the existing in-memory behavior. To retain jobs
and query history across restarts, set `STATE_DB_PATH=data/state.sqlite3` before
starting the app. Missing directories are created privately. An existing parent
must be owned by the application user and mode0700; database/sidecars are0600.
Symlink paths and linked database files are rejected. A bad/incompatible database
aborts startup rather than silently reverting to memory. See [M3_ADR.md](M3_ADR.md).

For Compose, set the same relative path; `/app/data` is an app-owned directory
backed by the `app-state` named volume. Keep one app/API process on one host and
local disk. Do not use NFS or scale API workers: rate counters and graph WebSocket
fanout are still local. Configured API keys protect durable job/history reads and
new event/cancel endpoints; existing graph reads and WebSocket metadata retain
their prior exposure. This is not private workspace authorization (M4).

Existing memory jobs/history cannot be recovered retroactively and are not
migrated. Source versions/evidence remain in Neo4j. Reuse the SAME SQLite database
and Neo4j graph after restart; restoring either independently can invalidate
recovery assumptions. Stop app workers before coordinated backups/upgrades.
Back up SQLite with its online backup API or while all connections are closed;
copying just the main file while WAL writes are active is unsafe. Schema1 is
validated at startup; no automatic downgrade is provided. Rollback requires a
coordinated app/state/graph backup. No paid backfill runs on startup, but enabling
a database with queued jobs resumes their configured provider work.

## Lifecycle, replay and cancellation

Ingest endpoints retain their202 job_id/status and wait=true behavior. Durable
mode additionally accepts `Idempotency-Key` (1–128 printable characters). Same
key+same complete command returns the retained job, including terminal jobs;
changed content with the same key returns409. This only applies while that job
is retained; use a new key for an intentional new submission.

Durable commands are inert, versioned JSON for URL/text/file only. Invalid inputs,
ordinary exceptions, unsuccessful extraction and source conflicts fail without
blind retry. Explicit internal retryable errors use persisted exponential backoff
(1,2,4 seconds, capped at30 seconds) within the bounded retry policy.
Defaults: two shared running slots,20 active jobs,100 retained terminal jobs,
two attempts,600-second timeout and30-second leases. Commands expire after24h,
including budget-blocked jobs. `INGEST_ATTEMPT_BUDGET` defaults to100 attempts per
fixed UTC-hour window, shared transactionally by managers using the same file.
It includes recovery/retry attempts and never refunds possible provider work.
This caps attempt starts, not tokens, sandboxes or dollars. Exhaustion leaves jobs
queued with `waiting_budget`; changing stored worker policy requires an explicit
offline maintenance procedure, not a second manager with different settings.

GET `/api/jobs/{id}/events?after=0&limit=100` replays ordered event metadata;
events are bounded to200 per job. POST `/api/jobs/{id}/cancel` requests cancellation
without requiring a reachable graph. Queued jobs cancel immediately; running jobs
retain their execution slot while cancellation cleanup completes. A graph commit
that wins the cancellation race remains published: cancellation is not rollback.
The primary UI resumes the most recent active retained job after page reload,
polls progress and offers cancellation for durable jobs.

Shutdown cancels/awaits owned work before releasing its lease, preserving queued
commands. A dead worker's expired lease can be claimed again within the attempt
budget. Progress and final status require the current lease token. There is no
exactly-once provider execution or billing guarantee. Post-publication embeddings
and verification may not replay if the process crashed after graph publication.

A stable job ID is the publication operation ID. Neo4j stores a receipt in the
SAME transaction as source publication, and checks it before later replay. Thus
crash-after-publish recovery skips extraction/publication even if another job
has since refreshed/deleted the source. Receipts survive clear/delete, contain
no excerpts and expire after seven days (longer than the24h command horizon).
Historical receipt results describe that original operation; they do not prove
the source/version is still present. Requery current evidence for that purpose.

## Retention and privacy

Durable mode persists raw text and base64 upload bytes privately while work is
queued/running. Public job params/results/events omit these command payloads.
Terminal success/failure/cancellation removes the command; terminal eviction also
removes that job's event/key association. History uses a50-entry rolling limit with
up to20 pinned entries exempt from eviction, with the existing nine-field public schema and4000-character
answer cap. Saved history survives restart; deleting it removes the stored row.
History is historical: rerun questions to validate current source evidence.

Logical deletion is not secure erasure of WAL, free pages, filesystems or backups.
Protect state volumes/backups at rest and follow your own retention policy.
Neo4j receipts have independent seven-day metadata retention. Excerpts already
published remain governed by source deletion, separately from job cancellation.

## Validation scope

Tests use real temporary SQLite files with reopen, competing managers, expired
leases, idempotency conflicts, shared bounds/budgets, cancellation cleanup and
payload pruning. Disposable Neo4j tests include a crash between graph publication
and SQLite terminal recording, intervening source refresh, then reopened-state
recovery with paid extraction forbidden. Offline provider mocks do not measure
live-provider reliability, spend or multi-host throughput.
