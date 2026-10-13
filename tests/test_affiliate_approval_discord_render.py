"""Effect-denied renderer regression against unchanged cancellation producer."""
import ast
import copy
import re
from pathlib import Path
from types import SimpleNamespace

from ops import discord_fmt
import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/affiliate_approval_auto_reconciler.py'


def load_functions():
    names = {'render_discord', 'reconcile_campaign_cancellation'}
    nodes = [n for n in ast.parse(SOURCE.read_text()).body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == names
    namespace = {'re': re, 'discord_fmt': discord_fmt, 'REPORT_DIR': Path('private-receipts')}
    exec(compile(ast.Module(nodes, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return SimpleNamespace(**namespace), namespace


def test_unverified_cancellation_renders_without_changing_effects():
    module, namespace = load_functions()
    receipts, states = [], []
    namespace.update(
        verify_affiliate_authentication=lambda *args: {'status': 'PASS'},
        save_receipt=lambda event_id, receipt: receipts.append(copy.deepcopy(receipt)) or Path('receipt.json'),
        mark_state=lambda *args, **kwargs: states.append((args, kwargs)),
    )

    class Denied:
        def __getattr__(self, name):
            raise AssertionError('Provider/Sheet/mailbox access forbidden')

    event = {'event_id': 'evt_cancellation'}
    result = module.reconcile_campaign_cancellation(event, {}, Denied(), Denied(), None, False)
    assert result == {'status': 'BLOCKED', 'event_id': event['event_id'], 'receipt': 'receipt.json',
                      'disposition': 'requires_provider_verification', 'changes': []}
    before = copy.deepcopy(result)
    message = module.render_discord([result])
    assert 'requires_provider_verification' in message
    assert result == before
    assert receipts[0]['sheet_write_count'] == 0
    assert receipts[0]['outbound_send'] is False
    assert receipts[0]['action_completed'] is False
    assert states[0][1]['blocker'] == 'requires_provider_verification'
    assert discord_fmt.lint(message, output_class='alert')['status'] == 'pass'


@pytest.mark.parametrize('fields, expected', [
    ({'blocker': 'legal_terms_capture', 'disposition': 'other'}, 'legal_terms_capture'),
    ({'disposition': 'requires_provider_verification'}, 'requires_provider_verification'),
    ({}, 'blocker details unavailable'),
])
def test_blocked_renderer_preserves_reason_or_explicit_unknown(fields, expected):
    module, _ = load_functions()
    output = {'status': 'BLOCKED', 'event_id': 'evt_blocked', **fields}
    before = copy.deepcopy(output)
    message = module.render_discord([output])
    assert expected in message
    assert output == before
    assert discord_fmt.lint(message, output_class='alert')['status'] == 'pass'


def test_pass_only_remains_silent():
    module, _ = load_functions()
    assert module.render_discord([{'status': 'PASS', 'event_id': 'evt_pass'}]) == ''
