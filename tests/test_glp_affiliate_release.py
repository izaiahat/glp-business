import importlib.util
import json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('affiliate_release',Path(__file__).resolve().parents[1]/'scripts/glp_affiliate_release.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)


def test_operator_release_cannot_forge_authority_or_skip_build(tmp_path,monkeypatch):
    monkeypatch.setattr(release,'ROOT',tmp_path)
    with pytest.raises(ValueError,match='operator_authority_invalid'):
        release.validate_operator_release({'operator_authority':{'path':'other','sha256':'a'*64}}, {})


def test_ci_registration_gap_is_read_only_not_failure(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('affiliate_job',Path(__file__).resolve().parents[1]/'scripts/glp_affiliate_sync_job.py')
    job=importlib.util.module_from_spec(spec);spec.loader.exec_module(job)
    records=[{'headRefOid':'a'*40,'statusCheckRollup':[]},
             {'headRefOid':'a'*40,'statusCheckRollup':[{'status':'IN_PROGRESS'}]},
             {'state':'MERGED','headRefOid':'a'*40,'statusCheckRollup':[{'status':'COMPLETED','conclusion':'SUCCESS'}]}]
    calls=[]
    def run(argv,**kw):
        calls.append(argv)
        if 'checks' in argv:return ''
        return json.dumps(records.pop(0))
    monkeypatch.setattr(job,'run',run);monkeypatch.setattr(job.time,'sleep',lambda _:None)
    monkeypatch.setattr(job,'save',lambda *a:None)
    assert job.ci('28',tmp_path)['state']=='MERGED'
    assert len(calls)==4
    assert all('create' not in c and 'merge' not in c for c in calls)


@pytest.fixture
def hourly_resume(tmp_path, monkeypatch):
    """Run real main/selection/execute/ci; deny all external effects and retention."""
    import subprocess
    import sys
    import types

    spec = importlib.util.spec_from_file_location(
        'f03_job', Path(__file__).resolve().parents[1] / 'scripts/glp_affiliate_sync_job.py')
    job = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(job)
    root = tmp_path / 'business'
    runs = root / 'reports/glp-clinic-scout/affiliate-machine/registry-site-sync'
    prior = runs / '20261012T001701Z'
    prior.mkdir(parents=True)
    head = '9f1459e94e5bc7a71b831824b21d2412ce1aa1a8'
    pr = 'https://github.com/izaiahat/glp-site-candidate/pull/63'
    (prior / 'source-intent.json').write_text(json.dumps({'head': head}))
    (prior / 'PR.json').write_text(json.dumps({'url': pr}))
    config = tmp_path / 'config.json'
    config.write_text('{}')
    report = runs / '20261015T181706Z'
    events = []
    checks = [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]
    record = {'state': 'MERGED', 'headRefOid': head,
              'mergeCommit': {'oid': 'b' * 40}, 'statusCheckRollup': checks}

    def denied(*args, **kwargs):
        raise AssertionError('external effect denied')

    def run(argv, **kwargs):
        if argv[:3] == ['gh', 'pr', 'view']:
            assert argv[3] == pr and argv[4:6] == ['--repo', job.REPO]
            events.append('ci-read')
            return json.dumps(record)
        if argv[:3] == ['gh', 'pr', 'checks']:
            assert argv[3] == pr and '--watch' in argv
            events.append('ci-watch')
            return ''
        if argv[:2] == ['git', 'clone']:
            events.append('clone')
            source = Path(argv[-1])
            source.mkdir()
            # Allocate a small real stand-in, never a production/network clone.
            (source / 'fixture-source').write_bytes(b'x' * 4096)
            return ''
        if argv[:2] == ['git', 'fetch']:
            events.append('fetch')
            return ''
        if argv == ['git', 'checkout', '--detach', head]:
            return ''
        if argv[:2] == ['git', 'rev-parse']:
            return head if argv[-1] == 'HEAD' else 'same-tree'
        denied(argv)

    registry = types.ModuleType('glp_affiliate_registry_sync')
    registry.sync = lambda *args: {'changed': 0}
    publisher = types.ModuleType('glp_release_main')
    publisher.main_pre_effect_refusal = denied
    monkeypatch.setitem(sys.modules, 'glp_affiliate_registry_sync', registry)
    monkeypatch.setitem(sys.modules, 'glp_release_main', publisher)
    monkeypatch.setattr(subprocess, 'run', denied)
    monkeypatch.setattr(subprocess, 'check_output', denied)
    monkeypatch.setattr(job, 'ROOT', root)
    monkeypatch.setattr(job, 'run', run)
    monkeypatch.setattr(job.time, 'sleep', lambda _: None)
    monkeypatch.setattr(job, 'save', lambda p, value: Path(p).write_text(json.dumps(value)))
    monkeypatch.setattr(job, 'retire_disposable_clones', denied)
    monkeypatch.setattr(sys, 'argv', ['f03', '--config', str(config), '--output', str(report)])
    return job, report, record, events, prior


@pytest.mark.parametrize('nonpass', ['failed', 'pending', 'missing', 'wrong-head'])
def test_hourly_resume_ci_refuses_before_source_allocation(hourly_resume, nonpass):
    job, report, record, events, _ = hourly_resume
    error = 'exact_ci_nonpass'
    if nonpass == 'failed':
        record['statusCheckRollup'][0]['conclusion'] = 'FAILURE'
    elif nonpass == 'pending':
        record['statusCheckRollup'][0]['status'] = 'IN_PROGRESS'
    elif nonpass == 'missing':
        record['statusCheckRollup'] = []
        error = 'ci_registration_timeout'
    else:
        record['headRefOid'] = 'c' * 40
        error = 'source_pr_head_mismatch'
    with pytest.raises(RuntimeError, match=error):
        job.main()
    allocated = sum(p.stat().st_blocks * 512 for p in report.rglob('*') if p.is_file())
    print(f'F03 {nonpass}: candidate={(report / "candidate").exists()} allocated_bytes={allocated} events={events}')
    assert 'clone' not in events and 'fetch' not in events
    assert not (report / 'candidate').exists()
    receipt = json.loads((report / 'FAILURE.json').read_text())
    assert receipt['error'] == error and (report / 'FAILURE.json').stat().st_size < 4096


def test_hourly_unchanged_green_resume_still_completes(hourly_resume):
    job, report, record, events, _ = hourly_resume
    assert job.main() == 0
    result = json.loads((report / 'RESULT.json').read_text())
    assert result['status'] == 'PASS_SOURCE_MERGED'
    assert result['source_head'] == record['headRefOid'] and result['deployed'] is False
    assert events.count('clone') == 1 and events.count('ci-watch') == 2
    assert events.index('ci-watch') < events.index('clone') < events.index('fetch')


def test_hourly_fresh_no_change_keeps_existing_flow(hourly_resume):
    job, report, _, events, prior = hourly_resume
    (prior / 'PR.json').unlink()
    assert job.main() == 0
    assert json.loads((report / 'RESULT.json').read_text())['status'] == 'PASS_SOURCE_NO_CHANGE'
    assert events == ['clone']


def test_build_ci_design_and_hold_proofs_are_required(tmp_path):
    head='a'*40;rows=[{'file':'index.html','sha256':'b'*64}]
    absence=tmp_path/'absence.json'
    absence.write_text(json.dumps({'status':'PASS','unaccounted':0,'remaining_unmapped':0,'evidenced_no_live_offer_presence':0,'rows':[]}))
    records={'build':{'head':head,'exit_code':0,'files':rows},
      'ci':{'state':'MERGED','headRefOid':head,'statusCheckRollup':[{'status':'COMPLETED','conclusion':'SUCCESS'}]},
      'design':{'head':head,'exit_code':0,'checks':['affiliate-relationship-sync','raw-held-output','native-build-validators'],
                'unmapped_adverse_evidence':release.pin(absence),
                'raw_held':{'held':[{'id':'fella-health'}],'htmlFiles':1,'jsonFiles':1,'anchors':[],'routes':[]}}}
    def gate():
        refs={}
        for k,v in records.items():
            p=tmp_path/(k+'.json');p.write_text(json.dumps(v));refs[k]=release.pin(p)
        return {'head':head,'scope':'operator-approved-affiliate-sync','release_authorized':False,'evidence':refs}
    release.validate_build_evidence(gate(),rows)
    records['ci']['statusCheckRollup'][0]['conclusion']='FAILURE'
    with pytest.raises(ValueError,match='ci_not_green'):release.validate_build_evidence(gate(),rows)
    records['ci']['statusCheckRollup'][0]['conclusion']='SUCCESS'
    records['design']['raw_held']['anchors']=[{'id':'fella-health'}]
    with pytest.raises(ValueError,match='hold_census_nonpass'):release.validate_build_evidence(gate(),rows)
    records['design']['raw_held']['anchors']=[]
    g=gate();(tmp_path/'design.json').write_text('{}')
    with pytest.raises(ValueError,match='evidence_drift'):release.validate_build_evidence(g,rows)
