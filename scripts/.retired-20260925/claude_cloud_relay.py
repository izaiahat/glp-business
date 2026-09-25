#!/usr/bin/env python3
"""Fail-closed Claude cloud relay. Never applies changes to the live candidate.

The official CLI creates sessions; GitHub is the authoritative PR/CI carrier.
There is currently no verified unattended subscription credit or transcript reader.
Unknown billing and unknown session completion deliberately block effects.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path('/home/ubuntu/business')
CONFIG = ROOT / 'ops/claude-cloud-relay/config.json'
JOB_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{2,63}$')
SESSION_RE = re.compile(r'https://claude\.ai/code/(?:session_[A-Za-z0-9_-]+|cse_[A-Za-z0-9_-]+)')
FORBIDDEN = re.compile(r'\b(?:deploy|publish|cloudflare|linear|n8n|secret|credential|token|password|push\s+to\s+main|merge\s+to\s+main)\b', re.I)


def cfg():
    return json.loads(CONFIG.read_text())


def path(relative):
    return ROOT / relative


def now():
    return dt.datetime.now(dt.timezone.utc)


def stamp():
    return now().isoformat()


def run(*argv, cwd=None, timeout=60):
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout)


def append_jsonl(file, obj):
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open('a') as fh:
        fh.write(json.dumps(obj, sort_keys=True) + '\n')
        fh.flush()
        os.fsync(fh.fileno())


def atomic_json(file, obj):
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(file.suffix + '.tmp')
    with tmp.open('w') as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write('\n')
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, file)


def locked(file, nonblocking=True):
    file.parent.mkdir(parents=True, exist_ok=True)
    fh = file.open('a+')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
    except BlockingIOError:
        fh.close()
        raise RuntimeError('LOCK_BUSY')
    return fh


def job_file(job):
    if not JOB_RE.fullmatch(job) or '..' in job:
        raise ValueError('INVALID_JOB_ID')
    return path(cfg()['jobs_dir']) / job / 'JOB.json'


def load_job(job):
    return json.loads(job_file(job).read_text())


def billing_gate(snapshot, auth, expected_email):
    """No fallback to an API key or historical/operator-only estimate."""
    if auth.get('authMethod') != 'claude.ai' or not auth.get('loggedIn') or auth.get('email') != expected_email:
        raise RuntimeError('SUBSCRIPTION_AUTH_REQUIRED')
    if snapshot.get('account') != expected_email or snapshot.get('source') != 'claude.ai/settings/usage live readback':
        raise RuntimeError('BILLING_READBACK_UNVERIFIED')
    try:
        age = (now() - dt.datetime.fromisoformat(snapshot['observed_at'].replace('Z', '+00:00'))).total_seconds()
        remaining = float(snapshot['credit_remaining_usd'])
    except (KeyError, ValueError, TypeError):
        raise RuntimeError('CREDIT_READBACK_UNKNOWN')
    if age < 0 or age > 300:
        raise RuntimeError('BILLING_READBACK_STALE')
    if snapshot.get('overage_enabled') is not False or snapshot.get('auto_reload_enabled') is not False:
        raise RuntimeError('OVERAGE_COULD_BILL')
    if snapshot.get('included_usage_available') is not True:
        raise RuntimeError('INCLUDED_USAGE_UNKNOWN_OR_EXHAUSTED')
    if remaining <= 0 or (snapshot.get('credit_expires_at') and dt.datetime.fromisoformat(snapshot['credit_expires_at'].replace('Z', '+00:00')) <= now()):
        raise RuntimeError('CREDIT_EXHAUSTED_OR_EXPIRED')
    return remaining


def verify_pr(job, pr):
    expected = f'claude-cloud/{job["job_id"]}'
    if (pr.get('headRefName') != expected or pr.get('baseRefName') != job['base_branch']
            or pr.get('headRepositoryOwner', {}).get('login') != job['repo'].split('/')[0]
            or pr.get('headRepository', {}).get('name') != job['repo'].split('/')[1]
            or pr.get('isCrossRepository')):
        raise RuntimeError('FOREIGN_PR_BINDING')
    if pr.get('state') != 'OPEN' or pr.get('isDraft'):
        raise RuntimeError('PR_NOT_OPEN_READY')
    return True


def prompt_policy(text, job_class):
    if job_class not in cfg()['allowed_classes']:
        raise RuntimeError('FORBIDDEN_JOB_CLASS')
    if FORBIDDEN.search(text):
        raise RuntimeError('FORBIDDEN_PROMPT_ACTION')


def job_instruction(job, user_text):
    review = job['job_class'] == 'review'
    return (f'Job {job["job_id"]} ({job["job_class"]}), issue {job["issue"]}. '
            f'Use repository {job["repo"]}, base {job["base_branch"]}. '
            'Read CLAUDE.md; this is a private mirror, not the live candidate. '
            'No deployment, Cloudflare, Linear, n8n, credentials, secret access or main push. '
            + ('Read-only review: do not modify files or open a PR. ' if review else
               f'Use branch claude-cloud/{job["job_id"]}; open a PR to {job["base_branch"]}, never merge it. Run setup/build before tests, record exact results. ')
            + '\n\nTask:\n' + user_text)


def submit(args):
    c = cfg()
    if args.repo != c['repo'] or args.branch != 'main':
        raise RuntimeError('REPO_OR_BASE_NOT_ALLOWED')
    jf = job_file(args.job)
    if jf.exists():
        raise RuntimeError('JOB_ALREADY_EXISTS_RECONCILE_FIRST')
    text = Path(args.prompt_file).read_text()
    prompt_policy(text, args.job_class)
    if not args.issue or not re.fullmatch(r'GLP-\d+', args.issue):
        raise RuntimeError('ISSUE_REQUIRED')
    if args.job_class != 'review' and not path('ops/claude-cloud-relay/post-build-baseline.json').exists():
        raise RuntimeError('POST_BUILD_BASELINE_REQUIRED')
    snap_path = path(c['billing_readback'])
    snapshot = json.loads(snap_path.read_text()) if snap_path.exists() else {}
    auth_cmd = run('claude', 'auth', 'status', timeout=15)
    auth = json.loads(auth_cmd.stdout) if auth_cmd.returncode == 0 else {}
    remaining = billing_gate(snapshot, auth, 'izaiahtilton1@gmail.com')
    active = [json.loads(p.read_text()) for p in path(c['jobs_dir']).glob('*/JOB.json')]
    if sum(x.get('status') in ('submitting', 'running', 'unknown') for x in active) >= c['max_concurrent_sessions']:
        raise RuntimeError('CLOUD_CONCURRENCY_LIMIT')
    # A clean checkout and exact origin guard are required: the CLI may otherwise upload a local bundle.
    repo_dir = Path(args.checkout).resolve()
    remote = run('git', 'remote', 'get-url', 'origin', cwd=repo_dir)
    branch = run('git', 'branch', '--show-current', cwd=repo_dir)
    dirty = run('git', 'status', '--porcelain', cwd=repo_dir)
    if any(x.returncode for x in (remote, branch, dirty)) or args.repo not in remote.stdout or branch.stdout.strip() != args.branch or dirty.stdout.strip():
        raise RuntimeError('CLEAN_REPO_CHECKOUT_REQUIRED')
    job = dict(job_id=args.job, job_class=args.job_class, repo=args.repo, base_branch=args.branch,
               issue=args.issue, status='submitting', created_at=stamp(), remaining_credit_before_usd=remaining)
    jf.parent.mkdir(parents=True, exist_ok=False)
    (jf.parent / 'PROMPT.md').write_text(text)
    atomic_json(jf, job)
    append_jsonl(path(c['ledger']), dict(ts=stamp(), kind='submit_reserved', job=args.job, remaining_credit_usd=remaining))
    # An ambiguous CLI exit is NEVER retried automatically. Capture only session URLs, not sensitive stdout.
    try:
        result = run('claude', '--cloud', job_instruction(job, text), cwd=repo_dir, timeout=180)
    except subprocess.TimeoutExpired:
        job['status'] = 'unknown'
        atomic_json(jf, job)
        raise RuntimeError('CLOUD_SUBMIT_AMBIGUOUS_TIMEOUT')
    matches = SESSION_RE.findall(result.stdout + result.stderr)
    if result.returncode or not matches:
        job['status'] = 'unknown'
        atomic_json(jf, job)
        raise RuntimeError('CLOUD_SUBMIT_UNCONFIRMED_RECONCILE')
    job.update(status='running', session_url=matches[-1], submitted_at=stamp())
    atomic_json(jf, job)
    append_jsonl(path(c['ledger']), dict(ts=stamp(), kind='submitted', job=args.job, session_url=matches[-1], remaining_credit_usd=remaining))
    print(json.dumps(job))


def gh_prs(job):
    result = run('gh', 'pr', 'list', '-R', job['repo'], '--state', 'open', '--head', f'claude-cloud/{job["job_id"]}',
                 '--json', 'url,headRefName,baseRefName,headRepositoryOwner,headRepository,isCrossRepository,state,isDraft,number,headRefOid', timeout=60)
    if result.returncode:
        raise RuntimeError('GH_PR_READBACK_FAILED')
    return json.loads(result.stdout)


def status(args):
    j = load_job(args.job)
    prs = gh_prs(j) if j.get('status') == 'running' and j['job_class'] != 'review' else []
    print(json.dumps(dict(job=j, prs=prs, note='A missing PR does not prove session completion. Review-class final messages require a verified cloud transcript reader.')))


def verified_final_message(job):
    """No documented CLI/API transcript readback for this Pro account.

    Enterprise compliance APIs do not apply to a personal subscription;
    an unauthenticated GitHub PR is not a final cloud-session message.
    """
    raise RuntimeError('SESSION_FINAL_MESSAGE_READBACK_UNAVAILABLE')


def land_one(job_id):
    c = cfg()
    jf = job_file(job_id)
    j = load_job(job_id)
    if j['status'] in ('landed', 'blocked'):
        return j['status']
    if j['status'] != 'running':
        return j['status']
    if j['job_class'] == 'review':
        return 'AWAITING_VERIFIED_SESSION_TRANSCRIPT'
    prs = gh_prs(j)
    if not prs:
        return 'AWAITING_PR_NOT_PROOF_OF_COMPLETION'
    if len(prs) != 1:
        raise RuntimeError('AMBIGUOUS_PR_BINDING')
    pr = prs[0]
    verify_pr(j, pr)
    pr_url = pr['url']
    # A green PR is not proof that its cloud session finished. This reader
    # refuses until an officially supported transcript/status adapter exists.
    final_message = verified_final_message(j)
    # GitHub CI only, never run untrusted PR code in the live repository.
    checks = run('gh', 'pr', 'checks', pr_url, '--json', 'name,state,bucket,link', timeout=60)
    if checks.returncode:
        return 'AWAITING_CI_CHECKS'
    rows = json.loads(checks.stdout)
    if not rows or any(row['bucket'] != 'pass' for row in rows):
        return 'AWAITING_GREEN_CI'
    diff = run('gh', 'pr', 'diff', pr_url, timeout=60)
    stat = run('gh', 'api', f'repos/{j["repo"]}/pulls/{pr["number"]}', '--jq', '[.additions,.deletions,.changed_files] | @json', timeout=60)
    if diff.returncode or stat.returncode:
        raise RuntimeError('PR_DIFF_READBACK_FAILED')
    out = jf.parent
    (out / 'PR.url').write_text(pr_url + '\n')
    (out / 'PR.diff').write_text(diff.stdout)
    (out / 'DIFF-STAT.json').write_text(stat.stdout.strip() + '\n')
    (out / 'RESULT.md').write_text(f'PR: {pr_url}\nCI: {json.dumps(rows)}\nFinal cloud message:\n{final_message}\n')
    receipt = dict(job=job_id, pr_url=pr_url, head_sha=pr['headRefOid'], branch=pr['headRefName'],
                   diff_path=str(out / 'PR.diff'), tests_result='GitHub PR checks pass: ' + ', '.join(x['name'] for x in rows),
                   issue=j['issue'], ts=stamp(), final_message=final_message)
    atomic_json(out / 'INGESTION.json', receipt)
    # Durable idempotency: ledger is the shared event dedupe authority after queue/event crash.
    event_key = json.dumps(['cloud_pr_ready', job_id, pr_url])
    events = path(c['events'])
    if not events.exists() or not any(json.loads(line).get('event_key') == event_key for line in events.read_text().splitlines() if line.strip()):
        queue = path(c['apply_queue'])
        if not queue.exists() or not any(json.loads(line).get('job') == job_id for line in queue.read_text().splitlines() if line.strip()):
            append_jsonl(queue, receipt)
        append_jsonl(events, dict(ts=stamp(), kind='cloud_pr_ready', seat='glp-design', issue=j['issue'],
                                  job=job_id, pr_url=pr_url, result_path=str(out / 'INGESTION.json'), event_key=event_key))
    j.update(status='landed', pr_url=pr_url, landed_at=stamp(), remaining_credit_after_usd=None)
    atomic_json(jf, j)
    append_jsonl(path(c['ledger']), dict(ts=stamp(), kind='landed', job=job_id, pr_url=pr_url,
                                          remaining_credit_usd=None, billing_readback='unavailable after landing'))
    return 'landed'


def monitor(args):
    with locked(path('ops/claude-cloud-relay/monitor.lock')):
        ids = [args.job] if args.job else sorted(p.parent.name for p in path(cfg()['jobs_dir']).glob('*/JOB.json'))
        for job in ids:
            try:
                print(f'{job}: {land_one(job)}')
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                print(f'{job}: BLOCKED {exc}', file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest='command', required=True)
    s = sp.add_parser('submit')
    for flag in ('repo', 'branch', 'prompt-file', 'job', 'job-class', 'issue', 'checkout'):
        s.add_argument('--' + flag, required=True)
    for command in ('status', 'poll', 'land'):
        p = sp.add_parser(command)
        p.add_argument('--job', required=True)
    p = sp.add_parser('monitor')
    p.add_argument('--job')
    args = ap.parse_args()
    try:
        if args.command == 'submit': submit(args)
        elif args.command == 'status': status(args)
        else: monitor(args)
    except (RuntimeError, ValueError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f'REFUSED {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
