#!/usr/bin/env python3
"""Read-only custody of the registered best-terms producer's current packet.

The weekly producer (affiliate_best_terms_machine.py) publishes latest-run only
AFTER its immutable run-receipt, matrix and staged-switch manifest. This reader
uses that chain, never a glob, reconstructed historical bytes or a newly hashed
untrusted file. PASS/PARTIAL describe producer output, NOT adoption eligibility.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

BUSINESS = Path('/home/ubuntu/business')
LATEST_RUN = BUSINESS / 'reports/glp-clinic-scout/affiliates/BEST-TERMS-MACHINE-20260825/latest-run.json'


def _read(path: Path, digest: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if digest is not None and actual != digest:
        raise ValueError('best_terms_current_binding_drift')
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError('best_terms_current_document_invalid')
    return document, {'path': str(path), 'sha256': actual, 'bytes': len(raw)}


def current_best_terms_bindings(
    latest_path: Path = LATEST_RUN, *, business: Path = BUSINESS,
) -> dict[str, dict[str, Any]]:
    """Resolve only exact paths/hash references emitted by the current producer."""
    latest_path = latest_path.resolve()
    report_root = latest_path.parent
    latest, latest_binding = _read(latest_path)

    def bound(binding: Any, *, exact: Path | None = None, parent: Path | None = None):
        if not isinstance(binding, dict) or not isinstance(binding.get('sha256'), str) or len(binding['sha256']) != 64:
            raise ValueError('best_terms_current_binding_invalid')
        path = Path(str(binding.get('path') or ''))
        path = (path if path.is_absolute() else business / path).resolve()
        if (exact is not None and path != exact.resolve()) or (parent is not None and not path.is_relative_to(parent.resolve())):
            raise ValueError('best_terms_current_binding_path_invalid')
        return (*_read(path, binding['sha256']), path)

    receipt, receipt_binding, receipt_path = bound(latest.get('run_receipt'), parent=report_root / 'runs')
    if receipt_path.name != 'run-receipt.json':
        raise ValueError('best_terms_current_receipt_path_invalid')
    if (receipt.get('schema') != 'glp.best-terms-run.v1'
            or receipt.get('status') not in {'PASS', 'PARTIAL'}
            or receipt.get('mode') != 'weekly'
            or any(latest.get(key) != value for key, value in receipt.items())):
        raise ValueError('best_terms_current_receipt_contract_invalid')
    matrix, matrix_binding, _ = bound(receipt.get('matrix'), exact=receipt_path.parent / 'matrix.json')
    switches, switches_binding, _ = bound(receipt.get('switch_backlog'), exact=report_root / 'switch-backlog.json')
    if (matrix.get('schema') != 'glp.best-collectible-offer-selection.v1'
            or not isinstance(matrix.get('providers'), list)
            or matrix.get('winner_eligibility_violations')
            or switches.get('schema') != 'glp.best-terms-switch-backlog.v1'
            or not isinstance(switches.get('items'), list)):
        raise ValueError('best_terms_current_packet_contract_invalid')
    # The registered producer claims one UTC ISO-week window. A timed-out
    # successor may leave latest-run untouched: hashes alone cannot mean current.
    # No grace/fallback: both clocks must be within seven days and this ISO week.
    now = dt.datetime.now(dt.timezone.utc)

    def clock(document):
        value = document.get('generated_at_utc')
        try:
            if not isinstance(value, str) or 'T' not in value:
                raise ValueError
            generated = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            if generated.tzinfo is None or generated.utcoffset() is None:
                raise ValueError
            generated = generated.astimezone(dt.timezone.utc)
        except (ValueError, OverflowError):
            raise ValueError('best_terms_current_clock_invalid') from None
        if generated > now:
            raise ValueError('best_terms_current_future_clock')
        if now - generated > dt.timedelta(days=7):
            raise ValueError('best_terms_current_expired_clock')
        return generated

    receipt_clock = clock(receipt)
    matrix_clock = clock(matrix)
    window = receipt.get('weekly_window')
    expected = now.isocalendar()
    expected_window = f'{expected.year}-W{expected.week:02d}'
    if (not isinstance(window, dict)
            or window.get('window_id') != expected_window
            or receipt_clock.isocalendar()[:2] != expected[:2]
            or matrix_clock.isocalendar()[:2] != expected[:2]):
        raise ValueError('best_terms_current_window_invalid')
    # The producer writes the matrix before its immutable receipt; equal clocks
    # are normal at second resolution, but a newer matrix is not the same run.
    if matrix_clock > receipt_clock:
        raise ValueError('best_terms_current_clock_order_invalid')
    return {
        'best_terms_current_pointer': latest_binding,
        'deploy_packet_manifest': receipt_binding,
        'matrix_data': matrix_binding,
        'best_terms_current_switch_manifest': switches_binding,
    }
