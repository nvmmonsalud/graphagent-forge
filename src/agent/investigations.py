"""Bounded saved research views; evidence metadata is retained, never excerpt text."""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field


class InvestigationConflict(ValueError):
    pass


class InvestigationLimit(ValueError):
    pass


class EvidenceNote(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    evidence_id: str = Field(pattern=r'^ev_[a-f0-9]{64}$')
    note: str = Field(max_length=2000)
    source_id: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=500)
    start: int = Field(ge=0, le=200000)
    end: int = Field(ge=0, le=200000)


class SavedView(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(min_length=1, max_length=200)
    query: str = Field(default='', max_length=4000)
    source_id: str | None = Field(default=None, min_length=1, max_length=500)
    selected_entity_ids: list[str] = Field(default_factory=list, max_length=5)
    annotations: list[EvidenceNote] = Field(default_factory=list, max_length=20)


def validated_view(payload):
    result = SavedView.model_validate(payload).model_dump()
    ids = result['selected_entity_ids']
    if any(not 1 <= len(value) <= 500 for value in ids) or len(set(ids)) != len(ids):
        raise ValueError('invalid selected entity IDs')
    notes = result['annotations']
    if len({note['evidence_id'] for note in notes}) != len(notes):
        raise ValueError('duplicate evidence notes')
    if any(note['end'] < note['start'] for note in notes):
        raise ValueError('invalid evidence offsets')
    return result


class InvestigationStore:
    def __init__(self, db=None):
        self.db = db
        self._memory = {}
        self._lock = asyncio.Lock()

    async def initialize(self):
        if self.db is not None:
            await self.db.run(lambda c: c.execute('''CREATE TABLE IF NOT EXISTS investigations (
                id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>=1),
                payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)'''),
                              write=True)

    @staticmethod
    def _public(row):
        if row is None:
            return None
        return {**json.loads(row['payload']), 'id': row['id'], 'revision': row['revision'],
                'created_at': row['created'], 'updated_at': row['updated']}

    async def list(self):
        if self.db is None:
            async with self._lock:
                return copy.deepcopy(sorted(self._memory.values(),
                                            key=lambda item: (-item['updated_at'], item['id'])))
        return await self.db.run(lambda c: [self._public(row) for row in c.execute(
            'SELECT * FROM investigations ORDER BY updated DESC,id LIMIT 50')])

    async def get(self, investigation_id):
        if self.db is None:
            async with self._lock:
                return copy.deepcopy(self._memory.get(investigation_id))
        return await self.db.run(lambda c: self._public(c.execute(
            'SELECT * FROM investigations WHERE id=?', (investigation_id,)).fetchone()))

    async def save(self, payload, investigation_id=None, revision=None):
        payload = validated_view(payload)
        create = investigation_id is None
        investigation_id = investigation_id or str(uuid.uuid4())
        now = time.time()
        if not create and (type(revision) is not int or revision < 1):
            raise InvestigationConflict('revision required')

        def updated(old, count):
            if create:
                if count >= 50:
                    raise InvestigationLimit('saved investigation limit reached')
                return {**payload, 'id': investigation_id, 'revision': 1,
                        'created_at': now, 'updated_at': now}
            if old is None:
                return None
            if old['revision'] != revision:
                raise InvestigationConflict('investigation changed; reload before saving')
            return {**payload, 'id': investigation_id, 'revision': revision + 1,
                    'created_at': old['created_at'], 'updated_at': now}

        if self.db is None:
            async with self._lock:
                result = updated(self._memory.get(investigation_id), len(self._memory))
                if result is not None:
                    self._memory[investigation_id] = result
                return copy.deepcopy(result)

        def save(connection):
            old = self._public(connection.execute(
                'SELECT * FROM investigations WHERE id=?', (investigation_id,)).fetchone())
            count = connection.execute('SELECT COUNT(*) FROM investigations').fetchone()[0]
            result = updated(old, count)
            if result is None:
                return None
            if create:
                connection.execute('INSERT INTO investigations VALUES (?,?,?,?,?)',
                                   (investigation_id, 1, json.dumps(payload), now, now))
            else:
                cursor = connection.execute('''UPDATE investigations
                    SET revision=?,payload=?,updated=? WHERE id=? AND revision=?''',
                    (revision + 1, json.dumps(payload), now, investigation_id, revision))
                if cursor.rowcount != 1:
                    raise InvestigationConflict('investigation changed; reload before saving')
            return result

        return await self.db.run(save, write=True)

    async def delete(self, investigation_id):
        if self.db is None:
            async with self._lock:
                return self._memory.pop(investigation_id, None) is not None
        return await self.db.run(lambda c: c.execute(
            'DELETE FROM investigations WHERE id=?', (investigation_id,)).rowcount > 0, write=True)
