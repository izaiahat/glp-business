#!/usr/bin/env python3
"""Admit the hourly coordinator in the shared host pool."""
import json
import os
from pathlib import Path
import subprocess
import sys
import auto_coordinator_guard as guard

ROOT = Path('/home/ubuntu/business')

def main():
    state = guard.inspect()
    if state['status'] != 'IDLE':
        print(json.dumps(state), flush=True)
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
    try:
        token = leases[0].token
        state = guard.inspect(owned_token=token)
        if state['status'] != 'IDLE':
            print(json.dumps(state), flush=True)
            return 10
        env = dict(os.environ, LINEAR_ROLE='orchestrator',
                   GLP_AUTO_COORDINATOR_MODE='plan-dispatch-close-only',
                   GLP_AUTO_COORDINATOR_LEASE_TOKEN=token,
                   GLP_AUTO_COORDINATOR_LEASE_PID=str(os.getpid()))
        command = ['/home/ubuntu/.hermes/hermes-agent/venv/bin/hermes', 'chat', '--oneshot',
                   '--query-file', str(ROOT / 'ops/linear/AUTO-COORDINATOR.md'),
                   '--provider', 'openai-codex', '-m', 'gpt-6.1-sol',
                   '--reasoning', 'high', '--yolo']
        print('HOST_ADMITTED coordinator_slots=1 active=%s limit=%s' % (active, limit), flush=True)
        return subprocess.run(command, cwd=ROOT, env=env, timeout=2400).returncode
    finally:
        for lease in leases:
            lease.release()

if __name__ == '__main__':
    sys.exit(main())
