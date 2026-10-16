"""Offline fixtures only: no GitHub, installer, Discord or Linear transport."""
import copy
import contextlib
import io
import importlib.util
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/auto_deployer.py'


def load():
    spec = importlib.util.spec_from_file_location('auto_deployer', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    head = 'a' * 40
    return {
        'number': 7, 'repo': 'izaiahat/glp-site-candidate', 'state': 'OPEN',
        'baseRefName': 'main', 'isDraft': False, 'headRefOid': head,
        'mergeStateStatus': 'CLEAN', 'body': 'fleet-ticket:GLP-358', 'labels': [],
        'headCommittedAt': '2026-10-16T01:00:00Z',
        'reviews': [{'id': 11, 'state': 'COMMENTED', 'commit_id': head,
                     'submitted_at': '2026-10-16T02:00:00Z',
                     'body': 'ACCEPT\nHead: ' + head + '\nReviewer: Astra\nSource-author: Sol\nIndependent: true\nNon-author: true'}],
        'checks': [{'name': 'ci', 'head_sha': head, 'status': 'completed', 'conclusion': 'success'}],
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
                return dict(fixture(), repo=repo, changed_files=1)
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
        pr = dict(fixture(), repo='izaiahat/glp-business', changed_files=1)
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
