"""Offline tests: synthetic captures/registry only; no credentials or live writes."""
import ast
import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'site-system/flagship-v2/candidates/glp34-provider-families-20260912/scripts/refresh-therapeutic-taxonomy.py'

@pytest.fixture
def producer(monkeypatch):
    stub = types.ModuleType('affiliate_direct_evidence_capture')
    def forbidden(*a, **k):
        raise AssertionError('live effect forbidden')
    stub._firecrawl_key = forbidden
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    spec = importlib.util.spec_from_file_location('taxonomy_refresh_test', SOURCE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(mod.subprocess, 'run', forbidden)
    return mod

def test_breeze_selected_with_exact_retained_tags(producer):
    assert 'breeze-meds-review' in producer.POLICIES
    p = producer.POLICIES['breeze-meds-review']
    assert p['url'] == 'https://www.breezemeds.com/'
    assert {f: set(p['quotes'].get(f, {})) for f in producer.FIELDS} == {
        'molecule_tags': {'semaglutide', 'tirzepatide'},
        'brand_name_tags': set(), 'delivery_form_tags': {'oral'},
        'treatment_type_tags': set()}


def policy_row(producer, pid):
    p = producer.POLICIES[pid]
    sources = p.get('sources', [p])
    return {**{f: sorted({tag for s in sources for tag in s.get('quotes', {}).get(f, {})})
               for f in producer.FIELDS},
            'source_urls': [s['url'] for s in sources],
            'verified_as_of': '2026-09-01', 'normalized_sha256': 'fixture-old',
            'evidence_bindings': [{'fixture': 'old'}]}


def run_fixture(producer, monkeypatch, tmp_path, ids, missing=(), linked=False, default=False, markdown_overrides=None):
    """Execute maintained main with only external effects replaced by fixtures."""
    monkeypatch.setattr(producer, 'BUSINESS', tmp_path)
    monkeypatch.setattr(producer, 'REPORTS', tmp_path / 'receipts')
    registry = tmp_path / 'fixture-registry.json'
    monkeypatch.setattr(producer, 'REGISTRY', registry)
    rows = {pid: policy_row(producer, pid) for pid in ids
            if not producer.POLICIES[pid].get('review_required')}
    for pid in set(ids) - rows.keys():
        rows[pid] = copy.deepcopy(SNAPSHOT['programs'][pid])
    before = {'therapeutic_taxonomy': {'programs': rows, 'authorities': {}}}
    registry.write_bytes(producer.encoded(before))
    captures = []
    def fake_capture(url, dest):
        captures.append(url)
        parts = []
        for pid in ids:
            p = producer.POLICIES[pid]
            for s in p.get('sources', [p]):
                if url in (s.get('url'), s.get('linked_url')):
                    parts.extend(q for f, tags in s.get('quotes', {}).items()
                                 for tag, q in tags.items() if (pid, f, tag) not in missing)
        receipt = {'received_at': '2026-09-26T12:00:00+00:00',
                   'response': {'data': {'markdown': '\n' + ('\n' + '.' * 200 + '\n').join(parts), 'links': []}}}
        if markdown_overrides and url in markdown_overrides:
            receipt['response']['data']['markdown'] = markdown_overrides[url]
        if linked:
            receipt['response']['data']['links'] = [p['linked_url'] for p in producer.POLICIES.values() if p.get('linked_url')]
        dest.write_bytes(producer.encoded(receipt))
        return receipt
    monkeypatch.setattr(producer, 'capture', fake_capture)
    def validate(candidate, selected):
        for pid in selected:
            candidate['therapeutic_taxonomy']['programs'][pid]['normalized_sha256'] = 'fixture-new'
        return candidate
    monkeypatch.setattr(producer, 'validate', validate)
    def lock(args, **kwargs):
        assert args[2] in ('acquire', 'release')
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({'status': 'ACQUIRED'}))
    monkeypatch.setattr(producer.subprocess, 'run', lock)
    monkeypatch.setattr(sys, 'argv', ['refresh'] + ([] if default else [a for pid in ids for a in ['--program', pid]]))
    code = producer.main()
    receipt = json.loads(next((tmp_path / 'receipts').glob('run-*/RESULT.json')).read_text())
    return code, receipt, before, json.loads(registry.read_text()), captures


def test_multi_source_success_and_unsupported_breeze_is_review_required(producer, monkeypatch, tmp_path):
    bad, good = 'breeze-meds-review', 'enhance-md'
    code, result, before, after, calls = run_fixture(
        producer, monkeypatch, tmp_path, [bad, good], missing={(bad, 'molecule_tags', 'tirzepatide')})
    assert code == 1
    assert result['status'] == 'REVIEW_REQUIRED'
    assert result['findings'][0]['condition'] == 'assertion_no_longer_supported:molecule_tags:tirzepatide'
    assert [w['program_id'] for w in result['written']] == [good]
    assert after['therapeutic_taxonomy']['programs'][bad] == before['therapeutic_taxonomy']['programs'][bad]
    row = after['therapeutic_taxonomy']['programs'][good]
    assert row['verified_as_of'] == '2026-09-26'
    assert len(row['evidence_bindings']) == 2
    assert {b['source_url'] for b in row['evidence_bindings']} == set(before['therapeutic_taxonomy']['programs'][good]['source_urls'])
    assert len(calls) == 3


def test_explicit_policy_review_never_refreshes_or_fetches(producer, monkeypatch, tmp_path):
    held, good = 'care-bare-rx-glp1-review', 'breeze-meds-review'
    code, result, before, after, calls = run_fixture(producer, monkeypatch, tmp_path, [held, good])
    assert code == 1 and result['status'] == 'REVIEW_REQUIRED'
    assert result['findings'][0]['condition'].startswith('policy_review_required:')
    assert after['therapeutic_taxonomy']['programs'][held] == before['therapeutic_taxonomy']['programs'][held]
    assert calls == ['https://www.breezemeds.com/']


@pytest.mark.parametrize('change', ['missing_tag', 'new_tag', 'source_drift', 'duplicate_tag'])
def test_multisource_policy_drift_refuses_before_fetch(producer, monkeypatch, tmp_path, change):
    pid = 'enhance-md'
    row = policy_row(producer, pid)
    p = copy.deepcopy(producer.POLICIES[pid])
    if change == 'missing_tag':
        del p['sources'][1]['quotes']['molecule_tags']['tirzepatide']
    elif change == 'new_tag':
        row['brand_name_tags'].append('wegovy')
    elif change == 'source_drift':
        row['source_urls'].pop()
    else:
        p['sources'].append(copy.deepcopy(p['sources'][0]))
    def no_fetch(*a):
        pytest.fail('must preflight before capture')
    monkeypatch.setattr(producer, 'capture', no_fetch)
    with pytest.raises(ValueError, match='tag_set_changed|reviewed_source_not_in_program'):
        producer.verify_sources(pid, row, p, tmp_path)


def test_multisource_does_not_borrow_assertion_from_other_page(producer, monkeypatch, tmp_path):
    p = producer.POLICIES['enhance-md']
    def swapped_capture(url, path):
        other = p['sources'][1] if url == p['sources'][0]['url'] else p['sources'][0]
        return {'received_at': '2026-09-26T00:00:00+00:00',
                'response': {'data': {'markdown': ' '.join(q for tags in other['quotes'].values() for q in tags.values())}}}
    monkeypatch.setattr(producer, 'capture', swapped_capture)
    monkeypatch.setattr(producer, 'BUSINESS', tmp_path)
    with pytest.raises(ValueError, match='assertion_no_longer_supported'):
        producer.verify_sources('enhance-md', policy_row(producer, 'enhance-md'), p, tmp_path)


def test_multisource_uses_oldest_actual_capture_date(producer, monkeypatch, tmp_path):
    pid = 'enhance-md'
    p = producer.POLICIES[pid]
    receipts = []
    def capture(url, path):
        index = [s['url'] for s in p['sources']].index(url)
        text = ('\n' + '.' * 200 + '\n').join(q for tags in p['sources'][index]['quotes'].values() for q in tags.values())
        receipt = {'received_at': ['2026-09-25T23:59:59+00:00', '2026-09-26T00:00:01+00:00'][index],
                   'response': {'data': {'markdown': text}}}
        receipts.append(receipt)
        return receipt
    monkeypatch.setattr(producer, 'capture', capture)
    monkeypatch.setattr(producer, 'BUSINESS', tmp_path)
    m = producer.verify_sources(pid, policy_row(producer, pid), p, tmp_path)
    assert m['verified_as_of'] == '2026-09-25'
    assert [s['verified_as_of'] for s in m['sources']] == ['2026-09-25', '2026-09-26']
    assert [s['capture_sha256'] for s in m['sources']] == [producer.sha(producer.encoded(r)) for r in receipts]


@pytest.mark.parametrize('pid,condition', [
    ('pallas-health-review', 'assertion_no_longer_supported:molecule_tags:semaglutide'),
    ('rxspan-md-telehealth-program-review', 'supplement_no_longer_linked_by_program')])
def test_existing_real_refusal_classes_still_review_required(producer, monkeypatch, tmp_path, pid, condition):
    missing = {(pid, 'molecule_tags', 'semaglutide')}
    code, result, before, after, _ = run_fixture(producer, monkeypatch, tmp_path, [pid, 'breeze-meds-review'], missing)
    assert code == 1 and result['status'] == 'REVIEW_REQUIRED'
    assert result['findings'][0]['condition'] == condition
    assert after['therapeutic_taxonomy']['programs'][pid] == before['therapeutic_taxonomy']['programs'][pid]


@pytest.mark.parametrize('negation', ['unavailable', 'discontinued', 'not offered', 'no longer provide'])
def test_original_unavailability_guard_preserved(producer, negation):
    p = producer.POLICIES['breeze-meds-review']
    text = '\n'.join(q for tags in p['quotes'].values() for q in tags.values())
    with pytest.raises(ValueError, match='assertion_qualified_unavailable'):
        producer.support(policy_row(producer, 'breeze-meds-review'), p, negation + ' ' + text)


SNAPSHOT = json.loads((ROOT / 'tests/fixtures/therapeutic-taxonomy-coverage.json').read_text())


def test_all_58_rows_accounted_for_and_all_48_near_expiry_selected(producer):
    rows = SNAPSHOT['programs']
    assert len(rows) == 58
    urgent = {pid for pid, r in rows.items() if
              dt.date.fromisoformat(r['verified_as_of']) + dt.timedelta(days=SNAPSHOT['freshness_max_days']) < dt.date(2026, 10, 10)}
    assert len(urgent) == 48
    assert urgent <= producer.POLICIES.keys()
    assert len(producer.POLICIES) == 57
    assert set(rows) - producer.POLICIES.keys() == {'coivas'}
    for pid, policy in producer.POLICIES.items():
        if policy.get('review_required'):
            assert policy['review_required'].strip()
            continue
        sources = policy.get('sources', [policy])
        assert all(s['url'] in rows[pid]['source_urls'] for s in sources)
        for field in producer.FIELDS:
            tags = [tag for s in sources for tag in s['quotes'].get(field, {})]
            assert sorted(tags) == rows[pid][field], (pid, field)
            assert all(q.strip() for s in sources for q in s['quotes'].get(field, {}).values())


def test_nine_original_policies_and_evidence_rules_are_unchanged(producer):
    old = {pid: producer.POLICIES[pid] for pid, row in SNAPSHOT['programs'].items() if row['previously_covered']}
    assert len(old) == 9
    assert hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest() == SNAPSHOT['original_policies_sha256']
    tree = ast.parse(SOURCE.read_text())
    for name, expected in SNAPSHOT['original_functions_source_sha256'].items():
        n = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        assert hashlib.sha256(ast.get_source_segment(SOURCE.read_text(), n).encode()).hexdigest() == expected


@pytest.mark.parametrize('pid', [pid for pid, row in SNAPSHOT['programs'].items() if row['previously_covered'] and pid != 'rxspan-md-telehealth-program-review'])
def test_previously_covered_rows_keep_their_single_source_binding(producer, monkeypatch, tmp_path, pid):
    code, result, before, after, calls = run_fixture(producer, monkeypatch, tmp_path, [pid])
    assert code == 0 and result['status'] == 'PASS'
    row = after['therapeutic_taxonomy']['programs'][pid]
    assert calls == [producer.POLICIES[pid]['url']]
    assert len(row['evidence_bindings']) == 1
    assert all(row[f] == before['therapeutic_taxonomy']['programs'][pid][f] for f in producer.FIELDS)


def test_default_selection_runs_all_policies_without_dropping_review_rows(producer, monkeypatch, tmp_path):
    ids = list(producer.POLICIES)
    code, result, before, after, _ = run_fixture(producer, monkeypatch, tmp_path, ids, linked=True, default=True)
    assert result['requested'] == ids and len(ids) == 57
    assert code == 1 and result['status'] == 'REVIEW_REQUIRED'
    assert 'breeze-meds-review' in [w['program_id'] for w in result['written']]
    held = {pid for pid, p in producer.POLICIES.items() if p.get('review_required')}
    assert {f['program_id'] for f in result['findings'] if f['status'] == 'REVIEW_REQUIRED'} == held
    for pid in held:
        assert after['therapeutic_taxonomy']['programs'][pid] == before['therapeutic_taxonomy']['programs'][pid]


def test_rxspan_linked_success_binding_stays_on_supplement(producer, monkeypatch, tmp_path):
    pid = 'rxspan-md-telehealth-program-review'
    code, result, _, after, calls = run_fixture(producer, monkeypatch, tmp_path, [pid], linked=True)
    p = producer.POLICIES[pid]
    assert code == 0 and result['status'] == 'PASS'
    assert calls == [p['url'], p['linked_url']]
    assert after['therapeutic_taxonomy']['programs'][pid]['evidence_bindings'][0]['source_url'] == p['linked_url']


# Fixed adversarial excerpts, not derived from the current policy. The first two
# reproduce the independent review: offering text gone, old disclaimers retained.
# Remaining cases were found by the all-new-policy audit. These are synthetic
# negative captures, not assertions about what any live site currently offers.
NON_OFFERING_CAPTURES = {
    'humecare-data-driven-glp-1-weight-loss-system': {
        'https://app.humecare.com/medications-safety': (
            'Compounded semaglutide and compounded tirzepatide are not approved by the FDA.\n\n'
            'Compounded semaglutide is an injectable prescription medicine')},
    'sprout-health': {
        'https://joinsprouthealth.com/shop': (
            'Compounded semaglutide and tirzepatide are not approved by the FDA\n\n'
            'Sprout Health provides access to compounded GLP-1 medications prepared by licensed U.S. pharmacies.')},
    'trimi-glp-1': {
        'https://trytrimi.com/': (
            'Compounded semaglutide and tirzepatide are prepared per individual prescription by state-licensed 503A pharmacies and are not FDA-approved as drugs.\n\n'
            'DosingOnce-weekly injection')},
    'live-vital-launch': {
        'https://www.livevital.io/treatments/semaglutide': 'Semaglutide is a once-weekly injectable GLP-1 receptor agonist.',
        'https://www.livevital.io/treatments/tirzepatide': 'Tirzepatide is a once-weekly injectable that activates two incretin receptors at the same time:'},
    'longevity-meds-glp-1-review': {
        'https://www.longevitymeds.com/': 'The Science Behind\n\nSemaglutide & Tirzepatide',
        'https://www.longevitymeds.com/legal/safety-information': 'Compounded Tirzepatide is an injectable prescription medication'},
    'novi-weight-loss-glp-1': {
        'https://www.joinnovi.com/begin': (
            'Which GLP-1 medication have you taken?\n\n'
            'Semaglutide (Ozempic or Wegovy)\n\nTirzepatide (Mounjaro or Zepbound)'),
        'https://www.joinnovi.com/': (
            'Semaglutide (Ozempic or Wegovy)\n\nTirzepatide (Mounjaro or Zepbound)')},
    # The other page still offers specific molecules, but this page now contains
    # only a generic compounding disclaimer, so it cannot renew that facet.
    'mademed': {
        'https://mademed.com/rx/oral-semaglutide/': 'This is a compounded medication that has not been approved by the FDA.'},
    'enhance-md': {
        'https://enhance.md/product/core-semaglutide': (
            'GLP-1 Core semaglutide prescription online for weight loss\n\n'
            'This compounded formulation is not FDA-approved and is intended solely for patients under active care of a licensed prescriber.'),
        'https://enhance.md/product/advanced-tirzepatide': 'GLP-1 Advanced tirzepatide prescription online for weight loss'},
    'futurhealth-wegovy-pillform': {
        'https://landing.fh.co/medication-wegovy-pillform5-discount': 'Wegovy® is the only FDA-approved semaglutide in a pill for weight loss.'},
    'breeze-meds-review': {
        'https://www.breezemeds.com/': 'Oral Semaglutide\n\nOral Tirzepatide'},
}


@pytest.mark.parametrize('pid', NON_OFFERING_CAPTURES)
def test_offering_removed_disclaimers_retained_never_renews(producer, monkeypatch, tmp_path, pid):
    good = 'coreage-rx-glp-1-1632'
    code, result, before, after, _ = run_fixture(
        producer, monkeypatch, tmp_path, [pid, good],
        markdown_overrides=NON_OFFERING_CAPTURES[pid])
    assert code == 1 and result['status'] == 'REVIEW_REQUIRED'
    assert [w['program_id'] for w in result['written']] == [good]
    assert result['findings'][0]['program_id'] == pid
    assert result['findings'][0]['status'] == 'REVIEW_REQUIRED'
    # Entire row equality includes its date, tags, bindings and normalized hash.
    assert after['therapeutic_taxonomy']['programs'][pid] == before['therapeutic_taxonomy']['programs'][pid]


# Every new molecular/brand assertion must survive a one-at-a-time negative,
# not merely the removal of all claims. Generic synthetic legal language keeps
# drug-name tokens present; it is deliberately NOT evidence for any live page.
NEW_ASSERTIONS = [
    (pid, field, tag)
    for pid, p in json.loads(SOURCE.with_name('therapeutic-taxonomy-refresh-policies.json').read_text()).items()
    for source in p.get('sources', [p])
    for field in ('molecule_tags', 'brand_name_tags')
    for tag in source.get('quotes', {}).get(field, {})
]


@pytest.mark.parametrize('pid,field,tag', NEW_ASSERTIONS)
def test_each_new_offering_required_with_generic_disclaimers_remaining(producer, monkeypatch, tmp_path, pid, field, tag):
    p = producer.POLICIES[pid]
    sources = p.get('sources', [p])
    source = next(s for s in sources if tag in s['quotes'].get(field, {}))
    removed = producer.normalized(source['quotes'][field][tag])
    text = '\n'.join(dict.fromkeys(q for tags in source['quotes'].values() for q in tags.values()))
    # Remove every occurrence, including the same quote reused for another facet.
    text = producer.normalized(text).replace(removed, '')
    generic = (
        'Compounded semaglutide and compounded tirzepatide are not FDA-approved. '
        'Compounded Semaglutide & Tirzepatide are not FDA-approved. '
        'Mounjaro® and Wegovy® and Zepbound® are registered trademarks. '
        'Mounjaro and Wegovy and Zepbound are registered trademarks.')
    good = next(p for p, r in SNAPSHOT['programs'].items() if r['previously_covered'])
    code, result, before, after, _ = run_fixture(
        producer, monkeypatch, tmp_path, [pid, good],
        markdown_overrides={source['url']: text + '\n' + '.' * 200 + '\n' + generic})
    assert code == 1 and result['status'] == 'REVIEW_REQUIRED'
    assert [w['program_id'] for w in result['written']] == [good]
    assert after['therapeutic_taxonomy']['programs'][pid] == before['therapeutic_taxonomy']['programs'][pid]


# R2 semantic counterexamples keep the complete normalized old name/definition
# or navigation link, not a shortened brand token. Every non-target offering is
# still present. These fixed replacements are synthetic, not provider quotes.
R2_SEMANTIC_CASES = [
    ('measured', 'brand_name_tags', 'wegovy', 'Wegovy® Pill',
     'General medication reference: Wegovy® Pill is a third-party product name. Daily semaglutide pill.'),
    ('healthrx', 'molecule_tags', 'tirzepatide',
     'Compounded Tirzepatide\n\nOnce-weekly injection · Dual-incretin',
     'Medication reference: Compounded Tirzepatide\n\nOnce-weekly injection · Dual-incretin.'),
    ('found-health', 'brand_name_tags', 'wegovy', 'Wegovy® pens, and Wegovy® pills',
     'Medication reference: Wegovy® pens, and Wegovy® pills.'),
    ('found-health', 'brand_name_tags', 'zepbound', 'including Zepbound® vials',
     'Medication reference including Zepbound® vials.'),
    # Both forms share one substantive prescription/checkout sentence. Removing
    # it tests that rule while every independent brand/molecule card remains.
    ('found-health', 'delivery_form_tags', 'injectable', 'Wegovy® pens, and Wegovy® pills',
     'Medication reference: Wegovy® pens, and Wegovy® pills.'),
    *[('eden-xentraffic-offer-3051', 'brand_name_tags', tag,
       f'[{name}® ᴿˣ](https://www.eden.health/treatment/{tag})',
       f'Medication reference: [{name}® ᴿˣ](https://www.eden.health/treatment/{tag})')
      for tag, name in [('mounjaro', 'Mounjaro'), ('wegovy', 'Wegovy'), ('zepbound', 'Zepbound')]],
    ('eden-xentraffic-offer-3051', 'delivery_form_tags', 'injectable',
     'The compounded medications come in various forms, from injectables to liquids.',
     'The compounded medications come in various forms, from injectables to liquids.'),
]


def r2_semantic_capture(producer, pid, field, tag, generic):
    policy = producer.POLICIES[pid]
    source = next(s for s in policy.get('sources', [policy]) if tag in s['quotes'].get(field, {}))
    removed = producer.normalized(source['quotes'][field][tag])
    others = list(dict.fromkeys(q for tags in source['quotes'].values() for q in tags.values()
                               if producer.normalized(q) != removed))
    if pid == 'found-health' and field == 'brand_name_tags':
        # Remove this brand's additional offering in the shared form sentence,
        # without removing the other brands' priced cards or compound plans.
        fragment = {'wegovy': ', Wegovy® pens, and Wegovy® pills',
                    'zepbound': 'Zepbound® vials, '}[tag]
        others = [q.replace(fragment, '') for q in others]
    reference_names = [name for other_pid, other_field, other_tag, name, _ in R2_SEMANTIC_CASES
                       if other_pid == pid and other_tag in source['quotes'].get(other_field, {})]
    text = ('\n' + '.' * 220 + '\n').join(others + ['Medication reference: ' + '\n'.join(reference_names), generic])
    # Other molecule/brand offers remain, even when a treatment facet shares one.
    for other_field in ('molecule_tags', 'brand_name_tags'):
        for other_tag, quote in source['quotes'].get(other_field, {}).items():
            if (other_field, other_tag) != (field, tag):
                assert producer.normalized(quote) in producer.normalized(text)
    return source, text


@pytest.mark.parametrize('pid,field,tag,old_name,generic', R2_SEMANTIC_CASES)
def test_r2_generic_name_is_not_an_offering(producer, monkeypatch, tmp_path, pid, field, tag, old_name, generic):
    source, text = r2_semantic_capture(producer, pid, field, tag, generic)
    assert producer.normalized(old_name) in producer.normalized(text)
    good = 'breeze-meds-review'
    code, result, before, after, _ = run_fixture(
        producer, monkeypatch, tmp_path, [pid, good], markdown_overrides={source['url']: text})
    assert code == 1 and result['status'] == 'REVIEW_REQUIRED'
    assert result['findings'][0]['condition'] == f'assertion_no_longer_supported:{field}:{tag}'
    assert [w['program_id'] for w in result['written']] == [good]
    assert producer.encoded(after['therapeutic_taxonomy']['programs'][pid]) == producer.encoded(before['therapeutic_taxonomy']['programs'][pid])
    assert after['therapeutic_taxonomy']['programs'][pid]['verified_as_of'] == before['therapeutic_taxonomy']['programs'][pid]['verified_as_of']


@pytest.mark.parametrize('fault', ['cache', 'host', 'status', 'empty', 'failed', 'stale'])
def test_capture_still_refuses_untrusted_responses(producer, monkeypatch, tmp_path, fault):
    data = {'metadata': {'statusCode': 200, 'sourceURL': 'https://example.invalid/'}, 'markdown': 'test fixture'}
    document = {'success': True, 'data': data}
    if fault == 'cache': data['metadata']['cacheState'] = 'hit'
    elif fault == 'host': data['metadata']['sourceURL'] = 'https://other.invalid/'
    elif fault == 'status': data['metadata']['statusCode'] = 403
    elif fault == 'empty': data['markdown'] = ''
    elif fault == 'failed': document['success'] = False
    else: data['metadata']['cachedAt'] = '2020-01-01T00:00:00Z'
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(document).encode()
    monkeypatch.setattr(producer, '_firecrawl_key', lambda: 'test-only-not-a-secret')
    monkeypatch.setattr(producer.urllib.request, 'urlopen', lambda *a, **k: Response())
    with pytest.raises(ValueError, match='cached_response_refused|source_host_changed|capture_failed_or_empty|stale_cache_refused'):
        producer.capture('https://example.invalid/', tmp_path / 'fixture.json')
