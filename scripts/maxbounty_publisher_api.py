#!/usr/bin/env python3
"""MaxBounty official Affiliate REST API (v1) read transport.

Source: MaxBounty portal Resources -> Developer Tools -> "Affiliate API (v1)",
https://mb1-cdn.com/resources/docs_affiliate_api_v1.html (base
https://api.maxbounty.com/affiliates/api). Authentication is POST
/authentication with the account email/password; it returns an
``mb-api-token`` that expires every two hours. MaxBounty offers no long-lived
publisher token to generate, so nothing is minted or stored: each run
authenticates on demand from the operator's existing private credential file
(``MAXBOUNTY_API_CREDENTIALS_FILE``) and keeps the short-lived token in memory.

Read-only by construction: only GET /affiliate, GET /search, GET
/campaign/{id} and the authentication POST are reachable. The tracking-link,
postback, bookmark and global-postback writes in the same API are not
implemented. No tracking URL is followed. Token/password values never enter a
receipt, exception or log line.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = 'https://api.maxbounty.com/affiliates/api'
DOCS_URL = 'https://mb1-cdn.com/resources/docs_affiliate_api_v1.html'
PORTAL = 'https://affiliates.maxbounty.com'
ENV_NAME = 'MAXBOUNTY_API_CREDENTIALS_FILE'
EXPECTED_ACCOUNT = 'ops@glpclinicscout.com'
SOURCE_CLASS = 'official_publisher_api_response'
PAGE_SIZE = 50
READ_METHOD = 'maxbounty_official_affiliate_api_v1'


class MaxBountyAPIError(RuntimeError):
    """Fixed, secret-free reason codes only."""


def _hermes_env_value(name: str) -> str | None:
    path = Path.home() / '.hermes' / '.env'
    if not path.is_file():
        return None
    for raw in path.read_text(encoding='utf-8', errors='replace').splitlines():
        if '=' in raw and not raw.lstrip().startswith('#'):
            key, value = raw.split('=', 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    return None


def credentials_path() -> Path | None:
    """Process env wins (an empty value disables the API); else ~/.hermes/.env."""
    value = os.environ[ENV_NAME] if ENV_NAME in os.environ else _hermes_env_value(ENV_NAME)
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_file() else None


def search_query(scope: dict[str, Any], number: int) -> dict[str, str]:
    """The portal's own /search query grammar, reused verbatim by the API."""
    return {'st': scope['value'] if scope['kind'] == 'search' else '',
            'm': scope['value'] if scope['kind'] == 'category' else '0',
            'c': '0', 'af': 'true' if scope['kind'] == 'approved' else 'false',
            'pn': str(number), 'ob': 'date', 'ad': 'undefined', 'as': 'true',
            **{key: 'true' for key in ('ep', 'cp', 'bp', 'sp', 'ip', 'lp', 'np', 'mp', 'bbp')},
            **{key: 'false' for key in ('ff', 'rf', 'ar')},
            'tstrict': '0', 'grstrict': '0',
            **{key: '' for key in ('gr', 'ct', 'prp', 'cpn', 'dl')}}


class MaxBountyPublisherAPI:
    """Short-lived token session. ``opener`` is injectable for offline tests."""

    fresh = True
    read_method = READ_METHOD

    def __init__(self, credentials_file: Path, *, opener: Callable[..., Any] = urlopen,
                 timeout: int = 30):
        self._credentials_file = Path(credentials_file)
        self._opener, self._timeout = opener, timeout
        self._token: str | None = None
        self.owned_session = 'maxbounty-official-api'
        self.account: dict[str, Any] | None = None

    @classmethod
    def from_environment(cls, **kwargs) -> 'MaxBountyPublisherAPI | None':
        path = credentials_path()
        return cls(path, **kwargs) if path is not None else None

    def _request(self, path: str, *, body: dict | None = None,
                 allow_404: bool = False) -> tuple[int, dict | None, bytes]:
        headers = {'Accept': 'application/json', 'User-Agent': 'glp-maxbounty-lane/1'}
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body).encode()
        if self._token:
            headers['x-access-token'] = self._token
        request = Request(API_BASE + path, data=data, headers=headers,
                          method='POST' if body is not None else 'GET')
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read()
                status = int(response.status)
        except HTTPError as exc:
            status = int(exc.code)
            raw = b''
            if not (allow_404 and status == 404):
                raise MaxBountyAPIError(f'maxbounty_api_http_{status}') from None
            try:
                raw = exc.read() or b''
            except Exception:
                raw = b''
            return status, None, raw
        except Exception:
            raise MaxBountyAPIError('maxbounty_api_transport_failed') from None
        if len(raw) > 5_000_000:
            raise MaxBountyAPIError('maxbounty_api_response_oversized')
        try:
            payload = json.loads(raw)
        except ValueError:
            raise MaxBountyAPIError('maxbounty_api_response_not_json') from None
        if not isinstance(payload, dict):
            raise MaxBountyAPIError('maxbounty_api_response_not_object')
        return status, payload, raw

    def authenticate(self) -> dict[str, Any]:
        try:
            secret = json.loads(self._credentials_file.read_text(encoding='utf-8'))
            email, password = secret['email'], secret['password']
        except Exception:
            raise MaxBountyAPIError('maxbounty_api_credentials_unreadable') from None
        if email != EXPECTED_ACCOUNT or not isinstance(password, str) or not password:
            raise MaxBountyAPIError('maxbounty_api_credentials_principal_mismatch')
        self._token = None
        _, payload, _ = self._request('/authentication', body={'email': email, 'password': password})
        del password, secret
        token = payload.get('mb-api-token') if payload.get('success') is True else None
        if not isinstance(token, str) or not token:
            raise MaxBountyAPIError('maxbounty_api_authentication_refused')
        self._token = token
        _, account, _ = self._request('/affiliate')
        info = account.get('affiliate_personal_information') or {}
        if account.get('success') is not True or info.get('email') != EXPECTED_ACCOUNT:
            raise MaxBountyAPIError('maxbounty_api_principal_unverified')
        self.account = {'account': EXPECTED_ACCOUNT, 'publisher_id_present': account.get('publisher_id') is not None,
                        'status': info.get('status'), 'verified_via': 'GET /affiliate'}
        return dict(self.account)

    def _require(self):
        if not self._token or not self.account:
            raise MaxBountyAPIError('maxbounty_api_not_authenticated')

    def search(self, scope: dict[str, Any], number: int) -> tuple[dict, str]:
        self._require()
        _, payload, raw = self._request('/search?' + urlencode(search_query(scope, number)))
        return payload, hashlib.sha256(raw).hexdigest()

    def campaign(self, program_id: str) -> tuple[int, dict | None, str]:
        self._require()
        if not re.fullmatch(r'[1-9][0-9]*', str(program_id)):
            raise MaxBountyAPIError('maxbounty_api_campaign_id_invalid')
        status, payload, raw = self._request('/campaign/' + str(program_id), allow_404=True)
        return status, payload, hashlib.sha256(raw).hexdigest()

    def close(self):
        self._token = None


class APIListingTransport:
    """collect_scopes fetch: official /search pages, persisted allowlisted fields."""

    def __init__(self, api: MaxBountyPublisherAPI, *, evidence_dir):
        self.api, self.directory = api, Path(evidence_dir)
        self.scope, self.number = None, 0

    def __call__(self, scope, cursor):
        from scripts.marketplace_adapters import maxbounty_discovery as discovery
        if (not isinstance(scope, dict) or scope.get('kind') not in ('search', 'category', 'approved')
                or not isinstance(scope.get('value'), str)):
            raise ValueError('maxbounty_api_scope_invalid')
        number = int(cursor or '1')
        if not 1 <= number <= 50 or (cursor is not None and (self.scope != scope or number != self.number + 1)):
            raise ValueError('maxbounty_api_cursor_out_of_sequence')
        url = PORTAL + '/search?' + urlencode(search_query(scope, number))
        if not discovery.safe_search_url(url):
            raise ValueError('maxbounty_api_search_url_invalid')
        payload, raw_sha = self.api.search(scope, number)
        matches = payload.get('matchingCampaigns')
        if payload.get('success') is not True or not isinstance(matches, list):
            raise ValueError('maxbounty_api_search_failed')
        pages = payload.get('total_pages')
        if not matches and pages in (None, 0, '0'):
            pages = 1
        if type(pages) is not int or not 1 <= pages <= 50 or number > pages:
            raise ValueError('maxbounty_api_total_pages_unproven')
        if len(matches) > PAGE_SIZE or (number < pages and len(matches) != PAGE_SIZE):
            raise ValueError('maxbounty_api_page_row_count_invalid')
        rows, ids = [], []
        for row in matches:
            pid = str(row.get('campaign_id', '')) if isinstance(row, dict) else ''
            name = ' '.join(str(row.get('name') or '').split()) if isinstance(row, dict) else ''
            if not re.fullmatch(r'[1-9][0-9]*', pid) or not name or pid in ids:
                raise ValueError('maxbounty_api_campaign_identity_invalid')
            ids.append(pid)
            rows.append({'program_id': pid, 'name': name, 'detail_url': PORTAL + '/campaign/' + pid,
                         'listing_text': ' '.join(filter(None, (name, str(row.get('rate_text') or ''))))})
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / ('api-page-' + uuid.uuid4().hex + '.json')
        observed = datetime.now(timezone.utc).isoformat()
        allowed = ('campaign_id', 'name', 'status', 'rate', 'rate_type', 'rate_text', 'total_rows')
        with target.open('x', encoding='utf-8') as handle:
            json.dump({'scope': scope, 'page_number': number, 'observed_at_utc': observed,
                       'endpoint': API_BASE + '/search', 'query_grammar_url': url,
                       'response': {'success': True, 'total_pages': pages,
                                    'matchingCampaigns': [{k: r[k] for k in allowed if k in r} for r in matches]},
                       'raw_response_sha256': raw_sha, 'read_method': READ_METHOD,
                       'docs': DOCS_URL}, handle, sort_keys=True, indent=2)
        target.chmod(0o400)
        self.scope, self.number = dict(scope), number
        total = None
        return {'scope_key': scope['key'], 'page_key': str(number), 'rows': rows,
                'total': total, 'terminal': number == pages,
                'next_cursor': str(number + 1) if number < pages else None,
                'evidence': {'source': str(target), 'observed_at_utc': observed, 'url': url,
                             'endpoint': API_BASE + '/search', 'category_label': '',
                             'total_text': None, 'published_badge_total': None,
                             'query_response': {'program_ids': ids, 'reported_total': total,
                                                'total_pages': pages, 'canonical_payload_sha256': raw_sha,
                                                'source_class': SOURCE_CLASS},
                             'badge_query_disagreement': False,
                             'terminal_basis': 'official_api_total_pages',
                             'pagination': {'current': number, 'last': pages, 'page_size': PAGE_SIZE,
                                            'basis': 'official_api_total_pages'}}}


_RELATIONSHIP = (
    (re.compile(r'^approved$', re.I), 'approved'),
    (re.compile(r'^request approval to run$', re.I), 'apply_needed'),
    (re.compile(r'pending|under review|requested', re.I), 'pending'),
    (re.compile(r'denied|declined|rejected', re.I), 'denied'),
    (re.compile(r'\bcap(acity)?\b|full', re.I), 'capacity_blocked'),
)


def capture_api_campaign(api: MaxBountyPublisherAPI, summary: dict[str, Any]) -> dict[str, Any]:
    from scripts.marketplace_adapters import maxbounty as mb
    pid = mb.program_id_from_url(summary['detail_url'])
    if not pid or pid != str(summary.get('program_id')):
        raise mb.MaxBountyError('maxbounty_api_campaign_id_required')
    status, payload, _ = api.campaign(pid)
    if status != 200 or not payload or payload.get('success') is not True or str(payload.get('campaign_id')) != pid:
        raise mb.MaxBountyError('maxbounty_api_campaign_unavailable')
    details = payload.get('details') or {}
    name = ' '.join(str(details.get('name') or '').split())
    if not name or name.casefold() != ' '.join(str(summary.get('name', '')).split()).casefold():
        raise mb.MaxBountyError('maxbounty_api_campaign_name_unverified')
    raw_status = ' '.join(str(details.get('affiliate_campaign_status') or '').split())
    relationship = next((rel for pattern, rel in _RELATIONSHIP if pattern.search(raw_status)), None)
    if relationship is None:
        raise mb.MaxBountyError('maxbounty_api_relationship_unknown')
    button = {'approved': mb.BUTTON_APPROVED, 'apply_needed': mb.BUTTON_APPLY}.get(relationship, raw_status)
    email = payload.get('email_restrictions') or {}
    terms = '\n\n'.join(dict.fromkeys(filter(None, (
        str(details.get('description') or '').strip(), str(details.get('rules') or '').strip(),
        ('Search Restrictions: ' + str(payload.get('search_restrictions')).strip()) if payload.get('search_restrictions') else '',
        ('Email Restrictions: ' + str(email.get('rules')).strip()) if isinstance(email, dict) and email.get('rules') else ''))))
    commission = [c for c in payload.get('commission') or [] if isinstance(c, dict)]
    default = [c for c in commission if c.get('default_rate') == 'Y'] or commission[:1]
    rate_text = ' '.join(str(default[0].get(k) or '') for k in ('rate', 'description')).strip() if default else ''
    landing = sorted({str(lp.get('full')) for lp in payload.get('landing_pages') or []
                      if isinstance(lp, dict) and str(lp.get('full') or '').startswith('https://')})
    coupon_text = str(details.get('coupon') or '')
    fit = mb.classify_vertical(name + '\n' + terms)
    return {'program_id': pid, 'name': summary['name'], 'detail_url': PORTAL + '/campaign/' + pid,
            'official_url': landing[0] if landing else '', 'landing_pages': landing,
            'relationship': relationship, 'relationship_button': button,
            'relationship_status_evidence': 'api:affiliate_campaign_status=' + raw_status,
            'button_evidence': [button], 'campaign_status': str(details.get('status') or ''),
            'terms': terms, 'coupon_section': coupon_text, 'coupons': mb.parse_coupons(coupon_text),
            'attribution_days': mb.parse_attribution_days(terms),
            'read_method': READ_METHOD + ':campaign',
            **mb.payout_details(rate_text, name), **fit,
            'effects': {'provider_mutations': 0, 'tracking_links_minted': 0,
                        'applications_submitted': 0, 'tracking_clicks': 0}}


def api_direct_recheck(api: MaxBountyPublisherAPI, account_context: dict[str, Any]):
    """Exact current-run GET /campaign/{id}; a 404 is the account API's own answer."""
    from scripts.marketplace_adapters import maxbounty_discovery as discovery
    receipts = Path(account_context['receipts_dir'])

    def recheck(pid: str) -> dict[str, Any]:
        status, payload, body_sha = api.campaign(pid)
        observed = discovery.now()
        if status == 404:
            receipts.mkdir(parents=True, exist_ok=True)
            data = {'schema': 'glp.maxbounty.retired404.v1', 'program_id': pid, 'disposition': 'RETIRED_404',
                    'observed_at_utc': observed, 'account_context': account_context,
                    'detail_url': PORTAL + '/campaign/' + pid,
                    'response': {'url': API_BASE + '/campaign/' + pid, 'method': 'GET', 'status': 404,
                                 'body_sha256': body_sha},
                    'classification': 'terminal_provider_unavailable', 'source_class': SOURCE_CLASS}
            target = receipts / (pid + '.json')
            raw = json.dumps(data, sort_keys=True, indent=2).encode()
            with target.open('xb') as handle:
                handle.write(raw)
            target.chmod(0o400)
            return {**data, 'evidence': {'path': str(target), 'sha256': hashlib.sha256(raw).hexdigest()}}
        details = (payload or {}).get('details') or {}
        raw_status = str(details.get('affiliate_campaign_status') or '')
        disposition = 'available' if (status == 200 and raw_status.casefold() == 'approved') else (
            'unavailable' if status == 200 else 'unknown')
        return {'program_id': pid, 'disposition': disposition, 'observed_at_utc': observed,
                'evidence': {'endpoint': API_BASE + '/campaign/' + pid, 'status': status,
                             'campaign_status': str(details.get('status') or ''),
                             'affiliate_campaign_status': raw_status, 'body_sha256': body_sha,
                             'source_class': SOURCE_CLASS}}
    return recheck
