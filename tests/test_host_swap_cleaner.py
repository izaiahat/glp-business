#!/usr/bin/env python3
"""Effect-free guard tests; does not signal processes or invoke swap utilities."""
import importlib.util
import unittest
from unittest.mock import patch
from pathlib import Path
spec = importlib.util.spec_from_file_location('cleaner', '/home/ubuntu/business/scripts/host_swap_cleaner.py')
assert spec is not None and spec.loader is not None
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class GuardTests(unittest.TestCase):
    def mem(self, **kw):
        return {'available_kib': 10 * 1048576, 'swap_used_kib': 4 * 1048576, 'load1': 2, **kw}

    def test_exact_margin(self):
        self.assertIsNone(c.guard(self.mem(), 0, 0))

    def test_insufficient_margin(self):
        self.assertIsNotNone(c.guard(self.mem(available_kib=10 * 1048576 - 1), 0, 0))

    def test_load_boundary(self):
        self.assertIsNone(c.guard(self.mem(load1=4), 0, 0))
        self.assertEqual(c.guard(self.mem(load1=4.01), 0, 0), 'load_above_4')

    def test_pressure_boundary(self):
        self.assertIsNotNone(c.guard(self.mem(), .5, 0))
        self.assertIsNone(c.guard(self.mem(), .49, 0))

    def test_swap_io_boundary(self):
        self.assertIsNotNone(c.guard(self.mem(), 0, .25))
        self.assertIsNone(c.guard(self.mem(), 0, .249))

    def test_orphan_browser_controls(self):
        r = {'pid': 123, 'ppid': 1, 'age': 90000, 'args': [b'/opt/google/chrome/chrome', b'--headless=new']}
        self.assertTrue(c.orphan_browser(r, {123: r}, ([], [])))
        for arg in (b'--remote-debugging-port=9324', b'--remote-debugging-pipe', b'--user-data-dir=katalys', b'--type=renderer'):
            self.assertFalse(c.orphan_browser({**r, 'args': r['args'] + [arg]}, {123: r}, ([], [])))
        self.assertFalse(c.orphan_browser({**r, 'ppid': 99}, {123: r}, ([], [])))
        self.assertFalse(c.orphan_browser({**r, 'age': 100}, {123: r}, ([], [])))
        self.assertFalse(c.orphan_browser(r, {123: r}, (['ESTAB 0 0 x:1 y:2 users:(("chrome",pid=123,fd=1))'], [])))

    def test_daemon_early_vetoes(self):
        r = {'pid': 123, 'ppid': 1, 'age': 90000, 'args': [b'python', b'-m', b'browser_harness.daemon'], 'env': {b'BU_NAME': b'old-task'}}
        for key, val in [('ppid', 2), ('age', 100), ('args', [b'python']), ('env', {b'BU_NAME': b'default'}), ('env', {b'BU_NAME': b'operator'}), ('env', {b'BU_NAME': b'old-task', b'BU_BROWSER_ID': b'cloud-id'})]:
            self.assertFalse(c.dormant_daemon({**r, key: val}, {123: r}, ([], []), 0))
        child = {'pid': 124, 'ppid': 123, 'args': [], 'env': {}}
        self.assertFalse(c.dormant_daemon(r, {123: r, 124: child}, ([], []), 0))
        caller = {'pid': 124, 'ppid': 1, 'args': [b'python', b'client.py'], 'env': r['env']}
        self.assertFalse(c.dormant_daemon(r, {123: r, 124: caller}, ([], []), 0))

    def test_daemon_positive_and_peer_veto(self):
        from unittest.mock import MagicMock
        import time
        row = {'pid': 123, 'ppid': 1, 'age': 90001,
               'args': [b'python', b'-m', b'browser_harness.daemon'],
               'env': {b'BU_NAME': b'historical-task'}}
        peers = ['u_str ESTAB 0 0 * 11 * 12 users:(("python",pid=123,fd=5))',
                 'u_str ESTAB 0 0 * 12 * 11 users:(("python",pid=123,fd=6))']
        log = MagicMock()
        log.resolve.return_value.name = 'bu-historical-task.log'
        log.stat.return_value.st_mtime = time.time() - 90001
        fd = MagicMock()
        entries = [MagicMock(), MagicMock()]
        entries[0].readlink.return_value = 'socket:[11]'
        entries[1].readlink.return_value = 'socket:[12]'
        fd.iterdir.return_value = entries
        def fake_path(*parts):
            return log if parts[-1] == '2' else fd
        with patch.object(c, 'Path', side_effect=fake_path):
            self.assertTrue(c.dormant_daemon(row, {123: row}, ([], peers), time.time()))
            tcp = ['ESTAB 0 0 x:1 y:2 users:(("python",pid=123,fd=10))']
            self.assertFalse(c.dormant_daemon(row, {123: row}, (tcp, peers), time.time()))
            external = peers + ['u_str ESTAB 0 0 * 11 * 99 users:(("python",pid=123,fd=5))']
            self.assertFalse(c.dormant_daemon(row, {123: row}, ([], external), time.time()))
            log.stat.return_value.st_mtime = time.time()
            self.assertFalse(c.dormant_daemon(row, {123: row}, ([], peers), time.time()))


if __name__ == '__main__':
    unittest.main()
