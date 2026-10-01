"""Offline canonical notice -> real sole writer -> existing registry delta.

All transport is in-memory; captures are deliberately synthetic and are NOT
runtime authority. Native site projection excerpts are a bounded build fixture,
not production acceptance. No affiliate URL is fetched.
"""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from test_glp250_partner_email_hold import lane as lane
from test_cleanup_affiliate_decisions import inbox as canonical_inbox

ROOT = Path(__file__).resolve().parents[1]
WHEN = '2026-10-01T02:00:00Z'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def withdrawal(lane, canonical_inbox, tmp_path, monkeypatch):
    from inbox_control_plane import affiliate_decision_queue as inbox
    import affiliate_program_withdrawal as withdrawal
    monkeypatch.setattr(withdrawal, 'BUSINESS', tmp_path)
    import affiliate_terms_fanout as fanout
    row = lane.service.rows[758]
    row[10], row[19], row[20] = 'approved_active_wired_live', True, True
    row[4], row[5] = 'Awin', 'https://ui.awin.com/merchant-profile/3277'
    row[9] = 'synthetic-glp-provider'
    row[14] = 'https://example.test/issued-link-never-followed'
    root = withdrawal.BUSINESS
    from test_cleanup_affiliate_decisions import Queue, mail
    cp, inbox, ledger, mailbox = canonical_inbox
    raw_bytes = mail('Affiliate program terminated', 'Your affiliate relationship for Synthetic GLP-1 provider (3277) has been terminated effective October 1, 2026. Remove all promotional links.')
    ledger.ingest(mailbox, 'fixture-termination', raw_bytes)
    transport = Queue()
    assert inbox.dispatch(ledger, live=True, client=transport)['delivered'] == 1
    notice = transport.items[0]
    notice['evidence'] = json.loads(notice['evidence'])
    assert notice['evidence']['decision_class'] == 'terminated'
    raw = Path(notice['evidence']['path'])
    capture = tmp_path / 'provider-readback.json'
    capture.write_text(json.dumps({
        'schema': 'affiliate-program-status-readback-v1',
        'source_kind': 'authenticated_provider_api', 'network': 'Awin',
        'program_id': '3277', 'program_name': row[2],
        'relationship_status': 'terminated', 'observed_at_utc': WHEN,
        'source_event_id': notice['evidence']['event_id'],
        'response': {'program_id': '3277', 'relationship_status': 'terminated'},
    }))
    verification = {'path': str(capture), 'sha256': digest(capture)}
    registry = tmp_path / 'program-registry.json'
    provider = {'program_id': 'synthetic-program', 'review_slug': 'synthetic-glp-provider',
                'display_name': row[2], 'commercial_state': 'live',
                'review_path': '/reviews/synthetic-glp-provider/',
                'go': {'route': '/go/synthetic-glp-provider', 'entry_state': 'live'},
                'profiles': {'glp-clinic-scout': {
                    'vertical_fit': True, 'wired': True,
                    'data_profile': {'has_price': True, 'has_review_facts': True},
                    'surfaces': {s: True for s in ('compare', 'quiz', 'calculator', 'review', 'go', 'sitemap')}}},
                'affiliate_terms': {'old_fact': 'must remain'},
                'affiliate_program_history': [{'event_id': 'old-adverse-event', 'state': 'paused'}]}
    other = copy.deepcopy(provider)
    other.update(program_id='unrelated', review_slug='unrelated', display_name='Other provider', review_path='/reviews/unrelated/')
    other['go'] = {'route': '/go/unrelated', 'entry_state': 'held'}
    registry.write_text(json.dumps({'schema': 'site-program-registry-v1', 'programs': [provider, other],
        'site_profiles': {'glp-clinic-scout': {'auto_generate': False, 'auto_publish': False}}}))
    identities = tmp_path / 'identities.json'
    identities.write_text(json.dumps({'mappings': [{
        'network': 'Awin', 'program_id': '3277', 'selected_relationship': True,
        'registry_program_id': provider['program_id'], 'provider_slug': provider['review_slug']}]}))
    return type('Fixture', (), dict(lane=lane, notice=notice, capture=capture,
        verification=verification, registry=registry, identities=identities, fanout=fanout,
        root=tmp_path, provider=provider, other=other))()


def promote(f):
    import affiliate_program_withdrawal as withdrawal
    return withdrawal.promote_verified_notice(f.notice, f.verification, root=f.root)


def run_fanout(f, result):
    event = Path(result['terms_fanout_event']['path'])
    return f.fanout.consume(event, f.registry, f.identities, f.root / 'fanout', enabled=True, apply=True)


def test_verified_termination_reaches_withdrawn_registry_and_link_removal_candidate(withdrawal):
    f = withdrawal
    queue = promote(f)
    assert queue['kind'] == 'network_cancelled'
    assert queue['evidence']['decision_class'] == 'terminated'
    result = f.lane.run(queue)['results'][0]
    assert result['status'] == 'applied', json.dumps(result, indent=2)
    row = f.lane.service.rows[758]
    assert row[10] == 'cancelled_by_network'
    assert row[19] is False and row[20] is False
    assert row[14] == 'https://example.test/issued-link-never-followed'
    assert row[23].startswith(f.lane.source[23])
    out = run_fanout(f, result)
    assert out['status'] == 'PENDING_REVIEWED_SITE_RELEASE', out
    after = json.loads(f.registry.read_text())
    p = after['programs'][0]
    assert p['affiliate_program_state'] == 'withdrawn'
    assert p['affiliate_program_history'][0] == f.provider['affiliate_program_history'][0]
    assert p['affiliate_program_history'][-1]['decision_class'] == 'terminated'
    assert p['affiliate_terms']['old_fact'] == 'must remain'
    assert p['go']['entry_state'] == 'held'
    assert not any(p['profiles']['glp-clinic-scout']['surfaces'].values())
    assert after['programs'][1] == f.other
    # Execute native selectors, not a Python approximation of site filtering.
    fixture = ROOT / 'tests/fixtures/glp331_native_surface_build.mjs'
    public = f.root / 'offline-public'
    built = subprocess.run(['node', str(fixture), str(f.registry), str(public)],
                           text=True, capture_output=True)
    assert built.returncode == 0, built.stderr
    surfaces = json.loads((public / 'surface-projection.json').read_text())
    assert surfaces['reviews'] == ['/reviews/unrelated/']
    assert [r['program_id'] for r in surfaces['compare'][0]['rows']] == ['unrelated']
    assert [r['program_id'] for r in surfaces['quiz']['candidates']] == ['unrelated']
    assert all('synthetic-glp-provider' not in ids for ids in surfaces['sets'].values())


def test_unverified_notice_and_direct_writer_cannot_withdraw(withdrawal):
    f = withdrawal
    notice_result = f.lane.run(f.notice)['results'][0]
    assert notice_result['status'] == 'escalated'
    assert not f.lane.service.writes
    q = copy.deepcopy(f.lane.queue)
    q.update(kind='network_cancelled', update_id='unverified')
    q['evidence'] = {'path': str(f.root / 'notice.eml'), 'event_id': 'event-fixture', 'event_at_utc': WHEN}
    result = f.lane.run(q)['results'][0]
    assert result['status'] in ('refused', 'escalated'), result
    assert 'provider_verification' in result['reason']
    assert not f.lane.service.writes


def test_pause_even_with_capture_is_not_withdrawal(withdrawal):
    f = withdrawal
    f.notice['evidence']['decision_class'] = 'paused'
    with pytest.raises(ValueError, match='not_cancellation'):
        promote(f)
    assert not f.lane.service.writes


def test_unknown_provider_identity_cannot_withdraw(withdrawal):
    f = withdrawal
    f.notice['program_id'] = 'unknown'
    with pytest.raises(ValueError, match='identity'):
        promote(f)
    assert not f.lane.service.writes


def test_replayed_notice_has_no_duplicate_sheet_history_or_registry_effect(withdrawal):
    f = withdrawal
    q = promote(f)
    first = f.lane.run(q)['results'][0]
    out = run_fanout(f, first)
    sheet_after = copy.deepcopy(f.lane.service.rows)
    writes = len(f.lane.service.writes)
    second = f.lane.run(q)['results'][0]
    assert second['status'] == 'noop', second
    assert f.lane.service.rows == sheet_after
    assert len(f.lane.service.writes) == writes
    again = run_fanout(f, second)
    assert again['status'] == 'ALREADY_APPLIED', again
    assert len(json.loads(f.registry.read_text())['programs'][0]['affiliate_program_history']) == 2


@pytest.mark.parametrize('axis', ['hash', 'status', 'event', 'response_identity', 'missing'])
def test_provider_verification_is_hash_and_identity_bound(withdrawal, axis):
    f = withdrawal
    if axis == 'hash':
        f.verification['sha256'] = '0' * 64
    elif axis == 'missing':
        f.verification = {}
    else:
        capture = json.loads(f.capture.read_text())
        if axis == 'status':
            capture['relationship_status'] = capture['response']['relationship_status'] = 'paused'
        elif axis == 'event':
            capture['source_event_id'] = 'other-event'
        else:
            capture['response']['program_id'] = 'other-program'
        f.capture.write_text(json.dumps(capture))
        f.verification['sha256'] = digest(f.capture)
    with pytest.raises(ValueError):
        promote(f)
    assert not f.lane.service.writes


def test_canonical_notice_with_attached_verification_uses_same_queue_and_writer(withdrawal):
    f = withdrawal
    f.notice['evidence']['provider_verification'] = f.verification
    result = f.lane.run(f.notice)['results'][0]
    assert result['status'] == 'applied', json.dumps(result, indent=2)
    assert result['source_notice_update_id'] == f.notice['update_id']
    assert result['terms_fanout_event']


def test_cancelled_status_fanout_alone_is_not_withdrawal_authority(withdrawal):
    f = withdrawal
    # Bypass promotion with only mail: the legacy projection cannot authorize
    # withdrawal even if presented with a superficially passing writer receipt.
    receipt = {'captured_at_utc': WHEN}
    event = {'kind': 'network_cancelled', 'network': 'Awin', 'program_id': '3277',
             'update_id': 'legacy-unverified', 'writer_receipt': {'path': 'unused', 'sha256': '0' * 64},
             'queue_source': {'evidence': {}}}
    row = copy.deepcopy(f.lane.service.rows[758])
    row[10] = 'cancelled_by_network'
    from unittest.mock import patch
    with patch.object(f.fanout, 'validate_event', return_value=(receipt, row)):
        with pytest.raises(ValueError, match='provider_verification'):
            f.fanout.project(json.loads(f.registry.read_text()), json.loads(f.identities.read_text()), event)


def test_cancelled_and_terminated_share_narrow_withdrawal_mode(withdrawal):
    f = withdrawal
    data = json.loads(f.capture.read_text())
    data['relationship_status'] = data['response']['relationship_status'] = 'cancelled'
    f.capture.write_text(json.dumps(data))
    f.verification['sha256'] = digest(f.capture)
    f.notice['evidence']['decision_class'] = 'cancelled'
    result = f.lane.run(promote(f))['results'][0]
    assert result['status'] == 'applied', result
    run_fanout(f, result)
    history = json.loads(f.registry.read_text())['programs'][0]['affiliate_program_history']
    assert history[-1]['decision_class'] == 'cancelled'


def test_replayed_notice_with_refreshed_provider_capture_refuses_before_duplicate_write(withdrawal):
    f = withdrawal
    first = f.lane.run(promote(f))['results'][0]
    assert first['status'] == 'applied'
    run_fanout(f, first)
    after = copy.deepcopy(f.lane.service.rows)
    writes = len(f.lane.service.writes)
    proof = json.loads(f.capture.read_text())
    proof['observed_at_utc'] = '2026-10-01T02:01:00Z'
    refreshed = f.root / 'provider-recheck.json'
    refreshed.write_text(json.dumps(proof))
    f.verification = {'path': str(refreshed), 'sha256': digest(refreshed)}
    replay = f.lane.run(promote(f))['results'][0]
    assert replay['status'] in {'refused', 'escalated'}, replay
    assert 'replayed_notice_proof_conflict' in replay['reason']
    assert f.lane.service.rows == after
    assert len(f.lane.service.writes) == writes
