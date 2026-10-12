#!/usr/bin/env python3
"""Firecrawl + Alexandria supply producer for the installed sponsor campaign.

Adds only *first-party, gate-passing* evidence to the campaign's existing
contact-discovery file, so the installed ``glp_sponsor_campaign.recipients()``
and ``mail_admit()`` decide admission. It never sends, never enables a send,
never edits sender/transport code, and never guesses or pattern-builds an email:
contacts are either published business-purpose addresses retained in scraped
first-party HTML, or Apollo (Alexandria) professional records whose email
domain equals the registry host. Verification uses only the shared Bouncer guard.

Phases (each idempotent, all under the campaign run.lock, waiting politely):
  capture  map + scrape registry hosts with admission gaps (rawHtml, US, maxAge 0)
  merge    re-derive evidence from retained pages and merge into discovery
  apollo   Alexandria apollo people/search (0 credits) + people/match (30)
  verify   installed mail_admit + shared Bouncer guard for unverified recipients
  daily    capture -> merge -> apollo -> merge -> verify, inside the credit caps

Budget: per-run Firecrawl cap (default 1500 credits), Apollo match cap (25/run),
and a hard stop when the team balance is at or below 3000 credits.
Every Firecrawl call is ledgered by firecrawl_client with caller names
``sponsor_firecrawl_supply`` / ``sponsor_firecrawl_alexandria``.
"""
from __future__ import annotations

import argparse
import concurrent.futures
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


def now():
    return dt.datetime.now(dt.timezone.utc)


def sentences(text):
    """Affirmative sentences only; never split at "U.S." or "Dr."; drop questions."""
    parts = re.split(r'(?<!U\.S)(?<!\bDr)(?<=[.!?])\s+(?=[A-Z])', text)
    return [s.strip() for s in parts if 20 <= len(s.strip()) <= 400 and not s.strip().endswith('?')]


def evidence(host, url, markup, sha, at, page_hits, business_locals, same_org):
    """Return (scope_rows, published_contacts) for one retained first-party page."""
    hits, text = page_hits(markup)
    med = next((s for s in sentences(text) if MEDICATION.search(s) and WE.search(s) and RX.search(s) and not NEG.search(s)), '')
    us = next((s for s in sentences(text) if US.search(s) and US_CTX.search(s) and US_SCOPE.search(s) and not US_REJECT.search(s)), '')
    rows = []
    if med:
        rows.append({'source_url': url, 'country': None, 'country_excerpt': '', 'medication_provider_excerpt': med, 'sha256': sha, 'observed_utc': at})
    if us:
        rows.append({'source_url': url, 'country': 'US', 'country_excerpt': us, 'medication_provider_excerpt': '', 'sha256': sha, 'observed_utc': at})
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
    def __init__(self, state_dir=None, run_cap=RUN_CREDIT_CAP, match_cap=MATCH_CAP):
        import glp_sponsor_campaign as campaign
        from firecrawl_client import FirecrawlClient
        self.c = campaign
        self.state = Path(state_dir or campaign.STATE)
        self.pages = self.state / 'firecrawl-supply-pages'
        self.cache = self.state / 'firecrawl-supply-alexandria'
        self.discovery = campaign.ROOT / 'reports/ops/sponsor-machine-20261003/contact-discovery.private.json'
        self.run = 'sponsor-supply-' + now().strftime('%Y%m%dT%H%M')
        self.client = FirecrawlClient(CALLER, run_id=self.run)
        self.alex = FirecrawlClient(ALEX_CALLER, run_id=self.run)
        usage = self.client.request('team/credit-usage', method='GET').get('data') or {}
        self.budget = Budget(int(usage.get('remainingCredits') or 0), cap=run_cap)
        self.match_cap = match_cap
        self.report = {'run': self.run, 'started_utc': now().isoformat(), 'remaining_at_start': self.budget.remaining, 'sends': 0}

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
    def _page(self, url, html, meta, source):
        at = now().isoformat()
        sha = hashlib.sha256(html.encode('utf-8', 'replace')).hexdigest()
        return {'url': url, 'final_url': url, 'html': html, 'sha256': sha, 'fetched_at_utc': at,
                'bytes': len(html.encode('utf-8', 'replace')), 'source': source}

    def capture_host(self, host, max_pages=7):
        out = self.pages / (host + '.json')
        if out.exists():
            res = self.c.load(out)
            if dt.datetime.fromisoformat(res['captured_utc']) > now() - dt.timedelta(days=25):
                return res
        if not self.budget.allow(max_pages + 1):
            return {'host': host, 'pages': [], 'skipped': 'budget'}
        res = {'host': host, 'pages': [], 'errors': [], 'credits': 0, 'captured_utc': now().isoformat()}
        try:
            m = self.client.request('map', {'url': 'https://' + host, 'limit': 200, 'includeSubdomains': False, 'sitemap': 'include'}, timeout=90)
            res['credits'] += 1
            links = [x.get('url') if isinstance(x, dict) else x for x in m.get('links', [])]
        except Exception as exc:  # noqa: BLE001 - recorded, never auto-retried
            res['errors'].append('map:' + type(exc).__name__)
            links = []
        picks = []
        for u in ['https://' + host + '/'] + [u for u in links if u and '?' not in u and self.c.source_on_provider(u, host) and URL_KEYS.search(u.split(host, 1)[-1])]:
            if u.rstrip('/') not in {p.rstrip('/') for p in picks}:
                picks.append(u)
        for u in picks[:max_pages]:
            try:
                s = self.client.scrape({'url': u, 'formats': ['rawHtml'], 'onlyMainContent': False, 'maxAge': 0, 'blockAds': True,
                                        'location': {'country': 'US', 'languages': ['en-US']}, 'timeout': 45000}, timeout=90)
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
        self.budget.charge(res['credits'])
        self.pages.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.c.save(out, res)
        return res

    def capture(self, hosts):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(self.capture_host, hosts))
        self.report['capture'] = {'hosts': len(hosts), 'with_pages': sum(bool(r['pages']) for r in results),
                                  'credits': sum(r.get('credits', 0) for r in results)}

    # -- merge -------------------------------------------------------------
    def merge(self):
        lock = self.locked()
        try:
            disc = self.c.load(self.discovery)
            ours = {p['sha256'] for f in self.pages.glob('*.json') for p in self.c.load(f)['pages']}
            for org in disc['organizations'].values():
                d0 = org.get('discovery') or {}
                if isinstance(d0.get('_markup'), list):
                    d0['_markup'] = [m for m in d0['_markup'] if m.get('sha256') not in ours]
                if isinstance(d0.get('candidates'), list):
                    d0['candidates'] = [x for x in d0['candidates'] if not (isinstance(x, dict) and x.get('page_sha256') in ours)]
                org['scope_evidence'] = [r for r in (org.get('scope_evidence') or []) if r.get('sha256') not in ours]
            added = {'hosts': 0, 'scope_rows': 0, 'contacts': 0}
            for f in sorted(self.pages.glob('*.json')):
                res = self.c.load(f)
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
                    rows, contacts = evidence(host, p['final_url'], p['html'], p['sha256'], p['fetched_at_utc'],
                                              self.c.public_page_hits, self.c.BUSINESS_LOCALS, self.c.same)
                    if not (rows or contacts):
                        continue
                    d['_markup'].append({k: p[k] for k in ('url', 'final_url', 'html', 'sha256', 'fetched_at_utc', 'bytes', 'source')})
                    org['scope_evidence'].extend(rows)
                    d['candidates'].extend(contacts)
                    added['scope_rows'] += len(rows)
                    added['contacts'] += len(contacts)
                added['hosts'] += 1
            self.c.save(self.discovery, disc)
        finally:
            lock.close()
        self.report.setdefault('merge', []).append(added)

    # -- alexandria --------------------------------------------------------
    def apollo(self):
        import glp_sponsor_alexandria as alexandria
        from glp328_outreach_policy import _same_adverse_org
        lock = self.locked()
        try:
            live, plan = self.plan()
            disc = self.c.load(self.discovery)
            suppressed = {x['domain'] for x in live['suppression']['entries']}
            hosts = [h for h in eligible_hosts(plan['exclusions'], suppressed, _same_adverse_org)
                     if not any(x.get('person_id') for x in (disc['organizations'].get(h) or {}).get('professional_contacts', []))]
            at = now()
            out = {'targets': len(hosts), 'matches': 0, 'qualified': [], 'empty': 0, 'unknown': 0}
            for host in hosts:
                if out['matches'] >= self.match_cap or not self.budget.allow(30):
                    break
                try:
                    found = alexandria.observe(self.cache, host, 'people/search', {'q_organization_domains_list': [host], 'person_locations': ['United States'],
                                               'page': 1, 'per_page': 50}, self.alex.scrape, at)
                    people = [p for p in found.get('people', []) if p.get('id') and p.get('has_email') is True and alexandria.business_title(p.get('title'))]
                    people.sort(key=lambda p: (not bool(PEOPLE_TITLES.search(p.get('title', ''))), p['id']))
                    if not people:
                        out['empty'] += 1
                        continue
                    matched = alexandria.observe(self.cache, host, 'people/match', {'id': people[0]['id'], 'reveal_personal_emails': False,
                                                 'reveal_phone_number': False}, self.alex.scrape, at)
                    out['matches'] += 1
                    self.budget.charge(30)
                    row = apollo_contact(host, matched.get('person') or {}, people[0]['id'], alexandria.business_title, at, matched)
                    if not row:
                        out['unknown'] += 1
                        continue
                    disc['organizations'].setdefault(host, {}).setdefault('professional_contacts', []).append(row)
                    out['qualified'].append(host)
                except (RuntimeError, OSError, ValueError, KeyError, TypeError):
                    out['unknown'] += 1
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
                                 'verified_hosts': verified, 'mail_refused': refused, 'verification_calls': calls}

    def daily(self):
        if self.budget.remaining <= FLOOR_CREDITS:
            self.report['status'] = 'STOPPED_CREDIT_FLOOR'
            return self.report
        from glp328_outreach_policy import _same_adverse_org
        live, plan = self.plan()
        hosts = eligible_hosts(plan['exclusions'], {x['domain'] for x in live['suppression']['entries']}, _same_adverse_org)
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
    args = ap.parse_args(argv)
    producer = Producer(run_cap=args.run_cap)
    report = producer.daily() if args.phase == 'daily' else (getattr(producer, args.phase)() or producer.report)
    path = producer.state / 'firecrawl-supply-latest.json'
    producer.c.save(path, report)
    print(json.dumps({k: v for k, v in report.items() if k != 'verify'} | {'verify': {k: v for k, v in (report.get('verify') or {}).items() if k != 'mail_refused'}}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
