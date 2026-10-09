"""Offline regression: weekly advice orders admitted R2 rows, never admits pages."""
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'systems/seo-blog-machine/scripts'))
sys.path.insert(0, str(ROOT / 'systems/analytics-brain'))
import existing_article_updates as updater


def test_updater_consumes_bound_strategy_without_changing_admission(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('sync', ROOT / 'ops/glp/weekly_strategy_sync.py')
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    from weekly_strategy_consumer import load_strategy

    class Client:
        def call(self, tool, args):
            if tool == 'write_context_node':
                self.node = args
                return {'path': args['path']}
            return self.node

    advice = {'schema': 'glp-weekly-strategy-updates-v1', 'week_start': '2026-10-05',
              'publication_authorized': False, 'strategy_input_write_authorized': True,
              'unknown_is_not_zero': True, 'topic_priorities': [], 'page2_targets': [],
              'refresh_list': [{'route': '/guides/d/', 'priority': 1},
                               {'route': '/guides/not-admitted/', 'priority': 1}],
              'keyword_gaps': None, 'evidence_bindings': {}}
    receipt = sync.sync_updates(Client(), advice, tmp_path / 'sync')
    pointer = tmp_path / 'current.json'
    sync.publish_current(pointer, advice, receipt)
    doc, meta = load_strategy(pointer, as_of=dt.date(2026, 10, 9))
    assert meta['applied'] is True
    candidates = [{'route': '/guides/' + letter + '/', 'priority': 'P2', 'position': 8.0, 'action': 'substantive_refresh',
                   'gsc_queries': [{'query': 'query ' + letter}]} for letter in 'abcd']
    plan = updater.weekly_plan(candidates, cogny_strategy=doc)
    assert [r['route'] for r in plan['selected']] == ['/guides/d/', '/guides/a/', '/guides/b/']
    assert plan['tier_capacity']['P2'] == {'required': 3, 'eligible': 4, 'selected': 3, 'deficit': 0}
    assert plan['selected'][0]['cogny_strategy_preference']['publication_authorized'] is False
    assert all(r['route'] != '/guides/not-admitted/' for r in plan['selected'])
    stale_doc, stale_meta = load_strategy(pointer, as_of=dt.date(2026, 10, 14))
    assert stale_doc is None and stale_meta['applied'] is False
    assert [r['route'] for r in updater.weekly_plan(candidates, cogny_strategy=stale_doc)['selected']] == ['/guides/a/', '/guides/b/', '/guides/c/']

    # Exercise the real selector boundary with unrelated observability/decision
    # I/O isolated. Its receipt must bind the same updates SHA used above.
    monkeypatch.setattr(updater, 'consumer_policy', lambda *a, **kw: {})
    monkeypatch.setattr(updater, 'require_allowed', lambda *a: None)
    monkeypatch.setattr(updater, 'load_observability', lambda *a, **kw: ({}, {'window_end': '2026-10-06'}))
    monkeypatch.setattr(updater, 'load_release_articles', lambda *a: ([], {}))
    monkeypatch.setattr(updater, 'apply_keyword_gap_class_a', lambda metrics, **kw: (metrics, {}))
    monkeypatch.setattr(updater, 'select_update_candidates', lambda *a, **kw: {'r2_candidates': candidates, 'p5_net_new_spokes': []})
    monkeypatch.setattr(updater, 'select_refresh_cadence_reserve', lambda *a, **kw: [])
    monkeypatch.setattr(updater, 'write_consumer_decision', lambda **kw: {})
    result = updater.build_selection_receipt(database=tmp_path / 'db', release_root=tmp_path,
                                            as_of=dt.date(2026, 10, 9), weekly_strategy_path=pointer)
    assert result['cogny_weekly_strategy']['updates']['sha256'] == receipt['updates']['sha256']
    assert result['cogny_weekly_strategy']['selected_preference_count'] == 1
    assert result['weekly_plan']['selected'][0]['route'] == '/guides/d/'
    assert result['receipt_sha256'] == updater.payload_hash(result)
