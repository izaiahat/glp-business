"""Contract tests for the weekly program-liveness audit.

The audit's whole value is that a wrong verdict is worse than no verdict: a
false "live" leaves a dead affiliate link in front of readers, and a false
"paused" would pull a working program off the site. So these tests pin the
verdict rules per network, the honesty of the unknown lane, the fact that the
audit never touches the site, and that only paused/gone reach the removal queue.

Pure functions only - every network call is stubbed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path('/home/ubuntu/business')


def load_module():
    spec = importlib.util.spec_from_file_location(
        'glp_program_liveness_check', ROOT / 'scripts/glp_program_liveness_check.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules['glp_program_liveness_check'] = module
    spec.loader.exec_module(module)
    return module


M = load_module()


class FakeState:
    """A NetworkState with catalogues injected instead of fetched."""

    def __init__(self, awin=None, cj=None, healthtrader=None, response=None, offline=False):
        self._awin, self._cj, self._ht = awin, cj, healthtrader
        self.response = response
        self.errors = {}
        self.offline = offline
        self.probe_clicks = 0

    def awin(self):
        return self._awin

    def cj(self):
        return self._cj

    def healthtrader(self):
        return self._ht


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError('a test made a real HTTP call')
    monkeypatch.setattr(M, 'http_get', explode)


def stub_http(monkeypatch, response):
    monkeypatch.setattr(M, 'http_get', lambda *a, **k: response)


# --- API-unavailable rails: no tracking-link requests -----------------------

@pytest.mark.parametrize('network', ['Katalys', 'RevOffers', 'Direct affiliate', 'GoAffPro'])
def test_api_unavailable_liveness_never_requests_tracking_url(network):
    """The autouse HTTP guard explodes on any tracking URL request."""
    state = FakeState()
    result = M.check_program({
        'identity': {'network': network, 'network_program_id': '384',
                     'tracking_url': 'https://track.example/aff_c?offer_id=384'},
    }, state, {})
    assert result['verdict'] == M.VERDICT_UNKNOWN
    assert result['check'] == 'portal_check_needed'
    assert state.probe_clicks == 0
    assert M.build_queue(receipt_with([result['verdict']]))['programs'] == []



def test_katalys_without_an_offer_id_is_unknown_and_probes_nothing():
    state = FakeState()
    result = M.check_katalys({'network': 'Katalys', 'network_program_id': ''}, state)
    assert result['verdict'] == M.VERDICT_UNKNOWN
    assert result['check'] == 'portal_check_needed'
    assert state.probe_clicks == 0



# --- Awin / CJ / HealthTrader: relationship APIs, zero clicks ---------------


def test_awin_active_joined_programme_is_live():
    state = FakeState(awin={'80963': {'id': 80963, 'name': 'ShedRX', 'status': 'Active'}})
    result = M.check_awin({'network': 'Awin', 'network_program_id': '80963'}, state)
    assert result['verdict'] == M.VERDICT_LIVE
    assert state.probe_clicks == 0


def test_awin_non_active_status_is_paused_and_absent_is_gone():
    state = FakeState(awin={'80963': {'id': 80963, 'name': 'ShedRX', 'status': 'Suspended'}})
    assert M.check_awin({'network_program_id': '80963'}, state)['verdict'] == M.VERDICT_PAUSED
    assert M.check_awin({'network_program_id': '999'}, state)['verdict'] == M.VERDICT_GONE


def test_awin_api_outage_is_unknown_never_gone():
    state = FakeState(awin=None)
    state.errors['awin'] = 'programmes_http_503'
    result = M.check_awin({'network_program_id': '80963'}, state)
    assert result['verdict'] == M.VERDICT_UNKNOWN
    assert '503' in result['evidence']


def test_cj_requires_both_active_account_and_joined_relationship():
    joined = {'6105366': {'account_status': 'Active', 'relationship_status': 'joined',
                          'advertiser_name': 'Paternity Lab'}}
    assert M.check_cj({'network_program_id': '6105366'},
                      FakeState(cj=joined))['verdict'] == M.VERDICT_LIVE
    ended = {'6105366': {'account_status': 'Active', 'relationship_status': 'notjoined',
                         'advertiser_name': 'Paternity Lab'}}
    assert M.check_cj({'network_program_id': '6105366'},
                      FakeState(cj=ended))['verdict'] == M.VERDICT_PAUSED
    assert M.check_cj({'network_program_id': '404'},
                      FakeState(cj=joined))['verdict'] == M.VERDICT_GONE


def test_healthtrader_presence_in_offers_list_is_the_signal():
    state = FakeState(healthtrader={'abc': {'offer_name': 'Something'}})
    assert M.check_healthtrader({'network_program_id': 'abc'}, state)['verdict'] == M.VERDICT_LIVE
    assert M.check_healthtrader({'network_program_id': 'zzz'}, state)['verdict'] == M.VERDICT_GONE


def test_every_api_lane_is_click_free(monkeypatch):
    """API adapters must never touch a tracking link."""
    state = FakeState(awin={}, cj={}, healthtrader={})
    for adapter in (M.check_awin, M.check_cj, M.check_healthtrader):
        adapter({'network_program_id': 'x'}, state)
    assert state.probe_clicks == 0


# --- portal-only lane: honest unknowns --------------------------------------


def test_portal_only_link_stays_unknown_without_request():
    result = M.check_portal_only(
        {'network': 'Direct affiliate', 'tracking_url': 'https://orderlymeds.com/?affid=2608'},
        FakeState())
    assert result['verdict'] == M.VERDICT_UNKNOWN
    assert result['check'] == 'portal_check_needed'
    assert 'publisher relationship API' in result['evidence']



def test_unmapped_network_without_a_link_is_unknown():
    result = M.check_portal_only({'network': '', 'tracking_url': None}, FakeState())
    assert result['verdict'] == M.VERDICT_UNKNOWN
    assert result['check'] == 'portal_check_needed'


def test_unknown_network_routes_to_the_portal_lane():
    assert M.NETWORK_ADAPTERS.get('rakuten') is None
    assert M.NETWORK_ADAPTERS['katalys'] is M.check_katalys
    assert M.NETWORK_ADAPTERS['awin'] is M.check_awin
    assert M.NETWORK_ADAPTERS['cj affiliate'] is M.check_cj


# --- queue + receipt semantics ---------------------------------------------


def receipt_with(verdicts):
    return {
        'checked_at_utc': '2026-08-18T00:00:00Z',
        'release': '/releases/test',
        'programs': [
            {'program': f'p{i}', 'display_name': f'P{i}', 'network': 'Katalys',
             'network_program_id': str(1000 + i), 'review_path': f'/reviews/p{i}/',
             'go_route': f'/go/p{i}', 'verdict': verdict, 'check': 'tracking_probe',
             'evidence': 'evidence'}
            for i, verdict in enumerate(verdicts)
        ],
    }


def test_only_paused_and_gone_reach_the_removal_queue():
    queue = M.build_queue(receipt_with([M.VERDICT_LIVE, M.VERDICT_PAUSED,
                                        M.VERDICT_UNKNOWN, M.VERDICT_GONE]))
    assert [row['verdict'] for row in queue['programs']] == [M.VERDICT_PAUSED, M.VERDICT_GONE]
    assert M.REMOVAL_VERDICTS == (M.VERDICT_PAUSED, M.VERDICT_GONE)


def test_an_unknown_never_pulls_a_program_off_the_site():
    queue = M.build_queue(receipt_with([M.VERDICT_UNKNOWN] * 5))
    assert queue['programs'] == []
    assert 'glp_site_paused_program_removal.py' in queue['note']


def test_the_audit_never_modifies_the_site():
    source = (ROOT / 'scripts/glp_program_liveness_check.py').read_text()
    for forbidden in ('write_text', 'subprocess', 'wrangler'):
        if forbidden == 'write_text':
            # It writes exactly two artefacts: the receipt and the queue.
            assert source.count('write_text') == 2
        else:
            assert forbidden not in source


def test_registered_dry_run_audits_real_release_without_effects(monkeypatch, capsys):
    spec = json.loads((ROOT / 'ops/n8n-bridge/actions.json').read_text())['program_liveness_check']
    assert spec['argv'] == ['python3', 'scripts/glp_program_liveness_check.py']
    assert spec['value_flags']['dry-run'] == {'1': ['--dry-run']}
    assert 'dry-run' in spec['allowed_args']

    def forbidden(*args, **kwargs):
        raise AssertionError('dry-run attempted HTTP or an artifact write')

    monkeypatch.setattr(M, 'http_get', forbidden)
    monkeypatch.setattr(Path, 'mkdir', forbidden)
    monkeypatch.setattr(Path, 'write_text', forbidden)
    monkeypatch.setattr(M, 'build_queue', forbidden)
    monkeypatch.setattr(sys, 'argv', [spec['argv'][1], *spec['value_flags']['dry-run']['1']])
    assert M.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'PASS'
    assert result['schema'] == 'glp.program_liveness_summary.v1'
    assert result['checked'] > 0
    assert result['unknown'] == result['checked']
    assert result['tracking_probe_clicks'] == 0
    assert result['dry_run'] is result['no_effects'] is True
    assert result['receipt'] is result['queue'] is None

# --- identity resolution ----------------------------------------------------


def test_identity_resolves_by_program_id_then_by_display_name():
    mapping = {
        'drhouse': {'network': 'Awin', 'network_program_id': '53271',
                    'tracking_url': None, 'identity_source': 'wire-in'},
        M.norm('CoreAge Rx GLP-1'): {'network': 'Katalys', 'network_program_id': '1632',
                                     'tracking_url': None, 'identity_source': 'wire-in'},
    }
    assert M.resolve_identity('drhouse', 'DrHouse', mapping)['network_program_id'] == '53271'
    assert M.resolve_identity('unknown-id', 'CoreAge Rx GLP-1',
                              mapping)['network_program_id'] == '1632'
    assert M.resolve_identity('nothing', 'Nothing', mapping)['network'] == ''


def test_snapshot_corroboration_is_attached_but_never_overrides_the_verdict():
    snapshot = M.snapshot_index([
        {'network': 'Katalys', 'program_id': '384', 'status': 'paused', 'relationship': 'Approved'},
    ])
    program = {'identity': {'network': 'Katalys', 'network_program_id': '384'}}
    result = M.check_program(program, FakeState(), snapshot)
    assert result['verdict'] == M.VERDICT_UNKNOWN
    assert result['marketplace_snapshot']['status'] == 'paused'


def test_snapshot_index_ignores_rows_without_an_identity():
    assert M.snapshot_index([{'network': '', 'program_id': '1'},
                             {'network': 'Awin', 'program_id': ''}]) == {}


def test_oversized_snapshot_file_is_loaded_without_data_loss(tmp_path):
    rows = [
        {'network': 'Awin', 'program_id': str(index), 'status': 'active',
         'relationship': 'approved', 'padding': 'x' * 300}
        for index in range(250)
    ]
    payload = json.dumps(rows)
    assert len(payload.encode('utf-8')) > 65_536
    snapshot_file = tmp_path / 'snapshot.json'
    snapshot_file.write_text(payload)

    loaded = M.load_snapshot_arg(SimpleNamespace(
        snapshot_file=str(snapshot_file), snapshot_json=None))

    assert loaded == rows


def test_snapshot_file_takes_precedence_over_inline_fallback(tmp_path):
    snapshot_file = tmp_path / 'snapshot.json'
    snapshot_file.write_text(json.dumps([{'network': 'Awin', 'program_id': 'file'}]))
    loaded = M.load_snapshot_arg(SimpleNamespace(
        snapshot_file=str(snapshot_file),
        snapshot_json=json.dumps([{'network': 'Awin', 'program_id': 'argv'}]),
    ))
    assert loaded[0]['program_id'] == 'file'


# --- go manifest parsing ----------------------------------------------------


def test_go_manifest_parser_reads_the_generated_module(tmp_path):
    release = tmp_path / 'release'
    (release / 'functions/go').mkdir(parents=True)
    manifest = {'manifest_sha256': 'abc', 'programs': {'shedrx': {'program_id': 'shedrx'}}}
    (release / 'functions/go/program-manifest.js').write_text(
        '// Generated\nexport const GO_RUNTIME_MANIFEST = Object.freeze('
        + json.dumps(manifest) + ');\n')
    parsed = M.go_manifest_programs(release)
    assert parsed['manifest_sha256'] == 'abc'
    assert list(parsed['programs']) == ['shedrx']


def test_registry_live_filter_excludes_unwired_and_dead_rows(tmp_path):
    release = tmp_path / 'release'
    (release / 'data').mkdir(parents=True)
    registry = {'programs': [
        {'program_id': 'live-one', 'commercial_state': 'live', 'display_name': 'Live',
         'profiles': {'glp-clinic-scout': {'wired': True, 'surfaces': {'go': True}}}},
        {'program_id': 'not-wired', 'commercial_state': 'live',
         'profiles': {'glp-clinic-scout': {'wired': False, 'surfaces': {'go': True}}}},
        {'program_id': 'not-live', 'commercial_state': 'staged',
         'profiles': {'glp-clinic-scout': {'wired': True, 'surfaces': {'go': True}}}},
        {'program_id': 'no-surface', 'commercial_state': 'live',
         'profiles': {'glp-clinic-scout': {'wired': True, 'surfaces': {'quiz': True}}}},
    ]}
    (release / 'data/program-registry.json').write_text(json.dumps(registry))
    assert list(M.registry_live_programs(release)) == ['live-one']
