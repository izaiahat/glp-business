#!/usr/bin/env python3
"""Mint the competitor-gap feed the topic queue consumes, from live sources.

Why this exists (bridge, 2026-08-15): the machine had been drawing eligible
topics from ``05c-X3-15-OPPORTUNITIES-20260801.json`` -- a hand-cut file frozen
on 2026-08-01 carrying 15 enriched keywords. Ten were published, the rest were
structurally blocked, and the selector then rejected 85 of 100 topic rows for
``competitor_gap_target_route_missing``. The buffer drained to zero and the
carrier had nothing to publish. Supply, not the publish chain, was the failure.

This mints the feed every day from three live sources instead:

  * the cluster rotation queue -- the sanctioned hub/spoke plan. Minting the
    keyword straight off the strategy row is what makes the selector's
    ``(normalized target_query, canonical route)`` join succeed by construction,
    so ``strategy_queue_join_missing`` cannot recur.
  * the measured keyword corpus -- real volume/difficulty/intent research, with
    the competitor URL that ranks for each keyword as gap evidence.
  * Search Console -- layered as a PREFERENCE on priority, never as an
    eligibility gate, matching how the selector already treats GSC.

Pillars are minted at the top priority band because a cluster's spokes are
gated on ``requires_pillar_predecessor``: publishing one hub unblocks its whole
spoke set. Publishing hubs first is both the ranking strategy and the only way
the queue stops starving itself.

Evidence provenance is recorded honestly. The corpus supplies a ranking
competitor URL per keyword, not fetched page content, so evidence rows carry the
URL, host, measured volume, and the match basis that produced them. No content
hashes or page descriptions are synthesized for pages this script never read.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from weekly_strategy_consumer import load_strategy, preference, preference_score

BUSINESS = Path("/home/ubuntu/business")
SCRIPTS = BUSINESS / "systems/seo-blog-machine/scripts"
sys.path.insert(0, str(BUSINESS / "systems/analytics-brain"))
from decision_policy import (  # noqa: E402
    consumer_policy, require_allowed, write_consumer_decision,
)
DEFAULT_CORPUS = BUSINESS / (
    "reports/glp-clinic-scout/excellence-pass-20260801/x3-source-20260801/x3_gaps_raw.json")
DEFAULT_OUTPUT = BUSINESS / (
    "reports/glp-clinic-scout/seo-blog-machine-20260730/competitor-gap-feed-current.json")
DEFAULT_FIRECRAWL_SUPPLEMENT = BUSINESS / (
    "reports/glp-clinic-scout/seo-machine/topic-supply/firecrawl-gap-evidence-current.json")
DEFAULT_KEYWORD_GAP_SUPPLY = BUSINESS / (
    "reports/glp-clinic-scout/seo-machine/keyword-gap/current-topic-supply.json")
SCHEMA = "seo-blog-machine-competitor-gap-input-v1"
FIRECRAWL_SUPPLEMENT_SCHEMA = "glp-topic-supply-firecrawl-evidence-v1"
KEYWORD_GAP_SUPPLY_SCHEMA = "glp-keyword-gap-topic-supply-v1"
DIRECTIVE_PREFIX = "auto-supply-20260815"

# Editorial phrasings in the rotation queue ("glp-1 cost by drug and provider")
# rarely equal a researched keyword string ("glp-1 cost") verbatim, so matching
# is token-overlap based. These carry no topical signal and would inflate every
# comparison toward a false match.
STOPWORDS = frozenset({
    "the", "a", "and", "or", "for", "of", "to", "in", "with", "without",
    "vs", "by", "how", "much", "does", "is", "what", "best", "online", "get",
})
# Measured on the live corpus: coverage plateaus at 0.25 (43 of 60 rows reach
# two distinct competitor URLs; loosening to 0.15 adds nothing but noise).
MATCH_MINIMUM = 0.25
MATCH_LIMIT = 8
EVIDENCE_MINIMUM = 2
PILLAR_PRIORITY = 5
SPOKE_PRIORITY_MAX = 4


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize(value: str) -> str:
    text = re.sub(r"\bglp\s*-?\s*1\b", "glp1", str(value).lower())
    return " ".join(re.findall(r"[a-z0-9]+", text))


def tokens(value: str) -> set[str]:
    return {t for t in normalize(value).split() if t and t not in STOPWORDS}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_keyword_gap_supply(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Load the weekly overlay fail-closed; absence is an honest zero state."""
    meta: dict[str, Any] = {"path": str(path), "status": "absent", "ready": 0, "held": 0}
    if not path.is_file():
        return [], [], meta
    payload = read_json(path)
    if payload.get("schema") != KEYWORD_GAP_SUPPLY_SCHEMA:
        raise RuntimeError("keyword_gap_supply_schema_invalid")
    source = Path(str(payload.get("source_opportunity_artifact") or "")).resolve(strict=True)
    opportunity = read_json(source)
    expected = str(payload.get("source_opportunity_sha256") or "")
    if opportunity.get("artifact_sha256") != expected:
        raise RuntimeError("keyword_gap_supply_source_sha_invalid")
    ready = list(payload.get("gaps") or [])
    held = list(payload.get("research_backlog") or [])
    for row in ready:
        if (
            row.get("opportunity_class") not in {"B", "C"}
            or row.get("queue_state") != "READY_FOR_RESEARCH"
            or row.get("operator_ready_for_research") is not True
            or not str(row.get("operator_directive_id") or "").startswith("operator-keyword-gap-20260823:")
        ):
            raise RuntimeError("keyword_gap_supply_ready_row_invalid")
    meta.update({
        "status": payload.get("status"), "ready": len(ready), "held": len(held),
        "created_at": payload.get("created_at"), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_opportunity_artifact": str(source), "source_opportunity_sha256": expected,
    })
    return ready, held, meta


def load_firecrawl_supplement(path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    meta: dict[str, Any] = {"path": str(path), "status": "absent", "targets": 0}
    if not path.is_file():
        return {}, meta
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get("schema") != FIRECRAWL_SUPPLEMENT_SCHEMA:
        meta["status"] = "schema_mismatch"
        return {}, meta
    rows: dict[str, list[dict[str, Any]]] = {}
    for target in payload.get("targets") or []:
        if not isinstance(target, dict):
            continue
        query = normalize(str(target.get("target_query") or ""))
        if not query:
            continue
        for result in target.get("results") or []:
            if not isinstance(result, dict):
                continue
            url = str(result.get("url") or "")
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname:
                continue
            rows.setdefault(query, []).append({
                "url": url,
                "host": parsed.hostname.lower(),
                "matched_keyword": str(target.get("target_query")),
                "search_volume": 0,
                "keyword_difficulty": None,
                "competitor_rank": None,
                "intent": None,
                "match_basis": "firecrawl_exact_query_result",
                "provider": payload.get("provider"),
                "search_id": target.get("search_id"),
                "captured_at_utc": payload.get("captured_at_utc"),
                "title": result.get("title"),
            })
    meta.update({
        "status": payload.get("status"),
        "captured_at_utc": payload.get("captured_at_utc"),
        "provider_calls": payload.get("provider_calls"),
        "targets": len(rows),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    })
    return rows, meta


def merge_exact_query_evidence(
    query: str,
    measured: list[dict[str, Any]],
    supplement: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    merged = list(measured)
    seen = {str(row.get("url")) for row in merged}
    for row in supplement.get(normalize(query), []):
        if row["url"] in seen:
            continue
        merged.append(row)
        seen.add(row["url"])
        if len(merged) >= MATCH_LIMIT:
            break
    return merged


def load_selector():
    """Reuse the selector's own route/ledger readers so exclusion logic cannot drift."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "select_opus_content_queue", SCRIPTS / "select_opus_content_queue.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["select_opus_content_queue"] = module
    spec.loader.exec_module(module)
    return module


def corpus_index(corpus: list[dict[str, Any]]) -> list[tuple[set[str], dict[str, Any]]]:
    rows = []
    for row in corpus:
        keyword = str(row.get("keyword") or "")
        if not keyword or not row.get("url"):
            continue
        rows.append((tokens(keyword), row))
    return rows


def match_corpus(query: str, index: list[tuple[set[str], dict[str, Any]]]) -> list[dict[str, Any]]:
    """Rank corpus keywords by token overlap with the strategy row's target query."""
    want = tokens(query)
    if not want:
        return []
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for have, row in index:
        if not have:
            continue
        overlap = len(want & have) / len(want | have)
        if overlap >= MATCH_MINIMUM:
            scored.append((overlap, int(row.get("search_volume") or 0), row))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    seen_urls: set[str] = set()
    out: list[dict[str, Any]] = []
    for overlap, volume, row in scored[:MATCH_LIMIT]:
        url = str(row.get("url"))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        out.append({
            "url": url,
            "host": str(row.get("source_domain") or ""),
            "matched_keyword": str(row.get("keyword")),
            "search_volume": int(row.get("search_volume") or 0),
            "keyword_difficulty": row.get("kd"),
            "competitor_rank": row.get("rank"),
            "intent": row.get("intent"),
            "match_basis": ("exact_keyword" if overlap >= 0.999
                            else f"token_overlap_{overlap:.2f}"),
        })
    return out


def gsc_signal(query: str, gsc_rows: Any) -> dict[str, Any] | None:
    """Search Console is a priority preference here, never an eligibility gate.

    ``gsc_preference`` returns a mapping of normalized query -> aggregated
    impressions/clicks/position, so this matches on the key.
    """
    if not gsc_rows:
        return None
    want = normalize(query)
    want_tokens = tokens(query)
    best = None
    for key, row in gsc_rows.items():
        key_tokens = tokens(key)
        related = key == want or (
            want_tokens and key_tokens
            and len(want_tokens & key_tokens) / len(want_tokens | key_tokens) >= 0.5)
        if not related:
            continue
        impressions = int(row.get("impressions") or 0)
        if best is None or impressions > best["impressions"]:
            best = {
                "query_text": key,
                "impressions": impressions,
                "clicks": int(row.get("clicks") or 0),
                "position": row.get("position"),
                "band": row.get("band"),
            }
    return best


def spoke_priority(volume: int, gsc: dict[str, Any] | None) -> int:
    """Spokes rank on measured demand, nudged up when GSC already sees the query."""
    if volume >= 10000:
        priority = 4
    elif volume >= 2000:
        priority = 3
    elif volume >= 500:
        priority = 2
    else:
        priority = 1
    if gsc and gsc.get("impressions", 0) > 0:
        priority = min(SPOKE_PRIORITY_MAX, priority + 1)
    return priority


def build_feed(*, strategy_path: Path, corpus_path: Path, database: Path,
               config_path: Path, firecrawl_supplement_path: Path,
               keyword_gap_supply_path: Path = DEFAULT_KEYWORD_GAP_SUPPLY) -> dict[str, Any]:
    policy = consumer_policy("competitor_gap_feed", decision_class="search")
    require_allowed(policy)
    search_policy = {**policy, "mode": "PASS_SEARCH_ONLY", "revenue_blind": True}
    selector = load_selector()
    config = selector.load_config(config_path)
    strategy = read_json(strategy_path)
    if strategy.get("schema") != "glp-seo-cluster-rotation-queue-v1":
        raise RuntimeError("strategy_queue_contract_invalid")
    corpus = read_json(corpus_path)
    index = corpus_index(corpus)
    firecrawl_supplement, firecrawl_meta = load_firecrawl_supplement(firecrawl_supplement_path)
    weekly_gaps, weekly_holds, weekly_meta = load_keyword_gap_supply(keyword_gap_supply_path)

    release_root = Path(config["_current_content_base_resolution"]["pointer"]["release_root"])
    live_routes = selector.current_catalog_routes(config) | selector.sitemap_routes(
        (release_root / "public/sitemap.xml").resolve(strict=True), config["vertical"]["domain"])
    published = selector.read_object(selector.DEFAULT_LEDGER.resolve(), "published_topic_ledger")
    held = selector.read_object(selector.DEFAULT_HOLD_LEDGER.resolve(), "held_topic_ledger")
    terminal_ids, terminal_routes = selector.ledger_terminals(published, held)
    receipt_ids, receipt_routes, _ = selector.terminal_receipts(selector.DEFAULT_RUNS.resolve())
    terminal_ids |= receipt_ids
    terminal_routes |= receipt_routes
    try:
        gsc_rows, gsc_window = selector.gsc_preference(database)
    except Exception:
        gsc_rows, gsc_window = [], None

    gaps: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in strategy.get("queue") or []:
        query = str(row.get("target_query") or "")
        route_value = str(row.get("target_route") or "")
        if not query or not route_value:
            skipped.append({"target_query": query, "reason": "strategy_row_incomplete"})
            continue
        route = selector.canonical_route(route_value)
        # Already shipped work is not a gap. The selector excludes these too;
        # filtering here keeps the feed honest about remaining supply.
        if route in live_routes or route in terminal_routes:
            skipped.append({"target_query": query, "target_route": route,
                            "reason": "already_live_or_terminal"})
            continue
        evidence = merge_exact_query_evidence(query, match_corpus(query, index), firecrawl_supplement)
        if len(evidence) < EVIDENCE_MINIMUM:
            # Real gap, but no measured competitor evidence to justify it yet.
            # Emitted as a research backlog entry rather than padded with
            # invented sources.
            skipped.append({
                "target_query": query, "target_route": route,
                "cluster_id": row.get("cluster_id"), "depth_stage": row.get("depth_stage"),
                "reason": "insufficient_measured_competitor_evidence",
                "evidence_found": len(evidence),
            })
            continue
        volume = max(item["search_volume"] for item in evidence)
        signal = gsc_signal(query, gsc_rows)
        is_pillar = str(row.get("depth_stage")) == "pillar"
        priority = PILLAR_PRIORITY if is_pillar else spoke_priority(volume, signal)
        rationale = (
            f"Cluster '{row.get('cluster_id')}' {row.get('depth_stage')} for the hub/spoke plan. "
            f"Highest measured monthly volume across {len(evidence)} matched competitor "
            f"keyword(s) is {volume}."
        )
        if is_pillar:
            rationale += (
                " Minted at top priority because this cluster's spokes are gated on "
                "requires_pillar_predecessor, so publishing the hub unblocks them.")
        if signal:
            rationale += (
                f" Search Console already records {signal['impressions']} impressions for "
                f"'{signal['query_text']}' at position {signal.get('position')}.")
        gaps.append({
            "keyword": query,
            "target_route": route,
            "opportunity_type": "competitor_gap_new_topic",
            "priority": priority,
            "rationale": rationale,
            "operator_ready_for_research": True,
            "operator_directive_id": f"{DIRECTIVE_PREFIX}-{hashlib.sha256(route.encode()).hexdigest()[:12]}",
            "evidence": evidence,
            "keyword_evidence": {
                "merged_search_volume": volume,
                "provider_count": len({item["host"] for item in evidence if item["host"]}),
                "match_basis": "strategy_rotation_queue_joined_to_measured_keyword_corpus",
            },
            "strategy_binding": {
                "cluster_id": row.get("cluster_id"),
                "cluster_name": row.get("cluster_name"),
                "depth_stage": row.get("depth_stage"),
                "queue_position": row.get("queue_position"),
                "requires_pillar_predecessor": row.get("requires_pillar_predecessor"),
            },
            "gsc_preference": signal,
        })

    # The operator's weekly order is existing-page class A in R2, then class B,
    # then class C. A never enters this net-new feed. Within the older strategy
    # rows, pillars still precede spokes so their predecessor gate remains true.
    seen_join_keys = {(normalize(str(row.get("keyword") or "")), str(row.get("target_route") or ""))
                      for row in gaps}
    for row in weekly_gaps:
        key = (normalize(str(row.get("keyword") or "")), str(row.get("target_route") or ""))
        if key in seen_join_keys:
            continue
        gaps.append(row)
        seen_join_keys.add(key)
    skipped.extend(weekly_holds)
    cogny_strategy, cogny_meta = load_strategy()
    for row in gaps:
        row["cogny_strategy_preference"] = preference(cogny_strategy, row["keyword"], row["target_route"], "new_article")
    gaps.sort(key=lambda item: (
        {"B": 0, "C": 1}.get(str(item.get("opportunity_class") or ""), 2),
        0 if (item.get("strategy_binding") or {}).get("depth_stage") == "pillar" else 1,
        -preference_score(cogny_strategy, item["keyword"], item["target_route"], "new_article"),
        -item["priority"],
        -item["keyword_evidence"]["merged_search_volume"],
        int((item.get("strategy_binding") or {}).get("queue_position") or 0),
    ))
    decision_receipts = []
    for row in gaps:
        volume = float((row.get("keyword_evidence") or {}).get("merged_search_volume") or 0)
        gsc = row.get("gsc_preference")
        decision_receipts.append(write_consumer_decision(
            consumer="competitor_gap_feed",
            decision_type="competitor_gap_inclusion",
            subject=str(row.get("keyword") or ""),
            action="include_in_research_feed",
            pre_signal_score=float(row.get("priority") or 0),
            components=[
                {"signal": "measured_competitor_demand", "available": True,
                 "delta": round(math.log10(volume + 1), 6)},
                {"signal": "gsc_existing_demand", "available": gsc is not None,
                 "delta": (1.0 if gsc and float(gsc.get("impressions") or 0) > 0 else 0.0)
                          if gsc is not None else None,
                 "reason": None if gsc is not None else "gsc_query_signal_unavailable"},
                {"signal": "normalized_revenue", "available": False, "delta": None,
                 "reason": "search_supply_decision_is_revenue_blind"},
            ],
            policy=search_policy,
        ))
    pillars = sum(1 for g in gaps if (g.get("strategy_binding") or {}).get("depth_stage") == "pillar")
    return {
        "schema": SCHEMA,
        "status": "PASS_SEARCH_ONLY",
        "created_at": now_utc(),
        "site_id": config.get("site_id"),
        "generator": "mint_competitor_gap_feed.py",
        "data_quality_mode": "PASS_SEARCH_ONLY",
        "revenue_blind": True,
        "sources": {
            "strategy_queue": {"path": str(strategy_path),
                               "sha256": hashlib.sha256(strategy_path.read_bytes()).hexdigest()},
            "keyword_corpus": {"path": str(corpus_path),
                               "sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
                               "rows": len(corpus)},
            "gsc_window": gsc_window,
            "firecrawl_supplement": firecrawl_meta,
            "weekly_keyword_gap_supply": weekly_meta,
            "cogny_weekly_strategy": cogny_meta,
            "current_content_base": str(release_root),
        },
        "summary": {
            "gaps": len(gaps),
            "pillars": pillars,
            "spokes": len(gaps) - pillars,
            "skipped": len(skipped),
            "provider_calls": 0,
            "incremental_spend_usd": 0.0,
            "publishes": 0,
            "site_writes": 0,
        },
        "gaps": gaps,
        "research_backlog": skipped,
        "decision_receipts": decision_receipts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-queue", type=Path)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--database", type=Path, default=BUSINESS / (
        "reports/glp-clinic-scout/operator-national-wow-20260723/data/glp-observability.sqlite"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--firecrawl-supplement", type=Path, default=DEFAULT_FIRECRAWL_SUPPLEMENT)
    parser.add_argument("--keyword-gap-supply", type=Path, default=DEFAULT_KEYWORD_GAP_SUPPLY)
    parser.add_argument("--print-only", action="store_true")
    args = parser.parse_args()

    selector = load_selector()
    strategy_path = (args.strategy_queue or selector.DEFAULT_STRATEGY_QUEUE).resolve(strict=True)
    config_path = (args.config or selector.DEFAULT_CONFIG).resolve(strict=True)
    feed = build_feed(strategy_path=strategy_path, corpus_path=args.corpus.resolve(strict=True),
                      database=args.database, config_path=config_path,
                      firecrawl_supplement_path=args.firecrawl_supplement,
                      keyword_gap_supply_path=args.keyword_gap_supply)
    if args.print_only:
        print(json.dumps(feed["summary"], indent=2, sort_keys=True))
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    temporary.write_text(json.dumps(feed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"status": feed["status"], "output": str(args.output),
                      **feed["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
