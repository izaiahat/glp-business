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


def test_read_transport_allowlist_and_redirect_refusal():
    reader = object.__new__(m.ReceiverReader)  # do not load credentials
    with pytest.raises(m.InputError, match='NON_ALLOWLISTED'):
        reader.query('DELETE FROM analytics_revenue_ledger_v1')
    with pytest.raises(m.InputError, match='REDIRECT_REFUSED'):
        m.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com/')
