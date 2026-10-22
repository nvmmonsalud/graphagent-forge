# Parallel milestone delivery

Baseline: `1713286` (2026-10-21). These are bounded implementation slices,
not claims that the full M0, M3 or M6 milestones are complete.

| Branch | Owned area | Acceptance |
|---|---|---|
| `milestone/m6-end-to-end` | Browser tests and M6 coverage documentation | Real UI interactions with synthetic HTTP/WebSocket responses: ingestion progress/recovery, citations, provider failures and merge recovery |
| `milestone/m0-security-ci` | CI workflows, security config, guarded integration runner | Pinned actions, least privilege, dependency/secret/SAST checks and disposable Neo4j integration job |
| `milestone/m3-usage-controls` | Usage store, provider hooks, route/resource lifecycle, usage tests/config | Opt-in atomic durable Ask/sandbox request quota; reported Ask token accounting, workspace isolation, reopen/concurrency/failure regressions |
| `milestone/research-evaluation` | Synthetic corpus, metric helpers, evaluation tests/docs | Predeclared relevant sources, offline metric/orchestration checks and opt-in actual Neo4j retrieval measurements |

## Frozen boundaries

Existing API response envelopes, provenance, graph schema and private access
checks remain compatible. M3 adds `/api/usage`, aggregate metadata only, with
`USAGE_REQUEST_LIMIT=0` disabled by default and `USAGE_WINDOW_SECONDS=3600`.
Quota reservations precede work and are not refunded. Token counters distinguish
unreported usage from reported zero; they cover Ask calls, not all ingestion.
Dollar ceilings, new provider calls and multiple API workers are outside scope.

No branch calls paid APIs, creates remote sandboxes, reads private documents or
modifies an existing database to validate its changes. Tests use invented public
fixtures. Browser stubs establish frontend behavior, not backend authentication
or live-provider correctness. Integration CI must create its own database and
refuse external credentials/destinations. Relevant sources are declared before
running the expanded corpus; record its first measured baseline before agreeing
future quality thresholds. Measurements are never inferred from mocked candidates.

## Integration and review

Each implementer commits only its worktree. Integrate into a separate local
branch in this order: M0, evaluation, M3, M6. Review the resulting commit for
correctness and security independently, repair findings, then run Ruff, offline
pytest, browser regressions and available disposable-database checks. Preserve
the original `main` checkout and existing worktrees. Remote push, PR creation,
main merge and deployment remain separate publication steps.

## Remaining milestone scope

The next execution contract is [H1 — private hosted staging](HOSTED_BACKEND_CONTRACT.md).
It freezes one persistent host, a same-origin private UI/API/WebSocket deployment,
and provider-free connected acceptance. The public Vercel synthetic preview stays
separate. Start the local acceptance harness first; bind host/domain/budget/recovery
inputs before remote provisioning. H1 is specified, not deployed or accepted.
The [slice A harness](H1_LOCAL_ACCEPTANCE.md) provides opt-in local connected
acceptance; its evidence does not close the hosted deployment gates.

M0 still needs fully reproducible dependency resolution and broader operational
telemetry. M3 still needs full provider accounting, monetary reservations/spend
ceilings and shared-worker event/rate delivery. M6 still needs browser acceptance
against the running backend. Human-reviewed real-provider claim support and
representative user documents remain separate evaluation gates.
