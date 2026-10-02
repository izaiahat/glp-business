#!/usr/bin/env python3
"""Read-only Everflow partner conversion reporting for four isolated tenants.

Contract (task brief, from https://developers.everflow.io/api-reference/post-affiliatesreportingconversions):
POST https://api.eflow.team/v1/affiliates/reporting/conversions with header
X-Eflow-API-Key (affiliate/partner key only; network, advertiser and
marketplace keys are not interchangeable). JSON body from/to dates, required
timezone_id, show_conversions and show_events. Query page/page_size (defaults
1/50); response paging.total_count. Dates must be within the prior 365 days.

Each tenant is its own account and key. Identity, credentials, receipts and
completeness are never shared between tenants. COMPLETE only when every page
is fetched and the summed rows equal paging.total_count. Events stay distinct
from base conversions; zero-payout actions are not paid customers; offer terms
never become commission or cash; TEST/REAL and currency stay UNKNOWN unless a
row states them. The timezone_id mapping was not verified offline, so the
reporting timezone remains UNKNOWN_TIMEZONE_ID_UNVERIFIED.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

from affiliate_network_access import AccessResult, ReadTransport, ProviderResponseError, _window

ENDPOINT = 'https://api.eflow.team/v1/affiliates/reporting/conversions'
PAGE_SIZE = 50
TIMEZONE_ID = 67  # requested as UTC; mapping unverified offline, see module docstring
TENANTS = {
    'everflow_orderly': 'OrderlyMeds',
    'everflow_ro': 'Ro',
    'everflow_eden': 'Eden',
    'everflow_reverse_health': 'Reverse Health',
}


class CountMismatch(ValueError):
    """Pagination or total_count inconsistency after validated pages."""


def _key(tenant):
    from marketplace_adapters import _base
    try:
        value = _base.secret_text('glpclinicscout', tenant + '_affiliate_api_key')
    except Exception:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _identifier(value):
    return (str(value) if isinstance(value, (str, int)) and not isinstance(value, bool)
            and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(value)) else None)


def _amount(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _project(tenant, row):
    identity = _identifier(row.get('conversion_id'))
    if identity is None:
        raise ValueError('identity')
    is_event = row.get('is_event')
    if type(is_event) is not bool:
        raise ValueError('event_flag_schema')
    event = row.get('event')
    event = event if is_event and isinstance(event, str) and re.fullmatch(r'[\w .:/-]{1,80}', event) else None
    payout = _amount(row.get('payout'))
    offer = row.get('relationship', {}).get('offer') if isinstance(row.get('relationship'), dict) else None
    currency = row.get('currency_id')
    stamp = row.get('conversion_unix_timestamp')
    status = row.get('status')
    test = row.get('is_test')
    return {'tenant': tenant, 'record_id': identity,
            'record_type': 'event' if is_event else 'base_conversion', 'event_name': event,
            'program_id': _identifier(offer.get('network_offer_id')) if isinstance(offer, dict) else None,
            'source_status': status if status in {'approved', 'pending', 'invalid', 'rejected'} else 'unknown',
            'reported_payout': str(payout) if payout is not None else None,
            'payout_semantics': 'zero_payout_action_not_paid_customer' if payout == 0 else 'reported_payout_not_cash',
            'paid_customer': 'UNKNOWN',
            'currency': currency if isinstance(currency, str) and re.fullmatch(r'[A-Z]{3}', currency) else None,
            'classification': {True: 'TEST', False: 'REAL'}.get(test) if type(test) is bool else 'UNKNOWN',
            'attribution': 'UNKNOWN', 'provider_hold': 'UNCHANGED',
            'occurred_at': datetime.fromtimestamp(stamp, timezone.utc).isoformat()
                if type(stamp) is int and stamp > 0 else None}


def collect(transport, *, tenant, start, end, deadline=None, max_requests=32, now=None):
    """Collect one tenant partition for a whole-UTC-day half-open window."""
    binding = 'CONFIGURED_PARTNER_KEY:' + tenant + ':NOT_RESPONSE_OWNED_IDENTITY'
    reader = ReadTransport(transport, deadline=deadline, max_requests=max_requests)
    records = []
    validated_pages = 0

    def result(coverage, reason, **extra):
        return AccessResult(tenant, 'conversions_earnings', 'SUPPORTED', ENDPOINT, coverage=coverage,
            receipts=tuple(reader.receipts), account_binding=binding, reason=reason, **extra)

    if tenant not in TENANTS:
        return AccessResult(tenant, 'conversions_earnings', reason='UNKNOWN_EVERFLOW_TENANT')
    try:
        a, b = _window(start, end)
        if any((a.hour, a.minute, a.second, b.hour, b.minute, b.second)):
            raise ValueError('whole_utc_day_window_required')
        now = now or datetime.now(timezone.utc)
        if a < now - timedelta(days=365) or b > now + timedelta(days=1):
            raise ValueError('window_outside_prior_365_days')
    except ValueError:
        return result('UNKNOWN', 'WINDOW_INVALID_OR_OUTSIDE_PRIOR_365_DAYS_NO_REQUEST')
    key = _key(tenant)
    if key is None:
        return result('UNKNOWN', 'UNKNOWN_CREDENTIAL_MISSING')
    body = {'from': a.date().isoformat(), 'to': (b - timedelta(days=1)).date().isoformat(),
            'timezone_id': TIMEZONE_ID, 'show_conversions': True, 'show_events': True}
    headers = {'X-Eflow-API-Key': key, 'Content-Type': 'application/json'}
    try:
        expected, seen, number = None, set(), 1
        while True:
            doc = reader.request('POST', ENDPOINT, headers=headers, json_body=body,
                                 params={'page': number, 'page_size': PAGE_SIZE}).json()
            if not isinstance(doc, dict) or not isinstance(doc.get('paging'), dict):
                raise ValueError('schema_invalid')
            rows, total = doc.get('conversions'), doc['paging'].get('total_count')
            if (not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0
                    or any(not isinstance(r, dict) for r in rows)):
                raise ValueError('schema_invalid')
            projected = [_project(tenant, r) for r in rows]
            if expected is not None and total != expected:
                raise CountMismatch('total_count_changed')
            expected = total
            for record in projected:
                if record['record_id'] in seen:
                    raise CountMismatch('duplicate_conversion')
                seen.add(record['record_id'])
            records.extend(projected)
            validated_pages += 1
            if len(records) > expected:
                raise CountMismatch('rows_exceed_total_count')
            if len(records) == expected:
                break
            if not rows:
                raise CountMismatch('empty_page_before_total_count')
            number += 1
        return result('COMPLETE',
            'ROWS_EQUAL_TOTAL_COUNT_EVENTS_DISTINCT_REVENUE_NOT_ZERO_CASH_TIMEZONE_UNKNOWN',
            records=tuple(records), record_count=len(records),
            report_complete_period=b <= datetime.now(timezone.utc))
    except CountMismatch:
        return result('PARTIAL', 'ROWS_DO_NOT_MATCH_TOTAL_COUNT_NO_RETRY', records=tuple(records))
    except Exception as error:
        if (isinstance(error, ValueError) and str(error) == 'read_budget_exhausted'
                and validated_pages and not isinstance(error, ProviderResponseError)):
            return result('PARTIAL', 'READ_BUDGET_EXHAUSTED_AFTER_VALIDATED_PAGE_NO_RETRY',
                          records=tuple(records))
        return result('UNKNOWN', 'CREDENTIAL_HTTP_SCHEMA_OR_BUDGET_FAILURE_NO_RETRY')
