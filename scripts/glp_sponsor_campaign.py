#!/usr/bin/env python3
"""Disabled-first sponsor campaign. State is campaign-local; no legacy writer imports.

Send requires exact packet approval + config enablement + fresh ledger, mail,
registry and contact admission. Ambiguous sends are durably claimed, never replayed.
"""
from __future__ import annotations
import argparse,base64,datetime as dt,fcntl,hashlib,json,os,re,sys,tempfile,time
from contextlib import contextmanager
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
INBOX_DB=Path('/home/ubuntu/.hermes/state/email-monitor.sqlite3')
INBOX_POLL_LOCK=Path('/home/ubuntu/.hermes/state/email-monitor-cron.lock')
INBOX_POLL_WAIT_SECONDS=240


def notify_inbox_poll_timeout(job):
 """One existing ops-alerts post; never retry an uncertain webhook submission."""
 import urllib.request
 body=(f'⚠️ **{job}** — canonical inbox poll lock unavailable after 240s\n\n'
       'Outbound sends **0** · eligibility checks **not started** · failed closed\n'
       '→ Inspect the running inbox poll and this job before any manual retry.\n'
       '-# source: scheduled sponsor inbox boundary · internal only')
 try:
  url=(Path.home()/'.hermes/secrets/n8n/ops-alerts-webhook.txt').read_text().strip()
  request=urllib.request.Request(url+('&' if '?' in url else '?')+'wait=true',
   data=json.dumps({'content':body,'allowed_mentions':{'parse':[]}}).encode(),method='POST',
   headers={'Content-Type':'application/json','User-Agent':'glp-sponsor-inbox-boundary/1.0'})
  with urllib.request.urlopen(request,timeout=30) as response:receipt=json.loads(response.read())
  if not receipt.get('id') or str(receipt.get('channel_id'))!='1487300132054765629':
   raise RuntimeError('ops_alert_readback_mismatch')
 except Exception as exc:
  # URL-bearing exceptions are secret; do not expose them or retry a possible post.
  print('sponsor_inbox_lock_alert_unconfirmed:'+type(exc).__name__,file=sys.stderr)


@contextmanager
def inbox_poll_boundary(*,alert=False,job='Sponsor outreach'):
 """Wait for the canonical cron, then retain its stable snapshot through transport.

 Lock order: poll -> campaign -> canonical SQLite boundary. Never unlink the
 shared inode or rerun a poll here. All original eligibility guards still run.
 """
 INBOX_POLL_LOCK.parent.mkdir(parents=True,exist_ok=True)
 with INBOX_POLL_LOCK.open('a+') as poll_lock:
  deadline=time.monotonic()+INBOX_POLL_WAIT_SECONDS
  while True:
   try:
    fcntl.flock(poll_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    break
   except BlockingIOError as exc:
    remaining=deadline-time.monotonic()
    if remaining<=0:
     if alert:notify_inbox_poll_timeout(job)
     raise RuntimeError('canonical_inbox_poll_lock_timeout') from exc
    time.sleep(min(0.25,remaining))
  try:yield
  finally:fcntl.flock(poll_lock,fcntl.LOCK_UN)


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
def require_sponsor_send_window(at=None):
 """Operator window: weekdays, 06:00 inclusive to 17:00 exclusive Pacific."""
 local=(at or now()).astimezone(PT)
 if local.weekday()>=5 or not 6<=local.hour<17:
  raise RuntimeError('outside_approved_weekday_06_17_PT_send_window')


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

def source_on_provider(url,host):
 from urllib.parse import urlsplit
 try:
  u=urlsplit(url);h=(u.hostname or '').lower()
  return u.scheme in ('http','https') and not u.username and not u.password and u.port in (None,80,443) and (h==host or h.endswith('.'+host)) and not u.query
 except (TypeError,ValueError):return False


def public_page_hits(markup):
 """Visible text/mailto/Cloudflare publication, not strings hidden in scripts."""
 from html.parser import HTMLParser
 class Page(HTMLParser):
  def __init__(self):super().__init__(convert_charrefs=True);self.text=[];self.methods={};self.positions={};self.hidden=0
  def handle_starttag(self,tag,attrs):
   if tag in ('script','style'):self.hidden+=1
   if self.hidden:return
   attrs=dict(attrs);href=attrs.get('href') or ''
   if href.lower().startswith('mailto:'):
    address=href[7:].split('?')[0].lower();self.methods[address]='mailto';self.positions[address]=len(' '.join(self.text))
   token=attrs.get('data-cfemail','')
   if token:
    try:
     b=bytes.fromhex(token);address=''.join(chr(x^b[0]) for x in b[1:]).lower();self.methods[address]='cloudflare_obfuscated';self.positions[address]=len(' '.join(self.text))
    except (ValueError,IndexError):pass
  def handle_endtag(self,tag):
   if tag in ('script','style'):self.hidden=max(0,self.hidden-1)
  def handle_data(self,data):
   if not self.hidden:self.text.append(data)
 page=Page();page.feed(markup);text=' '.join(' '.join(page.text).split())
 for address in re.findall(r'[a-zA-Z0-9][a-zA-Z0-9._%+-]*@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',text):page.methods.setdefault(address.lower(),'published_text')
 hits=[]
 for address,how in page.methods.items():
  offset=text.lower().find(address)
  if offset<0:offset=page.positions.get(address,-1)
  context=text[max(0,offset-180):offset+len(address)+180] if offset>=0 else ''
  hits.append({'address':address,'how':how,'excerpt':context})
 return hits,text


def public_business_contact(host,organization,at,exact_email=None):
 """Filter every actual captured candidate BEFORE selection. No guessed alias.

 Historical captures retain their honest decoded-UTF8 representation; new
 effect-boundary captures retain Requests decompressed content bytes. Neither
 is mislabelled wire bytes. Support/creator/infrastructure routes do not qualify.
 """
 import glp_sponsor_readiness as readiness
 d=organization.get('discovery',{});rows=[d.get('contact_evidence') or {}]+list(d.get('candidates') or [])
 for ev in rows:
  address=str(ev.get('address') or '').lower()
  if (not address or (exact_email and address!=exact_email) or address.count('@')!=1
      or not BUSINESS_LOCALS.fullmatch(address.split('@')[0]) or not same(address.split('@')[1],host)
      or ev.get('tier',d.get('contact_tier')) not in ('partnerships','named_on_partnerships_page')
      or not source_on_provider(ev.get('found_on'),host)
      or not readiness.current_evidence({'captured_at_utc':ev.get('captured_at_utc'),'evidence_sha256':ev.get('page_sha256')},at)):continue
  for page in d.get('_markup',[]):
   if (page.get('final_url',page.get('url'))!=ev['found_on'] or not source_on_provider(page.get('url'),host)
       or not source_on_provider(page.get('final_url',page.get('url')),host)):continue
   markup=page.get('html','')
   try:
    if page.get('body_representation')=='requests_decompressed_content':
     body=base64.b64decode(page['body_base64'],validate=True)
     if body.decode(page['encoding'],errors='replace')!=markup:continue
    else:body=markup.encode('utf-8','replace') # exact retained historical representation, NOT original response bytes
   except (ValueError,KeyError,LookupError,TypeError):continue
   if hashlib.sha256(body).hexdigest()!=ev['page_sha256'] or page.get('sha256')!=ev['page_sha256']:continue
   hits,_=public_page_hits(markup)
   for hit in hits:
    method='published_text' if ev.get('how')=='visible_text' else ev.get('how')
    if hit['address']!=address or hit['how']!=method:continue
    context=hit['excerpt']
    if (not re.search(r'partnership|affiliat|business development|marketing|sponsor',context,re.I)
        or re.search(r'patient|billing|customer support|affiliate support|creator recruitment|personal journey|infrastructure',context,re.I)):continue
    return {**ev,'address':address,'how':hit['how'],'excerpt':context}
 return None


def scope_evidence_current(host,organization,at):
 """Supplemental US/medication snippets must be anchored in retained first-party text."""
 import glp_sponsor_readiness as readiness
 out=[]
 for ev in organization.get('scope_evidence',[]):
  if not source_on_provider(ev.get('source_url'),host) or not readiness.current_evidence({'captured_at_utc':ev.get('observed_utc'),'evidence_sha256':ev.get('sha256')},at):continue
  for page in organization.get('discovery',{}).get('_markup',[]):
   markup=page.get('html','')
   if page.get('final_url',page.get('url'))!=ev['source_url'] or hashlib.sha256(markup.encode('utf-8','replace')).hexdigest()!=ev['sha256']:continue
   _,text=public_page_hits(markup);normalize_text=lambda s:' '.join(str(s).split()).casefold()
   if not all(not ev.get(k) or normalize_text(ev[k]) in normalize_text(text) for k in ('country_excerpt','medication_provider_excerpt')):continue
   country=str(ev.get('country_excerpt') or '')
   medication=str(ev.get('medication_provider_excerpt') or '')
   if ev.get('country')=='US' and not (re.search(r'United States|U\.S\.|USA|all 50 states',country,re.I) or re.search(r'\bUS\b',country)):continue
   # Custody of a keyword is not an affirmative medication-provider claim.
   # Ambiguous/negative or nonclinical scope remains unknown, not admitted.
   if medication:
    if (not MEDICATION.search(medication)
        or re.search(r"\b(?:no|not|never|without|cannot|can't|don't|doesn't|isn't|aren't|unable|marketing|advertising|coaching|education|demonstrations?)\b",medication,re.I)
        or not re.search(r'\b(?:we|our (?:doctors|clinicians|providers|prescribers))\s+(?:prescribe|offer|provide)\b',medication,re.I)
        or not re.search(r'\b(?:prescribe|prescription|medication|treatment)\b',medication,re.I)):continue
   out.append(ev);break
 return out


def fresh_public_contact(recipient,at):
 """Existing pre-send refresh: exact address, <=2 requests/25s/10s timeout.

 No generic discovery reranking or root rebinding; reject redirects BEFORE an
 off-property request. Keep response content bytes privately, not wire claims.
 """
 import requests,time
 from urllib.parse import urljoin
 url=recipient['contact_evidence']['found_on'];host=recipient['host'];start=time.monotonic();chain=[]
 if not source_on_provider(url,host):return None
 for _ in range(2):
  remaining=25-(time.monotonic()-start)
  if remaining<=0:return None
  try:
   with requests.get(url,headers={'Accept':'text/html','Accept-Encoding':'gzip, deflate'},timeout=min(10,remaining),allow_redirects=False,stream=True) as response:
    if not source_on_provider(response.url,host):return None
    if response.status_code in (301,302,303,307,308):
     target=urljoin(url,response.headers.get('Location',''))
     if not target or target==url or not source_on_provider(target,host):return None
     chain.append({'request_url':url,'status':response.status_code,'location':target});url=target;continue
    if response.status_code!=200:return None
    chunks=[];length=0
    for chunk in response.iter_content(chunk_size=65536):
     length+=len(chunk)
     if length>2*1024*1024 or time.monotonic()-start>25:return None
     chunks.append(chunk)
    body=b''.join(chunks);encoding=response.encoding or 'utf-8';markup=body.decode(encoding,errors='replace');sha=hashlib.sha256(body).hexdigest()
    captured=stamp(now());hits,_=public_page_hits(markup)
    candidates=[{**hit,'tier':'partnerships','found_on':response.url,'page_sha256':sha,'captured_at_utc':captured} for hit in hits]
    page={'url':url,'final_url':response.url,'html':markup,'sha256':sha,'fetched_at_utc':captured,'bytes':len(body),
          'encoding':encoding,'body_representation':'requests_decompressed_content','body_base64':base64.b64encode(body).decode(),'redirect_chain':chain}
    organization={'discovery':{'candidates':candidates,'_markup':[page]}}
    save(STATE/'contact-captures'/(hashlib.sha256(recipient['email'].encode()).hexdigest()+'.json'),organization['discovery'])
    return public_business_contact(host,organization,now(),exact_email=recipient['email'])
  except (requests.RequestException,ValueError,LookupError,TypeError):return None
 return None


# Targeting policy stays in the already packet-bound campaign module. The supply
# producer may propose evidence, but must not supply executable admission policy.
RESEARCH_ONLY = re.compile(r'research[- ]use[- ]only|for research (?:use|purposes) only|not for human (?:use|consumption)', re.I)
CATEGORY = {
    'telehealth_glp1': re.compile(r'(?:GLP[- ]?1|semaglutide|tirzepatide|Wegovy|Zepbound).{0,100}(?:treatment|prescription|telehealth)|(?:prescri\w+|telehealth|treatment).{0,100}(?:GLP[- ]?1|semaglutide|tirzepatide|Wegovy|Zepbound)', re.I),
    'pharmacy': re.compile(r'(?:compounding|mail[- ]order|specialty) pharmacy|pharmacy.{0,50}(?:compounding|prescription delivery)', re.I),
    'nutrition_protein': re.compile(r'protein (?:shakes?|powder|nutrition)|nutrition shakes?|meal replacement', re.I),
    'meals': re.compile(r'meal delivery|prepared meals|healthy meals|meal plans', re.I),
    'fitness': re.compile(r'fitness (?:app|training|program)|personal training|strength training|workout (?:app|program)', re.I),
    'cgm': re.compile(r'continuous glucose monitor|metabolic health|glucose monitoring', re.I),
    'weight_coaching': re.compile(r'weight[- ]loss (?:app|coaching|program)|weight management (?:app|coaching|program)', re.I),
}
COMMERCIAL = re.compile(r'\b(?:our|we|shop|buy|order|subscribe|membership|plans|products|program|patients|customers|offers?|provid\w+|prescri\w+|get started)\b', re.I)
US_ADDRESS = re.compile(r'\b(?:CA|NY|TX|FL|MA|CO|WA|IL|AZ|NC|GA|PA|NJ|UT|TN|OH|OR|VA|NV|MD|CT|MN|MI|WI|MO|IN|KY|SC|AL|LA|OK|KS|NM|ID|IA|NE|AR|MS|NH|ME|RI|DE|VT|WV|MT|WY|ND|SD|HI|AK|DC)\s+\d{5}(?:-\d{4})?\b')
LEGAL_NEGATOR = re.compile(
    r'\b(?:Do Not Sell(?: or Share)? My Personal Information|Do Not Sell["”] box to remove your consent|'
    r'no limit to the number of successful referrals|cannot be claimed again for subsequent referrals|'
    r'If it is not claimed within this period, the offer will expire|'
    r'not dependent on the referring client making any further successful referrals|'
    r'Timeframes for results are not guaranteed)\b', re.I)
SPONSOR_US_SCOPE = re.compile(r'(?:available|licensed|serv(?:e|es|ing)|operat\w*|residents|customers|patients|ship\w*|deliver\w*)\b.{0,80}(?:United States|U\.S\.|USA|\bUS\b|50 states)|(?:across|throughout|in all)\s+(?:the\s+)?(?:United States|U\.S\.|US|USA|50 states)|based in the United States', re.I)
SPONSOR_US_REJECT = re.compile(r'copyright|©|usa-today|1 out of 5|Pharmacopeia|not available in (?:all|every)|\bnot (?:yet )?(?:licensed|offered)', re.I)


def sponsor_fit(text):
    """Commercial first-party excerpt, not a seed/domain or generic blog keyword."""
    # Refuse ambiguous/negative context before clipping: a long product list
    # must not lose its governing negator. Deliberately prefer false negatives
    # (including double negatives and "not only") to unsupported admission.
    # Same narrow legal contexts as the producer; retain full-page product/RUO gate.
    if RESEARCH_ONLY.search(text) or re.search(r"\b(?:no|not|never|neither|nor|cannot|no longer|\w+n['’]t)\b", LEGAL_NEGATOR.sub('', text), re.I):
        return None
    for category, pattern in CATEGORY.items():
        for match in pattern.finditer(text):
            start, end = max(0, match.start()-120), match.end()+180
            # The receiver rechecks this original quote. Keep recognized literal
            # legal clauses whole rather than leaving unexplained negators.
            for clause in LEGAL_NEGATOR.finditer(text):
                if clause.start() < start < clause.end():
                    start = clause.start()
                if clause.start() < end < clause.end():
                    end = clause.end()
            excerpt = text[start:end].strip()
            if COMMERCIAL.search(excerpt):
                return category, excerpt
    return None


def research_only_organization(host, org, campaign):
    for page in org.get('discovery', {}).get('_markup', []):
        markup = page.get('html', '')
        if campaign.source_on_provider(page.get('final_url', page.get('url')), host) and hashlib.sha256(markup.encode('utf-8', 'replace')).hexdigest() == page.get('sha256'):
            if RESEARCH_ONLY.search(campaign.public_page_hits(markup)[1]):
                return True
    return False


def sponsor_scope_current(host, organization, campaign, at):
    import glp_sponsor_readiness as readiness
    qualified = []
    for ev in organization.get('scope_evidence', []):
        if not campaign.source_on_provider(ev.get('source_url'), host) or not readiness.current_evidence(
                {'captured_at_utc': ev.get('observed_utc'), 'evidence_sha256': ev.get('sha256')}, at):
            continue
        for page in organization.get('discovery', {}).get('_markup', []):
            markup = page.get('html', '')
            if page.get('final_url', page.get('url')) != ev['source_url'] or hashlib.sha256(markup.encode('utf-8', 'replace')).hexdigest() != ev['sha256']:
                continue
            _, text = campaign.public_page_hits(markup)
            if RESEARCH_ONLY.search(text):
                return []
            norm = lambda s: ' '.join(str(s).split()).casefold()
            excerpt = ev.get('sponsor_category_excerpt') or ev.get('country_excerpt')
            if not excerpt or norm(excerpt) not in norm(text):
                continue
            # Recheck the complete bound page, not merely a proposal substring
            # which may have dropped a negator before its category keyword.
            if ev.get('sponsor_category') in CATEGORY and sponsor_fit(text) and sponsor_fit(excerpt) and CATEGORY[ev['sponsor_category']].search(excerpt):
                qualified.append(ev)
            elif ev.get('country') == 'US' and not SPONSOR_US_REJECT.search(excerpt) and (SPONSOR_US_SCOPE.search(excerpt) or US_ADDRESS.search(excerpt)):
                qualified.append(ev)
    return qualified


def sponsor_rows(discovery, registry_rows, local, campaign, at):
    """Supplement the existing recipient census; never write a provider registry row."""
    from glp328_outreach_policy import _same_adverse_org
    held = {r['host'] for r in registry_rows if r.get('blocks_org') and r.get('host')}
    held_names = {campaign.normalize(r.get('program', '')) for r in registry_rows if r.get('blocks_org')}
    blocked = held | {x['domain'] for x in local['suppression']['entries']} | {x['domain'] for x in local['conversations']}
    out = {}
    if not local['suppression']['complete']:
        return out
    for host, org in sorted(discovery.get('organizations', {}).items()):
        name = str(org.get('provider') or host)
        if ('fella' in (host + name).lower() or campaign.normalize(name) in held_names
                or host.endswith(campaign.PLATFORMS) or any(_same_adverse_org(host, h) for h in blocked)
                or research_only_organization(host, org, campaign)):
            continue
        scope = sponsor_scope_current(host, org, campaign, at)
        if any(x.get('sponsor_category') for x in scope) and any(x.get('country') == 'US' for x in scope):
            out[host] = [{'program': name, 'host': host, 'row': None, 'population': 'pending', 'eligible': True}]
    return out


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
  scope=scope_evidence_current(h,discovery.get('organizations',{}).get(h,{}),now())
  med_evidenced=any(x.get('medication_provider_excerpt') for x in scope)
  us_evidenced=any(x.get('country')=='US' and re.search(r'customers|residents|areaServed|PostalAddress|available|across|pricing|states|doctors|WW USA',x.get('country_excerpt',''),re.I) and not re.search(r'copyright|usa-today|1 out of 5',x.get('country_excerpt',''),re.I) for x in scope)
  if any(same(h,b) for b in held_hosts) or normalize(name) in held_names or 'fella' in name.lower():reason='held_or_excluded_organization'
  elif research_only_organization(h,discovery.get('organizations',{}).get(h,{}),sys.modules[__name__]):reason='research_use_only_excluded'
  elif any(h.endswith(p) for p in PLATFORMS):reason='registry_official_host_is_portal_unresolved'
  elif not (MEDICATION.search(' '.join(map(str,[raw[3],raw[7],name]))) or med_evidenced) or re.search(r'snack|nutrition|serum|ring|tracker|belt|supplement',name,re.I):reason='not_glp_medication_provider'
  elif not (re.search(r'\bUS\b|United States|USA', ' '.join(map(str,[raw[3],raw[7],name])),re.I) or us_evidenced):reason='us_program_scope_unknown'
  if reason:exclusions.append({**r,'reason':reason});continue
  organizations.setdefault(h,[]).append(r)
 # Sponsor discovery is a separate commercial census, not a registry mutation.
 for h,rs in sponsor_rows(discovery,rows,local,sys.modules[__name__],now()).items():
  organizations.setdefault(h,rs)
 candidates=[]
 import glp_sponsor_readiness as readiness
 for h,rs in sorted(organizations.items()):
  organization=discovery.get('organizations',{}).get(h,{})
  professional=readiness.professional_contact(h,organization.get('professional_contacts',[]),now())
  reason='';d=organization.get('discovery',{});email=str(d.get('contact_email') or '').lower();ev=d.get('contact_evidence') or {}
  public=None if professional else public_business_contact(h,organization,now())
  if professional:
   email=professional['email'].lower();ev=professional
  elif public:email=public['address'];ev=public
  if not local['suppression']['complete']:reason='suppression_ledger_incomplete'
  elif any(same(h,x['domain']) for x in local['suppression']['entries']):reason='suppressed_organization'
  elif any(same(h,x['domain']) for x in local['conversations']):reason='existing_legacy_conversation'
  elif h in ('nuimage.com','nuimagemedical.com') or re.search(r'nu\s*image',rs[0]['program'],re.I):reason='nu_image_warm_conversation_excluded'
  elif not professional and (not email or not BUSINESS_LOCALS.fullmatch(email.split('@')[0]) or not same(email.split('@')[-1],h)):reason='no_verified_business_purpose_contact'
  elif not professional and ev.get('tier',d.get('contact_tier')) not in ('partnerships','named_on_partnerships_page'):reason='contact_purpose_unverified'
  elif not professional and (not ev.get('page_sha256') or not ev.get('found_on') or ev.get('address','').lower()!=email or not ev.get('captured_at_utc') or ev.get('how') not in ('mailto','cloudflare_obfuscated','visible_text','published_text')):reason='contact_evidence_incomplete'
  elif not professional and not public:reason='public_source_or_role_evidence_unknown'
  if reason:exclusions.append({'host':h,'rows':[r['row'] for r in rs],'reason':reason});continue
  if any(same(h,c['host']) or email==c['email'] for c in candidates):
   exclusions.append({'host':h,'reason':'organization_contact_deduplicated'});continue
  candidates.append({'organization':rs[0]['program'],'host':h,'email':email,'rows':[r['row'] for r in rs],'population':'approved' if any(r['population']=='approved' for r in rs) else 'pending','contact_evidence':ev,'purpose':'relevant US-provider sponsorship proposal; no patient data','consent_basis':'published business-purpose contact; operator campaign approval still required'})
  relation=readiness.relationship(h,organization.get('relationship_evidence',{}),now())
  candidates[-1].update(contact_source='apollo' if professional else 'published_business_contact',relationship=relation,verification=readiness.bouncer_readiness(email,ROOT/'reports/bouncer-shared-credit-ledger-2026-06-25.jsonl',now()))
 return {'registry_rows':len(rows),'candidate_rows':sum(r['eligible'] for r in rows),'recipients':candidates,'exclusions':exclusions,'send_enabled':False}

def footer(config):
 address=config.get('postal_address','').strip()
 return f"\nIzaiah Tilton\nGLP Clinic Scout | operated by Tilton Group LLC\nhttps://glpclinicscout.com\n{address or '[VERIFIED BUSINESS POSTAL ADDRESS REQUIRED BEFORE SEND]'}\n\nThis is a sponsorship proposal. Reply STOP or unsubscribe to opt out. Any reply pauses this sequence."

def organization_display(value):
 # Preserve raw registry/offer evidence; remove only display dashes authorized
 # for this campaign. Never scrub the completed message to bypass its guard.
 from inbox_control_plane.outbound_style_guard import DASH_CHARS
 return re.sub(' *['+re.escape(DASH_CHARS)+']+ *',' ',value)


def render(recipient,stage,config):
 copy=load(ROOT/'ops/sponsor-machine/copy.json')
 relation=recipient.get('relationship',{'variant':'cold'})
 variant=relation.get('variant','cold')
 if 'variants' in copy:
  if variant=='established_affiliate':
   import glp_sponsor_readiness as readiness
   relation=readiness.relationship(recipient['host'],relation.get('evidence',{}),now())
   variant=relation['variant']
  item=copy['variants'][variant][stage]
 else:item=copy['sequence'][stage]
 body=item['body'].replace('{organization}',organization_display(recipient['organization'])).replace('{network}',relation.get('network',''))+footer(config)
 subject=item['subject'].replace('{organization}',organization_display(recipient['organization']))
 if '\u2014' in body or '\u2013' in body or '\u2014' in item['subject']:raise RuntimeError('outbound_dash_rule')
 return {'to':recipient['email'],'from':SENDER,'subject':subject,'variant':variant,'body':body,'stage':stage,'attachments':['rate-card.pdf','placement-template.pdf','promo-page-preview.pdf']}

def due(recipient,ledger,at):
 key=recipient['email']; row=ledger.get(key,{})
 host=recipient.get('host',key.split('@')[-1])
 for email,entry in ledger.items():
  if email!=key and same(host,entry.get('host',email.split('@')[-1])):
   # A new address is neither a new provider nor permission to migrate an
   # existing sequence. Claims and stops survive contact/alias changes.
   if entry.get('sent') or entry.get('claimed') or entry.get('stopped'):return None
 if row.get('stopped') or row.get('claimed'):return None
 sent=row.get('sent',[])
 if not sent:return 0
 if len(sent)>=3:return None
 initial=dt.datetime.fromisoformat(sent[0]['sent_at']).astimezone(PT)
 last=dt.datetime.fromisoformat(sent[-1]['sent_at']).astimezone(PT)
 # Day 4 and day 10 are Pacific calendar days from enrollment, not rolling
 # delays from a late follow-up or its variable transport minute. The weekday
 # job and final transport gate defer weekend/out-of-window dates safely.
 day=initial.date()+dt.timedelta(days=DAY_DELAYS[len(sent)])
 if len(sent)>1:day=max(day,last.date()+dt.timedelta(days=1))
 earliest=dt.datetime.combine(day,dt.time(6),tzinfo=PT)
 return len(sent) if at>=earliest else None

def campaign_config():
 config=load(ROOT/'ops/sponsor-machine/campaign.json')
 activation=STATE/'activation.json'
 if activation.exists():
  switch=load(activation)
  if (set(switch)!={'send_enabled','packet_sha256'} or type(switch['send_enabled']) is not bool
      or not re.fullmatch('[0-9a-f]{64}',str(switch.get('packet_sha256','')))):
   raise ValueError('sponsor_activation_invalid')
  config.update(switch)
 return config


# This is one already-reviewed technical transition, not an arbitrary successor
# authorizer. The immutable personal approval stays in approval.json. Only the
# parent/coordinator may write technical-receiving.json after independent review;
# local custody has the same trust boundary as the existing personal receipt.
TECHNICAL_PREDECESSOR_SHA256='7ec96bf1fe44ef69fc4c7a5c2f1090420bff3d6e15140d00c02c1ac93444d95c'
TECHNICAL_SOURCE_SHA256={
 'scripts/firecrawl_client.py':'d850907ff65c53ba60ae5f51f485408484a599851b1792b80d7ec067d3bb6d3b',
 'scripts/glp328_readonly_evidence.py':'bf1854217b20be3b551994f882b1e7fb4c4971930ed195cec42c714dbc4fbaee',
 'scripts/glp_sponsor_paypal.py':'99751607fcb43551b7f58ca7d5b9236291786aaf0e8a503abce98ce2cd71d3f7',
 'ops/linear/approvals/OPERATOR-DECISION-20261008-paypal-ownership-attestation.md':'9b5cd76a5e81c5d7e5e1328888f6501ebafb1fb4bbb9621da9ea855dae5981e2',
}
# Exact current classifier closure, newly bound by the sealed sender's
# immutable code pins. These bytes are NOT permission to change shared discovery
# behavior or import its older application cadence into this campaign.
TECHNICAL_ADMISSION_SOURCE_SHA256={
 'scripts/glp328_outreach_policy.py':'1f3bb51c465a98fd5bc3da566f44e20731587123d2200792bc31e9b09fde3d21',
 'scripts/affiliate_provider_row_scope.py':'4c2eb769723efbde2c7cb6fcb5f5bd7da2b236333dc1376098cd08056db4af90',
 'scripts/affiliate_sheet_schema_guard.py':'c224fd6c67547af345e8d06fefaadc95f37816df70091d3ad088a3ae472c0d99',
}


def technical_packet_members(packet):
 import zipfile,stat
 from pathlib import PurePosixPath
 try:
  with zipfile.ZipFile(packet) as archive:
   infos=archive.infolist();names=[i.filename for i in infos]
   if (len(names)!=len(set(names)) or sum(i.file_size for i in infos)>16*1024*1024
       or any(not n or '\\' in n or PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts or str(PurePosixPath(n))!=n for n in names)
       or any(i.is_dir() or stat.S_ISLNK(i.external_attr>>16) for i in infos)):
    raise RuntimeError('technical_packet_member_ambiguous')
   return {n:archive.read(n) for n in names}
 except (zipfile.BadZipFile,KeyError,ValueError,TypeError) as error:
  raise RuntimeError('technical_packet_binding_missing') from error


def technical_equivalence(previous,current,sender_sha256):
 """Exact original outward/economics/cadence/assets; four fixed technical pins.

 Sender changes are allowed only at the exact independently reviewed digest
 recorded by the receiving parent, never via a caller-supplied path allowlist.
 """
 manifest='ops/sponsor-machine/assets.json';sender='scripts/glp_sponsor_campaign.py'
 allowed=set(TECHNICAL_SOURCE_SHA256)|{manifest,sender}
 if set(current)!=set(previous)|set(TECHNICAL_SOURCE_SHA256):raise RuntimeError('technical_packet_scope_changed')
 for rel,data in previous.items():
  if rel not in allowed and current[rel]!=data:raise RuntimeError('technical_outward_bytes_changed:'+rel)
 for rel,sha in TECHNICAL_SOURCE_SHA256.items():
  if hashlib.sha256(current[rel]).hexdigest()!=sha:raise RuntimeError('technical_source_not_reviewed:'+rel)
 if (not re.fullmatch('[0-9a-f]{64}',str(sender_sha256))
     or hashlib.sha256(current[sender]).hexdigest()!=sender_sha256):raise RuntimeError('technical_sender_not_reviewed')
 for members in (previous,current):
  sealed=json.loads(members[manifest])
  if not isinstance(sealed,dict) or manifest in sealed:raise RuntimeError('technical_manifest_invalid')
  for rel,sha in sealed.items():
   if rel not in members or hashlib.sha256(members[rel]).hexdigest()!=sha:raise RuntimeError('technical_manifest_binding_missing')
 old=json.loads(previous[manifest]);new=json.loads(current[manifest])
 if set(new)!=set(old)|set(TECHNICAL_SOURCE_SHA256):raise RuntimeError('technical_manifest_scope_changed')
 if any(new.get(rel)!=sha for rel,sha in old.items() if rel not in allowed):raise RuntimeError('technical_manifest_outward_changed')
 if new.get(sender)!=sender_sha256 or any(new.get(rel)!=sha for rel,sha in TECHNICAL_SOURCE_SHA256.items()):raise RuntimeError('technical_manifest_source_missing')


def coordinator_technical_ready(approval,config):
 # Never reuse the old message as approval of the successor. This branch accepts
 # only the actual original personal approval plus a distinct coordinator act.
 if (approval.get('actor')!='Izaiah Tilton' or approval.get('packet_sha256')!=TECHNICAL_PREDECESSOR_SHA256
     or approval.get('decision')!='APPROVE SPONSOR PACKET '+TECHNICAL_PREDECESSOR_SHA256
     or approval.get('discord_message_id')!='1557532710128324609'):
  raise RuntimeError('exact_operator_approval_missing')
 path=STATE/'technical-receiving.json'
 if not path.is_file():raise RuntimeError('exact_operator_approval_missing')
 def unique_authority_object(pairs):
  result={}
  for key,value in pairs:
   if key in result:raise ValueError('duplicate_json_key:'+key)
   result[key]=value
  return result
 # Decode this new authority representation strictly at every object depth;
 # field-set validation after permissive decoding cannot detect contradictions.
 receipt=json.loads(path.read_text(),object_pairs_hook=unique_authority_object)
 fields={'schema','actor','coordinator','parent_session_id','packet_sha256','predecessor_sha256','decision','reviewed_source_head','reviewed_sender_sha256','reviewed_technical_sources'}
 if (set(receipt)!=fields or receipt.get('schema')!='sponsor.coordinator_technical_receiving.v1'
     or receipt.get('actor')!='coordinator' or receipt.get('coordinator')!='Kael'
     or receipt.get('parent_session_id')!='20260922_191215_fffbbb'
     or receipt.get('packet_sha256')!=config.get('packet_sha256')
     or receipt.get('predecessor_sha256')!=TECHNICAL_PREDECESSOR_SHA256
     or receipt.get('decision')!='RECEIVE SPONSOR TECHNICAL SUCCESSOR '+config.get('packet_sha256','')
     or not re.fullmatch('[0-9a-f]{40}',str(receipt.get('reviewed_source_head','')))
     or receipt.get('reviewed_technical_sources')!=TECHNICAL_SOURCE_SHA256):
  raise RuntimeError('coordinator_technical_receiving_invalid')
 predecessor=ROOT/'reports/ops/sponsor-autonomous-20261007/review-packet/sponsor-review-packet.zip'
 if digest(predecessor)!=TECHNICAL_PREDECESSOR_SHA256:raise RuntimeError('technical_predecessor_changed')
 packet=ROOT/'reports/ops/sponsor-machine-20261003/sponsor-review-packet.zip'
 technical_equivalence(technical_packet_members(predecessor),technical_packet_members(packet),receipt['reviewed_sender_sha256'])
 for rel,sha in TECHNICAL_ADMISSION_SOURCE_SHA256.items():
  if not (ROOT/rel).is_file() or digest(ROOT/rel)!=sha:raise RuntimeError('technical_admission_source_changed:'+rel)


def approval_ready(config):
 if config.get('send_enabled') is not True:return False
 from glp_sponsor_current_receiving import approval_ready as current_receiving_ready
 current=current_receiving_ready(sys.modules[__name__],config)
 if current is not None:return current
 approval=load(STATE/'approval.json')
 if approval.get('discord_message_id')=='1557532710128324609' and approval.get('packet_sha256')!=TECHNICAL_PREDECESSOR_SHA256:
  raise RuntimeError('original_personal_message_scope_changed')
 if approval.get('actor')!='Izaiah Tilton' or approval.get('packet_sha256')!=config.get('packet_sha256') or approval.get('decision')!='APPROVE SPONSOR PACKET '+config.get('packet_sha256',''):
  coordinator_technical_ready(approval,config)
 if not approval.get('discord_message_id') or not config.get('postal_address'):raise RuntimeError('approval_or_postal_address_incomplete')
 packet=ROOT/'reports/ops/sponsor-machine-20261003/sponsor-review-packet.zip'
 if digest(packet)!=config['packet_sha256']:raise RuntimeError('packet_bytes_changed')
 manifest=load(ROOT/'ops/sponsor-machine/assets.json')
 import zipfile
 try:
  with zipfile.ZipFile(packet) as archive:
   names=archive.namelist()
   if len(names)!=len(set(names)) or any(Path(n).is_absolute() or '..' in Path(n).parts for n in names):
    raise RuntimeError('approved_packet_member_ambiguous')
   sealed=json.loads(archive.read('ops/sponsor-machine/assets.json'))
   if manifest!=sealed:raise RuntimeError('approved_manifest_not_in_packet')
   for rel,sha in sealed.items():
    target=(ROOT/rel).resolve()
    if not target.is_relative_to(ROOT.resolve()) or hashlib.sha256(archive.read(rel)).hexdigest()!=sha:
     raise RuntimeError('approved_packet_source_changed:'+rel)
 except (zipfile.BadZipFile,KeyError,ValueError,TypeError) as error:
  raise RuntimeError('approved_packet_binding_missing') from error
 if config.get('require_full_source_binding') is True:
  required={'scripts/glp_sponsor_campaign.py','scripts/glp_sponsor_paypal.py','scripts/glp_sponsor_readiness.py','ops/sponsor-machine/campaign.json','ops/sponsor-machine/copy.json','inbox_control_plane/control_plane.py','inbox_control_plane/sponsor_replies.py'}
  if config.get('bouncer_debits_enabled') is True:required.add('scripts/bouncer_shared_account_guard.py')
  if config.get('professional_enrichment_enabled') is True:required.add('scripts/firecrawl_client.py')
  if not required.issubset(manifest):raise RuntimeError('current_sponsor_source_binding_missing')
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

def prepare_message(client,preview,previous,attachments,*,at=None):
 """Shared alias, style, MIME and attachment admission; no send effect."""
 require_sponsor_send_window(at)
 import glp_application_email_apply as partner
 from inbox_control_plane.outbound_style_guard import assert_dash_free
 assert_dash_free(preview['subject']+'\n'+preview['body'])
 partner.require_alias(client)
 message=EmailMessage();message['From']=f'GLP Clinic Scout <{SENDER}>';message['Reply-To']=SENDER;message['To']=preview['to'];message['Subject']=preview['subject'];message['List-Unsubscribe']=f'<mailto:{SENDER}?subject=STOP>'
 if previous.get('sent'):
  initial=previous['sent'][0];message['In-Reply-To']=initial['rfc_message_id'];message['References']=initial['rfc_message_id']
 message.set_content(preview['body'])
 for p in attachments:message.add_attachment(p.read_bytes(),maintype='application',subtype='pdf',filename=p.name)
 return base64.urlsafe_b64encode(message.as_bytes()).decode()


def send_message(client,preview,previous,attachments):
 """Use existing partner-mail client and verified alias, with attachment readback."""
 raw=prepare_message(client,preview,previous,attachments)
 require_sponsor_send_window()  # Alias/authentication/attachments may cross 17:00.
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

def refresh_apollo_sources(plan,at,transport=None):
 """Bounded read-only sourcing within this same job; no second queue/sender."""
 import glp_sponsor_readiness as readiness
 cache_path=STATE/'apollo-sourcing.private.json'
 cache=load(cache_path) if cache_path.exists() else {}
 gaps={'no_verified_business_purpose_contact','contact_purpose_unverified','contact_evidence_incomplete'}
 targets=sorted({r['host'] for r in plan['recipients']}|{r['host'] for r in plan['exclusions'] if r.get('reason') in gaps})
 calls=0;missing=0
 for h in targets:
  if h in ('nuimage.com','nuimagemedical.com'):continue
  retained=cache.get(h,{})
  if not readiness.current_evidence(retained,at,days=7) and calls<25:
   try:
    sourcing=readiness.apollo_search(h,transport or readiness.apollo_transport)
    sourcing['captured_at_utc']=stamp(at)
    sourcing['evidence_sha256']=hashlib.sha256(json.dumps(sourcing,sort_keys=True).encode()).hexdigest()
    # Discovery expires; paid attempt/claim/result custody does not. A fresh
    # zero-credit search cannot reconcile or reopen an irreversible attempt.
    retained.update(sourcing)
    cache[h]=retained;calls+=1
   except (RuntimeError,OSError,ValueError):
    missing+=1
    # An unavailable account is not zero supply and does not permit a paid edge.
    break
  # Zero-credit records lack emails: retained for professional sourcing only,
  # never promoted into the admitted professional_contacts evidence schema.
 save(cache_path,cache)
 return {'status':'sourcing_only' if not missing else 'account_or_transport_unavailable','requests':calls,
         'provider_records':len(cache),'email_verification':'unknown','enrichment_debits':0,'verification_debits':0}


def refresh_professional_sources(plan,discovery,at,config,transport=None,bouncer_runner=None):
 """Actual supported Apollo match + shared Bouncer guard in this existing job.

 The caller enters only after exact approval; flags remain disabled in source.
 Retain requests/results in the existing sourcing cache. Uncertain debits are
 held for manual reconciliation, never automatically repeated or upgraded.
 """
 import glp_sponsor_readiness as readiness,subprocess
 if config.get('professional_enrichment_enabled') is not True or config.get('bouncer_debits_enabled') is not True:
  return {'status':'disabled_pending_approval','enrichment_requests':0,'verification_requests':0}
 transport=transport or readiness.apollo_transport
 runner=bouncer_runner or subprocess.run
 cache_path=STATE/'apollo-sourcing.private.json'
 cache=load(cache_path) if cache_path.exists() else {}
 gaps={'no_verified_business_purpose_contact','contact_purpose_unverified','contact_evidence_incomplete'}
 targets=sorted({r['host'] for r in plan['exclusions'] if r.get('reason') in gaps})
 # Failed/ambiguous prior contacts retain holds but cannot permanently occupy
 # the first 25 source positions and starve every later eligible provider.
 targets=[h for h in targets if not cache.get(h,{}).get('attempted_at')
          or readiness.professional_contact(h,cache.get(h,{}).get('professional_contacts',[]),at)][:25]
 counts={'status':'completed_bounded','enrichment_requests':0,'verification_requests':0,'verified':0,'unknown_or_blocked':0}
 for host in targets:
  if host in ('nuimage.com','nuimagemedical.com'):continue
  row=cache.setdefault(host,{})
  if row.get('enrichment_claim') or row.get('verification_claim'):
   counts['unknown_or_blocked']+=1;continue
  contacts=row.get('professional_contacts',[])
  contact=readiness.professional_contact(host,contacts,at)
  if not contact:
   if row.get('attempted_at'):
    counts['unknown_or_blocked']+=1;continue
   row['attempted_at']=stamp(at);save(cache_path,cache)
   try:
    search=transport('POST','/api/v1/mixed_people/api_search',{'q_organization_domains_list':[host],
       'person_locations':['United States'],'person_titles':['partnerships','marketing','business development','founder','CEO','COO'],
       'page':1,'per_page':25})
    row['search_response']=search;save(cache_path,cache)
    person=next((p for p in search.get('people',[]) if p.get('id') and readiness.PROFESSIONAL.search(p.get('title',''))),None)
    if not person:counts['unknown_or_blocked']+=1;continue
    payload={'id':person['id'],'domain':host,'reveal_personal_emails':False,'reveal_phone_number':False,
             'run_waterfall_email':False,'run_waterfall_phone':False}
    row['enrichment_claim']={'person_id':person['id'],'at':stamp(at)};save(cache_path,cache)
    response=transport('POST','/api/v1/people/match',payload);counts['enrichment_requests']+=1
    row['enrichment_response']=response;row.pop('enrichment_claim');save(cache_path,cache)
    p=response.get('person') or {}
    country=p.get('country')
    candidate={'source':'apollo','person_id':p.get('id'),'email':p.get('email'),'title':p.get('title',''),
        'country':'US' if country in ('United States','US') else country,
        'organization_domain':(p.get('organization') or {}).get('primary_domain',''),
        'captured_at_utc':stamp(at),'evidence_sha256':hashlib.sha256(json.dumps(response,sort_keys=True).encode()).hexdigest()}
    contact=readiness.professional_contact(host,[candidate],at)
    if not contact:counts['unknown_or_blocked']+=1;continue
    row['professional_contacts']=[contact];save(cache_path,cache)
   except (RuntimeError,OSError,ValueError,TypeError):
    counts['unknown_or_blocked']+=1;counts['status']='account_or_credit_access_unconfirmed';break
  discovery.setdefault('organizations',{}).setdefault(host,{})['professional_contacts']=[contact]
  shared=ROOT/'reports/bouncer-shared-credit-ledger-2026-06-25.jsonl'
  verified=readiness.bouncer_readiness(contact['email'],shared,at)
  if verified['send_eligible'] is not True and not row.get('verification_attempted_at'):
   row['verification_claim']={'email_hash':readiness.email_hash(contact['email']),'at':stamp(at)};save(cache_path,cache)
   try:
    outcome=runner([sys.executable,str(ROOT/'scripts/bouncer_shared_account_guard.py'),'verify-email',
       '--venture','glp-scout','--email',contact['email'],'--ref',CAMPAIGN+':'+host,'--timeout','10'],
       capture_output=True,text=True,timeout=90)
    counts['verification_requests']+=1
    if outcome.returncode not in (0,3):raise RuntimeError('bouncer_guard_access_unconfirmed')
    row['verification_attempted_at']=stamp(at);row.pop('verification_claim');save(cache_path,cache)
    # CLI output alone cannot verify: require the actual shared guard receipt.
    verified=readiness.bouncer_readiness(contact['email'],shared,at)
   except (RuntimeError,OSError,subprocess.TimeoutExpired):
    counts['unknown_or_blocked']+=1;counts['status']='account_or_credit_access_unconfirmed';break
  counts['verified' if verified['send_eligible'] else 'unknown_or_blocked']+=1
 save(cache_path,cache)
 return counts


def consume_reply_stops(ledger,db=None,at=None):
 """Consume stops and require fresh canonical cursor proof, never table emptiness."""
 import sqlite3,contextlib
 result={'status':'unknown','applied':0}
 if db is None and not INBOX_DB.exists():return result
 at=at or now()
 try:
  manager=contextlib.nullcontext(db) if db is not None else contextlib.closing(sqlite3.connect(INBOX_DB.as_uri()+'?mode=ro',uri=True))
  with manager as connection:
   rows=connection.execute("SELECT provider_receipt_json FROM mailbox_actions WHERE action='sponsor_reply_stop' AND status='verified' ORDER BY created_at,action_id").fetchall()
   for (raw,) in rows:
    stop=json.loads(raw)
    for email,entry in ledger.items():
     host=entry.get('host',email.split('@')[-1])
     if email in stop['enrolled_addresses'] or any(same(host,h) for h in stop['provider_hosts']):
      if entry.get('stopped') in {'opt_out','unsubscribe','complaint','bounce','not_interested'}:continue
      if entry.get('stop_event_id')==stop['event_id']:continue
      entry.update(stopped=stop['kind'],stop_event_id=stop['event_id'],reply_state=stop)
      result['applied']+=1
   try:
    run_columns={row[1] for row in connection.execute('PRAGMA table_info(inbox_runs)')}
    skipped='r.non_inbound_count' if 'non_inbound_count' in run_columns else '0'
    cursors=connection.execute("SELECT m.mailbox_id,m.lifecycle,m.freshness_sla_seconds,c.last_auth_success_at,c.last_read_success_at,c.last_complete_poll_at,c.unhealthy_reason,c.poll_receipt_id,r.finished_at,r.exit_status,r.discovered_count,r.persisted_count,r.duplicate_count,"+skipped+" FROM mailboxes m JOIN provider_cursors c USING(mailbox_id) JOIN inbox_runs r ON r.run_id=c.poll_receipt_id WHERE m.mailbox_id IN ('glp_ops_gmail','glp_izaiah_gmail')").fetchall()
    if {r[0] for r in cursors}!={'glp_ops_gmail','glp_izaiah_gmail'}:return result
    # Closure includes only provider-labelled non-inbound drafts/sent copies,
    # not unknown ingestion refusals or deferred unseen messages.
    for box,lifecycle,sla,auth,read,complete,unhealthy,receipt,finished,status,found,persisted,duplicates,non_inbound in cursors:
     if (lifecycle!='active' or not receipt or status!='pass' or not finished or unhealthy
         or non_inbound<0 or found!=persisted+duplicates+non_inbound):return result
     for timestamp in (auth,read,complete,finished):
      age=at-dt.datetime.fromisoformat(timestamp)
      if age<dt.timedelta(0):return result
      if age>dt.timedelta(seconds=min(int(sla),900)):
       result['reason']='stale_complete_poll'
       return result
    result.update(status='read_complete',mailboxes=[r[0] for r in cursors])
   except (sqlite3.Error,ValueError,TypeError):
    return result
 except (sqlite3.Error,ValueError,KeyError,TypeError):
  raise RuntimeError('canonical_sponsor_reply_coverage_incomplete')
 return result


def refresh_canonical_poll():
 """Use the installed read-only inbox producer under our existing poll flock.

 Never call the flock-taking cron wrapper recursively. No dispatch, decisions,
 provider drafts or sends; the canonical producer alone owns cursor writes.
 This call must finish before opening the SQLite send transaction.
 """
 import subprocess
 try:
  proc=subprocess.run([sys.executable,str(ROOT/'inbox_control_plane/control_plane.py'),
                       '--poll','--limit','250'],capture_output=True,text=True,timeout=240)
  receipt=json.loads(proc.stdout)
  if (proc.returncode!=0 or receipt.get('status')!='pass' or not receipt.get('run_id')
      or receipt.get('outbound_send_count')!=0 or receipt.get('provider_draft_count')!=0
      or receipt.get('provider_mutation_count')!=0 or receipt.get('secret_printed') is not False):
   raise ValueError('poll_receipt_refused')
 except (subprocess.TimeoutExpired,OSError,ValueError,TypeError,AttributeError) as exc:
  # Provider stdout/stderr and exception strings may contain private data.
  raise RuntimeError('canonical_sponsor_poll_refresh_failed') from None


def reply_coverage(ledger,*,allow_poll_refresh=False):
 """One actual poll can renew expired proof; a no-send forecast never writes it."""
 result=consume_reply_stops(ledger)
 if allow_poll_refresh and result.get('reason')=='stale_complete_poll':
  applied=result['applied']
  refresh_canonical_poll()
  result=consume_reply_stops(ledger) # New effect time and newly committed stops.
  result['applied']+=applied
 return result


from contextlib import contextmanager
@contextmanager
def canonical_send_boundary(ledger,at,required,*,persist_stops=True,allow_poll_refresh=False):
 """Serialize canonical stop commits with the durable claim and send receipt."""
 if not required:
  yield
  return
 if allow_poll_refresh:
  readiness=reply_coverage(ledger,allow_poll_refresh=True)
  if readiness['applied'] and persist_stops:save(STATE/'ledger.json',ledger)
  if readiness['status']!='read_complete':raise RuntimeError('canonical_sponsor_reply_coverage_unknown')
  at=now() # Never grant an expired caller time after a slow poll.
 import sqlite3
 db=sqlite3.connect(INBOX_DB.as_uri()+'?mode=rw',uri=True,timeout=5)
 try:
  with db:
   db.execute('BEGIN IMMEDIATE')
   readiness=consume_reply_stops(ledger,db=db,at=at)
   if readiness['applied'] and persist_stops:save(STATE/'ledger.json',ledger)
   if readiness['status']!='read_complete':raise RuntimeError('canonical_sponsor_reply_coverage_unknown')
   yield
 finally:db.close()


def invoice_provider_hosts(values,local):
 """Live registry/suppression holds also govern agreement-based invoices."""
 import glp328_outreach_policy as policy
 if not values or tuple(values[0])!=policy.EXPECTED_HEADERS or not local['suppression']['complete']:return set()
 rows=[policy.classify_row(i,row) for i,row in enumerate(values[1:],2)]
 held=[row for row in rows if row.get('blocks_org')]
 return {row['host'] for row in rows if row['eligible'] and row.get('host')
         and 'fella' not in row['host'] and 'fella' not in row['program'].lower()
         and not any(same(row['host'],x['host']) or normalize(row['program'])==normalize(x['program']) for x in held)
         and not any(same(row['host'],x['domain']) for x in local['suppression']['entries'])}


def refresh_paypal_orders(config,at,dry_run=False,finance_hosts=(),ledger=None):
 """Same job, same lock/orders ledger. Dry-run cannot request or send invoices."""
 import glp_sponsor_paypal as paypal
 path=STATE/'orders.json'
 orders=load(path) if path.exists() else {}
 result={'status':'unknown','orders':{},'placement_allowed':False,'financial_writes':0,'financial_write_attempts':0}
 client=None
 if config.get('paypal_read_only_polling') is True:
  try:client=paypal.PayPalClient()
  except (RuntimeError,OSError,ValueError):pass
 for key,order in orders.items():
  # Never consume caller-written payment_verified fields as evidence.
  observation=paypal.unknown('paypal_account_binding_missing')
  if client is not None:
   action=order.get('invoice_action')
   allowed=(not dry_run and order.get('provider_host') in finance_hosts and key==order.get('order_id')
            and (not order.get('invoice_id') or sum(x.get('invoice_id')==order['invoice_id'] for x in orders.values())==1)
            and action in ('draft','request') and
            config.get('paypal_invoice_'+('drafts' if action=='draft' else 'requests')+'_enabled') is True)
   pending=(action=='draft' and not order.get('invoice_id')) or (action=='request' and order.get('invoice_id') and not order.get('invoice_requested_at'))
   if allowed and pending and not order.get('invoice_claim'):
    try:
     approval=paypal.authorize_order(order,STATE,action)
     client.merchant(at) # Gate identity before reservation; this is read-only.
     # The registered job supplies its canonical ledger. Static finance_hosts
     # is only initial admission (and an offline fixture seam), not a final
     # permission surviving merchant authentication or other slow reads.
     current_hosts=invoice_provider_hosts(read_values(),read_local()) if ledger is not None else finance_hosts
     with canonical_send_boundary(ledger if ledger is not None else {},now(),ledger is not None,allow_poll_refresh=not dry_run):
      host=order['provider_host']
      if host not in current_hosts:raise ValueError('sponsor_invoice_provider_held')
      if ledger is not None:
       if any(same(host,row.get('host',email.split('@')[-1]))
              and row.get('stopped') not in (None,'agreement_candidate') for email,row in ledger.items()):raise ValueError('sponsor_invoice_provider_stopped')
      order['invoice_claim']={'action':action,'at':stamp(at),'approval_sha256':approval}
      save(path,orders) # Unknown effects remain durably reserved; never retry.
      result['financial_write_attempts']+=1
      response=client.invoice_write(order,STATE,action,at)
      result['financial_writes']+=1
      if action=='draft':
       invoice_id=response.get('id','')
       if not re.fullmatch(paypal.INVOICE_ID,invoice_id):raise ValueError('paypal_draft_readback_missing')
       order['invoice_id']=invoice_id;save(path,orders)
       readback=client.request('GET','/v2/invoicing/invoices/'+invoice_id)
       if readback.get('id')!=invoice_id or readback.get('status')!='DRAFT':raise ValueError('paypal_draft_readback_missing')
       paypal.invoice_matches(readback,order)
       order['invoice_drafted_at']=stamp(at)
      else:
       readback=client.request('GET','/v2/invoicing/invoices/'+order['invoice_id'])
       if readback.get('id')!=order['invoice_id'] or readback.get('status') not in ('SENT','PAID','PAYMENT_PENDING'):raise ValueError('paypal_request_readback_missing')
       paypal.invoice_matches(readback,order)
       order['invoice_requested_at']=stamp(at)
      order.pop('invoice_claim');save(path,orders)
    except (RuntimeError,OSError,ValueError,KeyError,TypeError):pass
   observation=paypal.poll_order(client,order,at,STATE)
  order['payment_readiness']=observation
  result['orders'][key]=observation
 # Durable transaction reservations survive negative/unknown polls. Two orders
 # cannot reuse the same invoice or paid transaction, even on a later run.
 owners={}
 for key,order in orders.items():
  tokens=set(order.get('allocated_transaction_ids',[]))|set(result['orders'][key].get('transaction_ids',[]))
  if order.get('invoice_id'):tokens.add('invoice:'+order['invoice_id'])
  for token in tokens:owners.setdefault(token,set()).add(key)
 conflicts=set().union(*(keys for keys in owners.values() if len(keys)>1)) if owners else set()
 for key,order in orders.items():
  if key in conflicts:
   result['orders'][key]=paypal.unknown('paypal_duplicate_order_allocation')
   order['payment_readiness']=result['orders'][key]
  elif result['orders'][key]['payment_verified']:
   order['allocated_transaction_ids']=result['orders'][key]['transaction_ids']
 if orders:save(path,orders)
 if result['orders']:result['status']='polled' if client is not None else 'unknown'
 return result


def send_interested_next_steps(ledger,config,finance_hosts,capacity):
 """One approved information reply per enrolled provider, never an invoice.

 Uses the existing send/receipt path and canonical stop lock. The claim has no
 timeout: an ambiguous response is a reconciliation hold, not a retry.
 """
 sent=0
 for email,entry in ledger.items():
  if sent>=capacity:break
  if (entry.get('stopped')!='interested' or not entry.get('sent') or not entry.get('stop_event_id')
      or entry.get('next_step_claim') or entry.get('next_step_receipt') or entry.get('host') not in finance_hosts):continue
  event=entry['stop_event_id']
  item=load(ROOT/'ops/sponsor-machine/copy.json')['interested_next_step']
  preview={'to':email,'from':SENDER,'subject':item['subject'],
           'body':item['body'].replace('{organization}',organization_display(entry['organization']))+footer(config),'attachments':[]}
  client=service('send')
  # Authentication can take time. Re-read current registry and suppression at
  # the same final boundary as invoice admission; canonical opt-outs win next.
  current_hosts=invoice_provider_hosts(read_values(),read_local())
  with canonical_send_boundary(ledger,now(),True,allow_poll_refresh=True):
   if entry.get('stopped')!='interested' or entry.get('stop_event_id')!=event or entry['host'] not in current_hosts:continue
   entry['next_step_claim']={'event_id':event,'at':stamp(),'packet_sha256':config['packet_sha256']}
   save(STATE/'ledger.json',ledger)
   receipt=send_message(client,preview,entry,[])
   entry['next_step_receipt']=receipt;entry.pop('next_step_claim');save(STATE/'ledger.json',ledger)
   sent+=1
 return sent


def run(config,dry_run=False,*,transport_preflight=False,preflight_at=None):
 if preflight_at is not None and not transport_preflight:raise ValueError('preflight_at_requires_transport_preflight')
 if transport_preflight and dry_run:raise ValueError('choose_preview_or_transport_preflight')
 STATE.mkdir(parents=True,exist_ok=True)
 with inbox_poll_boundary(alert=not dry_run and not transport_preflight), (STATE/'run.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  enabled=False if dry_run else approval_ready(config)
  if transport_preflight and not enabled:raise RuntimeError('transport_preflight_approval_required')
  at=preflight_at or now();pt=at.astimezone(PT)
  if enabled:require_sponsor_send_window(at)
  would_send=0;admitted=[]
  values=read_values();discovery=load(ROOT/'reports/ops/sponsor-machine-20261003/contact-discovery.private.json');local=read_local()
  plan=recipients(values,discovery,local)
  sourcing_readiness=refresh_apollo_sources(plan,at) if not transport_preflight and config.get('apollo_read_only_discovery') is True else {'status':'not_requested','email_verification':'unknown','enrichment_debits':0}
  if enabled and not transport_preflight:
   sourcing_readiness['professional']=refresh_professional_sources(plan,discovery,at,config)
   if sourcing_readiness['professional']['status']!='disabled_pending_approval':
    save(ROOT/'reports/ops/sponsor-machine-20261003/contact-discovery.private.json',discovery)
    plan=recipients(values,discovery,local)
  import glp_sponsor_readiness as readiness
  if not local['suppression']['complete']:raise RuntimeError('suppression_ledger_incomplete:'+','.join(local['faults']))
  ledger=load(STATE/'ledger.json') if (STATE/'ledger.json').exists() else {}
  reply_readiness=reply_coverage(ledger,allow_poll_refresh=enabled and not transport_preflight and config.get('require_canonical_reply_coverage') is True)
  if reply_readiness['applied'] and not transport_preflight:save(STATE/'ledger.json',ledger)
  finance_hosts=invoice_provider_hosts(values,local) if reply_readiness['status']=='read_complete' else ()
  finance_hosts={host for host in finance_hosts if not any(same(host,row.get('host',email.split('@')[-1]))
                 and row.get('stopped') not in (None,'agreement_candidate') for email,row in ledger.items())}
  payment_readiness=(dict(status='not_requested_transport_preflight',financial_writes=0,financial_write_attempts=0) if transport_preflight
                     else refresh_paypal_orders(config,at,dry_run=dry_run,finance_hosts=finance_hosts,ledger=ledger))
  if enabled and config.get('require_canonical_reply_coverage') is True and reply_readiness['status']!='read_complete':raise RuntimeError('canonical_sponsor_reply_coverage_unknown')
  sent_today=sum(dt.datetime.fromisoformat(s['sent_at']).astimezone(PT).date()==pt.date() for entry in ledger.values() for s in entry.get('sent',[]))
  # An outstanding claim may already have delivered despite failed readback.
  # Reserve its PT day's capacity without clearing the no-replay claim.
  sent_today+=sum(dt.datetime.fromisoformat(entry['claimed']['at']).astimezone(PT).date()==pt.date() for entry in ledger.values() if entry.get('claimed'))
  # Legacy configs retain their total ceiling; current configs give initial
  # provider enrollments and due follow-ups independent daily allowances.
  split='new_provider_daily_cap' in config
  used={0:0,1:0}
  for entry in ledger.values():
   for reply in (entry.get('next_step_receipt'),entry.get('next_step_claim')):
    if reply and dt.datetime.fromisoformat(reply.get('sent_at',reply.get('at'))).astimezone(PT).date()==pt.date():used[1]+=1
   for i,s in enumerate(entry.get('sent',[])):
    if dt.datetime.fromisoformat(s['sent_at']).astimezone(PT).date()==pt.date():used[int(i>0)]+=1
   claim=entry.get('claimed')
   if claim and dt.datetime.fromisoformat(claim['at']).astimezone(PT).date()==pt.date():used[int(claim['stage']>0)]+=1
  remaining={0:max(0,int(config.get('new_provider_daily_cap',config.get('batch_cap',25)))-used[0]),1:max(0,int(config.get('follow_up_daily_cap',25))-used[1])}
  total_remaining=max(0,config.get('batch_cap',25)-sent_today)
  next_steps=send_interested_next_steps(ledger,config,invoice_provider_hosts(read_values(),read_local()),remaining[1]) if enabled and not transport_preflight else 0
  selected={0:0,1:0};sent_by_kind={0:0,1:next_steps};previews=[];sent_count=next_steps;read_client=None;boundary_exclusions=[]
  for recipient in plan['recipients']:
   stage=due(recipient,ledger,at)
   if stage is None:continue
   if enabled and config.get('require_bouncer') is True:
    verified=readiness.bouncer_readiness(recipient['email'],ROOT/'reports/bouncer-shared-credit-ledger-2026-06-25.jsonl',at)
    if verified['send_eligible'] is not True:continue
   kind=int(stage>0)
   if split:
    if (sent_by_kind[kind] if enabled else selected[kind])>=remaining[kind]:continue
   elif ((sent_count+would_send) if enabled else len(previews))>=total_remaining:break
   rendered=render(recipient,stage,config);previews.append(rendered);selected[kind]+=1
   if not enabled:continue
   # Old discovery is a census, not a perpetual permission. Reverify the exact
   # published address and purpose before each effect; no guessed aliases.
   import glp_sponsor_readiness as readiness
   if config.get('require_bouncer') is True:
    verified=readiness.bouncer_readiness(recipient['email'],ROOT/'reports/bouncer-shared-credit-ledger-2026-06-25.jsonl',at)
    if verified['send_eligible'] is not True:continue
   if recipient.get('contact_source')=='apollo':
    current_contact=readiness.professional_contact(recipient['host'],[recipient['contact_evidence']],at)
    if not current_contact:continue
   else:
    current=fresh_public_contact(recipient,now())
    if not current:
     boundary_exclusions.append({'host':recipient['host'],'reason':'current_exact_public_contact_unqualified'});continue
   if read_client is None:read_client=service('read')
   previous=ledger.setdefault(recipient['email'],{})
   allowed,reason=mail_admit(recipient,previous,read_client)
   if not allowed:
    previous['stopped']=reason
    if not transport_preflight:save(STATE/'ledger.json',ledger)
    continue
   # Re-read suppression immediately before effect, unioned with all own stops.
   if not any(r['email']==recipient['email'] for r in recipients(read_values(),discovery,read_local())['recipients']):continue
   send_client=service('read' if transport_preflight else 'send') # Preflight requests no send-capable authentication.
   with canonical_send_boundary(ledger,now(),config.get('require_canonical_reply_coverage') is True,persist_stops=not transport_preflight,allow_poll_refresh=not transport_preflight):
    # Re-read the LAST shared-ledger decision after all contact/mail/auth work,
    # using effect-boundary time, not run-start time. This is a read, never a debit.
    boundary_at=at if transport_preflight else now();boundary_pt=boundary_at.astimezone(PT)
    if boundary_pt.weekday()>=5 or not 6<=boundary_pt.hour<17:
     boundary_exclusions.append({'host':recipient['host'],'reason':'outside_approved_weekday_06_17_PT_send_window'});continue
    if config.get('require_bouncer') is True:
     verified=readiness.bouncer_readiness(recipient['email'],ROOT/'reports/bouncer-shared-credit-ledger-2026-06-25.jsonl',boundary_at)
     if verified['send_eligible'] is not True:
      boundary_exclusions.append({'host':recipient['host'],'reason':'boundary_verification_'+verified['status']});continue
    if recipient.get('contact_source')=='apollo' and not readiness.professional_contact(recipient['host'],[recipient['contact_evidence']],boundary_at):
     boundary_exclusions.append({'host':recipient['host'],'reason':'boundary_professional_contact_expired'});continue
    # Stops committed during contact/mail/suppression reads win before claim.
    if due(recipient,ledger,boundary_at)!=stage:continue
    if not approval_ready(config):raise RuntimeError('sponsor_approval_changed_at_boundary')
    if not any(r['email']==recipient['email'] for r in recipients(read_values(),discovery,read_local())['recipients']):continue
    attachments=[ROOT/'reports/ops/sponsor-machine-20261003/assets'/n for n in rendered['attachments']]
    require_sponsor_send_window(boundary_at)
    if transport_preflight:
     prepare_message(send_client,rendered,previous,attachments,at=boundary_at)
     would_send+=1;admitted.append({'host':recipient['host'],'email':recipient['email'],'stage':stage})
     sent_by_kind[kind]+=1 # Capacity reservations only, never reported as sends.
     continue
    previous.update(host=recipient['host'],organization=recipient['organization'],contact_source=recipient.get('contact_source','published_business_contact'),variant=rendered['variant'])
    previous['claimed']={'stage':stage,'at':stamp(boundary_at),'packet_sha256':config['packet_sha256']};save(STATE/'ledger.json',ledger)
    receipt=send_message(send_client,rendered,previous,[ROOT/'reports/ops/sponsor-machine-20261003/assets'/n for n in rendered['attachments']])
    previous.setdefault('sent',[]).append(receipt);previous.pop('claimed');save(STATE/'ledger.json',ledger);sent_count+=1;sent_by_kind[kind]+=1
  verification_counts={}
  for recipient in plan['recipients']:
   status=recipient.get('verification',{}).get('status','unknown')
   verification_counts[status]=verification_counts.get(status,0)+1
  receipt={'interested_next_step_count':next_steps,'reply_readiness':reply_readiness,'sourcing_readiness':sourcing_readiness,'payment_readiness':payment_readiness,'verification_readiness':verification_counts,'new_provider_count':sent_by_kind[0],'follow_up_count':sent_by_kind[1],'new_provider_preview_count':selected[0],'follow_up_preview_count':selected[1],'daily_capacity_used':{'new_providers':used[0],'follow_ups':used[1]},'campaign':CAMPAIGN,'run_type':'registered_dry_run' if dry_run else 'scheduled','generated_utc':stamp(at),'send_enabled':enabled,'sent':sent_count,'eligible_recipient_count':len(plan['recipients']),'first_batch_count':len(previews),'registry_rows':plan['registry_rows'],'candidate_rows':plan['candidate_rows'],'previews':previews,'exclusions':plan['exclusions']}
  receipt.update(send_boundary_exclusions=boundary_exclusions,denominators={
   'registry_rows':'registry census; not organizations or qualified recipients',
   'candidate_rows':'initial eligible registry rows before US/medication/contact gates',
   'eligible_recipient_count':'purpose-qualified initial plan; not final transport admission',
   'first_batch_count':'rendered previews before final contact/mail/stop/verifier checks',
   'verification_readiness':'initial plan verification observations; not final send admission',
   'final_admission_checked':enabled,'new_provider_count':'actual reconciled initial sends in this run',
   'follow_up_count':'actual reconciled follow-up and interested-reply sends in this run',
   'target_new_provider_daily_cap':config.get('new_provider_daily_cap',config.get('batch_cap',25)),
   'current_new_provider_supply':None})
  receipt.update(would_send=would_send,admitted_recipients=admitted,
                 run_type='transport_preflight' if transport_preflight else receipt['run_type'],
                 preflight_slot=stamp(at) if transport_preflight else None,
                 coverage_observed_at=stamp() if transport_preflight else None)
  if transport_preflight:receipt.update(new_provider_count=0,follow_up_count=0)
  if not transport_preflight:save(STATE/'latest-dry-run.json' if not enabled else STATE/'latest-run.json',receipt)
  return receipt

def main():
 sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'ops'),str(ROOT)]
 ap=argparse.ArgumentParser();ap.add_argument('--dry-run',action='store_true');ap.add_argument('--scheduled',action='store_true')
 ap.add_argument('--transport-preflight',action='store_true');ap.add_argument('--preflight-at',type=dt.datetime.fromisoformat);args=ap.parse_args()
 config=campaign_config()
 try:r=run(config,dry_run=args.dry_run,transport_preflight=args.transport_preflight,preflight_at=args.preflight_at)
 except Exception as exc:
  if args.dry_run or args.transport_preflight:raise
  # A failed batch may have sent many messages. Report durable PT-day totals,
  # never claim this exception means zero effects or authorize a blind replay.
  failure={'status':'FAILED_RECONCILE_BEFORE_RETRY','error_class':type(exc).__name__,
           'generated_utc':stamp(),'scope':'PT-day ledger totals, not current-run totals',
           'replay_allowed':False,'day_sends':None,'outstanding_claims':None}
  try:
   ledger=load(STATE/'ledger.json');day=now().astimezone(PT).date()
   day_sends=[];claims=[]
   for email,entry in ledger.items():
    for sent in entry.get('sent',[])+([entry['next_step_receipt']] if entry.get('next_step_receipt') else []):
     if dt.datetime.fromisoformat(sent['sent_at']).astimezone(PT).date()==day:
      day_sends.append({'email':email,'message_id':sent['message_id'],'sent_at':sent['sent_at']})
    for key in ('claimed','next_step_claim'):
     claim=entry.get(key)
     if claim and dt.datetime.fromisoformat(claim['at']).astimezone(PT).date()==day:
      claims.append({'email':email,'kind':key,'at':claim['at']})
   failure.update(pt_day=day.isoformat(),day_sends=day_sends,outstanding_claims=claims)
  except (OSError,ValueError,KeyError,TypeError,AttributeError):pass # Unknown is not zero.
  try:save(STATE/'latest-failure.json',failure)
  except OSError:pass
  count='unknown' if failure['day_sends'] is None else len(failure['day_sends'])
  claims='unknown' if failure['outstanding_claims'] is None else len(failure['outstanding_claims'])
  print(f"Sponsor outreach FAILED ({failure['error_class']}); PT-day confirmed sends: {count}; outstanding claims: {claims}. Reconcile ledger and Gmail before retry; not current-run totals.",file=sys.stderr)
  raise SystemExit(1) from None
 if args.dry_run or args.transport_preflight:print(json.dumps({k:v for k,v in r.items() if k not in ('previews','exclusions')},sort_keys=True))
 elif r['send_enabled']:print(f"Sponsor outreach: {r['sent']} sent; eligible {r['eligible_recipient_count']}. Replies stop follow-ups.")
if __name__=='__main__':main()
