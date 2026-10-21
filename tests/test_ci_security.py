"""Exercise destructive-test fencing and exact secret-review boundaries offline."""
from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.check_ci_integration import IMAGE, PASSWORD, validate_service


def service():
    return {
        "Id": "a" * 64,
        "Config": {"Image": IMAGE, "Env": [f"NEO4J_AUTH=neo4j/{PASSWORD}"]},
        "NetworkSettings": {"Ports": {
            "7687/tcp": [{"HostIp": "127.0.0.1", "HostPort": "17687"}],
        }},
        "Mounts": [{"Type": "volume", "Name": "b" * 64}],
        "State": {"Health": {"Status": "healthy"}},
    }


ENV = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
       "CI_NEO4J_SERVICE_ID": "a" * 64}


def test_accept_job_created_disposable_service():
    validate_service(ENV, service())


@pytest.mark.parametrize("key,value", [
    ("GITHUB_ACTIONS", "false"), ("RUNNER_ENVIRONMENT", "self-hosted"),
    ("CI_NEO4J_SERVICE_ID", "c" * 64), ("CI_NEO4J_SERVICE_ID", "--all"),
])
def test_refuse_untrusted_runner_or_identity(key, value):
    with pytest.raises(ValueError):
        validate_service({**ENV, key: value}, service())


@pytest.mark.parametrize("change", ["image", "port", "host", "mount", "password", "health"])
def test_refuse_external_or_persistent_service(change):
    container = deepcopy(service())
    if change == "image":
        container["Config"]["Image"] = "neo4j:latest"
    elif change == "port":
        container["NetworkSettings"]["Ports"]["7687/tcp"][0]["HostPort"] = "7687"
    elif change == "host":
        container["NetworkSettings"]["Ports"]["7687/tcp"][0]["HostIp"] = "0.0.0.0"
    elif change == "mount":
        container["Mounts"] = [{"Type": "bind", "Source": "/data"}]
    elif change == "password":
        container["Config"]["Env"] = []
    else:
        container["State"]["Health"]["Status"] = "starting"
    with pytest.raises(ValueError):
        validate_service(ENV, container)


def test_secret_review_cannot_cover_another_value_path_or_detector():
    import hashlib

    # Scanner is an optional CI tool, not an application/test runtime dependency.
    pytest.importorskip("detect_secrets")
    from scripts.check_ci_secrets import unreviewed

    finding = {"type": "Secret Keyword", "hashed_secret": "a" * 40, "line_number": 10}
    reviewed = [{"filename": "fixture.txt", **finding, "reason": "Reviewed synthetic fixture"}]
    assert unreviewed({"fixture.txt": [finding]}, reviewed) == []
    assert unreviewed({"other.txt": [finding]}, reviewed)
    assert unreviewed({"fixture.txt": [{**finding, "hashed_secret": "b" * 40}]}, reviewed)
    assert unreviewed({"fixture.txt": [{**finding, "type": "Other"}]}, reviewed)
    metadata = {**finding, "hashed_secret": hashlib.sha1(
        finding["hashed_secret"].encode(), usedforsecurity=False
    ).hexdigest()}
    assert unreviewed({".security/reviewed-secrets.json": [metadata]}, reviewed) == []
    assert unreviewed({".security/reviewed-secrets.json": [finding]}, reviewed)


def test_integration_child_scrubs_external_targets_and_provider_keys(monkeypatch, tmp_path):
    import json

    from scripts import check_ci_integration as runner

    monkeypatch.chdir(tmp_path)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("NEO4J_URI", "bolt://production.example:7687")
    monkeypatch.setenv("KIMI_API_KEY", "inherited-provider-value")
    monkeypatch.setattr(runner.subprocess, "check_output", lambda args: json.dumps([service()]))
    calls = []
    monkeypatch.setattr(runner.subprocess, "call", lambda args, env: calls.append((args, env)) or 0)
    assert runner.main() == 0
    args, env = calls[0]
    assert args[-2:] == ["-m", "integration"]
    assert env["NEO4J_URI"] == "bolt://127.0.0.1:17687"
    assert env["NEO4J_PASSWORD"] == PASSWORD
    assert "KIMI_API_KEY" not in env
    assert env["RESEARCH_DISPOSABLE_NEO4J"] == "1"
    assert all(env[f"M{number}_DISPOSABLE_NEO4J"] == "1" for number in range(1, 6))


def test_integration_refuses_dotenv_before_docker_inspection(monkeypatch, tmp_path):
    from scripts import check_ci_integration as runner

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("NEO4J_URI=bolt://production.example:7687")
    monkeypatch.setattr(
        runner.subprocess, "check_output", lambda args: pytest.fail("Docker inspected")
    )
    with pytest.raises(SystemExit, match=".env"):
        runner.main()
