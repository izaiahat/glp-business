import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import glp_firecrawl_jobs as jobs  # noqa: E402


class FakeClient:
    def __init__(self, remaining=50000, discovered=None, results=None):
        self.remaining = remaining
        self.discovered = discovered or []
        self.results = results or {}
        self.calls = []
        self.caller, self.run_id, self.key = 'fake', 'run', 'k'

    def request(self, endpoint, payload=None, method='POST', timeout=120):
        self.calls.append((endpoint, payload))
        if endpoint == 'team/credit-usage':
            return {'data': {'remainingCredits': self.remaining, 'planCredits': 100000}}
        if endpoint == 'map':
            return {'links': [{'url': 'https://example.com/pricing'}, {'url': 'https://track.revoffers.com/aff_c?x=1'}]}
        if endpoint == 'search/gov':
            return {'data': {'web': [{'url': 'https://fda.gov/x', 'title': 't'}]}}
        raise AssertionError(endpoint)

    def scrape(self, payload, **kw):
        self.calls.append(('scrape', payload))
        if 'alexandria' in payload:
            call = payload['alexandria'][0]
            if call['capability'] == 'find-tools':
                items = [{'provider': p, 'capability': c, 'creditsCost': cost} for p, c, cost in self.discovered]
                return {'success': True, 'data': {'alexandria': [{'data': {'items': items}}]}}
            return {'success': True, 'data': {'alexandria': [{'provider': call['provider'], 'capability': call['capability'],
                                                              'creditsCost': 5, 'data': self.results.get(call['capability'], {})}]}}
        return {'data': {'markdown': 'We prescribe semaglutide.', 'metadata': {'url': payload['url'], 'statusCode': 200, 'creditsUsed': 1}}}

    def search(self, payload, **kw):
        self.calls.append(('search', payload))
        return {'creditsUsed': 2, 'data': {s: [] for s in payload['sources']}}


def ledger(tmp_path, rows):
    p = tmp_path / 'ops.jsonl'
    p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    return p


def test_daily_cap_counts_only_this_jobs_successful_calls_today(tmp_path):
    today = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')
    p = ledger(tmp_path, [
        {'caller': 'fc_seo_signals', 'at_utc': today + 'T01:00:00', 'outcome': 'success', 'creditsUsed': 30},
        {'caller': 'fc_seo_signals_alexandria', 'at_utc': today + 'T01:00:00', 'outcome': 'success', 'creditsUsed': None},
        {'caller': 'fc_seo_signals', 'at_utc': today + 'T01:00:00', 'outcome': 'started', 'creditsUsed': None},
        {'caller': 'other', 'at_utc': today + 'T01:00:00', 'outcome': 'success', 'creditsUsed': 999},
        {'caller': 'fc_seo_signals', 'at_utc': '2000-01-01T00:00:00', 'outcome': 'success', 'creditsUsed': 999}])
    assert jobs.spent_today(jobs.CALLERS['seo_signals'], p) == 31  # unknown cost counts as 1, not 0


def test_budget_refuses_at_cap_and_at_account_floor(tmp_path):
    p = ledger(tmp_path, [])
    b = jobs.Budget('seo_signals', FakeClient(remaining=50000), cap=10, ledger=p)
    assert b.allow(10) and not b.allow(11)
    b.charge(8)
    assert not b.allow(3)
    floor = jobs.Budget('seo_signals', FakeClient(remaining=3005), cap=1000, ledger=p)
    assert floor.allow(5) and not floor.allow(6)


def test_alexandria_refuses_capabilities_discovery_did_not_return(tmp_path):
    c = FakeClient(discovered=[('fda-gov', 'records/drug_recalls', 5)])
    b = jobs.Budget('provider_research', c, cap=100, ledger=ledger(tmp_path, []))
    ax = jobs.Alexandria(c)
    assert ax.discover('fda-gov', ['records/drug_recalls', 'records/drug_labels']) == {'records/drug_recalls': 5}
    try:
        ax.call('fda-gov', 'records/drug_labels', {}, b)
        raise AssertionError('undiscovered capability ran')
    except RuntimeError as exc:
        assert str(exc) == 'alexandria_capability_not_discovered'
    ax.call('fda-gov', 'records/drug_recalls', {'query': 'x'}, b)
    assert b.run_spent == 5
    tight = jobs.Budget('provider_research', c, cap=4, ledger=ledger(tmp_path, []))
    try:
        ax.call('fda-gov', 'records/drug_recalls', {'query': 'x'}, tight)
        raise AssertionError('over-cap call ran')
    except RuntimeError as exc:
        assert str(exc) == 'budget_refused'


def test_registry_excludes_fella_and_tracking_sources(tmp_path):
    contracts = tmp_path / 'c.json'
    contracts.write_text(json.dumps({'providers': [
        {'program_id': 'a', 'display_name': 'A Health', 'official_url': 'https://a.com/', 'field_sources': {'x': ['https://a.com/terms', 'https://track.revoffers.com/aff_c?offer_id=1']}},
        {'program_id': 'f', 'display_name': 'Fella Health', 'official_url': 'https://fellahealth.com/'},
        {'program_id': 'h', 'display_name': 'H', 'official_url': 'http://insecure.example/'}]}))
    rows = jobs.registry_providers(contracts)
    assert [r['program_id'] for r in rows] == ['a']
    assert rows[0]['field_sources'] == ['https://a.com/terms']


def test_first_party_rejects_tracking_and_foreign_hosts():
    assert jobs.first_party('https://www.example.com/pricing', 'example.com')
    assert not jobs.first_party('https://example.com/?utm_source=x', 'example.com')
    assert not jobs.first_party('https://evil.com/example.com', 'example.com')
    assert not jobs.first_party('http://example.com/', 'example.com')


def test_provider_packet_is_research_only_and_skips_tracking_urls(tmp_path, monkeypatch):
    c = FakeClient(discovered=[('npiregistry-cms-hhs-gov', 'providers/search', 5),
                               ('bbb-business-profiles-ratings-complaint', 'businesses/search', 1),
                               ('fda-gov', 'records/drug_recalls', 5)])
    b = jobs.Budget('provider_research', c, cap=500, ledger=ledger(tmp_path, []))
    ax = jobs.Alexandria(c)
    for prov, cap, _ in c.discovered:
        ax.discover(prov, [cap])
    p = {'program_id': 'ex', 'display_name': 'Example Health', 'official_url': 'https://example.com/', 'host': 'example.com',
         'field_sources': ['https://example.com/terms']}
    packet = jobs.provider_packet(p, c, ax, b)
    assert packet['status'] == 'RESEARCH_ONLY_NOT_ADOPTED'
    scraped = [x[1]['url'] for x in c.calls if x[0] == 'scrape' and 'url' in x[1]]
    assert scraped and all('revoffers' not in u for u in scraped)
    assert all(x[1]['maxAge'] == 0 for x in c.calls if x[0] == 'scrape' and 'url' in x[1])


def test_job_main_refuses_when_floor_reached(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(jobs, 'FirecrawlClient', lambda *a, **k: FakeClient(remaining=3000))
    monkeypatch.setattr(jobs, 'LEDGER', tmp_path / 'none.jsonl')
    monkeypatch.setattr(jobs, 'OUT', tmp_path)
    assert jobs.main(['seo_signals']) == 0
    assert 'REFUSED_BUDGET' in capsys.readouterr().out
