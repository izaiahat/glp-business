import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import glp_price_freshness as p


def test_exact_existing_attestation_renews_known_prices_not_unknowns():
    quote = 'Compounded semaglutide injection starts from $99/mo.'
    witness = {'source_url': 'https://clinic.example/pricing', 'quote': quote,
               'capture_path': 'data/pricing-evidence/' + 'a' * 64 + '.md',
               'capture_sha256': 'a' * 64, 'captured_at_utc': '2026-09-01T00:00:00Z'}
    row = {'row_id': 'clinic:sema', 'medication_id': 'compounded-semaglutide-injection',
           'price_kind': 'floor_from', 'facts': {
               'amount_usd': {'value': 99, 'evidence': [witness], 'artifact_needed': None},
               'period': {'value': '/mo', 'evidence': [witness], 'artifact_needed': None},
               'billing_period': {'value': 'UNKNOWN', 'evidence': [], 'artifact_needed': 'Checkout terms'}},
           'identity_evidence': [witness], 'evidence': [witness]}
    before = copy.deepcopy(row)
    cap = {'source_url': witness['source_url'], 'markdown': quote,
           'captured_at_utc': '2026-10-09T00:00:00Z', 'content_sha256': p.digest(quote)}
    after = p.reattest_row(row, {cap['source_url']: cap})
    assert after['facts']['amount_usd']['value'] == 99
    assert after['facts']['amount_usd']['evidence'][0]['captured_at_utc'] == cap['captured_at_utc']
    assert after['facts']['billing_period'] == before['facts']['billing_period']
    assert row == before
    assert p.reattest_row(row, {cap['source_url']: {**cap, 'markdown': quote.replace('$99', '$199')}}) is None
    assert p.reattest_row(row, {'https://other.example': cap}) is None


def test_weekly_batch_and_unchanged_unknown_page_skip_json(tmp_path, monkeypatch):
    row = {'row_id': 'clinic:sema', 'medication_id': 'compounded-semaglutide-injection',
           'price_kind': 'floor_from', 'facts': {'amount_usd': {'value': 99}, 'period': {'value': '/mo'}}}
    url = 'https://clinic.example/pricing'
    raw = 'Unpriced source. Unknown remains unknown. No attested medication-price quotation is present here.'
    release = tmp_path / 'release'
    p.put(release / 'data/program-registry.json', {'programs': [{'program_id': 'clinic', 'commercial_state': 'live'}],
        'provider_dynamic_profiles': {'profiles': {'clinic': {'provider': 'Clinic', 'source_urls': [url]}}}})
    p.put(release / 'data/provider-pricing-matrix.json', {'providers': {'clinic': {'medications': [row]}}})
    p.put(release / 'data/affiliate-activation-selection.json', {})
    state = tmp_path / 'state'
    key = p.digest('clinic|' + url)[:24]
    p.put(state / (key + '.json'), {'markdown': raw})
    class Client:
        def request(self, endpoint, payload=None, **kwargs):
            if endpoint == 'batch/scrape':
                assert payload['urls'] == [url]
                assert payload['formats'] == ['markdown']
                return {'success': True, 'id': 'fixture-batch'}
            return {'status': 'completed', 'total': 1, 'completed': 1, 'data': [
                {'markdown': raw, 'metadata': {'sourceURL': url, 'statusCode': 200}}]}
        def scrape(self, *args, **kwargs):
            raise AssertionError('unchanged page must not spend JSON extraction credits')
    report = p.run(SimpleNamespace(release=str(release), output=str(tmp_path / 'run'),
        state=str(state), concurrency=1, diligence=False, manual_registered=False), Client())
    assert report['failed_pages'] == 0
    assert report['json_extractions'] == 0
    assert report['price_coverage']['unknown_rows'] == 1
    assert report['cadence'] == 'weekly'
    assert json.loads((tmp_path / 'run/MONITOR-TARGETS.json').read_text())['requested_schedule'] == 'weekly Sunday 08:20 America/Los_Angeles'
    cron = ["CRON_TZ=America/Los_Angeles\n1 2 * * * unrelated-job\n20 * * * * python cron_bridge_action.py pricing_refresh --scheduled --cron '20 15 3 * *'\n"]
    monkeypatch.setattr(p.subprocess, 'check_output', lambda *a, **kw: cron[0])
    monkeypatch.setattr(p.subprocess, 'run', lambda *a, **kw: cron.__setitem__(0, kw['input']))
    installed = p.install_weekly()
    assert installed['line'].startswith('20 8 * * 0 ')
    assert "--cron '20 8 * * 0'" in installed['line']
    assert '1 2 * * * unrelated-job\n' in cron[0]
