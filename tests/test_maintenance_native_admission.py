"""Native admission uses disposable ledgers/proc fixtures, never live scheduling."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import maintenance_admission as admission

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
ENTRY = {'kind': 'hermes', 'id': 'job'}


@pytest.fixture
def native(tmp_path):
    cron = tmp_path / 'cron'
    cron.mkdir()
    proc = tmp_path / 'proc'
    proc.mkdir()
    job = {}
    locks = {'free': True}

    @contextmanager
    def fire_lock(jid):
        assert jid == 'job'
        yield locks['free']

    def claim_live(claim, now, ttl):
        at = admission._native_timestamp(claim.get('at')) if isinstance(claim, dict) else None
        return bool(at and 0 <= (now - at).total_seconds() < ttl)

    scheduler = SimpleNamespace(
        _current_cron_store=lambda: SimpleNamespace(cron_dir=cron),
        _fire_job_lock=fire_lock, _hermes_now=lambda: NOW,
        FIRE_CLAIM_TTL_SECONDS=60, _oneshot_run_claim_ttl_seconds=lambda: 120,
        _claim_is_live=claim_live,
    )
    return SimpleNamespace(cron=cron, proc=proc, job=job, locks=locks, scheduler=scheduler,
                           run=lambda: admission.native_runs(scheduler, [ENTRY], lambda _: job, proc=proc))


def process(native, pid=42, start=123, argv=('python', '/opt/hermes/gateway.py')):
    base = native.proc / str(pid)
    base.mkdir()
    fields = ['S', '1'] + ['0'] * 17 + [str(start)]
    (base / 'stat').write_text(str(pid) + ' (python) ' + ' '.join(fields))
    (base / 'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in argv))
    return base


def ledger(native, *, status='running', pid=42, started=123, pending=0):
    database = native.cron / 'executions.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE executions (id TEXT, job_id TEXT, pid INTEGER, process_started_at INTEGER, '
                   'status TEXT, claimed_at TEXT, finished_at TEXT, handoff_pending INTEGER)')
        db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?)',
                   ('attempt', 'job', pid, started, status,
                    (NOW - timedelta(minutes=20)).isoformat(),
                    (NOW - timedelta(minutes=5)).isoformat() if status in {'completed', 'failed'} else None, pending))
    return database


def stale_claim(native, owner=None, at=None):
    native.job['fire_claim'] = {'at': at or (NOW - timedelta(minutes=10)).isoformat(),
                                'by': owner or socket.gethostname() + ':42:token'}


def test_absent_ledger_without_claims_has_no_side_effects(native):
    assert native.run() == ([], set())
    assert list(native.cron.iterdir()) == []


def test_live_gateway_execution_blocks_job_without_rooting_gateway_tree(native):
    process(native)
    database = ledger(native)
    before = (database.read_bytes(), database.stat().st_mtime_ns)
    busy, owners = native.run()
    assert busy == [{'kind': 'hermes', 'id': 'job', 'reason': 'native_execution_owner_live'}]
    assert owners == set()
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before


def test_exact_external_worker_identity_roots_only_that_tree(native):
    payload = str(native.cron / 'external-workers/attempt.json')
    process(native, argv=('python', '-m', 'cron.scheduler', '--external-worker-file', payload))
    ledger(native)
    busy, owners = native.run()
    assert owners == {42}
    assert any(row['reason'] == 'native_external_worker_alive' for row in busy)


@pytest.mark.parametrize('status', ['claimed', 'running', 'unknown'])
def test_dead_execution_owner_and_valid_expired_claim_are_drained(native, status):
    ledger(native, status=status)
    stale_claim(native)
    assert native.run() == ([], set())


def test_pending_handoff_never_becomes_drained_from_owner_death(native):
    ledger(native, status='claimed', pending=1)
    assert native.run()[0][0]['reason'] == 'native_execution_handoff_pending'


def test_completed_gateway_attempt_authenticates_expired_claim(native):
    process(native)
    ledger(native, status='completed')
    stale_claim(native)
    assert native.run() == ([], set())


@pytest.mark.parametrize('at,owner', [(None, 'foreign-host:42'), ('junk', None),
                                    ('2040-01-01T00:00:00+00:00', None)])
def test_unresolved_expiry_or_foreign_owner_blocks(native, at, owner):
    stale_claim(native, owner=owner, at=at)
    assert native.run()[0]


def test_expired_same_host_live_owner_without_ledger_does_not_disappear(native):
    process(native)
    stale_claim(native)
    assert native.run()[0][0]['reason'] == 'native_unattributed_fire_claim'


def test_native_fresh_claim_and_held_fire_lock_still_block(native):
    native.job['run_claim'] = {'at': NOW.isoformat(), 'by': socket.gethostname() + ':42'}
    native.locks['free'] = False
    reasons = {row['reason'] for row in native.run()[0]}
    assert reasons == {'native_fire_lock_or_running', 'native_live_run_claim'}


def test_live_mismatched_owner_fingerprint_is_not_proof_of_death(native):
    process(native, start=456)
    ledger(native)
    assert native.run()[0][0]['reason'] == 'native_execution_owner_identity_changed'


def test_unreadable_owner_remains_an_identified_blocker(native, monkeypatch):
    base = process(native)
    ledger(native)
    read = Path.read_text
    def denied(path, *args, **kwargs):
        if path == base / 'stat':
            raise PermissionError('fixture unreadable related owner')
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', denied)
    assert native.run()[0][0]['reason'] == 'native_execution_owner_unreadable'


def test_corrupt_ledger_refuses_even_without_job_claim(native):
    (native.cron / 'executions.db').write_bytes(b'corrupt fixture')
    assert native.run()[0][0]['reason'] == 'native_execution_ledger_unreadable'


def test_unknown_live_gateway_execution_is_not_terminal_drain_evidence(native):
    process(native)
    database = ledger(native, status='unknown')
    # Native stale recovery may label a live wedged owner unknown and finished.
    with sqlite3.connect(database) as db:
        db.execute('UPDATE executions SET finished_at=?', ((NOW - timedelta(minutes=5)).isoformat(),))
    stale_claim(native)
    busy, owners = native.run()
    assert owners == set()
    assert {r['reason'] for r in busy} == {'native_execution_owner_live', 'native_unattributed_fire_claim'}
