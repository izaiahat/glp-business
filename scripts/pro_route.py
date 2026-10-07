#!/usr/bin/env python3
"""Durable, content-idempotent Linear → registered Pro relay route.

One dispatcher, separate bound tabs, existing account/global transport locks.
No recovery sends: uncertain attempts require the relay's governed disposition.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import sys

import gpt6pro_packet as packet
import gpt6pro_relay as relay

ROOT = Path('/home/ubuntu/business')


def directory():
    return ROOT / 'ops/gpt6pro-relay/route'


@contextmanager
def singleton():
    directory().mkdir(parents=True, exist_ok=True)
    with (directory() / 'dispatcher.lock').open('a') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def save(row):
    relay._atomic_json(directory() / 'queue' / (row['job'] + '.json'), row)


def enqueue(issue, prompt_file, account='auto', session=None):
    if not re.fullmatch(r'GLP-[1-9][0-9]*', issue):
        raise ValueError('Invalid Linear issue')
    text = Path(prompt_file).read_text(encoding='utf-8')
    if not text.strip():
        raise ValueError('Empty prompt')
    digest = hashlib.sha256((issue + '\0' + text).encode()).hexdigest()
    job = 'route-' + issue.lower() + '-' + digest[:24]
    with singleton():
        path = directory() / 'queue' / (job + '.json')
        if path.exists():
            row = json.loads(path.read_text())
            if row['request_sha256'] != digest:
                raise ValueError('Request digest conflict')
            return row
        if relay.job_state(job) or any(r.get('job') == job for r in relay.ledger_rows(relay.load_config())):
            raise ValueError('Orphan relay custody: reconcile, never recreate request')
        wave = {'schema': 'gpt6pro-wave-v1', 'issue': issue,
                'acceptance_verbatim': 'Read-only advisory for the named Linear issue; no implementation or live effects.',
                'dispatcher_profile': 'default', 'hermes_session_id': session or os.environ.get('HERMES_SESSION_ID') or 'pro-route-cli',
                'linear_child': issue, 'request_id': job, 'jobs': [{
                    'job': job, 'kind': 'freeform', 'job_class': 'research-synthesis',
                    'title': issue + ' source-bound advisory', 'situation': text,
                    'ruling': 'Read-only advisory. GitHub and Linear access is read-only. Do not execute tests, modify repositories, mutate issues, send messages, deploy, spend or change accounts. Preserve provider and privacy holds. Follow the exact relay Drive outbox protocol; save ADVISORY.md and OUTPUT-MANIFEST.json, hash the actual saved bytes, and emit the final manifest hash marker. Never invent hashes or results.',
                    'task': '1. Answer the supplied request using the supplied evidence and accessible current issue/source. Distinguish facts, recommendations and missing inputs.\n2. Give an actionable, bounded implementation/verification plan and risks. Do not repeat already-completed scope.\n3. Save the substantive advisory as ADVISORY.md in the exact relay outbox and comply with the message identity/manifest protocol.',
                    'files': [], 'json_schema': '{"issue":"' + issue + '","recommendation":"...","missing_inputs":[],"risks":[]}'}]}
        wave_path = ROOT / 'ops/gpt6pro-relay/waves' / (job + '.json')
        relay._atomic_json(wave_path, wave)
        wave['_path'] = str(wave_path)
        packet.build_freeform(wave['jobs'][0], wave)
        row = {'schema': 'pro-route-v1', 'job': job, 'issue': issue, 'request_sha256': digest,
               'requested_account': account, 'state': 'queued', 'created_at': relay.now_iso(),
               'wave': str(wave_path), 'attempts': [],
               'packet_sha256': {n: hash_file(relay.JOBS / job / n) for n in ('PROMPT.md', 'MANIFEST.json')}}
        save(row)
        return row


def choose_account(requested):
    cfg = relay.load_config()
    rows = relay.active_registry()
    if len(rows) >= min(6, cfg['envelope']['max_concurrent_chats_per_ip']):
        return None
    candidates = []
    control_path = directory() / 'CONTROL.json'
    control = json.loads(control_path.read_text()) if control_path.exists() else {}
    held = control.get('paused_accounts', {})
    if not isinstance(held, dict) or not set(held) <= {'acct1', 'acct2', 'acct3'}:
        raise ValueError('INVALID_ACCOUNT_CONTROL')
    aliases = {f'acct{i}': a for i, a in enumerate(cfg['accounts'], 1)}
    if requested != 'auto' and requested not in aliases:
        raise ValueError('Account must be auto or acct1/acct2/acct3')
    for alias, acct in aliases.items():
        if requested not in ('auto', alias) or alias in held:
            continue
        active = sum(r['account'] == acct['label'] for r in rows)
        if active >= min(3, cfg['envelope']['max_concurrent_chats_per_account']):
            continue
        siblings = relay.sibling_bindings(acct['label'], '')
        if any((s.get('status') in ('submitting', 'running') or s.get('retry_blocked'))
               and not relay.binding_url(s) and not relay._verified_terminal_unsent(s) for s in siblings):
            continue
        try:
            used, limit = relay.budget_check(cfg, acct)
            with socket.create_connection(('127.0.0.1', acct['cdp_port']), timeout=2):
                pass
        except (OSError, SystemExit):
            continue
        candidates.append((active, used / max(1, limit), alias))
    return min(candidates)[2] if candidates else None


def request_rows():
    return [json.loads(p.read_text()) for p in sorted((directory() / 'queue').glob('*.json'))]


def effect(row, action):
    """One non-waiting relay CLI call under owned heavy; no model/browser bypass."""
    if action == 'submit':
        pins = row.get('packet_sha256', {})
        if set(pins) != {'PROMPT.md', 'MANIFEST.json'} or any(
                hash_file(relay.JOBS / row['job'] / name) != digest for name, digest in pins.items()):
            raise ValueError('PACKET_DRIFT')
    seat = f'pro-route-{os.getpid()}'
    env = {**os.environ, 'CANDIDATE_BUILD_SEAT': seat, 'PYTHONDONTWRITEBYTECODE': '1'}
    lock = [sys.executable, str(ROOT / 'scripts/candidate_build_lock.py')]
    def run(args):
        return subprocess.run(args, capture_output=True, text=True, env=env, cwd=ROOT)
    status = run(lock + ['status'])
    if status.returncode or json.loads(status.stdout).get('status') != 'FREE':
        return {'deferred': True, 'reason': 'HEAVY_HELD'}
    acquired = run(lock + ['acquire', '--seat', seat, '--reason', 'Pro route ' + action])
    if acquired.returncode:
        return {'deferred': True, 'reason': 'HEAVY_REFUSED'}
    try:
        args = [sys.executable, str(Path(relay.__file__).resolve()), action, '--job', row['job']]
        if action == 'submit':
            cfg = relay.load_config()
            acct = cfg['accounts'][int(row['account'][4:]) - 1]
            d = relay.JOBS / row['job']
            manifest = json.loads((d / 'MANIFEST.json').read_text())
            args += ['--account', acct['label'], '--job-class', manifest['job_class'],
                     '--prompt', str(d / 'PROMPT.md'), '--prompt-on-drive']
            for option, field in (('--attach', 'attach'), ('--drive-attach', 'drive_attach')):
                if manifest.get(field):
                    args += [option, *manifest[field]]
            for key in relay.PROVENANCE_FIELDS:
                args += ['--' + key.replace('_', '-'), manifest[key]]
        # No outer timeout kills a sending process or releases heavy beneath it.
        cp = run(args)
        logs = directory() / 'logs'; logs.mkdir(parents=True, exist_ok=True)
        log = logs / f'{row["job"]}-{action}-{time.time_ns()}.log'
        log.write_text(cp.stdout + cp.stderr)
        result = {'rc': cp.returncode, 'log': str(log)}
        # Only explicit PRE-reservation backpressure permits another submit.
        if action == 'submit' and cp.returncode and not relay.job_state(row['job']):
            last = (cp.stdout + cp.stderr).strip().splitlines()
            if last and last[-1].startswith(('CAPACITY_', 'PACING_SUBMIT')) and not any(
                    r.get('job') == row['job'] for r in relay.ledger_rows(relay.load_config())):
                result.update(deferred=True, reason='PRE_RESERVATION_BACKPRESSURE')
        return result
    finally:
        released = run(lock + ['release', '--seat', seat])
        if released.returncode:
            raise RuntimeError('HEAVY_RELEASE_REFUSED')


def verified_files(job):
    """Rehash local files; never manufacture a native outbox receipt."""
    st = relay.job_state(job)
    receipt = st.get('fetch_verification', {})
    if receipt.get('state') != 'VERIFIED':
        raise ValueError('OUTBOX_NOT_VERIFIED')
    dest = relay.JOBS / job / 'outbox'
    raw = (dest / 'OUTPUT-MANIFEST.json').read_bytes()
    sha = lambda b: hashlib.sha256(b).hexdigest()
    manifest = json.loads(raw)
    files = manifest['files']
    if (sha(raw) != receipt['manifest_sha256'] or
            sha((dest.parent / 'RESPONSE.md').read_bytes()) != st['source_reply_sha256'] or
            set(p.name for p in dest.iterdir()) != set(files) | {'OUTPUT-MANIFEST.json'} or
            any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', n) for n in files) or
            any(sha((dest / n).read_bytes()) != digest for n, digest in files.items())):
        raise ValueError('OUTBOX_BYTES_CHANGED')
    return {**files, 'OUTPUT-MANIFEST.json': sha(raw)}


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assign(job, account):
    if account not in ('auto', 'acct1', 'acct2', 'acct3'):
        raise ValueError('INVALID_ACCOUNT')
    with singleton():
        row = next(r for r in request_rows() if r['job'] == job)
        if row['state'] != 'queued' or relay.job_state(job) or any(
                r.get('job') == job for r in relay.ledger_rows(relay.load_config())):
            raise ValueError('RESERVED_JOB_ACCOUNT_IMMUTABLE')
        row['requested_account'] = account
        save(row)


def requeue_unsent(job, authorize_preimage):
    """Explicit operator action AFTER the maintained reconciler, never automatic."""
    with singleton():
        path = relay.JOBS / job / 'JOB.json'
        st = json.loads(path.read_bytes())
        if (hash_file(path) != authorize_preimage or st.get('status') != 'unsent_reconciled' or
                st.get('typed') is not False or st.get('sent') is not False or
                st.get('retry_blocked') is not False or st.get('reason') != 'WRONG_MODEL'):
            raise ValueError('UNSENT_RECEIPT_REQUIRED')
        digest = st.get('preimage_sha256', '')
        if not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('UNSENT_RECEIPT_REQUIRED')
        backup = path.with_name('JOB.pre-model-reconcile-' + digest + '.json')
        original = json.loads(backup.read_bytes())
        rows = [r for r in relay.ledger_rows(relay.load_config()) if r.get('job') == job]
        if (hash_file(backup) != digest or original.get('typed') is not False or
                original.get('sent') is not False or original.get('reason') != 'WRONG_MODEL' or
                len(rows) != 3 or [(r.get('event'), r.get('phase')) for r in rows] != [
                    ('submitted', None), ('model_unsent_reconcile', 'STARTED'), ('model_unsent_reconcile', 'APPLIED')] or
                any(r.get('preimage_sha256') != digest for r in rows[1:])):
            raise ValueError('UNSENT_RECEIPT_REQUIRED')
        row = next(r for r in request_rows() if r['job'] == job)
        row.update(state='queued', authorize_unsent_preimage=authorize_preimage)
        save(row)


def dispatch(only=None, max_actions=6):
    results = []
    with singleton():
        for row in sorted(request_rows(), key=lambda r: (r.get('created_at', ''), r['job'])):
            if only and row['job'] != only:
                continue
            if len(results) >= max_actions:
                break
            st = relay.job_state(row['job'])
            if (row['state'] == 'queued' and row.get('authorize_unsent_preimage') and
                    st.get('status') == 'unsent_reconciled' and
                    hash_file(relay.JOBS / row['job'] / 'JOB.json') == row['authorize_unsent_preimage']):
                st = {}  # exact explicit reconciler receipt; consumed by durable submitting state
            if st.get('status') == 'running':
                row['state'] = 'running'
                action = 'poll'
            elif st.get('status') == 'done':
                action = 'fetch-outbox'
                if st.get('fetch_verification', {}).get('state') == 'VERIFIED':
                    row['state'] = 'complete'
                    save(row)
                    continue
                if row['state'] == 'outbox_blocked':
                    continue
            elif st or row['state'] in ('submitting', 'uncertain'):
                row['state'] = 'uncertain'
                save(row)
                continue
            elif row['state'] == 'queued':
                account = choose_account(row['requested_account'])
                if not account:
                    continue
                row['account'] = account
                row['state'] = 'submitting'
                action = 'submit'
            else:
                continue
            save(row)  # durable attempt marker BEFORE any external effect
            try:
                result = effect(row, action)
                row['attempts'].append({'action': action, 'at': relay.now_iso(), **result})
                if result.get('deferred'):
                    if action == 'submit':
                        row['state'] = 'queued'
                elif action == 'submit':
                    current = relay.job_state(row['job'])
                    row['state'] = 'running' if current.get('status') == 'running' else 'uncertain'
                elif action == 'fetch-outbox':
                    row['state'] = 'complete' if result['rc'] == 0 else 'outbox_blocked'
            except Exception as exc:
                row['state'] = 'uncertain' if action == 'submit' else 'outbox_blocked' if action == 'fetch-outbox' else 'running'
                result = {'error': type(exc).__name__}
                row['attempts'].append({'action': action, 'at': relay.now_iso(), **result})
            save(row)
            results.append({'job': row['job'], 'action': action, 'state': row['state'], **result})
    return results


def summary(row):
    st = relay.job_state(row['job'])
    result = {k: row.get(k) for k in ('job', 'issue', 'account', 'requested_account', 'state')}
    result['relay_status'] = st.get('status')
    result['response'] = str(relay.JOBS / row['job'] / 'RESPONSE.md') if st.get('status') == 'done' else None
    if st.get('fetch_verification', {}).get('state') == 'VERIFIED':
        result['files'] = verified_files(row['job'])
        result['outbox'] = str(relay.JOBS / row['job'] / 'outbox')
        result['state'] = 'complete'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    submit = sub.add_parser('submit', help='durably enqueue; attempt once unless --queue-only')
    submit.add_argument('--issue', required=True)
    submit.add_argument('--prompt-file', type=Path, required=True)
    submit.add_argument('--account', choices=['auto', 'acct1', 'acct2', 'acct3'], default='auto')
    submit.add_argument('--session')
    submit.add_argument('--queue-only', action='store_true')
    status = sub.add_parser('status', help='local custody only, never navigates browser')
    status.add_argument('--job')
    tick = sub.add_parser('dispatch', help='single bounded queue pass; safe cron entry')
    tick.add_argument('--max-actions', type=int, default=6)
    fetch = sub.add_parser('fetch', help='poll/fetch exact existing job; never submits')
    fetch.add_argument('--job', required=True)
    assign_parser = sub.add_parser('assign', help='change an unreserved queued request account')
    assign_parser.add_argument('--job', required=True)
    assign_parser.add_argument('--account', choices=['auto', 'acct1', 'acct2', 'acct3'], required=True)
    requeue = sub.add_parser('requeue-unsent', help='explicit opt-in after maintained WRONG_MODEL unsent reconciliation')
    requeue.add_argument('--job', required=True)
    requeue.add_argument('--authorize-preimage', required=True)
    args = parser.parse_args(argv)
    if args.command == 'assign':
        assign(args.job, args.account)
    elif args.command == 'requeue-unsent':
        requeue_unsent(args.job, args.authorize_preimage)
    elif args.command == 'submit':
        row = enqueue(args.issue, args.prompt_file, args.account, args.session)
        print(json.dumps({'job': row['job'], 'state': row['state']}), flush=True)
        if not args.queue_only:
            print(json.dumps(dispatch(only=row['job'])), flush=True)
    elif args.command == 'dispatch':
        if not 1 <= args.max_actions <= 6:
            parser.error('--max-actions must be 1..6')
        results = dispatch(max_actions=args.max_actions)
        if results:
            print(json.dumps(results), flush=True)
    elif args.command == 'status':
        print(json.dumps([summary(r) for r in request_rows() if not args.job or r['job'] == args.job], indent=2))
    else:
        with singleton():
            matches = [r for r in request_rows() if r['job'] == args.job]
            if len(matches) != 1:
                raise ValueError('NO_SUCH_ROUTE_JOB')
            row = matches[0]; st = relay.job_state(args.job)
            if st.get('status') not in ('running', 'done'):
                raise ValueError('NO_BOUND_RUNNING_OR_DONE_JOB')
            if st.get('fetch_verification', {}).get('state') != 'VERIFIED':
                action = 'fetch-outbox' if st['status'] == 'done' else 'poll'
                result = effect(row, action)
                row['attempts'].append({'action': action, 'at': relay.now_iso(), **result})
                save(row)
            print(json.dumps(summary(row), indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except BlockingIOError:
        print('PRO_ROUTE_BUSY: existing dispatcher owns the queue')
        sys.exit(0)
