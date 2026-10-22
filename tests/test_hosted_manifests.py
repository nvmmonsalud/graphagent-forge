"""Offline policy checks for the unbound staging template, not host acceptance."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "staging"


def manifest():
    # JSON is a YAML subset; stdlib parsing avoids a new runtime/test dependency.
    return json.loads((DEPLOY / "compose.yaml").read_text())


def test_only_tls_ingress_published_and_data_services_isolated():
    config = manifest()
    services = config["services"]
    assert set(services) == {"app", "neo4j", "proxy"}
    assert config["networks"]["private"]["internal"] is True
    for name in ("app", "neo4j"):
        service = services[name]
        assert not service.get("ports")
        assert set(service["networks"]) == {"private"}
        assert not service.get("privileged")
        assert not service.get("network_mode")
        assert not service.get("build")
        assert service["restart"] == "no"
    assert services["proxy"]["ports"] == ["443:443"]
    assert set(services["proxy"]["networks"]) == {"private", "ingress"}


def test_api_process_and_mounts_preserve_private_state_boundary():
    services = manifest()["services"]
    app = services["app"]
    command = app["command"]
    proxy_ip = services["proxy"]["networks"]["private"]["ipv4_address"]
    assert app["user"] == "1000:1000"
    assert command[:2] == ["uvicorn", "src.main:app"]
    assert command[command.index("--workers") + 1] == "1"
    assert "--reload" not in command
    assert command[command.index("--forwarded-allow-ips") + 1] == proxy_ip
    assert "--no-access-log" in command
    assert app["read_only"] is True
    assert app["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=256m,mode=1777"]
    assert services["proxy"]["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=64m,mode=1777"]
    assert app["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in app["security_opt"]
    for name, target in (("app", "/app/data"), ("neo4j", "/data")):
        mounts = services[name]["volumes"]
        assert len(mounts) == 1
        assert mounts[0]["type"] == "bind"
        assert mounts[0]["target"] == target
        assert mounts[0]["bind"]["create_host_path"] is False
        assert "${" in mounts[0]["source"] and ":?" in mounts[0]["source"]
    env = app["environment"]
    assert env["AUTH_MODE"] == "private" and env["AUTH_COOKIE_SECURE"] == "1"
    assert env["STATE_DB_PATH"].startswith("/app/data/")
    assert env["AUTH_CONFIG_PATH"].startswith("/app/data/")
    assert env["ENABLE_API_DOCS"] == "0" and env["DEV_RELOAD"] == "0"
    for key in ("KIMI_API_KEY", "DAYTONA_API_KEY", "NOSANA_API_KEY", "NOSANA_EMBEDDING_URL"):
        assert env[key] == ""
    assert "NEO4J_PASSWORD" not in env
    assert len(app["env_file"]) == len(services["neo4j"]["env_file"]) == 1
    assert app["env_file"] != services["neo4j"]["env_file"]
    assert ":?" in app["env_file"][0] and ":?" in services["neo4j"]["env_file"][0]
    for service in services.values():
        assert ":?" in service["image"]  # no fallback tag or locally rebuilt image
        assert service["mem_limit"] and service["cpus"] and service["pids_limit"]
        assert service["stop_grace_period"]
        assert service["logging"]["driver"] == "none"


def test_proxy_maintenance_and_forwarded_identity_are_fail_closed():
    config = (DEPLOY / "nginx.conf.template").read_text()
    assert "if (-f /control/admitted)" in config
    assert config.index("set $maintenance 1;") < config.index("if (-f /control/admitted)")
    assert "if ($maintenance = 1) { return 503; }" in config
    assert "geo $operator_probe { default 0; ${STAGING_OPERATOR_IP} 1; }" in config
    assert "real_ip_header" not in config and "set_real_ip_from" not in config
    assert 'proxy_set_header X-Forwarded-For $remote_addr;' in config
    assert 'proxy_set_header X-Forwarded-Proto https;' in config
    assert 'proxy_set_header Forwarded "";' in config
    assert 'proxy_set_header Host $host;' in config
    assert 'proxy_set_header Origin $http_origin;' in config
    assert "client_max_body_size 4m;" in config
    assert "proxy_set_header Upgrade $http_upgrade;" in config
    assert "access_log off;" in config and "error_log /dev/null crit;" in config
    assert "listen 80" not in config
    # Restrict envsubst: otherwise nginx variables become empty on startup.
    command = manifest()["services"]["proxy"]["command"][0]
    assert "envsubst '$$STAGING_HOSTNAME $$STAGING_OPERATOR_IP'" in command
    proxy = manifest()["services"]["proxy"]
    assert all(mount["read_only"] for mount in proxy["volumes"])
