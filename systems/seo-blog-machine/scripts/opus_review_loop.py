#!/usr/bin/env python3
"""Packet-bound Astra review with bounded source repair and autonomous retry.

The standing article policy permits no per-refusal human approval. Each batch
retains the five-gate rubric and yields after five rounds so passing alternatives
can reach the host publisher. Retry means fresh repair/reseal/re-review, not PASS.
Historical extension validators remain only for old receipt readers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from selection_receipt import (  # noqa: E402
    load_selection_receipt,
    selection_binding,
    validate_authority_selection_binding,
)
import review_transports  # noqa: E402

BUSINESS = Path("/home/ubuntu/business")
REVIEW_WRAPPER = BUSINESS / "scripts/hermes_sol_article_review.py"
RUBRIC = BUSINESS / "reports/glp-clinic-scout/seo-machine/OPUS-REVIEW-PATH-20260802.md"
REVIEW_SCHEMA = BUSINESS / "systems/seo-blog-machine/schemas/glp-opus-seo-review-v1.schema.json"
PROVIDER = "openai-codex"
MODEL = "gpt-5.6-sol"
EFFORT = "xhigh"
API_MODE = "codex_responses"
MAX_ROUNDS = 5
EXTENSION_MAX_ROUNDS = 3
GATES = ("A", "B", "C", "D", "E")
ADVISORY_HEADER = "INSTRUMENT ADVISORY — NOT BRIDGE"


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def payload_hash(value: dict[str, Any], field: str) -> str:
    unsigned = dict(value)
    unsigned.pop(field, None)
    return sha_bytes(canonical(unsigned))


def atomic_json(path: Path, value: Any, *, exclusive: bool = True) -> None:
    if exclusive and path.exists():
        raise RuntimeError(f"receipt_exists:{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def persist_stream(path: Path, value: str) -> dict[str, Any]:
    """Persist a complete child stream beside its execution receipt."""
    raw = value.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return {"path": str(path), "bytes": len(raw), "sha256": sha_bytes(raw)}


def stderr_failure_cause(value: str) -> str:
    """Return the innermost exception line without replacing full diagnostics."""
    candidates = []
    for raw in value.splitlines():
        line = " ".join(raw.strip().split())
        prefix = line.partition(":")[0]
        if line and (prefix == "Error" or prefix.endswith("Error")):
            candidates.append(line)
    if not candidates:
        return "child_stderr_unclassified"
    cause = candidates[-1]
    if "EACCES:" in cause and ", open " in cause:
        cause = cause.split(", open ", 1)[0]
    return cause[:240]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def require_file_spec(spec: dict[str, Any], label: str) -> tuple[Path, Any]:
    path = Path(str((spec or {}).get("path") or "")).resolve()
    expected = str((spec or {}).get("sha256") or "")
    if not path.is_file() or len(expected) != 64 or sha_file(path) != expected:
        raise RuntimeError(f"{label}_hash_invalid")
    return path, read_json(path)


def validate_bridge_extension(job: dict[str, Any]) -> dict[str, Any] | None:
    extension = job.get("bridge_extension")
    if extension is None:
        normal_rounds = job.get("max_rounds")
        if type(normal_rounds) is not int or not 1 <= normal_rounds <= MAX_ROUNDS:
            raise RuntimeError("review_job_contract_invalid")
        return None
    if (not isinstance(extension, dict)
            or extension.get("schema") != "glp-opus-bridge-extension-v1"
            or int(job.get("max_rounds") or 0) != EXTENSION_MAX_ROUNDS
            or extension.get("topic_id") != job.get("topic_id")):
        raise RuntimeError("bridge_extension_contract_invalid")
    ruling_path, ruling = require_file_spec(extension.get("ruling") or {}, "bridge_extension_ruling")
    escalation_path, escalation = require_file_spec(
        extension.get("predecessor_escalation") or {}, "bridge_extension_predecessor_escalation"
    )
    round_path, predecessor_round = require_file_spec(
        extension.get("predecessor_round_receipt") or {}, "bridge_extension_predecessor_round"
    )
    repair_path, repair = require_file_spec(extension.get("repair_receipt") or {}, "bridge_extension_repair")
    topic_id = job["topic_id"]
    if (ruling.get("schema") != "glp-opus-bridge-bounded-ruling-v1"
            or ruling.get("status") != "AUTHORIZED_BOUNDED_EXTENSION"
            or ruling.get("topic_id") != topic_id
            or ruling.get("max_rounds") != EXTENSION_MAX_ROUNDS):
        raise RuntimeError("bridge_extension_ruling_invalid")
    if (escalation.get("status") != "BLOCKED_OPUS_NONCONVERGENCE"
            or escalation.get("topic_id") != topic_id
            or escalation.get("publication_invoked") is not False
            or len(escalation.get("rounds") or []) != MAX_ROUNDS):
        raise RuntimeError("bridge_extension_predecessor_invalid")
    allowed = ruling.get("authorized_findings") or []
    original = predecessor_round.get("findings") or []
    allowed_ids = [row.get("id") for row in allowed]
    if (predecessor_round.get("status") != "BLOCKED_OPUS_SEO_DRAFT"
            or predecessor_round.get("topic_id") != topic_id
            or predecessor_round.get("round") != MAX_ROUNDS
            or allowed_ids != ["A-01", "A-02", "A-03"]
            or [row.get("id") for row in original] != allowed_ids):
        raise RuntimeError("bridge_extension_finding_set_invalid")
    allowed_changed = extension.get("allowed_changed_files")
    if (allowed_changed != ["04-draft.md", "05-claim-source-map.json"]
            or repair.get("schema") != "glp-opus-bounded-repair-receipt-v1"
            or repair.get("status") != "PASS_EXACT_THREE_FINDING_REPAIR"
            or repair.get("topic_id") != topic_id
            or repair.get("changed_source_files") != allowed_changed):
        raise RuntimeError("bridge_extension_repair_invalid")
    hold_path, hold = require_file_spec(extension.get("hold_ledger") or {}, "bridge_extension_hold_ledger")
    if hold.get("schema") != "glp-opus-held-topic-ledger-v1":
        raise RuntimeError("bridge_extension_hold_ledger_invalid")
    return {
        "ruling_path": str(ruling_path), "ruling_sha256": sha_file(ruling_path),
        "escalation_path": str(escalation_path), "escalation_sha256": sha_file(escalation_path),
        "round_path": str(round_path), "round_sha256": sha_file(round_path),
        "repair_path": str(repair_path), "repair_sha256": sha_file(repair_path),
        "hold_path": str(hold_path), "hold_sha256": sha_file(hold_path),
        "allowed_findings": allowed, "allowed_changed_files": allowed_changed,
    }


def validate_executable(spec: dict[str, Any], label: str) -> list[str]:
    argv = spec.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise RuntimeError(f"{label}_argv_invalid")
    executable = Path(str(spec.get("executable") or "")).resolve()
    expected = str(spec.get("executable_sha256") or "")
    if not executable.is_file() or len(expected) != 64 or sha_file(executable) != expected:
        raise RuntimeError(f"{label}_executable_hash_invalid")
    if Path(argv[0]).resolve() != executable:
        raise RuntimeError(f"{label}_argv_executable_mismatch")
    controls = spec.get("control_files") or {}
    if not isinstance(controls, dict):
        raise RuntimeError(f"{label}_control_files_invalid")
    for raw_path, expected_sha in controls.items():
        path = Path(str(raw_path)).resolve()
        if not path.is_file() or len(str(expected_sha)) != 64 or sha_file(path) != expected_sha:
            raise RuntimeError(f"{label}_control_file_hash_invalid:{path}")
    return argv


def validate_topic_authority(job: dict[str, Any]) -> dict[str, Any]:
    selection_spec = job.get("selection_receipt") or {}
    selection_path = Path(str(selection_spec.get("path") or "")).resolve()
    selection_sha = str(selection_spec.get("sha256") or "")
    if not selection_path.is_file() or len(selection_sha) != 64 or sha_file(selection_path) != selection_sha:
        raise RuntimeError("selection_receipt_hash_invalid")
    selection_payload, selected = load_selection_receipt(selection_path, expected_sha256=selection_sha)
    if selection_spec.get("receipt_sha256") != selection_payload["receipt_sha256"]:
        raise RuntimeError("selection_receipt_payload_hash_invalid")
    if job.get("selection_binding") != selection_binding(selection_payload):
        raise RuntimeError("review_job_selection_binding_mismatch")
    if selected["topic_id"] != job.get("topic_id"):
        raise RuntimeError("selection_topic_id_mismatch")
    if selected["target_query"] != job.get("target_query"):
        raise RuntimeError("selection_query_mismatch")
    if selected["target_route"] != job.get("target_route"):
        raise RuntimeError("selection_route_mismatch")
    authority = job.get("topic_authority") or {}
    path = Path(str(authority.get("path") or "")).resolve()
    expected = str(authority.get("sha256") or "")
    topic_id = str(job.get("topic_id") or "")
    if not path.is_file() or len(expected) != 64 or sha_file(path) != expected:
        raise RuntimeError("topic_authority_hash_invalid")
    payload = read_json(path)
    if payload.get("schema") == "glp-seo-r2-update-authority-v1":
        valid = (
            job.get("operation") == "r2_refresh"
            and payload.get("status") == "AUTHORIZED_R2_REFRESH"
            and payload.get("topic_id") == topic_id
            and payload.get("article_id") == selected.get("article_id")
            and payload.get("target_query") == selected["target_query"]
            and payload.get("target_route") == selected["target_route"]
            and payload.get("refresh_tier") == "R2"
        )
        authority_type = "gsc_r2_update_authority"
    elif payload.get("schema") == "seo-machine-topic-go-v1":
        valid = (
            payload.get("decision") == "GO"
            and payload.get("topic_id") == topic_id
            and bool(payload.get("operator"))
            and bool(payload.get("authority_reference"))
        )
        authority_type = "explicit_topic_go"
    else:
        topic = next((row for row in payload.get("queue") or [] if row.get("topic_id") == topic_id), None)
        directive = (topic or {}).get("competitor_gap") or {}
        valid = bool(
            topic
            and topic.get("evidence_state") == "READY_FOR_RESEARCH"
            and directive.get("operator_ready_for_research") is True
            and directive.get("operator_directive_id")
            and topic.get("target_query") == selected["target_query"]
            and directive.get("target_route") == selected["target_route"]
        )
        authority_type = "operator_directed_queue_item"
    if not valid:
        raise RuntimeError("topic_go_not_proven")
    packet_authority_file = job.get("packet_authority_file")
    if packet_authority_file:
        packet = Path(str(job.get("packet_dir") or "")).resolve()
        authority_path = (packet / str(packet_authority_file)).resolve()
        if authority_path.parent != packet or not authority_path.is_file():
            raise RuntimeError("packet_authority_invalid")
        packet_authority = read_json(authority_path)
        validate_authority_selection_binding(packet_authority, selection_path)
        ids = [packet_authority.get("topic_id"), packet_authority.get("requested_queue_item_id"),
               packet_authority.get("observed_queue_topic_id"),
               (packet_authority.get("queue_authority") or {}).get("topic_id")]
        bound_ids = [str(value) for value in ids if value]
        if not bound_ids or any(value != topic_id for value in bound_ids):
            raise RuntimeError("packet_topic_id_mismatch")
    return {"type": authority_type, "path": str(path), "sha256": expected, "topic_id": topic_id,
            "selection_receipt": dict(selection_spec), "selection_binding": selection_binding(selection_payload)}


def subject_paths(job: dict[str, Any]) -> list[Path]:
    packet = Path(str(job.get("packet_dir") or "")).resolve()
    if not packet.is_dir():
        raise RuntimeError("packet_dir_missing")
    raw = job.get("subject_files")
    if not isinstance(raw, list) or len(raw) < 5 or len(set(raw)) != len(raw):
        raise RuntimeError("subject_file_contract_invalid")
    paths: list[Path] = []
    for rel in raw:
        if not isinstance(rel, str) or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise RuntimeError("subject_path_invalid")
        path = (packet / rel).resolve()
        if packet not in path.parents or not path.is_file() or path.is_symlink():
            raise RuntimeError(f"subject_missing_or_unsafe:{rel}")
        paths.append(path)
    return paths


def subject_snapshot(packet: Path, paths: list[Path]) -> dict[str, str]:
    return {path.relative_to(packet).as_posix(): sha_file(path) for path in paths}


def seal_subjects(job: dict[str, Any], round_number: int, history: Path) -> tuple[dict[str, Any], Path]:
    packet = Path(job["packet_dir"]).resolve()
    rows = []
    for path in subject_paths(job):
        data = path.read_bytes()
        rows.append({
            "path": path.relative_to(packet).as_posix(),
            "bytes": len(data),
            "lines": len(data.decode("utf-8", errors="replace").splitlines()),
            "sha256": sha_bytes(data),
        })
    manifest = {
        "schema": "glp-opus-seo-subject-manifest-v1",
        "status": "SEALED_FOR_OPUS_REVIEW",
        "sealed_at_utc": now(),
        "job_id": job["job_id"],
        "topic_id": job["topic_id"],
        "round": round_number,
        "subject_files": rows,
    }
    manifest["manifest_sha256"] = payload_hash(manifest, "manifest_sha256")
    path = history / f"round-{round_number:02d}-subject-manifest.json"
    atomic_json(path, manifest)
    return manifest, path


def build_context_bundle(job: dict[str, Any], manifest: dict[str, Any], round_number: int, history: Path) -> Path:
    packet = Path(job["packet_dir"]).resolve()
    chunks = [
        "# SEALED PACKET CONTENT — ALREADY IN BAND",
        "",
        f"Subject manifest SHA-256: {manifest['manifest_sha256']}",
        "Every source line is prefixed with its original 1-based line number.",
    ]
    for path in subject_paths(job):
        rel = path.relative_to(packet).as_posix()
        chunks.extend(["", f"## FILE: {rel}", ""])
        chunks.extend(f"{number}|{line}" for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1))
    path = history / f"round-{round_number:02d}-context-bundle.md"
    path.write_text("\n".join(chunks) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def build_prompt(job: dict[str, Any], manifest: dict[str, Any], manifest_path: Path, round_number: int, history: Path) -> Path:
    rubric = RUBRIC.read_text(encoding="utf-8")
    extension_note = ""
    if job.get("bridge_extension"):
        ruling = read_json(Path(job["bridge_extension"]["ruling"]["path"]))
        extension_note = """
## Bridge-bounded extension

This is a three-round extension after normal nonconvergence. Review all five gates normally. Repair
authority is limited to the exact findings below. Reuse the listed finding ID if one remains. Any
different defect still blocks publication but is outside repair scope and terminalizes this article
to HOLD rather than authorizing broader edits.

""" + "\n".join(
            f"- {row['id']}: {row['name']} — {row['required_fix']}" for row in ruling["authorized_findings"]
        ) + "\n"
    schema = {
        "verdict": "PASS_OPUS_SEO_DRAFT or BLOCKED_OPUS_SEO_DRAFT",
        "round": round_number,
        "subject_manifest_sha256": manifest["manifest_sha256"],
        "gates": [{"gate": gate, "status": "PASS or FAIL", "findings": []} for gate in GATES],
        "findings": [{
            "id": "stable-id", "gate": "A-E", "severity": "BLOCKING",
            "file": "primary packet-relative path", "related_files": [], "line_start": 1, "line_end": 1,
            "issue": "specific defect", "required_fix": "exact bounded repair",
        }],
    }
    text = f"""# Packet-bound GLP SEO review — round {round_number}

All packet content is ALREADY PRESENT IN-BAND in the supplied context bundle, with original line numbers.
Do NOT call, request, describe, imitate, or emit Bash/Read/Search/Glob/web tools. You have no tools.
Review only the in-band bundle and do not infer live facts beyond it.
The sealed subject manifest has SHA-256 `{manifest['manifest_sha256']}`.
Apply every gate A-E below. PASS requires all five PASS and zero findings. Every BLOCKED finding
must cite one primary supplied packet-relative source file, every additional source file in related_files,
and an exact line range for the primary file. Do not offer optional advice. Files 06-article.json and
07-qa-receipt.json are deterministic derived views: inspect them, but cite only authoritative source files
(00-05) in file or related_files.
Your entire response MUST be ONLY one JSON object, with no preamble, markdown fences, commentary,
or tool-call syntax, matching this shape:

{json.dumps(schema, indent=2)}

## Governing rubric

{rubric}
{extension_note}
"""
    path = history / f"round-{round_number:02d}-review-prompt.md"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def parse_result(raw_receipt: dict[str, Any]) -> dict[str, Any]:
    result = raw_receipt.get("result")
    if not isinstance(result, str):
        raise RuntimeError("opus_result_missing")
    text = result.strip()
    if text.startswith(ADVISORY_HEADER):
        text = text[len(ADVISORY_HEADER):].strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"opus_result_not_strict_json:{exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("opus_result_shape_invalid")
    return payload


def validate_review(payload: dict[str, Any], raw: dict[str, Any], manifest: dict[str, Any], round_number: int) -> None:
    # Dual-authorized transport (operator authorization 2026-08-18): the raw
    # receipt must prove EITHER the Hermes/Sol path OR the independent Claude
    # Opus path ran clean. Both are held to one identical mechanical bar, kept
    # in review_transports so the accepted set cannot drift from the emitted
    # set. Nothing about the gates, the rubric, or the PASS bar changes.
    review_transports.validate_raw_review_runtime(raw, error=RuntimeError, prefix="opus")
    review_input = raw.get("review_input") or {}
    schema_input = review_input.get("json_schema_file") or {}
    if schema_input.get("path") != str(REVIEW_SCHEMA) or schema_input.get("sha256") != sha_file(REVIEW_SCHEMA):
        raise RuntimeError("opus_json_schema_unproven")
    if payload.get("round") != round_number or payload.get("subject_manifest_sha256") != manifest["manifest_sha256"]:
        raise RuntimeError("opus_subject_binding_invalid")
    gates = payload.get("gates")
    if not isinstance(gates, list) or [row.get("gate") for row in gates if isinstance(row, dict)] != list(GATES):
        raise RuntimeError("opus_gate_table_invalid")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise RuntimeError("opus_findings_invalid")
    rows = {row["path"]: row for row in manifest["subject_files"]}
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict) or finding.get("gate") not in GATES or finding.get("severity") != "BLOCKING":
            raise RuntimeError(f"opus_finding_shape_invalid:{index}")
        row = rows.get(str(finding.get("file") or ""))
        related = finding.get("related_files")
        start, end = finding.get("line_start"), finding.get("line_end")
        if (not row or not isinstance(related, list) or any(str(item) not in rows for item in related)
                or finding.get("file") in related or not isinstance(start, int) or not isinstance(end, int)
                or start < 1 or end < start or end > max(1, row["lines"])):
            raise RuntimeError(f"opus_finding_location_invalid:{index}")
        if not str(finding.get("issue") or "").strip() or not str(finding.get("required_fix") or "").strip():
            raise RuntimeError(f"opus_finding_content_invalid:{index}")
    verdict = payload.get("verdict")
    statuses = [row.get("status") for row in gates]
    embedded = [finding for row in gates for finding in (row.get("findings") or [])]
    if verdict == "PASS_OPUS_SEO_DRAFT":
        if findings or embedded or statuses != ["PASS"] * 5:
            raise RuntimeError("opus_false_pass")
    elif verdict == "BLOCKED_OPUS_SEO_DRAFT":
        if not findings or "FAIL" not in statuses:
            raise RuntimeError("opus_false_blocked")
    else:
        raise RuntimeError("opus_verdict_invalid")


def run_validator(job: dict[str, Any], round_number: int, history: Path) -> None:
    spec = job.get("validator") or {}
    argv = validate_executable(spec, "validator")
    proc = subprocess.run(argv, cwd=job["packet_dir"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=int(spec.get("timeout", 300)))
    receipt = {"round": round_number, "argv": argv, "exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    atomic_json(history / f"round-{round_number:02d}-validator.json", receipt)
    if proc.returncode != 0:
        raise RuntimeError("deterministic_validator_failed")


def run_repair(job: dict[str, Any], payload: dict[str, Any], round_number: int, history: Path, before: dict[str, str]) -> None:
    spec = job.get("repair") or {}
    argv = validate_executable(spec, "repair")
    # A substantive repair can invalidate a deterministic packet invariant
    # (word parity, SERP count, link-plan shape). Give the same bounded worker
    # the exact hash-pinned validator command so it closes those defects before
    # returning; otherwise round N+1 aborts before review and starves the buffer.
    validator_argv = validate_executable(job.get("validator") or {}, "validator")
    packet = Path(job["packet_dir"]).resolve()
    allowed_subject_files = {row["file"] for row in payload["findings"]}
    for row in payload["findings"]:
        allowed_subject_files.update(row.get("related_files") or [])
    extension = job.get("bridge_extension")
    if extension:
        bridge_files = set(extension.get("allowed_changed_files") or [])
        primary_files = {row["file"] for row in payload["findings"]}
        if not primary_files <= bridge_files:
            raise RuntimeError("bridge_extension_repair_scope_exceeded")
        allowed_subject_files = primary_files
    plan = {
        "schema": "glp-opus-seo-repair-plan-v1", "job_id": job["job_id"], "round": round_number,
        "findings": payload["findings"], "allowed_subject_files": sorted(allowed_subject_files),
        "derived_files": sorted(spec.get("derived_files") or []),
        "validator_argv": validator_argv,
    }
    plan["plan_sha256"] = payload_hash(plan, "plan_sha256")
    plan_path = history / f"round-{round_number:02d}-repair-plan.json"
    atomic_json(plan_path, plan)
    env = os.environ.copy()
    env.update({"SEO_REPAIR_PLAN": str(plan_path), "SEO_PACKET_DIR": str(packet), "SEO_JOB_ID": job["job_id"]})
    backups = {rel: (packet / rel).read_bytes() for rel in subject_paths(job)}
    try:
        proc = subprocess.run(argv, cwd=packet, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=int(spec.get("timeout", 1200)))
        atomic_json(history / f"round-{round_number:02d}-repair-execution.json", {"argv": argv, "exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})
        after = subject_snapshot(packet, subject_paths(job))
        changed = sorted(path for path in after if after[path] != before[path])
        allowed = set(plan["allowed_subject_files"]) | set(plan["derived_files"])
        failure = None
        if proc.returncode != 0:
            failure = "repair_command_failed"
        elif not changed:
            failure = "repair_no_changes"
        elif not set(changed) <= allowed or not (set(changed) & set(plan["allowed_subject_files"])):
            failure = "repair_scope_violation"
        if failure:
            raise RuntimeError(failure)
    except Exception:
        for rel, content in backups.items():
            target = packet / rel
            target.write_bytes(content)
            os.chmod(target, 0o600)
        raise


def run_publish(job: dict[str, Any], pass_receipt: Path, history: Path) -> None:
    spec = job.get("publish") or {}
    argv = validate_executable(spec, "publish")
    env = os.environ.copy()
    env.update({"SEO_OPUS_PASS_RECEIPT": str(pass_receipt), "SEO_JOB_ID": job["job_id"]})
    proc = subprocess.run(argv, cwd=job["packet_dir"], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=int(spec.get("timeout", 2400)))
    streams = {
        "stdout": persist_stream(history / "publication.stdout.txt", proc.stdout),
        "stderr": persist_stream(history / "publication.stderr.txt", proc.stderr),
    }
    execution_path = history / "publication-execution.json"
    atomic_json(execution_path, {
        "argv": argv, "exit_code": proc.returncode, "streams": streams,
        "stdout": proc.stdout, "stderr": proc.stderr,
    })
    if proc.returncode != 0:
        stderr_tail = " ".join(proc.stderr[-500:].split())
        raise RuntimeError(
            f"publication_command_failed:rc={proc.returncode}:"
            f"cause={stderr_failure_cause(proc.stderr)}:stderr_tail={stderr_tail}:"
            f"diagnostic={execution_path}:stderr_sha256={streams['stderr']['sha256']}:"
            f"stdout_sha256={streams['stdout']['sha256']}"
        )


def extension_scope_violation(extension: dict[str, Any] | None, payload: dict[str, Any]) -> str | None:
    if extension is None:
        return None
    allowed = {row["id"]: row for row in extension["allowed_findings"]}
    changed_files = set(extension["allowed_changed_files"])
    for finding in payload.get("findings") or []:
        finding_id = finding.get("id")
        if (finding_id not in allowed or finding.get("gate") != "A"
                or finding.get("file") not in changed_files):
            return f"out_of_scope_finding:{finding_id or 'missing-id'}"
    return None


def hold_extension(job: dict[str, Any], extension: dict[str, Any], rounds: list[dict[str, Any]],
                   history: Path, payload: dict[str, Any], reason: str) -> dict[str, Any]:
    signal = {
        "schema": "glp-opus-rubric-signal-v1", "status": "HOLD_OPUS_NONCONVERGENCE",
        "created_at_utc": now(), "job_id": job["job_id"], "topic_id": job["topic_id"],
        "target_query": job.get("target_query"), "target_route": job.get("target_route"),
        "reason": reason, "round_cap": EXTENSION_MAX_ROUNDS, "rounds_completed": len(rounds),
        "final_gates": payload.get("gates") or [], "final_findings": payload.get("findings") or [],
        "bridge_extension": extension, "publication_invoked": False,
        "rubric_review_question": "Is a gate too strict, or is the source material genuinely weak?",
    }
    signal["signal_sha256"] = payload_hash(signal, "signal_sha256")
    signal_path = history / "rubric-signal.json"
    atomic_json(signal_path, signal)
    hold_path = Path(extension["hold_path"])
    if sha_file(hold_path) != extension["hold_sha256"]:
        raise RuntimeError("bridge_extension_hold_ledger_drift")
    ledger = read_json(hold_path)
    if any(row.get("topic_id") == job["topic_id"] for row in ledger.get("entries") or []):
        raise RuntimeError("bridge_extension_hold_topic_already_recorded")
    ledger.setdefault("entries", []).append({
        "topic_id": job["topic_id"], "target_query": job.get("target_query"),
        "target_route": job.get("target_route"), "status": "HOLD_OPUS_NONCONVERGENCE",
        "held_at_utc": now(), "job_id": job["job_id"], "reason": reason,
        "rubric_signal": str(signal_path), "rubric_signal_sha256": sha_file(signal_path),
    })
    atomic_json(hold_path, ledger, exclusive=False)
    closeout = {
        "schema": "glp-opus-seo-loop-closeout-v1", "status": "HOLD_OPUS_NONCONVERGENCE",
        "completed_at_utc": now(), "job_id": job["job_id"], "topic_id": job["topic_id"],
        "rounds": rounds, "publication_invoked": False, "bridge_touched": True,
        "operator_card_created": False, "queue_may_advance": True,
        "rubric_signal": str(signal_path), "rubric_signal_sha256": sha_file(signal_path),
        "hold_ledger": str(hold_path), "hold_ledger_sha256": sha_file(hold_path),
    }
    closeout["closeout_sha256"] = payload_hash(closeout, "closeout_sha256")
    atomic_json(history / "loop-closeout.json", closeout)
    return closeout


def run(job_path: Path, apply: bool, *, review_only_once: bool = False) -> dict[str, Any]:
    if review_only_once and apply:
        raise RuntimeError("review_only_cannot_apply")
    job = read_json(job_path)
    if job.get("schema") != "glp-opus-seo-review-job-v1":
        raise RuntimeError("review_job_contract_invalid")
    if job.get("bridge_extension") is not None:
        raise RuntimeError("operator_extension_retired_rematerialize_for_autonomous_review")
    extension = validate_bridge_extension(job)
    if review_only_once and extension:
        raise RuntimeError("review_only_bridge_extension_forbidden")
    max_rounds = EXTENSION_MAX_ROUNDS if extension else job["max_rounds"]
    if review_only_once:
        max_rounds = 1
    # Which authorized transport runs this job. Jobs written before the
    # 2026-08-18 dual-transport authorization carry no field and stay on Sol.
    active_transport = review_transports.transport(job.get("review_transport"))
    if active_transport["transport_id"] != review_transports.ASTRA_TRANSPORT["transport_id"]:
        raise RuntimeError("astra_only_current_article_review_rematerialize_legacy_job")
    wrapper = Path(active_transport["wrapper"])
    if (sha_file(wrapper) != job.get("review_wrapper_sha256")
            or sha_file(RUBRIC) != job.get("rubric_sha256")
            or sha_file(REVIEW_SCHEMA) != job.get("review_schema_sha256")):
        raise RuntimeError("review_control_hash_invalid")
    authority = validate_topic_authority(job)
    history = Path(str(job.get("history_dir") or "")).resolve()
    history.mkdir(parents=True, exist_ok=True)
    os.chmod(history, 0o700)
    rounds: list[dict[str, Any]] = []
    last_payload: dict[str, Any] = {}
    for round_number in range(1, max_rounds + 1):
        run_validator(job, round_number, history)
        packet = Path(job["packet_dir"]).resolve()
        before = subject_snapshot(packet, subject_paths(job))
        manifest, manifest_path = seal_subjects(job, round_number, history)
        context_bundle = build_context_bundle(job, manifest, round_number, history)
        prompt = build_prompt(job, manifest, manifest_path, round_number, history)
        raw_path = history / f"round-{round_number:02d}-sol-raw.json"
        command = [
            sys.executable, str(wrapper), "--prompt-file", str(prompt),
            "--provider", active_transport["provider"], "--model", active_transport["model"],
            "--effort", active_transport["effort"], "--disable-tools",
            "--json-schema-file", str(REVIEW_SCHEMA),
            "--output-format", "json", "--receipt-file", str(raw_path),
            "--timeout", str(int(job.get("review_timeout") or 1200)),
        ]
        command.extend(["--context-file", str(context_bundle)])
        if review_only_once and active_transport['transport_id'] == 'claude-code-opus-policy-independent':
            command.append('--no-schema-retry')
        proc = subprocess.run(command, cwd=packet, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=int(job.get("review_timeout") or 1200) + 30)
        review_execution_path = history / f"round-{round_number:02d}-review-execution.json"
        review_streams = {
            "stdout": persist_stream(history / f"round-{round_number:02d}-review.stdout.txt", proc.stdout),
            "stderr": persist_stream(history / f"round-{round_number:02d}-review.stderr.txt", proc.stderr),
        }
        atomic_json(review_execution_path, {
            "argv": command, "exit_code": proc.returncode, "streams": review_streams,
            "job_id": job["job_id"], "round": round_number,
            "subject_manifest_sha256": sha_file(manifest_path),
            "raw_review_sha256": sha_file(raw_path) if raw_path.is_file() else None,
        })
        if proc.returncode != 0 or not raw_path.is_file():
            raise RuntimeError(
                f"opus_review_failed:{proc.returncode}:diagnostic={review_execution_path}:"
                f"stderr_sha256={review_streams['stderr']['sha256']}:"
                f"stdout_sha256={review_streams['stdout']['sha256']}"
            )
        raw = read_json(raw_path)
        payload = parse_result(raw)
        last_payload = payload
        validate_review(payload, raw, manifest, round_number)
        receipt = {
            "schema": "glp-opus-seo-round-receipt-v1", "status": payload["verdict"], "reviewed_at_utc": now(),
            "job_id": job["job_id"], "topic_id": job["topic_id"], "round": round_number,
            "provider": active_transport["provider"], "model": active_transport["model"],
            "effort": active_transport["effort"],
            "review_transport": active_transport["transport_id"],
            "tools_disabled": True, "authority": authority,
            "subject_manifest_path": str(manifest_path), "subject_manifest_sha256": sha_file(manifest_path),
            "raw_review_path": str(raw_path), "raw_review_sha256": sha_file(raw_path),
            "gates": payload["gates"], "findings": payload["findings"],
            "effects": {"publishes": 0, "deployments": 0, "cache_purges": 0, "gsc_submissions": 0},
        }
        receipt["receipt_sha256"] = payload_hash(receipt, "receipt_sha256")
        receipt_path = history / f"round-{round_number:02d}-receipt.json"
        atomic_json(receipt_path, receipt)
        rounds.append({"round": round_number, "verdict": payload["verdict"], "receipt": str(receipt_path), "sha256": sha_file(receipt_path)})
        if payload["verdict"] == "PASS_OPUS_SEO_DRAFT":
            if apply:
                run_publish(job, receipt_path, history)
            closeout = {
                "schema": "glp-opus-seo-loop-closeout-v1", "status": "PASS_OPUS_SEO_DRAFT",
                "completed_at_utc": now(), "job_id": job["job_id"], "topic_id": job["topic_id"],
                "rounds": rounds, "publication_invoked": apply, "bridge_touched": bool(extension),
                "operator_card_created": False,
                "review_execution_sha256": sha_file(review_execution_path),
                "raw_review_sha256": sha_file(raw_path),
                "subject_manifest_sha256": sha_file(manifest_path),
                "packet_subject_sha256": subject_snapshot(packet, subject_paths(job)),
            }
            closeout["closeout_sha256"] = payload_hash(closeout, "closeout_sha256")
            atomic_json(history / "loop-closeout.json", closeout)
            return closeout
        violation = extension_scope_violation(extension, payload)
        if violation:
            return hold_extension(job, extension, rounds, history, payload, violation)
        if round_number < max_rounds:
            run_repair(job, payload, round_number, history, before)
    if extension:
        return hold_extension(job, extension, rounds, history, last_payload, "three_round_extension_nonconvergence")
    escalation = {
        "schema": "glp-opus-seo-bridge-escalation-v1", "status": "BLOCKED_OPUS_NONCONVERGENCE",
        "created_at_utc": now(), "job_id": job["job_id"], "topic_id": job["topic_id"],
        "rounds": rounds, "publication_invoked": False,
        # Yield this bounded batch to other candidates; the registered refill
        # resumes source repair in a fresh job rather than asking an operator.
        # Refusal never becomes PASS and never authorizes publication.
        "bridge_escalation_required": False,
        "operator_card_created": False, "queue_may_advance": True,
        "autonomous_retry_required": True,
        "retry_policy": "repair_rematerialize_rereview_next_refill",
        "publication_policy": "host_consumes_only_passing_buffer_alternative",
    }
    escalation["escalation_sha256"] = payload_hash(escalation, "escalation_sha256")
    atomic_json(history / "bridge-escalation.json", escalation)
    return escalation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Invoke the manifest-bound publisher after a clean PASS.")
    parser.add_argument("--review-only-once", action="store_true")
    args = parser.parse_args()
    try:
        result = run(args.job.resolve(), args.apply, review_only_once=args.review_only_once)
    except Exception as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS_OPUS_SEO_DRAFT" else 2


if __name__ == "__main__":
    raise SystemExit(main())
