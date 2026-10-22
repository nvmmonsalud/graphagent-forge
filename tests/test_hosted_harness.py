"""Offline regressions for harness isolation before it can touch any database."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from hosted.harness import DisposableNeo4j, environment


def test_service_environment_does_not_inherit_operator_configuration(monkeypatch):
    for key in ('NEO4J_URI', 'KIMI_API_KEY', 'OPENAI_API_KEY', 'DAYTONA_API_KEY',
                'NOSANA_API_KEY', 'HTTPS_PROXY', 'PYTHONPATH', 'JAVA_TOOL_OPTIONS'):
        monkeypatch.setenv(key, 'operator-value-must-not-propagate')
    assert set(environment()) <= {'PATH', 'HOME', 'LANG', 'JAVA_HOME'}


def test_database_owner_probe_refuses_a_different_data_directory(tmp_path):
    instance = object.__new__(DisposableNeo4j)
    instance.directory = tmp_path
    instance.process = SimpleNamespace(poll=lambda: None)
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    session.run.return_value.single.return_value = {'value': '/different/database'}
    with pytest.raises(RuntimeError, match='different data directory'):
        instance.verify_owner(driver)
    assert session.run.call_count == 1
    assert 'SHOW SETTINGS' in session.run.call_args.args[0]


def test_database_clear_checks_ownership_before_any_destructive_query(tmp_path):
    instance = object.__new__(DisposableNeo4j)
    instance.directory = tmp_path
    instance.process = SimpleNamespace(poll=lambda: 1)
    driver = MagicMock()
    instance.driver = lambda: driver
    with pytest.raises(RuntimeError, match='not running'):
        instance.clear()
    driver.__enter__.return_value.session.assert_not_called()
