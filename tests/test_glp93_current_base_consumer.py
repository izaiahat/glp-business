"""Offline consumer regression; synthetic preparation is NOT publication authority.

Execute the unchanged consumer AST and weekly evidence helpers. Only the weekly
module's omitted eager builder and registered publisher fixture boundary are
isolated here; a separate retained-host probe exercises the real native chain.
"""
import ast
import base64
import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def load_weekly():
    path = ROOT / 'scripts/glp_price_index_weekly.py'
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'BUILDER' for t in node.targets):
            node.value = ast.Constant(None)  # uncurated eager builder only
    module = types.ModuleType('glp93_weekly_fixture')
    module.__file__ = str(path)
    exec(compile(ast.fix_missing_locations(tree), str(path), 'exec'), module.__dict__)
    return module


weekly = load_weekly()


def load_consumer(root, pointer):
    path = ROOT / 'scripts/glp_affiliate_wire_backlog_worker.py'
    tree = ast.parse(path.read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
             and n.name in {'_registry_by_id', 'B2Error', 'read_json'}]
    scope = {'Path': Path, 'Any': object, 'CURRENT_BASE': pointer,
             'RELEASES': root / 'site-system/flagship-v2/releases', 'json': json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), scope)
    return types.SimpleNamespace(**scope)


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CurrentBaseConsumer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='glp93-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.release = self.root / 'reports/ops/retained/prepared/release'
        self.base = self.root / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
        self.worker = load_consumer(self.root, self.base)
        self.row = {'program_id': 'synthetic-sesame', 'network': 'Impact',
                    'network_program_id': '13292', 'commercial_state': 'held'}
        self.registry = self.release / 'data/program-registry.json'
        put(self.registry, {'programs': [self.row]})
        put(self.release / 'public/index.html', {'fixture_only': True})
        prior_root = self.worker.RELEASES / 'prior'
        prior_root.mkdir(parents=True)
        prior = {'schema': 'glp-opus-current-content-base-v1', 'status': 'PASS',
                 'deployment_id': '22222222-2222-2222-2222-222222222222',
                 'release_root': str(prior_root), 'release_tree_sha256': 'b' * 64,
                 'public_tree_sha256': 'c' * 64,
                 'manifest': {'path': str(self.root / 'old/01-deployment-manifest.json'), 'sha256': 'd' * 64},
                 'final_receipt': {'path': str(self.root / 'old/10-final-publication-receipt.json'), 'sha256': 'e' * 64},
                 'supersedes': {'deployment_id': '33333333-3333-3333-3333-333333333333', 'release_root': str(prior_root.parent)}}
        prior_bytes = json.dumps(prior).encode()
        release_rows = weekly._legacy_rows(weekly._complete_current_rows(self.release))
        public_rows = weekly._legacy_rows(weekly._complete_current_rows(self.release / 'public'))
        deploy = self.release.parent / 'deploy-public'
        put(deploy / '_worker.js', {'fixture_only': True})
        deploy_rows = weekly._legacy_rows(weekly._complete_current_rows(deploy))
        def closure(rows):
            return {'tree_sha256': weekly._legacy_closure(rows), 'file_count': len(rows),
                    'total_bytes': sum(r['size'] for r in rows)}
        self.prepared_path = self.release.parent / 'PREPARED.json'
        self.prepared = {'schema': 'glp43-current-prepared-v1', 'prepared_dir': str(self.release.parent),
                         'site_head': 'a' * 40, 'site_repository': 'izaiahat/glp-site-candidate',
                         'project': 'glp-clinic-scout', 'branch': 'main', 'release_authorized': False,
                         'provider_mutations': 0, 'rollback_deployment_id': prior['deployment_id'],
                         'predecessor_pointer': {'path': str(self.base), 'sha256': hashlib.sha256(prior_bytes).hexdigest(),
                                                'bytes': len(prior_bytes), 'bytes_base64': base64.b64encode(prior_bytes).decode(),
                                                'deployment_id': prior['deployment_id'], 'release_root': str(prior_root)},
                         'compiled': {'worker_sha256': weekly.sha_file(deploy / '_worker.js')},
                         'release': closure(release_rows), 'deploy_directory': closure(deploy_rows)}
        put(self.prepared_path, self.prepared)
        self.manifest = self.base.parent / ('current-release-' + 'a' * 40) / '01-deployment-manifest.json'
        manifest_sha = put(self.manifest, {'schema': 'glp-opus-release-deployment-manifest-v1',
            'release_root': str(self.release), 'release_rows': release_rows,
            'release_tree_sha256': closure(release_rows)['tree_sha256'], 'site_head': 'a' * 40,
            'deploy_directory': closure(deploy_rows)})
        self.receipt = self.manifest.with_name('10-final-publication-receipt.json')
        receipt_sha = put(self.receipt, {'schema': 'glp-opus-production-terminal-receipt-v1',
            'status': 'PASS_PRODUCTION_TERMINAL', 'site_head': 'a' * 40,
            'deployment': {'deployment_id': '11111111-1111-1111-1111-111111111111'},
            'evidence_sha256': {self.manifest.name: manifest_sha},
            'immutable_closure': {'release_tree_sha256': closure(release_rows)['tree_sha256'],
                                  'public_tree_sha256': closure(public_rows)['tree_sha256']}})
        self.pointer = {'schema': 'glp-opus-current-content-base-v1', 'status': 'PASS',
            'deployment_id': '11111111-1111-1111-1111-111111111111', 'release_root': str(self.release),
            'release_tree_sha256': closure(release_rows)['tree_sha256'],
            'public_tree_sha256': closure(public_rows)['tree_sha256'], 'site_head': 'a' * 40,
            'deploy_directory_sha256': closure(deploy_rows)['tree_sha256'],
            'manifest': {'path': str(self.manifest), 'sha256': manifest_sha},
            'final_receipt': {'path': str(self.receipt), 'sha256': receipt_sha},
            'supersedes': {'deployment_id': prior['deployment_id'], 'release_root': str(prior_root)}}
        put(self.base, self.pointer)
        self.native_calls = []
        fixture_prepared_sha = weekly.sha_file(self.prepared_path)
        def fixture_native(*args):
            self.native_calls.append(args)
            if weekly.sha_file(self.prepared_path) != fixture_prepared_sha:
                raise RuntimeError('synthetic_native_prepared_hash_drift')
            return {'files': {}, 'fixture_only': True}
        for p in (patch.object(weekly, 'BUSINESS', self.root), patch.object(weekly, 'CURRENT_BASE', self.base),
                  patch.object(weekly, '_registered_prepared_binding', side_effect=fixture_native),
                  patch.dict(sys.modules, {'glp_price_index_weekly': weekly}),
                  patch.object(weekly, 'project_detail', side_effect=AssertionError('private calls forbidden'))):
            p.start(); self.addCleanup(p.stop)

    def consume(self):
        return self.worker._registry_by_id(self.pointer)

    def test_authenticated_retained_prepared_root_accepts_exact_registry(self):
        release, programs, document = self.consume()
        self.assertEqual(release, self.release)
        self.assertEqual(programs, {'synthetic-sesame': self.row})
        self.assertEqual(document, {'programs': [self.row]})
        self.assertEqual(len(self.native_calls), 2)

    def test_legacy_direct_child_contract_unchanged(self):
        release = self.worker.RELEASES / 'legacy'
        put(release / 'data/program-registry.json', {'programs': [self.row]})
        self.assertEqual(self.worker._registry_by_id({'release_root': str(release)})[1], {'synthetic-sesame': self.row})
        self.assertFalse(self.native_calls)

    def test_raw_report_path_cannot_be_an_override(self):
        with self.assertRaisesRegex(self.worker.B2Error, 'current_release_scope_invalid'):
            self.worker._registry_by_id({'release_root': str(self.release)})

    def test_noncanonical_pointer_refused(self):
        self.pointer['deployment_id'] = '44444444-4444-4444-4444-444444444444'
        with self.assertRaises(self.worker.B2Error):
            self.consume()

    def test_tampered_evidence_and_tree_refused(self):
        for path in (self.manifest, self.receipt, self.registry, self.prepared_path,
                     self.release.parent / 'deploy-public/_worker.js'):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b' ')
                try:
                    with self.assertRaises(self.worker.B2Error):
                        self.consume()
                finally:
                    path.write_bytes(original)

    def test_rehashed_receipt_deployment_substitution_refused(self):
        receipt = json.loads(self.receipt.read_bytes())
        receipt['deployment']['deployment_id'] = '44444444-4444-4444-4444-444444444444'
        self.pointer['final_receipt']['sha256'] = put(self.receipt, receipt)
        put(self.base, self.pointer)
        with self.assertRaises(self.worker.B2Error):
            self.consume()

    def test_rehashed_prepared_identity_substitution_refused(self):
        self.prepared['site_repository'] = 'synthetic/wrong'
        put(self.prepared_path, self.prepared)
        with self.assertRaises(self.worker.B2Error):
            self.consume()

    def test_symlink_root_refused(self):
        alias = self.release.parent / 'alias'
        alias.symlink_to(self.release, target_is_directory=True)
        self.pointer['release_root'] = str(alias)
        put(self.base, self.pointer)
        with self.assertRaises(self.worker.B2Error):
            self.consume()

    def _post_registry_read(self, effect):
        original_open = Path.open
        fired = False

        @contextmanager
        def observed_open(path, *args, **kwargs):
            nonlocal fired
            selected = path == self.registry and len(self.native_calls) == 1 and not fired
            if selected:
                fired = True
            with original_open(path, *args, **kwargs) as stream:
                yield stream
            if selected:
                effect()
        return patch.object(Path, 'open', observed_open)

    def test_post_registry_pointer_change_refused(self):
        def mutate():
            self.base.write_bytes(self.base.read_bytes() + b' ')
        with self._post_registry_read(mutate):
            with self.assertRaises(self.worker.B2Error):
                self.consume()

    def test_transient_registry_replacement_restored_before_revalidation_refused(self):
        self._assert_transient_registry_replacement_refused('offline-transient-marker')

    def test_same_size_transient_registry_replacement_refused(self):
        self._assert_transient_registry_replacement_refused('99999')

    def _assert_transient_registry_replacement_refused(self, network_program_id):
        # Schedule a real scratch-file replacement at the consumer's open, not
        # a fabricated reader return or evidence-validator result. Both path
        # validations would accept the restored tree on the vulnerable code.
        authentic = self.registry.read_bytes()
        replacement = json.loads(authentic)
        replacement['programs'][0]['network_program_id'] = network_program_id
        if network_program_id == '99999':
            self.assertEqual(len(json.dumps(replacement).encode()), len(authentic))
        validations = []
        validator = weekly._validate_base_evidence
        original_open = Path.open
        fired = False

        def observe_validation(*args):
            result = validator(*args)
            validations.append(result)
            return result

        @contextmanager
        def scheduled_open(path, *args, **kwargs):
            nonlocal fired
            replace = path == self.registry and len(validations) == 1 and not fired
            if replace:
                fired = True
                path.write_text(json.dumps(replacement))
            try:
                with original_open(path, *args, **kwargs) as stream:
                    yield stream
            finally:
                if replace:
                    path.write_bytes(authentic)

        refusal = None
        returned = None
        with patch.object(weekly, '_validate_base_evidence', side_effect=observe_validation), \
             patch.object(Path, 'open', scheduled_open):
            try:
                returned = self.consume()
            except self.worker.B2Error as exc:
                refusal = exc
        self.assertTrue(fired)
        self.assertEqual(self.registry.read_bytes(), authentic)
        # Independently revalidate the restored tree even when the consumer
        # correctly refuses before reaching its normal second validation.
        restored = validator(self.pointer, self.release)
        self.assertEqual(restored, validations[0])
        for result in validations:
            self.assertEqual(result, restored)
        self.assertIsInstance(refusal, self.worker.B2Error,
            f'Unauthenticated registry accepted: validations_passed={len(validations)}, '
            f'validation_results_identical=True, registry_restored=True, returned={returned}')
        self.assertIn('current_release_registry_bytes_mismatch', str(refusal))

    def test_first_batch_step_consumes_full_current_pointer(self):
        path = ROOT / 'scripts/glp_affiliate_wire_backlog_worker.py'
        tree = ast.parse(path.read_text())
        assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'source_registry' for t in n.targets))
        scope = {'_registry_by_id': self.worker._registry_by_id, 'pointer': self.pointer,
                 'current_source': self.release, 'current_predecessor': self.release}
        exec(compile(ast.Module(body=[assignment], type_ignores=[]), str(path), 'exec'), scope)
        self.assertEqual(scope['source_registry'], {'synthetic-sesame': self.row})

    def test_intermediate_batch_step_retains_direct_child_contract(self):
        stage = self.worker.RELEASES / '.stage-01'
        put(stage / 'data/program-registry.json', {'programs': [self.row]})
        path = ROOT / 'scripts/glp_affiliate_wire_backlog_worker.py'
        assignment = next(n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'source_registry' for t in n.targets))
        scope = {'_registry_by_id': self.worker._registry_by_id, 'pointer': self.pointer,
                 'current_source': stage, 'current_predecessor': self.release}
        exec(compile(ast.Module(body=[assignment], type_ignores=[]), str(path), 'exec'), scope)
        self.assertEqual(scope['source_registry'], {'synthetic-sesame': self.row})
        self.assertFalse(self.native_calls)

    def test_missing_bound_receipt_refused(self):
        self.receipt.unlink()
        with self.assertRaises(self.worker.B2Error):
            self.consume()

    def test_symlink_evidence_refused(self):
        destination = self.receipt.with_name('receipt-bytes.json')
        self.receipt.rename(destination)
        self.receipt.symlink_to(destination)
        with self.assertRaises(self.worker.B2Error):
            self.consume()

    def test_failed_pointer_status_refused(self):
        self.pointer['status'] = 'FAIL'
        put(self.base, self.pointer)
        with self.assertRaises(self.worker.B2Error):
            self.consume()

    def test_rehashed_arbitrary_report_root_refused(self):
        arbitrary = self.root / 'reports/ops/arbitrary'
        self.release.rename(arbitrary)
        self.pointer['release_root'] = str(arbitrary)
        put(self.base, self.pointer)
        with self.assertRaises(self.worker.B2Error):
            self.consume()

    def test_post_registry_tree_change_refused(self):
        def mutate():
            self.registry.write_bytes(self.registry.read_bytes() + b' ')
        with self._post_registry_read(mutate):
            with self.assertRaises(self.worker.B2Error):
                self.consume()


if __name__ == '__main__':
    unittest.main()
