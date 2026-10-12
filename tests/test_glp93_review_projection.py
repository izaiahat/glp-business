"""Q6 named renderer/producer regressions. Offline synthetic captures only."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp_review_editorial_projection as projection

# The constructor has uncurated host-only eager imports. Execute its unchanged
# pure citation-projection AST, with only its maintained helper dependency.
# This is function-level CI, not native authoring/adoption acceptance. Q7 also
# exercises the complete real host producer against authenticated Q4 captures.
import types
builder = types.ModuleType('review_producer_pure_fixture')
builder.__dict__.update(captured_review_sources=projection.captured_review_sources,
                        project_review_inputs=projection.project_review_inputs,
                        normalize_provider_review_editorial_source=projection.normalize_provider_review_editorial_source)
producer_tree = ast.parse((ROOT / 'scripts/build_glp_affiliate_wirein_incremental_v2.py').read_text())
nodes = [node for node in producer_tree.body if isinstance(node, ast.FunctionDef)
         and node.name == 'canonical_field_sources']
assert len(nodes) == 1
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'maintained-producer-pure-fixture', 'exec'), builder.__dict__)


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

    def test_current_calculator_inventory_anchor_preserves_unknown_eligibility(self):
        nodes = [n for n in producer_tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in {'sub_once', 'append_calculator_inventory_test_ids'}]
        self.assertEqual(len(nodes), 2, 'current canonical inventory assertion has no maintained migration')
        import re
        env = {'Path':Path, 're':re}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'real-producer-inventory-helper', 'exec'), env)
        f = self.root / 'test.mjs'
        for target in ['canonicalIds', 'eligibleIds']:
            before = f'for (const id of ["old"]) assert.ok({target}.has(id), id);\nassert.equal(eligibleIds.has("strut-health"), false);\n'
            f.write_text(before)
            env['append_calculator_inventory_test_ids'](f, ['new'])
            self.assertEqual(f.read_text(), before.replace('["old"]', '["old", "new"]'))
        f.write_text('for (const id of ["old"]) assert.ok(anything.has(id), id);')
        before=f.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'patch_match_count'):
            env['append_calculator_inventory_test_ids'](f, ['new'])
        self.assertEqual(f.read_bytes(), before)

    def test_internal_operator_notes_never_reach_reader_inputs(self):
        tyde = ("Reported information: Tyde's current official state list contains all 50 US states. "
                "The MaxBounty offer separately excludes California for campaign targeting; preserve that commercial exclusion.")
        instarx = ('UNKNOWN consumer service-state list: the captured homepage shows this question without an answer. '
                   'Network traffic-state restrictions are separate from clinical eligibility.')
        spec = {'program_id': 'fixture-review', 'page_copy': {'bottom_line': 'Clinician review. OPERATOR: hold link.'},
                'row_contract': {'medications': 'Compounded options', 'pros': ['Clear terms', 'Keep the Katalys row dark.']}}
        research = {'fields': {'states_served': {'value': tyde, 'quote': 'states'}, 'eligibility': {'value': instarx}}}
        actual, revised = builder.project_review_inputs(spec, research)
        self.assertEqual(revised['fields']['states_served']['value'],
                         "Reported information: Tyde's current official state list contains all 50 US states.")
        self.assertEqual(revised['fields']['eligibility']['value'],
                         'UNKNOWN consumer service-state list: the captured homepage shows this question without an answer.')
        self.assertEqual(actual['page_copy']['bottom_line'], 'Clinician review.')
        self.assertEqual(actual['row_contract']['pros'], ['Clear terms'])
        self.assertEqual(actual['row_contract']['medications'], 'Compounded options')
        self.assertEqual(len(actual['internal_notes']), 4)  # retained off-page, not discarded
        self.assertIn('MaxBounty', research['fields']['states_served']['value'])  # historical packet unmutated
        rendered = json.dumps([actual['page_copy'], actual['row_contract'],
                               {k: v['value'] for k, v in revised['fields'].items()}])
        self.assertIsNone(projection.INTERNAL_NOTE.search(rendered))
        self.assertEqual(builder.project_review_inputs(actual, revised)[1], revised)
        with self.assertRaisesRegex(RuntimeError, 'review_internal_note_emptied_copy:page_copy.title'):
            projection.project_internal_notes({'page_copy': {'title': 'Awin'}, 'row_contract': {}}, {'fields': {}})

    def test_feelgood_medications_follow_visible_product_labels(self):
        url = projection.FEELGOOD_WEIGHT_LOSS
        research = {'captured_at_utc': '2026-10-10T00:00:00Z', 'fields': {'pharmacy_type': {'value': 'Pharmacy UNKNOWN.'}},
                    'first_party_source_captures': self.bundle({url: 'Compounded Injections Compounded Tablets Original Injections',
                                                                projection.FDA_UNAPPROVED_GLP1: self.FDA_MD})}
        spec = {'program_id': 'feelgood-review', 'page_copy': {}, 'row_contract': {'medications': 'Compounded semaglutide and tirzepatide'}}
        actual, _ = builder.project_review_inputs(spec, research)
        self.assertEqual(actual['row_contract']['medications'], projection.FEELGOOD_MEDICATIONS)
        research['first_party_source_captures'] = self.bundle({url: 'Compounded Injections only',
                                                               projection.FDA_UNAPPROVED_GLP1: self.FDA_MD})
        with self.assertRaisesRegex(RuntimeError, 'official_quote_missing'):
            builder.project_review_inputs(spec, research)

    def test_feelgood_original_injections_label_never_becomes_brand_supply(self):
        # RV13 PR424-RV13-02: a generic "Original Injections" product label is
        # not evidence of a branded medication.
        url = projection.FEELGOOD_WEIGHT_LOSS
        research = {'captured_at_utc': '2026-10-10T00:00:00Z', 'fields': {'pharmacy_type': {'value': 'Pharmacy UNKNOWN.'}},
                    'first_party_source_captures': self.bundle({
                        url: '### Compounded Injections\n\n### Compounded Tablets\n\n### Original Injections\n\nQuantities are limited.',
                        projection.FDA_UNAPPROVED_GLP1: self.FDA_MD})}
        spec = {'program_id': 'feelgood-review', 'page_copy': {}, 'row_contract': {'medications': 'x'}}
        medications = builder.project_review_inputs(spec, research)[0]['row_contract']['medications']
        self.assertNotRegex(medications.lower(), r'brand')
        for label in ('Compounded Injections', 'Compounded Tablets', 'Original Injections'):
            self.assertIn(label, medications)
        self.assertIn('UNKNOWN', medications)

    def test_feelgood_compounded_warning_is_bound_to_fda_capture_not_feelgood_terms(self):
        # RV13 feelgood-review-regulatory: the nonapproval warning is FDA's
        # statement; FeelGood terms may support only the pharmacy-role claims.
        terms, fda = 'https://www.feelgoodmeds.com/terms-of-use', projection.FDA_UNAPPROVED_GLP1
        pharmacy = ('Reported information: FeelGood states it is not acting as a pharmacy. The captured pages do not '
                    'identify the assigned dispensing pharmacy or establish independent pharmacy licensing. '
                    'Compounded medications are not FDA-approved.')
        labels = 'Compounded Injections Compounded Tablets Original Injections'
        research = {'captured_at_utc': '2026-10-10T00:00:00Z',
                    'fields': {'pharmacy_type': {'value': pharmacy, 'source_url': terms}},
                    'first_party_source_captures': self.bundle({
                        projection.FEELGOOD_WEIGHT_LOSS: labels, terms: 'FeelGood is not acting as a pharmacy',
                        fda: self.FDA_MD})}
        spec = {'program_id': 'feelgood-review', 'page_copy': {}, 'row_contract': {'medications': 'x'},
                'field_sources': {'regulatory': [terms], 'pricing_and_inclusions': [terms], 'cancellation_and_roles': [terms]}}
        actual, revised = builder.project_review_inputs(spec, research)
        value = revised['fields']['pharmacy_type']['value']
        self.assertNotIn('Compounded medications are not FDA-approved.', value)
        self.assertTrue(value.endswith(projection.FEELGOOD_FDA_COMPOUNDED_NOTE), value)
        self.assertTrue(projection.FEELGOOD_FDA_COMPOUNDED_NOTE.startswith('FDA states'))
        self.assertEqual(builder.canonical_field_sources(actual, revised)['regulatory'], [terms, fda])
        self.assertEqual(builder.project_review_inputs(actual, revised), (actual, revised))
        self.assertIn('Compounded medications are not FDA-approved.', research['fields']['pharmacy_type']['value'])
        for missing in (self.FDA_MD.replace('compounded drugs are not FDA approved', 'compounded drugs vary'),
                        self.FDA_MD.replace('does not review compounded drugs', 'reviews some drugs')):
            research['first_party_source_captures'] = self.bundle({
                projection.FEELGOOD_WEIGHT_LOSS: labels, terms: 'FeelGood is not acting as a pharmacy', fda: missing})
            with self.assertRaisesRegex(RuntimeError, 'official_quote_missing:' + fda):
                builder.project_review_inputs(spec, research)
        research['first_party_source_captures'] = self.bundle({
            projection.FEELGOOD_WEIGHT_LOSS: labels, terms: 'FeelGood is not acting as a pharmacy'})
        with self.assertRaisesRegex(RuntimeError, 'official_quote_missing:' + fda):
            builder.project_review_inputs(spec, research)

    # Retained FDA capture structure (markdown link, list marker, line breaks).
    FDA_MD = ('## Concerns with compounded versions of these drugs\n\nA [compounded drug](https://www.fda.gov/q "Q") '
              'might be appropriate if a patient\u2019s medical need cannot be met. However, compounded drugs are not '
              'FDA approved. This means the agency does not review compounded drugs for safety, effectiveness or '
              'quality before they are marketed.\n\n### Retatrutide and cagrilintide cannot be used in compounding\n\n'
              'Retatrutide and cagrilintide cannot be used in compounding under federal law. Additionally, these are '
              'not components of FDA-approved drugs and have not been found safe and effective for any condition.\n\n'
              'The agency has [warned](https://www.fda.gov/w "Warning Letters"):\n\n- Telehealth companies for '
              'marketing unapproved drugs such as retatrutide, including instances of direct marketing to consumers.\n')
    RETA_PRODUCTS_MD = ('ruo![Retatrutide research use only](https://cdn.example/reta.png)\n\nRetatrutide\n\n'
                        'Research Use Only (RUO). Triple-receptor peptide. Not for human consumption.')

    def retaclinic_case(self, fda_md=None, products_md=None):
        fda, products = projection.FDA_UNAPPROVED_GLP1, projection.RETACLINIC_PRODUCTS
        research = {'captured_at_utc': '2026-10-10T00:00:00Z',
                    'fields': {'pharmacy_type': {'value': 'Compounded medications are not FDA-approved.'}},
                    'first_party_source_captures': self.bundle({
                        fda: self.FDA_MD if fda_md is None else fda_md,
                        products: self.RETA_PRODUCTS_MD if products_md is None else products_md})}
        spec = {'program_id': 'retaclinic-review', 'page_copy': {}, 'row_contract': {},
                'field_sources': {'regulatory': ['https://retaclinic.com/']}}
        return spec, research

    def test_retaclinic_risk_note_refuses_rv13_probe_without_retatrutide_ruo_product(self):
        # RV13 synthetic_no_retatrutide_product: "Research Use Only" without a
        # contiguous retatrutide product label must not emit the note.
        spec, research = self.retaclinic_case(products_md=(
            'Research Use Only materials are not medications. No prescription product offer is asserted by this '
            'synthetic fixture.'))
        with self.assertRaisesRegex(RuntimeError, 'official_quote_missing:' + projection.RETACLINIC_PRODUCTS):
            builder.project_review_inputs(spec, research)
        spec, research = self.retaclinic_case(products_md='Retatrutide (prescription). Semaglutide Research Use Only (RUO).')
        with self.assertRaisesRegex(RuntimeError, 'official_quote_missing:' + projection.RETACLINIC_PRODUCTS):
            builder.project_review_inputs(spec, research)

    def test_retaclinic_risk_note_refuses_rv13_probe_without_fda_component_clause(self):
        # RV13 synthetic_missing_fda_approval_clause: every emitted FDA clause is required.
        spec, research = self.retaclinic_case(fda_md=self.FDA_MD.replace(
            'these are not components of FDA-approved drugs and ', 'these '))
        with self.assertRaisesRegex(RuntimeError, 'official_quote_missing:' + projection.FDA_UNAPPROVED_GLP1):
            builder.project_review_inputs(spec, research)

    def test_retaclinic_risk_note_refuses_without_fda_warning_context(self):
        for fda_md in (self.FDA_MD.replace('The agency has [warned](https://www.fda.gov/w "Warning Letters"):\n\n',
                                           'Related topics:\n\n'),
                       self.FDA_MD.replace('Telehealth companies for marketing unapproved drugs such as retatrutide',
                                           'Telehealth companies'),
                       self.FDA_MD.replace('cannot be used in compounding under federal law.', 'are under review.')):
            spec, research = self.retaclinic_case(fda_md=fda_md)
            with self.assertRaisesRegex(RuntimeError, 'official_quote_missing:' + projection.FDA_UNAPPROVED_GLP1):
                builder.project_review_inputs(spec, research)

    def test_retaclinic_fda_risk_note_requires_captured_sources(self):
        fda, products = projection.FDA_UNAPPROVED_GLP1, projection.RETACLINIC_PRODUCTS
        spec, research = self.retaclinic_case()
        actual, revised = builder.project_review_inputs(spec, research)
        self.assertTrue(revised['fields']['pharmacy_type']['value'].endswith(projection.RETACLINIC_RISK_NOTE))
        self.assertEqual(actual['field_sources']['regulatory'], ['https://retaclinic.com/', fda])
        self.assertEqual(builder.project_review_inputs(actual, revised), (actual, revised))
        research['first_party_source_captures'] = self.bundle({products: 'Retatrutide Research Use Only'})
        with self.assertRaisesRegex(RuntimeError, 'official_quote_missing'):
            builder.project_review_inputs(spec, research)

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
