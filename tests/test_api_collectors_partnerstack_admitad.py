"""Provider-free reporting contract checks; credentials are synthetic."""
import importlib
import json
from datetime import datetime, timezone

import pytest
from marketplace_adapters import _base

START = '2026-09-21T00:00:00+00:00'
END = '2026-09-28T00:00:00+00:00'
STAMP = int(datetime.fromisoformat(START).timestamp()*1000)


class Transport:
    def __init__(self, documents, status=200):
        self.documents = list(documents)
        self.calls = []
        self.status = status

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.documents, 'unexpected request'
        return _base.Response(self.status, json.dumps(self.documents.pop(0)).encode())


def module(network):
    return importlib.import_module('affiliate_reporting_' + network)


@pytest.fixture(autouse=True)
def synthetic_secrets(monkeypatch):
    monkeypatch.setattr(_base, 'secret_text', lambda *parts: 'synthetic-only-secret')


def ps_page(rows=(), more=False, **extra):
    return {'status':200, 'data':{'items':list(rows), 'has_more':more, **extra}}


def ps_row(key='r1', **extra):
    return {'key':key, 'created_at':STAMP, 'company':{'key':'co1'},
            'amount':1200, 'currency':'USD', 'status':'approved', **extra}


def ad_page(rows=(), total=0, offset=0, limit=500):
    return {'results':list(rows), '_meta':{'count':total,'offset':offset,'limit':limit}}


def ad_row(identity=1, **extra):
    return {'action_id':identity, 'advcampaign_id':6, 'status':'approved',
            'payment':'12.34', 'currency':'USD', 'action_date':'2026-09-21 04:00:00', **extra}


def test_partnerstack_distinct_resources_and_cursor():
    t = Transport([ps_page([ps_row()], True), ps_page([ps_row('r2')]),
                   ps_page([ps_row('t1')]), ps_page([ps_row('p1')])])
    got = module('partnerstack').collect(t, start=START, end=END)
    assert got.coverage == 'COMPLETE' and got.record_count == 4
    assert got.report_complete_period
    assert [r['resource'] for r in got.records] == ['rewards','rewards','transactions','payouts']
    assert len({r['amount_semantics'] for r in got.records}) == 3
    assert got.cash_minor is None and got.commission_minor is None
    assert all(r['classification'] == 'UNKNOWN' for r in got.records)
    assert t.calls[1][2]['params']['starting_after'] == 'r1'
    assert t.calls[0][2]['params']['min_created'] == STAMP
    assert t.calls[0][2]['params']['max_created'] == int(datetime.fromisoformat(END).timestamp()*1000)-1
    assert t.calls[0][2]['params']['limit'] == 250
    assert 'synthetic-only-secret' not in json.dumps(got.to_dict())


@pytest.mark.parametrize('start,end', [(START,END),('2026-09-01T00:00:00Z','2026-10-01T00:00:00Z'),('2026-07-01T00:00:00Z','2026-09-29T00:00:00Z')])
def test_partnerstack_empty_windows_without_count_are_unproven(start,end):
    got = module('partnerstack').collect(Transport([ps_page(),ps_page(),ps_page()]),start=start,end=end)
    assert got.coverage == 'PARTIAL' and got.record_count is None
    assert got.commission_minor is None and got.cash_minor is None


@pytest.mark.parametrize('page', [ {'data':{'items':[]}}, ps_page([], total=1),
                                  ps_page([ps_row(),ps_row()]), ps_page([], True),
                                  ps_page([ps_row(created_at=0)]) ])
def test_partnerstack_incomplete_is_not_zero(page):
    got = module('partnerstack').collect(Transport([page]),start=START,end=END)
    assert got.coverage == 'PARTIAL' and got.record_count is None


@pytest.mark.parametrize('network', ['partnerstack','admitad'])
@pytest.mark.parametrize('status,document', [(403,{}),(429,{}),(200,{'error':'invalid_token'}),(200,{'status':400,'message':'invalid'})])
def test_explicit_failures_unknown(network,status,document):
    t = Transport([document],status)
    got = module(network).collect(t,start=START,end=END)
    assert got.coverage == 'UNKNOWN' and got.record_count is None
    assert len(t.calls) == 1


@pytest.mark.parametrize('network', ['partnerstack','admitad'])
def test_budget_stops_without_retry(network):
    page = ps_page([ps_row()],True) if network=='partnerstack' else ad_page([ad_row()],2)
    t=Transport([page])
    got=module(network).collect(t,start=START,end=END,max_requests=1)
    assert got.coverage=='PARTIAL' and got.record_count is None
    assert len(t.calls)==1


def test_admitad_missing_token_never_mints(monkeypatch):
    def missing(*parts):
        raise FileNotFoundError('synthetic missing token')
    monkeypatch.setattr(_base,'secret_text',missing)
    t=Transport([])
    got=module('admitad').collect(t,start=START,end=END)
    assert got.coverage=='UNKNOWN' and got.record_count is None and not t.calls
    assert got.reason=='CACHED_ACCESS_TOKEN_MISSING_NO_TOKEN_MINT'


def test_admitad_offsets_money_and_timezone_uncertainty():
    t=Transport([ad_page([ad_row()],2),ad_page([ad_row(2,currency='EUR')],2,1)])
    got=module('admitad').collect(t,start=START,end=END)
    assert got.coverage=='PARTIAL' and got.record_count is None
    assert not got.report_complete_period
    assert len(got.records)==2
    assert got.records[0]['reported_amount']=='12.34'
    assert all(r['classification']=='UNKNOWN' for r in got.records)
    assert got.cash_minor is None and got.commission_minor is None
    assert t.calls[1][2]['params']['offset']==1
    assert t.calls[0][2]['params']['date_end']=='27.09.2026'
    assert t.calls[0][2]['params']['limit']==500
    assert 'synthetic-only-secret' not in json.dumps(got.to_dict())


@pytest.mark.parametrize('page',[{'results':[]},ad_page([],1),ad_page([],0,1),ad_page([ad_row(),ad_row()],2)])
def test_admitad_unproven_empty_and_counts_partial(page):
    got=module('admitad').collect(Transport([page]),start=START,end=END)
    assert got.coverage=='PARTIAL' and got.record_count is None


@pytest.mark.parametrize('network',['partnerstack','admitad'])
def test_fella_and_sensitive_fields_are_not_projected(network):
    if network=='partnerstack':
        pages=[ps_page([ps_row(company={'key':'co1','name':'Fella Health'})]),ps_page(),ps_page()]
    else:
        pages=[ad_page([ad_row(advcampaign_name='Fella Health',order_id='private')],1)]
    got=module(network).collect(Transport(pages),start=START,end=END)
    assert not got.records
    assert 'private' not in json.dumps(got.to_dict())


def test_admitad_intraday_window_rejected_before_request():
    t=Transport([])
    got=module('admitad').collect(t,start='2026-09-21T01:00:00Z',end=END)
    assert got.coverage=='UNKNOWN' and not t.calls


@pytest.mark.parametrize('network',['partnerstack','admitad'])
def test_expired_deadline_makes_no_provider_request(network):
    import time
    t=Transport([])
    got=module(network).collect(t,start=START,end=END,deadline=time.monotonic()-1)
    assert got.coverage=='UNKNOWN' and got.record_count is None and not t.calls


def test_partnerstack_nonfinite_money_and_payout_units_unproven():
    t=Transport([ps_page([ps_row(amount='NaN')]),ps_page(),ps_page([ps_row('p1')])])
    got=module('partnerstack').collect(t,start=START,end=END)
    assert got.records[0]['reported_amount'] is None
    assert got.records[1]['amount_unit']=='UNKNOWN'


def test_partnerstack_timezone_offsets_normalized_to_utc():
    t=Transport([ps_page(total=0),ps_page(total=0),ps_page(total=0)])
    got=module('partnerstack').collect(t,start='2026-09-21T02:00:00+02:00',end='2026-09-28T02:00:00+02:00')
    assert got.coverage=='COMPLETE'
    assert t.calls[0][2]['params']['min_created']==STAMP


def test_admitad_changed_count_cannot_establish_complete():
    t=Transport([ad_page([ad_row()],3),ad_page([ad_row(2)],2,1)])
    got=module('admitad').collect(t,start=START,end=END)
    assert got.coverage=='PARTIAL' and got.record_count is None
    assert got.reason!='ROWS_RECONCILED_VENDOR_DATE_TIMEZONE_UNPROVEN_TEST_REAL_CASH_UNKNOWN'


@pytest.mark.parametrize('network',['partnerstack','admitad'])
@pytest.mark.parametrize('token',['','   '])
def test_blank_tokens_do_not_reach_provider(network,token,monkeypatch):
    monkeypatch.setattr(_base,'secret_text',lambda *parts:token)
    t=Transport([])
    got=module(network).collect(t,start=START,end=END)
    assert got.coverage=='UNKNOWN' and not t.calls


@pytest.mark.parametrize('network',['partnerstack','admitad'])
def test_redirect_http_failure_is_unknown(network):
    t=Transport([{}],status=302)
    got=module(network).collect(t,start=START,end=END)
    assert got.coverage=='UNKNOWN' and len(t.calls)==1


@pytest.mark.parametrize('stamp',['2026-09-20 23:59:59','2026-09-28 00:00:00',None,'nonsense'])
def test_admitad_outside_vendor_date_population_is_not_retained(stamp):
    got=module('admitad').collect(Transport([ad_page([ad_row(action_date=stamp)],1)]),start=START,end=END)
    assert got.coverage=='PARTIAL' and not got.records


def test_admitad_preserves_vendor_action_date_without_timezone_invention():
    got=module('admitad').collect(Transport([ad_page([ad_row()],1)]),start=START,end=END)
    assert got.records[0]['source_action_date']=='2026-09-21 04:00:00'
    assert got.records[0]['source_timezone']=='UNKNOWN'


def test_partnerstack_reconciled_explicit_zero_all_resources():
    got=module('partnerstack').collect(Transport([ps_page(total=0),ps_page(total=0),ps_page(total=0)]),start=START,end=END)
    assert got.coverage=='COMPLETE' and got.record_count==0
    assert got.cash_minor is None and got.commission_minor is None


def test_partnerstack_unproven_empty_resource_preserves_other_rows():
    got=module('partnerstack').collect(Transport([ps_page([ps_row()]),ps_page(),ps_page(total=0)]),start=START,end=END)
    assert got.coverage=='PARTIAL' and got.record_count is None
    assert len(got.records)==1
