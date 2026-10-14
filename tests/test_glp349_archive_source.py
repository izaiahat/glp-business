"""Exact curated archive source, synthetic /proc, no SQL/process/send effects."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ArchiveSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved_path = sys.path[:]
        cls.saved_modules = {n: sys.modules.get(n) for n in
                             ('discord_fmt', 'bounded_pruner', 'disk_retention', 'host_disk_pruner')}
        load('discord_fmt', 'ops/discord_fmt.py')
        load('bounded_pruner', 'scripts/bounded_pruner.py')
        cls.retention = load('disk_retention', 'scripts/disk_retention.py')
        cls.pruner = load('host_disk_pruner', 'scripts/host_disk_pruner.py')
        cls.archive = load('glp349_archive', 'scripts/hermes_state_archive/archive.py')
        cls.launcher = load('glp349_launcher', 'ops/hermes-cron-scripts/hermes_state_archive_weekly.py')
        # The maintained pruner has host path defaults; all evaluated modules above
        # are loaded explicitly from this checkout, never the operational tree.
        for module in (cls.retention, cls.pruner, cls.archive):
            assert module.__file__ is not None
            assert Path(module.__file__).is_relative_to(ROOT)

    @classmethod
    def tearDownClass(cls):
        sys.path[:] = cls.saved_path
        for name, value in cls.saved_modules.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value

    @contextlib.contextmanager
    def effects_denied(self):
        with contextlib.ExitStack() as stack:
            for owner, name in ((sqlite3, 'connect'), (subprocess, 'run'),
                                (subprocess, 'Popen'), (socket, 'socket'), (os, 'kill'),
                                (os, 'replace'), (Path, 'unlink')):
                stack.enter_context(mock.patch.object(owner, name,
                    side_effect=AssertionError('prohibited effect: ' + name)))
            yield

    def test_snapshot_helpers_and_owning_archive_refusal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proc = root / 'proc' / '42'
            (proc / 'fd').mkdir(parents=True)
            (proc / 'cmdline').write_bytes(b'fixture\0idle\0')
            (proc / 'environ').write_bytes(b'FIXTURE=1\0')
            (proc / 'cwd').symlink_to(root / 'elsewhere')
            archive = root / 'archive.db'
            archive.write_bytes(b'synthetic archive, not SQLite')
            proof = root / 'verify.json'
            proof.write_text(json.dumps({'archive': str(archive), 'result': 'PASS',
                'integrity_check': 'ok', 'problems': [],
                'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}))
            policy = {'archives': [{'verify': {'path': str(proof),
                'sha256': hashlib.sha256(proof.read_bytes()).hexdigest()}}]}
            real_path = Path
            def fixture_path(*args):
                if args and args[0] == '/proc':
                    return root / 'proc'
                return real_path(*args)
            with mock.patch.object(self.pruner, 'Path', side_effect=fixture_path), self.effects_denied():
                # Exact optimized helpers snapshot FD and maps once; (deleted)
                # suffix stripping and paths with spaces preserve old semantics.
                for kind in ('fd', 'maps'):
                    with self.subTest(reference=kind):
                        fd = proc / 'fd' / '9'
                        if kind == 'fd':
                            fd.symlink_to(str(archive) + ' (deleted)')
                            (proc / 'maps').write_text('')
                        else:
                            # Removing the synthetic link is fixture setup, not
                            # an archive effect, so bypass the patched API only here.
                            os.unlink(fd)
                            (proc / 'maps').write_text('1-2 r--p 0 00:00 0 ' + str(archive) + ' (deleted)\n')
                        rows = self.pruner.process_inventory()
                        self.assertEqual(len(rows), 1)
                        self.assertIn('_refs', rows[0])
                        self.assertIn(str(archive), rows[0]['_refs'])
                        self.assertTrue(self.pruner.path_is_active(archive, rows))
                        # Compatibility for callers without the optimized snapshot.
                        legacy = {k: v for k, v in rows[0].items() if k != '_refs'}
                        self.assertTrue(self.pruner.path_is_active(archive, [legacy]))
                        result = self.retention.archive_retention(self.pruner, policy, dry_run=False)
                        self.assertEqual(result[0]['status'], 'RETENTION_REFUSED')
                        self.assertEqual(result[0]['reason'], 'archive_active_or_hash_changed')
                        self.assertEqual(result[0]['bytes_freed'], 0)
                (proc / 'maps').write_text('')
                rows = self.pruner.process_inventory()  # fresh after reference removal
                self.assertFalse(self.pruner.path_is_active(archive, rows))
                self.assertEqual(self.retention.archive_retention(self.pruner, policy,
                    dry_run=True)[0]['status'], 'DRY_RUN')
                self.assertEqual(archive.read_bytes(), b'synthetic archive, not SQLite')
                for field in ('cmdline', 'environ', 'cwd'):
                    row = {'pid': 42, 'cmdline': '', 'environ': '', 'cwd': '/', '_refs': []}
                    if field == 'cwd':
                        row['_refs'] = [str(archive)]
                    else:
                        row[field] = str(archive)
                    self.assertTrue(self.pruner.path_is_active(archive, [row]))

    def test_quiet_flag_refuses_before_sql_or_process_effects(self):
        with self.effects_denied(), contextlib.redirect_stderr(io.StringIO()):
            for command in ('copy', 'delete', 'rebuild-fts', 'vacuum'):
                with self.subTest(command=command), self.assertRaises(SystemExit) as exc:
                    self.archive.main(['--db', '/not-opened/state.db', command])
                self.assertEqual(exc.exception.code, 2)

    def test_candidate_pin_closure_and_real_launcher_drift_refusal(self):
        pin = ROOT / 'scripts/hermes_state_archive/weekly-pins.json'
        data = json.loads(pin.read_text())
        for absolute, expected in data['files'].items():
            if absolute.startswith('/home/ubuntu/business/'):
                relative = absolute.removeprefix('/home/ubuntu/business/')
            else:
                self.assertEqual(absolute, '/home/ubuntu/.config/systemd/user/hermes-state-archive-weekly.service')
                relative = 'ops/systemd/hermes-state-archive-weekly.service'
            self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), expected)
        self.assertEqual(hashlib.sha256(pin.read_bytes()).hexdigest(), self.launcher.PIN_HASH)
        def mapped_path(absolute):
            if absolute.startswith('/home/ubuntu/business/'):
                return ROOT / absolute.removeprefix('/home/ubuntu/business/')
            return ROOT / 'ops/systemd/hermes-state-archive-weekly.service'
        with mock.patch.object(self.launcher, 'PIN_FILE', pin), \
             mock.patch.object(self.launcher, 'Path', side_effect=mapped_path), \
             mock.patch.object(self.launcher.subprocess, 'run', return_value=mock.Mock(returncode=0)) as start, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.launcher.main(), 0)
            self.assertEqual(start.call_args.args[0], ['systemctl', '--user', 'start', '--no-block',
                                                      'hermes-state-archive-weekly.service'])
        # A coherent old manifest/literal cannot bless changed dependency bytes.
        old = dict(data)
        old['files'] = dict(data['files'])
        old['files']['/home/ubuntu/business/scripts/host_disk_pruner.py'] = \
            'd05c344bc53c6f5b2f198ce1a8e47b38ff0406681942461c0eebbbd08bc42cec'
        with tempfile.TemporaryDirectory() as temp:
            stale = Path(temp) / 'pins.json'
            stale.write_text(json.dumps(old))
            with mock.patch.object(self.launcher, 'PIN_FILE', stale), \
                 mock.patch.object(self.launcher, 'PIN_HASH', hashlib.sha256(stale.read_bytes()).hexdigest()), \
                 mock.patch.object(self.launcher, 'Path', side_effect=mapped_path), \
                 self.effects_denied(), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.launcher.main(), 1)


if __name__ == '__main__':
    unittest.main()
