"""Offline regressions for PR119's five reproduced review findings."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import affiliate_network_access as c
from marketplace_adapters import _base as b, impact, partnerize, cj, katalys

START = '2026-09-21T00:00:00+00:00'
END = '2026-09-28T00:00:00+00:00'
PROFILE = '00000000-0000-0000-0000-000000000001'


class FixtureTransport:
    def __init__(self, doc, status=200, headers=None):
        self.doc, self.status, self.headers = doc, status, headers or {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        body = self.doc if isinstance(self.doc, str) else json.dumps(self.doc)
        return b.Response(self.status, body, self.headers)


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch):
    import socket
    def deny(*a, **k):
        raise BaseException('offline_external_effect_forbidden')
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(b.RequestsTransport, 'request', deny)
    monkeypatch.setattr(b, 'secret_json', deny)
    monkeypatch.setattr(b, 'env_values', deny)
    monkeypatch.setattr(b, 'secret_text', lambda *a: 'fixture-only')
    monkeypatch.setattr(impact, 'credentials', lambda: ('FIXTURE', 'fixture-only'))
    monkeypatch.setattr(partnerize, '_auth', lambda: ('FIXTURE', 'fixture-only'))
    monkeypatch.setattr(cj, '_secret', lambda: ('fixture-only', 'FIXTURE', 'PROPERTY'))


@pytest.mark.parametrize('network,doc', [
    ('impact', {'Actions': [], '@total': '0', '@numpages': '0', '@page': '1',
                'Errors': [{'Code': 'FIXTURE_UNAVAILABLE'}]}),
    ('partnerize', {'conversions': [], 'count': 0, 'errors': [{'message': 'FIXTURE_UNAVAILABLE'}]}),
])
def test_provider_error_envelopes_are_unknown_not_zero(network, doc):
    r = c.conversions_earnings(network, FixtureTransport(doc), start=START, end=END)
    assert r.coverage == 'UNKNOWN'
    assert r.record_count is r.commission_minor is r.cash_minor is None
    assert not r.records


@pytest.mark.parametrize('terms', [[{}], [{'title': None, 'content': ''}],
    [{'title': 'Title only', 'content': '<p> </p>'}],
    [{'content': 'Real fixture term'}, {}]])
def test_partnerize_empty_terms_do_not_prove_metadata(terms):
    r = c.terms_commission('partnerize', FixtureTransport({'terms': terms}), program_id='10')
    assert r.coverage != 'COMPLETE' and r.record_count is None


def test_partnerize_meaningful_terms_still_complete():
    r = c.terms_commission('partnerize', FixtureTransport({'terms': [{'content': '<p>Fixture terms</p>'}]}), program_id='10')
    assert r.coverage == 'COMPLETE' and r.record_count == 1


def test_cj_missing_relationship_does_not_prove_metadata():
    xml = '<cj-api><advertisers><advertiser><advertiser-id>10</advertiser-id></advertiser></advertisers></cj-api>'
    r = c.program_status('cj', FixtureTransport(xml), program_id='10')
    assert r.coverage != 'COMPLETE' and r.record_count is None


def test_fella_impact_campaign_name_excluded_before_projection():
    doc = {'Actions': [{'Id': 'FIXTURE', 'CampaignId': '10', 'CampaignName': 'Fella Health',
                        'Payout': '99.00', 'Currency': 'USD', 'State': 'approved'}],
           '@total': '1', '@numpages': '1', '@page': '1'}
    r = c.conversions_earnings('impact', FixtureTransport(doc), start=START, end=END)
    assert r.coverage == 'COMPLETE' and not r.records


@pytest.mark.parametrize('headers,minimum', [({}, 60), ({'Retry-After': '120'}, 120),
    ({'retry-after': '120'}, 120),
    ({'Retry-After': 'Thu, 01 Jan 1970 00:20:00 GMT'}, 200),
    ({'Retry-After': 'invalid'}, 60)])
def test_katalys_shared_429_cooldown_no_retry(tmp_path, headers, minimum):
    limiter = katalys.SharedReportingLimiter(tmp_path / 'limiter.json', clock=lambda: 1000., sleep=lambda _: None)
    t = FixtureTransport({'error': 'FIXTURE_RATE_LIMIT'}, 429, headers)
    r = c.conversions_earnings('katalys', t, start=START, end=END, profile_id=PROFILE, limiter=limiter)
    assert json.loads(limiter.path.read_text())['not_before'] >= 1000 + minimum
    assert len(t.calls) == 1
    assert r.coverage != 'COMPLETE' and r.record_count is None
    assert r.receipts[0]['http_status'] == 429


@pytest.mark.parametrize('userinfo', ['user:FIXTURE_PASSWORD', ':FIXTURE_PASSWORD', 'user', ''])
def test_all_url_userinfo_refused_before_call_or_receipt(userinfo):
    t = FixtureTransport([])
    guard = c.ReadTransport(t)
    with pytest.raises(ValueError, match='route_not_read_allowlisted'):
        guard.request('GET', f'https://{userinfo}@api.awin.com/publishers/2979387/transactions/')
    assert not t.calls and not guard.receipts


def test_weekly_error_envelopes_persist_unknown_not_zero(tmp_path):
    import glp_affiliate_weekly_reporting as weekly
    class Router:
        def request(self, method, url, **kw):
            doc = ({'Actions': [], '@total': '0', '@numpages': '0', '@page': '1', 'Errors': ['FIXTURE_ERROR']}
                   if 'impact.com' in url else {'conversions': [], 'count': 0, 'errors': ['FIXTURE_ERROR']})
            return FixtureTransport(doc).request(method, url, **kw)
    names = ['impact', 'partnerize']
    weekly.run(tmp_path / 'report', {'marketplace_count': 2, 'marketplaces': [{'marketplace': n} for n in names]}, [],
        as_of=datetime(2026, 9, 30, tzinfo=timezone.utc), collectors=c.weekly_collectors(names, Router()))
    doc = json.loads((tmp_path / 'report' / 'RESULT.json').read_text())
    assert len(doc['observations']) == 6
    assert all(r['record_count'] is None and r['coverage'] == 'UNKNOWN' for r in doc['observations'])
    assert doc['fleet_cash_minor'] is doc['fleet_commission_minor'] is None


# GLP-333: synthetic HTTP 200 envelope that declares one row but returns none.
KATALYS_ROWCOUNT_MISMATCH = {'success': True, 'rowCount': 1, 'rows': []}


def test_weekly_katalys_rowcount_rows_mismatch_reason_preserved(tmp_path, monkeypatch):
    import glp_affiliate_weekly_reporting as weekly
    limiter = katalys.SharedReportingLimiter(tmp_path / 'limiter.json', clock=lambda: 1000., sleep=lambda _: None)
    monkeypatch.setattr(katalys, 'SharedReportingLimiter', lambda *a, **k: limiter)
    t = FixtureTransport(KATALYS_ROWCOUNT_MISMATCH)
    weekly.run(tmp_path / 'report', {'marketplace_count': 1, 'marketplaces': [{'marketplace': 'katalys'}]}, [],
        as_of=datetime(2026, 9, 30, tzinfo=timezone.utc), collectors=c.weekly_collectors(['katalys'], t))
    doc = json.loads((tmp_path / 'report' / 'RESULT.json').read_text())
    assert len(doc['observations']) == 3 and len(t.calls) == 3
    for row in doc['observations']:
        assert row['reason'] == 'katalys_rowcount_rows_mismatch'
        assert row['declared_row_count'] == 1 and row['returned_row_count'] == 0
        assert row['coverage'] in {'PARTIAL', 'UNKNOWN'} and row['payload_complete'] is False
        assert row['record_count'] is None and row['records'] == []
    assert doc['fleet_cash_minor'] is doc['fleet_commission_minor'] is None
