"""Bounded structural validation of citations, not verification of claim entailment."""
from __future__ import annotations

MAX_ANSWER_CHUNKS = 3
MAX_EVIDENCE_CHARS = 24_000
MAX_ANSWER_CHARS = 16_000
MAX_CONFLICTS = 6


def bounded_evidence(chunks: list[dict]) -> list[dict]:
    """Select whole original excerpts; never make up offsets for truncated text."""
    selected: list[dict] = []
    seen: set[str] = set()
    size = 0
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        if not all(isinstance(chunk.get(k), str) and chunk[k] for k in (
            "id", "source_id", "version", "text",
        )):
            continue
        if any(len(chunk[k]) > 512 for k in ("id", "source_id", "version")):
            continue
        start, end = chunk.get("start"), chunk.get("end")
        if type(start) is not int or type(end) is not int or start < 0:
            continue
        if end - start != len(chunk["text"]) or chunk["id"] in seen:
            continue
        if size + len(chunk["text"]) > MAX_EVIDENCE_CHARS:
            continue
        selected.append(dict(chunk))
        seen.add(chunk["id"])
        size += len(chunk["text"])
        if len(selected) == MAX_ANSWER_CHUNKS:
            break
    return selected


def validate_answer(value: object, evidence: list[dict]) -> bool:
    """Reject a malformed result in its entirety, including malformed conflicts."""
    if not isinstance(value, dict) or type(value.get("insufficient_evidence")) is not bool:
        return False
    answer = value.get("answer")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_ANSWER_CHARS:
        return False
    allowed = {chunk["id"] for chunk in evidence}

    def valid_ids(ids: object, minimum: int) -> bool:
        return (
            isinstance(ids, list) and minimum <= len(ids) <= MAX_ANSWER_CHUNKS
            and all(isinstance(item, str) and item in allowed for item in ids)
            and len(set(ids)) == len(ids)
        )

    if not valid_ids(value.get("citations"), 0 if value["insufficient_evidence"] else 1):
        return False
    conflicts = value.get("conflicts")
    if not isinstance(conflicts, list) or len(conflicts) > MAX_CONFLICTS:
        return False
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            return False
        description = conflict.get("description")
        if not isinstance(description, str) or not description.strip() or len(description) > 2000:
            return False
        if not valid_ids(conflict.get("evidence_ids"), 2):
            return False
    return True


def evidence_unchanged(sent: list[dict], current: list[dict]) -> bool:
    """Recheck every supplied excerpt after generation, including uncited chunks."""
    if not isinstance(current, list) or not all(isinstance(c, dict) for c in current):
        return False
    by_id = {c.get("id"): c for c in current if isinstance(c.get("id"), str)}
    fields = ("id", "source_id", "version", "text", "start", "end")
    return all(
        chunk["id"] in by_id
        and all(by_id[chunk["id"]].get(k) == chunk.get(k) for k in fields)
        for chunk in sent
    )


def citation_metadata(chunk: dict) -> dict:
    """No excerpts or model-generated URLs in the citation response."""
    result = {k: chunk[k] for k in ("id", "source_id", "version", "start", "end")}
    for key, limit in (("title", 1000), ("uri", 2048)):
        value = chunk.get(key)
        result[key] = value[:limit] if isinstance(value, str) else ""
    return result
