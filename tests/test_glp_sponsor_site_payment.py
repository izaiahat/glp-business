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
