"""Sponsor replies inside the canonical inbox transaction and notification outbox.

No poller, queue, draft, outbound mail or placement writer. Any matched inbound
stops the whole enrolled provider; OOO holds require human review, never resume.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from email.parser import BytesParser
from email.policy import default

LEDGER_PATH=Path('/home/ubuntu/business/reports/ops/sponsor-machine-20261003/runtime/ledger.json')
DECISIONS='1524254375453786294'
OPS='1487300132054765629'
OWN={'partners@glpclinicscout.com','ops@glpclinicscout.com','izaiah@glpclinicscout.com'}


def classify(body,subject,headers,received_at):
    text=(subject+'\n'+body).lower()
    if re.search(r'\b(?:unsubscribe|stop|opt out|remove me|do not contact)\b',body,re.I):kind='opt_out'
    elif re.search(r'delivery status notification|undeliverable|delivery failure|mail delivery failed',text):kind='bounce'
    elif re.search(r'out of office|on vacation|away from (?:the )?office|automatic reply',text) or headers.get('Auto-Submitted','').lower()=='auto-replied':kind='out_of_office'
    elif re.search(r'\b(?:agree|accept|proceed|book the|let.s do)\b',body,re.I) and not re.search(r'\b(?:not|no|don.t|decline)\b',body,re.I):kind='agreement_candidate'
    else:kind='human_reply_or_review'
    result={'kind':kind,'auto_resume':False}
    if kind=='out_of_office':
        date=re.search(r'\b(20\d{2}-\d{2}-\d{2})\b',body)
        result['review_after']=date.group(1) if date else (dt.datetime.fromisoformat(received_at)+dt.timedelta(days=7)).date().isoformat()
    return result


def stage(store,*,event_id,mailbox,parsed,raw,thread_id,now,newest_body):
    if mailbox.get('mailbox_id') not in {'glp_ops_gmail','glp_izaiah_gmail'} or parsed.get('sender','').lower() in OWN:
        return None
    if not LEDGER_PATH.exists():return None
    # Malformed enrollment custody is an ingestion failure, not permission to
    # silently continue sending. The canonical cursor retries this same event.
    ledger=json.loads(LEDGER_PATH.read_text())
    sender=parsed.get('sender','').lower()
    matches=[]
    for address,row in ledger.items():
        if not row.get('sent') and not row.get('claimed'):continue
        host=row.get('host',address.split('@')[-1])
        domain=sender.split('@')[-1]
        exact_sender=sender==address or domain==host
        exact_thread=bool(thread_id and any(s.get('thread_id')==thread_id for s in row.get('sent',[])))
        # Thread IDs are scoped to a mailbox. Only ops owns sponsor sends.
        if exact_sender or (mailbox['mailbox_id']=='glp_ops_gmail' and exact_thread):matches.append((address,host))
    if not matches:return None
    headers=BytesParser(policy=default).parsebytes(raw)
    disposition=classify(newest_body,parsed.get('subject',''),headers,parsed['received_at'])
    receipt={**disposition,'provider_hosts':sorted({host for _,host in matches}),
             'enrolled_addresses':sorted({address for address,_ in matches}),'event_id':event_id,
             'received_at':parsed['received_at'],'raw_sha256':hashlib.sha256(raw).hexdigest(),
             'payment_verified':False,'order_confirmed':False,'activation_allowed':False}
    action_id='sponsor_'+hashlib.sha256(event_id.encode()).hexdigest()[:24]
    store.conn.execute("""INSERT OR IGNORE INTO mailbox_actions(action_id,event_id,action,status,provider_receipt_json,created_at,updated_at)
                         VALUES(?,?,'sponsor_reply_stop','verified',?,?,?)""",(action_id,event_id,json.dumps(receipt,sort_keys=True),now,now))
    purpose='sponsor_agreement_candidate' if disposition['kind']=='agreement_candidate' else 'sponsor_reply_stop'
    # No body/contact copied to a new channel; the existing private event is
    # the evidence. Agreement is merely a review candidate, not a payable order.
    payload=(f"Sponsor reply: {disposition['kind']}\nProvider: {', '.join(receipt['provider_hosts'])}\n"
             "All automatic follow-ups stopped. No order, payment or placement approved.\n"
             f"Review the private canonical event `{event_id}`.")
    store.queue_notification(event_id,purpose,DECISIONS if purpose=='sponsor_agreement_candidate' else OPS,
                             'sponsor-reply:'+event_id,payload,transactional=True)
    return receipt
