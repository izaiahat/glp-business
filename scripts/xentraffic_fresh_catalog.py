#!/usr/bin/env python3
"""XenTraffic visible catalogue projection in one caller-owned AI/vault browser.

Not a supported Everflow API adapter or a BFF/CSRF client. The account has no
publisher API key in retained evidence. Do not infer that from a login failure.
Only visible offer tables, visible published totals and observed first-party
pagination links are consumed. Button-only/unidentified UI remains incomplete.
No credentials, request headers, cookies, input values, app state or tracking
URLs are visited. The normal enumeration entry consumes this reader directly.
"""
from __future__ import annotations
import re
from urllib.parse import urlsplit

from scripts.marketplace_adapters.xentraffic import PORTAL_URL, SessionError


def _column(value):
    return re.sub(r'[^a-z0-9]', '', str(value).lower())


def project_catalog(snapshot, *, expected_start=1):
    """Project a single unambiguous visible table. Unknown is never an API total."""
    candidates = []
    for table in snapshot.get('tables') or []:
        headers = [_column(h) for h in table.get('headers') or []]
        if len(headers) != len(set(headers)):
            continue
        aliases = {'id': ('id', 'offerid'), 'name': ('name', 'offername', 'offer'),
                   'status': ('status', 'offerstatus'),
                   'affiliate_status': ('affiliatestatus', 'approvalstatus'),
                   'category': ('category',), 'payout': ('payout',), 'visibility': ('visibility',)}
        indexes = {key: headers.index(next(h for h in names if h in headers))
                   for key, names in aliases.items() if any(h in headers for h in names)}
        if all(key in indexes for key in ('id', 'name', 'status')):
            candidates.append((table, headers, indexes))
    if len(candidates) != 1:
        raise SessionError('xentraffic_visible_offer_table_unknown_or_ambiguous')
    table, headers, indexes = candidates[0]
    offers = []
    for row in table.get('rows') or []:
        cells = row.get('cells') or []
        if len(cells) != len(headers):
            raise SessionError('xentraffic_visible_offer_row_shape_unknown')
        values = {key: str(cells[index]).strip() for key, index in indexes.items()}
        if not re.fullmatch(r'[1-9][0-9]*', values['id']) or not values['name']:
            raise SessionError('xentraffic_visible_offer_identity_unknown')
        offers.append({'network_offer_id': int(values['id']), 'name': values['name'],
                       'offer_status': values['status'].lower(),
                       'category': values.get('category', ''), 'payout': values.get('payout', ''),
                       'visibility': values.get('visibility', ''),
                       'relationship': {'offer_affiliate_status': values.get('affiliate_status', '').lower()}})
    ranges = set()
    next_links = set()
    # Full-line grammar prevents 1,000 / 1.5 / malformed suffixes becoming 1.
    integer = r'(?:[1-9][0-9]{0,2}(?:,[0-9]{3})+|0|[1-9][0-9]*)'
    pattern = rf'(?:Showing\s+)?({integer})\s*(?:-|–|to)\s*({integer})\s+of\s+({integer})(?:\s+(?:entries|offers|results))?'
    for pager in snapshot.get('pagers') or []:
        # Pager link labels may occupy other lines; never accept a numeric prefix.
        for line in str(pager.get('text', '')).splitlines():
            if not re.search(r'\bof\b', line, re.I):
                continue
            match = re.fullmatch(pattern, line.strip(), re.I)
            if match is None:
                raise SessionError('xentraffic_visible_published_total_or_pager_unknown')
            start, end, total = (int(value.replace(',', '')) for value in match.groups())
            if not ((total == 0 and start == end == 0 and not offers and expected_start == 1)
                    or (total > 0 and start == expected_start and start <= end <= total
                        and end - start + 1 == len(offers))):
                raise SessionError('xentraffic_visible_published_total_or_pager_unknown')
            ranges.add((start, end, total))
        for link in pager.get('links') or []:
            if (link.get('disabled') is not True and
                    (link.get('rel') == 'next' or str(link.get('text', '')).strip().lower() == 'next')):
                next_links.add(str(link.get('href', '')))
    if len(ranges) != 1 or len(next_links) > 1:
        raise SessionError('xentraffic_visible_published_total_or_pager_unknown')
    _, end, total = ranges.pop()
    if end == total and next_links:
        raise SessionError('xentraffic_visible_published_total_or_pager_unknown')
    return {'offers': offers, 'paging': {'total_count': total}}, next(iter(next_links), None)


_MONEY = re.compile(r'^\$([0-9][0-9,]*(?:\.[0-9]+)?)$')
_PERCENT = re.compile(r'^([0-9]+(?:\.[0-9]+)?)%$')
LIVE_PAGE_SIZE = 100


def _live_payout(cell):
    """'CPA\n$100.000' / 'CPS\n70.00%' / '-' in the BFF display grammar."""
    lines = [line.strip() for line in str(cell).splitlines() if line.strip()]
    if lines in ([], ['-']):
        return {'payout_type': 'N/A', 'payout_amount': None, 'payout': 'N/A'}
    if lines == ['View Payouts']:
        # Multi-tier payout: the table links to the tiers instead of a value.
        return {'payout_type': 'MULTIPLE', 'payout_amount': None, 'payout': ''}
    if len(lines) != 2 or not re.fullmatch(r'[A-Z]{2,5}', lines[0]):
        raise SessionError('xentraffic_visible_payout_unknown')
    kind, value = lines
    money, percent = _MONEY.fullmatch(value), _PERCENT.fullmatch(value)
    if money:
        return {'payout_type': kind, 'payout_amount': float(money.group(1).replace(',', '')),
                'payout': f'{kind}: {value}'}
    if percent:
        return {'payout_type': kind, 'payout_amount': None, 'payout_percentage': float(percent.group(1)),
                'payout': f'{kind} {value}'}
    raise SessionError('xentraffic_visible_payout_unknown')


def project_live_catalog(snapshot, *, expected_start, expected_total=None):
    """Project the live Everflow /offers table and its single "N Total" label.

    Columns observed 2026-10-09: ID, Thumbnail, Name, Visibility, Category,
    Channels, Payout, Today's Revenue, Rules. The table exposes no offer status
    column, so status stays unknown (''), never inferred.
    """
    tables = snapshot.get('tables') or []
    candidates = []
    for table in tables:
        headers = [_column(h) for h in table.get('headers') or []]
        named = [h for h in headers if h]
        if len(named) != len(set(named)):
            continue
        if all(h in headers for h in ('id', 'name', 'visibility', 'category', 'payout')):
            candidates.append((table, headers))
    if len(candidates) != 1:
        raise SessionError('xentraffic_visible_offer_table_unknown_or_ambiguous')
    table, headers = candidates[0]
    at = {key: headers.index(key) for key in ('id', 'name', 'visibility', 'category', 'payout')}
    totals = set(snapshot.get('totals') or [])
    if len(totals) != 1:
        raise SessionError('xentraffic_visible_published_total_unknown')
    match = re.fullmatch(r'([1-9][0-9]{0,2}(?:,[0-9]{3})+|0|[1-9][0-9]*)\s+Total', next(iter(totals)))
    if not match:
        raise SessionError('xentraffic_visible_published_total_unknown')
    total = int(match.group(1).replace(',', ''))
    if expected_total is not None and total != expected_total:
        raise SessionError('xentraffic_catalog_total_changed_mid_read')
    offers = []
    for row in table.get('rows') or []:
        cells = row.get('cells') or []
        if len(cells) != len(headers):
            raise SessionError('xentraffic_visible_offer_row_shape_unknown')
        pid, name = str(cells[at['id']]).strip(), ' '.join(str(cells[at['name']]).split())
        if not re.fullmatch(r'[1-9][0-9]*', pid) or not name:
            raise SessionError('xentraffic_visible_offer_identity_unknown')
        vis = [line.strip() for line in str(cells[at['visibility']]).splitlines() if line.strip()]
        if not 1 <= len(vis) <= 2:
            raise SessionError('xentraffic_visible_visibility_unknown')
        offers.append({'network_offer_id': int(pid), 'name': name, 'offer_status': '',
                       'visibility': re.sub(r'\s+', '_', vis[0].lower()),
                       'category': str(cells[at['category']]).strip(),
                       'relationship': {'offer_affiliate_status': vis[1].lower() if len(vis) == 2 else ''},
                       **_live_payout(cells[at['payout']])})
    remaining = total - (expected_start - 1)
    if remaining < 0 or len(offers) != min(LIVE_PAGE_SIZE, remaining):
        raise SessionError('xentraffic_visible_page_row_count_mismatch')
    pages = snapshot.get('pages') or []
    active = {p.get('number') for p in pages if p.get('active')}
    expected_page = (expected_start - 1) // LIVE_PAGE_SIZE + 1
    if total > LIVE_PAGE_SIZE and active != {str(expected_page)}:
        raise SessionError('xentraffic_visible_pager_state_unknown')
    return {'offers': offers, 'paging': {'total_count': total}}


class FreshVisibleCatalogReader:
    fresh = True
    read_method = 'fresh_ai_browser_vault_visible_table'
    live = False

    def __init__(self, driver):
        if (driver is None or getattr(driver, 'fresh', False) is not True
                or not getattr(driver, 'owned_session', '')):
            raise SessionError('fresh_ai_browser_owned_runtime_required')
        self.driver = driver
        self.owned_session = driver.owned_session
        self.number = 0
        self.seen_count = 0
        self.next_url = None
        self.names = {}
        self.started = False

    def start(self):
        if callable(getattr(self.driver, 'xentraffic_catalog_snapshot', None)):
            return self._start_live()
        # Do not navigate a guessed account-settings origin. Require the actual
        # protected page to supply principal and tenant before reading offers.
        url = urlsplit(self.driver.url)
        expected = urlsplit(PORTAL_URL)
        if (url.scheme != 'https' or url.netloc != expected.netloc
                or url.path in ('/login', '/signin')):
            raise SessionError('xentraffic_protected_origin_unverified')
        body = self.driver.body_text()
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if (lines.count('ops@glpclinicscout.com') != 1 or
                len(re.findall(r'^(?:Partner|Affiliate) ID\s*:\s*4384\s*$', body, re.M | re.I)) != 1):
            raise SessionError('xentraffic_protected_principal_and_partner_unverified')
        self.driver.goto(PORTAL_URL + '/offers')
        self.started = True
        return self

    def _start_live(self):
        """Exact tenant principal on the portal's own My Account page."""
        self.driver.goto(PORTAL_URL + '/myaccount')
        lines = [line.strip() for line in self.driver.body_text().splitlines() if line.strip()]
        partner = [lines[i + 1] for i, line in enumerate(lines[:-1]) if line == 'Partner ID']
        if partner != ['4384'] or 'My Account' not in lines:
            raise SessionError('xentraffic_protected_principal_and_partner_unverified')
        self.driver.goto(PORTAL_URL + '/offers')
        self.live, self.started, self.total = True, True, None
        return self

    def _live_page(self, number, size):
        if size != LIVE_PAGE_SIZE:
            raise SessionError('xentraffic_visible_page_size_unsupported')
        snapshot = (self.driver.xentraffic_catalog_snapshot() if number == 1
                    else self.driver.xentraffic_offers_page(number))
        payload = project_live_catalog(snapshot, expected_start=self.seen_count + 1,
                                       expected_total=self.total)
        self.total = payload['paging']['total_count']
        return payload

    def api(self, path, method='GET', body=None):
        """Compatibility call shape only; performs visible DOM reads, not fetch."""
        if not self.started:
            raise SessionError('xentraffic_fresh_reader_not_started')
        catalog = re.fullmatch(r'/api/affiliates/offerstable\?page=([1-9][0-9]*)&page_size=([1-9][0-9]*)&order_field=id&order_direction=desc', path)
        if catalog:
            number, size = map(int, catalog.groups())
            if (method != 'POST' or body != {'filters': {'affiliate_status': '__all'}, 'search_terms': []}
                    or number != self.number + 1 or not 1 <= size <= 100 or number > 51):
                raise SessionError('xentraffic_visible_catalog_request_out_of_sequence')
            if getattr(self, 'live', False):
                payload = self._live_page(number, size)
                self.seen_count += len(payload['offers'])
                self.names.update({row['network_offer_id']: row['name'] for row in payload['offers']})
                self.number = number
                return payload
            if number > 1:
                if self.next_url is None:
                    raise SessionError('xentraffic_visible_pagination_not_integrated')
                parts, origin = urlsplit(self.next_url), urlsplit(PORTAL_URL)
                if (parts.scheme != 'https' or parts.netloc != origin.netloc
                        or parts.path != '/offers' or parts.fragment or parts.username or parts.password):
                    raise SessionError('xentraffic_visible_pager_outside_catalog')
                self.driver.goto(self.next_url)
            payload, self.next_url = project_catalog(self.driver.visible_tables(), expected_start=self.seen_count + 1)
            self.seen_count += len(payload['offers'])
            self.names.update({row['network_offer_id']: row['name'] for row in payload['offers']})
            self.number = number
            return payload
        detail = re.fullmatch(r'/api/affiliates/offers/([1-9][0-9]*)\?relationship=all', path)
        if detail and method == 'GET' and body is None:
            pid = int(detail.group(1))
            if pid not in self.names:
                raise SessionError('xentraffic_detail_outside_observed_catalog')
            self.driver.goto(f'{PORTAL_URL}/offers/{pid}')
            text = self.driver.body_text()
            if (self.names[pid] not in text or not re.search(r'Offer ID\s*:?\s*'+str(pid)+r'\b', text, re.I)):
                raise SessionError('xentraffic_visible_detail_identity_unverified')
            description = re.search(r'(?:^|\n)Description\s*\n(.*?)(?=\n(?:Terms|Restrictions|Tracking|Payout)\b|\Z)', text, re.S)
            return {'network_offer_id': pid, 'name': self.names[pid],
                    'html_description': description.group(1).strip() if description else '',
                    'terms_and_conditions': '', 'tracking_url': ''}
        raise SessionError('xentraffic_fresh_reader_operation_not_supported')
