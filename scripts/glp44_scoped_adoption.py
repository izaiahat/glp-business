#!/usr/bin/env python3
"""GLP-44 reviewed source/wrapper adoption; no job execution or schedule change.

Apply needs an independently accepted exact-preimage receipt and external
source-writer/direct-reader exclusion. The scheduler lock alone is not that
exclusion. A preview is read-only and is not an apply authorization.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import sync_from_repo as syncer
from maxbounty_pin_release import atomic_write, read_regular, preserve_custody

APPROVED = '1c459c7aff4a17063e803ba610627663d2025254'
PATHS = (
    'ops/glp/analytics-brain-control.json',
    'ops/glp/reporting-v2/source-manifest.json',
    'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting.sh',
    'systems/analytics-brain/analytics_brain.py',
    'systems/analytics-brain/collector.py',
)
SNAPSHOT = 'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting.sh'
WRAPPER = 'glp_affiliate_weekly_reporting.sh'
JOB = 'aadf6677c7f0'


def digest(body):
    return hashlib.sha256(body).hexdigest()


def observed(path):
    return read_regular(path) if path.exists() else (None, None)


def job_record(registry):
    document = json.loads(registry.read_bytes())
    hits = [j for j in document['jobs'] if j.get('id') == JOB]
    if len(hits) != 1:
        raise RuntimeError('reporting_job_count_changed')
    return hits[0], document['jobs']


def plan(checkout, root, hermes, review=None):
    checkout = checkout.resolve(strict=True)
    root = root.resolve(strict=True)
    hermes = hermes.resolve(strict=True)
    subprocess.run(['git', '-C', str(checkout), 'merge-base', '--is-ancestor', APPROVED, 'HEAD'], check=True)
    for rel in PATHS:
        committed = subprocess.check_output(['git', '-C', str(checkout), 'show', f'{APPROVED}:{rel}'])
        if (checkout / rel).read_bytes() != committed:
            raise RuntimeError('approved_source_moved:' + rel)
    adopted = tuple(p for p in PATHS if p not in json.loads((root / syncer.STATE).read_text()).get('hashes', {}))
    # An absent destination does not need an adoption flag; reviewed_prior_hashes
    # is only for preexisting, untracked-baseline files.
    adopted = tuple(p for p in adopted if (root / p).exists())
    if syncer.sync(root, checkout, False, adopted, PATHS) != 0:
        raise RuntimeError('scoped_source_preflight_refused')
    before = {p: observed(root / p) for p in PATHS}
    state = observed(root / syncer.STATE)
    snapshot = (checkout / SNAPSHOT).read_bytes()
    wrapper = hermes / 'scripts' / WRAPPER
    wrapper_before = read_regular(wrapper)
    if wrapper_before[0] != before[SNAPSHOT][0]:
        raise RuntimeError('installed_wrapper_not_curated_predecessor')
    registry = hermes / 'cron' / 'jobs.json'
    job, _ = job_record(registry)
    if (job.get('enabled') is not True or job.get('state') != 'scheduled'
            or job.get('script') not in (WRAPPER, str(wrapper))
            or job.get('script_sha256') != digest(wrapper_before[0])
            or job.get('fire_claim') or job.get('run_claim') or job.get('running')):
        raise RuntimeError('reporting_job_not_idle_or_preimage_moved')
    pins = json.loads((checkout / 'ops/glp/reporting-v2/source-manifest.json').read_text())['files']
    for absolute, expected in pins.items():
        path = Path(absolute)
        try:
            rel = path.relative_to(syncer.DEFAULT_ROOT)
        except ValueError:
            raise RuntimeError('unexpected_reporting_pin_path:' + absolute)
        if rel.as_posix() == 'systems/analytics-brain/analytics_brain.py':
            actual = (checkout / rel).read_bytes()
        else:
            actual = (root / rel).read_bytes()  # host-only leaves must already match
        if digest(actual) != expected:
            raise RuntimeError('reporting_pin_closure_refused:' + str(path))
    proposal = {'source_sha256': {p: digest((checkout / p).read_bytes()) for p in PATHS},
                'wrapper_sha256': digest(snapshot), 'job_id': JOB,
                'job_before': job, 'source_before_sha256': {
                    p: digest(value[0]) if value[0] is not None else None for p, value in before.items()},
                'wrapper_before_sha256': digest(wrapper_before[0]),
                'state_before_sha256': digest(state[0]) if state[0] is not None else None}
    if review is not None:
        for key in proposal:
            if review.get(key) != proposal[key]:
                raise RuntimeError('reviewed_preimage_or_candidate_moved:' + key)
        if (review.get('schema') != 'glp44-scoped-adoption-review-v1'
                or review.get('approved_source') != APPROVED
                or review.get('decision') != 'ACCEPT_EXACT_BYTES'
                or not review.get('reviewer') or not review.get('writer_and_reader_exclusion')):
            raise RuntimeError('independent_review_required')
    return proposal, adopted, before, state, wrapper_before, registry


def adopt(checkout, root, hermes, review, *, scheduler):
    """Caller must hold external source-writer AND direct-reader exclusion."""
    proposal, adopted, before, state, wrapper_before, registry = plan(checkout, root, hermes, review)
    if scheduler._current_cron_store().jobs_file != registry or scheduler._jobs_lock_file() != registry.parent / '.jobs.lock':
        raise RuntimeError('wrong_scheduler_store')
    wrapper = hermes / 'scripts' / WRAPPER
    original = {root / p: value for p, value in before.items()}
    original[root / syncer.STATE] = state
    original[wrapper] = wrapper_before
    with scheduler._jobs_file_lock:
        fd = os.open(scheduler._jobs_lock_file(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Revalidate every source, baseline, wrapper and full job under the lock.
            fresh, _, _, _, _, _ = plan(checkout, root, hermes, review)
            if fresh != proposal:
                raise RuntimeError('preimage_moved_under_lock')
            registry_before = registry.read_bytes()
            if any(body is None for body, _ in original.values()):
                raise RuntimeError('missing_predecessor_requires_separate_custody')
            stage = {root / p: (checkout / p).read_bytes() for p in PATHS}
            state_data = json.loads(state[0])
            state_data['hashes'].update(proposal['source_sha256'])
            stage[root / syncer.STATE] = (json.dumps(state_data, indent=2, sort_keys=True) + '\n').encode()
            stage[wrapper] = (checkout / SNAPSHOT).read_bytes()
            custody = root / 'reports/ops/pin-archive/glp44' / digest(json.dumps(review, sort_keys=True).encode())
            preserve_custody(custody, {**original, registry: read_regular(registry)}, stage)
            # Custody is not an exclusion lock; reject late edits before first source write.
            if any(observed(path) != value for path, value in original.items()) or registry.read_bytes() != registry_before:
                raise RuntimeError('preimage_moved_after_custody')
            source_written = False
            try:
                source_written = True  # sync may partially write before raising
                if syncer.sync(root, checkout, True, adopted, PATHS) != 0:
                    raise RuntimeError('scoped_source_apply_refused')
                if observed(wrapper) != wrapper_before:
                    raise RuntimeError('wrapper_moved_before_replace')
                atomic_write(wrapper, (checkout / SNAPSHOT).read_bytes(), wrapper_before[1])
                current, jobs = job_record(registry)
                if current != proposal['job_before']:
                    raise RuntimeError('job_moved_before_hash_update')
                next_jobs = copy.deepcopy(jobs)
                next(j for j in next_jobs if j['id'] == JOB)['script_sha256'] = proposal['wrapper_sha256']
                scheduler._record_load_stamp(None)
                scheduler._save_jobs_unlocked(next_jobs)
                actual, actual_jobs = job_record(registry)
                if actual_jobs != next_jobs or actual['script_sha256'] != proposal['wrapper_sha256']:
                    raise RuntimeError('scheduler_readback_mismatch_no_retry')
            except Exception:
                # A possibly landed scheduler write is held for reconciliation, never
                # blindly reversed or replayed. Only restore files while registry is unchanged.
                current, _ = job_record(registry)
                if registry.read_bytes() == registry_before and source_written:
                    for path, (body, mode) in original.items():
                        now = observed(path)
                        if now == (body, mode):
                            continue
                        if now != (stage[path], mode):
                            raise RuntimeError('rollback_conflict_no_overwrite:' + str(path))
                        atomic_write(path, body, mode)
                raise
    return {'applied': True, 'source': APPROVED, 'proposal': proposal, 'custody': str(custody)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=syncer.DEFAULT_ROOT)
    parser.add_argument('--hermes', type=Path, default=Path('/home/ubuntu/.hermes'))
    parser.add_argument('--review', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        review = json.loads(args.review.read_text()) if args.review else None
        if args.apply:
            if review is None:
                raise RuntimeError('independent_review_required')
            sys.path.insert(0, str(args.hermes / 'hermes-agent'))
            from cron import jobs as scheduler
            result = adopt(args.checkout, args.root, args.hermes, review, scheduler=scheduler)
        else:
            result = {'applied': False, 'proposal': plan(args.checkout, args.root, args.hermes, review)[0]}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print('GLP44_ADOPTION_REFUSED: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
