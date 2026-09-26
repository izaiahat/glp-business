import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('notice', Path(__file__).resolve().parents[1] / 'scripts/glp_earnings_notice.py')
assert SPEC is not None and SPEC.loader is not None
notice = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notice)


def event(**changes):
    e = dict(network='affiliati', account_id='15309', tenant_id='not_applicable', program_id='42896',
             provider_event_id='real-1', revision_key='a' * 64, receipt_sha256='b' * 64,
             event_type='commission', mapped_status='approved', authenticated=True,
             classification='REAL', attribution='GLP', commission_minor=1250, currency='USD')
    e.update(changes)
    return e


def test_real_once_and_replay(tmp_path):
    calls = []
    def sender(method, path, payload=None):
        calls.append((method, path, payload))
        return {'ok': True, 'json': {'id': '123456789', 'content': notice.card(event())}}
    p = tmp_path / 'journal.json'
    first = notice.deliver(event(), p, sender)
    second = notice.deliver(event(), p, sender)
    assert first['state'] == 'SENT' and second['state'] == 'ALREADY_SENT'
    assert len(calls) == 2 and calls[0][0] == 'POST' and calls[1][0] == 'GET'
    assert calls[0][1] == f'/channels/{notice.PAYMENTS}/messages'
    assert 'REAL' in calls[0][2]['content'] and '1250' not in calls[0][2]['content']
    assert calls[0][2]['allowed_mentions'] == {'parse': []}


@pytest.mark.parametrize('changes', [
    {'commission_minor': None}, {'commission_minor': 0}, {'classification': 'UNKNOWN'},
    {'authenticated': False}, {'mapped_status': 'unknown'}, {'mapped_status': 'reversed'},
    {'attribution': 'UNKNOWN'}, {'event_type': 'settlement'}, {'currency': None},
])
def test_unknown_zero_and_unverified_never_send(tmp_path, changes):
    def forbidden(*args):
        raise AssertionError('transport was called')
    p = tmp_path / 'journal.json'
    assert notice.deliver(event(**changes), p, forbidden)['state'] == 'SUPPRESSED_UNVERIFIED_OR_NONPOSITIVE'
    assert not p.exists()


def test_failure_queued_no_decisions_and_exact_reconciliation(tmp_path):
    calls = []
    def failure(method, path, payload=None):
        calls.append((method, path))
        return {'ok': False, 'json': {}}
    p = tmp_path / 'journal.json'
    assert notice.deliver(event(), p, failure)['state'] == 'QUEUED_UNCERTAIN'
    assert json.loads(p.read_text())['real:' + 'a' * 64]['state'] == 'uncertain'
    assert notice.deliver(event(), p, failure)['state'] == 'QUEUED_UNCERTAIN'
    assert calls == [('POST', f'/channels/{notice.PAYMENTS}/messages'),
                     ('GET', f'/channels/{notice.PAYMENTS}/messages?limit=100')]
    content = notice.card(event())
    assert notice.deliver(event(), p, lambda *a: {'ok': True, 'json': [{'id': '123456789', 'content': content}]})['state'] == 'RECONCILED_SENT'
    assert notice.deliver(event(), p, failure)['state'] == 'ALREADY_SENT'


def test_synthetic_test_explicit_and_separate_from_real(tmp_path):
    e = event(classification='TEST', commission_minor=None, mapped_status='unknown')
    p = tmp_path / 'journal.json'
    assert notice.deliver(e, p, None)['state'] == 'SUPPRESSED_UNVERIFIED_OR_NONPOSITIVE'
    dry = notice.deliver(e, p, None, test=True, dry_run=True)
    assert dry['state'] == 'DRY_RUN' and 'TEST — synthetic' in dry['content']
    assert 'Commission: unknown' in dry['content'] and not p.exists()
    def sender(method, path, payload=None):
        return {'ok': True, 'json': {'id': '123456789', 'content': notice.card(e, test=True)}}
    result = notice.deliver(e, p, sender, test=True)
    assert result['state'] == 'SENT'
    assert 'test:' + 'a' * 64 in json.loads(p.read_text())
