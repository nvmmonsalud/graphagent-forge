"""Uploaded-file content extraction (PDF / TXT / MD).

Sibling of :mod:`src.ingestion.extractor`, which handles URLs. This module owns
the *upload* path: bytes that arrived from a browser or Streamlit form rather
than from an HTTP fetch. It deliberately reuses ``MAX_CHARS``,
``TRUNCATION_SUFFIX`` and ``_envelope`` from that module so both ingestion paths
produce byte-identical truncation and the same base envelope keys.

Import direction is one-way: ``extractor`` must never import this module.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.ingestion.extractor import MAX_CHARS, TRUNCATION_SUFFIX, _envelope

log = logging.getLogger(__name__)

# pypdf is optional — mirrors daytona_exec.py's HAS_DAYTONA graceful degradation.
# Module import must NEVER fail because of it; txt/md keep working without it.
try:
    from pypdf import PdfReader

    HAS_PYPDF = True
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    HAS_PYPDF = False
    log.warning("pypdf not installed — PDF upload disabled (txt/md still work)")

MAX_UPLOAD_BYTES = 3 * 1024 * 1024  # mirrors extractor.MAX_BODY_BYTES
MAX_FILENAME_LEN = 300
SUPPORTED_EXTENSIONS: tuple[str, ...] = (".pdf", ".txt", ".md")

# How many leading bytes the magic-byte / binary sniff inspects.
SNIFF_BYTES = 8192

# Content types that carry no information: browsers and Streamlit send these for
# .md (and often .txt) constantly. Treating them as a mismatch would 415 nearly
# every real upload, so they — and None — never count as a declared-type conflict.
IGNORED_CONTENT_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

# Signatures that prove a "text" file is really a binary container.
_BINARY_MAGIC = (b"%PDF-", b"PK\x03\x04", b"MZ", b"\x7fELF")

# --- Error strings (asserted on by tests; keep the wording stable) -----------
ERR_NO_FILENAME = "Blocked: no filename provided"
ERR_FILENAME_TOO_LONG = f"Blocked: filename too long (max {MAX_FILENAME_LEN} characters)"
ERR_FILENAME_UNSAFE = "Blocked: filename contains a path separator or control character"
ERR_EMPTY_FILE = "Blocked: empty file"
ERR_TOO_LARGE = "Blocked: file exceeds the 3 MB limit"
ERR_NO_PYPDF = "PDF support unavailable — pypdf is not installed"
ERR_ENCRYPTED = "Encrypted or password-protected PDF — cannot extract text"
ERR_PDF_PARSE = "Could not parse PDF (corrupt or unsupported file)"
ERR_PDF_NO_TEXT = "No extractable text — scanned/image-only PDF? (OCR is not supported)"
ERR_NO_TEXT = "No extractable text in file"


def _err_unsupported_ext(ext: str) -> str:
    return (
        f"Blocked: unsupported file type '{ext}' "
        f"(supported: {', '.join(SUPPORTED_EXTENSIONS)})"
    )


def _err_type_mismatch(media_type: str, ext: str) -> str:
    return f"Blocked: declared content type '{media_type}' does not match extension '{ext}'"


def _err_magic_mismatch(ext: str) -> str:
    return f"Blocked: file content does not match extension '{ext}' (magic-byte check failed)"


def _err_binary_mismatch(ext: str) -> str:
    return f"Blocked: file content does not match extension '{ext}' (binary data)"


class _ParseError(Exception):
    """Structured, user-safe parse failure raised inside the worker thread."""

    def __init__(self, message: str, **extras: Any) -> None:
        super().__init__(message)
        self.message = message
        self.extras: dict[str, Any] = extras


def _clean_text(raw: str) -> str:
    """Strip lines and drop blanks — identical to extract_from_text's cleanup."""
    return "\n".join(line.strip() for line in raw.splitlines() if line.strip())


def _decode(data: bytes) -> str:
    """Decode uploaded text bytes defensively; never raises."""
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _parse_text(data: bytes) -> tuple[str, dict[str, Any]]:
    """Decode a txt/md upload. CPU-bound; only ever called in a worker thread."""
    return _decode(data), {"pages": None}


def _parse_pdf(data: bytes) -> tuple[str, dict[str, Any]]:
    """Extract text from a PDF. CPU-bound; only ever called in a worker thread."""
    if not HAS_PYPDF:
        raise _ParseError(ERR_NO_PYPDF)

    reader = PdfReader(io.BytesIO(data), strict=False)

    # Check encryption BEFORE touching pages: an encrypted reader constructs
    # fine and only explodes once a page is accessed.
    if reader.is_encrypted:
        raise _ParseError(ERR_ENCRYPTED)

    pages = len(reader.pages)
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        # Stop as soon as we have more than we would ever keep — mirrors
        # _read_capped_body's "never materialize more than the cap" rule.
        if total > MAX_CHARS:
            break
        chunk = page.extract_text() or ""
        parts.append(chunk)
        total += len(chunk)

    # `pages` is the true page count even when the loop broke early.
    return "\n".join(parts), {"pages": pages}


@dataclass(frozen=True)
class _Format:
    ext: str
    media_types: frozenset[str]
    magic: tuple[bytes, ...]
    parse: Callable[[bytes], tuple[str, dict[str, Any]]]  # (raw_text, extras)


# Adding a 4th format should be ONE entry here, never a new code path.
FORMATS: dict[str, _Format] = {
    ".pdf": _Format(
        ".pdf", frozenset({"application/pdf", "application/x-pdf"}), (b"%PDF-",), _parse_pdf
    ),
    ".txt": _Format(".txt", frozenset({"text/plain"}), (), _parse_text),
    ".md": _Format(
        ".md", frozenset({"text/markdown", "text/x-markdown", "text/plain"}), (), _parse_text
    ),
}


def _filename_error(raw: str) -> str | None:
    """Return why a client-supplied filename is unusable, or None if it is fine."""
    if not isinstance(raw, str) or not raw.strip():
        return ERR_NO_FILENAME
    name = raw.strip()
    if len(name) > MAX_FILENAME_LEN:
        return ERR_FILENAME_TOO_LONG
    if "/" in name or "\\" in name or ".." in name:
        return ERR_FILENAME_UNSAFE
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return ERR_FILENAME_UNSAFE
    return None


def safe_filename(raw: str) -> str | None:
    """Sanitize a client-supplied filename. None when it is not usable at all."""
    if _filename_error(raw) is not None:
        return None
    return raw.strip()


def check_upload_shape(filename: str, content_type: str | None = None) -> str | None:
    """Validate filename/extension/declared type. Returns an error message or None.

    Synchronous and dependency-free (like extractor.check_url_shape) so routes
    can reject obvious junk before reading a single byte of the body.
    """
    name_error = _filename_error(filename)
    if name_error is not None:
        return name_error

    ext = os.path.splitext(filename.strip())[1].lower()
    if ext not in FORMATS:
        return _err_unsupported_ext(ext)

    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type in IGNORED_CONTENT_TYPES:
        return None
    if media_type not in FORMATS[ext].media_types:
        return _err_type_mismatch(media_type, ext)

    return None


def sniff_format(head: bytes, ext: str) -> str | None:
    """Confirm the bytes match the extension. Returns the format ext, or None.

    Magic bytes are authoritative in BOTH directions: a .pdf must actually start
    with %PDF-, and a .txt/.md must NOT look like a binary container.
    """
    ext = ext.lower()
    fmt = FORMATS.get(ext)
    if fmt is None:
        return None

    window = bytes(head[:SNIFF_BYTES])
    if fmt.magic:
        return ext if window.startswith(fmt.magic) else None

    # No magic to match — apply the negative sniff instead.
    if b"\x00" in window or window.startswith(_BINARY_MAGIC):
        return None
    return ext


def _file_envelope(
    *,
    title: str = "",
    filename: str = "",
    content: str = "",
    error: str | None = None,
    fmt: str = "",
    body_truncated: bool = False,
    pages: int | None = None,
) -> dict[str, Any]:
    """Always the same 10 keys: the 6 base ones plus 4 file-specific extras."""
    return _envelope(
        None,
        title=title,
        domain=title,
        content=content,
        error=error,
        filename=filename,
        format=fmt,
        body_truncated=body_truncated,
        pages=pages,
    )


def _run_parse(data: bytes, ext: str) -> tuple[str, dict[str, Any]]:
    """All CPU work for one upload: parse + clean. Runs in a worker thread."""
    raw_text, extras = FORMATS[ext].parse(data)
    return _clean_text(raw_text), extras


async def extract_from_file(
    data: bytes,
    filename: str,
    content_type: str | None = None,
    *,
    label: str | None = None,
) -> dict[str, Any]:
    """Extract clean text from an uploaded file's bytes.

    Returns the standard 10-key envelope; failures are reported in `error`,
    never raised.
    """
    # Re-validate defensively: callers may have skipped check_upload_shape.
    shape_error = check_upload_shape(filename, content_type)
    if shape_error is not None:
        log.warning("Rejected upload: %s", shape_error)
        return _file_envelope(title=label or "", error=shape_error)

    name = safe_filename(filename) or ""
    title = label or name
    ext = os.path.splitext(name)[1].lower()

    if not data:
        return _file_envelope(title=title, filename=name, error=ERR_EMPTY_FILE)
    if len(data) > MAX_UPLOAD_BYTES:
        # Defense in depth — the route should already cap the stream.
        return _file_envelope(title=title, filename=name, error=ERR_TOO_LARGE)

    if sniff_format(data[:SNIFF_BYTES], ext) is None:
        error = _err_magic_mismatch(ext) if FORMATS[ext].magic else _err_binary_mismatch(ext)
        log.warning("Rejected upload %r: %s", name, error)
        return _file_envelope(title=title, filename=name, error=error)

    # ONE hop off the event loop wrapping ALL CPU work (decode/clean/page loop),
    # exactly like `await asyncio.to_thread(_parse_html, html)` in extractor.py.
    try:
        text, extras = await asyncio.to_thread(_run_parse, data, ext)
    except _ParseError as e:
        return _file_envelope(
            title=title, filename=name, error=e.message, fmt=ext, pages=e.extras.get("pages")
        )
    except Exception:
        # CRITICAL: never leak raw exception text into a file envelope. Unlike
        # extract_from_url (which formats `f"{type(e).__name__}: {e}"` for
        # network errors), pypdf messages can embed fragments of attacker-
        # supplied file bytes. Log the detail, return a generic message.
        # Do NOT "harmonize" this with the URL path.
        log.exception("Failed to parse uploaded file %r (%s)", name, ext)
        error = ERR_PDF_PARSE if ext == ".pdf" else ERR_NO_TEXT
        return _file_envelope(title=title, filename=name, error=error, fmt=ext)

    pages = extras.get("pages")
    if not text:
        error = ERR_PDF_NO_TEXT if ext == ".pdf" else ERR_NO_TEXT
        return _file_envelope(title=title, filename=name, error=error, fmt=ext, pages=pages)

    body_truncated = len(text) > MAX_CHARS
    if body_truncated:
        text = text[:MAX_CHARS] + TRUNCATION_SUFFIX

    return _file_envelope(
        title=title,
        filename=name,
        content=text,
        error=None,
        fmt=ext,
        body_truncated=body_truncated,
        pages=pages,
    )
