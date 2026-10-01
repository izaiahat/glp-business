#!/usr/bin/env python3
"""Exact-review-gated F09 carrier adoption; no job execution or fleet repinning.

Preview is read-only. Apply changes one carrier and one existing registration hash.
See ops/scheduled-job-health-carrier-release.md for separate owner admission.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import re

# Load shared, already-curated transaction primitives from THIS checkout only.
_HELPER_PATH = Path(__file__).resolve().with_name('maxbounty_pin_release.py')
_spec = importlib.util.spec_from_file_location('_f09_release_primitives', _HELPER_PATH)
assert _spec is not None and _spec.loader is not None
_primitives = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_primitives)
sha_bytes = _primitives.sha_bytes
strict_json = _primitives.strict_json
json_bytes = _primitives.json_bytes
read_regular = _primitives.read_regular
atomic_write = _primitives.atomic_write
git = _primitives.git
assert_unchanged = _primitives.assert_unchanged
preserve_custody = _primitives.preserve_custody

BUSINESS = Path('/home/ubuntu/business')
HERMES = Path('/home/ubuntu/.hermes')
JOB_ID = 'b86ec8937c20'
CARRIER = 'scheduled_job_health_manager.py'
SNAPSHOT = 'ops/hermes-cron-scripts/' + CARRIER
PRODUCER = 'scripts/scheduled_job_health_carrier_release.py'
CODE = frozenset((PRODUCER, 'scripts/maxbounty_pin_release.py'))
SOURCES = frozenset((SNAPSHOT, 'scripts/scheduled_source_drift.py',
                     'ops/scheduled-source-drift-manifest.json'))
# Exact installed predecessor also preserved in main's ancestry before PR #103.
PREDECESSOR_SHA256 = 'd652c1d7bd3b6968af117d36ebbdd39ce119aa6c9b0fcadb0f0fd79191370dc9'


def checked_plan(review_path, repo, business, hermes):
    review_preimage = read_regular(review_path)
    review = strict_json(review_preimage[0])
    if (review.get('schema') != 'scheduled-job-health-carrier-review-v1'
            or review.get('decision') not in {'REVIEW_ONLY', 'ACCEPT_EXACT_CURRENT_BYTES'}
            or not str(review.get('reviewer') or '').strip()
            or not str(review.get('author') or '').strip()
            or review['reviewer'] == review['author']
            or not isinstance(review.get('evidence'), list) or not review['evidence']
            or any(not isinstance(s, str) or not s.strip() for s in review['evidence'])
            or not str(review.get('writer_exclusion') or '').strip()):
        raise RuntimeError('independent_exact_review_contract')
    when = datetime.strptime(review['reviewed_at_utc'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    if when > datetime.now(timezone.utc):
        raise RuntimeError('review_timestamp_in_future')
    for key in ('producer_head', 'source_head'):
        if not re.fullmatch('[0-9a-f]{40}', review.get(key, '')):
            raise RuntimeError('exact_git_head_required')
    head = review['producer_head']
    if git(repo, 'rev-parse', 'HEAD').decode().strip() != head:
        raise RuntimeError('producer_head_moved')
    if git(repo, 'status', '--porcelain', '--untracked-files=no').strip():
        raise RuntimeError('dirty_producer_checkout')
    git(repo, 'merge-base', '--is-ancestor', review['source_head'], head)
    for name, path in ((PRODUCER, Path(__file__)), ('scripts/maxbounty_pin_release.py', _HELPER_PATH)):
        if git(repo, 'show', head+':'+name) != read_regular(path)[0]:
            raise RuntimeError('executing_producer_dependency_not_reviewed:' + name)
    registration = strict_json(git(repo, 'show', head+':ops/registered-producers.json'))
    for target in (str(HERMES/'scripts'/CARRIER), str(HERMES/'cron/jobs.json')):
        if not any(row['glob'] == target and PRODUCER in row['producers']
                   for row in registration['artifacts']):
            raise RuntimeError('producer_not_registered:' + target)
    source_hashes = review.get('source_sha256', {})
    if set(source_hashes) != SOURCES:
        raise RuntimeError('exact_source_set_required')
    observed = {review_path: review_preimage}
    for name in SOURCES:
        body = git(repo, 'show', review['source_head']+':'+name)
        if sha_bytes(body) != source_hashes[name]:
            raise RuntimeError('source_git_binding_mismatch:' + name)
        observed[business/name] = read_regular(business/name)
        if observed[business/name][0] != body:
            raise RuntimeError('installed_source_moved:' + name)
    carrier = hermes/'scripts'/CARRIER
    observed[carrier] = read_regular(carrier)
    if (sha_bytes(observed[carrier][0]) != PREDECESSOR_SHA256
            or review.get('predecessor_sha256') != PREDECESSOR_SHA256
            or observed[carrier][1] != review.get('predecessor_mode')):
        raise RuntimeError('unknown_carrier_preimage_or_mode')
    body = git(repo, 'show', review['source_head']+':'+SNAPSHOT)
    proposed = sha_bytes(body)
    if proposed == PREDECESSOR_SHA256:
        raise RuntimeError('successor_carrier_required')
    if review['decision'] == 'ACCEPT_EXACT_CURRENT_BYTES' and review.get('proposed_sha256') != proposed:
        raise RuntimeError('emitted_carrier_not_reviewed')
    registry = hermes/'cron/jobs.json'
    observed[registry] = read_regular(registry)
    jobs = strict_json(observed[registry][0])['jobs']
    hits = [j for j in jobs if j.get('id') == JOB_ID]
    if len(hits) != 1 or hits[0] != review.get('expected_job'):
        raise RuntimeError('registration_full_record_cas')
    job = hits[0]
    schedule = job.get('schedule', {})
    if (job.get('script') not in {CARRIER, str(carrier)}
            or job.get('script_sha256') != PREDECESSOR_SHA256
            or job.get('enabled') is not True or job.get('no_agent') is not True
            or job.get('state') != 'scheduled'
            or not isinstance(schedule, dict) or schedule.get('kind') != 'interval'
            or schedule.get('minutes') != 15
            or job.get('run_claim') or job.get('fire_claim') or job.get('running')):
        raise RuntimeError('registration_identity_schedule_or_claim')
    candidate = copy.deepcopy(jobs)
    next(j for j in candidate if j.get('id') == JOB_ID)['script_sha256'] = proposed
    result = {'schema':'scheduled-job-health-carrier-release-v1', 'applied':False,
              'producer_head':head, 'source_head':review['source_head'],
              'review_sha256':sha_bytes(review_preimage[0]), 'job_id':JOB_ID,
              'predecessor_sha256':PREDECESSOR_SHA256, 'proposed_sha256':proposed,
              'registration_delta':['script_sha256'], 'jobs_executed':0}
    return review, observed, {carrier:body}, candidate, result


def release(review_path, repo, *, business=BUSINESS, hermes=HERMES, apply=False, scheduler=None):
    """Scheduler lock is not exclusion of direct callers or source writers."""
    review, observed, staged, candidate, result = checked_plan(review_path, repo, business, hermes)
    if not apply:
        return result
    if review['decision'] != 'ACCEPT_EXACT_CURRENT_BYTES':
        raise RuntimeError('apply_requires_independent_exact_review')
    if scheduler is None:
        import sys
        sys.path.insert(0, str(hermes/'hermes-agent'))
        from cron import jobs as scheduler
    registry = hermes/'cron/jobs.json'
    if (scheduler._current_cron_store().jobs_file != registry
            or scheduler._jobs_lock_file() != registry.parent/'.jobs.lock'):
        raise RuntimeError('wrong_scheduler_store_or_lock')
    import fcntl
    import os
    with scheduler._jobs_file_lock:
        fd = os.open(scheduler._jobs_lock_file(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert_unchanged(observed)
            custody = business/'reports/ops/pin-archive/scheduled-job-health'/result['review_sha256']
            preserve_custody(custody, observed, staged)
            assert_unchanged(observed)
            current = dict(observed)
            try:
                for path, body in staged.items():
                    assert_unchanged(current)
                    atomic_write(path, body, observed[path][1])
                    current[path] = (body, observed[path][1])
                assert_unchanged(current)
                scheduler._record_load_stamp(None)
                try:
                    scheduler._save_jobs_unlocked(candidate)
                finally:
                    scheduler._record_load_stamp(None)
                if strict_json(read_regular(registry)[0])['jobs'] != candidate:
                    raise RuntimeError('registration_readback_mismatch_no_retry')
                current[registry] = read_regular(registry)
                assert_unchanged(current)
            except Exception:
                # Include post-rename failures. Never replay/revert an uncertain saver.
                if read_regular(registry) == observed[registry]:
                    for path in staged:
                        actual = read_regular(path)
                        if actual == observed[path]:
                            continue
                        if actual != (staged[path], observed[path][1]):
                            raise RuntimeError('rollback_conflict_no_overwrite')
                        atomic_write(path, *observed[path])
                raise
            result.update(applied=True, custody=str(custody))
            atomic_write(custody/'acceptance.json', json_bytes(result), 0o600)
            return result


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--review-receipt', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        result = release(args.review_receipt, args.repo, apply=args.apply)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status':'REFUSED', 'reason':str(exc)}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
