"""F12 offline evidence joins; no receiver, collector, ledger or payment calls."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp_affiliate_weekly_reporting as weekly

IDENTITY = dict(network='cj', account_id='publisher-1', tenant_id='not_applicable', program_id='program-1')
REGISTRY = [{**IDENTITY, 'status': 'verified', 'tenant_kind': 'not_applicable',
             'provider_slug': 'fixture-provider', 'evidence_ref': 'fixture:typed-identity'}]


def fixture():
    conversion = {**IDENTITY, 'revenue_event_id': 'receipt-1', 'conversion_id': 'native-conversion-1',
                  'cs_click_id': 'outbound-1', 'event_type': 'accepted_conversion',
                  'event_status': 'accepted', 'classification': 'REAL', 'attributes_json': '{}'}
    provenance = {'receipt_sha256': 'a' * 64, 'receipt_pointer': '/records/0',
                  'authenticated': True, 'classification': 'REAL', 'attribution': 'GLP'}
    earnings = {**IDENTITY, **provenance, 'provider_event_id': 'commission-1', 'event_type': 'commission',
                'conversion_id': 'native-conversion-1', 'conversion_join_evidence_ref': 'fixture:native-conversion-mapping',
                'currency': 'USD', 'commission_minor': 2500, 'payout_id': 'payout-1',
                'payout_join_evidence_ref': 'fixture:transaction-membership'}
    payout = {**IDENTITY, **provenance, 'provider_event_id': 'payout-1', 'event_type': 'settlement',
              'mapped_status': 'paid', 'currency': 'USD', 'net_paid_minor': 9000}
    return {'schema': 'glp-f12-reconciliation-input-v1', 'evidence_ref': 'fixture:d1-export',
            'complete': False, 'conversions': [conversion], 'clicks': [],
            'financial_records': [earnings, payout]}


def reconcile(snapshot=None, registry=None, observations=None):
    return weekly.reconcile_conversions(snapshot, observations or [], REGISTRY if registry is None else registry)


def row(snapshot, **kwargs):
    return reconcile(snapshot, **kwargs)['rows'][0]


def test_existing_weekly_producer_exposes_missing_conversion_input_as_unknown(tmp_path):
    result = weekly.run(tmp_path / 'report', {'marketplace_count': 1, 'marketplaces': [{'marketplace': 'cj'}]},
                        REGISTRY, as_of=datetime(2026, 10, 2, tzinfo=timezone.utc))
    report = result['conversion_reconciliation']
    assert report['status'] == 'UNKNOWN'
    assert report['input_event_count'] is None
    assert report['commission_minor'] is report['cash_minor'] is None
    assert report['gaps'] == ['CONVERSION_SNAPSHOT_MISSING']
    assert json.loads((tmp_path / 'report/RESULT.json').read_text())['conversion_reconciliation'] == report


def test_exact_source_supported_chain_keeps_payout_total_separate_from_conversion_cash():
    result = reconcile(fixture())
    got = result['rows'][0]
    assert got['earnings_state'] == got['payout_state'] == 'JOINED'
    assert got['commission_minor'] == 2500
    assert got['payout_total_minor'] == 9000
    assert got['cash_minor'] is None  # membership proves no per-conversion allocation
    assert got['payout_amount_scope'] == 'WHOLE_SETTLEMENT_NOT_CONVERSION_CASH'
    assert result['coverage_complete'] is False
    assert result['commission_minor'] is result['cash_minor'] is None
    assert result['provenance'] == 'SUPPLIED_LOCAL_EVIDENCE_NOT_VERIFIED_PRODUCTION_DATA'
    assert result['input_sha256'] == weekly.digest(fixture())


@pytest.mark.parametrize('field', ['payout_id', 'payout_join_evidence_ref'])
def test_missing_settlement_membership_never_borrows_paid_status_or_total(field):
    data = fixture()
    data['financial_records'][0].pop(field)
    got = row(data)
    assert got['earnings_state'] == 'JOINED'
    assert got['commission_minor'] == 2500
    assert got['payout_state'] == 'UNKNOWN'
    assert got['payout_total_minor'] is got['cash_minor'] is None
    assert 'SETTLEMENT_MEMBERSHIP_MISSING' in got['gaps']


@pytest.mark.parametrize('field', ['conversion_id', 'conversion_join_evidence_ref', 'receipt_sha256', 'receipt_pointer'])
def test_missing_native_conversion_mapping_or_provenance_stays_unknown(field):
    data = fixture()
    data['financial_records'][0].pop(field)
    got = row(data)
    assert got['earnings_state'] == 'UNKNOWN'
    assert got['commission_minor'] is got['payout_total_minor'] is got['cash_minor'] is None


def test_conversion_id_is_never_implicitly_a_provider_earning_id():
    data = fixture()
    data['financial_records'][0].pop('conversion_id')
    data['conversions'][0]['conversion_id'] = 'commission-1'
    assert row(data)['earnings_state'] == 'UNKNOWN'


@pytest.mark.parametrize('case', ['missing_tenant', 'missing_account', 'unverified', 'duplicate', 'fella'])
def test_unresolved_or_ambiguous_typed_identity_refuses_financial_join(case):
    data, registry = fixture(), deepcopy(REGISTRY)
    if case == 'missing_tenant': data['conversions'][0].pop('tenant_id')
    if case == 'missing_account': data['conversions'][0].pop('account_id')
    if case == 'unverified': registry[0]['status'] = 'unresolved'
    if case == 'duplicate': registry.append(deepcopy(registry[0]))
    if case == 'fella': registry[0]['provider_slug'] = 'fella-health'
    got = row(data, registry=registry)
    assert got['earnings_state'] == 'UNKNOWN'
    assert got['commission_minor'] is None


@pytest.mark.parametrize('kind', ['commission', 'settlement'])
def test_ambiguous_financial_rows_never_choose_last_winner(kind):
    data = fixture()
    original = next(r for r in data['financial_records'] if r['event_type'] == kind)
    other = deepcopy(original)
    other['provider_event_id'] = 'other-commission' if kind == 'commission' else original['provider_event_id']
    other['commission_minor' if kind == 'commission' else 'net_paid_minor'] = 8000
    data['financial_records'].append(other)
    got = row(data)
    assert got['earnings_state' if kind == 'commission' else 'payout_state'] == 'UNKNOWN'
    assert got['commission_minor' if kind == 'commission' else 'payout_total_minor'] is None


def test_repeated_identical_weekly_observation_is_deduplicated_but_conflict_is_unknown():
    data = fixture()
    records = deepcopy(data.pop('financial_records'))
    observations = [{'records': records}, {'accounting_projection': deepcopy(records)}]
    assert row(data, observations=observations)['payout_state'] == 'JOINED'
    observations[1]['accounting_projection'][0]['commission_minor'] = 5000
    assert row(data, observations=observations)['earnings_state'] == 'UNKNOWN'


@pytest.mark.parametrize('value', [None, False, '2500'])
def test_missing_or_untyped_amount_never_becomes_zero(value):
    data = fixture()
    data['financial_records'][0]['commission_minor'] = value
    got = row(data)
    assert got['commission_minor'] is None
    assert 'EARNINGS_AMOUNT_UNKNOWN' in got['gaps']


def test_explicit_zero_is_retained_only_after_evidence_join():
    data = fixture()
    data['financial_records'][0]['commission_minor'] = 0
    assert row(data)['commission_minor'] == 0
    data['financial_records'][0].pop('conversion_join_evidence_ref')
    assert row(data)['commission_minor'] is None


def test_currency_conflict_does_not_publish_payout_amount():
    data = fixture()
    data['financial_records'][1]['currency'] = 'EUR'
    got = row(data)
    assert got['payout_state'] == 'UNKNOWN'
    assert got['payout_total_minor'] is None
    assert 'PAYOUT_CURRENCY_MISMATCH' in got['gaps']


def test_readback_shaped_selftests_and_exact_affiliati_test_are_excluded():
    data = fixture()
    data['conversions'] = [{**data['conversions'][0], 'revenue_event_id': f'selftest-{n}',
                            'network': '__receiver_selftest__'} for n in range(12)]
    data['conversions'] += [{**fixture()['conversions'][0], 'revenue_event_id': f'affiliati-{n}',
                             'network': 'affiliati', 'program_id': 'jrnys',
                             'cs_click_id': 'test-click'} for n in range(2)]
    data['clicks'] = [{'click_token': 'test-click', 'network': 'affiliati', 'program_id': 'jrnys', 'is_test': True}]
    result = reconcile(data)
    assert result['rows'] == []
    assert result['excluded_counts'] == {'SELFTEST': 12, 'TEST': 2, 'PROBE': 0}
    assert result['commission_minor'] is result['cash_minor'] is None


@pytest.mark.parametrize('field', ['network', 'program_id'])
def test_test_marker_never_crosses_conflicting_click_rail_or_program(field):
    data = fixture()
    click = {'click_token': 'outbound-1', 'network': 'cj', 'program_id': 'program-1', 'is_test': True}
    click[field] = 'another'
    data['clicks'] = [click]
    assert len(reconcile(data)['rows']) == 1
    assert reconcile(data)['excluded_counts']['TEST'] == 0


def test_duplicate_exact_clicks_and_conversion_receipts_remain_ambiguous():
    data = fixture()
    click = {'click_token': 'outbound-1', 'network': 'cj', 'program_id': 'program-1'}
    data['clicks'] = [click, {**click, 'is_test': False}]
    assert row(data)['earnings_state'] == 'UNKNOWN'
    data['clicks'] = []
    data['conversions'].append(deepcopy(data['conversions'][0]))
    assert all(r['earnings_state'] == 'UNKNOWN' for r in reconcile(data)['rows'])


def test_duplicate_native_conversion_identity_cannot_repeat_an_earning():
    data = fixture()
    other = deepcopy(data['conversions'][0])
    other['revenue_event_id'] = 'receipt-2'
    data['conversions'].append(other)
    assert all(r['commission_minor'] is None for r in reconcile(data)['rows'])


def test_known_test_payout_identifier_never_publishes_cash():
    from glp_network_revenue_v2 import TEST_ID
    data = fixture()
    data['financial_records'][0]['payout_id'] = TEST_ID
    data['financial_records'][1]['provider_event_id'] = TEST_ID
    assert row(data)['payout_total_minor'] is None


def test_malformed_conversion_identifier_is_unknown_without_crashing():
    data = fixture()
    data['conversions'][0]['revenue_event_id'] = {'untyped': 'id'}
    assert row(data)['earnings_state'] == 'UNKNOWN'


@pytest.mark.parametrize('marker', [{'is_test': True}, {'event_status': 'test'},
                                  {'is_test': '1'}, {'event_status': 'TEST'},
                                  {'attributes_json': '{"is_test":"true"}'},
                                  {'attributes_json': '{"is_probe":1}'}])
def test_explicit_noncommercial_marks_override_real_claim(marker):
    data = fixture()
    data['conversions'][0].update(marker)
    assert reconcile(data)['rows'] == []


def test_unmarked_or_malformed_classification_never_defaults_to_real():
    data = fixture()
    data['conversions'][0].pop('classification')
    assert row(data)['commission_minor'] is None
    data['conversions'][0]['attributes_json'] = 'not-json'
    assert 'CONVERSION_ATTRIBUTES_INVALID' in row(data)['gaps']


@pytest.mark.parametrize('field,value', [('conversion_join_evidence_ref', {'not': 'a reference'}),
                                        ('payout_join_evidence_ref', True)])
def test_untyped_mapping_reference_cannot_authorize_a_join(field, value):
    data = fixture()
    data['financial_records'][0][field] = value
    got = row(data)
    assert got['commission_minor' if field.startswith('conversion') else 'payout_total_minor'] is None


def test_accepted_click_is_not_a_conversion_even_with_matching_financial_ids():
    data = fixture()
    data['conversions'][0]['event_type'] = 'accepted_click'
    assert row(data)['commission_minor'] is None


def test_exact_financial_probe_is_counted_as_probe():
    data = fixture()
    data['financial_records'][0]['is_probe'] = True
    result = reconcile(data)
    assert result['rows'] == []
    assert result['excluded_counts'] == {'SELFTEST': 0, 'TEST': 0, 'PROBE': 1}


def test_affiliati_without_financial_collector_and_identity_remains_unknown():
    data = fixture()
    data['conversions'][0]['network'] = 'affiliati'
    data['financial_records'] = []
    observations = [{'network': 'affiliati', 'coverage': 'COMPLETE', 'records': [
        {'program_id': 'program-1', 'reported_amount': '25.00', 'classification': 'UNKNOWN'}]}]
    got = row(data, observations=observations)
    assert got['commission_minor'] is got['cash_minor'] is None
    assert 'TYPED_IDENTITY_UNRESOLVED_OR_AMBIGUOUS' in got['gaps']


def test_weekly_offline_cli_and_job_forward_local_evidence(tmp_path, monkeypatch):
    data = fixture()
    supplied = tmp_path / 'input.json'
    supplied.write_text(json.dumps(data))
    roster, registry, pins = [tmp_path / name for name in ('roster.json', 'registry.json', 'pins.json')]
    roster.write_text(json.dumps({'marketplace_count': 1, 'marketplaces': [{'marketplace': 'cj'}]}))
    registry.write_text(json.dumps({'schema': 'reporting-identity-registry-v1', 'identities': REGISTRY}))
    required = ['glp_affiliate_weekly_reporting_job.py', 'glp_affiliate_weekly_reporting.py',
                'glp_network_revenue_v2.py', 'glp_network_revenue_import_v2.py']
    pins.write_text(json.dumps({'files': {str(ROOT / 'scripts' / n): hashlib.sha256((ROOT / 'scripts' / n).read_bytes()).hexdigest()
                                        for n in required}}))
    import glp_affiliate_weekly_reporting_job as job
    assert job.main(['--source-manifest', str(pins), '--manifest-sha256', hashlib.sha256(pins.read_bytes()).hexdigest(),
                     '--roster', str(roster), '--identity-registry', str(registry), '--report-root', str(tmp_path / 'reports'),
                     '--reconciliation-input', str(supplied)]) == 0
    result_file, = (tmp_path / 'reports').glob('*/RESULT.json')
    result = json.loads(result_file.read_text())
    assert result['conversion_reconciliation']['rows'][0]['payout_state'] == 'JOINED'
    assert result['invocation'] == 'OFFLINE_DRY_RUN_NO_PROVIDER_IO'
