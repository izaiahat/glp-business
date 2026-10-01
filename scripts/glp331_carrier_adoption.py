#!/usr/bin/env python3
"""One fixed GLP331 carrier generation, only inside a verified maintenance window.

Reuse fleet_pin_release's exact accepted sources and carrier derivation. No sync
state update, importer apply, job run, resume, schedule or other consumer repin.
Parent-owned exclusion of old importers/direct callers remains a prerequisite.
"""
import argparse
import copy
import datetime as dt
from contextlib import ExitStack
from pathlib import Path
import sys

import fleet_pin_release as fleet
import maxbounty_pin_release as p
import maintenance_window as mw
import sync_from_repo as syncer

GENERATION = 'glp331-carrier-20261001'
# Literal declarations for the maintained inventory; not caller-selected scope.
PATHS = (
    'scripts/affiliate_program_withdrawal.py',
    'scripts/affiliate_sheet_batch_writer.py',
    'scripts/affiliate_terms_fanout.py',
    'scripts/affiliate_approval_auto_reconciler.py',
    'scripts/affiliate_withdrawal_candidate.py',
    'scripts/glp_sheet_sync_drain.py',
    'ops/hermes-cron-scripts/glp_affiliate_reconciliation_honesty.sh',
)
WRAPPER = 'glp_affiliate_reconciliation_honesty.sh'
ABSENT_PATHS = ('scripts/affiliate_program_withdrawal.py', 'scripts/affiliate_withdrawal_candidate.py')


def plan(checkout, root, hermes, prepared_by, *, window=None):
    if window is None:
        raise RuntimeError('active_maintenance_window_required')
    root, hermes, checkout = (Path(v).resolve(strict=True) for v in (root, hermes, checkout))
    data = mw.read_journal(window.journal)
    if (data['root'] != str(root) or data['hermes'] != str(hermes)
            or not mw.MaintenanceWindow(window.backend, window.journal,
                mw.default_locks(root, hermes)).status()['verified']):
        raise RuntimeError('active_maintenance_window_required')
    proposal, observed, staged = fleet.prepare_glp331(checkout, fleet.GLP331_SOURCE_HEAD,
        prepared_by, business=root, hermes=hermes, active_window=window)
    expected = {root / rel for rel in PATHS} | {hermes / 'scripts' / WRAPPER}
    if set(PATHS) != fleet.GLP331_SOURCES or set(staged) != expected:
        raise RuntimeError('fixed_glp331_scope_moved')
    if {path for path, value in observed.items() if value is None} - {root / rel for rel in ABSENT_PATHS}:
        raise RuntimeError('unreviewed_missing_source')
    # Authenticate the additional executing module and its maintained declaration.
    for name in ('glp331_carrier_adoption.py', 'maintenance_inventory.py'):
        body = p.git(checkout, 'show', proposal['producer_head'] + ':scripts/' + name)
        path = Path(__file__).parent / name
        if p.read_regular(path)[0] != body:
            raise RuntimeError('executing_generation_not_reviewed:' + name)
        observed[path] = p.read_regular(path)
        proposal['preimages'][str(path)] = {'sha256': p.sha_bytes(body), 'mode': observed[path][1]}
    registered = p.strict_json(p.git(checkout, 'show', proposal['producer_head'] + ':ops/registered-producers.json'))
    for target in ('/home/ubuntu/.hermes/scripts/' + WRAPPER, '/home/ubuntu/.hermes/cron/jobs.json'):
        if not any(row['glob'] == target and 'scripts/glp331_carrier_adoption.py' in row['producers']
                   for row in registered['artifacts']):
            raise RuntimeError('generation_not_registered:' + target)
    registry = hermes / 'cron/jobs.json'
    records = p.strict_json(observed[registry][0])['jobs']
    # Registry is custody, NOT a handoff digest. CAS only complete consumer
    # records, with field presence intact; unrelated rows come from locked load.
    proposal['preimages'].pop(str(registry))
    proposal.update(generation=GENERATION, schema='glp331-carrier-generation-proposal-v1',
        consumer_records={jid: copy.deepcopy(next(j for j in records if j['id'] == jid))
                          for jid in fleet.GLP331_CONSUMERS},
        registry_mode=observed[registry][1], journal=str(Path(window.journal).resolve()),
        window_preimage_sha256=mw.json_sha({k: v for k, v in data.items() if k != 'adopter'}))
    return proposal, observed, staged


def validate_review(review, proposal):
    if not isinstance(review, dict) or review.get('proposal') != proposal:
        raise RuntimeError('reviewed_preimage_or_candidate_moved')
    when = dt.datetime.strptime(review.get('reviewed_at_utc', ''), '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
    if not 0 <= (dt.datetime.now(dt.timezone.utc) - when).total_seconds() <= 86400:
        raise RuntimeError('fresh_review_timestamp_required')
    preparer = proposal['prepared_by'].strip()
    runtime = review.get('runtime_review', {})
    if (review.get('schema') != 'glp331-carrier-generation-review-v1'
            or review.get('decision') != 'ACCEPT_EXACT_BYTES'
            or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()
            or review['reviewer'].strip() == preparer
            or not isinstance(review.get('writer_exclusion'), str) or not review['writer_exclusion'].strip()
            or not isinstance(review.get('evidence'), list) or not review['evidence']
            or any(not isinstance(v, str) or not v.strip() for v in review['evidence'])
            or runtime.get('schema') != 'glp331-carrier-runtime-review-v1'
            or runtime.get('decision') != 'ACCEPT_EXACT_BYTES'
            or not isinstance(runtime.get('reviewer'), str) or not runtime['reviewer'].strip()
            or runtime['reviewer'].strip() == preparer
            or runtime.get('proposal_sha256') != p.sha_bytes(p.json_bytes(proposal))):
        raise RuntimeError('independent_matching_runtime_review_required')


def adopt(checkout, root, hermes, review, *, scheduler, window=None):
    if window is None:
        raise RuntimeError('active_maintenance_window_required')
    if not isinstance(review, dict) or not isinstance(review.get('proposal'), dict):
        raise RuntimeError('independent_exact_review_required')
    # Check intent before any source read: possibly sealed bytes never get opened
    # on replay, even after a lost native acknowledgement or a fresh proposal.
    custody = Path(root) / 'reports/ops/pin-archive/glp331-window' / GENERATION
    intent = custody / 'cutover.json'
    if intent.exists():
        raise RuntimeError('cutover_already_attempted_reconcile_no_replay')
    with mw.require_active_window(window, root, hermes) as active:
        return _adopt(checkout, root, hermes, review, scheduler, active, custody, intent)


def _adopt(checkout, root, hermes, review, scheduler, active, custody, intent):
    root, hermes = Path(root).resolve(strict=True), Path(hermes).resolve(strict=True)
    registry = hermes / 'cron/jobs.json'
    if (scheduler._current_cron_store().jobs_file != registry
            or scheduler._jobs_lock_file() != registry.parent / '.jobs.lock'):
        raise RuntimeError('wrong_scheduler_store_or_lock')
    with ExitStack() as fences:
        for jid in sorted(fleet.GLP331_CONSUMERS):
            mw.require(fences.enter_context(scheduler._fire_job_lock(jid)), 'native_fire_fence_unavailable')
        fences.enter_context(mw.native_registry_lock(scheduler))
        fences.enter_context(syncer.source_lock(root))
        if intent.exists():
            raise RuntimeError('cutover_already_attempted_reconcile_no_replay')
        proposal, observed, staged = plan(checkout, root, hermes,
            review['proposal'].get('prepared_by'), window=active)
        validate_review(review, proposal)
        def unchanged_inputs():
            for path, value in observed.items():
                if path == registry:
                    continue
                if value is None:
                    mw.require(not path.exists() and not path.is_symlink(), 'absent_source_moved')
                elif p.read_regular(path) != value:
                    raise RuntimeError('source_preimage_moved:' + str(path))
        def current_jobs():
            body, mode = p.read_regular(registry)
            if mode != proposal['registry_mode']:
                raise RuntimeError('registry_mode_moved')
            jobs = p.strict_json(body)['jobs']
            if not isinstance(jobs, list) or any(not isinstance(j, dict) for j in jobs):
                raise RuntimeError('registry_jobs_invalid')
            ids = [j.get('id') for j in jobs]
            if len(ids) != len(set(ids)):
                raise RuntimeError('duplicate_registration_refused')
            for jid, expected in proposal['consumer_records'].items():
                if [j for j in jobs if j.get('id') == jid] != [expected]:
                    raise RuntimeError('consumer_record_cas_moved:' + jid)
            return jobs
        unchanged_inputs()
        current_jobs()
        p.preserve_custody(custody, {k: v for k, v in observed.items() if v is not None}, staged)
        unchanged_inputs()
        current_jobs()
        original = {path: observed[path] for path in staged}
        before_hash = {str(path): None if value is None else p.sha_bytes(value[0])
                       for path, value in original.items()}
        after_hash = {str(path): p.sha_bytes(body) for path, body in staged.items()}
        # No replay even if interruption happens between transaction and sealing.
        mw.atomic_json(intent, {'schema': 'glp331-window-cutover-intent-v1', 'proposal': proposal,
                               'review_sha256': p.sha_bytes(p.json_bytes(review)),
                               'absent': [name for name, value in before_hash.items() if value is None]})
        active.accept_file_updates(before_hash, after_hash,
                                  absent=[name for name, value in before_hash.items() if value is None])
        target = copy.deepcopy(proposal['consumer_records'][fleet.GLP331_JOB])
        target['script_sha256'] = after_hash[str(hermes / 'scripts' / WRAPPER)]
        # Job intent precedes the cohort journal snapshot (PR122 callback rule).
        active.accept_job_update(fleet.GLP331_JOB, proposal['consumer_records'][fleet.GLP331_JOB], target)
        def commit():
            for path, value in observed.items():
                if path not in staged and path != registry and p.read_regular(path) != value:
                    raise RuntimeError('retained_dependency_moved_at_commit:' + str(path))
            jobs = current_jobs()  # fresh full registry, not the proposal snapshot
            candidate = copy.deepcopy(jobs)
            next(j for j in candidate if j['id'] == fleet.GLP331_JOB)['script_sha256'] = target['script_sha256']
            scheduler._record_load_stamp(None)
            try:
                scheduler._save_jobs_unlocked(copy.deepcopy(candidate))
            finally:
                scheduler._record_load_stamp(None)
            if p.strict_json(p.read_regular(registry)[0])['jobs'] != candidate:
                raise RuntimeError('registration_readback_mismatch_no_retry')
        active.switch_glp331_files(staged, original, proposal['source_modes'], commit=commit)
        return {'applied': True, 'generation': GENERATION, 'proposal': proposal, 'custody': str(custody),
                'registration_delta': [fleet.GLP331_JOB + '.script_sha256']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', choices=(GENERATION,), default=GENERATION)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=syncer.DEFAULT_ROOT)
    parser.add_argument('--hermes', type=Path, default=Path('/home/ubuntu/.hermes'))
    parser.add_argument('--window', type=Path, required=True)
    parser.add_argument('--prepared-by')
    parser.add_argument('--review', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        if args.apply:
            if not args.review or args.prepared_by:
                raise RuntimeError('apply_requires_only_exact_review')
            import os
            os.environ['HERMES_HOME'] = str(args.hermes)
            sys.path.insert(0, str(args.hermes / 'hermes-agent'))
            from cron import jobs as scheduler
            result = adopt(args.checkout, args.root, args.hermes,
                p.strict_json(p.read_regular(args.review)[0]), scheduler=scheduler, window=args.window)
        else:
            if not args.prepared_by or args.review:
                raise RuntimeError('preview_requires_only_prepared_by')
            with mw.require_active_window(args.window, args.root, args.hermes) as active:
                proposal, _, _ = plan(args.checkout, args.root, args.hermes, args.prepared_by, window=active)
                result = {'applied': False, 'proposal': proposal}
        print(p.json_bytes(result).decode(), end='')
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print('GLP331_CARRIER_ADOPTION_REFUSED: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
