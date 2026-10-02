"""Explicit reporting gap; offer inventory is not affiliate earnings evidence.

The vendor-referenced Apiary documentation host could not be accessed and
HealthTrader's public tour describes portal exports without an API contract.
Do not guess a reporting path or turn offers/list into financial reporting.
"""
from affiliate_network_access import AccessResult, _window

SOURCE = 'https://api.healthtrader.com/affiliates/v1'
DOCS = ('http://docs.htaffiliate.apiary.io', 'https://healthtrader.com/us/tour')
REPORTING_TIMEZONE = 'UNKNOWN'
TIME_AXIS = 'UNKNOWN'


def collect(transport, *, start, end, deadline=None, max_requests=32):
    _window(start, end)
    return AccessResult('healthtrader','conversions_earnings','UNKNOWN',SOURCE,
        coverage='UNKNOWN',reason='OFFICIAL_REPORTING_ENDPOINT_UNVERIFIED_SKIP')
