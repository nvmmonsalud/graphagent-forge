"""Shared fixtures for the test suite."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Every test starts with no sponsor-service keys / auth configured.

    Keeps tests deterministic regardless of what a developer has in their
    shell or a stray `.env` load — routes/entity_parser/nosana tests all rely
    on these being absent by default and opt in with monkeypatch when needed.
    """
    for var in (
        "KIMI_API_KEY",
        "NOSANA_API_KEY",
        "NOSANA_EMBEDDING_URL",
        "DAYTONA_API_KEY",
        "API_KEY",
        "STATE_DB_PATH",
        "AUTH_MODE",
        "AUTH_CONFIG_PATH",
        "AUTH_COOKIE_SECURE",
        "GRAPHAGENT_TOKEN",
        "GRAPHAGENT_WORKSPACE",
        "INGEST_ATTEMPT_BUDGET",
        "USAGE_REQUEST_LIMIT",
        "USAGE_WINDOW_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def pytest_addoption(parser):
    parser.addoption('--browser', action='store_true', help='Run opt-in Chromium regressions')


def pytest_ignore_collect(collection_path, config):
    return 'browser' in collection_path.parts and not config.getoption('--browser')
