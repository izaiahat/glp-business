#!/usr/bin/env python3
"""Allowlisted Gmail mark-read action that runs only after durable action evidence.

This module can remove only the UNREAD label. It has no archive, delete, draft, or
send capability. The canonical inbox event and Gmail message ID are bound through
a query-only SQLite read before any provider mutation.
"""
from __future__ import annotations
import json, os, sqlite3, time
from pathlib import Path
from typing import Any, Callable

ALLOWED_MAILBOXES=frozenset({'glp_ops_gmail'})
ALLOWED_RESULTS=frozenset({'PASS','BLOCKED'})
ALLOWED_EVENT_STATES=frozenset({'resolved_externally','held','archived'})

def load_binding(db_path: Path, event_id: str) -> dict[str,str]:
    con=sqlite3.connect(f'file:{db_path}?mode=ro',uri=True)
    con.row_factory=sqlite3.Row
    try:
        con.execute('PRAGMA query_only=ON')
        row=con.execute('''SELECT e.event_id,e.mailbox_id,e.provider_message_id,e.state,m.provider
                           FROM inbox_events e JOIN mailboxes m USING(mailbox_id)
                           WHERE e.event_id=?''',(event_id,)).fetchone()
    finally:
        con.close()
    if not row: raise RuntimeError('mark_read_event_not_found')
    if row['mailbox_id'] not in ALLOWED_MAILBOXES: raise RuntimeError('mark_read_mailbox_not_allowlisted')
    if str(row['provider']).lower() not in {'gmail','gmail_api','gmail_api_dwd'}: raise RuntimeError('mark_read_provider_not_gmail')
    if not row['provider_message_id']: raise RuntimeError('mark_read_provider_message_id_missing')
    if str(row['state']) not in ALLOWED_EVENT_STATES: raise RuntimeError('mark_read_event_disposition_not_terminal')
    return {k:str(row[k]) for k in ('event_id','mailbox_id','provider_message_id','state')}

def validate_action_receipt(path: Path,event_id: str) -> dict[str,Any]:
    if not path.is_file(): raise RuntimeError('mark_read_action_receipt_missing')
    if path.stat().st_mode & 0o077: raise RuntimeError('mark_read_action_receipt_permissions')
    doc=json.loads(path.read_text())
    if doc.get('event_id')!=event_id: raise RuntimeError('mark_read_action_receipt_event_mismatch')
    status=str(doc.get('status') or '')
    if status not in ALLOWED_RESULTS: raise RuntimeError('mark_read_action_receipt_status_not_allowlisted')
    if status=='PASS':
        ok=bool(doc.get('action_completed') or (doc.get('provider_readback_verified') and doc.get('sheet_readback_verified')))
        if not ok: raise RuntimeError('mark_read_action_not_verified')
    else:
        if doc.get('durable_blocker_verified') is not True: raise RuntimeError('mark_read_blocker_not_verified')
    for key in ('external_send','provider_program_mutation','archive','delete','provider_draft'):
        if doc.get(key) not in (None,False,0): raise RuntimeError(f'mark_read_unsafe_receipt:{key}')
    return doc

def _labels(gmail:Any,message_id:str) -> tuple[str,set[str]]:
    doc=gmail.users().messages().get(userId='me',id=message_id,format='minimal').execute()
    if str(doc.get('id') or '')!=message_id: raise RuntimeError('mark_read_provider_identity_mismatch')
    return str(doc['id']),set(doc.get('labelIds') or [])

def mark_read_after_action(*,gmail:Any,db_path:Path,event_id:str,action_receipt_path:Path,retries:int=3,sleep_fn:Callable[[float],None]=time.sleep) -> dict[str,Any]:
    """Remove only UNREAD after exact binding and durable action/blocker proof.

    Ambiguous modify failures are reconciled by GET before any retry. A message
    already lacking UNREAD is a successful zero-mutation idempotent replay.
    """
    if retries<1: raise ValueError('retries_must_be_positive')
    binding=load_binding(db_path,event_id)
    action=validate_action_receipt(action_receipt_path,event_id)
    mid=binding['provider_message_id']
    _,before=_labels(gmail,mid)
    if 'UNREAD' not in before:
        return {'status':'PASS','event_id':event_id,'provider_message_id':mid,'action_status':action['status'],'idempotent_replay':True,'modify_calls':0,'removed_labels':[],'before_labels':sorted(before),'after_labels':sorted(before),'only_unread_removed':True}
    last_error=None
    for attempt in range(1,retries+1):
        try:
            gmail.users().messages().modify(userId='me',id=mid,body={'removeLabelIds':['UNREAD'],'addLabelIds':[]}).execute()
        except Exception as exc:
            last_error=exc
            try:
                _,after=_labels(gmail,mid)
                if 'UNREAD' not in after:
                    expected=before-{'UNREAD'}
                    if after!=expected: raise RuntimeError('mark_read_other_labels_changed')
                    return {'status':'PASS','event_id':event_id,'provider_message_id':mid,'action_status':action['status'],'idempotent_replay':False,'ambiguous_modify_reconciled':True,'modify_calls':attempt,'removed_labels':['UNREAD'],'before_labels':sorted(before),'after_labels':sorted(after),'only_unread_removed':True}
            except RuntimeError:
                raise
            except Exception:
                pass
            if attempt<retries: sleep_fn(min(2**(attempt-1),4)); continue
            raise RuntimeError('mark_read_modify_failed_after_retries') from last_error
        _,after=_labels(gmail,mid)
        expected=before-{'UNREAD'}
        if after!=expected: raise RuntimeError('mark_read_postcondition_failed')
        return {'status':'PASS','event_id':event_id,'provider_message_id':mid,'action_status':action['status'],'idempotent_replay':False,'modify_calls':attempt,'removed_labels':['UNREAD'],'before_labels':sorted(before),'after_labels':sorted(after),'only_unread_removed':True}
    raise RuntimeError('mark_read_unreachable')

def save_receipt(path:Path,receipt:dict[str,Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(receipt,indent=2,sort_keys=True)+'\n')
    os.chmod(path,0o600)
