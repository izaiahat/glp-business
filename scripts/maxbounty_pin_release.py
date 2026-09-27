#!/usr/bin/env python3
"""Exact-review-gated MaxBounty pins; never a baseline healer or activation.

See ops/maxbounty-pin-release.md. No discovery, portal or queue imports.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

BUSINESS = Path('/home/ubuntu/business')
MONITOR = 'scripts/glp_maxbounty_capacity_monitor.py'
FRESH = 'scripts/glp_maxbounty_fresh_probe.py'
PINS = 'scripts/glp_maxbounty_capacity_monitor.pins.json'
WRAPPER = 'glp_maxbounty_capacity_resume.sh'
SNAPSHOT = 'ops/hermes-cron-scripts/' + WRAPPER
BASELINE = 'ops/maxbounty-pin-predecessor.json'
SOURCES = frozenset((MONITOR, FRESH))
DEPENDENCIES = frozenset(('ops/__init__.py', 'ops/n8n_dt.py', 'scripts/__init__.py',
    'scripts/marketplace_adapters/__init__.py', 'scripts/marketplace_adapters/_base.py',
    'scripts/marketplace_adapters/maxbounty.py', 'scripts/marketplace_adapters/maxbounty_discovery.py',
    'scripts/marketplace_adapters/portal_lane.py'))
JOB_ID = '89cd7b766dba'
HEX = re.compile(r'^[0-9a-f]{64}$')


def sha_bytes(body):
    return hashlib.sha256(body).hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def strict_json(body):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeError('duplicate_json_key')
            result[key] = value
        return result
    return json.loads(body, object_pairs_hook=unique)


def stage_pins(before, wrapper, sources, *, business=BUSINESS):
    """Emit only the monitor substitution, fresh membership and two shell pins."""
    if set(sources) != SOURCES or any(not HEX.fullmatch(v) for v in sources.values()):
        raise RuntimeError('exact_source_set_required')
    document = strict_json(before)
    files = document.get('files', {})
    expected = {str(business / name) for name in DEPENDENCIES | {MONITOR}}
    if (set(document) != {'schema', 'files'}
            or document['schema'] != 'maxbounty-readonly-source-pins-v1'
            or set(files) != expected or any(not HEX.fullmatch(v) for v in files.values())):
        raise RuntimeError('predecessor_manifest_contract')
    old_monitor = files[str(business / MONITOR)]
    files.update({str(business / name): digest for name, digest in sources.items()})
    emitted = json_bytes(document)
    text = wrapper.decode()
    for variable, old, new in (('SCRIPT', old_monitor, sources[MONITOR]),
                               ('PINS', sha_bytes(before), sha_bytes(emitted))):
        # Match the literal inline comparison, not any other 64-byte token.
        token = f'"$(sha256sum "${variable}" | cut -d\' \' -f1)" == "{old}"'
        if text.count(token) != 1:
            raise RuntimeError('inline_pin_match_count:' + variable)
        text = text.replace(token, token.replace(old, new), 1)
    return emitted, text.encode()


def relocate(body, business, hermes):
    """Path-only relocation for isolated tests; CLI has no root override."""
    mapping = {str(BUSINESS).encode(): str(business).encode(),
               b'/home/ubuntu/.hermes': str(hermes).encode()}
    return re.sub(rb'/home/ubuntu/(?:business|\.hermes)', lambda m: mapping[m[0]], body)


def read_regular(path):
    import stat
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise RuntimeError('symlink_refused:' + str(path))
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise RuntimeError('regular_file_required')
    return path.read_bytes(), st.st_mode & 0o777


def atomic_write(path, body, mode):
    # Same fsync/rename convention as awin_pin_release, without fleet imports.
    import os
    import tempfile
    fd, name = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(body); out.flush(); os.fsync(out.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def git(repo, *args):
    import subprocess
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE)


def checked_plan(review_path, repo, business, hermes):
    import datetime as dt
    review_preimage = read_regular(review_path)
    review_body = review_preimage[0]
    review = strict_json(review_body)
    if (review.get('schema') != 'maxbounty-pin-review-v1'
            or review.get('decision') not in {'REVIEW_ONLY', 'ACCEPT_EXACT_CURRENT_BYTES'}
            or not str(review.get('reviewer') or '').strip()
            or not isinstance(review.get('evidence'), list) or not review['evidence']
            or any(not isinstance(s, str) or not s.strip() for s in review['evidence'])
            or not str(review.get('writer_exclusion') or '').strip()):
        raise RuntimeError('exact_review_contract')
    when = dt.datetime.strptime(review['reviewed_at_utc'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
    if when > dt.datetime.now(dt.timezone.utc):
        raise RuntimeError('review_timestamp_in_future')
    for key in ('producer_head', 'source_head'):
        if not re.fullmatch('[0-9a-f]{40}', review.get(key, '')):
            raise RuntimeError('exact_git_head_required')
    head = review['producer_head']
    if git(repo, 'rev-parse', 'HEAD').decode().strip() != head:
        raise RuntimeError('producer_head_moved')
    git(repo, 'merge-base', '--is-ancestor', review['source_head'], head)
    # The running producer itself, not merely an arbitrary checkout, is reviewed.
    if git(repo, 'show', head+':scripts/maxbounty_pin_release.py') != Path(__file__).read_bytes():
        raise RuntimeError('producer_bytes_not_reviewed_head')
    source_hashes = review.get('source_sha256', {})
    if set(source_hashes) != SOURCES:
        raise RuntimeError('exact_source_set_required')
    observed = {}
    for name in SOURCES:
        body = git(repo, 'show', review['source_head']+':'+name)
        if sha_bytes(body) != source_hashes[name]:
            raise RuntimeError('source_git_binding_mismatch')
        observed[business/name] = read_regular(business/name)
        if observed[business/name][0] != body:
            raise RuntimeError('installed_source_moved')
    baseline = git(repo, 'show', head+':'+BASELINE)
    snapshot = git(repo, 'show', head+':'+SNAPSHOT)
    expected_pins = relocate(baseline, business, hermes)
    expected_wrapper = relocate(snapshot, business, hermes).replace(
        sha_bytes(baseline).encode(), sha_bytes(expected_pins).encode())
    pins, wrapper = business/PINS, hermes/'scripts'/WRAPPER
    for path, expected in ((pins, expected_pins), (wrapper, expected_wrapper)):
        observed[path] = read_regular(path)
        if observed[path][0] != expected:
            raise RuntimeError('unknown_artifact_preimage:' + str(path))
    if review.get('predecessor_sha256') != {'manifest': sha_bytes(expected_pins), 'wrapper': sha_bytes(expected_wrapper)}:
        raise RuntimeError('predecessor_review_mismatch')
    # No opportunistic adoption of the eight unchanged dependency pins.
    for path, digest in strict_json(expected_pins)['files'].items():
        target = Path(path)
        if target == business/MONITOR:
            continue
        if target not in {business/name for name in DEPENDENCIES}:
            raise RuntimeError('unexpected_dependency')
        observed[target] = read_regular(target)
        if sha_bytes(observed[target][0]) != digest:
            raise RuntimeError('unreviewed_dependency_drift')
    after_pins, after_wrapper = stage_pins(expected_pins, expected_wrapper, source_hashes, business=business)
    proposed = {'manifest': sha_bytes(after_pins), 'wrapper': sha_bytes(after_wrapper)}
    if review['decision'] == 'ACCEPT_EXACT_CURRENT_BYTES' and review.get('proposed_sha256') != proposed:
        raise RuntimeError('emitted_artifacts_not_reviewed')
    registry = hermes/'cron/jobs.json'
    observed[registry] = read_regular(registry)
    doc = strict_json(observed[registry][0])
    jobs = doc['jobs']
    hits = [j for j in jobs if j.get('id') == JOB_ID]
    if len(hits) != 1 or hits[0] != review.get('expected_job'):
        raise RuntimeError('paused_registration_full_record_cas')
    job = hits[0]
    if (job.get('enabled') is not False or job.get('state') != 'paused'
            or job.get('script') not in {WRAPPER, str(wrapper)}
            or job.get('script_sha256') != sha_bytes(expected_wrapper)
            or job.get('fire_claim') or job.get('running')):
        raise RuntimeError('paused_registration_identity_or_state')
    import copy
    candidate = copy.deepcopy(jobs)
    next(j for j in candidate if j.get('id') == JOB_ID)['script_sha256'] = proposed['wrapper']
    observed[review_path] = review_preimage
    result = {'schema': 'maxbounty-pin-release-v1', 'applied': False,
              'producer_head': head, 'source_head': review['source_head'],
              'review_sha256': sha_bytes(review_body), 'job_id': JOB_ID,
              'proposed_sha256': proposed, 'pause_state_preserved': True}
    import difflib
    staged = {pins: after_pins, wrapper: after_wrapper}
    result['artifact_diffs'] = {str(path): ''.join(difflib.unified_diff(
        observed[path][0].decode().splitlines(keepends=True), body.decode().splitlines(keepends=True),
        fromfile=str(path), tofile=str(path)+'.candidate')) for path, body in staged.items()}
    return review, observed, staged, candidate, result


def assert_unchanged(observed):
    for path, expected in observed.items():
        if read_regular(path) != expected:
            raise RuntimeError('preimage_changed_before_apply:' + str(path))


def preserve_custody(root, observed, staged):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries = []
    for path, (body, mode) in sorted(observed.items()):
        entries.append({'path': str(path), 'sha256': sha_bytes(body), 'mode': mode, 'absent': False})
    for body in [v[0] for v in observed.values()] + list(staged.values()):
        target = root/(sha_bytes(body)+'.bytes')
        if target.exists():
            if read_regular(target)[0] != body:
                raise RuntimeError('custody_collision')
        else:
            with target.open('xb') as out:
                import os
                os.chmod(target, 0o600); out.write(body); out.flush(); os.fsync(out.fileno())
    metadata = root/'preimages.json'
    if metadata.exists():
        if read_regular(metadata)[0] != json_bytes(entries):
            raise RuntimeError('custody_metadata_collision')
    else:
        atomic_write(metadata, json_bytes(entries), 0o600)


def release(review_path, repo, *, business=BUSINESS, hermes=Path('/home/ubuntu/.hermes'),
            apply=False, scheduler=None):
    """Preview is read-only. Apply holds the actual scheduler lock through readback.

    External source/wrapper-owner exclusion is a separate admission prerequisite;
    the review names it. This lock never pretends to exclude arbitrary file writers.
    """
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
            # Never use the scheduler context that degrades after lock timeout.
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert_unchanged(observed)
            custody = business/'reports/ops/pin-archive/maxbounty'/result['review_sha256']
            preserve_custody(custody, observed, staged)
            assert_unchanged(observed)
            wrote = []
            current = dict(observed)
            try:
                for path, body in staged.items():
                    assert_unchanged(current)
                    atomic_write(path, body, observed[path][1])
                    wrote.append(path); current[path] = (body, observed[path][1])
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
                # An uncertain scheduler write cannot safely be replayed or reverted.
                # Retain coherent new artifacts and custody for owner reconciliation.
                if read_regular(registry) == observed[registry]:
                    for path in reversed(list(staged)):
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, type=Path, help='Exact reviewed producer checkout')
    parser.add_argument('--review-receipt', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        result = release(args.review_receipt, args.repo, apply=args.apply)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({'status': 'REFUSED', 'reason': str(exc)}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
