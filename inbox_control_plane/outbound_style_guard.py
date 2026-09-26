"""Fail closed on prohibited dash characters at outbound email transport."""
from __future__ import annotations

import re
from email import policy
from email.message import Message
from email.parser import BytesParser

DASH_CHARS = "-‐‑‒–—―"


class OutboundStyleError(ValueError):
    """An outbound message contains a prohibited dash in prose."""


def _strip_non_prose(text: str) -> str:
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", " ", text)
    return text


def assert_dash_free(text: str, *, context: str = "outbound") -> None:
    prose = _strip_non_prose(str(text or ""))
    hits = sorted({char for char in prose if char in DASH_CHARS})
    if hits:
        raise OutboundStyleError(
            f"dash_rule_violation:{context}:"
            + ",".join(f"U+{ord(char):04X}" for char in hits)
        )


def message_text(message: Message) -> str:
    parts = [str(message.get("Subject") or "")]
    if message.is_multipart():
        body = message.get_body(preferencelist=("plain", "html"))
        if body is not None:
            parts.append(str(body.get_content()))
    else:
        parts.append(str(message.get_content()))
    return "\n".join(parts)


def assert_message_dash_free(message: Message, *, context: str = "outbound") -> None:
    assert_dash_free(message_text(message), context=context)


def assert_raw_rfc822_dash_free(raw: bytes, *, context: str = "outbound") -> None:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert_message_dash_free(message, context=context)
