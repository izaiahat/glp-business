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
