#!/usr/bin/env python3
"""Bounded sponsor research discovery, never contact or eligibility authority.

Wikimedia category index + literal Wikidata P856 (CC0) supply free research
hosts. The existing guarded Firecrawl search supplies rotating US/category
queries, without scraping search hits. Persistent backlog is not a seen-host
exclusion: capture/registry identity and all qualification gates remain owned
by glp_sponsor_firecrawl_supply. No credentials, mailboxes, or sends here.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

WIKIPEDIA = 'https://en.wikipedia.org/w/api.php'
WIKIDATA = 'https://www.wikidata.org/w/api.php'
DIRECTORIES = (
    'Nutritional supplement companies of the United States',
    'Sports nutrition and bodybuilding supplement companies',
    'Health clubs in the United States', 'Fitness apps',
    'Weight loss companies', 'Energy food products',
)
# Query vocabulary expands discovery only, not the sealed category predicates.
CATEGORY_QUERIES = {
    'nutrition_protein': 'protein nutrition shakes powder brand shop',
    'meals': 'healthy prepared meals meal delivery company order',
    'fitness': 'personal training strength training fitness app membership',
    'cgm': 'continuous glucose monitoring metabolic health program',
    'weight_coaching': 'weight loss coaching weight management app program',
    'telehealth_glp1': 'GLP-1 telehealth prescription treatment provider',
    'pharmacy': 'compounding pharmacy prescription delivery',
}
GEOGRAPHIES = ('United States', 'California', 'Texas', 'Florida', 'New York',
               'Illinois', 'Pennsylvania', 'Ohio', 'Georgia', 'North Carolina',
               'Michigan', 'New Jersey', 'Virginia', 'Washington', 'Arizona',
               'Massachusetts', 'Tennessee', 'Indiana', 'Missouri', 'Maryland',
               'Wisconsin', 'Colorado', 'Minnesota', 'South Carolina', 'Alabama',
               'Louisiana', 'Kentucky', 'Oregon', 'Oklahoma', 'Connecticut',
               'Iowa', 'Utah', 'Nevada', 'Arkansas', 'Kansas', 'Mississippi',
               'New Mexico', 'Nebraska', 'Idaho', 'West Virginia', 'Hawaii',
               'New Hampshire', 'Maine', 'Montana', 'Rhode Island', 'Delaware',
               'South Dakota', 'North Dakota', 'Alaska', 'Vermont', 'Wyoming')
REFUSALS = {'sponsor_ledger_credit_cost_unknown', 'sponsor_daily_credit_cap',
            'sponsor_period_credit_cap', 'sponsor_account_credit_floor',
            'sponsor_account_credit_usage_unknown'}


def query_pack():
    return [{'category': category, 'query': f'{words} {place} {purpose}'}
            for purpose in ('partnerships', 'business contact')
            for place in GEOGRAPHIES for category, words in CATEGORY_QUERIES.items()]


def research_host(url):
    """Literal public HTTPS URL only; no invented HTTPS upgrade or tracking."""
    try:
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or '').lower().removeprefix('www.')
        if (p.scheme != 'https' or not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', host)
                or p.username or p.password or p.port not in (None, 443)
                or p.query or p.fragment or '..' in host
                or re.search(r'fella|(?:^|\.)wik(?:ipedia|idata)\.org$|(?:^|\.)archive\.org$', host)
                or re.search(r'/go/|/track|/redirect', p.path, re.I)):
            return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            return host
    except (TypeError, ValueError):
        return None


def directory_site(entity):
    """At most one literal official site per entity; skip regional foreign sites.

    A US-looking URL is NOT proof of US scope. This only avoids paying to
    recapture every regional franchise alias; unchanged first-party US gates
    still decide admission. Do not upgrade http or synthesize a .com address.
    """
    sites = []
    for claim in entity.get('claims', {}).get('P856', []):
        url = claim.get('mainsnak', {}).get('datavalue', {}).get('value')
        host = research_host(url)
        if (host and claim.get('rank') != 'deprecated'
                and host.rsplit('.', 1)[-1] not in {
                    'ca', 'uk', 'au', 'nz', 'jp', 'in', 'it', 'nl', 'ph', 'mx',
                    'de', 'fr', 'es', 'ua', 'tw', 'cn', 'br', 'sg', 'za', 'ie'}):
            sites.append((claim.get('rank') != 'preferred', len(host), host, url))
    return min(sites)[-1] if sites else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_json(api, params):
    if api not in (WIKIPEDIA, WIKIDATA):
        raise ValueError('directory_api_not_allowed')
    url = api + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'GLPClinicScoutResearch/1.0 (https://glpclinicscout.com)',
        'Accept': 'application/json'})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
        body = response.read(2_000_001)
        if response.status != 200 or len(body) > 2_000_000:
            raise ValueError('directory_response_invalid')
        data = json.loads(body)
    if not isinstance(data, dict) or data.get('error'):
        raise ValueError('directory_api_error')
    return data


class Discovery:
    def __init__(self, directory, *, fetch=public_json, search=None):
        self.directory = Path(directory)
        self.path = self.directory / 'state.json'
        self.fetch, self.search = fetch, search

    def load(self):
        if not self.path.exists():
            return {'schema': 'sponsor-discovery-v1', 'directory_index': 0,
                    'directory_continue': None, 'query_index': 0,
                    'pending_query': None, 'candidates': {}}
        state = json.loads(self.path.read_text())
        if state.get('schema') != 'sponsor-discovery-v1' or not isinstance(state.get('candidates'), dict):
            raise ValueError('sponsor_discovery_state_invalid')
        return state

    def save(self, state):
        from glp_firecrawl_jobs import private_json
        private_json(self.path, state)

    def receipt(self, kind, data):
        # Match private_json's exact on-disk serialization, not an unrelated
        # compact JSON digest (which would fail retained byte-hash checks).
        raw = json.dumps(data, sort_keys=True, indent=1).encode()
        digest = hashlib.sha256(raw).hexdigest()
        relative = 'receipts/' + kind + '-' + digest + '.json'
        from glp_firecrawl_jobs import private_json
        private_json(self.directory / relative, data)
        return {'receipt': relative, 'receipt_sha256': digest}

    def run(self, *, free_only=False, directory_batches=2, search_batches=4):
        import fcntl
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / 'discovery.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self._run(free_only=free_only, directory_batches=directory_batches, search_batches=search_batches)

    def _run(self, *, free_only=False, directory_batches=2, search_batches=4):
        state = self.load()
        before = set(state['candidates'])
        at = dt.datetime.now(dt.timezone.utc).isoformat()
        report = {'verified_contacts': 0, 'paid_search_attempts': 0,
                  'directory_status': 'not_requested', 'search_status': 'not_requested'}
        # Same-day directory failure is bounded; no automatic request retries.
        for _ in range(min(directory_batches, 6)):
            index = state['directory_index']
            if index >= len(DIRECTORIES):
                report['directory_status'] = 'exhausted'
                break
            if str(state.get('directory_failed_at', '')).startswith(at[:10]):
                report['directory_status'] = 'failed_today'
                break
            params = {'action': 'query', 'format': 'json', 'generator': 'categorymembers',
                      'gcmtitle': 'Category:' + DIRECTORIES[index], 'gcmnamespace': 0,
                      'gcmlimit': 25, 'prop': 'pageprops', 'ppprop': 'wikibase_item'}
            if state['directory_continue']:
                params['gcmcontinue'] = state['directory_continue']
            try:
                pages = self.fetch(WIKIPEDIA, params)
                articles = (pages.get('query') or {}).get('pages', {})
                if not isinstance(articles, dict):
                    raise ValueError('directory_pages_invalid')
                ids = {p.get('pageprops', {}).get('wikibase_item'): p['title'] for p in articles.values()
                       if re.fullmatch(r'Q\d+', p.get('pageprops', {}).get('wikibase_item', ''))}
                entities = self.fetch(WIKIDATA, {'action': 'wbgetentities', 'format': 'json',
                            'ids': '|'.join(sorted(ids)), 'props': 'claims'}) if ids else {'entities': {}}
                if not isinstance(entities.get('entities'), dict):
                    raise ValueError('directory_entities_invalid')
            except Exception as exc:
                report['directory_status'] = 'failed:' + type(exc).__name__
                state['directory_failed_at'] = at
                self.save(state)
                break
            bound = self.receipt('directory', {'at': at, 'params': params, 'pages': pages, 'entities': entities})
            for q, entity in entities['entities'].items():
                if q not in ids:
                    continue
                url = directory_site(entity)
                host = research_host(url)
                if host:
                    state['candidates'].setdefault(host, {'source': 'wikidata_P856', 'url': url,
                        'entity': q, 'title': ids[q], 'category': DIRECTORIES[index], 'at': at, **bound})
            token = (pages.get('continue') or {}).get('gcmcontinue')
            state['directory_continue'] = token
            if not token:
                state['directory_index'] += 1
            self.save(state)  # Receipt and backlog committed BEFORE consuming cursor.
            report['directory_status'] = 'ok'
        pack = query_pack()
        for _ in range(0 if free_only else min(search_batches, 4)):
            index = state['query_index']
            if state['pending_query'] is not None:
                report['search_status'] = 'pending_reconciliation'
                break
            if index >= len(pack):
                report['search_status'] = 'exhausted'
                break
            if str(state.get('search_refused_at', '')).startswith(at[:10]):
                report['search_status'] = state['search_refusal']
                break
            if self.search is None:
                report['search_status'] = 'transport_not_configured'
                break
            state['pending_query'] = index
            self.save(state)  # Interrupted paid query is NEVER automatically replayed.
            payload = {'query': pack[index]['query'], 'limit': 100, 'sources': ['web'], 'country': 'US'}
            report['paid_search_attempts'] += 1
            try:
                response = self.search(payload)
            except Exception as exc:
                if isinstance(exc, RuntimeError) and str(exc) in REFUSALS:
                    state.update(pending_query=None, search_refused_at=at, search_refusal=str(exc))
                    report['search_status'] = str(exc)
                else:
                    report['search_status'] = 'pending_reconciliation'
                self.save(state)
                break
            from firecrawl_client import search_rows
            bound = self.receipt('search', {'at': at, 'payload': payload, 'response': response})
            rows = search_rows(response, sources=('web',))
            for row in rows:
                host = research_host(row.get('url'))
                # Search snippets are NOT first-party qualification. The caller's
                # existing firstparty_research_host filter also applies at intake.
                if host:
                    state['candidates'].setdefault(host, {'source': 'firecrawl_search', **row,
                        'category': pack[index]['category'], 'at': at, **bound})
            state.update(query_index=index + 1, pending_query=None)
            self.save(state)
            report['search_status'] = 'ok'
        report.update(new_hosts=sorted(set(state['candidates']) - before),
                      candidate_hosts=sorted(state['candidates']),
                      directory_index=state['directory_index'], query_index=state['query_index'],
                      query_count=len(pack))
        return report
