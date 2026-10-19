"""Uploaded-document parsing: which files index, and as what text."""

from __future__ import annotations

from theygent_control_plane.rag.parse import parse_document, supported_extension


def test_source_code_indexes_as_plain_text() -> None:
    source = (
        b'def refund_window():\n    """Days a customer has to return an item."""\n    return 30\n'
    )
    assert supported_extension("billing/refunds.py")
    assert supported_extension("web/App.TSX")
    parsed = parse_document(source, filename="billing/refunds.py")
    assert parsed.text == source.decode() and parsed.title is None


def test_binary_and_unknown_files_stay_unsupported() -> None:
    for name in ("weights.gguf", "Makefile", "archive.zip", "photo.png"):
        assert not supported_extension(name), name
