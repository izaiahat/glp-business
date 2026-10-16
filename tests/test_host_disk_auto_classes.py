import json
import os
from pathlib import Path
import runpy
import pytest
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import host_disk_auto_classes as auto
import host_disk_cold_classes as m
import host_disk_large_classes as large
import glp_affiliate_sync_job as job
try:
    import host_disk_pruner  # noqa: F401  (live host has discord_fmt/disk_retention)
except ModuleNotFoundError:  # CI checkout: only the sweep's reference scan is exercised
    import types
    sys.modules['host_disk_pruner'] = types.ModuleType('host_disk_pruner')


class API:
    def path_is_active_privileged(self, p): return False
    def remove_disposable(self, p, deadline):
        import shutil
        shutil.rmtree(p); return True


REAL_TIME = time.time

import pytest


@pytest.fixture(autouse=True)
def isolated_scheduler_custody(monkeypatch):
    import host_disk_custody
    monkeypatch.setattr(host_disk_custody, 'collect', lambda: set())


def shift(monkeypatch, days):
    """ctime cannot be backdated; advance the clock instead (time module is shared)."""
    now = REAL_TIME()
    monkeypatch.setattr(time, 'time', lambda: now + days * 86400)


def young(path, monkeypatch_days):
    """Mark a tree as used 'now' on the shifted clock by moving its mtimes forward."""
    t = REAL_TIME() + monkeypatch_days * 86400
    for dirpath, dirs, files in os.walk(path):
        for n in files:
            os.utime(os.path.join(dirpath, n), (t, t))


def git(cwd, *args):
    subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True,
                   env={**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'})


def clone_run(root, name, result=None, extra=()):
    run = root / name; c = run / 'candidate'; c.mkdir(parents=True)
    git(c, 'init', '-q'); (c / 'a.html').write_text('x' * 5000); git(c, 'add', '.'); git(c, 'commit', '-qm', 'x')
    remote = root.parent / ('remote-' + name); git(root.parent, 'clone', '-q', '--bare', str(c), str(remote))
    git(c, 'remote', 'add', 'origin', str(remote)); git(c, 'fetch', '-q', 'origin')
    git(c, 'branch', '-q', '--set-upstream-to=origin/master') if False else None
    (run / ('RESULT.json' if result else 'FAILURE.json')).write_text(json.dumps({'status': result or 'FAIL'}))
    for marker in extra:
        (run / marker).write_text('{}')
    return run


def test_registry_clone_retention_bounds_hourly_regrowth(tmp_path, monkeypatch):
    monkeypatch.setattr(job, 'save', lambda p, v: Path(p).write_text(json.dumps(v)))
    root = tmp_path / 'registry-site-sync'
    old_noop = clone_run(root, '20261001T001701Z', 'PASS_ALREADY_CURRENT')
    old_fail = clone_run(root, '20261001T011701Z')
    custody = clone_run(root, '20261001T021701Z', 'PASS_PRODUCTION_TERMINAL', extra=('PUBLISH.json',))
    pinned = clone_run(root, '20261001T031701Z', 'PASS_ALREADY_CURRENT')
    (pinned / 'prepared/release').mkdir(parents=True)
    dirty = clone_run(root, '20261001T041701Z', 'PASS_ALREADY_CURRENT'); (dirty / 'candidate/new.txt').write_text('x')
    newest = [clone_run(root, f'20261009T0{i}1701Z', 'PASS_ALREADY_CURRENT') for i in range(3)]
    rows = job.retire_disposable_clones(root, pins=[pinned / 'prepared/release'])
    assert not (old_noop / 'candidate').exists() and not (old_fail / 'candidate').exists()
    assert json.loads((old_noop / 'candidate.RETIRED.json').read_text())['head']
    assert (old_noop / 'RESULT.json').exists() and (old_fail / 'FAILURE.json').exists()
    for kept in (custody, pinned, dirty, *newest):
        assert (kept / 'candidate/a.html').exists()
    assert sum(r['status'] == 'RETIRED' for r in rows) == 2


def test_registry_clone_retention_retires_orphans_and_preserves_generated_diff(tmp_path,monkeypatch):
    """Real disposable local Git clones/remotes; no host cleanup or network."""
    monkeypatch.setattr(job,'save',lambda p,v:Path(p).write_text(json.dumps(v)))
    root=tmp_path/'registry-site-sync'
    orphan=clone_run(root,'20261001T001701Z');(orphan/'FAILURE.json').unlink()
    noop=clone_run(root,'20261001T011701Z','PASS_SOURCE_NO_CHANGE')
    generated=clone_run(root,'20261001T021701Z');(generated/'FAILURE.json').unlink()
    source=generated/'candidate';(source/'data').mkdir();(source/'data/program-registry.json').write_text('{}\n')
    git(source,'add','.');git(source,'commit','-qm','base registry');git(source,'push','-q','origin','HEAD')
    (source/'data/program-registry.json').write_text('{"generated":true}\n')
    expected=subprocess.check_output(['git','-C',str(source),'diff','--no-ext-diff','--binary','HEAD'])
    unknown=clone_run(root,'20261001T031701Z');(unknown/'candidate/a.html').write_text('unknown source change')
    unpushed=clone_run(root,'20261001T041701Z');(unpushed/'candidate/a.html').write_text('unique commit')
    git(unpushed/'candidate','commit','-qam','unpushed')
    publication=clone_run(root,'20261001T051701Z',extra=('publish-intent.json',))
    pr=clone_run(root,'20261001T061701Z',extra=('PR.json',))
    pinned=clone_run(root,'20261001T071701Z')
    newest=[clone_run(root,f'20261009T0{i}1701Z') for i in range(3)]
    rows=job.retire_disposable_clones(root,pins=[pinned/'prepared/release'])
    for retired in (orphan,noop,generated): assert not (retired/'candidate').exists()
    assert (generated/'candidate.DIFF.patch').read_bytes()==expected
    receipt=json.loads((generated/'candidate.RETIRED.json').read_text())
    assert receipt['generated_diff']['sha256']==__import__('hashlib').sha256(expected).hexdigest()
    assert (generated/'candidate.DIFF.patch').is_file()
    for kept in (unknown,unpushed,publication,pr,pinned,*newest): assert (kept/'candidate/a.html').is_file()
    assert sum(row['status']=='RETIRED' for row in rows)==3


def test_main_registry_retention_keeps_policy_and_retires_candidate_and_prepared(tmp_path, monkeypatch):
    """Real main-* Git clones with second/microsecond names; no host effects."""
    monkeypatch.setattr(job, 'save', lambda p, v: Path(p).write_text(json.dumps(v)))
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    root = tmp_path / 'registry-site-sync'
    runs = []
    for i in range(21):
        stamp = f'20261001T{i:02}1701' + ('123456Z' if i % 2 else 'Z')
        run = clone_run(root, 'main-' + stamp)
        (run / 'prepared/release').mkdir(parents=True)
        (run / 'prepared/release/page.html').write_text('rebuildable')
        runs.append(run)
    for i in range(4):
        result = {'status': 'PASS_PRODUCTION_TERMINAL', 'deployment_id': f'past-{i}'}
        (runs[i] / 'MAIN-RELEASE-RESULT.json').write_text(json.dumps(result))
        (runs[i] / 'PUBLISH.json').write_text(json.dumps(result))
    current = runs[4]
    (current / 'MAIN-RELEASE-RESULT.json').write_text(json.dumps({'status': 'PASS_ALREADY_CURRENT', 'deployment_id': 'live'}))
    pointer = tmp_path / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({'deployment_id': 'live', 'release_root': str(current / 'prepared/release')}))
    pinned, dirty, held, orphan, failed, uncertain = runs[5:11]
    (dirty / 'candidate/new.txt').write_text('uncommitted evidence')
    (uncertain / 'publish-intent.json').write_text('{}')
    (orphan / 'FAILURE.json').unlink()
    calls = []
    def active(path):
        calls.append(path)
        return path == held or held in path.parents
    rows = job.retire_disposable_clones(root, pins=[pinned / 'prepared/release'], active=active)
    for retired in (orphan, failed):
        assert not (retired / 'candidate').exists(), rows
        assert not (retired / 'prepared').exists(), rows
        assert (retired / 'candidate.RETIRED.json').is_file()
        assert (retired / 'prepared.RETIRED.json').is_file()
    assert (failed / 'FAILURE.json').is_file()
    assert (dirty / 'candidate/new.txt').is_file()
    for kept in (*runs[:6], held, uncertain, *runs[11:]):
        assert (kept / 'candidate/a.html').is_file(), kept
        assert (kept / 'prepared/release/page.html').is_file(), kept
    assert calls, 'all-UID reference gate must be exercised before retirement'
    assert sum(r['status'] == 'RETIRED' for r in rows) >= 4


@pytest.mark.parametrize('hold', ['unpushed', 'database', 'environment', 'browser_profile', 'nested_git', 'pointer_drift', 'new_process'])
def test_main_retention_final_gates_preserve_held_payload(tmp_path, monkeypatch, hold):
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    root = tmp_path / 'registry-site-sync'
    run = clone_run(root, 'main-20261001T001701Z')
    prepared = run / 'prepared'
    prepared.mkdir()
    (prepared / 'page.html').write_text('rebuildable')
    pointer = tmp_path / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({'deployment_id': 'live', 'release_root': str(tmp_path / 'live')}))
    target = run / 'candidate' if hold == 'unpushed' else prepared
    if hold == 'unpushed':
        (target / 'new.txt').write_text('unpushed source')
        git(target, 'config', 'user.email', 'fixture@example.invalid')
        git(target, 'config', 'user.name', 'fixture')
        git(target, 'add', 'new.txt')
        git(target, 'commit', '-qm', 'unpublished')
    elif hold == 'database':
        (prepared / 'opaque').write_bytes(b'SQLite format 3\0' + b'fixture')
    elif hold == 'environment':
        (prepared / '.env').write_text('fixture, not a credential')
    elif hold == 'browser_profile':
        (prepared / 'DevToolsActivePort').write_text('fixture profile marker')
    elif hold == 'nested_git':
        (prepared / '.git').mkdir()
    def save(path, value):
        Path(path).write_text(json.dumps(value))
        if hold == 'pointer_drift':
            pointer.write_text(json.dumps({'deployment_id': 'new-live', 'release_root': str(prepared)}))
    monkeypatch.setattr(job, 'save', save)
    calls = []
    def active(path):
        calls.append(path)
        return hold == 'new_process' and len(calls) % 2 == 0
    rows = job.retire_main_payloads(root, pins=(), active=active, keep=0, pass_keep=0)
    assert target.is_dir(), rows
    assert any(r.get('status') == 'ERROR_PRESERVED_OR_PARTIAL' for r in rows)


def test_rebuildable_report_node_modules_removed_even_when_checkout_dirty(tmp_path, monkeypatch):
    reports = tmp_path / 'reports'; co = reports / 'ops/x-20260901/checkout'; co.mkdir(parents=True)
    git(co, 'init', '-q'); (co / '.gitignore').write_text('node_modules/\n'); (co / 'package-lock.json').write_text('{}')
    git(co, 'add', '.'); git(co, 'commit', '-qm', 'x'); (co / 'dirty.txt').write_text('evidence')
    nm = co / 'node_modules/pkg'; nm.mkdir(parents=True); (nm / 'index.js').write_text('x' * 4096)
    tracked = reports / 'ops/y-20260901/checkout'; tracked.mkdir(parents=True); git(tracked, 'init', '-q')
    (tracked / 'package-lock.json').write_text('{}'); (tracked / 'node_modules').mkdir(); (tracked / 'node_modules/a.js').write_text('x')
    git(tracked, 'add', '-f', '.'); git(tracked, 'commit', '-qm', 'x')
    shift(monkeypatch, 5)
    monkeypatch.setattr(large, 'lsof_inactive', lambda p: True)
    rows = auto.rebuildable_node_modules(API(), m, [co / 'node_modules', tracked / 'node_modules'], pins=(),
                                         deadline=time.monotonic() + 60, dry_run=False, reports_root=reports)
    assert rows[0]['status'] == 'REMOVED' and not (co / 'node_modules').exists() and (co / 'dirty.txt').exists()
    assert rows[1]['status'] == 'REFUSED' and (tracked / 'node_modules/a.js').exists()


def test_stale_tmp_entries_older_than_24h_removed_young_and_pinned_kept(tmp_path, monkeypatch):
    root = tmp_path / 'tmp'
    for name in ('old-build', 'young-build', 'pinned-build', 'com.google.Chrome.abc'):
        (root / name / 'sub').mkdir(parents=True); (root / name / 'sub/f.bin').write_bytes(b'x' * 4096)
    shift(monkeypatch, 2); young(root / 'young-build', 2); os.utime(root / 'young-build', (REAL_TIME() + 2 * 86400,) * 2)
    os.listdir(root / 'old-build/sub')  # our own listing must not make it young
    monkeypatch.setattr(large, 'lsof_inactive', lambda p: True)
    rows = auto.stale_tmp(API(), m, pins=(root / 'pinned-build',), deadline=time.monotonic() + 60,
                          dry_run=False, root=root, min_bytes=0)
    status = {Path(r['path']).name: r['status'] for r in rows}
    assert status == {'old-build': 'REMOVED', 'pinned-build': 'REFUSED'}
    assert (root / 'young-build').exists() and (root / 'com.google.Chrome.abc').exists()


def test_idle_scratch_child_listed_by_scanner_is_still_idle(tmp_path, monkeypatch):
    """Regression: dir atime bumped by the pruner's own listing refused every scratch entry."""
    scratch = Path('/home/ubuntu/.hermes/cache/scratch')
    if not scratch.is_dir():
        return
    p = Path(subprocess.check_output(['mktemp', '-d', '-p', str(scratch), 'autoprune-test-XXXX'], text=True).strip())
    try:
        (p / 'sub').mkdir(); (p / 'sub/f').write_text('x'); shift(monkeypatch, 2)
        listed = REAL_TIME() + 2 * 86400  # a scanner listed the dirs 'now' (relatime moves dir atime)
        for d in (p, p / 'sub'):
            os.utime(d, (listed, os.lstat(d).st_mtime))
        monkeypatch.setattr(large, 'lsof_inactive', lambda q: True)
        monkeypatch.setattr(m, 'live_symlink_targets', lambda: set())
        row = m.idle_scratch(API(), [p], pins=(), dry_run=True, deadline=time.monotonic() + 30)[0]
        assert row['status'] == 'ELIGIBLE', row
    finally:
        subprocess.run(['rm', '-rf', str(p)])


def test_cold_report_archive_refuses_open_pr_or_linear_reference(tmp_path, monkeypatch):
    report = tmp_path / 'reports/ops/glp1-20260901'; report.mkdir(parents=True)
    (report / 'evidence.json').write_text('x' * 9000); shift(monkeypatch, 10)
    referenced = Path(str(report)) if str(report).startswith('/home/ubuntu/business/') else report
    monkeypatch.setattr(auto, 'BUS', tmp_path)
    pins = auto.text_paths(f'see {referenced} for the checkout') | {referenced}
    row = m.cold_archive(API(), report, tmp_path / 'a.tar.zst', pins=tuple(pins), dry_run=False,
                         deadline=time.monotonic() + 60)
    assert row['status'] == 'REFUSED' or row.get('members', 0) == 0
    assert (report / 'evidence.json').exists()
    row = m.cold_archive(API(), report, tmp_path / 'b.tar.zst', pins=(), dry_run=False, deadline=time.monotonic() + 60)
    assert row['status'] == 'ARCHIVED_VERIFIED_RETIRED' and not (report / 'evidence.json').exists()


def test_site_archive_skips_live_release_and_r8(tmp_path, monkeypatch):
    site = tmp_path / 'site'
    for rel in ('releases/old-a', 'releases/live-b', 'candidates/glp347-r8-x', 'qa/old-q'):
        (site / rel).mkdir(parents=True); (site / rel / 'page.html').write_text('x' * 9000)
    shift(monkeypatch, 10)
    monkeypatch.setattr(auto, 'SITE', site)
    pins = (site / 'releases/live-b', *[p for p in auto.r8_pins(roots=(site / 'candidates',))])
    rows = auto.site_cold_archive(API(), m, tmp_path / 'report', pins=pins, deadline=time.monotonic() + 300,
                                  dry_run=False, site=site, min_bytes=0)
    assert [Path(r['path']).name for r in rows] == ['old-a'] and rows[0]['status'] == 'ARCHIVED_VERIFIED_RETIRED'
    assert (site / 'releases/live-b/page.html').exists() and (site / 'candidates/glp347-r8-x/page.html').exists()
    assert (site / 'qa/old-q/page.html').exists()


def test_worktree_and_prepared_references_become_pins(tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir(); git(repo, 'init', '-q'); (repo / 'f').write_text('x')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'x')
    wts = tmp_path / '.worktrees'; wts.mkdir(); git(repo, 'worktree', 'add', '-q', str(wts / 'a'))
    git(repo, 'worktree', 'add', '-q', str(tmp_path / 'elsewhere'))
    pins = auto.worktree_pins(roots=(wts,))
    assert {repo, wts / 'a', tmp_path / 'elsewhere'} <= pins
    prep = tmp_path / 'reports/x'; prep.mkdir(parents=True)
    (prep / 'PREPARED.json').write_text(json.dumps({'release': '/home/ubuntu/business/reports/ops/keep-me-20260901/release'}))
    pins = auto.prepared_pins(roots=(tmp_path / 'reports',))
    assert prep in pins and Path('/home/ubuntu/business/reports/ops/keep-me-20260901/release') in pins


def test_low_free_trigger_launches_only_below_threshold(tmp_path):
    t = runpy.run_path(str(ROOT / 'ops/hermes-cron-scripts/host_disk_low_free_trigger.py'))
    fake = tmp_path / 'trigger.py'
    fake.write_text("def trigger():\n    return {'unit':'host-disk-cleanup.service','launched':True}\n")
    assert t['main'](str(fake), fraction=0.20) == ''
    assert 'launched **host-disk-cleanup.service**' in t['main'](str(fake), fraction=0.10)
    fake.write_text("def trigger():\n    return {'unit':'host-disk-cleanup.service','launched':False}\n")
    assert t['main'](str(fake), fraction=0.10) == ''


def test_bare_scratch_and_tmp_references_do_not_pin_every_child(tmp_path, monkeypatch):
    """Regression: a reference to the scratch root itself refused all 300 idle entries."""
    import host_disk_large_sweep as sweep
    bus = tmp_path / 'business'; root = bus / 'reports/ops/glp347-pallas-20261007'; root.mkdir(parents=True)
    (bus / 'ops').mkdir(); (bus / 'ops/disk-retention-policy.json').write_text('{"protected_roots":[]}')
    (root / 'NOTE.md').write_text("TMPDIR=/home/ubuntu/.hermes/cache/scratch/. and '/home/ubuntu/.hermes/cache/scratch/'' ;"
                                  " keep /home/ubuntu/.hermes/cache/scratch/keep-me-x\n")
    monkeypatch.setattr(sweep, 'BUS', bus)
    report = tmp_path / 'report'; report.mkdir()
    pins = set(sweep.references(report))
    assert Path('/home/ubuntu/.hermes/cache/scratch') not in pins and Path('/tmp') not in pins
    assert Path('/home/ubuntu/.hermes/cache/scratch/keep-me-x') in pins


def test_bare_registry_root_reference_does_not_pin_every_run(tmp_path, monkeypatch):
    import host_disk_large_sweep as sweep
    bus = tmp_path / 'business'; root = bus / 'reports/ops/glp347-pallas-20261007'; root.mkdir(parents=True)
    (bus / 'ops').mkdir(); (bus / 'ops/disk-retention-policy.json').write_text('{"protected_roots":[]}')
    runs = 'reports/glp-clinic-scout/affiliate-machine/registry-site-sync'
    (root / 'NOTE.md').write_text(f"see {runs} and {runs}/20261003T030854Z/prepared/release\n")
    monkeypatch.setattr(sweep, 'BUS', bus)
    report = tmp_path / 'report'; report.mkdir()
    pins = set(sweep.references(report))
    assert bus / runs not in pins and bus / 'reports/glp-clinic-scout/affiliate-machine' not in pins
    assert bus / runs / '20261003T030854Z/prepared/release' in pins


def test_removed_registered_worktree_is_not_a_live_pin(tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir(); git(repo, 'init', '-q')
    (repo / 'f').write_text('x'); git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'x')
    wts = tmp_path / '.worktrees'; wts.mkdir()
    live = wts / 'live'; stale = wts / 'stale'
    git(repo, 'worktree', 'add', '-q', str(live))
    git(repo, 'worktree', 'add', '-q', str(stale))
    import shutil
    shutil.rmtree(stale)  # Coordinator removal can precede git's administrative prune.
    pins = auto.worktree_pins(roots=(wts,))
    assert stale not in pins
    assert {repo, live} <= pins


def test_external_inventory_discards_absent_paths_but_keeps_symlinks(tmp_path, monkeypatch):
    import host_disk_large_sweep as sweep
    live = tmp_path / 'live'; live.mkdir()
    stale = tmp_path / 'gone'
    link = tmp_path / 'link'; link.symlink_to(tmp_path / 'unavailable-target')
    payload = tmp_path / 'external.json'
    payload.write_text(json.dumps({'schema': 'host-disk-external-pins-v1',
                                  'collected_at': REAL_TIME(), 'pins': list(map(str, (live, stale, link)))}))
    assert sweep.load_external_pins(payload) == {live, link}


def test_archive_savings_gate_rolls_back_even_inside_pinned_report(tmp_path, monkeypatch):
    root = tmp_path / 'evidence'; root.mkdir()
    (root / 'proof.bin').write_bytes(os.urandom(65536)); shift(monkeypatch, 10)
    report = tmp_path / 'pruner-report'; report.mkdir()
    dest = report / 'a.tar.zst'
    row = m.cold_archive(API(), root, dest, pins=(report,), dry_run=False,
                         min_bytes=65536, deadline=time.monotonic() + 60)
    assert row['status'] == 'GATED_NO_NET_SAVINGS'
    assert (root / 'proof.bin').exists()
    assert not list(report.iterdir())  # No repeated space-consuming partial or sidecar.
    assert row['bytes_freed'] == 0


def test_failed_archive_verification_rolls_back_before_publish(tmp_path, monkeypatch):
    import pytest
    root = tmp_path / 'evidence'; root.mkdir(); (root / 'proof').write_bytes(b'x' * 8192)
    shift(monkeypatch, 10); dest = tmp_path / 'output/a.tar.zst'
    def fail(archive, rows, listing):
        assert archive.name.endswith('.partial') and not dest.exists()
        raise ValueError('injected verification failure')
    monkeypatch.setattr(m, 'verify_archive', fail)
    with pytest.raises(ValueError, match='injected verification'):
        m.cold_archive(API(), root, dest, dry_run=False, deadline=time.monotonic() + 60)
    assert (root / 'proof').exists() and not list(dest.parent.iterdir())


def abandoned_fixture(tmp_path):
    root = tmp_path / 'evidence'; root.mkdir(); (root / 'proof').write_bytes(b'proof')
    archive = tmp_path / 'prune/old/large/archives/a.tar.zst'; archive.parent.mkdir(parents=True)
    partial = Path(str(archive) + '.partial'); partial.write_bytes(b'uncommitted compressed bytes')
    manifest = Path(str(archive) + '.manifest.json')
    manifest.write_text(json.dumps({'source': str(root), 'members': [
        {'name': 'proof', 'signature': m.signature(root / 'proof'), 'sha256': m.sha(root / 'proof')}]}))
    return root, archive, partial, manifest


def test_abandoned_partial_cleanup_proves_all_originals_still_exist(tmp_path):
    root, archive, partial, manifest = abandoned_fixture(tmp_path)
    rows = m.cleanup_abandoned_archives(API(), tmp_path / 'prune', source_roots=(root,),
                                       dry_run=False, deadline=time.monotonic() + 60)
    assert rows[0]['status'] == 'ABANDONED_STAGING_REMOVED'
    assert rows[0]['bytes_freed'] > 0 and (root / 'proof').exists()
    assert not partial.exists() and not manifest.exists()


def test_partial_retirement_or_changed_original_is_never_discarded(tmp_path):
    root, archive, partial, manifest = abandoned_fixture(tmp_path)
    ledger = Path(str(archive) + '.retirement.jsonl'); ledger.write_text('')
    rows = m.cleanup_abandoned_archives(API(), tmp_path / 'prune', source_roots=(root,),
                                       dry_run=False, deadline=time.monotonic() + 60)
    assert partial.exists() and rows[0]['bytes_freed'] == 0
    ledger.unlink(); (root / 'proof').write_bytes(b'changed')
    rows = m.cleanup_abandoned_archives(API(), tmp_path / 'prune', source_roots=(root,),
                                       dry_run=False, deadline=time.monotonic() + 60)
    assert partial.exists() and manifest.exists() and rows[0]['bytes_freed'] == 0


def test_pressure_net_floor_is_separate_from_candidate_size(tmp_path, monkeypatch):
    root = tmp_path / 'evidence'; root.mkdir()
    (root / 'proof.bin').write_bytes(b'x' * 32768 + os.urandom(32768))
    shift(monkeypatch, 10)
    row = m.cold_archive(API(), root, tmp_path / 'out/a.tar.zst', dry_run=False,
                         min_bytes=65536, min_net_bytes=4096, deadline=time.monotonic() + 60)
    assert row['status'] == 'ARCHIVED_VERIFIED_RETIRED' and row['bytes_freed'] > 4096
    assert not (root / 'proof.bin').exists()


def test_disk_monitor_triggers_cleanup_even_when_alert_is_unchanged(tmp_path, monkeypatch):
    import types
    source = (Path(__file__).resolve().parents[1] / 'ops/hermes-cron-scripts/disk_monitor.sh').read_text()
    body = source.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    body = body.replace("Path('/home/ubuntu/.hermes')", f"Path({str(tmp_path / 'hermes')!r})")
    ns = {'__name__': 'fixture'}; exec(compile(body, 'disk_monitor.sh', 'exec'), ns)
    state = tmp_path / 'state'; state.write_text('critical')
    calls = []; posts = []
    monkeypatch.setitem(sys.modules, 'host_capacity_observer', types.SimpleNamespace(notify_state_store=lambda *a: 0))
    ns.update(STATE_FILE=state, log=lambda *a: None, read_df=lambda: (96, '581G', '555G', '26G'), post=posts.append)
    # The retained low-disk producer has its own threshold/duplicate regression.
    def main():
        calls.append('cleanup'); return ''  # Existing service owner => silent duplicate.
    ns['run_low_disk_trigger'] = main
    assert ns['main']() == 0
    assert calls == ['cleanup'] and posts == []


def test_disk_monitor_supplies_user_runtime_for_minimal_cron(tmp_path, monkeypatch):
    import runpy
    source = (Path(__file__).resolve().parents[1] / 'ops/hermes-cron-scripts/disk_monitor.sh').read_text()
    body = source.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    body = body.replace("Path('/home/ubuntu/.hermes')", f"Path({str(tmp_path / 'hermes')!r})")
    ns = {'__name__': 'fixture'}
    exec(compile(body, 'disk_monitor.sh', 'exec'), ns)
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    monkeypatch.setattr(os, 'getuid', lambda: 4242)
    calls = []
    def trigger_main():
        runtime = os.environ.get('XDG_RUNTIME_DIR')
        assert runtime == '/run/user/4242'
        calls.append('registered-main')
        return ''
    def run_path(path):
        assert path == str(tmp_path / 'hermes/scripts/host_disk_low_free_trigger.py')
        return {'main': trigger_main}
    monkeypatch.setattr(runpy, 'run_path', run_path)
    assert ns['run_low_disk_trigger']() == ''
    assert calls == ['registered-main']


def test_disk_monitor_preserves_explicit_runtime_and_trigger_failure(tmp_path, monkeypatch):
    import runpy
    import pytest
    source = (Path(__file__).resolve().parents[1] / 'ops/hermes-cron-scripts/disk_monitor.sh').read_text()
    body = source.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    body = body.replace("Path('/home/ubuntu/.hermes')", f"Path({str(tmp_path / 'hermes')!r})")
    ns = {'__name__': 'fixture'}
    exec(compile(body, 'disk_monitor.sh', 'exec'), ns)
    monkeypatch.setenv('XDG_RUNTIME_DIR', '/fixture/explicit-runtime')
    def run_path(path):
        runtime = os.environ.get('XDG_RUNTIME_DIR')
        assert runtime == '/fixture/explicit-runtime'
        raise RuntimeError('registered trigger refusal')
    monkeypatch.setattr(runpy, 'run_path', run_path)
    with pytest.raises(RuntimeError, match='registered trigger refusal'):
        ns['run_low_disk_trigger']()


def test_compressor_failure_rolls_back_all_uncommitted_outputs(tmp_path, monkeypatch):
    import pytest
    root = tmp_path / 'evidence'; root.mkdir(); (root / 'proof').write_bytes(b'x' * 8192)
    shift(monkeypatch, 10); dest = tmp_path / 'output/a.tar.zst'
    real_popen = m.subprocess.Popen
    def fail_compressor(command, **kwargs):
        return real_popen([sys.executable, '-c', 'import sys;sys.stdin.buffer.read();sys.exit(1)'], **kwargs)
    monkeypatch.setattr(m.subprocess, 'Popen', fail_compressor)
    with pytest.raises(ValueError, match='compression failed'):
        m.cold_archive(API(), root, dest, dry_run=False, deadline=time.monotonic() + 60)
    assert (root / 'proof').exists() and not list(dest.parent.iterdir())


def test_pressure_enrolls_cold_publisher_reports_without_enrolling_live_pins(tmp_path, monkeypatch):
    import host_disk_large_sweep as sweep
    bus = tmp_path / 'business'
    ops = bus / 'reports/ops'; ops.mkdir(parents=True)
    old = ops / 'old-20260901'; old.mkdir()
    reports = bus / 'reports/glp-clinic-scout'; reports.mkdir()
    cold = reports / 'evidence-refresh'; cold.mkdir()
    live = reports / 'live'; live.mkdir()
    link = reports / 'link'; link.symlink_to(cold, target_is_directory=True)
    monkeypatch.setattr(sweep, 'BUS', bus)
    assert sweep.cold_report_roots((live,), pressure=False) == [old]
    assert set(sweep.cold_report_roots((live,), pressure=True)) == {old, cold}


def test_report_ranking_does_not_silently_drop_large_slow_roots(tmp_path,monkeypatch):
    import host_disk_large_sweep as sweep
    from types import SimpleNamespace
    monkeypatch.setattr(sweep.time,'monotonic',lambda:100)
    def du(*args,**kwargs):
        assert kwargs['timeout']>=30
        return SimpleNamespace(returncode=0,stdout='50000000000 root\n',stderr='')
    monkeypatch.setattr(sweep.subprocess,'run',du)
    ranking,refusals=sweep.rank_reports([tmp_path],400)
    assert ranking==[(50000000000,tmp_path)] and refusals==[]


def test_release_toolchains_are_protected_roots():
    import host_disk_cold_classes as cold
    root = Path('/home/ubuntu/business/site-system/toolchains/wrangler-4.114.0-r13')
    assert cold.protected(root / 'node_modules')
    assert cold.protected(root / 'node_modules/wrangler/bin/wrangler.js')
    assert cold.protected(Path('/home/ubuntu/business/site-system/toolchains'))
    assert not cold.protected(Path('/home/ubuntu/business/site-system/flagship-v2/candidates/x/node_modules'))
