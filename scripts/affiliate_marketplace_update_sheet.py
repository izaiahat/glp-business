#!/usr/bin/env python3
"""Idempotently upsert verified cross-marketplace affiliate relationships into canonical Sheet."""
from __future__ import annotations
import argparse,fcntl,hashlib,json,os,re
from datetime import date,datetime,timezone
from pathlib import Path
from typing import Any
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from affiliate_sheet_schema_guard import (
 EXPECTED_HEADERS, EXPECTED_RANGE, column_letter, require_api_capabilities_value,
 require_checkbox_schema_pass, require_marketplace_parent_rows_not_mutated,
 require_vertical_value, schema_requests,
)
try:
 from affiliate_operator_rulings import load_snapshot,policy_receipt
except ImportError:
 from scripts.affiliate_operator_rulings import load_snapshot,policy_receipt
try:
 from affiliate_niche_geo_filter import choke_candidate,is_https_application_url
except ImportError:
 from scripts.affiliate_niche_geo_filter import choke_candidate,is_https_application_url
SHEET_ID='1P2Tr6ePYN9Q35_V90FyHs6ONUwam4iIErNEftdJPL78';TAB='Affiliate Applications';TOKEN=Path('/home/ubuntu/.hermes/secrets/glp-scout-ops-google/google_token.json')
LOCK=Path('/home/ubuntu/business/locks/glp-affiliate-sheet.lock')
HEADERS=list(EXPECTED_HEADERS)
NETWORK={
 'CJ Affiliate':{'login':'https://login.cj.com/','publisher_id':'8010186'},
 'Awin':{'login':'https://ui.awin.com/','publisher_id':'2979387'},
 'Partnerize':{'login':'https://console.partnerize.com/','publisher_id':'1100l432092'},
 'Katalys':{'login':'https://auth.katalys.com/login','publisher_id':'13031'},
 'Rakuten Advertising':{'login':'https://publisher.rakutenadvertising.com/','publisher_id':''},
 'HealthTrader':{'login':'https://affiliates.healthtrader.com/login','publisher_id':''},
 'Levanta':{'login':'https://app.levanta.io/auth/sign-in','publisher_id':''},
 'Digistore24 Marketplace':{'login':'https://www.digistore24-app.com/','publisher_id':''},
 'PartnerStack Network':{'login':'https://dash.partnerstack.com/','publisher_id':''},
 'Refersion Marketplace':{'login':'https://auth.refersion.com/','publisher_id':''}}
NETWORK['MaxBounty Network']={'login':'https://affiliates.maxbounty.com/','publisher_id':''}
SUCCESS={
 'SUBMITTED_VERIFIED','PENDING_VERIFIED','INVITED_VERIFIED','APPROVED_VERIFIED',
 'JOINED_VERIFIED','AUTO_APPROVED_VERIFIED','ALREADY_RELATIONSHIP',
 'PROVIDER_RECORD_UNAVAILABLE_VERIFIED','BLOCKED_ACCOUNT_PREREQ_VERIFIED',
 'BLOCKED_LEGAL_HARD_TRIGGER_VERIFIED','ATTEMPTED_ROUTE_BLOCKED_VERIFIED',
 'REJECTED_VERIFIED',
}
MUTABLE_COLUMNS=(10,11,18,23,24,25,26)

def gate_sheet_candidate(candidate, operation, persist=True, parking_path=None):
 candidate=dict(candidate)
 candidate.setdefault('application_url',candidate.get('apply_url') or candidate.get('program_url') or '')
 return choke_candidate(candidate,require_application_url=True,operation=operation,persist=persist,parking_path=parking_path)

def write_relation_transitions(candidates, persist=True):
 """Build the relation-transition subset without touching Google."""
 allowed=[];parked=[]
 for candidate in candidates or []:
  gate=gate_sheet_candidate(candidate,'affiliate_marketplace_update_sheet.write_relation_transitions',persist=persist)
  (allowed if gate['operational'] else parked).append(candidate if gate['operational'] else {'candidate':candidate,'gate':gate})
 return {'schema':'affiliate_relation_transition_plan_v1','candidates':allowed,'parked_candidates':parked,'no_sheet_write':True}

def creds():
 c=Credentials.from_authorized_user_info(json.loads(TOKEN.read_text()));
 if c.expired and c.refresh_token:c.refresh(Request())
 return c
def pad(r):return (r+['']*len(HEADERS))[:len(HEADERS)]
def date_serial(value):
 if value in (None,''):return ''
 if isinstance(value,(int,float)):return value
 parsed=value.date() if isinstance(value,datetime) else value if isinstance(value,date) else date.fromisoformat(str(value)[:10])
 return (parsed-date(1899,12,30)).days
def norm(v):return re.sub(r'[^a-z0-9]+','',str(v or '').lower())
def values_sha(v):return hashlib.sha256(json.dumps(v,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
def row_has_business_value(row):
 p=pad(row)
 return any(bool(value) if column in (1,11,19,20) else bool(str(value).strip()) for column,value in enumerate(p))
def substantive_values_sha(values):
 rows=[pad(row) for row in values]
 while len(rows)>1 and not row_has_business_value(rows[-1]):rows.pop()
 return values_sha(rows)
def key(r):return f"{str(r.get('network') or '').strip()}:{str(r.get('program_id') or '').strip()}"
def require_operator_ruling_clear(programs,snapshot):
 for program in programs:
  name=str(program.get('name') or program.get('provider') or program.get('program_name') or '').strip()
  if not name:
   if any(rule.verdict=='HOLD' for rule in snapshot.rulings.values()):raise RuntimeError('operator_ruling_identity_missing:parent_must_bind_sheet_target_name')
   continue
  decision=snapshot.evaluate_program(name)
  if decision is not None and decision.verdict=='HOLD':raise RuntimeError(f'operator_ruling_hold:{decision.instruction_key}')
def identity_from_row(r):
 p=pad(r);network=str(p[4]).strip();text=f"{p[5]} {p[23]}"
 if network in {'Digistore24 Marketplace','PartnerStack Network','Refersion Marketplace','MaxBounty Network'} and str(p[2]).strip() in {'Digistore24','PartnerStack Network','Refersion Marketplace','MaxBounty'}:return f'{network}:network-account'
 if (network in {'HealthTrader','Levanta'} and str(p[2]).strip()==network and str(p[3]).strip().lower()=='network rail'
     and is_https_application_url(p[5]) and is_https_application_url(p[6]) and str(p[7]).strip()):return f'{network}:network-account'
 pats={'CJ Affiliate':r'(?:CJ Affiliate programme|CJ advertiser|advertiserId[=:/]|provider_program_id=)\s*(\d+)','Awin':r'(?:Awin (?:programme|offer)|advertiserId[=:/]|programme[/ ]|merchant-profile/|provider_program_id=)\s*(\d+)','Partnerize':r'(?:Partnerize (?:campaign|programme)|campaignId[=:/]|provider_program_id=)\s*([0-9]+l[0-9]+)','Katalys':r'(?:Katalys (?:offer|programme)|Program ID[=:/]|explore/|provider_program_id=)\s*(\d+)','Rakuten Advertising':r'(?:Rakuten (?:advertiser|programme)|MID[=:/]|provider_program_id=)\s*(\d+)','HealthTrader':r'(?:HealthTrader programme|provider_program_id=)\s*([A-Za-z0-9_-]+)','Levanta':r'(?:Levanta programme|provider_program_id=)\s*([A-Za-z0-9_-]+)'}
 m=re.search(pats.get(network,r'a^'),text,re.I);return f'{network}:{m.group(1)}' if m else ''
def legacy_identity_collisions(targets,by_key,name_rows):
 out=[]
 for p in targets:
  k=key(p);name_key=f"{p['network']}:{norm(p['name'])}"
  if k not in by_key and name_rows.get(name_key):out.append({'identity':k,'name':p['name'],'rows':name_rows[name_key]})
 return out
def cell_equal(actual,expected):
 return str(actual).upper()==str(expected).upper() if isinstance(expected,bool) else str(actual)==str(expected)
def changed_cells(values,assignments,created_rows=()):
 created=set(created_rows);out=[]
 for row,program,expected in assignments:
  actual=pad(values[row-1] if len(values)>=row else []);columns=range(len(HEADERS)) if row in created else MUTABLE_COLUMNS
  for column in columns:
   if not cell_equal(actual[column],expected[column]):out.append((row,program,column,expected[column]))
 return out
def apply_value_assignments(svc,cells):
 if not cells:return False
 require_marketplace_parent_rows_not_mutated((row,column) for row,_,column,_ in cells)
 svc.spreadsheets().values().batchUpdate(spreadsheetId=SHEET_ID,body={'valueInputOption':'RAW','data':[{'range':f"'{TAB}'!{column_letter(column)}{row}",'values':[[value]]} for row,_,column,value in cells]}).execute();return True
def zero_sheet_write(changes,created_rows,rows_added,schema_write_requests):
 return not changes and not created_rows and int(rows_added)==0 and int(schema_write_requests)==0
def schema_materialization_is_safe(values_before,values_after,created_rows):
 """Allow Sheets to materialize FALSE only in checkbox cells of newly prepared rows."""
 created=set(created_rows);row_count=max(len(values_before),len(values_after))
 for row_num in range(1,row_count+1):
  before=pad(values_before[row_num-1] if row_num<=len(values_before) else []);after=pad(values_after[row_num-1] if row_num<=len(values_after) else [])
  if row_num not in created:
   if before!=after:return False
   continue
  for column,(old,new) in enumerate(zip(before,after)):
   if column in (1,11,19,20) and old in ('',False) and new in ('',False):continue
   if old!=new:return False
 return True
def normalized_sheet_values(values,row_count=None):
 count=max(len(values),int(row_count or 0));return [pad(values[index] if index<len(values) else []) for index in range(count)]
def expected_values_after_assignments(values,assignments):
 count=max([len(values),*[row for row,_,_ in assignments]]) if assignments else len(values)
 expected=normalized_sheet_values(values,count)
 for row,_,assigned in assignments:expected[row-1]=pad(assigned)
 return expected
def extended_basic_filter(current,sheet_id,row_count):
 if not current:return None
 updated=json.loads(json.dumps(current));rng=dict(updated.get('range') or {});rng.update({'sheetId':int(sheet_id),'startRowIndex':0,'endRowIndex':int(row_count),'startColumnIndex':0,'endColumnIndex':len(HEADERS)});updated['range']=rng
 return updated if updated!=current else None
def make_row(p,app,rel,existing):
 existing=pad(existing);network=p['network'];decision=str(p.get('decision') or '');state=str(rel.get('relationship') or rel.get('state') or '').lower();outcome=str(app.get('outcome_class') or '').upper();invited=app.get('status')=='INVITED_VERIFIED' and state in {'notjoined','invited'};approved=state in {'approved','joined','active'} or (not state and app.get('status') in {'APPROVED_VERIFIED','JOINED_VERIFIED','AUTO_APPROVED_VERIFIED'});unavailable=state in {'unavailable','retired','provider record unavailable'};duplicate=decision=='DUPLICATE_DO_NOT_USE'
 network_account=str(p.get('program_id') or '')=='network-account' or str(p.get('category') or '').strip().lower()=='network rail'
 outcome_status={
  'ACCOUNT_PREREQUISITE':'application_blocked_account_prerequisite',
  'LEGAL_HARD_TRIGGER':'application_blocked_legal_terms',
  'ROUTE_BLOCKED':'application_attempted_route_blocked',
  'REJECTED':'application_rejected',
 }
 if outcome in outcome_status:status=outcome_status[outcome]
 elif network_account:status='duplicate_do_not_use' if duplicate else ('provider_record_unavailable_or_retired' if unavailable else ('approved_marketplace_access' if approved else 'application_pending'))
 else:status='duplicate_do_not_use' if duplicate else ('provider_record_unavailable_or_retired' if unavailable else (('approved_active_tracking_ready' if (rel.get('tracking_link') or app.get('tracking_link') or existing[14]) else 'approved_active_not_wired') if approved else ('invited_pending_acceptance' if invited else 'applied_pending_brand_approval')))
 signup_complete=False if outcome in outcome_status else not invited
 tracking=str(rel.get('tracking_link') or app.get('tracking_link') or existing[14] or '');approval='' if duplicate else (date_serial(p.get('approval_date') or existing[18]) if approved and (p.get('approval_date') or existing[18]) else '')
 checked=app.get('verified_at_utc') or app.get('completed_at_utc') or existing[24] or datetime.now(timezone.utc).replace(microsecond=0).isoformat();notes=(f"{network} offer {p['program_id']}; application receipt={app.get('status')}; outcome={outcome or 'RELATIONSHIP_STATE'}; relationship={rel.get('relationship') or rel.get('state')}; blocker={app.get('blocker') or ''}; evidence={p.get('research_artifact') or 'affiliate-marketplace-expansion-2026-07-17'}. {p.get('notes') or ''}")[:5000]
 if p.get('preserve_notes') and any(str(value).strip() for value in existing):notes=str(existing[23] or '')
 if p.get('preserve_checked') and any(str(value).strip() for value in existing):checked=existing[24]
 if any(str(value).strip() for value in existing):
  if duplicate and (tracking or bool(existing[19]) or bool(existing[20])):raise RuntimeError(f"duplicate_route_requires_link_dark:{key(p)}")
  row=list(existing);row[10]=status;row[11]=signup_complete;row[18]=approval;row[23]=notes;row[24]=checked;row[25]=require_vertical_value(p.get('vertical'))
  if 'api_capabilities' in p:row[26]=require_api_capabilities_value(p.get('api_capabilities'))
  return row
 wire=bool(approved and tracking)
 return [p.get('priority') or 'A',True,p['name'],p.get('category') or 'GLP-1 / metabolic health',network,p.get('apply_url') or '',p.get('official_url') or '',p.get('fit_reason') or p.get('reason') or '',p.get('suggested_asset') or 'GLP Clinic Scout provider comparison / patient education',p.get('seo_slug') or '',status,signup_complete,NETWORK[network]['login'],NETWORK[network]['publisher_id'],tracking,'',p.get('commission_terms') or '',p.get('cookie_window') or 'Not stated',approval,wire,False,('not_clicked_preserve_attribution' if tracking else 'not_tested_no_link'),p.get('compliance_notes') or 'FTC affiliate disclosure; educational content only; no unsupported medical claims; use only brand-approved factual claims.',notes,checked,require_vertical_value(p.get('vertical')),require_api_capabilities_value(p.get('api_capabilities'))]
def load_baseline_rows(path):
 if not path:return {}
 payload=json.loads(Path(path).read_text());rows={}
 for item in payload.get('all_rows',[]):
  if not isinstance(item,dict) or item.get('row') is None:continue
  values=[item.get(header,'') for header in HEADERS]
  for column in (1,11,19,20):
   if str(values[column]).upper() in {'TRUE','FALSE'}:values[column]=str(values[column]).upper()=='TRUE'
  rows[int(item['row'])]=values
 return rows
ACCESS_MUTABLE_COLUMNS=(10,11,23,24)
def load_access_reconciliation(path):
 evidence=json.loads(Path(path).read_text())
 if evidence.get('schema')!='affiliate_access_sheet_reconciliation_v1' or evidence.get('status')!='PASS':raise RuntimeError('access_reconciliation_evidence_invalid')
 if evidence.get('network')!='Impact.com' or evidence.get('provider_access')!='CONFIRMED' or evidence.get('marketplace_status')!='DECLINED':raise RuntimeError('impact_access_authority_invalid')
 sources=evidence.get('sources') or []
 if not sources:raise RuntimeError('access_reconciliation_sources_missing')
 reports=Path('/home/ubuntu/business/reports').resolve()
 for source in sources:
  source_path=Path(str(source.get('path') or '')).resolve()
  if not source_path.is_relative_to(reports) or not source_path.is_file():raise RuntimeError('access_reconciliation_source_invalid')
  if hashlib.sha256(source_path.read_bytes()).hexdigest()!=source.get('sha256'):raise RuntimeError('access_reconciliation_source_hash_mismatch')
 updates=evidence.get('updates') or []
 if not updates or len({int(x.get('row') or 0) for x in updates})!=len(updates):raise RuntimeError('access_reconciliation_updates_empty_or_duplicated')
 return evidence
def access_reconciliation_assignments(values,evidence):
 assignments=[]
 for update in evidence['updates']:
  row_number=int(update['row']);existing=pad(values[row_number-1] if len(values)>=row_number else [])
  if existing[2]!=update['program'] or existing[4] not in update.get('expected_network_routes',[]):raise RuntimeError(f"access_reconciliation_identity_drift:row_{row_number}")
  if str(existing[10]) not in [str(x) for x in update.get('expected_statuses',[])]:raise RuntimeError(f"access_reconciliation_status_drift:row_{row_number}:{existing[10]}")
  expected=list(existing)
  if update.get('new_status') is not None:expected[10]=str(update['new_status'])
  if update.get('signup_complete') is not None:expected[11]=bool(update['signup_complete'])
  marker=str(update['note_marker']);notes=str(existing[23] or '').strip()
  if marker not in notes:expected[23]=(notes+' '+marker+' '+str(update['notes_addendum']).strip()).strip()
  expected[24]=str(evidence['captured_at_utc'])
  assignments.append((row_number,{'network':'Impact.com','program_id':str(row_number),'name':update['program']},expected))
 return assignments
def reconcile_access_sheet(path,out,apply):
 evidence=load_access_reconciliation(path);LOCK.parent.mkdir(parents=True,exist_ok=True);lock_handle=LOCK.open('a+');os.chmod(LOCK,0o600)
 try:fcntl.flock(lock_handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:raise RuntimeError('portfolio_sheet_apply_lock_busy')
 svc=build('sheets','v4',credentials=creds(),cache_discovery=False);values=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE').execute().get('values',[])
 if not values or pad(values[0])!=HEADERS:raise RuntimeError('canonical header mismatch')
 schema_preflight=require_checkbox_schema_pass(svc,SHEET_ID,TAB);baseline_sha=substantive_values_sha(values);assignments=access_reconciliation_assignments(values,evidence);changes=changed_cells(values,assignments)
 if any(column not in ACCESS_MUTABLE_COLUMNS for _,_,column,_ in changes):raise RuntimeError('access_reconciliation_column_escape')
 evidence_sha=hashlib.sha256(Path(path).read_bytes()).hexdigest()
 if not apply:
  receipt={'schema':'affiliate_access_sheet_reconciliation_receipt_v1','status':'PASS','dry_run':True,'captured_at_utc':datetime.now(timezone.utc).isoformat(),'evidence_sha256':evidence_sha,'full_sheet_values_precondition_sha256':baseline_sha,'rows_planned':len(assignments),'value_cells_planned':len(changes),'no_sheet_write':True,'schema_preflight_status':schema_preflight['status'],'provider_writes':0,'applications':0,'tracking_links_generated':0}
  out.write_text(json.dumps(receipt,indent=2)+'\n');os.chmod(out,0o600);print(json.dumps({'status':'PASS','dry_run':True,'rows_planned':len(assignments),'value_cells_planned':len(changes),'no_sheet_write':True,'output':str(out)}));return 0
 live=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE').execute().get('values',[])
 if substantive_values_sha(live)!=baseline_sha:raise RuntimeError('sheet_concurrency_drift_before_access_write')
 apply_value_assignments(svc,changes);reread=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE').execute().get('values',[]);expected=expected_values_after_assignments(values,assignments);observed=normalized_sheet_values(reread,len(expected));schema_post=require_checkbox_schema_pass(svc,SHEET_ID,TAB);second=access_reconciliation_assignments(reread,evidence);second_changes=changed_cells(reread,second)
 rows=[{'row':row,'program':program['name'],'status':pad(reread[row-1])[10],'signup_complete':pad(reread[row-1])[11],'last_checked':pad(reread[row-1])[24]} for row,program,_ in assignments]
 status='PASS' if expected==observed and not second_changes and schema_post['status']=='PASS' else 'FAIL';receipt={'schema':'affiliate_access_sheet_reconciliation_receipt_v1','status':status,'dry_run':False,'captured_at_utc':datetime.now(timezone.utc).isoformat(),'evidence_sha256':evidence_sha,'full_sheet_values_precondition_sha256':baseline_sha,'expected_post_values_sha256':values_sha(expected),'observed_post_values_sha256':values_sha(observed),'full_sheet_values_match_expected':expected==observed,'rows':rows,'value_cells_written':len(changes),'value_rows_written':len({x[0] for x in changes}),'idempotent_rerun_pass':not second_changes,'schema_preflight_status':schema_preflight['status'],'schema_post_status':schema_post['status'],'provider_writes':0,'applications':0,'tracking_links_generated':0,'tracking_links_clicked':0,'external_sends':0}
 out.write_text(json.dumps(receipt,indent=2)+'\n');os.chmod(out,0o600);print(json.dumps({'status':status,'rows':len(rows),'value_cells_written':len(changes),'idempotent_rerun_pass':not second_changes,'full_sheet_values_match_expected':expected==observed,'output':str(out)}));return 0 if status=='PASS' else 2
# --- supply-discovery candidate mode -----------------------------------------
# The affiliate supply-discovery lane finds NEW direct programs on the open web.
# They land here as CANDIDATES and nothing else: this mode may only CREATE rows,
# it writes exactly one status (`ready_to_apply`), and it can never set an
# approval, a tracking link or a wire-in flag. A row it creates is an intent to
# apply — approval truth is still established only by a provider readback on the
# reconciliation paths above.
SUPPLY_PLAN_SCHEMA='affiliate-supply-sheet-candidates-v1'
SUPPLY_CANDIDATE_STATUS='ready_to_apply'
SUPPLY_NETWORK_ROUTES={'Direct affiliate','Network signup'}
SUPPLY_COMPLIANCE_NOTE='FTC affiliate disclosure; educational content only; no unsupported medical claims; use only brand-approved factual claims.'
PLATFORM_DOMAINS={'awin.com','cj.com','impact.com','partnerize.com','katalys.com','rakutenadvertising.com','healthtrader.com','clickbank.com','digistore24.com','partnerstack.com','refersion.com','everflowclient.io','sovrn.com','levanta.io','shareasale.com','flexoffers.com','maxbounty.com','uppromote.com','goaffpro.com'}
def registrable(value):
 host=str(value or '').strip().lower()
 if '://' in host:host=host.split('://',1)[1]
 host=host.split('/')[0].split('?')[0].split('@')[-1].split(':')[0]
 if host.startswith('www.'):host=host[4:]
 parts=[p for p in host.split('.') if p]
 if len(parts)<2:return ''
 if len(parts)>=3 and parts[-2] in {'co','com','net','org','gov','ac'} and len(parts[-1])==2:return '.'.join(parts[-3:])
 return '.'.join(parts[-2:])
def load_supply_plan(path,policy_snapshot=None):
 plan=json.loads(Path(path).read_text())
 if plan.get('schema')!=SUPPLY_PLAN_SCHEMA:raise RuntimeError('supply_plan_schema_invalid')
 if plan.get('candidate_status')!=SUPPLY_CANDIDATE_STATUS or plan.get('never_marked_approved') is not True:raise RuntimeError('supply_plan_candidate_contract_invalid')
 candidates=plan.get('candidates') or []
 if not candidates:raise RuntimeError('supply_plan_empty')
 seen=set()
 for c in candidates:
  for required in ('provider','program_url','official_url','network_route','vertical','evidence'):
   if not c.get(required):raise RuntimeError(f"supply_candidate_missing_field:{required}")
  if c['network_route'] not in SUPPLY_NETWORK_ROUTES:raise RuntimeError(f"supply_candidate_route_invalid:{c['network_route']}")
  if not str(c['program_url']).lower().startswith('https://'):raise RuntimeError('supply_candidate_apply_url_not_https')
  require_vertical_value(c['vertical'])
  identity=norm(c['provider'])
  if identity in seen:raise RuntimeError(f"supply_candidate_duplicated:{c['provider']}")
  seen.add(identity)
 require_operator_ruling_clear(candidates,policy_snapshot or load_snapshot())
 return plan
def supply_existing_index(values):
 names,domains,used={},set(),set()
 for n,r in enumerate(values[1:],2):
  p=pad(r)
  if row_has_business_value(p):used.add(n)
  if p[2]:names[norm(p[2])]=n
  for column in (5,6):
   domain=registrable(p[column])
   if domain and domain not in PLATFORM_DOMAINS:domains.add(domain)
 return names,domains,used
def supply_candidate_row(c,checked):
 evidence=c.get('evidence') or {}
 notes=(f"Supply-discovery candidate; discovered_by={evidence.get('discovered_by','')}; "
        f"glp_fit_tier={evidence.get('tier','')}; reasons={','.join(evidence.get('reasons') or [])}; "
        f"evidence_source={evidence.get('source','')}; evidence_sha256={evidence.get('sha256','')}; "
        f"hosted_network={c.get('hosted_network','direct')}. Candidate only: no application submitted, "
        f"no approval claimed, link dark.")[:5000]
 return [
  c.get('priority') or 'B',True,c['provider'],c.get('category') or 'GLP-1 / metabolic health',
  c['network_route'],c['program_url'],c['official_url'],
  c.get('fit_reason') or 'Discovered GLP-1/weight-loss affiliate program pending application.',
  c.get('suggested_asset') or 'GLP Clinic Scout provider comparison / patient education','',
  SUPPLY_CANDIDATE_STATUS,False,'','','','',c.get('commission_terms') or '',
  c.get('cookie_window') or 'Not stated',date_serial(''),False,False,'not_tested_no_link',
  SUPPLY_COMPLIANCE_NOTE,notes,checked,require_vertical_value(c['vertical']),
  require_api_capabilities_value(c.get('api_capabilities')),
 ]
def write_supply_candidates(plan_path,out,apply,parking_path=None):
 policy_snapshot=load_snapshot()
 try:
  plan=load_supply_plan(plan_path,policy_snapshot)
 except RuntimeError as exc:
  # Let the shared choke own incomplete-candidate parking instead of failing
  # before the candidate reaches the local safety ledger. Other contract
  # failures remain fail-closed.
  if str(exc) not in {'supply_candidate_missing_field:program_url','supply_candidate_apply_url_not_https'}:raise
  plan=json.loads(Path(plan_path).read_text())
  if plan.get('schema')!=SUPPLY_PLAN_SCHEMA:raise
 allowed,parked=[],[]
 for candidate in plan['candidates']:
  gate=gate_sheet_candidate({**candidate,'application_url':candidate.get('program_url') or ''},'affiliate_marketplace_update_sheet.write_supply_candidates',persist=True,parking_path=parking_path)
  if gate['operational']:allowed.append(candidate)
  else:parked.append({'provider':candidate.get('provider'),'gate':gate})
 plan={**plan,'candidates':allowed,'parked_candidates':parked}
 if not allowed:
  receipt={'schema':'affiliate_supply_candidate_write_receipt_v1','status':'PASS','dry_run':bool(not apply),'plan':str(plan_path),'parked_candidates':parked,'created':[],'skipped':[],'applications':0,'provider_writes':0,'no_value_write':True,'no_sheet_write':True,'external_effects':{'google_sheet_writes':0,'provider_mutations':0}}
  out.write_text(json.dumps(receipt,indent=2)+'\n');os.chmod(out,0o600);print(json.dumps({'status':'PASS','dry_run':bool(not apply),'created_count':0,'parked_count':len(parked),'no_sheet_write':True,'output':str(out)}));return 0
 LOCK.parent.mkdir(parents=True,exist_ok=True);lock_handle=LOCK.open('a+');os.chmod(LOCK,0o600)
 try:fcntl.flock(lock_handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:raise RuntimeError('portfolio_sheet_apply_lock_busy')
 svc=build('sheets','v4',credentials=creds(),cache_discovery=False)
 meta=svc.spreadsheets().get(spreadsheetId=SHEET_ID,fields='sheets(basicFilter,properties(sheetId,title,gridProperties(rowCount,columnCount)))').execute();sheet_meta=next(x for x in meta['sheets'] if x['properties']['title']==TAB);props=sheet_meta['properties'];basic_filter_before=sheet_meta.get('basicFilter');grid_before=dict(props['gridProperties'])
 values=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE').execute().get('values',[])
 if not values or pad(values[0])!=HEADERS:raise RuntimeError('canonical header mismatch')
 schema_preflight=require_checkbox_schema_pass(svc,SHEET_ID,TAB);baseline_values_sha256=substantive_values_sha(values)
 names,domains,used=supply_existing_index(values);checked=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
 new,skipped=[],[]
 for c in plan['candidates']:
  identity=norm(c['provider'])
  if identity in names:skipped.append({'provider':c['provider'],'reason':'existing_program_name','row':names[identity]});continue
  domain=next((d for d in (registrable(c['official_url']),registrable(c['program_url'])) if d and d in domains),'')
  if domain:skipped.append({'provider':c['provider'],'reason':f'existing_domain:{domain}'});continue
  new.append(c)
  names[identity]=None
  for column in ('official_url','program_url'):
   d=registrable(c[column])
   if d and d not in PLATFORM_DOMAINS:domains.add(d)
 empty=[n for n in range(2,int(grid_before['rowCount'])+1) if n not in used];rows_added=max(0,len(new)-len(empty))
 live_before_resize=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE').execute().get('values',[])
 if substantive_values_sha(live_before_resize)!=baseline_values_sha256:raise RuntimeError('sheet_concurrency_drift_before_resize')
 if rows_added and apply:svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID,body={'requests':[{'appendDimension':{'sheetId':props['sheetId'],'dimension':'ROWS','length':rows_added}}]}).execute()
 if rows_added:empty.extend(range(int(grid_before['rowCount'])+1,int(grid_before['rowCount'])+rows_added+1))
 assignments,created=[],[]
 for c in new:
  if not empty:raise RuntimeError('no blank row; refusing grid resize')
  row=empty.pop(0);created.append({'provider':c['provider'],'row':row});assignments.append((row,{'network':c['network_route'],'program_id':c['provider'],'name':c['provider']},supply_candidate_row(c,checked)))
 changes=changed_cells(values,assignments,[x['row'] for x in created])
 if any(column in {14,18,19,20} and value not in ('',False) for _,_,column,value in changes):raise RuntimeError('supply_candidate_wire_or_approval_escape')
 if any(expected[10]!=SUPPLY_CANDIDATE_STATUS for _,_,expected in assignments):raise RuntimeError('supply_candidate_status_escape')
 filter_update=extended_basic_filter(basic_filter_before,props['sheetId'],int(grid_before['rowCount'])+rows_added)
 base={'schema':'affiliate_supply_candidate_write_receipt_v1','captured_at_utc':datetime.now(timezone.utc).isoformat(),'sheet_id':SHEET_ID,'tab':TAB,'plan':str(plan_path),'plan_sha256':hashlib.sha256(Path(plan_path).read_bytes()).hexdigest(),'candidate_status':SUPPLY_CANDIDATE_STATUS,'created':created,'skipped':skipped,'parked_candidates':parked,'skipped_count':len(skipped),'schema_preflight_status':schema_preflight['status'],'full_sheet_values_precondition_sha256':baseline_values_sha256,'approvals_written':0,'tracking_links_written':0,'applications':0,'provider_writes':0,'operator_rulings':policy_receipt(policy_snapshot)}
 if not apply:
  receipt={**base,'status':'PASS','dry_run':True,'grid_rows_planned':rows_added,'value_cells_planned':len(changes),'value_rows_planned':len({x[0] for x in changes}),'schema_write_requests_planned':len(created)+len(schema_requests(props['sheetId'],[x['row'] for x in created]))+(1 if filter_update else 0),'no_value_write':True,'no_sheet_write':True}
  out.write_text(json.dumps(receipt,indent=2)+'\n');os.chmod(out,0o600);print(json.dumps({'status':'PASS','dry_run':True,'created_count':len(created),'skipped_count':len(skipped),'value_cells_planned':len(changes),'no_sheet_write':True,'output':str(out)}));return 0
 req=[]
 if created:
  source=next((n for n,r in enumerate(values[1:],2) if str(pad(r)[4])=='Katalys'),None)
  if not source:raise RuntimeError('format source missing')
  for x in created:req.append({'copyPaste':{'source':{'sheetId':props['sheetId'],'startRowIndex':source-1,'endRowIndex':source,'startColumnIndex':0,'endColumnIndex':len(HEADERS)},'destination':{'sheetId':props['sheetId'],'startRowIndex':x['row']-1,'endRowIndex':x['row'],'startColumnIndex':0,'endColumnIndex':len(HEADERS)},'pasteType':'PASTE_FORMAT','pasteOrientation':'NORMAL'}})
  req.extend(schema_requests(props['sheetId'],[x['row'] for x in created]))
 if filter_update:req.append({'setBasicFilter':{'filter':filter_update}})
 if req:svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID,body={'requests':req}).execute()
 live_before_write=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE').execute().get('values',[])
 if substantive_values_sha(live_before_write)!=baseline_values_sha256:raise RuntimeError('sheet_concurrency_drift_before_value_write')
 apply_value_assignments(svc,changes)
 reread=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE').execute().get('values',[])
 expected=expected_values_after_assignments(values,assignments);observed=normalized_sheet_values(reread,len(expected));schema_post=require_checkbox_schema_pass(svc,SHEET_ID,TAB);second_changes=changed_cells(reread,assignments)
 status='PASS' if expected==observed and not second_changes and schema_post['status']=='PASS' else 'FAIL'
 receipt={**base,'status':status,'dry_run':False,'grid_rows_added':rows_added,'value_cells_written':len(changes),'value_rows_written':len({x[0] for x in changes}),'expected_post_values_sha256':values_sha(expected),'observed_post_values_sha256':values_sha(observed),'full_sheet_values_match_expected':expected==observed,'idempotent_rerun_pass':not second_changes,'schema_post_status':schema_post['status'],'rows':[{'provider':p['name'],'row':n,'status_value':pad(reread[n-1])[10],'vertical':pad(reread[n-1])[25]} for n,p,_ in assignments]}
 out.write_text(json.dumps(receipt,indent=2)+'\n');os.chmod(out,0o600);print(json.dumps({'status':status,'created_count':len(created),'skipped_count':len(skipped),'value_cells_written':len(changes),'idempotent_rerun_pass':not second_changes,'full_sheet_values_match_expected':expected==observed,'output':str(out)}));return 0 if status=='PASS' else 2

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--classification');ap.add_argument('--applications');ap.add_argument('--relationships');ap.add_argument('--access-reconciliation');ap.add_argument('--supply-candidates',help='Create ready_to_apply candidate rows from a supply-discovery plan.');ap.add_argument('--preserve-baseline');ap.add_argument('--output',required=True);ap.add_argument('--apply',action='store_true',help='Perform the planned Sheet writes; default is zero-write planning only.');a=ap.parse_args();out=Path(a.output)
 if a.supply_candidates:
  if any((a.classification,a.applications,a.relationships,a.access_reconciliation,a.preserve_baseline)):raise RuntimeError('supply_candidates_mode_mixed_inputs')
  return write_supply_candidates(a.supply_candidates,out,a.apply)
 if a.access_reconciliation:
  if any((a.classification,a.applications,a.relationships,a.preserve_baseline)):raise RuntimeError('access_reconciliation_mode_mixed_inputs')
  return reconcile_access_sheet(a.access_reconciliation,out,a.apply)
 if not all((a.classification,a.applications,a.relationships)):raise RuntimeError('classification_applications_relationships_required')
 baseline_rows=load_baseline_rows(a.preserve_baseline)
 LOCK.parent.mkdir(parents=True,exist_ok=True);lock_handle=LOCK.open('a+');os.chmod(LOCK,0o600)
 try:fcntl.flock(lock_handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:raise RuntimeError('portfolio_sheet_apply_lock_busy')
 cls=json.loads(Path(a.classification).read_text());apps=json.loads(Path(a.applications).read_text());rels=json.loads(Path(a.relationships).read_text());ab={key(x):x for x in apps.get('programs',[])};rb={key(x):x for x in rels.get('programs',[])}
 targets=[]
 for p in cls.get('programs',[]):
  k=key(p)
  if p.get('decision') in {'APPLY','ALREADY_RELATIONSHIP','RECONCILE_UNAVAILABLE','DUPLICATE_DO_NOT_USE'} and ab.get(k,{}).get('status') in SUCCESS and rb.get(k,{}).get('status')=='PASS':targets.append(p)
 if not targets or len({key(x) for x in targets})!=len(targets):raise RuntimeError('verified target set empty or duplicated')
 parked_targets=[];gated_targets=[]
 for target in targets:
  gate=gate_sheet_candidate(target,'affiliate_marketplace_update_sheet.relation_transition',persist=True)
  if gate['operational']:gated_targets.append(target)
  else:parked_targets.append({'program_id':target.get('program_id'),'name':target.get('name'),'gate':gate})
 targets=gated_targets
 if not targets:
  rec={'status':'PASS','dry_run':bool(not a.apply),'schema':'affiliate_relation_transition_receipt_v1','parked_targets':parked_targets,'created':[],'updated':[],'value_cells_planned':0,'no_value_write':True,'no_sheet_write':True,'external_effects':{'google_sheet_writes':0,'provider_mutations':0}}
  out.write_text(json.dumps(rec,indent=2)+'\n');os.chmod(out,0o600);print(json.dumps({'status':'PASS','dry_run':bool(not a.apply),'parked_count':len(parked_targets),'no_sheet_write':True,'output':str(out)}));return 0
 policy_snapshot=load_snapshot();require_operator_ruling_clear(targets,policy_snapshot)
 svc=build('sheets','v4',credentials=creds(),cache_discovery=False);meta=svc.spreadsheets().get(spreadsheetId=SHEET_ID,fields='sheets(basicFilter,properties(sheetId,title,gridProperties(rowCount,columnCount)))').execute();sheet_meta=next(x for x in meta['sheets'] if x['properties']['title']==TAB);props=sheet_meta['properties'];basic_filter_before=sheet_meta.get('basicFilter');grid_before=dict(props['gridProperties']);values=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE').execute().get('values',[])
 if not values or pad(values[0])!=HEADERS:raise RuntimeError('canonical header mismatch')
 schema_preflight=require_checkbox_schema_pass(svc,SHEET_ID,TAB);schema_repair={'status':'SKIPPED_WRITER_FAILS_CLOSED','repair_rows':[]};baseline_values_sha256=substantive_values_sha(values)
 before_nonblank=sum(1 for r in values[1:] if row_has_business_value(r));used=set();by_key={};name_rows={};duplicates={}
 for n,r in enumerate(values[1:],2):
  p=pad(r)
  if row_has_business_value(p):used.add(n)
  k=identity_from_row(p)
  if k:
   if k in by_key:duplicates.setdefault(k,[by_key[k]]).append(n)
   else:by_key[k]=n
  if p[4] and p[2]:name_rows.setdefault(f'{p[4]}:{norm(p[2])}',[]).append(n)
 if duplicates:raise RuntimeError(f'preexisting duplicate marketplace identities: {duplicates}')
 ambiguous=legacy_identity_collisions(targets,by_key,name_rows)
 if ambiguous:raise RuntimeError(f'legacy_identity_mapping_required:{ambiguous}')
 empty=[n for n in range(2,int(grid_before['rowCount'])+1) if n not in used]
 new_needed=sum(1 for p in targets if key(p) not in by_key)
 rows_added=max(0,new_needed-len(empty))
 live_before_resize=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE').execute().get('values',[])
 if substantive_values_sha(live_before_resize)!=baseline_values_sha256:raise RuntimeError('sheet_concurrency_drift_before_resize')
 if rows_added and a.apply:
  svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID,body={'requests':[{'appendDimension':{'sheetId':props['sheetId'],'dimension':'ROWS','length':rows_added}}]}).execute()
 if rows_added:
  empty.extend(range(int(grid_before['rowCount'])+1,int(grid_before['rowCount'])+rows_added+1))
 assign=[];created=[];updated=[]
 for p in targets:
  k=key(p);row=by_key.get(k);existing=pad(values[row-1]) if row else ['']*len(HEADERS)
  if row and baseline_rows:
   baseline=baseline_rows.get(row)
   if not baseline or identity_from_row(baseline)!=k:raise RuntimeError(f'baseline_identity_mismatch:{k}:row_{row}')
   baseline=pad(baseline);current=pad(values[row-1]);baseline[25]=current[25];baseline[26]=current[26];existing=baseline
  if row:updated.append({'network':p['network'],'program_id':str(p['program_id']),'name':p['name'],'row':row})
  else:
   if p.get('decision')=='DUPLICATE_DO_NOT_USE':raise RuntimeError(f'duplicate_route_target_missing_existing_row:{k}')
   if not empty:raise RuntimeError('no blank row; refusing grid resize')
   row=empty.pop(0);created.append({'network':p['network'],'program_id':str(p['program_id']),'name':p['name'],'row':row})
  assign.append((row,p,make_row(p,ab[k],rb[k],existing)))
 changed_assignments=changed_cells(values,assign,[x['row'] for x in created])
 filter_update=extended_basic_filter(basic_filter_before,props['sheetId'],int(grid_before['rowCount'])+rows_added)
 if not a.apply:
  rec={'status':'PASS','dry_run':True,'captured_at_utc':datetime.now(timezone.utc).isoformat(),'sheet_id':SHEET_ID,'tab':TAB,'created':created,'updated':updated,'parked_targets':parked_targets,'program_count_before':before_nonblank,'program_count_after':before_nonblank,'grid_before':grid_before,'grid_rows_planned':rows_added,'value_cells_planned':len(changed_assignments),'value_rows_planned':len({x[0] for x in changed_assignments}),'schema_write_requests_planned':len(created)+len(schema_requests(props['sheetId'],[x['row'] for x in created]))+(1 if filter_update else 0),'basic_filter_extension_planned':bool(filter_update),'no_value_write':True,'no_sheet_write':True,'schema_preflight_status':schema_preflight['status'],'full_sheet_values_precondition_sha256':baseline_values_sha256}
  out.write_text(json.dumps(rec,indent=2)+'\n');os.chmod(out,0o600);print(json.dumps({'status':'PASS','dry_run':True,'created_count':len(created),'updated_count':len(updated),'grid_rows_planned':rows_added,'value_cells_planned':len(changed_assignments),'no_value_write':True,'no_sheet_write':True,'output':str(out)}));return 0
 req=[]
 if created:
  source=next((n for n,r in enumerate(values[1:],2) if 'Katalys offer 1630' in str(pad(r)[23])),None)
  if not source:source=next((n for n,r in enumerate(values[1:],2) if str(pad(r)[4])=='Katalys'),None)
  if not source:raise RuntimeError('format source missing')
  for x in created:req.append({'copyPaste':{'source':{'sheetId':props['sheetId'],'startRowIndex':source-1,'endRowIndex':source,'startColumnIndex':0,'endColumnIndex':len(HEADERS)},'destination':{'sheetId':props['sheetId'],'startRowIndex':x['row']-1,'endRowIndex':x['row'],'startColumnIndex':0,'endColumnIndex':len(HEADERS)},'pasteType':'PASTE_FORMAT','pasteOrientation':'NORMAL'}})
  req.extend(schema_requests(props['sheetId'], [x['row'] for x in created]))
 if filter_update:req.append({'setBasicFilter':{'filter':filter_update}})
 if req:svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID,body={'requests':req}).execute()
 live_before_write=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE').execute().get('values',[])
 if substantive_values_sha(live_before_write)!=baseline_values_sha256 and not schema_materialization_is_safe(values,live_before_write,[x['row'] for x in created]):raise RuntimeError('sheet_concurrency_drift_before_value_write')
 # Schema preparation may materialize FALSE values in the new row; refresh the
 # immediate typed baseline so the expected post-write hash matches Sheets.
 if substantive_values_sha(live_before_write)!=baseline_values_sha256:
  values=live_before_write;baseline_values_sha256=substantive_values_sha(values)
 apply_value_assignments(svc,changed_assignments)
 reread=svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE').execute().get('values',[]);after_nonblank=sum(1 for r in reread[1:] if row_has_business_value(r));rows=[]
 expected_full_values=expected_values_after_assignments(values,assign)
 observed_full_values=normalized_sheet_values(reread,len(expected_full_values))
 expected_post_values_sha256=values_sha(expected_full_values);observed_post_values_sha256=values_sha(observed_full_values)
 full_sheet_values_match_expected=expected_full_values==observed_full_values
 for n,p,exp in assign:
  got=pad(reread[n-1] if len(reread)>=n else []);mis=[]
  for i,x in enumerate(exp):
   ok=str(got[i]).upper()==str(x).upper() if isinstance(x,bool) else str(got[i])==str(x)
   if not ok:mis.append({'column':HEADERS[i],'expected':x,'actual':got[i]})
  rows.append({'network':p['network'],'program_id':str(p['program_id']),'name':p['name'],'row':n,'status_value':got[10],'verified_cells':len(HEADERS)-len(mis),'mismatches':mis})
 allids={};
 for n,r in enumerate(reread[1:],2):
  k=identity_from_row(r)
  if k:allids.setdefault(k,[]).append(n)
 dup={k:v for k,v in allids.items() if len(v)>1};mins=min(n for n,_,_ in assign);maxs=max(n for n,_,_ in assign);grid=svc.spreadsheets().get(spreadsheetId=SHEET_ID,ranges=[f"'{TAB}'!A{mins}:AA{maxs}"],includeGridData=True,fields='sheets(data(startRow,rowData(values(dataValidation))))').execute();blk=grid['sheets'][0]['data'][0];start=blk.get('startRow',mins-1);rd=blk.get('rowData',[]);valid=[]
 for n,p,_ in assign:
  cells=rd[n-1-start].get('values',[]) if 0<=n-1-start<len(rd) else [];checks={HEADERS[c]:((cells[c].get('dataValidation',{}).get('condition',{}).get('type')=='BOOLEAN') if c<len(cells) else False) for c in (1,11,19,20)};valid.append({'network':p['network'],'program_id':str(p['program_id']),'row':n,'checkbox_validation':checks})
 schema_audit=require_checkbox_schema_pass(svc,SHEET_ID,TAB)
 meta2=svc.spreadsheets().get(spreadsheetId=SHEET_ID,fields='sheets(basicFilter,properties(title,gridProperties(rowCount,columnCount)))').execute();sheet_after=next(x for x in meta2['sheets'] if x['properties']['title']==TAB);grid_after=sheet_after['properties']['gridProperties'];basic_filter_after=sheet_after.get('basicFilter');expected_grid={'rowCount':int(grid_before['rowCount'])+rows_added,'columnCount':int(grid_before['columnCount'])};filter_range=(basic_filter_after or {}).get('range') or {};filter_covers_grid=bool(int(filter_range.get('endRowIndex',0))>=int(expected_grid['rowCount']) and int(filter_range.get('endColumnIndex',0))>=len(HEADERS));no_value_write=not changed_assignments;no_sheet_write=zero_sheet_write(changed_assignments,created,rows_added,len(req));second_plan_changes=changed_cells(reread,assign);second_plan_zero_writes=bool(not second_plan_changes and all(key(p) in allids for p in targets) and schema_audit['status']=='PASS');idem=second_plan_zero_writes;status='PASS' if full_sheet_values_match_expected and second_plan_zero_writes and all(x['verified_cells']==len(HEADERS) for x in rows) and all(all(x['checkbox_validation'].values()) for x in valid) and schema_audit['status']=='PASS' and filter_covers_grid and not dup and int(grid_after['rowCount'])==expected_grid['rowCount'] and int(grid_after['columnCount'])==expected_grid['columnCount'] else 'FAIL'
 written_cells=changed_assignments
 rec={'status':status,'captured_at_utc':datetime.now(timezone.utc).isoformat(),'sheet_id':SHEET_ID,'tab':TAB,'created':created,'updated':updated,'program_count_before':before_nonblank,'program_count_after':after_nonblank,'grid_before':grid_before,'grid_after':grid_after,'grid_rows_added':rows_added,'idempotent_rerun_pass':idem,'post_write_second_plan_zero_writes':second_plan_zero_writes,'second_plan_value_cells':len(second_plan_changes),'no_value_write':no_value_write,'no_sheet_write':no_sheet_write,'value_cells_written':len(written_cells),'value_rows_written':len({x[0] for x in written_cells}),'typed_repair_cells':0,'schema_write_requests':len(req),'basic_filter_before':basic_filter_before,'basic_filter_after':basic_filter_after,'basic_filter_covers_grid':filter_covers_grid,'schema_preflight_status':schema_preflight['status'],'duplicates':dup,'rows':rows,'validation_checks':valid,'schema_repair':schema_repair,'full_sheet_values_precondition_sha256':baseline_values_sha256,'expected_post_values_sha256':expected_post_values_sha256,'observed_post_values_sha256':observed_post_values_sha256,'full_sheet_values_match_expected':full_sheet_values_match_expected,'full_sheet_checkbox_audit':schema_audit};out.write_text(json.dumps(rec,indent=2)+'\n');os.chmod(out,0o600);print(json.dumps({'status':status,'created_count':len(created),'updated_count':len(updated),'rows':len(rows),'grid_rows_added':rows_added,'idempotent_rerun_pass':idem,'post_write_second_plan_zero_writes':second_plan_zero_writes,'full_sheet_values_match_expected':full_sheet_values_match_expected,'basic_filter_covers_grid':filter_covers_grid,'no_value_write':no_value_write,'no_sheet_write':no_sheet_write,'value_cells_written':len(written_cells),'value_rows_written':len({x[0] for x in written_cells}),'typed_repair_cells':0,'schema_write_requests':len(req),'duplicates':dup,'full_sheet_checkbox_audit':schema_audit['status'],'output':str(out)}));return 0 if status=='PASS' else 2
if __name__=='__main__':raise SystemExit(main())
