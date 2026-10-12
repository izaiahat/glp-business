#!/usr/bin/env python3
"""One exact authorized in-thread reply. No cold-send or approval override."""
import base64
import datetime as dt
import fcntl
import hashlib
import json
import sys
from email.parser import BytesParser
from email.policy import default
from email.utils import parseaddr
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import glp_sponsor_campaign as c
from inbox_control_plane.outbound_style_guard import assert_raw_rfc822_dash_free

LANE = c.ROOT/'reports/ops/fable-plan-2-20261009/lanes/S1'
QUEUE = LANE/'GLP125-QUEUE.json'
JOURNAL = LANE/'GLP125-SEND.json'
TO = 'andreas@nuimagemedical.com'
THREAD = '1a0e883504e54e66'
ARTIFACT_SHA = '7df84f73964c4ff851131d2ce2bfd1ca571995235cf740f9a73deeb7b7b5bf32'
BODY_SHA = 'c7004388dcab9180ce146d9825bd9d67f992d06ffdfd5fa5c889d8c0eb292f39'


def require_schedule():
    at=c.now()
    if at < dt.datetime.fromisoformat('2026-10-12T06:00:00-07:00'):
        raise RuntimeError('nuimage_not_before_Monday_0600_PT')
    if at >= dt.datetime.fromisoformat('2026-10-12T17:00:00-07:00'):
        raise RuntimeError('nuimage_exact_Monday_window_expired_do_not_defer')
    c.require_sponsor_send_window()


def raw_message(row):
    return base64.urlsafe_b64decode(row['raw']+'===')


def parsed_body(raw):
    message = BytesParser(policy=default).parsebytes(raw)
    body = message.get_body(preferencelist=('plain',))
    return message, body.get_content().replace('\r\n','\n') if body else ''


def suppression():
    local = c.read_local()
    if not local['suppression']['complete'] or local['faults']:
        raise RuntimeError('complete_suppression_required')
    if any(any(v in json.dumps(row).lower() for v in (TO,'nuimagemedical.com','nuimage.com'))
           for row in local['suppression']['entries']):
        raise RuntimeError('nuimage_suppressed')


def run(send=False):
    require_schedule()  # Before auth, readback, draft mutation or a claim.
    with c.inbox_poll_boundary(alert=send,job='Nu Image queued reply'), (c.STATE/'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        queue = c.load(QUEUE)
        if (queue.get('artifact_sha256') != ARTIFACT_SHA or queue.get('body_sha256') != BODY_SHA
            or queue.get('thread_id') != THREAD or queue.get('to') != TO
            or c.digest(LANE/'GLP125-NuImage-reply.txt') != ARTIFACT_SHA):
            raise RuntimeError('exact_approved_NuImage_queue_changed')
        suppression()
        client = c.service('send')
        aliases=client.service.users().settings().sendAs().list(userId='me').execute(num_retries=0).get('sendAs') or []
        alias=next((row for row in aliases if str(row.get('sendAsEmail','')).lower()==c.SENDER),None)
        if not alias or alias.get('verificationStatus')!='accepted':
            raise RuntimeError('partners_send_as_alias_not_verified')
        source = client.get_message(THREAD,format='raw')
        original,_ = parsed_body(raw_message(source))
        if (source['threadId'] != THREAD or parseaddr(str(original['From']))[1].lower() != TO
            or str(original['Message-ID']).strip() != queue['source_rfc_message_id']):
            raise RuntimeError('same_address_inbound_thread_required')
        matches=[]
        for item in client.get_thread(THREAD).get('messages',[]):
            row=client.get_message(item['id'],format='raw')
            message,body=parsed_body(raw_message(row))
            labels=row.get('labelIds',[])
            if 'SENT' in labels and parseaddr(str(message['To']))[1].lower()==TO:
                # Any already-sent copy of this sponsor proposal stops replay,
                # including an older punctuation spelling, not only this hash.
                if hashlib.sha256(body.encode()).hexdigest()==BODY_SHA or ('Our proposed sponsor pilot is $750' in body and 'Hi Andreas,' in body):
                    matches.append(item['id'])
            elif 'DRAFT' not in labels and int(row.get('internalDate','0'))>int(source.get('internalDate','0')):
                # Any new inbound invalidates the approved response context,
                # including a bare STOP or an unclassified change of mind.
                raise RuntimeError('later_inbound_requires_review')
        if matches:
            return dict(status='ALREADY_SENT_DO_NOT_RESEND',message_ids=matches,sends=0)
        if JOURNAL.exists():
            raise RuntimeError('existing_or_uncertain_send_claim_read_only_reconciliation_required')
        draft=client.service.users().drafts().get(userId='me',id=queue['draft_id'],format='raw').execute(num_retries=0)
        row=draft['message'];wire=raw_message(row);message,body=parsed_body(wire)
        if (row['threadId']!=THREAD or 'DRAFT' not in row.get('labelIds',[])
            or hashlib.sha256(body.encode()).hexdigest()!=BODY_SHA
            or parseaddr(str(message['From']))[1].lower()!=c.SENDER
            or parseaddr(str(message['To']))[1].lower()!=TO
            or str(message['Subject'])!=queue['subject']
            or str(message['In-Reply-To']).strip()!=queue['source_rfc_message_id']
            or message.is_multipart() or message.get_content_type() != 'text/plain'
            or message.defects
            or any(len(message.get_all(key, [])) != 1 for key in ('From','To','Subject','In-Reply-To'))
            or any(key.lower() in ('cc','bcc','resent-to','resent-cc','resent-bcc','resent-from','sender') for key in message.keys())
            or list(message.iter_attachments())):
            raise RuntimeError('draft_exact_bytes_thread_or_identity_changed')
        assert_raw_rfc822_dash_free(wire,context='GLP125-exact-approved-draft')
        suppression()  # Authentication and thread reads have completed.
        require_schedule()  # Final installed transport window, after setup.
        result=dict(status='PREFLIGHT_ONLY',draft_id=queue['draft_id'],thread_id=THREAD,sends=0)
        if not send:return result
        c.save(JOURNAL,dict(result,status='SUBMISSION_STARTED',claimed_at=c.stamp(),body_sha256=BODY_SHA))
        require_schedule()
        # Gmail supports supplying the validated MIME bytes in drafts.send;
        # sending only its mutable ID would race a concurrent draft edit.
        sent=client.service.users().drafts().send(userId='me',body={'id':queue['draft_id'],
            'message':{'raw':base64.urlsafe_b64encode(wire).decode(), 'threadId':THREAD}}).execute(num_retries=0)
        c.save(JOURNAL,dict(result,status='SUBMITTED_READBACK_PENDING',message_id=sent['id'],sends=1))
        got=client.get_message(sent['id'],format='raw');verified,body=parsed_body(raw_message(got))
        if ('SENT' not in got.get('labelIds',[]) or got['threadId']!=THREAD
            or hashlib.sha256(body.encode()).hexdigest()!=BODY_SHA
            or parseaddr(str(verified['From']))[1].lower()!=c.SENDER
            or parseaddr(str(verified['To']))[1].lower()!=TO
            or str(verified['In-Reply-To']).strip()!=queue['source_rfc_message_id']):
            raise RuntimeError('submitted_readback_mismatch_never_resend')
        result.update(status='SENT_RAW_VERIFIED_NOT_DELIVERY_PROOF',message_id=sent['id'],sends=1,verified_at=c.stamp())
        c.save(JOURNAL,result)
        return result


if __name__=='__main__':
    print(json.dumps(run(send='--send' in sys.argv),indent=2))
