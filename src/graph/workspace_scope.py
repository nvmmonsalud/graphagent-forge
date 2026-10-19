"""Namespace the application's owned, static Cypher; never accepts client Cypher.

The database login remains privileged. This wrapper is application isolation, not
an alternative to Neo4j database authorization or protection against an operator.
"""
from __future__ import annotations

import hashlib
import re

from neo4j import AsyncGraphDatabase

from src.graph.neo4j_client import Neo4jClient

LABELS = {'Entity', 'SemanticEntity', 'Source', 'EvidenceChunk', 'GraphState', 'IngestOperation'}
RELATIONSHIPS = {'RELATES_TO', 'HAS_CHUNK'}
SCHEMA_NAMES = {
    'source_id', 'evidence_id', 'ingest_operation_id', 'ingest_operation_created',
    'graph_state_id', 'evidence_source_id', 'entity_norm_label', 'entity_semantic_embedding',
}
ID_PATTERN = re.compile(r'[a-z0-9_-]{1,48}\Z')
_LITERAL = re.compile(r"'(?:\\.|''|[^'\\])*'|\"(?:\\.|\"\"|[^\"\\])*\"|`[^`]*`")


def namespace(workspace_id: str) -> str:
    if type(workspace_id) is not str or not ID_PATTERN.fullmatch(workspace_id):
        raise ValueError('Invalid workspace identifier')
    return 'W' + hashlib.sha256(workspace_id.encode()).hexdigest() + '_'


def scope_query(query: str, prefix: str) -> str:
    """Rewrite only internal queries, preserving parameters and string data.

    All relationship patterns, including anonymous and variable paths, are typed
    and their endpoints labeled. Variable paths additionally constrain *every*
    intermediate node, so even a database-created cross-partition link is hidden.
    """
    literals = []

    def mask(match):
        literals.append(match.group())
        return f'__LITERAL_{len(literals)-1}__'

    code = _LITERAL.sub(mask, query)
    # Anonymous degree/count patterns have the same isolation as normal edges.
    code = re.sub(r'\)\s*--\s*\(', ')-[:RELATES_TO]-(', code)
    # Only relationship brackets (not list expressions or parameter subscripts).
    def relation(match):
        body = match.group(1)
        if ':' not in body:
            body = re.sub(r'^(\w*)', r'\1:RELATES_TO', body, count=1)
        return '-[' + body + ']'
    code = re.sub(r'-\[([^\]]*)\]', relation, code)
    declared = dict(re.findall(r'\(\s*(\w+)\s*:\s*(\w+)', code))

    def node(match):
        body = match.group(1)
        if ':' in body:
            return match.group()
        variable = body.strip()
        label = declared.get(variable, 'Entity')
        if label not in LABELS:
            raise ValueError('Unsupported workspace node label')
        return '(' + body + ':' + label + ')'

    # Bound variables cannot repeat labels in MERGE/CREATE relationship clauses.
    # Their preceding MATCH is scoped; only read patterns need endpoint labels.
    pieces = re.split(r'(?=\b(?:MATCH|MERGE|CREATE|RETURN|WITH|SET|REMOVE|DELETE)\b)', code)
    for index, piece in enumerate(pieces):
        if re.match(r'(?:MERGE|CREATE)\b', piece):
            continue
        piece = re.sub(r'\((\s*\w*\s*)\)(?=\s*(?:<-|-\[))', node, piece)
        piece = re.sub(r'(?<=\])(-?>?)\((\s*\w*\s*)\)',
                       lambda m: m.group(1) + node(_NodeMatch(m.group(2))), piece)
        pieces[index] = piece
    code = ''.join(pieces)
    if re.search(r'\bMATCH\s+path\s*=', code, re.I):
        # Current application has two named paths; neither has a path WHERE.
        code, count = re.subn(
            r'(MATCH\s+path\s*=.*?)(\bRETURN\b)',
            r'\1 WHERE all(__workspace_node IN nodes(path) WHERE '
            r'__workspace_node:Entity) \2', code, flags=re.I | re.S,
        )
        if count != 1:
            raise ValueError('Unsupported workspace path query')
    for token in re.findall(r'[\[(]\s*\w*\s*:\s*([A-Za-z_]\w*)', code):
        if token not in LABELS | RELATIONSHIPS:
            raise ValueError('Unsupported workspace schema token')
    # Owned labels and relationship types use colon syntax, never parameters.
    def label(match):
        token = match.group(1)
        if token in LABELS | RELATIONSHIPS:
            return ':' + prefix + token
        return match.group()
    code = re.sub(r':\s*([A-Za-z_]\w*)', label, code)
    # Named schema objects must also be isolated; anonymous objects are scoped by label.
    def schema_name(match):
        if match.group(2) not in SCHEMA_NAMES:
            raise ValueError('Unsupported workspace schema name')
        return match.group(1) + ' ' + prefix + match.group(2)
    code = re.sub(r'\b(INDEX|CONSTRAINT)\s+(\w+)(?=\s+IF\b)', schema_name, code)
    for index, literal in enumerate(literals):
        if literal in ("'entity_semantic_embedding'", '"entity_semantic_embedding"'):
            literal = literal[0] + prefix + 'entity_semantic_embedding' + literal[-1]
        code = code.replace(f'__LITERAL_{index}__', literal)
    return code


class _NodeMatch:
    def __init__(self, body):
        self.body = body

    def group(self, index=0):
        return self.body if index == 1 else '(' + self.body + ')'


class _Runner:
    def __init__(self, runner, prefix):
        self._runner, self._prefix = runner, prefix

    async def run(self, query, *args, **kwargs):
        if not isinstance(query, str):
            raise TypeError('Workspace queries must be internal static strings')
        return await self._runner.run(scope_query(query, self._prefix), *args, **kwargs)

    def __getattr__(self, name):
        # Callback transaction helpers would receive the raw driver transaction
        # and bypass run(). Fail closed rather than inherit future query APIs.
        if name not in {"commit", "rollback", "close"}:
            raise AttributeError(name)
        return getattr(self._runner, name)


class _Session(_Runner):
    async def __aenter__(self):
        await self._runner.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self._runner.__aexit__(*args)

    async def begin_transaction(self, *args, **kwargs):
        return _Runner(await self._runner.begin_transaction(*args, **kwargs), self._prefix)


class ScopedDriver:
    def __init__(self, driver, workspace_id):
        self._driver, self.prefix = driver, namespace(workspace_id)

    def session(self, *args, **kwargs):
        return _Session(self._driver.session(*args, **kwargs), self.prefix)

    async def close(self):
        await self._driver.close()


class WorkspaceNeo4jClient(Neo4jClient):
    def __init__(self, uri, user, password, workspace_id):
        super().__init__(uri, user, password)
        self.workspace_id = workspace_id
        self.namespace = namespace(workspace_id)

    async def connect(self):
        raw = AsyncGraphDatabase.driver(self.uri, auth=(self.user, self.password))
        # Install scope before any query or schema migration can execute.
        self.driver = ScopedDriver(raw, self.workspace_id)
        await raw.verify_connectivity()
