#!/usr/bin/env python3
"""One manual invocation of registered no-agent scripts, with native fire fences.

The native script runner owns interpreter selection, environment sanitization,
output redaction and timeout process-tree cleanup. It does not deliver messages.
Its output omits successful stderr; timeout/launch failures have no exit code.
"""
from contextlib import ExitStack
import copy
import hashlib
import json
from pathlib import Path
import re
import stat
import tempfile


class RuntimeRefused(RuntimeError):
    pass


def _job(hermes, job_id):
    jobs = json.loads((hermes / 'cron/jobs.json').read_bytes())['jobs']
    hits = [job for job in jobs if job.get('id') == job_id]
    if len(hits) != 1:
        raise RuntimeRefused('registered_job_identity_not_unique:' + job_id)
    return hits[0]


def _script(hermes, job):
    if (job.get('no_agent') is not True or job.get('fire_claim') is not None
            or job.get('run_claim') is not None or job.get('running')):
        raise RuntimeRefused('registered_job_not_idle_script:' + job['id'])
    value = job.get('script')
    if not isinstance(value, str) or not value or '\x00' in value:
        raise RuntimeRefused('registered_script_invalid')
    directory = hermes / 'scripts'
    raw = Path(value)
    path = raw if raw.is_absolute() else directory / raw
    # Do not allow symlink aliases even when their destination stays in scope.
    if not path.is_relative_to(directory) or '..' in path.parts:
        raise RuntimeRefused('registered_script_outside_profile')
    for member in (path, *path.parents):
        if member == hermes:
            break
        if member.is_symlink():
            raise RuntimeRefused('registered_script_symlink')
    metadata = path.stat()
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISREG(metadata.st_mode) or mode & 0o7022:
        raise RuntimeRefused('registered_script_unsafe_mode')
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if job.get('script_sha256') != digest:
        raise RuntimeRefused('registered_script_pin_mismatch')
    after = path.stat()
    signature = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, mode)
    if signature != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, stat.S_IMODE(after.st_mode)):
        raise RuntimeRefused('registered_script_moved')
    return path, signature, digest


def _native_runner(job):
    from cron.scheduler_script import _run_job_script
    return _run_job_script(job['script'], workdir=job.get('workdir'), job=job)


def _outcome(success, output):
    if success is True:
        return 0, 'succeeded'
    match = re.match(r'^Script exited with code (-?\d+)(?:\n|$)', output)
    if match:
        code = int(match.group(1))
        return (code if code != 0 else None), 'failed'
    if output.startswith('Script timed out after '):
        return None, 'timed_out'
    return None, 'unknown'


def _pause_group(scheduler, hermes, job_ids, reason):
    errors = []
    for job_id in job_ids:
        try:
            _job(hermes, job_id)  # Never let a native prefix lookup pick another job.
            scheduler.pause_job(job_id, reason=reason)
            job = _job(hermes, job_id)
            if job.get('enabled') is not False or job.get('state') != 'paused':
                raise RuntimeRefused('pause_readback_failed')
        except Exception as exc:
            errors.append({'job_id': job_id, 'error': str(exc)})
    return errors


def run_registered(root, hermes, job_ids, output_dir, *, scheduler=None, runner=None):
    """Run the paired cohort once; resume only after every run succeeds.

    ``runner(job)`` is an offline-test injection returning the native
    ``(success, formatted_output)`` pair. A failure never retries an effect.
    All cohort fire locks stay held through execution and pause/resume readback.
    """
    root, hermes = Path(root).resolve(strict=True), Path(hermes).resolve(strict=True)
    job_ids = tuple(job_ids)
    if (not job_ids or len(set(job_ids)) != len(job_ids)
            or any(not isinstance(jid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', jid) for jid in job_ids)):
        raise RuntimeRefused('invalid_registered_job_selection')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix='registered-', dir=output_dir))
    if scheduler is None:
        from maintenance_window import LinuxBackend
        scheduler = LinuxBackend(root, hermes, attempt / 'runtime.json').scheduler()
    if Path(scheduler._current_cron_store().jobs_file).resolve() != hermes / 'cron/jobs.json':
        raise RuntimeRefused('wrong_scheduler_store')
    runner = _native_runner if runner is None else runner
    runs = []
    attempted = set()
    current = job_ids[0]
    try:
        with ExitStack() as fences:
            try:
                for current in sorted(job_ids):
                    if not fences.enter_context(scheduler._fire_job_lock(current)):
                        raise RuntimeRefused('native_fire_fence_unavailable:' + current)
                originals, signatures = {}, {}
                for current in job_ids:
                    originals[current] = _job(hermes, current)
                    signatures[current] = _script(hermes, originals[current])
                for current in job_ids:
                    job = _job(hermes, current)
                    if job != originals[current] or _script(hermes, job) != signatures[current]:
                        raise RuntimeRefused('registered_job_moved_before_run:' + current)
                    attempted.add(current)
                    success, output = runner(copy.deepcopy(job))
                    if not isinstance(success, bool) or not isinstance(output, str):
                        raise RuntimeRefused('native_runner_result_invalid')
                    code, status = _outcome(success, output)
                    output_path = attempt / (current + '.log')
                    output_path.write_text(output)
                    output_path.chmod(0o600)
                    runs.append({'job_id': current, 'exit_code': code, 'status': status,
                                 'run_type': 'manual_registered', 'output_path': str(output_path)})
                    if not success:
                        raise RuntimeRefused('registered_run_' + status + ':' + current)
                    if _job(hermes, current) != job or _script(hermes, job) != signatures[current]:
                        raise RuntimeRefused('registered_job_moved_during_run:' + current)
                # Recheck the complete cohort before enabling its first member.
                for current in job_ids:
                    if (_job(hermes, current) != originals[current]
                            or _script(hermes, originals[current]) != signatures[current]):
                        raise RuntimeRefused('registered_cohort_moved_before_resume:' + current)
                for current in job_ids:
                    scheduler.resume_job(current)
                    actual = _job(hermes, current)
                    if actual.get('enabled') is not True or actual.get('state') != 'scheduled':
                        raise RuntimeRefused('resume_readback_failed:' + current)
                return {'success': True, 'runs': runs, 'run_type': 'manual_registered'}
            except Exception as exc:
                reason = 'glp_ship registered run failed; no retry: ' + str(exc)
                errors = _pause_group(scheduler, hermes, job_ids, reason)
                if not runs or runs[-1]['job_id'] != current:
                    output_path = attempt / (current + '.log')
                    output_path.write_text(reason)
                    output_path.chmod(0o600)
                    runs.append({'job_id': current, 'exit_code': None,
                                 'status': 'unknown' if current in attempted else 'not_run',
                                 'run_type': 'manual_registered', 'output_path': str(output_path)})
                return {'success': False, 'runs': runs, 'error': str(exc),
                        'pause_errors': errors, 'run_type': 'manual_registered'}
    except Exception:
        # Failure while releasing a native fence is not a successful run receipt.
        _pause_group(scheduler, hermes, job_ids, 'glp_ship native fence release failed; reconcile before retry')
        raise
