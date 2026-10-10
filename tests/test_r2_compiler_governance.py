"""Real compiler regression, synthetic packet and sealed catalog only."""
import hashlib
import json
from pathlib import Path
import sys
import yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'systems/seo-blog-machine/scripts'))
import compile_review_packet_article as c
import existing_article_updates as u

def test_r2_preserves_route_governance(tmp_path):
    before={'article_id':'test-id','canonical_path':'/guides/test/','updated_at':'2026-07-23','risk_tier':'MEDIUM','comparison_required':False,'comparison_spec':None,'has_affiliate_cta':True,'inline_ftc_disclosure':True,'inline_ftc_disclosure_text':'Existing inactive disclosure'}
    raw=json.dumps({'articles':[before]}).encode()
    p=tmp_path/'catalog.json';p.write_bytes(raw)
    sha=lambda b:hashlib.sha256(b).hexdigest()
    auth={'operation':'r2_refresh','existing_article_id':'test-id','r2_contract':{},'existing_article':{'catalog_id':'test','records_key':'articles','article_index':0,'article_id':'test-id','canonical_path':'/guides/test/','article_sha256':sha((json.dumps(before,sort_keys=True,separators=(',',':'))+'\n').encode())},'inputs':{'catalogs':[{'catalog_id':'test','path':str(p),'sha256':sha(raw)}]}}
    meta={'slug':'/guides/test/','title':'Test title','h1':'Test heading','primaryKeyword':'test','metaDescription':'Synthetic test','dateModified':'2026-10-09','captureContract':{'mode':'on-page-only','emailField':None,'healthDataFields':[],'followUpConsent':False}}
    (tmp_path/'04-draft.md').write_text('---\n'+yaml.safe_dump(meta)+'---\n# Test\n\n> **Quick answer:** Synthetic test only.\n\n## Answer\nSynthetic answer.\n')
    for name,value in [('00-authority.json',auth),('01-evidence-ledger.json',{'sources':[]}),('05-claim-source-map.json',{'claims':[]}),('02-serp-brief.json',{})]:
        (tmp_path/name).write_text(json.dumps(value))
    result=c.compile_packet(tmp_path)
    for key in u.R2_ROUTE_GOVERNANCE_FIELDS:
        assert (key in result)==(key in before), key
        assert result.get(key)==before.get(key), key
    assert result['date_published']=='2026-07-23'
    assert result['updated_at']=='2026-10-09'
