#!/usr/bin/env python3
"""Firecrawl + Alexandria supply producer for the installed sponsor campaign.

Adds only *first-party, gate-passing* evidence to the campaign's existing
contact-discovery file, so the installed ``glp_sponsor_campaign.recipients()``
and ``mail_admit()`` decide admission. It never sends, never enables a send,
never edits sender/transport code, and never guesses or pattern-builds an email:
contacts are either published business-purpose addresses retained in scraped
first-party HTML, or Apollo (Alexandria) professional records whose email
domain equals the organization host. Verification uses only the shared Bouncer guard.

Phases (merge/admission under the existing campaign run.lock, waiting politely):
  capture  map + scrape relevant sponsor research hosts (rawHtml, US, maxAge 0)
  merge    re-derive evidence from retained pages and merge into discovery
  apollo   Alexandria apollo people/search (0 credits) + people/match (30)
  verify   installed mail_admit + shared Bouncer guard for unverified recipients
  daily    capture -> merge -> apollo -> merge -> verify, inside the credit caps

Budget: ledger-bounded daily Firecrawl cap (1500 credits), Apollo match cap (25/run),
and a hard stop when the team balance is at or below 3000 credits.
Every Firecrawl call is ledgered by firecrawl_client with caller names
``sponsor_firecrawl_supply`` / ``sponsor_firecrawl_alexandria``.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path('/home/ubuntu/business')
sys.path.extend(p for p in (str(ROOT / 'scripts'), str(ROOT / 'ops'), str(ROOT)) if p not in sys.path)

CALLER = 'sponsor_firecrawl_supply'
ALEX_CALLER = 'sponsor_firecrawl_alexandria'
FLOOR_CREDITS = 3000
RUN_CREDIT_CAP = 1500
PERIOD_CREDIT_CAP = 45000
# OPERATOR-DECISION-20261014-firecrawl-cap: prepaid period only, no purchase.
# Default automatically remains 45k for every other authenticated period.
PERIOD_CAP_END = dt.datetime(2026, 10, 19, 23, 59, 59, tzinfo=dt.timezone.utc)
APPROVED_PERIOD_CREDIT_CAP = 60000
SPONSOR_CALLERS = (CALLER, ALEX_CALLER, 'fc71_sponsor_supply', 'fc71_sponsor_alexandria', 'sponsor_campaign')
MATCH_CAP = 25
GAPS = {'us_program_scope_unknown', 'not_glp_medication_provider', 'no_verified_business_purpose_contact',
        'contact_purpose_unverified', 'contact_evidence_incomplete', 'public_source_or_role_evidence_unknown'}
URL_KEYS = re.compile(r'glp|semaglutide|tirzepatide|weight|how-it-works|faq|about|terms|partner|affiliat|contact|pricing|press|media|advertis', re.I)
PARTNER_CTX = re.compile(r'partnership|affiliat|business development|marketing|sponsor', re.I)
BAD_CTX = re.compile(r'patient|billing|customer support|affiliate support|creator recruitment|personal journey|infrastructure', re.I)
# Medication-provider sentence: identical vocabulary to the installed
# scope_evidence_current() so a merged excerpt is re-accepted, never widened.
NEG = re.compile(r"\b(?:no|not|never|without|cannot|can't|don't|doesn't|isn't|aren't|unable|marketing|advertising|coaching|education|demonstrations?)\b", re.I)
WE = re.compile(r'\b(?:we|our (?:doctors|clinicians|providers|prescribers))\s+(?:prescribe|offer|provide)\b', re.I)
RX = re.compile(r'\b(?:prescribe|prescription|medication|treatment)\b', re.I)
MEDICATION = re.compile(r'glp[- ]?1|semaglutide|tirzepatide|wegovy|zepbound|mounjaro|ozempic', re.I)
US = re.compile(r'United States|U\.S\.|USA|all 50 states|\bUS\b')
US_CTX = re.compile(r'customers|residents|areaServed|PostalAddress|available|across|pricing|states|doctors|WW USA', re.I)
US_SCOPE = re.compile(r'(?:available|licensed|serv(?:e|es|ing)|operat\w*|residents|customers|patients|ship\w*|deliver\w*)\b.{0,80}(?:United States|U\.S\.|USA|\bUS\b|50 states)'
                      r'|(?:across|throughout|in all)\s+(?:the\s+)?(?:United States|U\.S\.|US|USA|50 states)|based in the United States', re.I)
US_REJECT = re.compile(r'copyright|©|usa-today|1 out of 5|Pharmacopeia|not available in (?:all|every)|\bnot (?:yet )?(?:licensed|offered)', re.I)
PEOPLE_TITLES = re.compile(r'partnership|marketing|growth|business development', re.I)
# Discovery seeds are research targets, never qualification or registry additions.
SPONSOR_SEEDS = '''ozarihealth.com urgentcare.com helloklarity.com sesamecare.com
plushcare.com joinmochi.com edenhealth.com futurhealth.com getthinusa.com
empowerpharmacy.com hallandalerx.com strivepharmacy.com olympiapharmacy.com
belmarpharmasolutions.com redrockpharmacy.com revive-rx.com anazaohealth.com
wellsrx.com southendpharmacy.com mediverarx.com precisionrx.com
fullscript.com gethuel.com huel.com drinkhum.com drinkwholesome.com
fairlife.com premierprotein.com owyn.com drinkkoia.com ritual.com
orgain.com gainful.com transparentlabs.com legionathletics.com
promixnutrition.com kachava.com vitalproteins.com momentous.com
liveowyn.com drinkiconic.com cleanprotein.com equipfoods.com
needed.com dailyharvest.com hungryroot.com factor75.com cookunity.com
bistromd.com trifectanutrition.com thistle.co modifyhealth.com
territoryfoods.com snapkitchen.com mealpro.net freshly.com
noom.com myfitnesspal.com loseit.com lifesum.com fitbit.com
levels.com nutrisense.io signos.com dexcom.com freestyle.abbott
lumen.me zoe.com simple.life found.com welldoc.com
future.co trainwell.net caliberstrong.com centr.com tonal.com
onepeloton.com obefitness.com alo.moves beachbody.com
'''.split()
RESEARCH_ONLY = re.compile(r'research[- ]use[- ]only|for research (?:use|purposes) only|not for human (?:use|consumption)', re.I)
CATEGORY = {
    'telehealth_glp1': re.compile(r'(?:GLP[- ]?1|semaglutide|tirzepatide|Wegovy|Zepbound).{0,100}(?:treatment|prescription|telehealth)|(?:prescri\w+|telehealth|treatment).{0,100}(?:GLP[- ]?1|semaglutide|tirzepatide|Wegovy|Zepbound)', re.I),
    'pharmacy': re.compile(r'(?:compounding|mail[- ]order|specialty) pharmacy|pharmacy.{0,50}(?:compounding|prescription delivery)', re.I),
    'nutrition_protein': re.compile(r'protein (?:shakes?|powder|nutrition)|nutrition shakes?|meal replacement', re.I),
    'meals': re.compile(r'meal delivery|prepared meals|healthy meals|meal plans', re.I),
    'fitness': re.compile(r'fitness (?:app|training|program)|personal training|strength training|workout (?:app|program)', re.I),
    'cgm': re.compile(r'continuous glucose monitor|metabolic health|glucose monitoring', re.I),
    'weight_coaching': re.compile(r'weight[- ]loss (?:app|coaching|program)|weight management (?:app|coaching|program)', re.I),
}
COMMERCIAL = re.compile(r'\b(?:our|we|shop|buy|order|subscribe|membership|plans|products|program|patients|customers|offers?|provid\w+|prescri\w+|get started)\b', re.I)
US_ADDRESS = re.compile(r'\b(?:CA|NY|TX|FL|MA|CO|WA|IL|AZ|NC|GA|PA|NJ|UT|TN|OH|OR|VA|NV|MD|CT|MN|MI|WI|MO|IN|KY|SC|AL|LA|OK|KS|NM|ID|IA|NE|AR|MS|NH|ME|RI|DE|VT|WV|MT|WY|ND|SD|HI|AK|DC)\s+\d{5}(?:-\d{4})?\b')
LEGAL_NEGATOR = re.compile(
    r'\b(?:Do Not Sell(?: or Share)? My Personal Information|Do Not Sell["”] box to remove your consent|'
    r'no limit to the number of successful referrals|cannot be claimed again for subsequent referrals|'
    r'If it is not claimed within this period, the offer will expire|'
    r'not dependent on the referring client making any further successful referrals|'
    r'Timeframes for results are not guaranteed)\b', re.I)


def sponsor_fit(text):
    """Commercial first-party excerpt, not a seed/domain or generic blog keyword."""
    # Refuse ambiguous/negative context before clipping: a long product list
    # must not lose its governing negator. Deliberately prefer false negatives
    # (including double negatives and "not only") to unsupported admission.
    # Narrow literal legal contexts only: do not drop sentences or clip away
    # governing product negation. Quotes/hashes/category matching use original text.
    if RESEARCH_ONLY.search(text) or re.search(r"\b(?:no|not|never|neither|nor|cannot|no longer|\w+n['’]t)\b", LEGAL_NEGATOR.sub('', text), re.I):
        return None
    for category, pattern in CATEGORY.items():
        for match in pattern.finditer(text):
            start, end = max(0, match.start()-120), match.end()+180
            # The receiver rechecks this original quote. Keep recognized literal
            # legal clauses whole rather than leaving unexplained negators.
            for clause in LEGAL_NEGATOR.finditer(text):
                if clause.start() < start < clause.end():
                    start = clause.start()
                if clause.start() < end < clause.end():
                    end = clause.end()
            excerpt = text[start:end].strip()
            if COMMERCIAL.search(excerpt):
                return category, excerpt
    return None


def firstparty_research_host(row):
    """Affirmative own-category offer, not query-keyword or search-host authority.

    Reject publishing/directory/profile content forms rather than a hostname
    blacklist. This is a research filter; all US/product/send gates still apply.
    """
    from urllib.parse import urlsplit
    parts = urlsplit(str(row.get('url') or ''))
    host = (parts.hostname or '').lower().removeprefix('www.')
    title, description = str(row.get('title') or ''), str(row.get('description') or '')
    text = title + ' ' + description
    if not (any(pattern.search(text) for pattern in CATEGORY.values()) or re.search(r'\bmeal prep\b', text, re.I)):
        return None
    if RESEARCH_ONLY.search(text):
        return None
    if re.search(r'\b(?:news|journal|newsletter|directory|directories|market (?:report|research|size)|research report|industry analysis|cross-sectional|study|studies|reviews?|comparisons?)\b', title, re.I):
        return None
    # Ranked guides and outbound shopping references describe someone else's
    # offer even when the publisher's own brand occurs in the title.
    if (re.search(r'\b(?:best|top)\b.*\b(?:services?|kits?|brands?|powders?|supplements?|plans?)\b', title, re.I)
            or re.search(r'\bshop at\b', description, re.I)):
        return None
    if re.search(r'/(?:company|companies|posts|groups|forum|articles|reports|p|watch)/', parts.path, re.I):
        return None
    normalize = lambda s: re.sub(r'[^a-z0-9]', '', s.lower()).replace('and', '')
    brand = normalize(host.split('.')[-2]) if '.' in host else ''
    if len(brand) < 5 or brand not in normalize(text):
        return None
    category = lambda s: any(pattern.search(s) for pattern in CATEGORY.values()) or re.search(r'\bmeal prep\b', s, re.I)
    offer = re.compile(r'\b(?:services?|manufacturers?|suppliers?|bulk|wholesale|online|available|treatments?|telehealth|delivery|delivered|contract manufacturing)\b', re.I)
    # Negative product statements are not offer evidence. Refuse the governing
    # sentence, without treating unrelated "no minimum order" terms as negation.
    sentences = re.split(r'[.!?\n]+', text)
    if any(category(s) and re.search(r"\b(?:not|never|neither|nor|cannot|\w+n['’]t)\b|\bno\s+(?:protein|prepared meals?|meal (?:prep|delivery)|training|telehealth)\b", LEGAL_NEGATOR.sub('', s), re.I) for s in sentences):
        return None
    # Category wording is not independent evidence of a merchant: e.g. a
    # publisher's "Classifying Meal Delivery Companies" only names the industry.
    # Require an additional offer/transaction signal beyond the category span.
    title_offer = title
    for pattern in CATEGORY.values():
        title_offer = pattern.sub('', title_offer)
    title_offer = re.sub(r'\bmeal prep\b', '', title_offer, flags=re.I)
    branded_offer = (brand in normalize(title) and category(title)
                     and (offer.search(title_offer) or re.search(
                         r'\b(?:shop|buy|order|subscribe|membership|products|get started)\b', title_offer, re.I)))
    own_offer = any(category(s) and re.search(r'\b(?:our|we)\b', s, re.I)
                    and (COMMERCIAL.search(s) or offer.search(s)) for s in sentences)
    own_commercial = any(re.search(r'\b(?:our|we)\b', s, re.I)
                         and (COMMERCIAL.search(s) or offer.search(s)) for s in sentences)
    if not (branded_offer or own_offer or (brand in normalize(title) and category(title) and own_commercial)):
        return None
    return host


def bound_lead_rows(path):
    """Bind admitted JSONL rows to their retained genuine search receipts."""
    path = Path(path)
    records, receipts = [], {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            name = row.get('receipt')
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError('receipt missing or unsafe')
            receipt = path.parent / name
            if name not in receipts:
                data = json.loads(receipt.read_bytes())
                hits = (data.get('data') or {}).get('web')
                if data.get('success') is False or not isinstance(hits, list):
                    raise ValueError('receipt has no successful web results')
                receipts[name] = (hits, hashlib.sha256(receipt.read_bytes()).hexdigest())
            hits, receipt_sha = receipts[name]
            if not any(all(hit.get(k) == row.get(k) for k in ('url','title','description')) for hit in hits):
                raise ValueError('lead not in receipt')
        except (OSError, ValueError, AttributeError, TypeError):
            raise RuntimeError(f'retained_lead_receipt_unbound:{path}:{number}') from None
        records.append((row, receipt_sha))
    return records


def research_only_organization(host, org, campaign):
    for page in org.get('discovery', {}).get('_markup', []):
        markup = page.get('html', '')
        if campaign.source_on_provider(page.get('final_url', page.get('url')), host) and hashlib.sha256(markup.encode('utf-8', 'replace')).hexdigest() == page.get('sha256'):
            if RESEARCH_ONLY.search(campaign.public_page_hits(markup)[1]):
                return True
    return False


def sponsor_scope_current(host, organization, campaign, at):
    import glp_sponsor_readiness as readiness
    qualified = []
    for ev in organization.get('scope_evidence', []):
        if not campaign.source_on_provider(ev.get('source_url'), host) or not readiness.current_evidence(
                {'captured_at_utc': ev.get('observed_utc'), 'evidence_sha256': ev.get('sha256')}, at):
            continue
        for page in organization.get('discovery', {}).get('_markup', []):
            markup = page.get('html', '')
            if page.get('final_url', page.get('url')) != ev['source_url'] or hashlib.sha256(markup.encode('utf-8', 'replace')).hexdigest() != ev['sha256']:
                continue
            _, text = campaign.public_page_hits(markup)
            if RESEARCH_ONLY.search(text):
                return []
            norm = lambda s: ' '.join(str(s).split()).casefold()
            excerpt = ev.get('sponsor_category_excerpt') or ev.get('country_excerpt')
            if not excerpt or norm(excerpt) not in norm(text):
                continue
            # Recheck the complete bound page, not merely a proposal substring
            # which may have dropped a negator before its category keyword.
            if ev.get('sponsor_category') in CATEGORY and sponsor_fit(text) and sponsor_fit(excerpt) and CATEGORY[ev['sponsor_category']].search(excerpt):
                qualified.append(ev)
            elif ev.get('country') == 'US' and not US_REJECT.search(excerpt) and (US_SCOPE.search(excerpt) or US_ADDRESS.search(excerpt)):
                qualified.append(ev)
    return qualified


def sponsor_rows(discovery, registry_rows, local, campaign, at):
    """Supplement the existing recipient census; never write a provider registry row."""
    from glp328_outreach_policy import _same_adverse_org
    held = {r['host'] for r in registry_rows if r.get('blocks_org') and r.get('host')}
    held_names = {campaign.normalize(r.get('program', '')) for r in registry_rows if r.get('blocks_org')}
    blocked = held | {x['domain'] for x in local['suppression']['entries']} | {x['domain'] for x in local['conversations']}
    out = {}
    if not local['suppression']['complete']:
        return out
    for host, org in sorted(discovery.get('organizations', {}).items()):
        name = str(org.get('provider') or host)
        if ('fella' in (host + name).lower() or campaign.normalize(name) in held_names
                or host.endswith(campaign.PLATFORMS) or any(_same_adverse_org(host, h) for h in blocked)
                or research_only_organization(host, org, campaign)):
            continue
        scope = sponsor_scope_current(host, org, campaign, at)
        if any(x.get('sponsor_category') for x in scope) and any(x.get('country') == 'US' for x in scope):
            out[host] = [{'program': name, 'host': host, 'row': None, 'population': 'pending', 'eligible': True}]
    return out



def now():
    return dt.datetime.now(dt.timezone.utc)


def sentences(text):
    """Affirmative sentences only; never split at "U.S." or "Dr."; drop questions."""
    parts = re.split(r'(?<!U\.S)(?<!\bDr)(?<=[.!?])\s+(?=[A-Z])', text)
    return [s.strip() for s in parts if 20 <= len(s.strip()) <= 400 and not s.strip().endswith('?')]


def evidence(host, url, markup, sha, at, page_hits, business_locals, same_org, sponsor=False):
    """Return (scope_rows, published_contacts) for one retained first-party page."""
    hits, text = page_hits(markup)
    med = next((s for s in sentences(text) if MEDICATION.search(s) and WE.search(s) and RX.search(s) and not NEG.search(s)), '')
    us = next((s for s in sentences(text) if US.search(s) and US_CTX.search(s) and US_SCOPE.search(s) and not US_REJECT.search(s)), '')
    rows = []
    if med:
        rows.append({'source_url': url, 'country': None, 'country_excerpt': '', 'medication_provider_excerpt': med, 'sha256': sha, 'observed_utc': at})
    if us:
        rows.append({'source_url': url, 'country': 'US', 'country_excerpt': us, 'medication_provider_excerpt': '', 'sha256': sha, 'observed_utc': at})
    fit = sponsor_fit(text) if sponsor and not re.search(r'/(?:blog|posts?|news|articles?|guides?|clinical-trials)(?:/|$)', url, re.I) else None
    if fit:
        rows.append({'source_url': url, 'sponsor_category': fit[0], 'sponsor_category_excerpt': fit[1],
                     'sha256': sha, 'observed_utc': at})
        if not us:
            address = US_ADDRESS.search(text)
            if address:
                excerpt = text[max(0, address.start()-100):address.end()+30]
                rows.append({'source_url': url, 'country': 'US', 'country_excerpt': excerpt,
                             'sha256': sha, 'observed_utc': at})
    contacts = []
    for hit in hits:
        address = hit['address']
        local, _, domain = address.partition('@')
        if address.count('@') != 1 or not business_locals.fullmatch(local) or not same_org(domain, host):
            continue
        if not PARTNER_CTX.search(hit['excerpt']) or BAD_CTX.search(hit['excerpt']):
            continue
        contacts.append({'address': address, 'how': hit['how'], 'tier': 'partnerships', 'found_on': url, 'page_sha256': sha, 'captured_at_utc': at})
    return rows, contacts


def eligible_hosts(exclusions, suppressed, same_adverse_org):
    """Registry hosts whose only exclusions are evidence gaps; Fella/suppressed never."""
    reasons = {}
    for row in exclusions:
        if row.get('host'):
            reasons.setdefault(row['host'], set()).add(row.get('reason'))
    out = []
    for host, why in sorted(reasons.items()):
        if 'fella' in host or not why <= GAPS or host.endswith(('.ie', '.uk', '.au', '.ca')) or 'everflowclient' in host:
            continue
        if any(same_adverse_org(host, s) for s in suppressed):
            continue
        out.append(host)
    return out


def apollo_contact(host, person, wanted_id, business_title, at, response):
    """Admit an Apollo record only when id, title, US country, org and email domain all bind to host."""
    email = str(person.get('email') or '').lower()
    if (person.get('id') != wanted_id or not business_title(person.get('title')) or person.get('country') != 'United States'
            or (person.get('organization') or {}).get('primary_domain') != host
            or not re.fullmatch(r'[a-z0-9._%+-]+@' + re.escape(host), email)):
        return None
    return {'source': 'apollo', 'person_id': person['id'], 'organization_domain': host, 'country': 'US', 'title': person['title'],
            'email': email, 'captured_at_utc': at.isoformat(),
            'evidence_sha256': hashlib.sha256(json.dumps(response, sort_keys=True).encode()).hexdigest(),
            'verification_status': 'unknown', 'send_eligible': False}


class Budget:
    """Per-run credit cap plus a hard floor on the team balance."""

    def __init__(self, remaining, cap=RUN_CREDIT_CAP, floor=FLOOR_CREDITS):
        self.remaining, self.cap, self.floor, self.spent = remaining, cap, floor, 0

    def allow(self, estimate):
        return self.spent + estimate <= self.cap and self.remaining - self.spent - estimate > self.floor

    def charge(self, credits):
        self.spent += int(credits or 0)


class Producer:
    def __init__(self, state_dir=None, run_cap=RUN_CREDIT_CAP, match_cap=MATCH_CAP, *, import_only=False, budget_lock=None):
        import glp_sponsor_campaign as campaign
        from firecrawl_client import FirecrawlClient
        self.c = campaign
        self.state = Path(state_dir or campaign.STATE)
        self.pages = self.state / 'firecrawl-supply-pages'
        self.cache = self.state / 'firecrawl-supply-alexandria'
        self.discovery = campaign.ROOT / 'reports/ops/sponsor-machine-20261003/contact-discovery.private.json'
        self.run = 'sponsor-supply-' + now().strftime('%Y%m%dT%H%M')
        self.import_only = import_only
        self._budget_lock = budget_lock
        if import_only:
            self.report = {'run': self.run, 'started_utc': now().isoformat(), 'sends': 0,
                           'import_only': True, 'paid_calls': 0}
            return  # No client, credit probe, reservation, enrichment or verification.
        self.client = FirecrawlClient(CALLER, run_id=self.run)
        self.alex = FirecrawlClient(ALEX_CALLER, run_id=self.run)
        usage = self.client.request('team/credit-usage', method='GET').get('data') or {}
        from glp_firecrawl_jobs import spent_today
        # Caller renames do not reset the same daily sponsor job allowance.
        self.start_spent = spent_today(SPONSOR_CALLERS)
        self.budget_day = now().date().isoformat()
        self.reservations = self.state / 'firecrawl-supply-budget.json'
        if self.reservations.exists():
            prior = self.c.load(self.reservations)
            if prior['day'] == self.budget_day:
                self.start_spent = max(self.start_spent, int(prior['reserved']))
        self.budget = Budget(int(usage.get('remainingCredits') or 0),
                             cap=max(0, min(run_cap, RUN_CREDIT_CAP) - self.start_spent))
        self.match_cap = match_cap
        self.report = {'run': self.run, 'started_utc': now().isoformat(), 'remaining_at_start': self.budget.remaining,
                       'daily_spent_at_start': self.start_spent, 'daily_cap': RUN_CREDIT_CAP, 'sends': 0}

    @contextlib.contextmanager
    def budget_locked(self):
        # Reuse real entrypoint custody; library callers contend on the SAME flock.
        if getattr(self, '_budget_lock', None) is not None:
            if Path(self._budget_lock.name).resolve() != (self.state / 'firecrawl-supply-budget.lock').resolve():
                raise RuntimeError('sponsor_budget_credit_fence_wrong_path')
            fcntl.flock(self._budget_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
            return
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state / 'firecrawl-supply-budget.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def operations(self):
        from firecrawl_client import LEDGER
        if not LEDGER.exists():
            return []
        with LEDGER.open() as ledger:
            fcntl.flock(ledger, fcntl.LOCK_SH)
            return [json.loads(line) for line in ledger if line.strip()]

    def daily_accounted(self, day):
        from glp_firecrawl_jobs import spent_today
        # The existing shared caller reader stays authoritative; additionally
        # bound pending/null-cost operations conservatively, never call them zero.
        latest = {}
        for row in self.operations():
            if row.get('caller') in SPONSOR_CALLERS and str(row.get('at_utc', '')).startswith(day):
                latest[row['operation_id']] = row
        total, unknown = 0, []
        for ident, row in latest.items():
            if row.get('endpoint') == '/v2/team/credit-usage':
                continue  # read-only account query, not a paid operation
            cost = row.get('creditsUsed')
            if type(cost) in (int, float) and cost >= 0:
                total += cost
                continue
            # Map is bounded at one; validated same-run single reveals retain
            # the existing 30-credit cap. Historical URL scrape ceilings need
            # complete request proof that sanitized ledger options do not retain.
            if row.get('endpoint') == '/v2/map':
                ceiling = 1
            elif row.get('endpoint') == '/v2/scrape' and row.get('url_hash'):
                # Sanitized options omit parsers/modifiers: formats/proxy alone
                # cannot prove the complete historical request ceiling. Preserve
                # UNKNOWN and durable pending; refuse, never invent 1/5 or replay.
                raise RuntimeError('sponsor_ledger_credit_cost_unknown')
            elif row.get('caller') in (ALEX_CALLER, 'fc71_sponsor_alexandria'):
                ceiling = 30
            else:
                raise RuntimeError('sponsor_ledger_credit_cost_unknown')
            total += ceiling
            unknown.append({'operation_id':ident, 'creditsUsed':None, 'accounted_ceiling':ceiling})
        self.report['unknown_credit_costs'] = unknown
        return max(total, spent_today(SPONSOR_CALLERS))

    def pending_reservations(self):
        prior = self.c.load(self.reservations) if self.reservations.exists() else {}
        pending = prior.get('pending_by_day')
        if pending is None:
            # Legacy cumulative reservations cannot be proved settled. Preserve
            # them conservatively, including across a UTC-day boundary.
            pending = {prior['day']:prior['reserved']} if prior else {}
        if not isinstance(pending, dict) or any(type(v) not in (int, float) or v < 0 for v in pending.values()):
            raise RuntimeError('sponsor_reservation_credit_unknown')
        return dict(pending)

    def reserve(self, credits):
        # Fresh boundary, not constructor Budget/start_spent. Persist before the
        # transport; an uncertain effect never refunds a durable reservation.
        with self.budget_locked():
            usage = self.client.request('team/credit-usage', method='GET').get('data') or {}
            try:
                remaining, plan = usage['remainingCredits'], usage['planCredits']
                start = dt.datetime.fromisoformat(usage['billingPeriodStart'])
                end = dt.datetime.fromisoformat(usage['billingPeriodEnd'])
                at = now()
                if (type(remaining) is not int or type(plan) is not int or not 0 <= remaining <= plan
                        or start.tzinfo is None or end.tzinfo is None or not start <= at < end):
                    raise ValueError('invalid credit period/balance')
            except (KeyError, TypeError, ValueError):
                raise RuntimeError('sponsor_account_credit_usage_unknown') from None
            day = at.date().isoformat()
            pending = self.pending_reservations()
            accounted = self.daily_accounted(day)
            if (day != self.budget_day or type(credits) is not int or credits <= 0
                    or self.budget.spent + credits > self.budget.cap
                    or accounted + pending.get(day, 0) + credits > RUN_CREDIT_CAP):
                raise RuntimeError('sponsor_daily_credit_cap')
            period_cap = APPROVED_PERIOD_CREDIT_CAP if end == PERIOD_CAP_END else PERIOD_CREDIT_CAP
            if plan - remaining + sum(pending.values()) + credits > period_cap:
                raise RuntimeError('sponsor_period_credit_cap')
            if remaining - sum(pending.values()) - credits <= FLOOR_CREDITS:
                raise RuntimeError('sponsor_account_credit_floor')
            pending[day] = pending.get(day, 0) + credits
            self.c.save(self.reservations, {'day':day, 'reserved':accounted + pending[day], 'pending_by_day':pending})
            self.budget.charge(credits)
            self.report['last_paid_admission'] = {'at':at.isoformat(), 'remaining':remaining,
                'period_used':plan-remaining, 'daily_accounted':accounted, 'reserved':pending[day], 'credits':credits}

    def paid(self, credits, transport, payload, **kwargs):
        self.reserve(credits)
        response = transport(payload, **kwargs)  # no automatic retry
        from firecrawl_client import _receipt
        _, cost = _receipt(response)
        # Only explicit, successful observed cost settles our pending estimate.
        # Null cost remains UNKNOWN and its durable reservation is retained.
        if response.get('success') is True and type(cost) in (int, float) and 0 <= cost <= credits:
            with self.budget_locked():
                pending = self.pending_reservations()
                pending[self.budget_day] -= credits
                accounted = self.daily_accounted(self.budget_day)
                self.c.save(self.reservations, {'day':self.budget_day,
                    'reserved':accounted + pending[self.budget_day], 'pending_by_day':pending})
        return response

    def paid_match(self, payload):
        return self.paid(30, self.apollo_scrape, payload)

    def capture_admissible(self, endpoint, url):
        digest = hashlib.sha256(url.encode()).hexdigest()
        latest = {}
        for row in self.operations():
            if row.get('caller') in SPONSOR_CALLERS and row.get('endpoint') == '/v2/' + endpoint and digest in row.get('url_hash', []):
                latest[row['operation_id']] = row
        if any(row.get('outcome') != 'success' for row in latest.values()):
            raise RuntimeError('ambiguous_capture_effect_no_replay')

    # -- locking -----------------------------------------------------------
    def locked(self, seconds=420):
        lock = (self.state / 'run.lock').open('a')
        end = time.monotonic() + seconds
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock
            except BlockingIOError:
                if time.monotonic() > end:
                    raise
                time.sleep(5)

    def plan(self):
        live = self.c.read_local()
        if not live['suppression']['complete'] or live['faults']:
            raise RuntimeError('suppression_or_local_sources_incomplete')
        return live, self.c.recipients(self.c.read_values(), self.c.load(self.discovery), live)

    # -- capture -----------------------------------------------------------
    def retained_contact_targets(self, res):
        """Inert continuation from current first-party navigation, not contacts.

        A host cache is not proof that every literal partnership page was
        visited. Reuse its scope evidence, but never replay any attempted URL,
        remap the host, guess a path, or re-enrich an existing contact.
        """
        from html.parser import HTMLParser
        from urllib.parse import urljoin, urlsplit
        import glp_sponsor_readiness as readiness

        def literal_url(value, absolute=False):
            # Reject parser-repaired syntax, never turn it into an authorized
            # destination. In particular WHATWG treats backslashes as slashes
            # in HTTP(S) authorities while urllib treats them as host bytes.
            if not isinstance(value, str) or re.search(r'[\\\x00-\x20\x7f-\x9f]', value):
                return False
            try:
                u = urlsplit(value)
            except ValueError:
                return False
            if absolute or u.scheme:
                if not re.match(r'https?://[^/]', value, re.I) or not u.netloc:
                    return False
            if value.startswith('//') and (value.startswith('///') or not u.netloc):
                return False
            # Literal ASCII DNS authority only: no userinfo, escapes, Unicode
            # host mappings, empty/nonliteral ports or missing authority.
            return not u.netloc or bool(re.fullmatch(
                r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?'
                r'(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::(?:80|443))?', u.netloc, re.I | re.A))

        class Links(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.hrefs = []
                self.hidden = 0
                self.base_href = None

            def handle_starttag(self, tag, attrs):
                if tag in ('script', 'style', 'template', 'noscript'):
                    self.hidden += 1
                if tag == 'a' and not self.hidden:
                    self.hrefs.extend(v for k, v in attrs if k == 'href' and v)
                if tag == 'base' and not self.hidden and self.base_href is None:
                    self.base_href = dict(attrs).get('href')

            def handle_endtag(self, tag):
                if tag in ('script', 'style', 'template', 'noscript'):
                    self.hidden = max(0, self.hidden - 1)

        host = res['host']
        if not hasattr(self, '_retained_contact_orgs'):
            self._retained_contact_orgs = self.c.load(self.discovery).get('organizations', {})
        org = self._retained_contact_orgs.get(host, {})
        if org.get('professional_contacts') or self.c.public_business_contact(host, org, now()):
            return []
        pages, scope = [], list(org.get('scope_evidence', []))
        retained = list(res.get('pages', [])) + list(org.get('discovery', {}).get('_markup', []))
        for page in retained:
            html = page.get('html', '')
            final = page.get('final_url', page.get('url'))
            if (not literal_url(page.get('url'), absolute=True)
                    or not literal_url(final, absolute=True)
                    or not self.c.source_on_provider(page.get('url'), host)
                    or not self.c.source_on_provider(final, host)
                    or hashlib.sha256(html.encode('utf-8', 'replace')).hexdigest() != page.get('sha256')
                    or not readiness.current_evidence({'captured_at_utc':page.get('fetched_at_utc'),
                            'evidence_sha256':page.get('sha256')}, now())):
                continue
            rows, contacts = evidence(host, final, html, page['sha256'], page['fetched_at_utc'],
                self.c.public_page_hits, self.c.BUSINESS_LOCALS, self.c.same, sponsor=True)
            if contacts:
                return []  # negative Bouncer contacts are not retry candidates either
            scope.extend(rows)
            pages.append(page)
        bound = {'scope_evidence':scope, 'discovery':{'_markup':pages}}
        qualified = self.c.sponsor_scope_current(host, bound, self.c, now())
        own_us = [r for r in qualified if r.get('country') == 'US'
            and US_SCOPE.search(r.get('country_excerpt', ''))
            and re.search(r'\b(?:we|our|available|ship\w*|deliver\w*|residents?)\b',
                          r.get('country_excerpt', ''), re.I)]
        if not own_us or not any(r.get('sponsor_category') for r in qualified):
            return []
        def key(url):
            u = urlsplit(url)
            return ((u.hostname or '').removeprefix('www.'), u.path.rstrip('/'))
        seen = {key(p[k]) for p in retained for k in ('url', 'final_url')
                if literal_url(p.get(k), absolute=True)}
        attempted = {digest for op in self.operations() if op.get('endpoint') == '/v2/scrape'
                     for digest in op.get('url_hash', [])}
        route = re.compile(r'(?:/(?:en-us|us|en|pages|company))?/(?:partners?|partnerships?|'
            r'corporate-partnerships|brand-partnerships|affiliates?|affiliate-program|'
            r'affiliate-programme|become-an-affiliate)/?', re.I)
        targets = {}
        for page in pages:
            parser = Links(); parser.feed(page['html'])
            if not literal_url(parser.base_href or ''):
                continue
            base = urljoin(page['final_url'], parser.base_href or '')
            if not literal_url(base, absolute=True) or not self.c.source_on_provider(base, host):
                continue
            for href in parser.hrefs:
                if not literal_url(href):
                    continue
                try:
                    url = urljoin(base, href)
                    if not literal_url(url, absolute=True):
                        continue
                    u = urlsplit(url)
                except ValueError:
                    continue
                if (u.scheme != 'https' or u.query or u.fragment
                        or not self.c.source_on_provider(url, host)
                        or not (route.fullmatch(u.path) or (u.path in ('', '/')
                            and (u.hostname or '').split('.')[0] in ('affiliate', 'affiliates', 'partners')))
                        or key(url) in seen or hashlib.sha256(url.encode()).hexdigest() in attempted):
                    continue
                targets.setdefault(key(url), url)
        return sorted(targets.values())

    def _page(self, url, html, meta, source):
        at = now().isoformat()
        sha = hashlib.sha256(html.encode('utf-8', 'replace')).hexdigest()
        return {'url': url, 'final_url': url, 'html': html, 'sha256': sha, 'fetched_at_utc': at,
                'bytes': len(html.encode('utf-8', 'replace')), 'source': source}

    def capture_host(self, host, max_pages=7):
        out = self.pages / (host + '.json')
        continuation = False
        res, picks = {}, []
        if out.exists():
            res = self.c.load(out)
            if dt.datetime.fromisoformat(res['captured_utc']) > now() - dt.timedelta(days=25):
                picks = self.retained_contact_targets(res)
                if not picks:
                    return res
                continuation = True
        if not self.budget.allow(min(max_pages, len(picks)) if continuation else max_pages + 1):
            if continuation:
                return res
            return {'host': host, 'pages': [], 'skipped': 'budget'}
        if continuation:
            return self._capture_pages(out, res, picks[:max_pages])
        res = {'host': host, 'pages': [], 'errors': [], 'credits': 0, 'captured_utc': now().isoformat()}
        try:
            self.capture_admissible('map', 'https://' + host)
            m = self.paid(1, lambda payload, **kw: self.client.request('map', payload, **kw),
                          {'url': 'https://' + host, 'limit': 200, 'includeSubdomains': False, 'sitemap': 'include'}, timeout=90)
            res['credits'] += 1
            links = [x.get('url') if isinstance(x, dict) else x for x in m.get('links', [])]
        except Exception as exc:  # noqa: BLE001 - recorded, never auto-retried
            res['errors'].append('map:' + type(exc).__name__)
            links = []
        picks = []
        for u in ['https://' + host + '/'] + [u for u in links if u and '?' not in u and self.c.source_on_provider(u, host) and URL_KEYS.search(u.split(host, 1)[-1])]:
            if u.rstrip('/') not in {p.rstrip('/') for p in picks}:
                picks.append(u)
        picks[1:] = sorted(picks[1:], key=lambda u: (not bool(re.search(r'partner|affiliat|contact', u, re.I)),
                                                     not bool(re.search(r'about|terms', u, re.I)), u))
        return self._capture_pages(out, res, picks[:max_pages])

    def _capture_pages(self, out, res, picks):
        host = res['host']
        for u in picks:
            try:
                self.capture_admissible('scrape', u)
                # Official v2 /scrape parsers=[] disables per-page PDF parsing;
                # mapped documents/redirects are flat 1 credit, not N PDF pages.
                # Raw HTML/basic only, without optional paid formats/modifiers.
                s = self.paid(1, self.client.scrape, {'url': u, 'formats': ['rawHtml'], 'parsers': [], 'onlyMainContent': False, 'maxAge': 0, 'blockAds': True,
                                        'proxy': 'basic', 'location': {'country': 'US', 'languages': ['en-US']}, 'timeout': 45000}, timeout=90)
            except Exception as exc:  # noqa: BLE001
                res['errors'].append('scrape:' + type(exc).__name__)
                continue
            data = s.get('data') or {}
            meta = data.get('metadata') or {}
            res['credits'] += meta.get('creditsUsed') or 1
            final = meta.get('url') or meta.get('sourceURL') or u
            html = data.get('rawHtml') or ''
            if html and meta.get('statusCode') in (200, None) and self.c.source_on_provider(final, host) and len(html) <= 2_000_000:
                res['pages'].append(self._page(final, html, meta, 'firecrawl_v2_scrape_rawHtml'))
        self.pages.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.c.save(out, res)
        return res

    def capture(self, hosts):
        # Serialize bounded charges: parallel hosts previously raced the cap.
        cap = self.budget.cap
        self.budget.cap = max(0, cap - min(180, cap // 2))
        try:
            results = [self.capture_host(h, max_pages=4) for h in hosts]
        finally:
            self.budget.cap = cap
        self.report['capture'] = {'hosts': len(hosts), 'with_pages': sum(bool(r['pages']) for r in results),
                                  'credits': sum(r.get('credits', 0) for r in results)}

    # -- merge -------------------------------------------------------------
    def retained_pages(self, path, expected_sha):
        """Import-only decoded receipts. Never populate capture/effect caches.

        The caller pins the reviewed manifest's bytes; the installed operation
        ledger independently corroborates the exact requested URL and terminal
        effect. A decoded receipt hash is NOT raw HTTP response provenance.
        """
        from firecrawl_client import LEDGER
        import urllib.parse
        import glp_sponsor_readiness as readiness
        import glp328_outreach_policy as policy
        path = Path(path).resolve()
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha:
            raise RuntimeError('retained_cohort_hash_mismatch')
        manifest = json.loads(raw)
        rows = manifest.get('pages')
        if manifest.get('schema') != 'sponsor-retained-cohort-v1' or not isinstance(rows, list) or not 1 <= len(rows) <= 200:
            raise RuntimeError('retained_cohort_shape')
        receipt_base = (path.parent / manifest.get('receipt_base', '.')).resolve()
        operations = [json.loads(line) for line in LEDGER.read_text().splitlines() if line]
        live = self.c.read_local()
        if not live['suppression']['complete'] or live['faults']:
            raise RuntimeError('suppression_or_local_sources_incomplete')
        values = self.c.read_values()
        if not values or tuple(values[0]) != policy.EXPECTED_HEADERS:
            raise RuntimeError('registry_schema_drift')
        registry = [policy.classify_row(i, r) for i, r in enumerate(values[1:], 2)]
        blocked = {x['domain'] for x in live['suppression']['entries']} | {x['domain'] for x in live['conversations']}
        blocked |= {r['host'] for r in registry if r.get('blocks_org') and r.get('host')}
        grouped = {}
        for row in rows:
            host = row['host']
            requested, source = row['requested_url'], row['source_url']
            def owned(url):
                u = urllib.parse.urlsplit(url)
                return (u.scheme == 'https' and not u.fragment and
                        self.c.source_on_provider(url, host) and
                        (u.hostname or '').lower().removeprefix('www.') == host)
            if (not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', host) or 'fella' in host
                    or host.endswith(self.c.PLATFORMS) or not owned(requested) or not owned(source)):
                raise RuntimeError('retained_source_mismatch')
            stem = self.c.normalize(host.split('.')[0])
            if (any(policy._same_adverse_org(host, h) for h in blocked) or
                    any(stem == self.c.normalize(r.get('program', '')) and r.get('host') != host for r in registry)):
                raise RuntimeError('retained_org_held_or_named')
            receipt_path = (receipt_base / row['receipt']).resolve()
            if not receipt_path.is_relative_to(receipt_base):
                raise RuntimeError('retained_receipt_path')
            receipt_raw = receipt_path.read_bytes()
            if hashlib.sha256(receipt_raw).hexdigest() != row['receipt_sha256']:
                raise RuntimeError('retained_receipt_hash_mismatch')
            receipt = json.loads(receipt_raw)
            if receipt.get('host') != host:
                raise RuntimeError('retained_source_mismatch')
            # Successful exact terminal receipt; an outstanding started/ambiguous
            # effect on this URL remains held even if a different ID succeeded.
            url_sha = hashlib.sha256(requested.encode()).hexdigest()
            attempts = {}
            for op in operations:
                if op.get('caller') == CALLER and url_sha in op.get('url_hash', []) and op.get('endpoint') == '/v2/scrape':
                    attempts.setdefault(op['operation_id'], []).append(op)
            if not attempts or any(len(v) != 2 or v[0].get('outcome') != 'started' or
                    v[-1].get('outcome') != 'success' or v[-1].get('http_status') != 200 for v in attempts.values()):
                raise RuntimeError('retained_effect_uncertain')
            terminal = (attempts.get(row['operation_id']) or [{}])[-1]
            if terminal.get('outcome') != 'success':
                raise RuntimeError('retained_effect_mismatch')
            response = receipt.get('response')
            if response is not None:
                data = response.get('data') or {}
                meta = data.get('metadata') or {}
                if (receipt.get('requested_url') != requested or response.get('success') is not True
                        or meta.get('statusCode') != 200 or not owned(meta.get('url', ''))
                        or not owned(meta.get('sourceURL', '')) or meta['url'] != source or meta['sourceURL'] != source
                        or receipt.get('observed_at') != row['observed_utc']
                        or not meta.get('scrapeId') or terminal.get('scrape_search_id') !=
                        'sha256:' + hashlib.sha256(meta['scrapeId'].encode()).hexdigest()):
                    raise RuntimeError('retained_source_mismatch')
                html = data.get('rawHtml')
                provenance_kind = 'retained_decoded_firecrawl_receipt'
            else:
                # Legacy producer capture receipts lack the raw envelope and
                # requested URL. Require their exact page and terminal time join;
                # preserve this limitation rather than inventing HTTP metadata.
                capture = receipt.get('capture') or {}
                pages = [p for p in capture.get('pages', []) if p.get('final_url') == source and
                         p.get('sha256') == row['sha256'] and p.get('fetched_at_utc') == row['observed_utc']]
                if capture.get('host') != host or len(pages) != 1:
                    raise RuntimeError('retained_source_mismatch')
                html = pages[0]['html']
                provenance_kind = 'retained_producer_capture'
            if (not isinstance(html, str) or not html or len(html) > 2_000_000 or
                    hashlib.sha256(html.encode('utf-8', 'replace')).hexdigest() != row['sha256']):
                raise RuntimeError('retained_page_hash_mismatch')
            if not readiness.current_evidence({'captured_at_utc': row['observed_utc'], 'evidence_sha256': row['sha256']}, now()):
                raise RuntimeError('retained_evidence_expired')
            delta = dt.datetime.fromisoformat(row['observed_utc']) - dt.datetime.fromisoformat(terminal['at_utc'])
            if not dt.timedelta(0) <= delta <= dt.timedelta(seconds=120):
                raise RuntimeError('retained_effect_mismatch')
            page = {'url': requested, 'final_url': source, 'html': html, 'sha256': row['sha256'],
                    'fetched_at_utc': row['observed_utc'], 'bytes': len(html.encode('utf-8', 'replace')),
                    'source': provenance_kind, 'provenance': {
                        'cohort_sha256': expected_sha, 'receipt_path': str(receipt_path),
                        'receipt_sha256': row['receipt_sha256'], 'operation_id': row['operation_id'],
                        'requested_url': requested, 'source_url': source, 'raw_http_provenance': False}}
            grouped.setdefault(host, []).append(page)
        for host, pages in grouped.items():
            scope = [r for p in pages for r in evidence(host, p['final_url'], p['html'], p['sha256'], p['fetched_at_utc'],
                self.c.public_page_hits, self.c.BUSINESS_LOCALS, self.c.same, sponsor=True)[0]]
            qualified = self.c.sponsor_scope_current(host, {'scope_evidence': scope, 'discovery': {'_markup': pages}}, self.c, now())
            if not (any(r.get('sponsor_category') for r in qualified) and any(r.get('country') == 'US' for r in qualified)):
                raise RuntimeError('retained_scope_refused')
        return [{'host': h, 'pages': pages} for h, pages in sorted(grouped.items())]

    def merge(self, *, retained_cohort=None, cohort_sha256=None):
        lock = self.locked()
        try:
            disc = self.c.load(self.discovery)
            captures = [self.c.load(f) for f in sorted(self.pages.glob('*.json'))] if not getattr(self, 'import_only', False) else []
            if retained_cohort:
                captures.extend(self.retained_pages(retained_cohort, cohort_sha256))
            ours = {p['sha256'] for res in captures for p in res['pages']} if not retained_cohort else set()
            for org in disc['organizations'].values():
                d0 = org.get('discovery') or {}
                if isinstance(d0.get('_markup'), list):
                    d0['_markup'] = [m for m in d0['_markup'] if m.get('sha256') not in ours or m.get('provenance') or m.get('retained_provenance')]
                if isinstance(d0.get('candidates'), list):
                    d0['candidates'] = [x for x in d0['candidates'] if not (isinstance(x, dict) and x.get('page_sha256') in ours and not x.get('provenance') and not x.get('retained_provenance'))]
                org['scope_evidence'] = [r for r in (org.get('scope_evidence') or []) if r.get('sha256') not in ours or r.get('provenance') or r.get('retained_provenance')]
            added = {'hosts': 0, 'scope_rows': 0, 'contacts': 0}
            for res in captures:
                host = res['host']
                if not res['pages']:
                    continue
                org = disc['organizations'].setdefault(host, {'provider': host, 'rows': [], 'population': 'pending', 'url': 'https://' + host,
                                                              'discovery': {'_markup': [], 'candidates': [], 'root_host': host}, 'scope_evidence': []})
                d = org.setdefault('discovery', {})
                d.setdefault('_markup', [])
                d.setdefault('candidates', [])
                org.setdefault('scope_evidence', [])
                for p in res['pages']:
                    existing = max((m for m in d['_markup'] if m.get('final_url', m.get('url')) == p['final_url'] and m.get('sha256') == p['sha256']),
                                   key=lambda m: dt.datetime.fromisoformat(m['fetched_at_utc']), default=None)
                    # A later independently captured observation gets its own
                    # evidence rows; retain the original receipt-bearing records.
                    if existing is not None and dt.datetime.fromisoformat(p['fetched_at_utc']) <= dt.datetime.fromisoformat(existing['fetched_at_utc']):
                        if p.get('provenance') and p['fetched_at_utc'] == existing['fetched_at_utc']:
                            bound = [existing] + [r for r in org['scope_evidence'] if r.get('source_url') == p['final_url'] and r.get('sha256') == p['sha256'] and r.get('observed_utc') == p['fetched_at_utc']]
                            bound += [r for r in d['candidates'] if isinstance(r, dict) and r.get('page_sha256') == p['sha256'] and r.get('captured_at_utc') == p['fetched_at_utc']]
                            for record in bound:
                                if record.get('provenance') != p['provenance'] and p['provenance'] not in record.get('retained_provenance', []):
                                    record.setdefault('retained_provenance', []).append(p['provenance'])
                        continue  # Keep original source and any different-content conflicts.
                    rows, contacts = evidence(host, p['final_url'], p['html'], p['sha256'], p['fetched_at_utc'],
                                              self.c.public_page_hits, self.c.BUSINESS_LOCALS, self.c.same, sponsor=True)
                    d['_markup'].append({k: p[k] for k in ('url', 'final_url', 'html', 'sha256', 'fetched_at_utc', 'bytes', 'source', 'provenance') if k in p})
                    if 'provenance' in p:
                        for row in rows:
                            row['provenance'] = p['provenance']
                        for contact in contacts:
                            contact['provenance'] = p['provenance']
                    org['scope_evidence'].extend(rows)
                    d['candidates'].extend(contacts)
                    added['scope_rows'] += len(rows)
                    added['contacts'] += len(contacts)
                added['hosts'] += 1
            self.c.reconcile_public_stock(disc, now())
            self.c.save(self.discovery, disc)
        finally:
            lock.close()
        self.report.setdefault('merge', []).append(added)

    # -- alexandria --------------------------------------------------------
    def discover_apollo_contacts(self):
        """Free catalogue discovery; classify contracts, never filter stale IDs.

        The aliases below are the existing journal/cleaning operations, not
        provider IDs. Keep them stable so ambiguous claims cannot be replayed
        just because the upstream catalogue renamed a capability.
        """
        self.apollo_capabilities = {}
        res = self.alex.search({'query': 'Apollo search people by organization domain and enrich person business work email',
                                'sources': ['alexandria'], 'limit': 20})
        tools = (res.get('data') or {}).get('tools')
        if res.get('success') is not True or not isinstance(tools, list):
            raise RuntimeError('apollo_contact_discovery_unknown')
        ids = sorted({t['capability'] for t in tools if isinstance(t, dict) and t.get('provider') == 'apollo'
                      and isinstance(t.get('capability'), str) and t['capability']})
        if not ids:
            raise RuntimeError('apollo_contact_capabilities_not_discovered')
        inspected = self.alex.scrape({'alexandria': [{'provider': 'firecrawl', 'capability': 'find-tools',
            'options': {'providers': ['apollo'], 'capabilities': ids, 'level': 'tools', 'expand': ['options'], 'limit': 20}}]})
        entries = (inspected.get('data') or {}).get('alexandria', [])
        if (inspected.get('success') is not True or len(entries) != 1 or entries[0].get('error')
                or entries[0].get('provider') != 'firecrawl' or entries[0].get('capability') != 'find-tools'):
            raise RuntimeError('apollo_contact_discovery_unknown')
        items = (entries[0].get('data') or {}).get('items', [])
        required = {
            'people/search': {'q_organization_domains_list', 'person_locations', 'page', 'per_page'},
            'people/match': {'id', 'reveal_personal_emails', 'reveal_phone_number'},
        }
        selected = {}
        for alias, names in required.items():
            candidates = []
            for item in items:
                execute = item.get('execute') or {}
                options = {o.get('name') for o in item.get('options', []) if isinstance(o, dict)}
                cost = item.get('creditsCost')
                price_ok = (type(cost) is int and cost == (0 if alias == 'people/search' else 30))
                if alias == 'people/match':
                    ceiling = (item.get('billing') or {}).get('maxCredits')
                    price_ok = price_ok and type(ceiling) is int and ceiling == 30
                if (item.get('provider') == 'apollo' and item.get('capability') in ids and names <= options and price_ok
                        and execute.get('provider') == 'apollo' and execute.get('capability') == item.get('capability')):
                    candidates.append(item['capability'])
            if len(candidates) != 1:
                raise RuntimeError('apollo_contact_capabilities_not_discovered')
            selected[alias] = candidates[0]
        if len(set(selected.values())) != 2:
            raise RuntimeError('apollo_contact_capabilities_not_discovered')
        self.apollo_capabilities = selected
        self.report['alexandria_discovered'] = selected
        return selected

    def apollo_scrape(self, payload):
        """Translate stable journal aliases only through this run's catalogue."""
        rows = payload.get('alexandria', [])
        if len(rows) != 1 or rows[0].get('provider') != 'apollo':
            raise RuntimeError('apollo_contact_execution_unknown')
        row = rows[0]
        alias = row.get('capability')
        capability = self.apollo_capabilities.get(alias)
        if not capability:
            raise RuntimeError('apollo_contact_capabilities_not_discovered')
        response = self.alex.scrape({'alexandria': [row | {'capability': capability}]})
        entries = (response.get('data') or {}).get('alexandria', [])
        if len(entries) != 1 or entries[0].get('provider') != 'apollo' or entries[0].get('capability') != capability:
            raise RuntimeError('apollo_contact_execution_unknown')
        # Only the envelope alias changes; observe still checks success/errors,
        # status and minimizes contact fields before completing the same journal.
        return response | {'data': response['data'] | {'alexandria': [entries[0] | {'capability': alias}]}}

    def apollo(self):
        import glp_sponsor_alexandria as alexandria
        from glp328_outreach_policy import _same_adverse_org
        self.discover_apollo_contacts()
        lock = self.locked()
        try:
            live, plan = self.plan()
            disc = self.c.load(self.discovery)
            suppressed = {x['domain'] for x in live['suppression']['entries']} | {x['domain'] for x in live['conversations']}
            hosts = [h for h in eligible_hosts(plan['exclusions'], suppressed, _same_adverse_org)
                     if not any(x.get('person_id') for x in (disc['organizations'].get(h) or {}).get('professional_contacts', []))]
            at = now()
            out = {'targets': len(hosts), 'matches': 0, 'qualified': [], 'empty': 0, 'unknown': 0}
            for host in hosts:
                if out['matches'] >= self.match_cap or not self.budget.allow(30):
                    break
                try:
                    found = alexandria.observe(self.cache, host, 'people/search', {'q_organization_domains_list': [host], 'person_locations': ['United States'],
                                               'page': 1, 'per_page': 50}, self.apollo_scrape, at)
                    people = [p for p in found.get('people', []) if p.get('id') and p.get('has_email') is True and alexandria.business_title(p.get('title'))]
                    people.sort(key=lambda p: (not bool(PEOPLE_TITLES.search(p.get('title', ''))), p['id']))
                    if not people:
                        out['empty'] += 1
                        continue
                    matched = alexandria.observe(self.cache, host, 'people/match', {'id': people[0]['id'], 'reveal_personal_emails': False,
                                                 'reveal_phone_number': False}, self.paid_match, at)
                    out['matches'] += 1
                    row = apollo_contact(host, matched.get('person') or {}, people[0]['id'], alexandria.business_title, at, matched)
                    if not row:
                        out['unknown'] += 1
                        continue
                    disc['organizations'].setdefault(host, {}).setdefault('professional_contacts', []).append(row)
                    out['qualified'].append(host)
                except (RuntimeError, OSError, ValueError, KeyError, TypeError):
                    out['unknown'] += 1
            self.c.reconcile_public_stock(disc, now())
            self.c.save(self.discovery, disc)
        finally:
            lock.close()
        self.report['apollo'] = out

    # -- verification ------------------------------------------------------
    def verify(self, cap=25):
        import glp_sponsor_readiness as readiness
        lock = self.locked()
        try:
            _, plan = self.plan()
            mail = self.c.service('read')
            admitted, refused, calls = [], [], 0
            journal_dir = self.state / 'firecrawl-supply-verify'
            journal_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            for row in plan['recipients']:
                ok, why = self.c.mail_admit(row, {}, mail)
                if not ok:
                    refused.append({'host': row['host'], 'why': str(why)[:120]})
                    continue
                admitted.append(row)
                if readiness.bouncer_readiness(row['email'])['status'] not in ('unknown', 'expired') or calls >= cap:
                    continue
                journal = journal_dir / (readiness.email_hash(row['email']) + '.json')
                if journal.exists() and self.c.load(journal)['status'] != 'observed':
                    continue  # ambiguous prior attempt: never replay automatically
                receipt = {'status': 'claimed', 'host': row['host'], 'at': now().isoformat()}
                self.c.save(journal, receipt)
                proc = subprocess.run([sys.executable, str(self.c.ROOT / 'scripts/bouncer_shared_account_guard.py'), 'verify-email', '--venture', 'glp-scout',
                                       '--email', row['email'], '--ref', self.c.CAMPAIGN + ':firecrawl-supply', '--timeout', '30'],
                                      text=True, capture_output=True, timeout=90)
                calls += 1
                receipt.update(status='observed', returncode=proc.returncode, readiness=readiness.bouncer_readiness(row['email'])['status'])
                self.c.save(journal, receipt)
            verified = sorted({x['host'] for x in admitted if readiness.bouncer_readiness(x['email'])['send_eligible']})
        finally:
            lock.close()
        self.report['verify'] = {'recipients': len(plan['recipients']), 'mail_admitted': len(admitted), 'verified_admitted': len(verified),
                                 'rolling_verified_recipients': len(verified), 'rolling_target': 75, 'rolling_target_met': len(verified) >= 75,
                                 'next_weekday_batch_capacity': min(25, len(verified)),
                                 'verified_hosts': verified, 'mail_refused': refused, 'verification_calls': calls}

    def discovery_search(self, payload):
        from glp_sponsor_discovery import AdmissionRefused, REFUSALS
        entered = False

        def transport(body, **kwargs):
            nonlocal entered
            entered = True
            return self.client.search(body, **kwargs)

        try:
            return self.paid(20, transport, payload, timeout=90)
        except RuntimeError as exc:
            if not entered and str(exc) in REFUSALS:
                raise AdmissionRefused(str(exc)) from exc
            raise  # Same error after HTTP/settlement is ambiguous, never replay.

    def discover(self, *, free_only=False, directory_batches=2):
        from glp_sponsor_discovery import Discovery
        discovery = Discovery(self.state / 'sponsor-discovery', search=None if free_only else self.discovery_search)
        self.report['discovery'] = discovery.run(free_only=free_only, directory_batches=directory_batches)
        return discovery.load()['candidates']

    def daily(self, *, retained_leads=()):
        if self.budget.remaining <= FLOOR_CREDITS:
            self.report['status'] = 'STOPPED_CREDIT_FLOOR'
            return self.report
        from glp328_outreach_policy import _same_adverse_org
        live, plan = self.plan()
        import glp328_outreach_policy as policy
        values = self.c.read_values()
        registry = [policy.classify_row(i, r) for i, r in enumerate(values[1:], 2)]
        blocked = {x['domain'] for x in live['suppression']['entries']} | {x['domain'] for x in live['conversations']} | {r['host'] for r in registry if r.get('blocks_org') and r.get('host')}
        targets = set(eligible_hosts(plan['exclusions'], blocked, _same_adverse_org)) | set(SPONSOR_SEEDS)
        discovery_dir = self.c.ROOT / 'data/firecrawl/jobs/provider-discovery'
        for path in discovery_dir.glob('*.json'):
            for candidate in self.c.load(path).get('candidates', []):
                ev = candidate.get('evidence') or {}
                host = candidate.get('host', '')
                if (ev.get('page_sha256') and ev.get('glp_sentence') and ev.get('us_sentence')
                        and self.c.source_on_provider(ev.get('page_url'), host)):
                    targets.add(host)
        # Explicit retained search receipts feed this same producer, never a new
        # store/job. Search URLs are research leads, not qualification evidence.
        from urllib.parse import parse_qsl, urlsplit
        known = {r['host'] for r in registry if r.get('host')}
        organizations = self.c.load(self.discovery).get('organizations', {})
        known.update(organizations)
        known.update(f.stem for f in self.pages.glob('*.json'))
        known_names = {self.c.normalize(r.get('program', '')) for r in registry}
        known_names.update(self.c.normalize(o.get('provider', '')) for o in organizations.values())
        # A recurring producer, not a finite seed replay. Persisted candidates
        # remain a backlog until capture; discovery's own observation is NOT a
        # seen-host exclusion. Directories give literal official URLs only, not
        # US/category/contact permission. Search keeps the existing own-offer filter.
        fresh_hosts = set()
        from glp_sponsor_discovery import research_host
        for h, row in ({} if self.import_only else self.discover()).items():
            host = research_host(row.get('url'))
            if (host != h or (row.get('source') != 'wikidata_P856' and not firstparty_research_host(row))
                    or any(_same_adverse_org(h, b) for b in known | blocked)
                    or any(self.c.normalize(label) in known_names for label in h.split('.')[:-1])):
                continue
            fresh_hosts.add(h)
            known.add(h)
            known_names.add(self.c.normalize(h.split('.')[0]))
        targets.update(fresh_hosts)
        retained_hosts, bindings, refused = set(), [], []
        for path in retained_leads:
            path = Path(path)
            input_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            records = bound_lead_rows(path)
            bindings.append({'path':str(path), 'sha256':input_sha, 'rows':len(records),
                             'receipts':dict((row['receipt'], sha) for row, sha in records)})
            for row, receipt_sha in records:
                url = row.get('url', '')
                parts = urlsplit(url)
                host = policy.canonical_host(url)
                relevant = firstparty_research_host(row)
                # Firecrawl's search token is not an affiliate link; all other
                # query-bearing/tracking URLs retain the original refusal.
                query_ok = not parts.query or all(k == 'srsltid' for k, v in parse_qsl(parts.query, keep_blank_values=True))
                if (not relevant or parts.scheme != 'https' or not query_ok or parts.fragment or parts.username or parts.password
                        or not host or not self.c.source_on_provider(parts._replace(query='').geturl(), host)
                        or any(_same_adverse_org(host, h) for h in known | blocked)
                        # A named root alias carries the same known/adverse
                        # identity into descendants, even before its root row.
                        or any(self.c.normalize(label) in known_names for label in host.split('.')[:-1])):
                    refused.append({'url':url, 'reason':'irrelevant_or_known_held_unsafe', 'receipt_sha256':receipt_sha})
                    continue
                retained_hosts.add(host)
                known.add(host)  # Parent/subdomain aliases in this same batch.
                known_names.add(self.c.normalize(host.split('.')[0]))
        targets.update(retained_hosts)
        # Seeds have no authority; all must later prove first-party US/product scope.
        hosts = sorted((h for h in targets if re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', h)
                       and 'fella' not in h and not h.endswith(self.c.PLATFORMS)
                       and not any(_same_adverse_org(h, b) for b in blocked)),
                       key=lambda h: ((self.pages / (h + '.json')).exists(), h not in fresh_hosts, h not in retained_hosts, h))
        self.report['target_universe'] = {'research_hosts': len(hosts), 'registry_dependency': False,
            'untouched_discovery_hosts': len(set(hosts) & fresh_hosts),
            'discovery_selected_hosts': sorted(set(hosts) & fresh_hosts),
            'untouched_retained_hosts': len(set(hosts) & retained_hosts),
            'retained_lead_inputs':bindings, 'retained_selected_hosts':sorted(retained_hosts),
            'retained_refused':refused}
        self.capture(hosts)
        self.merge()
        self.apollo()
        self.merge()
        self.verify()
        self.report.update(status='COMPLETED', credits_spent_estimate=self.budget.spent, finished_utc=now().isoformat())
        return self.report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('phase', choices=('daily', 'merge', 'apollo', 'verify'))
    ap.add_argument('--run-cap', type=int, default=RUN_CREDIT_CAP)
    ap.add_argument('--retained-leads', type=Path, action='append', default=[],
                    help='Existing retained search-hit JSONL; untouched research leads for daily only')
    ap.add_argument('--retained-cohort', type=Path, help='Pinned exact first-party receipt manifest; import-only merge, never recapture')
    ap.add_argument('--cohort-sha256', help='SHA256 of independently reviewed manifest bytes')
    args = ap.parse_args(argv)
    if args.retained_leads and args.phase != 'daily':
        ap.error('retained leads only feed daily selection')
    if bool(args.retained_cohort) != bool(args.cohort_sha256) or (args.retained_cohort and args.phase != 'merge'):
        ap.error('retained cohort and hash are required together, only for merge')
    import glp_sponsor_campaign as campaign
    campaign.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (campaign.STATE / 'firecrawl-supply-budget.lock').open('a') as budget_lock:
        fcntl.flock(budget_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        producer = Producer(run_cap=args.run_cap, import_only=bool(args.retained_cohort), budget_lock=budget_lock)
        if args.retained_cohort:
            producer.merge(retained_cohort=args.retained_cohort, cohort_sha256=args.cohort_sha256)
            report = producer.report
        else:
            report = producer.daily(retained_leads=args.retained_leads) if args.phase == 'daily' else (getattr(producer, args.phase)() or producer.report)
        path = producer.state / 'firecrawl-supply-latest.json'
        producer.c.save(path, report)
        print(json.dumps({k: v for k, v in report.items() if k != 'verify'} | {'verify': {k: v for k, v in (report.get('verify') or {}).items() if k != 'mail_refused'}}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
