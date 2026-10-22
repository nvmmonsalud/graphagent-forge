"""Explicit opt-in; own a fresh DB and loopback app, never external services."""
import os
from pathlib import Path

import pytest

from hosted.harness import DisposableNeo4j, Hosted
from hosted.provider import FixtureProvider


@pytest.fixture(scope='session')
def hosted_database(tmp_path_factory):
    location = os.getenv('HOSTED_NEO4J_HOME')
    if not location:
        pytest.fail('Set HOSTED_NEO4J_HOME to a local Neo4j 5.26 distribution')
    if os.getenv('PYTEST_XDIST_WORKER'):
        pytest.fail('Connected acceptance does not support parallel pytest workers')
    directory = tmp_path_factory.mktemp('hosted-services')
    database = DisposableNeo4j(Path(location), directory / 'neo4j')
    try:
        database.start()
        yield database
    finally:
        database.close()


@pytest.fixture
def hosted(hosted_database, tmp_path):
    hosted_database.clear()
    with FixtureProvider() as provider:
        instance = Hosted(hosted_database, provider, tmp_path / 'private-app')
        try:
            instance.start()
            yield instance
        finally:
            provider.release.set()
            instance.close()
            hosted_database.clear()


@pytest.fixture
def hosted_browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        executable = os.getenv('BROWSER_EXECUTABLE')
        browser = playwright.chromium.launch(
            **({'executable_path': executable} if executable else {}))
        yield browser
        browser.close()
