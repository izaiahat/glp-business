"""Copied source provenance remains exact and native-author authenticated."""
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import article_review_recovery as r

def test_copied_authorship_requires_exact_subject_and_native_author(tmp_path):
    fresh=tmp_path/'reports/fresh';old=tmp_path/'reports/origin';fresh.mkdir(parents=True);old.mkdir()
    name='04-draft.md'
    (fresh/name).write_text('exact reviewed source')
    (old/name).write_bytes((fresh/name).read_bytes())
    seen=[]
    def native(db,paths,reviewer,when):
        seen.extend(paths);return [{'session_id':'actual-distinct-writer'}]
    args=(fresh,[name],{name:r.spec(old/name)},tmp_path,None,{},123)
    assert r.copied_source_authors(*args,authenticate=native)[0]['session_id']=='actual-distinct-writer'
    assert seen==[old/name]
    (old/name).write_text('not the sealed bytes')
    with pytest.raises(ValueError):r.copied_source_authors(*args,authenticate=native)
    changed=(fresh,[name],{name:r.spec(old/name)},tmp_path,None,{},123)
    with pytest.raises(ValueError,match='copied_source_bytes'):
        r.copied_source_authors(*changed,authenticate=native)

@pytest.mark.parametrize('bad', ['outside','wrong-name','symlink','missing','relative'])
def test_copied_origin_shape_refuses_before_auth(tmp_path,bad):
    f=tmp_path/'reports/fresh';f.mkdir(parents=True);p=f/'04-draft.md';p.write_text('same')
    q=tmp_path/'04-draft.md';q.write_text('same')
    if bad=='wrong-name':q=f/'other.md';q.write_text('same')
    if bad=='symlink':q=f/'alias';q.symlink_to(p)
    origin={'path':str(q),'sha256':r.sha(p)}
    if bad=='missing':origin['path']=str(f/'missing')
    if bad=='relative':origin['path']='reports/fresh/04-draft.md'
    with pytest.raises((ValueError,FileNotFoundError)):
        r.copied_source_authors(f,[p.name],{p.name:origin},tmp_path,None,{},123,
            authenticate=lambda *a:pytest.fail('native auth reached bad origin'))


def test_origin_provenance_is_native_bound(tmp_path,monkeypatch):
    import json,sys,types
    f=tmp_path/'review/packet';f.mkdir(parents=True);(f/'04-draft.md').write_text('same')
    h=f.parent/'history';h.mkdir()
    m={'subject_files':[{'path':'04-draft.md','sha256':r.sha(f/'04-draft.md')}],
       'sealed_at_utc':'2026-10-01T00:00:00Z'}
    from hashlib import sha256
    def payload_hash(v,key):return sha256(json.dumps({k:x for k,x in v.items() if k!=key},sort_keys=True).encode()).hexdigest()
    m['manifest_sha256']=payload_hash(m,'manifest_sha256')
    (h/'manifest.json').write_text(json.dumps(m))
    _check_native_origin(tmp_path,monkeypatch,f,h,m,payload_hash)


def _check_native_origin(tmp_path,monkeypatch,f,h,m,payload_hash):
    import json,sys,types
    # Minimal offline native transport, never a real runtime acceptance fixture.
    inputs={'context_files':[{'path':'fixture'}],'max_context_chars':1000,'max_total_chars':2000}
    runtime={'model':'fixture','provider':'fixture','effort':'high','session_id':'fixture','result_custody':{'started_at':1790812801,'ended_at':1790812802}}
    raw={'review_input':inputs,'hermes_runtime':runtime,'result':'fixture result'}
    (h/'raw.json').write_text(json.dumps(raw))
    import sys,types
    header='SEALED ARTICLE SUBJECTS — READ ONLY\nmanifest_sha256='+m['manifest_sha256']+'\n'
    def build(args,captured,schema):captured['context_files']=[{'content':header}];return 'native input'
    transport=types.SimpleNamespace(build_payload=build,load_schema=lambda *a:None,
        review_input=lambda *a:inputs,authenticate_result=lambda *a,**kw:runtime['result_custody'])
    monkeypatch.setitem(sys.modules,'hermes_sol_article_review',transport)
    monkeypatch.setitem(sys.modules,'opus_review_loop',types.SimpleNamespace(payload_hash=payload_hash))
    proof={'manifest':r.spec(h/'manifest.json'),'raw':r.spec(h/'raw.json')}
    check=lambda:r.authenticate_source_origin(f,proof,['04-draft.md'],tmp_path,None,1790812810)
    assert check()==1790812800
    header='wrong native seal header'
    with pytest.raises(ValueError,match='copied_source_context_seal'):check()
    header='SEALED ARTICLE SUBJECTS — READ ONLY\nmanifest_sha256='+m['manifest_sha256']+'\n'
    transport.authenticate_result=lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('native refusal'))
    with pytest.raises(RuntimeError,match='native refusal'):check()
    (f/'04-draft.md').write_text('retargeted')
    with pytest.raises(ValueError,match='copied_source_sealed_bytes'):check()


def test_copied_authorship_never_bypasses_native_refusal(tmp_path):
    f=tmp_path/'reports/fresh';f.mkdir(parents=True);p=f/'04-draft.md';p.write_text('same')
    def denied(*a):raise ValueError('same-model writer')
    with pytest.raises(ValueError,match='same-model'):
        r.copied_source_authors(f,[p.name],{},tmp_path,None,{},123,authenticate=denied)
    with pytest.raises(ValueError):
        r.copied_source_authors(f,[p.name],{'wrong.md':r.spec(p)},tmp_path,None,{},123,authenticate=denied)
