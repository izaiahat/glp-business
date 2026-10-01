#!/usr/bin/env python3
"""Read-only, conservative scheduler closure for the GLP adoption window.

Unknown scheduler control is an error. Opaque code under a known control is
conservatively affected, including disabled jobs; partial census is diagnostic only.
No scheduler command, imported module, environment file or workflow is executed.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys


class InventoryError(RuntimeError):
    def __init__(self, errors, report=None):
        self.errors = list(errors)
        self.report = report
        super().__init__('inventory_incomplete: ' + '; '.join(self.errors))


# The inventory reads the maintained adoption declarations without importing them.
def affected_paths(root: Path, hermes: Path) -> set[Path]:
    paths = {root / 'ops/.rebuild-sync-state.json'}
    for filename in ('glp44_scoped_adoption.py', 'glp73_paired_adoption.py'):
        tree = ast.parse((Path(__file__).parent / filename).read_text())
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            names = {n.id for n in node.targets if isinstance(n, ast.Name)}
            if ('PATHS' in names or
                    (filename == 'glp44_scoped_adoption.py' and names & {'PR119_PATHS', 'GLP331_PATHS'})):
                paths.update(root / p for p in ast.literal_eval(node.value))
            if 'WRAPPER' in names:
                paths.add(hermes / 'scripts' / ast.literal_eval(node.value))
            if 'JOBS' in names:
                paths.update(hermes / 'scripts' / row[1] for row in ast.literal_eval(node.value))
    return {p.resolve() for p in paths}


def _secret_path(path: Path) -> bool:
    return any(part == '.env' or part.startswith('.env.') or part.lower() in
               {'secrets', 'credentials', 'cookies', 'cookies.json'} for part in path.parts)


def _safe_command(command: str) -> None:
    # Do not copy embedded secrets into a durable plan or an error message.
    if re.search(r'(?i)(?:token|password|secret|api[_-]?key|cookie)\s*(?:=|\s)\S+', command):
        raise InventoryError(['command_contains_sensitive_literal_or_option'])


class Tracer:
    """Static local import/script closure; opaque execution refuses classification.

    A reached affected path is sufficient to conservatively stop the entire entry.
    Unrelated classification requires complete supported static closure.
    """
    def __init__(self, root: Path, hermes: Path, affected=None):
        self.root, self.hermes = root.resolve(), hermes.resolve()
        self.affected = affected if affected is not None else affected_paths(self.root, self.hermes)
        self.cache = {}
        # Cache only complete entrypoint closures within this discovery, never
        # partial nested results that depend on which ancestors were cut out.
        self.failures = {}

    def command(self, command: str, cwd: Path | None = None, visiting=None):
        visiting = set() if visiting is None else visiting
        _safe_command(command)
        cwd = cwd or self.root
        if any(str(p) in command or str(p.relative_to(self.root)) in command
               for p in self.affected if p.is_relative_to(self.root)):
            # Explicit affected operands suffice even for cat/cp/rsync/shell pins.
            return True, [str(p) for p in self.affected if str(p) in command or
                          (p.is_relative_to(self.root) and str(p.relative_to(self.root)) in command)]
        try:
            words = shlex.split(command, comments=True)
        except ValueError:
            raise InventoryError(['unparseable_command']) from None
        if not words:
            raise InventoryError(['empty_command'])
        if any(x in command for x in ('`', '$(', '${', '\n', '<<')) or re.search(r'[;&|<>]', command):
            raise InventoryError(['dynamic_shell_command'])
        # Strip simple constant environment assignments, without loading env files.
        while words and re.fullmatch(r'[A-Za-z_]\w*=.*', words[0]):
            words.pop(0)
        if not words:
            raise InventoryError(['assignment_without_command'])
        executable = Path(words[0]).name
        if executable == 'env':
            return self.command(shlex.join(words[1:]), cwd, visiting)
        if executable in {'bash', 'sh', 'dash'} and len(words) > 2 and words[1] == '-c':
            return self.command(words[2], cwd, visiting)
        if executable.startswith('python'):
            args = words[1:]
            while args and args[0] in ('-u', '-B', '-I', '-s', '-E'):
                args.pop(0)
            if len(args) >= 2 and args[0] == '-m':
                source = self.module(args[1], cwd)
                if source is None:
                    raise InventoryError(['unresolved_standard_library_entrypoint:' + args[1]])
                # -m package executes __main__ after its initializer; an import
                # of that package alone does not execute the entrypoint.
                name = args[1] + '.__main__' if source.name == '__init__.py' else args[1]
                return self.module_trace(name, cwd, visiting)
            if not args or args[0].startswith('-'):
                raise InventoryError(['dynamic_python_command'])
            return self.file(self.resolve(args[0], cwd), visiting)
        if executable in {'bash', 'sh', 'dash', 'exec'}:
            if len(words) < 2 or words[1].startswith('-'):
                raise InventoryError(['unsupported_shell_invocation'])
            return self.file(self.resolve(words[1], cwd), visiting)
        if words[0].endswith(('.py', '.sh')):
            return self.file(self.resolve(words[0], cwd), visiting)
        if executable in {'true', 'false', 'echo', 'printf', 'date'} and not re.search(r'[|&;<>]', command):
            return False, []
        raise InventoryError(['unclassified_executable:' + (executable if re.fullmatch(r'[A-Za-z0-9_.+-]{1,80}', executable) else '<opaque>')])

    def resolve(self, value, cwd):
        path = Path(value)
        if not path.is_absolute():
            path = cwd / path
        path = path.resolve()
        if _secret_path(path):
            raise InventoryError(['secret_file_reference_not_read'])
        return path

    def module(self, name, cwd, *, relative=False):
        rel = Path(*name.split('.'))
        bases = (cwd,) if relative else (cwd, self.root, self.root / 'scripts', self.hermes / 'scripts')
        suffixes = (rel.with_suffix('.py'), rel / '__init__.py') if name else (Path('__init__.py'),)
        candidates = [base / suffix for base in bases for suffix in suffixes]
        existing = {p.resolve() for p in candidates if p.is_file()}
        if len(existing) == 1:
            return existing.pop()
        if len(existing) > 1:
            raise InventoryError(['ambiguous_local_import:' + name])
        if not relative and name.split('.')[0] in sys.stdlib_module_names:
            return None
        raise InventoryError(['unresolved_import:' + name])

    def module_files(self, name, cwd, *, relative=False):
        """Every initializer executes before a dotted module's own file."""
        parts = name.split('.')
        for index in range(1, len(parts) + 1):
            yield self.module('.'.join(parts[:index]), cwd, relative=relative)

    def module_trace(self, name, cwd, visiting):
        reached, trace, errors = False, [], []
        try:
            for source in self.module_files(name, cwd):
                try:
                    hit, children = self.file(source, visiting)
                    reached |= hit
                    trace.extend(children)
                except InventoryError as exc:
                    errors.extend(exc.errors)
        except InventoryError as exc:
            errors.extend(exc.errors)
        if errors and not reached:
            raise InventoryError(sorted(set(errors)))
        return reached, sorted(set(trace) | {str(source) for source in visiting})

    def import_files(self, node, path):
        """Yield every Python file executed by a supported static import.

        Importing a dotted module executes its package initializers too. A
        from-import can name a package child, not just an attribute in __init__.
        Unknown children (including wildcard exports and namespace packages)
        deliberately refuse unrelated classification instead of disappearing.
        """
        base = path.parent
        relative = isinstance(node, ast.ImportFrom) and bool(node.level)
        if relative:
            for _ in range(node.level - 1):
                base = base.parent
        names = [item.name for item in node.names] if isinstance(node, ast.Import) else [node.module or '']
        for name in names:
            for source in self.module_files(name, base, relative=relative):
                yield source
            if isinstance(node, ast.ImportFrom) and source is not None and source.name == '__init__.py':
                for item in node.names:
                    if item.name == '*':
                        raise InventoryError(['unresolved_package_wildcard:' + str(source)])
                    # Restrict child lookup to this package: a same-named module
                    # elsewhere cannot stand in for a missing relative child.
                    yield self.module(item.name, source.parent, relative=True)

    def file(self, path, visiting):
        if path is None:
            return False, []
        path = path.resolve()
        if path in self.affected:
            return True, [str(path)]
        if path in self.cache:
            return self.cache[path]
        if path in visiting:
            return False, [str(path)]
        if path in self.failures:
            raise InventoryError(self.failures[path])
        entrypoint = not visiting
        # Siblings share this set; only successfully read, allowed sources are
        # added below, so complete entrypoint custody can retain every read.
        if _secret_path(path):
            raise InventoryError(['secret_file_reference_not_read'])
        if not path.is_relative_to(self.root) and not path.is_relative_to(self.hermes / 'scripts'):
            raise InventoryError(['source_outside_inventory_roots:' + str(path)])
        try:
            body = path.read_text()
        except (OSError, UnicodeError):
            raise InventoryError(['unreadable_source:' + str(path)]) from None
        visiting.add(path)
        trace, errors = [str(path)], []
        reached = False
        # Literal affected references catch hash readers and writers as well as imports.
        for target in self.affected:
            spellings = [str(target)]
            if target.is_relative_to(self.root):
                spellings.append(str(target.relative_to(self.root)))
            if any(s in body for s in spellings):
                reached = True
                trace.append(str(target))
        if reached:
            return True, sorted(set(trace))
        if path.suffix == '.py':
            try:
                tree = ast.parse(body)
            except SyntaxError:
                raise InventoryError(['invalid_python:' + str(path)]) from None
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    try:
                        for child in self.import_files(node, path):
                            try:
                                hit, children = self.file(child, visiting)
                                reached |= hit
                                trace.extend(children)
                            except InventoryError as exc:
                                errors.extend(exc.errors)
                    except InventoryError as exc:
                        errors.extend(exc.errors)
                if isinstance(node, ast.Call):
                    fn = ast.unparse(node.func)
                    # Literal importlib and runpy calls can be traced without execution.
                    if fn.endswith(('import_module', '__import__')):
                        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                            try:
                                hit, children = self.module_trace(node.args[0].value, path.parent, visiting)
                                reached |= hit
                                trace.extend(children)
                            except InventoryError as exc:
                                errors.extend(exc.errors)
                        else:
                            errors.append('dynamic_import:' + str(path))
                    elif fn.startswith('subprocess.') and fn.rsplit('.', 1)[-1] in {'run', 'Popen', 'call', 'check_call', 'check_output'}:
                        try:
                            argv = ast.literal_eval(node.args[0]) if node.args else None
                            if not isinstance(argv, list) or not all(isinstance(v, str) for v in argv):
                                raise ValueError()
                            hit, children = self.command(shlex.join(argv), self.root, visiting)
                            reached |= hit
                            trace.extend(children)
                        except (ValueError, TypeError, InventoryError):
                            errors.append('unresolved_subprocess:' + str(path))
                    elif fn.endswith(('exec', 'eval', 'compile', 'run_path', 'spec_from_file_location',
                                      'run', 'Popen', 'call', 'check_call', 'check_output', 'system',
                                      'open', 'read_text', 'read_bytes', 'write_text', 'write_bytes',
                                      'unlink', 'rename', 'replace', 'copy', 'copy2', 'move')):
                        errors.append('unresolved_runtime_access:' + str(path))
                    else:
                        # A callable's spelling does not establish its capability:
                        # builtins.open, io.open, imported functions, assignments,
                        # descriptors and callbacks can all hide file access.
                        # Without a proven closure, keep the scheduled task in
                        # the affected set via discover's conservative fallback.
                        errors.append('unresolved_callable:' + str(path))
        elif path.suffix == '.sh':
            for line in body.splitlines():
                line = line.strip()
                if not line or line.startswith('#') or line in ('set -e', 'set -eu', 'set -euo pipefail'):
                    continue
                try:
                    hit, children = self.command(line, path.parent, visiting)
                    reached |= hit
                    trace.extend(children)
                except InventoryError as exc:
                    errors.extend(exc.errors)
        else:
            errors.append('unsupported_source_type:' + str(path))
        if errors and not reached:
            errors = sorted(set(errors))
            if entrypoint:
                self.failures[path] = errors
            raise InventoryError(errors)
        if entrypoint:
            # A failed branch may have been shared with a later affected one.
            # Keep all read sources rather than losing its custody on dedupe.
            trace.extend(str(source) for source in visiting)
        result = reached, sorted(set(trace))
        if entrypoint:
            self.cache[path] = result
        return result


# These fields carry scheduling/admission state, never prompt, delivery, or auth.
HERMES_PRIOR = ('enabled', 'state', 'paused', 'paused_at', 'paused_at_utc', 'paused_reason',
                'disabled_reason', 'next_run_at', 'schedule', 'repeat', 'fire_claim', 'run_claim',
                'script', 'script_sha256', 'script_timeout_seconds', 'no_agent', 'workdir', 'pending_slot')


def _run(argv):
    try:
        result = subprocess.run(argv, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise InventoryError(['scheduler_probe_unavailable:' + argv[0]]) from None
    if result.returncode:
        if argv == ['crontab', '-l'] and b'no crontab for' in result.stderr:
            return ''
        # stderr may include unrelated data; report only the operation and exit code.
        raise InventoryError(['scheduler_probe_failed:' + ' '.join(argv[:3]) + ':' + str(result.returncode)])
    return result.stdout.decode()  # No universal-newline translation of cron custody.


TIMER_CONFIGURATION_FIELDS = (
    'Triggers', 'TimersCalendar', 'TimersMonotonic', 'AccuracyUSec',
    'RandomizedDelayUSec', 'Persistent', 'WakeSystem', 'RemainAfterElapse')
SERVICE_EXEC_FIELDS = ('ExecStart', 'ExecCondition', 'ExecStartPre', 'ExecStartPost',
                       'ExecStop', 'ExecStopPost')
SERVICE_CONFIGURATION_FIELDS = SERVICE_EXEC_FIELDS + (
    'WorkingDirectory', 'EnvironmentFiles', 'Type', 'User', 'Group',
    'RootDirectory', 'RootImage', 'Restart', 'RestartUSec', 'KillMode')


def configuration_sha256(unit, kind):
    """Hash static unit configuration, excluding runtime Exec and timer stamps."""
    fields = TIMER_CONFIGURATION_FIELDS if kind == 'timer' else SERVICE_CONFIGURATION_FIELDS
    static = {}
    for field in fields:
        value = unit.get(field, '')
        if field in SERVICE_EXEC_FIELDS:
            # Exec properties append PID/exit/start/stop observations. Only the
            # configured executable, arguments and error policy are static.
            value = re.sub(r'; (?:start_time|stop_time|pid|code|status)=[^;}]*', '', value)
        elif field in {'TimersCalendar', 'TimersMonotonic'}:
            value = re.sub(r'; next_elapse[^=;{}]*=[^;}]*', '', value)
        static[field] = value.strip()
    return hashlib.sha256(json.dumps(static, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _unit(unit, probe=_run):
    # Never request Environment: it may contain credentials. EnvironmentFiles
    # contains only paths, which are recorded as config but never opened.
    fields = tuple(dict.fromkeys(('Id', 'LoadState', 'ActiveState', 'UnitFileState', 'MainPID')
                                + TIMER_CONFIGURATION_FIELDS + SERVICE_CONFIGURATION_FIELDS))
    text = probe(['systemctl', '--user', 'show', unit, '--property=' + ','.join(fields)])
    result = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    if result.get('LoadState') != 'loaded':
        raise InventoryError(['unit_not_loaded:' + unit])
    return result


def _exec_start(value):
    matches = re.findall(r'argv\[\]=(.*?) ;', value)
    if len(matches) != 1:
        raise InventoryError(['unsupported_systemd_execstart'])
    return matches[0]


def timer_metadata(name, root, *, probe=_run):
    """Read one timer/service pair using the census's exact metadata contract.

    Return the parsed metadata and service properties for lifecycle tracing.
    This is not scheduler coverage: only discover() establishes the full cohort.
    """
    timer = _unit(name, probe)
    services = timer.get('Triggers', '').split()
    if len(services) != 1 or not services[0].endswith('.service'):
        raise InventoryError(['unresolved_timer_service:' + name])
    service = _unit(services[0], probe)
    prior = {'timer_active': timer['ActiveState'], 'timer_enabled': timer['UnitFileState'],
             'service': services[0], 'service_active': service['ActiveState'],
             'service_enabled': service.get('UnitFileState', ''),
             'timer_configuration_sha256': configuration_sha256(timer, 'timer'),
             'service_configuration_sha256': configuration_sha256(service, 'service')}
    return {'command': _exec_start(service.get('ExecStart', '')), 'prior': prior,
            'cwd': str(Path(service.get('WorkingDirectory') or root))}, service


def discover(root: Path, hermes: Path, *, probe=None) -> dict:
    """Enumerate ALL four scheduler surfaces; refuse partial or opaque coverage.

    InventoryError.report retains safe diagnostic entries for a read-only plan.
    The lifecycle must never enter using that partial report.
    """
    probe = probe or _run
    root, hermes = root.resolve(), hermes.resolve()
    tracer = Tracer(root, hermes)
    report = {'schema': 'glp.maintenance.inventory.v1', 'entries': [], 'coverage': {}, 'errors': [],
              'root': str(root), 'hermes': str(hermes), 'crontab_text': ''}

    def record(kind, identity, command, prior, cwd=None, forced=False):
        entry = {'kind': kind, 'id': identity, 'command': command, 'prior': prior,
                 'cwd': str(cwd or root), 'affected': None, 'trace': []}
        command_valid = False
        try:
            _safe_command(command)
            try:
                if not shlex.split(command):
                    raise ValueError()
            except ValueError:
                raise InventoryError(['unparseable_command']) from None
            command_valid = True
            if forced:
                entry.update(affected=True, trace=['unrestricted_agent_execution'])
            else:
                hit, trace = tracer.command(command, cwd)
                entry.update(affected=hit, trace=trace)
        except InventoryError as exc:
            hard_errors = [] if command_valid else exc.errors
            if hard_errors:
                report['errors'].extend(kind + ':' + identity + ':' + error for error in hard_errors)
            else:
                # A positively identified scheduler can stop opaque executable
                # code too. Close the whole entry; never label it unrelated.
                executable_sources = []
                for operand in shlex.split(command):
                    if operand.endswith(('.py', '.sh')):
                        source = Path(operand)
                        if not source.is_absolute():
                            source = (cwd or root) / source
                        if not _secret_path(source):
                            executable_sources.append(str(source.resolve()))
                entry.update(affected=True, classification='conservative',
                             opaque_reasons=sorted(set(exc.errors)),
                             trace=['unrestricted_execution', *executable_sources])
            # A sensitive literal must not land in diagnostics or the journal.
            if not command_valid and 'command_contains_sensitive_literal_or_option' in exc.errors:
                entry['command'] = '<redacted-unclassifiable>'
        entry['source_sha256'] = {}
        for source in entry['trace']:
            path = Path(source)
            if path.is_absolute() and not _secret_path(path):
                try:
                    entry['source_sha256'][source] = hashlib.sha256(path.read_bytes()).hexdigest()
                except FileNotFoundError:
                    entry['source_sha256'][source] = None
                except OSError:
                    report['errors'].append(kind + ':' + identity + ':source_hash_unreadable')
        report['entries'].append(entry)

    try:
        jobs = json.loads((hermes / 'cron/jobs.json').read_text())['jobs']
        ids = [j['id'] for j in jobs]
        if len(ids) != len(set(ids)):
            raise InventoryError(['duplicate_hermes_job_id'])
        for job in jobs:
            prior = {key: job[key] for key in HERMES_PRIOR if key in job}
            script = job.get('script')
            if script:
                path = Path(script)
                if not path.is_absolute():
                    path = hermes / 'scripts' / path
                record('hermes', job['id'], shlex.quote(str(path)), prior,
                       Path(job.get('workdir') or root))
                if job.get('no_agent') is not True:
                    # A script may admit a following agent turn; include that capability.
                    report['entries'][-1]['affected'] = True
                    report['entries'][-1]['trace'].append('unrestricted_agent_execution')
            else:
                # Agent jobs have arbitrary filesystem capability: always stop them.
                record('hermes', job['id'], 'hermes-agent-job', prior, forced=True)
        report['coverage']['hermes'] = True
    except (OSError, ValueError, KeyError, TypeError, InventoryError) as exc:
        report['errors'].append('hermes_census_failed:' + type(exc).__name__)
    try:
        cron = probe(['crontab', '-l'])
        # Refuse storing a full crontab containing inline credentials.
        _safe_command(cron)
        report['crontab_text'] = cron
        for index, raw_line in enumerate(cron.splitlines()):
            # Preserve identity through our reversible comment pause. The
            # backend verifies the journal-specific marker before acceptance.
            marked = re.fullmatch(r'# GLP44/73 maintenance ([0-9a-f]{16}) (.*)', raw_line)
            line = marked.group(2) if marked else raw_line
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            if re.match(r'^[A-Za-z_]\w*\s*=', stripped):
                # PATH/SHELL/HOME can change command resolution; never assume defaults.
                raise InventoryError(['crontab_environment_requires_resolution'])
            parts = stripped.split(None, 1 if stripped.startswith('@') else 5)
            if len(parts) != (2 if stripped.startswith('@') else 6):
                raise InventoryError(['invalid_crontab_line:' + str(index)])
            record('crontab', str(index), parts[-1], {'line': line, 'index': index,
                   'maintenance_marker': marked.group(1) if marked else None})
        report['coverage']['crontab'] = True
    except InventoryError as exc:
        report['errors'].extend(exc.errors)
    try:
        units = set()
        for argv in (['systemctl', '--user', 'list-unit-files', '--type=timer', '--no-legend', '--no-pager'],
                     ['systemctl', '--user', 'list-units', '--all', '--type=timer', '--no-legend', '--no-pager']):
            for line in probe(argv).splitlines():
                units.update(word for word in line.split() if word.endswith('.timer'))
        for name in sorted(units):
            metadata, service = timer_metadata(name, root, probe=probe)
            prior = metadata['prior']
            record('systemd', name, metadata['command'], prior, Path(metadata['cwd']))
            timer_entry = report['entries'][-1]
            hooks = [field for field in SERVICE_EXEC_FIELDS if field != 'ExecStart' and service.get(field)]
            if hooks or service.get('EnvironmentFiles'):
                # The timer admits the complete service lifecycle. Never infer
                # safety from a harmless main ExecStart when hooks/env can run.
                timer_entry.update(affected=True, classification='conservative')
                timer_entry['trace'].append('service_lifecycle_or_environment')
            timer_entry['lifecycle_commands'] = []
            for field in hooks:
                commands = re.findall(r'argv\[\]=(.*?) ;', service[field])
                if not commands:
                    timer_entry.setdefault('opaque_reasons', []).append('opaque_systemd_' + field)
                for command in commands:
                    record('systemd', name, command, prior, Path(service.get('WorkingDirectory') or root))
                    hook_entry = report['entries'].pop()
                    timer_entry['lifecycle_commands'].append({'field': field, 'command': hook_entry['command']})
                    timer_entry['trace'].extend(hook_entry['trace'])
                    timer_entry['source_sha256'].update(hook_entry['source_sha256'])
            timer_entry['trace'] = sorted(set(timer_entry['trace']))
        report['coverage']['systemd'] = True
    except (InventoryError, KeyError) as exc:
        report['errors'].extend(exc.errors if isinstance(exc, InventoryError) else ['invalid_systemd_state'])
    try:
        # The actual host action registry is the bridge's maintained launch contract.
        actions_path = root / 'ops/n8n-bridge/actions.json'
        actions = json.loads(actions_path.read_text())
        bridge = _unit('n8n-bridge.service', probe)
        prior = {'unit': 'n8n-bridge.service', 'service': 'n8n-bridge.service', 'service_active': bridge['ActiveState'],
                 'service_enabled': bridge.get('UnitFileState', ''),
                 'actions_sha256': hashlib.sha256(actions_path.read_bytes()).hexdigest(),
                 'main_pid': bridge.get('MainPID', '')}
        action_start = len(report['entries'])
        for name, action in sorted(actions.items()):
            argv = action['argv']
            if not isinstance(argv, list) or not all(isinstance(v, str) for v in argv):
                raise InventoryError(['invalid_n8n_argv:' + name])
            record('n8n', name, shlex.join(argv), dict(prior), Path(action.get('cwd') or root))
        # Gate the listener once, preserving actual per-action command traces.
        # Arbitrary agent actions make the listener unconditionally affected.
        action_entries = report['entries'][action_start:]
        del report['entries'][action_start:]
        record('n8n', 'n8n-bridge.service', _exec_start(bridge.get('ExecStart', '')), prior, forced=True)
        report['entries'][-1]['actions'] = action_entries
        report['entries'][-1]['trace'] = sorted({source for entry in action_entries for source in entry['trace']} | {'unrestricted_agent_execution', str(actions_path)})
        report['entries'][-1]['source_sha256'].update({source: digest for entry in action_entries for source, digest in entry['source_sha256'].items()})
        report['entries'][-1]['source_sha256'][str(actions_path)] = prior['actions_sha256']
        report['coverage']['n8n'] = True
    except (OSError, ValueError, KeyError, TypeError, InventoryError) as exc:
        report['errors'].extend(exc.errors if isinstance(exc, InventoryError) else ['n8n_census_failed:' + type(exc).__name__])
    if report['errors'] or set(report['coverage']) != {'hermes', 'crontab', 'systemd', 'n8n'}:
        raise InventoryError(sorted(set(report['errors'])), report)
    return report
