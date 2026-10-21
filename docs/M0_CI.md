# M0 CI and security contract

This bounded slice adds CI gates; it does not complete M0's evaluation, telemetry,
or reproducible dependency-lock work. No paid providers or developer databases
are needed. Checks run for push and pull requests, with `contents: read`, no
persisted checkout credentials, and per-job timeouts. Checkout v4.3.1 and
setup-python v5.6.0 are pinned to commit SHAs verified through their official
repositories on 2026-10-21. There is no `pull_request_target` execution or secrets
injection.

## Blocking security checks

- `pip-audit==2.9.0 -r requirements.txt` resolves and audits runtime dependencies
  and their transitives. Advisory/network/resolution failures fail the job; there
  are no ignored advisories. It does not audit optional Streamlit/browser tools,
  the base container, or every older version allowed by minimum requirements.
- `python -m scripts.check_ci_secrets` uses `detect-secrets==1.5.0` on all tracked
  files, with default detectors/heuristics and no credential verification calls.
  Logs contain only filenames, line numbers and detector names. The reviewed
  fingerprint list binds filename, detector and secret hash; each synthetic
  fixture or UI/documentation false positive has a reason. It excludes no path.
  Exact SHA-1 metadata strings inside the fingerprint list are recognized as
  nonsecret hashes; other values in that file are still scanned. A new value,
  filename or detector requires explicit review. Review changes to
  `.security/reviewed-secrets.json` as security-sensitive changes. This scans the
  current tree, not historical Git objects or untracked files.
- `bandit==1.8.6 -r src scripts -ll` blocks medium/high severity issues. It has no
  skipped rules or `nosec` additions. Low severity remains visible in run metrics;
  run without `-ll` for details. Existing B110 findings are intentional best-effort
  chmod cleanup. New CI subprocess findings use fixed executables/argument arrays
  without a shell; Docker service IDs are validated before use. B105 is a public,
  disposable CI-only database password. These low findings are reviewed, not
  evidence that arbitrary subprocess input or hardcoded production secrets are safe.

The legacy MD5 source namespace explicitly uses `usedforsecurity=False`: it is
compatibility identity hashing, not authentication, and generated IDs are unchanged.

## Destructive integration fence

The integration job creates its own Neo4j 5.26 Community service with anonymous
Docker volumes and a dedicated `127.0.0.1:17687` Bolt mapping. The launcher accepts
only a GitHub-hosted runner and the exact service ID supplied by Actions, then
inspects the image, health, port binding, mounts and fixed disposable identity.
Host binds/named volumes and repository `.env` files are refused. The child test
process receives a minimal environment with an overwritten loopback database target,
no provider credentials, and all M1–M5/research disposable flags. The whole marked
integration suite runs, including graph-clearing tests. GitHub tears down the
service after the job. Never reuse this password or expose the service publicly.

This protects against accidental external database targeting; untrusted workflow
changes still require review. The Neo4j `5.26-community` image tag is not digest
pinned, and GitHub-hosted CI remains the required live validation environment.

## Local validation and remaining work

Use the normal development environment for offline tests and Ruff. Install the
three pinned scanners separately to reproduce security checks; secret tests skip
their optional scanner-specific case when the tool is absent. Do not invoke the
integration launcher locally or export real Neo4j credentials to CI.

On 2026-10-21 a temporary resolved runtime snapshot (81 packages, macOS/Python
3.11) had no known vulnerabilities. The local pip-audit requirement resolver hit
an `ensurepip` abort; validation instead used `uv pip compile` and audited every
resolved pin with `--no-deps --disable-pip`. This is a transient audit snapshot,
not a committed lock or Linux CI result. A broader installed development-environment
audit reported Pillow 12.3.0 (eight advisories, fixes 13.0.0), bootstrap pip 24.0
(six advisories), and setuptools 79.0.1 (one advisory). Those tools are outside
the runtime gate; developer-tool audit/remediation remains follow-up work.

Keep dependency locking, container digest pinning/scanning, historical secret
scanning, and developer/browser-tool auditing open. First GitHub CI execution
must confirm the disposable-service health and complete integration suite.
