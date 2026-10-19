# Codebase review — 2026-10-19

Reviewed baseline: `3588b91` on `security/low-severity`. This is a scoped engineering
review, not a penetration test or production certification. Three independent
agents reviewed code/retrieval, security, and product capabilities; the coordinator
checked their conclusions against the implementation and researched primary sources.

Follow-up: R5 was addressed in the M0 lifecycle slice; R1–R4 are addressed by M1.
The findings below remain as evidence of the reviewed baseline. See
[M1_UPGRADE.md](M1_UPGRADE.md) for delivered behavior, migration and limits.

The baseline's most recent validation was 1,462 passing offline tests, 14 integration
tests deselected, and Ruff passing. That result predates these documentation files.
Tests do not establish retrieval quality or security by themselves. No live Kimi,
Nosana, Daytona, or production database testing was performed for this review. The
security reviewer reproduced process cancellation locally and cleaned up its child.
The frontend was inspected as source, not exercised in a browser.

## What is already built

URL/text/PDF/TXT/MD ingestion; bounded asynchronous ingestion jobs and progress;
graph visualization, search, paths, source filtering/deletion, JSON/CSV export;
duplicate suggestions and explicit merges; graph analytics; verification/fan-out/
sweep; query history with saved answers; degraded provider behavior.

Strengths include atomic graph writes, offline tests, sanitized errors, SSRF/DNS/
redirect guards, upload validation, and explicit fallback/result envelopes. Build on
these contracts rather than replacing the app with a new framework.

## Findings to address before expanding scope

All line references below refer to the reviewed baseline.

| ID | Priority | Verified behavior and consequence | Evidence | Suggested fix |
|---|---|---|---|---|
| R1 | P2 | A one-or-more-hop path excludes isolated entities; selected `center_summary` is never formatted. Relevant entity evidence can be absent from the answer prompt. | `src/graph/neo4j_client.py:266–297` | Always include the chosen entity's summary; expand neighbors optionally. |
| R2 | P2 | Retrieval converts candidate IDs into labels, then expands every matching label. Unmerged same-label entities can mix; reported sources cover seed candidates, not all traversed evidence. | `src/graph/graphrag.py:64–86`, `src/graph/neo4j_client.py:275–294` | Expand by immutable entity ID and return structured evidence/provenance for every included assertion. |
| R3 | P2 | URL source identity is the page title; node prefixes derive from that identity. Different URLs with equal titles and equal extracted IDs target the same stored node. | `src/agent/core.py:105,116`, `src/ingestion/graph_writer.py:114–132`, `src/graph/neo4j_client.py:226` | Separate source IDs from titles; migrate provenance and use an explicit reingestion/version policy. |
| R4 | P2 | A concurrent embedding batch uses one mutable `last_embedding_method`; hash and semantic vectors share the indexed property, without semantic filtering in vector search. Mixed/degraded ingestion can mislabel vectors and contaminate later semantic results. | `src/agent/core.py:254–280`, `src/graph/neo4j_client.py:128–141,313–324,647–663` | Return metadata per embedding; separate compatible semantic vectors from fallback data and rebuild/backfill the index. |
| R5 | P2 | Cancelling local execution bypasses `except Exception`, leaving the subprocess alive while the semaphore permit is released. Timeout calls `kill()` without awaiting termination. | `src/agent/daytona_exec.py:218–245`, `src/agent/jobs.py:320–325` | Kill and reap children on cancellation/timeout/error; retain capacity until cleanup finishes, then propagate cancellation. |

These are new review findings, not claims that the earlier DNS/WebSocket fixes failed.
Suggested deterministic regressions: isolated-node answer context; two distinct
same-label entities; two equal-title URLs; mixed semantic/hash batches; actual child
termination after cancellation and timeout.

## Deployment and maintenance limits

- **Public reads are intentional today.** `API_KEY` protects writes and spend, not
  graph reads/export, `/jobs`, `/history`, or WebSocket subscriptions. See
  `src/api/routes.py:471–483,908–917` and `src/main.py:392` onward. Origin validation
  does not authenticate clients. Private/team use requires authorization on all
  these surfaces; a shared write key must not be described as privacy protection.
- Jobs/history/rate limits/broadcasts are process-local. Restart loses state;
  multiple workers do not share the queue, history, events, or quotas.
- Ingested text is clipped at 50,000 characters. A fact later in a long document
  cannot be retrieved from the current extraction. Add coverage metadata and
  bounded chunks before advertising complete-document understanding.
- Keyword search uses `CONTAINS`, not a Neo4j full-text index. Candidates are
  appended, not fused into a calibrated hybrid ranking. Measure before changing it.
- CI runs lint and offline pytest. It lacks live disposable-Neo4j integration,
  browser interaction checks, retrieval evaluation, and dependency/security scans.
  Minimum dependency versions are not reproducible resolved dependency locks.
- The large inline frontend and shared `routes.py`/`neo4j_client.py` are editing
  contention points. Give them explicit ownership during parallel work.

## Review disposition

Prioritize R5 and retrieval integrity (R1–R4). Keep the app local/demo-scoped until
private-read authorization exists. The roadmap assigns these fixes to M0/M1 and
defines later features as proposals, not implemented capabilities. No application
code was changed by this planning review. See [ROADMAP.md](ROADMAP.md) and
[MULTI_AGENT_WORKFLOW.md](MULTI_AGENT_WORKFLOW.md).
