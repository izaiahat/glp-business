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
