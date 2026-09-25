#!/usr/bin/env python3
"""Resolve approved Katalys GLP rows that are missing provider-issued links.

The 07:15 affiliate reconciliation lane invokes this before B2. It derives the
queue from the latest complete backrun, uses the authenticated Katalys Create
Link flow, and emits frozen intents for the sole Sheet queue drain.
This producer never writes Sheets or invokes the drain. Offline capture mode
performs only local reads and report writes; queue acceptance is not readiness.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BUSINESS = Path("/home/ubuntu/business")
MACHINE = BUSINESS / "reports/glp-clinic-scout/affiliate-machine"
GENERATOR = BUSINESS / "scripts/katalys_generate_tracking_links.py"
OUT_BASE = MACHINE / "katalys-approved-link-resolution"
QUEUE_TABLE = "hJiP3eJKBvtZ6bQU"
QUEUE_KIND = "katalys_tracking_ready"
PT = ZoneInfo("America/Los_Angeles")
ALLOWED_STATUSES = {"approved_active_not_wired", "applied_approved_not_wired"}
PLAN_SCHEMA = "g6-affiliate-wire-in-sheet-plan-v1"


class ResolverError(RuntimeError):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResolverError(f"json_read_failed:{path}:{type(exc).__name__}") from exc


def write_private(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def truth(value: Any) -> bool:
    return value is True or str(value or "").strip().upper() == "TRUE"


def latest_complete_backrun(root: Path = MACHINE) -> Path:
    for directory in sorted(root.glob("backrun-*"), reverse=True):
        required = [
            directory / "coverage-receipt.json",
            directory / "sheet-full-row-export.json",
            directory / "marketplace/katalys-inventory.json",
        ]
        if not all(path.is_file() for path in required):
            continue
        coverage = read_json(required[0])
        if coverage.get("status") == "PASS" and (coverage.get("coverage_assertion") or {}).get(
            "every_nonblank_row_dispositioned"
        ):
            return directory
    raise ResolverError("complete_backrun_missing")


def approved_unlinked_rows(backrun: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sheet = read_json(backrun / "sheet-full-row-export.json")
    coverage = read_json(backrun / "coverage-receipt.json")
    inventory = read_json(backrun / "marketplace/katalys-inventory.json")
    headers = sheet.get("headers")
    rows = sheet.get("rows")
    if not isinstance(headers, list) or not isinstance(rows, dict):
        raise ResolverError("sheet_export_shape_invalid")
    index = {name: position for position, name in enumerate(headers)}
    required_headers = {
        "Program / Network", "Network / Route", "Apply URL", "Status",
        "Signup Complete?", "Tracking Link / Deep Link", "Wire-In Ready?", "Vertical",
    }
    if not required_headers.issubset(index):
        raise ResolverError("sheet_export_headers_missing")

    joins: dict[int, list[dict[str, Any]]] = {}
    for item in (coverage.get("join") or {}).get("program_dispositions") or []:
        if (
            str(item.get("network") or "").strip().casefold() == "katalys"
            and item.get("disposition") == "MATCHED_TO_SHEET"
            and item.get("sheet_row") is not None
        ):
            joins.setdefault(int(item["sheet_row"]), []).append(item)
    relationship = {
        str(item.get("program_id") or ""): str(item.get("relationship") or "").strip().casefold()
        for item in inventory.get("programs") or []
    }

    sys.path.insert(0, str(BUSINESS / "scripts"))
    from affiliate_operator_rulings import load_snapshot
    from affiliate_niche_geo_filter import match_bridge_ruling
    policy = load_snapshot()
    selected: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row_key, values in sorted(rows.items(), key=lambda item: int(item[0])):
        row_number = int(row_key)
        padded = list(values) + [""] * max(0, len(headers) - len(values))
        if str(padded[index["Network / Route"]]).strip().casefold() != "katalys":
            continue
        if str(padded[index["Status"]]).strip() not in ALLOWED_STATUSES:
            continue
        if not truth(padded[index["Signup Complete?"]]):
            continue
        if str(padded[index["Vertical"]]).strip().casefold() != "glp1-weightloss":
            continue
        if str(padded[index["Tracking Link / Deep Link"]] or "").strip():
            continue
        matches = joins.get(row_number, [])
        apply_url = str(padded[index["Apply URL"]] or "").strip()
        exact_url_id = re.search(r"/explore/(\d+)(?:[/?#]|$)", apply_url)
        if exact_url_id:
            matches = [
                item for item in matches
                if str(item.get("program_id") or "") == exact_url_id.group(1)
            ]
        identities = {(str(item.get("program_id") or ""), str(item.get("name") or "").strip()) for item in matches}
        if len(identities) != 1:
            blocked.append({
                "row": row_number,
                "name": str(padded[index["Program / Network"]]),
                "reason": "exact_katalys_program_identity_missing_or_ambiguous",
                "candidate_count": len(identities),
            })
            continue
        program_id, joined_name = next(iter(identities))
        names = [joined_name, str(padded[index["Program / Network"]])]
        reason = ""
        if not re.fullmatch(r"[1-9][0-9]*", program_id):
            reason = "literal_program_id_invalid"
        elif len([p for p in inventory.get("programs", []) if str(p.get("program_id")) == program_id]) != 1:
            reason = "inventory_program_identity_ambiguous"
        elif any("fella" in name.casefold() for name in names):
            reason = "permanently_excluded_fella"
        elif any((r := policy.evaluate_program(name)) is not None and r.verdict == "HOLD" for name in names):
            reason = "operator_ruling_hold"
        elif any((r := match_bridge_ruling(name, dict(policy.bridge_rulings))) is not None and r[1] == "NOT_FIT_PARKED" for name in names):
            reason = "bridge_ruling_hold"
        elif f"katalys:{program_id}" in policy.terminal_program_rulings:
            reason = "terminal_program_ruling"
        elif "Wired Live?" in index and truth(padded[index["Wired Live?"]]):
            reason = "already_wired_live"

        if reason:
            blocked.append({"row": row_number, "name": names[1], "program_id": program_id, "reason": reason})
            continue
        if relationship.get(program_id) != "approved":
            blocked.append({
                "row": row_number,
                "name": str(padded[index["Program / Network"]]),
                "program_id": program_id,
                "reason": f"current_katalys_relationship_not_approved:{relationship.get(program_id) or 'missing'}",
            })
            continue
        selected.append({
            "row": row_number,
            "offer_id": int(program_id),
            "program_id": program_id,
            "name": joined_name or str(padded[index["Program / Network"]]),
            "application_url": apply_url,
            "decision": "ALREADY_ACTIVE",
        })
    return selected, blocked


def build_wire_plan(
    targets: list[dict[str, Any]],
    tracking_payload: dict[str, Any],
    *,
    authority_sha256: str,
    captured_at_utc: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    links: dict[str, list[dict]] = {}
    for item in tracking_payload.get("programs") or []:
        if isinstance(item, dict):
            links.setdefault(str(item.get("offer_id")), []).append(item)
    rows: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for target in targets:
        matches = links.get(str(target["offer_id"]), [])
        result = matches[0] if len(matches) == 1 else {"status": "tracking_generation_result_missing_or_ambiguous"}
        link = str(result.get("tracking_link") or "").strip()
        if result.get("status") != "GENERATED_VERIFIED" or not link:
            blocked.append({
                "row": target["row"],
                "name": target["name"],
                "program_id": target["program_id"],
                "reason": str(result.get("status") or "tracking_generation_result_missing"),
                "detail": str(result.get("error") or "")[:300],
            })
            continue
        rows.append({
            "row": target["row"],
            "program": target["name"],
            "program_id": target["program_id"],
            "cells": {
                "10": "approved_active_tracking_ready",
                "14": link,
                "19": "TRUE",
                "21": "not_clicked_preserve_attribution",
                "24": captured_at_utc,
            },
        })
    return {
        "schema": PLAN_SCHEMA,
        "authority_sha256": authority_sha256,
        "tracking_clicks": 0,
        "rows": rows,
    }, blocked


def run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=BUSINESS,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def queue_update_id(program_id: str, link: str) -> str:
    if not isinstance(program_id, str) or not re.fullmatch(r"[0-9]+", program_id):
        raise ResolverError("literal_program_id_invalid")
    key = [QUEUE_KIND, "Katalys", program_id, hashlib.sha256(link.encode()).hexdigest()]
    return "katalys-tracking-" + hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()


def semantic_intent(envelope: dict[str, Any]) -> tuple:
    evidence = json.loads(envelope["evidence"])
    return (envelope["kind"], envelope["network"], envelope["program_id"],
            envelope["program_name"], evidence["network_program_id"], evidence["tracking_link"])


def validate_evidence(envelope: dict[str, Any]) -> None:
    evidence = json.loads(envelope["evidence"])
    for prefix in ("tracking_capture", "coverage", "inventory", "sheet_capture"):
        if sha_file(Path(evidence[prefix + "_path"])) != evidence[prefix + "_sha256"]:
            raise ResolverError(f"evidence_hash_mismatch:{prefix}")
    authority = hashlib.sha256(b"".join(Path(evidence[p + "_path"]).read_bytes()
                                      for p in ("coverage", "inventory", "tracking_capture"))).hexdigest()
    if authority != evidence["authority_sha256"]:
        raise ResolverError("authority_hash_mismatch")
    capture = read_json(Path(evidence["tracking_capture_path"]))
    matches = [p for p in capture.get("programs", [])
               if str(p.get("offer_id")) == envelope["program_id"]]
    if (len(matches) != 1 or matches[0].get("status") != "GENERATED_VERIFIED"
            or matches[0].get("tracking_link") != evidence["tracking_link"]
            or capture.get("captured_at_utc") != evidence["checked_at_utc"]
            or evidence["network_program_id"] != envelope["program_id"]
            or envelope["update_id"] != queue_update_id(envelope["program_id"], evidence["tracking_link"])):
        raise ResolverError("tracking_capture_correspondence_invalid")


def build_queue_envelopes(plan: dict, backrun: Path, tracking_path: Path, captured_at: str) -> list[dict]:
    evidence = {"checked_at_utc": captured_at, "authority_sha256": plan["authority_sha256"]}
    for prefix, path in (("coverage", backrun / "coverage-receipt.json"),
                         ("inventory", backrun / "marketplace/katalys-inventory.json"),
                         ("sheet_capture", backrun / "sheet-full-row-export.json"),
                         ("tracking_capture", tracking_path)):
        evidence[prefix + "_path"] = str(path.resolve())
        evidence[prefix + "_sha256"] = sha_file(path)
    envelopes = []
    for row in plan["rows"]:
        link = row["cells"]["14"]
        item = {"update_id": queue_update_id(row["program_id"], link),
                "kind": QUEUE_KIND, "network": "Katalys", "program_id": row["program_id"],
                "program_name": row["program"], "sheet_row": row["row"],
                "evidence": json.dumps({**evidence, "network_program_id": row["program_id"],
                                         "tracking_link": link}, sort_keys=True),
                "status": "queued", "queued_by": "katalys_approved_link_resolver",
                "queued_at_utc": captured_at, "resolved_at_utc": "", "note": "PENDING_DRAIN"}
        validate_evidence(item)
        envelopes.append(item)
    return envelopes


def enqueue_frozen(envelope: dict, outbox: Path) -> dict:
    """One insert attempt per durable ID; uncertain outcomes become read-only.

    The lock and freeze precede every transport call. A prepared record may be
    inserted after an exact absence read; attempting/unresolved/confirmed records
    can only reconcile, never insert again. Missing/corrupt members fail closed.
    """
    import fcntl
    import tempfile
    outbox.mkdir(parents=True, exist_ok=True)
    def durable(path, value):
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, sort_keys=True, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    with (outbox / "producer.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ResolverError("producer_lock_busy") from exc
        folder = outbox / envelope["update_id"]
        record_path = folder / "record.json"
        if folder.exists():
            record = read_json(record_path)
            frozen = record["envelope"]
            if semantic_intent(frozen) != semantic_intent(envelope):
                raise ResolverError("frozen_semantic_intent_conflict")
            if record.get("envelope_sha256") != hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest():
                raise ResolverError("frozen_envelope_corrupt")
            if record.get("state") not in {"prepared", "attempting", "unresolved", "confirmed"}:
                raise ResolverError("frozen_state_invalid")
        else:
            folder.mkdir()
            frozen = envelope
            record = {"envelope": frozen, "state": "prepared", "envelope_sha256":
                      hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()}
            durable(record_path, record)
        validate_evidence(frozen)
        sys.path.insert(0, str(BUSINESS / "ops"))
        import n8n_dt
        def lookup():
            found = n8n_dt.rows(QUEUE_TABLE, n8n_dt.eq("update_id", frozen["update_id"]))
            if not isinstance(found, list):
                raise ResolverError("queue_lookup_inconclusive")
            if len(found) > 1:
                raise ResolverError("duplicate_update_id")
            if found:
                remote = found[0]
                if remote.get("update_id") != frozen["update_id"] or semantic_intent(remote) != semantic_intent(frozen):
                    raise ResolverError("queue_semantic_intent_conflict")
                validate_evidence(remote)
                return remote
            return None
        try:
            remote = lookup()
            if remote is None and record["state"] == "prepared":
                record["state"] = "attempting"
                durable(record_path, record)
                try:
                    n8n_dt.insert(QUEUE_TABLE, frozen)
                except Exception:
                    # An exception is not absence. Always reconcile once by ID.
                    pass
                remote = lookup()
            if remote is None:
                raise ResolverError("queue_outcome_unresolved_no_blind_retry")
            record["state"] = "confirmed"
            record["remote_status"] = remote.get("status")
            durable(record_path, record)
            return {"update_id": frozen["update_id"], "status": "ENQUEUED",
                    "readiness": "PENDING_DRAIN", "queue_status": remote.get("status"),
                    "outbox": str(record_path)}
        except Exception:
            record["state"] = "unresolved"
            durable(record_path, record)
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backrun")
    parser.add_argument("--output-dir")
    parser.add_argument("--cdp", default="http://127.0.0.1:9324")
    effects = parser.add_mutually_exclusive_group()
    effects.add_argument("--apply-sheet", action="store_true", help="Deprecated queue-only alias for --apply-queue")
    effects.add_argument("--apply-queue", action="store_true")
    effects.add_argument("--offline-tracking-capture", type=Path, help="Local capture only; no generator or network")
    parser.add_argument("--queue-outbox-dir", type=Path)
    parser.add_argument("--program-id", action="append", default=[])
    args = parser.parse_args()
    apply_queue = args.apply_queue or args.apply_sheet

    pt_day = dt.datetime.now(dt.timezone.utc).astimezone(PT).date().isoformat()
    backrun = Path(args.backrun).resolve() if args.backrun else latest_complete_backrun()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (
        MACHINE / "katalys-approved-link-resolution" / pt_day
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    captured_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    source_paths = [backrun / "coverage-receipt.json", backrun / "marketplace/katalys-inventory.json",
                    backrun / "sheet-full-row-export.json"]
    source_hashes = {path: sha_file(path) for path in source_paths}
    coverage = read_json(source_paths[0])
    if coverage.get("status") != "PASS" or (coverage.get("coverage_assertion") or {}).get("every_nonblank_row_dispositioned") is not True:
        raise ResolverError("complete_backrun_required")
    targets, preblocked = approved_unlinked_rows(backrun)
    if args.program_id:
        wanted = {str(value).strip() for value in args.program_id}
        targets = [item for item in targets if item["program_id"] in wanted]
        preblocked = [item for item in preblocked if str(item.get("program_id") or "") in wanted]
    if not targets:
        payload = {
            "schema": "katalys-approved-link-resolution-v1",
            "status": "PASS" if not preblocked else "BLOCKED",
            "captured_at_utc": captured_at,
            "backrun": str(backrun),
            "approved_unlinked_detected": len(preblocked),
            "links_generated_verified": 0,
            "rows_became_wire_ready": 0,
            "blocked": preblocked,
            "loud_if_unresolved": bool(preblocked),
        }
        write_private(output_dir / "result.json", payload)
        print(json.dumps(payload, sort_keys=True))
        return 0 if not preblocked else 2

    classification_path = output_dir / "classification.json"
    applications_path = output_dir / "applications.json"
    tracking_path = args.offline_tracking_capture.resolve() if args.offline_tracking_capture else output_dir / "tracking-links.json"
    plan_path = output_dir / "sheet-plan.json"  # diagnostic projection only; never executed
    write_private(classification_path, {"schema": "katalys-approved-link-resolution-input-v1", "programs": targets})
    write_private(applications_path, {"schema": "katalys-approved-link-resolution-applications-v1", "programs": []})

    generated = subprocess.CompletedProcess([], 0, stdout="OFFLINE_CAPTURE" )
    if not args.offline_tracking_capture and not tracking_path.exists():
        # Preserve and revalidate an existing capture rather than issuing links again.
        generated = run([
            sys.executable, str(GENERATOR),
            "--classification", str(classification_path),
            "--applications", str(applications_path),
            "--output", str(tracking_path), "--cdp", args.cdp,
        ], timeout=420)
    tracking = read_json(tracking_path) if tracking_path.is_file() else {"programs": []}
    captured_at = str(tracking.get("captured_at_utc") or "")
    try:
        if not captured_at or dt.datetime.fromisoformat(captured_at.replace("Z", "+00:00")).utcoffset() != dt.timedelta(0):
            raise ValueError("not UTC")
    except ValueError as exc:
        raise ResolverError("tracking_capture_timestamp_invalid") from exc
    if any(sha_file(path) != digest for path, digest in source_hashes.items()):
        raise ResolverError("source_changed_during_generation")
    authority = hashlib.sha256(
        (backrun / "coverage-receipt.json").read_bytes()
        + (backrun / "marketplace/katalys-inventory.json").read_bytes()
        + (tracking_path.read_bytes() if tracking_path.is_file() else b"")
    ).hexdigest()
    plan, generation_blocked = build_wire_plan(
        targets, tracking, authority_sha256=authority, captured_at_utc=captured_at
    )
    write_private(plan_path, plan)

    envelopes = []
    queue_results = []
    outbox = args.queue_outbox_dir or ((output_dir if args.output_dir else OUT_BASE) / "queue-outbox")
    for row in plan["rows"]:
        try:
            envelope = build_queue_envelopes({**plan, "rows": [row]}, backrun, tracking_path, captured_at)[0]
            envelopes.append(envelope)
            if apply_queue:
                queue_results.append(enqueue_frozen(envelope, outbox))
        except Exception as exc:
            generation_blocked.append({"row": row["row"], "name": row["program"],
                                       "program_id": row["program_id"],
                                       "reason": f"queue_intent_unresolved:{type(exc).__name__}:{exc}"})
    write_private(output_dir / "queue-plan.json", {"schema": "katalys-tracking-ready-queue-v1", "rows": envelopes})
    blocked = [*preblocked, *generation_blocked]
    status = "BLOCKED" if blocked or generated.returncode != 0 else ("ENQUEUED" if apply_queue else "PENDING_DRAIN")
    payload = {
        "schema": "katalys-approved-link-resolution-v1",
        "status": status,
        "captured_at_utc": captured_at,
        "backrun": str(backrun),
        "approved_unlinked_detected": len(targets) + len(preblocked),
        "links_generated_verified": len(plan["rows"]),
        "rows_became_wire_ready": 0,
        "queue_results": queue_results,
        "queue_status": "PENDING_DRAIN",
        "rows_enqueued": len(queue_results),
        "offline_capture": bool(args.offline_tracking_capture),
        "generator_exit_code": generated.returncode,
        "generator_output_tail": generated.stdout[-2000:],
        "blocked": blocked,
        "loud_if_unresolved": bool(blocked),
        "tracking_clicks": 0,
        "artifacts": {
            "tracking_links": str(tracking_path),
            "sheet_plan": str(plan_path),
            "queue_plan": str(output_dir / "queue-plan.json"),
        },
    }
    write_private(output_dir / "result.json", payload)
    print(json.dumps(payload, sort_keys=True))
    return 2 if status == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
