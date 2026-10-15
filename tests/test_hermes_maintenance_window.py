"""Offline maintenance contract; no installed Hermes imports or live effects."""
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from contextlib import ExitStack, redirect_stdout
import io
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('maintenance', ROOT / 'scripts/hermes_maintenance_window.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_capacity_exact_boundary(self):
        self.assertEqual(m.required_space(3), 5 + 2 * 1024**3)
        self.assertTrue(m.has_space(400, m.required_space(400)))
        self.assertFalse(m.has_space(400, m.required_space(400)-1))

    def test_delegate_logs_recent_and_stale(self):
        live = self.root / 'live'
        (live / 'child').mkdir(parents=True)
        log = live / 'child' / 'worker.log'
        log.write_text('running')
        now = time.time()
        self.assertEqual(m.recent_delegates(live, now), [str(log)])
        os.utime(log, (now-301, now-301))
        self.assertEqual(m.recent_delegates(live, now), [])

    def test_missing_delegate_directory_refuses(self):
        with self.assertRaises(m.Refusal):
            m.recent_delegates(self.root / 'absent', time.time())

    def test_backup_vacuum_integrity_journal_and_data(self):
        db = self.root / 'state.db'
        with closing(sqlite3.connect(db)) as c:
            c.execute('pragma journal_mode=wal')
            c.execute('create table t (id integer primary key, body blob)')
            c.executemany('insert into t(body) values (?)', [(b'x'*3000,)]*150)
            c.execute('delete from t where id > 5')
            c.commit()
        before = db.stat().st_size
        backup = m.verified_backup(db, self.root / 'backup')
        self.assertEqual(len(backup['sha256']), 64)
        self.assertEqual(backup['integrity_check'], 'ok')
        self.assertEqual(Path(backup['path']).stat().st_mode & 0o777, 0o600)
        result = m.vacuum(db)
        self.assertEqual(result['journal_before'], 'wal')
        self.assertEqual(result['journal_after'], 'wal')
        self.assertEqual(result['integrity_check'], 'ok')
        self.assertLess(db.stat().st_size, before)
        with closing(sqlite3.connect(backup['path'])) as c:
            self.assertEqual(c.execute('select count(*) from t').fetchone()[0], 5)
        with self.assertRaises(FileExistsError):
            m.verified_backup(db, self.root / 'backup')

    def test_dry_run_no_effect_functions_or_db_connection(self):
        fake = {'checks': [], 'blockers': ['active_delegates'], 'plan': m.PLAN}
        with patch.object(m, 'preflight', return_value=fake), \
             patch.object(m, 'execute', side_effect=AssertionError('live')), \
             patch.object(m.sqlite3, 'connect', side_effect=AssertionError('DB')), \
             patch.object(m, 'REPORT', self.root / 'RUN.json'), \
             patch.object(m, 'save_report') as save, redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['--dry-run']), 2)
            self.assertTrue(save.call_args.args[1]['dry_run'])

    def test_non_ff_fork_does_not_merge_install_or_reset(self):
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            if 'status' in argv:
                return ''
            if 'branch' in argv:
                return 'izaiah-main\n'
            if 'symbolic-ref' in argv:
                return 'refs/remotes/upstream/main\n'
            if 'merge-base' in argv:
                raise m.Refusal('command_failed')
            return 'abcdef\n'
        with patch.object(m, 'command', side_effect=command):
            result = m.update_fork(self.root)
        self.assertEqual(result['status'], 'SKIPPED_NON_FAST_FORWARD')
        self.assertFalse(any('merge' in c or 'install' in c or 'reset' in c for c in calls))

    def test_default_profile_only(self):
        self.assertTrue(m.default_scope({'HERMES_HOME': str(m.HOME)}, []))
        self.assertFalse(m.default_scope({'HERMES_HOME': str(m.HOME / 'profiles/x')}, []))
        self.assertFalse(m.default_scope({}, ['python', '--profile', 'other']))

    def test_live_requires_registered_unit_and_no_tui_ancestor(self):
        with self.assertRaises(m.Refusal):
            m.registered_context(None)

    def test_report_is_exclusive_private_and_no_replay(self):
        report = self.root / 'RUN.json'
        m.save_report(report, {'step': 'one'})
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        m.save_report(report, {'step': 'two'})
        self.assertIn('two', report.read_text())

    def test_literal_pid_identity_refusal_never_signals(self):
        with patch.object(m, 'same_process', return_value=False), \
             patch.object(m.os, 'pidfd_open', side_effect=AssertionError('signal boundary')):
            with self.assertRaises(m.Refusal):
                m.literal_signal({'pid': 1234}, m.signal.SIGTERM)

    def test_chrome_descendant_refuses_but_unrelated_browser_untouched(self):
        ps = 'PID PPID COMMAND\n10 1 python -m tui_gateway.entry\n11 10 chrome --remote-debugging-port=0\n12 1 chrome\n'
        with patch.object(m, 'command', return_value=ps):
            self.assertEqual(m.protected_browser_descendants([{'pid':10}]), [11])

    def test_sqlite_delete_mode_preserved(self):
        db = self.root / 'delete.db'
        with closing(sqlite3.connect(db)) as c:
            c.execute('create table t(x)')
        self.assertEqual(m.vacuum(db)['journal_after'], 'delete')

    def test_owning_live_sequence_on_scratch_and_backup_failure_restores(self):
        for fail in (False, True):
            with self.subTest(backup_failure=fail):
                home = self.root / str(fail)
                home.mkdir()
                db = home / 'state.db'
                with closing(sqlite3.connect(db)) as c:
                    c.execute('create table t(x)')
                old = {'pid':10,'ppid':9,'kind':'gateway','argv':['python','-m','hermes_cli.main','gateway','run'],'cwd':'/fixture'}
                parent = {'pid':20}
                new = {**old, 'pid':11}
                current = {'blockers':[], 'processes':[old], 'tui_parents':[parent]}
                report = {'run_id':'one','dry_run':False,'steps':[]}
                commands, signals = [], []
                def fake_command(argv, **kwargs):
                    commands.append([str(x) for x in argv])
                    if '--channel' in argv:
                        return '{"ok": true}'
                    return ''
                with ExitStack() as stack:
                    for name, value in [('HOME', home), ('BACKUPS', home/'backups')]:
                        stack.enter_context(patch.object(m, name, value))
                    stack.enter_context(patch.object(m, 'registered_context', return_value=None))
                    stack.enter_context(patch.object(m, 'preflight', return_value=current))
                    stack.enter_context(patch.object(m, 'command', side_effect=fake_command))
                    stack.enter_context(patch.object(m, 'recent_delegates', return_value=[]))
                    stack.enter_context(patch.object(m, 'protected_browser_descendants', return_value=[]))
                    stack.enter_context(patch.object(m, 'census', return_value=[new]))
                    stack.enter_context(patch.object(m, 'holders', return_value=[]))
                    stack.enter_context(patch.object(m, 'same_process', return_value=False))
                    stack.enter_context(patch.object(m, 'literal_signal', side_effect=lambda row,sig: signals.append((row['pid'],sig))))
                    update = stack.enter_context(patch.object(m, 'update_fork', return_value={'status':'SKIPPED_NON_FAST_FORWARD'}))
                    if fail:
                        stack.enter_context(patch.object(m, 'verified_backup', side_effect=m.Refusal('injected_backup_failure')))
                    rc = m.execute(report, home/'RUN.json', 'fixture.service')
                self.assertEqual(rc, int(fail))
                steps = [s['step'] for s in report['steps']]
                self.assertLess(steps.index('START'), steps.index('preflight'))
                self.assertLess(steps.index('term_10'), steps.index('verified_backup'))
                self.assertIn('unmask_gateway', steps)
                self.assertIn((20,m.signal.SIGCONT), signals)
                self.assertEqual(steps[-1], 'END')
                self.assertEqual(update.called, not fail)
                if fail:
                    self.assertNotIn('vacuum', steps)
                else:
                    self.assertLess(steps.index('verified_backup'), steps.index('vacuum'))
                    self.assertLess(steps.index('vacuum'), steps.index('fork_update'))
                    self.assertEqual(report['backup']['integrity_check'], 'ok')
                self.assertFalse(any('pkill' in c for c in commands))


if __name__ == '__main__':
    unittest.main()
