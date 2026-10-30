#!/bin/sh
# Run on the Mac with Docker Desktop and Python 3.11+ installed.
set -eu
umask 077
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
config_dir="$HOME/Library/Application Support/GraphAgent Forge/local-backend"
cd "$repo_dir"
python3 -m scripts.macos_backend --directory "$config_dir"
export GRAPHAGENT_LOCAL_CONFIG="$config_dir"
# The private files are read by Compose, never sourced into this shell.
docker compose -p graphagent-mac-private -f deploy/macos/compose.yaml config --quiet
docker compose -p graphagent-mac-private -f deploy/macos/compose.yaml up --build -d
# Wait for private authentication boot, without logging responses or credentials.
python3 - <<'CHECK'
import http.client
import json
import time
for attempt in range(45):
    connection = http.client.HTTPConnection('127.0.0.1', 8000, timeout=2)
    try:
        connection.request('GET', '/api/auth/status')
        response = connection.getresponse()
        if response.status == 200 and json.loads(response.read(65536)).get('private') is True:
            break
    except (OSError, ValueError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    time.sleep(2)
else:
    raise SystemExit('Private app did not respond; check Docker Desktop and container status locally.')
CHECK
printf '%s\n' 'Open http://127.0.0.1:8000 and sign in with the token in:' "$config_dir/editor-token.txt"
printf '%s\n' 'Use only 127.0.0.1 (the configured browser origin). Providers are disabled.'
