#!/usr/bin/env python3
"""Truthful-`lastmod` build gate for GLP Clinic Scout (B-04 / SEO §3 / AEO P0-D).

THE RULE THIS ENFORCES
----------------------
`lastmod` only moves when content moves. glpwinner bulk-stamps 314/423 URLs with
the build date; that is the documented anti-pattern and the reason our dates are
worth anything. So this gate fails a build on BOTH failure directions:

  * a sitemap URL with no `<lastmod>` at all            -> lastmod_missing
  * a page whose bytes changed but whose lastmod did not -> stale_lastmod
  * a lastmod that moved while the content hash did not  -> false_lastmod_bump
  * a lastmod that is not an ISO date, or is in the future

Provenance, not build time, is the source of the date. Each release carries
`data/lastmod-provenance.json`:

    {"schema": "glp-lastmod-provenance-v1",
     "entries": {"/reviews/orderlymeds/": {"content_sha256": "...",
                                           "lastmod": "2026-08-19"}}}

`--mint` regenerates that file from the tree, and it CANNOT invent a date: a page
with no prior provenance must carry a visible `<time datetime="YYYY-MM-DD">`
dateline or an existing sitemap lastmod, otherwise minting fails. There is no
"just use today" path, by design.

Usage:
    python3 scripts/glp_sitemap_lastmod_gate.py --release <root>
    python3 scripts/glp_sitemap_lastmod_gate.py --release <root> --predecessor <root>
    python3 scripts/glp_sitemap_lastmod_gate.py --release <root> --mint
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCHEMA = "glp-sitemap-lastmod-gate-v1"
PROVENANCE_SCHEMA = "glp-lastmod-provenance-v1"
PROVENANCE_RELPATH = "data/lastmod-provenance.json"
SITE_ORIGIN = "https://glpclinicscout.com"

_COMMENT = re.compile(rb"<!--.*?-->", re.DOTALL)
_BUILD_ATTR = re.compile(rb'\sdata-build-[a-z-]+="[^"]*"')
_WS = re.compile(rb"\s+")
_NOINDEX = re.compile(
    rb"""<meta[^>]+name=["']robots["'][^>]*content=["'][^"']*noindex""", re.IGNORECASE)
_TIME_TAG = re.compile(rb'<time[^>]*\sdatetime="([^"]*)"', re.IGNORECASE)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def content_fingerprint(html: bytes) -> str:
    """Content hash with build-volatile bytes normalised out.

    Comments, `data-build-*` attributes and whitespace runs are removed so that a
    rebuild of unchanged content produces an unchanged hash — otherwise every
    build would "justify" a lastmod bump and we would be bulk-stamping too.
    """
    body = _COMMENT.sub(b"", html)
    body = _BUILD_ATTR.sub(b"", body)
    body = _WS.sub(b" ", body).strip()
    return hashlib.sha256(body).hexdigest()


def parse_sitemap(text: str) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for block in re.findall(r"<url>(.*?)</url>", text, re.DOTALL):
        loc_m = re.search(r"<loc>(.*?)</loc>", block, re.DOTALL)
        if not loc_m:
            continue
        last_m = re.search(r"<lastmod>(.*?)</lastmod>", block, re.DOTALL)
        out.append((loc_m.group(1).strip(), last_m.group(1).strip() if last_m else None))
    return out


def url_to_route(url: str) -> str:
    path = urlparse(url).path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def route_to_file(public: Path, route: str) -> Path | None:
    rel = route.strip("/")
    if rel == "":
        candidate = public / "index.html"
    elif rel.endswith(".html") or "." in Path(rel).name:
        candidate = public / rel
    else:
        candidate = public / rel / "index.html"
    return candidate if candidate.is_file() else None


def is_noindex(html: bytes) -> bool:
    return bool(_NOINDEX.search(html))


def page_inventory(public: Path) -> set[str]:
    """Indexable rendered pages in the tree, as routes."""
    routes: set[str] = set()
    for path in public.rglob("index.html"):
        if path.is_symlink():
            continue
        html = path.read_bytes()
        if is_noindex(html):
            continue
        rel = path.parent.relative_to(public).as_posix()
        routes.add("/" if rel == "." else f"/{rel}/")
    return routes


def visible_dateline(html: bytes) -> str | None:
    for value in _TIME_TAG.findall(html):
        text = value.decode("utf-8", "replace").strip()
        if _ISO_DATE.match(text):
            return text
    return None


def load_provenance(release: Path) -> dict[str, dict[str, str]]:
    path = release / PROVENANCE_RELPATH
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != PROVENANCE_SCHEMA:
        raise RuntimeError(f"lastmod_provenance_schema_invalid:{payload.get('schema')}")
    return payload.get("entries") or {}


def mint_provenance(release: Path, today: str | None = None) -> dict[str, Any]:
    """Rebuild data/lastmod-provenance.json from the tree. Never invents a date."""
    public = release / "public"
    sitemap = parse_sitemap((public / "sitemap.xml").read_text(encoding="utf-8"))
    previous = load_provenance(release)
    entries: dict[str, dict[str, str]] = {}
    failures: list[str] = []
    for url, lastmod in sitemap:
        route = url_to_route(url)
        file = route_to_file(public, route)
        if file is None:
            failures.append(f"sitemap_url_not_in_tree:{route}")
            continue
        html = file.read_bytes()
        digest = content_fingerprint(html)
        prior = previous.get(route)
        if prior and prior.get("content_sha256") == digest:
            entries[route] = {"content_sha256": digest, "lastmod": prior["lastmod"]}
            continue
        # Content is new or changed: the date must come from the page itself or
        # from an existing honest sitemap stamp. Never from the clock.
        honest = visible_dateline(html) or (lastmod if lastmod and _ISO_DATE.match(lastmod) else None)
        if honest is None:
            failures.append(f"no_honest_date_available:{route}")
            continue
        entries[route] = {"content_sha256": digest, "lastmod": honest}
    return {
        "schema": PROVENANCE_SCHEMA,
        "minted_at_utc": today or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "entries": dict(sorted(entries.items())),
        "failures": failures,
    }


def evaluate(release: Path, predecessor: Path | None = None,
             today: str | None = None) -> dict[str, Any]:
    public = release / "public"
    failures: list[str] = []
    sitemap_path = public / "sitemap.xml"
    if not sitemap_path.is_file():
        return {"schema": SCHEMA, "status": "FAIL", "failure_count": 1,
                "failures": [f"sitemap_missing:{sitemap_path}"], "release": str(release)}

    sitemap = parse_sitemap(sitemap_path.read_text(encoding="utf-8"))
    provenance = load_provenance(release)
    predecessor_provenance = load_provenance(predecessor) if predecessor else {}
    now = today or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    with_lastmod = 0
    for url, lastmod in sitemap:
        route = url_to_route(url)
        if not lastmod:
            failures.append(f"lastmod_missing:{route}")
            continue
        with_lastmod += 1
        if not _ISO_DATE.match(lastmod):
            failures.append(f"lastmod_not_iso_date:{route}:{lastmod}")
            continue
        if lastmod > now:
            failures.append(f"lastmod_in_the_future:{route}:{lastmod}")
        entry = provenance.get(route)
        if entry is None:
            failures.append(f"provenance_missing:{route}")
            continue
        if entry.get("lastmod") != lastmod:
            failures.append(
                f"lastmod_not_from_provenance:{route}:sitemap={lastmod}:provenance={entry.get('lastmod')}")
        file = route_to_file(public, route)
        if file is None:
            failures.append(f"sitemap_url_not_in_tree:{route}")
            continue
        digest = content_fingerprint(file.read_bytes())
        if digest != entry.get("content_sha256"):
            failures.append(f"stale_lastmod:{route}:content changed without a provenance update")
        prior = predecessor_provenance.get(route)
        if prior and prior.get("content_sha256") == entry.get("content_sha256") \
                and prior.get("lastmod") != entry.get("lastmod"):
            failures.append(
                f"false_lastmod_bump:{route}:{prior.get('lastmod')}->{entry.get('lastmod')} with identical content hash")

    sitemap_routes = {url_to_route(u) for u, _ in sitemap}
    inventory = page_inventory(public)
    missing_from_sitemap = sorted(inventory - sitemap_routes)
    for route in missing_from_sitemap:
        failures.append(f"indexable_page_absent_from_sitemap:{route}")

    total = len(sitemap)
    return {
        "schema": SCHEMA,
        "status": "FAIL" if failures else "PASS",
        "release": str(release),
        "predecessor": str(predecessor) if predecessor else None,
        "sitemap_url_count": total,
        "lastmod_present": with_lastmod,
        "lastmod_coverage": (with_lastmod / total) if total else 0.0,
        "indexable_page_count": len(inventory),
        "indexable_pages_missing_from_sitemap": missing_from_sitemap,
        "failure_count": len(failures),
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--release", type=Path, required=True)
    ap.add_argument("--predecessor", type=Path, help="predecessor release, enables the false-bump check")
    ap.add_argument("--mint", action="store_true", help="regenerate data/lastmod-provenance.json")
    ap.add_argument("--today", help=argparse.SUPPRESS)
    ns = ap.parse_args(argv)

    if ns.mint:
        payload = mint_provenance(ns.release, today=ns.today)
        out = ns.release / PROVENANCE_RELPATH
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({k: v for k, v in payload.items() if k != "failures"},
                                  indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"status": "FAIL" if payload["failures"] else "PASS",
                          "provenance": str(out),
                          "entry_count": len(payload["entries"]),
                          "failures": payload["failures"]}, indent=2, sort_keys=True))
        return 1 if payload["failures"] else 0

    result = evaluate(ns.release, ns.predecessor, today=ns.today)
    result["failures_sample"] = result["failures"][:20]
    printable = dict(result)
    printable.pop("failures", None)
    print(json.dumps(printable, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
