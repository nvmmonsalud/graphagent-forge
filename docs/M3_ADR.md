# ADR: local SQLite for durable application state

Decision: opt-in SQLite, alongside the existing memory adapters. Neo4j remains
authoritative for graph/source versions and atomic publication receipts. SQLite
stores ingest commands, lifecycle events, leases, attempt reservations and bounded
query history. A filesystem path is sufficient for the current single-host app;
no broker or second network database is introduced.

Each operation uses a short explicit SQLite transaction in a worker thread, with
WAL, foreign keys and a busy timeout. Provider work runs outside transactions.
Claims, capacity, idempotency checks and budget reservations are serialized with
BEGIN IMMEDIATE. Lease tokens fence progress and terminal state. A separate
Neo4j receipt commits with graph publication, resolving the crash gap between
graph success and SQLite completion; it does not make both databases atomic.

Supported deployment: one API process, local persistent disk, bounded workers.
Two-manager tests exercise coordination/recovery, but do not establish a supported
multi-API-worker deployment. Existing request-rate counters and WebSocket graph
fanout remain per process. NFS/shared network filesystems, multiple hosts, tenant
isolation and exact monetary accounting require separate designs.

Alternatives: process memory remains useful for demos/tests but loses state;
PostgreSQL is a future option when shared-host or hosted requirements justify it.
Redis/broker-only persistence would still require durable commands/history and
cross-store publication handling. Repository interfaces keep HTTP independent
of the chosen state backend without claiming an interchangeable cluster today.
