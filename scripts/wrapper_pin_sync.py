#!/usr/bin/env python3
"""wrapper_pin_sync.py — keep the reconciliation wrapper's integrity pins in step with the files they cover.

THE DEFECT THIS CLOSES (2026-09-11/12, three deploy attempts lost to it)
The wrapper `~/.hermes/scripts/glp_affiliate_reconciliation_honesty.sh` refuses to run when any file it pins by
sha256 has changed. That gate is correct and stays. But every time a lane legitimately REPAIRS one of those files
— the builder under GLP-170/174/179, the activation script under GLP-181 — the next deploy refuses at preflight
with `*_hash_drift`, publishes nothing, and someone has to notice and re-pin by hand. Three deploy attempts died
that way in one night, each costing a full orchestrator round trip.

WHAT THIS DOES AND WHAT IT REFUSES TO DO
It re-pins a covered path to the sha of the file ON DISK, and nothing else. It does not weaken, remove or add a
pin, never edits the wrapper's logic, and writes a receipt naming every path it moved and both shas. The gate
still fires for any file this tool was not asked to sync, so an unexpected change is still caught. Run it with
`--paths` (the exact files an issue changed) after that issue PASSes; `--all` is a deliberate, receipted sweep.

PIN FORMS IT UNDERSTANDS (all three the wrapper uses)
  * `expected_payload_sha256='<sha>'`         — the payload script
  * `wire_<name>_expected_sha256='<sha>'`     — paired with `wire_<name>='<path>'`
  * `declare -A b2_pins=( ['<path>']='<sha>' … )`
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

WRAPPER = Path(os.environ.get('GLP_RECONCILIATION_WRAPPER')
               or Path.home() / '.hermes/scripts/glp_affiliate_reconciliation_honesty.sh')
RECEIPTS = Path(os.environ.get('GLP_PIN_SYNC_RECEIPTS')
                or Path(__file__).resolve().parents[1] / 'reports/ops/wrapper-pin-sync')
PAYLOAD_RE = re.compile(r"^payload='([^']+)'", re.M)
PAYLOAD_PIN_RE = re.compile(r"^expected_payload_sha256=['\"]([a-f0-9]{64})['\"]", re.M)
WIRE_PATH_RE = re.compile(r"^(wire_[a-z_]+)=['\"]([^'\"]+)['\"]", re.M)
WIRE_PIN_RE = re.compile(r"^(wire_[a-z_]+)_expected_sha256=['\"]([a-f0-9]{64})['\"]", re.M)
B2_ROW_RE = re.compile(r"^\s*\['([^']+)'\]='([a-f0-9]{64})'", re.M)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def covered(text: str) -> list[tuple[str, str, int, int]]:
    """All (absolute path, hash, hash-start, hash-end) enforcement occurrences.

    Scalar and associative co-pins can cover the same path. Path-keyed maps
    silently discard one gate; exact spans preserve every occurrence instead.
    """
    out = []
    payload = PAYLOAD_RE.search(text)
    if payload:
        for pin in PAYLOAD_PIN_RE.finditer(text):
            out.append((payload.group(1), pin.group(1), *pin.span(1)))
    paths = {name: value for name, value in WIRE_PATH_RE.findall(text)}
    for pin in WIRE_PIN_RE.finditer(text):
        if pin.group(1) in paths:
            out.append((paths[pin.group(1)], pin.group(2), *pin.span(2)))
    for pin in B2_ROW_RE.finditer(text):
        out.append((pin.group(1), pin.group(2), *pin.span(2)))
    return sorted(out, key=lambda row: row[2])


def sync(paths=None, all_drifted=False, wrapper=WRAPPER, apply=True):
    text = wrapper.read_text()
    pins = covered(text)
    wanted = None
    if paths:
        wanted = {str(Path(p).resolve()) for p in paths}
    moved, skipped, missing = [], [], []
    for path, pinned, start, end in reversed(pins):
        target = Path(path)
        if wanted is not None and str(target.resolve() if target.exists() else target) not in wanted:
            continue
        if not target.is_file():
            missing.append(path)
            continue
        actual = sha256_of(target)
        if actual == pinned:
            skipped.append(path)
            continue
        if wanted is None and not all_drifted:
            continue
        text = text[:start] + actual + text[end:]
        moved.append({'path': path, 'from': pinned, 'to': actual,
                      'hash_offset': start})
    receipt = {'schema': 'glp.wrapper_pin_sync.v1', 'wrapper': str(wrapper),
               'at_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
               'repinned': moved, 'already_current': skipped, 'covered_but_absent': missing,
               'wrapper_sha256_before': sha256_of(wrapper)}
    if moved and apply:
        shutil.copy2(wrapper, wrapper.with_suffix('.sh.bak-pinsync'))
        tmp = wrapper.with_suffix('.sh.tmp')
        tmp.write_text(text)
        tmp.chmod(wrapper.stat().st_mode)
        os.replace(tmp, wrapper)
        receipt['wrapper_sha256_after'] = sha256_of(wrapper)
        RECEIPTS.mkdir(parents=True, exist_ok=True)
        (RECEIPTS / ('pin-sync-%s.json' % receipt['at_utc'].replace(':', ''))).write_text(
            json.dumps(receipt, indent=1))
    return receipt


CRON_JOBS = Path(os.environ.get('HERMES_CRON_JOBS') or Path.home() / '.hermes/cron/jobs.json')


def cron_pin_state(wrapper=WRAPPER, jobs=CRON_JOBS):
    """The OUTER layer of the same pin problem: the cron pins the WRAPPER by sha.

    wrapper_pin_sync keeps the wrapper's pins of its payloads in step. Nothing kept the cron's pin of the
    wrapper in step, so an authorized wrapper re-pin left the scheduled run pointing at a hash that no longer
    exists and the 07:15 execution would refuse (GLP-198, 2026-09-12). Report it; repairing a cron entry is the
    cron owner's job, not this tool's.
    """
    try:
        data = json.loads(Path(jobs).read_text())
    except (OSError, ValueError) as error:
        return {'error': str(error)[:120]}
    rows = data if isinstance(data, list) else (data.get('jobs') or [])
    name = Path(wrapper).name
    actual = sha256_of(Path(wrapper))
    out = []
    for job in rows:
        if not isinstance(job, dict) or str(job.get('script') or '') != name:
            continue
        pinned = str(job.get('script_sha256') or '')
        out.append({'job': job.get('id'), 'script': name, 'pinned': pinned, 'actual': actual,
                    'matches': pinned == actual})
    return {'jobs': out, 'drifted': [j for j in out if not j['matches']]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--paths', nargs='*', help='the exact files an issue changed')
    ap.add_argument('--all', action='store_true', help='re-pin every drifted covered path (receipted sweep)')
    ap.add_argument('--check', action='store_true', help='report drift, change nothing')
    ap.add_argument('--check-cron', action='store_true',
                    help="report whether the CRON's pin of the wrapper still matches it")
    a = ap.parse_args(argv)
    if a.check_cron:
        state = cron_pin_state()
        print(json.dumps(state, indent=1))
        return 1 if state.get('drifted') or state.get('error') else 0
    if a.check:
        text = WRAPPER.read_text()
        drift = [{'path': p, 'pinned': s, 'actual': sha256_of(Path(p))}
                 for p, s, _start, _end in covered(text)
                 if Path(p).is_file() and sha256_of(Path(p)) != s]
        print(json.dumps({'drifted': drift, 'count': len(drift)}, indent=1))
        return 1 if drift else 0
    if not a.paths and not a.all:
        ap.error('give --paths, --all or --check')
    print(json.dumps(sync(paths=a.paths, all_drifted=a.all), indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
