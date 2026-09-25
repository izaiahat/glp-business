"""Offline controller tests. No production DB, posts or process changes."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import archive
import weekly
spec = importlib.util.spec_from_file_location('archive_fixture', HERE / 'test_archive.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'fixture.db'
        self.db.write_bytes(b'fixture')
        self.config = self.root / 'config.yaml'
        self.config.write_text('sessions:\n  vacuum_after_prune: false\n')
        self.calls = []

    def executor(self, argv, log):
        self.calls.append(argv)
        return 0

    def run_chain(self, executor=None, **kwargs):
        return weekly.run_chain(self.root / 'runs', self.db, self.config,
                                executor=executor or self.executor, **kwargs)

    def test_refusal_never_plans_or_writes(self):
        def refused(argv, log):
            self.calls.append(argv)
            return 1
        r = self.run_chain(refused)
        self.assertEqual(r['status'], 'REFUSED_PREFLIGHT')
        self.assertFalse(r['effects_started'])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('--i-am-in-the-quiet-window', self.calls[0])
        self.assertFalse((self.root / 'runs/ACTIVE.json').exists())

    def test_check_only_never_plans_or_effects(self):
        r = self.run_chain(check_only=True)
        self.assertEqual(r['status'], 'CHECK_ONLY_PASS_NO_EFFECTS')
        self.assertEqual(len(self.calls), 1)

    def test_verify_failure_prevents_delete_and_replay(self):
        def bad(argv, log):
            self.calls.append(argv)
            return 1 if argv[-1] == 'verify' else 0
        r = self.run_chain(bad)
        self.assertEqual(r['status'], 'BLOCKED_VERIFY')
        self.assertFalse(any('delete' in a for a in self.calls))
        self.assertTrue((self.root / 'runs/ACTIVE.json').exists())
        count = len(self.calls)
        self.assertEqual(self.run_chain()['status'], 'BLOCKED_AMBIGUOUS_PRIOR_RUN')
        self.assertEqual(count, len(self.calls))

    def test_writer_reappears_prevents_next_effect(self):
        def bad(argv, log):
            self.calls.append(argv)
            return int(argv[-1] == 'preflight' and len(self.calls) > 5)
        r = self.run_chain(bad)
        self.assertEqual(r['status'], 'BLOCKED_WRITER_REAPPEARED')
        self.assertFalse(any('delete' in a for a in self.calls))

    def test_pins_fail_closed(self):
        pin = self.root / 'pins.json'
        pin.write_text(json.dumps({'files': {str(self.db): '0' * 64}}))
        with self.assertRaisesRegex(RuntimeError, 'pin_drift'):
            weekly.check_pins(pin)

    def test_executor_exception_keeps_intent(self):
        def bad(argv, log):
            if argv[-1] == 'copy':
                raise OSError('fixture only')
            return 0
        r = self.run_chain(bad)
        self.assertEqual(r['status'], 'BLOCKED_CONTROLLER_EXCEPTION')
        self.assertTrue((self.root / 'runs/ACTIVE.json').exists())

    def test_receipt_lints_as_one_line(self):
        sys.path.insert(0, '/home/ubuntu/business/ops')
        import discord_fmt
        for status in ['ARCHIVE_VERIFIED', 'REFUSED_PREFLIGHT', 'BLOCKED_VERIFY']:
            text = weekly.format_receipt({'status': status, 'run_id': 'fixture',
                                         'before_bytes': 100, 'after_bytes': 50})
            self.assertEqual(len(text.splitlines()), 1)
            self.assertEqual(discord_fmt.lint(text, 'report')['status'], 'pass')

    def test_notification_intent_not_retried(self):
        outbox = self.root / 'delivery/fixture.json'
        weekly.save(outbox, {'status': 'DISPATCH_INTENT'})
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('no network')):
            self.assertEqual(weekly.notify({'run_id': 'fixture'}, self.root)['status'], 'DISPATCH_INTENT')

    def test_real_fixture_chain(self):
        self.db.unlink()
        manifest = fixture.build_fixture(str(self.db), time.time(), include_live_lease=False)
        conn = sqlite3.connect(self.db)
        conn.execute('update compression_locks set expires_at=?', (time.time() - 60,))
        conn.commit()
        conn.close()
        def real(argv, log):
            with log.open('w') as stream, contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                return archive.main(argv[2:])
        # Only process detection is replaced: the isolated fixture is not the live DB.
        with mock.patch.object(archive, 'hermes_procs', return_value=[]):
            r = self.run_chain(real)
        self.assertEqual(r['status'], 'ARCHIVE_VERIFIED', r)
        self.assertTrue(r['effects_started'])
        self.assertFalse((self.root / 'runs/ACTIVE.json').exists())
        with sqlite3.connect(self.db) as conn:
            ids = {r[0] for r in conn.execute('select id from sessions')}
            self.assertEqual(ids, manifest['kept'])
            self.assertEqual(conn.execute('pragma integrity_check').fetchone()[0], 'ok')
        steps = [r['command'] for r in r['steps']]
        self.assertLess(steps.index('verify'), steps.index('delete'))
        self.assertEqual(steps.count('copy'), 1)
        self.assertEqual(steps.count('vacuum'), 1)

    def test_actual_launcher_admits_only_detached_unit(self):
        path = Path('/home/ubuntu/.hermes/scripts/hermes_state_archive_weekly.py')
        spec = importlib.util.spec_from_file_location('weekly_launcher', path)
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        output = io.StringIO()
        with mock.patch.object(launcher.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
            with contextlib.redirect_stdout(output):
                self.assertEqual(launcher.main(), 0)
        self.assertEqual(output.getvalue().strip(), '[SILENT]')
        self.assertEqual(run.call_args.args[0], ['systemctl', '--user', 'start', '--no-block',
                                               'hermes-state-archive-weekly.service'])

    def test_notification_checks_exact_channel_and_content(self):
        webhook = self.root / 'webhook.txt'
        webhook.write_text('https://discord.com/api/webhooks/fixture/fixture')
        webhook.chmod(0o600)
        result = {'status': 'REFUSED_PREFLIGHT', 'run_id': 'notify-fixture'}
        message = weekly.format_receipt(result)
        replies = [
            {'channel_id': weekly.CHANNEL},
            {'id': 'fixture-message'},
            {'id': 'fixture-message', 'channel_id': weekly.CHANNEL, 'content': message},
        ]
        class Response(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
        calls = []
        def fake(req, **kwargs):
            calls.append(req)
            return Response(json.dumps(replies.pop(0)).encode())
        with mock.patch.object(weekly, 'WEBHOOK_FILE', webhook), mock.patch('urllib.request.urlopen', side_effect=fake):
            receipt = weekly.notify(result, self.root)
        self.assertEqual(receipt['status'], 'READBACK_VERIFIED')
        self.assertEqual([r.get_method() for r in calls], ['GET', 'POST', 'GET'])
        self.assertEqual(json.loads(calls[1].data)['allowed_mentions'], {'parse': []})

    def test_wrong_notification_channel_never_posts(self):
        webhook = self.root / 'webhook.txt'
        webhook.write_text('https://discord.com/api/webhooks/fixture/fixture')
        webhook.chmod(0o600)
        with mock.patch.object(weekly, 'WEBHOOK_FILE', webhook), mock.patch('urllib.request.urlopen') as request:
            request.return_value.__enter__.return_value = io.BytesIO(b'{"channel_id":"wrong"}')
            receipt = weekly.notify({'status': 'REFUSED_PREFLIGHT', 'run_id': 'wrong-channel'}, self.root)
        self.assertEqual(receipt['status'], 'QUEUED_NO_POST')
        self.assertEqual(request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
