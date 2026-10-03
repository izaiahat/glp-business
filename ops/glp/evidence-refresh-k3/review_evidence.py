#!/usr/bin/env python3
"""Confined regeneration of reviewed packets, never a combined/live writer."""
import argparse
import importlib.util
from pathlib import Path
import json
import sys

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('daily_review', HERE / 'daily_evidence_refresh.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def review(day_dir, decisions, out):
    d.check_pins()
    day_dir, decisions, out = Path(day_dir).resolve(), Path(decisions).resolve(), d.output_root(out)
    if out.exists():
        raise RuntimeError('REVIEW_OUTPUT_EXISTS_RECONCILE_NO_REPLAY')
    if out == day_dir or day_dir in out.parents or out in day_dir.parents:
        raise RuntimeError('REVIEW_MUST_NOT_OVERLAP_DAILY_INPUT')
    run = d.read(day_dir / 'RUN.json')
    d.check_cache(run, day_dir)
    bound = d.binding()
    if run['binding'] != bound:
        raise RuntimeError('REVIEW_CURRENT_BASE_DRIFT')
    er = d.load(d.ENGINE, 'review_engine')
    release = Path(bound['pointer']['release_root'])
    rows = []
    with d.lock(out):
        selected_rows = d.read(day_dir / 'SELECTED.json')
        profiles = d.read(release / 'data/program-registry.json')['provider_dynamic_profiles']['profiles']
        selected_rows.sort(key=lambda r: (r['program_id'] not in profiles, r['program_id']))
        for selected in selected_rows:
            pid = selected['program_id']
            if not d.re.fullmatch('[a-z0-9][a-z0-9-]*', pid) or 'fella' in pid:
                rows.append({'program_id': pid, 'status': 'EXCLUDED_UNSAFE_IDENTITY'})
                continue
            if pid not in profiles:
                rows.append(d.stage_program(er, release, pid, out / pid, str(day_dir), run['as_of_utc'][:10]))
                continue
            decision = decisions / pid / 'ADJUDICATION.json'
            if not decision.is_file():
                rows.append({'program_id': pid, 'status': 'UNRESOLVED_MISSING_ADJUDICATION'})
                continue
            decision_sha = d.sha(decision)
            try:
                row = d.stage_program(er, release, pid, out / pid, str(day_dir), run['as_of_utc'][:10], decision)
            except (RuntimeError, ValueError, KeyError, OSError) as exc:
                row = {'program_id': pid, 'status': 'BLOCKED_REVIEW', 'reason': str(exc)}
            if d.sha(decision) != decision_sha:
                raise RuntimeError('ADJUDICATION_CHANGED_DURING_REVIEW')
            row.update(adjudication_path=str(decision), adjudication_sha256=decision_sha)
            rows.append(row)
        d.unchanged(bound)
        composer = d.load(HERE / 'compose_current_base.py', 'k3_composer')
        composition = composer.compose(d, er, bound, rows, out)
        handoff = {'schema': 'k3-reviewed-writer-handoff-v1', 'status': composition['status'],
                   'binding': bound, 'daily_run_path': str(day_dir / 'RUN.json'), 'daily_run_sha256': d.sha(day_dir / 'RUN.json'),
                   'programs': rows, 'ready_program_ids': [r['program_id'] for r in rows if r['status'] == 'PACKET_READY'],
                   'combined_patch': composition['combined_patch'], 'composition': composition,
                   'candidate_applied': False, 'independent_acceptance': 'NOT_REQUESTED',
                   'required_next_artifact': 'Resolve capture/fact/whole-base validation gaps, then independent review and governed adoption of the bound combined intake; no automatic candidate write',
                   'serial_shared_preimage_apply': 'FORBIDDEN',
                   'artifacts': {str(p.relative_to(out)): d.sha(p) for p in sorted(out.rglob('*')) if p.is_file() and p.name != '.daily.lock'}}
        d.put(out / 'WRITER-HANDOFF.json', handoff)
        return handoff


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--day-dir', required=True)
    p.add_argument('--decisions', required=True)
    p.add_argument('--out', required=True)
    a = p.parse_args()
    try:
        result = review(a.day_dir, a.decisions, a.out)
        print(json.dumps({'status': result['status'], 'ready_program_ids': result['ready_program_ids']}))
        return 0
    except (RuntimeError, ValueError, KeyError, OSError) as exc:
        print(json.dumps({'status': 'BLOCKED', 'reason': str(exc)}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
