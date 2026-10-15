import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/fleet_ingest.py'


class FleetTest(unittest.TestCase):
    def test_completion_runs_checks_once_and_writes_stable_row(self):
        self.assertTrue(SOURCE.exists(), 'fleet ingestion producer is missing')
        spec = importlib.util.spec_from_file_location('fleet_ingest', SOURCE)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = root / 'reports/ops/native/RESULT.json'
            result.parent.mkdir(parents=True)
            result.write_text(json.dumps({'issue': 'GLP-44', 'status': 'DONE_READY_TO_CLOSE'}))
            calls = []
            def runner(argv, cwd, timeout):
                calls.append(argv)
                return {'rc': 0, 'stdout': 'GLP-44 PASS rows=12', 'stderr': ''}
            event = m.event('native', 'GLP-44', 'sol', result, [result], result)
            plan = {'proof_cmd': ['stat', '-c', '%s', str(result)]}
            row = m.ingest(root, event, plan, runner)
            self.assertEqual(row['status'], 'READY_TO_CLOSE')
            self.assertEqual(calls[1][-1], '--check-only')
            target = root / 'reports/ops/fleet-inbox/GLP-44/sol/INGEST.json'
            before = target.read_bytes()
            m.ingest(root, event, plan, runner)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(len(calls), 2)
    def load(self):
        spec = importlib.util.spec_from_file_location('fleet_ingest', SOURCE)
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_registered_root_lane_requires_current_terminal_native_custody(self):
        """Private replay of the real native manifest shape, never live ingestion."""
        import os
        from unittest.mock import patch
        m = self.load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = root / 'lanes/AN-OWNER/RESULT.json'
            result.parent.mkdir(parents=True)
            payload = {'issue': 'GLP-336', 'owner': 'AN-OWNER',
                       'status': 'DONE_READY_TO_CLOSE'}
            result.write_text(json.dumps(payload))
            live = root / 'runtime-owned-live'
            manifest = live / 'deleg_c9b812c8/manifest.json'
            manifest.parent.mkdir(parents=True)
            # Same keys/statuses as retained deleg_c9b812c8; only paths and
            # narrative are fixture-local. This is not a business receipt.
            native = {'delegation_id': 'deleg_c9b812c8',
                      'started': '2026-10-14 00:08:31', 'task_count': 1,
                      'model': 'gpt-6.1-sol', 'provider': 'openai-codex',
                      'tasks': [{'index': 0,
                                 'goal': f'AN-OWNER continuation / GLP336 retain {result}',
                                 'log': str(manifest.parent / 'task-0.log'),
                                 'status': 'completed', 'exit_reason': 'completed'}],
                      'completed': '2026-10-14 00:19:12'}
            def save_native():
                manifest.write_text(json.dumps(native))
            save_native()
            config = {'issues': {'GLP-336': {'worker_result': 'lanes/AN-OWNER/RESULT.json'}}}
            def discover(cfg=config):
                return m.discover(root, live, cfg, [])
            with patch.object(m, 'run', side_effect=AssertionError('effects denied')), \
                    patch('subprocess.run', side_effect=AssertionError('subprocess denied')), \
                    patch('socket.socket', side_effect=AssertionError('network denied')):
                self.assertEqual(discover({})[0], [])
                items, errors = discover()
                self.assertEqual(errors, [])
                self.assertEqual(len(items), 1, 'registered root-lane terminal was falsely omitted')
                item = items[0]
                self.assertEqual(item['worker_result'], str(result))
                self.assertEqual(item['issue'], 'GLP-336')
                self.assertEqual(item['evidence'], {str(result): m.digest(result),
                                                   str(manifest): m.digest(manifest)})
                # Config attachment is not proof, and terminal status is not READY.
                calls = []
                def refused(argv, cwd, timeout):
                    calls.append(argv)
                    return {'rc': 2, 'stdout': '', 'stderr': 'fixture: acceptance unavailable'}
                row = m.ingest(root, item, config['issues']['GLP-336'], refused)
                self.assertEqual(row['status'], 'REDISPATCH')
                self.assertEqual(row['linear_mutations'], 0)
                self.assertEqual(calls[-1][-1], '--check-only')
                self.assertNotIn('--apply', calls[-1])
                self.assertEqual(m.ingest(root, item, config['issues']['GLP-336'], refused), row)
                self.assertEqual(len(calls), 1)
                with self.assertRaisesRegex(ValueError, 'lane collision'):
                    m.ingest(root, dict(item, identity='different-source'), {}, refused)
                active = live / 'deleg_active/manifest.json'
                for change in ('running-task', 'running-owner-without-path',
                               'running-manifest-pid', 'running-task-pid',
                               'running-result-pid', 'missing-terminal', 'wrong-issue',
                               'wrong-owner', 'unbound', 'post-terminal-change', 'symlink'):
                    with self.subTest(change=change):
                        original = json.loads(json.dumps(native))
                        if change in {'running-task', 'running-owner-without-path'}:
                            active.parent.mkdir(parents=True, exist_ok=True)
                            task = dict(native['tasks'][0], status='running')
                            if change == 'running-owner-without-path':
                                task['goal'] = 'AN-OWNER / GLP336 same current owner'
                            active.write_text(json.dumps({'tasks': [task]}))
                        elif change == 'running-manifest-pid': native['pid'] = os.getpid()
                        elif change == 'running-task-pid': native['tasks'][0]['pid'] = os.getpid()
                        elif change == 'running-result-pid': result.write_text(json.dumps(dict(payload, pid=os.getpid())))
                        elif change == 'missing-terminal': native.pop('completed')
                        elif change == 'wrong-issue': result.write_text(json.dumps(dict(payload, issue='GLP-93')))
                        elif change == 'wrong-owner': native['tasks'][0]['goal'] = f'REVIEW / GLP336 read {result}'
                        elif change == 'unbound': native['tasks'][0]['goal'] = 'AN-OWNER / GLP336 no exact result pointer'
                        elif change == 'symlink':
                            result.rename(result.with_name('saved.json'))
                            result.symlink_to(result.with_name('saved.json'))
                        save_native()
                        if change == 'post-terminal-change':
                            newer = manifest.stat().st_mtime_ns + 1000000
                            os.utime(result, ns=(newer, newer))
                        self.assertEqual(discover()[0], [])
                        if change in {'running-task', 'running-owner-without-path'}: active.unlink()
                        if change == 'symlink':
                            result.unlink()
                            result.with_name('saved.json').rename(result)
                        result.write_text(json.dumps(payload))
                        native = original
                        save_native()
                # Neither shared issue registration nor unstable reads authenticate a result.
                shared = json.loads(json.dumps(config))
                shared['issues']['GLP-93'] = dict(shared['issues']['GLP-336'])
                self.assertEqual(discover(shared)[0], [])
                real_digest = m.digest
                reads = []
                def changing_digest(path):
                    sha = real_digest(path)
                    if path == result:
                        reads.append(sha)
                        if len(reads) > 1:
                            return '0' * 64
                    return sha
                with patch.object(m, 'digest', side_effect=changing_digest):
                    self.assertEqual(discover()[0], [])
                # Snapshot changes must not silently become new authenticated bytes.
                item = discover()[0][0]
                result.write_text(json.dumps(dict(payload, status='REDISPATCH_NEEDED')))
                changed = m.ingest(root, item, config['issues']['GLP-336'], refused, retry=True)
                self.assertEqual(changed['status'], 'REDISPATCH')
                self.assertIn('native evidence changed', changed['failure_text'])

    def test_discovery_native_codex_pro_requires_terminal_evidence(self):
        m = self.load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            def save(rel, data):
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(data))
                return p
            native = save('reports/ops/wave/RESULT.json', {'issue': 'GLP-44', 'lane': 'sol', 'pid': 999999999, 'status': 'DONE'})
            live = save('reports/ops/wave/live/RESULT.json', {'issue': 'GLP-44', 'pid': __import__('os').getpid(), 'status': 'DONE'})
            save('reports/ops/glp44-ultra/RUN.json', {'finished_at': '2026-10-09', 'exit_code': 0, 'model': 'gpt-6-astra', 'response_exists': True})
            (root / 'reports/ops/glp44-ultra/RESPONSE.md').write_text('completed review')
            save('ops/gpt6pro-relay/route/queue/job.json', {'issue': 'GLP-44', 'job': 'job', 'state': 'complete'})
            out = save('reports/ops/gpt6pro-relay/jobs/job/outbox/OUTPUT-MANIFEST.json', {'files': {}})
            save('reports/ops/gpt6pro-relay/jobs/job/JOB.json', {'fetch_verification': {'state': 'VERIFIED', 'manifest_sha256': m.digest(out)}, 'source_reply_sha256': ''})
            (out.parent.parent / 'RESPONSE.md').write_text('reply')
            state = m.read(out.parent.parent / 'JOB.json')
            state['source_reply_sha256'] = m.digest(out.parent.parent / 'RESPONSE.md')
            save('reports/ops/gpt6pro-relay/jobs/job/JOB.json', state)
            events, errors = m.discover(root, root / 'no-live', {}, [])
            self.assertEqual({e['source'] for e in events}, {'native', 'codex', 'pro'})
            self.assertNotIn(str(live), [e['identity'] for e in events])
            self.assertIn(str(native), [e['identity'] for e in events])
            out.write_text('changed bytes')
            events, errors = m.discover(root, root / 'no-live', {}, [])
            self.assertIn('OUTBOX', str(errors))
            self.assertNotIn('pro', {e['source'] for e in events})
    def test_dot_pr_registered_cron_and_unsafe_proof(self):
        m = self.load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / 'reports/ops/dot-tickets/ticket/CONFIG.json'
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({'issue': 'GLP-44', 'dot': 'rook'}))
            (cfg.parent / 'REPLY.md').write_text('real reply file')
            receipt = root / 'reports/ops/n8n-to-cron-20260924/runs/a.json'
            receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({'bridge': {'job_id': 'registered', 'status': 'done', 'rc': 0}}))
            bad = receipt.parent / 'bool-bridge.json'
            bad.write_text(json.dumps({'bridge': True}))
            config = {'cron_bindings': {'registered': {'issue': 'GLP-44', 'lane': 'analytics'}}}
            prs = [{'number': 1, 'url': 'https://github.com/a/b/pull/1', 'state': 'MERGED', 'headRefOid': 'a' * 40, 'labels': [{'name': 'fleet-ticket:GLP-44'}], 'statusCheckRollup': [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}]
            items, errors = m.discover(root, root / 'missing', config, prs)
            self.assertEqual({e['source'] for e in items}, {'dot', 'github', 'cron'})
            self.assertEqual(m.discover(root, root / 'missing', {}, [])[0][0]['source'], 'dot')
            for command in ['/home/ubuntu/untrusted/stat x', 'python3 scripts/send.py --apply', 'curl -X POST https://example.com', 'stat x; rm x', 'check receipts >= 3']:
                with self.assertRaises(ValueError):
                    m.proof_argv(command)
            cfg.write_text('{')
            items, errors = m.discover(root, root / 'missing', config, [])
            self.assertNotIn('dot', {e['source'] for e in items})
    def test_zero_exit_without_pass_is_not_ready(self):
        m = self.load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = root / 'RESULT.json'
            result.write_text('{}')
            item = m.event('native', 'GLP-44', 'sol', result, [result], result)
            row = m.ingest(root, item, {'proof_cmd': ['stat', str(result)]},
                lambda *args: {'rc': 0, 'stdout': 'GLP-44 Done old dry-run', 'stderr': ''})
            self.assertEqual(row['status'], 'REDISPATCH')
            self.assertIn('explicit PASS', row['failure_text'])

    def test_partial_proof_and_exact_proof_allowlist(self):
        m = self.load()
        self.assertEqual(m.proof_argv(['python3', 'scripts/fleet_plan_proof.py', 'GLP-356'])[-1], 'GLP-356')
        for bad in [['python3', 'scripts/fleet_plan_proof.py', 'GLP-356', '--apply'],
                    ['python3', 'scripts/glp_cogny_queue_drain.py', '--mode', 'tick']]:
            with self.assertRaises(ValueError):
                m.proof_argv(bad)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = root / 'RESULT.json'
            result.write_text('{}')
            item = m.event('native', 'GLP-44', 'sol', result, [result], result)
            row = m.ingest(root, item, {'proof_cmd': ['stat', str(result)],
                'proof_complete': False, 'proof_incomplete_reason': 'operator review still required'},
                lambda *args: {'rc': 0, 'stdout': 'GLP-44 PASS', 'stderr': ''})
            self.assertEqual(row['status'], 'REDISPATCH')
            self.assertIn('operator review still required', row['failure_text'])

    def test_failed_checks_and_changed_closer_are_revalidated(self):
        m = self.load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = root / 'RESULT.json'
            result.write_text('{}')
            item = m.event('native', 'GLP-44', 'sol', result, [result], result)
            policy = {'proof_cmd': ['stat', str(result)]}
            calls = []
            def runner(argv, cwd, timeout):
                calls.append(argv)
                return {'rc': 3, 'stderr': 'REFUSED required acceptance absent', 'stdout': ''}
            row = m.ingest(root, item, policy, runner)
            self.assertEqual(row['status'], 'REDISPATCH')
            self.assertIn('REFUSED required acceptance absent', row['failure_text'])
            closer = root / 'scripts/linear_close.py'
            closer.parent.mkdir()
            closer.write_text('# new no-write closer')
            row2 = m.ingest(root, item, policy, runner)
            self.assertNotEqual(row['fingerprint'], row2['fingerprint'])
            self.assertEqual(len(calls), 4)
            self.assertEqual(m.proof_argv(['curl', '--fail', '--silent', 'https://glpclinicscout.com/guides/a/'])[0], 'curl')
            with self.assertRaises(ValueError):
                m.proof_argv(['curl', 'https://glpclinicscout.com/go/a/'])
    def test_policy_can_bind_genuine_result_but_cannot_cross_issue(self):
        m = self.load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            advisory = root / 'ADVISORY.md'
            advisory.write_text('analysis only')
            result = root / 'RESULT.json'
            result.write_text(json.dumps({'issue': 'GLP-44', 'status': 'DONE_READY_TO_CLOSE'}))
            item = m.event('pro', 'GLP-44', 'pro', advisory, [advisory])
            policy = {'proof_cmd': ['stat', str(result)], 'worker_result': str(result)}
            def runner(argv, cwd, timeout):
                return {'rc': 0, 'stdout': 'GLP-44 PASS actual output', 'stderr': ''}
            row = m.ingest(root, item, policy, runner)
            self.assertEqual(row['status'], 'READY_TO_CLOSE')
            self.assertIn(str(result), row['evidence'])
            result.write_text(json.dumps({'issue': 'GLP-93', 'status': 'DONE_READY_TO_CLOSE'}))
            row = m.ingest(root, item, policy, runner)
            self.assertEqual(row['status'], 'REDISPATCH')
            self.assertIn('issue mismatch', row['failure_text'])
            policy['worker_result'] = '/outside/RESULT.json'
            row = m.ingest(root, item, policy, runner)
            self.assertEqual(row['status'], 'REDISPATCH')
            self.assertIsNone(row['worker_result'])
            self.assertNotIn('argv', row['checks']['linear_close'])
            self.assertEqual(m.binding({'issues': ['GLP-355']}, root), 'GLP-355')
    def test_changed_evidence_revokes_previous_ready_row(self):
        m = self.load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = root / 'RESULT.json'
            result.write_text('{}')
            item = m.event('native', 'GLP-44', 'sol', result, [result], result)
            m.ingest(root, item, {'proof_cmd': ['stat', str(result)]}, lambda *a: {'rc': 0, 'stdout': 'GLP-44 PASS valid', 'stderr': ''})
            result.write_text('changed')
            m.revoke_stale(root, [])
            row = m.read(root / 'reports/ops/fleet-inbox/GLP-44/sol/INGEST.json')
            self.assertEqual(row['status'], 'REDISPATCH')
            self.assertIn('source evidence changed', row['failure_text'])
    def test_poll_records_real_metrics_without_claiming_admission(self):
        from unittest.mock import patch
        m = self.load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            proc = root / 'proc'
            (proc / 'pressure').mkdir(parents=True)
            (proc / 'sys/vm').mkdir(parents=True)
            (proc / 'meminfo').write_text('MemAvailable: 8000000 kB\nSwapFree: 2000000 kB\n')
            (proc / 'pressure/memory').write_text('full avg10=0.20 avg60=0.10\n')
            (proc / 'vmstat').write_text('pswpin 12\npswpout 34\n')
            (proc / 'sys/vm/swappiness').write_text('10\n')
            budget = root / 'budget.json'
            budget.write_text('[{"pid": 1, "start": "2", "token": "x"}]')
            obs = m.admission_observation(proc, budget)
            self.assertEqual(obs['swap_io_pages'], 46)
            self.assertEqual(obs['reserved_slots_unvalidated'], 1)
            self.assertIsNone(obs['actual_memory_gate_refusals'])
            self.assertEqual(obs['errors'], [])
            with patch.object(m, 'run', return_value={'rc': 0, 'stdout': '[]', 'stderr': ''}), patch.object(m, 'admission_observation', return_value=obs):
                row = m.poll(root, root / 'live', root / 'plan', root / 'config')
                metrics = row['admission_metrics']
                self.assertEqual(metrics['full_acceptance'], 'UNPROVEN')
                self.assertEqual(metrics['observations'], [obs])
                row = m.poll(root, root / 'live', root / 'plan', root / 'config')
                self.assertEqual(len(row['admission_metrics']['observations']), 2)
            budget.write_text('{}')
            unknown = m.admission_observation(root / 'missing', budget)
            self.assertIsNone(unknown['mem_available_kb'])
            self.assertIsNone(unknown['reserved_slots_unvalidated'])
            self.assertTrue(unknown['errors'])
            stale = dict(obs, observed_at_utc='2000-01-01T00:00:00+00:00')
            metrics = m.admission_metrics({'admission_metrics': {'observations': [stale]}}, obs)
            self.assertEqual(metrics['observations'], [obs])

    def test_stat_thresholds_are_checked_not_just_exit_zero(self):
        m = self.load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = root / 'RESULT.json'
            p.write_text('{}')
            item = m.event('native', 'GLP-110', 'sol', p, [p], p)
            policy = {'proof_cmd': ['stat', '-c', '%s', str(p)], 'proof_stdout_minimums': [4000000001, 600001]}
            row = m.ingest(root, item, policy, lambda *args: {'rc': 0, 'stdout': '0\n0\n', 'stderr': ''})
            self.assertEqual(row['status'], 'REDISPATCH')
            self.assertIn('minimum', row['failure_text'])


if __name__ == '__main__':
    unittest.main()
