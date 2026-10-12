"""XenTraffic regression: genuine empty financial intake was never dispatched."""
import hashlib,json,os,sys,time
from pathlib import Path
from datetime import datetime,timezone,timedelta
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from affiliate_reporting_xentraffic_j7 import capture,collector
from glp_affiliate_weekly_reporting_j6 import windows

pytestmark = pytest.mark.usefixtures('j7_financial_clock')

class Browser:
    def __init__(self,total=0):self.total=total;self.closed=False
    def js(self,expression):
        if 'Partner ID' in expression:return {'partner':'4384','calendar':True,'currency':True}
        if expression.startswith('window.__j7FinancialRead('):
            return {'http_status':200,'response_sha256':'a'*64,'keys':['conversions','paging'],
                    'paging':{'page':1,'page_size':100,'total_count':self.total},'row_count':self.total,
                    'captured_at':datetime.now(timezone.utc).isoformat()}
        if 'typeof window.__j7FinancialClose' in expression:self.closed=True
        return True
    def goto(self,url):assert url=='https://xentraffic.everflowclient.io/reporting/conversions'

def make(total=0):
    b=Browser(total)
    w=windows(datetime.now(timezone.utc))
    d=capture(b.js,owned_session='test-fake-not-live',window=w,goto_url=b.goto,wait_for_load=lambda:None)
    assert b.closed
    return w,d

def test_xentraffic_real_empty_account_intake_reaches_registered_collector(tmp_path):
    w,d=make()
    p=tmp_path/'input.json';p.write_text(json.dumps(d))
    digest=hashlib.sha256(p.read_bytes()).hexdigest()
    if os.environ.get('J7_REGRESSION_LEGACY'):
        from affiliate_network_access_j6 import weekly_collectors
        c=weekly_collectors(['xentraffic'],object())['xentraffic']
    else:c=collector(p,digest)
    rows=c(w,time.monotonic()+5,32,sink=lambda r:None)
    assert len(rows)==3 and all(r.get('report_complete_period') for r in rows)
    assert all(r['commission_minor']==0 and r['cash_minor'] is None for r in rows)
    assert all(r['financial_classification']=='EMPTY_CONVERSION_POPULATION' for r in rows)
    assert all(r['reporting_timezone']=='America/New_York' for r in rows)
    assert all(r['start_inclusive'].endswith('-04:00') for r in rows)

def test_nonempty_intake_never_becomes_real_or_zero():
    _,d=make(1)
    assert all(not r['report_complete_period'] and r['commission_minor'] is None and r['cash_minor'] is None
               and r['financial_classification']=='UNKNOWN' for r in d['observations'])

def test_missing_tampered_stale_and_mismatched_inputs_refuse(tmp_path):
    w,d=make();p=tmp_path/'input.json';p.write_text(json.dumps(d));sha=hashlib.sha256(p.read_bytes()).hexdigest()
    c=collector(p,sha)
    with pytest.raises(ValueError,match='renewal'):c(dict(w,lookback_start='wrong'),0,1,sink=lambda r:None)
    with pytest.raises(ValueError,match='pin'):collector(p,'0'*64)
    d['captured_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat();p.write_text(json.dumps(d))
    c=collector(p,hashlib.sha256(p.read_bytes()).hexdigest())
    with pytest.raises(ValueError,match='renewal'):c(w,0,1,sink=lambda r:None)
    with pytest.raises(ValueError,match='regular'):collector(tmp_path/'missing',sha)

def test_portal_week_closes_at_new_york_midnight_not_utc(j7_financial_clock):
    """Regression for the Monday 00:00Z–04:00Z CI failure interval."""
    import affiliate_reporting_eden_portal_j7 as eden
    w = windows(datetime(2026, 10, 12, 0, tzinfo=timezone.utc))
    for module, partner in ((__import__('affiliate_reporting_xentraffic_j7'), '4384'), (eden, '62')):
        def js(expression):
            if 'protected_account_required' in expression:
                return {'partner': partner, 'calendar': True, 'currency': True}
            if expression.startswith('window.__j7FinancialRead('):
                return {'http_status': 200, 'response_sha256': 'a'*64,
                        'keys': ['conversions', 'paging'],
                        'paging': {'page': 1, 'page_size': 100, 'total_count': 0},
                        'row_count': 0, 'captured_at': datetime.now(timezone.utc).isoformat()}
            return True
        for hour, minute, closed in ((0, 0, False), (3, 59, False), (4, 0, True), (6, 0, True)):
            j7_financial_clock.current = datetime(2026, 10, 12, hour, minute, tzinfo=timezone.utc)
            doc = module.capture(js, owned_session='synthetic-clock-regression', window=w,
                                 goto_url=lambda url: None, wait_for_load=lambda: None)
            row = next(r for r in doc['observations'] if r['period_purpose'] == 'closed_week')
            assert row['end_exclusive'] == '2026-10-12T00:00:00-04:00'
            assert row['report_complete_period'] is closed
            assert doc['captured_at'] == j7_financial_clock.current.isoformat()


def test_wrong_principal_and_failed_browser_cleanup():
    b=Browser()
    def wrong(e):
        if 'Partner ID' in e:return {'partner':'999','calendar':True,'currency':True}
        return b.js(e)
    with pytest.raises(ValueError,match='protected_account'):capture(wrong,owned_session='test',window={},goto_url=b.goto,wait_for_load=lambda:None)
    assert b.closed
