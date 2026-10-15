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


def test_sole_private_registry_is_protected():
    root=Path('/home/ubuntu/business/site-system/flagship-v2/candidates/glp34-provider-families-20260912')
    assert cold.protected(root,())
    assert cold.protected(root/'data/approved-program-registry.json',())
    assert not cold.protected(root.parent/'ordinary-unreferenced-run',())


def test_new_lane_result_vetoes_admitted_index(tmp_path):
    config=tmp_path/'current.json';config.write_text('{}')
    lane=tmp_path/'lanes/EXISTING/RESULT.json';lane.parent.mkdir(parents=True);lane.write_text('{}')
    index=ep.ReferenceIndex([config],tmp_path)
    root=tmp_path/'reports/cold'
    assert not index.references(root)
    new=tmp_path/'lanes/NEW/RESULT.json';new.parent.mkdir();new.write_text(json.dumps({'proof':str(root/'proof.json')}))
    assert index.references(root), 'new lane discovery must veto the admitted index'


def test_new_linear_hold_vetoes_admitted_index(tmp_path,monkeypatch):
    config=tmp_path/'current.json';config.write_text('{}')
    reader=Reader('')
    original=relay.collect
    monkeypatch.setattr(relay,'collect',lambda **kw:original(**kw,reader=reader))
    index=ep.ReferenceIndex([config],tmp_path)
    root=tmp_path/'reports/cold'
    assert not index.references(root)
    reader.description=str(root/'proof.json')
    assert index.references(root), 'open Linear must be read fresh at each boundary'


def test_layout_literal_does_not_pin_every_reclaim_class(tmp_path):
    bus=Path('/home/ubuntu/business')
    pins=tuple(relay.text_pins(str(bus),bus))
    for name in ('site-system/flagship-v2/candidates/old-unreferenced',
                 'site-system/flagship-v2/qa/old-unreferenced',
                 'worktrees/merged-pushed','reports/cold-session-spill'):
        assert not cold.protected(bus/name,pins), name


def test_existing_lane_edit_vetoes_admitted_index(tmp_path):
    config=tmp_path/'current.json';config.write_text('{}')
    lane=tmp_path/'lanes/EXISTING/RESULT.json';lane.parent.mkdir(parents=True);lane.write_text('{}')
    index=ep.ReferenceIndex([config],tmp_path)
    assert not index.references(tmp_path/'reports/cold')
    lane.write_text(json.dumps({'proof':'reports/cold/proof.json'}))
    assert index.references(tmp_path/'reports/cold')


def test_large_guard_keeps_absent_namespace_control_eligible(tmp_path):
    from host_disk_reference_guard import ReferenceGuardAPI
    class API:
        def path_is_active_privileged(self,path): return False
    root=tmp_path/'lanes'
    guard=ReferenceGuardAPI(API(),{str(root):None})
    assert not guard.path_is_active_privileged(tmp_path/'unreferenced')
    root.mkdir()
    assert guard.path_is_active_privileged(tmp_path/'unreferenced')
    assert guard.last_reference_veto==str(root)


def test_lane_namespace_absence_and_nested_discovery_are_bound(tmp_path):
    pins=relay.collect(bus=tmp_path)
    root=tmp_path/'lanes'
    assert str(root) in pins.identities and pins.identities[str(root)] is None
    lane=root/'OWNER/nested/RESULT.json';lane.parent.mkdir(parents=True);lane.write_text('{}')
    pins=relay.collect(bus=tmp_path)
    for directory in (root,lane.parent.parent,lane.parent):
        assert pins.identities[str(directory)]==relay.signature(directory)


def test_linear_failure_at_fresh_gate_retains_candidate(tmp_path,monkeypatch):
    import host_disk_auto_classes as auto
    config=tmp_path/'current.json';config.write_text('{}')
    original=relay.collect
    reader=Reader('')
    monkeypatch.setattr(relay,'collect',lambda **kw:original(**kw,reader=reader))
    index=ep.ReferenceIndex([config],tmp_path)
    assert not index.references(tmp_path/'reports/cold')
    def refused(*a,**kw): raise RuntimeError('fixture incomplete pagination')
    monkeypatch.setattr(auto,'linear_pins',refused)
    assert index.references(tmp_path/'reports/cold')
    assert index.last_changed_source=='relay_lane_linear_refresh_incomplete'


def test_layout_only_result_reaches_real_cold_eligibility_but_exact_sibling_stays(tmp_path):
    root=tmp_path/'reports/cold';root.mkdir(parents=True)
    free=root/'free.txt';free.write_text('unreferenced fixture')
    held=root/'held.txt';held.write_text('held fixture')
    lane=tmp_path/'lanes/OWNER/RESULT.json';lane.parent.mkdir(parents=True)
    lane.write_text(json.dumps({'receiving_argv':[str(tmp_path)],'held':str(held)}))
    pins=tuple(relay.collect(bus=tmp_path))
    rows,skipped=cold.inventory(root,pins,time.time()+1,time.monotonic()+10,hashes=True)
    assert [row['name'] for row in rows]==['free.txt']
    assert str(held) in skipped


def test_explicit_policy_layout_root_is_not_dropped(tmp_path):
    lane=tmp_path/'lanes/OWNER/RESULT.json';lane.parent.mkdir(parents=True)
    lane.write_text(json.dumps({'protected_roots':[str(tmp_path)]}))
    pins=tuple(relay.collect(bus=tmp_path))
    assert cold.protected(tmp_path/'reports/cold/free.txt',pins)


def test_hold_arriving_during_archive_hash_vetoes_actual_c_commit(tmp_path,monkeypatch):
    """Real scratch compression/verification; synthetic Git/process/Linear only."""
    import shutil
    root=tmp_path/'reports/cold';root.mkdir(parents=True)
    proof=root/'ordinary.txt';proof.write_bytes(b'compressible scratch proof\n'*65536)
    old=time.time()-40*86400
    ep.os.utime(proof,(old,old))
    config=tmp_path/'current.json';config.write_text('{}')
    reader=Reader('');original=relay.collect
    monkeypatch.setattr(relay,'collect',lambda **kw:original(**kw,reader=reader))
    index=ep.ReferenceIndex([config],tmp_path)
    real_sha=ep.sha
    def hash_edge(path,deadline):
        digest=real_sha(path,deadline)
        if str(path).endswith('.tar.zst.partial'): reader.description=str(proof)
        return digest
    monkeypatch.setattr(ep,'sha',hash_edge)
    monkeypatch.setattr(ep,'git_veto',lambda *a:(False,{}))
    class API:
        def path_is_active_privileged(self,p): return False
        def remove_disposable(self,p,deadline):
            assert tmp_path in p.parents
            shutil.rmtree(p);return True
    row=ep.archive_tree(API(),root,index,dry_run=False,deadline=time.monotonic()+120)
    assert row['status']=='GATED_REFERENCE_BEFORE_COMMIT', row
    assert proof.read_bytes()==b'compressible scratch proof\n'*65536
    assert not root.with_name('.cold.archive-retiring').exists()


def test_newest_three_site_class_roots_remain_pinned(tmp_path):
    site=tmp_path/'site-system/flagship-v2'
    roots={}
    for kind in ('candidates','qa','releases','work'):
        roots[kind]=[]
        for number in range(6):
            root=site/kind/f'run-{number}';root.mkdir(parents=True)
            roots[kind].append(root)
    pins=tuple(relay.collect(bus=tmp_path))
    for candidates in roots.values():
        assert all(cold.protected(root/'ordinary.txt',pins) for root in candidates[-3:])
        assert all(not cold.protected(root/'ordinary.txt',pins) for root in candidates[:-3])


def test_transport_timeout_at_fresh_gate_keeps_originals(tmp_path,monkeypatch):
    import subprocess
    config=tmp_path/'current.json';config.write_text('{}')
    index=ep.ReferenceIndex([config],tmp_path)
    def failed(**kw): raise subprocess.TimeoutExpired('fixture',1)
    monkeypatch.setattr(relay,'collect',failed)
    assert index.references(tmp_path/'reports/cold')
    assert index.last_changed_source=='relay_lane_linear_refresh_incomplete'


def test_edit_during_live_refresh_cannot_replace_admitted_identity(tmp_path,monkeypatch):
    lane=tmp_path/'lanes/OWNER/RESULT.json';lane.parent.mkdir(parents=True);lane.write_text('{}')
    config=tmp_path/'current.json';config.write_text('{}')
    index=ep.ReferenceIndex([config],tmp_path)
    original=relay.collect
    def raced(**kw):
        lane.write_text('{"changed":true}')
        return original(**kw)
    monkeypatch.setattr(relay,'collect',raced)
    assert index.references(tmp_path/'reports/cold')
    assert index.last_changed_source==str(lane)


def test_internal_lane_directory_alias_is_bound_without_skipping_real_result(tmp_path):
    lane=tmp_path/'lanes/OWNER/real/RESULT.json';lane.parent.mkdir(parents=True)
    proof=tmp_path/'reports/cold/proof.json'
    lane.write_text(json.dumps({'proof':str(proof)}))
    alias=lane.parent.with_name('pytest-current');alias.symlink_to(lane.parent,target_is_directory=True)
    pins=relay.collect(bus=tmp_path)
    assert proof in pins and alias in pins
    assert pins.identities[str(alias)]==relay.signature(alias)
    assert str(lane) in pins.identities


def test_relay_walk_error_is_not_silently_omitted(tmp_path,monkeypatch):
    import pytest
    root=tmp_path/'ops/gpt6pro-relay';root.mkdir(parents=True)
    original=relay.os.walk
    def walk(path,**kw):
        if Path(path)==root:
            if kw.get('onerror'): kw['onerror'](PermissionError('fixture unreadable pending-job namespace'))
            return iter(())
        return original(path,**kw)
    monkeypatch.setattr(relay.os,'walk',walk)
    with pytest.raises(PermissionError): relay.collect(bus=tmp_path)


def test_corrupt_ledger_refuses_inventory(tmp_path):
    import pytest
    fixture(tmp_path)
    ledger=tmp_path/'ops/gpt6pro-relay/ledger.jsonl'
    ledger.write_text('{not-json')
    with pytest.raises(ValueError): relay.collect(bus=tmp_path)
