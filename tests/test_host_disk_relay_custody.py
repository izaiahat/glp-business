"""Synthetic relay/open-issue custody; no host effects or network."""
import json, sys, time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import host_disk_relay_custody as relay
import host_disk_cold_classes as cold
from test_glp110_archive_admission import ep

class Reader:
    def __init__(self,description): self.description=description
    def pages(self,*args):
        return [dict(identifier='GLP-125',description=self.description,state=dict(type='started'),team=dict(key='GLP'))]

def fixture(tmp_path):
    root=tmp_path/'reports/cold'
    root.mkdir(parents=True)
    proof=root/'DISTINCT-PREFLIGHT.json'
    proof.write_text('{}')
    job=tmp_path/'ops/gpt6pro-relay/jobs/one/JOB.json'
    job.parent.mkdir(parents=True)
    job.write_text(json.dumps(dict(proof_path=str(proof))))
    ledger=job.parent.parent.parent/'ledger.jsonl'
    ledger.write_text(json.dumps(dict(status='reserved',metadata=dict(required='reports/cold/MISSING-PROOF.json')))+'\n')
    return root,proof

def test_actual_archive_index_keeps_job_referenced_proof(tmp_path):
    root,proof=fixture(tmp_path)
    config=tmp_path/'current.json';config.write_text('{}')
    index=ep.ReferenceIndex([config],tmp_path)
    assert index.references(root)

def test_member_inventory_retains_ledger_and_open_issue_paths(tmp_path,monkeypatch):
    root,proof=fixture(tmp_path)
    other=root/'UNREFERENCED.txt';other.write_text('cold fixture only')
    issue=root/'OPEN-ISSUE.md';issue.write_text('held fixture')
    pins=relay.collect(bus=tmp_path,reader=Reader(str(issue)))
    rows,skipped=cold.inventory(root,tuple(pins),time.time()+1,time.monotonic()+30,hashes=True)
    assert {row['name'] for row in rows}=={'UNREFERENCED.txt'}
    assert str(proof) in skipped and str(issue) in skipped
    assert root/'MISSING-PROOF.json' in pins
    assert tmp_path/'reports/ops/system-resume-20261006' in pins
    assert tmp_path/'ops/gpt6pro-relay' in pins

def test_historical_run_member_sets_are_not_retired(tmp_path,monkeypatch):
    cases={
      'large-full-20261014T004733Z':['pro-admission-repair-20261007/EXACT-DISPOSITION.json','pro244-live-completion/ACCT1-PREFLIGHT.json'],
      'large-full-20261014T040350Z':['pro247-corrective-completion-20261007/DISPOSITION-FACTS.json','pro247-corrective-completion-20261007/DISTINCT-PREFLIGHT.json','pro-model-diagnosis-20261007/OBSERVATION.json']}
    root=tmp_path/'reports/ops/system-resume-20261006';root.mkdir(parents=True)
    job=tmp_path/'ops/gpt6pro-relay/jobs/reserved/JOB.json';job.parent.mkdir(parents=True)
    targets=[]
    for run,names in cases.items():
        for name in names:
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps({'target_evidence':[]}));targets.append(str(path))
    job.write_text(json.dumps({'status':'retry_blocked','terminal_unsent':{'reports':targets},'run_receipts':list(cases)}))
    ledger=job.parents[2]/'ledger.jsonl';ledger.write_text(json.dumps({'state':'APPLIED','job':str(job),'proofs':targets})+'\n')
    monkeypatch.setattr(__import__('host_disk_custody'),'collect',lambda:relay.collect(bus=tmp_path))
    class API:
        def path_is_active_privileged(self,path): return False
    row=cold.cold_archive(API(),root,tmp_path/'archive.tar.zst',dry_run=False,deadline=time.monotonic()+20,cold_days=7)
    assert row['status']=='REFUSED' and row['bytes_freed']==0
    assert all(Path(path).is_file() for path in targets)
    assert not (tmp_path/'archive.tar.zst').exists()


def test_job_and_ledger_drift_veto_actual_archive_index(tmp_path):
    root,proof=fixture(tmp_path)
    config=tmp_path/'current.json';config.write_text('{}')
    index=ep.ReferenceIndex([config],tmp_path)
    job=tmp_path/'ops/gpt6pro-relay/jobs/one/JOB.json'
    assert str(job) in index.relay_source_identities
    job.write_text('{"changed":true}')
    assert index.references(tmp_path/'reports/unreferenced')
    assert index.last_changed_source==str(job)


def test_lane_result_transitive_target_evidence(tmp_path):
    root,proof=fixture(tmp_path)
    nested=root/'NESTED.json';nested.write_text('{}')
    proof.write_text(json.dumps({'target_evidence':[{'path':str(nested),'sha256':'fixture'}]}))
    result=tmp_path/'lanes/OWNER/RESULT.json';result.parent.mkdir(parents=True);result.write_text(json.dumps({'proof':str(proof)}))
    pins=relay.collect(bus=tmp_path)
    assert proof in pins and nested in pins and str(nested) in pins.identities


def test_corrupt_ledger_refuses_inventory(tmp_path):
    import pytest
    fixture(tmp_path)
    ledger=tmp_path/'ops/gpt6pro-relay/ledger.jsonl'
    ledger.write_text('{not-json')
    with pytest.raises(ValueError): relay.collect(bus=tmp_path)
