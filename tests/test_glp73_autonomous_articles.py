"""Portable GLP73 source regressions; no host state/model invocation."""
import ast
from pathlib import Path
import re
import unittest
import importlib.util
import json
import tempfile
import sys
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'systems/seo-blog-machine/scripts'

def assignment(name):
    tree = ast.parse((SCRIPTS / 'validate_opus_article_packet.py').read_text())
    node = next(x for x in tree.body if isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in x.targets))
    scope = {'re': re}
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<maintained route predicate>', 'exec'), scope)
    return scope[name]

class ReviewPromptAuthority(unittest.TestCase):
    def test_current_transport_supersedes_historical_rubric_without_changing_gates(self):
        sys.path.insert(0, str(SCRIPTS))
        import opus_review_loop
        import review_transports
        rubric = 'Historical Sol/xhigh only.\n### Gate A — evidence\nEvery claim requires a quote.\n'
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            rubric_path = root / 'rubric.md'
            rubric_path.write_text(rubric)
            job = {'review_transport': review_transports.ASTRA_TRANSPORT['transport_id']}
            with patch.object(opus_review_loop, 'RUBRIC', rubric_path):
                prompt = opus_review_loop.build_prompt(job, {'manifest_sha256': 'a' * 64}, root / 'manifest.json', 1, root).read_text()
            self.assertIn('Current invocation authority supersedes historical transport instructions', prompt)
            for value in ('hermes-openai-codex-astra-independent', 'gpt-6-astra-900k', 'high', 'default'):
                self.assertIn(value, prompt)
            self.assertIn('Historical transports are receipt-reading only', prompt)
            self.assertIn(rubric, prompt)
            self.assertIn('PASS requires all five PASS and zero findings', prompt)
            self.assertIn('commercialLinkTaxonomy', prompt)
            self.assertIn('An absent, incorrect or inconsistent canonical commercial-link declaration remains blocking', prompt)
            self.assertEqual(rubric_path.read_text(), rubric)
            with patch.object(opus_review_loop, 'RUBRIC', rubric_path):
                with self.assertRaises(review_transports.TransportError):
                    opus_review_loop.build_prompt({'review_transport':'unapproved'}, {'manifest_sha256':'a'*64}, root/'manifest.json', 2, root)


class CommercialLinkMetadata(unittest.TestCase):
    def test_classification_survives_without_changing_renderer_medical_guard(self):
        tree = ast.parse((SCRIPTS/'compile_review_packet_article.py').read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name=='commercial_link_metadata']
        self.assertEqual(len(nodes), 1)
        ns = {'Any':object}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<metadata>', 'exec'), ns)
        parse = ns['commercial_link_metadata']
        output = parse({'commercialLinkTaxonomy':'commercial_intent','exactServices':['Directory A']})
        self.assertEqual(output['commercial_link_taxonomy'], 'commercial_intent')
        self.assertEqual(output['exact_services'], ['Directory A'])
        self.assertNotIn('surface_taxonomy', output)
        compiler = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name=='compile_packet')
        self.assertIn('commercial_link_metadata(meta)', ast.unparse(compiler))
        self.assertIn("'surface_taxonomy': 'medical'", ast.unparse(compiler))
        self.assertIn("'suppress_cta': True", ast.unparse(compiler))
        for bad in ({'commercialLinkTaxonomy':'commercial_intent'}, {'commercialLinkTaxonomy':'unknown'}):
            with self.assertRaises(RuntimeError): parse(bad)

class ArticleRoutes(unittest.TestCase):
    def test_reviews_are_articles_and_unsafe_routes_stay_rejected(self):
        pattern = assignment('RENDERER_ARTICLE_ROUTE_PATTERN')
        for route in ['/reviews/example-provider-cost/', '/guides/evidence/', '/best/programs/']:
            self.assertIsNotNone(pattern.fullmatch(route), route)
        for route in ['/cost/price/', '/tools/calculator/', '/reviews/a/b/', '/reviews/a?b/', '/reviews/../']:
            self.assertIsNone(pattern.fullmatch(route), route)

class ReviewAliases(unittest.TestCase):
    def test_redirected_native_review_is_not_net_new_article_supply(self):
        path = SCRIPTS / 'select_opus_content_queue.py'
        nodes = [n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == 'redirected_review_routes']
        self.assertEqual(len(nodes), 1, 'redirect alias preauthoring exclusion missing')
        ns = {'re':re, 'Path':Path}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), ns)
        with tempfile.TemporaryDirectory() as work:
            p=Path(work)/'_redirects';p.write_text('/reviews/shed/ /reviews/shedrx/ 301\n/reviews/* /providers/ 301\n/guides/old/ /guides/new/ 301\n')
            self.assertEqual(ns['redirected_review_routes'](p), {'/reviews/shed/'})

class RepairScope(unittest.TestCase):
    def test_new_evidence_and_inbound_failures_have_real_source_repair_scope(self):
        from collections import defaultdict
        path = SCRIPTS / 'validate_opus_article_packet.py'
        tree = ast.parse(path.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'deterministic_repair_request')
        ns = {'json': json, 'Any': object}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), ns)
        result = ns['deterministic_repair_request']({'alexandria_claim_ledger':False,'inbound_link_plan_publishable':False,'selection_receipt_bound':False}, defaultdict(int), parity_min_words=2400, parity_max_words=3600)
        rows = {r['check']:r for r in result}
        self.assertTrue(rows['alexandria_claim_ledger']['repairable'])
        self.assertIn('01-evidence-ledger.json', rows['alexandria_claim_ledger']['allowed_subject_files'])
        self.assertIn('02-serp-brief.json', rows['inbound_link_plan_publishable']['allowed_subject_files'])
        self.assertFalse(rows['selection_receipt_bound']['repairable'])
        self.assertEqual(rows['selection_receipt_bound']['allowed_subject_files'], [])
        for row in result: self.assertNotIn('00-authority.json', row['allowed_subject_files'])

class R2ReviewRoute(unittest.TestCase):
    def test_r2_uses_same_current_astra_route_as_new_articles(self):
        sys.path.insert(0, str(SCRIPTS))
        import review_transports
        path = ROOT / 'scripts/glp_seo_updater_run.py'
        tree = ast.parse(path.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'REVIEW_TRANSPORT' for t in n.targets))
        # The real current transport table is safe metadata; policy Opus lookup
        # is an obsolete host dependency and must not run at updater import.
        with patch.object(review_transports, 'policy_opus_transport', side_effect=AssertionError('legacy Opus invoked')):
            ns = {'review_transports': review_transports}
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), ns)
        self.assertEqual(ns['REVIEW_TRANSPORT'], review_transports.ASTRA_TRANSPORT['transport_id'])
        advisory = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'execute_review_only')
        self.assertNotIn('policy_opus_transport', ast.unparse(advisory))

    def test_codex_review_capacity_uses_reported_windows_not_claude_session_fields(self):
        path = ROOT / 'scripts/glp_seo_updater_run.py'
        tree = ast.parse(path.read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'current_review_pool']
        self.assertEqual(len(nodes), 1, 'current provider capacity resolver missing')
        import datetime as dt
        ns = {'dt': dt}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), ns)
        now = dt.datetime(2026,10,7,23,tzinfo=dt.timezone.utc)
        pool = {'provider':'openai-codex','role':'ACTIVE','freshness':'live','fetched_at':'2026-10-07T22:55:00Z',
                'windows':{'seven_day':{'remaining_pct':76}}, 'session_left_pct':None}
        self.assertEqual(ns['current_review_pool']({'pools':[pool]}, now=now), pool)
        for bad in [{**pool,'provider':'anthropic'}, {**pool,'fetched_at':'2026-10-01T00:00:00Z'},
                    {**pool,'windows':{}}, {**pool,'windows':{'seven_day':{'remaining_pct':0}}}]:
            with self.assertRaises(RuntimeError): ns['current_review_pool']({'pools':[bad]}, now=now)

class CarrierInventory(unittest.TestCase):
    def test_reserve_deficit_is_not_a_base_compatibility_failure(self):
        path = SCRIPTS / 'validate_article_carrier_base_compatibility.py'
        tree = ast.parse(path.read_text())
        selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {'require', 'require_publishable_inventory'}]
        namespace = {}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), namespace)
        check = namespace.get('require_publishable_inventory')
        self.assertIsNotNone(check, 'missing separation of refill capacity and publishability')
        check({'status':'BUFFER_CAPACITY_DEFICIT', 'entries':[{'entry_id':'passing'}], 'pool':{'ready':1,'minimum':3,'target':4}})
        for value in [{'status':'UNKNOWN', 'entries':[{}]}, {'status':'BUFFER_CAPACITY_DEFICIT', 'entries':[]}, {'status':'PASS_BUFFER_STATUS', 'entries':None}]:
            with self.assertRaises(RuntimeError): check(value)

class AutonomousReview(unittest.TestCase):
    def test_legacy_invocation_and_bridge_approvals_are_not_current_routes(self):
        sys.path.insert(0, str(SCRIPTS))
        import opus_review_loop as loop
        with tempfile.TemporaryDirectory() as work:
            p = Path(work) / 'job.json'
            for extra in [{'review_transport': 'hermes-openai-codex-sol'}, {'bridge_extension': {'old': 'approval'}}]:
                p.write_text(json.dumps({'schema':'glp-opus-seo-review-job-v1', 'max_rounds':5, **extra}))
                with self.assertRaisesRegex(RuntimeError, 'astra_only|operator_extension_retired'):
                    loop.run(p, False)

    def test_refusals_repair_then_yield_without_operator_or_publishing(self):
        sys.path.insert(0, str(SCRIPTS))
        import opus_review_loop as loop
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            job = {'schema': 'glp-opus-seo-review-job-v1', 'max_rounds': 5, 'job_id': 'test',
                   'topic_id': 'test', 'history_dir': str(root), 'packet_dir': str(root),
                   'review_transport': loop.review_transports.DEFAULT_TRANSPORT_ID,
                   'review_wrapper_sha256': 'x', 'rubric_sha256': 'x', 'review_schema_sha256': 'x'}
            jobpath = root / 'job.json'; jobpath.write_text(json.dumps(job))
            raw = root / 'raw'; raw.write_text('{}')
            def review(argv, **kwargs):
                Path(argv[argv.index('--receipt-file') + 1]).write_text('{}')
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            from contextlib import ExitStack
            with ExitStack() as stack:
                for name, value in {'sha_file': 'x', 'validate_topic_authority': {}, 'run_validator': None,
                                    'subject_snapshot': {}, 'subject_paths': [], 'seal_subjects': ({}, raw),
                                    'build_context_bundle': raw, 'build_prompt': raw,
                                    'parse_result': {'verdict': 'BLOCKED_OPUS_SEO_DRAFT', 'gates': {}, 'findings': [{'id': 'A01'}]},
                                    'validate_review': None}.items():
                    stack.enter_context(patch.object(loop, name, return_value=value))
                stack.enter_context(patch.object(loop.subprocess, 'run', side_effect=review))
                repair = stack.enter_context(patch.object(loop, 'run_repair'))
                publish = stack.enter_context(patch.object(loop, 'run_publish'))
                result = loop.run(jobpath, False)
            self.assertEqual(repair.call_count, 4)
            publish.assert_not_called()
            self.assertFalse(result['bridge_escalation_required'])
            self.assertTrue(result['autonomous_retry_required'])
            self.assertTrue(result['queue_may_advance'])
            self.assertFalse(result['operator_card_created'])
            self.assertEqual(len(result['rounds']), 5)

if __name__ == '__main__': unittest.main()
