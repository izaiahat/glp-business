"""Offline contract checks: no real keys, browser, network or fleet lease."""
import contextlib
import importlib
import json
from pathlib import Path
import sys
import time
import types

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import codex_cloud_vpn_renewal as r
heavy = types.SimpleNamespace(LOCK=None, _oracle_gate=None, _critical_section=None, _take=None, _read_raw=None)


@pytest.mark.parametrize('contents', [b'{"seat":"other","at":0}', b'broken', b'{"seat":"codex-cloud-key-renewal","at":0}'])
def test_free_only_admission_preserves_even_stale_or_own_occupied_lock(tmp_path, monkeypatch, contents):
    lock = tmp_path / 'lock'
    lock.write_bytes(contents)
    monkeypatch.setattr(heavy, 'LOCK', lock)
    monkeypatch.setattr(heavy, '_oracle_gate', lambda: 0)
    monkeypatch.setattr(heavy, '_critical_section', contextlib.nullcontext)
    monkeypatch.setattr(heavy, '_take', lambda *a, **k: pytest.fail('must not reclaim'))
    monkeypatch.setattr(heavy, '_read_raw', lock.read_bytes)
    assert r.acquire_free(heavy, 'codex-cloud-key-renewal', 'rotation') == 1
    assert lock.read_bytes() == contents


def test_free_only_admission_runs_oracle_and_takes_under_guard(monkeypatch):
    events = []
    @contextlib.contextmanager
    def guard():
        events.append('enter')
        yield
        events.append('exit')
    monkeypatch.setattr(heavy, '_oracle_gate', lambda: events.append('oracle') or 0)
    monkeypatch.setattr(heavy, '_critical_section', guard)
    monkeypatch.setattr(heavy, '_read_raw', lambda: None)
    monkeypatch.setattr(heavy, '_take', lambda *a, **k: events.append('take') or 0)
    assert r.acquire_free(heavy, 'renewal', 'rotation') == 0
    assert events == ['oracle', 'enter', 'take', 'exit']


def test_main_uses_free_only_and_does_not_release_inherited(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(acquire=lambda *a: pytest.fail('reclaim-capable API'),
        acquire_free=lambda *a: calls.append('free') or 0,
        require_held=lambda *a: calls.append('require') or 0,
        release=lambda *a: calls.append('release'))
    monkeypatch.setitem(sys.modules, 'candidate_build_lock', fake)
    monkeypatch.setattr(r, 'acquire_free', lambda *a: calls.append('free') or 0, raising=False)
    monkeypatch.setattr(r, 'run_cli', lambda: calls.append('run') or 0)
    monkeypatch.delenv('CANDIDATE_BUILD_SEAT', raising=False)
    assert r.main() == 0
    assert calls == ['free', 'run', 'release']
    calls.clear()
    monkeypatch.setenv('CANDIDATE_BUILD_SEAT', 'DOT-OWNER')
    assert r.main() == 0
    assert calls == ['require', 'run']


def test_browser_source_never_reads_or_inserts_key():
    source = Path(r.__file__).with_name('codex_cloud_vpn_browser.py').read_text()
    assert 'Input.insertText' not in source
    assert 'private_read' not in source
    assert "secret['key']" not in source


def vault_fixture(tmp_path, monkeypatch, *, success=True, matching=True):
    import codex_cloud_vpn_vault as v
    events = []
    env = {'name':'fixture-account1', 'account':'fixture@example.test', 'environment_id':'fixture-config'}
    secret_path = tmp_path / 'pending-fixture-account1.json'
    r.atomic(secret_path, {'key':'OFFLINE-NOT-A-CREDENTIAL', 'key_id':'fixture-key', 'environment_id':env['environment_id']})
    row = {**env, 'phase':'VAULT_FILL_REQUIRED', 'key_id':'fixture-key', 'secret_path':str(secret_path),
           'editor_target':'fixture-target', 'draft_task_url':'https://chatgpt.com/local/fixture-draft'}
    state = {'environments':{env['name']:row}}
    meta = types.SimpleNamespace(id='vault_fixture', kind='login', origin='https://chatgpt.com',
        identifier=env['name'], label='Codex VPN fixture-account1 fixture-key (not account password)')
    store = types.SimpleNamespace(list_items=lambda:[meta], add_item=lambda *a, **k:pytest.fail('duplicate stage'))
    supervisor = types.SimpleNamespace(bind_exec_target=lambda t:events.append(('bind', t)) or {'ok':True},
        evaluate_runtime=lambda expr:{'ok':True,'result':matching})
    registry = types.SimpleNamespace(get_or_start=lambda **kw:supervisor)
    tools = types.SimpleNamespace(browser_vault_list=lambda:events.append('list') or json.dumps({'success':True,'items':[
        {'handle':meta.id,'origin':meta.origin,'identifier':meta.identifier,'label':meta.label,'kind':'login'}]}),
        browser_vault_fill=lambda handle, task_id:events.append(('fill',handle)) or json.dumps({'success':success,'origin':'https://chatgpt.com','kind':'login','filled_fields':1}))
    monkeypatch.setattr(v, 'dependencies', lambda:(store, registry, tools))
    monkeypatch.setattr(r, 'SECURE', tmp_path)
    monkeypatch.setattr(r, 'STATE', tmp_path / 'state.json')
    monkeypatch.setitem(sys.modules, 'gpt6pro_relay', types.SimpleNamespace(load_config=lambda:{}, account_of=lambda *a:{'cdp_port':9340}))
    return v, env, row, state, events


def test_vault_transfer_uses_registered_list_fill_on_bound_target(tmp_path, monkeypatch):
    v, env, row, state, events = vault_fixture(tmp_path, monkeypatch)
    v.fill(env,row,r,state)
    assert events == [('bind','fixture-target'),'list',('fill','vault_fixture')]
    assert row['phase'] == 'VAULT_FILLED'
    assert row['vault_fill_receipt']['origin'] == 'https://chatgpt.com'
    assert 'OFFLINE-NOT-A-CREDENTIAL' not in r.STATE.read_text()


@pytest.mark.parametrize('matching,success', [(False,True),(True,False)])
def test_vault_refusal_never_advances_or_replays(tmp_path, monkeypatch, matching, success):
    v, env, row, state, events = vault_fixture(tmp_path, monkeypatch,matching=matching,success=success)
    with pytest.raises(ValueError): v.fill(env,row,r,state)
    assert row['phase'] != 'VAULT_FILLED'
    before = list(events)
    if matching:
        with pytest.raises(ValueError,match='uncertain'): v.fill(env,row,r,state)
        assert events == before
    else:
        assert not any(isinstance(e,tuple) and e[0]=='fill' for e in events)


@pytest.mark.parametrize('phase', ['MINTED','VAULT_FILL_INTENT','VAULT_FILL_REQUIRED','VPN_SAVE_INTENT'])
def test_browser_save_requires_completed_vault_fill(phase):
    import codex_cloud_vpn_browser as b
    with pytest.raises(ValueError, match='secure_vault_fill_required'):
        b.save_vpn({}, {'phase':phase}, r, {})


def test_browser_save_binds_target_and_journals_before_click(tmp_path, monkeypatch):
    import codex_cloud_vpn_browser as b
    events = []
    url = 'https://chatgpt.com/local/fixture'
    row = {'phase':'VAULT_FILLED', 'editor_target':'target', 'draft_task_url':url,
        'vault_fill_receipt':{'origin':'https://chatgpt.com','target':'target','draft_task_url':url}}
    state = {'environments':{'fixture':row}}
    monkeypatch.setattr(b, 'owned_target', lambda *a:None)
    monkeypatch.setattr(b, 'js', lambda e:url if e=='location.href' else True, raising=False)
    monkeypatch.setattr(b, 'click', lambda *a:events.append(('click',a)))
    monkeypatch.setattr(b, 'wait_for', lambda *a:None)
    fake = types.SimpleNamespace(STATE=tmp_path/'state',
        atomic=lambda *a:events.append(('journal',row['phase'])))
    b.save_vpn({},row,fake,state)
    assert events[0] == ('journal','VPN_SAVE_INTENT')
    assert row['phase'] == 'VPN_SAVED'
    row['phase'] = 'VAULT_FILLED'
    row['editor_target'] = 'replacement-target'
    events.clear()
    with pytest.raises(ValueError,match='filled_editor_changed_no_save'):
        b.save_vpn({},row,fake,state)
    assert events == []


@pytest.mark.parametrize('command', ['--run','--mint','--check-safety'])
def test_no_effect_preflight_refuses_before_mint_browser_or_dispatch(tmp_path, monkeypatch, command):
    monkeypatch.setattr(r, 'API', lambda:pytest.fail('must not construct credential client'))
    monkeypatch.setattr(r, 'mint_due', lambda *a, **kw:pytest.fail('must not mint'))
    monkeypatch.setattr(r, 'browser', lambda *a, **kw:pytest.fail('must not browse'))
    monkeypatch.setattr(r, 'alert_failure', lambda *a: None)
    monkeypatch.setattr(sys, 'argv', ['renewal',command])
    monkeypatch.setattr(r, 'STATE', tmp_path / 'state.json')
    monkeypatch.setattr(r, 'CONFIG', tmp_path / 'absent-config.json')
    assert r.run_cli() == 1
    assert not r.STATE.exists()
