#!/usr/bin/env python3
"""Fixed PR161 monitor -> A4 JSON map -> live wrapper -> native pin adoption.

No fleet repinning, job execution, schedule change or repair of pre-existing
weekly drift. Apply is a reviewed coherent switch in a parent-owned maintained
window; old importer/direct-reader exclusion remains the install owner's duty.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

GENERATION = 'glp345-a4-pr161-20261002'
SOURCE = '148a3091f1de752970422fef84ea6eb7f2ca5200'
BUSINESS = Path('/home/ubuntu/business')
HERMES = Path('/home/ubuntu/.hermes')
MONITOR = 'scripts/glp_ads_three_platform_monitor.py'
MAP = 'reports/ops/orchestration-restore-20260907/PLAN/A4/SOURCE-PINS.json'
# Literal scope declaration for the maintained static inventory.
PATHS = (
    'scripts/glp_ads_three_platform_monitor.py',
    'reports/ops/orchestration-restore-20260907/PLAN/A4/SOURCE-PINS.json',
)
WRAPPER = 'glp_weekly_ads_analytics.py'
JOB = 'ea5bad730e53'
MAP_SNAPSHOT = 'ops/glp345-a4/SOURCE-PINS.predecessor.json'
WRAPPER_SNAPSHOT = 'ops/glp345-a4/wrapper.predecessor.py'
BEFORE = 'bf6dfdb0f93a69a0ddacfc49840650e4e4608e31511218a1948007ed0fa43a4e'
AFTER = '7bb04b77bb476ca50e34acf446ba5c2641c2b7fe1401836f46f250aa66eee180'
MAP_BEFORE = '62bfd7a9fffdae836ace33f6148e3225aa5ebe1be6853e4c0e59d717a402c5dc'
WRAPPER_BEFORE = 'b39204ba33af406227ef430087a84f4217c9d1b2a97924ce68b42724a38d9871'

# Explicit continuation only: PR115's reviewed report reader after PR165's
# installed monitor closure. No dependency/module rollback or arbitrary repin.
WEEKLY_GENERATION = 'glp341-weekly-pr115-after-pr165-20261002'
WEEKLY_SOURCE = 'b951f2abb6d2c258269e3b8012ec3cfbaacb05ee'
WEEKLY_PREDECESSOR_SOURCE = 'efe3067b460427b42cc7b796cdfe84009c3abf33'
WEEKLY = 'reports/ops/orchestration-restore-20260907/PLAN/A4/weekly_ads_loop.py'
WEEKLY_PATHS = (
    'reports/ops/orchestration-restore-20260907/PLAN/A4/weekly_ads_loop.py',
    'reports/ops/orchestration-restore-20260907/PLAN/A4/SOURCE-PINS.json',
)
WEEKLY_SNAPSHOT = 'ops/glp345-a4/weekly.predecessor.py'
WEEKLY_BEFORE = 'd51b9ea03eddfd72641a4cd79e04d6b76e114e80c645ba6f866a7ca29705d1d1'
WEEKLY_AFTER = '05bda1aa7f23fc2dca94d35bbcf65276b8bd1e743a9c0e4eb21445867f306653'
WEEKLY_MAP_BEFORE = '9f74f31d94a93bf89c7f2f1405f907095a527262761a5c26f51fdf323e69ef00'
WEEKLY_WRAPPER_BEFORE = '3a702ed55ab6e619d7f133463c039c8e954a9e5e762ea40b0349bcc621b72f8d'
RAILS = 'ops/glp/seo_data_rails.py'
RAILS_BEFORE = 'aa0be353d3bd08d592a99193117d76d69ea80f828ce0d459d6844d1600506fc4'
RAILS_AFTER = 'e82495acdba978c9211339f81b2169c144e90fb6808f00f186d801e0d70fc333'
A3 = 'reports/ops/orchestration-restore-20260907/PLAN/A3/RESULT.json'
A3_HASH = '257339ab71e99ebbf845b56b6e1414d2baef8ec9ee7eb014eaf555f5cff8ce94'


def sha(body):
    return hashlib.sha256(body).hexdigest()


def wrapper_pins(body):
    tree = ast.parse(body)
    hits = [n.value for n in tree.body if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'PINS' for t in n.targets)]
    if len(hits) != 1 or not isinstance(hits[0], ast.Dict):
        raise RuntimeError('wrapper_pin_shape')
    pins = ast.literal_eval(hits[0])
    if len(pins) != len(hits[0].keys):
        raise RuntimeError('duplicate_wrapper_pin')
    return pins


def stage_pins(before, wrapper):
    # Authenticate complete predecessors, not a caller-provided hash allowlist.
    if sha(before) != MAP_BEFORE or sha(wrapper) != WRAPPER_BEFORE:
        raise RuntimeError('unknown_a4_artifact_preimage')
    if before.count(BEFORE.encode()) != 1 or wrapper.count(MAP_BEFORE.encode()) != 1:
        raise RuntimeError('exact_pin_site_count')
    emitted = before.replace(BEFORE.encode(), AFTER.encode(), 1)
    carrier = wrapper.replace(MAP_BEFORE.encode(), sha(emitted).encode(), 1)
    return emitted, carrier


def stage_weekly_pins(before, wrapper):
    """Replace only obsolete consumed edges, preserving actual PR165 wrapper."""
    if sha(before) != WEEKLY_MAP_BEFORE or sha(wrapper) != WEEKLY_WRAPPER_BEFORE:
        raise RuntimeError('unknown_weekly_artifact_preimage')
    if before.count(RAILS_BEFORE.encode()) != 1:
        raise RuntimeError('exact_posthog_pin_site_count')
    emitted = before.replace(RAILS_BEFORE.encode(), RAILS_AFTER.encode(), 1)
    pins = wrapper_pins(wrapper)
    pins[str(BUSINESS / WEEKLY)] = WEEKLY_AFTER
    pins[str(BUSINESS / MAP)] = sha(emitted)
    # PR115 removes today's closeout/authoring-brief validator from collect();
    # replace those unused edges with the actual frozen historical observation.
    del pins[str(BUSINESS / 'scripts/result_contract.py')]
    del pins[str(BUSINESS / 'reports/ops/orchestration-restore-20260907/briefs/plan/A3-CAMPAIGNS-THREE-PLATFORMS.md')]
    pins[str(BUSINESS / A3)] = A3_HASH
    node = next(n for n in ast.parse(wrapper).body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'PINS' for t in n.targets))
    rows = wrapper.splitlines(keepends=True)
    carrier = b''.join(rows[:node.lineno-1] + [('PINS = ' + repr(pins) + '\n').encode()] + rows[node.end_lineno:])
    return emitted, carrier


def authenticate(checkout, generation=GENERATION):
    """Bind executing producer/helpers, snapshots and reviewed source bytes."""
    import maxbounty_pin_release as p
    import maintenance_inventory as inventory
    import maintenance_window as mw
    import maintenance_cohort as cohort
    import maintenance_admission as admission
    import sync_from_repo as syncer
    checkout = checkout.resolve(strict=True)
    head = p.git(checkout, 'rev-parse', 'HEAD').decode().strip()
    if p.git(checkout, 'status', '--porcelain', '--untracked-files=no').strip():
        raise RuntimeError('dirty_producer_checkout')
    p.git(checkout, 'merge-base', '--is-ancestor', SOURCE, head)
    manifest = p.strict_json(p.git(checkout, 'show', head + ':' + syncer.MANIFEST))
    paths = [Path(__file__), *(Path(m.__file__) for m in (p, inventory, mw, cohort, admission, syncer))]
    for path in paths:
        rel = 'scripts/' + path.name
        body = p.git(checkout, 'show', head + ':' + rel)
        if (path.resolve().parent != Path(__file__).resolve().parent
                or p.read_regular(path)[0] != body
                or p.read_regular(checkout / rel)[0] != body
                or manifest['files'][rel]['sha256'] != sha(body)):
            raise RuntimeError('executing_code_not_committed:' + rel)
    for rel, expected in ((MAP_SNAPSHOT, MAP_BEFORE), (WRAPPER_SNAPSHOT, WRAPPER_BEFORE)):
        body = p.git(checkout, 'show', head + ':' + rel)
        if sha(body) != expected or p.read_regular(checkout / rel)[0] != body:
            raise RuntimeError('snapshot_binding_moved')
    registry = p.strict_json(p.git(checkout, 'show', head + ':ops/registered-producers.json'))
    for target in (MAP, str(HERMES / 'scripts' / WRAPPER), str(HERMES / 'cron/jobs.json')):
        if not any(row['glob'] == target and 'scripts/glp345_a4_adoption.py' in row['producers']
                   for row in registry['artifacts']):
            raise RuntimeError('producer_not_registered:' + target)
    body = p.git(checkout, 'show', SOURCE + ':' + MONITOR)
    if (sha(body) != AFTER or (checkout / MONITOR).read_bytes() != body
            or sha(p.git(checkout, 'show', SOURCE + '^:' + MONITOR)) != BEFORE):
        raise RuntimeError('approved_monitor_source_moved')
    if generation == WEEKLY_GENERATION:
        p.git(checkout, 'merge-base', '--is-ancestor', WEEKLY_SOURCE, head)
        p.git(checkout, 'merge-base', '--is-ancestor', WEEKLY_PREDECESSOR_SOURCE, head)
        snap = p.git(checkout, 'show', head + ':' + WEEKLY_SNAPSHOT)
        if sha(snap) != WEEKLY_BEFORE or p.read_regular(checkout / WEEKLY_SNAPSHOT)[0] != snap:
            raise RuntimeError('weekly_predecessor_binding_moved')
        # Genuine prior installed map/carrier are the accepted PR165 derivation,
        # not the older incompatible curated wrapper.
        prior = stage_pins(p.git(checkout, 'show', WEEKLY_PREDECESSOR_SOURCE + ':' + MAP_SNAPSHOT),
                           p.git(checkout, 'show', WEEKLY_PREDECESSOR_SOURCE + ':' + WRAPPER_SNAPSHOT))
        if tuple(map(sha, prior)) != (WEEKLY_MAP_BEFORE, WEEKLY_WRAPPER_BEFORE):
            raise RuntimeError('accepted_pr165_outputs_moved')
        emitted = stage_weekly_pins(*prior)
        for rel, body in zip((MAP, 'ops/hermes-cron-scripts/' + WRAPPER), emitted):
            if (p.git(checkout, 'show', head + ':' + rel) != body
                    or p.read_regular(checkout / rel)[0] != body
                    or manifest['files'][rel]['sha256'] != sha(body)):
                raise RuntimeError('weekly_co_pin_source_moved:' + rel)
        for rel, digest in ((WEEKLY, WEEKLY_AFTER), (RAILS, RAILS_AFTER)):
            approved = p.git(checkout, 'show', WEEKLY_SOURCE + ':' + rel)
            if (sha(approved) != digest or p.git(checkout, 'show', head + ':' + rel) != approved
                    or p.read_regular(checkout / rel)[0] != approved
                    or manifest['files'][rel]['sha256'] != digest):
                raise RuntimeError('approved_weekly_input_moved:' + rel)
        if sha(p.git(checkout, 'show', head + ':tests/fixtures/glp341-a3-result.json')) != A3_HASH:
            raise RuntimeError('historical_a3_source_moved')
        if not any(row['glob'] == WEEKLY and 'scripts/glp345_a4_adoption.py' in row['producers']
                   for row in registry['artifacts']):
            raise RuntimeError('weekly_producer_not_registered')
        body = p.git(checkout, 'show', WEEKLY_SOURCE + ':' + WEEKLY)
    elif generation != GENERATION:
        raise RuntimeError('unknown_a4_generation')
    return head, body


def plan(checkout, root, hermes, prepared_by, *, window=None, generation=GENERATION):
    import copy
    import maxbounty_pin_release as p
    import sync_from_repo as syncer
    root, hermes = root.resolve(strict=True), hermes.resolve(strict=True)
    if not isinstance(prepared_by, str) or not prepared_by.strip():
        raise RuntimeError('preparer_required')
    if generation not in (GENERATION, WEEKLY_GENERATION):
        raise RuntimeError('unknown_a4_generation')
    weekly = generation == WEEKLY_GENERATION
    head, source = authenticate(checkout, generation=generation) if weekly else authenticate(checkout)
    subject = WEEKLY if weekly else MONITOR
    before = WEEKLY_BEFORE if weekly else BEFORE
    after = WEEKLY_AFTER if weekly else AFTER
    wrapper_before = WEEKLY_WRAPPER_BEFORE if weekly else WRAPPER_BEFORE
    observed = {root / subject: p.read_regular(root / subject),
                root / MAP: p.read_regular(root / MAP),
                hermes / 'scripts' / WRAPPER: p.read_regular(hermes / 'scripts' / WRAPPER),
                root / syncer.STATE: p.read_regular(root / syncer.STATE)}
    if sha(observed[root / subject][0]) != before:
        raise RuntimeError('source_preimage_moved')
    state = p.strict_json(observed[root / syncer.STATE][0])
    # These retained A4 paths had no imported baseline. Bind both exact live
    # preimages before admitting only absence or their authenticated predecessor.
    expected_baseline = (None, before) if weekly else (before,)
    if state.get('schema') != 1 or state.get('hashes', {}).get(subject) not in expected_baseline:
        raise RuntimeError('source_import_baseline_moved')
    if weekly and state['hashes'].get(MAP) not in (None, WEEKLY_MAP_BEFORE):
        raise RuntimeError('map_import_baseline_moved')
    emitted, carrier = (stage_weekly_pins if weekly else stage_pins)(
        observed[root / MAP][0], observed[hermes / 'scripts' / WRAPPER][0])
    state['hashes'][subject] = after
    if weekly:
        state['hashes'][MAP] = sha(emitted)
    staged = {root / subject: source, root / MAP: emitted,
              hermes / 'scripts' / WRAPPER: carrier, root / syncer.STATE: p.json_bytes(state)}
    # Retain all actual weekly dependencies. Existing drift is explicit review
    # context, never "fixed" by dropping a pin or accepting arbitrary live bytes.
    expected = wrapper_pins(carrier)
    expected.update({v['path']: v['sha256'] for v in p.strict_json(emitted).values()})
    drift = []
    for absolute, digest in expected.items():
        path = root / Path(absolute).relative_to(BUSINESS)
        observed.setdefault(path, p.read_regular(path))
        actual = sha(staged.get(path, observed[path][0]))
        if actual != digest:
            drift.append({'path': str(path), 'expected': digest, 'actual': actual})
    if weekly and drift:
        raise RuntimeError('weekly_dependency_drift:' + ','.join(item['path'] for item in drift))
    registry = hermes / 'cron/jobs.json'
    observed[registry] = p.read_regular(registry)
    jobs = p.strict_json(observed[registry][0])['jobs']
    if not isinstance(jobs, list) or any(not isinstance(j, dict) for j in jobs):
        raise RuntimeError('invalid_native_registry')
    ids = [j.get('id') for j in jobs]
    if len(set(ids)) != len(ids):
        raise RuntimeError('duplicate_native_job')
    hits = [j for j in jobs if j.get('id') == JOB]
    if len(hits) != 1:
        raise RuntimeError('weekly_registration_count')
    job = hits[0]
    if (job.get('script') != WRAPPER or job.get('script_sha256') != wrapper_before
            or job.get('no_agent') is not True or job.get('run_claim') is not None
            or job.get('fire_claim') is not None or job.get('running')
            or job.get('schedule', {}).get('kind') != 'cron'
            or job['schedule'].get('expr') != '30 9 * * 1'):
        raise RuntimeError('weekly_identity_schedule_or_claim_moved')
    original = copy.deepcopy(job) if window is None else window.original_job(JOB)
    if original.get('enabled') is not True or original.get('state') != 'scheduled':
        raise RuntimeError('weekly_original_schedule_moved')
    if window is not None and (job.get('enabled') is not False or job.get('state') != 'paused'):
        raise RuntimeError('weekly_not_held_by_window')
    proposal = {'schema': 'glp345-a4-proposal-v1', 'generation': generation,
                'producer_head': head, 'source_head': WEEKLY_SOURCE if weekly else SOURCE, 'prepared_by': prepared_by.strip(),
                'root': str(root), 'hermes': str(hermes),
                'journal': str(Path(window.journal).resolve()) if window else None,
                'window_preimage_sha256': sha(p.json_bytes({k: v for k, v in
                    p.strict_json(p.read_regular(window.journal)[0]).items() if k != 'adopter'})) if window else None,
                'preimages': {str(path): {'sha256': sha(v[0]), 'mode': v[1]}
                             for path, v in observed.items() if path != registry},
                'outputs': {str(path): sha(body) for path, body in staged.items()},
                'registry_mode': observed[registry][1], 'job_before': copy.deepcopy(job),
                'job_original': original, 'weekly_residual_drift': drift}
    return proposal, observed, staged


def validate_review(review, proposal):
    from datetime import datetime, timezone
    import maxbounty_pin_release as p
    if not isinstance(review, dict) or review.get('proposal') != proposal or not proposal['journal']:
        raise RuntimeError('exact_in_window_proposal_required')
    when = datetime.strptime(review.get('reviewed_at_utc', ''), '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    if not 0 <= (datetime.now(timezone.utc) - when).total_seconds() <= 86400:
        raise RuntimeError('fresh_review_required')
    runtime = review.get('runtime_review', {})
    author = proposal['prepared_by']
    if (review.get('schema') != 'glp345-a4-review-v1'
            or review.get('decision') != 'ACCEPT_EXACT_BYTES'
            or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()
            or review['reviewer'].strip() == author
            or not isinstance(review.get('writer_exclusion'), str) or not review['writer_exclusion'].strip()
            or not isinstance(review.get('evidence'), list) or not review['evidence']
            or any(not isinstance(v, str) or not v.strip() for v in review['evidence'])
            or runtime.get('decision') != 'ACCEPT_EXACT_BYTES'
            or not isinstance(runtime.get('reviewer'), str) or not runtime['reviewer'].strip()
            or runtime['reviewer'].strip() == author
            or runtime.get('proposal_sha256') != sha(p.json_bytes(proposal))):
        raise RuntimeError('independent_source_and_runtime_review_required')


def adopt(checkout, root, hermes, review, *, scheduler, window=None):
    import maintenance_window as mw
    if window is None:
        raise RuntimeError('active_maintenance_window_required')
    if not isinstance(review, dict) or not isinstance(review.get('proposal'), dict):
        raise RuntimeError('independent_review_required')
    generation = review['proposal'].get('generation')
    if generation not in (GENERATION, WEEKLY_GENERATION):
        raise RuntimeError('unknown_a4_generation')
    custody = root / 'reports/ops/pin-archive/glp345-a4' / generation
    intent = custody / 'cutover.json'
    # A lost acknowledgement is not permission to reopen sealed sources, undo
    # the monitor alone, or call the scheduler saver for a second time.
    if intent.exists():
        raise RuntimeError('cutover_already_attempted_reconcile_no_replay')
    with mw.require_active_window(window, root, hermes) as active:
        return _adopt(checkout, root, hermes, review, scheduler, active, custody, intent)


def _adopt(checkout, root, hermes, review, scheduler, active, custody, intent):
    import copy
    from contextlib import ExitStack
    import maxbounty_pin_release as p
    import maintenance_window as mw
    import sync_from_repo as syncer
    root, hermes = root.resolve(strict=True), hermes.resolve(strict=True)
    registry = hermes / 'cron/jobs.json'
    if (scheduler._current_cron_store().jobs_file != registry
            or scheduler._jobs_lock_file() != registry.parent / '.jobs.lock'):
        raise RuntimeError('wrong_native_store_or_lock')
    with ExitStack() as locks:
        mw.require(locks.enter_context(scheduler._fire_job_lock(JOB)), 'native_fire_fence_unavailable')
        locks.enter_context(mw.native_registry_lock(scheduler))
        locks.enter_context(syncer.source_lock(root))
        if intent.exists():
            raise RuntimeError('cutover_already_attempted_reconcile_no_replay')
        proposal, observed, staged = plan(checkout, root, hermes,
            review['proposal'].get('prepared_by'), window=active,
            generation=review['proposal']['generation'])
        validate_review(review, proposal)
        def current_jobs():
            body, mode = p.read_regular(registry)
            jobs = p.strict_json(body)['jobs']
            ids = [j.get('id') for j in jobs]
            if (len(ids) != len(set(ids)) or mode != proposal['registry_mode']
                    or [j for j in jobs if j.get('id') == JOB] != [proposal['job_before']]):
                raise RuntimeError('native_full_target_record_cas')
            return jobs
        p.assert_unchanged({path: value for path, value in observed.items() if path != registry})
        current_jobs()
        p.preserve_custody(custody, observed, staged)
        p.assert_unchanged({path: value for path, value in observed.items() if path != registry})
        current_jobs()
        original = {path: observed[path] for path in staged}
        mw.atomic_json(intent, {'schema': 'glp345-a4-cutover-intent-v1', 'proposal': proposal,
                               'review_sha256': sha(p.json_bytes(review))})
        active.accept_file_updates({str(k): sha(v[0]) for k, v in original.items()}, proposal['outputs'])
        target = copy.deepcopy(proposal['job_before'])
        target['script_sha256'] = sha(staged[hermes / 'scripts' / WRAPPER])
        active.accept_job_update(JOB, proposal['job_before'], target)
        def commit():
            p.assert_unchanged({path: value for path, value in observed.items()
                                if path not in staged and path != registry})
            candidate = copy.deepcopy(current_jobs())
            next(j for j in candidate if j['id'] == JOB)['script_sha256'] = target['script_sha256']
            scheduler._record_load_stamp(None)
            try:
                scheduler._save_jobs_unlocked(copy.deepcopy(candidate))
            finally:
                scheduler._record_load_stamp(None)
            if p.strict_json(p.read_regular(registry)[0])['jobs'] != candidate:
                raise RuntimeError('native_readback_mismatch_no_retry')
        # Real maintained DAC cohort holds every source/cache sealed through
        # native save/readback. It owns pre-commit rollback and uncertain seals.
        active.switch_files(staged, original, commit=commit)
        return {'applied': True, 'generation': proposal['generation'], 'proposal': proposal,
                'custody': str(custody), 'registration_delta': [JOB + '.script_sha256'],
                'weekly_ready': not proposal['weekly_residual_drift'], 'jobs_executed': 0}


def main(argv=None):
    import argparse
    import json
    import os
    import sys
    from contextlib import nullcontext
    import maintenance_window as mw
    import maxbounty_pin_release as p
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=BUSINESS)
    parser.add_argument('--hermes', type=Path, default=HERMES)
    parser.add_argument('--prepared-by', default='F08 source owner')
    parser.add_argument('--generation', choices=(GENERATION, WEEKLY_GENERATION), default=GENERATION)
    parser.add_argument('--window', type=Path)
    parser.add_argument('--review', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.apply:
            if args.window is None or args.review is None:
                raise RuntimeError('window_and_independent_review_required')
            if any(n == 'cron' or n.startswith('cron.') for n in sys.modules):
                raise RuntimeError('fresh_native_interpreter_required')
            os.environ['HERMES_HOME'] = str(args.hermes.resolve())
            sys.path.insert(0, str(args.hermes / 'hermes-agent'))
            from cron import jobs as scheduler
            review = p.strict_json(args.review.read_bytes())
            if review.get('proposal', {}).get('generation') != args.generation:
                raise RuntimeError('review_generation_mismatch')
            result = adopt(args.checkout, args.root, args.hermes, review,
                           scheduler=scheduler, window=args.window)
        else:
            ctx = mw.require_active_window(args.window, args.root, args.hermes) if args.window else nullcontext(None)
            with ctx as active:
                proposal, _, _ = plan(args.checkout, args.root, args.hermes, args.prepared_by,
                                       window=active, generation=args.generation)
            result = {'applied': False, 'proposal': proposal}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print('GLP345_A4_ADOPTION_REFUSED: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
