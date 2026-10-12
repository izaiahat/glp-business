"""Offline changed-URL submission, deployment custody and no-blind-replay."""
import hashlib
import json
import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp_release_search_notify as notify


def test_release_changed_set_deployment_ledger_and_replay(tmp_path, monkeypatch):
    import glp_release_main as release
    old, new = tmp_path/'old', tmp_path/'new'
    for root in (old,new):
        (root/'public/guides/unchanged').mkdir(parents=True)
        (root/'public/guides/unchanged/index.html').write_text('<html>same</html>')
        (root/'public/robots.txt').write_text('User-agent: *\nAllow: /\n')
    (new/'public/guides/new').mkdir()
    (new/'public/guides/new/index.html').write_text('<html>new guide</html>')
    (new/'public/sitemap.xml').write_text('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://glpclinicscout.com/guides/new/</loc></url><url><loc>https://glpclinicscout.com/guides/unchanged/</loc></url></urlset>')
    (new/'public/fixture.txt').write_text('fixture')
    (new/'data').mkdir(); (new/'data/indexnow.json').write_text(json.dumps({'host':'glpclinicscout.com','key':'fixture','key_location':'https://glpclinicscout.com/fixture.txt'}))
    final=tmp_path/'final.json'; final.write_text(json.dumps({'status':'PASS_PRODUCTION_TERMINAL','deployment':{'deployment_id':'dep-1'},'site_head':'head-1'}))
    pointer={'deployment_id':'dep-1','site_head':'head-1','release_root':str(new),'final_receipt':{'path':str(final),'sha256':hashlib.sha256(final.read_bytes()).hexdigest()}}
    pointer['supersedes'] = {'release_root': str(old)}
    calls=[]
    monkeypatch.setattr(notify,'current_pointer',lambda:pointer)
    monkeypatch.setattr(notify,'live_sitemap',lambda:new.joinpath('public/sitemap.xml').read_bytes())
    def post(endpoint,payload):
        calls.append(('indexnow',payload['urlList'])); return 200,b''
    def gsc(xml):
        calls.append(('gsc',xml));return {'http_status':204,'provider_readback':[{'path':notify.SITEMAP,'isPending':True}]}
    report=tmp_path/'receipts'; ledger=tmp_path/'indexnow.jsonl'
    kwargs=dict(root=tmp_path,report_root=report,ledger=ledger,poster=post,gsc_submit=gsc)
    with pytest.raises(RuntimeError, match='predecessor_binding'):
        notify.submit_deployment(pointer, new, **kwargs)
    assert calls == []
    value=notify.submit_deployment(pointer,old,**kwargs)
    assert value['status']=='PASS_SEARCH_SUBMITTED'
    assert value['deployment_id']=='dep-1'
    assert value['indexnow']['submitted_urls']==['https://glpclinicscout.com/guides/new/']
    rows=[json.loads(x) for x in ledger.read_text().splitlines()]
    assert len(rows)==1 and rows[0]['deployment_id']=='dep-1'
    assert rows[0]['receipt_sha256']==notify.sha(report/'dep-1/indexnow.json')
    assert notify.submit_deployment(pointer,old,**kwargs)==value
    assert len(calls)==2
    # An uncertain effect intent must not be retried as a fresh POST.
    (report/'dep-1/search-receipt.json').unlink(); (report/'dep-1/gsc.json').unlink()
    with pytest.raises(RuntimeError,match='uncertain_gsc'):
        notify.submit_deployment(pointer,old,**kwargs)
    assert len(calls)==2
    pointer['deployment_id']='different'
    with pytest.raises(RuntimeError,match='terminal_deployment_binding'):
        notify.submit_deployment(pointer,old,**kwargs)
    # Installed main publisher's terminal path must invoke this maintained seam.
    source=(ROOT/'scripts/glp_release_main.py').read_text()
    assert 'submit_deployment(' in source and 'search_submission' in source
