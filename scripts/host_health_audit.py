#!/usr/bin/env python3
"""Bounded local kael-audit replacement: observation, never remediation or sends.

A successful audit means a fresh report was produced, NOT that the host is healthy.
The assessment and findings preserve unhealthy observations explicitly.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import tempfile

HOME = Path('/home/ubuntu/.hermes')
DEFAULT_REPORT = Path('/home/ubuntu/business/reports/host-health-audit')
FLEET_DISK_FLOOR_BYTES = 100_000_000_000


def write_json(path: Path, value: dict) -> None:
    # mkstemp uses O_CREAT|O_EXCL: never truncate a pre-existing link/inode.
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def snapshot() -> dict:
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        if key in {'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'}:
            memory[key] = int(value.split()[0]) * 1024
    pressure = {}
    for line in Path('/proc/pressure/memory').read_text().splitlines():
        kind, *fields = line.split()
        pressure[kind] = {k: float(v) for k, v in (x.split('=') for x in fields)}
    processes = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            fields = dict(x.split(':', 1) for x in (proc / 'status').read_text().splitlines()
                          if ':' in x)
            processes.append({'pid': int(proc.name), 'ppid': int(fields['PPid']),
                              'name': fields['Name'].strip(),
                              'rss_bytes': int(fields.get('VmRSS', '0').split()[0]) * 1024,
                              'swap_bytes': int(fields.get('VmSwap', '0').split()[0]) * 1024})
        except (OSError, ValueError, KeyError):
            continue
    findings = []
    state: dict[str, int | None] = {'fleet_threshold_bytes': 8_000_000_000}
    for suffix, key in [('', 'main_bytes'), ('-wal', 'wal_bytes'), ('-shm', 'shm_bytes')]:
        path = HOME / ('state.db' + suffix)
        state[key] = path.stat().st_size if path.exists() else (None if not suffix else 0)
    if state['main_bytes'] is None:
        findings.append('state_db_missing')
    try:
        jobs_data = json.loads((HOME / 'cron/jobs.json').read_text())
        jobs = jobs_data.get('jobs', []) if isinstance(jobs_data, dict) else jobs_data
        if not isinstance(jobs, list):
            raise ValueError('invalid jobs registry')
    except (OSError, ValueError):
        jobs = []
        findings.append('housekeeping_registry_unavailable')
    selected = []
    for job in jobs:
        if job.get('id') in {'fbf9ba7b76c2', '075ed81c6b43', 'a0d5cb0609b9', '419c6ade4e69'}:
            selected.append({key: job.get(key) for key in
                             ['id', 'name', 'enabled', 'script', 'schedule', 'next_run_at', 'last_status']})
    reservation_file = HOME / 'cache/delegation-host-budget.json'
    try:
        reservations = json.loads(reservation_file.read_text())
        if not isinstance(reservations, list):
            raise ValueError('invalid capacity ledger')
        active = 0
    except (OSError, ValueError):
        reservations = []
        active = None
        findings.append('capacity_ledger_unavailable')
    tokens = set()
    unverifiable = False
    for row in reservations:
        if (not isinstance(row, dict) or type(row.get('pid')) is not int or row['pid'] <= 0
                or not isinstance(row.get('start'), str) or not row['start'].isascii()
                or not row['start'].isdigit() or not isinstance(row.get('token'), str)
                or not row['token'] or row['token'] in tokens):
            unverifiable = True
            continue
        tokens.add(row['token'])
        try:
            # Use /proc start ticks to exclude reused/dead PIDs; no process signalling.
            fields = Path(f"/proc/{row['pid']}/stat").read_text().rsplit(')', 1)[1].split()
            if fields[19] == row['start']:
                active = int(active or 0) + 1
        except (FileNotFoundError, ProcessLookupError):
            # Only a definitely absent owner is stale; denied reads are unknown.
            continue
        except (OSError, ValueError, IndexError):
            unverifiable = True
    if unverifiable:
        active = None
        findings.append('capacity_ledger_unverifiable')
    disk = shutil.disk_usage('/')
    # shutil.disk_usage.free is f_bavail on Linux: excludes reserved blocks.
    available_bytes = disk.free
    if available_bytes < FLEET_DISK_FLOOR_BYTES:
        findings.append('root_disk_below_fleet_floor')
    if state['main_bytes'] is not None and state['main_bytes'] >= 8_000_000_000:
        findings.append('state_db_over_fleet_threshold')
    if memory['SwapTotal'] and (memory['SwapTotal'] - memory['SwapFree']) / memory['SwapTotal'] >= .75:
        findings.append('swap_guard_high_water')
    if any(p['rss_bytes'] >= 4 * 1024**3 for p in processes):
        findings.append('swap_guard_runaway_rss')
    if state['main_bytes'] is not None and available_bytes < state['main_bytes'] + int(state['wal_bytes'] or 0) + 1024**3:
        findings.append('insufficient_state_backup_compaction_headroom')
    if any(j['id'] == 'fbf9ba7b76c2' and not j['enabled'] for j in selected):
        findings.append('legacy_session_pruner_disabled')
    return {'schema': 'glp-host-health-audit-v1',
            'observed_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'assessment': 'degraded' if findings else 'healthy', 'findings': findings,
            'memory': memory, 'memory_pressure': pressure, 'state_db': state,
            'root_disk': {'free_bytes': disk.free, 'total_bytes': disk.total,
                          'available_bytes': available_bytes,
                          'fleet_floor_bytes': FLEET_DISK_FLOOR_BYTES},
            'top_processes': sorted(processes, key=lambda x: x['rss_bytes'], reverse=True)[:10],
            'housekeeping_jobs': selected,
            'capacity': {'active_reservations': active, 'ledger_entries': len(reservations),
                         'eight_worker_24h_acceptance': 'not_established_by_point_snapshot'},
            'effects': {'process_signals': 0, 'db_writes': 0,
                        'external_sends': 0, 'inference_calls': 0}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    os.umask(0o077)
    args.report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = snapshot()
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    write_json(args.report_dir / ('audit-' + stamp + '.json'), report)
    write_json(args.report_dir / 'LATEST.json', report)
    print(json.dumps({'status': 'report_produced', 'assessment': report['assessment'],
                      'findings': report['findings'], 'receipt': str(args.report_dir / 'LATEST.json')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
