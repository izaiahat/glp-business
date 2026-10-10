"""Q6 named renderer/producer regressions. Offline synthetic captures only."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_glp_affiliate_wirein_incremental_v2 as builder
import glp_review_editorial_projection as projection


class ReviewProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def bundle(self, captures):
        rows = []
        for i, (url, markdown) in enumerate(captures.items()):
            path = self.root / f'capture-{i}.json'
            raw = json.dumps({'data': {'markdown': markdown}}).encode()
            path.write_bytes(raw)
            rows.append(dict(url=url, markdown=markdown, raw_capture_path=str(path),
                             raw_capture_sha256=hashlib.sha256(raw).hexdigest(), status=200))
        path = self.root / 'bundle.json'
        path.write_text(json.dumps({'captures': rows}))
        return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def renderer(self):
        # Retained defective helper bytes, plus both exact template fragments.
        source = projection.OLD_SCORE + '\n' + projection.OLD_CRITERIA + '\n'
        source += 'function context(linkDark, endedNotice) { const integration = {hold_copy: "one active affiliate link"}; ' + projection.OLD_HOLD + ' return holdCopy; }\n'
        for heading in ('Evidence criteria', 'Published scoring inputs'):
            source += 'const template' + str(len(source)) + ' = `<section class="criteria" id="criteria"><h2>' + heading + '</h2><p>old methodology</p><ul>${criteria}</ul></section>`;\n'
        path = self.root / 'src/provider-review.mjs'
        path.parent.mkdir()
        path.write_text(source)
        return path

    def migrated_js(self, expression):
        path = self.renderer()
        builder.normalize_provider_review_editorial_source(self.root)
        source = path.read_text()
        # Execute actual migrated helper functions and conditional section,
        # independent of site schema/network/authority dependencies.
        source = source[:source.index('const template')]
        preamble = 'const esc=String; const WEIGHTS={price:30,care:25,insurance:15,shipping:15,public_reviews:15}; const LABELS={}; const score=r=>r.overall??null; const hasComparableReviewEvidence=r=>r.social_proof?.status==="VERIFIED" && r.criteria_scores?.public_reviews>0;\n'
        result = subprocess.run(['node', '--input-type=module', '-e', preamble + source + '\nconsole.log(JSON.stringify(' + expression + '));'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_shared_affiliate_status_matches_effective_held_active_and_ended_cta(self):
        result = self.migrated_js('[context(true,null), context(false,null), context(false,"Relationship ended")]')
        self.assertNotIn('one active', result[0])
        self.assertIn('No active', result[0])
        self.assertIn('one active affiliate link', result[1])
        self.assertEqual(result[2], 'Relationship ended')

    def test_shared_missing_scores_omit_section_not_unpublished_or_zero(self):
        result = self.migrated_js('({list:criteriaList({criteria_scores:{price:null}}), summary:scoreBlock({criteria_scores:{}},"Provider")})')
        self.assertEqual(result['list'], '')
        self.assertNotIn('has not published', result['summary'])
        self.assertNotIn('Not published', result['summary'])
        self.assertNotIn('/10', result['summary'])
        source = (self.root / 'src/provider-review.mjs').read_text()
        self.assertEqual(source.count(projection.SECTION), 2)

    def test_shared_criteria_prose_counts_exact_rendered_finite_criteria(self):
        result = self.migrated_js('({list:criteriaList({criteria_scores:{price:70,care:80,shipping:0,public_reviews:90}}), summary:scoreBlock({overall:70,criteria_scores:{price:70,care:80,shipping:0,public_reviews:90}},"Provider")})')
        self.assertEqual(result['list'].count('<li>'), 3)
        self.assertIn('the 3 assessed criteria', result['summary'])
        self.assertIn('0 of 15%', result['list'])

    def test_shared_dated_citation_requires_stored_capture_and_drops_unsupported(self):
        url = 'https://provider.example/'
        research = {'captured_at_utc':'2026-10-10T00:00:00Z', 'fields':{},
                    'first_party_source_captures':self.bundle({url:'official source'})}
        spec = {'program_id':'fixture', 'checked_as_of':'2026-10-10', 'display_name':'Provider',
                'field_sources':{name:[url, 'https://provider.example/unfetched'] for name in ('pricing_and_inclusions', 'cancellation_and_roles', 'regulatory')}}
        output = builder.canonical_field_sources(spec, research)
        self.assertEqual(output['pricing_and_inclusions'], [url])
        captures = projection.captured_review_sources(research)
        self.assertEqual(captures[url]['binding']['raw_capture_sha256'],
                         json.loads(Path(research['first_party_source_captures']['path']).read_text())['captures'][0]['raw_capture_sha256'])
        with self.assertRaisesRegex(RuntimeError, 'field_source_group_missing'):
            builder.canonical_field_sources(spec, {'fields':{}})

    def test_tampered_capture_refuses_not_restamps(self):
        research = {'captured_at_utc':'2026-10-10T00:00:00Z', 'first_party_source_captures':self.bundle({'https://provider.example/':'official'})}
        path = self.root / 'capture-0.json'
        path.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'raw_capture_hash_mismatch'):
            projection.captured_review_sources(research)

    def test_migration_idempotent_and_drift_refuses_without_writing(self):
        path = self.renderer()
        builder.normalize_provider_review_editorial_source(self.root)
        expected = path.read_bytes()
        builder.normalize_provider_review_editorial_source(self.root)
        self.assertEqual(path.read_bytes(), expected)
        path.write_text(path.read_text().replace('const count = assessedCriteria(row).length;', 'bad anchor'))
        expected = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'anchor_drift'):
            builder.normalize_provider_review_editorial_source(self.root)
        self.assertEqual(path.read_bytes(), expected)

    def test_rightwell_comparative_removed_without_changing_other_inputs(self):
        spec = {'program_id':'rightwell-review', 'page_copy':{'bottom_line':'RightWell is strongest for adults comparing care'}, 'row_contract':{}}
        research = {'fields':{}}
        actual, revised = builder.project_review_inputs(spec, research)
        self.assertEqual(actual['page_copy']['bottom_line'], 'RightWell describes care')
        self.assertEqual(revised, research)
        self.assertIn('is strongest', spec['page_copy']['bottom_line'])

    def test_ivim_conflict_shipping_and_affirmative_brand_not_comparator(self):
        terms = 'https://www.ivimhealth.com/terms-and-conditions/'
        plan = 'https://www.ivimhealth.com/glp1idwt/'
        offer = 'https://www.ivimhealth.com/wegovypill/'
        conflict = 'Because GLP-1 medications are prescribed and shipped based on your selected program length, cancellation is not available once your program has started.'
        shipping = 'GLP-1 ID Program medications are not auto-shipped. Each shipment requires clinical review, titration assessment, and individual provider approval prior to dispensing.'
        research = {'captured_at_utc':'2026-10-10T00:00:00Z', 'fields':{'cancellation':{'quote':'Refund less fees.', 'verified_as_of':'2026-10-10'}, 'shipping_wait':{}},
                    'provider_dynamic_profile':{'taxonomy':{'brand_name_tags':['mounjaro','wegovy','zepbound']}, 'evidence_bindings':[{'fields':['taxonomy.brand_name_tags'],'evidence_text':'retail price comparators'}]},
                    'first_party_source_captures':self.bundle({terms:'Refund less fees. '+shipping, plan:conflict, offer:'# The Wegovy® Pill is Here'})}
        spec = {'program_id':'ivim-health-review','page_copy':{},'row_contract':{}}
        _, actual = builder.project_review_inputs(spec,research)
        self.assertIn(conflict, actual['fields']['cancellation']['value'])
        self.assertIn('eligibility remain UNKNOWN', actual['fields']['cancellation']['value'])
        self.assertNotIn('all compounded medications', actual['fields']['shipping_wait']['value'])
        self.assertEqual(actual['provider_dynamic_profile']['taxonomy']['brand_name_tags'], ['wegovy'])
        self.assertEqual(actual['provider_dynamic_profile']['brand_offer_unknown'], ['mounjaro','zepbound'])
        self.assertEqual(actual['provider_dynamic_profile']['evidence_bindings'][0]['evidence_text'], '# The Wegovy® Pill is Here')

    def test_willow_products_qualified_and_pharmacy_claims_attributed(self):
        spec = {'program_id':'willow-review','page_copy':{'description':'oral and injectable GLP-1','bottom_line':'Willow offers semaglutide and tirzepatide'},
                'row_contract':{'best_for':'oral and injectable semaglutide and tirzepatide','medications':'Semaglutide and tirzepatide','starting_price':'Semaglutide from $299; tirzepatide from $399','pros':['Oral and injectable options']}}
        research = {'fields':{'monthly_price':{'value':'Injectable semaglutide $299'},'pharmacy_type':{'value':'licensing assertion','quote':'Provider licensing and USP statement'}}}
        actual, revised = builder.project_review_inputs(spec,research)
        self.assertIn('compounded',actual['page_copy']['description'])
        self.assertIn('Compounded',actual['row_contract']['starting_price'])
        self.assertTrue(revised['fields']['pharmacy_type']['value'].startswith('Willow states:'))
        self.assertIn('Compounded injectable',revised['fields']['monthly_price']['value'])
        self.assertEqual(builder.project_review_inputs(actual, revised), (actual, revised))


if __name__ == '__main__':
    unittest.main()
