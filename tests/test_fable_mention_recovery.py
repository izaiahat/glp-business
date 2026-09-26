"""Offline sender and exact-bound recovery tests; real canonical authority guard."""
import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

bridge = load('mention_bridge', 'scripts/fable_bridge.py')
guard = load('mention_guard', 'ventures/boring-ai/holistic-machine/scripts/bridge_authority_guard.py')
poster = load('mention_poster', 'scripts/post_discord_message.py')
SOURCE = '1553357347328499764'
PREDECESSOR = '1553266002706309203'
SIGNER = '1529571092371476591'


class MentionSenderTests(unittest.TestCase):
    def test_generic_parse_empty_sender_refuses_review_before_credentials_or_post(self):
        # Today's exact transport failure: textual mention survives but parse=[]
        # yields no structured mention. This generic sender must not send reviews.
        text = f'<@{SIGNER}> TRUE-BRIDGE FABLE REVIEW REQUEST\nissue_id: fixture'
        with patch.object(poster, 'load_token') as token, patch.object(poster.urllib.request, 'urlopen') as post:
            post.return_value.__enter__.return_value.read.return_value = b'{"id":"1553357347328499764","mentions":[]}'
            with self.assertRaisesRegex(RuntimeError, 'registered_bridge_sender_required'):
                poster.send(bridge.CHANNEL, text)
        token.assert_not_called()
        post.assert_not_called()
        with patch.object(bridge, 'SELF_ID', SIGNER):
            message = dict(content=text, author={'id': bridge.KAEL_BOT_ID, 'bot': True}, mentions=[])
            self.assertIsNone(bridge.classify_message(message))
            message['mentions'] = [{'id': SIGNER}]
            self.assertEqual(bridge.classify_message(message)[0], 'review_request')


class RegisteredFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.issues = self.root / 'issues'
        self.artifacts = self.root / 'ops'
        self.artifacts.mkdir()
        self.artifact = self.artifacts / 'manifest.json'
        self.artifact.write_text('{"fixture":true}\n')
        self.digest = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        for obj, name, value in [(guard, 'BRIDGE_ISSUE_ROOT', self.issues),
                                  (guard, 'REVIEW_ARTIFACT_ROOT', self.artifacts)]:
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        p.start()
        self.addCleanup(p.stop)
        self.path, self.issue = guard.issue_request(subject_id='glp:GLP-42:fixture',
            tuple_sha256=self.digest, review_artifact_path=self.artifact)
        self.issue.update(predecessor_request_message_id=PREDECESSOR,
            predecessor_issue_id='bridge-issue-' + 'a' * 32,
            predecessor_reply_message_id='1553281139517423701',
            succession_reason='terminal_no_authority_decision')
        self.write_issue()
        self.message = dict(id=SOURCE, channel_id=bridge.CHANNEL, type=0,
            author={'id': bridge.KAEL_BOT_ID, 'bot': True}, mentions=[{'id': SIGNER}],
            content=self.issue['request_content'], timestamp=guard._utc_now(), edited_timestamp=None)
        p = patch.object(guard, '_fetch_discord_message', side_effect=lambda *args: copy.deepcopy(self.message))
        p.start()
        self.addCleanup(p.stop)

    def write_issue(self):
        self.issue['record_sha256'] = guard._record_hash(self.issue)
        self.path.write_text(json.dumps(self.issue))


class RegisteredSenderTests(RegisteredFixture):
    def test_send_binds_exact_issued_successor_with_explicit_reviewer_mention_once(self):
        with patch.object(guard.urllib.request, 'urlopen') as network, patch.object(guard, '_discord_bot_token', return_value='fixture'):
            network.return_value.__enter__.return_value.read.return_value = json.dumps(self.message).encode()
            _, bound = guard.send_successor_issue(issue_record_path=self.path)
            payload = json.loads(network.call_args.args[0].data)
        self.assertEqual(payload['content'], self.issue['request_content'])
        self.assertEqual(payload['allowed_mentions'], {'parse': [], 'users': [SIGNER]})
        self.assertTrue(payload['enforce_nonce'])
        self.assertEqual(bound['status'], 'BOUND')
        self.assertEqual(bound['request_message_id'], SOURCE)
        with self.assertRaises(RuntimeError):
            guard.send_successor_issue(issue_record_path=self.path)

    def test_suppressed_payload_refuses_before_claim_credentials_or_network(self):
        payload = guard.successor_request_payload(self.issue)
        payload['allowed_mentions'] = {'parse': []}
        with patch.object(guard, 'successor_request_payload', return_value=payload), \
                patch.object(guard, '_discord_bot_token') as token, \
                patch.object(guard.urllib.request, 'urlopen') as network:
            with self.assertRaisesRegex(RuntimeError, 'payload_suppresses_mention'):
                guard.send_successor_issue(issue_record_path=self.path)
        token.assert_not_called()
        network.assert_not_called()
        self.assertFalse(self.path.with_suffix('.send-claim.json').exists())

    def test_direct_successor_bind_also_refuses_suppressed_mention(self):
        self.message['mentions'] = []
        with self.assertRaisesRegex(RuntimeError, 'eligibility_missing'):
            guard.bind_request_issue(issue_record_path=self.path, request_message_id=SOURCE)
        self.assertEqual(json.loads(self.path.read_text())['status'], 'ISSUED')

    def test_ambiguous_send_is_persistently_nonretryable(self):
        with patch.object(guard.urllib.request, 'urlopen', side_effect=TimeoutError('uncertain')) as network, \
                patch.object(guard, '_discord_bot_token', return_value='fixture'):
            with self.assertRaises(TimeoutError):
                guard.send_successor_issue(issue_record_path=self.path)
            with self.assertRaisesRegex(RuntimeError, 'already_claimed'):
                guard.send_successor_issue(issue_record_path=self.path)
        self.assertEqual(network.call_count, 1)
        self.assertEqual(json.loads(self.path.read_text())['status'], 'ISSUED')

    def test_send_readback_missing_mention_withholds_bind_and_resend(self):
        self.message['mentions'] = []
        with patch.object(guard.urllib.request, 'urlopen') as network, \
                patch.object(guard, '_discord_bot_token', return_value='fixture'):
            network.return_value.__enter__.return_value.read.return_value = json.dumps(self.message).encode()
            with self.assertRaisesRegex(RuntimeError, 'eligibility_missing'):
                guard.send_successor_issue(issue_record_path=self.path)
            with self.assertRaisesRegex(RuntimeError, 'already_claimed'):
                guard.send_successor_issue(issue_record_path=self.path)
        self.assertEqual(network.call_count, 1)
        self.assertEqual(json.loads(self.path.read_text())['status'], 'ISSUED')


class RecoveryTests(RegisteredFixture):
    def setUp(self):
        super().setUp()
        _, self.issue = guard.bind_request_issue(issue_record_path=self.path, request_message_id=SOURCE)
        self.message['mentions'] = []
        self.state_path = self.root / 'state.json'
        self.state = {'last_id': SOURCE, 'reviewer_receipts': {}, 'successor_issues': {
            PREDECESSOR: {'status': 'issued', 'issue_id': self.issue['issue_id'],
                'predecessor_issue_id': self.issue['predecessor_issue_id'],
                'predecessor_reply_message_id': self.issue['predecessor_reply_message_id'],
                'issue_record_path': str(self.path)}}}
        self.save()
        self.posts = []
        self.extra = []
        self.inferences = Mock(return_value=SimpleNamespace(returncode=0,
            stdout='TRUE-BRIDGE FABLE — REJECT\nOffline fixture only.', stderr=''))
        for name, value in [('STATE', self.state_path), ('ISSUE_DIR', self.issues),
            ('ARTIFACT_ROOT', self.artifacts), ('SELF_ID', SIGNER),
            ('SESSION_ID', '00000000-0000-4000-8000-000000000001'),
            ('expected_author_id', Mock(return_value=SIGNER)),
            ('_native_session_id', Mock(return_value='20260922_120000_abcdef')),
            ('_resolve_model', Mock(return_value=('claude-fable-5-1', 'xhigh'))),
            ('_routing_policy', Mock(return_value={'threshold_percent': 100})),
            ('refresh_included_capacity', Mock(side_effect=self.capacity)),
            ('api', Mock(side_effect=self.api)), ('log', Mock())]:
            p = patch.object(bridge, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(bridge.subprocess, 'run', self.inferences)
        p.start()
        self.addCleanup(p.stop)

    def save(self):
        self.state_path.write_text(json.dumps(self.state))

    def capacity(self):
        return {'collected_at_utc': datetime.now(timezone.utc).isoformat(), 'records': [
            dict(provider='anthropic', source='oauth_usage_api', freshness='live', capacity_status='available',
                session_remaining_pct=50, weekly_remaining_pct=60)]}

    def api(self, method, path, body=None):
        if method == 'GET' and path.endswith('/' + SOURCE):
            return copy.deepcopy(self.message)
        if method == 'GET' and '?before=' in path:
            return [*self.extra, copy.deepcopy(self.message)]
        if method == 'POST':
            self.posts.append(body)
            return {'id': str(int(SOURCE) + 100)}
        raise AssertionError((method, path))

    def recover(self):
        # Import the candidate real guard, with only filesystem roots replaced.
        with patch.object(bridge, '_authority_guard', return_value=guard):
            return bridge.recover_missing_mention(SOURCE, self.issue['issue_id'], self.digest)

    def test_bound_missing_mention_recovery_claims_once_without_rewind_or_resend(self):
        original = copy.deepcopy(self.message)
        def inference(*args, **kwargs):
            receipt = bridge.load_state()['reviewer_receipts'][SOURCE]
            self.assertEqual(receipt['recovery']['status'], 'invocation_reserved')
            self.assertTrue(receipt['attempts'][0]['invoked'])
            command = args[0]
            self.assertIn('claude-fable-5-1', command)
            self.assertIn('xhigh', command)
            return SimpleNamespace(returncode=0, stdout='TRUE-BRIDGE FABLE — REJECT\nFixture', stderr='')
        self.inferences.side_effect = inference
        result = self.recover()
        self.assertEqual(result['status'], 'posted')
        self.assertEqual(self.message, original)
        self.assertEqual(bridge.load_state()['last_id'], SOURCE)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]['message_reference']['message_id'], SOURCE)
        with self.assertRaises(RuntimeError):
            self.recover()
        self.assertEqual(self.inferences.call_count, 1)

    def assert_refused(self, match=None):
        before = self.state_path.read_bytes()
        with self.assertRaisesRegex((RuntimeError, ValueError, OSError), match or '.'):
            self.recover()
        self.inferences.assert_not_called()
        self.assertFalse(self.posts)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_all_issue_bindings_and_canonical_hashes_refuse_drift(self):
        baseline = copy.deepcopy(self.issue)
        for field, value in [
            ('status', 'ISSUED'), ('schema', 'invalid'), ('issue_id', 'bridge-issue-' + 'b' * 32),
            ('request_message_id', str(int(SOURCE) + 1)), ('channel_id', 'wrong'),
            ('request_author_id', SIGNER), ('tuple_sha256', '0' * 64),
            ('review_artifact_sha256', '0' * 64), ('request_content_sha256', '0' * 64),
            ('request_content', self.issue['request_content'] + '\n'),
            ('request_timestamp', '2026-09-26T00:00:00Z'), ('request_edited_timestamp', 'edited'),
            ('challenge', 'invalid'), ('succession_reason', 'other'),
            ('predecessor_request_message_id', SOURCE), ('predecessor_issue_id', None),
            ('predecessor_reply_message_id', None), ('review_artifact_path', str(self.root / 'missing')),
        ]:
            with self.subTest(field=field):
                self.issue = copy.deepcopy(baseline)
                self.issue[field] = value
                self.write_issue()
                self.assert_refused()
        self.issue = baseline
        self.write_issue()
        self.issue['record_sha256'] = '0' * 64
        self.path.write_text(json.dumps(self.issue))
        self.assert_refused('record_hash_mismatch')

    def test_missing_issue_and_artifact_drift_are_refused(self):
        self.artifact.write_text('changed')
        self.assert_refused('artifact_hash_mismatch')
        self.path.unlink()
        self.assert_refused()

    def test_all_message_eligibility_defects_refuse_even_without_mention(self):
        baseline = copy.deepcopy(self.message)
        for field, value in [
            ('id', str(int(SOURCE) + 1)), ('channel_id', 'other'),
            ('content', self.message['content'] + '\n'), ('timestamp', 'changed'),
            ('author', {'id': bridge.KAEL_BOT_ID, 'bot': False}),
            ('author', {'id': SIGNER, 'bot': True}), ('author', {'id': bridge.OPERATOR_ID, 'bot': False}),
            ('webhook_id', 'some-hook'), ('edited_timestamp', 'edited'), ('type', 19),
            ('mentions', None), ('mentions', [{'id': SIGNER}]),
        ]:
            with self.subTest(field=field, value=value):
                self.message = copy.deepcopy(baseline)
                self.message[field] = value
                self.assert_refused()
        self.message = baseline

    def test_absent_malformed_or_prior_receipt_and_cursor_refuse(self):
        baseline = copy.deepcopy(self.state)
        for field, value in [
            ('reviewer_receipts', None), ('reviewer_receipts', {SOURCE: None}),
            ('reviewer_receipts', {SOURCE: {'attempts': [{'invoked': True}]}}),
            ('reviewer_receipts', {SOURCE: {'recovery': {'status': 'reserved'}}}),
            ('last_id', None), ('last_id', str(int(SOURCE) - 1)), ('last_id', 'bad'),
            ('successor_issues', {}), ('successor_issues', {PREDECESSOR: {'status': 'reserved'}}),
        ]:
            with self.subTest(field=field):
                self.state = copy.deepcopy(baseline)
                self.state[field] = value
                self.save()
                self.assert_refused()
        self.state_path.write_text('{')
        self.assert_refused()
        self.state_path.unlink()
        with self.assertRaises(OSError):
            self.recover()
        self.inferences.assert_not_called()

    def test_alias_receipt_with_same_source_is_prior_invocation(self):
        self.state['reviewer_receipts']['other'] = {'source_message_id': SOURCE, 'invocation_claimed': True}
        self.save()
        self.assert_refused('receipt_or_cursor_ineligible')

    def test_reply_by_any_author_and_incomplete_history_refuse(self):
        for author in [SIGNER, bridge.KAEL_BOT_ID, 'other']:
            self.extra = [{'id': str(int(SOURCE) + 1), 'author': {'id': author},
                           'message_reference': {'message_id': SOURCE}}]
            self.assert_refused('additional_reviewer_reply_present')
        self.extra = []
        for result in [None, [], [{'id': str(int(SOURCE) + 1)}]]:
            with patch.object(bridge, 'api', side_effect=lambda method, path, body=None:
                    result if '?before=' in path else self.api(method, path, body)):
                self.assert_refused('reply_history_')

    def test_fresh_positive_included_capacity_is_mandatory(self):
        original = self.capacity()
        snapshots = [None, {}, dict(original, collected_at_utc='2000-01-01T00:00:00Z'),
                     dict(original, collected_at_utc='2100-01-01T00:00:00Z')]
        for field, value in [('weekly_remaining_pct', 0), ('session_remaining_pct', 0),
            ('weekly_remaining_pct', None), ('session_remaining_pct', -1),
            ('weekly_remaining_pct', True), ('session_remaining_pct', float('inf')),
            ('weekly_remaining_pct', 101), ('session_remaining_pct', '50'),
            ('freshness', 'cached'), ('provider', 'openai'), ('capacity_status', 'unknown')]:
            changed = copy.deepcopy(original)
            changed['records'][0][field] = value
            snapshots.append(changed)
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                bridge.refresh_included_capacity.side_effect = lambda: snapshot
                self.assert_refused('fresh_positive_included_capacity_required')

    def test_signer_and_reviewer_route_refusals(self):
        with patch.object(bridge, 'expected_author_id', return_value=bridge.KAEL_BOT_ID):
            self.assert_refused('independent_signer_mismatch')
        for backend in [('astra-standin', 'gpt-6-astra', 'xhigh'),
                        ('fable', 'claude-other', 'xhigh'), ('fable', 'claude-fable-5-1', 'high')]:
            with patch.object(bridge, 'select_backend', return_value=backend):
                self.assert_refused('reviewer_model_changed')
        with patch.object(bridge, 'top_stakes_category', return_value='production'):
            self.assert_refused('request_routing_changed')
        with patch.object(bridge, '_design_review', return_value=False):
            self.assert_refused('request_routing_changed')

    def test_revalidation_after_capacity_and_after_inference(self):
        def compete():
            self.extra.append({'id': str(int(SOURCE) + 1), 'author': {'id': SIGNER},
                               'message_reference': {'message_id': SOURCE}})
        def refresh():
            compete()
            return self.capacity()
        bridge.refresh_included_capacity.side_effect = refresh
        self.assert_refused('additional_reviewer_reply_present')
        self.extra = []
        bridge.refresh_included_capacity.side_effect = self.capacity
        def inference(*args, **kwargs):
            compete()
            return SimpleNamespace(returncode=0, stdout='Offline review', stderr='')
        self.inferences.side_effect = inference
        with self.assertRaisesRegex(RuntimeError, 'additional_reviewer_reply_present'):
            self.recover()
        self.assertFalse(self.posts)
        self.assertEqual(bridge.load_state()['reviewer_receipts'][SOURCE]['posting'], 'withheld')
        with self.assertRaises(RuntimeError):
            self.recover()
        self.assertEqual(self.inferences.call_count, 1)

    def test_invocation_failure_and_ambiguous_post_remain_nonretryable(self):
        self.inferences.side_effect = TimeoutError('uncertain inference')
        with self.assertRaisesRegex(RuntimeError, 'no_safe_inference'):
            self.recover()
        with self.assertRaises(RuntimeError):
            self.recover()
        self.assertEqual(self.inferences.call_count, 1)
        self.assertFalse(self.posts)

    def test_ambiguous_post_never_replays_review_or_post(self):
        with patch.object(bridge, 'post', side_effect=TimeoutError('uncertain post')) as post:
            with self.assertRaises(TimeoutError):
                self.recover()
            with self.assertRaises(RuntimeError):
                self.recover()
        self.assertEqual(self.inferences.call_count, 1)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(bridge.load_state()['reviewer_receipts'][SOURCE]['posting'], 'failed_or_ambiguous')

    def test_oversize_recovery_uses_existing_verbatim_attachment_readback(self):
        raw = 'Offline initial response é\n' * 150
        outputs = iter([raw, 'TRUE-BRIDGE FABLE — REJECT\nOffline compact decision'])
        def inference(*args, **kwargs):
            self.assertNotIn('original_stdout', bridge.load_state()['reviewer_receipts'][SOURCE])
            return SimpleNamespace(returncode=0, stdout=next(outputs), stderr='')
        self.inferences.side_effect = inference
        posted_id = str(int(SOURCE) + 100)
        posted = {}
        http = Mock()
        def multipart(method, url, **kwargs):
            self.assertEqual(method, 'POST')
            body = json.loads(kwargs['data']['payload_json'])
            self.assertEqual(kwargs['files']['files[0]'][1], raw.encode())
            self.posts.append(body)
            posted.update(id=posted_id, author={'id': SIGNER}, content=body['content'],
                message_reference=body['message_reference'], edited_timestamp=None,
                attachments=[{'filename': 'reviewer-response.txt', 'size': len(raw.encode()),
                              'url': 'https://cdn.discordapp.com/attachments/fixture/response.txt'}])
            return Mock(status_code=200, json=lambda: {'id': posted_id})
        http.request.side_effect = multipart
        def api(method, path, body=None):
            if path.endswith('/' + posted_id):
                return posted
            return self.api(method, path, body)
        with patch.object(bridge, 'HTTP', http), patch.object(bridge, 'api', side_effect=api), \
                patch.object(bridge.requests, 'get', return_value=Mock(content=raw.encode())) as cdn:
            result = self.recover()
        receipt = bridge.load_state()['reviewer_receipts'][SOURCE]
        self.assertEqual(result['status'], 'posted')
        self.assertEqual(self.inferences.call_count, 2)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(receipt['attachment_verification']['attachment_sha256'], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(receipt['original_stdout_bytes'], len(raw.encode()))
        self.assertTrue(posted['content'].startswith('TRUE-BRIDGE FABLE — REJECT\nOffline compact decision'))
        self.assertNotIn('original_stdout', receipt)
        cdn.assert_called_once()
        with self.assertRaises(RuntimeError):
            self.recover()

    def test_shared_lock_and_fsync_precede_inference(self):
        import fcntl
        def inference(*args, **kwargs):
            with self.state_path.with_suffix('.json.lock').open('a+') as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertGreater(sync.call_count, 0)
            return SimpleNamespace(returncode=0, stdout='Offline fixture', stderr='')
        self.inferences.side_effect = inference
        with patch.object(bridge.os, 'fsync', wraps=bridge.os.fsync) as sync:
            self.recover()

    def test_model_changes_during_capacity_refresh_never_reroute(self):
        def refresh():
            bridge._resolve_model.return_value = ('claude-other', 'xhigh')
            return self.capacity()
        bridge.refresh_included_capacity.side_effect = refresh
        self.assert_refused('design_review_requires_fable_5_1')

    def test_capacity_expiry_at_invocation_boundary_withholds_inference(self):
        with patch.object(bridge, '_included_quota_available', side_effect=[True, True, False]):
            with self.assertRaisesRegex(RuntimeError, 'no_safe_inference'):
                self.recover()
        self.inferences.assert_not_called()
        self.assertFalse(self.posts)
        with self.assertRaises(RuntimeError):
            self.recover()

    def test_claim_write_failure_does_not_invoke_or_send(self):
        with patch.object(bridge, 'save_state', side_effect=OSError('disk full')):
            self.assert_refused('disk full')


if __name__ == '__main__':
    unittest.main()
