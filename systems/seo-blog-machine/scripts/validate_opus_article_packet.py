#!/usr/bin/env python3
"""Deterministic pre-Opus QA for a GLP article packet."""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from selection_receipt import (
    load_selection_receipt,
    validate_authority_selection_binding,
    validate_file_spec,
)
from existing_article_updates import _routes_in, _substantive_projection
from r2_update_carrier import object_hash, validate_r2_packet_contract, validate_update_authority
from machine_config import load_config

DEFAULT_CONFIG = Path("/home/ubuntu/business/systems/seo-blog-machine/config/verticals/glp-clinic-scout.json")

HERE = Path(__file__).resolve().parent
COMPILER = HERE / "compile_review_packet_article.py"
ROUTE_PATTERN = re.compile(r"^/(?:guides|best|reviews|cost|tools)/[a-z0-9]+(?:-[a-z0-9]+)*/$")
# The DEPLOYED renderer decides which namespaces can exist as catalog articles.
# ``scripts/build-staging.mjs`` (``addGuideMetadata``) throws
# ``article_canonical_path_invalid`` for anything outside /guides/ and /best/;
# /cost/, /tools/ and /reviews/ pages on the live site are hand-built statics,
# not batch articles. ROUTE_PATTERN above is deliberately wider because
# selection and link plans legitimately reference those static routes.
#
# 2026-08-18: a packet minted at /cost/glp1-pills-cost/ passed selection, the
# deterministic validator and a full five-gate review, entered the standing
# buffer, and only died inside ``npm run build`` — after the entire research +
# review spend. This check moves the renderer's own contract to the front of the
# chain. Keep it byte-identical to the renderer: the parity test in
# tests/test_renderer_article_route_namespace.py asserts the live accepted
# base still carries exactly this pattern.
RENDERER_ARTICLE_ROUTE_SOURCE = r"^\/(?:guides|best)\/[a-z0-9-]+\/$"
RENDERER_ARTICLE_ROUTE_PATTERN = re.compile(r"^/(?:guides|best)/[a-z0-9-]+/$")
PRIMARY_MEDICAL_HOSTS = (
    "fda.gov", "hhs.gov", "nih.gov", "ncbi.nlm.nih.gov",
    "pubmed.ncbi.nlm.nih.gov", "cms.gov", "ftc.gov", "uscourts.gov",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _serp_fully_observed(serp: dict) -> bool:
    """Require parity evidence or an exact dated failure for every observed result."""
    result_rows = serp.get("rankings", []) or serp.get("observed_serp", [])
    positions = [row.get("rank", row.get("position")) for row in result_rows]
    positions = [p for p in positions if isinstance(p, int)]
    contiguous = positions == list(range(1, 11))
    if not contiguous:
        contiguous = (
            5 <= len(positions)
            and positions == list(range(1, len(positions) + 1))
            and bool(str(serp.get("serp_depth_limitation") or "").strip())
        )
    if not contiguous:
        return False
    for row in result_rows:
        status = row.get("capture_status")
        provenance = row.get("capture_provenance") or {}
        if status == "CAPTURED_AND_EXTRACTED":
            if not (
                isinstance(row.get("normalized_article_words"), int)
                and row["normalized_article_words"] > 0
                and isinstance(row.get("outline"), list)
                and bool(row["outline"])
                and isinstance(row.get("entities"), list)
                and isinstance(row.get("page_features"), dict)
                and bool(row["page_features"])
                and bool(row.get("scrape_id"))
                and len(str(provenance.get("raw_capture_file_sha256") or "")) == 64
                and len(str(provenance.get("article_extract_sha256") or "")) == 64
                and bool(provenance.get("extraction_source_line_ranges_inclusive"))
            ):
                return False
        elif status == "CAPTURE_FAILED_CLOSED":
            if not (
                re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row.get("capture_date") or ""))
                and provenance.get("fail_closed") is True
                and bool(str(provenance.get("exact_failure") or "").strip())
                and len(str(provenance.get("failure_file_sha256") or "")) == 64
                and row.get("normalized_article_words") is None
                and row.get("outline") is None
                and row.get("entities") is None
            ):
                return False
        else:
            return False
    features = serp.get("serp_feature_inventory")
    return isinstance(features, dict) and all(
        bool(str(features.get(key) or "").strip())
        for key in ("featured_snippet", "people_also_ask", "image_pack", "video_pack")
    )


def claim_quote_line_coverage_complete(draft: str, mapping: dict, ledger: dict) -> bool:
    """Fail closed unless every cited factual line binds to owned exact quote IDs."""
    quote_owner = {
        quote.get("quote_id"): source.get("source_id")
        for source in ledger.get("sources", [])
        for quote in source.get("quotes", [])
        if quote.get("quote_id") and source.get("source_id")
    }
    claims = {row.get("claim_id"): row for row in mapping.get("claims", []) if row.get("claim_id")}
    if not claims or mapping.get("unmapped_claims") != []:
        return False
    for claim in claims.values():
        source_ids = set(claim.get("source_ids") or [])
        citation_tokens = set(claim.get("citation_tokens") or [])
        quote_ids = claim.get("evidence_quote_ids") or []
        if not source_ids or citation_tokens != source_ids or not quote_ids:
            return False
        if any(quote_id not in quote_owner or quote_owner[quote_id] not in source_ids for quote_id in quote_ids):
            return False

    lines = draft.splitlines()
    start = 0
    if lines and lines[0] == "---":
        try:
            start = lines.index("---", 1) + 1
        except ValueError:
            return False
    expected: dict[str, dict] = {}
    for index, line in enumerate(lines[start:], start + 1):
        if line.startswith("## Official sources"):
            break
        tokens = list(dict.fromkeys(re.findall(r"\[(S\d+)\]", line)))
        if not tokens:
            continue
        line_hash = hashlib.sha256(line.encode()).hexdigest()
        if line_hash in expected:
            return False
        expected[line_hash] = {"line_number": index, "tokens": tokens}

    coverage_rows = mapping.get("claim_line_coverage") or []
    if len(coverage_rows) != len(expected):
        return False
    seen: set[str] = set()
    for row in coverage_rows:
        line_hash = row.get("draft_line_sha256")
        if line_hash in seen or line_hash not in expected:
            return False
        seen.add(line_hash)
        expected_row = expected[line_hash]
        if row.get("draft_line_number") != expected_row["line_number"]:
            return False
        if row.get("citation_tokens") != expected_row["tokens"]:
            return False
        claim_ids = row.get("claim_ids") or []
        if not claim_ids or any(claim_id not in claims for claim_id in claim_ids):
            return False
        mapped_sources = {
            source_id for claim_id in claim_ids for source_id in claims[claim_id].get("source_ids") or []
        }
        if mapped_sources != set(expected_row["tokens"]):
            return False
    return seen == set(expected)


def words(value: str) -> int:
    return len(re.findall(r"\b[\w'$.-]+\b", value))


def load_compiler():
    spec = importlib.util.spec_from_file_location("glp_opus_packet_compiler", COMPILER)
    if spec is None or spec.loader is None:
        raise RuntimeError("compiler_load_failed")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def atomic(path: Path, value: dict) -> None:
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(temp, 0o600); os.replace(temp, path)


def packet_authority_binding(authority: dict, expected_topic_id: str | None = None,
                             expected_route: str | None = None) -> dict[str, bool]:
    ids = [authority.get("topic_id"), authority.get("requested_queue_item_id"),
           authority.get("observed_queue_topic_id"),
           (authority.get("queue_authority") or {}).get("topic_id")]
    bound_ids = [str(value) for value in ids if value]
    routes = [authority.get("route"), (authority.get("target") or {}).get("route")]
    return {
        "authority_topic_ids_exact": bool(bound_ids) and len(set(bound_ids)) == 1,
        "authority_matches_requested_topic_id": expected_topic_id is None or (
            bool(bound_ids) and all(value == expected_topic_id for value in bound_ids)),
        "authority_matches_requested_route": expected_route is None or all(
            str(value) == expected_route for value in routes if value),
    }


def operation_route_presence_checks(operation: str, route_live: bool) -> dict[str, bool]:
    if operation == "new_article":
        return {"target_route_not_live": not route_live, "r2_target_route_is_live": True}
    if operation == "r2_refresh":
        return {"target_route_not_live": True, "r2_target_route_is_live": route_live}
    raise RuntimeError("packet_operation_invalid")


def article_source_contract_checks(article: dict) -> dict[str, bool]:
    """Mirror the deployed renderer's source-count, map, and primary-host gate."""
    sources = article.get("sources") or []
    risk = str(article.get("risk_tier") or "MEDIUM").upper()
    minimum = 3 if risk == "MEDIUM" else 2
    source_rows_valid = isinstance(sources, list) and all(
        isinstance(row, dict)
        and str(row.get("url") or "").startswith("https://")
        and bool(row.get("source_id"))
        and isinstance(row.get("claim_ids"), list)
        and bool(row.get("verified_as_of"))
        for row in sources
    )
    source_ids = [row.get("source_id") for row in sources if isinstance(row, dict)]
    primary_present = risk != "MEDIUM" or any(
        any(
            (urlparse(str(row.get("url") or "")).hostname or "").endswith(host)
            for host in PRIMARY_MEDICAL_HOSTS
        )
        for row in sources
        if isinstance(row, dict)
    )
    return {
        "article_source_minimum_and_map_complete": source_rows_valid and len(sources) >= minimum,
        "article_source_ids_unique": len(set(source_ids)) == len(source_ids),
        "primary_medical_source_present": primary_present,
    }


def inbound_link_plan_publishable(
    article: dict,
    operation: str,
    source_routes: set[str] | None = None,
) -> bool:
    """Enforce the release builder's inbound_link_plan contract before review.

    ``build_opus_article_release.py`` appends each plan's ``context_sentence``
    into the source article's last section, so the sentence must END with the
    markdown link to the target route — ``...](/cost/example/).`` — or the
    inserted cross-link renders as dead prose. The builder raises
    ``inbound_link_plan_invalid``, but it only runs AFTER the five-gate review
    and after admission, so on 2026-08-16 three unlinked packets passed review,
    entered the standing buffer, and every publish attempt died at the builder
    with the whole buffer unpublishable. This deterministic check moves that
    contract to the front of the chain where a repair round can still act.
    """
    plans = article.get("inbound_link_plan") or []
    if not isinstance(plans, list):
        return False
    for plan in plans:
        if not isinstance(plan, dict):
            return False
        target = str(plan.get("target_route") or "")
        sentence = str(plan.get("context_sentence") or "")
        if not target or not sentence.endswith(")."):
            return False
        if f"]({target})" not in sentence:
            return False
        if operation == "new_article" and target != article.get("canonical_path"):
            return False
        source_route = str(plan.get("source_route") or "")
        if not source_route:
            return False
        if source_routes is not None and source_route not in source_routes:
            return False
    return True


def current_article_catalog_routes(config_path: Path) -> set[str]:
    """Resolve the same two article catalogs the release builder can mutate."""
    config = load_config(config_path)
    routes: set[str] = set()
    for catalog in config["paths"]["content_catalogs"]:
        if catalog.get("kind") != "article":
            continue
        payload = json.loads(Path(catalog["_resolved_path"]).read_text(encoding="utf-8"))
        records = payload.get(catalog["records_key"])
        if not isinstance(records, list):
            raise RuntimeError(f"article_catalog_records_invalid:{catalog['catalog_id']}")
        for row in records:
            route = row.get("canonical_path") if isinstance(row, dict) else None
            if isinstance(route, str) and route:
                routes.add(route)
    return routes


def r2_predecessor_article(config_path: Path, update_authority: dict) -> dict:
    """Load the exact authority-bound predecessor used by the release builder."""
    identity = update_authority.get("existing_article") or {}
    config = load_config(config_path)
    catalogs = [
        catalog for catalog in config["paths"]["content_catalogs"]
        if catalog.get("catalog_id") == identity.get("catalog_id")
    ]
    if len(catalogs) != 1:
        raise RuntimeError(f"r2_predecessor_catalog_match_invalid:{len(catalogs)}")
    catalog = catalogs[0]
    payload = json.loads(Path(catalog["_resolved_path"]).read_text(encoding="utf-8"))
    records = payload
    for part in str(identity.get("records_key") or "").split("."):
        if part:
            records = records.get(part) if isinstance(records, dict) else None
    index = identity.get("article_index")
    if (
        not isinstance(records, list)
        or not isinstance(index, int)
        or isinstance(index, bool)
        or index < 0
        or index >= len(records)
        or not isinstance(records[index], dict)
    ):
        raise RuntimeError("r2_predecessor_record_invalid")
    predecessor = records[index]
    if (
        predecessor.get("canonical_path") != identity.get("canonical_path")
        or predecessor.get("article_id") != identity.get("article_id")
        or object_hash(predecessor) != identity.get("article_sha256")
    ):
        raise RuntimeError("r2_predecessor_identity_drift")
    return predecessor


def r2_new_outbound_internal_links(
    predecessor: dict,
    candidate: dict,
    current_routes: set[str],
) -> list[str]:
    """Mirror the downstream R2 novelty rule before review and publication.

    ``inbound_link_plan`` is excluded by the downstream substantive projection:
    a link from another article to this route is not a new outbound link from
    the refreshed article.
    """
    route = str(predecessor.get("canonical_path") or "")
    old_links = _routes_in(_substantive_projection(predecessor))
    return sorted(
        link
        for link in _routes_in(_substantive_projection(candidate)) - old_links
        if link != route and link in current_routes
    )



# A literal HTML tag or comment anywhere in a prose field. The renderer treats
# every prose field as a TEXT node and escapes it (correctly), so a tag in prose
# does not become markup — it becomes visible garbage in the reader's copy.
HTML_MARKUP_IN_PROSE = re.compile(r"<\s*/?\s*(?:[a-zA-Z][^>]*)?>|<!--|&lt;\s*/?\s*[a-zA-Z!]|&gt;")

PROSE_SCALAR_FIELDS = (
    "title", "h1", "meta_description", "direct_answer", "conclusion",
    "byline_attribution", "medical_advice_disclaimer", "audience", "intent",
)
PROSE_LIST_FIELDS = ("pros", "cons")


def _prose_strings(article: dict) -> list[tuple[str, str]]:
    """Every reader-visible string the renderer emits as an escaped text node."""
    rows: list[tuple[str, str]] = []

    def add(label: str, value: object) -> None:
        if isinstance(value, str):
            rows.append((label, value))

    for name in PROSE_SCALAR_FIELDS:
        add(name, article.get(name))
    for name in PROSE_LIST_FIELDS:
        for index, item in enumerate(article.get(name) or []):
            add(f"{name}[{index}]", item)
    for index, section in enumerate(article.get("sections") or []):
        if not isinstance(section, dict):
            continue
        add(f"sections[{index}].heading", section.get("heading"))
        for key in ("paragraphs", "bullets", "ordered", "items"):
            for item_index, item in enumerate(section.get(key) or []):
                add(f"sections[{index}].{key}[{item_index}]", item)
        for table_index, table in enumerate(section.get("tables") or []):
            if not isinstance(table, dict):
                continue
            add(f"sections[{index}].tables[{table_index}].caption", table.get("caption"))
            for cell_index, cell in enumerate(table.get("headers") or []):
                add(f"sections[{index}].tables[{table_index}].headers[{cell_index}]", cell)
            for row_index, row in enumerate(table.get("rows") or []):
                for cell_index, cell in enumerate(row or []):
                    add(f"sections[{index}].tables[{table_index}].rows[{row_index}][{cell_index}]", cell)
    for index, faq in enumerate(article.get("faqs") or []):
        if isinstance(faq, dict):
            add(f"faqs[{index}].question", faq.get("question") or faq.get("q"))
            add(f"faqs[{index}].answer", faq.get("answer") or faq.get("a"))
    for index, claim in enumerate(article.get("claims") or []):
        if isinstance(claim, dict):
            add(f"claims[{index}].statement", claim.get("statement"))
    return rows


def prose_html_markup_offenders(article: dict) -> list[str]:
    """Return every prose field carrying literal HTML markup.

    2026-08-18 defect: the 08-16 hand-built packets wrote standalone
    ``<a id="slug"></a>`` jump anchors into 04-draft.md, and an older r2 refresh
    wrote a whole ``<!-- R2 TOC START --><nav>…</nav>`` block into section prose.
    The compiler carries those lines through as ordinary paragraphs and the
    renderer escapes them, so readers saw ``<a id="wegovy-oral-coverage-checker">``
    as body copy on two published pages.

    Nothing is lost by refusing them: ``src/blog-system.mjs`` already emits
    ``<h2 id="section-N-slug">`` for every section AND its own ``guide-toc``
    jump-link nav, so in-page anchors are the renderer's job, never the prose's.
    """
    return [label for label, value in _prose_strings(article)
            if HTML_MARKUP_IN_PROSE.search(value)]


def deterministic_repair_request(
    checks: dict[str, bool],
    metrics: dict[str, int],
    *,
    parity_min_words: int,
    parity_max_words: int,
) -> list[dict[str, Any]]:
    """Explain failed checks to a drafter without changing any gate.

    Measurements and thresholds come from the same values used by ``validate``.
    The unchanged validator remains the sole PASS authority.
    """
    numeric: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
        "title_50_60": (
            {"title_chars": metrics["title_chars"]},
            {"minimum_chars": 50, "maximum_chars": 60},
        ),
        "meta_150_160": (
            {"meta_chars": metrics["meta_chars"]},
            {"minimum_chars": 150, "maximum_chars": 160},
        ),
        "quick_answer_40_60": (
            {"quick_answer_words": metrics["quick_answer_words"]},
            {"minimum_words": 40, "maximum_words": 60},
        ),
        "draft_parity_words": (
            {"draft_words": metrics["draft_words"]},
            {"minimum_words": parity_min_words, "maximum_words": parity_max_words},
        ),
        "faq_floor": (
            {"faq_count": metrics["faq_count"]},
            {"minimum_count": 5},
        ),
        "internal_link_floor": (
            {"internal_link_count": metrics["internal_link_count"]},
            {"minimum_count": 3},
        ),
        "r2_new_outbound_internal_link_present": (
            {
                "r2_new_outbound_internal_link_count":
                    metrics.get("r2_new_outbound_internal_link_count", 0)
            },
            {"minimum_count": 1},
        ),
        "two_column_tables": (
            {
                "table_count": metrics["table_count"],
                "non_two_column_table_count": metrics["non_two_column_table_count"],
            },
            {"minimum_table_count": 3, "required_columns_per_table": 2},
        ),
    }
    draft_checks = {
        "single_h1_exact", "title_50_60", "meta_150_160", "keyword_first_100",
        "quick_answer_40_60", "draft_parity_words", "faq_floor", "internal_link_floor",
        "r2_new_outbound_internal_link_present",
        "organization_author", "article_and_faq_schema_requested", "capture_contract",
        "two_column_tables", "forbidden_patterns_absent", "no_affiliate_cta",
        "prose_free_of_html_markup", "inbound_link_plan_publishable",
    }
    claim_checks = {
        "all_citations_known", "all_claim_quote_ids_known", "all_article_claim_sources_known",
        "claim_quote_line_coverage_complete", "claim_map_status",
        "article_source_minimum_and_map_complete", "article_source_ids_unique",
        "primary_medical_source_present",
    }
    evidence_checks = {"evidence_status"}
    serp_checks = {"serp_top_10", "serp_wedge_present"}
    immutable_checks = {
        "authority_topic_ready", "authority_topic_id_present", "authority_topic_ids_exact",
        "authority_matches_requested_topic_id", "authority_matches_requested_route",
        "canonical_route_renderer_supported", "selection_receipt_bound",
        "selection_topic_query_route_exact", "selection_operation_exact",
        "target_route_not_live", "r2_contract_bound", "r2_target_route_is_live",
        "r2_article_id_exact", "local_current_sitemap_hash_bound",
    }
    requests: list[dict[str, Any]] = []
    for name in sorted(check for check, passed in checks.items() if not passed):
        measured, required = numeric.get(name, ({"passed": False}, {"passed": True}))
        if name in draft_checks:
            allowed = ["03-editorial-brief.md", "04-draft.md", "05-claim-source-map.json"]
        elif name in claim_checks:
            allowed = ["01-evidence-ledger.json", "04-draft.md", "05-claim-source-map.json"]
        elif name in evidence_checks:
            allowed = ["01-evidence-ledger.json", "03-editorial-brief.md", "04-draft.md", "05-claim-source-map.json"]
        elif name in serp_checks:
            allowed = ["02-serp-brief.json", "03-editorial-brief.md", "04-draft.md"]
        else:
            allowed = []
        repairable = name not in immutable_checks and bool(allowed)
        if name == "draft_parity_words":
            required_fix = (
                f"Revise the article to {parity_min_words}..{parity_max_words} draft words. "
                "If expansion is required, add only topic-relevant, evidence-backed analysis, comparisons, "
                "reader decisions, or safety context and bind factual additions to existing captured sources. "
                "If reduction is required, remove repetition without dropping evidence, safety boundaries, or "
                "reader utility. Never add filler, repetition, invented claims, citations, or sources."
            )
        elif name == "r2_new_outbound_internal_link_present" and repairable:
            required_fix = (
                "Add at least one reader-useful outbound internal link in the refreshed article to a current "
                "site route that is absent from the authority-bound predecessor. A separate inbound_link_plan "
                "points from another article to this route and does not satisfy this outbound-link requirement. "
                "Preserve every passing check and never add filler or fabricate claims, evidence, citations, or sources."
            )
        elif repairable:
            required_fix = (
                f"Make the smallest substantive packet-source changes that cause {name} to meet "
                f"{json.dumps(required, sort_keys=True)} on the unchanged validator. Preserve every passing "
                "check. Never use filler or fabricate claims, evidence, citations, or sources."
            )
        else:
            required_fix = (
                "This authority or binding defect is not drafter-repairable. Preserve the gate and fail this "
                "authority closed; the carrier must substitute the next authorised authority. Required: "
                f"{json.dumps(required, sort_keys=True)}."
            )
        requests.append({
            "check": name,
            "measured": measured,
            "required": required,
            "repairable": repairable,
            "allowed_subject_files": allowed,
            "required_fix": required_fix,
        })
    return requests


def validate(packet: Path, expected_topic_id: str | None = None,
             expected_query: str | None = None, expected_route: str | None = None,
             selection_receipt: Path | None = None, operation: str = "new_article",
             r2_authority: Path | None = None, config_path: Path = DEFAULT_CONFIG) -> dict:
    if operation not in {"new_article", "r2_refresh"}:
        raise RuntimeError("packet_operation_invalid")
    if operation == "new_article" and r2_authority is not None:
        raise RuntimeError("r2_authority_only_allowed_for_r2_refresh")
    failures: list[str] = []
    required = ["00-authority.json", "01-evidence-ledger.json", "02-serp-brief.json",
                "03-editorial-brief.md", "04-draft.md", "05-claim-source-map.json"]
    if any(not (packet / name).is_file() for name in required):
        raise RuntimeError("required_packet_file_missing")
    authority = json.loads((packet / required[0]).read_text())
    receipt_path = selection_receipt or Path(str((authority.get("selection_receipt") or {}).get("path") or ""))
    selection_payload, selected = load_selection_receipt(receipt_path)
    validate_authority_selection_binding(authority, receipt_path)
    update_authority = None
    if operation == "r2_refresh":
        if r2_authority is None:
            raise RuntimeError("r2_update_authority_required")
        update_authority = validate_update_authority(r2_authority, config_path=config_path)
        update_spec = selection_payload["inputs"].get("update_authority") or {}
        resolved_authority = r2_authority.resolve(strict=True)
        if Path(str(update_spec.get("path") or "")).resolve(strict=True) != resolved_authority:
            raise RuntimeError("r2_selection_authority_path_mismatch")
        validate_file_spec(update_spec, "r2_update_authority")
        direct_selected = {
            "topic_id": update_authority["topic_id"],
            "article_id": update_authority["article_id"],
            "target_query": update_authority["target_query"],
            "target_route": update_authority["target_route"],
            "refresh_tier": update_authority["refresh_tier"],
            "priority": update_authority["priority"],
            "r2_contract": update_authority["r2_contract"],
        }
        for key, value in direct_selected.items():
            if selected.get(key) != value:
                raise RuntimeError(f"r2_selection_authority_field_mismatch:{key}")
        selected = {**selected, **direct_selected}
    if expected_topic_id is not None and selected["topic_id"] != expected_topic_id:
        raise RuntimeError("selection_topic_id_mismatch")
    if expected_query is not None and selected["target_query"] != expected_query:
        raise RuntimeError("selection_query_mismatch")
    if expected_route is not None and selected["target_route"] != expected_route:
        raise RuntimeError("selection_route_mismatch")
    if not ROUTE_PATTERN.fullmatch(selected["target_route"]):
        raise RuntimeError("selection_route_namespace_invalid")
    ledger = json.loads((packet / required[1]).read_text())
    serp = json.loads((packet / required[2]).read_text())
    mapping = json.loads((packet / required[5]).read_text())
    draft = (packet / "04-draft.md").read_text()
    article = load_compiler().compile_packet(packet)
    article_path = packet / "06-article.json"
    article_path.write_text(json.dumps(article, indent=2, sort_keys=True) + "\n")
    r2_new_outbound_links: list[str] = []
    if update_authority is not None:
        validate_r2_packet_contract(authority, article, update_authority)
        predecessor = r2_predecessor_article(config_path, update_authority)
        r2_new_outbound_links = r2_new_outbound_internal_links(
            predecessor,
            article,
            set((update_authority.get("r2_contract") or {}).get("current_routes") or []),
        )
    title = article["title"]; meta = article["meta_description"]
    quick = article["direct_answer"]
    h1s = re.findall(r"^# (.+)$", draft, re.M)
    body = draft.split("---", 2)[-1]
    first_100 = " ".join(body.split()[:100]).lower()
    topic_id = str(authority.get("observed_queue_topic_id") or authority.get("topic_id") or authority.get("queue_authority", {}).get("topic_id") or "")
    queue_authority = authority.get("queue_authority") or {}
    parity_target = int(article.get("serp_contract", {}).get("parity_target_words") or 3000)
    checks = {
        "authority_topic_id_present": len(topic_id) >= 12,
        "authority_topic_ready": (queue_authority.get("evidence_state") or authority.get("evidence_state")
                                  or authority.get("authorization", {}).get("queue_evidence_state")) == "READY_FOR_RESEARCH",
        "title_50_60": 50 <= len(title) <= 60,
        "meta_150_160": 150 <= len(meta) <= 160,
        "single_h1_exact": h1s == [article["h1"]],
        "keyword_first_100": article["primary_keyword"] in first_100,
        "quick_answer_40_60": 40 <= words(quick) <= 60,
        "draft_parity_words": parity_target
        <= article["word_count"]
        <= max(3600, int(parity_target * 1.10)),
        # Re-anchored 2026-08-06: require a FULLY OBSERVED contiguous SERP, not
        # exactly 10. Long-tail queries legitimately expose fewer than 10 organic
        # results; padding to 10 would fabricate rankings. A shallow SERP is
        # acceptable only when it is contiguous from position 1 AND the packet
        # documents WHY it is shallow (serp_depth_limitation) — that honesty
        # field makes this stricter, not weaker.
        "serp_top_10": _serp_fully_observed(serp),
        "serp_wedge_present": bool((serp.get("ranking_wedge") or {}).get("statement")
                                   if isinstance(serp.get("ranking_wedge"), dict)
                                   else serp.get("ranking_wedge")) or bool(serp.get("recommended_wedge")),
        "faq_floor": len(article["faqs"]) >= 5,
        "internal_link_floor": len(article["internal_links"]) >= 3,
        "organization_author": article["author"].get("entity_type") == "Organization" and article["author"].get("clinical_credentials_claimed") is False,
        "article_and_faq_schema_requested": set(["Article", "FAQPage"]) <= set(__import__("yaml").safe_load(draft.split("---", 2)[1])["schemaRequirements"]),
        "capture_contract": article.get("capture_surface", {}).get("status") == "WORKING_BASE_TEMPLATE_CONTRACT" and article["capture_surface"].get("health_data_fields") == [],
        "two_column_tables": sum(len(section.get("tables") or []) for section in article["sections"]) >= 3 and all(len(table["headers"]) == 2 and all(len(row) == 2 for row in table["rows"]) for section in article["sections"] for table in section.get("tables") or []),
        "claim_map_status": mapping.get("status") in {"PASS_ALL_FACTUAL_CLAIMS_MAPPED_TO_CAPTURED_QUOTES", "READY_FOR_OPUS_REVIEW"},
        "evidence_status": ledger.get("status") == "PASS_SOURCE_PACK" or bool(ledger.get("sources")),
    }
    checks.update(packet_authority_binding(authority, expected_topic_id, expected_route))
    checks["selection_receipt_bound"] = True
    checks["selection_topic_query_route_exact"] = (
        topic_id == selected["topic_id"]
        and article["canonical_path"] == selected["target_route"]
        and " ".join(str(article["primary_keyword"]).lower().split()) == " ".join(selected["target_query"].lower().split())
    )
    source_ids = {row["source_id"] for row in ledger.get("sources", [])}
    quote_ids = {quote["quote_id"] for row in ledger.get("sources", []) for quote in row.get("quotes", [])}
    checks["all_citations_known"] = set(re.findall(r"\[(S\d+)\]", draft)) <= source_ids
    checks["all_claim_quote_ids_known"] = all(set(row.get("evidence_quote_ids") or []) <= quote_ids for row in mapping.get("claims", []))
    checks["claim_quote_line_coverage_complete"] = claim_quote_line_coverage_complete(draft, mapping, ledger)
    checks["all_article_claim_sources_known"] = all(set(row.get("source_ids") or []) <= source_ids for row in article["claims"])
    checks.update(article_source_contract_checks(article))
    forbidden = [r"(?i)guaranteed copay", r"(?i)medically reviewed by",
                 r"(?i)\b(?:dose schedule|weight loss result|expected weight loss)\b", r'href="/go/']
    checks["forbidden_patterns_absent"] = not any(re.search(pattern, draft) for pattern in forbidden)
    checks["no_affiliate_cta"] = article["has_affiliate_cta"] is False
    checks["inbound_link_plan_publishable"] = inbound_link_plan_publishable(
        article,
        operation,
        current_article_catalog_routes(config_path),
    )
    checks["canonical_route_renderer_supported"] = bool(
        RENDERER_ARTICLE_ROUTE_PATTERN.fullmatch(str(article.get("canonical_path") or ""))
    )
    checks["prose_free_of_html_markup"] = not prose_html_markup_offenders(article)
    sitemap_path = validate_file_spec(selection_payload["inputs"]["sitemap_readback"], "selection_sitemap")
    sitemap_text = sitemap_path.read_text(encoding="utf-8")
    checks["local_current_sitemap_hash_bound"] = True
    route_live = f"https://glpclinicscout.com{article['canonical_path']}" in sitemap_text
    checks.update(operation_route_presence_checks(operation, route_live))
    checks["selection_operation_exact"] = selection_payload.get("operation", "new_article") == operation
    if operation == "r2_refresh":
        checks["r2_article_id_exact"] = article.get("article_id") == selected.get("article_id")
        checks["r2_contract_bound"] = article.get("r2_contract") == authority.get("r2_contract")
        checks["r2_new_outbound_internal_link_present"] = bool(r2_new_outbound_links)
    for key, passed in checks.items():
        if not passed:
            failures.append(key)
    table_count = sum(len(section.get("tables") or []) for section in article["sections"])
    non_two_column_table_count = sum(
        1 for section in article["sections"] for table in section.get("tables") or []
        if len(table["headers"]) != 2 or any(len(row) != 2 for row in table["rows"])
    )
    parity_max_words = max(3600, int(parity_target * 1.10))
    metrics = {
        "title_chars": len(title), "meta_chars": len(meta),
        "quick_answer_words": words(quick), "draft_words": article["word_count"],
        "draft_parity_min_words": parity_target,
        "draft_parity_max_words": parity_max_words,
        "faq_count": len(article["faqs"]),
        "internal_link_count": len(article["internal_links"]),
        "r2_new_outbound_internal_link_count": len(r2_new_outbound_links),
        "table_count": table_count,
        "non_two_column_table_count": non_two_column_table_count,
    }
    return {"schema": "glp-opus-seo-deterministic-qa-v1", "status": "PASS" if not failures else "BLOCKED",
            "checked_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "checks": checks, "failures": failures,
            "metrics": metrics,
            "repair_request": deterministic_repair_request(
                checks, metrics,
                parity_min_words=parity_target,
                parity_max_words=parity_max_words,
            ),
            "artifacts": {name: {"sha256": sha(packet / name), "bytes": (packet / name).stat().st_size}
                          for name in required + ["06-article.json"]},
            "effects": {"publishes": 0, "deployments": 0, "cache_purges": 0, "gsc_submissions": 0}}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--packet", required=True)
    parser.add_argument("--expected-topic-id"); parser.add_argument("--expected-query"); parser.add_argument("--expected-route")
    parser.add_argument("--selection-receipt", type=Path, required=True)
    parser.add_argument("--operation", choices=("new_article", "r2_refresh"), default="new_article")
    parser.add_argument("--r2-authority", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(); packet = Path(args.packet).resolve()
    receipt = validate(
        packet,
        expected_topic_id=args.expected_topic_id,
        expected_query=args.expected_query,
        expected_route=args.expected_route,
        selection_receipt=args.selection_receipt.resolve(),
        operation=args.operation,
        r2_authority=args.r2_authority,
        config_path=args.config,
    )
    atomic(packet / "07-qa-receipt.json", receipt)
    print(json.dumps({"status": receipt["status"], "failures": receipt["failures"], "receipt": str(packet / "07-qa-receipt.json")}, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
