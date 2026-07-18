"""Uploaded document bytes → markdown-ish text, behind one function.

markitdown is the lightweight choice: pure-Python converters (pdfminer, mammoth,
python-pptx, pandas) with no ML weights to bundle — right for a locally-shipped product. Plain
text and markdown short-circuit it entirely. Scanned-PDF OCR is out of scope here (a heavier,
model-backed parser is an additive upgrade behind this same function).

Parsing runs in a worker thread (``asyncio.to_thread`` at the call site) — pdfminer on a large
PDF is CPU-bound for seconds.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import PurePosixPath

_TEXTUAL_EXTENSIONS = {".md", ".markdown", ".txt", ".rst", ".text"}
_SUPPORTED_EXTENSIONS = _TEXTUAL_EXTENSIONS | {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".xls",
    ".csv",
    ".html",
    ".htm",
    ".json",
    ".xml",
    ".epub",
}


class UnsupportedDocument(ValueError):
    """The upload's type has no converter — surfaced as a clean 422, never a crash."""


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    title: str | None


def supported_extension(filename: str) -> bool:
    return PurePosixPath(filename.lower()).suffix in _SUPPORTED_EXTENSIONS


def parse_document(
    data: bytes, *, filename: str, content_type: str | None = None
) -> ParsedDocument:
    """Convert one uploaded file to text. Raises :class:`UnsupportedDocument` for types no
    converter handles; any converter failure propagates (the ingest service records it as the
    document's honest ``failed`` error)."""
    suffix = PurePosixPath(filename.lower()).suffix
    if suffix in _TEXTUAL_EXTENSIONS:
        return ParsedDocument(text=data.decode("utf-8", errors="replace"), title=None)
    if suffix not in _SUPPORTED_EXTENSIONS:
        raise UnsupportedDocument(
            f"unsupported document type {suffix or '(no extension)'!r}; "
            f"supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
        )
    # Imported lazily: markitdown drags in its converter stack, which only the upload path needs.
    from markitdown import MarkItDown, StreamInfo

    result = MarkItDown().convert_stream(
        io.BytesIO(data),
        stream_info=StreamInfo(extension=suffix, mimetype=content_type, filename=filename),
    )
    text = (result.text_content or "").strip()
    if not text:
        raise UnsupportedDocument(
            f"no text could be extracted from {filename!r} (scanned/image-only document?)"
        )
    return ParsedDocument(text=text, title=result.title or None)
