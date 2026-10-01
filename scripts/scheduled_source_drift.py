#!/usr/bin/env python3
"""Read-only scheduled-source census; hashes only, no job imports or effects.

The reviewed inventory binds host-cron launchers and explicit transitive pins.
Native registries are rediscovered (including paused jobs). Added/changed bindings,
missing inputs and untracked sources are UNKNOWN, never a fabricated clean result.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BUSINESS = Path('/home/ubuntu/business')
HERMES = Path('/home/ubuntu/.hermes')


def daily_due(state, now):
    local = now.astimezone(ZoneInfo('America/Los_Angeles'))
    return local.hour >= 8 and state.get('last_source_drift_day') != local.date().isoformat()


def audit(root=BUSINESS, hermes=HERMES, *, crontab_ids=None, inventory_path=None):
    findings = []
    checks = set()
    counts = Counter(script_drift=0, scheduler_drift=0, payload_drift=0, unknown=0)

    def unknown(job, reason, path: str | Path = ''):
        counts['unknown'] += 1
        findings.append({'job_id':job, 'kind':'unknown', 'reason':reason, 'path':str(path)})

    def read_json(path):
        return json.loads(path.read_text())

    def digest(path):
        # Do not follow aliases into credentials, or inspect protected payloads.
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('symlink_refused')
        if any(p in {'secrets', 'credentials'} for p in path.parts):
            raise ValueError('protected_path_refused')
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def check(job, kind, path, expected, owner=''):
        key = (job, kind, str(path), expected, owner)
        if key in checks:
            return
        checks.add(key)
        if not isinstance(expected, str) or len(expected)!=64 or any(c not in '0123456789abcdef' for c in expected):
            unknown(job, 'invalid_or_absent_pin', path)
            return
        try:
            actual = digest(path)
        except (OSError, ValueError):
            unknown(job, 'missing_unreadable_or_aliased', path)
            return
        if actual != expected:
            counts[kind] += 1
            findings.append({'job_id':job, 'kind':kind, 'path':str(path), 'owner':owner,
                             'expected_sha256':expected, 'actual_sha256':actual})

    try:
        document = read_json(inventory_path or root/'ops/scheduled-source-drift-manifest.json')
        if document['schema'] != 'scheduled-source-drift-v1' or not isinstance(document['jobs'], list):
            raise ValueError('inventory_schema')
        inventory = {j['id']:j for j in document['jobs']}
        if len(inventory) != len(document['jobs']):
            raise ValueError('duplicate_job_identity')
        source_files = read_json(root/'ops/rebuild-source-manifest.json')['files']
        state_path = root/'ops/.rebuild-sync-state.json'
        imported = read_json(state_path).get('hashes', {}) if state_path.exists() else {}
    except (OSError, ValueError, KeyError, TypeError):
        unknown('inventory', 'inventory_or_source_manifest_invalid')
        inventory, source_files, imported = {}, {}, {}

    def source_check(job, path, spec=None):
        spec = spec or {}
        rel = spec.get('source')
        if not rel:
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = 'ops/hermes-cron-scripts/'+path.name
        expected = imported.get(rel) or spec.get('reviewed_sha256') or source_files.get(rel, {}).get('sha256')
        check(job, 'script_drift', path, expected, rel)

    def inventory_payloads(job, spec, path):
        pins = spec.get('payloads') if spec else None
        # Only an explicit reviewed list (including []) establishes coverage.
        if not isinstance(pins, list):
            unknown(job, 'transitive_pin_inventory_missing', path)
            return []
        return pins

    def payload_check(job, pin, depth=0, seen=None):
        path = Path(pin['path']); owner = pin.get('owner', '')
        if not path.is_absolute():
            path = root/path
        check(job, 'payload_drift', path, pin.get('sha256'), owner)
        # A pinned JSON manifest is a transitive owner as well as a pinned target.
        seen = set() if seen is None else seen
        if path in seen or depth >= 8:
            return
        seen.add(path)
        if path.suffix != '.json' or not any(x in path.name.lower() for x in ('pin', 'manifest')):
            return
        try:
            digest(path)  # same no-alias/protected-path policy before decoding
            value = read_json(path)
            files = value.get('files')
            if not isinstance(files, dict) or not files:
                unknown(job, 'transitive_manifest_coverage_missing', path)
                return
            for target, spec in files.items():
                if isinstance(spec, str):
                    child = {'path':target, 'sha256':spec, 'owner':str(path)}
                elif isinstance(spec, dict) and 'sha256' in spec:
                    child = {'path':spec.get('path', target), 'sha256':spec['sha256'], 'owner':str(path)}
                else:
                    unknown(job, 'unsupported_manifest_pin', path)
                    continue
                payload_check(job, child, depth+1, seen)
        except (OSError, ValueError, TypeError, AttributeError):
            unknown(job, 'transitive_manifest_unreadable', path)

    native_ids = set()
    for registry in [hermes/'cron/jobs.json', *sorted(hermes.glob('profiles/*/cron/jobs.json'))]:
        try:
            value = read_json(registry)
            jobs = value['jobs'] if isinstance(value, dict) else value
            if not isinstance(jobs, list):
                raise ValueError('registry_schema')
            for job in jobs:
                if not job.get('script'):
                    continue  # agent-only jobs have no installed script
                jid = job['id']
                if jid in native_ids:
                    unknown(jid, 'ambiguous_profile_identity', registry)
                    continue
                native_ids.add(jid)
                raw = Path(job['script'])
                path = raw if raw.is_absolute() else registry.parent.parent/'scripts'/raw
                spec = inventory.get(jid)
                if spec and str(path)!=spec['path']:
                    unknown(jid, 'installed_binding_changed', path)
                    continue
                source_check(jid, path, spec)
                if job.get('script_sha256'):
                    check(jid, 'scheduler_drift', path, job['script_sha256'], str(registry))
                for pin in inventory_payloads(jid, spec, path):
                    payload_check(jid, pin)
        except (OSError, ValueError, KeyError, TypeError):
            unknown('native_registry', 'registry_invalid', registry)

    known_cron = {jid for jid,spec in inventory.items() if spec['surface']=='crontab'}
    if crontab_ids is None:
        unknown('crontab', 'live_crontab_census_unavailable')
        crontab_ids=[]
    for jid in set(crontab_ids)-known_cron:
        unknown(jid, 'new_or_changed_host_cron_binding')
    for jid in set(crontab_ids) & known_cron:
        spec=inventory[jid]
        for source in spec['sources']:
            source_check(jid, Path(source['path']), source)
        for pin in inventory_payloads(jid, spec, spec['path']):
            payload_check(jid, pin)
    return {'schema':'scheduled-source-drift-result-v1', 'status':'UNKNOWN' if counts['unknown'] else ('DRIFT' if findings else 'CLEAN'),
            'counts':dict(counts), 'checked':len(checks), 'native_script_jobs':len(native_ids),
            'host_cron_jobs':len(set(crontab_ids)), 'findings':findings,
            'coverage':'explicit reviewed transitive inventory; new bindings fail closed; not a runtime effect or resident-code audit'}


def live_audit(root=BUSINESS, hermes=HERMES):
    import subprocess
    run = subprocess.run(['crontab','-l'], capture_output=True, text=True, timeout=10)
    ids = None
    if run.returncode == 0:
        ids = ['crontab:'+hashlib.sha256(line.strip().encode()).hexdigest()[:16]
               for line in run.stdout.splitlines() if line.strip() and not line.lstrip().startswith('#')
               and (line.split()[0].startswith('@') or '=' not in line.split()[0])]
    return audit(root, hermes, crontab_ids=ids)


def render_summary(result):
    c=result['counts']
    return ('**Daily installed-source drift** — '+result['status']+'\n'
            f"source: **{c['script_drift']}** · scheduler pins: **{c['scheduler_drift']}** · payload pins: **{c['payload_drift']}** · unknown: **{c['unknown']}**\n"
            '→ Reviewed-source/pin repair required; no automatic install, rerun or repin.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=BUSINESS)
    parser.add_argument('--hermes',type=Path,default=HERMES)
    args=parser.parse_args()
    result=live_audit(args.root,args.hermes)
    print(json.dumps(result,indent=2))
    return 2 if result['status']=='UNKNOWN' else (1 if result['status']=='DRIFT' else 0)

if __name__=='__main__':
    raise SystemExit(main())
