import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


def test_daily_calls_fresh_discovery_and_prioritizes_new_hosts_without_new_admission(monkeypatch, tmp_path):
    import glp_sponsor_firecrawl_supply as supply
    import glp_sponsor_campaign as campaign
    import glp328_outreach_policy as policy
    p = supply.Producer(state_dir=tmp_path / 'state', import_only=True)
    p.import_only = False  # Exercise daily integration, all effects intercepted.
    monkeypatch.setattr(campaign, 'ROOT', tmp_path)
    monkeypatch.setattr(campaign, 'read_values', lambda: [[]])
    monkeypatch.setattr(campaign, 'load', lambda path: {'organizations': {}})
    monkeypatch.setattr(policy, 'classify_row', lambda i, r: r)
    monkeypatch.setattr(supply, 'SPONSOR_SEEDS', ['old.example'])
    p.plan = lambda: ({'suppression': {'complete': True, 'entries': [{'domain': 'held.example'}]}, 'conversations': []}, {'exclusions': []})
    p.budget = supply.Budget(10000)
    calls = []
    def discover(**kwargs):
        calls.append('discover')
        return {'fresh.example': {'source': 'wikidata_P856', 'url': 'https://fresh.example/'},
                'held.example': {'source': 'wikidata_P856', 'url': 'https://held.example/'},
                'fellahealth.com': {'source': 'wikidata_P856', 'url': 'https://fellahealth.com/'}}
    p.discover = discover
    p.capture = lambda hosts: calls.append(hosts)
    p.merge = lambda: calls.append('merge')
    p.apollo = lambda: calls.append('apollo')
    p.verify = lambda: calls.append('verify')
    p.daily()
    assert calls == ['discover', ['fresh.example', 'old.example'], 'merge', 'apollo', 'merge', 'verify']
    assert p.report['target_universe']['untouched_discovery_hosts'] == 1


def test_paid_search_is_fenced_by_existing_reservation_before_transport(tmp_path):
    import glp_sponsor_firecrawl_supply as supply
    p = supply.Producer(state_dir=tmp_path, import_only=True)
    p.import_only = False
    p.reserve = lambda n: (_ for _ in ()).throw(RuntimeError('sponsor_ledger_credit_cost_unknown'))
    class Client:
        def search(self, *a, **kw):
            pytest.fail('search bypassed shared sponsor reservation')
    p.client = Client()
    p.discover(directory_batches=0)
    assert p.report['discovery']['search_status'] == 'sponsor_ledger_credit_cost_unknown'
    assert p.report['discovery']['query_index'] == 0
