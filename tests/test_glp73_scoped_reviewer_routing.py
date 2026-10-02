"""Subject-bound routing only. All issues, messages and inferences are offline fixtures."""
import copy
import datetime as dt
import hashlib
import runpy
import sys
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load('scoped_bridge', 'scripts/fable_bridge.py')
guard = load('scoped_guard', 'ventures/boring-ai/holistic-machine/scripts/bridge_authority_guard.py')
SIGNER = '1529571092371476591'
SOURCE = '1553357347328499764'
DESIGNATION = 'glp42-pr8-astra-20260926'
HEAD = '38133df58fc5406adeb87ed88771cc6ceeb26342'
SUBJECT = 'glp:GLP-42:pr8:' + HEAD
# Verbatim retained nonsecret decision, not synthetic acceptance or reviewer output.
AUTHORITY = '''# Operator decision: release acceptance review by Astra, not Fable (2026-09-26, Izaiah, TUI)

Verbatim: "we dont have enough quota this week for any more fable reviews so we will have to use an astra max or xhigh agent for that - so make sure that when the time comes for that - u remember to use astra for the indeependent review and it doesnt become a blocker waiting on fable."

Scope:
- The GLP-42 designated release acceptance review is performed by an independent Astra agent (gpt-6-astra-900k, openai-codex, reasoning effort xhigh or max), not Fable, until the operator says otherwise.
- The reviewer must not be the author of any change in the release and must bind its verdict to the exact final PR #8 head, sealed public tree hash and deploy package manifest.
- Same acceptance content as the Fable request would have had: full gate receipt, renders, consent/analytics behaviour (operator decisions on default-on and GA4 disclosed), deploy package, rollback target ca0251a2-dd4e-469c-be3b-79ee8805c5f3.
- Deploy proceeds on that Astra APPROVE plus the exact-target readback. Fable quota is not awaited.
'''


class ScopedRoutingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.artifact = self.root / 'review.json'
        self.authority = self.root / 'operator.md'
        self.authority.write_text(AUTHORITY)
        self.assertEqual(hashlib.sha256(self.authority.read_bytes()).hexdigest(),
                         '7d0d49def4254a19e088d4d6586c9963325311da17cfbd6054dd175876240fb0')
        self.tuple = {
            'subject_id': SUBJECT,
            'site_head': HEAD,
            'sealed_root': '/home/ubuntu/business/reports/ops/glp43-wrangler-shape-repair-20260927/prepared-final381/release',
            'seal_closure_sha256': '87aec3395aa7e234fa441ac7c47e5972ae3216bf874f2375e62dc8688e179ec2',
            'public_tree_sha256': 'f186ce38f36fcc1eab8c9f2f000289a09cd76f8327b8fec255969802dcb7b82d',
            'deployment_manifest_sha256': '61461fe6d224571f2bfb0d579c347b779d2026e1294b0ed40bd22b26e1bd8c8c',
            'deployment_id': '8291d24b-a1c6-4340-bd42-2efa9f2ba2b7',
            'rollback_target': 'ca0251a2-dd4e-469c-be3b-79ee8805c5f3',
            'reviewer_authority_sha256': hashlib.sha256(self.authority.read_bytes()).hexdigest(),
            'inputs': [],  # Routing fixture, NEVER a complete release-review packet.
        }
        self.packet = {'schema': 'glp.q44.independent-review.v1',
                       'status': 'READY_FOR_INDEPENDENT_REVIEW_NOT_ACCEPTED',
                       'reviewer_designation': DESIGNATION, 'tuple': self.tuple}
        self.write_packet()
        self.runner = Mock(return_value=SimpleNamespace(returncode=0, stdout='Fixture inspection only; no authority decision.', stderr=''))
        for obj, name, value in (
            (guard, 'REVIEW_ARTIFACT_ROOT', self.root), (guard, 'BRIDGE_ISSUE_ROOT', self.root),
            (guard, 'GLP42_REVIEW_AUTHORITY', self.authority),
            (guard, '_fetch_discord_message', self.fetch),
            (bridge, '_authority_guard', lambda: guard), (bridge, 'ISSUE_DIR', self.root),
            (bridge, 'SELF_ID', SIGNER), (bridge, 'log', Mock()),
            (bridge, '_routing_policy', lambda: {'hermes_model': 'gpt-6-astra-900k', 'threshold_percent': 95}),
            (bridge, '_resolve_model', lambda: ('claude-fable-5-1', 'xhigh')),
            (bridge, '_usage_exhausted', lambda *a: False),
            (bridge, '_included_quota_available', lambda *a: True),
            (bridge, '_native_session_id', lambda *a: '20260922_120000_abcdef'),
            (bridge.subprocess, 'run', self.runner),
            (bridge.requests, 'Session', Mock(side_effect=AssertionError('SDK forbidden'))),
            (bridge, 'api', Mock(side_effect=AssertionError('Discord forbidden'))),
            (guard, '_discord_bot_token', Mock(side_effect=AssertionError('credentials forbidden'))),
        ):
            p = patch.object(obj, name, value, create=name == 'GLP42_REVIEW_AUTHORITY')
            p.start()
            self.addCleanup(p.stop)

    def write_packet(self):
        self.digest = guard.sha256_text(guard.canonical_json(self.tuple))
        self.packet['tuple_sha256'] = self.digest
        self.artifact.write_text(json.dumps(self.packet))

    def fetch(self, channel, message_id):
        self.assertEqual(channel, bridge.CHANNEL)
        self.assertEqual(message_id, SOURCE)
        return copy.deepcopy(self.message)

    def issue_and_bind(self):
        self.path, self.issue = guard.issue_request(subject_id=SUBJECT,
            tuple_sha256=self.digest, review_artifact_path=self.artifact)
        self.message = dict(id=SOURCE, channel_id=bridge.CHANNEL, type=0,
            author={'id': bridge.KAEL_BOT_ID, 'bot': True}, mentions=[{'id': SIGNER}],
            content=self.issue['request_content'], timestamp=guard._utc_now(), edited_timestamp=None)
        self.path, self.issue = guard.bind_request_issue(issue_record_path=self.path, request_message_id=SOURCE)
        return bridge.classify_message(self.message)

    def review(self, source_id=SOURCE, text=None):
        kind, canonical = bridge.classify_message(self.message)
        receipt = {'source_message_id': source_id}
        mode, reply = bridge.review_action(kind, canonical if text is None else text,
                                           source_message_id=source_id, receipt=receipt)
        return mode, reply, receipt

    def test_canonical_authorized_request_routes_astra_without_exhaustion(self):
        self.issue_and_bind()
        mode, reply, receipt = self.review()
        self.assertEqual(mode, 'model')
        self.assertEqual(receipt['backend'], 'astra-glp42')
        self.assertIn('reviewer_designation: ' + DESIGNATION, self.issue['request_content'])
        self.assertNotIn('NON-DESIGN REVIEW', self.issue['request_content'])
        self.assertNotIn('claude_exhausted', reply)
        self.assertEqual(self.runner.call_count, 1)

    def test_scope_is_fixed_in_the_hashed_tuple_not_a_marker(self):
        original = copy.deepcopy(self.tuple)
        for field in original.keys() - {'inputs'}:
            with self.subTest(field=field):
                self.tuple[field] = 'another-subject'
                self.write_packet()  # Even correctly rehashed mismatches cannot route.
                with self.assertRaisesRegex(guard.BridgeAuthorityError, 'scope_mismatch'):
                    self.issue_and_bind()
                self.tuple[field] = original[field]
        self.write_packet()
        self.packet['reviewer_designation'] = 'arbitrary-astra-marker'
        self.write_packet()
        with self.assertRaisesRegex(guard.BridgeAuthorityError, 'scope_mismatch'):
            self.issue_and_bind()
        self.assertEqual(list(self.root.glob('bridge-issue-*.json')), [])

    def test_changed_or_missing_retained_authority_refuses_new_issues(self):
        self.authority.write_text(AUTHORITY + '\nchanged\n')
        with self.assertRaisesRegex(guard.BridgeAuthorityError, 'authority_changed'):
            self.issue_and_bind()
        self.authority.unlink()
        with self.assertRaises(OSError):
            self.issue_and_bind()
        self.assertEqual(list(self.root.glob('bridge-issue-*.json')), [])

    def test_canonical_tuple_hash_cannot_be_declared_without_recomputation(self):
        self.digest = 'a' * 64
        self.packet['tuple_sha256'] = self.digest
        self.artifact.write_text(json.dumps(self.packet))
        with self.assertRaisesRegex(guard.BridgeAuthorityError, 'scope_mismatch'):
            self.issue_and_bind()

    def test_historical_v1_v2_records_reconstruct_without_new_authority(self):
        self.packet.pop('reviewer_designation')
        self.write_packet()
        self.issue_and_bind()
        self.authority.unlink()  # Historical requests must not need the new ruling.
        original = self.path.read_bytes()
        _, loaded = guard.load_issue_record(self.path, expected_subject_id=SUBJECT, expected_tuple_sha256=self.digest)
        self.assertNotIn('reviewer_designation', loaded)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.review()[2]['backend'], 'fable')
        self.issue['schema'] = guard.BRIDGE_ISSUE_SCHEMA_V1
        self.issue['request_content'] = '\n'.join(line for line in self.issue['request_content'].splitlines()
            if not line.startswith(('inspection_note_min_chars:', 'inspection_note_required_prefix:')))
        self.issue['request_content_sha256'] = guard.sha256_text(self.issue['request_content'])
        self.issue['record_sha256'] = guard._record_hash(self.issue)
        self.path.write_text(json.dumps(self.issue))
        before = self.path.read_bytes()
        guard.load_issue_record(self.path, expected_subject_id=SUBJECT, expected_tuple_sha256=self.digest)
        self.assertEqual(before, self.path.read_bytes())

    def test_posthoc_marker_cannot_upgrade_an_issued_request(self):
        self.packet.pop('reviewer_designation')
        self.write_packet()
        self.issue_and_bind()
        text = bridge.classify_message(self.message)[1] + '\nreviewer_designation: ' + DESIGNATION
        _, reply, receipt = self.review(text=text)
        self.assertEqual(receipt['attempts'], [])
        self.assertIn('no authority decision', reply)
        self.runner.assert_not_called()
        self.issue['request_content'] += '\nreviewer_designation: ' + DESIGNATION
        self.issue['request_content_sha256'] = guard.sha256_text(self.issue['request_content'])
        self.issue['record_sha256'] = guard._record_hash(self.issue)
        self.path.write_text(json.dumps(self.issue))
        with self.assertRaisesRegex(guard.BridgeAuthorityError, 'request_invalid'):
            guard.load_issue_record(self.path, expected_subject_id=SUBJECT, expected_tuple_sha256=self.digest)

    def test_authenticated_request_binding_and_replay_negatives(self):
        self.issue_and_bind()
        text = bridge.classify_message(self.message)[1]
        good = copy.deepcopy(self.message)
        for change in (
            {'author': {'id': SIGNER, 'bot': True}}, {'channel_id': '999999999999999999'},
            {'content': self.message['content'] + '\nother'}, {'edited_timestamp': guard._utc_now()},
            {'mentions': []}, {'type': 1}, {'webhook_id': 'fixture'},
            {'timestamp': '2026-01-01T00:00:00Z'}, {'id': '1553357347328499765'},
        ):
            with self.subTest(change=change):
                self.message = {**good, **change}
                receipt = {'source_message_id': SOURCE}
                reply = bridge.ask_fable('review_request', text, receipt=receipt)
                self.assertEqual(receipt['attempts'], [])
                self.assertIn('no authority decision', reply)
        self.message = good
        for source in ('1553357347328499765', None):
            receipt = {'source_message_id': source}
            bridge.ask_fable('review_request', text, receipt=receipt)
            self.assertEqual(receipt['attempts'], [])
        self.runner.assert_not_called()

    def test_top_stakes_and_generic_outward_design_are_not_reclassified(self):
        self.issue_and_bind()
        for phrase in ('production deploy', 'pricing', 'cloudflare', 'evidence-sensitive'):
            mode, _, receipt = self.review(text=bridge.classify_message(self.message)[1] + '\n' + phrase)
            self.assertEqual(mode, 'deferred')
            self.assertEqual(receipt['attempts'], [])
        with patch.object(bridge, '_usage_exhausted', return_value=True):
            receipt = {}
            bridge.ask_fable('review_request', 'Final outward-facing visual design review', receipt=receipt)
        self.assertEqual(receipt['backend'], 'fable')
        self.assertTrue(bridge._design_review('review_request', self.issue['request_content']))

    def test_actual_native_entry_sets_standard_without_mutating_saved_config(self):
        self.issue_and_bind()
        observed = []
        def command(cmd, **kwargs):
            self.assertEqual(cmd[:4], [str(bridge.HERMES_PYTHON), str(ROOT / 'scripts/fable_bridge.py'), '--native-reviewer', 'chat'])
            for flag, value in (('--provider', 'openai-codex'), ('-m', 'gpt-6-astra-900k'),
                                ('--reasoning', 'xhigh'), ('--toolsets', 'fable-review-readonly')):
                self.assertEqual(cmd[cmd.index(flag) + 1], value)
            prompt = Path(cmd[cmd.index('--query-file') + 1]).read_text()
            self.assertIn('If you authored any change in this release, issue no acceptance.', prompt)
            self.assertIn('default-on and GA4 disclosures', prompt)
            config = {'agent': {'service_tier': 'priority'}, 'fallback_providers': [{'provider': 'not-allowed'}],
                      'fallback_model': {'provider': 'legacy-not-allowed'}}
            create = Mock()
            def main():
                observed.append(copy.deepcopy(config))
                self.assertEqual(sys.argv[1], 'chat')
            with patch.dict(sys.modules, {'toolsets': SimpleNamespace(create_custom_toolset=create),
                    'hermes_cli.main': SimpleNamespace(main=main), 'cli': SimpleNamespace(CLI_CONFIG=config)}), \
                    patch.object(sys, 'argv', cmd[1:]):
                runpy.run_path(str(ROOT / 'scripts/fable_bridge.py'), run_name='__main__')
            self.assertEqual(create.call_args.args[2], ['read_file', 'search_files', 'web_search', 'web_extract', 'vision_analyze'])
            return SimpleNamespace(returncode=0, stdout='Offline fixture, no decision.', stderr='')
        self.runner.side_effect = command
        _, _, receipt = self.review()
        self.assertEqual(observed, [{'agent': {'service_tier': 'default'}, 'fallback_providers': []}])
        self.assertEqual(receipt['provider'], 'openai-codex')
        self.assertEqual(receipt['service_tier_config'], 'default')
        self.assertEqual(receipt['attempts'][0]['session_name'], receipt['standin_session_name'])
        self.assertNotIn('provider_returned_service_tier', receipt)

    def test_compact_reply_reuses_scoped_session_backend_and_deadline(self):
        self.issue_and_bind()
        self.runner.side_effect = [SimpleNamespace(returncode=0, stdout='x' * 2000, stderr=''),
                                SimpleNamespace(returncode=0, stdout='Offline compact, no decision.', stderr='')]
        _, _, receipt = self.review()
        commands = [call.args[0] for call in self.runner.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertEqual([a['backend'] for a in receipt['attempts']], ['astra-glp42'] * 2)
        self.assertEqual(commands[0][commands[0].index('--continue') + 1], commands[1][commands[1].index('--continue') + 1])
        self.assertIn('--create-if-missing', commands[0])
        self.assertNotIn('--create-if-missing', commands[1])
        self.assertGreaterEqual(self.runner.call_args_list[0].kwargs['timeout'], self.runner.call_args_list[1].kwargs['timeout'])

    def test_exact_guard_acceptance_checks_still_apply_to_scoped_request(self):
        self.issue_and_bind()
        request = copy.deepcopy(self.message)
        answer_id = '1553357347328499766'
        answer = dict(id=answer_id, channel_id=bridge.CHANNEL, author={'id': SIGNER},
            edited_timestamp=None, message_reference={'message_id': SOURCE},
            timestamp=(guard._timestamp(request['timestamp'], 'request') + dt.timedelta(seconds=35)).isoformat(),
            content='\n'.join([guard.EXACT_ACCEPTANCE_DECISION_LINE, SUBJECT, self.digest,
                self.issue['issue_id'], self.issue['challenge'],
                'inspection_note: Offline fixture inspection of exact immutable bytes and preserved obligations; this is not real release acceptance.']))
        def validate():
            return guard.validate_live_acceptance(request_message_id=SOURCE, acceptance_message_id=answer_id,
                subject_id=SUBJECT, tuple_sha256=self.digest, issue_record_path=self.path)
        with patch.object(guard, '_fetch_discord_message', side_effect=lambda c, m: copy.deepcopy(request if m == SOURCE else answer)):
            self.assertEqual(validate()['authority_origin'], 'guard_owned_authenticated_discord_readback')
            good = copy.deepcopy(answer)
            for change in ({'author': {'id': bridge.KAEL_BOT_ID}}, {'message_reference': {'message_id': answer_id}},
                           {'edited_timestamp': request['timestamp']}, {'timestamp': request['timestamp']},
                           {'content': good['content'].replace(self.digest, 'b' * 64)},
                           {'content': good['content'] + '\nREJECT'}):
                answer = {**good, **change}
                with self.assertRaises(guard.BridgeAuthorityError):
                    validate()

    def test_bound_artifact_drift_is_rechecked_before_invocation(self):
        self.issue_and_bind()
        original = bridge._invoke_reviewer
        def drift(*args, **kwargs):
            self.artifact.write_text(self.artifact.read_text() + ' ')
            return original(*args, **kwargs)
        with patch.object(bridge, '_invoke_reviewer', side_effect=drift):
            _, reply, receipt = self.review()
        self.runner.assert_not_called()
        self.assertIn('no authority decision', reply)
        self.assertFalse(receipt['attempts'][0]['invoked'])

    def test_ordinary_daemon_path_records_scoped_identity_and_never_replays(self):
        self.issue_and_bind()
        state_path = self.root / 'state.json'
        state_path.write_text(json.dumps({'last_id': str(int(SOURCE) - 1)}))
        sid = self.root / 'sid.txt'
        sid.write_text('00000000-0000-4000-8000-000000000001')
        def api(method, path, body=None):
            self.assertEqual(method, 'GET')
            return [copy.deepcopy(self.message)]
        def posted(*args, **kwargs):
            receipt = bridge.load_state()['reviewer_receipts'][SOURCE]
            self.assertEqual(receipt['backend'], 'astra-glp42')
            self.assertTrue(receipt['invocation_claimed'])
            self.assertEqual(receipt['posting'], 'prepared')
            return '1553357347328499766'
        with patch.object(bridge, 'STATE', state_path), patch.object(bridge, 'SID_FILE', sid), \
                patch.object(bridge, 'STOP_REQUESTED', False), \
                patch.object(bridge, 'token', return_value='offline-fixture'), \
                patch.object(bridge, 'validate_startup_identity', return_value=SIGNER), \
                patch.object(bridge.requests, 'Session', return_value=SimpleNamespace(headers={})), \
                patch.object(bridge, 'api', side_effect=api), \
                patch.object(bridge, 'post', side_effect=posted) as post, \
                patch.object(bridge.signal, 'signal'), \
                patch.object(bridge.time, 'sleep', side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                bridge.main()
            receipt = bridge.load_state()['reviewer_receipts'][SOURCE]
            self.assertEqual(receipt['posting'], 'posted')
            self.assertEqual(receipt['reviewer_designation'], DESIGNATION)
            with self.assertRaisesRegex(RuntimeError, 'already_receipted_source_message'):
                bridge.main()
            post.assert_called_once()
        self.runner.assert_called_once()


if __name__ == '__main__':
    unittest.main()
