"""Synthetic source-only capture-root regressions; not business acceptance."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp_wire_in_ready_queue as producer


class CaptureRootsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.machine = self.root / 'affiliate-machine'
        self.chain = self.root / 'approval-chain'
        self.machine.mkdir()
        self.capture_dir = self.chain / 'event' / 'run'
        self.capture_dir.mkdir(parents=True)
        self.payload = producer.rails.bind_tracking_capture(
            'MaxBounty', '12345', 'https://fixture-only.example/trk/opaque',
            program='Fixture Only Provider',
            base={'tracking_clicks': 0, 'proof_class': 'synthetic_fixture_only',
                  'minted_at_utc': '2026-09-01T00:00:00Z'})
        self.capture = self.capture_dir / 'tracking-link-12345.json'
        self.capture.write_text(json.dumps(self.payload))
        self.sha = self.payload['tracking_link_sha256']

    def load(self, *extra):
        return producer.load_identity_captures(
            self.machine, recursive=True, additional_directories=extra)

    def test_exact_additional_root_recovers_omitted_capture(self):
        self.assertNotIn(self.sha, producer.load_identity_captures(
            self.machine, recursive=True).get('_by_link_sha256', {}))
        record = self.load(self.chain)['_by_link_sha256'][self.sha]
        self.assertEqual(record['_sha256'], hashlib.sha256(self.capture.read_bytes()).hexdigest())
        self.assertEqual(record['immutable_id'], '12345')

    def test_overlapping_roots_deduplicate_files(self):
        self.capture.write_text(json.dumps(dict(self.payload, tracking_clicks=1)))
        loaded = self.load(self.chain, self.capture_dir, self.chain)
        self.assertEqual(len(loaded['_rejected']['items']), 1)
        self.assertNotIn(self.sha, loaded.get('_by_link_sha256', {}))

    def test_additional_root_preserves_contract_hash_refusal(self):
        self.capture.write_text(json.dumps(dict(self.payload, tracking_link_sha256='0' * 64)))
        loaded = self.load(self.chain)
        self.assertNotIn(self.sha, loaded.get('_by_link_sha256', {}))
        self.assertIn(self.sha, loaded['_rejected_by_link_sha256'])

    def test_additional_root_preserves_legacy_capture_refusal(self):
        legacy = dict(self.payload, status='MINTED_NOT_CLICKED', program_id='12345')
        legacy.pop('schema')
        self.capture.write_text(json.dumps(legacy))
        loaded = self.load(self.chain)
        self.assertEqual(loaded['_rejected_by_link_sha256'][self.sha]['reason'],
                         'legacy_tracking_capture_contract_missing')

    def test_missing_primary_root_does_not_hide_explicit_additional_root(self):
        loaded = producer.load_identity_captures(
            self.root / 'missing', recursive=True, additional_directories=(self.chain,))
        self.assertIn(self.sha, loaded['_by_link_sha256'])

    def test_normal_main_consumes_repeated_additional_roots(self):
        values = self.root / 'values.json'
        values.write_text('[]')
        with mock.patch.object(producer, 'run', return_value={'loud_refusals': []}) as run:
            self.assertEqual(producer.main([
                '--values', str(values), '--captures', str(self.machine),
                '--additional-captures', str(self.chain),
                '--additional-captures', str(self.capture_dir), '--recursive-captures',
                '--output', str(self.root / 'out')]), 0)
        self.assertEqual(run.call_args.kwargs['additional_captures_dirs'],
                         (self.chain, self.capture_dir))
        self.assertFalse(run.call_args.kwargs['apply'])

    def test_run_consumes_additional_roots_with_no_queue_effect(self):
        with mock.patch.object(producer, 'plan_rows', return_value=[]) as plan, \
             mock.patch.object(producer, 'metadata_queue_rows', return_value=([], [])), \
             mock.patch.object(producer, 'insert_queue_rows', side_effect=AssertionError('effect')):
            result = producer.run([], apply=False, only_row=None, captures_dir=self.machine,
                                  recursive_captures=True, additional_captures_dirs=(self.chain,),
                                  receipt_dir=self.root / 'output')
        self.assertIn(self.sha, plan.call_args.kwargs['captures']['_by_link_sha256'])
        self.assertTrue(all(value == 0 for value in result['effects'].values()))

    def test_only_current_carrier_binds_exact_chain_root_and_scanner(self):
        carrier = (ROOT / 'ops/hermes-cron-scripts/glp_affiliate_reconciliation_current_base.sh').read_text()
        self.assertIn('--additional-captures /home/ubuntu/business/reports/glp-affiliate-auto-reconciler/maxbounty-approval-chain', carrier)
        scanner_sha = hashlib.sha256((ROOT / 'scripts/glp_wire_in_ready_queue.py').read_bytes()).hexdigest()
        self.assertIn("wire_scanner_expected_sha256='" + scanner_sha + "'", carrier)
        historical = ROOT / 'ops/hermes-cron-scripts/glp_affiliate_reconciliation_honesty.sh'
        self.assertEqual(hashlib.sha256(historical.read_bytes()).hexdigest(),
                         '99bcc4cca7c63f41e3546bb0e470976907027557bf1e7b89d1a0dbb8aaebcfb4')
        self.assertEqual(subprocess.run(['bash', '-n', str(ROOT / 'ops/hermes-cron-scripts/glp_affiliate_reconciliation_current_base.sh')]).returncode, 0)

    def test_historical_scanner_bytes_are_authentic_test_input_only(self):
        import glp93_current_base_binding as binding
        body = binding.historical_wire_scanner_bytes(ROOT)
        self.assertEqual(hashlib.sha256(body).hexdigest(), binding.HISTORICAL_WIRE_SHA256)
        self.assertNotIn(b'additional_directories', body)

    def test_historical_fixture_reader_refuses_changed_current_scanner(self):
        import glp93_current_base_binding as binding
        path = self.root / binding.WIRE_SCANNER
        path.parent.mkdir()
        path.write_bytes((ROOT / binding.WIRE_SCANNER).read_bytes() + b'\n# synthetic drift\n')
        with self.assertRaisesRegex(RuntimeError, 'current_wire_scanner_drift'):
            binding.historical_wire_scanner_bytes(self.root)


if __name__ == '__main__':
    unittest.main()
