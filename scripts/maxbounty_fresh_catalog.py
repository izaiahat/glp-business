#!/usr/bin/env python3
"""Visible MaxBounty search/campaign reads through the owned AI browser.

Uses the maintained observed /search query grammar and scoped card projection,
not auth APIs, hidden state, guessed endpoints or Playwright. No naturally
observed approved-scope API response is manufactured: that acceptance stays
unproved and the existing collector returns INCOMPLETE where it requires one.
"""
from __future__ import annotations
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit


def require_driver(driver):
    if (driver is None or getattr(driver, 'fresh', False) is not True
            or not getattr(driver, 'owned_session', '')):
        raise ValueError('fresh_ai_browser_owned_runtime_required')


class FreshVisibleListingTransport:
    def __init__(self, driver, *, evidence_dir):
        require_driver(driver)
        self.driver, self.directory = driver, Path(evidence_dir)
        self.scope, self.number = None, 0

    def __call__(self, scope, cursor):
        from scripts.marketplace_adapters import maxbounty_discovery as discovery
        if (not isinstance(scope, dict) or scope.get('kind') not in ('search', 'category', 'approved')
                or not isinstance(scope.get('value'), str)):
            raise ValueError('maxbounty_visible_scope_invalid')
        number = int(cursor or '1')
        if not 1 <= number <= 50 or (cursor is not None and (self.scope != scope or number != self.number + 1)):
            raise ValueError('maxbounty_visible_cursor_out_of_sequence')
        query = {'st': scope['value'] if scope['kind'] == 'search' else '',
                 'm': scope['value'] if scope['kind'] == 'category' else '0',
                 'c': '0', 'af': 'true' if scope['kind'] == 'approved' else 'false',
                 'pn': str(number), 'ob': 'date', 'ad': 'undefined',
                 # Live 2026-10-09: without the native form's as=true the SPA
                 # renders an empty root on direct navigation (no rows, no pager).
                 'as': 'true',
                 **{key: 'true' for key in ('ep','cp','bp','sp','ip','lp','np','mp','bbp')},
                 **{key: 'false' for key in ('ff','rf','ar')},
                 'tstrict': '0', 'grstrict': '0',
                 **{key: '' for key in ('gr','ct','prp','cpn','dl')}}
        target = 'https://affiliates.maxbounty.com/search?' + urlencode(query)
        if not discovery.safe_search_url(target):
            raise ValueError('maxbounty_visible_search_url_invalid')
        self.driver.goto(target)
        raw = self.driver.maxbounty_search_snapshot()
        buttons = raw.get('buttons') or []
        # Persist only the fixed visible projection, never arbitrary link fields.
        rows = [{'name': link['name'], 'href': link['href'], 'program_id': link['program_id'],
                 'listing_text': row['text'], 'cells': row['cells']}
                for row in raw.get('rows') or [] for link in row.get('links') or []]
        snapshot = {'url': self.driver.url, 'page_number': number,
                    'category_label': raw.get('category_label', ''),
                    'observed_at_utc': datetime.now(timezone.utc).isoformat(),
                    'visible_page_numbers': [b['number'] for b in buttons],
                    'active_page_numbers': [b['number'] for b in buttons if b.get('active') is True],
                    'empty_marker_present': raw.get('empty_marker_present') is True, 'rows': rows}
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / ('visible-page-' + uuid.uuid4().hex + '.json')
        projected = discovery.project_ui_snapshot(snapshot, scope, source=str(target))
        with target.open('x', encoding='utf-8') as handle:
            json.dump({'scope': scope, 'snapshot': snapshot, 'query_response': None,
                       'read_method': 'fresh_ai_browser_vault_visible_search'}, handle, sort_keys=True, indent=2)
        target.chmod(0o400)
        self.scope, self.number = dict(scope), number
        return projected


def capture_visible_campaign(driver, summary):
    from scripts.marketplace_adapters import maxbounty as mb
    require_driver(driver)
    pid = mb.program_id_from_url(summary['detail_url'])
    if not pid:
        raise mb.MaxBountyError('maxbounty_visible_campaign_id_required')
    target = 'https://affiliates.maxbounty.com/campaign/' + pid
    driver.goto(target)
    projection = driver.maxbounty_campaign_sections()
    wanted = ' '.join(str(summary.get('name', '')).split()).casefold()
    headings = [' '.join(str(h).split()).casefold() for h in projection.get('title_headings') or []]
    if driver.url != target or not wanted or headings.count(wanted) != 1:
        raise mb.MaxBountyError('maxbounty_visible_campaign_name_unverified')
    buttons = driver.buttons()
    controls = [b for b in buttons if b in (mb.BUTTON_APPROVED, mb.BUTTON_APPLY)]
    if len(controls) != 1:
        raise mb.MaxBountyError('maxbounty_visible_relationship_unknown_or_ambiguous')
    relationship = mb.REL_APPROVED if controls[0] == mb.BUTTON_APPROVED else mb.REL_APPLY_NEEDED
    sections = projection.get('sections') or {}
    text = lambda label: str((sections.get(label) or {}).get('text') or '')
    terms = '\n\n'.join(dict.fromkeys(filter(None, (text(label) for label in (
        'Campaign Description','Terms','Campaign Terms','Search Restrictions',
        'Email Restrictions','Display Restrictions','Social Restrictions')))))
    links = (sections.get('Landing Pages') or {}).get('links') or []
    external = sorted(set(link for link in links if urlsplit(link).scheme == 'https'
                          and urlsplit(link).hostname not in ('maxbounty.com','affiliates.maxbounty.com','api.maxbounty.com')))
    fit = mb.classify_vertical(str(summary['name']) + '\n' + terms)
    if not terms and fit['fit_classification'] == 'out_of_scope':
        fit = {'fit_classification': 'unknown', 'vertical': 'unknown', 'vertical_name': 'unknown',
               'evidence': 'scoped campaign terms not exposed', 'fit_reason': 'Missing terms are not irrelevance.'}
    return {'program_id': pid, 'name': summary['name'], 'detail_url': target,
            'official_url': external[0] if external else '', 'landing_pages': external,
            'relationship': relationship, 'relationship_button': controls[0],
            'relationship_status_evidence': controls[0], 'button_evidence': controls,
            'terms': terms, 'coupon_section': text('Coupon'), 'coupons': mb.parse_coupons(text('Coupon')),
            'attribution_days': mb.parse_attribution_days(terms),
            'read_method': 'fresh_ai_browser_vault_visible_campaign',
            **mb.payout_details(text('Rates'), summary['name']), **fit,
            'effects': {'provider_mutations': 0, 'tracking_links_minted': 0,
                        'applications_submitted': 0, 'tracking_clicks': 0}}
