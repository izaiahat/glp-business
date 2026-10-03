"""Offline exact-checkout tests; synthetic mail, SQLite and queue only."""
from __future__ import annotations
import importlib.util
import json
import subprocess
from pathlib import Path
from unittest import mock
import pytest

ROOT = Path(__file__).resolve().parents[1]


def wrapper():
    spec = importlib.util.spec_from_file_location('cleanup_inbox_cron', ROOT / 'ops/hermes-cron-scripts/inbox_control_plane_cron.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_healthy_poll_with_already_represented_best_terms_lock_failure():
    w = wrapper()
    preflight = subprocess.CompletedProcess([], 0, 'PASS', '')
    poll = subprocess.CompletedProcess([], 0, json.dumps({'status': 'pass'}), '')
    failure = {'status': 'FAIL', 'rc': 1,
               'error': 'xentraffic_stale_or_foreign_singleton_lock',
               'failure_notification': {'status': 'PASS', 'readback_exact': True}}
    with mock.patch.object(w.subprocess, 'run', side_effect=[preflight, poll]), \
         mock.patch.object(w, 'run_best_terms_approval_trigger', return_value=failure):
        assert w.run_locked() == 0


@pytest.mark.parametrize('notice', [
    {}, {'status': 'PASS', 'readback_exact': False}, {'status': 'FAILED'}])
def test_unrepresented_continuation_failure_remains_failure(notice):
    w = wrapper()
    with mock.patch.object(w.subprocess, 'run', side_effect=[
        subprocess.CompletedProcess([], 0, 'PASS', ''),
        subprocess.CompletedProcess([], 0, '{"status":"pass"}', '')]), \
         mock.patch.object(w, 'run_best_terms_approval_trigger', return_value={
             'status': 'FAIL', 'rc': 1, 'failure_notification': notice}):
        assert w.run_locked() == 2


def test_duplicate_window_is_a_successful_continuation():
    w = wrapper()
    with mock.patch.object(w.subprocess, 'run', side_effect=[
        subprocess.CompletedProcess([], 0, 'PASS', ''),
        subprocess.CompletedProcess([], 0, '{"status":"pass"}', '')]), \
         mock.patch.object(w, 'run_best_terms_approval_trigger', return_value={
             'status': 'PASS_DUPLICATE_WINDOW', 'rc': 0}):
        assert w.run_locked() == 0


def test_poll_identity_is_forwarded_to_continuation():
    w = wrapper()
    with mock.patch.object(w.subprocess, 'run', side_effect=[
        subprocess.CompletedProcess([], 0, 'PASS', ''),
        subprocess.CompletedProcess([], 0, '{"status":"pass","run_id":"inboxrun_fixture"}', '')]), \
         mock.patch.object(w, 'run_best_terms_approval_trigger', return_value={
             'status': 'PASS_NO_NEW_APPROVALS', 'rc': 0}) as continuation:
        assert w.run_locked() == 0
        continuation.assert_called_once_with('inboxrun_fixture')
