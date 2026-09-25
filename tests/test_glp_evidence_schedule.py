import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


class ScheduledEvidenceTests(unittest.TestCase):
    def test_standing_acceptance_receipt_is_bound_and_referenced(self):
        import glp_evidence_refresh_cycle as cycle
        self.assertTrue(hasattr(cycle, 'STANDING_ACCEPTANCE_RECEIPT'))
        receipt = json.loads(cycle.STANDING_ACCEPTANCE_RECEIPT.read_text())
        authority = Path(receipt['authority_source']).read_bytes()
        self.assertEqual(receipt['authority_sha256'], hashlib.sha256(authority).hexdigest())
        self.assertEqual(receipt['authority_text'], authority.decode())
        self.assertFalse(receipt['routine_refresh_requires_new_ruling'])
        with tempfile.TemporaryDirectory() as directory:
            result = cycle.run_cycle({'records': []}, {'observations': []},
                '2026-08-02T00:00:00Z', datetime(2026, 9, 14, tzinfo=timezone.utc),
                45, Path(directory), lambda url: self.fail(), lambda: self.fail())
            self.assertEqual(result['standing_acceptance_receipt'], str(cycle.STANDING_ACCEPTANCE_RECEIPT))

    def test_due_sources_and_observation_clocks_reach_adopter_without_input_mutation(self):
        script = ROOT / 'scripts/glp_evidence_refresh_cycle.py'
        self.assertTrue(script.exists(), 'registered pre-expiry cycle does not exist')
        spec = importlib.util.spec_from_file_location('cycle', script)
        cycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cycle)
        ledger = {'generated_at_utc': '2026-09-14T00:00:00Z', 'records': [
            {'capture_id': 'provider', 'disposition': 'ACCEPTED', 'cached_at': '2026-09-14T00:00:00Z',
             'requested_url': 'https://provider.example/pricing', 'resolved_url': 'https://provider.example/pricing',
             'exact_quote': 'Price includes care.'},
            {'capture_id': 'ftc', 'disposition': 'ACCEPTED', 'cached_at': '2026-08-02T00:00:00Z',
             'requested_url': 'https://www.ftc.gov/rule', 'resolved_url': 'https://www.ftc.gov/rule',
             'exact_quote': 'Reviews must be genuine.'},
            {'capture_id': cycle.FDA_ID, 'disposition': 'ACCEPTED', 'cached_at': '2026-09-14T00:00:00Z',
             'requested_url': cycle.FDA_URL, 'resolved_url': cycle.FDA_URL, 'exact_quote': 'FDA warning.'},
            {'capture_id': 'rejected', 'disposition': 'REJECTED_SOURCE_404', 'cached_at': None,
             'requested_url': 'https://rejected.example', 'resolved_url': 'https://rejected.example/404', 'exact_quote': ''}]}
        config = {'observations': [{'id': 'observation', 'capture_id': 'provider',
                  'observed_at': '2026-08-02T00:00:00Z', 'source_excerpt': 'Price includes care.'}]}
        before = json.dumps([ledger, config], sort_keys=True)
        calls = []
        def fetch(url):
            calls.append(url)
            record = next(r for r in ledger['records'] if r['requested_url'] == url)
            return {'success': True, 'data': {'markdown': record['exact_quote'], 'metadata': {
                'statusCode': 200, 'sourceURL': url, 'url': url,
                'scrapeId': '00000000-0000-0000-0000-000000000001'}}}
        now = datetime(2026, 9, 14, tzinfo=timezone.utc)
        adopted = []
        def adopt(pairs):
            for record, proof in pairs:
                adopted.append(record['capture_id'])
                self.assertTrue(proof['material_change']['retained_claims_supported'])
                self.assertEqual(proof['material_change']['missing_claims'], [])
            return {record['capture_id']: {'status': 'ADOPTED'} for record, _ in pairs}, {}
        with tempfile.TemporaryDirectory() as directory:
            result = cycle.run_cycle(ledger, config, '2026-08-02T00:00:00Z', now, 45,
                                     Path(directory), fetch, adopt)
            self.assertEqual(result['status'], 'COMPLETED')
            self.assertEqual(len(result['records']), len(ledger['records']))
            self.assertEqual(len(calls), 2)
            self.assertEqual([r['capture_id'] for r in result['records'] if r['status'] == 'ADOPTED'], ['provider', 'ftc'])
            self.assertEqual(adopted, ['provider', 'ftc'])
            provider = result['records'][0]
            proof = json.loads(Path(provider['capture_path']).read_text())
            self.assertEqual(proof['observation_checks'][0]['observed_at'], '2026-08-02T00:00:00Z')
            self.assertEqual(proof['observation_checks'][0]['disposition'], 'EXCERPT_PRESENT_REVIEW_REQUIRED')
            self.assertEqual(json.dumps([ledger, config], sort_keys=True), before)
            self.assertEqual(result['records'][2]['status'], 'NOT_DUE')
            self.assertEqual(result['records'][3]['status'], 'REJECTED_SOURCE_REMAINS_REJECTED')
            self.assertFalse(list(Path(directory).rglob('adoptions.jsonl')))
            # Transport refusal must stop, never substitute a source or claim coverage.
            refused = cycle.run_cycle(ledger, config, '2026-08-02T00:00:00Z', now, 45,
                Path(directory) / 'refused', lambda url: {'success': False}, lambda: self.fail())
            self.assertEqual(refused['status'], 'SOURCE_REFUSED')
            self.assertEqual(refused['records'][0]['status'], 'SOURCE_REFUSED')
            self.assertEqual(refused['records'][1]['status'], 'NOT_ATTEMPTED_AFTER_SOURCE_REFUSAL')

    def test_changed_claim_is_a_finding_not_an_observation_refresh(self):
        script = ROOT / 'scripts/glp_evidence_refresh_cycle.py'
        self.assertTrue(script.exists(), 'registered pre-expiry cycle does not exist')
        spec = importlib.util.spec_from_file_location('cycle', script)
        cycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cycle)
        record = {'capture_id': 'provider', 'disposition': 'ACCEPTED', 'cached_at': '2026-08-02T00:00:00Z',
                  'requested_url': 'https://provider.example/pricing', 'resolved_url': 'https://provider.example/pricing',
                  'exact_quote': 'Old price is $100.'}
        response = {'success': True, 'data': {'markdown': 'New price is $200.', 'metadata': {
            'sourceURL': record['requested_url'], 'url': record['requested_url'], 'statusCode': 200,
            'scrapeId': '00000000-0000-0000-0000-000000000001'}}}
        with tempfile.TemporaryDirectory() as directory:
            result = cycle.run_cycle({'records': [record]}, {'observations': []}, '2026-08-02T00:00:00Z',
                datetime(2026, 9, 14, tzinfo=timezone.utc), 45, Path(directory), lambda url: response, lambda: self.fail())
            proof = json.loads(Path(result['records'][0]['capture_path']).read_text())
            self.assertEqual(proof['missing_retained_excerpts'], ['Old price is $100.'])
            self.assertEqual(proof['whole_document_comparison'], 'UNKNOWN_NO_HISTORICAL_BODY')
            self.assertEqual(result['records'][0]['decision_required'], 'MATERIAL_REVIEW_REQUIRED')


if __name__ == '__main__':
    unittest.main()
