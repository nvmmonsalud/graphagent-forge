"""Private WS handshake and every-delivery authorization on real ASGI sockets."""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, WebSocket

from src.agent.state import StateDatabase
from src.main import websocket_graph
from src.security.identity import IdentityStore
from src.security.workspaces import WorkspaceConnectionManager, WorkspaceRuntime

ORIGIN = 'http://test'
TOKEN = 'private-websocket-test-token-' + 'x' * 40


@pytest.fixture
async def ws_boundary(tmp_path):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    config_path = directory / 'identities.json'
    config = {'version': 1, 'workspaces': [{'id': key, 'title': key} for key in ('a', 'b')],
              'principals': [{'id': 'alice',
                              'token_sha256': hashlib.sha256(TOKEN.encode()).hexdigest(),
                              'memberships': {'a': 'editor'}}]}

    def rewrite():
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)

    rewrite()
    db = StateDatabase(directory / 'root.sqlite3')
    await db.initialize()
    identity = IdentityStore(db, config_path, [ORIGIN], secure=False)
    await identity.initialize()
    cookie, auth = await identity.create_session(TOKEN)
    runtime = WorkspaceRuntime(identity, directory / 'root.sqlite3')
    managers = {key: WorkspaceConnectionManager(identity, key) for key in ('a', 'b')}
    resources = {key: SimpleNamespace(ws_manager=manager) for key, manager in managers.items()}
    runtime.get = AsyncMock(side_effect=lambda key: resources[key])
    app = FastAPI()
    app.state.auth_mode = 'private'
    app.state.workspace_runtime = runtime
    yield SimpleNamespace(app=app, runtime=runtime, managers=managers, identity=identity,
                          config=config, rewrite=rewrite, cookie=cookie, auth=auth)
    for manager in managers.values():
        await manager.shutdown()


class Socket:
    """ASGI websocket transport: acceptance and outgoing frames remain observable."""

    def __init__(self, app, *, cookie=None, workspace='a', origin=ORIGIN, bearer=None):
        headers = [(b'host', b'test')]
        if origin is not None:
            headers.append((b'origin', origin.encode()))
        if cookie is not None:
            headers.append((b'cookie', f'ga_session={cookie}; ga_workspace={workspace}'.encode()))
        elif workspace is not None:
            headers.append((b'cookie', f'ga_workspace={workspace}'.encode()))
        if bearer is not None:
            headers.append((b'authorization', ('Bearer ' + bearer).encode()))
        self.queue = asyncio.Queue()
        self.queue.put_nowait({'type': 'websocket.connect'})
        self.messages = []
        self.accepted = asyncio.Event()
        self.closed = asyncio.Event()
        self.websocket = WebSocket({'type': 'websocket', 'path': '/ws/graph',
                                    'headers': headers, 'query_string': b'',
                                    'scheme': 'ws', 'app': app, 'state': {}},
                                   self.queue.get, self.send)

    async def send(self, message):
        self.messages.append(message)
        if message['type'] == 'websocket.accept':
            self.accepted.set()
        if message['type'] == 'websocket.close':
            self.closed.set()

    def texts(self):
        return [item.get('text') for item in self.messages if item['type'] == 'websocket.send']

    def ping(self):
        self.queue.put_nowait({'type': 'websocket.receive', 'text': 'ping'})

    async def finish(self, task):
        self.queue.put_nowait({'type': 'websocket.disconnect', 'code': 1000})
        await asyncio.wait_for(task, 1)


@pytest.mark.parametrize('kwargs', [
    {}, {'cookie': 'forged'}, {'bearer': 'incorrect'}, {'bearer': TOKEN, 'workspace': 'b'},
    {'bearer': TOKEN, 'origin': 'http://evil.invalid'}, {'bearer': TOKEN, 'origin': None},
])
async def test_private_ws_denies_before_accept_and_before_resource_lookup(ws_boundary, kwargs):
    env = ws_boundary
    socket = Socket(env.app, **kwargs)
    await websocket_graph(socket.websocket)
    assert not socket.accepted.is_set()
    assert socket.messages == [{'type': 'websocket.close', 'code': 1008, 'reason': ''}]
    env.runtime.get.assert_not_awaited()


async def test_ws_broadcast_partition_and_membership_revocation(ws_boundary):
    env = ws_boundary
    socket = Socket(env.app, cookie=env.cookie)
    task = asyncio.create_task(websocket_graph(socket.websocket))
    await asyncio.wait_for(socket.accepted.wait(), 1)
    try:
        await env.managers['b'].broadcast({'type': 'graph_update', 'private': 'workspace-b'})
        assert socket.texts() == []
        await env.managers['a'].broadcast({'type': 'graph_update', 'private': 'workspace-a'})
        assert json.loads(socket.texts()[0])['private'] == 'workspace-a'
        env.config['principals'][0]['memberships'] = {}
        env.rewrite()
        await env.managers['a'].broadcast({'type': 'graph_update', 'private': 'must-not-send'})
        assert len(socket.texts()) == 1
        assert socket.closed.is_set()
        assert socket.websocket not in env.managers['a'].active_connections
    finally:
        await socket.finish(task)


@pytest.mark.parametrize('invalidate', ['expiry', 'logout', 'token_rotation'])
async def test_every_delivery_rechecks_credentials(ws_boundary, invalidate):
    env = ws_boundary
    socket = Socket(env.app, cookie=env.cookie)
    task = asyncio.create_task(websocket_graph(socket.websocket))
    await asyncio.wait_for(socket.accepted.wait(), 1)
    try:
        if invalidate == 'expiry':
            await env.identity.db.run(lambda c: c.execute('UPDATE auth_sessions SET expires=0'),
                                      write=True)
        elif invalidate == 'logout':
            await env.identity.revoke(env.auth)
        else:
            env.config['principals'][0]['token_sha256'] = hashlib.sha256(b'new-token').hexdigest()
            env.rewrite()
        await env.managers['a'].broadcast({'type': 'job_update', 'private': 'must-not-send'})
        assert socket.texts() == [] and socket.closed.is_set()
    finally:
        await socket.finish(task)


async def test_ping_rechecks_revocation_before_sending_pong(ws_boundary):
    env = ws_boundary
    socket = Socket(env.app, cookie=env.cookie)
    task = asyncio.create_task(websocket_graph(socket.websocket))
    await asyncio.wait_for(socket.accepted.wait(), 1)
    await env.identity.revoke(env.auth)
    socket.ping()
    await asyncio.wait_for(task, 1)
    assert socket.closed.is_set() and socket.texts() == []


async def test_idle_socket_is_revoked_without_waiting_for_next_broadcast(ws_boundary):
    env = ws_boundary
    socket = Socket(env.app, cookie=env.cookie)
    task = asyncio.create_task(websocket_graph(socket.websocket))
    await asyncio.wait_for(socket.accepted.wait(), 1)
    await env.identity.revoke(env.auth)
    await asyncio.wait_for(task, 5)
    assert socket.closed.is_set() and socket.texts() == []
