"""Synthetic Everflow partner fixtures only; never read a credential or call a provider."""
import importlib
import importlib.util
import json
from datetime import datetime, timezone

import pytest

import affiliate_network_access as access
from marketplace_adapters import _base

START = '2026-09-21T00:00:00+00:00'
END = '2026-09-28T00:00:00+00:00'
NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
ENDPOINT = 'https://api.eflow.team/v1/affiliates/reporting/conversions'
TENANTS = ('everflow_orderly', 'everflow_ro', 'everflow_eden', 'everflow_reverse_health')
KEYS = {tenant: 'synthetic-key-' + tenant for tenant in TENANTS}


class Transport:
    def __init__(self, documents):
        self.documents = list(documents)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.documents, 'unexpected request'
        item = self.documents.pop(0)
        if isinstance(item, _base.Response):
            return item
        return _base.Response(200, json.dumps(item).encode(), {})


def collector():
    assert importlib.util.find_spec('affiliate_reporting_everflow'), 'Everflow collector missing'
    return importlib.import_module('affiliate_reporting_everflow')


@pytest.fixture(autouse=True)
def synthetic_keys(monkeypatch):
    def lookup(*parts):
        name = parts[-1]
        for tenant, key in KEYS.items():
            if name == tenant + '_affiliate_api_key':
                return key
        raise FileNotFoundError('secret-path ' + name)
    monkeypatch.setattr(_base, 'secret_text', lookup)


def row(identity, **extra):
    return {'conversion_id': identity, 'conversion_unix_timestamp': 1790000000,
            'status': 'approved', 'payout': 12.5, 'is_event': False, 'event': 'Base',
            'currency_id': 'USD', 'relationship': {'offer': {'network_offer_id': 7}}, **extra}


def page(rows, total, number=1, size=50):
    return {'conversions': list(rows), 'paging': {'page': number, 'page_size': size, 'total_count': total}}


def run(tenant='everflow_orderly', documents=(), **kwargs):
    module = collector()
    transport = Transport(documents)
    kwargs.setdefault('now', NOW)
    result = module.collect(transport, tenant=tenant, start=START, end=END, **kwargs)
    return result, transport


def test_multi_page_complete_only_when_rows_equal_total_count():
    first = [row('c%d' % i) for i in range(50)]
    result, transport = run(documents=[page(first, 52), page([row('c50'), row('c51')], 52, 2)])
    assert result.network == 'everflow_orderly'
    assert result.status == 'SUPPORTED' and result.coverage == 'COMPLETE'
    assert result.record_count == 52 and len(result.records) == 52
    assert result.report_complete_period
    assert result.commission_minor is None and result.cash_minor is None
    assert [c[2]['params'] for c in transport.calls] == [{'page': 1, 'page_size': 50}, {'page': 2, 'page_size': 50}]
    method, url, kwargs = transport.calls[0]
    assert method == 'POST' and url == ENDPOINT
    assert kwargs['headers']['X-Eflow-API-Key'] == KEYS['everflow_orderly']
    body = kwargs['json_body']
    assert body['from'] == '2026-09-21' and body['to'] == '2026-09-27'
    assert body['show_conversions'] is True and body['show_events'] is True
    assert isinstance(body['timezone_id'], int)
    assert 'synthetic-key' not in json.dumps(result.to_dict())


def test_total_count_mismatch_is_partial_without_numeric_count():
    result, _ = run(documents=[page([row('c1'), row('c2')], 3), page([], 3, 2)])
    assert result.coverage == 'PARTIAL'
    assert result.record_count is None and not result.report_complete_period
    assert len(result.records) == 2


def test_rows_beyond_total_count_are_partial():
    result, _ = run(documents=[page([row('c1'), row('c2')], 1)])
    assert result.coverage == 'PARTIAL' and result.record_count is None


def test_total_count_changing_between_pages_is_partial():
    first = [row('c%d' % i) for i in range(50)]
    result, _ = run(documents=[page(first, 51), page([row('c50')], 52, 2)])
    assert result.coverage == 'PARTIAL' and result.record_count is None


def test_duplicate_conversion_across_pages_is_partial():
    first = [row('c%d' % i) for i in range(50)]
    result, _ = run(documents=[page(first, 51), page([row('c0')], 51, 2)])
    assert result.coverage == 'PARTIAL' and result.record_count is None


def test_missing_key_is_unknown_credential_missing_without_request(monkeypatch):
    monkeypatch.delitem(KEYS, 'everflow_eden')
    module = collector()
    transport = Transport([])
    result = module.collect(transport, tenant='everflow_eden', start=START, end=END, now=NOW)
    assert result.network == 'everflow_eden'
    assert result.coverage == 'UNKNOWN' and result.record_count is None
    assert result.reason == 'UNKNOWN_CREDENTIAL_MISSING'
    assert transport.calls == [] and result.receipts == ()
    assert 'secret-path' not in json.dumps(result.to_dict())


def test_missing_key_for_one_tenant_does_not_affect_another(monkeypatch):
    def only_ro(*parts):
        if parts[-1] == 'everflow_ro_affiliate_api_key':
            return KEYS['everflow_ro']
        raise FileNotFoundError('absent')
    monkeypatch.setattr(_base, 'secret_text', only_ro)
    missing, t1 = run('everflow_orderly')
    present, t2 = run('everflow_ro', [page([row('r1')], 1)])
    assert missing.reason == 'UNKNOWN_CREDENTIAL_MISSING' and t1.calls == []
    assert present.coverage == 'COMPLETE' and present.record_count == 1


def test_tenants_are_isolated_by_identity_credential_and_completeness():
    results = {}
    for tenant in TENANTS:
        docs = [page([row(tenant + '-1')], 1)] if tenant != 'everflow_eden' else [page([row('e1')], 2), page([], 2, 2)]
        result, transport = run(tenant, docs)
        results[tenant] = result
        assert all(c[2]['headers']['X-Eflow-API-Key'] == KEYS[tenant] for c in transport.calls)
        assert result.network == tenant
        assert tenant in result.account_binding
        assert all(r['tenant'] == tenant for r in result.records)
        text = json.dumps(result.to_dict())
        assert not any(other in text for other in TENANTS if other != tenant and other not in tenant)
    assert results['everflow_eden'].coverage == 'PARTIAL'
    assert all(results[t].coverage == 'COMPLETE' for t in TENANTS if t != 'everflow_eden')


def test_unknown_tenant_is_refused_without_request():
    result, transport = run('everflow', [])
    assert result.coverage == 'UNKNOWN' and transport.calls == []


def test_event_types_stay_distinct_and_zero_payout_is_not_a_paid_customer():
    rows = [row('c1'), row('c2', is_event=True, event='Add To Cart', payout=0),
            row('c3', is_event=True, event='Purchase', payout=0)]
    result, _ = run(documents=[page(rows, 3)])
    base, cart, purchase = result.records
    assert base['record_type'] == 'base_conversion' and base['event_name'] is None
    assert cart['record_type'] == 'event' and cart['event_name'] == 'Add To Cart'
    assert purchase['event_name'] == 'Purchase'
    assert cart['paid_customer'] == purchase['paid_customer'] == 'UNKNOWN'
    assert cart['payout_semantics'] == 'zero_payout_action_not_paid_customer'
    assert base['payout_semantics'] == 'reported_payout_not_cash'
    assert all('sale' not in json.dumps(r).casefold() for r in result.records)


def test_test_real_and_currency_unknown_unless_response_states_them():
    rows = [row('c1', currency_id=None), row('c2', is_test=True), row('c3', is_test=False),
            row('c4', currency_id='usd')]
    result, _ = run(documents=[page(rows, 4)])
    assert [r['currency'] for r in result.records] == [None, 'USD', 'USD', None]
    assert [r['classification'] for r in result.records] == ['UNKNOWN', 'TEST', 'REAL', 'UNKNOWN']
    assert all(r['attribution'] == 'UNKNOWN' for r in result.records)


def test_empty_population_is_never_zero_revenue():
    result, _ = run(documents=[page([], 0)])
    assert result.coverage == 'COMPLETE' and result.record_count == 0
    assert result.commission_minor is None and result.cash_minor is None
    assert 'REVENUE_NOT_ZERO' in result.reason


def test_offer_terms_and_sensitive_fields_are_not_projected():
    sensitive = row('c1', revenue=99, sub1='private@example.invalid', email='private@example.invalid',
                    relationship={'offer': {'network_offer_id': 7, 'payout_amount': 40, 'name': 'Private'}})
    result, _ = run(documents=[page([sensitive], 1)])
    text = json.dumps(result.to_dict())
    assert 'private@example.invalid' not in text and '99' not in text and '40' not in text
    assert result.records[0]['program_id'] == '7'
    assert result.records[0]['reported_payout'] == '12.5'


@pytest.mark.parametrize('document', [
    {'conversions': None, 'paging': {'total_count': 1}},
    {'conversions': [], 'paging': {}},
    {'conversions': [{'conversion_id': None}], 'paging': {'total_count': 1}},
    {'errors': ['synthetic-key-everflow_orderly']},
    _base.Response(401, b'synthetic-key-everflow_orderly', {}),
])
def test_http_provider_or_schema_failure_is_unknown(document):
    result, _ = run(documents=[document])
    assert result.coverage == 'UNKNOWN' and result.record_count is None and result.records == ()
    assert 'synthetic-key' not in json.dumps(result.to_dict())


def test_request_budget_after_validated_page_is_partial():
    first = [row('c%d' % i) for i in range(50)]
    result, transport = run(documents=[page(first, 60)], max_requests=1)
    assert result.coverage == 'PARTIAL' and result.record_count is None
    assert len(result.records) == 50 and len(transport.calls) == 1


@pytest.mark.parametrize('start,end', [
    ('2025-09-01T00:00:00+00:00', '2025-09-08T00:00:00+00:00'),
    ('2026-09-21T06:00:00+00:00', END),
    (END, START),
])
def test_windows_outside_365_days_or_not_whole_utc_days_perform_no_request(start, end):
    module = collector()
    transport = Transport([])
    result = module.collect(transport, tenant='everflow_ro', start=start, end=end, now=NOW)
    assert result.coverage == 'UNKNOWN' and transport.calls == []


def test_dispatch_and_weekly_partitions_per_tenant(monkeypatch, tmp_path):
    assert access.EVERFLOW_TENANTS == TENANTS
    module = collector()
    seen = []
    def synthetic(transport, *, tenant, **kwargs):
        seen.append(tenant)
        return access.AccessResult(tenant, 'conversions_earnings', reason='SYNTHETIC')
    monkeypatch.setattr(module, 'collect', synthetic)
    for tenant in TENANTS:
        assert access.conversions_earnings(tenant, object(), start=START, end=END).network == tenant
    assert seen == list(TENANTS)
    from glp_affiliate_weekly_reporting import run as weekly
    roster = {'marketplace_count': 4, 'marketplaces': [{'marketplace': t} for t in TENANTS]}
    result = weekly(tmp_path / 'report', roster, [], as_of=NOW,
                    collectors=access.weekly_collectors(TENANTS, object()))
    assert len(result['observations']) == 12
    assert {r['network'] for r in result['observations']} == set(TENANTS)
    assert all(r['reporting_timezone'] == access.REPORTING_TIMEZONES['everflow'] for r in result['observations'])


def test_everflow_route_is_read_allowlisted_and_rejects_query_strings():
    transport = Transport([page([], 0)])
    wrapped = access.ReadTransport(transport)
    assert wrapped.request('POST', ENDPOINT, json_body={'from': '2026-09-21'}, params={'page': 1}).status == 200
    with pytest.raises(ValueError, match='route_not_read_allowlisted'):
        wrapped.request('POST', ENDPOINT + '?page=1', json_body={})
    with pytest.raises(ValueError, match='route_not_read_allowlisted'):
        wrapped.request('GET', ENDPOINT)
