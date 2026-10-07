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

class ArticleRoutes(unittest.TestCase):
    def test_reviews_are_articles_and_unsafe_routes_stay_rejected(self):
        pattern = assignment('RENDERER_ARTICLE_ROUTE_PATTERN')
        for route in ['/reviews/example-provider-cost/', '/guides/evidence/', '/best/programs/']:
            self.assertIsNotNone(pattern.fullmatch(route), route)
        for route in ['/cost/price/', '/tools/calculator/', '/reviews/a/b/', '/reviews/a?b/', '/reviews/../']:
            self.assertIsNone(pattern.fullmatch(route), route)

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
