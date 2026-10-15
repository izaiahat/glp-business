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

    def held_replacement_fixture(self, root):
        """Synthetic admission doubles; real seals, hold, lineage and source IO."""
        buffer = carrier.buffer
        source = root / 'source'
        base = source / carrier.SOURCE_REL
        base.mkdir(parents=True)
        (base / 'manifest.json').write_text(json.dumps({
            'schema':'glp-accepted-guides-source-v1', 'entries':[]}))
        pool = root / 'pool'
        old_id, new_id = 'a'*24, 'b'*24
        controls = root / 'control.py'; controls.write_text('# synthetic control')
        selection = root / 'selection.json'; selection.write_text('{}')
        entries = []
        for ident, title in ((old_id, 'Synthetic predecessor'), (new_id, 'Synthetic successor')):
            home = pool / 'entries' / ident
            packet = home / 'packet'; packet.mkdir(parents=True)
            (packet / '06-article.json').write_text(json.dumps({'title':title, 'canonical_path':'/guides/new-guide/'}))
            (packet / '07-qa-receipt.json').write_text('{}')
            review = home / 'review.json'; review.write_text('{}')
            entry = {'entry_id':ident, 'schema':buffer.ENTRY_SCHEMA,
                     'status':'READY_UNPUBLISHED', 'publication_effects':0,
                     'topic_id':'synthetic-topic', 'article_id':'synthetic-article',
                     'target_route':'/guides/new-guide/', 'packet_closure_sha256':ident*2,
                     'topic_provenance':{}, 'topic_authority':buffer._spec(selection),
                     'selection_receipt':buffer._spec(selection),
                     'control_files':{str(controls):carrier.digest(controls)},
                     'pass_receipt':buffer._spec(review), 'qa':buffer._spec(packet / '07-qa-receipt.json')}
            entries.append(entry)
        old, new = entries
        old_manifest = pool / 'entries' / old_id / 'entry.json'
        buffer._write_once(old_manifest, old, 'entry_sha256')
        old = buffer._read_sealed(old_manifest, buffer.ENTRY_SCHEMA, 'entry_sha256')
        old_packet = old_manifest.parent / 'packet'
        carrier.land_packet(source, old_packet, old)
        old_row = carrier.source_rows(source)[0]
        hold = pool / 'held' / (old_id + '.json')
        buffer._write_once(hold, {'schema':buffer.HOLD_SCHEMA, 'status':'HELD_READY_ENTRY',
            'entry_id':old_id, 'entry_manifest':buffer._spec(old_manifest),
            **{key:old[key] for key in ('topic_id','article_id','target_route','packet_closure_sha256')},
            'publication_effects':0, 'disposition':'excluded_from_ready_selection_no_successor_admission',
            'reason':'synthetic independent block', 'evidence':buffer._spec(selection)}, 'hold_sha256')
        new['successor_of'] = {'entry_id':old_id, 'entry_manifest':buffer._spec(old_manifest), 'hold':buffer._spec(hold)}
        reviewed_packet = pool / 'entries' / 'packet'
        reviewed_packet.mkdir()
        for name in ('06-article.json','07-qa-receipt.json'):
            (reviewed_packet / name).write_bytes((pool / 'entries' / new_id / 'packet' / name).read_bytes())
        new['registered_review'] = {key:buffer._spec(pool / 'entries' / new_id / 'review.json')
                                    for key in ('receipt','closeout','execution')}
        new_manifest = pool / 'entries' / new_id / 'entry.json'
        buffer._write_once(new_manifest, new, 'entry_sha256')
        new = buffer._read_sealed(new_manifest, buffer.ENTRY_SCHEMA, 'entry_sha256')
        return source, new_manifest.parent / 'packet', old, new, old_row

    def replacement_admission_doubles(self, old, new):
        from contextlib import ExitStack
        stack = ExitStack()
        def validate(path, **kwargs):
            actual = carrier.buffer._read_sealed(path, carrier.buffer.ENTRY_SCHEMA, 'entry_sha256')
            return actual
        stack.enter_context(patch.object(carrier.buffer, 'validate_entry', side_effect=validate))
        evidence = {**new, 'subjects':[], 'subject_manifest':{}, 'raw_review':{}}
        stack.enter_context(patch.object(carrier.buffer, 'validate_admission_evidence', return_value=evidence))
        stack.enter_context(patch.object(carrier.buffer, '_registered_successor_review', return_value=new['registered_review']))
        stack.enter_context(patch.object(carrier.buffer, 'load_selection_receipt', return_value=({}, {})))
        return stack

    def test_source_bound_held_predecessor_replacement_preserves_history_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, packet, old, new, old_row = self.held_replacement_fixture(root)
            preserved = {str(p):p.read_bytes() for p in (root / 'pool').rglob('*') if p.is_file()}
            old_source = {key:(source / old_row[key]['path']).read_bytes() for key in ('article','qa','review')}
            with self.replacement_admission_doubles(old, new):
                self.assertTrue(carrier.land_packet(source, packet, new))
                manifest = json.loads((source / carrier.SOURCE_REL / 'manifest.json').read_text())
                self.assertEqual(len(manifest['entries']), 1)
                row = manifest['entries'][0]
                self.assertEqual(row['entry_id'], new['entry_id'])
                self.assertEqual(manifest['replacement_history'], [{'predecessor':old_row,
                    'successor_entry_id':new['entry_id'], 'successor_of':new['successor_of']}])
                self.assertEqual((source / row['article']['path']).read_bytes(), (packet / '06-article.json').read_bytes())
                before = {str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()}
                self.assertFalse(carrier.land_packet(source, packet, new))
                self.assertEqual(before, {str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()})
            self.assertEqual(preserved, {str(p):p.read_bytes() for p in (root / 'pool').rglob('*') if p.is_file()})
            for key, contents in old_source.items():
                self.assertEqual((source / old_row[key]['path']).read_bytes(), contents)

    def test_held_replacement_refusal_controls_by_contract(self):
        controls = {
            'ordinary_same_route_without_explicit_lineage':'source_identity_drift',
            'unsealed_successor_argument':'source_successor_entry_binding_invalid',
            'wrong_predecessor_row_id':'source_successor_predecessor_identity_invalid',
            'predecessor_source_byte_drift':'source_packet_identity_drift',
            'missing_hold_not_replacement_authority':'source_predecessor_hold_pin_invalid',
            'tampered_hold_pin':'source_predecessor_hold_pin_invalid',
            'ambiguous_existing_route':'source_identity_drift',
            'different_successor_route_existing_id':'source_identity_drift',
            'consumed_predecessor':'source_successor_already_consumed',
            'consumed_successor':'source_successor_already_consumed',
            'held_predecessor_quarantine_conflict':'buffer_successor_predecessor_quarantined_recorded',
            'active_predecessor_claim':'buffer_successor_predecessor_claims_recorded',
            'published_predecessor_record':'buffer_successor_predecessor_published_recorded',
            'successor_hold':'source_successor_not_ready',
            'successor_quarantine':'source_successor_not_ready',
            'review_subject_byte_drift':'source_successor_review_binding_invalid',
            'registered_review_rejected':'synthetic_registered_review_refusal',
            'acceptance_gate_rejected':'synthetic_acceptance_refusal',
            'versioned_destination_drift':'source_identity_drift',
        }
        for contract, reason in controls.items():
            with self.subTest(contract=contract), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, packet, old, new, old_row = self.held_replacement_fixture(root)
                pool = root / 'pool'
                mp = source / carrier.SOURCE_REL / 'manifest.json'
                manifest = json.loads(mp.read_text())
                if contract == 'ordinary_same_route_without_explicit_lineage':
                    new.pop('successor_of')
                elif contract == 'unsealed_successor_argument':
                    new['article_id'] = 'unadmitted'
                elif contract == 'wrong_predecessor_row_id':
                    manifest['entries'][0]['entry_id'] = 'c'*24
                elif contract == 'predecessor_source_byte_drift':
                    (source / old_row['article']['path']).write_text('{}')
                elif contract == 'missing_hold_not_replacement_authority':
                    (pool / 'held' / (old['entry_id']+'.json')).unlink()
                elif contract == 'tampered_hold_pin':
                    hold = pool / 'held' / (old['entry_id']+'.json')
                    hold.chmod(0o644)
                    hold.write_text('{}')
                elif contract == 'ambiguous_existing_route':
                    manifest['entries'].append(old_row)
                elif contract == 'different_successor_route_existing_id':
                    manifest['entries'][0].update(entry_id=new['entry_id'],route='/guides/other/')
                elif contract.startswith('consumed_'):
                    consumed_entry = old if contract.endswith('predecessor') else new
                    operation = 'synthetic-' + contract
                    carrier.record_consumption(pool, consumed_entry, operation,
                        {'status':'PASS_BUFFER_PUBLISHED',
                         'receipt':str(root / 'synthetic-publication.json')})
                    consumed_path = carrier.consumed_path(pool, operation)
                    consumed = carrier.buffer._read_sealed(consumed_path,
                        carrier.buffer.CONSUME_SCHEMA, 'consumption_sha256')
                    self.assertEqual(consumed['entry_id'], consumed_entry['entry_id'])
                    self.assertEqual(consumed['operation_id'], operation)
                    self.assertNotEqual(consumed_path.name, consumed_entry['entry_id']+'.json')
                elif contract in ('held_predecessor_quarantine_conflict','active_predecessor_claim','published_predecessor_record','successor_hold','successor_quarantine'):
                    directory = {'held_predecessor_quarantine_conflict':'quarantined',
                                 'active_predecessor_claim':'claims','published_predecessor_record':'published',
                                 'successor_hold':'held','successor_quarantine':'quarantined'}[contract]
                    ident = new['entry_id'] if contract.startswith('successor_') else old['entry_id']
                    (pool / directory).mkdir(exist_ok=True)
                    record = pool / directory / (ident+'.json')
                    if directory == 'quarantined':
                        carrier.buffer._write_once(record, {'schema':carrier.buffer.QUARANTINE_SCHEMA,
                            'entry_id':ident, 'publication_effects':0}, 'quarantine_sha256')
                    elif directory == 'held':
                        payload = json.loads((pool / 'held' / (old['entry_id']+'.json')).read_text())
                        payload.update({key:new[key] for key in ('entry_id','topic_id','article_id','target_route','packet_closure_sha256')})
                        payload['entry_manifest'] = carrier.buffer._spec(packet.parent / 'entry.json')
                        carrier.buffer._write_once(record,payload,'hold_sha256')
                    else:
                        record.write_text(json.dumps({'entry_id':ident}))
                elif contract == 'review_subject_byte_drift':
                    (pool / 'entries/packet/06-article.json').write_text('{}')
                elif contract == 'versioned_destination_drift':
                    (source / carrier.SOURCE_REL / 'new-guide' / ('article-'+new['entry_id']+'.json')).write_text('{}')
                mp.write_text(json.dumps(manifest))
                before = {str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()}
                with self.replacement_admission_doubles(old, new) as stack:
                    if contract == 'registered_review_rejected':
                        stack.enter_context(patch.object(carrier.buffer,'_registered_successor_review',side_effect=RuntimeError('synthetic_registered_review_refusal')))
                    if contract == 'acceptance_gate_rejected':
                        stack.enter_context(patch.object(carrier.buffer,'validate_admission_evidence',side_effect=RuntimeError('synthetic_acceptance_refusal')))
                    with self.assertRaisesRegex(RuntimeError,reason):
                        carrier.land_packet(source,packet,new)
                self.assertEqual(before, {str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()})

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

    def test_published_operation_get_only_recovery_requires_current_bound_source(self):
        import article_buffer_cli as cli
        from types import SimpleNamespace
        cron = SimpleNamespace(article_current_base=lambda: None, article_production_head_id=lambda: None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pointer = root / 'pointer.json'; pointer.write_text('{}')
            entry = {'entry_id':'a'*24,'topic_id':'topic','target_route':'/guides/new/'}
            op = 'host-fixture-op'
            claim = root / 'claim.json'
            carrier.buffer._write_once(claim,{'schema':carrier.buffer.CLAIM_SCHEMA,
                'operation_id':op,'entry_id':entry['entry_id']},'claim_sha256')
            publication = {'status':'PASS_BUFFER_PUBLISHED','source_reconciled':True,
                'deployment_id':'dep','current_base':carrier.buffer._spec(pointer), 'receipt':str(pointer)}
            carrier.record_consumption(root,entry,op,publication,claim)
            with patch.object(cli,'DEFAULT_ROOT',root), patch.object(cli,'POINTER',pointer), \
                 patch.object(cli,'current_context',return_value=(None,root,{entry['target_route']},set())), \
                 patch.object(carrier,'source_rows',return_value=[entry]), \
                 patch.object(carrier,'accepted_entry',return_value=(root,entry,{'title':'Reviewed title'})), \
                 patch.object(carrier,'live_readback',return_value={'http':200,'title':'Reviewed title','url':'https://glpclinicscout.com/guides/new/'}) as live, \
                 patch.object(cron,'article_current_base',return_value={'deployment_id':'dep'}), \
                 patch.object(cron,'article_production_head_id',return_value='dep') as head, \
                 patch.dict(sys.modules,{'scripts.cron_bridge_action':cron}), \
                 patch.object(carrier,'run',side_effect=AssertionError('writer must not run')):
                self.assertEqual(carrier.published_operation_readback(op)['entry_id'],entry['entry_id'])
                self.assertIsNone(carrier.published_operation_readback('wrong-operation'))
                head.return_value='other'
                self.assertIsNone(carrier.published_operation_readback(op)); head.return_value='dep'
                live.side_effect=RuntimeError('wrong exact title')
                self.assertIsNone(carrier.published_operation_readback(op)); live.side_effect=None
                pointer.write_text('{"changed":true}')
                self.assertIsNone(carrier.published_operation_readback(op)); pointer.write_text('{}')
                claim.chmod(0o600)
                claim.write_text('{"tampered":true}')
                self.assertIsNone(carrier.published_operation_readback(op))

    def test_normal_sealed_consumption_get_only_readback(self):
        """Real seals/native terminal shape, no reconciliation-only fields/writers."""
        import article_buffer_cli as cli
        from types import SimpleNamespace
        cron = SimpleNamespace(article_current_base=lambda: None, article_production_head_id=lambda: None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            op = 'host-normal-success'
            entry = {'entry_id':'a'*24,'topic_id':'topic','target_route':'/guides/new/'}
            row = {**entry, 'article':{'path':'article.json','sha256':'a'*64}}
            pr = {'state':'MERGED','url':'https://github.com/izaiahat/glp-site-candidate/pull/50',
                  'headRefOid':'a'*40,'mergeCommit':{'oid':'b'*40}}
            terminal = root / 'terminal.json'
            terminal.write_text(json.dumps({'schema':'glp-opus-production-terminal-receipt-v1',
                'status':'PASS_PRODUCTION_TERMINAL','site_head':pr['headRefOid'],
                'deployment':{'deployment_id':'dep'}}))
            pointer = root / 'pointer.json'
            pointer.write_text(json.dumps({'deployment_id':'dep','site_head':pr['headRefOid'],
                'final_receipt':carrier.buffer._spec(terminal)}))
            claim = root / 'claim.json'
            carrier.buffer._write_once(claim,{'schema':carrier.buffer.CLAIM_SCHEMA,
                'operation_id':op,'entry_id':entry['entry_id']},'claim_sha256')
            result = {'status':'PASS_PRODUCTION_TERMINAL','scope':'operator-approved-main-release',
                      'deployment_id':'dep','site_main':pr['mergeCommit']['oid'],'source_pr':pr['url']}
            live = {'http':200,'title':'Reviewed title','url':carrier.SITE + entry['target_route']}
            publication = carrier.publication_receipt(pr,result,live,row)
            publication['receipt'] = str(terminal)
            carrier.record_consumption(root,entry,op,publication,claim)
            sealed = carrier.consumed_path(root,op)
            before = {p:p.read_bytes() for p in (sealed,claim,pointer,terminal)}
            current = {'deployment_id':'dep','pointer_sha256':carrier.digest(pointer)}
            with patch.object(cli,'DEFAULT_ROOT',root), patch.object(cli,'POINTER',pointer), \
                 patch.object(cli,'current_context',return_value=(None,root,{entry['target_route']},set())), \
                 patch.object(carrier,'source_rows',return_value=[row]), \
                 patch.object(carrier,'accepted_entry',return_value=(root,entry,{'title':'Reviewed title'})), \
                 patch.object(carrier,'live_readback',return_value=live), \
                 patch.object(cron,'article_current_base',return_value=current), \
                 patch.object(cron,'article_production_head_id',return_value='dep'), \
                 patch.dict(sys.modules,{'scripts.cron_bridge_action':cron}), \
                 patch.object(carrier,'run',side_effect=AssertionError('no subprocess')), \
                 patch.object(carrier,'record_consumption',side_effect=AssertionError('no seal writes')):
                recovered = carrier.published_operation_readback(op)
                self.assertIsNotNone(recovered, 'normal successful consumption must acknowledge without recovery-only fields')
                self.assertEqual(recovered['status'],'PASS_PUBLISHED')
                self.assertEqual(recovered['receipt_path'],str(sealed))
                # Exercise the registered wrapper's real postcondition path;
                # only external GET/heartbeat/notification edges are isolated.
                import ast
                from unittest.mock import MagicMock
                tree = ast.parse((ROOT / 'scripts/cron_bridge_action.py').read_text())
                finish = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name == 'finish_action')
                namespace = {'machine_receipt':lambda *a,**k: {'status':'BLOCKED_CONSUME_FAILED','operation_id':op},
                             'heartbeat':lambda *a,**k: {}, 'post_once':lambda *a,**k: {}}
                exec(compile(ast.Module(body=[finish],type_ignores=[]),'registered-finish','exec'),namespace)
                response = MagicMock()
                response.__enter__.return_value.status = 200
                with patch.dict(sys.modules,{'scripts.article_source_carrier':carrier}), \
                     patch('urllib.request.urlopen',return_value=response) as get:
                    effects = namespace['finish_action']('article_carrier_consume',{}, {'rc':0}, {},root / 'state')
                self.assertEqual(effects['machine_status'],'PASS_PUBLISHED')
                self.assertEqual(effects['output_count'],1)
                self.assertEqual(effects['live_status'],200)
                self.assertEqual(get.call_args.args[0].full_url,live['url'])
                # Genuine seals with mismatched terminal/source identities must
                # remain pending, even when the public route still returns 200.
                import copy
                mutations = [
                    ('publication','deployment_id','stale'),
                    ('publication','source_entry',{'entry_id':'b'*24}),
                    ('publication','receipt',str(root / 'wrong-terminal.json')),
                    ('publication','main_release','status','FAIL'),
                    ('publication','main_release','prepare_only',True),
                    ('publication','main_release','site_main','c'*40),
                    ('publication','main_release','source_pr','wrong-pr'),
                    ('publication','source_pr','state','OPEN'),
                    ('publication','source_pr','headRefOid','invalid'),
                ]
                original = json.loads(sealed.read_text())
                for index, change in enumerate(mutations):
                    with self.subTest(change=change):
                        variant = copy.deepcopy(original)
                        variant_op = f'{op}-{index}'
                        variant['operation_id'] = variant_op
                        variant_claim = root / f'claim-{index}.json'
                        carrier.buffer._write_once(variant_claim,{'schema':carrier.buffer.CLAIM_SCHEMA,
                            'operation_id':variant_op,'entry_id':entry['entry_id']},'claim_sha256')
                        variant['claim'] = carrier.buffer._spec(variant_claim)
                        target = variant
                        for key in change[:-2]:
                            target = target[key]
                        target[change[-2]] = change[-1]
                        variant.pop('consumption_sha256')
                        carrier.buffer._write_once(carrier.consumed_path(root,variant_op),variant,'consumption_sha256')
                        self.assertIsNone(carrier.published_operation_readback(variant_op))
                cron.article_production_head_id.return_value = 'stale'
                self.assertIsNone(carrier.published_operation_readback(op))
                cron.article_production_head_id.return_value = 'dep'
                terminal.write_text('{}')
                self.assertIsNone(carrier.published_operation_readback(op))
                terminal.write_bytes(before[terminal])
            self.assertEqual({p:p.read_bytes() for p in before},before)

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
