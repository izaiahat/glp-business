"""Explicit successor to the frozen K3 access contract; only MaxBounty changes.

Legacy routes, readers and response oracles remain byte-identical. New reporting
acceptance is additive, never a repin or relaxation of the retained generation.
"""
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse
import affiliate_network_access as legacy
_window = legacy._window


@dataclass(frozen=True)
class AccessResult(legacy.AccessResult):
    reported_totals: dict | None = None


class ReadTransport(legacy.ReadTransport):
    def request(self, method, url, **kwargs):
        parsed = urlparse(url)
        if parsed.hostname != 'api.maxbounty.com':
            return super().request(method, url, **kwargs)
        if (method != 'GET' or parsed.scheme != 'https' or parsed.netloc != 'api.maxbounty.com'
                or not re.fullmatch(r'/affiliates/api/(affiliate|reports/(earnings|conversions|reversals)|accountBalance/[0-9]{4})', parsed.path)):
            raise ValueError('route_not_read_allowlisted')
        if len(self.receipts) >= self.max_requests or time.monotonic() >= self.deadline:
            raise ValueError('budget_exhausted')
        kwargs['timeout'] = max(.1, min(30, self.deadline-time.monotonic()))
        response = self.transport.request(method, url, **kwargs)
        receipt = {'method': method, 'endpoint':url.split('?')[0], 'http_status':response.status,
                   'response_sha256':hashlib.sha256(response.body).hexdigest(),
                   'fetched_at':datetime.now(timezone.utc).isoformat()}
        self.receipts.append(receipt)
        if response.status != 200:
            raise legacy.ProviderResponseError('http_status_unproven')
        doc = response.json()
        if not isinstance(doc, dict) or doc.get('success') is not True:
            raise legacy.ProviderResponseError('provider_response_unproven')
        self.raw_documents.append(doc)
        return response


def conversions_earnings(network, transport, **kwargs):
    if network != 'maxbounty':
        return legacy.conversions_earnings(network, transport, **kwargs)
    from affiliate_reporting_maxbounty import collect
    return collect(transport, **kwargs)


def weekly_collectors(names, transport):
    collectors = legacy.weekly_collectors(names, transport)
    if 'maxbounty' not in names:
        return collectors
    def collect(window, deadline, max_requests, *, sink):
        observations, remaining = [], max_requests
        for label, start, end in [('closed_week',window['week_start'],window['end_exclusive']),
                                  ('lookback',window['lookback_start'],window['end_exclusive']),
                                  ('closed_month',window['last_closed_month_start'],window['last_closed_month_end'])]:
            result = conversions_earnings('maxbounty',transport,start=start,end=end,deadline=deadline,max_requests=remaining)
            remaining -= len(result.receipts)
            page=result.to_dict()
            page.update(schema='coverage_observation',period_purpose=label,start_inclusive=start,end_exclusive=end,
                        payload_complete=False,account_scope_complete=False,financial_classification='UNKNOWN',
                        reporting_timezone='UNKNOWN_VENDOR_CALENDAR')
            sink(page)
            observations.append(page)
            if any(r['http_status'] in (401,403,429) for r in result.receipts):
                break
        return observations
    collectors['maxbounty'] = collect
    return collectors
