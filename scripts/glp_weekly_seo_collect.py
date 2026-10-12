#!/usr/bin/env python3
"""Read-only weekly producer. Exclusive output is an execution journal, not resumable.
Never rerun a submitted billable run: reconcile its local ledger/raw receipts first.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request
import urllib.error
from urllib.parse import urlsplit, unquote, quote
import uuid
from concurrent.futures import ThreadPoolExecutor

B=Path('/home/ubuntu/business')
sys.path.insert(0,str(B))
from ops.glp.cogny_client import CognyClient, TOOL
from ops.glp.seo_data_rails import GoogleRail, DataForSEO, PostHog
from ops.glp.seo_quickwin_common import inspect_html

HOST='https://glpclinicscout.com'
PROPERTY='sc-domain:glpclinicscout.com'
def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def sha(raw): return hashlib.sha256(raw).hexdigest()
def windows(today):
 end=today-dt.timedelta(days=3)
 return {key:(str(stop-dt.timedelta(days=days-1)),str(stop),days) for key,days,stop in [('28',28,end),('90',90,end),('prior-28',28,end-dt.timedelta(days=28))]}
def safe_route(route):
 if not isinstance(route,str) or not route.startswith('/') or route.startswith('//'): return False
 decoded=unquote(route)
 p=urlsplit(decoded)
 return not (p.scheme or p.netloc or p.query or p.fragment or '\\' in decoded or '%' in decoded or any(x in ('.','..') for x in p.path.split('/')) or p.path.lower()=='/go' or p.path.lower().startswith('/go/') or any(ord(c)<32 for c in decoded))
def assert_stable(sources):
 for path,raw in sources.items():
  if path.read_bytes()!=raw: raise ValueError('source_pointer_or_bytes_changed:'+str(path))
class Store:
 def __init__(self,path):
  self.path=Path(path).absolute();self.path.mkdir(parents=True,exist_ok=False)
 def raw(self,name,raw):
  p=self.path/name;p.parent.mkdir(parents=True,exist_ok=True)
  with p.open('xb') as f: f.write(raw);f.flush();os.fsync(f.fileno())
  return {'path':str(p),'sha256':sha(raw)}
 def save(self,name,doc):return self.raw(name,(json.dumps(doc,indent=2,allow_nan=False)+'\n').encode())
def paginate(fetch,limit=25000,max_pages=40):
 rows=[];seen=set();pages=0
 try:
  for i in range(max_pages):
   doc=fetch(i*limit)
   if not isinstance(doc,dict) or 'rows' not in doc or not isinstance(doc['rows'],list): raise ValueError('gsc_envelope_unknown')
   batch=doc['rows'];pages+=1
   sig=sha(json.dumps(batch,sort_keys=True).encode())
   if batch and sig in seen:raise ValueError('gsc_repeated_page')
   seen.add(sig);rows.extend(batch)
   if len(batch)<limit:return {'ok':True,'complete':True,'rows':rows,'pages':pages}
  raise ValueError('gsc_page_cap')
 except Exception as exc:return {'ok':False,'complete':False,'rows':rows,'pages':pages,'error':type(exc).__name__,'state':'UNKNOWN_PARTIAL'}
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None

def capture_page(route,store):
 if not safe_route(route): return route,None,{'route':route,'reason':'UNSAFE_ROUTE_NOT_REQUESTED'}
 began=now();url=HOST+route
 try:
  opener=urllib.request.build_opener(NoRedirect())
  req=urllib.request.Request(url,headers={'User-Agent':'glp-weekly-evidence/1.0'},method='GET')
  try: resp=opener.open(req,timeout=45)
  except urllib.error.HTTPError as exc:resp=exc
  with resp:
   status=resp.code;raw=resp.read();final=resp.geturl();location=resp.headers.get('Location')
  binding=store.raw('html/'+sha(route.encode())+'.html',raw)
  receipt={'path':route,'status':status,'file':binding['path'],'sha256':binding['sha256'],'captured_at_utc':now(),'request_started_at_utc':began,'final_url':final,'redirect_location':location,'redirect_followed':False}
  store.save('html/'+sha(route.encode())+'.json',receipt)
  if status!=200:return route,None,{'route':route,'capture':receipt,'reason':'NON_200_CAPTURE_NOT_COPY_EVIDENCE'}
  try: parsed=inspect_html(raw.decode('utf-8'))
  except UnicodeDecodeError:return route,None,{'route':route,'capture':receipt,'reason':'NON_UTF8_HTML'}
  parsed['capture']=receipt
  return route,parsed,None
 except Exception as exc:return route,None,{'route':route,'status':None,'captured_at_utc':now(),'reason':'UNKNOWN_'+type(exc).__name__}

class RecordedCogny(CognyClient):
 def __init__(self,store):super().__init__(retries=1,timeout=90);self.store=store;self.n=0
 def _post(self,payload):
  began=now();headers,body=super()._post(payload)
  self.n+=1
  # No authorization/session headers or initialization response saved.
  if payload.get('method') in ('tools/list','tools/call'):
   self.store.save(f'raw/cogny-{self.n}.json',{'request_started_at_utc':began,'captured_at_utc':now(),'request':payload,'response_body':body})
   from ops.glp.cogny_client import _parse
   parsed=_parse(body)
   if isinstance(parsed.get('result'),dict) and parsed['result'].get('isError'):
    raise ValueError('cogny_mcp_tool_error')
  return headers,body

class RecordedDFS(DataForSEO):
 def __init__(self,store,run_id,budget=None,prefix='dfs'):
  super().__init__(ledger_path=store.path/'dataforseo-ledger.jsonl',run_id=run_id);self.store=store;self.n=0;self.budget=budget;self.prefix=prefix
 def _journal_cost(self):
  if not self.ledger_path.is_file():return None
  rows=[json.loads(x) for x in self.ledger_path.read_text().splitlines() if x.strip()]
  rows=[r for r in rows if r.get('run_id')==self.run_id and r.get('event') in ('RESULT','AMBIGUOUS')]
  return rows[-1].get('cost') if rows and rows[-1]['event']=='RESULT' else None
 def _post(self,path,payload,timeout=180):
  # GLP-44 weekly cap: reserve a published-price ceiling BEFORE the request;
  # BudgetRefused (over cap / unknown cost) means nothing was sent.
  if self.budget is None: raise ValueError('weekly_budget_required_for_metered_call')
  reservation=self.budget.reserve('dataforseo',path,payload,run_id=self.run_id)
  self.n+=1;began=now()
  self.store.save(f'raw/{self.prefix}-{self.n}-SUBMITTED.json',{'request_started_at_utc':began,'endpoint':path,'payload':payload,'run_id':self.run_id,'state':'SUBMITTED_DO_NOT_REPLAY','reservation_id':reservation['reservation_id'],'estimate_usd':reservation['estimate_usd']})
  try:
   doc=super()._post(path,payload,timeout)
  except Exception:
   # A provider-rejected call may still carry a measured cost in the rail
   # journal; transport ambiguity stays unknown and blocks further spend.
   self.budget.settle(reservation,self._journal_cost());raise
  self.budget.settle(reservation,doc.get('cost') if isinstance(doc,dict) else None)
  self.store.save(f'raw/{self.prefix}-{self.n}-COMPLETE.json',{'request_started_at_utc':began,'captured_at_utc':now(),'endpoint':path,'payload':payload,'response':doc,'run_id':self.run_id})
  return doc

def run(output, *, allow_metered=False, report_date=None, budget=None, max_cost_usd=1.0):
 helper_path=Path(__file__).resolve().parents[1]/'ops/glp/weekly_strategy_inputs.py'
 if sha(helper_path.read_bytes())!='d60df3ea3605385b1bcf04268a50ea8d2c2fd8dda2bbc361252531ab890f51c7':
  raise ValueError('weekly_estate_helper_source_drift')
 report_date=report_date or dt.datetime.now(dt.timezone.utc).date()
 store=Store(output);run_id='S3b-'+str(uuid.uuid4());started=now()
 store.save('RUN-SUBMITTED.json',{'run_id':run_id,'started_at_utc':started,'mode':'MANUAL_CURRENT_DATA_DRY_RUN','resume_allowed':False})
 sources={};bindings={}
 def snap(key,path):
  raw=path.read_bytes();sources[path]=raw;bindings[key]=store.raw('inputs/'+key+'.json',raw);return json.loads(raw)
 basepath=B/'reports/glp-clinic-scout/seo-machine/current-content-base.json'
 base=snap('content-base',basepath);release=Path(base['release_root']);data=release/'data'
 registry=snap('registry',data/'program-registry.json')
 for key,name in [('ledger','program-content-ledger.json'),('pricing','provider-pricing-matrix.json'),('contracts','provider-page-contracts.json'),('seo-copy','page-seo-copy.json')]:snap(key,data/name)
 supply=snap('supply',B/'reports/glp-clinic-scout/seo-machine/keyword-gap/current-topic-supply.json')
 renderers={}
 for p in sorted((release/'src').rglob('*')):
  if p.is_file() and p.suffix in ('.mjs','.js') and (b'page-seo-copy' in p.read_bytes() or p.name=='page-seo-copy.mjs'):
   raw=p.read_bytes();sources[p]=raw;renderers[str(p)]={'sha256':sha(raw),'snapshot':store.raw('source/'+str(p.relative_to(release)),raw)}
 # Confirm exact release authority bindings without editing the authority chain.
 for key in ('manifest','final_receipt'):
  item=base[key];raw=Path(item['path']).read_bytes()
  if sha(raw)!=item['sha256']:raise ValueError('base_authority_hash_mismatch')
  sources[Path(item['path'])]=raw;store.raw('source/'+key+'.json',raw)
 from ops.glp.seo_weekly_cogny import collect as collect_cogny, read_binding
 client=RecordedCogny(store)
 try:
  client.initialize()
 except Exception as exc:
  store.save('cogny-initialize.json',{'error_type':type(exc).__name__,'state':'UNKNOWN_INITIALIZATION; individual queries remain separately receipted'})
 cogny=collect_cogny(client,store,report_date)
 bindings['cogny-weekly-evidence']=cogny['binding']
 bindings['ga4']=cogny['snapshots']['ga4']
 cogny_has_domain=cogny['domain_property_available']
 google=GoogleRail();direct_count=0
 def direct(endpoint,body=None):
  nonlocal direct_count
  direct_count+=1;began=now()
  req=urllib.request.Request(endpoint,headers={**google._headers(),'Content-Type':'application/json'},data=json.dumps(body).encode() if body is not None else None,method='POST' if body is not None else 'GET')
  try:
   with urllib.request.urlopen(req,timeout=90) as response:raw=response.read();status=response.status
  except urllib.error.HTTPError as exc:raw=exc.read();status=exc.code
  store.save(f'raw/google-{direct_count}.json',{'request_started_at_utc':began,'captured_at_utc':now(),'endpoint':endpoint,'request':body,'status':status,'response_body':raw.decode('utf-8','replace')})
  if status!=200:raise ValueError('google_http_non200')
  return json.loads(raw)
 if not cogny_has_domain:
  direct_properties=direct('https://searchconsole.googleapis.com/webmasters/v3/sites')
  store.save('google-property-discovery.json',{'captured_at_utc':now(),'result':direct_properties})
  if PROPERTY not in [p['siteUrl'] for p in direct_properties.get('siteEntry',[])]:raise ValueError('exact_domain_property_not_confirmed')
 snapshots={}
 for key,(start,end,days) in windows(report_date).items():
  if cogny_has_domain:
   snapshot=read_binding(cogny['snapshots']['gsc-'+key])
   snapshots[key]=snapshot;bindings['gsc-'+key]=cogny['snapshots']['gsc-'+key]
   continue
  began=now()
  context={'property':PROPERTY,'start_date':start,'end_date':end,'dimensions':['query','page'],'search_type':'web','window_days':days,'data_state':'final','privacy_omissions_remain':True,'complete_means_available_API_rows_only':True,'rail':'Cogny' if cogny_has_domain else 'GoogleRail OAuth direct; Cogny identity lacks domain property'}
  def fetch(offset):
   if cogny_has_domain:return client.call(TOOL['gsc_search_analytics'],{'site_url':PROPERTY,'start_date':start,'end_date':end,'dimensions':['query','page'],'search_type':'web','row_limit':25000,'start_row':offset})
   doc=direct('https://searchconsole.googleapis.com/webmasters/v3/sites/'+quote(PROPERTY,safe='')+'/searchAnalytics/query',{'startDate':start,'endDate':end,'dimensions':['query','page'],'type':'web','dataState':'final','rowLimit':25000,'startRow':offset})
   if 'rows' not in doc and 'responseAggregationType' in doc:doc['rows']=[]
   return doc
  result=paginate(fetch);snapshot={'request_started_at_utc':began,'captured_at_utc':now(),'context':context,'result':result}
  snapshots[key]=snapshot;bindings['gsc-'+key]=store.save('inputs/gsc-'+key+'.json',snapshot)
 # Prefix-only Cogny snapshots remain separately bound; never relabel as domain.
 began=now()
 if allow_metered:
  if budget is None: raise ValueError('weekly_budget_required_for_metered_collection')
  dfs=RecordedDFS(store,run_id,budget)
  result=dfs.ranked_keywords(['glpwinner.com','glpclinicscout.com'],page_size=1000,max_pages_per_target=10,max_cost_usd=max_cost_usd)
  ledger=[json.loads(x) for x in dfs.ledger_path.read_text().splitlines()] if dfs.ledger_path.is_file() else []
  costs=[x['cost'] for x in ledger if x['event']=='RESULT' and x.get('cost') is not None]
  ambiguous=any(x['event']=='AMBIGUOUS' for x in ledger)
  actual_cost=None if ambiguous else sum(costs)
 else:
  result={'ok':False,'complete':False,'domains':{},'state':'UNKNOWN_METERED_COLLECTION_HELD','required_artifact':'Explicit recurring DataForSEO collection authority and per-run ceiling; no prior capture retimestamped'}
  actual_cost=0
 bindings['footprint']=store.save('inputs/footprint.json',{'request_started_at_utc':began,'captured_at_utc':now(),'context':{'targets':['glpwinner.com','glpclinicscout.com'],'location_code':2840,'language_code':'en','max_pages_per_target':10,'max_cost_usd':max_cost_usd if allow_metered else 0,'weekly_budget':budget.receipt() if allow_metered else None,'rail_account_order':['tryboringai','ops'],'run_id':run_id,'ledger_actual_cost_usd':actual_cost,'scope':'Bounded existing comparator, not market census'},'result':result})
 # GLP-44: current Google Ads volume for this week's supply gap candidates
 # (one bounded task; unknown volume stays null, never zero).
 began=now()
 if allow_metered:
  words=sorted({' '.join(str(r.get('keyword') or '').lower().split()) for r in supply.get('gaps',[]) if isinstance(r,dict)})
  words=[w for w in words if w and 'fella' not in w and len(w)<=80 and len(w.split())<=10 and all(c.isalnum() or c in " '-" for c in w)][:1000]
  volume=RecordedDFS(store,run_id+':volume',budget,prefix='dfs-volume').search_volume(words) if words else {}
  gap_volume={'ok':bool(words) and all(v.get('ok') for v in volume.values()) and len(volume)>0,'requested':len(words),'rows':volume}
 else:
  gap_volume={'ok':False,'requested':0,'rows':{},'state':'UNKNOWN_METERED_COLLECTION_HELD'}
 bindings['gap-volume']=store.save('inputs/gap-volume.json',{'request_started_at_utc':began,'captured_at_utc':now(),'context':{'endpoint':'/keywords_data/google_ads/search_volume/live','location_code':2840,'language_code':'en','scope':'Current supply gap keywords only','weekly_budget':budget.receipt() if allow_metered else None},'result':gap_volume})
 routes={'/providers/','/reviews/','/pharmacies/'}
 routes.update(p['review_path'] for p in registry['programs'] if p.get('review_path'))
 for s in snapshots.values():
  for r in s['result']['rows']:
   if r.get('position') is not None and 11<=float(r['position'])<=20 and len(r.get('keys',[]))==2:
    u=urlsplit(r['keys'][1])
    if u.scheme=='https' and u.netloc=='glpclinicscout.com' and not u.query and not u.fragment:routes.add(u.path or '/')
 store.save('page-roster.json',{'routes':sorted(routes),'count':len(routes),'basis':'Exact current registry review routes, money directories, 28/90/prior28 GSC avg-position 11-20 pages; no href following'})
 pages={};exclusions=[]
 with ThreadPoolExecutor(max_workers=4) as pool:
  for route,page,excluded in pool.map(lambda r:capture_page(r,store),sorted(routes)):
   if page is not None:pages[route]=page
   if excluded:exclusions.append(excluded)
 bindings['pages']=store.save('inputs/pages.json',pages)
 store.save('page-exclusions.json',{'rows':exclusions,'count':len(exclusions)})
 began=now()
 try:
  posthog=PostHog().neutral_pageviews(start_date=str(report_date-dt.timedelta(days=14)),end_date=str(report_date))
 except Exception as exc:
  posthog={'ok':False,'complete':False,'state':'UNKNOWN','rows':[],'total_views':None,'affiliate_clicks':None,'error_type':type(exc).__name__}
 bindings['posthog']=store.save('inputs/posthog.json',{'request_started_at_utc':began,'captured_at_utc':now(),'context':{'source':'PostHog direct approved neutral pageviews','requested':True,**posthog.get('context',{})},'result':posthog})
 from ops.glp.weekly_strategy_inputs import estate_snapshot
 bindings['estate']=store.save('inputs/estate.json',estate_snapshot(B))
 assert_stable(sources)
 manifest={'schema':'glp-weekly-module-inputs-v1','inputs':bindings,'built_at_utc':now(),'run_id':run_id,'current_base_sha256':sha(sources[basepath]),'current_base_deployment_id':base['deployment_id'],'source_bindings':{str(p):{'sha256':sha(raw)} for p,raw in sources.items()},'renderer_bindings':renderers,'pointer_stability':'PASS_EXACT_BYTES_PRE_POST','raw_receipts':{str(p.relative_to(store.path)):{'sha256':sha(p.read_bytes())} for p in sorted((store.path/'raw').glob('*'))},'publication_authorized':False}
 binding=store.save('MODULE-INPUTS.json',manifest)
 summary={'run_id':run_id,'manifest':binding,'gsc_rows':{k:len(v['result']['rows']) for k,v in snapshots.items()},'gsc_complete':{k:v['result']['complete'] for k,v in snapshots.items()},'footprints':{k:{'rows':len(v['items']),'total_count':v['total_count'],'complete':v['complete'],'pages':v['pages']} for k,v in result['domains'].items()},'actual_cost_usd':actual_cost,'dataforseo_ok':result['ok'],'routes':len(routes),'pages_200':len(pages),'excluded_pages':len(exclusions),'renderer_count':len(renderers),'mode':'MANUAL_CURRENT_DATA_DRY_RUN','natural_execution_id':None,'effects':{'queue_writes':0,'site_writes':0,'schedules':0,'sends':0},'limitations':['Cogny identity lacks domain property unless explicitly present; domain reads use confirmed GoogleRail identity','GSC query privacy omissions and internal limits remain','GA4/PostHog/estate UNKNOWN, not requested','Public HTML only; no JS, affiliate requests or redirects','No S14 history retimestamped; no category volume refetch']}
 summary['weekly_spend']=budget.receipt() if allow_metered else None
 summary['gap_volume_rows']=sum(1 for v in gap_volume['rows'].values() if v.get('ok'))
 summary['cogny_query_count']=len(cogny['queries'])
 summary['cogny_evidence']=cogny['binding']
 summary['limitations']=[x.replace('GA4/PostHog/estate UNKNOWN, not requested','GA4/PostHog are bounded neutral observations, not article/click attribution or cash; estate contains fresh bound analytics outputs, not inferred natural execution') for x in summary['limitations']]
 store.save('COLLECTION-RESULT.json',summary)
 return summary
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
 try:print(json.dumps(run(args.output),indent=2))
 except Exception as exc:
  print(json.dumps({'status':'BLOCKED','error_type':type(exc).__name__,'output':args.output,'replay_forbidden':'Inspect submitted journals/ledger before any subsequent billable work'}));sys.exit(1)
