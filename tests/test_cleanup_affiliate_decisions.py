"""Synthetic exact-checkout decision routing; no provider calls or mail sends."""
import json
import sys
from pathlib import Path
from email.message import EmailMessage
import pytest

ROOT = Path(__file__).resolve().parents[1]
DECISIONS = [
    ('approval', 'Fixture affiliate program', 'Your application has been approved.'),
    ('rejection', 'Membership Rejection', 'Your application has been declined.'),
    ('rate_change', 'Changes to your commission rates', 'The commission rate has changed.'),
    ('paused', 'The Program Fixture Has Been Paused', 'The program has been paused.'),
    ('cancelled', 'Program Fixture cancelled', 'The program has been cancelled.'),
    ('terminated', 'Program Fixture terminated', 'The program has been terminated.'),
]


class Queue:
    def __init__(self):
        self.items = []
        self.inserts = 0
        self.lose_response = False

    def eq(self, key, value):
        return key, value

    def rows(self, table, condition, limit=2):
        from inbox_control_plane import affiliate_decision_queue as q
        assert table == q.TABLE
        key, value = condition
        return [r for r in self.items if r[key] == value][:limit]

    def insert(self, table, row):
        from inbox_control_plane import affiliate_decision_queue as q
        assert table == q.TABLE
        self.inserts += 1
        self.items.append(dict(row))
        if self.lose_response:
            raise TimeoutError('synthetic lost response')


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    from inbox_control_plane import control_plane as cp
    from inbox_control_plane import affiliate_supply_intake as supply
    from inbox_control_plane import affiliate_decision_queue as q
    assert Path(cp.__file__).resolve().is_relative_to(ROOT)
    assert Path(q.__file__).resolve().is_relative_to(ROOT)
    for name in ('PRIVATE_ROOT', 'RAW_ROOT', 'ATTACHMENT_ROOT', 'BACKUP_ROOT', 'REPORT_ROOT', 'DAILY_AUDIT_DIR'):
        monkeypatch.setattr(cp, name, tmp_path / name)
    monkeypatch.setattr(supply, 'INTAKE_ROOT', tmp_path / 'supply')
    ledger = cp.Ledger(tmp_path / 'inbox.sqlite3')
    mailbox = {
        'mailbox_id': 'glp_ops_gmail', 'venture': 'glp_clinic_scout',
        'company': 'GLP Clinic Scout', 'canonical_address': 'ops@glpclinicscout.com',
        'aliases': ['partners@glpclinicscout.com'], 'groups': [],
        'provider': 'gmail', 'adapter': 'gmail_api_readonly',
        'auth_reference': 'synthetic-unused', 'owner': cp.CANONICAL_INBOX_JOB_ID,
        'lifecycle': 'active', 'poll_seconds': 300, 'freshness_sla_seconds': 900,
        'inbound_surface': 'email', 'routing_policy': 'glp_default', 'dmarc_domains': [],
    }
    registry = {'mailboxes': [mailbox]}
    ledger.sync_registry(registry)
    yield cp, q, ledger, mailbox
    ledger.close()


def mail(subject, body, recipient='ops@glpclinicscout.com', sender='notice@awin.com'):
    m = EmailMessage()
    m['From'] = sender
    m['To'] = recipient
    m['Subject'] = subject
    m['Date'] = 'Thu, 01 Oct 2026 02:00:00 +0000'
    m['Message-ID'] = '<synthetic-decision@example.invalid>'
    m.set_content(body)
    return m.as_bytes()


@pytest.mark.parametrize('decision,subject,body', DECISIONS)
def test_each_decision_is_ingested_and_routed_to_single_writer_queue(inbox, decision, subject, body):
    cp, q, ledger, mailbox = inbox
    raw = mail(subject, body)
    result = ledger.ingest(mailbox, 'synthetic-' + decision, raw)
    queued = Queue()
    assert q.dispatch(ledger, live=True, client=queued)['status'] == 'pass'
    assert len(queued.items) == 1
    item = queued.items[0]
    assert item['kind'] == 'affiliate_decision_notice'
    evidence = json.loads(item['evidence'])
    assert evidence['decision_class'] == decision
    assert evidence['intended_writer_kind'] == q.KINDS[decision]
    assert evidence['sheet_write_authority'] is False
    assert evidence['raw_sha256'] == cp.sha256_bytes(raw)
    assert Path(evidence['path']).read_bytes() == raw
    assert 'body' not in evidence and 'sender' not in evidence
    assert ledger.ingest(mailbox, 'synthetic-' + decision, raw)['status'] == 'duplicate'
    assert q.dispatch(ledger, live=True, client=queued)['delivered'] == 0
    assert queued.inserts == 1
    assert result['status'] == 'persisted'


def test_lost_response_reconciles_without_replay(inbox):
    cp, q, ledger, mailbox = inbox
    ledger.ingest(mailbox, 'lost', mail('Program update', 'The program has been terminated.'))
    queue = Queue()
    queue.lose_response = True
    assert q.dispatch(ledger, live=True, client=queue)['failed'] == 1
    assert q.dispatch(ledger, live=True, client=queue)['delivered'] == 1
    assert queue.inserts == 1


def test_uncertain_absence_never_replays(inbox):
    cp, q, ledger, mailbox = inbox
    ledger.ingest(mailbox, 'absent', mail('Program update', 'The program has been cancelled.'))
    queue = Queue()
    queue.lose_response = True
    q.dispatch(ledger, live=True, client=queue)
    queue.items.clear()
    assert q.dispatch(ledger, live=True, client=queue)['uncertain'] == 1
    assert queue.inserts == 1


@pytest.mark.parametrize('kwargs', [{'bootstrap': True}, {'labels': ['DRAFT']}])
def test_bootstrap_and_draft_are_not_queued(inbox, kwargs):
    cp, q, ledger, mailbox = inbox
    ledger.ingest(mailbox, 'suppressed', mail('Program update', 'The program has been paused.'), **kwargs)
    queue = Queue()
    q.dispatch(ledger, live=True, client=queue)
    assert not queue.items


@pytest.mark.parametrize('subject,body,sender,recipient', [
    ('Old decision', 'On Tue a partner wrote:\n> The program has been terminated.', 'notice@awin.com', 'ops@glpclinicscout.com'),
    ('Re: Your application has been approved', 'Thank you for your application. We are reviewing it.', 'notice@awin.com', 'ops@glpclinicscout.com'),
    ('Program update', 'If the program is paused, we will contact you.', 'notice@awin.com', 'ops@glpclinicscout.com'),
    ('Program update', 'The program has been cancelled.', 'notice@awin.com.evil.invalid', 'ops@glpclinicscout.com'),
    ('Program update', 'The program has been terminated.', 'notice@awin.com', 'unknown@example.invalid'),
    ('Fella Health program', 'Your application has been approved.', 'notice@awin.com', 'ops@glpclinicscout.com'),
])
def test_quotes_conditional_lookalike_unknown_identity_and_exclusion_do_not_queue(inbox, subject, body, sender, recipient):
    cp, q, ledger, mailbox = inbox
    ledger.ingest(mailbox, 'negative', mail(subject, body, sender=sender, recipient=recipient))
    queue = Queue()
    q.dispatch(ledger, live=True, client=queue)
    assert not queue.items


def test_held_event_is_not_dispatched(inbox):
    cp, q, ledger, mailbox = inbox
    result = ledger.ingest(mailbox, 'held', mail('Program update', 'The program has been paused.'))
    with ledger.conn:
        ledger.conn.execute("UPDATE inbox_events SET state='held' WHERE event_id=?", (result['event_id'],))
    queue = Queue()
    q.dispatch(ledger, live=True, client=queue)
    assert not queue.items


def test_registered_transport_never_uses_replaying_public_insert():
    from inbox_control_plane import affiliate_decision_queue as q
    from unittest.mock import Mock
    transport = Mock()
    transport.eq.return_value = {'exact': 'one-id'}
    row = {'update_id': 'one-id'}
    q.RegisteredQueueClient(transport).insert(q.TABLE, row)
    transport.insert.assert_not_called()
    transport._native_insert_after_reconciliation.assert_called_once_with(
        q.TABLE, row, {'exact': 'one-id'})


@pytest.mark.parametrize('decision,subject,body', DECISIONS)
def test_sheet_drain_records_notice_without_any_writer_mode(inbox, tmp_path, monkeypatch, decision, subject, body):
    import importlib.util
    from support.google_stubs import google_import_stubs
    cp, q, ledger, mailbox = inbox
    ledger.ingest(mailbox, 'drain-' + decision, mail(subject, body))
    queue = Queue()
    q.dispatch(ledger, live=True, client=queue)
    assert queue.items
    with google_import_stubs():
        monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
        spec = importlib.util.spec_from_file_location('cleanup_sheet_drain', ROOT / 'scripts/glp_sheet_sync_drain.py')
        assert spec is not None and spec.loader is not None
        d = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(d)
        assert Path(d.writer.__file__).resolve().is_relative_to(ROOT)
        monkeypatch.setattr(d.writer, 'creds', lambda: None)
        monkeypatch.setattr(d, 'read_sheet', lambda *a, **k: [])
        monkeypatch.setattr(d, 'run_mode_with_retry', lambda *a, **k: pytest.fail('notice must never mutate the Sheet'))
        result = d.drain(queue.items, tmp_path / 'drain', apply=True)
        r = result['results'][0]
        assert r['status'] == 'escalated'
        assert r['decision_class'] == decision
        assert r['mode'] is None
        assert r['cells_written'] == 0
