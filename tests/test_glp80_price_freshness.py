"""Full-field monitoring, same-source validation and held-provider regression."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import glp_price_freshness as p


def test_swapped_digits_are_changed_even_if_firecrawl_says_same():
    assert p.changed('$12/month', '$21/month', {'changeStatus': 'same'})
    assert not p.changed('$12/month', '$12/month', {'changeStatus': 'changed'})


def fixture():
    row = {'row_id': 'clinic:sema', 'medication_id': 'compounded-semaglutide-injection',
           'label': 'Compounded semaglutide injection', 'price_kind': 'floor_from',
           'facts': {'amount_usd': {'value': 12}, 'period': {'value': '/month'}}}
    quote = 'Compounded semaglutide injection starts at $21/month.'
    result = {'row_id': row['row_id'], 'medication_id': row['medication_id'],
              'price_kind': row['price_kind'], 'identity_quote': quote,
              'source_url': 'https://clinic.example/pricing',
              'fields': [{'field': 'amount_usd', 'value': 21, 'quote': quote},
                         {'field': 'period', 'value': '/month', 'quote': quote}]}
    return row, result, quote


def test_exact_same_source_complete_fields_are_confirmed():
    row, result, quote = fixture()
    assert p.review(row, result, quote, result['source_url']) == []


def test_unchanged_price_with_schema_nulls_is_unknown_evidence_not_price_review():
    row, result, quote = fixture()
    result['fields'][0]['value'] = 12
    result['identity_quote'] = None
    result['fields'][1]['value'] = None
    verdict = p.classify_price_observation(row, result, quote, result['source_url'])
    assert verdict['status'] == 'NO_PRICE_CHANGE_DETECTED'
    assert verdict['review_required'] is False
    assert verdict['validation_errors']


def test_month_alias_and_exact_medication_line_are_proven_not_null_or_fake_quote():
    row, result, quote = fixture()
    raw = '[Compounded Semaglutide injection · From $21/mo](https://clinic.example/pricing)'
    result['identity_quote'] = 'From $21/mo'
    result['fields'][0]['quote'] = 'From $21/mo'
    result['fields'][1].update(value='monthly', quote='From $21/mo')
    verdict = p.classify_price_observation(row, result, raw, result['source_url'])
    assert verdict['status'] == 'CONFIRMED_CHANGE'
    assert verdict['proposal']['fields'][1]['value'] == '/month'
    assert verdict['proposal']['fields'][0]['quote'] in raw
    assert 'Semaglutide' in verdict['proposal']['fields'][0]['quote']


def test_unsupported_number_on_another_drug_is_not_a_real_change():
    row, result, quote = fixture()
    result['fields'][0]['quote'] = 'Tirzepatide $21/month'
    verdict = p.classify_price_observation(row, result, 'Tirzepatide $21/month', result['source_url'])
    assert verdict['status'] == 'UNSUPPORTED_EXTRACTION'
    assert verdict['review_required'] is False


def test_real_bound_price_delta_with_unknown_billing_terms_needs_review():
    row, result, quote = fixture()
    result['fields'][1]['value'] = None
    verdict = p.classify_price_observation(row, result, quote, result['source_url'])
    assert verdict['status'] == 'PRICE_CHANGE_REVIEW_REQUIRED'
    assert verdict['review_required'] is True


def test_injection_price_does_not_price_unpriced_oral_alternative():
    quote = 'Compounded semaglutide as a weekly injection from $89/month or a daily tablet.'
    inj = {'medication_id': 'compounded-semaglutide-injection'}
    oral = {'medication_id': 'compounded-semaglutide-oral'}
    assert p.amount_route_supported(inj, 89, quote)
    assert not p.amount_route_supported(oral, 89, quote)


def test_truncated_source_attested_supply_wording_is_not_real_movement():
    row, result, _ = fixture()
    quote = 'Compounded semaglutide injection includes 12 weeks of medication for $12/month.'
    row['facts']['supply_period'] = {'value': '12 weeks of medication', 'evidence': []}
    result['fields'] = [{'field': 'amount_usd', 'value': 12, 'quote': quote},
                        {'field': 'period', 'value': '/month', 'quote': quote},
                        {'field': 'supply_period', 'value': '12 weeks', 'quote': quote}]
    result['identity_quote'] = quote
    verdict = p.classify_price_observation(row, result, quote, result['source_url'])
    assert verdict['status'] == 'NO_PRICE_CHANGE_DETECTED'
    assert verdict['changed_fields'] == []


def test_same_amount_but_changed_billing_period_requires_price_review():
    row, result, _ = fixture()
    quote = 'Compounded semaglutide injection is $12 every 4 weeks.'
    result['identity_quote'] = quote
    result['fields'] = [{'field': 'amount_usd', 'value': 12, 'quote': quote},
                        {'field': 'period', 'value': 'every 4 weeks', 'quote': quote}]
    verdict = p.classify_price_observation(row, result, quote, result['source_url'])
    assert verdict['status'] == 'PRICE_CHANGE_REVIEW_REQUIRED'
    assert verdict['changed_fields'] == ['period']


def test_other_plan_monthly_average_does_not_relabel_four_week_renewal():
    quote = 'Compounded semaglutide is $139 first month, then $199 every 4 weeks, or $139/mo average on annual plan.'
    fields = [{'field': 'ongoing_usd', 'value': 199}]
    assert not p.period_at_amount('ongoing_period', '/month', quote, fields)
    assert p.period_at_amount('ongoing_period', 'every 4 weeks', quote, fields)


@pytest.mark.parametrize('mutate', [
    lambda x: x['fields'][0].update(value=12),
    lambda x: x.update(source_url='https://other.example/pricing'),
    lambda x: x.update(medication_id='compounded-tirzepatide-injection'),
    lambda x: x['fields'].pop(),
    lambda x: x['fields'].append(copy.deepcopy(x['fields'][0])),
    lambda x: x.update(price_kind='monthly_cash'),
    lambda x: x.update(identity_quote='not in the source'),
    lambda x: x['fields'][0].update(value=0),
    lambda x: x['fields'][0].update(value=True),
    lambda x: x['fields'][0].update(value=None),
])
def test_incomplete_conflicting_unknown_and_fabricated_fields_refuse(mutate):
    row, result, quote = fixture()
    mutate(result)
    assert p.review(row, result, quote, result['source_url'] if 'other' not in result['source_url'] else 'https://clinic.example/pricing')


def test_medication_context_is_not_just_a_number_somewhere_on_page():
    row, result, quote = fixture()
    result['identity_quote'] = 'Compounded semaglutide injection'
    result['fields'][0]['quote'] = 'Tirzepatide $21/month.'
    assert p.review(row, result, 'Compounded semaglutide injection\nTirzepatide $21/month.', result['source_url'])


def test_selects_all_active_us_and_preserves_holds_fella_aliases():
    prof = {'provider': 'Clinic', 'source_urls': ['https://clinic.example/pricing'],
            'calculator': {'source_url': 'https://clinic.example/pricing'}, 'registry_program_ids': ['a']}
    reg = {'provider_dynamic_profiles': {'profiles': {'a': prof, 'held': prof, 'fella-health': prof}},
           'programs': [{'program_id': 'a', 'commercial_state': 'live'},
                        {'program_id': 'held', 'commercial_state': 'held'},
                        {'program_id': 'fella-health', 'commercial_state': 'live'}]}
    active, holds = p.providers(reg, {'providers': {}}, {})
    assert list(active) == ['a']
    assert 'held' in holds
    assert all('fella' not in x for x in active)


def test_unknown_fields_never_become_zero_or_remove_attested_row():
    row, result, quote = fixture()
    original = copy.deepcopy(row)
    result['fields'][0]['value'] = None
    assert p.review(row, result, quote, result['source_url'])
    assert row == original


def test_keyed_schema_cannot_assign_injection_offer_to_oral_row():
    row, _, _ = fixture()
    item = p.extraction_schema([row])['properties']['row_observations']
    assert item['additionalProperties'] is False
    assert item['required'] == [row['row_id']]
    assert item['properties'][row['row_id']]['properties']['medication_id']['const'] == row['medication_id']


def test_explicit_engine_refusal_recovers_once_and_retains_failure_metadata():
    import io
    import urllib.error
    class Client:
        calls = []
        def scrape(self, payload, **kwargs):
            self.calls.append(payload)
            if len(self.calls) == 1:
                raise urllib.error.HTTPError(payload['url'], 500, 'engines', {}, io.BytesIO(b'{"code":"SCRAPE_ALL_ENGINES_FAILED"}'))
            return {'success': True, 'data': {'markdown': 'actual scraped source'}}
    client = Client()
    result = p.scrape(client, 'https://clinic.example/pricing', ['markdown'])
    assert len(client.calls) == 2
    assert client.calls[1]['proxy'] == 'enhanced'
    assert result['transport_recovery']['first_http_status'] == 500
    assert result['data']['markdown'] == 'actual scraped source'


def test_persistent_engine_failure_is_not_success_or_unbounded_retry():
    import io
    import urllib.error
    class Client:
        calls = 0
        def scrape(self, payload, **kwargs):
            self.calls += 1
            raise urllib.error.HTTPError(payload['url'], 500, 'engines', {}, io.BytesIO(b'{"code":"SCRAPE_ALL_ENGINES_FAILED"}'))
    client = Client()
    with pytest.raises(urllib.error.HTTPError):
        p.scrape(client, 'https://clinic.example/pricing', ['markdown'])
    assert client.calls == 2


def test_completed_malformed_extraction_retried_once_with_both_raw_receipts_retained(tmp_path):
    row, result, quote = fixture()
    url = result['source_url']
    class Client:
        calls = 0
        def scrape(self, payload, **kwargs):
            self.calls += 1
            content = {'rows': [result, result]} if self.calls == 1 else {'row_observations': {row['row_id']: result}}
            return {'success': True, 'data': {'markdown': quote * 3, 'metadata': {'sourceURL': url}, 'json': content}}
    client = Client()
    _, data = p.extract(client, 'a', 'A', url, [row], tmp_path / 'source')
    assert client.calls == 2
    assert data['extraction_attempts'] == 2
    assert data['json']['rows'] == [result]
    assert (tmp_path / 'source-attempt-1.json').exists()
    assert (tmp_path / 'source-attempt-1-refusal.json').exists()
    assert (tmp_path / 'source-attempt-2.json').exists()


def test_persistent_schema_failure_remains_failure_after_two_attempts():
    row, result, quote = fixture()
    class Client:
        calls = 0
        def scrape(self, payload, **kwargs):
            self.calls += 1
            return {'success': True, 'data': {'markdown': quote * 3, 'json': {'rows': [result, result]}}}
    client = Client()
    with pytest.raises(ValueError, match='structured_row_key_coverage_mismatch'):
        p.extract(client, 'a', 'A', result['source_url'], [row])
    assert client.calls == 2


def test_transport_timeout_is_not_blindly_replayed():
    row, result, _ = fixture()
    class Client:
        calls = 0
        def scrape(self, payload, **kwargs):
            self.calls += 1
            raise TimeoutError('ambiguous transport')
    client = Client()
    with pytest.raises(TimeoutError):
        p.extract(client, 'a', 'A', result['source_url'], [row])
    assert client.calls == 1


def test_schema_requires_nullable_fields_quotes_urls_and_no_extra_fields():
    row, _, _ = fixture()
    schema = p.extraction_schema([row])
    props = schema['properties']['row_observations']['properties'][row['row_id']]['properties']
    assert props['source_url']['type'] == 'string'
    assert props['fields']['items']['properties']['value']['type'] == ['number', 'string', 'null']
    assert props['fields']['items']['additionalProperties'] is False


def test_adoption_uses_existing_writer_and_refuses_source_drift(tmp_path):
    fixtures = Path(__file__).resolve().parent / 'fixtures/glp344-price-evidence'
    import shutil
    shutil.copytree(fixtures, tmp_path / 'capture')
    root = tmp_path / 'capture'
    delta = p.load_delta()
    claims = json.loads((root / 'claims.json').read_text())
    branch = delta.monthly_branch('example-telehealth', '2026-10', '2026-10-03T00:00:00+00:00', claims, root / 'provider-pricing-matrix.json', root)
    assert p.adopt_matrix(branch, claims, root / 'provider-pricing-matrix.json', root,
                          '2026-10-03T00:00:00+00:00', [])['providers']['example-telehealth']['medications'][0]['facts']['amount_usd']['value'] == 249
    (root / 'provider-pricing-matrix.json').write_text('{}')
    with pytest.raises(ValueError, match='stale delta'):
        p.adopt_matrix(branch, claims, root / 'provider-pricing-matrix.json', root,
                       '2026-10-03T00:00:00+00:00', [])


def test_stage_candidate_exercises_writer_and_witness_handoff(tmp_path, monkeypatch):
    from types import SimpleNamespace
    row, extracted, raw = fixture()
    for fact in row['facts'].values():
        fact['evidence'] = []
    candidate, run = tmp_path / 'candidate', tmp_path / 'run'
    folder = run / 'clinic'
    folder.mkdir(parents=True)
    registry = {'programs': [{'program_id': 'clinic', 'commercial_state': 'live'}],
                'provider_dynamic_profiles': {'profiles': {'clinic': {'provider': 'Clinic',
                    'registry_program_ids': ['clinic'], 'source_urls': [extracted['source_url']]}}}}
    matrix = {'providers': {'clinic': {'medications': [row]}}}
    p.put(candidate / 'data/program-registry.json', registry)
    p.put(candidate / 'data/provider-pricing-matrix.json', matrix)
    p.put(candidate / 'data/affiliate-activation-selection.json', {})
    captured = p.utc()
    (folder / 'source.md').write_text(raw)
    p.put(folder / 'source-normalized-extraction.json', {'rows': [extracted]})
    cap = {'source_url': extracted['source_url'], 'content_sha256': p.digest(raw),
           'captured_at_utc': captured, 'markdown_path': 'source.md',
           'forced_fresh': True, 'status': 'CAPTURED', 'status_code': 200, 'usable_evidence': True}
    delta = p.load_delta()
    claim = {**{k: cap[k] for k in ('source_url', 'content_sha256', 'captured_at_utc')},
             'kind': 'price_component', 'row_id': row['row_id'], 'field': 'amount_usd',
             'value': 21, 'quote': raw, 'capture_key': 'source'}
    doc = {'schema': delta.CLAIMS_SCHEMA, 'program_id': 'clinic', 'first_party_hosts': ['clinic.example'],
           'captures': {'source': cap}, 'claims': [claim]}
    as_of = p.utc()
    branch = delta.monthly_branch('clinic', as_of[:7], as_of, doc,
                                   candidate / 'data/provider-pricing-matrix.json', folder)
    assert branch['acceptance']['disposition'] == 'ACCEPTED'
    p.put(folder / 'REGISTRY-BRANCH.json', branch)
    p.put(folder / 'CLAIMS.json', doc)
    p.put(run / 'RESULT.json', {'finished_at_utc': as_of, 'active_provider_count': 1,
        'release_root': str(tmp_path / 'live'), 'providers': [{'program_id': 'clinic'}],
        'registry_sha256': p.digest((candidate / 'data/program-registry.json').read_bytes()),
        'matrix_sha256': p.digest((candidate / 'data/provider-pricing-matrix.json').read_bytes())})
    # Only the external CLI boundary is stubbed; the real F04 writer/review/CAS run.
    monkeypatch.setattr(p.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='{}', stderr=''))
    receipt = p.stage_candidate(run, candidate)
    assert receipt['status'] == 'STAGED_VALIDATED'
    assert receipt['rows'] == 1
    assert p.read(candidate / 'data/provider-pricing-matrix.json')['providers']['clinic']['medications'][0]['facts']['amount_usd']['value'] == 21
    witness = p.read(candidate / 'data/price-refresh-ledger.json')['entries'][0]
    assert witness['before'] == row
    assert (candidate / witness['markdown_path']).read_text() == raw
    assert receipt['publication'] == 'NOT_PUBLISHED'


@pytest.mark.parametrize('primary,completed,failed', [(0, 2, 0), (2, 1, 0), (2, 2, 1)])
def test_bridge_never_counts_incomplete_price_monitor_as_live(primary, completed, failed, monkeypatch, tmp_path):
    import cron_bridge_action as bridge
    row = {'schema': p.SCHEMA, 'active_provider_count': 2,
           'completed_provider_count': completed, 'primary_pages_successful': primary,
           'successful_pages': 3, 'failed_primary_pages': failed, 'receipt': 'fixture'}
    monkeypatch.setattr(bridge, 'machine_receipt', lambda *a, **kw: row)
    with pytest.raises(bridge.TerminalRefusal, match='price_monitor_cohort_incomplete'):
        bridge.finish_action('pricing_refresh', {}, {}, {}, tmp_path / 'state')


def test_auxiliary_source_failure_cannot_emit_a_success_heartbeat(monkeypatch, tmp_path):
    import cron_bridge_action as bridge
    row = {'schema': p.SCHEMA, 'active_provider_count': 2, 'completed_provider_count': 2,
           'primary_pages_successful': 2, 'successful_pages': 3, 'failed_pages': 1,
           'failed_primary_pages': 0, 'confirmed_changes': 0, 'review_required': 0, 'receipt': 'fixture'}
    monkeypatch.setattr(bridge, 'machine_receipt', lambda *a, **kw: row)
    with pytest.raises(bridge.TerminalRefusal, match='price_monitor_cohort_incomplete'):
        bridge.finish_action('pricing_refresh', {}, {}, {}, tmp_path / 'state')


def test_bridge_reports_monitoring_not_publication(monkeypatch, tmp_path):
    import cron_bridge_action as bridge
    row = {'schema': p.SCHEMA, 'active_provider_count': 2, 'completed_provider_count': 2,
           'primary_pages_successful': 2, 'successful_pages': 3, 'failed_pages': 0, 'failed_primary_pages': 0,
           'confirmed_changes': 0, 'review_required': 4, 'receipt': 'fixture'}
    monkeypatch.setattr(bridge, 'machine_receipt', lambda *a, **kw: row)
    calls = []
    monkeypatch.setattr(bridge, 'heartbeat', lambda *a: calls.append(a) or {'status': 'fixture'})
    effects = bridge.finish_action('pricing_refresh', {}, {}, {}, tmp_path / 'state')
    assert effects['providers_monitored'] == 2
    assert 'notification' not in effects
    assert 'publication is a separate F06 effect' in calls[0][3]

