import json
import os
from pathlib import Path
import runpy
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
    dirty = clone_run(root, '20261001T041701Z', 'PASS_ALREADY_CURRENT'); (dirty / 'candidate/new.txt').write_text('x')
    newest = [clone_run(root, f'20261009T0{i}1701Z', 'PASS_ALREADY_CURRENT') for i in range(3)]
    rows = job.retire_disposable_clones(root, pins=[pinned / 'prepared/release'])
    assert not (old_noop / 'candidate').exists() and not (old_fail / 'candidate').exists()
    assert json.loads((old_noop / 'candidate.RETIRED.json').read_text())['head']
    assert (old_noop / 'RESULT.json').exists() and (old_fail / 'FAILURE.json').exists()
    for kept in (custody, pinned, dirty, *newest):
        assert (kept / 'candidate/a.html').exists()
    assert sum(r['status'] == 'RETIRED' for r in rows) == 2


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


def test_release_toolchains_are_protected_roots():
    import host_disk_cold_classes as cold
    root = Path('/home/ubuntu/business/site-system/toolchains/wrangler-4.114.0-r13')
    assert cold.protected(root / 'node_modules')
    assert cold.protected(root / 'node_modules/wrangler/bin/wrangler.js')
    assert cold.protected(Path('/home/ubuntu/business/site-system/toolchains'))
    assert not cold.protected(Path('/home/ubuntu/business/site-system/flagship-v2/candidates/x/node_modules'))
