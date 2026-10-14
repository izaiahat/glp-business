#!/usr/bin/env python3
"""Pinned detached archive controller. Never stops writers or retries effects."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
HOME = Path('/home/ubuntu/.hermes')
ROOT = HOME / 'state-archive-weekly'
CHANNEL = '1487300132054765629'
WEBHOOK_FILE = HOME / 'secrets/n8n/ops-alerts-webhook.txt'
PYTHON = HOME / 'hermes-agent/venv/bin/python3'


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def check_pins(path: Path = HERE / 'weekly-pins.json') -> None:
    pins = json.loads(path.read_text())
    for name, digest in pins['files'].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('archive_source_pin_drift:' + Path(name).name)


def execute(argv: list[str], log: Path) -> int:
    with log.open('wb') as stream:
        # Detached service owns lifetime. No cron-sized timeout or automatic retry.
        return subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT,
                              check=False, cwd=HERE).returncode


def run_chain(root: Path, db: Path, config: Path, *, executor=execute,
              check_only: bool = False) -> dict:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with (root / 'controller.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'DEFERRED_LOCK', 'effects_started': False}
        active = root / 'ACTIVE.json'
        if active.exists():
            return {'status': 'BLOCKED_AMBIGUOUS_PRIOR_RUN', 'effects_started': False,
                    'prior_journal': str(active)}
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        run = root / stamp
        run.mkdir(mode=0o700)
        common = [str(PYTHON), str(HERE / 'archive.py'), '--db', str(db),
                  '--config', str(config), '--receipts', str(run / 'receipts'),
                  '--archive', str(run / 'archive.db')]
        result = {'status': 'IN_PROGRESS', 'run_id': stamp, 'run_dir': str(run),
                  'db': str(db), 'effects_started': False, 'steps': [],
                  'before_bytes': db.stat().st_size, 'quiet_window_acquired': False}

        def step(command: str, *args: str, effect: bool = False) -> int:
            entry = {'command': command, 'state': 'INTENT', 'effectful': effect}
            result['steps'].append(entry)
            save(run / 'RESULT.json', result)
            argv = common + (['--i-am-in-the-quiet-window'] if effect else []) + [command, *args]
            rc = executor(argv, run / f'{len(result["steps"]):02}-{command}.log')
            entry.update(state='RETURNED', exit_code=rc)
            save(run / 'RESULT.json', result)
            return rc

        try:
            if step('preflight') != 0:
                result['status'] = 'REFUSED_PREFLIGHT'
            elif check_only:
                result['status'] = 'CHECK_ONLY_PASS_NO_EFFECTS'
            elif step('plan', '--days', '7') != 0 or step('preflight') != 0:
                result['status'] = 'REFUSED_PLAN_OR_PREFLIGHT'
            else:
                # A PASS is an observation, not a lock against future process starts.
                # The operator must keep the quiet window throughout this run.
                # Journal before any effect; never silently re-plan or replay it.
                save(active, {'run_id': stamp, 'run_dir': str(run)})
                result['effects_started'] = True
                failed = False
                for command, args, effect in [
                    ('copy', [], True), ('verify', [], False),
                    ('delete', [], True), ('rebuild-fts', [], True),
                    ('vacuum', [], True), ('report', ['--stage', 'after'], False),
                ]:
                    if effect and step('preflight') != 0:
                        result['status'] = 'BLOCKED_WRITER_REAPPEARED'
                        failed = True
                        break
                    if step(command, *args, effect=effect) != 0:
                        result['status'] = 'BLOCKED_' + command.upper().replace('-', '_')
                        failed = True
                        break
                if not failed:
                    result['status'] = 'ARCHIVE_VERIFIED'
                    save(run / 'RESULT.json', result)
                    # Only exact successful completion clears ambiguous-effect recovery.
                    active.unlink()
        except Exception as exc:
            result['status'] = 'BLOCKED_CONTROLLER_EXCEPTION'
            result['error_type'] = type(exc).__name__
        result['after_bytes'] = db.stat().st_size if db.exists() else None
        save(run / 'RESULT.json', result)
        save(root / 'LATEST.json', result)
        return result


def format_receipt(result: dict) -> str:
    return ('⚙️ **State Archive** — ' + result['status'] +
            '; bytes ' + str(result.get('before_bytes', 'UNKNOWN')) + ' → ' +
            str(result.get('after_bytes', 'UNKNOWN')) + '; effects_started=' +
            str(result.get('effects_started', False)).lower() +
            '; receipt ' + result.get('run_id', 'prior journal'))


def retain_completed_archive(result: dict, *, api=None) -> dict:
    """Compress at the first safe terminal point, without opening the live DB.

    The controller still needs its uncompressed archive through delete/report.
    Do not compress inside cmd_verify, change its contract, or bypass the journal.
    """
    if result.get('status') != 'ARCHIVE_VERIFIED':
        return {'status': 'NOT_ELIGIBLE', 'effects_started': False}
    run = Path(result['run_dir'])
    verify_paths = sorted((run/'receipts').glob('*-verify.json'))
    if not verify_paths:
        return {'status': 'RETENTION_REFUSED', 'reason': 'missing_verify_receipt'}
    verify = verify_paths[-1]
    proof = json.loads(verify.read_text())
    if Path(proof.get('archive', '')) != run/'archive.db':
        return {'status': 'RETENTION_REFUSED', 'reason': 'archive_run_binding_mismatch'}
    written = dt.datetime.fromisoformat(proof['written_at'].replace('Z', '+00:00'))
    if written.tzinfo is None:
        return {'status': 'RETENTION_REFUSED', 'reason': 'verify_timezone_unknown'}
    entry = {'verify': {'path': str(verify),
                        'sha256': hashlib.sha256(verify.read_bytes()).hexdigest()},
             'verified_at_epoch': written.timestamp()}
    # Reuse the pruner's artifact-only implementation. No state.db access here.
    sys.path.insert(0, str(HERE.parent))
    from disk_retention import archive_retention
    if api is None:
        import host_disk_pruner as api
    rows = archive_retention(api, {'archives': [entry]}, dry_run=False)
    retained = rows[0]
    save(run/'RETENTION.json', {'schema': 'k4c-archive-retention-v1',
                               'entry': entry, 'result': retained})
    return retained


def notify(result: dict, root: Path = ROOT) -> dict:
    """One attempted post, exact target readback. Ambiguous sends are never retried."""
    key = result.get('run_id', 'blocked-prior')
    outbox = root / 'delivery' / (key + '.json')
    if outbox.exists():
        return json.loads(outbox.read_text())
    message = format_receipt(result)
    delivery = {'status': 'PREPARED', 'channel_id': CHANNEL, 'content': message}
    save(outbox, delivery)
    try:
        fd = os.open(WEBHOOK_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise RuntimeError('webhook_file_permissions')
            url = os.read(fd, 8192).decode().strip()
        finally:
            os.close(fd)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'discord.com' or '/webhooks/' not in parsed.path:
            raise RuntimeError('webhook_endpoint_invalid')
        base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))

        def request(endpoint, body=None):
            req = urllib.request.Request(endpoint, data=json.dumps(body).encode() if body is not None else None,
                                         headers={'Content-Type': 'application/json', 'User-Agent': 'HermesStateArchive/1'})
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.load(response)

        identity = request(base)
        if str(identity.get('channel_id')) != CHANNEL:
            raise RuntimeError('wrong_channel')
        delivery['status'] = 'DISPATCH_INTENT'
        save(outbox, delivery)
        sent = request(base + '?wait=true', {'content': message, 'allowed_mentions': {'parse': []}})
        mid = str(sent['id'])
        delivery.update(status='ACKNOWLEDGED_READBACK_PENDING', message_id=mid)
        save(outbox, delivery)
        readback = request(base + '/messages/' + mid)
        if readback.get('content') != message or str(readback.get('channel_id')) != CHANNEL:
            raise RuntimeError('message_readback_mismatch')
        delivery['status'] = 'READBACK_VERIFIED'
    except Exception as exc:
        delivery['error_type'] = type(exc).__name__
        delivery['status'] = ('QUEUED_NO_POST' if delivery['status'] == 'PREPARED'
                              else 'AMBIGUOUS_NO_RETRY')
    save(outbox, delivery)
    return delivery


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--no-notify', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    check_pins()
    result = run_chain(ROOT, HOME / 'state.db', HOME / 'config.yaml', check_only=args.check_only)
    if result.get('status') == 'ARCHIVE_VERIFIED':
        try:
            result['retention'] = retain_completed_archive(result)
        except Exception as exc:
            result['retention'] = {'status': 'RETENTION_REFUSED', 'error_type': type(exc).__name__}
        if result['retention'].get('status') != 'VERIFIED_COMPRESSED':
            result['status'] = 'ARCHIVE_VERIFIED_RETENTION_FAILED'
        save(Path(result['run_dir'])/'RESULT.json', result)
        save(ROOT/'LATEST.json', result)
    if not args.no_notify:
        notify(result)
    print(format_receipt(result))
    return 0 if result['status'] in {'ARCHIVE_VERIFIED', 'CHECK_ONLY_PASS_NO_EFFECTS'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
