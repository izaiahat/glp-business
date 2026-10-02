#!/usr/bin/env python3
"""Exact hourly bridge-cron registration and a journaled single-line failure hold."""
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time

from maintenance_window import LinuxBackend, SCHEMA, atomic_json, identity, operation_lock
from maxbounty_pin_release import read_regular

SOURCES = ('scripts/cron_bridge_action.py', 'scripts/glp_analytics_brain_cadence.py')
MANIFEST = 'ops/scheduled-source-drift-manifest.json'
ACTIONS = 'ops/n8n-bridge/actions.json'


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _require(value, message):
    if not value:
        raise RuntimeError(message)


def _command(root, hermes, entry):
    command = entry['command']
    _require(all(re.fullmatch(r'[A-Za-z0-9_./-]+', str(path)) for path in (root, hermes)),
             'unsupported_shell_target_path')
    # Only this registered shell grammar is supported, including its redirect.
    # Reject expansion even when shlex would otherwise turn it into an operand.
    _require(not any(token in command for token in ('$', '`', '\n', '\r', '\x00', ';', '|', '<')),
             'unsupported_hourly_command_grammar')
    words = shlex.split(command)
    expected = ['cd', str(root), '&&', str(hermes / 'hermes-agent/venv/bin/python'),
                str(root / SOURCES[0]), 'analytics_brain', '--scheduled', '--cron',
                '7 * * * *', '--timezone', 'America/Los_Angeles', '--arg', 'phase=hourly',
                '>>', str(root / 'reports/ops/n8n-to-cron-20260924/cron.log'), '2>&1']
    _require(words == expected, 'unsupported_hourly_command_grammar')
    prior = entry['prior']
    _require(not prior.get('maintenance_marker'), 'hourly_registration_already_paused')
    parts = prior['line'].strip().split(None, 5)
    _require(parts[:5] == ['7', '*', '*', '*', '*'] and parts[5] == command,
             'hourly_schedule_or_command_moved')
    return expected[-2]


def registration(root, hermes, backend=None):
    """Read only: bind the actual hourly command, full control preimage and leaves."""
    root, hermes = Path(root).resolve(strict=True), Path(hermes).resolve(strict=True)
    backend = backend or LinuxBackend(root, hermes, root / 'reports/ops/glp-ship-unused.json')
    _require(Path(backend.root).resolve() == root and Path(backend.hermes).resolve() == hermes,
             'collector_backend_target_mismatch')
    report = backend.inventory()
    _require(report.get('complete', not report.get('errors')) and not report.get('errors'),
             'collector_inventory_incomplete')
    candidates = [entry for entry in report['entries'] if entry['kind'] == 'crontab'
                  and 'cron_bridge_action.py' in entry.get('command', '')
                  and 'analytics_brain' in entry.get('command', '')
                  and 'phase=hourly' in entry.get('command', '')]
    _require(len(candidates) == 1, 'hourly_registration_not_unique')
    entry = candidates[0]
    log_path = _command(root, hermes, entry)
    prior = backend.snapshot(entry)
    lines = prior['text'].splitlines()
    _require(lines[prior['index']] == prior['line'], 'hourly_control_preimage_changed')
    job_id = 'crontab:' + _sha(prior['line'].strip().encode())[:16]
    manifest_body = read_regular(root / MANIFEST)[0]
    manifest = json.loads(manifest_body)
    _require(manifest.get('schema') == 'scheduled-source-drift-v1', 'unknown_source_drift_manifest')
    bindings = [row for row in manifest['jobs'] if row.get('id') == job_id and row.get('surface') == 'crontab']
    _require(len(bindings) == 1 and bindings[0].get('payloads') == [], 'hourly_registered_source_binding_missing')
    sources = bindings[0]['sources']
    _require(len(sources) == len(SOURCES) and {row.get('source') for row in sources} == set(SOURCES),
             'hourly_retained_source_scope_changed')
    retained = {}
    for row in sources:
        rel = row['source']
        actual = _sha(read_regular(root / rel)[0])
        _require(row.get('reviewed_sha256') == actual, 'hourly_retained_source_pin_mismatch:' + rel)
        retained[rel] = actual
    actions_body = read_regular(root / ACTIONS)[0]
    action = json.loads(actions_body)['analytics_brain']
    supported_argv = [['python3', SOURCES[1]],
                      [str(hermes / 'hermes-agent/venv/bin/python'), str(root / SOURCES[1])]]
    _require(action.get('argv') in supported_argv and action.get('cwd') == str(root)
             and action.get('allowed_args') == ['phase'], 'hourly_bridge_action_contract_changed')
    timeout = action.get('timeout_s', 300)
    _require(isinstance(timeout, int) and not isinstance(timeout, bool) and 0 < timeout <= 86400,
             'hourly_bridge_timeout_invalid')
    return {'schema': 'glp-ship-hourly-registration-v1', 'job_id': job_id,
            'entry': copy.deepcopy(entry), 'prior': prior, 'command': entry['command'],
            'registered_log_path': log_path, 'retained_sources': retained,
            'source_manifest_sha256': _sha(manifest_body), 'actions_sha256': _sha(actions_body),
            'action_sha256': _sha(json.dumps(action, sort_keys=True).encode()),
            'timeout_seconds': timeout + 180}


def _execute(command, *, env, cwd, timeout):
    """Exact shell command; timeout leaves an uncertain result, never a retry."""
    process = subprocess.Popen(['/bin/bash', '-c', command], cwd=cwd, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, errors='replace', start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # A daemon that escaped its group is not a confirmed terminal effect.
                process.stdout.close()
                output = exc.output or ''
        raise subprocess.TimeoutExpired(command, timeout, output=output) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout=output)


def _pause(root, hermes, record, backend, journal, reason):
    """Use the maintained writer and full-text CAS with one durable intent."""
    _command(root, hermes, record['entry'])
    prior = record['prior']
    _require(record['command'] == record['entry']['command']
             and prior['line'] == record['entry']['prior']['line']
             and prior['index'] == record['entry']['prior']['index']
             and prior['text'].splitlines()[prior['index']] == prior['line']
             and record['job_id'] == 'crontab:' + _sha(prior['line'].strip().encode())[:16]
             and set(record['retained_sources']) == set(SOURCES),
             'collector_hold_registration_invalid')
    old_journal = backend.journal
    backend.journal = journal
    try:
        with operation_lock(journal):
            _require(not journal.exists(), 'collector_hold_journal_already_exists')
            now = time.time()
            data = {'schema': SCHEMA, 'state': 'recovery_required', 'reason': reason,
                    'journal': str(journal), 'root': str(root), 'hermes': str(hermes),
                    'created_at': now, 'expires_at': now, 'keeper': identity(), 'locks': [],
                    'source_sha256': {str(root / rel): digest for rel, digest in record['retained_sources'].items()},
                    'entries': [{'entry': record['entry'], 'prior': record['prior'], 'intent': True,
                                 'paused': False, 'restore_intent': False, 'restored': False}]}
            atomic_json(journal, data)
            try:
                backend.pause(record['entry'], record['prior'])
                _require(backend.paused(record['entry'], record['prior']), 'collector_pause_readback_failed')
                data['entries'][0]['paused'] = True
                atomic_json(journal, data)
            except Exception as exc:
                data['error'] = str(exc)
                atomic_json(journal, data)
                raise
    finally:
        backend.journal = old_journal


def run_registered(root, hermes, record, output, backend=None, runner=None):
    root, hermes = Path(root).resolve(strict=True), Path(hermes).resolve(strict=True)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix='hourly-', dir=output))
    backend = backend or LinuxBackend(root, hermes, attempt / 'unused-journal.json')
    runner = runner or _execute
    code, status, captured, error = None, 'not_run', '', None
    started = time.time()
    try:
        _require(registration(root, hermes, backend=backend) == record, 'collector_registration_moved')
        environment = dict(os.environ)
        environment['CRON_BRIDGE_MANUAL_REGISTERED'] = '1'
        status = 'unknown'
        result = runner(record['command'], env=environment, cwd=str(root), timeout=record['timeout_seconds'])
        _require(isinstance(result.returncode, int), 'collector_returncode_missing')
        code = result.returncode
        captured = result.stdout or ''
        status = 'succeeded' if code == 0 else 'failed'
        _require(code == 0, 'collector_registered_run_failed')
        _require(registration(root, hermes, backend=backend) == record, 'collector_registration_moved_during_run')
    except subprocess.TimeoutExpired as exc:
        status, captured, error = 'timed_out', exc.output or '', 'collector_timeout_effect_uncertain_no_retry'
    except Exception as exc:
        error = str(exc)
    if isinstance(captured, bytes):
        captured = captured.decode(errors='replace')
    transcript = attempt / 'execution.json'
    run = {'job_id': record['job_id'], 'exit_code': code, 'status': status,
           'run_type': 'manual_registered', 'output_path': record['registered_log_path'],
           'transcript_path': str(transcript), 'output_scope': 'registered_shared_log_not_attributed',
           'started_at': started, 'finished_at': time.time()}
    transcript_error = None
    try:
        atomic_json(transcript, {**run, 'captured_process_output': captured, 'error': error})
        run['transcript_status'] = 'saved'
    except Exception as exc:
        # A lost receipt is not successful acceptance, even if execution exited
        # zero. Persistence failure must not bypass the independent hold path.
        transcript_error = 'collector_transcript_write_failed:' + type(exc).__name__
        run['transcript_status'] = 'unverified'
        error = (error + '; ' if error else '') + transcript_error
    result = {'success': error is None and code == 0, 'runs': [run], 'run_type': 'manual_registered'}
    if transcript_error:
        result['transcript_error'] = transcript_error
    if not result['success']:
        journal = attempt / 'failure-hold.json'
        result.update(error=error, pause_journal=str(journal))
        try:
            _pause(root, hermes, record, backend, journal, error or 'collector outcome unknown; no retry')
            result['paused'] = True
        except Exception as exc:
            result.update(paused=False, pause_error=str(exc))
    return result
