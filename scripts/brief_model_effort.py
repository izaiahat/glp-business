"""Parse the routing declared by a seat brief and shown by the native TUI status bar."""
from __future__ import annotations

import re

_HEADER = re.compile(r"^(MODEL|PROVIDER|REASONING):[ \t]*([^\r\n]+)$", re.M | re.I)
_EFFORT = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_MODEL = re.compile(r"^[\w.-]+(?:\s+[\w.-]+)*$", re.I)


def brief_fields(body: str) -> tuple[str, str, str] | None:
    """Require one unambiguous top-level model/provider/effort declaration."""
    fields = {}
    for match in _HEADER.finditer(body[:4096]):
        key, value = match.group(1).upper(), match.group(2).strip().lower()
        if key in fields:
            return None
        fields[key] = value
    model, provider, effort = (fields.get(k, '') for k in ('MODEL', 'PROVIDER', 'REASONING'))
    # A semicolon may introduce a human note, but punctuation on the effort token is invalid.
    effort = effort.split(';', 1)[0].strip()
    if (not re.fullmatch(r'[a-z0-9][a-z0-9._-]*', model)
            or not re.fullmatch(r'[a-z0-9][a-z0-9._-]*', provider)
            or effort not in _EFFORT):
        return None
    return model, provider, effort


def brief_route(body: str) -> tuple[str, str] | None:
    """Only the top-level declaration counts; prose may name other child routes."""
    fields = brief_fields(body)
    return (fields[0], fields[2]) if fields else None


def route_instruction(model: str, effort: str) -> str:
    """Transport suffix for briefs; the artifact path itself remains pointer-only."""
    return (f' — BEFORE starting this brief, verify the native status bar shows '
            f'{model} {effort}; that readback is sufficient and requires no operator command. '
            f'Only if mismatched, request a guarded /model {model} or /reasoning {effort} correction. '
            f'Use the brief route for its oneshots too.')


def status_route(screen: str) -> tuple[str, str | None] | None:
    """Use only the status-bar model field, not conversation text or a prompt."""
    for line in reversed(screen.splitlines()):
        if not re.search(r"─\s+(?:ready|.+?)\s*│", line):
            continue
        parts = line.split("│")
        if len(parts) < 3:
            continue
        field = parts[1].strip()
        if not _MODEL.fullmatch(field):
            continue
        words = field.lower().split()
        effort = words.pop() if words and words[-1] in _EFFORT else None
        model = re.sub(r"[\s_-]+", "-", " ".join(words))
        return model, effort
    return None
