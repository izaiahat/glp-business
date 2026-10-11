#!/usr/bin/env python3
"""Rebind existing MaxBounty review CTAs from API receipts and sole-writer rows.

No network call, tracker request, Sheet write or publication. This successor
uses the maintained paired CTA constructor and never relaxes current holds.
"""
import argparse,copy,hashlib,json,sys
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from build_glp_affiliate_wirein_incremental_v2 import paired_cta_tracking_successor,write_json
from glp_affiliate_link_rails import validate_tracking_capture

def project(target, receipts, snapshot):
 target,receipts=Path(target),Path(receipts)
 s=json.loads(Path(snapshot).read_text()); rows=s['rows']
 data=target/'data'; raw=(data/'provider-page-contracts.json').read_bytes(); contracts=json.loads(raw)
 migration=json.loads((data/'provider-cta-identity-v2.json').read_text())
 registry=json.loads((data/'program-registry.json').read_text())
 authority=json.loads((data/'go-runtime-promotion-authority.json').read_text())
 updates={}; changes={}; sources={}
 for file in sorted(receipts.glob('maxbounty-*.json')):
  c=json.loads(file.read_text());validate_tracking_capture(c)
  if c['network']!='MaxBounty':raise RuntimeError('review_tracking_network_not_supported')
  capture=Path('/home/ubuntu/business')/c['capture_path']; b=capture.read_bytes()
  if hashlib.sha256(b).hexdigest()!=c['capture_sha256']:raise RuntimeError('review_tracking_capture_drift')
  receipt=json.loads(b); api=receipt['campaign']; issued=receipt['tracking_api_receipt'];link=issued['response']['tracking-link'];offer=str(api['campaign_id'])
  if issued.get('publisher_id')!='803003' or api['details']['affiliate_campaign_status']!='Approved' or issued['response'].get('success') is not True or issued['tracking_clicks']!=0 or issued['status']!=200:raise RuntimeError('review_tracking_not_approved')
  query=parse_qs(urlparse(link).query)
  if query.get('a')!=['803003'] or query.get('o')!=[offer] or query.get('l') not in [[str(lp['landing_page_id'])] for lp in api['landing_pages']] or link!=c['tracking_link']:raise RuntimeError('review_tracking_identity_mismatch')
  matched=[r for r in rows if r['sheet_row']==c['sheet_row']]
  if len(matched)!=1:raise RuntimeError('review_tracking_sheet_identity_missing')
  cells=matched[0]['cells'];pid=cells[9]
  if cells[4]!='MaxBounty' or cells[10]!='approved_active_tracking_ready' or cells[14]!=link or cells[19] not in (True,'TRUE','true'):raise RuntimeError('review_tracking_sole_writer_not_ready')
  programs=[p for p in registry['programs'] if p['program_id']==pid]
  if len(programs)!=1 or programs[0].get('affiliate_relationship_snapshot',{}).get('promotion_denied') is not False:raise RuntimeError('review_tracking_relationship_denied_or_unknown')
  existing=authority['programs'].get(pid)
  if existing and (existing.get('status')=='WITHHELD' or existing.get('promotion_authorized') is False):raise RuntimeError('review_tracking_current_hold')
  matches=[r for r in contracts['providers'] if r['program_id']==pid]
  if len(matches)!=1 or matches[0]['network_contract']['network']!='MaxBounty' or str(matches[0]['network_contract']['immutable_offer_id'])!=offer:raise RuntimeError('review_tracking_contract_identity_drift')
  row=matches[0];old=row['network_contract']['tracking_url'];updates[pid]=link;changes[old]=link
  row['network_contract']['tracking_url']=link;row['row_contract']['affiliate_href']=link
  if 'tracking_host' in row:row['tracking_host']=urlparse(link).hostname
  if 'affiliate_tracking_host' in row['row_contract']:row['row_contract']['affiliate_tracking_host']=urlparse(link).hostname
  sources[pid]=receipt
  authority['programs'][pid]={'status':'VERIFIED','promotion_authorized':True,'constructor_verified':True,'network':'MaxBounty','direct_only':True,'issued_link':link,'tracking_link_sha256':hashlib.sha256(link.encode()).hexdigest(),'reason':'approved_api_link_sole_writer_ready','source_receipt':f'data/review-tracking-receipts.json#{pid}'}
 if not updates:raise RuntimeError('review_tracking_receipts_empty')
 successor=paired_cta_tracking_successor(raw,contracts,migration,updates)
 # Existing generated comparison hrefs and approval corroboration receive the
 # same exact string. All unrelated values, metadata and prose are retained.
 def replace(value):
  if isinstance(value,dict):
   result={k:replace(v) for k,v in value.items()}
   pid=value.get('program_id') or value.get('existing_release_alias')
   if pid in updates:
    if 'affiliate_tracking_host' in result:result['affiliate_tracking_host']=urlparse(updates[pid]).hostname
    if 'tracking_host' in result:result['tracking_host']=urlparse(updates[pid]).hostname
   return result
  if isinstance(value,list):return [replace(v) for v in value]
  if isinstance(value,str):return changes.get(value,value)
  return value
 outputs={'provider-page-contracts.json':contracts,'provider-cta-identity-v2.json':successor,'go-runtime-promotion-authority.json':authority,'review-tracking-receipts.json':{'schema':'glp-review-approved-tracking-v1','tracking_clicks':0,'programs':sources}}
 for rel in ('affiliate-wire-in-approvals.json','money-page-specs.json'):
  outputs[rel]=replace(json.loads((data/rel).read_text()))
 for rel,value in outputs.items():write_json(data/rel,value)
 return {'status':'PASS','providers':sorted(updates),'changed_files':[f'data/{p}' for p in outputs],'tracking_visits':0,'holds_unchanged':True}
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--target',required=True);p.add_argument('--receipts',required=True);p.add_argument('--snapshot',required=True);a=p.parse_args();print(json.dumps(project(a.target,a.receipts,a.snapshot),indent=2))
if __name__=='__main__':main()
