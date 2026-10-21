"""Run destructive integration tests only on the GitHub-created Neo4j service."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

IMAGE = "neo4j:5.26-community"
PASSWORD = "ci-disposable-only"


def validate_service(env: dict[str, str], container: dict) -> None:
    if env.get("GITHUB_ACTIONS") != "true" or env.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise ValueError("Integration CI requires a GitHub-hosted Actions runner")
    service_id = env.get("CI_NEO4J_SERVICE_ID", "")
    if not re.fullmatch(r"[a-f0-9]{64}", service_id) or container.get("Id") != service_id:
        raise ValueError("Service identity does not match the job-created container")
    if container.get("Config", {}).get("Image") != IMAGE:
        raise ValueError("Unexpected Neo4j service image")
    ports = container.get("NetworkSettings", {}).get("Ports", {})
    if ports.get("7687/tcp") != [{"HostIp": "127.0.0.1", "HostPort": "17687"}]:
        raise ValueError("Neo4j must bind the dedicated loopback CI port")
    if container.get("Mounts"):
        # Neo4j declares anonymous data/log volumes. Named volumes and host binds
        # could retain developer data, so allow only Docker-created anonymous ones.
        for mount in container["Mounts"]:
            if mount.get("Type") != "volume" or not re.fullmatch(
                r"[a-f0-9]{64}", mount.get("Name", "")
            ):
                raise ValueError("Service must not mount host paths or named volumes")
    if f"NEO4J_AUTH=neo4j/{PASSWORD}" not in container.get("Config", {}).get("Env", []):
        raise ValueError("Service must use the fixed disposable CI identity")
    if container.get("State", {}).get("Health", {}).get("Status") != "healthy":
        raise ValueError("Neo4j service is not healthy")


def main() -> int:
    if Path(".env").exists():
        raise SystemExit("Refusing integration CI with a repository .env file")
    # Validate the ID before using it as a Docker argument; inspect is read-only.
    service_id = os.environ.get("CI_NEO4J_SERVICE_ID", "")
    if not re.fullmatch(r"[a-f0-9]{64}", service_id):
        raise SystemExit("Missing job-created Neo4j service identity")
    container = json.loads(subprocess.check_output(["docker", "inspect", service_id]))[0]
    validate_service(dict(os.environ), container)
    # Do not inherit provider credentials, dotenv settings or external DB targets.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ}
    env.update(NEO4J_URI="bolt://127.0.0.1:17687", NEO4J_USER="neo4j", NEO4J_PASSWORD=PASSWORD)
    for milestone in range(1, 6):
        env[f"M{milestone}_DISPOSABLE_NEO4J"] = "1"
    env["RESEARCH_DISPOSABLE_NEO4J"] = "1"
    return subprocess.call([sys.executable, "-m", "pytest", "-q", "-m", "integration"], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
