"""Katalys contract-change notices cannot be silently archived as routine."""
import asyncio
import importlib.util
from pathlib import Path
import pytest

from tests.inbox_notice_loader import load
a = load()

@pytest.mark.parametrize('name', ['CoreAge Rx GLP-1', 'GLP-1 Weight Loss - MEDGm Program'])
def test_katalys_exact_contract_terms_notice_requires_review(name):
    c = a.detect(f'Your Contract Terms for {name} Have Changed 🔧', 'notifications@katalys.com', 'Your contract terms have changed. View your contract.')
    assert c['action'] == 'contract_terms_review'
    assert c['program_name'] == name
    assert c['network'] == 'Katalys'

@pytest.mark.parametrize('subject,sender', [
    ('Introducing new contract terms', 'notifications@katalys.com'),
    ('Your Contract Terms for CoreAge Rx GLP-1 Have Changed 🔧', 'spam@example.org'),
])
def test_untrusted_or_promotional_notice_does_not_use_contract_review(subject, sender):
    assert (a.detect(subject, sender, 'Program updates: apply to join') or {}).get('action') != 'contract_terms_review'

def test_terms_review_has_no_sheet_or_mailbox_effect(tmp_path, monkeypatch):
    monkeypatch.setattr(a, 'REPORT_DIR', tmp_path / 'reports')
    monkeypatch.setattr(a, 'STATE_DB', tmp_path / 'state.sqlite3')
    monkeypatch.setattr(a, 'load_control_plane', lambda: pytest.fail('terms notice must not reach mailbox writer'))
    event = {'event_id':'evt_terms_fixture', 'state':'ingested', 'raw_path':'unused', 'canonical_category':'affiliate_network_response'}
    monkeypatch.setattr(a, 'message_text', lambda _: ('Your Contract Terms for CoreAge Rx GLP-1 Have Changed 🔧', 'notifications@katalys.com', 'Contract terms have changed'))
    monkeypatch.setattr(a, 'verify_affiliate_authentication', lambda *args: {'status':'PASS'})
    monkeypatch.setattr(a, 'load_snapshot', lambda: {})
    monkeypatch.setattr(a, 'require_operator_ruling_clear', lambda *args: None)
    with pytest.raises(a.AffiliateReviewRequired, match='contract_terms_changed_requires_verified_capture'):
        asyncio.run(a.process_event(event, None, None, None, False, {}))

def test_terminal_terms_recovery_requires_exact_zero_effect_receipt():
    assert a.terms_notice_recovery_allowed({'status':'PASS','action_kind':'routine_affiliate_information','sheet_write_count':0,'provider_mutation':False,'provider_readback_verified':False,'sheet_readback_verified':False})
    assert not a.terms_notice_recovery_allowed({'status':'PASS','action_kind':'routine_affiliate_information','sheet_write_count':1})
    assert not a.terms_notice_recovery_allowed({'status':'PASS','action_kind':'affiliate_relationship_reconciliation','sheet_write_count':0})


def test_terms_mode_rejects_unknown_effects_before_consumer(tmp_path, monkeypatch):
    import json
    import sys
    monkeypatch.setattr(a, 'STATE_DB', tmp_path / 'state.sqlite3')
    prior = tmp_path / 'prior.json'
    prior.write_text(json.dumps({'status':'PASS','action_kind':'routine_affiliate_information','sheet_write_count':1}))
    state = a.init_state()
    a.mark_state(state, 'evt_terms_fixture', 'pass', receipt=prior)
    state.close()
    event = {'event_id':'evt_terms_fixture','state':'resolved_externally','raw_path':'unused'}
    monkeypatch.setattr(a, 'fetch_inbox_events', lambda *args, **kwargs: [event])
    monkeypatch.setattr(a, 'message_text', lambda _: ('Your Contract Terms for CoreAge Rx GLP-1 Have Changed 🔧','notifications@katalys.com','Contract terms changed'))
    monkeypatch.setattr(sys, 'argv', ['reconciler','--contract-terms-verification-only','--event-id','evt_terms_fixture'])
    monkeypatch.setattr(a, 'google_service', lambda: pytest.fail('Sheet client forbidden'))
    with pytest.raises(RuntimeError, match='contract_terms_prior_effects_not_zero'):
        asyncio.run(a.main())


def test_unowned_action_routes_minimized_triage_once_per_event(tmp_path, monkeypatch):
    """Exact source slice; intake/API edge mocked, host roundtrip is separate."""
    import ast, hashlib, json, sys, types, uuid
    source = Path(__file__).resolve().parents[1] / 'scripts/affiliate_approval_auto_reconciler.py'
    tree = ast.parse(source.read_text())
    selected = [n for n in tree.body if getattr(n, 'name', '') == 'route_unowned_inbox_action']
    assert selected, 'reconciler lacks canonical unowned-action to Triage producer'
    # Reduced mirror omits host-only intake client; these two edges are mocks,
    # not fake business evidence. Host invocation is verified after install.
    linear_triage = types.ModuleType('linear_triage')
    def validate(item):
        assert uuid.UUID(item['operation_id']).version==4
        assert item['source_id'].startswith('inbox:')
        assert item['out_of_scope'] is item['privacy_reviewed'] is True
    linear_triage.validate_item = validate
    linear_triage.submit_idea = None
    monkeypatch.setitem(sys.modules,'linear_triage',linear_triage)
    linear_client = types.ModuleType('linear_client')
    linear_client.Client = lambda: pytest.fail('unmocked remote client')
    monkeypatch.setitem(sys.modules,'linear_client',linear_client)
    ns = dict(Path=Path, hashlib=hashlib, json=json, BUSINESS=tmp_path,
              TERMINAL_EVENT_STATES={'resolved_externally','held','archived'},
              message_text=lambda _: ('PRIVATE SUBJECT', 'secret@example.invalid', 'PRIVATE BODY'),
              detect=lambda *_: None)
    exec(compile(ast.Module(selected, type_ignores=[]), str(source), 'exec'), ns)
    route = ns['route_unowned_inbox_action']
    raw = tmp_path / 'raw.eml'; raw.write_text('PRIVATE BODY')
    event = dict(event_id='evt_regression', state='ingested', raw_path=str(raw),
                 raw_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
                 canonical_actionable=1, canonical_category='unknown',
                 canonical_venture='glp', canonical_lifecycle='active')
    binding = dict(event_id=event['event_id'], raw_sha256=event['raw_sha256'],
                   owning_machine=None, machine_ownership_reviewed=True,
                   privacy_reviewed=True, title='Review infrastructure retention notice',
                   summary='Review the retained notice; no paid upgrade is authorized.',
                   lane='lane:kael', **{'class':'class:ops'})
    calls = []
    def publish(client, item, journal, **kwargs):
        linear_triage.validate_item(item)
        assert kwargs == {'entry_class':'inbox-email','purpose':'blocker','dry_run':False}
        calls.append(item)
        # Replay identity stays stable, as required by real submit_idea journal.
        if len(calls)>1: assert calls[-1] == calls[0]
        return {'status':'LINKED','issue':{'identifier':'GLP-FIXTURE'}}
    monkeypatch.setattr(linear_triage, 'submit_idea', publish)
    class Client:
        def team(self, _): return {'id':'team'}
        def connection(self, *_): return [{}]*self.count
        count=19
    client = Client()
    for _ in range(2):
        assert route(event,binding,client=client)['status']=='LINKED'
    assert calls[0]['source_id']=='inbox:evt_regression'
    assert uuid.UUID(calls[0]['operation_id']).version==4
    assert 'PRIVATE' not in json.dumps(calls)
    for changed in ({'canonical_actionable':0},{'state':'sent_verified'},
                    {'canonical_category':'affiliate_network_response'}):
        assert route(dict(event,**changed),binding,client=client) is None
    assert route(event,dict(binding,owning_machine='sponsor'),client=client) is None
    assert route(event,None,client=client) is None  # unknown ownership is not idle
    with pytest.raises(RuntimeError,match='binding'):
        route(event,dict(binding,raw_sha256='0'*64),client=client)
    client.count=20
    with pytest.raises(RuntimeError,match='cap'):
        route(event,binding,client=client)
    assert len(calls)==2
