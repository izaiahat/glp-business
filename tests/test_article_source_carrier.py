"""Accepted-source carrier regressions with isolated external effects."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'systems/seo-blog-machine/scripts')]
import article_source_carrier as carrier

class SourceCarrierTests(unittest.TestCase):
    def test_source_landing_additive_idempotent_and_refuses_changed_packet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, packet = root / 'source', root / 'packet'
            packet.mkdir()
            base = source / carrier.SOURCE_REL
            base.mkdir(parents=True)
            (base / 'manifest.json').write_text(json.dumps({'schema':'glp-accepted-guides-source-v1','entries':[]}))
            (packet / '06-article.json').write_text('{"title":"Reviewed title"}')
            (packet / '07-qa-receipt.json').write_text('{}')
            review = root / 'review.json'
            review.write_text('{}')
            entry = {'entry_id':'a'*24,'target_route':'/guides/new-guide/','pass_receipt':{'path':str(review)}}
            self.assertTrue(carrier.land_packet(source, packet, entry))
            original = (base / 'manifest.json').read_bytes()
            self.assertFalse(carrier.land_packet(source, packet, entry))
            self.assertEqual((base / 'manifest.json').read_bytes(), original)
            (packet / '06-article.json').write_text('{"title":"Changed claim"}')
            with self.assertRaisesRegex(RuntimeError, 'source_identity_drift'):
                carrier.land_packet(source, packet, entry)

    def test_live_readback_requires_exact_title_indexability_and_canonical(self):
        html = '<title>Reviewed &amp; title</title><meta name="robots" content="index,follow"><link rel="canonical" href="https://glpclinicscout.com/guides/new/">'
        with patch.object(carrier, 'fetch', return_value=(200,html)):
            self.assertEqual(carrier.live_readback('/guides/new/','Reviewed & title')['http'],200)
            with self.assertRaisesRegex(RuntimeError,'live_title'):
                carrier.live_readback('/guides/new/','Unreviewed title')
        with patch.object(carrier, 'fetch', return_value=(200,html.replace('index,follow','noindex,nofollow'))):
            with self.assertRaisesRegex(RuntimeError,'live_indexability'):
                carrier.live_readback('/guides/new/','Reviewed & title')

    def test_reconciliation_uses_real_packet_and_never_republishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = {'entry_id':'b'*24,'topic_id':'topic','target_route':'/guides/already/'}
            manifest = root / 'entries' / entry['entry_id'] / 'entry.json'
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{}')
            pointer = root / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
            pointer.parent.mkdir(parents=True)
            pointer.write_text(json.dumps({'deployment_id':'fixture-deploy','site_head':'fixture-main'}))
            with patch.object(carrier,'ROOT',root), patch.object(carrier,'source_rows',return_value=[{'entry_id':entry['entry_id']}]), patch.object(carrier,'accepted_entry',return_value=(manifest,entry,{'title':'Already live'})), patch.object(carrier,'live_readback',return_value={'http':200,'title':'Already live'}), patch.object(carrier,'record_consumption') as record:
                self.assertEqual(carrier.reconcile_published(root,root)['status'],'PASS_SOURCE_RECONCILED')
                self.assertEqual(record.call_count,1)

    def test_compatibility_preflight_uses_native_source_lane(self):
        import validate_article_carrier_base_compatibility as compatibility
        with patch.object(carrier,'preflight',return_value={'status':'PASS_SOURCE_MAIN_COMPATIBILITY'}) as preflight:
            result = compatibility.validate_all()
        self.assertEqual(result['status'],'PASS_CURRENT_BASE_AND_NEXT_ENTRY_COMPATIBLE')
        self.assertEqual(result['source_lane']['status'],'PASS_SOURCE_MAIN_COMPATIBILITY')
        preflight.assert_called_once()

    def test_materializer_accounts_for_accepted_source_insights_cards(self):
        import ast
        import hashlib
        import html
        import re
        path = ROOT / 'systems/seo-blog-machine/scripts/materialize_opus_run.py'
        node = next(n for n in ast.parse(path.read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'new_article_insights_public_delta')
        namespace = {'Path': Path, 're': re, 'json': json, 'html': html, 'hashlib': hashlib}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'src').mkdir()
            (base / 'src/phase3-page-graph.mjs').write_text('renderPhase3InsightsPage(articles, pageNumber = 1, pageSize = 3)')
            blog = base / 'data/blog'; blog.mkdir(parents=True)
            (blog / 'content-batch.json').write_text(json.dumps({'articles':[{'canonical_path':'/guides/a/'}]}))
            (blog / 'content-national-wow-additions.json').write_text(json.dumps({'articles':[{'canonical_path':'/guides/b/'}]}))
            accepted = blog / 'accepted-guides/c'; accepted.mkdir(parents=True)
            article = accepted / 'article.json'; article.write_text('{"canonical_path":"/guides/c/"}')
            manifest = blog / 'accepted-guides/manifest.json'
            manifest.write_text(json.dumps({'schema':'glp-accepted-guides-source-v1','entries':[{
                'route':'/guides/c/','article':{'path':'data/blog/accepted-guides/c/article.json',
                'sha256':hashlib.sha256(article.read_bytes()).hexdigest()}}]}))
            insights = base / 'public/insights'; insights.mkdir(parents=True)
            (insights / 'index.html').write_text(''.join(f'<a class="p3-story" href="/guides/{slug}/">' for slug in ['a','b','c']))
            result = namespace['new_article_insights_public_delta'](base, '/guides/new/')
            self.assertEqual(result['added'], ['insights/page/2/index.html'])
            article.write_text('{"canonical_path":"/guides/unreviewed/"}')
            with self.assertRaisesRegex(RuntimeError, 'source'):
                namespace['new_article_insights_public_delta'](base, '/guides/new/')

    def test_registered_consume_routes_to_main_source_lane(self):
        import article_buffer_cli
        import inspect
        self.assertIn('article_source_carrier',inspect.getsource(article_buffer_cli.consume_cli))
        self.assertNotIn('apply_opus_article_release.py',inspect.getsource(article_buffer_cli.consume_cli))

if __name__ == '__main__':
    unittest.main()
