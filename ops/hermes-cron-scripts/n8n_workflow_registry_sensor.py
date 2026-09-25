#!/usr/bin/env python3
"""K4 daily registry inventory carrier; no model and no external sends."""
import fcntl
import hashlib
from pathlib import Path
import subprocess
import sys
ROOT=Path('/home/ubuntu/business')
PINS={'scripts/n8n_workflow_registry_sensor.py':'68c93f2f18f515ee44b60b1bac560b786242217259537b0822b4985d49470578','ops/n8n-workflow-registry.json':'743a52b0f8bb679399c2e61b33d046d1d7d6c348f85fc397bbe492a79aa21bd1'}
for name,expected in PINS.items():
 if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=expected:
  print('BLOCKED_REGISTRY_SENSOR_PIN_MISMATCH');raise SystemExit(2)
with Path('/home/ubuntu/.hermes/state/n8n-workflow-registry-sensor.lock').open('a+') as lock:
 try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:print('BLOCKED_SENSOR_ALREADY_RUNNING');raise SystemExit(2)
 p=subprocess.run([sys.executable,str(ROOT/'scripts/n8n_workflow_registry_sensor.py')],cwd=ROOT,timeout=240,check=False)
 raise SystemExit(p.returncode)
