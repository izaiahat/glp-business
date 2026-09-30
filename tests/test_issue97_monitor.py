"""One offline group; all GitHub reads are synthetic and local state is temporary."""
import importlib.util
import json
import pathlib
import tempfile
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'scripts/issue97_monitor.py'


class Issue97MonitorTests(unittest.TestCase):
    def load(self):
        self.assertTrue(SOURCE.is_file(), 'scheduled issue97 monitor is missing')
        spec = importlib.util.spec_from_file_location('monitor', SOURCE)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_duplicate_tick_own_echo_and_fresh_legacy_cursor(self):
        m = self.load()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cursor = root / 'cursor.json'
            cursor.write_text('{"last_id":"100"}')
            inbox = root / 'monitor'
            comments = [dict(id=101, body='Fable-Operator | own'),
                        dict(id=102, body='Rook\n\x1b[31m', user={'login': 'rook'})]
            read = lambda: comments
            m.tick(inbox, cursor, read=read, now=lambda: 1)
            m.tick(inbox, cursor, read=read, now=lambda: 1)
            state = json.loads((inbox / 'inbox.json').read_text())
            self.assertEqual(state['last_id'], '102')
            self.assertEqual(len(state['pending']), 1)
            self.assertEqual([c['id'] for c in state['pending'][0]['comments']], ['102'])
            self.assertEqual(cursor.read_text(), '{"last_id":"100"}')
            # The parent may advance its cursor; never regress or redeliver it.
            cursor.write_text('{"last_id":"104"}')
            m.tick(inbox, cursor, read=lambda: comments + [dict(id=103, body='old')], now=lambda: 1)
            self.assertEqual(json.loads((inbox / 'inbox.json').read_text())['last_id'], '104')
            self.assertEqual(cursor.read_text(), '{"last_id":"104"}')

    def test_failure_once_and_bounded_queue_without_cursor_loss(self):
        m = self.load()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cursor = root / 'cursor.json'
            cursor.write_text('{"last_id":"100"}')
            inbox = root / 'monitor'
            def broken():
                raise RuntimeError('RAW_SECRET_MUST_NOT_ESCAPE')
            m.tick(inbox, cursor, read=broken, now=lambda: 1)
            first = (inbox / 'inbox.json').read_bytes()
            m.tick(inbox, cursor, read=broken, now=lambda: 1)
            self.assertEqual((inbox / 'inbox.json').read_bytes(), first)
            self.assertNotIn(b'RAW_SECRET', first)
            self.assertEqual(json.loads(first)['last_id'], '100')
            m.tick(inbox, cursor, read=lambda: [], now=lambda: 1)
            self.assertFalse(json.loads((inbox / 'inbox.json').read_text())['failure_latched'])
            m.tick(inbox, cursor, read=lambda: [dict(id=i, body='x') for i in range(101, 100 + m.MAX_PENDING * m.BATCH_SIZE + 2)], now=lambda: 1)
            state = json.loads((inbox / 'inbox.json').read_text())
            self.assertEqual(state['last_id'], '100')
            self.assertEqual(state['pending'], [])
            self.assertTrue(state['failure_latched'])
    def test_receive_queued_claim_once_and_arm_before_drain(self):
        m = self.load()
        self.assertTrue(hasattr(m, 'receive'), 'blocking local receive-one is missing')
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cursor = root / 'cursor.json'
            cursor.write_text('{"last_id":"100"}')
            inbox = root / 'monitor'
            m.tick(inbox, cursor, read=lambda: [dict(id=101, body='queued')], now=lambda: 1)
            result = m.receive(inbox, cursor, max_wait=0, now=lambda: 1)
            self.assertEqual(result['comments'][0]['id'], '101')
            self.assertEqual(m.receive(inbox, cursor, max_wait=0, now=lambda: 1)['status'], 'DEADLINE')
            state = json.loads((inbox / 'inbox.json').read_text())
            self.assertEqual(state['last_claim'], '101')
            self.assertEqual(state['pending'], [])
            original = m.watch
            def raced(path):
                fd = original(path)
                m.tick(inbox, cursor, read=lambda: [dict(id=102, body='arm/drain race')], now=lambda: 1)
                return fd
            with patch.object(m, 'watch', side_effect=raced):
                self.assertEqual(m.receive(inbox, cursor, max_wait=0, now=lambda: 1)['comments'][0]['id'], '102')

    def test_new_inotify_event_no_poll_and_listener_singleton(self):
        m = self.load()
        self.assertTrue(hasattr(m, 'receive'), 'blocking local receive-one is missing')
        import threading
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cursor = root / 'cursor.json'
            cursor.write_text('{"last_id":"100"}')
            inbox = root / 'monitor'
            with m.lock(inbox, 'receiver', blocking=False):
                self.assertEqual(m.receive(inbox, cursor, max_wait=0, now=lambda: 1)['status'], 'RECEIVER_BUSY')
            with m.lock(inbox, 'tick', blocking=False):
                m.tick(inbox, cursor, read=lambda: self.fail('duplicate tick read'), now=lambda: 1)
            waiting = threading.Event()
            original = m.select.select
            results = []
            def blocking(*args):
                waiting.set()
                return original(*args)
            with patch.object(m.select, 'select', side_effect=blocking), patch.object(m.time, 'sleep', side_effect=AssertionError('no timer polling')):
                thread = threading.Thread(target=lambda: results.append(m.receive(inbox, cursor, max_wait=2, now=lambda: 1)))
                thread.start()
                try:
                    self.assertTrue(waiting.wait(1), 'listener never reached a real select wait')
                    m.tick(inbox, cursor, read=lambda: [dict(id=101, body='event')], now=lambda: 1)
                finally:
                    thread.join(3)
                self.assertFalse(thread.is_alive())
            self.assertEqual(results[0]['comments'][0]['id'], '101')

    def test_deadline_failure_claim_and_output_controls(self):
        m = self.load()
        self.assertTrue(hasattr(m, 'receive'), 'blocking local receive-one is missing')
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cursor = root / 'cursor.json'
            cursor.write_text('{"last_id":"100"}')
            inbox = root / 'monitor'
            m.tick(inbox, cursor, read=lambda: self.fail('expired network read'), now=lambda: m.EXPIRY)
            self.assertEqual(m.receive(inbox, cursor, max_wait=2, now=lambda: m.EXPIRY)['status'], 'EXPIRED')
            self.assertEqual(m.receive(inbox, cursor, max_wait=0, now=lambda: 1)['status'], 'DEADLINE')
            def broken():
                raise OSError('secret failure')
            m.tick(inbox, cursor, read=broken, now=lambda: 1)
            self.assertEqual(m.receive(inbox, cursor, max_wait=0, now=lambda: 1)['status'], 'TICK_FAILED')
            m.tick(inbox, cursor, read=broken, now=lambda: 1)
            self.assertEqual(m.receive(inbox, cursor, max_wait=0, now=lambda: 1)['status'], 'DEADLINE')
            m.tick(inbox, cursor, read=lambda: [dict(id=i, body='\u202e\x1b\n' * 4000) for i in range(101, 109)], now=lambda: 1)
            result = m.receive(inbox, cursor, max_wait=0, now=lambda: 1)
            encoded = m.encode(result)
            self.assertLessEqual(len(encoded.encode()), m.MAX_OUTPUT)
            self.assertNotIn('\x1b', encoded)
            self.assertNotIn('\n', encoded)
            self.assertTrue(all(c['body_truncated'] for c in result['comments']))
            with patch.object(m, 'watch', side_effect=OSError('SECRET')):
                self.assertEqual(m.receive(inbox, cursor, max_wait=0, now=lambda: 1)['status'], 'RECEIVE_FAILED')
    def test_gh_exact_target_process_bounds_and_safe_failures(self):
        m = self.load()
        from unittest.mock import patch
        import subprocess
        import sys
        original = subprocess.Popen
        commands = []
        def fake(command, **kwargs):
            commands.append(command)
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
            return original([sys.executable, '-c', 'print(\'{"id":101,"body":"synthetic"}\')'], **kwargs)
        with patch.object(m.subprocess, 'Popen', side_effect=fake):
            self.assertEqual(m.gh_read()[0]['id'], 101)
        self.assertEqual(commands[0][-1], 'repos/izaiahat/glp-business/issues/97/comments?per_page=100')
        self.assertEqual(commands[0][:3], ['gh', 'api', '--paginate'])
        with patch.object(m.subprocess, 'Popen', side_effect=fake), patch.object(m.select, 'select', return_value=([], [], [])):
            with self.assertRaises(TimeoutError):
                m.gh_read()
        def too_large(command, **kwargs):
            return original([sys.executable, '-c', 'import sys; sys.stdout.write("x" * (4 * 1024 * 1024 + 1))'], **kwargs)
        with patch.object(m.subprocess, 'Popen', side_effect=too_large):
            with self.assertRaises(ValueError):
                m.gh_read()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cursor = root / 'cursor.json'
            cursor.write_text('not-json')
            result = m.tick(root / 'monitor', cursor, read=lambda: [], now=lambda: 1)
            self.assertEqual(result['status'], 'TICK_STATE_UNAVAILABLE')
            self.assertNotIn('not-json', m.encode(result))
    def test_cli_watchdog_on_blocked_state_lock_and_watch_loss(self):
        m = self.load()
        import subprocess
        import sys
        import struct
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cursor = root / 'cursor.json'
            cursor.write_text('{"last_id":"100"}')
            inbox = root / 'monitor'
            # Exercise the real main/CLI alarm with a future fixture clock even
            # after this short-lived pilot expires. No live listener or network.
            code = ('import runpy,time; m=runpy.run_path(' + repr(str(SOURCE)) + '); '
                    'm["main"].__globals__["EXPIRY"]=time.time()+100; m["main"]()')
            with m.lock(inbox, 'state'):
                response = subprocess.run([sys.executable, '-B', '-c', code,
                    '--receive-one', '--root', str(inbox), '--cursor', str(cursor), '--max-wait', '0'],
                    capture_output=True, text=True, timeout=5)
            self.assertEqual(response.returncode, 0)
            self.assertEqual(response.stderr, '')
            self.assertEqual(json.loads(response.stdout)['status'], 'DEADLINE')
            with patch.object(m.select, 'select', return_value=([1], [], [])), patch.object(m.os, 'read', return_value=struct.pack('iIII', -1, 0x4000, 0, 0)):
                self.assertEqual(m.receive(inbox, cursor, max_wait=1, now=lambda: 1)['status'], 'RECEIVE_FAILED')


if __name__ == '__main__':
    unittest.main()
