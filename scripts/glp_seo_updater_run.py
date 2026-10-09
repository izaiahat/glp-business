#!/usr/bin/env python3
"""Webhook-only end-to-end adapter for one due GLP R2 article refresh.

The adapter owns execution, not prioritization. It consumes the newest existing
R2 daily plan and packet, rebinds only current-base/selection authority to the
latest immutable release, then enters the normal deterministic QA -> independent
review -> serialized build/publish chain. It never mints a topic or changes the
weekly ordering.

Retries are keyed to the selected topic+route rather than a calendar day. A
failed attempt does not consume quota; a completed claim makes a repeated
webhook call return PASS_DAILY_QUOTA_COMPLETE without re-running an effectful
step. Three unchanged failed attempts exhaust one authority and advance to its
sealed substitute; a new QA receipt or exact publication-control revision
reopens the repaired authority for one newly pinned attempt unless immutable
route governance makes that authority terminal.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests

BUSINESS = Path("/home/ubuntu/business")
SYSTEM = BUSINESS / "systems/seo-blog-machine"
SYSTEM_SCRIPTS = SYSTEM / "scripts"
if str(SYSTEM_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SYSTEM_SCRIPTS))

import r2_update_carrier as r2  # noqa: E402
import review_transports  # noqa: E402
from current_content_base import verify_resolution  # noqa: E402
from existing_article_updates import canonical_route, load_observability, load_release_articles  # noqa: E402
from machine_config import load_config  # noqa: E402
from r2_execution_control import execution_surface_sha256  # noqa: E402
from selection_receipt import selection_binding, selection_spec  # noqa: E402

SCHEMA = "n8n.seo_updater.v1"
SITE = "https://glpclinicscout.com"
PT = ZoneInfo("America/Los_Angeles")
R2_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/r2-update-carrier"
CLAIM_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/r2-update-operation-claims"
FAILURE_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/r2-update-failures"
ARTICLE_BUFFER_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/article-buffer"
ARTICLE_BUFFER_CLI = SYSTEM_SCRIPTS / "article_buffer_cli.py"
GLOBAL_LOCK = Path("/home/ubuntu/.hermes/state/glp-seo-updater.lock")
SHARED_CARRIER_LOCK = Path("/home/ubuntu/.hermes/state/glp-seo-carrier.lock")
MATERIALIZER = SYSTEM_SCRIPTS / "materialize_opus_run.py"
REVIEW_LOOP = SYSTEM_SCRIPTS / "opus_review_loop.py"
PUBLISHER = SYSTEM_SCRIPTS / "publish_opus_article_release.py"
PRODUCTION_VERIFIER = SYSTEM_SCRIPTS / "verify_opus_article_production.py"
VERIFIER_HELPER = BUSINESS / "scripts/verify_glp_wave2_production.py"
OUTPUT_HEALTH = BUSINESS / "scripts/business_output_health_sensor.py"
OUTPUT_HEALTH_RECEIPT = BUSINESS / "reports/ops/output-health-latest.json"
RELEASES = BUSINESS / "site-system/flagship-v2/releases"
PYTHON = Path("/usr/bin/python3.12")
REVIEW_TRANSPORT = review_transports.ASTRA_TRANSPORT["transport_id"]
REVIEW_REQUEST_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/r2-review-requests"
AUTHORITY_ATTEMPT_LIMIT = 3
PACKET_AUTHORITY_RETRY_SCOPE = "PACKET_OR_AUTHORITY"
PUBLICATION_CONTROL_RETRY_SCOPE = "PUBLICATION_CONTROL"
IMMUTABLE_ROUTE_GOVERNANCE_RETRY_SCOPE = "IMMUTABLE_ROUTE_GOVERNANCE"
UNKNOWN_RETRY_SCOPE = "UNKNOWN"
HTTP_HEADERS = {
    "User-Agent": "GLP-Clinic-Scout-Updater-E2E/1.0",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def child_failure_cause(stderr: bytes, stdout: bytes = b"") -> str:
    """Surface one stable cause while the receipt retains the complete stream."""
    lines = [" ".join(raw.strip().split()) for raw in stderr.decode("utf-8", "replace").splitlines()]
    blocked = [line.removeprefix("BLOCKED: ") for line in lines if line.startswith("BLOCKED: ")]
    if blocked:
        message = blocked[-1]
        if ":cause=" in message:
            message = message.split(":cause=", 1)[1].split(":diagnostic=", 1)[0]
        return message[:240]
    candidates = []
    for line in lines:
        prefix = line.partition(":")[0]
        if line and (prefix == "Error" or prefix.endswith("Error")):
            candidates.append(line)
    if candidates:
        return candidates[-1][:240]
    # The review loop's terminal refusal is JSON on stdout with empty stderr.
    # Preserve that diagnostic only; it grants no retry or publication authority.
    terminal = last_json_line(stdout)
    if (terminal and terminal.get("schema") == "glp-opus-seo-bridge-escalation-v1"
            and terminal.get("publication_invoked") is False
            and terminal.get("status") == "BLOCKED_OPUS_NONCONVERGENCE"):
        return "review_controller:BLOCKED_OPUS_NONCONVERGENCE"
    return "child_stderr_unclassified"


class ChildFailure(RuntimeError):
    def __init__(self, label: str, exit_code: int, cause: str, receipt: Path):
        self.label = label
        self.exit_code = exit_code
        self.cause = cause
        self.receipt = receipt
        super().__init__(
            f"child_failed:{label}:rc={exit_code}:cause={cause}:diagnostic={receipt}"
        )


def failure_identity(error: Exception) -> dict[str, str]:
    """Keep the stable cause structured across the bridge machine boundary."""
    if isinstance(error, ChildFailure):
        cause = error.cause
    else:
        detail = " ".join(str(error).split())
        cause = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
    return {
        "failure_cause": cause[:240],
        "reason": f"{type(error).__name__}: {error}",
    }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pt_date() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(PT).date().isoformat()


def validate_target_pt_date(value: str | None) -> str | None:
    """Validate an explicit late-due handoff without permitting future plans."""
    if value is None:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise RuntimeError("target_pt_date_invalid")
    try:
        target = dt.date.fromisoformat(value)
        current = dt.date.fromisoformat(pt_date())
    except ValueError as error:
        raise RuntimeError("target_pt_date_invalid") from error
    if target > current:
        raise RuntimeError("target_pt_date_future")
    return target.isoformat()


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def payload_hash(value: dict[str, Any], field: str) -> str:
    unsigned = dict(value)
    unsigned.pop(field, None)
    raw = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return sha_bytes(raw)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"json_object_required:{path}")
    return value


def atomic_json(path: Path, value: dict[str, Any], *, self_hash: str | None = None) -> Path:
    payload = dict(value)
    if self_hash:
        payload[self_hash] = payload_hash(payload, self_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return path


def persist_bytes(path: Path, value: bytes) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return {"path": str(path), "bytes": len(value), "sha256": sha_bytes(value)}


def last_json_line(value: bytes) -> dict[str, Any] | None:
    for raw in reversed(value.decode("utf-8", "replace").splitlines()):
        text = raw.strip()
        if not (text.startswith("{") and text.endswith("}")):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def run_child(
    label: str,
    argv: list[str],
    *,
    cwd: Path,
    output_dir: Path,
    timeout: int,
    require_zero: bool = True,
) -> tuple[int, dict[str, Any] | None, Path]:
    started = utc_now()
    timed_out = False
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as error:
        timed_out = True
        exit_code = 124
        stdout = error.stdout or b""
        stderr = (error.stderr or b"") + f"\nupdater child timeout after {timeout}s\n".encode()
    streams = {
        "stdout": persist_bytes(output_dir / f"{label}.stdout.txt", stdout),
        "stderr": persist_bytes(output_dir / f"{label}.stderr.txt", stderr),
    }
    receipt = output_dir / f"{label}-execution.json"
    failure_cause = child_failure_cause(stderr, stdout) if exit_code != 0 else None
    execution = {
        "schema": "glp-seo-updater-child-execution-v1",
        "status": "PASS_CHILD" if exit_code == 0 else "BLOCKED_CHILD_FAILED",
        "label": label,
        "started_at_utc": started,
        "finished_at_utc": utc_now(),
        "argv": argv,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "streams": streams,
        "parsed_last_json": last_json_line(stdout),
    }
    if failure_cause is not None:
        execution["failure_cause"] = failure_cause
    atomic_json(receipt, execution)
    if require_zero and exit_code != 0:
        raise ChildFailure(label, exit_code, failure_cause or "child_stderr_unclassified", receipt)
    return exit_code, last_json_line(stdout), receipt


def process_start_ticks(pid: int) -> int | None:
    """Return Linux process birth ticks so a recycled PID cannot look active."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return int(fields[21])
    except (OSError, ValueError, IndexError):
        return None


def effectful_process_active(state: dict[str, Any]) -> bool:
    pid = state.get("pid")
    ticks = state.get("process_start_ticks")
    return (
        isinstance(pid, int)
        and isinstance(ticks, int)
        and process_start_ticks(pid) == ticks
    )


def effectful_execution_state(attempt: Path) -> tuple[dict[str, Any] | None, Path]:
    path = attempt / "children/review-and-publish-execution.json"
    if not path.is_file():
        return None, path
    state = read_json(path)
    if state.get("execution_sha256") != payload_hash(state, "execution_sha256"):
        raise RuntimeError("effectful_apply_execution_self_hash_invalid")
    return state, path


def pending_effectful_execution(
    claim: dict[str, Any], attempt: Path,
) -> tuple[dict[str, Any] | None, Path]:
    """Inspect durable execution authority only while the claim is in progress."""
    path = attempt / "children/review-and-publish-execution.json"
    if claim.get("status") != "IN_PROGRESS":
        return None, path
    return effectful_execution_state(attempt)


def run_effectful_child(
    label: str,
    argv: list[str],
    *,
    cwd: Path,
    output_dir: Path,
) -> tuple[int, dict[str, Any] | None, Path]:
    """Run an effectful child in its own session with no kill-on-timeout path.

    The durable IN_PROGRESS receipt is written before waiting. If the bridge or
    updater parent is terminated, the publication child survives and the next
    registered invocation either reconciles its terminal receipt or observes the
    exact PID/birth-tick pair and refuses a duplicate launch.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = output_dir / f"{label}.stdout.txt"
    stderr_path = output_dir / f"{label}.stderr.txt"
    execution_path = output_dir / f"{label}-execution.json"
    started = utc_now()
    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
            close_fds=True,
        )
        ticks = process_start_ticks(process.pid)
        if ticks is None:
            process.terminate()
            process.wait()
            raise RuntimeError("effectful_apply_process_identity_unavailable")
        atomic_json(execution_path, {
            "schema": "glp-seo-updater-effectful-child-v1",
            "status": "IN_PROGRESS",
            "label": label,
            "argv": argv,
            "cwd": str(cwd),
            "started_at_utc": started,
            "pid": process.pid,
            "process_start_ticks": ticks,
            "timeout_seconds": None,
            "kill_on_parent_timeout": False,
            "streams": {
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            },
        }, self_hash="execution_sha256")
        exit_code = process.wait()
    stdout = stdout_path.read_bytes()
    stderr = stderr_path.read_bytes()
    failure_cause = child_failure_cause(stderr, stdout) if exit_code != 0 else None
    execution = {
        "schema": "glp-seo-updater-effectful-child-v1",
        "status": "COMPLETE",
        "label": label,
        "argv": argv,
        "cwd": str(cwd),
        "started_at_utc": started,
        "completed_at_utc": utc_now(),
        "pid": process.pid,
        "process_start_ticks": ticks,
        "timeout_seconds": None,
        "kill_on_parent_timeout": False,
        "exit_code": exit_code,
        "streams": {
            "stdout": persist_bytes(stdout_path, stdout),
            "stderr": persist_bytes(stderr_path, stderr),
        },
        "parsed_last_json": last_json_line(stdout),
    }
    if failure_cause is not None:
        execution["failure_cause"] = failure_cause
    atomic_json(execution_path, execution, self_hash="execution_sha256")
    if exit_code != 0:
        raise ChildFailure(
            label,
            exit_code,
            failure_cause or "child_stderr_unclassified",
            execution_path,
        )
    return exit_code, last_json_line(stdout), execution_path


def operation_id(topic_id: str, route: str) -> str:
    return f"r2-refresh:{topic_id}:{canonical_route(route)}"


def claim_path_for(operation: str, claim_root: Path = CLAIM_ROOT) -> Path:
    return claim_root / f"{sha_bytes(operation.encode('utf-8'))}.json"


def load_claim(operation: str, claim_root: Path = CLAIM_ROOT) -> dict[str, Any] | None:
    path = claim_path_for(operation, claim_root)
    if not path.is_file():
        return None
    claim = read_json(path)
    if claim.get("operation_id") != operation:
        raise RuntimeError("r2_operation_claim_hash_collision")
    expected = claim.get("claim_sha256")
    if expected != payload_hash(claim, "claim_sha256"):
        raise RuntimeError("r2_operation_claim_self_hash_invalid")
    return claim


def write_claim(operation: str, claim: dict[str, Any], claim_root: Path = CLAIM_ROOT) -> Path:
    path = claim_path_for(operation, claim_root)
    return atomic_json(path, claim, self_hash="claim_sha256")


def landed_publication_job(claim: dict[str, Any] | None) -> Path | None:
    """Find the one exact publication whose provider effect already landed."""
    attempts = (claim or {}).get("attempts") or []
    if not attempts or not isinstance(attempts[-1], dict):
        return None
    raw_attempt = attempts[-1].get("attempt_dir")
    if not isinstance(raw_attempt, str) or not Path(raw_attempt).is_absolute():
        return None
    attempt = Path(raw_attempt)
    if attempt.is_symlink() or not attempt.is_dir():
        return None
    candidates = []
    for job_path in sorted(attempt.glob("publication-*/release-job.json")):
        report = job_path.parent
        deployment_path = report / "06-deployment-receipt.json"
        final_path = report / "10-final-publication-receipt.json"
        if final_path.exists() or not deployment_path.is_file():
            continue
        deployment = read_json(deployment_path)
        effects = deployment.get("effects") or {}
        if (
            deployment.get("status") == "FAIL_RECONCILIATION_REQUIRED"
            and effects.get("deployments") == 1
        ):
            candidates.append(job_path)
    if len(candidates) > 1:
        raise RuntimeError("multiple_landed_publications_require_reconciliation")
    if not candidates:
        return None
    job_path = candidates[0]
    job = read_json(job_path)
    report = job_path.parent
    if (
        job.get("schema") != "glp-opus-article-release-job-v1"
        or job.get("report_dir") != str(report)
        or job.get("target_route") != (claim or {}).get("target_route")
        or job.get("topic_id") != (claim or {}).get("topic_id")
        or job.get("article_id") != (claim or {}).get("article_id")
    ):
        raise RuntimeError("landed_publication_job_binding_invalid")
    return job_path


def reconciliation_child_label() -> str:
    """Version reconciliation evidence by every control that can change proof."""
    controls = []
    for path in (PUBLISHER, PRODUCTION_VERIFIER, VERIFIER_HELPER):
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("landed_publication_reconciliation_control_invalid")
        controls.append({"path": str(path), "sha256": sha(path)})
    revision = sha_bytes(
        json.dumps(controls, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )[:16]
    return "reconcile-publication-" + revision


def resume_landed_publication(claim: dict[str, Any], attempt: Path) -> Path | None:
    """Resume only the receipt-aware publisher for an already-landed deploy."""
    job_path = landed_publication_job(claim)
    if job_path is None:
        return None
    original_state, _original_path = effectful_execution_state(attempt)
    if original_state is None:
        raise RuntimeError("landed_publication_original_execution_missing")
    if effectful_process_active(original_state):
        raise RuntimeError("landed_publication_original_execution_still_active")
    if (
        original_state.get("status") != "COMPLETE"
        or not isinstance(original_state.get("exit_code"), int)
        or original_state.get("exit_code") == 0
    ):
        raise RuntimeError("landed_publication_original_execution_invalid")

    children = attempt / "children"
    label = reconciliation_child_label()
    execution_path = children / f"{label}-execution.json"
    final = job_path.parent / "10-final-publication-receipt.json"
    for prior_path in sorted(children.glob("reconcile-publication*-execution.json")):
        state = read_json(prior_path)
        if state.get("execution_sha256") != payload_hash(state, "execution_sha256"):
            raise RuntimeError("landed_publication_reconciliation_self_hash_invalid")
        expected_label = prior_path.name.removesuffix("-execution.json")
        if (
            expected_label != state.get("label")
            or not re.fullmatch(r"reconcile-publication(?:-[0-9a-f]{16})?", expected_label)
        ):
            raise RuntimeError("landed_publication_reconciliation_label_invalid")
        if effectful_process_active(state):
            raise RuntimeError("landed_publication_reconciliation_in_progress")
        if state.get("status") != "COMPLETE" or not isinstance(state.get("exit_code"), int):
            raise RuntimeError("landed_publication_reconciliation_outcome_ambiguous")
        if state.get("exit_code") == 0:
            if final.is_file():
                return job_path
            raise RuntimeError("landed_publication_reconciliation_outcome_ambiguous")
        if prior_path == execution_path:
            raise RuntimeError("landed_publication_reconciliation_outcome_ambiguous")

    run_effectful_child(
        label,
        [str(PYTHON), str(PUBLISHER), "--job", str(job_path)],
        cwd=BUSINESS,
        output_dir=children,
    )
    return job_path


def source_daily_plans(
    root: Path = R2_ROOT,
    required_generation: dict[str, Any] | None = None,
    target_pt_date: str | None = None,
) -> list[Path]:
    candidates = sorted(
        root.glob("????-??-??/day-*/daily-plan.json"),
        # A publish remints the same PT day's plan under a new release-generation
        # suffix. Generation hashes are not chronological, so filesystem time —
        # not lexical hash order — selects the latest immutable mint for the day.
        key=lambda path: (path.parent.parent.name, path.stat().st_mtime_ns, path.parent.name),
        reverse=True,
    )
    if target_pt_date is not None:
        candidates = [
            path for path in candidates
            if path.parent.parent.name == target_pt_date
        ]
    if required_generation is None:
        return candidates
    return [
        path for path in candidates
        if read_json(path).get("release_generation") == required_generation
    ]


def current_release_generation() -> dict[str, Any]:
    """Resolve the exact live content generation accepted by the R2 carrier."""
    config = load_config(r2.DEFAULT_CONFIG)
    resolution = config["_current_content_base_resolution"]
    verify_resolution(resolution)
    return r2.current_release_generation(resolution)


def packet_for_authority(plan_path: Path, authority: dict[str, Any]) -> Path:
    return plan_path.parent / f"{int(authority['slot']):02d}-{authority['topic_id']}-packet"


def prior_same_day_generation_packet(
    plan_path: Path,
    plan: dict[str, Any],
    authority: dict[str, Any],
) -> Path | None:
    """Recover an exact-identity QA packet omitted by a same-day release remint."""
    current_key = (plan_path.stat().st_mtime_ns, plan_path.parent.name)
    candidates = sorted(
        plan_path.parent.parent.glob("day-*/daily-plan.json"),
        key=lambda path: (path.stat().st_mtime_ns, path.parent.name),
        reverse=True,
    )
    identity_fields = ("topic_id", "target_route", "target_query", "article_id")
    for candidate_plan_path in candidates:
        if candidate_plan_path == plan_path:
            continue
        if (candidate_plan_path.stat().st_mtime_ns, candidate_plan_path.parent.name) >= current_key:
            continue
        try:
            candidate_plan = read_json(candidate_plan_path)
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError):
            continue
        if candidate_plan.get("as_of") != plan.get("as_of"):
            continue
        matching = [
            row for row in authority_pool(candidate_plan)
            if all(row.get(field) == authority.get(field) for field in identity_fields)
            and row.get("operation") == authority.get("operation")
        ]
        if len(matching) != 1:
            continue
        packet = packet_for_authority(candidate_plan_path, matching[0])
        qa_path = packet / "07-qa-receipt.json"
        authority_path = packet / "00-authority.json"
        if (
            packet.is_symlink()
            or not packet.is_dir()
            or qa_path.is_symlink()
            or not qa_path.is_file()
            or authority_path.is_symlink()
            or not authority_path.is_file()
        ):
            continue
        try:
            qa = read_json(qa_path)
            packet_authority = read_json(authority_path)
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError):
            continue
        if qa.get("status") != "PASS" or not all(
            packet_authority.get(field) == authority.get(field) for field in identity_fields
        ):
            continue
        artifacts = qa.get("artifacts")
        if not isinstance(artifacts, dict) or not artifacts:
            continue
        artifacts_valid = True
        for name, binding in artifacts.items():
            path = packet / str(name)
            if (
                type(name) is not str
                or Path(name).name != name
                or not isinstance(binding, dict)
                or type(binding.get("sha256")) is not str
                or path.is_symlink()
                or not path.is_file()
                or sha(path) != binding["sha256"]
            ):
                artifacts_valid = False
                break
        if artifacts_valid:
            return packet
    return None


def authority_pool(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the sealed primary-first retry universe for one daily plan."""
    return [
        *(dict(row) for row in plan.get("authorities") or []),
        *(dict(row) for row in plan.get("substitute_authorities") or []),
    ]


def bound_file(binding: Any, label: str) -> Path:
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise RuntimeError(f"{label}_spec_invalid")
    raw_path = binding.get("path")
    expected = binding.get("sha256")
    if (
        type(raw_path) is not str
        or not raw_path
        or not Path(raw_path).is_absolute()
        or type(expected) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", expected)
    ):
        raise RuntimeError(f"{label}_spec_invalid")
    try:
        path = Path(raw_path).resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as error:
        raise RuntimeError(f"{label}_file_invalid") from error
    if str(path) != raw_path or path.is_symlink() or not path.is_file() or sha(path) != expected:
        raise RuntimeError(f"{label}_file_invalid")
    return path


def carried_authorities(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate exact same-day completions carried across generation remints."""
    raw = plan.get("carried_completed_authorities")
    if raw is None:
        if plan.get("carried_completed") not in (None, 0):
            raise RuntimeError("r2_carried_completion_count_invalid")
        return []
    if not isinstance(raw, list) or not all(isinstance(row, dict) for row in raw):
        raise RuntimeError("r2_carried_completion_rows_invalid")
    quota = int(plan.get("quota") or 0)
    if (
        plan.get("carried_completed") != len(raw)
        or plan.get("remaining_quota") != quota - len(raw)
        or len(raw) > quota
    ):
        raise RuntimeError("r2_carried_completion_count_invalid")
    actionable_routes = {
        canonical_route(str(row.get("target_route") or ""))
        for row in authority_pool(plan)
    }
    carried_routes: set[str] = set()
    validated: list[dict[str, Any]] = []
    for row in raw:
        topic = str(row.get("topic_id") or "")
        article = str(row.get("article_id") or "")
        route = canonical_route(str(row.get("target_route") or ""))
        operation = str(row.get("operation_id") or "")
        if (
            row.get("status") != "PASS_REFRESHED"
            or not topic
            or not article
            or row.get("target_route") != route
            or operation != operation_id(topic, route)
            or route in actionable_routes
            or route in carried_routes
            or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("target_sha256") or ""))
        ):
            raise RuntimeError("r2_carried_completion_identity_invalid")
        source_plan_path = bound_file(row.get("source_plan"), "r2_carried_source_plan")
        source_plan = read_json(source_plan_path)
        source_day_path = bound_file(row.get("source_day_receipt"), "r2_carried_source_day")
        source_day = read_json(source_day_path)
        claim_path = bound_file(row.get("claim"), "r2_carried_claim")
        claim = read_json(claim_path)
        final_path = bound_file(row.get("publication_receipt"), "r2_carried_publication")
        final = read_json(final_path)
        source_candidate = [
            candidate for candidate in source_day.get("candidates") or []
            if isinstance(candidate, dict) and candidate.get("operation_id") == operation
        ]
        if (
            source_plan.get("plan_sha256") != r2.payload_hash(source_plan, "plan_sha256")
            or source_plan.get("as_of") != plan.get("as_of")
            or source_day.get("receipt_sha256") != payload_hash(source_day, "receipt_sha256")
            or source_day.get("pt_date") != plan.get("as_of")
            or (source_day.get("daily_plan") or {}) != row.get("source_plan")
            or len(source_candidate) != 1
            or source_candidate[0].get("status") != "PASS_REFRESHED"
            or source_candidate[0].get("claim") != row.get("claim")
            or claim.get("claim_sha256") != payload_hash(claim, "claim_sha256")
            or claim.get("status") != "PASS_REFRESHED"
            or claim.get("quota_consumed") is not True
            or claim.get("operation_id") != operation
            or claim.get("topic_id") != topic
            or claim.get("article_id") != article
            or canonical_route(str(claim.get("target_route") or "")) != route
            or claim.get("publication_receipt") != row.get("publication_receipt")
            or final.get("receipt_sha256") != payload_hash(final, "receipt_sha256")
            or final.get("schema") != "glp-opus-article-final-publication-v1"
            or final.get("status") != "PASS_LIVE_GSC_READBACK_PASS"
            or final.get("operation") != "r2_refresh"
            or final.get("topic_id") != topic
            or final.get("article_id") != article
            or final.get("live_url") != f"{SITE}{route}"
            or final.get("target_sha256") != row.get("target_sha256")
        ):
            raise RuntimeError("r2_carried_completion_binding_invalid")
        carried_routes.add(route)
        validated.append(dict(row))
    return validated


def packet_qa_sha256(packet: Path | None) -> str | None:
    if packet is None:
        return None
    qa = packet / "07-qa-receipt.json"
    return sha(qa) if qa.is_file() else None


def next_attempt_number(
    previous: dict[str, Any] | None,
    attempt_root: Path,
) -> int:
    """Reserve a monotonic attempt identity without colliding with orphaned evidence."""
    observed: set[int] = set()
    attempts = (previous or {}).get("attempts") or []
    if not isinstance(attempts, list):
        raise RuntimeError("claim_attempts_invalid")
    for row in attempts:
        number = row.get("attempt") if isinstance(row, dict) else None
        if type(number) is not int or number <= 0:
            raise RuntimeError("claim_attempt_identity_invalid")
        observed.add(number)
    if attempt_root.exists():
        if attempt_root.is_symlink() or not attempt_root.is_dir():
            raise RuntimeError("attempt_root_invalid")
        for child in attempt_root.iterdir():
            match = re.fullmatch(r"attempt-(\d+)-.+", child.name)
            if match:
                observed.add(int(match.group(1)))
    return max(observed, default=0) + 1


def publication_control_revision_changed(claim: dict[str, Any] | None) -> bool:
    """Reopen an exhausted authority only after its exact apply controls changed.

    Every attempt's apply template pins whole-file controls plus the normalized
    R2 execution surface. Comparing that immutable failed-attempt snapshot with
    current regular-file controls lets a repaired builder/control receive one
    new registered-entry attempt without resetting the claim, editing packet QA,
    or weakening the three-attempt bound for unchanged controls. Missing or
    malformed evidence fails closed.
    """
    attempts = (claim or {}).get("attempts") or []
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        return False
    raw_attempt = attempts[-1].get("attempt_dir")
    if type(raw_attempt) is not str or not raw_attempt or not Path(raw_attempt).is_absolute():
        return False
    try:
        attempt = Path(raw_attempt).resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return False
    if str(attempt) != raw_attempt or not attempt.is_dir() or attempt.is_symlink():
        return False
    templates = sorted(attempt.glob("apply-template-*.json"))
    if len(templates) != 1 or templates[0].is_symlink():
        return False
    try:
        template = read_json(templates[0])
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError):
        return False
    controls = template.get("control_files")
    if template.get("schema") != "glp-opus-article-apply-template-v1" or not isinstance(controls, dict) or not controls:
        return False
    changed = False
    for raw_path, expected in controls.items():
        if (
            type(raw_path) is not str
            or not Path(raw_path).is_absolute()
            or type(expected) is not str
            or len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
        ):
            return False
        try:
            path = Path(raw_path).resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            return False
        if str(path) != raw_path or not path.is_file() or path.is_symlink():
            return False
        changed = changed or sha(path) != expected
    surface = template.get("r2_execution_surface")
    if template.get("operation") == "r2_refresh":
        if not isinstance(surface, dict) or set(surface) != {"path", "surface_sha256"}:
            return False
        raw_path = surface.get("path")
        expected = surface.get("surface_sha256")
        if (
            type(raw_path) is not str
            or not Path(raw_path).is_absolute()
            or type(expected) is not str
            or len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
        ):
            return False
        try:
            path = Path(raw_path).resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            return False
        if str(path) != raw_path or not path.is_file() or path.is_symlink():
            return False
        try:
            actual = execution_surface_sha256(path)
        except (OSError, UnicodeError, SyntaxError, RuntimeError):
            return False
        changed = changed or actual != expected
    elif surface is not None:
        return False
    return changed


def claim_effective_status(
    claim: dict[str, Any] | None,
    packet: Path | None = None,
) -> str:
    """Bound one authority without consuming quota or wedging substitutes."""
    status = str((claim or {}).get("status") or "NOT_ATTEMPTED")
    if (
        status == "FAILED_RETRYABLE"
        and len((claim or {}).get("attempts") or []) >= AUTHORITY_ATTEMPT_LIMIT
    ):
        attempts = (claim or {}).get("attempts") or []
        retry_scope = effective_failure_retry_scope(claim)
        if retry_scope == IMMUTABLE_ROUTE_GOVERNANCE_RETRY_SCOPE:
            return "FAILED_EXHAUSTED"
        claimed_revision = str(
            (attempts[-1].get("final_packet_qa_sha256") if isinstance(attempts[-1], dict) else None)
            or (claim or {}).get("source_packet_qa_sha256")
            or ""
        )
        current_revision = str(packet_qa_sha256(packet) or "")
        if claimed_revision and current_revision and claimed_revision != current_revision:
            return "FAILED_RETRYABLE"
        if (
            retry_scope != PACKET_AUTHORITY_RETRY_SCOPE
            and publication_control_revision_changed(claim)
        ):
            return "FAILED_RETRYABLE"
        return "FAILED_EXHAUSTED"
    return status


def find_due(
    root: Path = R2_ROOT,
    claim_root: Path = CLAIM_ROOT,
    required_generation: dict[str, Any] | None = None,
    target_pt_date: str | None = None,
) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None, Path | None]:
    latest_plan: Path | None = None
    latest_payload: dict[str, Any] | None = None
    for plan_path in source_daily_plans(
        root, required_generation, target_pt_date,
    ):
        plan = read_json(plan_path)
        if latest_plan is None:
            latest_plan, latest_payload = plan_path, plan
        if quota_state(plan, claim_root)["deficit"] == 0:
            break
        # A provider write that already landed outranks every fresh attempt,
        # even if the authority would otherwise be exhausted. Reconcile the
        # exact deployment before another topic can advance the shared head.
        for authority in authority_pool(plan):
            operation = operation_id(authority["topic_id"], authority["target_route"])
            claim = load_claim(operation, claim_root)
            if landed_publication_job(claim) is None:
                continue
            packet = packet_for_authority(plan_path, authority)
            if not packet.exists():
                packet = prior_same_day_generation_packet(plan_path, plan, authority) or packet
            if packet.is_dir() and (packet / "07-qa-receipt.json").is_file():
                return plan_path, plan, authority, packet
        for authority in authority_pool(plan):
            operation = operation_id(authority["topic_id"], authority["target_route"])
            claim = load_claim(operation, claim_root)
            packet = packet_for_authority(plan_path, authority)
            if not packet.exists():
                packet = prior_same_day_generation_packet(plan_path, plan, authority) or packet
            effective_packet, _reason = retry_source_packet(claim, packet)
            if claim_effective_status(claim, effective_packet) in {"PASS_REFRESHED", "FAILED_EXHAUSTED"}:
                continue
            if effective_packet.is_dir() and (effective_packet / "07-qa-receipt.json").is_file():
                return plan_path, plan, authority, effective_packet
        # Only the newest minted day is eligible. Older plans are historical,
        # not a backlog that can silently override current prioritization.
        break
    return latest_plan, latest_payload, None, None


def quota_state(plan: dict[str, Any], claim_root: Path = CLAIM_ROOT) -> dict[str, int]:
    quota = int(plan.get("quota") or 0)
    authorized = authority_pool(plan)
    carried = carried_authorities(plan)
    completed = len(carried)
    for row in authorized:
        claim = load_claim(operation_id(row["topic_id"], row["target_route"]), claim_root)
        completed += int(bool(claim and claim.get("status") == "PASS_REFRESHED"))
    return {
        "required": quota,
        "authorized": len(carried) + len(authorized),
        "completed": completed,
        "deficit": max(0, quota - completed),
    }


def write_day_receipt(
    plan_path: Path,
    plan: dict[str, Any],
    claim_root: Path = CLAIM_ROOT,
) -> Path:
    """Write current, hash-bound day output truth without claiming future quota."""
    candidates: list[dict[str, Any]] = []
    carried = carried_authorities(plan)
    completed = len(carried)
    published = len(carried)
    attempts = 0
    for row in carried:
        claim_path = Path(row["claim"]["path"])
        claim = read_json(claim_path)
        attempts += len(claim.get("attempts") or [])
        candidates.append({
            "authority_role": "carried",
            "operation_id": row["operation_id"],
            "topic_id": row["topic_id"],
            "target_route": row["target_route"],
            "status": "PASS_REFRESHED",
            "claim": row["claim"],
            "publication_receipt": row["publication_receipt"],
            "source_plan": row["source_plan"],
            "source_day_receipt": row["source_day_receipt"],
            "target_sha256": row["target_sha256"],
        })
    for row in authority_pool(plan):
        operation = operation_id(row["topic_id"], row["target_route"])
        claim_path = claim_path_for(operation, claim_root)
        claim = load_claim(operation, claim_root)
        status = claim_effective_status(claim, packet_for_authority(plan_path, row))
        completed += int(status == "PASS_REFRESHED")
        published += int(status in {"PUBLISHED_PENDING_HEALTH", "PASS_REFRESHED"})
        attempts += len((claim or {}).get("attempts") or [])
        candidates.append({
            "authority_role": row.get("authority_role") or "primary",
            "operation_id": operation,
            "topic_id": row["topic_id"],
            "target_route": row["target_route"],
            "status": status,
            "claim": (
                {"path": str(claim_path), "sha256": sha(claim_path)}
                if claim is not None else None
            ),
        })
    required = int(plan.get("quota") or 0)
    status = (
        "PASS_DAILY_QUOTA"
        if completed >= required
        else "PASS_NEVER_ZERO_QUOTA_IN_PROGRESS"
        if published > 0
        else "IN_PROGRESS_ZERO_PUBLISHED"
    )
    return atomic_json(plan_path.parent / "r2-day-receipt.json", {
        "schema": "glp-seo-r2-day-receipt-v1",
        "status": status,
        "observed_at_utc": utc_now(),
        "pt_date": str(plan.get("as_of") or ""),
        "daily_plan": {"path": str(plan_path), "sha256": sha(plan_path)},
        "required": required,
        "authorized": len(candidates),
        "completed": completed,
        "published": published,
        "deficit": max(0, required - completed),
        "attempts": attempts,
        "candidates": candidates,
    }, self_hash="receipt_sha256")


def active_article_carrier_claims(route: str, root: Path = ARTICLE_BUFFER_ROOT) -> list[dict[str, Any]]:
    target = canonical_route(route)
    claims = root / "claims"
    consumed = root / "consumed"
    collisions: list[dict[str, Any]] = []
    if not claims.is_dir():
        return collisions
    for path in sorted(claims.glob("*.json")):
        if (consumed / path.name).is_file():
            continue
        try:
            claim = read_json(path)
        except Exception:
            continue
        if claim.get("target_route") == target:
            collisions.append({
                "claim": str(path),
                "operation_id": claim.get("operation_id"),
                "entry_id": claim.get("entry_id"),
                "target_route": target,
            })
    return collisions


def assert_no_article_carrier_collision(route: str, attempt: Path) -> dict[str, Any]:
    active = active_article_carrier_claims(route)
    _rc, status, execution = run_child(
        "article-carrier-status",
        [str(PYTHON), str(ARTICLE_BUFFER_CLI), "status"],
        cwd=BUSINESS,
        output_dir=attempt / "children",
        timeout=300,
        require_zero=False,
    )
    ready_routes = [
        row.get("target_route")
        for row in ((status or {}).get("entries") or [])
        if isinstance(row, dict)
    ]
    if active:
        raise RuntimeError(f"article_carrier_active_route_claim_collision:{active}")
    if route in ready_routes:
        raise RuntimeError(f"article_carrier_ready_route_collision:{route}")
    return {
        "status": "PASS_NO_ROUTE_OR_CLAIM_COLLISION",
        "target_route": route,
        "active_article_carrier_claims": 0,
        "article_carrier_ready_routes": ready_routes,
        "article_carrier_status_execution": str(execution),
        "updater_claim_root": str(CLAIM_ROOT),
        "article_carrier_claim_root": str(ARTICLE_BUFFER_ROOT / "claims"),
        "operation_namespace": "r2-refresh:<topic-id>:<route>",
        "shared_mutation_lock": str(SHARED_CARRIER_LOCK),
    }


def current_authority(
    source: dict[str, Any],
    *,
    as_of: dt.date,
    output_dir: Path,
) -> tuple[dict[str, Any], Path, dict[str, Any], Path, dict[str, Any]]:
    config = load_config(r2.DEFAULT_CONFIG)
    resolution = config["_current_content_base_resolution"]
    verify_resolution(resolution)
    release_root = Path(resolution["pointer"]["release_root"])
    articles, _catalogs = load_release_articles(release_root)
    current_routes = {canonical_route(row["canonical_path"]) for row in articles}
    metrics, _evidence = load_observability(r2.DEFAULT_DATABASE, window_end=as_of)
    # Prioritization is the existing immutable daily plan's job. Re-running the
    # weekly scorer after an unrelated deploy can legitimately return a
    # different set and must not erase an already-authorized, researched packet
    # whose quota is still outstanding. Rebind only its current-base inputs.
    source_authority_spec = source.get("authority") or {}
    source_authority_path = Path(str(source_authority_spec.get("path") or "")).resolve(strict=True)
    if sha(source_authority_path) != source_authority_spec.get("sha256"):
        raise RuntimeError("r2_source_daily_authority_hash_drift")
    source_authority = read_json(source_authority_path)
    weekly_spec = (source_authority.get("inputs") or {}).get("weekly_gsc_selection") or {}
    weekly_source = Path(str(weekly_spec.get("path") or "")).resolve(strict=True)
    if sha(weekly_source) != weekly_spec.get("sha256"):
        raise RuntimeError("r2_source_weekly_selection_hash_drift")
    weekly = read_json(weekly_source)
    candidate = r2.selected_weekly_row(
        weekly,
        source["target_route"],
        source["article_id"],
    )
    if r2.topic_id(candidate, as_of) != source["topic_id"]:
        raise RuntimeError("r2_prioritization_identity_changed")
    weekly_dir = output_dir / "authority"
    weekly_path = r2.write_sealed(
        r2.current_release_weekly_selection_path(weekly_dir, resolution),
        weekly,
        "receipt_sha256",
    )
    striking_routes = {
        canonical_route(route)
        for route, metric in metrics.items()
        if (
            (5 <= float(metric.get("position") or 0) <= 20 and int(metric.get("impressions") or 0) >= 50)
            or any(
                5 <= float(query.get("position") or 0) <= 20
                and int(query.get("impressions") or 0) > 0
                for query in metric.get("queries") or []
                if isinstance(query, dict)
            )
        )
        and route in current_routes
    }
    minted = r2.mint_candidate(
        candidate,
        output_dir=weekly_dir,
        config=config,
        config_path=r2.DEFAULT_CONFIG.resolve(strict=True),
        database=r2.DEFAULT_DATABASE.resolve(strict=True),
        weekly_selection=weekly_path,
        resolution=resolution,
        current_routes=current_routes,
        striking_routes=striking_routes,
        as_of=as_of,
        slot=int(source["slot"]),
    )
    authority_path = Path(minted["authority"]["path"])
    selection_path = Path(minted["selection"]["path"])
    return read_json(authority_path), authority_path, read_json(selection_path), selection_path, {
        "owner": "external keyword-gap/prioritization lane",
        "priority_authority": {"path": str(source_authority_path), "sha256": sha(source_authority_path)},
        "weekly_selection_authority": {"path": str(weekly_source), "sha256": sha(weekly_source)},
        "current_release_rebind": {
            "status": "PASS_REBOUND_TO_CURRENT_RELEASE",
            "previous_release": (source_authority.get("current_release") or {}).get("release_root"),
            "current_release": str(release_root),
        },
        "selected_route_unchanged": True,
        "selected_topic_id_unchanged": True,
        "prioritization_changed": False,
    }


def rebind_packet_selection_metadata(
    packet: Path,
    packet_authority: dict[str, Any],
    update_authority: dict[str, Any],
) -> list[str]:
    """Rebind brief identity only; keep evidence, authored prose and dates intact."""
    binding = packet_authority["selection_binding"]
    selected = packet_authority["selection_receipt"]
    changed: list[str] = []
    serp_path = packet / "02-serp-brief.json"
    if serp_path.is_file():
        serp = json.loads(serp_path.read_text(encoding="utf-8"))
        serp["selection_binding"] = {
            **binding,
            "article_id": update_authority["article_id"],
            "selection_path": selected["path"],
            "selection_sha256": selected["sha256"],
            "selection_receipt_sha256": selected["receipt_sha256"],
        }
        atomic_json(serp_path, serp)
        changed.append(serp_path.name)
    brief_path = packet / "03-editorial-brief.md"
    if brief_path.is_file():
        text = brief_path.read_text(encoding="utf-8")
        fields = {
            "Selection": selected["path"],
            "Selection SHA-256": selected["sha256"],
            "Selection receipt SHA-256": selected["receipt_sha256"],
            "Operator directive ID": binding["operator_directive_id"],
            "Queue SHA-256": binding["queue_sha256"],
            "Strategy queue SHA-256": binding["strategy_queue_sha256"],
            "Strategy map SHA-256": binding["strategy_map_sha256"],
        }
        for label, value in fields.items():
            text = re.sub(r"^- " + re.escape(label) + r":.*$",
                          lambda _m, line=f"- {label}: `{value}`": line, text, flags=re.M)
        base = update_authority["inputs"]["current_content_base_state"]
        text = re.sub(r"^- Current content base:.*$",
                      lambda _m: f"- Current content base: `{base['path']}` SHA-256 `{base['sha256']}`",
                      text, flags=re.M)
        brief_path.write_text(text, encoding="utf-8")
        changed.append(brief_path.name)
    return changed


def rebind_packet(
    source_packet: Path,
    target_packet: Path,
    authority: dict[str, Any],
    authority_path: Path,
    selection: dict[str, Any],
    selection_path: Path,
    output_dir: Path,
) -> Path:
    shutil.copytree(source_packet, target_packet)
    packet_authority_path = target_packet / "00-authority.json"
    before = packet_authority_path.read_bytes()
    packet_authority = json.loads(before)
    packet_authority.update(authority)
    # This packet is a mutable review subject, not the sealed update authority.
    # Keeping the authority's self-hash after adding packet fields would be a lie.
    packet_authority.pop("authority_sha256", None)
    selected = selection["selected"][0]
    topic = authority["topic_id"]
    route = authority["target_route"]
    query = authority["target_query"]
    packet_authority.update({
        "requested_queue_item_id": topic,
        "observed_queue_topic_id": topic,
        "existing_article_id": authority["article_id"],
        "route": route,
        "topic": query,
        "target": {"query": query, "route": route},
        "selection_receipt": selection_spec(selection_path),
        "selection_binding": selection_binding(selection),
    })
    queue = dict(packet_authority.get("queue_authority") or {})
    queue.update({
        "topic_id": topic,
        "target_query": query,
        "target_route": route,
        "operator_ready_for_research": True,
        "evidence_state": "READY_FOR_RESEARCH",
        "operator_directive_id": selected["operator_directive_id"],
    })
    packet_authority["queue_authority"] = queue
    atomic_json(packet_authority_path, packet_authority)
    rebound_metadata = rebind_packet_selection_metadata(target_packet, packet_authority, authority)
    receipt = output_dir / "packet-authority-rebind.json"
    atomic_json(receipt, {
        "schema": "glp-seo-r2-packet-authority-rebind-v1",
        "status": "PASS_CURRENT_BASE_REBOUND_WITHOUT_REPRIORITIZATION",
        "created_at_utc": utc_now(),
        "source_packet": str(source_packet),
        "target_packet": str(target_packet),
        "packet_authority_before_sha256": sha_bytes(before),
        "packet_authority_after_sha256": sha(packet_authority_path),
        "fresh_update_authority": {"path": str(authority_path), "sha256": sha(authority_path)},
        "fresh_selection": selection_spec(selection_path),
        "authored_subject_files_changed": ["00-authority.json", *rebound_metadata],
        "authored_content_files_preserved": [
            "01-evidence-ledger.json", "04-draft.md", "05-claim-source-map.json",
        ],
        "selection_metadata_only_rebound": rebound_metadata,
        "prioritization_changed": False,
    })
    return receipt


def sitemap_lastmod(value: bytes, url: str) -> str | None:
    root = ElementTree.fromstring(value)
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for row in root.findall("s:url", namespace):
        if row.findtext("s:loc", namespaces=namespace) == url:
            return row.findtext("s:lastmod", namespaces=namespace)
    return None


def live_snapshot(route: str, output_dir: Path, label: str) -> dict[str, Any]:
    url = SITE + canonical_route(route)
    page = requests.get(url, headers=HTTP_HEADERS, timeout=60)
    sitemap = requests.get(SITE + "/sitemap.xml", headers=HTTP_HEADERS, timeout=60)
    page_spec = persist_bytes(output_dir / f"{label}-page.html", page.content)
    sitemap_spec = persist_bytes(output_dir / f"{label}-sitemap.xml", sitemap.content)
    lastmod = sitemap_lastmod(sitemap.content, url) if sitemap.status_code == 200 else None
    return {
        "captured_at_utc": utc_now(),
        "url": url,
        "http_status": page.status_code,
        "page": page_spec,
        "sitemap_url": SITE + "/sitemap.xml",
        "sitemap_http_status": sitemap.status_code,
        "sitemap": sitemap_spec,
        "lastmod": lastmod,
    }


def visible_text(value: bytes) -> str:
    text = value.decode("utf-8", "replace")
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def verify_live_delta(route: str, before: dict[str, Any], output_dir: Path, timeout_s: int = 600) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    attempts = 0
    while True:
        attempts += 1
        after = live_snapshot(route, output_dir, "live-after")
        page_changed = after["page"]["sha256"] != before["page"]["sha256"]
        lastmod_changed = bool(
            before.get("lastmod") and after.get("lastmod") and after["lastmod"] > before["lastmod"]
        )
        if (
            after["http_status"] == 200
            and after["sitemap_http_status"] == 200
            and page_changed
            and lastmod_changed
        ):
            old = Path(before["page"]["path"]).read_bytes()
            new = Path(after["page"]["path"]).read_bytes()
            similarity = difflib.SequenceMatcher(None, visible_text(old), visible_text(new), autojunk=False).ratio()
            return {
                "status": "PASS_LIVE_CONTENT_AND_LASTMOD_DELTA",
                "attempts": attempts,
                "before": before,
                "after": after,
                "content_delta": {
                    "page_sha256_changed": True,
                    "before_sha256": before["page"]["sha256"],
                    "after_sha256": after["page"]["sha256"],
                    "before_bytes": before["page"]["bytes"],
                    "after_bytes": after["page"]["bytes"],
                    "visible_text_change_fraction": round(1.0 - similarity, 6),
                },
                "sitemap_delta": {
                    "before_lastmod": before["lastmod"],
                    "after_lastmod": after["lastmod"],
                    "advanced": True,
                },
            }
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "live_delta_not_observed:"
                f"http={after['http_status']}:sitemap_http={after['sitemap_http_status']}:"
                f"page_changed={page_changed}:before_lastmod={before.get('lastmod')}:"
                f"after_lastmod={after.get('lastmod')}"
            )
        time.sleep(15)


def output_health_evaluation(attempt: Path) -> dict[str, Any]:
    rc, _payload, execution = run_child(
        "output-health-evaluation",
        [str(PYTHON), str(OUTPUT_HEALTH)],
        cwd=BUSINESS,
        output_dir=attempt / "children",
        timeout=300,
        require_zero=False,
    )
    receipt = read_json(OUTPUT_HEALTH_RECEIPT)
    row = next(
        (item for item in receipt.get("results") or [] if item.get("machine") == "seo_update_carrier"),
        None,
    )
    if not row or row.get("ok") is not True:
        raise RuntimeError(f"seo_update_carrier_output_health_not_healthy:{row}")
    return {
        "status": "PASS_SEO_UPDATE_CARRIER_HEALTHY",
        "sensor_exit_code": rc,
        "sensor_receipt": {"path": str(OUTPUT_HEALTH_RECEIPT), "sha256": sha(OUTPUT_HEALTH_RECEIPT)},
        "sensor_execution": str(execution),
        "row": row,
    }


def stage_publication_for_health(
    claim: dict[str, Any],
    *,
    operation: str,
    plan_path: Path,
    plan: dict[str, Any],
    final_path: Path,
    live_proof: dict[str, Any],
    claim_root: Path = CLAIM_ROOT,
) -> Path:
    """Expose a live publication truthfully before the fleet sensor reads it."""
    claim.update({
        "status": "PUBLISHED_PENDING_HEALTH",
        "quota_consumed": False,
        "publication_receipt": {"path": str(final_path), "sha256": sha(final_path)},
        "live_proof": live_proof,
        "updated_at_utc": utc_now(),
    })
    write_claim(operation, claim, claim_root)
    return write_day_receipt(plan_path, plan, claim_root)


def historical_publication_health(
    *,
    plan_path: Path,
    plan: dict[str, Any],
    operation: str,
    day_receipt_path: Path,
) -> dict[str, Any]:
    """Validate a stranded prior-day publication without reading today's quota."""
    as_of = str(plan.get("as_of") or "")
    if not as_of or as_of >= pt_date():
        raise RuntimeError("historical_publication_health_date_invalid")
    receipt = read_json(day_receipt_path)
    daily_plan = receipt.get("daily_plan") or {}
    candidates = receipt.get("candidates") or []
    matching = [
        row for row in candidates
        if isinstance(row, dict) and row.get("operation_id") == operation
    ]
    if (
        receipt.get("receipt_sha256") != payload_hash(receipt, "receipt_sha256")
        or receipt.get("schema") != "glp-seo-r2-day-receipt-v1"
        or receipt.get("pt_date") != as_of
        or daily_plan.get("path") != str(plan_path)
        or daily_plan.get("sha256") != sha(plan_path)
        or receipt.get("status") not in {"PASS_NEVER_ZERO_QUOTA_IN_PROGRESS", "PASS_DAILY_QUOTA"}
        or not isinstance(receipt.get("published"), int)
        or int(receipt["published"]) < 1
        or len(matching) != 1
        or matching[0].get("status") not in {"PUBLISHED_PENDING_HEALTH", "PASS_REFRESHED"}
    ):
        raise RuntimeError("historical_publication_day_receipt_not_healthy")
    return {
        "status": "PASS_HISTORICAL_R2_PUBLICATION_HEALTHY",
        "plan_as_of": as_of,
        "day_receipt": {"path": str(day_receipt_path), "sha256": sha(day_receipt_path)},
        "row": matching[0],
        "current_day_sensor_skipped": True,
        "reason": "the fleet sensor is date-scoped to the current PT day; exact prior-day claim/day truth was validated directly",
    }


def terminal_publication_binding(
    final_path: Path,
    final: dict[str, Any],
    *,
    route: str,
    topic_id: str,
    article_id: str,
) -> dict[str, Any]:
    """Bind updater success to the exact self-hashed R2 article and live bytes."""
    if final.get("receipt_sha256") != payload_hash(final, "receipt_sha256"):
        raise RuntimeError("terminal_publication_receipt_self_hash_invalid")
    expected_url = SITE + canonical_route(route)
    if (
        final.get("schema") != "glp-opus-article-final-publication-v1"
        or final.get("status") != "PASS_LIVE_GSC_READBACK_PASS"
        or final.get("operation") != "r2_refresh"
        or final.get("live_url") != expected_url
        or str(final.get("topic_id") or "") != topic_id
        or str(final.get("article_id") or "") != article_id
        or not re.fullmatch(r"[0-9a-f]{64}", str(final.get("target_sha256") or ""))
        or not isinstance(final.get("target_bytes"), int)
        or int(final["target_bytes"]) <= 0
    ):
        raise RuntimeError("terminal_publication_exact_article_binding_invalid")
    manifest_path = final_path.parent / "01-deployment-manifest.json"
    manifest_spec = final.get("manifest") or {}
    if (
        manifest_spec.get("path") != str(manifest_path)
        or manifest_spec.get("sha256") != sha(manifest_path)
    ):
        raise RuntimeError("terminal_publication_manifest_binding_invalid")
    manifest = read_json(manifest_path)
    if manifest.get("manifest_payload_sha256") != payload_hash(
        manifest, "manifest_payload_sha256"
    ):
        raise RuntimeError("terminal_publication_manifest_self_hash_invalid")
    delta = manifest.get("delta") or {}
    if (
        delta.get("target_sha256") != final["target_sha256"]
        or delta.get("target_bytes") != final["target_bytes"]
        or manifest.get("release_tree_sha256") != final.get("release_tree_sha256")
        or manifest.get("public_tree_sha256") != final.get("public_tree_sha256")
    ):
        raise RuntimeError("terminal_publication_manifest_target_binding_invalid")
    production_head = final.get("canonical_production_head") or {}
    if production_head.get("deployment_id") != final.get("deployment_id"):
        raise RuntimeError("terminal_publication_production_head_binding_invalid")
    readbacks = final.get("live_readback") or []
    hosts = {str(row.get("host") or "") for row in readbacks if isinstance(row, dict)}
    required_hosts = {SITE, "https://www.glpclinicscout.com"}
    canonical = next(
        (row for row in readbacks if isinstance(row, dict) and row.get("host") == SITE),
        None,
    )
    if (
        not required_hosts.issubset(hosts)
        or not readbacks
        or not all(isinstance(row, dict) and row.get("passed") is True for row in readbacks)
        or canonical is None
        or not re.fullmatch(r"[0-9a-f]{64}", str(canonical.get("sha256") or ""))
    ):
        raise RuntimeError("terminal_publication_live_readback_binding_invalid")
    return {
        "status": "PASS_EXACT_ARTICLE_AND_LIVE_SHA_BOUND",
        "topic_id": topic_id,
        "article_id": article_id,
        "live_url": expected_url,
        "target_sha256": final["target_sha256"],
        "target_bytes": final["target_bytes"],
        "canonical_live_sha256": canonical["sha256"],
        "deployment_id": final["deployment_id"],
        "manifest": {"path": str(manifest_path), "sha256": sha(manifest_path)},
        "publication_receipt": {"path": str(final_path), "sha256": sha(final_path)},
    }


def reconcile_completed_publication(
    claim: dict[str, Any],
    *,
    route: str,
    attempt: Path,
) -> tuple[dict[str, Any], Path] | None:
    """Recover a publish that finished before the claim reached PASS.

    Once a terminal publication receipt exists, retries only re-read live bytes
    and Output Health. They never blind-rerun the effectful chain.
    """
    publication = claim.get("publication_receipt") or {}
    candidates: list[Path] = []
    if publication.get("path"):
        candidates.append(Path(str(publication["path"])))
    candidates.extend(sorted(attempt.rglob("10-final-publication-receipt.json")))
    for final_path in dict.fromkeys(candidates):
        if not final_path.is_file():
            continue
        final = read_json(final_path)
        if (
            final.get("status") != "PASS_LIVE_GSC_READBACK_PASS"
        ):
            continue
        publication_binding = terminal_publication_binding(
            final_path,
            final,
            route=route,
            topic_id=str(claim.get("topic_id") or ""),
            article_id=str(claim.get("article_id") or ""),
        )
        live_proof = claim.get("live_proof")
        if not isinstance(live_proof, dict):
            before_page = attempt / "live/live-before-page.html"
            before_sitemap = attempt / "live/live-before-sitemap.xml"
            if not before_page.is_file() or not before_sitemap.is_file():
                raise RuntimeError("published_attempt_missing_before_readback")
            before_sitemap_bytes = before_sitemap.read_bytes()
            before = {
                "captured_at_utc": None,
                "url": SITE + route,
                "http_status": 200,
                "page": {"path": str(before_page), "bytes": before_page.stat().st_size, "sha256": sha(before_page)},
                "sitemap_url": SITE + "/sitemap.xml",
                "sitemap_http_status": 200,
                "sitemap": {"path": str(before_sitemap), "bytes": before_sitemap.stat().st_size, "sha256": sha(before_sitemap)},
                "lastmod": sitemap_lastmod(before_sitemap_bytes, SITE + route),
            }
            live_proof = verify_live_delta(route, before, attempt / "live")
        live_proof["publication_binding"] = publication_binding
        return live_proof, final_path
    return None


def finalized_claim_reconciliation_candidate(
    claim_root: Path = CLAIM_ROOT,
    r2_root: Path = R2_ROOT,
) -> dict[str, Any] | None:
    """Find one terminal publication stranded before its claim/day commit."""
    candidates: list[dict[str, Any]] = []
    root = r2_root.resolve(strict=True)
    if not claim_root.is_dir():
        return None
    for claim_path in sorted(claim_root.glob("*.json")):
        if claim_path.is_symlink() or not claim_path.is_file():
            raise RuntimeError("r2_operation_claim_entry_invalid")
        raw = read_json(claim_path)
        operation = str(raw.get("operation_id") or "")
        if not operation or claim_path_for(operation, claim_root) != claim_path:
            raise RuntimeError("r2_operation_claim_path_binding_invalid")
        claim = load_claim(operation, claim_root)
        if not claim or claim.get("status") == "PASS_REFRESHED" or claim.get("quota_consumed") is True:
            continue
        attempts = claim.get("attempts") or []
        if not attempts or not isinstance(attempts[-1], dict):
            continue
        raw_attempt = attempts[-1].get("attempt_dir")
        if not isinstance(raw_attempt, str) or not Path(raw_attempt).is_absolute():
            raise RuntimeError("finalized_claim_attempt_path_invalid")
        attempt = Path(raw_attempt)
        try:
            resolved_attempt = attempt.resolve(strict=True)
            resolved_attempt.relative_to(root)
        except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
            raise RuntimeError("finalized_claim_attempt_path_invalid") from exc
        if attempt.is_symlink() or not resolved_attempt.is_dir() or str(resolved_attempt) != raw_attempt:
            raise RuntimeError("finalized_claim_attempt_path_invalid")
        final_paths = sorted(resolved_attempt.glob("publication-*/10-final-publication-receipt.json"))
        if len(final_paths) > 1:
            raise RuntimeError("multiple_final_publication_receipts_for_claim")
        if not final_paths:
            continue
        final = read_json(final_paths[0])
        if final.get("status") != "PASS_LIVE_GSC_READBACK_PASS":
            continue
        if len(resolved_attempt.parents) < 3:
            raise RuntimeError("finalized_claim_plan_path_invalid")
        plan_path = resolved_attempt.parents[2] / "daily-plan.json"
        if plan_path.is_symlink() or not plan_path.is_file():
            raise RuntimeError("finalized_claim_plan_path_invalid")
        plan = read_json(plan_path)
        matching = [
            row for row in authority_pool(plan)
            if row.get("topic_id") == claim.get("topic_id")
            and row.get("target_route") == claim.get("target_route")
            and row.get("article_id") == claim.get("article_id")
        ]
        if len(matching) != 1:
            raise RuntimeError("finalized_claim_plan_authority_binding_invalid")
        candidates.append({
            "claim": claim,
            "claim_path": claim_path,
            "operation": operation,
            "attempt": resolved_attempt,
            "plan_path": plan_path,
            "plan": plan,
            "authority": matching[0],
            "final_path": final_paths[0],
            "final_completed_at_utc": str(final.get("completed_at_utc") or ""),
        })
    if not candidates:
        return None
    candidates.sort(key=lambda row: (
        str(row["plan"].get("as_of") or ""),
        row["final_completed_at_utc"],
        row["operation"],
    ))
    selected = candidates[0]
    selected["pending_reconciliation_count"] = len(candidates)
    return selected


def retry_source_packet(
    previous: dict[str, Any] | None,
    default_packet: Path,
) -> tuple[Path, str]:
    """Carry a valid post-review packet into a retry after build failure.

    Review repairs are packet-local. Re-copying the original daily packet after
    a later build failure discards those accepted fixes and wastes the next
    bounded attempt rediscovering them. Carry-forward is allowed only when the
    prior attempt has an actual build-child failure and its current deterministic
    QA receipt passes; otherwise the immutable daily packet remains authoritative.
    """
    attempts = (previous or {}).get("attempts") or []
    if not attempts:
        return default_packet, "daily_packet"
    prior_attempt = Path(str(attempts[-1].get("attempt_dir") or ""))
    candidate = prior_attempt / "packet"
    qa_path = candidate / "07-qa-receipt.json"
    if not candidate.is_dir() or not qa_path.is_file():
        return default_packet, "daily_packet"
    qa = read_json(qa_path)
    build_failures = list((prior_attempt / "child-failures").glob("*-build.json"))
    if qa.get("status") != "PASS" or not build_failures:
        return default_packet, "daily_packet"
    return candidate, "prior_post_review_packet"


def pin_final_attempt_packet_revision(claim: dict[str, Any], attempt: Path) -> str | None:
    """Bind retry decisions to the exact QA packet the attempt finished with."""
    revision = packet_qa_sha256(attempt / "packet")
    attempts = claim.get("attempts") or []
    if not revision or not attempts or not isinstance(attempts[-1], dict):
        return None
    if attempts[-1].get("attempt_dir") != str(attempt):
        return None
    attempts[-1]["final_packet_qa_sha256"] = revision
    claim["source_packet_qa_sha256"] = revision
    return revision


def classify_failure_retry_scope(attempt: Path) -> str:
    """Keep unrelated builder fixes from reopening packet-authority failures."""
    failure_root = attempt / "child-failures"
    if not failure_root.is_dir():
        return UNKNOWN_RETRY_SCOPE
    fragments: list[str] = []
    for path in sorted(failure_root.glob("*")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            fragments.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    corpus = "\n".join(fragments)
    if "rendered_medical_promotional_cta_forbidden" in corpus:
        return IMMUTABLE_ROUTE_GOVERNANCE_RETRY_SCOPE
    if "primary_medical_source_missing" in corpus:
        return PACKET_AUTHORITY_RETRY_SCOPE
    publication_markers = (
        "article_route_master_path_invalid",
        "lastmod_provenance_stale",
        "public_delta_invalid",
        "immutable_base_",
        "candidate_cow_",
    )
    if any(marker in corpus for marker in publication_markers):
        return PUBLICATION_CONTROL_RETRY_SCOPE
    return UNKNOWN_RETRY_SCOPE


def effective_failure_retry_scope(claim: dict[str, Any] | None) -> str:
    """Infer typed scope from immutable child evidence for legacy UNKNOWN claims."""
    scope = str((claim or {}).get("failure_retry_scope") or UNKNOWN_RETRY_SCOPE)
    if scope != UNKNOWN_RETRY_SCOPE:
        return scope
    attempts = (claim or {}).get("attempts") or []
    if not attempts or not isinstance(attempts[-1], dict):
        return UNKNOWN_RETRY_SCOPE
    raw_attempt = attempts[-1].get("attempt_dir")
    if type(raw_attempt) is not str:
        return UNKNOWN_RETRY_SCOPE
    attempt = Path(raw_attempt)
    if not attempt.is_absolute() or attempt.is_symlink():
        return UNKNOWN_RETRY_SCOPE
    try:
        resolved = attempt.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return UNKNOWN_RETRY_SCOPE
    if str(resolved) != raw_attempt or not resolved.is_dir():
        return UNKNOWN_RETRY_SCOPE
    return classify_failure_retry_scope(resolved)


def base_receipt() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "observed_at_utc": utc_now(),
        "pt_date": pt_date(),
        "status": "BLOCKED_UNKNOWN",
        "refreshed": False,
        "published": False,
        "route": None,
        "url": None,
        "topic_id": None,
        "operation_id": None,
        "receipt_path": None,
        "reason": "",
    }


def execute(target_pt_date: str | None = None) -> dict[str, Any]:
    if str(BUSINESS) not in sys.path:
        sys.path.insert(0,str(BUSINESS))
    from ops.glp.seo_business_context import load_business_context
    business_context = load_business_context('article_updater')
    result = base_receipt()
    result['business_context'] = {k: v for k, v in business_context.items() if k != 'text'}
    target_pt_date = validate_target_pt_date(target_pt_date)
    result["target_pt_date"] = target_pt_date
    GLOBAL_LOCK.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(GLOBAL_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result.update({
                "status": "PASS_ALREADY_RUNNING",
                "reason": "another updater webhook owns the global updater lock; no duplicate started",
            })
            return result

        finalized = finalized_claim_reconciliation_candidate()
        if finalized is not None:
            claim = finalized["claim"]
            operation = finalized["operation"]
            attempt = finalized["attempt"]
            plan_path = finalized["plan_path"]
            plan = finalized["plan"]
            authority_row = finalized["authority"]
            reconciled = reconcile_completed_publication(
                claim,
                route=str(authority_row["target_route"]),
                attempt=attempt,
            )
            if reconciled is None:
                raise RuntimeError("finalized_claim_publication_binding_missing")
            live_proof, final_path = reconciled
            if final_path != finalized["final_path"]:
                raise RuntimeError("finalized_claim_publication_path_drift")
            staged_day_receipt = stage_publication_for_health(
                claim,
                operation=operation,
                plan_path=plan_path,
                plan=plan,
                final_path=final_path,
                live_proof=live_proof,
            )
            health = (
                output_health_evaluation(attempt)
                if str(plan.get("as_of") or "") == pt_date()
                else historical_publication_health(
                    plan_path=plan_path,
                    plan=plan,
                    operation=operation,
                    day_receipt_path=staged_day_receipt,
                )
            )
            claim.update({
                "status": "PASS_REFRESHED",
                "quota_consumed": True,
                "publication_receipt": {"path": str(final_path), "sha256": sha(final_path)},
                "live_proof": live_proof,
                "output_health": health,
                "completed_at_utc": utc_now(),
                "updated_at_utc": utc_now(),
            })
            write_claim(operation, claim)
            day_receipt = write_day_receipt(plan_path, plan)
            result.update({
                "status": "PASS_REFRESHED",
                "refreshed": True,
                "published": True,
                "route": authority_row["target_route"],
                "url": SITE + authority_row["target_route"],
                "topic_id": authority_row["topic_id"],
                "operation_id": operation,
                "claim_path": str(finalized["claim_path"]),
                "receipt_path": str(final_path),
                "publication_receipt_sha256": sha(final_path),
                "live_proof": live_proof,
                "output_health": health,
                "quota_after": quota_state(plan),
                "day_receipt": {"path": str(day_receipt), "sha256": sha(day_receipt)},
                "reason": "recovered terminal publication before current-generation plan selection; no effectful step rerun",
            })
            return result

        required_generation = current_release_generation()
        plan_path, plan, authority_row, source_packet = find_due(
            required_generation=required_generation,
            target_pt_date=target_pt_date,
        )
        if plan is None or plan_path is None:
            historical_plans = source_daily_plans(target_pt_date=target_pt_date)
            if historical_plans:
                latest_historical = historical_plans[0]
                result.update({
                    "status": "BLOCKED_CURRENT_GENERATION_PLAN_MISSING",
                    "reason": (
                        "R2 plans exist only for a superseded release generation; "
                        "the registered producer must remint today's plan before publication"
                    ),
                    "required_release_generation": required_generation,
                    "latest_historical_plan": {
                        "path": str(latest_historical),
                        "sha256": sha(latest_historical),
                        "release_generation": read_json(latest_historical).get("release_generation"),
                    },
                })
            else:
                result.update({
                    "status": "PASS_NOTHING_DUE",
                    "reason": "no R2 daily plan exists",
                    "required_release_generation": required_generation,
                })
            return result
        result["daily_plan"] = {"path": str(plan_path), "sha256": sha(plan_path)}
        result["quota_before"] = quota_state(plan)
        if authority_row is None or source_packet is None:
            progress_path = write_day_receipt(plan_path, plan)
            progress = read_json(progress_path)
            result.update({
                "status": (
                    "PASS_DAILY_QUOTA_COMPLETE"
                    if progress["deficit"] == 0
                    else "BLOCKED_NO_READY_PACKET"
                ),
                "quota_after": quota_state(plan),
                "day_receipt": {"path": str(progress_path), "sha256": sha(progress_path)},
                "reason": (
                    "daily quota is complete; duplicate invocation skipped"
                    if progress["deficit"] == 0
                    else "daily quota remains due but no QA-complete packet is ready; author or repair the next primary/substitute"
                ),
            })
            return result

        route = authority_row["target_route"]
        topic = authority_row["topic_id"]
        operation = operation_id(topic, route)
        claim_path = claim_path_for(operation)
        previous = load_claim(operation)
        if previous and previous.get("attempts"):
            prior_attempt = Path(str(previous["attempts"][-1].get("attempt_dir") or ""))
            if prior_attempt.is_dir():
                resume_landed_publication(previous, prior_attempt)
                reconciled = reconcile_completed_publication(
                    previous,
                    route=route,
                    attempt=prior_attempt,
                )
                if reconciled is not None:
                    live_proof, final_path = reconciled
                    staged_day_receipt = stage_publication_for_health(
                        previous,
                        operation=operation,
                        plan_path=plan_path,
                        plan=plan,
                        final_path=final_path,
                        live_proof=live_proof,
                    )
                    health = (
                        output_health_evaluation(prior_attempt)
                        if str(plan.get("as_of") or "") == pt_date()
                        else historical_publication_health(
                            plan_path=plan_path,
                            plan=plan,
                            operation=operation,
                            day_receipt_path=staged_day_receipt,
                        )
                    )
                    previous.update({
                        "status": "PASS_REFRESHED",
                        "quota_consumed": True,
                        "publication_receipt": {"path": str(final_path), "sha256": sha(final_path)},
                        "live_proof": live_proof,
                        "output_health": health,
                        "completed_at_utc": utc_now(),
                        "updated_at_utc": utc_now(),
                    })
                    write_claim(operation, previous)
                    day_receipt = write_day_receipt(plan_path, plan)
                    result.update({
                        "status": "PASS_REFRESHED",
                        "refreshed": True,
                        "published": True,
                        "route": route,
                        "url": SITE + route,
                        "topic_id": topic,
                        "operation_id": operation,
                        "claim_path": str(claim_path),
                        "receipt_path": str(final_path),
                        "publication_receipt_sha256": sha(final_path),
                        "live_proof": live_proof,
                        "output_health": health,
                        "quota_after": quota_state(plan),
                        "day_receipt": {"path": str(day_receipt), "sha256": sha(day_receipt)},
                        "reason": "reconciled prior terminal publication; no effectful step rerun",
                    })
                    return result
                effectful_state, effectful_state_path = pending_effectful_execution(
                    previous, prior_attempt,
                )
                if effectful_state is not None:
                    active = effectful_process_active(effectful_state)
                    result.update({
                        "status": (
                            "BLOCKED_EFFECTFUL_CHILD_IN_PROGRESS"
                            if active else "BLOCKED_EFFECTFUL_CHILD_OUTCOME_AMBIGUOUS"
                        ),
                        "reason": (
                            "the exact review/publish child is still active; duplicate launch refused"
                            if active else
                            "the exact review/publish child stopped without a terminal publication receipt; manual receipt reconciliation required"
                        ),
                        "operation_id": operation,
                        "receipt_path": str(effectful_state_path),
                        "effectful_execution": {
                            "path": str(effectful_state_path),
                            "sha256": sha(effectful_state_path),
                        },
                        "quota_after": quota_state(plan),
                    })
                    return result
        attempt_source_packet, attempt_source_reason = retry_source_packet(previous, source_packet)
        base_state = read_json(r2.BASE_STATE)
        base_tree = str(base_state.get("release_tree_sha256") or "unknown")[:16]
        attempt_root = plan_path.parent / "updater-attempts" / sha_bytes(operation.encode())[:20]
        attempt_number = next_attempt_number(previous, attempt_root)
        attempt = attempt_root / f"attempt-{attempt_number:02d}-{base_tree}"
        attempt.mkdir(parents=True, exist_ok=False)
        os.chmod(attempt, 0o700)
        result.update({
            "route": route,
            "url": SITE + route,
            "topic_id": topic,
            "operation_id": operation,
            "attempt_dir": str(attempt),
            "claim_path": str(claim_path),
        })
        claim = dict(previous or {})
        claim.update({
            "schema": "glp-seo-r2-operation-claim-v1",
            "status": "IN_PROGRESS",
            "operation_id": operation,
            "topic_id": topic,
            "target_route": route,
            "article_id": authority_row["article_id"],
            "source_packet_qa_sha256": packet_qa_sha256(attempt_source_packet),
            "quota_consumed": False,
            "updated_at_utc": utc_now(),
        })
        claim.setdefault("created_at_utc", utc_now())
        claim.setdefault("attempts", []).append({
            "attempt": attempt_number,
            "attempt_dir": str(attempt),
            "base_release_tree_sha256": base_state.get("release_tree_sha256"),
            "source_packet": str(attempt_source_packet),
            "source_packet_reason": attempt_source_reason,
            "source_packet_qa_sha256": packet_qa_sha256(attempt_source_packet),
            "started_at_utc": utc_now(),
        })
        write_claim(operation, claim)
        write_day_receipt(plan_path, plan)

        try:
            collision = assert_no_article_carrier_collision(route, attempt)
            before = live_snapshot(route, attempt / "live", "live-before")
            if before["http_status"] != 200 or before["sitemap_http_status"] != 200 or not before["lastmod"]:
                raise RuntimeError(f"live_before_readback_invalid:{before}")

            as_of = dt.date.fromisoformat(str(plan["as_of"]))
            fresh_authority, fresh_authority_path, fresh_selection, fresh_selection_path, seam = current_authority(
                authority_row,
                as_of=as_of,
                output_dir=attempt,
            )
            packet = attempt / "packet"
            rebind_receipt = rebind_packet(
                attempt_source_packet,
                packet,
                fresh_authority,
                fresh_authority_path,
                fresh_selection,
                fresh_selection_path,
                attempt,
            )
            run_id = f"{topic}-webhook-{stamp()}"
            # target_pt_date selects retained plan/quota identity, not a release
            # clock. A newer accepted base can contain genuine material edits
            # after that plan day; preserve those dates and use today's PT day
            # for publication admission. This does not refresh source evidence.
            publication_date = pt_date()
            release_root = RELEASES / f"glp-r2-updater-{stamp()}-{sha_bytes(operation.encode())[:10]}"
            _rc, materialized, materialize_execution = run_child(
                "materialize",
                [
                    str(PYTHON), str(MATERIALIZER),
                    "--packet", str(packet),
                    "--topic-id", topic,
                    "--route", route,
                    "--run-id", run_id,
                    "--release-root", str(release_root),
                    "--publication-date", publication_date,
                    "--selection-receipt", str(fresh_selection_path),
                    "--r2-authority", str(fresh_authority_path),
                    "--operation", "r2_refresh",
                    "--materialization-reason", "r2_refresh",
                    "--review-transport", REVIEW_TRANSPORT,
                    "--pt-date", publication_date,
                ],
                cwd=BUSINESS,
                output_dir=attempt / "children",
                timeout=600,
            )
            if not materialized or materialized.get("status") != "PASS_OPUS_RUN_MATERIALIZED":
                raise RuntimeError(f"materializer_receipt_invalid:{materialized}")
            review_job = Path(materialized["review_job"])
            _rc, reviewed, review_execution = run_effectful_child(
                "review-and-publish",
                [str(PYTHON), str(REVIEW_LOOP), "--job", str(review_job), "--apply"],
                cwd=BUSINESS,
                output_dir=attempt / "children",
            )
            if not reviewed or reviewed.get("status") != "PASS_OPUS_SEO_DRAFT":
                raise RuntimeError(f"review_closeout_invalid:{reviewed}")
            final_path = packet.parent / f"publication-{run_id}" / "10-final-publication-receipt.json"
            final = read_json(final_path)
            publication_binding = terminal_publication_binding(
                final_path,
                final,
                route=route,
                topic_id=topic,
                article_id=str(authority_row["article_id"]),
            )
            live_proof = verify_live_delta(route, before, attempt / "live")
            live_proof["publication_binding"] = publication_binding
            pin_final_attempt_packet_revision(claim, attempt)
            # Publish is terminal and live-proved. Persist the domain output
            # before the fleet sensor evaluates this machine; quota remains
            # unconsumed until that health row passes.
            staged_day_receipt = stage_publication_for_health(
                claim,
                operation=operation,
                plan_path=plan_path,
                plan=plan,
                final_path=final_path,
                live_proof=live_proof,
            )
            health = (
                output_health_evaluation(attempt)
                if str(plan.get("as_of") or "") == pt_date()
                else historical_publication_health(
                    plan_path=plan_path,
                    plan=plan,
                    operation=operation,
                    day_receipt_path=staged_day_receipt,
                )
            )
            claim.update({
                "status": "PASS_REFRESHED",
                "quota_consumed": True,
                "completed_at_utc": utc_now(),
                "output_health": health,
                "updated_at_utc": utc_now(),
            })
            write_claim(operation, claim)
            day_receipt = write_day_receipt(plan_path, plan)
            result.update({
                "status": "PASS_REFRESHED",
                "refreshed": True,
                "published": True,
                "reason": f"refreshed {SITE + route}; live content and sitemap lastmod advanced",
                "receipt_path": str(final_path),
                "publication_receipt_sha256": sha(final_path),
                "live_proof": live_proof,
                "quota_after": quota_state(plan),
                "collision_proof": collision,
                "prioritization_seam": seam,
                "packet_rebind_receipt": {"path": str(rebind_receipt), "sha256": sha(rebind_receipt)},
                "materialize_execution": str(materialize_execution),
                "review_execution": str(review_execution),
                "output_health": health,
                "day_receipt": {"path": str(day_receipt), "sha256": sha(day_receipt)},
            })
            terminal = attempt / "updater-result.json"
            atomic_json(terminal, result)
            result["updater_result"] = {"path": str(terminal), "sha256": sha(terminal)}
            return result
        except Exception as error:
            failure_retry_scope = classify_failure_retry_scope(attempt)
            identity = failure_identity(error)
            failure = {
                "schema": "glp-seo-r2-updater-failure-v1",
                "status": "BLOCKED_UPDATE_FAILED",
                "observed_at_utc": utc_now(),
                "operation_id": operation,
                "topic_id": topic,
                "target_route": route,
                "attempt_dir": str(attempt),
                "error_class": type(error).__name__,
                "error": str(error),
                "failure_cause": identity["failure_cause"],
                "quota_before": result["quota_before"],
                "quota_after": quota_state(plan),
                "quota_consumed": False,
                "failure_retry_scope": failure_retry_scope,
            }
            FAILURE_ROOT.mkdir(parents=True, exist_ok=True)
            failure_path = FAILURE_ROOT / f"{stamp()}-{sha_bytes(operation.encode())[:16]}.json"
            atomic_json(failure_path, failure)
            pin_final_attempt_packet_revision(claim, attempt)
            claim.update({
                "status": "FAILED_RETRYABLE",
                "quota_consumed": False,
                "failure_retry_scope": failure_retry_scope,
                "last_failure": {"path": str(failure_path), "sha256": sha(failure_path)},
                "updated_at_utc": utc_now(),
            })
            write_claim(operation, claim)
            day_receipt = write_day_receipt(plan_path, plan)
            result.update({
                "status": "BLOCKED_UPDATE_FAILED",
                **identity,
                "receipt_path": str(failure_path),
                "failure_receipt_sha256": sha(failure_path),
                "quota_after": quota_state(plan),
                "day_receipt": {"path": str(day_receipt), "sha256": sha(day_receipt)},
            })
            return result
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def current_review_pool(pacing, *, now=None):
    """Fresh active Codex quota observation; not an account-selection override.

    Codex reports named windows, not Claude's session_left_pct. Missing windows
    and known depleted windows refuse; unknown is never converted to capacity.
    The subscription wrapper still owns actual inference admission/no fallback.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    pools = [p for p in pacing.get('pools', [])
             if p.get('provider') == 'openai-codex' and p.get('role') == 'ACTIVE']
    if len(pools) != 1:
        raise RuntimeError('review_pool_unknown')
    pool = pools[0]
    try:
        fetched = dt.datetime.fromisoformat(str(pool.get('fetched_at') or '').replace('Z', '+00:00'))
        age = (now - fetched).total_seconds()
        windows = pool.get('windows') or {}
        remaining = [w.get('remaining_pct') for w in windows.values()]
        valid = (pool.get('freshness') == 'live' and 0 <= age <= 7200
                 and bool(remaining) and all(type(value) in (int, float) and 0 < value <= 100 for value in remaining))
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise RuntimeError('review_pool_no_fresh_headroom')
    return pool


def execute_review_only(request_path: str, request_sha256: str) -> dict[str, Any]:
    """One hash-bound advisory review; never enter update/reconcile/publish code."""
    path = Path(request_path)
    if (not path.is_absolute() or path.is_symlink()
            or not path.resolve().is_relative_to(REVIEW_REQUEST_ROOT.resolve())):
        raise RuntimeError("review_request_path_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", request_sha256) or sha(path) != request_sha256:
        raise RuntimeError("review_request_hash_invalid")
    request = read_json(path)
    if request.get("schema") != "glp-r2-review-only-request-v1":
        raise RuntimeError("review_request_contract_invalid")
    job_path = Path(str(request.get("source_job") or ""))
    failure_path = Path(str(request.get("failure_receipt") or ""))
    if (not job_path.resolve().is_relative_to(R2_ROOT.resolve())
            or not failure_path.resolve().is_relative_to(FAILURE_ROOT.resolve())
            or sha(job_path) != request.get("source_job_sha256")
            or sha(failure_path) != request.get("failure_receipt_sha256")):
        raise RuntimeError("review_source_binding_invalid")
    job = read_json(job_path)
    failure = read_json(failure_path)
    operation = operation_id(str(job.get("topic_id") or ""), str(job.get("target_route") or ""))
    if (operation != request.get("operation_id") or operation != failure.get("operation_id")
            or failure.get("status") != "BLOCKED_UPDATE_FAILED"
            or job_path.parent != Path(str(failure.get("attempt_dir") or ""))):
        raise RuntimeError("review_operation_binding_invalid")
    packet = Path(job["packet_dir"]).resolve()
    subjects = job.get("subject_files") or []
    if (not subjects or any(Path(rel).is_absolute() or '..' in Path(rel).parts for rel in subjects)
            or any(not (packet / rel).resolve().is_relative_to(packet) for rel in subjects)):
        raise RuntimeError("review_subject_paths_invalid")
    before = {rel: sha(packet / rel) for rel in subjects}
    if before != request.get("subject_sha256"):
        raise RuntimeError("review_subject_binding_invalid")
    transport = review_transports.transport(REVIEW_TRANSPORT)
    pacing = read_json(BUSINESS / "ops/subscription-capacity/PACING.json")
    pool = current_review_pool(pacing)
    GLOBAL_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with GLOBAL_LOCK.open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        output = path.parent / (request_sha256 + '-review')
        terminal = output / 'receipt.json'
        if terminal.exists():
            return read_json(terminal)
        if output.exists():
            raise RuntimeError("review_prior_attempt_requires_reconciliation")
        output.mkdir(mode=0o700)
        result = base_receipt()
        result.update({"review_only": True, "operation_id": operation,
                       "request_sha256": request_sha256, "source_job": str(job_path),
                       "subject_sha256": before, "receipt_path": str(terminal),
                       "pool": pool, "transport_id": transport['transport_id'],
                       "model": transport['model'], "effort": transport['effort'],
                       "BLOCKED_NO_READY_PACKET_cleared": False})
        atomic_json(output / 'started.json', result)
        try:
            # The deterministic validator regenerates QA/article derivatives.
            # Isolate those writes; original failed-attempt evidence is immutable.
            copied_packet = output / 'packet'
            shutil.copytree(packet, copied_packet, symlinks=True)
            if any(p.is_symlink() for p in copied_packet.rglob('*')):
                raise RuntimeError('review_packet_symlink_forbidden')
            job['packet_dir'] = str(copied_packet)
            job['validator']['argv'] = [
                str(copied_packet) + arg[len(str(packet)):]
                if arg == str(packet) or arg.startswith(str(packet) + '/') else arg
                for arg in job['validator']['argv']
            ]
            result['review_packet'] = str(copied_packet)
            # Only transport, history, and bounded round controls change. All
            # content, selection, validator and publication pins stay intact.
            job.update({"review_transport": transport['transport_id'],
                        "review_wrapper_sha256": sha(Path(transport['wrapper'])),
                        "history_dir": str(output / 'history'), "max_rounds": 1})
            successor = output / 'review-job.json'
            atomic_json(successor, job)
            rc, reviewed, execution = run_child(
                'review-only', [str(PYTHON), str(REVIEW_LOOP), '--job', str(successor),
                                '--review-only-once'],
                cwd=BUSINESS, output_dir=output / 'children', timeout=1300)
            if not reviewed or reviewed.get('publication_invoked') is not False:
                raise RuntimeError('review_only_closeout_invalid')
            result.update({"status": "PASS_REVIEW_ONLY" if reviewed.get('status') == 'PASS_OPUS_SEO_DRAFT'
                           else "BLOCKED_REVIEW_ONLY", "review": reviewed,
                           "review_execution": str(execution),
                           "reason": "Advisory review only; current-base packet admission and publication remain separate."})
        except Exception as error:
            result.update({"status": "BLOCKED_REVIEW_ONLY", "reason": str(error),
                           "failure_cause": f"{type(error).__name__}:{error}"})
        after = {rel: sha(packet / rel) for rel in subjects}
        result['subject_bytes_unchanged'] = before == after
        if before != after:
            result.update({"status": "BLOCKED_REVIEW_SUBJECT_DRIFT", "reason": "subject bytes changed during review"})
        atomic_json(terminal, result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pt-date")
    parser.add_argument("--review-request")
    parser.add_argument("--review-request-sha256")
    args = parser.parse_args()
    try:
        if args.review_request or args.review_request_sha256:
            if args.pt_date or not args.review_request or not args.review_request_sha256:
                raise RuntimeError("review_only_args_invalid")
            receipt = execute_review_only(args.review_request, args.review_request_sha256)
        else:
            receipt = execute(args.pt_date)
    except Exception as error:  # outer boundary must still emit one parseable receipt
        receipt = base_receipt()
        receipt.update({
            "status": "BLOCKED_ADAPTER_ERROR",
            "reason": f"{type(error).__name__}: {error}",
        })
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
