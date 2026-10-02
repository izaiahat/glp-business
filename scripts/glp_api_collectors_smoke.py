#!/usr/bin/env python3
"""One bounded read per new network; failed credentials/endpoints stay UNKNOWN.

A smoke row count describes one HTTP response, never a period revenue total.
Reserving the output before I/O prevents an accidental second smoke run.
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from affiliate_network_access import ADDITIONAL_REPORTING_NETWORKS, conversions_earnings
from glp_affiliate_weekly_reporting import save, windows
from marketplace_adapters._base import RequestsTransport


def _row_count(network, response):
    if not response.ok:
        return None
    try:
        document=response.json()
        if isinstance(document,dict) and (document.get('error') or document.get('errors')):
            return None
        if network == 'partnerstack':
            rows=document.get('data',{}).get('items')
        elif network == 'admitad':
            rows=document.get('results')
        elif network == 'refersion':
            rows=document.get('data',{}).get('conversions')
        elif network == 'clickbank':
            accounts=document.get('accountData') if isinstance(document,dict) else document
            if not isinstance(accounts,list) or any(not isinstance(a,dict) or not isinstance(a.get('quickStats'),list) for a in accounts):
                return None
            return sum(len(a['quickStats']) for a in accounts)
        else:
            return None
        return len(rows) if isinstance(rows,list) else None
    except (ValueError,AttributeError,TypeError):
        return None


class OneCallTransport:
    def __init__(self, network, transport):
        self.network=network
        self.transport=transport
        self.calls=0
        self.http_status=None
        self.row_count=None
        if isinstance(transport,RequestsTransport):
            transport._sess().max_redirects=0

    def request(self, method, url, **kwargs):
        if self.calls:
            raise ValueError('smoke_one_request_only')
        self.calls+=1
        response=self.transport.request(method,url,**kwargs)
        self.http_status=response.status
        self.row_count=_row_count(self.network,response)
        return response


def run_smoke(output, *, as_of=None, transport_factory=RequestsTransport):
    output=Path(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    os.close(fd)
    as_of=as_of or datetime.now(timezone.utc)
    window=windows(as_of)
    report={'schema':'affiliate-api-smoke-v1','captured_at':as_of.isoformat(),
        'start_inclusive':window['week_start'],'end_exclusive':window['end_exclusive'],
        'reporting_timezone':'UTC','count_scope':'SINGLE_RESPONSE_NOT_COMPLETE_PERIOD',
        'provider_mutations':0,'tracking_clicks':0,'networks':{}}
    save(output,report)
    for network in ADDITIONAL_REPORTING_NETWORKS:
        transport=OneCallTransport(network,transport_factory())
        result=conversions_earnings(network,transport,start=window['week_start'],
            end=window['end_exclusive'],deadline=time.monotonic()+35,max_requests=1)
        report['networks'][network]={'http_status':transport.http_status,'row_count':transport.row_count,
            'request_count':transport.calls,'coverage':result.coverage,'reason':result.reason,
            'report_complete_period':result.report_complete_period}
        save(output,report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=run_smoke(args.output)
    print(json.dumps(report,sort_keys=True))


if __name__=='__main__':
    main()
