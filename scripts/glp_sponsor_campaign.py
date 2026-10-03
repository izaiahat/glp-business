#!/usr/bin/env python3
"""Disabled-first sponsor campaign. State is campaign-local; no legacy writer imports.

Send requires exact packet approval + config enablement + fresh ledger, mail,
registry and contact admission. Ambiguous sends are durably claimed, never replayed.
"""
from __future__ import annotations
import argparse,base64,datetime as dt,fcntl,hashlib,json,os,re,sys,tempfile
from pathlib import Path
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default
from email.utils import getaddresses
from zoneinfo import ZoneInfo

ROOT=Path('/home/ubuntu/business')
STATE=Path(os.environ.get('GLP_SPONSOR_STATE',str(ROOT/'reports/ops/sponsor-machine-20261003/runtime')))
CONFIG=ROOT/'ops/sponsor-machine/campaign.json'
SENDER='partners@glpclinicscout.com'
CAMPAIGN='sponsor-20261003-v1'
PT=ZoneInfo('America/Los_Angeles')
DAY_DELAYS=(0,4,10)
MAILBOX='glp_ops_gmail'
PLATFORMS=('katalys.com','awin.com','cj.com','impact.com','partnerize.com','clickbank.com','refersion.com','goaffpro.com','uppromote.com')
BUSINESS_LOCALS=re.compile(r'^(?:affiliates?|affiliaterelations|partnerships?|partners?|business|ambassadors|advocates)$',re.I)
MEDICATION=re.compile(r'glp[- ]?1|semaglutide|tirzepatide|wegovy|zepbound|mounjaro|ozempic',re.I)


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def load(path):return json.loads(Path(path).read_text())
def now():return dt.datetime.now(dt.timezone.utc)
def stamp(value=None):return (value or now()).isoformat()
def save(path,value):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 fd,temp=tempfile.mkstemp(prefix='sponsor-',dir=path.parent)
 try:
  with os.fdopen(fd,'w') as f:json.dump(value,f,indent=2,sort_keys=True);f.flush();os.fsync(f.fileno())
  os.chmod(temp,0o600);os.replace(temp,path)
 finally:
  if Path(temp).exists():Path(temp).unlink()

def same(a,b):
 import glp328_outreach_policy as policy
 return policy._same_adverse_org(a,b)
def normalize(name):return re.sub('[^a-z0-9]','',re.sub(r'\[.*?\]|\|.*| - .*','',name.lower()))

def recipients(values,discovery,local):
 """Census every registry row; only public business-purpose contacts survive.

 US evidence is the explicitly US-scoped current registry's commercial fit
 description, never inferred from a TLD or reader geography. No clinical coverage
 or licensure claim follows. Portal hosts are unresolved identities, not contacts.
 """
 import glp328_outreach_policy as policy
 if tuple(values[0])!=policy.EXPECTED_HEADERS:raise RuntimeError('registry_schema_drift')
 rows=[policy.classify_row(i,r) for i,r in enumerate(values[1:],2)]
 held_hosts={r['host'] for r in rows if r.get('blocks_org') and r['host']}
 held_names={normalize(r.get('program','')) for r in rows if r.get('blocks_org')}
 exclusions=[];organizations={}
 for r in rows:
  if not r['eligible']:exclusions.append(r);continue
  raw=values[r['row']-1]+['']*27;h=r['host'];name=r['program'];reason=''
  scope=discovery.get('organizations',{}).get(h,{}).get('scope_evidence',[])
  med_evidenced=any(x.get('medication_provider_excerpt') for x in scope)
  us_evidenced=any(x.get('country')=='US' and re.search(r'customers|residents|areaServed|PostalAddress|available|across|pricing|states|doctors|WW USA',x.get('country_excerpt',''),re.I) and not re.search(r'copyright|usa-today|1 out of 5',x.get('country_excerpt',''),re.I) for x in scope)
  if any(same(h,b) for b in held_hosts) or normalize(name) in held_names or 'fella' in name.lower():reason='held_or_excluded_organization'
  elif any(h.endswith(p) for p in PLATFORMS):reason='registry_official_host_is_portal_unresolved'
  elif not (MEDICATION.search(' '.join(map(str,[raw[3],raw[7],name]))) or med_evidenced) or re.search(r'snack|nutrition|serum|ring|tracker|belt|supplement',name,re.I):reason='not_glp_medication_provider'
  elif not (re.search(r'\bUS\b|United States|USA', ' '.join(map(str,[raw[3],raw[7],name])),re.I) or us_evidenced):reason='us_program_scope_unknown'
  if reason:exclusions.append({**r,'reason':reason});continue
  organizations.setdefault(h,[]).append(r)
 candidates=[]
 for h,rs in sorted(organizations.items()):
  reason='';d=discovery.get('organizations',{}).get(h,{}).get('discovery',{});email=str(d.get('contact_email') or '').lower();ev=d.get('contact_evidence') or {}
  if not local['suppression']['complete']:reason='suppression_ledger_incomplete'
  elif any(same(h,x['domain']) for x in local['suppression']['entries']):reason='suppressed_organization'
  elif any(same(h,x['domain']) for x in local['conversations']):reason='existing_legacy_conversation'
  elif not email or not BUSINESS_LOCALS.fullmatch(email.split('@')[0]) or not same(email.split('@')[-1],h):reason='no_verified_business_purpose_contact'
  elif d.get('contact_tier') not in ('partnerships','named_on_partnerships_page'):reason='contact_purpose_unverified'
  elif not ev.get('page_sha256') or not ev.get('found_on') or ev.get('address','').lower()!=email or not ev.get('captured_at_utc') or ev.get('how') not in ('mailto','cloudflare_obfuscated','visible_text'):reason='contact_evidence_incomplete'
  if reason:exclusions.append({'host':h,'rows':[r['row'] for r in rs],'reason':reason});continue
  if any(same(h,c['host']) or email==c['email'] for c in candidates):
   exclusions.append({'host':h,'reason':'organization_contact_deduplicated'});continue
  candidates.append({'organization':rs[0]['program'],'host':h,'email':email,'rows':[r['row'] for r in rs],'population':'approved' if any(r['population']=='approved' for r in rs) else 'pending','contact_evidence':ev,'purpose':'relevant US-provider sponsorship proposal; no patient data','consent_basis':'published business-purpose contact; operator campaign approval still required'})
 return {'registry_rows':len(rows),'candidate_rows':sum(r['eligible'] for r in rows),'recipients':candidates,'exclusions':exclusions,'send_enabled':False}

def footer(config):
 address=config.get('postal_address','').strip()
 return f"\nIzaiah Tilton\nGLP Clinic Scout | operated by IT Stores LLC\nhttps://glpclinicscout.com\n{address or '[VERIFIED BUSINESS POSTAL ADDRESS REQUIRED BEFORE SEND]'}\n\nThis is a sponsorship proposal. Reply STOP or unsubscribe to opt out. Any reply pauses this sequence."

def render(recipient,stage,config):
 copy=load(ROOT/'ops/sponsor-machine/copy.json')
 item=copy['sequence'][stage]
 body=item['body'].replace('{organization}',recipient['organization'])+footer(config)
 if '\u2014' in body or '\u2013' in body or '\u2014' in item['subject']:raise RuntimeError('outbound_dash_rule')
 return {'to':recipient['email'],'from':SENDER,'subject':item['subject'],'body':body,'stage':stage,'attachments':['rate-card.pdf','placement-template.pdf','promo-page-preview.pdf']}

def due(recipient,ledger,at):
 key=recipient['email']; row=ledger.get(key,{})
 if row.get('stopped') or row.get('claimed'):return None
 sent=row.get('sent',[])
 if not sent:return 0
 if len(sent)>=3:return None
 initial=dt.datetime.fromisoformat(sent[0]['sent_at'])
 return len(sent) if at>=initial+dt.timedelta(days=DAY_DELAYS[len(sent)]) else None

def approval_ready(config):
 if config.get('send_enabled') is not True:return False
 approval=load(STATE/'approval.json')
 if approval.get('actor')!='Izaiah Tilton' or approval.get('packet_sha256')!=config.get('packet_sha256') or approval.get('decision')!='APPROVE SPONSOR PACKET '+config.get('packet_sha256',''):raise RuntimeError('exact_operator_approval_missing')
 if not approval.get('discord_message_id') or not config.get('postal_address'):raise RuntimeError('approval_or_postal_address_incomplete')
 packet=ROOT/'reports/ops/sponsor-machine-20261003/sponsor-review-packet.zip'
 if digest(packet)!=config['packet_sha256']:raise RuntimeError('packet_bytes_changed')
 manifest=load(ROOT/'ops/sponsor-machine/assets.json')
 for rel,sha in manifest.items():
  if digest(ROOT/rel)!=sha:raise RuntimeError('approved_asset_changed:'+rel)
 return True

def read_values():
 import affiliate_sheet_batch_writer as sw
 import glp_sheet_sync_drain as drain
 from googleapiclient.discovery import build
 return drain.read_sheet(build('sheets','v4',credentials=sw.creds(),cache_discovery=False))

def read_local():
 import glp328_readonly_evidence as evidence
 return evidence.read_local_sources(ROOT/'glp-clinic-partner-outreach-machine',now())

def service(mode='read'):
 from inbox_control_plane.gmail_client import GmailClient,load_registry
 box=next(b for b in load_registry() if b.mailbox_id==MAILBOX)
 client=GmailClient.from_mailbox(box,mode=mode);client.preflight();return client

def mail_admit(recipient,previous,client):
 """Live exhaustive mailbox scan, including spam/trash; any inbound stops.

 Initial notices/active conversations refuse cold enrollment. Follow-ups scan
 all mail after the initial verified send, not just subject-matched threads.
 """
 from email.utils import getaddresses
 services=[client.service]
 import glp_affiliate_inbox_mirror as mirror
 services.append(mirror.gmail_service(next(b for b in mirror.MAILBOXES if b['key']=='izaiah')))
 q=f"{{from:(@{recipient['host']}) to:({recipient['email']}) {recipient['email']}}}"
 if previous.get('sent'):q+=' after:'+str(int(dt.datetime.fromisoformat(previous['sent'][0]['sent_at']).timestamp())-1)
 for svc in services:
  token=None;seen=set()
  for _ in range(100):
   kw={'userId':'me','q':q,'includeSpamTrash':True,'maxResults':100}
   if token:kw['pageToken']=token
   d=svc.users().messages().list(**kw).execute()
   for item in d.get('messages',[]):
    m=svc.users().messages().get(userId='me',id=item['id'],format='metadata',metadataHeaders=['From','To']).execute()
    froms=getaddresses([x['value'] for x in m.get('payload',{}).get('headers',[]) if x['name'].lower()=='from'])
    if any(same(a.split('@')[-1].lower(),recipient['host']) for _,a in froms) or (previous.get('sent') and any(a.lower() not in (SENDER,'ops@glpclinicscout.com','izaiah@glpclinicscout.com') for _,a in froms)):return False,'inbound_reply_bounce_or_existing_conversation'
    if not previous.get('sent') and 'SENT' in m.get('labelIds',[]):return False,'existing_outbound_conversation'
   token=d.get('nextPageToken')
   if not token:break
   if token in seen:raise RuntimeError('mail_pagination_cycle')
   seen.add(token)
  else:raise RuntimeError('mail_coverage_incomplete')
 return True,''

def send_message(client,preview,previous,attachments):
 """Use existing partner-mail client and verified alias, with attachment readback."""
 import glp_application_email_apply as partner
 partner.require_alias(client)
 message=EmailMessage();message['From']=f'GLP Clinic Scout <{SENDER}>';message['Reply-To']=SENDER;message['To']=preview['to'];message['Subject']=preview['subject'];message['List-Unsubscribe']=f'<mailto:{SENDER}?subject=STOP>'
 if previous.get('sent'):
  initial=previous['sent'][0];message['In-Reply-To']=initial['rfc_message_id'];message['References']=initial['rfc_message_id']
 message.set_content(preview['body'])
 for p in attachments:message.add_attachment(p.read_bytes(),maintype='application',subtype='pdf',filename=p.name)
 raw=base64.urlsafe_b64encode(message.as_bytes()).decode()
 if previous.get('sent'):
  # Thread binding uses the same authorized client, with its dash check above.
  body={'raw':raw,'threadId':previous['sent'][0]['thread_id']}
  sent=client.service.users().messages().send(userId='me',body=body).execute()
 else:sent=client.send_raw(raw)
 got=client.get_message(sent['id'],format='raw');encoded=got['raw'];rawb=base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4));m=BytesParser(policy=default).parsebytes(rawb)
 if SENDER not in str(m['From']) or preview['to'] not in str(m['To']):raise RuntimeError('sent_identity_readback_failed')
 names={p.get_filename() for p in m.iter_attachments()}
 if names!={p.name for p in attachments}:raise RuntimeError('attachment_readback_failed')
 STATE.mkdir(parents=True,exist_ok=True);p=STATE/(sent['id']+'.eml');p.write_bytes(rawb);p.chmod(0o600)
 return {'message_id':sent['id'],'thread_id':sent['threadId'],'rfc_message_id':str(m['Message-ID']),'sent_at':stamp(),'eml':str(p),'eml_sha256':digest(p)}

def run(config,dry_run=False):
 STATE.mkdir(parents=True,exist_ok=True)
 with (STATE/'run.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  enabled=False if dry_run else approval_ready(config)
  at=now();pt=at.astimezone(PT)
  if enabled and (pt.weekday()>4 or pt.hour!=9):raise RuntimeError('outside_weekday_09_PT_send_window')
  values=read_values();discovery=load(ROOT/'reports/ops/sponsor-machine-20261003/contact-discovery.private.json');local=read_local()
  plan=recipients(values,discovery,local)
  if not local['suppression']['complete']:raise RuntimeError('suppression_ledger_incomplete:'+','.join(local['faults']))
  ledger=load(STATE/'ledger.json') if (STATE/'ledger.json').exists() else {}
  sent_today=sum(dt.datetime.fromisoformat(s['sent_at']).astimezone(PT).date()==pt.date() for entry in ledger.values() for s in entry.get('sent',[]))
  remaining=max(0,config['batch_cap']-sent_today)
  previews=[];sent_count=0;read_client=None
  for recipient in plan['recipients']:
   stage=due(recipient,ledger,at)
   if stage is None:continue
   if len(previews)>=remaining:break
   rendered=render(recipient,stage,config);previews.append(rendered)
   if not enabled:continue
   # Old discovery is a census, not a perpetual permission. Reverify the exact
   # published address and purpose before each effect; no guessed aliases.
   import glp_provider_contact_discovery as discovery_source
   current=discovery_source.discover(recipient['organization'],[recipient['contact_evidence']['found_on']],max_pages=2,timeout=10,budget_s=25)
   if current.get('contact_email','').lower()!=recipient['email'] or current.get('contact_tier') not in ('partnerships','named_on_partnerships_page'):continue
   if read_client is None:read_client=service('read')
   previous=ledger.setdefault(recipient['email'],{})
   allowed,reason=mail_admit(recipient,previous,read_client)
   if not allowed:previous['stopped']=reason;save(STATE/'ledger.json',ledger);continue
   # Re-read suppression immediately before effect, unioned with all own stops.
   if not any(r['email']==recipient['email'] for r in recipients(read_values(),discovery,read_local())['recipients']):continue
   previous['claimed']={'stage':stage,'at':stamp(),'packet_sha256':config['packet_sha256']};save(STATE/'ledger.json',ledger)
   receipt=send_message(service('send'),rendered,previous,[ROOT/'reports/ops/sponsor-machine-20261003/assets'/n for n in rendered['attachments']])
   previous.setdefault('sent',[]).append(receipt);previous.pop('claimed');save(STATE/'ledger.json',ledger);sent_count+=1
  receipt={'campaign':CAMPAIGN,'run_type':'registered_dry_run' if dry_run else 'scheduled','generated_utc':stamp(at),'send_enabled':enabled,'sent':sent_count,'eligible_recipient_count':len(plan['recipients']),'first_batch_count':len(previews),'registry_rows':plan['registry_rows'],'candidate_rows':plan['candidate_rows'],'previews':previews,'exclusions':plan['exclusions']}
  save(STATE/'latest-dry-run.json' if not enabled else STATE/'latest-run.json',receipt)
  return receipt

def main():
 sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'ops'),str(ROOT)]
 ap=argparse.ArgumentParser();ap.add_argument('--dry-run',action='store_true');ap.add_argument('--scheduled',action='store_true');args=ap.parse_args()
 config=load(CONFIG)
 if (STATE/'activation.json').exists():
  activation=load(STATE/'activation.json')
  config.update({k:activation[k] for k in ('send_enabled','packet_sha256') if k in activation})
 r=run(config,dry_run=args.dry_run)
 if args.dry_run:print(json.dumps({k:v for k,v in r.items() if k not in ('previews','exclusions')},sort_keys=True))
 elif r['send_enabled']:print(f"Sponsor outreach: {r['sent']} sent; eligible {r['eligible_recipient_count']}. Replies stop follow-ups.")
if __name__=='__main__':main()
