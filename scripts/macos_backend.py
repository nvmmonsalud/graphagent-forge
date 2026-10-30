"""Prepare private, local-only Docker Desktop configuration without printing secrets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path


def prepare(directory: Path) -> None:
    directory = directory.expanduser().absolute()
    for parent in [*reversed(directory.parents), directory]:
        if parent.is_symlink():
            raise ValueError('configuration path must not contain symlinks')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('configuration directory must be owned by you and mode 0700')
    names = ('auth.json', 'editor-token.txt', 'app-db.env', 'neo4j.env')
    if any((directory / name).exists() or (directory / name).is_symlink() for name in names):
        for name in names:
            path = directory / name
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError('existing configuration must be complete and private')
        return  # Never rotate credentials implicitly on restart.
    token = secrets.token_urlsafe(32)
    password = secrets.token_hex(32)
    config = {'version': 1, 'workspaces': [{'id': 'local', 'title': 'Local research'}],
              'principals': [{'id': 'owner',
                              'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
                              'memberships': {'local': 'editor'}}]}
    contents = (json.dumps(config), token + '\n', 'NEO4J_PASSWORD=' + password + '\n',
                'NEO4J_AUTH=neo4j/' + password + '\n')
    for name, content in zip(names, contents, strict=True):
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.directory)
    except (OSError, ValueError):
        parser.exit(1, 'Local configuration unavailable; check directory and file permissions.\n')
    print('Local configuration ready. Credentials were not printed.')


if __name__ == '__main__':
    main()
