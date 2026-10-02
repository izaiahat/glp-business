"""Smoke runner permits one synthetic provider request per network and no writes."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from marketplace_adapters import _base
import affiliate_network_access as access


def test_smoke_records_only_http_counts_and_never_retries(monkeypatch,tmp_path):
    assert importlib.util.find_spec('glp_api_collectors_smoke'), 'smoke entry missing'
    import glp_api_collectors_smoke as smoke
    class SyntheticTransport:
        def request(self,*args,**kwargs):
            return _base.json_response({'data':{'items':[]},'private':'SYNTHETIC_SECRET'})
    calls=[]
    def collector(network,transport,**kwargs):
        calls.append((network,kwargs))
        if network in ('healthtrader','admitad'):
            return access.AccessResult(network,'conversions_earnings',reason='SYNTHETIC_SKIP')
        response=transport.request('GET','https://api.partnerstack.com/api/v2/rewards')
        return access.AccessResult(network,'conversions_earnings','SUPPORTED',coverage='PARTIAL',
                                  receipts=({'http_status':response.status},),reason='SYNTHETIC_BOUND')
    monkeypatch.setattr(smoke,'conversions_earnings',collector)
    output=tmp_path/'SMOKE.json'
    result=smoke.run_smoke(output,as_of=datetime(2026,10,2,tzinfo=timezone.utc),transport_factory=SyntheticTransport)
    assert result['start_inclusive']=='2026-09-21T00:00:00+00:00'
    assert result['end_exclusive']=='2026-09-28T00:00:00+00:00'
    assert len(result['networks'])==5
    assert all(kwargs['max_requests']==1 for _,kwargs in calls)
    assert result['networks']['partnerstack']['http_status']==200
    assert result['networks']['partnerstack']['row_count']==0
    assert result['networks']['admitad']['http_status'] is None
    assert 'SYNTHETIC_SECRET' not in output.read_text()
    assert json.loads(output.read_text())==result
    with pytest.raises(FileExistsError):
        smoke.run_smoke(output,transport_factory=SyntheticTransport)
    assert len(calls)==5
