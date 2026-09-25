#!/usr/bin/env python3
"""Fail closed when a reconciliation card assigns stale auth work to the operator.

The daily reconciliation lane is not an auth authority by itself. Fresh PASS auth
probes produced by sibling marketplace lanes take precedence over stale local
blocker text for the same network. The default invocation emits the preflight
facts injected into the cron agent. ``--validate-receipt`` additionally audits a
candidate receipt and optional card before delivery.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

MACHINE_DIR = Path("/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine")
BLOCKER_STORE = MACHINE_DIR / "operator-blockers.json"
MAX_PROBE_AGE = dt.timedelta(hours=24)
FUTURE_SKEW = dt.timedelta(minutes=5)
PASS_AUTH_STATE = "PASS"

_NETWORK_ALIASES = {
    "rakuten": "rakuten",
    "rakuten advertising": "rakuten",
    "linkshare": "rakuten",
    "cj": "cj",
    "cj affiliate": "cj",
    "commission junction": "cj",
    "awin": "awin",
    "partnerize": "partnerize",
    "impact": "impact",
    "impact radius": "impact",
    "clickbank": "clickbank",
    "digistore24": "digistore24",
    "partnerstack": "partnerstack",
    "healthtrader": "healthtrader",
    "uppromote": "uppromote",
    "goaffpro": "goaffpro",
    "katalys": "katalys",
}
_NETWORK_MARKERS = {
    "rakuten": ("rakuten", "linkshare"),
    "cj": ("cj affiliate", "commission junction", "cj"),
    "awin": ("awin",),
    "partnerize": ("partnerize",),
    "impact": ("impact radius", "impact"),
    "clickbank": ("clickbank",),
    "digistore24": ("digistore24",),
    "partnerstack": ("partnerstack",),
    "healthtrader": ("healthtrader",),
    "uppromote": ("uppromote",),
    "goaffpro": ("goaffpro",),
    "katalys": ("katalys",),
}
_NEGATED_OPERATOR = re.compile(
    r"\b(?:no|not|never|zero)\s+(?:stale\s+)?operator(?:[- ](?:action|ask|handoff))?\b|"
    r"\boperator(?:[- ](?:action|ask|handoff))?\s*[:=]\s*(?:none|false|0)\b|"
    r"\bnot\s+(?:an?\s+)?operator\s+(?:action|ask|handoff)\b",
    re.IGNORECASE,
)
_OPERATOR_OWED = re.compile(
    r"\boperator\b.*\b(?:required|must|needed|action|handoff|hand-off|hand\s+off|reply|provide|retrieve)\b|"
    r"\b(?:awaiting|requires?|required|needs?)\b.*\boperator\b|"
    r"\boperator_[a-z0-9_]*(?:required|needed)\b|"
    r"\b(?:sid|client\s+id|client\s+secret|token[- ]?key|credentials?)\b.*\b(?:required|handoff|hand-off|provide|retrieve)\b",
    re.IGNORECASE,
)
_AUTH_BLOCKED = re.compile(
    r"\b(?:oauth|auth(?:entication)?|token|credentials?|oauth_catalog)\b.*\b(?:blocked|missing|required|unavailable|handoff|hand-off)\b|"
    r"\b(?:blocked|missing|required|unavailable)\b.*\b(?:oauth|auth(?:entication)?|token|credentials?)\b|"
    r"BLOCKED_OPERATOR_HANDOFF",
    re.IGNORECASE,
)
_AUTH_SUBJECT = re.compile(
    r"\b(?:oauth|auth(?:entication)?|token|credentials?|sid|client\s+(?:id|secret)|"
    r"login|log\s+in|sign\s+in|session|portal\s+access|account\s+access)\b",
    re.IGNORECASE,
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_missing_timezone")
    return parsed.astimezone(dt.timezone.utc)


def iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_network(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return _NETWORK_ALIASES.get(text, text)


def _probe_timestamp(doc: dict[str, Any]) -> str:
    for key in ("captured_at_utc", "generated_at_utc", "checked_at_utc", "completed_at_utc"):
        value = doc.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def discover_fresh_pass_probes(
    *, probe_root: Path = MACHINE_DIR, now_utc: dt.datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Return the newest exact PASS inventory probe per network within 24 hours."""
    now = (now_utc or utc_now()).astimezone(dt.timezone.utc)
    newest: dict[str, dict[str, Any]] = {}
    for path in sorted(probe_root.glob("enumeration-*/*-inventory.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict) or str(doc.get("auth_state") or "").upper() != PASS_AUTH_STATE:
            continue
        network = normalize_network(doc.get("network") or path.name.removesuffix("-inventory.json"))
        stamp_text = _probe_timestamp(doc)
        if not network or not stamp_text:
            continue
        try:
            stamp = parse_utc(stamp_text)
        except (TypeError, ValueError):
            continue
        age = now - stamp
        if age < -FUTURE_SKEW or age > MAX_PROBE_AGE:
            continue
        row = {
            "network": network,
            "auth_state": PASS_AUTH_STATE,
            "captured_at_utc": iso_utc(stamp),
            "age_seconds": max(0, int(age.total_seconds())),
            "path": str(path),
            "sha256": sha256(path),
            "completeness": doc.get("completeness"),
            "completeness_detail": doc.get("completeness_detail"),
            "enumerated": doc.get("enumerated"),
            "reported_total": doc.get("reported_total"),
            "blockers_count": len(doc.get("blockers") or []),
        }
        prior = newest.get(network)
        if prior is None or row["captured_at_utc"] > prior["captured_at_utc"]:
            newest[network] = row
    return newest


def _network_in_text(network: str, text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _NETWORK_MARKERS.get(network, (network,)))


def _flatten_strings(value: Any, path: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _flatten_strings(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _flatten_strings(child, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def _operator_claim(text: str) -> bool:
    return not _NEGATED_OPERATOR.search(text) and bool(_OPERATOR_OWED.search(text))


def _auth_block_claim(text: str) -> bool:
    return not _NEGATED_OPERATOR.search(text) and bool(_AUTH_BLOCKED.search(text))


def _semantic_operator_rows(value: Any, path: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        network = normalize_network(value.get("network") or value.get("provider") or "")
        if (value.get("operator_action_required") is True and network
                and _AUTH_SUBJECT.search(json.dumps(value, sort_keys=True))):
            yield path, network
        for key, child in value.items():
            yield from _semantic_operator_rows(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _semantic_operator_rows(child, f"{path}[{index}]")


def validate_payload(
    receipt: dict[str, Any], card_text: str, fresh_probes: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    """Reject stale operator/auth claims contradicted by a fresh sibling PASS."""
    violations: list[dict[str, str]] = []
    for path, row_network in _semantic_operator_rows(receipt):
        if row_network in fresh_probes:
            violations.append({
                "code": "fresh_sibling_pass_forbids_operator_owed_blocker",
                "network": row_network,
                "location": path,
            })
    strings = list(_flatten_strings(receipt))
    strings.extend((f"$card[{index}]", line) for index, line in enumerate(card_text.splitlines(), 1))
    seen: set[tuple[str, str, str]] = set()
    for network in sorted(fresh_probes):
        for path, text in strings:
            context = f"{path} {text}"
            if not _network_in_text(network, context):
                continue
            code = ""
            if _operator_claim(text) and _AUTH_SUBJECT.search(text):
                code = "fresh_sibling_pass_forbids_operator_owed_blocker"
            elif _auth_block_claim(text):
                code = "fresh_sibling_pass_forbids_stale_auth_blocker"
            if code and (code, network, path) not in seen:
                seen.add((code, network, path))
                violations.append({
                    "code": code,
                    "network": network,
                    "location": path,
                    "excerpt": text[:240],
                })
    return violations


def blocker_conflicts(
    fresh_probes: dict[str, dict[str, Any]], blocker_store: Path = BLOCKER_STORE
) -> list[dict[str, str]]:
    try:
        store = json.loads(blocker_store.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [{"code": "blocker_store_unreadable", "location": str(blocker_store)}]
    conflicts: list[dict[str, str]] = []
    for blocker_id, entry in (store.get("blockers") or {}).items():
        if not isinstance(entry, dict) or entry.get("operator_action_required") is not True:
            continue
        text = json.dumps(entry, sort_keys=True)
        if not _AUTH_SUBJECT.search(text):
            continue
        for network in fresh_probes:
            if _network_in_text(network, text):
                conflicts.append({
                    "code": "fresh_sibling_pass_conflicts_with_operator_blocker_store",
                    "network": network,
                    "location": f"$.blockers.{blocker_id}",
                })
    return conflicts


def build_preflight(
    *, probe_root: Path = MACHINE_DIR, blocker_store: Path = BLOCKER_STORE,
    now_utc: dt.datetime | None = None,
) -> dict[str, Any]:
    now = (now_utc or utc_now()).astimezone(dt.timezone.utc)
    probes = discover_fresh_pass_probes(probe_root=probe_root, now_utc=now)
    conflicts = blocker_conflicts(probes, blocker_store)
    rakuten = probes.get("rakuten")
    return {
        "schema": "glp_affiliate_reconciliation_honesty_preflight_v1",
        "checked_at_utc": iso_utc(now),
        "rule": "no operator-owed blocker when any sibling lane has PASS auth for the same network within 24h",
        "fresh_pass_auth_probes": probes,
        "operator_ask_forbidden_networks": sorted(probes),
        "rakuten_auth_contract": {
            "status": "PASS_SIBLING_PROBE" if rakuten else "NO_FRESH_PASS_PROBE",
            "token_contract": "marketplace_adapters.rakuten mint_token: client_id + client_secret, bearer token, scope SID",
            "probe": rakuten,
            "legacy_partnership_report_is_catalog_auth_authority": False,
        },
        "blocker_store_conflicts": conflicts,
        "status": "FAIL" if conflicts else "PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe-root", type=Path, default=MACHINE_DIR)
    parser.add_argument("--blocker-store", type=Path, default=BLOCKER_STORE)
    parser.add_argument("--now", help="UTC ISO timestamp; tests only")
    parser.add_argument("--validate-receipt", type=Path)
    parser.add_argument("--card-file", type=Path)
    args = parser.parse_args()

    now = parse_utc(args.now) if args.now else utc_now()
    preflight = build_preflight(
        probe_root=args.probe_root, blocker_store=args.blocker_store, now_utc=now)
    if not args.validate_receipt:
        print(json.dumps(preflight, sort_keys=True, separators=(",", ":")))
        return 0 if preflight["status"] == "PASS" else 1

    try:
        receipt = json.loads(args.validate_receipt.read_text(encoding="utf-8"))
        card = args.card_file.read_text(encoding="utf-8") if args.card_file else ""
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "schema": "glp_affiliate_reconciliation_honesty_validation_v1",
            "status": "FAIL",
            "violations": [{"code": "candidate_unreadable", "detail": type(exc).__name__}],
        }, sort_keys=True, separators=(",", ":")))
        return 1
    violations = list(preflight["blocker_store_conflicts"])
    violations.extend(validate_payload(receipt, card, preflight["fresh_pass_auth_probes"]))
    result = {
        "schema": "glp_affiliate_reconciliation_honesty_validation_v1",
        "checked_at_utc": iso_utc(now),
        "status": "PASS" if not violations else "FAIL",
        "candidate_receipt": str(args.validate_receipt),
        "candidate_receipt_sha256": sha256(args.validate_receipt),
        "card_file": str(args.card_file) if args.card_file else None,
        "card_sha256": sha256(args.card_file) if args.card_file else None,
        "fresh_pass_auth_probes": preflight["fresh_pass_auth_probes"],
        "violations": violations,
        "stale_operator_asks": len([row for row in violations if "operator" in row["code"]]),
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
