from types import SimpleNamespace
from pathlib import Path
import json
import time
from scripts import affiliate_catalog_batch as batch
from scripts import affiliate_catalog_discovery as discovery
import pytest


def test_replay_reads_carried_operations_without_retiring_unknowns():
    rows = [{'kind': kind, 'arguments': args, 'status': 'pending'} for kind, args in [
        ('search', ['old clinical query', 10]), ('scrape', ['https://example.invalid/affiliate']),
        ('catalog:partnerize', []), ('unknown_reader', [])]]
    checkpoint = SimpleNamespace(doc={'operations': dict(enumerate(rows))}, save=lambda: None)
    calls = []
    def reader(index):
        def invoke(*args):
            calls.append((rows[index]['kind'], list(args)))
            rows[index]['status'] = 'ok'
        return invoke
    discovery.replay_pending(checkpoint, {rows[i]['kind']: reader(i) for i in range(3)}, ())
    assert calls == [(row['kind'], row['arguments']) for row in rows[:3]]
    assert rows[3]['status'] == 'pending'


def test_replay_preserves_deferred_reads_and_never_opens_tracking_url():
    rows = [{'kind': 'search', 'arguments': ['query', 10], 'status': 'pending'},
            {'kind': 'scrape', 'arguments': ['https://tracking.invalid/link'], 'status': 'pending'}]
    checkpoint = SimpleNamespace(doc={'operations': dict(enumerate(rows))}, save=lambda: None)
    def deferred(*args):
        raise ValueError('budget exhausted; no read started')
    def forbidden(*args):
        raise AssertionError('tracking URL opened')
    discovery.replay_pending(checkpoint, {'search': deferred, 'scrape': forbidden},
                             (ValueError,), safe_scrape=lambda url: False)
    assert rows[0]['status'] == 'pending'
    assert rows[1]['status'] == 'unavailable'
    assert rows[1]['reason'] == 'unsafe_legacy_scrape_url'


def test_explicit_manual_fresh_read_does_not_relabel_cached_evidence(tmp_path, monkeypatch):
    calls = []
    def read(transport):
        calls.append('actual injected API read')
        return inventory()
    legacy = contract({'awin': read})
    state = tmp_path / 'state.json'
    batch.run(legacy, tmp_path / 'one', state=state)
    batch.run(legacy, tmp_path / 'two', state=state)
    assert len(calls) == 1
    monkeypatch.setenv('AFFILIATE_CATALOG_REFRESH', '1')
    result = batch.run(legacy, tmp_path / 'fresh', state=state)
    assert len(calls) == 2
    assert result['batch']['attempted'] == ['awin']
    assert result['batch']['forced_fresh_read'] is True


def contract(sweeps):
    def verdict(enumerated, total, blockers):
        complete = not blockers and total is not None and enumerated >= total
        return ('COMPLETE', 'complete') if complete else ('BLOCKED', 'unknown')
    return SimpleNamespace(SWEEPS=sweeps, SCHEMA='fixture', verdict=verdict,
                           PORTAL_ONLY={},
                           b=SimpleNamespace(RequestsTransport=lambda **kwargs: None))


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


def test_awin_bucket_denominator_and_negative_relationship_survive(tmp_path):
    doc = {'relationships': {'publisher': {'pending': [
        {'id': '001', 'name': 'GLP clinic', 'displayUrl': 'https://example.invalid'}]}},
           'enumerated': 1, 'reported_total': 1, 'blockers': [], 'auth_state': 'PASS'}
    legacy = contract({'awin': lambda _: doc})
    state = tmp_path / 'state.json'
    summary = batch.run(legacy, tmp_path / 'out', state=state)
    assert summary['networks']['awin']['enumerated'] == 1
    assert summary['networks']['awin']['completeness'] == 'COMPLETE'
    core = SimpleNamespace(glp_evidence=lambda name, text, url: {'fit': True})
    row = discovery.adapters(core, state=state)['awin']['programs'][0]
    assert row['program_id'] == '001'
    assert row['relationship'] == 'pending'


@pytest.mark.parametrize('state', ['Active', 'Paused', ''])
def test_awin_api_status_survives_normalizer_and_scheduled_bucket(monkeypatch, state):
    from scripts.marketplace_adapters import awin, _base
    monkeypatch.setattr(awin, '_headers', lambda: {})
    raw = {'id': '001', 'name': 'Fixture GLP clinic', 'status': state,
           'primaryRegion': {'countryCode': 'US'}}
    transport = _base.RecordingTransport([
        ('GET', '/programmes', _base.json_response([raw]))])
    legacy = SimpleNamespace(awin=awin, SCHEMA='fixture')
    doc = batch.awin_inventory(legacy, transport)
    rows = doc['relationships'][str(awin.PUBLISHER_ID)]
    assert rows['notjoined'][0]['network_status'] == state
    assert rows['pending'][0]['network_status'] == state
    assert rows['notjoined'][0]['program_id'] == '001'
    assert doc['enumerated'] == 5


def test_awin_checkpoint_without_status_is_refreshed_not_abandoned(tmp_path):
    legacy = contract({'awin': lambda _: inventory()})
    state = tmp_path / 'state.json'
    batch.run(legacy, tmp_path / 'one', state=state)
    checkpoint = json.loads(state.read_text())
    checkpoint['networks']['awin'].pop('evidence_revision')
    batch.save(state, checkpoint)
    summary = batch.run(legacy, tmp_path / 'two', state=state)
    assert summary['batch']['attempted'] == ['awin']


def test_terms_registered_copy_uses_business_not_hermes_parent(tmp_path, monkeypatch):
    import importlib.util
    import shutil
    source = Path(__file__).resolve().parents[1] / 'ops/hermes-cron-scripts/glp_best_terms_weekly.py'
    target = tmp_path / '.hermes/scripts/glp_best_terms_weekly.py'
    target.parent.mkdir(parents=True)
    shutil.copyfile(source, target)
    spec = importlib.util.spec_from_file_location('copied_terms_wrapper', target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def registered(command, **kwargs):
        assert command[1] == '/home/ubuntu/business/scripts/affiliate_best_terms_machine.py'
        assert '--resume-census' in command
        return SimpleNamespace(stdout='fixture receipt', returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', registered)
    assert module.main() == 0


