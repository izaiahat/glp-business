"""Offline synthetic controls: never use these fixtures as live completion evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import linear_business_proof as b
import fleet_ingest as f
from prepare_business_proof_plan import transform


def test_exact_read_only_allowlist():
    for issue in b.VERIFIERS:
        cmd = ['python3','-B','scripts/linear_business_proof.py',issue]
        assert f.proof_argv(cmd) == cmd
    for command in [
        ['python3','-B','scripts/linear_business_proof.py','GLP-1'],
        ['python3','-B','scripts/linear_business_proof.py','GLP-125','--apply'],
        ['python3','scripts/linear_close.py','GLP-125','worker.json','--check-only'],
        ['sh','-c','python3 -B scripts/linear_business_proof.py GLP-125'],
        ['python3','-B','scripts/linear_business_proof.py','GLP-125;touch x'],
    ]:
        with pytest.raises(ValueError):
            f.proof_argv(command)


@pytest.mark.parametrize('issue', sorted(b.VERIFIERS))
def test_review_or_missing_output_never_equals_completion(tmp_path, monkeypatch, issue):
    worker = tmp_path/'worker.json'
    worker.write_text(json.dumps({'issue':issue,'status':'ACCEPT','verdict':'ACCEPT','source_accepted':True}))
    row = {'issue':issue,'worker_result':'worker.json','acceptance_rewrite':'Exact live business criterion.'}
    monkeypatch.setattr(b,'live_issue',lambda _: {'description':'## Acceptance\n\nExact live business criterion.'})
    monkeypatch.setattr(b,'gh',lambda *a: {'state':'OPEN','mergeCommit':None})
    monkeypatch.setattr(b.Proof,'cron',lambda *a: {'enabled':False,'state':'paused'})
    monkeypatch.setattr(b.Proof,'onboard_live',lambda *a: (_ for _ in ()).throw(b.Refused('no live delivery')))
    monkeypatch.setattr(b,'ops_mail',lambda: (_ for _ in ()).throw(b.Refused('no real mail')))
    monkeypatch.setattr(b.shutil,'disk_usage',lambda _: SimpleNamespace(free=0))
    # Nu Image's adapter cannot import live credential-bearing code in this test.
    if issue == 'GLP-125':
        monkeypatch.setitem(b.VERIFIERS,issue,lambda p: (_ for _ in ()).throw(b.Refused('SENT absent')))
    with pytest.raises((b.Refused, KeyError)):
        b.run(issue,root=tmp_path,plan={'issues':[row]})
    assert not list(tmp_path.rglob('__pycache__'))


def test_current_acceptance_is_queried_not_cached(tmp_path, monkeypatch):
    row = {'issue':'GLP-125','acceptance_rewrite':'old criterion','worker_result':'missing.json'}
    monkeypatch.setattr(b,'live_issue',lambda _: {'description':'## Acceptance\n\nnew criterion'})
    with pytest.raises(b.Refused, match='current Linear acceptance changed'):
        b.run('GLP-125',root=tmp_path,plan={'issues':[row]})


def test_locator_requires_native_output_exact_bytes_not_accept(tmp_path):
    (tmp_path/'worker.json').write_text('{}')
    p = b.Proof('GLP-125', {'worker_result':'worker.json'},tmp_path)
    artifact = tmp_path/'reports/ops/native/data.json'
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"real_rows":1}')
    p.inputs['native'] = {'path':str(artifact),'sha256':b.digest(artifact.read_bytes())}
    assert p.native('native','reports/ops/native')['real_rows'] == 1
    artifact.write_text('{"real_rows":2}')
    with pytest.raises(b.Refused,match='bytes changed'):
        p.native('native','reports/ops/native')
    accept = artifact.with_name('ACCEPT.json')
    accept.write_text('{"status":"ACCEPT"}')
    p.inputs['native'] = {'path':str(accept),'sha256':b.digest(accept.read_bytes())}
    with pytest.raises(b.Refused,match='review is not business output'):
        p.native('native','reports/ops/native')


def test_no_symlink_or_path_escape(tmp_path):
    outside = tmp_path.parent/'outside-proof-test.json'
    outside.write_text('{}')
    (tmp_path/'link.json').symlink_to(outside)
    with pytest.raises(b.Refused,match='symlink'):
        b.confined(tmp_path,'link.json')
    with pytest.raises(b.Refused,match='not a workspace file'):
        b.confined(tmp_path,'../outside-proof-test.json')


@pytest.mark.parametrize('route',['//example.org/x','/go/abc?click=1','/../secret','https://example.org'])
def test_no_tracking_or_offsite_get(route):
    with pytest.raises(b.Refused,match='noncanonical'):
        b.http(route)


class FakeProof:
    def __init__(self, outputs=None):
        self.outputs=outputs or {}
        self.observed={}
        self.root=Path('/synthetic')
        self.inputs={}
    def native(self,name,*args):
        assert name in self.outputs, 'synthetic output locator not provided: '+name
        return self.outputs[name]
    def reviews(self,scopes):
        self.observed['external_acceptance_gates']=list(scopes)
    def pr(self,*args):
        return {'headRefOid':'a'*40}
    def page(self,spec):
        return 'savings sponsor disclosure $750 $250 $500'
    def cron(self,*args):
        return {'enabled':True,'state':'scheduled'}
    def no_effects(self,r):
        assert r['effects']=={'tracking_requests':0,'provider_writes':0}
    def sheet(self):
        return [['synthetic-sheet']]


def test_pruner_actual_reclaim_and_subsequent_scheduled_cycle(monkeypatch):
    monkeypatch.setattr(b,'disk_floor',lambda p: b.FLOOR)
    monkeypatch.setattr(b,'confined',lambda *a: SimpleNamespace(read_bytes=lambda:b'allowlist'))
    first={'invocation':'manual','status':'COMPLETE','partial':False,'protected_removed_count':0,
           'active_removed_count':0,'registered_entrypoint':'host_disk_pruner.py','allowlist_sha256':b.digest(b'allowlist'),
           'allowlist_path':'allowlist.json','free_bytes_after':b.FLOOR,'actual_freed_bytes':123,
           'started_utc':'2026-10-16T01:00:00Z','completed_utc':'2026-10-16T02:00:00Z'}
    second={**first,'invocation':'scheduled','started_utc':'2026-10-16T03:00:00Z','actual_freed_bytes':0}
    p=FakeProof({'manual_prune':first,'scheduled_prune':second})
    assert b.pruner(p)['freed_bytes']==123
    second['partial']=True
    with pytest.raises(b.Refused,match='incomplete/unsafe'):
        b.pruner(p)
    second['partial']=False
    first['actual_freed_bytes']=0
    with pytest.raises(b.Refused,match='actual reclaim'):
        b.pruner(p)


def test_sponsor_served_output_terms_and_both_renders():
    pages=[{'route':'/sponsor','kind':'rate_card','sha256':'a'},
           {'route':'/example','kind':'example_placement','sha256':'b'}]
    output={'source_pr':20,'pages':pages,'terms':{'initial_usd':750,'setup_usd':250,'placement_usd':500,'affirmative_renewal_usd':500},
            'renders':[{'viewport':v,'http':200,'page_sha256':sha} for v,sha in [('desktop','a'),('mobile','b')]],
            'editorial_ranking_before_sha256':'r','editorial_ranking_after_sha256':'r',
            'holds_before_sha256':'h','holds_after_sha256':'h'}
    p=FakeProof({'site_output':output})
    assert b.sponsor_site(p)['external_acceptance_gates']==['design']
    output['terms']['initial_usd']=751
    with pytest.raises(b.Refused,match='economics'):
        b.sponsor_site(p)


def test_unknown_financial_coverage_never_proven_zero():
    report={'conversion_reconciliation':{'coverage_complete':False,'input_completeness':'UNKNOWN',
             'provenance':'SUPPLIED_LOCAL_EVIDENCE_NOT_VERIFIED_PRODUCTION_DATA','rows':[]}}
    p=FakeProof({'reconciliation':report})
    with pytest.raises(b.Refused,match='UNKNOWN, not a zero'):
        b.conversions(p)


def test_true_network_zero_and_superseded_attempt_refusal():
    report={'conversion_reconciliation':{'coverage_complete':True,'input_completeness':'COMPLETE',
             'provenance':'AUTHENTICATED_PRODUCTION_DATA','rows':[],'network_proven_zero':True,'test_rows_in_financial_totals':0},
             'completed_at':'2026-10-16T02:00:00Z','effects':{'tracking_requests':0,'provider_writes':0}}
    attempt={'status':'complete','attempt_sha256':'f'*64,'preclaim_source_pr':560,'fresh_attempt_count':1,'started_at':'2026-10-16T01:00:00Z'}
    p=FakeProof({'reconciliation':report,'fresh_attempt':attempt})
    assert b.conversions(p)['real_lineage_rows']==0
    attempt['attempt_sha256']='e4678e3d'+'0'*56
    with pytest.raises(b.Refused,match='old effect never replayed'):
        b.conversions(p)


def test_plan_exact_fourteen_business_not_preflight_bindings():
    p=json.loads((ROOT/'ops/linear/PLAN.json').read_text())
    assert len(p['issues'])==14 and {r['issue'] for r in p['issues']}==set(b.VERIFIERS)
    for row in p['issues']:
        assert row['proof_cmd']==['python3','-B','scripts/linear_business_proof.py',row['issue']]
        assert row['proof_cwd']=='/home/ubuntu/business' and row['proof_timeout_s']==240
        assert 'Read-only proof:' not in row['acceptance_rewrite']
    rows={r['issue']:r for r in p['issues']}
    assert 'SEO-ACCEPT' not in rows['GLP-320']['worker_result']
    for issue in ('GLP-125','GLP-88','GLP-250','GLP-253'):
        worker=json.loads((ROOT/rows[issue]['worker_result']).read_text())
        assert worker['issue']==issue
        assert worker['status'] in ('REDISPATCH_NEEDED','DONE_READY_TO_CLOSE')
