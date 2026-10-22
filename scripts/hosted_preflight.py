"""Read-only local Linux staging inspection. Never starts or changes services."""

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import stat
import subprocess
from pathlib import Path

from scripts.hosted_config import ConfigError, load_config

LOCAL_FILESYSTEMS = {"ext4", "xfs", "btrfs", "zfs"}
COMMAND = [
    # Comparison constant only; preflight never binds a listener.
    "uvicorn", "src.main:app", "--host", "0.0.0.0",  # nosec B104
    "--port", "8000", "--workers", "1",
    "--proxy-headers", "--forwarded-allow-ips", "172.30.63.2", "--no-access-log",
    "--timeout-graceful-shutdown", "180",
]


def check(identifier, passed, reason="binding_mismatch"):
    return {"id": identifier, "status": "pass" if passed else "fail",
            "reason": "verified" if passed else reason}


def unverified(identifier, reason):
    return {"id": identifier, "status": "unverified", "reason": reason}


def report(checks):
    return {"version": 1, "ok": all(item["status"] == "pass" for item in checks), "checks": checks}


def _mounts(text):
    mounts = {}
    for line in text.splitlines():
        before, after = line.split(" - ", 1)
        fields = before.split()
        point = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        mounts[point] = (after.split()[0], fields[5].split(","))
    return mounts


def inspect_storage(config, mountinfo):
    checks = []
    mounts = _mounts(mountinfo)
    for name in ("state_dir", "neo4j_dir", "control_dir", "tls_dir"):
        path = Path(config[name])
        try:
            info = path.lstat()
            safe = (not any(p.is_symlink() for p in (path, *path.parents))
                    and stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700)
            if name == "state_dir":
                safe = safe and info.st_uid == 1000 and info.st_gid == 1000
            if name == "neo4j_dir":
                safe = safe and info.st_uid == 7474 and info.st_gid == 7474
            if name == "control_dir":
                safe = safe and info.st_uid == 101 and info.st_gid == 101
            checks.append(check(f"storage.{name}.permissions", safe))
        except OSError:
            checks.append(unverified(f"storage.{name}.permissions", "path_unavailable"))
        if name in ("state_dir", "neo4j_dir"):
            mount = mounts.get(str(path))
            checks.append(check(f"storage.{name}.persistent", bool(
                mount and mount[0] in LOCAL_FILESYSTEMS and "rw" in mount[1]),
                "local_mount_required"))
    for filename in ("auth.json", "identity.sqlite3"):
        path = Path(config["state_dir"]) / filename
        try:
            info = path.lstat()
            checks.append(check(f"storage.{filename}.private_file",
                                stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                                and stat.S_IMODE(info.st_mode) == 0o600
                                and info.st_uid == 1000 and info.st_gid == 1000))
        except OSError:
            checks.append(unverified(f"storage.{filename}.private_file", "path_unavailable"))
    return checks



def _api_process_count(output):
    """Count executable identities, excluding init/wrapper command arguments."""
    count = 0
    for line in output.decode("utf-8").splitlines()[1:]:
        arguments = shlex.split(line)
        if not arguments:
            continue
        executable = Path(arguments[0]).name
        tail = arguments[1:]
        if executable == "uvicorn":
            launches_uvicorn = True
        elif re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", executable):
            while tail and tail[0] in ("-u", "-B", "-I", "-E", "-s", "-S"):
                tail = tail[1:]
            launches_uvicorn = bool(tail) and (Path(tail[0]).name == "uvicorn" or
                                               tail[:2] == ["-m", "uvicorn"])
        else:
            launches_uvicorn = False
        if launches_uvicorn and "src.main:app" in tail:
            count += 1
    return count

def docker_snapshot():
    """Use the local socket explicitly; ignore remote Docker contexts/environment."""
    prefix = ["docker", "--host", "unix:///var/run/docker.sock"]
    environment = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent"}
    listing = subprocess.run(prefix + ["ps", "-aq"], capture_output=True, timeout=15,
                             check=True, env=environment)
    if len(listing.stdout) > 65536:
        raise ValueError("invalid_snapshot")
    identifiers = listing.stdout.decode("ascii").split()
    if len(identifiers) > 256 or any(not re.fullmatch(r"[a-f0-9]{12,64}", value)
                                    for value in identifiers):
        raise ValueError("invalid_snapshot")
    if not identifiers:
        return []
    result = subprocess.run(prefix + ["inspect", *identifiers], capture_output=True,
                            timeout=15, check=True, env=environment)
    if len(result.stdout) > 4 * 1024 * 1024:
        raise ValueError("invalid_snapshot")
    snapshot = json.loads(result.stdout)
    if not isinstance(snapshot, list):
        raise ValueError("invalid_snapshot")
    networks = sorted({name for item in snapshot for name in
                       item.get("NetworkSettings", {}).get("Networks", {})})
    if any(not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", name) for name in networks):
        raise ValueError("invalid_snapshot")
    network_data = []
    if networks:
        inspected = subprocess.run(prefix + ["network", "inspect", *networks],
                                   capture_output=True, timeout=15, check=True, env=environment)
        if len(inspected.stdout) > 4 * 1024 * 1024:
            raise ValueError("invalid_snapshot")
        network_data = json.loads(inspected.stdout)
    internals = {item["Name"]: item.get("Internal") is True for item in network_data}
    for item in snapshot:
        item["_H1Networks"] = internals
        if (item.get("State", {}).get("Running") is True and item.get("Config", {}).get(
                "Labels", {}).get("com.docker.compose.service") == "app"):
            identifier = item.get("Id", "")
            if not re.fullmatch(r"[a-f0-9]{12,64}", identifier):
                raise ValueError("invalid_snapshot")
            processes = subprocess.run(prefix + ["top", identifier, "-eo", "args"],
                                       capture_output=True, timeout=15, check=True, env=environment)
            if len(processes.stdout) > 65536:
                raise ValueError("invalid_snapshot")
            item["_H1ApiProcesses"] = _api_process_count(processes.stdout)
        if (item.get("State", {}).get("Running") is True and item.get("Config", {}).get(
                "Labels", {}).get("com.docker.compose.service") == "proxy"):
            identifier = item.get("Id", "")
            if not re.fullmatch(r"[a-f0-9]{12,64}", identifier):
                raise ValueError("invalid_snapshot")
            # Read an existing file inside the inspected container; never create a temp file.
            rendered = subprocess.run(prefix + [
                "exec", identifier, "cat", "/tmp/nginx.conf"],  # nosec B108
                capture_output=True, timeout=15, check=True, env=environment)
            processes = subprocess.run(prefix + ["top", identifier, "-eo", "uid,args"],
                                       capture_output=True, timeout=15, check=True, env=environment)
            if len(rendered.stdout) > 65536 or len(processes.stdout) > 65536:
                raise ValueError("invalid_snapshot")
            item["_H1NginxHash"] = hashlib.sha256(rendered.stdout).hexdigest()
            workers = [line.split(None, 1)[0] for line in
                       processes.stdout.decode("utf-8").splitlines()[1:]
                       if "nginx: worker process" in line]
            item["_H1ProxyWorkers"] = workers
    return snapshot


def _env(container):
    result = {}
    for item in container["Config"].get("Env", []):
        key, value = item.split("=", 1)
        if key in result:
            raise ValueError("duplicate_environment")
        result[key] = value
    return result


def _binding(container, source, destination, writable):
    matches = [item for item in container.get("Mounts", [])
               if item.get("Destination") == destination]
    return (len(matches) == 1 and matches[0].get("Type") == "bind"
            and matches[0].get("Source") == source and matches[0].get("RW") is writable)



def _overlaps(source, state):
    if not isinstance(source, str) or not source.startswith("/"):
        return False
    first, second = Path(source).resolve(), Path(state).resolve()
    return first == second or first in second.parents or second in first.parents


def read_template(path):
    """Read public configuration without symlink or group-writable substitutions."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe_template")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            if (info.st_uid not in (0, os.getuid()) or
                    (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
                raise ValueError("unsafe_template")
        file_descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  dir_fd=descriptor)
        try:
            info = os.fstat(file_descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022 or
                    info.st_size > 65536):
                raise ValueError("unsafe_template")
            data = os.read(file_descriptor, 65537)
            if len(data) > 65536:
                raise ValueError("unsafe_template")
            return data
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _proxy_template(container):
    matches = [mount for mount in container.get("Mounts", []) if mount.get("Destination") ==
               "/etc/nginx/nginx.conf.template"]
    if len(matches) != 1 or matches[0].get("Type") != "bind" or matches[0].get("RW") is not False:
        return False
    try:
        reviewed = Path(__file__).resolve().parents[1] / "deploy/staging/nginx.conf.template"
        expected = read_template(reviewed)
        actual = read_template(matches[0].get("Source", ""))
        return hashlib.sha256(expected).digest() == hashlib.sha256(actual).digest()
    except (OSError, ValueError, TypeError):
        return False


def _proxy_render(container, config):
    try:
        reviewed = Path(__file__).resolve().parents[1] / "deploy/staging/nginx.conf.template"
        rendered = read_template(reviewed).replace(
            b"${STAGING_HOSTNAME}", config["origin"].removeprefix("https://").encode()).replace(
            b"${STAGING_OPERATOR_IP}", config["probe_address"].encode())
        return container.get("_H1NginxHash") == hashlib.sha256(rendered).hexdigest()
    except (OSError, ValueError):
        return False


def _proxy_command(container):
    # Compose escapes '$' as '$$'; inspect exposes the resulting literal command.
    reviewed = Path(__file__).resolve().parents[1] / "deploy/staging/compose.yaml"
    try:
        service = json.loads(read_template(reviewed))["services"]["proxy"]
        expected = [value.replace("$$", "$") for value in service["command"]]
        return (container["Config"].get("Cmd") == expected and
                container["Config"].get("Entrypoint") == service["entrypoint"])
    except (OSError, ValueError, KeyError, TypeError):
        return False

def inspect_containers(config, snapshot):
    checks = []
    running = [item for item in snapshot if item.get("State", {}).get("Running") is True]
    project = [item for item in running if item["Config"].get("Labels", {}).get(
        "com.docker.compose.project") == config["compose_project"]]
    services = {}
    checks.append(check("runtime.project.services", len(project) == 3 and {
        item["Config"].get("Labels", {}).get("com.docker.compose.service")
        for item in project} == {"app", "neo4j", "proxy"}))
    private_name = f'{config["compose_project"]}_private'
    attached = [item for item in running if private_name in item.get(
        "NetworkSettings", {}).get("Networks", {})]
    checks.append(check("runtime.private_network.members", len(attached) == 3 and
                        all(any(item is member for member in project) for item in attached)))
    for service in ("app", "neo4j", "proxy"):
        matches = [item for item in project if item["Config"].get("Labels", {}).get(
            "com.docker.compose.service") == service]
        checks.append(check(f"runtime.{service}.singleton", len(matches) == 1))
        if len(matches) == 1:
            services[service] = matches[0]
    # A container outside this Compose project can still write the same state.
    writers = [item for item in running if any(mount.get("RW") is True and
               _overlaps(mount.get("Source"), config["state_dir"])
               for mount in item.get("Mounts", []))]
    checks.append(check("runtime.state_writer.singleton", len(writers) == 1
                        and writers[0] is services.get("app")))
    for service, container in services.items():
        metadata = container["Config"]
        checks.append(check(f"runtime.{service}.image",
                            metadata.get("Image") == config["images"][service]))
        if service != "proxy":
            checks.append(check(f"runtime.{service}.private_ports", not any(
                container.get("HostConfig", {}).get("PortBindings", {}).values())))
            networks = container.get("NetworkSettings", {}).get("Networks", {})
            checks.append(check(f"runtime.{service}.network", len(networks) == 1
                                and next(iter(networks), "") ==
                                f'{config["compose_project"]}_private'
                                and container.get("_H1Networks", {}).get(
                                    f'{config["compose_project"]}_private') is True))
        private_network = container.get("NetworkSettings", {}).get("Networks", {}).get(
            f'{config["compose_project"]}_private', {})
        address = {"app": "172.30.63.3", "neo4j": "172.30.63.4", "proxy": "172.30.63.2"}[service]
        checks.append(check(f"runtime.{service}.private_address",
                            private_network.get("IPAddress") == address))
        host = container.get("HostConfig", {})
        checks.append(check(f"runtime.{service}.resource_bounds",
                            0 < host.get("Memory", 0) <= (2 * 1024**3)
                            and 0 < host.get("NanoCpus", 0) <= 2 * 10**9
                            and 0 < host.get("PidsLimit", 0) <= 256))
        minimum_stop = {"app": 210, "neo4j": 120, "proxy": 30}[service]
        checks.append(check(f"runtime.{service}.stop_budget",
                            metadata.get("StopTimeout", 0) >= minimum_stop))
    app = services.get("app")
    if app:
        environment = _env(app)
        expected = {
            "AUTH_MODE": "private", "AUTH_CONFIG_PATH": "/app/data/auth.json",
            "STATE_DB_PATH": "/app/data/identity.sqlite3", "AUTH_COOKIE_SECURE": "1",
            "ALLOWED_ORIGINS": config["origin"], "ENABLE_API_DOCS": "0", "DEV_RELOAD": "0",
            "USAGE_REQUEST_LIMIT": str(config["usage_request_limit"]),
            "USAGE_WINDOW_SECONDS": str(config["usage_window_seconds"]),
            "NEO4J_URI": "bolt://172.30.63.4:7687", "NEO4J_USER": "neo4j",
        }
        checks.append(check("runtime.app.private_configuration",
                            all(environment.get(key) == value for key, value in expected.items())
                            and environment.get("NEO4J_PASSWORD", "")
                            not in ("", "graphagent-dev")))
        checks.append(check("runtime.app.provider_free", not any(environment.get(key) for key in (
            "KIMI_API_KEY", "DAYTONA_API_KEY", "NOSANA_API_KEY", "NOSANA_EMBEDDING_URL"))))
        checks.append(check("runtime.app.user", app["Config"].get("User") in ("1000", "1000:1000")))
        checks.append(check("runtime.app.processes", app.get("_H1ApiProcesses") == 1))
        checks.append(check("runtime.app.command", app["Config"].get("Cmd") == COMMAND
                            and not app["Config"].get("Entrypoint")))
        checks.append(check("runtime.app.source_commit", app["Config"].get("Labels", {}).get(
            "org.opencontainers.image.revision") == config["bindings"]["source_commit"]))
        checks.append(check("runtime.app.state_mount",
                            _binding(app, config["state_dir"], "/app/data", True)
                            # Compare mount destinations only; never open a host temp path.
                            and all(mount.get("Destination") == "/app/data" or (
                                mount.get("Type") == "tmpfs" and mount.get("Destination") ==
                                "/tmp")  # nosec B108
                                    for mount in app.get("Mounts", []))))
        host = app.get("HostConfig", {})
        checks.append(check("runtime.app.isolation",
                            not host.get("Privileged") and host.get("ReadonlyRootfs") is True
                            and "ALL" in host.get("CapDrop", [])
                            and any(option in ("no-new-privileges", "no-new-privileges:true")
                                    for option in host.get("SecurityOpt", []))
                            and host.get("NetworkMode") not in ("host", "none")
                            and host.get("PidMode", "") != "host"
                            and host.get("Init") is True))
    if "neo4j" in services:
        checks.append(check("runtime.neo4j.user",
                            services["neo4j"]["Config"].get("User") in ("7474", "7474:7474")))
        if app:
            auth = _env(services["neo4j"]).get("NEO4J_AUTH", "")
            checks.append(check("runtime.neo4j.credentials",
                                bool(environment.get("NEO4J_PASSWORD"))
                                and auth == "neo4j/" + environment.get("NEO4J_PASSWORD", "")))
        checks.append(check("runtime.neo4j.data_mount",
                            _binding(services["neo4j"], config["neo4j_dir"], "/data", True)))
    if "proxy" in services:
        proxy = services["proxy"]
        checks.append(check("runtime.proxy.command", _proxy_command(proxy)))
        checks.append(check("runtime.proxy.template", _proxy_template(proxy)))
        checks.append(check("runtime.proxy.rendered_config", _proxy_render(proxy, config)))
        checks.append(check("runtime.proxy.worker_identity",
                            proxy.get("_H1ProxyWorkers") == ["101"]))
        allowed_mounts = {"/control", "/etc/nginx/tls", "/etc/nginx/nginx.conf.template"}
        # Compare mount destinations only; never open a host temp path.
        checks.append(check("runtime.proxy.mount_overlays", all(
            mount.get("Destination") in allowed_mounts or (
                mount.get("Type") == "tmpfs" and mount.get("Destination") ==
                "/tmp")  # nosec B108
            for mount in proxy.get("Mounts", []))))
        checks.append(check("runtime.proxy.control_mount",
                            _binding(proxy, config["control_dir"], "/control", False)))
        checks.append(check("runtime.proxy.tls_mount",
                            _binding(proxy, config["tls_dir"], "/etc/nginx/tls", False)))
        proxy_env = _env(proxy)
        checks.append(check("runtime.proxy.operator_binding",
                            proxy_env.get("STAGING_HOSTNAME") ==
                            config["origin"].removeprefix("https://").removesuffix(":443")
                            and proxy_env.get("STAGING_OPERATOR_IP") == config["probe_address"]))
        networks = proxy.get("NetworkSettings", {}).get("Networks", {})
        checks.append(check("runtime.proxy.network", set(networks) == {
            f'{config["compose_project"]}_private', f'{config["compose_project"]}_ingress'}
            and proxy.get("_H1Networks", {}).get(f'{config["compose_project"]}_private') is True
            and proxy.get("_H1Networks", {}).get(f'{config["compose_project"]}_ingress') is False))
        bindings = proxy.get("HostConfig", {}).get("PortBindings", {})
        checks.append(check("runtime.proxy.ingress_port", set(bindings) == {"443/tcp"}
                            and bool(bindings["443/tcp"]) and all(
                                item.get("HostPort") == "443" for item in bindings["443/tcp"])))
    return checks


def preflight(config):
    checks = [check("config.bindings", True)]
    if platform.system() != "Linux":
        return report(checks + [unverified("host.linux", "linux_host_required")])
    try:
        checks.extend(inspect_storage(config, Path("/proc/self/mountinfo").read_text()))
    except (OSError, ValueError, IndexError):
        checks.append(unverified("storage.inspection", "mount_inspection_unavailable"))
    try:
        checks.extend(inspect_containers(config, docker_snapshot()))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        checks.append(unverified("runtime.inspection", "docker_inspection_unavailable"))
    return report(checks)


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ConfigError("invalid_arguments")
    parser = Parser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
        result = preflight(load_config(args.config))
    except ConfigError:
        result = report([check("config.bindings", False, "invalid_private_config")])
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
