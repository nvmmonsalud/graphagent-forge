# M3 usage controls: first slice

## Scope and contract

`USAGE_REQUEST_LIMIT` (default `0`, disabled; maximum 1,000,000) caps the
combined number of Ask and sandbox HTTP requests per resource in a fixed UTC
window. `USAGE_WINDOW_SECONDS` defaults to 3600 (60–86400). Invalid settings
fail startup. Existing per-IP rate limits remain additional guards.

Reserve one request after authentication and graph availability checks, before
provider/CPU work. Ask and verify/fanout/sweep/analytics share the same counter;
a sweep costs one request regardless of sandbox count. Failed work is not
refunded. Exhaustion returns 429; unavailable quota state returns 503. This is
request admission control, not a token, sandbox-count, concurrency or money cap.

`GET /api/usage` uses configured-key/private membership authentication and returns
current-window numeric aggregates only. No prompt, answer, excerpt, URL, model
name, credentials or per-call ledger is stored. Reported prompt/completion tokens
are collected only within HTTP Ask execution. Missing/malformed provider usage
and provider exceptions increment a separate missing-usage counter; reported zero
is a measured zero. Tokens from truncated/invalid answers are still counted.
Ingestion, scheduled work, embeddings, direct helper calls, sandbox billing,
provider-internal retries and monetary charges are outside this slice.

## Storage and isolation

Memory is the default and resets on restart. With `STATE_DB_PATH`, one fixed
SQLite row is atomically updated under the existing state transaction policy;
reopening retains the current quota and totals. Private workspaces each own a
store in their existing workspace database. No new public response fields or
WebSocket events are added. Context-local provider recording prevents overlapping
Ask requests from mixing workspace totals and always resets after execution.

Usage recording is best effort after provider responses: recording failure is
logged without content and marks accounting unavailable for that resource until
restart. Crashes or cancellation between provider completion and recording can lose usage. No
exactly-once billing or complete spend accounting is promised. Single API process
hosting remains required; a shared SQLite quota alone does not implement shared
WebSockets, rate delivery, or hosted multiworker support.

## Upgrade and rollback

Back up state while stopped. Startup creates the additive `usage_window` table;
no graph migration or paid call is required. Use the same quota/window settings
for every manager sharing a database. Changing window duration can reset current
window accounting; deploy configuration changes while stopped. Disabling the
limit preserves recording. Rolling back code leaves the bounded table unused;
never remove the database containing jobs/history to reset a quota. Old windows
are overwritten, so this endpoint is not a historical reporting ledger.
