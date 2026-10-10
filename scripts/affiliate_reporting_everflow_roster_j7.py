"""Read bound Everflow roster partitions; never infer whole-network coverage.

Wisp and LifeRx remain unbound. Account money remains unknown. Read-only
financial intake does not clear any brand or provider hold.
"""
from dataclasses import dataclass
from affiliate_network_access import AccessResult
import affiliate_reporting_everflow as official

TENANT_HOSTS = {
    'everflow_eden': 'eden.everflowclient.io',
    'everflow_reverse_health': 'reversehealth.everflowclient.io',
    'everflow_orderly': 'orderlywellnesscorporation.everflowclient.io',
}

@dataclass(frozen=True)
class Result(AccessResult):
    tenant_partitions: tuple[dict, ...] = ()
    unbound_tenants: tuple[str, ...] = ('liferxmd.everflowclient.io', 'wisp.everflowclient.io')


def collect(transport, *, start, end, deadline=None, max_requests=32):
    partitions, receipts, records = [], [], []
    remaining = max_requests
    for tenant, host in TENANT_HOSTS.items():
        if remaining <= 0:
            break
        result = official.collect(transport, tenant=tenant, start=start, end=end,
                                  deadline=deadline, max_requests=remaining).to_dict()
        partitions.append(dict(result, tenant_host=host))
        receipts.extend(dict(row, tenant_host=host) for row in result['receipts'])
        records.extend(dict(row, tenant_host=host, tenant=tenant) for row in result['records'])
        remaining -= len(result['receipts'])
    return Result('everflow', 'conversions_earnings', 'SUPPORTED', official.ENDPOINT,
        coverage='PARTIAL' if receipts else 'UNKNOWN', records=tuple(records), receipts=tuple(receipts),
        account_binding='EXPLICIT_MULTI_TENANT_HOST_CROSSWALK_NOT_RESPONSE_OWNED_IDENTITY',
        report_complete_period=False, tenant_partitions=tuple(partitions),
        reason='PARTIAL_ROSTER_OFFICIAL_FINANCIAL_READERS_BOUND_WISP_LIFERX_AND_FINANCIAL_LINEAGE_UNKNOWN')
