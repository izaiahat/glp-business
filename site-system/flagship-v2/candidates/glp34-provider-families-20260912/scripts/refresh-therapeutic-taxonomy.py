#!/usr/bin/env python3
"""Live Firecrawl re-verification of reviewed taxonomy assertions; never infer tags.

Default: all explicit reviewed policies. --program selects one. Captures and
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
# GLP-80: the independently accepted PR18 negative-claim correction, not a
# generic tag editor. Full target-projection preimages bind the installed
# predecessor; a changed projection requires review rather than inferred tags.
CARE_BARE_CORRECTION = {
    'program_id': 'care-bare-rx-glp1-review',
    'reviewed_head': '18dd240a9caaa341d44290c60dd2e604779f3f68',
    'merged_head': '7455455d8e000a49aad192cdc91138de1be071e7',
    'brand_name_tags_before': ['mounjaro'],
    'brand_name_tags_after': [],
    'positive_binding_sha256': '68baad60813dacfd9096bd4c5adf49da9fd6d6432d0d2405733af032a890793d',
    'preimages': {
        'taxonomy_row': 'd5ae86f33e413f2962f590694ca3765947649fe24f02e33feeca908101e537f0',
        'dynamic_profile': '8b11a7b47d7024add49beff9bc5436ade953a7b3c8f2352d69f33621cb0a8c75',
        'research_dynamic_profile': 'fe343435204151bd664d9f76fc0aaaa5f32384e6992dad1d02672ac92a6b9006',
    },
}
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
        'quotes': {'molecule_tags': {'semaglutide': 'Compounded semaglutide injection is $139 for your first month', 'tirzepatide': 'compounded tirzepatide injection is $179 for your first month'}},
        'finding': 'Retained first-party capture received 2026-10-02T12:01:28.411212Z adds the word injection to both existing molecule excerpts. No additional tags, price renewal, or RxSpan inference.'
    },
    'rxspan-md-telehealth-program-review': {
        'url': 'https://www.rxspanmd.com/',
        'linked_url': 'https://www.rxspanmd.com/category/weight-loss',
        'intermediate_url': 'https://www.rxspanmd.com/main-weight-loss',
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
# Explicit retained-source policy only: never derive assertions from tag names.
# These additions do not refresh dates. Live capture/support and all write gates
# below remain mandatory. Missing policy file refuses rather than hiding rows.
ADDITIONAL_POLICIES = json.loads(Path(__file__).with_name('therapeutic-taxonomy-refresh-policies.json').read_text())
if set(ADDITIONAL_POLICIES) & set(POLICIES):
    raise ValueError('duplicate_reviewed_policy')
POLICIES.update(ADDITIONAL_POLICIES)


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


def correction_projections(registry):
    pid = CARE_BARE_CORRECTION['program_id']
    programs = [p for p in registry['programs'] if p['program_id'] == pid]
    if len(programs) != 1:
        raise ValueError('reviewed_correction_program_identity_changed')
    return {
        'taxonomy_row': registry['therapeutic_taxonomy']['programs'][pid],
        'dynamic_profile': registry['provider_dynamic_profiles']['profiles'][pid],
        'research_dynamic_profile': programs[0]['research_dynamic_profile'],
    }


def prepare_reviewed_correction(registry, pid):
    """Return a capture-only view; do not mutate evidence or dates on staging."""
    row = registry['therapeutic_taxonomy']['programs'][pid]
    if pid != CARE_BARE_CORRECTION['program_id']:
        return copy.deepcopy(row), None
    projections = correction_projections(registry)
    tags = [row['brand_name_tags'], projections['dynamic_profile']['taxonomy']['brand_name_tags'],
            projections['research_dynamic_profile']['taxonomy']['brand_name_tags']]
    if tags == [[], [], []]:
        # Already-adopted successors use ordinary exact-policy re-verification.
        if any(b.get('evidence_sha256') == CARE_BARE_CORRECTION['positive_binding_sha256']
               for b in row['evidence_bindings']):
            raise ValueError('reviewed_correction_positive_binding_remains')
        return copy.deepcopy(row), None
    for name, projection in projections.items():
        digest = sha(json.dumps(projection, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
        if digest != CARE_BARE_CORRECTION['preimages'][name]:
            raise ValueError('reviewed_correction_preimage_changed:' + name)
    staged = copy.deepcopy(row)
    staged['brand_name_tags'] = []
    staged['evidence_bindings'] = [b for b in staged['evidence_bindings']
                                 if b['evidence_sha256'] != CARE_BARE_CORRECTION['positive_binding_sha256']]
    return staged, copy.deepcopy(CARE_BARE_CORRECTION)


def apply_reviewed_correction(registry, pid, receipt):
    """Guard the capture-to-commit interval before changing the three projections."""
    if pid != CARE_BARE_CORRECTION['program_id'] or receipt != CARE_BARE_CORRECTION:
        raise ValueError('reviewed_correction_receipt_changed')
    row, expected_receipt = prepare_reviewed_correction(registry, pid)
    if expected_receipt != receipt:
        raise ValueError('reviewed_correction_preimage_changed:already_adopted')
    projections = correction_projections(registry)
    projections['taxonomy_row']['brand_name_tags'] = row['brand_name_tags']
    projections['taxonomy_row']['evidence_bindings'] = row['evidence_bindings']
    projections['dynamic_profile']['taxonomy']['brand_name_tags'] = []
    projections['research_dynamic_profile']['taxonomy']['brand_name_tags'] = []
    # Preserve mandatory empty-brand assessment coverage and historical prices.
    # Native digest/count derivation follows, without renewing profile clocks.


def derive_reviewed_metadata(registry):
    """Derive only Care's profile digest and facet counts, at unchanged clocks."""
    code = '''import fs from 'node:fs';
import {normalizedProfileSha256,deriveDynamicFacetCounts} from './src/provider-dynamic-profiles.mjs';
const registry=JSON.parse(fs.readFileSync(0,'utf8'));
const document=registry.provider_dynamic_profiles;
const profile=document.profiles['care-bare-rx-glp1-review'];
profile.normalized_sha256=normalizedProfileSha256(profile);
document.expected_counts=deriveDynamicFacetCounts(document.profiles);
process.stdout.write(JSON.stringify(registry));'''
    result = subprocess.run(['node', '--input-type=module', '-e', code], cwd=ROOT,
                            input=json.dumps(registry), text=True, capture_output=True)
    if result.returncode:
        raise ValueError('reviewed_correction_native_metadata_refused:' + result.stderr[-2400:])
    return json.loads(result.stdout)


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


def verify_sources(pid, row, policy, run):
    """Reverify explicit per-page assertions, never combine unrelated page text."""
    sources = policy['sources']
    expected = {field: [] for field in FIELDS}
    for source in sources:
        if source['url'] not in row['source_urls']:
            raise ValueError('reviewed_source_not_in_program')
        for field in FIELDS:
            expected[field].extend(source['quotes'].get(field, {}))
    for field in FIELDS:
        if (set(row[field]) != set(expected[field]) or
                len(expected[field]) != len(set(expected[field]))):
            raise ValueError(f'tag_set_changed:{field}:review_required')
    captures, assertions = [], {field: {} for field in FIELDS}
    for index, source in enumerate(sources):
        path = run / f'{pid}-source-{index}.json'
        receipt = capture(source['url'], path)
        fragment = {field: list(source['quotes'].get(field, {})) for field in FIELDS}
        found = support(fragment, source, receipt['response']['data']['markdown'])
        for field in FIELDS:
            assertions[field].update(found[field])
        captures.append({'source_url': source['url'], 'verified_as_of': receipt['received_at'][:10],
                         'captured_at': receipt['received_at'], 'capture_sha256': sha(encoded(receipt)),
                         'capture_path': str(path.relative_to(BUSINESS)), 'assertions': found})
    if not captures:
        raise ValueError('no_reviewed_sources')
    return {'program_id': pid, 'sources': captures, 'assertions': assertions,
            'verified_as_of': min(c['verified_as_of'] for c in captures),
            'finding': policy.get('finding'), 'previous_row': row,
            'tags_unchanged': {field: row[field] for field in FIELDS},
            **({'claim_qualification': row['claim_qualification']}
               if row.get('claim_qualification') else {})}


def prepare_qualified_source_row(row, pid):
    """Stage only Eden's exact reviewed closure; retain clocks/tags/bindings."""
    staged = copy.deepcopy(row)
    if pid != 'eden-xentraffic-offer-3051':
        return staged
    policy = POLICIES[pid]
    expected = sorted(source['url'] for source in policy['sources'])
    if row['source_urls'] not in (policy['previous_source_urls'], expected):
        raise ValueError('qualified_source_membership_changed')
    qualification = policy['claim_qualification']
    if 'claim_qualification' in row and row['claim_qualification'] != qualification:
        raise ValueError('qualified_source_note_changed')
    for field in FIELDS:
        tags = sorted(tag for source in policy['sources'] for tag in source['quotes'].get(field, {}))
        if row[field] != tags:
            raise ValueError('qualified_source_tags_changed:' + field)
    staged['source_urls'] = expected
    staged['claim_qualification'] = qualification
    return staged


def apply_qualified_source_row(row, pid, manifest):
    """Consume the successful four-source manifest inside the existing CAS writer."""
    if pid != 'eden-xentraffic-offer-3051':
        return
    staged = prepare_qualified_source_row(row, pid)
    if (manifest.get('claim_qualification') != staged['claim_qualification'] or
            sorted(s['source_url'] for s in manifest.get('sources', [])) != staged['source_urls'] or
            manifest.get('tags_unchanged') != {field: row[field] for field in FIELDS}):
        raise ValueError('qualified_source_manifest_changed')
    row['source_urls'] = staged['source_urls']
    row['claim_qualification'] = staged['claim_qualification']


def validate_qualified_renderer(registry, ids):
    """Refuse source adoption until the paired authenticated consumer is present."""
    if not any(registry['therapeutic_taxonomy']['programs'][pid].get('claim_qualification') for pid in ids):
        return
    code = '''import fs from 'node:fs';
import {taxonomyDigestBasis} from './src/therapeutic-taxonomy.mjs';
const x=JSON.parse(fs.readFileSync(0,'utf8'));
for(const id of x.ids) {
 const row=x.registry.therapeutic_taxonomy.programs[id];
 if(row.claim_qualification && taxonomyDigestBasis(row).claim_qualification!==row.claim_qualification) throw new Error('qualified_source_renderer_not_installed');
}'''
    result = subprocess.run(['node', '--input-type=module', '-e', code], cwd=ROOT,
        input=json.dumps({'registry':registry,'ids':ids}), text=True, capture_output=True)
    if result.returncode:
        raise ValueError('qualified_source_renderer_refused:' + result.stderr[-2400:])


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
            row, correction = prepare_reviewed_correction(before, pid)
            row = prepare_qualified_source_row(row, pid)
            if policy.get('review_required'):
                raise ValueError('policy_review_required:' + policy['review_required'])
            if 'sources' in policy:
                manifest = verify_sources(pid, row, policy, run)
            else:
                if policy['url'] not in row['source_urls']:
                    raise ValueError('reviewed_source_not_in_program')
                primary_path = run / (pid + '-primary.json')
                primary = capture(policy['url'], primary_path)
                navigation = [(policy['url'], primary_path, primary)]
                selected, url = primary, policy['url']
                if policy.get('intermediate_url'):
                    middle = policy['intermediate_url']
                    if middle not in primary['response']['data'].get('links', []):
                        raise ValueError('intermediate_no_longer_linked_by_program')
                    middle_path = run / (pid + '-intermediate.json')
                    selected = capture(middle, middle_path)
                    navigation.append((middle, middle_path, selected))
                if policy.get('linked_url'):
                    url = policy['linked_url']
                    if url not in selected['response']['data'].get('links', []):
                        raise ValueError('supplement_no_longer_linked_by_program')
                    selected = capture(url, run / (pid + '-linked.json'))
                assertions = support(row, policy, selected['response']['data']['markdown'])
                selected_path = run / (pid + ('-linked.json' if policy.get('linked_url') else '-primary.json'))
                manifest = {'program_id': pid, 'source_url': url, 'verified_as_of': selected['received_at'][:10],
                    'captured_at': selected['received_at'], 'assertions': assertions,
                    'capture_sha256': sha(selected_path.read_bytes()), 'capture_path': str(selected_path.relative_to(BUSINESS)),
                    'finding': policy.get('finding'), 'previous_row': row,
                    'tags_unchanged': {field: row[field] for field in FIELDS}}
                if policy.get('linked_url'):
                    # Seal every outgoing edge inside the authority, not at intake.
                    targets = [entry[0] for entry in navigation[1:]] + [url]
                    manifest['navigation_edges'] = [
                        {'source_url': edge_url, 'linked_url': target,
                         'capture_path': str(edge_path.relative_to(BUSINESS)),
                         'capture_sha256': sha(edge_path.read_bytes()),
                         'started_at': receipt['started_at'], 'captured_at': receipt['received_at'],
                         'verified_as_of': receipt['received_at'][:10]}
                        for (edge_url, edge_path, receipt), target in zip(navigation, targets)]
            # CAS uses the actual pre-capture row, not the corrected view.
            # The immutable receipt records the sole reviewed tag-set exception.
            manifest['previous_row'] = before['therapeutic_taxonomy']['programs'][pid]
            if correction is not None:
                manifest['reviewed_correction'] = correction
                manifest['verified_tags'] = manifest.pop('tags_unchanged')
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
            if manifest.get('reviewed_correction') is not None:
                apply_reviewed_correction(candidate, pid, manifest['reviewed_correction'])
            authority = 'taxonomy_refresh_' + pid.replace('-', '_') + '_' + run.name
            candidate['therapeutic_taxonomy']['authorities'][authority] = {
                'path': str(manifest_path.relative_to(BUSINESS)), 'sha256': sha(manifest_path.read_bytes())}
            sources = manifest.get('sources', [manifest])
            apply_qualified_source_row(row, pid, manifest)
            row['source_urls'] = sorted(set(row['source_urls'] + [s['source_url'] for s in sources]))
            row['verified_as_of'] = manifest['verified_as_of']
            # The prior active bindings remain in the immutable verification
            # receipt, not in the current evidence set with an expired clock.
            row['evidence_bindings'] = [{'authority': authority,
                'authority_sha256': sha(manifest_path.read_bytes()), 'source_url': s['source_url'],
                'verified_as_of': s['verified_as_of'], 'evidence_sha256': s['capture_sha256']} for s in sources]
        if any(manifest.get('reviewed_correction') is not None for manifest, _ in proposals.values()):
            candidate = derive_reviewed_metadata(candidate)
        validate_qualified_renderer(candidate, list(proposals))
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
