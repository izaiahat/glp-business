"""Offline fixtures, never production revenue or credential material."""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import affiliate_network_access as access
from marketplace_adapters import _base


def test_maxbounty_official_reporting_is_dispatched_not_portal_only(monkeypatch, tmp_path):
    credentials = tmp_path / 'offline-credentials.json'
    credentials.write_text(json.dumps({'email': 'ops@glpclinicscout.com', 'password': 'SYNTHETIC_PASSWORD'}))
    monkeypatch.setenv('MAXBOUNTY_API_CREDENTIALS_FILE', str(credentials))
    class Transport:
        def __init__(self):
            self.calls = []
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs.get('params')))
            if url.endswith('/authentication'):
                return _base.json_response({'success': True, 'mb-api-token': 'SYNTHETIC_TOKEN'})
            if url.endswith('/affiliate'):
                return _base.json_response({'success': True, 'publisher_id': 123, 'affiliate_personal_information': {'email': 'ops@glpclinicscout.com'}})
            if url.endswith('/earnings'):
                return _base.json_response({'success': True, 'report': [{'campaign_id': 456, 'name': 'Offline candidate', 'earnings': 12.5}], 'totals': {'earnings': 12.5, 'leads': 1, 'clicks': 2}})
            if url.endswith('/conversions'):
                return _base.json_response({'success': True, 'report': [{'key_id': 'offline-conversion', 'campaign_id': 456, 'name': 'Offline candidate', 'date': '2026-09-12T12:00:00Z', 'earnings': 12.5, 'status': 'Payable', 'ip_address': 'SYNTHETIC_PRIVATE_IP'}], 'page': 1, 'total_pages': 1})
            if url.endswith('/reversals'):
                return _base.json_response({'success': True, 'report': [], 'page': 1, 'total_pages': 0})
            if '/accountBalance/' in url:
                return _base.json_response({'success': True, 'account_balances': [{'date': '2026-09-30T00:00:00Z', 'type': 'Earned', 'amount': 12.5, 'notes': 'SYNTHETIC_PRIVATE_NOTE'}]})
            raise AssertionError('non-read route')
    transport = Transport()
    result = access.conversions_earnings('maxbounty', transport,
        start='2026-09-01T00:00:00+00:00', end='2026-10-01T00:00:00+00:00').to_dict()
    assert result['status'] == 'SUPPORTED'
    assert result['coverage'] == 'PARTIAL'
    assert result['account_binding'] == 'RESPONSE_OWNED_PUBLISHER_ID:123'
    assert result['record_count'] is None
    assert result['reported_totals']['earnings'] == '12.5'
    assert any(r.get('provider_event_id') == 'offline-conversion' for r in result['records'])
    assert any(r.get('record_kind') == 'account_balance_transaction' for r in result['records'])
    serialized = json.dumps(result)
    for secret in ('SYNTHETIC_PASSWORD', 'SYNTHETIC_TOKEN', 'SYNTHETIC_PRIVATE_IP', 'SYNTHETIC_PRIVATE_NOTE'):
        assert secret not in serialized
    assert result['report_complete_period'] is False
    assert all(r.get('currency') is None and r.get('classification') == 'UNKNOWN' for r in result['records'])
    assert all(r.get('commission_minor') is None and r.get('cash_minor') is None for r in result['records'])
    assert len(result['receipts']) == 5
    assert [m for m, _, _ in transport.calls].count('POST') == 1
    assert all('/tracking' not in url and '/campaign' not in url for _, url, _ in transport.calls)


def test_maxbounty_read_guard_refuses_tracking_mutations_and_redirect_hosts():
    import pytest
    wrapped = access.ReadTransport(object())
    for method, url in [
        ('GET', 'https://api.maxbounty.com/affiliates/api/trackingLink/456'),
        ('POST', 'https://api.maxbounty.com/affiliates/api/reports/earnings'),
        ('GET', 'https://api.maxbounty.com/affiliates/api/reports/earnings/mutate'),
        ('GET', 'https://unbound.example/affiliates/api/reports/earnings')]:
        with pytest.raises(ValueError, match='route_not_read_allowlisted'):
            wrapped.request(method, url)


def test_maxbounty_missing_credentials_keeps_financial_fields_null(monkeypatch):
    import affiliate_reporting_maxbounty as reader
    monkeypatch.setattr(reader, 'credentials_path', lambda: None)
    result = reader.collect(object(), start='2026-09-01T00:00:00+00:00', end='2026-10-01T00:00:00+00:00')
    assert result.status == 'SUPPORTED' and result.coverage == 'UNKNOWN'
    assert result.records == () and result.receipts == ()
    assert result.reported_totals is result.commission_minor is result.cash_minor is None


def test_j5_successor_preserves_historical_wrapper_and_pins_exact_source():
    import hashlib
    root = Path(__file__).resolve().parents[1]
    original = (root / 'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting.sh').read_bytes()
    old_manifest = (root / 'ops/glp/reporting-v2/source-manifest.json').read_bytes()
    manifest = (root / 'ops/glp/reporting-v2/source-manifest-j5.json').read_bytes()
    before, after = hashlib.sha256(old_manifest).hexdigest().encode(), hashlib.sha256(manifest).hexdigest().encode()
    assert original.count(before) == original.count(b'ops/glp/reporting-v2/source-manifest.json') == 2
    expected = original.replace(before, after).replace(b'ops/glp/reporting-v2/source-manifest.json', b'ops/glp/reporting-v2/source-manifest-j5.json')
    assert (root / 'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting_j5.sh').read_bytes() == expected
    pins = json.loads(manifest)['files']
    for relative in ('scripts/affiliate_network_access.py', 'scripts/affiliate_reporting_maxbounty.py', 'scripts/maxbounty_publisher_api.py'):
        assert pins['/home/ubuntu/business/' + relative] == hashlib.sha256((root / relative).read_bytes()).hexdigest()
