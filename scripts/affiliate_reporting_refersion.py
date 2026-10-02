#!/usr/bin/env python3
"""Read-only marketplace-affiliate Refersion reporting, not ledger or cash proof.

Reviewed 2026-10-02 official contracts:
https://www.refersion.dev/docs/graphql/for-marketplace-affiliates/example-queries/
https://www.refersion.dev/docs/graphql/reference/queries/
https://www.refersion.dev/docs/graphql/reference/types/conversion/
https://www.refersion.dev/docs/graphql/reference/types/payment/
https://www.refersion.dev/docs/graphql/pagination/

created_from is exclusive; created_to inclusive. first forces id ascending.
pageInfo is deprecated/inaccurate. A terminal empty page after observed rows
proves enumeration of the API-visible population only: archived conversions
and cancelled payments are omitted by the provider. No provider hold changes.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from affiliate_network_access import AccessResult, ReadTransport, _window

ENDPOINT = 'https://graphql.refersion.com'
FIELDS = {
    'conversions': 'id created status commission_total currency offer { id } affiliate { id } shop { name } payment { id created }',
    'payments': 'id created commission_total currency total_conversions shop { name }',
}


def collect(transport, *, start, end, deadline=None, max_requests=32):
    """Collect separate created-time populations for a UTC half-open window."""
    from marketplace_adapters import _base
    reader = ReadTransport(transport, deadline=deadline, max_requests=max_requests)
    records = []
    validated_pages = 0
    try:
        a, b = _window(start, end)
        token = _base.secret_text('glpclinicscout', 'refersion_graphql_token')
        if not isinstance(token, str) or not token.strip():
            raise ValueError('credential_missing')
        complete = True
        for kind in ('conversions', 'payments'):
            offset = 0
            previous_id = 0
            while True:
                query = ('{ %s(created_from:%d created_to:%d first:200 offset:%d) { %s } }'
                         % (kind, int(a.timestamp())-1, int(b.timestamp())-1, offset, FIELDS[kind]))
                doc = reader.request('POST', ENDPOINT,
                    headers={'X-Refersion-Key': token, 'Content-Type': 'application/json'},
                    json_body={'query': query}).json()
                if not isinstance(doc, dict) or not isinstance(doc.get('data'), dict):
                    raise ValueError('schema_invalid')
                rows = doc['data'].get(kind)
                if not isinstance(rows, list) or len(rows) > 200 or any(not isinstance(r, dict) for r in rows):
                    raise ValueError('rows_invalid')
                if not rows:
                    validated_pages += 1
                    complete = complete and offset > 0
                    break
                for row in rows:
                    identity = row.get('id')
                    if type(identity) is not int or identity <= previous_id:
                        raise ValueError('identity_or_page_order_invalid')
                    previous_id = identity
                    created = row.get('created')
                    if not isinstance(created, str) or not re.fullmatch(r'[0-9]{1,10}', created):
                        raise ValueError('created_invalid')
                    if not int(a.timestamp()) <= int(created) < int(b.timestamp()):
                        raise ValueError('outside_requested_window')
                    shop = row.get('shop')
                    if not isinstance(shop, dict) or not isinstance(shop.get('name'), str) or not shop['name'].strip():
                        raise ValueError('shop_identity_missing_for_exclusion')
                    if 'fella' in shop['name'].casefold():
                        continue
                    try:
                        amount = Decimal(str(row.get('commission_total')))
                        money = str(amount) if amount.is_finite() else None
                    except (InvalidOperation, ValueError):
                        money = None
                    currency = row.get('currency')
                    currency = currency if isinstance(currency, str) and re.fullmatch(r'[A-Z]{3}', currency) else None
                    status = row.get('status')
                    status = status if status in {'APPROVED', 'PENDING', 'DENIED', 'UNQUALIFIED'} else 'UNKNOWN'
                    program = None
                    if kind == 'conversions':
                        offer = row.get('offer')
                        if offer is not None:
                            if not isinstance(offer, dict):
                                raise ValueError('offer_schema_invalid')
                            program = offer.get('id')
                            if program is not None and (type(program) is not int or program <= 0):
                                raise ValueError('offer_identity_invalid')
                    records.append({'record_type': 'conversion' if kind == 'conversions' else 'payment',
                        'record_id': str(row['id']), 'program_id': str(program) if program is not None else None,
                        'source_status': status, 'reported_amount': money,
                        'amount_semantics': 'reported_commission_not_cash' if kind == 'conversions' else 'reported_payment_not_cash',
                        'currency': currency, 'classification': 'UNKNOWN', 'attribution': 'UNKNOWN',
                        'provider_hold': 'UNCHANGED', 'source_timezone': 'UTC_UNIX_SECONDS',
                        'occurred_at': datetime.fromtimestamp(int(created), timezone.utc).isoformat()})
                validated_pages += 1
                offset += len(rows)
        return AccessResult('refersion', 'conversions_earnings', 'SUPPORTED', ENDPOINT,
            coverage='COMPLETE' if complete else 'PARTIAL', records=tuple(records),
            record_count=len(records) if complete else None,
            receipts=tuple(reader.receipts), account_binding='CONFIGURED_CREDENTIAL_NOT_RESPONSE_OWNED_IDENTITY',
            report_complete_period=complete and b <= datetime.now(timezone.utc),
            reason='API_VISIBLE_CONVERSIONS_PAYMENTS_DISTINCT_ARCHIVED_CANCELLED_EXCLUDED_CASH_UNKNOWN'
                if complete else 'EMPTY_POPULATION_WITHOUT_INDEPENDENT_COUNT_PROOF')
    except Exception as error:
        if isinstance(error, ValueError) and str(error) == 'read_budget_exhausted' and validated_pages:
            return AccessResult('refersion', 'conversions_earnings', 'SUPPORTED', ENDPOINT,
                coverage='PARTIAL', records=tuple(records), receipts=tuple(reader.receipts),
                account_binding='CONFIGURED_CREDENTIAL_NOT_RESPONSE_OWNED_IDENTITY',
                reason='READ_BUDGET_EXHAUSTED_AFTER_VALIDATED_PAGE_NO_RETRY')
        return AccessResult('refersion', 'conversions_earnings', 'SUPPORTED', ENDPOINT,
            coverage='UNKNOWN', receipts=tuple(reader.receipts),
            reason='CREDENTIAL_HTTP_SCHEMA_OR_BUDGET_FAILURE_NO_RETRY')
