#!/usr/bin/env python3
"""GLP Reddit daily human-post scout — produces a daily pack, every day.

Mission (bridge rebuild 2026-08-13): the previous scout (glp_reddit_pack_scout.py)
never emitted a single pack because unknown or unproven subreddit rules were
treated as ineligible (BLOCKED_NO_EXACT_RULE...). This scout inverts that:

  * A rule blocks a draft ONLY when it EXPLICITLY forbids self-promotion or
    links. UNKNOWN rules mean eligible-with-warning — the daily card carries the
    captured rule excerpt so the operator judges.
  * A stale or failed rules capture NEVER blocks the run; it degrades that sub
    to a warning state with the capture age attached.
  * Zero eligible threads is a valid daily outcome and still produces a pack
    payload with per-sub evidence of which rule/search excluded what.

Reused from build-3 (glp_reddit_pack_scout.py): the no-promo lint regexes, the
max-one-link discipline, the zero-effects capability block, and the pack-card
shape (compact JSON payload the canonical dispatcher can card).

Effects: Firecrawl reads only (search + scrape). Writes only under
reports/glp-clinic-scout/growth/build-3/. Never posts to Reddit or Discord,
never sends email, never touches cron. OPERATOR POSTS MANUALLY.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import statistics
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

BUSINESS = Path("/home/ubuntu/business")
BUILD3 = BUSINESS / "reports" / "glp-clinic-scout" / "growth" / "build-3"
LEDGER_PATH = BUILD3 / "rules-ledger.json"
PACKS_DIR = BUILD3 / "daily-packs"
RELEASES_DIR = BUSINESS / "site-system" / "flagship-v2" / "releases"
HERMES_ENV = Path.home() / ".hermes" / ".env"

PT = ZoneInfo("America/Los_Angeles")

SITE_DOMAIN = "glpclinicscout.com"
PRICE_INDEX_URL = "https://glpclinicscout.com/glp-1-price-index/"

# Target subs. tirzepatidecompound chosen as the most active compounding sub by
# Firecrawl past-day thread volume on 2026-08-13 (tirzepatidecompound=50
# threads/day vs compoundedtirzepatide=12); evidence stored in the rules ledger.
TARGET_SUBS = ["Mounjaro", "semaglutide", "tirzepatide", "Zepbound", "tirzepatidecompound"]
COMPOUNDING_SUB_SELECTION = {
    "chosen": "tirzepatidecompound",
    "method": "firecrawl_search_past_day_thread_count_20260813",
    "counts": {"tirzepatidecompound": 50, "compoundedtirzepatide": 12, "CompoundedSemaglutide": 23},
    "notes": "counts are /comments/ results from site:reddit.com/r/<sub> searches; "
             "past-week counts saturated the 50-result cap for both tirzepatide subs, "
             "past-day separated them decisively.",
}

RULES_MAX_AGE_DAYS = 7
THREAD_MAX_AGE_DAYS = 7
DEFAULT_MAX_THREADS = 6
DEFAULT_MAX_SCRAPES = 8
RULE_EXCERPT_LIMIT = 900
QUESTION_EXCERPT_LIMIT = 600
PACK_SOFT_LIMIT_BYTES = 48_000

# ---------------------------------------------------------------- lint (reused
# from build-3 glp_reddit_pack_scout.py, adapted for max-one-link discipline)

AFFILIATE_RE = re.compile(
    r"awin1\.com|clickbank|hop\.clickbank|go2cloud|impact\.com/.*(?:aff|click)|"
    r"[?&](?:aff(?:iliate)?|ref(?:erral)?|subid|clickref|coupon|promo(?:code)?)=|"
    r"\b(?:affiliate link|referral link|coupon code|referral code|use my code)\b",
    re.I,
)
PROMOTIONAL_RE = re.compile(
    r"\b(?:buy now|sign up (?:now|here|with)|click here|dm me|message me|our deal|"
    r"limited time|today only|use code|sponsored by)\b",
    re.I,
)
DRAFT_MEDICAL_CLAIM_RE = re.compile(
    r"\b(?:cure[sd]?|treats?|diagnos(?:e|es|ed|is)|clinically proven|doctor recommended|"
    r"safe for (?:you|everyone)|guarantee(?:d|s)?|lose \d+ (?:lb|lbs|pounds?))\b",
    re.I,
)
DRAFT_CTA_RE = re.compile(
    r"\b(?:buy now|sign up|click here|dm me|message me|use my code|limited time|today only)\b",
    re.I,
)
PROMPT_INJECTION_RE = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|system\s+prompt|"
    r"developer\s+message|<\/?(?:tool|system|assistant)|jailbreak|do\s+not\s+follow\s+the\s+rules|"
    r"(?:post|comment|vote|dm|message)\s+(?:this|it)\s+(?:to|on)\s+reddit",
    re.I,
)
MEDICAL_RISK_RE = re.compile(
    r"\b(?:what dose|increase (?:my|the) dose|dosage advice|diagnos(?:e|is|ed)|"
    r"cure|treat my|safe for me|should i take|suicid(?:e|al)|overdose|emergency|"
    r"severe (?:pain|vomiting|depression)|medical advice|prescribe|prescription advice)\b",
    re.I,
)
LINK_RE = re.compile(r"https?://[^\s)\]>\"']+", re.I)
OUR_LINK_RE = re.compile(r"^https://glpclinicscout\.com(?:/[^?#]*)?$", re.I)

# ------------------------------------------------------- rule classification

EXPLICIT_PROMO_FORBID_RE = re.compile(
    r"no\s+(?:unsolicited\s+)?(?:self.?)?promot(?:ion|ing|e)s?|"
    r"no\s+advertis(?:ing|ements?)|"
    r"spamming\s+includes\s+promoting|"
    r"do\s+not\s+(?:share|post)\s+links|"
    r"don'?t\s+(?:share|post)\s+links|"
    r"no\s+links?\s+to\s+(?:providers?|vendors?|websites?|sellers?)|"
    r"no\s+(?:vendor|provider|website)\s+links?|"
    r"links?\s+(?:to\s+\w+\s+)?(?:are\s+)?(?:not\s+(?:allowed|permitted)|prohibited|banned)|"
    r"no\s+(?:coupon|referral|affiliate|promo)\s+(?:codes?|links?)|"
    r"no\s+solicit(?:ing|ation)|"
    r"no\s+surveys|"
    r"self.?promotion\s+(?:is\s+)?(?:not\s+(?:allowed|permitted)|prohibited|banned|will\s+be\s+removed)",
    re.I,
)
COMPOUND_TOPIC_FORBID_RE = re.compile(
    r"no\s+discussions?\s+about\s+compounding|"
    r"no\s+compounding\b|"
    r"compound(?:ed|ing)?[^.\n]{0,120}(?:off.?topic|will\s+be\s+removed|not\s+allowed|prohibited|banned)|"
    r"(?:do\s+not|don'?t)\s+allow[^.\n]{0,120}compound(?:ed|ing)?|"
    r"(?:posts?|comments?)[^.\n]{0,80}compound(?:ed|ing)[^.\n]{0,80}removed",
    re.I,
)
RULES_WALL_RE = re.compile(
    r"recaptcha|verification\s+required|are\s+you\s+a\s+human|access\s+denied|please\s+log\s+in",
    re.I,
)

# ------------------------------------------------------------ intent patterns

INTENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("compounded_availability", re.compile(
        r"compound(?:ed|ing)?[^.\n]{0,80}(?:available|availability|still\s+(?:get|order|offer)|"
        r"where|who|source|carry|sell)|"
        r"(?:where|who|how)[^.\n]{0,60}(?:get|order|buy|find)[^.\n]{0,40}compound", re.I)),
    ("provider_choice", re.compile(
        r"\brecommend(?:ation)?s?\b|which\s+(?:provider|company|clinic|telehealth|pharmacy|site)|"
        r"who\s+do\s+you\s+(?:use|recommend|go)|where\s+(?:to|do|should|can)\b[^.\n]{0,50}"
        r"(?:get|order|buy|start|go)|anyone\s+(?:tried|used|heard\s+of|have\s+experience)|"
        r"switch(?:ing)?\s+(?:from|to)|reputable|legit(?:imate)?\b|good\s+(?:provider|company|"
        r"experience\s+with)|looking\s+for\s+a\s+(?:provider|company|new)", re.I)),
    ("pricing", re.compile(
        r"\b(?:price|prices|pricing|cost|costs|cheap|cheapest|afford|affordable|deal|deals|"
        r"discount|coupon|expensive|\$\d+|per\s+month|monthly\s+(?:price|cost|fee))\b", re.I)),
    ("comparison", re.compile(
        r"\bvs\.?\b|\bversus\b|\bcompar(?:e|ing|ison)s?\b|difference\s+between|better\s+than|"
        r"\bor\b[^.\n]{0,30}\bwhich\b", re.I)),
]

LINKABLE_INTENTS = {"pricing", "comparison", "compounded_availability"}

AGE_RE = re.compile(
    r"(?:^|[\s•·(])(\d+)\s*(min(?:ute)?s?|hr?s?|hours?|d(?:ay)?s?|wk?s?|weeks?|mo(?:nth)?s?|yr?s?|years?)\.?\s+ago\b",
    re.I,
)
JUST_NOW_RE = re.compile(r"\bjust\s+now\b", re.I)
THREAD_URL_RE = re.compile(
    r"^https?://(?:www\.|old\.|new\.)?reddit\.com/r/([A-Za-z0-9_]+)/comments/([a-z0-9]+)(?:/([^/?#]*))?",
    re.I,
)


# ---------------------------------------------------------------- utilities

def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return sha256_text(raw)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def pt_day(now: dt.datetime) -> str:
    return now.astimezone(PT).date().isoformat()


def clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def canonical_thread_url(url: str) -> str | None:
    match = THREAD_URL_RE.match(str(url or "").strip())
    if not match:
        return None
    sub, thread_id, slug = match.group(1), match.group(2), match.group(3) or ""
    tail = f"{slug}/" if slug else ""
    return f"https://www.reddit.com/r/{sub}/comments/{thread_id}/{tail}"


def thread_subreddit(url: str) -> str | None:
    match = THREAD_URL_RE.match(str(url or "").strip())
    return match.group(1) if match else None


# ---------------------------------------------------------------- firecrawl

def _firecrawl_key() -> str:
    for line in HERMES_ENV.read_text().splitlines():
        if line.startswith("FIRECRAWL_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
            if key:
                return key
    raise RuntimeError("firecrawl_api_key_missing: FIRECRAWL_API_KEY not in ~/.hermes/.env")


def live_search_fn() -> Callable[..., list[dict[str, Any]]]:
    key = _firecrawl_key()

    def search(query: str, *, tbs: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"query": query, "limit": int(limit)}
        if tbs:
            body["tbs"] = tbs
        request = urllib.request.Request(
            "https://api.firecrawl.dev/v1/search",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.load(response)
        return [
            {"url": row.get("url"), "title": row.get("title") or "", "description": row.get("description") or ""}
            for row in (payload.get("data") or [])
            if row.get("url")
        ]

    return search


def live_scrape_fn() -> Callable[[str], dict[str, Any]]:
    key = _firecrawl_key()

    def scrape(url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            "https://api.firecrawl.dev/v1/scrape",
            data=json.dumps({"url": url, "formats": ["markdown"], "timeout": 40000, "maxAge": 0}).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.load(response)
        except Exception as exc:  # noqa: BLE001 — carried as evidence, never fatal
            return {"status": f"error:{type(exc).__name__}", "markdown": "", "metadata": {}}
        data = payload.get("data") or {}
        markdown = str(data.get("markdown") or "")
        return {"status": "ok" if markdown else "empty", "markdown": markdown,
                "metadata": data.get("metadata") or {}}

    return scrape


# ------------------------------------------------------------- rules ledger

def rules_capture_valid(markdown: str) -> bool:
    """A capture is valid only when it carries real rule content — a reCAPTCHA
    or login wall page must NOT silently classify as COMMENT_ALLOWED (defect
    found on the 2026-08-13 first live run: r/Mounjaro + r/tirzepatide walls)."""
    text = str(markdown or "")
    if len(text) < 40:
        return False
    core = re.sub(r"\[Skip to main content\]\([^)]*\)", "", text)
    core = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", core)
    core = re.sub(
        r"(?i)recaptcha|protected\s+by|verification\.?|community\s+rules|"
        r"are\s+you\s+sure\?|rules\s+can.t\s+be\s+recovered\.?|yes,\s*delete|"
        r"discard\s+changes\?|any\s+edits\s+to\s+this\s+rule\s+will\s+be\s+lost\.?|"
        r"cancel|yes,\s*discard|[#*|]",
        "", core)
    core_len = len(re.sub(r"\s+", " ", core).strip())
    if RULES_WALL_RE.search(text) and core_len < 400:
        return False
    return core_len >= 80


def classify_rules(markdown: str, sub: str = "") -> dict[str, Any]:
    """Classify a captured rules page. UNKNOWN is NEVER ineligible."""
    text = str(markdown or "").strip()
    if not text or len(text) < 40:
        return {
            "eligibility_state": "UNKNOWN_ELIGIBLE_WITH_WARNING",
            "link_policy": "allowed_with_warning",
            "matched_rule_excerpts": [],
            "topic_restrictions": [],
            "warnings": ["rules_not_captured_operator_judges"],
        }
    matched: list[str] = []
    for match in EXPLICIT_PROMO_FORBID_RE.finditer(text):
        start = max(0, match.start() - 120)
        matched.append(clip(text[start: match.end() + 160].replace("\n", " "), 320))
    topics: list[str] = []
    if "compound" not in sub.lower():
        # A compounding-focused sub's "no compounding sources" style rules are
        # scope rules, not a ban on the sub's own topic — skip detection there.
        for match in COMPOUND_TOPIC_FORBID_RE.finditer(text):
            start = max(0, match.start() - 120)
            topics.append(clip(text[start: match.end() + 160].replace("\n", " "), 320))
    if matched:
        return {
            "eligibility_state": "COMMENT_ALLOWED_NO_PROMO",
            "link_policy": "forbidden",
            "matched_rule_excerpts": matched[:4],
            "topic_restrictions": topics[:3],
            "warnings": [],
        }
    return {
        "eligibility_state": "COMMENT_ALLOWED",
        "link_policy": "allowed",
        "matched_rule_excerpts": [],
        "topic_restrictions": topics[:3],
        "warnings": [],
    }


def load_ledger() -> dict[str, Any]:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"schema": "glp-reddit-rules-ledger-v1", "updated_utc": None, "subs": {},
            "compounding_sub_selection": COMPOUNDING_SUB_SELECTION}


def entry_age_days(entry: dict[str, Any], now: dt.datetime) -> float | None:
    captured = entry.get("capture_utc")
    if not captured:
        return None
    try:
        then = dt.datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - then).total_seconds() / 86400.0


def refresh_rules(
    ledger: dict[str, Any],
    scrape: Callable[[str], dict[str, Any]],
    *,
    now: dt.datetime,
    subs: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Capture/refresh rules for target subs. Failures degrade, never block."""
    ledger.setdefault("subs", {})
    ledger["compounding_sub_selection"] = COMPOUNDING_SUB_SELECTION
    for sub in subs or TARGET_SUBS:
        entry = ledger["subs"].get(sub) or {}
        age = entry_age_days(entry, now)
        stored_markdown = str(entry.get("markdown_excerpt") or "")
        stored_valid = entry.get("status") == "CAPTURED" and rules_capture_valid(stored_markdown)
        if not force and stored_valid and age is not None and age <= RULES_MAX_AGE_DAYS:
            # Re-classify from the stored capture so classifier fixes propagate
            # to the ledger without a network refresh.
            entry = dict(entry)
            entry.update(classify_rules(stored_markdown, sub))
            ledger["subs"][sub] = entry
            continue
        rules_url = f"https://www.reddit.com/r/{sub}/about/rules/"
        markdown, last_status = "", "unattempted"
        for _attempt in range(2):  # walls/403s are flaky — one retry is cheap
            result = scrape(rules_url)
            markdown = str(result.get("markdown") or "")
            last_status = str(result.get("status"))
            if last_status == "ok" and rules_capture_valid(markdown):
                break
        if last_status == "ok" and rules_capture_valid(markdown):
            classification = classify_rules(markdown, sub)
            ledger["subs"][sub] = {
                "rules_url": rules_url,
                "status": "CAPTURED",
                "capture_utc": now.isoformat(),
                "markdown_excerpt": clip(markdown, 6000),
                "rule_excerpt_short": clip(markdown, RULE_EXCERPT_LIMIT),
                **classification,
            }
        else:
            if last_status == "ok":
                stale_note = "rules_capture_walled_or_empty"
            else:
                stale_note = f"rules_capture_failed:{last_status}"
            if stored_valid:  # keep a previously VALID capture, degraded
                entry = dict(entry)
                entry.setdefault("warnings", [])
                if stale_note not in entry["warnings"]:
                    entry["warnings"] = [*entry["warnings"], stale_note]
                entry["last_refresh_attempt_utc"] = now.isoformat()
                ledger["subs"][sub] = entry
            else:
                ledger["subs"][sub] = {
                    "rules_url": rules_url,
                    "status": "CAPTURE_FAILED",
                    "capture_utc": now.isoformat(),
                    "markdown_excerpt": "",
                    "rule_excerpt_short": "",
                    **classify_rules("", sub),
                }
                ledger["subs"][sub]["warnings"] = [stale_note, "rules_not_captured_operator_judges"]
    ledger["updated_utc"] = now.isoformat()
    return ledger


def sub_state(ledger: dict[str, Any], sub: str, now: dt.datetime) -> dict[str, Any]:
    entry = ledger.get("subs", {}).get(sub)
    if not entry:
        state = classify_rules("", sub)
        return {"subreddit": sub, "capture_utc": None, "capture_age_days": None,
                "rule_excerpt": "", **state}
    age = entry_age_days(entry, now)
    warnings = list(entry.get("warnings") or [])
    if age is not None and age > RULES_MAX_AGE_DAYS:
        warnings.append(f"rules_capture_stale_{age:.0f}d_operator_judges")
    excerpt = entry.get("rule_excerpt_short") or ""
    if entry.get("matched_rule_excerpts"):
        excerpt = " | ".join(entry["matched_rule_excerpts"])
    return {
        "subreddit": sub,
        "capture_utc": entry.get("capture_utc"),
        "capture_age_days": round(age, 1) if age is not None else None,
        "eligibility_state": entry.get("eligibility_state", "UNKNOWN_ELIGIBLE_WITH_WARNING"),
        "link_policy": entry.get("link_policy", "allowed_with_warning"),
        "matched_rule_excerpts": entry.get("matched_rule_excerpts") or [],
        "topic_restrictions": entry.get("topic_restrictions") or [],
        "rule_excerpt": clip(excerpt, RULE_EXCERPT_LIMIT),
        "warnings": warnings,
    }


# ----------------------------------------------------------- release data

def newest_release_data_dir() -> Path:
    candidates = [
        path for path in RELEASES_DIR.iterdir()
        if path.is_dir() and (path / "data" / "price-tracker.json").exists()
    ]
    if not candidates:
        raise RuntimeError(f"no_release_data_dir_with_price_tracker_under:{RELEASES_DIR}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_release_facts(release_dir: Path | None = None) -> dict[str, Any]:
    release_dir = release_dir or newest_release_data_dir()
    tracker = json.loads((release_dir / "data" / "price-tracker.json").read_text())
    price_index = tracker.get("price_index") or {}
    records = price_index.get("records") or []
    observations = price_index.get("observations") or []
    data_as_of = None
    if observations:
        data_as_of = observations[-1].get("data_as_of") or observations[-1].get("observed_on")
    stats: dict[str, dict[str, Any]] = {}
    for medication in sorted({row.get("medication") for row in records if row.get("medication")}):
        amounts = sorted(
            float(row["amount_usd"]) for row in records
            if row.get("medication") == medication and isinstance(row.get("amount_usd"), (int, float))
        )
        if amounts:
            stats[medication] = {
                "providers": len(amounts),
                "min_usd": amounts[0],
                "max_usd": amounts[-1],
                "median_usd": float(statistics.median(amounts)),
            }
    provider_names: dict[str, str] = {}
    registry_path = release_dir / "data" / "program-registry.json"
    if registry_path.exists():
        registry = json.loads(registry_path.read_text())
        for program in registry.get("programs") or []:
            pid, name = program.get("program_id"), program.get("display_name")
            if pid and name:
                provider_names[pid] = name
    provider_prices: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        pid = row.get("provider_id")
        if pid:
            provider_prices.setdefault(pid, []).append(row)
    return {
        "release_dir": release_dir.name,
        "data_as_of": data_as_of,
        "stats": stats,
        "provider_names": provider_names,
        "provider_prices": provider_prices,
    }


# ------------------------------------------------------------ thread finder

def detect_intent(text: str) -> str | None:
    for intent, pattern in INTENT_PATTERNS:
        if pattern.search(text or ""):
            return intent
    return None


def parse_age_days(markdown: str) -> float | None:
    text = str(markdown or "")
    if JUST_NOW_RE.search(text[:4000]):
        return 0.0
    match = AGE_RE.search(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith(("min", "m")) and not unit.startswith("mo"):
        return 0.0
    if unit.startswith(("hr", "hour", "h")):
        return value / 24.0
    if unit.startswith("d"):
        return value
    if unit.startswith("w"):
        return value * 7.0
    if unit.startswith("mo"):
        return value * 30.0
    if unit.startswith(("yr", "year", "y")):
        return value * 365.0
    return None


def previously_packed_urls(packs_dir: Path = PACKS_DIR, *, exclude_day: str | None = None) -> set[str]:
    """URLs already carded on PREVIOUS days. Today's own pack is excluded so a
    same-day rerun regenerates (not dedupes-away) today's threads."""
    seen: set[str] = set()
    if not packs_dir.exists():
        return seen
    for path in sorted(packs_dir.glob("*.json")):
        if exclude_day and path.name == f"{exclude_day}.json":
            continue
        try:
            pack = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for thread in pack.get("threads") or []:
            url = thread.get("url")
            if url:
                seen.add(url)
    return seen


def find_candidates(
    search: Callable[..., list[dict[str, Any]]],
    *,
    subs: list[str],
    already_packed: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run per-sub past-week searches; return candidates + per-sub evidence."""
    evidence: dict[str, Any] = {}
    candidates: dict[str, dict[str, Any]] = {}
    for sub in subs:
        queries = [
            f"site:reddit.com/r/{sub} (recommend OR recommendation OR \"which provider\" OR "
            f"\"who do you use\" OR \"anyone tried\" OR switch)",
            f"site:reddit.com/r/{sub} (price OR cost OR cheapest OR afford OR compare)",
        ]
        sub_rows: list[dict[str, Any]] = []
        errors: list[str] = []
        for query in queries:
            try:
                sub_rows.extend(search(query, tbs="qdr:w", limit=20))
            except Exception as exc:  # noqa: BLE001 — carried as evidence
                errors.append(f"{type(exc).__name__}: {exc}")
        excluded: dict[str, int] = {}

        def count(reason: str) -> None:
            excluded[reason] = excluded.get(reason, 0) + 1

        kept = 0
        for row in sub_rows:
            url = canonical_thread_url(row.get("url") or "")
            if not url:
                count("not_a_thread_url")
                continue
            row_sub = thread_subreddit(url) or ""
            if row_sub.lower() != sub.lower():
                count("not_target_sub")
                continue
            if url in candidates:
                count("duplicate_result")
                continue
            if url in already_packed:
                count("already_packed_previous_day")
                continue
            text = f"{row.get('title') or ''} {row.get('description') or ''}"
            if PROMPT_INJECTION_RE.search(text):
                count("prompt_injection_suspected")
                continue
            if AFFILIATE_RE.search(text):
                count("affiliate_or_referral_content")
                continue
            if MEDICAL_RISK_RE.search(text):
                count("medical_advice_or_crisis_content")
                continue
            if SITE_DOMAIN in text.lower():
                count("mentions_our_site")
                continue
            intent = detect_intent(text)
            if not intent:
                count("no_active_question_intent")
                continue
            candidates[url] = {
                "url": url,
                "subreddit": sub,
                "title": clip(re.sub(r"\s*:\s*r/\w+\s*$", "", str(row.get("title") or "")), 300),
                "search_snippet": clip(row.get("description") or "", QUESTION_EXCERPT_LIMIT),
                "detected_intent": intent,
            }
            kept += 1
        evidence[sub] = {
            "queries": queries,
            "search_window": "past_week (tbs=qdr:w)",
            "result_count": len(sub_rows),
            "candidate_count": kept,
            "excluded_counts": excluded,
            "errors": errors,
        }
    intent_rank = {"provider_choice": 0, "pricing": 1, "compounded_availability": 2, "comparison": 3}
    ordered = sorted(candidates.values(), key=lambda row: intent_rank.get(row["detected_intent"], 9))
    return ordered, evidence


def enrich_candidate(candidate: dict[str, Any], scrape: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """Scrape the thread for freshness + fuller question text. Never blocks."""
    result = scrape(candidate["url"])
    markdown = str(result.get("markdown") or "")
    if result.get("status") == "ok" and markdown:
        age = parse_age_days(markdown)
        candidate["age_days"] = age
        candidate["freshness_basis"] = (
            "thread_page_relative_age" if age is not None else "firecrawl_search_window_past_week"
        )
        body = extract_question(markdown, candidate.get("title") or "")
        if body:
            candidate["question_excerpt"] = clip(body, QUESTION_EXCERPT_LIMIT)
            candidate["question_source"] = "thread_page"
    else:
        candidate["age_days"] = None
        candidate["freshness_basis"] = "firecrawl_search_window_past_week"
        candidate["scrape_status"] = result.get("status")
    candidate.setdefault("question_excerpt", candidate.get("search_snippet") or "")
    candidate.setdefault("question_source", "search_snippet")
    return candidate


def extract_question(markdown: str, title: str) -> str:
    """Best-effort extraction of the post body from thread markdown."""
    text = str(markdown or "")
    anchor = 0
    if title:
        found = text.find(title[:60])
        if found >= 0:
            anchor = found + len(title[:60])
    window = text[anchor: anchor + 4000]
    lines = []
    for line in window.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("[", "!", "#", "|", "Sort by", "Log in", "Open menu", "Skip to")):
            continue
        if re.match(r"^\d+\s+(?:votes?|comments?|upvotes?)", stripped, re.I):
            continue
        if AGE_RE.search(stripped) and len(stripped) < 60:
            continue
        lines.append(stripped)
        if sum(len(part) for part in lines) > 900:
            break
    return " ".join(lines).strip()


# ------------------------------------------------------------- draft builder

def _fmt_usd(value: float) -> str:
    return f"${value:,.0f}"


def _medication_for_sub(sub: str, text: str) -> str:
    sub_lowered = sub.lower()
    if any(token in sub_lowered for token in ("tirzepatide", "mounjaro", "zepbound")):
        return "tirzepatide"
    if any(token in sub_lowered for token in ("semaglutide", "ozempic", "wegovy")):
        return "semaglutide"
    lowered = str(text or "").lower()
    if any(token in lowered for token in ("tirzepatide", "mounjaro", "zepbound")):
        return "tirzepatide"
    return "semaglutide"


def provider_fact(candidate: dict[str, Any], facts: dict[str, Any]) -> str | None:
    text = f"{candidate.get('title') or ''} {candidate.get('question_excerpt') or ''}".lower()
    for pid, name in facts.get("provider_names", {}).items():
        if len(name) < 3 or name.lower() not in text:
            continue
        rows = facts.get("provider_prices", {}).get(pid) or []
        for row in rows:
            amount = row.get("amount_usd")
            if isinstance(amount, (int, float)):
                return (
                    f"For {name} specifically, the lowest explicit monthly price we track is "
                    f"{_fmt_usd(float(amount))} for {row.get('medication', 'GLP-1')} "
                    f"({row.get('price_phase', 'starting')} price)."
                )
    return None


CHECKLIST = (
    "Whatever provider you compare, get the all-in number: whether medication, provider fees or "
    "membership, labs, and shipping are included, how the price changes as the dose increases, "
    "and what cancellation requires."
)
DISCLOSURE = "Disclosure: I help run GLP Clinic Scout, a price comparison site for GLP-1 telehealth providers."


def build_draft(candidate: dict[str, Any], sub_info: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    intent = candidate.get("detected_intent") or "provider_choice"
    link_policy = sub_info.get("link_policy", "allowed_with_warning")
    compound_restricted = bool(sub_info.get("topic_restrictions"))
    medication = _medication_for_sub(candidate.get("subreddit", ""),
                                     f"{candidate.get('title')} {candidate.get('question_excerpt')}")
    stats = facts.get("stats", {}).get(medication)
    data_as_of = facts.get("data_as_of") or "recently"
    parts: list[str] = []
    basis: str

    if compound_restricted:
        basis = "branded_general_guidance"
        parts.append(
            "Since this sub keeps compounding off-topic, sticking to the brand-name side: the biggest "
            "price differences usually come from whether you qualify for the manufacturer savings card, "
            "what your insurance requires for prior authorization, and the pharmacy's cash price with "
            "and without the card - it is worth pricing all three before committing."
        )
        parts.append(CHECKLIST)
    elif stats:
        basis = "compounded_price_index"
        openers = {
            "provider_choice": (
                "Rather than one recommendation, it might help to see the actual spread - "
                "the providers differ far more on price and terms than most people expect."
            ),
            "comparison": "On the price dimension at least, here is the current spread.",
            "compounded_availability": (
                f"Plenty of telehealth providers still offer compounded {medication}, "
                "and their published prices vary a lot."
            ),
        }
        opener = openers.get(intent)
        if opener:
            parts.append(opener)
        parts.append(
            f"Across the {stats['providers']} telehealth providers we track that publish an explicit "
            f"monthly price for compounded {medication}, starting prices run "
            f"{_fmt_usd(stats['min_usd'])} to {_fmt_usd(stats['max_usd'])} a month with a median around "
            f"{_fmt_usd(stats['median_usd'])} (as of {data_as_of}). The advertised number is usually the "
            "first-month or lowest-dose price, so the spread between providers gets wider at higher doses."
        )
        fact = provider_fact(candidate, facts)
        if fact:
            parts.append(fact)
        parts.append(CHECKLIST)
    else:
        basis = "general_guidance"
        parts.append(CHECKLIST)

    include_link = (
        intent in LINKABLE_INTENTS
        and link_policy in ("allowed", "allowed_with_warning")
        and basis == "compounded_price_index"
    )
    if include_link:
        parts.append(
            f"We keep a free index of every tracked provider's current published price at "
            f"{PRICE_INDEX_URL} if that helps with the comparison."
        )
    parts.append(DISCLOSURE)
    text = " ".join(parts)
    return {
        "text": text,
        "basis": basis,
        "includes_link": include_link,
        "link_count": len(LINK_RE.findall(text)),
        "sha256": sha256_text(text),
    }


# --------------------------------------------------------------------- lint

def lint_draft(text: str, *, link_policy: str) -> list[str]:
    """No-promo lint, adapted from build-3: max ONE link, our domain only."""
    reasons: list[str] = []
    value = str(text or "").strip()
    words = len(value.split())
    if words < 40:
        reasons.append("draft_too_short_not_self_contained")
    if len(value) > 1600 or words > 240:
        reasons.append("draft_overflow")
    links = LINK_RE.findall(value)
    if len(links) > 1:
        reasons.append("multiple_links")
    for link in links:
        if not OUR_LINK_RE.match(link.rstrip(".,;")):
            reasons.append("external_or_tracking_link_present")
    if links and link_policy == "forbidden":
        reasons.append("link_forbidden_by_sub_rule")
    if AFFILIATE_RE.search(value):
        reasons.append("affiliate_surface_present")
    if PROMOTIONAL_RE.search(value) or DRAFT_CTA_RE.search(value):
        reasons.append("promotional_cta_present")
    if DRAFT_MEDICAL_CLAIM_RE.search(value):
        reasons.append("medical_claim_present")
    lowered = value.lower()
    if "glp clinic scout" not in lowered or not any(
        marker in lowered for marker in ("disclosure", "affiliated", "i help run")
    ):
        reasons.append("affiliation_disclosure_missing")
    return sorted(set(reasons))


# --------------------------------------------------------------------- pack

ZERO_EFFECTS = {
    "reddit_posts": 0, "reddit_comments": 0, "reddit_votes": 0, "reddit_dms": 0,
    "discord_posts": 0, "emails_sent": 0, "cron_changes": 0, "paid_provider_writes": 0,
}
POSTING_BOUNDARY = (
    "OPERATOR POSTS MANUALLY from their own account after reading the live thread and the sub's "
    "current rules. This pack never posts, comments, votes, messages, or logs in to Reddit."
)


def build_pack(
    *,
    now: dt.datetime,
    ledger: dict[str, Any],
    facts: dict[str, Any],
    threads: list[dict[str, Any]],
    search_evidence: dict[str, Any],
    subs: list[str],
) -> dict[str, Any]:
    sub_states = {sub: sub_state(ledger, sub, now) for sub in subs}
    drafted = [thread for thread in threads if thread.get("lint_pass")]
    status = "PACK_READY" if drafted else "NO_ELIGIBLE_THREADS"
    card_lines = [
        f"Reddit daily pack {pt_day(now)} - {len(drafted)} draft(s) ready across "
        f"{len({t['subreddit'] for t in drafted}) if drafted else 0} sub(s).",
    ]
    for thread in drafted:
        state = sub_states.get(thread["subreddit"], {})
        card_lines.append(
            f"r/{thread['subreddit']} [{state.get('eligibility_state', '?')}] "
            f"{thread['title']} -> {thread['url']}"
        )
    if not drafted:
        card_lines.append("Zero eligible threads today; per-sub evidence attached (rule + search exclusions).")
    pack = {
        "schema": "glp-reddit-daily-pack-v1",
        "pack_date_pt": pt_day(now),
        "generated_utc": now.isoformat(),
        "status": status,
        "posting_boundary": POSTING_BOUNDARY,
        "zero_effects": dict(ZERO_EFFECTS),
        "data_source": {
            "release_dir": facts.get("release_dir"),
            "data_as_of": facts.get("data_as_of"),
            "price_stats": facts.get("stats"),
        },
        "rules_ledger": {
            "path": str(LEDGER_PATH.relative_to(BUSINESS)),
            "updated_utc": ledger.get("updated_utc"),
            "subs": sub_states,
        },
        "search_evidence": search_evidence,
        "threads": threads,
        "counts": {
            "candidate_threads": len(threads),
            "drafts_ready": len(drafted),
            "lint_failed": sum(1 for thread in threads if thread.get("lint_pass") is False),
        },
        "card_summary": "\n".join(card_lines),
    }
    return pack


def shrink_pack(pack: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(pack, ensure_ascii=False)
    if len(raw.encode()) <= PACK_SOFT_LIMIT_BYTES:
        return pack
    for sub in pack.get("rules_ledger", {}).get("subs", {}).values():
        sub["rule_excerpt"] = clip(sub.get("rule_excerpt", ""), 300)
        sub["matched_rule_excerpts"] = [clip(x, 200) for x in sub.get("matched_rule_excerpts", [])][:2]
    for thread in pack.get("threads", []):
        thread["question_excerpt"] = clip(thread.get("question_excerpt", ""), 300)
    return pack


# --------------------------------------------------------------------- main

def run(
    *,
    search: Callable[..., list[dict[str, Any]]],
    scrape: Callable[[str], dict[str, Any]],
    now: dt.datetime | None = None,
    subs: list[str] | None = None,
    max_threads: int = DEFAULT_MAX_THREADS,
    max_scrapes: int = DEFAULT_MAX_SCRAPES,
    refresh_rules_force: bool = False,
    packs_dir: Path = PACKS_DIR,
    ledger_path: Path = LEDGER_PATH,
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    subs = subs or TARGET_SUBS
    facts = facts or load_release_facts()

    ledger = load_ledger() if ledger_path == LEDGER_PATH else (
        json.loads(ledger_path.read_text()) if ledger_path.exists()
        else {"schema": "glp-reddit-rules-ledger-v1", "updated_utc": None, "subs": {}}
    )
    ledger = refresh_rules(ledger, scrape, now=now, subs=subs, force=refresh_rules_force)
    atomic_write_json(ledger_path, ledger)

    already = previously_packed_urls(packs_dir, exclude_day=pt_day(now))
    candidates, search_evidence = find_candidates(search, subs=subs, already_packed=already)

    all_errored = subs and all(
        search_evidence[sub]["errors"] and search_evidence[sub]["result_count"] == 0 for sub in subs
    )
    if all_errored:
        errors = {sub: search_evidence[sub]["errors"] for sub in subs}
        raise RuntimeError(f"all_sub_searches_failed_no_pack_written: {json.dumps(errors)}")

    threads: list[dict[str, Any]] = []
    scrapes_used = 0
    for candidate in candidates:
        if len(threads) >= max_threads:
            break
        if scrapes_used < max_scrapes:
            candidate = enrich_candidate(candidate, scrape)
            scrapes_used += 1
        else:
            candidate.setdefault("age_days", None)
            candidate.setdefault("freshness_basis", "firecrawl_search_window_past_week")
            candidate.setdefault("question_excerpt", candidate.get("search_snippet") or "")
            candidate.setdefault("question_source", "search_snippet")
        age = candidate.get("age_days")
        if age is not None and age > THREAD_MAX_AGE_DAYS:
            sub_ev = search_evidence.get(candidate["subreddit"], {})
            counts = sub_ev.setdefault("excluded_counts", {})
            counts["thread_older_than_7_days"] = counts.get("thread_older_than_7_days", 0) + 1
            continue
        if MEDICAL_RISK_RE.search(candidate.get("question_excerpt") or ""):
            sub_ev = search_evidence.get(candidate["subreddit"], {})
            counts = sub_ev.setdefault("excluded_counts", {})
            counts["medical_advice_or_crisis_content"] = counts.get(
                "medical_advice_or_crisis_content", 0) + 1
            continue
        info = sub_state(ledger, candidate["subreddit"], now)
        draft = build_draft(candidate, info, facts)
        lint = lint_draft(draft["text"], link_policy=info["link_policy"])
        thread = {
            "url": candidate["url"],
            "subreddit": candidate["subreddit"],
            "title": candidate["title"],
            "question_excerpt": candidate.get("question_excerpt", ""),
            "question_source": candidate.get("question_source", "search_snippet"),
            "detected_intent": candidate["detected_intent"],
            "freshness": {
                "age_days": age,
                "basis": candidate.get("freshness_basis"),
                "window": "<=7d",
            },
            "eligibility_state": info["eligibility_state"],
            "link_policy": info["link_policy"],
            "rule_excerpt": info["rule_excerpt"],
            "rule_warnings": info["warnings"],
            "draft": draft["text"],
            "draft_sha256": draft["sha256"],
            "draft_basis": draft["basis"],
            "draft_link_count": draft["link_count"],
            "lint_pass": not lint,
            "lint_reasons": lint,
        }
        threads.append(thread)

    seen_drafts: dict[str, str] = {}
    for thread in threads:
        first_url = seen_drafts.get(thread["draft_sha256"])
        if first_url:
            thread["duplicate_draft_note"] = (
                f"Same core answer as {first_url} - vary the wording before posting."
            )
        else:
            seen_drafts[thread["draft_sha256"]] = thread["url"]

    pack = build_pack(now=now, ledger=ledger, facts=facts, threads=threads,
                      search_evidence=search_evidence, subs=subs)
    pack = shrink_pack(pack)
    pack_path = packs_dir / f"{pt_day(now)}.json"
    pack_sha = atomic_write_json(pack_path, pack)
    pack["_written"] = {"path": str(pack_path), "sha256": pack_sha}
    return pack


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GLP Reddit daily human-post scout (local pack only).")
    parser.add_argument("--refresh-rules", action="store_true", help="force re-capture all sub rules")
    parser.add_argument("--max-threads", type=int, default=DEFAULT_MAX_THREADS)
    parser.add_argument("--max-scrapes", type=int, default=DEFAULT_MAX_SCRAPES)
    args = parser.parse_args(argv)
    try:
        pack = run(
            search=live_search_fn(),
            scrape=live_scrape_fn(),
            max_threads=args.max_threads,
            max_scrapes=args.max_scrapes,
            refresh_rules_force=args.refresh_rules,
        )
    except RuntimeError as exc:
        print(f"FATAL glp_reddit_daily_scout: {exc}", file=sys.stderr)
        return 2
    written = pack.get("_written", {})
    print(json.dumps({
        "status": pack["status"],
        "pack_path": written.get("path"),
        "pack_sha256": written.get("sha256"),
        "counts": pack["counts"],
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
