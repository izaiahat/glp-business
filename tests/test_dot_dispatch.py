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

    def test_external_handoff_reconciliation_paginates_without_losing_newer_replies(self):
        calls = []
        reply = {}
        newer = dict(id='1560000000000000002', channel_id=d.DOTS['rook']['channel'],
                     author={'id':'unrelated'}, content='unrelated')
        def api(method, route, payload=None):
            calls.append((method, route))
            if method == 'POST':
                self.message['content'] = payload['content']
                return dict(self.message)
            if route.endswith('messages?limit=100'):
                return [newer]
            if route.endswith('messages?limit=100&before=1560000000000000002'):
                return [reply, self.message]
            return reply if route.endswith('/1560000000000000000') else self.message
        result = self.dispatch(api)
        ticket = Path(result['ticket_dir'])
        row = json.loads((ticket/'TICKET.json').read_text())
        reply = dict(id='1560000000000000000', channel_id=row['channel_id'], type=19,
            author={'id':d.DOTS['rook']['user']}, attachments=[],
            message_reference=dict(message_id=row['request_id'], channel_id=row['channel_id'], guild_id=d.GUILD),
            content=f"<@{d.SENDER}> RESULT {row['ticket']}\nEvidence. Delivery is authorized direct fallback, NOT native-correlated: no request_id.")
        with patch.object(d.time, 'time', return_value=row['sent_at']+21601):
            outcome = d.expire(ticket, reason='authenticated external reconciliation', owner='coordinator',
                api=api, probe=lambda _: {'status':'request_expired'},
                handoff_reply_id=reply['id'], handoff_content_sha256=d.digest(reply['content']))
        self.assertEqual(outcome['state'], 'expired')
        audit = json.loads((ticket/'EXPIRY.json').read_text())
        self.assertEqual(audit['history_messages_checked'], 3)
        self.assertEqual(audit['external_handoff'], reply)
        self.assertFalse((ticket/'COMPLETION.json').exists())
        self.assertEqual(sum(method == 'POST' for method, _ in calls), 1)

    def test_exact_hash_context_is_audited_but_never_hides_another_terminal(self):
        replies = []
        def api(method, route, payload=None):
            if method == 'POST':
                self.message['content'] = payload['content']
                return self.message
            if '?limit=100' in route:
                return [self.message] + replies
            return next((m for m in replies if route.endswith('/'+m['id'])), self.message)
        result = self.dispatch(api)
        ticket = Path(result['ticket_dir'])
        row = json.loads((ticket/'TICKET.json').read_text())
        handoff = dict(id='1560000000000000000', channel_id=row['channel_id'], type=19,
            author={'id':d.DOTS['rook']['user']}, attachments=[],
            message_reference=dict(message_id=row['request_id'], channel_id=row['channel_id'], guild_id=d.GUILD),
            content=f"<@{d.SENDER}> RESULT {row['ticket']}\nEvidence. Delivery is authorized direct fallback, NOT native-correlated: no request_id.")
        context = dict(id='1560000000000000001', channel_id=row['channel_id'], type=0,
            author={'id':d.DOTS['rook']['user']}, attachments=[], message_reference=None,
            content=f"<@{d.SENDER}> Please reconcile {row['ticket']}; no replay.")
        kwargs = dict(reason='exact external handoff and reviewed status', owner='coordinator', api=api,
            probe=lambda _: {'status':'request_expired'}, handoff_reply_id=handoff['id'],
            handoff_content_sha256=d.digest(handoff['content']), handoff_context_id=context['id'],
            handoff_context_sha256=d.digest(context['content']))
        with patch.object(d.time, 'time', return_value=row['sent_at']+21601):
            for bad in (dict(context, content=context['content']+' drift'),
                        dict(context, author={'id':'foreign'}),
                        dict(context, message_reference=handoff['message_reference']),
                        dict(context, content=f"<@{d.SENDER}> BLOCKED {row['ticket']} other terminal"),
                        dict(context, content=f"<@{d.SENDER}> \nBLOCKED {row['ticket']} other terminal")):
                replies[:] = [handoff, bad]
                tested = dict(kwargs, handoff_context_sha256=d.digest(bad['content'])) if 'BLOCKED' in bad['content'] else kwargs
                with self.assertRaises(ValueError):
                    d.expire(ticket, **tested)
                self.assertFalse((ticket/'EXPIRY.json').exists())
            replies[:] = [handoff, context, dict(context, id='1560000000000000002')]
            with self.assertRaises(ValueError):
                d.expire(ticket, **kwargs)
            replies[:] = [handoff, context]
            outcome = d.expire(ticket, **kwargs)
            self.assertEqual(d.expire(ticket, **kwargs), outcome)
        audit = json.loads((ticket/'EXPIRY.json').read_text())
        self.assertEqual(audit['external_context'], context)
        self.assertEqual(audit['external_context_content_sha256'], kwargs['handoff_context_sha256'])
        self.assertEqual(audit['matching_reply_count'], 2)
        self.assertFalse((ticket/'COMPLETION.json').exists())

    def test_second_exact_correlation_status_retained_without_hiding_other_reply(self):
        replies = []
        calls = []
        def api(method, route, payload=None):
            calls.append((method, route))
            if method == 'POST':
                self.message['content'] = payload['content']
                return self.message
            if '?limit=100' in route:
                return [self.message] + replies
            return next((m for m in replies if route.endswith('/'+m['id'])), self.message)
        result = self.dispatch(api)
        ticket = Path(result['ticket_dir'])
        row = json.loads((ticket/'TICKET.json').read_text())
        handoff = dict(id='1560000000000000000', channel_id=row['channel_id'], type=19,
            author={'id':d.DOTS['rook']['user']}, attachments=[],
            message_reference=dict(message_id=row['request_id'], channel_id=row['channel_id'], guild_id=d.GUILD),
            content=f"<@{d.SENDER}> RESULT {row['ticket']}\nEvidence. Delivery is authorized direct fallback, NOT native-correlated: no request_id.")
        context = dict(id='1560000000000000002', channel_id=row['channel_id'], type=0,
            author={'id':d.DOTS['rook']['user']}, attachments=[], message_reference=None,
            content=f"<@{d.SENDER}> Please reconcile {row['ticket']}; no replay.")
        reconciliation = dict(context, id='1560000000000000001',
            content=f"<@{d.SENDER}> RV472 reconciliation facts: {row['correlation']}. No native reply or replay.")
        kwargs = dict(reason='reviewed exact terminal and both nonterminal statuses', owner='coordinator', api=api,
            probe=lambda _: {'status':'request_expired'}, handoff_reply_id=handoff['id'],
            handoff_content_sha256=d.digest(handoff['content']), handoff_context_id=context['id'],
            handoff_context_sha256=d.digest(context['content']),
            handoff_reconciliation_id=reconciliation['id'],
            handoff_reconciliation_sha256=d.digest(reconciliation['content']))
        with patch.object(d.time, 'time', return_value=row['sent_at']+21601):
            replies[:] = [handoff, context, reconciliation]
            before = (ticket/'TICKET.json').read_bytes()
            with self.assertRaisesRegex(ValueError, 'handoff_binding_refused'):
                d.expire(ticket, **{k:v for k,v in kwargs.items() if not k.startswith('handoff_reconciliation')})
            for field, value in [('author', {'id':'foreign'}), ('channel_id', 'foreign'),
                    ('guild_id', 'foreign'), ('type', 19), ('webhook_id', 'hook'),
                    ('attachments', [{'id':'attachment'}]), ('message_reference', handoff['message_reference']),
                    ('content', reconciliation['content']+' drift'),
                    ('content', f"<@{d.SENDER}> \nBLOCKED {row['correlation']} other terminal"),
                    ('content', f"<@{d.SENDER}> reconciliation foreign correlation")]:
                bad = dict(reconciliation, **{field:value})
                replies[:] = [handoff, context, bad]
                tested = dict(kwargs)
                if field == 'content' and not value.endswith(' drift'):
                    tested['handoff_reconciliation_sha256'] = d.digest(value)
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    d.expire(ticket, **tested)
                self.assertEqual((ticket/'TICKET.json').read_bytes(), before)
                self.assertFalse((ticket/'EXPIRY.json').exists())
            replies[:] = [handoff, context, reconciliation, dict(reconciliation,id='1560000000000000003')]
            with self.assertRaisesRegex(ValueError, 'handoff_binding_refused'):
                d.expire(ticket, **kwargs)
            replies[:] = [handoff, context, reconciliation]
            outcome = d.expire(ticket, **kwargs)
            self.assertEqual(outcome['state'], 'expired')
            self.assertEqual(d.expire(ticket, **kwargs), outcome)
            with self.assertRaisesRegex(ValueError, 'audit_handoff_conflict'):
                d.expire(ticket, **dict(kwargs, handoff_reconciliation_sha256='0'*64))
        audit = json.loads((ticket/'EXPIRY.json').read_text())
        self.assertEqual(audit['external_reconciliation'], reconciliation)
        self.assertEqual(audit['external_reconciliation_content_sha256'], kwargs['handoff_reconciliation_sha256'])
        self.assertEqual(audit['matching_reply_count'], 3)
        self.assertFalse((ticket/'COMPLETION.json').exists())
        self.assertEqual(sum(method == 'POST' for method, _ in calls), 1)

    def test_second_context_cli_passes_exact_bindings_and_requires_primary(self):
        import contextlib
        import io
        import sys
        argv = ['dot_dispatch.py', '--expire', str(self.root/'ticket'), '--reason', 'reviewed',
                '--owner', 'coordinator', '--external-handoff-id', '111',
                '--external-handoff-sha256', 'a'*64, '--external-context-id', '113',
                '--external-context-sha256', 'b'*64, '--external-reconciliation-id', '112',
                '--external-reconciliation-sha256', 'c'*64]
        with patch.object(sys, 'argv', argv), patch.object(d, 'expire', return_value={'state':'expired'}) as expiry, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d.main(), 0)
        self.assertEqual(expiry.call_args.kwargs['handoff_reconciliation_id'], '112')
        self.assertEqual(expiry.call_args.kwargs['handoff_reconciliation_sha256'], 'c'*64)
        for params in ({'handoff_reconciliation_id':'112'},
                {'handoff_reconciliation_sha256':'c'*64},
                {'handoff_reconciliation_id':'112', 'handoff_reconciliation_sha256':'c'*64}):
            with self.subTest(params=params), self.assertRaisesRegex(ValueError,'context_binding_refused'):
                d.expire(self.root/'missing', reason='reviewed', owner='coordinator', **params)
        self.assertFalse((self.root/'missing').exists())

    def test_discord_api_allows_only_numeric_read_only_before_cursor(self):
        import post_discord_message as poster
        from unittest.mock import MagicMock
        channel = d.DOTS['rook']['channel']
        route = f'/channels/{channel}/messages?limit=100&before=1560000000000000002'
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'[]'
        with patch.object(poster, 'load_token', return_value='offline-test-not-a-credential'), \
             patch.object(d.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(d.discord_api('GET', route), [])
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://discord.com/api/v10'+route)
        with patch.object(poster, 'load_token', side_effect=AssertionError('refuse before credential read')):
            for method, bad in [('POST',route), ('GET',route+'&after=100'),
                    ('GET',route.replace('before=1560000000000000002','before=foreign')),
                    ('GET',route.replace('limit=100','limit=1000'))]:
                with self.assertRaisesRegex(ValueError, 'discord_route_refused'):
                    d.discord_api(method, bad)

    def test_history_cursor_and_page_bound_fail_closed(self):
        channel = d.DOTS['rook']['channel']
        message = lambda value: dict(id=str(value), channel_id=channel)
        for pages in (
                [[message(200)], [message(200)]],
                [[message(200)], [dict(message(150), channel_id='foreign')]],
                [[message(200)], [message(150), message(150)]],
                [[message(200)], []]):
            with self.subTest(pages=pages):
                remaining = iter(pages)
                with self.assertRaises(ValueError):
                    d.expiry_history(channel, '100', lambda *_: next(remaining))
        count = 0
        def unbounded(method, route):
            nonlocal count
            self.assertEqual(method, 'GET')
            count += 1
            return [message(1000-count)]
        with self.assertRaisesRegex(ValueError, 'does_not_cover_request'):
            d.expiry_history(channel, '100', unbounded)
        self.assertEqual(count, 20)

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


class NotificationMentionTests(unittest.TestCase):
    def assert_readback_refused(self, **changes):
        import contextlib
        import io
        import post_discord_message as poster
        user = d.DOTS['rook']['user']
        channel = d.DOTS['rook']['channel']
        content = '<@'+user+'> status'
        fetched = dict(id='123', channel_id=channel, content=content,
                       mentions=[{'id': user}], mention_everyone=False, mention_roles=[])
        fetched.update(changes)
        argv = ['poster', '--channel', channel, '--message', content, '--ping-user', user]
        with patch.object(sys, 'argv', argv), \
             patch.object(poster, 'send', return_value={'id': '123'}) as sender, \
             patch.object(poster, 'readback', return_value=fetched) as reader, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, 'exact readback failed'):
                poster.main()
            sender.assert_called_once_with(channel, content, ping_user=user)
            reader.assert_called_once_with(channel, '123')
            self.assertEqual(output.getvalue(), '')

    def test_cli_refuses_intended_plus_foreign_user_readback(self):
        self.assert_readback_refused(mentions=[{'id': d.DOTS['rook']['user']},
                                               {'id': d.DOTS['finch']['user']}])

    def test_cli_refuses_everyone_readback(self):
        self.assert_readback_refused(mention_everyone=True)

    def test_cli_refuses_role_readback(self):
        self.assert_readback_refused(mention_roles=['1234567890123456'])

    def test_cli_refuses_malformed_user_readback(self):
        for mentions in (None, {}, 'not-a-list', [{'id': d.DOTS['rook']['user']}, {}],
                         [{'id': d.DOTS['rook']['user']}, None], [{'id': 123}]):
            with self.subTest(mentions=mentions):
                self.assert_readback_refused(mentions=mentions)

    def test_cli_passes_explicit_ping_and_refuses_missing_structured_readback(self):
        import contextlib
        import io
        import post_discord_message as poster
        user = d.DOTS['rook']['user']
        channel = d.DOTS['rook']['channel']
        content = '<@'+user+'> status'
        argv = ['post_discord_message.py', '--channel', channel, '--message', content, '--ping-user', user]
        fetched = dict(id='123', channel_id=channel, content=content, mentions=[{'id':user}])
        for mentions in ([{'id':user}], []):
            with patch.object(sys, 'argv', argv), \
                 patch.object(poster, 'send', return_value={'id':'123'}) as sender, \
                 patch.object(poster, 'readback', return_value=dict(fetched, mentions=mentions)), \
                 contextlib.redirect_stdout(io.StringIO()):
                if mentions:
                    self.assertEqual(poster.main(), 0)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'exact readback failed'):
                        poster.main()
                sender.assert_called_once_with(channel, content, ping_user=user)

    def test_only_explicit_single_user_can_be_pinged_and_default_remains_suppressed(self):
        import post_discord_message as poster
        user = d.DOTS['rook']['user']
        content = '<@'+user+'> GLP-359 status'
        requests = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return b'{"id":"123"}'
        def open_request(request, **_):
            requests.append(json.loads(request.data))
            return Response()
        with patch.object(poster, 'load_token', return_value='offline-test-not-a-credential'), \
             patch.object(poster.urllib.request, 'urlopen', side_effect=open_request):
            poster.send(d.DOTS['rook']['channel'], content)
            poster.send(d.DOTS['rook']['channel'], content, ping_user=user)
        self.assertEqual(requests[0]['allowed_mentions'], {'parse': []})
        self.assertEqual(requests[1]['allowed_mentions'],
                         {'parse': [], 'users':[user], 'replied_user':False})
        with patch.object(poster, 'load_token', side_effect=AssertionError('must refuse before credential read')):
            for invalid in ('everyone', '123', user+','+d.DOTS['finch']['user'], [user]):
                with self.assertRaises(ValueError):
                    poster.send(d.DOTS['rook']['channel'], content, ping_user=invalid)
            with self.assertRaises(ValueError):
                poster.send(d.DOTS['rook']['channel'], 'no actual mention', ping_user=user)


if __name__ == '__main__':
    unittest.main()
