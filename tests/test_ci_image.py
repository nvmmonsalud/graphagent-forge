"""Adversarial image-gate checks; no Docker daemon, image pulls or builds."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import check_ci_image as gate

COMMIT = "a" * 40
IMAGE = "sha256:" + "b" * 64


def metadata():
    return [{"Id": IMAGE, "Config": {
        "Labels": {"org.opencontainers.image.revision": COMMIT}, "User": "appuser",
        "Cmd": ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"],
        "Env": ["PATH=/usr/local/bin:/usr/bin:/bin"],
    }}]


def probe():
    return {"uid": 1000, "gid": 1000, "python": [3, 11], "private_files_absent": True,
            "fixtures_absent": True, "original_ui": True,
            "dependencies": [{"name": "z-package", "version": "1.0"},
                             {"name": "A_package", "version": "2.3.0"}]}


@pytest.fixture
def fake_runner(monkeypatch):
    calls = []
    failures = {}

    def run(arguments, home, **kwargs):
        calls.append(arguments)
        if arguments[0] == "git":
            return (COMMIT + "\n").encode() if arguments[1] == "rev-parse" else b""
        operation = arguments[len(gate.DOCKER):]
        if operation[0] in failures:
            raise gate.GateError(failures[operation[0]])
        if operation[:2] == ["image", "inspect"]:
            return json.dumps(metadata()).encode()
        if operation[0] == "run":
            return json.dumps(probe()).encode()
        return b""

    monkeypatch.setattr(gate, "run", run)
    return calls, failures


def env(tmp_path):
    return {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
            "RUNNER_TEMP": str(tmp_path)}


def test_gate_writes_only_public_evidence_and_cleans_its_container_and_image(tmp_path, fake_runner):
    calls, _ = fake_runner
    destination = tmp_path / "evidence"
    output = gate.gate(COMMIT, destination, env(tmp_path))
    assert output["ok"] and output["source_commit"] == COMMIT and output["image_id"] == IMAGE
    assert set(path.name for path in destination.iterdir()) == {
        "report.json", "dependencies.json", "runtime-requirements.txt"}
    assert destination.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in destination.iterdir())
    assert (destination / "runtime-requirements.txt").read_text() == (
        "a-package==2.3.0\nz-package==1.0\n")
    assert json.loads((destination / "report.json").read_text()) == output
    run_command = next(command for command in calls if command[3:4] == ["run"])
    assert run_command[run_command.index("--network") + 1] == "none"
    assert "--read-only" in run_command and "--cap-drop" in run_command
    assert run_command[run_command.index("--entrypoint") + 1] == "python"
    assert run_command[run_command.index("-c") - 1] == IMAGE
    assert calls[-2][3:7] == ["container", "rm", "--force", "--volumes"]
    assert calls[-1][3:6] == ["image", "rm", "--force"]
    assert calls[-2][-1] == run_command[run_command.index("--name") + 1]
    assert str(tmp_path) not in json.dumps(output)


@pytest.mark.parametrize("operation", ["build", "run", "container", "image"])
def test_failures_fail_closed_without_evidence_or_raw_logs(tmp_path, fake_runner, operation):
    calls, failures = fake_runner
    failures[operation] = "command_failed"
    destination = tmp_path / "evidence"
    result = gate.gate(COMMIT, destination, env(tmp_path))
    assert not result["ok"]
    assert not destination.exists()
    assert calls[-1][3:6] == ["image", "rm", "--force"]
    if operation in ("run", "container"):
        assert any(command[3:5] == ["container", "rm"] for command in calls)


@pytest.mark.parametrize("runner", [None, "self-hosted", ""])
def test_non_github_hosted_runner_does_not_execute_commands(tmp_path, fake_runner, runner):
    calls, _ = fake_runner
    environment = env(tmp_path)
    environment["RUNNER_ENVIRONMENT"] = runner
    output = gate.gate(COMMIT, tmp_path / "evidence", environment)
    assert not output["ok"] and not calls


@pytest.mark.parametrize("field,value", [
    ("User", "root"), ("Entrypoint", ["sh"]), ("Volumes", {"/private": {}}),
    ("Cmd", ["uvicorn", "src.main:app", "--reload"]),
    ("Labels", {"org.opencontainers.image.revision": "c" * 40}),
    ("Env", ["KIMI_API_KEY=public-fake-test-credential"]),
    ("Env", ["PATH=/bin", "PATH=/usr/bin"]),
])
def test_image_policy_rejects_wrong_code_process_privilege_or_credentials(field, value):
    image = metadata()
    image[0]["Config"][field] = value
    with pytest.raises(gate.GateError):
        gate.image_policy(image, COMMIT)


@pytest.mark.parametrize("field,value", [
    ("uid", 0), ("gid", 0), ("python", [3, 12]), ("private_files_absent", False),
    ("fixtures_absent", False), ("original_ui", False),
    ("dependencies", [{"name": "bad\npackage", "version": "1"}]),
    ("dependencies", [{"name": "package", "version": "https://private.invalid/token"}]),
    ("dependencies", [{"name": "a.b", "version": "1"}, {"name": "a-b", "version": "1"}]),
])
def test_container_inventory_rejects_unsafe_or_ambiguous_values(field, value):
    response = probe()
    response[field] = value
    with pytest.raises(gate.GateError):
        gate.inventory(response)


def test_output_cannot_overwrite_or_escape_runner_temp(tmp_path, fake_runner):
    calls, _ = fake_runner
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("preserve")
    for destination in (existing, tmp_path.parent / "outside", tmp_path / "nested" / "child"):
        result = gate.gate(COMMIT, destination, env(tmp_path))
        assert not result["ok"]
    assert not calls
    assert (existing / "keep").read_text() == "preserve"


def test_output_rejects_symlink(tmp_path, fake_runner):
    calls, _ = fake_runner
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    assert not gate.gate(COMMIT, link, env(tmp_path))["ok"]
    assert not calls


def test_checkout_rejects_changed_or_dirty_source(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "run", lambda args, *_: b"b" * 40 + b"\n")
    with pytest.raises(gate.GateError, match="checkout_mismatch"):
        gate.checkout(COMMIT, tmp_path)
    monkeypatch.setattr(gate, "run", lambda args, *_: (COMMIT + "\n").encode()
                        if args[1] == "rev-parse" else b" M Dockerfile\n")
    with pytest.raises(gate.GateError, match="checkout_mismatch"):
        gate.checkout(COMMIT, tmp_path)


def test_subprocesses_fence_secrets_and_diagnostics(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("KIMI_API_KEY", "public-fake-test-credential")
    monkeypatch.setenv("DOCKER_HOST", "tcp://outside.invalid:2375")

    def execute(arguments, **kwargs):
        assert "KIMI_API_KEY" not in kwargs["env"] and "DOCKER_HOST" not in kwargs["env"]
        assert kwargs["env"]["HOME"] == str(tmp_path)
        kwargs["stderr"].write(b"private diagnostic must not be printed")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(subprocess, "run", execute)
    with pytest.raises(gate.GateError, match="command_failed"):
        gate.run(["docker", "inspect"], tmp_path)
    assert capsys.readouterr().out == ""
    assert capsys.readouterr().err == ""


def test_timeout_is_sanitized(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("private-command", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(gate.GateError, match="command_unavailable"):
        gate.run(["docker", "build"], tmp_path)


def test_cli_invalid_input_never_echoes_argument(capsys):
    assert gate.main(["--invalid", "private-value"]) == 1
    captured = capsys.readouterr()
    assert not captured.err and "private-value" not in captured.out
    assert json.loads(captured.out)["ok"] is False


@pytest.mark.parametrize("private_file", [None, "identity.sqlite3", "workspaces/a/state.sqlite3"])
def test_probe_rejects_packaged_state_in_an_actual_directory(tmp_path, private_file):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/index.html").write_text("<html>Original UI</html>")
    (tmp_path / "data").mkdir()
    if private_file:
        path = tmp_path / "data" / private_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("invented fixture only")
    program = gate.PROBE.replace("Path('/app')", f"Path({str(tmp_path)!r})")
    completed = subprocess.run([sys.executable, "-c", program], capture_output=True,
                               check=True, timeout=10)
    response = json.loads(completed.stdout)
    assert response["private_files_absent"] is (private_file is None)
    assert response["fixtures_absent"] and response["original_ui"]
