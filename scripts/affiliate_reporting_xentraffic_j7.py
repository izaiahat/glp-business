"""Fresh AI/vault XenTraffic financial intake; no credential/profile access.

The portal itself supplies the account currency/calendar and the exact reporting
request. This reader performs only the observed conversions reporting POST.
Empty conversion reports substantiate zero reported commission, NOT zero cash.
Nonempty reports remain partial until native classification/settlement is bound.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

ORIGIN = 'https://xentraffic.everflowclient.io'
REPORT = '/api/affiliates/reporting/conversions'


def capture(js, *, owned_session, window, goto_url, wait_for_load):
    try:
        return _capture(js, owned_session=owned_session, window=window, goto_url=goto_url, wait_for_load=wait_for_load)
    finally:
        js('typeof window.__j7FinancialClose==="function" ? (window.__j7FinancialClose(),true) : true')


def _capture(js, *, owned_session, window, goto_url, wait_for_load):
    if not owned_session:
        raise ValueError('fresh_owned_ai_vault_session_required')
    # No arbitrary state/cookie/storage inspection; exact protected page only.
    identity = js('''(() => {if(location.origin!=="https://xentraffic.everflowclient.io"||location.pathname!=="/myaccount")throw Error("protected_account_required");let t=document.body.innerText;return {partner:(t.match(/Partner ID\\s+(\\d+)/)||[])[1],calendar:t.includes("America/New_York"),currency:t.includes("US Dollar (USD)")};})()''')
    if identity != {'partner': '4384', 'calendar': True, 'currency': True}:
        raise ValueError('protected_account_calendar_currency_unverified')
    goto_url(ORIGIN+'/reporting/conversions')
    wait_for_load()
    import time
    for _ in range(40):
        if js('document.body.innerText.includes("Run Report")'):
            break
        time.sleep(.25)
    # Reuse only this page's authenticated reporting request, entirely inside
    # the fresh browser. Headers never leave the closure or enter a receipt.
    # No cookie/localStorage/profile export or persistence is performed.
    js('''(() => {if(location.origin!=="https://xentraffic.everflowclient.io"||location.pathname!=="/reporting/conversions")throw Error("report_page_required");let original=window.fetch,template=null;window.fetch=function(input,init){let r=new Request(input,init),u=new URL(r.url);if(r.method==="POST"&&u.origin===location.origin&&u.pathname==="/api/affiliates/reporting/conversions")template=r.clone();return original.apply(this,arguments);};window.__j7FinancialReady=()=>template!==null;window.__j7FinancialRead=async q=>{if(!template||location.origin!=="https://xentraffic.everflowclient.io"||location.pathname!=="/reporting/conversions")throw Error("report_auth_not_bound");let r=await original(new Request(template,{body:JSON.stringify(q),redirect:"error",signal:AbortSignal.timeout(30000)}));let raw=await r.text(),d=JSON.parse(raw);return {http_status:r.status,response_sha256:Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(raw)))).map(x=>x.toString(16).padStart(2,"0")).join(""),keys:Object.keys(d).sort(),paging:d.paging,row_count:Array.isArray(d.conversions)?d.conversions.length:null,captured_at:new Date().toISOString()};};window.__j7FinancialClose=()=>{template=null;window.fetch=original;delete window.__j7FinancialRead;delete window.__j7FinancialReady;delete window.__j7FinancialClose;};Array.from(document.querySelectorAll("button")).find(x=>x.innerText.trim()==="Run Report").click();return true;})()''')
    # Changing the visible preset invalidates the portal's query cache; an
    # unchanged Run Report click can reuse data without emitting a request.
    js('Array.from(document.querySelectorAll("button")).find(x=>x.querySelector(".ef-icon-calendar")).click()')
    js('Array.from(document.querySelectorAll("*")).find(x=>x.children.length===0&&x.innerText?.trim()==="Last Week").click()')
    js('Array.from(document.querySelectorAll("button")).find(x=>x.innerText.trim()==="Run Report").click()')
    for _ in range(40):
        if js('window.__j7FinancialReady()'):
            break
        time.sleep(.25)
    observations = []
    for label, start, end in [('closed_week',window['week_start'],window['end_exclusive']),
                              ('lookback',window['lookback_start'],window['end_exclusive']),
                              ('closed_month',window['last_closed_month_start'],window['last_closed_month_end'])]:
        a, b = start[:10], end[:10]
        inclusive_end = (datetime.fromisoformat(b)-timedelta(days=1)).date().isoformat()
        query = {'timezone_id':80,'from':a,'to':inclusive_end,'show_events':True,'show_conversions':True,
                 'query':{'filters':[],'search_terms':[]}}
        # Project a minimal envelope before data leaves the browser. No IPs,
        # customer fields, tokens, cookies or arbitrary provider text returned.
        expression = 'window.__j7FinancialRead('+json.dumps(query)+')'
        receipt = js(expression)
        paging = receipt.get('paging')
        if (receipt.get('http_status')!=200 or receipt.get('keys')!=['conversions','paging']
            or not isinstance(paging,dict) or paging.get('page')!=1
            or type(paging.get('total_count')) is not int or type(receipt.get('row_count')) is not int
            or not re.fullmatch('[a-f0-9]{64}',receipt.get('response_sha256',''))):
            raise ValueError('report_envelope_unverified')
        empty = receipt['row_count']==paging['total_count']==0
        start_actual=datetime.fromisoformat(a).replace(tzinfo=ZoneInfo('America/New_York')).isoformat()
        end_actual=datetime.fromisoformat(b).replace(tzinfo=ZoneInfo('America/New_York')).isoformat()
        closed=datetime.fromisoformat(end_actual)<=datetime.now(timezone.utc)
        observations.append({'schema':'coverage_observation','network':'xentraffic','status':'PORTAL_FINANCIAL_READ',
            'account_id':'4384','tenant_id':'xentraffic.everflowclient.io','account_binding':'RESPONSE_OWNED_PARTNER_ID:4384',
            'period_purpose':label,'requested_utc_window':{'start':start,'end':end},
            'start_inclusive':start_actual,'end_exclusive':end_actual,'reporting_timezone':'America/New_York',
            'query':query,'receipts':[dict(receipt,endpoint=ORIGIN+REPORT)],'record_count':receipt['row_count'],
            'payload_complete':empty,'report_complete_period':empty and closed,'account_scope_complete':empty,
            'financial_classification':'EMPTY_CONVERSION_POPULATION' if empty else 'UNKNOWN',
            'currency':'USD','commission_minor':0 if empty else None,'cash_minor':None,'records':[],
            'reason':'SOURCE_EMPTY_CONVERSIONS_ZERO_COMMISSION_NOT_PAYOUT_STATEMENT' if empty else 'NONEMPTY_NATIVE_CLASSIFICATION_AND_PAGINATION_REQUIRED',
            'reported_totals':{'conversions':receipt['row_count'],'commission_minor':0 if empty else None,'cash_minor':None},
            'request_count':1})
    return {'schema':'xentraffic-fresh-financial-v1','captured_at':datetime.now(timezone.utc).isoformat(),
            'transport':'fresh_ai_browser_vault','owned_session':owned_session,'window':window,
            'observations':observations,'effects':0,'tracking_requests':0,'secrets_printed':False}


def collector(path, expected_sha256):
    """One pinned immutable intake, explicitly dated; never a persistent session."""
    path=Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('regular_financial_intake_required')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha256:
        raise ValueError('financial_intake_pin_mismatch')
    data=json.loads(raw)
    if data.get('schema')!='xentraffic-fresh-financial-v1' or data.get('transport')!='fresh_ai_browser_vault':
        raise ValueError('financial_intake_schema')
    def collect(window, deadline, max_requests, *, sink):
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(data['captured_at'])).total_seconds()
        if not 0<=age<=86400 or window!=data['window']:
            raise ValueError('fresh_vault_financial_intake_renewal_required')
        rows=data['observations']
        if len(rows)!=3 or {r['period_purpose'] for r in rows}!={'closed_week','lookback','closed_month'}:
            raise ValueError('financial_intake_periods_incomplete')
        for row in rows:
            sink(dict(row,intake_sha256=expected_sha256,intake_source=str(path),intake_captured_at=data['captured_at']))
        return rows
    return collect
