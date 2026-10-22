# H1 slice A — local connected acceptance

This is the local harness from [the H1 contract](HOSTED_BACKEND_CONTRACT.md).
It runs the original application in a separate private-mode process, with real
HTTP/WebSockets, actual SQLite state and a newly owned Neo4j 5.26 database.
Chromium loads the real UI; API and WebSocket responses are not intercepted.
Only the external completion provider is scripted with invented Apricot/Cedar
data. This is not hosted, TLS, proxy, production or real-provider acceptance.

## Run

Use Python 3.11 and an unpacked local Neo4j Community 5.26 distribution with its
required Java runtime. The suite does not download or provision either service.

```bash
.venv/bin/python -m pip install -r requirements-hosted.txt
.venv/bin/python -m playwright install chromium
HOSTED_NEO4J_HOME=/path/to/neo4j-community-5.26.0 .venv/bin/python -m pytest --hosted tests/hosted -q
```

`HOSTED_NEO4J_HOME` supplies binaries only. The harness generates separate temporary
configuration, data, logs and run directories, disables database telemetry, uses
random loopback ports and verifies the live server's data directory and version
before schema setup or clearing data. It never accepts `NEO4J_URI` or credentials
from the operator environment. Authentication is disabled only on this owned,
temporary, loopback database. Never point another application at that port.

`BROWSER_EXECUTABLE` optionally selects a local Chromium binary. Explicit hosted
collection fails if its requested tools are missing; it does not silently skip.
The default offline suite excludes `tests/hosted/` before importing its services
or browser tooling. Offline harness-safety tests use only local Python fixtures.
Parallel pytest/xdist execution is not supported.

## Isolation and evidence

- The app child receives a small explicit environment, fake provider credentials,
  private temporary identity configuration and per-workspace SQLite. Dotenv loading
  is disabled before app imports. No fixture hook is added to production `src/`.
- A test-only Python audit hook allows outbound socket operations only to the
  assigned literal-loopback fixture and owned Neo4j ports; it fences DNS, binds,
  IPv6 and unexpected destinations and records numeric counters only. The test
  bootstrap explicitly selects the standard-library asyncio loop so Python socket
  operations stay visible to the audit hook. The fixture
  retains counts, not prompts/credentials. This is an accidental-egress guard in
  the Python app process, **not an OS sandbox or production firewall**. Local
  fixed analytics/parser subprocesses are not independently fenced by that hook.
  A dependency's startup IPv6 bind-capability probe is deliberately denied; a
  nonzero denied counter does not mean that external provider access succeeded.
- Browser tests block decorative font requests. They save no screenshots, traces,
  video, storage snapshots or authentication artifacts. The normal application
  frontend is used, without the Vercel preview shim.
- Service logs stay in private temporary files and are not copied into assertion
  messages or CI artifacts. Identity tokens are generated for each test and never
  printed. Each test stops its app/provider; session cleanup stops the owned DB.
- Crash tests really kill the API after graph publication using a test-bootstrap
  barrier, then expire the dead owner's SQLite lease deterministically. Scoped
  direct-DB fixture setup refreshes or deletes the source before HTTP recovery.
  These establish receipt replay behavior; they do not measure real lease-wait
  latency, storage corruption, host failure or recovery-point objectives.

## Coverage and remaining gates

The harness exercises cookie/bearer authorization, reader denials, CSRF and WS
Origin checks; text/file publication and telemetry; source-scoped answers and
exact citation offsets; guessed-ID isolation and deletion; actual browser login,
workspace switching and logout; saved history, graph/jobs and quotas across API
restart; active cancellation and queued submitter/socket revocation; provider
failure/missing-key behavior; and accidental external-fetch denial.

Tests distinguish successful job execution from `result.success`. A network
policy violation may produce a sanitized failed job rather than a provider
business failure. No denied request may reach the fixture or publish a graph.
All completion/token values are fixture protocol inputs, not measured real-model
usage or a quality result.

This covers local portions of H1-03 through H1-11, not all H1 acceptance rows.
HTTP loopback deliberately uses the existing development cookie exception; it
does not prove Secure cookies, TLS renewal, forwarded-header trust, ingress body
limits, external port isolation, persistent volume mounting, backup/restore,
readiness admission or a deployed image. Those remain slices B/C. Real-provider
evaluation still needs a separate explicit call/cost budget. H1 remains open.

The connected browser run exposed a late WebSocket error during workspace
transitions. The frontend fix captures the socket for its callbacks and fences
superseded sockets; a deterministic browser regression also covers a late error
after the global socket has been cleared or replaced.
