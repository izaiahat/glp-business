#!/usr/bin/env python3
"""In-memory, staged AI-browser producer for the capacity monitor's probe(target).

Browser helpers are the real browser_harness.helpers cdp/drain_events/switch_tab
callables, not Playwright or a portal API client. The owner must retain this
object (and its lease context) across the explicit vault/MFA tool boundary.
There is deliberately no receipt-import, credential, persistent-profile,
signing, or background-daemon interface. Operational adoption is separate.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from datetime import datetime, timezone

from scripts import glp_maxbounty_capacity_monitor as monitor

ORIGIN = 'https://affiliates.maxbounty.com'
LOGIN_URL = ORIGIN + '/'
ACCOUNT = 'ops@glpclinicscout.com'
CAMPAIGN_RESPONSE = 'https://api.maxbounty.com/affiliates/api/campaign/14449'
AUTH_RESPONSE = 'https://api.maxbounty.com/affiliates/api/authenticationApp'
TARGET = {'queue_id': monitor.TARGET_QUEUE_ID, 'program_id': monitor.TARGET_ID,
          'detail_url': monitor.DETAIL_URL}

# Read rendered account evidence, never input values, localStorage or auth tokens.
# A unique email in protected rendered chrome is conservative: if this portal
# does not expose it, principal evidence remains UNKNOWN (no selector guessing).
PRINCIPAL_JS = r"""(() => {
 const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
   return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'};
 const text=document.body?.innerText||'';
 return {origin:location.origin, url:location.href,
   emails:[...new Set((text.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi)||[]).map(x=>x.toLowerCase()))],
   protected_page:/\bcampaigns\b/i.test(text)&&/\b(?:dashboard|affiliate manager|account)\b/i.test(text),
   challenge:/authentication code|verification code|two-factor|captcha/i.test(text),
   password_visible:[...document.querySelectorAll('input[type=password]')].some(visible),
   apply:[...document.querySelectorAll('button,[role=button]')].some(e=>visible(e)&&e.innerText.trim()==='Request approval to run'),
   approved:[...document.querySelectorAll('button,[role=button]')].some(e=>visible(e)&&e.innerText.trim()==='Build tracking link')};
})()"""


class FreshCapacityProbe:
    """One owned incognito context; all network remains paused between poll calls.

    Construct only inside monitor.session_leases(). begin() guards the blank
    target before opening login. The caller uses the maintained vault tools,
    then calls poll() while authentication settles and finally probe(target).
    No generic success receipt or configured account can establish identity.

    This is a candidate controller, not a claim that browser_exec preserves
    Python objects across calls. See the runbook's explicit adoption blockers.
    """
    def __init__(self, *, cdp, drain_events, switch_tab, clock=time.monotonic):
        self.cdp, self.drain_events, self.switch_tab = cdp, drain_events, switch_tab
        self.clock = clock
        self.context = self.tab = self.session = self.main_frame = None
        self.detail_documents = 0
        self.guard = monitor.RequestGuard()
        self.authenticated = False
        self.auth_attempt = None
        self.campaign_attempt = None
        self.response = None
        self.armed = self.used = False
        self.fault = None

    def _fail(self, code):
        self.fault = code
        raise monitor.Refusal(code)

    def _call(self, method, **params):
        try:
            return self.cdp(method, session_id=self.session, **params)
        except Exception:
            self._fail('FRESH_BROWSER_HELPER_FAILURE')

    def begin(self):
        if self.context or self.used:
            self._fail('FRESH_BROWSER_ALREADY_STARTED')
        # Do not auto-attach a shared browser. Named browser alone is not proof.
        targets = self._call('Target.getTargets').get('targetInfos', [])
        if not targets or any(t.get('type') != 'page' or t.get('url') != 'about:blank'
                              for t in targets):
            self._fail('FRESH_BROWSER_NOT_EXCLUSIVE_BLANK')
        if self._call('Target.getBrowserContexts').get('browserContextIds'):
            self._fail('FRESH_BROWSER_CONTEXT_ALREADY_EXISTS')
        self.context = self._call('Target.createBrowserContext', disposeOnDetach=True)['browserContextId']
        try:
            self.tab = self._call('Target.createTarget', url='about:blank',
                                 browserContextId=self.context)['targetId']
            self.session = self.switch_tab(self.tab)
            info = self._call('Target.getTargetInfo', targetId=self.tab)['targetInfo']
            if info.get('browserContextId') != self.context or info.get('url') != 'about:blank':
                self._fail('FRESH_CONTEXT_BINDING_FAILED')
            self.main_frame = self._call('Page.getFrameTree').get('frameTree', {}).get('frame', {}).get('id')
            if not self.main_frame:
                self._fail('FRESH_FRAME_BINDING_FAILED')
            # The inspected helper sends Target.* at the browser root. Scope
            # auto-attachment to this owned target and its related children,
            # not the original blank tab. Unsupported CDP capability refuses.
            # No child is resumed by this producer.
            self._call('Target.autoAttachRelated', targetId=self.tab, waitForDebuggerOnStart=True)
            self._call('Network.enable')
            self._call('Network.setCacheDisabled', cacheDisabled=True)
            self._call('Network.setBlockedURLs', urls=['ws://*', 'wss://*'])
            self._call('Fetch.enable', patterns=[{'urlPattern': '*', 'requestStage': 'Request'},
                {'urlPattern': CAMPAIGN_RESPONSE, 'requestStage': 'Response'},
                {'urlPattern': AUTH_RESPONSE, 'requestStage': 'Response'}])
            self._navigate(LOGIN_URL)
            return {'stage': 'EXPLICIT_VAULT_AUTH_REQUIRED', 'target_id': self.tab,
                    'api_availability': 'UNKNOWN'}
        except BaseException:
            self.close()
            raise

    def _navigate(self, url):
        # Schedule normal top-level navigation so Fetch pauses can be drained;
        # a synchronous Page.navigate can wait on the paused request itself.
        self._call('Runtime.evaluate', expression='setTimeout(()=>location.assign('
                   + json.dumps(url) + '),0);true', returnByValue=True)

    def _principal(self, *, detail=False):
        value = self._call('Runtime.evaluate', expression=PRINCIPAL_JS,
                           returnByValue=True).get('result', {}).get('value')
        if not isinstance(value, dict) or value.get('origin') != ORIGIN:
            self._fail('AUTHENTICATED_PRINCIPAL_UNCONFIRMED')
        if value.get('emails') != [ACCOUNT]:
            self._fail('AUTHENTICATED_PRINCIPAL_MISMATCH')
        if (value.get('protected_page') is not True or value.get('password_visible') is not False
                or value.get('challenge') is not False):
            self._fail('LOGIN_WALL_OR_CHALLENGE')
        if detail and value.get('url') != monitor.DETAIL_URL:
            self._fail('CAMPAIGN_IDENTITY_MISMATCH')
        return value

    def _allow(self, method, url, resource):
        from urllib.parse import urlsplit
        u = urlsplit(url)
        # Tighten (never widen) the legacy guard. No bulk/account API GETs,
        # arbitrary GET effects, redirects, query strings or alternate targets.
        if u.query or u.fragment or u.username or u.password:
            return False
        if url == CAMPAIGN_RESPONSE:
            return self.armed and self.guard.allow(method, url)
        if url in {AUTH_RESPONSE, 'https://api.maxbounty.com/affiliates/api/affiliate/pageView'}:
            return self.guard.allow(method, url) and method == 'POST'
        if u.netloc != 'affiliates.maxbounty.com' or u.scheme != 'https':
            return False
        if url == monitor.DETAIL_URL:
            if not self.armed or self.detail_documents or method != 'GET' or resource != 'Document':
                return False
            self.detail_documents += 1
        allowed_page = url == LOGIN_URL or (url == monitor.DETAIL_URL and self.armed)
        asset = resource in {'Script', 'Stylesheet', 'Image', 'Font'} and bool(
            re.fullmatch(r'/[A-Za-z0-9_./-]+\.(?:js|css|png|jpg|jpeg|svg|ico|woff2?|ttf)', u.path))
        return (allowed_page or asset) and self.guard.allow(method, url)

    def poll(self):
        if self.fault:
            raise monitor.Refusal(self.fault)
        if not self.context:
            self._fail('FRESH_BROWSER_NOT_STARTED')
        try:
            events = self.drain_events()
        except Exception:
            self._fail('FRESH_BROWSER_EVENT_READ_FAILED')
        # The inspected harness uses deque(maxlen=500). A full buffer might
        # have dropped guard/response evidence, so never accept it as complete.
        if not isinstance(events, list) or len(events) >= 500:
            self._fail('FRESH_BROWSER_EVENT_COVERAGE_UNKNOWN')
        for event in events:
            method, params = event.get('method'), event.get('params', {})
            if method == 'Target.attachedToTarget':
                target = params.get('targetInfo', {})
                if target.get('targetId') != self.tab:
                    self._fail('CHILD_TARGET_BOUNDARY_REFUSED')
            if method == 'Target.detachedFromTarget' and params.get('sessionId') == self.session:
                self._fail('FRESH_BROWSER_SESSION_LOST')
            if event.get('session_id') != self.session:
                continue
            if method in {'Network.webSocketCreated', 'Network.webTransportCreated'}:
                self._fail('UNHANDLED_NETWORK_CHANNEL')
            if method != 'Fetch.requestPaused':
                continue
            request = params.get('request', {})
            rid, url = params.get('requestId'), request.get('url')
            verb = request.get('method')
            if not rid:
                self._fail('REQUEST_CORRELATION_MISSING')
            if params.get('frameId') != self.main_frame:
                self._call('Fetch.failRequest', requestId=rid, errorReason='BlockedByClient')
                self._fail('CHILD_FRAME_BOUNDARY_REFUSED')
            if 'responseStatusCode' in params:
                self._response(params, rid, verb, url)
                continue
            # Redirect hops and subframe campaign calls cannot acquire a read.
            if params.get('redirectedRequestId') or not self._allow(verb, url, params.get('resourceType')):
                self.guard.blocked += 1
                self._call('Fetch.failRequest', requestId=rid, errorReason='BlockedByClient')
                if url == CAMPAIGN_RESPONSE:
                    self._fail('CAMPAIGN_READ_COUNT_INVALID')
                continue
            if url == CAMPAIGN_RESPONSE:
                if self.campaign_attempt is not None:
                    self._fail('CAMPAIGN_READ_COUNT_INVALID')
                self.campaign_attempt = rid  # before dispatch, even on timeout
            if url == AUTH_RESPONSE:
                if self.auth_attempt is not None:
                    self._fail('AUTHENTICATION_READ_COUNT_INVALID')
                self.auth_attempt = rid
            self._call('Fetch.continueRequest', requestId=rid)

    def _response(self, params, rid, verb, url):
        auth = url == AUTH_RESPONSE
        expected = self.auth_attempt if auth else self.campaign_attempt
        if (url not in {AUTH_RESPONSE, CAMPAIGN_RESPONSE} or rid != expected
                or verb != ('POST' if auth else 'GET')):
            self._fail('RESPONSE_BINDING_MISMATCH')
        status = params['responseStatusCode']
        if (auth and status != 200) or (not auth and status not in {200, 404}):
            self._fail('AUTH_OR_CAMPAIGN_RESPONSE_REFUSED')
        raw = self._call('Fetch.getResponseBody', requestId=rid)
        try:
            text = (base64.b64decode(raw['body'], validate=True).decode('utf-8')
                    if raw.get('base64Encoded') else raw['body'])
            data = json.loads(text)
        except (KeyError, TypeError, ValueError, UnicodeError):
            self._fail('RESPONSE_SCHEMA_UNCONFIRMED')
        if auth:
            if not isinstance(data, dict) or data.get('success') is not True or self.authenticated:
                self._fail('FRESH_AUTHENTICATION_UNCONFIRMED')
            self.authenticated = True
        else:
            if self.response is not None:
                self._fail('CAMPAIGN_RESPONSE_COUNT_INVALID')
            if not isinstance(data, dict):
                self._fail('RESPONSE_SCHEMA_UNCONFIRMED')
            if status == 200 and (str(data.get('campaign_id')) != monitor.TARGET_ID
                                  or not isinstance(data.get('details'), dict)):
                self._fail('CAMPAIGN_RESPONSE_IDENTITY_MISMATCH')
            # Raw decoded-text hash, NOT a projection hash. No raw body retained.
            self.response = {'status': status, 'body_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
                             'details': {k: ' '.join(str(v).split())[:500] for k, v in data.get('details', {}).items()
                                         if k in {'status', 'affiliate_campaign_status'} and isinstance(v, (str, int, float, bool))},
                             'observed_at_utc': datetime.now(timezone.utc).isoformat()}
        self._call('Fetch.continueRequest', requestId=rid)

    def probe(self, target):
        if target != TARGET:
            self._fail('PROGRAM_PROJECTION_IDENTITY_MISMATCH')
        if self.used:
            self._fail('CAMPAIGN_READ_COUNT_INVALID')
        self.used = True
        self.poll()
        if not self.authenticated:
            self._fail('FRESH_AUTHENTICATION_UNCONFIRMED')
        self._principal()
        self.armed = True
        self._navigate(monitor.DETAIL_URL)
        deadline = self.clock() + 20
        while self.response is None and self.clock() < deadline:
            self.poll()
            time.sleep(.01)
        if self.response is None:
            self._fail('CAMPAIGN_RESPONSE_NOT_OBSERVED')
        dom = self._principal(detail=True)
        self.poll()  # fail on any second request before returning evidence
        if self.guard.campaign_gets != 1:
            self._fail('CAMPAIGN_READ_COUNT_INVALID')
        relation = self.response['details'].get('affiliate_campaign_status', '')
        blocked = bool(re.search(r'maximum number of by request campaigns.*new requests are not allowed', relation, re.I))
        if self.response['status'] == 404:
            classification = 'terminal_provider_unavailable'
        elif blocked:
            classification = 'provider_capacity_blocked'
        elif re.search(r'\b(?:denied|declined|rejected)\b', relation, re.I):
            classification = 'application_denied'
        elif re.search(r'\b(?:pending|under review|submitted|received|awaiting (?:advertiser )?approval)\b', relation, re.I):
            classification = 'application_pending'
        elif dom.get('apply') is True:
            classification = 'apply_needed'
        elif dom.get('approved') is True:
            classification = 'already_approved'
        else:
            self._fail('CAMPAIGN_EVIDENCE_UNCONFIRMED')
        return {'classification': classification, 'capacity_marker_seen': blocked,
                'campaign_api_status': self.response['status'],
                'campaign_api_body_sha256': self.response['body_sha256'],
                'authentication_outcome': 'fresh_authorized_login',
                'principal_source': 'protected_rendered_dom',
                'observed_at_utc': self.response['observed_at_utc'],
                'network_boundary': {'campaign_gets': self.guard.campaign_gets,
                    'authentication_posts': self.guard.auth_posts,
                    'page_view_posts': self.guard.pageview_posts,
                    'requests_blocked_before_network': self.guard.blocked}}

    def close(self):
        if self.context:
            context, self.context = self.context, None
            self._call('Target.disposeBrowserContext', browserContextId=context)
        self.response = None

    def __enter__(self):
        self.begin()
        return self

    def __exit__(self, *_):
        self.close()
