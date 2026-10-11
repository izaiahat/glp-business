"""GLP-354: all cleanup effects are confined to disposable fixture trees."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import host_disk_cold_classes as cold
import host_disk_large_classes as large
import host_disk_tmp_custody as custody


class FixtureAPI:
    def __init__(self, root):
        self.root = root
        self.removed = []

    def path_is_active_privileged(self, path):
        return False

    def remove_disposable(self, path, *, deadline):
        assert path.parent == self.root
        self.removed.append(path)
        shutil.rmtree(path)
        return True


class DeclaredTmpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix='glp354-fixture-')
        self.addCleanup(self.fixture.cleanup)
        self.base = Path(self.fixture.name)
        self.tmp = self.base / 'tmp'
        self.tmp.mkdir()
        self.candidate = self.tmp / 'aeo-declared-idle'
        self.candidate.mkdir()
        (self.candidate / 'payload.txt').write_text('disposable fixture\n' * 100)
        self.api = FixtureAPI(self.tmp)
        self.links = self.base / 'links'
        self.links.mkdir()
        self.schedules = self.base / 'systemd'
        self.schedules.mkdir()
        self.jobs = self.base / 'jobs.json'
        self.jobs.write_text('{"jobs": []}')
        self.proc = self.base / 'proc'
        self.process = self.proc / '1'
        (self.process / 'fd').mkdir(parents=True)
        (self.process / 'cwd').symlink_to(self.base, target_is_directory=True)
        (self.process / 'comm').write_text('systemd\n')
        (self.process / 'stat').write_text('1 (systemd) S ' + '0 ' * 18 + '123\n')
        (self.proc / 'mounts').write_text('proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n')
        now = time.time()
        self.enterContext(patch.object(time, 'time', return_value=now + 2 * 86400))
        self.enterContext(patch.object(cold, 'TMP_ROOT', self.tmp, create=True))
        self.enterContext(patch.object(cold, 'live_symlink_targets', return_value=set()))
        self.enterContext(patch.object(large, 'lsof_inactive', return_value=True))
        self.enterContext(patch.object(custody, 'SYMLINK_ROOTS', (self.links,)))
        self.enterContext(patch.object(custody, 'SCHEDULE_ROOTS', (self.schedules,)))
        self.enterContext(patch.object(custody, 'REQUIRED_FILES', (self.jobs,)))
        self.enterContext(patch.object(custody, 'PROC', self.proc))
        self.enterContext(patch.object(custody.os, 'geteuid', return_value=0))

    def run_candidate(self, *, dry_run):
        return cold.idle_scratch(self.api, [self.candidate], pins=(),
                                 dry_run=dry_run, deadline=time.monotonic() + 10)[0]

    def test_idle_declared_directory_is_eligible_and_reclaimed(self):
        preview = self.run_candidate(dry_run=True)
        self.assertEqual(preview['status'], 'ELIGIBLE', preview)
        self.assertTrue(self.candidate.is_dir())
        result = self.run_candidate(dry_run=False)
        self.assertEqual(result['status'], 'REMOVED', result)
        self.assertFalse(self.candidate.exists())
        self.assertEqual(self.api.removed, [self.candidate])

    def assert_kept(self):
        result = self.run_candidate(dry_run=False)
        self.assertEqual(result['status'], 'REFUSED', result)
        self.assertTrue((self.candidate / 'payload.txt').exists())
        self.assertFalse(self.api.removed)
        return result

    def test_symlink_to_descendant_keeps_directory(self):
        (self.links / 'live').symlink_to(self.candidate / 'payload.txt')
        self.assertIn('symlink', self.assert_kept()['reason'])

    def test_cron_reference_keeps_directory(self):
        self.jobs.write_text('{"command": "cat ' + str(self.candidate / 'payload.txt') + '"}')
        self.assertIn('cron/systemd', self.assert_kept()['reason'])

    def test_systemd_reference_keeps_directory(self):
        (self.schedules / 'reader.service').write_text('[Service]\nWorkingDirectory=' + str(self.candidate))
        self.assert_kept()

    def test_symlinked_unit_target_is_checked_after_plain_symlink_walk(self):
        target = self.links / 'helper.conf'
        target.write_text('[Service]\nWorkingDirectory=' + str(self.candidate))
        (self.schedules / 'reader.service').symlink_to(target)
        with patch.object(custody, 'SYMLINK_ROOTS', (self.schedules, self.links)), \
                patch.object(custody, 'SCHEDULE_ROOTS', ()):
            self.assert_kept()

    def test_malformed_native_cron_evidence_keeps_directory(self):
        self.jobs.write_text('{broken')
        self.assert_kept()

    def test_json_escaped_cron_reference_keeps_directory(self):
        text = json.dumps({'command': str(self.candidate / 'payload.txt')})
        self.jobs.write_text(text.replace('/', '\\/'))
        self.assert_kept()

    def test_scheduler_glob_keeps_directory(self):
        self.jobs.write_text('{"command": "du /tmp/aeo-declared-*"}')
        self.assert_kept()

    def test_process_cwd_keeps_directory(self):
        (self.process / 'cwd').unlink()
        (self.process / 'cwd').symlink_to(self.candidate)
        self.assertIn('cwd', self.assert_kept()['reason'])

    def test_open_file_keeps_directory(self):
        (self.process / 'fd/3').symlink_to(self.candidate / 'payload.txt')
        self.assertIn('open file', self.assert_kept()['reason'])

    def test_open_hardlink_alias_keeps_directory(self):
        alias = self.base / 'alias.txt'
        os.link(self.candidate / 'payload.txt', alias)
        (self.process / 'fd/3').symlink_to(alias)
        self.assertIn('open inode', self.assert_kept()['reason'])

    def test_missing_native_cron_evidence_keeps_directory(self):
        self.jobs.unlink()
        self.assertIn('missing', self.assert_kept()['reason'])

    def test_missing_required_symlink_root_keeps_directory(self):
        self.links.rmdir()
        self.assert_kept()

    def test_unreadable_schedule_keeps_directory(self):
        read_text = Path.read_text
        def unreadable(path, *args, **kwargs):
            if path == self.jobs:
                raise PermissionError('fixture denied')
            return read_text(path, *args, **kwargs)
        with patch.object(Path, 'read_text', unreadable):
            self.assert_kept()

    def test_incomplete_process_evidence_keeps_directory(self):
        (self.process / 'cwd').unlink()
        self.assertIn('incomplete process', self.assert_kept()['reason'])

    def test_private_process_namespace_keeps_directory(self):
        (self.process / 'comm').write_text('Codex\n')
        self.assert_kept()

    def test_hidden_process_mount_keeps_directory(self):
        (self.proc / 'mounts').write_text('proc /proc proc rw,hidepid=2 0 0\n')
        self.assert_kept()

    def test_generic_name_has_no_explicit_class_eligibility(self):
        generic = self.tmp / 'old-generic'
        self.candidate.rename(generic)
        self.candidate = generic
        self.assertIn('scope/protection', self.assert_kept()['reason'])

    def test_bare_declared_prefix_cannot_fall_back_to_generic_class(self):
        import host_disk_auto_classes as auto
        bare = self.tmp / 'aeo-declared-'
        self.candidate.rename(bare)
        rows = auto.stale_tmp(self.api, cold, pins=(), deadline=time.monotonic() + 10,
                              dry_run=False, root=self.tmp, min_bytes=0)
        self.assertEqual(rows[0]['status'], 'REFUSED', rows)
        self.assertTrue((bare / 'payload.txt').exists())

    def test_young_descendant_keeps_directory(self):
        now = time.time()
        os.utime(self.candidate / 'payload.txt', (now, now))
        self.assert_kept()

    def test_lsof_unknown_keeps_directory(self):
        with patch.object(large, 'lsof_inactive', return_value=False):
            self.assert_kept()

    def test_new_reference_on_final_check_keeps_directory(self):
        original = custody.require_unreferenced
        calls = []
        def changed(path, deadline):
            calls.append(path)
            if len(calls) == 2:
                self.jobs.write_text(json.dumps({'command': str(path / 'payload.txt')}))
            original(path, deadline)
        with patch.object(custody, 'require_unreferenced', changed):
            self.assert_kept()
        self.assertEqual(len(calls), 2)

    def test_use_after_final_custody_scan_keeps_directory(self):
        original = custody.require_unreferenced
        calls = []
        def changed(path, deadline):
            calls.append(path)
            original(path, deadline)
            if len(calls) == 2:
                now = time.time()
                os.utime(path / 'payload.txt', (now, now))
        with patch.object(custody, 'require_unreferenced', changed):
            self.assert_kept()

    def test_automatic_tmp_class_cannot_bypass_custody(self):
        import host_disk_auto_classes as auto
        self.jobs.write_text(json.dumps({'command': str(self.candidate / 'payload.txt')}))
        rows = auto.stale_tmp(self.api, cold, pins=(), deadline=time.monotonic() + 10,
                              dry_run=False, root=self.tmp, min_bytes=0)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['status'], 'REFUSED', rows)
        self.assertTrue(self.candidate.is_dir())
        self.assertFalse(self.api.removed)

    def test_reference_edit_during_process_scan_keeps_directory(self):
        original = custody._process_scan
        def changed(path, deadline, inodes):
            original(path, deadline, inodes)
            self.jobs.write_text(json.dumps({'command': str(path / 'payload.txt')}))
        with patch.object(custody, '_process_scan', changed):
            self.assertIn('inventory changed', self.assert_kept()['reason'])

    def test_unreadable_process_fd_inventory_keeps_directory(self):
        scandir = os.scandir
        def unreadable(path):
            if Path(path) == self.process / 'fd':
                raise PermissionError('fixture fd denied')
            return scandir(path)
        with patch.object(os, 'scandir', unreadable):
            self.assert_kept()

    def test_expired_custody_budget_keeps_directory(self):
        result = cold.idle_scratch(self.api, [self.candidate], pins=(),
                                  dry_run=False, deadline=time.monotonic() - 1)[0]
        self.assertEqual(result['status'], 'REFUSED', result)
        self.assertTrue(self.candidate.exists())
        self.assertFalse(self.api.removed)

    def test_policy_pin_to_descendant_keeps_directory(self):
        result = cold.idle_scratch(self.api, [self.candidate],
                                  pins=(self.candidate / 'payload.txt',),
                                  dry_run=False, deadline=time.monotonic() + 10)[0]
        self.assertEqual(result['status'], 'REFUSED', result)
        self.assertTrue(self.candidate.exists())
        self.assertFalse(self.api.removed)

    def test_automatic_tmp_class_reclaims_unreferenced_declared_fixture(self):
        import host_disk_auto_classes as auto
        rows = auto.stale_tmp(self.api, cold, pins=(), deadline=time.monotonic() + 10,
                              dry_run=False, root=self.tmp, min_bytes=0)
        self.assertEqual(rows[0]['status'], 'REMOVED', rows)
        self.assertEqual(self.api.removed, [self.candidate])

    def test_sweep_discovers_declared_and_browser_classes_only(self):
        with patch.dict(sys.modules, {'host_disk_pruner': types.ModuleType('host_disk_pruner')}):
            import host_disk_large_sweep as sweep
        scratch = self.base / 'scratch'
        scratch.mkdir()
        (scratch / 'owned-scratch').mkdir()
        browser = self.tmp / 'glp-ultracode-browser-nowrite-fixture'
        browser.mkdir()
        (self.tmp / 'old-generic').mkdir()
        self.assertEqual(set(sweep.idle_targets(scratch, self.tmp)),
                         {scratch / 'owned-scratch', browser, self.candidate})
        self.assertEqual(set(sweep.idle_targets(scratch, self.tmp, include_declared=False)),
                         {scratch / 'owned-scratch', browser})

    def test_sweep_reference_inventory_pins_declared_descendant(self):
        with patch.dict(sys.modules, {'host_disk_pruner': types.ModuleType('host_disk_pruner')}):
            import host_disk_large_sweep as sweep
        bus = self.base / 'business'
        root = bus / 'reports/ops/glp347-pallas-20261007'
        root.mkdir(parents=True)
        (bus / 'ops').mkdir()
        (bus / 'ops/disk-retention-policy.json').write_text('{"protected_roots": []}')
        target = '/tmp/aeo-declared-reviewed/payload.txt'
        (root / 'NOTE.txt').write_text('retain ' + target)
        report = self.base / 'report'
        report.mkdir()
        with patch.object(sweep, 'BUS', bus):
            pins = sweep.references(report)
        self.assertIn(Path(target), pins)


if __name__ == '__main__':
    unittest.main()
