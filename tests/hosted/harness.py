"""Own all service processes and state; never accept a developer database URI."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def private_file(path: Path, content: str):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(content)


def environment() -> dict[str, str]:
    # Deliberately exclude proxy settings, provider keys, database configuration,
    # Python startup hooks and Java options inherited from an operator shell.
    return {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'JAVA_HOME')
            if key in os.environ}


def stop(process, timeout=30):
    if process is None:
        return
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


class DisposableNeo4j:
    """A new data/config/run directory and an ownership probe before any write."""

    def __init__(self, distribution: Path, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(mode=0o700)
        self.port = free_port()
        self.uri = f'bolt://127.0.0.1:{self.port}'
        self.process = None
        self.output = None
        self.config = self.directory / 'conf'
        self.config.mkdir(mode=0o700)
        binary = distribution.resolve() / 'bin/neo4j'
        if not binary.is_file():
            raise RuntimeError('HOSTED_NEO4J_HOME must contain a Neo4j 5.26 distribution')
        self.binary = binary
        for name in ('user-logs.xml', 'server-logs.xml'):
            source = distribution / 'conf' / name
            if source.is_file():
                shutil.copyfile(source, self.config / name)
        settings = {
            'server.default_listen_address': '127.0.0.1',
            'server.bolt.listen_address': f'127.0.0.1:{self.port}',
            'server.bolt.advertised_address': f'127.0.0.1:{self.port}',
            'server.http.enabled': 'false', 'server.https.enabled': 'false',
            'dbms.security.auth_enabled': 'false', 'dbms.usage_report.enabled': 'false',
            'server.memory.heap.initial_size': '256m',
            'server.memory.heap.max_size': '512m', 'server.memory.pagecache.size': '128m',
            **{f'server.directories.{name}': str(self.directory / name)
               for name in ('data', 'logs', 'run')},
        }
        private_file(self.config / 'neo4j.conf',
                     '\n'.join(f'{key}={value}' for key, value in settings.items()) + '\n')

    def driver(self):
        return GraphDatabase.driver(self.uri, auth=('neo4j', 'disposable-fixture'),
                                    connection_timeout=1, max_connection_pool_size=2)

    def verify_owner(self, driver):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError('Owned disposable Neo4j process is not running')
        with driver.session() as session:
            row = session.run("SHOW SETTINGS YIELD name,value "
                              "WHERE name='server.directories.data' RETURN value").single()
            if row is None or Path(row['value']).resolve() != self.directory / 'data':
                raise RuntimeError('Refusing database with a different data directory')
            version = session.run('CALL dbms.components() YIELD versions '
                                  'RETURN versions[0] AS version').single()['version']
            if not version.startswith('5.26.'):
                raise RuntimeError('Connected acceptance requires Neo4j 5.26')

    def start(self):
        log_path = self.directory / 'launcher.log'
        private_file(log_path, '')
        self.output = log_path.open('w')
        env = {**environment(), 'NEO4J_CONF': str(self.config)}
        self.process = subprocess.Popen([str(self.binary), 'console'], env=env,
                                        stdout=self.output, stderr=subprocess.STDOUT,
                                        start_new_session=True, cwd=self.directory)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('Disposable Neo4j failed to start; logs stay private')
            try:
                with self.driver() as driver:
                    self.verify_owner(driver)
                return
            except (OSError, ConnectionError):
                pass
            except Exception as exc:
                # Driver availability errors are expected during startup. Ownership
                # errors must never be retried into a later destructive operation.
                if isinstance(exc, RuntimeError):
                    raise
            time.sleep(.2)
        raise RuntimeError('Disposable Neo4j startup timed out; logs stay private')

    def clear(self):
        with self.driver() as driver:
            self.verify_owner(driver)
            with driver.session() as session:
                session.run('MATCH (n) DETACH DELETE n').consume()

    def close(self):
        try:
            stop(self.process)
        finally:
            if self.output is not None:
                self.output.close()


class Hosted:
    """One actual private API process with persistent state across restarts."""

    def __init__(self, database, provider, directory):
        self.database = database
        self.provider = provider
        self.directory = directory
        self.directory.mkdir(mode=0o700)
        self.state = self.directory / 'state'
        self.state.mkdir(mode=0o700)
        self.config_path = self.state / 'auth.json'
        self.tokens = {name: secrets.token_urlsafe(32) for name in ('alice', 'bob', 'reader')}
        self.config = {
            'version': 1,
            'workspaces': [{'id': name, 'title': name.title()} for name in ('alpha', 'beta')],
            'principals': [
                {'id': name, 'token_sha256': hashlib.sha256(value.encode()).hexdigest(),
                 'memberships': ({'alpha': 'editor', 'beta': 'editor'} if name == 'alice' else
                                 {'beta': 'editor'} if name == 'bob' else {'alpha': 'reader'})}
                for name, value in self.tokens.items()
            ],
        }
        self.write_config()
        self.port = free_port()
        self.origin = f'http://127.0.0.1:{self.port}'
        self.process = None
        self.output = None
        self.generation = 0
        self.report = self.directory / 'network.json'
        self.barrier = self.directory / 'publication.barrier'

    def write_config(self):
        temporary = self.config_path.with_suffix('.new')
        private_file(temporary, json.dumps(self.config))
        temporary.replace(self.config_path)

    def revoke(self, principal, workspace):
        for item in self.config['principals']:
            if item['id'] == principal:
                item['memberships'].pop(workspace, None)
        self.write_config()

    def client(self, principal='alice', workspace='alpha'):
        return httpx.Client(base_url=self.origin, timeout=30, trust_env=False,
                            headers={'Authorization': 'Bearer ' + self.tokens[principal],
                                     'X-Workspace-ID': workspace})

    def login(self, client, principal='alice', workspace='alpha'):
        response = client.post('/api/auth/session', headers={'Origin': self.origin},
                               json={'token': self.tokens[principal], 'workspace_id': workspace})
        assert response.status_code == 200
        return response

    def start(self, *, provider_enabled=True, publication_barrier=False):
        self.generation += 1
        log = self.directory / f'app-{self.generation}.log'
        private_file(log, '')
        self.output = log.open('w')
        env = {
            **environment(), 'PYTHONPATH': os.pathsep.join((str(ROOT), str(ROOT / 'tests'))),
            'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1',
            'H1_APP_PORT': str(self.port),
            'H1_ALLOWED_PORTS': f'{self.database.port},{self.provider.port}',
            'H1_NETWORK_REPORT': str(self.report),
            'AUTH_MODE': 'private', 'STATE_DB_PATH': str(self.state / 'identity.sqlite3'),
            'AUTH_CONFIG_PATH': str(self.config_path), 'AUTH_COOKIE_SECURE': '0',
            'ALLOWED_ORIGINS': self.origin,
            'NEO4J_URI': self.database.uri, 'NEO4J_USER': 'neo4j',
            'NEO4J_PASSWORD': 'disposable-fixture',
            'KIMI_BASE_URL': self.provider.base_url,
            'KIMI_API_KEY': 'local-fixture-only' if provider_enabled else '',
            'USAGE_REQUEST_LIMIT': '8', 'USAGE_WINDOW_SECONDS': '86400',
            'INGEST_ATTEMPT_BUDGET': '100', 'RATE_LIMIT_MAX': '100',
            'SANDBOX_RATE_LIMIT_MAX': '100', 'LOG_LEVEL': 'WARNING',
        }
        if publication_barrier:
            self.barrier.unlink(missing_ok=True)
            env['H1_PUBLICATION_BARRIER'] = str(self.barrier)
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / 'tests/hosted/serve_app.py')], env=env,
            cwd=self.directory, stdout=self.output, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 40
        with httpx.Client(base_url=self.origin, trust_env=False, timeout=1) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError('Connected app failed to start; logs stay private')
                try:
                    response = client.get('/api/health')
                    if response.status_code == 200 and response.json().get('neo4j') is True:
                        if client.get('/api/auth/status').json().get('private') is not True:
                            raise RuntimeError('Connected app is not private')
                        return
                except httpx.TransportError:
                    pass
                time.sleep(.1)
        raise RuntimeError('Connected app startup timed out; logs stay private')

    def restart(self, *, provider_enabled=True, publication_barrier=False):
        self.close()
        self.start(provider_enabled=provider_enabled, publication_barrier=publication_barrier)

    def crash(self):
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)
        self.close()

    def close(self):
        try:
            stop(self.process)
        finally:
            self.process = None
            if self.output is not None:
                self.output.close()
                self.output = None

    def wait_job(self, client, job_id, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = client.get('/api/jobs/' + job_id)
            assert response.status_code == 200, f'job polling returned HTTP {response.status_code}'
            job = response.json()
            if job['status'] in ('succeeded', 'failed', 'cancelled'):
                return job
            time.sleep(.05)
        raise AssertionError('Connected job did not reach a terminal state')

    def network_counts(self):
        return json.loads(self.report.read_text())
