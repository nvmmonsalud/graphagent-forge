# H1 slice C — CI acceptance and image validation

Status: CI configuration and locally tested gate logic. No registry image,
release approval, host or deployment is created here. The workflow must run on
GitHub before its jobs can be described as passing CI. [H1](HOSTED_BACKEND_CONTRACT.md)
still requires the host, TLS, admission, recovery and release acceptance evidence.

## Connected acceptance in CI

`.github/workflows/h1-gates.yml` runs on push and pull request with read-only
repository permissions and GitHub-hosted Ubuntu runners. It uses Python 3.11,
Java 21 and Chromium. The exact Neo4j Community 5.26.0 tarball is downloaded over
HTTPS and checked against its pinned SHA-256 **before** extraction into a fresh
runner directory. The pin comes from the publisher's
[checksum](https://dist.neo4j.org/neo4j-community-5.26.0-unix.tar.gz.sha256);
updating it requires verifying the release and rerunning the acceptance baseline.
This is the disposable local acceptance version, not a selected hosting image.

The existing [slice A harness](H1_LOCAL_ACCEPTANCE.md) owns a new database and
uses invented provider responses. It never receives provider or external database
credentials from GitHub secrets. Fixture state stays in a private temporary
runner directory. All pytest output, including collection errors and warnings, is redirected to a
private temporary file and discarded; only a fixed pass/fail summary reaches the
CI log. No logs, database/config files, screenshots, storage state or traces are
uploaded. Reproduce locally with disposable fixtures when deeper diagnostics
are needed. Do not upload raw reproduction data.

This job adds real local app/HTTP/WS/SQLite/Neo4j/browser evidence alongside the
existing synthetic browser and disposable integration jobs. It proves no hosted
TLS, real-provider quality or live-environment isolation property.

## CI image build gate

The independent image job runs:

```bash
python -m scripts.check_ci_image --commit "$GITHUB_SHA" --output-dir "$RUNNER_TEMP/h1-image-evidence" --json
```

The helper refuses non-GitHub-hosted contexts, dirty/mismatched checkouts, local
`.env` files and existing/escaping output directories. It builds a uniquely tagged
local image from the exact checkout with a revision label. Build output and Docker
inspection details stay in private temporary files and are discarded. Subprocesses
receive no operator provider credentials, registry credentials or Docker contexts.

It checks the image's revision, default command, non-root identity and absence of
configured credentials/volumes. A short metadata probe runs by immutable image ID without network,
with a read-only filesystem and dropped capabilities. It checks actual UID/GID
1000, Python 3.11, absence of private files and packaged test fixtures, and the
original UI without the public preview shim. It does **not** start the application.
It records the actual installed package names/versions and cleans up its own
container and image tag, including on failure. Cleanup failure fails the gate.

The workflow audits that installed inventory with pinned `pip-audit` and no
vulnerability ignores, rather than assuming the host Python installation matches
the image. Only after successful build/policy/audit steps does it upload exactly:

- `report.json`: source commit, local image ID, inventory hash and fixed check codes.
- `dependencies.json`: sorted public package names and versions.
- `runtime-requirements.txt`: the same validated `name==version` inventory for audit.

Retention is seven days. These files contain no environment dumps, operator
bindings, credentials, private graph data or raw logs. The helper emits only its
sanitized JSON report. An unavailable Docker daemon, failed build, mismatched
image or audit failure must fail; do not bypass these checks to make CI green.

## Evidence boundaries and release order

A local Docker **image ID is not a registry manifest digest**. The gate removes
its image after inspection and does not produce a deployable release artifact.
Dependencies and the Python base tag remain unlocked; package inventory records
what that build contained but does not make a later rebuild identical. Full
reproducibility remains M0 work. Metadata from a pull request is diagnostic,
not release authorization, a signed attestation or an approved image binding.

Before staging, require all CI jobs for the exact candidate commit to pass;
independently review it; build/publish an approved immutable release image through
a separately reviewed registry workflow; inspect/audit that exact digest and bind
it with the tested source commit in the private operator configuration. Never
substitute a fresh rebuild based only on an earlier image ID or a green branch.

Then use [slice B preflight/readiness](H1_STAGING_PREFLIGHT.md) on the bound host,
retain maintenance while replacing the single API instance, and run the exact
private origin's authenticated browser smoke. Record externally verified port/
provider-egress isolation, forwarding headers, drain/crash recovery and coordinated
backup/restore. Continuous admission withholding and measured rollback remain
open. Release approval must enumerate unresolved H1 acceptance rows.

The existing Vercel synthetic preview workflow and its manual promotion gate are
unchanged. Passing these new CI jobs does not authorize production promotion or
paid provider calls.
