#!/usr/bin/env python3
"""Fixed GLP73 14-source / three-wrapper / three-paused-job adoption.

Apply is not launch exclusion: an authorized caller must separately exclude source
writers and *all* direct readers/launchers for the whole operation. The scheduler
locks only its own registry. No multi-file atomic transaction is claimed.
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
from maxbounty_pin_release import atomic_write, preserve_custody, read_regular

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


def plan(checkout, root, hermes):
    checkout, root, hermes = (p.resolve(strict=True) for p in (checkout, root, hermes))
    subprocess.run(['git', '-C', str(checkout), 'merge-base', '--is-ancestor', SOURCE, 'HEAD'], check=True)
    producer = checkout / 'scripts/glp73_paired_adoption.py'
    if subprocess.check_output(['git', '-C', str(checkout), 'show', 'HEAD:scripts/glp73_paired_adoption.py']) != producer.read_bytes():
        raise RuntimeError('producer_not_committed_head')
    before = {root / p: exact(root / p, *SOURCE_BEFORE[i]) for i, p in enumerate(PATHS)}
    state = root / syncer.STATE
    before[state] = exact(state, STATE_BEFORE)
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


def adopt(checkout, root, hermes, review, *, scheduler):
    """External writer/direct-reader exclusion must already be independently held."""
    before, stages, sources, records = plan(checkout, root, hermes)
    proposal = {'source_sha256': sources, 'wrapper_sha256': {name: sha(stages[hermes / 'scripts' / name]) for _, name, *_ in JOBS},
                'source_before_sha256': {p: SOURCE_BEFORE[i][0] for i, p in enumerate(PATHS)},
                'state_before_sha256': STATE_BEFORE, 'job_before': records,
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
            fresh = plan(checkout, root, hermes)
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
            custody = root / 'reports/ops/pin-archive/glp73' / sha(json.dumps(review, sort_keys=True).encode())
            preserve_custody(custody, before, stage)
            if any(read_regular(path) != value for path, value in before.items()):
                raise RuntimeError('preimage_moved_after_custody')
            registry_before = before[registry]
            source_written = False
            try:
                source_written = True
                if syncer.sync(root, checkout, True, ADOPT, PATHS) != 0:
                    raise RuntimeError('source_apply_refused')
                for path, body in stages.items():
                    if read_regular(path) != before[path]:
                        raise RuntimeError('wrapper_moved_before_replace')
                    atomic_write(path, body, before[path][1])
                if read_regular(registry) != registry_before:
                    raise RuntimeError('registry_moved_before_saver')
                jobs = json.loads(registry_before[0])['jobs']
                next_jobs = copy.deepcopy(jobs)
                for jid, name, old, new, mode in JOBS:
                    hit = next(j for j in next_jobs if j.get('id') == jid)
                    if hit != records[jid]:
                        raise RuntimeError('job_moved_before_saver')
                    hit['script_sha256'] = new
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
                    for path, (body, mode) in before.items():
                        if path == registry or path not in stage:
                            continue
                        now = read_regular(path)
                        if now == (body, mode):
                            continue
                        if now != (stage[path], mode):
                            raise RuntimeError('rollback_conflict_no_overwrite:' + str(path))
                        atomic_write(path, body, mode)
                raise
    return {'applied': True, 'source': SOURCE, 'proposal': proposal, 'custody': str(custody)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', required=True, type=Path)
    parser.add_argument('--root', type=Path, default=syncer.DEFAULT_ROOT)
    parser.add_argument('--hermes', type=Path, default=Path('/home/ubuntu/.hermes'))
    parser.add_argument('--review', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        if args.apply:
            if args.review is None:
                raise RuntimeError('independent_review_required')
            sys.path.insert(0, str(args.hermes / 'hermes-agent'))
            from cron import jobs as scheduler
            result = adopt(args.checkout, args.root, args.hermes, json.loads(args.review.read_text()), scheduler=scheduler)
        else:
            before, staged, sources, records = plan(args.checkout, args.root, args.hermes)
            result = {'applied': False, 'source_sha256': sources,
                      'wrapper_sha256': {p.name: sha(b) for p, b in staged.items()},
                      'job_before': records, 'registry_before_sha256': sha(before[args.hermes / 'cron/jobs.json'][0])}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print('GLP73_ADOPTION_REFUSED: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
