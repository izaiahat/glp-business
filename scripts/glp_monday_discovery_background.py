#!/usr/bin/env python3
"""Detach the registered Monday discovery owner from short tool-call lifetimes.

360s discovery + 120s admission and pre-admission reads can exceed a 420s
terminal call. This launcher returns after dispatch, NOT after completion.
The existing native scheduler retains locks, pins, watchdog and execution ledger.
Never rerun an uncertain request: inspect DISPATCH.json, native execution history
and exact durable business targets first. A request directory is create-exclusive.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

JOB_ID = '58202b06bdc0'
ROOT = Path('/home/ubuntu/business')


def save_new(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    Path(path).chmod(0o600)


def launch(output):
    executable = shutil.which('hermes')
    if not executable:
        raise RuntimeError('native_hermes_cli_unavailable')
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    intent = {'job_id': JOB_ID, 'status': 'INTENT_NO_REPLAY',
              'requested_at': datetime.now(timezone.utc).isoformat(),
              'command': [executable, 'cron', 'run', JOB_ID]}
    save_new(output / 'INTENT.json', intent)
    # A real CLI process, not an orphanable daemon thread in a dying caller.
    # Do not add communicate/wait or a second shorter subprocess watchdog.
    with (output / 'native-cli.log').open('xb') as log:
        process = subprocess.Popen(intent['command'], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
    receipt = {**intent, 'status': 'DISPATCHED_NOT_COMPLETED', 'pid': process.pid,
               'log': str(output / 'native-cli.log'),
               'completion_source': 'native cron executions.db and business artifacts'}
    save_new(output / 'DISPATCH.json', receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True,
                        help='new private request directory; existing requests refuse')
    args = parser.parse_args(argv)
    print(json.dumps(launch(args.output)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
