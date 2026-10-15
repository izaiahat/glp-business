#!/usr/bin/env python3
"""Fast cron trigger. No heavy work executes in the gateway's cron job."""
import json
import subprocess
import time

UNIT='host-disk-cleanup.service'


def trigger():
    started=time.monotonic()
    status=subprocess.run(['systemctl','--user','is-active',UNIT],capture_output=True,text=True,timeout=5)
    active=status.stdout.strip() in {'active','activating','reloading'}
    if not active:
        result=subprocess.run(['systemctl','--user','start','--no-block',UNIT],capture_output=True,text=True,timeout=5)
        if result.returncode:
            raise RuntimeError('systemd cleanup launch failed; inspect the unit journal')
    return {'unit':UNIT,'launched':not active,'single_instance':True,
            'runtime_seconds':round(time.monotonic()-started,3)}


if __name__=='__main__':
    result=trigger()
    verb='launched' if result['launched'] else 'already running; skipped duplicate launch'
    print(f"⚙️ **Host Disk** — {verb} **{UNIT}** · detached full backlog pass · completion receipt and reclaimed GiB will be posted by the unit")
