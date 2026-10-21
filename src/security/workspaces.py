"""Bounded private workspace resources, authorized commands and live delivery."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException, WebSocketDisconnect

from src.agent.commands import command_handler
from src.agent.core import GraphAgent
from src.agent.durable_history import SQLiteQueryHistory
from src.agent.durable_jobs import DurableJobManager
from src.agent.state import StateDatabase
from src.graph.workspace_scope import WorkspaceNeo4jClient, namespace

log = logging.getLogger(__name__)


class WorkspaceAccessRevoked(BaseException):
    """Abort a private ingest through ordinary progress callback error handling."""


class WorkspaceConnectionManager:
    def __init__(self, identity, workspace_id, *, max_connections=100):
        self.identity = identity
        self.workspace_id = workspace_id
        self.max_connections = max_connections
        self.active_connections = []
        self._pending = 0

    async def _authorize(self, websocket):
        auth = await self.identity.authenticate(websocket.headers, websocket.cookies)
        await self.identity.authorize(auth, self.workspace_id)
        return auth

    async def connect(self, websocket):
        await self._authorize(websocket)
        if len(self.active_connections) + self._pending >= self.max_connections:
            return False
        self._pending += 1
        try:
            await websocket.accept()
            self.active_connections.append(websocket)
            return True
        finally:
            self._pending -= 1

    def disconnect(self, websocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def _close(self, websocket, code=1008):
        self.disconnect(websocket)
        try:
            await asyncio.wait_for(websocket.close(code=code), 2)
        except Exception:
            pass

    async def send(self, websocket, message):
        try:
            # Re-authenticate the original cookie/bearer, including rotation,
            # expiry, logout and every current workspace membership check.
            await self._authorize(websocket)
            await asyncio.wait_for(websocket.send_text(message), 2)
        except Exception:
            await self._close(websocket)

    async def broadcast(self, data):
        message = json.dumps(data)
        await asyncio.gather(*(self.send(ws, message) for ws in list(self.active_connections)))

    async def shutdown(self):
        await asyncio.gather(*(self._close(ws, 1001) for ws in list(self.active_connections)))


class WorkspaceRuntime:
    def __init__(self, identity, state_path, *, neo4j_factory=None, agent_factory=None):
        self.identity = identity
        self.state_path = Path(state_path)
        self._neo4j_factory = neo4j_factory or WorkspaceNeo4jClient
        self._agent_factory = agent_factory or GraphAgent
        self._resources = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def get(self, workspace_id):
        namespace(workspace_id)  # path components are never caller-controlled strings
        workspaces, _ = await self.identity.configuration()
        if workspace_id not in workspaces:
            raise HTTPException(403, 'workspace access denied')
        async with self._lock:
            if self._closed:
                raise RuntimeError('workspace runtime closed')
            if workspace_id in self._resources:
                return self._resources[workspace_id]
            if len(self._resources) >= 8:
                raise HTTPException(503, 'workspace resources unavailable')
            resource = await self._create(workspace_id)
            self._resources[workspace_id] = resource
            return resource

    async def _create(self, workspace_id):
        path = self.state_path.parent / 'workspaces' / workspace_id / 'state.sqlite3'
        database = StateDatabase(path)
        await database._thread(lambda: database._check_paths(create=True))
        descriptor = os.open(path.parent / '.runtime.lock',
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        neo4j = None
        jobs = None
        agent = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            await database.initialize()
            manager = WorkspaceConnectionManager(self.identity, workspace_id)
            neo4j = self._neo4j_factory(
                uri=os.getenv('NEO4J_URI', 'bolt://localhost:7687'),
                user=os.getenv('NEO4J_USER', 'neo4j'),
                password=os.getenv('NEO4J_PASSWORD', ''), workspace_id=workspace_id,
            )
            available = False
            try:
                await neo4j.connect()
                await neo4j.init_schema()
                available = True
            except Exception:
                log.warning('Private workspace graph unavailable')
            agent = self._agent_factory(neo4j, ws_manager=manager)
            history = SQLiteQueryHistory(database)
            await history.initialize()
            legacy_handler = command_handler(agent)

            async def handle(command, progress, operation_id):
                async def check():
                    try:
                        if command.get('_workspace_id') != workspace_id:
                            raise HTTPException(403, 'workspace access denied')
                        principal = command.get('_principal_id')
                        if not principal:
                            raise HTTPException(403, 'workspace access denied')
                        await self.identity.authorize_principal(
                            principal, workspace_id, write=True,
                        )
                    except Exception:
                        await jobs.cancel(operation_id)
                        raise WorkspaceAccessRevoked() from None

                await check()

                async def checked_progress(stage):
                    await check()
                    await progress(stage)

                clean = {k: v for k, v in command.items() if not k.startswith('_')}
                from src.ingestion.graph_writer import ingestion_authorization

                context = ingestion_authorization.set(check)
                try:
                    return await legacy_handler(clean, checked_progress, operation_id)
                finally:
                    ingestion_authorization.reset(context)

            jobs = DurableJobManager(
                database, handle, broadcast=manager.broadcast,
                attempt_budget=int(os.getenv('INGEST_ATTEMPT_BUDGET', '100')),
            )
            await jobs.initialize()
            await jobs.start()
            resource = SimpleNamespace(
                neo4j=neo4j, agent=agent, jobs=jobs, history=history,
                ws_manager=manager, neo4j_available=available, storage_mode='sqlite',
                database=database, lock_descriptor=descriptor,
            )
            from src.agent.usage import UsageConfig, UsageStore
            resource.usage = UsageStore(database, config=UsageConfig.from_env())
            await resource.usage.initialize()
            from src.agent.research_services import initialize_research
            await initialize_research(resource, identity=self.identity, workspace_id=workspace_id)
            return resource
        except BaseException:
            if jobs is not None:
                await jobs.shutdown()
            if neo4j is not None and getattr(neo4j, 'driver', None) is not None:
                await neo4j.close()
            if agent is not None:
                await agent.nosana.aclose()
            os.close(descriptor)
            raise

    async def websocket(self, websocket):
        manager = None
        try:
            if websocket.headers.get('origin') not in self.identity.allowed_origins:
                raise HTTPException(403, 'origin denied')
            auth = await self.identity.authenticate(websocket.headers, websocket.cookies)
            workspace_id = websocket.cookies.get('ga_workspace')
            await self.identity.authorize(auth, workspace_id)
            resource = await self.get(workspace_id)
            manager = resource.ws_manager
            if not await manager.connect(websocket):
                await websocket.close(code=1013)
                return
            while True:
                try:
                    message = await asyncio.wait_for(websocket.receive_text(), 4)
                except TimeoutError:
                    await manager._authorize(websocket)
                    continue
                await manager._authorize(websocket)
                if message == 'ping':
                    await manager.send(websocket, json.dumps({'type': 'pong'}))
        except (HTTPException, WebSocketDisconnect):
            if manager is not None:
                await manager._close(websocket)
            else:
                await websocket.close(code=1008)
        except Exception:
            if manager is not None:
                await manager._close(websocket)
            else:
                await websocket.close(code=1008)
        finally:
            if manager is not None:
                manager.disconnect(websocket)

    async def shutdown(self):
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            resources = list(self._resources.values())
            self._resources.clear()

        async def close(resource):
            from src.agent.research_services import shutdown_research

            errors = []
            try:
                for cleanup in (
                    lambda: shutdown_research(resource), resource.jobs.shutdown,
                    resource.ws_manager.shutdown, resource.neo4j.close,
                    resource.agent.nosana.aclose,
                ):
                    try:
                        await cleanup()
                    except BaseException as error:
                        errors.append(error)
            finally:
                os.close(resource.lock_descriptor)
            if errors:
                raise errors[0]

        results = await asyncio.gather(*(close(resource) for resource in resources),
                                       return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            log.error('Private workspace shutdown encountered an error')
