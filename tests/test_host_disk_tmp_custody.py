"""GLP-354: all cleanup effects are confined to disposable fixture trees."""
import json
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

CANDIDATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANDIDATE / 'scripts'))


def load_candidate(name, path):
    """Bind the tested modules before sweep can prepend installed scripts."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == path.resolve()
    return module


for name in ('host_disk_payload_guard', 'host_disk_custody', 'host_disk_cold_classes',
             'host_disk_large_classes', 'host_disk_tmp_custody',
             'host_disk_auto_classes', 'host_disk_reference_guard'):
    load_candidate(name, CANDIDATE / 'scripts' / (name + '.py'))
with patch.dict(sys.modules, {'host_disk_pruner': types.ModuleType('host_disk_pruner')}):
    sweep = load_candidate('host_disk_large_sweep', CANDIDATE / 'scripts/host_disk_large_sweep.py')
sys.modules['host_disk_large_sweep'] = sweep
sys.path.insert(0, str(CANDIDATE / 'scripts'))
cold = sys.modules['host_disk_cold_classes']
large = sys.modules['host_disk_large_classes']
custody = sys.modules['host_disk_tmp_custody']


class FixtureAPI:
    def __init__(self, root):
        self.root = root
        self.removed = []

    def path_is_active_privileged(self, path):
        return False

    def remove_disposable(self, path, *, deadline, expected=None, admission=None):
        assert path.parent == self.root
        if expected is not None:
            # The actual maintained boundary, compiled without CLI import effects.
            import ast
            source = CANDIDATE / 'scripts/host_disk_pruner.py'
            node = next(n for n in ast.parse(source.read_bytes()).body
                        if isinstance(n, ast.FunctionDef) and n.name == 'remove_disposable')
            namespace = dict(Path=Path)
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
            result = namespace['remove_disposable'](path, deadline=deadline, expected=expected, admission=admission)
            if result['status'] == 'REMOVED':
                self.removed.append(path)
            return result
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
        self.candidate = self.tmp / 'aeo-declared-Ab09xY'
        self.candidate.mkdir()
        (self.candidate / 'payload.txt').write_text('disposable fixture\n' * 100)
        self.rebuild_source = self.base / 'rebuild-source'
        # Private source-present seam; these are NOT fabricated live evidence.
        for relative in ('tests/lastmod-declared-routes.test.mjs',
                         'scripts/postbuild-aeo-surface.mjs',
                         'data/page-content-provenance.json', 'data/llms-priority.json'):
            source = self.rebuild_source / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('synthetic rebuild input\n')
        for relative in ('src', 'public'):
            directory = self.rebuild_source / relative
            directory.mkdir()
            (directory / ('required.mjs' if relative == 'src' else 'index.html')).write_text('synthetic rebuild content\n')
        # A tiny explicit carrier closure, not a claim about real article bytes.
        import hashlib
        for slug in ('ezra-glp-1-reviews', 'top-rated-glp-1-providers'):
            directory = self.rebuild_source / 'data/carrier-articles' / slug
            directory.mkdir(parents=True)
            snapshot = directory / 'index.html'
            snapshot.write_text('synthetic retained carrier\n')
            asset = self.rebuild_source / 'assets' / (slug + '.webp')
            asset.parent.mkdir(exist_ok=True)
            asset.write_bytes(b'synthetic asset bytes')
            manifest = {'snapshot': str(snapshot.relative_to(self.rebuild_source)),
                        'html_sha256': hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                        'bindings': [{'path': 'data/llms-priority.json'}],
                        'files': [{'path': str(asset.relative_to(self.rebuild_source)),
                                   'bytes': asset.stat().st_size,
                                   'sha256': hashlib.sha256(asset.read_bytes()).hexdigest()}]}
            (directory / 'manifest.json').write_text(json.dumps(manifest))
        # Small private contract fixture, explicitly NOT historical authority.
        import hashlib
        self.predecessor = self.base / 'predecessor'
        shutil.copytree(self.rebuild_source, self.predecessor)
        provenance = {'schema': 'glp-page-content-provenance-v1', 'pages': {
            '/how-we-rate/': {'lastmod': '2026-08-19'},
            '/reviews/orderlymeds/': {'lastmod': '2026-08-19'}}}
        (self.predecessor / 'data/page-content-provenance.json').write_text(json.dumps(provenance))
        for route in ('how-we-rate', 'reviews/orderlymeds'):
            page = self.predecessor / 'public' / route / 'index.html'
            page.parent.mkdir(parents=True)
            page.write_text('private predecessor page')
        self.base_manifest = self.base / 'deployment-manifest.json'
        self.base_receipt = self.base / 'final-receipt.json'
        self.base_pointer = self.base / 'current-content-base.json'
        rows = [{'path': str(p.relative_to(self.predecessor)),
                 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in sorted(self.predecessor.rglob('*')) if p.is_file()]
        self.base_manifest.write_text(json.dumps({'release_root': str(self.predecessor), 'release_rows': rows}))
        self.base_receipt.write_text(json.dumps({'status': 'PASS_PRODUCTION_TERMINAL',
                                               'deployment': {'deployment_id': 'private-test-deployment'}}))
        self.base_pointer.write_text(json.dumps({'schema': 'glp-opus-current-content-base-v1', 'status': 'PASS',
            'deployment_id': 'private-test-deployment', 'release_root': str(self.predecessor),
            'manifest': {'path': str(self.base_manifest), 'sha256': hashlib.sha256(self.base_manifest.read_bytes()).hexdigest()},
            'final_receipt': {'path': str(self.base_receipt), 'sha256': hashlib.sha256(self.base_receipt.read_bytes()).hexdigest()}}))
        self.enterContext(patch.object(custody, 'DECLARED_CONTENT_BASE', self.base_pointer))
        self.enterContext(patch.object(custody, 'DECLARED_REBUILD_ROOT', self.rebuild_source, create=True))
        self.api = FixtureAPI(self.tmp)
        self.links = self.base / 'links'
        self.links.mkdir()
        self.schedules = self.base / 'systemd'
        self.schedules.mkdir()
        self.jobs = self.base / 'jobs.json'
        self.jobs.write_text('{"jobs": []}')
        (self.jobs.parent / '.jobs.lock').touch()
        base_lock = self.base / 'publication.lock'
        base_lock.touch()
        self.enterContext(patch.object(custody, 'DECLARED_BUSINESS_ROOT', self.base))
        self.enterContext(patch.object(custody, 'DECLARED_BASE_LOCK', base_lock))
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
        self.enterContext(patch.object(sys.modules['host_disk_custody'], 'collect', return_value=set()))
        self.enterContext(patch.object(large, 'lsof_inactive', return_value=True))
        self.enterContext(patch.object(custody, 'SYMLINK_ROOTS', (self.links,)))
        self.enterContext(patch.object(custody, 'CRON_ROOTS', ()))
        self.enterContext(patch.object(custody, 'SYSTEMD_ROOTS', (self.schedules,)))
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

    def test_final_snapshot_root_replacement_reaches_real_helper_but_is_retained(self):
        original = large.snapshot
        calls = 0
        saved = self.base / 'retained-original'
        def swap(path, deadline):
            nonlocal calls
            value = original(path, deadline)
            calls += 1
            if calls == 5:
                path.rename(saved)
                path.mkdir()
                (path / 'payload.txt').write_text('unreviewed young replacement')
            return value
        with patch.object(large, 'snapshot', side_effect=swap):
            row = self.run_candidate(dry_run=False)
        self.assertEqual(calls, 5)
        self.assertEqual(row['status'], 'REFUSED', row)
        self.assertEqual(row['bytes_freed'], 0)
        self.assertTrue((saved / 'payload.txt').exists())
        self.assertEqual((self.candidate / 'payload.txt').read_text(), 'unreviewed young replacement')

    def test_late_lsof_native_hold_is_revalidated_at_handoff(self):
        calls = 0
        def lsof(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.jobs.write_text(json.dumps({'command': str(self.candidate / 'payload.txt')}))
            return True
        with patch.object(large, 'lsof_inactive', side_effect=lsof):
            row = self.assert_kept()
        self.assertEqual(calls, 2)
        self.assertIn('inventory changed', row['reason'])

    def test_native_writer_lock_is_held_through_actual_member_unlink(self):
        import fcntl
        unlink = os.unlink
        attempted = []
        def observe(path, **kwargs):
            if path == 'member':
                with (self.jobs.parent / '.jobs.lock').open('rb') as writer:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                attempted.append(path)
            return unlink(path, **kwargs)
        with patch.object(os, 'unlink', side_effect=observe):
            row = self.run_candidate(dry_run=False)
        self.assertEqual(row['status'], 'REMOVED', row)
        self.assertEqual(attempted, ['member'])
        with (self.jobs.parent / '.jobs.lock').open('rb') as writer:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_source_and_pointer_writers_excluded_through_unlink(self):
        import fcntl
        unlink = os.unlink
        seen = []
        def observe(path, **kwargs):
            if path == 'member':
                from sync_from_repo import source_lock
                with self.assertRaisesRegex(ValueError, 'source_import_busy'):
                    with source_lock(self.base):
                        self.fail('source writer admitted during retirement')
                with custody.DECLARED_BASE_LOCK.open('rb') as writer:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append(path)
            return unlink(path, **kwargs)
        with patch.object(os, 'unlink', side_effect=observe):
            row = self.run_candidate(dry_run=False)
        self.assertEqual(row['status'], 'REMOVED', row)
        self.assertEqual(seen, ['member'])

    def test_busy_native_writer_refuses_before_effect(self):
        import fcntl
        with (self.jobs.parent / '.jobs.lock').open('rb') as writer:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_kept()

    def test_synthetic_421_exact_mkdtemp_names_are_eligible_without_deletion(self):
        # The original 421-target identity manifest is not retained. Historical
        # receipts retain only 405 names, all mkdtemp's six ASCII alphanumerics.
        # This synthetic cohort mirrors that exact grammar, not live identities.
        targets = []
        for number in range(421):
            target = self.tmp / f'aeo-declared-{number:06x}'
            target.mkdir()
            (target / 'payload.txt').write_text('rebuildable fixture\n' * 100)
            targets.append(target)
        rows = cold.idle_scratch(self.api, targets, pins=(), dry_run=True,
                                 deadline=time.monotonic() + 30)
        self.assertEqual(len(rows), 421)
        self.assertTrue(all(row['status'] == 'ELIGIBLE' for row in rows),
                        [row for row in rows if row['status'] != 'ELIGIBLE'][:3])
        self.assertGreater(sum(row['eligible_bytes'] for row in rows), 0)
        self.assertEqual(sum(row['bytes_freed'] for row in rows), 0)
        self.assertTrue(all((target / 'payload.txt').is_file() for target in targets))
        self.assertFalse(self.api.removed)

    def test_missing_real_pointer_contract_refuses_before_removal(self):
        self.base_pointer.unlink()
        self.assertIn('rebuild source', self.assert_kept()['reason'])

    def test_changed_predecessor_manifest_refuses_before_removal(self):
        self.base_manifest.write_text('{}')
        self.assertIn('predecessor digest', self.assert_kept()['reason'])

    def test_changed_predecessor_receipt_refuses_before_removal(self):
        self.base_receipt.write_text('{}')
        self.assertIn('predecessor digest', self.assert_kept()['reason'])

    def test_missing_predecessor_page_refuses_before_removal(self):
        (self.predecessor / 'public/how-we-rate/index.html').unlink()
        self.assert_kept()

    def test_predecessor_page_changed_during_process_scan_refuses(self):
        original = custody._process_scan
        def change(*args):
            original(*args)
            (self.predecessor / 'public/how-we-rate/index.html').write_text('changed predecessor')
        with patch.object(custody, '_process_scan', side_effect=change):
            self.assert_kept()

    def test_missing_rebuild_source_refuses_both_tmp_entries(self):
        self.rebuild_source.joinpath('tests/lastmod-declared-routes.test.mjs').unlink()
        self.assertIn('rebuild source', self.assert_kept()['reason'])
        import host_disk_auto_classes as auto
        rows = auto.stale_tmp(self.api, cold, pins=(), deadline=time.monotonic() + 10,
                              dry_run=False, root=self.tmp, min_bytes=0)
        self.assertEqual(rows[0]['status'], 'REFUSED', rows)
        self.assertIn('rebuild source', rows[0]['reason'])
        self.assertFalse(self.api.removed)

    def test_rebuild_source_alias_is_refused(self):
        source = self.rebuild_source / 'scripts/postbuild-aeo-surface.mjs'
        saved = self.rebuild_source / 'saved.mjs'
        source.rename(saved)
        source.symlink_to(saved)
        self.assertIn('rebuild source', self.assert_kept()['reason'])

    def test_rebuild_source_drift_during_process_scan_is_refused(self):
        original = custody._process_scan
        def changed(path, deadline, inodes):
            original(path, deadline, inodes)
            (self.rebuild_source / 'data/llms-priority.json').write_text('changed input')
        with patch.object(custody, '_process_scan', changed):
            self.assertIn('inventory changed', self.assert_kept()['reason'])

    def test_absence_after_final_snapshot_has_zero_reclaim_with_actual_helper(self):
        helper = DisposableHelperTests('test_real_child_retires_present_fixture')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        original = large.snapshot
        calls = []
        retained = self.base / 'retained-original'
        inode = (self.candidate / 'payload.txt').stat().st_ino
        def disappear(path, deadline):
            value = original(path, deadline)
            calls.append(path)
            if len(calls) == 5:
                path.rename(retained)
            return value
        with patch.object(large, 'snapshot', side_effect=disappear), \
                patch.object(self.api, 'remove_disposable', side_effect=helper.remove):
            row = self.run_candidate(dry_run=False)
        self.assertEqual(len(calls), 5)
        self.assertNotEqual(row['status'], 'REMOVED', row)
        self.assertEqual(row['bytes_freed'], 0, row)
        self.assertEqual((retained / 'payload.txt').stat().st_ino, inode)

    def test_empty_named_rebuild_files_are_refused(self):
        for relative in custody.DECLARED_REBUILD_FILES:
            (self.rebuild_source / relative).write_bytes(b'')
        self.assertIn('rebuild source', self.assert_kept()['reason'])

    def test_empty_copy_tree_is_refused(self):
        shutil.rmtree(self.rebuild_source / 'src')
        (self.rebuild_source / 'src').mkdir()
        self.assertIn('empty rebuild source directory', self.assert_kept()['reason'])

    def test_nested_existing_source_content_drift_is_refused(self):
        original = custody._process_scan
        source = self.rebuild_source / 'src/required.mjs'
        def changed(*args):
            original(*args)
            source.write_text('different existing file bytes\n')
        with patch.object(custody, '_process_scan', changed):
            self.assertIn('rebuild source inventory changed', self.assert_kept()['reason'])

    def test_missing_carrier_dependency_is_refused(self):
        (self.rebuild_source / 'assets/ezra-glp-1-reviews.webp').unlink()
        self.assertIn('rebuild source', self.assert_kept()['reason'])

    def test_corrupt_carrier_dependency_is_refused(self):
        (self.rebuild_source / 'assets/ezra-glp-1-reviews.webp').write_bytes(b'corrupt asset')
        self.assertIn('carrier asset digest', self.assert_kept()['reason'])

    def test_carrier_dependency_drift_after_inventory_is_refused(self):
        original = custody._process_scan
        source = self.rebuild_source / 'assets/ezra-glp-1-reviews.webp'
        def changed(*args):
            original(*args)
            source.write_bytes(b'changed carrier asset')
        with patch.object(custody, '_process_scan', changed):
            self.assertIn('rebuild source inventory changed', self.assert_kept()['reason'])

    def test_nested_source_alias_is_refused(self):
        source = self.rebuild_source / 'src/required.mjs'
        source.unlink()
        source.symlink_to(self.rebuild_source / 'scripts/postbuild-aeo-surface.mjs')
        self.assertIn('aliased', self.assert_kept()['reason'])

    def test_source_lost_on_final_check_stays_protected(self):
        original = custody.require_unreferenced
        calls = []
        def changed(path, deadline):
            calls.append(path)
            if len(calls) == 2:
                self.rebuild_source.joinpath('scripts/postbuild-aeo-surface.mjs').unlink()
            return original(path, deadline)
        with patch.object(custody, 'require_unreferenced', changed):
            self.assertIn('rebuild source', self.assert_kept()['reason'])
        self.assertEqual(len(calls), 2)

    def test_non_mkdtemp_declared_names_never_qualify(self):
        original = self.candidate
        for name in ('aeo-declared-', 'aeo-declared-idle', 'aeo-declared-abc12',
                     'aeo-declared-abc1234', 'aeo-declared-abc_12',
                     'aeo-declared-abc123.tar', 'aeo-declared-é12345'):
            with self.subTest(name=name):
                renamed = self.tmp / name
                original.rename(renamed)
                self.candidate = renamed
                try:
                    rows = cold.idle_scratch(self.api, [renamed], pins=(), dry_run=True,
                                             deadline=time.monotonic() + 10)
                    self.assertEqual(rows[0]['status'], 'REFUSED', rows)
                    self.assertTrue((renamed / 'payload.txt').is_file())
                finally:
                    renamed.rename(original)
                    self.candidate = original

    def test_protected_roots_never_qualify_even_with_declared_child(self):
        original = self.candidate
        for name in ('tmux-1000', '.org.chromium.Chromium.fixture',
                     'claude-1000', 'hermes-results'):
            with self.subTest(name=name):
                protected = self.tmp / name
                protected.mkdir()
                nested = protected / original.name
                original.rename(nested)
                rows = cold.idle_scratch(self.api, [protected, nested], pins=(),
                                         dry_run=False, deadline=time.monotonic() + 10)
                self.assertTrue(all(row['status'] == 'REFUSED' for row in rows), rows)
                self.assertTrue((nested / 'payload.txt').is_file())
                self.assertFalse(self.api.removed)
                nested.rename(original)

    def test_runtime_user_unit_is_a_scheduler_source(self):
        self.assertTrue(custody._schedule(Path('/run/user/1000/systemd/user/reader.service')))
        self.assertFalse(custody._schedule(Path('/run/user/1000/package/systemd/reader.service')))

    def test_hermes_cron_bytecode_is_not_a_scheduler_source(self):
        cache = self.links / 'hermes-agent/cron/__pycache__'
        cache.mkdir(parents=True)
        (cache / 'jobs.cpython-311.pyc').write_bytes(bytes.fromhex('a7 0d 0d 0a 00 ff'))
        result = self.run_candidate(dry_run=True)
        self.assertEqual(result['status'], 'ELIGIBLE', result)
        self.assertTrue(self.candidate.is_dir())
        self.assertFalse(self.api.removed)

    def test_masked_systemd_unit_does_not_pin_idle_directory(self):
        (self.schedules / 'masked.service').symlink_to('/dev/null')
        result = self.run_candidate(dry_run=True)
        self.assertEqual(result['status'], 'ELIGIBLE', result)
        self.assertTrue(self.candidate.is_dir())
        self.assertFalse(self.api.removed)

    def test_bytecode_tree_symlink_custody_is_still_checked(self):
        cache = self.links / 'hermes-agent/cron/__pycache__'
        cache.mkdir(parents=True)
        (cache / 'payload.pyc').symlink_to(self.candidate / 'payload.txt')
        self.assertIn('symlink', self.assert_kept()['reason'])

    def test_binary_actual_systemd_source_is_still_refused(self):
        (self.schedules / 'reader.service').write_bytes(bytes.fromhex('a7 0d 0d 0a 00 ff'))
        self.assert_kept()

    def test_device_other_than_null_is_not_a_masked_unit(self):
        (self.schedules / 'reader.service').symlink_to('/dev/zero')
        self.assertIn('unreadable tmp schedule', self.assert_kept()['reason'])

    def test_non_unit_link_to_null_is_not_a_masked_unit(self):
        (self.schedules / 'reader.conf').symlink_to('/dev/null')
        self.assertIn('unreadable tmp schedule', self.assert_kept()['reason'])

    def test_indirect_link_to_null_is_not_the_mask_exception(self):
        alias = self.base / 'null-alias'
        alias.symlink_to('/dev/null')
        (self.schedules / 'reader.service').symlink_to(alias)
        self.assertIn('unreadable tmp schedule', self.assert_kept()['reason'])

    def test_cron_link_to_null_is_not_a_masked_systemd_unit(self):
        cron = self.links / 'registered-cron'
        cron.mkdir()
        (cron / 'reader.service').symlink_to('/dev/null')
        with patch.object(custody, 'CRON_ROOTS', (cron,)):
            self.assertIn('unreadable tmp schedule', self.assert_kept()['reason'])

    def test_unreadable_registered_cron_source_is_still_refused(self):
        cron = self.links / 'registered-cron'
        cron.mkdir()
        (cron / 'reader').write_bytes(bytes.fromhex('a7 0d 0d 0a 00 ff'))
        with patch.object(custody, 'CRON_ROOTS', (cron,)):
            self.assert_kept()

    def test_unit_unmasked_during_process_scan_is_refused(self):
        unit = self.schedules / 'reader.service'
        unit.symlink_to('/dev/null')
        original = custody._process_scan
        def changed(path, deadline, inodes):
            original(path, deadline, inodes)
            unit.unlink()
            unit.write_text('[Service]\nWorkingDirectory=' + str(path))
        with patch.object(custody, '_process_scan', changed):
            self.assertIn('inventory changed', self.assert_kept()['reason'])

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
        target = str(self.candidate / 'payload.txt')
        (root / 'NOTE.txt').write_text('retain ' + target)
        report = self.base / 'report'
        report.mkdir()
        with patch.object(sweep, 'BUS', bus):
            pins = sweep.references(report)
        self.assertIn(Path(target), pins)


class DisposableHelperTests(unittest.TestCase):
    """Execute the maintained helper itself; every target belongs to scratch."""
    def setUp(self):
        import ast
        import subprocess
        self.subprocess = subprocess
        source = CANDIDATE / 'scripts/host_disk_pruner.py'
        node = next(n for n in ast.parse(source.read_bytes()).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'remove_disposable')
        self.helper_globals = dict(Path=Path, time=time, sys=sys, subprocess=subprocess,
                                   remaining_budget=lambda seconds: seconds)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), self.helper_globals)
        self.remove = self.helper_globals['remove_disposable']
        self.fixture = tempfile.TemporaryDirectory(prefix='glp354-real-helper-')
        self.addCleanup(self.fixture.cleanup)
        self.base = Path(self.fixture.name)
        self.target = self.base / 'target'
        self.target.mkdir()
        (self.target / 'payload').write_text('owned disposable fixture\n' * 100)

    def test_expected_root_replacement_is_retained_without_credit(self):
        expected, _ = large.snapshot(self.target, time.monotonic() + 10)
        saved = self.base / 'original'
        self.target.rename(saved)
        self.target.mkdir()
        (self.target / 'payload').write_text('unreviewed replacement')
        result = self.remove(self.target, expected=expected, deadline=time.monotonic() + 10)
        self.assertEqual(result['bytes_freed'], 0)
        self.assertEqual(result['status'], 'REFUSED')
        self.assertEqual((self.target / 'payload').read_text(), 'unreviewed replacement')
        self.assertTrue((saved / 'payload').exists())

    def test_expected_member_replacement_is_retained_without_credit(self):
        expected, _ = large.snapshot(self.target, time.monotonic() + 10)
        original = self.target / 'payload'
        original.rename(self.base / 'original-payload')
        original.write_text('unreviewed member')
        result = self.remove(self.target, expected=expected, deadline=time.monotonic() + 10)
        self.assertEqual(result['bytes_freed'], 0)
        self.assertEqual(result['status'], 'REFUSED')
        self.assertEqual(original.read_text(), 'unreviewed member')
        self.assertTrue((self.base / 'original-payload').exists())

    def test_expected_present_tree_has_own_retirement_accounting(self):
        (self.target / 'nested').mkdir()
        (self.target / 'nested/second').write_bytes(b'owned second payload' * 500)
        expected, amount = large.snapshot(self.target, time.monotonic() + 10)
        result = self.remove(self.target, expected=expected, deadline=time.monotonic() + 10)
        self.assertEqual(result['status'], 'REMOVED', result)
        self.assertEqual(result['bytes_freed'], amount)
        self.assertEqual(result['retired_members'], len(expected))
        self.assertFalse(self.target.exists())

    def test_expected_external_link_is_retired_without_allocation_credit(self):
        os.link(self.target / 'payload', self.base / 'external')
        expected, _ = large.snapshot(self.target, time.monotonic() + 10)
        result = self.remove(self.target, expected=expected, deadline=time.monotonic() + 10)
        self.assertEqual(result['status'], 'REMOVED', result)
        self.assertEqual(result['bytes_freed'], 0)
        self.assertTrue((self.base / 'external').exists())

    def test_root_replacement_inside_capture_is_restored_without_unlink(self):
        expected, _ = large.snapshot(self.target, time.monotonic() + 10)
        rename = os.rename
        saved = self.base / 'original'
        def replace(source, destination, **kwargs):
            if source == self.target.name:
                rename(self.target, saved)
                self.target.mkdir()
                (self.target / 'payload').write_text('replacement at rename')
            return rename(source, destination, **kwargs)
        with patch.object(os, 'rename', side_effect=replace):
            result = self.remove(self.target, expected=expected, deadline=time.monotonic() + 10)
        self.assertEqual(result['status'], 'REFUSED', result)
        self.assertEqual(result['bytes_freed'], 0)
        self.assertEqual((self.target / 'payload').read_text(), 'replacement at rename')
        self.assertTrue((saved / 'payload').exists())

    def test_member_replacement_inside_capture_is_restored_without_unlink(self):
        expected, _ = large.snapshot(self.target, time.monotonic() + 10)
        rename = os.rename
        saved = self.base / 'original-payload'
        def replace(source, destination, **kwargs):
            if source == 'payload':
                rename(source, saved, src_dir_fd=kwargs['src_dir_fd'])
                fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600, dir_fd=kwargs['src_dir_fd'])
                try:
                    os.write(fd, b'replacement at member rename')
                finally:
                    os.close(fd)
            return rename(source, destination, **kwargs)
        with patch.object(os, 'rename', side_effect=replace):
            result = self.remove(self.target, expected=expected, deadline=time.monotonic() + 10)
        self.assertEqual(result['status'], 'REFUSED', result)
        self.assertEqual(result['bytes_freed'], 0)
        self.assertEqual((self.target / 'payload').read_text(), 'replacement at member rename')
        self.assertTrue(saved.exists())

    def test_internal_hardlinks_earn_one_last_link_allocation(self):
        os.link(self.target / 'payload', self.target / 'second')
        expected, amount = large.snapshot(self.target, time.monotonic() + 10)
        result = self.remove(self.target, expected=expected, deadline=time.monotonic() + 10)
        self.assertEqual(result['status'], 'REMOVED', result)
        self.assertEqual(result['bytes_freed'], amount)

    def test_real_child_retires_present_fixture(self):
        self.assertTrue(self.remove(self.target, deadline=time.monotonic() + 10))
        self.assertFalse(self.target.exists())

    def test_absent_name_never_dispatches_or_earns_credit(self):
        retained = self.base / 'retained-original'
        self.target.rename(retained)
        with patch.object(self.subprocess, 'run') as transport:
            self.assertFalse(self.remove(self.target, deadline=time.monotonic() + 10))
        transport.assert_not_called()
        self.assertTrue((retained / 'payload').is_file())

    def test_disappearance_at_child_handoff_is_not_success(self):
        transport = self.subprocess.run
        retained = self.base / 'retained-original'
        inode = (self.target / 'payload').stat().st_ino
        def disappear(command, **kwargs):
            self.target.rename(retained)
            return transport(command, **kwargs)
        with patch.object(self.subprocess, 'run', side_effect=disappear):
            self.assertFalse(self.remove(self.target, deadline=time.monotonic() + 10))
        self.assertEqual((retained / 'payload').stat().st_ino, inode)

    def test_timeout_and_absence_cannot_prove_owned_retirement(self):
        retained = self.base / 'retained-original'
        def timeout(command, **kwargs):
            self.target.rename(retained)
            raise self.subprocess.TimeoutExpired(command, 0.01)
        with patch.object(self.subprocess, 'run', side_effect=timeout):
            self.assertFalse(self.remove(self.target, deadline=time.monotonic() + 10))
        self.assertTrue((retained / 'payload').is_file())


if __name__ == '__main__':
    if sys.argv[1:] == ['--pytest-guards']:
        import pytest
        raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider',
            str(CANDIDATE / 'tests/test_host_disk_auto_classes.py'),
            str(CANDIDATE / 'tests/test_host_disk_large_guards.py'),
            str(CANDIDATE / 'tests/test_host_disk_custody.py'),
            str(CANDIDATE / 'tests/test_host_disk_report_caches.py'),
            str(CANDIDATE / 'tests/test_host_disk_r2.py')]))
    unittest.main()
