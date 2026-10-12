#!/usr/bin/env python3
"""Guarded host maintenance: reap proven dormant local daemons; observe cold swap.
No model/context changes, swap mutation, pattern kills, or protected CDP browser kills.
Cold live heap is not garbage: swapoff creates page-in IO and later swap-out churn.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

HOME = Path('/home/ubuntu/.hermes')
STATE = HOME / 'runstate' / 'host-swap-cleaner'
SWAPS = {'/swapfile', '/swapfile2', '/swapfile-rescue-20261002', '/swapfile-headroom-20261002'}
MARGIN_KIB = 6 * 1024 * 1024
MIN_IDLE = 86400
COOLDOWN = 86400


def atomic_json(path, data):
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def memory():
    d = {k: int(v.split()[0]) for k, v in (x.split(':', 1) for x in Path('/proc/meminfo').read_text().splitlines())}
    return {'total_kib': d['MemTotal'], 'available_kib': d['MemAvailable'], 'swap_total_kib': d['SwapTotal'],
            'swap_used_kib': d['SwapTotal'] - d['SwapFree'], 'load1': os.getloadavg()[0]}


def snapshot(pid):
    p = Path('/proc') / str(pid)
    stat = (p / 'stat').read_text().rsplit(')', 1)[1].split()
    args = (p / 'cmdline').read_bytes().split(b'\0')
    env = dict(x.split(b'=', 1) for x in (p / 'environ').read_bytes().split(b'\0') if b'=' in x)
    return {'pid': pid, 'ppid': int(stat[1]), 'start': int(stat[19]), 'ticks': int(stat[11]) + int(stat[12]),
            'io': (p / 'io').read_text(), 'args': args, 'env': env,
            'age': float(Path('/proc/uptime').read_text().split()[0]) - int(stat[19]) / os.sysconf('SC_CLK_TCK')}


def census():
    result = {}
    for p in Path('/proc').iterdir():
        if p.name.isdigit():
            try:
                result[int(p.name)] = snapshot(int(p.name))
            except (OSError, ValueError):
                pass
    return result


def socket_rows():
    return (subprocess.check_output(['ss', '-tanpH'], text=True).splitlines(),
            subprocess.check_output(['ss', '-xapnH'], text=True).splitlines())


def dormant_daemon(row, processes, sockets, now):
    """Require old task-scoped local daemon, no namespace caller, child or peer.
    Cloud sessions and operator/default namespaces are never candidates.
    UNIX self-pairs (asyncio wakeup) are the only established peer exemption.
    """
    if b'browser_harness.daemon' not in row['args'] or row['ppid'] != 1 or row['age'] < MIN_IDLE:
        return False
    env = row['env']
    name = env.get(b'BU_NAME', b'default').decode(errors='replace')
    if env.get(b'BU_BROWSER_ID') or name == 'default' or any(t in name.lower() for t in ('operator', 'chatgpt', 'claude', 'observe')):
        return False
    if any(p['ppid'] == row['pid'] for p in processes.values()):
        return False
    if any(p['pid'] != row['pid'] and b'browser_harness.daemon' not in p['args'] and
           p['env'].get(b'BU_NAME') == env.get(b'BU_NAME') for p in processes.values()):
        return False
    try:
        log = Path('/proc', str(row['pid']), 'fd', '2')
        if not log.resolve().name.endswith('.log') or now - log.stat().st_mtime < MIN_IDLE:
            return False
        own = {m.group(1) for f in Path('/proc', str(row['pid']), 'fd').iterdir()
               if (m := re.fullmatch(r'socket:\[(\d+)\]', str(f.readlink())))}
    except OSError:
        return False
    marker = f'pid={row["pid"]},'
    tcp, unix = sockets
    # Refuse even a CONNECTING TCP socket, not just ESTAB.
    if any(marker in l and l.split()[0] != 'LISTEN' for l in tcp):
        return False
    matched = [l for l in unix if marker in l]
    if not matched:
        return False
    for line in matched:
        parts = line.split()
        if parts[1] == 'LISTEN':
            continue
        if parts[1] != 'ESTAB' or len(parts) < 8 or parts[5] not in own or parts[7] not in own:
            return False
    return True


def orphan_browser(row, processes, sockets):
    args = b' '.join(row['args'])
    if row['ppid'] != 1 or row['age'] < MIN_IDLE or b'chrome' not in row['args'][0] or b'--headless' not in args:
        return False
    if any(t in args for t in (b'--remote-debugging', b'katalys', b'awin', b'--type=')):
        return False
    tree = {row['pid']}
    while True:
        extended = tree | {p['pid'] for p in processes.values() if p['ppid'] in tree}
        if tree == extended:
            break
        tree = extended
    return not any(any(f'pid={pid},' in line for pid in tree) and ('ESTAB' in line or 'SYN-SENT' in line)
                   for line in sockets[0])


def reap(apply):
    first = census()
    sockets = socket_rows()
    picked = [r for r in first.values() if dormant_daemon(r, first, sockets, time.time()) or orphan_browser(r, first, sockets)]
    result = {'candidates': [r['pid'] for r in picked], 'terminated': [], 'refused': []}
    if not apply or not picked:
        return result
    time.sleep(2)
    second = census()
    sockets = socket_rows()
    for old in picked:
        pid = old['pid']
        current = second.get(pid)
        if not current or current['start'] != old['start'] or current['ticks'] != old['ticks'] or current['io'] != old['io']:
            result['refused'].append(pid)
            continue
        if not (dormant_daemon(current, second, sockets, time.time()) or orphan_browser(current, second, sockets)):
            result['refused'].append(pid)
            continue
        fd = None
        try:
            fd = os.pidfd_open(pid)
            # PID identity, argv, parent and activity must still match after pinning.
            live = snapshot(pid)
            if any(live[k] != current[k] for k in ('start', 'args', 'ppid', 'ticks', 'io')):
                result['refused'].append(pid)
                continue
            # Refresh connectivity immediately before the per-PID decision.
            fresh_sockets = socket_rows()
            if not (dormant_daemon(live, second, fresh_sockets, time.time()) or orphan_browser(live, second, fresh_sockets)):
                result['refused'].append(pid)
                continue
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            result['terminated'].append(pid)
        except (OSError, ValueError):
            result['refused'].append(pid)
        finally:
            if fd is not None:
                os.close(fd)
    return result


def vm_sample():
    return {k: int(v) for k, v in (l.split() for l in Path('/proc/vmstat').read_text().splitlines())}


def psi_full():
    match = re.search(r'full avg10=([\d.]+)', Path('/proc/pressure/memory').read_text())
    return float(match.group(1)) if match else 100.0


def guard(mem, full, swap_mib_s):
    if mem['available_kib'] < mem['swap_used_kib'] + MARGIN_KIB:
        return 'insufficient_available_RAM_plus_6GiB_reserve'
    if mem['load1'] > 4.0:
        return 'load_above_4'
    if full >= 0.5 or swap_mib_s >= 0.25:
        return 'active_PSI_or_swap_IO'
    return None


def swaps():
    rows = []
    for line in Path('/proc/swaps').read_text().splitlines()[1:]:
        name, kind, size, used, priority = line.split()
        if name in SWAPS and kind == 'file':
            rows.append({'path': name, 'size_kib': int(size), 'used_kib': int(used), 'priority': int(priority)})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--all-files', action='store_true', help='legacy no-effect option; automatic swap rotation is retired')
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'skip', 'reason': 'maintenance_lock_busy'}))
            return 0
        receipt = {'at': time.time(), 'apply': args.apply, 'before': memory(), 'reaper': reap(args.apply), 'rotations': []}
        if receipt['reaper']['terminated']:
            time.sleep(2)
        vm = vm_sample()
        start = time.monotonic()
        time.sleep(2)
        after_vm = vm_sample()
        rate = sum(after_vm[k] - vm[k] for k in ('pswpin', 'pswpout')) * os.sysconf('SC_PAGE_SIZE') / 1048576 / (time.monotonic() - start)
        full = psi_full()
        receipt['swap_io_mib_s'] = rate
        receipt['psi_full_avg10'] = full
        receipt['skip_reason'] = guard(memory(), full, rate)
        # A quiet two-second sample does not make cold live pages reclaimable.
        # Historical cron rotations forced 2.5-2.7GiB back in while workers ran.
        # Preserve pressure guard measurements and orphan protections, but never
        # manufacture swap IO as housekeeping. --all-files is legacy/no-effect.
        receipt['swap_rotation_policy'] = 'disabled_cold_live_heap'
        receipt['after'] = memory()
        receipt['status'] = 'error' if any(r['swapoff_rc'] or r['swapon_rc'] or not r['active_after'] for r in receipt['rotations']) else 'ok'
        atomic_json(STATE / 'latest.json', receipt)
        atomic_json(STATE / (time.strftime('%Y%m%dT%H%M%S', time.gmtime()) + '.json'), receipt)
        print(json.dumps(receipt))
        return 1 if receipt['status'] == 'error' else 0


if __name__ == '__main__':
    raise SystemExit(main())
