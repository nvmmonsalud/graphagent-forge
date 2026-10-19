"""Versioned, inert ingestion commands for durable job replay."""
from __future__ import annotations

import base64
import binascii
from typing import Any

from src.ingestion.chunks import MAX_DOCUMENT_CHARS
from src.ingestion.extractor import check_url_shape
from src.ingestion.file_extractor import MAX_UPLOAD_BYTES, check_upload_shape


def _string(value: Any, limit: int, *, nullable: bool = False) -> bool:
    return (nullable and value is None) or (type(value) is str and len(value) <= limit)


def validate_command(command: Any) -> dict:
    """Return a validated plain command; malformed commands never invoke the agent.

    Errors deliberately omit supplied values, which can contain private documents.
    Full URL DNS/redirect checks and file content checks still run in the extractors.
    """
    if type(command) is not dict or type(command.get("version")) is not int:
        raise ValueError("Invalid ingestion command")
    kind = command.get("kind")
    fields = {
        "url": {"version", "kind", "url"},
        "text": {"version", "kind", "text", "source"},
        "file": {"version", "kind", "data_b64", "filename", "content_type", "source"},
    }
    if (command["version"] != 1 or type(kind) is not str or kind not in fields
            or set(command) != fields[kind]):
        raise ValueError("Invalid ingestion command")
    if kind == "url":
        url = command["url"]
        if not _string(url, 8192) or not url or check_url_shape(url):
            raise ValueError("Invalid ingestion URL")
    elif kind == "text":
        if (not _string(command["text"], MAX_DOCUMENT_CHARS) or not command["text"]
                or not _string(command["source"], 300)):
            raise ValueError("Invalid ingestion text command")
    else:
        if (not _string(command["filename"], 300)
                or not _string(command["content_type"], 300, nullable=True)
                or not _string(command["source"], 300, nullable=True)
                or check_upload_shape(command["filename"], command["content_type"])):
            raise ValueError("Invalid ingestion file metadata")
        encoded = command["data_b64"]
        if not _string(encoded, 4 * ((MAX_UPLOAD_BYTES + 2) // 3)):
            raise ValueError("Invalid ingestion file data")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Invalid ingestion file data") from None
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("Invalid ingestion file size")
        # One canonical encoding prevents alternate representations of one payload.
        if base64.b64encode(data).decode("ascii") != encoded:
            raise ValueError("Invalid ingestion file encoding")
    return dict(command)


def command_handler(agent):
    """Build a dispatcher over the three explicit ingestion methods only."""
    async def handle(command, progress, operation_id):
        command = validate_command(command)
        if type(operation_id) is not str or not operation_id or len(operation_id) > 128:
            raise ValueError("Invalid ingestion operation ID")
        receipt = await agent.neo4j.get_ingest_operation(operation_id)
        if receipt is not None:
            return receipt
        kind = command["kind"]
        kwargs = {"progress": progress, "operation_id": operation_id}
        if kind == "url":
            return await agent.ingest_url(command["url"], **kwargs)
        if kind == "text":
            return await agent.ingest_text(command["text"], source=command["source"], **kwargs)
        return await agent.ingest_file(
            base64.b64decode(command["data_b64"], validate=True),
            command["filename"], content_type=command["content_type"],
            source=command["source"], **kwargs,
        )

    return handle
