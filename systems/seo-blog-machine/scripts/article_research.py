#!/usr/bin/env python3
"""Bounded Alexandria clinical/regulatory discovery for an article packet.

Discovery is not substantiation: the drafter must inspect original sources and
bind exact quotes in 01/05; independent Astra decides applicability. No article,
publication, consent, CRM or provider mutations. No network retry after ambiguity.
Provider contracts: reports/ops/firecrawl-research-20261007/A-ALEXANDRIA.md §8.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request
import urllib.error
import sys
from urllib.parse import urlencode
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))

ENDPOINT = 'https://api.firecrawl.dev/v2/scrape'
SCHEMA = 'glp-article-alexandria-research-v2'
LEGACY_SCHEMA = 'glp-article-alexandria-research-v1'
READS = {
    'pmid': ('pubmed-ncbi-nlm-nih-gov', 'publications/article', 'pmid', r'[0-9]{1,9}'),
    'pmc': ('pubmed-ncbi-nlm-nih-gov', 'publications/full_text', 'pmcid', r'PMC[0-9]{1,9}'),
    'trial': ('clinicaltrials-gov', 'clinical-trials/study', 'nct_id', r'NCT[0-9]{8}'),
}

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def spec(path):
    return {'path': str(Path(path).resolve()), 'sha256': digest(path)}

def save(path, data):
    # Exclusive capture files: a transport failure never triggers blind replay.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2, sort_keys=True); handle.write('\n')
    path.chmod(0o600)

def key():
    value = os.environ.get('FIRECRAWL_API_KEY', '').strip()
    if not value:
        env = Path.home() / '.hermes/.env'
        if env.is_file():
            for line in env.read_text().splitlines():
                if line.startswith('FIRECRAWL_API_KEY='):
                    value = line.split('=', 1)[1].strip().strip('\"').strip("'"); break
    if not value: raise ValueError('firecrawl_api_key_missing')
    return value

def request_live(body):
    from firecrawl_client import FirecrawlClient, firecrawl_urlopen
    execution = body['alexandria'][0]
    if execution['provider'] in ('firecrawl-research-index', 'firecrawl-gov-index'):
        client = FirecrawlClient('seo.article_research')
        options = execution['options']
        if execution['provider'] == 'firecrawl-research-index':
            payload = client.request('search/research/papers?' + urlencode({'query': options['query'], 'k': 5}), method='GET')
        else:
            payload = client.request('search/gov', {'query': options['query'], 'k': 5})
        return {'success': payload.get('success'), 'data': {'alexandria': [{**execution, 'data': payload}]}}
    request = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + key(), 'Content-Type': 'application/json'}, method='POST')
    try:
        with firecrawl_urlopen(request, caller='seo.article_research', timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'firecrawl_http_{exc.code}') from None
    except (TimeoutError, urllib.error.URLError):
        raise RuntimeError('firecrawl_transport_uncertain_no_automatic_retry') from None

def read_request(row):
    if row.get('kind') not in READS: raise ValueError('research_connector_forbidden')
    provider, capability, field, pattern = READS[row['kind']]
    identifier = str(row.get('id', ''))
    if not re.fullmatch(pattern, identifier): raise ValueError('research_identifier_invalid')
    options = {field: identifier}
    if row['kind'] == 'pmc': options['max_chars'] = 100000
    if row['kind'] == 'trial': options['include_results'] = True
    return {'provider': provider, 'capability': capability, 'options': options}

def validate_manifest(path, target_query):
    path = Path(path)
    data = json.loads(path.read_text())
    if data.get('schema') not in (SCHEMA, LEGACY_SCHEMA) or data.get('target_query') != target_query:
        raise ValueError('research_manifest_binding_invalid')
    if data['schema'] == SCHEMA and len(data.get('citations', [])) < 3:
        raise ValueError('research_three_doi_pmid_citations_required')
    captures = data.get('captures') or []
    if len(captures) < 2 or len(captures) > 6: raise ValueError('research_capture_count_invalid')
    required = [('firecrawl-research-index', 'search'), ('firecrawl-gov-index', 'search')]
    if [(x.get('provider'), x.get('capability')) for x in captures[:2]] != required:
        raise ValueError('research_discovery_missing')
    for capture in captures:
        for name in ['request', 'response']:
            s = capture[name]; p = Path(s['path'])
            if p.is_symlink() or not p.is_file() or digest(p) != s['sha256']:
                raise ValueError('research_capture_hash_invalid')
        body = json.loads(Path(capture['request']['path']).read_text())
        response = json.loads(Path(capture['response']['path']).read_text())
        execution = body['alexandria'][0]
        if (execution['provider'], execution['capability']) != (capture['provider'], capture['capability']):
            raise ValueError('research_capture_identity_invalid')
        check_response(response, execution)
    return data

def check_response(response, execution):
    if response.get('success') is not True: raise ValueError('research_capture_failed')
    rows = (response.get('data') or {}).get('alexandria') or []
    if len(rows) != 1 or rows[0].get('provider') != execution['provider'] or rows[0].get('capability') != execution['capability']:
        raise ValueError('research_response_identity_invalid')
    if rows[0].get('success') is False or rows[0].get('error') or rows[0].get('data') is None:
        raise ValueError('research_provider_failed')
    return rows[0].get('creditsCost')


def citations(response):
    rows = response['data']['alexandria'][0]['data'].get('results', [])
    result = []
    for paper in rows:
        ids = paper.get('ids', {})
        pmid = next(iter(ids.get('pmid', [])), None)
        doi = next(iter(ids.get('doi', [])), None)
        if not pmid and not doi:
            continue
        result.append({'title': paper['title'], 'pmid': pmid, 'doi': doi,
            'source_url': 'https://pubmed.ncbi.nlm.nih.gov/' + pmid + '/' if pmid else 'https://doi.org/' + doi,
            'abstract': paper.get('abstract'), 'status': 'SOURCE_INSPECTION_REQUIRED_NOT_CLINICAL_APPROVAL'})
    return list({row['pmid'] or row['doi']: row for row in result}.values())

def collect(root, *, target_query, clinical_query, regulatory_query, reads=(), request=request_live):
    root = Path(root).resolve()
    if not all(isinstance(x, str) and x.strip() for x in [target_query, clinical_query, regulatory_query]):
        raise ValueError('research_query_missing')
    if len(reads) > 4: raise ValueError('research_20_credit_ceiling')
    calls = [
        {'provider': 'firecrawl-research-index', 'capability': 'search', 'options': {'query': clinical_query, 'k': 5}},
        {'provider': 'firecrawl-gov-index', 'capability': 'search', 'options': {'query': regulatory_query, 'top_k': 5, 'full_content': True}},
        *[read_request(row) for row in reads],
    ]
    manifest = root / 'research-manifest.json'
    bindings = {'target_query': target_query, 'clinical_query': clinical_query, 'regulatory_query': regulatory_query, 'requested_reads': list(reads)}
    if manifest.exists():
        result = validate_manifest(manifest, target_query)
        if any(result.get(k) != v for k, v in bindings.items()): raise ValueError('research_resume_request_drift')
        return result
    captures = []
    references = []
    for index, execution in enumerate(calls):
        body = {'alexandria': [execution]}
        identity = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
        req = root / f'{index:02d}-{identity}-request.json'
        out = root / f'{index:02d}-{identity}-response.json'
        if req.exists() and not out.exists(): raise ValueError('research_ambiguous_attempt_requires_reconciliation')
        if not req.exists():
            save(req, body)
            response = request(body)
            save(out, response)
        else:
            if json.loads(req.read_text()) != body: raise ValueError('research_request_drift')
            response = json.loads(out.read_text())
        credits = check_response(response, execution)
        if execution['provider'] == 'firecrawl-research-index':
            references = citations(response)
            if len(references) < 3:
                raise ValueError('research_three_doi_pmid_citations_required')
        captures.append({'provider': execution['provider'], 'capability': execution['capability'],
            'request': spec(req), 'response': spec(out), 'credits_reported': credits})
    charges = [x['credits_reported'] for x in captures]
    result = {'schema': SCHEMA, **bindings, 'captured_at_utc': datetime.now(timezone.utc).isoformat(),
        'captures': captures, 'citations': references, 'credits_ceiling': 20,
        'credits_reported': sum(charges) if all(type(x) in (int, float) for x in charges) else None,
        'status': 'DISCOVERY_CAPTURED_NOT_CLINICAL_APPROVAL',
        'claim_policy': 'Original source, product, population, duration, endpoint, study design, limitations and exact quote required; preprints labeled; no extrapolation to compounded products or provider outcomes.'}
    save(manifest, result)
    return validate_manifest(manifest, target_query)

def packet_research_valid(packet, target_query):
    """Mechanical custody only; Astra must reject false classification/applicability."""
    def strings(value):
        if isinstance(value, str): yield value
        elif isinstance(value, dict):
            for item in value.values(): yield from strings(item)
        elif isinstance(value, list):
            for item in value: yield from strings(item)
    try:
        packet = Path(packet)
        ledger = json.loads((packet / '01-evidence-ledger.json').read_text())
        mapping = json.loads((packet / '05-claim-source-map.json').read_text())
        binding = ledger['alexandria_research']
        p = Path(binding['path'])
        if p.is_symlink() or digest(p) != binding['sha256']: return False
        research = validate_manifest(p, target_query)
        if research['schema'] == SCHEMA and ledger.get('research_citations') != research['citations']: return False
        rows = ledger['research_claim_ledger']
        ids = [row['claim_id'] for row in rows]
        expected = [row['claim_id'] for row in mapping['claims']]
        if not ids or len(set(ids)) != len(ids) or set(ids) != set(expected): return False
        for row in rows:
            if row['kind'] not in {'clinical', 'regulatory', 'provider', 'editorial'}: return False
            if row['kind'] in {'provider', 'editorial'}:
                if not str(row.get('applicability_note') or '').strip(): return False
                continue  # Existing quote/source-map checks still govern these claims.
            required = ['source_url', 'quote', 'limitations']
            required += ['product', 'population', 'duration', 'endpoint', 'study_design'] if row['kind'] == 'clinical' else ['jurisdiction', 'effective_as_of', 'applicability']
            if any(not isinstance(row.get(k), str) or not row[k].strip() for k in required): return False
            capture = row['capture']; source = Path(capture['path'])
            if source.is_symlink() or digest(source) != capture['sha256']: return False
            raw = source.read_text()
            try: texts = strings(json.loads(raw))
            except json.JSONDecodeError: texts = [raw]
            if not any(row['quote'] in text for text in texts): return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--target-query', required=True)
    parser.add_argument('--clinical-query', required=True)
    parser.add_argument('--regulatory-query', required=True)
    for kind in READS: parser.add_argument('--' + kind, action='append', default=[])
    args = parser.parse_args()
    result = collect(args.output, target_query=args.target_query, clinical_query=args.clinical_query,
        regulatory_query=args.regulatory_query, reads=[{'kind': kind, 'id': value} for kind in READS for value in getattr(args, kind)])
    print(json.dumps({'status': result['status'], 'manifest': spec(args.output / 'research-manifest.json'), 'credits_reported': result['credits_reported']}))

if __name__ == '__main__': main()
