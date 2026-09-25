"""Dispatch-route regressions; only disposable inert tmux panes are used."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

import pytest

from test_tui_transport import pane, _conformant_brief, send, tmux
from brief_model_effort import route_instruction
import brief_lint
import seat_send


@pytest.mark.parametrize(('remove', 'expected'), [
    ('MODEL: gpt-6-sol\n', 'MODEL:'),
    ('PROVIDER: openai-codex\n', 'PROVIDER:'),
    ('REASONING: medium\n', 'REASONING:'),
    ('> For file surfaces run `python3 scripts/skill_router.py --paths <the files you will touch>`.\n',
     'skill_router.py --paths'),
    ('Delegate independent reasoning and disjoint file work through host-shared admission; verify effective routes.',
     'delegation instruction'),
])
def test_each_missing_route_clause_refuses_before_typing(pane, tmp_path, remove, expected):
    brief = _conformant_brief(tmp_path)
    brief.write_text(brief.read_text().replace(remove, ''))
    assert any(expected in defect for defect in brief_lint.lint(brief))
    out = send(pane, brief, issue='GLP-30')
    assert out.returncode == 2 and pane['received'].read_bytes() == b''


def test_invalid_effort_and_duplicate_model_refuse(pane, tmp_path):
    brief = _conformant_brief(tmp_path)
    for variant in ('REASONING: impossible', 'REASONING: high.'):
        brief.write_text(brief.read_text().replace('REASONING: medium', variant))
        assert send(pane, brief, issue='GLP-30').returncode == 2
        brief.write_text(brief.read_text().replace(variant, 'REASONING: medium'))
    brief.write_text(brief.read_text().replace('PROVIDER: openai-codex',
                                                    'MODEL: gpt-6-other\nPROVIDER: openai-codex'))
    assert send(pane, brief, issue='GLP-30').returncode == 2
    assert pane['received'].read_bytes() == b''


def test_mismatch_corrects_model_then_effort_before_brief(pane, tmp_path):
    brief = _conformant_brief(tmp_path)
    brief.write_text(brief.read_text().replace('MODEL: gpt-6-sol', 'MODEL: gpt-6-astra-900k')
                                          .replace('REASONING: medium', 'REASONING: high'))
    result = send(pane, brief, issue='GLP-30')
    assert result.returncode == 3, result.stdout + result.stderr
    time.sleep(.2)
    assert pane['received'].read_bytes() == (
        b'/model gpt-6-astra-900k --provider openai-codex\r/reasoning high\r' +
        (f'READ {brief} — GLP-30' + route_instruction('gpt-6-astra-900k', 'high')).encode() + b'\r')


def test_already_aligned_sends_pointer_without_slash_commands(pane, tmp_path):
    brief = _conformant_brief(tmp_path)
    result = send(pane, brief, issue='GLP-30')
    assert result.returncode == 3, result.stdout + result.stderr
    assert pane['received'].read_bytes() == (
        f'READ {brief} — GLP-30' + route_instruction('gpt-6-sol', 'medium')).encode() + b'\r'


def test_busy_brief_waits_for_idle_without_interrupt(pane, tmp_path):
    import threading
    brief = _conformant_brief(tmp_path)
    tty = tmux('display-message', '-p', '-t', pane['pane'], '#{pane_tty}')
    with open(tty, 'w') as screen:
        screen.write('\x1b[2J\x1b[H ❯ \r\n─ working │ gpt 6 sol medium │ 1 session\r\nCtrl+C to interrupt\r\n')
    def become_idle():
        with open(tty, 'w') as screen:
            screen.write('\x1b[2J\x1b[H ❯ \r\n─ ready │ gpt 6 sol medium │ 1 session\r\n')
    timer = threading.Timer(.5, become_idle)
    timer.start()
    try:
        result = send(pane, brief, issue='GLP-30')
    finally:
        timer.join()
    assert result.returncode == 3, result.stdout + result.stderr
    assert pane['received'].read_bytes().startswith(f'READ {brief}'.encode())


def test_unverified_model_command_refuses_before_pointer(pane, tmp_path, monkeypatch):
    brief = _conformant_brief(tmp_path)
    brief.write_text(brief.read_text().replace('MODEL: gpt-6-sol', 'MODEL: gpt-6-astra-900k'))
    pane['ignore_route'].touch()
    monkeypatch.setenv('TUI_SEND_ROUTE_WAIT_S', '.7')
    result = send(pane, brief, issue='GLP-30')
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'correction unverified' in result.stdout
    assert pane['received'].read_bytes() == b'/model gpt-6-astra-900k --provider openai-codex\r'


def test_unreadable_status_refuses_without_guessing_or_typing(pane, tmp_path):
    brief = _conformant_brief(tmp_path)
    tty = tmux('display-message', '-p', '-t', pane['pane'], '#{pane_tty}')
    with open(tty, 'w') as screen:
        screen.write('\x1b[2J\x1b[H ❯ \r\nno native route visible\r\n')
    result = send(pane, brief, issue='GLP-30')
    assert result.returncode == 2 and pane['received'].read_bytes() == b''


def test_operator_directed_skips_only_cached_acceptance(pane, tmp_path):
    brief = _conformant_brief(tmp_path)
    text = brief.read_text().replace('LINEAR: GLP-30', 'LINEAR: GLP-999999')
    brief.write_text(text)
    args = ['bash', str(Path(__file__).resolve().parents[1] / 'scripts/tui_send.sh'),
            pane['pane'], pane['tmux_session'], pane['session'], str(brief), 'GLP-999999']
    normal = subprocess.run(args, capture_output=True, text=True)
    assert normal.returncode == 2 and pane['received'].read_bytes() == b''
    directed = subprocess.run(args[:2] + ['--operator-directed'] + args[2:],
                              capture_output=True, text=True)
    assert directed.returncode == 3 and 'operator-directed' in directed.stderr
    assert pane['received'].read_bytes().startswith(f'READ {brief}'.encode())


def test_operator_directed_still_requires_route_skills_delegation(pane, tmp_path):
    brief = _conformant_brief(tmp_path)
    args = ['bash', str(Path(__file__).resolve().parents[1] / 'scripts/tui_send.sh'),
            '--operator-directed', pane['pane'], pane['tmux_session'], pane['session'],
            str(brief), 'GLP-30']
    for fragment in ('MODEL: gpt-6-sol\n', 'REASONING: medium\n',
                     'python3 scripts/skill_router.py --paths',
                     'Delegate independent reasoning and disjoint file work'):
        original = brief.read_text()
        brief.write_text(original.replace(fragment, ''))
        out = subprocess.run(args, capture_output=True, text=True)
        assert out.returncode == 2 and pane['received'].read_bytes() == b''
        brief.write_text(original)


def test_wrapper_refuses_wrong_issue_before_transport(tmp_path, monkeypatch):
    brief = tmp_path / 'brief.md'
    brief.write_text('LINEAR: GLP-2\n')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'seats': {'scout': {'issue': 'GLP-1', 'pane': '%1',
                                                       'tmux': '$1', 'session': 'sid'}}}))
    monkeypatch.setattr(seat_send, 'CONFIG', config)
    assert seat_send.send('scout', brief) == 2


def test_wrapper_calls_registered_transport_and_requires_native_busy(tmp_path, monkeypatch):
    brief = tmp_path / 'brief.md'
    brief.write_text('LINEAR: GLP-1\n')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'seats': {'scout': {'issue': 'GLP-1', 'pane': '%1',
                                                       'tmux': '$1', 'session': 'sid'}}}))
    monkeypatch.setattr(seat_send, 'CONFIG', config)
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == 'bash':
            return subprocess.CompletedProcess(cmd, 3, 'SUBMITTED_UNCONFIRMED', '')
        return subprocess.CompletedProcess(cmd, 0, '─ musing │ gpt 6 sol medium │ 1 session\nCtrl+C to interrupt\n', '')
    monkeypatch.setattr(seat_send.subprocess, 'run', run)
    assert seat_send.send('scout', brief, operator_directed=True) == 0
    assert calls[0] == ['bash', str(seat_send.TRANSPORT), '--operator-directed', '%1', '$1', 'sid',
                        str(brief), 'GLP-1']
    assert calls[1][:2] == ['tmux', 'capture-pane']
