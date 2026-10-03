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


def login_wall(url: str, expected_host: str) -> bool:
    parts = urlsplit(str(url or ""))
    return (parts.scheme != "https" or parts.hostname != expected_host
            or bool(LOGIN_PATH_RE.search(parts.path or "")))


@contextlib.contextmanager
def fresh_context(pw: Any, *, headless: bool = True) -> Iterator[Any]:
    """Compatibility name: Playwright portal transport is not an allowed route."""
    raise FreshAIBrowserRequired("fresh_ai_browser_vault_stage_required")
    yield  # pragma: no cover


class _ReadLocator:
    def __init__(self, driver, selector, index=None):
        self.driver, self.selector, self.index = driver, selector, index

    def _read(self, expression):
        visible = ":visible" in self.selector
        selector = self.selector.replace(":visible", "")
        prefix = """const nodes=Array.from(document.querySelectorAll(%s))
          .filter(e=>%s || (()=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
             return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';})());
          const e=nodes[%d];""" % (json.dumps(selector), "false" if visible else "true", self.index or 0)
        return self.driver.evaluate("(() => {" + prefix + expression + "})()")

    def count(self):
        return self._read("return nodes.length;")

    def nth(self, index):
        return _ReadLocator(self.driver, self.selector, index)

    @property
    def first(self):
        return self.nth(0)

    def inner_text(self, **kwargs):
        return self._read("return e?.innerText||'';")

    def is_visible(self):
        return bool(_ReadLocator(self.driver, self.selector + ":visible", self.index).count())

    def is_enabled(self):
        return bool(self._read("return !!e&&!e.disabled;"))

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
        if not self.allowed_hosts:
            raise FreshAIBrowserRequired("exact_first_party_hosts_required")
        self._close_owned = close_owned
        self.page = self
        self._ctx = SimpleNamespace(pages=[self])
        self._closed = False

    def _guard(self):
        if self._closed:
            raise FreshAIBrowserRequired("owned_read_bridge_closed")
        url = str(self._js("location.href"))
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in self.allowed_hosts
                or parts.username is not None or parts.password is not None):
            raise FreshAIBrowserRequired("protected_first_party_origin_required")
        if login_wall(url, parts.hostname):
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
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in self.allowed_hosts
                or parts.username is not None or parts.password is not None
                or re.search(r"(?:awin1\.com|cread\.php|awclick\.php|/go/)", url, re.I)):
            raise FreshAIBrowserRequired("read_navigation_outside_first_party_scope")
        self._goto(url)
        self._wait()
        self._guard()
        # The AI helper gives no reliable HTTP response object. Unknown is not 200.
        return {"http_status": None}

    def evaluate(self, expression, arg=None):
        self._guard()
        if arg is None:
            # Playwright-style function expressions must be invoked for js().
            stripped = expression.strip()
            expression = "(" + expression + ")()" if stripped.startswith("() =>") else expression
        else:
            expression = "(" + expression + ")(" + json.dumps(arg) + ")"
        return self._js(expression)

    def title(self):
        return self.evaluate("document.title")

    def locator(self, selector):
        return _ReadLocator(self, selector)

    def body_text(self, limit=None):
        text = str(self.evaluate("document.body?.innerText||''"))
        return text[:limit] if limit else text

    def buttons(self, scope=""):
        if scope:
            raise FreshAIBrowserRequired("read_only_bridge_no_dialog_scope")
        return self.evaluate("""(() => Array.from(document.querySelectorAll('button'))
          .filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;})
          .map(e=>(e.innerText||'').replace(/\\s+/g,' ').trim().slice(0,160)))()""")

    def links(self, limit=400):
        return self.evaluate("""(n) => Array.from(document.querySelectorAll('a[href]')).slice(0,n)
          .map(a=>({href:a.href,text:(a.innerText||'').replace(/\\s+/g,' ').trim().slice(0,120)}))""", limit)

    def wait(self, ms):
        time.sleep(min(max(ms, 0), 5000) / 1000)

    def close(self):
        if not self._closed:
            self._closed = True
            if self._close_owned is not None:
                self._close_owned()

    def click_button(self, *args, **kwargs):
        raise FreshAIBrowserRequired("read_only_bridge_no_provider_mutation")

    scope_fill = click_button
    scope_check = click_button
    click_submit = click_button
    fill_field = click_button
    select_field = click_button
    check_field = click_button
