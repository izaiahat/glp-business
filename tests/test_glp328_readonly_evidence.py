"""Only synthetic files/mail services; no live writers or credentials."""
import importlib
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


def fixture(root):
    targets = [{'target_id': 'fixture-t', 'url': 'https://alpha.example',
                'public_email': 'patients@alpha.example'}]
    controls = [{'target_id': 'fixture-t', 'scope': 'target', 'active': True},
                {'email': 'bd@beta.example', 'scope': 'email', 'active': True}]
    for name, rows in [('targets', targets), ('suppression_list', controls),
                       ('message_ledger', []), ('send_ledger', []), ('reply_ledger', [])]:
        (root / (name + '.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in rows))
    with sqlite3.connect(root / 'outreach.sqlite') as con:
        con.execute('create table targets (target_id text, url text)')
        con.execute('insert into targets values (?,?)', ('fixture-t', 'https://alpha.example'))
        con.execute('create table suppressions (suppression_id text,target_id text,email text,domain text,scope text,active integer)')
        con.execute('insert into suppressions values (?,?,?,?,?,?)',
                    ('fixture-s', 'fixture-t', '', '', 'organization', 1))


def test_real_file_projection_preserves_union_without_writes_or_contacts(tmp_path):
    fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert importlib.util.find_spec('glp328_readonly_evidence') is not None, 'read-only projection missing'
    adapter = importlib.import_module('glp328_readonly_evidence')
    local = adapter.read_local_sources(tmp_path, NOW)
    assert local['suppression']['complete'] is True
    assert {e['domain'] for e in local['suppression']['entries']} == {'alpha.example', 'beta.example'}
    assert local['sources']['sqlite']['query_only'] == 1
    assert local['sources']['sqlite']['uri_mode'] == 'ro'
    assert local['sources']['targets.jsonl']['rows'] == 1
    assert 'contacts' not in local  # identity records never become contacts
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    # Actual legacy formats: display-name From and global forbidden prefixes.
    with (tmp_path / 'suppression_list.jsonl').open('a') as handle:
        handle.write(json.dumps({'email': 'privacy@*', 'domain': '*'})+'\n')
    (tmp_path / 'reply_ledger.jsonl').write_text(json.dumps({
        'target_id': 'fixture-t', 'from_addr': 'Fixture Business <bd@alpha.example>'})+'\n')
    legacy = adapter.read_local_sources(tmp_path, NOW)
    assert legacy['suppression']['complete'] is True, legacy['faults']
    assert legacy['policy_prefix_controls'] == [{'source': 'suppression_list.jsonl',
                                               'position': 3, 'prefix': 'privacy'}]
    # An unknown wildcard cannot silently ride on the existing prefix policy.
    with (tmp_path / 'suppression_list.jsonl').open('a') as handle:
        handle.write(json.dumps({'email': 'newprefix@*', 'domain': '*'})+'\n')
    assert adapter.read_local_sources(tmp_path, NOW)['suppression']['complete'] is False


def test_incomplete_sources_refuse_actual_planner_without_mail_calls(tmp_path):
    fixture(tmp_path)
    adapter = importlib.import_module('glp328_readonly_evidence')
    from affiliate_sheet_schema_guard import EXPECTED_HEADERS
    values = [list(EXPECTED_HEADERS)]
    def forbidden(*args):
        raise AssertionError('mail read after failed local preflight')
    assert hasattr(adapter, 'plan_current'), 'current snapshot-to-planner connection missing'
    # One acceptance behavior: missing/malformed/unresolved input never becomes
    # an empty complete snapshot. Keep an adverse resolvable domain as control.
    for content in (None, '{broken', json.dumps({'scope':'domain', 'domain':'beta.example', 'active':0.0}),
                    json.dumps({'scope':'target','target_id':'unknown', 'domain':'beta.example'})):
        p = tmp_path / 'suppression_list.jsonl'
        if content is None:
            p.unlink()
        else:
            p.write_text(content+'\n')
        result = adapter.plan_current(values, tmp_path, [], forbidden, now=NOW)
        assert result['status'] == 'REFUSED_INCOMPLETE_SOURCES'
        assert result['planner_refusal'] == 'suppression_snapshot_incomplete'
        assert result['plan'] is None and result['send_enabled'] is False
        assert result['mailbox_coverage']['status'] == 'NOT_ATTEMPTED_LOCAL_REFUSAL'
        assert result['local']['suppression']['complete'] is False
        assert result['local']['faults']


class Mail:
    """Only the two permitted Gmail methods exist; pages are explicit fixtures."""
    def __init__(self, fail_second=False):
        self.calls = []
        self.fail_second = fail_second
    def users(self): return self
    def messages(self): return self
    def list(self, **kw):
        self.calls.append(('list', kw))
        assert kw['includeSpamTrash'] is True and 'after:' not in kw['q']
        page = kw.get('pageToken')
        if page and self.fail_second:
            raise RuntimeError('fixture page outage')
        data = ({'messages': [{'id': 'fixture-mail-1'}], 'nextPageToken': 'two'} if not page
                else {'messages': [{'id': 'fixture-mail-2'}]})
        return type('Response', (), {'execute': lambda self: data})()
    def get(self, **kw):
        self.calls.append(('get', kw))
        assert kw['format'] == 'metadata'
        data = {'id': kw['id'], 'threadId': 'fixture-thread', 'internalDate': '1790640000000',
                'labelIds': ['SENT'] if kw['id'] == 'fixture-sent' else ['INBOX'],
                'payload': {'headers': [{'name': 'From', 'value': 'bd@gamma.example'},
                                        {'name': 'To', 'value': 'partners@glpclinicscout.com'}]}}
        return type('Response', (), {'execute': lambda self: data})()


BOXES = [{'key': 'ops', 'address': 'ops@glpclinicscout.com'},
         {'key': 'izaiah', 'address': 'izaiah@glpclinicscout.com'}]


def test_paginated_mail_reconciliation_is_complete_or_refuses(tmp_path):
    fixture(tmp_path)
    adapter = importlib.import_module('glp328_readonly_evidence')
    from affiliate_sheet_schema_guard import EXPECTED_HEADERS
    fields = {'Program / Network': 'Fixture Gamma', 'Official URL': 'https://gamma.example',
              'Status': 'approved_active_tracking_ready', 'Category': 'Affiliate program',
              'Network / Route': 'Impact.com', 'Fit Reason': 'fixture', 'Vertical': 'glp1-weightloss'}
    values = [list(EXPECTED_HEADERS), [fields.get(h, '') for h in EXPECTED_HEADERS]]
    mail = Mail()
    result = adapter.plan_current(values, tmp_path, BOXES, lambda b: mail, now=NOW)
    assert result['status'] == 'DISABLED_PLAN_ONLY', result
    assert result['plan']['candidates'] == []  # no contact/US evidence inferred
    assert result['plan']['excluded'][0]['reason'] == 'us_location_evidence_missing'
    assert result['mailbox_coverage']['complete'] is True
    assert result['mailbox_coverage']['checked_host_count'] == 1
    assert len([c for c in mail.calls if c[0] == 'list']) == 4
    assert len([c for c in mail.calls if c[0] == 'get']) == 4
    # Exact known IDs get SENT readback, not an invented accepted follow-up receipt.
    assert hasattr(adapter, 'read_mail_sources')
    proof = adapter.read_mail_sources(['gamma.example'], [], [
        {'domains': ['gamma.example'], 'provider_message_id': 'fixture-sent',
         'provider_thread_id': 'fixture-thread', 'source_position': 1}],
        BOXES, lambda b: Mail(), NOW)
    assert proof['complete'] is True
    assert proof['sent_readbacks'][0]['sent_label_confirmed'] is True
    assert proof['sent_readbacks'][0]['verified'] is False
    for boxes, service in ((BOXES, Mail(True)), ([], Mail())):
        bad = adapter.plan_current(values, tmp_path, boxes, lambda b: service, now=NOW)
        assert bad['status'] == 'REFUSED_INCOMPLETE_SOURCES'
        assert bad['planner_refusal'] == 'conversation_snapshot_incomplete'
        assert bad['plan'] is None


def test_source_drift_during_mail_read_refuses(tmp_path):
    fixture(tmp_path)
    adapter = importlib.import_module('glp328_readonly_evidence')
    from affiliate_sheet_schema_guard import EXPECTED_HEADERS
    mail = Mail()
    def mutate_fixture(box):
        with (tmp_path / 'suppression_list.jsonl').open('a') as handle:
            handle.write(json.dumps({'domain': 'new.example', 'scope': 'domain'})+'\n')
        return mail
    result = adapter.plan_current([list(EXPECTED_HEADERS)], tmp_path, BOXES, mutate_fixture, now=NOW)
    assert result['status'] == 'REFUSED_INCOMPLETE_SOURCES'
    assert 'local_sources_changed_during_mail_read' in result['local']['faults']
    assert result['plan'] is None
