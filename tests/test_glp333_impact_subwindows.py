"""GLP-333: Impact Actions ranges longer than 45 days split into half-open sub-windows.

Offline only: fixtures deny sockets, real transports and secret reads.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import affiliate_network_access as c
from glp_affiliate_weekly_reporting import windows
from marketplace_adapters import _base as b, impact

START = '2026-07-02T00:00:00+00:00'
END = '2026-09-30T00:00:00+00:00'  # 90 days
W1 = ('2026-07-02T00:00:00Z', '2026-08-15T23:59:59Z')
W2 = ('2026-08-16T00:00:00Z', '2026-09-29T23:59:59Z')


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch):
    import socket
    def deny(*a, **k):
        raise BaseException('offline_external_effect_forbidden')
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(b.RequestsTransport, 'request', deny)
    monkeypatch.setattr(b, 'secret_json', deny)
    monkeypatch.setattr(b, 'env_values', deny)
    monkeypatch.setattr(impact, 'load_impact_api_credentials', deny)
    monkeypatch.setattr(impact, 'credentials', lambda: ('FIXTURE', 'fixture-only'))


def action(identity, state='pending', event_date='2026-07-03T00:00:00Z', **extra):
    return {'Id': identity, 'CampaignId': '10', 'CampaignName': 'Fixture Brand', 'State': state,
            'Payout': '1.00', 'Currency': 'USD', 'EventDate': event_date, **extra}


def page(rows, *, number, numpages, total):
    return {'Actions': rows, '@page': str(number), '@numpages': str(numpages), '@total': str(total)}


class WindowTransport:
    """Pages keyed by (StartDate, Page); any other request is a fixture failure."""
    def __init__(self, pages, status=None):
        self.pages, self.status = pages, status or {}
        self.calls = []

    def request(self, method, url, **kwargs):
        params = kwargs['params']
        self.calls.append(dict(params))
        key = (params['StartDate'], params['Page'])
        if key in self.status:
            return b.Response(self.status[key], b'{}', {})
        return b.Response(200, json.dumps(self.pages[key]), {})


def empty_windows(*starts):
    return {(s, 1): page([], number=1, numpages=0, total=0) for s in starts}


def test_90_day_lookback_splits_into_two_contiguous_45_day_windows():
    t = WindowTransport(empty_windows(W1[0], W2[0]))
    r = c.conversions_earnings('impact', t, start=START, end=END)
    sent = [(p['StartDate'], p['EndDate']) for p in t.calls]
    assert sent == [W1, W2]
    assert all('EventDate' not in k for p in t.calls for k in p)
    assert r.coverage == 'COMPLETE' and r.record_count == 0
    assert [(x['start_inclusive'], x['end_exclusive']) for x in r.receipts] == [
        ('2026-07-02T00:00:00+00:00', '2026-08-16T00:00:00+00:00'),
        ('2026-08-16T00:00:00+00:00', '2026-09-30T00:00:00+00:00')]


def test_window_partition_is_half_open_and_bounded():
    a = datetime(2026, 1, 1, 12, 30, 5, tzinfo=timezone.utc)
    for days in (1, 44, 45, 46, 90, 91, 100, 366):
        b_ = a + timedelta(days=days, seconds=7)
        parts = c._impact_windows(a, b_)
        assert parts[0][0] == a and parts[-1][1] == b_
        assert all(l < r <= l + timedelta(days=45) for l, r in parts)
        assert all(parts[i][1] == parts[i+1][0] for i in range(len(parts)-1))
    assert len(c._impact_windows(a, a + timedelta(days=45))) == 1
    assert len(c._impact_windows(a, a + timedelta(days=45, seconds=1))) == 2


def test_weekly_lookback_window_reads_impact_in_sub_windows():
    w = windows(datetime(2026, 10, 1, 9, tzinfo=timezone.utc), 90)
    assert (w['lookback_start'], w['end_exclusive']) == ('2026-06-30T00:00:00+00:00', '2026-09-28T00:00:00+00:00')
    t = WindowTransport(empty_windows('2026-06-30T00:00:00Z', '2026-08-14T00:00:00Z'))
    r = c.conversions_earnings('impact', t, start=w['lookback_start'], end=w['end_exclusive'])
    assert [(p['StartDate'], p['EndDate']) for p in t.calls] == [
        ('2026-06-30T00:00:00Z', '2026-08-13T23:59:59Z'), ('2026-08-14T00:00:00Z', '2026-09-27T23:59:59Z')]
    assert r.coverage == 'COMPLETE' and r.record_count == 0


def test_pagination_and_count_reconciliation_reset_per_window():
    pages = {
        (W1[0], 1): page([action('A1'), action('A2')], number=1, numpages=2, total=3),
        (W1[0], 2): page([action('A3')], number=2, numpages=2, total=3),
        (W2[0], 1): page([action('B1')], number=1, numpages=2, total=2),
        (W2[0], 2): page([action('B2')], number=2, numpages=2, total=2),
    }
    t = WindowTransport(pages)
    r = c.conversions_earnings('impact', t, start=START, end=END)
    assert [(p['StartDate'], p['Page']) for p in t.calls] == [(W1[0], 1), (W1[0], 2), (W2[0], 1), (W2[0], 2)]
    assert r.coverage == 'COMPLETE' and r.record_count == 5 and len(r.records) == 5
    assert [x['page'] for x in r.receipts] == [1, 2, 1, 2]


def test_repeated_action_ids_across_windows_are_not_double_counted():
    # Impact's StartDate/EndDate axis is not EventDate: an action updated in both
    # sub-windows is returned twice and its latest read is kept once.
    pages = {
        (W1[0], 1): page([action('A1'), action('A2')], number=1, numpages=1, total=2),
        (W2[0], 1): page([action('A2', state='approved'), action('B1', event_date='2026-01-05T00:00:00Z')],
                         number=1, numpages=1, total=2),
    }
    r = c.conversions_earnings('impact', WindowTransport(pages), start=START, end=END)
    assert r.coverage == 'COMPLETE' and r.record_count == 3 and len(r.records) == 3
    assert [x['source_status'] for x in r.records] == ['pending', 'approved', 'pending']


def test_duplicate_action_id_within_one_window_stays_unknown():
    pages = {(W1[0], 1): page([action('A1'), action('A1')], number=1, numpages=1, total=2)}
    r = c.conversions_earnings('impact', WindowTransport(pages), start=START, end=END)
    assert r.coverage == 'UNKNOWN' and r.record_count is None and not r.records


def test_fella_excluded_across_sub_windows():
    pages = {
        (W1[0], 1): page([action('A1', CampaignName='Fella Health')], number=1, numpages=1, total=1),
        (W2[0], 1): page([action('B1')], number=1, numpages=1, total=1),
    }
    r = c.conversions_earnings('impact', WindowTransport(pages), start=START, end=END)
    assert r.coverage == 'COMPLETE' and r.record_count == 2
    assert len(r.records) == 1 and 'fella' not in json.dumps(r.to_dict()).casefold()


@pytest.mark.parametrize('failure', ['http_500', 'count_short', 'page_mismatch'])
def test_partial_window_failure_keeps_whole_window_unknown(failure):
    pages = {(W1[0], 1): page([action('A1')], number=1, numpages=1, total=1)}
    status = {}
    if failure == 'http_500':
        status[(W2[0], 1)] = 500
    elif failure == 'count_short':
        pages[(W2[0], 1)] = page([action('B1')], number=1, numpages=1, total=2)
    else:
        pages[(W2[0], 1)] = page([action('B1')], number=2, numpages=1, total=1)
    t = WindowTransport(pages, status)
    r = c.conversions_earnings('impact', t, start=START, end=END)
    assert [p['StartDate'] for p in t.calls] == [W1[0], W2[0]]
    assert r.coverage == 'UNKNOWN'
    assert r.record_count is r.commission_minor is r.cash_minor is None
    assert not r.records and len(r.receipts) == 2


def test_short_window_is_one_unchanged_request():
    t = WindowTransport(empty_windows('2026-09-21T00:00:00Z'))
    r = c.conversions_earnings('impact', t, start='2026-09-21T00:00:00+00:00', end='2026-09-28T00:00:00+00:00')
    assert t.calls == [{'StartDate': '2026-09-21T00:00:00Z', 'EndDate': '2026-09-27T23:59:59Z',
                        'PageSize': 100, 'Page': 1}]
    assert r.coverage == 'COMPLETE' and r.record_count == 0
