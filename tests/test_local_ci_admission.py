"""Exact-head receipts; commands run in disposable Git fixtures, never live effects."""
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


def fixture_receipt(tmp_path, repo='izaiahat/glp-business', command='printf fixture-pass', workflow_text=None, files=None):
    ci = importlib.import_module('local_ci_admission')
    tree = tmp_path / 'tree'
    tree.mkdir()
    workflow = tree / '.github/workflows/ci.yml'
    workflow.parent.mkdir(parents=True)
    workflow.write_text(workflow_text or ('name: fixture\non: pull_request\njobs:\n  test:\n    steps:\n      - run: ' + command + '\n'))
    for name, content in (files or {}).items():
        target = tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    for args in (['init'], ['add', '.'], ['-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture']):
        subprocess.run(['git', '-C', str(tree), *args], check=True, capture_output=True)
    head = subprocess.check_output(['git', '-C', str(tree), 'rev-parse', 'HEAD'], text=True).strip()
    authority = tmp_path / ci.DECISION
    authority.parent.mkdir(parents=True)
    authority.write_text('fixture operator decision')
    receipt = ci.produce(tree, repo, f'https://github.com/{repo}/pull/1', head,
                         ['fixture.py'], tmp_path / 'lane', authority_root=tmp_path)
    if receipt['status'] == 'PASS':
        index = tmp_path / 'reports/ops/local-ci' / repo.split('/')[1] / (head + '.json')
        ci.write_new(index, ci.pin(tmp_path / 'lane/LOCAL-CI.json'))
    return ci, receipt, head, tree, authority


def test_declared_setup_python_cannot_admit_inherited_wrong_version(tmp_path):
    workflow = """on: pull_request
jobs:
  test:
    steps:
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065
        with:
          python-version: '3.12'
      - run: python3 -c 'import sys; print(sys.version); assert sys.version_info[:2] == (3, 11)'
"""
    ci, receipt, head, _, _ = fixture_receipt(tmp_path, workflow_text=workflow)
    assert receipt['status'] == 'FAIL'
    with pytest.raises(ValueError, match='commands_not_passed'):
        ci.validate_local(receipt, repo=receipt['repo'], pr=receipt['pr'], head=head,
                          changed_paths=['fixture.py'], authority_root=tmp_path)



def test_registered_runner_receipt_admits_exact_clean_head(tmp_path):
    ci, receipt, head, _, _ = fixture_receipt(tmp_path)
    admitted = ci.validate_local(receipt, repo=receipt['repo'], pr=receipt['pr'], head=head,
                                 changed_paths=['fixture.py'], authority_root=tmp_path)
    assert admitted['mode'] == 'local-ci'
    assert admitted['head_sha'] == head
    assert receipt['pass_count'] == 1 and receipt['fail_count'] == 0


@pytest.mark.parametrize('change,reason', [
    ('sha', 'receipt_head_mismatch'), ('dirty_before', 'dirty_tree'), ('dirty_after', 'dirty_tree'),
    ('commands', 'workflow_commands_missing_or_changed'), ('exit', 'commands_not_passed'),
    ('count', 'commands_not_passed'), ('status', 'commands_not_passed'),
    ('runner', 'runner_provenance_mismatch'), ('decision', 'regular_evidence_required'),
    ('log', 'command_log_drift'), ('files', 'pr_files_mismatch'), ('live_dirty', 'dirty_tree'),
    ('missing_result', 'command_results_missing'), ('repo', 'receipt_identity_mismatch'),
    ('pr', 'receipt_identity_mismatch'),
])
def test_each_local_receipt_refusal(tmp_path, change, reason):
    ci, r, head, tree, authority = fixture_receipt(tmp_path)
    if change == 'sha': r['head_sha'] = 'b' * 40
    elif change == 'dirty_before': r['worktree_before']['clean'] = False
    elif change == 'dirty_after': r['worktree_after']['clean'] = False
    elif change == 'commands': r['commands'] = []
    elif change == 'exit': r['results'][0]['exit_code'] = 1
    elif change == 'count': r['pass_count'] = True
    elif change == 'status': r['status'] = 'FAIL'
    elif change == 'runner': r['producer']['runner']['sha256'] = '0' * 64
    elif change == 'decision': authority.unlink()
    elif change == 'log': Path(r['results'][0]['log']['path']).write_text('tampered')
    elif change == 'files': r['changed_paths'] = []
    elif change == 'live_dirty': (tree / 'dirty.txt').write_text('dirt')
    elif change == 'missing_result': r['results'] = []
    elif change == 'repo': r['repo'] = 'izaiahat/glp-site-candidate'
    elif change == 'pr': r['pr'] += '2'
    with pytest.raises(ValueError, match=reason):
        ci.validate_local(r, repo='izaiahat/glp-business', pr='https://github.com/izaiahat/glp-business/pull/1',
                          head=head, changed_paths=['fixture.py'], authority_root=tmp_path)


def test_billing_locked_head_uses_local_receipt(tmp_path):
    ci, receipt, head, _, _ = fixture_receipt(tmp_path)
    assert hasattr(ci, 'admit'), 'billing-locked admission is missing'
    remote = {'headRefOid': head, 'files': [{'path': 'fixture.py'}], 'statusCheckRollup': []}
    locked = lambda *a: {'check_runs': [{'id': 1, 'head_sha': head, 'status': 'completed', 'conclusion': 'failure'}]} if a[-1].endswith('check-runs?per_page=100') else [
        {'message': 'The job was not started because your account is locked due to a billing issue.'}]
    mode = ci.admit(remote, repo=receipt['repo'], pr=receipt['pr'], head=head,
                    local_binding=ci.pin(tmp_path / 'lane/LOCAL-CI.json'), authority_root=tmp_path, gh=locked)
    assert mode['mode'] == 'local-ci'


def test_linear_closer_records_admission_mode(monkeypatch):
    import linear_close as closer
    import local_ci_admission as ci
    head = 'a' * 40
    remote = {'state': 'MERGED', 'mergeCommit': {'oid': 'b' * 40}, 'headRefOid': head,
              'statusCheckRollup': [{'name': 'hosted', 'detailsUrl': 'url', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}],
              'files': []}
    result = closer.resolve_pr({'pr': 'PR1'}, gh=lambda u: remote)
    assert result.get('ci_admission', {}).get('mode') == 'hosted-success'


def test_release_carrier_records_admission_mode(tmp_path, monkeypatch):
    import glp_affiliate_sync_job as job
    head = 'a' * 40
    remote = {'state': 'MERGED', 'url': 'https://github.com/izaiahat/glp-site-candidate/pull/1',
              'headRefOid': head, 'statusCheckRollup': [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}
    monkeypatch.setattr(job, 'run', lambda argv, **kw: '' if 'checks' in argv else json.dumps(remote))
    monkeypatch.setattr(job, 'save', lambda p, r: None)
    result = job.ci(remote['url'], tmp_path)
    assert result.get('ci_admission', {}).get('mode') == 'hosted-success'


def locked_gh(head):
    def gh(*args):
        if 'check-runs?' in args[-1]:
            return {'total_count': 1, 'check_runs': [{'id': 1, 'head_sha': head, 'status': 'completed', 'conclusion': 'failure'}]}
        return [{'message': 'The job was not started because your account is locked due to a billing issue.'}]
    return gh


def test_hosted_success_preferred_without_local_inputs(tmp_path):
    import local_ci_admission as ci
    head = 'a' * 40
    remote = {'headRefOid': head, 'statusCheckRollup': [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}
    def forbidden(*a): raise AssertionError('hosted success read a fallback input')
    assert ci.admit(remote, repo='izaiahat/glp-business', pr='PR1', head=head, local_binding={'bad': True},
                    authority_root=tmp_path, gh=forbidden) == {'mode': 'hosted-success', 'head_sha': head}


@pytest.mark.parametrize('fault', ['unproven', 'wrong_head', 'pending', 'ordinary_failure', 'no_annotation', 'truncated'])
def test_billing_lock_is_narrow(tmp_path, fault):
    ci, receipt, head, _, _ = fixture_receipt(tmp_path)
    def gh(*args):
        value = locked_gh(head)(*args)
        if 'check-runs?' in args[-1]:
            row = value['check_runs'][0]
            if fault == 'unproven': value['check_runs'] = []
            elif fault == 'wrong_head': row['head_sha'] = 'f' * 40
            elif fault == 'pending': row['status'] = 'in_progress'
            elif fault == 'truncated': value['total_count'] = 101
        elif fault == 'ordinary_failure': value = [{'message': 'A real test failed'}]
        elif fault == 'no_annotation': value = []
        return value
    with pytest.raises(ValueError, match='billing|hosted_failure'):
        ci.admit({'headRefOid': head, 'files': [{'path': 'fixture.py'}]}, repo=receipt['repo'], pr=receipt['pr'], head=head,
                 local_binding=ci.pin(tmp_path / 'lane/LOCAL-CI.json'), authority_root=tmp_path, gh=gh)


def test_runner_failure_receipt_is_not_admissible(tmp_path):
    ci, receipt, head, _, _ = fixture_receipt(tmp_path, command='exit 7')
    assert receipt['status'] == 'FAIL' and receipt['fail_count'] == 1
    with pytest.raises(ValueError, match='commands_not_passed'):
        ci.validate_local(receipt, repo=receipt['repo'], pr=receipt['pr'], head=head,
                          changed_paths=['fixture.py'], authority_root=tmp_path)


def test_runner_dirty_after_never_records_pass(tmp_path):
    ci, receipt, head, _, _ = fixture_receipt(tmp_path, command='touch dirty')
    assert receipt['status'] == 'FAIL'
    assert receipt['worktree_after']['clean'] is True  # retained input is not executed in place
    assert receipt['jobs'][0]['worktree_after']['clean'] is False
    with pytest.raises(ValueError, match='commands_not_passed'):
        ci.validate_local(receipt, repo=receipt['repo'], pr=receipt['pr'], head=head,
                          changed_paths=['fixture.py'], authority_root=tmp_path)


def test_local_ci_flows_through_closer_and_typed_gate(tmp_path, monkeypatch):
    import linear_close as closer
    ci, receipt, head, _, _ = fixture_receipt(tmp_path)
    original = ci.admit
    monkeypatch.setattr(ci, 'admit', lambda remote, **kw: original(remote, **{**kw, 'gh': locked_gh(head)}))
    monkeypatch.setattr(closer, 'ROOT', tmp_path)
    remote = {'state': 'MERGED', 'mergeCommit': {'oid': 'b' * 40}, 'headRefOid': head,
              'files': [{'path': 'fixture.py'}], 'statusCheckRollup': []}
    result = closer.resolve_pr({'pr': 'PR1', 'local_ci': str(tmp_path / 'lane/LOCAL-CI.json')}, gh=lambda _: remote)
    assert result['ci_admission']['mode'] == 'local-ci'
    assert ci.validate_pr_ci(result, remote, tmp_path)['mode'] == 'local-ci'
    data = {'pr': result, 'description_sha256': 'c' * 64, 'evidence': []}
    assert 'mode=local-ci' in closer.acceptance_body(data)
    rpath = tmp_path / 'lane/LOCAL-CI.json'
    rpath.write_text('{}')
    with pytest.raises(ValueError, match='evidence_drift'):
        ci.validate_pr_ci(result, remote, tmp_path)


def test_local_ci_flows_through_release_prebuild_gate(tmp_path, monkeypatch):
    import glp_affiliate_sync_job as job
    ci, receipt, head, _, _ = fixture_receipt(tmp_path, repo='izaiahat/glp-site-candidate')
    original = ci.admit
    monkeypatch.setattr(ci, 'admit', lambda remote, **kw: original(remote, **{**kw, 'gh': locked_gh(head)}))
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    remote = {'state': 'MERGED', 'url': receipt['pr'], 'headRefOid': head, 'files': [{'path': 'fixture.py'}],
              'statusCheckRollup': [{'status': 'COMPLETED', 'conclusion': 'FAILURE'}]}
    def run(argv, **kw):
        if 'checks' in argv: raise RuntimeError('billing-locked-watch')
        return json.dumps(remote)
    monkeypatch.setattr(job, 'run', run)
    result = job.ci(receipt['pr'], tmp_path, head=head, local_ci=tmp_path / 'lane/LOCAL-CI.json')
    assert result['ci_admission']['mode'] == 'local-ci'
    assert json.loads((tmp_path / 'CI.json').read_text())['ci_admission']['head_sha'] == head
    remote['headRefOid'] = 'b' * 40
    with pytest.raises(RuntimeError, match='source_pr_head_mismatch'):
        job.ci(receipt['pr'], tmp_path, head=head, local_ci=tmp_path / 'lane/LOCAL-CI.json')


def test_registered_runner_cli_writes_real_receipt_and_index(tmp_path, monkeypatch):
    import local_ci_runner as runner
    ci, receipt, head, tree, _ = fixture_receipt(tmp_path)
    index = tmp_path / 'reports/ops/local-ci/glp-business' / (head + '.json')
    index.unlink()  # separate fixture production from CLI production
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    original = ci.produce
    monkeypatch.setattr(runner, 'produce', lambda *a: original(*a, authority_root=tmp_path))
    monkeypatch.setattr(runner, 'gh_json', lambda *a: {'headRefOid': head, 'files': [{'path': 'fixture.py'}]})
    assert runner.main(['--worktree', str(tree), '--pr', receipt['pr'], '--head', head, '--out', str(tmp_path / 'cli')]) == 0
    assert ci.bound(ci.discover_binding(receipt['repo'], head, tmp_path))['head_sha'] == head
    (tree / 'dirty').write_text('dirt')
    with pytest.raises(ValueError, match='dirty_tree'):
        runner.main(['--worktree', str(tree), '--pr', receipt['pr'], '--head', head, '--out', str(tmp_path / 'dirty-cli')])
    assert not (tmp_path / 'dirty-cli/LOCAL-CI.json').exists()


def test_unregistered_receipt_cannot_be_selected(tmp_path):
    ci, receipt, head, _, _ = fixture_receipt(tmp_path)
    remote = {'headRefOid': head, 'files': [{'path': 'fixture.py'}]}
    with pytest.raises(ValueError, match='receipt_not_registered'):
        ci.admit(remote, repo=receipt['repo'], pr=receipt['pr'], head=head, local_binding={'path': 'other', 'sha256': 'a' * 64},
                 authority_root=tmp_path, gh=locked_gh(head))


def test_release_build_binding_revalidates_local_ci_and_keeps_hold_gate(tmp_path, monkeypatch):
    import glp_affiliate_release as release
    ci, receipt, head, tree, _ = fixture_receipt(tmp_path, repo='izaiahat/glp-site-candidate')
    monkeypatch.setattr(release, 'ROOT', tmp_path)
    original = ci.admit
    monkeypatch.setattr(ci, 'admit', lambda remote, **kw: original(remote, **{**kw, 'gh': locked_gh(head)}))
    remote = {'state': 'MERGED', 'url': receipt['pr'], 'headRefOid': head,
              'files': [{'path': 'fixture.py'}], 'statusCheckRollup': []}
    admission = ci.admit(remote, repo=receipt['repo'], pr=receipt['pr'], head=head, authority_root=tmp_path)
    actual_output = subprocess.check_output
    monkeypatch.setattr(subprocess, 'check_output', lambda argv, **kw: json.dumps(remote) if argv[0] == 'gh' else actual_output(argv, **kw))
    rows = [{'file': 'index.html', 'sha256': 'b' * 64}]
    absence = tmp_path / 'absence.json'
    absence.write_text(json.dumps({'status': 'PASS', 'unaccounted': 0, 'remaining_unmapped': 0,
                                   'evidenced_no_live_offer_presence': 0, 'rows': []}))
    records = {
        'build': {'head': head, 'exit_code': 0, 'files': rows},
        'ci': {**remote, 'ci_admission': admission},
        'design': {'head': head, 'exit_code': 0,
                   'checks': ['affiliate-relationship-sync', 'raw-held-output', 'native-build-validators'],
                   'unmapped_adverse_evidence': release.pin(absence),
                   'raw_held': {'held': [{'id': 'fella-health'}], 'htmlFiles': 1, 'jsonFiles': 1, 'anchors': [], 'routes': []}}}
    def gate():
        refs = {}
        for key, value in records.items():
            path = tmp_path / (key + '.json')
            path.write_text(json.dumps(value))
            refs[key] = release.pin(path)
        return {'head': head, 'scope': 'operator-approved-affiliate-sync', 'release_authorized': False, 'evidence': refs}
    release.validate_build_evidence(gate(), rows)
    records['design']['raw_held']['anchors'] = [{'id': 'fella-health'}]
    with pytest.raises(ValueError, match='hold_census_nonpass'):
        release.validate_build_evidence(gate(), rows)
    records['design']['raw_held']['anchors'] = []
    (tree / 'dirt').write_text('dirt')
    with pytest.raises(ValueError, match='dirty_tree'):
        release.validate_build_evidence(gate(), rows)


@pytest.mark.parametrize('glob', ['**/*.py', '*.p?', '*.py[ab]', '{a,b}.py', '!fixture.py'])
def test_unsupported_glob_never_silently_omits_commands(tmp_path, glob):
    ci, _, _, tree, _ = fixture_receipt(tmp_path)
    path = tree / '.github/workflows/ci.yml'
    text = path.read_text().replace('on: pull_request', 'on:\n  pull_request:\n    paths: [' + json.dumps(glob) + ']')
    path.write_text(text)
    subprocess.run(['git', '-C', str(tree), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(tree), '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'glob'], check=True, capture_output=True)
    with pytest.raises(ValueError, match='unsupported_workflow_glob'):
        ci.workflow_plan(tree, ci.git(tree, 'rev-parse', 'HEAD'), ['fixture.py'])


def test_receipt_for_another_remote_head_refuses_before_fallback(tmp_path):
    ci, receipt, head, _, _ = fixture_receipt(tmp_path)
    with pytest.raises(ValueError, match='remote_head_mismatch'):
        ci.admit({'headRefOid': 'c' * 40}, repo=receipt['repo'], pr=receipt['pr'], head=head,
                 authority_root=tmp_path, gh=locked_gh(head))


def test_independent_jobs_do_not_admit_ignored_dependency_leak(tmp_path):
    workflow = """on: pull_request
jobs:
  seed:
    steps:
      - run: mkdir -p .venv; touch .venv/leaked-dependency
  check:
    steps:
      - run: test -f .venv/leaked-dependency
"""
    ci, receipt, head, _, _ = fixture_receipt(tmp_path, workflow_text=workflow,
                                            files={'.gitignore': '.venv/\n'})
    assert receipt['status'] == 'FAIL'
    assert receipt['results'][0]['exit_code'] == 0
    assert receipt['results'][1]['exit_code'] != 0
    with pytest.raises(ValueError, match='commands_not_passed'):
        ci.validate_local(receipt, repo=receipt['repo'], pr=receipt['pr'], head=head,
                          changed_paths=['fixture.py'], authority_root=tmp_path)


def test_workflow_default_cwd_cannot_admit_wrong_root_script(tmp_path):
    workflow = """on: pull_request
defaults:
  run:
    working-directory: subdir
jobs:
  test:
    steps:
      - run: bash check.sh
"""
    ci, receipt, head, _, _ = fixture_receipt(tmp_path, workflow_text=workflow,
        files={'check.sh': 'exit 0\n', 'subdir/check.sh': 'exit 9\n'})
    assert receipt['status'] == 'FAIL'
    assert receipt['results'][0]['exit_code'] == 9
    with pytest.raises(ValueError, match='commands_not_passed'):
        ci.validate_local(receipt, repo=receipt['repo'], pr=receipt['pr'], head=head,
                          changed_paths=['fixture.py'], authority_root=tmp_path)


def test_runner_temp_is_writable_and_job_private(tmp_path):
    workflow = """on: pull_request
jobs:
  seed:
    steps:
      - run: test -n "$RUNNER_TEMP"; touch "$RUNNER_TEMP/marker"
      - run: test -f "$RUNNER_TEMP/marker"
  check:
    steps:
      - run: test -n "$RUNNER_TEMP"; test ! -e "$RUNNER_TEMP/marker"; test -w "$RUNNER_TEMP"
"""
    _, receipt, _, _, _ = fixture_receipt(tmp_path, workflow_text=workflow)
    assert receipt['status'] == 'PASS'


@pytest.mark.parametrize('kind', ['workflow', 'job'])
def test_unsupported_default_shell_refuses(tmp_path, kind):
    workflow = "on: pull_request\n"
    if kind == 'workflow':
        workflow += "defaults:\n  run:\n    shell: sh\n"
    workflow += "jobs:\n  test:\n"
    if kind == 'job':
        workflow += "    defaults:\n      run:\n        shell: sh\n"
    workflow += "    steps:\n      - run: 'true'\n"
    with pytest.raises(ValueError, match='unsupported_shell'):
        fixture_receipt(tmp_path, workflow_text=workflow)


def test_default_cwd_precedence_and_supported_shell_override(tmp_path):
    workflow = """on: pull_request
defaults:
  run:
    working-directory: workflow-dir
    shell: sh
jobs:
  test:
    defaults:
      run:
        working-directory: job-dir
        shell: bash
    steps:
      - run: test "$PWD" = "$GITHUB_WORKSPACE/job-dir"
      - run: test "$PWD" = "$GITHUB_WORKSPACE/step-dir"
        working-directory: step-dir
"""
    ci, receipt, head, _, _ = fixture_receipt(tmp_path, workflow_text=workflow,
        files={name + '/.keep': '' for name in ('workflow-dir', 'job-dir', 'step-dir')})
    assert receipt['status'] == 'PASS'
    ci.validate_local(receipt, repo=receipt['repo'], pr=receipt['pr'], head=head,
                      changed_paths=['fixture.py'], authority_root=tmp_path)


@pytest.mark.parametrize('change', ['runtime', 'job', 'log'])
def test_runtime_job_evidence_tamper_refuses(tmp_path, change):
    ci, receipt, head, _, _ = fixture_receipt(tmp_path)
    if change == 'runtime':
        receipt['jobs'][0]['runtimes']['python']['version'] = '0.0.0'
    elif change == 'job':
        receipt['jobs'] = []
    else:
        Path(receipt['jobs'][0]['setup_log']['path']).write_text('changed')
    with pytest.raises(ValueError, match='runtime_evidence_changed|job_evidence_missing|setup_log_drift'):
        ci.validate_local(receipt, repo=receipt['repo'], pr=receipt['pr'], head=head,
                          changed_paths=['fixture.py'], authority_root=tmp_path)


def test_declared_setup_python_positive_binds_actual_version(tmp_path):
    workflow = """on: pull_request
jobs:
  test:
    steps:
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065
        with:
          python-version: '3.12'
      - run: python3 -c 'import sys; assert sys.version_info[:2] == (3, 12)'
"""
    ci, receipt, head, _, _ = fixture_receipt(tmp_path, workflow_text=workflow)
    assert receipt['status'] == 'PASS'
    assert receipt['jobs'][0]['runtimes']['python']['version'].startswith('3.12.')
    ci.validate_local(receipt, repo=receipt['repo'], pr=receipt['pr'], head=head,
                      changed_paths=['fixture.py'], authority_root=tmp_path)


def test_jobs_have_private_home_and_python_packages(tmp_path):
    workflow = """on: pull_request
jobs:
  seed:
    steps:
      - run: |
          python -c 'import site, pathlib; p=pathlib.Path(site.getsitepackages()[0]); (p / "leaked_dependency.py").write_text("value=1")'
          touch "$HOME/marker"
      - run: python -c 'import leaked_dependency'; test -f "$HOME/marker"
  check:
    steps:
      - run: python -c 'import importlib.util; assert importlib.util.find_spec("leaked_dependency") is None'; test ! -e "$HOME/marker"
"""
    ci, receipt, head, _, _ = fixture_receipt(tmp_path, workflow_text=workflow)
    assert receipt['status'] == 'PASS'
    ci.validate_local(receipt, repo=receipt['repo'], pr=receipt['pr'], head=head,
                      changed_paths=['fixture.py'], authority_root=tmp_path)


@pytest.mark.parametrize('depth', [0, 1])
def test_checkout_depth_is_executed_and_bound(tmp_path, depth):
    expected = 'true' if depth == 1 else 'false'
    workflow = f"""on: pull_request
jobs:
  test:
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
        with:
          fetch-depth: {depth}
          persist-credentials: false
      - run: test "$(git rev-parse --is-shallow-repository)" = {expected}
"""
    ci, receipt, head, _, _ = fixture_receipt(tmp_path, workflow_text=workflow)
    assert receipt['status'] == 'PASS'
    ci.validate_local(receipt, repo=receipt['repo'], pr=receipt['pr'], head=head,
                      changed_paths=['fixture.py'], authority_root=tmp_path)


@pytest.mark.parametrize('options,reason', [
    ('node-version: "0"', 'declared_runtime_mismatch'),
    ('node-version: "22"\n          cache: npm', 'unsupported_setup_version_or_options'),
])
def test_node_setup_mismatch_or_unsupported_inputs_refuse(tmp_path, options, reason):
    workflow = f"""on: pull_request
jobs:
  test:
    steps:
      - uses: actions/setup-node@1111111111111111111111111111111111111111
        with:
          {options}
      - run: 'true'
"""
    with pytest.raises(ValueError, match=reason):
        fixture_receipt(tmp_path, workflow_text=workflow)


def test_unsupported_github_file_command_refuses(tmp_path):
    with pytest.raises(ValueError, match='unsupported_runner_file_command'):
        fixture_receipt(tmp_path, command='echo X=1 >> "$GITHUB_ENV"')



def test_job_tmpdir_supports_nested_unix_socket_fixture(tmp_path):
    # Native Chrome fixtures nest tempfile + profile + singleton socket paths.
    command = "python -c 'import os,pathlib,socket; p=pathlib.Path(os.environ[\"TMPDIR\"])/ (\"nested-\"+\"x\"*20)/ (\"profile-\"+\"x\"*8); p.mkdir(parents=True); s=socket.socket(socket.AF_UNIX); s.bind(str(p/\"SingletonSocket\")); s.close()'"
    ci, receipt, head, _, _ = fixture_receipt(tmp_path, command=command)
    assert receipt['status'] == 'PASS'
    ci.validate_local(receipt, repo=receipt['repo'], pr=receipt['pr'], head=head,
                      changed_paths=['fixture.py'], authority_root=tmp_path)
