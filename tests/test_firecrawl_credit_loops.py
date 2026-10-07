import datetime as dt
import importlib.util
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import glp_evidence_refresh_cycle as cycle


def test_pending_review_backoff_and_content_idempotency(tmp_path):
    record = {'capture_id':'fixture', 'disposition':'ACCEPTED', 'cached_at':'2026-08-01T00:00:00Z',
              'requested_url':'https://example.invalid/price', 'resolved_url':'https://example.invalid/price', 'exact_quote':'Old offer'}
    response = {'success':True, 'data':{'markdown':'New offer', 'metadata':{'statusCode':200,
                'sourceURL':record['requested_url'], 'scrapeId':'00000000-0000-0000-0000-000000000001'}}}
    calls = []
    def fetch(url):
        calls.append(url)
        return response
    def adopt(*args):
        pytest.fail('material change must never auto-adopt')
    def run(day, name):
        return cycle.run_cycle({'records':[record]}, {'observations':[]}, '2026-08-01T00:00:00Z',
          dt.datetime(2026,10,day,tzinfo=dt.timezone.utc),45,tmp_path/name,fetch,adopt)
    assert run(7,'first')['records'][0]['status']=='ESCALATED'
    assert run(8,'second')['records'][0]['status']=='REVIEW_PENDING_BACKOFF'
    assert len(calls)==1
    third = run(15,'third')['records'][0]
    assert third['status']=='ESCALATED'
    assert third['content_idempotent'] is True
    assert len(calls)==2
    assert record['cached_at']=='2026-08-01T00:00:00Z'


def taxonomy():
    path=ROOT/'site-system/flagship-v2/candidates/glp34-provider-families-20260912/scripts/refresh-therapeutic-taxonomy.py'
    spec=importlib.util.spec_from_file_location('taxonomy_credit_test',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_taxonomy_url_memo_and_failure_reuse(tmp_path,monkeypatch):
    module=taxonomy()
    module.CAPTURE_MEMO={}
    calls=[]
    receipt={'received_at':'2026-10-07T10:00:00Z','response':{'data':{'markdown':'real fixture'}}}
    def capture(url,dest):
        calls.append(url)
        if url.endswith('/bad'):
            raise ValueError('source_host_changed')
        dest.write_bytes(module.encoded(receipt))
        return receipt
    monkeypatch.setattr(module,'capture',capture)
    assert module.memoized_capture('https://example.invalid/',tmp_path/'a') == receipt
    assert module.memoized_capture('https://example.invalid/',tmp_path/'b') == receipt
    assert (tmp_path/'a').read_bytes()==(tmp_path/'b').read_bytes()
    for n in range(2):
        with pytest.raises(ValueError,match='source_host_changed'):
            module.memoized_capture('https://example.invalid/bad',tmp_path/f'bad{n}')
    assert len(calls)==2
    now=dt.datetime(2026,10,7,tzinfo=dt.timezone.utc)
    assert not module.due({'verified_as_of':'2026-09-08'},now)
    assert module.due({'verified_as_of':'2026-09-07'},now)
