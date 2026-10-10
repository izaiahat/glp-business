"""MaxBounty official v1 read-only reporting; no tracking/postback/write routes.

Contract: https://mb1-cdn.com/resources/docs_affiliate_api_v1.html
Reuse the existing authorized short-lived API credential-file authenticator.
Authentication bodies/tokens stay in memory and outside read receipts. Source
amounts are visible, not classified revenue: currency, period timezone, TEST vs
REAL, settlement membership and per-conversion cash allocation stay UNKNOWN.
"""
from datetime import timedelta
import json
import re
import time
from urllib.parse import urlsplit, parse_qs

from affiliate_network_access import AccessResult, ReadTransport, _window
from affiliate_reporting_partnerstack import _amount, _identifier
from marketplace_adapters import _base
from maxbounty_publisher_api import MaxBountyPublisherAPI, API_BASE, credentials_path

SOURCE = API_BASE + '/reports/conversions'


class _Response:
    def __init__(self, response):
        self.status, self.body = response.status, response.body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.body


def _session(t):
    path = credentials_path()
    if path is None:
        raise FileNotFoundError('existing_api_credentials_missing')
    def opener(request, *, timeout):
        parsed = urlsplit(request.full_url)
        if parsed.scheme != 'https' or parsed.hostname != 'api.maxbounty.com' or parsed.port or parsed.fragment:
            raise ValueError('maxbounty_origin_refused')
        url = API_BASE + parsed.path.removeprefix('/affiliates/api')
        headers = dict(request.header_items())
        if request.method == 'POST' and request.full_url == API_BASE + '/authentication':
            if time.monotonic() >= t.deadline:
                raise ValueError('read_budget_exhausted')
            raw = t.transport
            if isinstance(raw, _base.RequestsTransport):
                raw._sess().max_redirects = 0
            # Fixed-origin login only; never hash/store its token-bearing body.
            auth = getattr(raw, 'auth_request', raw.request)
            response = auth('POST', url, headers=headers, json_body=json.loads(request.data),
                            timeout=max(.1, min(30, t.deadline-time.monotonic())))
            if response.status != 200:
                raise ValueError('authentication_refused_no_retry')
        elif request.method == 'GET':
            response = t.request('GET', url, headers=headers,
                                 params={k: v[0] for k, v in parse_qs(parsed.query).items()})
        else:
            raise ValueError('maxbounty_mutation_refused')
        return _Response(response)
    return MaxBountyPublisherAPI(path, opener=opener)


def _row(row, *, kind, pointer):
    if not isinstance(row, dict):
        raise ValueError('row_schema')
    # Exclude the prohibited provider; don't request a campaign or tracking link.
    if 'fella' in str(row.get('name', '')).casefold():
        return None
    stamp = row.get('date') or row.get('reverse_date')
    return {'record_kind': kind, 'receipt_pointer': pointer,
            'provider_event_id': _identifier(row.get('key_id')),
            'program_id': _identifier(row.get('campaign_id')),
            'reported_amount': _amount(row.get('earnings') if kind != 'account_balance_transaction' else row.get('amount')),
            'source_status': row.get('status') if row.get('status') in {'Payable', 'Reversal'} else 'UNKNOWN',
            'source_transaction_type': row.get('type') if row.get('type') in {'Earned', 'Paid', 'Payment', 'Adjustment'} else None,
            'source_date': stamp if isinstance(stamp, str) and re.fullmatch(r'[0-9T:.Z+ -]{10,40}', stamp) else None,
            'classification': 'UNKNOWN', 'currency': None, 'commission_minor': None, 'cash_minor': None,
            'amount_semantics': 'NETWORK_REPORTED_AMOUNT_CURRENCY_AND_REAL_CLASSIFICATION_UNPROVEN'}


def collect(transport, *, start, end, deadline=None, max_requests=32):
    t = ReadTransport(transport, deadline=deadline, max_requests=max_requests)
    records, totals, binding, api = [], None, 'UNKNOWN', None
    reason = 'CREDENTIAL_HTTP_SCHEMA_OR_PERIOD_UNPROVEN_NO_RETRY'
    try:
        a, b = _window(start, end)
        if any((x.hour, x.minute, x.second, x.microsecond) != (0, 0, 0, 0) for x in (a, b)):
            raise ValueError('calendar_boundaries_required')
        api = _session(t)
        api.authenticate()
        account = t.raw_documents[-1]
        publisher = _identifier(account.get('publisher_id'))
        if publisher is None:
            raise ValueError('publisher_identity_missing')
        binding = 'RESPONSE_OWNED_PUBLISHER_ID:' + publisher
        dates = {'startDate': a.strftime('%Y-%m-%d'), 'endDate': (b-timedelta(days=1)).strftime('%Y-%m-%d')}
        from urllib.parse import urlencode
        _, doc, _ = api._request('/reports/earnings?' + urlencode(dates))
        if doc.get('success') is not True or not isinstance(doc.get('report'), list) or not isinstance(doc.get('totals'), dict):
            raise ValueError('earnings_schema')
        totals = {k: _amount(doc['totals'].get(k)) for k in ('earnings', 'clicks', 'leads', 'sales')}
        totals.update(receipt_sha256=t.receipts[-1]['response_sha256'], receipt_pointer='/totals',
                      scope='WHOLE_ACCOUNT_UNCLASSIFIED_NOT_PROVIDER_ELIGIBLE_REVENUE', currency=None)
        for index, row in enumerate(doc['report']):
            projected = _row(row, kind='campaign_earnings', pointer='/report/'+str(index))
            if projected:
                records.append(dict(projected, receipt_sha256=t.receipts[-1]['response_sha256']))
        for kind in ('conversions', 'reversals'):
            page, expected = 1, None
            while True:
                _, doc, _ = api._request('/reports/' + kind + '?' + urlencode(dict(dates, page=page)))
                rows, pages = doc.get('report'), doc.get('total_pages')
                if (doc.get('success') is not True or not isinstance(rows, list) or len(rows) > 50
                        or type(pages) is not int or pages < 0 or type(doc.get('page')) is not int or doc['page'] != page):
                    raise ValueError('pagination_schema')
                if expected is not None and expected != pages:
                    raise ValueError('page_count_changed')
                expected = pages
                if pages == 0 and (page != 1 or rows):
                    raise ValueError('empty_page_count_mismatch')
                if page > pages and not (pages == 0 and page == 1):
                    raise ValueError('page_overrun')
                for index, row in enumerate(rows):
                    projected = _row(row, kind=kind, pointer='/report/'+str(index))
                    if projected:
                        records.append(dict(projected, receipt_sha256=t.receipts[-1]['response_sha256']))
                if pages == 0 or page == pages:
                    break
                if not rows:
                    raise ValueError('missing_nonterminal_page')
                page += 1
        for year in range(a.year, (b-timedelta(days=1)).year+1):
            _, doc, _ = api._request('/accountBalance/' + str(year))
            if doc.get('success') is not True or not isinstance(doc.get('account_balances'), list):
                raise ValueError('balance_schema_no_empty_inference')
            for index, row in enumerate(doc['account_balances']):
                projected = _row(row, kind='account_balance_transaction', pointer='/account_balances/'+str(index))
                if projected:
                    records.append(dict(projected, receipt_sha256=t.receipts[-1]['response_sha256'], source_scope='WHOLE_YEAR_NOT_REQUESTED_PERIOD'))
        reason = 'OFFICIAL_ROWS_READ_PERIOD_TIMEZONE_CURRENCY_REAL_AND_SETTLEMENT_UNKNOWN'
    except FileNotFoundError:
        reason = 'EXISTING_API_CREDENTIAL_PATH_MISSING'
    except Exception:
        pass  # fixed reason only: no raw response/credential exception strings
    finally:
        if api:
            api.close()
    return AccessResult('maxbounty', 'conversions_earnings', 'SUPPORTED', SOURCE,
        coverage='PARTIAL' if records or t.receipts else 'UNKNOWN',
        records=tuple(records), receipts=tuple(t.receipts), account_binding=binding,
        reported_totals=totals, reason=reason)
