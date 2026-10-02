"""Real shell -> pinned job -> pinned reporter -> collector-construction boundary.

Portable fixture relocates absolute roots and rehashes only the relocated manifest
and synthetic roster. Identity bytes/pin and every executable stay unchanged.
No credentials, transport, report, ledger or scheduler effects are permitted.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIVE = '/home/ubuntu/business'
WRAPPER = 'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting.sh'
IDENTITY = 'ops/glp/reporting-v2/identity-registry.json'
MANIFEST = 'ops/glp/reporting-v2/source-manifest.json'
OLD_PIN = '93aa4fa83cba6ee24c81504fe0e58133c87014cb7bb618e1ff9140ab66b21271'


def sha(body):
    return hashlib.sha256(body).hexdigest()


def put(root, rel, body):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


GUARD = '''import os, sys
class ForbiddenEffect(BaseException): pass
def audit(event, args):
    if event == 'open':
        path, mode, flags = args
        if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
            raise ForbiddenEffect('write:' + str(path))
        if isinstance(path, (str, bytes)) and any(s in str(path).lower() for s in ('credentials', '/secrets/', '.env')) and not str(path).endswith(('.py', '.pyc')):
            raise ForbiddenEffect('credential:' + str(path))
    if event.startswith(('socket.', 'subprocess.', 'sqlite3.')) or event in ('os.system', 'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', 'os.putenv', 'os.exec', 'os.fork'):
        raise ForbiddenEffect(event)
def trace(frame, event, arg):
    if event == 'call' and frame.f_code.co_name == 'weekly_collectors' and frame.f_code.co_filename.endswith('/scripts/affiliate_network_access.py'):
        assert frame.f_locals['names'] == ['cj']
        print('F07_REGISTERED_COLLECTOR_BOUNDARY_NO_EFFECTS', flush=True)
        raise SystemExit(73)
    return trace
sys.addaudithook(audit)
sys.settrace(trace)
'''


def entry_fixture(tmp_path, wrapper=None):
    root = tmp_path / 'business'
    pins_body = (ROOT / MANIFEST).read_bytes()
    pins = json.loads(pins_body)
    relocated = {}
    for absolute, expected in pins['files'].items():
        rel = str(Path(absolute).relative_to(LIVE))
        body = (ROOT / rel).read_bytes()
        assert sha(body) == expected, rel
        relocated[str(put(root, rel, body))] = expected
    put(root, IDENTITY, (ROOT / IDENTITY).read_bytes())
    pins['files'] = relocated
    new_manifest = json.dumps(pins).encode()
    put(root, MANIFEST, new_manifest)
    text = wrapper or (ROOT / WRAPPER).read_text()
    roster_match = re.search(r'^check_source (reports/\S+) ([0-9a-f]{64})$', text, re.M)
    assert roster_match is not None
    roster_rel, roster_pin = roster_match.groups()
    roster = b'{"marketplaces":[{"marketplace":"cj"}]}'
    put(root, roster_rel, roster)
    # Only environment-dependent fixture adaptations; never change identity pin.
    text = text.replace(LIVE, str(root)).replace('/home/ubuntu/.hermes/hermes-agent/venv/bin/python', sys.executable)
    text = text.replace(sha(pins_body), sha(new_manifest)).replace(roster_pin, sha(roster))
    script = put(root, WRAPPER, text.encode())
    guard = tmp_path / 'guard'
    put(guard, 'sitecustomize.py', GUARD.encode())
    return root, script, guard


def run_entry(tmp_path, wrapper=None):
    root, script, guard = entry_fixture(tmp_path, wrapper)
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}
    env = {**os.environ, 'PYTHONPATH': str(guard), 'PYTHONDONTWRITEBYTECODE': '1', 'HOME': str(tmp_path)}
    result = subprocess.run(['/bin/bash', str(script)], env=env, capture_output=True, text=True, timeout=20)
    after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}
    assert before == after
    assert not (root / 'reports/glp-clinic-scout').exists()
    return result


def test_registered_entry_reaches_collector_boundary_without_effects(tmp_path):
    result = run_entry(tmp_path)
    assert result.returncode == 73, (result.stdout, result.stderr)
    assert result.stdout.strip() == 'F07_REGISTERED_COLLECTOR_BOUNDARY_NO_EFFECTS'
    assert result.stderr == ''


def test_predecessor_identity_pin_refuses_before_python_entry(tmp_path):
    wrapper = (ROOT / WRAPPER).read_text()
    wrapper = re.sub(r'(?m)^(check_source ' + re.escape(IDENTITY) + r' )[0-9a-f]{64}$', r'\g<1>' + OLD_PIN, wrapper)
    result = run_entry(tmp_path, wrapper)
    assert result.returncode == 66
    assert result.stdout == ''
    assert result.stderr.strip() == 'K3_REPORTING_SOURCE_DRIFT'


def test_all_committed_wrapper_source_pins_match_checkout():
    rows = re.findall(r'^check_source (\S+) ([0-9a-f]{64})$', (ROOT / WRAPPER).read_text(), re.M)
    assert len(rows) == 4
    for rel, expected in rows:
        if rel.startswith('reports/'):
            continue  # Private retained roster, independently checked at receiving review.
        assert sha((ROOT / rel).read_bytes()) == expected, rel
