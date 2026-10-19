"""Research routes validate manifests and never echo payloads."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.agent.durable_jobs import DurableJobManager
from src.agent.research_jobs import ResearchJobs
from src.agent.state import StateDatabase
from src.api.bulk_routes import router
from src.api.routes import install_exception_handlers
from src.security.identity import IdentityStore
from src.security.middleware import WorkspaceMiddleware


@pytest.fixture
async def client():
    app = FastAPI()
    app.state.auth_mode = "local"
    app.state.neo4j_available = True
    app.state.research_jobs = SimpleNamespace(
        bulk=AsyncMock(return_value={"id": "batch", "items": []}),
        list_batches=AsyncMock(return_value=[]), cancel_batch=AsyncMock(return_value=None),
        create_refresh=AsyncMock(return_value={"id": "refresh"}),
        list_refreshes=AsyncMock(return_value=[]), run_refresh=AsyncMock(return_value=None),
        delete_refresh=AsyncMock(return_value=None),
    )
    app.include_router(router)
    install_exception_handlers(app)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as result:
        yield result, app


async def test_bulk_route_and_idempotency(client):
    http, app = client
    command = {"version": 1, "kind": "url", "url": "https://example.com"}
    response = await http.post("/api/research/bulk", json={"commands": [command]},
                               headers={"Idempotency-Key": "batch-key"})
    assert response.status_code == 202
    app.state.research_jobs.bulk.assert_awaited_once_with([command], "batch-key", None)
    assert (await http.get("/api/research/batches")).json() == {"batches": []}
    assert (await http.post("/api/research/batches/guess/cancel")).status_code == 404


async def test_refresh_invalid_actor_fields_and_missing_ids(client):
    http, app = client
    response = await http.post("/api/research/refreshes", json={
        "url": "https://example.com?secret=credential", "interval_seconds": 1,
        "_principal_id": "mallory",
    })
    assert response.status_code == 422
    assert "credential" not in response.text
    app.state.research_jobs.create_refresh.assert_not_awaited()
    assert (await http.post("/api/research/refreshes/guess/run")).status_code == 404
    assert (await http.delete("/api/research/refreshes/guess")).status_code == 404
    assert (await http.get("/api/research/refreshes")).json() == {"refreshes": []}


async def test_bulk_value_errors_sanitized(client):
    http, app = client
    app.state.research_jobs.bulk.side_effect = ValueError("private document")
    response = await http.post("/api/research/bulk", json={"commands": [{"private": "text"}]})
    assert response.status_code == 422
    assert "private document" not in response.text


@pytest.fixture
async def private_client(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    config_path = directory / "identities.json"
    tokens = {name: name + "x" * 40 for name in ("alice", "bob", "reader")}
    config_path.write_text(json.dumps({
        "version": 1, "workspaces": [{"id": key, "title": key} for key in ("a", "b")],
        "principals": [{"id": key,
                        "token_sha256": hashlib.sha256(value.encode()).hexdigest(),
                        "memberships": {"b" if key == "bob" else "a":
                                        "reader" if key == "reader" else "editor"}}
                       for key, value in tokens.items()],
    }))
    config_path.chmod(0o600)
    root_db = StateDatabase(directory / "root.sqlite3")
    await root_db.initialize()
    identity = IdentityStore(root_db, config_path, ["http://test"], secure=False)
    await identity.initialize()
    resources = {}
    for key in ("a", "b"):
        db = StateDatabase(directory / key / "state.sqlite3")
        await db.initialize()
        jobs = DurableJobManager(db, AsyncMock())
        await jobs.initialize()
        graph = AsyncMock()
        graph.get_source.return_value = None
        research = ResearchJobs(db, jobs, graph, AsyncMock(), identity, key)
        await research.initialize()
        resources[key] = SimpleNamespace(research_jobs=research, jobs=jobs,
                                         neo4j_available=True)
    app = FastAPI()
    app.state.auth_mode = "private"
    app.state.identity = identity
    app.state.workspace_runtime = SimpleNamespace(
        get=AsyncMock(side_effect=lambda key: resources[key]))
    app.include_router(router)
    app.add_middleware(WorkspaceMiddleware)

    def headers(name, workspace="a"):
        return {"Authorization": "Bearer " + tokens[name], "X-Workspace-ID": workspace}

    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        yield client, resources, headers, app.state.workspace_runtime


async def test_private_unauthenticated_and_reader_denied_before_services(private_client):
    http, resources, headers, runtime = private_client
    for path, body in (("/api/research/bulk", {"commands": [{"secret": "payload"}]}),
                       ("/api/research/refreshes", {"url": "https://example.com",
                                                  "interval_seconds": 3600})):
        assert (await http.post(path, json=body)).status_code == 401
        assert (await http.post(path, json=body, headers=headers("reader"))).status_code == 403
    runtime.get.assert_not_awaited()
    assert await resources["a"].jobs.list() == []


async def test_private_batches_schedules_scoped_and_actor_server_assigned(private_client):
    http, resources, headers, _ = private_client
    response = await http.post("/api/research/bulk", headers=headers("bob", "b"), json={
        "commands": [{"version": 1, "kind": "text", "source": "private-b", "text": "secret-b"}],
    })
    assert response.status_code == 202
    batch = response.json()
    assert (await http.post("/api/research/batches/" + batch["id"] + "/cancel",
                            headers=headers("alice"))).status_code == 404
    assert (await http.get("/api/research/batches", headers=headers("alice"))).json() == {
        "batches": []}
    command = await resources["b"].research_jobs.db.run(lambda c: c.execute(
        "SELECT command FROM durable_jobs").fetchone()[0])
    assert json.loads(command)["_principal_id"] == "bob"
    assert json.loads(command)["_workspace_id"] == "b"
    response = await http.post("/api/research/refreshes", headers=headers("bob", "b"), json={
        "url": "https://example.com?secret=private-b", "interval_seconds": 3600,
    })
    assert response.status_code == 201
    refresh_id = response.json()["id"]
    for method, path in (("POST", f"/api/research/refreshes/{refresh_id}/run"),
                         ("DELETE", f"/api/research/refreshes/{refresh_id}")):
        assert (await http.request(method, path, headers=headers("alice"))).status_code == 404
    denied = await http.get("/api/research/refreshes", headers=headers("alice", "b"))
    assert denied.status_code == 403
    assert "private-b" not in denied.text
