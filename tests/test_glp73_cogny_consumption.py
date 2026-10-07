import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

PATH=Path(__file__).resolve().parents[1]/'systems/seo-blog-machine/scripts/weekly_strategy_consumer.py'
class CognyInputs(unittest.TestCase):
    def load(self):
        s=importlib.util.spec_from_file_location('weekly_strategy_consumer',PATH)
        m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
    def fixture(self, root):
        updates={'schema':'glp-weekly-strategy-updates-v1','week_start':'2026-10-05',
                 'publication_authorized':False,'strategy_input_write_authorized':True,'unknown_is_not_zero':True,
                 'topic_priorities':[{'query':'target query','route':'/guides/existing/','priority':1,'evidence_class':'MONTHLY_PROXY_AT_LEAST_100'}],
                 'page2_targets':[], 'refresh_list':[{'route':'/guides/existing/','priority':1,'queries':['target query']}],
                 'keyword_gaps':[{'keyword':'new query'}], 'evidence_bindings':{},'source_observations':{}}
        p=root/'updates.json';p.write_text(json.dumps(updates))
        readback=root/'readback.json';readback.write_text(json.dumps({'response':{'content':'# fixture\n'+p.read_text()}}))
        spec=lambda p:{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        pointer={'schema':'glp-weekly-strategy-current-v1','week_start':'2026-10-05','publication_authorized':False,
                 'updates':spec(p),'cogny':{'status':'COGNY_EXACT_CONTENT_READBACK','updates':spec(p),'readback':spec(readback)}}
        dest=root/'current.json';dest.write_text(json.dumps(pointer));return dest,p
    def test_current_bound_cogny_changes_only_known_candidate_order(self):
        m=self.load()
        with tempfile.TemporaryDirectory() as work:
            path,updates=self.fixture(Path(work))
            doc,meta=m.load_strategy(path,as_of=dt.date(2026,10,7))
            self.assertEqual(meta['status'],'VALID_COGNY_STRATEGY_INPUT')
            self.assertGreater(m.preference(doc,'new query','/guides/new/','new_article')['score'],0)
            self.assertIsNone(m.preference(doc,'other query','/guides/other/','new_article'))
            # Existing-page recommendations can never create net-new demand.
            self.assertIsNone(m.preference(doc,'target query','/guides/new/','new_article'))
            self.assertGreater(m.preference(doc,'target query','/guides/existing/','r2_refresh')['score'],0)
            self.assertIsNone(m.preference(doc,'fella health','/reviews/fella-health/','r2_refresh'))
            doc,meta=m.load_strategy(path,as_of=dt.date(2026,10,20))
            self.assertIsNone(doc);self.assertEqual(meta['status'],'UNKNOWN_COGNY_STRATEGY')
            updates.write_text('{}')
            doc,meta=m.load_strategy(path,as_of=dt.date(2026,10,7))
            self.assertIsNone(doc);self.assertIn('hash',meta['reason'])
    def test_actual_mint_selector_and_r2_ordering_consume_preferences(self):
        import ast
        m=self.load()
        with tempfile.TemporaryDirectory() as work:
            path,_=self.fixture(Path(work));doc,_=m.load_strategy(path,as_of=dt.date(2026,10,7))
            scripts=PATH.parent
            a={'keyword':'other query','target_query':'other query','target_route':'/guides/other/','route':'/guides/other/',
               'opportunity_class':'B','opportunity_class_rank':0,'priority':1,'strategy_binding':{'depth_stage':'pillar','queue_position':1},
               'keyword_evidence':{'merged_search_volume':10},'gsc_preference':{'weight':1},'class_balance_multiplier':1,
               'demand_multiplier':1,'queue_position':1,'topic_id':'a'}
            b={**a,'keyword':'new query','target_query':'new query','target_route':'/guides/new/','route':'/guides/new/','topic_id':'b'}
            ns={'gaps':[a,b],'preferred':[],'selection_pool':[a,b],'cogny_strategy':doc,
                'preference_score':m.preference_score,'cogny_preference_score':m.preference_score}
            for filename,target in [('mint_competitor_gap_feed.py','gaps'),('select_opus_content_queue.py','ordered')]:
                tree=ast.parse((scripts/filename).read_text())
                if target=='gaps':
                    node=next(n for n in ast.walk(tree) if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
                              and isinstance(n.value.func,ast.Attribute) and isinstance(n.value.func.value,ast.Name)
                              and n.value.func.value.id=='gaps' and n.value.func.attr=='sort')
                else:
                    node=next(n for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='ordered' for t in n.targets))
                exec(compile(ast.Module(body=[node],type_ignores=[]),filename,'exec'),ns)
                self.assertEqual(ns[target][0]['topic_id'],'b',filename)
            tree=ast.parse((scripts/'r2_update_carrier.py').read_text())
            node=next(n for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='r2_candidates' for t in n.targets)
                      and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Name) and n.value.func.id=='sorted')
            ns['r2_candidates']=[{**a,'priority':'P1'}, {k:v for k,v in {**b,'priority':'P1','route':'/guides/existing/','gsc_queries':[{'query':'target query'}]}.items() if k != 'target_query'}]
            exec(compile(ast.Module(body=[node],type_ignores=[]),'r2_update_carrier.py','exec'),ns)
            self.assertEqual(ns['r2_candidates'][0]['topic_id'],'b')

    def test_missing_is_unknown_not_empty_or_publication_authority(self):
        m=self.load()
        with tempfile.TemporaryDirectory() as work:
            doc,meta=m.load_strategy(Path(work)/'missing',as_of=dt.date(2026,10,7))
            self.assertIsNone(doc);self.assertFalse(meta['applied']);self.assertIsNone(meta['keyword_gap_count'])

if __name__=='__main__':unittest.main()
