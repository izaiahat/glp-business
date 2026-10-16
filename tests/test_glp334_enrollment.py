"""Exact target regressions. Native metadata is nonsecret; all writes disposable.

Git blobs are genuine, not re-pinned synthetic wrapper fixtures. The optional
full retained-input rehearsal uses only an explicit local read-only fixture root;
CI does not pretend the private untracked reports are available.
"""
import copy
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest

import glp44_scoped_adoption as pair
import glp_ship as ship
from glp_ship_generation import GitGeneration, json_sha
from glp_ship_registry import (registered_job, enrollment_hashes, GLP334_JOBS,
                               GLP334_SOURCE, GLP334_PREDECESSOR, GLP334_PATHS)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def effects_denied(monkeypatch):
    if not os.environ.get('GLP334_EFFECT_DENIAL'):
        return
    import socket
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('network effect denied'))
    popen = subprocess.Popen
    def read_only_git(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv[:2] == ['git', '-C']
        assert argv[3] in {'show', 'diff', 'ls-tree', 'merge-base', 'rev-parse', 'ls-files', 'cat-file'}
        return popen(argv, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', read_only_git)


def blob(commit, path):
    return subprocess.check_output(['git', '-C', str(ROOT), 'show', commit + ':' + path])


def native_metadata():
    # Authenticated receiving metadata; no prompt, provider config or credentials.
    return [{'id': jid, 'script': name, 'script_sha256': old,
             'enabled': True, 'state': 'scheduled', 'workdir': '/home/ubuntu/business',
             'no_agent': True, 'script_timeout_seconds': timeout,
             'schedule': {'kind': 'cron', 'expr': cron, 'display': cron}, 'fire_claim': None}
            for jid, name, old, timeout, cron in GLP334_JOBS]


def native_fixture(tmp_path):
    home = tmp_path / 'home'
    (home / 'scripts').mkdir(parents=True)
    (home / 'cron').mkdir()
    for jid, name, old, *_ in GLP334_JOBS:
        path = home / 'scripts' / name
        path.write_bytes(blob(GLP334_PREDECESSOR, 'ops/hermes-cron-scripts/' + name))
        # External receiving lstat, NOT Git's 100755 source snapshot mode.
        path.chmod(0o700 if jid == 'aadf6677c7f0' else 0o755)
        assert pair.digest(path.read_bytes()) == old
    registry = home / 'cron/jobs.json'
    registry.write_text(json.dumps({'jobs': native_metadata() + [{'id': 'unrelated', 'schedule': 'retained'}]}))
    return home, registry


@pytest.mark.parametrize('job_id', ['aadf6677c7f0', '2ca359464bfa'])
def test_real_registered_target_selects_paired_j7_and_retained_consumer(job_id):
    spec = registered_job(job_id)
    assert spec.name == 'GLP334'
    assert spec.kind == 'reporting-enrollment'
    assert spec.tier == 'A'
    assert spec.jobs == ('aadf6677c7f0', '2ca359464bfa')
    assert spec.wrappers == ('glp_affiliate_weekly_reporting_j7.sh', 'glp334_live_reconciliation.sh')
    assert spec.paths == GLP334_PATHS and len(spec.paths) == 17
    assert spec.window


def test_literal_identity_is_normalized_before_tier_dispatch():
    for jid, *_ in GLP334_JOBS:
        for value in (jid, jid.upper(), ''.join(c.upper() if i % 2 else c for i, c in enumerate(jid))):
            assert registered_job(value) == registered_job(jid)
            assert registered_job(value).kind == 'reporting-enrollment'
            assert registered_job(value).tier == 'A'
    assert registered_job('GLP333').kind == 'reporting'
    assert registered_job('GLP333').wrappers == ('glp_affiliate_weekly_reporting.sh',)


def test_finite_union_of_both_authenticated_owned_path_lists():
    analytics = json.loads(blob(GLP334_SOURCE, 'ops/glp/reporting-v2/glp334-install-cohort.json'))['selected_paths']
    # The PR550 owned source diff excludes only its packaging manifest.
    owned = subprocess.check_output(['git', '-C', str(ROOT), 'diff', '--name-only',
        'b34c5de8fddcbd0bb8b9ecf1ebc04959064b4f4f',
        '064a9c433c6f473d746aa62aac2a01fbfbcaa388'], text=True).splitlines()
    owned.remove('ops/rebuild-source-manifest.json')
    assert len(owned) == 11
    assert len(set(analytics) & set(owned)) == 8
    assert set(GLP334_PATHS) == set(analytics) | set(owned)
    assert len(GLP334_PATHS) == 17


@pytest.mark.parametrize('fault', ['extra', 'missing', 'wrong_source', 'normalize_mode', 'financial_guard'])
def test_union_refuses_unrelated_missing_or_unaccepted_declaration(monkeypatch, fault):
    from glp_ship_registry import receiving_cohort, GLP334_UNION
    path = ROOT / GLP334_UNION
    declaration = json.loads(path.read_bytes())
    if fault == 'extra': declaration['selected_paths'].append('scripts/unrelated.py')
    elif fault == 'missing': declaration['selected_paths'].pop()
    elif fault == 'wrong_source': declaration['sources']['PR550']['accepted_head'] = GLP334_PREDECESSOR
    elif fault == 'normalize_mode': declaration['preserved_destination_modes'] = {}
    else: declaration['maintained_successor']['sha256'] = '0' * 64
    original = Path.read_bytes
    monkeypatch.setattr(Path, 'read_bytes', lambda self: json.dumps(declaration).encode() if self == path else original(self))
    with pytest.raises(RuntimeError, match='glp334_finite_receiving_union_moved'):
        receiving_cohort(ROOT)


def test_genuine_old_external_native_identities_and_reviewed_successors(tmp_path):
    home, registry = native_fixture(tmp_path)
    hashes = enrollment_hashes(ROOT)
    for jid, name, old, *_ in GLP334_JOBS:
        snap = 'ops/hermes-cron-scripts/' + name
        plan, before = pair._target_preimage(tmp_path, home, registry, None, jid, name, snap, ROOT)
        assert before[0] == blob(GLP334_PREDECESSOR, snap)
        assert before[1] == (0o700 if jid == 'aadf6677c7f0' else 0o755)
        assert plan['wrapper_before_mode'] == before[1]
        assert plan['wrapper_before_owner'] == {'uid': os.geteuid(), 'gid': os.getegid()}
        assert plan['job_before']['script_sha256'] == old
        assert hashes[snap] == pair.digest(blob(GLP334_SOURCE, snap)) != old
    assert hashes['ops/hermes-cron-scripts/glp_affiliate_weekly_reporting_j7.sh'] == '9caf9e6ddec18de5dcec393377dda87bc5b27e4fed30abea736f804c8c53f602'
    assert hashes['ops/hermes-cron-scripts/glp334_live_reconciliation.sh'] == 'a9c71fbd7afa4971eca21436facad2a09eb20ff91de96a15ce4866a1b23194f8'


@pytest.mark.parametrize('field,value', [('id', 'aadf6677c7f1'),
    ('script', 'glp_affiliate_weekly_reporting.sh'),
    ('workdir', '/unrelated'), ('no_agent', False), ('script_timeout_seconds', 301),
    ('schedule', {'kind': 'cron', 'expr': '0 0 * * *'}), ('running', True),
    ('fire_claim', {'owner': 'other'}), ('script_sha256', '0' * 64)])
def test_wrong_j7_or_native_identity_refuses_without_writes(tmp_path, field, value):
    home, registry = native_fixture(tmp_path)
    data = json.loads(registry.read_text())
    data['jobs'][0][field] = value
    registry.write_text(json.dumps(data))
    before = registry.read_bytes()
    jid, name, *_ = GLP334_JOBS[0]
    with pytest.raises(RuntimeError):
        pair._target_preimage(tmp_path, home, registry, None, jid, name,
                              'ops/hermes-cron-scripts/' + name, ROOT)
    assert registry.read_bytes() == before


@pytest.mark.parametrize('index,mode', [(0, m) for m in (0o755, 0o750, 0o600, 0o777, 0o4700, 0o2700, 0o1700)]
                         + [(1, m) for m in (0o700, 0o750, 0o644, 0o777, 0o4755)])
def test_exact_external_mode_contract_refuses_all_other_modes(tmp_path, index, mode):
    home, registry = native_fixture(tmp_path)
    jid, name, *_ = GLP334_JOBS[index]
    wrapper = home / 'scripts' / name
    wrapper.chmod(mode)  # Disposable copy only.
    before = (registry.read_bytes(), pair.read_regular(wrapper), wrapper.lstat())
    with pytest.raises(RuntimeError, match='glp334_genuine_target_predecessor_required:' + jid):
        pair._target_preimage(tmp_path, home, registry, None, jid, name,
                              'ops/hermes-cron-scripts/' + name, ROOT)
    assert registry.read_bytes() == before[0] and pair.read_regular(wrapper) == before[1]
    assert wrapper.lstat() == before[2]


@pytest.mark.parametrize('fault', ['id', 'name', 'snapshot', 'uid', 'gid', 'hardlink'])
def test_enrollment_target_tuple_and_owner_are_not_caller_waivers(tmp_path, monkeypatch, fault):
    home, registry = native_fixture(tmp_path)
    jid, name, *_ = GLP334_JOBS[0]
    wrapper = home / 'scripts' / name
    snap = 'ops/hermes-cron-scripts/' + name
    if fault in ('uid', 'gid'):
        lstat = Path.lstat
        def changed_owner(path, *args, **kwargs):
            observed = lstat(path, *args, **kwargs)
            if path == wrapper:
                fields = list(observed); fields[4 if fault == 'uid' else 5] += 1
                return os.stat_result(fields)
            return observed
        monkeypatch.setattr(Path, 'lstat', changed_owner)
    elif fault == 'hardlink': os.link(wrapper, home / 'extra-link')
    else:
        if fault == 'id':
            document = json.loads(registry.read_bytes())
            document['jobs'][0]['id'] = jid = 'aadf6677c7f1'
            registry.write_text(json.dumps(document))
        elif fault == 'name':
            changed = 'other.sh'; wrapper.rename(home / 'scripts' / changed)
            document = json.loads(registry.read_bytes()); document['jobs'][0]['script'] = changed
            registry.write_text(json.dumps(document)); name = changed
        else: snap = 'ops/hermes-cron-scripts/' + GLP334_JOBS[1][1]
    before = registry.read_bytes()
    with pytest.raises(RuntimeError, match='glp334_genuine_target_predecessor_required:' + jid):
        pair._target_preimage(tmp_path, home, registry, None, jid, name, snap, ROOT)
    assert registry.read_bytes() == before


@pytest.mark.parametrize('fault', ['drift', 'absent', 'symlink', 'already_successor'])
def test_missing_drift_or_replay_external_custody_refused(tmp_path, fault):
    home, registry = native_fixture(tmp_path)
    jid, name, *_ = GLP334_JOBS[1]
    wrapper = home / 'scripts' / name
    if fault == 'drift': wrapper.write_bytes(wrapper.read_bytes() + b'\n')
    elif fault == 'absent': wrapper.unlink()
    elif fault == 'symlink':
        wrapper.unlink(); wrapper.symlink_to(ROOT / 'ops/hermes-cron-scripts' / name)
    else:
        wrapper.write_bytes(blob(GLP334_SOURCE, 'ops/hermes-cron-scripts/' + name))
        data = json.loads(registry.read_text())
        data['jobs'][1]['script_sha256'] = pair.digest(wrapper.read_bytes())
        registry.write_text(json.dumps(data))
    with pytest.raises((RuntimeError, OSError)):
        pair._target_preimage(tmp_path, home, registry, None, jid, name,
                              'ops/hermes-cron-scripts/' + name, ROOT)


def test_no_other_registered_id_or_cohort_and_static_inventory(tmp_path):
    from maintenance_inventory import affected_paths
    for value in ('2ca359464bfb', 'aadf6677c7f1', 'glp334', 'all', 'fleet'):
        with pytest.raises(RuntimeError, match='unregistered_ship_job'):
            registered_job(value)
    paths = affected_paths(tmp_path / 'live', tmp_path / 'home')
    assert {tmp_path / 'live' / p for p in GLP334_PATHS} <= paths
    assert {tmp_path / 'home/scripts' / r[1] for r in GLP334_JOBS} <= paths


def test_enrollment_route_never_calls_registered_runtime(tmp_path, monkeypatch):
    # Exercise the real caller routing after a simulated maintained adopter;
    # production runtime must remain unreachable for this registered kind.
    import maintenance_window as mw
    import glp_ship_runtime as runtime
    class Window:
        def __init__(self, *a): pass
        def enter(self): pass
        def exit(self): pass
        def status(self): return {'state': 'closed'}
    monkeypatch.setattr(mw, 'MaintenanceWindow', Window)
    monkeypatch.setattr(mw, 'require_active_window', lambda *a: nullcontext(None))
    monkeypatch.setattr(ship, 'preview', lambda *a, **kw: {})
    monkeypatch.setattr(ship, 'zero_diff', lambda *a, **kw: None)
    monkeypatch.setattr(pair, 'adopt', lambda *a, **kw: {'applied': True})
    monkeypatch.setattr(runtime, 'run_registered', lambda *a, **kw: pytest.fail('native replay'))
    generation = SimpleNamespace(source='fixture', proposal={})
    spec = registered_job(GLP334_JOBS[0][0])
    result = ship._apply_and_run(tmp_path, tmp_path, tmp_path, spec, generation, {},
                                  tmp_path, tmp_path / 'window.json', object(), object())
    assert result['runs'] == [] and result['policy'] == 'ENROLLMENT_ONLY_NO_EXECUTION'


def test_genuine_union_projection_closes_installed_dependencies_without_receiving():
    origin = os.environ.get('GLP334_RETAINED_FIXTURE_ROOT')
    if not origin:
        pytest.skip('explicit local retained fixture root required')
    from glp_ship_registry import receiving_cohort
    import glp334_pin_closure as closure
    cohort = receiving_cohort(ROOT)
    for rel in cohort['selected_paths']:
        assert (ROOT / rel).read_bytes() == blob(GLP334_SOURCE, rel)
    result = closure.project(ROOT, Path(origin), cohort)
    assert result['status'] == 'READY_SOURCE_ONLY' and result['drifts'] == []
    assert len(result['selected_paths']) == 17
    assert result['effects'] == 'NONE_READ_ONLY_PROJECTION_NOT_INSTALLATION'


def retained_fixture(tmp_path, monkeypatch):
    origin = os.environ.get('GLP334_RETAINED_FIXTURE_ROOT')
    if not origin:
        pytest.skip('full genuine retained-input fixture requires explicit local read-only root')
    origin = Path(origin)
    live = tmp_path / 'live'
    live.mkdir()
    from glp_ship_registry import receiving_cohort
    co = receiving_cohort(ROOT)
    paths = set(GLP334_PATHS)
    for name in ('', '-j5', '-j6', '-j7'):
        rel = 'ops/glp/reporting-v2/source-manifest' + name + '.json'
        paths.update(str(Path(p).relative_to('/home/ubuntu/business'))
                     for p in json.loads(blob(GLP334_SOURCE, rel))['files'])
    for rel in [p for p in GLP334_PATHS if p.endswith('.sh')]:
        text = blob(GLP334_SOURCE, rel).decode()
        paths.update(re.findall(r'^check_source (\S+) ', text, re.M))
        paths.update(str(Path(p).relative_to('/home/ubuntu/business')) for p in
                     re.findall(r'--(?:xentraffic-intake|eden-intake|statement-intake) (\S+)', text))
    for rel in paths:
        target = live / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob(GLP334_SOURCE, rel) if rel in GLP334_PATHS else (origin / rel).read_bytes())
        target.chmod(co['preserved_destination_modes'].get(rel,
            int(subprocess.check_output(['git', '-C', str(ROOT), 'ls-tree', GLP334_SOURCE, '--', rel]).split()[0], 8) & 0o777)
            if rel in GLP334_PATHS else (origin / rel).stat().st_mode & 0o777)
    # Disposable post-receiving state, never the installed source baseline.
    hashes = {rel: pair.digest((live / rel).read_bytes()) for rel in GLP334_PATHS}
    (live / pair.syncer.STATE).write_text(json.dumps({'hashes': hashes}))
    (live / pair.syncer.MANIFEST).write_bytes(blob(GLP334_SOURCE, pair.syncer.MANIFEST))
    home, registry = native_fixture(tmp_path)
    # Real full planning over this disposable root; no host scheduler commands.
    actions = live / 'ops/n8n-bridge/actions.json'
    actions.parent.mkdir(parents=True, exist_ok=True)
    actions.write_text('{}')
    import maintenance_window as mw
    def scheduler_probe(self, argv, payload=None):
        assert payload is None
        if argv == ['/usr/bin/crontab', '-l'] or any(k in argv for k in ('list-units', 'list-unit-files')):
            return ''
        if 'show' in argv and argv[3] == 'n8n-bridge.service':
            return 'Id=n8n-bridge.service\nLoadState=loaded\nActiveState=inactive\nExecStart={ path=/usr/bin/true ; argv[]=/usr/bin/true ; }\n'
        raise AssertionError(argv)
    monkeypatch.setattr(mw.LinuxBackend, 'command', scheduler_probe)
    spec = registered_job(GLP334_JOBS[0][0])
    generation = ship.prepare(ROOT, live, home, spec,
        subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(), {}, 'offline-preparer')
    return live, home, registry, spec, generation


@pytest.mark.parametrize('fault', ['success', 'contention', 'wrapper1-before', 'wrapper1-after',
    'wrapper2-before', 'wrapper2-after', 'native-before', 'native-after', 'record1', 'record2',
    'restoration', 'inherited', 'foreign', 'closed', 'thread', 'entry',
    'mode-reviewed', 'mode-custody', 'mode-switch', 'owner-custody', 'owner-switch', 'owner-open', 'full-record-reviewed'])
def test_cleanup_singleton_covers_real_enrollment_effects(tmp_path, monkeypatch, fault):
    """Real kernel contention and maintained switch; only scheduling is private."""
    import fcntl
    import threading
    from contextlib import ExitStack
    import maintenance_window as mw
    import maintenance_cohort as cohort
    live, home, reg, spec, generation = retained_fixture(tmp_path, monkeypatch)
    # bwrap maps only one GID. Model a preservable supplementary-group drift at
    # the exact stat/fstat/fchown seam; all bytes/modes/locks/saves remain real.
    foreign_gid = os.getegid() + 1
    changed_owners = {}
    real_stat, real_fstat, real_fchown = Path.stat, os.fstat, os.fchown
    def owner_stat(st):
        if (st.st_dev, st.st_ino) not in changed_owners: return st
        values = list(st); values[5] = foreign_gid
        return os.stat_result(values, {k: getattr(st, k) for k in ('st_atime_ns', 'st_mtime_ns', 'st_ctime_ns')})
    def drift_owner(path):
        st = real_stat(path); changed_owners[st.st_dev, st.st_ino] = foreign_gid
    def owner_chown(fd, uid, gid):
        if gid == foreign_gid:
            st = real_fstat(fd); changed_owners[st.st_dev, st.st_ino] = foreign_gid
            gid = os.getegid()
        return real_fchown(fd, uid, gid)
    if fault.startswith('owner-'):
        groups = os.getgroups()
        monkeypatch.setattr(os, 'getgroups', lambda: groups + [foreign_gid])
        monkeypatch.setattr(Path, 'stat', lambda p, **kw: owner_stat(real_stat(p, **kw)))
        monkeypatch.setattr(os, 'fstat', lambda fd: owner_stat(real_fstat(fd)))
        monkeypatch.setattr(os, 'fchown', owner_chown)
    old = json.loads(reg.read_bytes())['jobs']
    paused = copy.deepcopy(old)
    for row in paused[:2]: row.update(enabled=False, state='paused')
    reg.write_text(json.dumps({'jobs': paused}))
    journal = tmp_path / 'window.json'
    journal.write_text(json.dumps({'schema': 'glp44-glp73-maintenance-window-v1', 'entries': [
        {'entry': {'kind': 'hermes', 'id': row['id']}, 'prior': {'fields': {
            k: row[k] for k in mw.CONTROL_FIELDS if k in row}, 'script_sha256': row['script_sha256']}}
        for row in old[:2]]}))
    cleanup = home / 'runstate/host-disk-cleanup.lock'
    cleanup.parent.mkdir(exist_ok=True)
    tid = threading.get_ident()
    events = []
    def held(path):
        if not path.exists(): return False
        fd = os.open(path, os.O_RDONLY)
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: return True
        finally:
            os.close(fd)
        return False
    @contextmanager
    def lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+') as h:
            fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield h
    locks = mw.default_locks(live, home)
    wrappers = [home / 'scripts' / name for name in spec.wrappers]
    if fault == 'mode-reviewed': wrappers[0].chmod(0o755)
    if fault == 'full-record-reviewed':
        rows = json.loads(reg.read_bytes()); rows['jobs'][0]['last_status'] = 'drift'
        reg.write_text(json.dumps(rows))
    def witness(edge, native=True):
        assert threading.get_ident() == tid
        assert held(cleanup), 'cleanup singleton absent at ' + edge
        assert held(live) and all(held(p) for p in locks)
        if native:
            assert held(home / 'cron/.jobs.lock')
            assert all(held(home / 'cron' / ('fire-' + jid + '.lock')) for jid in spec.jobs)
        events.append(edge)
    class Scheduler:
        saves = 0
        fires = set()
        def _current_cron_store(self): return SimpleNamespace(jobs_file=reg)
        def _jobs_lock_file(self): return reg.parent / '.jobs.lock'
        @contextmanager
        def _fire_job_lock(self, jid):
            assert held(cleanup), 'cleanup must precede native fire acquisition'
            if jid in self.fires:
                yield True
                return
            with lock(home / 'cron' / ('fire-' + jid + '.lock')):
                self.fires.add(jid)
                try: yield True
                finally: self.fires.remove(jid)
        @contextmanager
        def _jobs_lock(self, *, require_cross_process):
            assert require_cross_process
            with lock(reg.parent / '.jobs.lock'): yield
        def _record_load_stamp(self, _): pass
        def _save_jobs_unlocked(self, jobs):
            witness('native-save')
            self.saves += 1
            if fault == 'native-before': raise OSError('native-before')
            reg.write_text(json.dumps({'jobs': jobs}))
            if fault == 'native-after': raise OSError('native-after')
    scheduler = Scheduler()
    class Backend:
        root = live; hermes = home
        def __init__(self, *a): pass
        def scheduler(self): return scheduler
        def job(self, jid): return copy.deepcopy(pair.job_record(reg, jid)[0])
        def paused(self, entry, prior): return self.job(entry['id'])['enabled'] is False
        def inflight(self, entries): return []
    active = mw.ActiveWindow(journal, Backend())
    monkeypatch.setattr(mw, 'locks_verified', lambda _: all(held(p) for p in locks))
    @contextmanager
    def active_window(*a):
        try:
            yield active
            active.finish(True)
        except BaseException:
            active.finish(False)
            raise
    monkeypatch.setattr(mw, 'require_active_window', active_window)
    class Window:
        def __init__(self, *a): pass
        def enter(self):
            witness('window-enter', False)
            if fault == 'entry': reg.write_text(json.dumps({'jobs': paused}))
        def exit(self):
            witness('window-exit', False)
            if fault == 'restoration': raise OSError('restoration')
        def status(self):
            witness('window-readback', False)
            return {'state': 'closed'}
    monkeypatch.setattr(mw, 'MaintenanceWindow', Window)
    replace = cohort.os.replace
    monkeypatch.setattr(mw, 'LinuxBackend', Backend)
    triggered = False
    def checked_replace(src, dst, *a, **kw):
        nonlocal triggered
        if Path(dst) not in wrappers: return replace(src, dst, *a, **kw)
        index = wrappers.index(Path(dst)) + 1
        witness('wrapper' + str(index))
        selected = not triggered and fault.startswith('wrapper' + str(index))
        if selected:
            triggered = True
            if fault.endswith('before'): raise OSError(fault)
        result = replace(src, dst, *a, **kw)
        if fault == 'record' + str(index):
            rows = json.loads(reg.read_bytes()); rows['jobs'][1]['extra_field'] = 'drift'
            reg.write_text(json.dumps(rows))
        if selected and fault.endswith('after'): raise OSError(fault)
        return result
    monkeypatch.setattr(cohort.os, 'replace', checked_replace)
    if fault in ('mode-custody', 'owner-custody'):
        preserve = pair.preserve_custody
        def drift_after_custody(*args, **kwargs):
            result = preserve(*args, **kwargs)
            if fault == 'mode-custody': wrappers[0].chmod(0o755)
            else: drift_owner(wrappers[0])
            return result
        monkeypatch.setattr(pair, 'preserve_custody', drift_after_custody)
    if fault in ('mode-switch', 'owner-switch'):
        switch = active.switch_files
        def drift_at_switch(*args, **kwargs):
            if fault == 'mode-switch': wrappers[0].chmod(0o755)
            else: drift_owner(wrappers[0])
            return switch(*args, **kwargs)
        monkeypatch.setattr(active, 'switch_files', drift_at_switch)
    if fault == 'owner-open':
        opened = cohort.os.open
        def drift_at_open(path, flags, *args, **kwargs):
            if Path(path) == wrappers[0] and flags == os.O_RDONLY | os.O_NOFOLLOW:
                drift_owner(wrappers[0])
            return opened(path, flags, *args, **kwargs)
        monkeypatch.setattr(cohort.os, 'open', drift_at_open)
    review = {'schema': 'glp-ship-review-v1', 'prepared_by': 'offline-preparer',
              'reviewer': 'independent-fixture', 'decision': 'ACCEPT_EXACT_PROPOSAL',
              'proposal_sha256': json_sha(generation.proposal)}
    preimages = {p: pair.read_regular(p) for p in wrappers}
    before = reg.read_bytes()
    source = {p: pair.read_regular(live / p) for p in GLP334_PATHS + (pair.syncer.STATE,)}
    def run(owner=None):
        if fault == 'entry':
            reg.write_text(json.dumps({'jobs': old}))
            return ship.apply_generation(ROOT, live, home, spec, generation, review, tmp_path)
        return ship._apply_and_run(ROOT, live, home, spec, generation, review, tmp_path,
                                   journal, Backend(), scheduler, _cleanup_owner=owner)
    with ExitStack() as stack:
        for path in locks: stack.enter_context(lock(path))
        if fault in ('contention', 'foreign'):
            foreign = stack.enter_context(lock(cleanup))
            with pytest.raises(RuntimeError, match='cleanup'):
                if fault == 'foreign':
                    with pair.cleanup_session(home, foreign): pass
                else: run()
            assert held(cleanup) and not foreign.closed
            assert not events and scheduler.saves == 0 and reg.read_bytes() == before
        elif fault in ('inherited', 'closed', 'thread'):
            with pair.cleanup_session(home) as owner:
                if fault == 'inherited':
                    result = run(owner)
                    assert result['runs'] == [] and held(cleanup)
                elif fault == 'thread':
                    errors = []
                    def other():
                        try:
                            with pair.cleanup_session(home, owner): pass
                        except RuntimeError as exc: errors.append(str(exc))
                    thread = threading.Thread(target=other); thread.start(); thread.join(3)
                    assert not thread.is_alive() and len(errors) == 1
                    assert held(cleanup)
            if fault == 'closed':
                with pytest.raises(RuntimeError, match='cleanup'):
                    with pair.cleanup_session(home, owner): pass
        elif fault in ('success', 'entry'):
            result = run()
            assert result['runs'] == [] and scheduler.saves == 1
            assert {'wrapper1', 'wrapper2', 'native-save', 'window-exit', 'window-readback'} <= set(events)
            assert [p.stat().st_mode & 0o7777 for p in wrappers] == [0o700, 0o755]
            assert [(p.stat().st_uid, p.stat().st_gid) for p in wrappers] == [(os.geteuid(), os.getegid())] * 2
        else:
            refusal = {'mode-reviewed': 'glp334_genuine_target_predecessor_required:',
                       'mode-custody': 'preimage_moved_after_custody',
                       'mode-switch': 'rollback_conflict_no_overwrite:source_cohort',
                       'owner-custody': 'glp334_genuine_target_predecessor_required:',
                       'owner-switch': 'cohort_owner_preimage_moved:',
                       'owner-open': 'cohort_owner_preimage_moved:',
                       'full-record-reviewed': 'ship_job_preimage_moved:'}.get(fault)
            with pytest.raises((RuntimeError, OSError), match=refusal) as caught: run()
            if fault == 'mode-switch':
                # The cohort first refuses its full mode CAS. The adopter then
                # refuses to overwrite the foreign mode during inverse cleanup.
                assert 'cohort_preimage_moved:' in str(caught.value.__context__)

            assert 'window-exit' in events
            if fault.startswith(('mode-', 'owner-')) or fault == 'full-record-reviewed':
                assert scheduler.saves == 0 and reg.read_bytes() == before
                assert not any(e in events for e in ('wrapper1', 'wrapper2', 'native-save'))
                assert [p.stat().st_mode & 0o7777 for p in wrappers] == ([0o755, 0o755] if fault.startswith('mode-') else [0o700, 0o755])
                if fault.startswith('owner-'):
                    assert wrappers[0].stat().st_gid == foreign_gid
            if fault.startswith('wrapper'):
                assert scheduler.saves == 0 and reg.read_bytes() == before
                assert {p: pair.read_regular(p) for p in wrappers} == preimages
                assert [p.stat().st_mode & 0o7777 for p in wrappers] == [0o700, 0o755]
                assert [(p.stat().st_uid, p.stat().st_gid) for p in wrappers] == [(os.geteuid(), os.getegid())] * 2
            if fault.startswith('native') or fault.startswith('record'):
                sw = mw.read_journal(journal)['source_switch']
                assert sw['commit_started'] and sw['state'] == 'commit_uncertain'
                assert all(p.stat().st_mode & 0o777 == 0 for p in wrappers)
                saves = scheduler.saves
                with pytest.raises((RuntimeError, OSError)): run()
                assert scheduler.saves == saves
                with pytest.raises(RuntimeError, match='external_commit'): cohort.recovery_proposal(journal)
    assert not held(cleanup), 'cleanup lease leaked on return/exception'
    assert {p: pair.read_regular(live / p) for p in source} == source


def test_genuine_complete_offline_enrollment_and_source_unchanged(tmp_path, monkeypatch):
    from maintenance_cohort import switch_files
    import maintenance_window as mw
    live, home, registry, spec, generation = retained_fixture(tmp_path, monkeypatch)
    original = copy.deepcopy(json.loads(registry.read_text())['jobs'])
    current = copy.deepcopy(original)
    for row in current[:2]: row.update(enabled=False, state='paused')
    registry.write_text(json.dumps({'jobs': current}))
    journal = tmp_path / 'window.json'
    journal.write_text('{"schema":"glp44-glp73-maintenance-window-v1"}')
    source_before = {p: pair.read_regular(live / p) for p in GLP334_PATHS + (pair.syncer.STATE, pair.syncer.MANIFEST)}
    events = []
    class Window:
        def __init__(self): self.journal = journal
        def original_job(self, jid): return next(copy.deepcopy(j) for j in original if j['id'] == jid)
        def current_job(self, jid): return pair.job_record(registry, jid)[0]
        def accept_job_update(self, jid, before, after):
            assert {k:v for k,v in before.items() if k != 'script_sha256'} == {k:v for k,v in after.items() if k != 'script_sha256'}
            events.append(('intent', jid))
        def accept_file_updates(self, before, after):
            assert set(before) == {str((home / 'scripts' / name).resolve()) for _, name, *_ in GLP334_JOBS}
        def switch_files(self, stage, before, commit=None, **kwargs):
            return switch_files(journal, stage, before, lambda: [], commit=commit, **kwargs)
    window = Window()
    monkeypatch.setattr(mw, 'require_active_window', lambda *a: nullcontext(window))
    class Scheduler:
        def _current_cron_store(self): return SimpleNamespace(jobs_file=registry)
        def _jobs_lock_file(self): return registry.parent / '.jobs.lock'
        @contextmanager
        def _fire_job_lock(self, jid):
            events.append(('fire', jid)); yield True
        @contextmanager
        def _jobs_lock(self, *, require_cross_process):
            assert require_cross_process
            self.lease = True
            yield
            self.lease = False
        def _record_load_stamp(self, _): pass
        def _save_jobs_unlocked(self, jobs):
            assert self.lease
            events.append(('save', None))
            registry.write_text(json.dumps({'jobs': jobs}))
    review = {'schema': 'glp-ship-review-v1', 'prepared_by': 'offline-preparer',
              'reviewer': 'offline-independent-fixture', 'decision': 'ACCEPT_EXACT_PROPOSAL',
              'proposal_sha256': json_sha(generation.proposal)}
    result = pair.adopt(ROOT, live, home, review, scheduler=Scheduler(), window=journal, generation=generation)
    assert result['applied']
    assert {p: pair.read_regular(live / p) for p in source_before} == source_before
    after = json.loads(registry.read_text())['jobs']
    assert after[2:] == current[2:]
    for index, (jid, name, *_rest) in enumerate(GLP334_JOBS):
        assert pair.read_regular(home / 'scripts' / name) == (blob(GLP334_SOURCE, 'ops/hermes-cron-scripts/' + name),
                                                            0o700 if jid == 'aadf6677c7f0' else 0o755)
        assert {k:v for k,v in after[index].items() if k != 'script_sha256'} == {k:v for k,v in current[index].items() if k != 'script_sha256'}
    assert events.count(('save', None)) == 1
    assert {jid for event,jid in events if event == 'fire'} == set(spec.jobs)
    assert Path(result['custody'], 'preimages.json').is_file()
    with pytest.raises(RuntimeError):
        pair.adopt(ROOT, live, home, review, scheduler=Scheduler(), window=journal, generation=generation)
