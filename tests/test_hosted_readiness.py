"""Offline adversarial probes: transport failure, durable state and session cleanup."""
import json
from pathlib import Path

import httpx
import pytest

from scripts import hosted_readiness as readiness
from scripts.hosted_config import ConfigError

CONFIG = {"origin": "https://staging.example.invalid", "workspace_id": "probe",
          "usage_request_limit": 10, "usage_window_seconds": 3600}
# Generated-looking credentials are unnecessary: this public string is a fake test input.
TOKEN = "invented-reader-token-for-offline-tests"
SESSION = "invented-cookie-for-offline-tests-1234"
MEMBERSHIP = {"workspace": {"id": "probe", "role": "reader"},
              "workspaces": [{"id": "probe", "role": "reader"}]}


class Server:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.requests = []
        self.revoked = False

    def handle(self, request):
        key = (request.method, request.url.path)
        self.requests.append(request)
        if key in self.overrides:
            response = self.overrides[key]
            if callable(response):
                return response(request)
            return response
        headers = {"cache-control": "no-store"}
        if key == ("GET", "/api/auth/status"):
            body = {"private": True}
        elif key == ("POST", "/api/auth/session"):
            assert request.headers["origin"] == CONFIG["origin"]
            assert json.loads(request.content) == {"token": TOKEN, "workspace_id": "probe"}
            headers = [("cache-control", "no-store"),
                       ("set-cookie", f"ga_session={SESSION}; Path=/; Secure; HttpOnly; "
                        "SameSite=Strict"),
                       ("set-cookie", "ga_workspace=probe; Path=/; Secure; HttpOnly; "
                        "SameSite=Strict")]
            body = MEMBERSHIP
        elif key == ("GET", "/api/auth/workspaces"):
            assert "ga_session=" in request.headers["cookie"]
            if self.revoked:
                return httpx.Response(401, json={"detail": "authentication required"})
            body = MEMBERSHIP
        elif key == ("GET", "/api/graph/stats"):
            body = {"nodes": 0, "edges": 0, "entity_types": []}
        elif key == ("GET", "/api/usage"):
            body = {"storage_mode": "sqlite", "accounting_available": True,
                    "request_limit": 10, "window_seconds": 3600}
        elif key == ("DELETE", "/api/auth/session"):
            self.revoked = True
            body = {"ok": True}
        else:
            pytest.fail("Unexpected readiness endpoint")
        return httpx.Response(200, json=body, headers=headers)

    async def run(self):
        return await readiness.probe(CONFIG, TOKEN, transport=httpx.MockTransport(self.handle))


@pytest.mark.asyncio
async def test_real_probe_sequence_uses_cookie_reads_then_verifies_revocation():
    server = Server()
    result = await server.run()
    assert result["ok"]
    assert [item["id"] for item in result["checks"]] == [
        "private_auth", "reader_session", "identity_roundtrip", "graph_read",
        "durable_usage", "session_revoked"]
    assert len(server.requests) == 7
    assert not any("authorization" in request.headers for request in server.requests)
    assert all(request.url.host == "staging.example.invalid" for request in server.requests)
    assert server.revoked


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"storage_mode": "memory", "accounting_available": True,
     "request_limit": 10, "window_seconds": 3600},
    {"storage_mode": "sqlite", "accounting_available": False,
     "request_limit": 10, "window_seconds": 3600},
    {"storage_mode": "sqlite", "accounting_available": True,
     "request_limit": 0, "window_seconds": 3600},
    {"storage_mode": "sqlite", "accounting_available": True,
     "request_limit": 10, "window_seconds": 60},
])
async def test_memory_unavailable_or_mismatched_quota_fails_and_revokes(body):
    server = Server({("GET", "/api/usage"): httpx.Response(
        200, json=body, headers={"cache-control": "no-store"})})
    result = await server.run()
    assert not result["ok"]
    assert result["checks"][-2] == readiness.check(
        "durable_usage", False, "durable_quota_unavailable")
    assert server.revoked


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(503, json={"secret": TOKEN}),
    httpx.Response(200, content=b"not-json", headers={"cache-control": "no-store"}),
    httpx.Response(200, content=b"x" * 65537),
    httpx.Response(200, json={"nodes": True, "edges": 0},
                   headers={"cache-control": "no-store"}),
])
async def test_graph_failure_never_uses_cached_health_and_still_cleans_session(response):
    server = Server({("GET", "/api/graph/stats"): response})
    result = await server.run()
    assert not result["ok"]
    assert server.revoked
    assert "/api/health" not in [request.url.path for request in server.requests]
    assert TOKEN not in json.dumps(result)
    assert SESSION not in json.dumps(result)


@pytest.mark.asyncio
async def test_redirect_is_not_followed_and_credentials_are_not_sent():
    server = Server({("GET", "/api/auth/status"): httpx.Response(
        302, headers={"location": "https://outside.invalid/?private-value"})})
    result = await server.run()
    assert not result["ok"]
    assert len(server.requests) == 1
    assert b"token" not in server.requests[0].content
    assert "outside" not in json.dumps(result)


@pytest.mark.asyncio
async def test_local_auth_or_broad_reader_principal_fails_closed():
    local = Server({("GET", "/api/auth/status"): httpx.Response(
        200, json={"private": False}, headers={"cache-control": "no-store"})})
    assert not (await local.run())["ok"]
    assert len(local.requests) == 1
    broad = dict(MEMBERSHIP)
    broad["workspaces"] = [*MEMBERSHIP["workspaces"], {"id": "other", "role": "reader"}]
    server = Server({("GET", "/api/auth/workspaces"): httpx.Response(
        200, json=broad, headers={"cache-control": "no-store"})})
    result = await server.run()
    assert not result["ok"]
    assert server.revoked
    assert "/api/graph/stats" not in [request.url.path for request in server.requests]


@pytest.mark.asyncio
async def test_failed_logout_or_cookie_revocation_is_not_success():
    server = Server({("DELETE", "/api/auth/session"): httpx.Response(503)})
    result = await server.run()
    assert not result["ok"]
    assert result["checks"][-1] == readiness.check(
        "session_revoked", False, "revocation_unverified")


@pytest.mark.asyncio
async def test_transport_error_diagnostics_do_not_escape():
    def fail(request):
        raise httpx.ConnectError(TOKEN, request=request)

    server = Server({("GET", "/api/auth/status"): fail})
    result = await server.run()
    assert result == readiness.result([readiness.check("private_auth", False, "transport_failed")])
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize("attribute", ["Secure", "HttpOnly", "SameSite=Strict"])
def test_cookie_policy_rejects_missing_security_attribute(attribute):
    header = f"ga_session={SESSION}; Path=/; Secure; HttpOnly; SameSite=Strict"
    headers = [("set-cookie", header.replace("; " + attribute, "")),
               ("set-cookie", "ga_workspace=probe; Path=/; Secure; HttpOnly; "
                        "SameSite=Strict")]
    response = httpx.Response(200, headers=headers,
                              request=httpx.Request("GET", CONFIG["origin"]))
    jar = httpx.Cookies()
    jar.extract_cookies(response)
    assert not readiness._cookies_safe(jar, CONFIG["origin"])


def test_credential_file_is_private_and_bounded(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "reader.token"
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    assert readiness.read_token(path) == TOKEN
    path.chmod(0o644)
    with pytest.raises(ConfigError):
        readiness.read_token(path)
    path.chmod(0o600)
    path.write_text("x" * 514)
    with pytest.raises(ConfigError):
        readiness.read_token(path)


def test_invalid_cli_prints_only_fixed_json(capsys):
    assert readiness.main(["--unexpected", TOKEN]) == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert TOKEN not in output.out
    assert json.loads(output.out) == readiness.result([
        readiness.check("configuration", False, "invalid_private_inputs")])


def test_main_private_inputs_never_become_command_arguments(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(readiness, "load_config", lambda _path: CONFIG)
    monkeypatch.setattr(readiness, "read_token", lambda _path: TOKEN)

    async def fake_probe(config, token):
        assert config == CONFIG and token == TOKEN
        return readiness.result([readiness.check("probe", True)])

    monkeypatch.setattr(readiness, "probe", fake_probe)
    assert readiness.main(["--config", str(tmp_path / "config.json"),
                           "--credential-file", str(tmp_path / "reader.token"), "--json"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert TOKEN not in output.out
    assert str(Path(tmp_path)) not in output.out


@pytest.mark.asyncio
async def test_probe_against_actual_private_routes_and_sqlite(tmp_path):
    import hashlib
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import FastAPI

    from src.agent.state import StateDatabase
    from src.agent.usage import UsageConfig, UsageStore
    from src.api.routes import router
    from src.security.auth_routes import router as auth_router
    from src.security.identity import IdentityStore
    from src.security.middleware import WorkspaceMiddleware

    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    path = folder / "auth.json"
    path.write_text(json.dumps({
        "version": 1, "workspaces": [{"id": "probe", "title": "Probe"}],
        "principals": [{"id": "reader", "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                        "memberships": {"probe": "reader"}}],
    }))
    path.chmod(0o600)
    database = StateDatabase(folder / "identity.sqlite3")
    await database.initialize()
    identity = IdentityStore(database, path, [CONFIG["origin"]], secure=True)
    await identity.initialize()
    workspace_db = StateDatabase(folder / "workspace" / "state.sqlite3")
    await workspace_db.initialize()
    usage = UsageStore(workspace_db, config=UsageConfig(10, 3600))
    await usage.initialize()
    agent = AsyncMock()
    agent.get_graph_stats.return_value = {"nodes": 0, "edges": 0, "entity_types": []}
    resources = SimpleNamespace(neo4j_available=True, agent=agent, usage=usage)
    app = FastAPI()
    app.state.auth_mode = "private"
    app.state.identity = identity
    app.state.workspace_runtime = SimpleNamespace(get=AsyncMock(return_value=resources))
    app.include_router(auth_router)
    app.include_router(router, prefix="/api")
    app.add_middleware(WorkspaceMiddleware)
    output = await readiness.probe(CONFIG, TOKEN, transport=httpx.ASGITransport(app=app))
    assert output["ok"]
    agent.get_graph_stats.assert_awaited_once()
    retained = await database.run(lambda connection: connection.execute(
        "SELECT COUNT(*) FROM auth_sessions").fetchone()[0])
    assert retained == 0
    assert (await usage.snapshot())["storage_mode"] == "sqlite"


@pytest.mark.parametrize("origin", [1, [], {}, None, True])
def test_cli_malformed_origin_is_fixed_json(tmp_path, capsys, origin):
    from scripts.hosted_config import BINDINGS

    tmp_path.chmod(0o700)
    config = {
        "version": 1, **CONFIG, "origin": origin, "compose_project": "forge",
        "images": {name: f"example/{name}@sha256:" + "a" * 64
                   for name in ("app", "neo4j", "proxy")},
        **{name + "_dir": str(tmp_path / name) for name in ("state", "neo4j", "control", "tls")},
        "probe_address": "192.0.2.8",
        "bindings": {key: 1 if key == "enabled_workspace_count" else
                     "a" * 40 if key == "source_commit" else "operator-bound"
                     for key in BINDINGS},
    }
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    assert readiness.main(["--config", str(path), "--credential-file", "unused", "--json"]) == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == readiness.result([
        readiness.check("configuration", False, "invalid_private_inputs")])
