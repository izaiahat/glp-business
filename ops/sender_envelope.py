#!/usr/bin/env python3
"""Fail-closed sender envelope adapters for Allocant, GLP, and Boring AI."""
from dataclasses import dataclass
from typing import Callable, Dict, Any

VENTURES = {'allocant', 'glp', 'boring-ai'}

class SendBlocked(RuntimeError):
    pass

@dataclass(frozen=True)
class EnvelopeItem:
    venture: str
    risk_class: str
    approved: bool = False
    approval_id: str = ''
    ask: str = ''


def normalize_venture(venture: str) -> str:
    v = venture.strip().lower().replace('_', '-')
    if v in {'boring', 'boringai'}:
        v = 'boring-ai'
    if v not in VENTURES:
        raise SendBlocked(f'unknown venture={venture}; fail-closed')
    return v


def assert_can_send(venture: str, item: Dict[str, Any] | EnvelopeItem) -> bool:
    v = normalize_venture(venture)
    data = item.__dict__ if isinstance(item, EnvelopeItem) else dict(item)
    risk = str(data.get('risk_class', '')).upper()
    approved = bool(data.get('approved'))
    approval_id = str(data.get('approval_id') or '')
    if risk == 'RED':
        raise SendBlocked(f'{v} send blocked: RED item cannot auto-send')
    if not approved or not approval_id:
        raise SendBlocked(f'{v} send blocked: explicit approval_id required')
    return True


def sender_adapter(venture: str, send_fn: Callable[..., Any], item: Dict[str, Any], *args: Any, **kwargs: Any) -> Any:
    assert_can_send(venture, item)
    return send_fn(*args, **kwargs)
