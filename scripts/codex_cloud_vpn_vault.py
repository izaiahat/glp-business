"""Fail-closed renewal credential-transfer boundary.

The registered vault currently enforces origin only, not authenticated account,
config, exact editor document/URL and target atomically with the write. Until a
supported exact-context contract exists, no pending secret is read or staged,
no browser/vault operation is started, and no VAULT_FILLED receipt is recorded.
"""
from pathlib import Path


def fill(env, row, renewal, state):
    if row.get('phase') in ('VAULT_FILL_INTENT', 'VPN_SAVE_INTENT'):
        raise ValueError('vault_or_save_uncertain_no_replay')
    if row.get('phase') != 'VAULT_FILL_REQUIRED':
        raise ValueError('vault_fill_phase_required')
    if any(row.get(k) != env[k] for k in ('name', 'account', 'environment_id')):
        raise ValueError('vault_account_environment_binding_mismatch')
    path = Path(row['secret_path'])
    expected = renewal.SECURE / ('pending-' + env['name'] + '.json')
    if path != expected or path.parent.resolve() != renewal.SECURE.resolve():
        raise ValueError('private_key_path_binding_mismatch')
    target, url = row.get('editor_target'), row.get('draft_task_url')
    if not target or not url or not url.startswith('https://chatgpt.com/local/'):
        raise ValueError('owned_editor_required_for_vault')
    # The maintained registered browser_vault_fill(handle, task_id) checks only
    # origin at the write. It cannot enforce this account/environment + exact
    # editor URL/document in the same operation as the credential write. An
    # earlier precheck (even after staging) and a later Save check both race.
    # No supported exact-context contract exists: refuse BEFORE private_read,
    # vault staging/list/fill, browser startup or intent/state writes. Do not
    # invent an accepting adapter, use raw CDP, or record VAULT_FILLED here.
    raise ValueError('registered_vault_exact_context_fill_unavailable')
