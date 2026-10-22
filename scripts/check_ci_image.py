"""Build and inspect an ephemeral CI image; emit public metadata, never raw Docker logs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
PROBE = r'''
import importlib.metadata, json, os, pathlib, sys
root = pathlib.Path('/app')
html = (root / 'frontend/index.html').read_text()
print(json.dumps({
    'uid': os.getuid(), 'gid': os.getgid(), 'python': list(sys.version_info[:2]),
    'private_files_absent': not any(root.glob('.env*')) and not (root / '.git').exists()
                            and (root / 'data').is_dir() and not any((root / 'data').iterdir()),
    'fixtures_absent': not (root / 'tests').exists(),
    'original_ui': 'preview.js' not in html,
    'dependencies': [{'name': d.metadata['Name'], 'version': d.version}
                     for d in importlib.metadata.distributions()],
}))
'''


class GateError(Exception):
    """A fixed report code only."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise GateError("invalid_arguments")


def run(arguments, home, *, timeout=30, limit=2 * 1024 * 1024):
    # Docker auth/context/provider variables never reach subprocesses. Temporary
    # files keep diagnostics out of stdout and are removed even on failure.
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home),
                   "LANG": "C.UTF-8", "DOCKER_CONFIG": str(home / "docker-config")}
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            completed = subprocess.run(arguments, cwd=ROOT, env=environment,
                                       stdout=output, stderr=errors, timeout=timeout, check=False)
            if completed.returncode:
                raise GateError("command_failed")
            if output.tell() > limit:
                raise GateError("output_too_large")
            output.seek(0)
            return output.read(limit + 1)
    except (OSError, subprocess.SubprocessError):
        raise GateError("command_unavailable") from None


def checkout(commit, home):
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise GateError("invalid_commit")
    actual = run(["git", "rev-parse", "HEAD"], home).decode("ascii").strip()
    if actual != commit or run(["git", "status", "--porcelain", "--untracked-files=all"], home):
        raise GateError("checkout_mismatch")
    if (ROOT / ".env").exists():
        raise GateError("dotenv_present")


def image_policy(metadata, commit):
    if not isinstance(metadata, list) or len(metadata) != 1 or not isinstance(metadata[0], dict):
        raise GateError("image_metadata_invalid")
    image = metadata[0]
    identifier = image.get("Id", "")
    config = image.get("Config", {})
    if (not re.fullmatch(r"sha256:[a-f0-9]{64}", identifier)
            or config.get("Labels", {}).get("org.opencontainers.image.revision") != commit
            or config.get("User") not in ("appuser", "1000", "1000:1000")
            or config.get("Entrypoint") or config.get("Volumes")
            or config.get("Cmd") != ["uvicorn", "src.main:app", "--host", "0.0.0.0",  # nosec B104
                                     "--port", "8000"]):
        raise GateError("image_policy_failed")
    environment = {}
    for entry in config.get("Env", []):
        key, value = entry.split("=", 1)
        if key in environment:
            raise GateError("image_environment_invalid")
        environment[key] = value
    private = {"API_KEY", "KIMI_API_KEY", "DAYTONA_API_KEY", "NOSANA_API_KEY",
               "GRAPHAGENT_TOKEN", "NEO4J_PASSWORD", "AUTH_CONFIG_PATH", "STATE_DB_PATH"}
    if any(environment.get(key) for key in private):
        raise GateError("image_credentials_present")
    return identifier


def inventory(probe):
    if (not isinstance(probe, dict) or type(probe.get("uid")) is not int
            or probe["uid"] != 1000 or probe.get("gid") != 1000
            or probe.get("python") != [3, 11]
            or any(probe.get(key) is not True for key in (
                "private_files_absent", "fixtures_absent", "original_ui"))):
        raise GateError("container_policy_failed")
    packages = probe.get("dependencies")
    if not isinstance(packages, list) or not 1 <= len(packages) <= 512:
        raise GateError("inventory_invalid")
    names = set()
    output = []
    for package in packages:
        if (not isinstance(package, dict) or set(package) != {"name", "version"}
                or not isinstance(package["name"], str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", package["name"])
                or not isinstance(package["version"], str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}", package["version"])):
            raise GateError("inventory_invalid")
        name = re.sub(r"[-_.]+", "-", package["name"]).lower()
        if name in names:
            raise GateError("inventory_invalid")
        names.add(name)
        output.append({"name": name, "version": package["version"]})
    return sorted(output, key=lambda package: package["name"])


def report(checks, **metadata):
    return {"version": 1, "ok": bool(checks) and all(c["status"] == "pass" for c in checks),
            "checks": checks, **metadata}


def check(identifier, passed, reason="verified"):
    return {"id": identifier, "status": "pass" if passed else "fail", "reason": reason}


def output_directory(path, runner_temp):
    root = Path(runner_temp)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise GateError("invalid_output_directory")
    # Exactly one fresh child of the actual runner temporary directory; no
    # arbitrary output paths, existing directories or symlink traversal.
    if not path.is_absolute() or path.parent != root or path.name in ("", ".", ".."):
        raise GateError("invalid_output_directory")
    if any(parent.is_symlink() for parent in (root, *root.parents)):
        raise GateError("invalid_output_directory")
    if path.exists() or path.is_symlink():
        raise GateError("output_already_exists")


def write_new(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value)


def gate(commit, destination, environment):
    checks = []
    if (environment.get("GITHUB_ACTIONS") != "true"
            or environment.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        return report([check("runner", False, "github_hosted_runner_required")])
    current = "checkout"
    try:
        output_directory(destination, environment.get("RUNNER_TEMP", ""))
        with tempfile.TemporaryDirectory(prefix="h1-image-", dir=destination.parent) as folder:
            home = Path(folder)
            checkout(commit, home)
            checks.append(check(current, True))
            tag = "graphagent-h1-ci:" + commit + "-" + secrets.token_hex(8)
            container_name = "graphagent-h1-probe-" + secrets.token_hex(8)
            container_attempted = False
            current = "image_build"
            image_id = None
            packages = None
            try:
                run(DOCKER + ["build", "--pull", "--no-cache", "--label",
                              f"org.opencontainers.image.revision={commit}", "--tag", tag,
                              "--file", str(ROOT / "Dockerfile"), str(ROOT)], home,
                    timeout=600, limit=16 * 1024 * 1024)
                checks.append(check(current, True))
                current = "image_policy"
                image_id = image_policy(json.loads(run(DOCKER + ["image", "inspect", tag], home)),
                                        commit)
                checks.append(check(current, True))
                current = "container_policy"
                container_attempted = True
                raw = run(DOCKER + ["run", "--name", container_name,
                                   "--network", "none", "--read-only",
                                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                                   "--entrypoint", "python", image_id, "-c", PROBE],
                          home, timeout=60)
                packages = inventory(json.loads(raw))
                checks.append(check(current, True))
            finally:
                if container_attempted:
                    try:
                        run(DOCKER + ["container", "rm", "--force", "--volumes", container_name],
                            home, timeout=30)
                        checks.append(check("container_cleanup", True))
                    except Exception:
                        checks.append(check("container_cleanup", False, "cleanup_failed"))
                # The unique tag belongs only to this invocation. No registry push,
                # named volumes or long-lived application containers are involved.
                try:
                    run(DOCKER + ["image", "rm", "--force", tag], home, timeout=30)
                    checks.append(check("image_cleanup", True))
                except Exception:
                    checks.append(check("image_cleanup", False, "cleanup_failed"))
            if not all(item["status"] == "pass" for item in checks):
                return report(checks)
            current = "evidence"
            encoded = json.dumps(packages, sort_keys=True, separators=(",", ":")) + "\n"
            output = report(checks, source_commit=commit, image_id=image_id,
                            inventory_sha256=hashlib.sha256(encoded.encode()).hexdigest())
            destination.mkdir(mode=0o700)
            write_new(destination / "dependencies.json", encoded)
            write_new(destination / "runtime-requirements.txt", "".join(
                f'{package["name"]}=={package["version"]}\n' for package in packages))
            write_new(destination / "report.json", json.dumps(output, separators=(",", ":")) + "\n")
            return output
    except GateError as exc:
        checks.append(check(current, False, exc.args[0]))
    except Exception:
        checks.append(check(current, False, "gate_failed"))
    return report(checks)


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--commit", required=True)
        parser.add_argument("--output-dir", required=True, type=Path)
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args(argv)
        output = gate(args.commit, args.output_dir, dict(os.environ))
    except GateError:
        output = report([check("configuration", False, "invalid_arguments")])
    print(json.dumps(output, separators=(",", ":")))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
