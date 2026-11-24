"""Real filesystem counterexamples; no replacement deletion guard."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import host_disk_large_classes as large
import host_disk_pruner as api
import host_disk_auto_classes as auto
import safe_dependency_retention as retention


class DependencyRetirementSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.parent = self.base / 'build'
        self.path = self.parent / 'node_modules'
        self.path.mkdir(parents=True)
        (self.path / 'payload.js').write_text('reinstallable\n')
        self.deadline = time.monotonic() + 30

    def test_real_retirement_and_symlink_leaf_do_not_follow_target(self):
        evidence = self.base / 'RESULT.json'
        evidence.write_text('preserve')
        (self.path / 'alias').symlink_to(evidence)
        before, _ = large.snapshot(self.path, self.deadline)
        self.assertTrue(api.remove_dependency_tree(self.path, before, deadline=self.deadline))
        self.assertFalse(self.path.exists())
        self.assertEqual(evidence.read_text(), 'preserve')

    def test_parent_symlink_swap_after_open_refuses_without_touching_either_tree(self):
        before, _ = large.snapshot(self.path, self.deadline)
        elsewhere = self.base / 'outside'
        (elsewhere / 'node_modules').mkdir(parents=True)
        protected = elsewhere / 'node_modules/RESULT.json'
        protected.write_text('preserve')
        real_check = api.path_is_active_privileged
        swapped = False
        def swap(path):
            nonlocal swapped
            if not swapped:
                swapped = True
                self.parent.rename(self.base / 'original')
                self.parent.symlink_to(elsewhere, target_is_directory=True)
            return real_check(path)
        with patch.object(api, 'path_is_active_privileged', side_effect=swap):
            self.assertFalse(api.remove_dependency_tree(self.path, before, deadline=self.deadline))
        self.assertEqual(protected.read_text(), 'preserve')
        self.assertTrue((self.base / 'original/node_modules/payload.js').is_file())

    def test_new_live_user_at_final_check_preserves_tree(self):
        before, _ = large.snapshot(self.path, self.deadline)
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=self.path)
        try:
            self.assertFalse(api.remove_dependency_tree(self.path, before, deadline=self.deadline))
            self.assertTrue((self.path / 'payload.js').is_file())
        finally:
            child.terminate(); child.wait(timeout=5)

    def test_same_filesystem_bind_mount_is_rejected_in_private_namespace(self):
        # Mount exists only in the child namespace, never on the host.
        source = self.base / 'reports'
        source.mkdir(); (source / 'RESULT.json').write_text('preserve')
        mountpoint = self.path / 'mounted'
        mountpoint.mkdir()
        code = '''import pathlib,subprocess,sys,time
sys.path.insert(0,sys.argv[1])
import host_disk_large_classes as large
import host_disk_pruner as api
source,target,root=map(pathlib.Path,sys.argv[2:])
expected,_=large.snapshot(root,time.monotonic()+5)
subprocess.run(['mount','--bind',str(source),str(target)],check=True)
try:
    assert not api.remove_dependency_tree(root,expected,deadline=time.monotonic()+5)
    try: large.snapshot(root,time.monotonic()+5)
    except ValueError as error: assert 'mount' in str(error),error
    else: raise AssertionError('same-device bind mount admitted')
    assert (source/'RESULT.json').read_text()=='preserve'
finally: subprocess.run(['umount',str(target)],check=True)
'''
        command = ['sudo','-n','unshare','--mount','--propagation','private',sys.executable,'-c',code,
                   str(Path(large.__file__).parent),str(source),str(mountpoint),str(self.path)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        if 'Operation not permitted' in result.stderr or 'sudo: a password' in result.stderr:
            self.skipTest('private mount namespace unavailable: '+result.stderr.strip())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((source / 'RESULT.json').read_text(), 'preserve')

    def test_ignored_dependency_result_is_not_rebuildable(self):
        (self.parent / 'package-lock.json').write_text('{}')
        (self.path / 'RESULT.json').write_text('unique evidence')
        with self.assertRaisesRegex(ValueError, 'evidence'):
            auto.reject_dependency_evidence(self.path, large.snapshot(self.path, self.deadline)[0])
        self.assertTrue((self.path / 'RESULT.json').exists())

    def test_inventory_caps_refuse_whole_candidate(self):
        with self.assertRaisesRegex(ValueError, 'item cap'):
            large.snapshot(self.path, self.deadline, max_items=1)
        with self.assertRaisesRegex(ValueError, 'byte cap'):
            large.snapshot(self.path, self.deadline, max_bytes=1)
        self.assertTrue((self.path / 'payload.js').is_file())

    def test_activity_checks_use_unprivileged_reads_and_only_escalate_unknown(self):
        from types import SimpleNamespace
        with patch.object(large.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout=b'', stderr=b'')) as read:
            self.assertTrue(large.lsof_inactive(self.path))
            self.assertEqual(read.call_args.args[0], ['lsof','-nP','+D',str(self.path)])
        inactive = {'returncode':0, 'stdout_tail':'INACTIVE'}
        unknown = {'returncode':2, 'stdout_tail':'UNKNOWN'}
        with patch.object(api, 'run', return_value=inactive) as read:
            self.assertFalse(api.path_is_active_privileged(self.path))
            self.assertEqual(read.call_count, 1)
            self.assertEqual(read.call_args.args[0][:3], ['/usr/bin/python3','-I','-B'])
        with patch.object(api.os, 'geteuid', return_value=1000), patch.object(api, 'run', side_effect=[unknown, inactive]) as read:
            self.assertFalse(api.path_is_active_privileged(self.path))
            first, second = [call.args[0] for call in read.call_args_list]
            self.assertEqual(second, ['sudo','-n',*first])
            self.assertTrue(first[3].endswith('/pruner_process_reference_guard.py'))
        with patch.object(api.os, 'geteuid', return_value=1000), patch.object(api, 'run', side_effect=[unknown, unknown]):
            self.assertTrue(api.path_is_active_privileged(self.path))

    def test_partial_deletion_log_survives_later_discovery_timeout(self):
        import contextlib
        import io
        root = self.base
        lock = root / 'singleton'; lock.touch()
        def discover(*args):
            yield self.path
            raise TimeoutError('discovery incomplete')
        subprocess.run(['git','init','-q',str(self.parent)], check=True)
        (self.parent/'.gitignore').write_text('node_modules/\n')
        (self.parent/'package-lock.json').write_text('{}')
        stream = io.StringIO()
        import host_disk_cold_classes as cold
        import host_disk_custody as custody
        with patch.object(retention,'BUS',root), patch.object(retention,'ROOTS',(root,)), patch.object(retention,'LOCK',lock), patch.object(retention,'dependencies',side_effect=discover), patch.object(auto,'external_pins',return_value=()), patch.object(custody,'collect',return_value=()), patch.object(cold,'LIVE_HOME',root/'absent-home'), patch.object(time,'time',return_value=time.time()+4*86400), contextlib.redirect_stdout(stream):
            with self.assertRaises(TimeoutError): retention.clean(apply=True)
        self.assertFalse(self.path.exists())
        self.assertIn('"status": "REMOVED"', stream.getvalue())
        self.assertIn('"bytes_freed":', stream.getvalue())

    def test_registered_entry_has_hard_time_limit_for_reference_collection(self):
        def slow():
            print('reference collection started', flush=True)
            time.sleep(30)
        started = time.monotonic()
        self.assertEqual(retention.run_bounded(slow, 0.1), 75)
        self.assertLess(time.monotonic() - started, 3)


if __name__ == '__main__': unittest.main()
