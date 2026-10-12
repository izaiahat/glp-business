#!/usr/bin/env python3
"""Scheduled Firecrawl jobs for GLP machines (FC71 plan items 2, 3, 4, 6).

Jobs, each run by its own cron wrapper with a per-day credit cap that is read
back from data/firecrawl/operations.jsonl before every paid call:
  provider_research  F04: Firecrawl + Alexandria research packet per registry
                     provider (US Rx only, Fella excluded): official pages,
                     terms, pricing, NPI, BBB, FDA, Gov Index, research index.
  provider_discovery F01: search + Alexandria for US Rx GLP-1 telehealth
                     providers and their affiliate programs -> candidates.
  seo_signals        F05/K44 feed: news, research-paper, Gov Index and SERP
                     signals written as an input file. K44 owns the weekly
                     review; this job never writes that review's outputs.
  budget             Plan item 6: credit readback, per-job spend, pace.

Every job writes evidence files only. It never publishes, never applies to a
program, never sends anything and never changes a provider hold.
Alexandria rule: a capability is called only after free find-tools discovery
in the same run returned it, and every call is ledgered with a caller name.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get('GLP_BUSINESS_ROOT', '/home/ubuntu/business'))
sys.path.extend(p for p in (str(ROOT / 'scripts'),) if p not in sys.path)
from firecrawl_client import FirecrawlClient, firecrawl_urlopen  # noqa: E402

LEDGER = ROOT / 'data/firecrawl/operations.jsonl'
OUT = ROOT / 'data/firecrawl/jobs'
CONTRACTS = ROOT / 'site-system/flagship-v2/candidates/glp34-provider-families-20260912/data/provider-page-contracts.json'
FLOOR = 3000
PAGES_PER_PROVIDER = 20
PERIOD_END = '2026-10-19T23:59:59+00:00'
CAPS = {'provider_research': 6000, 'provider_discovery': 2500, 'seo_signals': 1500, 'sponsor_supply': 1500}
CALLERS = {
    'provider_research': ('fc_provider_research', 'fc_provider_research_alexandria'),
    'provider_discovery': ('fc_provider_discovery', 'fc_provider_discovery_alexandria'),
    'seo_signals': ('fc_seo_signals', 'fc_seo_signals_alexandria'),
    'sponsor_supply': ('sponsor_firecrawl_supply', 'sponsor_firecrawl_alexandria'),
}
EXCLUDED = re.compile(r'fella', re.I)
TRACKING = re.compile(r'aff_c|revoffers|awin1|clickdealer|everflow|/track|utm_|irclickid|tracking', re.I)


def now():
    return dt.datetime.now(dt.timezone.utc)


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def spent_today(callers, ledger=LEDGER, at=None):
    """Credits ledgered today (UTC) by these callers. Unknown cost rows count 1."""
    day = (at or now()).strftime('%Y-%m-%d')
    total = 0
    if not Path(ledger).exists():
        return 0
    with open(ledger) as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get('caller') in callers and str(row.get('at_utc', '')).startswith(day) and row.get('outcome') == 'success':
                credits = row.get('creditsUsed')
                total += credits if isinstance(credits, int) else 1
    return total


def spent_by_caller(since, ledger=LEDGER):
    out = {}
    if not Path(ledger).exists():
        return out
    with open(ledger) as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get('at_utc', '')) < since or row.get('outcome') != 'success':
                continue
            c = row.get('caller') or 'unknown'
            credits = row.get('creditsUsed')
            out[c] = out.get(c, 0) + (credits if isinstance(credits, int) else 0)
    return out


class Budget:
    """Per-job daily cap plus the hard account floor. Refuses before a call."""

    def __init__(self, job, client, cap=None, ledger=LEDGER, floor=FLOOR):
        self.job, self.client, self.ledger, self.floor = job, client, ledger, floor
        self.cap = CAPS[job] if cap is None else cap
        self.callers = CALLERS[job]
        usage = client.request('team/credit-usage', method='GET')
        data = usage.get('data') or usage
        self.remaining = int(data['remainingCredits'])
        self.start_spent = spent_today(self.callers, ledger)
        self.run_spent = 0

    def allow(self, estimate):
        if self.remaining - self.run_spent - estimate < self.floor:
            return False
        return self.start_spent + self.run_spent + estimate <= self.cap

    def charge(self, credits):
        self.run_spent += credits if isinstance(credits, int) and credits >= 0 else 1


class Alexandria:
    """Discover-then-execute. Only capabilities returned by free discovery run."""

    def __init__(self, client):
        self.client = client
        self.allowed = {}

    def discover(self, provider, capabilities):
        payload = {'alexandria': [{'provider': 'firecrawl', 'capability': 'find-tools',
                                   'options': {'providers': [provider], 'capabilities': list(capabilities),
                                               'level': 'tools', 'expand': ['options'], 'limit': 20}}]}
        res = self.client.scrape(payload)
        for entry in (res.get('data') or {}).get('alexandria', []):
            for item in (entry.get('data') or {}).get('items', []):
                if item.get('provider') == provider and item.get('capability') in capabilities:
                    self.allowed[(provider, item['capability'])] = int(item.get('creditsCost') or 0)
        return {k[1]: v for k, v in self.allowed.items() if k[0] == provider}

    def call(self, provider, capability, options, budget):
        if (provider, capability) not in self.allowed:
            raise RuntimeError('alexandria_capability_not_discovered')
        price = self.allowed[(provider, capability)]
        if not budget.allow(price):
            raise RuntimeError('budget_refused')
        res = self.client.scrape({'alexandria': [{'provider': provider, 'capability': capability, 'options': options}]})
        entries = (res.get('data') or {}).get('alexandria', [])
        if res.get('success') is not True or len(entries) != 1 or entries[0].get('error'):
            budget.charge(price)
            raise RuntimeError('alexandria_result_unknown')
        budget.charge(entries[0].get('creditsCost', price))
        return entries[0].get('data')


# ---------------------------------------------------------------- web helpers

def first_party(url, host):
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    h = (u.hostname or '').lower()
    apex = '.'.join(host.split('.')[-2:])
    return u.scheme == 'https' and (h == host or h.endswith('.' + apex) or h == apex) and not TRACKING.search(url)


def scrape_page(client, budget, url, formats=('markdown',), extra=None, est=1):
    """One first-party page with changeTracking (no extra cost) and maxAge 0."""
    if not budget.allow(est):
        raise RuntimeError('budget_refused')
    body = {'url': url, 'formats': list(formats) + [{'type': 'changeTracking', 'modes': ['git-diff']}],
            'onlyMainContent': True, 'maxAge': 0, 'blockAds': True, 'timeout': 45000,
            'location': {'country': 'US', 'languages': ['en-US']}}
    body.update(extra or {})
    res = client.scrape(body, timeout=90)
    data = res.get('data') or {}
    meta = data.get('metadata') or {}
    budget.charge(meta.get('creditsUsed') or est)
    md = data.get('markdown') or ''
    return {'url': meta.get('url') or meta.get('sourceURL') or url, 'status': meta.get('statusCode'),
            'title': meta.get('title'), 'markdown_sha256': hashlib.sha256(md.encode()).hexdigest(),
            'markdown': md[:60000], 'change': (data.get('changeTracking') or {}).get('changeStatus'),
            'json': data.get('json'), 'summary': data.get('summary'), 'fetched_at_utc': now().isoformat()}


def map_site(client, budget, url, search, limit=25):
    if not budget.allow(1):
        raise RuntimeError('budget_refused')
    res = client.request('map', {'url': url, 'search': search, 'limit': limit, 'includeSubdomains': False}, timeout=90)
    budget.charge(1)
    links = []
    for x in res.get('links', []):
        u = x.get('url') if isinstance(x, dict) else x
        if u:
            links.append(u)
    return links


def gov_search(client, query, k=10):
    """Firecrawl Government Index: free (0 credits). Ledgered by the client."""
    res = client.request('search/gov', {'query': query, 'k': k}, timeout=60)
    data = res.get('data') or {}
    return data.get('web') or data.get('results') or res.get('results') or []


def research_papers(client, query, k=10):
    """Research Index paper search (GET). Ledgered through firecrawl_urlopen."""
    url = 'https://api.firecrawl.dev/v2/search/research/papers?' + urllib.parse.urlencode({'query': query, 'k': k})
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + client_key(client)}, method='GET')
    with firecrawl_urlopen(req, caller=client.caller, run_id=client.run_id, timeout=60) as r:
        return json.load(r).get('results') or []


def client_key(client):
    from firecrawl_client import api_key
    return client.key or api_key()


def web_search(client, budget, query, limit=10, sources=('web',), tbs=None, categories=None):
    est = 2 * ((limit + 9) // 10) * len(sources)
    if not budget.allow(est):
        raise RuntimeError('budget_refused')
    body = {'query': query, 'limit': limit, 'sources': list(sources), 'country': 'US'}
    if tbs:
        body['tbs'] = tbs
    if categories:
        body['categories'] = list(categories)
    res = client.search(body, timeout=90)
    budget.charge(res.get('creditsUsed') or est)
    data = res.get('data') or {}
    return {s: data.get(s) or [] for s in sources}


# ------------------------------------------------------- provider_research F04

PRICE_SCHEMA = {'type': 'object', 'properties': {
    'medications_offered': {'type': ['array', 'null'], 'items': {'type': 'string'}},
    'compounded_offered': {'type': ['boolean', 'null']},
    'monthly_price_usd_min': {'type': ['number', 'null']},
    'monthly_price_usd_max': {'type': ['number', 'null']},
    'price_quote': {'type': ['string', 'null'], 'description': 'exact sentence from the page stating the price'},
    'states_served_quote': {'type': ['string', 'null']},
    'cancellation_quote': {'type': ['string', 'null']},
    'pharmacy_partner_quote': {'type': ['string', 'null']}}}


def registry_providers(contracts_path=CONTRACTS):
    rows = json.loads(Path(contracts_path).read_text())['providers']
    out = []
    for r in rows:
        url = r.get('official_url') or ''
        if EXCLUDED.search(json.dumps(r.get('display_name')) + url) or not url.startswith('https://'):
            continue
        sources = sorted({u for v in (r.get('field_sources') or {}).values() for u in v
                          if isinstance(u, str) and u.startswith('https://') and not TRACKING.search(u)})
        host = (urllib.parse.urlsplit(url).hostname or '').lower()
        out.append({'program_id': r['program_id'], 'display_name': r.get('display_name'), 'official_url': url,
                    'host': host, 'field_sources': sources,
                    'state': (r.get('publication_state') or {}).get('affiliate_activation_state')})
    return out


def due(packet_path, days):
    if not packet_path.exists():
        return True
    try:
        at = dt.datetime.fromisoformat(json.loads(packet_path.read_text())['generated_at_utc'])
    except (ValueError, KeyError, OSError):
        return True
    return now() - at >= dt.timedelta(days=days)


def provider_packet(p, client, ax, budget):
    """One research packet. Unknown fields stay null; nothing here is adoption."""
    host, name = p['host'], p['display_name'] or p['host']
    packet = {'schema': 'glp-provider-research-packet-v1', 'program_id': p['program_id'], 'display_name': name,
              'host': host, 'generated_at_utc': now().isoformat(), 'status': 'RESEARCH_ONLY_NOT_ADOPTED',
              'pages': [], 'pricing_extract': None, 'npi': None, 'bbb': None, 'fda': None,
              'gov_index': [], 'news': [], 'errors': []}
    urls = list(dict.fromkeys([p['official_url']] + p['field_sources']))
    try:
        for u in map_site(client, budget, 'https://' + host, 'pricing terms faq states pharmacy semaglutide tirzepatide', 40):
            if first_party(u, host) and '?' not in u and u not in urls:
                urls.append(u)
    except Exception as exc:  # noqa: BLE001
        packet['errors'].append('map:' + type(exc).__name__)
    for i, u in enumerate(urls[:PAGES_PER_PROVIDER]):
        if not first_party(u, host):
            continue
        try:
            fmts = ('markdown', {'type': 'json', 'schema': PRICE_SCHEMA,
                                 'prompt': 'Extract only facts stated on this page; null when absent.'}) if i == 0 else ('markdown',)
            page = scrape_page(client, budget, u, fmts, est=5 if i == 0 else 1)
            if i == 0:
                packet['pricing_extract'] = page.pop('json', None)
            packet['pages'].append(page)
        except Exception as exc:  # noqa: BLE001
            packet['errors'].append('scrape:' + type(exc).__name__)
            if str(exc) == 'budget_refused':
                break
    base = re.sub(r'\b(review|health|meds|rx|md|weight loss|glp-1|glp1)\b', '', name, flags=re.I).strip() or name
    for key, provider, capability, options in (
            ('npi', 'npiregistry-cms-hhs-gov', 'providers/search', {'organization_name': base[:60] + '*', 'enumeration_type': 'organization', 'limit': 10}),
            ('bbb', 'bbb-business-profiles-ratings-complaint', 'businesses/search', {'query': base[:100]}),
            ('fda', 'fda-gov', 'records/drug_recalls', {'query': 'recalling_firm:"%s"' % base[:80].replace('"', ''), 'limit': 5})):
        try:
            packet[key] = ax.call(provider, capability, options, budget)
        except Exception as exc:  # noqa: BLE001
            packet['errors'].append(key + ':' + str(exc)[:60])
    try:
        packet['gov_index'] = gov_search(client, f'"{base}" telehealth compounded semaglutide', 5)
    except Exception as exc:  # noqa: BLE001
        packet['errors'].append('gov:' + type(exc).__name__)
    try:
        packet['news'] = web_search(client, budget, f'"{base}" GLP-1', 5, ('news',))['news']
    except Exception as exc:  # noqa: BLE001
        packet['errors'].append('news:' + type(exc).__name__)
    return packet


def job_provider_research(client, ax_client, budget, out=OUT, days=1, limit=None):
    ax = Alexandria(ax_client)
    ax.discover('npiregistry-cms-hhs-gov', ['providers/search'])
    ax.discover('bbb-business-profiles-ratings-complaint', ['businesses/search', 'businesses/profile'])
    ax.discover('fda-gov', ['records/drug_recalls'])
    done, skipped = [], []
    for p in registry_providers()[:limit]:
        path = Path(out) / 'provider-research' / (p['program_id'] + '.json')
        if not due(path, days):
            skipped.append(p['program_id'])
            continue
        if not budget.allow(20):
            break
        packet = provider_packet(p, client, ax, budget)
        private_json(path, packet)
        done.append(p['program_id'])
    return {'job': 'provider_research', 'packets_written': done, 'not_due': skipped,
            'discovered': sorted(f'{a}/{b}' for a, b in ax.allowed)}


# ------------------------------------------------------ provider_discovery F01

DISCOVERY_QUERIES = (
    'online GLP-1 weight loss telehealth semaglutide prescription affiliate program',
    'tirzepatide telehealth provider "affiliate program"',
    'compounded semaglutide online doctor "become an affiliate"',
    'GLP-1 weight loss clinic online "partner program" US',
    'oral semaglutide telehealth prescription new provider 2026',
    'Wegovy pill online prescription telehealth company',
    'Zepbound vial telehealth provider LillyDirect partner',
    'weight loss telehealth startup launches GLP-1 program',
)
AFFILIATE_PATH = re.compile(r'affiliat|partner|ambassador|refer|influencer', re.I)


def known_hosts():
    hosts = set()
    for p in registry_providers():
        hosts.add('.'.join(p['host'].split('.')[-2:]))
    disc = ROOT / 'reports/ops/sponsor-machine-20261003/contact-discovery.private.json'
    if disc.exists():
        try:
            for h in json.loads(disc.read_text()).get('organizations', {}):
                hosts.add('.'.join(h.split('.')[-2:]))
        except ValueError:
            pass
    return hosts


def job_provider_discovery(client, ax_client, budget, out=OUT, queries=DISCOVERY_QUERIES):
    """Search for new US Rx GLP-1 telehealth providers and their affiliate pages.
    Output is a candidate list for the existing application lane; it does not
    apply, sign up, follow tracking links or change the Sheet."""
    known = known_hosts()
    candidates = {}
    for q in queries:
        try:
            rows = web_search(client, budget, q, 10, ('web',), tbs='qdr:y')['web']
        except Exception as exc:  # noqa: BLE001
            if str(exc) == 'budget_refused':
                break
            continue
        for r in rows:
            url = r.get('url') or ''
            host = (urllib.parse.urlsplit(url).hostname or '').lower().removeprefix('www.')
            apex = '.'.join(host.split('.')[-2:])
            if not host or apex in known or EXCLUDED.search(host) or TRACKING.search(url):
                continue
            if re.search(r'katalys|impact\.com|awin|cj\.com|partnerize|clickbank|shareasale|everflow|maxbounty|offers|forum|fortune|bloomberg|reuters|cnbc|businessinsider|har\.com|tellescope|news|times|post|magazine|blog|reddit|youtube|facebook|instagram|tiktok|wikipedia|forbes|healthline|webmd|nytimes|cnn|fda\.gov|nih\.gov|amazon|linkedin|x\.com|twitter', host):
                continue
            c = candidates.setdefault(apex, {'host': apex, 'queries': [], 'urls': [], 'titles': []})
            c['queries'].append(q)
            c['urls'].append(url)
            c['titles'].append(r.get('title'))
    verified = []
    for apex, c in sorted(candidates.items(), key=lambda kv: -len(kv[1]['queries']))[:25]:
        try:
            links = map_site(client, budget, 'https://' + apex, 'affiliate partner program semaglutide tirzepatide', 40)
        except Exception as exc:  # noqa: BLE001
            if str(exc) == 'budget_refused':
                break
            continue
        aff = [u for u in links if AFFILIATE_PATH.search(urllib.parse.urlsplit(u).path) and first_party(u, apex)][:3]
        glp = [u for u in links if re.search(r'semaglutide|tirzepatide|glp', u, re.I) and first_party(u, apex)][:2]
        c.update(affiliate_pages=aff, glp_pages=glp)
        page = None
        target = (glp or [('https://' + apex)])[0]
        try:
            page = scrape_page(client, budget, target, ('markdown',))
        except Exception as exc:  # noqa: BLE001
            c['scrape_error'] = type(exc).__name__
        text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', (page or {}).get('markdown', ''))
        text = re.sub(r'[#*_>`|]+', ' ', text)
        c['evidence'] = {
            'glp_sentence': next((s for s in re.split(r'(?<=[.!?])\s+', text) if re.search(r'semaglutide|tirzepatide|GLP-1', s, re.I) and re.search(r'prescri', s, re.I) and 30 <= len(s) <= 400), None),
            'us_sentence': next((s for s in re.split(r'(?<=[.!?])\s+', text) if re.search(r'all 50 states|United States|licensed in', s, re.I)), None),
            'page_url': (page or {}).get('url'), 'page_sha256': (page or {}).get('markdown_sha256')}
        c['status'] = 'CANDIDATE_FOR_APPLICATION_LANE_REVIEW' if (aff and c['evidence']['glp_sentence']) else 'CANDIDATE_EVIDENCE_INCOMPLETE'
        verified.append(c)
    result = {'schema': 'glp-provider-discovery-v1', 'generated_at_utc': now().isoformat(), 'known_hosts_excluded': len(known),
              'candidates': verified, 'application_effects': 0}
    private_json(Path(out) / 'provider-discovery' / (now().strftime('%Y%m%dT%H%MZ') + '.json'), result)
    private_json(Path(out) / 'provider-discovery' / 'latest.json', result)
    return {'job': 'provider_discovery', 'candidates': len(verified),
            'ready_for_review': sum(c['status'].startswith('CANDIDATE_FOR') for c in verified)}


# ---------------------------------------------------- seo_signals (K44 input)

SEO_TOPICS = ('semaglutide', 'tirzepatide', 'compounded GLP-1', 'oral Wegovy pill', 'Zepbound vials',
              'GLP-1 telehealth cost', 'GLP-1 insurance coverage', 'orforglipron')


def job_seo_signals(client, ax_client, budget, out=OUT, topics=SEO_TOPICS):
    """News (past week), research papers, Gov Index and Trends related queries.
    Written as an *input* file for K44's GLP-44 weekly review; K44 owns gap
    selection and topic decisions, so this never writes keyword_gaps."""
    ax = Alexandria(ax_client)
    ax.discover('firecrawl-trends', ['trends/related_queries'])
    signals = {'schema': 'glp-seo-firecrawl-signals-v1', 'generated_at_utc': now().isoformat(),
               'consumer': 'GLP-44 weekly review (K44 owner); input only', 'topics': {}}
    for t in topics:
        row = {'news': [], 'papers': [], 'gov': [], 'related_queries': None, 'errors': []}
        try:
            row['news'] = [{k: n.get(k) for k in ('url', 'title', 'date', 'snippet')}
                           for n in web_search(client, budget, t + ' news', 10, ('news',), tbs='qdr:w')['news']]
        except Exception as exc:  # noqa: BLE001
            row['errors'].append('news:' + str(exc)[:40])
        try:
            row['papers'] = [{k: p.get(k) for k in ('primaryId', 'title', 'abstract', 'score')}
                             for p in research_papers(client, t + ' weight loss', 8)]
        except Exception as exc:  # noqa: BLE001
            row['errors'].append('papers:' + type(exc).__name__)
        try:
            row['gov'] = [{k: g.get(k) for k in ('url', 'title', 'description')} for g in gov_search(client, t + ' FDA', 8)]
        except Exception as exc:  # noqa: BLE001
            row['errors'].append('gov:' + type(exc).__name__)
        try:
            row['related_queries'] = ax.call('firecrawl-trends', 'trends/related_queries',
                                             {'keyword': t, 'geo': 'US', 'time': 'today 3-m'}, budget)
        except Exception as exc:  # noqa: BLE001
            row['errors'].append('trends:' + str(exc)[:40])
        signals['topics'][t] = row
    private_json(Path(out) / 'seo-signals' / (now().strftime('%Y%m%d') + '.json'), signals)
    private_json(Path(out) / 'seo-signals' / 'latest.json', signals)
    return {'job': 'seo_signals', 'topics': len(signals['topics']),
            'news': sum(len(r['news']) for r in signals['topics'].values()),
            'papers': sum(len(r['papers']) for r in signals['topics'].values())}


# ----------------------------------------------------------------- budget

def job_budget(client, period_start='2026-09-19T23:59:59+00:00', target_spend=62000):
    usage = client.request('team/credit-usage', method='GET')
    data = usage.get('data') or usage
    remaining, plan = int(data['remainingCredits']), int(data['planCredits'])
    end = dt.datetime.fromisoformat(PERIOD_END)
    hours_left = max(0.0, (end - now()).total_seconds() / 3600)
    spent = plan - remaining
    since = (now() - dt.timedelta(days=1)).isoformat()
    report = {'schema': 'glp-firecrawl-budget-v1', 'at_utc': now().isoformat(), 'remaining': remaining,
              'plan': plan, 'spent_period': spent, 'period_end': PERIOD_END, 'hours_left': round(hours_left, 1),
              'floor': FLOOR, 'target_spend_period': target_spend,
              'target_remaining_to_spend': max(0, target_spend - spent),
              'needed_per_day': round(max(0, target_spend - spent) / max(hours_left / 24, 0.04)),
              'daily_caps': CAPS, 'last_24h_by_caller': spent_by_caller(since),
              'stop': remaining <= FLOOR}
    private_json(OUT / 'budget' / 'latest.json', report)
    return report


JOBS = {'provider_research': job_provider_research, 'provider_discovery': job_provider_discovery,
        'seo_signals': job_seo_signals}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('job', choices=sorted(JOBS) + ['budget'])
    ap.add_argument('--cap', type=int, default=None, help='override the per-day credit cap for this job')
    ap.add_argument('--limit', type=int, default=None)
    args = ap.parse_args(argv)
    run = 'fc-' + args.job + '-' + now().strftime('%Y%m%dT%H%M%SZ')
    if args.job == 'budget':
        print(json.dumps(job_budget(FirecrawlClient('fc_budget', run_id=run)), sort_keys=True))
        return 0
    web_caller, ax_caller = CALLERS[args.job]
    client = FirecrawlClient(web_caller, run_id=run)
    budget = Budget(args.job, client, cap=args.cap)
    if not budget.allow(1):
        print(json.dumps({'job': args.job, 'status': 'REFUSED_BUDGET', 'remaining': budget.remaining,
                          'spent_today': budget.start_spent, 'cap': budget.cap}))
        return 0
    kwargs = {'limit': args.limit} if args.job == 'provider_research' and args.limit else {}
    result = JOBS[args.job](client, FirecrawlClient(ax_caller, run_id=run), budget, **kwargs)
    result.update(run=run, run_credits=budget.run_spent, cap=budget.cap,
                  spent_today=spent_today(CALLERS[args.job]), remaining_at_start=budget.remaining)
    private_json(OUT / 'receipts' / (run + '.json'), result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
