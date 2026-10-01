#!/usr/bin/env python3
"""Fixed GLP73 cohorts, including the PR136 archiver / two-paused-job successor.

Apply requires a verified active maintenance window and exact independent review.
Complete source cohorts switch through the window admission barrier; exact
preimages and uncertain scheduler outcomes remain guarded.
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
from maxbounty_pin_release import preserve_custody, read_regular

SOURCE = '209c3beea6e2045dcb81367d5339a23a225eda40'
PATHS = (
    'scripts/glp_current_content_base_schema.py', 'scripts/glp_price_index_weekly.py',
    'systems/seo-blog-machine/scripts/current_content_base.py',
    'scripts/glp_deploy_completeness_sensor.py', 'scripts/glp_deploy_sensor_hook.py',
    'systems/seo-blog-machine/scripts/article_images.py',
    'systems/seo-blog-machine/scripts/build_opus_article_release.py',
    'systems/seo-blog-machine/scripts/carrier_fixture_contracts.py',
    'systems/seo-blog-machine/scripts/review_transports.py', 'ops/glp/seo_business_context.py',
    'ops/glp/seo_data_rails.py', 'scripts/glp_price_index_weekly_successor.py',
    'scripts/glp_price_index_recovery.py', 'scripts/reports_archiver.py',
)
ADOPT = (
    'scripts/glp_current_content_base_schema.py', 'scripts/glp_price_index_weekly.py',
    'systems/seo-blog-machine/scripts/current_content_base.py',
    'scripts/glp_deploy_completeness_sensor.py', 'scripts/glp_deploy_sensor_hook.py',
    'scripts/glp_price_index_weekly_successor.py', 'scripts/reports_archiver.py',
)
# Source preimages and modes observed in the retained STOP preflight. No absence
# or arbitrary reviewed-baseline adoption is admitted by this fixed entry.
SOURCE_BEFORE = (
    ('a2a65183b8f501213e9f03730a64101e141c79c43035009a7bf7b28c0a676e46', 0o644),
    ('efe4185455b03c141bb289355318f95654442cf7ecc6bf77e8971c5e8086a9f4', 0o700),
    ('841e7f4330f6ee435d3b0f1ab551cf789bc2f0b3e5cf47deca4190c2ca98588c', 0o600),
    ('953c98503178f511b571bf87b79ab35a95815c27561f8a94700a73f951b52a4c', 0o644),
    ('88471ac7b8073a48d91dda3a66cac4320d99ab98b2d1112f45e6a569828060c4', 0o644),
    ('792d813f58d3da108905c617be03bcdf02547d8fcf03d2efa0bb53ab79196d8d', 0o644),
    ('54de35c3bb622127b584dbafb4f511ff8cc6dd1d8f83bced34cfc8bb830cd82a', 0o644),
    ('1dc7a08b80e25e7448843cb2c2e2ae077abd67402e4936428502c57114d1d054', 0o644),
    ('07f5afe93e2467fe4bc6a117c323b3399f1faff134274a4e842cfa8007fe39c4', 0o644),
    ('def00df050f44bdc90305a9a3bc412af7a8f44e7cf0c70a78fef0ac2abf724b8', 0o644),
    ('e82495acdba978c9211339f81b2169c144e90fb6808f00f186d801e0d70fc333', 0o644),
    ('d70510d518021e1da9f5a8987b2b11ff0942425920aaaa5c036d3879fb91f241', 0o644),
    ('20d84ddc3f27eb18aba86de11d80177ca66ad46b5059ca3ed816beac04300ef9', 0o644),
    ('ef09a4ab7b1cb4119340c233e07422a3702cc9bcef0583371fc316823a6462cf', 0o600),
)
STATE_BEFORE = '20a6396a78a4cb4cf82e473b0b3805b01ed279cd2e6526c2032caa10814e7436'
SNAPSHOTS = 'ops/hermes-cron-scripts/glp73-predecessors'
# Only these line/path targets change; all other wrapper bytes are retained.
WEEKLY = {
    16: 'scripts/glp_price_index_weekly_successor.py',
    20: 'systems/seo-blog-machine/scripts/build_opus_article_release.py',
    28: 'ops/claude_model_policy.py', 30: 'ops/glp/seo_business_context.py',
    31: 'ops/glp/seo_data_rails.py', 33: 'scripts/glp_current_content_base_schema.py',
    34: 'scripts/glp_deploy_completeness_sensor.py', 35: 'scripts/glp_deploy_sensor_hook.py',
    39: 'scripts/glp_price_index_weekly.py', 40: 'scripts/glp_price_index_weekly_successor.py',
    47: 'systems/seo-blog-machine/scripts/article_image_release_contract.py',
    48: 'systems/seo-blog-machine/scripts/article_images.py',
    49: 'systems/seo-blog-machine/scripts/build_opus_article_release.py',
    50: 'systems/seo-blog-machine/scripts/carrier_fixture_contracts.py',
    52: 'systems/seo-blog-machine/scripts/current_content_base.py',
    56: 'systems/seo-blog-machine/scripts/gpt_image_2_native.py',
    62: 'systems/seo-blog-machine/scripts/review_transports.py',
}
# These are already accepted retained installed leaves, not imported sources.
RETAINED = {
    'ops/claude_model_policy.py': '0cb92558f8a88ccd3fa76d7f954d8eda1eff634d1825db17f424c6c039f7f011',
    'systems/seo-blog-machine/scripts/article_image_release_contract.py': '346631d530a2989fc28bc9880b5ddc9d717b5bb2cb20df0a4d19c25c63546ce2',
    'systems/seo-blog-machine/scripts/gpt_image_2_native.py': 'b8006272a9928290a1eb625956d3f67bb933cf08c6a7ff15bc3b6d77cdcbd46d',
}
JOBS = (
    ('567f20134cc2', 'glp_price_index_weekly.sh', '5e7e4c67ed1d4a514a7776403eb7a7f87450740bb6074b22a81cf2c943b69522', '04208af727b6beeaa9117179e8f5c1a4d2cb3246ccfbbbbcb6f26c528337fcb8', 0o700),
    ('f3d2db07d83f', 'f12_generator_archiver.py', 'e1bdddb158f7c5d66e325c12099a4ad0a90d53ecb27b513ecf1b7f8fd9c37633', '720a1aece6827b150d4f833df058897cfaf705fefe2bbddd05bea5fbb569ad07', 0o755),
    ('69a28f878ce3', 'f13_growth_retention.py', '5f4ca02cc33d2d547ca22e46b897db3fb832a6a075ddbc66efc26226c61ef797', '811c5c89c50845d09a4a619eb3da7b215a1fe04ad4ad8b25f8a389d143ed78cb', 0o755),
)

# The historical SOURCE/PATHS/preconditions above remain immutable. This one
# successor consumes PR136 only; schema and all other wrapper pins are retained.
RETENTION_GENERATION = 'pr136-retention-20261001'
RETENTION_SOURCE = '95660fd8b8f9ec07e46d90c92a77846776c11c88'
RETENTION_PATHS = ('scripts/reports_archiver.py',)
RETENTION_BEFORE = 'e305cd57898e49b2dc57e3486aade3dc22c361b81844cf667c492c725a730020'
RETENTION_HASH = 'd4c2a0bfd0bf4944e3d23da77f047ac09d3abb21c4ad7f188f819a1a66317124'
RETENTION_SCHEMA = 'e1c2c57c90f08503742a1d0ad7256c10f89473e39f1feb7c717764e749bbc479'
RETENTION_JOBS = (
    ('f3d2db07d83f', 'f12_generator_archiver.py', '720a1aece6827b150d4f833df058897cfaf705fefe2bbddd05bea5fbb569ad07', 'dc4a4171478b6610fa293259238a423f6677771aaae62ac6366abf55f2571a68', 0o755),
    ('69a28f878ce3', 'f13_growth_retention.py', '811c5c89c50845d09a4a619eb3da7b215a1fe04ad4ad8b25f8a389d143ed78cb', '9f9021ef4e147970042792307c211fde2f3c53b2f5ef8ca3ba552467b821f3ee', 0o755),
)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def exact(path, digest, mode=None):
    body, actual_mode = read_regular(path)
    if sha(body) != digest or (mode is not None and actual_mode != mode):
        raise RuntimeError('preimage_or_mode_moved:' + str(path))
    return body, actual_mode


def replace_lines(body, updates):
    lines = body.splitlines(keepends=True)
    for number, (old, new) in updates.items():
        line = lines[number - 1]
        if len(old) != 64 or len(new) != 64 or line.count(old.encode()) != 1:
            raise RuntimeError('literal_predecessor_moved:' + str(number))
        lines[number - 1] = line.replace(old.encode(), new.encode(), 1)
    return b''.join(lines)


def plan(checkout, root, hermes, *, state_rebaseline=None, generation='historical'):
    if generation == RETENTION_GENERATION:
        if state_rebaseline is not None:
            raise RuntimeError('retention_generation_does_not_rebaseline')
        return _retention_plan(checkout, root, hermes)
    if generation != 'historical':
        raise RuntimeError('unknown_glp73_generation')
    checkout, root, hermes = (p.resolve(strict=True) for p in (checkout, root, hermes))
    subprocess.run(['git', '-C', str(checkout), 'merge-base', '--is-ancestor', SOURCE, 'HEAD'], check=True)
    producer = checkout / 'scripts/glp73_paired_adoption.py'
    if subprocess.check_output(['git', '-C', str(checkout), 'show', 'HEAD:scripts/glp73_paired_adoption.py']) != producer.read_bytes():
        raise RuntimeError('producer_not_committed_head')
    before = {root / p: exact(root / p, *SOURCE_BEFORE[i]) for i, p in enumerate(PATHS)}
    state = root / syncer.STATE
    expected_state = STATE_BEFORE
    if state_rebaseline is not None:
        from glp73_sync_rebaseline import validate
        expected_state = validate(root, state_rebaseline)
    before[state] = exact(state, expected_state)
    sources = {}
    for p in PATHS:
        body = subprocess.check_output(['git', '-C', str(checkout), 'show', SOURCE + ':' + p])
        if (checkout / p).read_bytes() != body:
            raise RuntimeError('source_moved:' + p)
        sources[p] = sha(body)
    if syncer.sync(root, checkout, False, ADOPT, PATHS) != 0:
        raise RuntimeError('scoped_source_preflight_refused')
    # Also inspect retained installed guards before any write.
    for p, expected in RETAINED.items():
        before[root / p] = exact(root / p, expected)
    stages = {}
    records = {}
    for jid, name, old, new, mode in JOBS:
        snapshot = checkout / SNAPSHOTS / name
        snapshot_body = subprocess.check_output(['git', '-C', str(checkout), 'show', 'HEAD:' + SNAPSHOTS + '/' + name])
        if snapshot.read_bytes() != snapshot_body or sha(snapshot_body) != old:
            raise RuntimeError('wrapper_snapshot_moved:' + name)
        target = hermes / 'scripts' / name
        before[target] = exact(target, old, mode)
        if name == 'glp_price_index_weekly.sh':
            updates = {}
            for line, path in WEEKLY.items():
                old_pin = snapshot_body.splitlines()[line - 1].decode().split('"')[-2]
                # Lines 28+ use inline quoted hashes; line 16 is assignment.
                if len(old_pin) != 64:
                    raise RuntimeError('weekly_pin_shape:' + str(line))
                updates[line] = (old_pin, sources.get(path, RETAINED.get(path)))
                if updates[line][1] is None:
                    raise RuntimeError('unexpected_weekly_target')
        else:
            schema = sources['scripts/glp_current_content_base_schema.py']
            archive = sources['scripts/reports_archiver.py']
            updates = ({16: (SOURCE_BEFORE[13][0], archive), 20: (SOURCE_BEFORE[0][0], schema)}
                       if name.startswith('f12_') else
                       {19: (SOURCE_BEFORE[13][0], archive), 23: (SOURCE_BEFORE[0][0], schema)})
        stages[target] = replace_lines(snapshot_body, updates)
        if sha(stages[target]) != new:
            raise RuntimeError('candidate_wrapper_mismatch:' + name)
    registry = hermes / 'cron/jobs.json'
    before[registry] = read_regular(registry)
    jobs = json.loads(before[registry][0])['jobs']
    for jid, name, old, new, mode in JOBS:
        hits = [j for j in jobs if j.get('id') == jid]
        if len(hits) != 1 or hits[0].get('script') != name or hits[0].get('script_sha256') != old:
            raise RuntimeError('paused_job_identity_moved:' + jid)
        job = hits[0]
        if (job.get('enabled') is not False or job.get('state') != 'paused'
                or job.get('run_claim') is not None or job.get('fire_claim') is not None
                or job.get('running')):
            raise RuntimeError('paused_job_claim_or_state:' + jid)
        records[jid] = copy.deepcopy(job)
    return before, stages, sources, records


def _retention_plan(checkout, root, hermes):
    checkout, root, hermes = (p.resolve(strict=True) for p in (checkout, root, hermes))
    subprocess.run(['git', '-C', str(checkout), 'merge-base', '--is-ancestor', RETENTION_SOURCE, 'HEAD'], check=True)
    manifest = json.loads((checkout / syncer.MANIFEST).read_bytes())
    # Authenticate the executing producer and its real inventory, as well as
    # the approved payload. No approved source hash is derived from HEAD.
    for name in ('glp73_paired_adoption.py', 'maintenance_inventory.py'):
        rel = 'scripts/' + name
        committed = subprocess.check_output(['git', '-C', str(checkout), 'show', 'HEAD:' + rel])
        if ((checkout / rel).read_bytes() != committed
                or (Path(__file__).parent / name).read_bytes() != committed
                or manifest['files'][rel]['sha256'] != sha(committed)):
            raise RuntimeError('retention_producer_not_committed_head:' + name)
    rel = RETENTION_PATHS[0]
    approved = subprocess.check_output(['git', '-C', str(checkout), 'show', RETENTION_SOURCE + ':' + rel])
    if sha(approved) != RETENTION_HASH or (checkout / rel).read_bytes() != approved:
        raise RuntimeError('retention_approved_source_moved')
    before = {root / rel: exact(root / rel, RETENTION_BEFORE, 0o600)}
    # This dependency is already installed and already co-pinned; do not stage,
    # import, change its baseline, or repin the separate weekly job.
    schema = root / 'scripts/glp_current_content_base_schema.py'
    before[schema] = exact(schema, RETENTION_SCHEMA, 0o644)
    state = root / syncer.STATE
    before[state] = read_regular(state)
    state_doc = json.loads(before[state][0])
    if state_doc.get('hashes', {}).get(rel) != RETENTION_BEFORE:
        raise RuntimeError('retention_source_baseline_moved')
    from contextlib import redirect_stdout
    from io import StringIO
    with redirect_stdout(StringIO()):
        if syncer.sync(root, checkout, False, (), RETENTION_PATHS) != 0:
            raise RuntimeError('scoped_source_preflight_refused')
    stages = {}
    for jid, name, old, new, mode in RETENTION_JOBS:
        target = hermes / 'scripts' / name
        before[target] = exact(target, old, mode)
        number = 16 if name.startswith('f12_') else 19
        stages[target] = replace_lines(before[target][0], {number: (RETENTION_BEFORE, RETENTION_HASH)})
        if sha(stages[target]) != new:
            raise RuntimeError('candidate_wrapper_mismatch:' + name)
    registry = hermes / 'cron/jobs.json'
    before[registry] = read_regular(registry)
    jobs = json.loads(before[registry][0])['jobs']
    records = {}
    for jid, name, old, new, mode in RETENTION_JOBS:
        hits = [j for j in jobs if j.get('id') == jid]
        if len(hits) != 1 or hits[0].get('script') != name or hits[0].get('script_sha256') != old:
            raise RuntimeError('paused_job_identity_moved:' + jid)
        job = hits[0]
        if (job.get('enabled') is not False or job.get('state') != 'paused'
                or job.get('run_claim') is not None or job.get('fire_claim') is not None or job.get('running')):
            raise RuntimeError('paused_job_claim_or_state:' + jid)
        records[jid] = copy.deepcopy(job)
    return before, stages, {rel: RETENTION_HASH}, records


def retention_proposal(checkout, root, hermes, planned):
    checkout, root, hermes = (p.resolve(strict=True) for p in (checkout, root, hermes))
    before, stages, sources, records = planned
    rel = RETENTION_PATHS[0]
    return {'generation': RETENTION_GENERATION, 'source': RETENTION_SOURCE,
            'producer_head': subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD']).decode().strip(),
            'producer_sha256': sha((checkout / 'scripts/glp73_paired_adoption.py').read_bytes()),
            'inventory_sha256': sha((checkout / 'scripts/maintenance_inventory.py').read_bytes()),
            'producer_manifest_sha256': sha((checkout / syncer.MANIFEST).read_bytes()),
            'target': {'root': str(root.resolve()), 'hermes': str(hermes.resolve())},
            'source_sha256': sources, 'source_before_sha256': {rel: sha(before[root / rel][0])},
            'source_before_mode': before[root / rel][1],
            'retained_schema': {'sha256': RETENTION_SCHEMA, 'mode': before[root / 'scripts/glp_current_content_base_schema.py'][1]},
            'wrapper_sha256': {p.name: sha(body) for p, body in stages.items()},
            'wrapper_before': {name: {'sha256': sha(before[hermes / 'scripts' / name][0]), 'mode': mode}
                               for _, name, _, _, mode in RETENTION_JOBS},
            'state_before_sha256': sha(before[root / syncer.STATE][0]),
            'state_before_mode': before[root / syncer.STATE][1],
            'registry_before_mode': before[hermes / 'cron/jobs.json'][1], 'job_before': records}


def _retain_retention_seals(journal, root, hermes):
    """Re-seal a verified publication whose caller acknowledgement was lost.

    Use the existing receipt, authenticated candidates and committed inode
    identities. Never recover sources or decide/replay the external outcome.
    """
    from contextlib import ExitStack
    from maintenance_cohort import metadata, read_all
    from maintenance_window import atomic_json, identity, read_journal, require
    data = read_journal(journal)
    transaction, switch = data.get('source_transaction', {}), data.get('source_switch', {})
    if (transaction.get('outcome') not in {'pending', 'uncertain'}
            or transaction.get('adopter') != identity()
            or not switch.get('commit_started') or switch.get('state') != 'published'):
        return
    expected = {str(root / RETENTION_PATHS[0]), str(root / syncer.STATE),
                *(str(hermes / 'scripts' / name) for _, name, *_ in RETENTION_JOBS)}
    require(set(transaction['after']) == expected
            and {name for name, item in switch['files'].items() if not item['cache']} == expected
            and set(transaction['job_ids']) == {jid for jid, *_ in RETENTION_JOBS},
            'retention_commit_receipt_scope_moved')
    # Durable uncertainty precedes any renewed fence, so a second interruption
    # cannot certify the readable publication or admit automatic replay.
    switch['state'] = 'commit_uncertain'
    atomic_json(journal, data)
    with ExitStack() as handles:
        for name in sorted(expected):
            path, item = Path(name), switch['files'][name]
            require(item['after'] == transaction['after'][name]
                    and sha(read_regular(Path(item['candidate']))[0]) == item['after'],
                    'retention_commit_candidate_custody_moved')
            require(metadata(path.lstat()) == item['after_metadata'] and not path.is_symlink(),
                    'retention_committed_inode_moved')
            if path.stat().st_mode & 0o777 == 0:
                continue  # Never reopen or chmod a sealed source to inspect it.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            handles.callback(os.close, fd)
            st = os.fstat(fd)
            require(metadata(st) == item['after_metadata'] and st.st_nlink == 1
                    and st.st_uid == os.geteuid() and sha(read_all(fd)) == item['after'],
                    'retention_committed_source_moved')
            os.fchmod(fd, 0)
            os.fsync(fd)
            require(metadata(path.lstat()) == metadata(os.fstat(fd)), 'retention_commit_fence_inode_moved')


def _adopt_retention(checkout, root, hermes, review, *, scheduler, window):
    from contextlib import ExitStack
    checkout, root, hermes = (p.resolve(strict=True) for p in (checkout, root, hermes))
    planned = plan(checkout, root, hermes, generation=RETENTION_GENERATION)
    proposal = retention_proposal(checkout, root, hermes, planned)
    runtime = review.get('runtime_review', {})
    preparer = str(review.get('prepared_by') or '').strip()
    if (review.get('schema') != 'glp73-retention-adoption-review-v1'
            or review.get('decision') != 'ACCEPT_EXACT_BYTES' or review.get('proposal') != proposal
            or not preparer or not str(review.get('reviewer') or '').strip() or str(review['reviewer']).strip() == preparer
            or runtime.get('schema') != 'glp73-retention-runtime-review-v1'
            or runtime.get('decision') != 'ACCEPT_EXACT_BYTES'
            or not str(runtime.get('reviewer') or '').strip() or str(runtime['reviewer']).strip() == preparer
            or runtime.get('proposal_sha256') != sha(json.dumps(proposal, sort_keys=True).encode())):
        raise RuntimeError('independent_exact_source_and_runtime_review_required_or_moved')
    registry = hermes / 'cron/jobs.json'
    if (scheduler._current_cron_store().jobs_file != registry
            or scheduler._jobs_lock_file() != registry.parent / '.jobs.lock'):
        raise RuntimeError('wrong_scheduler_store')
    with ExitStack() as fences:
        for jid in sorted(proposal['job_before']):
            if not fences.enter_context(scheduler._fire_job_lock(jid)):
                raise RuntimeError('fire_fence_unavailable:' + jid)
        fences.enter_context(scheduler._jobs_file_lock)
        fd = os.open(scheduler._jobs_lock_file(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fresh = plan(checkout, root, hermes, generation=RETENTION_GENERATION)
            if retention_proposal(checkout, root, hermes, fresh) != proposal:
                raise RuntimeError('preimage_moved_under_lock')
            # Snapshot unrelated records at the locked boundary, rather than
            # freezing them in a two-target review. Preserve them in native save.
            before, stages, sources, records = fresh
            state = json.loads(before[root / syncer.STATE][0])
            state['hashes'].update(sources)
            stage = {root / RETENTION_PATHS[0]: subprocess.check_output(
                        ['git', '-C', str(checkout), 'show', RETENTION_SOURCE + ':' + RETENTION_PATHS[0]]),
                     root / syncer.STATE: (json.dumps(state, indent=2, sort_keys=True) + '\n').encode(), **stages}
            original = {path: before[path] for path in stage}
            custody = root / 'reports/ops/pin-archive/glp73' / sha(json.dumps(review, sort_keys=True).encode())
            preserve_custody(custody, before, stage)
            if any(read_regular(path) != value for path, value in before.items()):
                raise RuntimeError('preimage_moved_after_custody')
            window.accept_file_updates({str(p): sha(v[0]) for p, v in original.items()},
                                       {str(p): sha(v) for p, v in stage.items()})
            jobs = json.loads(before[registry][0])['jobs']
            next_jobs = copy.deepcopy(jobs)
            for jid, name, old, new, mode in RETENTION_JOBS:
                after = next(j for j in next_jobs if j['id'] == jid)
                after['script_sha256'] = new
                # Durable job intent precedes the cohort's journal capture.
                window.accept_job_update(jid, records[jid], after)
            def commit():
                if read_regular(registry) != before[registry]:
                    raise RuntimeError('registry_moved_before_saver')
                schema = root / 'scripts/glp_current_content_base_schema.py'
                if read_regular(schema) != before[schema]:
                    raise RuntimeError('retained_schema_moved_before_saver')
                scheduler._record_load_stamp(None)
                try:
                    scheduler._save_jobs_unlocked(next_jobs)
                finally:
                    scheduler._record_load_stamp(None)
                if json.loads(read_regular(registry)[0])['jobs'] != next_jobs:
                    raise RuntimeError('scheduler_readback_mismatch_no_retry')
            # The maintained cohort owns rollback before durable external
            # intent, and keeps custody/seals for every possibly-landed save.
            try:
                window.switch_files(stage, original, commit=commit)
            except BaseException:
                # ActiveWindow's own acknowledgement can fail after the native
                # cohort returns. Keep this repair within the new generation.
                _retain_retention_seals(window.journal, root, hermes)
                raise
    return {'applied': True, 'source': RETENTION_SOURCE, 'proposal': proposal, 'custody': str(custody)}


def adopt(checkout, root, hermes, review, *, scheduler, window=None, state_rebaseline=None, generation='historical'):
    if window is None:
        raise RuntimeError('active_maintenance_window_required')
    from maintenance_window import require_active_window
    if generation == RETENTION_GENERATION:
        if state_rebaseline is not None:
            raise RuntimeError('retention_generation_does_not_rebaseline')
        result = None
        try:
            with require_active_window(window, root, hermes) as active:
                result = _adopt_retention(checkout, root, hermes, review, scheduler=scheduler, window=active)
            return result
        except BaseException:
            if result is not None:
                # Post-yield admission/readback acknowledgement is also part
                # of this operation. Re-enter its existing journal lock only.
                from maintenance_window import operation_lock, OPERATION_ADMISSION_SECONDS
                with operation_lock(window, timeout=OPERATION_ADMISSION_SECONDS):
                    _retain_retention_seals(window, root.resolve(), hermes.resolve())
            raise
    with require_active_window(window, root, hermes) as active:
        if generation != 'historical':
            raise RuntimeError('unknown_glp73_generation')
        return _adopt_in_window(checkout, root, hermes, review, scheduler=scheduler,
                                window=active, state_rebaseline=state_rebaseline)


def _adopt_in_window(checkout, root, hermes, review, *, scheduler, window, state_rebaseline):
    before, stages, sources, records = plan(checkout, root, hermes, state_rebaseline=state_rebaseline)
    proposal = {'source_sha256': sources, 'wrapper_sha256': {name: sha(stages[hermes / 'scripts' / name]) for _, name, *_ in JOBS},
                'source_before_sha256': {p: SOURCE_BEFORE[i][0] for i, p in enumerate(PATHS)},
                'state_before_sha256': sha(before[root / syncer.STATE][0]), 'job_before': records,
                'state_rebaseline_sha256': sha(json.dumps(state_rebaseline, sort_keys=True).encode()) if state_rebaseline else None,
                'registry_before_sha256': sha(before[hermes / 'cron/jobs.json'][0])}
    if (review.get('schema') != 'glp73-paired-adoption-review-v1'
            or review.get('decision') != 'ACCEPT_EXACT_BYTES'
            or review.get('source') != SOURCE or not review.get('reviewer')
            or any(review.get(key) != value for key, value in proposal.items())):
        raise RuntimeError('independent_exact_review_required_or_moved')
    registry = hermes / 'cron/jobs.json'
    if (scheduler._current_cron_store().jobs_file != registry
            or scheduler._jobs_lock_file() != registry.parent / '.jobs.lock'):
        raise RuntimeError('wrong_scheduler_store')
    from contextlib import ExitStack
    with ExitStack() as fences:
        for jid, *_ in JOBS:
            if not fences.enter_context(scheduler._fire_job_lock(jid)):
                raise RuntimeError('fire_fence_unavailable:' + jid)
        fences.enter_context(scheduler._jobs_file_lock)
        fd = os.open(scheduler._jobs_lock_file(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fresh = plan(checkout, root, hermes, state_rebaseline=state_rebaseline)
            if fresh != (before, stages, sources, records):
                raise RuntimeError('preimage_moved_under_lock')
            if any(read_regular(path) != value for path, value in before.items()):
                raise RuntimeError('preimage_moved_before_custody')
            state_path = root / syncer.STATE
            state_doc = json.loads(before[state_path][0])
            state_doc['hashes'].update(sources)
            state_bytes = (json.dumps(state_doc, indent=2, sort_keys=True) + '\n').encode()
            stage = {root / p: (checkout / p).read_bytes() for p in PATHS}
            stage[state_path] = state_bytes
            stage.update(stages)
            if any(sha(stage[root / p]) != sources[p] for p in PATHS):
                raise RuntimeError('reviewed_candidate_moved_before_switch')
            custody = root / 'reports/ops/pin-archive/glp73' / sha(json.dumps(review, sort_keys=True).encode())
            preserve_custody(custody, before, stage)
            if any(read_regular(path) != value for path, value in before.items()):
                raise RuntimeError('preimage_moved_after_custody')
            registry_before = before[registry]
            window.accept_file_updates(
                {str(path.resolve()): sha(before[path][0]) for path in stage},
                {str(path.resolve()): sha(body) for path, body in stage.items()},
            )
            source_written = False
            try:
                source_written = True
                window.switch_files(stage, {path: before[path] for path in stage})
                if read_regular(registry) != registry_before:
                    raise RuntimeError('registry_moved_before_saver')
                jobs = json.loads(registry_before[0])['jobs']
                next_jobs = copy.deepcopy(jobs)
                for jid, name, old, new, mode in JOBS:
                    hit = next(j for j in next_jobs if j.get('id') == jid)
                    if hit != records[jid]:
                        raise RuntimeError('job_moved_before_saver')
                    previous = copy.deepcopy(hit)
                    hit['script_sha256'] = new
                    window.accept_job_update(jid, previous, hit)
                scheduler._record_load_stamp(None)
                try:
                    scheduler._save_jobs_unlocked(next_jobs)
                finally:
                    scheduler._record_load_stamp(None)
                if json.loads(read_regular(registry)[0])['jobs'] != next_jobs:
                    raise RuntimeError('scheduler_readback_mismatch_no_retry')
            except Exception:
                # Only if registry stayed unchanged: an uncertain saver result is
                # never rolled back or retried. Inspect every path, including a
                # rename that landed before its call raised.
                if read_regular(registry) == registry_before and source_written:
                    original_files = {path: before[path] for path in stage}
                    current_files = {path: read_regular(path) for path in stage}
                    staged_files = {path: (body, before[path][1]) for path, body in stage.items()}
                    if current_files != original_files:
                        if current_files != staged_files:
                            raise RuntimeError('rollback_conflict_no_overwrite:source_cohort')
                        window.switch_files({path: value[0] for path, value in original_files.items()}, staged_files)
                raise
    return {'applied': True, 'source': SOURCE, 'proposal': proposal, 'custody': str(custody)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', choices=('historical', RETENTION_GENERATION), default='historical',
                        help='immutable historical cohort or PR136 archiver with only F12/F13 co-pins')
    parser.add_argument('--checkout', required=True, type=Path)
    parser.add_argument('--root', type=Path, default=syncer.DEFAULT_ROOT)
    parser.add_argument('--hermes', type=Path, default=Path('/home/ubuntu/.hermes'))
    parser.add_argument('--review', type=Path)
    parser.add_argument('--window', type=Path, help='active maintenance journal; required for apply')
    parser.add_argument('--state-rebaseline', type=Path, help='independent exact explained-diff review')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        state_rebaseline = json.loads(args.state_rebaseline.read_text()) if args.state_rebaseline else None
        if args.apply:
            if args.window is None:
                raise RuntimeError('active_maintenance_window_required')
            if args.review is None:
                raise RuntimeError('independent_review_required')
            sys.path.insert(0, str(args.hermes / 'hermes-agent'))
            from cron import jobs as scheduler
            result = adopt(args.checkout, args.root, args.hermes, json.loads(args.review.read_text()), scheduler=scheduler, window=args.window, state_rebaseline=state_rebaseline, generation=args.generation)
        else:
            from contextlib import nullcontext
            from maintenance_window import require_active_window
            context = require_active_window(args.window, args.root, args.hermes) if args.window else nullcontext(None)
            with context:
                planned = plan(args.checkout, args.root, args.hermes, state_rebaseline=state_rebaseline, generation=args.generation)
                before, staged, sources, records = planned
            if args.generation == RETENTION_GENERATION:
                result = {'applied': False, 'proposal': retention_proposal(args.checkout, args.root, args.hermes, planned)}
            else:
                result = {'applied': False, 'source_sha256': sources,
                      'wrapper_sha256': {p.name: sha(b) for p, b in staged.items()},
                      'job_before': records, 'registry_before_sha256': sha(before[args.hermes / 'cron/jobs.json'][0]),
                      'source_before_sha256': {p: SOURCE_BEFORE[i][0] for i, p in enumerate(PATHS)},
                      'state_before_sha256': sha(before[args.root / syncer.STATE][0]),
                      'state_rebaseline_sha256': sha(json.dumps(state_rebaseline, sort_keys=True).encode()) if state_rebaseline else None}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print('GLP73_ADOPTION_REFUSED: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
