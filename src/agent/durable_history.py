"""SQLite-backed history with the same bounded public records as QueryHistory."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from src.agent.history import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_ANSWER_CHARS,
    DEFAULT_MAX_LIST_ITEMS,
    DEFAULT_SAVED_LIMIT,
    UNKNOWN_STATUS,
    _clip_list,
    _clip_text,
    _utcnow,
)
from src.agent.state import StateDatabase


class SQLiteQueryHistory:
    """Async history adapter. SQLite enforces bounds across all local managers."""

    def __init__(
        self,
        db: StateDatabase,
        *,
        limit: int = DEFAULT_LIMIT,
        saved_limit: int = DEFAULT_SAVED_LIMIT,
        max_answer_chars: int = DEFAULT_MAX_ANSWER_CHARS,
        max_list_items: int = DEFAULT_MAX_LIST_ITEMS,
    ) -> None:
        self.db = db
        self._limit = max(1, int(limit))
        self._saved_limit = max(1, int(saved_limit))
        self._max_answer_chars = max(1, int(max_answer_chars))
        self._max_list_items = max(1, int(max_list_items))

    async def initialize(self) -> None:
        def create(connection: sqlite3.Connection) -> None:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS query_history (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    status TEXT NOT NULL,
                    context_nodes TEXT NOT NULL,
                    source_docs TEXT NOT NULL,
                    saved INTEGER NOT NULL DEFAULT 0 CHECK(saved IN (0, 1)),
                    created_at TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0)
                )
            """)
            # Validate existing table definitions before accepting durable
            # history; CREATE IF NOT EXISTS alone hides incompatible schemas.
            expected = {
                "seq": "INTEGER",
                "id": "TEXT",
                "question": "TEXT",
                "answer": "TEXT",
                "status": "TEXT",
                "context_nodes": "TEXT",
                "source_docs": "TEXT",
                "saved": "INTEGER",
                "created_at": "TEXT",
                "duration_ms": "INTEGER",
            }
            columns = connection.execute("PRAGMA table_info(query_history)").fetchall()
            if {row["name"]: row["type"].upper() for row in columns} != expected:
                raise ValueError("unsupported query history schema")
            # A missing uniqueness constraint is unsafe even with matching
            # columns: duplicated public IDs would break updates/deletion.
            unique_id = False
            for index in connection.execute("PRAGMA index_list(query_history)").fetchall():
                if index["unique"] and not index["partial"]:
                    indexed = connection.execute(
                        "SELECT name FROM pragma_index_info(?)", (index["name"],)
                    ).fetchall()
                    unique_id |= [item["name"] for item in indexed] == ["id"]
            constraints = all(
                row["pk"] == int(row["name"] == "seq")
                and row["notnull"] == int(row["name"] != "seq")
                for row in columns
            )
            definition = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'query_history'"
            ).fetchone()[0]
            compact = "".join(definition.upper().split())
            required_constraints = (
                "AUTOINCREMENT",
                "CHECK(SAVEDIN(0,1))",
                "CHECK(DURATION_MS>=0)",
            )
            if (
                not unique_id
                or not constraints
                or not all(clause in compact for clause in required_constraints)
            ):
                raise ValueError("unsupported query history constraints")

        await self.db.run(create, write=True)

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "question": row["question"],
            "answer": row["answer"],
            "status": row["status"],
            "context_nodes": json.loads(row["context_nodes"]),
            "source_docs": json.loads(row["source_docs"]),
            "saved": bool(row["saved"]),
            "created_at": row["created_at"],
            "duration_ms": row["duration_ms"],
        }

    async def record(self, *, question: str, response: dict, duration_ms: int) -> dict[str, Any]:
        payload = response if isinstance(response, dict) else {}
        try:
            duration = max(0, int(duration_ms))
        except (TypeError, ValueError, OverflowError):
            duration = 0
        duration = min(duration, 2**63 - 1)
        entry_id = uuid.uuid4().hex
        status = payload.get("status")
        # Bound status too: unlike memory storage this string is on disk.
        status = (
            status[: self._max_answer_chars]
            if isinstance(status, str) and status
            else UNKNOWN_STATUS
        )
        values = (
            entry_id,
            _clip_text(question, self._max_answer_chars),
            _clip_text(payload.get("answer"), self._max_answer_chars),
            status,
            json.dumps(
                _clip_list(
                    payload.get("context_nodes"), self._max_list_items, self._max_answer_chars
                )
            ),
            json.dumps(
                _clip_list(payload.get("source_docs"), self._max_list_items, self._max_answer_chars)
            ),
            _utcnow(),
            duration,
        )

        def insert(connection: sqlite3.Connection) -> dict[str, Any]:
            connection.execute(
                """
                INSERT INTO query_history
                    (id, question, answer, status, context_nodes, source_docs,
                     created_at, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                values,
            )
            overflow = connection.execute("SELECT COUNT(*) FROM query_history").fetchone()[0]
            overflow -= self._limit
            if overflow > 0:
                connection.execute(
                    """
                    DELETE FROM query_history WHERE seq IN (
                        SELECT seq FROM query_history WHERE saved = 0 AND id != ?
                        ORDER BY seq LIMIT ?
                    )
                """,
                    (entry_id, overflow),
                )
            return self._public(
                connection.execute(
                    "SELECT * FROM query_history WHERE id = ?", (entry_id,)
                ).fetchone()
            )

        return await self.db.run(insert, write=True)

    async def list(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            limit = 20
        if limit <= 0:
            return []
        limit = min(limit, 2**63 - 1)
        return await self.db.run(
            lambda connection: [
                self._public(row)
                for row in connection.execute(
                    "SELECT * FROM query_history ORDER BY created_at DESC, seq DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
        )

    async def get(self, entry_id: str) -> dict[str, Any] | None:
        def get(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT * FROM query_history WHERE id = ?", (entry_id,)
            ).fetchone()
            return self._public(row) if row else None

        return await self.db.run(get)

    async def set_saved(self, entry_id: str, saved: bool) -> dict[str, Any] | None:
        def update(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT * FROM query_history WHERE id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                return None
            want = bool(saved)
            saved_count = connection.execute(
                "SELECT COUNT(*) FROM query_history WHERE saved = 1"
            ).fetchone()[0]
            if want and not row["saved"] and saved_count >= self._saved_limit:
                return self._public(row)
            connection.execute("UPDATE query_history SET saved = ? WHERE id = ?", (want, entry_id))
            return self._public(
                connection.execute(
                    "SELECT * FROM query_history WHERE id = ?", (entry_id,)
                ).fetchone()
            )

        return await self.db.run(update, write=True)

    async def delete(self, entry_id: str) -> bool:
        return await self.db.run(
            lambda connection: (
                connection.execute("DELETE FROM query_history WHERE id = ?", (entry_id,)).rowcount
                > 0
            ),
            write=True,
        )

    async def saved_count(self) -> int:
        return (await self.counts())["saved_count"]

    async def counts(self) -> dict[str, int]:
        def counts(connection: sqlite3.Connection) -> dict[str, int]:
            row = connection.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(saved), 0) AS saved_count "
                "FROM query_history"
            ).fetchone()
            return dict(row)

        return await self.db.run(counts)
