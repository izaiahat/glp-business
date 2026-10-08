"""Synthetic input-contract regressions; CI only, no live providers or credentials."""
import importlib.util
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('f12consumer', ROOT / 'scripts/glp334_live_reconciliation.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Reader:
    receipts = []

    def __init__(self, events, after=None):
        self.events = events
        self.counts = iter([len(events), len(events) if after is None else after])

    def query(self, sql):
        if sql == m.COUNT_SQL:
            return [{'n': next(self.counts)}]
        return self.events if sql == m.EVENT_SQL else []


def test_fresh_capture_never_infers_real_or_cash(tmp_path):
    rows = [{'network': 'affiliati', 'conversion_id': 'fixture-test'},
            {'network': 'cj', 'conversion_id': 'fixture-unknown'},
            {'network': 'cj', 'program_id': 'fella-health'}]
    snapshot = m.capture(Reader(rows), tmp_path, 'fixture-test')
    assert snapshot['receiver_row_count'] == 3 and snapshot['fella_excluded_count'] == 1
    assert snapshot['conversions'][0]['classification'] == 'TEST'
    assert 'classification' not in snapshot['conversions'][1]
    assert snapshot['financial_records'] == [] and snapshot['complete'] is False
    with pytest.raises(m.InputError, match='CHANGED_OR_TRUNCATED'):
        m.capture(Reader(rows, after=4), tmp_path, 'fixture-test')
    assert 'ELSE attributes_json' not in m.EVENT_SQL  # no raw customer/health attributes


def test_missing_or_stale_weekly_is_unknown_not_empty_success(tmp_path):
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    with pytest.raises(m.InputError, match='MISSING'):
        m.weekly_input(tmp_path, now)
    p = tmp_path / '20261002T110117Z-9d3123c2e214' / 'RESULT.json'
    p.parent.mkdir()
    doc = {'schema': 'glp-affiliate-weekly-report-v1', 'captured_at': now.isoformat(),
           'completed_at': now.isoformat(), 'observations': [], 'window': {},
           'invocation': 'MANUAL_READ_ONLY', 'source_pins_sha256': 'synthetic-not-real'}
    p.write_text(json.dumps(doc))
    got, ref = m.weekly_input(tmp_path, now)
    assert got == doc and ref['sha256'] == m.sha(p.read_bytes())
    with pytest.raises(m.InputError, match='STALE'):
        m.weekly_input(tmp_path, now + timedelta(days=9))
    doc['invocation'] = 'OFFLINE_DRY_RUN_NO_PROVIDER_IO'
    p.write_text(json.dumps(doc))
    with pytest.raises(m.InputError, match='NOT_COMPLETED_LIVE_READ'):
        m.weekly_input(tmp_path, now)


def test_full_roster_never_sums_overlapping_periods(monkeypatch):
    monkeypatch.setattr(m, 'ROOT', ROOT)
    # Restore sys.modules after the pin-bound native import to isolate sibling tests.
    import sys
    old = {name: sys.modules.get(name) for name in m.DEPENDENCIES}
    try:
        reporter, _ = m.load_reconciler()
        obs = [{'network': 'cj', 'period_purpose': period, 'commission_minor': None,
                'cash_minor': None, 'record_count': 0} for period in ('closed_week', 'lookback')]
        reconciliation = reporter.reconcile_conversions(None, obs, [])
        rows = m.network_rows({'cj', 'unavailable'}, obs, None, reconciliation, reporter)
        assert len(rows) == 2
        assert all(r['receiver_event_count'] is None and r['status'] == 'UNKNOWN' for r in rows)
        assert all(r['commission_minor'] is None and r['cash_minor'] is None for r in rows)
        assert len(rows[0]['periods']) == 2
        assert 'NETWORK_REPORT_UNAVAILABLE' in rows[1]['gaps']
    finally:
        for name, value in old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def test_financial_only_network_rows_are_owned_not_silently_dropped():
    class Reporter:
        digest = staticmethod(lambda value: 'a' * 64)
        _noncommercial = staticmethod(lambda value: 'TEST' if value.get('classification') == 'TEST' else None)

    observations = [{'network': 'partnerstack', 'period_purpose': 'closed_month',
        'start_inclusive': '2026-09-01T00:00:00+00:00', 'end_exclusive': '2026-10-01T00:00:00+00:00',
        'records': [{'provider_event_id': 'fixture-payout', 'resource': 'payouts',
                     'reported_amount': '0', 'amount_unit': 'UNKNOWN', 'currency': 'USD',
                     'classification': 'UNKNOWN', 'customer_email': 'DO_NOT_COPY'},
                    {'provider_event_id': 'fixture-test', 'classification': 'TEST'}]}]
    rows = m.network_rows({'partnerstack'}, observations, {'conversions': []}, {'rows': []}, Reporter)
    inputs = rows[0]['financial_input_records']
    assert len(inputs) == 1
    assert inputs[0]['provider_event_id'] == 'fixture-payout'
    assert inputs[0]['reported_amount'] == '0' and inputs[0]['amount_unit'] == 'UNKNOWN'
    assert inputs[0]['reconciliation_state'] == 'UNKNOWN' and inputs[0]['owner'] == 'GLP-334'
    assert inputs[0]['source_pointer'] == '/records/0'
    assert 'customer_email' not in inputs[0]
    assert rows[0]['commission_minor'] is rows[0]['cash_minor'] is None
    assert 'UNRECONCILED_NETWORK_FINANCIAL_RECORDS' in rows[0]['gaps']


def test_latest_closed_month_uses_execution_clock_not_closed_week(monkeypatch):
    monkeypatch.setattr(m, 'ROOT', ROOT)
    import sys
    old = {name: sys.modules.get(name) for name in m.DEPENDENCIES}
    try:
        reporter, _ = m.load_reconciler()
        w = reporter.windows(datetime(2026, 10, 3, tzinfo=timezone.utc))
        assert w['end_exclusive'] == '2026-09-28T00:00:00+00:00'
        assert w['last_closed_month_start'] == '2026-09-01T00:00:00+00:00'
        assert w['last_closed_month_end'] == '2026-10-01T00:00:00+00:00'
    finally:
        for name, value in old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture
def native_reporter(monkeypatch):
    """Use the pinned producer/exclusions, without leaking native module imports."""
    import sys
    monkeypatch.setattr(m, 'ROOT', ROOT)
    old = {name: sys.modules.get(name) for name in m.DEPENDENCIES}
    try:
        yield m.load_reconciler()[0]
    finally:
        for name, value in old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def test_actual_cj_producer_projection_reaches_financial_diagnostics(native_reporter, monkeypatch):
    """Fictional transport -> actual CJ producer -> consumer; never live income."""
    import socket
    import time
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('network forbidden'))
    document = {'data': {'publisherCommissions': {'count': 1, 'payloadComplete': True,
        'maxCommissionId': '999999', 'records': [{'commissionId': '999999',
            'advertiserId': '888888', 'advertiserName': 'OFFLINE FIXTURE ONLY',
            'publisherId': '1', 'postingDate': '2026-09-02T12:00:00Z',
            'eventDate': '2026-09-01T12:00:00Z', 'actionStatus': 'new',
            'validationStatus': 'new', 'original': True, 'pubCommissionAmountUsd': '25.00',
            'customer_email': 'DO_NOT_COPY', 'authorization': 'DO_NOT_COPY'}]}}}

    class Response:
        status = 200
        body = json.dumps(document).encode()
        def json(self):
            return document

    class Transport:
        calls = 0
        def request(self, method, url, **kwargs):
            assert (method, url) == ('POST', 'https://commissions.api.cj.com/query')
            assert 'sincePostingDate: "2026-09-01T00:00:00Z"' in kwargs['json_body']['query']
            assert 'beforePostingDate: "2026-09-08T00:00:00Z"' in kwargs['json_body']['query']
            self.calls += 1
            return Response()

    transport = Transport()
    observations = native_reporter.collect_cj(transport, 'OFFLINE-NONSECRET-FIXTURE', '1',
        '2026-09-01T00:00:00+00:00', '2026-09-08T00:00:00+00:00',
        deadline=time.monotonic() + 5)
    observations[0]['period_purpose'] = 'closed_week'
    assert transport.calls == 1 and observations[0]['status'] == 'READ_COMPLETE'
    assert observations[0]['accounting_projection'][0]['commission_minor'] == 2500
    assert 'records' not in observations[0]
    reconciliation = native_reporter.reconcile_conversions(None, observations, [])
    network = m.network_rows({'cj'}, observations, None, reconciliation, native_reporter)[0]
    inputs = network['financial_input_records']
    assert len(inputs) == 1  # Baseline silently drops the actual producer's array.
    record = inputs[0]
    assert record['provider_event_id'] == '999999' and record['program_id'] == '888888'
    assert record['source_pointer'] == '/accounting_projection/0'
    assert record['observation_sha256'] == native_reporter.digest(observations[0])
    for key in ('period_purpose', 'start_inclusive', 'end_exclusive'):
        assert record[key] == observations[0][key]
    assert record['owner'] == 'GLP-334'
    assert record['classification'] == record['attribution'] == record['reconciliation_state'] == 'UNKNOWN'
    assert record['commission_minor'] is record['cash_minor'] is None
    assert not {'customer_email', 'authorization', 'authenticated', 'posting_date',
                'receipt_pointer', 'pubCommissionAmountUsd', 'reported_amount'} & record.keys()
    assert network['commission_minor'] is network['cash_minor'] is None
    assert reconciliation['coverage_complete'] is False and reconciliation['rows'] == []
    assert 'UNRECONCILED_NETWORK_FINANCIAL_RECORDS' in network['gaps']


@pytest.mark.parametrize('field', ['records', 'accounting_projection'])
def test_native_exclusions_privacy_and_source_indexes_in_both_arrays(field, native_reporter):
    from glp_network_revenue_v2 import TEST_ID
    observation = {'network': 'cj', 'period_purpose': 'closed_month',
        'start_inclusive': '2026-09-01T00:00:00+00:00', 'end_exclusive': '2026-10-01T00:00:00+00:00',
        field: [None, {'classification': 'TEST'}, {'network': '__receiver_selftest__'},
            {'is_probe': True}, {'provider_slug': 'fella-health'}, {'program_id': 'Fella'},
            {'provider_event_id': TEST_ID}, {'is_test': 1}, {'probe_kind': 'program_liveness'},
            {'provider_event_id': 'fixture-real', 'classification': 'REAL', 'attribution': 'GLP',
             'authenticated': True, 'customer_email': 'DO_NOT_COPY', 'authorization': 'DO_NOT_COPY',
             'commission_minor': 2500, 'cash_minor': 2500, 'reported_amount': '0',
             'amount_unit': 'UNKNOWN', 'currency': 'USD'},
            {'provider_event_id': 'invalid@email.example', 'reported_amount': 'not-money',
             'currency': 'usd', 'raw_customer': 'DO_NOT_COPY'}]}
    records = m.financial_input_rows([observation], native_reporter)
    assert len(records) == 2
    assert [r['source_pointer'] for r in records] == ['/' + field + '/9', '/' + field + '/10']
    assert records[0]['provider_event_id'] == 'fixture-real'
    assert records[0]['reported_amount'] == '0' and records[0]['amount_unit'] == 'UNKNOWN'
    assert not {'provider_event_id', 'reported_amount', 'currency'} & records[1].keys()
    for record in records:
        assert record['observation_sha256'] == native_reporter.digest(observation)
        assert record['owner'] == 'GLP-334'
        assert record['classification'] == record['attribution'] == record['reconciliation_state'] == 'UNKNOWN'
        assert record['commission_minor'] is record['cash_minor'] is None
        assert not {'authenticated', 'customer_email', 'authorization', 'raw_customer'} & record.keys()
        for key in ('period_purpose', 'start_inclusive', 'end_exclusive'):
            assert record[key] == observation[key]


def test_both_native_arrays_keep_distinct_pointers_not_additive_money(native_reporter):
    record = {'provider_event_id': 'fixture-same', 'reported_payout': '25.00'}
    observations = [{'network': 'cj', 'period_purpose': purpose,
        'records': [record], 'accounting_projection': [record]}
        for purpose in ('closed_week', 'lookback')]
    rows = m.network_rows({'cj'}, observations, None, {'rows': []}, native_reporter)
    inputs = rows[0]['financial_input_records']
    assert len(inputs) == 4
    assert [(r['period_purpose'], r['source_pointer']) for r in inputs] == [
        ('closed_week', '/records/0'), ('closed_week', '/accounting_projection/0'),
        ('lookback', '/records/0'), ('lookback', '/accounting_projection/0')]
    assert [r['observation_sha256'] for r in inputs] == [
        native_reporter.digest(o) for o in observations for _ in range(2)]
    assert all(r['commission_minor'] is r['cash_minor'] is None for r in inputs)
    assert rows[0]['commission_minor'] is rows[0]['cash_minor'] is None


def test_read_transport_allowlist_and_redirect_refusal():
    reader = object.__new__(m.ReceiverReader)  # do not load credentials
    with pytest.raises(m.InputError, match='NON_ALLOWLISTED'):
        reader.query('DELETE FROM analytics_revenue_ledger_v1')
    with pytest.raises(m.InputError, match='REDIRECT_REFUSED'):
        m.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com/')
