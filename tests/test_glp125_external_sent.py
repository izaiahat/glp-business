"""Owning regression for record-only out-of-band SENT receiving (offline)."""
import base64
import copy
import hashlib
import json
import sqlite3
import tempfile
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from inbox_control_plane import control_plane as cp


def test_external_sent_receiving_binds_original_event_without_faking_send_history():
    # One actual bug: the monitor has no receiver without a local draft.
    assert hasattr(cp, 'observe_glp125_external_sent'), 'missing record-only external-SENT receiver'
    from inbox_control_plane import external_sent as es
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / 'monitor.sqlite3'
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.executescript(cp.SCHEMA_SQL)
        conn.execute("INSERT INTO mailboxes(mailbox_id,venture,company,canonical_address,aliases_json,groups_json,provider,adapter,auth_reference,owner,lifecycle,poll_seconds,freshness_sla_seconds,inbound_surface,routing_policy,dmarc_domains_json,registry_hash,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (es.MAILBOX,'glp_partners','fixture','ops@glpclinicscout.com','[]','[]','gmail_api_dwd','gmail_api_dwd_readonly','fixture','fixture','active',300,600,'fixture','fixture','[]','fixture','now'))
        source = EmailMessage()
        source['From'] = es.TO
        source['To'] = es.SENDER
        source['Message-ID'] = '<original@example.invalid>'
        source.set_content('fixture inbound')
        raw_path = root / 'source.eml'
        raw_path.write_bytes(source.as_bytes())
        source_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        conn.execute("INSERT INTO inbox_events(event_id,mailbox_id,provider_message_id,provider_thread_id,rfc_message_id,sender,sender_domain,recipients_json,subject,received_at,raw_path,raw_sha256,body_sha256,labels_json,parse_defects_json,ingested_at,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (es.EVENT,es.MAILBOX,es.THREAD,es.THREAD,source['Message-ID'],es.TO,'nuimagemedical.com',json.dumps([es.SENDER]),'fixture','now',str(raw_path),source_sha,'fixture','[]','[]','now','ingested'))
        conn.commit()
        conn.close()
        payload = {'schema':'external-sent-observation.v1','event_id':es.EVENT,'mailbox_id':es.MAILBOX,'source_message_id':es.THREAD,'thread_id':es.THREAD,'sent_message_id':es.SENT,'source_raw_sha256':source_sha,'sent_raw_sha256':'fixture','body_sha256':es.BODY_SHA,'approval':{'message_id':5098910},'delivery_proven':False,'provider_mutations':0,'outbound_send':False}
        with mock.patch.object(es, 'collect_evidence', return_value=payload), mock.patch.object(es, 'SOURCE_SHA', source_sha), mock.patch.object(cp, 'Ledger', wraps=cp.Ledger) as ledger:
            # The CLI must bypass Ledger constructor, registry sync and migrations.
            ledger.side_effect = AssertionError('constructor is not record-only')
            assert cp.observe_glp125_external_sent(db, apply=False)['recorded'] is False
        # Exercise the real append-only audit writer; no fixture Ledger ctor.
        def snapshot():
            with sqlite3.connect(db) as c:
                return {r[0]:c.execute('SELECT * FROM '+r[0]).fetchall() for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'")}
        before = snapshot()
        with mock.patch.object(es, 'collect_evidence', return_value=payload), mock.patch.object(es, 'SOURCE_SHA', source_sha):
            first = cp.observe_glp125_external_sent(db, apply=True)
            assert first['recorded'] and not first['idempotent_replay']
            after = snapshot()
            assert len(after['audit_events']) == 1
            assert after['audit_events'][0][1:4] == ('external_sent_observed','inbox_event',es.EVENT)
            for table in before:
                if table != 'audit_events':
                    assert after[table] == before[table], table
            assert cp.observe_glp125_external_sent(db, apply=True)['idempotent_replay']
            assert snapshot() == after
            # A changed receipt conflicts; it is never a second audit or SEND.
            changed = copy.deepcopy(payload)
            changed['sent_raw_sha256'] = 'changed'
            with mock.patch.object(es, 'collect_evidence', return_value=changed):
                try:
                    cp.observe_glp125_external_sent(db, apply=True)
                    assert False, 'conflicting receipt accepted'
                except ValueError as error:
                    assert str(error) == 'external_sent_observation_conflict'
            assert snapshot() == after
        # Wrong/unknown source identity must refuse even when evidence was collected.
        for field, value in [('mailbox_id','unknown'),('provider_thread_id','wrong'),('raw_sha256','wrong')]:
            with sqlite3.connect(db) as c:
                old = c.execute('SELECT '+field+' FROM inbox_events WHERE event_id=?',(es.EVENT,)).fetchone()[0]
                c.execute('UPDATE inbox_events SET '+field+'=? WHERE event_id=?',(value,es.EVENT))
            with mock.patch.object(es, 'collect_evidence', return_value=payload), mock.patch.object(es, 'SOURCE_SHA', source_sha):
                try:
                    cp.observe_glp125_external_sent(db, apply=True)
                    assert False, field
                except ValueError as error:
                    assert str(error) == 'external_sent_source_binding_mismatch'
            with sqlite3.connect(db) as c:
                c.execute('UPDATE inbox_events SET '+field+'=? WHERE event_id=?',(old,es.EVENT))
        assert snapshot() == after


def test_external_sent_real_verifiers_refuse_provider_or_authority_substitution():
    from inbox_control_plane import external_sent as es
    assert hasattr(es, 'verify_sent_row'), 'provider verification must be independently exercised'
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        body = 'fixture approved body\n'
        mail = EmailMessage()
        mail['From'] = es.SENDER
        mail['To'] = es.TO
        mail['Subject'] = 'fixture reply'
        mail['In-Reply-To'] = '<original@example.invalid>'
        mail.set_content(body)
        wire = mail.as_bytes()
        sent_row = {'id':es.SENT,'threadId':es.THREAD,'labelIds':['SENT'],'internalDate':'1791810332000','raw':base64.urlsafe_b64encode(wire).decode()}
        queue = {'subject':'fixture reply'}
        source = {'rfc_message_id':'<original@example.invalid>'}
        candidate = b'Subject: fixture reply\n\n' + body.encode()
        with mock.patch.object(es, 'SENT_RAW_SHA', es.digest(wire)), mock.patch.object(es, 'BODY_SHA', es.digest(body.encode())):
            es.verify_sent_row(sent_row, queue, source, candidate)
            for field, value in [('id','unknown'),('threadId','wrong'),('labelIds',[]),('internalDate','0'),('raw',base64.urlsafe_b64encode(b'wrong').decode())]:
                changed = dict(sent_row, **{field:value})
                try:
                    es.verify_sent_row(changed, queue, source, candidate)
                    assert False, field
                except ValueError as error:
                    assert str(error).startswith('external_sent_provider_')
            with mock.patch.object(es, 'BODY_SHA', 'mismatched'):
                try:
                    es.verify_sent_row(sent_row, queue, source, candidate)
                    assert False, 'wrong body accepted'
                except ValueError as error:
                    assert str(error) == 'external_sent_provider_body_mismatch'
        # Authenticated custody is the fixed SessionDB, not a report declaration.
        session_db = root / 'sessions.sqlite3'
        with sqlite3.connect(session_db) as c:
            c.executescript('CREATE TABLE sessions(id TEXT PRIMARY KEY,parent_session_id TEXT);CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,timestamp REAL,content TEXT,tool_calls TEXT,tool_call_id TEXT,tool_name TEXT);')
            c.executemany('INSERT INTO sessions VALUES(?,?)', [(es.OPERATOR_SESSION,None),(es.S1_SESSION,es.OPERATOR_SESSION)])
            c.executemany('INSERT INTO messages(id,session_id,role,timestamp,content) VALUES(?,?,?,?,?)', [(5098910,es.OPERATOR_SESSION,'user',1.0,'fixture approval'),(5137413,es.S1_SESSION,'user',2.0,'fixture word steering')])
            calls = [{'id':'fixture-call','function':{'name':'terminal','arguments':json.dumps({'workdir':str(es.BUSINESS),'command':'python3 queue_nu_reply.py'})}}]
            c.execute('INSERT INTO messages(id,session_id,role,tool_calls) VALUES(?,?,?,?)',(5138937,es.S1_SESSION,'assistant',json.dumps(calls)))
            original_queue = {'artifact_sha256':es.ARTIFACT_SHA,'body_sha256':es.BODY_SHA,'thread_id':es.THREAD,'sender':es.SENDER,'to':es.TO}
            c.execute('INSERT INTO messages(id,session_id,role,tool_call_id,tool_name,content) VALUES(?,?,?,?,?,?)',(5138938,es.S1_SESSION,'tool','fixture-call','terminal',json.dumps({'exit_code':0,'output':json.dumps(original_queue)})))
        bindings = ((5098910,es.OPERATOR_SESSION,1.0,es.digest(b'fixture approval')),(5137413,es.S1_SESSION,2.0,es.digest(b'fixture word steering')))
        with mock.patch.object(es, 'SESSION_DB', session_db), mock.patch.object(es, 'AUTHORITY_BINDINGS', bindings):
            assert es.authenticate_authority()['proposal_is_operator_approval'] is False
            for field, value, old in [('role','assistant','user'),('content','forged approval','fixture approval'),('session_id','unknown',es.OPERATOR_SESSION)]:
                with sqlite3.connect(session_db) as c:
                    c.execute('UPDATE messages SET '+field+'=? WHERE id=5098910',(value,))
                try:
                    es.authenticate_authority()
                    assert False, field
                except ValueError as error:
                    assert str(error) == 'external_sent_retained_authority_mismatch'
                with sqlite3.connect(session_db) as c:
                    c.execute('UPDATE messages SET '+field+'=? WHERE id=5098910',(old,))
            with sqlite3.connect(session_db) as c:
                c.execute("UPDATE messages SET tool_call_id='unknown' WHERE id=5138938")
            try:
                es.authenticate_authority()
                assert False, 'unmatched S1 receipt accepted'
            except ValueError as error:
                assert str(error) == 'external_sent_s1_tool_binding_mismatch'
