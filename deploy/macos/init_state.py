"""One-shot root init inside Docker; application itself runs as UID 1000."""
import os
from pathlib import Path

state = Path('/app/data')
source = Path('/bootstrap/auth.json').read_bytes()
target = state / 'auth.json'
if target.exists():
    if target.read_bytes() != source:
        raise SystemExit('Existing identity differs; refusing to replace persistent credentials.')
else:
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(source)
os.chown(state, 1000, 1000)
os.chmod(state, 0o700)
os.chown(target, 1000, 1000)
os.chmod(target, 0o600)
