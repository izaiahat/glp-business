"""Offline regressions; import edited source and stub only external writer boundaries."""
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def machine(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    monkeypatch.syspath_prepend(str(ROOT / 'ops'))
    writer = ModuleType('affiliate_sheet_batch_writer')
    writer.HEADERS = [''] * 27
    writer.pad = lambda row: list(row) + [''] * (27 - len(row))
    monkeypatch.setitem(sys.modules, 'affiliate_sheet_batch_writer', writer)
    monkeypatch.setitem(sys.modules, 'sheet_drain_invoke', ModuleType('sheet_drain_invoke'))
    for name, path in [('affiliate_payout_policy', ROOT / 'scripts/affiliate_payout_policy.py'),
                       ('best_terms_recovery_subject', ROOT / 'scripts/affiliate_best_terms_machine.py')]:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    for name in ('LATEST_MATRIX', 'LATEST_RUN', 'APPROVAL_CURSOR', 'FAILURE_STATE', 'CLINIC_FITS', 'PAYOUT_COMPARISONS'):
        monkeypatch.setattr(module, name, tmp_path / f'{name}.json')
    monkeypatch.setattr(module, 'RUNS_ROOT', tmp_path / 'runs')
    monkeypatch.setattr(module, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(module, 'read_live_sheet', lambda: [writer.HEADERS])
    module.offline_test_original_sheet_offers = module.sheet_offers
    module.offline_test_original_inventory_programs = module.inventory_programs
    monkeypatch.setattr(module, 'sheet_offers', lambda _: [])
    module.offline_test_original_application_ledger_offers = module.application_ledger_offers
    monkeypatch.setattr(module, 'application_ledger_offers', lambda: [])
    module.offline_test_original_inventory_programs = module.inventory_programs
    monkeypatch.setattr(module, 'inventory_programs', lambda _: [])
    module.offline_test_original_performance_loader = module.load_performance_rows
    monkeypatch.setattr(module, 'load_performance_rows', lambda: [])
    return module


def test_pro_f5_unknown_currency_stays_unknown_and_cannot_compete(machine, tmp_path):
    from scripts import affiliate_payout_policy as policy
    source = {'name': 'Fixture Clinic', 'network': 'impact', 'program_id': 'rail',
              'relationship': 'approved', 'wire_in_ready': True, 'traffic_collectible': True,
              'headline_payout': 500, 'payout_model': 'CPL', 'paid_event': 'qualified lead'}
    def normalize(currency_marker):
        (tmp_path / 'impact.json').write_text(json.dumps({
            'network': 'impact', 'programs': [{**source, **currency_marker}]}))
        return machine.offline_test_original_inventory_programs(tmp_path)[0]
    for fields in ({}, {'currency': None}, {'currency': ''}, {'currency': '  '}):
        unknown = normalize(fields)
        assert unknown['currency'] is None
        eligibility = policy.collectible_eligibility(unknown)
        assert not eligibility['eligible'] and 'currency_unavailable' in eligibility['reasons']
        unknown['wire_in_ready'] = True
        usd = normalize({'currency': ' usd '})
        usd.update(network='cj', program_id='usd', headline_payout=50, wire_in_ready=True)
        assert usd['currency'] == 'USD'
        result = machine.selection_with_offers([unknown, usd], [])
        assert result['providers'][0]['selected_winner']['program_id'] == 'usd'
        table = result['providers'][0]['cross_rail_comparisons']
        assert not table[0]['comparable']
        assert not policy._comparable(unknown, usd)
    for currency in ('USD', 'GBP', 'CAD'):
        assert normalize({'currency': currency})['currency'] == currency


def test_held_xentraffic_lock_no_longer_aborts_eligible_terms(machine, monkeypatch, tmp_path):
    failure = json.loads((ROOT / 'tests/fixtures/best_terms/held_xentraffic_failure.json').read_text())
    monkeypatch.setattr(machine, 'approval_receipts_since_cursor', lambda *a, **k: [{'path': 'fixture', 'payload': {'row_number': 2}}])
    monkeypatch.setattr(machine, 'approval_affected_providers', lambda *a: set())
    monkeypatch.setattr(machine, 'approval_lock_held', lambda: True)
    def forbidden(*args):
        raise RuntimeError(failure['error'])
    monkeypatch.setattr(machine, 'run_xentraffic_census', forbidden)
    result = machine.run_machine(first_run=False, approval_events=True, stage=False, census_dir=tmp_path)
    assert result['census']['networks']['xentraffic']['completeness'] == 'EXCLUDED_OPERATOR_HOLD'
    assert result['effects']['live_link_changes'] == 0
    assert machine.LATEST_MATRIX.exists()


def test_held_and_excluded_offers_cannot_enter_selection(machine):
    records = [{'provider_name': name, 'network': network, 'program_id': name,
                'headline_payout': None, 'relationship': 'approved', 'wire_in_ready': True}
               for name, network in [('Allowed Clinic', 'impact'), ('Allowed Clinic', 'xentraffic'),
                                     ('Telos Health', 'impact'), ('HealthRX', 'direct'), ('Fella Health', 'cj')]]
    result = machine.selection_with_offers(records, [])
    assert [p['provider_name'] for p in result['providers']] == ['Allowed Clinic']
    assert result['providers'][0]['offers'][0]['headline_payout'] is None
    assert result['providers'][0]['selected_winner'] is None
    assert len(result['excluded_offers']) == 4


def test_historical_first_run_winner_cannot_restore_held_route(machine):
    machine.PAYOUT_COMPARISONS.write_text(json.dumps({'duplicate_providers': [{
        'provider': 'Telos Health', 'scope': 'clinic_scout', 'switch_staged': True,
        'offers': [{'network': 'xentraffic', 'program_id': '1', 'payout_per_lead': 200}]}]}))
    decisions = machine.baseline_comparison_decisions({'providers': []})
    assert decisions[0]['switch_staged'] is False
    assert decisions[0]['winner']['network'] == ''


def test_config_and_paths_come_from_checkout(machine):
    assert machine.BUSINESS == ROOT
    assert machine.BEST_TERMS_CONFIG.is_file()


def test_weekly_machine_writes_matrix_with_held_source_explicit(machine, monkeypatch, tmp_path):
    summary = {'networks': {name: {'completeness': 'COMPLETE', 'enumerated': 0, 'reported_total': 0}
                            for name in machine.REQUIRED_MARKETPLACES if name != 'xentraffic'}}
    (tmp_path / '_sweep-summary.json').write_text(json.dumps(summary))
    monkeypatch.setattr(machine, 'run_xentraffic_census', lambda *_: pytest.fail('held source launched'))
    result = machine.run_machine(first_run=False, approval_events=False, stage=False, census_dir=tmp_path)
    assert result['status'] == 'PASS'
    assert result['census']['denominator_scope'] == 'eligible_nonheld_marketplaces_only'
    assert result['census']['excluded_marketplaces'] == ['xentraffic']
    assert result['census']['networks']['xentraffic']['enumerated'] is None
    assert result['sheet_queue']['status'] == 'DRY_RUN'
    assert json.loads(machine.LATEST_MATRIX.read_text())['providers'] == []


def test_supply_launch_excludes_held_network(machine, monkeypatch, tmp_path):
    from types import SimpleNamespace
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'affiliate_catalog_batch.py').touch()
    monkeypatch.setattr(machine, 'SCRIPTS', scripts)
    monkeypatch.setattr(machine, 'collector_runtime_preflight', lambda: {'status': 'PASS'})
    def launched(command, **kwargs):
        assert '--only' in command
        assert 'xentraffic' not in command[command.index('--only') + 1:]
        assert 'impact' in command
        out = Path(command[command.index('--outdir') + 1])
        out.mkdir()
        (out / '_sweep-summary.json').write_text(json.dumps({'networks': {}, 'status': 'PASS'}))
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(machine.subprocess, 'run', launched)
    _, summary = machine.run_supply_census(tmp_path)
    assert summary['collector_runtime_preflight']['status'] == 'PASS'


def test_missing_collector_source_is_explicit_before_launch(machine, monkeypatch, tmp_path):
    monkeypatch.setattr(machine, 'SCRIPTS', tmp_path / 'absent')
    monkeypatch.setattr(machine.subprocess, 'run', lambda *a, **k: pytest.fail('missing source launched'))
    with pytest.raises(RuntimeError, match='best_terms_collector_source_missing'):
        machine.run_supply_census(tmp_path)


def test_missing_performance_is_not_fabricated_zero(machine, tmp_path):
    with pytest.raises(RuntimeError, match='affiliate_payout_performance_unreadable'):
        machine.offline_test_original_performance_loader(tmp_path / 'missing-performance.json')


@pytest.mark.parametrize('filename', ['impact.json', 'impact-inventory.json'])
@pytest.mark.parametrize('adverse', [
    {'held': True}, {'suppressed': True}, {'status': 'suppressed'},
    {'relationship': 'held'},
])
def test_review_r1_real_machine_excludes_held_rail_by_exact_id(
        machine, monkeypatch, tmp_path, filename, adverse):
    """Reviewer pipeline: approved Sheet plus adverse census with another label."""
    census = tmp_path / 'census'
    census.mkdir()
    raw = {'program_id': '00123', 'id': '00123', 'name': 'Other Census Label',
           'relationship': 'approved', 'headline_payout': 250,
           'payout_model': 'CPL', 'paid_event': 'qualified_lead', **adverse}
    (census / filename).write_text(json.dumps({'network': 'impact', 'programs': [raw]}))
    (census / '_sweep-summary.json').write_text(json.dumps({'networks': {}}))
    row = [''] * 27
    row[2], row[4], row[10] = 'Pipeline Hold Clinic', 'Impact', 'approved_active'
    row[16], row[19], row[23] = '$20 per qualified lead', True, 'provider_program_id=00123'
    monkeypatch.setattr(machine, 'PAYOUT_MATRIX', tmp_path / 'prior-payout.json')
    monkeypatch.setattr(machine, 'sheet_offers', machine.offline_test_original_sheet_offers)
    monkeypatch.setattr(machine, 'inventory_programs', machine.offline_test_original_inventory_programs)
    monkeypatch.setattr(machine, 'read_live_sheet', lambda: [machine.sheet_writer.HEADERS, row])
    result = machine.run_machine(first_run=False, approval_events=False, stage=False, census_dir=census)
    matrix = json.loads(machine.LATEST_MATRIX.read_text())
    assert not any(p.get('selected_winner') for p in matrix['providers'])
    assert any(r['program_id'] == '00123' for r in matrix['excluded_offers'])
    assert result['sheet_queue']['queued'] == 0
    assert all(v == 0 for v in result['effects'].values())
    # Final selection is defensive even if a caller bypasses merge_offers.
    live = {'provider_name': 'Allowed Clinic', 'network': 'impact', 'program_id': '00123',
            'relationship': 'approved', 'wire_in_ready': True, 'headline_payout': 20,
            'payout_model': 'CPL', 'currency': 'USD', 'paid_event': 'qualified_lead'}
    sibling = {**live, 'program_id': '123'}  # Leading zeros are identity, not labels.
    selected = machine.selection_with_offers([live, {**live, **adverse}, sibling], [])
    winners = [p['selected_winner'] for p in selected['providers'] if p['selected_winner']]
    assert len(winners) == 1 and winners[0]['program_id'] == '123'


@pytest.mark.parametrize('field,value', [
    pytest.param('offer_status', 'suppressed', id='offer_status_suppressed'),
    pytest.param('network_status', 'held', id='network_status_held'),
    pytest.param('held', True, id='held_true'),
    pytest.param('suppressed', True, id='suppressed_true'),
])
def test_review_rr1_ledger_adverse_evidence_blocks_stale_sheet_alias(
        machine, monkeypatch, tmp_path, field, value):
    """Keep the actual ledger -> Sheet merge -> run_machine selection path."""
    import socket

    def denied(*args, **kwargs):
        pytest.fail('live transport or effect attempted')

    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(machine, 'run_supply_census', denied)
    monkeypatch.setattr(machine, 'run_xentraffic_census', denied)
    monkeypatch.setattr(machine, 'send_discord', denied)
    monkeypatch.setattr(machine, 'BUSINESS', tmp_path)
    monkeypatch.setattr(machine, 'PAYOUT_MATRIX', tmp_path / 'prior-payout.json')
    ledger = tmp_path / 'reports/glp-clinic-scout/affiliate-machine/applications-ledger.json'
    ledger.parent.mkdir(parents=True)
    raw = {'network': 'Impact Radius', 'program_id': '00073',
           'name': 'Other Ledger Label', 'current_relationship': 'approved',
           'status': 'submitted', field: value}
    ledger.write_text(json.dumps({'applications': [raw]}))
    census = tmp_path / 'census'
    census.mkdir()
    (census / '_sweep-summary.json').write_text(json.dumps({'networks': {}}))
    row = [''] * 27
    row[2], row[4], row[10] = 'Safe Sheet Alias', 'IMPACT.COM', 'approved_active'
    row[16], row[19], row[20] = '$37 per qualified lead', True, True
    row[23] = 'provider_program_id=00073'
    monkeypatch.setattr(machine, 'read_live_sheet', lambda: [machine.sheet_writer.HEADERS, row])
    monkeypatch.setattr(machine, 'sheet_offers', machine.offline_test_original_sheet_offers)
    monkeypatch.setattr(machine, 'application_ledger_offers', machine.offline_test_original_application_ledger_offers)
    monkeypatch.setattr(machine, 'inventory_programs', machine.offline_test_original_inventory_programs)

    result = machine.run_machine(first_run=False, approval_events=False, stage=False, census_dir=census)
    matrix = json.loads(machine.LATEST_MATRIX.read_text())
    assert not any(p.get('selected_winner') for p in matrix['providers'])
    assert {(r['provider_name'], r['network'], r['program_id'], r['reason'])
            for r in matrix['excluded_offers']} == {
                (label, 'impact', '00073', 'source_hold_or_suppression')
                for label in ('Other Ledger Label', 'Safe Sheet Alias')}
    assert result['sheet_queue']['queued'] == 0
    assert all(v == 0 for v in result['effects'].values())

    normalized = machine.application_ledger_offers()[0]
    assert normalized[field] == value
    assert normalized['source_exclusion_reason'] == 'source_hold_or_suppression'
    assert normalized['headline_payout'] is None  # Unknown is not zero.
    live = machine.sheet_offers([machine.sheet_writer.HEADERS, row])[0]
    sibling = {**live, 'program_id': '73'}  # Opaque leading zeros remain distinct.
    for records in ([live, normalized], [normalized, live]):
        for offers in (records, machine.merge_offers(records)):
            selected = machine.selection_with_offers([*offers, sibling], [])
            winners = [p['selected_winner'] for p in selected['providers'] if p['selected_winner']]
            assert [w['program_id'] for w in winners] == ['73']
