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

    def test_manual_registered_no_send_never_imports_messaging(self):
        import os
        import ast
        namespace = {'os':os,'CHANNEL':'existing-ops-alerts'}
        path = ROOT / 'scripts/cron_bridge_action.py'
        for function, args in [('post_once', ('No delivery', {}, {}, None)),
                               ('notify_failure', ('article_carrier_consume', 'retained-receipt', {}, None))]:
            node = next(n for n in ast.parse(path.read_text()).body
                        if isinstance(n,ast.FunctionDef) and n.name == function)
            exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
            with patch.dict(os.environ,{'CRON_BRIDGE_MANUAL_REGISTERED':'1','CRON_BRIDGE_NO_SEND':'1'}):
                result = namespace[function](*args)
            self.assertEqual(result['status'],'SUPPRESSED_NO_SEND')

    def test_new_guide_refill_uses_native_source_contract(self):
        prompt = (ROOT / 'ops/hermes-cron-prompts/glp-seo-buffer-refill.md').read_text()
        self.assertIn('Native guide carrier contract (workflow-v3)', prompt)
        self.assertIn('scripts/article_source_carrier.py', prompt)
        self.assertIn('scripts/glp_release_main.py', prompt)
        self.assertIn('not new-guide admission gates', prompt)
        self.assertIn('Do not admit HIGH', prompt)
        self.assertIn('exact PASS_OPUS_SEO_DRAFT', prompt)

    def test_consume_child_import_resolves_from_cli_source_location(self):
        import subprocess
        source = ROOT / 'systems/seo-blog-machine/scripts/article_buffer_cli.py'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / 'systems/seo-blog-machine/scripts/article_buffer_cli.py'
            cli.parent.mkdir(parents=True)
            cli.write_text(source.read_text())
            scripts = root / 'scripts'; scripts.mkdir()
            (scripts / 'article_source_carrier.py').write_text('def consume(args): return args\n')
            child = '''import ast,sys
from pathlib import Path
path=Path(sys.argv[1])
node=next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='consume_cli')
node.returns=None
for arg in node.args.args: arg.annotation=None
ns={'__file__':str(path),'Path':Path,'sys':sys}
exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
assert ns['consume_cli']('isolated-child')=='isolated-child'
'''
            result = subprocess.run([sys.executable,'-S','-c',child,str(cli)],cwd=tmp,
                                    capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_source_pr_waits_for_checks_to_register_before_watch(self):
        head = 'a' * 40
        pr = {'url': 'https://github.com/izaiahat/glp-site-candidate/pull/50',
              'headRefOid': head}
        registered = {'seen': False, 'queries': 0}
        def github(*argv):
            if argv[1] == 'list':
                return [{**pr, 'state': 'OPEN'}]
            if argv[-1] == 'url,headRefOid':
                return pr
            if argv[-1] == 'headRefOid,statusCheckRollup':
                registered['queries'] += 1
                checks = [] if registered['queries'] == 1 else [{'conclusion': 'SUCCESS'}]
                registered['seen'] = bool(checks)
                return {**pr, 'statusCheckRollup': checks}
            return {**pr, 'state': 'MERGED', 'mergeCommit': {'oid': 'b' * 40}}
        def command(argv, **kwargs):
            if argv[:3] == ['gh', 'pr', 'checks'] and not registered['seen']:
                raise RuntimeError('no checks reported on newly created branch')
            return ''
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(carrier, 'gh', side_effect=github), \
                 patch.object(carrier, 'run', side_effect=command), \
                 patch.object(carrier, 'land_packet', return_value=False), \
                 patch.object(carrier.time, 'sleep'):
                result = carrier.ensure_source_main(Path(tmp), Path(tmp), {'entry_id': 'c'*24})
        self.assertEqual(result['state'], 'MERGED')
        self.assertGreaterEqual(registered['queries'], 3)
        for observation, reason in [
                ({**pr, 'statusCheckRollup': []}, 'source_ci_registration_timeout'),
                ({'headRefOid': 'd'*40, 'statusCheckRollup': []}, 'source_exact_head_ci_not_green')]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                with patch.object(carrier, 'gh', side_effect=[[{**pr, 'state':'OPEN'}], pr, observation]), \
                     patch.object(carrier, 'run') as command, \
                     patch.object(carrier, 'land_packet', return_value=False), \
                     patch.object(carrier.time, 'monotonic', side_effect=[0, 121]):
                    with self.assertRaisesRegex(RuntimeError, reason):
                        carrier.ensure_source_main(Path(tmp), Path(tmp), {'entry_id':'c'*24})
                self.assertFalse(any(c.args[0][:3] == ['gh','pr','merge'] for c in command.call_args_list))

    def test_registered_consume_routes_to_main_source_lane(self):
        import article_buffer_cli
        import inspect
        self.assertIn('article_source_carrier',inspect.getsource(article_buffer_cli.consume_cli))
        self.assertNotIn('apply_opus_article_release.py',inspect.getsource(article_buffer_cli.consume_cli))

if __name__ == '__main__':
    unittest.main()
