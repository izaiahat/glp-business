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
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import maintenance_admission as admission
import maintenance_cohort as cohort
import maintenance_window as window
import sync_from_repo as importer

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
DEPENDENCIES = (_primitives, admission, cohort, window, importer)
CODE = frozenset((PRODUCER, *(str(Path('scripts') / Path(m.__file__).name) for m in DEPENDENCIES)))
SOURCES = frozenset((SNAPSHOT, 'scripts/scheduled_source_drift.py',
                     'ops/scheduled-source-drift-manifest.json',
                     'ops/n8n_execution_budget.py', 'ops/n8n_dt.py'))
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
    paths = [Path(__file__), *(Path(m.__file__) for m in DEPENDENCIES)]
    for path in paths:
        name = 'scripts/' + path.name
        if (path.resolve().parent != Path(__file__).resolve().parent
                or git(repo, 'show', head+':'+name) != read_regular(path)[0]):
            raise RuntimeError('executing_producer_dependency_not_reviewed:' + name)
    registration = strict_json(git(repo, 'show', head+':ops/registered-producers.json'))
    for target in (str(HERMES/'scripts'/CARRIER), str(HERMES/'cron/jobs.json')):
        if not any(row['glob'] == target and PRODUCER in row['producers']
                   for row in registration['artifacts']):
            raise RuntimeError('producer_not_registered:' + target)
    source_hashes = review.get('source_sha256', {})
    if not isinstance(source_hashes, dict) or set(source_hashes) != SOURCES:
        raise RuntimeError('exact_source_set_required')
    source_modes = review.get('source_mode', {})
    if (not isinstance(source_modes, dict) or set(source_modes) != SOURCES
            or any(type(mode) is not int or not 0 <= mode <= 0o777
                   for mode in source_modes.values())):
        raise RuntimeError('exact_source_modes_required')
    observed = {review_path: review_preimage}
    for name in SOURCES:
        body = git(repo, 'show', review['source_head']+':'+name)
        if sha_bytes(body) != source_hashes[name]:
            raise RuntimeError('source_git_binding_mismatch:' + name)
        observed[business/name] = read_regular(business/name)
        if observed[business/name][0] != body:
            raise RuntimeError('installed_source_moved:' + name)
        if observed[business/name][1] != source_modes[name]:
            raise RuntimeError('installed_source_mode_moved:' + name)
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
    """Review outside exclusion; exact CAS inside a short, sealed cutover.

    No pause/resume: the native fire + registry fences close scheduled admission
    without changing any control/due field. DAC closes first reads of the sole
    mutable carrier, including direct runpy/callable imports, then census drains
    pre-seal readers. The five preinstalled inputs are never written here.
    """
    if not apply:
        return checked_plan(review_path, repo, business, hermes)[-1]
    with importer.source_lock(business):
        review, observed, staged, candidate, result = checked_plan(review_path, repo, business, hermes)
        if review['decision'] != 'ACCEPT_EXACT_CURRENT_BYTES':
            raise RuntimeError('apply_requires_independent_exact_review')
        if scheduler is None:
            os.environ['HERMES_HOME'] = str(hermes)
            sys.path.insert(0, str(hermes/'hermes-agent'))
            from cron import jobs as scheduler
        registry = hermes/'cron/jobs.json'
        carrier = hermes/'scripts'/CARRIER
        if (scheduler._current_cron_store().jobs_file != registry
                or scheduler._jobs_lock_file() != registry.parent/'.jobs.lock'):
            raise RuntimeError('wrong_scheduler_store_or_lock')
        with scheduler._fire_job_lock(JOB_ID) as fenced, window.native_registry_lock(scheduler):
            window.require(fenced, 'native_fire_fence_unavailable')
            assert_unchanged(observed)
            entry = {'kind': 'hermes', 'id': JOB_ID}

            def drain():
                # Native fire fence is reentrant on the owning thread. No ledger
                # mutation, no TTL-only discharge, no effects or forced drain.
                def job(_):
                    jobs = strict_json(read_regular(registry)[0])['jobs']
                    hits = [j for j in jobs if j.get('id') == JOB_ID]
                    window.require(len(hits) == 1, 'registration_identity_changed')
                    return hits[0]
                busy, owners = admission.native_runs(scheduler, [entry], job)
                busy += admission.process_readers([carrier], [carrier], owners, {os.getpid()})
                return busy

            window.require(not drain(), 'carrier_not_drained')
            custody = business/'reports/ops/pin-archive/scheduled-job-health'/result['review_sha256']
            journal = custody/'cutover.json'
            window.require(not journal.exists(), 'cutover_already_attempted_reconcile_no_replay')
            preserve_custody(custody, observed, staged)
            assert_unchanged(observed)
            window.atomic_json(journal, {'schema': window.SCHEMA, 'kind': 'f09-short-cutover-v1',
                'review_sha256': result['review_sha256'], 'job_id': JOB_ID})
            # All but the carrier stay readable, unchanged and importer-fenced.
            remaining = {p: value for p, value in observed.items() if p != carrier}

            def commit():
                assert_unchanged(remaining)
                window.require(not drain(), 'carrier_not_drained')
                scheduler._record_load_stamp(None)
                try:
                    scheduler._save_jobs_unlocked(copy.deepcopy(candidate))
                finally:
                    scheduler._record_load_stamp(None)
                actual = read_regular(registry)
                if strict_json(actual[0])['jobs'] != candidate:
                    raise RuntimeError('registration_readback_mismatch_no_retry')
                remaining[registry] = actual
                assert_unchanged(remaining)

            # Do NOT reopen between rename and native save/readback. On a saver
            # exception the durable commit intent and sealed inode survive; no
            # source-only rollback can certify a registry outcome.
            cohort.switch_files(journal, staged, {carrier: observed[carrier]}, drain, commit=commit)
            assert_unchanged({**remaining, carrier: (staged[carrier], observed[carrier][1])})
            result.update(applied=True, custody=str(custody), admission_restored=True,
                          lifecycle_mutations=0, cutover_journal=str(journal))
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
