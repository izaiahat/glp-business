"""Synthetic processor GET fixtures. No account, money or public-site effects."""
import copy
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
import pytest
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'scripts')]
import glp_sponsor_paypal as paypal
from test_glp_sponsor_paypal import order_fixture, invoice_fixture, report_fixture, fake_client, AT, TX


def fixture(tmp_path):
    order = order_fixture(tmp_path)
    order.update(provider='Synthetic Fixture Provider', slug='synthetic-fixture-provider',allocated_transaction_ids=[TX])
    # Updating invoice-bound identity requires new SYNTHETIC invoice authority.
    from test_glp_sponsor_paypal import approve_fixture
    approve_fixture(tmp_path, order)
    site = {'order_reference':order['order_id'], 'provider':order['provider'], 'slug':order['slug'],
            'held':False, 'us_provider':True, 'release_approved':True,
            'operator_approval':True, 'partner_creative_approval':True,
            'eligibility_approved':True, 'inventory_reserved':True,
            'inventory_slot':'providers-directory-panel', 'affiliate':False,
            'destination':'https://example.invalid/program/',
            'starts_at':paypal.iso(AT-dt.timedelta(hours=1)),
            'ends_at':paypal.iso(AT+dt.timedelta(hours=719)),
            'facts':{key:{'text':'Synthetic reviewed '+key,'source_url':'https://example.invalid/reported/',
                           'checked_at':'2026-10-06'}
                     for key in ('program_summary','published_price','eligibility_states','current_offer')}}
    order['site_order'] = site
    (tmp_path/'orders.json').write_text(json.dumps({order['order_id']:order}))
    evidence=tmp_path/'synthetic-release.txt'
    evidence.write_text('SYNTHETIC APPROVAL - NOT LIVE AUTHORITY')
    digest=hashlib.sha256(json.dumps(site,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    (tmp_path/'site-approvals.json').write_text(json.dumps({order['order_id']:{
        'actor':'Izaiah Tilton','discord_message_id':'SYNTHETIC-NOT-AUTHORITY',
        'decision':'APPROVE SPONSOR SITE ORDER '+digest,
        'evidence_path':str(evidence),'evidence_sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}}))
    return order,site


def observe(tmp_path, monkeypatch, order, site, invoice=None, report=None):
    import glp_sponsor_site_payment as bridge
    monkeypatch.setattr(bridge,'eligible_hosts',lambda state,at:{'example.invalid'})
    client=fake_client(monkeypatch,invoice or invoice_fixture(order),report or report_fixture(order))
    monkeypatch.setattr(paypal,'PayPalClient',lambda:client)
    return bridge.verify(site,tmp_path,AT)


def test_real_observer_path_not_copied_paid_flag(tmp_path,monkeypatch):
    order,site=fixture(tmp_path)
    result=observe(tmp_path,monkeypatch,order,site)
    assert result['payment_verified'] is True
    assert result['source']=='paypal_authenticated_invoice_reporting_observer'
    assert result['order_reference']==order['order_id']
    assert result['transaction_ids']==[TX]
    # Observer writes no financial records or inventory, even on a positive result.
    assert 'payment_readiness' not in json.loads((tmp_path/'orders.json').read_text())[order['order_id']]


@pytest.mark.parametrize('failure',['unpaid','refund','external','payer','merchant','stale','allocation','unallocated','invoice_replay','creative','approval','eligibility','held','fella','boolean','expired','identity'])
def test_negative_observer_or_approval_never_admits(tmp_path,monkeypatch,failure):
    order,site=fixture(tmp_path)
    inv=invoice_fixture(order);report=report_fixture(order)
    if failure=='unpaid':inv['status']='SENT'
    if failure=='refund':inv['refunds']={'transactions':[{'type':'PAYPAL'}]}
    if failure=='external':inv['payments']['transactions'][0]['type']='EXTERNAL'
    if failure=='payer':report['transaction_details'][0]['payer_info']['email_address']='other@example.invalid'
    if failure=='merchant':report['account_number']='OTHER'
    if failure=='stale':report['last_refreshed_datetime']='2026-10-01T00:00:00Z'
    if failure in ('allocation','invoice_replay'):
        rows=json.loads((tmp_path/'orders.json').read_text())
        rows['other']={'order_id':'other','allocated_transaction_ids':[TX] if failure=='allocation' else [],'invoice_id':order['invoice_id'] if failure=='invoice_replay' else 'INV2-OTHER'}
        (tmp_path/'orders.json').write_text(json.dumps(rows))
    if failure=='unallocated':
        rows=json.loads((tmp_path/'orders.json').read_text())
        rows[order['order_id']].pop('allocated_transaction_ids')
        (tmp_path/'orders.json').write_text(json.dumps(rows))
    if failure=='creative':site['destination']='https://example.invalid/tampered/'
    if failure=='approval':(tmp_path/'site-approvals.json').unlink()
    if failure=='eligibility':
        import glp_sponsor_site_payment as bridge
        monkeypatch.setattr(bridge,'eligible_hosts',lambda state,at:set())
        client=fake_client(monkeypatch,inv,report)
        monkeypatch.setattr(paypal,'PayPalClient',lambda:client)
        assert bridge.verify(site,tmp_path,AT)['payment_verified'] is False
        return
    if failure=='held':site['held']=True
    if failure=='fella':site['provider']='Fella Health'
    if failure=='boolean':site['release_approved']='true'
    if failure=='expired':site['ends_at']=paypal.iso(AT-dt.timedelta(seconds=1))
    if failure=='identity':site['provider']='Other Provider'
    if failure=='unpaid':
        # Even a canonically approved copied flag cannot replace a fresh GET.
        site['payment_verification']={'payment_verified':True,'authenticated':True}
        rows=json.loads((tmp_path/'orders.json').read_text())
        rows[order['order_id']]['site_order']=site
        (tmp_path/'orders.json').write_text(json.dumps(rows))
        approvals=json.loads((tmp_path/'site-approvals.json').read_text())
        digest=hashlib.sha256(json.dumps(site,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        approvals[order['order_id']]['decision']='APPROVE SPONSOR SITE ORDER '+digest
        (tmp_path/'site-approvals.json').write_text(json.dumps(approvals))
    assert observe(tmp_path,monkeypatch,order,site,inv,report)['payment_verified'] is False


def test_cli_denies_unknown_caller_paid_json_without_network(tmp_path):
    import subprocess
    path=Path(__file__).resolve().parents[1]/'scripts/glp_sponsor_site_payment.py'
    result=subprocess.run([sys.executable,'-I',str(path)],input=json.dumps({'order_reference':'UNKNOWN','payment_verified':True}),text=True,capture_output=True)
    assert result.returncode==0
    assert json.loads(result.stdout)['payment_verified'] is False
    assert result.stderr==''


def test_host_observer_to_existing_native_site_assets(tmp_path,monkeypatch):
    import os
    import subprocess
    # CI does not contain the separate site repo/private runtime. The host runs
    # this exact fixture with its existing sponsor checkout; no checkout/build.
    site_root=Path(os.environ.get('GLP_SPONSOR_SITE_TEST_ROOT','/home/ubuntu/business/.worktrees/sponsor-site-r4-20261006'))
    script=site_root/'tests/fixtures/glp328-observer-render.mjs'
    if not script.is_file():pytest.skip('separate site fixture not available in portable business checkout')
    order,site=fixture(tmp_path)
    observed=observe(tmp_path,monkeypatch,order,site)
    assert observed['payment_verified'] is True
    run=subprocess.run(['node','--experimental-test-module-mocks',str(script)],
        input=json.dumps({'site':site,'observed':observed,'at':paypal.iso(AT)}),
        capture_output=True,text=True,timeout=30,cwd=site_root)
    assert run.returncode==0,run.stderr
    proof=json.loads(run.stdout)
    assert proof['observer_to_inventory_to_native_assets']=='PASS'
    assert proof['financial_writes']==proof['public_writes']==proof['deploys']==0


@pytest.mark.parametrize('mode',['fresh','stale','optout_then_agreement','new_human_reply'])
def test_native_reply_snapshot_requires_freshness_and_sticky_optout(tmp_path,monkeypatch,mode):
    import sqlite3
    import glp_sponsor_campaign as campaign
    import glp_sponsor_site_payment as bridge
    dbpath=tmp_path/'inbox.sqlite'
    with sqlite3.connect(dbpath) as db:
        db.execute("CREATE TABLE mailbox_actions(action,status,provider_receipt_json,created_at DEFAULT '',action_id INTEGER PRIMARY KEY)")
        if mode=='optout_then_agreement':
            for kind in ('opt_out','agreement_candidate'):
                db.execute('INSERT INTO mailbox_actions(action,status,provider_receipt_json) VALUES(?,?,?)',('sponsor_reply_stop','verified',json.dumps({'kind':kind,'provider_hosts':['example.invalid']})))
    before=dbpath.read_bytes()
    monkeypatch.setattr(campaign,'INBOX_DB',dbpath)
    monkeypatch.setattr(campaign,'read_local',lambda:{'suppression':{'complete':True}})
    monkeypatch.setattr(campaign,'read_values',lambda:[])
    monkeypatch.setattr(campaign,'invoice_provider_hosts',lambda *a:{'example.invalid'})
    def consume(ledger,db,at):
        assert db.execute('PRAGMA query_only').fetchone()[0]==1
        if mode=='new_human_reply':ledger['hello@example.invalid']={'host':'example.invalid','stopped':'human_reply_or_review'}
        if mode=='optout_then_agreement':ledger['hello@example.invalid']={'host':'example.invalid','stopped':'agreement_candidate'}
        return {'status':'unknown' if mode=='stale' else 'read_complete'}
    monkeypatch.setattr(campaign,'consume_reply_stops',consume)
    if mode=='stale':
        with pytest.raises(ValueError):bridge.eligible_hosts(tmp_path,AT)
    else:assert bridge.eligible_hosts(tmp_path,AT)==({'example.invalid'} if mode=='fresh' else set())
    assert dbpath.read_bytes()==before
    assert not (tmp_path/'ledger.json').exists()


@pytest.fixture
def native_observer(tmp_path,monkeypatch):
    """Real canonical Ledger/poller closure; synthetic-only external transports."""
    import sqlite3
    import types
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from inbox_control_plane import control_plane as cp
    import glp_sponsor_campaign as campaign
    import glp_sponsor_site_payment as bridge
    from test_glp_sponsor_campaign import inputs
    for name in ('PRIVATE_ROOT','RAW_ROOT','ATTACHMENT_ROOT','BACKUP_ROOT','REPORT_ROOT','DAILY_AUDIT_DIR'):
        monkeypatch.setattr(cp,name,tmp_path/name)
    clock=[AT]
    class Frozen(dt.datetime):
        @classmethod
        def now(cls,tz=None):return clock[0]
    monkeypatch.setattr(bridge,'dt',types.SimpleNamespace(datetime=Frozen,timezone=dt.timezone,timedelta=dt.timedelta))
    monkeypatch.setattr(cp,'utc_now',lambda:AT.isoformat())
    class Response:
        def __init__(self,value):self.value=value
        def execute(self):return self.value
    class EmptyGmail:
        def __init__(self,box):self.box=box
        def users(self):return self
        def messages(self):return self
        def getProfile(self,**kw):return Response({'emailAddress':self.box['canonical_address']})
        def list(self,**kw):return Response({'messages':[]})
    monkeypatch.setattr(cp,'gmail_service_for',EmptyGmail)
    monkeypatch.setattr(cp,'sync_operator_decisions',lambda *a,**kw:{'failed':0})
    monkeypatch.setattr(cp,'project_kael_daily_audits',lambda *a,**kw:{'status':'pass','projected':0,'existing':0,'invalid':[]})
    monkeypatch.setattr(cp,'dispatch_outbox',lambda *a,**kw:{'failed':0})
    from inbox_control_plane import affiliate_decision_queue
    monkeypatch.setattr(affiliate_decision_queue,'dispatch',lambda *a,**kw:{'status':'pass'})
    registry={'mailboxes':[{'mailbox_id':box,'venture':'SYNTHETIC','company':'SYNTHETIC',
        'canonical_address':box+'@example.invalid','provider':'gmail','adapter':'gmail_api_readonly',
        'auth_reference':'SYNTHETIC-NO-CREDENTIAL','owner':'SYNTHETIC','lifecycle':'active',
        'poll_seconds':300,'freshness_sla_seconds':900,'inbound_surface':'email','routing_policy':'SYNTHETIC'}
        for box in ('glp_ops_gmail','glp_izaiah_gmail')]}
    database=tmp_path/'inbox.sqlite'
    ledger=cp.Ledger(database)
    try:
        ledger.sync_registry(registry)
        assert cp.run_poll(ledger,registry,bootstrap=True,dispatch=False,limit=20)['status']=='pass'
    finally:ledger.close()
    monkeypatch.setattr(campaign,'INBOX_DB',database)
    monkeypatch.setattr(campaign,'ROOT',tmp_path/'not-live')
    order,site=fixture(tmp_path)
    (tmp_path/'ledger.json').write_text(json.dumps({'partner@example.invalid':{'host':'example.invalid'}}))
    sheet,_,local=inputs()
    row=sheet[1];row[2]='Synthetic Fixture Provider';row[6]='https://example.invalid'
    sheet=[sheet[0]]+[['']*27 for _ in range(20)]+[row]
    monkeypatch.setattr(campaign,'read_values',lambda:copy.deepcopy(sheet))
    monkeypatch.setattr(campaign,'read_local',lambda:copy.deepcopy(local))
    assert bridge.eligible_hosts(tmp_path,AT)=={'example.invalid'}
    client=fake_client(monkeypatch,invoice_fixture(order),report_fixture(order))
    monkeypatch.setattr(paypal,'PayPalClient',lambda:client)
    def stop(kind,event='synthetic-stop'):
        receipt={'kind':kind,'event_id':event,'provider_hosts':['example.invalid'],
                 'enrolled_addresses':['partner@example.invalid']}
        with sqlite3.connect(database) as db:
            db.execute('INSERT INTO mailbox_actions(action_id,event_id,action,status,provider_receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                (event,event,'sponsor_reply_stop','verified',json.dumps(receipt),paypal.iso(AT),paypal.iso(AT)))
        # sqlite3's context manager commits but does not close. Finish fixture
        # WAL checkpointing now, not during the observer's read-only assertion.
        db.close()
    return types.SimpleNamespace(state=tmp_path,site=site,db=database,sheet=sheet,local=local,
                                 client=client,stop=stop,clock=clock,bridge=bridge)


@pytest.mark.parametrize('case',['opt_out','hold','suppression','stale_cursor','elapsed_freshness'])
def test_observer_rereads_current_admission_after_get(native_observer,monkeypatch,case):
    import sqlite3
    e=native_observer
    original=e.client.request
    mutated=[]
    def request(*args,**kwargs):
        result=original(*args,**kwargs)
        if not mutated:
            mutated.append(True)
            if case=='opt_out':e.stop('opt_out')
            elif case=='hold':e.sheet[-1][10]='hold'
            elif case=='suppression':e.local['suppression']['entries']=[{'domain':'example.invalid'}]
            elif case=='elapsed_freshness':e.clock[0]=AT+dt.timedelta(seconds=901)
            else:
                with sqlite3.connect(e.db) as db:db.execute("UPDATE provider_cursors SET last_complete_poll_at='2026-10-01T00:00:00Z'")
        return result
    monkeypatch.setattr(e.client,'request',request)
    assert e.bridge.verify(e.site,e.state,AT)['payment_verified'] is False
    assert mutated==[True]


@pytest.mark.parametrize('kind',['not_interested','bounce','question'])
def test_observer_missing_enrollment_keeps_provider_stop(native_observer,kind):
    e=native_observer
    e.stop(kind)
    (e.state/'ledger.json').write_text('{}')
    before={p.name:p.read_bytes() for p in e.state.glob('*.json')}
    assert e.bridge.verify(e.site,e.state,AT)['payment_verified'] is False
    assert {p.name:p.read_bytes() for p in e.state.glob('*.json')}==before


@pytest.mark.parametrize('history',[[],['agreement_candidate'],['question','agreement_candidate'],
                                    ['opt_out','agreement_candidate'],['bounce','agreement_candidate'],
                                    ['not_interested','agreement_candidate']])
def test_observer_native_reply_fold_preserves_agreement_and_sticky_history(native_observer,history):
    e=native_observer
    for index,kind in enumerate(history):e.stop(kind,str(index))
    (e.state/'ledger.json').write_text('{}')
    before={p.name:p.read_bytes() for p in e.state.glob('*.json')}
    expected=not any(kind in {'opt_out','bounce','not_interested'} for kind in history)
    assert e.bridge.verify(e.site,e.state,AT)['payment_verified'] is expected
    assert {p.name:p.read_bytes() for p in e.state.glob('*.json')}==before


@pytest.mark.parametrize('history',[
    ['interested'],['interested','agreement_candidate'],
    ['agreement_candidate','interested'],['opt_out','interested','agreement_candidate'],
    ['not_interested','interested','agreement_candidate'],
    ['bounce','interested','agreement_candidate']])
def test_observer_real_classifier_interest_is_nonsticky_stop(native_observer,history):
    from inbox_control_plane import sponsor_replies
    e=native_observer
    bodies={'interested':'We are interested, send us the details.',
            'agreement_candidate':'We agree, proceed.', 'opt_out':'Please unsubscribe.',
            'not_interested':'No thanks, not interested.',
            'bounce':'Delivery status notification: undeliverable.'}
    for index,kind in enumerate(history):
        classified=sponsor_replies.classify(bodies[kind],'Re: sponsor',{},paypal.iso(AT))
        assert classified['kind']==kind
        assert classified['auto_resume'] is False
        e.stop(classified['kind'],str(index))
    (e.state/'ledger.json').write_text('{}')
    before={p.name:p.read_bytes() for p in e.state.glob('*.json')}
    db_before=e.db.read_bytes()
    expected=history==['interested','agreement_candidate']
    assert e.bridge.verify(e.site,e.state,AT)['payment_verified'] is expected
    assert {p.name:p.read_bytes() for p in e.state.glob('*.json')}==before
    assert e.db.read_bytes()==db_before


def test_observer_unrelated_classified_interest_is_provider_scoped(native_observer):
    from inbox_control_plane import sponsor_replies
    import sqlite3
    e=native_observer
    classified=sponsor_replies.classify('Send us the details.','Re: sponsor',{},paypal.iso(AT))
    assert classified['kind']=='interested'
    e.stop(classified['kind'])
    with sqlite3.connect(e.db) as db:
        raw=json.loads(db.execute('SELECT provider_receipt_json FROM mailbox_actions').fetchone()[0])
        raw.update(provider_hosts=['other.invalid'],enrolled_addresses=['partner@other.invalid'])
        db.execute('UPDATE mailbox_actions SET provider_receipt_json=?',(json.dumps(raw),))
    before=e.db.read_bytes()
    assert e.bridge.verify(e.site,e.state,AT)['payment_verified'] is True
    assert e.db.read_bytes()==before


@pytest.mark.parametrize('receipt',[
    {'kind':'unknown_future_kind','provider_hosts':['other.invalid']},
    {'kind':'interested','provider_hosts':[]},
    {'kind':'interested','provider_hosts':'other.invalid'},
    {'kind':'interested','provider_hosts':['']},
    {'kind':'interested','provider_hosts':[None]},
    {'provider_hosts':['other.invalid']}])
def test_observer_unknown_or_malformed_interest_custody_still_denies(native_observer,receipt):
    import sqlite3
    e=native_observer
    e.stop('interested')
    with sqlite3.connect(e.db) as db:
        db.execute('UPDATE mailbox_actions SET provider_receipt_json=?',(json.dumps(receipt),))
    # Empty enrollment exercises the observer's provider-history validation;
    # it must still reject bad custody without the campaign's row-fold seam.
    (e.state/'ledger.json').write_text('{}')
    before=e.db.read_bytes()
    with pytest.raises(ValueError,match='canonical_reply_custody_unknown'):
        e.bridge.eligible_hosts(e.state,AT)
    assert e.bridge.verify(e.site,e.state,AT)['payment_verified'] is False
    assert e.db.read_bytes()==before
