"""Pure provider x marketplace scope; network approval is not provider fit.

This is an exclusion gate, not an affirmative legal/terms/geo approval.
C5 unruled health rows remain pending; they are never auto-archived.
"""
from __future__ import annotations
from typing import Any
from pathlib import Path
import hashlib
import json
from affiliate_sheet_schema_guard import EXPECTED_HEADERS, REQUIRED_MARKETPLACE_PARENT_ROWS


def provider_row_scope(row_number: int, values: list[Any]) -> dict[str, Any]:
    cells = dict(zip(EXPECTED_HEADERS, values))
    text = lambda name: str(cells.get(name) or '').strip()
    program = text('Program / Network')
    category = text('Category').casefold()
    network = text('Network / Route').casefold()
    fit = text('Fit Reason').casefold()
    vertical = text('Vertical').casefold()
    if not program:
        kind, reason = 'blank', 'blank_row'
    elif 'fella' in program.casefold():
        kind, reason = 'permanently_excluded', 'permanent_fella_exclusion'
    elif row_number in set(REQUIRED_MARKETPLACE_PARENT_ROWS.values()):
        kind, reason = 'marketplace_parent', 'canonical_marketplace_parent_not_provider'
    elif category in {'network rail', 'affiliate marketplace/network'} or network in {
        'network signup', 'publisher marketplace account', 'network marketplace',
    }:
        kind, reason = 'network_account', 'network_account_not_provider'
    elif vertical == 'out-of-scope':
        kind, reason = 'out_of_scope', 'contract:PARK_NOT_FIT:park_not_glp_relevant'
    elif 'fit gate pending' in fit or 'fit-gate-pending' in fit:
        kind, reason = 'fit_pending', 'row_fit_ruling_pending'
    else:
        kind, reason = 'provider_candidate', 'continue_existing_provider_checks'
    return {'eligible': kind == 'provider_candidate', 'row_class': kind, 'reason': reason}


def require_scope_retraction(entry: dict[str, Any], row_number: int,
                             actual: list[Any], root: Path) -> None:
    """Exact row-scoped, idempotent flag retraction; never grants a gate."""
    evidence = entry.get('evidence') or {}
    path = (root / str(evidence.get('scope_retraction_path') or '')).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise RuntimeError('provider_scope_retraction_manifest_path')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != evidence.get('scope_retraction_sha256'):
        raise RuntimeError('provider_scope_retraction_manifest_hash')
    manifest = json.loads(raw)
    if manifest.get('schema') != 'glp.provider_scope_retraction.v1':
        raise RuntimeError('provider_scope_retraction_manifest_schema')
    matches = [r for r in manifest.get('rows', []) if r.get('sheet_row') == row_number]
    if len(matches) != 1:
        raise RuntimeError('provider_scope_retraction_row_not_unique')
    record = matches[0]
    if record.get('action') != 'revoke_wire_ready' or not record.get('source_cells'):
        raise RuntimeError('provider_scope_retraction_action_invalid')
    scope = provider_row_scope(row_number, actual)
    if scope['eligible'] or scope['row_class'] in {'blank', 'permanently_excluded'}:
        raise RuntimeError('provider_scope_retraction_not_allowed')
    cells = {str(k):v for k,v in entry['cells'].items()}
    if str(cells.get('19')).upper() != 'FALSE':
        raise RuntimeError('provider_scope_retraction_must_revoke')
    clear_slug = record.get('clear_slug') is True
    if ('9' in cells) != clear_slug or (clear_slug and cells['9'] != ''):
        raise RuntimeError('provider_scope_retraction_slug_scope')
    if clear_slug and scope['row_class'] != 'fit_pending':
        raise RuntimeError('provider_scope_retraction_slug_not_pending_fit')
    required = {'2','3','4','7','9','10','19','20','25'}
    source = record['source_cells']
    if set(source) != required:
        raise RuntimeError('provider_scope_retraction_source_projection')
    if str(actual[20]).upper() == 'TRUE':
        raise RuntimeError('provider_scope_retraction_live_row')
    for key, before in source.items():
        current = actual[int(key)]
        if key == '19' and str(current).upper() == 'FALSE':
            continue  # exact postimage is replay-safe
        if key == '9' and clear_slug and current == '':
            continue
        if current != before:
            raise RuntimeError(f'provider_scope_retraction_source_drift:{row_number}:{key}')
