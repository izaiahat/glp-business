#!/usr/bin/env python3
"""Live Firecrawl re-verification of reviewed taxonomy assertions; never infer tags.

Default: the reviewed GLP-38 programs. --program selects one. Captures and
findings survive refusals. Only a complete validated registry is atomically saved.
"""
import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BUSINESS = ROOT.parents[3]
sys.path.insert(0, str(BUSINESS / 'scripts'))
from affiliate_direct_evidence_capture import _firecrawl_key

REPORTS = BUSINESS / 'reports/ops/glp38-20260915/taxonomy-refresh'
REGISTRY = ROOT / 'data/program-registry.json'
FIELDS = ('molecule_tags', 'brand_name_tags', 'delivery_form_tags', 'treatment_type_tags')
# Exact affirmative excerpts reviewed against each program's own live pages.
# Empty fields mean no assertion, not proof of absence. Added tags need review.
POLICIES = {
    'bodybuilding-com-glp-1-longevity-review': {
        'url': 'https://bbhealthplus.com/',
        'quotes': {'molecule_tags': {'semaglutide': 'Compounded Semaglutide', 'tirzepatide': 'Compounded Tirzepatide'}},
    },
    'coreage-rx-glp-1-1632': {
        'url': 'https://get.coreagerx.com/home35/',
        'quotes': {'molecule_tags': {'semaglutide': 'START SEMAGLUTIDE NOW', 'tirzepatide': 'START TIRZEPATIDE NOW'},
                   'treatment_type_tags': {'compounded': 'Physicians may prescribe compounded medications as needed to meet patient requirements or drug shortages.'}},
    },
    'pallas-health-review': {
        'url': 'https://www.pallashealth.co/',
        'quotes': {'molecule_tags': {'semaglutide': 'Compounded semaglutide is $139 for your first month', 'tirzepatide': 'compounded tirzepatide is $179 for your first month'}},
    },
    'rxspan-md-telehealth-program-review': {
        'url': 'https://www.rxspanmd.com/',
        'linked_url': 'https://www.rxspanmd.com/category/weight-loss',
        'finding': 'Homepage no longer names the retained drugs. Its linked first-party weight-loss catalog supplies the current medication assertions; old homepage bindings remain historical.',
        'quotes': {'molecule_tags': {'semaglutide': 'Semaglutide Injection', 'tirzepatide': 'Tirzepatide Injection'},
                   'brand_name_tags': {'wegovy': 'Wegovy®', 'zepbound': 'Zepbound®'}},
    },
    'skinnyrx-1-glp-weight-loss-provider-review': {
        'url': 'https://skinnyrx.com/',
        'quotes': {'molecule_tags': {'semaglutide': 'Compounded Injectable Semaglutide', 'tirzepatide': 'Compounded Injectable Tirzepatide'},
                   'delivery_form_tags': {'injectable': 'Once-weekly injection with semaglutide'}},
    },
    # Fable 2026-09-18 (GLP-30 closure): the five programs above were the only ones this registered refresher admitted, so
    # dudemeds-review and wellmedr-review expired at the 31-day window (2026-09-18 ~00:00Z) with no registered path to
    # refresh them (bug class: frozen value required fresh). Policies below were reviewed against live captures in
    # reports/ops/glp38-20260915/taxonomy-refresh/probe-20260918/.
    'dudemeds-review': {
        'url': 'https://www.dudemeds.com/weight-loss',
        'quotes': {'molecule_tags': {'semaglutide': 'Semaglutide is an FDA-approved medication shown to significantly aid weight loss', 'tirzepatide': 'Tirzepatide is an FDA-approved medication'},
                   'delivery_form_tags': {'injectable': 'Oral and Injectable options available.', 'oral': 'Oral and Injectable options available.'}},
    },
    'wellmedr-review': {
        'url': 'https://www.wellmedr.com/pages/weight-loss',
        'finding': 'The get-started page carries the injectable/compounded/Zepbound price assertions but no oral wording; the weight-loss page carries all five retained assertions (oral tablets, injections, compounded GLP-1, Zepbound, both molecules).',
        'quotes': {'molecule_tags': {'semaglutide': 'medications like Tirzepatide and Semaglutide', 'tirzepatide': 'medications like Tirzepatide and Semaglutide'},
                   'brand_name_tags': {'zepbound': 'Zepbound'},
                   'delivery_form_tags': {'injectable': 'Whether you prefer injections or oral tablets', 'oral': 'Whether you prefer injections or oral tablets'},
                   'treatment_type_tags': {'compounded': 'Compounded GLP-1'}},
    },
    'nu-image-medical-review': {
        'url': 'https://nuimagemedical.com/weight-loss-medications',
        'quotes': {'molecule_tags': {'semaglutide': 'Semaglutide Injection', 'tirzepatide': 'Tirzepatide Injection'},
                   'delivery_form_tags': {'injectable': 'Tirzepatide Injection'}},
    },
    'synergy-rx-review': {
        'url': 'https://www.synergyrx.co/',
        'quotes': {'molecule_tags': {'semaglutide': 'Our licensed healthcare providers may prescribe semaglutide or tirzepatide',
                                    'tirzepatide': 'Our licensed healthcare providers may prescribe semaglutide or tirzepatide'},
                   'brand_name_tags': {'mounjaro': 'Mounjaro®', 'wegovy': 'Wegovy®'},
                   'treatment_type_tags': {'brand_name': 'FDA-approved brand name medications',
                                           'compounded': 'compounded GLP-1 formulations'}},
    },
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode()


def normalized(text):
    return re.sub(r'\s+', ' ', str(text).replace('*', '').replace('#', '')).strip().casefold()


def support(row, policy, text):
    """Require every retained tag's reviewed assertion; do not silently edit tags."""
    found = {}
    plain = normalized(text)
    for field in FIELDS:
        quotes = policy['quotes'].get(field, {})
        if set(row[field]) != set(quotes):
            raise ValueError(f'tag_set_changed:{field}:review_required')
        found[field] = {}
        for tag, quote in quotes.items():
            needle = normalized(quote)
            pos = plain.find(needle)
            if pos < 0:
                raise ValueError(f'assertion_no_longer_supported:{field}:{tag}')
            context = plain[max(0, pos - 80):pos + len(needle) + 100]
            if re.search(r"\b(unavailable|discontinued|not (?:available|offered|prescribed|provided)|(?:do(?:es)? not|don['’]t|doesn['’]t|never|no longer)\s+(?:offer|prescrib|provid|supply|sell|carry|avail)\w*)\b", context):
                raise ValueError(f'assertion_qualified_unavailable:{field}:{tag}')
            found[field][tag] = {'quote': quote, 'context': context}
    return found


def check_previous_row(current, previous):
    if current != previous:
        raise ValueError('program_changed_during_capture')


def capture(url, dest):
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    request_body = {'url': url, 'formats': ['markdown', 'links'], 'onlyMainContent': False, 'maxAge': 0}
    request = urllib.request.Request('https://api.firecrawl.dev/v2/scrape', data=json.dumps(request_body).encode(),
        headers={'Authorization': 'Bearer ' + _firecrawl_key(), 'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
    received = dt.datetime.now(dt.timezone.utc).isoformat()
    document = json.loads(raw)
    receipt = {'request': request_body, 'started_at': started, 'received_at': received, 'response': document}
    dest.write_bytes(encoded(receipt))
    data = document.get('data') or {}
    meta = data.get('metadata') or {}
    if not document.get('success') or meta.get('statusCode') != 200 or not data.get('markdown'):
        raise ValueError('capture_failed_or_empty')
    if str(meta.get('cacheState', '')).lower() in ('hit', 'cached'):
        raise ValueError('cached_response_refused')
    for key in ('sourceURL', 'url'):
        if meta.get(key) and urlparse(meta[key]).hostname != urlparse(url).hostname:
            raise ValueError('source_host_changed')
    if meta.get('cachedAt') and dt.datetime.fromisoformat(meta['cachedAt'].replace('Z', '+00:00')) < dt.datetime.fromisoformat(started):
        raise ValueError('stale_cache_refused')
    return receipt


def validate(registry, ids):
    code = '''import fs from 'node:fs';
import {normalizedTaxonomySha256,reconcileTherapeuticTaxonomyWireIn} from './src/therapeutic-taxonomy.mjs';
const x=JSON.parse(fs.readFileSync(0,'utf8'));
for(const id of x.ids) x.registry.therapeutic_taxonomy.programs[id].normalized_sha256=normalizedTaxonomySha256(x.registry.therapeutic_taxonomy.programs[id]);
const money=JSON.parse(fs.readFileSync('data/money-page-specs.json','utf8'));
const quiz=JSON.parse(fs.readFileSync('data/quiz-config.json','utf8'));
reconcileTherapeuticTaxonomyWireIn({registry:x.registry,profileId:'glp-clinic-scout',pages:money.pages,quizConfig:quiz});
process.stdout.write(JSON.stringify(x.registry,null,2)+'\\n');'''
    result = subprocess.run(['node', '--input-type=module', '-e', code], cwd=ROOT,
        input=json.dumps({'registry': registry, 'ids': ids}), text=True, capture_output=True)
    if result.returncode:
        raise ValueError('validateTherapeuticTaxonomy_refused:' + result.stderr[-2400:])
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--program', choices=sorted(POLICIES), action='append')
    args = parser.parse_args()
    ids = args.program or list(POLICIES)
    run = REPORTS / ('run-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    run.mkdir(parents=True)
    before = json.loads(REGISTRY.read_text())
    proposals, findings = {}, []
    for pid in ids:
        policy = POLICIES[pid]
        try:
            row = before['therapeutic_taxonomy']['programs'][pid]
            if policy['url'] not in row['source_urls']:
                raise ValueError('reviewed_source_not_in_program')
            primary = capture(policy['url'], run / (pid + '-primary.json'))
            selected, url = primary, policy['url']
            if policy.get('linked_url'):
                url = policy['linked_url']
                if url not in primary['response']['data'].get('links', []):
                    raise ValueError('supplement_no_longer_linked_by_program')
                selected = capture(url, run / (pid + '-linked.json'))
            assertions = support(row, policy, selected['response']['data']['markdown'])
            manifest = {'program_id': pid, 'source_url': url, 'verified_as_of': selected['received_at'][:10],
                'captured_at': selected['received_at'], 'assertions': assertions,
                'capture_sha256': sha(encoded(selected)), 'capture_path': str((run / (pid + ('-linked.json' if policy.get('linked_url') else '-primary.json'))).relative_to(BUSINESS)),
                'finding': policy.get('finding'), 'previous_row': row,
                'tags_unchanged': {field: row[field] for field in FIELDS}}
            manifest_path = run / (pid + '-verification.json')
            manifest_path.write_bytes(encoded(manifest))
            proposals[pid] = (manifest, manifest_path)
            findings.append({'program_id': pid, 'status': 'SUPPORTED', 'finding': policy.get('finding')})
        except Exception as exc:
            findings.append({'program_id': pid, 'status': 'REVIEW_REQUIRED', 'condition': str(exc)})
    result = {'producer': str(Path(__file__)), 'requested': ids, 'findings': findings, 'written': []}
    lock = BUSINESS / 'scripts/candidate_build_lock.py'
    acquired = False
    try:
        if not proposals:
            raise ValueError('no_supported_programs')
        acquired_run = subprocess.run([sys.executable, str(lock), 'acquire', '--seat', 'glp-design', '--reason', 'taxonomy refresh'], capture_output=True, text=True)
        result['lock_acquire'] = {'exit_code': acquired_run.returncode, 'output': acquired_run.stdout}
        if acquired_run.returncode:
            raise ValueError('candidate_lock_unavailable')
        # The fleet lock treats a second same-seat process as ALREADY_YOURS.
        # This scheduled writer must never borrow Design's active edit/build lock.
        if json.loads(acquired_run.stdout.splitlines()[-1])['status'] != 'ACQUIRED':
            raise ValueError('candidate_lock_already_held_by_design')
        acquired = True
        original = REGISTRY.read_bytes()
        current = json.loads(original)
        candidate = copy.deepcopy(current)
        for pid, (manifest, manifest_path) in proposals.items():
            row = candidate['therapeutic_taxonomy']['programs'][pid]
            check_previous_row(row, manifest['previous_row'])
            authority = 'taxonomy_refresh_' + pid.replace('-', '_') + '_' + run.name
            candidate['therapeutic_taxonomy']['authorities'][authority] = {
                'path': str(manifest_path.relative_to(BUSINESS)), 'sha256': sha(manifest_path.read_bytes())}
            row['source_urls'] = sorted(set(row['source_urls'] + [manifest['source_url']]))
            row['verified_as_of'] = manifest['verified_as_of']
            # The prior active bindings remain in the immutable verification
            # receipt, not in the current evidence set with an expired clock.
            row['evidence_bindings'] = [{'authority': authority,
                'authority_sha256': sha(manifest_path.read_bytes()), 'source_url': manifest['source_url'],
                'verified_as_of': manifest['verified_as_of'], 'evidence_sha256': manifest['capture_sha256']}]
        candidate = validate(candidate, list(proposals))
        if REGISTRY.read_bytes() != original:
            raise ValueError('registry_changed_during_validation')
        temp = REGISTRY.with_name(REGISTRY.name + '.taxonomy-refresh.tmp')
        temp.write_bytes(encoded(candidate))
        os.replace(temp, REGISTRY)
        readback = json.loads(REGISTRY.read_text())
        for pid, (manifest, _) in proposals.items():
            row = readback['therapeutic_taxonomy']['programs'][pid]
            assert row == candidate['therapeutic_taxonomy']['programs'][pid], 'exact_row_readback_failed'
            result['written'].append({'program_id': pid, 'verified_as_of': row['verified_as_of'], 'normalized_sha256': row['normalized_sha256']})
        result['status'] = 'PASS' if len(proposals) == len(ids) else 'REVIEW_REQUIRED'
    except Exception as exc:
        result['status'] = 'BLOCKED'
        result['condition'] = str(exc)
    finally:
        if acquired:
            released = subprocess.run([sys.executable, str(lock), 'release', '--seat', 'glp-design'], capture_output=True, text=True)
            result['lock_release'] = {'exit_code': released.returncode, 'output': released.stdout}
        (run / 'RESULT.json').write_bytes(encoded(result))
        print(json.dumps({'status': result['status'], 'receipt': str(run / 'RESULT.json'), 'written': result['written'], 'findings': findings, 'condition': result.get('condition')}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
