"""Offline dispatcher integration: real validators, fake authenticated transports only.

Protected fixtures retain the existing host-only typed-lint boundary spy; all
scope, custody, current-description, independent review and evidence gates run.
These artifacts are test inputs, never receipts for live acceptance.
"""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import types

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fleet_ingest as fi
import auto_coordinator_loop as loop
from test_linear_ordinary_completion import (completion, readonly_completion,
    protected_noncode_completion, protected_runtime_completion, lo)


def snapshot(root):
    return {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def validator_runner(target, calls):
    def run(argv, root, timeout):
        calls.append(argv)
        assert argv[-1] == '--check-only' and '--apply' not in argv
        assert Path(argv[1]).name == 'linear_orchestrator.py'
        assert argv[2:4] == ['pass', target['identifier']]
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                lo.pass_issue(target, argv[4], {}, check_only=True, **{
                    argv[5][2:].replace('-', '_'): True})
        except SystemExit as exc:
            return {'rc': 3, 'stdout': output.getvalue(), 'stderr': str(exc)}
        return {'rc': 0, 'stdout': output.getvalue(), 'stderr': ''}
    return run


@pytest.mark.parametrize('contract,fixture', [
    ('ordinary', 'readonly_completion'),
    ('ordinary', 'completion'),
    ('protected-noncode', 'protected_noncode_completion'),
    ('protected-runtime', 'protected_runtime_completion'),
])
def test_typed_contract_positive_through_real_validator(request, contract, fixture):
    target, path, data, writes, *_ = request.getfixturevalue(fixture)
    calls = []
    before = snapshot(path.parent.parent)
    result = fi.completion_preflight(path.parent.parent, target['identifier'], str(path),
        {'completion_contract': contract}, validator_runner(target, calls))
    assert result['rc'] == 0, result
    assert len(calls) == 1 and calls[0][-2:] == ['--' + contract, '--check-only']
    assert json.loads(result['stdout'])['writes'] == 0
    assert writes == [] and snapshot(path.parent.parent) == before


@pytest.mark.parametrize('defect', ['missing', 'stale', 'wrong-issue', 'wrong-author',
    'missing-evidence', 'changed-evidence', 'protected-as-ordinary', 'source-only'])
def test_typed_protected_refusals_reach_no_writes(protected_noncode_completion, defect):
    target, path, data, writes, parent, independent, *_ = protected_noncode_completion
    contract = 'protected-noncode'
    if defect == 'missing': data.pop('review')
    elif defect == 'stale': parent['createdAt'] = '2000-01-01T00:00:00+00:00'
    elif defect == 'wrong-issue': data['issue'] = 'GLP-999'
    elif defect == 'wrong-author': independent['user']['id'] = data['author_id']
    elif defect == 'missing-evidence': Path(data['evidence'][0]['path']).unlink()
    elif defect == 'changed-evidence': Path(data['evidence'][0]['path']).write_text('drift')
    elif defect == 'protected-as-ordinary': contract = 'ordinary'
    elif defect == 'source-only': data['status'] = 'SOURCE_ONLY'
    path.write_text(json.dumps(data))
    before = snapshot(path.parent.parent)
    result = fi.completion_preflight(path.parent.parent, target['identifier'], str(path),
        {'completion_contract': contract}, validator_runner(target, []))
    assert result['rc'] != 0
    assert writes == [] and snapshot(path.parent.parent) == before


@pytest.mark.parametrize('change', ['missing', 'outside', 'symlink', 'schema', 'scope',
    'blocked', 'worker-status', 'unknown-contract'])
def test_typed_route_refuses_before_invocation(readonly_completion, change):
    target, path, data, writes, *_ = readonly_completion
    root = path.parent.parent
    contract = 'ordinary'
    if change == 'missing': path.unlink()
    elif change in {'outside', 'symlink'}:
        other = root / 'outside/RESULT.json'
        other.parent.mkdir()
        other.write_text(path.read_text())
        if change == 'outside': path = other
        else:
            path.unlink()
            path.symlink_to(other)
    else:
        if change == 'schema': data['schema'] = 'unknown-completion-v1'
        elif change == 'scope': data['scope'] = 'protected-runtime'
        elif change == 'blocked': data['status'] = 'BLOCKED'
        elif change == 'worker-status': data['status'] = 'DONE_READY_TO_CLOSE'
        elif change == 'unknown-contract': contract = 'autodetect'
        path.write_text(json.dumps(data))
    def forbidden(*args): pytest.fail('invalid typed route must not execute a validator')
    result = fi.completion_preflight(root, target['identifier'], str(path),
        {'completion_contract': contract}, forbidden)
    assert result['rc'] != 0 and writes == []


@pytest.mark.parametrize('output', ['', 'GLP-60 PASS', 'prefix {"identifier":"GLP-60","validated":true,"writes":0}',
    '{"identifier":"GLP-999","validated":true,"writes":0}',
    '{"identifier":"GLP-60","validated":1,"writes":0}',
    '{"identifier":"GLP-60","validated":true,"writes":false}',
    '{"identifier":"GLP-60","validated":true,"writes":1}',
    '{"identifier":"GLP-60","validated":true}', '[]'])
def test_typed_response_is_strict_not_prose(readonly_completion, output):
    target, path, *_ = readonly_completion
    result = fi.completion_preflight(path.parent.parent, target['identifier'], str(path),
        {'completion_contract': 'ordinary'}, lambda *a: {'rc': 0, 'stdout': output, 'stderr': ''})
    assert result['rc'] != 0


def test_valid_response_with_nonzero_exit_still_refuses(readonly_completion):
    target, path, *_ = readonly_completion
    result = fi.completion_preflight(path.parent.parent, target['identifier'], str(path),
        {'completion_contract': 'ordinary'}, lambda *a: {'rc': 7, 'stdout': json.dumps({
            'identifier': target['identifier'], 'validated': True, 'writes': 0}), 'stderr': 'refused'})
    assert result['rc'] == 7


def test_loop_binds_coordinator_config_contract_and_detects_change(tmp_path):
    plan = tmp_path / loop.PLAN
    plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({'issues': [{'issue': 'GLP-358', 'proof_complete': True}]}))
    config = tmp_path / 'reports/ops/fleet-inbox/CONFIG.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'issues': {'GLP-358': {'completion_contract': 'protected-runtime'}}}))
    rows, inbox = loop.inputs(tmp_path)
    assert rows[0]['completion_contract'] == 'protected-runtime'
    backend = loop.InstalledBackend(tmp_path, tmp_path / 'loop', 'fixture-token')
    action = {'issue': 'GLP-358', 'plan': rows[0]}
    backend.current(action)
    config.write_text(json.dumps({'issues': {'GLP-358': {'completion_contract': 'ordinary'}}}))
    with pytest.raises(ValueError, match='PLAN changed'): backend.current(action)


def test_ingest_and_loop_share_typed_route_and_revalidate(readonly_completion, monkeypatch):
    target, path, data, writes, *_ = readonly_completion
    root = path.parent.parent
    # Mirror maintained sources so byte changes invalidate cached readiness.
    scripts = root / 'scripts'; scripts.mkdir(exist_ok=True)
    for name in ('fleet_ingest.py', 'linear_close.py', 'linear_orchestrator.py'):
        (scripts / name).write_bytes((Path(fi.__file__).parent / name).read_bytes())
    policy = {'issue': target['identifier'], 'proof_complete': True,
              'proof_cmd': ['stat', str(path)], 'completion_contract': 'ordinary'}
    calls = []
    validate = validator_runner(target, calls)
    def runner(argv, cwd, timeout):
        if argv[0] == 'stat': return {'rc': 0, 'stdout': 'fixture proof', 'stderr': ''}
        return validate(argv, cwd, timeout)
    item = fi.event('native', target['identifier'], 'owner', path, [path], path)
    row = fi.ingest(root, item, policy, runner)
    assert row['status'] == 'READY_TO_CLOSE', row['failure_text']
    assert len(calls) == 1
    assert fi.ingest(root, item, policy, runner) == row and len(calls) == 1
    for name in ('fleet_ingest.py', 'linear_orchestrator.py'):
        (scripts / name).write_text((scripts / name).read_text() + '\n# validator change\n')
        row = fi.ingest(root, item, policy, runner)
        assert len(calls) >= 2 and row['status'] == 'READY_TO_CLOSE'
    monkeypatch.setattr(loop, 'inputs', lambda root: ([policy], [row]))
    backend = loop.InstalledBackend(root, root / 'loop', 'fixture-token')
    def call(argv):
        r = runner(argv, root, 30)
        return types.SimpleNamespace(returncode=r['rc'], stdout=r['stdout'], stderr=r['stderr'])
    monkeypatch.setattr(backend, 'call', call)
    action = {'issue': target['identifier'], 'plan': policy, 'inbox': row}
    assert backend.preflight(action) is True
    policy['completion_contract'] = 'protected-runtime'
    assert backend.preflight(action) is False
    with pytest.raises(ValueError, match='coordinator'):
        backend.effect(dict(action, kind='close'))
    assert writes == []
