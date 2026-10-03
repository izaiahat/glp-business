#!/usr/bin/env python3
"""Restore ONE authenticated pruned UNKNOWN audit row, then use native disposition.

This is not retry/recovery-by-execution: no payload, scheduling or terminal-state
transition is allowed. The only INSERT is the byte-authenticated historical row.
A durable exclusive intent makes every interrupted attempt inspect-only. Existing
native PR4 admission, CAS, work locks and claim disposition remain unchanged.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

JOB = '69a28f878ce3'
EXECUTION = '437d02f1fc5748abafc0fb4080b03ad6'
HOME = Path('/home/ubuntu/.hermes')
ROOT = Path('/home/ubuntu/business')
LOCAL = 'c2636553424089c30489172420016777c707a30c64c695da6ce798135d8b382f'
REMOTE = 'f9dead271c3d5e35800c9e628e64636b95154c47d96f8ca90c210ccd3b79721e'
ROW = 'fc57a1b42bdc9b6a6b4890782350ae5d0c2a722ea6c9dc2ee55f2d33359350e1'
ARCHIVE = 'a918609f9d8372047fbc0a05d8f7cf426520b2e3c2cbbf58dc95512a76b8eef7'
NATIVE = {'jobs.py': '3a8e56584c14585f7d99ce6c2b1d803cddd3d87758f435cd58e32cb6a095837b',
          'executions.py': '6132c248aa4e835697161e115f168305803993eea60ad29a730ecadc6ee72644',
          'claim_disposition.py': 'f16a22a11befa4d1c29a64f7cf8dbab13d0c46d9e8fc33f3feb0930296422bd5'}


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def archived_row(local, remote):
    require(digest(local) == LOCAL and digest(remote) == REMOTE, 'historical_evidence_moved')
    evidence, r2 = json.loads(local), json.loads(remote)
    row = evidence['ledger']
    require(digest(canonical(row)) == ROW, 'historical_row_moved')
    require(row['id'] == EXECUTION and row['job_id'] == JOB and row['status'] == 'unknown', 'wrong_target')
    require(evidence['counts'] == {'MATCH': 66, 'MISSING': 8}, 'reconciliation_moved')
    require(r2['retrievable_exact_bytes'] and r2['retrieved_sha256'] == ARCHIVE, 'remote_receipt_moved')
    return row, evidence['archive']['path']


def insert_exact(conn, row):
    """Caller owns BEGIN IMMEDIATE and durable intent. Never replace/update a row."""
    require(digest(canonical(row)) == ROW, 'historical_row_moved')
    before = [dict(r) for r in conn.execute('SELECT * FROM executions ORDER BY id')]
    require(not any(r['job_id'] == JOB for r in before), 'target_ledger_not_empty')
    fields = [r[1] for r in conn.execute('PRAGMA table_info(executions)')]
    require(set(fields) == set(row), 'ledger_schema_moved')
    # Column identifiers come exclusively from the actual fixed native schema.
    require(all(k.replace('_', '').isalnum() for k in fields), 'unsafe_column')
    conn.execute('INSERT INTO executions (' + ','.join(fields) + ') VALUES (' + ','.join('?' for _ in fields) + ')',
                 [row[k] for k in fields])
    after = [dict(r) for r in conn.execute('SELECT * FROM executions ORDER BY id')]
    require(after == sorted(before + [row], key=lambda r: r['id']), 'ledger_insert_readback_moved')


def append(path, value, first=False):
    flags = os.O_WRONLY | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if first else os.O_APPEND)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(value, sort_keys=True) + '\n'); stream.flush(); os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run(apply=False):
    evidence_root = ROOT / 'reports/ops/f11-exact-recovery-20261002'
    local, remote = evidence_root / 'LOCAL-RECONCILIATION.json', evidence_root / 'R2-READBACK.json'
    row, archive = archived_row(local.read_bytes(), remote.read_bytes())
    archive = Path(archive)
    require(not archive.is_symlink() and digest(archive.read_bytes()) == ARCHIVE, 'archive_moved')
    for name, expected in NATIVE.items():
        require(digest((HOME / 'hermes-agent/cron' / name).read_bytes()) == expected, 'native_source_moved:' + name)
    sys.path.insert(0, str(HOME / 'hermes-agent'))
    from cron import claim_disposition as native, jobs
    require(native._home() == HOME, 'wrong_native_home')
    out = ROOT / 'reports/ops/glp110-ship-20261002'
    journal = out / 'RESTORE-PRUNED-EXECUTION.jsonl'
    require(not journal.exists(), 'restore_intent_exists_inspect_only')
    require(not native._journal_path(HOME, EXECUTION).exists(), 'disposition_intent_exists_inspect_only')
    with ExitStack() as stack:
        tick = stack.enter_context((HOME / 'cron/.tick.lock').open('a+'))
        fcntl.flock(tick, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(stack.enter_context(jobs._fire_job_lock(JOB)), 'fire_fence_unavailable')
        stack.enter_context(jobs._jobs_lock(require_cross_process=True))
        rows = native._jobs()
        job = next(j for j in rows if j['id'] == JOB)
        work = HOME / 'runstate/reports-archiver.lock'; st = work.lstat()
        now = datetime.now(timezone.utc)
        request = {'home': str(HOME), 'job': job, 'execution': row,
                   'created_at': now.isoformat(), 'expires_at': (now + timedelta(minutes=10)).isoformat(),
                   'reason': 'GLP110 restore authentic pruned UNKNOWN audit row; native non-executing disposition only. Archive has 66 matching and 8 missing source members; exact offhost HEAD+GET retained. No retry or terminal rewrite.',
                   'evidence': [{'path': str(p), 'sha256': digest(p.read_bytes())} for p in (local, remote)],
                   'work_locks': [{'path': str(work), 'device': st.st_dev, 'inode': st.st_ino}]}
        namespace = native._control_namespace(HOME, native._journal_path(HOME, EXECUTION))
        with native._ledger(HOME) as conn, native._work_locks(request):
            require(not native._rows(conn, JOB), 'target_ledger_not_empty')
            native._validate(request, rows, [row])
            native._check_control_namespace(HOME, native._journal_path(HOME, EXECUTION), namespace)
            require(not list(conn.execute("SELECT id FROM executions WHERE status IN ('claimed','running')")),
                    'native_executions_must_drain_before_restore')
            if not apply:
                return {'status': 'ready', 'execution_id': EXECUTION, 'historical_row_sha256': ROW}
            out.mkdir(parents=True, exist_ok=True)
            append(journal, {'event': 'intent', 'request': request, 'row_sha256': ROW}, first=True)
            insert_exact(conn, row)
            conn.commit()
            require(native._rows(conn, JOB) == [row], 'restoration_uncertain_inspect_only')
            append(journal, {'event': 'restored', 'row_sha256': ROW})
        # Keep tick/jobs/fire exclusion, release the work/ledger locks so the
        # unchanged native disposition can reacquire them in its own order.
        preview = native.dispose_claim(request)
        result = native.dispose_claim(request, apply=True)
        inspected = native.inspect_disposition(EXECUTION)
        require(inspected['status'] == 'applied', 'native_disposition_uncertain_inspect_only')
        append(journal, {'event': 'native_disposed', 'preview': preview, 'result': result, 'inspect': inspected})
        return {'status': 'restored_and_disposed', 'execution_status': 'unknown', 'journal': str(journal)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.apply), sort_keys=True))
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError) as exc:
        print(json.dumps({'status': 'refused_or_uncertain', 'error': str(exc), 'retry_allowed': False}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
