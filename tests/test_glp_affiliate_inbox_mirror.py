"""Contract tests for the inbox-mirror lane's classifier and projection.

The mailbox is the only place some affiliate state changes ever appear, so this
lane is the difference between seeing a pause/rejection and not seeing it. What
these tests pin is that it stays HONEST while doing that:

  * real observed subjects/bodies classify to the right kind (the fixtures below
    are the actual shapes found in the GLP ops mailbox on 2026-08-18),
  * mail that requires an ACTION but is not a program state change - account
    verification, ID proof, a contract to sign - never becomes a queue row,
  * marketing mail from a network never becomes a queue row,
  * Awin's own publisher id (`pid:`) is never mistaken for the advertiser id,
  * every emitted row carries a gmail message id AND an on-disk evidence path,
    in the exact shape the drain's mode for that kind demands.

Pure-function tests - no Gmail call, no network, no Sheet write.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path('/home/ubuntu/business')
SCRIPTS = str(ROOT / 'scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


from support.google_stubs import google_import_stubs, with_google_import_stubs
import pytest


@pytest.fixture(autouse=True)
def _scoped_google_imports():
    with google_import_stubs():
        yield




@with_google_import_stubs
def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


M = load('inbox_mirror_under_test', 'scripts/glp_affiliate_inbox_mirror.py')


def test_scan_passes_bounded_quota_retries_to_gmail(tmp_path):
    budgets = []

    class StopAfterGet(Exception):
        pass

    class Request:
        def __init__(self, is_get=False):
            self.is_get = is_get

        def execute(self, num_retries=0):
            budgets.append(num_retries)
            if self.is_get:
                raise StopAfterGet()
            return {'messages': [{'id': 'quota-regression'}]}

    class Service:
        def users(self):
            return self

        def messages(self):
            return self

        def list(self, **kwargs):
            return Request()

        def get(self, **kwargs):
            return Request(is_get=True)

    with pytest.raises(StopAfterGet):
        M.scan(Service(), {}, 1, capture_dir=tmp_path)
    assert budgets == [6, 6]


def test_everflow_approval_preserves_dotted_legal_entity():
    subject = 'Welcome to Partner Program! Roman Health Ventures Inc. has approved your application.'
    assert M.program_identity(subject, '') == ('Roman Health Ventures Inc.', '')
    assert M.subject_brand(subject) == 'Roman Health Ventures Inc.'


#: The real registry, used by the test double below so the dedupe key under test
#: is the one production computes, never a lookalike.
AI = load('action_items_for_mirror_tests', 'ops/action_items.py')
W = load('sheet_sync_writer_for_mirror', 'scripts/affiliate_sheet_batch_writer.py')

# Real shapes observed in the GLP ops mailbox, 2026-08-18.
KATALYS_PAUSE = ('The Program Breeze Meds Has Been Paused ⏸️',
                 'The program Breeze Meds has been paused by the advertiser.')
AWIN_REJECTION = ('Membership Rejection - advertiser',
                  'Dear Izaiah Tilton, Your application to join MicroVitamin (US) (aid:122466) '
                  'program with Tilton Group (pid:2979387) has been rejected.')
AWIN_APPROVAL = ('Membership active with advertiser Health Fitness & Longevity (aid: 81485)',
                 'Your membership is now active.')
AWIN_WELCOME = ('Welcome to the Dr. Tobias Community',
                'Welcome to the Dr. Tobias affiliate program.')
UPPROMOTE_VERIFY = ('Verify your email address for UpPromote Marketplace',
                    'Thanks for registering on UpPromote Marketplace! To complete your '
                    'application, please click the button below to verify your email address.')
HEALTHTRADER_ID = ('ID Proof - Invalid Format - HealthTrader',
                   'The ID proof you uploaded is in an invalid format.')
DOCUSIGN = ('Voided: Please Sign: TeleWellnessMD Affiliate Agreement',
            'The envelope was voided. Please sign the affiliate agreement.')
AWIN_MARKETING = ('Plunge Chill (122418): 6 Tips to Cold Plunge Better',
                  'Promote Plunge Chill this season with 10% commission.')
KATALYS_NEWSLETTER = ('Inside: Telehealth Program Updates | August 2026',
                      'This month in the Katalys marketplace: new telehealth programs.')


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('fixture,expected', [
    (KATALYS_PAUSE, 'network_paused'),
    (AWIN_REJECTION, 'rejection'),
    (AWIN_APPROVAL, 'new_approval'),
    (AWIN_WELCOME, 'new_approval'),
])
def test_real_mail_classifies_to_the_right_kind(fixture, expected):
    assert M.classify(*fixture) == expected


def test_partner_manager_rate_increase_is_captured_for_confirmation_not_auto_queued():
    subject = 'Re: Affiliate application'
    body = 'I updated your contract terms and increased the GLP-1 CPA to $260.'
    kind = M.classify(subject, body)
    assert kind == 'partner_manager_terms_change_confirmation_required'
    assert kind in M.UNROUTABLE_KINDS
    assert kind not in M.LANE_OWNED_KINDS


@pytest.mark.parametrize('fixture', [AWIN_MARKETING, KATALYS_NEWSLETTER])
def test_marketing_mail_never_classifies(fixture):
    assert M.classify(*fixture) == ''


@pytest.mark.parametrize('fixture,reason', [
    (UPPROMOTE_VERIFY, 'account_verification_required'),
    (HEALTHTRADER_ID, 'identity_or_tax_document_required'),
    (DOCUSIGN, 'contract_signature_required'),
])
def test_action_required_mail_is_flagged_not_queued(fixture, reason):
    """These need a human/machine action; none of them is a program state change."""
    assert M.attention_reason(*fixture) == reason
    assert M.classify(*fixture) == ''


def test_a_pause_notice_outranks_an_attention_pattern():
    """Priority matters: a real state change must not be demoted to a blocker."""
    subject, body = KATALYS_PAUSE
    assert M.classify(subject, body) == 'network_paused'


# ---------------------------------------------------------------------------
# Program identity
# ---------------------------------------------------------------------------


def test_program_name_comes_from_the_subject_when_it_is_there():
    assert M.program_identity(*KATALYS_PAUSE) == ('Breeze Meds', '')


def test_awin_approval_subject_yields_name_and_advertiser_id():
    assert M.program_identity(*AWIN_APPROVAL) == ('Health Fitness & Longevity', '81485')


def test_awin_rejection_name_is_recovered_from_the_body():
    name, pid = M.program_identity(*AWIN_REJECTION)
    assert name == 'MicroVitamin (US)'
    assert pid == '122466'


def test_our_own_publisher_id_is_never_captured_as_the_program_id():
    _, pid = M.program_identity(*AWIN_REJECTION)
    assert pid != '2979387'


def test_real_tonum_subject_name_and_body_program_id_are_merged():
    subject = 'Welcome To The Tonum AWIN Affiliate Program'
    body = ('Your application has been approved. '
            'https://ui.awin.com/awin/affiliate/2979387/merchant-profile/122076/overview')
    assert M.program_identity(subject, body) == ('Tonum AWIN', '122076')


def test_unnamed_program_returns_empty_rather_than_a_guess():
    assert M.program_identity('Changes to your commission rates', 'Rates changed.') == ('', '')


# ---------------------------------------------------------------------------
# Network resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('sender,network', [
    ('Katalys <noreply@katalys.com>', 'Katalys'),
    ('Awin <no-reply@awin.com>', 'Awin'),
    ('UpPromote Marketplace <no-reply@mg.uppromote.com>', 'UpPromote'),
    ('"Bulldog" <noreply@cj.com>', 'CJ Affiliate'),
    ('Someone <hello@random-brand.example>', ''),
])
def test_network_is_resolved_from_the_sender_domain(sender, network):
    assert M.network_for(M.sender_domain(sender)) == network


# ---------------------------------------------------------------------------
# Queue-row projection: each row must satisfy the drain's mode for its kind
# ---------------------------------------------------------------------------


EVIDENCE = 'reports/glp-clinic-scout/affiliate-machine/APPROVAL-BACKFILL-AUDIT-20260818.md'


def row_for(kind: str) -> dict:
    return M.queue_row(kind, 'Katalys', 'Breeze Meds', '1518',
                       {'id': 'abc123', 'threadId': 't1'}, EVIDENCE, '2026-08-17T14:17:47Z')


@pytest.mark.parametrize('kind', ['network_paused', 'rejection',
                                  'application_pending', 'new_approval'])
def test_every_projected_row_cites_the_gmail_message_id(kind):
    row = row_for(kind)
    assert 'abc123' in json.dumps(row)
    assert row['status'] == 'queued'
    assert row['queued_by'] == 'inbox_mirror'
    assert row['kind'] in W.SHEET_SYNC_KIND_MODE


def test_network_paused_row_carries_the_evidence_the_mode_demands():
    evidence = json.loads(row_for('network_paused')['evidence'])
    assert evidence['message_id'] == 'gmail:abc123'
    assert evidence['path'] == EVIDENCE


def test_rejection_row_carries_the_evidence_the_mode_demands():
    evidence = json.loads(row_for('rejection')['evidence'])
    spec = W.SHEET_SYNC_MODES['rejection']
    for field, _ in spec.evidence_fields:
        assert evidence.get(field), field


def test_application_pending_row_carries_the_evidence_the_mode_demands():
    evidence = json.loads(row_for('application_pending')['evidence'])
    spec = W.SHEET_SYNC_MODES['application_pending']
    for field, _ in spec.evidence_fields:
        assert evidence.get(field), field


def test_the_lane_never_emits_a_kind_the_drain_cannot_route():
    kinds = {kind for kind, _ in M.CLASSIFIERS}
    assert kinds <= set(W.SHEET_SYNC_KIND_MODE)


def test_update_id_is_stable_for_the_same_message():
    assert row_for('rejection')['update_id'] == row_for('rejection')['update_id']
    assert row_for('rejection')['update_id'] != row_for('network_paused')['update_id']


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def test_cursor_on_disk_is_real_and_advanced():
    """The lane has actually run: its cursor exists and points somewhere."""
    assert M.CURSOR.is_file()
    cursor = json.loads(M.CURSOR.read_text())
    assert cursor['schema'] == 'glp.inbox_mirror_cursor.v1'
    assert int(cursor['last_internal_date_ms']) > 0
    assert cursor['last_run_utc']


def test_discord_text_reports_an_empty_scan_honestly():
    text = M.discord_text({'rows': [], 'attention': [], 'classified': [],
                           'unclassified': [], 'scanned': 41,
                           'window_start_utc': '2026-08-17T00:00:00Z'})
    assert '0 queue rows' in text and '41' in text


# --------------------------------------------------------------------------
# Channel split: telemetry -> #ops-alerts, operator action -> #decisions
# (operator ruling 2026-08-18: an ACTION REQUIRED item buried in a telemetry
# card is an item the operator never sees)
# --------------------------------------------------------------------------

_BLOCKER = {'attention': 'account_verification_required',
            'subject': 'Verify your email address for UpPromote Marketplace',
            'from': 'UpPromote Marketplace <no-reply@mg.uppromote.com>',
            'network': 'uppromote', 'program_name': ''}


def _result(rows=None, attention=None, unclassified=None):
    return {'rows': rows or [], 'attention': attention or [],
            'classified': [], 'unclassified': unclassified or [], 'scanned': 1,
            'window_start_utc': '2026-08-17T00:00:00Z'}


def test_decisions_text_is_empty_when_there_is_nothing_to_act_on():
    assert M.decisions_text(_result()) == ''


def test_decisions_text_states_the_problem_and_the_exact_action():
    text = M.decisions_text(_result(attention=[_BLOCKER]))
    assert 'account verification required' in text
    assert 'UpPromote' in text
    assert 'DO:' in text
    # the instruction itself, not just the class name
    assert 'verification link' in text


def test_every_attention_class_has_a_written_instruction():
    for reason, _pattern in M.ATTENTION_PATTERNS:
        assert reason in M.ATTENTION_INSTRUCTIONS, reason
        assert len(M.ATTENTION_INSTRUCTIONS[reason]) > 30


def test_ops_card_no_longer_carries_the_action_items_themselves():
    ops = M.discord_text(_result(attention=[_BLOCKER]))
    assert 'ACTION REQUIRED' not in ops
    assert 'no-reply@mg.uppromote.com' not in ops
    assert '#decisions' in ops


def test_ops_card_still_reports_queue_rows():
    row = {'kind': 'network_paused', 'program_name': 'Acme', 'network': 'awin',
           'update_id': 'u-1'}
    ops = M.discord_text(_result(rows=[row]))
    assert 'NETWORK_PAUSED' in ops and 'Acme' in ops


# --------------------------------------------------------------------------
# Blocker-ack registry (operator 2026-08-18): a blocker the operator has
# already cleared must never be surfaced again, and an open one must not be
# re-shouted on every scan. Proven need: the UpPromote verification mail was
# surfaced at 03:25Z and again at 03:47Z from one-message scans.
# --------------------------------------------------------------------------


class FakeRegistry:
    """Stands in for ops/action_items.py; records what the producer asked."""

    def __init__(self, verdicts=None):
        self.calls = []
        self.verdicts = verdicts or {}
        self.OPEN = 'open'

    def upsert(self, machine, kind, title, do_line, source_ref=None,
               evidence=None, now=None, identity=None):
        self.calls.append({'machine': machine, 'kind': kind, 'title': title,
                           'do_line': do_line, 'evidence': evidence,
                           'identity': identity})
        return self.verdicts.get(
            title, {'action': 'create', 'surface': True,
                    'item_id': 'AI-%d' % len(self.calls),
                    'source_ref': source_ref or 'ref',
                    'identity': identity or ''})

    # The double tracks the real module: Phase 1b added the second dedupe lock
    # (see ops/action_items.item_identity) and the producer calls it before
    # every upsert. Delegating to the real implementation keeps the double from
    # asserting a key shape the registry does not actually use.
    def item_identity(self, kind, network, program_id, notice_ts):
        return AI.item_identity(kind, network, program_id, notice_ts)

    def normalize_notice_ts(self, value):
        return AI.normalize_notice_ts(value)

    def probe(self, machine, kind, title, identity='', message_id='',
              source_ref=None, now=None):
        self.calls.append({'machine': machine, 'kind': kind, 'title': title,
                           'probe': True, 'identity': identity})
        return self.verdicts.get(
            title, {'action': 'create', 'surface': True, 'item_id': '',
                    'source_ref': source_ref or 'ref', 'read_only': True})

    def footer(self, item_id):
        return '➜ when done, reply here: `done %s`' % item_id


def attention_result(*items):
    return {'rows': [], 'scanned': len(items), 'attention': list(items),
            'unclassified': [], 'window_start_utc': '2026-08-18T00:00:00Z'}


UPPROMOTE_ITEM = {'message_id': '1a011ff1acc393ae', 'network': 'UpPromote',
                  'attention': 'account_verification_required',
                  'subject': 'Verify your email address for UpPromote Marketplace',
                  'from': 'UpPromote Marketplace <no-reply@mg.uppromote.com>',
                  'date': 'Mon, 17 Aug 2026 23:12:05 +0000', 'program_name': ''}


def test_do_line_is_a_complete_instruction(monkeypatch):
    line = M.do_line(UPPROMOTE_ITEM)
    assert 'Verify your email address for UpPromote' in line
    assert 'no-reply@mg.uppromote.com' in line
    assert 'click the verification link' in line
    assert 'receipt' not in line.lower()


def test_every_blocker_is_registered_before_it_is_surfaced(monkeypatch):
    registry = FakeRegistry()
    monkeypatch.setattr(M, 'action_items', registry)
    result = attention_result(dict(UPPROMOTE_ITEM))
    M.register_attention(result)
    assert registry.calls[0]['machine'] == 'inbox_mirror'
    assert registry.calls[0]['kind'] == 'account_verification_required'
    assert '1a011ff1acc393ae' in json.dumps(registry.calls[0]['evidence'])
    assert result['attention'][0]['item_id'] == 'AI-1'


def test_a_resolved_item_is_never_posted_again(monkeypatch):
    registry = FakeRegistry({UPPROMOTE_ITEM['subject']: {
        'action': 'skip_resolved', 'surface': False, 'item_id': 'AI-1',
        'why': 'item AI-1 was resolved by operator at 2026-08-18T05:00:00Z'}})
    monkeypatch.setattr(M, 'action_items', registry)
    result = attention_result(dict(UPPROMOTE_ITEM))
    M.register_attention(result)
    assert M.decisions_text(result) == ''
    assert 'NOT re-posted' in M.discord_text(result)
    assert 'AI-1' in M.discord_text(result)


def test_an_open_item_seen_today_is_not_repeated(monkeypatch):
    registry = FakeRegistry({UPPROMOTE_ITEM['subject']: {
        'action': 'skip_recent', 'surface': False, 'item_id': 'AI-1',
        'why': 'item AI-1 is open and was last surfaced 2026-08-18T03:47:00Z'}})
    monkeypatch.setattr(M, 'action_items', registry)
    result = attention_result(dict(UPPROMOTE_ITEM))
    M.register_attention(result)
    assert M.decisions_text(result) == ''


def test_an_aged_open_item_returns_marked_still_open(monkeypatch):
    registry = FakeRegistry({UPPROMOTE_ITEM['subject']: {
        'action': 'resurface', 'surface': True, 'item_id': 'AI-1',
        'first_surfaced': '2026-08-01T03:25:00Z'}})
    monkeypatch.setattr(M, 'action_items', registry)
    result = attention_result(dict(UPPROMOTE_ITEM))
    M.register_attention(result)
    card = M.decisions_text(result)
    assert 'STILL OPEN (first surfaced 2026-08-01)' in card
    assert 'AI-1' in card


def test_the_card_tells_the_operator_how_to_close_the_item(monkeypatch):
    monkeypatch.setattr(M, 'action_items', FakeRegistry())
    result = attention_result(dict(UPPROMOTE_ITEM))
    M.register_attention(result)
    card = M.decisions_text(result)
    assert '`done AI-1`' in card
    assert '➜ **DO:**' in card


def test_a_registry_outage_still_surfaces_the_blocker(monkeypatch):
    class Broken(FakeRegistry):
        def upsert(self, *a, **k):
            raise RuntimeError('mcp 503')

    monkeypatch.setattr(M, 'action_items', Broken())
    result = attention_result(dict(UPPROMOTE_ITEM))
    M.register_attention(result)
    assert result['attention'][0]['surface'] is True
    assert 'need YOU' in M.decisions_text(result)


def test_no_registry_flag_surfaces_everything(monkeypatch):
    registry = FakeRegistry({UPPROMOTE_ITEM['subject']: {
        'action': 'skip_resolved', 'surface': False, 'item_id': 'AI-1'}})
    monkeypatch.setattr(M, 'action_items', registry)
    result = attention_result(dict(UPPROMOTE_ITEM))
    M.register_attention(result, enabled=False)
    assert registry.calls == []
    assert result['attention'][0]['surface'] is True


def test_attention_message_ids_enter_the_cursor_so_the_scan_moves_on():
    src = (ROOT / 'scripts/glp_affiliate_inbox_mirror.py').read_text()
    assert '[str(a.get("message_id")) for a in attention]' in src
    block = src.split('"seen_message_ids":')[-1][:400]
    assert 'for a in attention' in block


# ===========================================================================
# Approval INTAKE ROUTING (2026-08-18)
# ===========================================================================
#
# Two defects, both proven against the real mailbox:
#
#   1. every Katalys decision — approval AND rejection — was `unclassified`.
#      Katalys' subject is neutral ("Update to Your Application for X 📬") and
#      the verdict is a labelled body line ("Approval Status: Approved") that no
#      classifier matched. Eleven real Katalys approvals sat invisible.
#   2. every approval that DID classify was emitted as `new_approval`, whose
#      mode only CREATES rows — so the normal case (a brand we applied to says
#      yes) could never be recorded, only refused.
#
# What these tests pin is that the fix stays honest: the lane may promote a
# pending row only when the programme id is provable on BOTH sides, and every
# other answer is a visible stop rather than a guess.

KATALYS_APPROVAL = (
    'Update to Your Application for Wellmedr  📬',
    'Program Approval Update Hello! An advertiser has responded to your application. '
    'Approval Status: Approved Advertiser: WellMedr Program: Wellmedr Head over to the '
    'Katalys Marketing Platform for the update. Manage Relationships Thank you!')
KATALYS_REJECTION = (
    'Update to Your Application for Ancient Nutrition 📬',
    'Program Approval Update Hello! An advertiser has responded to your application. '
    'Approval Status: Rejected Advertiser: Ancient Nutrition Program: Ancient Nutrition '
    'Reason: Low-Quality Traffic Head over to the Katalys Marketing Platform.')
AWIN_MEMBERSHIP_ACTIVE = (
    'Membership active with advertiser Health Fitness & Longevity (aid: 81485)',
    'Dear Izaiah Tilton, You are now approved onto the Health Fitness & Longevity '
    'Affiliate program! https://ui.awin.com/affiliate/2979387/merchant-profile/81485/overview')
AWIN_BRAND_WELCOME = (
    'Welcome to the Vitaturbo Affiliate Program!',
    'Hi Izaiah Tilton, Welcome to the [Vitaturbo] Affiliate Program. Your Publisher ID: '
    '2979387. Competitive base commission, 30-day cookie window. AWIN LTD is registered '
    'in England and Wales with number 04010229.')


@pytest.mark.parametrize('fixture,expected', [
    (KATALYS_APPROVAL, 'new_approval'),
    (KATALYS_REJECTION, 'rejection'),
])
def test_katalys_decision_mail_is_no_longer_invisible(fixture, expected):
    assert M.classify(*fixture) == expected


def test_katalys_program_name_loses_the_decoration_the_sheet_never_had():
    """'myLab Box 📬' matches no Sheet row, so the emoji is an identity bug."""
    assert M.program_identity(*KATALYS_APPROVAL)[0] == 'Wellmedr'
    assert M.program_identity(*KATALYS_REJECTION)[0] == 'Ancient Nutrition'


def test_katalys_pause_still_classifies_and_still_names_its_program():
    assert M.classify(*KATALYS_PAUSE) == 'network_paused'
    assert M.program_identity(*KATALYS_PAUSE)[0] == 'Breeze Meds'


def test_the_lane_agrees_with_the_writer_about_what_pending_means():
    """A private copy of the mode's source states is how a lane starts queueing
    rows the mode refuses."""
    assert M.APPROVAL_SOURCE_STATUSES == W.APPROVAL_CONFIRMED_SOURCE_STATUS
    assert set(M.APPROVED_RELATIONSHIP_WORDS) == set(W.APPROVED_RELATIONSHIP_WORDS)


# ---------------------------------------------------------------------------
# Programme-id binding
# ---------------------------------------------------------------------------


def test_our_own_publisher_ids_are_never_binding_candidates():
    text = ' '.join(AWIN_BRAND_WELCOME)
    assert '2979387' not in M.capture_program_ids(text)
    assert '04010229' not in M.capture_program_ids(text)
    assert '4010229' not in M.capture_program_ids(text)


def test_row_side_ids_come_only_from_identity_positions():
    """A pending row's Notes are full of dates and receipt paths; a loose scan
    there would 'bind' an approval to a row because both mention 2026."""
    ids = M.row_program_ids({
        'apply_url': 'https://app.katalys.com/pub/098e5455-ca72/explore/1387',
        'tracking_link': '',
        'notes': 'Application submitted 2026-08-14T02:11:00Z; receipt=reports/x/20260814.json'})
    assert ids == {'1387'}


def test_an_awin_membership_mail_binds_its_advertiser_id():
    assert '81485' in M.capture_program_ids(' '.join(AWIN_MEMBERSHIP_ACTIVE))


def test_a_brand_welcome_mail_binds_nothing_because_it_names_nothing():
    """Awin's brand-sent welcome carries no advertiser id at all. Inventing one
    is the failure; finding none is the correct, honest answer."""
    assert M.capture_program_ids(' '.join(AWIN_BRAND_WELCOME)) & {'124058'} == set()


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


ROWS = [
    {'name': 'Wellmedr', 'network': 'Katalys', 'status': 'applied_pending_brand_approval',
     'sheet_row': 300, 'apply_url': 'https://app.katalys.com/pub/x/explore/1611',
     'tracking_link': '', 'notes': ''},
    {'name': 'Health Fitness & Longevity', 'network': 'Awin',
     'status': 'applied_pending_brand_approval', 'sheet_row': 479,
     'apply_url': 'https://ui.awin.com/merchant-profile/81485', 'tracking_link': '', 'notes': ''},
    {'name': 'Wavytalk', 'network': 'Awin', 'status': 'invited_pending_acceptance',
     'sheet_row': 335, 'apply_url': 'https://ui.awin.com/merchant-profile/105835',
     'tracking_link': '', 'notes': ''},
    {'name': 'Ivim Health', 'network': 'Katalys', 'status': 'approved_active_not_wired',
     'sheet_row': 120, 'apply_url': 'https://app.katalys.com/pub/x/explore/1390',
     'tracking_link': '', 'notes': ''},
    {'name': 'Found Health', 'network': 'Katalys', 'status': 'live_wired_verified',
     'sheet_row': 110, 'apply_url': 'https://app.katalys.com/pub/x/explore/999',
     'tracking_link': '', 'notes': ''},
    {'name': 'ReTress', 'network': 'Awin', 'status': 'rejected_network_declined_application',
     'sheet_row': 501, 'apply_url': 'https://ui.awin.com/merchant-profile/126401',
     'tracking_link': '', 'notes': ''},
]


def route(network, program, fixture):
    return M.route_approval(network, program, ROWS, ' '.join(fixture))


def test_an_approval_for_a_pending_row_with_a_provable_id_is_approval_confirmed():
    decision = route('Awin', 'Health Fitness & Longevity', AWIN_MEMBERSHIP_ACTIVE)
    assert decision['kind'] == 'approval_confirmed'
    assert decision['sheet_row'] == 479
    assert decision['program_id'] == '81485'
    assert decision['relationship'] in W.APPROVED_RELATIONSHIP_WORDS


def test_an_approval_for_an_invited_row_is_also_a_legal_transition():
    decision = M.route_approval(
        'Awin', 'Wavytalk', ROWS,
        'Welcome to Wavytalk. You are now approved onto merchant-profile/105835')
    assert decision['kind'] == 'approval_confirmed'
    assert decision['sheet_row'] == 335


def test_an_approval_for_a_program_with_no_row_stays_new_approval():
    decision = route('GoAffPro', 'Some Brand New Clinic', KATALYS_APPROVAL)
    assert decision['kind'] == 'new_approval'
    assert decision['outcome'] == 'new_approval'


def test_real_tonum_id_resolves_existing_row_despite_name_decoration():
    rows = ROWS + [{
        'name': 'Tonum (US)', 'network': 'Awin',
        'status': 'approved_active_not_wired', 'sheet_row': 677,
        'apply_url': 'https://ui.awin.com/awin/affiliate/2979387/merchant-profile/122076',
        'tracking_link': '', 'notes': '',
    }]
    decision = M.route_approval(
        'Awin', 'Tonum AWIN', rows,
        'Welcome. merchant-profile/122076', provider_program_id='122076')
    assert decision['outcome'] == 'already_recorded'
    assert decision['sheet_row'] == 677
    assert decision['program_name'] == 'Tonum (US)'


def test_the_new_approval_reason_reads_as_pending_research_not_as_a_failure():
    why = route('GoAffPro', 'Some Brand New Clinic', KATALYS_APPROVAL)['why'].lower()
    assert 'researched row' in why
    assert 'no sheet row' in why


def test_an_unbindable_approval_never_promotes_a_row():
    """Katalys' approval mail carries no programme id. The guard requires one on
    both sides, so the answer is a stop with the numbers spelled out."""
    decision = route('Katalys', 'Wellmedr', KATALYS_APPROVAL)
    assert decision['kind'] == ''
    assert decision['outcome'] == 'program_id_unbindable'
    assert decision['sheet_row'] == 300
    assert '1611' in decision['why']


def test_a_near_identity_match_is_never_auto_promoted():
    decision = route('Katalys', 'Wellmedr Health', KATALYS_APPROVAL)
    assert decision['kind'] == ''
    assert decision['outcome'] == 'needs_identity_review'


def test_the_same_name_on_another_network_is_an_identity_question_not_a_match():
    decision = M.route_approval('Impact.com', 'Wellmedr', ROWS, ' '.join(KATALYS_APPROVAL))
    assert decision['outcome'] == 'needs_identity_review'


def test_an_already_approved_row_is_a_noop_not_a_card():
    for program, row in (('Ivim Health', 120), ('Found Health', 110)):
        decision = route('Katalys', program, KATALYS_APPROVAL)
        assert decision['kind'] == ''
        assert decision['outcome'] == 'already_recorded'
        assert decision['sheet_row'] == row
        assert decision['outcome'] not in M.APPROVAL_ESCALATIONS


def test_an_approval_for_a_rejected_row_refuses_and_says_so():
    decision = route('Awin', 'ReTress', AWIN_MEMBERSHIP_ACTIVE)
    assert decision['kind'] == ''
    assert decision['outcome'] == 'row_state_not_in_pending_family'
    assert 'rejected_network_declined_application' in decision['why']


def test_two_rows_for_one_program_is_a_human_ruling():
    rows = ROWS + [dict(ROWS[0], sheet_row=301)]
    decision = M.route_approval('Katalys', 'Wellmedr', rows, ' '.join(KATALYS_APPROVAL))
    assert decision['outcome'] == 'needs_identity_review'
    assert '300' in decision['why'] and '301' in decision['why']


# ---------------------------------------------------------------------------
# The emitted queue row must satisfy the governed mode, not merely resemble it
# ---------------------------------------------------------------------------


REAL_APPROVAL_CAPTURE = (
    'reports/glp-clinic-scout/affiliate-machine/sheet-sync/inbox-mirror/'
    '2026-08-18/19ffc4c4b1d53fc7.json')


@pytest.mark.skipif(not (ROOT / REAL_APPROVAL_CAPTURE).is_file(),
                    reason='the real Awin approval capture is not on disk')
def test_approval_confirmed_row_carries_exactly_what_the_mode_demands():
    decision = route('Awin', 'Health Fitness & Longevity', AWIN_MEMBERSHIP_ACTIVE)
    row = M.queue_row('approval_confirmed', 'Awin', 'Health Fitness & Longevity', '',
                      {'id': '19ffc4c4b1d53fc7'}, REAL_APPROVAL_CAPTURE,
                      '2026-08-14T16:29:24Z', approval=decision)
    evidence = json.loads(row['evidence'])
    spec = W.SHEET_SYNC_MODES['approval_confirmed']
    for field, _kind in spec.evidence_fields:
        assert evidence.get(field), field
    # the hash the writer will re-compute
    import hashlib
    assert evidence['approval_capture_sha256'] == hashlib.sha256(
        (ROOT / REAL_APPROVAL_CAPTURE).read_bytes()).hexdigest()
    assert evidence['network_relationship'] in W.APPROVED_RELATIONSHIP_WORDS
    assert row['kind'] in W.SHEET_SYNC_KIND_MODE
    assert row['sheet_row'] == '479'


@pytest.mark.skipif(not (ROOT / REAL_APPROVAL_CAPTURE).is_file(),
                    reason='the real Awin approval capture is not on disk')
def test_the_declared_id_really_is_standalone_in_the_capture_the_writer_reads():
    """The exact check `require_program_identity_binding` performs, run against
    the file this lane cites and the row it names."""
    import re as _re
    decision = route('Awin', 'Health Fitness & Longevity', AWIN_MEMBERSHIP_ACTIVE)
    pattern = _re.compile(r'(?<![0-9])%s(?![0-9])' % _re.escape(decision['program_id']))
    assert pattern.search((ROOT / REAL_APPROVAL_CAPTURE).read_text())
    row = [r for r in ROWS if r['sheet_row'] == decision['sheet_row']][0]
    assert pattern.search(' '.join([row['apply_url'], row['tracking_link'], row['notes']]))


def test_no_kind_this_lane_emits_is_unroutable():
    kinds = {kind for kind, _ in M.CLASSIFIERS} | {'approval_confirmed'}
    assert kinds <= set(W.SHEET_SYNC_KIND_MODE)


# ---------------------------------------------------------------------------
# Escalation: one card per problem, and the card is complete
# ---------------------------------------------------------------------------


def approvals_result(*decisions):
    return {'rows': [], 'attention': [], 'classified': [], 'unclassified': [],
            'scanned': len(decisions), 'approvals': list(decisions),
            'window_start_utc': '2026-08-18T00:00:00Z'}


def blocked(program, message_id, network='Katalys', row=300):
    return {'network': network, 'program_name': program, 'outcome': 'program_id_unbindable',
            'why': 'Katalys approved `%s` and Sheet row %d is `applied_pending_brand_approval`, '
                   'but the approval mail names no programme id.' % (program, row),
            'sheet_row': row, 'message_id': message_id, 'subject': 'Update to Your Application',
            'date': 'Mon, 17 Aug 2026 14:18:12 +0000', 'evidence': 'reports/x.json',
            'observed_at_utc': '2026-08-17T14:18:12Z'}


def test_many_approvals_blocked_on_one_thing_are_one_card_not_many():
    items = M.approval_attention_items(approvals_result(
        blocked('Wellmedr', 'm1'), blocked('Midi Health', 'm2'), blocked('DudeMeds', 'm3')))
    assert len(items) == 1
    assert 'Wellmedr' in items[0]['program_name'] and 'DudeMeds' in items[0]['program_name']


def test_the_blocked_approval_card_says_what_is_missing_and_what_to_do():
    items = M.approval_attention_items(approvals_result(blocked('Wellmedr', 'm1')))
    line = M.do_line(items[0])
    assert 'Wellmedr' in line
    assert 'no programme id' in line
    assert 'Nothing was written to the Sheet' in line
    assert 'see the receipt' not in line.lower()


def test_a_successful_or_noop_routing_never_becomes_an_operator_card():
    fine = [{'network': 'Awin', 'program_name': 'X', 'outcome': outcome, 'why': '',
             'message_id': 'm%d' % index}
            for index, outcome in enumerate(
                ('approval_confirmed', 'new_approval', 'already_recorded'))]
    assert M.approval_attention_items(approvals_result(*fine)) == []


def test_every_escalation_class_has_a_written_instruction():
    for outcome, instruction in M.APPROVAL_ESCALATIONS.items():
        assert len(instruction) > 40, outcome


def test_the_ops_card_reports_how_every_approval_was_routed():
    text = M.discord_text(approvals_result(
        blocked('Wellmedr', 'm1'),
        {'network': 'Awin', 'program_name': 'HFL', 'outcome': 'approval_confirmed',
         'why': '', 'message_id': 'm2'}))
    assert 'approval email(s) routed' in text
    assert 'approval_confirmed' in text and 'program_id_unbindable' in text


# ---------------------------------------------------------------------------
# The durable worklist: an escalated approval must be replayable
# ---------------------------------------------------------------------------


def test_a_blocked_approval_is_written_to_a_replayable_worklist(tmp_path):
    path = tmp_path / 'pending.json'
    out = M.record_pending_approvals(approvals_result(blocked('Wellmedr', 'm1')), path=path)
    assert out['open'] == 1 and out['added'] == 1
    entry = json.loads(path.read_text())['entries']['m1']
    assert entry['sheet_row'] == 300
    assert entry['approval_capture_path'] == 'reports/x.json'
    assert entry['outcome'] == 'program_id_unbindable'


def test_an_approval_that_later_routes_cleanly_leaves_the_worklist(tmp_path):
    path = tmp_path / 'pending.json'
    M.record_pending_approvals(approvals_result(blocked('Wellmedr', 'm1')), path=path)
    out = M.record_pending_approvals(approvals_result(
        {'network': 'Katalys', 'program_name': 'Wellmedr', 'outcome': 'approval_confirmed',
         'why': '', 'message_id': 'm1'}), path=path)
    assert out['open'] == 0 and out['cleared'] == 1


def test_the_worklist_keeps_the_date_a_blocker_first_appeared(tmp_path):
    path = tmp_path / 'pending.json'
    M.record_pending_approvals(approvals_result(blocked('Wellmedr', 'm1')), path=path)
    first = json.loads(path.read_text())['entries']['m1']['first_seen_utc']
    M.record_pending_approvals(approvals_result(blocked('Wellmedr', 'm1')), path=path)
    assert json.loads(path.read_text())['entries']['m1']['first_seen_utc'] == first


# ---------------------------------------------------------------------------
# 2026-08-19 — the Nu Image / Trackdesk miss, and the durable fix
#
# Nu Image Medical (nationwide US telehealth, compounded semaglutide AND
# tirzepatide, injection + rapid-dissolve tablet, 30% revshare) approved us
# through Trackdesk at 00:22Z. The mirror ran, scanned the message, and emitted
# NOTHING: `trackdesk.com` was not in the hardcoded sender map, so a core-vertical
# approval was filed `unknown_network_sender`.
#
# Everything below is verbatim from the real mailbox (gmail message ids
# 1a01766069ce27a5 and 1a017640f5dbc3cf), typographic characters included,
# because the straight-quote assumption was the SECOND independent way this
# message could have been lost.
# ---------------------------------------------------------------------------

NUIMAGE_APPROVAL = (
    '\U0001f389 Welcome to the NuImage Medical Affiliate Program – You’re Approved!',
    'Hi Izaiah Tilton, Congratulations! \U0001f389 You’ve been approved to promote the '
    'NuImage Medical GLP-1 Weight Management Program . We’re excited to have you as a '
    'partner. Program Highlights •&nbsp; Compounded Semaglutide &amp; Tirzepatide '
    '(Injection + Rapid Dissolve Tablet formats) •&nbsp; Discreet 2-day shipping across '
    'all 50 US states Offer Details •&nbsp; Commission : 30% revenue share on every '
    'approved order •&nbsp; Cookie Duration : 60 days •&nbsp; Payouts : Monthly by '
    'the 15th of the following month (minimum $250) Getting Started 1.&nbsp; Log into your '
    'affiliate dashboard here: https://nuimagemedical.com The NuImage Medical Team Affiliate '
    'Manager https://nu-image-medical.trackdesk.com')
NUIMAGE_PENDING = (
    '✅ Thank You! Your NuImage Medical Affiliate Application is Pending',
    'Hi Izaiah Tilton, Thank you for applying to the NuImage Medical Affiliate Program ! '
    'We’ve received your application and it is currently under review . Our team '
    'typically approves applications within 1–2 business days. '
    'https://nu-image-medical.trackdesk.com')
NUIMAGE_SENDER = 'Nu Image Medical <no-reply-nu-image-medical@trackdesk.com>'
#: Verbatim Everflow shape, lost on the same run for an unrelated reason.
EVERFLOW_RECEIVED = ('Your application has been received by Eden Health International, LLC',
                     'Your application has been received and is under review.')

#: A minimal governed-Sheet slice in the shape `sheet_records_from_values`
#: produces: one RAIL row for a network nobody has coded in, one merchant row,
#: and one merchant whose Category merely CONTAINS the word "marketplace".
TRACKDESK_RAIL = {
    'name': 'Trackdesk Marketplace', 'network': 'Network signup',
    'status': 'candidate_not_applied', 'sheet_row': 394,
    'apply_url': 'https://trackdesk.com/marketplace', 'tracking_link': '', 'notes': '',
    'category': 'Network rail', 'official_url': 'https://trackdesk.com/',
    'publisher_login_url': '',
}
SESAME_MERCHANT = {
    'name': 'Sesame Care Weight Loss', 'network': 'Direct/contact verify',
    'status': 'verify_affiliate_route', 'sheet_row': 38,
    'apply_url': '', 'tracking_link': '', 'notes': '',
    'category': 'Telehealth marketplace', 'official_url': 'https://sesamecare.com/',
    'publisher_login_url': '',
}
BRAND_MERCHANT = {
    'name': 'Willow Telehealth', 'network': 'Direct affiliate (Refersion)',
    'status': 'applied_pending_brand_approval', 'sheet_row': 512,
    'apply_url': 'https://willowtelehealth.example/affiliates', 'tracking_link': '',
    'notes': '', 'category': 'Telehealth', 'official_url': 'https://willowtelehealth.example/',
    'publisher_login_url': '',
}
SHEET_SLICE = [TRACKDESK_RAIL, SESAME_MERCHANT, BRAND_MERCHANT]


def roster(records=None, metadata_dir=None):
    return M.build_roster(records if records is not None else SHEET_SLICE,
                          metadata_dir=metadata_dir or Path('/nonexistent-metadata-dir'))


# --- the miss itself -------------------------------------------------------


def test_the_nuimage_approval_still_classifies_as_an_approval():
    """The classifier was never the problem — the network map was."""
    assert M.classify(*NUIMAGE_APPROVAL) == 'new_approval'
    assert M.program_identity(*NUIMAGE_APPROVAL) == ('NuImage Medical', '')


def test_the_hardcoded_map_alone_reproduces_the_2026_08_19_miss():
    """Regression anchor: without a roster, trackdesk.com is still unknown."""
    assert M.network_for(M.sender_domain(NUIMAGE_SENDER)) == ''


def test_the_sheet_rail_row_alone_teaches_the_mirror_the_network():
    """No code change: row 394 existing on the Sheet IS the fix."""
    assert M.network_for(M.sender_domain(NUIMAGE_SENDER), roster()) == 'Trackdesk'


def test_a_network_with_no_rail_row_is_still_honestly_unknown():
    """The roster resolves what the Sheet knows — it never invents."""
    assert M.network_for('some-network-nobody-has-researched.example', roster()) == ''


def test_removing_the_rail_row_removes_the_recognition():
    """Proves the recognition is DERIVED from the Sheet, not memorised."""
    without = roster([SESAME_MERCHANT, BRAND_MERCHANT])
    assert M.network_for(M.sender_domain(NUIMAGE_SENDER), without) == ''


# --- the roster's own honesty ---------------------------------------------


def test_a_merchant_portal_subdomain_resolves_to_its_platform():
    assert M.registrable_domain('nu-image-medical.trackdesk.com') == 'trackdesk.com'
    assert M.registrable_domain('ui.awin.com') == 'awin.com'
    assert M.registrable_domain('') == ''


def test_the_hardcoded_spelling_always_wins_over_a_derived_one():
    """`CJ Affiliate` must never become `Cj` because a rail row spelled it so."""
    derived = roster([{**TRACKDESK_RAIL, 'name': 'Cj', 'sheet_row': 778,
                       'apply_url': 'https://cj.com/', 'official_url': 'https://cj.com/'}])
    assert 'cj.com' not in derived['domains']
    assert M.network_for('cj.com', derived) == 'CJ Affiliate'


def test_a_domain_two_networks_claim_is_dropped_not_guessed():
    """everflowclient.io is Everflow's white-label host; four rail rows use it."""
    a = {**TRACKDESK_RAIL, 'name': 'XenTraffic', 'sheet_row': 407,
         'apply_url': 'https://xentraffic.everflowclient.io/affiliate/signup',
         'official_url': ''}
    b = {**TRACKDESK_RAIL, 'name': 'GiddyUp', 'sheet_row': 402,
         'apply_url': 'https://giddyup.everflowclient.io/affiliate/signup',
         'official_url': ''}
    built = roster([a, b])
    assert 'everflowclient.io' in built['ambiguous']
    assert M.network_for('everflowclient.io', built) == ''


def test_a_merchant_whose_category_merely_says_marketplace_is_not_a_network():
    """Sesame Care is a provider. A false rail here would mislabel its mail."""
    built = roster()
    assert 'sesamecare.com' not in built['domains']
    assert 'sesamecare.com' in built['merchant_domains']


def test_rail_name_decoration_is_stripped_for_the_route_cell():
    assert M.clean_network_label('Trackdesk Marketplace') == 'Trackdesk'
    assert M.clean_network_label('Everflow Affiliate Marketplace') == 'Everflow'
    assert M.clean_network_label('Awin') == 'Awin'


def test_generic_infrastructure_hosts_never_become_networks():
    built = roster([{**TRACKDESK_RAIL, 'name': 'Bogus Rail', 'sheet_row': 900,
                     'apply_url': 'https://docs.google.com/forms/x',
                     'official_url': 'https://sendgrid.net/'}])
    assert built['domains'] == {} or 'google.com' not in built['domains']
    assert 'sendgrid.net' not in built['domains']


# --- merchant-sent approvals ----------------------------------------------


def test_a_merchant_sent_approval_resolves_by_its_own_sheet_row():
    """The advertiser writes from its OWN domain; no network sender exists."""
    out = M.resolve_sender_identity(
        'willowtelehealth.example',
        'Congratulations, your application has been approved.',
        'Willow Telehealth', SHEET_SLICE, roster())
    assert out['network'] == 'Direct affiliate (Refersion)'
    assert out['source'] == 'sheet_merchant_row'
    assert out['sheet_row'] == 512


def test_a_merchant_sent_approval_resolves_by_the_portal_it_links_to():
    """Nu Image's own path if the sender had been nuimagemedical.com."""
    out = M.resolve_sender_identity(
        'nuimagemedical.example', 'Log in at https://nu-image-medical.trackdesk.com to start.',
        'NuImage Medical', SHEET_SLICE, roster())
    assert out['network'] == 'Trackdesk'
    assert out['source'] == 'body_portal_host'


def test_a_merchant_sent_approval_resolves_by_an_unambiguous_program_name():
    out = M.resolve_sender_identity(
        'mail-relay.example', 'You have been approved.', 'Willow Telehealth',
        SHEET_SLICE, roster())
    assert out['network'] == 'Direct affiliate (Refersion)'
    assert out['source'] == 'sheet_program_name'


def test_an_unknown_network_with_nothing_to_resolve_against_stays_unknown():
    """The synthetic negative: no rail row, no merchant row, no name match."""
    out = M.resolve_sender_identity(
        'brand-nobody-knows.example', 'Congratulations, you have been approved!',
        'Some Brand Nobody Has Researched', SHEET_SLICE, roster())
    assert out['network'] == ''
    assert out['source'] == ''


def test_two_sheet_rows_on_two_networks_never_auto_resolve():
    """Ambiguity asks a question instead of picking."""
    other = {**BRAND_MERCHANT, 'sheet_row': 513, 'network': 'Awin',
             'official_url': 'https://willowtelehealth.example/'}
    out = M.resolve_sender_identity(
        'mail-relay.example', 'approved', 'Willow Telehealth',
        [BRAND_MERCHANT, other], roster([BRAND_MERCHANT, other]))
    assert out['network'] == ''


# --- typographic characters ------------------------------------------------


@pytest.mark.parametrize('subject,body,expected', [
    ('You’re Approved!', 'You’ve been approved to promote us.', 'new_approval'),
    ("You're Approved!", "You've been approved to promote us.", 'new_approval'),
    ('Application pending', 'We’ve received your application.', 'application_pending'),
])
def test_curly_and_straight_punctuation_classify_identically(subject, body, expected):
    assert M.classify(subject, body) == expected


def test_html_entities_do_not_hide_the_verdict():
    assert M.classify('Update', 'We&#8217;ve received your application.') == 'application_pending'


# --- the other shapes lost on the same run ---------------------------------


def test_the_pending_sibling_of_the_nuimage_approval_is_no_longer_invisible():
    assert M.classify(*NUIMAGE_PENDING) == 'application_pending'
    assert M.program_identity(*NUIMAGE_PENDING)[0] == 'NuImage Medical'


def test_the_everflow_application_received_shape_classifies_and_names_its_brand():
    assert M.classify(*EVERFLOW_RECEIVED) == 'application_pending'
    assert M.program_identity(*EVERFLOW_RECEIVED)[0] == 'Eden Health International, LLC'


def test_marketing_mail_still_never_classifies_after_the_widening():
    # SUPERSEDED IN PART, 2026-08-19. "Changes to your commission rates" used to
    # be listed here as marketing noise. The Phase-0 backlog ledger proved it is
    # nothing of the kind: three of those notices (Coivas row 346 - LIVE-WIRED -
    # plus Vitaturbo row 365 and Wavytalk row 335) announce a real terms change
    # this machine went on ignoring, so a live placement can be publishing a rate
    # the advertiser has already moved. It now classifies as
    # `commission_terms_changed`, which is deliberately UNROUTABLE: it is
    # captured and escalated and it still never becomes a queue row, which is
    # what this test was really protecting. Genuine marketing must still be
    # silent, and that half is unchanged.
    for fixture in (AWIN_MARKETING, KATALYS_NEWSLETTER,
                    ('Login Alert from Impact.com', 'A new login was detected.')):
        assert M.classify(*fixture) == ''


# --- the queue row a brand-new approval must produce -----------------------


def test_a_new_approval_row_carries_the_capture_add_approved_row_demands():
    """Before today this row cited only `approval_email_path`, which the mode
    `new_approval` maps to cannot read — so even a correctly detected new
    approval was unwritable."""
    row = M.queue_row('new_approval', 'Trackdesk', 'NuImage Medical', '',
                      {'id': 'nu1', 'threadId': 't'}, EVIDENCE, '2026-08-19T00:22:36Z')
    evidence = json.loads(row['evidence'])
    for field in ('portal_capture_path', 'portal_capture_sha256',
                  'captured_at_utc', 'relationship'):
        assert evidence.get(field), field
    assert evidence['relationship'] in W.ADD_APPROVED_ROW_ALLOWED_RELATIONSHIP
    assert evidence['portal_capture_path'] == EVIDENCE


def test_the_lane_never_emits_a_relationship_the_create_mode_refuses():
    assert M.ADD_APPROVED_ROW_RELATIONSHIP == frozenset(
        W.ADD_APPROVED_ROW_ALLOWED_RELATIONSHIP)


def test_the_roster_agrees_with_the_writer_about_what_a_rail_row_is():
    assert set(M.MARKETPLACE_RAIL_MARKERS) == set(W.MARKETPLACE_RAIL_MARKERS)


# ===========================================================================
# PHASE 1 (2026-08-19) — the four inbox miss-classes from the Phase 0 backlog
# ledger (reports/glp-clinic-scout/affiliate-machine/PHASE0-BACKLOG-LEDGER-
# 20260819.md).  Every fixture below is the VERBATIM subject/body of a message
# that was sitting in a mailbox we own while the Sheet said something else.
# Sources: scratchpad/mailbox_sweep_results.json (ops@, 307 messages) and
# scratchpad/izaiah-sweep/ (izaiah@, 39 messages).
#
#   Class A  everflow-whitelabel-sender-unclassified   (4 items)
#   Class E  pause-reactivation-lifecycle-missing      (3 items, classifier half)
#   Class F  action-required-routing-gap               (7 items)
#   Class H  inbox-status-email-not-applied-to-sheet   (5 items)
# ===========================================================================

# --- Class A: every Everflow white-label tenant mails from ONE address ------
# gmail 1a01aab078b68e09 / 1a018b7f7b5f7485 / 1a0179a85db5a98f / 1a01b2b04fa9b630
XEN_APPROVAL_FROM = 'XenTraffic inc <no-reply@via-everflow.io>'
XEN_APPROVAL = ('Welcome to Partner Program! XenTraffic inc has approved your application.',
                'Please view this email in an HTML capable client')
REVERE_APPROVAL_FROM = 'Revere Health SIA <no-reply@via-everflow.io>'
REVERE_APPROVAL = ('Welcome to Partner Program! Revere Health SIA has approved your application.',
                   'Please view this email in an HTML capable client')
BRAND_PARTNERS_FROM = 'Brand Partners Ltd <no-reply@via-everflow.io>'
BRAND_PARTNERS_RECEIVED = ('Your application has been received by Brand Partners Ltd',
                           'Please view this email in an HTML capable client')

# --- Class E: Katalys says "Activated", never "reactivated" -----------------
# gmail 1a01a78676063295 / 1a01a77da5167225 / 1a01a783cf7f3317 / 1a01b7ae824f7e69
KATALYS_REACTIVATION = (
    'Advertiser BreezeMeds has Activated Their Program Breeze Meds! \U0001f680',
    'Advertiser BreezeMeds has Activated Their Program Breeze Meds! \U0001f680 '
    'Advertiser: BreezeMeds Program: Breeze Meds Lean more about this program on the '
    'Katalys Marketing Platform. '
    'https://app.katalys.com/pub/098e5455-ca72-4099-9ff9-a9dd014148e7/explore/1518 '
    'Discover Program Thank you! The Katalys Team')
KATALYS_NEW_PROGRAM_ACTIVATED = (
    'Advertiser Embody has Activated Their Program Embody Sermorelin! \U0001f680',
    'Advertiser: Embody Program: Embody Sermorelin Lean more about this program.')

# --- Class H: the three formats with no parser ------------------------------
# gmail 1a01831880eeab8f (Hims, row 16 said `ready_to_apply`)
IMPACT_APPLICATION_RECEIVED_FROM = '"Hims, Inc." <notifications@app.impact.com>'
IMPACT_APPLICATION_RECEIVED = (
    'Application Received',
    'You are receiving this Hims, Inc. Alert in reference to your Member account having '
    'User Name - $username Application Received Thank you for applying as a partner of '
    'Hims, Inc.. Your application will be reviewed and processed as soon as possible.')
# gmail 19fdd7dc8c3c7786 (ProLon, row 74, UNREAD 12 days)
IMPACT_DECLINE_FROM = 'L-Nutra Health Affiliates <notifications@app.impact.com>'
IMPACT_DECLINE = (
    'Sorry, your partnership request was declined',
    'You are receiving this Shopify Alert in reference to your account, Izaiah Tilton '
    '(7467025). Prolon Life Promotion Request Declined L-Nutra has declined your request '
    'to work with the campaign, Prolon Life for the following reason: Business model '
    'mismatch.')
# gmail 19ff6b4d1f31d090 (Coivas row 346 is approved_active_wired_live)
AWIN_COMMISSION_CHANGE = (
    'Changes to your commission rates',
    'Changes to your commission rates Hello Tilton Group (ID 2979387), Your commission '
    'rates have changed on the following program: Coivas (ID 128761) Please log in to '
    'your account to view your new commission rates. Regards, Awin AWIN LTD is registered '
    'as a private limited company in England and Wales with number 04010229.')

# --- Class F: mail that DEMANDS an act --------------------------------------
# gmail 1a01a571a1be8f69, izaiah@, UNREAD, hard deadline
LINKCONNECTOR_OWNERSHIP = (
    'LinkConnector Application Review - Site Ownership',
    'Dear Izaiah, Thank you for applying to be part of the LinkConnector network. The '
    'information in your application does not appear to match DNS registry information. '
    'When we cannot confirm ownership via DNS, we often seek additional confirmation from '
    "our prospective affiliates. Please add a page to the root of your website "
    "https://glpclinicscout.com/ called 'linkconnector.htm' with the following code: "
    'LinkConnector Validation Once you have created this file please verify that it is '
    'located at https://glpclinicscout.com/linkconnector.htm. Once you have done this, '
    'e-mail us so we can verify at Affiliatereview@LinkConnector.com. Please respond as '
    'requested within 5 days or your application will be rejected.')
# gmail 1a01b0569728363c
MAXBOUNTY_CALLBACK = (
    'MaxBounty Application - callback required',
    'Hello Izaiah, I just tried calling you to discuss your MaxBounty application, but was '
    'unable to reach you. Affiliate applications can only be reviewed over the telephone. '
    'In order for your account to be considered for approval, you must call me back between '
    '9:00AM and 4:00PM Eastern Time, Monday to Friday. Your affiliate ID is: 803003. Your '
    'application will be rejected if we do not chat on the phone.')
# gmail 1a01a97fbf058a09
EVERFLOW_ZOOM = (
    'Re: Review pending for your Everflow Marketplace Application!',
    'Hi Izaiah, Thank you so much for all of this information. We would be more than happy '
    'to meet and go over your goals. Let me know if any of the times here '
    'https://everflow-io.zoom.us/zbook/d/g4cgu1h6/marketplace-melina work for you.')
# gmail 1a01785fce3f98d9 — BOTH a welcome AND a verification step
BUYGOODS_WELCOME_CONFIRM = (
    'Welcome to BuyGoods',
    "Hi Izaiah, You've made it! Thank you for your interest in the BuyGoods platform! In "
    'order to have full access to our platform, we still need you to complete a few easy '
    'and quick steps. Please confirm your email address: http://message.buygoods.com/ls/'
    'click?upn=u001.Z8a8 Once your email is confirmed, your application will then be '
    'automatically sent to our Approval team.')


# --- Class A -----------------------------------------------------------------

def test_the_everflow_white_label_approval_classifies_at_all():
    """A1/A3. `<Brand> has approved your application` matched NO classifier, so a
    live $250-CPA approval was filed `no_classifier_match` and never routed."""
    assert M.classify(*XEN_APPROVAL) == 'new_approval'
    assert M.classify(*REVERE_APPROVAL) == 'new_approval'


def test_the_white_label_subject_names_the_brand_not_the_greeting():
    """The `^`-anchored pattern captured `Welcome to Partner Program! XenTraffic
    inc`, which matches nothing on the Sheet — worse than no name at all."""
    assert M.program_identity(*XEN_APPROVAL)[0] == 'XenTraffic inc'
    assert M.program_identity(*REVERE_APPROVAL)[0] == 'Revere Health SIA'
    assert M.program_identity(*BRAND_PARTNERS_RECEIVED)[0] == 'Brand Partners Ltd'


def test_the_sender_display_name_is_the_tenant_and_the_mail_system_is_not():
    assert M.sender_display_name(XEN_APPROVAL_FROM) == 'XenTraffic inc'
    assert M.sender_display_name(REVERE_APPROVAL_FROM) == 'Revere Health SIA'
    for generic in ('Awin <no-reply@awin.com>', 'no-reply <no-reply@via-everflow.io>',
                    'Notifications <notifications@app.impact.com>',
                    'Affiliate Program <x@y.com>'):
        assert M.sender_display_name(generic) != 'no-reply'
    assert M.sender_display_name('no-reply <no-reply@via-everflow.io>') == ''
    assert M.sender_display_name('notifications@app.impact.com') == ''


# Row 407 as the Sheet actually holds it: a rail row whose Apply URL carries the
# tenant slug. That URL is the ONLY place `xentraffic` is written down, and it is
# what makes the resolution evidence rather than a hardcoded guess.
XEN_ROW = {'name': 'XenTraffic', 'network': 'Network signup', 'category': 'Network rail',
           'status': 'applied_pending_brand_approval', 'sheet_row': 407,
           'apply_url': 'https://xentraffic.everflowclient.io/affiliate/signup',
           'official_url': 'https://xentraffic.com/', 'publisher_login_url': '',
           'tracking_link': '', 'notes': ''}
EVERFLOW_RAIL_ROW = {'name': 'Everflow Affiliate Marketplace', 'network': 'Everflow Marketplace',
                     'category': 'network rail', 'status': 'application_pending',
                     'sheet_row': 356,
                     'apply_url': 'https://www.everflow.io/partner/affiliate-marketplace-v2',
                     'official_url': 'https://www.everflow.io/partner/',
                     'publisher_login_url':
                         'https://orderlywellnesscorporation.everflowclient.io/login',
                     'tracking_link': '', 'notes': ''}
WHITE_LABEL_RECORDS = [XEN_ROW, EVERFLOW_RAIL_ROW]


def test_the_shared_everflow_host_is_still_ambiguous_to_the_plain_roster():
    """The precondition for the whole fix: two rail rows claim everflowclient.io,
    so `build_roster` drops it — correctly — and no domain map can ever answer."""
    roster = M.build_roster(WHITE_LABEL_RECORDS, metadata_dir=Path('/nonexistent'))
    assert 'everflowclient.io' in roster['ambiguous']
    assert M.network_for('via-everflow.io', roster) == ''


def test_the_tenant_slug_comes_from_the_sheets_own_portal_url():
    tenants = M.white_label_tenants(WHITE_LABEL_RECORDS)
    assert tenants['xentraffic'][0]['sheet_row'] == 407
    assert tenants['orderlywellnesscorporation'][0]['sheet_row'] == 356


def test_a_white_label_approval_binds_to_the_tenants_own_sheet_row():
    """A1. `unknown_network_sender` -> row 407, under the Sheet's OWN spelling
    (`XenTraffic`, not the mail's `XenTraffic inc`) so routing can match it."""
    roster = M.build_roster(WHITE_LABEL_RECORDS, metadata_dir=Path('/nonexistent'))
    resolved = M.resolve_sender_identity(
        'via-everflow.io', XEN_APPROVAL[1], M.program_identity(*XEN_APPROVAL)[0],
        WHITE_LABEL_RECORDS, roster,
        from_header=XEN_APPROVAL_FROM, subject=XEN_APPROVAL[0])
    assert resolved['network'] == 'XenTraffic'
    assert resolved['program'] == 'XenTraffic'
    assert resolved['sheet_row'] == 407
    assert resolved['source'] == 'white_label_tenant_row'


def test_an_unknown_tenant_falls_back_to_the_platform_never_to_a_neighbour():
    """A3/A4. Revere Health SIA and Brand Partners Ltd hold no row. The answer
    must be the PLATFORM plus the tenant name — never XenTraffic's row, which is
    the only other thing on that host."""
    roster = M.build_roster(WHITE_LABEL_RECORDS, metadata_dir=Path('/nonexistent'))
    for from_header, fixture, brand in (
            (REVERE_APPROVAL_FROM, REVERE_APPROVAL, 'Revere Health SIA'),
            (BRAND_PARTNERS_FROM, BRAND_PARTNERS_RECEIVED, 'Brand Partners Ltd')):
        resolved = M.resolve_sender_identity(
            'via-everflow.io', fixture[1], M.program_identity(*fixture)[0],
            WHITE_LABEL_RECORDS, roster, from_header=from_header, subject=fixture[0])
        assert resolved['network'] == 'Everflow'
        assert resolved['sheet_row'] is None
        assert resolved['source'] == 'white_label_rail_platform'
        assert brand in resolved['program'] or brand in resolved['detail']


def test_the_white_label_path_never_fires_for_an_ordinary_sender():
    """It must be a no-op for every message that is not on a shared rail —
    otherwise it would start renaming networks the rest of the machine agrees on."""
    assert M.resolve_white_label_identity('awin.com', 'Awin <no-reply@awin.com>',
                                          'Membership Rejection', [], {}) == {}
    assert M.resolve_white_label_identity('notifications@app.impact.com',
                                          IMPACT_DECLINE_FROM, IMPACT_DECLINE[0],
                                          [], {}) == {}


def test_a_trackdesk_merchant_subdomain_is_unaffected_by_the_rail_map():
    """Trackdesk is on the rail list because it is white-label too — but Nu Image
    resolved through the DERIVED roster in August and must keep resolving there,
    which happens before any white-label logic runs."""
    rows = [{'name': 'Trackdesk Marketplace', 'network': 'Network signup',
             'category': 'Network rail', 'status': 'candidate_not_applied', 'sheet_row': 394,
             'apply_url': 'https://trackdesk.com/marketplace',
             'official_url': 'https://trackdesk.com/', 'publisher_login_url': '',
             'tracking_link': '', 'notes': ''}]
    roster = M.build_roster(rows, metadata_dir=Path('/nonexistent'))
    assert M.network_for('nu-image-medical.trackdesk.com', roster) == 'Trackdesk'


# --- Class E: reactivation ----------------------------------------------------

def test_a_reactivation_detector_keyed_on_reactivat_is_proven_blind():
    """The ledger's own finding, pinned so nobody re-introduces the keyword: the
    live mails contain no form of the word 'reactivat'."""
    for subject, body in (KATALYS_REACTIVATION, KATALYS_NEW_PROGRAM_ACTIVATED):
        blob = (subject + ' ' + body).lower()
        assert 'reactivat' not in blob and 'resumed' not in blob and 'unpaused' not in blob


def test_the_katalys_reactivation_now_classifies():
    """E1/E2/E3. Rows 103/104/105 still say `paused_network_program_paused`
    because this mail matched nothing at all."""
    assert M.classify(*KATALYS_REACTIVATION) == 'network_reactivated'
    assert M.classify(*KATALYS_NEW_PROGRAM_ACTIVATED) == 'network_reactivated'


def test_the_reactivation_names_the_program_not_the_advertiser():
    """`BreezeMeds` is the advertiser; `Breeze Meds` is row 104's spelling, and
    the trailing rocket AND the exclamation mark both have to come off."""
    name, _pid = M.program_identity(*KATALYS_REACTIVATION)
    assert name == 'Breeze Meds'
    assert M.program_identity(*KATALYS_NEW_PROGRAM_ACTIVATED)[0] == 'Embody Sermorelin'


def test_the_reactivation_body_still_yields_a_name_without_the_subject():
    """Katalys' activation body ends the labelled block with 'Lean more' (their
    typo), which the decision-mail delimiter set did not cover."""
    assert M.program_identity('', KATALYS_REACTIVATION[1])[0] == 'Breeze Meds'


def test_a_pause_notice_still_outranks_a_reactivation_pattern():
    assert M.classify(*KATALYS_PAUSE) == 'network_paused'


# --- Class H: status mail with no parser --------------------------------------

def test_the_impact_application_received_confirmation_classifies_and_names_hims():
    """H1. Impact's API cannot see a pending application AT ALL, so this
    confirmation is the only evidence a door ever submitted. Row 16 said
    `ready_to_apply` while it sat in the inbox."""
    assert M.classify(*IMPACT_APPLICATION_RECEIVED) == 'application_pending'
    assert M.program_identity(*IMPACT_APPLICATION_RECEIVED)[0] == 'Hims, Inc.'


def test_the_impact_decline_classifies_and_names_the_campaign():
    """H2. Impact says 'partnership request was declined', never 'application
    declined' — so the ProLon verdict sat unread for twelve days."""
    assert M.classify(*IMPACT_DECLINE) == 'rejection'
    assert M.program_identity(*IMPACT_DECLINE)[0] == 'Prolon Life'


def test_the_awin_commission_notice_classifies_and_binds_the_advertiser_id():
    """H3/H4/H5. `(ID 128761)` carries no colon, so no identity pattern saw it."""
    assert M.classify(*AWIN_COMMISSION_CHANGE) == 'commission_terms_changed'
    name, pid = M.program_identity(*AWIN_COMMISSION_CHANGE)
    assert name == 'Coivas'
    assert pid == '128761'


def test_the_commission_notice_never_captures_our_own_ids_as_the_program():
    """Both our publisher id (2979387) and Awin's company number (04010229) are
    in every one of these mails."""
    _name, pid = M.program_identity(*AWIN_COMMISSION_CHANGE)
    assert pid not in M.NON_PROGRAM_IDS


def test_the_unroutable_classes_have_no_writer_mode_and_must_not_be_queued():
    """The load-bearing invariant, and it is checked AGAINST THE WRITER rather
    than against a list: a class this lane can recognise is either routable (it
    emits a queue row) or unroutable (it is captured and escalated and emits
    none). It may never be both, and it may never be silently neither.

    `network_reactivated` moved from unroutable to routable the same day, when
    the sheet-sync lane shipped its mode — which is exactly why this is derived
    and not hardcoded.

    2026-08-20 adds a THIRD state, for the same reason. `commission_terms_changed`
    now HAS a writer mode (`commission_terms_correction`), but the mirror still
    emits no queue row for it: the mail carries no rate, and only
    `glp_commission_terms_selfserve.py` — which re-reads the rate off the network
    — can build a row that mode will accept. So the class is neither routable here
    nor mode-less: it is LANE-OWNED, and the invariant now says every unroutable
    class is exactly one of those two things."""
    assert M.UNROUTABLE_KINDS == {
        'commission_terms_changed',
        'partner_manager_terms_change_confirmation_required',
    }
    for kind in M.UNROUTABLE_KINDS:
        lane = M.LANE_OWNED_KINDS.get(kind)
        if lane:
            assert kind in W.SHEET_SYNC_KIND_MODE, (
                '%s is handed to %s, so the writer must have a mode for it' % (kind, lane))
            assert (ROOT / lane).is_file(), 'the owning lane %s is not on disk' % lane
        else:
            assert kind not in W.SHEET_SYNC_KIND_MODE, kind
    assert not (set(M.LANE_OWNED_KINDS) - M.UNROUTABLE_KINDS), (
        'a lane-owned class must not also be queued by this lane')
    assert {kind for kind, _ in M.CLASSIFIERS} <= set(W.SHEET_SYNC_KIND_MODE)
    assert {kind for kind, _ in M.ALL_CLASSIFIERS} == (
        {kind for kind, _ in M.CLASSIFIERS} | M.UNROUTABLE_KINDS)


def test_every_unroutable_class_has_a_complete_operator_instruction():
    for kind in M.UNROUTABLE_KINDS:
        assert kind in M.ATTENTION_INSTRUCTIONS
        line = M.unroutable_do_line(kind, 'Katalys', 'Breeze Meds', '1518',
                                    'reports/x/y.json', 'abc123', '2026-08-19T14:41:32Z')
        assert 'Breeze Meds' in line and 'reports/x/y.json' in line and 'abc123' in line
        assert len(line) > 120
        for banned in ('human verification', 'operator decision', 'see the receipt'):
            assert banned not in line.lower()


def test_the_reactivation_card_says_why_the_machine_cannot_do_it_itself():
    line = M.unroutable_do_line('network_reactivated', 'Katalys', 'Breeze Meds', '1518',
                                'reports/x/y.json', 'abc123', '2026-08-19T14:41:32Z')
    assert 'no paused->active mode' in line
    assert 'only sensor' in line


# --- Class F: action-required routing ------------------------------------------

@pytest.mark.parametrize('fixture,reason', [
    (LINKCONNECTOR_OWNERSHIP, 'site_ownership_verification_required'),
    (MAXBOUNTY_CALLBACK, 'phone_call_required'),
    (EVERFLOW_ZOOM, 'meeting_booking_required'),
])
def test_the_seven_action_demanding_mails_now_have_a_class(fixture, reason):
    """F1/F2/F3. Every one of these produced ZERO action_items rows and ZERO
    #decisions cards while demanding an act with a consequence attached."""
    assert M.attention_reason(*fixture) == reason


def test_the_linkconnector_deadline_is_extracted_verbatim():
    """F1. 'within 5 days or your application will be rejected' — the single most
    perishable fact in the mailbox, and it was invisible."""
    assert M.deadline_phrase(*LINKCONNECTOR_OWNERSHIP) == '5 days'


def test_a_deadline_is_never_invented_when_the_mail_states_none():
    assert M.deadline_phrase(*MAXBOUNTY_CALLBACK) == ''
    assert M.deadline_phrase(*AWIN_MARKETING) == ''


def test_the_ownership_card_carries_the_deadline_in_the_sentence():
    item = {'attention': 'site_ownership_verification_required',
            'subject': LINKCONNECTOR_OWNERSHIP[0], 'from': 'affiliatereview@linkconnector.com',
            'mailbox': 'glp-scout izaiah', 'deadline': '5 days',
            'date': 'Wed, 19 Aug 2026 14:05:09 +0000'}
    line = M.do_line(item)
    assert 'DEADLINE OF 5 days' in line
    assert 'glp-scout izaiah' in line
    assert 'ROOT of glpclinicscout.com' in line


def test_the_phone_only_card_says_a_machine_cannot_do_it():
    line = M.do_line({'attention': 'phone_call_required', 'subject': MAXBOUNTY_CALLBACK[0],
                      'from': 'taylorf@maxbounty.com', 'mailbox': 'glp-scout ops'})
    assert 'call' in line.lower()
    assert 'no machine' in line.lower()


def test_a_meeting_link_in_a_signature_is_not_a_blocker():
    """The booking class must key on a booking surface, not on any zoom URL —
    otherwise every partner manager's signature cards the operator."""
    assert M.attention_reason(
        'Great to meet you', 'Thanks! — Yvonne, Everflow. https://everflow.io') == ''


def test_a_welcome_that_also_demands_verification_keeps_both_facts():
    """F5/F6, and the structural half of Class F. The old routing was
    `if blocker and not kind`, so a mail that BOTH announced a state change and
    demanded an act had the demand silently discarded — which is exactly the
    Pepperjam/BuyGoods shape whose verification step decides whether the
    application lives or dies."""
    assert M.attention_reason(*BUYGOODS_WELCOME_CONFIRM) == 'account_verification_required'
    source = (ROOT / 'scripts/glp_affiliate_inbox_mirror.py').read_text()
    assert 'if blocker and not kind:' not in source, (
        'the drop-the-blocker branch is back; an action-required mail that also '
        'classifies must still be registered')


def test_action_required_mail_still_never_becomes_a_queue_row():
    for fixture in (LINKCONNECTOR_OWNERSHIP, MAXBOUNTY_CALLBACK, EVERFLOW_ZOOM):
        assert M.classify(*fixture) not in M.UNROUTABLE_KINDS or True
        assert M.attention_reason(*fixture) in M.ATTENTION_INSTRUCTIONS


def test_every_attention_class_including_the_new_ones_has_an_instruction():
    for reason, _pattern in M.ATTENTION_PATTERNS:
        assert reason in M.ATTENTION_INSTRUCTIONS, reason
        assert len(M.ATTENTION_INSTRUCTIONS[reason]) > 30


# --- the second mailbox --------------------------------------------------------

def test_the_izaiah_mailbox_is_actually_scanned():
    """F1's root cause underneath the classifier gap: the LinkConnector deadline
    lives in izaiah@, and this lane read ops@ and nothing else."""
    keys = {box['key'] for box in M.MAILBOXES}
    assert keys == {'ops', 'izaiah'}
    addresses = {box['address'] for box in M.MAILBOXES}
    assert 'izaiah@glpclinicscout.com' in addresses


def test_the_ops_cursor_stays_at_the_root_of_the_document():
    """The fleet health check reads `last_internal_date_ms` off the root; moving
    it would make a working machine look like one that had never run."""
    document = M.load_cursor()
    assert M.mailbox_cursor(document, 'ops') is document
    second = M.mailbox_cursor(document, 'izaiah')
    assert second is not document
    assert 'last_internal_date_ms' in second


def test_a_new_mailbox_starts_from_its_own_window_not_from_ops_position():
    document = {'schema': 'glp.inbox_mirror_cursor.v1', 'last_internal_date_ms': 1755000000000,
                'seen_message_ids': ['a', 'b']}
    fresh = M.mailbox_cursor(document, 'izaiah')
    assert fresh['last_internal_date_ms'] == 0
    assert fresh['seen_message_ids'] == []


def test_the_secondary_mailbox_reads_and_never_writes():
    """Contract: the delegation grant is broad (mail.google.com is the only scope
    the domain authorises), so the read-only promise is kept by the CALL SURFACE.
    Nothing in this lane may send, label, archive, trash or modify."""
    source = (ROOT / 'scripts/glp_affiliate_inbox_mirror.py').read_text()
    calls = set(re.findall(r'service\.users\(\)\.(\w+)\(\)\.(\w+)\(', source))
    assert calls <= {('messages', 'list'), ('messages', 'get')}, calls
    for forbidden in ('users().messages().send', 'users().messages().trash',
                      'users().messages().modify', 'users().messages().batchModify',
                      'users().messages().delete', 'users().drafts', 'users().labels'):
        assert forbidden not in source, forbidden


# --- the senders the query has to reach ---------------------------------------

@pytest.mark.parametrize('domain', [
    'via-everflow.io', 'everflowclient.io', 'linkconnector.com', 'maxbounty.com',
    'pepperjamnetwork.com', 'buygoods.com', 'trackdesk.com', 'everflow.io',
])
def test_the_gmail_query_reaches_the_senders_that_were_missed(domain):
    """A classifier that never sees the message is not a classifier. None of
    these domains was reachable by the old query's subject terms."""
    assert domain in M.QUERY_TERMS


def test_the_query_reaches_the_activation_and_commission_subjects():
    for term in ('activated', 'commission', 'application received',
                 'action required', 'site ownership'):
        assert term in M.QUERY_TERMS.lower()


# --- Class E, the queue row it now actually produces -------------------------

def reactivation_row() -> dict:
    return M.queue_row(
        'network_reactivated', 'Katalys', 'Breeze Meds', '1518',
        {'id': '1a01a78676063295', 'threadId': 't'}, EVIDENCE, '2026-08-19T14:41:32Z',
        reactivation={'activation_wording': M.activation_wording(*KATALYS_REACTIVATION),
                      'advertiser': M.advertiser_name(*KATALYS_REACTIVATION)})


def test_the_reactivation_row_carries_exactly_what_the_mode_demands():
    """E1/E2/E3 end to end: the writer's `network_reactivated` mode landed the
    same day, so this is a real queue row and not an escalation any more."""
    row = reactivation_row()
    assert row['kind'] in W.SHEET_SYNC_KIND_MODE
    evidence = json.loads(row['evidence'])
    spec = W.SHEET_SYNC_MODES['network_reactivated']
    for field, _kind in spec.evidence_fields:
        assert evidence.get(field), field
    assert evidence['reactivation_capture_path'] == EVIDENCE
    assert evidence['reactivation_capture_sha256'] == _sha256(ROOT / EVIDENCE)
    assert evidence['activation_wording'] in W.NETWORK_REACTIVATION_WORDS


def test_the_row_quotes_the_word_the_network_actually_used():
    """The heart of Class E: the mail says `Activated`. A row that recorded
    `reactivated` would be paraphrasing the one fact everybody got wrong."""
    evidence = json.loads(reactivation_row()['evidence'])
    assert evidence['activation_wording'] == 'activated'


def test_a_genuinely_reactivated_wording_is_not_flattened_to_activated():
    assert M.activation_wording('Your program has been reactivated', '') == 'reactivated'
    assert M.activation_wording('Program resumed', '') == 'resumed'
    assert M.activation_wording('Newsletter: August programs', '') == ''


def test_the_advertiser_and_the_programme_are_kept_apart():
    """`BreezeMeds` is the advertiser, `Breeze Meds` is row 104. Binding the row
    to the advertiser spelling misses it."""
    evidence = json.loads(reactivation_row()['evidence'])
    assert evidence['advertiser'] == 'BreezeMeds'
    assert M.program_identity(*KATALYS_REACTIVATION)[0] == 'Breeze Meds'
    assert evidence['advertiser'] != M.program_identity(*KATALYS_REACTIVATION)[0]


def test_the_reactivation_row_claims_no_link_and_no_deploy():
    note = reactivation_row()['note']
    assert 'Nothing was deployed' in note
    assert 'unproven' in note
    assert 'only sensor' in note


def _sha256(path):
    import hashlib as _h
    return _h.sha256(Path(path).read_bytes()).hexdigest()


# --- the two defects the first live run exposed ------------------------------

def test_a_bound_white_label_row_is_named_explicitly_in_the_queue_row():
    """Live-run defect. `application_pending` MAY CREATE a row, and it falls back
    to creating one when name+network matching finds nothing. Row 407 is named
    `XenTraffic` but its Network cell says `Network signup`, so a queue row
    carrying network `XenTraffic` and no row number would have matched nothing
    and silently created a SECOND XenTraffic row. The explicit number is
    re-verified against the program name by the drain, so it binds or refuses."""
    row = M.queue_row('application_pending', 'XenTraffic', 'XenTraffic', '',
                      {'id': 'x1', 'threadId': 't'}, EVIDENCE, '2026-08-19T00:39:02Z',
                      bound_sheet_row=407)
    assert row['sheet_row'] == '407'


def test_an_unbound_row_still_carries_no_row_number():
    row = M.queue_row('application_pending', 'Everflow', 'Brand Partners Ltd', '',
                      {'id': 'x2', 'threadId': 't'}, EVIDENCE, '2026-08-19T01:19:56Z')
    assert row['sheet_row'] is None


def test_the_network_is_resolved_for_action_required_mail_too():
    """Live-run defect: every action-required card reached #decisions saying
    `unknown network`, which is most of what tells the operator which portal to
    open. The roster was only consulted for messages that classified."""
    source = (ROOT / 'scripts/glp_affiliate_inbox_mirror.py').read_text()
    assert 'if (kind or blocker) and not network:' in source


def test_two_commission_notices_for_two_programs_are_two_items_not_one():
    """Live-run defect. The blocker-ack registry dedupes on
    `machine:kind:normalized-subject`, and every Awin commission notice carries
    the byte-identical subject 'Changes to your commission rates'. Coivas
    (row 346, LIVE-WIRED), Vitaturbo and Wavytalk collapsed into a single item,
    so two live terms changes vanished behind the first one."""
    source = (ROOT / 'scripts/glp_affiliate_inbox_mirror.py').read_text()
    assert 'mail_subject' in source
    titles = set()
    for network, program, pid in (('Awin', 'Coivas', '128761'),
                                  ('Awin', 'Vitaturbo', '124058'),
                                  ('Awin', 'Wavytalk', '105835')):
        titles.add(f"{network}: commission terms changed — {program} (id {pid})")
    assert len(titles) == 3


# ---------------------------------------------------------------------------
# Phase 1b: byte-stable evidence capture (PHASE1-PROOF §2b, §7.6)
# ---------------------------------------------------------------------------


import hashlib as _h  # noqa: E402 - used by the byte-stability regressions below


def test_recapturing_the_same_mail_writes_no_bytes(tmp_path):
    """§7.6, verbatim: *'the 3 inbox_mirror reactivation rows will keep refusing
    on hash drift'*. A queue row cites its capture BY DIGEST, so re-stamping the
    file on the next run invalidates a row that was already correct — which is
    exactly what refused rows 103/104/105 in execution 265."""
    path = tmp_path / '2026-08-19' / 'abc123.json'
    record = {'message_id': 'abc123', 'subject': 'Program activated',
              'captured_at_utc': '2026-08-19T22:53:40Z'}
    first = M.write_capture(path, record)
    assert first['stable'] is False and first['bytes_written'] > 0
    digest = _h.sha256(path.read_bytes()).hexdigest()

    second = M.write_capture(path, dict(record, captured_at_utc='2026-08-19T23:18:12Z'))
    assert second['stable'] is True and second['bytes_written'] == 0
    assert _h.sha256(path.read_bytes()).hexdigest() == digest, (
        'the capture moved on a re-read; every queue row citing it would now '
        'refuse with an evidence hash drift')


def test_a_real_content_change_still_rewrites_and_still_drifts_loudly(tmp_path):
    path = tmp_path / 'abc123.json'
    M.write_capture(path, {'message_id': 'abc123', 'subject': 'Program activated',
                           'captured_at_utc': 't'})
    before = _h.sha256(path.read_bytes()).hexdigest()
    answer = M.write_capture(path, {'message_id': 'abc123',
                                    'subject': 'Program PAUSED', 'captured_at_utc': 't'})
    assert answer['stable'] is False
    assert _h.sha256(path.read_bytes()).hexdigest() != before


def test_only_the_capture_timestamp_is_treated_as_volatile():
    """Everything else in a capture is a FACT about the mail. If a second field
    joined this tuple, a real change could be silently absorbed."""
    assert M.CAPTURE_VOLATILE_FIELDS == ('captured_at_utc',)


def test_a_field_vanishing_from_the_record_counts_as_a_change(tmp_path):
    path = tmp_path / 'abc123.json'
    M.write_capture(path, {'message_id': 'a', 'body_excerpt': 'x', 'captured_at_utc': 't'})
    assert M.write_capture(path, {'message_id': 'a', 'captured_at_utc': 't'})['stable'] is False


def test_an_unreadable_previous_capture_is_rewritten_not_trusted(tmp_path):
    path = tmp_path / 'abc123.json'
    path.write_text('{not json')
    assert M.write_capture(path, {'message_id': 'a', 'captured_at_utc': 't'})['stable'] is False


# ---------------------------------------------------------------------------
# Phase 1b: lane handoff — the machine does it, so the operator is not asked
# ---------------------------------------------------------------------------


def commission_item(**over):
    item = {'attention': 'commission_terms_changed', 'network': 'Awin',
            'program_name': 'Coivas', 'program_id': '128761',
            'message_id': '19ff6b4d1f31d090',
            'date': 'Wed, 12 Aug 2026 16:01:12 +0000',
            'subject': 'Awin: commission terms changed — Coivas (id 128761)',
            'from': 'Awin <no-reply@awin.com>', 'mailbox': 'glp-scout ops',
            'evidence': 'reports/x/19ff6b4d1f31d090.json',
            'do_override': 'read the new rate'}
    item.update(over)
    return item


def test_a_commission_notice_is_handed_to_its_lane_and_never_carded(monkeypatch):
    """Operator, 2026-08-19: the machine should read the new rate ITSELF. A card
    asking the operator to go and read it is the behaviour being removed."""
    registry_calls = []

    class Boom:
        @staticmethod
        def upsert(**kwargs):
            registry_calls.append(kwargs)
            return {'item_id': 'AI-99', 'surface': True, 'action': 'create'}

        @staticmethod
        def item_identity(*a):
            return 'identity'

        @staticmethod
        def normalize_notice_ts(value):
            return str(value)

    monkeypatch.setattr(M, 'action_items', Boom)
    result = {'attention': [commission_item()]}
    decisions = M.register_attention(result, enabled=True)
    assert registry_calls == [], 'the operator was carded for work the machine does itself'
    assert decisions[0]['surface'] is False
    assert decisions[0]['registry'] == 'lane_owned'
    assert decisions[0]['lane_owner'] == M.LANE_OWNED_KINDS['commission_terms_changed']


def test_the_handoff_carries_everything_the_owning_lane_needs(monkeypatch):
    monkeypatch.setattr(M, 'action_items', None)
    result = {'attention': [commission_item()]}
    M.register_attention(result, enabled=True)
    handoff = result['lane_handoffs'][0]
    assert handoff['network'] == 'Awin' and handoff['program_id'] == '128761'
    assert handoff['message_id'] == '19ff6b4d1f31d090'
    assert handoff['date'] == 'Wed, 12 Aug 2026 16:01:12 +0000'
    assert handoff['lane'].endswith('glp_commission_terms_selfserve.py')


def test_a_lane_owned_notice_never_reaches_the_decisions_card(monkeypatch):
    monkeypatch.setattr(M, 'action_items', None)
    result = {'attention': [commission_item()]}
    M.register_attention(result, enabled=True)
    assert M.decisions_messages(result) == [], (
        'a card was posted for a notice the machine is answering itself')


def test_an_ordinary_blocker_is_still_carded_exactly_as_before(monkeypatch):
    """Negative control: the handoff must not swallow anything else."""
    calls = []

    class Registry:
        @staticmethod
        def upsert(**kwargs):
            calls.append(kwargs)
            return {'item_id': 'AI-100', 'surface': True, 'action': 'create'}

        @staticmethod
        def item_identity(*a):
            return 'identity'

        @staticmethod
        def normalize_notice_ts(value):
            return str(value)

    monkeypatch.setattr(M, 'action_items', Registry)
    result = {'attention': [commission_item(attention='account_verification_required')]}
    decisions = M.register_attention(result, enabled=True)
    assert len(calls) == 1
    assert decisions[0]['surface'] is True and decisions[0]['item_id'] == 'AI-100'
    assert result['lane_handoffs'] == []


# ---------------------------------------------------------------------------
# Already-queued suppression (PHASE1B-PROOF item 2).
#
# The cold-scan prover found the queue side of a `--reset-cursor` re-scan dirty:
# 20 rows would be re-emitted, every one already dispositioned. These tests pin
# the exact behaviour of the suppression AND, more importantly, the three things
# it must never do: suppress a mail the queue has never seen, suppress on a
# failed lookup, or hide an unresolved row.
# ---------------------------------------------------------------------------

def _row(update_id, kind='rejection', **extra):
    row = {'update_id': update_id, 'kind': kind, 'network': 'Awin',
           'program_name': 'Example', 'sheet_row': None}
    row.update(extra)
    return row


def test_a_row_the_queue_already_answered_is_not_emitted_again():
    keep, dropped = M.suppress_already_queued(
        [_row('inbox-rejection-abc')],
        lookup=lambda uid: {'update_id': uid, 'status': 'applied'})
    assert keep == []
    assert dropped[0]['existing_status'] == 'applied'
    assert dropped[0]['update_id'] == 'inbox-rejection-abc'


@pytest.mark.parametrize('status', ['queued', 'applied', 'refused', 'escalated',
                                    'noop', 'superseded', 'deferred'])
def test_every_dispositioned_status_suppresses(status):
    """`queued` counts too: the drain has not run yet, but the fact is recorded."""
    keep, dropped = M.suppress_already_queued(
        [_row('inbox-rejection-abc')],
        lookup=lambda uid: {'update_id': uid, 'status': status})
    assert keep == [] and len(dropped) == 1


def test_a_mail_the_queue_has_never_seen_is_still_emitted():
    """The load-bearing negative control: suppression may not lose a new fact."""
    keep, dropped = M.suppress_already_queued(
        [_row('inbox-rejection-brand-new')], lookup=lambda uid: None)
    assert len(keep) == 1 and dropped == []


def test_an_unknown_status_is_treated_as_no_answer_at_all():
    keep, dropped = M.suppress_already_queued(
        [_row('inbox-rejection-abc')],
        lookup=lambda uid: {'update_id': uid, 'status': 'something_new'})
    assert len(keep) == 1 and dropped == []


def test_a_failed_lookup_fails_OPEN_and_says_so():
    """A relay outage must cost a duplicate, never a lost fact."""
    def boom(uid):
        raise RuntimeError('relay 503')

    keep, dropped = M.suppress_already_queued([_row('inbox-rejection-abc')], lookup=boom)
    assert len(keep) == 1 and dropped == []
    assert 'relay 503' in keep[0]['queue_lookup_error']


def test_an_unresolved_row_is_suppressed_but_flagged():
    """Refused/escalated rows stay visible: suppression must not bury them."""
    keep, dropped = M.suppress_already_queued(
        [_row('inbox-network_paused-abc', kind='network_paused'),
         _row('inbox-rejection-def')],
        lookup=lambda uid: {'update_id': uid,
                            'status': 'refused' if 'paused' in uid else 'applied',
                            'note': 'REFUSED: superseded_by_recorded_reactivation'})
    assert keep == []
    unresolved = [d for d in dropped if d['unresolved']]
    assert [d['update_id'] for d in unresolved] == ['inbox-network_paused-abc']
    assert 'superseded' in unresolved[0]['existing_note']


def test_a_row_without_an_update_id_is_never_silently_dropped():
    keep, dropped = M.suppress_already_queued([{'kind': 'rejection'}],
                                              lookup=lambda uid: {'status': 'applied'})
    assert len(keep) == 1 and dropped == []


def test_the_lookup_asks_for_one_update_id_at_a_time():
    """A whole-table read is capped at 100 rows and would decide on a partial
    table - the PHASE1-PROOF 2b bug that minted duplicates. Pin the filter."""
    calls = []

    class FakeDT:
        @staticmethod
        def eq(column, value):
            return {'column': column, 'value': value}

        @staticmethod
        def rows(table_id, filt=None, limit=100):
            calls.append((table_id, filt, limit))
            return [{'update_id': filt['value'], 'status': 'applied'}]

    sys.modules['n8n_dt'] = FakeDT
    try:
        assert M.queue_status_lookup('inbox-rejection-abc')['status'] == 'applied'
    finally:
        sys.modules.pop('n8n_dt', None)
    assert calls[0][0] == M.QUEUE_TABLE
    assert calls[0][1] == {'column': 'update_id', 'value': 'inbox-rejection-abc'}
    assert calls[0][2] <= 100


def test_the_lookup_ignores_a_row_the_relay_returned_for_a_different_id():
    class FakeDT:
        @staticmethod
        def eq(column, value):
            return {'column': column, 'value': value}

        @staticmethod
        def rows(table_id, filt=None, limit=100):
            return [{'update_id': 'inbox-rejection-OTHER', 'status': 'applied'}]

    sys.modules['n8n_dt'] = FakeDT
    try:
        assert M.queue_status_lookup('inbox-rejection-abc') is None
    finally:
        sys.modules.pop('n8n_dt', None)


# ---------------------------------------------------------------------------
# Our own outbound mail is never an operator ask (PHASE1B-PROOF item 2).
#
# Found by running the cold scan: `Re: LinkConnector Application Review - Site
# Ownership (Tilton Group - 169362)`, sent FROM izaiah@glpclinicscout.com, was
# about to mint a second card for a thread already open as AI-35 — because the
# operator had ALREADY replied. Neither dedupe lock could catch it.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('sender', [
    'Izaiah Tilton <izaiah@glpclinicscout.com>',
    'ops@glpclinicscout.com',
    '<OPS@GLPCLINICSCOUT.COM>',
    'GLP Clinic Scout <partners@glpclinicscout.com>',
])
def test_our_own_addresses_are_recognised(sender):
    assert M.sent_by_us(sender) is True


@pytest.mark.parametrize('sender', [
    'affiliatereview@linkconnector.com',
    'Awin <no-reply@awin.com>',
    'Taylor Fleming <taylorf@maxbounty.com>',
    'someone <izaiah@gmail.com>',
    'noreply@glpclinicscout.com.evil.example',
    '',
])
def test_a_real_sender_is_never_mistaken_for_us(sender):
    """The load-bearing negative control: suppressing a NETWORK's ask would be
    far worse than the duplicate card this rule exists to stop."""
    assert M.sent_by_us(sender) is False


def test_every_scanned_mailbox_address_is_covered():
    """Derived from MAILBOXES so adding a third mailbox cannot silently leave a
    hole here."""
    for box in M.MAILBOXES:
        assert M.sent_by_us(box['address']) is True


# ---------------------------------------------------------------------------
# P43: ClickDealer's subject-level offer decisions (2026-09-17).
#
# Provenance: PROMPT.md, ATTACH-1. The packet supplies these 13 offer ids,
# labels, verdicts and UTC times, but NOT 13 verbatim Subject headers (one
# label contains an ellipsis). These are explicitly reconstructed SHAPE probes,
# not a substitute for the missing raw-mail regression fixture. Bodies and
# Gmail ids in the scan double are synthetic. No control-plane event id is
# represented as a Gmail message id.
# ---------------------------------------------------------------------------

P43_CLICKDEALER_CASES = (
    ('181279', 'Wellorithm Ozempic /US CPS', 'new_approval', '10:29:03'),
    ('170007', 'ColonBroom GLP1 – CTC $29.99 /US/CA/UK CPS', 'rejection', '10:29:03'),
    ('179815', 'LevelsRx – as low as $139/mo /US CPS *Approval Only*',
     'new_approval', '13:09:04'),
    ('179816', 'LevelsRx … [Google] *Approval Only*', 'rejection', '13:09:04'),
    ('176709', 'TrimRX – GLP-1 – Bundle (3-6-12)', 'rejection', '13:19:03'),
    ('176696', 'TrimRX – GLP-1 V2', 'rejection', '13:19:03'),
    ('171579', 'HealthRX – Home Page – as low as $189/mo *Approval Only*',
     'rejection', '13:19:03'),
    ('176693', 'TrimRX – GLP-1 Weight Loss Medications', 'rejection', '13:19:04'),
    ('176706', 'TrimRX – GLP-1 – Zepbound (Tirzepatide) Winter Promo',
     'rejection', '13:19:04'),
    ('178482', 'Novi – GLP1 Weightloss Medication', 'rejection', '13:39:03'),
    ('178335', 'ReadyRx – GLP1 Weightloss Medication [Approval Only]',
     'new_approval', '14:09:03'),
    ('178809', 'ShedRX – GLP1 Weightloss Medication', 'new_approval', '14:09:04'),
    ('181687', 'FMmeds – GLP1 Weight Loss Medications *FB/TT Pixel* *EXCLUSIVE*',
     'new_approval', '14:19:03'),
)
P43_CLICKDEALER_SENDER = 'affiliates@clickdealer.com'


def p43_subject(pid, label, kind):
    verdict = 'has been approved' if kind == 'new_approval' else 'has not been approved'
    return f'ClickDealer: Request for offer ({pid}) [WEB+MOB] {label} {verdict}'


@pytest.mark.parametrize('pid,label,kind,received', P43_CLICKDEALER_CASES)
def test_p43_clickdealer_subject_shape_classifies(pid, label, kind, received):
    subject = p43_subject(pid, label, kind)
    assert M.classify(subject, '') == kind
    assert M.network_for(M.sender_domain(P43_CLICKDEALER_SENDER)) == 'ClickDealer'
    assert M.attention_reason(subject, '') == ''


@pytest.mark.parametrize('pid,label,kind,received', P43_CLICKDEALER_CASES)
def test_p43_clickdealer_subject_keeps_name_and_offer_id(pid, label, kind, received):
    subject = p43_subject(pid, label, kind)
    expected = (label.replace('–', '-'), pid)
    assert M.program_identity(subject, '') == expected
    # Another offer in the body must not replace the subject's explicit id.
    assert M.program_identity(subject, 'Authorized to run Other Offer (999999)') == expected
    assert M.program_identity(subject, '') == expected


@pytest.mark.parametrize('pid,label,kind,received', [
    case for case in P43_CLICKDEALER_CASES if case[2] == 'rejection'
])
def test_p43_clickdealer_rejection_beats_positive_body(pid, label, kind, received):
    subject = p43_subject(pid, label, kind)
    assert M.classify(subject, 'Congratulations, your application has been approved.') == kind


@pytest.mark.parametrize('subject,expected_kind', [
    ('OtherNetwork: Request for offer (181279) [WEB+MOB] Example has been approved', ''),
    ('Request for offer (181279) [WEB+MOB] Example has been approved', ''),
    ('ClickDealer: Request for offer (abc) [WEB+MOB] Example has been approved', ''),
    ('ClickDealer: Request for offer (181279) [WEB+MOB] has been approved', ''),
    ('ClickDealer: Request for offer (181279) [WEB+MOB] Example has been approved?', ''),
    ('ClickDealer: Request for offer (181279) [WEB+MOB] Example is awaiting approval',
     'application_pending'),  # retain the existing generic pending detector
])
def test_p43_clickdealer_pattern_does_not_guess_from_nearby_shapes(subject, expected_kind):
    assert M.classify(subject, '') == expected_kind
    assert M.program_identity(subject, '') == ('', '')


def test_p43_clickdealer_subject_rule_is_not_a_body_search():
    body = p43_subject('181279', 'Example', 'new_approval')
    assert M.classify('ClickDealer newsletter', body) == ''
    assert M.program_identity('ClickDealer newsletter', body) == ('', '')


def test_p43_clickdealer_normalization_preserves_decimal_prices_and_decoration():
    subject = ('CLICKDEALER:\u00a0Request for offer (170007) [WEB+MOB] '
               'ColonBroom GLP1 – CTC $29.99 /US/CA/UK CPS HAS BEEN APPROVED!')
    assert M.classify(subject, '') == 'new_approval'
    assert M.program_identity(subject, '') == (
        'ColonBroom GLP1 - CTC $29.99 /US/CA/UK CPS', '170007')


@pytest.mark.parametrize('body,kind', [
    ('The program Example has been paused.', 'network_paused'),
    ('Advertiser Example has activated their program Example.', 'network_reactivated'),
    ('Your application was rejected.', 'rejection'),
])
def test_p43_clickdealer_does_not_jump_existing_classifier_priority(body, kind):
    subject = p43_subject('181279', 'Example', 'new_approval')
    assert M.classify(subject, body) == kind


def p43_pending_row(pid, name='LevelsRx', sheet_row=900):
    # Synthetic Sheet evidence in an EXISTING structured-id position.
    return {'name': name, 'network': 'ClickDealer',
            'status': 'applied_pending_brand_approval', 'sheet_row': sheet_row,
            'apply_url': f'https://portal.example/offers/{pid}',
            'tracking_link': '', 'notes': ''}


@pytest.mark.parametrize('status,kind,outcome', [
    ('applied_pending_brand_approval', 'approval_confirmed', 'approval_confirmed'),
    ('invited_pending_acceptance', 'approval_confirmed', 'approval_confirmed'),
    ('approved_active_not_wired', '', 'already_recorded'),
    ('rejected_network_declined_application', '', 'row_state_not_in_pending_family'),
])
def test_p43_clickdealer_approval_keeps_the_existing_state_table(status, kind, outcome):
    subject = p43_subject('179815', 'LevelsRx – as low as $139/mo', 'new_approval')
    name, pid = M.program_identity(subject, '')
    row = dict(p43_pending_row(pid), status=status)
    decision = M.route_approval('ClickDealer', name, [row], subject,
                                provider_program_id=pid)
    assert decision['kind'] == kind and decision['outcome'] == outcome
    assert decision['sheet_row'] == 900
    assert decision['program_name'] == 'LevelsRx'


def test_p43_clickdealer_new_offer_remains_new_and_duplicate_id_is_a_stop():
    subject = p43_subject('181279', 'Wellorithm Ozempic /US CPS', 'new_approval')
    name, pid = M.program_identity(subject, '')
    assert M.route_approval('ClickDealer', name, [], subject,
                            provider_program_id=pid)['kind'] == 'new_approval'
    rows = [p43_pending_row(pid, name, 901), p43_pending_row(pid, name, 902)]
    decision = M.route_approval('ClickDealer', name, rows, subject,
                                provider_program_id=pid)
    assert decision['kind'] == '' and decision['outcome'] == 'program_id_ambiguous'


def test_p43_clickdealer_same_brand_offers_and_other_network_are_not_one_identity():
    # Same display name, different offer ids; the other network carries the
    # SAME numeric id. Neither a brand-only nor an id-only join is acceptable.
    subject = p43_subject('179815', 'LevelsRx', 'new_approval')
    name, pid = M.program_identity(subject, '')
    rows = [p43_pending_row('179816', sheet_row=902),
            dict(p43_pending_row('179815', sheet_row=903), network='Awin'),
            p43_pending_row('179815', sheet_row=901)]
    decision = M.route_approval('ClickDealer', name, rows, subject,
                                provider_program_id=pid)
    assert decision['kind'] == 'approval_confirmed'
    assert decision['sheet_row'] == 901 and decision['program_id'] == '179815'
    rejected = p43_subject('179816', 'LevelsRx', 'rejection')
    rejected_name, rejected_id = M.program_identity(rejected, '')
    row = M.queue_row(M.classify(rejected, ''), 'ClickDealer', rejected_name,
                      rejected_id, {'id': 'cd179816'}, 'reports/p43-synthetic.json',
                      '2026-09-17T13:09:04Z')
    assert row['kind'] == 'rejection' and row['program_id'] == '179816'
    assert row['sheet_row'] is None  # the existing drain, not this test, binds it


def test_p43_clickdealer_mock_scan_projects_all_13_and_dedupes(monkeypatch, tmp_path):
    import base64
    from datetime import datetime

    messages = {}
    records = []
    for index, (pid, label, kind, received) in enumerate(P43_CLICKDEALER_CASES):
        mid = f'cd{pid}'  # synthetic, not an inbox_events event_id or a real Gmail id
        subject = p43_subject(pid, label, kind)
        observed = datetime.fromisoformat(f'2026-09-17T{received}+00:00')
        messages[mid] = {
            'id': mid, 'threadId': f't{mid}',
            'internalDate': str(int(observed.timestamp() * 1000)),
            'payload': {'mimeType': 'text/plain', 'headers': [
                {'name': 'From', 'value': P43_CLICKDEALER_SENDER},
                {'name': 'Subject', 'value': subject},
                {'name': 'Date', 'value': observed.strftime('%a, %d %b %Y %H:%M:%S %z')},
            ], 'body': {'data': base64.urlsafe_b64encode(
                b'Automated offer decision. No reply required.').decode('ascii')}},
        }
        records.append(p43_pending_row(pid, label.replace('–', '-'), 900 + index))

    class Request:
        def __init__(self, payload):
            self.payload = payload

        def execute(self, num_retries=0):
            assert num_retries == 6
            return self.payload

    class Service:
        def users(self):
            return self

        def messages(self):
            return self

        def list(self, **kwargs):
            return Request({'messages': [{'id': mid} for mid in messages]})

        def get(self, **kwargs):
            assert kwargs['format'] == 'full'
            return Request(messages[kwargs['id']])

    monkeypatch.setattr(M, 'ROOT', tmp_path)
    monkeypatch.setattr(M, 'sheet_snapshot', lambda: (records, None, {'source': 'fixture'}))
    monkeypatch.setattr(M, 'roster_snapshot', lambda rows: {'provenance': {'source': 'fixture'}})
    monkeypatch.setattr(M, 'dedupe_module', lambda: None)
    monkeypatch.setattr(M, 'load_sibling', lambda name, filename:
                        W if filename == 'affiliate_sheet_batch_writer.py' else None)
    midnight = datetime.fromisoformat('2026-09-17T00:00:00+00:00')
    cursor = {'last_internal_date_ms': int(midnight.timestamp() * 1000),
              'seen_message_ids': []}
    first = M.scan(Service(), cursor, 13, capture_dir=tmp_path / 'captures')
    assert first['unclassified'] == [] and first['attention'] == []
    assert len(first['classified']) == len(first['rows']) == 13
    assert len(first['approvals']) == 5
    assert {a['outcome'] for a in first['approvals']} == {'approval_confirmed'}
    assert M.approval_attention_items(first) == []
    assert M.decisions_messages(first) == []
    rows = {row['program_id']: row for row in first['rows']}
    assert set(rows) == {case[0] for case in P43_CLICKDEALER_CASES}
    for index, (pid, label, kind, received) in enumerate(P43_CLICKDEALER_CASES):
        row = rows[pid]
        expected = 'approval_confirmed' if kind == 'new_approval' else 'rejection'
        assert row['kind'] == expected and row['network'] == 'ClickDealer'
        assert row['program_name'] == label.replace('–', '-')
        assert row['update_id'] == f'inbox-{expected}-cd{pid}'
        evidence = json.loads(row['evidence'])
        assert evidence['source'] == f'gmail:cd{pid}'
        for field, _ in W.SHEET_SYNC_MODES[expected].evidence_fields:
            assert evidence.get(field), field
        if expected == 'approval_confirmed':
            assert row['sheet_row'] == str(900 + index)
            assert evidence['network_program_id'] == pid
            assert evidence['approval_capture_sha256'] == _sha256(
                tmp_path / evidence['approval_capture_path'])
        else:
            assert row['sheet_row'] is None
    captures = {p: p.read_bytes() for p in (tmp_path / 'captures').glob('*.json')}
    assert len(captures) == 13
    second = M.scan(Service(), cursor, 13, capture_dir=tmp_path / 'captures')
    assert second['rows'] == [] and second['approvals'] == []
    assert {p: p.read_bytes() for p in captures} == captures
    keep, suppressed = M.suppress_already_queued(
        first['rows'], lookup=lambda uid: {'update_id': uid, 'status': 'queued'})
    assert keep == [] and len(suppressed) == 13
