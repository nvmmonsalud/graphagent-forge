"""Anti-regression guard for the demo-facing docs (README.md, DEMO_PITCH.md).

These strings were either always false, or became false once other parts of
the demo-polish pass landed (e.g. a Daytona boot time claim, a stale node
count from a specific past demo run, a "screenshots as backup" instruction
that no longer matches the live-demo-first pitch). None of them should ever
reappear.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
DEMO_PITCH_PATH = REPO_ROOT / "DEMO_PITCH.md"
LICENSE_PATH = REPO_ROOT / "LICENSE"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
CLAUDE_MD_PATH = REPO_ROOT / "CLAUDE.md"
FRONTEND_PATH = REPO_ROOT / "frontend" / "index.html"

BANNED_CLAIMS = (
    "0.5s",
    "sub-second",
    "1m context",
    "kimi ai k3",
    "scoring",
    "set up before demo day",
    "485 nodes",
    "screenshots",
)

# Matches the scripted question wherever it's delimited by backticks or
# quotes (DEMO_PITCH.md style: `...`; frontend style: placeholder="...") —
# identified by content rather than a fixed surrounding phrase, since the
# exact wording around it ("Type:", "Type the placeholder question
# verbatim:", ...) isn't part of the contract.
QUESTION_RE = re.compile(r"[`\"']([^`\"']*sponsors[^`\"']*)[`\"']", re.I)


def _read(path: Path) -> str:
    if not path.exists():
        pytest.fail(f"{path} does not exist")
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def docs_text() -> str:
    return _read(README_PATH) + "\n" + _read(DEMO_PITCH_PATH)


@pytest.mark.parametrize("claim", BANNED_CLAIMS)
def test_banned_claim_absent(docs_text: str, claim: str) -> None:
    assert claim not in docs_text.lower(), f"banned claim {claim!r} found in README/DEMO_PITCH"


def test_license_exists_and_is_mit() -> None:
    text = _read(LICENSE_PATH)
    assert "MIT" in text


def test_env_example_documents_ingest_job_timeout() -> None:
    text = _read(ENV_EXAMPLE_PATH)
    assert "INGEST_JOB_TIMEOUT" in text


def test_env_example_documents_graphagent_api() -> None:
    text = _read(ENV_EXAMPLE_PATH)
    assert "GRAPHAGENT_API" in text


def test_claude_md_mentions_file_ingestion() -> None:
    text = _read(CLAUDE_MD_PATH)
    assert "ingest/file" in text or "file_extractor" in text


def test_scripted_demo_question_matches_frontend_placeholder() -> None:
    demo_pitch = _read(DEMO_PITCH_PATH)
    frontend = _read(FRONTEND_PATH)

    demo_match = QUESTION_RE.search(demo_pitch)
    # Anchored to the query-input field specifically — the file has several
    # other `placeholder="..."` attributes on unrelated inputs.
    frontend_match = re.search(
        r'id="query-input"\s+placeholder="([^"]+)"', frontend
    )

    assert demo_match, "DEMO_PITCH.md has no scripted question in the assumed format"
    assert frontend_match, "frontend/index.html has no query-input placeholder"
    assert demo_match.group(1) == frontend_match.group(1), (
        f"scripted question mismatch: DEMO_PITCH.md says {demo_match.group(1)!r}, "
        f"frontend placeholder says {frontend_match.group(1)!r}"
    )
