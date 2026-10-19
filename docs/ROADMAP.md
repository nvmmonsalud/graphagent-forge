# Feature roadmap

Prepared 2026-10-19 against `3588b91`. These are proposed repository milestones;
they are not GitHub milestone objects, delivery promises, or implemented features.
Assumption: evolve the hackathon app into a trustworthy research workspace while
retaining a simple local mode. No user interviews or market-demand validation were
performed; prioritization is engineering judgment informed by code and research.

Start with source integrity and inspectable answers. More autonomous research or
more connectors will amplify existing provenance/retrieval problems if built first.
See [the independent review](CODEBASE_REVIEW.md) for evidence and
[the execution playbook](MULTI_AGENT_WORKFLOW.md) for agent roles and review gates.

## Milestones and dependencies

| Milestone | User outcome | Dependency | Relative effort |
|---|---|---|---|
| M0 — Reliable execution and quality baseline | Failures leave no leaked workers; regressions are measurable | None | Small–medium |
| M1 — Stable sources and correct retrieval | Documents stay distinct; answers use the intended entities | M0 baseline | Medium |
| M2 — Evidence-backed document answers | Click a citation to inspect its exact supporting excerpt | M1 | Large |
| M3 — Durable research workflows | Jobs and saved research survive restarts and recover safely | M1; M0 process cleanup | Large |
| M4 — Private workspaces | Share research with authenticated readers/editors | M1 contracts; M3 before multi-worker rollout | Large |
| M5 — Scalable research and discovery | Bulk refresh, ranked search, saved investigations | M2/M3; M4 for hosted/private use | Medium–large per slice |

Effort is qualitative, not a schedule; size each issue after its schema/API contract
is agreed. M4 design and M0 testing can run alongside other milestones. No shared
or private hosted release should precede M4, even if M2/M3 are already complete.

## M0 — Reliable execution and quality baseline

Delivery status: in progress; lifecycle slice implemented. R5 local child cleanup
and remote sandbox ownership during cancellation have process-lifecycle and mocked
SDK regressions. Validation: 1,484 offline tests passed, 14 integration tests
deselected, Ruff passed; independent code and security reviews passed. Remote
provider behavior was mocked; a hanging SDK acquisition/deletion can still delay
cancellation, and provider deletion failures remain logged best effort. The
evaluation corpus, expanded CI, telemetry, and dependency/security checks below
remain separate work items.

Scope:

- Fix R5: deterministic local child cleanup on cancellation, timeout, and errors;
  verify remote sandbox deletion on those paths too.
- Add a small labeled retrieval corpus: isolated nodes, ambiguous labels, duplicate
  titles, conflicting sources, mixed embedding methods, and unsupported questions.
- Add disposable-Neo4j integration CI; include `scripts/` in lint. Add bounded
  browser smoke tests for ingest progress, auth prompts, graph merge, and citations
  when introduced. Keep static contract tests for their current purpose.
- Record stage latency, provider/method, token usage when supplied, and failure
  counts without logging document bodies, secrets, or full prompts by default.
- Resolve dependency versions reproducibly and add dependency audit, secret scan,
  and SAST checks with explicit CI permissions and pinned action revisions.

Exit criteria: child termination is awaited before releasing a permit; timeout and
shutdown regressions pass; evaluation fixtures and baseline results are committed;
integration tests cannot target a non-disposable database; both reviewers approve.
Tools that need new dependencies/services get their own reviewed setup changes.

## M1 — Stable sources and correct retrieval

Delivery status: implemented. See [M1_UPGRADE.md](M1_UPGRADE.md)
for legacy metadata migration, rollback, re-embedding, compatibility and limits.
Validation: 1,522 offline tests and all 31 disposable Neo4j 5.26 integration tests
passed, plus Ruff and Chromium source-title/refetch/unchanged-state checks.
Independent code/security reviewers approved the final diff, with exact-commit
confirmation required before push. Provider behavior used mocks; no paid APIs ran.

Scope:

- Fix R1/R2: retrieve by entity ID, always include center summaries, and carry
  source/relationship provenance through traversal. Preserve alias matching in
  candidate search without turning a selected ID back into an ambiguous label.
- Fix R3: add `Source` records with immutable IDs, display titles, canonical URL
  or upload identity, content hashes, and extraction/version metadata. Define URL
  canonicalization conservatively; do not remove query parameters blindly.
- Plan an explicit migration from `source_docs` strings, including merged nodes,
  edge provenance, seed fixtures, deletion, export, and backward-compatible APIs.
  Preserve old display labels and back up before applying migrations.
- Fix R4: each embedding result carries vector, method, model, dimension, and
  version; only compatible semantic vectors enter semantic search. Reindex with
  an explicit migration rather than filtering an already polluted top-k silently.
- Make unchanged-source ingestion idempotent; define atomic version publication
  so failed or concurrent refreshes cannot replace the current graph partially.

Exit criteria: equal-title URLs remain separate; isolated-node evidence reaches
the prompt; equal-label entities do not mix unless explicitly selected/merged;
traversed evidence has complete provenance; mixed embedding batches retain their
own metadata; source deletion preserves other documents' merged entities. Migration
and rollback pass on a disposable database with legacy data, not just an empty DB.

## M2 — Evidence-backed document answers

Delivery status: bounded implementation delivered; release validation and
independent code/security review results are recorded in the M2 PR. See
[M2_UPGRADE.md](M2_UPGRADE.md) for migration, storage/privacy and concrete limits.
The bounded policy captures up to 200,000 normalized characters and processes
at most the first 91,600 through 12 overlapping chunks; PDF locations use text
offsets rather than page references. Legacy graphs require deliberate reingestion
for original excerpts; startup does not run paid extraction or a text backfill.

Scope:

- Store bounded chunks and source-version lineage with text offsets or PDF page
  references. Preserve original extracted evidence separately from LLM summaries.
- Replace clipping-only ingestion with budgeted chunk processing; expose completed,
  skipped, and failed coverage. Bound total chunks, bytes, tokens, and concurrency.
- Link extracted assertions to evidence; return structured citations referencing
  only evidence actually retrieved. Server-side validation rejects nonexistent
  IDs. Citation validity is not proof that a claim is supported: test support too.
- Add an evidence drawer, source/version preview, explicit insufficient-evidence
  state, and visible conflicting assertions rather than silently reconciling them.

Exit criteria: a fact after character 50,000 can be found within configured limits;
each displayed citation resolves to retrieved evidence in the current source/access
scope (cross-principal workspace enforcement is added in M4);
normalized-text offset references survive rendering; deletion/version replacement invalidates
stale citations; malicious instructions in documents stay untrusted data. Measure
retrieval recall@k and citation validity on the fixed corpus; assess claim support
with human-reviewed samples. Any numeric release threshold must be set from the
baseline before implementation, not invented after seeing the result.

Delivered evaluation: real Neo4j source recall@3 is 1.000 on five predeclared
synthetic questions (six expected source hits across eight sources). This is a
small contract fixture with distractors, not a general precision or answer-quality
result. Real-provider human claim-support evaluation remains follow-up work.

## M3 — Durable research workflows

Scope:

- Introduce repository interfaces and serializable job commands instead of trying
  to persist Python closures. Keep a local in-memory adapter for offline tests.
- Choose one durable application-state store in a short architecture decision
  (e.g. SQLite for a single-node first release, PostgreSQL for shared operation);
  do not add a broker/cluster until deployment requirements justify it.
- Persist jobs, query history, source versions, and events; add worker leases,
  bounded retry/backoff, idempotency keys, cancellation, and recovery states.
- Use durable stage checkpoints and atomic version publication. At-least-once
  delivery requires idempotent effects; do not promise exactly-once processing.
- Add shared event delivery and rate/cost budgets before supporting many API
  workers. Present token/sandbox usage and enforce configured spend ceilings.

Exit criteria: restart retains saved answers and queued/terminal jobs; a crashed
worker's lease can recover without duplicate graph publication; permanent input
errors are not retried; cancellation leaves no children or partial published
version; two-worker tests share progress/quotas correctly; retention and deletion
remove stored payloads consistently. Preserve current public response envelopes.

## M4 — Private workspaces

Scope:

- Authenticate principals and WebSocket subscriptions; authorize graph reads,
  exports, sources, jobs, history, evidence, and writes. Separate reader/editor
  roles and enforce workspace membership before retrieval, not after generation.
- Choose an identity/session design before implementation. Prefer scoped secure
  sessions or short-lived WebSocket tickets over tokens in URLs; review CSRF,
  CORS, expiry, revocation, and secret storage together. Retain explicit local mode.
- Scope graph IDs, queries, caches, durable events, and broadcasts; add audit
  events and workspace retention/deletion. Cache keys include authorization scope.
- Clear browser-stored credentials on logout/account changes; segregate or clear
  cached query history on workspace changes. Test shared-browser account switching.
- Show external provider destinations and data handling clearly for private
  documents; redact secrets from diagnostics. Prompt fencing is defense in depth,
  not access control.

Exit criteria: two-principal adversarial tests prove HTTP and WebSocket isolation,
including guessed IDs, exports, indirect traversal, caches, reconnect/replay, and
membership revocation; readers cannot mutate; unauthorized data is never sent to
the LLM. Security reviewer approval is a release requirement for private hosting.

## M5 — Scalable research and discovery

Deliver independently after foundational gates:

1. **Ranked hybrid retrieval:** indexed lexical search plus semantic candidates,
   rank fusion, and bounded graph expansion; expose evidence/ranking diagnostics.
   Benchmark against the M0 corpus before adding a reranker. Stay compatible with
   the repository's Neo4j 5.26; newer Cypher examples may require newer servers.
2. **Bulk import and source refresh:** bounded URL/upload manifests, unchanged
   content detection, refresh schedules, freshness/diff views, and cancellation.
   Reapply SSRF/redirect validation on every refresh and preserve version lineage.
3. **Investigation workspace:** saved graph views, selected-entity comparison,
   filtered neighborhoods, annotated evidence, and source-specific Q&A. Add cursor
   browsing/progressive graph expansion before attempting huge force layouts.
4. **Optional global questions:** community summaries and corpus-wide synthesis
   only if the evaluation set demonstrates demand beyond entity-local questions.
   Summaries retain evidence IDs, freshness, access scope, and measured cost.

Exit criteria per slice: demonstrable user flow, version-compatible integration
tests, bounded CPU/token/memory use, security review, and a measured quality or
workflow improvement. Treat unsupported questions, stale sources, and provider
outages as explicit outcomes. Do not equate more agents with better answers.

## Research behind these choices

Sources checked 2026-10-19. Recommendations above are project-specific inferences;
these sources support the design options, not claims of measured improvement here.

- [Neo4j GraphRAG retrieval guide](https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_rag.html):
  vector, hybrid, and traversal retrievers provide reference implementations. Adopt
  the retrieval pattern selectively; a full framework migration is unnecessary.
- [Neo4j hybrid-search guide](https://neo4j.com/developer/genai-ecosystem/hybrid-search/):
  rank fusion is an available approach; its newest examples need a newer Neo4j/
  Cypher version, so use compatible procedures or an explicit upgrade plan.
- [Microsoft GraphRAG query overview](https://microsoft.github.io/graphrag/query/overview/):
  local entity retrieval and global community-summary queries solve different
  needs. Global synthesis is a later capability, not a prerequisite for citations.
- [FastAPI WebSockets](https://fastapi.tiangolo.com/advanced/websockets/) and
  [background tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/):
  dependency-based WebSocket authentication and external workers are supported
  patterns; process-local state cannot become durable simply by adding workers.
- [Celery task guidance](https://docs.celeryq.dev/en/stable/userguide/tasks.html):
  retry/acknowledgement choices depend on idempotency. Celery is an option, not a
  required dependency for the first durable implementation.
- [OWASP prompt injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) and
  [sensitive information disclosure](https://genai.owasp.org/llmrisk/llm022025-sensitive-information-disclosure/):
  untrusted retrieved content and sensitive context need explicit boundaries;
  prompt text alone cannot enforce data authorization.
- [Ragas metrics](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/):
  context quality and answer faithfulness are distinct evaluation targets. Start
  with deterministic retrieval/citation fixtures; LLM judges are optional and
  require a separately configured budget and human spot checks.
- [Playwright Python CI](https://playwright.dev/python/docs/ci-intro): real browser
  interaction/trace tests complement the existing source-text UI checks.
- [OpenTelemetry GenAI conventions](https://github.com/open-telemetry/semantic-conventions-genai):
  use a pinned convention version for provider/token telemetry; avoid assuming
  evolving attribute names are stable and keep content capture opt-in.
