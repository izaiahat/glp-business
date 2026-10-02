from __future__ import annotations

import hashlib
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE).decode().strip()


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'config', 'user.name', 'Fixture')
    (repo / 'scripts').mkdir()
    (repo / 'scripts/job.py').write_bytes(b'print("fixture")\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'fixture')
    head = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'update-ref', 'refs/remotes/origin/main', head)
    return repo, head


def provenance():
    spec = importlib.util.find_spec('glp_ship_provenance')
    assert spec is not None, 'provenance module is not implemented'
    return importlib.import_module('glp_ship_provenance')


def test_pins_derive_from_committed_bytes(checkout):
    repo, head = checkout
    assert provenance().derive_pins(repo, head, ['scripts/job.py']) == {
        'scripts/job.py': hashlib.sha256(b'print("fixture")\n').hexdigest()
    }


@pytest.mark.parametrize('kind', ['dirty', 'missing', 'symlink', 'directory_symlink', 'unsafe', 'short_sha', 'tree'])
def test_pins_reject_nonexact_inputs(checkout, kind):
    repo, head = checkout
    path = 'scripts/job.py'
    if kind == 'dirty':
        (repo / path).write_bytes(b'changed\n')
    elif kind == 'missing':
        (repo / path).unlink()
    elif kind == 'symlink':
        original = repo / path
        original.rename(repo / 'other.py')
        original.symlink_to('../other.py')
    elif kind == 'directory_symlink':
        (repo / 'scripts').rename(repo / 'other')
        (repo / 'scripts').symlink_to('other', target_is_directory=True)
    elif kind == 'unsafe':
        path = 'scripts/../scripts/job.py'
    elif kind == 'short_sha':
        head = head[:8]
    elif kind == 'tree':
        path = 'scripts'
    with pytest.raises(RuntimeError):
        provenance().derive_pins(repo, head, [path])


def test_pins_reject_committed_symlink(checkout):
    repo, _ = checkout
    (repo / 'link.py').symlink_to('scripts/job.py')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'symlink')
    with pytest.raises(RuntimeError):
        provenance().derive_pins(repo, git(repo, 'rev-parse', 'HEAD'), ['link.py'])


@pytest.fixture
def github(checkout):
    _, merge = checkout
    pr_head = 'a' * 40
    pr = {
        'number': 31, 'merged': True, 'merged_at': '2026-10-02T01:00:00Z',
        'merge_commit_sha': merge, 'base': {'ref': 'main', 'repo': {'full_name': 'izaiahat/glp-business'}},
        'head': {'sha': pr_head, 'ref': 'fix/fixture', 'repo': {'full_name': 'izaiahat/glp-business'}},
    }
    run = {
        'id': 70, 'workflow_id': 1, 'run_attempt': 1, 'run_number': 100, 'head_sha': pr_head,
        'event': 'pull_request', 'status': 'completed', 'conclusion': 'success',
        'path': '.github/workflows/business-source-ci.yml',
        'repository': {'full_name': 'izaiahat/glp-business'}, 'head_branch': 'fix/fixture',
        'head_repository': {'full_name': 'izaiahat/glp-business'},
        'pull_requests': [{'number': 31, 'head': {'sha': pr_head}}],
    }
    # Native commit->PR read for the exact head; absent unless a test supplies it.
    data = {'pr': pr, 'runs': [run], 'run': run, 'calls': [], 'head_pulls': []}
    def api(endpoint):
        data['calls'].append(endpoint)
        base = 'repos/izaiahat/glp-business/'
        assert endpoint.startswith(base)
        tail = endpoint[len(base):].split('?')[0]
        if tail == f'commits/{merge}/pulls':
            return [{'number': 31}]
        if tail == 'pulls/31':
            return data['pr']
        if tail == f'commits/{pr_head}/pulls':
            return data['head_pulls']
        if tail == 'actions/workflows/business-source-ci.yml/runs':
            assert f'head_sha={pr_head}' in endpoint
            assert 'event=pull_request' in endpoint
            return {'workflow_runs': data['runs']}
        if tail == 'actions/runs':
            return {'workflow_runs': data.get('all_runs', data['runs'])}
        if tail.startswith('actions/runs/'):
            return data['run']
        raise AssertionError(endpoint)
    data['api'] = api
    return data


def test_verify_exact_merged_pr_and_head_ci(checkout, github):
    repo, merge = checkout
    module = provenance()
    assert hasattr(module, 'verify_merge'), 'merged PR verifier is not implemented'
    assert module.verify_merge(repo, merge, api=github['api']) == {
        'merge_sha': merge, 'pr_number': 31, 'pr_head': 'a' * 40,
        'ci_run_id': 70, 'ci_attempt': 1,
    }


@pytest.mark.parametrize('field,value', [
    ('merged', False), ('merged_at', None), ('merge_commit_sha', 'b' * 40),
    ('base', {'ref': 'other', 'repo': {'full_name': 'izaiahat/glp-business'}}),
    ('base', {'ref': 'main', 'repo': {'full_name': 'other/repo'}}),
    ('head', {'sha': 'short'}), ('number', 32),
])
def test_verify_rejects_wrong_pr(checkout, github, field, value):
    repo, merge = checkout
    github['pr'][field] = value
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge, api=github['api'])


@pytest.mark.parametrize('field,value', [
    ('head_sha', 'b' * 40), ('status', 'in_progress'), ('conclusion', 'failure'),
    ('event', 'push'), ('path', '.github/workflows/other.yml'),
    ('repository', {'full_name': 'other/repo'}), ('pull_requests', []),
    ('run_attempt', 0), ('id', 0),
])
def test_verify_rejects_wrong_ci(checkout, github, field, value):
    repo, merge = checkout
    github['run'][field] = value
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge, api=github['api'])


def test_verify_requires_main_ancestry_before_network(checkout, github):
    repo, _ = checkout
    (repo / 'new').write_text('unmerged')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'unmerged')
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, git(repo, 'rev-parse', 'HEAD'), api=github['api'])
    assert github['calls'] == []


def test_verify_rejects_short_sha_before_network(checkout, github):
    repo, merge = checkout
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge[:8], api=github['api'])
    assert github['calls'] == []


def test_verify_refuses_stale_success_when_new_run_failed(checkout, github):
    repo, merge = checkout
    old = dict(github['run'])
    github['runs'].append({**old, 'id': 71, 'run_number': 101, 'conclusion': 'failure'})
    def api(endpoint):
        if endpoint.endswith('actions/runs/71'):
            return github['runs'][1]
        return github['api'](endpoint)
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge, api=api)


def test_verify_missing_ci_fails_closed(checkout, github):
    repo, merge = checkout
    github['runs'] = []
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge, api=github['api'])


def test_verify_checks_later_ci_pages(checkout, github):
    repo, merge = checkout
    old = dict(github['run'])
    def api(endpoint):
        if 'actions/workflows/' in endpoint:
            if endpoint.endswith('&page=1'):
                return {'workflow_runs': [dict(old, id=i + 1, run_number=i + 1) for i in range(100)]}
            assert endpoint.endswith('&page=2')
            return {'workflow_runs': [dict(old, id=101, run_number=101, conclusion='failure')]}
        if endpoint.endswith('actions/runs/100'):
            return dict(old, id=100, run_number=100)
        if endpoint.endswith('actions/runs/101'):
            return dict(old, id=101, run_number=101, conclusion='failure')
        return github['api'](endpoint)
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge, api=api)


def test_verify_finds_exact_pr_on_later_page(checkout, github):
    repo, merge = checkout
    def api(endpoint):
        if f'commits/{merge}/pulls' in endpoint:
            if endpoint.endswith('&page=1'):
                return [{'number': number} for number in range(100, 200)]
            return [{'number': 31}]
        if '/pulls/' in endpoint and not endpoint.endswith('/31'):
            number = int(endpoint.rsplit('/', 1)[1])
            return dict(github['pr'], number=number, merged=False)
        return github['api'](endpoint)
    assert provenance().verify_merge(repo, merge, api=api)['pr_number'] == 31


def test_github_transport_is_fixed_read_only_and_bounded(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}', stderr='')
    monkeypatch.setattr(subprocess, 'run', run)
    assert provenance().github_json('repos/izaiahat/glp-business/pulls/31') == {'ok': True}
    command, kwargs = calls[0]
    assert command[:7] == ['gh', 'api', '--hostname', 'github.com', '--method', 'GET', 'repos/izaiahat/glp-business/pulls/31']
    assert kwargs['timeout'] == 45
    assert kwargs.get('shell', False) is False


@pytest.mark.parametrize('endpoint', ['https://evil.invalid/data', 'repos/other/repo/pulls/31',
                                      'repos/izaiahat/glp-business/../secrets'])
def test_github_transport_refuses_foreign_paths(endpoint, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('must reject before network')
    monkeypatch.setattr(subprocess, 'run', denied)
    with pytest.raises(RuntimeError):
        provenance().github_json(endpoint)


def test_pins_reject_tree_id_even_with_valid_blobs(checkout):
    repo, _ = checkout
    tree = git(repo, 'rev-parse', 'HEAD^{tree}')
    with pytest.raises(RuntimeError):
        provenance().derive_pins(repo, tree, ['scripts/job.py'])


def test_pins_reject_checkout_symlink(checkout):
    repo, head = checkout
    alias = repo.parent / 'alias'
    alias.symlink_to(repo, target_is_directory=True)
    with pytest.raises(RuntimeError):
        provenance().derive_pins(alias, head, ['scripts/job.py'])


def test_pins_refuse_unknown_untracked_source(checkout):
    repo, head = checkout
    (repo / 'unknown.py').write_text('new')
    with pytest.raises(RuntimeError):
        provenance().derive_pins(repo, head, ['unknown.py'])


def test_current_rerun_readback_overrides_list_success(checkout, github):
    repo, merge = checkout
    github['run'] = dict(github['run'], run_attempt=2, status='in_progress', conclusion=None)
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge, api=github['api'])


def test_current_successful_rerun_binds_new_attempt(checkout, github):
    repo, merge = checkout
    github['run'] = dict(github['run'], run_attempt=2)
    assert provenance().verify_merge(repo, merge, api=github['api'])['ci_attempt'] == 2


@pytest.mark.parametrize('failure', ['invalid_json', 'failed_command', 'timeout'])
def test_github_transport_sanitizes_errors(monkeypatch, failure):
    def run(command, **kwargs):
        if failure == 'failed_command':
            raise subprocess.CalledProcessError(1, command, stderr='secret diagnostic')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, 45, stderr='secret diagnostic')
        return subprocess.CompletedProcess(command, 0, stdout='secret diagnostic', stderr='')
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='^ship_github_read_failed$'):
        provenance().github_json('repos/izaiahat/glp-business/pulls/31')


def test_malformed_provider_proof_fails_closed(checkout):
    repo, merge = checkout
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge, api=lambda endpoint: None)


@pytest.mark.parametrize('status,conclusion', [('completed', 'failure'), ('queued', None),
                                               ('in_progress', None), ('completed', 'cancelled')])
def test_other_exact_head_workflow_must_be_green(checkout, github, status, conclusion):
    repo, merge = checkout
    other = dict(github['run'], id=80, workflow_id=2, run_number=1,
                 path='.github/workflows/other.yml', status=status, conclusion=conclusion)
    github['all_runs'] = [github['run'], other]
    def api(endpoint):
        if endpoint.endswith('actions/runs/80'):
            return other
        return github['api'](endpoint)
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge, api=api)


def test_unrelated_pr_workflow_does_not_block(checkout, github):
    repo, merge = checkout
    other = dict(github['run'], id=80, workflow_id=2, conclusion='failure',
                 pull_requests=[{'number': 32, 'head': {'sha': 'a' * 40}}])
    github['all_runs'] = [github['run'], other]
    assert provenance().verify_merge(repo, merge, api=github['api'])['ci_run_id'] == 70


def test_receipt_binds_current_business_attempt_after_all_checks(checkout, github):
    repo, merge = checkout
    reads = 0
    def api(endpoint):
        nonlocal reads
        if endpoint.endswith('actions/runs/70'):
            reads += 1
            return dict(github['run'], run_attempt=reads)
        return github['api'](endpoint)
    assert provenance().verify_merge(repo, merge, api=api)['ci_attempt'] == 2


# --- Omitted run association (F07 / GLP-44, PR166) ---------------------------
# Fixture of real observed metadata SHAPES: PR166 head 01a94111..., Actions run
# 36973008150 (check suite 100147445103) completed/success on the exact head,
# event pull_request, canonical repository/head_repository, yet BOTH run detail
# and check-suite returned pull_requests: []. GET commits/<head>/pulls returned
# exactly PR166 (merged, base main, exact merge_commit_sha). The merge SHA here
# is the local fixture commit so Git ancestry stays real.
PR166_HEAD = '01a941118e212de020d3d48798615a6b9496b58d'
PR166_BRANCH = 'fix/f07-route-coverage-20261002'


@pytest.fixture
def pr166(checkout):
    _, merge = checkout
    repo_name = {'full_name': 'izaiahat/glp-business'}
    pr = {'number': 166, 'merged': True, 'merged_at': '2026-10-02T07:26:33Z',
          'merge_commit_sha': merge, 'base': {'ref': 'main', 'repo': dict(repo_name)},
          'head': {'sha': PR166_HEAD, 'ref': PR166_BRANCH, 'repo': dict(repo_name)}}
    run = {'id': 36973008150, 'check_suite_id': 100147445103, 'workflow_id': 1,
           'run_attempt': 1, 'run_number': 900, 'head_sha': PR166_HEAD,
           'head_branch': PR166_BRANCH, 'event': 'pull_request', 'status': 'completed',
           'conclusion': 'success', 'path': '.github/workflows/business-source-ci.yml',
           'repository': dict(repo_name), 'head_repository': dict(repo_name),
           'pull_requests': []}
    data = {'pr': pr, 'run': run, 'runs': [run], 'all_runs': [run], 'details': {},
            'head_pulls': [dict(pr)], 'calls': []}
    def api(endpoint):
        data['calls'].append(endpoint)
        base = 'repos/izaiahat/glp-business/'
        assert endpoint.startswith(base)
        tail = endpoint[len(base):].split('?')[0]
        if tail == f'commits/{merge}/pulls':
            return [{'number': 166}]
        if tail == 'pulls/166':
            return data['pr']
        if tail == f'commits/{PR166_HEAD}/pulls':
            return data['head_pulls']
        if tail == 'actions/workflows/business-source-ci.yml/runs':
            assert f'head_sha={PR166_HEAD}' in endpoint and 'event=pull_request' in endpoint
            return {'workflow_runs': data['runs']}
        if tail == 'actions/runs':
            return {'workflow_runs': data['all_runs']}
        if tail.startswith('actions/runs/'):
            run_id = int(tail.rsplit('/', 1)[1])
            return data['details'].get(run_id, data['run'] if run_id == data['run']['id'] else None)
        raise AssertionError(endpoint)
    data['api'] = api
    return data


def test_pr166_omitted_association_binds_only_with_native_corroboration(checkout, pr166):
    repo, merge = checkout
    assert provenance().verify_merge(repo, merge, api=pr166['api']) == {
        'merge_sha': merge, 'pr_number': 166, 'pr_head': PR166_HEAD,
        'ci_run_id': 36973008150, 'ci_attempt': 1,
    }
    head_reads = [c for c in pr166['calls'] if f'commits/{PR166_HEAD}/pulls' in c]
    assert head_reads and all('per_page=100' in c for c in head_reads)


def test_empty_association_without_corroboration_still_refuses(checkout, github):
    repo, merge = checkout
    github['run']['pull_requests'] = []
    with pytest.raises(RuntimeError, match='^ship_ci_association_uncorroborated$'):
        provenance().verify_merge(repo, merge, api=github['api'])


def _pr(pr166, **changes):
    row = dict(pr166['pr'])
    for key, value in changes.items():
        row[key] = value
    return row


@pytest.mark.parametrize('case', [
    'none', 'ambiguous', 'other_pr', 'wrong_head', 'wrong_merge', 'unmerged',
    'wrong_base_ref', 'wrong_base_repo', 'fork_head',
])
def test_omitted_association_refuses_missing_or_bad_corroboration(checkout, pr166, case):
    repo, merge = checkout
    head = pr166['pr']['head']
    pr166['head_pulls'] = {
        'none': [],
        'ambiguous': [pr166['pr'], _pr(pr166, number=167)],
        'other_pr': [_pr(pr166, number=167)],
        'wrong_head': [_pr(pr166, head=dict(head, sha='b' * 40))],
        'wrong_merge': [_pr(pr166, merge_commit_sha='b' * 40)],
        'unmerged': [_pr(pr166, merged_at=None)],
        'wrong_base_ref': [_pr(pr166, base={'ref': 'other', 'repo': pr166['pr']['base']['repo']})],
        'wrong_base_repo': [_pr(pr166, base={'ref': 'main', 'repo': {'full_name': 'other/repo'}})],
        'fork_head': [_pr(pr166, head=dict(head, repo={'full_name': 'fork/glp-business'}))],
    }[case]
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge, api=pr166['api'])


@pytest.mark.parametrize('field,value', [
    ('head_branch', 'other-branch'), ('head_repository', {'full_name': 'fork/glp-business'}),
    ('repository', {'full_name': 'other/repo'}), ('head_sha', 'b' * 40), ('event', 'push'),
    ('conclusion', 'failure'), ('status', 'in_progress'), ('path', '.github/workflows/other.yml'),
    ('pull_requests', None), ('pull_requests', [{'number': 167, 'head': {'sha': PR166_HEAD}}]),
])
def test_omitted_association_run_counterexamples_refuse(checkout, pr166, field, value):
    repo, merge = checkout
    pr166['run'][field] = value
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge, api=pr166['api'])


@pytest.mark.parametrize('status,conclusion', [('completed', 'failure'), ('queued', None),
                                               ('in_progress', None), (None, None)])
def test_omitted_association_cannot_hide_other_workflow(checkout, pr166, status, conclusion):
    repo, merge = checkout
    other = dict(pr166['run'], id=80, workflow_id=2, run_number=1,
                 path='.github/workflows/other.yml', status=status, conclusion=conclusion)
    pr166['all_runs'] = [pr166['run'], other]
    pr166['details'][80] = other
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge, api=pr166['api'])


def test_uncorroborated_empty_workflow_is_not_skipped(checkout, github):
    repo, merge = checkout
    # The business run is explicitly associated, but another exact-head
    # workflow omits association and failed: unknown must not read as unrelated.
    other = dict(github['run'], id=80, workflow_id=2, run_number=1, pull_requests=[],
                 path='.github/workflows/other.yml', conclusion='failure')
    github['all_runs'] = [github['run'], other]
    with pytest.raises(RuntimeError, match='^ship_ci_association_uncorroborated$'):
        provenance().verify_merge(repo, merge, api=github['api'])


def test_omitted_association_latest_rerun_still_governs(checkout, pr166):
    repo, merge = checkout
    newer = dict(pr166['run'], id=36973008151, run_number=901, conclusion='failure')
    pr166['runs'] = [pr166['run'], newer]
    pr166['all_runs'] = [pr166['run'], newer]
    pr166['details'][newer['id']] = newer
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge, api=pr166['api'])
    pr166['details'][pr166['run']['id']] = dict(pr166['run'], run_attempt=2, status='in_progress',
                                                conclusion=None)
    pr166['runs'] = pr166['all_runs'] = [pr166['run']]
    with pytest.raises(RuntimeError):
        provenance().verify_merge(repo, merge, api=pr166['api'])
