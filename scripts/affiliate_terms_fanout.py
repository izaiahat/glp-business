#!/usr/bin/env python3
"""Replay sole-writer readbacks into candidate registry commercial metadata.

No Sheet/network client lives here. Enablement and mutation are separate switches.
The registry is an explicitly supplied writable SITE SOURCE checkout, never a
sealed release. A later reviewed build can be handed to the existing GLP43
publication carrier; its independent approval and release gates remain intact.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

KINDS = {
    "commission_terms_changed": "commission_terms_correction",
    "approval_confirmed": "approval_confirmed",
    "status_correction": "status_correction",
    "network_cancelled": "network_cancelled",
    "partner_email_hold": "partner_email_hold",
    "network_reactivated": "network_reactivated",
}
SCHEMA = "glp.affiliate_terms_fanout.v1"


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encoded(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def values_sha(values: list) -> str:
    return digest(json.dumps(values, separators=(",", ":"), ensure_ascii=False).encode())


def atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".fanout-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Persist the rename AND every containing directory entry. A fresh
        # state tree (or one left by a failed barrier) is not durable merely
        # because its leaf directory was fsynced. Any failure propagates.
        parent = path.parent.resolve()
        for ancestor in (parent, *parent.parents):
            directory = os.open(ancestor, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read(path: Path) -> Any:
    return json.loads(path.read_bytes())


def binding(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": digest(path.read_bytes())}


def bound(item: dict) -> tuple[Path, Any]:
    path = Path(item["path"])
    raw = path.read_bytes()
    if digest(raw) != item.get("sha256"):
        raise ValueError("fanout_source_hash_drift")
    return path, json.loads(raw)


def capture_readback(queue_row: dict, result: dict, plan_path: Path,
                     receipt_path: Path, values: list, event_path: Path) -> dict:
    """Called only after drain apply and its additional whole-Sheet readback.

    These are replayable writer receipts, not another executable queue. Retain
    even with downstream disabled so terminal Sheet rows can be caught up later.
    """
    receipt = read(receipt_path)
    if (receipt.get("status") != "PASS" or receipt.get("dry_run") is not False
            or receipt.get("readback_mismatches") != []
            or receipt.get("collateral_changes") != []
            or receipt.get("idempotent_rerun") is not True
            or receipt.get("post_write_values_sha256") != values_sha(values)):
        raise ValueError("fanout_writer_readback_not_trustworthy")
    event = {
        "schema": SCHEMA, "update_id": result["update_id"],
        "kind": queue_row["kind"], "network": queue_row["network"],
        "program_id": str(queue_row["program_id"]), "sheet_row": result["sheet_row"],
        "queue_source": queue_row,
        "plan": binding(plan_path), "writer_receipt": binding(receipt_path),
        "sheet_values": values,
    }
    validate_event(event)
    atomic(event_path, encoded(event))
    return binding(event_path)


def validate_event(event: dict) -> tuple[dict, list]:
    if event.get("schema") != SCHEMA or event.get("kind") not in KINDS:
        raise ValueError("fanout_event_schema_or_kind_invalid")
    _, plan = bound(event["plan"])
    _, receipt = bound(event["writer_receipt"])
    update = event["update_id"]
    mode = KINDS[event["kind"]]
    queue = event.get("queue_source", {})
    authority = digest(json.dumps(queue, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    if (not update or not event.get("network") or not event.get("program_id")
            or receipt.get("mode") != mode or receipt.get("status") != "PASS"
            or receipt.get("dry_run") is not False
            or receipt.get("readback_mismatches") != []
            or receipt.get("collateral_changes") != []
            or receipt.get("idempotent_rerun") is not True
            or receipt.get("plan_sha256") != event["plan"]["sha256"]
            or receipt.get("queue_update_ids") != [update]
            or plan.get("queue_update_id") != update
            or plan.get("authority_sha256") != authority
            or receipt.get("authority_sha256") != authority
            or receipt.get("post_write_values_sha256") != values_sha(event["sheet_values"])):
        raise ValueError("fanout_writer_binding_invalid")
    rows = plan.get("rows", [])
    row_number = event["sheet_row"]
    if len(rows) != 1 or rows[0].get("row") != row_number or receipt.get("rows") != [row_number]:
        raise ValueError("fanout_row_binding_invalid")
    row = event["sheet_values"][row_number - 1]
    row = row + [""] * max(0, 27 - len(row))
    evidence = rows[0].get("evidence", {})
    evidence_id = str(evidence.get("network_program_id") or evidence.get("program_id") or "")
    if (str(row[4]).casefold() != str(event["network"]).casefold()
            or any(str(queue.get(key)) != str(event[key]) for key in ("network", "program_id", "kind", "update_id"))
            or (evidence_id and evidence_id != event["program_id"])):
        raise ValueError("fanout_network_program_binding_invalid")
    for key, expected in rows[0]["cells"].items():
        index = int(key)
        actual = row[index]
        if index in {1, 11, 19, 20}:
            actual = str(actual).upper() == "TRUE"
            expected = str(expected).upper() == "TRUE"
        if actual != expected:
            raise ValueError("fanout_planned_cell_readback_mismatch")
    return receipt, row


def hold_reason(program: dict, row: list) -> str | None:
    names = " ".join(str(v) for v in (program.get("program_id"), program.get("display_name"), row[2])).casefold()
    if any(name in names for name in ("fella", "telos", "xentraffic", "healthrx", "health rx")):
        return "permanent_exclusion_or_provider_hold"
    return None


def project(registry: dict, identity_map: dict, event: dict) -> tuple[dict, str]:
    receipt, row = validate_event(event)
    matches = [m for m in identity_map.get("mappings", [])
               if m.get("network", "").casefold() == event["network"].casefold()
               and m.get("program_id") == event["program_id"]]
    if len(matches) != 1:
        raise ValueError("fanout_exact_identity_mapping_required")
    pid = matches[0]["registry_program_id"]
    programs = [p for p in registry.get("programs", []) if p.get("program_id") == pid]
    if len(programs) != 1:
        raise ValueError("fanout_registry_identity_missing_or_ambiguous")
    program = programs[0]
    reason = hold_reason(program, row)
    if reason:
        raise ValueError(reason)
    cancellation = None
    if event["kind"] == "network_cancelled":
        import affiliate_program_withdrawal as withdrawal
        source = withdrawal.evidence_of(event['queue_source'])
        cancellation = withdrawal.verify_writer_entry({
            'program': row[2], 'evidence': {**source, 'network': event['network'],
                                                'network_program_id': event['program_id']}},
            row, root=withdrawal.BUSINESS)
        if row[10] != "cancelled_by_network":
            raise ValueError("fanout_withdrawal_status_readback_required")
    # Terms alone never change prices, routes, selected rail or activation.
    # Adverse selected-relationship status may only retract commercial surfaces.
    result = copy.deepcopy(registry)
    target = next(p for p in result["programs"] if p["program_id"] == pid)
    key = event["network"].casefold() + ":" + event["program_id"]
    if cancellation:
        import affiliate_program_withdrawal as withdrawal
        source = withdrawal.evidence_of(event["queue_source"])
        prior = [h for h in target.get("affiliate_program_history", [])
                 if h.get("event_id") == source["event_id"] and h.get("relationship") == key]
        if prior:
            if len(prior) != 1 or prior[0].get("provider_verification") != source["provider_verification"]:
                raise ValueError("fanout_withdrawal_event_payload_conflict")
            return result, pid
    terms = {
        "network": event["network"], "network_program_id": event["program_id"],
        "commission_terms": row[16] if row[16] not in ("", None) else None,
        "cookie_window": row[17] if row[17] not in ("", None) else None,
        "relationship_status": row[10] if row[10] not in ("", None) else None,
        "reported_information": True, "sheet_checked_at": row[24] or None,
        "writer_captured_at": receipt["captured_at_utc"],
        "update_id": event["update_id"], "writer_receipt_sha256": event["writer_receipt"]["sha256"],
    }
    previous = target.get("affiliate_terms", {}).get(key, {})
    incoming_time = datetime.fromisoformat(terms["writer_captured_at"].replace("Z", "+00:00"))
    if incoming_time.tzinfo is None:
        raise ValueError("fanout_writer_timestamp_timezone_required")
    if previous.get("writer_captured_at"):
        previous_time = datetime.fromisoformat(previous["writer_captured_at"].replace("Z", "+00:00"))
        if previous_time > incoming_time:
            raise ValueError("fanout_stale_writer_event")
    target.setdefault("affiliate_terms", {})[key] = terms
    adverse = event["kind"] == "status_correction" or any(word in str(row[10]).casefold() for word in
                  ("hold", "held", "suppressed", "excluded", "cancelled", "paused", "rejected"))
    if adverse:
        # A cancelled non-selected rail must not deactivate a healthy sibling.
        # The explicit crosswalk records which relationship currently serves
        # this registry identity. Ambiguity refuses instead of guessing.
        if matches[0].get("selected_relationship") is not True:
            raise ValueError("fanout_adverse_status_selected_relationship_required")
        target["commercial_state"] = "held"
        target.setdefault("go", {})["entry_state"] = "held"
        for profile in target.get("profiles", {}).values():
            profile["wired"] = False
            surfaces = ("compare", "quiz", "calculator", "go", "review", "sitemap") if cancellation else ("compare", "quiz", "calculator", "go")
            for surface in surfaces:
                profile.setdefault("surfaces", {})[surface] = False
                profile.setdefault("exceptions", {}).setdefault(surface, {
                    "enabled": True,
                    "reason": "Reported affiliate relationship is held or inactive; no activation authority.",
                    "surface_restriction_basis": "sole_writer_relationship_status_readback",
                    "authority_path": event["writer_receipt"]["path"],
                    "authority_sha256": event["writer_receipt"]["sha256"],
                })
    if cancellation:
        # Native schema permits live/held only. Keep that safety gate and record
        # explicit withdrawn lifecycle separately, not as an invented pause.
        target["affiliate_program_state"] = "withdrawn"
        target.setdefault("affiliate_program_history", []).append({
            "state": "withdrawn", "decision_class": source["decision_class"],
            "provider_relationship_status": cancellation["relationship_status"],
            "event_id": source["event_id"], "relationship": key,
            "update_id": event["update_id"], "raw_sha256": source["raw_sha256"],
            "notice_path": source["path"], "provider_verification": source["provider_verification"],
            "observed_at_utc": cancellation["observed_at_utc"],
            "writer_receipt": event["writer_receipt"], "sheet_row": event["sheet_row"],
        })
    return result, pid


def dispatch(request: dict, handoff_path: Path, runner=subprocess.run) -> dict:
    """Invoke only the existing independently approved publication carrier."""
    handoff = read(handoff_path)
    if handoff.get("registry_sha256") != request["registry_after_sha256"]:
        raise ValueError("fanout_release_registry_binding_invalid")
    prepared = Path(handoff["prepared_dir"]).resolve()
    if digest((prepared / "release/data/program-registry.json").read_bytes()) != request["registry_after_sha256"]:
        raise ValueError("fanout_prepared_registry_drift")
    if digest((prepared / "PREPARED.json").read_bytes()) != handoff["expected_prepared_manifest_sha256"]:
        raise ValueError("fanout_prepared_manifest_drift")
    approval = Path(handoff["approval"]).resolve()
    if digest(approval.read_bytes()) != handoff["expected_approval_sha256"]:
        raise ValueError("fanout_release_approval_drift")
    carrier = Path(__file__).resolve().with_name("glp_deploy_glp34_authority_v1.py")
    argv = [sys.executable, str(carrier), "--publish-current", "--prepared-dir", str(prepared),
            "--expected-prepared-manifest-sha256", handoff["expected_prepared_manifest_sha256"],
            "--approval", str(approval), "--expected-approval-sha256", handoff["expected_approval_sha256"],
            "--authorize-production"]
    outcome = runner(argv, capture_output=True, timeout=1800, check=False)
    # Do not echo carrier output; retain a digest. Its own durable release
    # receipts and locks own effects/reconciliation, not this consumer.
    result = {"returncode": outcome.returncode,
              "stdout_sha256": digest(outcome.stdout), "stderr_sha256": digest(outcome.stderr)}
    if outcome.returncode == 0:
        terminal = json.loads(outcome.stdout)
        if terminal.get("status") != "PASS_PRODUCTION_TERMINAL":
            raise ValueError("fanout_carrier_terminal_success_missing")
        _, receipt = bound({"path": terminal["receipt"], "sha256": terminal["receipt_sha256"]})
        if receipt.get("status") != "PASS_PRODUCTION_TERMINAL":
            raise ValueError("fanout_carrier_terminal_receipt_invalid")
        result["terminal_receipt"] = {key: terminal[key] for key in
                                      ("receipt", "receipt_sha256", "deployment_id", "site_head")}
    return result


def consume(event_path: Path, registry_path: Path, identity_map_path: Path,
            state_dir: Path, *, enabled=False, apply=False, dispatch_site=False,
            release_handoff: Path | None = None, dispatcher=dispatch) -> dict:
    if not enabled:
        return {"status": "DISABLED", "registry_written": False, "site_dispatched": False}
    event = read(event_path)
    event_hash = digest(encoded(event))
    if event.get("kind") == "network_cancelled":
        import affiliate_program_withdrawal as withdrawal
        proof = withdrawal.evidence_of(event["queue_source"])
        # A new zero-write Sheet receipt is not a new cancellation. Full event
        # validation still runs; semantic custody owns replay identity.
        event_hash = digest(encoded({k: proof.get(k) for k in
            ("event_id", "raw_sha256", "decision_class", "provider_verification")}))
    registry_raw = registry_path.read_bytes()
    successor, pid = project(json.loads(registry_raw), read(identity_map_path), event)
    successor_raw = encoded(successor)
    request = {"schema": SCHEMA, "event_sha256": event_hash,
               "update_id": event["update_id"], "registry_program_id": pid,
               "registry_path": str(registry_path.resolve()),
               "link_removal_candidate_required": event['kind'] == 'network_cancelled',
               "retired_tracking_link": event['sheet_values'][event['sheet_row'] - 1][14] if event['kind'] == 'network_cancelled' else None,
               "registry_before_sha256": digest(registry_raw),
               "registry_after_sha256": digest(successor_raw),
               "status": "PENDING_REVIEWED_SITE_RELEASE", "site_dispatched": False}
    if not apply:
        return {**request, "status": "DRY_RUN", "registry_written": False}
    if (registry_path.is_symlink() or registry_path.stat().st_nlink != 1
            or registry_path.stat().st_mode & 0o222 == 0):
        raise ValueError("fanout_registry_must_be_detached_writable_source")
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "consumer.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        operation = digest((str(registry_path.resolve()) + "\0" + event["update_id"]).encode())
        journal_path = state_dir / (operation + ".json")
        request['candidate_request'] = str(journal_path.resolve())
        if journal_path.exists():
            request = read(journal_path)
            if request["event_sha256"] != event_hash:
                raise ValueError("fanout_update_id_payload_conflict")
            if request.get("site_dispatched"):
                return {**request, "status": "ALREADY_APPLIED", "registry_written": False}
            if request.get("dispatch_intent"):
                raise ValueError("fanout_dispatch_uncertain_reconcile_carrier_receipt_before_retry")
            if (event["kind"] == "network_cancelled" and not dispatch_site
                    and digest(registry_path.read_bytes()) == request["registry_after_sha256"]):
                return {**request, "status": "ALREADY_APPLIED", "registry_written": False}
        # Re-establish the journal barrier even on a retry after an earlier
        # fsync failure; existence alone does not acknowledge durability.
        atomic(journal_path, encoded(request))  # intent before candidate mutation
        current = digest(registry_path.read_bytes())
        if current == request["registry_before_sha256"]:
            if digest(successor_raw) != request["registry_after_sha256"]:
                raise ValueError("fanout_candidate_drift")
            atomic(registry_path, successor_raw)
        elif current != request["registry_after_sha256"]:
            raise ValueError("fanout_registry_cas_drift")
        request["registry_written"] = True
        if dispatch_site:
            if release_handoff is None:
                raise ValueError("fanout_reviewed_release_handoff_required")
            request["dispatch_intent"] = True
            atomic(journal_path, encoded(request))
            outcome = dispatcher(request, release_handoff)
            request["dispatch_result"] = outcome
            request["site_dispatched"] = outcome["returncode"] == 0
            request["status"] = "DISPATCHED" if request["site_dispatched"] else "CARRIER_REFUSED_OR_FAILED"
        atomic(journal_path, encoded(request))
        return request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--identity-map", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dispatch-site", action="store_true")
    parser.add_argument("--release-handoff", type=Path)
    parser.add_argument("--build-candidate", action="store_true", help="Build detached withdrawal source only; no release")
    args = parser.parse_args()
    try:
        if args.build_candidate and (not args.enable or not args.apply or args.dispatch_site):
            raise ValueError('fanout_candidate_build_requires_enable_apply_and_no_dispatch')
        result = consume(args.event, args.registry, args.identity_map, args.state_dir,
                         enabled=args.enable, apply=args.apply, dispatch_site=args.dispatch_site,
                         release_handoff=args.release_handoff)
        if args.build_candidate:
            import affiliate_withdrawal_candidate as candidate
            result['link_removal_build'] = candidate.build(
                Path(result['candidate_request']), args.registry.resolve().parent.parent)
    except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 1 if result.get('link_removal_build', {}).get('status') == 'BLOCKED_NATIVE_BUILD' else 0


if __name__ == "__main__":
    raise SystemExit(main())
