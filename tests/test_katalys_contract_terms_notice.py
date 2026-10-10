"""Katalys contract-change notices cannot be silently archived as routine."""
import asyncio
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('terms_notice_reconciler', Path(__file__).resolve().parents[1] / 'scripts/affiliate_approval_auto_reconciler.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

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
