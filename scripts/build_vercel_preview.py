"""Build an allowlisted static display preview, never the application backend."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ('preview.js', 'vendor/d3.v7.min.js', 'vendor/README.md')
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; "
    "connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)
BANNER = (
    '<div id="static-preview-banner" role="status" style="position:fixed;top:0;left:0;'
    'right:0;z-index:20000;padding:12px;background:#30204f;color:white;'
    'text-align:center;font:14px system-ui">STATIC PREVIEW · Synthetic display data · '
    'Backend actions unavailable</div>'
)


def build(output: Path, commit: str, *, root: Path = ROOT) -> None:
    """Require an empty output directory so stale files cannot leak into a release."""
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('A full Git commit SHA is required')
    if output.is_symlink() or (output.exists() and any(output.iterdir())):
        raise ValueError('Output must be an empty directory')
    output.mkdir(parents=True, exist_ok=True)
    html = (root / 'frontend/index.html').read_text()
    if html.count('<head>') != 1 or html.count('<body>') != 1:
        raise ValueError('Expected one head and body element')
    # Remove decorative third-party font requests from the standalone artifact.
    html = re.sub(r"@import url\('https://fonts.googleapis.com/[^']*'\);", '', html)
    html = html.replace('<head>', '<head>\n<meta http-equiv="Content-Security-Policy" '
                        f'content="{CSP}">\n<script src="/preview.js"></script>', 1)
    html = html.replace('<body>', '<body style="padding-top:44px">' + BANNER, 1)
    (output / 'index.html').write_text(html)
    for name in FILES:
        source = root / 'frontend' / name
        if source.is_symlink() or not source.is_file():
            raise ValueError(f'Expected regular allowlisted file: {name}')
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    (output / 'build-manifest.json').write_text(json.dumps({
        'commit': commit, 'mode': 'static-synthetic-preview', 'backend': False,
        'files': ['index.html', *FILES, 'build-manifest.json'],
    }, indent=2) + '\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'public')
    parser.add_argument('--commit', default=os.getenv('VERCEL_GIT_COMMIT_SHA', ''))
    args = parser.parse_args()
    build(args.output, args.commit)


if __name__ == '__main__':
    main()
