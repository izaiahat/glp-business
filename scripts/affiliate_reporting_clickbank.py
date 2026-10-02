"""ClickBank daily Quickstats read adapter; vendor timezone/currency unproven.

Official daily summary schema supplies sale/refund/chargeback decimals, not
individual conversions or settlement. No Analytics subscription/vendor reads.
"""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from affiliate_network_access import AccessResult, ReadTransport, ProviderResponseError, _window
from marketplace_adapters import _base as base

SOURCE = 'https://api.clickbank.com/rest/1.3/quickstats/list'
DOCS = (
    'https://support.clickbank.com/en/articles/10535409-quickstats-api',
    'https://support.clickbank.com/en/articles/10535397-clickbank-api-specifications',
)
REPORTING_TIMEZONE = 'UNKNOWN_VENDOR_CALENDAR'
TIME_AXIS = 'vendor_daily_summary'


def _amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return str(number) if number.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _rows(document, a, b, seen):
    if isinstance(document, dict):
        accounts = document.get('accountData')
    else:
        accounts = document
    if not isinstance(accounts, list):
        raise ValueError('summary_accounts_missing')
    rows = []
    for account in accounts:
        if (not isinstance(account, dict) or not isinstance(account.get('nickName'), str)
                or not re.fullmatch(r'[a-z0-9]{5,10}', account['nickName'])):
            raise ValueError('account_identity_missing')
        stats = account.get('quickStats')
        if not isinstance(stats, list):
            raise ValueError('daily_summary_missing')
        for row in stats:
            if not isinstance(row, dict) or not isinstance(row.get('quickStatDate'), str):
                raise ValueError('summary_date_missing')
            date = datetime.fromisoformat(row['quickStatDate'].replace('Z','+00:00')).date()
            identity = (account['nickName'], date)
            if not a.date() <= date < b.date() or identity in seen:
                raise ValueError('summary_date_outside_window_or_repeated')
            seen.add(identity)
            rows.append({'summary_date': date.isoformat(), 'account_id': account['nickName'],
                'reported_sale': _amount(row.get('sale')),
                'reported_refund': _amount(row.get('refund')),
                'reported_chargeback': _amount(row.get('chargeback')),
                'amount_semantics': 'vendor_daily_summary_not_affiliate_commission_or_cash',
                'currency': None, 'reporting_timezone': REPORTING_TIMEZONE,
                'classification': 'UNKNOWN', 'attribution': 'UNKNOWN'})
    return rows


def collect(transport, *, start, end, deadline=None, max_requests=32):
    a, b = _window(start, end)
    t = ReadTransport(transport, deadline=deadline, max_requests=max_requests)
    records = []
    try:
        if any(x.hour or x.minute or x.second for x in (a,b)):
            raise ValueError('calendar_window_required')
        secret = base.secret_text('glpclinicscout','clickbank_api_key')
        if not secret:
            raise OSError('credential_missing')
        page, seen = 1, set()
        while True:
            response = t.request('GET', SOURCE,
                headers={'Authorization':secret,'Accept':'application/json','Page':str(page)},
                params={'startDate':a.date().isoformat(),
                        'endDate':(b-timedelta(days=1)).date().isoformat()})
            if response.status not in (200,206):
                raise ValueError('unexpected_status')
            document = response.json()
            # The bounded live read returned literal JSON null (not [] or an
            # accountData envelope). The official Quickstats contract does not
            # establish an empty population from null; never normalize to zero.
            if document is None:
                return AccessResult('clickbank','conversions_earnings','SUPPORTED',SOURCE,
                    coverage='UNKNOWN',receipts=tuple(t.receipts),
                    reason='HTTP200_NULL_BODY_NO_POPULATION_PROOF',
                    account_binding='CONFIGURED_CREDENTIAL_NOT_RESPONSE_OWNED_IDENTITY')
            rows = _rows(document, a, b, seen)
            records.extend(rows)
            if response.status == 200:
                break
            if not rows:
                raise ValueError('empty_nonterminal_page')
            page += 1
        return AccessResult('clickbank','conversions_earnings','SUPPORTED',SOURCE,
            coverage='PARTIAL',records=tuple(records),receipts=tuple(t.receipts),
            reason='SUMMARY_ONLY_TIMEZONE_CURRENCY_TEST_REAL_AND_UTC_PERIOD_UNKNOWN',
            account_binding='CONFIGURED_CREDENTIAL_NOT_RESPONSE_OWNED_IDENTITY')
    except Exception as error:
        failed_http = any(r['http_status'] not in (200,206) for r in t.receipts)
        failed_schema = isinstance(error,ValueError) and str(error) in {
            'summary_accounts_missing','account_identity_missing','daily_summary_missing',
            'summary_date_missing','unexpected_status'}
        unknown = failed_http or not t.receipts or isinstance(error,ProviderResponseError) or failed_schema
        return AccessResult('clickbank','conversions_earnings','SUPPORTED',SOURCE,
            coverage='UNKNOWN' if unknown else 'PARTIAL',
            receipts=tuple(t.receipts), reason='HTTP200_SCHEMA_UNRECOGNIZED_NO_RETRY' if failed_schema
                else 'CREDENTIAL_HTTP_SCHEMA_OR_PERIOD_UNPROVEN_NO_RETRY')
