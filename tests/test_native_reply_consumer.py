"""Offline native-custody consumer checks; no credentials or real Discord."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import receive_one_discord_message as receiver


class NativeConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.scope = receiver.Scope('discord-100', 'rook.reply', '100', 'author', receiver.ROOK_BOT,
            1, str(self.root), 'issuer', 'audience', 'subject')
        self.data = {'id': '101', 'channel_id': receiver.CHANNEL, 'guild_id': receiver.GUILD,
            'type': 0, 'author': {'id': receiver.ROOK_BOT}, 'attachments': [],
            'content': 'external\n-# rook.reply · discord-100'}
        policy = {'issuer': 'issuer', 'audience': 'audience', 'subject': 'subject'}
        self.binding = {'id': 'discord-100', 'message_id': '100', 'state': 'confirmed',
            'policy': hashlib.sha256(json.dumps(policy, ensure_ascii=True, separators=(',', ':'), sort_keys=True).encode()).hexdigest(),
            'auth_method': 'RS256-authenticated-tools/call', 'subject': 'subject', 'auth_subject': 'subject',
            'auth_expires': time.time(), 'confirmed_at': time.time() - 3600,
            'reply_id': '101', 'reply_author': receiver.ROOK_BOT, 'guild_id': receiver.GUILD,
            'channel_id': receiver.CHANNEL, 'reply_kind': 'rook.reply',
            'reply_sha256': hashlib.sha256(self.data['content'].encode()).hexdigest()}
        self.write_binding()

    def tearDown(self):
        self.tmp.cleanup()

    def write_binding(self):
        db = sqlite3.connect(self.root / 'rook.sqlite3')
        db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('INSERT OR REPLACE INTO requests VALUES (?,?)', ('discord-100', json.dumps(self.binding)))
        db.commit(); db.close()
        (self.root / 'rook.sqlite3').chmod(0o600)

    def test_consumer_stages_once_and_owner_disposition_is_separate(self):
        import importlib.util
        spec = importlib.util.find_spec('native_reply_consumer')
        self.assertIsNotNone(spec, 'native-custody consumer missing')
        import native_reply_consumer as consumer
        registration = consumer.Registration(self.scope, 'parent-1', 'task-owner', 'inspect source')
        with consumer.Receipts(self.root) as receipts:
            result = consumer.collect(registration, receipts, reader=lambda channel, identity: self.data)
            self.assertEqual(result['status'], 'staged')
            self.assertEqual(result['parent'], 'parent-1')
            self.assertEqual(result['task_owner'], 'task-owner')
            self.assertEqual(result['content_sha256'], self.binding['reply_sha256'])
            duplicate = consumer.collect(registration, receipts, reader=lambda *_: self.fail('duplicate GET'))
            self.assertEqual(duplicate['status'], 'awaiting_coordinator_ack')
            with self.assertRaises(ValueError):
                receipts.record(result['receipt_id'], 'task-owner', 'received', 'notification read')
            receipts.record(result['receipt_id'], 'parent-1', 'received', 'notification read')
            with self.assertRaises(ValueError):
                receipts.record(result['receipt_id'], 'parent-1', 'applied', 'bad role')
            receipts.record(result['receipt_id'], 'task-owner', 'rejected', 'source-only verdict; runtime unknown')
            self.assertEqual(consumer.collect(registration, receipts, reader=lambda *_: self.fail('duplicate GET'))['status'], 'rejected')
        with consumer.Receipts(self.root) as receipts:
            self.assertEqual(receipts.get(result['receipt_id'])['status'], 'rejected')

    def test_registration_channel_and_identity_cannot_be_rebound(self):
        import native_reply_consumer as consumer
        config = {'parent': 'parent-1', 'receipt_state_dir': str(self.root), 'registrations': [{
            'correlation': 'discord-100', 'channel': '999', 'state_dir': str(self.root),
            'issuer': 'issuer', 'audience': 'audience', 'subject': 'subject',
            'task_owner': 'owner', 'action': 'inspect'}]}
        path = self.root / 'config.json'
        path.write_text(json.dumps(config)); path.chmod(0o600)
        with self.assertRaises(ValueError):
            consumer.load_registrations(path)
        config['registrations'][0]['channel'] = receiver.CHANNEL
        path.write_text(json.dumps(config))
        _, entries = consumer.load_registrations(path)
        with consumer.Receipts(self.root) as receipts:
            consumer.collect(entries[0], receipts, reader=lambda *_: self.data)
            config['parent'] = 'different-parent'
            path.write_text(json.dumps(config))
            _, changed = consumer.load_registrations(path)
            with self.assertRaises(ValueError):
                consumer.collect(changed[0], receipts, reader=lambda *_: self.fail('duplicate GET'))

    def test_refuses_tampering_attachments_and_unsafe_custody(self):
        import native_reply_consumer as consumer
        from copy import deepcopy
        registration = consumer.Registration(self.scope, 'parent-1', 'task-owner', 'inspect')
        with consumer.Receipts(self.root) as receipts:
            for delta in ({'attachments': [{'url': 'https://evil.invalid'}]},
                    {'content': 'forged\n-# rook.reply · discord-100'}, {'webhook_id': '1'},
                    {'author': {'id': '200'}}, {'channel_id': consumer.SHARED_CHANNEL},
                    {'guild_id': '200'}, {'id': '102'}, {'type': 19}):
                bad = deepcopy(self.data); bad.update(delta)
                with self.subTest(delta=delta), self.assertRaises(ValueError):
                    consumer.collect(registration, receipts, reader=lambda *_: bad)
            self.binding['attachment'] = {'url': 'https://evil.invalid'}
            self.write_binding()
            with self.assertRaises(ValueError):
                consumer.collect(registration, receipts, reader=lambda *_: self.fail('attachment GET'))
            (self.root / 'rook.sqlite3').chmod(0o644)
            with self.assertRaises(ValueError):
                consumer.collect(registration, receipts, reader=lambda *_: self.fail('unsafe GET'))

    def test_finch_exact_channel_effect_and_full_wire_sha(self):
        import native_reply_consumer as consumer
        import rook_mcp_events as native
        from dataclasses import replace
        identity = native.FinchIdentity(consumer.FINCH_CHANNEL, consumer.SHARED_CHANNEL,
            consumer.FINCH_BOT, Path('/home/ubuntu/.hermes/secrets/dot-bots/finch.token'),
            (native.OPERATOR, native.BOT, native.ROOK_BOT))
        for channel in identity.channels:
            scope = replace(self.scope, reply_kind='finch.reply', origin_id=identity.bot,
                finch=identity, finch_channel=channel)
            data = dict(self.data, channel_id=channel, author={'id': identity.bot},
                content='source-only\n-# finch.reply · discord-100')
            policy = {'issuer': 'issuer', 'audience': 'audience', 'subject': 'subject', 'identity': identity.policy_fields()}
            self.binding.update(reply_kind='finch.reply', channel_id=channel, reply_author=identity.bot,
                policy=hashlib.sha256(consumer.canonical(policy).encode()).hexdigest(), attachment=None,
                reply_sha256=hashlib.sha256(data['content'].encode()).hexdigest())
            self.binding['reply_effect_sha256'] = hashlib.sha256(consumer.canonical([
                receiver.GUILD, channel, self.binding['reply_sha256'], None]).encode()).hexdigest()
            self.write_binding()
            registration = consumer.Registration(scope, 'parent-1', 'Rook', 'source-only review')
            with consumer.Receipts(self.root) as receipts:
                received = consumer.collect(registration, receipts, reader=lambda *_: data)
                self.assertEqual(received['channel_id'], channel)
                self.assertFalse(received['operator_approval'])
                self.assertEqual(received['trust'], consumer.TRUST)
                self.assertEqual(consumer.collect(registration, receipts, replay=True)['receipt_id'], received['receipt_id'])
                with self.assertRaises((BlockingIOError, OSError)):
                    consumer.Receipts(self.root)

    def test_pending_native_row_never_reads_discord(self):
        import native_reply_consumer as consumer
        self.binding['state'] = 'ready'; self.write_binding()
        with consumer.Receipts(self.root) as receipts:
            result = consumer.collect(consumer.Registration(self.scope, 'parent-1', 'owner', 'inspect'),
                receipts, reader=lambda *_: self.fail('unconfirmed API GET'))
            self.assertEqual(result['status'], 'waiting_native_confirmation')

    def test_parent_hard_deadline_terminates_blocked_readback_and_preserves_replay(self):
        import native_reply_consumer as consumer
        import subprocess
        # Exercise the documented Linux parent launch, not a mocked clock or timer.
        # Both stalls occur inside the real run()/collect() path with network denied.
        script = Path(consumer.__file__).resolve()
        for stall in ('readback', 'durable_before_stdout'):
            with self.subTest(stall=stall):
                receipt_dir = self.root / stall
                receipt_dir.mkdir(mode=0o700)
                config = self.root / (stall + '.json')
                config.write_text(json.dumps({'parent': 'parent-1', 'receipt_state_dir': str(receipt_dir),
                    'registrations': [{'correlation': 'discord-100', 'channel': receiver.CHANNEL,
                        'state_dir': str(self.root), 'issuer': 'issuer', 'audience': 'audience',
                        'subject': 'subject', 'task_owner': 'owner-1', 'action': 'inspect source'}]}))
                config.chmod(0o600)
                marker = receipt_dir / 'entered-stall'
                code = f'''
import sys, time, json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, {str(script.parent)!r})
import native_reply_consumer as c, rook_mcp_events as n
config = Path({str(config)!r})
data = json.loads({json.dumps(self.data)!r})
def block():
    Path({str(marker)!r}).write_text('blocking')
    time.sleep(1.25)
def read(method, path, **kwargs):
    assert method == 'GET' and path == '/channels/{receiver.CHANNEL}/messages/101'
    if {stall!r} == 'readback': block()
    return data
put = c.Receipts.put
def stage(self, value):
    put(self, value)
    if {stall!r} == 'durable_before_stdout': block()
with patch.object(n, 'discord_request', side_effect=read), \\
     patch.object(n, 'load_rook_token', side_effect=AssertionError('credential forbidden')), \\
     patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \\
     patch.object(c.Receipts, 'put', stage):
    c.main(['--config', str(config), '--max-wait-seconds', '1'])
'''
                start = time.monotonic()
                # Missing prefix reproduces the previously documented unbounded launch.
                prefix = getattr(consumer, 'PARENT_DEADLINE_PREFIX', ())
                command = [*prefix, *(['1s'] if prefix else []), sys.executable, '-B', '-c', code]
                process = subprocess.run(command, capture_output=True, text=True, timeout=5)
                elapsed = time.monotonic() - start
                self.assertTrue(marker.exists(), 'did not reach blocking run/collect seam')
                self.assertEqual(process.returncode, 137, process.stderr)
                self.assertLess(elapsed, 1.5, 'hard deadline failed to terminate readback')
                self.assertEqual(process.stdout, '', 'must not claim completion after budget')
                # The process is reaped; lock released and committed staging survives SIGKILL.
                _, entries = consumer.load_registrations(config)
                with consumer.Receipts(receipt_dir) as receipts:
                    old = receipts.get(entries[0].receipt_id)
                    if stall == 'readback':
                        self.assertIsNone(old)
                    else:
                        self.assertEqual(old['status'], 'staged')
                        replay = consumer.collect(entries[0], receipts, replay=True,
                            reader=lambda *_: self.fail('replay must not GET or restage'))
                        self.assertTrue(replay['replay'])
                        self.assertEqual(replay['content_sha256'], self.binding['reply_sha256'])
                        self.assertEqual(consumer.collect(entries[0], receipts)['status'],
                            'awaiting_coordinator_ack')
                        with self.assertRaises(ValueError):
                            receipts.record(old['receipt_id'], 'owner-1', 'applied', 'not received')
                        receipts.record(old['receipt_id'], 'parent-1', 'received', 'offline replay read')
                        receipts.record(old['receipt_id'], 'owner-1', 'rejected', 'offline timeout recovery')

    def test_registered_recovery_does_not_relax_gateway_freshness(self):
        self.assertFalse(receiver.verified_reply_binding(self.data, self.scope, receiver.ROOK_BOT))
        check = getattr(receiver, 'verified_registered_reply_binding', None)
        self.assertIsNotNone(check, 'native replay verification missing')
        self.assertTrue(check(self.data, self.scope, receiver.ROOK_BOT))
        bad = dict(self.data, content=self.data['content'] + 'tamper')
        self.assertFalse(check(bad, self.scope, receiver.ROOK_BOT))


if __name__ == '__main__':
    unittest.main()
