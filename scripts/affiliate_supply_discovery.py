#!/usr/bin/env python3
"""Affiliate Monday discovery lane for direct programs and new marketplaces.

Operator authority 2026-08-06: *"there is no real shortage of GLP telehealth
providers; if inventories run dry the machine finds more outlets, joins them
itself, or tells the operator exactly what to unblock."*

Every Monday this lane discovers review candidates independently of application
volume. It finds supply along three routes:

  1. DIRECT PROGRAMS — web discovery of GLP/telehealth providers that run their
     own affiliate program ("<provider> affiliate program", "GLP-1 telehealth
     affiliate program", review-site disclosure pages, provider directories, and
     the Sheet's own provider list). Every find is normalised, GLP-screened with
     the SAME gate the apply lane uses, deduped against the Sheet + inventories
     + applications ledger, and emitted two ways: as an inventory the apply lane
     picks up on its next run, and as candidate rows for the allowlisted Sheet
     writer (status `ready_to_apply` — NEVER approved).

  2. NEW MARKETPLACES — affiliate networks that host GLP programs and that we do
     not have an account on. Ranked by observed GLP supply. Discovery never joins
     one; operator review is required first.

  3. UNBLOCK CARDS — when a marketplace refuses self-service (human gate,
     phone/interview, manual publisher review), the finding is the deliverable:
     an operator card naming the marketplace, the exact step, and the exact
     action required. Routed to #decisions, because it is an operator action.

Design rules (same as the rest of the machine):
  * PROVIDER-FREE CORE. `search` and `scrape` are injected callables; the whole
    test suite runs with fakes and touches no network. The live Firecrawl
    implementations are built lazily, only by the CLI.
  * NEVER fetches a tracking/redirect URL. `is_tracking_url` refuses them before
    a scrape and before a record is emitted.
  * No applications are submitted here and no Sheet write happens here: this
    lane produces the PLAN the allowlisted CAS writer and the apply lane consume.
  * Receipts 0600 under the machine dir; no secret ever reaches a receipt.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import importlib.util
import json
import os
import re
import signal
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BUSINESS = Path(__file__).resolve().parents[1]
MACHINE_DIR = BUSINESS / "reports/glp-clinic-scout/affiliate-machine"
SUPPLY_DIR = MACHINE_DIR / "supply-discovery"
LEDGER_PATH = MACHINE_DIR / "applications-ledger.json"
DEFAULT_DISCOVERY_TARGET = 25
DEFAULT_RUN_BUDGET_SECONDS = 360
DEFAULT_SOURCE_TIMEOUT_SECONDS = 20

DIR_MODE = stat.S_IRWXU
FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


nfilter = _load("affiliate_niche_geo_filter", BUSINESS / "scripts/affiliate_niche_geo_filter.py")
operator_rulings = _load("affiliate_operator_rulings", BUSINESS / "scripts/affiliate_operator_rulings.py")
supply_expansion = _load("affiliate_supply_expansion", BUSINESS / "scripts/affiliate_supply_expansion.py")

# --------------------------------------------------------------- query pack

# Deterministic and ordered: the same run on the same day asks the same
# questions, so two runs are comparable and a receipt is reproducible.
DIRECT_QUERIES: tuple[str, ...] = (
    "GLP-1 telehealth affiliate program",
    "semaglutide affiliate program",
    "tirzepatide telehealth affiliate program",
    "weight loss telehealth affiliate program commission",
    "compounded semaglutide clinic affiliate program",
    "medical weight loss clinic affiliate program sign up",
    "GLP-1 weight loss affiliate program high commission",
    "online weight loss clinic partner program affiliate",
    "best GLP-1 affiliate programs list",
    "weight loss telehealth affiliate disclosure \"we may earn a commission\"",
    "telehealth semaglutide \"affiliate program\" apply",
    "obesity medicine telehealth referral partner program",
)

MARKETPLACE_QUERIES: tuple[str, ...] = (
    "affiliate network health weight loss offers GLP-1",
    "best affiliate networks for health and wellness offers",
    "affiliate network telehealth pharmacy offers publisher signup",
    "health vertical CPA network weight loss offers join as affiliate",
    "affiliate marketplace supplements telehealth publisher application",
)

# Seeded from the Sheet's own provider list: any GLP-vertical provider we have
# NOT yet found a direct program for is worth one targeted query.
SEED_QUERY_TEMPLATE = "{provider} affiliate program"
MAX_SEED_QUERIES = 40

# Item 4 supply-source expansion. These are deliberately bounded and ordered so
# a daily receipt is comparable with the prior day's receipt. They are discovery
# queries only: every returned program still passes the common KnownSupply,
# domestic-US GLP-fit, and operator-ruling gates below.
PROVIDER_ENUMERATION_QUERIES: tuple[str, ...] = (
    "US telehealth weight loss provider directory GLP-1",
    "LegitScript certified telemedicine weight loss providers",
    "compounding pharmacy telehealth partner semaglutide provider list",
    "site:everflowclient.io GLP-1 telehealth",
)
PROVIDER_PROGRAM_QUERY_TEMPLATES: tuple[str, ...] = (
    '"{provider}" affiliate program',
    'site:{domain} (affiliate OR partners OR ambassador)',
)
QUERY_EXPANSION_QUERIES: tuple[str, ...] = (
    "semaglutide affiliate program",
    "tirzepatide affiliate program",
    "compounded semaglutide partner program",
    "compounded tirzepatide partner program",
    "telehealth weight loss ambassador program",
    "GLP-1 pharmacy referral program",
    "oral semaglutide affiliate program",
    "medical weight loss creator partner program",
)
MAX_QUERY_EXPANSION_QUERIES = 8
MAX_PROVIDER_SEEDS = 40
MAX_COMPETITOR_ARTIFACTS = 60
MAX_NEWSLETTER_ARTIFACTS = 30
MAX_SOURCE_JSON_BYTES = 2 * 1024 * 1024
SEO_RESEARCH_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine"
NEWSLETTER_INTAKE_ROOT = MACHINE_DIR / "newsletter-supply-intake"

# Official/public discovery evidence and the exact method supported by current
# research. A route definition is not an invented provider endpoint. Auth-only
# surfaces are explicit and fail closed unless an injected adapter supplies a
# provider response. Public-search surfaces use only the injected search seam.
PLATFORM_SWEEP_DEFINITIONS: tuple[dict, ...] = (
    {"slug": "uppromote", "mode": "public_search",
     "queries": ("site:uppromote.com/affiliate-programs weight loss affiliate programs",),
     "evidence": ("https://uppromote.com/affiliate-programs/weight-loss/",)},
    {"slug": "goaffpro", "mode": "public_search",
     "queries": ("GoAffPro marketplace GLP-1 weight loss affiliate",),
     "evidence": ("https://goaffpro.com/marketplace",)},
    {"slug": "refersion", "mode": "authenticated_catalog",
     "queries": (), "evidence": ("https://marketplace.refersion.com/products",
                                    "https://support.refersion.com/en/articles/6812794-how-to-discover-brands-and-promote-their-products")},
    {"slug": "levanta", "mode": "authenticated_catalog",
     "queries": ("site:levanta.io (semaglutide OR tirzepatide OR GLP-1 OR telehealth) affiliate",),
     "evidence": ("https://knowledge.levanta.io/articles/7672914299-getting-started-with-creators",)},
    {"slug": "sovrn", "mode": "public_search",
     "queries": ("site:commerce.sovrn.com (semaglutide OR tirzepatide OR GLP-1 OR telehealth)",),
     "evidence": ("https://www.sovrn.com/publishers/commerce/",)},
    {"slug": "partnerize", "mode": "official_api_v2",
     "queries": (), "evidence": ("GET https://api.partnerize.com/v2/publishers/{publisherId}/discovery/advertisers",
                                    "https://api-docs.partnerize.com/partner/")},
    {"slug": "rakuten", "mode": "authenticated_catalog",
     "queries": (), "evidence": ("Rakuten advertiser discovery requires authenticated OAuth",)},
    {"slug": "everflow", "mode": "public_search",
     "queries": ("site:everflowclient.io (semaglutide OR tirzepatide OR GLP-1)",),
     "evidence": ("https://developers.everflow.io/docs/affiliate/",)},
    {"slug": "stape", "mode": "no_public_catalog_evidence", "queries": (),
     "evidence": ("NO_PUBLIC_CATALOG_EVIDENCE:Stape",)},
    {"slug": "tapfiliate", "mode": "no_public_catalog_evidence", "queries": (),
     "evidence": ("NO_PUBLIC_CATALOG_EVIDENCE:Tapfiliate",)},
    {"slug": "firstpromoter", "mode": "no_public_catalog_evidence", "queries": (),
     "evidence": ("NO_PUBLIC_CATALOG_EVIDENCE:FirstPromoter",)},
)

# ------------------------------------------------------- URL classification

# Hosts that ARE affiliate platforms rather than a provider's own site. A
# program URL on one of these means the provider runs through that network.
PLATFORM_HOSTS: dict[str, str] = {
    "awin.com": "awin", "awin1.com": "awin", "ui.awin.com": "awin",
    "cj.com": "cj", "dpbolvw.net": "cj", "anrdoezrs.net": "cj",
    "impact.com": "impact", "impactradius.com": "impact", "pxf.io": "impact",
    "partnerize.com": "partnerize", "prf.hn": "partnerize",
    "katalys.com": "katalys", "revoffers.com": "katalys",
    "rakutenadvertising.com": "rakuten", "linksynergy.com": "rakuten",
    "healthtrader.com": "healthtrader",
    "clickbank.com": "clickbank", "clickbank.net": "clickbank",
    "digistore24.com": "digistore24",
    "partnerstack.com": "partnerstack",
    "refersion.com": "refersion",
    "everflowclient.io": "everflow", "everflow.io": "everflow",
    "sovrn.com": "sovrn", "viglink.com": "sovrn",
    "levanta.io": "levanta",
    "shareasale.com": "shareasale",
    "flexoffers.com": "flexoffers",
    "maxbounty.com": "maxbounty",
    "avantlink.com": "avantlink",
    "pepperjam.com": "pepperjam", "ascendpartner.com": "pepperjam",
    "linkconnector.com": "linkconnector",
    "webgains.com": "webgains",
    "tradedoubler.com": "tradedoubler",
    "admitad.com": "admitad",
    "goaffpro.com": "goaffpro",
    "tapfiliate.com": "tapfiliate",
    "firstpromoter.com": "firstpromoter",
    "getrewardful.com": "rewardful", "rewardful.com": "rewardful",
    "leaddyno.com": "leaddyno",
    "postaffiliatepro.com": "postaffiliatepro",
    "shareasale.io": "shareasale",
    "affiliatly.com": "affiliatly",
    "uppromote.com": "uppromote",
    "offervault.com": "offervault",
    "cpagrip.com": "cpagrip",
    "clickdealer.com": "clickdealer",
    "gasmobi.com": "gasmobi",
    "adcombo.com": "adcombo",
    "crakrevenue.com": "crakrevenue",
    "dr-cash.com": "drcash", "drcash.com": "drcash",
}

# Never fetched, never emitted: a click on one of these is an attribution event.
TRACKING_MARKERS: tuple[str, ...] = (
    "awin1.com", "awclick", "cread.php", "anrdoezrs", "dpbolvw", "jdoqocy",
    "tkqlhce", "kqzyfj", "track.revoffers.com", "prf.hn", "linksynergy.com",
    "shareasale.com/r.cfm", "go.skimresources", "?aff_c", "&aff_c", "/go/",
    "clickbank.net/?", "hop.clickbank", "imp.pxf.io", "pxf.io/t/", "rstyle.me/",
    "redirectingat.com", "go2cloud.org", "/aff/", "?ref=", "&ref=", "utm_",
    "redirect?", "/track/", "/click/", "/out/",
)

# Aggregator/forum hosts: useful as EVIDENCE (they name programs) but never a
# program URL themselves.
NON_PROGRAM_HOSTS: tuple[str, ...] = (
    # social / forum / encyclopedia
    "reddit.com", "quora.com", "youtube.com", "facebook.com", "twitter.com",
    "x.com", "linkedin.com", "instagram.com", "tiktok.com", "medium.com",
    "pinterest.com", "wikipedia.org", "amazon.com", "glpclinicscout.com",
    "blackhatworld.com", "warriorforum.com", "stackexchange.com",
    # affiliate-industry directories and blogs: they INDEX programs, they are
    # not programs. Left in the pool they become providers called "Strackr".
    "affpaying.com", "affilitizer.com", "affiliatecompare.co.uk", "affililist.com",
    "offervault.com", "strackr.com", "authorityhacker.com", "mediakix.com",
    "marketingweek.com", "affiliatefix.com", "affiliateprograms.com",
    # press-release wires and link-shortener/partner-link services
    "prweb.com", "prnewswire.com", "c212.net", "businesswire.com", "einpresswire.com",
    "globenewswire.com", "dub.co", "linkclicky.com", "affi.io", "audenticity.com",
    "referralrocket.io", "affiliatly.com",
)

# Path/anchor shapes that mean "this is the program's signup surface".
PROGRAM_PATH_RE = re.compile(
    r"(?i)(affiliate|partner|ambassador|referral|refer-a-friend|influencer|creator)"
    r"(s)?(-|_|/|\b)"
)
SIGNUP_PATH_RE = re.compile(r"(?i)(sign[-_ ]?up|apply|join|get-?started|register|application)")

# EDITORIAL pages — listicles, blog posts, press releases, forum threads and
# affiliate-DISCLOSURE pages. These are not programs and must never be recorded
# as one ("Strackr" is not a GLP clinic). They are, however, exactly where the
# open web keeps its list of who runs a program, so the lane MINES them for the
# providers they name instead of throwing them away.
EDITORIAL_PATH_RE = re.compile(
    r"(?i)(/blog/|/news/|/releases?/|/press|/article|/guide|/resources?/|/reviews?/"
    r"|affiliate-disclosure|disclosure-policy|/forum|/thread|/seo/|/community|/offers?/"
    # agency/marketing-blog sections: a growth agency writing up someone else's
    # program is editorial, however program-shaped the last path segment looks
    r"|/digital-marketing/|/marketing/|/growth/|/insights?/|/case-stud)")
# A DIRECTORY index: `.../affiliate-program(s)/<brand>` lists a program that
# belongs to somebody else. The page is a source of names, never a program.
# `/affiliate-program/get-started` is NOT that — it is a step INSIDE a real
# program, so the generic step words below are excluded by name.
DIRECTORY_INDEX_RE = re.compile(
    r"(?i)/(affiliate-programs?|partner-programs?|programs|offers|merchants|brands|stores)/([^/?#]+)")
PROGRAM_STEP_SEGMENTS = {
    "get-started", "getstarted", "signup", "sign-up", "apply", "application",
    "join", "register", "registration", "terms", "terms-and-conditions",
    "conditions", "agreement", "faq", "faqs", "details", "overview",
    "how-it-works", "login", "log-in", "dashboard", "info", "about", "benefits",
}


def _is_directory_index(path: str) -> bool:
    match = DIRECTORY_INDEX_RE.search(path)
    if not match:
        return False
    return match.group(2).strip("-_ ").lower() not in PROGRAM_STEP_SEGMENTS
EDITORIAL_TITLE_RE = re.compile(
    r"(?i)(\b\d+\s+best\b|\bbest\s+\d+\b|\btop\s+\d+\b|\bbest\b.*\bprograms\b"
    r"|\bprograms?\s+(?:in|for|of)\s+20\d\d|\bhow to\b|\bguide\b|\bvs\.?\b|\breview\b"
    r"|affiliate disclosure|\bannounces\b|\blist of\b)")
# Paths this deep are editorial by construction: a real program surface lives at
# /affiliate-program, not at /a/b/c/d/affiliate-program.
MAX_PROGRAM_PATH_SEGMENTS = 3


def is_editorial(url: str, title: str = "") -> bool:
    """True when this page WRITES ABOUT programs rather than being one."""
    try:
        path = urlparse(str(url)).path or "/"
    except ValueError:
        return True
    if EDITORIAL_PATH_RE.search(path) or _is_directory_index(path):
        return True
    if EDITORIAL_TITLE_RE.search(str(title or "")):
        return True
    return len([seg for seg in path.split("/") if seg]) > MAX_PROGRAM_PATH_SEGMENTS


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def private_json(path: Path, payload) -> str:
    path = Path(path)
    # A receipt path is data, not authority to traverse a symlink. Refuse an
    # existing symlink/special target and any symlinked existing parent before
    # mkdir/write so a fixture or CLI argument cannot redirect local output.
    cursor = path.parent.absolute()
    existing = []
    while not cursor.exists() and cursor != cursor.parent:
        cursor = cursor.parent
    while True:
        existing.append(cursor)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    if any(part.is_symlink() for part in existing) or path.is_symlink():
        raise ValueError("unsafe_output_symlink")
    if path.exists() and not path.is_file():
        raise ValueError("unsafe_output_not_regular_file")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, DIR_MODE)
    body = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(body.encode()).hexdigest()


class SourceDeferred(Exception):
    """A declared read is pending; this never means an empty source."""


class SourceUnavailable(Exception):
    """An attempted read failed; keep it distinct from zero results."""


class _ReadDeadline(BaseException):
    # Provider adapters often catch Exception internally. The timer must reach
    # our boundary instead of being swallowed and starting another retry.
    pass


class DiscoveryCheckpoint:
    """Replay successful read-only inputs while continuing unfinished sources.

    A source failure is retained for this cycle (so a broken first source cannot
    starve later work). A fresh completed cycle retries failures next Monday.
    The cron lock serializes checkpoint writers. No application/join callbacks
    may be passed here: interrupting a read is safe; interrupting effects isn't.
    """

    def __init__(self, path, *, run_id, budget_s, source_timeout_s=20, max_calls=200):
        self.path = Path(path)
        self.deadline = time.monotonic() + budget_s
        self.source_timeout_s = source_timeout_s
        self.max_calls = max_calls
        self.calls = 0
        self.visited = set()
        self.doc = {"schema": "affiliate-discovery-checkpoint-v1", "cycle_id": run_id,
                    "complete": False, "operations": {}}
        if self.path.exists():
            if self.path.is_symlink():
                raise ValueError("unsafe_checkpoint_symlink")
            prior = json.loads(self.path.read_text())
            if prior.get("schema") != self.doc["schema"]:
                raise ValueError("unsupported_discovery_checkpoint")
            if not prior.get("complete") or prior.get("cycle_id") == run_id:
                self.doc = prior

    def wrap(self, kind, callback):
        def bounded(*args):
            identity = json.dumps([kind, args], sort_keys=True)
            key = hashlib.sha256(identity.encode()).hexdigest()
            self.visited.add(key)
            operations = self.doc["operations"]
            saved = operations.get(key)
            if saved and saved["status"] == "ok":
                return saved["result"]
            if saved and saved["status"] == "unavailable":
                raise SourceUnavailable(saved["reason"])
            row = {"kind": kind, "arguments": args, "status": "pending"}
            operations[key] = row
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or self.calls >= self.max_calls:
                row["reason"] = "run_budget_exhausted"
                # No read started. Persist the full tail once in finish(), not
                # an increasingly large checkpoint for every skipped callback.
                raise SourceDeferred("run_budget_exhausted")
            self.calls += 1
            self.save()  # an interrupted process leaves a retryable pending read
            prior_handler = signal.getsignal(signal.SIGALRM)
            prior_timer = signal.getitimer(signal.ITIMER_REAL)
            if prior_timer[0]:
                raise RuntimeError("discovery_requires_exclusive_read_timer")
            def expire(_signum, _frame):
                raise _ReadDeadline()
            signal.signal(signal.SIGALRM, expire)
            try:
                signal.setitimer(signal.ITIMER_REAL, min(remaining, self.source_timeout_s))
                result = callback(*args)
                if isinstance(result, dict) and str(result.get("status", "")).lower().startswith("error"):
                    raise SourceUnavailable("provider_read_error")
                row.update(status="ok", result=result)
            except _ReadDeadline:
                if remaining < self.source_timeout_s:
                    row.update(status="pending", reason="run_budget_exhausted")
                else:
                    row.update(status="unavailable", reason="source_timeout")
            except Exception as exc:
                # Never serialize provider exception text (URLs/headers may be secret).
                row.update(status="unavailable", reason=type(exc).__name__)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, prior_handler)
            self.save()
            if row["status"] == "pending":
                raise SourceDeferred(row["reason"])
            if row["status"] != "ok":
                raise SourceUnavailable(row["reason"])
            return result
        bounded.discovery_checkpoint = self
        return bounded

    def save(self):
        private_json(self.path, self.doc)

    def finish(self):
        for key, row in self.doc["operations"].items():
            if key not in self.visited and row["status"] == "pending":
                row.update(status="retired", reason="no_longer_in_current_input_plan")
        operations = list(self.doc["operations"].values())
        pending = [row for row in operations if row["status"] == "pending"]
        unavailable = [row for row in operations if row["status"] == "unavailable"]
        self.doc["complete"] = not pending
        self.save()
        return {"checkpoint": str(self.path), "cycle_id": self.doc["cycle_id"],
                "complete": not pending, "calls_this_run": self.calls,
                "operations": len(operations), "pending": pending,
                "unavailable": unavailable,
                "status": "PARTIAL_RESUMABLE" if pending else (
                    "DISCOVERED_WITH_SOURCE_ERRORS" if unavailable else "DISCOVERED")}


def host_of(url: str) -> str:
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def public_http_url(url: str) -> tuple[bool, str]:
    """Fail-closed syntactic URL gate used before every injected scrape.

    This intentionally does not resolve DNS (which would itself be provider I/O),
    but it rejects literal private/loopback/link-local hosts, localhost-style
    names, userinfo/credentials, hostile schemes, malformed ports, and controls.
    """
    raw = str(url or "").strip()
    if not raw or len(raw) > 4096 or any(ord(ch) < 32 for ch in raw):
        return False, "empty_oversized_or_control_character"
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        _ = parsed.port
    except (ValueError, UnicodeError):
        return False, "malformed_url"
    if parsed.scheme.lower() not in {"http", "https"}:
        return False, "non_http_scheme"
    if parsed.username is not None or parsed.password is not None:
        return False, "userinfo_or_credentials_forbidden"
    if not host or host in {"localhost", "localhost.localdomain"} \
            or host.endswith((".localhost", ".local", ".internal", ".home", ".lan")):
        return False, "local_or_missing_host"
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    # Python's strict ``ipaddress`` parser rejects legacy IPv4 spellings that
    # URL/socket stacks may still resolve as addresses: a 32-bit integer,
    # shortened dotted forms, or octal/hex components. Treat every all-numeric
    # 1-4 component legacy form as an IP literal and reject it rather than
    # allowing it to reach an injected network-capable scraper.
    legacy_ipv4_parts = host.split(".")
    legacy_ipv4_component = re.compile(
        r"(?:0[xX][0-9A-Fa-f]+|0[0-7]*|[0-9]+)\Z"
    )
    if ip is None and 1 <= len(legacy_ipv4_parts) <= 4 \
            and all(legacy_ipv4_component.fullmatch(part)
                    for part in legacy_ipv4_parts):
        return False, "noncanonical_ip_literal"
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local
                           or ip.is_reserved or ip.is_unspecified or ip.is_multicast):
        return False, "private_loopback_or_reserved_host"
    return True, ""


def safe_json_artifact(path: Path, *, root: Path, max_bytes: int = MAX_SOURCE_JSON_BYTES) -> tuple[dict | list | None, str]:
    """Read one bounded JSON artifact without following links or escaping root."""
    path, root = Path(path).absolute(), Path(root).absolute()
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None, "path_escape"
    cursor = root
    if root.is_symlink() or not root.is_dir():
        return None, "unsafe_source_root"
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return None, "symlink_rejected"
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except (FileNotFoundError, OSError):
        return None, "missing_or_unreadable"
    if not stat.S_ISREG(mode):
        return None, "special_or_nonregular_file"
    size = path.stat(follow_symlinks=False).st_size
    if size <= 0 or size > int(max_bytes):
        return None, "empty_or_oversized_json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None, "malformed_json"
    if not isinstance(payload, (dict, list)):
        return None, "json_root_not_object_or_array"
    return payload, "ok"


def registrable(url_or_host: str) -> str:
    """Best-effort registrable domain — good enough for dedup, no PSL needed."""
    host = url_or_host if "://" not in str(url_or_host) else host_of(url_or_host)
    host = str(host).lower().strip().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return host
    # co.uk / com.au style suffixes
    if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org", "gov", "ac"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_tracking_url(url: str) -> bool:
    """True when following this URL would fire an affiliate attribution event."""
    low = str(url or "").lower()
    if not low:
        return True
    return any(marker in low for marker in TRACKING_MARKERS)


def tracking_identity(url: str, label: str = "") -> dict:
    """Derive network/provider hints from tracker bytes without requesting it."""
    raw = str(url or "").strip()
    network = platform_for_url(raw) or hosting_network(raw) or "unknown"
    destination = ""
    try:
        values = parse_qs(urlparse(raw).query, keep_blank_values=False)
    except ValueError:
        values = {}
    for key in ("ued", "url", "destination", "dest", "redirect", "redirect_url", "landing_page"):
        for value in values.get(key, []):
            candidate = unquote(str(value or "")).strip()
            safe, _ = public_http_url(candidate)
            if safe and not is_tracking_url(candidate):
                destination = candidate
                break
        if destination:
            break
    core = domain_core(destination) if destination else ""
    provider = name_from_text(label, core) if core else ""
    if not provider:
        cleaned = re.sub(r"(?i)\b(?:affiliate|partner|program|apply|join|click|learn more)\b", " ",
                         str(label or ""))
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:|")
        if 1 <= len(cleaned.split()) <= 8 and len(cleaned) <= 80:
            provider = cleaned
    if not provider and core:
        provider = core.title()
    return {"network": network, "provider": provider, "destination_url": destination,
            "tracking_url_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "tracking_url_requested": False}


def platform_for_url(url: str) -> str | None:
    """The affiliate network this URL belongs to, or None for a provider's own site."""
    host = host_of(url)
    if not host:
        return None
    for platform_host, network in PLATFORM_HOSTS.items():
        if host == platform_host or host.endswith("." + platform_host):
            return network
    return None


# Platform brand tokens are only safe to match in free text when they are not
# ordinary English. "impact" and "everflow" would fire on any page that says
# "high impact"; those networks are recognised by their full host only.
GENERIC_PLATFORM_TOKENS = {"impact", "impactradius", "prf", "cj", "sovrn", "levanta",
                           "ascendpartner", "adcombo", "gasmobi"}
PLATFORM_BRAND_TOKENS = tuple(sorted(
    {(host.split(".")[0], network) for host, network in PLATFORM_HOSTS.items()
     if host.split(".")[0] not in GENERIC_PLATFORM_TOKENS and len(host.split(".")[0]) >= 6}))


def hosting_network(text: str) -> str | None:
    """Which affiliate network a program page says it runs on, if any.

    A provider whose own page says "we run our program on ShareASale" is a
    ShareASale program, not a direct one — recording it as direct would send the
    apply lane to a signup form that does not exist.
    """
    low = str(text or "").lower()
    for platform_host, network in PLATFORM_HOSTS.items():
        if platform_host in low:
            return network
    for token, network in PLATFORM_BRAND_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", low):
            return network
    return None


def looks_like_program_surface(url: str, title: str = "") -> bool:
    """Does this URL/title look like an affiliate program page rather than prose?"""
    try:
        path = urlparse(str(url)).path or "/"
    except ValueError:
        return False
    if PROGRAM_PATH_RE.search(path):
        return True
    text = str(title or "").lower()
    return "affiliate program" in text or "partner program" in text


# ------------------------------------------------------------ known supply

def _freshest(pattern: str) -> Path | None:
    paths = []
    for path in MACHINE_DIR.glob(pattern):
        try:
            mode = path.stat(follow_symlinks=False).st_mode
            size = path.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        if path.is_symlink() or not stat.S_ISREG(mode) or size <= 0 or size > MAX_SOURCE_JSON_BYTES:
            continue
        paths.append(path)
    paths.sort(key=lambda p: p.stat(follow_symlinks=False).st_mtime)
    return paths[-1] if paths else None


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


@dataclass
class KnownSupply:
    """Everything the machine already knows about — the dedup universe."""
    domains: set = field(default_factory=set)
    names: set = field(default_factory=set)
    ledger_keys: set = field(default_factory=set)
    networks: set = field(default_factory=set)
    sheet_rows: int = 0

    def has(self, *, name: str = "", url: str = "", official_url: str = "") -> str:
        """Return the dedup reason, or "" when this is genuinely new supply."""
        for candidate_url in (official_url, url):
            domain = registrable(candidate_url)
            if domain and domain in self.domains:
                return f"known_domain:{domain}"
        norm = _norm_name(name)
        if norm and norm in self.names:
            return f"known_program_name:{norm[:48]}"
        if f"direct:{name}" in self.ledger_keys:
            return "already_in_applications_ledger"
        return ""


def load_known_supply(*, sheet_export: dict | None = None, inventories: list[dict] | None = None,
                      ledger: dict | None = None) -> KnownSupply:
    """Build the dedup universe from the Sheet, the inventories and the ledger.

    Every argument is injectable so the tests never touch the report tree.
    """
    known = KnownSupply()

    if sheet_export is None:
        path = _freshest("backrun-*/sheet-full-row-export.json")
        sheet_export = (safe_json_artifact(path, root=MACHINE_DIR)[0] if path else {}) or {}
    headers = sheet_export.get("headers") or []
    idx = {h: i for i, h in enumerate(headers)}
    rows = (sheet_export.get("rows") or {})
    known.sheet_rows = len(rows)
    for row in rows.values():
        row = list(row) + [""] * len(headers)
        def cell(header: str) -> str:
            i = idx.get(header, -1)
            return str(row[i]).strip() if 0 <= i < len(row) else ""
        for header in ("Official URL", "Apply URL"):
            domain = registrable(cell(header))
            # A marketplace console URL is not a provider domain: keeping it out
            # of the dedup set is what lets a provider found on Awin still be
            # discovered as a DIRECT program.
            if domain and domain not in PLATFORM_HOSTS:
                known.domains.add(domain)
        if cell("Program / Network"):
            known.names.add(_norm_name(cell("Program / Network")))
        if cell("Network / Route"):
            known.networks.add(cell("Network / Route").strip().lower())

    if inventories is None:
        inventories = []
        for pattern in ("enumeration-*/*.json", "marketplace-adapters-*/*-readback.json",
                        "backrun-*/marketplace/*.json"):
            best: dict[str, Path] = {}
            for path in MACHINE_DIR.glob(pattern):
                if path.name.startswith("_"):
                    continue
                net = path.stem.split("-")[0]
                if net not in best or path.stat().st_mtime > best[net].stat().st_mtime:
                    best[net] = path
            for path in best.values():
                doc, _status = safe_json_artifact(path, root=MACHINE_DIR)
                if isinstance(doc, dict):
                    inventories.append(doc)
    for doc in inventories:
        for program in (doc.get("programs") or []):
            known.names.add(_norm_name(program.get("name")))
            domain = registrable(str(program.get("url") or ""))
            if domain and domain not in PLATFORM_HOSTS:
                known.domains.add(domain)
        for buckets in (doc.get("relationships") or {}).values():
            for bucket in buckets.values():
                for program in (bucket or []):
                    known.names.add(_norm_name(program.get("name")))

    if ledger is None:
        doc, _status = safe_json_artifact(LEDGER_PATH, root=MACHINE_DIR) \
            if LEDGER_PATH.exists() else ({}, "missing")
        ledger = doc if isinstance(doc, dict) else {}
    for application in (ledger.get("applications") or []):
        known.ledger_keys.add(f"{application.get('network')}:{application.get('program_id')}")
        known.names.add(_norm_name(application.get("name")))

    known.names.discard("")
    known.domains.discard("")
    return known


def memberships() -> set:
    """Networks we actually have a RELATIONSHIP with.

    Deliberately not "networks with an adapter module": the prospect adapters
    (`_prospect.py`) exist precisely for marketplaces we have NOT joined, and
    counting them as members would make this lane blind to the supply it was
    built to find. A network is a member when it has a credential — portal or
    API — or when a real (non-prospect) adapter covers it.
    """
    joined = set()
    try:
        sys.path.insert(0, str(BUSINESS))
        from scripts import marketplace_adapters as adapters

        for network in adapters.NETWORKS:
            module = adapters.get(network)
            state = getattr(module, "membership", None)
            if state is None:            # a real adapter, i.e. a real relationship
                joined.add(network)
                continue
            live = state()
            portal_credential = live.get("portal_credential_present")
            if "network_specific_credential_present" in live:
                portal_credential = live["network_specific_credential_present"]
            if portal_credential or live.get("api_credential_present"):
                joined.add(network)
    except Exception:  # noqa: BLE001 - discovery must survive a broken adapter tree
        pass
    joined.update(sheet_networks())
    joined = {str(n).lower() for n in joined}
    # A network that merged into one we belong to is already ours.
    joined.update(network for network, target in MERGED_NETWORKS.items() if target in joined)
    return joined


def sheet_networks(sheet_export: dict | None = None, catalog: dict | None = None) -> set:
    """Marketplaces the Sheet already records a route against.

    MaxBounty and FlexOffers have no adapter and no secrets directory, but the
    Sheet carries rows for both (one blocked on operator identity, one declined
    us). Re-registering on a network we are already recorded against would burn
    an attempt and risk a duplicate-account flag, so the Sheet is part of the
    membership answer.
    """
    if sheet_export is None:
        path = _freshest("backrun-*/sheet-full-row-export.json")
        if not path:
            return set()
        try:
            sheet_export = json.loads(path.read_text())
        except (OSError, ValueError):
            return set()
    headers = sheet_export.get("headers") or []
    if "Network / Route" not in headers:
        return set()
    column = headers.index("Network / Route")
    routes = " | ".join(
        str((list(row) + [""] * len(headers))[column]).lower()
        for row in (sheet_export.get("rows") or {}).values())
    return {slug for slug in (catalog or MARKETPLACE_CATALOG)
            if re.search(rf"\b{re.escape(slug)}\b", routes)}


# ------------------------------------------------------------- discovery 1

@dataclass
class Hit:
    url: str
    title: str
    description: str
    query: str


def run_search(search, queries, *, limit: int = 10, deadline: float | None = None,
               log: list | None = None) -> list[Hit]:
    """Execute the query pack. `search(query, limit) -> [{url,title,description}]`."""
    hits: list[Hit] = []
    for query in queries:
        if deadline is not None and time.monotonic() >= deadline:
            if log is not None:
                log.append({"query": query, "status": "skipped_time_budget"})
            continue
        try:
            results = search(query, limit) or []
            status, count = "ok", len(results)
        except Exception as exc:  # noqa: BLE001 - one bad query never kills the sweep
            results, status, count = [], f"error:{type(exc).__name__}", 0
        if log is not None:
            log.append({"query": query, "status": status, "results": count})
        for result in results:
            hits.append(Hit(url=str(result.get("url") or ""),
                            title=str(result.get("title") or ""),
                            description=str(result.get("description") or ""),
                            query=query))
    return hits


def glp_evidence(name: str, text: str, url: str = "") -> dict:
    """Screen a find with the SAME GLP gate the apply lane applies."""
    verdict = nfilter.glp_application_fit(nfilter.Candidate(
        name=name, network="direct", program_id=name,
        description=str(text)[:4000], region="US", url=url))
    return {"fit": bool(verdict.fit), "tier": verdict.tier,
            "reasons": list(verdict.reasons),
            "off_verticals": list(verdict.off_verticals_in_name or verdict.off_verticals_in_text)}


def domain_core(url: str) -> str:
    return registrable(url).split(".")[0].replace("-", "")


def name_from_text(text: str, core: str, *, max_words: int = 8) -> str:
    """The word-window in `text` that spells `core`, or "".

    The DOMAIN is the truth ("orderlymeds.com"); prose only supplies the pretty
    spelling of it. Deriving a name from free text alone is how "Join The
    Affiliate Program at Orderly Meds" becomes a provider called "The", so a
    fragment is accepted only when it actually spells the domain's core token.
    """
    if not core:
        return ""
    words = [w for w in re.split(r"[\s|·:–—]+", str(text or "")) if w.strip(" ,.")]
    for size in range(min(max_words, len(words)), 0, -1):
        for start in range(len(words) - size + 1):
            window = " ".join(words[start:start + size]).strip(" ,.:-")
            if window and _norm_name(window) == core:
                return window[:60]
    return ""


def provider_name(hit: Hit) -> str:
    """The provider's brand, from the search title where it spells the domain."""
    core = domain_core(hit.url)
    if not core:
        return ""
    return name_from_text(hit.title, core, max_words=4) or core.title()


HEADING_RE = re.compile(r"(?m)^#{1,3}\s+(.{3,140})$")


def better_name_from_page(body: str, core: str) -> str:
    """The brand as the provider's OWN page spells it.

    A domain like `centerformedicalweightloss.com` has no word boundaries, so
    the fallback name is the unreadable "Centerformedicalweightloss" — and the
    downstream fit gate, which looks for "weight loss" IN THE NAME, cannot see
    a weight-loss clinic in it. The page's own H1 has the spacing.
    """
    for heading in HEADING_RE.findall(str(body or ""))[:10]:
        found = name_from_text(heading, core)
        if found:
            return found
    return name_from_text(str(body or "")[:400], core)


def to_candidate(hit: Hit) -> dict | None:
    """Normalise one search hit into a supply candidate, or drop it.

    Returns None for anything that cannot be an application surface: a tracking
    URL, a forum/aggregator page, an editorial page about programs, or a page
    with no program shape at all.
    """
    url = hit.url.strip()
    safe, _ = public_http_url(url)
    if not safe or is_tracking_url(url):
        return None
    host = host_of(url)
    if not host or any(host == h or host.endswith("." + h) for h in NON_PROGRAM_HOSTS):
        return None
    if not looks_like_program_surface(url, hit.title):
        return None
    if is_editorial(url, hit.title):
        return None
    network = platform_for_url(url) or "direct"
    name = provider_name(hit)
    if not name:
        return None
    text = f"{hit.title} {hit.description}"
    official = f"https://{registrable(url)}/" if network == "direct" else ""
    try:
        path = urlparse(url).path or "/"
    except ValueError:
        path = "/"
    return {
        "provider": name,
        "program_url": url,
        "official_url": official,
        "network": network,
        "signup_surface": "native_form" if network == "direct" else "marketplace_program",
        "domain": registrable(url),
        "discovered_by": hit.query,
        "title": hit.title[:200],
        "snippet": hit.description[:500],
        "glp_evidence": glp_evidence(name, text, url),
        "evidence_source": "search_result",
        "has_signup_path": bool(SIGNUP_PATH_RE.search(path)),
    }


def enrich(candidate: dict, scrape) -> dict:
    """Fetch the program page for real GLP evidence and terms. Never a tracker."""
    url = candidate["program_url"]
    safe, reason = public_http_url(url)
    if not safe:
        candidate["enrichment"] = {"status": f"refused_unsafe_url:{reason}"}
        return candidate
    if is_tracking_url(url):
        candidate["enrichment"] = {"status": "refused_tracking_url"}
        return candidate
    try:
        page = scrape(url) or {}
    except Exception as exc:  # noqa: BLE001
        candidate["enrichment"] = {"status": f"error:{type(exc).__name__}"}
        return candidate
    if str(page.get("status")) != "ok":
        candidate["enrichment"] = {"status": str(page.get("status") or "unavailable")}
        return candidate
    markdown = str(page.get("markdown") or "")
    body = markdown[:20000]
    hosted_by = hosting_network(body)
    # Re-spell the brand from the page itself. Safe for dedup: any name this
    # returns normalises to the same domain core the staged name did.
    better = better_name_from_page(body, domain_core(url))
    if better and better != candidate["provider"]:
        candidate["provider_from_domain"] = candidate["provider"]
        candidate["provider"] = better
    verdict = glp_evidence(candidate["provider"], f"{candidate['title']} {body}", url)
    excerpt = _evidence_excerpt(body)
    if verdict["fit"] and not excerpt:
        # The page's GLP-1 tokens all sit in disclaimers. A provider that says
        # "we do not compound GLP-1s" is not supply, however many times it says
        # "GLP-1" while saying so.
        verdict = {"fit": False, "tier": "none",
                   "reasons": ["glp_claim_only_in_negated_context",
                               "no_affirmative_glp1_or_weight_loss_sentence_on_page"],
                   "off_verticals": verdict["off_verticals"]}
    candidate["glp_evidence"] = verdict
    candidate["evidence_source"] = "program_page"
    candidate["evidence_sha256"] = hashlib.sha256(markdown.encode()).hexdigest()
    candidate["commercial_page_evidence"] = commercial_page_evidence(body)
    candidate["commission_terms"] = _commission_terms(body)
    candidate["evidence_excerpt"] = excerpt
    if hosted_by and candidate["network"] == "direct":
        candidate["network"] = hosted_by
        candidate["signup_surface"] = "marketplace_program"
    candidate["enrichment"] = {"status": "ok", "chars": len(markdown),
                               "hosted_by": hosted_by}
    return candidate


def commercial_page_evidence(body: str) -> dict:
    """Capture affirmative service and recruitment statements, never infer them.

    Only enrich() calls this on a successful first-party program-page read.
    Search snippets, a US mention, or an /affiliate URL alone prove neither.
    Adverse statements outrank positive language elsewhere on the same page.
    """
    us = r"(?:United States|USA|U\.S\.A?\.?|all 50 states|US)"
    sentences = [part.strip() for part in re.split(r"\n+|(?<=[!?])\s+|(?<=\.)\s+(?=[A-Z])", body)
                 if part.strip()]
    negatives = [part for part in sentences if re.search(r"\b" + us + r"\b", part, re.I)
                 and re.search(r"\b(?:not|never|cannot|can't|don't|except|excluding|outside|unavailable)\b", part, re.I)]
    service = None
    if not negatives:
        for part in sentences:
            if re.search(r"\b(?:serve|serving|available|deliver|ship|based|located|operate|operating)\b"
                         + r"[^.!?\n]{0,100}\b" + us + r"\b", part, re.I):
                service = part[:600]
                break
    status, quote = "", None
    for part in sentences:
        if re.search(r"\baffiliate (?:program|applications?)\b", part, re.I) and re.search(
                r"\b(?:closed|paused|suspended|discontinued|not accepting|no longer accepting)\b", part, re.I):
            status, quote = "paused", part[:600]
            break
    if not status:
        for part in sentences:
            if re.search(r"\b(?:join|apply to|apply for)\s+(?:our\s+|the\s+)?affiliate program\b"
                         r"|\b(?:now accepting|accepting new) affiliate applications\b"
                         r"|\baffiliate program is (?:active|open)\b", part, re.I):
                status, quote = "active", part[:600]
                break
    return {"us_service_evidence": service, "offer_status": status,
            "offer_status_evidence": quote}


COMMISSION_RE = re.compile(
    r"(?i)(\$\s?\d{1,4}(?:\.\d{2})?\s*(?:per|/)\s*(?:sale|referral|customer|order|conversion)"
    r"|\d{1,2}(?:\.\d+)?\s*%\s*(?:commission|per sale|of (?:each|every) sale|recurring))")


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")

# "We do NOT compound GLP-1s (semaglutide, tirzepatide...)" is a sentence packed
# with GLP-1 tokens that means the exact opposite of a GLP-1 offering. The
# shared fit gate is polarity-blind — it counts tokens — so a provider that
# explicitly disclaims the vertical would otherwise be filed as supply. Proven
# live on valorcompounding.com, 2026-08-06.
NEGATION_CUE_RE = re.compile(
    r"(?i)\b(?:do(?:es)?\s+not|do\s?n'?t|does\s?n'?t|did\s+not|cannot|can\s?n?'?t|"
    r"will\s+not|won'?t|no\s+longer|never|nor|neither|we\s+are\s+not|is\s+not|"
    r"are\s+not|without|excludes?|unable\s+to)\b")


def _is_negated_claim(sentence: str) -> bool:
    """True when the GLP-1/weight-loss token sits inside a DISCLAIMER.

    Scope is the whole sentence up to the token, because the disclaimer's verb
    and the token can be a long list apart:
        "We do NOT make peptides, IV nutrients or vitamins, growth hormones,
         nor semiglutide and other weight loss compounds."
    A window-based rule missed that by 100 characters.
    """
    match = (nfilter.GLP1_EXPLICIT_RE.search(sentence)
             or nfilter.WEIGHT_LOSS_RE.search(sentence))
    if not match:
        return False
    return bool(NEGATION_CUE_RE.search(sentence[:match.start()]))


def _evidence_excerpt(text: str, limit: int = 460) -> str:
    """The actual page sentences that make this a GLP-1/weight-loss offering.

    This travels with the record into the apply lane's inventory. Without it the
    apply lane re-runs the GLP gate against a 160-character SEARCH SNIPPET and
    reaches a different verdict than the gate that accepted the program here —
    which is how Center for Medical Weight Loss got discovered and then dropped.
    """
    picked: list[str] = []
    for sentence in SENTENCE_SPLIT_RE.split(str(text or "")):
        sentence = " ".join(sentence.split())
        if len(sentence) < 20 or sentence.startswith(("#", "|", "-", "*", "[")):
            continue
        if not (nfilter.GLP1_EXPLICIT_RE.search(sentence)
                or nfilter.WEIGHT_LOSS_RE.search(sentence)):
            continue
        if _is_negated_claim(sentence):
            continue          # the sentence DISCLAIMS the vertical
        if sentence not in picked:
            picked.append(sentence)
        if sum(len(s) + 1 for s in picked) >= limit:
            break
    return " ".join(picked)[:limit]


def _commission_terms(text: str) -> str:
    found = []
    for match in COMMISSION_RE.finditer(text):
        value = re.sub(r"\s+", " ", match.group(0)).strip()
        if value not in found:
            found.append(value)
        if len(found) >= 3:
            break
    return "; ".join(found)


MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]{1,120})\]\((https?://[^\s)]+)\)")


def mine_editorial(url: str, scrape, *, source_query: str = "") -> list:
    """Harvest the programs a review/listicle/disclosure page NAMES.

    Competitor review sites and FTC disclosure pages are the open web's index of
    who runs an affiliate program — the operator asked for exactly this angle.
    Only outbound links whose own path looks like a program surface are taken,
    and a tracking link is never followed or emitted.
    """
    safe, _ = public_http_url(url)
    if not safe or is_tracking_url(url):
        return []
    try:
        page = scrape(url) or {}
    except Exception:  # noqa: BLE001
        return []
    if str(page.get("status")) != "ok":
        return []
    source_host = host_of(url)
    seen: set = set()
    found: list = []
    for anchor, link in MARKDOWN_LINK_RE.findall(str(page.get("markdown") or "")[:120000]):
        link = link.rstrip(").,")
        link_safe, _ = public_http_url(link)
        if not link_safe or is_tracking_url(link):
            continue
        host = host_of(link)
        if not host or host == source_host:
            continue
        if any(host == h or host.endswith("." + h) for h in NON_PROGRAM_HOSTS):
            continue
        if not looks_like_program_surface(link, anchor) or is_editorial(link, anchor):
            continue
        domain = registrable(link)
        if domain in seen:
            continue
        seen.add(domain)
        found.append(Hit(url=link, title=anchor.strip(),
                         description=f"named as an affiliate program by {source_host}",
                         query=f"mined:{source_host}"
                               + (f" <- {source_query}" if source_query else "")))
    return found


def discover_direct_programs(search, scrape, *, known: KnownSupply, queries=None,
                             limit: int = 10, target: int = DEFAULT_DISCOVERY_TARGET,
                             gap: int | None = None,
                             deadline: float | None = None, enrich_cap: int =60,
                             mine_cap: int = 25, policy_snapshot=None,
                             initial_hits: list[Hit] | None = None) -> dict:
    """Route 1 — find provider-run affiliate programs we do not already have."""
    if gap is not None:  # compatibility for provider-free tests and old callers
        target = gap
    policy_snapshot = policy_snapshot or operator_rulings.load_snapshot()
    if getattr(scrape, "discovery_checkpoint", None):
        # Durable read budgets replace lossy count caps. Every source is visited
        # and receives either a result or an explicit pending/error disposition.
        enrich_cap = mine_cap = float("inf")
    query_log: list = []
    hits = list(initial_hits or [])
    if queries is not None or initial_hits is None:
        effective_queries = DIRECT_QUERIES if queries is None else tuple(queries)
        hits.extend(run_search(search, effective_queries, limit=limit,
                               deadline=deadline, log=query_log))
    seen: set = set()
    seen_domains: dict[str, str] = {}
    seen_names: dict[str, str] = {}
    rejected: list = []
    staged: list = []
    editorial: list = []

    def stage(hit: Hit) -> None:
        candidate = to_candidate(hit)
        if candidate is None:
            if is_editorial(hit.url, hit.title) and looks_like_program_surface(hit.url, hit.title) \
                    and not is_tracking_url(hit.url):
                editorial.append(hit)
                rejected.append({"url": hit.url, "reason": "editorial_source_queued_for_mining",
                                 "query": hit.query})
            else:
                rejected.append({"url": hit.url, "reason": "not_a_program_surface",
                                 "query": hit.query})
            return
        key = (candidate["domain"], _norm_name(candidate["provider"]))
        if key in seen:
            rejected.append({"url": candidate["program_url"], "provider": candidate["provider"],
                             "reason": "duplicate_source_identity", "query": candidate["discovered_by"]})
            return
        domain, norm_name = key
        if domain and domain in seen_domains and seen_domains[domain] != norm_name:
            rejected.append({"url": candidate["program_url"], "provider": candidate["provider"],
                             "reason": f"conflicting_source_identity:domain:{domain}",
                             "query": candidate["discovered_by"]})
            return
        if norm_name and norm_name in seen_names and seen_names[norm_name] != domain:
            rejected.append({"url": candidate["program_url"], "provider": candidate["provider"],
                             "reason": f"conflicting_source_identity:name:{norm_name[:48]}",
                             "query": candidate["discovered_by"]})
            return
        seen.add(key)
        if domain:
            seen_domains[domain] = norm_name
        if norm_name:
            seen_names[norm_name] = domain
        dup = known.has(name=candidate["provider"], url=candidate["program_url"],
                        official_url=candidate["official_url"])
        if dup:
            rejected.append({"url": candidate["program_url"], "provider": candidate["provider"],
                             "reason": dup, "query": candidate["discovered_by"]})
            return
        staged.append(candidate)

    for hit in hits:
        stage(hit)

    # MINE the editorial pages: a "10 best GLP-1 affiliate programs" post and a
    # competitor's FTC disclosure page both name real programs, and those names
    # are the supply. The pages themselves are never recorded as programs.
    mined = 0
    mined_hosts: set = set()
    for hit in editorial:
        if len(mined_hosts) >= mine_cap or (deadline is not None and time.monotonic() >= deadline):
            break
        host = host_of(hit.url)
        if host in mined_hosts:
            continue
        mined_hosts.add(host)
        for found in mine_editorial(hit.url, scrape, source_query=hit.query):
            mined += 1
            stage(found)

    # Enrich the best-looking ones first: a real signup path and a search-stage
    # GLP hit are the two cheap signals that a scrape will pay for itself.
    staged.sort(key=lambda c: (not c["glp_evidence"]["fit"], not c["has_signup_path"],
                               c["provider"].lower()))
    accepted: list = []
    enriched = 0
    for candidate in staged:
        if enriched < enrich_cap and (deadline is None or time.monotonic() < deadline):
            enrich(candidate, scrape)
            enriched += 1
        policy = policy_snapshot.evaluate_program(candidate.get("provider"))
        if policy is not None and policy.verdict == "HOLD":
            rejected.append({"url": candidate["program_url"], "provider": candidate["provider"],
                             "reason": f"operator_ruling:{policy.instruction_key}",
                             "query": candidate["discovered_by"]})
        elif candidate["glp_evidence"]["fit"]:
            accepted.append(candidate)
        else:
            rejected.append({"url": candidate["program_url"], "provider": candidate["provider"],
                             "reason": "not_glp_fit:" + ";".join(candidate["glp_evidence"]["reasons"][:2]),
                             "query": candidate["discovered_by"]})
    accepted.sort(key=lambda c: (c["evidence_source"] != "program_page",
                                 c["network"] != "direct", c["provider"].lower()))
    return {
        "schema": "affiliate-supply-direct-discovery-v1",
        "captured_at_utc": utc(),
        "candidate_target": int(target),
        "queries_run": query_log,
        "hits": len(hits),
        "editorial_pages_mined": len(mined_hosts),
        "editorial_links_harvested": mined,
        "staged": len(staged),
        "enriched": enriched,
        "accepted_count": len(accepted),
        "accepted": accepted,
        "rejected_count": len(rejected),
        "rejected": rejected,
        "effects": {"applications_submitted": 0, "tracking_links_clicked": 0, "sheet_writes": 0},
        "secret_printed": False,
    }


def seed_queries_from_sheet(sheet_export: dict, known: KnownSupply, *, cap: int = MAX_SEED_QUERIES) -> list:
    """`<provider> affiliate program` for GLP-vertical Sheet rows on a marketplace
    route — the provider may also run a direct program that pays more."""
    headers = sheet_export.get("headers") or []
    idx = {h: i for i, h in enumerate(headers)}
    out: list = []
    for row in (sheet_export.get("rows") or {}).values():
        row = list(row) + [""] * len(headers)
        def cell(header: str) -> str:
            i = idx.get(header, -1)
            return str(row[i]).strip() if 0 <= i < len(row) else ""
        if cell("Vertical").lower() != "glp1-weightloss":
            continue
        name = cell("Program / Network")
        route = cell("Network / Route").lower()
        if not name or "direct" in route:
            continue
        query = SEED_QUERY_TEMPLATE.format(provider=name)
        if query not in out:
            out.append(query)
        if len(out) >= cap:
            break
    return out


# ------------------------------------------------------------- discovery 2

# Marketplaces we know how to talk about. `signup_url` is the PUBLISHER signup
# surface; `human_gate` records a known non-self-service step so the lane can
# emit an accurate unblock card without burning an attempt.
# Networks that no longer exist independently. Proven live 2026-08-06: the
# ShareASale publisher signup URL 302s to `awin.com/us/getting-started-sas`, so
# ShareASale's merchant base is reachable through the Awin publisher account we
# already hold (2979387). Attempting to "join" a merged network wastes an
# attempt and risks a duplicate publisher identity.
MERGED_NETWORKS: dict[str, str] = {"shareasale": "awin"}

MARKETPLACE_CATALOG: dict[str, dict] = {
    "shareasale": {"display": "ShareASale (merged into Awin)", "host": "shareasale.com",
                   "signup_url": "https://www.awin.com/us/getting-started-sas",
                   "merged_into": "awin",
                   "glp_note": "large US health/supplement merchant base, now served "
                               "through the existing Awin publisher account"},
    "flexoffers": {"display": "FlexOffers", "host": "flexoffers.com",
                   "signup_url": "https://publishers.flexoffers.com/signup",
                   "glp_note": "aggregates health/telehealth advertisers"},
    "maxbounty": {"display": "MaxBounty", "host": "maxbounty.com",
                  "signup_url": "https://www.maxbounty.com/affiliates-signup.cfm",
                  "human_gate": "affiliate manager phone/interview approval",
                  "glp_note": "CPA network with weight-loss offers"},
    "avantlink": {"display": "AvantLink", "host": "avantlink.com",
                  "signup_url": "https://classic.avantlink.com/affiliate_signup.php",
                  "glp_note": "health and outdoor merchants"},
    "pepperjam": {"display": "Ascend by Partnerize (Pepperjam)", "host": "pepperjam.com",
                  "signup_url": "https://www.pepperjam.com/publishers",
                  "glp_note": "US retail/health advertisers"},
    "linkconnector": {"display": "LinkConnector", "host": "linkconnector.com",
                      "signup_url": "https://www.linkconnector.com/affiliate-signup.php",
                      "human_gate": "manual publisher vetting call",
                      "glp_note": "health/nutra merchants"},
    "webgains": {"display": "Webgains", "host": "webgains.com",
                 "signup_url": "https://advertisers.webgains.com/affiliate/signup",
                 "glp_note": "health advertisers, mostly EU"},
    "admitad": {"display": "Admitad", "host": "admitad.com",
                "signup_url": "https://www.admitad.com/en/webmaster/signup/",
                "glp_note": "global nutra/health offers"},
    "crakrevenue": {"display": "CrakRevenue", "host": "crakrevenue.com",
                    "signup_url": "https://www.crakrevenue.com/signup/",
                    "glp_note": "health/nutra CPA offers"},
    "drcash": {"display": "Dr.Cash", "host": "dr-cash.com",
               "signup_url": "https://dr.cash/registration/",
               "glp_note": "nutra-only network, weight-loss vertical"},
    "clickdealer": {"display": "ClickDealer", "host": "clickdealer.com",
                    "signup_url": "https://clickdealer.com/affiliates/",
                    "human_gate": "manager review before dashboard access",
                    "glp_note": "health/nutra CPA offers"},
    "uppromote": {"display": "UpPromote Marketplace", "host": "uppromote.com",
                  "signup_url": "https://marketplace.uppromote.com/affiliate/register",
                  "glp_note": "Shopify DTC health brands"},
    "goaffpro": {"display": "GoAffPro", "host": "goaffpro.com",
                 "signup_url": "https://goaffpro.com/marketplace",
                 "glp_note": "Shopify DTC health brands"},
}


def discover_marketplaces(hits, *, joined: set, catalog: dict | None = None,
                          extra_hits=None) -> dict:
    """Route 2 — rank affiliate networks we are NOT in by observed GLP supply."""
    catalog = catalog or MARKETPLACE_CATALOG
    joined = {str(n).lower() for n in joined}
    signals: dict[str, dict] = {}
    uncatalogued: dict[str, dict] = {}
    for hit in list(hits) + list(extra_hits or []):
        text = f"{hit.title} {hit.description}".lower()
        network = platform_for_url(hit.url)
        mentioned = {network} if network else set()
        for platform_host, slug in PLATFORM_HOSTS.items():
            if platform_host.split(".")[0] in text:
                mentioned.add(slug)
        glp_signal = bool(nfilter.GLP1_EXPLICIT_RE.search(text) or nfilter.WEIGHT_LOSS_RE.search(text))
        for slug in mentioned:
            if not slug or slug in joined:
                continue
            if slug not in catalog:
                # An affiliate platform we can recognise but have no publisher
                # signup surface for. Inventing a registration endpoint is not
                # allowed, so the gap itself is the finding: it needs a catalog
                # entry (a documented signup URL) before the lane can join it.
                row = uncatalogued.setdefault(slug, {"network": slug, "glp_signals": 0,
                                                     "mentions": 0, "evidence": []})
                row["mentions"] += 1
                row["glp_signals"] += int(glp_signal)
                if hit.url and len(row["evidence"]) < 3 and not is_tracking_url(hit.url):
                    row["evidence"].append({"url": hit.url, "query": hit.query})
                continue
            row = signals.setdefault(slug, {"network": slug, "glp_signals": 0,
                                            "mentions": 0, "evidence": []})
            row["mentions"] += 1
            if glp_signal:
                row["glp_signals"] += 1
            if hit.url and len(row["evidence"]) < 5 and not is_tracking_url(hit.url):
                row["evidence"].append({"url": hit.url, "query": hit.query})

    ranked = []
    for slug, row in signals.items():
        meta = catalog[slug]
        ranked.append({
            **row,
            "display": meta["display"],
            "signup_url": meta["signup_url"],
            "known_human_gate": meta.get("human_gate", ""),
            "merged_into": meta.get("merged_into", ""),
            "glp_note": meta.get("glp_note", ""),
            "self_join_eligible": not (meta.get("human_gate") or meta.get("merged_into")),
        })
    ranked.sort(key=lambda r: (-r["glp_signals"], -r["mentions"], r["network"]))
    return {
        "schema": "affiliate-supply-marketplace-discovery-v1",
        "captured_at_utc": utc(),
        "already_joined": sorted(joined),
        "candidates_count": len(ranked),
        "candidates": ranked,
        "needs_catalog_entry_count": len(uncatalogued),
        "needs_catalog_entry": sorted(uncatalogued.values(),
                                      key=lambda r: (-r["glp_signals"], -r["mentions"],
                                                     r["network"])),
        "effects": {"signups_attempted": 0},
        "secret_printed": False,
    }


# --------------------------------------------------------- self-join (live)

def attempt_self_join(network: str, *, run_id: str, session_factory=None,
                      headless: bool = True, joined: set | None = None) -> dict:
    """Route 2b — a real publisher signup through the portal lane.

    `session_factory(network)` is injected in tests; live it is
    `portal_lane.signup_session`. Exactly ONE attempt per network per run — the
    portal lane's own AttemptBudget enforces it, and a refusal is a receipt.
    A network we already hold a relationship with is never re-registered: a
    duplicate publisher account is the fastest way to get an ops identity
    flagged.
    """
    joined = memberships() if joined is None else joined
    if str(network).lower() in joined:
        return {"network": network, "status": "ALREADY_REGISTERED",
                "step": "membership_check",
                "reason": "existing_relationship_recorded",
                "operator_action": ""}
    if session_factory is None:
        from scripts.marketplace_adapters import portal_lane  # noqa: PLC0415

        def session_factory(net):
            return portal_lane.signup_session(net, run_id=run_id, headless=headless)

    try:
        session = session_factory(network)
    except Exception as exc:  # noqa: BLE001
        return {"network": network, "status": "BLOCKED_NO_SIGNUP_SPEC",
                "step": "session_open", "reason": type(exc).__name__,
                "operator_action": f"add a signup spec for {network} to portal_lane.SIGNUP_PORTALS"}
    try:
        with session as active:
            result = active.signup()
    except Exception as exc:  # noqa: BLE001
        receipt = getattr(exc, "receipt", None)
        if callable(receipt):
            out = dict(receipt())
            out.setdefault("network", network)
            out.setdefault("status", "BLOCKED")
            return out
        return {"network": network, "status": "BLOCKED", "step": "signup",
                "reason": type(exc).__name__}
    result.setdefault("network", network)
    return result


# ------------------------------------------------------------- unblock card

def unblock_cards(join_results: list, marketplaces: dict | None = None) -> list:
    """One precise operator card per marketplace that refused self-service."""
    cards: list = []
    known_gates = {row["network"]: row for row in (marketplaces or {}).get("candidates", [])}
    for result in join_results:
        status = str(result.get("status") or "")
        if status in {"SIGNED_UP", "ALREADY_REGISTERED"}:
            continue
        network = str(result.get("network") or "unknown")
        meta = known_gates.get(network, {})
        cards.append({
            "marketplace": meta.get("display") or network,
            "network": network,
            "step": str(result.get("step") or "unknown"),
            "reason": str(result.get("reason") or status or "unknown"),
            "operator_action": str(result.get("operator_action")
                                   or meta.get("known_human_gate")
                                   or "complete the blocked step in the portal, then tell the lane to resume"),
            "signup_url": meta.get("signup_url") or str(result.get("url") or ""),
            "glp_signals": meta.get("glp_signals", 0),
            "receipt": str(result.get("receipt_path") or ""),
        })
    cards.sort(key=lambda c: (-int(c.get("glp_signals") or 0), c["network"]))
    return cards


# ---------------------------------------------------- outputs the lanes eat

def carry_forward_supply(ledger: dict | None = None, machine_dir: Path | None = None) -> list:
    """Previously discovered programs the apply lane has NOT applied to yet.

    `freshest_inventories()` keeps only the newest file per network, so a run
    that emitted a fresh `direct-supply.json` would otherwise ERASE yesterday's
    unapplied discoveries from the apply pool the moment it wrote its own. The
    inventory is therefore cumulative: anything still absent from the
    applications ledger rides along into the new file.
    """
    root = machine_dir or MACHINE_DIR
    if ledger is None:
        ledger = json.loads(LEDGER_PATH.read_text()) if LEDGER_PATH.exists() else {}
    applied = {f"{a.get('network')}:{a.get('program_id')}"
               for a in (ledger.get("applications") or [])}
    prior = sorted(root.glob("enumeration-supply-*/direct-supply.json"),
                   key=lambda p: p.stat().st_mtime)
    if not prior:
        return []
    try:
        doc = json.loads(prior[-1].read_text())
    except (OSError, ValueError):
        return []
    return [program for program in (doc.get("programs") or [])
            if f"direct:{program.get('program_id')}" not in applied]


def build_apply_inventory(accepted: list, carried: list | None = None) -> dict:
    """Stable first-party identities with explicitly sourced US service only.

    Public route availability is discovery evidence, never relationship approval.
    Older inventories with inferred US/name IDs remain research history; they
    cannot be carried into the new admission contract.
    """
    programs = []
    seen = set()
    for program in (carried or []):
        key = str(program.get("program_id") or "")
        if (not key.startswith("domain:") or key in seen
                or program.get("discovery_geo_contract") != "explicit_us_v1"):
            continue
        seen.add(key)
        programs.append({**program, "carried_forward": True})
    for candidate in accepted:
        official = str(candidate.get("official_url") or "")
        host = (urlparse(official).hostname or "").lower().removeprefix("www.")
        key = "domain:" + host if host else ""
        if key in seen and key:
            continue
        if key:
            seen.add(key)
        excerpt = str(candidate.get("evidence_excerpt") or "")
        page_verified = (candidate.get("evidence_source") == "program_page"
                         and candidate.get("enrichment", {}).get("status") == "ok"
                         and bool(candidate.get("evidence_sha256")))
        page_evidence = candidate.get("commercial_page_evidence", {}) if page_verified else {}
        us_evidence = page_evidence.get("us_service_evidence")
        programs.append({
            "program_id": key,
            "name": candidate["provider"],
            "description": excerpt,
            "region": "US" if us_evidence else "",
            "currency": None,
            "payout": None,
            "terms": None,
            "discovery_geo_contract": "explicit_us_v1",
            "us_service_evidence": us_evidence,
            "offer_status": page_evidence.get("offer_status", ""),
            "offer_status_evidence": page_evidence.get("offer_status_evidence"),
            "evidence_sha256": candidate.get("evidence_sha256"),
            "official_url": official,
            "url": candidate["program_url"],
            "application_url": candidate["program_url"],
            "relationship": "available",
            "hosted_network": candidate.get("network"),
            "glp_evidence_tier": candidate["glp_evidence"]["tier"],
            "discovered_by": candidate["discovered_by"],
            "evidence_source": candidate["evidence_source"],
        })
    return {
        "schema": "affiliate-supply-discovered-inventory-v2",
        "network": "direct",
        "captured_at_utc": utc(),
        "mode": "supply_discovery",
        "auth_state": "not_applicable_public_web_discovery",
        "programs": programs,
        "effects": {"provider_mutations": 0, "applications_submitted": 0},
        "secret_printed": False,
    }


def build_sheet_candidate_plan(accepted: list, *, run_id: str) -> dict:
    """The plan the allowlisted CAS writer consumes (`--supply-candidates`).

    Every row is a CANDIDATE: `ready_to_apply`, signup not complete, no tracking
    link, no approval date. The writer re-asserts all of that; this is the first
    of the two places it is stated.
    """
    candidates = []
    for candidate in accepted:
        evidence = candidate["glp_evidence"]
        candidates.append({
            "provider": candidate["provider"],
            "program_url": candidate["program_url"],
            "official_url": candidate["official_url"] or f"https://{candidate['domain']}/",
            "network_route": ("Direct affiliate" if candidate["network"] == "direct"
                              else "Network signup"),
            "hosted_network": candidate["network"],
            "vertical": "glp1-weightloss",
            "category": "GLP-1 / metabolic health",
            "fit_reason": (f"Discovered {candidate['evidence_source']}; GLP fit tier "
                           f"{evidence['tier']} ({', '.join(evidence['reasons'][:2])})."),
            "commission_terms": candidate.get("commission_terms", ""),
            "evidence": {
                "tier": evidence["tier"],
                "reasons": evidence["reasons"][:4],
                "source": candidate["evidence_source"],
                "discovered_by": candidate["discovered_by"],
                "sha256": candidate.get("evidence_sha256", ""),
            },
        })
    return {
        "schema": "affiliate-supply-sheet-candidates-v1",
        "captured_at_utc": utc(),
        "run_id": run_id,
        "authority": "operator 2026-08-06 affiliate supply discovery lane",
        "candidate_status": "ready_to_apply",
        "never_marked_approved": True,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def build_research_only_queue_rows(prospects: list, *, capture_path: str) -> list[dict]:
    """Project source-shaped care subjects into the existing Sheet queue only.

    This is intentionally separate from the ready-to-apply inventory and supply
    plan: neither an unknown programme nor a retail CJ action is an application.
    The sole writer independently validates the hash-bound capture and row.
    """
    from urllib.parse import urlsplit

    path = BUSINESS / capture_path
    if not capture_path.startswith("reports/") or ".." in capture_path or not path.is_file():
        raise ValueError("research_capture_path_invalid")
    raw = path.read_bytes()
    if json.loads(raw) != prospects:
        raise ValueError("research_capture_projection_drift")
    digest = hashlib.sha256(raw).hexdigest()
    seen = set()
    rows = []
    for prospect in prospects:
        domain_id = str(prospect.get("provider_id") or "")
        domain = domain_id.removeprefix("domain:")
        observations = prospect.get("source_observations") or []
        if (not domain_id.startswith("domain:") or not domain or domain in seen
                or prospect.get("requested_state") != "research_only_hold"
                or prospect.get("research_intake_eligible") is not True
                or prospect.get("application_eligible") is not False
                or prospect.get("apply") is not False
                or prospect.get("wire_in_ready") is not False
                or prospect.get("wired_live") is not False
                or not prospect.get("unknowns_and_application_hold") or not observations):
            raise ValueError("research_prospect_contract_invalid")
        seen.add(domain)
        official = next((o for o in observations if urlsplit(str(o.get("url") or "")).hostname
                         and (urlsplit(o["url"]).hostname == domain or
                              urlsplit(o["url"]).hostname.endswith("." + domain))), None)
        if not official:
            raise ValueError("research_prospect_domain_unbound")
        if any(not o.get("source_sha256") or not o.get("observed_at") or not o.get("quote")
               for o in observations):
            raise ValueError("research_prospect_source_incomplete")
        for observation in observations:
            source_path = str(observation["source_path"])
            if (not source_path.startswith("reports/") or ".." in source_path
                    or not re.fullmatch(r"[a-f0-9]{64}", str(observation["source_sha256"]))
                    or not (BUSINESS / source_path).is_file()
                    or hashlib.sha256((BUSINESS / source_path).read_bytes()).hexdigest()
                       != observation["source_sha256"]):
                raise ValueError("research_prospect_source_hash_drift")
        # Stable source identity; no run clock or application queue is involved.
        evidence = {
            "research_capture_path": capture_path, "research_capture_sha256": digest,
            "provider_id": domain_id, "official_url": official["url"],
            "observed_at_utc": official["observed_at"],
            "source_observations": observations,
            "known_program_observation": prospect.get("known_program_observation"),
            "application_hold": prospect["unknowns_and_application_hold"],
        }
        rows.append({
            "update_id": "research-provider:" + domain + ":" + digest[:16],
            "kind": "research_only_provider", "network": "Research-only provider",
            "program_id": domain_id, "program_name": prospect["identity"],
            "sheet_row": None, "queued_by": "affiliate_supply_discovery",
            "evidence": json.dumps(evidence, sort_keys=True, ensure_ascii=False),
        })
    return rows


# ------------------------------------------------------------- live wiring

def firecrawl_search():
    """Live search callable. Built lazily so the module stays provider-free."""
    import urllib.request  # noqa: PLC0415

    sys.path.insert(0, str(BUSINESS / "scripts"))
    from glp_price_verification import env_value  # noqa: PLC0415

    key = env_value("FIRECRAWL_API_KEY")

    def search(query: str, limit: int = 10) -> list:
        request = urllib.request.Request(
            "https://api.firecrawl.dev/v1/search",
            data=json.dumps({"query": query, "limit": int(limit)}).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.load(response)
        return [{"url": row.get("url"), "title": row.get("title"),
                 "description": row.get("description")}
                for row in (payload.get("data") or []) if row.get("url")]

    return search


def firecrawl_scrape():
    """Live scrape callable. Refuses tracking URLs before any request is made."""
    import urllib.request  # noqa: PLC0415

    sys.path.insert(0, str(BUSINESS / "scripts"))
    from glp_price_verification import env_value  # noqa: PLC0415

    key = env_value("FIRECRAWL_API_KEY")

    def scrape(url: str) -> dict:
        if is_tracking_url(url):
            return {"status": "refused_tracking_url"}
        request = urllib.request.Request(
            "https://api.firecrawl.dev/v1/scrape",
            data=json.dumps({"url": url, "formats": ["markdown"],
                             "onlyMainContent": True, "timeout": 45000}).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.load(response)
        except Exception as exc:  # noqa: BLE001
            return {"status": f"error:{type(exc).__name__}"}
        markdown = str(((payload.get("data") or {}).get("markdown")) or "")
        if len(markdown) < 200:
            return {"status": "too_thin", "markdown": markdown}
        return {"status": "ok", "markdown": markdown}

    return scrape


# --------------------------------------------------------------------- CLI

def run(*, search, scrape, run_id: str, out_dir: Path,
        target: int = DEFAULT_DISCOVERY_TARGET, gap: int | None = None,
        known: KnownSupply | None = None, seed_from_sheet: bool = True,
        limit: int = 10, time_budget_s: float | None = None,
        do_marketplaces: bool = True, join_network: str = "",
        session_factory=None, headless: bool = True,
        do_five_routes: bool = True, expansion_fixtures: dict | None = None,
        platform_adapters: dict | None = None, checkpoint_path: Path | None = None,
        source_timeout_s: float = DEFAULT_SOURCE_TIMEOUT_SECONDS,
        max_source_calls: int = 200) -> dict:
    """One full discovery run. Writes every receipt; submits and joins nothing."""
    if gap is not None:  # compatibility only; no application-volume trigger remains
        target = gap
    checkpoint = DiscoveryCheckpoint(
        checkpoint_path or out_dir / "checkpoint.json", run_id=run_id,
        budget_s=time_budget_s or DEFAULT_RUN_BUDGET_SECONDS,
        source_timeout_s=source_timeout_s, max_calls=max_source_calls)
    search = checkpoint.wrap("search", search)
    scrape = checkpoint.wrap("scrape", scrape)
    platform_adapters = {
        slug: checkpoint.wrap("catalog:" + slug, adapter) if callable(adapter) else adapter
        for slug, adapter in (platform_adapters or {}).items()}
    # Traversal continues after the budget: wrappers record each pending read,
    # while replayed results remain available to discover the rest of the work.
    deadline = None
    policy_snapshot = operator_rulings.load_snapshot()
    if known is None:
        known = load_known_supply()

    queries = list(DIRECT_QUERIES)
    sheet_source_doc: dict = {}
    if seed_from_sheet:
        path = _freshest("backrun-*/sheet-full-row-export.json")
        if path:
            doc, _status = safe_json_artifact(path, root=MACHINE_DIR)
            if isinstance(doc, dict):
                sheet_source_doc = doc
                queries += seed_queries_from_sheet(doc, known)

    # Item 4 replaces the opaque legacy direct query wave with five attributable
    # receipts. The old wave remains available when callers explicitly disable
    # the expansion, preserving compatibility without double-counting a daily
    # route hit before its named route runs.
    direct_queries = queries if not do_five_routes else ()
    direct = discover_direct_programs(search, scrape, known=known, queries=direct_queries,
                                      limit=limit, target=target, deadline=deadline,
                                      policy_snapshot=policy_snapshot)
    policy_held: list[dict] = []
    accepted_direct = []
    for candidate in direct.get("accepted") or []:
        policy = policy_snapshot.evaluate_program(candidate.get("provider") or candidate.get("name"))
        if policy is not None and policy.verdict == "HOLD":
            policy_held.append({"name": candidate.get("provider") or candidate.get("name"),
                                "instruction_key": policy.instruction_key, "reason": policy.reason,
                                "source": "direct_supply"})
            direct.setdefault("rejected", []).append({**candidate, "reason": f"operator_ruling_hold:{policy.instruction_key}"})
        else:
            accepted_direct.append(candidate)
    direct["accepted"] = accepted_direct
    direct["accepted_count"] = len(accepted_direct)
    direct["rejected_count"] = len(direct.get("rejected") or [])
    direct["operator_rulings"] = operator_rulings.policy_receipt(policy_snapshot)
    private_json(out_dir / "direct-programs.json", direct)
    supply_expansion._mark_known(sys.modules[__name__], known, direct["accepted"])

    # The five new routes are provider-free and share this exact KnownSupply
    # object. As each route admits a candidate it enters the same dedupe universe
    # before the next route starts. The combined accepted set alone feeds the
    # unchanged candidate-plan/apply-inventory builders below.
    expansion = {
        "schema": "affiliate-supply-five-route-run-v1",
        "route_order": [], "routes": {}, "accepted": [],
        "totals": {"found": 0, "fit": 0, "applied": 0, "parked": 0,
                   "evidenced_empty_routes": 0},
        "effects": dict(supply_expansion.ZERO_EFFECTS), "secret_printed": False,
    }
    if do_five_routes:
        fixtures = dict(expansion_fixtures or {})
        if "provider_docs" not in fixtures and sheet_source_doc:
            headers = sheet_source_doc.get("headers") or []
            idx = {header: position for position, header in enumerate(headers)}
            providers = []
            for row in (sheet_source_doc.get("rows") or {}).values():
                values = list(row) + [""] * len(headers)
                def value(header):
                    position = idx.get(header, -1)
                    return str(values[position]).strip() if 0 <= position < len(values) else ""
                if value("Vertical").lower() != "glp1-weightloss":
                    continue
                providers.append({"name": value("Program / Network"),
                                  "url": value("Official URL")})
            fixtures["provider_docs"] = [{"providers": providers}]
        expansion = supply_expansion.run_five_routes(
            sys.modules[__name__], search, scrape, known=known,
            policy_snapshot=policy_snapshot, research_root=SEO_RESEARCH_ROOT,
            newsletter_root=NEWSLETTER_INTAKE_ROOT, fixtures=fixtures,
            platform_adapters=platform_adapters, limit=limit, deadline=deadline)
    private_json(out_dir / "five-route-receipts.json", expansion)

    accepted = list(direct["accepted"]) + list(expansion.get("accepted") or [])
    # Keep the historical aggregate receipt readable for existing local
    # consumers while replacing its opaque source framing with route metadata.
    direct["accepted"] = accepted
    direct["accepted_count"] = len(accepted)
    direct["five_route_receipt"] = str(out_dir / "five-route-receipts.json")
    direct["source_routes"] = list(expansion.get("route_order") or [])
    private_json(out_dir / "direct-programs.json", direct)
    carried = carry_forward_supply()
    inventory = build_apply_inventory(accepted, carried)
    inventory_dir = MACHINE_DIR / f"enumeration-supply-{run_id}"
    private_json(inventory_dir / "direct-supply.json", inventory)

    sheet_plan = build_sheet_candidate_plan(accepted, run_id=run_id)
    private_json(out_dir / "sheet-candidate-plan.json", sheet_plan)

    marketplaces: dict = {}
    join_results: list = []
    if do_marketplaces:
        market_log: list = []
        market_hits = run_search(search, MARKETPLACE_QUERIES, limit=limit,
                                 deadline=deadline, log=market_log)
        direct_hits = [Hit(url=row.get("url", ""), title=row.get("provider", ""),
                           description=row.get("snippet", ""),
                           query=row.get("discovered_by", ""))
                       for row in accepted]
        marketplaces = discover_marketplaces(market_hits, joined=memberships(),
                                             extra_hits=direct_hits)
        candidates = []
        for candidate in marketplaces.get("candidates") or []:
            policy = policy_snapshot.evaluate_program(candidate.get("name") or candidate.get("display") or candidate.get("network"))
            if policy is not None and policy.verdict == "HOLD":
                policy_held.append({"name": candidate.get("name") or candidate.get("display") or candidate.get("network"),
                                    "instruction_key": policy.instruction_key, "reason": policy.reason,
                                    "source": "marketplace_supply"})
                continue
            candidates.append(candidate)
        if "candidates" in marketplaces:
            marketplaces["candidates"] = candidates
            marketplaces["candidates_count"] = len(candidates)
        marketplaces["operator_rulings"] = operator_rulings.policy_receipt(policy_snapshot)
        marketplaces["queries_run"] = market_log
        private_json(out_dir / "marketplaces.json", marketplaces)

    if join_network:
        policy = policy_snapshot.evaluate_program(join_network)
        if policy is not None and policy.verdict == "HOLD":
            policy_held.append({"name": join_network, "instruction_key": policy.instruction_key,
                                "reason": policy.reason, "source": "marketplace_join"})
            join_results.append({"status": "POLICY_HOLD", "network": join_network,
                                 "reason": policy.reason, "instruction_key": policy.instruction_key})
        else:
            join_results.append(attempt_self_join(join_network, run_id=run_id,
                                                  session_factory=session_factory,
                                                  headless=headless))
        private_json(out_dir / "marketplace-join.json",
                     {"schema": "affiliate-supply-marketplace-join-v1",
                      "captured_at_utc": utc(), "results": join_results,
                      "secret_printed": False})

    cards = unblock_cards(join_results, marketplaces)
    private_json(out_dir / "unblock-cards.json",
                 {"schema": "affiliate-supply-unblock-cards-v1",
                  "captured_at_utc": utc(), "count": len(cards), "cards": cards,
                  "route": "discord:1524254375453786294", "secret_printed": False})

    coverage = checkpoint.finish()
    summary = {
        "schema": "affiliate-supply-discovery-summary-v1",
        "status": coverage["status"],
        "source_coverage": coverage,
        "captured_at_utc": utc(),
        "run_id": run_id,
        "candidate_target": int(target),
        "direct_programs_found": len(accepted),
        "unapplied_supply_carried_forward": len(carried),
        "apply_pool_size": len(inventory["programs"]),
        "direct_hits": direct["hits"],
        "direct_rejected": direct["rejected_count"],
        "sheet_candidates_queued": sheet_plan["candidate_count"],
        "supply_routes": {
            route: {key: receipt.get(key) for key in
                    ("status", "found", "fit", "applied", "parked", "evidenced_empty")}
            for route, receipt in (expansion.get("routes") or {}).items()
        },
        "five_route_totals": expansion.get("totals") or {},
        "five_route_receipt": str(out_dir / "five-route-receipts.json"),
        "apply_inventory": str(inventory_dir / "direct-supply.json"),
        "sheet_candidate_plan": str(out_dir / "sheet-candidate-plan.json"),
        "marketplace_candidates": marketplaces.get("candidates_count", 0),
        "marketplace_join_results": join_results,
        "policy_held": policy_held,
        "operator_rulings": operator_rulings.policy_receipt(policy_snapshot),
        "unblock_cards": cards,
        "out_dir": str(out_dir),
        "effects": {"applications_submitted": 0, "sheet_writes": 0,
                    "tracking_links_clicked": 0, "outreach_sent": 0},
        "secret_printed": False,
    }
    private_json(out_dir / "summary.json", summary)
    return summary


def live_platform_discovery_adapters() -> dict:
    """Build authenticated read-only discovery adapters for the live CLI only.

    Importing the core remains provider-free. Partnerize is the one researched
    platform with an official partner-side Brand Discovery v2 endpoint; its
    adapter is therefore imported and constructed only if that route executes.
    Tests inject fixtures and never invoke this callback.
    """
    def partnerize_v2_discovery():
        scripts_dir = str(BUSINESS / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from marketplace_adapters import _base, partnerize  # noqa: PLC0415

        return partnerize.discovery(_base.RequestsTransport())

    return {"partnerize": partnerize_v2_discovery}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", type=int, default=DEFAULT_DISCOVERY_TARGET,
                        help="bounded discovery target; independent of application volume")
    parser.add_argument("--limit", type=int, default=10, help="results per query")
    parser.add_argument("--time-budget-seconds", type=float, default=DEFAULT_RUN_BUDGET_SECONDS)
    parser.add_argument("--source-timeout-seconds", type=float, default=DEFAULT_SOURCE_TIMEOUT_SECONDS)
    parser.add_argument("--max-source-calls", type=int, default=200)
    parser.add_argument("--checkpoint", default=str(SUPPLY_DIR / "checkpoint.json"))
    parser.add_argument("--admit-applications", action="store_true",
                        help="admit eligible discoveries to the existing queue; never submit applications")
    parser.add_argument("--no-marketplaces", action="store_true")
    parser.add_argument("--join", default="", help="attempt ONE marketplace self-signup")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--join-only", action="store_true",
                        help="attempt the --join marketplace signup and nothing else")
    ns = parser.parse_args(argv)
    if not (0 < ns.time_budget_seconds <= 480 and 0 < ns.source_timeout_seconds <= 60
            and ns.max_source_calls > 0):
        parser.error("require run budget in (0,480], source timeout in (0,60], and positive call cap")
    if ns.admit_applications and ns.time_budget_seconds > 360:
        parser.error("queue admission requires a discovery budget <=360s to reserve scheduler headroom")

    if ns.join_only:
        if not ns.join:
            parser.error("--join-only requires --join <network>")
        run_id = ns.run_id or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(ns.output_dir) if ns.output_dir else SUPPLY_DIR / f"join-{run_id}"
        result = attempt_self_join(ns.join, run_id=run_id, headless=not ns.headed)
        cards = unblock_cards([result])
        private_json(out_dir / "marketplace-join.json",
                     {"schema": "affiliate-supply-marketplace-join-v1",
                      "captured_at_utc": utc(), "results": [result],
                      "unblock_cards": cards, "secret_printed": False})
        print(json.dumps({"status": result.get("status"), "network": ns.join,
                          "step": result.get("step", ""), "reason": result.get("reason", ""),
                          "operator_action": result.get("operator_action", ""),
                          "out_dir": str(out_dir)}, indent=2))
        return 0

    run_id = ns.run_id or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(ns.output_dir) if ns.output_dir else SUPPLY_DIR / f"run-{run_id}"
    summary = run(search=firecrawl_search(), scrape=firecrawl_scrape(), target=max(1, ns.target),
                  run_id=run_id, out_dir=out_dir, limit=ns.limit,
                  time_budget_s=ns.time_budget_seconds or None,
                  checkpoint_path=Path(ns.checkpoint),
                  source_timeout_s=ns.source_timeout_seconds,
                  max_source_calls=ns.max_source_calls,
                  do_marketplaces=not ns.no_marketplaces, join_network=ns.join,
                  headless=not ns.headed,
                  platform_adapters=live_platform_discovery_adapters())
    if ns.admit_applications:
        # Imports and queue access happen only with explicit scheduled admission.
        sys.path.insert(0, str(BUSINESS / "scripts"))
        import affiliate_auto_apply
        inventories = [Path(summary["apply_inventory"])] + affiliate_auto_apply.freshest_inventories(
            include_direct=False)
        summary["application_admission"] = affiliate_auto_apply.admit_discovery(
            inventories, apply=True, deadline_seconds=120)
        if summary["application_admission"]["status"] != "COMPLETE":
            summary["discovery_status"] = summary["status"]
            summary["status"] = "ADMISSION_INCOMPLETE"
        private_json(out_dir / "summary.json", summary)
    print(json.dumps(summary if ns.json else {
        "status": summary["status"],
        "candidate_target": summary["candidate_target"],
        "direct_programs_found": summary["direct_programs_found"],
        "sheet_candidates_queued": summary["sheet_candidates_queued"],
        "marketplace_candidates": summary["marketplace_candidates"],
        "unblock_cards": len(summary["unblock_cards"]),
        "out_dir": summary["out_dir"],
    }, indent=2))
    return 2 if summary.get("status") == "ADMISSION_INCOMPLETE" else 0


if __name__ == "__main__":
    sys.exit(main())
