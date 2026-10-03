#!/usr/bin/env python3
"""Fresh per-job browser for affiliate portal callers (GLP-268).

Affiliate callers used to attach to long-lived Chrome listeners
(127.0.0.1:9324 Katalys, 127.0.0.1:9325 Awin) or saved profiles under
``~/.hermes/browser-profiles``. A caller now uses the network's API first and,
only where no API exists, this helper: one throwaway headless browser per job
with an empty context (no ``user_data_dir``, no ``storage_state``), closed when
the job ends. This helper never reads credentials: a login, where one is
needed, comes only from the origin-bound vault stage.

``refuse_persistent`` is the guard: any CDP endpoint or saved-profile value a
caller is still handed is refused rather than silently attached or ignored.
"""
from __future__ import annotations

import contextlib
import re
from typing import Any, Iterator
from urllib.parse import urlsplit

TRANSPORT = "fresh_per_job"
DEFAULT_VIEWPORT = {"width": 1440, "height": 1000}
LOGIN_PATH_RE = re.compile(r"/(?:login|log-in|signin|sign-in|sign_in|auth|oauth|sso|idp)(?:/|$)", re.I)


class PersistentBrowserRefused(RuntimeError):
    """A caller asked for a persistent Chrome/CDP endpoint or saved profile."""


def refuse_persistent(value: Any) -> None:
    """Refuse every non-empty CDP endpoint or saved-profile path."""
    if value not in (None, "") and str(value).strip():
        raise PersistentBrowserRefused("persistent_browser_endpoint_or_profile_refused")


def login_wall(url: str, expected_host: str) -> bool:
    """True when a fresh browser landed anywhere but the expected app host/path."""
    parts = urlsplit(str(url or ""))
    return parts.hostname != expected_host or bool(LOGIN_PATH_RE.search(parts.path or ""))


@contextlib.contextmanager
def fresh_context(pw: Any, *, headless: bool = True) -> Iterator[Any]:
    """Yield an empty context in a newly launched browser; close both after."""
    browser = pw.chromium.launch(headless=headless)
    try:
        context = browser.new_context(viewport=DEFAULT_VIEWPORT)
        try:
            yield context
        finally:
            with contextlib.suppress(Exception):
                context.close()
    finally:
        with contextlib.suppress(Exception):
            browser.close()

