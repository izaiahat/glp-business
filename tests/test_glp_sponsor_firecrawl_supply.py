import datetime as dt
import json
import re
import sys
import pytest
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


def test_unsealed_supply_helpers_cannot_change_campaign_admission(monkeypatch):
    import hashlib
    import glp_sponsor_campaign as campaign
    import glp328_outreach_policy as policy
    host = 'unqualified.example'
    html = '<p>Commercial partnerships email partners@unqualified.example</p>'
    sha = hashlib.sha256(html.encode()).hexdigest()
    observed = dt.datetime.now(dt.timezone.utc).isoformat()
    rows, contacts = supply.evidence(host, 'https://'+host+'/', html, sha, observed,
        campaign.public_page_hits, campaign.BUSINESS_LOCALS, campaign.same, sponsor=True)
    discovery = {'organizations': {host: {'provider':host, 'scope_evidence':rows,
        'discovery': {'_markup':[{'html':html,'sha256':sha,'url':'https://'+host+'/','final_url':'https://'+host+'/'}], 'candidates':contacts}}}}
    local = {'suppression':{'complete':True,'entries':[]}, 'conversations':[]}
    values = [list(policy.EXPECTED_HEADERS)]
    assert campaign.recipients(values, discovery, local)['recipients'] == []
    monkeypatch.setattr(supply, 'sponsor_rows', lambda *a: {host:[{'host':host,'program':host,'population':'pending','eligible':True,'row':None}]}, raising=False)
    assert campaign.recipients(values, discovery, local)['recipients'] == []


def test_sponsor_fit_refuses_negated_product_claims():
    for text in ("We don't offer meal plans.", "We are not a meal delivery service.",
                 "We no longer provide personal training.", "We do not offer protein shakes.",
                 "We don’t sell protein shakes."):
        assert supply.sponsor_fit(text) is None, text
    assert supply.sponsor_fit('We offer protein shakes for our customers.')[0] == 'nutrition_protein'


def test_no_product_quantifier_refused_by_producer_and_sealed_census():
    import hashlib
    import glp_sponsor_campaign as campaign
    import glp328_outreach_policy as policy
    at = dt.datetime.now(dt.timezone.utc)
    host = 'negative.example'
    local = {'suppression': {'complete': True, 'entries': []}, 'conversations': []}
    for statement, category in (('We offer no protein shakes.', 'nutrition_protein'),
                                ('We provide no meal delivery service.', 'meals')):
        assert supply.sponsor_fit(statement) is None
        assert campaign.sponsor_fit(statement) is None
        # Even a proposed category row anchored to real first-party negative text
        # must not authorize the organization through the actual sender census.
        text = statement + ' We ship to customers across the United States. Partnerships partners@negative.example.'
        html = '<p>' + text + '</p>'
        sha = hashlib.sha256(html.encode()).hexdigest()
        url = 'https://' + host + '/'
        rows, contacts = supply.evidence(host, url, html, sha, at.isoformat(),
            campaign.public_page_hits, campaign.BUSINESS_LOCALS, campaign.same, sponsor=True)
        rows.append({'source_url': url, 'sponsor_category': category,
                     'sponsor_category_excerpt': statement, 'observed_utc': at.isoformat(), 'sha256': sha})
        org = {'provider': host, 'scope_evidence': rows, 'discovery': {'candidates': contacts,
            '_markup': [{'url': url, 'final_url': url, 'html': html, 'sha256': sha}]}}
        assert campaign.recipients([list(policy.EXPECTED_HEADERS)],
            {'organizations': {host: org}}, local)['recipients'] == []
    assert supply.sponsor_fit('We offer protein shakes.')[0] == 'nutrition_protein'
    assert campaign.sponsor_fit('We provide meal delivery service.')[0] == 'meals'


def test_correlative_negation_refused_by_helpers_and_sealed_census():
    import hashlib
    import glp_sponsor_campaign as campaign
    import glp328_outreach_policy as policy
    at = dt.datetime.now(dt.timezone.utc)
    host = 'negative.example'
    local = {'suppression': {'complete': True, 'entries': []}, 'conversations': []}
    for statement in ('We offer neither protein shakes nor meal delivery.',
                      'Our products include neither protein shakes nor prepared meals.'):
        html = '<p>' + statement + ' We ship to customers across the United States. Partnerships partners@negative.example.</p>'
        sha = hashlib.sha256(html.encode()).hexdigest()
        url = 'https://' + host + '/'
        rows, contacts = supply.evidence(host, url, html, sha, at.isoformat(),
            campaign.public_page_hits, campaign.BUSINESS_LOCALS, campaign.same, sponsor=True)
        rows.append({'source_url': url, 'sponsor_category': 'nutrition_protein',
                     'sponsor_category_excerpt': statement, 'observed_utc': at.isoformat(), 'sha256': sha})
        org = {'provider': host, 'scope_evidence': rows, 'discovery': {'candidates': contacts,
            '_markup': [{'url': url, 'final_url': url, 'html': html, 'sha256': sha}]}}
        assert campaign.recipients([list(policy.EXPECTED_HEADERS)],
            {'organizations': {host: org}}, local)['recipients'] == []
        assert supply.sponsor_fit(statement) is None
        assert campaign.sponsor_fit(statement) is None


def test_negative_context_cannot_be_lost_by_excerpt_clipping():
    import hashlib
    import glp_sponsor_campaign as campaign
    import glp328_outreach_policy as policy
    at = dt.datetime.now(dt.timezone.utc)
    host = 'negative.example'
    local = {'suppression': {'complete': True, 'entries': []}, 'conversations': []}
    statement = 'We offer no ' + 'vitamin tablets, ' * 12 + 'protein shakes for our customers.'
    html = '<p>' + statement + ' We ship to customers across the United States. Partnerships partners@negative.example.</p>'
    sha = hashlib.sha256(html.encode()).hexdigest()
    url = 'https://' + host + '/'
    rows, contacts = supply.evidence(host, url, html, sha, at.isoformat(),
        campaign.public_page_hits, campaign.BUSINESS_LOCALS, campaign.same, sponsor=True)
    org = {'provider': host, 'scope_evidence': rows, 'discovery': {'candidates': contacts,
        '_markup': [{'url': url, 'final_url': url, 'html': html, 'sha256': sha}]}}
    assert campaign.recipients([list(policy.EXPECTED_HEADERS)],
        {'organizations': {host: org}}, local)['recipients'] == []
    assert supply.sponsor_fit(statement) is None
    assert campaign.sponsor_fit(statement) is None
    # Historical/clipped proposals cannot discard the negator from the page.
    rows.append({'source_url': url, 'sponsor_category': 'nutrition_protein',
        'sponsor_category_excerpt': 'protein shakes for our customers.',
        'observed_utc': at.isoformat(), 'sha256': sha})
    assert campaign.recipients([list(policy.EXPECTED_HEADERS)],
        {'organizations': {host: org}}, local)['recipients'] == []
    assert not any(x.get('sponsor_category') for x in supply.sponsor_scope_current(host, org, campaign, at))
    # Retain conservative refusal of ambiguous double negatives/not-only claims;
    # a separate explicit affirmative source is required, not NLP inference.
    for text in ('We do not offer no protein shakes.',
                 'We offer not only protein shakes but also meal delivery.',
                 'We offer no protein shakes, meal plans or personal training.'):
        assert supply.sponsor_fit(text) is None
        assert campaign.sponsor_fit(text) is None
    for text in ('We offer protein shakes, meal delivery and personal training.',
                 'Our protein shakes contain vitamins for our customers.'):
        assert supply.sponsor_fit(text)[0] == 'nutrition_protein'
        assert campaign.sponsor_fit(text)[0] == 'nutrition_protein'


def test_failed_capture_reserves_credits_and_next_run_cannot_reset(monkeypatch, tmp_path):
    import firecrawl_client
    import glp_firecrawl_jobs as jobs
    calls = []
    class Client:
        def __init__(self, *a, **kw): pass
        def request(self, endpoint, *a, **kw):
            if endpoint == 'team/credit-usage': return {'data':{'remainingCredits':10000}}
            calls.append(endpoint)
            raise OSError('uncertain paid effect')
        def scrape(self, *a, **kw):
            calls.append('scrape')
            raise OSError('uncertain paid effect')
    monkeypatch.setattr(firecrawl_client, 'FirecrawlClient', Client)
    monkeypatch.setattr(jobs, 'spent_today', lambda *a: 0)
    p = supply.Producer(state_dir=tmp_path, run_cap=3)
    p.capture_host('first.example', max_pages=1)
    assert p.budget.spent == 2 and calls == ['map','scrape']
    p = supply.Producer(state_dir=tmp_path, run_cap=3)
    assert p.budget.cap == 1
    assert p.capture_host('second.example', max_pages=1)['skipped'] == 'budget'
    assert calls == ['map','scrape']


def test_entrypoint_budget_lock_refuses_before_usage_or_paid_call(monkeypatch, tmp_path):
    import fcntl
    import pytest
    import glp_sponsor_campaign as campaign
    monkeypatch.setattr(campaign, 'STATE', tmp_path)
    monkeypatch.setattr(supply, 'Producer', lambda **kw: pytest.fail('contender entered producer'))
    with (tmp_path/'firecrawl-supply-budget.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            supply.main(['daily'])
        with pytest.raises(BlockingIOError):
            supply.main(['merge', '--retained-cohort', 'unused.json', '--cohort-sha256', '0' * 64])


def test_daily_cap_counts_legacy_and_installed_callers_without_reset(monkeypatch, tmp_path):
    import firecrawl_client
    import glp_firecrawl_jobs as jobs
    class Client:
        def __init__(self, *a, **kw): pass
        def request(self, *a, **kw): return {'data': {'remainingCredits': 10000}}
    monkeypatch.setattr(firecrawl_client, 'FirecrawlClient', Client)
    seen = []
    def spent(callers):
        seen.extend(callers)
        return 3833
    monkeypatch.setattr(jobs, 'spent_today', spent)
    p = supply.Producer(state_dir=tmp_path)
    assert 'fc71_sponsor_supply' in seen and 'fc71_sponsor_alexandria' in seen
    assert p.budget.cap == 0 and not p.budget.allow(1)


def test_apollo_discovers_current_contracts_before_executing_renamed_ids(tmp_path):
    """Catalogue IDs are observations, not a pinned filter or execution fallback."""
    import glp_sponsor_alexandria as alexandria
    calls = []
    ids = {'search': 'contacts/preview-v3', 'match': 'contacts/resolve-v3'}
    class Client:
        def search(self, payload):
            calls.append(('search', payload))
            assert payload['sources'] == ['alexandria']
            return {'success': True, 'creditsUsed': 0, 'data': {'tools': [
                {'provider': 'apollo', 'capability': value} for value in ids.values()]}}
        def scrape(self, payload):
            calls.append(('scrape', payload))
            row = payload['alexandria'][0]
            if row['provider'] == 'firecrawl':
                assert set(row['options']['capabilities']) == set(ids.values())
                return {'success': True, 'data': {'alexandria': [{
                    'provider': 'firecrawl', 'capability': 'find-tools', 'creditsCost': 0,
                    'data': {'items': [
                        {'provider': 'apollo', 'capability': ids['search'], 'creditsCost': 0,
                         'options': [{'name': k} for k in ('q_organization_domains_list', 'person_locations', 'page', 'per_page')],
                         'execute': {'provider': 'apollo', 'capability': ids['search']}},
                        {'provider': 'apollo', 'capability': ids['match'], 'creditsCost': 30,
                         'billing': {'maxCredits': 30},
                         'options': [{'name': k} for k in ('id', 'reveal_personal_emails', 'reveal_phone_number')],
                         'execute': {'provider': 'apollo', 'capability': ids['match']}}]}}]}}
            assert row['provider'] == 'apollo'
            if row['capability'] == ids['search']:
                data = {'people': [{'id': 'p1', 'title': 'Marketing', 'has_email': True}]}
            else:
                assert row['capability'] == ids['match']
                assert row['options'] == {'id': 'p1', 'reveal_personal_emails': False, 'reveal_phone_number': False}
                data = {'person': {'id': 'p1', 'title': 'Marketing', 'email': 'work@example.com',
                    'country': 'United States', 'organization': {'primary_domain': 'example.com'}, 'phone': 'discard'}}
            return {'success': True, 'data': {'alexandria': [row | {'upstreamStatus': 200, 'data': data}]}}
    p = supply.Producer.__new__(supply.Producer)
    p.alex = Client()
    p.report = {}
    contracts = p.discover_apollo_contacts()
    assert contracts['people/search'] == ids['search'] and contracts['people/match'] == ids['match']
    result = alexandria.observe(tmp_path, 'example.com', 'people/search',
        {'q_organization_domains_list': ['example.com']}, p.apollo_scrape, dt.datetime.now(dt.timezone.utc))
    assert result['people'][0]['id'] == 'p1'
    assert len(calls) == 3
    reserved = []
    p.reserve = reserved.append
    match_options = {'id': 'p1', 'reveal_personal_emails': False, 'reveal_phone_number': False}
    matched = alexandria.observe(tmp_path, 'example.com', 'people/match', match_options,
        p.paid_match, dt.datetime.now(dt.timezone.utc))
    assert matched['person']['email'] == 'work@example.com' and 'phone' not in matched['person']
    assert reserved == [30] and len(calls) == 4
    # A renamed upstream contract must not replay a completed reveal or turn an
    # uncertain old effect into a new request. Stable journals remain authoritative.
    ids['search'] = 'people/preview-v4'
    ids['match'] = 'people/reveal-v4'
    p.discover_apollo_contacts()
    count = len(calls)
    assert alexandria.observe(tmp_path, 'example.com', 'people/match', match_options,
        p.paid_match, dt.datetime.now(dt.timezone.utc)) == matched
    assert len(calls) == count and reserved == [30]
    for journal in tmp_path.glob('*.json'):
        receipt = json.loads(journal.read_text())
        if receipt.get('capability') == 'people/match':
            receipt['status'] = 'claimed'
            journal.write_text(json.dumps(receipt))
    with pytest.raises(RuntimeError, match='ambiguous_prospect_effect'):
        alexandria.observe(tmp_path, 'example.com', 'people/match', match_options,
            p.paid_match, dt.datetime.now(dt.timezone.utc))
    assert len(calls) == count and reserved == [30]


@pytest.mark.parametrize('case', ['missing', 'failed', 'wrong_provider', 'unknown_price', 'price_increased',
    'ceiling_increased', 'ambiguous', 'wrong_execute', 'option_missing', 'error'])
def test_apollo_discovery_fails_closed_without_contact_execution(case):
    search = {'provider': 'apollo', 'capability': 'current/preview', 'creditsCost': 0,
        'options': [{'name': k} for k in ('q_organization_domains_list', 'person_locations', 'page', 'per_page')],
        'execute': {'provider': 'apollo', 'capability': 'current/preview'}}
    match = {'provider': 'apollo', 'capability': 'current/resolve', 'creditsCost': 30,
        'billing': {'maxCredits': 30},
        'options': [{'name': k} for k in ('id', 'reveal_personal_emails', 'reveal_phone_number')],
        'execute': {'provider': 'apollo', 'capability': 'current/resolve'}}
    items = [search, match]
    if case == 'unknown_price': match.pop('creditsCost')
    if case == 'price_increased': match['creditsCost'] = 60
    if case == 'ceiling_increased': match['billing']['maxCredits'] = 60
    if case == 'ambiguous': items.append(dict(search))
    if case == 'wrong_execute': match['execute']['capability'] = 'old/guess'
    if case == 'option_missing': match['options'].pop()
    calls = []
    class Client:
        def search(self, payload):
            calls.append('discovery')
            return {'success': case != 'failed', 'data': {'tools': [] if case == 'missing' else [
                {'provider': 'other' if case == 'wrong_provider' else 'apollo', 'capability': i['capability']} for i in items]}}
        def scrape(self, payload):
            calls.append('inspection')
            assert payload['alexandria'][0]['provider'] == 'firecrawl'
            return {'success': True, 'data': {'alexandria': [{
                'provider': 'firecrawl', 'capability': 'find-tools',
                'error': {'status': 400} if case == 'error' else None, 'data': {'items': items}}]}}
    p = supply.Producer.__new__(supply.Producer)
    p.alex = Client()
    p.report = {}
    p.apollo_capabilities = {'people/match': 'old/guess'}
    with pytest.raises(RuntimeError, match='apollo_contact_'):
        p.discover_apollo_contacts()
    assert not p.apollo_capabilities
    assert len(calls) <= 2


def retained_fixture(tmp_path, monkeypatch):
    """Synthetic decoded receipt + genuine-shaped ledger, never live effects."""
    import hashlib
    import firecrawl_client
    import glp_sponsor_campaign as campaign
    import glp328_outreach_policy as policy
    at = dt.datetime.now(dt.timezone.utc)
    url = 'https://example.com/scope'
    html = '<p>We offer protein shakes. We ship to customers across the United States.</p>'
    sha = hashlib.sha256(html.encode()).hexdigest()
    receipt = {'host': 'example.com', 'requested_url': url, 'observed_at': at.isoformat(),
        'response': {'success': True, 'data': {'rawHtml': html, 'metadata': {
            'url': url, 'sourceURL': url, 'statusCode': 200, 'scrapeId': 'fixture-id'}}}}
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(receipt))
    row = {'host': 'example.com', 'source_url': url, 'requested_url': url,
        'receipt': 'receipt.json', 'receipt_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'sha256': sha, 'observed_utc': at.isoformat(), 'operation_id': 'fixture-op'}
    manifest = tmp_path / 'cohort.json'
    manifest.write_text(json.dumps({'schema': 'sponsor-retained-cohort-v1', 'pages': [row]}))
    ledger = tmp_path / 'operations.jsonl'
    base = {'caller': supply.CALLER, 'endpoint': '/v2/scrape', 'operation_id': 'fixture-op',
        'at_utc': at.isoformat(), 'url_hash': [hashlib.sha256(url.encode()).hexdigest()]}
    ledger.write_text(json.dumps(base | {'outcome': 'started'}) + '\n' + json.dumps(base | {
        'outcome': 'success', 'http_status': 200,
        'scrape_search_id': 'sha256:' + hashlib.sha256(b'fixture-id').hexdigest()}) + '\n')
    monkeypatch.setattr(firecrawl_client, 'LEDGER', ledger)
    monkeypatch.setattr(firecrawl_client, 'FirecrawlClient', lambda *a, **k: pytest.fail('network client created'))
    monkeypatch.setattr(campaign, 'STATE', tmp_path)
    monkeypatch.setattr(campaign, 'read_local', lambda: {'suppression': {'complete': True, 'entries': []}, 'conversations': [], 'faults': []})
    monkeypatch.setattr(campaign, 'read_values', lambda: [list(policy.EXPECTED_HEADERS)])
    monkeypatch.setattr(supply.Producer, 'capture_host', lambda *a, **k: pytest.fail('capture fallback'))
    monkeypatch.setattr(supply.Producer, 'reserve', lambda *a, **k: pytest.fail('paid reservation'))
    monkeypatch.setattr(campaign, 'mail_admit', lambda *a, **k: pytest.fail('mailbox admission'))
    monkeypatch.setattr(supply.subprocess, 'run', lambda *a, **k: pytest.fail('effectful subprocess'))
    p = supply.Producer(state_dir=tmp_path, import_only=True)
    p.discovery = tmp_path / 'discovery.json'
    p.discovery.write_text(json.dumps({'organizations': {'unrelated.example': {'professional_contacts': [{'id': 'keep'}]}}}))
    return p, manifest, receipt, row, ledger


def test_retained_cohort_merges_full_pages_without_transport_or_restamping(tmp_path, monkeypatch):
    import hashlib
    p, manifest, receipt, row, ledger = retained_fixture(tmp_path, monkeypatch)
    before = ledger.read_bytes()
    old_claim = tmp_path / 'old-enrichment-claim.json'
    old_claim.write_text('{"status":"claimed","operation_id":"old-unknown"}')
    reservation = tmp_path / 'firecrawl-supply-budget.json'
    reservation.write_text('{"day":"2026-10-13","reserved":960}')
    old_bytes, reserved_bytes = old_claim.read_bytes(), reservation.read_bytes()
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    p.merge(retained_cohort=manifest, cohort_sha256=digest)
    disc = json.loads(p.discovery.read_text())
    org = disc['organizations']['example.com']
    assert org['discovery']['candidates'] == []  # scope input is not a contact
    assert {r.get('sponsor_category') for r in org['scope_evidence']} >= {'nutrition_protein'}
    assert any(r.get('country') == 'US' for r in org['scope_evidence'])
    page = org['discovery']['_markup'][0]
    assert page['html'] == receipt['response']['data']['rawHtml']
    assert page['fetched_at_utc'] == row['observed_utc']
    assert page['provenance']['receipt_sha256'] == row['receipt_sha256']
    first = p.discovery.read_bytes()
    p.merge(retained_cohort=manifest, cohort_sha256=digest)
    assert p.discovery.read_bytes() == first and ledger.read_bytes() == before
    assert old_claim.read_bytes() == old_bytes and reservation.read_bytes() == reserved_bytes
    assert not p.pages.exists() and not p.cache.exists()
    assert disc['organizations']['unrelated.example']['professional_contacts'] == [{'id': 'keep'}]


@pytest.mark.parametrize('case,why', [
    ('requested_foreign', 'retained_source_mismatch'),
    ('final_foreign', 'retained_source_mismatch'),
    ('sourceURL_foreign', 'retained_source_mismatch'),
    ('sourceURL_disagreement', 'retained_source_mismatch'),
    ('subdomain', 'retained_source_mismatch'),
    ('path_traversal', 'retained_receipt_path'),
    ('expired', 'retained_evidence_expired'),
    ('future', 'retained_evidence_expired'),
    ('named_duplicate', 'retained_org_held_or_named'),
    ('held_alias', 'retained_org_held_or_named'),
    ('manifest_hash', 'retained_cohort_hash_mismatch'),
    ('receipt_hash', 'retained_receipt_hash_mismatch'),
    ('page_hash', 'retained_page_hash_mismatch'),
    ('missing_terminal', 'retained_effect_uncertain'),
    ('ambiguous_prior', 'retained_effect_uncertain'),
    ('wrong_operation', 'retained_effect_mismatch'),
    ('negative_product', 'retained_scope_refused'),
    ('research_only', 'retained_scope_refused'),
    ('unapproved_category', 'retained_scope_refused'),
    ('suppressed', 'retained_org_held_or_named'),
    ('contacted', 'retained_org_held_or_named'),
    ('incomplete_suppression', 'suppression_or_local_sources_incomplete'),
])
def test_retained_cohort_denials_are_atomic_and_never_replay(tmp_path, monkeypatch, case, why):
    import hashlib
    p, manifest, receipt, row, ledger = retained_fixture(tmp_path, monkeypatch)
    before = p.discovery.read_bytes()
    meta = receipt['response']['data']['metadata']
    if case == 'requested_foreign': row['requested_url'] = 'https://evil.example/scope'
    if case == 'final_foreign': meta['url'] = 'https://evil.example/scope'
    if case == 'sourceURL_foreign': meta['sourceURL'] = 'https://evil.example/scope'
    if case == 'sourceURL_disagreement': meta['sourceURL'] = 'https://example.com/different'
    if case == 'path_traversal': row['receipt'] = '../receipt.json'
    if case in ('expired', 'future'):
        row['observed_utc'] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=-31 if case == 'expired' else 1)).isoformat()
        receipt['observed_at'] = row['observed_utc']
    if case in ('named_duplicate', 'held_alias'):
        import glp328_outreach_policy as policy
        monkeypatch.setattr(p.c, 'read_values', lambda: [list(policy.EXPECTED_HEADERS), []])
        monkeypatch.setattr(policy, 'classify_row', lambda *a: {'program': 'Example' if case == 'named_duplicate' else 'Held provider',
            'host': '' if case == 'named_duplicate' else 'www.example.com', 'blocks_org': case == 'held_alias'})
    if case == 'subdomain': row['source_url'] = 'https://other.example.com/scope'
    if case == 'page_hash': row['sha256'] = '0' * 64
    if case == 'missing_terminal': ledger.write_text(ledger.read_text().splitlines()[0] + '\n')
    if case == 'ambiguous_prior':
        started = json.loads(ledger.read_text().splitlines()[0]) | {'operation_id': 'old-uncertain'}
        ledger.write_text(ledger.read_text() + json.dumps(started) + '\n')
    if case == 'wrong_operation': row['operation_id'] = 'not-the-observed-effect'
    if case in ('negative_product', 'research_only', 'unapproved_category'):
        html = {
            'negative_product': '<p>We do not sell protein shakes. We ship across the United States.</p>',
            'research_only': '<p>We offer protein shakes for research use only. We ship across the United States.</p>',
            'unapproved_category': '<p>We offer prescription-adjacent skin creams. We ship across the United States.</p>',
        }[case]
        receipt['response']['data']['rawHtml'] = html
        row['sha256'] = hashlib.sha256(html.encode()).hexdigest()
    if case in ('suppressed', 'contacted', 'incomplete_suppression'):
        monkeypatch.setattr(p.c, 'read_local', lambda: {
            'suppression': {'complete': case != 'incomplete_suppression',
                            'entries': [{'domain': 'example.com'}] if case == 'suppressed' else []},
            'conversations': [{'domain': 'example.com'}] if case == 'contacted' else [], 'faults': []})
    (tmp_path / 'receipt.json').write_text(json.dumps(receipt))
    row['receipt_sha256'] = hashlib.sha256((tmp_path / 'receipt.json').read_bytes()).hexdigest()
    if case == 'receipt_hash': row['receipt_sha256'] = '0' * 64
    manifest.write_text(json.dumps({'schema': 'sponsor-retained-cohort-v1', 'pages': [row]}))
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if case == 'manifest_hash': digest = '0' * 64
    ledger_before = ledger.read_bytes()
    with pytest.raises(RuntimeError, match=why):
        p.merge(retained_cohort=manifest, cohort_sha256=digest)
    assert p.discovery.read_bytes() == before
    assert ledger.read_bytes() == ledger_before
    assert not p.pages.exists() and not p.cache.exists()


def test_retained_cli_refuses_combined_paid_phases_before_client_creation(monkeypatch):
    import firecrawl_client
    monkeypatch.setattr(firecrawl_client, 'FirecrawlClient', lambda *a, **k: pytest.fail('network client created'))
    for args in (['daily', '--retained-cohort', 'x', '--cohort-sha256', '0' * 64],
                 ['merge', '--retained-cohort', 'x']):
        with pytest.raises(SystemExit) as exc:
            supply.main(args)
        assert exc.value.code == 2


def test_retained_legacy_capture_preserves_owned_pages_conflicts_and_professional_contacts(tmp_path, monkeypatch):
    import hashlib
    p, manifest, receipt, row, ledger = retained_fixture(tmp_path, monkeypatch)
    html = receipt['response']['data']['rawHtml']
    page = {'url': row['source_url'], 'final_url': row['source_url'], 'html': html,
        'sha256': row['sha256'], 'fetched_at_utc': row['observed_utc'], 'source': 'firecrawl_v2_scrape_rawHtml'}
    receipt = {'host': row['host'], 'capture': {'host': row['host'], 'pages': [page]}}
    (tmp_path / 'receipt.json').write_text(json.dumps(receipt))
    row['receipt_sha256'] = hashlib.sha256((tmp_path / 'receipt.json').read_bytes()).hexdigest()
    manifest.write_text(json.dumps({'schema': 'sponsor-retained-cohort-v1', 'pages': [row]}))
    scope, _ = supply.evidence(row['host'], row['source_url'], html, row['sha256'], row['observed_utc'],
        p.c.public_page_hits, p.c.BUSINESS_LOCALS, p.c.same, sponsor=True)
    conflict = page | {'html': '<p>Earlier conflicting evidence</p>',
        'sha256': hashlib.sha256(b'<p>Earlier conflicting evidence</p>').hexdigest()}
    p.discovery.write_text(json.dumps({'organizations': {row['host']: {
        'discovery': {'_markup': [page.copy(), conflict], 'candidates': []}, 'scope_evidence': scope,
        'professional_contacts': [{'person_id': 'old-professional', 'status': 'unknown'}]}}}))
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    p.merge(retained_cohort=manifest, cohort_sha256=digest)
    result = json.loads(p.discovery.read_bytes())['organizations'][row['host']]
    assert len(result['discovery']['_markup']) == 2
    assert result['discovery']['_markup'][0]['source'] == page['source']
    assert result['discovery']['_markup'][0]['retained_provenance'][0]['raw_http_provenance'] is False
    assert result['discovery']['_markup'][1] == conflict
    assert len(result['scope_evidence']) == len(scope)
    assert result['professional_contacts'] == [{'person_id': 'old-professional', 'status': 'unknown'}]
    first = p.discovery.read_bytes()
    p.merge(retained_cohort=manifest, cohort_sha256=digest)
    assert p.discovery.read_bytes() == first


def test_retained_cli_import_executes_supported_writer_only(tmp_path, monkeypatch):
    import hashlib
    p, manifest, receipt, row, ledger = retained_fixture(tmp_path, monkeypatch)
    def factory(**kwargs):
        assert kwargs['import_only'] is True
        return p
    monkeypatch.setattr(supply, 'Producer', factory)
    assert supply.main(['merge', '--retained-cohort', str(manifest), '--cohort-sha256',
                       hashlib.sha256(manifest.read_bytes()).hexdigest()]) == 0
    report = json.loads((tmp_path / 'firecrawl-supply-latest.json').read_text())
    assert report['import_only'] and report['paid_calls'] == report['sends'] == 0
    assert 'example.com' in json.loads(p.discovery.read_text())['organizations']


@pytest.mark.parametrize('mode', ['empty', 'reordered', 'reordered_held_row'])
def test_retained_registry_schema_refuses_before_hold_lookup(tmp_path, monkeypatch, mode):
    import hashlib
    import glp328_outreach_policy as policy
    p, manifest, receipt, row, ledger = retained_fixture(tmp_path, monkeypatch)
    headers = list(policy.EXPECTED_HEADERS)
    bad = [] if mode == 'empty' else [list(reversed(headers))]
    if mode == 'reordered_held_row':
        held = [''] * len(headers)
        held[policy.PROGRAM_COLUMN] = 'Example'
        held[policy.OFFICIAL_URL_COLUMN] = 'https://example.com'
        held[policy.STATUS_COLUMN] = 'HOLD'
        assert policy.classify_row(2, held)['blocks_org'] is True
        # A real column reorder moves the header and value together.
        i = policy.STATUS_COLUMN
        headers[i], headers[0] = headers[0], headers[i]
        held[i], held[0] = held[0], held[i]
        assert not policy.classify_row(2, held).get('blocks_org')
        bad = [headers, held]
    monkeypatch.setattr(p.c, 'read_values', lambda: bad)
    classified = []
    classify = policy.classify_row
    def lookup(*args):
        classified.append(args)
        return classify(*args)
    monkeypatch.setattr(policy, 'classify_row', lookup)
    before = p.discovery.read_bytes()
    with pytest.raises(RuntimeError, match='registry_schema_drift'):
        p.merge(retained_cohort=manifest, cohort_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
    assert not classified and p.discovery.read_bytes() == before
    assert not p.pages.exists() and not p.cache.exists()


@pytest.mark.parametrize('capture_kind', ['normal', 'retained'])
def test_later_identical_capture_refreshes_without_rewriting_retained_receipts(tmp_path, monkeypatch, capture_kind):
    import copy
    import hashlib
    p, manifest, receipt, row, ledger = retained_fixture(tmp_path, monkeypatch)
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    p.merge(retained_cohort=manifest, cohort_sha256=digest)
    initial = json.loads(p.discovery.read_text())['organizations']['example.com']
    original_page = copy.deepcopy(initial['discovery']['_markup'][0])
    original_scope = copy.deepcopy(initial['scope_evidence'])
    original_bytes = (tmp_path / 'receipt.json').read_bytes()
    original_manifest = manifest.read_bytes()
    later = dt.datetime.fromisoformat(row['observed_utc']) + dt.timedelta(days=31)
    monkeypatch.setattr(supply, 'now', lambda: later)
    args = {}
    if capture_kind == 'normal':
        # Already retained genuine-shaped capture, not a capture transport call.
        fresh = copy.deepcopy(original_page)
        fresh.pop('provenance')
        fresh['fetched_at_utc'] = later.isoformat()
        fresh['source'] = 'firecrawl_v2_scrape_rawHtml'
        p.pages.mkdir()
        p.c.save(p.pages / 'example.com.json', {'host': 'example.com', 'pages': [fresh]})
        p.import_only = False
    else:
        # New independent receipt/operation; never restamp the original receipt.
        fresh_receipt = copy.deepcopy(receipt)
        fresh_receipt['observed_at'] = later.isoformat()
        fresh_receipt['response']['data']['metadata']['scrapeId'] = 'later-fixture-id'
        fresh_path = tmp_path / 'later-receipt.json'
        fresh_path.write_text(json.dumps(fresh_receipt))
        fresh_row = row | {'observed_utc': later.isoformat(), 'receipt': fresh_path.name,
            'receipt_sha256': hashlib.sha256(fresh_path.read_bytes()).hexdigest(), 'operation_id': 'later-fixture-op'}
        ops = [json.loads(line) | {'at_utc': later.isoformat(), 'operation_id': 'later-fixture-op'}
               for line in ledger.read_text().splitlines()]
        ops[-1]['scrape_search_id'] = 'sha256:' + hashlib.sha256(b'later-fixture-id').hexdigest()
        ledger.write_text(ledger.read_text() + ''.join(json.dumps(op) + '\n' for op in ops))
        fresh_manifest = tmp_path / 'later-cohort.json'
        fresh_manifest.write_text(json.dumps({'schema': 'sponsor-retained-cohort-v1', 'pages': [fresh_row]}))
        args = {'retained_cohort': fresh_manifest, 'cohort_sha256': hashlib.sha256(fresh_manifest.read_bytes()).hexdigest()}
    ledger_before = ledger.read_bytes()
    p.merge(**args)
    org = json.loads(p.discovery.read_text())['organizations']['example.com']
    qualified = p.c.sponsor_scope_current('example.com', org, p.c, later)
    assert any(r.get('sponsor_category') for r in qualified) and any(r.get('country') == 'US' for r in qualified)
    assert all(r['observed_utc'] == later.isoformat() for r in qualified)
    assert org['discovery']['_markup'][0] == original_page
    assert org['scope_evidence'][:len(original_scope)] == original_scope
    assert len(org['discovery']['_markup']) == 2
    current_page = org['discovery']['_markup'][-1]
    assert current_page['fetched_at_utc'] == later.isoformat()
    assert current_page['sha256'] == original_page['sha256'] and current_page['html'] == original_page['html']
    if capture_kind == 'retained':
        assert current_page['provenance']['operation_id'] == 'later-fixture-op'
        assert current_page['provenance']['receipt_path'] == str(fresh_path)
    else:
        assert 'provenance' not in current_page and current_page['source'] == 'firecrawl_v2_scrape_rawHtml'
    first = p.discovery.read_bytes()
    p.merge(**args)
    assert p.discovery.read_bytes() == first
    assert (tmp_path / 'receipt.json').read_bytes() == original_bytes and manifest.read_bytes() == original_manifest
    assert ledger.read_bytes() == ledger_before
