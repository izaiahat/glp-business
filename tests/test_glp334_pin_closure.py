"""Offline projected receiving contract; no importer, scheduler or provider I/O."""
import importlib.util
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COHORT = ROOT / 'ops/glp/reporting-v2/glp334-install-cohort.json'


def fixture_root(tmp_path):
    """Portable CI tree. Retained non-source report bytes are explicitly synthetic."""
    cohort = json.loads(COHORT.read_text())
    paths = set(cohort['selected_paths']) | {'ops/glp/reporting-v2/glp334-install-cohort.json'}
    for manifest in ('source-manifest.json', 'source-manifest-j5.json',
                     'source-manifest-j6.json', 'source-manifest-j7.json'):
        pins = json.loads((ROOT/'ops/glp/reporting-v2'/manifest).read_text())['files']
        paths.update(str(Path(p).relative_to('/home/ubuntu/business')) for p in pins)
    for wrapper in [p for p in paths if p.endswith('.sh')]:
        text = (ROOT/wrapper).read_text()
        for path, expected in re.findall(r'^check_source (\S+) ([a-f0-9]{64})$', text, re.M):
            paths.add(path)
    intake_pairs = [('xentraffic-intake', 'xentraffic-sha256'),
                    ('eden-intake', 'eden-intake-sha256'),
                    ('statement-intake', 'statement-sha256')]
    j7 = (ROOT/'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting_j7.sh').read_text()
    for flag, _ in intake_pairs:
        path = re.search(r'--'+flag+r' (\S+)', j7).group(1)
        paths.add(str(Path(path).relative_to('/home/ubuntu/business')))
    for path in paths:
        target = tmp_path/path
        target.parent.mkdir(parents=True, exist_ok=True)
        if (ROOT/path).is_file():
            shutil.copyfile(ROOT/path, target)
        else:
            assert path.startswith('reports/'), path
            target.write_text('{}')
    for wrapper in tmp_path.glob('ops/hermes-cron-scripts/*.sh'):
        text = wrapper.read_text()
        for path, expected in re.findall(r'^check_source (\S+) ([a-f0-9]{64})$', text, re.M):
            if path.startswith('reports/'):
                text = text.replace(expected, hashlib.sha256((tmp_path/path).read_bytes()).hexdigest())
        for flag, hashflag in intake_pairs:
            if '--'+flag+' ' in text:
                path = re.search(r'--'+flag+r' (\S+)', text).group(1)
                expected = re.search(r'--'+hashflag+r' ([a-f0-9]{64})', text).group(1)
                data = (tmp_path/Path(path).relative_to('/home/ubuntu/business')).read_bytes()
                text = text.replace(expected, hashlib.sha256(data).hexdigest())
        wrapper.write_text(text)
    return tmp_path


def load_checker():
    spec = importlib.util.spec_from_file_location('closure', ROOT/'scripts/glp334_pin_closure.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reviewed_cohort_closes_source_and_consumer_without_repinning(tmp_path):
    # This declaration was missing: PR551-only selection left the installed
    # predecessor reconciler and its dependent consumer outside the cohort.
    assert COHORT.is_file(), 'missing reviewed projected receiving cohort'
    module = load_checker()
    tree = fixture_root(tmp_path)
    result = module.project(tree, tree, json.loads(COHORT.read_text()))
    assert result['status'] == 'READY_SOURCE_ONLY', result
    assert result['drifts'] == []
    assert len(result['selected_paths']) == 14
    assert result['effects'] == 'NONE_READ_ONLY_PROJECTION_NOT_INSTALLATION'


@pytest.mark.parametrize('path', [
    'scripts/glp_affiliate_weekly_reporting.py',
    'scripts/glp334_live_reconciliation.py',
    'scripts/affiliate_cj_checkpoint.py',
    'reports/ops/f12-host-followthrough-20261002/reconciliation-input.json',
])
def test_missing_or_drifted_dependency_is_not_repaired_or_ignored(tmp_path, path):
    tree = fixture_root(tmp_path)
    target = tree/path
    target.write_bytes(target.read_bytes()+b'\n')
    before = target.read_bytes()
    result = load_checker().project(tree, tree, json.loads(COHORT.read_text()))
    assert result['status'] == 'BLOCKED_PROJECTED_PINS'
    assert any(d['path'] == path for d in result['drifts'])
    assert target.read_bytes() == before


@pytest.mark.parametrize('flag', ['xentraffic-intake', 'eden-intake', 'statement-intake'])
@pytest.mark.parametrize('condition', ['missing', 'tampered'])
@pytest.mark.parametrize('selected_intake', [False, True])
def test_every_retained_intake_pin_checks_projected_bytes(tmp_path, flag, condition, selected_intake):
    tree = fixture_root(tmp_path/'checkout')
    installed = fixture_root(tmp_path/'installed')
    wrapper = tree/'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting_j7.sh'
    path = re.search(r'--'+flag+r' (\S+)', wrapper.read_text()).group(1)
    path = str(Path(path).relative_to('/home/ubuntu/business'))
    cohort = json.loads(COHORT.read_text())
    selected = cohort['selected_paths'] + ([path] if selected_intake else [])
    target = (tree if selected_intake else installed)/path
    if condition == 'missing':
        target.unlink()
    else:
        target.write_bytes(b'tampered synthetic intake')
    result = load_checker().project(tree, installed, cohort, selected)
    assert result['status'] == 'BLOCKED_PROJECTED_PINS', result
    assert any(d['path'] == path and d['selected'] == selected_intake for d in result['drifts'])


WRAPPERS = ['ops/hermes-cron-scripts/glp_affiliate_weekly_reporting'+s+'.sh'
            for s in ('', '_j5', '_j6', '_j7')]+[
                'ops/hermes-cron-scripts/glp334_live_reconciliation.sh']


@pytest.mark.parametrize('wrapper', WRAPPERS)
@pytest.mark.parametrize('condition', ['missing', 'empty', 'directory', 'invalid_utf8'])
def test_expected_wrapper_refuses_explicitly(tmp_path, wrapper, condition):
    tree = fixture_root(tmp_path)
    target = tree/wrapper
    if condition in ('missing', 'directory'):
        target.unlink()
        if condition == 'directory':
            target.mkdir()
    else:
        target.write_bytes(b'' if condition == 'empty' else b'\xff')
    result = load_checker().project(tree, tree, json.loads(COHORT.read_text()))
    assert result['status'] == 'BLOCKED_PROJECTED_PINS', result
    assert any(d['path'] == wrapper and d.get('reason') for d in result['drifts'])


@pytest.mark.parametrize('wrapper', WRAPPERS)
def test_expected_wrapper_cannot_disappear_from_cohort_declaration(tmp_path, wrapper):
    tree = fixture_root(tmp_path)
    (tree/wrapper).unlink()
    cohort = json.loads(COHORT.read_text())
    cohort['selected_paths'].remove(wrapper)
    result = load_checker().project(tree, tree, cohort)
    assert result['status'] == 'BLOCKED_PROJECTED_PINS', result
    assert any(d['path'] == wrapper for d in result['drifts'])


def mandatory_declarations():
    for wrapper in WRAPPERS:
        text = (ROOT/wrapper).read_text()
        if wrapper == WRAPPERS[-1]:
            yield wrapper, re.search(r'^source_path=.*$', text, re.M).group(0)
            yield wrapper, re.search(r'== ([a-f0-9]{64})', text).group(1)
            continue
        for line in re.findall(r'^check_source .*$', text, re.M):
            yield wrapper, line
        for flag in ['source-manifest', 'manifest-sha256'] + (
                ['xentraffic-intake', 'xentraffic-sha256', 'eden-intake',
                 'eden-intake-sha256', 'statement-intake', 'statement-sha256']
                if wrapper.endswith('_j7.sh') else []):
            yield wrapper, re.search(r'--'+flag+r' [^\s()]+', text).group(0)


@pytest.mark.parametrize('wrapper,declaration', list(mandatory_declarations()))
@pytest.mark.parametrize('condition', ['removed', 'malformed', 'duplicate'])
def test_mandatory_wrapper_pin_declarations_refuse_explicitly(tmp_path, wrapper, declaration, condition):
    tree = fixture_root(tmp_path)
    target = tree/wrapper
    # Fixture-only report pins differ; locate the corresponding declaration by
    # its path/flag, never restore a real intake pin into synthetic source.
    if declaration.startswith('check_source'):
        declaration = re.search(r'^'+re.escape(' '.join(declaration.split()[:2]))+r' .*$', target.read_text(), re.M).group(0)
    elif declaration.startswith('--'):
        flag = declaration.split()[0]
        declaration = re.search(re.escape(flag)+r' [^\s()]+', target.read_text()).group(0)
    replacement = {'removed': '', 'malformed': declaration+'INVALID',
                   'duplicate': declaration+'\n'+declaration}[condition]
    target.write_text(target.read_text().replace(declaration, replacement))
    result = load_checker().project(tree, tree, json.loads(COHORT.read_text()))
    assert result['status'] == 'BLOCKED_PROJECTED_PINS', result
    assert any(d.get('reason') for d in result['drifts']), result


@pytest.mark.parametrize('wrapper', WRAPPERS)
@pytest.mark.parametrize('condition', ['missing', 'empty'])
def test_actual_checker_cli_refuses_missing_or_empty_wrapper(tmp_path, wrapper, condition):
    tree = fixture_root(tmp_path)
    target = tree/wrapper
    target.unlink() if condition == 'missing' else target.write_bytes(b'')
    run = subprocess.run([sys.executable, '-B', str(ROOT/'scripts/glp334_pin_closure.py'),
                          '--checkout', str(tree), '--installed-root', str(tree)],
                         capture_output=True, text=True)
    assert run.returncode == 2, run.stdout+run.stderr
    result = json.loads(run.stdout)
    assert result['status'] == 'BLOCKED_PROJECTED_PINS'
    assert any(d['path'] == wrapper for d in result['drifts'])
    assert not run.stderr


def test_consumer_copin_must_travel_with_successor(tmp_path):
    tree = fixture_root(tmp_path/'source')
    installed = fixture_root(tmp_path/'installed')
    predecessor = json.loads(COHORT.read_text())['installed_predecessor']['sha256']
    consumer = installed/'scripts/glp334_live_reconciliation.py'
    current = json.loads(COHORT.read_text())['maintained_successor']['sha256']
    consumer.write_text(consumer.read_text().replace(current, predecessor))
    cohort = json.loads(COHORT.read_text())
    selection = [p for p in cohort['selected_paths'] if p != 'scripts/glp334_live_reconciliation.py']
    result = load_checker().project(tree, installed, cohort, selection)
    assert result['status'] == 'BLOCKED_PROJECTED_PINS'
    assert any(d['owner'] == 'scripts/glp334_live_reconciliation.py' for d in result['drifts'])


def test_unmodified_job_verifies_full_manifest_then_imports_real_reporter(tmp_path):
    tree = fixture_root(tmp_path)
    manifest = tree/'ops/glp/reporting-v2/source-manifest-j7.json'
    document = json.loads(manifest.read_text())
    # Relocate only manifest path keys for portable CI; all source bytes/pins
    # including the job and reporter remain unchanged.
    document['files'] = {str(tree/Path(p).relative_to('/home/ubuntu/business')): h
                         for p, h in document['files'].items()}
    manifest.write_text(json.dumps(document))
    command = f'''
import builtins, importlib.util, socket, sys
from pathlib import Path
root = Path({str(tree)!r})
sys.path.insert(0, str(root/'scripts'))
def forbidden(*a, **k): raise AssertionError('provider or credential I/O forbidden')
socket.socket = forbidden
original_open = builtins.open
def safe_open(file, *a, **k):
    assert '/.hermes/secrets/' not in str(file) and '/.env' not in str(file)
    return original_open(file, *a, **k)
builtins.open = safe_open
spec = importlib.util.spec_from_file_location('real_job', root/'scripts/glp_affiliate_weekly_reporting_j7_job.py')
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)
def stop_after_verified_import(frame, event, arg):
    if event == 'call' and frame.f_code.co_name == 'main' and frame.f_globals.get('__name__') == 'glp_affiliate_weekly_reporting_j7':
        print('REAL_JOB_FULL_MANIFEST_VERIFIED_REPORTER_IMPORTED')
        raise SystemExit(0)
    return stop_after_verified_import
sys.settrace(stop_after_verified_import)
job.main(['--source-manifest', str(root/'ops/glp/reporting-v2/source-manifest-j7.json'),
    '--manifest-sha256', {hashlib.sha256(manifest.read_bytes()).hexdigest()!r},
    '--roster', 'UNREAD', '--identity-registry', 'UNREAD', '--report-root', 'UNWRITTEN'])
'''
    run = subprocess.run([sys.executable, '-B', '-c', command], text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == 'REAL_JOB_FULL_MANIFEST_VERIFIED_REPORTER_IMPORTED'
    assert not (tree/'UNWRITTEN').exists()


def test_real_consumer_pin_loader_imports_successor_and_retains_null_money(tmp_path):
    tree = fixture_root(tmp_path)
    code = f'''
import importlib.util, socket, ssl, sys
from pathlib import Path
socket.socket = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network forbidden'))
root = Path({str(tree)!r})
sys.path.insert(0, str(root/'scripts'))
spec = importlib.util.spec_from_file_location('consumer', root/'scripts/glp334_live_reconciliation.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.ROOT = root
reporter, test_id = m.load_reconciler()
result = reporter.reconcile_conversions(None, [], [])
assert not result['coverage_complete']
assert result['commission_minor'] is None and result['cash_minor'] is None
print('REAL_CONSUMER_PIN_IMPORT_NULL_MONEY_OK')
'''
    run = subprocess.run([sys.executable, '-B', '-c', code], text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == 'REAL_CONSUMER_PIN_IMPORT_NULL_MONEY_OK'
