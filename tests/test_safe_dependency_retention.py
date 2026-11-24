import contextlib
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import safe_dependency_retention as m


class SafeDependencyRetentionTests(unittest.TestCase):
    def test_discovery_only_dependencies_no_follow_no_nested_modules(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'run/node_modules/nested/node_modules').mkdir(parents=True)
            (root/'run/RESULT.json').write_text('real evidence')
            (root/'browser-profile/Default/node_modules').mkdir(parents=True)
            (root/'alias').symlink_to(root/'run', target_is_directory=True)
            self.assertEqual(list(m.dependencies(root,time.monotonic()+5)),[root/'run/node_modules'])
            self.assertEqual((root/'run/RESULT.json').read_text(),'real evidence')

    def test_foreign_owner_and_discovery_deadline_refuse(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(m.os,'getuid',return_value=os.getuid()+1):
                with self.assertRaisesRegex(ValueError,'foreign owner'):
                    m.owned_tree(root,time.monotonic()+5)
            with self.assertRaises(TimeoutError):
                list(m.dependencies(root,time.monotonic()-1))

    def test_actual_caller_retires_real_old_ignored_dependencies(self):
        import subprocess
        import host_disk_auto_classes as auto
        import host_disk_custody as custody
        import host_disk_cold_classes as cold
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); run = root/'run'; run.mkdir()
            subprocess.run(['git','init','-q',str(run)], check=True)
            (run/'.gitignore').write_text('node_modules/\n')
            (run/'package-lock.json').write_text('{}')
            modules = run/'node_modules'; modules.mkdir()
            (modules/'index.js').write_text('reinstallable dependency' * 4096)
            (run/'RESULT.json').write_text('retained evidence')
            lock = root/'singleton'; lock.touch()
            # Isolate reference sources and advance wall time (ctime cannot be
            # backdated). Process/lsof/identity/payload/deletion gates stay real.
            with patch.object(m,'BUS',root), patch.object(m,'ROOTS',(root,)), patch.object(m,'LOCK',lock), patch.object(auto,'external_pins',return_value=()), patch.object(custody,'collect',return_value=()), patch.object(cold,'LIVE_HOME',root/'absent-home'), patch.object(time,'time',return_value=time.time()+4*86400):
                self.assertEqual(m.clean()['rows'][0]['status'], 'ELIGIBLE')
                self.assertTrue(modules.exists())
                self.assertEqual(m.clean(apply=True)['rows'][0]['status'], 'REMOVED')
                self.assertFalse(modules.exists())
                self.assertEqual((run/'RESULT.json').read_text(), 'retained evidence')
                modules.mkdir(); (modules/'RESULT.json').write_text('embedded evidence')
                self.assertEqual(m.clean(apply=True)['rows'][0]['status'], 'REFUSED')
                self.assertEqual((modules/'RESULT.json').read_text(), 'embedded evidence')

    def test_busy_existing_singleton_preserves_all_payloads(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);lock=root/'singleton';lock.touch()
            with lock.open('r') as owner:
                fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with patch.object(m,'BUS',root),patch.object(m,'LOCK',lock):
                    with self.assertRaises(BlockingIOError):m.clean(apply=True)
            self.assertTrue(lock.exists())

    def test_schedule_and_scope_exclude_backups_reports_tmp_and_ahs_effects(self):
        repo=Path(__file__).parents[1]
        timer=(repo/'ops/systemd/kael-safe-dependency-retention.timer').read_text()
        self.assertIn('OnCalendar=*-*-* *:45:00',timer)
        self.assertTrue(all(str(p).startswith('/home/ubuntu/business/') for p in m.ROOTS))
        self.assertNotIn(Path('/tmp'),m.ROOTS)
        self.assertFalse(any('backup' in str(p) for p in m.ROOTS))


if __name__ == '__main__':unittest.main()
