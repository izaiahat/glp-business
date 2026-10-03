#!/usr/bin/env python3
"""First-class AI-browser read bridge; no browser launch or credential access.

Construct only inside an owned, new named browser_exec session, after the
origin-bound vault tool stages. The helper does not create/attach a browser,
read vault secrets, export cookies, or submit forms. Legacy shell/Playwright
callers must stop before I/O, not quietly launch a second unauthenticated browser.
A browser bridge is not permission to activate a held job or provider.
"""
from __future__ import annotations

import contextlib
import json
import re
import time
from types import SimpleNamespace
from typing import Any, Iterator
from urllib.parse import urlsplit

TRANSPORT = "fresh_ai_browser_vault"
LOGIN_PATH_RE = re.compile(r"/(?:login|log-in|signin|sign-in|sign_in|auth|oauth|sso|idp)(?:/|$)", re.I)


class PersistentBrowserRefused(RuntimeError):
    pass


class FreshAIBrowserRequired(RuntimeError):
    pass


def refuse_persistent(value: Any) -> None:
    if value not in (None, "") and str(value).strip():
        raise PersistentBrowserRefused("persistent_browser_endpoint_or_profile_refused")


def _safe_helper_call(stage, helper, *args):
    try:
        return helper(*args)
    except Exception:
        pass
    # Raise outside the handler, not just `from None` inside it: the raw
    # exception must not survive in __context__ for alternate formatters.
    raise FreshAIBrowserRequired(f"browser_helper_failed:{stage}")


def _https_origin(url):
    # Reject parser/browser disagreements before splitting. No raw URL is ever
    # placed in an exception; malformed ports and authorities fail closed.
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme == "https" and parts.hostname
                and parts.username is None and parts.password is None):
            return (parts.scheme, parts.hostname.lower(), parts.port if parts.port is not None else 443)
    except ValueError:
        pass
    return None


def login_wall(url: str, expected_host: str) -> bool:
    parts = urlsplit(str(url or ""))
    return (parts.scheme != "https" or parts.hostname != expected_host
            or bool(LOGIN_PATH_RE.search(parts.path or "")))


@contextlib.contextmanager
def fresh_context(pw: Any, *, headless: bool = True) -> Iterator[Any]:
    """Compatibility name: Playwright portal transport is not an allowed route."""
    raise FreshAIBrowserRequired("fresh_ai_browser_vault_stage_required")
    yield  # pragma: no cover


# Only the selectors used by AwinFreshBrowserTransport._visible and the
# existing button reader are supported. Arbitrary CSS can probe input values
# with attribute predicates even when the returned result is just a count.
_LOGIN_SELECTORS = frozenset({
    "input[type='email'], input[name='email'], input[name='username'], input[autocomplete='username']",
    "input[type='password'], input[autocomplete='current-password']",
})
_BUTTON_SELECTORS = frozenset({"button", "button:visible"})
_READS = {
    "title": "document.title",
    "body_text": "document.body?.innerText||''",
    "buttons": """(() => Array.from(document.querySelectorAll('button'))
      .filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;})
      .map(e=>(e.innerText||'').replace(/\\s+/g,' ').trim().slice(0,160)))()""",
    "links": """(n) => Array.from(document.querySelectorAll('a[href]')).slice(0,n)
      .map(a=>({href:a.href,text:(a.innerText||'').replace(/\\s+/g,' ').trim().slice(0,120)}))""",
}
_LOCATOR_READS = {
    "count": "return nodes.length;",
    "visible": "return !!e;",
    "enabled": "return !!e&&!e.disabled;",
    "button_text": "return e?.innerText||'';",
}


class _ReadLocator:
    def __init__(self, driver, selector, index=0):
        if selector not in _LOGIN_SELECTORS | _BUTTON_SELECTORS:
            raise FreshAIBrowserRequired("read_only_bridge_unsupported_selector")
        if type(index) is not int or index < 0:
            raise FreshAIBrowserRequired("read_only_bridge_invalid_index")
        self.driver, self.selector, self.index = driver, selector, index

    def _read(self, operation):
        visible = self.selector.endswith(":visible") or operation == "visible"
        selector = self.selector.removesuffix(":visible")
        prefix = """const nodes=Array.from(document.querySelectorAll(%s))
          .filter(e=>%s || (()=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
             return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';})());
          const e=nodes[%d];""" % (json.dumps(selector), "false" if visible else "true", self.index)
        self.driver._guard()
        return self.driver._call("locator", self.driver._js,
                                 "(() => {" + prefix + _LOCATOR_READS[operation] + "})()")

    def count(self):
        return self._read("count")

    def nth(self, index):
        return _ReadLocator(self.driver, self.selector, index)

    @property
    def first(self):
        return self.nth(0)

    def inner_text(self, **kwargs):
        if self.selector not in _BUTTON_SELECTORS:
            raise FreshAIBrowserRequired("read_only_bridge_no_control_text")
        return self._read("button_text")

    def is_visible(self):
        return bool(self._read("visible"))

    def is_enabled(self):
        return bool(self._read("enabled"))

    def fill(self, *args, **kwargs):
        raise FreshAIBrowserRequired("read_only_bridge_no_form_mutation")

    click = fill
    check = fill
    select_option = fill


class FreshAIBrowserReadDriver:
    """Inject registered browser_exec helpers, never a CDP endpoint or profile.

    This is a read-only driver, not an authentication adapter. Use real vault
    tools outside Python and the maintained portal-specific login stages first.
    Missing protected principal readback must still block in the consuming caller.
    A read bridge cannot grant application/terms-acceptance authority.
    """
    fresh = True

    def __init__(self, *, js, goto_url, wait_for_load, allowed_hosts,
                 owned_session, close_owned=None):
        if (not owned_session or not all(callable(x) for x in (js, goto_url, wait_for_load))):
            raise FreshAIBrowserRequired("fresh_ai_browser_owned_runtime_required")
        self.owned_session = owned_session
        self._js, self._goto, self._wait = js, goto_url, wait_for_load
        self.allowed_hosts = frozenset(allowed_hosts)
        if (not self.allowed_hosts or any(
                not isinstance(host, str) or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", host)
                for host in self.allowed_hosts)):
            raise FreshAIBrowserRequired("exact_first_party_hosts_required")
        # This compatibility API accepts hostnames, never implicit wildcard
        # ports. Each host denotes exactly its HTTPS default-port origin.
        self._allowed_origins = frozenset(("https", host, 443) for host in self.allowed_hosts)
        self._close_owned = close_owned
        self.page = self
        self._ctx = SimpleNamespace(pages=[self])
        self._closed = False

    def _ensure_open(self):
        if self._closed:
            raise FreshAIBrowserRequired("owned_read_bridge_closed")

    def _call(self, stage, helper, *args):
        self._ensure_open()
        return _safe_helper_call(stage, helper, *args)

    def _guard(self):
        url = str(self._call("origin", self._js, "location.href"))
        origin = _https_origin(url)
        if origin not in self._allowed_origins:
            raise FreshAIBrowserRequired("protected_first_party_origin_required")
        if login_wall(url, origin[1]):
            raise FreshAIBrowserRequired("fresh_ai_browser_vault_stage_required")
        return url

    def start(self):
        self._guard()
        return self

    @property
    def url(self):
        return self._guard()

    def current_url(self):
        return self._guard()

    def goto(self, url, **kwargs):
        self._ensure_open()
        if (_https_origin(url) not in self._allowed_origins
                or re.search(r"(?:awin1\.com|cread\.php|awclick\.php|/go/)", url, re.I)):
            raise FreshAIBrowserRequired("read_navigation_outside_first_party_scope")
        self._call("navigate", self._goto, url)
        self._call("load", self._wait)
        self._guard()
        # The AI helper gives no reliable HTTP response object. Unknown is not 200.
        return {"http_status": None}

    def evaluate(self, expression, arg=None):
        # Compatibility refusal only. No caller-supplied JavaScript is run,
        # including apparently harmless expressions; this is not a JS sandbox.
        # Awin's input-valued website reader, CJ's fetch, form scans and join
        # helpers are deliberately unsupported by this read bridge.
        raise FreshAIBrowserRequired("read_only_bridge_no_arbitrary_javascript")

    def _read(self, operation, arg=None):
        expression = _READS[operation]
        if arg is not None:
            expression = "(" + expression + ")(" + json.dumps(arg) + ")"
        self._guard()
        return self._call(operation, self._js, expression)

    def title(self):
        return self._read("title")

    def locator(self, selector):
        return _ReadLocator(self, selector)

    def body_text(self, limit=None):
        text = str(self._read("body_text"))
        return text[:limit] if limit else text

    def buttons(self, scope=""):
        if scope:
            raise FreshAIBrowserRequired("read_only_bridge_no_dialog_scope")
        return self._read("buttons")

    def links(self, limit=400):
        if type(limit) is not int or not 0 <= limit <= 400:
            raise FreshAIBrowserRequired("read_only_bridge_invalid_link_limit")
        return self._read("links", limit)

    def wait(self, ms):
        self._call("wait", time.sleep, min(max(ms, 0), 5000) / 1000)

    def close(self):
        if not self._closed:
            self._closed = True
            if self._close_owned is not None:
                _safe_helper_call("close", self._close_owned)

    def click_button(self, *args, **kwargs):
        raise FreshAIBrowserRequired("read_only_bridge_no_provider_mutation")

    scope_fill = click_button
    scope_check = click_button
    click_submit = click_button
    fill_field = click_button
    select_field = click_button
    check_field = click_button
