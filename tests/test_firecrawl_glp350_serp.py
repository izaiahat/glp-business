from test_firecrawl_integration import ROOT, fc
import importlib.util
import json
from pathlib import Path

def test_page_two_us_weekly_top_three():
    assert fc._options({'location': 'United States', 'tbs': 'qdr:w'})['location'] == 'United States'
    assert fc._options({'location': 'United States', 'tbs': 'qdr:w'})['tbs'] == 'qdr:w'
    spec = importlib.util.spec_from_file_location('serp', ROOT / 'systems/seo-blog-machine/scripts/refresh_topic_supply_firecrawl.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert module.MAX_TARGETS == 40 and module.RESULT_LIMIT == 3 and module.REFRESH_AFTER_HOURS == 168
    _, rows = module.normalized_results({'success': True, 'data': {'web': [{'url': 'https://example.org', 'markdown': 'Captured source'}]}})
    assert rows[0]['markdown'] == 'Captured source'
    assert '"location": "United States"' in Path(module.__file__).read_text()


def test_manual_force_refreshes_fresh_target_set(monkeypatch, tmp_path):
    import sys
    spec = importlib.util.spec_from_file_location('serp_force', ROOT / 'systems/seo-blog-machine/scripts/refresh_topic_supply_firecrawl.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    feed, output, report = [tmp_path / name for name in ('feed.json', 'output.json', 'report.json')]
    feed.write_text(json.dumps({'research_backlog': [{'reason': 'insufficient_measured_competitor_evidence', 'target_query': 'fixture query', 'target_route': '/guides/fixture/'}]}))
    calls = []
    monkeypatch.setattr(module, 'api_key', lambda: None)
    def search(key, query):
        calls.append(query)
        return None, [{'url': 'https://example.org', 'markdown': 'Fixture capture'}]
    monkeypatch.setattr(module, 'firecrawl_search', search)
    command = ['serp', '--feed', str(feed), '--output', str(output), '--page-two-report', str(report)]
    monkeypatch.setattr(sys, 'argv', command)
    assert module.main() == 0
    assert module.main() == 0 and len(calls) == 1
    monkeypatch.setattr(sys, 'argv', command + ['--force'])
    assert module.main() == 0 and len(calls) == 2
    assert json.loads(report.read_text())['provider_calls'] == 1
