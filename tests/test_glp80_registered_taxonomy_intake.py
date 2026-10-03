"""Synthetic projection tests; no provider requests, approvals or live adoption."""
import copy
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / 'ops/glp/evidence-refresh-k3/registered_taxonomy.py'
    spec = importlib.util.spec_from_file_location('registered_taxonomy_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_only_verified_taxonomy_metadata_changes_and_prices_stay_frozen():
    m = module()
    row = {'program_id': 'fixture', 'molecule_tags': ['semaglutide'],
           'commercial_link_state': 'held', 'classification_state': 'verified',
           'verified_as_of': '2026-09-08', 'source_urls': ['https://example.test/'],
           'evidence_bindings': [{'authority': 'prior'}], 'normalized_sha256': 'old'}
    base = {'programs': [{'program_id': 'fixture', 'commission': None}],
            'therapeutic_taxonomy': {'programs': {'fixture': row}, 'authorities': {'prior': {'sha256': 'old'}}},
            'provider_dynamic_profiles': {'profiles': {'fixture': {'calculator': {'checked_as_of': '2026-09-08', 'mode': 'manual'}, 'verified_as_of': '2026-09-08'}}}}
    refreshed = copy.deepcopy(row)
    refreshed.update(verified_as_of='2026-10-03', evidence_bindings=[{'authority': 'captured'}], normalized_sha256='new')
    before = copy.deepcopy(base)
    result = m.project_verified_row(base, 'fixture', refreshed, 'captured', {'sha256': 'real-capture-fixture'})
    assert base == before
    assert result['programs'] == before['programs']
    assert result['provider_dynamic_profiles'] == before['provider_dynamic_profiles']
    assert result['therapeutic_taxonomy']['programs']['fixture']['verified_as_of'] == '2026-10-03'
    refreshed['commercial_link_state'] = 'active'
    with pytest.raises(ValueError, match='FACT_OR_AUTHORITY_CHANGE'):
        m.project_verified_row(base, 'fixture', refreshed, 'captured', {})
    refreshed['commercial_link_state'] = 'held'
    refreshed['molecule_tags'].append('tirzepatide')
    with pytest.raises(ValueError, match='FACT_OR_AUTHORITY_CHANGE'):
        m.project_verified_row(base, 'fixture', refreshed, 'captured', {})


def test_native_proof_requires_written_row_capture_and_current_policy(tmp_path):
    import hashlib, json, types
    m = module()
    script = ROOT / 'site-system/flagship-v2/candidates/glp34-provider-families-20260912/scripts/refresh-therapeutic-taxonomy.py'
    policies = script.with_name('therapeutic-taxonomy-refresh-policies.json')
    d = types.SimpleNamespace(ROOT=tmp_path, TAXONOMY_PRODUCER=script,
        TAXONOMY_POLICIES=policies, TAXONOMY_REGISTRY=tmp_path / 'registry.json')
    d.sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    d.read = lambda p: json.loads(Path(p).read_text())
    er = types.SimpleNamespace(safe_url=lambda url, hosts: url.startswith('https://www.breezemeds.com/'))
    def put(p, x):
        p.parent.mkdir(parents=True, exist_ok=True);p.write_text(json.dumps(x));return p
    pid = 'breeze-meds-review'
    policy = json.loads(policies.read_text())[pid]
    row = {f:list(policy['quotes'].get(f,{})) for f in m.FIELDS}
    row.update(program_id=pid, verified_as_of='2026-10-03', normalized_sha256='fixture-normalized',
               source_urls=[policy['url']], evidence_bindings=[{'authority':'taxonomy_refresh_fixture'}])
    folder = tmp_path / 'reports/ops/glp38-20260915/taxonomy-refresh/run-fixture'
    text = ('\n' + '.' * 300 + '\n').join(q for values in policy['quotes'].values() for q in values.values())
    raw = put(folder / (pid+'-primary.json'), {'request': {'url':policy['url'], 'maxAge':0},
        'started_at':'2026-10-03T00:00:00Z','received_at':'2026-10-03T00:00:01Z',
        'response':{'success':True,'data':{'markdown':text,'metadata':{'statusCode':200,'url':policy['url']}}}})
    manifest = put(folder / (pid+'-verification.json'), {'program_id':pid,
        'source_url':policy['url'],'captured_at':'2026-10-03T00:00:01Z','verified_as_of':'2026-10-03',
        'capture_path':str(raw.relative_to(tmp_path)),'capture_sha256':d.sha(raw),
        'tags_unchanged':{f:row[f] for f in m.FIELDS}})
    authority={'path':str(manifest.relative_to(tmp_path)),'sha256':d.sha(manifest)}
    row['evidence_bindings']=[{'authority':'taxonomy_refresh_fixture','authority_sha256':authority['sha256'],
        'source_url':policy['url'],'verified_as_of':'2026-10-03','evidence_sha256':d.sha(raw)}]
    put(d.TAXONOMY_REGISTRY, {'therapeutic_taxonomy':{'programs':{pid:row},'authorities':{'taxonomy_refresh_fixture':authority}}})
    result = put(folder/'RESULT.json', {'producer':str(script),'status':'REVIEW_REQUIRED',
        'written':[{'program_id':pid,'verified_as_of':'2026-10-03','normalized_sha256':'fixture-normalized'}]})
    body=tmp_path/'body.md';body.write_text(text)
    caps={'fixture':{'source_url':policy['url'],'raw_capture_path':str(raw),'raw_capture_sha256':d.sha(raw),
          'markdown_path':str(body),'markdown_sha256':d.sha(body),'captured_at_utc':'2026-10-03T00:00:01Z'}}
    proof=m.verified_row(d,er,pid,row,{'source_urls':[policy['url']]},caps,'2026-10-03')
    assert proof[0]==row and proof[1]=='taxonomy_refresh_fixture'
    put(result, {'producer':str(script),'status':'BLOCKED','written':[]})
    assert m.verified_row(d,er,pid,row,{'source_urls':[policy['url']]},caps,'2026-10-03') is None
    put(result, {'producer':str(script),'status':'PASS','written':[{'program_id':pid,'verified_as_of':'2026-10-03','normalized_sha256':'fixture-normalized'}]})
    raw.write_text(raw.read_text()+' ')
    with pytest.raises(ValueError,match='CAPTURE_DRIFT'):
        m.verified_row(d,er,pid,row,{'source_urls':[policy['url']]},caps,'2026-10-03')


def test_contradictory_taxonomy_review_cannot_be_overridden(tmp_path):
    m = module()
    decision = {'facts': [{'path':'profile.taxonomy.molecule_tags', 'status':'CHANGED',
                          'stored_value':['semaglutide'], 'current_value':[]}]}
    assert m.make_packet(None, None, tmp_path, 'fixture', tmp_path/'out', {}, '2026-10-03', decision) is None


@pytest.mark.parametrize('composition_status,expected', [('COMPOSED_PENDING_GOVERNED_ADOPTION','TAXONOMY_INTAKE_READY'), ('BLOCKED_PRICING_VALIDATION','BLOCKED_PRICING_VALIDATION')])
def test_stage_reports_actual_composition_not_scheduler_success(tmp_path, monkeypatch, composition_status, expected):
    import json, types
    path=ROOT/'ops/glp/evidence-refresh-k3/daily_evidence_refresh.py'
    spec=importlib.util.spec_from_file_location('stage_scope_test',path);d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
    release=tmp_path/'release';(release/'data').mkdir(parents=True)
    (release/'data/program-registry.json').write_text('{"therapeutic_taxonomy":{}}')
    bound={'pointer':{'release_root':str(release)}}
    monkeypatch.setattr(d,'check_pins',lambda:{'fixture':'pins'})
    monkeypatch.setattr(d,'output_root',lambda p:Path(p))
    monkeypatch.setattr(d,'binding',lambda:bound)
    monkeypatch.setattr(d,'unchanged',lambda b:None)
    monkeypatch.setattr(d,'capture_input_snapshot',lambda day:{})
    engine=types.SimpleNamespace(select_stale=lambda *a:[{'program_id':'fixture'}])
    composer=types.SimpleNamespace(compose=lambda *a:{'status':composition_status,'combined_patch':None})
    monkeypatch.setattr(d,'load',lambda p,n:engine if p==d.ENGINE else composer)
    def stage(er,root,pid,folder,*a):
        d.put(folder/'PACKET.json',{'program_id':pid,'status':'PACKET_READY'})
        return {'program_id':pid,'status':'PACKET_READY','adjudication_scope':'taxonomy_only'}
    monkeypatch.setattr(d,'stage_program',stage)
    result=d.run_daily(tmp_path/'stage','2026-10-03T02:00:00Z')
    assert result['status']==expected and result['registered_taxonomy_ready_count']==1
    assert result['candidate_writes']==0 and result['network_requests']==0
