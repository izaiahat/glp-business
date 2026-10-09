"""Offline fixtures, never business/payment evidence."""
import copy
import datetime as dt
import sys
from pathlib import Path
import pytest
sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'scripts')]
import glp_sponsor_campaign as campaign
import glp_sponsor_paypal as paypal

AT=dt.datetime(2026,10,7,1,tzinfo=dt.timezone.utc)
TX='1AB23456CD789012E'


def order_fixture(tmp_path):
    # Entirely synthetic agreement and operator approval, isolated filesystem.
    order={'order_id':'fixture-initial','provider_host':'example.invalid','recipient':'billing@example.invalid',
           'kind':'initial','invoice_note':'Synthetic fixture sponsor pilot',
           'invoice_terms':'30-day pilot. Optional $250 renewal by new agreement only. No automatic charges.',
           'scope':'setup_and_30_day_placement','agreement_sha256':'', 'invoice_id':'INV2-FIXT-URE0-0000-0000'}
    import hashlib
    path=tmp_path/'agreements'/('fixture-initial.eml');path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(b'SYNTHETIC AGREEMENT - NOT A REAL SPONSOR')
    order['agreement_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    approve_fixture(tmp_path,order)
    return order


def invoice_fixture(order):
    return {'id':order['invoice_id'],'status':'PAID','detail':{'reference':'GLP:example.invalid:fixture-initial',
            'invoice_number':'fixture-initial','currency_code':'USD','note':order['invoice_note'],
            'terms_and_conditions':order['invoice_terms'],'payment_term':{'term_type':'DUE_ON_RECEIPT'},
            'metadata':{'create_time':'2026-10-06T00:00:00Z'}},
            'invoicer':{'email_address':'ops@glpclinicscout.com'},
            'primary_recipients':[{'billing_info':{'email_address':order['recipient']}}],
            'items':[{'name':'Sponsor pilot setup','quantity':'1','unit_amount':{'currency_code':'USD','value':'250.00'}},
                     {'name':'30-day sponsor placement','quantity':'1','unit_amount':{'currency_code':'USD','value':'250.00'}}],
            'configuration':{'partial_payment':{'allow_partial_payment':False},'allow_tip':False},
            'amount':{'currency_code':'USD','value':'500.00'},'due_amount':{'currency_code':'USD','value':'0.00'},
            'payments':{'paid_amount':{'currency_code':'USD','value':'500.00'},'transactions':[
                {'type':'PAYPAL','payment_id':TX,'amount':{'currency_code':'USD','value':'500.00'}}]}}


def report_fixture(order):
    return {'account_number':'FIXTURE-MERCHANT','page':1,'total_pages':1,'total_items':1,
            'last_refreshed_datetime':'2026-10-07T00:00:00Z','transaction_details':[
                {'transaction_info':{'paypal_account_id':'FIXTURE-MERCHANT','transaction_id':TX,
                 'transaction_status':'S','transaction_event_code':'T0000','invoice_id':'fixture-initial',
                 'transaction_amount':{'currency_code':'USD','value':'500.00'}},
                 'cart_info':{'paypal_invoice_id':order['invoice_id']},
                 'payer_info':{'email_address':order['recipient']}}]}


def fake_client(monkeypatch, invoice, report):
    client=object.__new__(paypal.PayPalClient)
    monkeypatch.setattr(client,'merchant',lambda at:'FIXTURE-MERCHANT')
    def read(method,path,payload=None):
        assert method=='GET', 'no money effect allowed in polling'
        if path.startswith('/v2/invoicing/'):return copy.deepcopy(invoice)
        assert path.startswith('/v1/reporting/transactions?')
        return copy.deepcopy(report)
    monkeypatch.setattr(client,'request',read)
    return client


@pytest.mark.parametrize('lifetime_days', [1, 38])
def test_exact_paypal_transaction_not_mark_paid_proves_money(tmp_path,monkeypatch,lifetime_days):
    order=order_fixture(tmp_path)
    inv=invoice_fixture(order);report=report_fixture(order)
    inv['detail']['metadata']['create_time']=paypal.iso(AT-dt.timedelta(days=lifetime_days))
    result=paypal.poll_order(fake_client(monkeypatch,inv,report),order,AT,tmp_path)
    assert result['payment_verified'] is True
    assert result['status']=='paid'
    assert result['transaction_ids']==[TX]
    assert result['placement_allowed'] is False # Payment is not release authority.


@pytest.mark.parametrize('failure',['external','partial','refund','reversal','cancelled','pending','payer','merchant','currency','amount','invoice','stale','incomplete','tip'])
def test_negative_or_incomplete_readback_revokes_paid(tmp_path,monkeypatch,failure):
    order=order_fixture(tmp_path);inv=invoice_fixture(order);report=report_fixture(order)
    info=report['transaction_details'][0]['transaction_info']
    if failure=='external':inv['payments']['transactions'][0]['type']='EXTERNAL'
    if failure=='partial':inv['status']='PARTIALLY_PAID'
    if failure=='refund':inv['refunds']={'transactions':[{'type':'PAYPAL'}]}
    if failure=='reversal':info['transaction_status']='V'
    if failure=='cancelled':inv['status']='CANCELLED'
    if failure=='pending':info['transaction_status']='P'
    if failure=='payer':report['transaction_details'][0]['payer_info']['email_address']='other@example.invalid'
    if failure=='merchant':report['account_number']='OTHER'
    if failure=='currency':info['transaction_amount']['currency_code']='EUR'
    if failure=='amount':info['transaction_amount']['value']='250.00'
    if failure=='invoice':report['transaction_details'][0]['cart_info']['paypal_invoice_id']='INV2-OTHER'
    if failure=='stale':report['last_refreshed_datetime']='2026-10-01T00:00:00Z'
    if failure=='incomplete':report['total_items']=2
    if failure=='tip':inv['configuration']['allow_tip']=True
    result=paypal.poll_order(fake_client(monkeypatch,inv,report),order,AT,tmp_path)
    assert result['payment_verified'] is False
    assert result['placement_allowed'] is False



def approve_fixture(tmp_path,order,send=False):
    campaign.save(tmp_path/'invoice-approvals.json', {order['order_id']:{'actor':'Izaiah Tilton',
        'decision':'APPROVE SPONSOR INVOICE '+paypal.approval_digest(order),
        'discord_message_id':'SYNTHETIC-NOT-AUTHORITY','request_send':send}})


def test_existing_job_draft_then_request_no_replay(tmp_path,monkeypatch):
    order=order_fixture(tmp_path);order.pop('invoice_id');order['invoice_action']='draft'
    monkeypatch.setattr(campaign,'STATE',tmp_path)
    assert hasattr(paypal,'approval_digest'), 'missing exact invoice authorization binding'
    approve_fixture(tmp_path,order,True)
    inv=invoice_fixture({**order,'invoice_id':'INV2-FIXT-URE0-0000-0000'});inv['status']='DRAFT'
    client=fake_client(monkeypatch,inv,report_fixture({**order,'invoice_id':inv['id']}))
    effects=[]
    def write(order,state,action,at):
        effects.append(action)
        if action=='draft':return copy.deepcopy(inv)
        inv['status']='SENT';return {}
    monkeypatch.setattr(client,'invoice_write',write)
    monkeypatch.setattr(paypal,'PayPalClient',lambda:client)
    campaign.save(tmp_path/'orders.json',{order['order_id']:order})
    config={'paypal_read_only_polling':True,'paypal_invoice_drafts_enabled':True,'paypal_invoice_requests_enabled':True}
    assert campaign.refresh_paypal_orders(config,AT,dry_run=True)['financial_writes']==0
    assert effects==[]
    assert campaign.refresh_paypal_orders(config,AT,finance_hosts=('example.invalid',))['financial_writes']==1
    assert campaign.refresh_paypal_orders(config,AT,finance_hosts=('example.invalid',))['financial_writes']==0
    assert effects==['draft']
    orders=campaign.load(tmp_path/'orders.json');orders[order['order_id']]['invoice_action']='request'
    campaign.save(tmp_path/'orders.json',orders)
    assert campaign.refresh_paypal_orders(config,AT,finance_hosts=('example.invalid',))['financial_writes']==1
    assert campaign.refresh_paypal_orders(config,AT,finance_hosts=('example.invalid',))['financial_writes']==0
    assert effects==['draft','request']
    assert campaign.load(tmp_path/'orders.json')[order['order_id']]['invoice_requested_at']


def test_unknown_draft_effect_keeps_claim_forever(tmp_path,monkeypatch):
    order=order_fixture(tmp_path);order.pop('invoice_id');order['invoice_action']='draft'
    monkeypatch.setattr(campaign,'STATE',tmp_path)
    assert hasattr(paypal,'approval_digest')
    approve_fixture(tmp_path,order)
    client=object.__new__(paypal.PayPalClient);effects=[]
    def uncertain(*args):
        effects.append('draft');raise RuntimeError('SYNTHETIC unknown effect')
    monkeypatch.setattr(client,'merchant',lambda at:'FIXTURE-MERCHANT')
    monkeypatch.setattr(client,'invoice_write',uncertain)
    monkeypatch.setattr(paypal,'PayPalClient',lambda:client)
    campaign.save(tmp_path/'orders.json',{order['order_id']:order})
    config={'paypal_read_only_polling':True,'paypal_invoice_drafts_enabled':True}
    campaign.refresh_paypal_orders(config,AT,finance_hosts=('example.invalid',))
    campaign.refresh_paypal_orders(config,AT+dt.timedelta(days=90),finance_hosts=('example.invalid',))
    assert effects==['draft']
    assert campaign.load(tmp_path/'orders.json')[order['order_id']]['invoice_claim']['action']=='draft'


def test_registered_run_consumes_fresh_paypal_poll_not_order_boolean(tmp_path,monkeypatch):
    from test_glp_sponsor_campaign import offline_campaign
    config,mail,_=offline_campaign(tmp_path,monkeypatch)
    order=order_fixture(campaign.STATE)
    campaign.save(campaign.STATE/'orders.json',{order['order_id']:order})
    client=fake_client(monkeypatch,invoice_fixture(order),report_fixture(order))
    monkeypatch.setattr(paypal,'PayPalClient',lambda:client)
    monkeypatch.setattr(campaign,'now',lambda:AT)
    config.update(paypal_read_only_polling=True)
    result=campaign.run(config,dry_run=True)
    assert result['payment_readiness']['orders'][order['order_id']]['payment_verified'] is True
    assert result['payment_readiness']['financial_writes']==0
    assert result['sent']==0 and mail.deliveries==[]


def test_read_edge_denies_all_money_and_webhook_endpoints(monkeypatch):
    client=object.__new__(paypal.PayPalClient)
    monkeypatch.setattr(client,'_authorized',lambda *a,**k:pytest.fail('denied operation reached auth'))
    for method,path in [('POST','/v2/invoicing/invoices'),('POST','/v2/payments/captures/X/refund'),
                        ('POST','/v1/payments/payouts'),('GET','/v1/notifications/webhooks'),
                        ('POST','/v2/invoicing/invoices/INV2-X/payments')]:
        with pytest.raises(RuntimeError,match='operation_not_authorized'):client.request(method,path)


@pytest.mark.parametrize('failure',['agreement','approval','copy','recipient','renewal','hold'])
def test_invoice_denial_precedes_durable_claim_and_external_write(tmp_path,monkeypatch,failure):
    order=order_fixture(tmp_path);order.pop('invoice_id');order['invoice_action']='draft'
    monkeypatch.setattr(campaign,'STATE',tmp_path)
    if failure=='agreement':(tmp_path/'agreements/fixture-initial.eml').write_text('changed')
    if failure=='approval':(tmp_path/'invoice-approvals.json').unlink()
    if failure=='copy':order['invoice_note']='unapproved new copy'
    if failure=='recipient':order['recipient']='other@example.invalid'
    if failure=='renewal':order.update(kind='renewal',scope='30_day_renewal',renewal_agreed=False)
    client=object.__new__(paypal.PayPalClient)
    monkeypatch.setattr(client,'invoice_write',lambda *a:pytest.fail('unauthorized effect'))
    monkeypatch.setattr(paypal,'PayPalClient',lambda:client)
    campaign.save(tmp_path/'orders.json',{order['order_id']:order})
    result=campaign.refresh_paypal_orders({'paypal_read_only_polling':True,'paypal_invoice_drafts_enabled':True},AT,
                  finance_hosts=() if failure=='hold' else ('example.invalid',))
    assert result['financial_writes']==0
    assert 'invoice_claim' not in campaign.load(tmp_path/'orders.json')[order['order_id']]


def test_real_write_method_exact_endpoints_and_no_extra_recipients(tmp_path,monkeypatch):
    order=order_fixture(tmp_path);approve_fixture(tmp_path,order,True)
    inv=invoice_fixture(order);inv['status']='DRAFT';inv['detail']['invoice_date']='2026-10-06'
    client=fake_client(monkeypatch,inv,report_fixture(order));calls=[]
    monkeypatch.setattr(client,'_authorized',lambda *args:calls.append(args) or {})
    client.invoice_write(order,tmp_path,'draft',AT)
    client.invoice_write(order,tmp_path,'request',AT)
    assert calls[0]==('POST','/v2/invoicing/invoices',paypal.invoice_payload(order))
    assert calls[1]==('POST','/v2/invoicing/invoices/'+order['invoice_id']+'/send',
                     {'send_to_invoicer':False,'send_to_recipient':True,'additional_recipients':[]})
    for action in ('mark-paid','charge','refund','payout','renew'):
        with pytest.raises(ValueError):client.invoice_write(order,tmp_path,action,AT)
    inv['items'][0]['description']='Unapproved customer-visible claim'
    with pytest.raises(ValueError):client.invoice_write(order,tmp_path,'request',AT)
    assert len(calls)==2


def test_refund_reference_without_invoice_id_still_revokes_money(tmp_path,monkeypatch):
    order=order_fixture(tmp_path);report=report_fixture(order)
    report['transaction_details'].append({'transaction_info':{'transaction_id':'REFUND-FIXTURE',
        'paypal_reference_id':TX,'transaction_status':'S','transaction_event_code':'T1107',
        'transaction_amount':{'currency_code':'USD','value':'-1.00'}}})
    report['total_items']=2
    result=paypal.poll_order(fake_client(monkeypatch,invoice_fixture(order),report),order,AT,tmp_path)
    assert result['payment_verified'] is False


def test_same_invoice_cannot_send_or_allocate_twice(tmp_path,monkeypatch):
    order=order_fixture(tmp_path);order['invoice_action']='request';approve_fixture(tmp_path,order,True)
    monkeypatch.setattr(campaign,'STATE',tmp_path)
    client=fake_client(monkeypatch,invoice_fixture(order),report_fixture(order))
    monkeypatch.setattr(client,'invoice_write',lambda *a:pytest.fail('duplicate invoice requested'))
    monkeypatch.setattr(paypal,'PayPalClient',lambda:client)
    orders={order['order_id']:order,'other-order':{**order,'order_id':'other-order'}}
    campaign.save(tmp_path/'orders.json',orders)
    result=campaign.refresh_paypal_orders({'paypal_read_only_polling':True,'paypal_invoice_requests_enabled':True},AT,
                                         finance_hosts=('example.invalid',))
    assert result['financial_writes']==0
    assert not any(x['payment_verified'] for x in result['orders'].values())
    assert all(x['reason']=='paypal_duplicate_order_allocation' for x in result['orders'].values())


def test_live_merchant_pin_requires_app_account_and_reviewed_receipt(tmp_path,monkeypatch):
    import hashlib
    client=object.__new__(paypal.PayPalClient);client.app={'client_id':'SYNTHETIC-NOT-A-KEY'}
    evidence=tmp_path/'receipt.json';evidence.write_text('SYNTHETIC ONLY')
    binding={'actor':'Izaiah Tilton','business_email':paypal.MERCHANT_EMAIL,'account_type':'BUSINESS',
             'merchant_id':'FIXTURE-MERCHANT','client_id_sha256':hashlib.sha256(client.app['client_id'].encode()).hexdigest(),
             'source':'authenticated_paypal_account_and_app','approval_message_id':'SYNTHETIC-NOT-AUTHORITY',
             'evidence_path':str(evidence),'evidence_sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}
    pin=tmp_path/'account-binding.json';campaign.save(pin,binding);monkeypatch.setattr(paypal,'BINDING',pin)
    monkeypatch.setattr(client,'request',lambda *a:{'account_number':'FIXTURE-MERCHANT'})
    assert client.merchant(AT)=='FIXTURE-MERCHANT'
    monkeypatch.setattr(client,'request',lambda *a:{'account_number':'WRONG-MERCHANT'})
    with pytest.raises(RuntimeError,match='merchant_mismatch'):client.merchant(AT)
    monkeypatch.setattr(client,'request',lambda *a:{'account_number':'FIXTURE-MERCHANT'})
    binding['business_email']='other@example.invalid';campaign.save(pin,binding)
    with pytest.raises(RuntimeError,match='binding_missing'):client.merchant(AT)
    binding['business_email']=paypal.MERCHANT_EMAIL;binding['client_id_sha256']='a'*64;campaign.save(pin,binding)
    with pytest.raises(RuntimeError,match='binding_missing'):client.merchant(AT)


def test_actual_invoice_write_cannot_use_scope_as_identity(tmp_path,monkeypatch):
    order=order_fixture(tmp_path);approve_fixture(tmp_path,order,True)
    client=object.__new__(paypal.PayPalClient);client.app={'client_id':'SYNTHETIC-NOT-A-KEY'}
    monkeypatch.setattr(paypal,'BINDING',tmp_path/'absent-binding.json')
    monkeypatch.setattr(client,'_authorized',lambda *a:pytest.fail('missing identity reached effect'))
    with pytest.raises(OSError):client.invoice_write(order,tmp_path,'draft',AT)


@pytest.mark.parametrize('failure', ['duplicate_final_credit', 'pages_drift', 'refresh_drift', 'valid_unique'])
def test_two_page_reporting_requires_unique_stable_window_coverage(tmp_path, monkeypatch, failure):
    import urllib.parse
    order = order_fixture(tmp_path)
    invoice = invoice_fixture(order)
    report = report_fixture(order)
    credit = copy.deepcopy(report['transaction_details'][0])
    rows = [credit]
    for number in range(499):
        row = copy.deepcopy(credit)
        row['transaction_info'].update(transaction_id=f'UNRELATED{number:08d}', invoice_id='unrelated')
        row['cart_info']['paypal_invoice_id'] = 'INV2-UNRELATED'
        rows.append(row)
    first = {**report, 'page': 1, 'total_pages': 2, 'total_items': 501, 'transaction_details': rows}
    final = {**first, 'page': 2, 'transaction_details': [copy.deepcopy(credit)]}
    if failure != 'duplicate_final_credit':
        final['transaction_details'][0]['transaction_info']['transaction_id'] = 'UNRELATED-FINAL'
        final['transaction_details'][0]['transaction_info']['invoice_id'] = 'unrelated'
        final['transaction_details'][0]['cart_info']['paypal_invoice_id'] = 'INV2-UNRELATED'
        if failure == 'pages_drift':final['total_pages'] = 1
        elif failure == 'refresh_drift':final['last_refreshed_datetime'] = '2026-10-07T00:30:00Z'
    client = fake_client(monkeypatch, invoice, report)
    def read(method, path, payload=None):
        assert method == 'GET'
        if path.startswith('/v2/invoicing/'):return copy.deepcopy(invoice)
        page = int(urllib.parse.parse_qs(urllib.parse.urlparse(path).query)['page'][0])
        return copy.deepcopy(first if page == 1 else final)
    monkeypatch.setattr(client, 'request', read)
    result = paypal.poll_order(client, order, AT, tmp_path)
    if failure == 'valid_unique':
        assert result['payment_verified'] is True and result['gross_usd'] == '500.00'
    else:
        assert result['status'] == 'unknown' and result['payment_verified'] is False
    assert result['placement_allowed'] is False


def test_approved_invoice_projection_retains_safe_server_fields(tmp_path, monkeypatch):
    order = order_fixture(tmp_path)
    approve_fixture(tmp_path, order, True)
    invoice = invoice_fixture(order)
    invoice['status'] = 'DRAFT'
    invoice['detail'].update(invoice_date='2026-10-06', attachments=[], memo='')
    invoice['detail']['payment_term']['due_date'] = '2026-10-06'
    invoice['links'] = [{'rel': 'self', 'href': 'https://example.invalid/server-link'}]
    invoice['additional_recipients'] = []
    invoice['configuration'].update(tax_calculated_after_discount=True, tax_inclusive=False, has_conditional_rule=False)
    invoice['amount']['breakdown'] = {'item_total': {'currency_code': 'USD', 'value': '500.00'},
                                      'tax_total': {'currency_code': 'USD', 'value': '0.00'}}
    for index, item in enumerate(invoice['items']):
        item.update(id=f'server-item-{index}', unit_of_measure='QUANTITY', description='')
        item['unit_amount']['value'] = '250.0'
    original = copy.deepcopy(invoice)
    client = fake_client(monkeypatch, invoice, report_fixture(order))
    effects = []
    monkeypatch.setattr(client, '_authorized', lambda *args: effects.append(args) or {})
    client.invoice_write(order, tmp_path, 'request', AT)
    assert len(effects) == 1
    assert invoice == original # Projection never mutates the API readback.
    invoice['status'] = 'PAID'
    result = paypal.poll_order(client, order, AT, tmp_path)
    assert result['payment_verified'] is True and result['gross_usd'] == '500.00'
    assert result['placement_allowed'] is False


@pytest.mark.parametrize('drift', ['payment_term', 'attachment', 'conditional_discount'])
def test_unapproved_invoice_schema_cannot_send_or_verify_payment(tmp_path, monkeypatch, drift):
    # Independent R2 paths, not a top-level attachments approximation.
    order = order_fixture(tmp_path)
    approve_fixture(tmp_path, order, True)
    invoice = invoice_fixture(order)
    invoice['status'] = 'DRAFT'
    invoice['detail']['invoice_date'] = '2026-10-06'
    if drift == 'payment_term':
        invoice['detail']['payment_term'] = {'term_type': 'NET_90'}
    elif drift == 'attachment':
        invoice['detail']['attachments'] = [{'id': 'unapproved-file', 'reference_url': 'https://example.invalid/unapproved.pdf'}]
    else:
        invoice['detail']['payment_term']['conditional_rules'] = {'early_payment_discount': {
            'discount_end_date': '2026-10-10', 'percent': '50', 'is_applied': False}}
    client = fake_client(monkeypatch, invoice, report_fixture(order))
    effects = []
    monkeypatch.setattr(client, '_authorized', lambda *args: effects.append(args) or {})
    try:
        client.invoice_write(order, tmp_path, 'request', AT)
    except (RuntimeError, ValueError, KeyError, TypeError):
        pass
    assert effects == [], f'Unapproved {drift} reached invoice send'
    invoice['status'] = 'PAID'
    result = paypal.poll_order(client, order, AT, tmp_path)
    assert result['status'] == 'unknown' and result['payment_verified'] is False
    assert result['placement_allowed'] is False


@pytest.mark.parametrize('change', ['opt_out', 'registry_hold', 'suppression', 'stale_cursor', 'allowed'])
def test_invoice_send_rechecks_committed_optout_before_claim(tmp_path, monkeypatch, change):
    # Reuse independent review R1: commit a real canonical stop during the
    # synthetic merchant read, before the real campaign claim/write boundary.
    import json
    import sqlite3
    from test_glp_sponsor_campaign import offline_campaign, inputs
    from test_glp_sponsor_boundary_repairs import inbox
    config, mail, recipients = offline_campaign(tmp_path, monkeypatch)
    recipients.clear()
    config.update(paypal_read_only_polling=True, paypal_invoice_requests_enabled=True,
                  require_canonical_reply_coverage=True)
    monkeypatch.setattr(campaign, 'INBOX_DB', tmp_path/'inbox.sqlite')
    inbox(campaign.INBOX_DB)
    order = order_fixture(campaign.STATE)
    order['invoice_action'] = 'request'
    approve_fixture(campaign.STATE, order, True)
    campaign.save(campaign.STATE/'orders.json', {order['order_id']: order})
    campaign.save(campaign.STATE/'ledger.json', {order['recipient']: {'host': order['provider_host'], 'stopped': 'agreement_candidate'}})
    sheet, _, local = inputs()
    sheet[1][6] = 'https://example.invalid'
    monkeypatch.setattr(campaign, 'read_values', lambda: sheet)
    monkeypatch.setattr(campaign, 'read_local', lambda: local)
    inv = invoice_fixture(order)
    inv['status'] = 'DRAFT'
    inv['detail']['invoice_date'] = '2026-10-06'
    client = fake_client(monkeypatch, inv, report_fixture(order))
    effects = []
    committed = []
    def merchant(at):
        if not committed:
            if change == 'opt_out':
                stop = {'kind': 'opt_out', 'event_id': 'review-stop',
                        'provider_hosts': [order['provider_host']], 'enrolled_addresses': [order['recipient']]}
                with sqlite3.connect(campaign.INBOX_DB, timeout=0) as db:
                    db.execute('INSERT INTO mailbox_actions VALUES(?,?,?,?,?,?)',
                               ('review-stop','review-stop','sponsor_reply_stop','verified',json.dumps(stop),at.isoformat()))
            elif change == 'registry_hold':sheet[1][10] = 'hold'
            elif change == 'suppression':local['suppression']['entries'].append({'domain': order['provider_host']})
            elif change == 'stale_cursor':
                with sqlite3.connect(campaign.INBOX_DB) as db:
                    db.execute("UPDATE provider_cursors SET last_complete_poll_at='2026-10-01T00:00:00+00:00'")
            committed.append(True)
        return 'FIXTURE-MERCHANT'
    def assert_serialized():
        assert campaign.load(campaign.STATE/'orders.json')[order['order_id']]['invoice_claim']['action'] == 'request'
        with sqlite3.connect(campaign.INBOX_DB, timeout=0) as db:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                db.execute("INSERT INTO mailbox_actions VALUES('contender','contender','sponsor_reply_stop','verified','{}','now')")
    def effect(method, path, payload=None):
        assert_serialized()
        effects.append((method,path))
        inv['status'] = 'SENT'
        return {}
    original_read = client.request
    readbacks = []
    def read(method, path, payload=None):
        if path.startswith('/v2/invoicing/') and inv['status'] == 'SENT' and not readbacks:
            assert_serialized()
            readbacks.append(True)
        return original_read(method, path, payload)
    monkeypatch.setattr(client, 'request', read)
    monkeypatch.setattr(client, 'merchant', merchant)
    monkeypatch.setattr(client, '_authorized', effect)
    monkeypatch.setattr(paypal, 'PayPalClient', lambda: client)
    result = campaign.run(config)
    assert committed == [True]
    assert result['sent'] == 0 and mail.deliveries == []
    if change == 'allowed':
        assert len(effects) == 1 and readbacks == [True]
        assert campaign.load(campaign.STATE/'orders.json')[order['order_id']]['invoice_requested_at']
    else:
        assert effects == [] and result['payment_readiness']['financial_write_attempts'] == 0
    assert 'invoice_claim' not in campaign.load(campaign.STATE/'orders.json')[order['order_id']]
    if change == 'opt_out':
        assert campaign.load(campaign.STATE/'ledger.json')[order['recipient']]['stopped'] == 'opt_out'


def test_existing_job_persists_unknown_without_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign, 'STATE', tmp_path)
    orders = {'fixture-order': {'order_id':'fixture-order', 'provider_host':'example.invalid'}}
    campaign.save(tmp_path/'orders.json', orders)
    assert hasattr(campaign, 'refresh_paypal_orders'), 'existing job lacks persistent PayPal integration'
    result = campaign.refresh_paypal_orders({}, dt.datetime.now(dt.timezone.utc), dry_run=True)
    assert result['orders']['fixture-order']['payment_verified'] is False
    assert campaign.load(tmp_path/'orders.json')['fixture-order']['payment_readiness']['status']=='unknown'
    assert result['financial_writes']==0
