# H1 slice B — staging configuration and operator probes

Status: implemented, locally tested tooling and **unexecuted host templates**.
No provider, region, domain, image digest, storage purchase or deployment is
selected by these files. [H1](HOSTED_BACKEND_CONTRACT.md) remains open.

## Bind the template

Follow [the staging guide](../deploy/staging/README.md). Copy
`deploy/staging/operator.example.json` into an operator-owned directory (0700),
make the copy 0600, and fill every field. The example deliberately fails
validation. Keep the separate Neo4j credential files and reader token private;
never paste them into commands or a resolved Compose/inspect dump.

Bindings include exact HTTPS origin (canonical hostname, no path or explicit
default port), workspace, request quota/window, Compose project, image digests,
separate state/Neo4j mountpoints, TLS/control directories, exact operator probe
source IP, source commit, host resources, hosting ceiling, renewal ownership,
backup access/encryption and recovery objectives. These recorded choices are
operator inputs, not measurements or approvals to purchase resources.

The template uses one API process, UID/GID 1000, private SQLite, Neo4j on a private
internal network and same-origin nginx TLS ingress. Only 443 is published.
Provider keys are empty; the Vercel preview is independent. Nginx syntax and
startup still require the selected image. Certificate issuance/renewal is an
operator binding, not an included ACME service.

## Nonmutating preflight

Run on the actual Linux Docker host with permission to inspect the local Docker
socket, private files and mount table:

```bash
python -m scripts.hosted_preflight --config /private/operator/host.json --json
```

This inspects actual running containers rather than trusting a Compose declaration:
image/commit bindings, one API process, overlapping container state writers,
private ports and networks, effective private auth/quotas/provider settings,
mount sources, permissions, resource/stop bounds and the reviewed proxy setup.
State and Neo4j directories must be separate explicit local writable mountpoints
on ext4, XFS, Btrfs or ZFS; a writable directory on the host root filesystem is
insufficient evidence. Unsupported filesystems/hosts or missing inspection
capabilities fail closed. Run on the host, not inside an app container or against
a remote Docker context. The fixed local socket ignores operator Docker contexts.

Preflight is a point-in-time inspection of the declared Docker deployment.
It cannot exclude arbitrary privileged host processes or prove host firewall,
physical durability, image supply-chain integrity, absence of provider egress,
or successful backup/restore. Docker inspection values stay in memory and are
never copied into the report. It does not start, stop or provision anything.

## Authenticated readiness

Provision a dedicated principal with **reader access to exactly one test workspace**
through the existing private identity configuration process. Store its token in
a 0600 file inside a 0700 operator-owned directory. The tool never reads dotenv.

```bash
python -m scripts.hosted_readiness --config /private/operator/host.json --credential-file /private/operator/reader.token --json
```

Readiness requires system-trusted HTTPS to the exact configured origin. It ignores
proxy/netrc environment variables and follows no redirects. It checks private auth,
creates a session, validates scoped reader membership and Secure/HttpOnly/Strict
cookies, reads identity through that cookie, executes `/api/graph/stats`, and reads
`/api/usage` to verify live SQLite accounting and effective quota/window. It calls
no Ask, ingest, sandbox, schedule or provider endpoint. Each request has bounded
time and response size. A static UI or cached `/api/health` cannot pass this gate.

After any issued session, cleanup attempts logout even if a later check fails,
then verifies the old cookie returns 401. Failure to establish revocation fails
the gate; the operator must investigate/revoke or rotate that dedicated test
credential before proceeding. Never share raw request/response diagnostics.

Both tools print only `{ "version": 1, "ok": ..., "checks": [...] }`, with stable
check IDs, `pass|fail|unverified`, and fixed reason codes. Exit 0 requires all
checks in that tool's scope to pass; failures/unverified checks exit 1. No response
bodies, cookies, credentials, operator paths or exception messages appear.

## Admission, recovery and remaining evidence

Keep `/control/admitted` absent during startup and changes. The exact operator
source IP bypasses maintenance only; private application auth still applies.
Normal admissions remain closed until the operator verifies preflight and
readiness and explicitly admits traffic. Remove admission before stopping the old
API; verify it exited before starting a new instance against the same state.
Measure graceful drain and forced recovery on the bound host.

This slice has **manual admission**, not a continuous readiness monitor. A failed
probe returns nonzero; it does not remove markers, restart containers or deploy
anything. Automatic failure-triggered admission withholding, maintenance socket
drain, externally verified port/egress isolation, trusted proxy behavior under
real ingress, measured shutdown budgets, coordinated encrypted backup/restore,
release/rollback and the exact hosted browser smoke remain open acceptance work.
Restore both SQLite/config and Neo4j as one coordinated set with writers stopped;
never repair a rollback by deleting state. Keep provider egress denied and review
retained commands/schedules before any restoration or provider pilot.

Offline tests include malformed/private input files, adversarial Docker snapshots,
proxy and mount deviations, HTTP failure/redirect/credential cleanup, and real
private-route/SQLite round trips with mocked graph I/O. These do not prove a Linux
host or public TLS deployment. Run `python -m pytest -q` and Ruff locally; run
[the connected slice A suite](H1_LOCAL_ACCEPTANCE.md) for its distinct evidence.
