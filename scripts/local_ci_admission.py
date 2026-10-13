"""Shared fail-closed exact-head local-CI evidence contract (no release effects)."""
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path('/home/ubuntu/business')
HERE = Path(__file__).resolve().parent
DECISION = 'ops/linear/approvals/OPERATOR-DECISION-20261013-public-repos-local-ci.md'
REGISTRY = HERE.parent / 'ops/local-ci-runner.json'
SCHEMA = 'glp-local-ci-v1'


def require(ok, reason):
    if not ok:
        raise ValueError('local_ci_' + reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def pin(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'regular_evidence_required')
    return {'path': str(path.resolve()), 'sha256': digest(path.read_bytes())}


def bound(binding):
    path = Path(binding['path'])
    require(pin(path) == binding, 'evidence_drift')
    return json.loads(path.read_bytes())


def write_new(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as f:
        json.dump(data, f, indent=2)
        f.write('\n')


def gh_json(*argv):
    return json.loads(subprocess.check_output(['gh', *argv], text=True, timeout=60))


def git(tree, *argv, strip=True):
    value = subprocess.check_output(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                                     '-C', str(tree), *argv], text=True, timeout=60)
    return value.strip() if strip else value


def clean_head(tree, head):
    require(re.fullmatch(r'[0-9a-f]{40}', head or ''), 'full_head_required')
    tree = Path(tree)
    require(git(tree, 'rev-parse', 'HEAD') == head, 'head_mismatch')
    require(not git(tree, 'status', '--porcelain', '--untracked-files=all'), 'dirty_tree')
    require(not git(tree, 'diff', 'HEAD', '--'), 'dirty_tree')
    require(all(row.startswith('H ') for row in git(tree, 'ls-files', '-v').splitlines()), 'hidden_index_flags')
    return {'path': str(tree.resolve()), 'head_sha': head, 'tree_sha': git(tree, 'rev-parse', 'HEAD^{tree}'),
            'clean': True}


def producer():
    registration = json.loads(REGISTRY.read_bytes())
    require(registration['schema'] == 'glp-local-ci-runner-registration-v1' and
            registration['runner'] == 'scripts/local_ci_runner.py' and
            registration['admission'] == 'scripts/local_ci_admission.py', 'runner_unregistered')
    return {key: {'file': relative, 'sha256': digest((HERE.parent / relative).read_bytes())}
            for key, relative in (('registration', 'ops/local-ci-runner.json'),
                                  ('runner', 'scripts/local_ci_runner.py'),
                                  ('admission', 'scripts/local_ci_admission.py'))}


def resolve_env(value, head, tree):
    require(isinstance(value, str), 'unsupported_environment')
    def expression(match):
        terms = [t.strip() for t in match[1].split('||')]
        if terms == ['github.workspace']:
            return str(Path(tree).resolve())
        require(all(t in ('github.event.pull_request.head.sha', 'github.sha', 'inputs.source_sha',
                          'env.EXPECTED_SHA') for t in terms), 'unsupported_workflow_expression')
        return head
    return re.sub(r'\$\{\{\s*(.*?)\s*\}\}', expression, value)


def workflow_plan(tree, head, changed_paths):
    """Complete applicable workflow run blocks; unsupported semantics refuse."""
    import yaml
    require(isinstance(changed_paths, list) and changed_paths and all(isinstance(p, str) and p for p in changed_paths),
            'pr_files_missing')
    plan = []
    for file in git(tree, 'ls-files', '.github/workflows').splitlines():
        if not file.endswith(('.yml', '.yaml')):
            continue
        raw = git(tree, 'show', head + ':' + file, strip=False).encode()
        workflow = yaml.safe_load(raw)
        triggers = workflow.get('on', workflow.get(True))
        if isinstance(triggers, str):
            triggers = {triggers: None}
        if isinstance(triggers, list):
            triggers = dict.fromkeys(triggers)
        if not isinstance(triggers, dict) or 'pull_request' not in triggers:
            continue
        event = triggers['pull_request'] or {}
        paths = event.get('paths')
        ignored = event.get('paths-ignore', [])
        require(not (paths and ignored), 'unsupported_path_filters')
        def matches(path, patterns):
            require(not any(p.startswith('!') or '**/' in p or any(c in p for c in '?[]{}+') for p in patterns),
                    'unsupported_workflow_glob')
            return any(fnmatch.fnmatchcase(path, p) for p in patterns)
        if paths and not any(matches(p, paths) for p in changed_paths):
            continue
        if ignored and all(matches(p, ignored) for p in changed_paths):
            continue
        for job_name, job in workflow['jobs'].items():
            require('strategy' not in job and 'if' not in job and 'container' not in job and
                    'services' not in job, 'unsupported_job_semantics')
            dependencies = []
            for step in job['steps']:
                if step.get('uses', '').startswith('actions/checkout@') and step.get('with', {}).get('repository'):
                    dep = step['with']
                    require(dep['repository'] == 'izaiahat/hermes-agent' and re.fullmatch(r'[0-9a-f]{40}', str(dep.get('ref', '')))
                            and re.fullmatch(r'[A-Za-z0-9_-]+', dep.get('path', '')), 'unsupported_checkout_dependency')
                    dependencies.append({'repo': dep['repository'], 'head': dep['ref'], 'path': dep['path']})
            for index, step in enumerate(job['steps']):
                if 'uses' in step:
                    require(re.fullmatch(r'actions/(checkout|setup-node|setup-python)@[0-9a-f]{40}', step['uses']),
                            'unsupported_action')
                    continue
                require('run' in step and step.get('if', 'always()') == 'always()', 'unsupported_step_semantics')
                require(step.get('shell', 'bash') == 'bash', 'unsupported_shell')
                env = {'EXPECTED_HEAD': head, 'EXPECTED_SHA': head}
                for source in (workflow.get('env', {}), job.get('env', {}), step.get('env', {})):
                    env.update({k: resolve_env(v, head, tree) for k, v in source.items()})
                command = step['run']
                require(isinstance(command, str) and command.strip() and '${{' not in command,
                        'unsupported_command')
                cwd = step.get('working-directory', job.get('defaults', {}).get('run', {}).get('working-directory', '.'))
                require(isinstance(cwd, str) and not Path(cwd).is_absolute() and '..' not in Path(cwd).parts,
                        'unsupported_working_directory')
                plan.append({'workflow': file, 'workflow_sha256': digest(raw), 'job': job_name, 'step': index,
                             'name': step.get('name', str(index)), 'command': command, 'env': env, 'cwd': cwd,
                             'dependencies': dependencies})
    require(plan, 'workflow_commands_missing')
    return plan


def check_dependencies(tree, plan, *, prepare=False):
    seen = set()
    for row in plan:
        for dep in row['dependencies']:
            key = (dep['repo'], dep['head'], dep['path'])
            if key in seen:
                continue
            seen.add(key)
            target = Path(tree) / dep['path']
            require(not target.is_symlink(), 'checkout_dependency_symlink')
            if prepare and not target.exists():
                subprocess.run(['git', 'init', str(target)], check=True, capture_output=True)
                git(target, 'remote', 'add', 'origin', f'https://github.com/{dep["repo"]}.git')
                git(target, 'fetch', '--depth=1', 'origin', dep['head'])
                git(target, 'checkout', '--detach', 'FETCH_HEAD')
            clean_head(target, dep['head'])


def produce(tree, repo, pr, head, changed_paths, out, *, authority_root=ROOT):
    require(repo in json.loads(REGISTRY.read_bytes())['repositories'], 'repository_unregistered')
    tree, out = Path(tree).resolve(), Path(out).resolve()
    require(not out.is_relative_to(tree), 'receipt_inside_worktree')
    authority = pin(Path(authority_root) / DECISION)
    before = clean_head(tree, head)
    plan = workflow_plan(tree, head, changed_paths)
    check_dependencies(tree, plan, prepare=True)
    clean_head(tree, head)
    require(not (out / 'LOCAL-CI.json').exists() and not (out / 'home').exists()
            and not any(out.glob('command-*.log')), 'output_already_exists')
    out.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    results = []
    # Isolated HOME keeps workflow git --global writes away from the operator.
    home = out / 'home'
    home.mkdir()
    environment = {k: os.environ[k] for k in ('PATH', 'TMPDIR', 'LANG', 'VIRTUAL_ENV') if k in os.environ}
    environment.update(HOME=str(home), PYTHONDONTWRITEBYTECODE='1', GITHUB_STEP_SUMMARY=str(out / 'summary.txt'))
    produced_by = producer()
    for i, row in enumerate(plan):
        try:
            clean_head(tree, head)
        except ValueError:
            break  # retain a terminal FAIL receipt rather than executing on dirt
        log = out / ('command-%03d.log' % i)
        with log.open('xb') as stream:
            try:
                run = subprocess.run(['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', row['command']],
                                     cwd=tree / row['cwd'], env={**environment, **row['env']},
                                     stdout=stream, stderr=subprocess.STDOUT, timeout=1800)
                exit_code = run.returncode
            except subprocess.TimeoutExpired:
                stream.write(b'\nLOCAL_CI_COMMAND_TIMEOUT\n')
                exit_code = 124
        results.append({'exit_code': exit_code, 'log': pin(log)})
        print(json.dumps({'step': i, 'workflow': row['workflow'], 'name': row['name'], 'exit_code': exit_code}), flush=True)
    try:
        after = clean_head(tree, head)
    except ValueError as exc:
        after = {**before, 'clean': False, 'error': str(exc)}
    require(produced_by == producer(), 'runner_changed_during_execution')
    receipt = {'schema': SCHEMA, 'repo': repo, 'pr': pr, 'head_sha': head, 'producer': produced_by,
               'authority': authority, 'worktree_before': before, 'worktree_after': after,
               'changed_paths': sorted(set(changed_paths)), 'commands': plan, 'results': results,
               'pass_count': sum(r['exit_code'] == 0 for r in results),
               'fail_count': sum(r['exit_code'] != 0 for r in results), 'started': started,
               'finished': datetime.now(timezone.utc).isoformat()}
    receipt['status'] = 'PASS' if receipt['fail_count'] == 0 and after.get('clean') is True else 'FAIL'
    write_new(out / 'LOCAL-CI.json', receipt)
    return receipt


def validate_local(receipt, *, repo, pr, head, changed_paths, authority_root=ROOT):
    require(repo in json.loads(REGISTRY.read_bytes())['repositories'], 'repository_unregistered')
    require(receipt.get('schema') == SCHEMA and receipt.get('repo') == repo and receipt.get('pr') == pr,
            'receipt_identity_mismatch')
    require(receipt.get('head_sha') == head, 'receipt_head_mismatch')
    require(receipt.get('producer') == producer(), 'runner_provenance_mismatch')
    require(receipt.get('authority') == pin(Path(authority_root) / DECISION), 'operator_decision_missing_or_changed')
    before, after = receipt['worktree_before'], receipt['worktree_after']
    require(before.get('clean') is True and after.get('clean') is True and before == after, 'dirty_tree')
    require(clean_head(before['path'], head) == before, 'worktree_binding_changed')
    require(receipt.get('changed_paths') == sorted(set(changed_paths)), 'pr_files_mismatch')
    plan = workflow_plan(before['path'], head, changed_paths)
    check_dependencies(before['path'], plan)
    require(receipt.get('commands') == plan, 'workflow_commands_missing_or_changed')
    results = receipt.get('results')
    require(isinstance(results, list) and len(results) == len(plan), 'command_results_missing')
    require(receipt.get('status') == 'PASS' and type(receipt.get('pass_count')) is int and
            receipt['pass_count'] == len(plan) and type(receipt.get('fail_count')) is int and receipt['fail_count'] == 0 and
            all(type(r.get('exit_code')) is int and r['exit_code'] == 0 for r in results), 'commands_not_passed')
    for row in results:
        require(pin(row['log']['path']) == row['log'], 'command_log_drift')
    started = datetime.fromisoformat(receipt['started'])
    finished = datetime.fromisoformat(receipt['finished'])
    require(started.tzinfo is not None and finished.tzinfo is not None and started <= finished <= datetime.now(timezone.utc),
            'invalid_run_timestamps')
    return {'mode': 'local-ci', 'head_sha': head, 'producer': receipt['producer'], 'authority': receipt['authority']}


BILLING_LOCK = 'The job was not started because your account is locked due to a billing issue.'


def hosted_success(remote):
    checks = remote.get('statusCheckRollup') or []
    return bool(checks) and all(c.get('status') == 'COMPLETED' and c.get('conclusion') == 'SUCCESS' for c in checks)


def billing_lock(repo, head, gh=gh_json):
    runs = gh('api', f'repos/{repo}/commits/{head}/check-runs?per_page=100')
    rows = runs.get('check_runs') or []
    require(rows and runs.get('total_count', len(rows)) == len(rows) and len(rows) < 100,
            'billing_lock_unproven_or_truncated')
    refused = []
    for row in rows:
        require(row.get('head_sha') == head and row.get('status') == 'completed', 'billing_lock_head_or_status_mismatch')
        if row.get('conclusion') == 'success':
            continue
        require(row.get('conclusion') in ('failure', 'cancelled') and type(row.get('id')) is int,
                'nonbilling_hosted_failure')
        notes = gh('api', f'repos/{repo}/check-runs/{row["id"]}/annotations?per_page=100')
        require(isinstance(notes, list) and len(notes) < 100 and any(n.get('message') == BILLING_LOCK for n in notes),
                'nonbilling_hosted_failure')
        refused.append(row['id'])
    require(refused, 'billing_lock_unproven')
    return {'head_sha': head, 'check_run_ids': refused, 'annotation': BILLING_LOCK}


def discover_binding(repo, head, root=ROOT):
    index = Path(root) / 'reports/ops/local-ci' / repo.split('/')[1] / (head + '.json')
    require(index.is_file() and not index.is_symlink(), 'registered_receipt_missing')
    return json.loads(index.read_bytes())


def admit(remote, *, repo, pr, head, local_binding=None, authority_root=ROOT, gh=gh_json):
    require(remote.get('headRefOid') == head, 'remote_head_mismatch')
    # Works without decision/receipt/runner inputs when Actions recovers.
    if hosted_success(remote):
        return {'mode': 'hosted-success', 'head_sha': head}
    require((Path(authority_root) / DECISION).is_file(), 'operator_decision_missing_or_changed')
    locked = billing_lock(repo, head, gh)
    registered = discover_binding(repo, head, authority_root)
    require(local_binding is None or local_binding == registered, 'receipt_not_registered')
    binding = registered
    receipt = bound(binding)
    mode = validate_local(receipt, repo=repo, pr=pr, head=head,
                          changed_paths=[r['path'] for r in remote.get('files', [])], authority_root=authority_root)
    return {**mode, 'receipt': binding, 'billing_lock': locked}


def validate_pr_ci(pr, remote, authority_root=ROOT):
    """Typed closer revalidates local evidence; existing hosted check pins survive."""
    repo = re.fullmatch(r'https://github.com/(izaiahat/glp-(?:business|site-candidate))/pull/[1-9]\d*', pr['url'])[1]
    mode = admit(remote, repo=repo, pr=pr['url'], head=pr['head_sha'],
                 local_binding=(pr.get('ci_admission') or {}).get('receipt'), authority_root=authority_root)
    if mode['mode'] == 'hosted-success':
        checks = remote['statusCheckRollup']
        require(isinstance(pr.get('checks'), list) and pr['checks'] and all(
            any(c.get('name') == e['name'] and c.get('detailsUrl') == e['url'] for c in checks)
            for e in pr['checks']), 'hosted_check_receipts_missing_or_changed')
    else:
        require(pr.get('ci_admission') == mode, 'admission_binding_changed')
    return mode
