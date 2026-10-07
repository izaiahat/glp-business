"""PayPal sponsor edge, invoked only by the existing locked campaign job.

No cached/caller-authored payment verdict is consumed. Secrets and OAuth tokens
stay in memory; financial endpoints are deny-by-default. API failures expose only
status classes, never provider bodies/headers or payer profiles.
"""
from __future__ import annotations
import base64
import datetime as dt
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SECRET = Path('/home/ubuntu/.hermes/secrets/paypal-glp/app.json')
BINDING = SECRET.parent/'account-binding.json'
MERCHANT_EMAIL = 'ops@glpclinicscout.com'
BASE = 'https://api-m.paypal.com'
INVOICE_ID = r'INV2-[A-Z0-9-]+'


def unknown(reason):
    return {'status':'unknown','payment_verified':False,'placement_allowed':False,'reason':reason}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None # Never forward Basic/Bearer to a redirect origin.


class PayPalClient:
    def __init__(self):
        if (SECRET.is_symlink() or SECRET.stat().st_uid!=os.getuid()
            or SECRET.stat().st_mode & 0o077 or SECRET.parent.stat().st_mode & 0o077):
            raise RuntimeError('paypal_secret_permissions')
        self.app = json.loads(SECRET.read_text())
        self.token = None

    def _http(self, method, path, headers, data=None):
        request = urllib.request.Request(BASE+path, method=method, headers=headers, data=data)
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            raise RuntimeError('paypal_http_'+str(error.code)) from None
        except (OSError, ValueError):
            raise RuntimeError('paypal_transport_unavailable') from None

    def request(self, method, path, payload=None):
        # Only supported read operations. Writes have a separately gated method.
        reads = (path.split('?')[0] in {'/v2/invoicing/invoices','/v1/reporting/transactions',
                    '/v1/identity/openidconnect/userinfo'} or
                 re.fullmatch('/v2/invoicing/invoices/'+INVOICE_ID, path))
        if method!='GET' or not reads:
            raise RuntimeError('paypal_operation_not_authorized')
        return self._authorized(method,path,payload)

    def _authorized(self, method, path, payload=None):
        if self.token is None:
            basic=base64.b64encode((self.app['client_id']+':'+self.app['client_secret']).encode()).decode()
            auth=self._http('POST','/v1/oauth2/token',{'Authorization':'Basic '+basic,
                           'Content-Type':'application/x-www-form-urlencoded'},b'grant_type=client_credentials')
            self.token=auth['access_token']
        return self._http(method,path,{'Authorization':'Bearer '+self.token,'Accept':'application/json',
                    'Content-Type':'application/json','Prefer':'return=representation'},
                    None if payload is None else json.dumps(payload).encode())

    def invoice_write(self, order, state, action, at):
        authorize_order(order,state,action)
        self.merchant(at) # Must prove real intended merchant before money/request effects.
        payload=invoice_payload(order)
        if action=='draft':
            return self._authorized('POST','/v2/invoicing/invoices',payload)
        if action!='request' or not re.fullmatch(INVOICE_ID,order.get('invoice_id','')):
            raise RuntimeError('paypal_operation_not_authorized')
        invoice=self.request('GET','/v2/invoicing/invoices/'+order['invoice_id'])
        invoice_matches(invoice,order)
        if (invoice.get('id')!=order['invoice_id'] or invoice.get('status')!='DRAFT'
            or dt.date.fromisoformat(invoice['detail']['invoice_date'])>at.date()):
            raise RuntimeError('paypal_request_target_not_current_draft')
        return self._authorized('POST','/v2/invoicing/invoices/'+order['invoice_id']+'/send',
            {'send_to_invoicer':False,'send_to_recipient':True,'additional_recipients':[]})

    def merchant(self, at):
        # Client-credentials userinfo currently returns only sub/user_id, NOT the
        # business email. A reviewed authenticated account/app binding is required.
        if BINDING.is_symlink() or BINDING.stat().st_uid!=os.getuid() or BINDING.stat().st_mode & 0o077:
            raise RuntimeError('paypal_account_binding_permissions')
        binding=json.loads(BINDING.read_text())
        if (binding.get('actor')!='Izaiah Tilton' or binding.get('business_email')!=MERCHANT_EMAIL
            or binding.get('account_type')!='BUSINESS' or not binding.get('merchant_id')
            or binding.get('client_id_sha256')!=hashlib.sha256(self.app['client_id'].encode()).hexdigest()
            or binding.get('source')!='authenticated_paypal_account_and_app'
            or not binding.get('approval_message_id') or not binding.get('evidence_path')
            or hashlib.sha256(Path(binding['evidence_path']).read_bytes()).hexdigest()!=binding.get('evidence_sha256')):
            raise RuntimeError('paypal_account_binding_missing')
        query=urllib.parse.urlencode({'start_date':iso(at-dt.timedelta(days=1)), 'end_date':iso(at),
                                     'page_size':1,'fields':'transaction_info'})
        report=self.request('GET','/v1/reporting/transactions?'+query)
        if report.get('account_number')!=binding['merchant_id']:
            raise RuntimeError('paypal_merchant_mismatch')
        return binding['merchant_id']


def iso(at):
    return at.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def money(row):
    from decimal import Decimal
    if row.get('currency_code')!='USD' or not re.fullmatch(r'-?[0-9]+(?:\.[0-9]{1,2})?',row.get('value','')):
        raise ValueError('paypal_usd_amount_invalid')
    return Decimal(row['value'])


def invoice_payload(order):
    oid=order['order_id'];host=order['provider_host'];recipient=order['recipient']
    if (not re.fullmatch(r'[A-Za-z0-9-]{1,25}',oid) or not re.fullmatch(r'[a-z0-9.-]+',host)
        or 'fella' in host or len(host)>80
        or not re.fullmatch(r'[a-z0-9._%+-]+@'+re.escape(host),recipient)
        or not re.fullmatch(r'[0-9a-f]{64}',order['agreement_sha256'])
        or not order['invoice_note'].strip() or not order['invoice_terms'].strip()):
        raise ValueError('sponsor_order_binding_invalid')
    if order['kind']=='initial' and order['scope']=='setup_and_30_day_placement':
        names=['Sponsor pilot setup','30-day sponsor placement']
    elif order['kind']=='renewal' and order['scope']=='30_day_renewal' and order.get('renewal_agreed') is True:
        names=['30-day sponsor renewal']
    else:raise ValueError('sponsor_terms_not_approved')
    return {'detail':{'reference':'GLP:'+host+':'+oid,'invoice_number':oid,'currency_code':'USD',
                     'note':order['invoice_note'],'terms_and_conditions':order['invoice_terms'],
                     'payment_term':{'term_type':'DUE_ON_RECEIPT'}},
            'invoicer':{'email_address':MERCHANT_EMAIL},
            'primary_recipients':[{'billing_info':{'email_address':recipient}}],
            'items':[{'name':name,'quantity':'1','unit_amount':{'currency_code':'USD','value':'250.00'}} for name in names],
            'configuration':{'partial_payment':{'allow_partial_payment':False},'allow_tip':False}}


def approval_digest(order):
    bound={'payload':invoice_payload(order),'agreement_sha256':order['agreement_sha256'],
           'kind':order['kind'],'scope':order['scope'],'renewal_agreed':order.get('renewal_agreed',False),
           'provider':order.get('provider'),'slug':order.get('slug'),
           'sources':{name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                      for name in ('glp_sponsor_paypal.py','glp_sponsor_campaign.py')}}
    return hashlib.sha256(json.dumps(bound,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def authorize_order(order,state,action):
    if action not in ('draft','request'):raise ValueError('paypal_operation_not_authorized')
    invoice_payload(order)
    agreement=Path(state)/'agreements'/(order['order_id']+'.eml')
    if hashlib.sha256(agreement.read_bytes()).hexdigest()!=order['agreement_sha256']:
        raise ValueError('sponsor_agreement_changed')
    approvals=json.loads((Path(state)/'invoice-approvals.json').read_text())
    approval=approvals[order['order_id']]
    if (approval.get('actor')!='Izaiah Tilton' or not approval.get('discord_message_id')
        or approval.get('decision')!='APPROVE SPONSOR INVOICE '+approval_digest(order)
        or (action=='request' and approval.get('request_send') is not True)):
        raise ValueError('sponsor_invoice_exact_approval_missing')
    return approval_digest(order)


def invoice_matches(invoice,order):
    expected=invoice_payload(order)
    for field in ('reference','invoice_number','currency_code','note','terms_and_conditions'):
        if invoice.get('detail',{}).get(field)!=expected['detail'][field]:
            raise ValueError('paypal_invoice_order_mismatch')
    if invoice.get('invoicer',{}).get('email_address','').lower()!=MERCHANT_EMAIL:
        raise ValueError('paypal_invoicer_mismatch')
    recipients=invoice.get('primary_recipients',[])
    if len(recipients)!=1 or recipients[0].get('billing_info',{}).get('email_address','').lower()!=order['recipient']:
        raise ValueError('paypal_recipient_mismatch')
    if invoice.get('additional_recipients'):
        raise ValueError('paypal_unapproved_cc')
    items=invoice.get('items',[])
    if len(items)!=len(expected['items']):raise ValueError('paypal_line_items_mismatch')
    for actual,want in zip(items,expected['items']):
        if (actual.get('name')!=want['name'] or actual.get('quantity')!='1' or money(actual['unit_amount'])!=250
            or actual.get('tax') or actual.get('discount') or actual.get('description')):
            raise ValueError('paypal_line_items_mismatch')
    if invoice.get('detail',{}).get('memo') or invoice.get('attachments'):
        raise ValueError('paypal_unapproved_copy_or_attachment')
    config=invoice.get('configuration',{})
    if config.get('allow_tip') is not False or config.get('partial_payment',{}).get('allow_partial_payment') is not False:
        raise ValueError('paypal_tip_or_partial_not_approved')
    total=250*len(items)
    if money(invoice['amount'])!=total:raise ValueError('paypal_invoice_gross_mismatch')
    return total


def reporting_rows(client,created,at,merchant):
    # Search the full lifetime in supported <=31-day chunks, without ID/status
    # filters which could hide referenced refunds, chargebacks or reversals.
    if created>at or at-created>dt.timedelta(days=1095):
        raise ValueError('paypal_reporting_lifetime_unavailable')
    start=created;rows=[];latest=None
    while start<at:
        end=min(start+dt.timedelta(days=30),at);page=1;collected=0;total=None
        while True:
            query=urllib.parse.urlencode({'start_date':iso(start),'end_date':iso(end),
                'fields':'transaction_info,payer_info,cart_info','balance_affecting_records_only':'Y',
                'page_size':500,'page':page})
            report=client.request('GET','/v1/reporting/transactions?'+query)
            if report.get('account_number')!=merchant:raise ValueError('paypal_reporting_merchant_mismatch')
            pages=int(report['total_pages']);count=int(report['total_items'])
            if pages>100 or count>50000 or int(report['page'])!=page or (total is not None and total!=count):
                raise ValueError('paypal_reporting_coverage_incomplete')
            total=count;batch=report.get('transaction_details',[]);collected+=len(batch);rows.extend(batch)
            refreshed=dt.datetime.fromisoformat(report['last_refreshed_datetime'].replace('Z','+00:00'))
            if not dt.timedelta(0)<=at-refreshed<=dt.timedelta(hours=3):
                raise ValueError('paypal_reporting_refresh_stale')
            latest=min(latest,refreshed) if latest else refreshed
            if page>=pages:
                if collected!=count:raise ValueError('paypal_reporting_coverage_incomplete')
                break
            page+=1
        start=end
    if latest is None:raise ValueError('paypal_reporting_coverage_incomplete')
    # Boundary overlap and identical repeated records do not double count.
    unique={json.dumps(row,sort_keys=True):row for row in rows}
    return list(unique.values()),latest


def poll_order(client, order, at, state):
    try:
        authorize_order(order,state,'draft')
        invoice_id=order['invoice_id']
        if not re.fullmatch(INVOICE_ID,invoice_id):raise ValueError('paypal_invoice_id_invalid')
        merchant=client.merchant(at)
        invoice=client.request('GET','/v2/invoicing/invoices/'+invoice_id)
        if invoice.get('id')!=invoice_id:raise ValueError('paypal_invoice_id_mismatch')
        total=invoice_matches(invoice,order)
        status=invoice.get('status','UNKNOWN')
        if status!='PAID':
            result=unknown('paypal_invoice_not_paid');result.update(status=status.lower(),observed_at=iso(at))
            return result
        if invoice.get('refunds',{}).get('transactions') or money(invoice['due_amount'])!=0:
            raise ValueError('paypal_refund_or_unpaid_balance')
        payments=invoice['payments'];transactions=payments['transactions']
        if not transactions or money(payments['paid_amount'])!=total:
            raise ValueError('paypal_paid_amount_mismatch')
        ids=[];gross=0;payment_amounts={}
        for transaction in transactions:
            tid=transaction.get('payment_id','')
            if transaction.get('type')!='PAYPAL' or not re.fullmatch(r'[A-Z0-9]{17,19}',tid) or tid in ids:
                raise ValueError('paypal_external_or_duplicate_payment')
            ids.append(tid);payment_amounts[tid]=money(transaction['amount']);gross+=payment_amounts[tid]
        if gross!=total:raise ValueError('paypal_payment_gross_mismatch')
        created=dt.datetime.fromisoformat(invoice['detail']['metadata']['create_time'].replace('Z','+00:00'))
        rows,refreshed=reporting_rows(client,created,at,merchant)
        credited={}
        for row in rows:
            info=row['transaction_info'];tid=info.get('transaction_id')
            linked=(tid in ids or info.get('paypal_reference_id') in ids or
                    row.get('cart_info',{}).get('paypal_invoice_id')==invoice_id or
                    info.get('invoice_id')==order['order_id'])
            if not linked:continue
            if (info.get('transaction_status')!='S' or info.get('transaction_event_code') not in
                {'T0000','T0005','T0006','T0007','T0023'} or money(info['transaction_amount'])<=0):
                raise ValueError('paypal_refund_reversal_hold_or_pending')
            # paypal_account_id is the counterparty. Reporting account_number
            # authenticates the merchant on every page; payer email binds order.
            if (tid not in ids or row.get('cart_info',{}).get('paypal_invoice_id')!=invoice_id
                or row.get('payer_info',{}).get('email_address','').lower()!=order['recipient']
                or money(info['transaction_amount'])!=payment_amounts[tid]):
                raise ValueError('paypal_transaction_allocation_mismatch')
            if tid in credited:raise ValueError('paypal_ambiguous_transaction')
            credited[tid]=money(info['transaction_amount'])
        if set(credited)!=set(ids) or sum(credited.values())!=total:
            raise ValueError('paypal_payment_not_in_reporting_yet')
        return {'status':'paid','payment_verified':True,'placement_allowed':False,
                'order_id':order['order_id'],'provider_host':order['provider_host'],
                'provider':order.get('provider'),'slug':order.get('slug'),'order_sha256':approval_digest(order),
                'transaction_ids':sorted(ids),'gross_usd':str(total)+'.00','observed_at':iso(at),
                'reporting_refreshed_at':iso(refreshed),'reason':'authenticated_invoice_and_balance_transactions',
                'reporting_lag_max_seconds':10800}
    except (RuntimeError,OSError,ValueError,KeyError,TypeError):
        # Do not surface arbitrary exception strings (may contain response/PII).
        return unknown('paypal_account_invoice_or_transaction_unconfirmed')
