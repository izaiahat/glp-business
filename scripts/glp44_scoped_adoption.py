#!/usr/bin/env python3
"""GLP-44 reviewed source/wrapper adoption; no job execution or schedule change.

Apply needs an independently accepted exact-preimage receipt and a verified
active maintenance window. A preview is read-only, not an apply authorization.
"""
import argparse
import copy
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import sync_from_repo as syncer
from maxbounty_pin_release import read_regular, preserve_custody

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

# One named successor, not a caller-supplied path/hash set or force migration.
PR119_GENERATION = 'pr119-reporting-20261001'
PR119_SOURCE = 'f5e4220b1e01e4b2ff96204aae4ac9a2424deccd'
# Literal declaration consumed by the shared static maintenance inventory.
# PATHS above remains the unchanged default PR71 cohort.
PR119_PATHS = (
    'ops/glp/reporting-v2/identity-registry.json',
    'scripts/glp_affiliate_weekly_reporting.py',
    'scripts/glp_affiliate_weekly_reporting_job.py',
    'ops/glp/reporting-v2/source-manifest.json',
    'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting.sh',
)
PR119_HASHES = {
    'ops/glp/reporting-v2/identity-registry.json': '93aa4fa83cba6ee24c81504fe0e58133c87014cb7bb618e1ff9140ab66b21271',
    'scripts/glp_affiliate_weekly_reporting.py': 'd3ac1c8aab8c484b41ff73d35e86569ff65611f199377a0becd372d23f345795',
    'scripts/glp_affiliate_weekly_reporting_job.py': '79e27c066307249971c5afc69dc2fd08dea76245f4267ecb5c084b16795ffd18',
    'ops/glp/reporting-v2/source-manifest.json': 'be4047627a84dd80efbff33b271a945c4a8e04da133bcfe8dc36bb69c200802f',
    SNAPSHOT: 'efe1cc68c12206a2df832148e468c5418edafec473b45d2e60df18aa180aa25d',
}
PR119_BEFORE = {
    'ops/glp/reporting-v2/identity-registry.json': '70f8139656cca7bcf834e447e142aa69bb2f8add7b5f28fd0d23a2f393688638',
    'scripts/glp_affiliate_weekly_reporting.py': 'c8a0503f3e092a162ca027403974235d14e7c800523bde15792de10d2243feb9',
    'scripts/glp_affiliate_weekly_reporting_job.py': 'ae7cba876c14fd52594a4827d2c9289e264080bd9d72fb355b4dab64b48e57d8',
    'ops/glp/reporting-v2/source-manifest.json': '34f39b915a78e9ce188777f9c33bd30e9edab147ceba7df26328bae75dba54bb',
    SNAPSHOT: '197d2f094203bb1f464ac558aac14376093b9311b816245fbf47d0a97b895d83',
}
PR119_PREREQUISITES = {
    'systems/analytics-brain/analytics_brain.py': '19ffa95c62340280cdf6f3d02f45ac5933392a52e66a59532b6e09ba7015bdaa',
    'systems/analytics-brain/collector.py': 'af1d8f9187384f025bcd042a17710581fabb1870fd5bf05c8dcff174f2a4203e',
    'ops/glp/analytics-brain-control.json': '68f8146b37f8c27f38f2822e8778972d4499cf0ec66acda1b816db21bb4ed815',
}


# Supported publication successor for the reviewed hourly collector (PR142 +
# PR146, merged at COLLECTOR_SOURCE). PR71 stays the immutable old-collector
# cohort; this generation publishes the same PR71 members plus the PR119
# reporting members from one committed tree, so the staged wrapper/manifest
# and the reporting pin closure all bind the new collector generation. The
# union is already declared above (PATHS/PR119_PATHS) for the static inventory.
COLLECTOR_GENERATION = 'glp44-collector-successor-20261002'
COLLECTOR_SOURCE = '488eeffd8a01f2fdb2fb19abf95636ad33829851'
COLLECTOR_HASHES = {
    'ops/glp/analytics-brain-control.json': '68f8146b37f8c27f38f2822e8778972d4499cf0ec66acda1b816db21bb4ed815',
    'ops/glp/reporting-v2/source-manifest.json': 'be4047627a84dd80efbff33b271a945c4a8e04da133bcfe8dc36bb69c200802f',
    SNAPSHOT: 'efe1cc68c12206a2df832148e468c5418edafec473b45d2e60df18aa180aa25d',
    'systems/analytics-brain/analytics_brain.py': '19ffa95c62340280cdf6f3d02f45ac5933392a52e66a59532b6e09ba7015bdaa',
    'systems/analytics-brain/collector.py': 'af1d8f9187384f025bcd042a17710581fabb1870fd5bf05c8dcff174f2a4203e',
    'ops/glp/reporting-v2/identity-registry.json': '93aa4fa83cba6ee24c81504fe0e58133c87014cb7bb618e1ff9140ab66b21271',
    'scripts/glp_affiliate_weekly_reporting.py': 'd3ac1c8aab8c484b41ff73d35e86569ff65611f199377a0becd372d23f345795',
    'scripts/glp_affiliate_weekly_reporting_job.py': '79e27c066307249971c5afc69dc2fd08dea76245f4267ecb5c084b16795ffd18',
}
COLLECTOR_PATHS = PATHS + tuple(p for p in PR119_PATHS if p not in PATHS)
# Reporting generations that rebind the job hash inside the maintained switch.
SUCCESSORS = {
    PR119_GENERATION: (PR119_HASHES, 'glp333-reporting-runtime-review-v1'),
    COLLECTOR_GENERATION: (COLLECTOR_HASHES, 'glp44-collector-successor-runtime-review-v1'),
}


# Dependency installation only: no carrier or job-pin authority. Apply before
# producing the PR71 preview in the same window (the full state preimage moves).
GLP331_GENERATION = 'glp331-dependencies-20261001'
GLP331_SOURCE = '0b61c25a7177896369f4fa76b4e84192de6bee70'
GLP331_PATHS = (
    'scripts/glp_affiliate_wire_backlog_worker.py',
    'scripts/glp_go_runtime_manifest_refresh.mjs',
)
GLP331_BEFORE = {
    'scripts/glp_affiliate_wire_backlog_worker.py': 'b313063f54848dbf4e7e8ff8b4dbf2bf697ff1ccb7245837d950b61c3ed255a0',
    'scripts/glp_go_runtime_manifest_refresh.mjs': '35f99538dd648fe21b781766a0a81952ad222265e0aec552b42dda5563eaccbe',
}
GLP331_HASHES = {
    'scripts/glp_affiliate_wire_backlog_worker.py': '59e25a8c605f6c0c2ae284edfb3dae99eb7354c2679146fb787a923a4544ef05',
    'scripts/glp_go_runtime_manifest_refresh.mjs': 'a2309d7e02b80f204b40011f0009cb7fcabaf046903dd09cfab085ccecd45e1e',
}


# Fixed PR132 launcher/cadence installation only. Inventory pins and native
# carriers are retained inputs, not members of this source-only generation.
GLP337_GENERATION = 'glp337-shared-bridge-20261001'
GLP337_SOURCE = '898b45e903dffcce4046aa5548fc3e6f23b60b78'
GLP337_PATHS = (
    'scripts/cron_bridge_action.py',
    'scripts/glp_analytics_brain_cadence.py',
)
GLP337_BEFORE = {
    'scripts/cron_bridge_action.py': 'f00f66759a9bdc3a0f3a4156c5fbe8a6acc73597f5b83732e818b9cc7dc1ec55',
    'scripts/glp_analytics_brain_cadence.py': '61ca597689080f6f3e5de9b00c733009df258850f58bce7f592843e527f17c53',
}
GLP337_HASHES = {
    'scripts/cron_bridge_action.py': 'f1055dea298e8354bd61d9ccad084a2b33ab9c028ed7070b54e23d64ad023032',
    'scripts/glp_analytics_brain_cadence.py': '4d51a627d7a5208238ab7ce7622772c118d9b141497942bf9ed8bd6d4587bcb2',
}
GLP337_CONTEXT = (
    'ops/scheduled-source-drift-manifest.json',
    'ops/n8n-bridge/actions.json',
    'ops/n8n-bridge/bridge.py',
)


def generation_contract(generation):
    if generation == GLP337_GENERATION:
        return GLP337_SOURCE, GLP337_PATHS
    if generation == GLP331_GENERATION:
        return GLP331_SOURCE, GLP331_PATHS
    if generation == 'pr71':
        return APPROVED, PATHS
    if generation == PR119_GENERATION:
        return PR119_SOURCE, PR119_PATHS
    if generation == COLLECTOR_GENERATION:
        return COLLECTOR_SOURCE, COLLECTOR_PATHS
    raise RuntimeError('unknown_reporting_generation')


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


@contextmanager
def candidate_checkout(checkout, generation):
    """Materialize PR71/GLP331/GLP337 immutable trees without moving HEAD.

    Keep the frozen caller HEAD for reviewed_prior_hashes: retained predecessor
    curation can postdate PR71. The index/files/global manifest are all APPROVED,
    so the unchanged scoped importer still validates a complete committed tree.
    PR119 retains its reviewed-current-head producer and source checks.
    """
    approved, _ = generation_contract(generation)
    checkout = checkout.resolve(strict=True)
    if generation not in ('pr71', GLP331_GENERATION, GLP337_GENERATION):
        yield checkout
        return
    head = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD']).decode().strip()
    subprocess.run(['git', '-C', str(checkout), 'merge-base', '--is-ancestor', approved, head], check=True)
    with tempfile.TemporaryDirectory(prefix='glp44-pr71-candidate-') as temp:
        candidate = Path(temp) / 'repo'
        subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(checkout), str(candidate)], check=True)
        subprocess.run(['git', '-C', str(candidate), 'update-ref', 'HEAD', head], check=True)
        subprocess.run(['git', '-C', str(candidate), 'read-tree', approved], check=True)
        subprocess.run(['git', '-C', str(candidate), 'checkout-index', '--all'], check=True)
        yield candidate


def plan(checkout, root, hermes, review=None, *, window=None, generation='pr71'):
    with candidate_checkout(checkout, generation) as candidate:
        return _plan(candidate, root, hermes, review, window=window, generation=generation)


def _plan_dependencies(checkout, root, hermes, review, window, generation=GLP331_GENERATION):
    """Read-only fixed source proposal; the importer is never used to apply."""
    approved, paths = generation_contract(generation)
    shared = generation == GLP337_GENERATION
    hashes = GLP337_HASHES if shared else GLP331_HASHES
    before_hashes = GLP337_BEFORE if shared else GLP331_BEFORE
    runtime_schema = ('glp337-shared-bridge-runtime-review-v1' if shared
                      else 'glp331-dependency-runtime-review-v1')
    if window is None:
        raise RuntimeError('active_maintenance_window_required')
    from maintenance_window import read_journal, MaintenanceWindow, default_locks
    root, hermes = root.resolve(strict=True), hermes.resolve(strict=True)
    journal = read_journal(window.journal)
    if not MaintenanceWindow(window.backend, window.journal, default_locks(root, hermes)).status()['verified']:
        raise RuntimeError('active_maintenance_window_required')
    if journal['state'] != 'active' or journal['root'] != str(root) or journal['hermes'] != str(hermes):
        raise RuntimeError('active_maintenance_window_required')
    # Producer is reviewed HEAD; candidate files/index/manifest are exclusively
    # the generation's immutable approved tree, not caller-worktree bytes.
    head = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD']).decode().strip()
    producer = subprocess.check_output(['git', '-C', str(checkout), 'show', 'HEAD:scripts/glp44_scoped_adoption.py'])
    if Path(__file__).read_bytes() != producer:
        raise RuntimeError('producer_bytes_not_reviewed_head')
    for rel, expected in hashes.items():
        committed = subprocess.check_output(['git', '-C', str(checkout), 'show', approved + ':' + rel])
        if digest(committed) != expected or read_regular(checkout / rel)[0] != committed:
            raise RuntimeError('approved_source_moved:' + rel)
    before = {rel: read_regular(root / rel) for rel in paths}
    for rel, value in before.items():
        if digest(value[0]) != before_hashes[rel] or value[1] != 0o644:
            raise RuntimeError('dependency_generation_predecessor_refused:' + rel)
    state = read_regular(root / syncer.STATE)
    baselines = json.loads(state[0]).get('hashes', {})
    adopted = tuple(rel for rel in paths if rel not in baselines)
    # GLP337 reuses PR132's fixed private no-apply importer handoff. Ordinary
    # sync still refuses the authentic launcher literal edit. No baseline healing.
    if shared:
        with syncer.source_lock(root):
            result = syncer._sync_locked(root, checkout, False, adopted, paths,
                                         _shared_bridge_preimage=GLP337_BEFORE[GLP337_PATHS[0]])
    else:
        result = syncer.sync(root, checkout, False, adopted, paths)
    if result != 0:
        raise RuntimeError('scoped_source_preflight_refused')
    state_after = json.loads(state[0])
    state_after['hashes'].update(hashes)
    state_body = (json.dumps(state_after, indent=2, sort_keys=True) + '\n').encode()
    registry = hermes / 'cron/jobs.json'
    registry_before = read_regular(registry)
    global_manifest = read_regular(root / syncer.MANIFEST)
    retained_context = {rel: read_regular(root / rel) for rel in GLP337_CONTEXT} if shared else {}
    # adopter is ephemeral context ownership; everything else binds the same
    # lease, paused cohort, prior transactions and exact source baselines.
    journal.pop('adopter', None)
    proposal = {
        'generation': generation, 'approved_source': approved,
        'producer_head': head, 'producer_sha256': digest(producer),
        'candidate_source_manifest_sha256': digest(read_regular(checkout / syncer.MANIFEST)[0]),
        'source_sha256': dict(hashes), 'source_before_sha256': dict(before_hashes),
        'source_before_modes': {rel: value[1] for rel, value in before.items()},
        'source_after_modes': {rel: value[1] for rel, value in before.items()},
        'state_before_sha256': digest(state[0]), 'state_after_sha256': digest(state_body),
        'state_before_mode': state[1], 'state_after_mode': state[1],
        'state_delta': dict(hashes), 'adopted_baselines': list(adopted),
        'installed_source_manifest_sha256': digest(global_manifest[0]),
        'installed_source_manifest_mode': global_manifest[1],
        'registry_before_sha256': digest(registry_before[0]), 'registry_before_mode': registry_before[1],
        'target': {'root': str(root), 'hermes': str(hermes), 'journal': str(Path(window.journal).resolve()),
                   'registry': str(registry.resolve())},
        'window_before_sha256': digest(json.dumps(journal, sort_keys=True).encode()),
    }
    if shared:
        proposal['retained_context'] = {rel: {'sha256': digest(value[0]), 'mode': value[1]}
                                        for rel, value in retained_context.items()}
    if review is not None:
        for key, value in proposal.items():
            if review.get(key) != value:
                raise RuntimeError('reviewed_preimage_or_candidate_moved:' + key)
        preparer = str(review.get('prepared_by') or '').strip()
        reviewer = str(review.get('reviewer') or '').strip()
        runtime = review.get('runtime_review', {})
        if (review.get('schema') != 'glp44-scoped-adoption-review-v1'
                or review.get('decision') != 'ACCEPT_EXACT_BYTES' or not preparer
                or not reviewer or reviewer == preparer
                or runtime.get('schema') != runtime_schema
                or runtime.get('decision') != 'ACCEPT_EXACT_BYTES'
                or not str(runtime.get('reviewer') or '').strip()
                or str(runtime['reviewer']).strip() == preparer
                or runtime.get('proposal_sha256') != digest(json.dumps(proposal, sort_keys=True).encode())):
            raise RuntimeError('independent_matching_runtime_review_required')
    return proposal, before, state, registry


def _adopt_dependencies(checkout, root, hermes, review, scheduler, window, generation=GLP331_GENERATION):
    approved, paths = generation_contract(generation)
    hashes = GLP337_HASHES if generation == GLP337_GENERATION else GLP331_HASHES
    if review is None:
        raise RuntimeError('independent_review_required')
    proposal, before, state, registry = _plan_dependencies(checkout, root, hermes, review, window, generation)
    if scheduler._current_cron_store().jobs_file != registry or scheduler._jobs_lock_file() != registry.parent / '.jobs.lock':
        raise RuntimeError('wrong_scheduler_store')
    from maintenance_window import read_journal
    job_ids = sorted(row['entry']['id'] for row in read_journal(window.journal)['entries']
                     if row['entry']['kind'] == 'hermes')
    with ExitStack() as fences:
        for jid in job_ids:
            if not fences.enter_context(scheduler._fire_job_lock(jid)):
                raise RuntimeError('dependency_fire_fence_unavailable')
        fences.enter_context(scheduler._jobs_file_lock)
        fd = os.open(scheduler._jobs_lock_file(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        lock = fences.enter_context(os.fdopen(fd, 'a+'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fresh, _, _, _ = _plan_dependencies(checkout, root, hermes, review, window, generation)
        if fresh != proposal:
            raise RuntimeError('preimage_moved_under_lock')
        # Serialize maintained imports throughout custody/publication. Previously
        # loaded importers and uncooperative writers still require owner exclusion.
        fences.enter_context(syncer.source_lock(root))
        original = {root / rel: value for rel, value in before.items()}
        original[root / syncer.STATE] = state
        retained = {registry: read_regular(registry), root / syncer.MANIFEST: read_regular(root / syncer.MANIFEST)}
        for rel, expected in proposal.get('retained_context', {}).items():
            value = read_regular(root / rel)
            if digest(value[0]) != expected['sha256'] or value[1] != expected['mode']:
                raise RuntimeError('dependency_retained_preimage_moved:' + rel)
            retained[root / rel] = value
        if (digest(retained[registry][0]) != proposal['registry_before_sha256']
                or retained[registry][1] != proposal['registry_before_mode']
                or digest(retained[root / syncer.MANIFEST][0]) != proposal['installed_source_manifest_sha256']
                or retained[root / syncer.MANIFEST][1] != proposal['installed_source_manifest_mode']):
            raise RuntimeError('dependency_retained_preimage_moved')
        stage = {root / rel: read_regular(checkout / rel)[0] for rel in paths}
        state_data = json.loads(state[0]); state_data['hashes'].update(hashes)
        stage[root / syncer.STATE] = (json.dumps(state_data, indent=2, sort_keys=True) + '\n').encode()
        if ({rel: digest(stage[root / rel]) for rel in paths} != proposal['source_sha256']
                or digest(stage[root / syncer.STATE]) != proposal['state_after_sha256']):
            raise RuntimeError('reviewed_candidate_moved_before_switch')
        custody = root / 'reports/ops/pin-archive/glp44' / digest(json.dumps(review, sort_keys=True).encode())
        preserve_custody(custody, {**original, **retained}, stage)
        if any(observed(path) != value for path, value in {**original, **retained}.items()):
            raise RuntimeError('preimage_moved_after_custody')
        window.accept_file_updates({str(p.resolve()): digest(value[0]) for p, value in original.items()},
                                   {str(p.resolve()): digest(body) for p, body in stage.items()})
        # Source-only: no callback, no accept_job_update, no scheduler saver.
        # The maintained switch handles exact inverse restoration and seals an
        # uncertain/partial outcome; require_active_window.finish certifies it.
        window.switch_files(stage, original)
        if any(observed(path) != value for path, value in retained.items()):
            raise RuntimeError('dependency_retained_preimage_moved_no_retry')
    return {'applied': True, 'source': approved, 'proposal': proposal, 'custody': str(custody)}


def _plan(checkout, root, hermes, review=None, *, window=None, generation='pr71'):
    if generation in (GLP331_GENERATION, GLP337_GENERATION):
        return _plan_dependencies(checkout, root, hermes, review, window, generation)
    approved, paths = generation_contract(generation)
    successor = generation in SUCCESSORS
    successor_hashes, runtime_schema = SUCCESSORS.get(generation, (None, None))
    checkout = checkout.resolve(strict=True)
    root = root.resolve(strict=True)
    hermes = hermes.resolve(strict=True)
    subprocess.run(['git', '-C', str(checkout), 'merge-base', '--is-ancestor', approved, 'HEAD'], check=True)
    for rel in paths:
        committed = subprocess.check_output(['git', '-C', str(checkout), 'show', f'{approved}:{rel}'])
        if (checkout / rel).read_bytes() != committed or (successor and digest(committed) != successor_hashes[rel]):
            raise RuntimeError('approved_source_moved:' + rel)
    if successor:
        producer_head = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD']).decode().strip()
        producer = 'scripts/glp44_scoped_adoption.py'
        committed = subprocess.check_output(['git', '-C', str(checkout), 'show', 'HEAD:' + producer])
        if read_regular(checkout / producer)[0] != committed or Path(__file__).read_bytes() != committed:
            raise RuntimeError('producer_bytes_not_reviewed_head')
    if generation == PR119_GENERATION:
        baselines = json.loads((root / syncer.STATE).read_text()).get('hashes', {})
        for rel, expected in PR119_PREREQUISITES.items():
            if digest(read_regular(root / rel)[0]) != expected or baselines.get(rel) != expected:
                raise RuntimeError('glp44_prerequisite_not_adopted:' + rel)
        for rel, expected in PR119_BEFORE.items():
            if digest(read_regular(root / rel)[0]) != expected:
                raise RuntimeError('reporting_generation_predecessor_refused:' + rel)
    adopted = tuple(p for p in paths if p not in json.loads((root / syncer.STATE).read_text()).get('hashes', {}))
    # An absent destination does not need an adoption flag; reviewed_prior_hashes
    # is only for preexisting, untracked-baseline files.
    adopted = tuple(p for p in adopted if (root / p).exists())
    if syncer.sync(root, checkout, False, adopted, paths) != 0:
        raise RuntimeError('scoped_source_preflight_refused')
    before = {p: observed(root / p) for p in paths}
    state = observed(root / syncer.STATE)
    snapshot = (checkout / SNAPSHOT).read_bytes()
    wrapper = hermes / 'scripts' / WRAPPER
    wrapper_before = read_regular(wrapper)
    if wrapper_before[0] != before[SNAPSHOT][0]:
        raise RuntimeError('installed_wrapper_not_curated_predecessor')
    registry = hermes / 'cron' / 'jobs.json'
    current_job, _ = job_record(registry)
    job = window.original_job(JOB) if window is not None else current_job
    if window is not None:
        if (current_job != window.current_job(JOB)
                or current_job.get('enabled') is not False or current_job.get('state') != 'paused'
                or current_job.get('fire_claim') or current_job.get('run_claim') or current_job.get('running')):
            raise RuntimeError('maintenance_reporting_job_moved_or_not_drained')
    if (job.get('enabled') is not True or job.get('state') != 'scheduled'
            or job.get('script') not in (WRAPPER, str(wrapper))
            or job.get('script_sha256') != digest(wrapper_before[0])
            or current_job.get('fire_claim') or current_job.get('run_claim') or current_job.get('running')):
        raise RuntimeError('reporting_job_not_idle_or_preimage_moved')
    pins = json.loads((checkout / 'ops/glp/reporting-v2/source-manifest.json').read_text())['files']
    retained = ({rel: read_regular(root / rel) for rel in PR119_PREREQUISITES}
                if generation == PR119_GENERATION else {})
    for absolute, expected in pins.items():
        path = Path(absolute)
        try:
            rel = path.relative_to(syncer.DEFAULT_ROOT)
        except ValueError:
            raise RuntimeError('unexpected_reporting_pin_path:' + absolute)
        if successor:
            value = read_regular((checkout if rel.as_posix() in paths else root) / rel)
            actual = value[0]
            if rel.as_posix() not in paths:
                retained[rel.as_posix()] = value
        elif rel.as_posix() == 'systems/analytics-brain/analytics_brain.py':
            actual = (checkout / rel).read_bytes()
        else:
            actual = (root / rel).read_bytes()  # host-only leaves must already match
        if digest(actual) != expected:
            raise RuntimeError('reporting_pin_closure_refused:' + str(path))
    proposal = {'source_sha256': {p: digest((checkout / p).read_bytes()) for p in paths},
                'wrapper_sha256': digest(snapshot), 'job_id': JOB,
                'job_before': job, 'job_window_before': current_job, 'source_before_sha256': {
                    p: digest(value[0]) if value[0] is not None else None for p, value in before.items()},
                'wrapper_before_sha256': digest(wrapper_before[0]),
                'state_before_sha256': digest(state[0]) if state[0] is not None else None}
    if successor:
        registry_body, registry_mode = read_regular(registry)
        proposal.update({
            'generation': generation, 'producer_head': producer_head,
            'producer_sha256': digest(committed),
            'producer_source_manifest_sha256': digest((checkout / syncer.MANIFEST).read_bytes()),
            'target': {'root': str(root), 'hermes': str(hermes),
                       'registry': str(registry.resolve()),
                       'journal': str(Path(window.journal).resolve()) if window is not None else None},
            'source_before_modes': {p: value[1] for p, value in before.items()},
            'source_candidate_modes': {p: read_regular(checkout / p)[1] for p in paths},
            'state_before_mode': state[1], 'wrapper_before_mode': wrapper_before[1],
            'registry_before_sha256': digest(registry_body), 'registry_before_mode': registry_mode,
            'retained_source': {p: {'sha256': digest(value[0]), 'mode': value[1]}
                                for p, value in retained.items()},
        })
    if review is not None:
        for key in proposal:
            if review.get(key) != proposal[key]:
                raise RuntimeError('reviewed_preimage_or_candidate_moved:' + key)
        if (review.get('schema') != 'glp44-scoped-adoption-review-v1'
                or review.get('approved_source') != approved
                or review.get('decision') != 'ACCEPT_EXACT_BYTES'
                or not review.get('reviewer')):
            raise RuntimeError('independent_review_required')
        if successor:
            runtime = review.get('runtime_review', {})
            preparer = str(review.get('prepared_by') or '').strip()
            if (not preparer or not str(review['reviewer']).strip()
                    or str(review['reviewer']).strip() == preparer
                    or runtime.get('schema') != runtime_schema
                    or runtime.get('decision') != 'ACCEPT_EXACT_BYTES'
                    or not str(runtime.get('reviewer') or '').strip()
                    or str(runtime['reviewer']).strip() == preparer
                    or runtime.get('proposal_sha256') != digest(json.dumps(proposal, sort_keys=True).encode())):
                raise RuntimeError('independent_matching_runtime_review_required')
    return proposal, adopted, before, state, wrapper_before, registry


def adopt(checkout, root, hermes, review, *, scheduler, window=None, generation='pr71'):
    if window is None:
        raise RuntimeError('active_maintenance_window_required')
    from maintenance_window import require_active_window
    with require_active_window(window, root, hermes) as active:
        return _adopt_in_window(checkout, root, hermes, review, scheduler=scheduler, window=active, generation=generation)


def _adopt_in_window(checkout, root, hermes, review, *, scheduler, window, generation='pr71'):
    with candidate_checkout(checkout, generation) as candidate:
        return _adopt_candidate_in_window(candidate, root, hermes, review, scheduler=scheduler,
                                          window=window, generation=generation)


def _adopt_candidate_in_window(checkout, root, hermes, review, *, scheduler, window, generation='pr71'):
    if generation in (GLP331_GENERATION, GLP337_GENERATION):
        return _adopt_dependencies(checkout, root, hermes, review, scheduler, window, generation)
    approved, paths = generation_contract(generation)
    successor = generation in SUCCESSORS
    proposal, adopted, before, state, wrapper_before, registry = _plan(checkout, root, hermes, review, window=window, generation=generation)
    if scheduler._current_cron_store().jobs_file != registry or scheduler._jobs_lock_file() != registry.parent / '.jobs.lock':
        raise RuntimeError('wrong_scheduler_store')
    wrapper = hermes / 'scripts' / WRAPPER
    original = {root / p: value for p, value in before.items()}
    original[root / syncer.STATE] = state
    original[wrapper] = wrapper_before
    with scheduler._fire_job_lock(JOB) as fenced, scheduler._jobs_file_lock:
        if not fenced:
            raise RuntimeError('reporting_fire_fence_unavailable')
        fd = os.open(scheduler._jobs_lock_file(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Revalidate every source, baseline, wrapper and full job under the lock.
            fresh, _, _, _, _, _ = _plan(checkout, root, hermes, review, window=window, generation=generation)
            if fresh != proposal:
                raise RuntimeError('preimage_moved_under_lock')
            registry_before = registry.read_bytes()
            if any(body is None for body, _ in original.values()):
                raise RuntimeError('missing_predecessor_requires_separate_custody')
            stage = {root / p: (checkout / p).read_bytes() for p in paths}
            state_data = json.loads(state[0])
            state_data['hashes'].update(proposal['source_sha256'])
            stage[root / syncer.STATE] = (json.dumps(state_data, indent=2, sort_keys=True) + '\n').encode()
            stage[wrapper] = (checkout / SNAPSHOT).read_bytes()
            if (any(digest(stage[root / p]) != proposal['source_sha256'][p] for p in paths)
                    or digest(stage[wrapper]) != proposal['wrapper_sha256']):
                raise RuntimeError('reviewed_candidate_moved_before_switch')
            custody = root / 'reports/ops/pin-archive/glp44' / digest(json.dumps(review, sort_keys=True).encode())
            retained = {root / p: read_regular(root / p) for p in proposal.get('retained_source', {})} if successor else {}
            preserve_custody(custody, {**original, **retained, registry: read_regular(registry)}, stage)
            # Custody is not an exclusion lock; reject late edits before first source write.
            if any(observed(path) != value for path, value in original.items()) or registry.read_bytes() != registry_before:
                raise RuntimeError('preimage_moved_after_custody')
            if successor:
                _plan(checkout, root, hermes, review, window=window, generation=generation)
            window.accept_file_updates(
                {str(path.resolve()): digest(original[path][0]) for path in stage},
                {str(path.resolve()): digest(body) for path, body in stage.items()},
            )
            if successor:
                job_after = copy.deepcopy(proposal['job_window_before'])
                job_after['script_sha256'] = proposal['wrapper_sha256']
                # Journal the hash intent before the maintained switch captures
                # its receipt; the commit callback only saves/readbacks registry.
                window.accept_job_update(JOB, proposal['job_window_before'], job_after)
            source_written = False
            commit_entered = False
            def commit():
                nonlocal commit_entered
                commit_entered = True
                current, jobs = job_record(registry)
                if current != proposal['job_window_before']:
                    raise RuntimeError('job_moved_before_hash_update')
                if successor and (read_regular(registry) != (registry_before, proposal['registry_before_mode'])
                                  or any(observed(path) != value for path, value in retained.items())):
                    raise RuntimeError('reporting_generation_cas_moved_before_native_save')
                next_jobs = copy.deepcopy(jobs)
                next(j for j in next_jobs if j['id'] == JOB)['script_sha256'] = proposal['wrapper_sha256']
                if not successor:
                    window.accept_job_update(JOB, current, next(j for j in next_jobs if j['id'] == JOB))
                scheduler._record_load_stamp(None)
                scheduler._save_jobs_unlocked(next_jobs)
                actual, actual_jobs = job_record(registry)
                if actual_jobs != next_jobs or actual['script_sha256'] != proposal['wrapper_sha256']:
                    raise RuntimeError('scheduler_readback_mismatch_no_retry')
            try:
                source_written = True  # the switch can publish before acknowledgement fails
                if successor:
                    # Reuse the maintained F09 seal/intent/save/readback boundary.
                    window.switch_files(stage, original, commit=commit)
                else:
                    # Preserve the original PR71 source-only switch contract.
                    window.switch_files(stage, original)
                    commit()
            except Exception:
                if successor and commit_entered:
                    # Durable intent already exists: keep seals/custody even if
                    # registry bytes look unchanged. No automatic inverse/replay.
                    raise
                # A possibly landed scheduler write is held for reconciliation, never
                # blindly reversed or replayed. Only restore files while registry is unchanged.
                current, _ = job_record(registry)
                if registry.read_bytes() == registry_before and source_written:
                    current_files = {path: observed(path) for path in stage}
                    staged_files = {path: (body, original[path][1]) for path, body in stage.items()}
                    if current_files != original:
                        if current_files != staged_files:
                            raise RuntimeError('rollback_conflict_no_overwrite:source_cohort')
                        window.switch_files({path: value[0] for path, value in original.items()}, staged_files)
                raise
    return {'applied': True, 'source': approved, 'proposal': proposal, 'custody': str(custody)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', choices=('pr71', PR119_GENERATION, COLLECTOR_GENERATION, GLP331_GENERATION, GLP337_GENERATION),
                        default='pr71',
                        help='exact approved cohort; GLP331/GLP337 source-only installs invalidate earlier previews; '
                             'PR119 requires completed GLP44 adoption; the collector successor publishes the '
                             'PR71 and PR119 members with the reviewed hourly collector')
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=syncer.DEFAULT_ROOT)
    parser.add_argument('--hermes', type=Path, default=Path('/home/ubuntu/.hermes'))
    parser.add_argument('--review', type=Path)
    parser.add_argument('--window', type=Path, help='active maintenance journal; required for apply')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        review = json.loads(args.review.read_text()) if args.review else None
        if args.apply:
            if args.window is None:
                raise RuntimeError('active_maintenance_window_required')
            if review is None:
                raise RuntimeError('independent_review_required')
            sys.path.insert(0, str(args.hermes / 'hermes-agent'))
            from cron import jobs as scheduler
            result = adopt(args.checkout, args.root, args.hermes, review, scheduler=scheduler, window=args.window, generation=args.generation)
        else:
            from contextlib import nullcontext
            from maintenance_window import require_active_window
            context = require_active_window(args.window, args.root, args.hermes) if args.window else nullcontext(None)
            with context as active:
                result = {'applied': False, 'proposal': plan(args.checkout, args.root, args.hermes, review, window=active, generation=args.generation)[0]}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print('GLP44_ADOPTION_REFUSED: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
