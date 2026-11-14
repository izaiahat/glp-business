"""Offline tests: no production scheduler, backup, or Discord calls."""
import importlib.util
from pathlib import Path
import unittest
import tempfile
import json
from unittest.mock import Mock, patch
from contextlib import contextmanager

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/disk_alarm.py'


def load():
    spec = importlib.util.spec_from_file_location('disk_alarm_test', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DiskAlarmTests(unittest.TestCase):
    def test_exact_thresholds(self):
        self.assertTrue(SOURCE.is_file(), 'disk alarm producer is missing')
        alarm = load()
        for available, expected in [(60 * 2**30, 'healthy'), (60 * 2**30 - 1, 'warning'),
                                    (40 * 2**30, 'warning'), (40 * 2**30 - 1, 'critical')]:
            with self.subTest(available=available):
                self.assertEqual(alarm.level(available), expected)


    def test_tick_alert_pause_dedupe_and_no_resume(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pause, notify = Mock(return_value='paused'), Mock(return_value={'message_id': '123'})
            alarm.tick(root, 60 * 2**30, pause, notify)
            pause.assert_not_called(); notify.assert_not_called()
            alarm.tick(root, 60 * 2**30 - 1, pause, notify)
            alarm.tick(root, 40 * 2**30, pause, notify)
            self.assertEqual(notify.call_count, 1)
            pause.assert_not_called()
            alarm.tick(root, 40 * 2**30 - 1, pause, notify)
            self.assertTrue((root / 'backup-paused').exists())
            pause.assert_called_once()
            self.assertEqual(notify.call_count, 2)
            alarm.tick(root, 70 * 2**30, pause, notify)
            self.assertTrue((root / 'backup-paused').exists())
            self.assertEqual(pause.call_count, 1)  # no resume function exists
            self.assertEqual(notify.call_count, 2)

    def test_ambiguous_send_stays_latched_after_recovery(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notify = Mock(side_effect=TimeoutError('ambiguous'))
            pause = Mock(return_value='paused')
            alarm.tick(root, 50 * 2**30, pause, notify)
            alarm.tick(root, 70 * 2**30, pause, notify)
            alarm.tick(root, 30 * 2**30, pause, notify)
            self.assertEqual(notify.call_count, 1)
            pause.assert_called_once()
            self.assertIsNotNone(json.loads((root / 'state.json').read_text())['pending'])

    def test_pause_failure_still_alerts_and_blocks_backup(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notify = Mock(return_value={'message_id': '123'})
            result = alarm.tick(root, 30 * 2**30, Mock(side_effect=RuntimeError('native refused')), notify)
            self.assertIn('pause_error', result)
            notify.assert_called_once()
            self.assertTrue((root / 'backup-paused').exists())

    def test_backup_gate_and_existing_script_only(self):
        alarm = load()
        launch = Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(RuntimeError):
                alarm.backup(root, 70 * 2**30, launch)
            alarm.tick(root, 70 * 2**30, Mock(), Mock())
            alarm.backup(root, 40 * 2**30, launch)
            launch.assert_called_once_with(['/bin/bash', str(alarm.BUSINESS / 'scripts/hermes-state-backup.sh')])
            launch.reset_mock()
            alarm.backup(root, 40 * 2**30 - 1, launch)
            alarm.backup(root, 70 * 2**30, launch)
            launch.assert_not_called()

    def test_singleton_lock_and_intent_before_send(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with alarm.lock(root):
                with self.assertRaises(BlockingIOError):
                    with alarm.lock(root):
                        self.fail('second owner admitted')
                def crash(message):
                    self.assertEqual(json.loads((root / 'state.json').read_text())['pending']['message'], message)
                    raise SystemExit('simulated process death')
                with self.assertRaises(SystemExit):
                    alarm.tick(root, 50 * 2**30, Mock(), crash)
            send = Mock()
            with alarm.lock(root):
                alarm.tick(root, 50 * 2**30, Mock(), send)
            send.assert_not_called()

    def test_sender_exact_route_and_one_attempt(self):
        alarm = load()
        with patch.object(alarm.subprocess, 'run', return_value=Mock(returncode=0, stdout='{"exact_readback":true,"message_id":"123"}')) as run:
            alarm.notify('message')
            args = run.call_args.args[0]
            self.assertEqual(args[1:], [str(alarm.BUSINESS / 'scripts/post_discord_message.py'), '--channel', alarm.CHANNEL,
                                      '--ping-user', alarm.OPERATOR, '--message', 'message'])
        with patch.object(alarm.subprocess, 'run', side_effect=TimeoutError()) as run:
            with self.assertRaises(TimeoutError):
                alarm.notify('message')
            self.assertEqual(run.call_count, 1)

    def test_cli_help_is_read_only(self):
        import subprocess
        result = subprocess.run(['python3', '-B', str(SOURCE), '--help'], capture_output=True, text=True, check=True)
        self.assertIn('{check,tick,backup}', result.stdout)

    def test_real_df_read_only(self):
        alarm = load()
        alarm.BUSINESS = SOURCE.parent
        self.assertGreaterEqual(alarm.free_bytes(), 0)

    def test_native_exact_target_preserves_unrelated_and_active_claim(self):
        alarm = load()
        import copy
        target = {'id': alarm.JOB_ID, 'script': alarm.JOB_SCRIPT, 'enabled': True,
                  'state': 'scheduled', 'fire_claim': {'execution_id': 'active'}, 'next_run_at': 'later'}
        unrelated = {'id': 'other', 'name': 'ahs-untouched', 'enabled': True}
        before = copy.deepcopy(unrelated)

        class Jobs:
            @contextmanager
            def _jobs_lock(self, *, require_cross_process):
                assert require_cross_process
                yield
            def get_job(self, job_id):
                assert job_id == alarm.JOB_ID
                return copy.deepcopy(target)
            def pause_job(self, job_id, reason):
                assert job_id == alarm.JOB_ID and reason
                target.update(enabled=False, state='paused', paused_reason=reason)
                return copy.deepcopy(target)
        jobs = Jobs()
        alarm.pause_registry(jobs)
        self.assertEqual(unrelated, before)
        self.assertEqual(target['fire_claim'], {'execution_id': 'active'})
        self.assertEqual(target['next_run_at'], 'later')
        self.assertFalse(target['enabled'])
        target['script'] = 'unrelated.sh'
        with self.assertRaises(RuntimeError):
            alarm.pause_registry(jobs)


if __name__ == '__main__':
    unittest.main()
