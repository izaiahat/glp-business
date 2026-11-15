"""Reader regressions: synthetic child output, no CBS requests."""
import fcntl
import hashlib
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPT = Path('/home/ubuntu/.hermes/scripts/cbs_pool_read.py')
spec = importlib.util.spec_from_file_location('cbs_reader', SCRIPT)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class ReaderRecoveryTests(unittest.TestCase):
    def test_competing_reader_waits_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = threading.Event()
            def hold_lock():
                with (root / '.reader.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    ready.set()
                    time.sleep(0.2)
            owner = threading.Thread(target=hold_lock)
            owner.start()
            self.assertTrue(ready.wait(1))
            try:
                completed = subprocess.CompletedProcess([], 0, '{"synthetic":true}', '')
                with patch.object(reader.subprocess, 'run', return_value=completed) as run:
                    result = reader.read_pool(root, budget=2, lock_wait=1)
                self.assertEqual(result, {'synthetic': True})
                run.assert_called_once()
            finally:
                owner.join()

    def test_one_transient_retry_then_success(self):
        with tempfile.TemporaryDirectory() as directory:
            outcomes = [
                subprocess.CompletedProcess([], 1, '', 'Error: DOM_NOT_READY: weekly'),
                subprocess.CompletedProcess([], 0, '{"synthetic":true}', ''),
            ]
            with patch.object(reader.subprocess, 'run', side_effect=outcomes) as run:
                result = reader.read_pool(Path(directory), budget=2)
            self.assertEqual(result, {'synthetic': True})
            self.assertEqual(run.call_count, 2)
            self.assertLessEqual(run.call_args.kwargs['timeout'], 2)

    def test_terminal_failure_is_not_retried_or_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / 'snapshot-latest.json'
            snapshot.write_text('{"stale":true}')
            before = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            for message, code in [('LOGIN_REQUIRED: redirected to login/join', 'LOGIN_REQUIRED'),
                                  ('OWN_ENTRY_AMBIGUOUS', 'OWN_ENTRY_AMBIGUOUS')]:
                with self.subTest(code=code):
                    failure = subprocess.CompletedProcess([], 1, '', 'Error: ' + message)
                    with patch.object(reader.subprocess, 'run', return_value=failure) as run:
                        with self.assertRaisesRegex(RuntimeError, 'CBS_READ_FAILED: ' + code):
                            reader.read_pool(root, budget=2)
                    run.assert_called_once()
                    self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), before)

    def test_retry_is_bounded_and_failure_preserves_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / 'snapshot-latest.json'
            snapshot.write_text('{"stale":true}')
            before = snapshot.read_bytes()
            failure = subprocess.CompletedProcess([], 1, '', 'Error: INCOMPLETE_SLATE: DOM count mismatch')
            with patch.object(reader.subprocess, 'run', return_value=failure) as run:
                with self.assertRaisesRegex(RuntimeError, 'CBS_READ_FAILED: INCOMPLETE_SLATE'):
                    reader.read_pool(root, budget=2)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(snapshot.read_bytes(), before)

    def test_collector_and_gate_preserve_sanitized_failure_code(self):
        failure = subprocess.CompletedProcess([], 1, '',
            'CBS_READ_FAILED: DOM_NOT_READY:weekly; NEVER_ECHO_BROWSER_TEXT')
        with patch('subprocess.run', return_value=failure), patch('sys.argv', ['context', '--manual-review']):
            with self.assertRaisesRegex(SystemExit, 'CBS_READ_FAILED: DOM_NOT_READY:weekly') as error:
                runpy.run_path('/home/ubuntu/.hermes/scripts/cbs_pool_research_context.py', run_name='__main__')
        self.assertNotIn('NEVER_ECHO', str(error.exception))
        def fake_read(path, *args, **kwargs):
            if path.name == 'config.json':return '{"research_enabled":true}'
            if path.name == 'snapshot-latest.json':return '{"retrieved_at":"2000-01-01T00:00:00Z"}'
            raise AssertionError('Unexpected file read: ' + str(path))
        output = io.StringIO()
        with patch('subprocess.run', return_value=failure), patch.object(Path, 'read_text', fake_read):
            with contextlib.redirect_stdout(output):
                runpy.run_path('/home/ubuntu/.hermes/scripts/cbs_pool_experiment_due.py')
        result = json.loads(output.getvalue())
        self.assertEqual(result['reader_failure'], 'CBS_READ_FAILED: DOM_NOT_READY:weekly')
        self.assertNotIn('NEVER_ECHO', output.getvalue())


if __name__ == '__main__':
    unittest.main()
