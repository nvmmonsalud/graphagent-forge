# Multi-agent feature delivery

Use this with [AGENTS.md](../AGENTS.md) and [ROADMAP.md](ROADMAP.md). This playbook
does not create background automations or start feature implementation by itself.

## Team and ownership

Use one coordinator and up to four implementers, followed by two independent
reviewers. With seven concurrent slots, either run coordinator + four implementers
+ two reviewers, or reuse implementer slots for narrowly scoped subagents. Do not
spawn an unbounded hierarchy. Small fixes need one implementer and proportionate
review, not a full team.

| Role | Owns | Can delegate |
|---|---|---|
| Coordinator/integrator | Contracts, dependency order, shared API/lifespan integration, final checks | Bounded research or test tasks |
| Graph/provenance agent | `src/graph/neo4j_client.py`, schema/migrations, graph integration tests | Migration fixtures and compatibility checks |
| Retrieval/evidence agent | `src/graph/graphrag.py`, evaluation fixtures, retrieval contracts | Offline benchmark or citation validator tasks |
| Workflow/ingestion agent | Jobs/history/provider adapters and agreed ingestion files | One parser/store adapter after interfaces freeze |
| Frontend agent | `frontend/index.html`, optional Streamlit, browser/UI tests | Isolated accessibility or interaction verification |
| Independent code reviewer | Final diff plus affected callers, failure paths, tests/migrations | Focused compatibility review if needed |
| Independent security reviewer | Threat boundaries, authorization, SSRF, execution/data lifecycle, dependencies | Adversarial tests in disposable environments |

File ownership is per task, not permanent. `core.py`, `routes.py`, `main.py`, and
the frontend are common contention points: assign each to exactly one writer or
have agents propose patches for the integrator. Reviewers do not author the
implementation they approve. If a reviewer supplies a substantive fix, another
independent reviewer must review that changed scope.

## Delivery sequence

1. **Define a vertical slice.** Coordinator writes outcome, non-goals, schema/API/
   event examples, migration/rollback, dependency order, acceptance tests, and
   external-service/cost boundaries. Reviewers inspect the plan before coding.
2. **Freeze interfaces.** Agree source/entity/evidence IDs and request/response
   schemas first. Create serializable command/store interfaces before parallel
   queue and UI work. Retain existing degraded-mode and graph-boundary contracts.
3. **Assign bounded tasks.** Each agent gets baseline commit, exact writable files,
   read-only dependencies, acceptance cases, commands, and a clear completion
   format. Use separate Git worktrees for independent branches. Agents in a shared
   checkout must not switch branches, stage, or commit behind one another.
4. **Use subagents for independent pieces.** A parent can delegate migration
   fixtures, a parser adapter, adversarial cases, or benchmark research. Give them
   disjoint files and a bounded output; the parent checks their result. Never
   delegate unresolved shared schema decisions to competing writers.
5. **Integrate in dependency order.** Schema/contracts → backend behavior → API/
   events → frontend. Mock agreed interfaces to unblock parallel work, then run
   real integration checks after replacing mocks. Only the integrator commits the
   combined result in a shared checkout.
6. **Independent review of the final commit.** Code reviewer checks correctness,
   compatibility, migrations, async/resource behavior, tests, and unsupported
   conclusions. Security reviewer checks the checklist below. Findings contain
   severity, file/line, trigger, impact, and a regression expectation.
7. **Repair and re-review.** Implementers fix findings; reviewers recheck changed
   paths and callers. Unresolved critical/high findings block release. Lower
   findings need a concrete fix or a documented risk decision with an owner;
   agents cannot waive a finding merely because tests pass.
8. **Validate and ship within authorization.** Run focused regressions, Ruff over
   `src/ scripts/ tests/`, and offline pytest. Use a disposable Neo4j for graph/
   migration tests and a browser for changed interactions. Report skipped checks,
   migration constraints, and residual risks. Push/PR/merge only within the user's
   scope; attach any created PR to the Codex chat. Any post-review change needs
   proportionate re-review on the new commit.

## Milestone task splits

| Milestone | Useful parallel tasks | Required integration check |
|---|---|---|
| M0 | Lifecycle cleanup; offline quality corpus; CI/browser harness | Cancellation really reaps children; baseline tests reproducible |
| M1 | Source schema/migration; ID-based retrieval; embedding metadata | Legacy merged-source data survives migration and deletion |
| M2 | Chunk/evidence storage; extraction lineage; citation API/UI | Citation resolves from answer through assertion to source excerpt |
| M3 | Serializable commands/store; leases/retry; events/budgets UI | Crash/restart and two-worker recovery without duplicate publication |
| M4 | Principal/session contract; scoped Cypher/store adapters; scoped UI/WS | Two users cannot observe each other's graph, history, jobs, or events |
| M5 | One slice at a time: retrieval, refresh, investigations, global queries | Benchmark and adversarial tests cover the selected slice |

## Reviewer gates

Code review:

- Every new user-visible success/error state has a meaningful test; mocks do not
  conceal broken Cypher, migrations, transport, or concurrency behavior.
- IDs and provenance survive reingestion, merge, source deletion, and export.
  Graph responses include every edge endpoint; response envelopes stay compatible.
- Async I/O, cancellation, transactions, leases, retries, and shutdown are correct.
  Stateful adapters have bounded retention and an explicit restart story.
- Retrieval/evidence improvements beat or preserve the agreed baseline; unsupported
  questions and provider failures are not rendered as confident answers.
- UI changes work in the browser; static source assertions alone are insufficient.

Security review:

- Authenticate and authorize reads, writes, exports, jobs/history, traversal, and
  WebSocket subscribe/replay before fetching or broadcasting private data.
- Exercise SSRF redirects/rebinding/mixed DNS answers, IPv4/IPv6, byte/time caps,
  and proxy configuration. Do not remove destination pinning to gain compatibility.
- Treat uploads, graph values, and LLM output as untrusted; retain type/magic-byte
  validation and UI escaping; include indirect prompt-injection fixtures.
- Fixed sandbox scripts receive serialized data. Do not expose arbitrary code
  execution or run agent-generated programs on the application host.
- Kill/reap subprocesses on cancellation/timeout, delete remote sandboxes, bound
  concurrency/CPU/memory/tokens, and retain permits until cleanup finishes.
- Check dependency changes, migration permissions, secret/log handling, deletion,
  cache scope, and provider data transfer. Use disposable data and redact reports.

## Copyable task prompts

Implementer:

> Build M1's ID-based retrieval slice at BASE_COMMIT. Read AGENTS.md and the M1
> contract. Own only src/graph/graphrag.py and agreed retrieval tests; the graph
> agent owns Neo4j queries. Preserve offline fallback/status envelopes. Add cases
> for isolated entities and equal labels with different IDs. Do not switch branches
> or stage shared files. Return changed files, tests, assumptions, and open risks.

Subagent:

> Independently check the M1 legacy-source migration fixtures. Own only the named
> fixture/test file. Cover merged nodes spanning two sources, deletion of one
> source, and rollback. Use only a disposable database. Report failures to the
> parent; do not change the migration contract or touch shared implementation files.

Code reviewer:

> Review BASE..HEAD independently. Read the milestone contract and affected
> callers, not only the diff. Check correctness, migration compatibility, failure
> paths, async cleanup, provenance, and test realism. Do not edit implementation.
> Return actionable findings with file/line, impact, reproduction, and required
> regression; explicitly state coverage limits and the reviewed commit.

Security reviewer:

> Review BASE..HEAD as the independent security reviewer. Trace principal and
> workspace through HTTP, graph reads, evidence, jobs/history, caches, and WebSocket
> events; check SSRF, uploads/LLM output, sandbox lifecycle, budgets, and secrets.
> Use safe local reproductions, no paid services or production data. Do not approve
> your own code. Report findings, release blockers, verification, and reviewed commit.
