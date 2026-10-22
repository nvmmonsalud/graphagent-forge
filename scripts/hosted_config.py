"""Strict private operator bindings; never reads dotenv or application modules."""

import ipaddress
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Fixed, safe diagnostic code only."""


def read_private_file(path: Path, max_bytes: int = 65536) -> bytes:
    """Read a small owner-only regular file without following its final symlink."""
    descriptor = None
    try:
        path = path.absolute()
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in path.parts[1:-1]:
                if component in (".", ".."):
                    raise ConfigError("unsafe_file")
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=parent)
                os.close(parent)
                parent = child
            directory = os.fstat(parent)
            if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
                raise ConfigError("unsafe_file")
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
        finally:
            os.close(parent)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > max_bytes):
            raise ConfigError("unsafe_file")
        data = os.read(descriptor, max_bytes + 1)
        if len(data) > max_bytes:
            raise ConfigError("unsafe_file")
        return data
    except (OSError, ValueError) as exc:
        raise ConfigError("unsafe_file") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


BINDINGS = {
    "host_provider", "region", "instance_resources", "domain_dns_owner", "tls_renewal",
    "storage_capacity", "monthly_hosting_ceiling", "enabled_workspace_count",
    "deployment_operator", "backup_destination", "backup_encryption", "backup_access",
    "recovery_point_objective", "recovery_time_objective", "source_commit",
}
FIELDS = {
    "version", "origin", "workspace_id", "usage_request_limit", "usage_window_seconds",
    "compose_project", "images", "state_dir", "neo4j_dir", "control_dir", "tls_dir",
    "probe_address", "bindings",
}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("invalid_config")
        result[key] = value
    return result


def load_config(path: Path) -> dict:
    """Return validated bindings, raising ConfigError without operator data on error."""
    try:
        config = json.loads(read_private_file(path), object_pairs_hook=_object)
        if not isinstance(config, dict) or set(config) != FIELDS or config["version"] != 1:
            raise ConfigError("invalid_config")
        if type(config["version"]) is not int:
            raise ConfigError("invalid_config")
        for name in ("compose_project",):
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", config[name]):
                raise ConfigError("invalid_config")
        if not re.fullmatch(r"[a-z0-9_-]{1,48}", config["workspace_id"]):
            raise ConfigError("invalid_config")
        if not isinstance(config["origin"], str):
            raise ConfigError("invalid_config")
        origin = urlsplit(config["origin"])
        if (origin.scheme != "https" or not origin.hostname or origin.username
                or origin.password or origin.path or origin.query or origin.fragment
                or config["origin"] != f"https://{origin.netloc}" or origin.port is not None
                or not re.fullmatch(r"[a-z0-9.-]+", origin.netloc)):
            raise ConfigError("invalid_config")
        for name in ("usage_request_limit", "usage_window_seconds"):
            if type(config[name]) is not int or not 0 < config[name] <= 2147483647:
                raise ConfigError("invalid_config")
        if config["usage_request_limit"] > 1_000_000:
            raise ConfigError("invalid_config")
        if not 60 <= config["usage_window_seconds"] <= 86400:
            raise ConfigError("invalid_config")
        paths = []
        for name in ("state_dir", "neo4j_dir", "control_dir", "tls_dir"):
            value = config[name]
            if (not isinstance(value, str) or not value.startswith("/")
                    or any(char.isspace() or ord(char) < 32 or ord(char) == 127
                           for char in value)):
                raise ConfigError("invalid_config")
            path_value = Path(value)
            if str(path_value) != value or ".." in path_value.parts or value == "/":
                raise ConfigError("invalid_config")
            paths.append(path_value)
        if any(a == b or a in b.parents or b in a.parents
               for index, a in enumerate(paths) for b in paths[index + 1:]):
            raise ConfigError("invalid_config")
        address = ipaddress.ip_address(config["probe_address"])
        if address.is_unspecified or address.is_multicast:
            raise ConfigError("invalid_config")
        images = config["images"]
        if not isinstance(images, dict) or set(images) != {"app", "neo4j", "proxy"}:
            raise ConfigError("invalid_config")
        for value in images.values():
            if not isinstance(value, str) or not re.fullmatch(
                    r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", value):
                raise ConfigError("invalid_config")
        bindings = config["bindings"]
        if not isinstance(bindings, dict) or set(bindings) != BINDINGS:
            raise ConfigError("invalid_config")
        for key, value in bindings.items():
            if key == "enabled_workspace_count":
                if type(value) is not int or not 0 < value <= 8:
                    raise ConfigError("invalid_config")
            elif not isinstance(value, str) or not value.strip() or len(value) > 512 or any(
                    ord(char) < 32 or ord(char) == 127 for char in value):
                raise ConfigError("invalid_config")
        if not re.fullmatch(r"[a-f0-9]{40}", bindings["source_commit"]):
            raise ConfigError("invalid_config")
        return config
    except (TypeError, KeyError, ValueError, UnicodeError, RecursionError) as exc:
        raise ConfigError("invalid_config") from exc
