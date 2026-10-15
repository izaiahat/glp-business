#!/usr/bin/env python3
"""Admit one no-model hourly coordinator; never inject into a running TUI."""
import json
import os
from pathlib import Path
import subprocess  # retained import for existing no-spawn guard regressions
import sys
import auto_coordinator_guard as guard
import auto_coordinator_receipt as receipt
import auto_coordinator_loop as loop

ROOT = Path('/home/ubuntu/business')


def main():
    state = guard.inspect()
    if state['status'] != 'IDLE':
        print(json.dumps(state), flush=True)
        return 10
    state = guard.inspect_idle_tui()
    if state['status'] != 'IDLE':
        print(json.dumps(state), flush=True)
        return 10
    # The existing wrapper owns the singleton. Children inherit it, so a parent
    # crash cannot let a second hourly pass race an orphaned worker.
    try:
        lock_fd = int(os.environ['GLP_AUTO_COORDINATOR_LOCK_FD'])
        os.fstat(lock_fd)
    except (KeyError, ValueError, OSError):
        print('SINGLETON_FD_REQUIRED', flush=True)
        return 10
    sys.path.insert(0, '/home/ubuntu/.hermes/hermes-agent')
    from tools.delegation_admission import admission_problem, try_reserve_host_children
    problem = admission_problem()
    if problem:
        print('ADMISSION_REFUSED ' + str(problem), flush=True)
        return 10
    leases, active, limit = try_reserve_host_children(1)
    if leases is None:
        print('HOST_SLOTS_REFUSED active=%s limit=%s' % (active, limit), flush=True)
        return 10
    backend = None
    try:
        token = leases[0].token
        state = guard.inspect_idle_tui(owned_token=token)
        if state['status'] != 'IDLE':
            print(json.dumps(state), flush=True)
            return 10
        plan, rows = loop.inputs(ROOT)
        directory = receipt.begin(ROOT, state)
        backend = loop.InstalledBackend(ROOT, directory, token)
        result = loop.run_pass(ROOT / 'reports/ops/auto-coordinator', plan, rows,
                               backend, slots=min(7, limit - 1))
        receipt.exclusive(directory / 'LOOP.json', result)
        print(loop.summary(result), flush=True)  # existing native cron delivery only
        return 1 if result.get('uncertain') else 0
    finally:
        if backend:
            # No timeout/kill. A launched effect keeps both singleton and host
            # leases until its genuine terminal exit, including on exceptions.
            backend.finish()
        for lease in leases:
            lease.release()


if __name__ == '__main__':
    sys.exit(main())
