import os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from affiliate_network_access import AccessResult
import affiliate_reporting_everflow_roster_j7 as roster


def test_generic_everflow_financial_roster_is_not_untyped_default_or_portal_only(monkeypatch):
    import affiliate_network_access_j6 as old
    calls=[]
    def official(transport,**kwargs):
        tenant=kwargs['tenant'];calls.append(tenant)
        return AccessResult(tenant,'conversions_earnings','SUPPORTED','https://api.eflow.team/v1/affiliates/reporting/conversions',
            coverage='PARTIAL',receipts=({'http_status':200,'response_sha256':'b'*64},),
            account_binding='CONFIGURED_PARTNER_KEY:'+tenant)
    monkeypatch.setattr(roster.official,'collect',official)
    args={'start':'2026-09-01T00:00:00+00:00','end':'2026-10-01T00:00:00+00:00'}
    result=(old.conversions_earnings('everflow',object(),**args) if os.getenv('J7_REGRESSION_LEGACY')
            else roster.collect(object(),**args)).to_dict()
    assert result['status']=='SUPPORTED' and len(result['receipts'])==3
    assert calls==['everflow_eden','everflow_reverse_health','everflow_orderly']
    assert result['unbound_tenants']==('liferxmd.everflowclient.io','wisp.everflowclient.io')
    assert result['report_complete_period'] is False
    assert result['cash_minor'] is None and result['commission_minor'] is None
    assert [x['tenant_host'] for x in result['tenant_partitions']]==list(roster.TENANT_HOSTS.values())
