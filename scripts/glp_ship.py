#!/usr/bin/env python3
"""Prepare or install one registered Git generation. See ops/glp-ship.md."""
import sys
# Read-only inspection includes import-time artifacts, even without caller -B.
sys.dont_write_bytecode = True

import argparse
from contextlib import contextmanager, ExitStack, nullcontext
import getpass
import json
import os
from pathlib import Path
import subprocess
import tempfile

import sync_from_repo as syncer
from glp_ship_generation import GitGeneration, fingerprint, json_sha, validate_review
from glp_ship_provenance import derive_pins, verify_merge
from glp_ship_registry import registered_job
from maxbounty_pin_release import read_regular


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('job_id')
    result.add_argument('--merge-sha', required=True)
    result.add_argument('--apply', action='store_true')
    result.add_argument('--review', type=Path)
    result.add_argument('--inspect-attempt', action='store_true')
    result.add_argument('--proposal', type=Path, help='Existing READY document for read-only inspection')
    result.add_argument('--refusal-evidence', type=Path)
    result.add_argument('--refusal-review', type=Path)
    result.add_argument('--checkout', type=Path, default=Path(__file__).resolve().parents[1])
    result.add_argument('--root', type=Path, default=syncer.DEFAULT_ROOT)
    result.add_argument('--hermes', type=Path, default=Path('/home/ubuntu/.hermes'))
    result.add_argument('--output', type=Path)
    result.add_argument('--prepared-by', default=getpass.getuser())
    return result


def write_json(path, value):
    from maintenance_window import atomic_json
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, value)


# Bounded executing route closure, including AST-only declaration readers and
# the enrollment checker/later bridge import. Historical fleet branches are NOT
# imported by this route and must not become generic whole-fleet adoption.
INSTALLER_FILES = (
    'glp_ship.py', 'glp_ship_generation.py', 'glp_ship_registry.py',
    'glp_ship_provenance.py', 'glp_ship_runtime.py', 'glp_ship_discovery.py',
    'glp_ship_collector.py', 'glp44_scoped_adoption.py', 'glp73_paired_adoption.py',
    'sync_from_repo.py', 'discovery_predecessor_migration.py',
    'maintenance_window.py', 'maintenance_inventory.py', 'maintenance_cohort.py',
    'maintenance_admission.py', 'maxbounty_pin_release.py',
    'glp331_carrier_adoption.py', 'glp345_a4_adoption.py',
    'glp44_safe_bridge_activation.py', 'glp334_pin_closure.py',
)


def authenticate_installer(checkout):
    for name in INSTALLER_FILES:
        if read_regular(Path(__file__).with_name(name))[0] != read_regular(Path(checkout) / 'scripts' / name)[0]:
            raise RuntimeError('ship_installer_not_exact_merge:' + name)


def preclaim_plan(root, hermes):
    # plan() is read-only. Do not use enter(), snapshot(), scheduler() or an
    # affected-path override here; every existing census/unknown guard applies.
    from maintenance_window import LinuxBackend, MaintenanceWindow, default_locks
    journal = root / 'reports/ops/glp-ship/preclaim/window.json'
    return MaintenanceWindow(LinuxBackend(root, hermes, journal), journal,
                             default_locks(root, hermes)).plan()


def claim_attempt(path, proposal, *, checkout=None):
    """Durable intent follows full preflight; existing intents never permit replay."""
    if path.exists() or path.is_symlink():
        raise RuntimeError('ship_attempt_exists_reconcile:' + str(path))
    if proposal.get('maintenance_window_required'):
        if checkout is None:
            raise RuntimeError('ship_preclaim_committed_checkout_required')
        authenticate_installer(checkout)
        preclaim_plan(Path(proposal['root']), Path(proposal['hermes']))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise RuntimeError('ship_attempt_exists_reconcile:' + str(path)) from None
    with os.fdopen(fd, 'w') as handle:
        json.dump({'state': 'started', 'proposal_sha256': json_sha(proposal)}, handle)
        handle.flush(); os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def inspect_attempt(checkout, root, hermes, spec, proposal, evidence=None, review=None):
    """Read-only diagnosis under the existing attempt contract, NEVER a reset.

    Legacy started intents have no authenticated contemporaneous boundary marker.
    Even reviewed traceback evidence plus equality now cannot authorize a state
    transition or reuse of the old proposal. Keep original bytes as evidence.
    """
    import hashlib
    import re
    source = proposal.get('source', '')
    if (spec.name != 'GLP334' or proposal.get('schema') != 'glp-ship-proposal-v1'
            or proposal.get('job_id') != spec.name or not re.fullmatch('[0-9a-f]{40}', source)
            or proposal.get('root') != str(root.resolve())
            or proposal.get('hermes') != str(hermes.resolve())
            or proposal.get('kind') != spec.kind or proposal.get('tier') != spec.tier
            or proposal.get('paths') != list(spec.paths)
            or set(proposal.get('jobs', {})) != set(spec.jobs)
            or proposal.get('maintenance_window_required') is not True):
        raise RuntimeError('ship_refusal_proposal_binding')
    intent = root / 'reports/ops/glp-ship/attempts' / (spec.name + '-' + source + '.json')
    original = read_regular(intent)[0]
    attempt_sha = hashlib.sha256(original).hexdigest()
    proposal_sha = json_sha(proposal)
    if json.loads(original) != {'state': 'started', 'proposal_sha256': proposal_sha}:
        raise RuntimeError('ship_refusal_attempt_binding')
    output = root / 'reports/ops/glp-ship' / spec.name / source
    # Absence is only a consistency prerequisite, never historical proof.
    for name in ('window.json', 'RESULT.json'):
        if (output / name).exists() or (output / name).is_symlink():
            raise RuntimeError('ship_refusal_window_or_result_present')
    result = {'state': 'started', 'attempt_sha256': attempt_sha,
              'proposal_sha256': proposal_sha, 'source': source,
              'transition_performed': False, 'replay_permitted': False,
              'assessment': 'UNRESOLVED_NO_REVIEWED_BOUNDARY_EVIDENCE',
              'limit': 'No authenticated contemporaneous boundary marker; retain original intent. '
                       'No reset, deletion, terminal success, or old proposal/apply replay.'}
    if evidence is None and review is None:
        return result
    # The old intent did not bind --output. Require the independently reviewed
    # original journal location for consistency; this still cannot prove history.
    journal = Path((evidence or {}).get('window_journal', ''))
    if (not journal.is_absolute() or journal.name != 'window.json'
            or journal != journal.resolve()
            or not journal.is_relative_to(root.resolve())
            or journal.exists() or journal.is_symlink()
            or any((journal.parent / n).exists() or (journal.parent / n).is_symlink()
                   for n in ('RESULT.json', 'enrollment-receipt.json'))):
        raise RuntimeError('ship_refusal_original_window_binding')
    binding = {'source': source, 'attempt_sha256': attempt_sha,
               'proposal_sha256': proposal_sha}
    if (not evidence or not review
            or any(evidence.get(k) != v or review.get(k) != v for k, v in binding.items())
            or evidence.get('schema') != 'glp-ship-pre-window-refusal-v1'
            or evidence.get('root') != str(root.resolve())
            or evidence.get('hermes') != str(hermes.resolve())
            or evidence.get('stage') != 'MaintenanceWindow.plan'
            or evidence.get('exception') != 'FileNotFoundError'
            or evidence.get('missing_path') not in [str(root / 'scripts' / name) for name in
                ('glp331_carrier_adoption.py', 'glp44_safe_bridge_activation.py')]
            or any(evidence.get(k) is not False for k in
                   ('snapshot_started', 'pause_started', 'adoption_started'))
            or not re.fullmatch('[0-9a-f]{64}', str(evidence.get('traceback_sha256', '')))):
        raise RuntimeError('ship_refusal_boundary_evidence_required')
    reviewer = str(review.get('reviewer') or '').strip().casefold()
    recorder = str(evidence.get('recorded_by') or '').strip().casefold()
    if (not reviewer or not recorder or reviewer in {recorder, str(proposal['prepared_by']).strip().casefold()}
            or review.get('schema') != 'glp-ship-refusal-review-v1'
            or review.get('decision') != 'ACCEPT_EXACT_PRE_WINDOW_REFUSAL'
            or review.get('evidence_sha256') != json_sha(evidence)):
        raise RuntimeError('ship_refusal_independent_review_required')
    proof = verify_merge(checkout, source)
    if (proof.get('merge_sha') != source or any(proposal['merge_proof'].get(k) != proof[k]
                                              for k in ('merge_sha', 'pr_number', 'pr_head'))):
        raise RuntimeError('ship_refusal_source_binding')
    generation = GitGeneration(source, spec.kind, spec.paths, proposal['hashes'],
                               proposal['merge_proof'], proposal)
    generation.verify(checkout)
    # Same every-preimage + complete-target-record check as authorize(), without
    # reusing, synthesizing, or granting any enrollment review/apply authority.
    generation.verify_target(root, hermes)
    if read_regular(intent)[0] != original:
        raise RuntimeError('ship_refusal_attempt_moved_during_inspection')
    if any((output / n).exists() or (output / n).is_symlink() for n in ('window.json', 'RESULT.json')):
        raise RuntimeError('ship_refusal_window_moved_during_inspection')
    if (journal.exists() or journal.is_symlink()
            or any((journal.parent / n).exists() or (journal.parent / n).is_symlink()
                   for n in ('RESULT.json', 'enrollment-receipt.json'))):
        raise RuntimeError('ship_refusal_original_window_moved_during_inspection')
    result['assessment'] = 'REVIEWED_REFUSAL_CURRENTLY_CONSISTENT'
    return result


@contextmanager
def committed_checkout(checkout, source):
    # Fetch the fixed repository, never a caller-controlled origin URL.
    subprocess.run(['git', '-C', str(checkout), 'fetch', '--no-tags', syncer.REMOTE,
                    '+refs/heads/main:refs/remotes/origin/main'], check=True, timeout=120)
    proof = verify_merge(checkout, source)
    with tempfile.TemporaryDirectory(prefix='glp-ship-source-') as temp:
        candidate = Path(temp) / 'repo'
        subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', '--no-checkout',
                        str(checkout), str(candidate)], check=True, timeout=120)
        subprocess.run(['git', '-C', str(candidate), 'checkout', '--quiet', '--detach', source],
                       check=True, timeout=120)
        # Authenticate the executing installer, not just its proposed payload.
        authenticate_installer(candidate)
        yield candidate, proof


def preview(checkout, root, hermes, generation, window=None, *, _source_owner=None):
    if generation.kind in ('reporting', 'reporting-enrollment'):
        import glp44_scoped_adoption as adopter
        return adopter.plan(checkout, root, hermes, window=window, generation=generation,
                            _source_owner=_source_owner)[0]
    if generation.kind == 'retention':
        import glp73_paired_adoption as adopter
        plan = adopter.plan(checkout, root, hermes, generation=generation, window=window,
                            allow_scheduled_preview=window is None, _source_owner=_source_owner)
        return adopter.retention_proposal(checkout, root, hermes, plan, generation=generation)
    from discovery_predecessor_migration import preview_generation
    return preview_generation(checkout, root, generation)


def prepare(checkout, root, hermes, spec, source, proof, prepared_by):
    if not str(prepared_by).strip():
        raise RuntimeError('preparer_identity_required')
    authenticate_installer(checkout)
    if spec.window:
        preclaim_plan(root, hermes)
    hashes = derive_pins(checkout, source, spec.paths)
    if spec.kind == 'reporting-enrollment':
        from glp_ship_registry import enrollment_hashes
        hashes = enrollment_hashes(checkout)
    proposal = {'schema': 'glp-ship-proposal-v1', 'source': source,
                'kind': spec.kind, 'job_id': spec.name, 'tier': spec.tier,
                'paths': list(spec.paths), 'hashes': hashes, 'merge_proof': proof,
                'root': str(root), 'hermes': str(hermes), 'prepared_by': prepared_by,
                'maintenance_window_required': spec.window,
                'run_policy': {'entry': 'manual_registered', 'job_ids': list(spec.jobs),
                               'on_success': 'enable', 'on_failure': 'pause'}}
    if spec.kind == 'reporting-enrollment':
        proposal['run_policy'] = {'entry': 'NONE_ENROLLMENT_ONLY', 'job_ids': list(spec.jobs),
                                  'on_success': 'preserve', 'on_failure': 'reconcile_no_replay'}
    generation = GitGeneration(source, spec.kind, spec.paths, hashes, proof, proposal)
    plan = preview(checkout, root, hermes, generation)
    paths = {root / p for p in spec.paths} | {root / syncer.STATE, root / syncer.MANIFEST}
    paths.update(hermes / 'scripts' / name for name in spec.wrappers)
    paths.update(root / rel for rel in plan.get('retained_source', {}))
    if spec.kind == 'retention':
        paths.add(root / 'scripts/glp_current_content_base_schema.py')
    # A writable baseline is not provenance. Every installed payload must also
    # be a Git-known predecessor; the F01 retained fixture has its own exact gate.
    for rel in spec.paths:
        if spec.kind == 'discovery' and not (root / rel).exists() and not (root / rel).is_symlink():
            continue
        current = fingerprint(root / rel)['sha256']
        if spec.kind == 'discovery' and rel == 'scripts/affiliate_supply_discovery.py':
            continue  # preview_generation authenticates the fixed retained fixture.
        if current != hashes[rel] and current not in syncer.reviewed_prior_hashes(checkout, rel):
            raise RuntimeError('unknown_installed_bytes:' + rel)
    proposal['before'] = {
        str(p): (None if spec.kind == 'discovery' and not p.exists() and not p.is_symlink()
                 else fingerprint(p)) for p in sorted(paths)}
    jobs = json.loads(read_regular(hermes / 'cron/jobs.json')[0])['jobs']
    proposal['jobs'] = {}
    for jid in spec.jobs:
        found = [job for job in jobs if job.get('id') == jid]
        if len(found) != 1 or any(found[0].get(k) for k in ('running', 'fire_claim', 'run_claim')):
            raise RuntimeError('ship_job_not_idle:' + jid)
        proposal['jobs'][jid] = found[0]
    if spec.kind == 'discovery':
        proposal['discovery_plan'] = plan
    if spec.name == 'GLP44':
        from glp_ship_collector import registration
        proposal['collector_registration'] = registration(root, hermes)
        retained = proposal['collector_registration']['retained_sources']
        if derive_pins(checkout, source, tuple(retained)) != retained:
            raise RuntimeError('collector_entry_not_exact_merge')
    return generation


def zero_diff(checkout, root, hermes, generation, spec, *, _source_owner=None):
    # sync's success alone means conflict-free, not zero differences.
    if spec.kind == 'reporting-enrollment':
        from glp44_scoped_adoption import _enrollment_sources
        _enrollment_sources(checkout, root, generation)
    elif syncer.sync(root, checkout, False, only=generation.paths, _source_owner=_source_owner) != 0:
        raise RuntimeError('ship_zero_diff_refused')
    state = json.loads(read_regular(root / syncer.STATE)[0])['hashes']
    for rel, expected in generation.hashes.items():
        if fingerprint(root / rel)['sha256'] != expected or state.get(rel) != expected:
            raise RuntimeError('ship_nonzero_diff:' + rel)
    jobs = json.loads(read_regular(hermes / 'cron/jobs.json')[0])['jobs']
    for jid, wrapper in zip(spec.jobs, spec.wrappers):
        hits = [job for job in jobs if job.get('id') == jid]
        if len(hits) != 1 or hits[0].get('script_sha256') != fingerprint(hermes / 'scripts' / wrapper)['sha256']:
            raise RuntimeError('ship_wrapper_readback_moved:' + jid)


def apply_generation(checkout, root, hermes, spec, generation, review, output):
    validate_review(generation.proposal, review)  # Before windows or scheduler effects.
    generation.authorize(review, root, hermes, None)
    from maintenance_window import LinuxBackend
    journal = output / 'window.json'
    backend = LinuxBackend(root, hermes, journal)
    scheduler = backend.scheduler()
    # Native fire fences are reentrant. Keep them through restoration and the
    # manual run so a due occurrence cannot slip into the restore/run gap.
    with ExitStack() as fences:
        # Cleanup's producer acquires this singleton before its fire fences.
        import glp44_scoped_adoption as pair
        cleanup_owner = fences.enter_context(pair.cleanup_session(hermes)) if spec.kind == 'reporting-enrollment' else None
        for jid in sorted(spec.jobs):
            if not fences.enter_context(scheduler._fire_job_lock(jid)):
                raise RuntimeError('ship_native_fire_fence_unavailable:' + jid)
        return _apply_and_run(checkout, root, hermes, spec, generation, review, output,
                              journal, backend, scheduler, _cleanup_owner=cleanup_owner)


def _apply_and_run(checkout, root, hermes, spec, generation, review, output, journal, backend, scheduler, *, _cleanup_owner=None):
    from maintenance_window import MaintenanceWindow, default_locks, require_active_window
    from glp_ship_runtime import run_registered
    # One maintained root-inode writer lock spans installation, custody, native
    # commit, exact readback and restoration. Explicit live leases let nested
    # previews join it without releasing or reacquiring the non-reentrant flock.
    import glp44_scoped_adoption as pair
    cleanup = pair.cleanup_session(hermes, _cleanup_owner) if spec.kind == 'reporting-enrollment' else nullcontext()
    with cleanup as cleanup_owner, syncer.source_lock(root) as owner:
        if spec.window:
            window = MaintenanceWindow(backend, journal, default_locks(root, hermes))
            window.enter()
            try:
                with require_active_window(journal, root, hermes) as active:
                    preview(checkout, root, hermes, generation, active, _source_owner=owner)
                if spec.kind in ('reporting', 'reporting-enrollment'):
                    import glp44_scoped_adoption as adopter
                else:
                    import glp73_paired_adoption as adopter
                cleanup_args = {'_cleanup_owner': cleanup_owner} if spec.kind == 'reporting-enrollment' else {}
                result = adopter.adopt(checkout, root, hermes, review, scheduler=scheduler,
                                       window=journal, generation=generation, _source_owner=owner, **cleanup_args)
                zero_diff(checkout, root, hermes, generation, spec, _source_owner=owner)
            finally:
                # The existing exit refuses uncertain commits; never override it.
                window.exit()
            if window.status()['state'] != 'closed':
                raise RuntimeError('ship_window_not_restored')
        else:
            from glp_ship_discovery import apply
            result = apply(checkout, root, hermes, generation, review, output, scheduler,
                           _source_owner=owner)
            zero_diff(checkout, root, hermes, generation, spec, _source_owner=owner)
    # Release the source lock before registered runtime admission, which takes
    # its own source fence; native fire fences remain held throughout the run.
    # Consumer locks belong to the maintenance window until exact restoration.
    # Native registered wrappers can acquire them only after that exit.
    if spec.kind == 'reporting-enrollment':
        return {'schema': 'glp-ship-result-v1', 'source': generation.source,
                'proposal_sha256': json_sha(generation.proposal), 'adoption': result,
                'success': True, 'runs': [], 'policy': 'ENROLLMENT_ONLY_NO_EXECUTION'}
    if spec.name == 'GLP44':
        from glp_ship_collector import run_registered as run_collector
        runs = run_collector(root, hermes, generation.proposal['collector_registration'], output)
    else:
        runs = run_registered(root, hermes, spec.jobs, output, scheduler=scheduler)
    return {'schema': 'glp-ship-result-v1', 'source': generation.source,
            'proposal_sha256': json_sha(generation.proposal), 'adoption': result, **runs}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        spec = registered_job(args.job_id)
        root, hermes = args.root.resolve(strict=True), args.hermes.resolve(strict=True)
        if args.inspect_attempt:
            if args.apply or args.review or args.output or not args.proposal:
                raise RuntimeError('ship_inspection_is_read_only_requires_existing_proposal')
            proposal = json.loads(read_regular(args.proposal)[0])['proposal']
            if proposal.get('source') != args.merge_sha:
                raise RuntimeError('ship_refusal_source_binding')
            evidence = json.loads(read_regular(args.refusal_evidence)[0]) if args.refusal_evidence else None
            refusal_review = json.loads(read_regular(args.refusal_review)[0]) if args.refusal_review else None
            print(json.dumps(inspect_attempt(args.checkout, root, hermes, spec, proposal,
                                             evidence, refusal_review), sort_keys=True))
            return 0  # Diagnostic completed, NOT terminal success or apply authority.
        if args.proposal or args.refusal_evidence or args.refusal_review:
            raise RuntimeError('ship_refusal_inputs_require_read_only_inspection')
        output = (args.output or root / 'reports/ops/glp-ship' / spec.name / args.merge_sha).resolve()
        review = json.loads(read_regular(args.review)[0]) if args.review else None
        with committed_checkout(args.checkout.resolve(strict=True), args.merge_sha) as (checkout, proof):
            generation = prepare(checkout, root, hermes, spec, args.merge_sha, proof, args.prepared_by)
            ready = {'schema': 'glp-ship-ready-v1', 'proposal': generation.proposal,
                     'proposal_sha256': json_sha(generation.proposal),
                     'independent_review_required': spec.tier == 'A'}
            if args.apply:
                validate_review(generation.proposal, review)
                intent = root / 'reports/ops/glp-ship/attempts' / (spec.name + '-' + args.merge_sha + '.json')
                claim_attempt(intent, generation.proposal, checkout=checkout)
                result = apply_generation(checkout, root, hermes, spec, generation, review, output)
                write_json(output / 'RESULT.json', result)
                write_json(intent, {'state': 'succeeded' if result['success'] else 'failed',
                                    'result': str(output / 'RESULT.json'),
                                    'proposal_sha256': ready['proposal_sha256']})
                print(json.dumps(result, sort_keys=True))
                return 0 if result['success'] else 1
            write_json(output / 'READY-FOR-REVIEW.json', ready)
            print(json.dumps({'ready': str(output / 'READY-FOR-REVIEW.json'),
                              'proposal_sha256': ready['proposal_sha256']}, sort_keys=True))
            return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print('GLP_SHIP_REFUSED: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
