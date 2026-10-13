"""Offline dispatch/consume contract; Discord and native adapter writes denied."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import dot_dispatch as d


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.root = Path(self.tmp.name) / 'tickets'
        self.message = {'id': '1559999999999999999', 'channel_id': d.DOTS['rook']['channel'],
                        'content': '', 'author': {'id': d.SENDER},
                        'mentions': [{'id': d.DOTS['rook']['user']}], 'attachments': []}

    def tearDown(self):
        self.tmp.cleanup()

    def dispatch(self, api):
        return d.dispatch('rook', 'GLP-348', 'parser fixtures; no release', root=self.root,
                          api=api, ready=lambda _: {'expires': 9999999999})

    def test_mentions_registration_and_repeat_reads_before_no_resend(self):
        calls = []
        def api(method, route, payload=None):
            calls.append((method, route, payload))
            if method == 'POST':
                self.message['content'] = payload['content']
                self.assertEqual(payload['allowed_mentions'], {'parse': [], 'users': [d.DOTS['rook']['user']], 'replied_user': False})
            return dict(self.message)
        result = self.dispatch(api)
        ticket = Path(result['ticket_dir'])
        native = json.loads((ticket / 'NATIVE-CONFIG.json').read_text())
        cfg = json.loads((ticket / 'CONFIG.json').read_text())
        self.assertEqual(cfg['issue'], 'GLP-348')
        self.assertEqual(cfg['dot'], 'rook')
        self.assertEqual(native['registrations'][0]['correlation'], 'discord-' + self.message['id'])
        self.assertEqual(native['registrations'][0]['result_key'], result['ticket'])
        self.assertEqual((ticket / 'CONFIG.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(ticket.stat().st_mode & 0o777, 0o700)
        self.dispatch(api)
        self.assertEqual([c[0] for c in calls].count('POST'), 1)
        self.assertEqual(calls[-1][0], 'GET')

    def test_uncertain_send_is_never_repeated(self):
        def api(*_):
            raise TimeoutError('unknown POST outcome')
        with self.assertRaises(TimeoutError):
            self.dispatch(api)
        with self.assertRaisesRegex(ValueError, 'reconcile'):
            self.dispatch(lambda *_: self.fail('must not resend'))
        state = json.loads(next(self.root.glob('*/TICKET.json')).read_text())
        self.assertEqual(state['state'], 'uncertain')

    def test_readback_failure_retains_id_and_can_reconcile(self):
        count = 0
        def api(method, route, payload=None):
            nonlocal count
            if method == 'POST':
                count += 1
                self.message['content'] = payload['content']
                return dict(self.message)
            return dict(self.message, mentions=[])
        with self.assertRaisesRegex(ValueError, 'readback'):
            self.dispatch(api)
        self.dispatch(lambda *_: dict(self.message))
        self.assertEqual(count, 1)

    def test_lengths_bridge_and_preflight_fail_before_post(self):
        for brief in ['x' * 2001, '\U0001f642' * 1000, 'TRUE-BRIDGE FABLE REVIEW REQUEST']:
            with self.assertRaises(ValueError):
                d.dispatch('rook', 'GLP-348', brief, root=self.root, api=lambda *_: self.fail('POST'), ready=lambda _: {})
        with self.assertRaises(ValueError):
            d.dispatch('rook', 'GLP-348', 'brief', root=self.root, api=lambda *_: self.fail('POST'), ready=lambda _: (_ for _ in ()).throw(ValueError('not ready')))

    def test_replies_are_atomic_terminal_external_data_and_idempotent(self):
        ticket = self.root / 'test'
        ticket.mkdir(parents=True, mode=0o700)
        state = {'ticket': 'test', 'issue': 'GLP-348', 'correlation': 'discord-100',
                 'state': 'sent', 'dot': 'rook', 'sent_at': 10}
        d.atomic_json(ticket / 'TICKET.json', state)
        value = {'status': 'staged', 'correlation': 'discord-100', 'text': 'RESULT test PR https://example.test/1',
                 'outcome': 'result', 'author_id': d.DOTS['rook']['user'], 'reply_id': '101',
                 'trust': d.TRUST, 'operator_approval': False, 'staged_at': 20}
        self.assertFalse(d.publish_reply(ticket, state, {'status': 'deadline', 'results': [{'status': 'waiting_native_confirmation'}]}))
        self.assertFalse((ticket / 'REPLY.md').exists())
        self.assertTrue(d.publish_reply(ticket, state, {'results': [value]}))
        original = (ticket / 'REPLY.md').read_bytes()
        self.assertIn(b'UNTRUSTED_EXTERNAL_DATA', original)
        self.assertTrue(d.publish_reply(ticket, state, {'results': [value]}))
        self.assertEqual(original, (ticket / 'REPLY.md').read_bytes())
        with self.assertRaises(ValueError):
            d.publish_reply(ticket, state, {'results': [dict(value, text='altered')]})

    def test_audited_no_reply_expiry_releases_guard_and_retains_late_reply(self):
        def api(method, route, payload=None):
            if method == 'POST':
                self.message['content'] = payload['content']
            if '?limit=100' in route:
                return [dict(self.message)]
            return dict(self.message)
        result = self.dispatch(api)
        ticket = Path(result['ticket_dir'])
        sent = json.loads((ticket / 'TICKET.json').read_text())['sent_at']
        with patch.object(d.time, 'time', return_value=sent + 21601):
            expired = d.expire(ticket, reason='D1 absent native intake after timeout',
                owner='orchestrator', api=api, probe=lambda _: {'status': 'absent'})
        self.assertEqual(expired['state'], 'expired')
        audit = json.loads((ticket / 'EXPIRY.json').read_text())
        self.assertEqual(audit['timeout_seconds'], 21600)
        self.assertEqual(audit['reason'], 'D1 absent native intake after timeout')
        self.assertEqual(audit['recorded_by'], 'orchestrator')
        self.assertFalse((ticket / 'REPLY.md').exists())
        d.dispatch('rook', 'GLP-359', 'next evidence read', root=self.root,
            api=api, ready=lambda _: {'expires': 9999999999})
        late = {'status': 'staged', 'correlation': expired['correlation'],
            'text': 'RESULT ' + result['ticket'] + ' retained evidence', 'outcome': 'result',
            'author_id': d.DOTS['rook']['user'], 'reply_id': '1560000000000000000',
            'trust': d.TRUST, 'operator_approval': False}
        with patch.object(d.subprocess, 'run', return_value=type('Proc', (), {
                'returncode': 0, 'stdout': json.dumps({'results': [late]}), 'stderr': ''})()):
            d.consume(self.root)
        final = json.loads((ticket / 'TICKET.json').read_text())
        self.assertEqual(final['state'], 'expired')
        self.assertTrue(final['late'])
        self.assertIn('LATE_REPLY_TO_EXPIRED_TICKET', (ticket / 'REPLY.md').read_text())
        self.assertTrue(json.loads((ticket / 'COMPLETION.json').read_text())['late'])

    def test_exact_hash_external_terminal_alternative_is_never_native_completion(self):
        def api(method, route, payload=None):
            if method == 'POST': self.message['content'] = payload['content']
            if '?limit=100' in route: return [self.message, reply]
            return reply if route.endswith('/1560000000000000000') else self.message
        result = self.dispatch(api)
        ticket = Path(result['ticket_dir'])
        row = json.loads((ticket/'TICKET.json').read_text())
        reply = dict(id='1560000000000000000', channel_id=row['channel_id'], type=19,
            author={'id':d.DOTS['rook']['user']}, attachments=[],
            message_reference=dict(message_id=row['request_id'], channel_id=row['channel_id'], guild_id=d.GUILD),
            content=f"<@{d.SENDER}> RESULT {row['ticket']}\nOriginal evidence. Delivery is authorized direct fallback, NOT native-correlated: no request_id.")
        fingerprint=d.digest(reply['content'])
        import copy
        cases = ('author','channel','guild','id','ref_message','ref_channel','ref_guild','ticket_suffix',
            'hash','webhook','attachment','duplicate','history','get_drift','native_confirmed',
            'native_sending','native_uncertain','early','short_timeout','staged','audit_conflict')
        for case in cases:
            bad=copy.deepcopy(reply); history=[self.message,bad]; get=bad
            if case=='author':bad['author']['id']='foreign'
            if case=='channel':bad['channel_id']='foreign'
            if case=='guild':bad['guild_id']='foreign'
            if case=='id':bad['id']='foreign'
            if case.startswith('ref_'):bad['message_reference'][{'ref_message':'message_id','ref_channel':'channel_id','ref_guild':'guild_id'}[case]]='foreign'
            if case=='ticket_suffix':bad['content']=bad['content'].replace(row['ticket'],row['ticket']+'suffix')
            if case=='hash':bad['content']+=' changed'
            if case=='webhook':bad['webhook_id']='hook'
            if case=='attachment':bad['attachments']=[{'id':'102'}]
            if case=='duplicate':history.append(dict(bad,id='1570000000000000000'))
            if case=='history':history=[bad]
            if case=='get_drift':get=copy.deepcopy(bad);get['content']+=' drift'
            if case=='staged':d.atomic_text(ticket/'REPLY.md','staged')
            if case=='audit_conflict':d.atomic_json(ticket/'EXPIRY.json',dict(schema='wrong'))
            def bad_api(method,route,payload=None):
                self.assertEqual(method,'GET')
                return history if '?limit=100' in route else get if route.endswith('/'+reply['id']) else self.message
            probes=iter([{'status':'request_expired'},{'status':case.removeprefix('native_')}])
            probe=(lambda _:next(probes)) if case.startswith('native_') else (lambda _:{'status':'request_expired'})
            clock=row['sent_at']+(1 if case=='early' else 21601)
            pre=(ticket/'TICKET.json').read_bytes()
            with self.subTest(external_negative=case),patch.object(d.time,'time',return_value=clock),self.assertRaises((ValueError,KeyError)):
                d.expire(ticket,reason='offline alternative',owner='parent',api=bad_api,probe=probe,
                    handoff_reply_id=reply['id'],handoff_content_sha256=fingerprint,
                    timeout_seconds=1 if case=='short_timeout' else 21600)
            self.assertEqual((ticket/'TICKET.json').read_bytes(),pre)
            for name in ('REPLY.md','EXPIRY.json'):
                (ticket/name).unlink(missing_ok=True)
        with patch.object(d.time, 'time', return_value=row['sent_at']+21601):
            expired = d.expire(ticket, reason='offline alternative', owner='parent', api=api,
                probe=lambda _:{'status':'request_expired'}, handoff_reply_id=reply['id'],
                handoff_content_sha256=fingerprint)
            self.assertEqual(d.expire(ticket,reason='offline alternative',owner='parent',api=lambda *_:self.fail('replay GET'),
                probe=lambda _:self.fail('replay probe'),handoff_reply_id=reply['id'],handoff_content_sha256=fingerprint),expired)
            with self.assertRaises(ValueError):
                d.expire(ticket,reason='offline alternative',owner='parent',handoff_reply_id=reply['id'],handoff_content_sha256='0'*64)
        audit = json.loads((ticket/'EXPIRY.json').read_text())
        self.assertEqual(expired['state'], 'expired')
        self.assertEqual(audit['external_handoff'], reply)
        self.assertEqual(audit['external_handoff_content_sha256'], d.digest(reply['content']))
        self.assertFalse((ticket/'REPLY.md').exists())
        self.assertFalse((ticket/'COMPLETION.json').exists())

    def test_exact_receiving_migration_preserves_preimages_and_is_replayable(self):
        import native_reply_consumer as n
        import rook_mcp_events as r
        import sqlite3
        ticket = self.root/'finch-glp-359-fixture'
        ticket.mkdir(parents=True, mode=0o700)
        self.root.chmod(0o700)
        state_dir = Path(self.tmp.name)/'native'; state_dir.mkdir(mode=0o700)
        receipts = ticket/'receipts'; receipts.mkdir(mode=0o700)
        config = dict(parent='parent', receipt_state_dir=str(receipts), registrations=[dict(
            correlation='discord-100', channel=n.FINCH_CHANNEL, state_dir=str(state_dir),
            issuer=d.ISSUER, audience=d.DOTS['finch']['audience'], subject=d.SUBJECT,
            task_owner='owner', action='inspect', result_key=ticket.name)])
        d.atomic_json(ticket/'NATIVE-CONFIG.json', config)
        _, entries = n.load_registrations(ticket/'NATIVE-CONFIG.json'); registration=entries[0]
        payload=b'assistant evidence copy\n'
        metadata=dict(id='102', filename='evidence.txt', content_type='text/plain', size=len(payload),
            url=f'https://cdn.discordapp.com/attachments/{n.FINCH_CHANNEL}/102/evidence.txt')
        attachment=r.finch_attachment_custody(metadata,n.FINCH_CHANNEL,hashlib.sha256(payload).hexdigest())
        reply=dict(id='101',channel_id=n.FINCH_CHANNEL,type=0,author={'id':n.FINCH_BOT},attachments=[metadata],
            content=f'RESULT {ticket.name} evidence\n-# finch.reply · discord-100')
        policy=dict(issuer=d.ISSUER,audience=d.DOTS['finch']['audience'],subject=d.SUBJECT,
            identity=registration.scope.finch.policy_fields())
        row=dict(id='discord-100',message_id='100',subject=d.SUBJECT,policy=d.digest(d.canonical(policy)),
            state='confirmed',reply_id='101',reply_sha256=d.digest(reply['content']),attachment=attachment,
            guild_id=d.GUILD,channel_id=n.FINCH_CHANNEL,reply_author=n.FINCH_BOT,reply_kind='finch.reply',
            auth_method='RS256-authenticated-tools/call',auth_subject=d.SUBJECT,confirmed_at=1,auth_expires=10)
        row['reply_effect_sha256']=d.digest(d.canonical([d.GUILD,n.FINCH_CHANNEL,row['reply_sha256'],
            {k:attachment[k] for k in ('filename','content_type','byte_length','sha256')}]))
        db=sqlite3.connect(state_dir/'rook.sqlite3');db.execute('CREATE TABLE requests(id TEXT PRIMARY KEY,value TEXT)')
        db.execute('INSERT INTO requests VALUES (?,?)',('discord-100',json.dumps(row)));db.commit();db.close()
        (state_dir/'rook.sqlite3').chmod(0o600)
        pins=d.source_pins(); old_pins=dict(pins,**{'native_reply_consumer.py':'0'*64})
        state=dict(ticket=ticket.name,issue='GLP-359',dot='finch',request_id='100',correlation='discord-100',
            state='sent',exact_readback=True,parent='parent',task_owner='owner',channel_id=n.FINCH_CHANNEL,
            source_pins=old_pins,sent_at=20)
        d.atomic_json(ticket/'TICKET.json',state);d.atomic_json(ticket/'CONFIG.json',d.fleet_config(ticket,state))
        pre={name:(ticket/name).read_text() for name in ('TICKET.json','NATIVE-CONFIG.json')}
        plan=dict(schema='DOT_RECEIVING_MIGRATION_V1',ticket=ticket.name,
            ticket_preimage_sha256=d.digest(pre['TICKET.json']),native_config_preimage_sha256=d.digest(pre['NATIVE-CONFIG.json']),
            native_row_sha256=d.digest(d.canonical(row)),old_source_pins=old_pins,new_source_pins=pins,
            reply_id='101',content_sha256=row['reply_sha256'],terminal_attachment={k:attachment[k] for k in
                ('id','filename','content_type','byte_length','sha256')},recorded_by='parent',reason='reviewed test')
        with patch.dict(d.DOTS['finch'],state_dir=str(state_dir)):
            for key, bad in [('ticket','foreign'),('old_source_pins',{}),('new_source_pins',{}),
                    ('ticket_preimage_sha256','0'*64),('native_config_preimage_sha256','0'*64),
                    ('native_row_sha256','0'*64),('reply_id','999'),('content_sha256','0'*64),
                    ('terminal_attachment',dict(plan['terminal_attachment'],sha256='0'*64))]:
                with self.subTest(migration_plan=key), self.assertRaises(ValueError):
                    d.migrate_receiving(ticket,dict(plan,**{key:bad}),reader=lambda *_:reply)
                self.assertEqual({name:(ticket/name).read_text() for name in pre},pre)
                self.assertFalse((ticket/'RECEIVING-MIGRATION.json').exists())
            for name in ('REPLY.md','COMPLETION.json','EXPIRY.json'):
                d.atomic_text(ticket/name,'present')
                with self.subTest(existing=name),self.assertRaises(ValueError):
                    d.migrate_receiving(ticket,plan,reader=lambda *_:reply)
                (ticket/name).unlink()
            with n.Receipts(receipts):
                with self.assertRaises((BlockingIOError,OSError)):
                    d.migrate_receiving(ticket,plan,reader=lambda *_:reply)
            with d.locked(self.root):
                with self.assertRaises((BlockingIOError,OSError)):
                    d.migrate_receiving(ticket,plan,reader=lambda *_:reply)
            with n.Receipts(receipts) as store:
                store.put(dict(receipt_id='conflicting',correlation='other',outcome='result'))
            with self.assertRaisesRegex(ValueError,'receipt_conflict'):
                d.migrate_receiving(ticket,plan,reader=lambda *_:reply)
            db=sqlite3.connect(receipts/'reply-consumers.sqlite3');db.execute('DELETE FROM receipts');db.commit();db.close()
            for key,bad in [('state','sending'),('state','uncertain'),('state','ready'),('policy','0'*64),
                    ('auth_subject','foreign'),('auth_method','forged'),('auth_expires',0),('id','discord-999')]:
                changed=dict(row,**{key:bad})
                db=sqlite3.connect(state_dir/'rook.sqlite3');db.execute('UPDATE requests SET value=?',(json.dumps(changed),));db.commit();db.close()
                with self.subTest(native=key,value=bad),self.assertRaises(ValueError):
                    d.migrate_receiving(ticket,dict(plan,native_row_sha256=d.digest(d.canonical(changed))),reader=lambda *_:reply)
                self.assertEqual({name:(ticket/name).read_text() for name in pre},pre)
                self.assertFalse((ticket/'RECEIVING-MIGRATION.json').exists())
            db=sqlite3.connect(state_dir/'rook.sqlite3');db.execute('UPDATE requests SET value=?',(json.dumps(row),));db.commit();db.close()
            real_atomic=d.atomic_json
            def crash_after_audit(path,value):
                real_atomic(path,value)
                if Path(path).name=='RECEIVING-MIGRATION.json':raise OSError('simulated crash after audit fsync')
            with patch.object(d,'atomic_json',side_effect=crash_after_audit),self.assertRaises(OSError):
                d.migrate_receiving(ticket,plan,reader=lambda *_:reply)
            self.assertEqual({name:(ticket/name).read_text() for name in pre},pre)
            result=d.migrate_receiving(ticket,plan,reader=lambda *_:reply)
            self.assertEqual(result['status'],'receiving_migrated')
            audit=json.loads((ticket/'RECEIVING-MIGRATION.json').read_text())
            self.assertEqual(audit['preimages'],pre)
            self.assertEqual(json.loads((ticket/'TICKET.json').read_text())['sent_at'],20)
            migrated=n.load_registrations(ticket/'NATIVE-CONFIG.json')[1][0]
            self.assertEqual(migrated.receipt_id,registration.receipt_id)
            self.assertNotEqual(migrated.registration_sha256,registration.registration_sha256)
            # Interrupted config-before-state migration resumes only this audited transition.
            d.atomic_text(ticket/'TICKET.json',pre['TICKET.json'])
            d.migrate_receiving(ticket,plan,reader=lambda *_:reply)
            self.assertEqual(d.migrate_receiving(ticket,plan,reader=lambda *_:reply)['status'],'receiving_migrated')
            with n.Receipts(receipts) as store:
                value=n.collect(migrated,store,reader=lambda *_:reply,attachment_reader=lambda *a,**k:payload)
            self.assertTrue(d.publish_reply(ticket,json.loads((ticket/'TICKET.json').read_text()),{'results':[value]}))
            self.assertIn('RESULT '+ticket.name,(ticket/'REPLY.md').read_text())
            with self.assertRaises(ValueError):d.migrate_receiving(ticket,plan,reader=lambda *_:reply)

    def test_live_ticket_still_blocks_and_cannot_expire_early(self):
        def api(method, route, payload=None):
            if method == 'POST':
                self.message['content'] = payload['content']
            return dict(self.message)
        result = self.dispatch(api)
        ticket = Path(result['ticket_dir'])
        with self.assertRaisesRegex(ValueError, 'timeout_not_elapsed'):
            d.expire(ticket, reason='not old enough', owner='orchestrator',
                api=lambda *_: self.fail('must refuse before history read'),
                probe=lambda _: self.fail('must refuse before native read'))
        with self.assertRaisesRegex(ValueError, 'one_open_ticket_per_dot'):
            d.dispatch('rook', 'GLP-359', 'new brief', root=self.root,
                api=lambda *_: self.fail('no POST'), ready=lambda _: {})
        sent = json.loads((ticket / 'TICKET.json').read_text())['sent_at']
        with patch.object(d.time, 'time', return_value=sent + 21601):
            with self.assertRaisesRegex(ValueError, 'native_confirmation_or_live_effect'):
                d.expire(ticket, reason='must not cancel effect', owner='orchestrator',
                    api=api, probe=lambda _: {'status': 'confirmed'})
        self.assertEqual(json.loads((ticket / 'TICKET.json').read_text())['state'], 'sent')

    def test_live_consumer_source_pins_do_not_follow_candidate_adapter(self):
        live = Path(self.tmp.name) / 'live'
        (live / 'scripts').mkdir(parents=True)
        for name in ('native_reply_consumer.py', 'receive_one_discord_message.py', 'rook_mcp_events.py'):
            (live / 'scripts' / name).write_text('frozen installed bytes')
        with patch.object(d, 'ROOT', live):
            self.assertEqual(d.consumer_dir(), live / 'scripts')
            self.assertEqual(d.source_pins()['rook_mcp_events.py'], hashlib.sha256(
                b'frozen installed bytes').hexdigest())

    def test_fleet_envelope_migration_preserves_native_registration(self):
        ticket = self.root / 'test'
        ticket.mkdir(parents=True)
        state = dict(ticket='test', issue='GLP-348', dot='rook', request_id='100', correlation='discord-100')
        native = dict(parent='parent', receipt_state_dir=str(ticket / 'receipts'), registrations=[
            dict(correlation='discord-100', result_key='test')])
        d.atomic_json(ticket / 'CONFIG.json', native)
        d.migrate_contract(ticket, state)
        self.assertEqual(json.loads((ticket / 'NATIVE-CONFIG.json').read_text()), native)
        self.assertEqual(json.loads((ticket / 'CONFIG.json').read_text()), d.fleet_config(ticket, state))
        d.migrate_contract(ticket, state)
        self.assertEqual(json.loads((ticket / 'NATIVE-CONFIG.json').read_text()), native)

    def test_non_result_and_foreign_correlation_never_publish(self):
        ticket = self.root / 'test'; ticket.mkdir(parents=True)
        state = {'ticket': 'test', 'correlation': 'discord-100', 'dot': 'finch'}
        for value in [{'status': 'non_result_reply'}, {'status': 'staged', 'correlation': 'discord-999'}]:
            with self.assertRaises(ValueError):
                d.publish_reply(ticket, state, {'results': [value]})
        self.assertFalse((ticket / 'REPLY.md').exists())


if __name__ == '__main__':
    unittest.main()
