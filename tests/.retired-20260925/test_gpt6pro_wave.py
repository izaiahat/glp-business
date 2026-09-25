"""Offline regressions for long-lived account-scoped wave admission."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

WAVE_PATH = Path(__file__).resolve().parents[1] / 'scripts/gpt6pro_wave.py'
sys.path.insert(0, str(WAVE_PATH.parent))
spec = importlib.util.spec_from_file_location('wave_admission_test_subject', WAVE_PATH)
wave = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wave)


def test_stale_runner_reloads_registry_after_terminal_failure(monkeypatch):
    """A process started before the THINKING_FAILED fix must not wait forever."""
    stale = [{'account': 'acct1', 'job': f'failed-{i}'} for i in range(3)]
    current = [{'account': 'acct2', 'job': 'generating'}]
    monkeypatch.setattr(wave.relay, 'active_registry', lambda: stale)
    def refresh(module):
        monkeypatch.setattr(module, 'active_registry', lambda: current)
        return module
    monkeypatch.setattr(wave.importlib, 'reload', refresh)
    assert wave.has_capacity('acct1', {'max_concurrent_chats_per_ip': 6,
                                        'max_concurrent_chats_per_account': 3})
