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


def enqueue(issue, prompt_file, account='auto', session=None, *, expected_sha256=None):
    if not re.fullmatch(r'GLP-[1-9][0-9]*', issue):
        raise ValueError('Invalid Linear issue')
    raw = Path(prompt_file).read_bytes()
    if expected_sha256 and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('BACKLOG_DRIFT ' + str(prompt_file))
    text = raw.decode('utf-8')
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


def enqueue_replacement(job, ruling):
    """Queue one same-situation successor after maintained disposition; never send.

    Only the normal dispatcher performs external effects. A partial packet is held
    for explicit reconciliation, never deleted or silently rebuilt.
    """
    with singleton():
        st = relay.job_state(job)
        if not relay._verified_supersession(st) or st['supersession']['ruling'] != ruling:
            raise ValueError('SUPERSESSION_RECEIPT_REQUIRED')
        proof = st['supersession']
        entry, ruling_raw = relay._supersession_ruling(ruling, job)
        digest = hashlib.sha256(ruling_raw).hexdigest()
        original = next(r for r in request_rows() if r['job'] == job)
        if (original['issue'] != entry['linear_child'] or
                original['request_sha256'] != entry['request_sha256'] or
                original['wave'] != str(ROOT / entry['wave'])):
            raise ValueError('SUPERSESSION_SOURCE_REQUEST_DRIFT')
        wave = json.loads((ROOT / entry['wave']).read_bytes())
        if (wave['issue'] != entry['linear_child'] or len(wave['jobs']) != 1 or
                wave['jobs'][0]['job'] != job or wave['jobs'][0]['job_class'] != 'research-synthesis' or
                wave['jobs'][0]['kind'] != 'freeform' or wave['jobs'][0].get('files') or
                hashlib.sha256((wave['issue'] + '\0' + wave['jobs'][0]['situation']).encode()).hexdigest() != entry['request_sha256']):
            raise ValueError('SUPERSESSION_SOURCE_SITUATION_DRIFT')
        replacement = entry['replacement_job']
        path = directory() / 'queue' / (replacement + '.json')
        if path.exists():
            row = json.loads(path.read_bytes())
            _replacement_receipt(row)
        else:
            wave_path = ROOT / 'ops/gpt6pro-relay/waves' / (replacement + '.json')
            if (relay.JOBS / replacement).exists() or wave_path.exists() or any(
                    r.get('job') == replacement for r in relay.ledger_rows(relay.load_config())):
                raise ValueError('SUPERSESSION_ORPHAN_REPLACEMENT_CUSTODY')
            # The supplied situation, task, ruling and source evidence are unchanged.
            # Only the request identity / output folder in the registered packet change.
            wave['request_id'] = replacement; wave['jobs'][0]['job'] = replacement
            relay._atomic_json(wave_path, wave)
            wave['_path'] = str(wave_path)
            packet.build_freeform(wave['jobs'][0], wave)
            row = {'schema': 'pro-route-v1', 'job': replacement, 'issue': entry['linear_child'],
                   'request_sha256': entry['request_sha256'], 'supersedes': job,
                   'supersession_ruling': ruling, 'supersession_ruling_sha256': digest,
                   'requested_account': entry['account_alias'], 'state': 'queued',
                   'created_at': relay.now_iso(), 'wave': str(wave_path),
                   'wave_sha256': hash_file(wave_path), 'attempts': [],
                   'packet_sha256': {n: hash_file(relay.JOBS / replacement / n)
                                     for n in ('PROMPT.md', 'MANIFEST.json')}}
            if original.get('backlog_order') is not None:
                row['backlog_order'] = original['backlog_order']
            save(row)
        original.update(state='superseded', replacement_job=replacement,
                        supersession_ruling=ruling, supersession_preimage_sha256=proof['preimage_sha256'])
        save(original)
        return row


def _replacement_receipt(row):
    """Recheck the full parent receipt, same prompt and exact account before effect."""
    st = relay.job_state(row['supersedes'])
    if not relay._verified_supersession(st):
        raise ValueError('SUPERSESSION_RECEIPT_REQUIRED')
    entry, ruling_raw = relay._supersession_ruling(row['supersession_ruling'], row['supersedes'])
    digest = hashlib.sha256(ruling_raw).hexdigest()
    if (row['job'] != entry['replacement_job'] or row['issue'] != entry['linear_child'] or
            row['request_sha256'] != entry['request_sha256'] or
            row['supersession_ruling_sha256'] != digest or
            row['requested_account'] != entry['account_alias'] or
            row.get('account', entry['account_alias']) != entry['account_alias'] or
            row['wave'] != str(ROOT / 'ops/gpt6pro-relay/waves' / (row['job'] + '.json')) or
            set(row['packet_sha256']) != {'PROMPT.md', 'MANIFEST.json'} or
            hash_file(Path(row['wave'])) != row['wave_sha256']):
        raise ValueError('SUPERSESSION_REPLACEMENT_DRIFT')
    old_wave = json.loads((ROOT / entry['wave']).read_bytes())
    expected = json.loads(json.dumps(old_wave))
    expected['request_id'] = row['job']; expected['jobs'][0]['job'] = row['job']
    if json.loads(Path(row['wave']).read_bytes()) != expected:
        raise ValueError('SUPERSESSION_REPLACEMENT_SITUATION_DRIFT')
    for name, digest in row['packet_sha256'].items():
        if hash_file(relay.JOBS / row['job'] / name) != digest:
            raise ValueError('PACKET_DRIFT ' + row['job'] + '/' + name)


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
        if active >= min(2, cfg['envelope']['max_concurrent_chats_per_account']):
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
    if row.get('supersedes'):
        _replacement_receipt(row)
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


def ingest_backlog():
    """Pin explicit numbered prompts before packet production; never auto-mint work.

    A crash after enqueue but before indexing is safe: enqueue is content-idempotent.
    Replacing a pinned file is refused; a new reviewed question needs a new filename.
    """
    path = directory() / 'BACKLOG.json'
    with singleton():
        index = json.loads(path.read_text()) if path.exists() else {}
        old = dict(index)
        inputs = []
        for prompt in sorted((directory() / 'backlog').glob('*.md')):
            match = re.fullmatch(r'([0-9]+)-(GLP-[1-9][0-9]*)-(auto|acct[123])\.md', prompt.name)
            if not match or prompt.is_symlink():
                raise ValueError('INVALID_BACKLOG_PROMPT ' + prompt.name)
            digest = hash_file(prompt)
            pinned = index.get(prompt.name)
            if pinned and pinned['sha256'] != digest:
                raise ValueError('BACKLOG_DRIFT ' + prompt.name)
            index.setdefault(prompt.name, {'sha256': digest})
            inputs.append((prompt, match[2], match[3], digest))
        if index != old:
            relay._atomic_json(path, index)
    added = []
    for prompt, issue, account, digest in inputs:
        if index[prompt.name].get('job'):
            continue
        row = enqueue(issue, prompt, account, 'pro-route-backlog', expected_sha256=digest)
        with singleton():
            current = json.loads(path.read_text())
            if current[prompt.name]['sha256'] != digest:
                raise ValueError('BACKLOG_DRIFT ' + prompt.name)
            current[prompt.name]['job'] = row['job']
            row.update(backlog_order=int(prompt.name.split('-', 1)[0]), backlog_file=prompt.name)
            save(row)
            relay._atomic_json(path, current)
        added.append(row['job'])
    return added


def dispatch(only=None, max_actions=6):
    results = []
    if not only:
        ingest_backlog()
    with singleton():
        # Refill vacant account slots before maintenance polls; rotate maintenance
        # by the durable last-attempt record so bounded/restarted ticks cannot starve.
        def priority(row):
            st = relay.job_state(row['job'])
            band = 0 if row['state'] == 'queued' and not st else 1
            attempts = row.get('attempts', [])
            last = attempts[-1].get('at', '') if attempts else ''
            return band, (row.get('backlog_order', 10**9), row.get('created_at', '')) if not band else (0, last), row['job']
        for row in sorted(request_rows(), key=priority):
            if only and row['job'] != only:
                continue
            if len(results) >= max_actions:
                break
            st = relay.job_state(row['job'])
            if (row['state'] == 'queued' and row.get('authorize_unsent_preimage') and
                    st.get('status') == 'unsent_reconciled' and
                    hash_file(relay.JOBS / row['job'] / 'JOB.json') == row['authorize_unsent_preimage']):
                st = {}  # exact explicit reconciler receipt; consumed by durable submitting state
            if st.get('status') == 'SUPERSEDED':
                row['state'] = 'superseded' if relay._verified_supersession(st) else 'uncertain'
                save(row)
                continue
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
            if action == 'poll' and not result.get('deferred'):
                # Match wave.poll_owned(): without spacing, the shared IP clock
                # admits only the first chat on every tick and starves siblings.
                # effect() has already released heavy/account locks here.
                time.sleep(max(20, relay.load_config()['envelope']['poll_stagger_s']))
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


def consume_outboxes(client=None, writer=None):
    """Deliver explicitly scoped native VERIFIED prep artifacts; comment-only.

    The durable STARTED state is a no-replay barrier. A failed/uncertain write
    is reconciled by paginating exact issue comments, never automatically resent.
    L2 continues to discover original route/outbox custody; these copies are prep,
    not a fabricated implementation RESULT or acceptance receipt.
    """
    control_path = directory() / 'CONSUMER.json'
    if not control_path.exists():
        return []
    control = json.loads(control_path.read_text())
    if control.get('schema') != 'pro-prep-consumer-v1' or control.get('enabled') is not True:
        return []
    jobs = control.get('jobs')
    if not isinstance(jobs, list) or len(jobs) != len(set(jobs)) or any(
            not isinstance(j, str) or not re.fullmatch(r'route-glp-[1-9][0-9]*-[A-Za-z0-9-]+', j) for j in jobs):
        raise ValueError('INVALID_PREP_CONSUMER_SCOPE')
    if client is None or writer is None:
        # The maintained host client is an installed dependency in this curated
        # checkout; its scope guards are not duplicated or bypassed here.
        sys.path.append(str(ROOT / 'scripts'))
        from linear_client import Client, write_entity
        client = client or Client(role='fleet', max_retries=0)
        writer = writer or write_entity
    if getattr(client, 'role', None) != 'fleet':
        raise ValueError('PREP_CONSUMER_FLEET_ONLY')
    author = client.operation('identity', {})['viewer']['id']
    comments_query = '''query($id:String!,$after:String){issue(id:$id){comments(first:50,after:$after){nodes{id body user{id} issue{id identifier}} pageInfo{hasNextPage endCursor}}}}'''
    results = []
    # Independent delivery lock, never a second account/wave/browser runner.
    with (directory() / 'consumer.lock').open('a') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for row in request_rows():
            job = row['job']
            if job not in jobs or row['state'] != 'complete':
                continue
            path = directory() / 'deliveries' / (job + '.json')
            receipt = json.loads(path.read_text()) if path.exists() else {}
            if receipt.get('state') == 'delivered':
                continue
            started = receipt.get('state') in ('posting', 'uncertain')
            try:
                if not re.fullmatch(r'GLP-[1-9][0-9]*', row.get('issue', '')):
                    raise ValueError('PREP_INVALID_ISSUE')
                files = verified_files(job)
                if 'ADVISORY.md' not in files:
                    raise ValueError('PREP_ADVISORY_MISSING')
                digest = files['ADVISORY.md']
                marker = 'PRO_PREP ' + job + ' ' + digest
                source = relay.JOBS / job / 'outbox'
                dest = ROOT / 'reports/ops/fleet-inbox' / row['issue'] / 'pro' / job
                if any(p.is_symlink() for p in (dest, *dest.parents) if p != ROOT.parent):
                    raise ValueError('PREP_DESTINATION_SYMLINK')
                dest.mkdir(parents=True, exist_ok=True)
                advisory = ''
                for name, expected in files.items():
                    src = source / name; target = dest / name
                    if src.is_symlink() or target.is_symlink() or (target.exists() and hash_file(target) != expected):
                        raise ValueError('PREP_COPY_DRIFT')
                    content = src.read_bytes()
                    if hashlib.sha256(content).hexdigest() != expected:
                        raise ValueError('PREP_COPY_DRIFT')
                    if name == 'ADVISORY.md':
                        advisory = content.decode('utf-8')
                    if not target.exists():
                        with target.open('xb') as out:
                            out.write(content); out.flush(); os.fsync(out.fileno())
                    if hash_file(target) != expected:
                        raise ValueError('PREP_COPY_DRIFT')
                issue = client.operation('issue_get', {'id': row['issue']})['issue']
                if issue.get('identifier') != row['issue'] or issue.get('team', {}).get('key') != 'GLP':
                    raise ValueError('PREP_ISSUE_MISMATCH')
                body = ('Pro implementation prep (advisory, not acceptance).\n\n' + marker +
                        '\nAccount: ' + str(row.get('account', 'unknown')) +
                        '\nArtifacts: ' + str(dest.relative_to(ROOT)) +
                        '\nNative manifest SHA256: ' + files['OUTPUT-MANIFEST.json'] +
                        '\n\n' + advisory)
                body_sha = hashlib.sha256(body.encode()).hexdigest()
                if started and receipt.get('body_sha256') != body_sha:
                    raise ValueError('PREP_POST_CUSTODY_DRIFT')
                comments = client.pages(comments_query, {'id': issue['id']}, ('issue', 'comments'))
                existing = [c for c in comments if c.get('user', {}).get('id') == author and
                            marker in c.get('body', '').splitlines() and
                            hashlib.sha256(c['body'].encode()).hexdigest() == body_sha]
                if len(existing) > 1:
                    raise ValueError('PREP_DUPLICATE_COMMENT')
                if existing:
                    result = existing[0]
                elif started:
                    raise ValueError('PREP_POST_UNCERTAIN_NO_REPLAY')
                else:
                    # Fleet client remains authoritative for admitted-issue scope.
                    # Backlog/closed refusals need the parent, never a role switch.
                    if hasattr(client, '_scoped_target'):
                        client._scoped_target(row['issue'])
                    receipt = {'schema': 'pro-prep-delivery-v1', 'state': 'posting', 'job': job,
                               'issue': row['issue'], 'artifact_sha256': digest,
                               'body_sha256': hashlib.sha256(body.encode()).hexdigest(), 'files': files,
                               'started_at': relay.now_iso()}
                    relay._atomic_json(path, receipt)
                    started = True
                    result = writer(client, 'comment', {'issueId': row['issue'], 'body': body})
                if (not result.get('id') or result.get('issue', {}).get('id') != issue['id'] or
                        marker not in result.get('body', '')):
                    raise ValueError('PREP_COMMENT_READBACK_MISMATCH')
                receipt.update(state='delivered', comment_id=result['id'],
                               comment_body_sha256=hashlib.sha256(result['body'].encode()).hexdigest(),
                               issue=row['issue'], job=job, files=files, artifact_sha256=digest,
                               delivered_at=relay.now_iso())
            except Exception as exc:
                receipt.update(state='uncertain' if started else 'blocked', job=job,
                               issue=row['issue'], error=str(exc), checked_at=relay.now_iso())
            relay._atomic_json(path, receipt)
            results.append({'job': job, 'issue': row['issue'], 'state': receipt['state'],
                            'comment_id': receipt.get('comment_id'), 'error': receipt.get('error')})
    return results


def open_prep_issues():
    """Fresh read-only Linear census; stale PLAN status is not an open issue."""
    sys.path.append(str(ROOT / 'scripts'))
    from linear_client import Client
    client = Client(role='fleet', max_retries=0)
    query = '''query($after:String){issues(first:50,after:$after,filter:{team:{key:{eq:"GLP"}},state:{type:{nin:["completed","canceled","backlog"]}}}){nodes{identifier} pageInfo{hasNextPage endCursor}}}'''
    return {r['identifier'] for r in client.pages(query, {}, ('issues',))}


def send_refill_note(body):
    """Use the existing authorized ops-alerts webhook; never disclose its URL."""
    import urllib.request
    url = (Path.home() / '.hermes/secrets/n8n/ops-alerts-webhook.txt').read_text().strip()
    request = urllib.request.Request(url + ('&' if '?' in url else '?') + 'wait=true',
                                    data=json.dumps({'content': body}).encode(), method='POST',
                                    headers={'Content-Type': 'application/json', 'User-Agent': 'glp-pro-refill/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        reply = json.loads(response.read())
    if not reply.get('id') or str(reply.get('channel_id')) != '1487300132054765629':
        raise ValueError('REFILL_NOTE_READBACK_MISMATCH')
    return {'message_id': str(reply['id']), 'channel': 'ops-alerts'}


def refill_alert():
    """Alert once per shortage episode, never mint/replay prompts or uncertain posts.

    Six is ready-to-submit supply, NOT running chats or uncertain/outbox-held jobs.
    Explicit second-round prompts count only when not blocked; arbitrary old prep
    comments or repeated first-round successes never cover the second-round need.
    """
    with singleton():
        control_path = directory() / 'CONTROL.json'
        control = json.loads(control_path.read_text()) if control_path.exists() else {}
        if not control.get('refill_plan'):
            return {'state': 'unconfigured'}
        plan_source = Path(control['refill_plan'])
        plan_path = plan_source.resolve(strict=True)
        if not plan_path.is_relative_to(ROOT.resolve()) or plan_source.is_symlink():
            raise ValueError('REFILL_PLAN_OUTSIDE_ROOT')
        rows = request_rows()
        queued = sum(r['state'] == 'queued' and not relay.job_state(r['job']) for r in rows)
        path = directory() / 'REFILL-ALERT.json'
        prior = json.loads(path.read_text()) if path.exists() else {}
        if queued >= 6:
            receipt = {'schema': 'pro-refill-alert-v1', 'state': 'stocked',
                       'unsubmitted': queued, 'checked_at': relay.now_iso()}
            if prior.get('state') not in ('stocked', 'posting', 'uncertain'):
                relay._atomic_json(path, receipt)
            return receipt
        # Posting/uncertain is a durable no-replay fence; even refills cannot
        # erase ambiguous custody. The parent must reconcile the exact note.
        if prior.get('state') in ('posting', 'uncertain', 'delivered'):
            return {**prior, 'unsubmitted': queued, 'retained': True}
        covered = set()
        for row in rows:
            name = row.get('backlog_file', '')
            if (row['state'] not in ('queued', 'running', 'complete') or
                    not re.fullmatch(r'[0-9]+-GLP-[1-9][0-9]*-(auto|acct[123])\.md', name)):
                continue
            prompt = directory() / 'backlog' / name
            lines = prompt.read_text().splitlines() if prompt.is_file() and not prompt.is_symlink() else []
            if lines and 'SECOND-ROUND PREP' in lines[0]:
                covered.add(row['issue'])
        live = open_prep_issues()
        plan = json.loads(plan_path.read_text())
        lacking = list(dict.fromkeys(r['issue'] for r in sorted(plan['issues'], key=lambda r: (r['order'], r['issue']))
                                     if r['issue'] in live and r['issue'] not in covered))
        body = ('ops-alerts: Pro prompt supply below refill threshold: ' + str(queued) +
                '/6 unsubmitted. Issues lacking usable second-round prep (PLAN order): ' +
                (', '.join(lacking) or 'none; replenish deeper source-bound work for open PLAN issues') +
                '. Owner: coordinator/prep supplier. Use installed backlog; no ambiguous resends, '
                'no automatic prompt minting. Pro remains research/planning only.')
        receipt = {'schema': 'pro-refill-alert-v1', 'state': 'posting', 'unsubmitted': queued,
                   'issues_lacking_second_round': lacking, 'plan_sha256': hash_file(plan_path),
                   'body_sha256': hashlib.sha256(body.encode()).hexdigest(), 'started_at': relay.now_iso()}
        relay._atomic_json(path, receipt)  # BEFORE outward effect
        try:
            reply = send_refill_note(body)
            receipt.update(state='delivered', **reply, delivered_at=relay.now_iso())
        except Exception as exc:
            # Errors can contain webhook URLs; retain only a nonsecret type.
            receipt.update(state='uncertain', error=type(exc).__name__)
        relay._atomic_json(path, receipt)
        return receipt


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
    sub.add_parser('consume', help='deliver only explicitly scoped VERIFIED prep outboxes; comment-only')
    fetch = sub.add_parser('fetch', help='poll/fetch exact existing job; never submits')
    fetch.add_argument('--job', required=True)
    assign_parser = sub.add_parser('assign', help='change an unreserved queued request account')
    assign_parser.add_argument('--job', required=True)
    assign_parser.add_argument('--account', choices=['auto', 'acct1', 'acct2', 'acct3'], required=True)
    replace = sub.add_parser('replace-superseded', help='queue-only same-prompt successor after reviewed relay disposition')
    replace.add_argument('--job', required=True)
    replace.add_argument('--ruling', required=True)
    requeue = sub.add_parser('requeue-unsent', help='explicit opt-in after maintained WRONG_MODEL unsent reconciliation')
    requeue.add_argument('--job', required=True)
    requeue.add_argument('--authorize-preimage', required=True)
    args = parser.parse_args(argv)
    if args.command == 'replace-superseded':
        row = enqueue_replacement(args.job, args.ruling)
        print(json.dumps({'job': row['job'], 'state': row['state'], 'supersedes': args.job}), flush=True)
    elif args.command == 'assign':
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
        deliveries = consume_outboxes()
        note = refill_alert()
        if results or deliveries or note.get('state') not in ('stocked', 'unconfigured'):
            print(json.dumps({'actions': results, 'prep_deliveries': deliveries, 'refill': note}), flush=True)
    elif args.command == 'consume':
        print(json.dumps(consume_outboxes()), flush=True)
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
