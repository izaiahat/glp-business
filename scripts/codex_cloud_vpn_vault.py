"""Renewal-key staging and registered secure-vault fill; no browser secret code.

Only already-issued pending renewal keys are staged. The vault tool resolves the
secret internally on an exact-origin, explicitly target-bound supervisor. No
credential is included in harness source, argv, public state or tool output.
"""
import json
from pathlib import Path
import uuid


def dependencies():
    from agent.vault_store import get_vault_store
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    from tools import browser_vault_tool
    from hermes_constants import get_hermes_home
    if Path(get_hermes_home()).resolve() != Path('/home/ubuntu/.hermes'):
        raise ValueError('renewal_default_profile_required')
    return get_vault_store(), SUPERVISOR_REGISTRY, browser_vault_tool


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
    import gpt6pro_relay as relay
    account = relay.account_of(relay.load_config(), env['account'])
    store, registry, tools = dependencies()
    session = 'codex-vpn-vault-' + uuid.uuid4().hex
    supervisor = registry.get_or_start(task_id=session,
        cdp_url='http://127.0.0.1:' + str(account['cdp_port']))
    if not supervisor.bind_exec_target(target).get('ok'):
        raise ValueError('vault_owned_target_binding_failed')
    expression = """(async()=>{
      const s=await(await fetch('/api/auth/session')).json();
      return location.href===%s && s.user?.email===%s &&
        document.querySelectorAll('input[type=password]').length===1 &&
        performance.getEntriesByType('resource').some(e=>e.name.includes(%s));
    })()""" % (json.dumps(url), json.dumps(env['account']),
                   json.dumps('/api/codex-cloud/v1/environment-configs/' + env['environment_id']))
    observed = supervisor.evaluate_runtime(expression)
    if not observed.get('ok') or observed.get('result') is not True:
        raise ValueError('vault_exact_account_editor_not_verified')
    private = renewal.private_read(path)
    if private['key_id'] != row['key_id'] or private['environment_id'] != env['environment_id']:
        raise ValueError('private_key_binding_mismatch')
    label = 'Codex VPN ' + env['name'] + ' ' + row['key_id'] + ' (not account password)'
    found = [m for m in store.list_items() if m.label == label]
    if len(found) > 1:
        raise ValueError('vault_stage_ambiguous')
    if found:
        meta = found[0]
        if meta.kind != 'login' or meta.origin != 'https://chatgpt.com' or meta.identifier != env['name']:
            raise ValueError('vault_staged_metadata_mismatch')
    else:
        meta = store.add_item('login', label,
            {'identifier_type':'username', 'identifier':env['name'], 'password':private['key']},
            origin='https://chatgpt.com')
    private = None
    listed = json.loads(tools.browser_vault_list())
    matches = [m for m in listed.get('items', []) if m.get('handle') == meta.id
        and m.get('origin') == 'https://chatgpt.com' and m.get('identifier') == env['name']
        and m.get('label') == label and m.get('kind') == 'login']
    if not listed.get('success') or len(matches) != 1:
        raise ValueError('vault_staged_handle_not_listed')
    row.update(phase='VAULT_FILL_INTENT', vault_handle=meta.id)
    renewal.atomic(renewal.STATE, state)
    # The ONLY secret-to-page operation is the registered vault fill path.
    result = json.loads(tools.browser_vault_fill(meta.id, task_id=session))
    if result.get('success') is not True or result.get('origin') != 'https://chatgpt.com' or result.get('kind') != 'login':
        raise ValueError('secure_vault_fill_failed_no_save')
    row.update(phase='VAULT_FILLED', vault_fill_receipt={
        'origin':result['origin'], 'kind':result['kind'], 'handle':meta.id,
        'target':target, 'draft_task_url':url})
    renewal.atomic(renewal.STATE, state)
