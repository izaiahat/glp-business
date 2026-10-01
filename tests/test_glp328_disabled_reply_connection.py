"""Synthetic offline counterexamples, never a pilot or delivery receipt."""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import glp328_readonly_evidence as evidence

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
SENT = NOW - timedelta(days=8)
MS = str(int(SENT.timestamp() * 1000))
BOXES = [{'key': 'ops', 'address': 'ops@glpclinicscout.com'},
         {'key': 'izaiah', 'address': 'izaiah@glpclinicscout.com'}]


def record(mid, thread, when, labels, **headers):
    return {'id': mid, 'threadId': thread, 'internalDate': when, 'labelIds': labels,
            'payload': {'headers': [{'name': k.replace('_', '-'), 'value': v}
                                    for k, v in headers.items()]}}


INITIAL = record('initial-id', 'thread-id', MS, ['SENT'],
                 From='partners@glpclinicscout.com', To='bd@nu.example',
                 Reply_To='partners@glpclinicscout.com', Message_ID='<initial@glpclinicscout.com>')


def receipt(**changes):
    return {'kind': 'initial', 'sender': 'partners@glpclinicscout.com',
            'message_id': 'initial-id', 'provider_message_id': 'initial-id',
            'provider_thread_id': 'thread-id', 'recipient': 'bd@nu.example',
            'sent_utc': SENT.isoformat(), **changes}


def local(root, suppression=None):
    rows = suppression or []
    for name in ('targets', 'message_ledger', 'send_ledger', 'reply_ledger', 'suppression_list'):
        (root / (name + '.jsonl')).write_text(''.join(json.dumps(r) + '\n' for r in rows if name == 'suppression_list'))
    with sqlite3.connect(root / 'outreach.sqlite') as con:
        con.execute('create table if not exists targets (target_id text,url text)')
        con.execute('create table if not exists suppressions (suppression_id text,target_id text,email text,domain text,scope text,active integer)')


class Mail:
    def __init__(self, key, initial=INITIAL, events=(), fail_page=False):
        self.key, self.initial, self.events, self.fail_page = key, initial, events, fail_page
        self.calls = []
    def users(self): return self
    def messages(self): return self
    def get(self, **kw):
        self.calls.append(('get', kw))
        assert kw['format'] == 'metadata' and kw['userId'] == 'me'
        items = {r['id']: r for r in self.events}
        if self.key == 'ops':
            items[self.initial['id']] = self.initial
        return Response(items[kw['id']])
    def list(self, **kw):
        self.calls.append(('list', kw))
        assert kw['includeSpamTrash'] is True and kw['q'].startswith('after:')
        if kw.get('pageToken'):
            if self.fail_page:
                raise RuntimeError('offline page failure')
            return Response({'messages': [{'id': r['id']} for r in self.events]})
        ids = ([{'id': self.initial['id']}] if self.key == 'ops' else [])
        return Response({'messages': ids, 'nextPageToken': 'second'})


class Response:
    def __init__(self, value): self.value = value
    def execute(self): return self.value


def run(root, initial=INITIAL, events=(), receipt_value=None, suppression=None, fail_page=False, boxes=BOXES):
    local(root, suppression)
    services = {'ops': Mail('ops', initial, events, fail_page),
                'izaiah': Mail('izaiah')}
    result = evidence.decide_follow_up_current(receipt() if receipt_value is None else receipt_value,
                                                 [], root, boxes, lambda b: services[b['key']], now=NOW)
    return result, services


def test_exact_binding_all_pages_disabled_only(tmp_path):
    got, services = run(tmp_path)
    assert got['status'] == 'DISABLED_DECISION_ONLY'
    assert got['decision']['reason'] == 'follow_up_allowed_disabled'
    assert got['decision']['eligible'] is True
    assert got['decision']['sendable'] is False and got['decision']['send_permission'] is False
    assert got['send_enabled'] is False
    assert all(len([c for c in s.calls if c[0] == 'list']) == 2 for s in services.values())
    assert all(c[0] in {'get', 'list'} for s in services.values() for c in s.calls)


@pytest.mark.parametrize('headers,kind', [
    ({'From': 'bd@nu.example', 'To': 'partners@glpclinicscout.com'}, 'reply'),
    ({'From': 'mailer-daemon@nu.example', 'To': 'partners@glpclinicscout.com',
      'Subject': 'Delivery Status Notification'}, 'bounce'),
    ({'From': 'bd@nu.example', 'To': 'partners@glpclinicscout.com',
      'Subject': 'Unsubscribe'}, 'reply'),
])
def test_reply_bounce_optout_all_stop_even_independent_thread(tmp_path, headers, kind):
    event = record('adverse-id', 'other-thread', str(int((SENT + timedelta(days=1)).timestamp() * 1000)),
                   ['INBOX'], **headers)
    got, _ = run(tmp_path, events=[event])
    assert got['decision']['reason'] == 'stop_' + kind
    assert got['decision']['eligible'] is False


def test_local_optout_and_one_followup_limit(tmp_path):
    got, _ = run(tmp_path, suppression=[{'email': 'optout@nu.example', 'scope': 'email'}])
    assert got['decision']['reason'] == 'stop_opt_out'
    local(tmp_path)
    services = {'ops': Mail('ops'), 'izaiah': Mail('izaiah')}
    got = evidence.decide_follow_up_current(receipt(), [{'recipient': 'bd@nu.example'}],
                                            tmp_path, BOXES, lambda b: services[b['key']], now=NOW)
    assert got['decision']['reason'] == 'follow_up_limit_reached'


@pytest.mark.parametrize('change', [
    {'threadId': 'other-thread'}, {'labelIds': ['INBOX']},
    {'payload': {'headers': [{'name': 'From', 'value': 'other@example.com'}]}},
])
def test_wrong_or_incomplete_initial_refuses(tmp_path, change):
    got, _ = run(tmp_path, initial={**INITIAL, **change})
    assert got['decision'] is None and got['status'] == 'REFUSED_INCOMPLETE_SOURCES'


def test_caller_verified_flag_cannot_override_wrong_recipient_or_time(tmp_path):
    for changed in ({'recipient': 'other@nu.example', 'verified': True,
                     'checks': {'all_passed': True}},
                    {'sent_utc': (SENT - timedelta(days=1)).isoformat(), 'verified': True}):
        got, _ = run(tmp_path, receipt_value=receipt(**changed))
        assert got['decision'] is None


def test_incomplete_global_page_or_unknown_bounce_refuses(tmp_path):
    got, _ = run(tmp_path, fail_page=True)
    assert got['decision'] is None
    unknown = record('unknown-bounce', 'other-thread', str(int((SENT + timedelta(days=1)).timestamp() * 1000)),
                     ['INBOX'], From='mailer-daemon@google.com', To='ops@glpclinicscout.com',
                     Subject='Delivery Status Notification')
    got, _ = run(tmp_path, events=[unknown])
    assert got['decision'] is None


def test_missing_local_source_or_mailbox_refuses_before_mail(tmp_path):
    got, _ = run(tmp_path, boxes=BOXES[:1])
    assert got['decision'] is None
    local(tmp_path)
    (tmp_path / 'suppression_list.jsonl').unlink()
    calls = []
    got = evidence.decide_follow_up_current(receipt(), [], tmp_path, BOXES,
                                             lambda b: calls.append(b), now=NOW)
    assert got['reason'] == 'suppression_snapshot_incomplete' and calls == []


def test_conflicting_singleton_initial_identity_refuses(tmp_path):
    first = {**INITIAL, 'payload': {'headers': [
        {'name': 'From', 'value': 'conflicting@example.net'},
        *INITIAL['payload']['headers']]}}
    got, _ = run(tmp_path, initial=first)
    assert got['status'] == 'REFUSED_INCOMPLETE_SOURCES' and got['decision'] is None


@pytest.mark.parametrize('headers', [
    {'From': 'route@network.example', 'To': 'elsewhere@network.example',
     'Cc': 'partners@glpclinicscout.com'},
    {},
    {'From': '<>', 'To': 'undisclosed-recipients:;'},
])
def test_scoped_unresolved_routing_refuses_zero_event_completeness(tmp_path, headers):
    event = record('unresolved-id', 'other-thread',
                   str(int((SENT + timedelta(days=1)).timestamp() * 1000)),
                   ['INBOX'], **headers)
    got, services = run(tmp_path, events=[event])
    assert got['status'] == 'REFUSED_INCOMPLETE_SOURCES' and got['decision'] is None
    assert any('Cc' in call[1]['metadataHeaders'] for call in services['ops'].calls if call[0] == 'get')
    if not headers:
        bound = {**event, 'threadId': 'thread-id'}
        got, _ = run(tmp_path, events=[bound])
        assert got['decision']['reason'] == 'stop_reply'


def test_distinct_related_adverse_at_initial_millisecond_stops_reply(tmp_path):
    event = record('equal-time-adverse', 'other-thread', MS, ['INBOX'],
                   From='bd@nu.example', To='partners@glpclinicscout.com')
    got, _ = run(tmp_path, events=[event])
    assert got['status'] == 'DISABLED_DECISION_ONLY'
    assert got['decision']['reason'] == 'stop_reply'
    assert got['decision']['eligible'] is False and got['decision']['sendable'] is False
    unrelated = record('older-unrelated', 'other-thread', str(int(MS) - 1), ['INBOX'],
                       From='stranger@unrelated.example', To='ops@glpclinicscout.com')
    older, _ = run(tmp_path, events=[unrelated])
    assert older['decision']['reason'] == 'follow_up_allowed_disabled'
