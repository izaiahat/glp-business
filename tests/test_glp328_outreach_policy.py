"""GLP-328 disabled outreach planner regressions; every record is fixture only."""
import copy
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import glp328_outreach_policy as policy
from affiliate_sheet_schema_guard import EXPECTED_HEADERS, REQUIRED_MARKETPLACE_PARENT_ROWS

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
CHECKED = (NOW - timedelta(hours=1)).isoformat()


def row(number, program, url, status='approved_active_tracking_ready', vertical='glp1-weightloss', **extra):
    fields = {'Program / Network': program, 'Official URL': url, 'Status': status,
              'Category': 'Affiliate program', 'Network / Route': 'Impact.com',
              'Fit Reason': 'fixture', 'Vertical': vertical, **extra}
    return {'row': number, 'values': [fields.get(h, '') for h in EXPECTED_HEADERS]}


def contact(email, host, name='Fixture Person', source=None, **extra):
    return {'email': email, 'organization_host': host, 'name': name, 'contact_type': 'business',
            'evidence': {'source_url': source or f'https://{host}/contact',
                         'observed_utc': CHECKED}, **extra}


def us(host):
    return {'country': 'US', 'source_url': f'https://{host}/about', 'observed_utc': CHECKED}


def snapshot(entries=()):
    return {'complete': True, 'checked_utc': CHECKED, 'entries': list(entries)}


def plan(rows, contacts, locations=None, suppression=None, conversation=None):
    hosts = {c['organization_host'] for c in contacts}
    return policy.plan_outreach(
        {'headers': list(EXPECTED_HEADERS), 'rows': rows}, contacts,
        {h: us(h) for h in hosts} if locations is None else locations,
        suppression or snapshot(), conversation or snapshot(), NOW)


def reasons(result):
    return {(e['row'] if 'row' in e else e['host']): e['reason'] for e in result['excluded']}


def test_plan_is_disabled_and_approved_pending_are_populations_not_permission():
    result = plan([row(2010, 'Alpha GLP', 'https://www.alpha.example/'),
                   row(2011, 'Beta GLP', 'https://beta.example', status='applied_pending_brand_approval')],
                  [contact('partners@alpha.example', 'alpha.example'),
                   contact('bd@beta.example', 'beta.example')])
    assert result['mode'] == 'disabled' and result['send_enabled'] is False
    got = {c['host']: c for c in result['candidates']}
    assert got['alpha.example']['population'] == 'approved'
    assert got['beta.example']['population'] == 'pending'
    assert all(c['send_permission'] is False for c in result['candidates'])

@pytest.mark.parametrize('status,population', [
    ('live_wired_verified', 'approved'),
    ('approved_active_not_wired', 'approved'),
    ('approved_active_wired_live', 'approved'),
    ('approved_active_tracking_ready', 'approved'),
    ('applied_pending_brand_approval', 'pending'),
    ('ready_to_apply', 'pending'),
    ('not_applied', 'pending'),
])
def test_actual_sheet_status_populations_are_not_lost(status, population):
    got = plan([row(2010, 'Alpha GLP', 'https://alpha.example', status=status)],
               [contact('bd@alpha.example', 'alpha.example')])
    assert got['candidates'][0]['population'] == population

def test_mailbox_safety_and_location_provenance_are_not_assumed():
    for address in ('privacy@alpha.example', 'patients@alpha.example', 'security@alpha.example'):
        got = plan([row(2010, 'Alpha GLP', 'https://alpha.example')],
                   [contact(address, 'alpha.example')])
        assert not got['candidates']
    got = plan([row(2010, 'Alpha GLP', 'https://alpha.example')],
               [contact('bd@alpha.example', 'alpha.example')],
               locations={'alpha.example': {'country': 'US', 'source_url': 'https://other.example/about',
                                            'observed_utc': CHECKED}})
    assert not got['candidates']


def test_dedupes_by_canonical_host_and_keeps_one_contact_per_org():
    result = plan([row(2010, 'Alpha GLP', 'https://www.alpha.example/a'),
                   row(2012, 'Alpha GLP (US)', 'http://ALPHA.example./b', status='pending_review')],
                  [contact('zed@alpha.example', 'www.alpha.example'),
                   contact('amy@alpha.example', 'alpha.example')])
    assert len(result['candidates']) == 1
    org = result['candidates'][0]
    assert org['rows'] == [2010, 2012] and org['population'] == 'approved'
    assert org['contact']['email'] == 'amy@alpha.example' and org['contacts_not_selected'] == 1


def test_reviewed_ww_weightwatchers_identity_without_losing_org_safety():
    # First-party /us/affiliate designates affiliates@ww.com; ww.com/us/ redirects
    # to weightwatchers.com/us/. Fixture inputs do not prove sendability or delivery.
    official = contact('affiliates@ww.com', 'weightwatchers.com',
                       source='https://www.weightwatchers.com/us/affiliate')
    brand = row(2020, 'WeightWatchers', 'https://www.weightwatchers.com/us/')
    alias = row(2021, 'WW US', 'https://www.ww.com/us/', status='pending_review')

    result = plan([brand], [official])
    assert result['mode'] == 'disabled' and result['send_enabled'] is False
    assert len(result['candidates']) == 1
    assert result['candidates'][0]['contact']['email'] == 'affiliates@ww.com'
    assert result['candidates'][0]['send_permission'] is False

    # The exact reviewed alias is an organization identity, not an email whitelist:
    # org dedupe, holds, suppressions and active conversations must cross it.
    result = plan([brand, alias], [official],
                  locations={'weightwatchers.com': us('weightwatchers.com'),
                             'ww.com': us('ww.com')})
    assert len(result['candidates']) == 1
    assert result['candidates'][0]['rows'] == [2020, 2021]
    assert result['candidates'][0]['contacts_not_selected'] == 0
    assert result['candidates'][0]['send_permission'] is False
    held = plan([brand, row(2021, 'WW hold', 'https://ww.com/us/', status='hold_legal')],
                [official])
    assert held['candidates'] == []
    assert reasons(held)['weightwatchers.com'] == 'status_hold_preserved'
    for suppression in ({'email': 'optout@ww.com'}, {'domain': 'ww.com'}):
        suppressed = plan([brand], [official], suppression=snapshot([suppression]))
        assert suppressed['candidates'] == []
        assert reasons(suppressed)['weightwatchers.com'] == 'organization_opted_out'
    active = plan([brand], [official],
                  conversation=snapshot([{'email': 'other@ww.com', 'state': 'active'}]))
    assert active['candidates'] == []
    assert reasons(active)['weightwatchers.com'] == 'organization_active_conversation'

    for bad, expected in (
        (contact('affiliates@other.com', 'weightwatchers.com',
                 source='https://www.weightwatchers.com/us/affiliate'),
         'contact_domain_not_official_host'),
        (contact('affiliates@ww.com', 'weightwatchers.com',
                 source='https://unrelated.example/affiliate'),
         'contact_evidence_not_official_source'),
        (contact('contact@ww.com', 'weightwatchers.com',
                 source='https://www.weightwatchers.com/us/affiliate'),
         'contact_unreachable_mailbox'),
        (contact('affiliates@ww.com', 'weightwatchers.com', contact_type='personal',
                 source='https://www.weightwatchers.com/us/affiliate'),
         'contact_not_business'),
    ):
        rejected = plan([brand], [bad])
        assert rejected['candidates'] == []
        assert reasons(rejected)['weightwatchers.com'] == expected
    no_us = plan([brand], [official], locations={})
    assert no_us['candidates'] == []
    assert reasons(no_us)['weightwatchers.com'] == 'us_location_evidence_missing'


def test_fella_holds_nonproviders_nonglp_and_non_population_statuses_excluded():
    parent = REQUIRED_MARKETPLACE_PARENT_ROWS['Impact.com']
    rows = [row(2020, 'Fella Health', 'https://fella.example'),
            row(2021, 'Held GLP', 'https://held.example', status='hold_sister_site_not_live'),
            row(parent, 'Impact.com', 'https://impact.example'),
            row(2023, 'Some Network', 'https://net.example', Category='Network rail'),
            row(2024, 'Hair Co', 'https://hair.example', vertical='mens-health-hair'),
            row(2025, 'Rejected GLP', 'https://rej.example', status='rejected'),
            row(2026, 'Not Approved GLP', 'https://notap.example', status='not_approved'),
            row(2027, 'Pending Fit', 'https://fit.example', **{'Fit Reason': 'fit gate pending'})]
    hosts = ['fella.example', 'held.example', 'impact.example', 'net.example',
             'hair.example', 'rej.example', 'notap.example', 'fit.example']
    result = plan(rows, [contact(f'bd@{h}', h) for h in hosts])
    assert result['candidates'] == []
    got = reasons(result)
    assert got[2020] == 'permanent_fella_exclusion'
    assert got[2021] == 'status_hold_preserved'
    assert got[parent] == 'canonical_marketplace_parent_not_provider'
    assert got[2023] == 'network_account_not_provider'
    assert got[2024] == 'non_glp_vertical:mens-health-hair'
    assert got[2025] == got[2026] == 'status_not_candidate_population'
    assert got[2027] == 'row_fit_ruling_pending'


def test_hold_or_fella_row_blocks_whole_organization_on_same_host():
    result = plan([row(2030, 'Gamma GLP', 'https://gamma.example'),
                   row(2031, 'Gamma GLP legacy', 'https://www.gamma.example', status='hold_legal')],
                  [contact('bd@gamma.example', 'gamma.example')])
    assert result['candidates'] == []
    assert reasons(result)['gamma.example'] == 'status_hold_preserved'

@pytest.mark.parametrize('blocked_program,blocked_status,expected_reason', [
    ('Held GLP', 'hold_legal', 'status_hold_preserved'),
    ('Fella Health', 'approved_active_tracking_ready', 'permanent_fella_exclusion'),
])
def test_f1_parent_hold_or_fella_blocks_all_sibling_subdomains(
        blocked_program, blocked_status, expected_reason):
    result = plan([
        row(2032, blocked_program, 'https://alpha.example', status=blocked_status),
        row(2033, 'Alpha US GLP', 'https://us.alpha.example'),
        row(2034, 'Alpha EU GLP', 'https://eu.alpha.example'),
    ], [contact('bd@us.alpha.example', 'us.alpha.example'),
        contact('bd@eu.alpha.example', 'eu.alpha.example')])
    assert not result['candidates']
    assert reasons(result)[2032] == expected_reason
    assert all(reasons(result)[h] == expected_reason
               for h in ('us.alpha.example', 'eu.alpha.example'))

def test_f2_parent_subdomain_one_recipient_preserves_program_provenance():
    result = plan([
        row(2035, 'Alpha GLP', 'https://alpha.example'),
        row(2036, 'Alpha US GLP', 'https://us.alpha.example', status='pending_review'),
    ], [contact('bd@alpha.example', 'alpha.example'),
        contact('bd@alpha.example', 'us.alpha.example')])
    assert len(result['candidates']) == 1
    candidate = result['candidates'][0]
    assert candidate['contact']['email'] == 'bd@alpha.example'
    assert candidate['rows'] == [2035, 2036]
    assert candidate['program_rows'] == [
        {'row': 2035, 'program': 'Alpha GLP', 'host': 'alpha.example', 'population': 'approved'},
        {'row': 2036, 'program': 'Alpha US GLP', 'host': 'us.alpha.example', 'population': 'pending'},
    ]
    assert candidate['contacts_not_selected'] == 0
    assert candidate['send_permission'] is False

@pytest.mark.parametrize('mailbox', ['records', 'patient.records'])
def test_f3_existing_patient_records_prefixes_stay_excluded(mailbox):
    result = plan([row(2037, 'Alpha GLP', 'https://alpha.example')],
                  [contact(f'{mailbox}@alpha.example', 'alpha.example')])
    assert not result['candidates']
    assert reasons(result)['alpha.example'] == 'contact_unreachable_mailbox'


def test_us_location_evidence_required():
    rows = [row(2040, 'Delta', 'https://delta.example'), row(2041, 'Eps', 'https://eps.example')]
    contacts = [contact('bd@delta.example', 'delta.example'), contact('bd@eps.example', 'eps.example')]
    result = plan(rows, contacts, locations={'eps.example': {**us('eps.example'), 'country': 'CA'}})
    got = reasons(result)
    assert got['delta.example'] == 'us_location_evidence_missing'
    assert got['eps.example'] == 'us_location_not_evidenced'


def test_opt_out_and_active_conversation_fail_closed_at_organization_level():
    rows = [row(2050, 'Zeta', 'https://zeta.example'), row(2051, 'Eta', 'https://eta.example'),
            row(2052, 'Theta', 'https://theta.example')]
    contacts = [contact('new@zeta.example', 'zeta.example'), contact('new@eta.example', 'eta.example'),
                contact('new@theta.example', 'theta.example')]
    result = plan(rows, contacts,
                  suppression=snapshot([{'domain': 'mail.zeta.example'}]),
                  conversation=snapshot([{'email': 'ceo@eta.example', 'state': 'active'},
                                         {'email': 'old@theta.example', 'state': 'closed'}]))
    got = reasons(result)
    assert got['zeta.example'] == 'organization_opted_out'
    assert got['eta.example'] == 'organization_active_conversation'
    assert [c['host'] for c in result['candidates']] == ['theta.example']


@pytest.mark.parametrize('bad', [
    {'complete': False, 'checked_utc': CHECKED, 'entries': []},
    {'complete': True, 'entries': []},
    {'complete': True, 'checked_utc': (NOW - timedelta(days=2)).isoformat(), 'entries': []},
    {'complete': True, 'checked_utc': (NOW + timedelta(hours=1)).isoformat(), 'entries': []},
    {'complete': True, 'checked_utc': CHECKED, 'entries': [{'note': 'no identity'}]},
])
def test_incomplete_or_unchecked_snapshots_fail_whole_plan(bad):
    rows = [row(2060, 'Iota', 'https://iota.example')]
    contacts = [contact('bd@iota.example', 'iota.example')]
    with pytest.raises(policy.OutreachPolicyError):
        plan(rows, contacts, suppression=bad)
    with pytest.raises(policy.OutreachPolicyError):
        plan(rows, contacts, conversation=bad)


def test_noncanonical_headers_fail_closed():
    with pytest.raises(policy.OutreachPolicyError, match='sheet_headers_not_canonical'):
        policy.plan_outreach({'headers': ['x'], 'rows': []}, [], {}, snapshot(), snapshot(), NOW)


@pytest.mark.parametrize('candidate,reason', [
    (None, 'no_evidenced_business_contact'),
    (contact('someone@gmail.com', 'kappa.example'), 'contact_domain_not_official_host'),
    (contact('bd@other.example', 'kappa.example'), 'contact_domain_not_official_host'),
    (contact('noreply@kappa.example', 'kappa.example'), 'contact_unreachable_mailbox'),
    (contact('bd@kappa.example', 'kappa.example', contact_type='personal'), 'contact_not_business'),
    (contact('bd@kappa.example', 'kappa.example', source='https://scraper.example/x'),
     'contact_evidence_not_official_source'),
    ({**contact('bd@kappa.example', 'kappa.example'), 'evidence': None}, 'contact_evidence_missing'),
])
def test_missing_or_unofficial_contact_evidence_fails_closed(candidate, reason):
    contacts = [candidate] if candidate else []
    result = policy.plan_outreach(
        {'headers': list(EXPECTED_HEADERS), 'rows': [row(2070, 'Kappa', 'https://kappa.example')]},
        contacts, {'kappa.example': us('kappa.example')}, snapshot(), snapshot(), NOW)
    assert result['candidates'] == []
    assert reasons(result)['kappa.example'] == reason


def test_inputs_are_not_mutated():
    sheet = {'headers': list(EXPECTED_HEADERS), 'rows': [row(2080, 'Lambda', 'https://lambda.example')]}
    contacts = [contact('bd@lambda.example', 'lambda.example')]
    locations = {'lambda.example': us('lambda.example')}
    before = copy.deepcopy((sheet, contacts, locations))
    policy.plan_outreach(sheet, contacts, locations, snapshot(), snapshot(), NOW)
    assert (sheet, contacts, locations) == before


def _candidate():
    return plan([row(2090, 'Mu GLP', 'https://mu.example')],
                [contact('bd@mu.example', 'mu.example', name='Pat')])['candidates'][0]


def test_preview_requires_explicit_unapproved_local_template_and_is_unsendable():
    template = {'status': 'UNAPPROVED', 'source': 'local', 'subject': 'Sponsorship for {organization}',
                'body': 'Hi {contact_name}, about {official_host}.'}
    preview = policy.render_preview(_candidate(), template)
    assert preview['sendable'] is False and preview['send_permission'] is False
    assert preview['template_status'] == 'UNAPPROVED' and 'NOT SENDABLE' in preview['banner']
    assert preview['subject'] == 'Sponsorship for Mu GLP'
    assert preview['body'] == 'Hi Pat, about mu.example.'
    for bad in ({**template, 'status': 'APPROVED'}, {k: v for k, v in template.items() if k != 'status'},
                {**template, 'source': 'gmail_drafts'}, {**template, 'body': 'Hi {email}'},
                {**template, 'subject': ''}):
        with pytest.raises(policy.OutreachPolicyError):
            policy.render_preview(_candidate(), bad)


SENT = NOW - timedelta(days=8)


def receipt(**extra):
    # Structural unit-test input only; never a claimed pilot receipt.
    return {'verified': True, 'verification_source': 'gmail_exact_sent_readback',
            'sender': 'partners@glpclinicscout.com', 'message_id': 'fixture-1',
            'provider_message_id': 'fixture-1', 'provider_thread_id': 'fixture-thread',
            'checks': {'all_passed': True},
            'kind': 'initial', 'recipient': 'bd@nu.example', 'sent_utc': SENT.isoformat(), **extra}


def follow_up(send=None, prior=(), replies=None, suppression=None, now=NOW):
    return policy.decide_follow_up(receipt() if send is None else send, list(prior), replies or snapshot(),
                                   suppression or snapshot(), now)


def test_follow_up_allowed_once_after_seven_days_but_still_unsendable():
    decision = follow_up()
    assert decision['eligible'] is True and decision['sendable'] is False
    assert decision['send_permission'] is False
    assert decision['earliest_utc'] == (SENT + timedelta(days=7)).isoformat()


@pytest.mark.parametrize('send,reason', [
    ({'verified': False}, 'send_receipt_not_verified'),
    ({'verification_source': ''}, 'send_receipt_not_verified'),
    ({'pilot': True}, 'send_receipt_not_real'),
    ({'simulated': True}, 'send_receipt_not_real'),
    ({'verification_source': 'fixture-provider-receipt'}, 'send_receipt_readback_missing'),
    ({'sender': 'other@example.com'}, 'send_receipt_readback_missing'),
    ({'checks': {'all_passed': False}}, 'send_receipt_readback_missing'),
    ({'kind': 'follow_up'}, 'send_receipt_not_initial'),
])
def test_follow_up_requires_prior_verified_real_send(send, reason):
    assert follow_up(send=receipt(**send))['reason'] == reason
    assert follow_up(send={})['reason'] == 'send_receipt_not_verified'


def test_follow_up_limits_and_timing():
    assert follow_up(prior=[{'recipient': 'other@nu.example'}])['reason'] == 'follow_up_limit_reached'
    assert follow_up(send=receipt(sent_utc=(NOW - timedelta(days=6, hours=23)).isoformat()))[
        'reason'] == 'follow_up_too_soon'
    with pytest.raises(policy.OutreachPolicyError):
        follow_up(prior=[{'note': 'unknown'}])


@pytest.mark.parametrize('kind', ['reply', 'bounce', 'opt_out'])
def test_follow_up_stops_on_reply_bounce_or_opt_out_event(kind):
    decision = follow_up(replies=snapshot([{'email': 'someone@nu.example', 'kind': kind}]))
    assert decision['eligible'] is False and decision['reason'] == f'stop_{kind}'


def test_follow_up_stops_on_suppression_and_requires_fresh_snapshots_after_send():
    assert follow_up(suppression=snapshot([{'email': 'bd@nu.example'}]))['reason'] == 'stop_opt_out'
    stale = {'complete': True, 'checked_utc': (SENT - timedelta(hours=1)).isoformat(), 'entries': []}
    with pytest.raises(policy.OutreachPolicyError, match='stale|not_after_send'):
        follow_up(replies=stale)
    recent_send = receipt(sent_utc=(NOW - timedelta(minutes=30)).isoformat())
    with pytest.raises(policy.OutreachPolicyError, match='reply_snapshot_not_after_send'):
        follow_up(send=recent_send)
    with pytest.raises(policy.OutreachPolicyError, match='reply_event_kind_unknown'):
        follow_up(replies=snapshot([{'email': 'x@nu.example', 'kind': 'seen'}]))


def test_module_has_no_io_send_or_network_surface():
    source = (ROOT / 'scripts/glp328_outreach_policy.py').read_text()
    imports = set(re.findall(r'^(?:from|import) ([\w.]+)', source, re.M))
    assert imports == {'__future__', 'copy', 're', 'datetime', 'ipaddress', 'typing',
                       'urllib.parse', 'affiliate_provider_row_scope', 'affiliate_sheet_schema_guard'}
    for banned in ('open(', 'write_text', 'write_bytes', 'subprocess', 'socket', '.send(', 'build('):
        assert banned not in source
