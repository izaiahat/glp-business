"""Reviewed forward reconciliation for a sealed GLP44 reporting commit.

Never infer no effects from an unchanged registry and never call the saver.
Only an exact already-landed full native AFTER record can authorize candidate
reconstruction. Custody supplies bytes; sealed inode metadata supplies target
CAS, not a claim that unreadable live bytes were hashed. Original intent,
preimages and candidate custody remain immutable. Caller owns exit/restoration.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import stat
import tempfile

import maintenance_cohort as cohort
import maintenance_window as mw
from maxbounty_pin_release import read_regular
from glp_ship_registry import REPORTING_PATHS
from glp44_scoped_adoption import JOB, WRAPPER, SNAPSHOT
import sync_from_repo as syncer

REVIEW_SCHEMA = 'glp44-commit-reconstruction-review-v1'


def proposal(window):
    """Read-only custody/identity inspection; no source is opened or unsealed."""
    raw = read_regular(window.journal)[0]
    data = mw.read_journal(window.journal)
    root, hermes = window.backend.root, window.backend.hermes
    mw.require(data['root'] == str(root) and data['hermes'] == str(hermes)
               and data['journal'] == str(window.journal), 'commit_recovery_wrong_target')
    switch, transaction = data.get('source_switch', {}), data.get('source_transaction', {})
    mw.require(data['state'] == 'recovery_required' and not data.get('pre_adoption_recovery')
               and switch.get('state') == 'commit_uncertain' and switch.get('commit_started') is True
               and not switch.get('commit_verified') and transaction.get('outcome') == 'uncertain',
               'commit_recovery_boundary_required')
    mw.require(data['entries'] and all(r.get('intent') and r.get('paused')
               and not r.get('restore_intent') and not r.get('restored') for r in data['entries']),
               'commit_recovery_pause_intent_required')
    expected = {str(root / rel) for rel in REPORTING_PATHS} | {
        str(root / syncer.STATE), str(hermes / 'scripts' / WRAPPER)}
    sources = {name: item for name, item in switch.get('files', {}).items() if item.get('cache') is False}
    mw.require(set(sources) == set(transaction.get('before', {})) == set(transaction.get('after', {}))
               == expected and transaction.get('job_ids') == [JOB] and set(data.get('job_updates', {})) == {JOB},
               'commit_recovery_exact_reporting_scope_required')
    registry = hermes / 'cron/jobs.json'
    job = window.backend.job(JOB)
    mw.require(mw.json_sha(job) == data['job_updates'][JOB]['after_sha256']
               and job.get('script_sha256') == data['job_updates'][JOB]['after_pin']
               == transaction['after'][str(hermes / 'scripts' / WRAPPER)]
               == transaction['after'][str(root / SNAPSHOT)]
               and job.get('enabled') is False and job.get('state') == 'paused'
               and not any(job.get(k) for k in ('fire_claim', 'run_claim', 'running')),
               'commit_recovery_exact_landed_job_required')
    files = {}
    for name, item in switch['files'].items():
        path = Path(name)
        original = Path(item['original'])
        mw.require(original.parent == Path(switch['custody']) and not original.is_symlink()
                   and cohort.digest(read_regular(original)[0]) == item['before'],
                   'commit_recovery_original_custody_moved')
        if item['cache']:
            mw.require(any(path == p.with_suffix('.pyc') or
                       (path.parent == p.parent / '__pycache__' and path.name.startswith(p.stem + '.')
                        and path.suffix == '.pyc') for p in map(Path, sources) if p.suffix == '.py')
                       and not path.exists() and not path.is_symlink(), 'commit_recovery_cache_moved')
            files[name] = {'cache': True, 'absent': True, 'before': item['before']}
            continue
        st = path.lstat()
        mw.require(stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and st.st_nlink == 1
                   and stat.S_IMODE(st.st_mode) == 0 and cohort.metadata(st) == item['after_metadata'],
                   'commit_recovery_sealed_target_moved:' + name)
        candidate = Path(item['candidate'])
        mw.require(candidate.parent == Path(switch['custody']) and not candidate.is_symlink()
                   and cohort.digest(read_regular(candidate)[0]) == item['after'] == transaction['after'][name]
                   and item['before'] == transaction['before'][name], 'commit_recovery_candidate_moved:' + name)
        mw.require(type(item['mode']) is int and 0 < item['mode'] <= 0o777
                   and st.st_gid in set(os.getgroups()) | {os.getegid()}, 'commit_recovery_mode_or_group')
        files[name] = {'cache': False, 'metadata': cohort.metadata(st), 'mode': item['mode'],
                       'candidate': str(candidate), 'sha256': item['after']}
    # Newly created bytecode is not permitted just because it was absent from custody.
    for p in map(Path, sources):
        if p.suffix == '.py':
            mw.require(not p.with_suffix('.pyc').exists() and not p.with_suffix('.pyc').is_symlink()
                       and not list((p.parent / '__pycache__').glob(p.stem + '.*.pyc')),
                       'commit_recovery_unrecorded_cache')
    registry_body, registry_mode = read_regular(registry)
    mw.require(read_regular(window.journal)[0] == raw, 'commit_recovery_journal_moved')
    stable = {k: v for k, v in data.items() if k not in {'keeper', 'locks'}}
    return {'schema': 'glp44-commit-reconstruction-proposal-v1', 'journal_sha256': mw.json_sha(stable),
            'target': {'root': str(root), 'hermes': str(hermes), 'journal': str(window.journal)},
            'producer_sha256': cohort.digest(Path(__file__).read_bytes()),
            'files': files, 'job_after_sha256': mw.json_sha(job),
            'registry_sha256': cohort.digest(registry_body), 'registry_mode': registry_mode}


def recover(window, review, *, _source_owner=None):
    """Keep window locks after success; caller must exit before releasing custody.

    Interrupted reconstruction remains sealed and requires a new supported
    disposition, never a blind retry of the initial commit-uncertain proposal.
    """
    mw.require(isinstance(review, dict) and review.get('schema') == REVIEW_SCHEMA
               and review.get('decision') == 'ACCEPT_EXACT_LANDED_JOB_AND_CANDIDATE'
               and isinstance(review.get('prepared_by'), str) and review['prepared_by'].strip()
               and isinstance(review.get('reviewer'), str) and review['reviewer'].strip()
               and review['prepared_by'].strip() != review['reviewer'].strip(), 'commit_recovery_review_required')
    mw.require(os.geteuid() != 0, 'cohort_privileged_controller_refused')
    caps = int(next(l.split()[1] for l in Path('/proc/self/status').read_text().splitlines()
                    if l.startswith('CapEff:')), 16)
    mw.require(not caps & ((1 << 1) | (1 << 2)), 'cohort_dac_override_refused')
    root, hermes = window.backend.root, window.backend.hermes
    scheduler = window.backend.scheduler()
    mw.require(scheduler._current_cron_store().jobs_file == hermes / 'cron/jobs.json'
               and scheduler._jobs_lock_file() == hermes / 'cron/.jobs.lock', 'wrong_scheduler_store')
    with syncer.source_session(root, _source_owner), mw.operation_lock(window.journal), ExitStack() as stack:
        mw.require(stack.enter_context(scheduler._fire_job_lock(JOB)), 'commit_recovery_fire_fence_busy')
        stack.enter_context(scheduler._jobs_lock(require_cross_process=True))
        current = proposal(window)
        mw.require(review.get('proposal') == current, 'commit_recovery_review_moved')
        data = mw.read_journal(window.journal)
        # Reject a live old keeper/adopter even when its old descriptors are gone.
        owners = [data.get(k) for k in ('keeper', 'controller', 'adopter')]
        owners.append(data['source_transaction'].get('adopter'))
        for owner in filter(None, owners):
            if owner == mw.identity() and mw.locks_verified(data):
                continue
            try:
                live = mw.identity(owner['pid'])
            except FileNotFoundError:
                mw.require(not (Path('/proc') / str(owner['pid'])).exists(), 'commit_recovery_owner_unknown')
            else:
                mw.require(live != owner, 'commit_recovery_owner_alive')
        if not (mw.locks_verified(data) and data['keeper'] == mw.identity()):
            window._take_locks(data)
        mw.require(proposal(window) == current, 'commit_recovery_preimage_moved')

        def drain():
            d = mw.read_journal(window.journal)
            mw.require(mw.locks_verified(d) and d['keeper'] == mw.identity(), 'commit_recovery_locks_lost')
            mw.require(all(window.backend.paused(r['entry'], r['prior']) for r in d['entries']),
                       'commit_recovery_pause_changed')
            mw.require(not window.backend.inflight([r['entry'] for r in d['entries']]),
                       'commit_recovery_reader_not_drained')
            mw.require(mw.json_sha(window.backend.job(JOB)) == current['job_after_sha256'],
                       'commit_recovery_job_moved')
        drain()
        staged = {}
        for name, item in current['files'].items():
            if item['cache']:
                continue
            path = Path(name)
            body = read_regular(Path(item['candidate']))[0]
            mw.require(cohort.digest(body) == item['sha256'], 'commit_recovery_candidate_moved')
            fd, temporary = tempfile.mkstemp(prefix='.glp-commit-recovery-', dir=path.parent)
            stack.callback(os.close, fd)
            stack.callback(lambda p=temporary: os.unlink(p) if os.path.exists(p) else None)
            os.fchown(fd, -1, item['metadata']['gid'])
            cohort.write_all(fd, body); os.fchmod(fd, 0); os.fsync(fd)
            staged[name] = (fd, temporary)
        mw.require(proposal(window) == current, 'commit_recovery_preimage_moved')
        data = mw.read_journal(window.journal)
        switch = data['source_switch']
        switch['reconstruction'] = {'review': review, 'state': 'replacing',
            'metadata': {p: cohort.metadata(os.fstat(fd)) for p, (fd, _) in staged.items()},
            'native_save_replayed': False}
        mw.atomic_json(window.journal, data)  # Preserve original identities and intent.
        try:
            for name, (fd, temporary) in staged.items():
                st = Path(name).lstat()
                mw.require(cohort.metadata(st) == current['files'][name]['metadata']
                           and stat.S_IMODE(st.st_mode) == 0, 'commit_recovery_target_moved')
                os.replace(temporary, name); cohort.sync_dir(Path(name).parent)
            for name, (fd, _) in staged.items():
                mw.require(cohort.digest(cohort.read_all(fd)) == current['files'][name]['sha256']
                           and cohort.metadata(Path(name).lstat()) == cohort.metadata(os.fstat(fd))
                           and stat.S_IMODE(Path(name).lstat().st_mode) == 0, 'commit_recovery_readback_failed')
            drain()
            switch['reconstruction']['state'] = 'verified_sealed'
            mw.atomic_json(window.journal, data)
            for name, (fd, _) in staged.items():
                os.fchmod(fd, current['files'][name]['mode']); os.fsync(fd)
            switch['commit_verified'] = True
            switch['state'] = 'published'
            switch['reconstruction']['state'] = 'complete'
            transaction = data['source_transaction']
            transaction.update(outcome='applied', switch_verified=True)
            data.setdefault('source_sha256', {}).update(transaction['after'])
            for name in transaction['after']:
                data.get('source_updates', {}).pop(name, None)
            data.setdefault('job_outcomes', {})[JOB] = data['job_updates'][JOB]['after_pin']
            mw.atomic_json(window.journal, data)
        except BaseException:
            for fd, _ in staged.values():
                os.fchmod(fd, 0); os.fsync(fd)
            raise  # No automatic replay, rollback, or control restoration.
        return {'reconciled': True, 'side': 'after', 'native_save_replayed': False,
                'review_sha256': mw.json_sha(review), 'source_files': len(staged)}
