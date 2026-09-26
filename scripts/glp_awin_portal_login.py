#!/usr/bin/env python3
"""Awin portal login over the persistent CDP profile — Auth0 universal login.

Operator 2026-08-18: *"AWIN IS UNLOCKED — 2FA is OFF."*

Awin fronts ui.awin.com with Auth0 (`id.awin.com/u/login/identifier`), which is a
TWO-STEP form: identifier, then password. This driver walks exactly that flow,
screenshots every step, and reports what the provider actually put on screen —
including a 2FA prompt if one appears, which it does NOT try to guess past.

Nothing here applies to a programme. Login is login; the Join mutation lives in
`glp_awin_portal_join.py` and only runs after this proves a session.

    /home/ubuntu/.hermes/venvs/glp-awin-runtime-r2/bin/python \
        scripts/glp_awin_portal_login.py --run-id <id>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")
CDP = "http://127.0.0.1:9325"
SECRET = Path.home() / ".hermes/secrets/affiliate-marketplaces/awin/ui_login.json"
PUBLISHER_ID = "2979387"
DASHBOARD = f"https://ui.awin.com/dashboard/awin/publisher/{PUBLISHER_ID}/us"
OUT = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/awin-portal-login"

TWOFA_MARKERS = ("verification code", "one-time", "authenticator", "two-factor",
                 "2-step", "enter the code", "security code", "verify your identity")
AUTHED_MARKERS = ("log out", "sign out", "publisher", "commission", "advertiser",
                  "dashboard", "my account")


def utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


class Step:
    """One captured screen. Secrets never enter a step record."""

    def __init__(self, page, run_dir: Path, name: str):
        page.wait_for_timeout(2500)
        self.name = name
        self.url = page.url
        self.title = page.title()
        try:
            self.text = (page.inner_text("body") or "")[:20000]
        except Exception:  # noqa: BLE001
            self.text = ""
        self.shot = run_dir / f"{name}.png"
        try:
            page.screenshot(path=str(self.shot), full_page=False)
        except Exception:  # noqa: BLE001
            pass
        (run_dir / f"{name}.txt").write_text(self.text)

    def record(self) -> dict:
        low = self.text.lower()
        return {"step": self.name, "url": self.url, "title": self.title,
                "text_len": len(self.text),
                "screenshot": str(self.shot.relative_to(BUSINESS)),
                "twofa_markers": [m for m in TWOFA_MARKERS if m in low],
                "authed_markers": [m for m in AUTHED_MARKERS if m in low],
                "on_login_host": bool(re.search(r"id\.awin\.com|/login|/prelogin",
                                                self.url, re.I))}


def _fill_first(page, selectors: list[str], value: str) -> str | None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.fill(value)
                return sel
        except Exception:  # noqa: BLE001
            continue
    return None


def _click_first(page, selectors: list[str]) -> str | None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click()
                return sel
        except Exception:  # noqa: BLE001
            continue
    return None


def login(run_dir: Path) -> dict:
    from playwright.sync_api import sync_playwright

    creds = json.loads(SECRET.read_text())
    email, password = creds["username"], creds["password"]
    steps, actions = [], []

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        try:
            page.goto(DASHBOARD, wait_until="domcontentloaded", timeout=60000)
            s = Step(page, run_dir, "01-entry")
            steps.append(s.record())

            if not s.record()["on_login_host"] and s.record()["authed_markers"]:
                return {"already_authenticated": True, "steps": steps, "actions": actions,
                        "final_url": page.url}

            # ---- Awin step 0: ui.awin.com's OWN prelogin form.
            # This is the step the first driver missed. `/idp/us/awin/login/prelogin`
            # is not a hand-off button — it is a real form carrying `input#email` +
            # `button#login`. Awin resolves the identifier to a network group here
            # and only THEN hands off to Auth0. Proven 2026-08-18: supplying the
            # identifier at this step resumed the profile's existing session and
            # landed straight on the dashboard, with no password and no 2FA.
            if page.locator("input#email").count():
                page.locator("input#email").first.fill(email)
                _click_first(page, ["button#login", "button[type=submit]"])
                actions.append({"prelogin_identifier": "input#email"})
                page.wait_for_timeout(7000)
                steps.append(Step(page, run_dir, "02-after-prelogin").record())

            # ---- Auth0 step 1: identifier (only when Awin escalates to id.awin.com)
            hit = None
            if (page.locator("input#username").count()
                    and page.locator("input#username").first.is_visible()):
                hit = _fill_first(page, ["input#username", "input[name=username]"], email)
            actions.append({"identifier_field": hit})
            if hit:
                _click_first(page, ["button[type=submit]", "button[name=action]",
                                    "button:has-text('Continue')", "button:has-text('Next')"])
                page.wait_for_timeout(6000)
                steps.append(Step(page, run_dir, "03-after-identifier").record())

            # ---- Auth0 step 2: password
            hit = _fill_first(page, [
                "input#password", "input[name=password]", "input[type=password]"], password)
            actions.append({"password_field": hit})
            if hit:
                _click_first(page, ["button[type=submit]", "button[name=action]",
                                    "button:has-text('Continue')", "button:has-text('Sign in')",
                                    "button:has-text('Log in')"])
                page.wait_for_timeout(9000)
                steps.append(Step(page, run_dir, "04-after-password").record())

            # ---- Land on the dashboard and record the truth
            page.goto(DASHBOARD, wait_until="domcontentloaded", timeout=60000)
            final = Step(page, run_dir, "05-dashboard")
            steps.append(final.record())
            rec = final.record()
            return {
                "already_authenticated": False,
                "authenticated": bool(rec["authed_markers"]) and not rec["on_login_host"],
                "twofa_prompted": bool(rec["twofa_markers"])
                                  or any(s["twofa_markers"] for s in steps),
                "steps": steps, "actions": actions, "final_url": page.url,
            }
        finally:
            page.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="")
    args = ap.parse_args()
    run_dir = OUT / (args.run_id or f"login-{utc()}")
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        out = login(run_dir)
    except Exception as exc:  # noqa: BLE001
        import traceback
        out = {"error": f"{type(exc).__name__}: {exc}",
               "trace": traceback.format_exc()[-2000:]}
    doc = {"schema": "awin_portal_login_v1", "cdp": CDP, "publisher_id": PUBLISHER_ID,
           "run_dir": str(run_dir.relative_to(BUSINESS)), "secret_printed": False, **out}
    (run_dir / "login.json").write_text(json.dumps(doc, indent=2, default=str))
    print(json.dumps({k: v for k, v in doc.items() if k != "steps"}, indent=2, default=str))
    for s in doc.get("steps", []):
        print(json.dumps(s, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
