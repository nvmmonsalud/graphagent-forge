"""Offline operator-tool checks. No Docker service or remote host is contacted."""

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import hosted_preflight as preflight
from scripts.hosted_config import BINDINGS, ConfigError, load_config, read_private_file


@pytest.fixture
def config(tmp_path):
    tmp_path.chmod(0o700)
    return {
        "version": 1, "origin": "https://staging.example.org", "workspace_id": "alpha",
        "usage_request_limit": 8, "usage_window_seconds": 3600, "compose_project": "forge",
        "images": {name: f"example/{name}@sha256:" + "a" * 64
                   for name in ("app", "neo4j", "proxy")},
        "state_dir": str(tmp_path / "state"), "neo4j_dir": str(tmp_path / "neo4j"),
        "control_dir": str(tmp_path / "control"), "tls_dir": str(tmp_path / "tls"),
        "probe_address": "192.0.2.8",
        "bindings": {key: 1 if key == "enabled_workspace_count" else
                     "a" * 40 if key == "source_commit" else "operator-bound"
                     for key in BINDINGS},
    }


def write_config(tmp_path, config):
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return path


@pytest.fixture(autouse=True)
def template_fixture(monkeypatch):
    original = preflight.read_template
    def read(path):
        if str(path) == "/operator/reviewed-template":
            path = Path(__file__).resolve().parents[1] / "deploy/staging/nginx.conf.template"
        return original(path)
    monkeypatch.setattr(preflight, "read_template", read)


def snapshot(config):
    result = []
    for service in ("app", "neo4j", "proxy"):
        item = {
            "State": {"Running": True}, "_H1ApiProcesses": 1,
            "_H1Networks": {"forge_private": True},
            "Config": {"Image": config["images"][service], "Labels": {
                "com.docker.compose.project": "forge", "com.docker.compose.service": service,
                "org.opencontainers.image.revision": "a" * 40}, "Env": [],
                "StopTimeout": {"app": 210, "neo4j": 120, "proxy": 30}[service]},
            "HostConfig": {"Memory": 1024**3, "NanoCpus": 10**9, "PidsLimit": 100,
                           "PortBindings": {}, "ReadonlyRootfs": True, "Init": True,
                           "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"]},
            "Mounts": [],
            "NetworkSettings": {"Networks": {"forge_private": {"IPAddress": {
                "app": "172.30.63.3", "neo4j": "172.30.63.4", "proxy": "172.30.63.2"}[service]}}},
        }
        result.append(item)
    app, neo, proxy = result
    environment = {
        "AUTH_MODE": "private", "AUTH_CONFIG_PATH": "/app/data/auth.json",
        "STATE_DB_PATH": "/app/data/identity.sqlite3", "AUTH_COOKIE_SECURE": "1",
        "ALLOWED_ORIGINS": config["origin"], "ENABLE_API_DOCS": "0", "DEV_RELOAD": "0",
        "USAGE_REQUEST_LIMIT": "8", "USAGE_WINDOW_SECONDS": "3600",
        "NEO4J_URI": "bolt://172.30.63.4:7687", "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "".join(["invented", "-test-only"]),
    }
    app["Config"].update(User="1000:1000", Cmd=preflight.COMMAND,
                         Env=[f"{key}={value}" for key, value in environment.items()])
    neo["Config"]["User"] = "7474:7474"
    neo["Config"]["Env"] = ["NEO4J_AUTH=neo4j/" + environment["NEO4J_PASSWORD"]]
    for container, source, destination, writable in (
        (app, "state_dir", "/app/data", True), (neo, "neo4j_dir", "/data", True),
        (proxy, "control_dir", "/control", False),
        (proxy, "tls_dir", "/etc/nginx/tls", False),
    ):
        container["Mounts"].append({"Type": "bind", "Source": config[source],
                                    "Destination": destination, "RW": writable})
    manifest = json.loads((Path(__file__).resolve().parents[1] /
                           "deploy/staging/compose.yaml").read_text())["services"]["proxy"]
    proxy["Config"]["Cmd"] = [part.replace("$$", "$") for part in manifest["command"]]
    proxy["Config"]["Entrypoint"] = manifest["entrypoint"]
    proxy["Mounts"].append({"Type": "bind", "Source": "/operator/reviewed-template",
                            "Destination": "/etc/nginx/nginx.conf.template", "RW": False})
    rendered = (Path(__file__).resolve().parents[1] /
                "deploy/staging/nginx.conf.template").read_bytes()
    rendered = rendered.replace(b"${STAGING_HOSTNAME}", b"staging.example.org").replace(
        b"${STAGING_OPERATOR_IP}", b"192.0.2.8")
    proxy["_H1NginxHash"] = hashlib.sha256(rendered).hexdigest()
    proxy["_H1ProxyWorkers"] = ["101"]
    proxy["Config"]["Env"] = ["STAGING_HOSTNAME=staging.example.org",
                              "STAGING_OPERATOR_IP=192.0.2.8"]
    proxy["NetworkSettings"]["Networks"]["forge_ingress"] = {}
    proxy["_H1Networks"]["forge_ingress"] = False
    proxy["HostConfig"]["PortBindings"] = {"443/tcp": [{"HostPort": "443"}]}
    return result


def test_load_private_config(config, tmp_path):
    assert load_config(write_config(tmp_path, config)) == config


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(origin="https://user:secret@staging.example.org"),
    lambda c: c.update(origin="https://staging.example.org/"),
    lambda c: c.update(usage_request_limit=0),
    lambda c: c.update(usage_window_seconds=59),
    lambda c: c.update(workspace_id="a" * 49),
    lambda c: c.update(state_dir="/tmp/with space"),
    lambda c: c["bindings"].update(enabled_workspace_count=9),
    lambda c: c["images"].update(app="example/app:latest"),
    lambda c: c.update(unexpected="must reject"),
    lambda c: c.update(probe_address="0.0.0.0"),
])
def test_invalid_bindings_fail_closed(config, tmp_path, mutate):
    mutate(config)
    with pytest.raises(ConfigError, match="invalid_config"):
        load_config(write_config(tmp_path, config))


@pytest.mark.parametrize("unsafe", ["mode", "parent", "symlink", "hardlink", "size", "fifo"])
def test_private_file_safety(tmp_path, unsafe):
    tmp_path.chmod(0o700)
    path = tmp_path / "private"
    path.write_text("private-content")
    path.chmod(0o600)
    if unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "parent":
        tmp_path.chmod(0o755)
    elif unsafe == "symlink":
        other = tmp_path / "other"
        path.rename(other)
        path.symlink_to(other)
    elif unsafe == "hardlink":
        os.link(path, tmp_path / "other")
    elif unsafe == "size":
        path.write_text("x" * 65537)
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    with pytest.raises(ConfigError, match="unsafe_file"):
        read_private_file(path)


def test_runtime_resolved_checks_pass(config):
    checks = preflight.inspect_containers(config, snapshot(config))
    assert all(item["status"] == "pass" for item in checks if
               not item["id"].startswith("boundary."))


@pytest.mark.parametrize(("mutation", "failed_id"), [
    (lambda s: s.append(copy.deepcopy(s[0])), "runtime.app.singleton"),
    (lambda s: s[0]["Config"]["Env"].append("KIMI_API_KEY=forbidden"),
     "runtime.app.provider_free"),
    (lambda s: s[0]["Config"].update(User="root"), "runtime.app.user"),
    (lambda s: s[0]["Config"].update(Cmd=["uvicorn", "src.main:app", "--workers", "2"]),
     "runtime.app.command"),
    (lambda s: s[0]["Mounts"][0].update(Source="/wrong"), "runtime.app.state_mount"),
    (lambda s: s[1]["HostConfig"].update(PortBindings={"7687/tcp": [{"HostPort": "7687"}]}),
     "runtime.neo4j.private_ports"),
    (lambda s: s[0]["Config"].update(Env=["AUTH_MODE=local"]),
     "runtime.app.private_configuration"),
])
def test_resolved_runtime_rejects_unsafe(config, mutation, failed_id):
    containers = snapshot(config)
    mutation(containers)
    checks = {item["id"]: item for item in preflight.inspect_containers(config, containers)}
    assert checks[failed_id]["status"] == "fail"


def test_other_project_writer_rejected(config):
    containers = snapshot(config)
    other = copy.deepcopy(containers[0])
    other["Config"]["Labels"]["com.docker.compose.project"] = "other"
    containers.append(other)
    checks = preflight.inspect_containers(config, containers)
    assert next(c for c in checks if c["id"] ==
                "runtime.state_writer.singleton")["status"] == "fail"


def test_exact_local_mount_required(config, monkeypatch):
    for name in ("state_dir", "neo4j_dir", "control_dir", "tls_dir"):
        path = __import__("pathlib").Path(config[name])
        path.mkdir(mode=0o700)
    lines = [f'1 0 8:1 / {config[name]} rw - ext4 /dev/example rw'
             for name in ("state_dir", "neo4j_dir")]
    checks = preflight.inspect_storage(config, "\n".join(lines))
    assert all(c["status"] == "pass" for c in checks if c["id"].endswith(".persistent"))
    missing = preflight.inspect_storage(config, "1 0 8:1 / / rw - ext4 /dev/example rw")
    assert all(c["status"] == "fail" for c in missing if c["id"].endswith(".persistent"))
    nfs = preflight.inspect_storage(config, "\n".join(lines).replace("ext4", "nfs"))
    assert all(c["status"] == "fail" for c in nfs if c["id"].endswith(".persistent"))


def test_unsupported_host_unverified(config, monkeypatch):
    monkeypatch.setattr(preflight.platform, "system", lambda: "Darwin")
    result = preflight.preflight(config)
    assert not result["ok"]
    assert result["checks"][-1]["status"] == "unverified"


def test_missing_docker_sanitized(config, monkeypatch):
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(preflight, "inspect_storage", lambda *_: [])
    def unavailable():
        raise OSError("private credential must not appear")
    monkeypatch.setattr(preflight, "docker_snapshot", unavailable)
    result = preflight.preflight(config)
    assert not result["ok"]
    assert "credential" not in json.dumps(result)


def test_cli_error_sanitized(tmp_path, capsys):
    assert preflight.main(["--config", str(tmp_path / "missing"), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {"version": 1, "ok": False, "checks": [{
        "id": "config.bindings", "status": "fail", "reason": "invalid_private_config"}]}


@pytest.mark.parametrize(("mutation", "failed_id"), [
    (lambda s: s[0]["HostConfig"].update(Privileged=True), "runtime.app.isolation"),
    (lambda s: s[0]["HostConfig"].update(ReadonlyRootfs=False), "runtime.app.isolation"),
    (lambda s: s[0]["_H1Networks"].update(forge_private=False), "runtime.app.network"),
    (lambda s: s[1]["Config"].update(Env=["NEO4J_AUTH=none"]), "runtime.neo4j.credentials"),
    (lambda s: s[2]["Config"].update(Env=[]), "runtime.proxy.operator_binding"),
    (lambda s: s[0]["Mounts"].append({"Type": "bind", "Source": "/other",
                                     "Destination": "/app/src", "RW": False}),
     "runtime.app.state_mount"),
])
def test_runtime_isolation_enforced(config, mutation, failed_id):
    containers = snapshot(config)
    mutation(containers)
    checks = preflight.inspect_containers(config, containers)
    assert next(item for item in checks if item["id"] == failed_id)["status"] == "fail"


def test_duplicate_json_key_rejected(config, tmp_path):
    path = write_config(tmp_path, config)
    path.write_text(path.read_text().replace('{"version": 1,', '{"version": 1,"version": 1,'))
    with pytest.raises(ConfigError):
        load_config(path)


def test_parent_symlink_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    private = real / "private"
    private.write_text("invented")
    private.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ConfigError):
        read_private_file(link / "private")


def test_docker_snapshot_uses_only_read_only_local_commands(monkeypatch):
    calls = []
    def run(arguments, **kwargs):
        from types import SimpleNamespace
        calls.append((arguments, kwargs))
        return SimpleNamespace(stdout=b"")
    monkeypatch.setattr(preflight.subprocess, "run", run)
    assert preflight.docker_snapshot() == []
    assert calls[0][0] == ["docker", "--host", "unix:///var/run/docker.sock", "ps", "-aq"]
    assert calls[0][1]["env"] == {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent"}
    assert "shell" not in calls[0][1]



def test_cli_invalid_argument_not_echoed(capsys):
    assert preflight.main(["--credential-secret"]) == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert "credential-secret" not in output.out
    assert json.loads(output.out)["ok"] is False



def test_proxy_trust_address_requires_actual_ip(config):
    containers = snapshot(config)
    containers[2]["NetworkSettings"]["Networks"]["forge_private"]["IPAddress"] = "172.30.63.9"
    checks = preflight.inspect_containers(config, containers)
    assert next(c for c in checks if c["id"] == "runtime.proxy.private_address")["status"] == "fail"


@pytest.mark.parametrize("source_kind", ["parent", "child"])
def test_other_project_overlapping_writer_rejected(config, source_kind):
    containers = snapshot(config)
    other = copy.deepcopy(containers[0])
    other["Config"]["Labels"]["com.docker.compose.project"] = "other"
    state = Path(config["state_dir"])
    other["Mounts"][0]["Source"] = str(
        state.parent if source_kind == "parent" else state / "nested")
    containers.append(other)
    checks = preflight.inspect_containers(config, containers)
    assert next(c for c in checks if c["id"] ==
                "runtime.state_writer.singleton")["status"] == "fail"


@pytest.mark.parametrize(("mutation", "failed_id"), [
    (lambda s: s[2]["Config"].update(Cmd=["nginx"]), "runtime.proxy.command"),
    (lambda s: s[2]["Config"].update(Entrypoint=["changed"]), "runtime.proxy.command"),
    (lambda s: s[2]["Mounts"][-1].update(RW=True), "runtime.proxy.template"),
    (lambda s: s[2]["Mounts"][-1].update(Source="/missing"), "runtime.proxy.template"),
    (lambda s: s[2].update(_H1NginxHash="wrong"), "runtime.proxy.rendered_config"),
    (lambda s: s[2].update(_H1ProxyWorkers=["0"]), "runtime.proxy.worker_identity"),
    (lambda s: s[2]["Mounts"].append({"Type": "bind", "Source": "/etc",
                                     "Destination": "/etc/nginx", "RW": False}),
     "runtime.proxy.mount_overlays"),
])
def test_proxy_resolved_configuration(config, mutation, failed_id):
    containers = snapshot(config)
    mutation(containers)
    checks = preflight.inspect_containers(config, containers)
    assert next(c for c in checks if c["id"] == failed_id)["status"] == "fail"


def test_other_project_private_network_member(config):
    containers = snapshot(config)
    other = copy.deepcopy(containers[2])
    other["Config"]["Labels"]["com.docker.compose.project"] = "unrelated"
    containers.append(other)
    checks = preflight.inspect_containers(config, containers)
    assert next(c for c in checks if c["id"] ==
                "runtime.private_network.members")["status"] == "fail"


def test_unknown_project_service(config):
    containers = snapshot(config)
    other = copy.deepcopy(containers[2])
    other["Config"]["Labels"]["com.docker.compose.service"] = "relay"
    containers.append(other)
    checks = preflight.inspect_containers(config, containers)
    assert next(c for c in checks if c["id"] == "runtime.project.services")["status"] == "fail"


@pytest.mark.parametrize("origin", [1, [], {}, True, "https://staging.example.org:443"])
def test_origin_type_and_default_port_rejected(config, tmp_path, capsys, origin):
    config["origin"] = origin
    path = write_config(tmp_path, config)
    with pytest.raises(ConfigError):
        load_config(path)
    assert preflight.main(["--config", str(path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["checks"][0]["reason"] == "invalid_private_config"


def test_template_public_safe_file_and_symlink(tmp_path):
    path = tmp_path / "template"
    path.write_bytes(b"public template")
    path.chmod(0o644)
    assert preflight.read_template(path) == b"public template"
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        preflight.read_template(link)
    path.chmod(0o666)
    with pytest.raises(ValueError):
        preflight.read_template(path)


@pytest.mark.parametrize(("processes", "expected"), [
    (b"COMMAND\n/sbin/docker-init -- uvicorn src.main:app --workers 1\n"
     b"/usr/local/bin/python /usr/local/bin/uvicorn src.main:app --workers 1\n", 1),
    (b"COMMAND\n/sbin/docker-init -- uvicorn src.main:app --workers 1\n"
     b"/usr/local/bin/python /usr/local/bin/uvicorn src.main:app --workers 1\n"
     b"/usr/local/bin/python /usr/local/bin/uvicorn src.main:app --workers 1\n", 2),
    (b"COMMAND\n/tini -- python -m uvicorn src.main:app\n"
     b"/usr/local/bin/python3.11 -u -m uvicorn src.main:app\n", 1),
    (b"COMMAND\n/bin/sh -c 'uvicorn src.main:app'\n"
     b"/usr/local/bin/uvicorn src.main:app --workers 1\n", 1),
    (b"COMMAND\n/usr/bin/python -c 'print(uvicorn)' src.main:app\n", 0),
])
def test_api_process_identity_ignores_init_and_wrappers(processes, expected):
    assert preflight._api_process_count(processes) == expected


def test_two_actual_api_processes_fail_singleton(config):
    containers = snapshot(config)
    containers[0]["_H1ApiProcesses"] = preflight._api_process_count(
        b"COMMAND\n/sbin/docker-init -- uvicorn src.main:app --workers 1\n"
        b"/usr/local/bin/python /usr/local/bin/uvicorn src.main:app\n"
        b"/usr/local/bin/python /usr/local/bin/uvicorn src.main:app\n")
    checks = preflight.inspect_containers(config, containers)
    assert next(c for c in checks if c["id"] == "runtime.app.processes")["status"] == "fail"



def test_neo4j_runtime_identity_must_match_private_storage(config):
    containers = snapshot(config)
    containers[1]["Config"]["User"] = "root"
    checks = preflight.inspect_containers(config, containers)
    assert next(c for c in checks if c["id"] == "runtime.neo4j.user")["status"] == "fail"


def test_neo4j_mount_owner_must_match_container_uid(config, monkeypatch):
    from types import SimpleNamespace
    for name in ("state_dir", "neo4j_dir", "control_dir", "tls_dir"):
        Path(config[name]).mkdir(mode=0o700)
    original = Path.lstat
    def lstat(path):
        info = original(path)
        if str(path) == config["neo4j_dir"]:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=1000, st_gid=1000)
        return info
    monkeypatch.setattr(Path, "lstat", lstat)
    checks = preflight.inspect_storage(config, "")
    assert next(c for c in checks if c["id"] == "storage.neo4j_dir.permissions")["status"] == "fail"
