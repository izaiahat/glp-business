#!/usr/bin/env python3
"""Advertiser site-evidence lane for the GLP application filter.

WHY THIS EXISTS
---------------
Operator verification task 2026-08-06. The application scoping reported 6
GLP-fit programs and rejected 1,269. The audit found the rejections were not
mostly wrong *judgements* — they were judgements made with no evidence:

  * Katalys emits `{name, program_id, relationship}` and nothing else, so all
    405 of its marketplace offers were scored on their brand name alone.
    "Ro.co", "Hims, Inc.", "PlushCare", "Ivim Health", "MEDVi", "Trim Rx" and
    "bmiMD Personalized Health RX" all read as `no_lane_match`. Every one of
    them prescribes semaglutide/tirzepatide — proven by fetching their sites.
  * An Awin record carries at most a 255-char marketing blurb. "Border Free
    Health US" describes itself as "an online prescription referral service"
    and never says GLP-1; its site sells Ozempic, Mounjaro and retatrutide.

The operator's rule is explicit: *a telehealth/clinic brand whose NAME doesn't
say weight-loss but which OFFERS GLP-1/semaglutide/tirzepatide care is a fit.*
You cannot apply that rule without looking at what the brand offers. This
module is that lookup.

CONTRACT
--------
  * `extract()` and `candidate_site()` are PURE — no network, no clock. The
    filter and the daily auto-apply job only ever touch those two, so the
    application path stays deterministic and the test suite stays offline.
  * `harvest()` takes an injected `fetcher`; production passes the Firecrawl
    one, tests pass a fake. Results land in a JSON cache keyed by advertiser,
    so a site is fetched once and every later run reads it for free.
  * Evidence is STRUCTURED (tokens + two booleans), never raw page text. A raw
    dump fed back into the body-copy lanes would trip an off-vertical veto on
    every page footer that mentions supplements.
  * Read-only by construction: GETs of public marketing pages. Never a
    tracking link (see `_TRACKING_HOSTS`), never an authenticated surface.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")
CACHE_PATH = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/site-evidence-cache.json"
CACHE_SCHEMA = "affiliate-site-evidence-cache-v1"

_spec = importlib.util.spec_from_file_location(
    "affiliate_niche_geo_filter", Path(__file__).resolve().with_name("affiliate_niche_geo_filter.py"))
nfilter = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = nfilter
_spec.loader.exec_module(nfilter)

# Evidence patterns. GLP1/CLINICAL are the filter's own — one vocabulary, so a
# token that counts on a marketplace blurb counts identically on a site.
GLP1_RE = nfilter.GLP1_EXPLICIT_RE
WEIGHT_LOSS_RE = nfilter.WEIGHT_LOSS_RE
CLINICAL_RE = nfilter.CLINICAL_CONTEXT_RE

# Affiliate redirectors: fetching one is a CLICK and would fire a commission
# event. The resolver is contractually forbidden from touching these.
_TRACKING_HOSTS = (
    "awin1.com", "awin.com/cread", "track.revoffers.com", "shareasale.com",
    "anrdoezrs.net", "dpbolvw.net", "jdoqocy.com", "kqzyfj.com", "tkqlhce.com",
    "click.linksynergy.com", "linksynergy.com/deeplink", "prf.hn", "go2cloud",
    "hop.clickbank.net", "impact.com/c/", "partnerize", "everflowclient.io",
)

# Paths a GLP-1 provider almost always publishes. Trying a couple beyond the
# homepage is what separates "Ivim Health" (homepage says "weight care") from a
# confirmed semaglutide prescriber.
_PROBE_PATHS = ("", "/weight-loss", "/programs", "/treatments")


def is_tracking_url(url: str) -> bool:
    low = (url or "").lower()
    return any(host in low for host in _TRACKING_HOSTS)


def normalize_target(url: str) -> str:
    """Advertiser homepage for a record's display URL, or "" if unusable."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    if is_tracking_url(raw):
        return ""
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    if not parts.hostname:
        return ""
    return urllib.parse.urlunsplit(("https", parts.netloc, "", "", ""))


def cache_key(row: dict) -> str:
    """Advertiser identity for the cache.

    Domain when we have one (two marketplaces listing the same clinic share the
    evidence); otherwise `network:program_id`, which is stable per record.
    """
    target = normalize_target(str(row.get("url") or ""))
    if target:
        host = urllib.parse.urlsplit(target).hostname or ""
        return "domain:" + host.lower().removeprefix("www.")
    return f"{row.get('network') or ''}:{row.get('program_id') or ''}".lower()


def extract(text: str, url: str = "") -> dict:
    """Structured evidence from fetched page text. PURE."""
    body = text or ""
    tokens = sorted({m.group(0).lower().replace(" ", "-") for m in GLP1_RE.finditer(body)})
    return {
        "url": url,
        "fetched": bool(body),
        "glp1_tokens": tokens[:8],
        "weight_loss": bool(WEIGHT_LOSS_RE.search(body)),
        "clinical": bool(CLINICAL_RE.search(body)),
        "error": "",
    }


def to_site_evidence(record: dict | None):
    """Cache record -> filter's SiteEvidence. PURE."""
    if not record:
        return None
    return nfilter.SiteEvidence(
        url=str(record.get("url") or ""),
        fetched=bool(record.get("fetched")),
        glp1_tokens=tuple(record.get("glp1_tokens") or ()),
        weight_loss=bool(record.get("weight_loss")),
        clinical=bool(record.get("clinical")),
        error=str(record.get("error") or ""),
    )


def candidate_site(row: dict, cache: dict):
    """Cached evidence for a candidate row, or None. PURE — never fetches."""
    return to_site_evidence((cache.get("advertisers") or {}).get(cache_key(row)))


# --------------------------------------------------------------------------
# Which candidates are worth a fetch
# --------------------------------------------------------------------------
# Not the whole pool. Fetching 21k Awin retail brands to learn that a sneaker
# shop does not prescribe semaglutide is waste. Three classes can CHANGE their
# verdict with site evidence, and nothing else can:
#
#   name_only      no description, no categories — the record says nothing, so
#                  the current verdict is an artefact of missing data.
#   clinic_sibling the NAME declares a sibling CLINICAL vertical (TRT, hair,
#                  peptides, sleep). Gate 1 rejects it today; a site proving
#                  GLP-1 care overturns that, per the operator's rule. A
#                  PRODUCT vertical in the name (supplements, pets, lab tests,
#                  SaaS, gym gear) is never rescuable, so it is never fetched.
#   clinical_text  the blurb reads clinical but carries no GLP-1 token — the
#                  "Border Free Health US" class, where the offering simply did
#                  not fit in 255 characters.

def harvest_class(row: dict) -> str | None:
    name = str(row.get("name") or "")
    desc = str(row.get("description") or "")
    cats = " ".join(str(x) for x in (row.get("categories") or ()))
    text = f"{name} {desc} {cats}"
    if GLP1_RE.search(text):
        return None                       # already proven by the record itself
    off_name = nfilter._off_verticals(name.lower())
    if off_name:
        if all(v in nfilter.CLINIC_SIBLING_VERTICALS for v in off_name):
            return "clinic_sibling"
        return None                       # product vertical: unrescuable
    if not desc.strip() and not cats.strip():
        return "name_only"
    if CLINICAL_RE.search(text):
        return "clinical_text"
    if WEIGHT_LOSS_RE.search(text):
        # weight_claim  the record sells weight loss but carries no clinical
        #               token, so `park_weight_claim_without_clinical_context`
        #               rejected it outright and it was never fetched. That is
        #               how "Center for Medical Weight Loss" and "MDWLS" —
        #               US medical weight-loss clinics that plausibly prescribe
        #               GLP-1 — were dropped without anyone looking at the site
        #               (operator, 2026-08-15). A weight-loss offering is
        #               exactly the class where the site is the deciding
        #               evidence, and a fetch that finds nothing is a cheap
        #               true negative.
        return "weight_claim"
    return None


# A `name_only` record is any row a network ships without a description — which
# after the CJ and Rakuten catalog fixes is 4,459 rows, most of them shoe shops.
# Fetching all of them to learn that Footshop.hu does not prescribe semaglutide
# is waste, so name-only rows must first show a health signal in the brand name
# or its domain. Deliberately loose ("labs", "life", "body" all pass): a fetch
# that finds nothing is a cheap true negative, whereas a missed clinic is the
# exact failure this whole lane exists to prevent.
HEALTH_SIGNAL_RE = re.compile(
    r"(\brx|rx\b|\bmd\b|med|health|care|clinic|well|weight|slim|trim|"
    r"skinny|lean|telehealth|telemed|doctor|\bdrs?\b|pharma|hormone|thin|shape|"
    r"body|vital|life|labs?\b|therapy|patient|prescri|script|hers|hims|nutri|"
    r"metabol|bmi|glp|semaglut|tirzepat|peptide|longevity|aesthet|derm|obes|"
    r"bariatric|inject|wegov|ozempic|mounjaro|zepbound)", re.I)


def _us_plausible(row: dict) -> bool:
    """Conservative pre-fetch geo gate.

    Confirmed US rows pass. Rows with no geo evidence also pass because a
    public-site fetch is the evidence-gathering step, not an application. Any
    explicit non-US region, currency, name, or TLD still fails before credits
    are spent. The downstream application gate remains fail-closed and still
    requires confirmed US evidence.
    """
    candidate = nfilter.Candidate(
        name=str(row.get("name") or ""), description=str(row.get("description") or ""),
        categories=tuple(row.get("categories") or ()),
        region=str(row.get("region") or ""), currency=str(row.get("currency") or ""),
        url=str(row.get("url") or ""), network_region=str(row.get("network_region") or ""))
    ok, reasons = nfilter.classify_geo(candidate)
    if ok:
        return True
    if reasons != ["region_unverified"]:
        return False
    # `classify_geo` intentionally parks unknowns. For read-only enrichment,
    # unknown is plausible only when the provider supplied no contradictory
    # region/currency field at all.
    region = (str(row.get("region") or "").strip()
              or str(row.get("network_region") or "").strip())
    currency = str(row.get("currency") or "").strip().upper()
    return not region and (not currency or currency == "USD")


def harvest_targets(pool: list[dict], cache: dict, prefilter: bool = True) -> list[dict]:
    """Rows needing a fetch, deduped by advertiser, cached ones removed."""
    known = cache.get("advertisers") or {}
    out, seen = [], set()
    for row in pool:
        klass = harvest_class(row)
        if not klass:
            continue
        if prefilter:
            if not _us_plausible(row):
                continue
            if klass == "name_only" and not HEALTH_SIGNAL_RE.search(
                    f"{row.get('name') or ''} {row.get('url') or ''}"):
                # Operator ruling 2026-08-15: exhaust every marketplace we
                # already have access to before joining more. A name-only row
                # with no health token in its brand was dropped forever, which
                # is precisely how a US GLP telehealth brand whose name says
                # nothing clinical ("Ro", "Found", "Calibrate") stays invisible.
                # Demote instead of discard: it still gets fetched, just after
                # every higher-signal class, and the cache means each advertiser
                # costs one fetch ever.
                if not (row.get("url") or row.get("displayUrl")):
                    continue
                klass = "unscreened_brand"
        key = cache_key(row)
        if key in known or key in seen:
            continue
        seen.add(key)
        out.append({**row, "_harvest_class": klass, "_cache_key": key})
    return out


def parked_not_fit_pool(pool: list[dict], cache: dict) -> list[dict]:
    """Return current inventory rows parked by the GLP application filter.

    This uses the exact cached site-evidence projection consumed by the planner.
    Already-fit rows never spend Firecrawl credits merely because their text is
    clinical. Final policy and execution gates are still re-run by the apply
    machine after this read-only enrichment stage.
    """
    parked: list[dict] = []
    for row in pool:
        decision, _ = nfilter.evaluate_application(nfilter.Candidate(
            name=str(row.get("name") or ""),
            network=str(row.get("network") or ""),
            program_id=str(row.get("program_id") or ""),
            description=str(row.get("description") or ""),
            categories=tuple(row.get("categories") or ()),
            region=str(row.get("region") or ""),
            network_region=str(row.get("network_region") or ""),
            currency=str(row.get("currency") or ""),
            url=str(row.get("url") or ""),
            site=candidate_site(row, cache),
        ))
        if not decision.apply_ok:
            parked.append(row)
    return parked


def prioritize_targets(rows: list[dict]) -> list[dict]:
    """Deterministic highest-yield order for the daily bounded fetch budget."""
    # weight_claim outranks name_only: the record already states a weight-loss
    # offering, so it is likelier to prove GLP-1 than an unnamed health-ish brand.
    priority = {"clinical_text": 0, "clinic_sibling": 1, "weight_claim": 2,
                "name_only": 3, "unscreened_brand": 4}
    return sorted(rows, key=lambda row: (
        priority.get(str(row.get("_harvest_class") or ""), 99),
        str(row.get("network") or "").lower(),
        str(row.get("name") or "").lower(),
        str(row.get("program_id") or ""),
    ))


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def load_cache(path: Path = CACHE_PATH) -> dict:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"schema": CACHE_SCHEMA, "advertisers": {}}
    doc.setdefault("advertisers", {})
    return doc


def save_cache(cache: dict, path: Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")
    os.chmod(path, 0o600)


def harvest(rows: list[dict], fetcher, cache: dict, max_workers: int = 6) -> dict:
    """Fetch each row's advertiser site and fold evidence into `cache`.

    `fetcher(name, url) -> (text, resolved_url, error)`. Injected so the whole
    module is testable without a provider.
    """
    stats = {"attempted": 0, "fetched": 0, "glp1_confirmed": 0, "failed": 0,
             "by_class": {}}

    def one(row):
        target = normalize_target(str(row.get("url") or ""))
        return row, fetcher(str(row.get("name") or ""), target)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for row, (text, resolved, error) in pool.map(one, rows):
            stats["attempted"] += 1
            klass = row.get("_harvest_class") or "unknown"
            stats["by_class"][klass] = stats["by_class"].get(klass, 0) + 1
            rec = extract(text, resolved or normalize_target(str(row.get("url") or "")))
            rec["error"] = error or ""
            rec["harvest_class"] = klass
            rec["program_name"] = str(row.get("name") or "")
            if rec["fetched"]:
                stats["fetched"] += 1
                if rec["glp1_tokens"] and rec["clinical"]:
                    stats["glp1_confirmed"] += 1
            else:
                stats["failed"] += 1
            cache.setdefault("advertisers", {})[row.get("_cache_key") or cache_key(row)] = rec
    cache["schema"] = CACHE_SCHEMA
    cache["last_harvest_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return stats


def firecrawl_fetcher(api_key: str | None = None, timeout: int = 120, max_requests: int = 40):
    """Map-first public evidence; all requests, including polls, share one cap."""
    key = api_key
    lock = threading.Lock()
    counts = {"requests": 0, "cap": max_requests}

    def reserve_request():
        with lock:
            if counts['requests'] >= max_requests:
                raise RuntimeError('firecrawl_request_cap_reached')
            counts['requests'] += 1
    def call(path: str, payload: dict | None):
        reserve_request()
        req = urllib.request.Request(
            f"https://api.firecrawl.dev/v2/{path}", data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {key or _firecrawl_key()}", "Content-Type": "application/json"})
        from firecrawl_client import firecrawl_urlopen
        with firecrawl_urlopen(req, caller='affiliate_site_evidence', timeout=timeout) as resp:
            return json.load(resp)

    def resolve(name: str) -> str:
        """Brand -> official domain, for records that carry no URL (Katalys)."""
        try:
            doc = call("search", {"query": f"{name} official website", "limit": 3})
        except Exception:
            return ""
        data = doc.get("data") or {}
        hits = data.get("web") if isinstance(data, dict) else data
        for hit in (hits or []):
            url = normalize_target(str((hit or {}).get("url") or ""))
            if url:
                return url
        # Only unknown brands reach the bounded Agent. Its answer is a lead,
        # not fit/approval evidence; map and source capture still follow.
        agent = call('agent', {'prompt': f'Find only the official US website for {name}. No tracking links or contact actions.',
                              'maxCredits': 10, 'schema': {'type': 'object', 'properties': {'website': {'type': 'string'}}}})
        if agent.get('id'):
            result = call('agent/' + agent['id'], None)
            if result.get('status') == 'completed':
                return normalize_target(str((result.get('data') or {}).get('website') or ''))
        return ""

    def fetch(name: str, url: str):
        target = normalize_target(url)
        if 'fella' in (name + url).lower():
            return '', '', 'excluded_provider'
        try:
            target = target or resolve(name)
            if not target:
                return '', '', 'no_resolvable_url'
            mapped = call('map', {'url': target, 'limit': 20, 'includeSubdomains': False})
            links = [normalize_target(str(x.get('url') if isinstance(x, dict) else x)) for x in mapped.get('links', [])]
            host = urllib.parse.urlsplit(target).hostname
            links = [u for u in links if u and urllib.parse.urlsplit(u).hostname == host and 'fella' not in u.lower()]
            relevant = [u for u in links if re.search(r'weight|glp|semaglutide|tirzepatide|affiliate|partner', u, re.I)]
            urls = list(dict.fromkeys([target, *relevant]))[:3]
            batch = call('batch/scrape', {'urls': urls, 'formats': ['markdown'], 'onlyMainContent': True, 'proxy': 'basic'})
            if not batch.get('id'):
                return '', target, 'batch_not_started'
            for _ in range(10):
                time.sleep(2)
                result = call('batch/scrape/' + batch['id'], None)
                if result.get('status') == 'completed':
                    text = '\n'.join(str(x.get('markdown') or '') for x in result.get('data', []))
                    return text, target, '' if text else 'empty_response'
                if result.get('status') in ('failed', 'cancelled'):
                    return '', target, 'batch_failed'
            return '', target, 'batch_pending_no_replay'
        except Exception as exc:
            return '', target, type(exc).__name__ + (':request_cap' if counts['requests'] >= max_requests else '')

    fetch.request_counts = counts
    fetch.reserve_request = reserve_request
    return fetch


def _firecrawl_key() -> str:
    env = Path.home() / ".hermes" / ".env"
    for line in env.read_text().splitlines():
        if line.startswith("FIRECRAWL_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("firecrawl_api_key_missing")


def inventory_pool(reader) -> list[dict]:
    pool = []
    for path in reader.freshest_inventories():
        rows, source = reader.read_inventory(path)
        for row in rows:
            row = dict(row)
            row['url'] = row.get('official_url') or ''
            row['_source_path'] = source['path']
            pool.append(row)
    return pool


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Harvest advertiser site evidence.")
    ap.add_argument("--pool", help="JSON file: list of candidate rows (default: live inventories)")
    ap.add_argument("--limit", type=int, default=40, help="max advertisers per run")
    ap.add_argument("--fetch-timeout", type=int, default=15,
                    help="per-Firecrawl-request timeout in seconds")
    ap.add_argument("--workers", type=int, default=6, help="parallel public-site fetches")
    ap.add_argument("--receipt", help="write a harvest receipt here")
    ap.add_argument("--max-requests", type=int, default=40, help="all Firecrawl requests, including map, batch and polls")
    args = ap.parse_args(argv)

    if args.pool:
        pool = json.loads(Path(args.pool).read_text())
    else:
        spec = importlib.util.spec_from_file_location(
            "affiliate_auto_apply", BUSINESS / "scripts/affiliate_auto_apply.py")
        aa = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = aa
        spec.loader.exec_module(aa)
        pool = inventory_pool(aa)

    cache = load_cache()
    cache_size_before = len(cache.get("advertisers") or {})
    parked = parked_not_fit_pool(pool, cache)
    available = prioritize_targets(harvest_targets(parked, cache, prefilter=True))
    targets = available[: max(0, args.limit)]
    fetcher = firecrawl_fetcher(timeout=max(1, args.fetch_timeout), max_requests=max(0, args.max_requests)) if targets else None
    stats = (harvest(targets, fetcher, cache, max_workers=max(1, args.workers))
             if targets else {"attempted": 0, "fetched": 0, "glp1_confirmed": 0,
                              "failed": 0, "by_class": {}})
    if not targets:
        cache["schema"] = CACHE_SCHEMA
        cache["last_harvest_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_cache(cache)
    rescued = []
    for row in targets:
        decision, verdict = nfilter.evaluate_application(nfilter.Candidate(
            name=str(row.get("name") or ""), network=str(row.get("network") or ""),
            program_id=str(row.get("program_id") or ""),
            description=str(row.get("description") or ""),
            categories=tuple(row.get("categories") or ()),
            region=str(row.get("region") or ""),
            network_region=str(row.get("network_region") or ""),
            currency=str(row.get("currency") or ""), url=str(row.get("url") or ""),
            site=candidate_site(row, cache),
        ))
        if decision.apply_ok:
            rescued.append({"candidate_id": f"{row.get('network')}:{row.get('program_id')}",
                            "network": row.get("network"), "program_id": row.get("program_id"),
                            "name": row.get("name"), "decision": decision.decision,
                            "fit_tier": verdict.tier})
    receipt_path = str(Path(args.receipt)) if args.receipt else ""
    receipt = {"schema": "affiliate-site-evidence-harvest-v2",
               "captured_at_utc": cache["last_harvest_utc"],
               "status": ("HARVESTED" if targets
                          else "ALL_RESCUABLE_CLASSES_CACHED_ZERO_NEW"),
               "pool_size": len(pool), "parked_not_fit_pool_size": len(parked),
               "rescuable_uncached_before_cap": len(available),
               "daily_cap": max(0, args.limit), "targets_selected": len(targets),
               "firecrawl_request_budget": getattr(fetcher, 'request_counts', {'requests': 0, 'cap': args.max_requests}),
               "cache_size_before": cache_size_before,
               "cache_size": len(cache.get("advertisers") or {}),
               "stats": stats, "filter_rescued_count": len(rescued),
               "filter_rescued_candidates": rescued,
               "source_paths": sorted({str(row.get("_source_path") or "") for row in pool
                                       if row.get("_source_path")}),
               "receipt": receipt_path,
               "tracking_clicks": 0, "provider_mutations": 0,
               "applications_submitted": 0, "secret_printed": False}
    if args.receipt:
        Path(args.receipt).parent.mkdir(parents=True, exist_ok=True)
        Path(args.receipt).write_text(json.dumps(receipt, indent=1) + "\n")
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
