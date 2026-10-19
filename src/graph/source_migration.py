"""Explicit, idempotent migration of legacy provenance to Source metadata.

No membership, entity ID or assertion is changed. Old title collisions are
irrecoverable: the report deliberately records that ambiguity rather than
inventing identities. Keep a database backup before applying any migration.
"""
from __future__ import annotations

MIGRATION = "legacy-sources-v1"


async def migrate_sources(client, *, apply: bool = False) -> dict:
    """Inspect legacy provenance, optionally create metadata under the graph lock."""
    async with client.driver.session() as session:
        tx = await session.begin_transaction()
        try:
            await client._lock_graph(tx)
            result = await tx.run("""
                CALL () {
                  MATCH (n:Entity) UNWIND coalesce(n.source_docs, []) AS id RETURN id
                  UNION MATCH (n:Entity) RETURN n.source_doc AS id
                  UNION MATCH ()-[r:RELATES_TO]->() RETURN r.source_doc AS id
                }
                WITH DISTINCT id WHERE id IS NOT NULL AND id <> ''
                OPTIONAL MATCH (s:Source {id: id})
                RETURN id, s IS NULL AS missing ORDER BY id
                """)
            rows = [dict(r) async for r in result]
            missing = [r["id"] for r in rows if r["missing"]]
            if apply:
                await tx.run("""
                    UNWIND $ids AS id
                    MERGE (s:Source {id: id})
                    ON CREATE SET s.title = id, s.kind = 'legacy', s._migration = $migration
                    """, ids=missing, migration=MIGRATION)
                await tx.commit()
            else:
                await tx.rollback()
        except BaseException:
            await tx.rollback()
            raise
    return {"applied": apply, "legacy_sources": len(rows), "created": len(missing) if apply else 0,
            "would_create": missing,
            "warning": ("Legacy title collisions cannot be reconstructed; "
                        "identities preserved verbatim.")}


async def rollback_sources(client, *, apply: bool = False) -> dict:
    """Remove only unchanged metadata created by this migration, never graph data.

    Metadata edited/published after migration is preserved. Existing Source
    records never receive a migration marker and cannot be selected here.
    """
    async with client.driver.session() as session:
        tx = await session.begin_transaction()
        try:
            await client._lock_graph(tx)
            result = await tx.run("""
                MATCH (s:Source {_migration: $migration})
                WHERE s.kind = 'legacy' AND s.title = s.id
                  AND all(k IN keys(s) WHERE k IN ['id', 'title', 'kind', '_migration'])
                RETURN s.id AS id ORDER BY id
                """, migration=MIGRATION)
            ids = [r["id"] async for r in result]
            if apply:
                await tx.run("MATCH (s:Source) WHERE s.id IN $ids DELETE s", ids=ids)
                await tx.commit()
            else:
                await tx.rollback()
        except BaseException:
            await tx.rollback()
            raise
    return {"applied": apply, "would_remove": ids, "removed": len(ids) if apply else 0}
