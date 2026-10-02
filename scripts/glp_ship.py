#!/usr/bin/env python3
"""Prepare or install one registered Git generation. See ops/glp-ship.md."""
import argparse
from contextlib import contextmanager, ExitStack
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
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


def claim_attempt(path, proposal):
    """Durable intent precedes the first runtime effect; no ambiguous replay."""
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
        for name in ('glp_ship.py', 'glp_ship_generation.py', 'glp_ship_registry.py',
                     'glp_ship_provenance.py', 'glp_ship_runtime.py',
                     'glp_ship_discovery.py', 'glp_ship_collector.py',
                     'glp44_scoped_adoption.py', 'glp73_paired_adoption.py',
                     'sync_from_repo.py', 'discovery_predecessor_migration.py',
                     'maintenance_window.py', 'maintenance_inventory.py',
                     'maintenance_cohort.py', 'maintenance_admission.py', 'maxbounty_pin_release.py'):
            if read_regular(Path(__file__).with_name(name))[0] != read_regular(candidate / 'scripts' / name)[0]:
                raise RuntimeError('ship_installer_not_exact_merge:' + name)
        yield candidate, proof


def preview(checkout, root, hermes, generation, window=None):
    if generation.kind == 'reporting':
        import glp44_scoped_adoption as adopter
        return adopter.plan(checkout, root, hermes, window=window, generation=generation)[0]
    if generation.kind == 'retention':
        import glp73_paired_adoption as adopter
        plan = adopter.plan(checkout, root, hermes, generation=generation, window=window)
        return adopter.retention_proposal(checkout, root, hermes, plan, generation=generation)
    from discovery_predecessor_migration import preview_generation
    return preview_generation(checkout, root, generation)


def prepare(checkout, root, hermes, spec, source, proof, prepared_by):
    if not str(prepared_by).strip():
        raise RuntimeError('preparer_identity_required')
    hashes = derive_pins(checkout, source, spec.paths)
    proposal = {'schema': 'glp-ship-proposal-v1', 'source': source,
                'kind': spec.kind, 'job_id': spec.name, 'tier': spec.tier,
                'paths': list(spec.paths), 'hashes': hashes, 'merge_proof': proof,
                'root': str(root), 'hermes': str(hermes), 'prepared_by': prepared_by,
                'maintenance_window_required': spec.window,
                'run_policy': {'entry': 'manual_registered', 'job_ids': list(spec.jobs),
                               'on_success': 'enable', 'on_failure': 'pause'}}
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


def zero_diff(checkout, root, hermes, generation, spec):
    # sync's success alone means conflict-free, not zero differences.
    if syncer.sync(root, checkout, False, only=generation.paths) != 0:
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
        for jid in sorted(spec.jobs):
            if not fences.enter_context(scheduler._fire_job_lock(jid)):
                raise RuntimeError('ship_native_fire_fence_unavailable:' + jid)
        return _apply_and_run(checkout, root, hermes, spec, generation, review, output,
                              journal, backend, scheduler)


def _apply_and_run(checkout, root, hermes, spec, generation, review, output, journal, backend, scheduler):
    from maintenance_window import MaintenanceWindow, default_locks, require_active_window
    from glp_ship_runtime import run_registered
    if spec.window:
        window = MaintenanceWindow(backend, journal, default_locks(root, hermes))
        window.enter()
        try:
            with require_active_window(journal, root, hermes) as active:
                preview(checkout, root, hermes, generation, active)
            if spec.kind == 'reporting':
                import glp44_scoped_adoption as adopter
            else:
                import glp73_paired_adoption as adopter
            result = adopter.adopt(checkout, root, hermes, review, scheduler=scheduler,
                                   window=journal, generation=generation)
            zero_diff(checkout, root, hermes, generation, spec)
        finally:
            # The existing exit refuses uncertain commits; never override it.
            window.exit()
        if window.status()['state'] != 'closed':
            raise RuntimeError('ship_window_not_restored')
    else:
        from glp_ship_discovery import apply
        result = apply(checkout, root, hermes, generation, review, output, scheduler)
        zero_diff(checkout, root, hermes, generation, spec)
    # Consumer locks belong to the maintenance window until exact restoration.
    # Native registered wrappers can acquire them only after that exit.
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
                claim_attempt(intent, generation.proposal)
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
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('GLP_SHIP_REFUSED: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
