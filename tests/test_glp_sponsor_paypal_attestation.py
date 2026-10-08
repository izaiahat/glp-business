"""Synthetic operator-attestation binding fixtures; never payment evidence."""
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import glp_sponsor_paypal as p
AT=dt.datetime(2026,10,8,tzinfo=dt.timezone.utc)

@pytest.fixture
def receiver(tmp_path,monkeypatch):
    authority=tmp_path/'operator.md'
    authority.write_text('SYNTHETIC direct operator attestation - no live authority')
    monkeypatch.setattr(p,'OPERATOR_ATTESTATION',authority,raising=False)
    monkeypatch.setattr(p,'OPERATOR_ATTESTATION_SHA256',hashlib.sha256(authority.read_bytes()).hexdigest(),raising=False)
    private=tmp_path/'private';private.mkdir(mode=0o700)
    monkeypatch.setattr(p,'BINDING',private/'account-binding.json')
    import glp_sponsor_campaign as campaign
    monkeypatch.setattr(campaign,'STATE',tmp_path/'state')
    client=object.__new__(p.PayPalClient);client.app={'client_id':'SYNTHETIC-APP'}
    calls=[]
    def request(method,path,payload=None):
        assert method=='GET' and path.startswith('/v1/reporting/transactions?')
        calls.append(path)
        return {'account_number':'SYNTHETIC-MERCHANT'}
    monkeypatch.setattr(client,'request',request)
    return client,authority,calls

def test_genuine_operator_source_can_receive_without_invented_provider_identity(receiver):
    client,authority,calls=receiver
    assert hasattr(p,'receive_operator_attestation'),'maintained receiver cannot express genuine operator authority'
    result=p.receive_operator_attestation(client,AT)
    assert result['ownership_source']=='operator_attested_paypal_account_and_app'
    binding=json.loads(p.BINDING.read_text())
    assert binding['merchant_id']=='SYNTHETIC-MERCHANT'
    assert binding['operator_attestation_sha256']==hashlib.sha256(authority.read_bytes()).hexdigest()
    assert not binding.get('business_email') and not binding.get('account_type')
    assert not binding.get('approval_message_id')
    assert binding['client_id_sha256']==hashlib.sha256(client.app['client_id'].encode()).hexdigest()
    assert p.BINDING.stat().st_mode&0o777==0o600
    assert client.merchant(AT)=='SYNTHETIC-MERCHANT'
    before=p.BINDING.read_bytes()
    p.receive_operator_attestation(client,AT)
    assert p.BINDING.read_bytes()==before
    assert calls and result['financial_write_attempts']==0

@pytest.mark.parametrize('failure',['authority','app','merchant','permissions','fake_business','wrong_source'])
def test_attested_binding_refuses_unbound_or_relabelled_identity(receiver,monkeypatch,failure):
    client,authority,calls=receiver
    assert hasattr(p,'receive_operator_attestation')
    p.receive_operator_attestation(client,AT)
    binding=json.loads(p.BINDING.read_text())
    if failure=='authority':authority.write_text('tampered operator record')
    elif failure=='app':client.app={'client_id':'OTHER-APP'}
    elif failure=='merchant':monkeypatch.setattr(client,'request',lambda *a,**k:{'account_number':'OTHER'})
    elif failure=='permissions':p.BINDING.chmod(0o644)
    elif failure=='fake_business':binding['account_type']='BUSINESS';p.BINDING.write_text(json.dumps(binding))
    else:binding['source']='authenticated_paypal_account_and_app';p.BINDING.write_text(json.dumps(binding))
    with pytest.raises((RuntimeError,OSError,ValueError)):client.merchant(AT)

def test_missing_current_account_readback_cannot_create_binding(receiver,monkeypatch):
    client,authority,calls=receiver
    assert hasattr(p,'receive_operator_attestation')
    monkeypatch.setattr(client,'request',lambda *a,**k:{})
    with pytest.raises((RuntimeError,ValueError)):p.receive_operator_attestation(client,AT)
    assert not p.BINDING.exists()


@pytest.mark.parametrize('case',['authority_before','authority_during_get','held_lock','existing_unreviewed_binding'])
def test_attestation_receiver_never_overwrites_or_crosses_unknown_boundary(receiver,monkeypatch,case):
    import fcntl
    import glp_sponsor_campaign as campaign
    client,authority,calls=receiver
    held=None
    if case=='authority_before':authority.write_text('tampered')
    elif case=='authority_during_get':
        original=client.request
        def request(*args,**kwargs):
            result=original(*args,**kwargs);authority.write_text('tampered after GET');return result
        monkeypatch.setattr(client,'request',request)
    elif case=='existing_unreviewed_binding':p.BINDING.write_text('{}');p.BINDING.chmod(0o600)
    else:
        campaign.STATE.mkdir();held=(campaign.STATE/'run.lock').open('a');fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
    before=p.BINDING.read_bytes() if p.BINDING.exists() else None
    try:
        with pytest.raises((RuntimeError,OSError,ValueError)):p.receive_operator_attestation(client,AT)
    finally:
        if held:held.close()
    assert (p.BINDING.read_bytes() if p.BINDING.exists() else None)==before
    if case in {'authority_before','held_lock','existing_unreviewed_binding'}:assert calls==[]
