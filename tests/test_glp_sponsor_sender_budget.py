import datetime as dt
import pytest
import glp_sponsor_campaign as c
import glp_sponsor_readiness as r
import glp_sponsor_firecrawl_supply as supply
import firecrawl_client as fc
import glp_firecrawl_jobs as jobs

@pytest.mark.parametrize('remaining,at,start,end,allowed',[
 (39999,'2026-10-14','2026-09-19','2026-10-19',False),
 (40029,'2026-10-14','2026-09-19','2026-10-19',False),
 (40030,'2026-10-14','2026-09-19','2026-10-19',True),
 (54000,'2026-10-14','2026-09-19','2026-10-19',True),
 (90000,'2026-10-14','2026-09-19','2026-10-19',True),
 (54000,'2026-10-20','2026-10-19','2026-11-19',False),
 (54000,'2026-09-14','2026-08-19','2026-09-19',False),
 (54000,'2026-10-19','2026-09-19','2026-10-19',True),
])
def test_sender_match_rechecks_shared_period_before_transport(monkeypatch,tmp_path,remaining,at,start,end,allowed):
 monkeypatch.setattr(c,'STATE',tmp_path)
 monkeypatch.setattr(fc,'LEDGER',tmp_path/'ops.jsonl')
 def spent(callers):
  assert 'sponsor_campaign' in callers
  return 0
 monkeypatch.setattr(jobs,'spent_today',spent)
 monkeypatch.setattr(supply,'now',lambda:dt.datetime.fromisoformat(at+'T12:00:00+00:00'))
 assert supply.RUN_CREDIT_CAP == 1500 and supply.FLOOR_CREDITS == 3000
 monkeypatch.setattr(supply.Producer,'discover_apollo_contacts',lambda self:setattr(self,'apollo_capabilities',{'people/match':'fixture-match'}))
 reads=[];paid=[]
 def request(self,endpoint,payload=None,**kw):
  if endpoint=='team/credit-usage':
   reads.append(endpoint)
   return {'data':{'remainingCredits':90000 if len(reads)==1 else remaining,'planCredits':100000,'billingPeriodStart':start+'T23:59:59Z','billingPeriodEnd':end+'T23:59:59.000Z'}}
  paid.append(payload)
  return {'success':True,'creditsUsed':30,'data':{'alexandria':[{'provider':'apollo','capability':'fixture-match','upstreamStatus':200,'data':{'person':{}}}]}}
 monkeypatch.setattr(fc.FirecrawlClient,'request',request)
 payload={k:False for k in ('reveal_personal_emails','reveal_phone_number','run_waterfall_email','run_waterfall_phone')}
 payload['id']='SYNTHETIC-NOT-A-PERSON'
 if not allowed:
  with pytest.raises(RuntimeError,match='sponsor_period_credit_cap'):
   r.apollo_transport('POST','/api/v1/people/match',payload)
  assert paid==[]
 else:
  r.apollo_transport('POST','/api/v1/people/match',payload)
  assert len(paid)==1
 assert len(reads)==2
