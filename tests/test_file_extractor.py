"""Tests for src.ingestion.file_extractor — file/PDF upload ingestion.

Contract (see WP-F5 task spec / CLAUDE.md): the envelope returned by
`extract_from_file` always has exactly 10 keys — `url` (always None),
`title`, `domain`, `content`, `char_count`, `error`, `filename`, `format`,
`body_truncated`, `pages` (int for PDF, None for txt/md, key always
present). `extract_from_text`'s 6-key envelope (url/title/domain/content/
char_count/error) is a strict subset of that key set.

`src.ingestion.file_extractor` is being built by a parallel agent; until it
exposes the contracted names this whole module fails to *collect* (an
ImportError at module scope), which is the expected "awaiting integration"
shape for this work package — not a bug in these tests.
"""
from __future__ import annotations

import io

import pypdf
import pytest

import src.ingestion.file_extractor as file_extractor
from src.ingestion.extractor import MAX_CHARS, TRUNCATION_SUFFIX, extract_from_text
from src.ingestion.file_extractor import (
    MAX_FILENAME_LEN,
    MAX_UPLOAD_BYTES,
    SUPPORTED_EXTENSIONS,
    check_upload_shape,
    extract_from_file,
    safe_filename,
    sniff_format,
)

ENVELOPE_KEYS = {
    "url", "title", "domain", "content", "char_count", "error",
    "filename", "format", "body_truncated", "pages",
}


# ------------------------------------------------------------------
# PDF fixture builders
# ------------------------------------------------------------------
def _build_pdf(objects: list[bytes]) -> bytes:
    """Assemble a minimal, xref-correct PDF from body strings (1-indexed)."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    n = len(objects) + 1
    out += f"xref\n0 {n}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def _text_pdf_bytes() -> bytes:
    content_stream = b"BT /F1 12 Tf 20 100 Td (Ada Lovelace works at Analytical Engine) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content_stream)} >>\nstream\n".encode()
        + content_stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf_bytes = _build_pdf(objects)
    # Verified round-trip (see task notes): pypdf must actually read the text
    # layer back, or this fixture is worthless for the "happy path" test.
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 1
    extracted = reader.pages[0].extract_text()
    assert "Lovelace" in extracted, f"fixture round-trip failed: {extracted!r}"
    return pdf_bytes


def _blank_pdf_bytes() -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _encrypted_pdf_bytes() -> bytes:
    reader = pypdf.PdfReader(io.BytesIO(_text_pdf_bytes()))
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("pw", algorithm="RC4-128")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _corrupt_pdf_bytes() -> bytes:
    return b"%PDF-1.4\n" + b"\x00garbage" * 32


TEXT_PDF_BYTES = _text_pdf_bytes()
BLANK_PDF_BYTES = _blank_pdf_bytes()
ENCRYPTED_PDF_BYTES = _encrypted_pdf_bytes()
CORRUPT_PDF_BYTES = _corrupt_pdf_bytes()


# ------------------------------------------------------------------
# Module constants
# ------------------------------------------------------------------
def test_max_upload_bytes_is_3mb() -> None:
    assert MAX_UPLOAD_BYTES == 3 * 1024 * 1024


def test_supported_extensions() -> None:
    assert set(SUPPORTED_EXTENSIONS) == {".pdf", ".txt", ".md"}


def test_max_filename_len() -> None:
    assert MAX_FILENAME_LEN == 300


# ------------------------------------------------------------------
# safe_filename
# ------------------------------------------------------------------
def test_safe_filename_rejects_path_traversal() -> None:
    assert safe_filename("../../etc/passwd") is None


def test_safe_filename_rejects_empty() -> None:
    assert safe_filename("") is None


def test_safe_filename_accepts_plain_name() -> None:
    assert safe_filename("notes.txt") == "notes.txt"


def test_safe_filename_rejects_too_long() -> None:
    long_name = ("a" * (MAX_FILENAME_LEN + 1)) + ".txt"
    assert safe_filename(long_name) is None


# ------------------------------------------------------------------
# check_upload_shape — extension + declared content-type handling
# ------------------------------------------------------------------
def test_check_upload_shape_rejects_unsupported_extension() -> None:
    err = check_upload_shape("report.docx")
    assert err is not None
    assert "Blocked: unsupported file type" in err


def test_check_upload_shape_declared_pdf_on_txt_is_mismatch() -> None:
    err = check_upload_shape("notes.txt", "application/pdf")
    assert err is not None
    assert "does not match extension" in err


@pytest.mark.parametrize(
    "content_type", ["", "application/octet-stream", "binary/octet-stream", None]
)
def test_check_upload_shape_ignores_generic_declared_types(content_type) -> None:
    assert check_upload_shape("notes.md", content_type) is None


# ------------------------------------------------------------------
# sniff_format — magic bytes authoritative both ways.
# Returns the matching ext on success, None on mismatch (NOT an error string).
# ------------------------------------------------------------------
def test_sniff_format_pdf_requires_pdf_magic() -> None:
    assert sniff_format(b"not a pdf at all", ".pdf") is None


def test_sniff_format_accepts_real_pdf_magic() -> None:
    assert sniff_format(TEXT_PDF_BYTES[:64], ".pdf") == ".pdf"


def test_sniff_format_txt_rejects_pdf_magic() -> None:
    assert sniff_format(b"%PDF-1.4\nrest", ".txt") is None


def test_sniff_format_txt_rejects_nul_bytes() -> None:
    assert sniff_format(b"hello\x00world", ".txt") is None


# ------------------------------------------------------------------
# extract_from_file — happy paths
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_extract_txt_happy_path() -> None:
    result = await extract_from_file(b"Ada Lovelace worked on the Analytical Engine.", "notes.txt")
    assert result["error"] is None
    assert result["url"] is None
    assert result["filename"] == "notes.txt"
    assert result["format"] == ".txt"
    assert result["pages"] is None
    assert "Lovelace" in result["content"]
    assert result["char_count"] == len(result["content"])


@pytest.mark.asyncio
async def test_extract_md_happy_path() -> None:
    result = await extract_from_file(b"# Title\n\nAda Lovelace content.", "notes.md")
    assert result["error"] is None
    assert result["filename"] == "notes.md"
    assert result["format"] == ".md"
    assert result["pages"] is None
    assert "Lovelace" in result["content"]


@pytest.mark.asyncio
async def test_extract_pdf_happy_path() -> None:
    result = await extract_from_file(TEXT_PDF_BYTES, "doc.pdf")
    assert result["error"] is None
    assert result["filename"] == "doc.pdf"
    assert result["format"] == ".pdf"
    assert result["pages"] == 1
    assert "Lovelace" in result["content"]


# ------------------------------------------------------------------
# extract_from_file — PDF error paths
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_extract_corrupt_pdf_returns_error_no_exception() -> None:
    result = await extract_from_file(CORRUPT_PDF_BYTES, "corrupt.pdf")
    assert result["error"] is not None
    assert "Could not parse PDF" in result["error"]
    # No raw exception class/traceback leakage into the client-facing field.
    assert "Traceback" not in result["error"]
    assert "PdfReadError" not in result["error"]


@pytest.mark.asyncio
async def test_extract_encrypted_pdf_reports_encrypted() -> None:
    result = await extract_from_file(ENCRYPTED_PDF_BYTES, "secret.pdf")
    assert result["error"] is not None
    assert "Encrypted" in result["error"]


@pytest.mark.asyncio
async def test_extract_blank_pdf_reports_scanned_image_only() -> None:
    result = await extract_from_file(BLANK_PDF_BYTES, "scanned.pdf")
    assert result["error"] is not None
    assert "scanned/image-only" in result["error"]


# ------------------------------------------------------------------
# extract_from_file — magic-byte mismatches (content authoritative both ways)
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_extract_txt_containing_pdf_magic_is_mismatch() -> None:
    result = await extract_from_file(b"%PDF-1.4\nnot really text-only", "fake.txt")
    assert result["error"] is not None
    assert "does not match extension" in result["error"]


@pytest.mark.asyncio
async def test_extract_pdf_containing_plain_text_is_mismatch() -> None:
    result = await extract_from_file(b"just some plain text, not a real pdf", "fake.pdf")
    assert result["error"] is not None
    assert "does not match extension" in result["error"]


# ------------------------------------------------------------------
# extract_from_file — unsupported extension / empty file
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_extract_unsupported_extension_docx() -> None:
    result = await extract_from_file(b"hello", "report.docx")
    assert result["error"] is not None
    assert "Blocked: unsupported file type" in result["error"]


@pytest.mark.asyncio
async def test_extract_empty_file_is_error() -> None:
    result = await extract_from_file(b"", "empty.txt")
    assert result["error"] is not None
    assert "Blocked: empty file" in result["error"]


@pytest.mark.asyncio
async def test_extract_no_extractable_text_from_pdf_like_content() -> None:
    # A blank-page PDF is already covered by the "scanned/image-only" test
    # above; this exercises the txt/md "no extractable text" branch instead
    # — whitespace-only content should not slip through as a real doc.
    result = await extract_from_file(b"   \n\t\n   ", "whitespace.txt")
    assert result["error"] is not None
    assert "No extractable text in file" in result["error"]


# ------------------------------------------------------------------
# MAX_CHARS truncation (shared TRUNCATION_SUFFIX with the URL/text path)
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_extract_txt_truncates_at_max_chars() -> None:
    big_text = "Ada Lovelace. " * (MAX_CHARS // len("Ada Lovelace. ") + 1000)
    result = await extract_from_file(big_text.encode(), "big.txt")
    assert result["error"] is None
    assert result["body_truncated"] is True
    assert result["content"].endswith(TRUNCATION_SUFFIX)


# ------------------------------------------------------------------
# HAS_PYPDF fallback
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pdf_support_unavailable_when_has_pypdf_false(monkeypatch) -> None:
    monkeypatch.setattr(file_extractor, "HAS_PYPDF", False)

    pdf_result = await extract_from_file(TEXT_PDF_BYTES, "doc.pdf")
    assert pdf_result["error"] is not None
    assert "PDF support unavailable" in pdf_result["error"]

    # The txt path must be entirely unaffected by the PDF library being down.
    txt_result = await extract_from_file(b"still works fine", "notes.txt")
    assert txt_result["error"] is None
    assert "still works fine" in txt_result["content"]


# ------------------------------------------------------------------
# Envelope key parity
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_envelope_has_exactly_ten_keys() -> None:
    result = await extract_from_file(b"hello world", "notes.txt")
    assert set(result.keys()) == ENVELOPE_KEYS


def test_extract_from_text_keys_are_subset_of_file_envelope() -> None:
    text_result = extract_from_text("x")
    assert set(text_result.keys()) <= ENVELOPE_KEYS
    assert set(text_result.keys()) == {
        "url", "title", "domain", "content", "char_count", "error",
    }
