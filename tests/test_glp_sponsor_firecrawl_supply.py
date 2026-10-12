import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import glp_sponsor_firecrawl_supply as supply  # noqa: E402

AT = '2026-10-12T03:00:00+00:00'
LOCALS = re.compile(r'^(?:affiliates?|affiliaterelations|partnerships?|partners?|business|ambassadors|advocates)$', re.I)


def hits_from(markup):
    """Minimal stand-in for campaign.public_page_hits: visible text + mailto."""
    text = re.sub(r'<[^>]+>', ' ', markup)
    text = ' '.join(text.split())
    hits = []
    for address in re.findall(r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}', text, re.I):
        i = text.find(address)
        hits.append({'address': address.lower(), 'how': 'published_text', 'excerpt': text[max(0, i - 180):i + 180]})
    return hits, text


def same(a, b):
    return a == b or a.endswith('.' + b) or b.endswith('.' + a)


def ev(markup, host='example.com'):
    return supply.evidence(host, 'https://example.com/p', markup, 'a' * 64, AT, hits_from, LOCALS, same)


def test_medication_sentence_requires_first_party_prescribing_claim():
    rows, _ = ev('<p>Our doctors prescribe semaglutide when appropriate for your treatment.</p>')
    assert rows and rows[0]['medication_provider_excerpt'].startswith('Our doctors prescribe semaglutide')
    for bad in ('<p>We do not prescribe semaglutide or any medication.</p>',
                '<p>Learn about GLP-1 medication in our education hub.</p>',
                '<p>Do we prescribe GLP-1 medication to everyone?</p>'):
        assert not ev(bad)[0], bad


def test_us_scope_is_affirmative_service_scope_not_pharmacopeia_or_question():
    rows, _ = ev('<p>Our providers are licensed in all 50 states.</p>')
    assert rows and rows[0]['country'] == 'US'
    for bad in ('<p>Compounded under United States Pharmacopeia standards across facilities.</p>',
                '<p>Do you ship to all 50 states?</p>',
                '<p>Our service is not available in all 50 states yet.</p>',
                '<p>Copyright 2026 Example, United States.</p>'):
        assert not ev(bad)[0], bad
    # "U.S." is not a sentence boundary.
    rows, _ = ev('<p>Lttl is available to adults located in the U.S. and nowhere else.</p>')
    assert rows and 'U.S. and nowhere else' in rows[0]['country_excerpt']


def test_published_contact_must_be_business_local_same_org_partner_context():
    _, contacts = ev('<p>For partnerships and affiliate marketing email partners@example.com today.</p>')
    assert [c['address'] for c in contacts] == ['partners@example.com']
    assert contacts[0]['tier'] == 'partnerships' and contacts[0]['page_sha256'] == 'a' * 64
    for bad in ('<p>For partnerships email jane.doe@example.com</p>',          # personal, never pattern-built
                '<p>For partnerships email partners@other.com</p>',           # other org
                '<p>Patient billing questions: partners@example.com</p>',     # support context
                '<p>Say hi at partners@example.com</p>'):                     # no business purpose
        assert not ev(bad)[1], bad


def test_eligible_hosts_excludes_fella_suppressed_holds_and_non_gap_reasons():
    rows = [{'host': 'a.com', 'reason': 'us_program_scope_unknown'},
            {'host': 'b.com', 'reason': 'no_verified_business_purpose_contact'},
            {'host': 'b.com', 'reason': 'status_hold_preserved'},
            {'host': 'fellahealth.com', 'reason': 'us_program_scope_unknown'},
            {'host': 'held.com', 'reason': 'not_glp_medication_provider'},
            {'host': 'uk.co.uk', 'reason': 'us_program_scope_unknown'}]
    assert supply.eligible_hosts(rows, {'held.com'}, lambda a, b: a == b) == ['a.com']


def test_apollo_contact_binds_id_title_country_org_and_email_domain():
    at = dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc)
    title = lambda t: bool(re.search('marketing|partnership', t or '', re.I))  # noqa: E731
    good = {'id': 'p1', 'title': 'Director of Marketing', 'email': 'Jo@Example.com', 'country': 'United States',
            'organization': {'primary_domain': 'example.com'}}
    row = supply.apollo_contact('example.com', good, 'p1', title, at, {'person': good})
    assert row['email'] == 'jo@example.com' and row['send_eligible'] is False and row['verification_status'] == 'unknown'
    for change in ({'id': 'p2'}, {'title': 'Patient Support'}, {'country': 'Canada'}, {'email': 'jo@gmail.com'},
                   {'organization': {'primary_domain': 'parent.com'}}):
        assert supply.apollo_contact('example.com', good | change, 'p1', title, at, {}) is None, change


def test_budget_respects_run_cap_and_three_thousand_floor():
    b = supply.Budget(remaining=10000, cap=100)
    assert b.allow(100) and not b.allow(101)
    b.charge(90)
    assert not b.allow(11)
    near_floor = supply.Budget(remaining=3050, cap=1000)
    assert near_floor.allow(49) and not near_floor.allow(50)


def test_daily_stops_at_credit_floor_without_any_effect(monkeypatch, tmp_path):
    p = supply.Producer.__new__(supply.Producer)
    p.budget = supply.Budget(remaining=supply.FLOOR_CREDITS)
    p.report = {}
    called = []
    p.plan = lambda: called.append('plan')
    assert p.daily()['status'] == 'STOPPED_CREDIT_FLOOR' and not called


def test_module_has_no_send_or_transport_surface():
    source = Path(supply.__file__).read_text()
    for forbidden in ('send_message', 'smtplib', "service('send')", 'send_enabled', 'campaign.json'):
        assert forbidden not in source, forbidden
    assert json.dumps  # stdlib only


def test_merged_rows_are_reaccepted_by_installed_scope_gate_with_real_page_parser():
    import hashlib
    import glp_sponsor_campaign as campaign
    html = ('<html><body><h1>Example</h1><p>Our providers prescribe tirzepatide as a weekly treatment.</p>'
            '<p>Example is licensed in all 50 states.</p><script>var x="we offer semaglutide prescription";</script></body></html>')
    sha = hashlib.sha256(html.encode('utf-8', 'replace')).hexdigest()
    at = dt.datetime.now(dt.timezone.utc)
    rows, _ = supply.evidence('example.com', 'https://example.com/glp', html, sha, at.isoformat(),
                              campaign.public_page_hits, campaign.BUSINESS_LOCALS, lambda a, b: a == b)
    assert {bool(r['medication_provider_excerpt']) for r in rows} == {True, False}
    org = {'scope_evidence': rows, 'discovery': {'_markup': [{'url': 'https://example.com/glp', 'final_url': 'https://example.com/glp', 'html': html, 'sha256': sha}]}}
    assert len(campaign.scope_evidence_current('example.com', org, at)) == 2
    # Script-only text is never evidence.
    assert 'semaglutide' not in ' '.join(r['medication_provider_excerpt'] for r in rows)


def test_nonregistry_sponsors_require_current_us_fit_and_preserve_all_exclusions(monkeypatch):
    import hashlib
    import glp_sponsor_campaign as campaign
    import glp328_outreach_policy as policy
    at = dt.datetime.now(dt.timezone.utc)
    def org(host, text):
        html = '<p>' + text + '</p>'
        sha = hashlib.sha256(html.encode()).hexdigest()
        rows, contacts = supply.evidence(host, 'https://' + host + '/', html, sha, at.isoformat(),
                                        campaign.public_page_hits, campaign.BUSINESS_LOCALS, campaign.same, sponsor=True)
        return {'provider': host, 'scope_evidence': rows, 'discovery': {'_markup': [
            {'html': html, 'sha256': sha, 'url': 'https://' + host + '/', 'final_url': 'https://' + host + '/'}], 'candidates': contacts}}
    text = 'We sell protein nutrition shakes. We ship to customers across the United States. For partnerships email partners@{host}.'
    hosts = ['nutrition.example', 'fella.example', 'held.example', 'suppressed.example', 'conversation.example', 'research.example']
    organizations = {h: org(h, text.format(host=h)) for h in hosts}
    organizations['research.example'] = org('research.example', text.format(host='research.example') + ' Our peptides are for research use only and not for human consumption.')
    organizations['unknown.example'] = org('unknown.example', 'We sell protein nutrition shakes. For partnerships email partners@unknown.example.')
    values = [list(policy.EXPECTED_HEADERS), [''] * len(policy.EXPECTED_HEADERS)]
    monkeypatch.setattr(policy, 'classify_row', lambda i, r: {'host':'held.example','program':'Held','eligible':False,'blocks_org':True,'row':i,'reason':'status_hold_preserved'})
    local = {'suppression': {'complete':True, 'entries':[{'domain':'suppressed.example'}]},
             'conversations':[{'domain':'conversation.example'}]}
    plan = campaign.recipients(values, {'organizations':organizations}, local)
    assert [r['host'] for r in plan['recipients']] == ['nutrition.example']
    # A forged excerpt, stale evidence, or an unbound page cannot qualify a sponsor.
    next(r for r in organizations['nutrition.example']['scope_evidence'] if r.get('sponsor_category'))['sponsor_category_excerpt'] = 'We sell fitness coaching.'
    assert not campaign.recipients(values, {'organizations':organizations}, local)['recipients']
    organizations['nutrition.example'] = org('nutrition.example', text.format(host='nutrition.example'))
    for row in organizations['nutrition.example']['scope_evidence']:
        row['observed_utc'] = '2020-01-01T00:00:00+00:00'
    assert not campaign.recipients(values, {'organizations':organizations}, local)['recipients']
