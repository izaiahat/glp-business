from types import SimpleNamespace
from pathlib import Path
import json
import time
from scripts import affiliate_catalog_batch as batch
from scripts import affiliate_catalog_discovery as discovery


def contract(sweeps):
    def verdict(doc):
        complete = not doc.get('blockers') and doc.get('reported_total') is not None
        return {'complete': complete, 'verdict': 'COMPLETE' if complete else 'BLOCKED',
                'detail': 'complete' if complete else 'unknown',
                'enumerated': len(doc['programs']), 'reported_total': doc.get('reported_total')}
    return SimpleNamespace(SWEEPS=sweeps, SCHEMA='fixture', completeness=verdict,
                           _base=SimpleNamespace(RequestsTransport=lambda **kwargs: None))


def inventory():
    return {'programs': [{'program_id': '001', 'name': 'GLP clinic',
                          'description': 'GLP-1 telehealth', 'offer_status': 'paused'}],
            'reported_total': 1, 'auth_state': 'PASS', 'blockers': []}


def test_timeout_rotates_and_completed_catalog_survives(tmp_path):
    calls = []
    def slow(_):
        calls.append('slow')
        time.sleep(.2)
    def fast(_):
        calls.append('fast')
        return inventory()
    legacy = contract({'slow': slow, 'fast': fast})
    state = tmp_path / 'state.json'
    first = batch.run(legacy, tmp_path / 'first', state=state,
                      budget=2, source_timeout=.02)
    assert first['networks']['slow']['complete'] is False
    assert first['networks']['fast']['complete'] is True
    second = batch.run(legacy, tmp_path / 'second', state=state,
                       budget=2, source_timeout=.02)
    assert calls == ['slow', 'fast', 'slow']
    assert second['networks']['fast']['complete'] is True
    assert json.loads((tmp_path / 'second/fast-inventory.json').read_text())['programs'][0]['program_id'] == '001'


def test_held_network_never_called(tmp_path):
    def forbidden(_):
        raise AssertionError('held network invoked')
    summary = batch.run(contract({'xentraffic': forbidden}), tmp_path / 'out',
                        state=tmp_path / 'state.json')
    assert summary['batch']['attempted'] == []
    assert not summary['all_complete']


def test_portal_refusal_preserves_partial_api_rows():
    partial = inventory()
    partial['auth_state'] = 'PARTIAL'
    output = batch.portal_refusal('clickbank', 'ClickBank', partial)
    assert len(output['programs']) == 1
    assert output['blockers'][0]['error_class'] == 'authorized_vault_browser_required'
    assert output['auth_state'] == 'PARTIAL'


def test_discovery_uses_complete_fresh_catalog_without_dropping_holds(tmp_path):
    legacy = contract({'awin': lambda _: inventory()})
    state = tmp_path / 'state.json'
    batch.run(legacy, tmp_path / 'out', state=state)
    core = SimpleNamespace(glp_evidence=lambda *args: {'fit': True})
    result = discovery.adapters(core, state=state)
    assert result['awin']['status'] == 'AUTHENTICATED_CATALOG_COMPLETE'
    assert result['awin']['programs'][0]['offer_status'] == 'paused'
    document = json.loads(state.read_text())
    document['networks']['awin']['complete'] = False
    batch.save(state, document)
    assert discovery.adapters(core, state=state) == {}
