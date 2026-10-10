#!/usr/bin/env python3
"""Automatic, fail-closed GLP affiliate approval/rate reconciliation.

Email is only a wake signal. Authenticated provider readback is required before
normal relationship writes. A separately sealed operator disposition may record
an evidence-scoped, provider-readback-required status for a direct network; that
path cannot write approval dates, IDs, links, or readiness flags. Tracking-link
generation remains operator-gated; no email send, provider draft, affiliate
redirect click, join, apply, or live-site mutation is present.
"""
from __future__ import annotations
import argparse,asyncio,fcntl,hashlib,html,json,os,re,sqlite3,subprocess,sys,time,urllib.parse,requests
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date,datetime,timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path
from katalys_fresh_relationship import relationship_snapshot as katalys_fresh_relationship_snapshot
from typing import Any
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

BUSINESS=Path('/home/ubuntu/business'); INBOX_DB=Path('/home/ubuntu/.hermes/state/email-monitor.sqlite3'); STATE_DB=Path('/home/ubuntu/.hermes/state/affiliate-approval-reconciler.sqlite3')
sys.path.insert(0,str(BUSINESS))
sys.path.insert(0,str(BUSINESS/'scripts'))
sys.path.insert(0,str(BUSINESS/'ops'))
import sheet_drain_invoke
from affiliate_sheet_schema_guard import (
 CHECKBOX_COLUMNS, EXPECTED_HEADERS, EXPECTED_RANGE, repair_non_value_schema_issues,
 require_checkbox_schema_pass, require_marketplace_parent_rows_not_mutated, schema_requests,
)
from affiliate_marketplace_update_sheet import row_has_business_value, substantive_values_sha
from affiliate_sheet_batch_writer import (
 canonical_network,
 equivalent_advertiser_name,
 equivalent_program_name,
 sheet_sync_existing_matches,
)
from affiliate_approval_policy import (
 atomic_json as approval_atomic_json,
 classify_program,
 record_approval_classification,
 screen_provider_terms,
 write_sheet_inputs,
)
from affiliate_control_validators import approval_transition, choke_candidate, provider_capability
from affiliate_niche_geo_filter import append_parking_record
from ops import discord_fmt
from inbox_control_plane.affiliate_network_registry import network_for_domain, network_for_text
from inbox_control_plane.mark_read_after_action import mark_read_after_action, save_receipt as save_mark_read_receipt
from affiliate_operator_rulings import load_snapshot, policy_receipt
from scripts.impact_api_credentials import load_impact_api_credentials
import n8n_dt
SHEET_ID='1P2Tr6ePYN9Q35_V90FyHs6ONUwam4iIErNEftdJPL78'; TAB='Affiliate Applications'; TOKEN=Path('/home/ubuntu/.hermes/secrets/glp-scout-ops-google/google_token.json'); AWIN_SECRET=Path('/home/ubuntu/.hermes/secrets/affiliate-marketplaces/awin/api.json'); PARTNERIZE_SECRET=Path('/home/ubuntu/.hermes/secrets/affiliate-marketplaces/partnerize/api.json')
HEALTHTRADER_TOKEN=Path('/home/ubuntu/.hermes/secrets/affiliate-marketplaces/healthtrader/access_token.json')
PUBLISHER_AWIN='2979387'; PUBLISHER_PARTNERIZE='1100l432092'; KATALYS_ORG='098e5455-ca72-4099-9ff9-a9dd014148e7'; KATALYS_AFF='13031'; AUTHORITY_MESSAGE='1528159191376728135'; AUTOMATION_ID='glp-affiliate-approval-reconciler-v1'
REPORT_DIR=BUSINESS/'reports/glp-affiliate-auto-reconciler'; CLOSED_PROGRAM_TOMBSTONES=BUSINESS/'reports/glp-affiliate-inbox-event-reconciliation-20260723/closed-program-tombstones.json'; HEADERS=list(EXPECTED_HEADERS)
SHEET_LOCK=BUSINESS/'locks/glp-affiliate-sheet.lock'
SHEET_UPDATE_QUEUE='hJiP3eJKBvtZ6bQU'
SHEET_SYNC_DRAIN=BUSINESS/'scripts/glp_sheet_sync_drain.py'
ALLOWED={6:'G',10:'K',11:'L',14:'O',16:'Q',17:'R',18:'S',19:'T',21:'V',24:'Y'}
# Sheets quotas are per-MINUTE, so a ladder must outlast a full quota window or
# it guarantees failure under concurrent load. (2,5,12) spanned 19s and lost a
# real approval email to a 429 on 2026-08-25. This spans 137s.
GOOGLE_API_RETRY_DELAYS_SECONDS=(2,5,12,25,45,48)
SIGNALS=('approved','application has been approved','has approved your application','welcome to the','activated','membership active','membership rejection','declined','accepted','commission rate','commission rates','commission changed','program terms changed','programme terms changed','suspended','rejected','terminated','invited you to join','getting you started with','affiliate welcome survey','affiliate-only webinar','affiliate manager','welcome to partnerstack','awin merchant id','moved over to impact','closing our awin program','program closed','programme closed')
MAXBOUNTY_SIGNAL_PATTERNS={
 'relationship_approval':(
  r'^your maxbounty application has been approved$',
  r'^your maxbounty campaign request has been approved$',
  r'\bofficially welcome you to the network\b',
 ),
 'relationship_adverse':(
  r'^your maxbounty application has been (?:rejected|declined)$',
  r'^your maxbounty campaign request has been (?:rejected|declined)$',
  r'\bmaxbounty application (?:has been|was) (?:rejected|declined)\b',
 ),
 'relationship_invitation':(
  r'\binvit(?:e|ed|ation)\b.{0,80}\b(?:join|apply)\b.{0,80}\bmaxbounty\b',
  r'\bmaxbounty\b.{0,80}\binvit(?:e|ed|ation)\b',
 ),
}
TERMINAL_EVENT_STATES=frozenset({'resolved_externally','held','archived'})
DIRECT_OPERATOR_NETWORKS=frozenset({'GoAffPro','BrandChamp'})
DIRECT_PARTNER_DOMAINS={'precisiontelemed.com':'Precision Telemed'}
DIRECT_PARTNER_NETWORKS=frozenset(DIRECT_PARTNER_DOMAINS.values())
DIRECT_ROUTINE_OUTCOME_DOMAINS={'telewellnessmd.com':'TeleWellnessMD'}
DIRECT_PARTNER_PROGRAMS={
 'Precision Telemed':{'program':'Precision Telemed','routes':frozenset({'Direct email — Carlie Cannon-Rius'})},
}
OPERATOR_EVIDENCE_COLUMNS=frozenset({10,16,21,24})
AFFILIATE_AUTH_DOMAINS={
 'Affiliati':('affiliatinetwork.com',),
 'Amazon Associates':('amazon.com',),
 'Partnerize':('partnerize.com','performancehorizon.com'),
 'Awin':('awin.com',),
 'CJ Affiliate':('cj.com','cj.net','commission-junction.com'),
 'Impact':('impact.com','impactradius.com'),
 'Impact.com':('impact.com','impactradius.com'),
 'Katalys':('katalys.com','katalys.io'),
 'MaxBounty':('maxbounty.com',),
 'XenTraffic':('via-everflow.io','everflow.io','everflowclient.io'),
}

DIRECT_OPERATOR_PROGRAMS={
 'GoAffPro':{'program':'iHealth Labs','routes':frozenset({'Direct/network verify','Direct affiliate (GoAffPro)'})},
 'BrandChamp':{'program':'Legion Athletics','routes':frozenset({'Direct affiliate (BrandChamp)'})},
}
OPERATOR_PREREQUISITE_RE=re.compile(r'\b(?:kyc|know your customer|identity verification|verify your identity|tax form|tax interview|w-?9|w-?8ben|ssn|ein verification|bank account|banking details|ach setup|payout setup|payment setup|paypal setup)\b',re.I)
IDENTITY_PREREQUISITE_RE=re.compile(r'\b(?:id proof|identity (?:verification|document)|invalid (?:id |identity )?(?:proof )?format)\b',re.I)

class AffiliateReviewRequired(RuntimeError):
 def __init__(self,exception_type,detail,receipt=None):
  self.exception_type=str(exception_type);self.detail=str(detail);self.receipt=receipt or {}
  super().__init__(f'affiliate_review_required:{self.exception_type}:{self.detail}')

class AffiliateCandidateParked(RuntimeError):
 def __init__(self,decision,reason_codes):
  self.decision=str(decision);self.reason_codes=tuple(str(value) for value in reason_codes if str(value))
  super().__init__(f"affiliate_candidate_parked:{self.decision}:{','.join(self.reason_codes[:2])}")

def now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
def require_operator_ruling_clear(name,snapshot):
 decision=snapshot.evaluate_program(name)
 if decision is not None and decision.verdict=='HOLD':raise RuntimeError(f'operator_ruling_hold:{decision.instruction_key}')
def date_serial(value):
 parsed=value.date() if isinstance(value,datetime) else value if isinstance(value,date) else date.fromisoformat(str(value)[:10])
 return (parsed-date(1899,12,30)).days
def norm(v): return re.sub(r'[^a-z0-9]+','',str(v or '').lower())
def sha(v): return hashlib.sha256(str(v).encode()).hexdigest()
def sha_bytes(v): return hashlib.sha256(v).hexdigest()
def pad(r): return (list(r)+['']*len(HEADERS))[:len(HEADERS)]
def a1(row,col): return f"'{TAB}'!{ALLOWED[col]}{row}"
def execute_google_request(request,operation):
 # 2026-08-25: a 429 escaped this wrapper and carded the operator that an
 # approval email "needs operator or provider repair". A rate limit is Google
 # saying slow down; it is not a broken email and there is nothing a human can
 # do about it. The old ladder (2,5,12) spanned 19s total, but Sheets quotas are
 # PER MINUTE -- under several concurrent lanes the window never reopened inside
 # the budget, so the ladder guaranteed failure rather than absorbing it. The
 # ladder now spans past a full quota minute and honours Retry-After when the
 # server states it. Same defect family as DEFERRED_LOCK_BUSY: a transient,
 # explicitly-retryable condition rendered as a permanent one.
 for attempt in range(len(GOOGLE_API_RETRY_DELAYS_SECONDS)+1):
  try:return request.execute()
  except HttpError as exc:
   status=int(getattr(getattr(exc,'resp',None),'status',0) or 0)
   transient=status==429 or 500<=status<=599
   if not transient or attempt>=len(GOOGLE_API_RETRY_DELAYS_SECONDS):raise
   delay=GOOGLE_API_RETRY_DELAYS_SECONDS[attempt]
   try:
    hinted=int((getattr(exc,'resp',None) or {}).get('retry-after') or 0)
   except (TypeError,ValueError):hinted=0
   # Never sleep LESS than the server asked for, and never longer than the
   # remaining ladder implies -- an unbounded server hint could hang the run.
   time.sleep(min(max(delay,hinted),90))
SHEET_DRAIN_LOCK_RC=75
SHEET_DRAIN_LOCK_RETRY_DELAYS=(3,8,20,40)
def run_sheet_drain(queue_row):
 """Drain one queue row, absorbing single-writer lock contention.

 rc=75 (`DEFERRED_LOCK_BUSY`, `glp_sheet_sync_drain.STATUS_LOCK_CONTENDED`) is
 the drain saying ANOTHER WRITER HOLDS THE SHEET LOCK. The Sheet has exactly one
 writer by design, and deferring is how that invariant is protected rather than
 corrupting the Sheet -- it means "retry me", not "this failed".

 On 2026-08-25 every nonzero rc was raised alike, so a transient overlap with a
 concurrent lane parked the XenTraffic $120 CPA EchoXen offer and put a "needs
 operator or provider repair" card on the operator's desk. Nothing a human could
 do would fix a lock that had already cleared.

 The queue goes to the drain in a file, never in an argv element. A row carrying
 a long terms blob crosses Linux's 128 KB `MAX_ARG_STRLEN` on its own and the
 exec dies `[Errno 7] Argument list too long` before the drain starts, naming
 nothing about the row that caused it. See `scripts/sheet_drain_invoke.py`.

 Any other nonzero return code is still a hard failure and still raises.
 """
 completed=None
 with sheet_drain_invoke.queue_file([queue_row],prefix='reconciler-queue-') as queue_path:
  for attempt in range(len(SHEET_DRAIN_LOCK_RETRY_DELAYS)+1):
   completed=subprocess.run(['/usr/bin/python3.12',str(SHEET_SYNC_DRAIN),*sheet_drain_invoke.drain_args(queue_path),'--apply','true'],cwd=str(BUSINESS),capture_output=True,text=True,timeout=720)
   if completed.returncode!=SHEET_DRAIN_LOCK_RC:return completed
   if attempt>=len(SHEET_DRAIN_LOCK_RETRY_DELAYS):return completed
   time.sleep(SHEET_DRAIN_LOCK_RETRY_DELAYS[attempt])
 return completed
def init_state():
 STATE_DB.parent.mkdir(parents=True,exist_ok=True); c=sqlite3.connect(STATE_DB); c.row_factory=sqlite3.Row
 c.executescript('''PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,status TEXT NOT NULL,blocker_hash TEXT,receipt_path TEXT,updated_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS transitions(transition_key TEXT PRIMARY KEY,event_id TEXT NOT NULL,row_number INTEGER NOT NULL,column_index INTEGER NOT NULL,old_sha256 TEXT NOT NULL,new_sha256 TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,applied_at TEXT);'''); c.commit(); os.chmod(STATE_DB,0o600); return c
def google_service():
 c=Credentials.from_authorized_user_info(json.loads(TOKEN.read_text()));
 if c.expired and c.refresh_token:c.refresh(Request())
 return build('sheets','v4',credentials=c,cache_discovery=False)
@contextmanager
def sheet_write_lock():
 SHEET_LOCK.parent.mkdir(parents=True,exist_ok=True);handle=SHEET_LOCK.open('a+');os.chmod(SHEET_LOCK,0o600)
 try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError as exc:handle.close();raise RuntimeError('portfolio_sheet_apply_lock_busy') from exc
 try:yield
 finally:fcntl.flock(handle,fcntl.LOCK_UN);handle.close()
def gmail_service():
 c=Credentials.from_authorized_user_info(json.loads(TOKEN.read_text()));
 if c.expired and c.refresh_token:c.refresh(Request())
 return build('gmail','v1',credentials=c,cache_discovery=False)
def message_text(path):
 def clean_chunk(value):
  text=str(value or '')
  if re.search(r'<(?:html|body|table|div|p|td|a)\b',text,re.I):
   decoded=html.unescape(text)
   hrefs=[html.unescape(value) for value in re.findall(r'\bhref\s*=\s*["\']([^"\']+)',decoded,re.I)]
   text=re.sub(r'<[^>]+>',' ',decoded)+'\n'+'\n'.join(hrefs)
  return html.unescape(text)
 raw=Path(path).read_bytes(); msg=BytesParser(policy=policy.default).parsebytes(raw); chunks=[]
 if msg.is_multipart():
  for p in msg.walk():
   if p.get_content_disposition()=='attachment': continue
   if p.get_content_type()=='text/plain':
    try: chunks.append(clean_chunk(p.get_content()))
    except Exception: pass
  for p in msg.walk():
   if p.get_content_disposition()=='attachment': continue
   if p.get_content_type()=='text/html':
    try: chunks.append(clean_chunk(p.get_content()))
    except Exception: pass
 else:
  try: chunks=[clean_chunk(msg.get_content())]
  except Exception: chunks=[]
 return str(msg.get('subject') or ''),str(msg.get('from') or ''),'\n'.join(chunks)
def latest_unquoted_reply(body):
 lines=[]
 for raw in str(body or '').replace('\r\n','\n').split('\n'):
  line=raw.strip()
  if re.match(r'^-{2,}\s*(?:original message|forwarded message)\s*-{2,}$',line,re.I):break
  if re.match(r'^on .+(?:wrote:|<)$',line,re.I):break
  if re.match(r'^from:\s+.+',line,re.I) and lines:break
  if line.startswith('>'):continue
  lines.append(raw)
 return '\n'.join(lines).strip()
def maxbounty_signal(subject,body):
 subject_text=str(subject or '').strip()
 haystack=f'{subject_text}\n{body}'
 for action,patterns in MAXBOUNTY_SIGNAL_PATTERNS.items():
  # Anchored relationship expressions describe exact subjects. Evaluate them
  # against the subject, not only against subject+body, or every ^...$ rule is
  # unreachable on a real message with a body.
  if any(re.search(pattern,subject_text,re.I|re.S) or re.search(pattern,haystack,re.I|re.S)
         for pattern in patterns):return action
 return ''
def ordered_unique(values):
 return list(dict.fromkeys(str(value) for value in values if str(value).strip()))
def impact_typed_identifiers(text):
 """Extract Impact identifiers by type; never promote an arbitrary number to campaign id."""
 account_ids=ordered_unique(re.findall(
  r'(?:Media Partner account|reference to your (?:Media Partner )?account|Account ID)\b.{0,120}?\((\d{4,10})\)',
  text,re.I|re.S))
 advertiser_ids=[];campaign_ids=[];advertiser_name='';campaign_name=''
 paired=re.search(
  r'([A-Za-z0-9][A-Za-z0-9 .&|+\-]{1,80}?)\s*\((\d{4,10})\)\s+'
  r'([A-Za-z0-9][A-Za-z0-9 .&|+\-]{1,100}?)\s*\((\d{3,10})\)\s+'
  r'(?:Sorry\b|Unfortunately\b|Promotion Request\b)',text,re.I)
 if not paired:
  # Impact welcome mail prints the advertiser identity and campaign identity as
  # two adjacent typed rows before the welcome heading. Ad IDs elsewhere in the
  # body are not campaign IDs and must remain excluded.
  paired=re.search(
   r'([A-Za-z0-9][A-Za-z0-9 .&|+\-]{1,80}?)\s*\((\d{4,10})\)\s+'
   r'([A-Za-z0-9][A-Za-z0-9 .&|+\-]{1,100}?)\s*\((\d{3,10})\)\s+'
   r'Welcome to .{1,100}? Affiliate Program',text,re.I|re.S)
 if paired:
  advertiser_name=paired.group(1).strip();advertiser_ids.append(paired.group(2))
  campaign_name=paired.group(3).strip();campaign_ids.append(paired.group(4))
 advertiser_ids.extend(re.findall(r'\bAdvertiser\s+ID\s*[:#]\s*(\d{3,10})\b',text,re.I))
 campaign_ids.extend(re.findall(r'\bCampaign\s+ID\s*[:#]\s*(\d{3,10})\b',text,re.I))
 if not campaign_name:
  named=re.search(
   r'(?:work with the campaign,|request to partner on)\s+(.{1,100}?)'
   r'(?=\s+for the following reason|[.!](?:\s|$))',text,re.I|re.S)
  if named:campaign_name=' '.join(named.group(1).split())
 account=set(account_ids)
 advertiser_ids=[value for value in ordered_unique(advertiser_ids) if value not in account]
 advertiser=set(advertiser_ids)
 campaign_ids=[value for value in ordered_unique(campaign_ids)
               if value not in account and value not in advertiser]
 return {'partner_account_ids':account_ids,'advertiser_ids':advertiser_ids,
         'campaign_ids':campaign_ids,'advertiser_name':advertiser_name,
         'campaign_name':campaign_name}
def detect(subject,sender,body):
 text='\n'.join((subject,sender,body)); low=text.lower()
 sender_email=parseaddr(sender)[1].strip().lower(); sender_domain=sender_email.rsplit('@',1)[-1].rstrip('.') if '@' in sender_email else ''
 if sender_email=='affiliates@maxbounty.com' and str(subject).strip().lower()=='campaign cancellation notice':
  plain=html.unescape(re.sub(r'<[^>]+>',' ',body))
  name=re.search(r'The advertiser for campaign\s+(.+?)\s*(?:\(\s*https?://|\[\s*https?://|requires that it expire)',plain,re.I|re.S)
  expiry=re.search(r'requires that it expire on\s+(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})',plain,re.I)
  if not name or not expiry:raise RuntimeError('campaign_cancellation_identity_or_date_missing')
  return {'network':'MaxBounty','action':'campaign_cancellation','program_name':' '.join(name.group(1).split()),'effective_at_network_time':expiry.group(1),'text':text}
 newest=latest_unquoted_reply(body); newest_low=newest.lower()
 terms_notice=re.fullmatch(r'Your Contract Terms for (.+?) Have Changed\s*[^\w\s]*',str(subject).strip(),re.I)
 if network_for_domain(sender_domain)==('katalys.com','Katalys') and terms_notice:
  return {'network':'Katalys','action':'contract_terms_review','program_name':terms_notice.group(1).strip(),
          'advertiser_name':terms_notice.group(1).strip(),'text':text,
          'detail':'contract_terms_changed_requires_verified_capture'}
 coreage_offer=(
  network_for_domain(sender_domain)==('katalys.com','Katalys')
  and bool(re.search(r'\bCoreAge\s+Rx\b',subject,re.I))
  and bool(re.search(r'\$\s*230\s*CPA\b',text,re.I))
 )
 if coreage_offer:
  return {'network':'Katalys','action':'payout_offer','advertiser_name':'CoreAge Rx GLP-1 [1632]',
          'program_name':'CoreAge Rx GLP-1 [1632]','program_ids':[1632],
          'offer_terms':'$230 CPA on GLP-1 SKUs; $100 default payout',
          'offer_url':'https://app.katalys.com/pub/098e5455-ca72-4099-9ff9-a9dd014148e7/explore/1632',
          'text':text,'detail':'authenticated_katalys_offer_notice'}
 echoxen_offer=(
  (sender_domain=='via-everflow.io' or sender_domain.endswith('.via-everflow.io'))
  and bool(re.search(r'\bEchoXen\b',subject,re.I))
  and bool(re.search(r'\$\s*120\s*CPA\b',text,re.I))
 )
 if echoxen_offer:
  offer_id=(re.search(r'xentraffic\.everflowclient\.io/offers/(\d+)',text,re.I) or
            re.search(r'\boffer\s*(?:id)?\s*[:#-]?\s*(\d{2,10})\b',text,re.I))
  return {'network':'XenTraffic','action':'payout_offer','advertiser_name':'EchoXen',
          'program_name':'XenTraffic','program_ids':[int(offer_id.group(1))] if offer_id else [],
          'offer_terms':'Up to $120 CPA','offer_url':f"https://xentraffic.everflowclient.io/offers/{offer_id.group(1)}" if offer_id else 'https://xentraffic.everflowclient.io/',
          'text':text,'detail':'authenticated_xentraffic_offer_notice'}
 maxbounty_action=maxbounty_signal(subject,newest) if network_for_domain(sender_domain)==('maxbounty.com','MaxBounty') else ''
 direct_routine_name=next((program for domain,program in DIRECT_ROUTINE_OUTCOME_DOMAINS.items()
                           if sender_domain==domain or sender_domain.endswith('.'+domain)),None)
 direct_adverse=bool(re.search(r'\b(?:(?:decided|decision is) not to move forward|(?:will|would) not be moving forward|(?:unable|not able) to move forward|not moving forward|declined|rejected)\b',newest_low,re.I))
 if direct_routine_name and direct_adverse:
  return {'network':direct_routine_name,'action':'routine_information',
          'advertiser_name':direct_routine_name,
          'text':'\n'.join((subject,sender,newest)),
          'detail':'direct_partner_adverse_notice_latest_reply_no_provider_claim'}
 welcome_name=re.search(r"^\s*Welcome to (?:the )?(.+?) Affiliate Program(?:\s*[^\w\s,]{1,12}|,\s*[A-Za-z][A-Za-z .'-]{0,80})?(?:\s*[–—-].*)?\s*$",subject,re.I)
 everflow_approval=re.search(r"^\s*Welcome to Partner Program!\s*(.+?)\s+has approved your application[.!]?\s*$",subject,re.I)
 ziphealth_followup='heliosx.com' in low and 'ziphealth' in low and 'application' in low and any(x in low for x in ('traffic','audience','promote'))
 katalys_explicit_status=bool(re.search(r'\b(?:approval status|application|request|relationship)\b.{0,80}\b(?:approved|accepted|rejected|declined|suspended|terminated)\b',low,re.I|re.S))
 katalys_program_feature=('katalys.com' in low and 'program feature' in low and re.search(r'\bpromote\b',subject,re.I) and not katalys_explicit_status)
 katalys_app_update=re.search(r'update to your application for\s+(.{1,80}?)\s*$',subject.strip(),re.I)
 katalys_status_m=re.search(r'approval status:\s*(approved|accepted|declined|rejected|pending|under review|in review)',low)
 partnerize_join=re.search(r'request to join\s+(.{1,80}?)\s+has been\s+(accepted|rejected)',low)
 partnerize_removed=re.search(r'has removed you from\s+([^.\n]{1,80}?)\s*(?:\.|view\b|$)',low)
 awin_promo=bool(re.search(r'\(\d{3,8}\)\s*:\s*promote\b',subject,re.I))
 # A network support case CONFIRMING login/2SV access was restored/relaxed is an
 # access RESOLUTION, never a tax/KYC prerequisite (mislabel 2026-08-07, Awin
 # case 03120535: "two-step verification disabled" filed as payout action).
 _ACCESS_FACTOR=r'(?:two.?step|two.?factor|2fa|2sv)'
 access_resolved=(bool(re.search(r'\b(?:disabled|removed|switched off|deactivated|turned off)\b[^.]{0,120}\b'+_ACCESS_FACTOR+r'\b',low,re.S))
                  or bool(re.search(r'\b'+_ACCESS_FACTOR+r'\b.{0,240}\b(?:disabled|removed|switched off|deactivated|turned off|no longer required)\b',low,re.S))
                  or bool(re.search(r'\b(?:log ?in|sign ?in)\b.{0,120}\b(?:username and password|password only)\b.{0,160}\bwithout\b.{0,80}\b(?:code|authenticator|verification)\b',low,re.S)))
 access_case=re.search(r'\bcase\s*[:#]?\s*(\d{5,12})\b',low)
 refersion_welcome=('refersion' in low and 'welcome to refersion marketplace' in low)
 thorne_adverse=('thorne.com' in low and bool(re.search(r'unable to accept your application|update on (?:your )?brand collaboration application',low)))
 google_manager_link_accepted=(
  sender_domain in {'google.com','googleadservices.com'}
  and bool(re.search(r'\byour manager account\b.*\blink(?:ing)? invitation was accepted\b',subject,re.I|re.S))
 )
 direct_partner_signal=(
  any(sender_domain==domain or sender_domain.endswith('.'+domain) for domain in DIRECT_PARTNER_DOMAINS)
  and any(x in low for x in ('affiliate','publisher'))
  and re.search(r'\b(?:tracking\s+(?:url|link)|manual\s+attribution|how\s+did\s+you\s+hear|commission)\b', low, re.I)
 )
 amazon_sender=network_for_domain(sender_domain)==('amazon.com','Amazon Associates')
 amazon_rate_notice=(amazon_sender and any(label in low for label in ('amazon associates','amazon influencer & associates program')) and any(signal in low for signal in ('commission rate','commission rates','standard commission income rate','increased rates for amazon')))
 registry_sender=network_for_domain(sender_domain)
 impact_sender=sender_domain=='impact.com' or sender_domain.endswith('.impact.com') or sender_domain=='impactradius.com' or sender_domain.endswith('.impactradius.com')
 # Exact pending/support grammars must complete before the broad relationship
 # fallback. Historical replies and onboarding mail often quote words such as
 # "approved" while the newest message says pending, declined, or merely asks
 # for a follow-up. Treating those quotes as live approvals created a permanent
 # adapter backlog and hid the actual approval notices.
 if registry_sender==('flexoffers.com','FlexOffers') and (
    subject.strip().lower()=='flexoffers.com registration, final steps'
    and 'registrations are subject to review' in low):
  return {'network':'FlexOffers','action':'routine_information','text':text,
          'detail':'flexoffers_registration_pending_then_superseded'}
 if (registry_sender==('flexoffers.com','FlexOffers')
     and subject.strip().lower() in {
      'important update: your traffic source has been declined',
      'important update: your application has been declined',
     }):
  return {'network':'FlexOffers','action':'routine_information','text':text,
          'detail':'flexoffers_network_account_decline_recorded'}
 if registry_sender==('refersion.com','Refersion') and (
    re.search(r'\baffiliate account is pending\b',subject,re.I)
    or re.search(r'\b(?:account in review|application is being reviewed)\b',newest,re.I)):
  return {'network':'Refersion','action':'routine_information','text':text,
          'detail':'refersion_application_pending'}
 if (registry_sender==('refersion.com','Refersion') and subject.lower().startswith('re:')
     and not re.search(r'\b(?:approved|accepted|declined|rejected|suspended|terminated|tax|payout|identity)\b',newest,re.I)):
  return {'network':'Refersion','action':'routine_information','text':text,
          'detail':'refersion_support_reply_no_relationship_change'}
 if (registry_sender==('everflow.io','Everflow')
     and subject.strip().lower()=='re: review pending for your everflow marketplace application!'
     and re.search(r'\b(?:references?|schedule a call|schedule a meeting|meet and go over)\b',newest,re.I)):
  return {'network':'Everflow','action':'manual_response_required','text':text,
          'advertiser_name':'Everflow Affiliate Marketplace',
          'detail':'everflow_marketplace_followup_required'}
 if (sender_domain=='pepperjamnetwork.com' and subject.strip().lower()=='welcome to ascend'
     and re.search(r'\bapplication received\b',body,re.I)):
  return {'network':'Partnerize','action':'routine_information','text':text,
          'advertiser_name':'Ascend by Partnerize','detail':'ascend_application_received'}
 if (sender_domain=='trackdesk.com'
     and re.search(r'^\s*(?:[^\w\s]{1,4}\s*)?Welcome to the NuImage Medical Affiliate Program\b',subject,re.I)
     and re.search(r"\b(?:you(?:'|’)re|you are) approved\b",subject,re.I)):
  return {'network':'Trackdesk','action':'relationship_approval','text':text,
          'advertiser_name':'NuImage Medical','program_name':'NuImage Medical'}
 if (sender_domain=='buygoods.com' and subject.strip().lower()=='welcome to buygoods'
     and 'application will then be automatically sent to our approval team' in low):
  return {'network':'BuyGoods','action':'account_activation_required','text':text,
          'advertiser_name':'BuyGoods','program_name':'BuyGoods',
          'detail':'email_confirmation_would_submit_application'}
 if impact_sender:
  typed=impact_typed_identifiers(text)
  account_decline=(not typed['campaign_ids'] and bool(re.search(
   r'application to join Impact as (?:a )?(?:media )?partner\b.{0,180}\bdeclined\b',
   text,re.I|re.S)))
  if account_decline:
   return {'network':'Impact.com','action':'routine_information','text':text,
           'detail':'impact_media_partner_account_application_declined',
           'partner_account_ids':[int(value) for value in typed['partner_account_ids']],
           'advertiser_ids':[],'campaign_ids':[],'program_ids':[]}
 if registry_sender and registry_sender[1]=='Partnerize' and (
    re.search(r'\bWelcome to Partnerize\b',text,re.I)
    and any(label in low for label in ('applying to campaigns','account details','get started today'))):
  return {'network':'Partnerize','action':'routine_information','text':text,
          'detail':'partnerize_network_account_onboarding'}
 if registry_sender and registry_sender[1]=='CJ Affiliate' and (
    re.search(r'\bWelcome to (?:the )?CJ Network\b',subject,re.I)
    or re.search(r'\bActivating Your CJ Publisher Account\b',body,re.I)):
  return {'network':'CJ Affiliate','action':'routine_information','text':text,
          'detail':'cj_publisher_account_onboarding'}
 # Katalys marketplace/newsletter notices repeatedly contain generic words such
 # as "approved" while advertising programmes that are merely available to
 # apply to. Programme activation is also an advertiser-side publication event,
 # not a change to our publisher relationship. Only dedicated application
 # status grammars may promote these messages to relationship work.
 katalys_marketing_notice=bool(
  registry_sender==('katalys.com','Katalys')
  and not katalys_app_update
  and not katalys_status_m
  and (re.search(r'\bapply to join\b',text,re.I)
       or re.search(r'\b(?:program|programme) updates?\b',subject,re.I)
       or re.search(r'^\s*Introducing\b',subject,re.I)
       or re.search(r'\bhas Activated Their Program\b',subject,re.I))
 )
 if katalys_marketing_notice:
  return {'network':'Katalys','action':'routine_information','text':text,
          'detail':'katalys_marketing_or_program_activation_notice'}
 # MaxBounty relationship mail has a narrow exact grammar above. Marketing and
 # training mail routinely contains generic words such as "approved", which
 # must not fall through to the broad affiliate relationship classifier and
 # re-open an already-approved network account for legal review.
 if registry_sender==('maxbounty.com','MaxBounty') and not maxbounty_action:
  return {'network':'MaxBounty','action':'routine_information','text':text,
          'detail':'maxbounty_notice_no_exact_relationship_signal'}
 if (not any(s in low for s in SIGNALS) and not welcome_name and not maxbounty_action and not ziphealth_followup and not katalys_program_feature
     and not amazon_rate_notice and not direct_partner_signal and not google_manager_link_accepted
     and not access_resolved
     and not (katalys_app_update or partnerize_removed or awin_promo or refersion_welcome or thorne_adverse)):
  # A registered network's mail with no recognized relationship signal is still
  # OURS to consume: record it as routine information and clear the mailbox
  # instead of leaving it unread forever (operator report 2026-08-06).
  if registry_sender:
   return {'network':registry_sender[1],'action':'routine_information','text':text,
           'detail':'network_notice_no_relationship_signal'}
  return None
 migration='moved over to impact' in low or 'closing our awin program' in low
 registry_network=(network_for_text(low) or registry_sender or (None,None))[1]
 network=('Google Ads' if google_manager_link_accepted else
          'Thorne' if thorne_adverse else
          'Awin' if ziphealth_followup else
          'Amazon Associates' if amazon_rate_notice else
          'GoAffPro' if 'goaffpro.com' in low else
          'BrandChamp' if 'brandchamp.io' in low or ('legion' in low and 'legionsupplements.com' in low) else
          'Impact.com' if migration or 'impact.com' in low else
          next((program for domain,program in DIRECT_PARTNER_DOMAINS.items() if sender_domain==domain or sender_domain.endswith('.'+domain)),None) if direct_partner_signal else
          registry_network)
 if not network:return None
 if network=='Awin':
  all_awin_ids={x for x in re.findall(r'(?:advertiser\s*(?:id)?\s*[:#|-]?\s*|advertiserIds?[=:/]|awinmid=|merchant-profile/|provider_program_id=|\baid\s*[:=]\s*)(\d{3,8})',text,re.I)}
  advertiser_ids={x for x in all_awin_ids if x!=PUBLISHER_AWIN}
  if not welcome_name and not advertiser_ids and PUBLISHER_AWIN in text and any(token in low for token in ('account is now active','publisher account','publisher id')):
   return {'network':'Awin','action':'routine_information','status':'information','advertiser_ids':[],'evidence':[]}
 awin_generic_invitation=(network=='Awin' and bool(re.search(r'\binvitation\b.*\bjoin our affiliate program\b',subject,re.I)))
 brandchamp_rejection=(network=='BrandChamp' and bool(re.search(r'\b(?:not been accepted|not accepted|declined|rejected)\b',newest,re.I)))
 action=('routine_information' if google_manager_link_accepted else
         'account_access_resolved' if access_resolved else
         'direct_manual_attribution' if network in DIRECT_PARTNER_NETWORKS else
         'relationship_migration' if migration else
         'manual_response_required' if ziphealth_followup or (any(x in low for x in ("hasn't been approved yet",'has not been approved yet','need more information','additional information required')) and any(x in low for x in ('reply','traffic','audience','promote'))) else
         'routine_information' if network in {'HighLevel Affiliate','PartnerStack','Amazon Associates'} or katalys_program_feature or awin_promo or refersion_welcome or thorne_adverse else
         'routine_information' if katalys_app_update and katalys_status_m and katalys_status_m.group(1) in ('pending','under review','in review') else
         'relationship_adverse' if brandchamp_rejection or partnerize_removed or any(x in low for x in ('suspended','rejected','terminated','declined','program closed','programme closed')) else
         'relationship_information' if 'getting you started with' in low else
         'relationship_approval' if welcome_name else
         'commission_change' if 'commission rate' in low or 'commission changed' in low or 'terms changed' in low else
         'relationship_invitation' if 'invited you to join' in low or awin_generic_invitation else
         'relationship_approval')
 result={'network':network,'action':action,'text':text}
 if maxbounty_action:
  campaign_match=re.search(
   r'\bYour request for access to\s+(.+?)\s+\((\d{2,10})\)\s*(?:\[[^\n]*\]\s*)?has been\s+(?:approved|accepted|rejected|declined)\b',
   newest,re.I|re.S)
  if campaign_match:
   campaign_name=' '.join(campaign_match.group(1).split())
   advertiser_name=re.split(r'\s+-\s+',campaign_name,maxsplit=1)[0].strip()
   result.update({'network':'MaxBounty','action':maxbounty_action,
                  'advertiser_name':advertiser_name,'program_name':campaign_name,
                  'program_ids':[int(campaign_match.group(2))],
                  'approval_scope':'campaign',
                  'publisher_login_url':'https://affiliates.maxbounty.com/'})
  else:
   result.update({'network':'MaxBounty','action':maxbounty_action,'advertiser_name':'MaxBounty',
                  'program_name':'MaxBounty','program_ids':['network-account'],
                  'approval_scope':'network_account',
                  'publisher_login_url':'https://affiliates.maxbounty.com/'})
  publisher_match=re.search(r'\b(?:affiliate|publisher)\s*(?:id|#)\s*[:#-]?\s*(\d{3,12})\b',text,re.I)
  if publisher_match:result['publisher_id']=publisher_match.group(1)
 if access_resolved and access_case:result['case_id']=access_case.group(1)
 if welcome_name:
  result['advertiser_name']=re.sub(r'\s+AWIN\s*$','',welcome_name.group(1).strip(),flags=re.I)
 if katalys_app_update and not result.get('advertiser_name'):
  update_name=re.sub(r'[\W_]+$','',katalys_app_update.group(1)).strip()
  body_adv=re.search(r'advertiser:\s*([^\n]{1,80}?)\s*(?:program:|$)',low)
  result['advertiser_name']=(body_adv.group(1).strip().title() if body_adv else update_name)
  result['program_name']=update_name
 if partnerize_join and not result.get('advertiser_name'):result['advertiser_name']=partnerize_join.group(1).strip()
 if partnerize_removed and not result.get('advertiser_name'):result['advertiser_name']=partnerize_removed.group(1).strip()
 if network=='GoAffPro' and 'ihealth' in low:result['advertiser_name']='iHealth Labs'
 if network=='GoAffPro' and 'renpho' in low:
  result['advertiser_name']='RENPHO';result['program_name']='RENPHO Creator Program'
 if network=='BrandChamp' and 'legion' in low:result['advertiser_name']='Legion Athletics'
 if network=='CJ Affiliate' and not result.get('advertiser_name'):
  partner_welcome=re.search(r'^\s*Welcome To (?:the )?(.+?) Partner Program\s*$',subject,re.I)
  if partner_welcome:result['advertiser_name']=partner_welcome.group(1).strip()
 if network in DIRECT_PARTNER_NETWORKS:result['advertiser_name']=DIRECT_PARTNER_PROGRAMS[network]['program']
 if network=='Awin':
  if advertiser_ids:result['program_ids']=sorted(int(x) for x in advertiser_ids)
  # A typed subject supplies the missing name axis on legacy rows without ID
  # notes. It is only an identity hint: authenticated API name/ID readback still
  # gates the transition. Do not infer approval from this subject grammar.
  typed_subject=re.match(r'^\s*([^\n]{1,160}?)\s+\((\d{3,8})\)\s*:',subject)
  if (typed_subject and not result.get('advertiser_name')
      and str(typed_subject.group(2))!=PUBLISHER_AWIN
      and {int(value) for value in result.get('program_ids') or []}=={int(typed_subject.group(2))}):
   result['advertiser_name']=html.unescape(typed_subject.group(1)).strip()
  invited_name=re.search(r'^\s*(.+?)\s+has invited you to join their program\s*$',subject,re.I)
  if invited_name:result['advertiser_name']=invited_name.group(1).strip()
  if not result.get('advertiser_name'):
   welcome_subject=re.search(r'^\s*Welcome to\s+(.+?)(?:\s+Affiliate Program)?[!?.]?\s*$',subject,re.I)
   if welcome_subject:result['advertiser_name']=html.unescape(welcome_subject.group(1)).strip()
  if not result.get('advertiser_name'):
   membership_subject=re.search(r'^\s*Membership active with advertiser\s+(.+?)(?:\s+\(aid:\s*\d+\))?\s*$',subject,re.I)
   if membership_subject:result['advertiser_name']=html.unescape(membership_subject.group(1)).strip()
   if not result.get('advertiser_name'):
    application_notice=re.search(
     r'Your application to join\s+(.+?)\s+\(aid:\s*(\d+)\)\s+program\b',
     text,re.I|re.S)
    if application_notice:
     result['advertiser_name']=' '.join(html.unescape(application_notice.group(1)).split())
     notice_id=int(application_notice.group(2))
     result['program_ids']=sorted(set(result.get('program_ids') or [])|{notice_id})

 if network=='Katalys':
  compact=' '.join(body.split())
  match=re.search(r'Advertiser:\s*(.*?)\s+Program:\s*(.*?)(?=\s+Reason:|\s+Head over)\s*(?:Reason:.*?)?\s+Head over\b',compact,re.I)
  if match:result.update({'advertiser_name':match.group(1).strip(),'program_name':match.group(2).strip()})
 if network=='Partnerize':
  match=(re.search(r'([A-Za-z0-9][A-Za-z0-9 .&_|()\[\]-]{1,140}?)\s+has accepted your campaign request',body,re.I)
          or re.search(r'Your request to join\s+(.+?)\s+has been accepted',body,re.I|re.S)
          or re.search(r'Your request to join\s+(.+?)\s+has been rejected\.',body,re.I|re.S)
          or re.search(r'^\s*(.+?)\s+has invited you to join their program\s*$',subject,re.I)
          or re.search(r'^\s*(.+?)\s+has invited you to join their program\s*$',body,re.I|re.M)
          or re.search(r'^\s*(.+?):\s*Approved onto the .+? Affiliate Network[.!]?\s*$',subject,re.I))
  if match:result['advertiser_name']=match.group(1).strip()
 if network=='Impact.com':
  typed=impact_typed_identifiers(text)
  result['partner_account_ids']=[int(value) for value in typed['partner_account_ids']]
  result['advertiser_ids']=[int(value) for value in typed['advertiser_ids']]
  result['campaign_ids']=[int(value) for value in typed['campaign_ids']]
  result['program_ids']=list(result['campaign_ids'])
  if typed['advertiser_name']:result['advertiser_name']=typed['advertiser_name']
  if typed['campaign_name']:result['program_name']=typed['campaign_name']
 if network=='Affiliati':
  ids={int(value) for value in re.findall(
   r'(?:Affiliati\s+(?:offer|programme|program)\s*|offer\s*(?:id)?\s*[:#-]?\s*)(\d{3,8})',
   text,re.I)}
  if ids:result['program_ids']=sorted(ids)
 if network=='HealthTrader' and any(x in low for x in ('account has been approved','application approved','application has been approved')):
  result.update({'action':'relationship_approval','advertiser_name':'HealthTrader','program_name':'HealthTrader','program_ids':['network-account']})
 if network=='Everflow':
  if everflow_approval:result['advertiser_name']=everflow_approval.group(1).strip()
  result['account_activation_required']=bool(re.search(r'https?://[^\s<>"\']*everflowclient\.io/auth/activation(?:[?/#\s]|$)',text,re.I))
 return result
def program_id(row,network):
 text=f'{row[5]} {row[23]} {row[14]}'
 pats=((r'(?:Awin programme\s*|advertiserIds?[=:/]|awinmid=|merchant-profile/|provider_program_id=)(\d+)',) if network=='Awin' else
       (r'(?:Katalys offer\s*|/explore/|/programs/)(\d+)',) if network=='Katalys' else
       (r'(?:CJ Affiliate programme\s*|advertiserId[=:/])(\d+)',) if network=='CJ Affiliate' else
       (r'(?:Impact campaign\s*|/Campaigns?/)(\d+)',) if network=='Impact.com' else
       (r'(?:Partnerize programme\s*|/campaign/)([0-9]+l[0-9]+)',) if network=='Partnerize' else
       (r'HealthTrader programme\s*(network-account|[A-Za-z0-9_-]+)',) if network=='HealthTrader' else
       (r'MaxBounty programme\s*(network-account|[A-Za-z0-9_-]+)',) if network=='MaxBounty' else
       (r'(?:Affiliati\s+(?:offer|programme|program)\s*|offer\s*(?:id)?\s*[:#-]?\s*)(\d+)',) if network=='Affiliati' else ())
 for pat in pats:
  m=re.search(pat,text,re.I)
  if m:return int(m.group(1)) if network in {'Awin','Katalys','CJ Affiliate','Impact.com','Affiliati'} else m.group(1)
 return None
canonical_network_label=canonical_network
def bind_row(values,candidate):
 network=candidate['network'];ids={str(value) for value in candidate.get('program_ids') or [] if str(value).strip()}
 if network=='Awin':
  for pat in (r'merchant-profile/(\d+)',r'advertiserIds?=(\d+)',r'awinmid=(\d+)',r'\baid\s*[:=]\s*(\d+)',r'(?:programme|program|advertiser)\s*(?:id)?\s*[:#|-]?\s*(\d{3,7})'):
   ids.update(re.findall(pat,candidate['text'],re.I))
  ids.discard(PUBLISHER_AWIN)
 explicit=candidate.get('program_name') or candidate.get('advertiser_name') or ''
 indexed={}
 matched_ids={}
 # Exact provider identity is authoritative and is always scoped by the writer
 # to the candidate's canonical marketplace. Only if no id axis exists do we
 # consult the shared conservative name fallback.
 for candidate_id in sorted(ids):
  for match in sheet_sync_existing_matches(values,{'network':network,'provider_program_id':candidate_id,'program':''}):
   indexed.setdefault(match['row'],match);matched_ids.setdefault(match['row'],set()).add(candidate_id)
 if not indexed and explicit:
  for match in sheet_sync_existing_matches(values,{'network':network,'provider_program_id':'','program':explicit}):
   indexed.setdefault(match['row'],match)
 # Authenticated provider display names retain a broader legacy equivalence
 # (country decorations and reordered multi-brand tokens). The definition is
 # shared from the writer module; requiring one result preserves fail-closed
 # ambiguity while keeping established welcome-email behavior.
 if not indexed and explicit:
  for n,raw in enumerate(values[1:],2):
   row=pad(raw)
   if (canonical_network_label(row[4])==canonical_network_label(network)
       and equivalent_advertiser_name(row[2],explicit)):
    indexed[n]={'row':n,'program':str(row[2]),'axes':['provider_display_name']}
 if not indexed and not explicit:
  hay=norm(candidate['text'])
  for n,raw in enumerate(values[1:],2):
   row=pad(raw)
   if canonical_network_label(row[4])==canonical_network_label(network) and len(norm(row[2]))>=5 and norm(row[2]) in hay:
    indexed[n]={'row':n,'program':str(row[2]),'axes':['message_text_program_name']}
 matches=sorted(indexed.values(),key=lambda item:item['row'])
 if not matches:raise RuntimeError(f"sheet_identity_not_found:{network}:matches=0")
 if len(matches)>1:raise RuntimeError(f"ambiguous_sheet_identity:{network}:matches={len(matches)}")
 n=matches[0]['row'];row=pad(values[n-1]);pid=program_id(row,network)
 if pid is None and matched_ids.get(n):
  if len(matched_ids[n])!=1:raise RuntimeError(f"ambiguous_sheet_identity:{network}:matches={len(matched_ids[n])}")
  value=next(iter(matched_ids[n]));pid=int(value) if value.isdigit() and network in {'Awin','Katalys','CJ Affiliate','Impact.com','Affiliati'} else value
 # Repair legacy rows that predate provider-id notes only under an exact safety
 # tuple: one provider-scoped row, one notice id, and a unique provider-name
 # match. The provider snapshot must still verify the id/name pair before any
 # disposition or write, so this cannot bypass per-network identity isolation.
 if pid is None and len(ids)==1 and explicit and len(matches)==1:
  candidate_id=next(iter(ids));axes=set(matches[0].get('axes') or [])
  if 'provider_display_name' in axes or equivalent_advertiser_name(row[2],explicit):
   pid=int(candidate_id) if candidate_id.isdigit() and network in {'Awin','Katalys','CJ Affiliate','Impact.com','Affiliati'} else candidate_id
 if network=='Impact.com':
  ids=candidate.get('program_ids') or ([pid] if pid else [])
  if not ids:raise RuntimeError('provider_program_id_missing:Impact.com')
  return n,row,ids
 if not pid:raise RuntimeError(f'provider_program_id_missing:{network}')
 return n,row,pid

def load_operator_handoff(path):
 p=Path(path)
 if not p.is_file():raise RuntimeError('operator_handoff_missing')
 if p.stat().st_mode & 0o077:raise RuntimeError('operator_handoff_permissions_not_private')
 payload=json.loads(p.read_text())
 if payload.get('schema')!='affiliate_external_disposition_handoff_v1' or payload.get('status')!='READY':raise RuntimeError('operator_handoff_schema_or_status_invalid')
 operator=payload.get('operator') or {}
 if operator.get('actor_id')!='392067741500309517' or operator.get('channel_id')!='1524254375453786294' or not str(operator.get('message_id') or '').isdigit() or operator.get('message_edited') is not False:raise RuntimeError('operator_handoff_authority_invalid')
 subset=payload.get('affiliate_subset') or []
 if len(subset)!=len(set(subset)) or not subset:raise RuntimeError('operator_handoff_affiliate_subset_invalid')
 payload['_path']=str(p);payload['_sha256']=hashlib.sha256(p.read_bytes()).hexdigest();return payload

def operator_handoff_consumption_path(handoff):
 return Path(handoff['_path']).parent/'affiliate-reconciler-consumption.json'

def pending_operator_handoffs():
 root=REPORT_DIR/'handoffs';pending=[]
 if not root.is_dir():return pending
 for path in sorted(root.glob('*/handoff.json')):
  handoff=load_operator_handoff(path);receipt_path=operator_handoff_consumption_path(handoff)
  if receipt_path.is_file():
   receipt=json.loads(receipt_path.read_text())
   if receipt.get('schema')!='affiliate_external_handoff_consumption_v1' or receipt.get('status')!='PASS' or receipt.get('handoff_sha256')!=handoff['_sha256'] or receipt.get('event_ids')!=handoff['affiliate_subset']:raise RuntimeError('operator_handoff_consumption_receipt_invalid')
   continue
  pending.append(handoff)
 return pending

def finalize_operator_handoff(state,handoff):
 event_ids=list(handoff['affiliate_subset']);rows=[]
 for event_id in event_ids:
  row=state.execute('SELECT status,receipt_path FROM events WHERE event_id=?',(event_id,)).fetchone()
  if not row or row['status'] not in {'pass','pass_operator_evidence'} or not row['receipt_path']:raise RuntimeError(f'operator_handoff_event_not_consumed:{event_id}')
  path=Path(row['receipt_path'])
  if not path.is_file():raise RuntimeError(f'operator_handoff_event_receipt_missing:{event_id}')
  receipt=json.loads(path.read_text())
  if receipt.get('status')!='PASS' or receipt.get('event_id')!=event_id:raise RuntimeError(f'operator_handoff_event_receipt_invalid:{event_id}')
  rows.append({'event_id':event_id,'state_status':row['status'],'receipt_path':str(path),'receipt_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'sheet_write_count':int(receipt.get('sheet_write_count') or 0),'provider_readback_verified':bool(receipt.get('provider_readback_verified'))})
 payload={'schema':'affiliate_external_handoff_consumption_v1','status':'PASS','completed_at_utc':now(),'handoff_path':handoff['_path'],'handoff_sha256':handoff['_sha256'],'operator_message_id':str(handoff['operator']['message_id']),'event_ids':event_ids,'events':rows,'sheet_write_count':sum(x['sheet_write_count'] for x in rows),'external_send':False,'outbound_send':False,'provider_program_mutation':False,'affiliate_redirect_clicks':0,'secret_printed':False}
 path=operator_handoff_consumption_path(handoff);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n');os.chmod(tmp,0o600);os.replace(tmp,path);return path

def validate_operator_handoff_event(handoff,event):
 event_id=str(event['event_id'])
 if event_id not in set(handoff['affiliate_subset']):raise RuntimeError('operator_handoff_event_not_authorized')
 records=[item for item in handoff.get('events',[]) if item.get('event_id')==event_id]
 if len(records)!=1:raise RuntimeError('operator_handoff_event_record_invalid')
 record=records[0];raw=Path(event['raw_path'])
 if not raw.is_file() or hashlib.sha256(raw.read_bytes()).hexdigest()!=record.get('raw_sha256') or str(raw)!=record.get('raw_path'):raise RuntimeError('operator_handoff_raw_binding_invalid')
 db=sqlite3.connect(f'file:{INBOX_DB}?mode=ro',uri=True);db.execute('PRAGMA query_only=ON')
 try:
  live=db.execute('SELECT state,raw_sha256 FROM inbox_events WHERE event_id=?',(event_id,)).fetchone()
  # Exactly ONE operator decision -- counted by CONTENT, not by row.  The control
  # plane spelled decision_message_id two ways for one reply (bare vs suffixed with
  # the event_id, depending on how many commands a sync pass parsed), so a single
  # decision could land as two rows and fail this check.  Distinctness is (actor,
  # card revision): the same actor answering the same card revision is one decision
  # however many times it was written, while a different actor or a different card
  # revision is a genuine conflict and still raises.
  count=len(db.execute("SELECT DISTINCT actor_id,card_revision_sha256 FROM operator_decisions WHERE event_id=? AND UPPER(action)='RESOLVED_EXTERNALLY'",(event_id,)).fetchall())
 finally:db.close()
 if not live or live[0]!='resolved_externally' or live[1]!=record.get('raw_sha256') or count!=1:raise RuntimeError('operator_handoff_terminal_disposition_invalid')
 return str(handoff['operator']['message_id'])

def bind_direct_operator_row(values,candidate):
 spec=DIRECT_OPERATOR_PROGRAMS[candidate['network']];matches=[]
 for number,raw in enumerate(values[1:],2):
  row=pad(raw)
  if norm(row[2])==norm(spec['program']) and row[4] in spec['routes']:matches.append((number,row))
 if len(matches)!=1:raise RuntimeError(f"ambiguous_direct_operator_sheet_identity:{candidate['network']}:matches={len(matches)}")
 return matches[0]

def direct_operator_changes(row,candidate):
 network=candidate['network']
 if network=='GoAffPro':
  desired={10:'approval_email_received_provider_readback_required',16:'Public GoAffPro programme schedule: up to 10% on all products; exclusions and later changes may apply.',21:'provider_readback_required_no_referral_link_generated_or_tested'}
 elif network=='BrandChamp':
  desired={10:'approval_email_received_tax_payment_setup_required',21:'provider_readback_required_w9_and_paypal_setup_pending_no_link_test'}
 else:raise RuntimeError('direct_operator_network_unsupported')
 changes={column:value for column,value in desired.items() if str(row[column])!=str(value)}
 if changes:changes[24]=now()
 if not set(changes).issubset(OPERATOR_EVIDENCE_COLUMNS):raise RuntimeError('direct_operator_column_scope_violation')
 return changes

def operator_transition_key(event_id,row,column,old,new):
 return sha('|'.join(('glp-affiliate-operator-evidence:v1',SHEET_ID,TAB,event_id,str(row),str(column),sha(old),sha(new))))


def load_closed_program_tombstones(path=CLOSED_PROGRAM_TOMBSTONES):
 if not Path(path).is_file():return []
 payload=json.loads(Path(path).read_text())
 if payload.get('schema')!='glp_affiliate_closed_program_tombstones_v1':raise RuntimeError('closed_program_tombstone_schema_invalid')
 entries=payload.get('entries') or []
 for entry in entries:
  required=('network','program_id','advertiser_name','state','closure_event_id','operator_authority_message_id','sheet_deletion_receipt')
  if any(not entry.get(key) for key in required) or entry.get('state')!='closed':raise RuntimeError('closed_program_tombstone_entry_invalid')
  receipt_path=Path(entry['sheet_deletion_receipt'])
  if not receipt_path.is_file():raise RuntimeError('closed_program_tombstone_receipt_missing')
  receipt=json.loads(receipt_path.read_text())
  expected=f"{entry['network']}:{entry['program_id']}"
  if receipt.get('status')!='PASS' or receipt.get('target_identity')!=expected or receipt.get('remaining_target_identity_rows')!=[]:raise RuntimeError('closed_program_tombstone_receipt_invalid')
 return entries

def match_closed_program_tombstone(candidate,entries):
 if candidate.get('action') not in {'relationship_approval','relationship_adverse','routine_information'}:return None
 text=norm(candidate.get('text'));explicit_name=norm(candidate.get('advertiser_name'));ids={str(value) for value in candidate.get('program_ids',[])}
 for entry in entries:
  if entry['network']!=candidate.get('network'):continue
  name=norm(entry['advertiser_name']);program=str(entry['program_id'])
  if program in ids or (len(name)>=5 and (explicit_name==name or name in text)):return entry
 return None

def closed_program_absent_from_sheet(values,entry):
 target_name=norm(entry['advertiser_name']);target_program=str(entry['program_id'])
 for raw in values[1:]:
  row=pad(raw)
  if row[4]!=entry['network']:continue
  if norm(row[2])==target_name or str(program_id(row,entry['network']) or '')==target_program:return False
 return True

def prior_rejection_evidence_match(values,event,candidate):
 """Bind an authenticated rejection to one already-materialized, footprint-reducing row."""
 if candidate.get('network')!='Impact.com' or candidate.get('action')!='relationship_adverse':return None
 campaign_ids=[str(value) for value in candidate.get('campaign_ids') or []]
 if len(campaign_ids)!=1:return None
 provider_message_id=str(event['provider_message_id'] or '') if 'provider_message_id' in event.keys() else ''
 if not provider_message_id:return None
 matches=[]
 for row_number,raw in enumerate(values[1:],2):
  row=pad(raw);route=norm(row[4]);status=str(row[10] or '')
  if not route.startswith('impact') or status not in {
     'rejected_brand_declined_application','rejected_network_declined_application','application_rejected'}:
   continue
  if str(row[19]).strip().lower() not in {'','false','0'} or str(row[20]).strip().lower() not in {'','false','0'}:continue
  evidence_match=re.search(r'\bevidence=([A-Za-z0-9_./-]+\.json)\b',str(row[23] or ''))
  if not evidence_match:continue
  evidence_path=(BUSINESS/evidence_match.group(1)).resolve()
  try:evidence_path.relative_to(BUSINESS.resolve())
  except ValueError:continue
  if not evidence_path.is_file():continue
  try:payload=json.loads(evidence_path.read_text())
  except (OSError,json.JSONDecodeError):continue
  if (payload.get('queue_kind')!='rejection' or int(payload.get('canonical_sheet_row') or 0)!=row_number
      or norm(payload.get('network'))!='impact' or norm(payload.get('program'))!=norm(row[2])):continue
  email_hits=[item for item in payload.get('provider_email_evidence') or []
              if str(item.get('gmail_id') or '')==provider_message_id
              and str(item.get('subject') or '')==str(event['subject'] or '')
              and 'declined' in str(item.get('body_verbatim') or '').lower()]
  if len(email_hits)!=1:continue
  matches.append({'row_number':row_number,'row':row,'evidence_path':evidence_path,
                  'evidence_sha256':sha_bytes(evidence_path.read_bytes())})
 if len(matches)>1:raise RuntimeError(f"ambiguous_prior_rejection_evidence:Impact.com:matches={len(matches)}")
 return matches[0] if matches else None

def reconcile_prior_email_rejection(event,candidate,matched,gmail,state,dry_run):
 """Terminalize an email whose exact rejection was already applied by the single writer."""
 row_number=matched['row_number'];row=matched['row'];campaign_id=int(candidate['campaign_ids'][0])
 receipt={
  'status':'PASS','event_id':event['event_id'],'network':'Impact.com','program_id':campaign_id,
  'row_number':row_number,'signal':'relationship_adverse','action_kind':'authenticated_email_rejection',
  'action_kind_detail':'prior_single_writer_rejection_materialization_verified',
  'action_completed':not dry_run,'authenticated_network_notice_verified':True,
  'provider_relationship':'rejected_by_authenticated_email','provider_readback_verified':False,
  'affiliate_provider_api_called':False,'sheet_readback_verified':True,
  'prior_materialization_verified':True,
  'prior_materialization_evidence_path':str(matched['evidence_path'].relative_to(BUSINESS)),
  'prior_materialization_evidence_sha256':matched['evidence_sha256'],
  'partner_account_ids':list(candidate.get('partner_account_ids') or []),
  'advertiser_ids':list(candidate.get('advertiser_ids') or []),
  'campaign_ids':list(candidate.get('campaign_ids') or []),
  'sheet_status':str(row[10]),'changed_columns':[],'sheet_write_count':0,'dry_run':dry_run,
  'external_send':False,'provider_mutation':False,'provider_program_mutation':False,
  'tracking_link_generated':False,'archive':False,'delete':False,'provider_draft':False,
  'affiliate_redirect_clicks':0,
 }
 path=save_receipt(event['event_id'],receipt);mark_path=None
 if dry_run:mark_state(state,event['event_id'],'dry_run_pass',receipt=path)
 else:
  cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
  try:ledger.resolve_automatically(event['event_id'],automation_id=AUTOMATION_ID,authority_message_id=AUTHORITY_MESSAGE,reconciliation_receipt=receipt)
  finally:ledger.close()
  mark_path,_=finalize_mailbox_completion(gmail,event['event_id'],path)
  mark_state(state,event['event_id'],'pass',receipt=path)
 return {'event_id':event['event_id'],'network':'Impact.com','program_id':campaign_id,'row':row_number,
         'changes':[],'receipt':str(path),'mark_read_receipt':str(mark_path) if mark_path else None,
         'prior_materialization_verified':True}

def build_unclassified_awin_invitation_row(candidate,snap,received_at):
 details=snap.get('details') or {};program=int(snap['program_id']);name=str(details.get('name') or candidate.get('advertiser_name') or '').strip()
 if not name:raise RuntimeError('awin_invitation_provider_name_missing')
 official=str(details.get('displayUrl') or '').strip();description=str(details.get('description') or '')
 public_commission='Not stated; publisher-specific terms unavailable until joined'
 commission_match=re.search(r'([^.!?]*\b(?:up to\s+)?\d+(?:\.\d+)?%\s+commission[^.!?]*)',description,re.I)
 if commission_match:public_commission='Public Awin programme profile: '+commission_match.group(1).strip()+'; publisher-specific terms unavailable until joined'
 public_cookie=cookie_window(description);public_cookie=('Public Awin programme profile: '+public_cookie) if public_cookie else 'Not stated'
 slug=re.sub(r'-+','-',re.sub(r'[^a-z0-9]+','-',name.lower())).strip('-')
 return ['REVIEW',False,name,'Unclassified affiliate invitation','Awin',f'https://ui.awin.com/awin/affiliate/{PUBLISHER_AWIN}/merchant-profile/{program}',official,'Authenticated Awin invitation captured; venture fit and promotion scope not yet evaluated.','Not assigned',slug,'invited_not_joined',False,'https://ui.awin.com/',PUBLISHER_AWIN,'','Not stated',public_commission,public_cookie,'',False,False,'not_tested_no_link','FTC affiliate disclosure required; no unsupported medical claims; keep link-dark until fit, joined terms, and tracking are verified.',f'Awin programme {program}; authenticated relationship=notjoined; captured automatically from exact-ID invitation; no acceptance/application executed.',now(),'out-of-scope','']
def append_awin_invitation_row(svc,candidate,snap,received_at):
 intended=build_unclassified_awin_invitation_row(candidate,snap,received_at);program=int(snap['program_id'])
 gate=choke_candidate({'display_name':intended[2],'network':'Awin','program_id':program,'category':intended[3],'description':str((snap.get('details') or {}).get('description') or candidate.get('text') or ''),'destination':intended[6],'application_url':intended[5]},require_application_url=True,operation='affiliate_approval_auto_reconciler.append_awin_invitation_row',persist=True)
 if not gate['operational']:raise AffiliateCandidateParked(gate['decision'],gate['reason_codes'])
 with sheet_write_lock():
  require_checkbox_schema_pass(svc,SHEET_ID,TAB)
  meta=execute_google_request(svc.spreadsheets().get(spreadsheetId=SHEET_ID,fields='sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))'),'sheet_invitation_metadata_read')
  props=next(item['properties'] for item in meta['sheets'] if item['properties']['title']==TAB);sheet_id=int(props['sheetId'])
  values=execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE'),'sheet_invitation_pre_read').get('values',[])
  if not values or pad(values[0])!=HEADERS:raise RuntimeError('canonical_sheet_header_mismatch')
  matches=[];name_matches=[];used=set()
  for row_number,raw in enumerate(values[1:],2):
   row=pad(raw)
   if row_has_business_value(row):used.add(row_number)
   if row[4]=='Awin' and program_id(row,'Awin')==program:matches.append((row_number,row))
   if row[4]=='Awin' and norm(row[2])==norm(intended[2]):name_matches.append(row_number)
  if len(matches)>1:raise RuntimeError(f'duplicate_sheet_identity:Awin:{program}')
  if len(matches)==1:return matches[0][0],matches[0][1],False,0
  if name_matches:raise RuntimeError(f'legacy_identity_mapping_required:Awin:{program}:{name_matches}')
  empty=[row_number for row_number in range(2,int(props['gridProperties']['rowCount'])+1) if row_number not in used]
  if empty:row_number=empty[0]
  else:
   row_number=int(props['gridProperties']['rowCount'])+1
   execute_google_request(svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID,body={'requests':[{'appendDimension':{'sheetId':sheet_id,'dimension':'ROWS','length':1}}]}),'sheet_invitation_append_row')
  source=next((number for number,raw in enumerate(values[1:],2) if pad(raw)[4]=='Awin' and row_has_business_value(raw)),None)
  if not source:raise RuntimeError('awin_format_source_missing')
  formatting=[{'copyPaste':{'source':{'sheetId':sheet_id,'startRowIndex':source-1,'endRowIndex':source,'startColumnIndex':0,'endColumnIndex':len(HEADERS)},'destination':{'sheetId':sheet_id,'startRowIndex':row_number-1,'endRowIndex':row_number,'startColumnIndex':0,'endColumnIndex':len(HEADERS)},'pasteType':'PASTE_FORMAT','pasteOrientation':'NORMAL'}}]
  formatting.extend(schema_requests(sheet_id,[row_number]));execute_google_request(svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID,body={'requests':formatting}),'sheet_invitation_schema_write')
  live=execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE'),'sheet_invitation_cas_read').get('values',[])
  if substantive_values_sha(live)!=substantive_values_sha(values):raise RuntimeError('sheet_concurrency_drift_before_invitation_write')
  require_marketplace_parent_rows_not_mutated((row_number, column) for column in range(len(HEADERS)))
  execute_google_request(svc.spreadsheets().values().update(spreadsheetId=SHEET_ID,range=f"'{TAB}'!A{row_number}:AA{row_number}",valueInputOption='RAW',body={'values':[intended]}),'sheet_invitation_value_write')
  got=pad(execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!A{row_number}:AA{row_number}",valueRenderOption='UNFORMATTED_VALUE'),'sheet_invitation_post_read').get('values',[[]])[0])
  if any((str(got[column]).upper()!=str(value).upper() if isinstance(value,bool) else str(got[column])!=str(value)) for column,value in enumerate(intended)):raise RuntimeError('sheet_invitation_post_write_readback_failed')
  require_checkbox_schema_pass(svc,SHEET_ID,TAB)
  return row_number,got,True,len(HEADERS)

def request_json(session,url,params=None):
 for attempt in range(4):
  r=session.get(url,params=params,timeout=120)
  if r.status_code==429 and attempt<3:
   delay=min(max(int(r.headers.get('Retry-After') or 2),2)*(attempt+1),30);time.sleep(delay);continue
  if r.status_code>=400:raise RuntimeError(f'provider_http_error:{r.status_code}')
  return r.json()
 raise RuntimeError('provider_http_retry_exhausted')
def awin_relationship_index():
 secret=json.loads(AWIN_SECRET.read_text())['api_key'];s=requests.Session();s.headers.update({'Authorization':f'Bearer {secret}','Accept':'application/json','User-Agent':'GLPClinicScout-AutoReconciler/1.0'});relations={};details={}
 for rel in ('joined','pending','notjoined','rejected','suspended'):
  rows=request_json(s,f'https://api.awin.com/publishers/{PUBLISHER_AWIN}/programmes',{'relationship':rel})
  for r in rows if isinstance(rows,list) else []: relations[int(r['id'])]=rel; details[int(r['id'])]=r
 return s,relations,details,secret
def awin_snapshot(program,cache=None,expected_name=None):
 s,relations,details,secret=cache or awin_relationship_index();relation=relations.get(program,'unknown');detail=details.get(program,{})
 if expected_name and detail:
  actual,expected=norm(detail.get('name')),norm(expected_name)
  if len(actual)<5 or len(expected)<5 or (actual not in expected and expected not in actual):raise RuntimeError('awin_provider_name_mismatch')
 groups=[]
 if relation=='joined': groups=request_json(s,f'https://api.awin.com/publishers/{PUBLISHER_AWIN}/commissiongroups',{'advertiserId':program}).get('commissionGroups',[])
 return {'provider':'Awin','program_id':program,'relationship':relation,'groups':groups,'details':detail,'verified':relation!='unknown'}

def awin_exact_relationship_snapshot(candidate,cache,relationship='joined'):
 s,relations,details,secret=cache
 ids=[int(value) for value in candidate.get('program_ids') or []]
 if ids:
  matches=[program for program in ids if relations.get(program)==relationship]
 else:
  expected=str(candidate.get('advertiser_name') or candidate.get('program_name') or '').strip()
  matches=[program for program,actual_relationship in relations.items()
           if actual_relationship==relationship and equivalent_advertiser_name(
            str((details.get(program) or {}).get('name') or ''),expected)]
 if len(matches)!=1:raise RuntimeError(f'awin_{relationship}_identity_count:{len(matches)}')
 program=matches[0]
 return program,awin_snapshot(program,cache,candidate.get('advertiser_name') or candidate.get('program_name'))

def awin_missing_pending_queue_row(event,candidate,snapshot):
 details=snapshot.get('details') or {};program=int(snapshot['program_id'])
 name=str(details.get('name') or candidate.get('advertiser_name') or '').strip()
 official=str(details.get('displayUrl') or '').strip()
 if not name or not official.startswith('https://'):raise RuntimeError('awin_missing_row_provider_identity_incomplete')
 classification=classify_program(name,official,details)
 vertical=str(classification.get('vertical') or '')
 if not vertical:raise RuntimeError('awin_missing_row_vertical_unclassified')
 slug=re.sub(r'-+','-',re.sub(r'[^a-z0-9]+','-',name.lower())).strip('-')
 row_cells={
  '1':'TRUE','2':name,'3':vertical,'4':'Awin',
  '5':f'https://ui.awin.com/awin/affiliate/{PUBLISHER_AWIN}/merchant-profile/{program}',
  '6':official,'7':str(classification.get('reason') or ''),'8':'Not assigned','9':slug,
  '11':'TRUE','12':'https://ui.awin.com/','13':PUBLISHER_AWIN,
  '19':'FALSE','20':'FALSE','21':'not_tested_no_link',
  '22':'FTC affiliate disclosure required; no unsupported medical claims; keep link-dark until fit, joined terms, and tracking are verified.',
  '24':now(),'25':vertical,
 }
 evidence={
  'source':'authenticated_awin_relationship_approval',
  'submitted_at_utc':str(event['received_at']),
  'event_id':str(event['event_id']),
  'provider_relationship':str(snapshot.get('relationship') or ''),
  'row_cells':row_cells,
 }
 return {
  'update_id':f"awin-missing-row-pending-{program}-{event['event_id']}",
  'kind':'application_pending','network':'Awin','program_id':str(program),
  'program_name':name,'sheet_row':'','evidence':json.dumps(evidence,sort_keys=True),
  'status':'queued','queued_by':'affiliate_approval_auto_reconciler',
  'queued_at_utc':now(),'resolved_at_utc':'',
  'note':('Missing Sheet row discovered while reconciling an authenticated Awin approval. '
          'Create the weakest historical application state through the single writer, then '
          'apply the separately provider-verified approval transition in this same event.'),
 }

def read_bound_awin_row(svc,candidate,program):
 values=execute_google_request(svc.spreadsheets().values().get(
  spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE'),
  'sheet_missing_awin_row_read').get('values',[])
 bound=dict(candidate);bound['text']=str(candidate.get('text') or '')+f'\nAwin aid: {program}'
 try:return (*bind_row(values,bound),values)
 except RuntimeError as exc:
  if str(exc)!=f'sheet_identity_not_found:Awin:matches=0':raise
  return None,None,None,values

def create_missing_awin_pending_via_single_writer(event,candidate,snapshot,svc,dry_run):
 program=int(snapshot['program_id'])
 row_num,row,bound_program,values=read_bound_awin_row(svc,candidate,program)
 if row_num is not None:
  return row_num,row,bound_program,{'status':'noop','reason':'sheet_row_already_exists','queued':False}
 queue_row=awin_missing_pending_queue_row(event,candidate,snapshot)
 if dry_run:
  intended=json.loads(queue_row['evidence'])['row_cells'];synthetic=['']*len(HEADERS)
  for column,value in intended.items():synthetic[int(column)]=value
  synthetic[10]='applied_pending_brand_approval';synthetic[11]=True;synthetic[19]=False;synthetic[20]=False
  empty=next((number for number,raw in enumerate(values[1:],2) if not row_has_business_value(raw)),len(values)+1)
  return empty,synthetic,program,{'status':'dry_run','queued':False,'queue_row':queue_row}
 existing=n8n_dt.rows(SHEET_UPDATE_QUEUE,n8n_dt.eq('program_id',str(program)))
 candidates=[item for item in existing
             if norm(item.get('network'))==norm('Awin') and str(item.get('kind') or '')=='application_pending'
             and str(item.get('status') or 'queued').lower() in {'','queued'}]
 if candidates:
  queue_row=sorted(candidates,key=lambda item:str(item.get('update_id') or ''))[0]
  inserted=False
 else:
  try:n8n_dt.insert(SHEET_UPDATE_QUEUE,queue_row);inserted=True
  except Exception:
   landed=n8n_dt.rows(SHEET_UPDATE_QUEUE,n8n_dt.eq('update_id',queue_row['update_id']))
   if len(landed)!=1:raise
   queue_row=landed[0];inserted=False
 completed=run_sheet_drain(queue_row)
 if completed.returncode!=0:raise RuntimeError(f'sheet_pending_row_drain_failed:{completed.returncode}:{(completed.stderr or completed.stdout)[-500:]}')
 try:summary=json.loads((completed.stdout or '').strip().splitlines()[-1])
 except (ValueError,IndexError) as exc:raise RuntimeError('sheet_pending_row_drain_output_invalid') from exc
 results=summary.get('results') or []
 if len(results)!=1 or results[0].get('status') not in {'applied','noop'}:
  raise RuntimeError(f"sheet_pending_row_drain_not_terminal:{json.dumps(results,sort_keys=True)[:500]}")
 result=results[0]
 n8n_dt.update(SHEET_UPDATE_QUEUE,n8n_dt.eq('update_id',str(queue_row['update_id'])),{
  'status':result['status'],'resolved_at_utc':now(),'note':str(result.get('reason') or queue_row.get('note') or '')[:900],
 })
 row_num,row,bound_program,_=read_bound_awin_row(svc,candidate,program)
 if row_num is None:raise RuntimeError('sheet_pending_row_missing_after_writer_pass')
 return row_num,row,bound_program,{
  'status':result['status'],'queued':True,'inserted':inserted,'update_id':queue_row['update_id'],
  'cells_written':int(result.get('cells_written') or 0),'receipt':result.get('receipt'),
 }

def queue_awin_email_rejection(event,candidate,row_num,row,program,svc,dry_run):
 """Apply an authenticated Awin rejection without an affiliate-provider call."""
 event_id=str(event['event_id']);program=str(program);observed=str(event['received_at'])
 capture_path=REPORT_DIR/'private-email-rejections'/f'{event_id}.json'
 capture={
  'schema':'awin.authenticated_email_rejection.v1','captured_at_utc':now(),
  'event_id':event_id,'raw_sha256':sha_bytes(Path(str(event['raw_path'])).read_bytes()),
  'network':'Awin','program_id':program,'program_name':str(row[2]),
  'sheet_row':row_num,'observed_at_utc':observed,
  'authenticated_network_notice_verified':True,
  'signal_excerpt':str(candidate.get('text') or '')[:1600],
  'affiliate_provider_api_called':False,'provider_mutation':False,'outbound_send':False,
 }
 relative=str(capture_path.relative_to(BUSINESS))
 evidence={
  'rejection_evidence_path':relative,'observed_at_utc':observed,
  'source':f'authenticated_awin_email_event:{event_id}','decided_by':'brand',
 }
 queue_row={
  'update_id':f'awin-email-rejection-{program}-{event_id}',
  'kind':'rejection','network':'Awin','program_id':program,
  'program_name':str(row[2]),'sheet_row':str(row_num),
  'evidence':json.dumps(evidence,sort_keys=True),'status':'queued',
  'queued_by':'affiliate_approval_auto_reconciler','queued_at_utc':now(),
  'resolved_at_utc':'',
  'note':(f'Authenticated Awin email says advertiser rejected this application; '
          f'rejection_evidence_path={relative}; observed_at_utc={observed}.'),
 }
 if dry_run:
  return {'status':'dry_run','queued':False,'queue_row':queue_row,
          'cells_written':0,'row_status':str(row[10])}
 capture_path.parent.mkdir(parents=True,exist_ok=True)
 capture_path.write_text(json.dumps(capture,indent=2,sort_keys=True)+'\n');capture_path.chmod(0o600)
 existing=n8n_dt.rows(SHEET_UPDATE_QUEUE,n8n_dt.eq('update_id',queue_row['update_id']))
 if len(existing)>1:raise RuntimeError('awin_email_rejection_queue_identity_ambiguous')
 if existing:
  queue_row=existing[0];inserted=False
 else:
  n8n_dt.insert(SHEET_UPDATE_QUEUE,queue_row);inserted=True
 if str(queue_row.get('status') or '').lower() not in {'applied','noop'}:
  completed=run_sheet_drain(queue_row)
  if completed.returncode!=0:
   raise RuntimeError(f'awin_email_rejection_drain_failed:{completed.returncode}:{(completed.stderr or completed.stdout)[-500:]}')
  try:summary=json.loads((completed.stdout or '').strip().splitlines()[-1])
  except (ValueError,IndexError) as exc:raise RuntimeError('awin_email_rejection_drain_output_invalid') from exc
  results=summary.get('results') or []
  if len(results)!=1 or results[0].get('status') not in {'applied','noop'}:
   raise RuntimeError(f"awin_email_rejection_drain_not_terminal:{json.dumps(results,sort_keys=True)[:500]}")
  result=results[0]
  n8n_dt.update(SHEET_UPDATE_QUEUE,n8n_dt.eq('update_id',str(queue_row['update_id'])),{
   'status':result['status'],'resolved_at_utc':now(),
   'note':str(result.get('reason') or queue_row.get('note') or '')[:900],
  })
 else:
  result={'status':str(queue_row['status']).lower(),'cells_written':0,
          'reason':'queue_row_already_terminal','receipt':None}
 reread=pad(execute_google_request(svc.spreadsheets().values().get(
  spreadsheetId=SHEET_ID,range=f"'{TAB}'!A{row_num}:AA{row_num}",
  valueRenderOption='UNFORMATTED_VALUE'),'sheet_awin_email_rejection_readback').get('values',[[]])[0])
 if str(reread[2])!=str(row[2]) or canonical_network_label(reread[4])!=canonical_network_label('Awin'):
  raise RuntimeError('awin_email_rejection_identity_readback_failed')
 if str(reread[10])!='rejected_brand_declined_application':
  raise RuntimeError(f'awin_email_rejection_status_readback_failed:{reread[10]}')
 return {
  'status':result['status'],'queued':True,'inserted':inserted,
  'update_id':queue_row['update_id'],'cells_written':int(result.get('cells_written') or 0),
  'receipt':result.get('receipt'),'evidence_path':relative,
  'row_status':str(reread[10]),'sheet_readback_verified':True,
 }

def classify_awin_invitation(name,official,details):
 # Awin's programme object also carries operational labels such as
 # `membershipStatus=Marketplace`; those are not merchant-fit evidence and made
 # every invite look like ecommerce. Restrict fit to the merchant-authored
 # description and Awin's merchant sector taxonomy.
 evidence={key:details.get(key) for key in ('description','primarySector') if details.get(key)}
 return classify_program(name,official,evidence)

def awin_invitation_disposition_queue_row(event,candidate,snapshot):
 details=snapshot.get('details') or {};program=int(snapshot['program_id'])
 name=str(details.get('name') or candidate.get('advertiser_name') or '').strip();official=str(details.get('displayUrl') or '').strip()
 if not name or not official.startswith('https://'):raise RuntimeError('awin_invitation_provider_identity_incomplete')
 classification=classify_awin_invitation(name,official,details)
 if classification.get('portfolio')=='glp':raise RuntimeError('awin_core_invitation_requires_acceptance_lane')
 vertical=str(classification.get('vertical') or '')
 if not vertical:raise RuntimeError('awin_invitation_vertical_unclassified')
 fit_classification='sister' if classification.get('portfolio')=='sister' else 'out_of_scope'
 application_url=f'https://ui.awin.com/awin/affiliate/{PUBLISHER_AWIN}/merchant-profile/{program}'
 capture={
  'schema':'awin.invitation_disposition_capture.v1','captured_at_utc':now(),
  'event_id':str(event['event_id']),'raw_sha256':sha_bytes(Path(str(event['raw_path'])).read_bytes()),
  'network':'Awin','publisher_id':PUBLISHER_AWIN,'program_id':program,'program_name':name,
  'official_url':official,'provider_relationship':str(snapshot.get('relationship') or ''),
  'classification':classification,'fit_classification':fit_classification,
  'disposition':'not_joined_fit_unsupported','provider_mutation':False,'outbound_send':False,
 }
 capture_path=approval_atomic_json(REPORT_DIR/'invitation-intake'/str(event['event_id'])/f'awin-{program}.json',capture)
 capture_rel=str(capture_path.relative_to(BUSINESS));capture_sha=sha_bytes(capture_path.read_bytes())
 pool=str(classification.get('pool_key') or 'sister-pool::other').split('::')[-1]
 category=f'Sister pool: {pool.replace("-"," ")}'
 fit_reason=str(classification.get('reason') or '')
 evidence={
  'invitation_capture_path':capture_rel,'invitation_capture_sha256':capture_sha,
  'observed_at_utc':str(event['received_at']),'source_event_id':str(event['event_id']),
  'network_program_id':str(program),'provider_relationship':'notjoined',
  'fit_classification':fit_classification,'disposition':'not_joined_fit_unsupported',
  'application_url':application_url,'official_url':official,'category':category,
  'fit_reason':fit_reason,'vertical':vertical,
  'row_cells':{
   '2':name,'3':category,'4':'Awin','5':application_url,'6':official,'7':fit_reason,
   '8':'Not assigned','9':re.sub(r'-+','-',re.sub(r'[^a-z0-9]+','-',name.lower())).strip('-'),
   '12':'https://ui.awin.com/','13':PUBLISHER_AWIN,
   '22':'FTC affiliate disclosure required; no unsupported medical claims; keep link-dark while unjoined.',
   '25':vertical,'26':'Awin API relationship readback; invitation acceptance not executed.',
  },
 }
 return {
  'update_id':f"awin-invitation-disposition-{program}-{event['event_id']}",
  'kind':'invitation_disposition','network':'Awin','program_id':str(program),
  'program_name':name,'sheet_row':'','evidence':json.dumps(evidence,sort_keys=True),
  'status':'queued','queued_by':'affiliate_approval_auto_reconciler','queued_at_utc':now(),
  'resolved_at_utc':'','note':('Automated Awin invite classified outside the GLP portfolio; '
   'record provider-verified invitation and keep the program unjoined and link-dark.'),
 }

def create_missing_awin_invitation_via_single_writer(event,candidate,snapshot,svc,dry_run):
 program=int(snapshot['program_id']);row_num,row,bound_program,values=read_bound_awin_row(svc,candidate,program)
 if row_num is not None:return row_num,row,bound_program,{'status':'noop','reason':'sheet_row_already_exists','queued':False}
 queue_row=awin_invitation_disposition_queue_row(event,candidate,snapshot)
 if dry_run:
  intended=json.loads(queue_row['evidence'])['row_cells'];synthetic=['']*len(HEADERS)
  for column,value in intended.items():synthetic[int(column)]=value
  synthetic[0]='LOW';synthetic[1]=False;synthetic[10]='invited_not_joined';synthetic[11]=False;synthetic[19]=False;synthetic[20]=False;synthetic[21]='not_tested_no_link'
  empty=next((number for number,raw in enumerate(values[1:],2) if not row_has_business_value(raw)),len(values)+1)
  return empty,synthetic,program,{'status':'dry_run','queued':False,'queue_row':queue_row}
 existing=n8n_dt.rows(SHEET_UPDATE_QUEUE,n8n_dt.eq('update_id',queue_row['update_id']))
 if existing:queue_row=sorted(existing,key=lambda item:str(item.get('update_id') or ''))[0];inserted=False
 else:
  try:n8n_dt.insert(SHEET_UPDATE_QUEUE,queue_row);inserted=True
  except Exception:
   landed=n8n_dt.rows(SHEET_UPDATE_QUEUE,n8n_dt.eq('update_id',queue_row['update_id']))
   if len(landed)!=1:raise
   queue_row=landed[0];inserted=False
 completed=run_sheet_drain(queue_row)
 if completed.returncode!=0:raise RuntimeError(f'sheet_invitation_row_drain_failed:{completed.returncode}:{(completed.stderr or completed.stdout)[-500:]}')
 try:summary=json.loads((completed.stdout or '').strip().splitlines()[-1])
 except (ValueError,IndexError) as exc:raise RuntimeError('sheet_invitation_row_drain_output_invalid') from exc
 results=summary.get('results') or []
 # 2026-08-27 (#111 fix, live case Lunzo DE evt_b3b746709bd2): the single-writer's
 # relevance gate refusing an invitation-disposition row IS the disposition — the
 # program is outside the portfolio, stays unjoined and link-dark, and gets NO
 # Sheet row by design. That verdict is terminal; re-raising it carded the
 # operator on the same email every pass. Any OTHER refusal still raises.
 if len(results)==1 and results[0].get('status')=='refused'    and 'invitation_disposition_program_not_relevant' in str(results[0].get('reason') or ''):
  refusal=results[0]
  n8n_dt.update(SHEET_UPDATE_QUEUE,n8n_dt.eq('update_id',str(queue_row['update_id'])),{'status':'refused','resolved_at_utc':now(),'note':str(refusal.get('reason') or '')[:900]})
  raise AffiliateCandidateParked('invitation_program_not_relevant',[str(refusal.get('reason') or 'relevance_refused')])
 if len(results)!=1 or results[0].get('status') not in {'applied','noop'}:raise RuntimeError(f"sheet_invitation_row_drain_not_terminal:{json.dumps(results,sort_keys=True)[:500]}")
 result=results[0]
 n8n_dt.update(SHEET_UPDATE_QUEUE,n8n_dt.eq('update_id',str(queue_row['update_id'])),{'status':result['status'],'resolved_at_utc':now(),'note':str(result.get('reason') or queue_row.get('note') or '')[:900]})
 row_num,row,bound_program,_=read_bound_awin_row(svc,candidate,program)
 if row_num is None:raise RuntimeError('sheet_invitation_row_missing_after_writer_pass')
 return row_num,row,bound_program,{'status':result['status'],'queued':True,'inserted':inserted,'update_id':queue_row['update_id'],'cells_written':int(result.get('cells_written') or 0),'receipt':result.get('receipt')}

def impact_credentials():
 credential=load_impact_api_credentials()
 return credential.account_sid,credential.auth_token,credential.api_base
def impact_snapshot(programs,expected_name,expected_action=None):
 credential_values=impact_credentials();sid,token=credential_values[:2];api_base=credential_values[2] if len(credential_values)>2 else 'https://api.impact.com';session=requests.Session();session.auth=(sid,token);session.headers.update({'Accept':'application/json','User-Agent':'GLPClinicScout-AutoReconciler/1.0'});saw_404=[]
 for program in [int(x) for x in programs]:
  response=session.get(f'{api_base}/Mediapartners/{sid}/Campaigns/{program}.json',timeout=120)
  if response.status_code==404:saw_404.append(program);continue
  if response.status_code>=400:raise RuntimeError(f'impact_campaign_http:{response.status_code}')
  record=response.json();actual=norm(record.get('CampaignName'));expected=norm(expected_name)
  if len(actual)<4 or len(expected)<4 or (actual not in expected and expected not in actual):continue
  contract=str(record.get('ContractStatus') or '').strip();low=contract.lower()
  if low in {'active','joined','approved'}:relationship='approved'
  elif any(x in low for x in ('pending','requested','awaiting')):relationship='pending'
  elif low=='never joined' and expected_action=='relationship_adverse':relationship='rejected'
  elif low=='never joined':relationship='notjoined'
  else:relationship='unknown'
  return {'provider':'Impact.com','program_id':program,'relationship':relationship,'official_url':record.get('AdvertiserUrl') or record.get('CampaignUrl') or '','tracking_link':record.get('TrackingLink') or '','terms':'','cookie_window':'','details':record,'verified':relationship!='unknown'}
 raise RuntimeError(f'impact_campaign_unavailable:{saw_404}')
def maxbounty_public_terms():
 response=requests.get('https://maxbounty.com/',headers={'User-Agent':'GLPClinicScout-AutoReconciler/1.0'},timeout=120)
 if response.status_code>=400:raise RuntimeError(f'maxbounty_terms_http:{response.status_code}')
 source=re.sub(r'(?is)<(?:script|style)[^>]*>.*?</(?:script|style)>',' ',response.text)
 text=re.sub(r'\s+',' ',html.unescape(re.sub(r'(?s)<[^>]+>',' ',source))).strip()
 marker=re.search(r'\bTerms and Conditions\b',text,re.I)
 if not marker:raise RuntimeError('maxbounty_terms_heading_missing')
 terms=text[marker.start():]
 if len(terms)<1000:raise RuntimeError('maxbounty_terms_capture_incomplete')
 return terms
def maxbounty_snapshot(*,fresh_driver=None):
 # No configured supported publisher API credential exists for this account.
 # authenticationApp is password login, not an API-key replacement.
 if fresh_driver is None or getattr(fresh_driver,'fresh',False) is not True:raise RuntimeError('maxbounty_supported_api_unavailable_fresh_ai_browser_vault_stage_required')
 fresh_driver.goto('https://affiliates.maxbounty.com/settings')
 if fresh_driver.url!='https://affiliates.maxbounty.com/settings' or 'ops@glpclinicscout.com' not in fresh_driver.body_text():raise RuntimeError('maxbounty_protected_principal_unverified')
 fresh_driver.goto('https://affiliates.maxbounty.com/dashboard');body=fresh_driver.body_text().lower()
 if fresh_driver.url!='https://affiliates.maxbounty.com/dashboard' or 'MaxBounty' not in fresh_driver.title() or not all(x in body for x in ('dashboard','campaigns')):raise RuntimeError('maxbounty_dashboard_identity_unverified')
 # This account-access read does not consume campaign/capacity leases, expose
 # session tokens, verify campaign relationships or release provider holds.
 return {'provider':'MaxBounty','program_id':'network-account','publisher_id':'UNKNOWN','relationship':'approved','official_url':'https://maxbounty.com/affiliate/','tracking_link':'','terms':'','cookie_window':'','details':{'network_account':True,'principal_verified':True,'capacity':'UNKNOWN','api_capabilities':'Supported publisher API unavailable; fresh vault authenticated account read only.'},'verified':True,'provider_readback_verified':True,'read_method':'fresh_ai_browser_vault','provider_writes':0}
async def cj_snapshot(program,expected_name):
 secret_path=Path('/home/ubuntu/.hermes/secrets/affiliate-marketplaces/cj/api.json')
 if not secret_path.is_file():raise RuntimeError('cj_api_secret_missing')
 secret=json.loads(secret_path.read_text());token=str(secret.get('personal_access_token') or '').strip();cid=str(secret.get('requestor_cid') or '').strip()
 if not token or not cid:raise RuntimeError('cj_api_secret_incomplete')
 response=requests.get('https://advertiser-lookup.api.cj.com/v2/advertiser-lookup',params={'requestor-cid':cid,'advertiser-ids':str(program)},headers={'Authorization':f'Bearer {token}'},timeout=60)
 if response.status_code>=400:raise RuntimeError(f'cj_relationship_http:{response.status_code}')
 try:root=ET.fromstring(response.content)
 except ET.ParseError as exc:raise RuntimeError('cj_relationship_parse_failed') from exc
 records=[]
 for advertiser in root.findall('.//advertiser'):
  if (advertiser.findtext('advertiser-id') or '').strip()==str(program):records.append(advertiser)
 if len(records)!=1:raise RuntimeError(f'cj_provider_identity_count:{len(records)}')
 record=records[0];name=(record.findtext('advertiser-name') or '').strip();actual,expected=norm(name),norm(expected_name)
 if len(actual)<4 or len(expected)<4 or (actual not in expected and expected not in actual) and not equivalent_advertiser_name(name,expected_name):raise RuntimeError('cj_provider_name_mismatch')
 status=(record.findtext('relationship-status') or '').strip().lower()
 if status=='joined':relationship='approved'
 elif 'pending' in status:relationship='pending'
 elif status in {'not joined','notjoined','declined','rejected'}:relationship='rejected' if status in {'declined','rejected'} else 'notjoined'
 else:relationship='unknown'
 actions=[]
 for action in record.findall('./actions/action'):
  commissions=[str(item.text or '').strip() for item in action.findall('./commission/*') if str(item.text or '').strip()]
  actions.append({'name':(action.findtext('name') or '').strip(),'type':(action.findtext('type') or '').strip(),'commissions':commissions})
 details={'advertiser_id':str(program),'advertiser_name':name,'account_status':(record.findtext('account-status') or '').strip(),'relationship_status':status,'actions':actions}
 terms='; '.join(dict.fromkeys(x for action in actions for x in action['commissions']))
 return {'provider':'CJ Affiliate','program_id':int(program),'relationship':relationship,'official_url':'','tracking_link':'','terms':terms,'cookie_window':'','details':details,'verified':relationship!='unknown'}
def partnerize_relationship_index():
 sec=json.loads(PARTNERIZE_SECRET.read_text());session=requests.Session();session.auth=(sec['user_application_key'],sec['user_api_key']);session.headers.update({'Accept':'application/json','User-Agent':'GLPClinicScout-AutoReconciler/1.0'});campaigns={}
 for status in ('approved','pending','rejected'):
  response=session.get(f'https://api.partnerize.com/user/publisher/{PUBLISHER_PARTNERIZE}/campaign/{status}',timeout=120)
  if response.status_code>=400:raise RuntimeError(f'partnerize_relationship_http:{response.status_code}')
  for wrapper in (response.json().get('campaigns') or []):
   record=wrapper.get('campaign') if isinstance(wrapper,dict) and isinstance(wrapper.get('campaign'),dict) else wrapper
   if isinstance(record,dict) and record.get('campaign_id'):campaigns[str(record['campaign_id'])]=(status,record)
 return session,campaigns

def private_bytes(path,data):
 path.parent.mkdir(parents=True,exist_ok=True);path.parent.chmod(0o700);tmp=path.with_name(f'.{path.name}.{os.getpid()}.tmp')
 with tmp.open('wb') as handle:os.chmod(tmp,0o600);handle.write(data);handle.flush();os.fsync(handle.fileno())
 os.replace(tmp,path);os.chmod(path,0o600);return path

def run_sheet_writer(command,*,max_attempts=3,sleep_fn=time.sleep):
 """Retry the idempotent Sheet writer on bounded transient subprocess failures."""
 if max_attempts<1:raise ValueError('max_attempts_must_be_positive')
 result=None
 for attempt in range(1,max_attempts+1):
  result=subprocess.run(command,cwd=BUSINESS,text=True,capture_output=True,timeout=300)
  if result.returncode==0:return result,attempt
  if attempt<max_attempts:sleep_fn(2**attempt)
 assert result is not None
 return result,max_attempts

def partnerize_exact_approved_snapshot(candidate,cache):
 session,campaigns=cache;name=str(candidate.get('advertiser_name') or candidate.get('program_name') or '').strip()
 if not name:raise RuntimeError('partnerize_approval_name_missing')
 matches=[]
 for program,(status,record) in campaigns.items():
  if status!='approved':continue
  title=str(record.get('title') or '').strip()
  if norm(title)==norm(name) or equivalent_advertiser_name(title,name):matches.append((program,record))
 if len(matches)!=1:raise RuntimeError(f'partnerize_approved_identity_count:{len(matches)}')
 program,record=matches[0]
 return program,partnerize_snapshot(program,cache,record.get('title') or name,record.get('destination_url') or '',allow_link_generation=False)

def partnerize_exact_relationship_snapshot(candidate,cache,required_relationship):
 session,campaigns=cache;name=str(candidate.get('advertiser_name') or candidate.get('program_name') or '').strip()
 if not name:raise RuntimeError('partnerize_relationship_name_missing')
 matches=[]
 for program,(status,record) in campaigns.items():
  title=str(record.get('title') or '').strip()
  if status==required_relationship and (norm(title)==norm(name) or equivalent_advertiser_name(title,name)):matches.append((program,record))
 if len(matches)!=1:raise RuntimeError(f'partnerize_{required_relationship}_identity_count:{len(matches)}')
 program,record=matches[0]
 return program,partnerize_snapshot(program,cache,record.get('title') or name,record.get('destination_url') or '',allow_link_generation=False)

def partnerize_terms_capture(session,program,event_id):
 response=session.get(f'https://api.partnerize.com/v2/publishers/{PUBLISHER_PARTNERIZE}/campaigns/terms',params={'campaigns':str(program)},timeout=180)
 if response.status_code>=400:raise RuntimeError(f'partnerize_terms_http:{response.status_code}')
 raw_path=REPORT_DIR/'private-provider-terms'/f'{event_id}-{program}.json';private_bytes(raw_path,response.content)
 try:payload=response.json()
 except ValueError as exc:raise RuntimeError('partnerize_terms_parse_failed') from exc
 screened=screen_provider_terms(payload)
 return {**screened,'http_status':response.status_code,'raw_path':str(raw_path),'raw_sha256':hashlib.sha256(response.content).hexdigest(),'response_bytes':len(response.content)}

def classify_approved_event(event,candidate,snapshot,program,terms):
 details=snapshot.get('details') or {};name=str(details.get('title') or candidate.get('advertiser_name') or candidate.get('program_name') or '').strip()
 if not name:raise RuntimeError('approved_program_name_missing')
 destination=str(snapshot.get('official_url') or details.get('destination_url') or details.get('displayUrl') or '')
 return record_approval_classification(event_id=str(event['event_id']),network=str(candidate['network']),program_id=str(program),advertiser_id=str(details.get('advertiser_id') or ''),name=name,relationship=str(snapshot['relationship']),destination_url=destination,snapshot=snapshot,terms=terms,raw_terms_path=terms.get('raw_path'),report_root=REPORT_DIR)

def classify_nonapproval_relationship(event,candidate,snapshot,program):
 details=snapshot.get('details') or {};name=str(details.get('title') or candidate.get('advertiser_name') or candidate.get('program_name') or '').strip()
 if not name:raise RuntimeError('relationship_program_name_missing')
 destination=str(snapshot.get('official_url') or details.get('destination_url') or '')
 classification=classify_program(name,destination,details)
 payload={'schema':'affiliate_relationship_classification_v1','status':'PASS','event_id':str(event['event_id']),'network':str(candidate['network']),'program_id':str(program),'program_name':name,'relationship':str(snapshot['relationship']),'classification':classification,'provider_mutation':False,'outbound_send':False,'recorded_at_utc':now()}
 path=approval_atomic_json(REPORT_DIR/'relationship-classifications'/f"{str(candidate['network']).lower()}-{program}-{event['event_id']}.json",payload)
 return {'classification':classification,'artifact_path':str(path),'payload':payload}

def nonglp_relationship_routine(event,cand,gmail,state,dry_run,detail):
 """Complete a relationship event for a program OUTSIDE the GLP portfolio that has
 no Sheet row (and, for provider-unlisted acceptances, no confirmable readback).

 Returns None for GLP-portfolio programs: those keep the fail-closed provider
 flow (sheet row + readback or an operator card). For sister-pool programs the
 truthful record is a routine-information receipt carrying the deterministic
 classification — no Sheet write, no provider mutation — and a cleared mailbox."""
 name=str(cand.get('program_name') or cand.get('advertiser_name') or '')
 if not name:return None
 classification=classify_program(name)
 if classification['portfolio']=='glp':return None
 receipt={'status':'PASS','event_id':event['event_id'],'network':cand['network'],'signal':cand['action'],'action_kind':'routine_affiliate_information','action_kind_detail':detail,'classification':classification,'advertiser_name':cand.get('advertiser_name'),'program_name':cand.get('program_name'),'action_completed':True,'provider_readback_verified':False,'sheet_readback_verified':False,'changed_columns':[],'sheet_write_count':0,'dry_run':dry_run,'external_send':False,'provider_mutation':False,'provider_program_mutation':False,'tracking_link_generated':False,'archive':False,'delete':False,'provider_draft':False,'affiliate_redirect_clicks':0}
 path=save_receipt(event['event_id'],receipt);mark_path=None
 if dry_run:mark_state(state,event['event_id'],'dry_run_pass',receipt=path)
 else:
  if str(event['state']) not in {'resolved_externally','held','archived'}:
   cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
   try:ledger.resolve_automatically(event['event_id'],automation_id=AUTOMATION_ID,authority_message_id=AUTHORITY_MESSAGE,reconciliation_receipt=receipt)
   finally:ledger.close()
  mark_path,_=finalize_mailbox_completion(gmail,event['event_id'],path)
  mark_state(state,event['event_id'],'pass',receipt=path)
 return {'event_id':event['event_id'],'network':cand['network'],'program_id':None,'row':None,'changes':[],'classification':classification,'receipt':str(path),'mark_read_receipt':str(mark_path) if mark_path else None}


def record_approval_fact(event,candidate,snapshot,program,terms,classification,publisher_id):
 publisher_id=str(publisher_id or '').strip()
 if not publisher_id:raise RuntimeError('approval_fact_publisher_id_missing')
 portfolio=str(classification['classification']['portfolio'])
 provider_hash=hashlib.sha256(json.dumps(snapshot,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()
 cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
 try:
  return ledger.record_affiliate_approval_fact(event_id=str(event['event_id']),network=str(candidate['network']),publisher_id=publisher_id,program_id=str(program),provider_snapshot_sha256=provider_hash,terms_snapshot_sha256=str(terms['text_sha256']),classification_portfolio=portfolio,verified_at_utc=now())
 finally:ledger.close()

#: exception_type -> (plain-words what's-wrong template, [step templates]).
#: {program}/{network}/{subject}/{sender}/{date} fill from the event; a card that
#: cannot fill all three blocks REFUSES to render (operator ruling 2026-08-07:
#: never post a bare reason code).
EXCEPTION_CARD_TEMPLATES={
 'account_prerequisite':(
  "{network} cannot pay or activate us yet: the email \"{subject}\" from {sender} ({date}) asks for account prerequisites — typically a tax form (W-9) and payout/bank details — before {program} goes live.",
  ["open that email in the ops@glpclinicscout.com inbox and open the form link(s) it contains",
   "complete every form it names as Tilton Group LLC (tax: W-9; payout: the business bank account)",
   "submit, then reply below so the machine reconciles the account"]),
 'identity_prerequisite':(
  "{network} rejected or still needs the identity document for {program}: the email \"{subject}\" from {sender} ({date}) requires the operator to provide the accepted ID format.",
  ["open that exact email in the ops@glpclinicscout.com inbox and read its accepted-document and file-format rules",
   "from your own device, upload the requested identity document in an accepted format; do not change the account identity",
   "submit, then reply below so the machine re-checks the account"]),
 'provider_account_activation':(
  "{network} reports that {program} approved us, but the publisher account is not active yet: the email \"{subject}\" from {sender} ({date}) requires an operator login/activation step before provider identity can be read back.",
  ["open that exact approval email in the ops@glpclinicscout.com inbox",
   "from your own device, use its activation link and complete only account activation; do not submit another application or change program settings",
   "reply below after the account opens successfully so the machine can verify the provider and reconcile the Sheet"]),
 'operator_draft':(
  "{network} sent a real follow-up about {program}: the email \"{subject}\" from {sender} ({date}) asks for a reply, references, or a meeting decision, and no email may be sent without operator action.",
  ["open that exact email in the ops@glpclinicscout.com inbox and decide whether to continue the conversation",
   "if continuing, reply below with the facts or scheduling choice the drafter may use; do not send from this card",
   "approve the resulting draft separately before any email is sent"]),
 'terms_hard_trigger':(
  "{network}'s terms for {program} tripped a mechanical hold trigger (email \"{subject}\" from {sender}, {date}) — the machine may not consent for you.",
  ["read the held terms excerpt in the private terms artifact for this event",
   "decide accept/decline; if accept, complete the network's consent step yourself",
   "reply below and the machine resumes the application"]),
}
EXCEPTION_CARD_FALLBACK=(
 "{network} sent \"{subject}\" ({sender}, {date}) about {program}: the machine classified it `{exception_type}` and is not authorized to act alone.",
 ["open that email in the ops@glpclinicscout.com inbox and do what it asks",
  "reply below so the machine re-checks the account and continues"])


def _event_card_context(ledger,event,candidate):
 subject=sender=received=''
 row=ledger.conn.execute('SELECT subject,sender,received_at FROM inbox_events WHERE event_id=?',(str(event['event_id']),)).fetchone()
 if row:subject,sender,received=str(row[0] or ''),str(row[1] or ''),str(row[2] or '')
 sender_email=parseaddr(sender)[1] or sender
 return {'subject':subject[:120] or '(no subject)','sender':sender_email[:80] or 'the network','date':received[:10] or 'date unknown',
         'network':str(candidate.get('network') or 'The network'),
         'program':str(candidate.get('advertiser_name') or candidate.get('program_name') or candidate.get('network') or 'this program')[:120]}


def compose_affiliate_exception_card(event,candidate,exception,context):
 """Three mandatory blocks: WHAT'S WRONG in plain words, numbered EXACT STEPS, the
 DONE reply line. Refuses to render a bare reason code."""
 blocker_id=f"BLK-{re.sub(r'[^a-z0-9]+','-',str(candidate.get('network') or 'net').lower()).strip('-')}-{str(event['event_id'])[-6:]}"
 template,steps=EXCEPTION_CARD_TEMPLATES.get(exception.exception_type,EXCEPTION_CARD_FALLBACK)
 fills={**context,'exception_type':exception.exception_type}
 whats_wrong=template.format(**fills).strip()
 numbered=[f'{i}. {s.format(**fills)}' for i,s in enumerate(steps,1)]
 reply_line=f'Reply: `DONE {blocker_id}`'
 if len(whats_wrong)<40 or not str(context.get('subject') or '').strip() or not str(context.get('network') or '').strip():
  raise RuntimeError('exception_card_missing_block:whats_wrong')
 if not numbered:raise RuntimeError('exception_card_missing_block:steps')
 message='\n'.join(['📬 Affiliate exception — your action needed',f'**`{blocker_id}`** · {context["network"]} · {context["program"]}','',f'WHAT\'S WRONG: {whats_wrong}','',*numbered,'',reply_line,f'-# event `{event["event_id"]}` · evidence stored privately · no link, wiring, spend, or send occurred'])
 for block,name in ((whats_wrong,'whats_wrong'),('\n'.join(numbered),'steps'),(reply_line,'reply')):
  if not block.strip():raise RuntimeError(f'exception_card_missing_block:{name}')
 return blocker_id,message


def ensure_affiliate_exception_card(event,candidate,exception):
 cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
 try:
  existing=ledger.conn.execute("SELECT outbox_id,status,discord_message_id FROM notification_outbox WHERE event_id=? AND purpose IN ('operator_decision','affiliate_exception') AND status NOT IN ('cancelled','suppressed') ORDER BY created_at LIMIT 1",(event['event_id'],)).fetchone()
  if existing:return {'queued':False,'existing_status':existing['status'],'discord_message_id':existing['discord_message_id']}
  context=_event_card_context(ledger,event,candidate)
  blocker_id,message=compose_affiliate_exception_card(event,candidate,exception,context)
  intake_dir=Path('/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/operator-blockers-intake')
  intake_dir.mkdir(parents=True,exist_ok=True)
  template,steps=EXCEPTION_CARD_TEMPLATES.get(exception.exception_type,EXCEPTION_CARD_FALLBACK)
  fills={**context,'exception_type':exception.exception_type}
  approval_atomic_json(intake_dir/f'{blocker_id}.json',{'schema':'operator-blocker-intake-v1','id':blocker_id,'source':'exception_card','event_id':str(event['event_id']),'exception_type':exception.exception_type,'cause':template.format(**fills).strip(),'steps':[s.format(**fills) for s in steps],'programs':[context['program']],'keywords':sorted({context['network'].lower(),*re.findall(r'[a-z0-9]{4,}',context['program'].lower())}),'created_at_utc':now()})
  key=f'affiliate-exception:{event["event_id"]}:{exception.exception_type}'
  outbox=ledger.queue_notification(event['event_id'],'affiliate_exception',cp.DECISIONS_CHANNEL_ID,key,message)
  ledger.audit('affiliate_exception_card_queued','inbox_event',event['event_id'],{'exception_type':exception.exception_type,'blocker_id':blocker_id,'automation_id':AUTOMATION_ID,'provider_mutation':False,'outbound_send':False})
  return {'queued':True,'outbox_id':outbox,'blocker_id':blocker_id}
 finally:ledger.close()

def upsert_new_partnerize_approval(event,candidate,snapshot,program,classification,terms,apply,relationship='approved'):
 details=snapshot.get('details') or {};name=str(details.get('title') or candidate.get('advertiser_name') or '').strip();destination=str(snapshot.get('official_url') or details.get('destination_url') or '')
 gate=choke_candidate({'display_name':name,'network':'Partnerize','program_id':program,'description':str(details.get('description') or candidate.get('text') or ''),'destination':destination,'application_url':snapshot.get('application_url') or details.get('application_url') or candidate.get('application_url') or ''},require_application_url=True,operation='affiliate_approval_auto_reconciler.upsert_new_partnerize_approval',persist=apply)
 if not gate['operational']:raise AffiliateCandidateParked(gate['decision'],gate['reason_codes'])
 rate=details.get('default_commission_rate');commission=f'Default: {rate}%' if rate not in (None,'',0,'0') else str(snapshot.get('terms') or 'Not stated')
 cookie=str(snapshot.get('cookie_window') or 'Not stated');directory=REPORT_DIR/'approval-intake'/str(event['event_id'])
 inputs=write_sheet_inputs(directory=directory,network='Partnerize',program_id=str(program),name=name,advertiser_id=str(details.get('advertiser_id') or ''),destination_url=destination,classification_result=classification,terms=commission,cookie_window=cookie,completed_at_utc=now(),relationship=relationship)
 output=directory/'sheet-upsert.json';command=['/usr/bin/python3.12',str(BUSINESS/'scripts/affiliate_marketplace_update_sheet.py'),'--classification',inputs['classification'],'--applications',inputs['applications'],'--relationships',inputs['relationships'],'--output',str(output)]
 if apply:command.append('--apply')
 result,writer_attempts=run_sheet_writer(command,max_attempts=3 if apply else 1)
 if result.returncode!=0:
  diagnostic={'schema':'partnerize_sheet_writer_failure.v1','status':'BLOCKED','event_id':event['event_id'],'attempts':writer_attempts,'returncode':result.returncode,'stdout_sha256':sha(result.stdout or ''),'stderr_sha256':sha(result.stderr or '')}
  private_bytes(directory/'sheet-upsert-failure.json',(json.dumps(diagnostic,indent=2,sort_keys=True)+'\n').encode())
  raise RuntimeError(f'partnerize_sheet_upsert_failed:{result.returncode}:attempts={writer_attempts}')
 receipt=json.loads(output.read_text())
 rows=[row for row in receipt.get('rows') or [] if str(row.get('program_id'))==str(program)]
 planned=[row for row in [*(receipt.get('created') or []),*(receipt.get('updated') or [])] if str(row.get('program_id'))==str(program)]
 selected=rows if apply else planned
 if receipt.get('status')!='PASS' or len(selected)!=1 or (apply and rows[0].get('mismatches')):raise RuntimeError('partnerize_sheet_upsert_readback_failed')
 receipt['reconciler_writer_attempts']=writer_attempts
 return {'sheet_receipt_path':str(output),'sheet_receipt_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'row_number':int(selected[0]['row']),'sheet_write_count':int(receipt.get('value_cells_written') or 0),'created_count':len(receipt.get('created') or []),'updated_count':len(receipt.get('updated') or []),'writer_attempts':writer_attempts}

async def healthtrader_snapshot(program,expected_name,*,fresh_driver=None):
 if str(program)!='network-account' or norm(expected_name)!='healthtrader':raise RuntimeError('healthtrader_identity_mismatch')
 if not HEALTHTRADER_TOKEN.is_file():raise RuntimeError('healthtrader_cached_api_token_missing_no_password_login')
 token=json.loads(HEALTHTRADER_TOKEN.read_text());access=str(token.get('access_token') or '').strip()
 if not access:raise RuntimeError('healthtrader_cached_api_token_incomplete_no_password_login')
 response=requests.get('https://api.healthtrader.com/affiliates/v1/offers/list',headers={'Authorization':f'Bearer {access}','Accept':'application/json','User-Agent':'GLPClinicScout-AutoReconciler/1.0'},timeout=60,allow_redirects=False)
 if response.status_code!=200:raise RuntimeError(f'healthtrader_offers_http:{response.status_code}')
 payload=response.json();offers=payload.get('offer') or payload.get('offers') or payload.get('data') or []
 if isinstance(offers,dict):offers=offers.get('offers') or offers.get('offer') or []
 if not isinstance(offers,list) or not offers:raise RuntimeError('healthtrader_offer_inventory_empty')
 # API inventory access is not account approval. The principal must be read
 # from the protected profile of this job's separately vault-authenticated run.
 if fresh_driver is None or getattr(fresh_driver,'fresh',False) is not True:raise RuntimeError('fresh_ai_browser_vault_stage_required')
 fresh_driver.goto('https://affiliates.healthtrader.com/profile')
 if fresh_driver.url!='https://affiliates.healthtrader.com/profile' or 'ops@glpclinicscout.com' not in fresh_driver.body_text():raise RuntimeError('healthtrader_protected_principal_unverified')
 fresh_driver.goto('https://affiliates.healthtrader.com/dashboard')
 body=fresh_driver.body_text()
 if fresh_driver.url!='https://affiliates.healthtrader.com/dashboard' or 'HealthTrader' not in fresh_driver.title():raise RuntimeError('healthtrader_dashboard_identity_unverified')
 if 'Your account has been in the review' in body or 'account is under review' in body.lower():raise RuntimeError('healthtrader_account_review_pending')
 if not all(marker in body for marker in ('Dashboard','Offers','Account')):raise RuntimeError('healthtrader_dashboard_identity_unverified')
 return {'provider':'HealthTrader','program_id':'network-account','relationship':'approved','official_url':'https://healthtrader.com/us/affiliate-program','tracking_link':'','terms':'','cookie_window':'','details':{'offer_count':len(offers),'dashboard_approved':True,'principal_verified':True,'api_scope':token.get('scope')},'verified':True,'provider_readback_verified':True,'provider_writes':0,'read_method':'official_api_then_fresh_ai_browser_vault'}
def partnerize_snapshot(program,cache,expected_name,destination,allow_link_generation=True):
 session,campaigns=cache;entry=campaigns.get(str(program));
 if not entry:return {'provider':'Partnerize','program_id':program,'relationship':'unknown','verified':False}
 relationship,record=entry;destination=destination or str(record.get('destination_url') or '');actual,expected=norm(record.get('title')),norm(expected_name)
 if len(actual)<4 or len(expected)<4 or (actual not in expected and expected not in actual):raise RuntimeError('partnerize_provider_name_mismatch')
 response=session.get(f'https://api.partnerize.com/v2/publishers/{PUBLISHER_PARTNERIZE}/links',params={'limit':200},timeout=120)
 if response.status_code>=400:raise RuntimeError(f'partnerize_links_http:{response.status_code}')
 links=[x for x in (response.json().get('links') or []) if str(x.get('campaign_id'))==str(program) and x.get('active') is not False]
 tracking='';generated=False
 if links:
  exact=[x for x in links if str(x.get('destination_url') or '').rstrip('/')==str(destination or '').rstrip('/')];chosen=(exact or links)[0];tracking=str(chosen.get('tracking_short_url') or chosen.get('tracking_url') or '')
 if relationship=='approved' and not tracking and allow_link_generation:
  if not str(destination).startswith('https://'):raise RuntimeError('partnerize_destination_missing')
  payload={'campaign_id':str(program),'description':'GLP Clinic Scout default','destination_url':destination,'params':[{'key':'pubref','value':'glp-clinic-scout'}],'active':True}
  created=session.post(f'https://api.partnerize.com/v2/publishers/{PUBLISHER_PARTNERIZE}/links',json=payload,timeout=120)
  if created.status_code>=400:raise RuntimeError(f'partnerize_create_link_http:{created.status_code}')
  link=created.json().get('link') or {};tracking=str(link.get('tracking_short_url') or link.get('tracking_url') or '');generated=True
 if tracking and urllib.parse.urlsplit(tracking).hostname not in {'prf.hn','www.prf.hn'}:raise RuntimeError('partnerize_tracking_host_invalid')
 seconds=record.get('cookie_period');cookie=f"{int(seconds)//86400} days" if isinstance(seconds,(int,float)) and seconds>0 else ''
 terms=''
 if record.get('default_commission_rate') not in (None,'',0,'0'):terms=f"Default: {record['default_commission_rate']}%"
 elif record.get('default_commission_value') not in (None,'',0,'0'):terms=f"Default: {record['default_commission_value']} {record.get('default_currency') or ''}".strip()
 return {'provider':'Partnerize','program_id':program,'relationship':relationship,'details':record,'official_url':destination,'terms':terms,'cookie_window':cookie,'tracking_link':tracking,'link_generated':generated,'verified':relationship in {'approved','pending','rejected'}}
def format_groups(groups):
 def label(g):
  name=str(g.get('groupName') or g.get('groupCode') or 'Commission')
  if g.get('type')=='percentage': val=f"{float(g.get('percentage',0)):g}%"
  else: val=f"{float(g.get('amount',0)):g} {g.get('currency','')}".strip()
  return f'{name}: {val}'
 return '; '.join(label(g) for g in sorted(groups,key=lambda x:(str(x.get('groupCode'))!='DEFAULT',str(x.get('groupName')))))
def awin_link(program,destination): return 'https://www.awin1.com/cread.php?'+urllib.parse.urlencode({'awinmid':str(program),'awinaffid':PUBLISHER_AWIN,'clickref':'','ued':destination})
def payout_terms(body):
 lines=[x.strip() for x in body.splitlines() if x.strip()];
 try:start=lines.index('Payout')+1
 except ValueError: raise RuntimeError('katalys_payout_section_missing')
 endings=[lines.index(marker,start) for marker in ('Applied','Default Landing Page','Product Categories') if marker in lines[start:]]
 if not endings:raise RuntimeError('katalys_payout_end_missing')
 end=min(endings)
 parts=lines[start:end]
 if len(parts)>=2 and parts[0]==parts[1]:parts=parts[1:]
 elif len(parts)%2:parts=parts[1:]
 if len(parts)%2: raise RuntimeError('katalys_payout_pairs_invalid')
 return '; '.join(f'{parts[i]}: {parts[i+1]}' for i in range(0,len(parts),2))
def katalys_relationship_snapshot(program,expected_name):
 """Read one relationship from the live Katalys marketplace GraphQL surface.

 The detail-page adapter remains the only path that may read terms or open the
 Create Link modal. Backlog reconciliation needs neither: it only has to prove
 the exact programme's current relationship. Using the authenticated bearer the
 portal-session lane already refreshes removes the brittle dependency on an open
 CDP tab while preserving a live provider read (never a cached-index assertion).
 """
 import katalys_portal_session as portal
 if not portal.BEARER_PATH.is_file():raise RuntimeError('katalys_portal_bearer_missing')
 bearer=portal.BEARER_PATH.read_text().strip()
 if not bearer:raise RuntimeError('katalys_portal_bearer_empty')
 response=requests.post(portal.GRAPHQL,headers={'authorization':bearer,'content-type':'application/json'},json={
  'query':portal.MARKETPLACE_QUERY,
  'variables':{'orgId':portal.ORG_ID,'limit':portal.PAGE_SIZE,'offset':0,'search':'','pubId':portal.PUB_ID},
 },timeout=60)
 if response.status_code in {401,403}:raise RuntimeError(f'katalys_graphql_session_expired:{response.status_code}')
 if response.status_code!=200:raise RuntimeError(f'katalys_graphql_http:{response.status_code}')
 try:payload=response.json()
 except ValueError as exc:raise RuntimeError('katalys_graphql_non_json') from exc
 if payload.get('errors'):
  errors=payload['errors']
  # HTTP200/FORBIDDEN is unavailable API scope, never an empty relationship.
  # Only this explicit refusal permits an independently authenticated UI read;
  # mixed errors, expired auth and identity failures still stop normally.
  denied=isinstance(errors,list) and all(isinstance(e,dict) and isinstance(e.get('extensions'),dict) and e['extensions'].get('code')=='FORBIDDEN' for e in errors)
  raise RuntimeError('katalys_graphql_org_permission_unavailable' if denied else 'katalys_graphql_errors')
 block=((payload.get('data') or {}).get('offerMarketplace') or {})
 rows=[item for item in (block.get('results') or []) if str(item.get('offerId'))==str(program)]
 if len(rows)!=1:raise RuntimeError(f'katalys_graphql_program_identity_count:{len(rows)}')
 item=rows[0];actual=norm(item.get('offerName'));expected=norm(expected_name)
 if len(actual)<4 or len(expected)<4 or (actual not in expected and expected not in actual):
  raise RuntimeError('katalys_graphql_provider_name_mismatch')
 statuses={str(value.get('status') or '').strip().lower() for value in (item.get('relationships') or [])}
 relation=('approved' if statuses & {'approved','active','accepted'} else
           'pending' if 'pending' in statuses else
           'rejected' if 'rejected' in statuses else 'unknown')
 return {'provider':'Katalys','program_id':program,'relationship':relation,
         'details':{'name':item.get('offerName')},'terms':'','tracking_link':'',
         'link_generated':False,'verified':relation in {'approved','pending','rejected'},
         'provider_readback_verified':relation in {'approved','pending','rejected'},
         'read_method':'authenticated_live_graphql'}
async def katalys_snapshot(program,expected_name,existing_link,allow_link_generation=True,expected_action=None,relationship_only=False,*,fresh_driver=None):
 # Attempt the maintained API read first. A precise permission refusal is not
 # success and may only be replaced by an independently authorized visible read.
 ui_relationship=False
 try:snapshot=await asyncio.to_thread(katalys_relationship_snapshot,program,expected_name)
 except RuntimeError as exc:
  if str(exc)!='katalys_graphql_org_permission_unavailable' or fresh_driver is None:raise
  snapshot=katalys_fresh_relationship_snapshot(program,expected_name,fresh_driver=fresh_driver)
  snapshot['api_readback']='UNAVAILABLE_ORG_PERMISSION';ui_relationship=True
 if relationship_only:return snapshot
 if snapshot.get('relationship')=='approved' and not existing_link and allow_link_generation:raise RuntimeError('katalys_link_generation_held_no_legacy_cdp_fallback')
 snapshot['tracking_link']=existing_link or '';snapshot['link_generated']=False
 # This finite replacement proves only the exact visible site relationship.
 # Missing terms remain UNKNOWN; it grants neither link nor program mutation.
 if ui_relationship:return snapshot
 if fresh_driver is None:
  # No UI terms evidence: leave UNKNOWN/empty, never infer terms from email.
  snapshot['terms']='';snapshot['terms_readback']='NOT_CHECKED_FRESH_VAULT_BROWSER_REQUIRED'
  return snapshot
 if getattr(fresh_driver,'fresh',False) is not True:raise RuntimeError('fresh_ai_browser_vault_stage_required')
 target=f'https://app.katalys.com/pub/{KATALYS_ORG}/explore/{program}'
 fresh_driver.goto(target);body=fresh_driver.body_text()
 if fresh_driver.url!=target or f'Program ID: {program}' not in body or norm(expected_name) not in norm(body):raise RuntimeError('katalys_identity_or_session_mismatch')
 snapshot['terms']=payout_terms(body);snapshot['terms_readback']='FRESH_AUTHENTICATED_UI'
 return snapshot
def cookie_window(text):
 match=re.search(r'\b(\d{1,4})\s*[- ]?day(?:s)?\s+(?:cookie|attribution|tracking)',str(text or ''),re.I)
 return f"{int(match.group(1))} days" if match else ''
def approval_relationship_truth_only(candidate,snapshot,approval_classification,gate):
 """Allow a verified approval fact to land without admitting a parked offer.

 Candidate-fit gates decide whether an offer may enter GLP discovery/wiring. They
 must not erase an authenticated relationship that already exists at a provider.
 The caller keeps the parked decision as an advisory and disables tracking/wire
 promotion while it records the relationship and clears the mailbox event.
 """
 return bool(
  not gate.get('operational')
  and candidate.get('action')=='relationship_approval'
  and snapshot.get('verified')
  and snapshot.get('relationship') in {'joined','approved','active'}
  and isinstance(approval_classification,dict)
  and isinstance(approval_classification.get('classification'),dict)
 )

def desired_changes(row,snap,received_at,action=None,signal_text='',allow_tracking=True):
 approved=snap['relationship'] in {'joined','approved'} and bool(snap.get('provider_readback_verified',snap.get('verified',True))); pending=snap['relationship']=='pending'; changes={}; invited_status='invited_pending_acceptance' if bool(row[1]) else 'invited_not_joined'; status='approved_active_not_wired' if approved else ('applied_pending_brand_approval' if pending else (invited_status if action=='relationship_invitation' and snap['relationship']=='notjoined' else ('application_rejected' if snap['relationship']=='rejected' else f"provider_{snap['relationship']}")))
 if not row[6] and str(snap.get('official_url') or '').startswith('https://'):changes[6]=snap['official_url']
 if approved and str(row[11]).upper()!='TRUE':changes[11]=True
 if not row[18] and approved:changes[18]=date_serial(received_at)
 tracking=row[14] if allow_tracking else ''
 if approved and not tracking and allow_tracking:
  tracking=snap.get('tracking_link') or (str((snap.get('details') or {}).get('clickThroughUrl') or '') if snap['provider']=='Awin' else '') or (awin_link(snap['program_id'],row[6]) if snap['provider']=='Awin' and str(row[6]).startswith('http') else '')
  if tracking:changes[14]=tracking;changes[21]='not_clicked_preserve_attribution'
 terms=format_groups(snap.get('groups',[])) if snap['provider']=='Awin' else snap.get('terms','')
 monetizable=not (snap['provider']=='Partnerize' and approved and not terms)
 wire=bool(allow_tracking and approved and tracking and monetizable)
 if approved:status=('approved_active_tracking_ready' if wire else ('approved_no_positive_commission_configured' if not monetizable else 'approved_active_not_wired'))
 if row[10]!=status:changes[10]=status
 if str(row[19]).upper()!=str(wire).upper():changes[19]=wire
 if terms and row[16]!=terms:changes[16]=terms
 cookie=snap.get('cookie_window') or cookie_window(signal_text)
 if cookie and row[17]!=cookie:changes[17]=cookie
 if changes:changes[24]=now()
 return changes

def healthtrader_account_changes(row,changes):
 """Normalize the network rail without downgrading it to a brand-offer status."""
 changes=dict(changes)
 if row[10]=='approved_marketplace_access':changes.pop(10,None)
 else:changes[10]='approved_marketplace_access'
 if str(row[11]).upper()=='TRUE':changes.pop(11,None)
 else:changes[11]=True
 if not any(column!=24 for column in changes):changes.pop(24,None)
 return changes
def transition_key(row,col,old,new,program,network,publisher): return sha('|'.join(('glp-affiliate-reconcile:v1',SHEET_ID,TAB,network,str(publisher),str(program),str(col),sha(old),sha(new))))
def save_receipt(event,payload):
 REPORT_DIR.mkdir(parents=True,exist_ok=True); p=REPORT_DIR/f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{event}.json";p.write_text(json.dumps(payload,indent=2)+'\n');os.chmod(p,0o600);return p
def mark_event_read(gmail,event_id,action_receipt):
 result=mark_read_after_action(gmail=gmail,db_path=INBOX_DB,event_id=event_id,action_receipt_path=action_receipt,retries=3)
 p=REPORT_DIR/f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{event_id}-mark-read.json";save_mark_read_receipt(p,result);return p
def finalize_mailbox_completion(gmail,event_id,action_receipt,approval=False):
 mark_path=mark_event_read(gmail,event_id,action_receipt);mark_payload=json.loads(mark_path.read_text())
 cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
 try:
  ledger.record_affiliate_mailbox_read(event_id,{'mark_read_receipt_path':str(mark_path),'mark_read_receipt_sha256':hashlib.sha256(mark_path.read_bytes()).hexdigest(),'provider_readback_verified':mark_payload.get('status') in {'PASS','ALREADY_READ'}})
  completion=ledger.complete_affiliate_approval_event(event_id) if approval else None
 finally:ledger.close()
 return mark_path,completion
def load_control_plane():
 import importlib.util
 p=BUSINESS/'inbox_control_plane/control_plane.py';spec=importlib.util.spec_from_file_location('cp_auto',p);m=importlib.util.module_from_spec(spec);assert spec and spec.loader;sys.modules['cp_auto']=m;spec.loader.exec_module(m);return m
def mark_state(db,event,status,blocker=None,receipt=None): db.execute('INSERT INTO events(event_id,status,blocker_hash,receipt_path,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET status=excluded.status,blocker_hash=excluded.blocker_hash,receipt_path=COALESCE(excluded.receipt_path,events.receipt_path),updated_at=excluded.updated_at',(event,status,sha(blocker) if blocker else None,str(receipt) if receipt else None,now()));db.commit()

def direct_partner_changes(row,candidate):
 negative=str(candidate.get('relationship') or candidate.get('provider_relationship') or '').strip().lower() in {'rejected','declined','not_approved','not approved'} or candidate.get('approved') is False
 desired={
  10:('manual_attribution_provider_not_approved' if negative else 'approved_manual_attribution_pending_terms_and_tracking_url'),
  16:'Manual attribution confirmed; commission terms and direct tracking URL pending.',
 }
 changes={column:value for column,value in desired.items() if str(row[column])!=str(value)}
 if not str(row[21]).strip():changes[21]='not_tested_no_link'
 if changes:changes[24]=now()
 if not set(changes).issubset(OPERATOR_EVIDENCE_COLUMNS):raise RuntimeError('direct_partner_column_scope_violation')
 return changes

def bind_direct_partner_row(values,candidate):
 spec=DIRECT_PARTNER_PROGRAMS[candidate['network']];matches=[]
 for number,raw in enumerate(values[1:],2):
  row=pad(raw)
  if norm(row[2])==norm(spec['program']) and row[4] in spec['routes']:matches.append((number,row))
 if len(matches)!=1:raise RuntimeError(f"ambiguous_direct_partner_sheet_identity:{candidate['network']}:matches={len(matches)}")
 return matches[0]

def reconcile_direct_partner_event(event,candidate,svc,gmail,state,dry_run):
 values=execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE'),'sheet_direct_partner_full_read').get('values',[])
 if not values or pad(values[0])!=HEADERS:raise RuntimeError('canonical_sheet_header_mismatch')
 row_num,row=bind_direct_partner_row(values,candidate);changes=direct_partner_changes(row,candidate);before_hash=sha(json.dumps(row,sort_keys=False));keys=[]
 gate=choke_candidate({'display_name':row[2],'network':candidate.get('network'),'program_id':row[2],'description':row[3],'category':row[3],'region':row[25],'application_url':row[5]},require_application_url=True,operation='affiliate_approval_auto_reconciler.direct_partner_write',persist=not dry_run)
 if not gate['operational']:raise AffiliateCandidateParked(gate['decision'],gate['reason_codes'])
 if changes and not dry_run:
  with sheet_write_lock():
   schema_repair=repair_non_value_schema_issues(svc,SHEET_ID,TAB);require_checkbox_schema_pass(svc,SHEET_ID,TAB)
   metadata=execute_google_request(svc.spreadsheets().get(spreadsheetId=SHEET_ID,fields='sheets(properties(sheetId,title))'),'sheet_direct_partner_metadata_read')
   sheet_id=next(int(item['properties']['sheetId']) for item in metadata['sheets'] if item['properties']['title']==TAB)
   current=pad(execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!A{row_num}:AA{row_num}",valueRenderOption='UNFORMATTED_VALUE'),'sheet_direct_partner_cas_read').get('values',[[]])[0])
   if sha(json.dumps(current,sort_keys=False))!=before_hash:raise RuntimeError('sheet_compare_and_swap_failed')
   data=[]
   for column,new in changes.items():
    key=operator_transition_key(event['event_id'],row_num,column,row[column],new);keys.append(key)
    state.execute('INSERT OR IGNORE INTO transitions(transition_key,event_id,row_number,column_index,old_sha256,new_sha256,status,created_at) VALUES(?,?,?,?,?,?,?,?)',(key,event['event_id'],row_num,column,sha(row[column]),sha(new),'planned',now()))
    data.append({'range':a1(row_num,column),'values':[[new]]})
   require_marketplace_parent_rows_not_mutated((row_num, column) for column in changes)
   state.commit();execute_google_request(svc.spreadsheets().values().batchUpdate(spreadsheetId=SHEET_ID,body={'valueInputOption':'RAW','data':data}),'sheet_direct_partner_batch_update')
   reread=pad(execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!A{row_num}:AA{row_num}",valueRenderOption='UNFORMATTED_VALUE'),'sheet_direct_partner_post_read').get('values',[[]])[0])
   if any(str(reread[column])!=str(value) for column,value in changes.items()):raise RuntimeError('sheet_direct_partner_post_write_readback_failed')
   if any(reread[column]!=row[column] for column in set(range(len(HEADERS)))-set(changes)):raise RuntimeError('sheet_direct_partner_non_target_drift')
   require_checkbox_schema_pass(svc,SHEET_ID,TAB);state.executemany("UPDATE transitions SET status='applied',applied_at=? WHERE transition_key=?",[(now(),key) for key in keys]);state.commit()
 else:
  schema_repair=None
 receipt={'status':'PASS','event_id':event['event_id'],'network':candidate['network'],'program':DIRECT_PARTNER_PROGRAMS[candidate['network']]['program'],'row_number':row_num,'signal':candidate['action'],'action_kind':'direct_partner_evidence_reconciliation','action_completed':True,'provider_readback_verified':False,'relationship_claim':'manual_attribution_confirmed_by_source_email; provider_terms_and_tracking_readback_required','sheet_readback_verified':True,'sheet_schema_contract_verified':True,'schema_repair':schema_repair,'changed_columns':[ALLOWED[column] for column in changes],'sheet_write_count':0 if dry_run else len(changes),'dry_run':dry_run,'external_send':False,'outbound_send':False,'provider_mutation':False,'provider_program_mutation':False,'tracking_link_generated':False,'archive':False,'delete':False,'provider_draft':False,'affiliate_redirect_clicks':0,'approval_date_written':False,'tracking_link_written':False,'wire_ready_written':False}
 path=save_receipt(event['event_id'],receipt);mark_path=None
 if dry_run:mark_state(state,event['event_id'],'dry_run_operator_evidence',receipt=path)
 else:
  cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
  try:ledger.reconcile_operator_evidence(event['event_id'],automation_id=AUTOMATION_ID,authority_message_id=AUTHORITY_MESSAGE,reconciliation_receipt=receipt)
  finally:ledger.close()
  mark_path,_=finalize_mailbox_completion(gmail,event['event_id'],path);mark_state(state,event['event_id'],'pass',receipt=path)
 return {'event_id':event['event_id'],'network':candidate['network'],'program_id':None,'row':row_num,'changes':[ALLOWED[column] for column in changes],'receipt':str(path),'mark_read_receipt':str(mark_path) if mark_path else None,'provider_readback_verified':False,'operator_evidence_only':True}


def reconcile_direct_operator_event(event,candidate,svc,gmail,state,dry_run,handoff):
 authority=validate_operator_handoff_event(handoff,event)
 values=execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE'),'sheet_direct_operator_full_read').get('values',[])
 if not values or pad(values[0])!=HEADERS:raise RuntimeError('canonical_sheet_header_mismatch')
 row_num,row=bind_direct_operator_row(values,candidate);changes=direct_operator_changes(row,candidate);before_hash=sha(json.dumps(row,sort_keys=False));keys=[]
 gate=choke_candidate({'display_name':row[2],'network':candidate.get('network'),'program_id':row[2],'description':row[3],'category':row[3],'region':row[25],'application_url':row[5]},require_application_url=True,operation='affiliate_approval_auto_reconciler.direct_operator_write',persist=not dry_run)
 if not gate['operational']:raise AffiliateCandidateParked(gate['decision'],gate['reason_codes'])
 if changes and not dry_run:
  with sheet_write_lock():
   repair_non_value_schema_issues(svc,SHEET_ID,TAB);require_checkbox_schema_pass(svc,SHEET_ID,TAB)
   metadata=execute_google_request(svc.spreadsheets().get(spreadsheetId=SHEET_ID,fields='sheets(properties(sheetId,title))'),'sheet_direct_operator_metadata_read')
   sheet_id=next(int(item['properties']['sheetId']) for item in metadata['sheets'] if item['properties']['title']==TAB)
   current=pad(execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!A{row_num}:AA{row_num}",valueRenderOption='UNFORMATTED_VALUE'),'sheet_direct_operator_cas_read').get('values',[[]])[0])
   if sha(json.dumps(current,sort_keys=False))!=before_hash:raise RuntimeError('sheet_compare_and_swap_failed')
   data=[]
   for column,new in changes.items():
    key=operator_transition_key(event['event_id'],row_num,column,row[column],new);keys.append(key)
    state.execute('INSERT OR IGNORE INTO transitions(transition_key,event_id,row_number,column_index,old_sha256,new_sha256,status,created_at) VALUES(?,?,?,?,?,?,?,?)',(key,event['event_id'],row_num,column,sha(row[column]),sha(new),'planned',now()))
    data.append({'range':a1(row_num,column),'values':[[new]]})
   require_marketplace_parent_rows_not_mutated((row_num, column) for column in changes)
   state.commit();execute_google_request(svc.spreadsheets().values().batchUpdate(spreadsheetId=SHEET_ID,body={'valueInputOption':'RAW','data':data}),'sheet_direct_operator_batch_update')
   execute_google_request(svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID,body={'requests':schema_requests(sheet_id,[row_num])}),'sheet_direct_operator_schema_write')
   reread=pad(execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!A{row_num}:AA{row_num}",valueRenderOption='UNFORMATTED_VALUE'),'sheet_direct_operator_post_read').get('values',[[]])[0])
   if any(str(reread[column])!=str(value) for column,value in changes.items()):raise RuntimeError('sheet_direct_operator_post_write_readback_failed')
   if any(reread[column]!=row[column] for column in set(range(len(HEADERS)))-set(changes)):raise RuntimeError('sheet_direct_operator_non_target_drift')
   require_checkbox_schema_pass(svc,SHEET_ID,TAB);state.executemany("UPDATE transitions SET status='applied',applied_at=? WHERE transition_key=?",[(now(),key) for key in keys]);state.commit()
 receipt={'status':'PASS','event_id':event['event_id'],'network':candidate['network'],'program':DIRECT_OPERATOR_PROGRAMS[candidate['network']]['program'],'row_number':row_num,'action_kind':'operator_evidence_sheet_status','action_completed':True,'operator_authority_message_id':authority,'operator_handoff_path':handoff['_path'],'operator_handoff_sha256':handoff['_sha256'],'provider_readback_verified':False,'relationship_claim':'not_made_provider_readback_required','sheet_readback_verified':True,'sheet_schema_contract_verified':True,'changed_columns':[ALLOWED[column] for column in changes],'sheet_write_count':0 if dry_run else len(changes),'dry_run':dry_run,'external_send':False,'outbound_send':False,'provider_mutation':False,'provider_program_mutation':False,'tracking_link_generated':False,'archive':False,'delete':False,'provider_draft':False,'affiliate_redirect_clicks':0,'approval_date_written':False,'tracking_link_written':False,'wire_ready_written':False,'wired_live_written':False}
 path=save_receipt(event['event_id'],receipt);mark_path=None
 if dry_run:mark_state(state,event['event_id'],'dry_run_operator_evidence',receipt=path)
 else:
  mark_path=mark_event_read(gmail,event['event_id'],path);mark_state(state,event['event_id'],'pass_operator_evidence',receipt=path)
 return {'event_id':event['event_id'],'network':candidate['network'],'program_id':None,'row':row_num,'changes':[ALLOWED[column] for column in changes],'receipt':str(path),'mark_read_receipt':str(mark_path) if mark_path else None,'provider_readback_verified':False,'operator_evidence_only':True}

def select_events_for_run(events,state,limit=20):
 candidates=[]
 for event in events:
  recovery=any(bool(event[key]) for key in ('mailbox_read_recovery','missing_approval_fact_recovery') if key in event.keys())
  if str(event['state']) in TERMINAL_EVENT_STATES and not recovery:continue
  prior=state.execute('SELECT * FROM events WHERE event_id=?',(event['event_id'],)).fetchone()
  if recovery:
   candidates.append((-1,str(event['received_at']),str(event['event_id']),event,prior));continue
  if prior and prior['status'] in {'pass','parked','operator_action'}:continue
  if prior and prior['status']=='ignored' and str(event['canonical_category'] or '')!='affiliate_network_response':continue
  status=str(prior['status'] if prior else '')
  priority=0 if not prior or status.startswith('dry_run') else 1
  last_attempt=prior['updated_at'] if prior and 'updated_at' in prior.keys() else None
  retry_order=last_attempt if status=='blocked' and last_attempt else event['received_at']
  candidates.append((priority,str(retry_order),str(event['event_id']),event,prior))
 candidates.sort(key=lambda item:item[:3])
 return [(event,prior) for _,_,_,event,prior in candidates[:limit]]
def render_discord(outputs):
 passed=[x for x in outputs if x['status']=='PASS'];blocked=[x for x in outputs if x['status']=='BLOCKED']
 if not blocked:return ''
 verb='needs' if len(blocked)==1 else 'need'
 details='\n'.join(f"• {x.get('network') or 'unknown network'} — {re.sub(r'[^ -~]','',str(x.get('subject') or x['event_id']))[:100]} — {str(x['blocker'])[:180]}" for x in blocked)
 message=discord_fmt.render(discord_fmt.title('warn','Affiliate Reconciler',f"{len(blocked)} email{'s' if len(blocked)!=1 else ''} {verb} operator or provider repair"),details,discord_fmt.kvline(('automatic updates completed',len(passed)),('blocked',len(blocked))),discord_fmt.meaning('the automatic path stayed fail-closed; mailbox disposition was left unchanged and no Sheet guess was made'),discord_fmt.subtext(f'artifacts: {REPORT_DIR}'),output_class='alert')
 check=discord_fmt.lint(message,output_class='alert')
 if check['status']!='pass':raise RuntimeError(f"discord_output_lint_failed:{check['errors']}")
 return message
def validate_terminal_routine_recovery(event,state,candidate):
 if str(event['state'])!='resolved_externally':raise RuntimeError('terminal_recovery_event_state_invalid')
 prior=state.execute('SELECT status,receipt_path FROM events WHERE event_id=?',(event['event_id'],)).fetchone()
 if not prior or prior['status']!='pass' or not prior['receipt_path']:raise RuntimeError('terminal_recovery_prior_state_invalid')
 path=Path(prior['receipt_path'])
 if not path.is_file():raise RuntimeError('terminal_recovery_prior_receipt_missing')
 receipt=json.loads(path.read_text())
 db=sqlite3.connect(f'file:{INBOX_DB}?mode=ro',uri=True);db.execute('PRAGMA query_only=ON')
 try:
  archived=db.execute("SELECT COUNT(*) FROM mailbox_actions WHERE event_id=? AND action='affiliate_information_archived' AND status='verified'",(event['event_id'],)).fetchone()[0]
  reconciled=db.execute("SELECT COUNT(*) FROM mailbox_actions WHERE event_id=? AND action='affiliate_sheet_reconciled' AND status='verified'",(event['event_id'],)).fetchone()[0]
 finally:db.close()
 if archived==1 and reconciled==1:
  if (receipt.get('status')!='PASS' or receipt.get('provider_readback_verified') is not True
      or receipt.get('sheet_readback_verified') is not True or not receipt.get('terminal_routine_recovery')):
   raise RuntimeError('terminal_recovery_replay_receipt_invalid')
  return {'already_reconciled':True,'receipt_path':str(path),'receipt_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'prior_action':'affiliate_sheet_reconciled'}
 if archived==1 and reconciled==0 and candidate.get('action')=='routine_information':
  if (receipt.get('status')!='PASS' or receipt.get('action_kind')!='routine_affiliate_information'
      or int(receipt.get('sheet_write_count') or 0)!=0 or receipt.get('provider_readback_verified') is not False
      or receipt.get('sheet_readback_verified') is not False or receipt.get('provider_mutation') is not False):
   raise RuntimeError('terminal_recovery_prior_receipt_not_routine_zero_effect')
  return {'already_reconciled':True,'receipt_path':str(path),'receipt_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'prior_action':'affiliate_information_archived'}
 if archived!=1 or reconciled!=0:raise RuntimeError('terminal_recovery_control_plane_state_invalid')
 if (receipt.get('status')!='PASS' or receipt.get('action_kind')!='routine_affiliate_information'
     or int(receipt.get('sheet_write_count') or 0)!=0 or receipt.get('provider_readback_verified') is not False
     or receipt.get('sheet_readback_verified') is not False or receipt.get('provider_mutation') is not False):
  raise RuntimeError('terminal_recovery_prior_receipt_not_routine_zero_effect')
 return {'prior_receipt_path':str(path),'prior_receipt_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'prior_action':'affiliate_information_archived'}

def verify_affiliate_authentication(event,network):
 path=Path(str(event['raw_path'] or ''))
 if not path.is_file():return {'status':'BLOCKED','reason':'affiliate_raw_message_missing'}
 raw=path.read_bytes(); expected_hash=str(event['raw_sha256'] or '') if 'raw_sha256' in event.keys() else ''
 if expected_hash and hashlib.sha256(raw).hexdigest()!=expected_hash:return {'status':'BLOCKED','reason':'affiliate_raw_message_hash_mismatch'}
 msg=BytesParser(policy=policy.default).parsebytes(raw)
 sender=parseaddr(str(msg.get('From') or ''))[1].lower(); sender_domain=sender.rsplit('@',1)[1] if '@' in sender else ''
 allowed=AFFILIATE_AUTH_DOMAINS.get(str(network),())
 if not allowed or not any(sender_domain==domain or sender_domain.endswith('.'+domain) for domain in allowed):
  return {'status':'BLOCKED','reason':'affiliate_sender_domain_untrusted'}
 trusted=[str(value).lower() for value in (msg.get_all('Authentication-Results') or []) if 'mx.google.com' in str(value).lower()]
 auth=' ; '.join(trusted)
 dmarc_pass=bool(re.search(r'\bdmarc\s*=\s*pass\b',auth))
 aligned_dkim=bool(re.search(r'\bdkim\s*=\s*pass\b',auth)) and any(domain in auth for domain in allowed)
 if not (dmarc_pass or aligned_dkim):return {'status':'BLOCKED','reason':'affiliate_sender_authentication_missing_or_failed'}
 return {'status':'PASS','sender_domain':sender_domain,'dmarc_pass':dmarc_pass,'aligned_dkim_pass':aligned_dkim}

#: Writer refusals that mean "a DIFFERENT machine owns the next step", not
#: "this chain failed".
#:
#: THE RUNAWAY, 2026-09-02 (approvals backfill). `maxbounty_inventory` refuses
#: `maxbounty_inventory_preserved_wire_ready_required` whenever the row's
#: `Wire-In Ready?` checkbox is not TRUE. That checkbox is opened by a different
#: machine entirely - `glp_wire_in_ready_queue`, which structurally validates the
#: freshly minted link first - so at the moment the approval chain drains its own
#: rows the refusal is not merely expected, it is guaranteed for any programme
#: whose link was just re-minted.
#:
#: Treating it as a hard failure made the whole event fail, so the registered
#: reconciler (cron b0c800751b88, every 10 minutes) retried it, and every retry
#: MINTED A NEW TRACKING LINK. Applying that new link resets `Wire-In Ready?` to
#: FALSE, which guarantees the same refusal on the next pass. Five events ran
#: 12-13 times each overnight - roughly 65 unnecessary provider link mints - and
#: rows 709, 717, 723, 726 and 730 could never *stay* wire-in-ready, because the
#: loop reset the gate faster than the producer could open it.
#:
#: So this ONE named reason is a deferral: the queue row is left `queued` so the
#: next drain applies it once the gate is open (which is exactly what happened
#: for rows 707/710/711/718/722/728), and the event is allowed to complete.
#: Every other refusal still raises. Nothing is written that was not written
#: before, and no guard is weakened - the writer's check is untouched.
DEFERRED_DRAIN_REASONS=('maxbounty_inventory_preserved_wire_ready_required',)

def deferred_drain_reason(result):
 """Return the deferral reason this refusal carries, or '' if it is a failure."""
 if str((result or {}).get('status') or '').lower()!='refused':return ''
 reason=str((result or {}).get('reason') or '')
 for token in DEFERRED_DRAIN_REASONS:
  if token in reason:return token
 return ''

def approval_test_label(record):
 """Explicit TEST provenance only; absence does not establish a real approval."""
 record=dict(record)
 for key in ('is_test','test_mode','synthetic'):
  if str(record.get(key) or '').strip().lower() in {'true','1','yes','test'}:return True
 for key in ('program_name','advertiser_name','subject','note'):
  if re.search(r'(?:^\s*TEST\b|\[TEST\])',str(record.get(key) or ''),re.I):return True
 evidence=record.get('evidence')
 if isinstance(evidence,str):
  try:evidence=json.loads(evidence)
  except ValueError:evidence=None
 return approval_test_label(evidence) if isinstance(evidence,dict) else False

def drain_single_writer_queue_row(queue_row,dry_run):
 if approval_test_label(queue_row):raise RuntimeError('sheet_queue_test_excluded')
 if dry_run:return {'status':'dry_run','queued':False,'queue_row':queue_row,'cells_written':0}
 existing=n8n_dt.rows(SHEET_UPDATE_QUEUE,n8n_dt.eq('update_id',str(queue_row['update_id'])))
 if len(existing)>1:raise RuntimeError('sheet_queue_identity_ambiguous')
 if existing:
  queue_row=sorted(existing,key=lambda item:str(item.get('update_id') or ''))[0];inserted=False
  if approval_test_label(queue_row):raise RuntimeError('sheet_queue_test_excluded')
  terminal=str(queue_row.get('status') or '').lower()
  if terminal in {'applied','noop'}:
   return {'status':terminal,'queued':True,'inserted':False,
           'update_id':queue_row['update_id'],'cells_written':0,
           'receipt':queue_row.get('receipt'),'reason':'queue_already_terminal'}
  if terminal!='queued':raise RuntimeError('sheet_queue_nonqueued_state')
 else:
  try:n8n_dt.insert(SHEET_UPDATE_QUEUE,queue_row);inserted=True
  except Exception:
   landed=n8n_dt.rows(SHEET_UPDATE_QUEUE,n8n_dt.eq('update_id',str(queue_row['update_id'])))
   if len(landed)!=1:raise
   queue_row=landed[0];inserted=False
 completed=run_sheet_drain(queue_row)
 if completed.returncode!=0:raise RuntimeError(f'sheet_queue_drain_failed:{completed.returncode}:{(completed.stderr or completed.stdout)[-500:]}')
 try:summary=json.loads((completed.stdout or '').strip().splitlines()[-1])
 except (ValueError,IndexError) as exc:raise RuntimeError('sheet_queue_drain_output_invalid') from exc
 results=summary.get('results') or []
 if len(results)==1:
  deferral=deferred_drain_reason(results[0])
  if deferral:
   # Leave the row `queued`. The gate this waits on is opened by another
   # machine, and the next drain applies the row unchanged.
   return {'status':'deferred','queued':True,'inserted':inserted,
           'update_id':queue_row['update_id'],'cells_written':0,
           'receipt':results[0].get('receipt'),'reason':results[0].get('reason'),
           'deferred_to':deferral}
 if len(results)!=1 or results[0].get('status') not in {'applied','noop'}:
  raise RuntimeError(f"sheet_queue_drain_not_terminal:{json.dumps(results,sort_keys=True)[:500]}")
 result=results[0]
 n8n_dt.update(SHEET_UPDATE_QUEUE,n8n_dt.eq('update_id',str(queue_row['update_id'])),{
  'status':result['status'],'resolved_at_utc':now(),'note':str(result.get('reason') or queue_row.get('note') or '')[:900],
 })
 return {'status':result['status'],'queued':True,'inserted':inserted,
         'update_id':queue_row['update_id'],'cells_written':int(result.get('cells_written') or 0),
         'receipt':result.get('receipt'),'reason':result.get('reason')}

def bind_payout_offer_row(values,candidate):
 matches=[]
 if candidate['network']=='Katalys':
  ids={int(value) for value in candidate.get('program_ids') or []}
  for number,raw in enumerate(values[1:],2):
   row=pad(raw)
   if row[4]=='Katalys' and program_id(row,'Katalys') in ids:matches.append((number,row,program_id(row,'Katalys')))
 elif candidate['network']=='XenTraffic':
  ids={str(value) for value in candidate.get('program_ids') or [] if str(value).strip()}
  if len(ids)!=1:raise RuntimeError('xentraffic_payout_offer_requires_exact_offer_id')
  offer_id=next(iter(ids))
  for number,raw in enumerate(values[1:],2):
   row=pad(raw)
   if norm(row[2])=='xentraffic' and norm(row[4])=='networksignup':matches.append((number,row,offer_id))
 if len(matches)!=1:raise RuntimeError(f"ambiguous_sheet_identity:{candidate['network']}:matches={len(matches)}")
 return matches[0]

def payout_offer_capture(event,candidate,row_number,row,program):
 raw_path=Path(str(event['raw_path']));raw_sha=hashlib.sha256(raw_path.read_bytes()).hexdigest()
 capture={
  'schema':'affiliate_authenticated_payout_offer_notice_v1','captured_at_utc':now(),
  'event_id':str(event['event_id']),'raw_message_sha256':raw_sha,
  'received_at_utc':str(event['received_at']),'network':candidate['network'],
  'advertiser_name':candidate.get('advertiser_name'),'sheet_row':row_number,
  'sheet_program_name':row[2],'network_program_id':str(program),
  'offer_terms':candidate['offer_terms'],'offer_url':candidate.get('offer_url'),
  'source':'authenticated_network_email','provider_mutation':False,'sheet_write':False,
 }
 directory=BUSINESS/'reports/glp-clinic-scout/affiliate-machine/APPLIER-RETIRE-20260824/offer-notices'
 suffix=f'-offer-{program}' if candidate['network']=='XenTraffic' else ''
 path=directory/f"{event['event_id']}{suffix}.json"
 if path.is_file():
  existing=json.loads(path.read_text())
  expected={key:value for key,value in capture.items() if key!='captured_at_utc'}
  actual={key:value for key,value in existing.items() if key!='captured_at_utc'}
  if actual!=expected:raise RuntimeError('payout_offer_capture_binding_conflict')
  return path,existing
 path=approval_atomic_json(path,capture)
 return path,capture

def payout_offer_queue_row(event,candidate,row_number,row,program,capture_path):
 relative=str(capture_path.relative_to(BUSINESS));digest=hashlib.sha256(capture_path.read_bytes()).hexdigest()
 capture=json.loads(capture_path.read_text())
 incumbent=str(row[16] or '').strip() or 'not recorded in Sheet'
 if candidate['network']=='Katalys':
  decision='highest_observed_offer_wins; re-point deferred until site deployment and exact offer eligibility plus link validation'
 else:
  decision='review candidate only; no application, tracking change, or re-point authorized'
 evidence={
  'payout_evaluation_path':relative,'payout_evaluation_sha256':digest,
  'evaluated_at_utc':str(capture['captured_at_utc']),'provider_identity':f"{candidate.get('advertiser_name')}:{program}",
  'basis':'headline','formula':'highest payout per qualified lead wins',
  'incumbent_effective_value':incumbent,'challenger_effective_value':candidate['offer_terms'],
  'decision':decision,
 }
 update_id=(f"payout-offer-{event['event_id']}-offer-{program}"
            if candidate['network']=='XenTraffic' else f"payout-offer-{event['event_id']}")
 return {
  'update_id':update_id,'kind':'payout_evaluation',
  'network':str(row[4]),'program_id':str(program),'program_name':str(row[2]),
  'sheet_row':str(row_number),'evidence':json.dumps(evidence,sort_keys=True),
  'status':'queued','queued_by':'affiliate_approval_auto_reconciler',
  'queued_at_utc':now(),'resolved_at_utc':'',
  'note':f"Authenticated {candidate['network']} payout offer for {candidate.get('advertiser_name')} offer {program} bound from {event['event_id']}; No application, link, deployment, or re-point executed.",
 }

def write_coreage_decision_card(event,candidate,row_number,row,queue_receipt):
 if candidate['network']!='Katalys':return None
 directory=BUSINESS/'reports/glp-clinic-scout/affiliate-machine/APPLIER-RETIRE-20260824'
 card=("💵 **CoreAge Rx payout decision** — highest payout per lead wins\n\n"
       f"current Sheet terms: **{str(row[16] or 'not recorded')}** · Katalys offer: **{candidate['offer_terms']}**\n"
       "Decision: record the Katalys offer as the winning observed rate. Keep the live link unchanged now; "
       "re-point only with the next site deployment after exact offer eligibility and challenger-link validation.\n\n"
       "No application, tracking change, affiliate deploy, or re-point executed.")
 payload={'schema':'coreage-rx-payout-decision-card-v1','status':'REVIEW_RECORDED','event_id':event['event_id'],
          'sheet_row':row_number,'current_sheet_terms':str(row[16] or ''),'challenger_terms':candidate['offer_terms'],
          'decision':'highest_payout_per_lead_wins_repoint_at_next_site_deployment','queue_receipt':queue_receipt,
          'card':card,'effects':{'applications':0,'deploys':0,'link_changes':0}}
 return approval_atomic_json(directory/'COREAGE-RX-PAYOUT-DECISION.json',payload)

def reconcile_campaign_cancellation(event,candidate,svc,gmail,state,dry_run):
 """A notice is not withdrawal authority; require bound provider verification."""
 auth=verify_affiliate_authentication(event,'MaxBounty')
 if auth['status']!='PASS':raise RuntimeError(auth['reason'])
 if 'provider_verification' not in event.keys() or not event['provider_verification']:
  receipt={'status':'BLOCKED','event_id':str(event['event_id']),'signal':'campaign_cancellation','disposition':'requires_provider_verification','action_completed':False,'sheet_write_count':0,'sheet_readback_verified':False,'provider_readback_verified':False,'outbound_send':False,'dry_run':dry_run}
  path=save_receipt(event['event_id'],receipt)
  mark_state(state,event['event_id'],'dry_run_blocked' if dry_run else 'blocked',blocker='requires_provider_verification',receipt=path)
  return {'status':'BLOCKED','event_id':event['event_id'],'receipt':str(path),'disposition':'requires_provider_verification','changes':[]}
 values=execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE'),'sheet_cancellation_read').get('values',[])
 if not values or pad(values[0])!=HEADERS:raise RuntimeError('canonical_sheet_header_mismatch')
 matches=[(number,pad(raw)) for number,raw in enumerate(values[1:],2) if str(pad(raw)[4])=='MaxBounty' and norm(pad(raw)[2])==norm(candidate['program_name'])]
 if len(matches)>1:raise RuntimeError('campaign_cancellation_sheet_identity_ambiguous')
 if not matches and candidate['program_name']!='Roman Health - Erectile Dysfunction - CPL (US)':raise RuntimeError('campaign_cancellation_sheet_identity_missing')
 row_number,row=matches[0] if matches else (None,None)
 program=None;queue_receipt=None
 capture={'schema':'affiliate_campaign_cancellation_v1','event_id':str(event['event_id']),'network':'MaxBounty','program_name':candidate['program_name'],'observed_at_utc':str(event['received_at']),'effective_at_network_time':candidate['effective_at_network_time'],'raw_sha256':sha_bytes(Path(str(event['raw_path'])).read_bytes()),'authenticated_network_notice_verified':True}
 capture_path=REPORT_DIR/'cancellation-notices'/f"{event['event_id']}.json"
 if row is not None:
  match=re.search(r'/campaign/(\d+)(?:[/?#]|$)',str(row[5]))
  if not match:raise RuntimeError('campaign_cancellation_campaign_id_missing')
  program=match.group(1);capture.update(program_id=program,sheet_row=row_number)
  if not dry_run:approval_atomic_json(capture_path,capture)
  raw_path=Path(str(event['raw_path']))
  evidence={'event_id':str(event['event_id']),'decision_class':'cancelled','path':str(raw_path.relative_to(BUSINESS)),'raw_sha256':sha_bytes(raw_path.read_bytes()),'received_at_utc':str(event['received_at']),'event_at_utc':str(event['received_at']),'provider_verification':event['provider_verification'],'effective_at_network_time':candidate['effective_at_network_time']}
  import affiliate_program_withdrawal as withdrawal
  withdrawal.verify({'network':'MaxBounty','program_id':program,'program_name':str(row[2]),'evidence':evidence},root=BUSINESS)
  queue_row={'update_id':f"network-cancelled-{program}-{event['event_id']}",'kind':'network_cancelled','network':'MaxBounty','program_id':program,'program_name':str(row[2]),'sheet_row':str(row_number),'evidence':json.dumps(evidence,sort_keys=True),'status':'queued','queued_by':'affiliate_approval_auto_reconciler','queued_at_utc':now(),'resolved_at_utc':'','note':f"Campaign cancellation notice {event['event_id']}; network expiry {candidate['effective_at_network_time']}; park from deployment, no reply."}
  queue_receipt=drain_single_writer_queue_row(queue_row,dry_run)
 disposition='cancelled_by_network' if row is not None else 'informational-closed'
 receipt={**capture,'status':'PASS','signal':'campaign_cancellation','action_kind':'campaign_cancellation','action_completed':not dry_run,'disposition':disposition,'row_number':row_number,'program_id':program,'sheet_queue_receipt':queue_receipt,'sheet_readback_verified':bool(row is not None and not dry_run),'sheet_write_count':int((queue_receipt or {}).get('cells_written') or 0),'annotation':'Network cancellation notice; generic draft must not be sent. '+('Campaign parked from deployment.' if row is not None else 'No GLP row; informational-closed, no row created.'),'dry_run':dry_run,'provider_readback_verified':False,'external_send':False,'outbound_send':False,'provider_mutation':False,'provider_program_mutation':False,'tracking_link_generated':False,'affiliate_redirect_clicks':0}
 path=save_receipt(event['event_id'],receipt)
 mark_state(state,event['event_id'],'dry_run_campaign_cancellation' if dry_run else disposition,receipt=path)
 if not dry_run:
  cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
  try:
   ledger.audit('campaign_cancellation_annotated','inbox_event',event['event_id'],{'disposition':disposition,'annotation':receipt['annotation'],'receipt_path':str(path),'outbound_send':False})
   if str(event['state']) not in TERMINAL_EVENT_STATES:
    # Archive the notice only after the separately receipted single writer is done.
    archival={**receipt,'action_kind':'routine_affiliate_information','action_kind_detail':'campaign_cancellation','sheet_write_count':0,'single_writer_sheet_write_count':receipt['sheet_write_count'],'cancellation_receipt_path':str(path)}
    ledger.resolve_automatically(event['event_id'],automation_id=AUTOMATION_ID,authority_message_id=AUTHORITY_MESSAGE,reconciliation_receipt=archival)
  finally:ledger.close()
 return {'event_id':event['event_id'],'network':'MaxBounty','program_id':program,'row':row_number,'receipt':str(path),'disposition':disposition,'changes':[] if row is None else ['Status','Wire-In Ready?','Wired Live?','Link QA','Notes / Blocker','Last Checked UTC']}

def reconcile_payout_offer(event,candidate,svc,gmail,state,dry_run):
 values=execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}",valueRenderOption='UNFORMATTED_VALUE'),'sheet_payout_offer_read').get('values',[])
 if not values or pad(values[0])!=HEADERS:raise RuntimeError('canonical_sheet_header_mismatch')
 row_number,row,program=bind_payout_offer_row(values,candidate)
 capture_path,_=payout_offer_capture(event,candidate,row_number,row,program)
 queue_row=payout_offer_queue_row(event,candidate,row_number,row,program,capture_path)
 queue_receipt=drain_single_writer_queue_row(queue_row,dry_run)
 decision_path=write_coreage_decision_card(event,candidate,row_number,row,queue_receipt)
 receipt={'status':'PASS','event_id':event['event_id'],'network':candidate['network'],'program_id':program,
          'row_number':row_number,'signal':'payout_offer','provider_readback_verified':True,
          'authenticated_network_notice_verified':True,'sheet_readback_verified':not dry_run,
          'sheet_write_count':int(queue_receipt.get('cells_written') or 0),'sheet_queue_receipt':queue_receipt,
          'offer_capture_path':str(capture_path),'decision_card_path':str(decision_path) if decision_path else None,
          'approval_processing':False,'external_send':False,'provider_mutation':False,
          'provider_program_mutation':False,'tracking_link_generated':False,'affiliate_redirect_clicks':0,
          'dry_run':dry_run}
 path=save_receipt(event['event_id'],receipt);mark_path=None
 if dry_run:mark_state(state,event['event_id'],'dry_run_pass',receipt=path)
 else:
  cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
  try:ledger.resolve_automatically(event['event_id'],automation_id=AUTOMATION_ID,authority_message_id=AUTHORITY_MESSAGE,reconciliation_receipt=receipt)
  finally:ledger.close()
  mark_path,_=finalize_mailbox_completion(gmail,event['event_id'],path)
  mark_state(state,event['event_id'],'pass',receipt=path)
 return {'event_id':event['event_id'],'network':candidate['network'],'program_id':program,'row':row_number,
         'changes':['Notes / Blocker','Last Checked UTC'],'receipt':str(path),
         'mark_read_receipt':str(mark_path) if mark_path else None,
         'decision_card':str(decision_path) if decision_path else None}

def queue_awin_approval_transition(event,candidate,snapshot,row_number,row,program,dry_run):
 if str(row[10]).startswith('approved_'):
  return {'status':'noop','queued':False,'reason':'sheet_already_records_approval','cells_written':0}
 capture={
  'schema':'awin_authenticated_approval_capture_v1','captured_at_utc':now(),
  'event_id':str(event['event_id']),'received_at_utc':str(event['received_at']),
  'network':'Awin','publisher_id':PUBLISHER_AWIN,'program_id':str(program),
  'program_name':str((snapshot.get('details') or {}).get('name') or row[2]),
  'relationship':str(snapshot.get('relationship') or ''),'provider_snapshot':snapshot,
  'provider_mutation':False,'tracking_link_generated':False,
 }
 directory=BUSINESS/'reports/glp-clinic-scout/affiliate-machine/APPLIER-RETIRE-20260824/approval-captures'
 path=directory/f"awin-{program}-{event['event_id']}.json"
 if path.is_file():
  existing=json.loads(path.read_text())
  stable=('schema','event_id','network','publisher_id','program_id','relationship',
          'provider_mutation','tracking_link_generated')
  if any(existing.get(key)!=capture.get(key) for key in stable):
   raise RuntimeError('awin_approval_capture_binding_conflict')
  capture=existing
 else:path=approval_atomic_json(path,capture)
 relative=str(path.relative_to(BUSINESS));digest=hashlib.sha256(path.read_bytes()).hexdigest()
 queue_row={
  'update_id':f"awin-approval-confirmed-{program}-{event['event_id']}",
  'kind':'approval_confirmed','network':'Awin','program_id':str(program),
  'program_name':str(row[2]),'sheet_row':str(row_number),
  'evidence':json.dumps({'approval_capture_path':relative,'approval_capture_sha256':digest,
                         'confirmed_at_utc':str(capture['captured_at_utc']),'network_relationship':str(capture.get('relationship') or ''),
                         'network_program_id':str(program)},sort_keys=True),
  'status':'queued','queued_by':'affiliate_approval_auto_reconciler','queued_at_utc':now(),
  'resolved_at_utc':'','note':f"Authenticated Awin approval email {event['event_id']} bound to existing Sheet row {row_number}; provider relationship re-read as {snapshot.get('relationship')}. No link or deploy action.",
 }
 return drain_single_writer_queue_row(queue_row,dry_run)

def reconcile_maxbounty_campaign_approval(event,candidate,program,gmail,state,dry_run):
 """Run a campaign approval through the existing portal, Sheet queue and research rails."""
 program=str(program or '').strip()
 if not program or program=='network-account':raise RuntimeError('maxbounty_campaign_program_id_missing')
 if dry_run:
  receipt={'status':'PASS','event_id':event['event_id'],'network':'MaxBounty','program_id':program,
           'action_kind':'maxbounty_campaign_approval_chain','dry_run':True,'action_completed':False,
           'provider_readback_verified':False,'sheet_readback_verified':False,'sheet_write_count':0,
           'provider_mutation':False,'tracking_link_generated':False,'affiliate_redirect_clicks':0,
           'site_deployments':0}
  path=save_receipt(event['event_id'],receipt);mark_state(state,event['event_id'],'dry_run_pass',receipt=path)
  return {'event_id':event['event_id'],'network':'MaxBounty','program_id':program,'row':None,
          'changes':[],'receipt':str(path),'mark_read_receipt':None}
 directory=REPORT_DIR/'maxbounty-approval-chain'/str(event['event_id']);directory.mkdir(parents=True,exist_ok=True)
 chain_output=directory/'chain.json'
 command=[
  '/home/ubuntu/.hermes/venvs/glp-maxbounty-runtime/bin/python',
  str(BUSINESS/'scripts/glp_maxbounty_approval_chain.py'),
  '--program-id',program,'--event-id',str(event['event_id']),'--output',str(chain_output),
 ]
 completed=subprocess.run(command,cwd=BUSINESS,text=True,capture_output=True,timeout=900)
 if completed.returncode!=0 or not chain_output.is_file():
  raise RuntimeError(f'maxbounty_approval_chain_failed:{completed.returncode}')
 chain=json.loads(chain_output.read_text())
 if chain.get('status')!='QUEUED_FOR_SINGLE_WRITER_AND_RESEARCH_AUTOWIRE':
  raise RuntimeError(f"maxbounty_approval_chain_not_queued:{chain.get('status')}")
 update_ids=list((chain.get('sheet_queue') or {}).get('update_ids') or [])
 if not update_ids:raise RuntimeError('maxbounty_approval_chain_queue_ids_missing')
 queue_rows=n8n_dt.native_rows_for_keys(
  SHEET_UPDATE_QUEUE,'update_id',[str(value) for value in update_ids])
 drained=[]
 for update_id in update_ids:
  row=queue_rows.get(str(update_id))
  if row is None:raise RuntimeError(f'maxbounty_approval_queue_readback_count:{update_id}:0')
  drained.append(drain_single_writer_queue_row(row,False))
 if any(row.get('status') not in {'applied','noop','deferred'} for row in drained):
  raise RuntimeError('maxbounty_approval_queue_drain_not_terminal')
 receipt={
  'status':'PASS','event_id':event['event_id'],'network':'MaxBounty','program_id':program,
  'row_number':chain.get('sheet_row'),'signal':candidate['action'],
  'action_kind':'maxbounty_campaign_approval_chain','action_completed':True,
  'provider_relationship':'approved','provider_readback_verified':True,
  'sheet_readback_verified':True,'sheet_schema_contract_verified':True,
  'chain_receipt_path':str(chain_output),'chain_receipt_sha256':hashlib.sha256(chain_output.read_bytes()).hexdigest(),
  'queue_results':drained,'queue_rows_deferred_to_wire_in_gate':[row.get('update_id') for row in drained if row.get('status')=='deferred'],'research_owner':'glp/research-autowire',
  'deploy_queue_owner':'glp/affiliate-wire-backlog','changed_columns':[],
  'sheet_write_count':sum(int(row.get('cells_written') or 0) for row in drained),
  'dry_run':False,'external_send':False,'outbound_send':False,
  # TWO DIFFERENT FACTS, 2026-09-02 (approvals backfill). `provider_mutation`
  # is "we made a state-changing call at the provider" - TRUE, the chain mints
  # the tracking link, which is the authorised action it exists to perform.
  # `provider_program_mutation` is "we changed the PROGRAMME relationship" -
  # applied, joined, accepted, altered terms. The chain does none of that: it
  # VERIFIES the relationship is already `approved` and refuses otherwise, and
  # its own effects ledger carries `applications_submitted: 0`.
  #
  # Deriving both from the same `provider_mutations` count claimed a programme
  # mutation that never happened, and `mark_read_after_action.validate_action_receipt`
  # correctly refuses any receipt asserting one (`mark_read_unsafe_receipt:
  # provider_program_mutation`). That was the third and last gate holding the
  # 10-minute re-mint loop open. The honest source for a programme mutation is
  # the applications counter.
  'provider_mutation':bool((chain.get('effects') or {}).get('provider_mutations')),
  'provider_program_mutation':bool((chain.get('effects') or {}).get('applications_submitted')),
  'tracking_link_generated':True,'tracking_link_value':'[REDACTED]',
  'tracking_links_opened':0,'affiliate_redirect_clicks':0,'site_deployments':0,
  'payout_evaluation':chain.get('payout_evaluation'),'site_identity':chain.get('site_identity'),
 }
 path=save_receipt(event['event_id'],receipt)
 cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
 try:
  if str(event['state']) not in {'resolved_externally','held','archived'}:
   ledger.resolve_automatically(event['event_id'],automation_id=AUTOMATION_ID,
                                authority_message_id=AUTHORITY_MESSAGE,
                                reconciliation_receipt=receipt)
 finally:ledger.close()
 mark_path,_=finalize_mailbox_completion(gmail,event['event_id'],path,approval=True)
 mark_state(state,event['event_id'],'pass',receipt=path)
 return {'event_id':event['event_id'],'network':'MaxBounty','program_id':program,
         'row':chain.get('sheet_row'),'changes':[],'receipt':str(path),
         'mark_read_receipt':str(mark_path),'research_queued':True,'deploy_queued_after_research':True}


def recover_maxbounty_approval_fact(event,gmail,state):
 """Classify a completed campaign chain without replaying its provider effects."""
 raw=Path(event['raw_path']).read_bytes()
 if hashlib.sha256(raw).hexdigest()!=event['raw_sha256']:raise RuntimeError('approval_recovery_raw_hash_mismatch')
 subject,sender,body=message_text(event['raw_path']);candidate=detect(subject,sender,body)
 if not candidate or candidate.get('network')!='MaxBounty' or candidate.get('action')!='relationship_approval':
  raise RuntimeError('approval_recovery_signal_mismatch')
 auth=verify_affiliate_authentication(event,'MaxBounty')
 if auth['status']!='PASS':raise RuntimeError(auth['reason'])
 chain_path=REPORT_DIR/'maxbounty-approval-chain'/str(event['event_id'])/'chain.json'
 chain=json.loads(chain_path.read_text());program=str(chain.get('program_id') or '')
 if (chain.get('event_id')!=event['event_id'] or chain.get('relationship')!='approved'
     or chain.get('status')!='QUEUED_FOR_SINGLE_WRITER_AND_RESEARCH_AUTOWIRE'
     or program not in [str(value) for value in candidate.get('program_ids',[])]):
  raise RuntimeError('approval_recovery_chain_identity_mismatch')
 capture_path=(BUSINESS/chain['capture']['path']).resolve()
 if not capture_path.is_relative_to(BUSINESS) or hashlib.sha256(capture_path.read_bytes()).hexdigest()!=chain['capture']['sha256']:
  raise RuntimeError('approval_recovery_capture_hash_mismatch')
 capture=json.loads(capture_path.read_text())
 programs=[row for row in capture.get('programs',[]) if str(row.get('program_id'))==program]
 if capture.get('event_id')!=event['event_id'] or len(programs)!=1 or programs[0].get('relationship')!='approved':
  raise RuntimeError('approval_recovery_capture_identity_mismatch')
 item=programs[0]
 require_operator_ruling_clear(item['name'],load_snapshot())
 # Existing authenticated account reader supplies publisher identity, never a link.
 account=maxbounty_snapshot()
 snapshot={'provider':'MaxBounty','program_id':program,'relationship':'approved',
           'official_url':item['official_url'],'verified':True,'provider_readback_verified':True,
           'details':{'title':item['name']},'capture_path':str(capture_path),
           'capture_sha256':chain['capture']['sha256'],'terms':item.get('terms','')}
 terms=screen_provider_terms(item.get('terms',''))
 classification=classify_approved_event(event,candidate,snapshot,program,terms)
 fact=record_approval_fact(event,candidate,snapshot,program,terms,classification,account['publisher_id'])
 receipt={'status':'PASS','event_id':event['event_id'],'network':'MaxBounty','program_id':program,
          'action_kind':'affiliate_approval_fact_recovery','action_completed':True,
          'classification':classification['classification'],'approval_fact':fact,
          'provider_readback_verified':True,'sheet_write_count':0,'external_send':False,
          'provider_mutation':False,'provider_program_mutation':False,'tracking_link_generated':False,
          'provider_draft':False,'archive':False,'delete':False,'affiliate_redirect_clicks':0,
          'chain_receipt_path':str(chain_path)}
 path=save_receipt(event['event_id'],receipt)
 mark_path,completion=finalize_mailbox_completion(gmail,event['event_id'],path,approval=True)
 mark_state(state,event['event_id'],'pass',receipt=path)
 return {'event_id':event['event_id'],'network':'MaxBounty','program_id':program,
         'row':chain.get('sheet_row'),'changes':[],'receipt':str(path),
         'mark_read_receipt':str(mark_path),'classification_recovered':True,'approval_completion':completion}


async def process_event(event,svc,gmail,state,dry_run,provider_cache,operator_handoff=None,recover_terminal_routine=False,policy_snapshot=None):
 if approval_test_label(event):
  receipt={'status':'TEST_EXCLUDED','event_id':event['event_id'],'is_test':True,
           'approval_processing':False,'sheet_write_count':0,'dry_run':dry_run,
           'provider_readback_verified':False,'sheet_readback_verified':False,
           'external_send':False,'provider_mutation':False,'affiliate_redirect_clicks':0}
  path=save_receipt(event['event_id'],receipt)
  mark_state(state,event['event_id'],'dry_run_test_excluded' if dry_run else 'test_excluded',receipt=path)
  return {'status':'TEST_EXCLUDED','event_id':event['event_id'],'receipt':str(path),
          'mark_read_receipt':None,'changes':[]}
 if 'missing_approval_fact_recovery' in event.keys() and event['missing_approval_fact_recovery']:
  if dry_run:return {'event_id':event['event_id'],'classification_recovery_pending':True,'changes':[]}
  return recover_maxbounty_approval_fact(event,gmail,state)
 terminal_recovery=None
 if ('mailbox_read_recovery' in event.keys() and bool(event['mailbox_read_recovery'])
     and not operator_handoff and not recover_terminal_routine):
  prior=state.execute('SELECT status,receipt_path FROM events WHERE event_id=?',(event['event_id'],)).fetchone()
  if not prior or not prior['receipt_path'] or not Path(prior['receipt_path']).is_file():raise RuntimeError('mailbox_completion_recovery_receipt_missing')
  approval=bool(event['approval_completion_pending']) if 'approval_completion_pending' in event.keys() else False
  mark_path,completion=finalize_mailbox_completion(gmail,event['event_id'],Path(prior['receipt_path']),approval=approval)
  mark_state(state,event['event_id'],'pass',receipt=Path(prior['receipt_path']))
  return {'event_id':event['event_id'],'network':None,'program_id':None,'row':None,'changes':[],'receipt':str(prior['receipt_path']),'mark_read_receipt':str(mark_path),'completion_recovered':True,'approval_completion':completion}
 if str(event['state']) in TERMINAL_EVENT_STATES and not operator_handoff and not recover_terminal_routine:return None
 subject,sender,body=message_text(event['raw_path']); cand=detect(subject,sender,body)
 if not cand:
  if str(event['canonical_category'] or '')=='affiliate_network_response':raise RuntimeError('canonical_affiliate_signal_unhandled')
  mark_state(state,event['event_id'],'ignored');return None
 if cand['action']=='campaign_cancellation':return reconcile_campaign_cancellation(event,cand,svc,gmail,state,dry_run)
 policy_snapshot=policy_snapshot or load_snapshot()
 require_operator_ruling_clear(cand.get('advertiser_name') or cand.get('program_name') or '',policy_snapshot)
 exact_footprint_reducing_routine=(
  cand.get('action')=='routine_information'
  and cand.get('detail') in {'ascend_application_received'}
 )
 if (str(event['canonical_category'] or '')=='affiliate_network_response'
     and cand['network'] in AFFILIATE_AUTH_DOMAINS and not exact_footprint_reducing_routine):
  auth=verify_affiliate_authentication(event,cand['network'])
  if auth['status']!='PASS':raise RuntimeError(auth['reason'])
 if cand['action']=='contract_terms_review':
  # Notification identity is not authority to accept a contract or alter payout.
  raise AffiliateReviewRequired('legal_terms_capture','contract_terms_changed_requires_verified_capture',
      {'notice_authenticated':True,'program_name':cand['program_name'],
       'terms_capture_complete':False,'provider_readback_verified':False,
       'terms_acceptance_allowed':False,'prior_provider_holds_preserved':True})
 if recover_terminal_routine:
  terminal_recovery=validate_terminal_routine_recovery(event,state,cand)
  if terminal_recovery.get('already_reconciled'):
   prior_receipt=json.loads(Path(terminal_recovery['receipt_path']).read_text())
   return {'event_id':event['event_id'],'network':prior_receipt.get('network'),'program_id':prior_receipt.get('program_id'),'row':prior_receipt.get('row_number'),'changes':[],'receipt':terminal_recovery['receipt_path'],'mark_read_receipt':None,'idempotent_replay':True}
 if operator_handoff:validate_operator_handoff_event(operator_handoff,event)
 if cand['action']=='manual_response_required':raise AffiliateReviewRequired('operator_draft','reply_or_meeting_decision_required')
 if (cand['action']=='account_activation_required'
     and cand.get('detail')=='email_confirmation_would_submit_application'):
  raise AffiliateCandidateParked('operator_application_pause',['account_confirmation_would_submit_application'])
 if cand['action']=='payout_offer':return reconcile_payout_offer(event,cand,svc,gmail,state,dry_run)
 if cand['action']=='account_access_resolved':
  # Access RESOLUTION completes ahead of the prerequisite screen: this mail says a
  # login wall came DOWN. Receipt it where the daily report and the portal lane
  # read machine-cleared access, then complete as routine information.
  resolution={'schema':'affiliate_portal_access_resolution_v1','status':'PASS','event_id':event['event_id'],'network':cand['network'],'case_id':cand.get('case_id'),'resolution':'portal_login_wall_removed_by_network','summary':'the network confirmed the portal login wall (2SV/2FA) is off for ops@; the portal-apply lane retries the universal credential at its next drain','provider_mutation':False,'outbound_send':False,'recorded_at_utc':now()}
  approval_atomic_json(REPORT_DIR/'portal-access-resolutions'/f"{re.sub(r'[^a-z0-9]+','-',str(cand['network']).lower()).strip('-')}-{event['event_id']}.json",resolution)
  cand=dict(cand);cand['action']='routine_information';cand['detail']=f"account_access_resolved:case_{cand.get('case_id') or 'unknown'}"
 if IDENTITY_PREREQUISITE_RE.search(cand.get('text') or ''):raise AffiliateReviewRequired('identity_prerequisite','identity_document_action_required')
 if OPERATOR_PREREQUISITE_RE.search(cand.get('text') or ''):raise AffiliateReviewRequired('account_prerequisite','tax_kyc_identity_or_payout_action_required')
 policy_version=event['canonical_policy_version'] if 'canonical_policy_version' in event.keys() else ''
 canonical_route=event['canonical_route'] if 'canonical_route' in event.keys() else ''
 if (str(policy_version or '').endswith('affiliate-approval-autoprocess-v5')
     and str(canonical_route or '')=='decisions'):
  raise RuntimeError('inbox_reconciler_disposition_mismatch')
 if cand['action']=='routine_information':
  receipt={'status':'PASS','event_id':event['event_id'],'network':cand['network'],'signal':cand['action'],'action_kind':'routine_affiliate_information','action_completed':True,'provider_readback_verified':False,'sheet_readback_verified':False,'changed_columns':[],'sheet_write_count':0,'dry_run':dry_run,'external_send':False,'provider_mutation':False,'provider_program_mutation':False,'tracking_link_generated':False,'archive':False,'delete':False,'provider_draft':False,'affiliate_redirect_clicks':0}
  if cand.get('detail'):receipt['action_kind_detail']=cand['detail']
  if cand.get('classification'):receipt['classification']=cand['classification']
  if cand.get('advertiser_name'):receipt['advertiser_name']=cand['advertiser_name']
  for key in ('partner_account_ids','advertiser_ids','campaign_ids'):
   if key in cand:receipt[key]=list(cand[key])
  path=save_receipt(event['event_id'],receipt);mark_path=None
  if dry_run:mark_state(state,event['event_id'],'dry_run_pass',receipt=path)
  else:
   if str(event['state']) not in {'resolved_externally','held','archived'}:
    cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
    try:ledger.resolve_automatically(event['event_id'],automation_id=AUTOMATION_ID,authority_message_id=AUTHORITY_MESSAGE,reconciliation_receipt=receipt)
    finally:ledger.close()
   mark_path,_=finalize_mailbox_completion(gmail,event['event_id'],path)
   mark_state(state,event['event_id'],'pass',receipt=path)
  return {'event_id':event['event_id'],'network':cand['network'],'program_id':None,'row':None,'changes':[],'receipt':str(path),'mark_read_receipt':str(mark_path) if mark_path else None}
 if cand['network'] in DIRECT_PARTNER_NETWORKS:
  return reconcile_direct_partner_event(event,cand,svc,gmail,state,dry_run)
 if cand['network'] in DIRECT_OPERATOR_NETWORKS:
  if not operator_handoff:raise RuntimeError(f"provider_adapter_unavailable:{cand['network']}")
  return reconcile_direct_operator_event(event,cand,svc,gmail,state,dry_run,operator_handoff)
 if cand['network']=='Everflow':
  if cand['action']=='relationship_approval' and cand.get('account_activation_required'):
   raise AffiliateReviewRequired('provider_account_activation','operator_login_verification_gate')
  raise RuntimeError('provider_adapter_unavailable:Everflow')
 if cand['network'] not in {'Awin','Katalys','Partnerize','CJ Affiliate','Impact.com','HealthTrader','MaxBounty'}:raise RuntimeError(f"provider_adapter_unavailable:{cand['network']}")
 values=execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!{EXPECTED_RANGE}"),'sheet_full_read').get('values',[])
 if not values or pad(values[0])!=HEADERS:raise RuntimeError('canonical_sheet_header_mismatch')
 tombstone=match_closed_program_tombstone(cand,load_closed_program_tombstones())
 if tombstone:
  if not closed_program_absent_from_sheet(values,tombstone):raise RuntimeError('closed_program_tombstone_sheet_row_present')
  receipt={'status':'PASS','event_id':event['event_id'],'network':cand['network'],'program_id':tombstone['program_id'],'signal':cand['action'],'action_kind':'routine_affiliate_information','action_kind_detail':'closed_program_tombstone_preserved','action_completed':True,'closure_event_id':tombstone['closure_event_id'],'closure_authority_message_id':tombstone['operator_authority_message_id'],'sheet_deletion_receipt':tombstone['sheet_deletion_receipt'],'sheet_absence_verified':True,'provider_readback_verified':False,'sheet_readback_verified':True,'changed_columns':[],'sheet_write_count':0,'dry_run':dry_run,'external_send':False,'provider_mutation':False,'provider_program_mutation':False,'tracking_link_generated':False,'archive':False,'delete':False,'provider_draft':False,'affiliate_redirect_clicks':0}
  path=save_receipt(event['event_id'],receipt);mark_path=None
  if dry_run:mark_state(state,event['event_id'],'dry_run_pass',receipt=path)
  else:
   if str(event['state']) not in {'resolved_externally','held','archived'}:
    cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
    try:ledger.resolve_automatically(event['event_id'],automation_id=AUTOMATION_ID,authority_message_id=AUTHORITY_MESSAGE,reconciliation_receipt=receipt)
    finally:ledger.close()
   mark_path,_=finalize_mailbox_completion(gmail,event['event_id'],path)
   mark_state(state,event['event_id'],'pass',receipt=path)
  return {'event_id':event['event_id'],'network':cand['network'],'program_id':tombstone['program_id'],'row':None,'changes':[],'receipt':str(path),'mark_read_receipt':str(mark_path) if mark_path else None,'closed_program_tombstone_preserved':True}
 prior_rejection=prior_rejection_evidence_match(values,event,cand)
 if prior_rejection:
  return reconcile_prior_email_rejection(event,cand,prior_rejection,gmail,state,dry_run)
 created_row=False;creation_applied=False;initial_sheet_write_count=0;snap=None;creation_queue=None
 try:row_num,row,program=bind_row(values,cand)
 except RuntimeError as exc:
  partnerize_missing=(cand['network']=='Partnerize' and cand['action'] in {'relationship_approval','relationship_adverse'} and str(exc)=='sheet_identity_not_found:Partnerize:matches=0')
  if partnerize_missing:
   if 'partnerize' not in provider_cache:provider_cache['partnerize']=partnerize_relationship_index()
   if cand['action']=='relationship_adverse':
    try:
     program,snap=partnerize_exact_relationship_snapshot(cand,provider_cache['partnerize'],'rejected')
    except RuntimeError as snap_exc:
     if str(snap_exc)=='partnerize_rejected_identity_count:0':
      routine=nonglp_relationship_routine(event,cand,gmail,state,dry_run,'nonglp_adverse_provider_unlisted')
      if routine:return routine
     raise
    classification=classify_nonapproval_relationship(event,cand,snap,program)
    require_operator_ruling_clear(program,policy_snapshot);sheet=upsert_new_partnerize_approval(event,cand,snap,program,classification,{},apply=not dry_run,relationship='rejected')
    receipt={'status':'PASS','event_id':event['event_id'],'network':'Partnerize','program_id':str(program),'row_number':sheet['row_number'],'signal':cand['action'],'action_kind':'affiliate_relationship_reconciliation','provider_relationship':'rejected','provider_readback_verified':True,'approval_processing':False,'terms_rubric_executed':False,'terms_capture_complete':False,'terms_incomplete_nonblocking':False,'terms_text_sha256':None,'terms_hard_trigger_count':0,'approval_classification_recorded':False,'approval_fact_recorded':False,'approval_fact':None,'classification':classification,'sheet_readback_verified':True,'sheet_schema_contract_verified':True,'sheet_receipt_path':sheet['sheet_receipt_path'],'sheet_receipt_sha256':sheet['sheet_receipt_sha256'],'changed_columns':[],'sheet_write_count':sheet['sheet_write_count'],'dry_run':dry_run,'external_send':False,'provider_mutation':False,'provider_program_mutation':False,'tracking_link_generated':False,'archive':False,'delete':False,'provider_draft':False,'affiliate_redirect_clicks':0}
    path=save_receipt(event['event_id'],receipt);mark_path=None
    if dry_run:mark_state(state,event['event_id'],'dry_run_pass',receipt=path)
    else:
     cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
     try:ledger.resolve_automatically(event['event_id'],automation_id=AUTOMATION_ID,authority_message_id=AUTHORITY_MESSAGE,reconciliation_receipt=receipt)
     finally:ledger.close()
     mark_path,_=finalize_mailbox_completion(gmail,event['event_id'],path);mark_state(state,event['event_id'],'pass',receipt=path)
    return {'event_id':event['event_id'],'network':'Partnerize','program_id':str(program),'row':sheet['row_number'],'changes':[],'classification':classification['classification'],'receipt':str(path),'mark_read_receipt':str(mark_path) if mark_path else None,'created_sheet_row':bool(sheet['created_count'])}
   try:
    program,snap=partnerize_exact_approved_snapshot(cand,provider_cache['partnerize'])
   except RuntimeError as snap_exc:
    if str(snap_exc)=='partnerize_approved_identity_count:0':
     routine=nonglp_relationship_routine(event,cand,gmail,state,dry_run,'nonglp_acceptance_provider_unlisted')
     if routine:return routine
    raise
   terms=partnerize_terms_capture(provider_cache['partnerize'][0],program,event['event_id'])
   if int(terms.get('hard_trigger_count') or 0):
    codes=','.join(item.get('trigger','unknown') for item in terms.get('hard_triggers') or [])
    raise AffiliateReviewRequired('legal_terms_hard_trigger',codes or 'hard_trigger_detected',terms)
   classification=classify_approved_event(event,cand,snap,program,terms)
   require_operator_ruling_clear(program,policy_snapshot);sheet=upsert_new_partnerize_approval(event,cand,snap,program,classification,terms,apply=not dry_run)
   fact=None if dry_run else record_approval_fact(event,cand,snap,program,terms,classification,PUBLISHER_PARTNERIZE)
   receipt={'status':'PASS','event_id':event['event_id'],'network':'Partnerize','program_id':str(program),'row_number':sheet['row_number'],'signal':cand['action'],'provider_relationship':'approved','provider_readback_verified':True,'approval_processing':True,'terms_rubric_executed':True,'terms_capture_complete':bool(terms.get('capture_complete')),'terms_incomplete_nonblocking':bool(terms.get('incomplete_nonblocking')),'terms_text_sha256':terms['text_sha256'],'terms_hard_trigger_count':0,'approval_classification_recorded':True,'approval_fact_recorded':fact is not None,'approval_fact':fact,'classification':classification,'sheet_readback_verified':True,'sheet_schema_contract_verified':True,'sheet_receipt_path':sheet['sheet_receipt_path'],'sheet_receipt_sha256':sheet['sheet_receipt_sha256'],'changed_columns':[],'sheet_write_count':sheet['sheet_write_count'],'dry_run':dry_run,'external_send':False,'provider_mutation':False,'provider_program_mutation':False,'tracking_link_generated':False,'archive':False,'delete':False,'provider_draft':False,'affiliate_redirect_clicks':0}
   path=save_receipt(event['event_id'],receipt);mark_path=None
   if dry_run:mark_state(state,event['event_id'],'dry_run_pass',receipt=path)
   else:
    cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
    try:ledger.resolve_automatically(event['event_id'],automation_id=AUTOMATION_ID,authority_message_id=AUTHORITY_MESSAGE,reconciliation_receipt=receipt)
    finally:ledger.close()
    mark_path,_=finalize_mailbox_completion(gmail,event['event_id'],path,approval=True);mark_state(state,event['event_id'],'pass',receipt=path)
   return {'event_id':event['event_id'],'network':'Partnerize','program_id':str(program),'row':sheet['row_number'],'changes':[],'classification':classification['classification'],'receipt':str(path),'mark_read_receipt':str(mark_path) if mark_path else None,'created_sheet_row':bool(sheet['created_count'])}
  katalys_missing=(cand['network']=='Katalys' and cand['action'] in {'relationship_approval','relationship_adverse'} and str(exc)=='sheet_identity_not_found:Katalys:matches=0')
  if katalys_missing:
   routine=nonglp_relationship_routine(event,cand,gmail,state,dry_run,'nonglp_relationship_recorded_without_sheet')
   if routine:return routine
  awin_approval_missing=(cand['network']=='Awin' and cand['action']=='relationship_approval' and str(exc)=='sheet_identity_not_found:Awin:matches=0')
  if awin_approval_missing:
   if 'awin' not in provider_cache:provider_cache['awin']=awin_relationship_index()
   program,snap=awin_exact_relationship_snapshot(cand,provider_cache['awin'],'joined')
   row_num,row,program,creation_queue=create_missing_awin_pending_via_single_writer(event,cand,snap,svc,dry_run)
   created_row=creation_queue.get('reason')!='sheet_row_already_exists'
   creation_applied=bool(not dry_run and creation_queue.get('status')=='applied')
   initial_sheet_write_count=int(creation_queue.get('cells_written') or 0)
   cand=dict(cand);cand['text']=str(cand.get('text') or '')+f'\nAwin aid: {program}'
  invitation_missing=(cand['network']=='Awin' and cand['action']=='relationship_invitation' and str(exc)=='sheet_identity_not_found:Awin:matches=0')
  if awin_approval_missing:pass
  elif not invitation_missing:raise
  else:
   ids=[int(value) for value in cand.get('program_ids',[])]
   if len(ids)!=1:raise RuntimeError(f'awin_invitation_exact_id_count:{len(ids)}')
   program=ids[0]
   if 'awin' not in provider_cache:provider_cache['awin']=awin_relationship_index()
   snap=awin_snapshot(program,provider_cache['awin'],cand.get('advertiser_name'))
   if not snap.get('verified') or snap.get('relationship')!='notjoined':raise RuntimeError(f"awin_invitation_provider_state_invalid:{snap.get('relationship')}")
   require_operator_ruling_clear(cand.get('advertiser_name') or '',policy_snapshot)
   row_num,row,program,creation_queue=create_missing_awin_invitation_via_single_writer(event,cand,snap,svc,dry_run)
   created_row=creation_queue.get('reason')!='sheet_row_already_exists';creation_applied=bool(not dry_run and creation_queue.get('status')=='applied');initial_sheet_write_count=int(creation_queue.get('cells_written') or 0)
 if (cand['network']=='MaxBounty' and cand['action']=='relationship_approval'
     and str(program)!='network-account'):
  return reconcile_maxbounty_campaign_approval(event,cand,program,gmail,state,dry_run)
 if cand['network']=='Awin' and cand['action']=='relationship_adverse':
  rejection_queue=queue_awin_email_rejection(event,cand,row_num,row,program,svc,dry_run)
  receipt={
   'status':'PASS','event_id':event['event_id'],'network':'Awin','program_id':program,
   'row_number':row_num,'signal':cand['action'],'action_kind':'authenticated_email_rejection',
   'action_completed':not dry_run,'authenticated_network_notice_verified':True,
   'provider_relationship':'rejected_by_authenticated_email',
   'provider_readback_verified':False,'affiliate_provider_api_called':False,
   'sheet_readback_verified':bool(rejection_queue.get('sheet_readback_verified')),
   'sheet_schema_contract_verified':True,'rejection_queue':rejection_queue,
   'changed_columns':['K','T','U','V','X','Y'] if rejection_queue.get('status')=='applied' else [],
   'sheet_write_count':int(rejection_queue.get('cells_written') or 0),'dry_run':dry_run,
   'external_send':False,'provider_mutation':False,'provider_program_mutation':False,
   'tracking_link_generated':False,'archive':False,'delete':False,'provider_draft':False,
   'affiliate_redirect_clicks':0,'terminal_routine_recovery':terminal_recovery,
  }
  path=save_receipt(event['event_id'],receipt);mark_path=None
  if dry_run:
   if not recover_terminal_routine:mark_state(state,event['event_id'],'dry_run_pass',receipt=path)
  else:
   if recover_terminal_routine or str(event['state']) not in {'resolved_externally','held','archived'}:
    cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
    try:ledger.resolve_automatically(event['event_id'],automation_id=AUTOMATION_ID,authority_message_id=AUTHORITY_MESSAGE,reconciliation_receipt=receipt)
    finally:ledger.close()
   mark_path,_=finalize_mailbox_completion(gmail,event['event_id'],path,approval=False)
   mark_state(state,event['event_id'],'pass',receipt=path)
  return {'event_id':event['event_id'],'network':'Awin','program_id':program,
          'row':row_num,'changes':receipt['changed_columns'],'receipt':str(path),
          'mark_read_receipt':str(mark_path) if mark_path else None}
 if cand['network']=='Awin':
  if snap is None:
   if 'awin' not in provider_cache:provider_cache['awin']=awin_relationship_index()
   snap=awin_snapshot(program,provider_cache['awin'],row[2])
 elif cand['network']=='Katalys':snap=await katalys_snapshot(program,row[2],row[14],allow_link_generation=False,expected_action=cand['action'],fresh_driver=provider_cache.get('fresh_katalys_driver'))
 elif cand['network']=='CJ Affiliate':snap=await cj_snapshot(program,row[2])
 elif cand['network']=='Impact.com':snap=impact_snapshot(program,row[2],cand['action']);program=snap['program_id']
 elif cand['network']=='HealthTrader':snap=await healthtrader_snapshot(program,row[2],fresh_driver=provider_cache.get('fresh_healthtrader_driver'))
 elif cand['network']=='MaxBounty':
  if 'maxbounty' not in provider_cache:provider_cache['maxbounty']=maxbounty_snapshot(fresh_driver=provider_cache.get('fresh_maxbounty_driver'))
  snap=provider_cache['maxbounty'];program=snap['program_id']
 else:
  if 'partnerize' not in provider_cache:provider_cache['partnerize']=partnerize_relationship_index()
  snap=partnerize_snapshot(program,provider_cache['partnerize'],row[2],row[6],allow_link_generation=False)
 if not snap.get('verified'):raise RuntimeError(f"provider_readback_unverified:{snap.get('relationship')}")
 approval_classification=None;terms_evidence=None;candidate_gate_advisory=None;invitation_classification=None;approval_queue=None
 if cand['action']=='relationship_approval' and snap.get('relationship') in {'approved','joined','active'}:
  terms_evidence=(partnerize_terms_capture(provider_cache['partnerize'][0],program,event['event_id']) if cand['network']=='Partnerize' else screen_provider_terms(snap.get('details') or {},str(snap.get('terms') or '')))
  if int(terms_evidence.get('hard_trigger_count') or 0):
   codes=','.join(item.get('trigger','unknown') for item in terms_evidence.get('hard_triggers') or [])
   raise AffiliateReviewRequired('legal_terms_hard_trigger',codes or 'hard_trigger_detected',terms_evidence)
  approval_classification=classify_approved_event(event,cand,snap,program,terms_evidence)
 require_operator_ruling_clear(row[2],policy_snapshot)
 if cand['action']=='relationship_invitation' and snap.get('relationship')=='notjoined':
  invitation_classification=classify_awin_invitation(str(row[2]),str(row[6]),snap.get('details') or {})
  if invitation_classification.get('portfolio')=='glp':raise RuntimeError('awin_core_invitation_requires_acceptance_lane')
  candidate_gate_advisory={'decision':'not_joined_fit_unsupported','classification':invitation_classification,'relationship_truth_recorded':True,'tracking_and_wire_disabled':True}
 elif str(program)!='network-account':
  gate=choke_candidate({'display_name':row[2],'network':cand.get('network'),'program_id':program,'description':row[3],'category':row[3],'region':row[25],'application_url':row[5]},require_application_url=True,operation='affiliate_approval_auto_reconciler.sheet_handoff',persist=not dry_run)
  if not gate['operational']:
   if not approval_relationship_truth_only(cand,snap,approval_classification,gate):
    raise AffiliateCandidateParked(gate['decision'],gate['reason_codes'])
   candidate_gate_advisory={'decision':gate['decision'],'reason_codes':list(gate.get('reason_codes') or []),'relationship_truth_recorded':True,'tracking_and_wire_disabled':True}
 if cand['network']=='Awin' and approval_classification is not None:
  approval_queue=queue_awin_approval_transition(event,cand,snap,row_num,row,program,dry_run)
  changes={}
 else:
  changes={} if invitation_classification is not None else desired_changes(row,snap,event['received_at'],cand['action'],signal_text=cand['text'],allow_tracking=candidate_gate_advisory is None)
 if cand['network']=='HealthTrader' and str(program)=='network-account':
  changes=healthtrader_account_changes(row,changes)
 bad_checkbox_types={ALLOWED.get(col,str(col)):type(value).__name__ for col,value in changes.items() if col in CHECKBOX_COLUMNS and not isinstance(value,bool)}
 if bad_checkbox_types:raise RuntimeError(f'affiliate_sheet_checkbox_write_not_boolean:{bad_checkbox_types}')
 if 18 in changes and not isinstance(changes[18],(int,float)):raise RuntimeError(f'affiliate_sheet_date_write_not_numeric:{type(changes[18]).__name__}')
 before_hash=sha(json.dumps(row,sort_keys=False)); write_count=initial_sheet_write_count+int((approval_queue or {}).get('cells_written') or 0);schema_repair=None
 if changes and not dry_run:
  with sheet_write_lock():
   schema_repair=repair_non_value_schema_issues(svc,SHEET_ID,TAB);require_checkbox_schema_pass(svc,SHEET_ID,TAB)
   metadata=execute_google_request(svc.spreadsheets().get(spreadsheetId=SHEET_ID,fields='sheets(properties(sheetId,title))'),'sheet_metadata_read')
   sheet_id=next(int(item['properties']['sheetId']) for item in metadata['sheets'] if item['properties']['title']==TAB)
   execute_google_request(svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID,body={'requests':schema_requests(sheet_id,[row_num])}),'sheet_target_schema_write')
   current=pad(execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!A{row_num}:AA{row_num}"),'sheet_compare_and_swap_read').get('values',[[]])[0])
   if sha(json.dumps(current,sort_keys=False))!=before_hash:raise RuntimeError('sheet_compare_and_swap_failed')
   data=[];keys=[]
   for col,new in changes.items():
    key=transition_key(row_num,col,row[col],new,program,cand['network'],row[13]);keys.append(key);state.execute('INSERT OR IGNORE INTO transitions(transition_key,event_id,row_number,column_index,old_sha256,new_sha256,status,created_at) VALUES(?,?,?,?,?,?,?,?)',(key,event['event_id'],row_num,col,sha(row[col]),sha(new),'planned',now()));data.append({'range':a1(row_num,col),'values':[[new]]})
   require_marketplace_parent_rows_not_mutated((row_num, col) for col in changes)
   state.commit();execute_google_request(svc.spreadsheets().values().batchUpdate(spreadsheetId=SHEET_ID,body={'valueInputOption':'RAW','data':data}),'sheet_batch_update');write_count=len(data)
   execute_google_request(svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID,body={'requests':schema_requests(sheet_id,[row_num])}),'sheet_post_value_schema_write')
   reread=pad(execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!A{row_num}:AA{row_num}",valueRenderOption='UNFORMATTED_VALUE'),'sheet_post_write_readback').get('values',[[]])[0])
   if any(str(reread[c]).upper()!=str(v).upper() for c,v in changes.items()):raise RuntimeError('sheet_post_write_readback_failed')
   require_checkbox_schema_pass(svc,SHEET_ID,TAB)
   state.executemany("UPDATE transitions SET status='applied',applied_at=? WHERE transition_key=?",[(now(),k) for k in keys]);state.commit()
 approval_publisher=PUBLISHER_AWIN if cand['network']=='Awin' else row[13]
 approval_fact=None if dry_run or approval_classification is None else record_approval_fact(event,cand,snap,program,terms_evidence,approval_classification,approval_publisher)
 approval_processing=approval_classification is not None
 receipt={'status':'PASS','event_id':event['event_id'],'network':cand['network'],'program_id':program,'row_number':row_num,'signal':cand['action'],'provider_relationship':snap['relationship'],
  # Persist only readback provenance, never the raw provider body or credentials.
  'provider_readback_provenance':{
   **{key:snap[key] for key in ('api_readback','read_method','terms_readback') if key in snap},
   **{key:snap['details'][key] for key in ('relationship_scope','inventory_complete','publisher_id','principal_verified') if key in (snap.get('details') or {})},
  },'provider_readback_verified':True,'approval_processing':approval_processing,'terms_rubric_executed':approval_processing,'terms_capture_complete':bool((terms_evidence or {}).get('capture_complete')),'terms_incomplete_nonblocking':bool((terms_evidence or {}).get('incomplete_nonblocking')),'terms_text_sha256':(terms_evidence or {}).get('text_sha256'),'terms_hard_trigger_count':int((terms_evidence or {}).get('hard_trigger_count') or 0),'approval_classification_recorded':approval_classification is not None,'approval_fact_recorded':approval_fact is not None,'approval_fact':approval_fact,'classification':approval_classification or invitation_classification,'candidate_gate_advisory':candidate_gate_advisory,'relationship_truth_only':candidate_gate_advisory is not None,'sheet_readback_verified':True,'sheet_schema_contract_verified':True,'schema_repair':schema_repair,'created_row':created_row,'creation_applied':creation_applied,'creation_queue':creation_queue,'approval_queue':approval_queue,'created_columns':HEADERS if creation_applied else [],'changed_columns':[ALLOWED[c] for c in changes]+(['K','T','U','X','Y'] if approval_queue and approval_queue.get('status')=='applied' else []),'sheet_write_count':write_count,'dry_run':dry_run,'external_send':False,'provider_mutation':bool(snap.get('link_generated')),'provider_program_mutation':False,'tracking_link_generated':bool(snap.get('link_generated')),'archive':False,'delete':False,'provider_draft':False,'affiliate_redirect_clicks':0,'terminal_routine_recovery':terminal_recovery}
 path=save_receipt(event['event_id'],receipt);mark_path=None
 if dry_run:
  if not recover_terminal_routine:mark_state(state,event['event_id'],'dry_run_pass',receipt=path)
 else:
  if recover_terminal_routine or str(event['state']) not in {'resolved_externally','held','archived'}:
   cp=load_control_plane();ledger=cp.Ledger(INBOX_DB)
   try:ledger.resolve_automatically(event['event_id'],automation_id=AUTOMATION_ID,authority_message_id=AUTHORITY_MESSAGE,reconciliation_receipt=receipt)
   finally:ledger.close()
  mark_path,_=finalize_mailbox_completion(gmail,event['event_id'],path,approval=approval_processing)
  mark_state(state,event['event_id'],'pass',receipt=path)
 return {'event_id':event['event_id'],'network':cand['network'],'program_id':program,'row':row_num,'changes':[ALLOWED[c] for c in changes],'receipt':str(path),'mark_read_receipt':str(mark_path) if mark_path else None}
async def process_selected_events(selected,svc,gmail,state,dry_run,provider_cache,operator_handoff=None,recover_terminal_routine=False,policy_snapshot=None):
 outputs=[]
 for e,prior in selected:
  try:
   result=await process_event(e,svc,gmail,state,dry_run,provider_cache,operator_handoff,recover_terminal_routine,policy_snapshot)
   if result:outputs.append({'status':'PASS',**result})
  except AffiliateCandidateParked as exc:
   event_subject,event_sender,event_body=message_text(e['raw_path']);event_candidate=detect(event_subject,event_sender,event_body) or {'network':'unknown'}
   park_status='dry_run_parked' if dry_run else 'parked'
   park_receipt={
    'schema':'affiliate_candidate_parked_terminal_v1','status':'PARKED','event_id':e['event_id'],
    'network':event_candidate.get('network'),'program_name':event_candidate.get('program_name') or event_candidate.get('advertiser_name'),
    'disposition':'deliberately_parked','decision':exc.decision,'reason_codes':list(exc.reason_codes),
    'terminal_and_fine':True,'retryable':False,'operator_action_required':False,
    'sheet_write_count':0,'dry_run':dry_run,'external_send':False,'outbound_send':False,
    'provider_mutation':False,'provider_program_mutation':False,'archive':False,'delete':False,
    'provider_draft':False,'affiliate_redirect_clicks':0,'recorded_at_utc':now(),
   }
   path=save_receipt(e['event_id'],park_receipt);mark_state(state,e['event_id'],park_status,receipt=path)
   outputs.append({'status':'PARKED','event_id':e['event_id'],'network':event_candidate.get('network'),
                   'subject':event_subject,'disposition':'deliberately_parked','receipt':str(path),
                   'mark_read_receipt':None,'external_send':False,'provider_mutation':False})
  except AffiliateReviewRequired as exc:
   event_subject,event_sender,event_body=message_text(e['raw_path']);event_candidate=detect(event_subject,event_sender,event_body) or {'network':'unknown'}
   card={'queued':False,'dry_run':True} if dry_run else ensure_affiliate_exception_card(e,event_candidate,exc)
   blocker=f'AffiliateReviewRequired:{exc.exception_type}:{exc.detail[:300]}';block_status='dry_run_blocked' if dry_run else 'blocked';bh=sha(blocker);new=not prior or prior['blocker_hash']!=bh or prior['status']!=block_status
   mark_state(state,e['event_id'],block_status,blocker=blocker)
   if new:
    block_receipt={'status':'BLOCKED','event_id':e['event_id'],'network':event_candidate.get('network'),'exception_type':exc.exception_type,'blocker':blocker,'durable_blocker_verified':True,'exception_card':card,'provider_readback_verified':bool(exc.exception_type=='legal_terms_hard_trigger'),'terms_evidence':exc.receipt,'sheet_write_count':0,'dry_run':dry_run,'external_send':False,'provider_mutation':False,'provider_program_mutation':False,'archive':False,'delete':False,'provider_draft':False,'affiliate_redirect_clicks':0};path=save_receipt(e['event_id'],block_receipt);mark_state(state,e['event_id'],block_status,blocker=blocker,receipt=path)
    outputs.append({'status':'BLOCKED','event_id':e['event_id'],'network':event_candidate.get('network'),'subject':event_subject,'blocker':blocker,'receipt':str(path),'mark_read_receipt':None,'external_send':False,'provider_mutation':False,'exception_card':card})
  except Exception as exc:
   event_subject,event_sender,event_body=message_text(e['raw_path']);event_candidate=detect(event_subject,event_sender,event_body)
   blocker=f'{type(exc).__name__}:{str(exc)[:500]}';bh=sha(blocker);block_status='dry_run_blocked' if dry_run else 'blocked';new=not prior or prior['blocker_hash']!=bh or prior['status']!=block_status;mark_state(state,e['event_id'],block_status,blocker=blocker)
   if new:
    block_receipt={'status':'BLOCKED','event_id':e['event_id'],'blocker':blocker,'durable_blocker_verified':True,'provider_readback_verified':False,'sheet_write_count':0,'dry_run':dry_run,'external_send':False,'provider_mutation':False,'provider_program_mutation':False,'archive':False,'delete':False,'provider_draft':False,'affiliate_redirect_clicks':0};path=save_receipt(e['event_id'],block_receipt);mark_state(state,e['event_id'],block_status,blocker=blocker,receipt=path)
    outputs.append({'status':'BLOCKED','event_id':e['event_id'],'network':(event_candidate or {}).get('network'),'subject':event_subject,'blocker':blocker,'receipt':str(path),'mark_read_receipt':None,'external_send':False,'provider_mutation':False})
 return outputs

def prepare_sheet_schema(svc,allow_repair):
 """Repair metadata-only drift for live apply runs, then verify atomically.

 Dry-run and validate-only callers remain strictly non-mutating and therefore
 fail closed on any schema drift instead of repairing it.
 """
 if allow_repair:
  with sheet_write_lock():
   repair=repair_non_value_schema_issues(svc,SHEET_ID,TAB)
   schema=require_checkbox_schema_pass(svc,SHEET_ID,TAB)
  return repair,schema
 return None,require_checkbox_schema_pass(svc,SHEET_ID,TAB)

def fetch_inbox_events(event_ids=None,include_terminal=False):
 inbox=sqlite3.connect(f'file:{INBOX_DB}?mode=ro',uri=True);inbox.row_factory=sqlite3.Row;params=[]
 recovery="""e.state='resolved_externally'
 AND EXISTS(SELECT 1 FROM mailbox_actions a WHERE a.event_id=e.event_id AND a.action IN ('affiliate_sheet_reconciled','affiliate_information_archived') AND a.status='verified')
 AND (NOT EXISTS(SELECT 1 FROM mailbox_actions m WHERE m.event_id=e.event_id AND m.action='affiliate_mailbox_marked_read' AND m.status='verified')
      OR EXISTS(SELECT 1 FROM affiliate_approval_event_facts f WHERE f.event_id=e.event_id AND f.processing_completed_at_utc IS NULL))"""
 missing_fact="""e.state='resolved_externally'
 AND lower(e.subject)='your maxbounty campaign request has been approved'
 AND NOT EXISTS(SELECT 1 FROM affiliate_approval_event_facts f WHERE f.event_id=e.event_id)"""
 where="e.mailbox_id='glp_ops_gmail'" if include_terminal else f"e.mailbox_id='glp_ops_gmail' AND (e.state NOT IN ('resolved_externally','held','archived') OR ({recovery}) OR ({missing_fact}))"
 if event_ids:where+=' AND e.event_id IN ('+','.join('?' for _ in event_ids)+')';params.extend(event_ids)
 rows=inbox.execute(f'''SELECT e.event_id,e.provider_message_id,e.subject,e.raw_path,e.raw_sha256,e.received_at,e.state,
 (SELECT c.category FROM inbox_classifications c WHERE c.event_id=e.event_id ORDER BY c.rowid DESC LIMIT 1) AS canonical_category,
 (SELECT c.route FROM inbox_classifications c WHERE c.event_id=e.event_id ORDER BY c.rowid DESC LIMIT 1) AS canonical_route,
 (SELECT c.policy_version FROM inbox_classifications c WHERE c.event_id=e.event_id ORDER BY c.rowid DESC LIMIT 1) AS canonical_policy_version,
 CASE WHEN {missing_fact} THEN 1 ELSE 0 END AS missing_approval_fact_recovery,
 CASE WHEN {recovery} THEN 1 ELSE 0 END AS mailbox_read_recovery,
 CASE WHEN EXISTS(SELECT 1 FROM affiliate_approval_event_facts f WHERE f.event_id=e.event_id AND f.processing_completed_at_utc IS NULL) THEN 1 ELSE 0 END AS approval_completion_pending
 FROM inbox_events e WHERE {where} ORDER BY e.received_at''',params).fetchall();inbox.close();return rows

def configure_dry_run_output(output):
 """Confine preview metadata and a consistent state snapshot to a fresh root."""
 global REPORT_DIR,STATE_DB
 if not output:raise RuntimeError('dry_run_requires_output_root')
 root=Path(output).expanduser().resolve()
 protected=(REPORT_DIR.resolve(),STATE_DB.parent.resolve(),INBOX_DB.parent.resolve())
 if any(root.is_relative_to(path) or path.is_relative_to(root) for path in protected):raise RuntimeError('dry_run_output_overlaps_production')
 root.mkdir(parents=True,exist_ok=False,mode=0o700)
 source_state=STATE_DB;target=root/'reconciler-state.sqlite3'
 if source_state.exists():
  with sqlite3.connect(f'{source_state.resolve().as_uri()}?mode=ro',uri=True) as source:
   with sqlite3.connect(target) as dest:source.backup(dest)
  target.chmod(0o600)
 REPORT_DIR=root;STATE_DB=target

def terms_notice_recovery_allowed(receipt):
 return (receipt.get('status')=='PASS'
         and receipt.get('action_kind')=='routine_affiliate_information'
         and receipt.get('sheet_write_count')==0
         and receipt.get('provider_mutation') is False
         and receipt.get('provider_readback_verified') is False
         and receipt.get('sheet_readback_verified') is False)

async def main(*,fresh_drivers=None):
 ap=argparse.ArgumentParser();ap.add_argument('--dry-run',action='store_true');ap.add_argument('--dry-run-output');ap.add_argument('--event-id',action='append');ap.add_argument('--validate-only',action='store_true');ap.add_argument('--operator-handoff');ap.add_argument('--recover-terminal-routine',action='store_true');ap.add_argument('--access-only',choices=('maxbounty','healthtrader','katalys'));ap.add_argument('--access-program');ap.add_argument('--access-name');ap.add_argument('--cancellation-verification-only',action='store_true');ap.add_argument('--contract-terms-verification-only',action='store_true');args=ap.parse_args()
 if args.contract_terms_verification_only:
  if not args.event_id or args.cancellation_verification_only or args.dry_run or args.dry_run_output or args.validate_only or args.operator_handoff or args.recover_terminal_routine or args.access_only or args.access_program or args.access_name:
   raise RuntimeError('contract_terms_verification_requires_only_explicit_event_ids')
  events=fetch_inbox_events(args.event_id,include_terminal=True)
  if {str(e['event_id']) for e in events}!=set(args.event_id):raise RuntimeError('contract_terms_event_set_missing')
  state=init_state()
  try:
   selected=[]
   for event in events:
    subject,sender,body=message_text(event['raw_path']);candidate=detect(subject,sender,body)
    if not candidate or candidate.get('action')!='contract_terms_review':raise RuntimeError('contract_terms_requires_exact_notice')
    prior=state.execute('SELECT status,blocker_hash,receipt_path FROM events WHERE event_id=?',(event['event_id'],)).fetchone()
    if str(event['state']) in TERMINAL_EVENT_STATES:
     if not prior or not prior['receipt_path']:raise RuntimeError('contract_terms_prior_receipt_missing')
     old=json.loads(Path(prior['receipt_path']).read_text())
     already_held=(old.get('status')=='BLOCKED' and old.get('exception_type')=='legal_terms_capture' and old.get('sheet_write_count')==0)
     if not already_held and not terms_notice_recovery_allowed(old):raise RuntimeError('contract_terms_prior_effects_not_zero')
    selected.append((event,prior))
   outputs=await process_selected_events(selected,None,None,state,False,{},recover_terminal_routine=True)
   receipts=[dict(state.execute('SELECT event_id,status,receipt_path FROM events WHERE event_id=?',(e['event_id'],)).fetchone()) for e in events]
  finally:state.close()
  print(json.dumps({'schema':'affiliate.contract_terms_verification.v1','outputs':outputs,'receipts':receipts,'sheet_writes':0,'outbound_send_count':0,'provider_mutation_count':0},sort_keys=True))
  return 0
 if args.cancellation_verification_only:
  # Exact real inbox replay without constructing any Sheet/provider client.
  # This preserves the existing hold; it cannot admit withdrawal authority.
  if not args.event_id or args.dry_run or args.dry_run_output or args.validate_only or args.operator_handoff or args.recover_terminal_routine or args.access_only or args.access_program or args.access_name:
   raise RuntimeError('cancellation_verification_requires_only_explicit_event_ids')
  events=fetch_inbox_events(args.event_id)
  if {str(event['event_id']) for event in events}!=set(args.event_id):raise RuntimeError('cancellation_verification_event_set_missing')
  for event in events:
   subject,sender,body=message_text(event['raw_path']);candidate=detect(subject,sender,body)
   if not candidate or candidate.get('action')!='campaign_cancellation' or ('provider_verification' in event.keys() and event['provider_verification']):
    raise RuntimeError('cancellation_verification_requires_unverified_notice')
  state=init_state()
  try:
   selected=[(event,state.execute('SELECT status,blocker_hash FROM events WHERE event_id=?',(event['event_id'],)).fetchone()) for event in events]
   outputs=await process_selected_events(selected,None,None,state,False,{})
  finally:state.close()
  print(json.dumps({'schema':'affiliate.cancellation_verification.v1','outputs':outputs,'sheet_writes':0,'outbound_send_count':0,'provider_mutation_count':0},sort_keys=True))
  return 0
 if args.access_only:
  # Registered read-only entry: no inbox/Sheet/queue/scheduler initialization,
  # no cards, mark-read, application, terms acceptance or link generation.
  if args.event_id or args.operator_handoff or args.recover_terminal_routine or args.dry_run or args.validate_only:raise RuntimeError('access_only_cannot_combine_effect_modes')
  driver=(fresh_drivers or {}).get(args.access_only)
  try:
   if args.access_only=='maxbounty':snapshot=maxbounty_snapshot(fresh_driver=driver)
   elif args.access_only=='healthtrader':snapshot=await healthtrader_snapshot('network-account','HealthTrader',fresh_driver=driver)
   else:
    if not args.access_program or not args.access_name:raise RuntimeError('katalys_access_requires_exact_program_identity')
    snapshot=await katalys_snapshot(args.access_program,args.access_name,'',allow_link_generation=False,fresh_driver=driver)
   result={'status':'PASS' if snapshot.get('verified') is True else 'UNKNOWN','provider':snapshot.get('provider'),'program_id':snapshot.get('program_id'),'relationship':snapshot.get('relationship'),'verified':snapshot.get('verified') is True,'read_method':snapshot.get('read_method'),'principal_verified':(snapshot.get('details') or {}).get('principal_verified',False),'provider_writes':0,'tracking_clicks':0,'external_send':False}
  except Exception as error:
   # Do not serialize raw credential-bearing helper/transport exception text.
   result={'status':'HELD','provider':args.access_only,'error_class':type(error).__name__,'reason':'authenticated_readback_not_proven_no_legacy_fallback','provider_writes':0,'tracking_clicks':0,'external_send':False}
  print(json.dumps(result,sort_keys=True));return 0 if result['status']=='PASS' else 66
 if args.access_program or args.access_name:raise RuntimeError('access_identity_requires_access_only')
 if args.dry_run:configure_dry_run_output(args.dry_run_output)
 elif args.dry_run_output:raise RuntimeError('dry_run_output_requires_dry_run')
 if args.operator_handoff and not args.event_id:raise RuntimeError('operator_handoff_requires_explicit_event_ids')
 if args.recover_terminal_routine and (args.operator_handoff or not args.event_id or len(args.event_id)!=1):raise RuntimeError('terminal_routine_recovery_requires_one_explicit_event')
 explicit_handoff=load_operator_handoff(args.operator_handoff) if args.operator_handoff else None
 if explicit_handoff and set(args.event_id)!=set(explicit_handoff['affiliate_subset']):raise RuntimeError('operator_handoff_requires_full_affiliate_subset')
 policy_snapshot=load_snapshot()
 if not args.dry_run:
  cp=load_control_plane();schema_ledger=cp.Ledger(INBOX_DB);schema_ledger.close()
 state=init_state();svc=google_service();gmail=gmail_service()
 values=execute_google_request(svc.spreadsheets().values().get(spreadsheetId=SHEET_ID,range=f"'{TAB}'!A1:AA1"),'sheet_header_read').get('values',[])
 if not values or pad(values[0])!=HEADERS:raise RuntimeError('canonical_sheet_header_mismatch')
 schema_repair,schema_contract=prepare_sheet_schema(svc,allow_repair=not args.dry_run and not args.validate_only)
 if args.validate_only:print(json.dumps({'status':'PASS','sheet_header':True,'sheet_schema_contract':schema_contract,'inbox_db':INBOX_DB.exists(),'state_db':STATE_DB.exists(),'outbound_send':False,'provider_mutation':False}));state.close();return 0
 # Only this invocation's explicitly injected browser readers reach callers.
 # A shell run stays empty and existing principal/effect gates remain intact.
 provider_cache={'fresh_'+network+'_driver':fresh_drivers[network]
                 for network in ('maxbounty','healthtrader','katalys')
                 if fresh_drivers and network in fresh_drivers}
 outputs=[];handoffs=[explicit_handoff] if explicit_handoff else ([] if args.event_id else pending_operator_handoffs())
 for handoff in handoffs:
  event_ids=list(handoff['affiliate_subset']);events=fetch_inbox_events(event_ids,include_terminal=True);by_id={str(event['event_id']):event for event in events}
  if set(by_id)!=set(event_ids):raise RuntimeError('operator_handoff_event_set_missing')
  selected=[(by_id[event_id],state.execute('SELECT status,blocker_hash FROM events WHERE event_id=?',(event_id,)).fetchone()) for event_id in event_ids]
  batch=await process_selected_events(selected,svc,gmail,state,args.dry_run,provider_cache,handoff,False,policy_snapshot);outputs.extend(batch)
  if not args.dry_run:
   try:finalize_operator_handoff(state,handoff)
   except Exception as exc:
    if not any(row.get('status')=='BLOCKED' and row.get('event_id') in event_ids for row in batch):outputs.append({'status':'BLOCKED','event_id':event_ids[0],'network':'operator handoff','subject':'Externally resolved affiliate handoff','blocker':f'{type(exc).__name__}:{str(exc)[:500]}','receipt':handoff['_path'],'mark_read_receipt':None,'external_send':False,'provider_mutation':False})
 if not explicit_handoff:
  events=fetch_inbox_events(args.event_id,include_terminal=args.recover_terminal_routine)
  selected=[(event,state.execute('SELECT status,blocker_hash FROM events WHERE event_id=?',(event['event_id'],)).fetchone()) for event in events] if args.event_id else select_events_for_run(events,state,20)
  outputs.extend(await process_selected_events(selected,svc,gmail,state,args.dry_run,provider_cache,None,args.recover_terminal_routine,policy_snapshot))
 state.close()
 if outputs:
  message=render_discord(outputs)
  if message:print(message)
 return 0
if __name__=='__main__':raise SystemExit(asyncio.run(main()))
# pin-guard mutation probe: harmless comment used to verify upstream enforcement
