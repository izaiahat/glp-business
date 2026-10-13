"""Lossless physical-source line numbering for reviewer context and replay."""
from __future__ import annotations


def physical_source_lines(data: bytes) -> list[str]:
    """Decode strict UTF-8; split LF/CRLF only, preserving all other content.

    Unicode NEL/line/paragraph separators are legal JSON string characters,
    not record boundaries. A standalone CR is content (including at EOF),
    whereas a CR immediately followed by LF belongs to the line delimiter.
    One terminating newline is not an extra empty source line.
    """
    text = data.decode("utf-8", errors="strict").replace("\r\n", "\n")
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines
