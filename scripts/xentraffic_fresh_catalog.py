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


def project_catalog(snapshot):
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
    totals = set()
    next_links = set()
    for pager in snapshot.get('pagers') or []:
        # Only a visible range/total, never the count of table rows or nav links.
        matches = re.findall(r'\b\d+\s*(?:-|–|to)\s*\d+\s+of\s+(\d+)\b', str(pager.get('text', '')), re.I)
        totals.update(int(total) for total in matches)
        for link in pager.get('links') or []:
            if (link.get('disabled') is not True and
                    (link.get('rel') == 'next' or str(link.get('text', '')).strip().lower() == 'next')):
                next_links.add(str(link.get('href', '')))
    if len(totals) != 1 or len(next_links) > 1:
        raise SessionError('xentraffic_visible_published_total_or_pager_unknown')
    return {'offers': offers, 'paging': {'total_count': totals.pop()}}, next(iter(next_links), None)


class FreshVisibleCatalogReader:
    fresh = True
    read_method = 'fresh_ai_browser_vault_visible_table'

    def __init__(self, driver):
        if (driver is None or getattr(driver, 'fresh', False) is not True
                or not getattr(driver, 'owned_session', '')):
            raise SessionError('fresh_ai_browser_owned_runtime_required')
        self.driver = driver
        self.owned_session = driver.owned_session
        self.number = 0
        self.next_url = None
        self.names = {}
        self.started = False

    def start(self):
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
            if number > 1:
                if self.next_url is None:
                    raise SessionError('xentraffic_visible_pagination_not_integrated')
                parts, origin = urlsplit(self.next_url), urlsplit(PORTAL_URL)
                if (parts.scheme != 'https' or parts.netloc != origin.netloc
                        or parts.path != '/offers' or parts.fragment or parts.username or parts.password):
                    raise SessionError('xentraffic_visible_pager_outside_catalog')
                self.driver.goto(self.next_url)
            payload, self.next_url = project_catalog(self.driver.visible_tables())
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
