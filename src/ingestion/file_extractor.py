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
import json
import logging
import os
import sys
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
ERR_PDF_TOO_MANY_PAGES = "Blocked: PDF has too many pages (max {max_pages})"
ERR_PDF_TIMEOUT = "PDF took too long to parse"

# PDF parsing runs in a separate, resource-limited child process: pypdf on a
# hostile file (huge page trees, decompression bombs, costly content streams)
# can burn CPU and memory, and a worker THREAD can't be cancelled when the job
# times out. A child process can be killed.
MAX_PDF_PAGES = 300
PDF_PARSE_TIMEOUT = 30.0  # wall clock, seconds
PDF_PARSE_CPU_SECONDS = 20
PDF_PARSE_MAX_MEMORY = 1024 * 1024 * 1024  # 1 GiB address space (Linux only)
PDF_PARSE_CONCURRENCY = 2
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_pdf_gate: asyncio.Semaphore | None = None


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
    if pages > MAX_PDF_PAGES:
        raise _ParseError(ERR_PDF_TOO_MANY_PAGES.format(max_pages=MAX_PDF_PAGES), pages=pages)
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


def _limit_child_resources() -> None:  # pragma: no cover - runs in the child
    """Cap the PDF child's CPU seconds and (where supported) memory.

    Called first thing inside the child rather than as a `preexec_fn`, which
    is unsafe in a multi-threaded parent (the job workers use threads)."""
    if os.name != "posix":
        return
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (PDF_PARSE_CPU_SECONDS, PDF_PARSE_CPU_SECONDS))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (PDF_PARSE_MAX_MEMORY, PDF_PARSE_MAX_MEMORY))
    except (ValueError, OSError):
        pass  # macOS doesn't enforce RLIMIT_AS; the CPU cap and timeout still apply


def _pdf_child_main() -> None:  # pragma: no cover - runs in the child
    """Child entry point: PDF bytes on stdin, one JSON result on stdout."""
    _limit_child_resources()
    data = sys.stdin.buffer.read()
    try:
        text, extras = _run_parse(data, ".pdf")
        out = {"text": text, "extras": extras}
    except _ParseError as e:
        out = {"error": e.message, "extras": e.extras}
    sys.stdout.write(json.dumps(out))


async def _run_pdf_isolated(data: bytes) -> tuple[str, dict[str, Any]]:
    """Parse a PDF in a killable child process. Raises _ParseError on failure."""
    if not HAS_PYPDF:
        raise _ParseError(ERR_NO_PYPDF)

    global _pdf_gate
    if _pdf_gate is None:
        _pdf_gate = asyncio.Semaphore(PDF_PARSE_CONCURRENCY)

    code = (
        f"import sys; sys.path.insert(0, {_REPO_ROOT!r}); "
        "from src.ingestion.file_extractor import _pdf_child_main; _pdf_child_main()"
    )
    async with _pdf_gate:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=data), timeout=PDF_PARSE_TIMEOUT
            )
        except BaseException:
            # Timeout, or the ingest job itself was cancelled: never leave the
            # parser running behind us.
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
            raise

    if proc.returncode != 0:
        # Killed by RLIMIT_CPU (SIGXCPU), out of memory, or a pypdf crash.
        log.error(
            "PDF child exited with %s: %s",
            proc.returncode,
            stderr.decode(errors="replace")[-2000:],
        )
        raise RuntimeError(f"PDF parser exited with status {proc.returncode}")
    result = json.loads(stdout)
    if "error" in result:
        raise _ParseError(result["error"], **(result.get("extras") or {}))
    return result["text"], result["extras"]


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

    # ONE hop off the event loop wrapping ALL CPU work (decode/clean/page loop).
    # Text formats use a worker thread, like `_parse_html` in extractor.py; PDFs
    # go to a killable, resource-limited child process (see _run_pdf_isolated).
    try:
        if ext == ".pdf":
            text, extras = await _run_pdf_isolated(data)
        else:
            text, extras = await asyncio.to_thread(_run_parse, data, ext)
    except TimeoutError:
        log.warning("PDF parse of %r timed out after %ss", name, PDF_PARSE_TIMEOUT)
        return _file_envelope(title=title, filename=name, error=ERR_PDF_TIMEOUT, fmt=ext)
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
