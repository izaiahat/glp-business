"""Offline research-only sent/bound supersession; no browser/Drive/Linear effects."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import gpt6pro_relay as relay

sha = lambda b: hashlib.sha256(b.read_bytes() if isinstance(b, Path) else b).hexdigest()
RULING = 'ops/linear/approvals/OPERATOR-DECISION-test-pro-supersession.json'


class SupersessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'reports/ops/gpt6pro-relay/jobs'
        self.job = 'route-glp-358-fixture'
        self.replacement = self.job + '-p17'
        d = self.jobs / self.job
        d.mkdir(parents=True)
        self.cfg = {'accounts': [{'label': 'fixture-account', 'cdp_port': 9999}],
                    'ledger': 'ops/gpt6pro-relay/ledger.jsonl',
                    'envelope': {'max_concurrent_chats_per_account': 2, 'max_concurrent_chats_per_ip': 6}}
        prompt = d / 'PROMPT.md'; prompt.write_text('Research only. No live effects.\n')
        manifest = d / 'MANIFEST.json'; manifest.write_text(json.dumps({'attach': [], 'drive_attach': []}))
        self.state = {'job': self.job, 'job_class': 'research-synthesis', 'account': 'fixture-account',
                      'status': 'running', 'retry_blocked': False, 'typed': True, 'sent': True,
                      'binding_version': 2, 'conversation_id': '22222222-2222-2222-2222-222222222222',
                      'pending_conversation_url': 'https://chatgpt.com/c/22222222-2222-2222-2222-222222222222',
                      'tab': 'retained-generating-tab', 'linear_child': 'GLP-358',
                      'dispatcher_profile': 'default', 'hermes_session_id': 'fixture', 'request_id': self.job,
                      'reply_token': 'a' * 64,
                      'input_sha256': sha(json.dumps([(prompt.name, sha(prompt.read_bytes()))], separators=(',', ':')).encode())}
        (d / 'MESSAGE.md').write_text(relay._failed_generation_identity(self.state))
        self.preimage = json.dumps(self.state, indent=1).encode()
        (d / 'JOB.json').write_bytes(self.preimage)
        self.ledger = self.root / self.cfg['ledger']; self.ledger.parent.mkdir(parents=True)
        rows = [{'event': 'submitted', 'job': self.job}, {'event': 'submission_bound', 'job': self.job,
                 'account': 'fixture-account', 'conversation_url': self.state['pending_conversation_url'],
                 **{k: self.state[k] for k in relay.PROVENANCE_FIELDS}}]
        self.ledger.write_bytes(b''.join(json.dumps(r).encode() + b'\n' for r in rows))
        entry = {'job': self.job, 'replacement_job': self.replacement, 'linear_child': 'GLP-358',
                 'account_alias': 'acct1', 'job_sha256': sha(self.preimage), 'input_sha256': self.state['input_sha256'],
                 'files': {str(p.relative_to(self.root)): sha(p.read_bytes()) for p in d.iterdir() if p.name != 'JOB.json'},
                 'ledger_lines_sha256': [sha(line) for line in self.ledger.read_bytes().splitlines()]}
        self.ruling = {'schema': 'pro-research-supersession-ruling-v1', 'authority': 'operator',
                       'decision_date': '2026-10-13', 'verbatim': 'i approve everything',
                       'research_only': True, 'no_side_effects': True, 'jobs': [entry]}
        self.ruling_path = self.root / RULING
        self.ruling_path.parent.mkdir(parents=True)
        self.ruling_path.write_text(json.dumps(self.ruling))
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.allowed = {RULING: sha(self.ruling_path.read_bytes())}
        for name, value in [('BUSINESS', self.root), ('JOBS', self.jobs),
                            ('SUPERSESSION_RULINGS', self.allowed)]:
            self.stack.enter_context(mock.patch.object(relay, name, value, create=True))
        self.stack.enter_context(mock.patch.object(relay, 'load_config', return_value=self.cfg))
        self.stack.enter_context(mock.patch.object(relay, '_legacy_live_jobs', return_value=set()))
        for name in ('bu', 'drive', 'read_reply', 'close_tab_quiet'):
            self.stack.enter_context(mock.patch.object(relay, name, side_effect=AssertionError('no external effects: ' + name)))

    def call(self, apply=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return relay.supersede_research(self.job, RULING, apply=apply,
                    authorize_preimage=sha(self.preimage) if apply else None)

    def test_post_ruling_native_observation_is_preserved_not_effect_authority(self):
        relay._record_generation_observation(self.job, self.cfg, self.state,
            {'tab': self.state['tab'], 'state': {'stop': True}},
            '2026-10-13T02:00:00+00:00', '2026-10-13T02:00:01+00:00')
        self.assertEqual(self.call(True), 0)
        self.assertTrue(relay._verified_supersession(relay.job_state(self.job)))
        self.assertEqual(len(relay.ledger_rows(self.cfg)), 5)

    def test_post_ruling_observation_identity_or_schema_drift_is_refused(self):
        relay._record_generation_observation(self.job, self.cfg, self.state,
            {'tab': self.state['tab'], 'state': {'stop': True}},
            '2026-10-13T02:00:00+00:00', '2026-10-13T02:00:01+00:00')
        lines = self.ledger.read_bytes().splitlines(); native = json.loads(lines[-1])
        for key, value in [('event', 'followup'), ('tab', 'sibling'), ('input_sha256', '0' * 64),
                           ('authenticated_generation_observed', True), ('coverage', 'interval'),
                           ('read_started_at', '2026-10-14T02:00:00+00:00')]:
            row = {**native, key: value}
            self.ledger.write_bytes(b'\n'.join(lines[:-1] + [json.dumps(row).encode()]) + b'\n')
            with self.subTest(key=key), self.assertRaisesRegex(SystemExit, 'SUPERSESSION_REFUSED'):
                self.call(True)
            self.assertEqual((self.jobs / self.job / 'JOB.json').read_bytes(), self.preimage)

    def test_post_ruling_effect_record_is_refused(self):
        relay.ledger_append(self.cfg, {'event': 'followup', 'job': self.job})
        with self.assertRaisesRegex(SystemExit, 'SUPERSESSION_REFUSED'):
            self.call(True)
        self.assertEqual((self.jobs / self.job / 'JOB.json').read_bytes(), self.preimage)

    def test_apply_preserves_bound_history_and_protects_retained_tab(self):
        self.assertEqual(self.call(), 0)
        self.assertEqual((self.jobs / self.job / 'JOB.json').read_bytes(), self.preimage)
        before = self.ledger.read_bytes()
        self.assertEqual(self.call(True), 0)
        st = relay.job_state(self.job)
        self.assertEqual(st['status'], 'SUPERSEDED')
        self.assertTrue(st['retry_blocked'])
        self.assertTrue(st['sent']); self.assertTrue(st['typed'])
        self.assertEqual(st['supersession']['replacement_job'], self.replacement)
        self.assertTrue(relay._verified_supersession(st))
        self.assertEqual(relay.active_registry(), [])
        self.assertEqual(relay.protected_tabs({'account': 'fixture-account', 'job': 'new-job'}), ['retained-generating-tab'])
        self.assertTrue(self.ledger.read_bytes().startswith(before))
        self.assertEqual([r['phase'] for r in relay.ledger_rows(self.cfg)[2:]], ['STARTED', 'APPLIED'])
        self.assertEqual((self.jobs / self.job / ('JOB.pre-supersession-' + sha(self.preimage) + '.json')).read_bytes(), self.preimage)
        self.assertFalse((self.jobs / self.replacement).exists())
        self.assertEqual(self.call(True), 0)  # exact idempotent readback, no new journal rows
        self.assertEqual(len(relay.ledger_rows(self.cfg)), 4)


    def prepare_route(self):
        spec = importlib.util.spec_from_file_location('pro_route_supersession_test', ROOT / 'scripts/pro_route.py')
        route = importlib.util.module_from_spec(spec); spec.loader.exec_module(route)
        self.stack.enter_context(mock.patch.object(route, 'ROOT', self.root))
        self.stack.enter_context(mock.patch.object(route, 'relay', relay))
        self.stack.enter_context(mock.patch.object(route.packet, 'BUSINESS', self.root))
        self.stack.enter_context(mock.patch.object(route.packet, 'JOBS', self.jobs))
        self.stack.enter_context(mock.patch.object(route.subprocess, 'run', side_effect=AssertionError('external effect')))
        text = 'Original source-bound research situation; no operational effects.'
        digest = hashlib.sha256(('GLP-358\0' + text).encode()).hexdigest()
        wave = {'schema': 'gpt6pro-wave-v1', 'issue': 'GLP-358', 'acceptance_verbatim': 'Research only.',
                'request_id': self.job, 'linear_child': 'GLP-358', 'dispatcher_profile': 'default',
                'hermes_session_id': 'fixture', 'jobs': [{'job': self.job, 'kind': 'freeform',
                'job_class': 'research-synthesis', 'title': 'fixture', 'situation': text,
                'ruling': 'Research only; no effects.', 'task': 'Advise only.', 'files': []}]}
        path = self.root / 'ops/gpt6pro-relay/waves' / (self.job + '.json')
        relay._atomic_json(path, wave)
        approval = json.loads(self.ruling_path.read_bytes()); entry = approval['jobs'][0]
        entry.update(wave=str(path.relative_to(self.root)), request_sha256=digest)
        entry['files'][str(path.relative_to(self.root))] = sha(path)
        relay._atomic_json(self.ruling_path, approval); self.allowed[RULING] = sha(self.ruling_path)
        route.save({'job': self.job, 'issue': 'GLP-358', 'request_sha256': digest,
                    'wave': str(path), 'state': 'running', 'attempts': [{'action': 'submit'}]})
        return route, wave

    def test_real_registered_replacement_is_same_situation_and_idempotent(self):
        route, wave = self.prepare_route()
        with self.assertRaisesRegex(ValueError, 'SUPERSESSION_RECEIPT_REQUIRED'):
            route.enqueue_replacement(self.job, RULING)
        self.call(True)
        row = route.enqueue_replacement(self.job, RULING)
        new_wave = json.loads(Path(row['wave']).read_bytes())
        expected = json.loads(json.dumps(wave))
        expected['request_id'] = self.replacement; expected['jobs'][0]['job'] = self.replacement
        self.assertEqual(new_wave, expected)
        self.assertEqual(row['requested_account'], 'acct1')
        self.assertEqual(row['supersedes'], self.job)
        manifest = json.loads((self.jobs / self.replacement / 'MANIFEST.json').read_bytes())
        self.assertEqual(manifest['producer'], 'scripts/gpt6pro_packet.py')
        self.assertIn(wave['jobs'][0]['situation'], (self.jobs / self.replacement / 'PROMPT.md').read_text())
        route._replacement_receipt(row)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(route.enqueue_replacement(self.job, RULING), row)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        original_row = next(r for r in route.request_rows() if r['job'] == self.job)
        self.assertEqual(original_row['state'], 'superseded')
        self.assertEqual(original_row['attempts'], [{'action': 'submit'}])
        with mock.patch.object(route, 'effect', side_effect=AssertionError('old tab touched')):
            self.assertEqual(route.dispatch(only=self.job), [])

    def test_replacement_drift_stops_before_heavy_lease(self):
        route, wave = self.prepare_route(); self.call(True)
        row = route.enqueue_replacement(self.job, RULING)
        for key, value in [('job', 'other'), ('requested_account', 'acct2'), ('account', 'acct3')]:
            with self.assertRaisesRegex(ValueError, 'SUPERSESSION_REPLACEMENT_DRIFT'):
                route.effect({**row, key: value}, 'submit')
        changed = json.loads(Path(row['wave']).read_bytes())
        changed['jobs'][0]['situation'] = 'Different work'
        relay._atomic_json(Path(row['wave']), changed)
        with self.assertRaisesRegex(ValueError, 'SUPERSESSION_REPLACEMENT_DRIFT'):
            route.effect(row, 'submit')
        row['wave_sha256'] = sha(Path(row['wave']))
        with self.assertRaisesRegex(ValueError, 'SUPERSESSION_REPLACEMENT_SITUATION_DRIFT'):
            route.effect(row, 'submit')

    def test_rehashed_replacement_payload_cannot_change_approved_work(self):
        route, _ = self.prepare_route(); self.call(True)
        row = route.enqueue_replacement(self.job, RULING)
        for name in ('PROMPT.md', 'MANIFEST.json'):
            path = self.jobs / self.replacement / name
            before = path.read_bytes()
            changed = json.loads(json.dumps(row))
            if name == 'PROMPT.md':
                path.write_text('Unapproved replacement task.\n')
            else:
                manifest = json.loads(before)
                manifest['linear_child'] = 'GLP-336'
                path.write_text(json.dumps(manifest, indent=1))
            changed['packet_sha256'][name] = sha(path)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'SUPERSESSION_REPLACEMENT_PAYLOAD_DRIFT'):
                route.effect(changed, 'submit')
            path.write_bytes(before)
        route._replacement_receipt(row)  # genuine producer bytes remain accepted

    def test_missing_replacement_link_cannot_bypass_receipt_validation(self):
        route, _ = self.prepare_route(); self.call(True)
        row = route.enqueue_replacement(self.job, RULING)
        row.pop('supersedes')
        row['account'] = 'acct1'
        with self.assertRaisesRegex(ValueError, 'SUPERSESSION_REPLACEMENT_LINK_REQUIRED'):
            route.effect(row, 'submit')

    def test_interrupted_packet_is_held_without_rebuild_or_deletion(self):
        route, _ = self.prepare_route(); self.call(True)
        with mock.patch.object(route.packet, 'build_freeform', side_effect=OSError('fixture interruption')):
            with self.assertRaises(OSError): route.enqueue_replacement(self.job, RULING)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ValueError, 'ORPHAN_REPLACEMENT_CUSTODY'):
            route.enqueue_replacement(self.job, RULING)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_superseded_original_can_never_be_resubmitted_even_without_flag(self):
        self.call(True)
        st = relay.job_state(self.job); st['retry_blocked'] = False
        relay.job_write(self.job, st)
        args = type('Args', (), {'job': self.job, 'job_class': 'research-synthesis',
                                'account': 'fixture-account'})()
        self.cfg['job_classes_allowed'] = ['research-synthesis']
        with self.assertRaisesRegex(SystemExit, 'REFUSED_DUPLICATE'):
            relay._cmd_submit(args)

    def test_refusals_do_not_mutate_any_custody(self):
        cases = ['ruling_tamper', 'unauthorized', 'bad_authority', 'wrong_issue', 'nonresearch',
                 'unsent', 'legacy', 'packet_drift', 'ledger_drift', 'existing_replacement',
                 'response_present', 'live_runner']
        for case in cases:
            with self.subTest(case=case), contextlib.ExitStack() as stack:
                saved = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                if case == 'ruling_tamper': self.ruling_path.write_text('{}')
                if case == 'unauthorized': stack.enter_context(mock.patch.object(relay, 'SUPERSESSION_RULINGS', {}))
                if case == 'packet_drift': (self.jobs / self.job / 'PROMPT.md').write_text('drift')
                if case == 'ledger_drift': self.ledger.write_text('{}\n')
                if case == 'existing_replacement': (self.jobs / self.replacement).mkdir()
                if case == 'response_present': (self.jobs / self.job / 'RESPONSE.md').write_text('answer')
                if case == 'live_runner': stack.enter_context(mock.patch.object(relay, '_legacy_live_jobs', return_value={self.job}))
                if case in ('wrong_issue', 'nonresearch', 'unsent', 'legacy'):
                    st = dict(self.state)
                    key, value = {'wrong_issue': ('linear_child', 'GLP-336'),
                                  'nonresearch': ('job_class', 'patch-draft'),
                                  'unsent': ('sent', False), 'legacy': ('binding_version', 1)}[case]
                    st[key] = value
                    relay.job_write(self.job, st)
                before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                # Lock files are coordination, not job/ruling/journal custody.
                with self.assertRaisesRegex(SystemExit, 'SUPERSESSION_REFUSED'):
                    if case == 'bad_authority':
                        relay.supersede_research(self.job, RULING, apply=True, authorize_preimage='0' * 64)
                    else: self.call(True)
                after = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file() and '/locks/' not in str(p)}
                self.assertEqual(after, {p: b for p, b in before.items() if '/locks/' not in str(p)})
                for p in list(self.root.rglob('*')):
                    if p.is_file() and p not in saved: p.unlink()
                for p, b in saved.items(): p.write_bytes(b)
                replacement = self.jobs / self.replacement
                if replacement.exists(): replacement.rmdir()

    def test_interrupted_apply_remains_active_and_retry_blocked(self):
        real = relay.ledger_append
        def interrupt(cfg, row):
            if row.get('phase') == 'APPLIED': raise OSError('injected journal interruption')
            return real(cfg, row)
        with mock.patch.object(relay, 'ledger_append', side_effect=interrupt):
            with self.assertRaisesRegex(SystemExit, 'SUPERSESSION_REFUSED'):
                self.call(True)
        st = relay.job_state(self.job)
        self.assertEqual(st['status'], 'SUPERSEDED')
        self.assertFalse(relay._verified_supersession(st))
        self.assertEqual([r['job'] for r in relay.active_registry()], [self.job])
        with self.assertRaisesRegex(SystemExit, 'INCOMPLETE_OR_CHANGED_HISTORY'):
            self.call(True)

    def test_tampered_receipt_never_releases_capacity(self):
        self.call(True)
        original = relay.job_state(self.job)
        for key, value in [('retry_blocked', False), ('sent', False), ('tab', 'changed')]:
            st = {**original, key: value}; relay.job_write(self.job, st)
            self.assertFalse(relay._verified_supersession(st))
            self.assertEqual(len(relay.active_registry()), 1)
        relay.job_write(self.job, original)
        copied = self.jobs / 'copied-history'; copied.mkdir()
        (copied / 'JOB.json').write_text(json.dumps(original))
        self.assertEqual([r['job'] for r in relay.active_registry()], ['copied-history'])
        (copied / 'JOB.json').unlink(); copied.rmdir()
        self.ledger.write_bytes(self.ledger.read_bytes().rsplit(b'\n', 2)[0] + b'\n')
        self.assertFalse(relay._verified_supersession(original))
        self.assertEqual(len(relay.active_registry()), 1)

    def test_verified_retention_increases_tab_inventory_not_chat_admission(self):
        self.call(True)
        script = []
        with mock.patch.object(relay, 'bu', side_effect=lambda code, **kw: script.append(code) or 'RELAY_JSON:{}'):
            relay.bu_json('print("fixture")')
        self.assertIn('_tab_cap=5', script[0])  # 2 active + 2 normal overhead + 1 protected retired tab
        self.assertEqual(self.cfg['envelope']['max_concurrent_chats_per_account'], 2)


if __name__ == '__main__':
    unittest.main()
