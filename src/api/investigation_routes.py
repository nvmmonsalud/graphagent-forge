"""Saved investigations, exact entity comparison and bounded graph browsing."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.agent.investigations import InvestigationConflict, InvestigationLimit
from src.api.routes import _resources, _store_call, require_api_key, require_graph

router = APIRouter(prefix='/api/research', tags=['research'])
AUTH = [Depends(require_api_key)]
GRAPH = [Depends(require_api_key), Depends(require_graph)]


class AnnotationInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    evidence_id: str = Field(pattern=r'^ev_[a-f0-9]{64}$')
    note: str = Field(max_length=2000)


class ViewInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(min_length=1, max_length=200)
    query: str = Field(default='', max_length=4000)
    source_id: str | None = Field(default=None, min_length=1, max_length=500)
    selected_entity_ids: list[str] = Field(default_factory=list, max_length=5)
    annotations: list[AnnotationInput] = Field(default_factory=list, max_length=20)

    @field_validator('selected_entity_ids')
    @classmethod
    def bounded_ids(cls, value):
        if len(set(value)) != len(value) or any(not 1 <= len(item) <= 500 for item in value):
            raise ValueError('invalid selected entity IDs')
        return value

    @field_validator('annotations')
    @classmethod
    def unique_notes(cls, value):
        if len({item.evidence_id for item in value}) != len(value):
            raise ValueError('duplicate evidence notes')
        return value


class ViewUpdate(ViewInput):
    revision: int = Field(ge=1, le=2**63 - 2)


class CompareInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    entity_ids: list[str] = Field(min_length=1, max_length=5)
    _bounded = field_validator('entity_ids')(ViewInput.bounded_ids.__func__)


async def _annotated(view, neo4j):
    if view is None:
        raise HTTPException(404, 'investigation not found')
    result = {**view, 'annotations': []}
    for annotation in view['annotations']:
        current = await _store_call(neo4j.get_evidence, annotation['evidence_id'])
        stale = current is None or any(
            current.get(key) != annotation[key] for key in ('source_id', 'version', 'start', 'end'))
        result['annotations'].append({**annotation, 'stale': stale})
    return result


async def _validated_notes(body, resource, previous=None):
    previous_notes = {item['evidence_id']: item for item in (previous or {}).get('annotations', [])}
    notes = []
    for item in body.annotations:
        current = await _store_call(resource.neo4j.get_evidence, item.evidence_id)
        prior = previous_notes.get(item.evidence_id)
        if prior is not None:
            # Preserve the originally observed version even when a stale note is edited.
            notes.append({**prior, 'note': item.note})
            continue
        if current is None:
            raise HTTPException(409, 'evidence is no longer current')
        notes.append({'evidence_id': item.evidence_id, 'note': item.note,
                      **{key: current[key] for key in ('source_id', 'version', 'start', 'end')}})
    return notes


@router.get('/investigations', dependencies=AUTH)
async def list_investigations(request: Request):
    views = await _store_call(_resources(request).investigations.list)
    # Listing metadata avoids up to 1000 graph lookups; opening a view checks every note.
    return {'investigations': [{key: value for key, value in view.items() if key != 'annotations'}
                              | {'annotation_count': len(view['annotations'])} for view in views]}


@router.get('/investigations/{investigation_id}', dependencies=AUTH)
async def get_investigation(investigation_id: str, request: Request):
    resource = _resources(request)
    view = await _store_call(resource.investigations.get, investigation_id)
    return await _annotated(view, resource.neo4j)


async def _save(body, request, investigation_id=None):
    resource = _resources(request)
    old = None
    if investigation_id:
        old = await _store_call(resource.investigations.get, investigation_id)
        if old is None:
            raise HTTPException(404, 'investigation not found')
    payload = body.model_dump(exclude={'revision'})
    payload['annotations'] = await _validated_notes(body, resource, old)
    try:
        result = await resource.investigations.save(
            payload, investigation_id=investigation_id, revision=getattr(body, 'revision', None))
    except (InvestigationConflict, InvestigationLimit) as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        raise HTTPException(500, 'internal error') from None
    return await _annotated(result, resource.neo4j)


@router.post('/investigations', status_code=201, dependencies=AUTH)
async def create_investigation(body: ViewInput, request: Request):
    return await _save(body, request)


@router.put('/investigations/{investigation_id}', dependencies=AUTH)
async def update_investigation(investigation_id: str, body: ViewUpdate, request: Request):
    return await _save(body, request, investigation_id)


@router.delete('/investigations/{investigation_id}', dependencies=AUTH)
async def delete_investigation(investigation_id: str, request: Request):
    if not await _store_call(_resources(request).investigations.delete, investigation_id):
        raise HTTPException(404, 'investigation not found')
    return {'deleted': investigation_id}


@router.post('/compare', dependencies=GRAPH)
async def compare_entities(body: CompareInput, request: Request):
    neo4j = _resources(request).neo4j
    contexts, missing = [], []
    for entity_id in body.entity_ids:
        context = await _store_call(neo4j.get_entity_context, entity_id, depth=1)
        if not context or not any(node.get('id') == entity_id for node in context.get('nodes', [])):
            missing.append(entity_id)
        else:
            contexts.append({'id': entity_id, **context})
    return {'contexts': contexts, 'missing_ids': missing}


def _source_hash(source_id):
    return hashlib.sha256(json.dumps(source_id).encode()).hexdigest()


def _cursor_decode(cursor, source_id):
    try:
        raw = base64.b64decode(cursor, altchars=b'-_', validate=True)
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {'v', 'after', 'source_hash'}
                or type(value['v']) is not int or value['v'] != 1
                or not isinstance(value['after'], str) or not 1 <= len(value['after']) <= 500
                or value['source_hash'] != _source_hash(source_id)):
            raise ValueError('invalid cursor')
        return value['after']
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise HTTPException(422, 'invalid browse cursor') from None


@router.get('/browse', dependencies=GRAPH)
async def browse_graph(request: Request, cursor: str | None = Query(default=None, max_length=1024),
                       limit: int = Query(default=100, ge=1, le=100),
                       source_id: str | None = Query(default=None, min_length=1, max_length=500)):
    after_id = _cursor_decode(cursor, source_id) if cursor else None
    result = await _store_call(_resources(request).neo4j.browse_graph,
                               after_id=after_id, limit=limit, source_id=source_id)
    result = dict(result)
    next_id = result.pop('next_id', None)
    result['next_cursor'] = None
    if next_id is not None:
        payload = {'v': 1, 'after': next_id, 'source_hash': _source_hash(source_id)}
        result['next_cursor'] = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(',', ':')).encode()).decode()
    return result
