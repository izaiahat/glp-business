"""Persistent S3 supply and bounded 2/account scheduling; no live effects."""
import importlib.util
import json
from pathlib import Path
import sys
from contextlib import nullcontext

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('s3_route', ROOT / 'scripts/pro_route.py')
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    monkeypatch.setattr(m, 'ROOT', tmp_path)
    monkeypatch.setattr(m.relay, 'BUSINESS', tmp_path)
    monkeypatch.setattr(m.relay, 'JOBS', tmp_path / 'jobs')
    monkeypatch.setattr(m.packet, 'BUSINESS', tmp_path)
    monkeypatch.setattr(m.packet, 'JOBS', tmp_path / 'jobs')
    monkeypatch.setattr(m.relay, 'load_config', lambda: {'ledger': 'ledger.jsonl', 'envelope': {'poll_stagger_s': 20}})
    return m


def test_backlog_survives_restart_without_duplicate_packet(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    d = m.directory() / 'backlog'; d.mkdir(parents=True)
    p = d / '001-GLP-334-acct1.md'; p.write_text('Closed-period join design only; no live effects.')
    first = m.ingest_backlog()
    assert len(first) == 1
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    m = load(tmp_path, monkeypatch)
    assert m.ingest_backlog() == []
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    p.write_text('Replacement bytes must never become an implicit second request.')
    import pytest
    with pytest.raises(ValueError, match='BACKLOG_DRIFT'):
        m.ingest_backlog()
    assert len(m.request_rows()) == 1


def test_two_per_account_even_if_legacy_config_says_three(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    cfg = {'accounts': [{'label': str(i), 'cdp_port': i} for i in range(3)],
           'envelope': {'max_concurrent_chats_per_account': 3, 'max_concurrent_chats_per_ip': 6}}
    monkeypatch.setattr(m.relay, 'load_config', lambda: cfg)
    monkeypatch.setattr(m.relay, 'active_registry', lambda: [{'account': '0'}] * 2)
    monkeypatch.setattr(m.relay, 'sibling_bindings', lambda *a: [])
    monkeypatch.setattr(m.relay, 'budget_check', lambda *a: (0, 200))
    monkeypatch.setattr(m.socket, 'create_connection', lambda *a, **k: nullcontext())
    assert m.choose_account('acct1') is None
    assert m.choose_account('auto') in ('acct2', 'acct3')


def test_vacant_slot_refills_before_old_running_polls(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    for i in range(6):
        m.save({'job': str(i), 'state': 'running', 'created_at': 'a', 'attempts': []})
    m.save({'job': 'new', 'state': 'queued', 'created_at': 'z', 'requested_account': 'auto', 'attempts': []})
    monkeypatch.setattr(m.relay, 'job_state', lambda job: {} if job == 'new' else {'status': 'running'})
    monkeypatch.setattr(m, 'choose_account', lambda requested: 'acct2')
    monkeypatch.setattr(m.time, 'sleep', lambda seconds: None)
    calls = []
    monkeypatch.setattr(m, 'effect', lambda row, action: calls.append((row['job'], action)) or {'rc': 3})
    m.dispatch(max_actions=1)
    assert calls == [('new', 'submit')]


def test_polls_rotate_persistently_across_bounded_restarts(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    for i in range(6):
        m.save({'job': str(i), 'state': 'running', 'created_at': 'a', 'attempts': []})
    monkeypatch.setattr(m.relay, 'job_state', lambda job: {'status': 'running'})
    monkeypatch.setattr(m.time, 'sleep', lambda seconds: None)
    calls = []
    monkeypatch.setattr(m, 'effect', lambda row, action: calls.append(row['job']) or {'rc': 3})
    m.dispatch(max_actions=2)
    m.dispatch(max_actions=2)
    m.dispatch(max_actions=2)
    assert len(set(calls)) == 6
