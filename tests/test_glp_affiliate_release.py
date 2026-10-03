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


def test_build_ci_design_and_hold_proofs_are_required(tmp_path):
    head='a'*40;rows=[{'file':'index.html','sha256':'b'*64}]
    records={'build':{'head':head,'exit_code':0,'files':rows},
      'ci':{'headRefOid':head,'statusCheckRollup':[{'status':'COMPLETED','conclusion':'SUCCESS'}]},
      'design':{'head':head,'exit_code':0,'checks':['affiliate-relationship-sync','raw-held-output','native-build-validators'],
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
