"""Offline real-caller regressions; only the CDP transport/DOM is a fixture.

Not live portal evidence. Never load credentials or launch any browser.
"""
import base64
import hashlib
import json
from pathlib import Path

import pytest

from scripts import glp_maxbounty_capacity_monitor as m
from scripts import glp_maxbounty_fresh_probe as f

BLOCKED = 'You have reached the maximum number of by request campaigns. New requests are not allowed.'
ROW = {'queue_id': m.TARGET_QUEUE_ID, 'program_id': m.TARGET_ID, 'network': 'maxbounty',
       'status': 'BLOCKED', 'payload': {'program': {'program_id': m.TARGET_ID, 'detail_url': m.DETAIL_URL}}}


class BrowserFixture:
    """CDP-level fixture, not a replacement producer or preclassified receipt."""
    def __init__(self, principal=f.ACCOUNT, relation=BLOCKED, status=200, campaign='14449'):
        self.calls = []
        self.events = []
        self.principal, self.relation, self.status, self.campaign = principal, relation, status, campaign
        self.location = f.LOGIN_URL
        self.sent = []
        self.raw = json.dumps({'campaign_id': campaign, 'details': {'affiliate_campaign_status': relation}}, ensure_ascii=False)
        self.second = False
        self.drop = False
        self.base64 = False
        self.challenge = False
        self.targets = [{'type': 'page', 'url': 'about:blank', 'targetId': 'initial'}]
        self.disposed = []

    def event(self, rid, url, method, response=None):
        params = {'requestId': rid, 'request': {'url': url, 'method': method}, 'resourceType': 'XHR', 'frameId': 'main-frame'}
        if response is not None:
            params['responseStatusCode'] = response
        return {'method': 'Fetch.requestPaused', 'session_id': 'owned-session', 'params': params}

    def switch_tab(self, target):
        assert target == 'owned-tab'
        return 'owned-session'

    def drain_events(self):
        events, self.events = self.events, []
        return events

    def cdp(self, method, session_id=None, **kw):
        self.calls.append((method, kw))
        if method == 'Target.getTargets':
            return {'targetInfos': self.targets}
        if method == 'Target.getBrowserContexts':
            return {'browserContextIds': []}
        if method == 'Target.createBrowserContext':
            assert kw == {'disposeOnDetach': True}
            return {'browserContextId': 'owned-context'}
        if method == 'Target.createTarget':
            assert kw['url'] == 'about:blank'
            assert kw['browserContextId'] == 'owned-context'
            return {'targetId': 'owned-tab'}
        if method == 'Page.getFrameTree':
            return {'frameTree': {'frame': {'id': 'main-frame'}}}
        if method == 'Target.getTargetInfo':
            return {'targetInfo': {'url': 'about:blank', 'browserContextId': 'owned-context'}}
        if method == 'Target.disposeBrowserContext':
            self.disposed.append(kw['browserContextId'])
        if method == 'Runtime.evaluate':
            if kw['expression'] == f.PRINCIPAL_JS:
                return {'result': {'value': {'origin': f.ORIGIN, 'url': self.location,
                    'emails': [self.principal], 'protected_page': True,
                    'password_visible': False, 'challenge': self.challenge,
                    'apply': True, 'approved': False}}}
            if m.DETAIL_URL in kw['expression']:
                self.location = m.DETAIL_URL
                self.events.append(self.event('campaign-1', f.CAMPAIGN_RESPONSE, 'GET'))
        if method == 'Fetch.continueRequest':
            rid = kw['requestId']
            if rid not in self.sent:
                self.sent.append(rid)
                if rid == 'auth-1':
                    self.events.append(self.event(rid, f.AUTH_RESPONSE, 'POST', 200))
                elif rid == 'campaign-1' and not self.drop:
                    self.events.append(self.event(rid, f.CAMPAIGN_RESPONSE, 'GET', self.status))
            elif rid == 'campaign-1' and self.second:
                self.events.append(self.event('campaign-2', f.CAMPAIGN_RESPONSE, 'GET'))
        if method == 'Fetch.getResponseBody':
            body = json.dumps({'success': True, 'fixture_only_ignored_secret': 'NOT_A_REAL_TOKEN'}) if kw['requestId'] == 'auth-1' else self.raw
            return {'body': base64.b64encode(body.encode()).decode() if self.base64 else body,
                    'base64Encoded': self.base64}
        return {}

    def vault_fixture(self, producer):
        # Sole simulated authentication effect. No claimed real vault invocation.
        self.events.append(self.event('auth-1', f.AUTH_RESPONSE, 'POST'))
        producer.poll()
        producer.poll()


@pytest.fixture
def caller(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'BUSINESS', tmp_path)
    for name in ['reports/glp-clinic-scout/affiliate-machine/applications/.application-machine.lock',
                 'reports/ops/orchestration-restore-20260907/PLAN/MARKETPLACE-ACCESS/M2b/MAXBOUNTY-LIVE/owner.lock']:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    profile = tmp_path / 'profile'
    profile.mkdir()
    proc = tmp_path / 'proc'
    proc.mkdir()
    original = m.assert_profile_idle
    monkeypatch.setattr(m, 'assert_profile_idle', lambda _profile: original(profile, proc))
    sources = [Path(m.__file__).resolve(), Path(f.__file__).resolve()]
    pins = tmp_path / 'pins.json'
    pins.write_text(json.dumps({'files': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}}))

    def invoke(browser, *, login=True, before=None, rows=None):
        state = tmp_path / 'state.json'
        leases, producer = m.fresh_browser_session(pins=pins, cdp=browser.cdp,
            drain_events=browser.drain_events, switch_tab=browser.switch_tab)
        with leases:
            if login:
                browser.vault_fixture(producer)
            if before:
                before(producer)
            result = m.run_once(state, lambda: [ROW] if rows is None else rows, producer.probe)
            assert json.loads(state.read_text()) == result
        assert browser.disposed == ['owned-context']
        return result, producer
    invoke.profile, invoke.pins = profile, pins
    return invoke


@pytest.mark.parametrize('encoded', [False, True])
def test_actual_caller_fresh_principal_and_blocked_marker(caller, encoded):
    browser = BrowserFixture()
    browser.base64 = encoded
    result, producer = caller(browser)
    assert result['status'] == 'OBSERVED'
    assert result['capacity'] == 'BLOCKED'
    assert result['classification'] == 'provider_capacity_blocked'
    assert result['successful_reads'] == 1
    assert result['provider_pending_requests'] is None
    assert result['campaign_api_body_sha256'] == hashlib.sha256(browser.raw.encode()).hexdigest()
    assert result['network_boundary']['campaign_gets'] == 1
    assert result['network_boundary']['authentication_posts'] == 1
    assert browser.sent == ['auth-1', 'campaign-1']
    assert producer.authenticated is True
    assert 'NOT_A_REAL_TOKEN' not in json.dumps(result)
    methods = [x[0] for x in browser.calls]
    assert methods.index('Fetch.enable') < methods.index('Runtime.evaluate')
    assert not any('reload' in name.lower() or name in {'Network.setCookie', 'Network.setCookies'} for name in methods)


def test_wrong_server_rendered_principal_refuses_before_campaign(caller):
    browser = BrowserFixture(principal='other@example.invalid')
    result, _ = caller(browser)
    assert result['reason'] == 'AUTHENTICATED_PRINCIPAL_MISMATCH'
    assert result['capacity'] == 'UNKNOWN'
    assert result['successful_reads'] == 0
    assert browser.sent == ['auth-1']


@pytest.mark.parametrize('relation,classification', [
    ('', 'apply_needed'), ('Pending', 'application_pending'), ('Denied', 'application_denied'),
    ('maximum number of campaigns; new requests allowed', 'apply_needed')])
def test_absent_exact_marker_never_means_available(caller, relation, classification):
    result, _ = caller(BrowserFixture(relation=relation))
    assert result['capacity'] == 'UNKNOWN'
    assert result['status'] == 'OBSERVED'
    assert result['classification'] == classification
    assert result['provider_pending_requests_evidence'] == 'NOT_OBSERVED'


def test_fresh_auth_is_not_cookie_reuse(caller):
    browser = BrowserFixture()
    result, _ = caller(browser, login=False)
    assert result['reason'] == 'FRESH_AUTHENTICATION_UNCONFIRMED'
    assert browser.sent == []


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_http_failures_remain_unknown(caller, status):
    result, _ = caller(BrowserFixture(status=status))
    assert result['reason'] == 'AUTH_OR_CAMPAIGN_RESPONSE_REFUSED'
    assert result['successful_reads'] == 0
    assert result['capacity'] == 'UNKNOWN'


def test_valid_404_not_account_capacity(caller):
    result, _ = caller(BrowserFixture(status=404))
    assert result['classification'] == 'terminal_provider_unavailable'
    assert result['capacity'] == 'UNKNOWN'


def test_wrong_body_identity(caller):
    result, _ = caller(BrowserFixture(campaign='99999'))
    assert result['reason'] == 'CAMPAIGN_RESPONSE_IDENTITY_MISMATCH'
    assert result['capacity'] == 'UNKNOWN'


def test_second_request_aborted_before_network(caller):
    browser = BrowserFixture()
    browser.second = True
    result, _ = caller(browser)
    assert result['reason'] == 'CAMPAIGN_READ_COUNT_INVALID'
    assert browser.sent == ['auth-1', 'campaign-1']
    assert ('Fetch.failRequest', {'requestId': 'campaign-2', 'errorReason': 'BlockedByClient'}) in browser.calls


def test_timeout_consumes_attempt_no_reload_no_retry(caller):
    browser = BrowserFixture()
    browser.drop = True
    def fast_clock(producer):
        times = iter([0, 0, 21])
        producer.clock = lambda: next(times)
    result, producer = caller(browser, before=fast_clock)
    assert result['reason'] == 'CAMPAIGN_RESPONSE_NOT_OBSERVED'
    assert producer.guard.campaign_gets == 1
    assert browser.sent == ['auth-1', 'campaign-1']
    with pytest.raises(m.Refusal, match='CAMPAIGN_READ_COUNT_INVALID'):
        producer.probe(f.TARGET)


def test_profile_lock_still_refuses_before_browser(caller):
    (caller.profile / 'SingletonLock').symlink_to('fixture-owner')
    browser = BrowserFixture()
    with pytest.raises(m.Refusal, match='PROFILE_ALREADY_OWNED'):
        caller(browser)
    assert browser.calls == []


def test_source_pin_failure_precedes_browser(caller):
    caller.pins.write_text(json.dumps({'files': {str(Path(f.__file__).resolve()): '0' * 64}}))
    browser = BrowserFixture()
    with pytest.raises(m.Refusal, match='SOURCE_PIN_MISMATCH'):
        caller(browser)
    assert browser.calls == []


def test_no_legacy_default_transport(monkeypatch):
    from contextlib import ExitStack
    monkeypatch.setattr(m, 'session_leases', ExitStack)
    monkeypatch.setattr(m, 'restricted_classes', lambda _: pytest.fail('legacy transport called'))
    with pytest.raises(m.Refusal, match='FRESH_VAULT_STAGES_NOT_INTEGRATED'):
        m.live_probe(f.TARGET)


def test_markerless_unknown_does_not_overwrite_observed_state_on_disk(caller):
    # This is ONLY a temporary caller state; the operational receipt is untouched.
    result, _ = caller(BrowserFixture(relation='Pending'))
    assert result['effects'] == {'queue_writes': 0, 'applications': 0, 'sheet_calls': 0, 'tracking_clicks': 0, 'tracking_mints': 0}


@pytest.mark.parametrize('url,method,resource', [
    ('https://api.maxbounty.com/affiliates/api/campaign/99999', 'GET', 'XHR'),
    ('https://api.maxbounty.com/affiliates/api/campaignHub', 'GET', 'XHR'),
    ('https://api.maxbounty.com/affiliates/api/requestApproval', 'POST', 'XHR'),
    ('https://affiliates.maxbounty.com/tracking.js', 'GET', 'Script'),
    ('https://affiliates.maxbounty.com/go/123', 'GET', 'Document'),
    (f.CAMPAIGN_RESPONSE + '?retry=1', 'GET', 'XHR'),
    ('https://elsewhere.invalid/style.css', 'GET', 'Stylesheet'),
])
def test_effects_and_unrelated_reads_blocked(url, method, resource):
    browser = BrowserFixture()
    p = f.FreshCapacityProbe(cdp=browser.cdp, drain_events=browser.drain_events, switch_tab=browser.switch_tab)
    p.armed = True
    assert p._allow(method, url, resource) is False


def test_existing_queue_identity_oracle_still_runs(caller):
    browser = BrowserFixture()
    result, _ = caller(browser, rows=[dict(ROW, queue_id='other')])
    assert result['reason'] == 'QUEUE_IDENTITY_OR_STATE_MISMATCH'
    assert browser.sent == ['auth-1']


def test_reused_browser_refused(caller):
    browser = BrowserFixture()
    browser.targets[0]['url'] = f.LOGIN_URL
    with pytest.raises(m.Refusal, match='FRESH_BROWSER_NOT_EXCLUSIVE_BLANK'):
        caller(browser)
    assert not any(name == 'Target.createBrowserContext' for name, _ in browser.calls)


def test_awin_stages_do_not_require_a_retained_python_controller():
    """Positive contrast: re-import Awin between stages; browser DOM survives.

    This is the actual PR #17 caller, not a new password adapter. The boolean
    below models the separate secure vault tool, never a real credential.
    """
    import importlib.util
    from tests.test_glp_awin_portal_login import FakePage

    def load_stage():
        spec = importlib.util.spec_from_file_location(
            'isolated_awin_stage', Path(m.__file__).with_name('glp_awin_portal_login.py'))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    page = FakePage()
    first = load_stage()
    assert first.prepare(page.page_info, page.js, page.click, page.cdp, page.wait)['stage'] == 'vault_fill_required'
    del first
    page.password = True
    second = load_stage()
    result = second.finish(page.page_info, page.js, page.wait, page.goto)
    assert result['authenticated'] is True
    assert result['publisher_id'] == '2979387'
    assert page.clicks.count('submit') == 1


def test_maxbounty_reconstructing_controller_cannot_resume_awin_style(caller):
    """Browser state alone cannot recreate consumed budgets or owned context."""
    browser = BrowserFixture()
    leases, original = m.fresh_browser_session(pins=caller.pins, cdp=browser.cdp,
        drain_events=browser.drain_events, switch_tab=browser.switch_tab)
    with leases:
        browser.vault_fixture(original)
        assert original.authenticated is True
        resumed = f.FreshCapacityProbe(cdp=browser.cdp, drain_events=browser.drain_events,
                                      switch_tab=browser.switch_tab)
        result = m.run_once(caller.pins.parent / 'resumed.json', lambda: [ROW], resumed.probe)
        assert result['reason'] == 'FRESH_BROWSER_NOT_STARTED'
        assert result['capacity'] == 'UNKNOWN'
        assert result['successful_reads'] == 0
        assert browser.sent == ['auth-1']
    assert browser.disposed == ['owned-context']


def test_real_session_leases_do_not_survive_interpreter_exit(caller):
    """Use real flock + real short-lived Python; never touch the live leases.

    The child deliberately leaves its ExitStack alive until interpreter exit,
    just as a browser_exec variable would. A second owner is blocked while the
    child lives but succeeds between stages. Reacquisition is not continuous
    custody. This tests a concrete constraint, not a requirement for a bridge.
    """
    import subprocess
    import sys

    code = '''
import sys
from pathlib import Path
from scripts import glp_maxbounty_capacity_monitor as m
m.BUSINESS = Path(sys.argv[1])
original = m.assert_profile_idle
m.assert_profile_idle = lambda _: original(m.BUSINESS / 'profile', m.BUSINESS / 'proc')
leases = m.session_leases()
print('FIXTURE_LEASES_HELD', flush=True)
sys.stdin.readline()
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(caller.pins.parent)],
        cwd=Path(m.__file__).resolve().parents[1], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        import selectors
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=10), 'fixture child did not acquire leases'
        assert child.stdout.readline().strip() == 'FIXTURE_LEASES_HELD'
        with pytest.raises(m.Refusal, match='SESSION_LEASE_BUSY'):
            m.session_leases()
        _, err = child.communicate('\n', timeout=10)
        assert child.returncode == 0, err
        with m.session_leases():
            pass  # another owner can enter before the next browser_exec stage
    finally:
        if child.poll() is None:
            child.communicate('\n', timeout=10)
