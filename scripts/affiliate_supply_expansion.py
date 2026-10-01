#!/usr/bin/env python3
"""Provider-free five-route Bridge supply-source expansion.

This module is a bounded Item 4 candidate. It consumes only injected search,
scrape, adapter, and artifact seams. Every candidate is handed back to
``affiliate_supply_discovery.discover_direct_programs`` for the shared
KnownSupply, domestic-US GLP fit, operator-ruling hold, candidate-plan, and
apply-inventory choke points.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\]\[\"']+", re.I)
HTML_HREF_RE = re.compile(r"(?i)href\s*=\s*[\"'](https?://[^\"']+)[\"']")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]{1,160})\]\((https?://[^\s)]+)\)")
NEWSLETTER_NAME_RE = re.compile(
    r"(?im)^(?:new\s+)?(?:advertiser|brand|merchant|program)\s*[:\-–—]\s*"
    r"([A-Za-z0-9][A-Za-z0-9&.' +\-]{1,78})\s*$"
)

PROVIDER_ENUMERATION_QUERIES = (
    "US telehealth weight loss provider directory GLP-1",
    "LegitScript certified telemedicine weight loss providers",
    "compounding pharmacy telehealth partner semaglutide provider list",
    "site:everflowclient.io GLP-1 telehealth",
)
PROVIDER_PROGRAM_QUERY_TEMPLATES = (
    '"{provider}" affiliate program',
    'site:{domain} (affiliate OR partners OR ambassador)',
)
QUERY_EXPANSION_QUERIES = (
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

# No invented endpoints. Each source is either an official/public research
# surface or an explicit auth/no-public-catalog disposition.
PLATFORM_SWEEP_DEFINITIONS = (
    {"slug": "uppromote", "mode": "public_search",
     "queries": ("site:uppromote.com/affiliate-programs weight loss affiliate programs",),
     "evidence": ("https://uppromote.com/affiliate-programs/weight-loss/",)},
    {"slug": "goaffpro", "mode": "public_search",
     "queries": ("GoAffPro marketplace GLP-1 weight loss affiliate",),
     "evidence": ("https://goaffpro.com/marketplace",)},
    {"slug": "refersion", "mode": "authenticated_catalog", "queries": (),
     "evidence": ("https://marketplace.refersion.com/products",
                  "https://support.refersion.com/en/articles/6812794-how-to-discover-brands-and-promote-their-products")},
    {"slug": "levanta", "mode": "authenticated_catalog",
     "queries": ("site:levanta.io (semaglutide OR tirzepatide OR GLP-1 OR telehealth) affiliate",),
     "evidence": ("https://knowledge.levanta.io/articles/7672914299-getting-started-with-creators",)},
    {"slug": "sovrn", "mode": "public_search",
     "queries": ("site:commerce.sovrn.com (semaglutide OR tirzepatide OR GLP-1 OR telehealth)",),
     "evidence": ("https://www.sovrn.com/publishers/commerce/",)},
    {"slug": "partnerize", "mode": "official_api_v2", "queries": (),
     "evidence": ("GET https://api.partnerize.com/v2/publishers/{publisherId}/discovery/advertisers",
                  "https://api-docs.partnerize.com/partner/")},
    {"slug": "rakuten", "mode": "authenticated_catalog", "queries": (),
     "evidence": ("Rakuten advertiser discovery requires authenticated OAuth",)},
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

ZERO_EFFECTS = {
    "provider_reads": 0,
    "provider_writes": 0,
    "applications_submitted": 0,
    "tracking_links_clicked": 0,
    "sheet_writes": 0,
    "emails_sent": 0,
    "deployments": 0,
    "spend_usd": 0,
}


def _unique(values, cap: int = 200) -> list[str]:
    # Count caps used to silently discard the tail forever. Read callbacks now
    # enforce a durable per-run budget; retain the whole declared denominator.
    out: list[str] = []
    for value in values:
        clean = re.sub(r"\s+", " ", str(value or "")).strip()
        if clean and clean not in out:
            out.append(clean)
    return out


def _source_links(value: Any, *, label: str = "", depth: int = 0):
    """Yield explicit links from a bounded saved artifact/fixture structure."""
    if depth > 8:
        return
    if isinstance(value, dict):
        local_label = str(
            value.get("anchor") or value.get("text") or value.get("title")
            or value.get("name") or label or ""
        )[:160]
        for key, child in value.items():
            low = str(key).lower()
            if low in {"href", "url", "program_url", "apply_url", "official_url", "destination_url"}:
                if isinstance(child, str):
                    yield local_label, child
            elif low in {"links", "outbound_links", "hrefs", "anchors", "advertisers", "brands",
                         "merchants", "programs"}:
                yield from _source_links(child, label=local_label, depth=depth + 1)
            elif low in {"markdown", "html", "raw_html", "content", "body", "body_text"} \
                    and isinstance(child, str):
                text = child[:200_000]
                for anchor, link in MARKDOWN_LINK_RE.findall(text):
                    yield anchor, link
                for link in HTML_HREF_RE.findall(text):
                    yield local_label, link
                for link in URL_IN_TEXT_RE.findall(text):
                    yield local_label, link.rstrip("),.;")
    elif isinstance(value, list):
        for child in value[:1000]:
            yield from _source_links(child, label=label, depth=depth + 1)
    elif isinstance(value, str):
        text = value[:200_000]
        for anchor, link in MARKDOWN_LINK_RE.findall(text):
            yield anchor, link
        for link in URL_IN_TEXT_RE.findall(text):
            yield label, link.rstrip("),.;")


def _artifact_docs(core, paths: list[Path] | None, *, root: Path,
                   glob_pattern: str, cap: int) -> tuple[list[Any], list[dict]]:
    docs: list[Any] = []
    evidence: list[dict] = []
    candidates = list(paths) if paths is not None else sorted(root.glob(glob_pattern))
    for path in candidates:
        doc, status = core.safe_json_artifact(Path(path), root=root)
        evidence.append({"path": str(path), "status": status})
        if doc is not None:
            docs.append(doc)
    if not candidates:
        evidence.append({"path": str(root / glob_pattern), "status": "no_artifacts"})
    return docs, evidence


def _mark_known(core, known, candidates: list[dict]) -> None:
    """Make cross-route dedupe use the same KnownSupply object immediately."""
    for candidate in candidates:
        name = core._norm_name(candidate.get("provider"))
        domain = core.registrable(
            candidate.get("official_url") or candidate.get("program_url") or ""
        )
        if name:
            known.names.add(name)
        if domain and domain not in core.PLATFORM_HOSTS:
            known.domains.add(domain)


def _receipt(core, route: str, discovery: dict, *, evidence: list,
             query_log: list | None = None, subroutes: list | None = None) -> dict:
    accepted = discovery.get("accepted") or []
    rejected = discovery.get("rejected") or []
    observations = list(evidence) + list(query_log or []) + list(subroutes or [])
    incomplete = any(any(word in str(row.get("status", "")).lower()
                         for word in ("error", "blocked", "skipped", "unavailable",
                                      "no_artifacts", "too_thin", "refused"))
                     for row in observations)
    incomplete = incomplete or any(
        row.get("reason") == "NO_PUBLIC_CATALOG_EVIDENCE" for row in observations)
    evidenced_empty = not accepted and bool(query_log) and not incomplete
    return {
        "schema": "affiliate-supply-route-receipt-v1",
        "route": route,
        "captured_at_utc": core.utc(),
        "status": "INCOMPLETE" if incomplete else "FIT_FOUND" if accepted else (
            "EVIDENCED_EMPTY" if evidenced_empty else "BLOCKED_NO_EVIDENCE"
        ),
        "found": int(discovery.get("hits") or 0),
        "fit": len(accepted),
        "applied": 0,
        "parked": len(rejected),
        "evidenced_empty": evidenced_empty,
        "accepted": accepted,
        "parked_records": rejected,
        "evidence": evidence,
        "queries_run": list(query_log or []),
        "subroutes": list(subroutes or []),
        "effects": dict(ZERO_EFFECTS),
        "secret_printed": False,
    }


def _gate_hits(core, route: str, hits: list, scrape, *, known,
               policy_snapshot, deadline=None) -> dict:
    # Every route converges here. No route owns a bespoke fit/dedupe/hold result.
    result = core.discover_direct_programs(
        lambda _query, _limit=10: [], scrape, known=known, queries=(),
        initial_hits=hits, deadline=deadline, policy_snapshot=policy_snapshot,
    )
    for candidate in result.get("accepted") or []:
        candidate["source_route"] = route
    _mark_known(core, known, result.get("accepted") or [])
    return result


def competitor_outbound(core, search, scrape, *, known, policy_snapshot,
                        research_root: Path, artifact_paths: list[Path] | None = None,
                        artifact_docs: list[dict] | None = None,
                        limit: int = 10, deadline=None) -> dict:
    """Route 1: read saved SEO extracts and never request a tracking URL."""
    root = Path(research_root)
    if artifact_docs is not None:
        docs = list(artifact_docs)
        evidence = [{"source": "injected_fixture", "status": "ok"}]
    else:
        docs, evidence = _artifact_docs(
            core, artifact_paths, root=root,
            glob_pattern="*/research/*competitor*extract*.json",
            cap=MAX_COMPETITOR_ARTIFACTS,
        )
    direct_hits: list[Any] = []
    providers: list[dict] = []
    seen_links: set[str] = set()
    for doc in docs:
        source_url = str(doc.get("url") or "") if isinstance(doc, dict) else ""
        source_host = core.host_of(source_url)
        for label, raw_link in _source_links(doc):
            link = str(raw_link or "").strip().rstrip("),.;")
            if link in seen_links:
                continue
            seen_links.add(link)
            safe, reason = core.public_http_url(link)
            if not safe:
                evidence.append({
                    "url_sha256": hashlib.sha256(link.encode()).hexdigest(),
                    "status": f"parked_unsafe_url:{reason}",
                })
                continue
            if source_host and core.host_of(link) == source_host:
                continue
            if core.is_tracking_url(link):
                identity = core.tracking_identity(link, label)
                evidence.append({"status": "tracking_identity_derived_without_request", **identity})
                if identity["provider"]:
                    providers.append(identity)
                continue
            if core.looks_like_program_surface(link, label) and not core.is_editorial(link, label):
                direct_hits.append(core.Hit(
                    link, label or core.registrable(link),
                    "competitor outbound affiliate program", "competitor_outbound_saved_extract",
                ))
            else:
                token = core.domain_core(link)
                provider = core.name_from_text(label, token) or (token.title() if token else "")
                if provider:
                    providers.append({
                        "provider": provider, "destination_url": link,
                        "network": core.platform_for_url(link) or "unknown",
                        "tracking_url_requested": False,
                    })
    queries = _unique(
        (f"{row['provider']} affiliate program" for row in providers if row.get("provider")),
        cap=80,
    )
    query_log: list = []
    direct_hits.extend(core.run_search(
        search, queries, limit=limit, deadline=deadline, log=query_log,
    ))
    discovery = _gate_hits(
        core, "competitor_outbound_mining", direct_hits, scrape, known=known,
        policy_snapshot=policy_snapshot, deadline=deadline,
    )
    return _receipt(
        core, "competitor_outbound_mining", discovery,
        evidence=evidence, query_log=query_log,
    )


def _provider_seeds(core, doc: Any) -> list[dict]:
    seeds: list[dict] = []
    rows: list[Any] = []
    if isinstance(doc, dict):
        for key in ("providers", "advertisers", "brands", "merchants", "programs"):
            if isinstance(doc.get(key), list):
                rows.extend(doc[key])
        if any(key in doc for key in ("name", "provider", "domain", "url", "official_url")):
            rows.append(doc)
    elif isinstance(doc, list):
        rows.extend(doc)
    for row in rows[:1000]:
        if isinstance(row, str):
            name, domain = row.strip(), ""
        elif isinstance(row, dict):
            name = str(
                row.get("provider") or row.get("name") or row.get("advertiser") or ""
            ).strip()
            raw = str(
                row.get("domain") or row.get("official_url") or row.get("url") or ""
            ).strip()
            if raw and "://" not in raw:
                raw = "https://" + raw.strip("/") + "/"
            safe, _ = core.public_http_url(raw) if raw else (False, "")
            domain = core.registrable(raw) if safe else ""
        else:
            continue
        if name or domain:
            seeds.append({"provider": name or domain.split(".")[0].title(), "domain": domain})
    return seeds


def provider_first(core, search, scrape, *, known, policy_snapshot,
                   source_docs: list[dict] | None = None,
                   enumeration_queries=PROVIDER_ENUMERATION_QUERIES,
                   limit: int = 10, deadline=None) -> dict:
    """Route 2: enumerate vertical providers, then probe only public surfaces."""
    query_log: list = []
    enum_hits = core.run_search(
        search, enumeration_queries, limit=limit, deadline=deadline, log=query_log,
    )
    seeds: list[dict] = []
    evidence = [{
        "source": "injected_or_first_party_provider_sources",
        "documents": len(source_docs or []),
    }]
    for doc in source_docs or []:
        seeds.extend(_provider_seeds(core, doc))
    for hit in enum_hits:
        safe, reason = core.public_http_url(hit.url)
        text = f"{hit.title} {hit.description}"
        if not safe:
            evidence.append({"status": f"provider_seed_unsafe:{reason}", "query": hit.query})
            continue
        if not (core.nfilter.GLP1_EXPLICIT_RE.search(text)
                or core.nfilter.WEIGHT_LOSS_RE.search(text)):
            evidence.append({"status": "provider_seed_not_glp_vertical", "url": hit.url})
            continue
        name = core.provider_name(hit)
        domain = core.registrable(hit.url)
        if name and domain and domain not in core.NON_PROGRAM_HOSTS:
            seeds.append({"provider": name, "domain": domain})
    unique: dict[tuple[str, str], dict] = {}
    for seed in seeds:
        key = (core._norm_name(seed.get("provider")), str(seed.get("domain") or ""))
        if key != ("", ""):
            unique.setdefault(key, seed)
    program_hits: list[Any] = []
    targeted_queries: list[str] = []
    for seed in unique.values():
        provider = seed.get("provider") or ""
        domain = seed.get("domain") or ""
        for template in PROVIDER_PROGRAM_QUERY_TEMPLATES:
            if "{domain}" in template and not domain:
                continue
            targeted_queries.append(template.format(provider=provider, domain=domain))
        if domain:
            # Footer probe is the provider homepage only; no invented path is
            # requested. Affiliate routes must appear as public non-tracking links.
            homepage = f"https://{domain}/"
            try:
                page = scrape(homepage) or {}
            except Exception as exc:  # noqa: BLE001
                evidence.append({
                    "source": homepage, "status": f"footer_probe_error:{type(exc).__name__}",
                })
            else:
                if str(page.get("status")) == "ok":
                    for anchor, link in MARKDOWN_LINK_RE.findall(
                            str(page.get("markdown") or "")[:120_000]):
                        link_safe, _ = core.public_http_url(link)
                        if (link_safe and not core.is_tracking_url(link)
                                and core.looks_like_program_surface(link, anchor)
                                and not core.is_editorial(link, anchor)):
                            program_hits.append(core.Hit(
                                link, anchor, f"footer route on {domain}",
                                f"provider_footer:{domain}",
                            ))
                evidence.append({
                    "source": homepage, "status": str(page.get("status") or "unavailable"),
                })
    targeted_queries = _unique(targeted_queries, cap=MAX_PROVIDER_SEEDS * 2)
    program_hits.extend(core.run_search(
        search, targeted_queries, limit=limit, deadline=deadline, log=query_log,
    ))
    discovery = _gate_hits(
        core, "provider_first", program_hits, scrape, known=known,
        policy_snapshot=policy_snapshot, deadline=deadline,
    )
    evidence.append({
        "provider_seeds": len(unique), "targeted_queries": len(targeted_queries),
    })
    return _receipt(core, "provider_first", discovery, evidence=evidence, query_log=query_log)


def _platform_rows(doc: Any) -> list[dict]:
    if isinstance(doc, list):
        return [row for row in doc if isinstance(row, dict)]
    if not isinstance(doc, dict):
        return []
    rows: list[dict] = []
    for key in ("brands", "advertisers", "merchants", "programs", "hits"):
        if isinstance(doc.get(key), list):
            rows.extend(row for row in doc[key] if isinstance(row, dict))
    return rows


def platform_sweeps(core, search, scrape, *, known, policy_snapshot,
                    adapters: dict[str, Any] | None = None,
                    definitions=PLATFORM_SWEEP_DEFINITIONS,
                    limit: int = 10, deadline=None) -> dict:
    """Route 3: deterministic definitions; no platform is a silent zero."""
    adapters = adapters or {}
    all_hits: list[Any] = []
    query_log: list = []
    subroutes: list[dict] = []
    evidence: list[dict] = []
    for definition in definitions:
        slug = definition["slug"]
        mode = definition["mode"]
        subroute_start = len(all_hits)
        rows: list[dict] = []
        status = ""
        reason = ""
        adapter = adapters.get(slug)
        if adapter is not None:
            try:
                doc = adapter() if callable(adapter) else adapter
            except Exception as exc:  # noqa: BLE001
                status = "EVIDENCED_BLOCKED"
                reason = f"injected_adapter_error:{type(exc).__name__}"
                doc = None
            else:
                rows = _platform_rows(doc)
                status = str(doc.get("status") or "ADAPTER_READ") \
                    if isinstance(doc, dict) else "ADAPTER_READ"
        elif mode == "public_search":
            before = len(all_hits)
            all_hits.extend(core.run_search(
                search, definition.get("queries") or (), limit=limit,
                deadline=deadline, log=query_log,
            ))
            if len(all_hits) > before:
                status = "PUBLIC_SEARCH_RESULTS"
            else:
                status = "EVIDENCED_EMPTY"
                reason = "injected public search returned zero"
        elif mode in {"authenticated_catalog", "official_api_v2"}:
            before = len(all_hits)
            queries = definition.get("queries") or ()
            all_hits.extend(core.run_search(
                search, queries, limit=limit, deadline=deadline, log=query_log,
            ))
            fallback = len(all_hits) - before
            status = ("AUTHENTICATED_BLOCKED_PUBLIC_FALLBACK_RESULTS" if fallback
                      else "AUTHENTICATED_BLOCKED_PUBLIC_FALLBACK_EMPTY")
            reason = "authenticated_adapter_not_injected; public fallback completed"
        else:
            status = "EVIDENCED_EMPTY"
            reason = "NO_PUBLIC_CATALOG_EVIDENCE"
        for row in rows:
            name = str(
                row.get("name") or row.get("provider") or row.get("advertiser") or ""
            ).strip()
            url = str(
                row.get("program_url") or row.get("apply_url") or row.get("url") or ""
            ).strip()
            description = str(
                row.get("description") or row.get("vertical") or row.get("category") or ""
            )
            safe, _ = core.public_http_url(url) if url else (False, "")
            if (safe and not core.is_tracking_url(url)
                    and core.looks_like_program_surface(url, name)):
                all_hits.append(core.Hit(
                    url, name or core.registrable(url), description,
                    f"platform:{slug}:adapter",
                ))
            elif name:
                all_hits.extend(core.run_search(
                    search, (f"{name} affiliate program",), limit=limit,
                    deadline=deadline, log=query_log,
                ))
        subroutes.append({
            "platform": slug,
            "mode": mode,
            "status": status,
            "reason": reason,
            "adapter_rows": len(rows),
            "hits_emitted": len(all_hits) - subroute_start,
            "scan_attempted": True,
            "public_fallback": bool(mode in {"authenticated_catalog", "official_api_v2"}
                                    and definition.get("queries")),
            "evidence": list(definition.get("evidence") or ()),
        })
        evidence.extend({"platform": slug, "source": source}
                        for source in definition.get("evidence") or ())
    discovery = _gate_hits(
        core, "platform_sweeps", all_hits, scrape, known=known,
        policy_snapshot=policy_snapshot, deadline=deadline,
    )
    receipt = _receipt(
        core, "platform_sweeps", discovery, evidence=evidence,
        query_log=query_log, subroutes=subroutes,
    )
    receipt["platform_receipts"] = subroutes
    return receipt


def _newsletter_seeds(core, doc: Any) -> list[dict]:
    seeds = _provider_seeds(core, doc)
    text_parts: list[str] = []
    if isinstance(doc, dict):
        classification = doc.get("classification") or {}
        category = str(
            doc.get("category") or classification.get("category") or ""
        ).lower()
        if category and category not in {"newsletter", "affiliate_network_response"}:
            return []
        for key in ("subject", "body", "body_text", "markdown", "html", "content"):
            if isinstance(doc.get(key), str):
                text_parts.append(doc[key])
        parsed = doc.get("parsed")
        if isinstance(parsed, dict):
            for key in ("subject", "body", "body_text", "markdown", "html"):
                if isinstance(parsed.get(key), str):
                    text_parts.append(parsed[key])
    text = "\n".join(text_parts)[:400_000]
    for name in NEWSLETTER_NAME_RE.findall(text):
        seeds.append({"provider": name.strip(), "domain": ""})
    for label, link in _source_links({"body": text}):
        safe, _ = core.public_http_url(link)
        if not safe or core.is_tracking_url(link):
            continue
        token = core.domain_core(link)
        name = core.name_from_text(label, token) or (token.title() if token else "")
        if name:
            seeds.append({
                "provider": name, "domain": core.registrable(link), "url": link,
            })
    return seeds


def inbox_newsletters(core, search, scrape, *, known, policy_snapshot,
                      intake_root: Path, artifact_paths: list[Path] | None = None,
                      artifact_docs: list[dict] | None = None,
                      limit: int = 10, deadline=None) -> dict:
    """Route 4: consume canonical/injected artifacts; never open an email system."""
    root = Path(intake_root)
    if artifact_docs is not None:
        docs = list(artifact_docs)
        evidence = [{"source": "injected_newsletter_fixture", "status": "ok"}]
    else:
        docs, evidence = _artifact_docs(
            core, artifact_paths, root=root, glob_pattern="*.json",
            cap=MAX_NEWSLETTER_ARTIFACTS,
        )
    seeds: list[dict] = []
    for doc in docs:
        seeds.extend(_newsletter_seeds(core, doc))
    queries = _unique(
        (f"{row['provider']} affiliate program" for row in seeds if row.get("provider")),
        cap=80,
    )
    query_log: list = []
    hits = core.run_search(
        search, queries, limit=limit, deadline=deadline, log=query_log,
    )
    for row in seeds:
        url = str(row.get("url") or "")
        safe, _ = core.public_http_url(url) if url else (False, "")
        if (safe and core.looks_like_program_surface(url, row.get("provider") or "")
                and not core.is_tracking_url(url)):
            hits.append(core.Hit(
                url, row.get("provider") or core.registrable(url),
                "affiliate network newsletter advertiser", "inbox_newsletter_artifact",
            ))
    discovery = _gate_hits(
        core, "inbox_newsletter", hits, scrape, known=known,
        policy_snapshot=policy_snapshot, deadline=deadline,
    )
    evidence.append({"artifacts_consumed": len(docs), "advertiser_seeds": len(seeds)})
    return _receipt(
        core, "inbox_newsletter", discovery, evidence=evidence, query_log=query_log,
    )


def query_expansion(core, search, scrape, *, known, policy_snapshot,
                    queries=QUERY_EXPANSION_QUERIES, limit: int = 10,
                    deadline=None) -> dict:
    """Route 5: a resumable therapeutic/modality-specific query wave."""
    declared = tuple(queries)
    capped = declared
    query_log: list = []
    hits = core.run_search(
        search, capped, limit=limit, deadline=deadline, log=query_log,
    )
    discovery = _gate_hits(
        core, "query_expansion", hits, scrape, known=known,
        policy_snapshot=policy_snapshot, deadline=deadline,
    )
    evidence = [{
        "query_cap": None,
        "work_bound": "durable read callback time/call budget",
        "queries_declared": len(declared),
        "queries_executed": len(query_log),
    }]
    return _receipt(
        core, "query_expansion", discovery, evidence=evidence, query_log=query_log,
    )


def run_five_routes(core, search, scrape, *, known, policy_snapshot,
                    research_root: Path, newsletter_root: Path,
                    fixtures: dict | None = None,
                    platform_adapters: dict[str, Any] | None = None,
                    limit: int = 10, deadline=None) -> dict:
    """Run all five routes serially against one mutable KnownSupply universe."""
    fixtures = fixtures or {}
    routes = []
    routes.append(competitor_outbound(
        core, search, scrape, known=known, policy_snapshot=policy_snapshot,
        research_root=research_root,
        artifact_paths=fixtures.get("competitor_artifact_paths"),
        artifact_docs=fixtures.get("competitor_docs"),
        limit=limit, deadline=deadline,
    ))
    routes.append(provider_first(
        core, search, scrape, known=known, policy_snapshot=policy_snapshot,
        source_docs=fixtures.get("provider_docs"),
        enumeration_queries=fixtures.get(
            "provider_enumeration_queries", PROVIDER_ENUMERATION_QUERIES,
        ),
        limit=limit, deadline=deadline,
    ))
    routes.append(platform_sweeps(
        core, search, scrape, known=known, policy_snapshot=policy_snapshot,
        adapters=platform_adapters or fixtures.get("platform_adapters"),
        definitions=fixtures.get("platform_definitions", PLATFORM_SWEEP_DEFINITIONS),
        limit=limit, deadline=deadline,
    ))
    routes.append(inbox_newsletters(
        core, search, scrape, known=known, policy_snapshot=policy_snapshot,
        intake_root=newsletter_root,
        artifact_paths=fixtures.get("newsletter_artifact_paths"),
        artifact_docs=fixtures.get("newsletter_docs"),
        limit=limit, deadline=deadline,
    ))
    routes.append(query_expansion(
        core, search, scrape, known=known, policy_snapshot=policy_snapshot,
        queries=fixtures.get("query_expansion_queries", QUERY_EXPANSION_QUERIES),
        limit=limit, deadline=deadline,
    ))
    accepted: list[dict] = []
    for receipt in routes:
        accepted.extend(receipt.get("accepted") or [])
    return {
        "schema": "affiliate-supply-five-route-run-v1",
        "captured_at_utc": core.utc(),
        "routes": {receipt["route"]: receipt for receipt in routes},
        "route_order": [receipt["route"] for receipt in routes],
        "totals": {
            "found": sum(int(receipt.get("found") or 0) for receipt in routes),
            "fit": len(accepted),
            "applied": 0,
            "parked": sum(int(receipt.get("parked") or 0) for receipt in routes),
            "evidenced_empty_routes": sum(bool(receipt.get("evidenced_empty")) for receipt in routes),
        },
        "accepted": accepted,
        "effects": dict(ZERO_EFFECTS),
        "secret_printed": False,
    }
