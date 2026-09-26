#!/usr/bin/env python3
"""Fresh Awin login stages for the AI browser and origin-bound vault.

Call prepare() in a fresh browser session on ui.awin.com; call the browser
vault list/fill tools at id.awin.com; call finish() after the vault fill.
No persistent CDP profile, credential file, password input, screenshots or
unredacted exception logging is permitted in this caller.
"""
from __future__ import annotations

import re
from typing import Callable

PUBLISHER_ID = "2979387"
IDENTIFIER = "ops@glpclinicscout.com"  # nonsecret account identifier
DIRECTORY = f"https://ui.awin.com/awin/affiliate/{PUBLISHER_ID}/merchant-directory"
PRELOGIN = "https://ui.awin.com/idp/us/awin/login/prelogin"
PASSWORD_ORIGIN = "https://id.awin.com"


class LoginBoundaryError(RuntimeError):
    """Only fixed error codes; never include browser exceptions or form values."""


def _field(js: Callable, selector: str) -> dict:
    return js("""(() => {const e=document.querySelector(%r);
      if(!e) return {visible:false};
      const r=e.getBoundingClientRect(),s=getComputedStyle(e);
      return {visible:r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'&&!e.disabled,
              x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};
    })()""" % selector)


def prepare(page_info: Callable, js: Callable, click_at_xy: Callable,
            cdp: Callable, wait_for_load: Callable) -> dict:
    """Click and insert the nonsecret identifier; stop at the vault boundary."""
    if not str(page_info().get("url", "")).startswith(PRELOGIN):
        raise LoginBoundaryError("expected_ui_awin_prelogin")
    field = _field(js, "input#email")
    if not field.get("visible"):
        raise LoginBoundaryError("identifier_controls_not_visible")
    click_at_xy(field["x"], field["y"])
    cdp("Input.insertText", text=IDENTIFIER)
    if js("document.querySelector('input#email')?.value===%r" % IDENTIFIER) is not True:
        raise LoginBoundaryError("identifier_not_committed")
    button = _field(js, "button#login")
    if not button.get("visible"):
        raise LoginBoundaryError("identifier_submit_not_enabled")
    js("document.querySelector('button#login').click()")
    wait_for_load()
    if not str(page_info().get("url", "")).startswith(PASSWORD_ORIGIN + "/"):
        raise LoginBoundaryError("password_origin_not_reached")
    if not _field(js, "input[type='password']").get("visible"):
        raise LoginBoundaryError("password_field_not_visible")
    return {"stage": "vault_fill_required", "origin": PASSWORD_ORIGIN,
            "publisher_id": PUBLISHER_ID, "secret_printed": False}


def finish(page_info: Callable, js: Callable, wait_for_load: Callable,
           goto_url: Callable) -> dict:
    """Submit only a vault-filled field; read back exact publisher directory."""
    if not str(page_info().get("url", "")).startswith(PASSWORD_ORIGIN + "/"):
        raise LoginBoundaryError("password_origin_mismatch")
    if not _field(js, "input[type='password']").get("visible"):
        raise LoginBoundaryError("password_field_not_visible")
    # Boolean only: never inspect or return the secret value.
    if js("!!document.querySelector('input[type=password]')?.value.length") is not True:
        raise LoginBoundaryError("vault_fill_not_observed")
    if not _field(js, "button[type='submit']").get("visible"):
        raise LoginBoundaryError("password_submit_not_visible")
    js("document.querySelector(\"button[type='submit']\").click()")
    wait_for_load()
    url = str(page_info().get("url", ""))
    if url.startswith(PASSWORD_ORIGIN + "/"):
        raise LoginBoundaryError("password_step_not_completed_or_challenge")
    if not url.startswith("https://ui.awin.com/"):
        raise LoginBoundaryError("unexpected_post_login_origin")
    goto_url(DIRECTORY)
    wait_for_load()
    if not str(page_info().get("url", "")).startswith(DIRECTORY):
        raise LoginBoundaryError("directory_readback_required")
    body = str(js("document.body?.innerText||''"))
    if not re.search(r"Tilton Group\s*\(\s*2979387\s*\)", body):
        raise LoginBoundaryError("publisher_identity_not_verified")
    return {"authenticated": True, "publisher_id": PUBLISHER_ID,
            "account_name": "Tilton Group", "directory_url": DIRECTORY,
            "secret_printed": False, "provider_writes": 0, "tracking_clicks": 0}
