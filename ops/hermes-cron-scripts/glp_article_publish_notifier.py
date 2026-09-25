#!/usr/bin/env python3
"""Machine-side Discord notification for every newly published GLP article.

Bridge build 2026-08-13 (operator: "i want a notification in discord whenever a
new seo article is published with a link to it"). Design: a sitemap-diff
watcher, deliberately independent of the publish scripts — it observes the
LIVE site, so it notifies for every publish path (daily carrier, waves,
optimizer applies, hand deploys) and can never be broken by publish-internals
refactors or hash pins.

Behavior: every run fetches the live sitemap, diffs content routes against a
route ledger, verifies each new route serves HTTP 200, posts ONE #decisions
card per new route (title + live URL), and appends the ledger. First run seeds
the ledger silently so the existing corpus never floods the channel. A route is
never announced twice. Failures exit nonzero (loud), success is quiet.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import urllib.request
from html import unescape
from pathlib import Path

SITE = "https://glpclinicscout.com"
UA = {"User-Agent": "glp-publish-notifier/1.0"}
LEDGER = Path("/home/ubuntu/business/reports/glp-clinic-scout/seo-machine/published-route-ledger.json")
DECISIONS = "1524254375453786294"
TOKEN_PATH = Path("/home/ubuntu/.hermes/secrets/fable-discord/bot_token")
CONTENT_PREFIXES = ("/guides/", "/best/", "/reviews/", "/compare/", "/tools/", "/quiz/")


def fetch(url: str, timeout: int = 45) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def head_ok(path: str) -> bool:
    req = urllib.request.Request(SITE + path, headers=UA, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def page_title(path: str) -> str:
    try:
        html = fetch(SITE + path)
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        return unescape(m.group(1)).strip()[:200] if m else path
    except Exception:
        return path


def post_card(route: str, title: str) -> str:
    token = TOKEN_PATH.read_text().strip()
    content = (f"📝 **New article published** — {title}\n"
               f"{SITE}{route}\n"
               f"-# autonomous SEO machine · glpclinicscout.com")
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{DECISIONS}/messages",
        data=json.dumps({"content": content}).encode(),
        headers={"Authorization": "Bot " + token, "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://discordapp.com, 10)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return str(json.load(r).get("id"))


def main() -> int:
    sitemap = fetch(SITE + "/sitemap.xml")
    routes = sorted({m for m in re.findall(
        r"<loc>https://glpclinicscout\.com(/[^<]+)</loc>", sitemap)
        if m.startswith(CONTENT_PREFIXES)})
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not LEDGER.is_file():
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps({
            "schema": "glp.published_route_ledger.v1", "seeded_at_utc": now,
            "routes": {r: {"seen_at_utc": now, "announced": False,
                           "seed": True} for r in routes},
        }, indent=1, sort_keys=True) + "\n")
        return 0
    ledger = json.loads(LEDGER.read_text())
    known = ledger.setdefault("routes", {})
    announced = 0
    for route in routes:
        if route in known:
            continue
        if not head_ok(route):
            # not fully live yet; leave unknown so the next run retries
            continue
        title = page_title(route)
        message_id = post_card(route, title)
        known[route] = {"seen_at_utc": now, "announced": True,
                        "message_id": message_id, "title": title}
        announced += 1
    ledger["last_run_utc"] = now
    LEDGER.write_text(json.dumps(ledger, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
