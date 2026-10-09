#!/usr/bin/env python3
from __future__ import annotations
import argparse,hashlib,json,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.parse import urlsplit
import requests
from firecrawl_client import firecrawl_post, search_rows
args=argparse.ArgumentParser(description='Bounded FDA warning-letter discovery; not legal clearance')
args.add_argument('--limit',type=int,default=13)
args.add_argument('--output',type=Path)
args.add_argument('--spec',type=Path,help='Onboarding program spec; check every non-excluded provider')
args=args.parse_args()
if not 1 <= args.limit <= 13: raise ValueError('limit_must_be_1_to_13')
B=Path('/home/ubuntu/business'); OUT=args.output or B/'reports/glp-clinic-scout/fda-warning-search'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'); RAW=OUT/'fda-warning-search-raw'; RAW.mkdir(parents=True,exist_ok=False)
key_name='FIRE'+'CRAWL'+'_API'+'_KEY'; key=''
for line in (Path.home()/'.hermes/.env').read_text(errors='ignore').splitlines():
    if line.startswith(key_name+'='): key=line.split('=',1)[1].strip(); break
if not key: raise RuntimeError('firecrawl_credential_missing')
head={'Authorization':f'Bearer {key}','Content-Type':'application/json'}
candidates=[
 ('healthrx-affiliate-program','HealthRX',['HealthRX','healthrx.com']),('mademed','MadeMed',['MadeMed','mademed.com']),('telos-rx','telos rx',['telos rx','telosrx.com']),('ageless','Ageless',['AgelessRx','tryageless.com']),('wellorithm-glp-1','Wellorithm',['Wellorithm','wellorithm.com']),('enhance-md','Enhance MD',['Enhance MD','enhance.md']),('synergy-rx','Synergy RX',['Synergy RX','synergyrx.co']),('care-bare-rx-glp1','Care Bare Rx',['Care Bare Rx','carebarerx.com']),('serena-health','Serena Health',['Serena Health','serenahealth.life']),('breeze-meds','Breeze Meds',['Breeze Meds','breezemeds.com']),('direct-meds-glp-1-offers-top-funnels','Direct Meds',['Direct Meds','directmeds.com']),('strut-health','Strut Health',['Strut Health','struthealth.com']),('emed-com','eMed',['eMed','emed.com'])]
rows=[]; checked=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
if args.spec:
    candidates=[(p['program_id'],p['display_name'],[p['display_name'],*[urlsplit(u).hostname for u in p.get('urls',[]) if urlsplit(u).hostname]]) for p in json.loads(args.spec.read_text())['programs']]
else:
    candidates=candidates[:args.limit]
candidates=[p for p in candidates if not any('fella' in str(x).lower() for x in p)]
for pid,name,aliases in candidates:
    query='site:fda.gov '+ ' '.join(f'"{x}"' for x in aliases) +' "warning letter"'
    r=firecrawl_post('https://api.firecrawl.dev/v2/search/gov',caller='research_glp_batch2_fda_warning_provenance',headers=head,json={'query':query,'k':10},timeout=60)
    try: payload=r.json()
    except Exception: payload={'success':False,'error_class':'non_json_response'}
    if r.status_code != 200 or payload.get('success') is not True: raise RuntimeError('fda_search_unavailable_not_no_match')
    raw={'program_id':pid,'display_name':name,'query':query,'searched_at_utc':checked,'http_status':r.status_code,'response':payload}
    raw_bytes=(json.dumps(raw,indent=2,sort_keys=True)+'\n').encode(); raw_path=RAW/f'{pid}.json'; raw_path.write_bytes(raw_bytes)
    results=search_rows(payload, sources=('web',))
    exact=[]
    needles=[a.lower() for a in aliases]
    for item in results:
        if not isinstance(item,dict): continue
        hay=' '.join(str(item.get(k,'')) for k in ('title','description','url')).lower()
        if any(n in hay for n in needles): exact.append({'title':item.get('title'),'url':item.get('url'),'description':item.get('description')})
    status='MATCH_MANUAL_REVIEW' if exact else 'NO_MATCH_BOUNDED'
    state_query=f'{name} US state telehealth prescribing GLP-1 compounded semaglutide rules restrictions'
    state=firecrawl_post('https://api.firecrawl.dev/v2/search/gov',caller='research_glp_batch2_fda_warning_provenance.states',headers=head,json={'query':state_query,'k':10},timeout=60)
    if state.status_code != 200 or state.json().get('success') is not True: raise RuntimeError('state_rules_unavailable_not_clearance')
    state_path=RAW/f'{pid}-state-rules.json'; state_path.write_text(json.dumps({'query':state_query,'response':state.json(),'status':'DISCOVERY_REQUIRES_JURISDICTION_REVIEW_NOT_CLEARANCE'},indent=2)+'\n')
    rows.append({'program_id':pid,'display_name':name,'aliases_and_domains_searched':aliases,'official_source_endpoint':'https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/warning-letters','firecrawl_query':query,'searched_at_utc':checked,'search_scope':'FDA-indexed warning-letter web results only; not legal clearance and not a complete enforcement-history guarantee','result_count':len(results),'exact_brand_or_domain_result_count':len(exact),'matched_results':exact,'status':status,'raw_receipt_path':raw_path.relative_to(B).as_posix(),'raw_receipt_sha256':hashlib.sha256(raw_bytes).hexdigest(),'refresh_due_date':(datetime.now(timezone.utc)+timedelta(days=30)).date().isoformat()})
    time.sleep(.15)
    rows[-1]['state_rules_receipt_path']=str(state_path)
ledger={'schema':'glp-clinic-scout-fda-warning-bounded-search-v1','generated_at_utc':checked,'method':'Independent Firecrawl search restricted to FDA-indexed warning-letter results for exact brand/domain aliases. NO_MATCH_BOUNDED is not a compliance claim or legal clearance.','programs':rows}
path=OUT/'fda-warning-bounded-search-ledger.json'; path.write_text(json.dumps(ledger,indent=2,sort_keys=True)+'\n')
print(json.dumps({'status':'PASS','programs':len(rows),'matches':[x['program_id'] for x in rows if x['status']!='NO_MATCH_BOUNDED'],'ledger':str(path)},sort_keys=True))
