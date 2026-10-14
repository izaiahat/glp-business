"""Offline J7 main regression: real collectors, synthetic wire, no live effects."""
import json
import socket
import sys
import time
import pytest
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import glp_affiliate_weekly_reporting_j7 as j7
from marketplace_adapters import _base as base
from marketplace_adapters import katalys_j7
import affiliate_reporting_maxbounty as maxbounty


class EffectDenied(BaseException):
    pass


class FixtureSession:
    """Synthetic protocol responses; records attempts, never touches a socket."""
    max_redirects = 30

    def __init__(self):
        self.attempts = []
        self.adapters = {}

    def mount(self, prefix, adapter):
        self.adapters[prefix] = adapter

    def request(self, method, url, **kwargs):
        self.attempts.append((method, url))
        if url.endswith('/authentication'):
            payload = {'success': True, 'mb-api-token': 'SYNTHETIC_TOKEN', 'mb-aid': 803003}
        elif url.endswith('/affiliate'):
            payload = {'success': True, 'publisher_id': 803003,
                       'affiliate_personal_information': {'email': 'ops@glpclinicscout.com'}}
        elif url.endswith('/reports/earnings'):
            payload = {'success': True, 'report': [], 'totals': {'earnings': 0, 'clicks': 0, 'leads': 0, 'sales': 0}}
        elif '/accountBalance/' in url:
            payload = {'success': False, 'errors': {'message': 'You currently have no account balances.'}}
        elif 'api.maxbounty.com' in url:
            payload = {'success': True, 'report': [], 'total_pages': 0, 'page': 1}
        elif 'api.katalys.com' in url:
            payload = {'success': True, 'rowCount': 1 if url.endswith('/conversions') else 0, 'rows': []}
        elif url.endswith('/getUserInfo'):
            payload = {'result': 'success', 'data': {'user_id': 5725668, 'granted_roles': 'affiliate'}}
        elif url.endswith('/listCommissions'):
            payload = {'result': 'success', 'data': {'item_count': 0, 'page_count': 0,
                       'items': [], 'page_no': 1, 'page_size': 100}}
        elif url.endswith('/listPayouts'):
            payload = {'result': 'success', 'data': {'payout_list': []}}
        else:
            raise EffectDenied('unexpected_fixture_route')
        return SimpleNamespace(status_code=200, content=json.dumps(payload).encode(), headers={}, url=url)


class FixtureLimiter:
    def request(self, transport, method, url, **kwargs):
        return transport.request(method, url, **kwargs)


def test_actual_j7_main_shares_32_attempts_across_networks_auth_and_corroboration(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        raise EffectDenied('network_or_live_guard_effect_denied')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    session = FixtureSession()
    # Replace only the session I/O seam; main and maintained dispatch/readers run.
    monkeypatch.setattr(base.RequestsTransport, '_sess', lambda self: session)
    monkeypatch.setattr(base, 'secret_text', lambda *args: 'SYNTHETIC_KEY')
    credentials = tmp_path / 'fixture-credentials.json'
    credentials.write_text(json.dumps({'email': 'ops@glpclinicscout.com', 'password': 'SYNTHETIC_PASSWORD'}))
    monkeypatch.setattr(maxbounty, 'credentials_path', lambda: credentials)
    monkeypatch.setattr(katalys_j7, 'SharedReportingLimiter', FixtureLimiter)
    # Immutable pin checking has a separate unchanged oracle. This synthetic
    # boundary prevents installed/shared source hashes, not maintained readers.
    monkeypatch.setattr(j7, 'verify_pins', lambda path, required: 'OFFLINE_FIXTURE_ONLY')
    roster = tmp_path / 'roster.json'
    names = ['maxbounty', 'katalys', 'digistore24']
    roster.write_text(json.dumps({'marketplace_count': len(names), 'marketplaces': [{'marketplace': n} for n in names]}))
    registry = tmp_path / 'registry.json'
    registry.write_text(json.dumps({'schema': 'reporting-identity-registry-v1', 'identities': []}))
    output = tmp_path / 'report'
    assert j7.main(['--source-manifest', str(tmp_path / 'not-used'), '--roster', str(roster),
                    '--identity-registry', str(registry), '--output', str(output),
                    '--live-network-readonly', '--max-requests', '32', '--budget-seconds', '240']) == 0
    result = json.loads((output / 'RESULT.json').read_text())
    assert len(session.attempts) == 32, f'run-wide32 exceeded: {len(session.attempts)} actual synthetic attempts'
    assert sum(url.endswith('/authentication') for _, url in session.attempts) == 3
    assert sum(url.endswith('/stats') for _, url in session.attempts) == 3
    assert result['http_budget'] == {'scope': 'RUN_WIDE_OUTBOUND_ATTEMPTS',
                                   'limit': 32, 'attempted': 32, 'remaining': 0, 'exhausted': True,
                                   'includes': 'REPORTING_AUTHENTICATION_FAILED_ATTEMPTS_AND_CALLER_RETRIES',
                                   'automatic_http_retries': 0, 'automatic_redirects': 0}
    assert session.max_redirects == 0
    assert all(adapter.max_retries.total == 0 for adapter in session.adapters.values())
    assert result['status'] == 'PARTIAL' and result['coverage_complete'] is False
    assert result['fleet_commission_minor'] is None and result['fleet_cash_minor'] is None
    assert result['payment_candidates'] == []
    assert any(g['state'] == 'REQUEST_BUDGET_EXHAUSTED_PARTITION' and g['network'] == 'digistore24'
               and 'closed_month' in g['unread_or_partial_periods'] for g in result['gaps'])
    assert all(page['financial_classification'] != 'REAL' for page in result['observations'])
    assert all(p['financial_classification'] == 'UNKNOWN' for p in result['observations'] if p['network'] == 'digistore24')
    text = (output / 'RESULT.json').read_text()
    assert 'SYNTHETIC_TOKEN' not in text and 'SYNTHETIC_PASSWORD' not in text and 'SYNTHETIC_KEY' not in text


def test_failed_auth_caller_retry_and_later_unread_share_one_counter(monkeypatch, tmp_path):
    import requests
    session = FixtureSession()
    attempts = []
    def failed(method, url, **kwargs):
        attempts.append((method, url))
        raise requests.exceptions.ConnectionError('OFFLINE_SYNTHETIC_FAILURE')
    session.request = failed
    monkeypatch.setattr(base.RequestsTransport, '_sess', lambda self: session)
    transport = j7.RunReadTransport(max_requests=2, deadline=time.monotonic()+60)
    def first(window, deadline, remaining, *, sink):
        assert remaining == 2
        for call in (transport.auth_request, transport.request):
            with pytest.raises(requests.exceptions.ConnectionError):
                call('POST', 'https://api.maxbounty.com/affiliates/api/authentication', json_body={})
        with pytest.raises(ValueError, match='run_request_budget_exhausted'):
            transport.request('GET', 'https://api.maxbounty.com/affiliates/api/affiliate')
        return []
    def unread(*args, **kwargs):
        raise EffectDenied('exhausted_collector_must_not_run')
    names = ['maxbounty', 'digistore24']
    result = j7.run(tmp_path/'report', {'marketplace_count': 2, 'marketplaces': [{'marketplace': n} for n in names]},
                    [], collectors=dict(zip(names, (first, unread))), max_requests=2, request_transport=transport)
    assert len(attempts) == transport.calls == result['http_budget']['attempted'] == 2
    assert result['http_budget']['exhausted'] is True
    assert result['fleet_cash_minor'] is None and result['fleet_commission_minor'] is None
    assert any(g['network'] == 'digistore24' and g['state'] == 'REQUEST_BUDGET_UNREAD'
               and len(g['unread_or_partial_periods']) == 3 for g in result['gaps'])
    checkpoint = json.loads((tmp_path/'report/checkpoint.json').read_text())
    assert checkpoint['http_budget'] == result['http_budget']


def test_real_session_redirect_is_one_attempt_and_deadline_denies_retry(monkeypatch):
    import requests
    from requests.adapters import HTTPAdapter
    sends = []
    def synthetic_send(self, request, **kwargs):
        assert self.max_retries.total == 0
        sends.append(request.url)
        response = requests.Response()
        response.status_code = 302
        response._content = b'OFFLINE_REDIRECT'
        response.url = request.url
        response.request = request
        response.headers['Location'] = 'https://example.invalid/never-follow'
        return response
    def denied(*args, **kwargs):
        raise EffectDenied('network_effect_denied')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(HTTPAdapter, 'send', synthetic_send)
    transport = j7.RunReadTransport(max_requests=32, deadline=time.monotonic()+60)
    with pytest.raises(requests.exceptions.TooManyRedirects):
        transport.request('GET', 'https://api.maxbounty.com/affiliates/api/affiliate')
    assert sends == ['https://api.maxbounty.com/affiliates/api/affiliate']
    assert transport.calls == 1
    transport.deadline = time.monotonic()-1
    with pytest.raises(ValueError, match='run_request_deadline_exhausted'):
        transport.auth_request('POST', 'https://api.maxbounty.com/affiliates/api/authentication')
    assert transport.calls == len(sends) == 1
