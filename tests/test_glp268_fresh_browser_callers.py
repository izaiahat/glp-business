"""GLP-268: affiliate portal callers use API first, then a fresh per-job browser.

The portal-apply lane (Awin probe/join/commission capture, Katalys apply) may
not attach to the persistent Chrome listeners (127.0.0.1:9324, 127.0.0.1:9325)
or a saved ``browser-profiles`` directory. Offline: Playwright and host-only
modules are replaced by recording fakes; no browser is launched.
"""
from __future__ import annotations

import contextlib
import importlib
import re
import sys
import types
import traceback
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import affiliate_fresh_browser as fresh  # noqa: E402

MIGRATED_CALLERS = (
    "scripts/marketplace_adapters/portal_apply.py",
    "scripts/affiliate_fresh_browser.py",
)
API_IDENTITY = {"verified": True, "publisher_id": "2979387",
                "account_id": "2979387", "legal_name": "Tilton Group Ltd"}


class FakeContext:
    def __init__(self, log):
        self.log = log

    def new_page(self):
        self.log.append("new_page")
        return types.SimpleNamespace(set_default_timeout=lambda ms: None)

    def close(self):
        self.log.append("context.close")


class FakeBrowser:
    def __init__(self, log):
        self.log = log

    def new_context(self, **kwargs):
        self.log.append(("new_context", sorted(kwargs)))
        return FakeContext(self.log)

    def close(self):
        self.log.append("browser.close")


class FakeChromium:
    def __init__(self, log):
        self.log = log

    def launch(self, **kwargs):
        self.log.append(("launch", sorted(kwargs)))
        return FakeBrowser(self.log)

    def connect_over_cdp(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("persistent CDP attach attempted")

    def launch_persistent_context(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("saved profile launch attempted")


FRESH_LIFECYCLE = [("launch", ["headless"]), ("new_context", ["viewport"]),
                   "new_page", "context.close", "browser.close"]


# --------------------------------------------------------------- acceptance

def test_portal_apply_has_no_persistent_chrome_or_saved_profile_attach():
    banned = re.compile(r"connect_over_cdp\(|launch_persistent_context\(|"
                        r"\"http://127\.0\.0\.1:932[45]\"|browser-profiles/")
    for relative in MIGRATED_CALLERS:
        assert not banned.search((ROOT / relative).read_text(encoding="utf-8")), relative


def test_fresh_context_refuses_playwright_before_launch():
    log: list = []
    with pytest.raises(fresh.FreshAIBrowserRequired, match="fresh_ai_browser_vault_stage_required"):
        with fresh.fresh_context(types.SimpleNamespace(chromium=FakeChromium(log))):
            pytest.fail("Playwright portal context yielded")
    assert log == []


@pytest.mark.parametrize("value", ["http://127.0.0.1:9325", "http://127.0.0.1:9324",
                                   "/home/ubuntu/.hermes/browser-profiles/awin-operator"])
def test_persistent_endpoint_or_profile_is_refused(value):
    with pytest.raises(fresh.PersistentBrowserRefused):
        fresh.refuse_persistent(value)
    fresh.refuse_persistent("")


def test_login_wall_detects_redirect_and_login_paths():
    assert fresh.login_wall("https://auth.katalys.com/login", "app.katalys.com")
    assert fresh.login_wall("https://app.katalys.com/login?next=/pub", "app.katalys.com")
    assert not fresh.login_wall("https://app.katalys.com/pub/org/explore/1", "app.katalys.com")


# ------------------------------------------------------- portal-apply lane

@pytest.fixture
def portal_apply(monkeypatch):
    """Import portal_apply with host-only dependencies replaced by fakes."""
    # Reduced source checkouts lack package markers. Bind the package paths so
    # a regular package in the live workspace cannot win import precedence.
    for name, relative in (("scripts", "scripts"),
                           ("scripts.marketplace_adapters", "scripts/marketplace_adapters")):
        package = types.ModuleType(name)
        package.__path__ = [str(ROOT / relative)]
        monkeypatch.setitem(sys.modules, name, package)

    class BaseDriver:
        def __init__(self, profile_dir, **kwargs):
            self.profile_dir, self.page, self._pw, self._ctx = profile_dir, None, None, None

        def start(self):  # pragma: no cover - profile-backed lane is not exercised
            raise AssertionError("profile-backed driver started")

        def close(self):
            pass

    class Anything(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return lambda *args, **kwargs: None

    portal_lane = Anything("scripts.marketplace_adapters.portal_lane")
    portal_lane.PlaywrightDriver = BaseDriver
    stubs = {"scripts.marketplace_adapters.portal_lane": portal_lane}
    for name in ("affiliate_contract_policy", "affiliate_operator_rulings",
                 "affiliate_blocker_completeness", "affiliate_niche_geo_filter",
                 "awin_api_first_application", "glp_item_e_application_queue"):
        stubs[f"scripts.{name}"] = Anything(f"scripts.{name}")
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
    import scripts
    monkeypatch.setattr(scripts, "affiliate_operator_rulings",
                        stubs["scripts.affiliate_operator_rulings"], raising=False)
    monkeypatch.delitem(sys.modules, "scripts.marketplace_adapters.portal_apply", raising=False)
    yield importlib.import_module("scripts.marketplace_adapters.portal_apply")
    sys.modules.pop("scripts.marketplace_adapters.portal_apply", None)


@pytest.fixture
def fake_sync_playwright(monkeypatch):
    log: list = []

    @contextlib.contextmanager
    def sync_playwright():
        log.append("playwright.start")
        yield types.SimpleNamespace(chromium=FakeChromium(log))
        log.append("playwright.stop")

    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api",
                        types.SimpleNamespace(sync_playwright=sync_playwright))
    return log


def test_api_identity_never_launches_a_portal_browser(portal_apply, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("API identity launched a browser")
    monkeypatch.setattr(portal_apply, "PlaywrightApplyDriver", forbidden)
    transport = portal_apply.AwinFreshBrowserTransport(api_identity=API_IDENTITY)
    assert transport.authenticate_and_verify()["identity_source"] == "api"
    transport.close()


def test_default_portal_driver_requires_ai_vault_stage(portal_apply, fake_sync_playwright):
    with pytest.raises(RuntimeError, match="fresh_ai_browser_vault_stage_required"):
        portal_apply.PlaywrightApplyDriver(fresh=True).start()
    assert fake_sync_playwright == []


def test_awin_transport_uses_api_without_launching_second_browser(portal_apply, fake_sync_playwright):
    transport = portal_apply.AwinFreshBrowserTransport(api_identity=API_IDENTITY)
    assert portal_apply.AwinPlaywrightCDPTransport is portal_apply.AwinFreshBrowserTransport
    assert transport.driver is None
    assert transport.authenticate_and_verify()["browser"] == "not_used"
    transport.close()
    assert fake_sync_playwright == []


def test_awin_transport_refuses_legacy_cdp_endpoint(portal_apply):
    with pytest.raises(portal_apply.AwinAuthFailure) as caught:
        portal_apply.AwinFreshBrowserTransport("http://127.0.0.1:9325")
    assert (caught.value.failed_step, caught.value.detail) == (
        "launch_fresh_browser", "persistent_cdp_endpoint_refused")
    with pytest.raises(RuntimeError, match="persistent_browser_endpoint_or_profile_refused"):
        portal_apply.PlaywrightApplyDriver(cdp_url="http://127.0.0.1:9324")


def test_awin_auth_report_names_fresh_browser_not_saved_profile(portal_apply):
    transport = portal_apply.AwinFreshBrowserTransport(
        driver=types.SimpleNamespace(close=lambda: None), api_identity=API_IDENTITY)
    report = transport._api_verified_report(captcha_used=False)
    assert report["browser"] == "fresh_ai_browser_vault"
    assert report["profile"] == "" and report["cdp_endpoint"] == ""


def _awin_login_wall_driver():
    page = types.SimpleNamespace(url="https://id.awin.com/u/login/identifier")
    page.locator = lambda selector: types.SimpleNamespace(
        count=lambda: 1 if "email" in selector else 0,
        nth=lambda index: types.SimpleNamespace(is_visible=lambda: True))
    return types.SimpleNamespace(page=page, goto=lambda url: None, wait=lambda ms: None,
                                 current_url=lambda: page.url, close=lambda: None)


class NoOsAccess:
    def __getattr__(self, name):
        raise AssertionError(f"os.{name} used during fresh-browser auth")


def test_awin_login_wall_uses_no_credential_file_or_captcha_solver(portal_apply, monkeypatch):
    # Regression: a fresh browser at the login form must not fall back to the
    # credential-file password or the paid CAPTCHA solver; vault stage only.
    monkeypatch.setattr(portal_apply, "os", NoOsAccess())
    transport = portal_apply.AwinFreshBrowserTransport(
        driver=_awin_login_wall_driver(), api_identity=API_IDENTITY)
    report = transport.authenticate_and_verify()
    assert (report["status"], report["identity_source"]) == ("API_VERIFIED", "api")
    assert not hasattr(transport, "_private_login")
    assert not hasattr(transport, "captcha_solver")


def test_awin_login_wall_without_api_identity_stops_for_vault_stage(portal_apply):
    transport = portal_apply.AwinFreshBrowserTransport(driver=_awin_login_wall_driver())
    with pytest.raises(portal_apply.AwinAuthFailure) as caught:
        transport.authenticate_and_verify()
    assert (caught.value.failed_step, caught.value.detail) == (
        "vault_login_required", "fresh_vault_login_stage_not_integrated")


def test_ai_bridge_requires_an_owned_runtime_and_refuses_mutations():
    with pytest.raises(fresh.FreshAIBrowserRequired):
        fresh.FreshAIBrowserReadDriver(js=None, goto_url=lambda _: None,
            wait_for_load=lambda: None,
            allowed_hosts={"ui.awin.com"}, owned_session="")
    events = []
    driver = fresh.FreshAIBrowserReadDriver(
        js=lambda expression: "https://ui.awin.com/protected" if expression == "location.href" else "protected body",
        goto_url=lambda url: events.append(url),
        wait_for_load=lambda: None, allowed_hosts={"ui.awin.com"},
        owned_session="test-owned", close_owned=lambda: events.append("closed"))
    assert driver.body_text() == "protected body"
    driver.goto("https://ui.awin.com/protected")
    with pytest.raises(fresh.FreshAIBrowserRequired, match="outside_first_party"):
        driver.goto("https://www.awin1.com/awclick.php")
    with pytest.raises(fresh.FreshAIBrowserRequired, match="read_only"):
        driver.page.locator("button").click()
    with pytest.raises(fresh.FreshAIBrowserRequired, match="read_only"):
        driver.page.locator("input").fill("not a credential")
    driver.close()
    driver.close()
    assert events == ["https://ui.awin.com/protected", "closed"]


def _recording_read_bridge():
    events = []
    protected = "https://ui.awin.com/awin/affiliate/2979387/merchant-directory/index/tab/active/page/1"

    def js(expression):
        events.append(("js", expression))
        return protected if expression == "location.href" else "fixture body"

    driver = fresh.FreshAIBrowserReadDriver(
        js=js, goto_url=lambda url: events.append(("goto", url)),
        wait_for_load=lambda: events.append(("wait",)),
        allowed_hosts={"ui.awin.com"}, owned_session="offline-regression",
        close_owned=lambda: events.append(("close",)))
    return driver, events, protected


def test_read_bridge_refuses_caller_javascript_and_value_selectors():
    driver, events, _ = _recording_read_bridge()
    for expression, arg in (
        ("() => document.querySelector('button').click()", None),
        ("() => document.querySelector('form').requestSubmit()", None),
        ("() => fetch('/fixture', {method:'POST'})", None),
        ("() => [document.querySelector('input').value, document.cookie]", None),
        ("x => x", {"fixture": "not executable"}),
        ("1 + 1", None),
    ):
        with pytest.raises(fresh.FreshAIBrowserRequired):
            driver.evaluate(expression, arg)
    # CSS value predicates must not become a secret-value oracle either.
    with pytest.raises(fresh.FreshAIBrowserRequired):
        driver.locator('input[value^="fixture"]').count()
    with pytest.raises(fresh.FreshAIBrowserRequired):
        driver.locator("input[type='password'], input[autocomplete='current-password']").inner_text()
    assert events == []
    assert driver.body_text() == "fixture body"


def test_read_bridge_binds_scheme_host_and_effective_port():
    driver, events, protected = _recording_read_bridge()
    for url in (
        protected.replace("ui.awin.com", "ui.awin.com:444"),
        protected.replace("ui.awin.com", "ui.awin.com:not-a-port"),
        protected.replace("https:", "http:"),
        protected.replace("ui.awin.com", "foreign.invalid"),
        protected.replace("ui.awin.com", ":fixture@ui.awin.com"),
        protected.replace("ui.awin.com", "ui.awin.com\\t"),
    ):
        driver._js = lambda expression: url
        with pytest.raises(fresh.FreshAIBrowserRequired):
            driver.body_text()
        with pytest.raises(fresh.FreshAIBrowserRequired):
            driver.goto(url)
    assert events == []  # no rejected target ever reaches navigation
    for url in (protected, protected.replace("ui.awin.com", "UI.AWIN.COM:443")):
        driver._js = lambda expression: url if expression == "location.href" else "body"
        assert driver.body_text() == "body"
        assert driver.goto(url) == {"http_status": None}
    assert [event[0] for event in events] == ["goto", "wait", "goto", "wait"]


def test_closed_read_bridge_refuses_before_any_helper_io(monkeypatch):
    driver, events, protected = _recording_read_bridge()
    button = driver.locator("button")
    monkeypatch.setattr(fresh.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
    driver.close()
    driver.close()
    assert events == [("close",)]
    events.clear()
    for action in (lambda: driver.goto(protected), driver.start, driver.current_url,
                   driver.body_text, driver.title, driver.buttons, driver.links,
                   button.count, lambda: driver.wait(1)):
        with pytest.raises(fresh.FreshAIBrowserRequired, match="owned_read_bridge_closed"):
            action()
    assert events == []
    # A helper closing its bridge must not allow the next helper in a batch.
    other, events, protected = _recording_read_bridge()
    other._goto = lambda url: other.close()
    with pytest.raises(fresh.FreshAIBrowserRequired, match="owned_read_bridge_closed"):
        other.goto(protected)
    assert events == [("close",)]


def test_read_bridge_and_awin_errors_drop_raw_cause_and_context(portal_apply, monkeypatch):
    sentinel = "FIXTURE_ONLY_PRIVATE_HELPER_DIAGNOSTIC"

    def fail(*args):
        raise RuntimeError(sentinel)

    for stage in ("origin", "body_text", "locator", "navigate", "load", "wait", "close"):
        driver, events, protected = _recording_read_bridge()
        action = driver.body_text
        if stage == "origin":
            driver._js = fail
        elif stage in ("body_text", "locator"):
            driver._js = lambda expression: protected if expression == "location.href" else fail()
            if stage == "locator":
                action = driver.locator("button").count
        elif stage in ("navigate", "load"):
            setattr(driver, "_goto" if stage == "navigate" else "_wait", fail)
            action = lambda: driver.goto(protected)
        elif stage == "wait":
            monkeypatch.setattr(fresh.time, "sleep", fail)
            action = lambda: driver.wait(1)
        else:
            driver._close_owned = fail
            action = driver.close
        with pytest.raises(fresh.FreshAIBrowserRequired) as caught:
            action()
        assert str(caught.value) == f"browser_helper_failed:{stage}"
        assert sentinel not in "".join(traceback.format_exception(caught.value))
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        if stage == "close":
            driver.close()  # failure still makes close terminal, not retryable
            with pytest.raises(fresh.FreshAIBrowserRequired, match="owned_read_bridge_closed"):
                driver.goto(protected)
    # Test the outer wrapper independently; a safe bridge error must not mask
    # whether a different injected driver's raw exception is still chained.
    driver = types.SimpleNamespace(page=None, goto=fail)
    with pytest.raises(portal_apply.AwinAuthFailure) as caught:
        portal_apply.AwinFreshBrowserTransport(driver=driver).authenticate_and_verify()
    assert sentinel not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert caught.value.failed_step == "navigate_publisher_directory"


def test_api_only_identity_cannot_authorize_terms_or_join(portal_apply):
    transport = portal_apply.AwinFreshBrowserTransport(api_identity=API_IDENTITY)
    for action in ("capture_program", "capture_authenticated_terms", "join_once"):
        with pytest.raises(portal_apply.AwinAuthFailure, match="vault_login_required"):
            transport.call(action, {"program_id": "123"})
    transport.close()


def test_katalys_executor_uses_fresh_browser_and_blocks_on_login_wall(portal_apply):
    login_page = types.SimpleNamespace(
        goto=lambda url: {"http_status": 200}, wait=lambda ms: None,
        body_text=lambda *a: "Sign in", current_url=lambda: "https://auth.katalys.com/login")
    executor = portal_apply.KatalysApplicationExecutor(driver=login_page, profile={})
    assert executor.make_driver().fresh is True
    result = executor.capture({"program_id": "123", "name": "Example"})
    assert result == {"status": "BLOCKED", "blocker": "katalys_fresh_login_required"}
