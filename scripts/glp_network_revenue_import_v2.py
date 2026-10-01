#!/usr/bin/env python3
"""Normalize affiliate network exports into the append-only revenue ledger.

Blank/missing monetary cells become SQL NULL. The string/numeric value zero is
preserved only when the export actually measured zero. Re-importing the exact
same source rows is idempotent; status transitions remain distinct append-only
events because status and event type participate in the event identity.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import sqlite3
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

BUSINESS = Path("/home/ubuntu/business")
BRAIN = BUSINESS / "systems/analytics-brain"
sys.path.insert(0, str(BRAIN))
from analytics_brain import apply_fact_model, canonical, utc_now  # noqa: E402

MIGRATION = BRAIN / "migrations/001_analytics_brain_additive.sql"
DEFAULT_DB = BUSINESS / "reports/glp-clinic-scout/operator-national-wow-20260723/data/glp-observability.sqlite"
CANONICAL_FIELDS = {
    "revenue_event_id", "network_click_id", "cs_click_id", "conversion_id",
    "event_type", "event_status", "currency", "order_amount", "commission",
    "occurred_at", "order_ref", "network_account_ref",
}
EVENT_TYPES = {
    "accepted_click", "accepted_conversion", "commission", "commission_status", "payment", "reversal",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rows_from_file(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("rows")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("network_export_json_rows_invalid")
        return rows
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def mapping_from_file(path: Path | None) -> dict[str, str]:
    if path is None:
        return {field: field for field in CANONICAL_FIELDS}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "glp-network-revenue-field-map-v1":
        raise RuntimeError("network_field_map_schema_invalid")
    mapping = payload.get("fields")
    if not isinstance(mapping, dict):
        raise RuntimeError("network_field_map_fields_invalid")
    unknown = set(mapping) - CANONICAL_FIELDS
    if unknown:
        raise RuntimeError("network_field_map_unknown_fields:" + ",".join(sorted(unknown)))
    return {str(key): str(value) for key, value in mapping.items() if value}


def canonical_row(raw: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    return {canonical_name: raw.get(source_name) for canonical_name, source_name in mapping.items()}


def optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def money_minor(value: Any, field: str) -> int | None:
    text = optional_text(value)
    if text is None:
        return None
    normalized = text.replace(",", "").replace("$", "").replace("€", "").replace("£", "")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise RuntimeError(f"network_money_invalid:{field}:{text[:40]}") from exc
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))




INSERT_SQL = """INSERT OR IGNORE INTO analytics_revenue_ledger_v1
(ledger_row_id,revenue_event_id,network,network_account_ref,program_id,network_click_id,
 cs_click_id,conversion_id,order_ref_hash,event_type,event_status,currency,order_amount_minor,
 commission_minor,occurred_at,imported_at,source_file_sha256,raw_row_sha256,attributes_json)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
COLUMNS = (
    "ledger_row_id", "revenue_event_id", "network", "network_account_ref", "program_id",
    "network_click_id", "cs_click_id", "conversion_id", "order_ref_hash", "event_type",
    "event_status", "currency", "order_amount_minor", "commission_minor", "occurred_at",
    "imported_at", "source_file_sha256", "raw_row_sha256", "attributes_json",
)




def _remote_client():
    path = BUSINESS / "reports/glp-clinic-scout/operator-national-wow-20260723/scripts/glp_observability_collect.py"
    spec = importlib.util.spec_from_file_location("revenue_import_d1", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("d1_client_import_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    values = module.load_env(module.D1_SECRET)
    token = values.get("CLOUDFLARE_API_TOKEN") or values.get("CF_API_TOKEN")
    if not token:
        raise RuntimeError("d1_token_missing")
    return module.D1Client(token)







# F08 v2 cutover: legacy receiver imports above remain unchanged. The importer
# CLI writes a dedicated v2 reporting DB only; no operational migration occurs.
from glp_network_revenue_v2 import normalize as normalize_v2, Store
CANONICAL_FIELDS |= {"network_tenant_ref", "source_updated_at", "source_revision_id", "is_test"}


def normalize(raw, *, network, program_id, mapping, default_event_type,
              source_sha, imported_at, identity_registry=None, row_index=0):
    if identity_registry is None:
        raise ValueError("typed_identity_registry_required")
    r=canonical_row(raw,mapping)
    identity={"network":network,"program_id":program_id,
              "account_id":r.get("network_account_ref"),"tenant_id":r.get("network_tenant_ref")}
    event=r.get("revenue_event_id") or r.get("conversion_id")
    if not event:
        raise ValueError("stable_provider_event_identity_required")
    kind=r.get("event_type") or default_event_type
    if kind not in EVENT_TYPES:
        raise ValueError("event_type_invalid")
    value={"provider_event_id":event,"event_type":"settlement" if kind=="payment" else "commission",
        "source_status":r.get("event_status"),"mapped_status":"unknown",
        "occurred_at":r.get("occurred_at"),"source_updated_at":r.get("source_updated_at"),
        "source_revision_id":r.get("source_revision_id"),"currency":r.get("currency"),
        "commission":r.get("commission"),"net_paid":None,
        "classification":"TEST" if r.get("is_test") in (True,"true","1",1) else "UNKNOWN",
        "attribution":"UNKNOWN","authenticated":False,"receipt_sha256":source_sha,
        "receipt_pointer":"/rows/"+str(row_index)}
    return normalize_v2(value,identity,identity_registry)


def local_import(database, rows, watermark):
    with Store(database) as store:
        inserted=sum(store.append(row)=="inserted" for row in rows)
    return len(rows),inserted


def main(argv=None):
    parser=argparse.ArgumentParser(description="F08 corrected local v2 importer; no remote target")
    parser.add_argument("--source-file",type=Path,required=True)
    parser.add_argument("--identity-registry",type=Path,required=True)
    parser.add_argument("--field-map",type=Path)
    parser.add_argument("--network",required=True)
    parser.add_argument("--program-id",required=True)
    parser.add_argument("--database",type=Path,required=True)
    parser.add_argument("--default-event-type",choices=sorted(EVENT_TYPES),default="commission_status")
    args=parser.parse_args(argv)
    registry=json.loads(args.identity_registry.read_text())
    if registry.get("schema")!="reporting-identity-registry-v1":
        raise ValueError("identity_registry_schema_invalid")
    source=args.source_file.resolve(strict=True)
    if source.stat().st_size>8*1024*1024:
        raise ValueError("source_file_bound_exceeded")
    source_sha=sha256_bytes(source.read_bytes())
    rows=rows_from_file(source)
    if len(rows)>10000:
        raise ValueError("source_row_bound_exceeded")
    mapping=mapping_from_file(args.field_map)
    normalized=[normalize(row,network=args.network,program_id=args.program_id,mapping=mapping,
        default_event_type=args.default_event_type,source_sha=source_sha,imported_at=utc_now(),
        identity_registry=registry["identities"],row_index=i) for i,row in enumerate(rows)]
    total,inserted=local_import(args.database,normalized,source_sha)
    print(json.dumps({"schema":"glp-network-revenue-import-receipt-v2","status":"LOCAL_V2_IMPORT",
        "rows_normalized":total,"rows_inserted":inserted,"duplicates":total-inserted,
        "source_file_sha256":source_sha,"operational_ledger_written":False,
        "empty_input_is_not_measured_zero":True}))
    return 0


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except (ValueError,RuntimeError,OSError,KeyError) as exc:
        print(json.dumps({"status":"BLOCKED","error_class":type(exc).__name__}))
        raise SystemExit(2)
