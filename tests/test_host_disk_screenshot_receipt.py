"""Rule-D receipt admission only: exact AST, scratch inputs, inventory sentinel.

No cleanup/producer module is imported. Placeholder implementation bytes bind a
fixture-only contract; they are not qualification or installation authority.
"""
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from typing import Any

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/screenshot_pruner.py'
EP_SOURCE = SOURCE.with_name('evidence_preserving_pruner.py')
RULE = 'D_cold_run_images'


class ReachedInventory(Exception):
    pass


class ScanBudget(Exception):
    pass


def extract(raw, names, namespace, filename):
    nodes: list[ast.stmt] = [node for node in ast.parse(raw, filename=str(filename)).body
                            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    assert {node.name for node in nodes} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(filename), 'exec'), namespace)


def inventory_stop(*args, **kwargs):
    raise ReachedInventory()


class ScreenshotReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        scripts = self.root / 'scripts'
        scripts.mkdir()
        # These are explicitly synthetic bytes, never copied installed authority.
        for name in ('screenshot_pruner.py', 'evidence_preserving_pruner.py',
                     'host_disk_pruner.py', 'pruner_process_reference_guard.py', 'bounded_pruner.py'):
            (scripts / name).write_bytes(SOURCE.read_bytes() if name == 'screenshot_pruner.py'
                                        else ('# fixture-only ' + name).encode())
        ens = dict(os=os, hashlib=hashlib, time=time, ScanBudget=ScanBudget)
        extract(EP_SOURCE.read_bytes(), {'sha', 'hash_stream'}, ens, EP_SOURCE)
        self.ep = types.SimpleNamespace(BUS=self.root, __file__=str(scripts / 'evidence_preserving_pruner.py'),
                                        ScanBudget=ScanBudget, sha=ens['sha'])
        self.ns: dict[str, Any] = dict(ep=self.ep, Path=Path, hashlib=hashlib, json=json, time=time,
                       os=os, stat=stat, __file__=str(scripts / 'screenshot_pruner.py'),
                       IMAGE_SUFFIXES={'.png', '.jpg', '.jpeg', '.webp', '.avif', '.gif'},
                       RULE=RULE, reference_files=inventory_stop)
        tree = ast.parse(SOURCE.read_bytes())
        names = {'run_rules', 'rule_contract'}
        names.update(node.name for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == 'read_dry_run_receipt')
        extract(SOURCE.read_bytes(), names, self.ns, SOURCE)
        flag = self.root / 'site-system/flagship-v2'
        self.approval = self.root / 'ops/linear/approvals/OPERATOR-DECISION-20261001-qa-screenshot-deletion.md'
        self.approval.parent.mkdir(parents=True)
        self.approval.write_bytes(b'fixture approval: no operational authority')
        self.policy = {'schema': 'disk-retention-v1', 'protected_roots': [str(flag / 'qa')],
                       'rule_D': {'image_roots': [str(flag / 'qa'), str(flag / 'candidates')],
                                  'cold_days': 30, 'image_extensions': sorted(self.ns['IMAGE_SUFFIXES']),
                                  'qa_protection_exception': str(flag / 'qa'),
                                  'approval': {'path': str(self.approval),
                                               'sha256': '3ac87b3d008a214b11df4fa540817435d6962e82a5887076c2ab9486cf390c3b'}}}
        # Only the independent fixed operator-approval input is synthetic. The
        # receipt hash/reader are actual source and must not be mocked.
        native_sha = self.ep.sha
        self.ep.sha = lambda path, deadline: (self.policy['rule_D']['approval']['sha256']
                                             if path == self.approval else native_sha(path, deadline))
        self.receipt = self.root / 'reports/ops/host-disk-pruner-screenshots-v2-20261001/fixture/dry-run-images.json'
        self.receipt.parent.mkdir(parents=True)
        self.contract = self.ns['rule_contract'](self.policy)
        self.valid = {'dry_run': True, 'bytes_freed': 0,
                      'rows': [{'rule': RULE, 'status': 'GATED_REFERENCED', 'ok': True, 'bytes_freed': 0},
                               {'rule': RULE + '_summary', 'status': 'COMPLETE', 'ok': True,
                                'partial': False, 'contract_sha256': self.contract, 'bytes_freed': 0}]}
        self.setup_receipt(self.valid)

    def setup_receipt(self, data, *, pin=True, raw=None):
        raw = json.dumps(data).encode() if raw is None else raw
        self.receipt.write_bytes(raw)
        self.policy['rule_D']['dry_run_receipt'] = {'path': str(self.receipt),
                                                 'sha256': hashlib.sha256(raw).hexdigest() if pin else '0' * 64}
        (self.root / 'ops/disk-retention-policy.json').write_text(json.dumps(self.policy))
        return raw

    def invoke(self):
        try:
            rows = self.ns['run_rules'](None, {'reference_roots': []}, dry_run=False,
                                        deadline=time.monotonic() + 30)
            return False, rows
        except ReachedInventory:
            return True, []

    def test_exact_complete_semantics_refuse_original_failed_and_string_false(self):
        # RV481's original two resealed semantic counterexamples, unchanged.
        for kind in ('failed_summary', 'string_false'):
            with self.subTest(kind=kind):
                altered = copy.deepcopy(self.valid)
                if kind == 'failed_summary':
                    altered['rows'][-1].update(status='FAILED', ok=False, partial=False)
                else:
                    altered['dry_run'] = 'false'
                self.setup_receipt(altered)
                admitted, rows = self.invoke()
                self.assertFalse(admitted, kind)
                self.assertEqual(rows[0]['reason'], 'D_dry_run_incomplete_or_stale')

    def test_authentication_and_parse_use_one_buffer_not_original_second_read(self):
        # RV481's actual scratch replacement at the second read_text boundary.
        # A repaired reader removes that boundary, so require the hook NEVER
        # executes. A positive original buffer remains admissible, not forged.
        replacement = copy.deepcopy(self.valid)
        replacement['rows'][-1].update(status='FAILED', ok=False, partial=False)
        receipt = self.receipt
        replacement_calls = []
        base = type(Path())
        class RacePath(base):
            def read_text(self, *args, **kwargs):
                if str(self) == str(receipt):
                    receipt.write_text(json.dumps(replacement))
                    replacement_calls.append(True)
                return super().read_text(*args, **kwargs)
        self.ns['Path'] = RacePath
        admitted, _ = self.invoke()
        self.assertEqual(replacement_calls, [], 'authenticated receipt was reopened for parsing')
        self.assertTrue(admitted, 'the untouched authentic-buffer positive was refused')
        self.assertEqual(json.loads(receipt.read_bytes()), self.valid)

    def test_retained_positive_hash_partial_source_drift_and_scope_controls(self):
        self.assertTrue(self.invoke()[0])  # Real GATED per-run detail stays valid.
        self.setup_receipt(self.valid, pin=False)
        self.assertFalse(self.invoke()[0])
        altered = copy.deepcopy(self.valid)
        altered['rows'][-1]['partial'] = True
        self.setup_receipt(altered)
        self.assertFalse(self.invoke()[0])
        self.setup_receipt(self.valid)
        source = self.root / 'scripts/bounded_pruner.py'
        source.write_bytes(source.read_bytes() + b'\n# drift')
        self.assertFalse(self.invoke()[0])
        self.policy['rule_D']['image_roots'].append(str(self.root / 'unapproved'))
        (self.root / 'ops/disk-retention-policy.json').write_text(json.dumps(self.policy))
        self.assertFalse(self.invoke()[0])

    def test_bounded_nofollow_reader_and_changed_bytes_fail_closed(self):
        reader = self.ns['read_dry_run_receipt']
        deadline = time.monotonic() + 30
        expected = self.policy['rule_D']['dry_run_receipt']['sha256']
        native_open = os.open
        flags_seen = []
        def observed_open(path, flags, *args, **kwargs):
            flags_seen.append(flags)
            return native_open(path, flags, *args, **kwargs)
        with patch.object(os, 'open', observed_open):
            self.assertEqual(reader(self.receipt, expected, deadline), self.valid)
        self.assertEqual(len(flags_seen), 1)
        self.assertTrue(flags_seen[0] & os.O_NOFOLLOW)
        self.assertTrue(flags_seen[0] & os.O_NOATIME)
        self.assertTrue(flags_seen[0] & os.O_NONBLOCK)
        link = self.receipt.with_name('link.json')
        link.symlink_to(self.receipt)
        with self.assertRaises(OSError):
            reader(link, expected, deadline)
        large = self.receipt.with_name('large.json')
        with large.open('wb') as stream:
            stream.truncate(32 * 1024 * 1024 + 1)
        with self.assertRaises(ValueError):
            reader(large, expected, deadline)
        fifo = self.receipt.with_name('fifo.json')
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            reader(fifo, expected, deadline)
        with self.assertRaises(ScanBudget):
            reader(self.receipt, expected, time.monotonic() - 1)
        # Real changed content at the hashing boundary cannot become consumed
        # authority: mutate the file while SHA256 receives its original buffer.
        original_sha256 = hashlib.sha256
        original_bytes = self.receipt.read_bytes()
        parsed = []
        original_loads = json.loads
        def replace_at_hash(raw):
            self.receipt.write_bytes(b'{"dry_run": false}')
            return original_sha256(raw)
        def capture_parse(raw, *args, **kwargs):
            parsed.append(raw)
            return original_loads(raw, *args, **kwargs)
        with patch.object(hashlib, 'sha256', replace_at_hash), patch.object(json, 'loads', capture_parse):
            data = reader(self.receipt, expected, deadline)
        self.assertEqual(data, self.valid)
        self.assertEqual(parsed, [original_bytes])
        self.assertEqual(original_sha256(parsed[0]).hexdigest(), expected)
        self.assertNotEqual(original_sha256(self.receipt.read_bytes()).hexdigest(), expected)

    def test_malformed_receipt_shapes_refuse_before_inventory(self):
        for data in ([], True, None, {'rows': None}, {'rows': {}}, {'rows': [None]},
                     {'rows': ['not a row']}, {'rows': [1]}):
            with self.subTest(data=data):
                self.setup_receipt(data)
                self.assertFalse(self.invoke()[0])
        self.setup_receipt(None, raw=b'not JSON')
        self.assertFalse(self.invoke()[0])

    def test_exact_booleans_single_summary_and_complete_status_controls(self):
        cases = [('dry_run', value) for value in (False, None, 0, 1, [], {}, 'true')]
        cases += [('ok', value) for value in (False, None, 0, 1, 'true')]
        cases += [('partial', value) for value in (True, None, 0, 1, 'false')]
        cases += [('status', value) for value in ('FAILED', 'TIME_BUDGET_PARTIAL', None)]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                altered = copy.deepcopy(self.valid)
                (altered if key == 'dry_run' else altered['rows'][-1])[key] = value
                self.setup_receipt(altered)
                self.assertFalse(self.invoke()[0])
        for key in ('dry_run', 'ok', 'partial', 'status'):
            altered = copy.deepcopy(self.valid)
            del (altered if key == 'dry_run' else altered['rows'][-1])[key]
            self.setup_receipt(altered)
            self.assertFalse(self.invoke()[0])
        for rows in ([], self.valid['rows'] * 2):
            altered = copy.deepcopy(self.valid)
            altered['rows'] = rows
            self.setup_receipt(altered)
            self.assertFalse(self.invoke()[0])


class CurrentPolicyPointerTests(unittest.TestCase):
    def test_exact_live_preimage_preserved_except_genuine_receipt_pointer(self):
        # Complete authenticated policy preimage, not an assumed checkout policy.
        # This static source regression does not install policy or qualify images.
        source = SOURCE.parents[1] / 'ops/disk-retention-policy.json'
        raw = source.read_text()
        policy = json.loads(raw)
        pointer = policy['rule_D']['dry_run_receipt']
        old_path = '/home/ubuntu/business/reports/ops/host-disk-pruner-screenshots-v2-20261001/pr233-qualification-20261004T075713Z-231834/dry-run-images.json'
        new_path = '/home/ubuntu/business/reports/ops/host-disk-pruner-screenshots-v2-20261001/host-repair-20261014T004149Z/dry-run-images.json'
        old_sha = 'b99d3c4244402a18217ac39b210dd5fcefe06ee4e6391d4520a9f035bdd369c4'
        new_sha = 'fc4b4aa14bba8122cd770c2477b90a5b9b4b7d5ccb6a4c41089f3221bf58b3e6'
        self.assertEqual(pointer, {'path': new_path, 'sha256': new_sha})
        self.assertEqual(raw.count(new_path), 1)
        self.assertEqual(raw.count(new_sha), 1)
        restored = raw.replace(new_path, old_path).replace(new_sha, old_sha).encode()
        self.assertEqual(hashlib.sha256(restored).hexdigest(),
                         '0438bdf671da6098f14343cce2cd4983dc376f7a48a3303f2b388cd60d3d4d73')
        self.assertIn('/home/ubuntu/business/site-system/toolchains/wrangler-4.114.0-r13',
                      policy['protected_roots'])
        self.assertNotIn('completed_ci_dependencies', policy)


if __name__ == '__main__':
    unittest.main()
