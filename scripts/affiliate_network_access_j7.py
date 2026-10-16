"""J7 source-owned reporting dispatch; no sender, cash writer or tracking I/O."""
from datetime import datetime,timedelta,timezone
import time
import affiliate_network_access_j6 as prior


def weekly_collectors(names,transport,*,eden_intake=None,eden_sha256=None,cj_resume_root=None):
    collectors=prior.weekly_collectors(names,transport)
    if 'cj' in names and cj_resume_root is not None:
        def cj(window,deadline,max_requests,*,sink):
            import hashlib,json
            from pathlib import Path
            from marketplace_adapters import cj as api
            from glp_affiliate_weekly_reporting_j7 import collect_cj
            try:
                secret,account,_=api._secret()
            except (OSError,ValueError,KeyError):
                return collectors_without_cj(window,deadline,max_requests,sink=sink)
            out=[];remaining=max_requests
            for label,start,end in [('closed_month',window['last_closed_month_start'],window['last_closed_month_end']),
                                    ('closed_week',window['week_start'],window['end_exclusive']),
                                    ('lookback',window['lookback_start'],window['end_exclusive'])]:
                # Exact partition identity survives moving week/lookback windows.
                key=hashlib.sha256(json.dumps([account,start,end]).encode()).hexdigest()
                read_transport=prior.ReadTransport(transport,deadline=deadline,max_requests=remaining)
                pages=collect_cj(read_transport,secret,account,start,end,deadline=deadline,
                                 max_requests=remaining,resume_path=Path(cj_resume_root)/(key+'.json'))
                remaining-=pages[-1]['request_count']
                summary=pages[-1]
                projection=[row for p in pages[:-1] if p.get('payload_complete') for row in p.get('accounting_projection',[])]
                row=dict(summary,period_purpose=label,capability='conversions_earnings',
                    coverage='COMPLETE' if summary['payload_complete'] else 'PARTIAL',
                    status='SUPPORTED', records=projection, returned_row_count=len(projection),
                    declared_row_count=summary['record_count'],
                    account_binding='request:publisher:'+account,
                    reason='ACCOUNT_ROW_COVERAGE_ONLY_CURRENCY_CLASSIFICATION_CASH_UNKNOWN' if summary['payload_complete']
                           else 'COUNT_PAGINATION_OR_PERIOD_PROOF_INCOMPLETE',
                    receipts=[{'http_status':p.get('http_status'),'response_sha256':p.get('response_sha256'),
                               'query_sha256':p.get('query_sha256'),'start_inclusive':p['start_inclusive'],
                               'end_exclusive':p['end_exclusive']} for p in pages[:-1]])
                sink(row);out.append(row)
                if summary['authentication_blocked'] or remaining<=0 or time.monotonic()>=deadline:
                    break
            return out
        collectors_without_cj=collectors['cj']
        collectors['cj']=cj
    if 'everflow' in names:
        from affiliate_reporting_eden_portal_j7 import collector as portal_reader
        portal=portal_reader(eden_intake,eden_sha256) if eden_intake else None
        def generic_everflow(window,deadline,max_requests,*,sink):
            from affiliate_reporting_everflow_roster_j7 import collect
            saved=[];refusal=None
            if portal:
                try:portal(window,deadline,max_requests,sink=saved.append)
                except ValueError:refusal='FRESH_PORTAL_INTAKE_RENEWAL_OR_BINDING_REQUIRED'
            by_period={r['period_purpose']:r for r in saved}
            rows=[];remaining=max_requests
            for label,start,end in [('closed_week',window['week_start'],window['end_exclusive']),
                    ('lookback',window['lookback_start'],window['end_exclusive']),
                    ('closed_month',window['last_closed_month_start'],window['last_closed_month_end'])]:
                result=collect(transport,start=start,end=end,deadline=deadline,max_requests=remaining,eden_observation=by_period.get(label))
                remaining-=len(result.receipts)
                page=result.to_dict()
                page['portal_intake_refusal']=refusal
                page.update(schema='coverage_observation',period_purpose=label,start_inclusive=start,end_exclusive=end,
                    payload_complete=False,account_scope_complete=False,financial_classification='UNKNOWN',
                    reporting_timezone='UNKNOWN_VENDOR_QUERY_CALENDAR')
                sink(page);rows.append(page)
            return rows
        collectors['everflow']=generic_everflow
    if 'maxbounty' in names:
        def maxbounty(window,deadline,max_requests,*,sink):
            from affiliate_reporting_maxbounty_j7 import collect
            out=[];remaining=max_requests
            for label,start,end in [('closed_week',window['week_start'],window['end_exclusive']),
                                    ('lookback',window['lookback_start'],window['end_exclusive']),
                                    ('closed_month',window['last_closed_month_start'],window['last_closed_month_end'])]:
                report=collect(transport,start=start,end=end,deadline=deadline,max_requests=remaining)
                remaining-=len(report.receipts)
                row=report.to_dict()
                row.update(schema='coverage_observation',period_purpose=label,start_inclusive=start,end_exclusive=end,
                           payload_complete=False,account_scope_complete=False,financial_classification='UNKNOWN',
                           reporting_timezone='UNKNOWN_VENDOR_CALENDAR')
                sink(row);out.append(row)
                if any(r['http_status'] in (401,403,429) for r in report.receipts):break
            return out
        collectors['maxbounty']=maxbounty
    if 'katalys' in names:
        def katalys(window,deadline,max_requests,*,sink):
            from marketplace_adapters import katalys_j7 as api
            out=[]
            for label,start,end in [('closed_week',window['week_start'],window['end_exclusive']),
                                    ('lookback',window['lookback_start'],window['end_exclusive']),
                                    ('closed_month',window['last_closed_month_start'],window['last_closed_month_end'])]:
                if time.monotonic()>=deadline or 2*(len(out)+1)>max_requests:break
                report=api.conversion_report(start[:10],(datetime.fromisoformat(end)-timedelta(days=1)).date().isoformat(),
                    transport,profile_id='098e5455-ca72-4099-9ff9-a9dd014148e7',timezone='UTC')
                complete=report.get('status')=='PASS' and report.get('completeness')=='COMPLETE' and report.get('period_state')=='CLOSED'
                # No projection of nonempty rows without an exclusion/classification crosswalk.
                empty=complete and report.get('rows')==[] and report.get('row_count')==0
                row={'schema':'coverage_observation','network':'katalys','capability':'conversions_earnings',
                     'status':'SUPPORTED','source':api.REPORT_API+'conversions','coverage':'COMPLETE' if empty else 'PARTIAL',
                     'period_purpose':label,'start_inclusive':start,'end_exclusive':end,'reporting_timezone':'UTC',
                     'account_binding':'RETAINED_PROFILE_BINDING:'+report['profile_id'],'profile_id':report['profile_id'],
                     'payload_complete':empty,'account_scope_complete':True,'report_complete_period':empty,
                     'record_count':0 if empty else None,'records':[], 'declared_row_count':report.get('vendor_rowCount'),
                     'returned_row_count':report.get('row_count'),'commission_minor':None,'cash_minor':None,'currency':None,
                     'financial_classification':'EMPTY_CONVERSION_POPULATION_NO_REAL_EVENTS' if empty else 'UNKNOWN',
                     'reason':report.get('reason','DOCUMENTED_ROWS_READ_NO_MONEY_CLASSIFICATION'),
                     'corroboration':report.get('corroboration'),'receipts':[{'http_status':report.get('http_status'),
                        'endpoint':api.REPORT_API+'conversions','response_sha256':report.get('response_sha256'),'query':report.get('query')}]}
                sink(row);out.append(row)
                if report.get('http_status') in (401,403,429):break
            return out
        collectors['katalys']=katalys
    return collectors
