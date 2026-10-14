from types import SimpleNamespace
from scripts import affiliate_catalog_batch as batch


def test_curated_manifest_tracks_exact_git_cardinality():
    import json
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).split(b'\0')[:-1]
    manifest = json.loads((root / 'ops/rebuild-source-manifest.json').read_text())
    assert manifest['tracked_count'] == len(files)
    assert set(manifest['files']) | {'ops/rebuild-source-manifest.json'} == {x.decode() for x in files}


def test_failed_partnerize_bucket_preserves_other_real_readbacks():
    called = []
    def readback(transport, statuses):
        status, = statuses
        called.append(status)
        if status == 'rejected':
            raise TimeoutError('fixture')
        return {'programs': [{'program_id': status, 'name': status,
                              'relationship': status}], 'blockers': []}
    discovery = {'http_status': 200, 'advertiser_count': 2, 'advertisers': [
        {'advertiser_id': 'brand-only', 'name': 'Not a campaign ID'},
        {'advertiser_id': 'brand', 'campaign_id': 'campaign', 'name': 'Campaign'}]}
    adapter = SimpleNamespace(STATUSES=('approved','pending','rejected','t'),
                              readback=readback, discovery=lambda _: discovery)
    def payload(display, auth, programs, extra):
        return {'auth_state': auth, 'programs': programs, 'enumerated': len(programs), **extra}
    legacy = SimpleNamespace(partnerize=adapter,
                             b=SimpleNamespace(readback_payload=payload))
    result = batch.partnerize_inventory(legacy, None)
    assert called == list(adapter.STATUSES)
    assert {r['program_id'] for r in result['programs']} == {'approved','pending','t','campaign'}
    assert {r['program_id']:r['relationship'] for r in result['programs']}['t']=='t'
    assert result['reported_total'] is None
    assert result['blockers'] == [{'error_class':'partnerize_campaign_bucket_read_failed',
                                   'status':'rejected','exception_class':'TimeoutError'}]
    assert result['discovery_advertisers_without_campaign_id'] == 1


def test_partnerize_nested_campaign_identity_status_and_batch_receiving(monkeypatch, tmp_path):
    """Synthetic public-contract shape; real adapter and scheduled batch boundary."""
    from scripts.marketplace_adapters import partnerize, _base
    monkeypatch.setattr(partnerize, '_auth', lambda: ())
    campaigns = [{'id': cid, 'status': status, 'title': cid,
                  'default_destination': 'https://example.invalid/',
                  'promotional_countries': [{'iso': 'US'}],
                  'vertical': {'id': '01', 'name': 'Fixture'},
                  'currency': {'iso': 'USD'}}
                 for cid, status in [(' 001 ', 'AVAILABLE'), ('001', 'REQUESTED'),
                                     ('Rejected', 'REJECTED'), ('invited', 'INVITED')]]
    raw = {'count': 1, 'advertisers': [{'id': ' Brand ', 'display_name': 'Fixture brand',
                                      'description': 'Fixture description', 'campaigns': campaigns}]}
    transport = _base.RecordingTransport([
        ('GET', '/discovery/advertisers', _base.json_response(raw)),
        ('GET', '/campaign/approved', _base.json_response({'campaigns': [
            {'campaign_id': 'retained-only', 'title': 'Retained', 'advertiser_id': 'Brand'}]})),
        ('GET', '/campaign/pending', _base.json_response({'campaigns': [
            {'campaign_id': '001', 'title': 'Prior requested', 'advertiser_id': ' Brand '}]})),
        ('GET', '/campaign/rejected', _base.json_response({'error': 'unavailable'}, 503)),
        ('GET', '/campaign/t', _base.json_response({'campaigns': []}))])
    discovered = partnerize.discovery(transport)
    assert {r['campaign_id'] for r in discovered['advertisers']} == {c['id'] for c in campaigns}
    assert discovered['reported_advertiser_total'] == 1
    assert discovered['reported_campaign_total'] is None
    assert discovered['advertiser_count'] == 1
    assert discovered['campaign_count'] == 4
    legacy = SimpleNamespace(partnerize=partnerize, b=_base, SCHEMA='fixture', PORTAL_ONLY={})
    result = batch.partnerize_inventory(legacy, transport)
    rows = {r['program_id']: r for r in result['programs']}
    assert len(rows) == result['program_count'] == 5
    assert {cid: row['relationship'] for cid, row in rows.items()} == {
        'retained-only': 'approved', ' 001 ': 'notjoined', '001': 'pending',
        'Rejected': 'rejected', 'invited': 'invited'}
    for campaign in campaigns:
        row = rows[campaign['id']]
        assert row['network_status'] == campaign['status']
        assert row['advertiser_id'] == ' Brand '
        assert row['publisher_id'] == partnerize.PUBLISHER_ID
        assert row['region'] == 'US' and row['currency'] == 'USD'
        assert row['categories'] == ['Fixture']
        assert row['discovery_provenance']['endpoint'] == discovered['endpoint']
    assert result['joined_only_enumerated'] == 1
    assert result['discovery_campaign_count'] == 4
    assert result['discovery_advertiser_reported_total'] == 1
    assert result['reported_total'] is None
    assert result['blockers'][0]['status'] == 'rejected'
    legacy.verdict = lambda n, total, blockers: ('INCOMPLETE', 'no campaign denominator')
    legacy.SWEEPS = {'partnerize': lambda _: result}
    summary = batch.run(legacy, tmp_path / 'out', state=tmp_path / 'checkpoint.json')
    assert not summary['networks']['partnerize']['complete']
    assert all(call['method'] == 'GET' for call in transport.calls)
    # Malformed numeric identities are never coerced or promoted from a brand.
    malformed = _base.RecordingTransport([('GET', '/discovery/advertisers', _base.json_response(
        {'count': 1, 'advertisers': [{'id': 'brand', 'campaigns': [{'id': 1}]}]}))])
    refused = partnerize.discovery(malformed)
    assert refused['advertisers'] == []
    assert refused['blockers']


def test_partnerize_union_quarantines_conflicting_observations(monkeypatch):
    """RV493 synthetic counterexamples, not claims about the retained corpus."""
    from scripts.marketplace_adapters import partnerize, _base
    monkeypatch.setattr(partnerize, '_auth', lambda: ())

    def campaign(status):
        return {'id': ' same ', 'title': 'Synthetic', 'status': status}

    def receive(advertisers, retained=()):
        transport = _base.RecordingTransport([('GET', '/discovery/advertisers',
            _base.json_response({'count': len(advertisers), 'advertisers': advertisers}))])
        discovered = partnerize.discovery(transport)
        def bucket(_, statuses):
            status, = statuses
            return _base.readback_payload('Partnerize', 'PASS',
                [dict(row) for row in retained if row['relationship'] == status],
                {'blockers': []})
        adapter = SimpleNamespace(STATUSES=partnerize.STATUSES,
            PUBLISHER_ID=partnerize.PUBLISHER_ID, readback=bucket,
            discovery=lambda _: discovered)
        return batch.partnerize_inventory(SimpleNamespace(partnerize=adapter, b=_base), None)

    approved = {'program_id': ' same ', 'advertiser_id': ' Brand ',
                'relationship': 'approved', 'name': 'Retained original'}
    cases = {
        'contradictory_discovery_status': receive([{'id': ' Brand ', 'campaigns': [
            campaign('AVAILABLE'), campaign('REJECTED')]}]),
        'cross_advertiser_approval': receive([{'id': 'other-brand', 'campaigns': [
            campaign('REJECTED')]}], [approved]),
        'retained_bucket_collision': receive([], [approved,
            {**approved, 'relationship': 'rejected'}]),
    }
    # Exercise all three counterexamples even on RED, retaining their real rows.
    failures = []
    for label, result in cases.items():
        row, = result['programs']
        if not (result['blockers'] and row.get('catalog_conflict') is True
                and row['relationship'] == 'unknown'):
            failures.append((label, result['blockers'], row))
    assert not failures, failures
    for result in cases.values():
        row, = result['programs']
        assert result['program_count'] == 1 and row['program_id'] == ' same '
        assert result['reported_total'] is None
        assert len(row['catalog_observations']) == 2
        blocker, = result['blockers']
        assert blocker['error_class'] == 'partnerize_campaign_union_conflict'
        assert blocker['program_id'] == ' same '
        assert all(o['publisher_id'] == partnerize.PUBLISHER_ID
                   for o in row['catalog_observations'])
    status_obs = cases['contradictory_discovery_status']['programs'][0]['catalog_observations']
    assert [o['row']['network_status'] for o in status_obs] == ['AVAILABLE', 'REJECTED']
    assert all(o['surface'] == 'discovery' and o['row']['advertiser_id'] == ' Brand '
               and o['row']['discovery_provenance']['campaign_id'] == ' same '
               for o in status_obs)
    advertiser_obs = cases['cross_advertiser_approval']['programs'][0]['catalog_observations']
    assert [o['row']['advertiser_id'] for o in advertiser_obs] == [' Brand ', 'other-brand']
    assert advertiser_obs[0]['row'] == approved
    bucket_obs = cases['retained_bucket_collision']['programs'][0]['catalog_observations']
    assert [o['bucket'] for o in bucket_obs] == ['approved', 'rejected']
    assert [o['row']['relationship'] for o in bucket_obs] == ['approved', 'rejected']

    # Identical discovery duplicates and same-advertiser retained authority work.
    for retained, expected in [((), 'notjoined'), ([approved], 'approved')]:
        result = receive([{'id': ' Brand ', 'campaigns': [
            campaign('AVAILABLE'), campaign('AVAILABLE')]}], retained)
        row, = result['programs']
        assert not result['blockers'] and not row.get('catalog_conflict')
        assert row['relationship'] == expected and row['network_status'] == 'AVAILABLE'
        assert row['advertiser_id'] == ' Brand ' and row['program_id'] == ' same '
        assert result['discovery_campaign_count'] == 2
        assert result['discovery_campaign_unique_count'] == 1
        assert result['reported_total'] is None
    # Missing retained advertiser provenance is not a contradictory nonempty ID.
    result = receive([{'id': ' Brand ', 'campaigns': [campaign('AVAILABLE')]}],
                     [{**approved, 'advertiser_id': ''}])
    assert not result['blockers'] and result['programs'][0]['relationship'] == 'approved'
    result = receive([], [approved])
    assert not result['blockers'] and result['programs'] == [approved]
    assert result['joined_only_enumerated'] == 1


def test_digistore_vendor_zero_is_not_full_affiliate_catalog(monkeypatch):
    from scripts.marketplace_adapters import digistore24, _base
    monkeypatch.setattr(digistore24, '_headers', lambda: {})
    for data, expected in [({'entries': [], 'count': 0}, 0), ({'entries': []}, None)]:
        transport = _base.RecordingTransport([('GET', '/listMarketplaceEntries',
            _base.json_response({'result': 'success', 'data': data}))])
        out = digistore24.readback(transport)
        assert out['scope'] == 'vendor_owned_marketplace_entries'
        assert out['scoped_reported_total'] == expected
        assert out['reported_total'] is None and out['full_marketplace_total'] is None
        assert out['marketplace_model'] == 'vendor_owned_entries_plus_per_product_affiliation_readback'
        assert any(b['error_class'] == 'authorized_vault_browser_required' for b in out['blockers'])
        assert out['programs'] == [] and out['program_count'] == 0
        assert len(transport.calls) == 1 and transport.calls[0]['method'] == 'GET'
