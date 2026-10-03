"""Portable chronology regression; fixture authority is synthetic."""
import hashlib,importlib.util,json
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
PATH=ROOT/'systems/seo-blog-machine/scripts/compile_review_packet_article.py'
spec=importlib.util.spec_from_file_location('date_compiler',PATH)
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
def sha(b): return hashlib.sha256(b).hexdigest()
def row_sha(a): return sha((json.dumps(a,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode())
def fixture(tmp_path):
    a={'article_id':'a','canonical_path':'/guides/a/','date_published':'2026-07-23','updated_at':'2026-08-29'}
    p=tmp_path/'catalog.json'; raw=json.dumps({'articles':[a]}).encode(); p.write_bytes(raw)
    e={'article_id':'a','canonical_path':a['canonical_path'],'article_index':0,'article_sha256':row_sha(a),'catalog_id':'c','records_key':'articles'}
    auth={'operation':'r2_refresh','existing_article_id':'a','existing_article':e,'inputs':{'catalogs':[{'catalog_id':'c','path':str(p),'sha256':sha(raw)}]}}
    return auth,p

def test_r2_date_is_bound_predecessor_not_draft(tmp_path):
    a,p=fixture(tmp_path)
    assert m.r2_publication_date(a)=='2026-07-23'
