"""Offline tests: no production scheduler, backup, or Discord calls."""
import importlib.util
from pathlib import Path
import unittest
import tempfile
import json
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/disk_alarm.py'


def load():
    spec = importlib.util.spec_from_file_location('disk_alarm_test', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DiskAlarmTests(unittest.TestCase):
    def test_exact_thresholds(self):
        alarm = load()
        for available, expected in [(60 * 2**30, 'healthy'), (60 * 2**30 - 1, 'warning'),
                                    (40 * 2**30, 'warning'), (40 * 2**30 - 1, 'critical')]:
            with self.subTest(available=available):
                self.assertEqual(alarm.level(available), expected)

    def test_tick_logs_alerts_and_pauses_only_backup_without_resume(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'mailbox-alerts.log'
            notify = Mock(return_value={'message_id': '123'})
            with patch.object(alarm.subprocess, 'run', side_effect=AssertionError('no scheduler calls')):
                for available in (60 * 2**30, 60 * 2**30 - 1, 40 * 2**30,
                                  40 * 2**30 - 1, 70 * 2**30):
                    alarm.tick(root, available, notify, log)
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([row['level'] for row in rows], ['healthy', 'warning', 'warning', 'critical', 'healthy'])
            self.assertEqual(notify.call_count, 2)
            self.assertTrue((root / 'backup-paused').exists())
            self.assertEqual({p.name for p in root.iterdir()}, {'state.json', 'backup-paused', log.name})
            self.assertNotIn('hermes', SOURCE.read_text().replace('hermes-state-backup.sh', ''))

    def test_ambiguous_send_stays_latched_after_recovery(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notify = Mock(side_effect=TimeoutError('ambiguous'))
            for available in (50 * 2**30, 70 * 2**30, 30 * 2**30):
                alarm.tick(root, available, notify)
            self.assertEqual(notify.call_count, 1)
            self.assertIsNotNone(json.loads((root / 'state.json').read_text())['pending'])
            self.assertTrue((root / 'backup-paused').exists())

    def test_backup_gate_and_existing_script_only(self):
        alarm = load()
        launch = Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(RuntimeError):
                alarm.backup(root, 70 * 2**30, launch)
            alarm.tick(root, 70 * 2**30, Mock())
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
                    alarm.tick(root, 50 * 2**30, crash)
            send = Mock()
            with alarm.lock(root):
                alarm.tick(root, 50 * 2**30, send)
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

    def test_timer_and_root_filesystem_read_only(self):
        alarm = load()
        base = SOURCE.parents[1]
        timer = (base / 'ops/systemd/kael-disk-alarm.timer').read_text()
        service = (base / 'ops/systemd/kael-disk-alarm.service').read_text()
        self.assertIn('OnCalendar=*-*-* *:00/15:00', timer)
        self.assertIn('disk_alarm.py tick', service)
        self.assertNotIn('HERMES_HOME', service)
        with patch.object(alarm.subprocess, 'run', return_value=Mock(stdout='Avail\n123\n')) as run:
            self.assertEqual(alarm.free_bytes(), 123)
            self.assertEqual(run.call_args.args[0], ['/usr/bin/df', '-B1', '--output=avail', '/'])
        self.assertGreaterEqual(load().free_bytes(), 0)
        import subprocess
        result = subprocess.run(['python3', '-B', str(SOURCE), 'check'], capture_output=True, text=True, check=True)
        self.assertFalse(json.loads(result.stdout)['effects'])


if __name__ == '__main__':
    unittest.main()
