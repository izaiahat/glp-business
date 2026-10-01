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
    monkeypatch.setattr(module, 'sheet_offers', lambda _: [])
    monkeypatch.setattr(module, 'application_ledger_offers', lambda: [])
    monkeypatch.setattr(module, 'inventory_programs', lambda _: [])
    module.offline_test_original_performance_loader = module.load_performance_rows
    monkeypatch.setattr(module, 'load_performance_rows', lambda: [])
    return module


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
    records = [{'provider_name': name, 'network': network, 'program_id': '1',
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
    (scripts / 'marketplace_supply_sweep.py').touch()
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
