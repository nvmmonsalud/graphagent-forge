"""Private provisioning never prints tokens; deletion requires offline access."""
import fcntl
import hashlib
import json
import os

import pytest

from scripts.manage_workspaces import main, provision, purge
from src.agent.state import StateDatabase
from src.security.identity import IdentityStore


@pytest.mark.asyncio
async def test_provision_private_files_and_reject_overwrite(tmp_path, capsys):
    folder = tmp_path / 'private'
    config, token_file = folder / 'auth.json', folder / 'token.txt'
    provision(config, token_file, 'research', 'alice')
    token = token_file.read_text().strip()
    assert len(token) >= 32
    assert config.stat().st_mode & 0o777 == 0o600
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert token not in config.read_text()
    body = json.loads(config.read_text())
    assert body['principals'][0]['token_sha256'] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in capsys.readouterr().out
    db = StateDatabase(folder / 'identity.sqlite3')
    await db.initialize()
    identity = IdentityStore(db, config, ['https://app.test'])
    await identity.initialize()
    with pytest.raises(ValueError):
        provision(config, token_file, 'research', 'alice')
    with pytest.raises(ValueError):
        await purge(folder / 'identity.sqlite3', config, 'research')


@pytest.mark.asyncio
async def test_disabled_workspace_purge_refuses_live_runtime_lock(tmp_path):
    folder = tmp_path / 'private'
    config, token_file = folder / 'auth.json', folder / 'token.txt'
    provision(config, token_file, 'research', 'alice')
    body = json.loads(config.read_text())
    body['principals'][0]['memberships'] = {}
    config.write_text(json.dumps(body))
    workspace = folder / 'workspaces' / 'research'
    workspace.mkdir(parents=True, mode=0o700)
    descriptor = os.open(workspace / '.runtime.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        with pytest.raises(BlockingIOError):
            await purge(folder / 'identity.sqlite3', config, 'research')
    finally:
        os.close(descriptor)


def test_admin_dryrun_changes_nothing(tmp_path, capsys):
    config = tmp_path / 'not-created' / 'auth.json'
    main(['init', '--config', str(config), '--workspace', 'research'])
    assert not config.exists()
    assert 'Dry run' in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize('origins,secure', [(['*'], '1'), ([], '1'),
                                           (['https://remote.test'], '0')])
async def test_private_startup_rejects_unsafe_cookie_origins(monkeypatch, origins, secure):
    from fastapi import FastAPI

    import src.main as module

    monkeypatch.setenv('AUTH_MODE', 'private')
    monkeypatch.setenv('AUTH_COOKIE_SECURE', secure)
    monkeypatch.setattr(module, 'ALLOWED_ORIGINS', origins)
    with pytest.raises(ValueError):
        async with module.lifespan(FastAPI()):
            pytest.fail('unsafe private startup succeeded')


@pytest.mark.asyncio
async def test_private_lifespan_never_initializes_legacy_graph(tmp_path, monkeypatch):
    from fastapi import FastAPI

    import src.main as module

    folder = tmp_path / 'private'
    config, token_file = folder / 'auth.json', folder / 'token.txt'
    provision(config, token_file, 'research', 'alice')
    monkeypatch.setenv('AUTH_MODE', 'private')
    monkeypatch.setenv('STATE_DB_PATH', str(folder / 'identity.sqlite3'))
    monkeypatch.setenv('AUTH_CONFIG_PATH', str(config))

    def forbidden(**kwargs):
        raise AssertionError('private mode touched legacy graph')

    monkeypatch.setattr(module, 'Neo4jClient', forbidden)
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import src.security.workspaces as workspaces

    runtime = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(neo4j_available=True)),
                              shutdown=AsyncMock())
    monkeypatch.setattr(workspaces, 'WorkspaceRuntime', lambda *args: runtime)
    app = FastAPI()
    async with module.lifespan(app):
        assert app.state.auth_mode == 'private'
        assert app.state.identity is not None
        assert app.state.workspace_runtime is not None
        runtime.get.assert_awaited_once_with('research')
