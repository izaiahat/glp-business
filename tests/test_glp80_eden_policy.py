"""Authentic four-page Eden replay. No main, locks, live fetch or registry write.

Portable checks exercise the registered producer's verify_sources/support path.
The optional host check exercises its unchanged actual Node validator, not a
stub. The one-row native projection is a labelled fixture, never live adoption.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import types

import pytest

BASE = Path(__file__).resolve().parents[1]
REL = Path('site-system/flagship-v2/candidates/glp34-provider-families-20260912')
SOURCE = BASE / REL / 'scripts/refresh-therapeutic-taxonomy.py'
FIXTURES = BASE / 'tests/fixtures/glp80-eden'
PID = 'eden-xentraffic-offer-3051'
URLS = [
    'https://www.eden.health/treatment/glp-1-treatments',
    'https://www.eden.health/treatment/mounjaro',
    'https://www.eden.health/treatment/wegovy',
    'https://www.eden.health/treatment/zepbound',
]
FIELDS = ('molecule_tags', 'brand_name_tags', 'delivery_form_tags', 'treatment_type_tags')
QUALIFICATION = ('Reported information: Eden advertises a conditional Zepbound-labelled offering. '
                 'Its explanatory paragraph names Mounjaro; the medication actually prescribed '
                 'or supplied has not been verified.')


def digest(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def replay(monkeypatch, tmp_path):
    # Only import dependencies are stubbed. No capture, support or validator
    # result is stubbed; capture is replaced with hash-verified retained bytes.
    class UnexpectedLock:
        def __init__(self, *args, **kwargs):
            raise AssertionError('lock acquisition forbidden in read-only fixture')
    monkeypatch.setitem(sys.modules, 'affiliate_direct_evidence_capture',
                        types.SimpleNamespace(_firecrawl_key=lambda: pytest.fail('network forbidden')))
    monkeypatch.setitem(sys.modules, 'glp_build_lock', types.SimpleNamespace(BuildLock=UnexpectedLock))
    spec = importlib.util.spec_from_file_location('eden_readonly_replay', SOURCE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    captured = json.loads((FIXTURES / 'capture-manifest.json').read_text())
    index = {'sources': [dict(item, url=item['request_url'],
                              sha256=item['capture_sha256'],
                              path=Path(item['path']).name) for item in captured]}
    receipts = {}
    for item in index['sources']:
        raw = (FIXTURES / item['path']).read_bytes()
        assert digest(raw) == item['sha256']
        receipt = json.loads(raw)
        assert digest(receipt['response']['data']['markdown'].encode()) == item['markdown_sha256']
        assert receipt['request']['url'] == item['url']
        assert receipt['received_at'] == item['received_at']
        assert receipt['response']['success'] is True
        assert receipt['response']['data']['metadata']['statusCode'] == 200
        receipts[item['url']] = receipt
    assert list(receipts) == URLS
    m.BUSINESS = tmp_path
    run = tmp_path / 'reports/eden-fixture'
    run.mkdir(parents=True)
    row = json.loads((FIXTURES / 'native-input.json').read_text())['therapeutic_taxonomy']['programs'][PID]
    # Membership adoption is NOT asserted. The fixture alone models the exact
    # reviewed closure while retaining original dates, tags and commercial state.
    row['source_urls'] = sorted(set(row['source_urls']) | set(URLS))
    return m, receipts, row, run, index


def collect(replay, overrides=None, row=None):
    m, receipts, current, run, index = replay
    receipts = copy.deepcopy(receipts)
    overrides = overrides or {}
    for url, text in overrides.items():
        receipts[url]['response']['data']['markdown'] = text
    calls = []
    def retained_capture(url, path):
        calls.append(url)
        receipt = receipts[url]
        # Negative-only body mutation is expressly synthetic; receive clocks
        # remain the real retained clocks, never claimed as a new observation.
        path.write_bytes(m.encoded(receipt))
        return receipt
    m.capture = retained_capture
    result = m.verify_sources(PID, row or current, m.POLICIES[PID], run)
    assert calls == URLS
    return result


def test_seven_assertions_exact_bytes_and_context(replay):
    m, receipts, row, run, index = replay
    p = m.POLICIES[PID]
    assert [s['url'] for s in p['sources']] == URLS
    assert QUALIFICATION in p['finding']
    assert 'prescription after online consultation' in p['finding']
    assert 'membership does not guarantee a prescription' in p['finding']
    assert p['claim_qualification'] == QUALIFICATION
    assert 'review_required' not in p
    assert 'Mounjaro® is an FDA-approved weekly injectable medication for type 2 diabetes' in receipts[URLS[3]]['response']['data']['markdown']
    assert 'Compounded medications are not FDA-approved' in receipts[URLS[0]]['response']['data']['markdown']
    result = collect(replay)
    count = 0
    for source, binding, item in zip(p['sources'], result['sources'], index['sources']):
        text = receipts[source['url']]['response']['data']['markdown']
        assert binding['capture_sha256'] == item['sha256']
        assert binding['captured_at'] == item['received_at']
        for field, tags in source['quotes'].items():
            for tag, exact in tags.items():
                assert exact in text
                assert binding['assertions'][field][tag]['quote'] == exact
                assert digest(binding['assertions'][field][tag]['quote'].encode()) == digest(exact.encode())
                count += 1
    assert count == 7
    assert result['verified_as_of'] == min(s['received_at'][:10] for s in index['sources'])
    assert result['finding'] == p['finding']
    assert result['tags_unchanged'] == {f: row[f] for f in FIELDS}


@pytest.mark.parametrize('brand,source_index', [('mounjaro', 1), ('wegovy', 2), ('zepbound', 3)])
def test_brand_offer_removed_only_target_fails(replay, brand, source_index):
    m, receipts, row, run, index = replay
    own = m.POLICIES[PID]['sources'][source_index]
    text = receipts[own['url']]['response']['data']['markdown']
    # The complete hero and bottom affirmative product offers disappear.
    # Nav, science, indication and trademark text remain, including the real
    # Mounjaro paragraph on the Zepbound route. Other pages stay byte-identical.
    hero = text.index('# ' + brand.capitalize() + '®')
    end = text.index('Related Products', hero)
    text = text[:hero] + text[end:]
    bottom = text.index('# ' + brand.capitalize() + '®')
    end = text.index('\\*Price includes medication only', bottom)
    text = text[:bottom] + text[end:]
    assert brand.capitalize() in text
    assert 'Disclaimer' in text
    assert 'Prescription Process: Online evaluations' in text
    if brand == 'zepbound':
        assert 'Mounjaro® is an FDA-approved weekly injectable medication for type 2 diabetes' in text
    with pytest.raises(ValueError, match=f'assertion_no_longer_supported:brand_name_tags:{brand}'):
        collect(replay, {own['url']:text})
    for other in m.POLICIES[PID]['sources']:
        if other['url'] == own['url']:
            continue
        fragment = {f:list(other['quotes'].get(f, {})) for f in FIELDS}
        m.support(fragment, other, receipts[other['url']]['response']['data']['markdown'])


def test_compound_offering_removed_disclaimers_remain(replay):
    m, receipts, row, run, index = replay
    root = m.POLICIES[PID]['sources'][0]
    text = receipts[URLS[0]]['response']['data']['markdown']
    # Remove the plan product offerings and the FAQ's conditional medication
    # sentence; leave legal nonapproval, injection context and all brand offers.
    start = text.index('![Clear vial with green cap labeled compounded semaglutide')
    end = text.index('# What’s inside matters.', start)
    text = text[:start] + text[end:]
    quote = root['quotes']['treatment_type_tags']['compounded']
    text = text.replace(quote, '')
    assert 'compounded semaglutide' in text.lower()
    assert 'compounded tirzepatide' in text.lower()
    assert 'not FDA-approved' in text
    assert root['quotes']['delivery_form_tags']['injectable'] in text
    with pytest.raises(ValueError, match='assertion_no_longer_supported:molecule_tags:semaglutide'):
        collect(replay, {URLS[0]:text})
    for field, tag in [('molecule_tags','semaglutide'), ('molecule_tags','tirzepatide'), ('treatment_type_tags','compounded')]:
        fragment = {f:([tag] if f == field else []) for f in FIELDS}
        single = dict(root, quotes={field:{tag:root['quotes'][field][tag]}})
        with pytest.raises(ValueError, match=f'assertion_no_longer_supported:{field}:{tag}'):
            m.support(fragment, single, text)


def test_injection_offering_removed_not_inferred_from_molecules(replay):
    m, receipts, row, run, index = replay
    root = m.POLICIES[PID]['sources'][0]
    text = receipts[URLS[0]]['response']['data']['markdown']
    injection = root['quotes']['delivery_form_tags']['injectable']
    text = text.replace(injection, '')
    # Other injection/safety/educational tokens are retained. They must not
    # replace the reviewed provider-level dose/needle/assessment offering.
    assert 'injection' in text.lower()
    assert root['quotes']['treatment_type_tags']['compounded'] in text
    with pytest.raises(ValueError, match='assertion_no_longer_supported:delivery_form_tags:injectable'):
        collect(replay, {URLS[0]:text})
    fragment = {f:list(root['quotes'].get(f, {})) if f != 'delivery_form_tags' else [] for f in FIELDS}
    remaining = dict(root, quotes={f:tags for f,tags in root['quotes'].items() if f != 'delivery_form_tags'})
    m.support(fragment, remaining, text)


def test_current_source_membership_still_refuses_before_capture(replay):
    m, receipts, row, run, index = replay
    original = json.loads((FIXTURES / 'native-input.json').read_text())['therapeutic_taxonomy']['programs'][PID]
    m.capture = lambda *a: pytest.fail('capture before membership validation')
    with pytest.raises(ValueError, match='reviewed_source_not_in_program'):
        m.verify_sources(PID, original, m.POLICIES[PID], run)


def test_native_validator_actual_not_stubbed(replay):
    m, receipts, row, run, index = replay
    native_root = Path('/home/ubuntu/business') / REL
    native = native_root / 'src/therapeutic-taxonomy.mjs'
    if not native.is_file():
        pytest.skip('host-only native source; portable CI runs authentic support replay')
    m.ROOT = native_root
    result = collect(replay)
    registry = json.loads((FIXTURES / 'native-input.json').read_text())
    original = registry['therapeutic_taxonomy']['programs'][PID]
    proposal = copy.deepcopy(row)
    proposal['verified_as_of'] = result['verified_as_of']
    manifest_bytes = m.encoded(result)
    registry['therapeutic_taxonomy']['authorities']['eden_fixture_manifest'] = {
        'path':'reports/eden-fixture/manifest.json', 'sha256':digest(manifest_bytes)}
    proposal['evidence_bindings'] = [
        {'authority':'eden_fixture_manifest', 'authority_sha256':digest(manifest_bytes),
         'source_url':s['source_url'], 'verified_as_of':s['verified_as_of'],
         'evidence_sha256':s['capture_sha256']} for s in result['sources']]
    registry['therapeutic_taxonomy']['programs'][PID] = proposal
    # Only the labelled one-row fixture is validated, not a false claim that
    # today's complete registry's four stale rows have become ready.
    accepted = m.validate(registry, [PID])
    actual = accepted['therapeutic_taxonomy']['programs'][PID]
    assert actual['verified_as_of'] == result['verified_as_of']
    assert actual['commercial_link_state'] == original['commercial_link_state']
    assert accepted['therapeutic_taxonomy']['freshness_max_days'] == 31
    assert accepted['therapeutic_taxonomy']['expected_counts']['provider_rows'] == 1
    stale = copy.deepcopy(accepted)
    stale['therapeutic_taxonomy']['programs'][PID]['verified_as_of'] = '2026-08-30'
    with pytest.raises(ValueError, match='therapeutic_taxonomy_evidence_stale'):
        m.validate(stale, [PID])
