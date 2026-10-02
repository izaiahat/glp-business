"""Synthetic vendor-calendar summaries never become UTC revenue or cash."""
from importlib import import_module
from datetime import datetime, timedelta, timezone
import pytest
import affiliate_network_access as access
from marketplace_adapters import _base as base
from marketplace_adapters._base import json_response

START='2026-09-21T00:00:00+00:00'
END='2026-09-28T00:00:00+00:00'

class Sequence:
    def __init__(self, *responses):
        self.responses=list(responses)
        self.calls=[]
    def request(self, method, url, **kwargs):
        self.calls.append((method,url,kwargs))
        return self.responses.pop(0)

def summary(day):
    return {'accountData': [{'nickName':'synthetic', 'quickStats':[
        {'quickStatDate':day, 'sale':'12.00','refund':'0.00','chargeback':'0.00'}]}]}

def test_clickbank_daily_summary_preserves_unknown_timezone_and_money(monkeypatch):
    monkeypatch.setattr(base, 'secret_text', lambda *p: 'SYNTHETIC_TOKEN')
    transport=Sequence(json_response(summary('2026-09-21T00:00:00Z')))
    result=access.conversions_earnings('clickbank',transport,start=START,end=END)
    assert result.coverage == 'PARTIAL'
    assert len(result.records) == 1
    assert result.records[0]['reported_sale'] == '12.00'
    assert result.records[0]['currency'] is None
    assert result.records[0]['reporting_timezone'] == 'UNKNOWN_VENDOR_CALENDAR'
    assert result.records[0]['classification'] == 'UNKNOWN'
    assert result.record_count is result.commission_minor is result.cash_minor is None
    assert not result.report_complete_period
    params=transport.calls[0][2]['params']
    assert params == {'startDate':'2026-09-21','endDate':'2026-09-27'}
    assert 'SYNTHETIC_TOKEN' not in str(result.to_dict())

def test_clickbank_206_pages_until_200_without_changing_dates(monkeypatch):
    monkeypatch.setattr(base,'secret_text',lambda *p:'SYNTHETIC_TOKEN')
    transport=Sequence(json_response(summary('2026-09-21T00:00:00Z'),206),
                       json_response(summary('2026-09-22T00:00:00Z'),200))
    result=access.conversions_earnings('clickbank',transport,start=START,end=END)
    assert len(transport.calls) == 2
    assert [call[2]['headers']['Page'] for call in transport.calls] == ['1','2']
    assert len(result.records) == 2
    assert len(result.receipts) == 2
    assert result.record_count is None

@pytest.mark.parametrize('payload', [{'accountData':[]}, {'accountData':[{'nickName':'synthetic','quickStats':[]}]}, {}])
def test_clickbank_empty_or_missing_is_not_zero(monkeypatch,payload):
    monkeypatch.setattr(base,'secret_text',lambda *p:'SYNTHETIC_TOKEN')
    result=access.conversions_earnings('clickbank',Sequence(json_response(payload)),start=START,end=END)
    assert result.coverage in ('PARTIAL','UNKNOWN')
    assert result.record_count is result.commission_minor is result.cash_minor is None
    assert not result.report_complete_period

@pytest.mark.parametrize('status', [401,403,429,500])
def test_clickbank_http_failure_is_unknown_and_never_retried(monkeypatch,status):
    monkeypatch.setattr(base,'secret_text',lambda *p:'SYNTHETIC_TOKEN')
    transport=Sequence(json_response({'error':'SYNTHETIC_TOKEN'},status))
    result=access.conversions_earnings('clickbank',transport,start=START,end=END)
    assert result.coverage == 'UNKNOWN'
    assert len(transport.calls) == 1
    assert 'SYNTHETIC_TOKEN' not in str(result.to_dict())

@pytest.mark.parametrize('start,end', [(START,END),('2026-08-01T00:00:00+00:00','2026-09-01T00:00:00+00:00'),('2026-06-30T00:00:00+00:00',END)])
def test_clickbank_explicit_week_month_lookback_dates(monkeypatch,start,end):
    monkeypatch.setattr(base,'secret_text',lambda *p:'SYNTHETIC_TOKEN')
    transport=Sequence(json_response({'accountData':[]}))
    result=access.conversions_earnings('clickbank',transport,start=start,end=end)
    assert transport.calls[0][2]['params'] == {'startDate':start[:10],
        'endDate':(datetime.fromisoformat(end)-timedelta(days=1)).date().isoformat()}
    assert result.record_count is None

def test_clickbank_duplicate_and_outside_window_remain_partial(monkeypatch):
    monkeypatch.setattr(base,'secret_text',lambda *p:'SYNTHETIC_TOKEN')
    for responses in [(json_response(summary('2026-09-28T00:00:00Z')),),
                      (json_response(summary('2026-09-21T00:00:00Z'),206),json_response(summary('2026-09-21T00:00:00Z')))]:
        result=access.conversions_earnings('clickbank',Sequence(*responses),start=START,end=END)
        assert result.coverage == 'PARTIAL'
        assert result.record_count is None

def test_healthtrader_missing_cached_token_never_calls_api(monkeypatch):
    def absent(*args,**kwargs):
        raise FileNotFoundError('synthetic missing bearer')
    monkeypatch.setattr(base,'secret_json',absent)
    transport=Sequence()
    result=access.conversions_earnings('healthtrader',transport,start=START,end=END)
    assert result.coverage == 'UNKNOWN'
    assert result.status == 'SUPPORTED'
    assert result.reason == 'CACHED_ACCESS_TOKEN_MISSING_NO_TOKEN_MINT'
    assert result.record_count is result.cash_minor is None
    assert not transport.calls

def test_clickbank_http200_error_is_unknown(monkeypatch):
    monkeypatch.setattr(base,'secret_text',lambda *p:'SYNTHETIC_TOKEN')
    result=access.conversions_earnings('clickbank',Sequence(json_response({'errors':['denied']})),start=START,end=END)
    assert result.coverage == 'UNKNOWN'
    assert result.record_count is None

def test_clickbank_keeps_distinct_response_owned_accounts(monkeypatch):
    monkeypatch.setattr(base,'secret_text',lambda *p:'SYNTHETIC_TOKEN')
    doc=summary('2026-09-21T00:00:00Z')
    doc['accountData'].append({'nickName':'fixture2','quickStats':doc['accountData'][0]['quickStats']})
    result=access.conversions_earnings('clickbank',Sequence(json_response(doc)),start=START,end=END)
    assert {r.get('account_id') for r in result.records} == {'synthetic','fixture2'}

def test_clickbank_unrecognized_http200_schema_is_unknown(monkeypatch):
    monkeypatch.setattr(base,'secret_text',lambda *p:'SYNTHETIC_TOKEN')
    result=access.conversions_earnings('clickbank',Sequence(json_response({'unrecognized':[]})),start=START,end=END)
    assert result.coverage == 'UNKNOWN'
    assert result.record_count is None
