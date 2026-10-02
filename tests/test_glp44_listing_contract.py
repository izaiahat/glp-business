"""Focused consumer tests; synthetic markup/transport, no provider or job effects."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'systems/analytics-brain'))
import collector
import analytics_brain as brain
from test_glp44_collector_release import synthetic_release, write_json, sha

EMPTY = '''<main data-listing-archetype="A" data-phase3-route="/duration/">
<span data-filter-result aria-live="polite" aria-atomic="true">0 comparable price results</span>
<div data-provider-filters data-duration-policy="fixed-6"><fieldset role="tablist"><button data-filter="drug" data-value="all" aria-selected="true">Both</button></fieldset></div>
<div data-phase3-provider-grid></div><div data-filter-empty><p>No source-backed record matches those filters. An empty result is not a medical or availability conclusion.</p><p data-active-filter-label>Active filters: none.</p><button type="button" data-filter-reset>Reset filters</button></div></main><script src="/assets/site.js?v=synthetic" defer></script>'''


def test_empty_contract_is_structural_not_a_marker():
    contract = collector._listing_contract(EMPTY)
    assert contract['empty'] is True
    assert contract['numeric_count'] == contract['unpublished_count'] == 0
    for damaged in [EMPTY.replace('data-filter-reset', 'data-broken-reset'),
                    EMPTY.replace('data-provider-filters', 'data-missing-filters'),
                    EMPTY.replace('Active filters: none.', 'Active filters: Texas.'),
                    EMPTY.replace('0 comparable', '1 comparable'),
                    EMPTY.replace('<div data-filter-empty>', '<div data-filter-empty hidden>'),
                    EMPTY.replace('<span data-filter-result', '<span hidden data-filter-result'),
                    EMPTY.replace('</main>', '<span data-filter-result>0 comparable price results</span></main>')]:
        assert collector._listing_contract(damaged) != contract
        assert not collector._listing_contract(damaged)['empty']


def test_empty_signature_is_not_an_affiliate_or_privacy_waiver(monkeypatch):
    contract = collector._listing_contract(EMPTY)
    static = dict(path='/duration/', http_status=200, ga4_static_present=True,
                  gtm_static_present=True, posthog_static_present=True, csp_present=True,
                  csp_gtm_allowed=True, csp_posthog_allowed=True,
                  consent_default_denied_present=True, affiliate_route_expected=True,
                  sponsored_anchor_count=0, client_marker_count=0, first_party_redirect_count=0,
                  affiliate_permission_check_complete=True, unauthorized_affiliate_anchor_count=0)
    # Native positive/unknown observations cannot be replaced by an empty marker.
    assert collector._listing_obligation(contract, contract) == 'forbidden'
    positive = copy.deepcopy(contract); positive['numeric_count'] = 1; positive['empty'] = False
    assert collector._listing_obligation(positive, contract) == 'unknown'
    assert collector._listing_obligation(None, contract) == 'unknown'
    assert collector._listing_obligation(contract, {'empty': True}) == 'unknown'
    data = dict(sitemap_routes=['/duration/'], sitemap_complete=True, static_routes=[static],
                affiliate_route_obligations={'/duration/': 'forbidden'})
    def passed(row, obligation='forbidden'):
        payload = copy.deepcopy(data); payload['static_routes'] = [row]
        payload['affiliate_route_obligations']['/duration/'] = obligation
        return brain.build_outputs(payload)['route-instrumentation-matrix.json']['routes'][0]['static_instrumentation_pass']
    assert passed(static)
    assert not passed(static, 'required')  # deleting a positive listing's anchors stays red
    assert not passed(static, 'unknown')
    for field, value in [('consent_default_denied_present', False), ('csp_present', False),
                         ('csp_gtm_allowed', False), ('csp_posthog_allowed', False),
                         ('posthog_static_present', False), ('gtm_static_present', False),
                         ('ga4_direct_loader_present', True), ('http_status', 404),
                         ('unauthorized_affiliate_anchor_count', 1),
                         ('affiliate_permission_check_complete', False), ('sponsored_anchor_count', 1)]:
        damaged = dict(static, **{field: value})
        assert not passed(damaged), field


@pytest.fixture
def listing_release(synthetic_release):
    """Miniature native module fixture; NOT production pricing or acceptance evidence."""
    fixture = synthetic_release
    fixture.control['affiliate_routes'].append('/duration/')
    decision = fixture.pointer.parent / 'authority.md'
    decision.write_text('Synthetic accepted empty contract, test only.')
    fixture.control['affiliate_obligations']['duration_listing_contract'] = {
        'routes': ['/duration/'], 'authority_path': str(decision),
        'authority_sha256': sha(decision.read_bytes())}
    sources = {
        'phase3-page-graph.mjs': '''import {readFileSync} from 'node:fs';
export const phase3Registry=(g,r)=>r;
export const loadPass2DoseCapture=()=>({});
export const renderConfiguredListing=()=>JSON.parse(readFileSync(new URL('../data/synthetic-render.json',import.meta.url),'utf8')).html;
''',
        'public-provider-names.mjs': 'export const publicProviderNames=x=>x;',
        'coupon-offers.mjs': 'export const activeCouponMap=x=>x;',
        'go-runtime-selection.mjs': 'export const loadGoPromotionAuthority=()=>({});',
        'cost-calculator.mjs': "export function attachPricingMatrix(providers,matrix){if(matrix.schema!=='glp-provider-pricing-matrix-v2'||!matrix.providers)throw new Error('provider_pricing_matrix_invalid');return providers;}",
        'affiliate-link-map.mjs': '''import {readFileSync} from 'node:fs';
export function loadAffiliateLinkMap(){try{return JSON.parse(readFileSync(new URL('../data/synthetic-link-authority.json',import.meta.url),'utf8'));}catch{return {'synthetic-allowed':{}};}}
''',
    }
    for name, body in sources.items():
        path = fixture.release / 'src' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    for name, value in {'data/phase3-page-graph.json': {'bulk_pages': [{'route': '/duration/', 'duration_months': 6}]},
                        'data/provider-pricing-matrix.json': {'schema': 'glp-provider-pricing-matrix-v2', 'providers': {}},
                        'data/money-page-specs.json': {'pages': [{'rows': [{'program_id': 'synthetic-allowed'}]}]},
                        'data/synthetic-link-authority.json': {'synthetic-allowed': {}},
                        'data/coupon-offers.json': {}, 'assets/provider-logos/manifest.json': {},
                        'assets/provider-logos/pass2-lp-atlas.json': {},
                        'data/synthetic-render.json': {'html': EMPTY.split('<script')[0]}}.items():
        write_json(fixture.release / name, value)
    public = fixture.release / 'public/duration/index.html'
    public.parent.mkdir(parents=True)
    public.write_text(EMPTY)
    runtime = fixture.release / 'public/assets/site.js'
    runtime.parent.mkdir(parents=True)
    runtime.write_text('// Synthetic runtime bytes, test only.\n')

    def seal():
        fixture.seal()
        manifest = json.loads(fixture.manifest.read_text())
        existing = {row['path'] for row in manifest['release_rows']}
        manifest['release_rows'] += [{'path': path.relative_to(fixture.release).as_posix(), 'sha256': sha(path.read_bytes())}
                                     for path in fixture.release.rglob('*') if path.is_file()
                                     and path.relative_to(fixture.release).as_posix() not in existing]
        write_json(fixture.manifest, manifest)
        pointer = json.loads(fixture.pointer.read_text())
        pointer['manifest']['sha256'] = sha(fixture.manifest.read_bytes())
        write_json(fixture.pointer, pointer)
    fixture.listing_seal = seal
    seal()
    return fixture


def test_bound_native_empty_and_positive_data_cannot_be_deleted(listing_release):
    fixture = listing_release
    static = {'path': '/duration/', 'listing_contract': collector._listing_contract(EMPTY),
              'sponsored_anchor_targets': [], 'sponsored_anchor_count': 0}
    assert static['listing_contract'] == json.loads(json.dumps(static['listing_contract']))
    assert collector.derive_affiliate_obligations(fixture.control, static_routes=[static])['/duration/'] == 'forbidden'
    # A sealed producer now emits a genuine-to-this-fixture positive cohort.
    positive = EMPTY.replace('0 comparable', '1 comparable').replace('<div data-phase3-provider-grid></div>',
              '<div data-phase3-provider-grid><article data-phase3-provider="synthetic-allowed">Synthetic comparable row</article></div>')
    write_json(fixture.release / 'data/synthetic-render.json', {'html': positive.split('<script')[0]})
    fixture.listing_seal()
    # Cached/deleted positive cards cannot masquerade as the prior empty cohort.
    assert collector.derive_affiliate_obligations(fixture.control, static_routes=[static])['/duration/'] == 'unknown'
    static['listing_contract'] = collector._listing_contract(positive)
    assert collector.derive_affiliate_obligations(fixture.control, static_routes=[static])['/duration/'] == 'required'
    del static['listing_contract']
    assert collector.derive_affiliate_obligations(fixture.control, static_routes=[static])['/duration/'] == 'unknown'


@pytest.mark.parametrize('damage', ['pricing', 'unknown-matrix', 'unbound-data', 'swallowed-drift', 'link-map-unknown', 'module', 'authority', 'runtime', 'loader'])
def test_listing_seal_damage_is_unknown_never_empty(listing_release, damage):
    fixture = listing_release
    if damage == 'unbound-data':
        manifest = json.loads(fixture.manifest.read_text())
        manifest['release_rows'] = [r for r in manifest['release_rows'] if r['path'] != 'data/provider-pricing-matrix.json']
        write_json(fixture.manifest, manifest)
        pointer = json.loads(fixture.pointer.read_text()); pointer['manifest']['sha256'] = sha(fixture.manifest.read_bytes())
        write_json(fixture.pointer, pointer)
    elif damage == 'unknown-matrix':
        write_json(fixture.release / 'data/provider-pricing-matrix.json', {})
        fixture.listing_seal()  # Even a re-sealed malformed input is UNKNOWN, not zero.
    elif damage in ('swallowed-drift', 'link-map-unknown'):
        write_json(fixture.release / 'data/synthetic-link-authority.json', {})
        if damage == 'link-map-unknown':
            fixture.listing_seal()
    elif damage == 'authority':
        (fixture.pointer.parent / 'authority.md').write_text('Changed synthetic authority')
    elif damage == 'loader':
        (fixture.release / 'public/duration/index.html').write_text(EMPTY.replace('/assets/site.js', '/assets/missing.js'))
        fixture.listing_seal()
    else:
        name = {'pricing': 'data/provider-pricing-matrix.json', 'module': 'src/phase3-page-graph.mjs',
                'runtime': 'public/assets/site.js'}[damage]
        (fixture.release / name).write_text('{}')
    row = {'path': '/duration/', 'listing_contract': collector._listing_contract(EMPTY),
           'sponsored_anchor_targets': [], 'sponsored_anchor_count': 0}
    assert collector.derive_affiliate_obligations(fixture.control, static_routes=[row])['/duration/'] == 'unknown'
    assert 'listing_contract_error' in row
