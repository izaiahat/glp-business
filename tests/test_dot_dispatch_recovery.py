"""Audited pre-dispatch selection recovery; fixtures are not live effects."""
import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import dot_dispatch as d

def fixture(tmp_path):
    tickets=tmp_path/'reports/ops/dot-tickets';tickets.mkdir(parents=True,mode=0o700)
    ledger=tickets.parent/'dot-refill/CLAIMS.json';ledger.parent.mkdir(mode=0o700)
    row={'dot':'finch','issue':'GLP-328','status':'selecting','selected_at':1,'files':[]}
    ledger.write_text(json.dumps({'scope-v2':row}))
    return tickets,ledger,row

def test_selection_without_ticket_is_audited_not_deleted(tmp_path):
    tickets,ledger,row=fixture(tmp_path)
    out=d.recover_selection('scope-v2',root=tickets,reason='R2b exact pre-flock failure')
    assert out['status']=='aborted_pre_dispatch' and out['redispatched'] is False
    after=json.loads(ledger.read_text())['scope-v2']
    assert after['selected_at']==1 and after['audit_sha256']
    assert not list(tickets.glob('*/TICKET.json'))
    with pytest.raises(ValueError,match='unbound'):
        d.recover_selection('scope-v2',root=tickets,reason='again')

@pytest.mark.parametrize('journal',[{'dot':'finch','issue':'GLP-328','sent_at':2}, {'content':'scope-v2'}, {'content':'scope-v2','state':'complete'}])
def test_journal_never_replayed(tmp_path,journal):
    tickets,ledger,row=fixture(tmp_path)
    (tickets/'existing').mkdir(mode=0o700)
    (tickets/'existing/TICKET.json').write_text(json.dumps(journal))
    before=ledger.read_bytes()
    with pytest.raises(ValueError,match='dispatch_journal_present'):
        d.recover_selection('scope-v2',root=tickets,reason='refused')
    assert ledger.read_bytes()==before

@pytest.mark.parametrize('change',[{'ticket':'known'}, {'request_id':'123'}, {'status':'dispatched'}, {'dot':'unknown'}])
def test_bound_or_invalid_claim_refused(tmp_path,change):
    tickets,ledger,row=fixture(tmp_path)
    row.update(change);ledger.write_text(json.dumps({'scope-v2':row}))
    before=ledger.read_bytes()
    with pytest.raises(ValueError):
        d.recover_selection('scope-v2',root=tickets,reason='refused')
    assert ledger.read_bytes()==before


@pytest.mark.parametrize('mode',['valid','foreign_author','webhook','attachment','no_disclaimer','wrong_id','native_absent','native_live','probe_disappears','stale_audit'])
def test_external_handoff_expiry_is_not_native_completion(tmp_path,monkeypatch,mode):
    root=tmp_path/'tickets'
    message={'id':'100','channel_id':d.DOTS['rook']['channel'],'content':'','author':{'id':d.SENDER},'mentions':[{'id':d.DOTS['rook']['user']}],'attachments':[]}
    fallback={}
    def api(method,route,payload=None):
        if method=='POST':message['content']=payload['content']
        if '?limit=100' in route:return [message,fallback]
        return fallback if route.endswith('/101') else message
    sent=d.dispatch('rook','GLP-354','fixture',root=root,api=api,ready=lambda _:{'expires':9999999999})
    ticket=Path(sent['ticket_dir']);row=json.loads((ticket/'TICKET.json').read_text())
    fallback.update(id='101',channel_id=message['channel_id'],author={'id':d.DOTS['rook']['user']},content='<@'+d.SENDER+'> DIRECT FALLBACK RESULT '+sent['ticket']+'. not a native-correlated receipt',attachments=[])
    monkeypatch.setattr(d.time,'time',lambda:row['sent_at']+21601)
    if mode=='foreign_author':fallback['author']={'id':'untrusted'}
    if mode=='webhook':fallback['webhook_id']='hook'
    if mode=='attachment':fallback['attachments']=[{'id':'unknown'}]
    if mode=='no_disclaimer':fallback['content']='RESULT '+sent['ticket']
    if mode=='wrong_id':fallback['id']='102'
    native={'status':'absent' if mode=='native_absent' else 'confirmed' if mode=='native_live' else 'request_expired'}
    sequence=iter([{'status':'request_expired'},{'status':'absent'}])
    probe=(lambda _:next(sequence)) if mode=='probe_disappears' else (lambda _:native)
    if mode=='stale_audit':
        d.atomic_json(ticket/'EXPIRY.json',dict(schema='DOT_NO_REPLY_EXPIRY_V1',ticket_preimage_sha256=d.digest(d.canonical(row)),source_pins=d.source_pins(),reason='fixture',recorded_by='R2b',timeout_seconds=21600))
    if mode!='valid':
        with pytest.raises(ValueError):
            d.expire(ticket,reason='fixture',owner='R2b',api=api,probe=probe,handoff_reply_id='101')
        assert json.loads((ticket/'TICKET.json').read_text())['state']=='sent'
        return
    with pytest.raises(ValueError,match='reply_present'):
        d.expire(ticket,reason='fixture',owner='R2b',api=api,probe=lambda _:{'status':'request_expired'})
    expired=d.expire(ticket,reason='fixture',owner='R2b',api=api,probe=lambda _:{'status':'request_expired'},handoff_reply_id='101')
    assert expired['state']=='expired' and not (ticket/'REPLY.md').exists()
    audit=json.loads((ticket/'EXPIRY.json').read_text())
    assert audit['schema']=='DOT_EXTERNAL_HANDOFF_EXPIRY_V1' and audit['matching_reply_count']==1
    assert audit['external_handoff']['id']=='101' and audit['operator_approval'] is False

