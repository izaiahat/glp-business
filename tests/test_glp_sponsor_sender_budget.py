import datetime as dt
import pytest
import glp_sponsor_campaign as c
import glp_sponsor_readiness as r
import glp_sponsor_firecrawl_supply as supply
import firecrawl_client as fc
import glp_firecrawl_jobs as jobs

@pytest.mark.parametrize('remaining',[54999,90000])
def test_sender_match_rechecks_shared_period_before_transport(monkeypatch,tmp_path,remaining):
 monkeypatch.setattr(c,'STATE',tmp_path)
 monkeypatch.setattr(fc,'LEDGER',tmp_path/'ops.jsonl')
 def spent(callers):
  assert 'sponsor_campaign' in callers
  return 0
 monkeypatch.setattr(jobs,'spent_today',spent)
 monkeypatch.setattr(supply,'now',lambda:dt.datetime(2026,10,14,tzinfo=dt.timezone.utc))
 monkeypatch.setattr(supply.Producer,'discover_apollo_contacts',lambda self:setattr(self,'apollo_capabilities',{'people/match':'fixture-match'}))
 reads=[];paid=[]
 def request(self,endpoint,payload=None,**kw):
  if endpoint=='team/credit-usage':
   reads.append(endpoint)
   return {'data':{'remainingCredits':90000 if len(reads)==1 else remaining,'planCredits':100000,'billingPeriodStart':'2026-09-19T23:59:59Z','billingPeriodEnd':'2026-10-19T23:59:59Z'}}
  paid.append(payload)
  return {'success':True,'creditsUsed':30,'data':{'alexandria':[{'provider':'apollo','capability':'fixture-match','upstreamStatus':200,'data':{'person':{}}}]}}
 monkeypatch.setattr(fc.FirecrawlClient,'request',request)
 payload={k:False for k in ('reveal_personal_emails','reveal_phone_number','run_waterfall_email','run_waterfall_phone')}
 payload['id']='SYNTHETIC-NOT-A-PERSON'
 if remaining==54999:
  with pytest.raises(RuntimeError,match='sponsor_period_credit_cap'):
   r.apollo_transport('POST','/api/v1/people/match',payload)
  assert paid==[]
 else:
  r.apollo_transport('POST','/api/v1/people/match',payload)
  assert len(paid)==1
 assert len(reads)==2
