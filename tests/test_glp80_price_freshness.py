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
    assert verdict['status'] == 'EVIDENCE_UNKNOWN'
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


@pytest.mark.parametrize('kind,oldvalues,newvalues,raw', [
    ('intro_then_ongoing', {'intro_usd': 99, 'intro_period': 'month', 'ongoing_usd': 249, 'ongoing_period': 'month'},
     {'intro_usd': 249, 'intro_period': 'month', 'ongoing_usd': 99, 'ongoing_period': 'month'},
     'Compounded semaglutide injection Standard plan: first month $99/month, then $249/month ongoing.'),
    ('floor_from', {'amount_usd': 249, 'period': '/month'}, {'amount_usd': 99, 'period': '/month'},
     'Compounded semaglutide injection: Standard plan starts from $249/month; annual prepaid plan starts from $99/month, billed $1188 upfront.'),
    ('floor_from', {'amount_usd': 249, 'period': '/month'}, {'amount_usd': 99, 'period': '/month'},
     'Compounded semaglutide injection Standard plan starts from $99/month for the first month only, then $249/month.'),
])
def test_exact_amount_is_not_proof_of_plan_phase_or_new_conditions(kind, oldvalues, newvalues, raw):
    row, proposed, _ = fixture()
    row.update(price_kind=kind, plan_label='Standard plan', qualifiers=[],
               facts={k: {'value': v} for k, v in oldvalues.items()})
    proposed.update(price_kind=kind, identity_quote=raw, plan_label='Standard plan',
                    plan_quote=raw, qualifiers=[], conditions_quote=raw,
                    fields=[{'field': k, 'value': v, 'quote': raw} for k, v in newvalues.items()])
    verdict = p.classify_price_observation(row, proposed, raw, proposed['source_url'])
    assert verdict['validation_errors']
    assert verdict['status'] != 'CONFIRMED_CHANGE'


def test_native_editorial_labels_and_cautions_do_not_need_fake_source_text():
    row, proposal, raw = fixture()
    row.update(plan_label='Reported S32 offer: Compounded semaglutide (injection)',
               qualifiers=['Advertised starting price; selected checkout charge UNKNOWN.'])
    old_raw = raw.replace('$21', '$12')
    for fact in row['facts'].values():
        fact['evidence'] = [{'quote': old_raw}]
    proposal.update(plan_label=row['plan_label'], plan_quote=raw,
                    qualifiers=row['qualifiers'], conditions_quote=raw)
    assert not p.review(row, proposal, raw, proposal['source_url'])
    assert p.classify_price_observation(row, proposal, raw, proposal['source_url'])['status'] == 'CONFIRMED_CHANGE'
    changed_conditions = raw + ' Rate limited to new accounts with a twelve-week commitment.'
    proposal['conditions_quote'] = changed_conditions
    assert 'new_or_ambiguous_conditions' in p.review(row, proposal, changed_conditions, proposal['source_url'])


def test_omitted_multiline_intro_and_known_unknown_are_not_full_validation():
    row, proposal, raw = fixture()
    row.update(plan_label='Standard plan', qualifiers=[])
    raw = raw.replace('injection starts', 'injection Standard plan starts')
    proposal.update(identity_quote=raw, plan_label=row['plan_label'], plan_quote=raw,
                    qualifiers=[], conditions_quote=raw)
    for field in proposal['fields']:
        field['quote'] = raw
    assert p.review(row, proposal, raw + '\nFor the first month only.', proposal['source_url'])
    row['facts']['billing_period'] = {'value': 'UNKNOWN'}
    proposal['fields'].append({'field': 'billing_period', 'value': None, 'quote': None})
    assert 'extracted_fact_unknown:billing_period' in p.review(row, proposal, raw, proposal['source_url'])


def test_run_reports_source_complete_but_unknown_prices_without_changing_facts(tmp_path):
    from types import SimpleNamespace
    row, extracted, raw = fixture()
    raw += '\n\nConfined synthetic source-monitor test; this is not a live provider or publication receipt.'
    row.update(plan_label='Standard plan', qualifiers=[])
    for field in extracted['fields']:
        field.update(value=None, quote=None)
    extracted.update(plan_label='Standard plan', plan_quote=None, qualifiers=[], conditions_quote=None)
    release = tmp_path / 'release'
    p.put(release / 'data/program-registry.json', {'programs': [
        {'program_id': 'clinic', 'provider': 'Clinic', 'commercial_state': 'live',
         'official_base_url': extracted['source_url'], 'service_markets': ['US']}],
        'provider_dynamic_profiles': {'profiles': {'clinic': {'provider': 'Clinic',
            'official_base_url': extracted['source_url'], 'source_urls': [extracted['source_url']]}}}})
    matrix = {'providers': {'clinic': {'medications': [row, {'medication_id': 'legacy'}]}}}
    p.put(release / 'data/provider-pricing-matrix.json', matrix)
    p.put(release / 'data/affiliate-activation-selection.json', {})
    before = (release / 'data/provider-pricing-matrix.json').read_bytes()
    class Client:
        def scrape(self, payload, **kwargs):
            data = {'markdown': raw, 'metadata': {'statusCode': 200, 'sourceURL': payload['url']}}
            if any(isinstance(f, dict) and f.get('type') == 'json' for f in payload['formats']):
                data['json'] = {'offers': [], 'uncertainties': [], 'row_observations': {row['row_id']: extracted}}
            return {'success': True, 'data': data}
    report = p.run(SimpleNamespace(release=str(release), output=str(tmp_path / 'report'),
        state=str(tmp_path / 'state'), manual_registered=False,
        concurrency=1, diligence=False), Client())
    assert report['status'] == 'SOURCE_MONITOR_COMPLETE'
    assert report['price_validation_status'] == 'PRICE_VALIDATION_INCOMPLETE'
    assert report['price_coverage']['fully_validated_rows'] == 0
    assert report['price_coverage']['unknown_rows'] == 1
    assert report['price_coverage']['legacy_rows_skipped'] == 1
    assert report['prices_unchanged'] is None
    assert report['confirmed_changes'] == 0
    assert (release / 'data/provider-pricing-matrix.json').read_bytes() == before


def test_price_coverage_never_infers_unchanged_from_complete_fetches():
    row, _, _ = fixture()
    active = {'clinic': {'rows': [row, {'medication_id': 'legacy'}]}, 'no-rows': {'rows': []}}
    result = p.price_coverage(active, [{'program_id': 'clinic', 'fully_validated_row_ids': [],
                                      'cached_validated_row_ids': [], 'evidence_unknown_row_ids': [row['row_id']]}])
    assert result['status'] == 'PRICE_VALIDATION_INCOMPLETE'
    assert result['typed_rows'] == 1
    assert result['fully_validated_rows'] == 0
    assert result['unknown_rows'] == 1
    assert result['legacy_rows_skipped'] == 1
    assert result['providers_without_price_rows'] == 1


def test_price_coverage_requires_full_field_support_and_counts_unique_rows():
    row, _, _ = fixture()
    active = {'clinic': {'rows': [row]}}
    results = [{'program_id': 'clinic', 'fully_validated_row_ids': [row['row_id'], row['row_id']],
                'cached_validated_row_ids': [], 'evidence_unknown_row_ids': [row['row_id']]}]
    coverage = p.price_coverage(active, results)
    assert coverage['fully_validated_rows'] == 1
    assert coverage['unknown_rows'] == 0
    assert coverage['status'] == 'PRICE_VALIDATION_COMPLETE'
    assert 'prices_unchanged' not in coverage
    results[0]['conflicting_source_rows'] = [row['row_id']]
    coverage = p.price_coverage(active, results)
    assert coverage['fully_validated_rows'] == 0
    assert coverage['unknown_rows'] == 1
    assert coverage['status'] == 'PRICE_VALIDATION_INCOMPLETE'


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


def test_stage_candidate_exercises_writer_and_witness_handoff(tmp_path):
    import os
    site = Path(os.environ.get('GLP80_SITE_SOURCE', ''))
    if not (site / 'src/pricing-evidence-v2.mjs').is_file():
        pytest.skip('paired-repo native acceptance requires GLP80_SITE_SOURCE')
    row, extracted, raw = fixture()
    row['row_id'] = 'clinic:s32-0:compounded-semaglutide-injection'
    extracted['row_id'] = row['row_id']
    row.update(plan_label='Standard plan', calculator_eligibility='display_only',
               qualifiers=[], unknown_components=['Eligibility requires clinical review'])
    raw = 'Compounded semaglutide injection Standard plan starts from $21/month.'
    extracted.update(plan_label='Standard plan', plan_quote=raw, qualifiers=[], conditions_quote=raw, identity_quote=raw)
    for field in extracted['fields']:
        field['quote'] = raw
    old_raw = raw.replace('$21', '$12')
    old_sha = p.digest(old_raw)
    old_witness = {'source_url': extracted['source_url'], 'capture_sha256': old_sha,
                   'capture_path': 'data/pricing-evidence/' + old_sha + '.md',
                   'captured_at_utc': '2026-10-06T00:00:00Z', 'quote': old_raw}
    row.update(evidence=[old_witness], identity_evidence=[old_witness])
    for fact in row['facts'].values():
        fact.update(evidence=[old_witness], artifact_needed=None)
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
    # Confined real Node subprocess; execute the actual paired native row gate
    # and receiver, not a stub of subprocess.run or a fabricated PASS response.
    scripts = candidate / 'scripts'
    scripts.mkdir()
    native = (site / 'src/pricing-evidence-v2.mjs').resolve().as_uri()
    receiver = (site / 'src/price-refresh-evidence.mjs').resolve().as_uri()
    renderer = (site / 'src/typed-price-labels.mjs').resolve().as_uri()
    surfaces = (site / 'tests/helpers/price-refresh-surfaces.mjs').resolve().as_uri()
    (candidate / old_witness['capture_path']).parent.mkdir(parents=True)
    (candidate / old_witness['capture_path']).write_text(old_raw)
    (scripts / 'validate-provider-pricing-matrix.mjs').write_text(
        "import {readFileSync} from 'node:fs';\n"
        f"import {{validatePriceRow}} from {json.dumps(native)};\n"
        f"import {{applyPriceRefreshEvidence}} from {json.dumps(receiver)};\n"
        f"import {{typedPriceText,renderTypedPrice}} from {json.dumps(renderer)};\n"
        f"import {{provePriceSurfaces}} from {json.dumps(surfaces)};\n"
        "const root=process.cwd();\n"
        "const matrix=JSON.parse(readFileSync(root+'/data/provider-pricing-matrix.json'));\n"
        "const ledger=JSON.parse(readFileSync(root+'/data/price-refresh-ledger.json'));\n"
        "const row=matrix.providers.clinic.medications[0];\n"
        "const errors=validatePriceRow(row,{root});\n"
        "if(errors.length)throw new Error(JSON.stringify(errors));\n"
        "const result=applyPriceRefreshEvidence(root,{clinic:{medications:[ledger.entries[0].before]}});\n"
        "if(JSON.stringify(result.clinic.medications[0])!==JSON.stringify(row))throw new Error('receiver mismatch');\n"
        "const surfaces=provePriceSurfaces(row,root+'/rendered-surfaces');\n"
        "console.log(JSON.stringify({native_errors:errors,price_text:typedPriceText(row),html:renderTypedPrice(row),surfaces}));\n")
    receipt = p.stage_candidate(run, candidate)
    assert receipt['status'] == 'STAGED_VALIDATED'
    assert receipt['rows'] == 1
    rendered = json.loads(receipt['stdout'])
    assert rendered['native_errors'] == []
    assert len(rendered['surfaces']['surfaces']) == 10
    assert rendered['surfaces']['structured_data'] == {
        'offers': 0, 'products': 0, 'source': 'actual provider-detail renderer'}
    assert rendered['surfaces']['numeric_projection'] == 'NOT_PROMOTED'
    assert '$21' in rendered['price_text']
    assert 'Standard plan' in rendered['html']
    assert '$12' not in rendered['price_text']
    after = p.read(candidate / 'data/provider-pricing-matrix.json')['providers']['clinic']['medications'][0]
    assert after['facts']['amount_usd']['value'] == 21
    assert after['facts']['amount_usd']['artifact_needed'] is None
    evidence = after['facts']['amount_usd']['evidence'][0]
    assert evidence['capture_sha256'] == p.digest(raw)
    assert (candidate / evidence['capture_path']).read_text() == raw
    assert set(evidence) == {'capture_path', 'capture_sha256', 'source_url', 'captured_at_utc', 'quote'}
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
           'confirmed_changes': 0, 'review_required': 4, 'receipt': 'fixture',
           'source_monitor_status': 'SOURCE_MONITOR_COMPLETE',
           'price_coverage': {'status': 'PRICE_VALIDATION_INCOMPLETE', 'typed_rows': 61,
                              'fully_validated_rows': 0, 'unknown_rows': 61, 'legacy_rows_skipped': 11}}
    monkeypatch.setattr(bridge, 'machine_receipt', lambda *a, **kw: row)
    calls = []
    monkeypatch.setattr(bridge, 'heartbeat', lambda *a: calls.append(a) or {'status': 'fixture'})
    effects = bridge.finish_action('pricing_refresh', {}, {}, {}, tmp_path / 'state')
    assert effects['providers_monitored'] == 2
    assert 'notification' not in effects
    assert 'publication is a separate F06 effect' in calls[0][3]


def test_hash_skip_uses_batch_bytes_not_second_json_scrape_bytes():
    raw = 'Compounded semaglutide injection $99/mo.'
    cache = {'markdown': 'Different JSON response formatting', 'monitor_sha256': p.digest(raw)}
    assert not p.monitor_changed(cache, raw)
    assert p.monitor_changed(cache, raw.replace('$99', '$109'))
    assert not p.monitor_changed({'markdown': raw}, raw)

