"""Offline tests: no production scheduler, backup, or Discord calls."""
import importlib.util
from pathlib import Path
import unittest
import tempfile
import json
import os
import subprocess
import contextlib
import io
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/disk_alarm.py'


def load():
    spec = importlib.util.spec_from_file_location('disk_alarm_test', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DiskAlarmTests(unittest.TestCase):
    def test_legacy_state_migrates_and_lifts_alarm_pause(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'state.json').write_text('{"sent":[],"pending":null}')
            with self.assertRaises(RuntimeError):
                alarm.admitted(root, 70 * 2**30)  # Legacy state alone is not admission.
            alarm.atomic_json(root / 'backup-paused', {'owner': 'disk-alarm'})
            notify = Mock(return_value={'message_id': 'unexpected'})
            result = alarm.tick(root, 70 * 2**30, notify)
            self.assertEqual(result['level'], 'healthy')
            self.assertEqual(result['backup_recovery'], 'alarm hold lifted')
            state = alarm.read_state(root / 'state.json')
            self.assertEqual(state['pending'], {})
            self.assertEqual(state['latest']['available_bytes'], 70 * 2**30)
            self.assertFalse((root / 'backup-paused').exists())
            self.assertTrue(alarm.admitted(root, 70 * 2**30))
            notify.assert_not_called()

    def test_corruption_alert_rearms_after_healthy_repair(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notify = Mock(return_value={'message_id': 'error-alert'})
            for episode in range(2):
                (root / 'state.json').write_text('{')
                self.assertEqual(alarm.tick(root, 70 * 2**30, notify)['level'], 'error')
                self.assertEqual(notify.call_count, episode + 1)
                (root / 'state.json').unlink()  # explicit scratch fixture repair
                alarm.tick(root, 70 * 2**30, notify)
            self.assertEqual(alarm.read_state(root / 'monitoring-error.json')['sent'], [])
            notify.side_effect = TimeoutError('uncertain third alert')
            (root / 'state.json').write_text('{')
            alarm.tick(root, 70 * 2**30, notify)
            pending = alarm.read_state(root / 'monitoring-error.json')['pending']
            (root / 'state.json').unlink()
            alarm.tick(root, 70 * 2**30, notify)
            self.assertEqual(alarm.read_state(root / 'monitoring-error.json')['pending'], pending)
            (root / 'state.json').write_text('{')
            alarm.tick(root, 70 * 2**30, notify)
            self.assertEqual(notify.call_count, 3)  # Recovery never replays uncertain delivery.

    def test_wrapper_child_runs_while_parent_admission_lock_is_held(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            scripts = base / 'scripts'
            scripts.mkdir()
            script = scripts / 'hermes-state-backup.sh'
            script.write_bytes((SOURCE.parent / script.name).read_bytes())
            source = SOURCE.read_text().replace("BUSINESS = Path('/home/ubuntu/business')", f'BUSINESS = Path({str(base)!r})')
            source = source.replace("if __name__ == '__main__':", "def free_bytes(): return 70 * 2**30\n\nif __name__ == '__main__':")
            (scripts / SOURCE.name).write_text(source)
            home, output = base / 'home', base / 'backups'
            home.mkdir()
            (home / 'SOUL.md').write_text('synthetic fixture')
            root = base / 'ops/runtime/disk-alarm'
            root.mkdir(parents=True)
            alarm.tick(root, 70 * 2**30, Mock())
            # Wait inside launch: force the child to reach its gate before parent unlock.
            def launch(*args, **kwargs):
                child = subprocess.Popen(*args, **kwargs, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                stdout, stderr = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, stdout + stderr)
                self.assertIn('hermes-state-backup done', stdout)
                return child
            with patch.object(alarm, 'BUSINESS', base), patch.dict(os.environ, {
                    'HERMES_HOME': str(home), 'HERMES_BACKUP_ROOT': str(output)}):
                with alarm.lock(root):
                    child = alarm.backup(root, 70 * 2**30, launch)
                    self.assertEqual(child.wait(), 0)
                    # An unadmitted direct child must refuse contention, not report success.
                    refused = subprocess.run(['/bin/bash', str(script)], capture_output=True, text=True, timeout=20)
                    self.assertNotEqual(refused.returncode, 0, refused.stdout)
            self.assertEqual(len(list(output.glob('hermes-state-????????.tar.gz'))), 1)
            with patch.object(alarm, 'STATE', root), patch('sys.argv', ['disk_alarm.py', 'backup']), \
                 contextlib.redirect_stdout(io.StringIO()), alarm.lock(root):
                self.assertEqual(alarm.main(), 3)
            (root / 'operator-backup-paused').touch()
            with patch.object(alarm, 'STATE', root), patch.object(alarm, 'free_bytes', return_value=70 * 2**30), \
                 patch('sys.argv', ['disk_alarm.py', 'backup']), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(alarm.main(), 3)

    def test_admission_remeasures_after_state_and_pause_write_failure(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alarm.tick(root, 70 * 2**30, Mock())
            healthy = (root / 'state.json').read_bytes()
            with patch.object(alarm, 'STATE', root), patch.object(alarm, 'LOG', root / 'log'), \
                 patch.object(alarm, 'free_bytes', return_value=30 * 2**30), \
                 patch.object(alarm, 'atomic_json', side_effect=OSError('state and pause unwritable')), \
                 patch('sys.argv', ['disk_alarm.py', 'tick']), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(alarm.main(), 1)
            self.assertEqual((root / 'state.json').read_bytes(), healthy)
            self.assertFalse((root / 'backup-paused').exists())
            # Even a pause writer that cannot persist a marker must never admit low space.
            with patch.object(alarm, 'backup_pause'):
                self.assertFalse(alarm.admitted(root, 30 * 2**30))
            for measurement in (30 * 2**30, OSError('df unavailable')):
                with self.subTest(measurement=measurement), patch.object(alarm, 'STATE', root), \
                     patch.object(alarm, 'free_bytes', **({'side_effect': measurement} if isinstance(measurement, Exception) else {'return_value': measurement})), \
                     patch.object(alarm, 'atomic_json', side_effect=OSError('still unwritable')), \
                     patch.object(alarm.subprocess, 'Popen') as launch, \
                     patch('sys.argv', ['disk_alarm.py', 'backup']), contextlib.redirect_stderr(io.StringIO()):
                    self.assertNotEqual(alarm.main(), 0)
                    launch.assert_not_called()

    def test_ambient_flag_cannot_bypass_low_space_and_operator_pause(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            scripts = base / 'scripts'
            scripts.mkdir()
            script = scripts / 'hermes-state-backup.sh'
            script.write_bytes((SOURCE.parent / script.name).read_bytes())
            source = SOURCE.read_text().replace("BUSINESS = Path('/home/ubuntu/business')", f'BUSINESS = Path({str(base)!r})')
            source = source.replace("if __name__ == '__main__':", "def free_bytes(): return 30 * 2**30\n\nif __name__ == '__main__':")
            (scripts / SOURCE.name).write_text(source)
            root = base / 'ops/runtime/disk-alarm'
            root.mkdir(parents=True)
            alarm.tick(root, 70 * 2**30, Mock())
            (root / 'operator-backup-paused').touch()
            home, output = base / 'home', base / 'backups'
            home.mkdir()
            (home / 'SOUL.md').write_text('synthetic fixture')
            env = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_BACKUP_ROOT': str(output),
                   'DISK_ALARM_BACKUP_ADMITTED': '1'}
            env.pop('DISK_ALARM_BACKUP_TOKEN', None)
            result = subprocess.run(['/bin/bash', str(script)], env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(output.exists())

    def test_warning_admission_refuses_failed_writes_after_failed_critical_tick(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(alarm.time, 'time', return_value=10000):
                alarm.tick(root, 70 * 2**30, Mock())
            healthy = (root / 'state.json').read_bytes()
            with patch.object(alarm.time, 'time', return_value=10900), \
                 patch.object(alarm, 'atomic_json', side_effect=OSError('all writes fail')) as persist:
                with self.assertRaises(OSError):
                    alarm.tick(root, 30 * 2**30, Mock())
                persist.reset_mock()
                self.assertFalse(alarm.admitted(root, 50 * 2**30))
                self.assertGreater(persist.call_count, 0)
            self.assertEqual((root / 'state.json').read_bytes(), healthy)
            self.assertFalse((root / 'backup-paused').exists())

    def test_critical_pause_survives_observation_write_failure(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(alarm.time, 'time', return_value=10000):
                alarm.tick(root, 70 * 2**30, Mock())
            persist = alarm.atomic_json
            def fail_observation(path, value):
                if path.name == 'state.json':
                    raise OSError('observation write failed')
                return persist(path, value)
            with patch.object(alarm.time, 'time', return_value=10900), \
                 patch.object(alarm, 'atomic_json', side_effect=fail_observation):
                self.assertFalse(alarm.admitted(root, 30 * 2**30))
            with patch.object(alarm.time, 'time', return_value=10901):
                self.assertFalse(alarm.admitted(root, 50 * 2**30))
            self.assertEqual(json.loads((root / 'backup-paused').read_text())['owner'], 'disk-alarm')
            with patch.object(alarm.time, 'time', return_value=10902):
                alarm.tick(root, 60 * 2**30, Mock())
                self.assertTrue(alarm.admitted(root, 50 * 2**30))

    def test_warning_admission_persists_without_refreshing_timer_freshness(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(alarm.time, 'time', return_value=10000):
                alarm.tick(root, 70 * 2**30, Mock())
            with patch.object(alarm.time, 'time', return_value=10900):
                self.assertTrue(alarm.admitted(root, 50 * 2**30))
            state = alarm.read_state(root / 'state.json')
            self.assertEqual(state['backup_observation'],
                             {'at': 10900, 'available_bytes': 50 * 2**30, 'level': 'warning'})
            self.assertEqual(state['latest']['at'], 10000)
            self.assertFalse((root / 'backup-paused').exists())
            with patch.object(alarm.time, 'time', return_value=11801):
                with self.assertRaises(RuntimeError):
                    alarm.admitted(root, 50 * 2**30)

    def test_admission_token_is_private_single_use_and_expires(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launch = Mock()
            with patch.object(alarm.time, 'time', return_value=10000):
                alarm.tick(root, 70 * 2**30, Mock())
                alarm.backup(root, 70 * 2**30, launch)
                token = launch.call_args.kwargs['env']['DISK_ALARM_BACKUP_TOKEN']
                self.assertEqual((root / 'admissions').stat().st_mode & 0o777, 0o700)
                self.assertFalse(alarm.consume_admission(root, '1'))
                self.assertTrue(alarm.consume_admission(root, token))
                self.assertFalse(alarm.consume_admission(root, token))
                alarm.backup(root, 70 * 2**30, launch)
                expired = launch.call_args.kwargs['env']['DISK_ALARM_BACKUP_TOKEN']
            with patch.object(alarm.time, 'time', return_value=10060):
                self.assertFalse(alarm.consume_admission(root, expired))

    def test_exact_thresholds(self):
        alarm = load()
        for available, expected in [(60 * 2**30, 'healthy'), (60 * 2**30 - 1, 'warning'),
                                    (40 * 2**30, 'warning'), (40 * 2**30 - 1, 'critical')]:
            with self.subTest(available=available):
                self.assertEqual(alarm.level(available), expected)

    def test_tick_logs_alerts_and_recovers_only_alarm_owned_pause(self):
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
            self.assertFalse((root / 'backup-paused').exists())
            self.assertEqual(rows[-1]['backup_recovery'], 'alarm hold lifted')
            self.assertEqual({p.name for p in root.iterdir()}, {'state.json', log.name})
            self.assertNotIn('hermes', SOURCE.read_text().replace('hermes-state-backup.sh', ''))
            (root / 'operator-backup-paused').write_text('operator hold')
            alarm.tick(root, 30 * 2**30, notify, log)
            alarm.tick(root, 60 * 2**30, notify, log)
            self.assertFalse((root / 'backup-paused').exists())
            self.assertEqual((root / 'operator-backup-paused').read_text(), 'operator hold')
            for manual in ('manual', '{}', '{"owner":"operator"}', '{"at":1,"available_bytes":2}'):
                (root / 'backup-paused').write_text(manual)
                alarm.tick(root, 70 * 2**30, notify, log)
                self.assertEqual((root / 'backup-paused').read_text(), manual)
                self.assertFalse(alarm.admitted(root, 70 * 2**30))

    def test_ambiguous_send_stays_latched_after_recovery(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notify = Mock(side_effect=TimeoutError('ambiguous'))
            for available in (50 * 2**30, 70 * 2**30, 30 * 2**30):
                alarm.tick(root, available, notify)
            self.assertEqual(notify.call_count, 2)  # critical is a distinct effect, never a warning retry
            pending = json.loads((root / 'state.json').read_text())['pending']
            self.assertEqual(set(pending), {'warning', 'critical'})
            alarm.tick(root, 30 * 2**30, notify)
            self.assertEqual(notify.call_count, 2)
            with self.assertRaises(ValueError):
                alarm.reconcile(root, 'state.json', 'critical', 'wrong-id', 'not-sent', 'readback')
            with self.assertRaises(ValueError):
                alarm.reconcile(root, 'state.json', 'critical', pending['critical']['id'], 'delivered', 'readback')
            alarm.reconcile(root, 'state.json', 'warning', pending['warning']['id'],
                            'delivered', 'exact channel readback URL', '123')
            alarm.reconcile(root, 'state.json', 'critical', pending['critical']['id'],
                            'not-sent', 'operator verified explicit rejection; no message')
            self.assertEqual(notify.call_count, 2)  # reconciliation itself cannot send
            notify.side_effect = None
            notify.return_value = {'message_id': '456'}
            alarm.tick(root, 30 * 2**30, notify)
            alarm.tick(root, 30 * 2**30, notify)
            self.assertEqual(notify.call_count, 3)
            state = alarm.read_state(root / 'state.json')
            self.assertEqual(state['pending'], {})
            self.assertEqual(len(state['reconciled']), 2)
            self.assertTrue((root / 'backup-paused').exists())

    def test_backup_gate_and_existing_script_only(self):
        alarm = load()
        launch = Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                alarm.backup(root, 70 * 2**30, launch)
            alarm.tick(root, 70 * 2**30, Mock())
            alarm.backup(root, 40 * 2**30, launch)
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[0],
                             ['/bin/bash', str(alarm.BUSINESS / 'scripts/hermes-state-backup.sh')])
            token = launch.call_args.kwargs['env']['DISK_ALARM_BACKUP_TOKEN']
            self.assertTrue(alarm.consume_admission(root, token))
            launch.reset_mock()
            alarm.backup(root, 40 * 2**30 - 1, launch)
            alarm.backup(root, 70 * 2**30, launch)
            launch.assert_not_called()
            alarm.tick(root, 70 * 2**30, Mock())
            alarm.backup(root, 70 * 2**30, launch)
            self.assertEqual(launch.call_count, 1)

    def test_singleton_lock_and_intent_before_send(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with alarm.lock(root):
                with self.assertRaises(BlockingIOError):
                    with alarm.lock(root):
                        self.fail('second owner admitted')
                def crash(message):
                    self.assertEqual(json.loads((root / 'state.json').read_text())['pending']['warning']['message'], message)
                    raise SystemExit('simulated process death')
                with self.assertRaises(SystemExit):
                    alarm.tick(root, 50 * 2**30, crash)
            send = Mock()
            with alarm.lock(root):
                alarm.tick(root, 50 * 2**30, send)
            send.assert_not_called()

    def test_failed_measurements_log_alert_pause_and_reject_bad_df(self):
        alarm = load()
        for output in ('', 'Avail\n-1\n', 'arbitrary\n123\n', 'Avail\n999999999999999999999999\n'):
            with self.subTest(output=output), patch.object(alarm.subprocess, 'run', return_value=Mock(stdout=output)):
                with self.assertRaises(RuntimeError):
                    alarm.free_bytes()
        for error in (subprocess.TimeoutExpired('df', 10), subprocess.CalledProcessError(1, 'df'),
                      RuntimeError('malformed'), OSError('df unavailable')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                notify = Mock(return_value={'message_id': 'error-alert'})
                real_tick = alarm.tick
                with patch.object(alarm, 'STATE', root), patch.object(alarm, 'LOG', root / 'log'), \
                     patch.object(alarm, 'free_bytes', side_effect=error), \
                     patch.object(alarm, 'tick', side_effect=lambda *a, **kw: real_tick(*a, send=notify, **kw)), \
                     patch('sys.argv', ['disk_alarm.py', 'tick']), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(alarm.main(), 1)
                row = json.loads((root / 'log').read_text())
                self.assertEqual(row['level'], 'error')
                self.assertIsNone(row['available_bytes'])
                self.assertEqual(row['measurement_error'], type(error).__name__)
                self.assertEqual(notify.call_count, 1)
                self.assertTrue((root / 'backup-paused').exists())
                with self.assertRaises(RuntimeError):
                    alarm.backup(root, 50 * 2**30, Mock(side_effect=AssertionError('launched')))
                alarm.tick(root, 70 * 2**30, notify)
                self.assertTrue(alarm.admitted(root, 70 * 2**30))

    def test_corrupt_state_observed_and_admission_refused(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notify = Mock(return_value={'message_id': 'error-alert'})
            launch = Mock()
            for broken in ('{', '[]', '{}', '{"sent": [], "pending": {}, "latest": []}'):
                (root / 'state.json').write_text(broken)
                with self.assertRaises((ValueError, KeyError)):
                    alarm.backup(root, 50 * 2**30, launch)
                result = alarm.tick(root, 50 * 2**30, notify)
                self.assertEqual(result['level'], 'error')
                self.assertIn('state_error', json.loads((root / 'disk-alerts.log').read_text().splitlines()[-1]))
                self.assertEqual((root / 'state.json').read_text(), broken)
                self.assertTrue((root / 'backup-paused').exists())
            launch.assert_not_called()
            self.assertEqual(notify.call_count, 1)
            (root / 'state.json').unlink()  # explicit fixture repair, not production recovery
            alarm.tick(root, 70 * 2**30, notify)
            state = alarm.read_state(root / 'state.json')
            state['latest']['at'] -= 1801
            alarm.atomic_json(root / 'state.json', state)
            with self.assertRaises(RuntimeError):
                alarm.backup(root, 50 * 2**30, launch)

    def test_direct_nightly_script_gate_before_all_backup_effects(self):
        alarm = load()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            scripts = base / 'scripts'
            scripts.mkdir()
            script = scripts / 'hermes-state-backup.sh'
            script.write_bytes((SOURCE.parent / script.name).read_bytes())
            # Exact gate/producer; only home and measurement have fixture bindings.
            source = SOURCE.read_text().replace("BUSINESS = Path('/home/ubuntu/business')", f'BUSINESS = Path({str(base)!r})')
            source = source.replace("if __name__ == '__main__':", "def free_bytes(): return 70 * 2**30\n\nif __name__ == '__main__':")
            (scripts / SOURCE.name).write_text(source)
            home, output = base / 'home', base / 'backups'
            home.mkdir()
            (home / 'SOUL.md').write_text('synthetic fixture, no credentials')
            env = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_BACKUP_ROOT': str(output)}
            def run():
                return subprocess.run(['/bin/bash', str(script)], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(run().returncode, 1)
            self.assertFalse(output.exists())
            root = base / 'ops/runtime/disk-alarm'
            notify = Mock(return_value={'message_id': 'fixture'})
            alarm.tick(root, 30 * 2**30, notify)
            paused = run()  # legacy cron entry, no disk_alarm.py wrapper
            self.assertEqual(paused.returncode, 3, paused.stderr)
            self.assertIn('SKIP backup schedule paused', paused.stdout)
            self.assertFalse(output.exists())
            alarm.tick(root, 70 * 2**30, notify)
            (root / 'operator-backup-paused').touch()
            self.assertIn('SKIP', run().stdout)
            self.assertFalse(output.exists())
            (root / 'operator-backup-paused').unlink()
            completed = run()
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn('hermes-state-backup done', completed.stdout)
            self.assertEqual(len(list(output.glob('hermes-state-????????.tar.gz'))), 1)

    def test_failed_filesystem_writes_never_send_or_admit(self):
        alarm = load()
        for failing in ('atomic_json', 'append_log'):
            with self.subTest(failing=failing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                notify = Mock()
                alarm.tick(root, 70 * 2**30, notify)
                notify.reset_mock()
                real_tick = alarm.tick
                errors = io.StringIO()
                with patch.object(alarm, 'STATE', root), patch.object(alarm, 'LOG', root / 'log'), \
                     patch.object(alarm, 'free_bytes', return_value=30 * 2**30), \
                     patch.object(alarm, 'tick', side_effect=lambda *a, **kw: real_tick(*a, send=notify, **kw)), \
                     patch.object(alarm, failing, side_effect=OSError('write refused')), \
                     patch('sys.argv', ['disk_alarm.py', 'tick']), contextlib.redirect_stderr(errors):
                    self.assertEqual(alarm.main(), 1)
                    launch = Mock(side_effect=AssertionError('launched'))
                    self.assertIsNone(alarm.backup(root, 30 * 2**30, launch))
                self.assertEqual(json.loads(errors.getvalue())['monitoring_error'], 'OSError')
                notify.assert_not_called()

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
