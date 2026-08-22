"""In-memory query/answer history.

Generic on purpose: this module knows nothing about GraphAgent, GraphRAG or
FastAPI. Callers hand :meth:`QueryHistory.record` the question, the opaque
answer payload the query pipeline produced and how long it took; the store
owns id assignment, bounding, eviction and the saved/unsaved flag.

Deliberately imports neither ``src.main`` nor ``src.agent.core`` — recording
happens in the route (the same injected-sink discipline ``src/agent/jobs.py``
follows), which keeps this importable from anywhere without a circular import.

State is per-process and in memory: everything is lost on restart.

**Synchronous by design.** Unlike :class:`src.agent.jobs.JobManager` there is
no ``start()``/``shutdown()`` and no event loop involved — the store performs
no I/O and owns no background tasks, so there is nothing to spawn and nothing
to drain. That is an intentional absence, not an omission: the lifespan hook
just assigns an instance and forgets about it.
"""
from __future__ import annotations

import uuid
from collections import deque
from datetime import UTC, datetime
from itertools import islice
from typing import Any

#: Default ring size. Older *unsaved* entries fall off past this.
DEFAULT_LIMIT = 50
#: How many entries may be pinned as `saved` at once.
DEFAULT_SAVED_LIMIT = 20
#: Per-string cap for `question` / `answer`.
DEFAULT_MAX_ANSWER_CHARS = 4000
#: Per-list cap for `context_nodes` / `source_docs`.
DEFAULT_MAX_LIST_ITEMS = 20

#: Placeholder for a `status` that wasn't a plain string.
UNKNOWN_STATUS = "unknown"


def _utcnow() -> str:
    # `UTC` is the 3.11+ alias of `timezone.utc` — same value, ruff-clean (UP017).
    return datetime.now(UTC).isoformat()


def _clip_text(value: Any, limit: int) -> str:
    """Coerce to a bounded string, dropping anything that isn't already one.

    Non-string values are discarded rather than `str()`-ed: the payload may
    carry exception objects or driver internals whose repr leaks bolt URIs and
    credentials, and this record is served verbatim by `GET /api/history`.
    """
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _clip_list(value: Any, limit: int, char_limit: int) -> list[str]:
    """Coerce to a bounded list of bounded strings; non-strings are dropped."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        out.append(item[:char_limit])
        if len(out) >= limit:
            break
    return out


class _Entry:
    """Internal record. Private fields are excluded by :meth:`to_public`."""

    __slots__ = (
        "id",
        "question",
        "answer",
        "status",
        "context_nodes",
        "source_docs",
        "saved",
        "created_at",
        "duration_ms",
        "_seq",
    )

    def __init__(
        self,
        *,
        entry_id: str,
        question: str,
        answer: str,
        status: str,
        context_nodes: list[str],
        source_docs: list[str],
        duration_ms: int,
        seq: int,
    ):
        self.id = entry_id
        self.question = question
        self.answer = answer
        self.status = status
        self.context_nodes = context_nodes
        self.source_docs = source_docs
        self.saved = False
        self.created_at: str = _utcnow()
        self.duration_ms = duration_ms
        self._seq = seq

    def to_public(self) -> dict[str, Any]:
        """JSON-serializable snapshot. The 9 keys here are the frozen public
        schema — the frontend and tests both pin it, so adding or renaming one
        is a breaking change.

        Lists are copied on the way out: a caller mutating the returned
        `context_nodes` must not reach back into the store's own record.
        """
        return {
            "id": self.id,
            "question": self.question,
            "answer": self.answer,
            "status": self.status,
            "context_nodes": list(self.context_nodes),
            "source_docs": list(self.source_docs),
            "saved": self.saved,
            "created_at": self.created_at,
            "duration_ms": self.duration_ms,
        }


class QueryHistory:
    """Bounded, in-memory ring of answered questions, newest-first on read."""

    def __init__(
        self,
        *,
        limit: int = DEFAULT_LIMIT,
        saved_limit: int = DEFAULT_SAVED_LIMIT,
        max_answer_chars: int = DEFAULT_MAX_ANSWER_CHARS,
        max_list_items: int = DEFAULT_MAX_LIST_ITEMS,
    ) -> None:
        self._limit = max(1, int(limit))
        self._saved_limit = max(1, int(saved_limit))
        self._max_answer_chars = max(1, int(max_answer_chars))
        self._max_list_items = max(1, int(max_list_items))

        # Oldest at the left, newest at the right — eviction pops from the left.
        self._entries: deque[_Entry] = deque()
        self._by_id: dict[str, _Entry] = {}
        self._seq = 0

    # ----------------------------------------------------------- public API
    def record(self, *, question: str, response: dict, duration_ms: int) -> dict[str, Any]:
        """Store one answered question and return its public record.

        `response` is treated as opaque and possibly malformed: every field is
        read with `.get` and coerced, so a partial or junk payload produces a
        degraded entry instead of raising. Recording must never be able to fail
        the answer it is recording.

        `sources` from the GraphRAG payload is deliberately NOT stored. It is a
        heterogeneous list of full node dicts (some carry a vector `score`,
        some don't), unbounded in size and never rendered by the frontend —
        keeping it would push megabytes of node bodies into a ring buffer for
        zero display value. `context_nodes` (labels) and `source_docs`
        (provenance) are the parts a history panel actually shows.
        """
        payload: dict = response if isinstance(response, dict) else {}
        try:
            duration = max(0, int(duration_ms))
        except (TypeError, ValueError):
            duration = 0

        status = payload.get("status")
        self._seq += 1
        entry = _Entry(
            entry_id=uuid.uuid4().hex,
            question=_clip_text(question, self._max_answer_chars),
            answer=_clip_text(payload.get("answer"), self._max_answer_chars),
            status=status if isinstance(status, str) and status else UNKNOWN_STATUS,
            context_nodes=_clip_list(
                payload.get("context_nodes"), self._max_list_items, self._max_answer_chars
            ),
            source_docs=_clip_list(
                payload.get("source_docs"), self._max_list_items, self._max_answer_chars
            ),
            duration_ms=duration,
            seq=self._seq,
        )
        self._entries.append(entry)
        self._by_id[entry.id] = entry
        self._evict()
        return entry.to_public()

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        """Newest-first snapshot of the ring."""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 20
        if limit <= 0:
            return []
        records = sorted(
            self._entries, key=lambda e: (e.created_at, e._seq), reverse=True
        )
        return [r.to_public() for r in records[:limit]]

    def get(self, entry_id: str) -> dict[str, Any] | None:
        entry = self._by_id.get(entry_id)
        return entry.to_public() if entry is not None else None

    def set_saved(self, entry_id: str, saved: bool) -> dict[str, Any] | None:
        """Pin/unpin an entry. Returns the (possibly unchanged) public record,
        or None when the id is unknown.

        Hitting `saved_limit` is a silent no-op *here* — the entry comes back
        with `saved` still false, which is exactly what the route turns into a
        409, so a caller can always tell a refusal from a success by reading
        the returned `saved` flag.
        """
        entry = self._by_id.get(entry_id)
        if entry is None:
            return None
        want = bool(saved)
        if want and not entry.saved and self.saved_count() >= self._saved_limit:
            return entry.to_public()
        entry.saved = want
        return entry.to_public()

    def delete(self, entry_id: str) -> bool:
        entry = self._by_id.pop(entry_id, None)
        if entry is None:
            return False
        try:
            self._entries.remove(entry)
        except ValueError:  # pragma: no cover — index and ring stay in step
            pass
        return True

    def saved_count(self) -> int:
        return sum(1 for e in self._entries if e.saved)

    def counts(self) -> dict[str, int]:
        return {"total": len(self._entries), "saved_count": self.saved_count()}

    # ------------------------------------------------------------ internals
    def _evict(self) -> None:
        """Drop the oldest *unsaved* entry until the ring fits `limit`.

        Saved entries are exempt — that exemption is the whole point of the
        flag, and it is what lets one store back both the rolling history and
        the pinned list without a second registry.

        The entry just appended is never its own victim: when every older
        entry is pinned, the ring overshoots `limit` instead of swallowing the
        answer that arrived a microsecond ago, which is the one outcome a user
        would read as "history is broken". Reachable only when `saved_limit`
        is configured >= `limit`; the ring stays bounded either way, at worst
        `limit + saved_limit`.
        """
        while len(self._entries) > self._limit:
            # `islice(..., len - 1)` = every entry except the newest.
            victim = next(
                (e for e in islice(self._entries, len(self._entries) - 1) if not e.saved),
                None,
            )
            if victim is None:  # everything older is pinned — nothing may go
                return
            self._entries.remove(victim)
            self._by_id.pop(victim.id, None)
