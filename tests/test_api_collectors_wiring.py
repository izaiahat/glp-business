"""Synthetic exact read-route and scheduled reporting integration checks."""
import json
import time
from datetime import datetime, timezone
import pytest
import affiliate_network_access as access
from marketplace_adapters._base import RecordingTransport, json_response

NEW_NETWORKS = ('partnerstack', 'admitad', 'clickbank', 'refersion', 'healthtrader')

@pytest.mark.parametrize('url', [
    'https://api.partnerstack.com/api/v2/rewards',
    'https://api.partnerstack.com/api/v2/transactions',
    'https://api.partnerstack.com/api/v2/payouts',
    'https://api.admitad.com/statistics/actions/',
    'https://api.clickbank.com/rest/1.3/quickstats/list',
])
def test_new_read_routes_reach_injected_transport(url):
    transport = RecordingTransport([('GET', url, json_response({'synthetic': True}))])
    wrapped = access.ReadTransport(transport)
    assert wrapped.request('GET', url).status == 200
    assert len(transport.calls) == 1
    assert wrapped.receipts[0]['endpoint'] == url

@pytest.mark.parametrize('network', NEW_NETWORKS)
def test_new_networks_dispatch_through_access_contract(monkeypatch, network):
    import sys
    import types
    module = types.ModuleType('affiliate_reporting_' + network)
    expected = access.AccessResult(network, 'conversions_earnings', reason='SYNTHETIC_COLLECTOR_REACHED')
    calls = []
    def collect(transport, **kwargs):
        calls.append(kwargs)
        return expected
    module.collect = collect
    monkeypatch.setitem(sys.modules, module.__name__, module)
    got = access.conversions_earnings(network, object(), start='2026-09-21T00:00:00+00:00',
                                    end='2026-09-28T00:00:00+00:00')
    assert got is expected
    assert calls[0]['start'] == '2026-09-21T00:00:00+00:00'

def test_weekly_periods_persist_new_collector_unknowns(monkeypatch,tmp_path):
    from glp_affiliate_weekly_reporting import run
    calls=[]
    def synthetic(network,transport,**kwargs):
        calls.append((network,kwargs['start'],kwargs['end']))
        return access.AccessResult(network,'conversions_earnings',reason='SYNTHETIC_ENDPOINT_UNAVAILABLE')
    monkeypatch.setattr(access,'conversions_earnings',synthetic)
    roster={'marketplace_count':len(NEW_NETWORKS), 'marketplaces':[{'marketplace':n} for n in NEW_NETWORKS]}
    result=run(tmp_path/'report',roster,[],as_of=datetime(2026,10,2,tzinfo=timezone.utc),
        collectors=access.weekly_collectors(NEW_NETWORKS,object()))
    assert len(result['observations']) == 15
    assert {row['period_purpose'] for row in result['observations']} == {'closed_week','lookback','closed_month'}
    assert all(row['coverage']=='UNKNOWN' and row['record_count'] is None for row in result['observations'])
    assert all(row['reporting_timezone'] in ('UTC','UNKNOWN','UNKNOWN_VENDOR_CALENDAR') for row in result['observations'])
    persisted=json.loads((tmp_path/'report'/'RESULT.json').read_text())
    assert persisted['fleet_commission_minor'] is None
    assert len(list((tmp_path/'report').glob('*-page-*.json'))) == 15
