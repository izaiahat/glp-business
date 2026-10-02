"""Portable shell -> real pinned job -> real offline producer regression.

Only host paths/interpreter and relocated data/manifest hashes are substituted.
No synthetic fixture is represented as production or a financial receipt.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
INPUT = 'reports/ops/f12-host-followthrough-20261002/reconciliation-input.json'
INPUT_SHA = '5ae5aee441a9dbc6c9cb17dfb66fe0c7f28068f68d267ba6707b3c333deb95f1'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def portable_wrapper(tmp_path):
    wrapper = (ROOT/'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting.sh').read_text()
    scripts = tmp_path/'scripts'
    scripts.mkdir()
    names = ['glp_affiliate_weekly_reporting_job.py', 'glp_affiliate_weekly_reporting.py',
             'glp_network_revenue_v2.py', 'glp_network_revenue_import_v2.py']
    for name in names:
        shutil.copyfile(ROOT/'scripts'/name, scripts/name)
    manifest = tmp_path/'ops/glp/reporting-v2/source-manifest.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'files': {str(scripts/n): sha(scripts/n) for n in names}}))
    old = sha(ROOT/'ops/glp/reporting-v2/source-manifest.json')
    wrapper = wrapper.replace(old, sha(manifest))
    registry = manifest.with_name('identity-registry.json')
    shutil.copyfile(ROOT/'ops/glp/reporting-v2/identity-registry.json', registry)
    roster = tmp_path/'reports/ops/orchestration-restore-20260907/WS-F/NETWORK-REPORTING-ALL/MARKETPLACES.json'
    roster.parent.mkdir(parents=True)
    roster.write_text(json.dumps({'marketplace_count': 1, 'marketplaces': [{'marketplace': 'affiliati'}]}))
    wrapper = wrapper.replace('9e0bc9ba3f1910e1f5e540bcbfcaa1c344a27366fbc0209ee55972ef4628a799', sha(roster))
    snapshot = tmp_path/INPUT
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps({'schema': 'glp-f12-reconciliation-input-v1',
        'evidence_ref': 'fixture:synthetic-explicit-TEST-only', 'complete': False,
        'conversions': [{'network': '__receiver_selftest__', 'event_type': 'accepted_conversion'}],
        'financial_records': [], 'clicks': []}))
    wrapper = wrapper.replace(INPUT_SHA, sha(snapshot))
    wrapper = wrapper.replace('/home/ubuntu/business', str(tmp_path))
    wrapper = wrapper.replace('/home/ubuntu/.hermes/hermes-agent/venv/bin/python', sys.executable)
    executable = tmp_path/'entry.sh'
    executable.write_text(wrapper)
    # The subprocess must not create sockets or launch a financial/ledger helper.
    (scripts/'sitecustomize.py').write_text('import sys\n'
        'def guard(event, args):\n'
        '    if event.startswith("socket.") or event in ("subprocess.Popen", "os.system", "sqlite3.connect"):\n'
        '        raise SystemExit("FORBIDDEN_EFFECT")\n'
        'sys.addaudithook(guard)\n')
    return executable, snapshot


def execute(executable):
    return subprocess.run(['bash', str(executable), '--dry-run'], text=True, capture_output=True,
        env={**os.environ, 'PYTHONPATH': str(executable.parent/'scripts'), 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=30)


def test_scheduled_shell_consumes_retained_input_in_real_job(tmp_path):
    executable, snapshot = portable_wrapper(tmp_path)
    completed = execute(executable)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    files = list((tmp_path/'reports/glp-clinic-scout/affiliate-machine/weekly-reporting').glob('*/RESULT.json'))
    assert len(files) == 1
    report = json.loads(files[0].read_text())
    value = report['conversion_reconciliation']
    assert value['input_event_count'] == 1
    assert value['excluded_counts'] == {'SELFTEST': 1, 'TEST': 0, 'PROBE': 0}
    assert value['provenance'] == 'SUPPLIED_LOCAL_EVIDENCE_NOT_VERIFIED_PRODUCTION_DATA'
    assert value['input_completeness'] == 'UNKNOWN'
    assert value['cash_minor'] is value['commission_minor'] is None
    assert value['coverage_complete'] is False
    assert report['invocation'] == 'OFFLINE_DRY_RUN_NO_PROVIDER_IO'


def test_retained_input_tamper_stops_before_job(tmp_path):
    executable, snapshot = portable_wrapper(tmp_path)
    snapshot.write_text(snapshot.read_text() + ' ')
    completed = execute(executable)
    assert completed.returncode == 66
    assert 'K3_REPORTING_SOURCE_DRIFT' in completed.stderr
    assert not (tmp_path/'reports/glp-clinic-scout/affiliate-machine/weekly-reporting').exists()


def test_absent_retained_input_stops_before_job(tmp_path):
    executable, snapshot = portable_wrapper(tmp_path)
    snapshot.unlink()
    completed = execute(executable)
    assert completed.returncode != 0
    assert not (tmp_path/'reports/glp-clinic-scout/affiliate-machine/weekly-reporting').exists()
