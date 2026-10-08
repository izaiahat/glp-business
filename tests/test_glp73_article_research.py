import importlib.util
from pathlib import Path
import tempfile
import unittest
import json

MODULE = Path(__file__).resolve().parents[1] / 'systems/seo-blog-machine/scripts/article_research.py'

class Research(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location('article_research', MODULE)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

    def test_free_discovery_and_bounded_full_text_preserve_real_responses(self):
        m = self.load(); calls = []
        def transport(body):
            calls.append(body)
            a = body['alexandria'][0]
            return {'success': True, 'data': {'alexandria': [{**a, 'creditsCost': 0 if 'index' in a['provider'] else 5,
                'data': {'text': 'Synthetic test evidence only', 'id': 'fixture'}}]}}
        with tempfile.TemporaryDirectory() as work:
            result = m.collect(Path(work), target_query='fixture provider', clinical_query='fixture GLP1 evidence',
                regulatory_query='fixture FDA rules', reads=[{'kind':'pmc','id':'PMC123'}], request=transport)
            self.assertEqual(len(calls), 3)
            self.assertEqual(result['credits_reported'], 5)
            self.assertEqual([x['provider'] for x in result['captures']][:2], ['firecrawl-research-index', 'firecrawl-gov-index'])
            self.assertEqual(m.validate_manifest(Path(work) / 'research-manifest.json', 'fixture provider')['target_query'], 'fixture provider')
            # Success replay is read-only, not a repeated quota effect.
            m.collect(Path(work), target_query='fixture provider', clinical_query='fixture GLP1 evidence', regulatory_query='fixture FDA rules', reads=[{'kind':'pmc','id':'PMC123'}], request=transport)
            self.assertEqual(len(calls), 3)
            path = Path(result['captures'][0]['response']['path']); path.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'research_capture_hash'):
                m.validate_manifest(Path(work) / 'research-manifest.json', 'fixture provider')

    def test_claim_ledger_rejects_unbound_or_invented_quotes(self):
        m = self.load()
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            def transport(body):
                a = body['alexandria'][0]
                return {'success': True, 'data': {'alexandria': [{**a, 'creditsCost': 0, 'data': {'text': 'Synthetic quoted passage'}}]}}
            m.collect(root / 'research', target_query='fixture', clinical_query='fixture', regulatory_query='fixture', request=transport)
            capture = root / 'source.json'; capture.write_text(json.dumps({'text': 'Synthetic quoted passage'}))
            row = {'claim_id':'C1', 'kind':'clinical', 'quote':'Synthetic quoted passage', 'capture':m.spec(capture),
                   **{k:'fixture only' for k in ['product','population','duration','endpoint','study_design','limitations','source_url']}}
            ledger = {'alexandria_research': m.spec(root / 'research/research-manifest.json'), 'research_claim_ledger':[row]}
            (root / '01-evidence-ledger.json').write_text(json.dumps(ledger))
            (root / '05-claim-source-map.json').write_text(json.dumps({'claims':[{'claim_id':'C1'}]}))
            self.assertTrue(m.packet_research_valid(root, 'fixture'))
            ledger['research_claim_ledger'][0]['quote'] = 'Invented claim'
            (root / '01-evidence-ledger.json').write_text(json.dumps(ledger))
            self.assertFalse(m.packet_research_valid(root, 'fixture'))

    def test_budget_and_identifiers_refuse_before_network(self):
        m = self.load()
        with tempfile.TemporaryDirectory() as work:
            for reads in [[{'kind':'pmc','id':'PMC1'}] * 5, [{'kind':'pmc','id':'https://tracker.invalid'}]]:
                with self.assertRaises(ValueError):
                    m.collect(Path(work), target_query='a', clinical_query='b', regulatory_query='c', reads=reads,
                              request=lambda _: self.fail('network must not run'))

if __name__ == '__main__': unittest.main()
