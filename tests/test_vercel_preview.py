"""Static deployments must contain only public display assets."""
import json
from pathlib import Path

import pytest

from scripts.build_vercel_preview import build

SHA = 'a' * 40
ROOT = Path(__file__).resolve().parents[1]


def test_preview_build_allowlist_and_provenance(tmp_path):
    output = tmp_path / 'public'
    build(output, SHA)
    manifest = json.loads((output / 'build-manifest.json').read_text())
    actual = {str(path.relative_to(output)) for path in output.rglob('*') if path.is_file()}
    assert actual == set(manifest['files'])
    assert actual == {'index.html', 'preview.js', 'vendor/d3.v7.min.js',
                      'vendor/README.md', 'build-manifest.json'}
    assert manifest['commit'] == SHA
    assert manifest['backend'] is False
    html = (output / 'index.html').read_text()
    assert 'STATIC PREVIEW · Synthetic display data · Backend actions unavailable' in html
    assert "connect-src 'none'" in html
    assert 'fonts.googleapis.com' not in html
    assert html.index('/preview.js') < html.index('/vendor/d3.v7.min.js')
    assert (output / 'vendor/d3.v7.min.js').read_bytes() == (
        ROOT / 'frontend/vendor/d3.v7.min.js').read_bytes()


def test_preview_rejects_stale_output_and_missing_commit(tmp_path):
    with pytest.raises(ValueError, match='commit SHA'):
        build(tmp_path / 'invalid', '')
    output = tmp_path / 'public'
    output.mkdir()
    (output / '.env').write_text('private sentinel')
    with pytest.raises(ValueError, match='empty directory'):
        build(output, SHA)
    assert (output / '.env').read_text() == 'private sentinel'


def test_preview_rejects_symlink_output(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    output = tmp_path / 'public'
    output.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match='empty directory'):
        build(output, SHA)
