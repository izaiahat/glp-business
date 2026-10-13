"""Raw UTF-8 physical records shared by article review producer and custody."""


def physical_lines(data: bytes) -> list[str]:
    """Split LF/CRLF only; retain Unicode boundaries and standalone CR.

    A final LF terminates the last record, rather than creating a phantom
    record. Normalize CR only when it is part of an actual CRLF delimiter.
    Invalid UTF-8 fails closed instead of changing reviewed source bytes.
    """
    text = data.decode('utf-8', errors='strict').replace('\r\n', '\n')
    if not text:
        return []
    lines = text.split('\n')
    if text.endswith('\n'):
        lines.pop()
    return lines
