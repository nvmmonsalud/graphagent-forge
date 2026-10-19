"""Explicit legacy Source migration CLI; dry-run unless --apply is supplied."""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from dotenv import load_dotenv

from src.graph.neo4j_client import Neo4jClient
from src.graph.source_migration import migrate_sources, rollback_sources


async def run(args):
    load_dotenv()
    client = Neo4jClient(os.environ["NEO4J_URI"], os.environ["NEO4J_USER"],
                         os.environ["NEO4J_PASSWORD"])
    await client.connect()
    try:
        # Deliberately do not call init_schema: inspection must not run unrelated
        # scalar-provenance or embedding migrations.
        if args.apply:
            async with client.driver.session() as session:
                await session.run("CREATE CONSTRAINT source_id IF NOT EXISTS "
                                  "FOR (s:Source) REQUIRE s.id IS UNIQUE")
                await session.run("CREATE CONSTRAINT graph_state_id IF NOT EXISTS "
                                  "FOR (s:GraphState) REQUIRE s.id IS UNIQUE")
        operation = rollback_sources if args.rollback else migrate_sources
        print(json.dumps(await operation(client, apply=args.apply), indent=2))
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Apply changes (backup DB first)")
    mode.add_argument("--dry-run", action="store_true", help="Inspect only; the default")
    parser.add_argument("--rollback", action="store_true",
                        help="Remove unchanged migrated metadata")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
