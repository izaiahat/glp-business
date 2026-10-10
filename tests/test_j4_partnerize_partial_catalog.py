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
