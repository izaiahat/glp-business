#!/usr/bin/env python3
"""Operator-ordered defect remediation for an already-published article.

A published page can carry a defect that no normal operation can repair. A
`new_article` release may not touch it (it is outside the delta contract), and an
`r2_refresh` demands growth — a substantive content delta, a new authoritative
source, a new outbound link, striking-distance inbound links — none of which a
pure defect DELETION can honestly provide. Before this module the only ways out
were to fake an R2 authority or to leave the defect live. Both were refused.

This is the narrow third path, and it is deliberately stricter than R2 rather
than looser: the remediated article must equal the published one with the defect
removed and NOTHING else changed. That equality is asserted structurally, so the
operation physically cannot smuggle a content edit through an operator order.

Standing pieces:

* an **authority registry** — each directive carries the operator order date, the
  defect report it cites, the defect class, and the exact routes it covers.
  Nothing outside a registered directive's route list can be touched.
* a **deterministic QA** that proves the remediation is deletion-only, replacing
  the five-gate review for this operation (there is no new prose to review).
* the **existing** COW build, `npm test`, delta gate, seal, deploy and live
  readback chain — no gate is skipped or softened.

First directive: the 2026-08-18 operator order to remove literal HTML that was
rendering as visible body copy ("fix the current article and make sure no other
articles have this"). It will recur — a future design pass or importer can leak
markup again — which is why this ships as machinery rather than a hand-fix.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BUSINESS = Path("/home/ubuntu/business")
SCRIPTS = Path(__file__).resolve().parent
DEFECT_REPORT = BUSINESS / "reports/glp-clinic-scout/seo-machine/CARRIER-ANCHOR-FIX-20260817.md"

OPERATION = "defect_remediation"
AUTHORITY_SCHEMA = "glp-seo-defect-remediation-authority-v1"
QA_SCHEMA = "glp-seo-defect-remediation-qa-v1"
RECEIPT_STATUS = "PASS_OPERATOR_DIRECTED_CURRENT_BYTE"

DIRECTIVES: dict[str, dict[str, Any]] = {
    "operator-directed-2026-08-15-glp-seo-article-update-bing-persistence": {
        # Pre-existing directive, retained exactly as the builder hardcoded it
        # before this registry existed. Not a prose-markup remediation.
        "operator_order_date": "2026-08-15",
        "defect_class": "legacy_operator_directed_current_byte",
        "routes": None,  # scope was enforced by its own r2 authority
        "defect_report": None,
    },
    "operator-directed-2026-08-18-glp-prose-html-markup-remediation": {
        "operator_order_date": "2026-08-18",
        "operator_order": (
            "fix the current article and make sure no other articles have this "
            "(literal HTML tags rendering as visible body copy)"),
        "defect_class": "prose_html_markup",
        "defect_report": str(DEFECT_REPORT),
        "routes": (
            "/guides/oral-glp-1-insurance-coverage/",
            "/guides/mounjaro-vs-zepbound-label-and-cost/",
        ),
        "rationale": (
            "src/blog-system.mjs renders every prose field through esc() as a text node, so a "
            "literal tag in prose can never become markup — it becomes visible reader copy. Two "
            "writers leaked it: the 2026-08-16 hand-built packet scripts (standalone <a id> jump "
            "anchors) and an older r2 refresh (a raw <!-- R2 TOC --> <nav> block). The renderer "
            "already emits <h2 id=\"section-N-slug\"> and its own guide-toc nav, so removing the "
            "markup restores the intended page exactly."),
    },
}


class DefectRemediationError(RuntimeError):
    pass


def _load_builder():
    name = "build_opus_article_release"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / "build_opus_article_release.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_validator():
    name = "validate_opus_article_packet"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, SCRIPTS / "validate_opus_article_packet.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def payload_hash(value: dict[str, Any], field: str) -> str:
    unsigned = dict(value)
    unsigned.pop(field, None)
    return sha_bytes(canonical(unsigned))


def directive(directive_id: str) -> dict[str, Any]:
    row = DIRECTIVES.get(str(directive_id))
    if not row:
        raise DefectRemediationError(f"defect_directive_unauthorized:{directive_id}")
    return row


def directive_allows_route(directive_id: str, route: str) -> bool:
    routes = directive(directive_id).get("routes")
    return routes is None or route in routes


def offenders(article: dict[str, Any]) -> list[str]:
    return _load_validator().prose_html_markup_offenders(article)


def remediate_article(before: dict[str, Any]) -> dict[str, Any]:
    """Return the published record with leaked markup deleted and nothing else."""
    builder = _load_builder()
    after = copy.deepcopy(before)
    route = str(after.get("canonical_path") or "")
    builder.scrub_catalog_prose({"_": {"articles": [after]}}, {route})
    return after


def validate_remediation(before: dict[str, Any], after: dict[str, Any],
                         *, directive_id: str) -> dict[str, Any]:
    """Fail closed unless `after` is exactly `before` minus the named defect.

    The equality check is the whole safety argument: an operator order authorises
    removing a defect, never rewriting an article. Recomputing the remediation
    from `before` and demanding the result match `after` byte-for-byte means a
    content edit cannot ride along, whatever produced `after`.
    """
    row = directive(directive_id)
    route = str(before.get("canonical_path") or "")
    if not route or not directive_allows_route(directive_id, route):
        raise DefectRemediationError(f"defect_route_outside_directive:{route}")
    if after.get("canonical_path") != route:
        raise DefectRemediationError("defect_canonical_route_changed")
    if not before.get("article_id") or after.get("article_id") != before.get("article_id"):
        raise DefectRemediationError("defect_article_id_changed")

    before_offenders = offenders(before)
    if not before_offenders:
        raise DefectRemediationError(f"defect_absent_from_published_article:{route}")
    after_offenders = offenders(after)
    if after_offenders:
        raise DefectRemediationError(f"defect_not_removed:{route}:{after_offenders[:3]}")

    expected = remediate_article(before)
    if canonical(expected) != canonical(after):
        raise DefectRemediationError("defect_remediation_not_deletion_only")

    removed = len(canonical(before)) - len(canonical(after))
    if removed <= 0:
        raise DefectRemediationError("defect_remediation_removed_nothing")
    return {
        "status": "PASS_DEFECT_REMEDIATION_CONTRACT",
        "operation": OPERATION,
        "directive_id": directive_id,
        "defect_class": row["defect_class"],
        "canonical_route": route,
        "article_id": after["article_id"],
        "defect_fields_before": before_offenders,
        "defect_fields_after": [],
        "bytes_removed": removed,
        "deletion_only": True,
    }


def deterministic_qa(before: dict[str, Any], after: dict[str, Any], *, directive_id: str,
                     base_catalog: Path) -> dict[str, Any]:
    """The QA receipt that stands in for a five-gate review on this operation."""
    row = directive(directive_id)
    contract = validate_remediation(before, after, directive_id=directive_id)
    report_spec = None
    if row.get("defect_report"):
        report = Path(row["defect_report"])
        report_spec = {"path": str(report), "sha256": sha_file(report)}
    receipt = {
        "schema": QA_SCHEMA,
        "status": "PASS",
        "checked_at_utc": datetime.now(timezone.utc).replace(microsecond=0)
                          .isoformat().replace("+00:00", "Z"),
        "operation": OPERATION,
        "directive_id": directive_id,
        "operator_order_date": row["operator_order_date"],
        "operator_order": row.get("operator_order"),
        "defect_class": row["defect_class"],
        "defect_report": report_spec,
        "base_catalog": {"path": str(base_catalog), "sha256": sha_file(base_catalog)},
        "contract": contract,
        "checks": {
            "route_unchanged": True,
            "article_id_unchanged": True,
            "defect_present_before": True,
            "defect_absent_after": True,
            "deletion_only_equality": True,
            "route_within_directive_scope": True,
        },
        "effects": {"publishes": 0, "deployments": 0, "cache_purges": 0, "gsc_submissions": 0},
    }
    receipt["receipt_sha256"] = payload_hash(receipt, "receipt_sha256")
    return receipt
