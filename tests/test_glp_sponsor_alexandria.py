import datetime as dt
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import glp_sponsor_alexandria as sourcing


def test_research_filters_holds_dedupes_and_reveals_only_business_email(tmp_path):
    calls=[]
    def scrape(payload):
        assert isinstance(payload['alexandria'],list), 'REST requires the Alexandria array envelope'
        assert 'requestId' not in payload, 'requestId is MCP-only, rejected by live REST v2'
        call=payload['alexandria'][0]; calls.append(call)
        assert call['provider']=='apollo'
        if call['capability']=='people/search':
            data={'people':[{'id':'person1','has_email':True,'title':'Head of Partnerships'}]}
        else:
            assert call['options']=={'id':'person1','reveal_personal_emails':False,'reveal_phone_number':False}
            data={'person':{'id':'person1','title':'Head of Partnerships','email':'business@example.com','country':'United States','organization':{'primary_domain':'example.com'}}}
        return {'success':True,'data':{'alexandria':[{'provider':'apollo','capability':call['capability'],'data':data,'upstreamStatus':200,'creditsCost':0}]}}
    now=dt.datetime(2026,10,7,tzinfo=dt.timezone.utc)
    result=sourcing.research(['example.com','example.com','fellahealth.com','held.example'],{'held.example'},tmp_path,scrape,now,25)
    assert len(calls)==2 and result['qualified_contacts']==1
    contacts=result['contacts']
    assert contacts[0]['email']=='business@example.com'
    assert contacts[0]['verification_status']=='unknown'
    # Exact known observations replay locally, with no enrichment/reveal retry.
    again=sourcing.research(['example.com'],set(),tmp_path,scrape,now,25)
    assert len(calls)==2 and again['qualified_contacts']==1


def test_ambiguous_call_remains_a_hold(tmp_path):
    calls=[]
    def fail(payload):
        calls.append(payload);raise TimeoutError()
    now=dt.datetime(2026,10,7,tzinfo=dt.timezone.utc)
    result=sourcing.research(['example.com'],set(),tmp_path,fail,now,25)
    assert result['qualified_contacts']==0
    result=sourcing.research(['example.com'],set(),tmp_path,fail,now,25)
    assert len(calls)==1 and result['unknown_hosts']==['example.com']


def test_campaign_apollo_adapter_uses_shared_alexandria_client(monkeypatch):
    import types
    import glp_sponsor_readiness as ready
    calls=[]
    class Client:
        def __init__(self,caller):assert caller=='sponsor_campaign'
        def scrape(self,payload):
            calls.append(payload)
            assert 'requestId' not in payload
            return {'success':True,'data':{'alexandria':[{'provider':'apollo','capability':'people/search','upstreamStatus':200,'data':{'people':[]}}]}}
    monkeypatch.setitem(sys.modules,'firecrawl_client',types.SimpleNamespace(FirecrawlClient=Client))
    result=ready.apollo_transport('POST','/api/v1/mixed_people/api_search',{'q_organization_domains_list':['example.com'],'page':1,'per_page':25})
    assert result=={'people':[]} and len(calls)==1


def test_attempted_gaps_do_not_starve_next_provider(tmp_path,monkeypatch):
    import glp_sponsor_campaign as c
    monkeypatch.setattr(c,'STATE',tmp_path)
    c.save(tmp_path/'apollo-sourcing.private.json',{f'p{i:02}.example.com':{'attempted_at':'2026-10-06T00:00:00+00:00'} for i in range(25)})
    plan={'recipients':[],'exclusions':[{'host':f'p{i:02}.example.com','reason':'no_verified_business_purpose_contact'} for i in range(26)]}
    calls=[]
    def transport(method,path,payload):calls.append(payload);return {'people':[]}
    c.refresh_professional_sources(plan,{'organizations':{}},dt.datetime(2026,10,7,tzinfo=dt.timezone.utc),
        {'professional_enrichment_enabled':True,'bouncer_debits_enabled':True},transport=transport)
    assert len(calls)==1 and calls[0]['q_organization_domains_list']==['p25.example.com']
