#!/usr/bin/env python3
"""Build a sanitized competitor-gap input from measured keyword evidence via Firecrawl search+scrape."""
from __future__ import annotations
import argparse, hashlib, json, os, re, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
import requests
import sys
sys.path.insert(0, str(Path('/home/ubuntu/business/scripts')))
from firecrawl_client import firecrawl_post, search_rows
BASE='https://api.firecrawl.dev/v2'
SELF='glpclinicscout.com'
NON_COMPETITOR_HOSTS={
 'pmc.ncbi.nlm.nih.gov','journals.ieditore.com','journals.econsciences.com',
 'journals.ub.uni-heidelberg.de','www.actabioethica.uchile.cl','policylab.us',
}

def now():return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
def sha(v):return hashlib.sha256((v if isinstance(v,bytes) else json.dumps(v,sort_keys=True,separators=(',',':')).encode())).hexdigest()
def env_key():
 for raw in Path('/home/ubuntu/.hermes/.env').read_text().splitlines():
  if raw.startswith('FIRECRAWL_API_KEY='):return raw.split('=',1)[1].strip().strip('"').strip("'")
 raise RuntimeError('firecrawl_key_missing')
def norm(s):return ' '.join(re.findall(r'[a-z0-9]+',str(s).lower().replace('glp-1','glp1')))
def credible_competitor_host(host):
 return bool(host) and host not in NON_COMPETITOR_HOSTS and not host.endswith(('.gov','.edu')) and 'journal' not in host
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--keywords',type=Path,required=True);ap.add_argument('--existing',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--raw-dir',type=Path,required=True);ap.add_argument('--limit',type=int,default=6);a=ap.parse_args();kw=json.loads(a.keywords.read_text());old=json.loads(a.existing.read_text());
 if not str(kw.get('status','')).startswith('PASS'):raise RuntimeError('keyword_source_not_pass')
 existing={norm(x['keyword']) for x in old.get('gaps',[])};rows=[x for x in kw.get('rows',[]) if int(x.get('merged_search_volume') or 0)>0 and norm(x.get('keyword')) not in existing];rows.sort(key=lambda x:(-int(x.get('merged_search_volume') or 0),norm(x.get('keyword'))));rows=rows[:a.limit]
 key=env_key();head={'Authorization':f'Bearer {key}','Content-Type':'application/json'};a.raw_dir.mkdir(parents=True,exist_ok=True);os.chmod(a.raw_dir,0o700);gaps=[];calls=[]
 for i,row in enumerate(rows):
  keyword=row['keyword'];payload={'query':f'"{keyword}" GLP-1 cost insurance provider guide','limit':5,'scrapeOptions':{'formats':['markdown'],'onlyMainContent':True}}
  r=firecrawl_post(BASE+'/search',caller='seo.competitor_gap',headers=head,json=payload,timeout=90);body=r.json() if r.headers.get('content-type','').startswith('application/json') else {'success':False};raw={'schema':'seo-firecrawl-competitor-search-v1','captured_at':now(),'keyword':keyword,'http_status':r.status_code,'response':body};rp=a.raw_dir/f'{i+1:02d}-{sha(keyword)[:12]}.json';rp.write_text(json.dumps(raw,indent=2,sort_keys=True)+'\n');os.chmod(rp,0o600);calls.append({'keyword':keyword,'http_status':r.status_code,'raw_path':str(rp),'raw_sha256':sha(rp.read_bytes())})
  if r.status_code!=200 or body.get('success') is False:raise RuntimeError(f'firecrawl_search_failed:{r.status_code}')
  data=search_rows(body, sources=('web',));e=[]
  for item in data:
   u=item.get('url') or '';host=(urlsplit(u).hostname or '').lower()
   if not u or host==SELF or host.endswith('.'+SELF) or not credible_competitor_host(host):continue
   md=str(item.get('markdown') or '');e.append({'title':item.get('title') or host,'url':u,'host':host,'content_sha256':sha(md.encode()) if md else None,'description':str(item.get('description') or '')[:300]})
   if len(e)>=3:break
  if len(e)<2:continue
  vol=int(row.get('merged_search_volume') or 0);priority=5 if vol>=10000 else 4 if vol>=1000 else 3 if vol>=100 else 2
  gaps.append({'keyword':keyword,'opportunity_type':'competitor_gap_new_topic','priority':priority,'rationale':f'Measured U.S. monthly search volume is {vol}; Firecrawl returned {len(e)} non-Clinic-Scout competitor pages. Coverage and duplicate-intent gates must decide new page versus refresh.','evidence':e,'keyword_evidence':{'merged_search_volume':vol,'provider_count':row.get('provider_count'),'source_receipt_sha256':sha(a.keywords.read_bytes())}})
  time.sleep(.2)
 out={'schema':'seo-blog-machine-competitor-gap-input-v1','created_at':now(),'research_method':'Automated Firecrawl search with main-content scrape; public web only','site_id':'glp-clinic-scout','status':'PASS','summary':{'provider_calls':len(calls),'gaps':len(gaps),'site_writes':0,'publishes':0},'calls':calls,'gaps':gaps};a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(out,indent=2,sort_keys=True)+'\n');os.chmod(a.output,0o600);print(json.dumps({'status':out['status'],**out['summary'],'output':str(a.output)}))
if __name__=='__main__':raise SystemExit(main())
