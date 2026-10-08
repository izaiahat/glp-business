#!/usr/bin/env python3
"""Read-only authenticated invoice/reporting observer for the site's existing gate.

No caller-supplied paid flag is trusted. No financial or public writes, sender,
new schedule, approval writer or production test override exists here. The site
supplies its exact reviewed order on stdin; durable canonical order/creative
approval, current registry/suppression, merchant and live processor GETs must
all agree. Shares the campaign's lock so allocation evidence cannot race it.
"""
from __future__ import annotations
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys
import sqlite3
# -I ignores PYTHONPATH/current directory; only this maintained sibling source
# directory is admitted. The site invokes the fixed installed path, not argv.
sys.path.insert(0,str(Path(__file__).resolve().parent))
import glp_sponsor_paypal as paypal

STATE = Path('/home/ubuntu/business/reports/ops/sponsor-machine-20261003/runtime')


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()


def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate_json_key')
        result[key]=value
    return result


def read(path):
    if path.is_symlink():raise ValueError('symlink_not_allowed')
    return json.loads(path.read_bytes(),object_pairs_hook=unique)


def denied():
    return {'payment_verified':False,'source':'paypal_authenticated_invoice_reporting_observer',
            'reason':'authenticated_payment_or_site_admission_unconfirmed'}


def eligible_hosts(state,at):
    # Installed campaign's authenticated registry + read-only cold-WAL snapshot.
    # No suppression writer or sender is called by this observer.
    import glp_sponsor_campaign as campaign
    local=campaign.read_local()
    if not local['suppression']['complete']:raise ValueError('suppression_incomplete')
    hosts=campaign.invoice_provider_hosts(campaign.read_values(),local)
    ledger=read(state/'ledger.json') if (state/'ledger.json').exists() else {}
    # Read the canonical snapshot and freshness closure, applying reply stops
    # only to this in-memory ledger. Never advance its cursor or write a stop.
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(campaign.INBOX_DB.as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        readiness=campaign.consume_reply_stops(ledger,db=db,at=at)
        if readiness['status']!='read_complete':raise ValueError('reply_coverage_unknown')
        opted_out=set()
        for (raw,) in db.execute("SELECT provider_receipt_json FROM mailbox_actions WHERE action='sponsor_reply_stop' AND status='verified'"):
            receipt=json.loads(raw,object_pairs_hook=unique)
            if receipt.get('kind')=='opt_out':opted_out.update(receipt['provider_hosts'])
    return {host for host in hosts if not any(campaign.same(host,h) for h in opted_out)
            and not any(campaign.same(host,row.get('host',email.split('@')[-1]))
            and row.get('stopped') not in (None,'agreement_candidate') for email,row in ledger.items())}


def verify(site,state=STATE,at=None):
    at=at or dt.datetime.now(dt.timezone.utc)
    state=Path(state)
    try:
        if not isinstance(site,dict):return denied()
        reference=site.get('order_reference','')
        if not isinstance(reference,str) or not re.fullmatch(r'[A-Za-z0-9-]{1,25}',reference):return denied()
        for key in ('operator_approval','partner_creative_approval','release_approved','eligibility_approved','inventory_reserved','us_provider'):
            if site.get(key) is not True:return denied()
        if (site.get('held') is not False or type(site.get('affiliate')) is not bool
            or site.get('inventory_slot')!='providers-directory-panel'
            or not isinstance(site.get('provider'),str) or not site['provider'].strip()
            or 'fella' in site['provider'].lower()
            or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',str(site.get('slug','')))
            or site['slug'] in ('rates','example-provider')):return denied()
        start=dt.datetime.fromisoformat(site['starts_at'].replace('Z','+00:00'))
        end=dt.datetime.fromisoformat(site['ends_at'].replace('Z','+00:00'))
        if end-start!=dt.timedelta(hours=720) or not start<=at<end:return denied()
        # State already exists for real orders. Never create a parallel custody store.
        if state.is_symlink() or not state.is_dir() or (state/'run.lock').is_symlink():return denied()
        with (state/'run.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            orders=read(state/'orders.json')
            order=orders[reference]
            if (order['order_id']!=reference or order.get('provider')!=site['provider']
                or order.get('slug')!=site['slug'] or canonical(order.get('site_order'))!=canonical(site)
                or 'fella' in order['provider_host'].lower()):return denied()
            digest=hashlib.sha256(canonical(site)).hexdigest()
            approval=read(state/'site-approvals.json')[reference]
            if (approval.get('actor')!='Izaiah Tilton' or not approval.get('discord_message_id')
                or approval.get('decision')!='APPROVE SPONSOR SITE ORDER '+digest
                or not approval.get('evidence_path') or not re.fullmatch('[0-9a-f]{64}',str(approval.get('evidence_sha256','')))):
                return denied()
            evidence=Path(approval['evidence_path'])
            if evidence.is_symlink() or hashlib.sha256(evidence.read_bytes()).hexdigest()!=approval['evidence_sha256']:return denied()
            # Fresh eligibility and sticky reply/suppression checks precede the GETs.
            if order['provider_host'] not in eligible_hosts(state,at):return denied()
            for key,other in orders.items():
                if key!=reference and other.get('invoice_id')==order.get('invoice_id'):return denied()
            observed=paypal.poll_order(paypal.PayPalClient(),order,at,state)
            if (observed.get('payment_verified') is not True or observed.get('status')!='paid'
                or observed.get('order_id')!=reference or observed.get('provider_host')!=order['provider_host']
                or observed.get('provider')!=site['provider'] or observed.get('slug')!=site['slug']
                or observed.get('order_sha256')!=paypal.approval_digest(order)
                or not observed.get('transaction_ids')):return denied()
            tokens=set(observed['transaction_ids'])
            # The existing campaign owns durable allocation. This read-only
            # consumer cannot reserve an unseen payment or create another ledger.
            if tokens!=set(order.get('allocated_transaction_ids',[])):return denied()
            if any(tokens.intersection(other.get('allocated_transaction_ids',[]))
                   for key,other in orders.items() if key!=reference):return denied()
            # Never cache a positive result or mutate an allocation/payment record.
            return {'payment_verified':True,'source':'paypal_authenticated_invoice_reporting_observer',
                    'order_reference':reference,'site_order_sha256':digest,
                    'transaction_ids':observed['transaction_ids'], 'observed_at':observed['observed_at'],
                    'reporting_refreshed_at':observed['reporting_refreshed_at']}
    except (RuntimeError,OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError,sqlite3.Error):
        return denied() # No arbitrary exceptions, raw response, payer or credentials.


def main():
    try:
        raw=sys.stdin.buffer.read(65537)
        site=json.loads(raw,object_pairs_hook=unique) if len(raw)<=65536 else None
        result=verify(site)
        if result.get('payment_verified') is True:
            result['request_sha256']=hashlib.sha256(raw).hexdigest()
    except (ValueError,TypeError):result=denied()
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
