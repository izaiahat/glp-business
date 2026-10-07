import importlib.util
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import firecrawl_client as fc


def test_grouped_v2_search_fixture():
    payload = {'success': True, 'id': 'search-id', 'data': {'web': [{'url': 'https://a', 'title': 'A'}, {'title': 'no URL'}], 'news': [{'url': 'https://b'}], 'images': [{'url': 'https://image'}]}}
    assert [r['url'] for r in fc.search_rows(payload)] == ['https://a', 'https://b']
    assert fc.search_rows({'data': [{'url': 'v1'}]}) == []
    assert fc.search_rows({'success': False, 'data': payload['data']}) == []


def test_concurrent_secret_free_ledger(tmp_path):
    path = tmp_path / 'ops.jsonl'
    body = {'url': 'https://example.com/?token=secret', 'query': 'person@email.test', 'headers': {'Authorization': 'Bearer secret'}, 'formats': [{'type': 'json', 'prompt': 'sensitive'}], 'alexandria': [{'options': {'email': 'person@email.test'}}]}
    def write(i):
        return fc.record_operation('fixture', '/v2/scrape', body, payload={'data': {'metadata': {'scrapeId': 'safe-id', 'creditsUsed': 5}}}, outcome='success', ledger_path=path, run_id='run', operation_id=str(i))
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(write, range(40)))
    text = path.read_text()
    rows = [json.loads(line) for line in text.splitlines()]
    assert len(rows) == 40 and len({r['operation_id'] for r in rows}) == 40
    assert all(r['creditsUsed'] == 5 and r['scrape_search_id'] == 'safe-id' for r in rows)
    assert 'secret' not in text and 'person@' not in text and 'sensitive' not in text
    assert path.stat().st_mode & 0o777 == 0o600
    assert fc.record_operation('test', '/v2/search', outcome='success', ledger_path=path)['creditsUsed'] is None


def test_no_v1_maintained_callers():
    for rel in ['scripts/affiliate_supply_discovery.py', 'scripts/glp_reddit_daily_scout.py', 'scripts/glp_price_verification.py', 'scripts/research_glp_batch2_fda_warning_provenance.py', 'scripts/g6_r1a_stage5_research_fetch.py', 'systems/seo-blog-machine/scripts/competitor_gap.py', 'tools/glp/evidence_refresh.py']:
        assert 'api.firecrawl.dev/v1' not in (ROOT / rel).read_text(), rel


def test_client_refuses_v1_and_uncapped_agent(tmp_path):
    client = fc.FirecrawlClient('test', api_key='not-used', ledger_path=tmp_path/'ledger')
    with pytest.raises(ValueError):
        client.request('https://api.firecrawl.dev/v1/search', {})
    with pytest.raises(ValueError):
        client.request('agent', {'prompt': 'test'})
    assert not (tmp_path/'ledger').exists()
