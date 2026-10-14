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

def test_copied_authorship_never_bypasses_native_refusal(tmp_path):
    f=tmp_path/'reports/fresh';f.mkdir(parents=True);p=f/'04-draft.md';p.write_text('same')
    def denied(*a):raise ValueError('same-model writer')
    with pytest.raises(ValueError,match='same-model'):
        r.copied_source_authors(f,[p.name],{},tmp_path,None,{},123,authenticate=denied)
    with pytest.raises(ValueError):
        r.copied_source_authors(f,[p.name],{'wrong.md':r.spec(p)},tmp_path,None,{},123,authenticate=denied)
