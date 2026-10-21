"""Bounded request admission and reported Kimi usage; never stores user content."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

log = logging.getLogger(__name__)
_COUNTERS = (
    'ask_requests', 'sandbox_requests', 'reported_calls', 'missing_usage_calls',
    'prompt_tokens', 'completion_tokens',
)
_active_usage: ContextVar[UsageStore | None] = ContextVar('active_usage', default=None)


@dataclass(frozen=True)
class UsageConfig:
    request_limit: int = 0
    window_seconds: int = 3600

    def __post_init__(self):
        if (type(self.request_limit) is not int or not 0 <= self.request_limit <= 1_000_000
                or type(self.window_seconds) is not int or not 60 <= self.window_seconds <= 86400):
            raise ValueError('invalid usage quota settings')

    @classmethod
    def from_env(cls):
        try:
            return cls(int(os.getenv('USAGE_REQUEST_LIMIT', '0')),
                       int(os.getenv('USAGE_WINDOW_SECONDS', '3600')))
        except ValueError:
            raise ValueError('invalid usage quota settings') from None


class QuotaExceeded(Exception):
    """The shared fixed-window request allowance is exhausted."""


class UsageStore:
    """One row per resource, with atomic reservations across SQLite managers."""

    def __init__(self, db=None, *, config=None, clock=time.time):
        self.db = db
        self.config = config or UsageConfig()
        self.clock = clock
        self._lock = asyncio.Lock()
        self._memory = None
        self.accounting_available = True

    def _empty(self):
        return {'window': int(self.clock()) // self.config.window_seconds,
                'window_seconds': self.config.window_seconds,
                **dict.fromkeys(_COUNTERS, 0)}

    async def initialize(self):
        if self.db is not None:
            await self.db.run(lambda conn: conn.execute('''
                CREATE TABLE IF NOT EXISTS usage_window (
                    id INTEGER PRIMARY KEY CHECK(id=1), window INTEGER NOT NULL,
                    window_seconds INTEGER NOT NULL,
                    ask_requests INTEGER NOT NULL, sandbox_requests INTEGER NOT NULL,
                    reported_calls INTEGER NOT NULL, missing_usage_calls INTEGER NOT NULL,
                    prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL
                )
            ''').close(), write=True)

    async def _change(self, change):
        fresh = self._empty()

        def apply(row):
            if (row is None or row['window_seconds'] != fresh['window_seconds']
                    or row['window'] < fresh['window']):
                row = dict(fresh)
            change(row)
            return row

        if self.db is None:
            async with self._lock:
                # A rejected reservation must not mutate the retained row.
                self._memory = apply(dict(self._memory) if self._memory is not None else None)
                return dict(self._memory)

        def transaction(conn):
            stored = conn.execute('SELECT * FROM usage_window WHERE id=1').fetchone()
            row = apply(dict(stored) if stored else None)
            columns = ('window', 'window_seconds', *_COUNTERS)
            conn.execute(
                "INSERT OR REPLACE INTO usage_window "
                "(id,window,window_seconds,ask_requests,sandbox_requests,reported_calls,"
                "missing_usage_calls,prompt_tokens,completion_tokens) "
                "VALUES (1,?,?,?,?,?,?,?,?)",
                tuple(row[key] for key in columns),
            )
            return row

        return await self.db.run(transaction, write=True)

    async def reserve(self, kind):
        if kind not in ('ask', 'sandbox'):
            raise ValueError('invalid usage kind')

        def change(row):
            total = row['ask_requests'] + row['sandbox_requests']
            if self.config.request_limit and total >= self.config.request_limit:
                raise QuotaExceeded()
            row[f'{kind}_requests'] += 1

        await self._change(change)

    async def record(self, response):
        """Count only provider-reported nonnegative integers; absent is not zero."""
        usage = getattr(response, 'usage', None)
        prompt = getattr(usage, 'prompt_tokens', None)
        completion = getattr(usage, 'completion_tokens', None)
        valid = all(type(value) is int and 0 <= value <= 1_000_000_000
                    for value in (prompt, completion))

        def change(row):
            if valid:
                row['reported_calls'] += 1
                row['prompt_tokens'] += prompt
                row['completion_tokens'] += completion
            else:
                row['missing_usage_calls'] += 1

        try:
            await self._change(change)
        except Exception:
            self.accounting_available = False
            log.error('Provider usage recording unavailable')

    async def snapshot(self):
        row = await self._change(lambda row: None)
        return {
            'window_start': row['window'] * row['window_seconds'],
            'window_seconds': row['window_seconds'],
            'request_limit': self.config.request_limit,
            'accounting_available': self.accounting_available,
            'storage_mode': 'sqlite' if self.db is not None else 'memory',
            **{key: row[key] for key in _COUNTERS},
        }


@contextmanager
def usage_scope(store):
    token = _active_usage.set(store)
    try:
        yield
    finally:
        _active_usage.reset(token)


async def record_completion(response=None):
    store = _active_usage.get()
    if store is not None:
        await store.record(response)
