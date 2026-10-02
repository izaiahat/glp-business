"""Synthetic Refersion fixtures only; never read a credential or call a provider."""
import importlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from marketplace_adapters import _base

START = '2026-09-01T00:00:00+00:00'
END = '2026-10-01T00:00:00+00:00'
STAMP = '1788220800'


class SyntheticTransport:
    def __init__(self, documents):
        self.documents = list(documents)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.documents.pop(0)
        if isinstance(item, _base.Response):
            return item
        return _base.Response(200, json.dumps(item).encode(), {})


def collector(monkeypatch):
    assert importlib.util.find_spec('affiliate_reporting_refersion'), 'Refersion collector missing'
    module = importlib.import_module('affiliate_reporting_refersion')
    monkeypatch.setattr(_base, 'secret_text', lambda *args: 'synthetic-token')
    return module


def conversion(identity=1, **kwargs):
    return dict(id=identity, created=STAMP, status='APPROVED', commission_total='4.80',
                currency='USD', offer={'id': 33}, affiliate={'id': 44},
                shop={'name': 'Held Provider'}, payment=None, **kwargs)


def payment(identity=2):
    return dict(id=identity, created=STAMP, commission_total='9.40', currency='USD',
                total_conversions=2, shop={'name': 'Held Provider'})


def test_collects_distinct_conversions_and_payments_with_terminal_pages(monkeypatch):
    module = collector(monkeypatch)
    transport = SyntheticTransport([
        {'data': {'conversions': [conversion()]}}, {'data': {'conversions': []}},
        {'data': {'payments': [payment()]}}, {'data': {'payments': []}},
    ])
    result = module.collect(transport, start=START, end=END)
    assert result.status == 'SUPPORTED'
    assert result.coverage == 'COMPLETE'
    assert result.record_count == 2
    assert result.report_complete_period
    assert [r['record_type'] for r in result.records] == ['conversion', 'payment']
    assert [r['reported_amount'] for r in result.records] == ['4.80', '9.40']
    assert result.commission_minor is None and result.cash_minor is None
    assert all(r['classification'] == 'UNKNOWN' for r in result.records)
    assert result.records[0]['program_id'] == '33'
    assert result.records[1]['program_id'] is None
    assert len(result.receipts) == 4
    query = transport.calls[0][2]['json_body']['query']
    assert 'created_from:1788220799' in query and 'created_to:1790812799' in query
    assert 'first:200' in query and 'offset:0' in query
    assert 'offset:1' in transport.calls[1][2]['json_body']['query']
    assert all(method == 'POST' and url == 'https://graphql.refersion.com'
               for method, url, _ in transport.calls)
    assert transport.calls[0][2]['headers']['X-Refersion-Key'] == 'synthetic-token'
    assert 'synthetic-token' not in json.dumps(result.to_dict())


@pytest.mark.parametrize('document', [
    {'data': {'conversions': [], 'payments': []}},
    {'errors': [{'message': 'synthetic-token customer@example.invalid'}], 'data': {'conversions': []}},
    {'data': {'conversions': None}},
    {'data': {}},
    {'data': {'conversions': ['wrong']}},
    _base.Response(403, b'synthetic-token customer@example.invalid', {}),
])
def test_unproven_or_failed_reads_never_publish_numeric_counts(monkeypatch, document):
    module = collector(monkeypatch)
    transport = SyntheticTransport([document, {'data': {'payments': []}}])
    result = module.collect(transport, start=START, end=END)
    assert result.coverage in {'UNKNOWN', 'PARTIAL'}
    assert result.record_count is None
    assert result.commission_minor is None and result.cash_minor is None
    assert not result.report_complete_period
    assert 'synthetic-token' not in json.dumps(result.to_dict())
    assert 'customer@example.invalid' not in json.dumps(result.to_dict())


def test_absent_credential_returns_safe_unknown_without_request(monkeypatch):
    module = collector(monkeypatch)
    def absent(*args):
        raise OSError('secret-path synthetic-token')
    monkeypatch.setattr(_base, 'secret_text', absent)
    transport = SyntheticTransport([])
    result = module.collect(transport, start=START, end=END)
    assert result.coverage == 'UNKNOWN'
    assert result.record_count is None and transport.calls == []
    assert 'synthetic-token' not in json.dumps(result.to_dict())


@pytest.mark.parametrize('change', [
    {'id': None}, {'id': True}, {'created': None}, {'created': '1790812800'},
    {'created': '1788220799'}, {'created': 'not-a-date'},
])
def test_invalid_identity_or_outside_half_open_window_is_unknown(monkeypatch, change):
    module = collector(monkeypatch)
    row = conversion()
    row.update(change)
    transport = SyntheticTransport([
        {'data': {'conversions': [row]}}, {'data': {'conversions': []}},
        {'data': {'payments': [payment()]}}, {'data': {'payments': []}},
    ])
    result = module.collect(transport, start=START, end=END)
    assert result.coverage == 'UNKNOWN' and result.record_count is None
    assert result.records == ()


def test_repeated_page_cannot_prove_enumeration(monkeypatch):
    module = collector(monkeypatch)
    transport = SyntheticTransport([
        {'data': {'conversions': [conversion()]}}, {'data': {'conversions': [conversion()]}},
        {'data': {'conversions': []}}, {'data': {'payments': [payment()]}},
        {'data': {'payments': []}},
    ])
    result = module.collect(transport, start=START, end=END)
    assert result.coverage == 'UNKNOWN' and result.record_count is None
    assert len(transport.calls) == 2


def test_fella_and_sensitive_fields_are_not_projected(monkeypatch):
    module = collector(monkeypatch)
    row = conversion(customer_email='private@example.invalid', notes='sensitive')
    row.update(currency='bogus', commission_total='NaN', status='invented-status')
    fella = conversion(2)
    fella['shop'] = {'name': 'Fella Health'}
    transport = SyntheticTransport([
        {'data': {'conversions': [row, fella]}}, {'data': {'conversions': []}},
        {'data': {'payments': [payment()]}}, {'data': {'payments': []}},
    ])
    result = module.collect(transport, start=START, end=END)
    assert result.coverage == 'COMPLETE'
    assert len(result.records) == 2
    assert result.records[0]['currency'] is None
    assert result.records[0]['reported_amount'] is None
    assert result.records[0]['source_status'] == 'UNKNOWN'
    assert result.records[0]['provider_hold'] == 'UNCHANGED'
    text = json.dumps(result.to_dict())
    assert 'Fella' not in text and 'private@example.invalid' not in text and 'sensitive' not in text
    query = transport.calls[0][2]['json_body']['query']
    assert not any(field in query for field in ('customer', 'notes', 'order_id', 'click', 'tracking'))


@pytest.mark.parametrize('end', ['2026-09-08T00:00:00+00:00', END, '2026-11-30T00:00:00+00:00'])
def test_week_month_and_ninety_day_windows_are_exact_utc(monkeypatch, end):
    from datetime import datetime
    module = collector(monkeypatch)
    transport = SyntheticTransport([
        {'data': {'conversions': [conversion()]}}, {'data': {'conversions': []}},
        {'data': {'payments': [payment()]}}, {'data': {'payments': []}},
    ])
    result = module.collect(transport, start=START, end=end)
    assert result.coverage == 'COMPLETE'
    upper = int(datetime.fromisoformat(end).timestamp()) - 1
    for _, _, kwargs in transport.calls:
        assert 'created_from:1788220799' in kwargs['json_body']['query']
        assert f'created_to:{upper}' in kwargs['json_body']['query']
        assert kwargs['timeout'] <= 30


def test_request_budget_preserves_validated_partial_read(monkeypatch):
    module = collector(monkeypatch)
    transport = SyntheticTransport([{'data': {'conversions': [conversion()]}}])
    result = module.collect(transport, start=START, end=END, max_requests=1)
    assert result.coverage == 'PARTIAL' and result.record_count is None
    assert len(result.records) == 1
    assert result.records[0]['record_id'] == '1'
    assert result.commission_minor is None and result.cash_minor is None
    assert len(transport.calls) == 1
    assert len(result.receipts) == 1


def test_expired_deadline_performs_no_request(monkeypatch):
    module = collector(monkeypatch)
    transport = SyntheticTransport([])
    result = module.collect(transport, start=START, end=END, deadline=0)
    assert result.coverage == 'UNKNOWN' and result.record_count is None
    assert transport.calls == []


@pytest.mark.parametrize('start,end', [
    ('2026-09-01', END), (END, START),
    ('2026-09-01T00:00:00.1+00:00', END),
])
def test_invalid_windows_perform_no_request(monkeypatch, start, end):
    module = collector(monkeypatch)
    transport = SyntheticTransport([])
    result = module.collect(transport, start=start, end=end)
    assert result.coverage == 'UNKNOWN' and result.record_count is None
    assert transport.calls == []


def test_untrusted_program_identity_is_never_serialized(monkeypatch):
    module = collector(monkeypatch)
    row = conversion()
    row['offer'] = {'id': 'private@example.invalid'}
    transport = SyntheticTransport([
        {'data': {'conversions': [row]}}, {'data': {'conversions': []}},
        {'data': {'payments': [payment()]}}, {'data': {'payments': []}},
    ])
    result = module.collect(transport, start=START, end=END)
    assert result.coverage == 'UNKNOWN'
    assert 'private@example.invalid' not in json.dumps(result.to_dict())


def test_provider_page_above_requested_limit_is_unknown(monkeypatch):
    module = collector(monkeypatch)
    transport = SyntheticTransport([
        {'data': {'conversions': [conversion(i) for i in range(1, 202)]}},
        {'data': {'conversions': []}}, {'data': {'payments': [payment()]}},
        {'data': {'payments': []}},
    ])
    result = module.collect(transport, start=START, end=END)
    assert result.coverage == 'UNKNOWN' and result.record_count is None
    assert result.records == () and len(transport.calls) == 1


def test_deadline_after_validated_page_preserves_partial_read(monkeypatch):
    import time
    module = collector(monkeypatch)
    clock = [1]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    class DeadlineTransport(SyntheticTransport):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            clock[0] = 3
            return response
    transport = DeadlineTransport([{'data': {'conversions': [conversion()]}}])
    result = module.collect(transport, start=START, end=END, deadline=2)
    assert result.coverage == 'PARTIAL' and result.record_count is None
    assert len(result.records) == 1 and len(transport.calls) == 1


@pytest.mark.parametrize('failure', [
    {'errors': [{'message': 'synthetic-token'}]},
    {'data': {'conversions': None}},
    _base.Response(403, b'synthetic-token', {}),
])
def test_provider_or_schema_failure_after_validated_page_remains_unknown(monkeypatch, failure):
    module = collector(monkeypatch)
    transport = SyntheticTransport([{'data': {'conversions': [conversion()]}}, failure])
    result = module.collect(transport, start=START, end=END)
    assert result.coverage == 'UNKNOWN' and result.record_count is None
    assert result.records == ()
    assert 'synthetic-token' not in json.dumps(result.to_dict())
