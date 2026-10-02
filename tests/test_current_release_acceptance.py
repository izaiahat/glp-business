"""Disposable synthetic approvals; no fixture is production authority."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('current_acceptance_test', ROOT / 'systems/seo-blog-machine/scripts/accepted_failure_contract.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CurrentAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'runtime'
        self.rows = [{'path': 'public/index.html', 'sha256': 'a' * 64, 'size': 12}]
        # Native GLP43 file_closure is path/NUL/size/NUL/hash, NOT legacy row_closure.
        self.tree = hashlib.sha256(('public/index.html\x0012\x00' + 'a' * 64 + '\n').encode()).hexdigest()
        self.release = str(self.root / 'prepared/release')
        self.authority = self.write('authority.md', 'SYNTHETIC operator designation', raw=True)
        self.authorities = {str(self.authority): c.descriptor(self.authority)['sha256']}
        self.prepared = self.write('prepared/PREPARED.json', {
            'schema': 'glp43-current-prepared-v1', 'site_head': 'b' * 40,
            'prepared_dir': str(self.root / 'prepared'), 'site_repository': 'izaiahat/glp-site-candidate',
            'source': {'git_tree': 'c' * 40}, 'public_tree_sha256': 'd' * 64,
            'package_manifest_sha256': 'e' * 64,
            'deploy_directory': {'tree_sha256': 'f' * 64},
            'compiled': {'worker_sha256': '1' * 64}, 'project': 'glp-clinic-scout',
            'build_receipt': {'sha256': '2' * 64},
            'release': {'tree_sha256': self.tree, 'file_count': 1, 'total_bytes': 12}})
        self.review = self.write('INDEPENDENT-REVIEW.json', {
            'schema': 'glp42-designated-astra-release-review-v1', 'verdict': 'APPROVE',
            'release_accepted': True, 'full_combined_deployment_accepted': True,
            'blocking_findings': [], 'release_authorized': False,
            'reviewer': {'model': 'gpt-6-astra-900k', 'provider': 'openai-codex',
                         'role': 'SYNTHETIC independent; not an author'},
            'subject': {'repository': 'izaiahat/glp-site-candidate', 'head': 'b' * 40,
                'git_tree': 'c' * 40, 'public_tree_sha256': 'd' * 64,
                'package_manifest_sha256': 'e' * 64, 'prepared_dir': str(self.prepared.parent),
                'prepared_manifest_sha256': c.descriptor(self.prepared)['sha256'],
                'deploy_directory_sha256': 'f' * 64, 'compiled_worker_sha256': '1' * 64,
                'project': 'glp-clinic-scout'},
            'reused_technical_acceptance': {'gate_sha256': '2' * 64}})
        self.manifest = self.write('01-deployment-manifest.json', {
            'schema': 'glp-opus-release-deployment-manifest-v1', 'release_root': self.release,
            'release_tree_sha256': self.tree, 'release_rows': self.rows,
            'site_head': 'b' * 40, 'deploy_directory': {'tree_sha256': 'f' * 64}})
        self.pointer = {'release_root': self.release, 'release_tree_sha256': self.tree,
                        'manifest': c.descriptor(self.manifest)}
        self.stdout = self.write('suite.log', 'TAP version 13\nok 1 - good\nnot ok 2 - known failure\n1..2\n# tests 2\n# suites 0\n# pass 1\n# fail 1\n# cancelled 0\n# skipped 0\n# todo 0\n# duration_ms 1\n', raw=True)
        self.suite_result = self.write('SUITE-RESULT.json', {
            'command': 'npm test', 'invocations': 1, 'exit_code': 1,
            'counts': {'tests': 2, 'suites': 0, 'pass': 1, 'fail': 1, 'cancelled': 0, 'skipped': 0, 'todo': 0},
            'custody': {'source_sha256': self.tree, 'source_unchanged': True}})
        for name, value in [('CURRENT_ASTRA_REVIEW', c.descriptor(self.review)),
                            ('CURRENT_ASTRA_AUTHORITIES', self.authorities),
                            ('RUNTIME_ROOT', self.runtime)]:
            patcher = mock.patch.object(c, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, name, data, raw=False):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if raw else json.dumps(data))
        return path

    def proposal(self):
        return c.prepare_current_binding(self.pointer, self.stdout, 1, minimum_tests=2, preparer_id='fixture-author', suite_result_path=self.suite_result)

    def accept(self, proposal_path):
        proposal = json.loads(proposal_path.read_text())
        return self.write('SUITE-REVIEW.json', {
            'schema': 'glp-current-release-suite-acceptance-v1', 'verdict': 'APPROVE',
            'proposal': c.descriptor(proposal_path), 'blocking_findings': [],
            'publication_allowance_only': True,
            'accepted_names': proposal['classification']['new_failures'],
            'reviewer': {'id': 'fixture-independent-reviewer', 'model': 'gpt-6-astra-900k',
                         'provider': 'openai-codex', 'independent': True},
            'authority': c.descriptor(self.authority)})

    def test_astra_proposal_requires_separate_explicit_failure_acceptance(self):
        proposal = self.proposal()
        self.assertEqual(proposal['classification']['status'], 'FAIL_NEW_FAILURES')
        self.assertEqual(proposal['classification']['new_failures'], ['known failure'])
        self.assertFalse(self.runtime.exists())
        path = self.write('PROPOSAL.json', proposal)
        review = self.accept(path)
        binding = c.build_current_binding(path, review, c.descriptor(review)['sha256'])
        self.assertEqual(binding['accepted_names'], ['known failure'])
        self.assertFalse(self.runtime.exists())
        c.install_binding(binding, self.pointer)
        self.assertEqual(c.load_binding(self.pointer)[0], binding)

    def test_rejects_bad_authority_subject_logs_and_self_acceptance(self):
        proposal = self.proposal()
        path = self.write('PROPOSAL.json', proposal)
        review_path = self.accept(path)
        original = json.loads(review_path.read_text())
        for mutation in ('self', 'model', 'authority', 'names', 'verdict', 'independent', 'proposal'):
            review = copy.deepcopy(original)
            if mutation == 'self': review['reviewer']['id'] = 'fixture-author'
            elif mutation == 'model': review['reviewer']['model'] = 'other'
            elif mutation == 'authority': review['authority']['sha256'] = '0' * 64
            elif mutation == 'names': review['accepted_names'].append('unobserved failure')
            elif mutation == 'verdict': review['verdict'] = 'CHANGES_REQUIRED'
            elif mutation == 'independent': review['reviewer']['independent'] = False
            else: review['proposal']['sha256'] = '0' * 64
            review_path.write_text(json.dumps(review))
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                c.build_current_binding(path, review_path, c.descriptor(review_path)['sha256'])
        review_path.write_text(json.dumps(original))
        with self.assertRaises(RuntimeError):
            c.build_current_binding(path, review_path, '0' * 64)
        self.stdout.write_text(self.stdout.read_text().replace('known failure', 'changed failure'))
        with self.assertRaises(RuntimeError):
            c.build_current_binding(path, review_path, c.descriptor(review_path)['sha256'])
        self.assertFalse(self.runtime.exists())

    def test_clean_suite_is_not_automatically_approved(self):
        self.stdout.write_text(self.stdout.read_text().replace('not ok 2 - known failure', 'ok 2 - repaired').replace('# pass 1', '# pass 2').replace('# fail 1', '# fail 0'))
        result = json.loads(self.suite_result.read_text())
        result['counts'].update({'pass': 2, 'fail': 0})
        result['exit_code'] = 0
        self.suite_result.write_text(json.dumps(result))
        proposal = c.prepare_current_binding(self.pointer, self.stdout, 0,
            minimum_tests=2, preparer_id='fixture-author', suite_result_path=self.suite_result)
        self.assertEqual(proposal['status'], 'REQUIRES_INDEPENDENT_SUITE_ACCEPTANCE')
        self.assertEqual(proposal['classification']['new_failures'], [])
        path = self.write('PROPOSAL.json', proposal)
        # The real final combined approval alone is not suite acceptance.
        with self.assertRaises(RuntimeError):
            c.build_current_binding(path, self.review, c.descriptor(self.review)['sha256'])
        review_path = self.accept(path)
        binding = c.build_current_binding(path, review_path, c.descriptor(review_path)['sha256'])
        self.assertEqual(binding['accepted_names'], [])
        self.assertEqual(binding['minimum_tests'], 2)
        self.assertFalse(self.runtime.exists())

    def test_review_and_suite_custody_drift_refuse_without_runtime_write(self):
        result = json.loads(self.suite_result.read_text())
        for mutation in ('tree', 'unchanged', 'counts', 'exit'):
            changed = copy.deepcopy(result)
            if mutation == 'tree': changed['custody']['source_sha256'] = '0' * 64
            elif mutation == 'unchanged': changed['custody']['source_unchanged'] = False
            elif mutation == 'counts': changed['counts']['fail'] = 0
            else: changed['exit_code'] = 0
            self.suite_result.write_text(json.dumps(changed))
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                self.proposal()
        self.suite_result.write_text(json.dumps(result))
        raw = self.review.read_text()
        self.review.write_text(raw + ' ')
        with self.assertRaises(RuntimeError):
            self.proposal()
        self.review.write_text(raw)
        self.authority.write_text('SYNTHETIC changed designation')
        with self.assertRaises(RuntimeError):
            self.proposal()
        self.assertFalse(self.runtime.exists())

    def test_cli_has_no_write_before_acceptance_and_preserves_proposal(self):
        pointer_path = self.write('pointer.json', self.pointer)
        out = self.root / 'cli/PROPOSAL.json'
        prepare_args = ['prepare-current', '--pointer', str(pointer_path),
            '--suite-stdout', str(self.stdout), '--suite-result', str(self.suite_result),
            '--suite-exit-code', '1', '--minimum-tests', '2', '--preparer-id', 'fixture-author', '--out', str(out)]
        self.assertEqual(c.current_binding_cli(prepare_args), 0)
        raw = out.read_bytes()
        with self.assertRaises(FileExistsError):
            c.current_binding_cli(prepare_args)
        self.assertEqual(out.read_bytes(), raw)
        install_args = ['install-current', '--pointer', str(pointer_path), '--proposal', str(out),
            '--suite-acceptance', str(self.review), '--expected-suite-acceptance-sha256', c.descriptor(self.review)['sha256']]
        with self.assertRaises(RuntimeError):
            c.current_binding_cli(install_args)
        self.assertFalse(self.runtime.exists())
        accepted = self.accept(out)
        install_args[-3], install_args[-1] = str(accepted), c.descriptor(accepted)['sha256']
        self.assertEqual(c.current_binding_cli(install_args), 0)
        binding, _ = c.load_binding(self.pointer)
        self.assertEqual(binding['accepted_names'], ['known failure'])
        self.assertEqual(c.current_binding_cli(install_args), 0)
        altered = dict(binding, accepted_names=['unobserved'])
        with self.assertRaises(RuntimeError):
            c.install_binding(altered, self.pointer)

    def test_rejects_release_drift_and_incomplete_suite(self):
        for mutation in ('size', 'duplicate', 'bool', 'extra', 'root', 'tree'):
            manifest = json.loads(self.manifest.read_text())
            changed = copy.deepcopy(manifest)
            if mutation == 'size': changed['release_rows'][0]['size'] += 1
            elif mutation == 'duplicate': changed['release_rows'].append(changed['release_rows'][0])
            elif mutation == 'bool': changed['release_rows'][0]['size'] = True
            elif mutation == 'extra': changed['release_rows'][0]['bytes'] = 12
            elif mutation == 'root': changed['release_root'] += '-other'
            else: changed['release_tree_sha256'] = '0' * 64
            self.manifest.write_text(json.dumps(changed))
            pointer = {**self.pointer, 'manifest': c.descriptor(self.manifest)}
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                c.prepare_current_binding(pointer, self.stdout, 1, minimum_tests=2, preparer_id='fixture-author', suite_result_path=self.suite_result)
            self.manifest.write_text(json.dumps(manifest))
        self.stdout.write_text(self.stdout.read_text().replace('# duration_ms 1\n', ''))
        with self.assertRaises(RuntimeError):
            self.proposal()
        self.assertFalse(self.runtime.exists())


if __name__ == '__main__':
    unittest.main()
