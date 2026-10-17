"""Exact provider receipt recovery; synthetic transport, no live effects."""
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sys
import urllib.parse
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import firecrawl_client as fc


import glp_sponsor_receipt_reconcile as reconciliation


@pytest.fixture(autouse=True)
def fixed_receipt_window(monkeypatch):
    # Activity retention is a contract, not a dependence on CI's wall clock.
    from types import SimpleNamespace
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026,10,17,0,30,tzinfo=dt.timezone.utc)
    monkeypatch.setattr(reconciliation, 'dt', SimpleNamespace(
        datetime=Clock, timedelta=dt.timedelta, timezone=dt.timezone))


def test_exact_started_scrape_plan_apply_once_with_real_client(tmp_path, monkeypatch):
    # An interrupted operation has neither a provider ID nor a terminal receipt.
    # The historical booking cutoff must NOT be widened to handle it.
    from glp_sponsor_receipt_reconcile import reconcile, OPERATION, encoded
    row = dict(OPERATION, schema='firecrawl-operation-v1', creditsUsed=None,
               options={'payload_sha256': '0bbca81cc5eb401e28f9a041d2a46554eb1b86a32de2aeba82befdd0c2af136a'},
               scrape_search_id=None)
    ledger = tmp_path/'operations.jsonl'; ledger.write_bytes(encoded(row))
    state = tmp_path/'state'; state.mkdir()
    reservations = state/'firecrawl-supply-budget.json'; reservations.write_text('{"reserved":45}')
    url = 'https://test.example/partners'
    monkeypatch.setitem(OPERATION, 'url_hash', [hashlib.sha256(url.encode()).hexdigest()])
    row['url_hash'] = OPERATION['url_hash']; ledger.write_bytes(encoded(row))
    calls = []
    def transport(request, timeout):
        calls.append((request.method, request.full_url))
        assert request.method == 'GET'
        if '/team/activity?' in request.full_url:
            payload = {'success':True, 'data':[{'id':'provider-job', 'endpoint':'scrape',
                'api_version':'v2','created_at':'2026-10-16 23:54:58.827000000', 'target':url}],
                'has_more':False, 'cursor':None}
        else:
            assert request.full_url == fc.API+'scrape/provider-job'
            payload = {'success':True,'data':{'metadata':{'scrapeId':'provider-job',
                'sourceURL':url,'creditsUsed':1,'statusCode':200}}}
        r = io.BytesIO(json.dumps(payload).encode()); r.status=200; r.headers={}; return r
    monkeypatch.setattr(fc.urllib.request, 'urlopen', transport)
    client = fc.FirecrawlClient('test-reconcile',api_key='synthetic-not-a-live-key')
    before = ledger.read_bytes(); reserved = reservations.read_bytes()
    plan = reconcile(state, ledger, client, tmp_path/'plan.json')
    assert plan['status']=='plan' and plan['creditsUsed']==1
    assert ledger.read_bytes()==before
    applied = reconcile(state,ledger,client,tmp_path/'apply.json',apply=True,
        expected_sha=plan['pre_ledger_sha256'],expected_evidence_sha=plan['evidence_sha256'])
    assert applied['status']=='applied'
    lines = [json.loads(l) for l in ledger.read_bytes().splitlines()]
    assert len(lines)==2 and lines[0]==row
    assert lines[1]['creditsUsed']==1 and lines[1]['outcome']=='started'
    assert lines[1]['operation_id']==row['operation_id']
    assert reservations.read_bytes()==reserved
    again = reconcile(state,ledger,client,tmp_path/'again.json',apply=True,
        expected_sha=applied['post_ledger_sha256'],expected_evidence_sha=plan['evidence_sha256'])
    assert again['status']=='already_reconciled' and ledger.read_bytes()==before+encoded(lines[1])
    assert len(calls)==4  # two GETs per plan/apply; idempotent readback makes none


def setup_effect(tmp_path, monkeypatch, mutate=None):
    r = reconciliation
    url='https://fixture.example/business'
    monkeypatch.setitem(r.OPERATION,'url_hash',[r.sha(url.encode())])
    row=dict(r.OPERATION,schema='firecrawl-operation-v1',creditsUsed=None,
             options={'payload_sha256':r.PAYLOAD_SHA},scrape_search_id=None)
    ledger=tmp_path/'ledger.jsonl';ledger.write_bytes(r.encoded(row))
    state=tmp_path/'state';state.mkdir()
    (state/'firecrawl-supply-budget.json').write_text('{"reserved":45}')
    calls=[]
    def transport(request,timeout):
        assert request.method=='GET';calls.append(request.full_url)
        if '/team/activity?' in request.full_url:
            payload={'success':True,'data':[{'id':'job','target':url,'endpoint':'scrape',
                'api_version':'v2','created_at':'2026-10-16 23:54:58.827000000'}],
                'has_more':False,'cursor':None}
        else:
            assert request.full_url==fc.API+'scrape/job'
            payload={'success':True,'data':{'metadata':{'scrapeId':'job','sourceURL':url,'creditsUsed':1}}}
        if mutate:mutate(payload,request.full_url,ledger)
        response=io.BytesIO(json.dumps(payload).encode());response.status=200;response.headers={};return response
    monkeypatch.setattr(fc.urllib.request,'urlopen',transport)
    return state,ledger,row,fc.FirecrawlClient('fixture',api_key='synthetic'),calls


@pytest.mark.parametrize('mutation', ['missing','duplicate','incomplete','repeat_cursor','missing_cost',
    'bool_cost','negative_cost','wrong_id','wrong_url','failed_result','other_local'])
def test_unknown_is_not_zero_or_a_ledger_write(tmp_path,monkeypatch,mutation):
    def mutate(payload,url,ledger):
        if '/team/activity?' in url:
            if mutation=='missing':payload['data']=[]
            if mutation=='duplicate':payload['data']*=2
            if mutation=='incomplete':payload.pop('has_more')
            if mutation=='repeat_cursor':payload.update(has_more=True,cursor='same')
        else:
            meta=payload['data']['metadata']
            if mutation=='missing_cost':meta.pop('creditsUsed')
            if mutation=='bool_cost':meta['creditsUsed']=False
            if mutation=='negative_cost':meta['creditsUsed']=-1
            if mutation=='wrong_id':meta['scrapeId']='other'
            if mutation=='wrong_url':meta['sourceURL']='https://outside.invalid/'
            if mutation=='failed_result':payload['success']=False
    state,ledger,row,client,calls=setup_effect(tmp_path,monkeypatch,mutate)
    if mutation=='other_local':
        other=dict(row,operation_id='unrelated',caller='other-caller')
        with ledger.open('ab') as f:f.write(reconciliation.encoded(other))
    before=ledger.read_bytes()
    result=reconciliation.reconcile(state,ledger,client,tmp_path/'unknown.json',apply=True,
        expected_sha=reconciliation.sha(before),expected_evidence_sha='cannot-authorize-unknown')
    assert result['status']=='UNKNOWN' and ledger.read_bytes()==before
    assert result['evidence']['reason'] and 'creditsUsed' not in result
    assert (state/'firecrawl-supply-budget.json').read_text()=='{"reserved":45}'
    assert all('/scrape?' not in url for url in calls)


def test_conflicting_zero_receipt_cannot_clear_unknown_cost_guard(tmp_path,monkeypatch):
    import glp_sponsor_firecrawl_supply as supply
    def mutate(payload,url,ledger):
        if '/team/activity?' not in url:
            payload['creditsUsed']=0
            payload['data']['metadata']['creditsUsed']=30
    state,ledger,row,client,calls=setup_effect(tmp_path,monkeypatch,mutate)
    before=ledger.read_bytes()
    monkeypatch.setattr(fc,'LEDGER',ledger)
    producer=supply.Producer(state_dir=state,import_only=True)
    with pytest.raises(RuntimeError,match='sponsor_ledger_credit_cost_unknown'):
        producer.daily_accounted('2026-10-16')
    plan=reconciliation.reconcile(state,ledger,client,tmp_path/'plan.json')
    result=reconciliation.reconcile(state,ledger,client,tmp_path/'apply.json',apply=True,
        expected_sha=plan['pre_ledger_sha256'],expected_evidence_sha=plan['evidence_sha256'])
    try:
        accounted=producer.daily_accounted('2026-10-16')
        guard='cleared'
    except RuntimeError as exc:
        guard=str(exc);accounted=None
    print(json.dumps({'metadata_cost':30,'envelope_cost':0,'status':result['status'],
        'accounted':accounted,'guard_after':guard,'ledger_unchanged':ledger.read_bytes()==before}))
    assert result['status']=='UNKNOWN' and ledger.read_bytes()==before
    assert guard=='sponsor_ledger_credit_cost_unknown'
    with pytest.raises(RuntimeError,match='ambiguous_capture_effect_no_replay'):
        producer.capture_admissible('scrape','https://fixture.example/business')
    assert (state/'firecrawl-supply-budget.json').read_text()=='{"reserved":45}'


@pytest.mark.parametrize('phase',['plan','apply'])
@pytest.mark.parametrize('location',['envelope','data','metadata'])
@pytest.mark.parametrize('key',['creditsUsed','credits_used','creditsCost','credits_cost'])
@pytest.mark.parametrize('value',[0,30,None,'1',False,-1,float('nan'),float('inf')])
def test_any_invalid_or_conflicting_cost_field_preserves_unknown(tmp_path,monkeypatch,phase,location,key,value):
    import glp_sponsor_firecrawl_supply as supply
    def mutate(payload,url,ledger):
        if '/team/activity?' not in url:
            payload['creditsUsed']=1
            obj={'envelope':payload,'data':payload['data'],'metadata':payload['data']['metadata']}[location]
            obj[key]=value
    state,ledger,row,client,calls=setup_effect(tmp_path,monkeypatch,mutate)
    before=ledger.read_bytes()
    monkeypatch.setattr(fc,'LEDGER',ledger)
    result=reconciliation.reconcile(state,ledger,client,tmp_path/'refused.json',apply=phase=='apply',
        expected_sha=reconciliation.sha(before),expected_evidence_sha='cannot-authorize-unknown')
    assert result['status']=='UNKNOWN' and ledger.read_bytes()==before
    assert 'creditsUsed' not in result
    with pytest.raises(RuntimeError,match='sponsor_ledger_credit_cost_unknown'):
        supply.Producer(state_dir=state,import_only=True).daily_accounted('2026-10-16')
    assert (state/'firecrawl-supply-budget.json').read_text()=='{"reserved":45}'
    assert len(calls)<=2


@pytest.mark.parametrize('case',['different_metadata_job','missing_metadata_job','conflicting_envelope_job',
    'missing_metadata_cost','missing_metadata_cost_alexandria_zero','alexandria_conflict'])
def test_ordinary_scrape_requires_own_unambiguous_metadata(tmp_path,monkeypatch,case):
    def mutate(payload,url,ledger):
        if '/team/activity?' in url:return
        meta=payload['data']['metadata']
        if case=='different_metadata_job':
            payload['id']='job';meta['scrapeId']='different'
        elif case=='missing_metadata_job':
            payload['id']='job';meta.pop('scrapeId')
        elif case=='conflicting_envelope_job':payload['id']='different'
        elif case=='alexandria_conflict':payload['data']['alexandria']=[{'creditsCost':30}]
        else:
            meta.pop('creditsUsed');payload['creditsUsed']=1
            if case.endswith('alexandria_zero'):
                payload.pop('creditsUsed');payload['data']['alexandria']=[{'creditsCost':0}]
    state,ledger,row,client,calls=setup_effect(tmp_path,monkeypatch,mutate)
    before=ledger.read_bytes()
    result=reconciliation.reconcile(state,ledger,client,tmp_path/'refused.json',apply=True,
        expected_sha=reconciliation.sha(before),expected_evidence_sha='cannot-authorize-unknown')
    assert result['status']=='UNKNOWN' and ledger.read_bytes()==before


@pytest.mark.parametrize('cost',[0,1,1.5])
def test_agreeing_numeric_cost_fields_can_reconcile(tmp_path,monkeypatch,cost):
    def mutate(payload,url,ledger):
        if '/team/activity?' in url:return
        payload['id']='job'
        for obj in (payload,payload['data'],payload['data']['metadata']):
            obj.update(creditsUsed=cost,credits_used=float(cost),creditsCost=cost,credits_cost=cost)
    state,ledger,row,client,calls=setup_effect(tmp_path,monkeypatch,mutate)
    plan=reconciliation.reconcile(state,ledger,client,tmp_path/'plan.json')
    result=reconciliation.reconcile(state,ledger,client,tmp_path/'apply.json',apply=True,
        expected_sha=plan['pre_ledger_sha256'],expected_evidence_sha=plan['evidence_sha256'])
    assert result['status']=='applied' and result['creditsUsed']==cost


def test_unterminated_ledger_refuses_before_transport(tmp_path,monkeypatch):
    state,ledger,row,client,calls=setup_effect(tmp_path,monkeypatch)
    ledger.write_bytes(ledger.read_bytes().rstrip(b'\n'));before=ledger.read_bytes()
    with pytest.raises(RuntimeError,match='unterminated_ledger'):
        reconciliation.reconcile(state,ledger,client,tmp_path/'plan.json')
    assert calls==[] and ledger.read_bytes()==before


@pytest.mark.parametrize('phase',['between_paid','between_metadata','during_paid','during_metadata'])
def test_real_client_metadata_does_not_exempt_any_concurrent_write(tmp_path,monkeypatch,phase):
    armed=[False]
    def append(ledger):
        row={'operation_id':'external','endpoint':'/v2/team/credit-usage' if phase.endswith('metadata') else '/v2/search'}
        with ledger.open('ab') as f:f.write(reconciliation.encoded(row))
    def mutate(payload,url,ledger):
        if armed[0] and '/team/activity?' in url:
            armed[0]=False;append(ledger)
    state,ledger,row,client,calls=setup_effect(tmp_path,monkeypatch,mutate)
    plan=reconciliation.reconcile(state,ledger,client,tmp_path/'plan.json')
    before=ledger.read_bytes()
    if phase.startswith('between'):append(ledger)
    else:armed[0]=True
    with pytest.raises(RuntimeError,match='ledger_changed'):
        reconciliation.reconcile(state,ledger,client,tmp_path/'apply.json',apply=True,
            expected_sha=plan['pre_ledger_sha256'],expected_evidence_sha=plan['evidence_sha256'])
    rows=[json.loads(l) for l in ledger.read_bytes().splitlines()]
    assert len(rows)==2 and rows[0]==row and rows[1]['operation_id']=='external'
    assert ledger.read_bytes().startswith(before)


@pytest.mark.parametrize('change',['caller','run','at_utc','url_hash','payload_sha256','operation_id'])
def test_literal_scope_refuses_mutated_operation_before_transport(tmp_path,monkeypatch,change):
    state,ledger,row,client,calls=setup_effect(tmp_path,monkeypatch)
    if change=='payload_sha256':row['options'][change]='forged'
    else:row[change]=['forged'] if change=='url_hash' else 'forged'
    ledger.write_bytes(reconciliation.encoded(row));before=ledger.read_bytes()
    with pytest.raises(RuntimeError,match='exact_operation'):
        reconciliation.reconcile(state,ledger,client,tmp_path/'plan.json')
    assert calls==[] and ledger.read_bytes()==before


def test_already_booked_started_effect_still_cannot_be_replayed(tmp_path,monkeypatch):
    import glp_sponsor_firecrawl_supply as supply
    state,ledger,row,client,calls=setup_effect(tmp_path,monkeypatch)
    plan=reconciliation.reconcile(state,ledger,client,tmp_path/'plan.json')
    reconciliation.reconcile(state,ledger,client,tmp_path/'apply.json',apply=True,
        expected_sha=plan['pre_ledger_sha256'],expected_evidence_sha=plan['evidence_sha256'])
    p=supply.Producer(state_dir=state,import_only=True)
    p.operations=lambda:[json.loads(l) for l in ledger.read_bytes().splitlines()]
    with pytest.raises(RuntimeError,match='ambiguous_capture_effect_no_replay'):
        p.capture_admissible('scrape','https://fixture.example/business')


@pytest.mark.parametrize('alias',['receipt','audit','ledger'])
def test_namespace_aliases_refuse_before_transport(tmp_path,monkeypatch,alias):
    state,ledger,row,client,calls=setup_effect(tmp_path,monkeypatch)
    receipt=tmp_path/'plan.json';before=ledger.read_bytes()
    if alias=='receipt':receipt.symlink_to(ledger)
    elif alias=='audit':Path(str(receipt)+'.GET-audit.jsonl').symlink_to(ledger)
    else:
        link=tmp_path/'ledger-link';link.symlink_to(ledger);ledger=link
    with pytest.raises((RuntimeError,OSError),match='.'):
        reconciliation.reconcile(state,ledger,client,receipt)
    assert calls==[] and ledger.read_bytes()==before
