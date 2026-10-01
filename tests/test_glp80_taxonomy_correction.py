"""GLP-80 pure in-memory correction controls: no producer main/locks/network."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'site-system/flagship-v2/candidates/glp34-provider-families-20260912/scripts/refresh-therapeutic-taxonomy.py'
POLICY = SOURCE.with_name('therapeutic-taxonomy-refresh-policies.json')
CARE = 'care-bare-rx-glp1-review'


def load_functions():
    # Execute only constants and function definitions; exclude host imports,
    # policy-file loading, main and all effectful calls.
    tree = ast.parse(SOURCE.read_text())
    names = {'FIELDS', 'CARE_BARE_CORRECTION'}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             or isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)]
    module = types.ModuleType('glp80_functions')
    module.__dict__.update(copy=copy, hashlib=hashlib, json=json)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), module.__dict__)
    return module


def fixture():
    row = {'program_id': CARE, 'brand_name_tags': ['mounjaro'], 'verified_as_of': '2026-08-30',
           'evidence_bindings': [{'evidence_sha256': 'positive'}, {'evidence_sha256': 'keep'}]}
    research = {'taxonomy': {'brand_name_tags': ['mounjaro']}, 'verified_as_of': '2026-08-30',
                'evidence_bindings': [{'fields': ['taxonomy.brand_name_tags'], 'evidence_sha256': 'positive'}],
                'calculator': {'mode': 'manual', 'first_month_base_usd': None}}
    profile = {**copy.deepcopy(research), 'normalized_sha256': 'old'}
    return {'therapeutic_taxonomy': {'programs': {CARE: row, 'other': {'sentinel': True}}},
            'provider_dynamic_profiles': {'profiles': {CARE: profile, 'other': {'sentinel': True}}},
            'programs': [{'program_id': CARE, 'research_dynamic_profile': research},
                         {'program_id': 'other', 'hold': 'HELD'}], 'protected_pin': 'unchanged'}


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.module = load_functions()
        self.registry = fixture()
        # Deliberately synthetic predecessor identities. Production is bound to
        # the authenticated exact installed/PR18 target projections instead.
        projections = {'taxonomy_row': self.registry['therapeutic_taxonomy']['programs'][CARE],
                       'dynamic_profile': self.registry['provider_dynamic_profiles']['profiles'][CARE],
                       'research_dynamic_profile': self.registry['programs'][0]['research_dynamic_profile']}
        if hasattr(self.module, 'CARE_BARE_CORRECTION'):
            self.module.CARE_BARE_CORRECTION = copy.deepcopy(self.module.CARE_BARE_CORRECTION)
            self.module.CARE_BARE_CORRECTION['preimages'] = {
                key: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
                for key, value in projections.items()}
            self.module.CARE_BARE_CORRECTION['positive_binding_sha256'] = 'positive'

    def test_exact_reviewed_correction_is_staged_without_mutating_input(self):
        original = copy.deepcopy(self.registry)
        self.assertTrue(hasattr(self.module, 'prepare_reviewed_correction'), 'producer lacks reviewed correction capability')
        row, receipt = self.module.prepare_reviewed_correction(self.registry, CARE)
        self.assertEqual(row['brand_name_tags'], [])
        self.assertEqual(row['evidence_bindings'], [{'evidence_sha256': 'keep'}])
        self.assertEqual(row['verified_as_of'], '2026-08-30')
        self.assertEqual(self.registry, original)
        self.assertEqual(receipt, self.module.CARE_BARE_CORRECTION)

    def test_apply_rechecks_all_preimages_and_preserves_non_targets_and_assessments(self):
        original = copy.deepcopy(self.registry)
        row, receipt = self.module.prepare_reviewed_correction(self.registry, CARE)
        self.assertTrue(hasattr(self.module, 'apply_reviewed_correction'), 'producer lacks guarded commit correction')
        self.module.apply_reviewed_correction(self.registry, CARE, receipt)
        expected = copy.deepcopy(original)
        expected['therapeutic_taxonomy']['programs'][CARE] = row
        expected['provider_dynamic_profiles']['profiles'][CARE]['taxonomy']['brand_name_tags'] = []
        expected['programs'][0]['research_dynamic_profile']['taxonomy']['brand_name_tags'] = []
        self.assertEqual(self.registry, expected)
        # Retain the native mandatory assessment coverage, its text/hash/dates,
        # UNKNOWN calculator amounts, holds and all non-target rows.
        self.assertEqual(self.registry['programs'][0]['research_dynamic_profile']['evidence_bindings'],
                         original['programs'][0]['research_dynamic_profile']['evidence_bindings'])
        for projection in ['taxonomy_row', 'dynamic_profile', 'research_dynamic_profile']:
            changed = copy.deepcopy(original)
            self.module.correction_projections(changed)[projection]['verified_as_of'] = '2026-08-31'
            before = copy.deepcopy(changed)
            with self.assertRaisesRegex(ValueError, 'preimage_changed'):
                self.module.apply_reviewed_correction(changed, CARE, receipt)
            self.assertEqual(changed, before)
        tampered = {**receipt, 'brand_name_tags_after': ['wegovy']}
        with self.assertRaisesRegex(ValueError, 'receipt_changed'):
            self.module.apply_reviewed_correction(original, CARE, tampered)

    def test_native_metadata_uses_native_digest_and_count_producers(self):
        self.assertTrue(hasattr(self.module, 'derive_reviewed_metadata'), 'missing native correction metadata derivation')
        expected = copy.deepcopy(self.registry)
        expected['provider_dynamic_profiles']['profiles'][CARE]['normalized_sha256'] = 'native-result'
        seen = []
        def run(argv, **kwargs):
            seen.append((argv, kwargs))
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(expected), stderr='')
        self.module.subprocess = types.SimpleNamespace(run=run)
        self.module.ROOT = Path('/synthetic/native/root')
        result = self.module.derive_reviewed_metadata(self.registry)
        self.assertEqual(result, expected)
        argv, kwargs = seen[0]
        self.assertEqual(argv[:3], ['node', '--input-type=module', '-e'])
        self.assertIn('normalizedProfileSha256', argv[3])
        self.assertIn('deriveDynamicFacetCounts', argv[3])
        self.assertNotIn('asOf', argv[3])
        self.assertNotIn('verified_as_of=', argv[3])
        self.assertEqual(json.loads(kwargs['input']), self.registry)

    def test_registered_caller_wires_correction_around_unchanged_guards(self):
        tree = ast.parse(SOURCE.read_text())
        main = ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main'))
        self.assertIn('prepare_reviewed_correction(before, pid)', main)
        self.assertLess(main.index('prepare_reviewed_correction('), main.index('verify_sources('))
        self.assertIn("manifest['previous_row'] = before['therapeutic_taxonomy']['programs'][pid]", main)
        self.assertIn("manifest['reviewed_correction'] = correction", main)
        self.assertLess(main.index('check_previous_row('), main.index('apply_reviewed_correction('))
        self.assertLess(main.index('apply_reviewed_correction('), main.index("row['verified_as_of'] ="))
        self.assertLess(main.index('derive_reviewed_metadata('), main.index('validate(candidate,'))
        self.assertLess(main.index('validate(candidate,'), main.index('os.replace('))
        self.assertIn("REGISTRY.read_bytes() != original", main)

    def test_curex_retained_affirmative_reanchor_and_missing_offer_refusal(self):
        # Exact paragraphs from retained capture sha256
        # 8712b05c8f971d6be6caca58dd3b379ec5e56662f9a0fbf4244dd4c0c912dd43,
        # https://getcurex.com/cl/glp, received 2026-09-30T12:01:27.664129+00:00.
        # Retained marketing context is fixture evidence, not an efficacy claim.
        import re
        self.module.re = re
        policy = json.loads(POLICY.read_text())['curex-glp-1']
        quote = 'If medication is appropriate for you, options may include compounded semaglutide or tirzepatide'
        self.assertEqual(policy['quotes']['treatment_type_tags']['compounded'], quote)
        offer = 'If medication is appropriate for you, options may include compounded semaglutide or tirzepatide — GLP-1 treatments formulated specifically for you by a licensed pharmacy, which may include supportive ingredients like vitamin B12 to help you reach your goals faster.'
        other = '\n\n'.join([
            'We do not prescribe Ozempic®, Wegovy®, Zepbound®, or Mounjaro®, but we may prescribe treatments containing compounded semaglutide or tirzepatide, formulated specifically for you by a licensed pharmacy.',
            'Curex offers oral dissolving tablets (ODT) for both semaglutide and tirzepatide. Simply dissolve under your tongue once daily — no needles, no syringes, no measuring.',
            'If you do choose the injectable format, we provide clear instructions and your care team is available to walk you through your first dose.',
            'Please note that in California, state regulations currently limit prescriptions to oral tablet formulations only — injectable medications are not available to California residents at this time.',
        ])
        row = {'molecule_tags': ['semaglutide', 'tirzepatide'], 'brand_name_tags': [],
               'delivery_form_tags': ['injectable', 'oral'], 'treatment_type_tags': ['compounded'],
               'verified_as_of': '2026-08-30'}
        before = copy.deepcopy((row, policy))
        supported = self.module.support(row, policy, offer + '\n\n' + other)
        self.assertEqual(supported['treatment_type_tags']['compounded']['quote'], quote)
        with self.assertRaisesRegex(ValueError, 'assertion_no_longer_supported:treatment_type_tags:compounded'):
            self.module.support(row, policy, other)
        self.assertEqual((row, policy), before)

    def test_reviewed_policy_has_two_care_sources_and_exact_five_novi_literals(self):
        policy = json.loads(POLICY.read_text())
        self.assertIn('sources', policy[CARE], 'Care reviewed policy not reconciled')
        sources = policy[CARE]['sources']
        self.assertEqual([s['url'] for s in sources], ['https://www.carebarerx.com/products/weight-loss-medications',
                                                    'https://www.carebarerx.com/home2'])
        self.assertFalse(any(s['quotes'].get('brand_name_tags') for s in sources))
        self.assertEqual(set(sources[0]['quotes']['molecule_tags']), {'semaglutide', 'tirzepatide'})
        self.assertEqual(set(sources[0]['quotes']['delivery_form_tags']), {'oral', 'injectable'})
        self.assertEqual(set(sources[1]['quotes']['treatment_type_tags']), {'compounded'})
        novi = json.dumps(policy['novi-weight-loss-glp-1'], ensure_ascii=False)
        self.assertEqual(novi.count('.avif)'), 5)
        self.assertNotIn('.png)', novi)


if __name__ == '__main__':
    unittest.main()
