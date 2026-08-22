"""Guards for the README's "See it running" image section (WP3).

Keeps the docs and the committed assets in sync: every relative image
reference must resolve to a real file, images must stay small enough for a
README to load comfortably, and the banned-claims guard in
test_docs_claims.py gets a belt-and-braces check here too so this file fails
loudly on its own if the image section ever regresses.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"

#: Per-image cap referenced from the README.
MAX_IMAGE_BYTES = 400 * 1024
#: Cap on the total size of everything under assets/ui/.
MAX_TOTAL_UI_BYTES = 1536 * 1024  # 1.5 MB

MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _read_readme() -> str:
    assert README_PATH.exists(), "README.md does not exist"
    return README_PATH.read_text(encoding="utf-8")


def _relative_image_paths(text: str) -> list[str]:
    paths = []
    for _alt, path in MARKDOWN_IMAGE_RE.findall(text):
        # Skip absolute URLs (badges, external images) — only relative
        # in-repo paths are covered by this guard.
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", path):
            continue
        paths.append(path)
    return paths


def test_all_relative_images_exist() -> None:
    text = _read_readme()
    paths = _relative_image_paths(text)
    assert paths, "expected at least one relative image reference in README.md"
    for rel_path in paths:
        resolved = REPO_ROOT / rel_path
        assert resolved.exists(), f"README.md references missing image: {rel_path!r}"


def test_referenced_images_are_small() -> None:
    text = _read_readme()
    for rel_path in _relative_image_paths(text):
        resolved = REPO_ROOT / rel_path
        assert resolved.exists(), f"README.md references missing image: {rel_path!r}"
        size = resolved.stat().st_size
        assert size < MAX_IMAGE_BYTES, (
            f"{rel_path} is {size} bytes, over the {MAX_IMAGE_BYTES}-byte per-image cap"
        )


def test_ui_assets_total_size_under_cap() -> None:
    ui_dir = REPO_ROOT / "assets" / "ui"
    assert ui_dir.is_dir(), "assets/ui/ does not exist"
    pngs = sorted(ui_dir.glob("*.png"))
    assert pngs, "expected at least one PNG under assets/ui/"
    total = sum(p.stat().st_size for p in pngs)
    assert total < MAX_TOTAL_UI_BYTES, (
        f"assets/ui/*.png totals {total} bytes, over the {MAX_TOTAL_UI_BYTES}-byte cap"
    )


def test_no_screenshots_directory_reference() -> None:
    text = _read_readme()
    assert "assets/screenshots" not in text


def test_ci_badge_points_at_ci_workflow() -> None:
    text = _read_readme()
    assert "actions/workflows/ci.yml/badge.svg" in text
    assert "actions/workflows/ci.yml)" in text


def test_no_banned_plural_word() -> None:
    # Belt-and-braces alongside test_docs_claims.py's BANNED_CLAIMS check —
    # this file should fail on its own if the image section regresses,
    # without depending on the other test module's tuple staying intact.
    text = _read_readme()
    assert "screenshots" not in text.lower()
