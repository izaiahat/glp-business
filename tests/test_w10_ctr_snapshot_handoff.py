"""Offline snapshot coverage; uncurated host imports excluded."""
import ast
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
SCRIPTS = Path(__file__).resolve().parents[1] / 'systems/seo-blog-machine/scripts'
sys.path.insert(0, str(SCRIPTS))

def snapshot_function():
    tree = ast.parse((SCRIPTS / 'improvement_loops.py').read_text())
    names = {'build_loop_b_snapshot', 'months_between'}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = dict(Path=Path, dt=dt, sqlite3=sqlite3, re=re, Any=object)
    ns['canonical_feedback_route'] = lambda route, aliases: aliases.get(route, route)
    ns['digest'] = lambda x: hashlib.sha256(json.dumps(x, sort_keys=True).encode()).hexdigest()
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<snapshot>', 'exec'), ns)
    return ns['build_loop_b_snapshot']

def ctr_proposals(page):
    tree = ast.parse((SCRIPTS / 'improvement_loops.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'loop_c']
    ns = dict(dt=dt, Any=object, RELEASE_CHAIN=['unchanged-release-chain'])
    ns['proposal_id'] = lambda *args: 'offline-fixture-proposal'
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<loop-c>', 'exec'), ns)
    return ns['loop_c'](page, dt.date(2026,10,9))

class SnapshotHandoff(unittest.TestCase):
    def test_native_acknowledgment_consumes_route_without_inventing_unknowns(self):
        from ctr_snapshot import route_handoff
        snapshot = {'as_of': '2026-10-09', 'pages': []}
        consumed = []
        def consume(page, date):
            consumed.append(page)
            return ctr_proposals(page)
        receipt = route_handoff(snapshot, '/guides/not-yet-published/', 'topic-exact', consume)
        self.assertEqual(consumed, [{'path': '/guides/not-yet-published/'}])
        self.assertEqual(receipt['status'], 'AWAITING_PUBLICATION_AND_QUALIFIED_GSC_DATA')
        self.assertIsNone(receipt['ctr_test'])
        self.assertEqual(receipt['proposals'], [])
        self.assertEqual(receipt['topic_id'], 'topic-exact')
        self.assertEqual(receipt['effects']['title_changes'], 0)

    def test_measured_route_reaches_ctr_consumer(self):
        with tempfile.TemporaryDirectory() as work:
            db = Path(work) / 'fixture.db'
            c = sqlite3.connect(db)
            c.execute('CREATE TABLE observability_page_daily(metric_date TEXT,page_path TEXT,gsc_clicks INT,gsc_impressions INT,gsc_position REAL)')
            c.executemany('INSERT INTO observability_page_daily VALUES(?,?,?,?,?)', [
                ('2026-10-09','/guides/new-article/',1,100,5.0),
                ('2026-10-09','/guides/peer/',10,100,5.2)])
            c.commit(); c.close()
            pages = snapshot_function()(db, dt.date(2026,10,9))['pages']
            ctr = next(p for p in pages if p['path']=='/guides/new-article/')['ctr_test']
            self.assertEqual(ctr['actual_ctr'], 0.01)
            self.assertEqual(ctr['expected_ctr'], 0.10)
            self.assertEqual(ctr['position'], 5.0)
            result = ctr_proposals({'path':'/guides/new-article/', 'ctr_test':ctr})
            self.assertEqual(result[0]['action'], 'PROPOSE_TITLE_REWRITE_AND_LOG_BASELINE')
            self.assertEqual(result[0]['evaluation_window_days'], [21,28])
            from ctr_snapshot import route_handoff
            native = route_handoff({'as_of': '2026-10-09', 'pages': pages}, '/guides/new-article/', 'topic-measured', lambda p, d: ctr_proposals(p))
            self.assertEqual(native['status'], 'MEASURED_CTR_CONSUMED')
            self.assertEqual(native['ctr_test'], ctr)
            self.assertEqual(native['proposals'], result)

    def test_unknown_stale_or_missing_peer_never_becomes_zero(self):
        from ctr_snapshot import measured_ctr_tests
        base = dict(metric_date='2026-10-09',page_path='/guides/a/',gsc_clicks=1,gsc_impressions=100,gsc_position=5)
        now = dt.date(2026,10,9)
        for rows in ([base], [{**base,'gsc_clicks':None}], [{**base,'gsc_position':float('nan')}], [{**base,'metric_date':'2026-09-01'}]):
            self.assertEqual(measured_ctr_tests(rows, now, lambda x:x), {})
        self.assertEqual(ctr_proposals({'path':'/guides/not-yet-published/'}), [])
