"""Sponsor-only evidence interfaces. No enrichment, verification debit or payment effects."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
import re
import urllib.request
from pathlib import Path

PROFESSIONAL = re.compile(r'partnership|marketing|business development|founder|\bCEO\b|\bCOO\b|chief executive',re.I)


def current_evidence(row,at,days=30):
    try:
        age=at-dt.datetime.fromisoformat(row['captured_at_utc'])
        return dt.timedelta(0)<=age<=dt.timedelta(days=days) and bool(re.fullmatch('[0-9a-f]{64}',row['evidence_sha256']))
    except (KeyError,TypeError,ValueError):
        return False


def professional_contact(host,rows,at):
    for row in rows:
        email=str(row.get('email') or '').lower()
        if (re.fullmatch(r'[a-z0-9][a-z0-9._%+-]*@[a-z0-9.-]+',email)
            and row.get('source')=='apollo' and row.get('person_id') and row.get('country')=='US'
            and row.get('organization_domain','').lower()==host and email.count('@')==1
            and email.split('@')[1]==host and PROFESSIONAL.search(row.get('title',''))
            and not re.search(r'patient|clinical|support|billing',row.get('title',''),re.I)
            and current_evidence(row,at)):
            return row
    return None


def relationship(host,evidence,at):
    if (evidence.get('provider_host')==host and evidence.get('status')=='approved'
        and evidence.get('network') and evidence.get('program_id') and current_evidence(evidence,at)):
        return {'variant':'established_affiliate','network':evidence['network'],'evidence':evidence}
    return {'variant':'cold'}


def apollo_transport(method,path,payload):
    # Allowlist the one documented zero-credit search operation. Never reveal,
    # enrich, export, waterfall, phone lookup or silently try another endpoint.
    if method!='POST' or path!='/api/v1/mixed_people/api_search':
        raise RuntimeError('apollo_credit_operation_not_authorized')
    key=os.environ.get('APOLLO_API_KEY')
    if not key:raise RuntimeError('apollo_account_unavailable')
    request=urllib.request.Request('https://api.apollo.io'+path,data=json.dumps(payload).encode(),method=method,
                                   headers={'x-api-key':key,'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=20) as response:
        return json.load(response)


def apollo_search(host,transport=apollo_transport):
    payload={'q_organization_domains_list':[host],'person_locations':['United States'],
             'person_titles':['partnerships','marketing','business development','founder','CEO','COO'],
             'page':1,'per_page':25}
    data=transport('POST','/api/v1/mixed_people/api_search',payload)
    contacts=[{'person_id':row.get('id'),'title':row.get('title'),'organization_domain':(row.get('organization') or {}).get('primary_domain'),
               'verification_status':'unknown'} for row in data.get('people',[])]
    return {'source':'apollo_zero_credit_people_search','contacts':contacts,'enrichment_debits':0,
            'status':'sourcing_only','provider_host':host,'captured_at_utc':dt.datetime.now(dt.timezone.utc).isoformat()}

def paypal_capture_readback(capture_id):
    # Read-only edge, deliberately not called by the outreach job. No token
    # creation, checkout, invoice, capture, refund, transfer or recurring charge.
    if not re.fullmatch('[A-Za-z0-9]+',capture_id):raise ValueError('invalid_capture_id')
    token=os.environ.get('PAYPAL_ACCESS_TOKEN')
    if not token:raise RuntimeError('paypal_authenticated_business_account_unavailable')
    request=urllib.request.Request('https://api-m.paypal.com/v2/payments/captures/'+capture_id,
                                   headers={'Authorization':'Bearer '+token,'Accept':'application/json'})
    with urllib.request.urlopen(request,timeout=20) as response:
        return {'authenticated':True,'environment':'live','http_status':response.status,'capture':json.load(response)}


def payment_readiness(order,transport=None):
    from decimal import Decimal,InvalidOperation
    result={'status':'unknown','payment_verified':False,'placement_allowed':False,'reason':'authenticated_payment_not_checked'}
    if not order.get('reviewed_order') or not order.get('order_reference') or not order.get('capture_id') or not order.get('merchant_id'):
        return dict(result,reason='reviewed_order_or_exact_account_missing')
    if transport is None:return result
    try:
        response=transport(order['capture_id']);capture=response.get('capture',{})
        payee=capture.get('payee',{});amount=capture.get('amount',{})
        valid=(response.get('authenticated') is True and response.get('http_status')==200 and response.get('environment')=='live'
               and capture.get('id')==order['capture_id'] and capture.get('status')=='COMPLETED'
               and capture.get('invoice_id')==order['order_reference']
               and payee.get('email_address','').lower()=='ops@glpclinicscout.com'
               and order.get('payee','').lower()=='ops@glpclinicscout.com' and payee.get('merchant_id')==order['merchant_id']
               and amount.get('currency_code')==order.get('currency')=='USD'
               and Decimal(amount.get('value','NaN'))==Decimal(order.get('amount','NaN'))>0)
    except (KeyError,TypeError,ValueError,InvalidOperation):
        return dict(result,status='blocked',reason='malformed_payment_readback')
    if not valid:return dict(result,status='blocked',reason='payment_order_account_or_status_mismatch')
    exact=json.dumps(response,sort_keys=True,separators=(',',':')).encode()
    return dict(result,status='verified',payment_verified=True,reason='authenticated_matching_completed_capture',
                capture_id=order['capture_id'],order_reference=order['order_reference'],payee='ops@glpclinicscout.com',
                amount=amount['value'],currency='USD',merchant_id=order['merchant_id'],receipt_json=exact.decode(),receipt_sha256=hashlib.sha256(exact).hexdigest(),
                source='paypal_live_authenticated_capture_readback')


BOUNCER_LEDGER = Path('/home/ubuntu/business/reports/bouncer-shared-credit-ledger-2026-06-25.jsonl')


def email_hash(email):
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def bouncer_readiness(email, path=BOUNCER_LEDGER, at=None):
    """Read the existing shared guard ledger, never call its debit endpoint.

    Last matching observation wins, including unknown/negative observations.
    Missing or damaged coverage fails closed; a capacity read is not verification.
    """
    at = at or dt.datetime.now(dt.timezone.utc)
    result = {'status': 'unknown', 'send_eligible': False, 'verification_debits': 0}
    if not Path(path).exists():
        return result
    latest = None
    try:
        for line in Path(path).read_text().splitlines():
            row = json.loads(line)
            if row.get('event') == 'verify_email' and row.get('email_hash') == email_hash(email):
                latest = row
        if latest is None:
            return result
        observed = dt.datetime.fromisoformat(latest['ts'])
        age = at - observed
        if age < dt.timedelta(0) or age > dt.timedelta(days=7):
            result['status'] = 'expired'
        elif (latest.get('http_status') == 200 and latest.get('provider_status') == 'deliverable'
              and latest.get('send_eligible') is True and latest.get('blockers') == []):
            result.update(status='verified', send_eligible=True)
        else:
            result['status'] = 'blocked'
        result.update(observed_at=latest['ts'], evidence_sha256=hashlib.sha256(json.dumps(latest,sort_keys=True).encode()).hexdigest())
    except (ValueError, KeyError, TypeError, OSError):
        result['status'] = 'coverage_incomplete'
    return result
