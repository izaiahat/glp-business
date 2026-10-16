"""Advisory F1-F4: captured public-business sources, no network."""
import base64, datetime as dt, hashlib, json, sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import glp_sponsor_campaign as c
AT=dt.datetime(2026,10,8,16,tzinfo=dt.timezone.utc)


def retained_sales_page():
 # Literal retained Firecrawl page; no fetch, guessed contact or enrichment join.
 fixture=json.loads((Path(__file__).parent/'fixtures/sponsor-procompounding-about.json').read_text())
 markup=fixture['html']
 assert hashlib.sha256(markup.encode()).hexdigest()=='dd328508df88938da61993ccf2d6335dca6a6c6f7d63968daaa7475c9aff0ae8'
 start=markup.index('<div class="bg-card border border-border rounded-lg p-6 text-center"><img src="https://irp.cdn-website.com/d5a35c4c/dms3rep/multi/Pro+compounding+KateKincaid.webp"')
 card=markup[start:markup.index('</div>',start)+6]
 assert hashlib.sha256(card.encode()).hexdigest()=='bf23252e2c60f1ccb07a3049465eea74cc83b8704a684635473482e43338f2f5'
 return markup,card


def extract_and_validate(markup,url='https://procompounding.com/about',address='kate.kincaid@procompounding.com',host='procompounding.com'):
 import glp_sponsor_firecrawl_supply as s
 at=dt.datetime(2026,10,16,1,tzinfo=dt.timezone.utc)
 sha=hashlib.sha256(markup.encode()).hexdigest()
 _,contacts=s.evidence(host,url,markup,sha,at.isoformat(),c.public_page_hits,c.BUSINESS_LOCALS,c.same,sponsor=True)
 # Also submit a correctly hash-bound inert projection to test the final gate
 # independently of whether the producer admitted the contact.
 ev={'address':address,'how':'mailto','tier':'named_on_partnerships_page','found_on':url,'page_sha256':sha,'captured_at_utc':at.isoformat()}
 org={'discovery':{'candidates':[ev],'_markup':[{'url':url,'final_url':url,'html':markup,'sha256':sha}]}}
 return contacts,c.public_business_contact(host,org,at,exact_email=address)


@pytest.mark.parametrize('fragment',[False,True])
def test_retained_named_marketing_sales_manager_recall(fragment,monkeypatch):
 import socket
 monkeypatch.setattr(socket.socket,'connect',lambda *a,**k:pytest.fail('network forbidden'))
 page,card=retained_sales_page()
 contacts,validated=extract_and_validate(card if fragment else page)
 assert [row['address'] for row in contacts]==['kate.kincaid@procompounding.com']
 assert validated and validated['address']=='kate.kincaid@procompounding.com'
 assert 'Marketing and Sales Manager' in validated['excerpt']
 assert 'patients' not in validated['excerpt'].lower()



@pytest.mark.parametrize('case',[
 'mismatched_mailto','mismatched_text','second_mailbox','nearby_role',
 'sibling_role','nested_unrelated_role','wrong_identity','support','patient','care','records',
 'hidden_role','script_role','unclosed_block','off_property','ambiguous_authority',
 'wrong_email_domain','text_only','mailto_only',
])
def test_named_business_card_refuses_ambiguous_or_adverse_evidence(case):
 _,card=retained_sales_page();address='kate.kincaid@procompounding.com'
 url='https://procompounding.com/about'
 if case=='mismatched_mailto':card=card.replace('mailto:'+address,'mailto:other@procompounding.com')
 elif case=='mismatched_text':card=card.replace('>'+address+'<','>other@procompounding.com<')
 elif case=='second_mailbox':card=card.replace('</div>','<p>other@procompounding.com</p></div>')
 elif case in ('nearby_role','sibling_role'):
  title='<p>Marketing and Sales Manager</p>'
  card=card.replace('Marketing and Sales Manager','Staff Member')
  card=(title+card) if case=='nearby_role' else '<div>'+title+'<div>'+card+'</div></div>'
 elif case=='nested_unrelated_role':card=card.replace('Marketing and Sales Manager','Staff Member').replace('</div>','<div><h3>Other Person</h3>Marketing and Sales Manager</div></div>')
 elif case=='wrong_identity':card=card.replace('Kate Kincaid','Other Person')
 elif case in ('support','patient','care','records'):card=card.replace('</div>',f'<p>For {case} inquiries.</p></div>')
 elif case in ('hidden_role','script_role'):
  tag='<span hidden>Marketing and Sales Manager</span>' if case=='hidden_role' else '<script>Marketing and Sales Manager</script>'
  card=card.replace('Marketing and Sales Manager',tag)
 elif case=='unclosed_block':card=card.removesuffix('</div>')
 elif case=='off_property':url='https://thirdparty.invalid/about'
 elif case=='ambiguous_authority':url='https://outside.invalid\\sub.procompounding.com/about'
 elif case=='wrong_email_domain':card=card.replace('@procompounding.com','@thirdparty.invalid');address=address.replace('@procompounding.com','@thirdparty.invalid')
 elif case=='text_only':card=card.replace('href="mailto:'+address+'"','href="/contact"')
 elif case=='mailto_only':card=card.replace('>'+address+'<','>Email Kate<')
 contacts,validated=extract_and_validate(card,url,address)
 assert contacts==[]
 assert validated is None


# RV548 retained reviewer counterexamples; identical mutations/assertions.
@pytest.mark.parametrize('role',[
 'Former Marketing and Sales Manager',
 'Not the Marketing and Sales Manager',
 'Works with our Marketing and Sales Manager',
 'Staff member. Jane Doe is our Marketing and Sales Manager',
])
def test_requires_role_to_describe_mailbox_owner(role):
 _,card=retained_sales_page()
 card=card.replace('Marketing and Sales Manager',role)
 contacts,validated=extract_and_validate(card)
 assert contacts==[] and validated is None, {'producer_admitted':bool(contacts),'final_admitted':bool(validated),'role':role}

@pytest.mark.parametrize('purpose',['privacy','data protection'])
def test_adverse_privacy_purpose_refused(purpose):
 _,card=retained_sales_page()
 card=card.replace('</div>',f'<p>Contact this address only for {purpose} inquiries.</p></div>')
 contacts,validated=extract_and_validate(card)
 assert contacts==[] and validated is None, {'producer_admitted':bool(contacts),'final_admitted':bool(validated),'purpose':purpose}

@pytest.mark.parametrize('purpose',['patient','support','care','records'])
def test_existing_adverse_refusals(purpose):
 _,card=retained_sales_page()
 card=card.replace('</div>',f'<p>Contact this address only for {purpose} inquiries.</p></div>')
 contacts,validated=extract_and_validate(card)
 assert contacts==[] and validated is None

def test_flat_second_person_cannot_lend_role():
 _,card=retained_sales_page()
 card=card.replace('Marketing and Sales Manager','Staff Member').replace('</div>','<h3>Jane Doe</h3><p>Marketing and Sales Manager</p></div>')
 contacts,validated=extract_and_validate(card)
 assert contacts==[] and validated is None, {'producer_admitted':bool(contacts),'final_admitted':bool(validated)}


@pytest.mark.parametrize('role',[
 'Ex-Marketing and Sales Manager', 'Previous Marketing and Sales Manager',
 'Marketing and Sales Manager (former)', 'Marketing and Sales Manager, previously',
 'no longer Marketing and Sales Manager', 'not our sales contact; Marketing and Sales Manager',
 'Staff member; John Smith, Marketing and Sales Manager',
 'Marketing and Sales Manager. Jane Doe is our Marketing and Sales Manager',
 'Marketing and Sales Manager assistant',
])
def test_qualified_or_other_person_role_is_not_affirmative(role):
 _,card=retained_sales_page()
 contacts,validated=extract_and_validate(card.replace('Marketing and Sales Manager',role))
 assert contacts==[] and validated is None


@pytest.mark.parametrize('purpose',['legal','compliance','privacy requests only','data-protection'])
def test_restricted_contact_purpose_is_not_business_stock(purpose):
 _,card=retained_sales_page()
 card=card.replace('</div>',f'<p>For {purpose} inquiries only.</p></div>')
 contacts,validated=extract_and_validate(card)
 assert contacts==[] and validated is None


@pytest.mark.parametrize('purpose',['privacy','legal','compliance'])
def test_restricted_named_mailbox_is_not_business_stock(purpose):
 _,card=retained_sales_page()
 card=card.replace('Kate Kincaid',purpose.title()+' Officer').replace('kate.kincaid',purpose+'.officer')
 contacts,validated=extract_and_validate(card,address=purpose+'.officer@procompounding.com')
 assert contacts==[] and validated is None


def test_unrelated_privacy_footer_does_not_contaminate_role_card():
 _,card=retained_sales_page()
 contacts,validated=extract_and_validate(card+'<footer>For privacy requests only: privacy@procompounding.com</footer>')
 assert [row['address'] for row in contacts]==['kate.kincaid@procompounding.com']
 assert validated and validated['address']=='kate.kincaid@procompounding.com'


def test_retained_patientcare_is_not_business_stock():
 page,_=retained_sales_page()
 contacts,validated=extract_and_validate(page,address='patientcare@procompounding.com')
 assert 'patientcare@procompounding.com' in page
 assert all(row['address']!='patientcare@procompounding.com' for row in contacts)
 assert validated is None


def test_retained_membersupport_remains_refused():
 fixture=json.loads((Path(__file__).parent/'fixtures/sponsor-retained-membersupport.json').read_text())
 markup=fixture['fragment']
 assert hashlib.sha256(markup.encode()).hexdigest()==fixture['fragment_sha256']
 contacts,validated=extract_and_validate(markup,url=fixture['source_url'],address='membersupport@1stoptimal.com',host='1stoptimal.com')
 assert contacts==[] and validated is None


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
