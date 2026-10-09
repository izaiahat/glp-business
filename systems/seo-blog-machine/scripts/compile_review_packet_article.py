#!/usr/bin/env python3
"""Compile one sealed GLP review packet into the source-driven article contract."""
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, re
from pathlib import Path
from typing import Any
import yaml

CITATION = re.compile(r"\[(S\d+)\]")
LINK = re.compile(r"\[([^\]]+)\]\((/[^)]+)\)")


def words(value: str) -> int:
    return len(re.findall(r"\b[\w'$.-]+\b", value))


def paragraph(lines: list[str]) -> str:
    return " ".join(line.strip() for line in lines).strip()


def parse_table(lines: list[str]) -> dict[str, Any]:
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    if len(rows) < 3 or len(rows[0]) != 2 or any(len(row) != 2 for row in rows):
        raise RuntimeError("table_not_two_column")
    return {"headers": rows[0], "rows": rows[2:]}


def parse_sections(body: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    sections: list[dict[str, Any]] = []
    faqs: list[dict[str, str]] = []
    current: dict[str, Any] | None = None
    mode = "body"
    buffer: list[str] = []
    table: list[str] = []

    def flush() -> None:
        nonlocal buffer, table
        if current is None:
            buffer, table = [], []
            return
        text = paragraph(buffer)
        if text:
            current.setdefault("paragraphs", []).append(text)
        if table:
            current.setdefault("tables", []).append(parse_table(table))
        buffer, table = [], []

    for raw in body.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            flush()
            heading = line[3:].strip()
            mode = "faq" if heading == "Frequently asked questions" else "skip" if heading in {"Table of contents", "Official sources"} else "body"
            current = None if mode != "body" else {"heading": heading, "paragraphs": [], "bullets": [], "ordered_items": [], "tables": []}
            if current is not None:
                sections.append(current)
            continue
        if mode == "faq" and line.startswith("### "):
            flush()
            current = {"heading": line[4:].strip(), "paragraphs": []}
            faqs.append({"q": current["heading"], "a": ""})
            continue
        if mode == "faq":
            if current is not None and line.strip():
                current["paragraphs"].append(line.strip())
                faqs[-1]["a"] = paragraph(current["paragraphs"])
            continue
        if mode != "body" or current is None:
            continue
        if line.startswith("### "):
            flush(); buffer.append(f"{line[4:].strip()}.")
        elif re.match(r"^\d+\.\s+", line):
            flush(); current["ordered_items"].append(re.sub(r"^\d+\.\s+", "", line).strip())
        elif line.startswith("- "):
            flush(); current["bullets"].append(line[2:].strip())
        elif line.startswith("|"):
            if buffer: flush()
            table.append(line)
        elif not line.strip():
            flush()
        else:
            buffer.append(line)
    flush()
    return sections, faqs


def r2_publication_date(authority: dict[str, Any]) -> str | None:
    """Read first-publication truth from the sealed predecessor, not frontmatter."""
    if authority.get('operation') != 'r2_refresh' or not authority.get('existing_article'):
        return None
    e = authority['existing_article']
    specs = [s for s in authority.get('inputs', {}).get('catalogs', []) if s.get('catalog_id') == e.get('catalog_id')]
    if len(specs) != 1:
        raise RuntimeError('r2_publication_catalog_binding_invalid')
    p = Path(specs[0]['path'])
    if p.is_symlink() or not p.is_file():
        raise RuntimeError('r2_publication_catalog_not_regular')
    raw = p.read_bytes()
    if hashlib.sha256(raw).hexdigest() != specs[0].get('sha256'):
        raise RuntimeError('r2_publication_catalog_hash_drift')
    rows = json.loads(raw)[e['records_key']]
    index = e.get('article_index')
    if type(index) is not int or not 0 <= index < len(rows):
        raise RuntimeError('r2_publication_article_index_invalid')
    a = rows[index]
    canonical = (json.dumps(a, sort_keys=True, separators=(',', ':'), ensure_ascii=False)+'\n').encode()
    if (hashlib.sha256(canonical).hexdigest() != e.get('article_sha256')
            or a.get('article_id') != e.get('article_id')
            or a.get('article_id') != authority.get('existing_article_id')
            or a.get('canonical_path') != e.get('canonical_path')):
        raise RuntimeError('r2_publication_article_binding_invalid')
    value = a.get('date_published') or a.get('updated_at')
    if not isinstance(value, str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is None:
        raise RuntimeError('r2_publication_date_invalid')
    dt.date.fromisoformat(value)
    return value


def article_source_dates(meta):
    """A source check is not evidence that a price was checked."""
    return {'source_checked_through':meta.get('sourceCheckedThrough') or meta.get('pricesCheckedDate'),
            'prices_checked_date':meta.get('pricesCheckedDate')}


def commercial_link_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    classification = meta.get('commercialLinkTaxonomy', 'medical_claims')
    if classification not in {'medical_claims','commercial_intent','editorial_noncommercial'}:
        raise RuntimeError('commercial_link_taxonomy_invalid')
    services = meta.get('exactServices', [])
    if not isinstance(services, list) or any(not isinstance(x, str) or not x.strip() for x in services):
        raise RuntimeError('exact_services_invalid')
    if classification == 'commercial_intent' and not services:
        raise RuntimeError('commercial_intent_services_missing')
    return {'commercial_link_taxonomy':classification, 'exact_services':services}


def compile_packet(packet: Path) -> dict[str, Any]:
    draft_path = packet / "04-draft.md"
    raw = draft_path.read_text()
    parts = raw.split("---", 2)
    if len(parts) != 3:
        raise RuntimeError("front_matter_missing")
    meta = yaml.safe_load(parts[1]); body = parts[2].strip()
    capture = meta.get("captureContract") or {}
    route_key = meta["slug"].strip("/").replace("/", "-")
    expected_capture = {
        "action": "/api/content-upgrade",
        "consentVersion": "glp-guide-resource-v1-20261007",
        "sourcePage": "guide-link",
        "leadMagnetKey": "guide-link-resource",
        "emailField": "email",
        "healthDataFields": ["guide_path"],
        "guidePath": meta["slug"],
        "purpose": "resource-delivery",
        "followUpConsent": False,
        "storedPageIdentifier": False,
    }
    legacy_capture = {"action": "/api/content-upgrade", "consentVersion": "glp-content-upgrade-v2-20260801",
                      "sourcePage": route_key, "leadMagnetKey": f"{route_key}-terms-checklist",
                      "emailField": "email", "healthDataFields": []}
    if not meta["slug"].startswith("/guides/"):
        expected_capture = legacy_capture
    elif capture == legacy_capture:
        # Exact retired packet metadata can compile only into the CURRENT
        # resource-delivery surface. Never render legacy marketing eligibility
        # or rewrite sealed source/evidence. Mixed or arbitrary contracts fail.
        capture = expected_capture.copy()
        # Pre-exactServices packets already declare the service subjects. Carry
        # that native metadata forward, never infer a provider from prose.
        if "exactServices" not in meta and "providersDiscussed" in meta:
            meta = dict(meta, exactServices=meta["providersDiscussed"])
    on_page_only = meta["slug"].startswith("/guides/") and capture.get("mode") == "on-page-only"
    if on_page_only:
        expected_capture = {"mode": "on-page-only", "emailField": None,
                            "healthDataFields": [], "followUpConsent": False}
        if capture != expected_capture:
            raise RuntimeError("capture_contract_invalid")
        if re.search(r"email\s+(?:me|this|the|a|your)|(?:inbox|subscribe)", body, re.I):
            raise RuntimeError("on_page_capture_promise_forbidden")
    elif any(capture.get(key) != value for key, value in expected_capture.items()):
        raise RuntimeError("capture_contract_invalid")
    ledger = json.loads((packet / "01-evidence-ledger.json").read_text())
    authority = json.loads((packet / "00-authority.json").read_text())
    mapping = json.loads((packet / "05-claim-source-map.json").read_text())
    serp = json.loads((packet / "02-serp-brief.json").read_text())
    quick = re.search(r"> \*\*Quick answer:\*\*\s*(.+)", body)
    if not quick:
        raise RuntimeError("quick_answer_missing")
    direct_answer = quick.group(1).strip()
    sections, faqs = parse_sections(body)
    source_claims = {row["source_id"]: [] for row in ledger["sources"]}
    claims = []
    for row in mapping["claims"]:
        source_ids = list(dict.fromkeys(row.get("citation_tokens") or row.get("source_ids") or []))
        claims.append({"claim_id": row["claim_id"], "statement": row["claim"], "source_ids": source_ids})
        for source_id in source_ids:
            if source_id in source_claims:
                source_claims[source_id].append(row["claim_id"])
    public_sources = [row for row in ledger["sources"] if str(row.get("url") or "").startswith("https://")]
    sources = [{"source_id": row["source_id"], "publisher": row["publisher"], "url": row["url"],
                "source_class": row.get("source_type") or row.get("tier") or "UNKNOWN",
                "verified_as_of": row.get("checked_date") or row.get("accessed_at"),
                "claim_ids": source_claims[row["source_id"]]} for row in public_sources]
    links = []
    for label, path in LINK.findall(body):
        # /go/ routes are tracked affiliate anchors, not related-guide links;
        # keeping them out of internal_links keeps them out of the rendered
        # "Keep comparing" nav, whose plain anchors carry no rel attribute.
        if path.startswith(("/authors/", "/go/", "/assets/")) or any(row["path"] == path for row in links):
            continue
        links.append({"label": label, "path": path})
    route_key = meta["slug"].strip("/").replace("/", "-")
    topic_id = str(authority.get("observed_queue_topic_id") or authority.get("topic_id") or authority.get("queue_authority", {}).get("topic_id") or route_key)
    intent = serp.get("intent") or serp.get("dominant_intent") or {}
    if isinstance(intent, dict):
        intent = intent.get("primary") or intent.get("classification") or "commercial investigation"
    parity = serp.get("competitive_parity") or serp.get("target_length_words") or {}
    wedge = serp.get("ranking_wedge") or serp.get("recommended_wedge") or {}
    operation = str(authority.get("operation") or "new_article")
    if operation not in {"new_article", "r2_refresh"}:
        raise RuntimeError("packet_operation_invalid")
    article_id = f"seo-{topic_id}-{route_key}"
    if operation == "r2_refresh":
        article_id = str(authority.get("existing_article_id") or "")
        if not article_id or not isinstance(authority.get("r2_contract"), dict):
            raise RuntimeError("r2_packet_authority_incomplete")
    result = {
        "schema": "blog-brief-v2-opus-autopublish", "article_id": article_id,
        "brief_id": f"seo-{topic_id}-{route_key}", "owner_key": route_key,
        "canonical_path": meta["slug"], "primary_keyword": meta["primaryKeyword"], "target_query": meta["primaryKeyword"],
        "title": meta["title"], "h1": meta["h1"], "meta_description": meta["metaDescription"],
        "updated_at": meta["dateModified"], **article_source_dates(meta),
        # Draft/check history never proves first publication. R2 can expose the
        # sealed predecessor's truth to review; new drafts remain pending None.
        # The builder still rebinds dates against its current predecessor.
        "date_published": r2_publication_date(authority),
        "status": "PASS_OPUS_PENDING", "risk_tier": str(meta.get("riskTier") or "MEDIUM").upper(), "search_intent": "transactional informational",
        "audience": f"U.S. readers researching {meta['primaryKeyword']} using current primary evidence",
        "intent": intent, "direct_answer": direct_answer,
        "medical_advice_disclaimer": "This evidence guide is educational and is not medical advice, diagnosis, individualized eligibility screening, or a treatment recommendation.",
        "author": {"author_id": "clinic-scout-editorial-team", "display_name": "Clinic Scout Editorial Team",
                   "bio_path": "/authors/clinic-scout-editorial-team/", "status": "verified_organization",
                   "entity_type": "Organization", "clinical_credentials_claimed": False},
        "byline_attribution": "By the Clinic Scout Editorial Team · no clinical credentials claimed",
        "ai_assistance_disclosure": "site-editorial-policy", "editorial_contract_version": "seo-phase1-v1",
        "sections": sections, "faqs": faqs, "internal_links": links,
        "claims": claims, "sources": sources, "comparison_required": False,
        "pros": ["Uses current primary evidence.", "Separates verified facts from unknowns.", "Maps decision-bearing claims to captured source text."],
        "cons": ["Terms, prices, labels, and coverage can change.", "Individual outcomes and eligibility cannot be inferred from general evidence.", "The page cannot replace clinician, plan, or pharmacy verification."],
        "conclusion": f"Use the dated source checks and decision framework above when evaluating {meta['primaryKeyword']}; verify current individual details with the relevant clinician, plan, pharmacy, or manufacturer.",
        "word_count": words(body), "batch_gate_passed": True, "operator_release_receipt": None,
        "has_affiliate_cta": False, "inline_ftc_disclosure": False,
        # Renderer medical/commercial taxonomy is not the rubric's three-way
        # commercial-link classification. Preserve the stricter medical guard.
        "surface_taxonomy": "medical", "suppress_cta": True,
        **commercial_link_metadata(meta),
        "capture_surface": ({"status": "ON_PAGE_ONLY", "email_field": None,
                             "health_data_fields": [], "follow_up_consent": False} if on_page_only else
                            {"status": "WORKING_BASE_TEMPLATE_CONTRACT", "action": capture["action"],
                            "source_page": capture["sourcePage"], "lead_magnet_key": capture["leadMagnetKey"],
                            "consent_version": capture["consentVersion"], "email_field": capture["emailField"],
                            "health_data_fields": capture["healthDataFields"],
                            **({"guide_path": capture["guidePath"], "purpose": capture["purpose"],
                                "follow_up_consent": capture["followUpConsent"],
                                "stored_page_identifier": capture["storedPageIdentifier"]}
                               if meta["slug"].startswith("/guides/") else {})}),
        "inbound_link_plan": serp.get("inbound_link_plan") or [],
        "serp_contract": {"path": str(packet / "02-serp-brief.json"),
                          "status": serp.get("status") or serp.get("schema_version") or "PASS",
                          "parity_target_words": parity.get("primary_target_words") or parity.get("target_words") or parity.get("target") or 3000,
                          "ranking_wedge": (wedge.get("statement") or wedge.get("wedge") if isinstance(wedge, dict) else wedge) or "source-bound decision support"},
        "evidence_review": {"status": "PASS", "reviewed_at": f"{meta['dateModified']}T00:00:00Z",
                            "content_sha256": hashlib.sha256(draft_path.read_bytes()).hexdigest(),
                            "claim_source_map": "claims[] -> source_ids[] -> sources[].source_id",
                            "clinical_reviewer": None, "clinical_credentials_claimed": False},
    }
    if operation == "r2_refresh":
        result["r2_contract"] = authority["r2_contract"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--packet", required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args(); value = compile_packet(Path(args.packet).resolve())
    output = Path(args.output).resolve(); output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": "PASS_ARTICLE_CONTRACT_COMPILED", "output": str(output),
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "word_count": value["word_count"]}, sort_keys=True))


if __name__ == "__main__":
    main()
