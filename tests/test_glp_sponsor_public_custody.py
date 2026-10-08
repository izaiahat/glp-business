"""Advisory F1-F4: captured public-business sources, no network."""
import base64, datetime as dt, hashlib, json, sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import glp_sponsor_campaign as c
AT=dt.datetime(2026,10,8,16,tzinfo=dt.timezone.utc)


def organization(address='partners@example.com',wording='Commercial partnerships:'):
 html=f'<p>{wording} <a href="mailto:{address}">{address}</a></p>'
 sha=hashlib.sha256(html.encode()).hexdigest();url='https://example.com/partnerships'
 ev={'address':address,'how':'mailto','tier':'partnerships','found_on':url,'page_sha256':sha,'captured_at_utc':AT.isoformat(),'excerpt':wording+' '+address}
 return {'discovery':{'contact_email':address,'contact_tier':'partnerships','contact_evidence':ev,'candidates':[ev],
                      '_markup':[{'url':url,'final_url':url,'html':html,'sha256':sha,'fetched_at_utc':AT.isoformat()}]}}


def test_text_only_is_truthfully_captured_not_relabelled_mailto():
 org=organization();page=org['discovery']['_markup'][0];page['html']='<p>Commercial partnerships: partners@example.com</p>';page['sha256']=hashlib.sha256(page['html'].encode()).hexdigest()
 ev=org['discovery']['contact_evidence'];ev.update(how='published_text',page_sha256=page['sha256'])
 selected=c.public_business_contact('example.com',org,AT)
 assert selected['address']=='partners@example.com' and selected['how']=='published_text'
 page['html']='changed capture';assert c.public_business_contact('example.com',org,AT) is None


@pytest.mark.parametrize('case',['missing_capture','off_property','stale','role_support','role_creator','fake_hash','fake_address'])
def test_public_source_or_role_ambiguity_is_nonadmitting(case):
 org=organization()
 if case=='missing_capture':org['discovery']['_markup']=[]
 elif case=='off_property':
  org['discovery']['_markup'][0]['final_url']='https://unrelated.invalid/partnerships';org['discovery']['contact_evidence']['found_on']='https://unrelated.invalid/partnerships'
 elif case=='stale':org['discovery']['contact_evidence']['captured_at_utc']=(AT-dt.timedelta(days=31)).isoformat()
 elif case=='role_support':org=organization(wording='Affiliate support for existing members:')
 elif case=='role_creator':org=organization(wording='Creator recruitment for your personal journey:')
 elif case=='fake_hash':org['discovery']['contact_evidence']['page_sha256']='a'*64
 elif case=='fake_address':org['discovery']['contact_evidence']['address']='business@example.com'
 assert c.public_business_contact('example.com',org,AT) is None


def test_sponsor_selection_filters_evidenced_candidates_before_ranking():
 org=organization('bd@example.com');fallback=organization('partners@example.com')
 org['discovery']['candidates']+=fallback['discovery']['candidates'];org['discovery']['_markup']+=fallback['discovery']['_markup']
 assert c.public_business_contact('example.com',org,AT)['address']=='partners@example.com'


@pytest.mark.parametrize('case',['valid','unanchored','off_property','stale','canada','pronoun_us','wrong_medication'])
def test_supplemental_provider_scope_requires_current_quoted_first_party_custody(case):
 org=organization();page=org['discovery']['_markup'][0]
 country='Available to customers in the United States';medication='Our doctors prescribe GLP-1 medication'
 if case=='canada':country='Available to customers in Canada'
 if case=='pronoun_us':country='Send pricing to us'
 if case=='wrong_medication':medication='We sell vitamins'
 page['html']+=f'<p>{country}. {medication}.</p>';page['sha256']=hashlib.sha256(page['html'].encode()).hexdigest()
 ev={'source_url':page['final_url'],'country':'US','country_excerpt':country,'medication_provider_excerpt':medication,'sha256':page['sha256'],'observed_utc':AT.isoformat()}
 if case=='unanchored':ev['medication_provider_excerpt']='Fictional prescriber statement'
 if case=='off_property':ev['source_url']='https://unrelated.invalid/'
 if case=='stale':ev['observed_utc']=(AT-dt.timedelta(days=31)).isoformat()
 org['scope_evidence']=[ev]
 assert bool(c.scope_evidence_current('example.com',org,AT))==(case=='valid')


@pytest.mark.parametrize('medication,accepted',[
 ('Our doctors prescribe GLP-1 medication',True),
 ('We provide GLP-1 treatment',True),
 ('We provide weight loss marketing services, not medical care',False),
 ('We do not prescribe GLP-1 medication',False),
 ('Our doctors never prescribe GLP-1 medication',False),
 ('We offer GLP-1 marketing services to doctors',False),
 ('Our doctors prescribe vitamins',False),
 ('GLP-1 prescription services are not offered here',False),
 ('We prescribe GLP-1 medication for marketing demonstrations, not patients',False),
 ('We provide weight loss coaching, not prescriptions',False),
])
def test_supplemental_scope_requires_affirmative_medication_provider_not_keywords(tmp_path,monkeypatch,medication,accepted):
 from test_glp_sponsor_campaign import inputs
 monkeypatch.setattr(c,'ROOT',tmp_path);monkeypatch.setattr(c,'now',lambda:AT)
 sheet,_,local=inputs();sheet[1][2]='Example Services';sheet[1][3]='';sheet[1][7]=''
 org=organization();page=org['discovery']['_markup'][0]
 country='Available to customers in the United States'
 page['html']+=f'<p>{country}. {medication}.</p>';page['sha256']=hashlib.sha256(page['html'].encode()).hexdigest()
 org['discovery']['contact_evidence']['page_sha256']=page['sha256']
 org['scope_evidence']=[{'source_url':page['final_url'],'country':'US','country_excerpt':country,
                         'medication_provider_excerpt':medication,'sha256':page['sha256'],'observed_utc':AT.isoformat()}]
 assert bool(c.scope_evidence_current('example.com',org,AT)) is accepted
 plan=c.recipients(sheet,{'organizations':{'example.com':org}},local)
 assert len(plan['recipients'])==int(accepted)
 if not accepted:assert plan['exclusions'][0]['reason']=='not_glp_medication_provider'


class Response:
 def __init__(self,url,body=b'',status=200,location=None):
  self.url=url;self.status_code=status;self.encoding='utf-8';self.headers={'Location':location} if location else {};self.body=body
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def iter_content(self,chunk_size):yield self.body


@pytest.mark.parametrize('case',['changed_winner','disappeared','support','off_property_redirect','tampered_root'])
def test_fresh_public_read_checks_exact_address_and_scope_before_redirect(tmp_path,monkeypatch,case):
 monkeypatch.setattr(c,'STATE',tmp_path)
 recipient={'email':'partners@example.com','host':'example.com','organization':'Example','contact_evidence':{'found_on':'https://example.com/partnerships'}}
 if case=='tampered_root':recipient['contact_evidence']['found_on']='https://unrelated.invalid/partnerships'
 body=b'<p>Commercial partnerships: bd@example.com and partners@example.com</p>'
 if case=='disappeared':body=b'<p>Commercial partnerships: business@example.com</p>'
 if case=='support':body=b'<p>Affiliate support for members: partners@example.com</p>'
 calls=[]
 def get(url,**kwargs):
  calls.append((url,kwargs));assert kwargs['allow_redirects'] is False
  if case=='off_property_redirect':return Response(url,status=302,location='https://unrelated.invalid/partnerships')
  return Response(url,body)
 monkeypatch.setattr('requests.get',get)
 result=c.fresh_public_contact(recipient,AT)
 assert bool(result)==(case=='changed_winner')
 if case=='changed_winner':
  assert result['address']==recipient['email'] and result['how']=='published_text'
  capture=json.loads(next(tmp_path.glob('contact-captures/*.json')).read_text());page=capture['_markup'][0]
  assert page['body_representation']=='requests_decompressed_content' and base64.b64decode(page['body_base64'])==body
 if case=='off_property_redirect':assert len(calls)==1 # Never issue an off-property request.
 if case=='tampered_root':assert calls==[]
