"""Inventory contracts use isolated scheduler fixtures; never operate host schedulers."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('maintenance_inventory', Path(__file__).parents[1] / 'scripts/maintenance_inventory.py')
inv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inv)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'business'
        self.hermes = Path(self.temp.name) / 'hermes'
        for directory in (self.root / 'scripts', self.root / 'ops/n8n-bridge', self.hermes / 'scripts', self.hermes / 'cron'):
            directory.mkdir(parents=True)
        self.target = self.root / 'scripts/glp_price_index_weekly.py'
        self.target.write_text('print("affected")\n')
        self.entry = self.root / 'scripts/entry.py'
        self.entry.write_text('import glp_price_index_weekly\n')
        (self.hermes / 'scripts/wrapper.sh').write_text('python3 ' + str(self.entry) + '\n')
        self.jobs = [{'id': 'job1', 'script': 'wrapper.sh', 'enabled': True, 'state': 'scheduled',
                      'next_run_at': '2030-01-01T00:00:00Z', 'no_agent': True}]
        self.write_jobs()
        (self.root / 'ops/n8n-bridge/actions.json').write_text(json.dumps({'entry': {'argv': ['python3', str(self.entry)]}}))
        self.cron = '# original comment\n0 1 * * * python3 ' + str(self.entry) + '\n'

    def write_jobs(self):
        (self.hermes / 'cron/jobs.json').write_text(json.dumps({'jobs': self.jobs}))

    def probe(self, argv):
        if argv == ['crontab', '-l']:
            return self.cron
        if 'list-unit-files' in argv:
            return 'reader.timer enabled enabled\n'
        if 'list-units' in argv:
            return 'reader.timer loaded active waiting\n'
        if 'show' in argv:
            unit = argv[3]
            if unit == 'reader.timer':
                return 'Id=reader.timer\nLoadState=loaded\nActiveState=active\nUnitFileState=enabled\nTriggers=reader.service\n'
            return ('Id=' + unit + '\nLoadState=loaded\nActiveState=active\nUnitFileState=enabled\nMainPID=123\n'
                    'ExecStart={ path=/usr/bin/python3 ; argv[]=python3 ' + str(self.entry) + ' ; ignore_errors=no ; }\n'
                    'WorkingDirectory=' + str(self.root) + '\n')
        raise AssertionError(argv)

    def test_inventory_derives_transitive_imports_and_exact_scheduler_preimages(self):
        report = inv.discover(self.root, self.hermes, probe=self.probe)
        self.assertEqual(set(report['coverage']), {'hermes', 'crontab', 'systemd', 'n8n'})
        self.assertEqual(report['crontab_text'], self.cron)
        self.assertTrue(all(row['affected'] for row in report['entries']))
        hermes = next(row for row in report['entries'] if row['kind'] == 'hermes')
        self.assertEqual(hermes['prior'], {k: v for k, v in self.jobs[0].items() if k != 'id'})
        self.assertIn(str(self.target), hermes['trace'])
        timer = next(row for row in report['entries'] if row['kind'] == 'systemd')
        self.assertEqual(timer['prior']['timer_enabled'], 'enabled')
        self.assertEqual(timer['prior']['service_active'], 'active')

    def test_from_package_import_child_must_not_skip_affected_reader(self):
        # Independent PR109 review oracle, retained against production discovery.
        package = self.root / 'scripts' / 'reader_package'
        package.mkdir()
        (package / '__init__.py').write_text('')
        (package / 'child.py').write_text('import glp_price_index_weekly\n')
        self.entry.write_text('from reader_package import child\n')
        report = inv.discover(self.root, self.hermes, probe=self.probe)
        row = next(row for row in report['entries'] if row['kind'] == 'hermes')
        self.assertEqual(set(report['coverage']), {'hermes', 'crontab', 'systemd', 'n8n'})
        self.assertTrue(row['affected'], f'complete census silently skips import child: {row}')
        self.assertIn(str(self.target), row['trace'])
        self.assertIn(str(package / 'child.py'), row['trace'])

    def test_unknown_callable_capability_must_not_skip_affected_reader(self):
        # Independent PR109 review oracle: aliases defeat suffix-based detection.
        self.entry.write_text('from builtins import open as read_source\n'
                              'path = "scripts/" + "glp_price_index_weekly.py"\n'
                              'read_source(path).read()\n')
        report = inv.discover(self.root, self.hermes, probe=self.probe)
        row = next(row for row in report['entries'] if row['kind'] == 'hermes')
        self.assertTrue(row['affected'], f'complete census silently skips aliased file reader: {row}')
        self.assertEqual(row['classification'], 'conservative')
        self.assertTrue(any('unresolved_callable' in error for error in row['opaque_reasons']))

    def test_relative_child_and_dotted_package_initializers_are_traced(self):
        package = self.root / 'scripts' / 'reader_package'
        package.mkdir()
        (package / '__init__.py').write_text('from . import child\n')
        (package / 'child.py').write_text('import glp_price_index_weekly\n')
        (package / 'empty.py').write_text('')
        for source in ('import reader_package\n', 'import reader_package.empty\n'):
            with self.subTest(source=source):
                self.entry.write_text(source)
                hit, trace = inv.Tracer(self.root, self.hermes).file(self.entry, set())
                self.assertTrue(hit)
                self.assertIn(str(self.target), trace)
                self.assertIn(str(package / '__init__.py'), trace)

    def test_literal_importlib_traces_dotted_package_initializers(self):
        package = self.root / 'scripts' / 'reader_package'
        package.mkdir()
        (package / '__init__.py').write_text('import glp_price_index_weekly\n')
        (package / 'child.py').write_text('')
        for call in ('importlib.import_module("reader_package.child")',
                     '__import__("reader_package.child")'):
            with self.subTest(call=call):
                self.entry.write_text('import importlib\n' + call + '\n')
                hit, trace = inv.Tracer(self.root, self.hermes).file(self.entry, set())
                self.assertTrue(hit)
                self.assertIn(str(package / '__init__.py'), trace)
                self.assertIn(str(self.target), trace)

    def test_python_module_command_traces_dotted_package_initializers(self):
        package = self.root / 'scripts' / 'reader_package'
        package.mkdir()
        (package / '__init__.py').write_text('import glp_price_index_weekly\n')
        (package / 'child.py').write_text('')
        hit, trace = inv.Tracer(self.root, self.hermes).command('python3 -m reader_package.child')
        self.assertTrue(hit)
        self.assertIn(str(package / '__init__.py'), trace)
        self.assertIn(str(self.target), trace)

    def test_python_package_module_command_traces_main(self):
        package = self.root / 'scripts' / 'reader_package'
        package.mkdir()
        (package / '__init__.py').write_text('')
        (package / '__main__.py').write_text('import glp_price_index_weekly\n')
        hit, trace = inv.Tracer(self.root, self.hermes).command('python3 -m reader_package')
        self.assertTrue(hit)
        self.assertIn(str(package / '__main__.py'), trace)
        self.assertIn(str(self.target), trace)

    def test_unresolved_import_children_and_callable_aliases_are_conservative(self):
        package = self.root / 'scripts' / 'reader_package'
        package.mkdir()
        (package / '__init__.py').write_text('')
        for source in ('from reader_package import missing\n',
                       'from reader_package import *\n',
                       'import builtins as b\nreader = b.open\nreader(input())\n',
                       'from io import open as reader\nreader(input())\n',
                       'callbacks[0](input())\n'):
            with self.subTest(source=source):
                self.entry.write_text(source)
                with self.assertRaises(inv.InventoryError):
                    inv.Tracer(self.root, self.hermes).file(self.entry, set())
                report = inv.discover(self.root, self.hermes, probe=self.probe)
                row = next(row for row in report['entries'] if row['kind'] == 'hermes')
                self.assertTrue(row['affected'])
                self.assertEqual(row['classification'], 'conservative')

    def test_resolved_empty_child_can_still_be_classified_unaffected(self):
        package = self.root / 'scripts' / 'reader_package'
        package.mkdir()
        (package / '__init__.py').write_text('')
        (package / 'child.py').write_text('import json\n')
        self.entry.write_text('from reader_package import child\n')
        hit, trace = inv.Tracer(self.root, self.hermes).file(self.entry, set())
        self.assertFalse(hit)
        self.assertIn(str(package / 'child.py'), trace)

    def test_opaque_import_is_never_classified_unaffected_even_when_paused(self):
        self.entry.write_text('import importlib\nimportlib.import_module(input())\n')
        self.jobs[0].update(enabled=False, state='paused')
        self.write_jobs()
        with self.assertRaises(inv.InventoryError) as raised:
            inv.Tracer(self.root, self.hermes).file(self.entry, set())
        self.assertTrue(any('dynamic_import' in error for error in raised.exception.errors))
        report = inv.discover(self.root, self.hermes, probe=self.probe)
        row = next(row for row in report['entries'] if row['kind'] == 'hermes')
        self.assertTrue(row['affected'])
        self.assertEqual(row['classification'], 'conservative')
        self.assertTrue(any('dynamic_import' in error for error in row['opaque_reasons']))

    def test_scheduler_probe_failure_never_becomes_empty_coverage(self):
        def failed(argv):
            if argv == ['crontab', '-l']:
                raise inv.InventoryError(['permission_denied'])
            return self.probe(argv)
        with self.assertRaises(inv.InventoryError) as raised:
            inv.discover(self.root, self.hermes, probe=failed)
        self.assertNotIn('crontab', raised.exception.report['coverage'])
        self.assertIn('permission_denied', raised.exception.errors)

    def test_unrestricted_agent_jobs_are_conservatively_affected(self):
        self.jobs.append({'id': 'agent', 'enabled': False, 'state': 'paused'})
        self.write_jobs()
        report = inv.discover(self.root, self.hermes, probe=self.probe)
        agent = next(row for row in report['entries'] if row['id'] == 'agent')
        self.assertTrue(agent['affected'])
        self.assertEqual(agent['trace'], ['unrestricted_agent_execution'])

    def test_paused_crontab_keeps_stable_entry_identity_and_command(self):
        before = inv.discover(self.root, self.hermes, probe=self.probe)
        lines = self.cron.splitlines(keepends=True)
        lines[1] = '# GLP44/73 maintenance 0123456789abcdef ' + lines[1]
        self.cron = ''.join(lines)
        after = inv.discover(self.root, self.hermes, probe=self.probe)
        key = lambda row: (row['kind'], row['id'], row['command'], row['affected'])
        self.assertEqual([key(row) for row in before['entries']], [key(row) for row in after['entries']])
        cron = next(row for row in after['entries'] if row['kind'] == 'crontab')
        self.assertEqual(cron['prior']['maintenance_marker'], '0123456789abcdef')
        bridge = [row for row in after['entries'] if row['kind'] == 'n8n']
        self.assertEqual(len(bridge), 1)
        self.assertEqual(bridge[0]['id'], 'n8n-bridge.service')
        self.assertEqual(bridge[0]['actions'][0]['id'], 'entry')

    def test_unknown_scheduler_identity_fails_closed(self):
        self.jobs.append(dict(self.jobs[0]))
        self.write_jobs()
        with self.assertRaises(inv.InventoryError):
            inv.discover(self.root, self.hermes, probe=self.probe)

    def test_timer_with_harmless_main_and_affected_pre_hook_is_included(self):
        def with_hook(argv):
            output = self.probe(argv)
            if 'show' in argv:
                properties = argv[-1].split('=', 1)[1].split(',')
                self.assertNotIn('Environment', properties)
                self.assertIn('TimersCalendar', properties)
                self.assertIn('ExecCondition', properties)
            if 'show' in argv and argv[3] == 'reader.service':
                output = ('Id=reader.service\nLoadState=loaded\nActiveState=inactive\nUnitFileState=static\n'
                          'ExecStart={ path=/bin/true ; argv[]=/bin/true ; ignore_errors=no ; pid=0 ; }\n'
                          'ExecStartPre={ path=/usr/bin/python3 ; argv[]=python3 ' + str(self.entry) +
                          ' ; ignore_errors=no ; pid=123 ; }\n')
            return output
        report = inv.discover(self.root, self.hermes, probe=with_hook)
        timer = next(row for row in report['entries'] if row['kind'] == 'systemd')
        self.assertTrue(timer['affected'])
        self.assertIn(str(self.target), timer['trace'])
        self.assertEqual(timer['lifecycle_commands'][0]['field'], 'ExecStartPre')
        self.assertEqual(len(timer['prior']['timer_configuration_sha256']), 64)
        self.assertEqual(len(timer['prior']['service_configuration_sha256']), 64)
        bridge = next(row for row in report['entries'] if row['kind'] == 'n8n')
        registry = str(self.root / 'ops/n8n-bridge/actions.json')
        self.assertIn(registry, bridge['trace'])
        self.assertEqual(bridge['source_sha256'][registry], bridge['prior']['actions_sha256'])

    def test_unit_configuration_fingerprint_ignores_runtime_stamps_but_detects_edits(self):
        first = {'ExecStart': '{ path=/bin/true ; argv[]=/bin/true ; ignore_errors=no ; pid=3 ; status=0 ; }'}
        later = {'ExecStart': first['ExecStart'].replace('pid=3', 'pid=99').replace('status=0', 'status=1')}
        self.assertEqual(inv.configuration_sha256(first, 'service'), inv.configuration_sha256(later, 'service'))
        later['ExecStartPre'] = '{ path=/bin/false ; argv[]=/bin/false ; ignore_errors=no ; }'
        self.assertNotEqual(inv.configuration_sha256(first, 'service'), inv.configuration_sha256(later, 'service'))
        timer = {'TimersCalendar': '{ OnCalendar=daily ; next_elapse=100 ; }', 'Persistent': 'yes'}
        moved = dict(timer, TimersCalendar='{ OnCalendar=daily ; next_elapse=200 ; }')
        self.assertEqual(inv.configuration_sha256(timer, 'timer'), inv.configuration_sha256(moved, 'timer'))
        moved['Persistent'] = 'no'
        self.assertNotEqual(inv.configuration_sha256(timer, 'timer'), inv.configuration_sha256(moved, 'timer'))

    def test_unknown_executable_and_sensitive_inputs_refuse_without_disclosure(self):
        tracer = inv.Tracer(self.root, self.hermes)
        for command in ('mystery-program --run', 'curl --token DO_NOT_EMIT', 'python3 -c "exec(input())"'):
            with self.assertRaises(inv.InventoryError) as raised:
                tracer.command(command)
            self.assertNotIn('DO_NOT_EMIT', str(raised.exception))
        secret = self.root / '.env'
        secret.write_text('NEVER_READ')
        with self.assertRaises(inv.InventoryError):
            tracer.file(secret, set())


if __name__ == '__main__':
    unittest.main()


def test_scheduler_probe_preserves_exact_crontab_line_endings(monkeypatch):
    from types import SimpleNamespace
    body = b'7 * * * * python reader.py\r\n# retained\r\n'
    monkeypatch.setattr(inv.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=body))
    assert inv._run(['crontab', '-l']) == body.decode()
