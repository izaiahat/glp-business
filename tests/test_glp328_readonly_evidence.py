"""Only synthetic files/mail services; no live writers or credentials."""
import importlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import pytest

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


@pytest.mark.parametrize('journal_mode', ['DELETE', 'WAL'])
@pytest.mark.parametrize('transaction', ['writer_refusal', 'reader_cleanup'])
def test_same_process_sqlite_locks_survive_snapshot(tmp_path, journal_mode, transaction):
    """A separate ordinary SQLite connection must observe the original lock.

    No inventory/open/close of the original DB or SHM occurs between probes:
    such a close would itself drop the same-process POSIX lock under test.
    """
    fixture(tmp_path)
    adapter = importlib.import_module('glp328_readonly_evidence')
    from affiliate_sheet_schema_guard import EXPECTED_HEADERS
    path = tmp_path / 'outreach.sqlite'
    holder = sqlite3.connect(path)
    try:
        assert holder.execute(f'PRAGMA journal_mode={journal_mode}').fetchone()[0].upper() == journal_mode
        if journal_mode == 'WAL':
            holder.execute("insert into suppressions values ('wal','','','wal.example','domain',1)")
            holder.commit()
        if transaction == 'writer_refusal':
            holder.execute('BEGIN IMMEDIATE')
        else:
            holder.execute('BEGIN')
            holder.execute('SELECT count(*) FROM suppressions').fetchone()
        assert holder.in_transaction

        action = ('checkpoint' if journal_mode == 'WAL' and transaction == 'reader_cleanup'
                  else 'BEGIN EXCLUSIVE' if transaction == 'reader_cleanup' else 'BEGIN IMMEDIATE')

        def probe():
            script = ("import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); "
                      "c.execute('PRAGMA busy_timeout=0'); "
                      "\ntry:\n"
                      " if sys.argv[2]=='checkpoint':\n"
                      "  print(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())\n"
                      " else:\n"
                      "  c.execute(sys.argv[2]); print('acquired'); c.rollback()\n"
                      "except sqlite3.OperationalError as e:\n"
                      " print('busy' if e.sqlite_errorcode==sqlite3.SQLITE_BUSY else 'unexpected')\n"
                      "finally: c.close()")
            completed = subprocess.run([sys.executable, '-B', '-c', script, str(path), action],
                                       capture_output=True, text=True, check=True, timeout=10)
            return completed.stdout.strip()

        expected = '(1, 1, 1)' if journal_mode == 'WAL' and transaction == 'reader_cleanup' else 'busy'
        assert probe() == expected
        if transaction == 'writer_refusal':
            calls = []
            result = adapter.plan_current([list(EXPECTED_HEADERS)], tmp_path, BOXES,
                                          lambda box: calls.append(box), now=NOW)
            assert result['planner_refusal'] == 'suppression_snapshot_incomplete'
            assert result['local']['faults'] and not calls
        else:
            local = adapter.read_local_sources(tmp_path, NOW)
            assert local['suppression']['complete'], local['faults']
            if journal_mode == 'WAL':
                assert 'wal.example' in {entry['domain'] for entry in local['suppression']['entries']}
        assert holder.in_transaction
        assert probe() == expected
        holder.rollback()
        assert probe() == ('(0, 0, 0)' if action == 'checkpoint' else 'acquired')
    finally:
        holder.close()


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


def test_wal_evidence_never_mutates_source_sidecars(tmp_path, monkeypatch):
    import shutil
    import os
    adapter = importlib.import_module('glp328_readonly_evidence')
    origin = tmp_path / 'origin'
    origin.mkdir()
    fixture(origin)
    writer = sqlite3.connect(origin / 'outreach.sqlite')
    try:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        writer.execute("insert into suppressions values ('wal-only','','','wal.example','domain',1)")
        writer.commit()
        cold = tmp_path / 'without-shm'
        cold.mkdir()
        for p in origin.iterdir():
            if not p.name.endswith('-shm'):
                shutil.copy2(p, cold / p.name)
        def inventory(root):
            return {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ctime_ns)
                    for p in root.iterdir()}
        before = inventory(cold)
        from affiliate_sheet_schema_guard import EXPECTED_HEADERS
        calls = []
        result = adapter.plan_current([list(EXPECTED_HEADERS)], cold, BOXES,
                                      lambda b: calls.append(b), now=NOW)
        local = result['local']
        assert inventory(cold) == before  # predecessor creates SHM here
        assert result['planner_refusal'] == 'suppression_snapshot_incomplete'
        assert not calls and result['plan'] is None
        # Unsupported no-SHM coherence must refuse, never silently drop WAL.
        assert not local['suppression']['complete']
        assert local['faults']
        before = inventory(origin)
        local = adapter.read_local_sources(origin, NOW)
        assert local['suppression']['complete'], local['faults']
        assert 'wal.example' in {e['domain'] for e in local['suppression']['entries']}
        assert inventory(origin) == before
        # Real SQLite write-lock contention must refuse without a private read.
        writer.execute('BEGIN IMMEDIATE')
        local = adapter.read_local_sources(origin, NOW)
        assert not local['suppression']['complete']
        writer.rollback()
        before = inventory(origin)  # exclude deliberate fixture writer's SHM read-mark changes
        # The kernel locks must stay held throughout the DB/WAL copy.
        real_read = os.read
        attempts = []
        def try_writer(fd, size):
            if not attempts:
                writer.execute('PRAGMA busy_timeout=0')
                try:
                    writer.execute('BEGIN IMMEDIATE')
                except sqlite3.OperationalError:
                    attempts.append('locked')
                else:
                    writer.rollback()
                    raise AssertionError('snapshot did not exclude SQLite writer')
            return real_read(fd, size)
        with monkeypatch.context() as m:
            m.setattr(adapter.os, 'read', try_writer)
            # Exercise the capture worker's copy fence directly; monkeypatches
            # in this test process do not propagate through its fresh exec.
            with tempfile.TemporaryDirectory() as private:
                assert adapter._capture_sqlite_snapshot(origin / 'outreach.sqlite', Path(private))
        assert attempts == ['locked']
        assert inventory(origin) == before
    finally:
        writer.close()


def test_domain_controls_keep_retired_references_without_global_refusal(tmp_path):
    fixture(tmp_path)
    adapter = importlib.import_module('glp328_readonly_evidence')
    from affiliate_sheet_schema_guard import EXPECTED_HEADERS
    row = {'target_id': 'retired-fixture', 'domain': 'retired.example',
           'email': 'bd@retired.example', 'scope': 'domain', 'active': True,
           'source': 'fixture-rollback', 'receipt_json': {'target_id': 'retired-fixture',
                                                       'note': 'removed before send'}}
    (tmp_path / 'suppression_list.jsonl').write_text(json.dumps(row)+'\n')
    with sqlite3.connect(tmp_path / 'outreach.sqlite') as con:
        con.execute('insert into suppressions values (?,?,?,?,?,?)',
                    ('retired-control', row['target_id'], row['email'], row['domain'], 'domain', 1))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    mail = Mail()
    result = adapter.plan_current([list(EXPECTED_HEADERS)], tmp_path, BOXES, lambda b: mail, now=NOW)
    local = result['local']
    assert local['suppression']['complete'], local['faults']
    assert result['status'] == 'DISABLED_PLAN_ONLY' and result['plan']['candidates'] == []
    assert not result['send_enabled'] and result['contact_evidence']['supplied_contacts'] == 0
    assert len([e for e in local['suppression']['entries'] if e['domain'] == 'retired.example']) == 2
    diagnostics = local['reference_diagnostics']
    assert len(diagnostics) == 2
    assert {d['source'] for d in diagnostics} == {'suppression_list.jsonl', 'sqlite.suppressions'}
    assert all(d['target_id'] == row['target_id'] and d['code'] == 'historical_target_identity_unresolved'
               and d['position'] >= 1 for d in diagnostics)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    # Same reference is NOT independent identity for these malformed/ambiguous
    # or target-dependent rows. Keep every resolvable adverse domain on refusal.
    for delta in ({'scope': 'target'}, {'scope': 'organization'}, {'scope': []},
                  {'domain': 'bad host.example'}, {'email': 'broken'},
                  {'email': 'bd@conflict.example'}, {'domain': '', 'email': ''},
                  {'target_id': ['malformed']}):
        (tmp_path / 'suppression_list.jsonl').write_text(json.dumps(row | delta)+'\n')
        calls = []
        result = adapter.plan_current([list(EXPECTED_HEADERS)], tmp_path, BOXES,
                                      lambda b: calls.append(b), now=NOW)
        assert result['planner_refusal'] == 'suppression_snapshot_incomplete', delta
        assert not calls and result['plan'] is None
        assert 'retired.example' in {e['domain'] for e in result['local']['suppression']['entries']}


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
