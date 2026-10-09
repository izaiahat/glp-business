"""Bounded Alexandria business-contact evidence, not a send/deliverability grant.

Called under the campaign's existing lock. No second scheduler, personal email,
phone lookup, Sheets writer, or paid fallback. Unknown effects never auto-retry.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from glp_sponsor_readiness import PROFESSIONAL


def private_save(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as file:
        json.dump(value,file,sort_keys=True);file.flush();os.fsync(file.fileno())
    os.replace(temp,path)


def observe(root, host, capability, options, scrape, at):
    payload={'alexandria':[{'provider':'apollo','capability':capability,'options':options}]}
    key=hashlib.sha256(('rest-v2-no-request-id:'+json.dumps(payload,sort_keys=True)).encode()).hexdigest()
    path=Path(root)/(key+'.json')
    if path.exists():
        receipt=json.loads(path.read_text())
        if receipt['status']!='complete':raise RuntimeError('ambiguous_prospect_effect')
        # Do not automatically replay a reveal merely because the evidence aged.
        if at-dt.datetime.fromisoformat(receipt['at'])>dt.timedelta(days=30):raise RuntimeError('prospect_evidence_expired')
        return receipt['data']
    request_id='sponsor-prospect-'+key
    private_save(path,{'status':'claimed','at':at.isoformat(),'host':host,'capability':capability,'requestId':request_id})
    response=scrape(payload)
    entries=response.get('data',{}).get('alexandria',[])
    if response.get('success') is not True or len(entries)!=1:
        raise RuntimeError('alexandria_response_unknown')
    row=entries[0]
    if row.get('error') or row.get('provider')!='apollo' or row.get('capability')!=capability or row.get('upstreamStatus')!=200:
        raise RuntimeError('alexandria_provider_unknown')
    data=row['data']
    # Retain only professional fields needed by the campaign, not upstream
    # personal/mobile/profile fields returned despite reveal flags being false.
    if capability=='people/search':
        clean={'people':[{k:p.get(k) for k in ('id','title','has_email')} for p in data.get('people',[])]}
    else:
        p=data.get('person') or {}
        clean={'person':{k:p.get(k) for k in ('id','title','email','country')}}
        clean['person']['organization']={'primary_domain':(p.get('organization') or {}).get('primary_domain')}
    private_save(path,{'status':'complete','at':at.isoformat(),'host':host,'capability':capability,
        'requestId':request_id,'alexandria_id':row.get('alexandriaId'),'credits':row.get('creditsCost'),
        'response_sha256':hashlib.sha256(json.dumps(response,sort_keys=True).encode()).hexdigest(),'data':clean})
    return clean


def business_title(title):
    return bool(PROFESSIONAL.search(title or '')) and not re.search(r'patient|clinical|support|billing',title or '',re.I)


def research(hosts, suppressed_hosts, root, scrape, at, limit=25):
    from glp328_outreach_policy import _same_adverse_org
    selected=[]
    for host in sorted(set(hosts)):
        if not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}',host) or 'fella' in host:continue
        if any(_same_adverse_org(host,h) for h in suppressed_hosts):continue
        if any(_same_adverse_org(host,h) for h in selected):continue
        selected.append(host)
    contacts=[];unknown=[];empty=[]
    for host in selected[:min(limit,25)]:
        try:
            result=observe(root,host,'people/search',{'q_organization_domains_list':[host],
                'person_locations':['United States'],'person_titles':['partnerships','marketing','business development','founder','CEO','COO'],
                'page':1,'per_page':25},scrape,at)
            people=[p for p in result.get('people',[]) if p.get('id') and p.get('has_email') is True and business_title(p.get('title'))]
            # Prefer partnership/marketing contacts over founders; one per org.
            people.sort(key=lambda p:(not bool(re.search('partnership|marketing',p.get('title',''),re.I)),p['id']))
            if not people:empty.append(host);continue
            result=observe(root,host,'people/match',{'id':people[0]['id'],'reveal_personal_emails':False,'reveal_phone_number':False},scrape,at)
            person=result.get('person') or {};email=str(person.get('email') or '').lower()
            if (person.get('id')!=people[0]['id'] or not business_title(person.get('title'))
                or person.get('country')!='United States' or person.get('organization',{}).get('primary_domain')!=host
                or not re.fullmatch(r'[a-z0-9._%+-]+@'+re.escape(host),email)):
                unknown.append(host);continue
            contacts.append({'source':'apollo','person_id':person['id'],'organization_domain':host,'country':'US',
                'title':person['title'],'email':email,'captured_at_utc':at.isoformat(),
                'evidence_sha256':hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest(),
                'verification_status':'unknown','send_eligible':False})
        except (RuntimeError,OSError,ValueError,KeyError,TypeError):unknown.append(host)
    result={'schema':'sponsor-professional-evidence-v1','generated_at':at.isoformat(),'selected_hosts':selected[:min(limit,25)],
        'qualified_contacts':len(contacts),'contacts':contacts,'unknown_hosts':unknown,'empty_hosts':empty,
        'delivery_verified':0,'send_authority':False,'new_providers_per_week_required':125}
    private_save(Path(root)/'latest.json',result)
    return result
