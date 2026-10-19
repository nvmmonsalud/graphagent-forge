"""Offline private-workspace provisioning and deletion; never prints credentials."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path

from dotenv import load_dotenv

from src.agent.state import StateDatabase
from src.graph.workspace_scope import ID_PATTERN, LABELS, WorkspaceNeo4jClient
from src.security.identity import IdentityStore


def _write_new(path: Path, text: str):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(text)


def provision(config: Path, token_output: Path, workspace: str, principal: str):
    if not ID_PATTERN.fullmatch(workspace) or not ID_PATTERN.fullmatch(principal):
        raise ValueError('invalid workspace or principal ID')
    if config == token_output:
        raise ValueError('config and token output must differ')
    for path in (config, token_output):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) & 0o077 or path.exists() or path.is_symlink()):
            raise ValueError('requires private directories and new output files')
        for ancestor in path.absolute().parents:
            if ancestor.is_symlink():
                raise ValueError('symlink paths are not supported')
    token = secrets.token_urlsafe(32)
    body = {'version': 1, 'workspaces': [{'id': workspace, 'title': workspace}],
            'principals': [{'id': principal,
                            'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
                            'memberships': {workspace: 'editor'}}]}
    _write_new(config, json.dumps(body, indent=2) + '\n')
    try:
        _write_new(token_output, token + '\n')
    except BaseException:
        config.unlink()
        raise


async def purge(state_path: Path, config: Path, workspace: str):
    if not ID_PATTERN.fullmatch(workspace):
        raise ValueError('invalid workspace ID')
    db = StateDatabase(state_path)
    await db.initialize()
    identity = IdentityStore(db, config, [])
    await identity.initialize()
    _, principals = await identity.configuration()
    if any(workspace in person['memberships'] for person in principals.values()):
        raise ValueError('remove all workspace memberships from config before purge')
    folder = state_path.absolute().parent / 'workspaces' / workspace
    if (folder.is_symlink() or not folder.is_dir()
            or any(parent.is_symlink() for parent in folder.parents)
            or stat.S_IMODE(folder.stat().st_mode) & 0o077):
        raise ValueError('workspace state directory missing or unsafe')
    lock_path = folder / '.runtime.lock'
    lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        graph = WorkspaceNeo4jClient(
            workspace_id=workspace, uri=os.getenv('NEO4J_URI', 'bolt://localhost:7687'),
            user=os.getenv('NEO4J_USER', 'neo4j'), password=os.getenv('NEO4J_PASSWORD', ''),
        )
        try:
            await graph.connect()
            async with graph.driver.session() as session:
                for label in sorted(LABELS):
                    await session.run(f'MATCH (n:{label}) DETACH DELETE n')
        finally:
            await graph.close()
        # Remove only known local state files. Never follow a link or recurse.
        for name in ('state.sqlite3', 'state.sqlite3-wal', 'state.sqlite3-shm'):
            path = folder / name
            if path.is_symlink():
                raise ValueError('unsafe workspace state file')
            path.unlink(missing_ok=True)
        await identity.audit('workspace.purge', 'completed', workspace_id=workspace)
    finally:
        os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'purge'))
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--principal', default='admin')
    parser.add_argument('--token-output', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if not args.apply:
        print('Dry run: no files, credentials or graph data changed. Use --apply to execute.')
        return
    load_dotenv()
    if args.action == 'init':
        if args.token_output is None:
            parser.error('init requires --token-output')
        provision(args.config, args.token_output, args.workspace, args.principal)
        print('Created private config and one-time token file; no credential printed.')
    else:
        if args.state is None:
            parser.error('purge requires --state')
        asyncio.run(purge(args.state, args.config, args.workspace))
        print('Purged disabled workspace graph/state; root audit metadata retained.')


if __name__ == '__main__':
    main()
