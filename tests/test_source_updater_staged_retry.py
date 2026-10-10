"""Synthetic, offline staged-retry admission; no effects or API responses."""
import ast
from pathlib import Path
import pytest
P=Path(__file__).resolve().parents[1]/'scripts/glp_seo_updater_source.py'
def predicate():
    tree=ast.parse(P.read_text())
    node=next((n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='verify_staged_retry'),None)
    assert node is not None, 'maintained retry admission absent'
    scope={};exec(compile(ast.Module(body=[node],type_ignores=[]),str(P),'exec'),scope)
    return scope['verify_staged_retry']
def row():
    return {'status':'PASS_SOURCE_UPDATE_STAGED','route':'/guides/a/','week_start':'2026-10-05','published':False,'review':{'status':'PASS_OPUS_SEO_DRAFT','publication_invoked':False}}
def test_exact_unpublished_predecessor_admits_retry():
    before={'article_id':'a','canonical_path':'/guides/a/','title':'original'}
    assert predicate()(row(),before,before,'/guides/a/','2026-10-05') is None
@pytest.mark.parametrize('change',['route','week','published','review','before'])
def test_retry_refuses_wrong_scope_or_already_changed_source(change):
    prior=row();before={'article_id':'a','canonical_path':'/guides/a/','title':'original'};current=dict(before)
    if change=='before':current['title']='already updated'
    elif change=='route':prior['route']='/guides/b/'
    elif change=='week':prior['week_start']='2026-09-28'
    elif change=='published':prior['published']=True
    else:prior['review']['status']='BLOCKED_OPUS_SEO_DRAFT'
    with pytest.raises(RuntimeError):predicate()(prior,current,before,'/guides/a/','2026-10-05')
