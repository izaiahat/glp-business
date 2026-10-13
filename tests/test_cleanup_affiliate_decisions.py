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


def mail(subject, body, recipient='ops@glpclinicscout.com', sender='notice@awin.com', headers=None):
    m = EmailMessage()
    m['From'] = sender
    m['To'] = recipient
    m['Subject'] = subject
    m['Date'] = 'Thu, 01 Oct 2026 02:00:00 +0000'
    m['Message-ID'] = '<synthetic-decision@example.invalid>'
    for key, value in (headers or {}).items():
        m[key] = value
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


def test_automated_network_invitation_is_not_a_direct_application(inbox):
    cp, _, ledger, mailbox = inbox
    raw = mail('Fixture advertiser has invited you to join their program',
               'You have been invited to join the affiliate program. '
               'Apply to join the program; contact partner@example.invalid.',
               sender='help@awin.com')
    parsed = cp.parse_message(raw, mailbox)
    classified = cp.classify_message(mailbox, parsed)
    assert classified['category'] == 'affiliate_network_response'
    assert classified['reason_code'] == 'affiliate_network_automated_invitation'
    assert classified['actionable'] is False
    assert cp.route_for(classified['category'], classified['reason_code']) == 'silent'
    assert cp.affiliate_response_disposition(parsed, classified) == 'other_affiliate_response'
    event = ledger.ingest(mailbox, 'automated-invitation', raw)
    assert ledger.conn.execute('SELECT count(*) FROM local_drafts WHERE event_id=?',
                               (event['event_id'],)).fetchone()[0] == 0


def test_real_human_direct_application_invitation_keeps_its_route(inbox):
    cp, _, _, mailbox = inbox
    parsed = cp.parse_message(mail(
        'Re: Affiliate or publisher partnership?',
        'Please send your social media accounts and relevant information directly '
        'to partnerships@joinamble.com. Our affiliate team will review your information.',
        sender='Angelica <angelica@amble.example>'), mailbox)
    classified = cp.classify_message(mailbox, parsed)
    assert classified['category'] == 'affiliate_network_response'
    assert classified['reason_code'] != 'affiliate_network_automated_invitation'
    assert cp.affiliate_response_disposition(parsed, classified) == 'auto_process_application_invitation'


@pytest.mark.parametrize('body,expected', [
    ('Your application has been declined. Apply to join our program.', 'auto_process_rejection'),
    ('Please complete KYC before you apply to join the program.', 'operator_prerequisite_card'),
])
def test_automated_invitation_does_not_override_earlier_gates(inbox, body, expected):
    cp, _, _, mailbox = inbox
    parsed = cp.parse_message(mail('Invitation', body), mailbox)
    classification = {'category': 'affiliate_network_response',
                      'reason_code': 'affiliate_network_automated_invitation'}
    assert cp.affiliate_response_disposition(parsed, classification) == expected


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


@pytest.mark.parametrize('bad', ['not-json', '[]', 'null'])
def test_malformed_notice_is_row_local_and_retains_writer_readbacks(tmp_path, monkeypatch, bad):
    import importlib.util
    from support.google_stubs import google_import_stubs
    with google_import_stubs():
        monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
        spec = importlib.util.spec_from_file_location('notice_batch_drain', ROOT / 'scripts/glp_sheet_sync_drain.py')
        assert spec is not None and spec.loader is not None
        d = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(d)
        monkeypatch.setattr(d.writer, 'creds', lambda: None)
        reads, attempts = [], []
        def read(*a, **k):
            reads.append(len(attempts))
            return [[]]
        # Synthetic authoritative row/mode transport; the actual drain owns
        # ordering, row refusal, result accumulation and post-mode readbacks.
        monkeypatch.setattr(d, 'read_sheet', read)
        monkeypatch.setattr(d, 'resolve_sheet_row', lambda *a: (1, None))
        monkeypatch.setattr(d, 'build_entry', lambda *a: {})
        monkeypatch.setattr(d, 'plan_for', lambda mode, entry, row: {'update_id': row['update_id']})
        monkeypatch.setattr(d, 'repo_relative', str)
        def mode(mode, plan, *a, **k):
            attempts.append(plan['update_id'])
            return {'rc': 0, 'receipt': {'status': 'PASS', 'value_cells_written': 1}}
        monkeypatch.setattr(d, 'run_mode_with_retry', mode)
        rows = [{'update_id': 'before', 'kind': 'approval_confirmed', 'evidence': '{}'},
                {'update_id': 'bad', 'kind': 'affiliate_decision_notice', 'evidence': bad},
                {'update_id': 'after', 'kind': 'approval_confirmed', 'evidence': '{}'}]
        result = d.drain(rows, tmp_path / 'drain', apply=True)
        assert [r['update_id'] for r in result['results']] == ['before', 'bad', 'after']
        assert [r['status'] for r in result['results']] == ['applied', 'refused', 'applied']
        refused = result['results'][1]
        assert refused['reason'].startswith('evidence_not_')
        assert refused['mode'] is None and refused['cells_written'] == 0
        assert attempts == ['before', 'after']
        assert reads == [0, 1, 2]
        assert result['applied'] == 2 and result['refused'] == 1
        assert result['cells_written'] == 2  # fixture writes only, never live


# Reviewed variants: Partnerize punctuation is documented in control_plane;
# MaxBounty wording is the maintained test_affiliate_cancellation_intake template.
REVIEWED_TEMPLATES = [
    ('partnerize.com', 'approval', 'Partnerize Email Alert', 'Your request to join the eMed.com partner program has been accepted.'),
    ('maxbounty.com', 'cancelled', 'Campaign Cancellation Notice', 'The advertiser for campaign Roman Health - Metabolic Weight Loss - CPL (US) requires that it expire on 09/14/2026 21:30.'),
    ('awin.com', 'approval', 'Membership Approved', 'Your membership for Northstar Health is now active.'),
    ('cj.com', 'rejection', 'Application Declined', 'Northstar Health has declined your application.'),
    ('impact.com', 'terminated', 'Partnership termination', 'Northstar Health has terminated your partnership.'),
]


@pytest.mark.parametrize('network,decision,subject,body', REVIEWED_TEMPLATES)
def test_reviewed_templates_route_from_ingest_once(inbox, network, decision, subject, body):
    cp, q, ledger, mailbox = inbox
    raw = mail(subject, body, sender='no-reply@' + network)
    ledger.ingest(mailbox, 'reviewed-template', raw)
    queue = Queue()
    assert q.dispatch(ledger, live=True, client=queue)['delivered'] == 1
    assert len(queue.items) == 1
    evidence = json.loads(queue.items[0]['evidence'])
    assert evidence['decision_class'] == decision
    assert evidence['requires_provider_verification'] is True
    assert evidence['sheet_write_authority'] is False
    assert queue.items[0]['program_id'] == '' and queue.items[0]['sheet_row'] is None
    assert ledger.ingest(mailbox, 'reviewed-template', raw)['status'] == 'duplicate'
    assert q.dispatch(ledger, live=True, client=queue)['delivered'] == 0
    assert queue.inserts == 1


@pytest.mark.parametrize('sender,subject,body', [
    ('no-reply@awin.com', REVIEWED_TEMPLATES[1][2], REVIEWED_TEMPLATES[1][3]),
    ('no-reply@maxbounty.com', 'Campaign Cancellation Notice', 'Browse campaigns that expire soon.'),
    ('no-reply@maxbounty.com', 'Campaign Cancellation Notice', 'The advertiser for campaign Fixture requires that it expire on an unknown date.'),
    ('no-reply@partnerize.com', 'Program update', 'Your request to join eMed.com is being reviewed. Your application has not been approved.'),
    ('no-reply@partnerize.com', 'Program update', 'Your application has been approved. Your request to join eMed.com has been rejected.'),
])
def test_reviewed_templates_do_not_weaken_network_structure_or_ambiguity(inbox, sender, subject, body):
    cp, q, ledger, mailbox = inbox
    ledger.ingest(mailbox, 'template-negative', mail(subject, body, sender=sender))
    queue = Queue()
    q.dispatch(ledger, live=True, client=queue)
    assert queue.inserts == 0


@pytest.mark.parametrize('sender,subject,body,headers', [
    ('newsletters@cj.com', 'CJ Partner Newsletter', 'Join our webinar on what to do when a program is paused. Register today!', {'Precedence': 'bulk', 'List-Unsubscribe': '<mailto:unsubscribe@cj.com>'}),
    ('newsletters@awin.com', 'Publisher newsletter', 'A new commission rate is available for selected advertisers. Explore our marketplace today!', {'Precedence': 'bulk'}),
    ('no-reply@cj.com', 'Program education', 'Learn what to do when the program is paused.', {}),
    ('no-reply@awin.com', 'Marketplace offers', 'A new commission rate is available for selected advertisers.', {}),
    ('newsletters@awin.com', 'Publisher newsletter', 'Example: Your application has been approved.', {}),
    ('no-reply@cj.com', 'CJ webinar', 'Example: Your application has been approved.', {'Precedence': 'bulk'}),
])
def test_promotion_sender_and_structure_never_stage_decision(inbox, sender, subject, body, headers):
    cp, q, ledger, mailbox = inbox
    ledger.ingest(mailbox, 'promotion', mail(subject, body, sender=sender, headers=headers))
    assert ledger.conn.execute('SELECT count(*) FROM mailbox_actions WHERE action=?', (q.ACTION,)).fetchone()[0] == 0
    queue = Queue()
    q.dispatch(ledger, live=True, client=queue)
    assert queue.inserts == 0


def test_multiline_training_template_is_not_an_account_approval(inbox, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network forbidden'))
    cp, q, ledger, mailbox = inbox
    raw = mail('Publisher template gallery',
               'Training snippet:\nYour application has been approved.\nThis is sample copy for our template gallery; no account status has changed. Explore the marketplace today.',
               sender='education@cj.com', headers={'Precedence': 'bulk',
               'Authentication-Results': 'mx.google.com; dmarc=pass header.from=cj.com'})
    assert ledger.ingest(mailbox, 'multiline-training-template', raw)['status'] == 'persisted'
    assert ledger.conn.execute('SELECT count(*) FROM mailbox_actions WHERE action=?', (q.ACTION,)).fetchone()[0] == 0
    queue = Queue()
    assert q.dispatch(ledger, live=True, client=queue)['delivered'] == 0
    assert queue.inserts == 0


def test_until_instruction_is_not_an_account_approval(inbox, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network forbidden'))
    cp, q, ledger, mailbox = inbox
    raw = mail('Publisher onboarding tips',
               'Until your application is approved, do not place advertiser links. Explore our marketplace and apply today.',
               sender='offers@awin.com', headers={'Precedence': 'bulk',
               'Authentication-Results': 'mx.google.com; dmarc=pass header.from=awin.com'})
    assert ledger.ingest(mailbox, 'conditional-until', raw)['status'] == 'persisted'
    assert ledger.conn.execute('SELECT count(*) FROM mailbox_actions WHERE action=?', (q.ACTION,)).fetchone()[0] == 0
    queue = Queue()
    assert q.dispatch(ledger, live=True, client=queue)['delivered'] == 0
    assert queue.inserts == 0


@pytest.mark.parametrize('subject,body', [
    ('Application status', 'Your application has been approved?'),
    ('Your application has been approved', 'Until your application is approved, do not place advertiser links.'),
    ('Publisher notes', 'Illustration:\n\nYour application has been approved.'),
    ('Publisher notes', 'Until\nyour application is approved, do not place advertiser links.'),
    ('Publisher notes', 'Your application is approved if you complete onboarding.'),
    ('Publisher notes', 'Please write "Your application has been approved." in the email.'),
    ('Publisher notes', 'Imagine your application has been approved.'),
    ('Publisher notes', 'The application has been approved.'),
    ('Publisher notes', 'Highlights:\nYour application has been approved.'),
])
def test_approval_requires_a_body_assertion_outside_displayed_copy(inbox, subject, body):
    cp, q, ledger, mailbox = inbox
    ledger.ingest(mailbox, 'assertion-boundary', mail(subject, body, sender='education@cj.com'))
    queue = Queue()
    q.dispatch(ledger, live=True, client=queue)
    assert queue.inserts == 0
    assert ledger.conn.execute('SELECT count(*) FROM mailbox_actions WHERE action=?', (q.ACTION,)).fetchone()[0] == 0


@pytest.mark.parametrize('body', [
    'Dear Publisher:\nYour application has been approved.',
    'Your application status:\nYour application has been approved.',
    'Your request to join the\neMed.com partner program has been accepted.',
    'We have approved your application.',
])
def test_affirmative_grammar_keeps_bulk_account_notices(inbox, body):
    cp, q, ledger, mailbox = inbox
    raw = mail('Publisher notes', body, sender='education@cj.com', headers={'Precedence': 'bulk'})
    ledger.ingest(mailbox, 'affirmative-grammar', raw)
    queue = Queue()
    assert q.dispatch(ledger, live=True, client=queue)['delivered'] == 1
    assert json.loads(queue.items[0]['evidence'])['decision_class'] == 'approval'
    assert queue.items[0]['sheet_row'] is None and queue.items[0]['program_id'] == ''
    assert ledger.ingest(mailbox, 'affirmative-grammar', raw)['status'] == 'duplicate'
    q.dispatch(ledger, live=True, client=queue)
    assert queue.inserts == 1


@pytest.mark.parametrize('network,decision,subject,body', REVIEWED_TEMPLATES + [
    ('awin.com', decision, subject, body) for decision, subject, body in DECISIONS
])
def test_true_bulk_status_templates_still_route(inbox, network, decision, subject, body):
    cp, q, ledger, mailbox = inbox
    raw = mail(subject, body, sender='no-reply@' + network, headers={
        'Precedence': 'bulk', 'List-Unsubscribe': '<mailto:unsubscribe@' + network + '>'})
    ledger.ingest(mailbox, 'bulk-status', raw)
    queue = Queue()
    assert q.dispatch(ledger, live=True, client=queue)['delivered'] == 1
    assert json.loads(queue.items[0]['evidence'])['decision_class'] == decision
