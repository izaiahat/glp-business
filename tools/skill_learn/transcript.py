#!/usr/bin/env python3
"""YouTube transcript fetch that works from this host (verified 2026-09-07 23:20Z).

Why this shape: this server's egress IP is bot-walled by YouTube. yt-dlp (all player
clients, every stored browser-profile cookie jar), youtube_transcript_api, a real
Chromium via Playwright (headless and headed), Invidious and Piped mirrors all fail or
return HTML error pages. Firecrawl's browser engine (its own egress) loads the watch
page; we expand the description, click "Show transcript", wait, and read the expanded
transcript engagement panel's innerText via executeJavascript. Parsing innerText (not
segment tag names) survives YouTube's periodic renderer renames. The caption-file URL in
ytInitialPlayerResponse is IP-bound and needs a PO token, so it is NOT used.

Cost: one Firecrawl scrape with actions per video (~60–120 s). Key: FIRECRAWL_API_KEY in
~/.hermes/.env (never printed). Output: plain text, one segment per line, order preserved.

Usage:  transcript.py <video_id> [more ids...]  -> JSON lines {id, ok, words, segments, source, error}
Module: fetch_transcript(video_id) -> dict(ok, text, words, segments, source, error)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

FIRECRAWL_URL = "https://api.firecrawl.dev/v1/scrape"
ENV_PATH = os.path.expanduser("~/.hermes/.env")

JS_EXPAND = '(()=>{const e=document.querySelector("tp-yt-paper-button#expand"); if(e) e.click(); return "expanded:"+!!e;})()'
JS_CLICK = ('(()=>{const b=[...document.querySelectorAll("button")].find(x=>(x.getAttribute("aria-label")||"").includes("Show transcript")'
            '||(x.textContent||"").includes("Show transcript")); if(b){b.click(); return "clicked"} return "no-button"})()')
JS_READ = ('(()=>{const ps=[...document.querySelectorAll("ytd-engagement-panel-section-list-renderer")];'
           'const p=ps.find(x=>(x.getAttribute("visibility")||"")==="ENGAGEMENT_PANEL_VISIBILITY_EXPANDED"&&((x.getAttribute("target-id")||"").includes("transcript")||(x.innerText||"").startsWith("Transcript")))'
           '||ps.find(x=>(x.innerText||"").startsWith("Transcript"));'
           'const segs=document.querySelectorAll("ytd-transcript-segment-renderer, [class*=segment-text]").length;'
           'return JSON.stringify({found:!!p, segs, text: p ? p.innerText : ""})})()')

TS_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
A11Y_RE = re.compile(r"^\d+ (second|seconds|minute|minutes|hour|hours)(,? \d+ (second|seconds|minute|minutes))*$")
HEADER_LINES = {"Transcript", "Search transcript", "Show transcript", "In this video", "Language", "English", "English (auto-generated)"}


def _key() -> str:
    for line in open(ENV_PATH):
        if line.startswith("FIRECRAWL_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("FIRECRAWL_API_KEY missing in ~/.hermes/.env")


def _scrape(video_id: str, settle_ms: int, timeout_ms: int = 230000) -> dict:
    body = {
        "url": f"https://www.youtube.com/watch?v={video_id}&hl=en",
        "formats": ["html"],
        "timeout": timeout_ms,
        "actions": [
            {"type": "wait", "milliseconds": 5000},
            {"type": "executeJavascript", "script": JS_EXPAND},
            {"type": "wait", "milliseconds": 2000},
            {"type": "executeJavascript", "script": JS_CLICK},
            {"type": "wait", "milliseconds": settle_ms},
            {"type": "executeJavascript", "script": JS_READ},
        ],
    }
    req = urllib.request.Request(FIRECRAWL_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + _key(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_ms / 1000 + 60) as r:
        return json.load(r)


def parse_panel_text(text: str) -> list[str]:
    """Turn the transcript panel's innerText into ordered segment strings 'm:ss text'."""
    lines = [l.strip() for l in text.splitlines()]
    segs: list[str] = []
    ts = None
    for l in lines:
        if not l or l in HEADER_LINES:
            continue
        if TS_RE.match(l):
            ts = l
            continue
        if A11Y_RE.match(l):
            continue
        if l.startswith("Search transcript") or l.startswith("Tap on a") or l.startswith("Show chapters") or l.startswith("Toggle timestamps"):
            continue
        segs.append(f"{ts} {l}" if ts else l)
        ts = None
    return segs


def fetch_transcript(video_id: str, retries: int = 2) -> dict:
    last_err = None
    for attempt in range(retries + 1):
        try:
            doc = _scrape(video_id, settle_ms=8000 + 5000 * attempt)
            if not doc.get("success"):
                last_err = str(doc.get("error"))[:200]
            else:
                data = doc.get("data") or {}
                html = data.get("html") or ""
                if re.search(r"confirm you.re not a bot", html):
                    last_err = "bot_wall_at_firecrawl"
                else:
                    rets = (data.get("actions") or {}).get("javascriptReturns") or []
                    read = None
                    clicked = None
                    for r in rets:
                        v = r.get("value")
                        if isinstance(v, str) and v.startswith('{"found"'):
                            read = json.loads(v)
                        elif v in ("clicked", "no-button"):
                            clicked = v
                    if read and read.get("found") and read.get("text"):
                        segs = parse_panel_text(read["text"])
                        if len(segs) >= 3:
                            text = "\n".join(segs)
                            return {"ok": True, "text": text, "words": len(re.sub(r"^\S+ ", "", text, flags=re.M).split()),
                                    "segments": len(segs), "source": "firecrawl_js_transcript_panel", "error": None}
                        last_err = f"panel_found_but_thin({len(segs)})"
                    else:
                        last_err = "no_transcript_button" if clicked == "no-button" else "panel_not_rendered"
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {str(e)[:160]}"
        time.sleep(3 * (attempt + 1))
    return {"ok": False, "text": None, "words": 0, "segments": 0, "source": None, "error": last_err}


if __name__ == "__main__":
    for vid in sys.argv[1:]:
        r = fetch_transcript(vid)
        print(json.dumps({"id": vid, "ok": r["ok"], "words": r["words"], "segments": r["segments"], "source": r["source"], "error": r["error"]}))
