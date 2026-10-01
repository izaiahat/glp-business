#!/usr/bin/env python3
"""K3 daily staging only. No network, scheduler, candidate writer or publisher.

A separately authorized capture producer may supply same-UTC-day CAPTURES.json
files. This runner never enables or invokes the engine's paid capture route.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path('/home/ubuntu/business')
HERE = Path(__file__).resolve().parent
STAGE = ROOT / 'reports/ops/orchestration-restore-20260907/PLAN/K3/cadence-candidate'
PRODUCTION_OUTPUT = ROOT / 'reports/glp-clinic-scout/evidence-refresh'
POINTER = ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
ENGINE = ROOT / 'tools/glp/evidence_refresh.py'
SCHEMA = ROOT / 'scripts/glp_current_content_base_schema.py'
RELEASES = ROOT / 'site-system/flagship-v2/releases'
PIN_FILE = HERE / 'SOURCE-PINS.json'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def put(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True) + '\n'
    if path.exists() and path.read_text() == payload:
        return
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(payload)
    os.replace(tmp, path)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_pins(pin_file=PIN_FILE):
    pins = read(pin_file)
    required = {str(ENGINE), str(ENGINE.with_name('evidence_refresh_verify.mjs')), str(SCHEMA)}
    if set(pins['sources']) != required:
        raise RuntimeError('SOURCE_PIN_SET_INVALID')
    for path, digest in pins['sources'].items():
        if not re.fullmatch('[0-9a-f]{64}', digest) or sha(path) != digest:
            raise RuntimeError('SOURCE_PIN_DRIFT:' + path)
    if sha(__file__) != pins['runner_sha256']:
        raise RuntimeError('RUNNER_PIN_DRIFT')
    return pins


def timestamp(value=None):
    now = datetime.fromisoformat(value.replace('Z', '+00:00')) if value else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return now.astimezone(timezone.utc)


def closure(rows):
    h = hashlib.sha256()
    for row in sorted(rows, key=lambda x: x['path']):
        h.update(f"{row['path']}\0{row['sha256']}\0{row['bytes']}\n".encode())
    return h.hexdigest()


def binding(pointer=POINTER, releases=RELEASES):
    before = sha(pointer)
    p = read(pointer)
    load(SCHEMA, 'k3_pointer_schema').validate_pointer_shape(p, releases_root=releases)
    evidence = {}
    for key in ('manifest', 'final_receipt'):
        spec = p[key]
        if sha(spec['path']) != spec['sha256']:
            raise RuntimeError('POINTER_EVIDENCE_DRIFT:' + key)
        evidence[key] = read(spec['path'])
    m, f = evidence['manifest'], evidence['final_receipt']
    if (m['release_root'] != p['release_root'] or m['release_tree_sha256'] != p['release_tree_sha256']
            or closure(m['release_rows']) != p['release_tree_sha256']
            or f['status'] != 'PASS_PRODUCTION_TERMINAL'
            or f['deployment']['deployment_id'] != p['deployment_id']
            or f['immutable_closure']['release_tree_sha256'] != p['release_tree_sha256']
            or f['immutable_closure']['public_tree_sha256'] != p['public_tree_sha256']
            or f['evidence_sha256'].get(Path(p['manifest']['path']).name) != p['manifest']['sha256']):
        raise RuntimeError('POINTER_CHAIN_MISMATCH')
    release = Path(p['release_root'])
    rows = m['release_rows']
    names = [r['path'] for r in rows]
    if len(names) != len(set(names)):
        raise RuntimeError('DUPLICATE_MANIFEST_ROW')
    required = {'data/program-registry.json', 'data/provider-pricing-matrix.json',
                'src/therapeutic-taxonomy.mjs', 'src/provider-dynamic-profiles.mjs'}
    if not required.issubset(names):
        raise RuntimeError('MISSING_RELEASE_INPUT')
    # Read all manifest-listed bytes, including validators and transitive imports.
    for row in rows:
        path = release / row['path']
        if Path(row['path']).is_absolute() or '..' in Path(row['path']).parts or not path.resolve().is_relative_to(release):
            raise RuntimeError('MANIFEST_PATH_ESCAPE')
        if path.stat().st_size != row['bytes'] or sha(path) != row['sha256']:
            raise RuntimeError('RELEASE_SOURCE_DRIFT:' + row['path'])
    if sha(pointer) != before:
        raise RuntimeError('POINTER_CHANGED_DURING_BINDING')
    return {'pointer_path': str(pointer), 'pointer_sha256': before, 'pointer': p,
            'manifest_rows_verified': len(rows),
            'target_preimages': {str(release / n): sha(release / n) for n in sorted(required)}}


def unchanged(bound):
    if sha(bound['pointer_path']) != bound['pointer_sha256']:
        raise RuntimeError('CURRENT_BASE_CHANGED_RESTAGE_NEXT_DAY')
    for path, digest in bound['target_preimages'].items():
        if sha(path) != digest:
            raise RuntimeError('TARGET_SOURCE_DRIFT:' + path)


def output_root(value):
    path = Path(value).absolute()
    if str(path.resolve()) != str(path):
        raise RuntimeError('OUTPUT_SYMLINK_OR_NONCANONICAL')
    if not (path == PRODUCTION_OUTPUT or path.is_relative_to(STAGE)):
        raise RuntimeError('OUTPUT_OUTSIDE_STAGING_REPORTS')
    # Reject existing descendant symlinks too: no writes through pre-planted paths.
    if path.exists() and any(p.is_symlink() for p in path.rglob('*')):
        raise RuntimeError('OUTPUT_SYMLINK')
    return path


@contextlib.contextmanager
def lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.daily.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('LOCKED') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def check_cache(index, folder):
    expected = index['artifacts']
    for rel, digest in expected.items():
        path = folder / rel
        if not path.resolve().is_relative_to(folder) or sha(path) != digest:
            raise RuntimeError('DAILY_ARTIFACT_DRIFT:' + rel)


def captures_for(er, source, pid, profile, pricing, day):
    if source is None:
        return {}, 'NOT_REQUESTED_NO_AUTHORIZED_CAPTURE_PRODUCER'
    path = Path(source) / pid / 'CAPTURES.json'
    if not path.exists():
        return {}, 'UNKNOWN_CAPTURE_MANIFEST_MISSING'
    caps = read(path)
    if len(caps) > 12:
        raise RuntimeError('CAPTURE_BUDGET_EXCEEDED')
    _, hosts = er.canonical_urls(profile, pricing)
    for cap in caps.values():
        if not er.safe_url(cap.get('source_url', ''), hosts):
            raise RuntimeError('UNSAFE_CAPTURE_SOURCE')
        if not er.safe_url(cap.get('final_url', cap['source_url']), hosts):
            raise RuntimeError('UNSAFE_CAPTURE_REDIRECT')
        if timestamp(cap['captured_at_utc']).date().isoformat() != day:
            raise RuntimeError('STALE_CAPTURE_NO_RESTAMP')
    er.load_captures(caps)
    return caps, 'IMPORTED_EXISTING_SAME_DAY_CAPTURE'


def stage_program(er, release, pid, folder, captures, day, review=None):
    reg = read(release / 'data/program-registry.json')
    profile = reg['provider_dynamic_profiles']['profiles'].get(pid)
    if not profile:
        return {'program_id': pid, 'status': 'UNRESOLVED_ALIAS_OR_PROFILE', 'capture_outcome': 'NOT_REQUESTED'}
    pricing = read(release / 'data/provider-pricing-matrix.json')['providers'].get(pid, {})
    caps, outcome = captures_for(er, captures, pid, profile, pricing, day)
    put(folder / 'CAPTURES.json', caps)
    command = [sys.executable, '-B', str(ENGINE), '--program', pid, '--candidate', str(release), '--out', str(folder), '--dry-run']
    if review:
        decision = read(review)
        if decision.get('captures') != caps:
            raise RuntimeError('REVIEW_CAPTURE_BINDING_MISMATCH')
        command += ['--adjudication', str(review)]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=90,
                             env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    except subprocess.TimeoutExpired:
        # Never leave a prior ready patch exposed after an ambiguous partial run.
        put(folder / 'DATA-PATCH.json', {'targets': [], 'applied_to_candidate': False, 'status': 'BLOCKED_TIMEOUT'})
        put(folder / 'PACKET.json', {'program_id': pid, 'status': 'BLOCKED_TIMEOUT'})
        return {'program_id': pid, 'status': 'BLOCKED_TIMEOUT', 'capture_outcome': outcome}
    if run.returncode:
        put(folder / 'DATA-PATCH.json', {'targets': [], 'applied_to_candidate': False, 'status': 'BLOCKED_ENGINE'})
        put(folder / 'PACKET.json', {'program_id': pid, 'status': 'BLOCKED_ENGINE'})
        return {'program_id': pid, 'status': 'BLOCKED_ENGINE', 'returncode': run.returncode, 'capture_outcome': outcome}
    packet = read(folder / 'PACKET.json')
    return {'program_id': pid, 'status': packet['status'], 'capture_outcome': outcome,
            'packet_path': str(folder / 'PACKET.json'), 'capture_count': len(caps),
            'missing_decisions': packet.get('missing_decisions', []),
            'unresolved_fact_count': sum(f['status'] == 'NEEDS_ADJUDICATION' for f in packet.get('mechanical_facts', [])) if packet['status'] == 'NEEDS_ADJUDICATION' else None}


def run_daily(out_root, as_of=None, capture_root=None, selector_only=False):
    pins = check_pins()
    root = output_root(out_root)
    if as_of is not None and root == PRODUCTION_OUTPUT:
        raise RuntimeError('AS_OF_OVERRIDE_OFFLINE_ONLY')
    now = timestamp(as_of)
    day = now.date().isoformat()  # UTC date namespace; scheduler may run in PT.
    with lock(root):
        bound = binding()
        folder = root / day
        index_path = folder / 'RUN.json'
        if index_path.exists():
            prior = read(index_path)
            if prior['binding'] != bound:
                raise RuntimeError('SAME_DAY_BASE_CHANGED_REQUIRES_RECONCILIATION')
            if prior['source_pins'] != pins or prior['selector_only'] != selector_only or prior['capture_root'] != capture_root:
                raise RuntimeError('SAME_DAY_INPUT_MODE_CHANGED')
            check_cache(prior, folder)
            return {**prior, 'invocation': 'IDEMPOTENT_REUSE'}
        if folder.exists():
            raise RuntimeError('INCOMPLETE_DAILY_RUN_REQUIRES_RECONCILIATION')
        folder.mkdir()
        put(folder / 'BINDING.json', bound)
        er = load(ENGINE, 'k3_evidence_refresh')
        release = Path(bound['pointer']['release_root'])
        selected = er.select_stale(read(release / 'data/program-registry.json')['therapeutic_taxonomy'], now.isoformat(), 7)
        put(folder / 'SELECTED.json', selected)
        rows = []
        deadline = time.monotonic() + 900
        for row in selected:
            pid = row['program_id']
            if time.monotonic() + 90 > deadline:
                rows.append({'program_id': pid, 'status': 'DEFERRED_DAILY_TIME_BUDGET', 'capture_outcome': 'NOT_REQUESTED'})
                continue
            if not re.fullmatch('[a-z0-9][a-z0-9-]*', pid) or 'fella' in pid:
                rows.append({'program_id': pid, 'status': 'EXCLUDED_UNSAFE_IDENTITY'})
                continue
            if selector_only:
                rows.append({'program_id': pid, 'status': 'SELECTED_ONLY', 'capture_outcome': 'NOT_REQUESTED'})
                continue
            unchanged(bound)
            try:
                rows.append(stage_program(er, release, pid, folder / pid, capture_root, day))
            except (ValueError, KeyError, OSError, RuntimeError) as exc:
                rows.append({'program_id': pid, 'status': 'BLOCKED_INPUT', 'reason': str(exc)})
        unchanged(bound)
        put(folder / 'HANDOFF.json', {'schema': 'k3-evidence-adjudication-handoff-v1',
            'model': 'gpt-6-astra', 'reasoning_effort': 'low', 'dispatch_status': 'STAGED_NOT_DISPATCHED',
            'binding': bound, 'programs': rows, 'review_prompt': str(HERE / 'ASTRA-LOW-PROMPT.md'),
            'candidate_writer_status': 'BLOCKED_NO_GENERIC_CURRENT_BASE_WRITER_CONTRACT',
            'combined_patch': None, 'applied': False, 'release_accepted': False})
        artifacts = {str(p.relative_to(folder)): sha(p) for p in sorted(folder.rglob('*')) if p.is_file()}
        result = {'schema': 'k3-daily-evidence-stage-v1', 'status': 'STAGED_PENDING_ADJUDICATION' if selected else 'NO_STALE_IDENTITIES',
                  'as_of_utc': now.isoformat(), 'day_timezone': 'UTC', 'stale_within_days': 7,
                  'binding': bound, 'source_pins': pins, 'selector_only': selector_only,
                  'capture_root': capture_root, 'selected_count': len(selected), 'programs': rows,
                  'artifacts': artifacts, 'network_requests': 0, 'candidate_writes': 0,
                  'scheduler_receipt': 'NOT_A_NATURAL_RUN', 'invocation': 'NEW_STAGE'}
        put(index_path, result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-root', default=str(PRODUCTION_OUTPUT))
    parser.add_argument('--as-of', help='Offline witness only: explicit timezone required')
    parser.add_argument('--capture-root', help='Read existing authorized same-day manifests; never fetch')
    parser.add_argument('--selector-only', action='store_true')
    args = parser.parse_args()
    try:
        result = run_daily(args.out_root, args.as_of, args.capture_root, args.selector_only)
        print(json.dumps({k: result[k] for k in ('status', 'selected_count', 'as_of_utc', 'invocation', 'network_requests')}))
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        print(json.dumps({'status': 'BLOCKED', 'reason': str(exc)}))
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
