#!/usr/bin/env python3
"""Guard /home/ubuntu/business/STATE.md so it remains a lean session-start state.

STATE.md is continuity, not a run log. This script validates size/shape, writes via
snapshot + atomic replace, and records every accepted write in a ledger.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path('/home/ubuntu/business')
POLICY_PATH = ROOT / 'state' / 'state-policy.yaml'
DEFAULT_POLICY: dict[str, Any] = {
    'max_bytes_warn': 14000,
    'max_bytes_fail': 18000,
    'max_line_chars_warn': 650,
    'max_line_chars_fail': 1000,
    'max_lines_warn': 220,
    'required_headings': [
        'Mission dashboard',
        'Active lanes',
        'Immediate next actions',
        'Waiting on Izaiah',
        'Active blockers / risks',
        'Locked decisions / standing rules',
        'Pointers',
        'Archive index',
    ],
    'forbidden_patterns': [],
    'archive_dir': str(ROOT / 'state' / 'archives'),
    'snapshot_dir': str(ROOT / 'state' / 'snapshots'),
    'ledger_path': str(ROOT / 'state' / 'state-write-ledger.jsonl'),
    'state_path': str(ROOT / 'STATE.md'),
}
SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*([A-Za-z0-9_\-]{16,})'),
    re.compile(r'sk-[A-Za-z0-9]{20,}'),
    re.compile(r'ghp_[A-Za-z0-9]{20,}'),
]


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_policy() -> dict[str, Any]:
    policy = dict(DEFAULT_POLICY)
    if POLICY_PATH.exists():
        try:
            import yaml  # type: ignore
            loaded = yaml.safe_load(POLICY_PATH.read_text()) or {}
            if isinstance(loaded, dict):
                policy.update(loaded)
        except Exception:
            # Minimal fallback for the keys we care about if PyYAML is missing/broken.
            for line in POLICY_PATH.read_text().splitlines():
                if ':' not in line or line.lstrip().startswith('-') or line.lstrip().startswith('#'):
                    continue
                k, v = line.split(':', 1)
                k = k.strip(); v = v.strip()
                if not k:
                    continue
                if v.isdigit():
                    policy[k] = int(v)
                elif v:
                    policy[k] = v
    return policy


def split_headings(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if line.startswith('## '):
            out.append(line[3:].strip())
    return out


def validate_text(text: str, policy: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    raw = text.encode('utf-8')
    if len(raw) > int(policy['max_bytes_fail']):
        errors.append(f'STATE too large: {len(raw)} bytes > fail cap {policy["max_bytes_fail"]}')
    elif len(raw) > int(policy['max_bytes_warn']):
        warnings.append(f'STATE near cap: {len(raw)} bytes > warn cap {policy["max_bytes_warn"]}')
    lines = text.splitlines()
    if len(lines) > int(policy.get('max_lines_warn', 220)):
        warnings.append(f'STATE has {len(lines)} lines > warn cap {policy.get("max_lines_warn")}')
    for i, line in enumerate(lines, 1):
        if len(line) > int(policy['max_line_chars_fail']):
            errors.append(f'line {i} too long: {len(line)} chars > fail cap {policy["max_line_chars_fail"]}')
        elif len(line) > int(policy['max_line_chars_warn']):
            warnings.append(f'line {i} long: {len(line)} chars > warn cap {policy["max_line_chars_warn"]}')
    headings = split_headings(text)
    for h in policy.get('required_headings', []):
        if h not in headings:
            errors.append(f'missing required heading: ## {h}')
    for pat in policy.get('forbidden_patterns', []) or []:
        try:
            rx = re.compile(pat, re.MULTILINE)
        except re.error as e:
            warnings.append(f'bad forbidden pattern {pat!r}: {e}')
            continue
        if rx.search(text):
            errors.append(f'forbidden run-log/detail pattern present: {pat}')
    for rx in SECRET_PATTERNS:
        m = rx.search(text)
        if m:
            errors.append(f'possible secret-like material in STATE near: {m.group(0)[:80]}')
    return errors, warnings


def validate_file(path: Path, policy: dict[str, Any], json_out: bool = False) -> int:
    text = path.read_text(errors='replace')
    errors, warnings = validate_text(text, policy)
    payload = {
        'status': 'fail' if errors else 'pass',
        'path': str(path),
        'bytes': len(text.encode('utf-8')),
        'lines': len(text.splitlines()),
        'sha256': sha256_bytes(text.encode('utf-8')),
        'warnings': warnings,
        'errors': errors,
        'timestamp_utc': now_utc(),
    }
    if json_out:
        print(json.dumps(payload, indent=2))
    else:
        print(f"STATE_GUARD status={payload['status']} bytes={payload['bytes']} lines={payload['lines']} sha256={payload['sha256']}")
        for w in warnings:
            print(f'WARN: {w}')
        for e in errors:
            print(f'ERROR: {e}')
    return 1 if errors else 0


def ensure_dirs(policy: dict[str, Any]) -> None:
    Path(policy['snapshot_dir']).mkdir(parents=True, exist_ok=True)
    Path(policy['archive_dir']).mkdir(parents=True, exist_ok=True)
    Path(policy['ledger_path']).parent.mkdir(parents=True, exist_ok=True)


def snapshot_current(state_path: Path, policy: dict[str, Any], label: str) -> Path | None:
    if not state_path.exists():
        return None
    snap = Path(policy['snapshot_dir']) / f'STATE-{stamp()}-{label}.md'
    shutil.copy2(state_path, snap)
    return snap


def append_ledger(policy: dict[str, Any], row: dict[str, Any]) -> None:
    ledger = Path(policy['ledger_path'])
    with ledger.open('a') as f:
        f.write(json.dumps(row, sort_keys=True) + '\n')


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except FileNotFoundError:
            pass


def write_state(input_path: Path, policy: dict[str, Any], actor: str, operation: str) -> int:
    ensure_dirs(policy)
    state_path = Path(policy['state_path'])
    new_text = input_path.read_text(errors='replace')
    errors, warnings = validate_text(new_text, policy)
    if errors:
        print(json.dumps({'status': 'fail', 'errors': errors, 'warnings': warnings}, indent=2))
        return 1
    old_bytes = state_path.read_bytes() if state_path.exists() else b''
    new_bytes = new_text.encode('utf-8')
    snap = snapshot_current(state_path, policy, operation)
    atomic_write(state_path, new_bytes)
    row = {
        'timestamp_utc': now_utc(),
        'actor': actor,
        'operation': operation,
        'previous_sha256': sha256_bytes(old_bytes) if old_bytes else None,
        'new_sha256': sha256_bytes(new_bytes),
        'previous_bytes': len(old_bytes),
        'new_bytes': len(new_bytes),
        'snapshot': str(snap) if snap else None,
        'archives_written': [],
        'validator_status': 'pass',
        'warnings': warnings,
    }
    append_ledger(policy, row)
    print(json.dumps({'status': 'pass', 'new_bytes': len(new_bytes), 'new_sha256': row['new_sha256'], 'snapshot': row['snapshot'], 'warnings': warnings}, indent=2))
    return 0


def suggest_prune(policy: dict[str, Any]) -> int:
    state_path = Path(policy['state_path'])
    text = state_path.read_text(errors='replace')
    outdir = ROOT / 'reports' / 'state-hygiene'
    outdir.mkdir(parents=True, exist_ok=True)
    ts = stamp()
    errors, warnings = validate_text(text, policy)
    headings = split_headings(text)
    suggestion = {
        'timestamp_utc': now_utc(),
        'state_path': str(state_path),
        'bytes': len(text.encode('utf-8')),
        'lines': len(text.splitlines()),
        'sha256': sha256_bytes(text.encode('utf-8')),
        'status': 'fail' if errors else 'pass',
        'warnings': warnings,
        'errors': errors,
        'headings': headings,
        'recommendation': 'Archive completed chronology to /home/ubuntu/business/state/archives and rewrite STATE.md as lean current-state dashboard via state_guard.py write.',
    }
    json_path = outdir / f'state-prune-suggestion-{ts}.json'
    md_path = outdir / f'state-prune-suggestion-{ts}.md'
    json_path.write_text(json.dumps(suggestion, indent=2) + '\n')
    md_path.write_text(
        f"# STATE prune suggestion — {ts}\n\n"
        f"- Bytes: `{suggestion['bytes']}`\n- Lines: `{suggestion['lines']}`\n- SHA256: `{suggestion['sha256']}`\n- Status: `{suggestion['status']}`\n\n"
        "## Errors\n" + ''.join(f"- {e}\n" for e in errors or ['none']) + "\n"
        "## Warnings\n" + ''.join(f"- {w}\n" for w in warnings or ['none']) + "\n"
        "## Headings\n" + ''.join(f"- {h}\n" for h in headings) + "\n"
        "## Recommended action\nArchive completed chronology to `/home/ubuntu/business/state/archives/` and write a lean dashboard using `state_guard.py write --kael-confirm --input <file>`.\n"
    )
    print(json.dumps({'status': 'written', 'json': str(json_path), 'markdown': str(md_path)}, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    v = sub.add_parser('validate')
    v.add_argument('--json', action='store_true')
    s = sub.add_parser('suggest-prune')
    w = sub.add_parser('write')
    w.add_argument('--input', required=True)
    w.add_argument('--kael-confirm', action='store_true')
    w.add_argument('--actor', default='kael-parent')
    p = sub.add_parser('snapshot')
    p.add_argument('--label', default='manual')
    args = ap.parse_args()
    policy = load_policy()
    state_path = Path(policy['state_path'])
    if args.cmd == 'validate':
        return validate_file(state_path, policy, json_out=args.json)
    if args.cmd == 'suggest-prune':
        return suggest_prune(policy)
    if args.cmd == 'write':
        if not args.kael_confirm:
            print('Refusing write without --kael-confirm', file=sys.stderr)
            return 2
        return write_state(Path(args.input), policy, actor=args.actor, operation='write')
    if args.cmd == 'snapshot':
        ensure_dirs(policy)
        snap = snapshot_current(state_path, policy, args.label)
        print(snap)
        return 0
    return 2

if __name__ == '__main__':
    raise SystemExit(main())
