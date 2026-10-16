"""A staged file cohort with a kernel DAC read fence during replacement.

Scheduled task admission must already be closed. This is a bounded unprivileged
reader boundary, not isolation against root, hostile same-UID chmod, or code that
already retained source in memory. Drain source descriptors/maps before switching.
Every public member is unreadable before any rename; all bytes are authenticated
before any member reopens. Interrupted switches retain custody and refuse exit.
"""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import stat
import tempfile


def digest(body):
    return hashlib.sha256(body).hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_all(fd, body):
    view = memoryview(body)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError('cohort_short_write')
        view = view[count:]


def read_all(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    parts = []
    while True:
        part = os.read(fd, 65536)
        if not part:
            return b''.join(parts)
        parts.append(part)


def metadata(st):
    return {k: getattr(st, 'st_' + k) for k in ('dev', 'ino', 'uid', 'gid', 'size', 'mtime_ns')}


def switch_files(journal, stage, before, drain, *, commit=None, expected_owners=None):
    """Optionally commit an external pin while sources/caches remain sealed.

    Once commit is entered, ANY failure retains the seal and custody, without
    rollback or replay. The external owner must reconcile its durable outcome;
    ordinary source-only cohort recovery cannot decide a native registry save.
    """
    from maintenance_window import WindowError, atomic_json, read_journal, require
    stage = {Path(p).absolute(): body for p, body in stage.items()}
    before = {Path(p).absolute(): value for p, value in before.items()}
    require(stage and set(stage) == set(before), 'cohort_scope_mismatch')
    require(os.geteuid() != 0, 'cohort_privileged_controller_refused')
    status = Path('/proc/self/status').read_text()
    caps = int(next(line.split()[1] for line in status.splitlines() if line.startswith('CapEff:')), 16)
    require(not caps & ((1 << 1) | (1 << 2)), 'cohort_dac_override_refused')
    expected_owners = expected_owners or {}
    require(set(expected_owners) <= set(stage) & set(before), 'cohort_owner_scope_moved')
    for path, expected in expected_owners.items():
        st = path.lstat()
        require((st.st_uid, st.st_gid) == expected, 'cohort_owner_preimage_moved:' + str(path))
    data = read_journal(journal)
    previous = data.get('source_switch', {})
    files = previous.get('files', {})
    sources = {Path(name): item for name, item in files.items() if not item.get('cache')}
    if (previous.get('state') == 'published' and set(stage) == set(sources)
            and all(digest(stage[path]) == item['before']
                    and digest(before[path][0]) == item['after']
                    and before[path][1] == item['mode'] for path, item in sources.items())):
        # An exact inverse is a rollback of this publication, not a new switch.
        # Reuse its authenticated sources AND removed caches; never replace custody.
        proposal = _recovery_proposal(journal, {'published'})
        return _restore_preimages(journal, proposal, drain, states={'published'})
    # Stale bytecode is another first-read path. Keep exact cache custody for
    # rollback, remove it only after its corresponding new source is complete.
    def cache_paths():
        paths = set()
        for path in stage:
            if path.suffix == '.py':
                candidates = [path.with_suffix('.pyc')]
                candidates += list((path.parent / '__pycache__').glob(path.stem + '.*.pyc'))
                paths.update(p for p in candidates if p.exists())
        return paths

    caches = {p: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) for p in cache_paths()}
    all_before = {**before, **caches}
    data = read_journal(journal)
    custody = Path(tempfile.mkdtemp(prefix='.cohort-', dir=Path(journal).parent))
    os.chmod(custody, 0o700)
    receipt = {'state': 'prepared', 'custody': str(custody), 'files': {}}
    handles, staged = {}, {}
    with ExitStack() as stack:
        for index, (path, (body, mode)) in enumerate(all_before.items()):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            st = os.fstat(fd)
            if path in expected_owners:
                require((st.st_uid, st.st_gid) == expected_owners[path], 'cohort_owner_preimage_moved:' + str(path))
            require(stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and st.st_nlink == 1,
                    'cohort_owner_regular_single_link_required:' + str(path))
            require(st.st_gid in set(os.getgroups()) | {os.getegid()}, 'cohort_group_not_preservable')
            require(stat.S_IMODE(st.st_mode) == mode and read_all(fd) == body,
                    'cohort_preimage_moved:' + str(path))
            handles[path] = fd
            original = custody / (str(index) + '.before')
            with original.open('xb') as out:
                os.fchmod(out.fileno(), 0o600)
                out.write(body)
                out.flush()
                os.fsync(out.fileno())
            item = {'before': digest(body), 'mode': mode, 'original': str(original),
                    'before_metadata': metadata(st), 'cache': path in caches}
            if path in stage:
                # Same directory guarantees rename stays on the target device.
                newfd, name = tempfile.mkstemp(prefix='.glp-cohort-', dir=path.parent)
                stack.callback(os.close, newfd)
                stack.callback(lambda p=name: os.unlink(p) if os.path.exists(p) else None)
                os.fchown(newfd, -1, st.st_gid)
                write_all(newfd, stage[path])
                os.fsync(newfd)
                os.fchmod(newfd, 0)
                staged[path] = (newfd, name)
                item.update(after=digest(stage[path]), after_metadata=metadata(os.fstat(newfd)))
                candidate = custody / (str(index) + '.after')
                with candidate.open('xb') as out:
                    os.fchmod(out.fileno(), 0o600)
                    out.write(stage[path])
                    out.flush()
                    os.fsync(out.fileno())
                item['candidate'] = str(candidate)
            receipt['files'][str(path)] = item
        sync_dir(custody)
        data['source_switch'] = receipt
        atomic_json(journal, data)  # All preimages/stages precede the first fence.
        try:
            receipt['state'] = 'sealing'
            atomic_json(journal, data)
            for path, fd in handles.items():
                require(metadata(path.stat()) == receipt['files'][str(path)]['before_metadata'],
                        'cohort_preimage_moved_before_fence')
                os.fchmod(fd, 0)
                os.fsync(fd)
            require(not drain(), 'cohort_source_reader_not_drained')
            require(cache_paths() == set(caches), 'cohort_cache_set_changed')
            receipt['state'] = 'switching'
            atomic_json(journal, data)
            for path, (fd, name) in staged.items():
                require(stat.S_IMODE(path.stat().st_mode) == 0, 'cohort_fence_changed')
                os.replace(name, path)
                sync_dir(path.parent)
            # Read using descriptors opened BEFORE sealing, never chmod to peek.
            for path, (fd, _) in staged.items():
                os.lseek(fd, 0, os.SEEK_SET)
                require(digest(read_all(fd)) == digest(stage[path])
                        and metadata(path.stat()) == metadata(os.fstat(fd))
                        and stat.S_IMODE(path.stat().st_mode) == 0, 'cohort_readback_failed')
            require(not drain(), 'cohort_source_reader_not_drained')
            require(cache_paths() == set(caches), 'cohort_cache_set_changed')
            receipt['state'] = 'sealed'
            atomic_json(journal, data)
            for cache in caches:
                os.unlink(cache)
                sync_dir(cache.parent)
            if commit is not None:
                receipt['commit_started'] = True
                receipt['state'] = 'commit_uncertain'
                atomic_json(journal, data)  # Durable intent BEFORE native saver.
                commit()
                for path, (fd, _) in staged.items():
                    require(read_all(fd) == stage[path]
                            and metadata(path.stat()) == metadata(os.fstat(fd))
                            and stat.S_IMODE(path.stat().st_mode) == 0,
                            'cohort_postcommit_readback_failed')
                receipt['commit_verified'] = True
                atomic_json(journal, data)
            for path, (fd, _) in staged.items():
                os.fchmod(fd, before[path][1])
                os.fsync(fd)
            receipt['state'] = 'published'
            atomic_json(journal, data)
            return receipt
        except BaseException:
            if receipt.get('commit_started'):
                # A saver can raise after its rename. Do not infer rollback from
                # an unchanged/readable registry, or expose an uncertain carrier.
                for path, (fd, _) in staged.items():
                    os.fchmod(fd, 0)
                    os.fsync(fd)
                receipt['state'] = 'commit_uncertain'
                atomic_json(journal, data)
                raise
            # Never expose a partially rolled-back cohort. An unknown inode or
            # content/metadata change stays sealed for independent recovery.
            try:
                for path, item in receipt['files'].items():
                    target = Path(path)
                    if item['cache'] and not target.exists():
                        continue
                    require(metadata(target.stat()) in [item['before_metadata'], item.get('after_metadata')],
                            'cohort_rollback_conflict')
                    os.chmod(target, 0)
                require(not drain(), 'cohort_rollback_reader_not_drained')
                rollback = {}
                for path, (body, mode) in all_before.items():
                    fd, name = tempfile.mkstemp(prefix='.glp-cohort-rollback-', dir=path.parent)
                    stack.callback(os.close, fd)
                    stack.callback(lambda p=name: os.unlink(p) if os.path.exists(p) else None)
                    os.fchown(fd, -1, receipt['files'][str(path)]['before_metadata']['gid'])
                    write_all(fd, body)
                    os.fsync(fd)
                    os.fchmod(fd, 0)
                    rollback[path] = (fd, name)
                    receipt['files'][str(path)]['rollback_metadata'] = metadata(os.fstat(fd))
                atomic_json(journal, data)
                for path, (fd, name) in rollback.items():
                    os.replace(name, path)
                    sync_dir(path.parent)
                for path, (fd, _) in rollback.items():
                    require(read_all(fd) == all_before[path][0]
                            and metadata(path.stat()) == metadata(os.fstat(fd))
                            and stat.S_IMODE(path.stat().st_mode) == 0, 'cohort_rollback_readback_failed')
                require(not drain(), 'cohort_rollback_reader_not_drained')
                for path, (fd, _) in rollback.items():
                    os.fchmod(fd, all_before[path][1])
                    os.fsync(fd)
                receipt['state'] = 'rolled_back'
                atomic_json(journal, data)
            except Exception:
                receipt['state'] = 'recovery_required'
                atomic_json(journal, data)
                raise WindowError('cohort_switch_uncertain_keep_admission_closed')
            raise


def recovery_proposal(journal):
    """Inspect sealed inode identities and authenticated custody without opening sources."""
    return _recovery_proposal(journal, {'prepared', 'sealing', 'switching', 'sealed',
                                       'recovery_required', 'recovering'})


def _recovery_proposal(journal, states):
    from maintenance_window import json_sha, read_journal, require
    from maxbounty_pin_release import read_regular
    raw = read_regular(Path(journal))[0]
    data = read_journal(journal)
    switch = data.get('source_switch', {})
    require(not switch.get('commit_started'), 'cohort_external_commit_requires_owner_reconciliation')
    require(switch.get('state') in states, 'cohort_recovery_not_required')
    require(isinstance(switch.get('files'), dict) and switch['files'], 'cohort_recovery_files_missing')
    files = {}
    for name, item in switch['files'].items():
        path, original = Path(name), Path(item['original'])
        require(path.is_absolute() and original.parent == Path(switch['custody']), 'cohort_recovery_custody_path')
        body, _ = read_regular(original)
        require(digest(body) == item['before'], 'cohort_recovery_custody_moved:' + name)
        try:
            st = path.lstat()
        except FileNotFoundError:
            require(item.get('cache') is True, 'cohort_recovery_source_missing:' + name)
            current = None
        else:
            require(stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and st.st_nlink == 1,
                    'cohort_recovery_unsafe_target:' + name)
            require(metadata(st) in [item.get(key) for key in ('before_metadata', 'after_metadata',
                                                              'rollback_metadata', 'recovery_metadata')],
                    'cohort_recovery_target_moved:' + name)
            require(stat.S_IMODE(st.st_mode) in (0, item['mode']), 'cohort_recovery_mode_moved:' + name)
            current = {'metadata': metadata(st), 'mode': stat.S_IMODE(st.st_mode)}
        files[name] = {'current': current, 'before_sha256': item['before'], 'mode': item['mode'],
                       'original': str(original), 'gid': item['before_metadata']['gid']}
    require(read_regular(Path(journal))[0] == raw, 'cohort_recovery_journal_moved')
    # The CLI separately authenticates replacement lock ownership. A dead keeper
    # can be replaced without changing the reviewed pauses, transaction or custody.
    stable = {key: value for key, value in data.items()
              if key not in {'keeper', 'locks', 'controller', 'adopter', 'exit_requested'}}
    return {'schema': 'glp-maintenance-cohort-recovery-proposal-v1',
            'journal_sha256': json_sha(stable), 'files': files}


def recover(journal, review, drain):
    """Restore a reviewed preimage while the caller retains paused task admission."""
    from maintenance_window import require
    require(isinstance(review, dict)
            and review.get('schema') == 'glp-maintenance-cohort-recovery-review-v1'
            and review.get('decision') == 'RESTORE_EXACT_COHORT_PREIMAGE'
            and isinstance(review.get('prepared_by'), str) and review['prepared_by'].strip()
            and isinstance(review.get('reviewer'), str) and review['reviewer'].strip()
            and review['prepared_by'].strip() != review['reviewer'].strip(), 'cohort_recovery_review_required')
    proposal = recovery_proposal(journal)
    require(review.get('proposal') == proposal, 'cohort_recovery_review_moved')
    return _restore_preimages(journal, proposal, drain, review=review)


def _restore_preimages(journal, proposal, drain, *, states=None, review=None):
    """Use the original custody and DAC fence for rollback or reviewed recovery."""
    import json
    from maintenance_window import atomic_json, read_journal, require
    from maxbounty_pin_release import read_regular
    require(os.geteuid() != 0, 'cohort_privileged_controller_refused')
    caps = int(next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines()
                    if line.startswith('CapEff:')), 16)
    require(not caps & ((1 << 1) | (1 << 2)), 'cohort_dac_override_refused')
    require(not drain(), 'cohort_recovery_reader_not_drained')
    data = read_journal(journal)
    switch, staged = data['source_switch'], {}
    with ExitStack() as stack:
        for name, item in proposal['files'].items():
            path = Path(name)
            body = read_regular(Path(item['original']))[0]
            require(digest(body) == item['before_sha256'], 'cohort_recovery_custody_moved')
            fd, temporary = tempfile.mkstemp(prefix='.glp-cohort-recovery-', dir=path.parent)
            stack.callback(os.close, fd)
            stack.callback(lambda p=temporary: os.unlink(p) if os.path.exists(p) else None)
            os.fchown(fd, -1, item['gid'])
            with os.fdopen(os.dup(fd), 'wb') as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.fchmod(fd, 0)
            os.fsync(fd)
            staged[name] = (fd, temporary)
            switch['files'][name]['recovery_metadata'] = metadata(os.fstat(fd))
        current = _recovery_proposal(journal, states) if states is not None else recovery_proposal(journal)
        require(current == proposal, 'cohort_recovery_preimage_moved')
        switch['state'] = 'recovering'
        if review is not None:
            switch['recovery_review_sha256'] = digest(json.dumps(review, sort_keys=True).encode())
        atomic_json(journal, data)  # Stage identities survive a second process/power loss.
        for name, item in proposal['files'].items():
            path = Path(name)
            if item['current'] is None:
                require(not path.exists() and not path.is_symlink(), 'cohort_recovery_target_moved')
            else:
                st = path.lstat()
                require({'metadata': metadata(st), 'mode': stat.S_IMODE(st.st_mode)} == item['current'],
                        'cohort_recovery_target_moved')
                os.chmod(path, 0, follow_symlinks=False)
        require(not drain(), 'cohort_recovery_reader_not_drained')
        for name, (fd, temporary) in staged.items():
            path, current = Path(name), proposal['files'][name]['current']
            if current is None:
                require(not path.exists() and not path.is_symlink(), 'cohort_recovery_target_moved')
            else:
                require(metadata(path.lstat()) == current['metadata']
                        and stat.S_IMODE(path.lstat().st_mode) == 0, 'cohort_recovery_target_moved')
            os.replace(temporary, path)
            sync_dir(path.parent)
        for name, (fd, _) in staged.items():
            require(digest(os.pread(fd, os.fstat(fd).st_size + 1, 0)) == proposal['files'][name]['before_sha256']
                    and metadata(Path(name).lstat()) == metadata(os.fstat(fd))
                    and stat.S_IMODE(Path(name).lstat().st_mode) == 0, 'cohort_recovery_readback_failed')
        require(not drain(), 'cohort_recovery_reader_not_drained')
        for name, (fd, _) in staged.items():
            os.fchmod(fd, proposal['files'][name]['mode'])
            os.fsync(fd)
        switch['state'] = 'rolled_back'
        if review is not None:
            switch['recovery'] = {'proposal': proposal, 'review_sha256': switch['recovery_review_sha256']}
        else:
            switch['rollback'] = {'proposal': proposal}
        atomic_json(journal, data)
        return switch
