import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/fleet_ingest.py'


class FleetTest(unittest.TestCase):
    def test_discovery_prunes_copies_before_descending(self):
        from unittest.mock import patch
        m = self.load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            live = root / 'lane/RESULT.json'
            live.parent.mkdir(parents=True)
            live.write_text('{}')
            excluded = ('sources', 'backups', 'preimages', 'browser-profile',
                        'worktree', '.venv-fixture', 'pytest', 'pytest-03',
                        'HARNESS', 'PREIMAGE', 'native-output', 'npm-cache')
            for name in excluded:
                path = root / name / 'deep/RESULT.json'
                path.parent.mkdir(parents=True)
                path.write_text('{}')
            checkout = root / 'arbitrary-clone'
            (checkout / '.git').mkdir(parents=True)
            (checkout / 'RESULT.json').write_text('{}')
            visited = []
            import os
            original = os.scandir
            def scandir(path):
                visited.append(Path(path))
                return original(path)
            with patch('os.scandir', side_effect=scandir):
                found = list(m.walk(root, {'RESULT.json'}))
            self.assertEqual(found, [live])
            self.assertFalse(any(root / name in visited for name in excluded))

    def test_poll_slow_verifiers_are_bounded_and_refused_not_passed(self):
        import sys
        import time
        from unittest.mock import patch
        m = self.load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            script = root / 'scripts/linear_business_proof.py'
            script.parent.mkdir()
            script.write_text('import time; time.sleep(2); print("GLP-358 PASS")')
            result = root / 'RESULT.json'
            result.write_text('{}')
            items = [m.event('native', 'GLP-358', 'sol-' + str(i),
                             result, [result], result) for i in range(6)]
            plan = root / 'plan.json'
            plan.write_text(json.dumps({'issues': [{'issue': 'GLP-358',
                'proof_cmd': ['python3', '-B', 'scripts/linear_business_proof.py', 'GLP-358'],
                'proof_timeout_s': 300}]}))
            real_run = m.run
            launched = []
            def run(argv, cwd, timeout, output_limit=4000):
                if argv[0] == 'gh':
                    return {'rc': 0, 'stdout': '[]', 'stderr': ''}
                launched.append(timeout)
                return real_run([sys.executable, str(script)], cwd, timeout)
            start = time.monotonic()
            with patch.object(m, 'run', side_effect=run), \
                    patch.object(m, 'MAX_COMMAND_SECONDS', .06, create=True), \
                    patch.object(m, 'POLL_COMMAND_BUDGET_SECONDS', .18, create=True), \
                    patch.object(m, 'discover', return_value=(items, [])):
                summary = m.poll(root, root / 'live', plan, root / 'config', timeout=2)
            self.assertLess(time.monotonic() - start, 1.0)
            self.assertLessEqual(len(launched), 4)
            self.assertTrue(all(t <= .06 for t in launched))
            self.assertEqual(summary['statuses'], {'REDISPATCH': 6})
            for path in (root / 'reports/ops/fleet-inbox').glob('GLP-*/*/INGEST.json'):
                row = m.read(path)
                self.assertIn('REFUSED', row['failure_text'])
                self.assertNotEqual(row['status'], 'READY_TO_CLOSE')
                self.assertEqual(row['checks']['proof']['rc'], 124)

            # Deferred receipts precede attempted timeouts on the next poll;
            # retries need no operator --retry and cannot starve behind lane 0.
            order = []
            original_ingest = m.ingest
            def ingest(root, item, policy, **kwargs):
                order.append(item['lane'])
                return original_ingest(root, item, policy, **kwargs)
            with patch.object(m, 'run', side_effect=run), \
                    patch.object(m, 'MAX_COMMAND_SECONDS', .06), \
                    patch.object(m, 'POLL_COMMAND_BUDGET_SECONDS', .18), \
                    patch.object(m, 'discover', return_value=(items, [])), \
                    patch.object(m, 'ingest', side_effect=ingest):
                summary = m.poll(root, root / 'live', plan, root / 'config', timeout=2)
            self.assertNotEqual(order[0], 'sol-0')
            self.assertEqual(summary['statuses'], {'REDISPATCH': 6})

    def test_timeout_retry_can_pass_only_after_real_success_then_cache(self):
        m = self.load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = root / 'RESULT.json'
            result.write_text('{}')
            item = m.event('native', 'GLP-358', 'sol', result, [result], result)
            policy = {'proof_cmd': ['stat', str(result)]}
            calls = []
            def runner(argv, cwd, timeout):
                calls.append(argv)
                if len(calls) == 1:
                    return {'rc': 124, 'stdout': '', 'stderr': 'REFUSED: timed out'}
                return {'rc': 0, 'stdout': 'GLP-358 PASS', 'stderr': ''}
            first = m.ingest(root, item, policy, runner=runner)
            second = m.ingest(root, item, policy, runner=runner)
            third = m.ingest(root, item, policy, runner=runner)
            self.assertEqual(first['status'], 'REDISPATCH')
            self.assertEqual(second['status'], 'READY_TO_CLOSE')
            self.assertEqual(second, third)
            self.assertEqual(len(calls), 4)
            history = root / 'reports/ops/fleet-inbox/GLP-358/sol/history'
            self.assertEqual(len(list(history.glob('*.json'))), 2)
            self.assertEqual(m.read(history / (first['fingerprint'] + '.json')), first)

    def test_timeout_reaps_verifier_helpers_holding_output_pipe(self):
        import sys
        import time
        m = self.load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            marker = root / 'helper-effect'
            child = 'import time; from pathlib import Path; time.sleep(.4); Path(%r).write_text("late")' % str(marker)
            parent = 'import subprocess, sys, time; subprocess.Popen([sys.executable, "-c", %r]); time.sleep(2)' % child
            start = time.monotonic()
            result = m.run([sys.executable, '-c', parent], root, .08)
            self.assertLess(time.monotonic() - start, .4)
            self.assertEqual(result['rc'], 124)
            self.assertIn('REFUSED', result['stderr'])
            time.sleep(.45)
            self.assertFalse(marker.exists())

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
                # Native _build_result_entry emits failed/error and interrupted;
                # _update_manifest persists them with a delegation completed marker.
                # Historical terminal failures are not live-task vetoes on a retry.
                previous = live / 'deleg_previous/manifest.json'
                previous.parent.mkdir()
                for status, reason in (('failed', 'error'), ('failed', 'completed'),
                                       ('failed', 'max_iterations'), ('error', 'error'),
                                       ('interrupted', 'interrupted'), ('timeout', 'timeout')):
                    with self.subTest(terminal_retry=status):
                        prior = json.loads(json.dumps(native))
                        prior['tasks'][0].update(status=status, exit_reason=reason)
                        previous.write_text(json.dumps(prior))
                        older = manifest.stat().st_mtime_ns - 10000000
                        os.utime(previous, ns=(older, older))
                        retried, errors = discover()
                        self.assertEqual(errors, [])
                        self.assertEqual(len(retried), 1, 'terminal failure vetoed completed retry')
                        self.assertEqual(retried[0]['source_failure'], '')
                        saved = manifest.read_bytes()
                        manifest.unlink()
                        self.assertEqual(discover()[0], [], 'failed-only custody is not completion')
                        manifest.write_bytes(saved)
                        for pid_owner in (prior, prior['tasks'][0]):
                            pid_owner['pid'] = os.getpid()
                            previous.write_text(json.dumps(prior))
                            self.assertEqual(discover()[0], [], 'live PID overrides failure status')
                            del pid_owner['pid']
                        prior.pop('completed')
                        previous.write_text(json.dumps(prior))
                        self.assertEqual(discover()[0], [], 'unfinalized failure must fail closed')
                prior = json.loads(json.dumps(native))
                prior['tasks'][0]['status'] = 'unknown-runtime-state'
                previous.write_text(json.dumps(prior))
                self.assertEqual(discover()[0], [], 'unknown state must fail closed')
                previous.unlink()
                # Neither shared issue registration nor unstable reads authenticate a result.
                shared = json.loads(json.dumps(config))
                shared['issues']['GLP-93'] = dict(shared['issues']['GLP-336'])
                self.assertEqual(discover(shared)[0], [])
                # Reproduce RV529's actual reader interleaving, never fabricate
                # digest values. Endpoint hashes cannot bless other parsed bytes.
                real_read_bytes, real_read_text = Path.read_bytes, Path.read_text
                for target in (manifest, result):
                    for reader_name in ('read_text', 'read_bytes'):
                        with self.subTest(consumed_buffer=target.name, reader=reader_name):
                            result.write_text(json.dumps(payload))
                            stale = json.loads(json.dumps(native if target == manifest else payload))
                            if target == manifest:
                                stale.pop('completed')
                                stale['tasks'][0]['status'] = 'running'
                                stale['tasks'][0].pop('exit_reason')
                            else:
                                stale['status'] = 'REDISPATCH_NEEDED'
                            target.write_text(json.dumps(stale))
                            if target == result:
                                save_native()  # genuine ordering: RESULT precedes terminal manifest
                            original_bytes = real_read_bytes(target)
                            terminal_bytes = json.dumps(native if target == manifest else payload).encode()
                            original_reader = real_read_text if reader_name == 'read_text' else real_read_bytes
                            consumed = []
                            def transient_read(path, *args, **kwargs):
                                if path == target and not consumed:
                                    st = path.stat()
                                    path.write_bytes(terminal_bytes)
                                    try:
                                        value = original_reader(path, *args, **kwargs)
                                        consumed.append(value)
                                        return value
                                    finally:
                                        path.write_bytes(original_bytes)
                                        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
                                return original_reader(path, *args, **kwargs)
                            with patch.object(Path, reader_name, transient_read):
                                raced, errors = discover()
                            self.assertEqual(errors, [])
                            self.assertEqual(real_read_bytes(target), original_bytes)
                            self.assertFalse([e for e in raced if not e['source_failure']],
                                             'parsed terminal bytes authenticated by nonterminal hash')
                            if reader_name == 'read_bytes':
                                self.assertTrue(consumed, 'exercise the snapshot byte-reader path')
                                self.assertEqual(raced, [], 'subsequent drift must refuse the snapshot')
                            result.write_text(json.dumps(payload))
                            save_native()
                # Persistent drift also refuses, with actual bytes, not a fake hash.
                for target in (manifest, result):
                    with self.subTest(persistent_drift=target.name):
                        consumed = []
                        def changing_read(path, *args, **kwargs):
                            value = real_read_bytes(path, *args, **kwargs)
                            if path == target and not consumed:
                                consumed.append(value)
                                path.write_bytes(value + b' ')
                            return value
                        with patch.object(Path, 'read_bytes', changing_read):
                            self.assertEqual(discover()[0], [])
                        result.write_text(json.dumps(payload))
                        save_native()
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
