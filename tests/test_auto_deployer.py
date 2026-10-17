"""Offline fixtures only: no GitHub, installer, Discord or Linear transport."""
import copy
import contextlib
import io
import importlib.util
import hashlib
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/auto_deployer.py'
FIXTURE_DIRS = []


def documents(repo):
    lane = 'lanes/' + repo.split('/')[1]
    common = {'pr': f'https://github.com/{repo}/pull/7', 'head': 'a' * 40}
    author = json.dumps(common)
    receipt = json.dumps(dict(common, verdict='ACCEPT_SOURCE'))
    custody = json.dumps(dict(common, coordinator_session_id='Kael', author_session_id='Sol', reviewer_session_id='Astra',
                              author_result=lane + '/RESULT.json', author_result_sha256=hashlib.sha256(author.encode()).hexdigest(),
                              receipt=lane + '/RV7/RESULT.json', receipt_sha256=hashlib.sha256(receipt.encode()).hexdigest()))
    return [(lane + '/RESULT.json', author, 'Sol'), (lane + '/RV7/RESULT.json', receipt, 'Astra'),
            (lane + '/custody.json', custody, 'Kael')]


def load():
    spec = importlib.util.spec_from_file_location('auto_deployer', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Synthetic runtime-owned DB and tool records; never read the real host DB.
    module._fixture_dir = tempfile.TemporaryDirectory()
    FIXTURE_DIRS.append(module._fixture_dir)
    module.NATIVE_REVIEW_ROOT = root = Path(module._fixture_dir.name)
    module.NATIVE_REVIEW_DB = root / 'state.db'
    with sqlite3.connect(module.NATIVE_REVIEW_DB) as db:
        db.executescript('CREATE TABLE sessions (id TEXT, parent_session_id TEXT, model TEXT, model_config TEXT, ended_at REAL, end_reason TEXT); '
                         'CREATE TABLE messages (session_id TEXT, role TEXT, tool_calls TEXT, tool_call_id TEXT, tool_name TEXT, content TEXT, timestamp REAL);')
        for sid in ('Kael', 'Sol', 'Astra'):
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?)', (sid, None if sid == 'Kael' else 'Kael',
                       'gpt-6-astra-900k' if sid == 'Astra' else 'gpt-6.1-sol', json.dumps({'_delegate_from': 'Kael'}), 5, 'agent_close'))
        for repo in module.REPOS:
            for index, (path, content, sid) in enumerate(documents(repo)):
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                call_id = repo + str(index)
                call = {'id': call_id, 'function': {'name': 'write_file', 'arguments': json.dumps({'path': str(target), 'content': content})}}
                proof = {'verified': True, 'resolved_path': str(target), 'bytes_written': len(content.encode())}
                db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)', (sid, 'assistant', json.dumps([call]), None, None, None, 1))
                db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)', (sid, 'tool', None, call_id, 'write_file', json.dumps(proof), 2))
    return module


def fixture(repo='izaiahat/glp-site-candidate'):
    head = 'a' * 40
    docs = documents(repo)
    binding = '\nReceipt: ' + docs[1][0] + '\nReceipt-SHA256: ' + hashlib.sha256(docs[1][1].encode()).hexdigest()
    binding += '\nCustody: ' + docs[2][0] + '\nCustody-SHA256: ' + hashlib.sha256(docs[2][1].encode()).hexdigest()
    return {
        'number': 7, 'repo': repo, 'state': 'OPEN',
        'baseRefName': 'main', 'isDraft': False, 'headRefOid': head,
        'mergeStateStatus': 'CLEAN', 'body': 'fleet-ticket:GLP-358', 'labels': [],
        'headCommittedAt': '2026-10-16T01:00:00Z',
        'reviews': [{'id': 11, 'state': 'COMMENTED', 'commit_id': head,
                     'submitted_at': '2026-10-16T02:00:00Z',
                     'body': 'ACCEPT\nHead: ' + head + '\nReviewer: Astra\nSource-author: Sol\nIndependent: true\nNon-author: true' + binding}],
        'checks': [{'name': 'ci', 'head_sha': head, 'status': 'completed', 'conclusion': 'success',
                    'app': {'slug': 'github-actions'}, 'check_suite': {'id': 123}}],
        'expected_workflows': [{'path': '.github/workflows/ci.yml', 'jobs': ['ci']}],
        'statuses': [], 'required_contexts': [],
        'workflow_runs': [{'path': '.github/workflows/ci.yml', 'head_sha': head, 'event': 'pull_request',
                           'check_suite_id': 123, 'status': 'completed', 'conclusion': 'success'}],
    }


class FakeRemote:
    def __init__(self, pr):
        self.pr = pr
        self.merges = []
        self.readbacks = []

    def snapshot(self, repo, number):
        return copy.deepcopy(self.pr)

    def files(self, repo, number):
        return [{'filename': 'scripts/example.py', 'status': 'modified'}]

    def manifest(self, repo, head):
        return {'files': {'scripts/example.py': {'sha256': 'd' * 64}}}

    def merge(self, repo, number, head):
        self.merges.append((repo, number, head))

    def merged(self, repo, number, head):
        self.readbacks.append((repo, number, head))
        return {'head': head, 'merge_commit': 'b' * 40, 'merged_at': '2026-10-16T03:00:00Z'}


class AutoDeployerTests(unittest.TestCase):
    def tearDown(self):
        while FIXTURE_DIRS:
            FIXTURE_DIRS.pop().cleanup()

    def test_review_counterexample_cannot_self_assert_independent_identity(self):
        m = load()
        for login, author in [('untrusted', 'Sol'), ('izaiahat', 'imaginary-author')]:
            with self.subTest(login=login), tempfile.TemporaryDirectory() as tmp:
                pr = fixture()
                pr['reviews'][0]['user'] = {'login': login}
                pr['reviews'][0]['body'] = pr['reviews'][0]['body'].replace('Source-author: Sol', 'Source-author: ' + author)
                pr['reviews'][0]['body'] = pr['reviews'][0]['body'].split('\nReceipt:')[0]
                remote = FakeRemote(pr)
                result = m.Deployer(Path(tmp), remote, notify=lambda _: None).process(pr)
                self.assertEqual(result['status'], 'REFUSED')
                self.assertEqual(remote.merges, [])
                self.assertIn('review field Receipt', result['reason'])
        # A real-looking file/hash is still author-controlled. Authentication must
        # read successful writes and parent linkage from the separate runtime DB.
        for attack in ('missing-db', 'author-wrote-custody', 'author-wrote-review', 'same-session',
                       'unrelated-reviewer', 'running-reviewer', 'wrong-model', 'altered-receipt', 'forged-author'):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as tmp:
                m = load()
                pr = fixture()
                if attack == 'missing-db':
                    m.NATIVE_REVIEW_DB = Path(tmp) / 'absent.db'
                elif attack == 'altered-receipt':
                    (m.NATIVE_REVIEW_ROOT / documents(pr['repo'])[1][0]).write_text('{"verdict":"ACCEPT"}')
                else:
                    with sqlite3.connect(m.NATIVE_REVIEW_DB) as db:
                        if attack == 'author-wrote-custody':
                            db.execute("UPDATE messages SET session_id='Sol' WHERE session_id='Kael'")
                        elif attack == 'author-wrote-review':
                            db.execute("UPDATE messages SET session_id='Sol' WHERE session_id='Astra'")
                        elif attack == 'same-session':
                            pr['reviews'][0]['body'] = pr['reviews'][0]['body'].replace('Source-author: Sol', 'Source-author: Astra')
                        elif attack == 'unrelated-reviewer':
                            db.execute("UPDATE sessions SET parent_session_id='other' WHERE id='Astra'")
                        elif attack == 'running-reviewer':
                            db.execute("UPDATE sessions SET ended_at=NULL WHERE id='Astra'")
                        elif attack == 'wrong-model':
                            db.execute("UPDATE sessions SET model='gpt-6.1-sol' WHERE id='Astra'")
                        elif attack == 'forged-author':
                            db.execute("DELETE FROM messages WHERE session_id='Sol'")
                remote = FakeRemote(pr)
                result = m.Deployer(Path(tmp), remote, notify=lambda _: None).process(pr)
                self.assertEqual(result['status'], 'REFUSED')
                self.assertIn('custody', result['reason'])
                self.assertEqual(remote.merges, [])

    def test_ci_counterexample_successful_subset_and_legacy_status_refuse(self):
        m = load()
        for missing, status in [(True, None), (False, 'failure'), (False, 'pending')]:
            with self.subTest(missing=missing, status=status), tempfile.TemporaryDirectory() as tmp:
                pr = fixture()
                if missing:
                    pr['expected_workflows'].append({'path': '.github/workflows/business-source-ci.yml',
                                                    'jobs': ['package-only-exact-head', 'python-tests']})
                pr['statuses'] = [] if status is None else [{'context': 'legacy-ci', 'state': status,
                    'url': f'https://api.github.com/repos/{pr["repo"]}/statuses/{pr["headRefOid"]}'}]
                remote = FakeRemote(pr)
                result = m.Deployer(Path(tmp), remote, notify=lambda _: None).process(pr)
                self.assertEqual(result['status'], 'REFUSED')
                self.assertIn('workflow' if missing else 'commit-status', result['reason'])
                self.assertEqual(remote.merges, [])
        for attack in ('missing-job', 'wrong-suite', 'wrong-app', 'pending-workflow', 'wrong-workflow-head',
                       'empty-policy', 'missing-context', 'wrong-status-head'):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as tmp:
                pr = fixture()
                if attack == 'missing-job':
                    pr['expected_workflows'][0]['jobs'].append('absent')
                elif attack == 'wrong-suite':
                    pr['checks'][0]['check_suite']['id'] = 999
                elif attack == 'wrong-app':
                    pr['checks'][0]['app']['slug'] = 'untrusted'
                elif attack == 'pending-workflow':
                    pr['workflow_runs'][0]['status'] = 'in_progress'
                elif attack == 'wrong-workflow-head':
                    pr['workflow_runs'][0]['head_sha'] = 'c' * 40
                elif attack == 'empty-policy':
                    pr['expected_workflows'] = []
                elif attack == 'missing-context':
                    pr['required_contexts'] = ['missing-legacy-context']
                elif attack == 'wrong-status-head':
                    pr['statuses'] = [{'context': 'legacy', 'state': 'success', 'sha': 'c' * 40}]
                remote = FakeRemote(pr)
                result = m.Deployer(Path(tmp), remote, notify=lambda _: None).process(pr)
                self.assertEqual(result['status'], 'REFUSED')
                self.assertEqual(remote.merges, [])
        positive = fixture()
        positive['statuses'] = [{'context': 'legacy-ci', 'state': 'success',
            'url': f'https://api.github.com/repos/{positive["repo"]}/statuses/{positive["headRefOid"]}'}]
        positive['required_contexts'] = ['legacy-ci']
        self.assertEqual(m.admission(positive)['issue'], 'GLP-358')
        workflows = SCRIPT.parent.parent / '.github/workflows'
        core = (workflows / 'business-source-ci.yml').read_text()
        filtered = (workflows / 'auto-deployer.yml').read_text()
        self.assertEqual(m.workflow_jobs(core, ['README.md'], 'main'), ['package-only-exact-head', 'python-tests'])
        self.assertEqual(m.workflow_jobs(filtered, ['scripts/auto_deployer.py'], 'main'), ['offline-exact-head'])
        self.assertEqual(m.workflow_jobs(filtered, ['README.md'], 'main'), [])
        self.assertTrue(m.path_match('tests/a.py', ['tests/**', '!tests/ignored/**']))
        self.assertFalse(m.path_match('tests/ignored/a.py', ['tests/**', '!tests/ignored/**']))
        # Deleting/weakening a workflow in the proposed head cannot erase base CI.
        github = m.GitHub()
        def policy_api(endpoint):
            import base64
            if '/git/trees/' in endpoint:
                name = 'business-source-ci' if '/base?' in endpoint else 'auto-deployer'
                return {'truncated': False, 'tree': [{'path': '.github/workflows/' + name + '.yml',
                        'type': 'blob', 'mode': '100644', 'sha': name}]}
            return {'content': base64.b64encode((core if endpoint.endswith('business-source-ci') else filtered).encode()).decode()}
        github.api = policy_api
        policy = github.expected_workflows(m.REPOS[0], 'base', 'head', ['scripts/auto_deployer.py'], 'main')
        self.assertEqual(len(policy), 2)
        # Exercise the actual snapshot transport: a failed legacy status must be
        # queried, retained, and refused even with otherwise complete success.
        pr = fixture()
        endpoints = []
        github = m.GitHub()
        github.files = lambda *a: []
        github.expected_workflows = lambda *a: pr['expected_workflows']
        github.required_contexts = lambda *a: []
        github.api = lambda endpoint: ({'head': {'sha': pr['headRefOid']}, 'base': {'sha': 'b' * 40, 'ref': 'main'},
            'state': 'open', 'draft': False, 'body': pr['body'], 'changed_files': 0, 'labels': []}
            if '/pulls/' in endpoint else {'commit': {'committer': {'date': pr['headCommittedAt']}}})
        def pages(endpoint, field=None):
            endpoints.append(endpoint)
            if '/statuses?' in endpoint:
                return [{'context': 'legacy-ci', 'state': 'failure',
                         'url': f'https://api.github.com/repos/{pr["repo"]}/statuses/{pr["headRefOid"]}'}]
            if '/check-runs?' in endpoint:
                return pr['checks']
            return pr['workflow_runs'] if '/actions/runs?' in endpoint else pr['reviews']
        github.pages = pages
        with patch.object(m, 'run', return_value=json.dumps({'headRefOid': pr['headRefOid'], 'mergeStateStatus': 'CLEAN'})):
            observed = github.snapshot(pr['repo'], 7)
        self.assertTrue(any('/statuses?' in endpoint for endpoint in endpoints))
        with tempfile.TemporaryDirectory() as tmp:
            remote = FakeRemote(observed)
            result = m.Deployer(Path(tmp), remote, notify=lambda _: None).process(observed)
            self.assertEqual(result['status'], 'REFUSED')
            self.assertIn('commit-status', result['reason'])
            self.assertEqual(remote.merges, [])

    def test_pagination_works_on_installed_gh_without_slurp_and_keeps_all_checks(self):
        m = load()
        remote = m.GitHub()
        with patch.object(m, 'run', return_value='[\n{"number": 1}\n]\n[{"number":2}]') as runner:
            self.assertEqual(remote.open_prs(m.REPOS[0]), [1, 2])
            self.assertNotIn('--slurp', runner.call_args.args[0])
        with patch.object(m, 'run', return_value='{"check_runs":[{"conclusion":"success"}]}\n{"check_runs":[{"conclusion":"failure"}]}'):
            self.assertEqual(len(remote.pages('endpoint', 'check_runs')), 2)

    def test_cli_dry_run_lists_both_repos_and_has_zero_local_or_external_effects(self):
        m = load()
        visited = []
        class Remote(FakeRemote):
            def open_prs(self, repo):
                visited.append(repo)
                return [7]
            def snapshot(self, repo, number):
                return dict(fixture(repo), changed_files=1)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as output:
            with patch.object(m, 'run', side_effect=AssertionError('no subprocess effects in fixture dry run')):
                self.assertEqual(m.main(['--dry-run'], remote=Remote(fixture()), root=Path(tmp)), 0)
            self.assertEqual(visited, list(m.REPOS))
            self.assertEqual(list(Path(tmp).iterdir()), [])
            self.assertEqual(len(output.getvalue().splitlines()), 2)
            self.assertTrue(all(json.loads(line)['status'] == 'WOULD_MERGE' for line in output.getvalue().splitlines()))

    def test_github_cas_command_and_exact_merge_parent_readback(self):
        m = load()
        calls = []
        def runner(argv, *args, **kwargs):
            calls.append(argv)
            return ''
        with patch.object(m, 'run', side_effect=runner):
            m.GitHub().merge(m.REPOS[0], 7, 'a' * 40)
        self.assertEqual(calls, [['gh', 'pr', 'merge', '7', '--repo', m.REPOS[0], '--merge', '--match-head-commit', 'a' * 40]])
        remote = m.GitHub()
        remote.api = lambda endpoint: ({'merged': True, 'head': {'sha': 'a' * 40}, 'merge_commit_sha': 'b' * 40, 'merged_at': 'now'}
            if '/pulls/' in endpoint else {'parents': [{'sha': 'c' * 40}]})
        with self.assertRaises(ValueError):
            remote.merged(m.REPOS[0], 7, 'a' * 40)

    def test_outbox_recovers_absent_inbox_without_replaying_merge_or_send(self):
        m = load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            messages = []
            remote = FakeRemote(fixture())
            deployer = m.Deployer(root, remote, notify=messages.append)
            # Model a crash immediately after the immutable final receipt.
            receipt = {'schema': 'auto-deployer-v1', 'repo': m.REPOS[1], 'pr': 7, 'head': 'a' * 40,
                       'issue': 'GLP-358', 'status': 'WAITING_LIVE_OUTPUT', 'live_output_proven': False}
            path = deployer.base / m.utc() / 'glp-site-candidate-7.json'
            m.immutable(path, receipt)
            self.assertEqual(deployer.reconcile(), [])
            self.assertEqual(len(messages), 1)
            self.assertEqual(deployer.reconcile(), [])
            self.assertEqual(len(messages), 1)
            self.assertEqual(remote.merges, [])
            self.assertEqual(len((root / 'reports/ops/fleet-inbox/auto-deployer.jsonl').read_text().splitlines()), 1)

    def test_admission_refuses_each_unsafe_input_without_effects(self):
        m = load()
        cases = {}
        def case(name, edit):
            pr = fixture()
            edit(pr)
            cases[name] = pr
        case('stale-head review', lambda p: p['reviews'][0].update(commit_id='c' * 40))
        case('old review clock', lambda p: p['reviews'][0].update(submitted_at='2026-10-16T00:00:00Z'))
        case('equal review clock', lambda p: p['reviews'][0].update(submitted_at=p['headCommittedAt']))
        case('failing CI', lambda p: p['checks'][0].update(conclusion='failure'))
        case('empty CI', lambda p: p.update(checks=[]))
        case('pending CI', lambda p: p['checks'][0].update(status='in_progress'))
        case('wrong CI head', lambda p: p['checks'][0].update(head_sha='c' * 40))
        case('missing fleet-ticket', lambda p: p.update(body='No ticket'))
        case('ambiguous fleet-ticket', lambda p: p.update(body='fleet-ticket:GLP-358\nfleet-ticket:GLP-359'))
        case('hold', lambda p: p.update(labels=[{'name': 'HOLD'}]))
        case('do not merge', lambda p: p.update(labels=[{'name': 'do-not-merge'}]))
        case('dirty', lambda p: p.update(mergeStateStatus='BEHIND'))
        case('draft', lambda p: p.update(isDraft=True))
        case('foreign repo', lambda p: p.update(repo='other/repo'))
        case('self review', lambda p: p['reviews'][0].update(body=p['reviews'][0]['body'].replace('Reviewer: Astra', 'Reviewer: Sol')))
        case('no independence', lambda p: p['reviews'][0].update(body=p['reviews'][0]['body'].replace('Independent: true', 'Independent: false')))
        case('GitHub approval', lambda p: p['reviews'][0].update(state='APPROVED'))
        case('verdict not prefix', lambda p: p['reviews'][0].update(body='Notes\n' + p['reviews'][0]['body']))
        case('newer rejection', lambda p: p['reviews'].append(dict(p['reviews'][0], id=12, body='CHANGES_REQUIRED', submitted_at='2026-10-16T02:01:00Z')))
        for name, pr in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                remote = FakeRemote(pr)
                result = m.Deployer(Path(tmp), remote, notify=lambda _: self.fail('alert')).process(pr)
                self.assertEqual(result['status'], 'REFUSED')
                self.assertEqual(remote.merges, [])
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_receipted_head_skipped_dry_run_no_writes_and_head_race(self):
        m = load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pr = fixture()
            remote = FakeRemote(pr)
            deployer = m.Deployer(root, remote, notify=lambda _: None)
            self.assertEqual(deployer.process(pr, dry_run=True)['status'], 'WOULD_MERGE')
            self.assertEqual(list(root.iterdir()), [])
            deployer.process(pr)
            before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(deployer.process(pr)['status'], 'SKIP_RECEIPTED')
            self.assertEqual(len(remote.merges), 1)
            self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()})
        with tempfile.TemporaryDirectory() as tmp:
            pr = fixture()
            remote = FakeRemote(dict(pr, headRefOid='c' * 40))
            result = m.Deployer(Path(tmp), remote, notify=lambda _: None).process(pr)
            self.assertEqual(result['status'], 'REFUSED')
            self.assertEqual(remote.merges, [])

    def test_uncertain_merge_is_read_back_once_never_retried(self):
        m = load()
        remote = FakeRemote(fixture())
        def uncertain(*args):
            remote.merges.append(args)
            raise TimeoutError('lost acknowledgement')
        remote.merge = uncertain
        with tempfile.TemporaryDirectory() as tmp:
            deployer = m.Deployer(Path(tmp), remote, notify=lambda _: None)
            result = deployer.process(fixture())
            self.assertEqual(result['status'], 'WAITING_LIVE_OUTPUT')
            deployer.process(fixture())
            self.assertEqual(len(remote.merges), 1)
            self.assertEqual(len(remote.readbacks), 1)

    def test_crash_intent_inspects_without_replaying_or_writing_in_dry_run(self):
        m = load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pr = fixture()
            remote = FakeRemote(pr)
            deployer = m.Deployer(root, remote, notify=lambda _: None)
            path = deployer.intent_path(pr)
            m.immutable(path, {'repo': pr['repo'], 'pr': 7, 'head': pr['headRefOid']})
            before = path.read_bytes()
            result = deployer.reconcile(dry_run=True)
            self.assertEqual(result[0]['status'], 'BLOCKED_UNCERTAIN')
            self.assertEqual(len(remote.readbacks), 1)
            self.assertEqual(remote.merges, [])
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(list(deployer.base.glob('*/*.json')), [])

    def test_business_scoped_dry_apply_dry0_and_uncertain_apply_inspection(self):
        m = load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / 'checkout'
            checkout.mkdir()
            (checkout / 'scripts').mkdir()
            (checkout / 'scripts/example.py').write_text('print(1)\n')
            calls = []
            def runner(argv, cwd, timeout=120):
                calls.append(argv)
                return 'DRY_RUN diffs=0 conflicts=0 tracked=1\n' if '--dry-run' in argv else 'APPLY diffs=1 conflicts=0 tracked=1\n'
            deployer = m.Deployer(root, FakeRemote(fixture()), notify=lambda _: None)
            result = deployer.install(checkout, ['scripts/example.py'], runner=runner)
            self.assertEqual(result['status'], 'INSTALLED_SOURCE')
            self.assertEqual([('--apply' in c) for c in calls], [False, True, False])
            for argv in calls:
                self.assertEqual(argv[argv.index('--only') + 1:], ['scripts/example.py'])
                self.assertNotIn('--adopt-reviewed-baseline', argv)
            calls.clear()
            def failed(argv, cwd, timeout=120):
                calls.append(argv)
                if '--apply' in argv:
                    raise TimeoutError('apply ack lost')
                return 'DRY_RUN diffs=1 conflicts=0 tracked=1\n'
            result = deployer.install(checkout, ['scripts/example.py'], runner=failed)
            self.assertEqual(result['status'], 'BLOCKED_UNCERTAIN')
            self.assertEqual(len(calls), 2)
            self.assertIn('scripts/example.py', result['target_inspection'])
            self.assertNotIn('example.py', result['target_inspection'])

    def test_runtime_path_selection_refuses_uncurated_deleted_and_never_global(self):
        m = load()
        manifest = {'files': {'scripts/a.py': {}, 'tests/test_a.py': {}, 'ops/policy.md': {}, 'systems/a/config.json': {}}}
        files = [{'filename': k, 'status': 'modified'} for k in manifest['files']]
        files += [{'filename': 'ops/rebuild-source-manifest.json', 'status': 'modified'}]
        self.assertEqual(m.runtime_paths(files, manifest), ['scripts/a.py', 'systems/a/config.json'])
        for bad in ('scripts/missing.py', '../scripts/a.py', '/scripts/a.py'):
            with self.assertRaises(ValueError):
                m.runtime_paths([{'filename': bad, 'status': 'modified'}], manifest)
        with self.assertRaises(ValueError):
            m.runtime_paths([{'filename': 'scripts/a.py', 'status': 'removed'}], manifest)
        with self.assertRaises(ValueError):
            m.runtime_paths([{'filename': 'scripts/a.py', 'previous_filename': 'scripts/b.py', 'status': 'renamed'}], manifest)
        self.assertEqual(m.runtime_paths([{'filename': 'README.md', 'status': 'modified'}], manifest), [])

    def test_business_process_dispatches_only_changed_runtime_after_merge(self):
        m = load()
        pr = dict(fixture('izaiahat/glp-business'), changed_files=1)
        remote = FakeRemote(pr)
        with tempfile.TemporaryDirectory() as tmp:
            deployer = m.Deployer(Path(tmp), remote, notify=lambda _: None)
            calls = []
            deployer.checkout = lambda repo, sha, **kwargs: calls.append(('checkout', repo, sha)) or Path(tmp)
            def install(checkout, paths):
                self.assertEqual(len(remote.readbacks), 1)
                calls.append(('install', paths))
                return {'status': 'INSTALLED_SOURCE', 'paths': paths}
            deployer.install = install
            result = deployer.process(pr)
            self.assertEqual(result['deployment']['status'], 'INSTALLED_SOURCE')
            self.assertEqual(calls, [('checkout', pr['repo'], 'b' * 40), ('install', ['scripts/example.py'])])

    def test_fleet_discovers_receipt_but_requires_existing_proof_and_worker(self):
        m = load()
        spec = importlib.util.spec_from_file_location('fleet_ingest', SCRIPT.with_name('fleet_ingest.py'))
        fleet = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fleet)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m.Deployer(root, FakeRemote(fixture()), notify=lambda _: None).process(fixture())
            rows, errors = fleet.auto_deployer_events(root)
            self.assertEqual(errors, [])
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row['issue'], 'GLP-358')
            self.assertIsNone(row['worker_result'])
            result = fleet.ingest(root, row, {}, runner=lambda *a: {'rc': 1, 'stdout': '', 'stderr': 'missing genuine proof'})
            self.assertEqual(result['status'], 'REDISPATCH')
            receipt = next((root / 'reports/ops/auto-deployer').glob('[0-9]*/*.json'))
            receipt.write_text(receipt.read_text() + ' ')
            rows, errors = fleet.auto_deployer_events(root)
            self.assertEqual(rows, [])
            self.assertEqual(len(errors), 1)

    def test_accepted_green_merges_and_readbacks_before_receipt(self):
        m = load()
        pr = fixture()
        remote = FakeRemote(pr)
        alerts = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deployer = m.Deployer(root, remote, notify=lambda text: alerts.append(text))
            result = deployer.process(pr)
            self.assertEqual(result['status'], 'WAITING_LIVE_OUTPUT')
            self.assertEqual(remote.merges, [(pr['repo'], 7, pr['headRefOid'])])
            self.assertEqual(len(remote.readbacks), 1)
            receipts = list((root / 'reports/ops/auto-deployer').glob('*/*.json'))
            self.assertEqual(len(receipts), 1)
            receipt = json.loads(receipts[0].read_text())
            self.assertEqual(receipt['merge']['merge_commit'], 'b' * 40)
            self.assertEqual(receipt['deployment']['release_job'], '415ec5eba194')
            self.assertFalse(receipt['live_output_proven'])
            rows = (root / 'reports/ops/fleet-inbox/auto-deployer.jsonl').read_text().splitlines()
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(alerts), 1)


if __name__ == '__main__':
    unittest.main()
