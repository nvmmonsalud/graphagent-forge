"""Local credential preparation must preserve private state across restarts."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.macos_backend import prepare


def test_configuration_is_private_consistent_and_restart_safe(tmp_path):
    root = tmp_path / 'config'
    prepare(root)
    original = {p.name: p.read_bytes() for p in root.iterdir()}
    config = json.loads(original['auth.json'])
    token = original['editor-token.txt'].decode().strip()
    assert config['principals'][0]['token_sha256'] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in original['auth.json'].decode()
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in root.iterdir())
    assert root.stat().st_mode & 0o777 == 0o700
    password = original['app-db.env'].decode().strip().split('=', 1)[1]
    assert original['neo4j.env'].decode().strip() == 'NEO4J_AUTH=neo4j/' + password
    prepare(root)
    assert original == {p.name: p.read_bytes() for p in root.iterdir()}


def test_partial_config_is_not_overwritten(tmp_path):
    root = tmp_path / 'config'
    root.mkdir(mode=0o700)
    (root / 'auth.json').write_text('existing')
    with pytest.raises((OSError, ValueError)):
        prepare(root)
    assert (root / 'auth.json').read_text() == 'existing'


def test_symlink_and_public_directory_are_refused(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    link = tmp_path / 'link'
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError):
        prepare(link)
    root.chmod(0o755)
    with pytest.raises(ValueError):
        prepare(root)


def test_permissions_not_weakened_by_umask(tmp_path):
    previous = os.umask(0)
    try:
        prepare(tmp_path / 'config')
    finally:
        os.umask(previous)
    assert (tmp_path / 'config' / 'editor-token.txt').stat().st_mode & 0o777 == 0o600


def test_macos_topology_is_private_loopback_singleton():
    manifest = json.loads((Path(__file__).parents[1] / 'deploy/macos/compose.yaml').read_text())
    app = manifest['services']['app']
    assert app['ports'] == ['127.0.0.1:8000:8000']
    assert app['command'][app['command'].index('--workers') + 1] == '1'
    assert app['environment']['AUTH_MODE'] == 'private'
    assert app['environment']['AUTH_COOKIE_SECURE'] == '0'  # explicit loopback dev exception
    assert app['environment']['KIMI_API_KEY'] == ''
    assert manifest['networks']['private']['internal'] is True
    assert 'ports' not in manifest['services']['neo4j']
