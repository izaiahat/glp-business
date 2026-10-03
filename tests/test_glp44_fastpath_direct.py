"""Synthetic source-binding regression; no host files or production requests."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'systems/analytics-brain'))
from test_glp44_collector_release import synthetic_release, write_json, sha, AUTHORITIES
import collector


@pytest.mark.parametrize('case', ['approved', 'go', 'wrong-href', 'wrong-surface', 'held-network', 'inactive', 'rejected-terms', 'source-drift'])
def test_fastpath_direct_without_go_membership(synthetic_release, case):
    f = synthetic_release
    pid = 'synthetic-direct-fastpath'
    href = 'https://direct.synthetic.invalid/issued?publisher=fixture'
    registry = json.loads((f.release / AUTHORITIES[1]).read_text())
    registry['programs'].append({'program_id': pid, 'commercial_state': 'live',
        'review_path': f'/reviews/{pid}/', 'profiles': {'glp-clinic-scout': {
            'vertical_fit': True, 'wired': True, 'surfaces': {'review': True, 'go': False}}}})
    write_json(f.release / AUTHORITIES[1], registry)
    decision = f.release.parent / 'direct-approval.md'
    decision.write_text('Synthetic direct approval, never redirect permission.\n')
    authority = json.loads((f.release / AUTHORITIES[2]).read_text())
    authority['programs'][pid] = {'promotion_authorized': True, 'status': 'VERIFIED',
        'network': 'synthetic', 'reason': 'operator_approved_glp_live',
        'operator_approved_live_decision': {'path': str(decision), 'sha256': sha(decision.read_bytes())}}
    write_json(f.release / AUTHORITIES[2], authority)
    names = ('data/provider-page-contracts.json', 'data/provider-fastpath-contract.json',
             'data/affiliate-wire-in-approvals.json')
    contract = {'program_id': pid, 'review_path': f'/reviews/{pid}/',
        'row_contract': {'program_id': pid, 'promotion_authorized': True, 'live_link_allowed': True,
            'affiliate_link_mode': 'transparent_direct', 'affiliate_network': 'synthetic', 'affiliate_href': href},
        'network_contract': {'network': 'synthetic', 'immutable_offer_id': '123',
            'activation_blocker': None, 'relationship_state': 'approved_or_active',
            'transparent_direct_supported_by_current_renderer': True, 'tracking_url': href}}
    active = {'program_id': pid, 'affiliate_cta_state': 'ACTIVE_PROVIDER_ISSUED_DIRECT',
              'route_state': 'LIVE_EDITORIAL_AFFILIATE_ACTIVE'}
    terms = {'existing_release_alias': pid, 'provider_program_id': '123',
             'status': 'operator_accepted_terms', 'verdict': 'operator_accepted_terms',
             'live_link_disposition': 'preserve_existing_live', 'future_change_terms_gate_open': True,
             'existing_live_surfaces': ['compare'], 'authorized_material_expansion_surfaces': []}
    if case == 'inactive':
        active['affiliate_cta_state'] = 'HELD'
    if case == 'rejected-terms':
        terms['verdict'] = 'operator_rejected_terms'
    for name, doc in zip(names, [
        {'schema': 'glp-provider-fastpath-contracts-v1', 'providers': [contract]},
        {'schema': 'glp-provider-fastpath-integration-v1', 'providers': [active]},
        {'schema': 'glp-affiliate-wire-in-approvals-v1', 'current_terms_reassessment': {'programs': [terms]}}
    ]):
        write_json(f.release / name, doc)
    f.seal()
    manifest = json.loads(f.manifest.read_text())
    manifest['release_rows'].extend({'path': name, 'sha256': sha((f.release / name).read_bytes())} for name in names)
    write_json(f.manifest, manifest)
    pointer = json.loads(f.pointer.read_text())
    pointer['manifest']['sha256'] = sha(f.manifest.read_bytes())
    write_json(f.pointer, pointer)
    if case == 'source-drift':
        (f.release / names[0]).write_text('{}')
    if case == 'held-network':
        held = f.release.parent / 'current-hold.md'
        held.write_text('Synthetic current hold.')
        f.control['affiliate_obligations']['current_network_holds'] = {'networks': ['synthetic'],
            'authority_path': str(held), 'authority_sha256': sha(held.read_bytes())}
    target = {'program_id': pid, 'href': f'/go/{pid}' if case == 'go' else href,
              'link_mode': 'transparent-direct', 'network': 'synthetic'}
    if case == 'wrong-href':
        target['href'] += '&unapproved=1'
    paths = [f'/providers/{pid}/', f'/reviews/{pid}/']
    if case == 'wrong-surface':
        paths = ['/compare/synthetic-one/', '/compare/synthetic-two/']
    static = [{'path': path, 'sponsored_anchor_count': 1,
               'sponsored_anchor_targets': [copy.deepcopy(target)]} for path in paths]
    if case == 'source-drift':
        with pytest.raises(RuntimeError, match='affiliate_fastpath_authority_drift'):
            collector.derive_affiliate_obligations(f.control, set(paths), static)
        return
    obligations = collector.derive_affiliate_obligations(f.control, set(paths), static)
    assert sum(row['unauthorized_affiliate_anchor_count'] for row in static) == (0 if case == 'approved' else 2)
    if case in ('approved', 'go', 'wrong-href'):
        assert all(obligations[path] == 'required' for path in paths)
    assert pid not in f.local['programs']  # Never added to redirect authority.
