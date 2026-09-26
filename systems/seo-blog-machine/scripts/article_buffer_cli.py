#!/usr/bin/env python3
"""Operational A3 article-buffer status, admission, and guarded-remint consume."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from article_buffer import (
    BufferEmptyError,
    PermanentArticleRejection,
    ROUTE_PATTERN,
    admit,
    consume_oldest,
    current_publication_truth,
    missing_inbound_source_routes,
    pool_status,
    pt_date_now,
    ready_entries,
    recycle_stale_claims,
    refill_base_status,
    stale_claims,
)
from machine_config import load_config

BUSINESS = Path("/home/ubuntu/business")
SYSTEM = BUSINESS / "systems/seo-blog-machine"
CONFIG = SYSTEM / "config/verticals/glp-clinic-scout.json"
POINTER = BUSINESS / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
PUBLISHED_LEDGER = BUSINESS / "reports/glp-clinic-scout/seo-machine/published-topic-ledger.json"
DEFAULT_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/article-buffer"
DEFAULT_WORK_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/article-buffer-work"
RELEASES = BUSINESS / "site-system/flagship-v2/releases"
PYTHON = Path("/usr/bin/python3.12")
CATALOGS = ("data/blog/content-batch.json", "data/blog/content-national-wow-additions.json")
CLI_RECEIPT_SCHEMA = "glp-seo-article-buffer-cli-receipt-v1"
ARTICLE_GATE_PATTERN = re.compile(
    r"blog_article_gate:([a-z0-9][a-z0-9-]*)(?::([a-z0-9_]+))?"
)
INBOUND_LINK_SOURCE_PATTERN = re.compile(
    r"inbound_link_source_missing:(/(?:guides|best)/[a-z0-9-]+/)"
)


class ChildProcessFailure(RuntimeError):
    def __init__(self, argv: list[str], returncode: int, stdout: str, stderr: str) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        child = Path(argv[1]).name if len(argv) > 1 else Path(argv[0]).name
        super().__init__(
            f"child_failed:{child}:{returncode}:{stderr[-1500:]}:{stdout[-1500:]}"
        )


class CliArgumentError(ValueError):
    pass


class JsonReceiptArgumentParser(argparse.ArgumentParser):
    """Turn invalid invocations into the same structured failure path."""

    def error(self, message: str) -> None:
        raise CliArgumentError(f"argument_error:{message}")


CONTROL_FILES = (
    SYSTEM / "scripts/article_buffer.py",
    SYSTEM / "scripts/article_buffer_cli.py",
    SYSTEM / "scripts/carrier_lock.py",
    SYSTEM / "scripts/materialize_opus_run.py",
    SYSTEM / "scripts/selection_receipt.py",
    SYSTEM / "scripts/validate_opus_article_packet.py",
    SYSTEM / "scripts/deterministic_qa_repair.py",
    SYSTEM / "scripts/r2_update_carrier.py",
    SYSTEM / "scripts/compile_review_packet_article.py",
    SYSTEM / "scripts/opus_review_loop.py",
    SYSTEM / "scripts/apply_opus_article_release.py",
    SYSTEM / "scripts/build_opus_article_release.py",
    SYSTEM / "scripts/publish_opus_article_release.py",
    SYSTEM / "scripts/deploy_opus_article_engine.py",
    SYSTEM / "scripts/current_content_base.py",
    SYSTEM / "scripts/autonomous_release_gate.py",
    SYSTEM / "config/verticals/glp-clinic-scout.json",
    SYSTEM / "pipeline-contract.json",
    SYSTEM / "schemas/glp-opus-seo-review-v1.schema.json",
    BUSINESS / "scripts/hermes_sol_article_review.py",
    BUSINESS / "scripts/site_tree_materializer.py",
    BUSINESS / "reports/glp-clinic-scout/seo-machine/OPUS-REVIEW-PATH-20260802.md",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def control_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for path in CONTROL_FILES:
        source = path.resolve(strict=True)
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"buffer_control_invalid:{source}")
        pins[str(source)] = sha(source)
    return pins


def post_deploy_resume_job(materialized: dict[str, Any]) -> Path | None:
    """Resolve an exact deployed generation for publisher-only continuation.

    The deployment already happened, so replaying apply/build against the old
    pointer is both impossible and wrong. This local preflight is intentionally
    strict; the publisher independently revalidates these receipts, the live
    canonical Cloudflare head, and the target bytes before any truth advances.
    """
    spec = materialized.get("apply_template")
    if not isinstance(spec, dict) or set(spec) < {"path", "sha256"}:
        return None
    template_path = Path(str(spec.get("path") or "")).resolve()
    if (
        not template_path.is_file()
        or template_path.is_symlink()
        or sha(template_path) != spec.get("sha256")
    ):
        raise RuntimeError("post_deploy_resume_template_pin_invalid")
    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("post_deploy_resume_template_invalid") from error
    raw_report = template.get("report_dir")
    raw_release = template.get("release_root")
    if not isinstance(raw_report, str) or not isinstance(raw_release, str):
        return None
    report = Path(raw_report).resolve()
    release = Path(raw_release).resolve()
    if raw_report != str(report) or raw_release != str(release):
        raise RuntimeError("post_deploy_resume_path_alias")
    final = report / "10-final-publication-receipt.json"
    deployment_paths = [
        report / name
        for name in ("06-deployment-receipt.json", "06b-deployment-reconciliation.json")
        if (report / name).exists()
    ]
    required = [
        report / "01-deployment-manifest.json",
        report / "02-immutable-release-seal.json",
        report / "07-live-byte-verification.json",
        report / "08-cache-purge-receipt.json",
        report / "release-job.json",
    ]
    effect_markers = [
        *deployment_paths,
        report / "07-live-byte-verification.json",
        report / "08-cache-purge-receipt.json",
    ]
    if not any(path.exists() for path in effect_markers):
        return None
    if final.exists():
        return None
    if (
        len(deployment_paths) not in {1, 2}
        or not report.is_dir()
        or report.is_symlink()
        or not release.is_dir()
        or release.is_symlink()
        or any(not path.is_file() or path.is_symlink() for path in [*deployment_paths, *required])
    ):
        raise RuntimeError("post_deploy_resume_receipts_incomplete")
    try:
        job = json.loads((report / "release-job.json").read_text(encoding="utf-8"))
        manifest_path = report / "01-deployment-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        deployment = json.loads(deployment_paths[-1].read_text(encoding="utf-8"))
        live = json.loads((report / "07-live-byte-verification.json").read_text(encoding="utf-8"))
        purge = json.loads((report / "08-cache-purge-receipt.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("post_deploy_resume_receipts_unreadable") from error
    bindings = {
        "operation": template.get("operation", "new_article"),
        "manifest_id": template.get("manifest_id"),
        "topic_id": template.get("topic_id"),
        "target_query": template.get("target_query"),
        "article_id": template.get("article_id"),
        "target_route": template.get("target_route"),
        "publication_date": template.get("publication_date"),
        "release_root": raw_release,
        "report_dir": raw_report,
        "base_state_path": template.get("base_state_path"),
        "base_state_sha256": template.get("base_state_sha256"),
        "published_ledger_path": template.get("published_ledger_path"),
        "published_ledger_sha256": template.get("published_ledger_sha256"),
    }
    if (
        not isinstance(job, dict)
        or job.get("schema") != "glp-opus-article-release-job-v1"
        or any(job.get(field) != value for field, value in bindings.items())
        or materialized.get("release_root") != raw_release
        or manifest.get("manifest_id") != job.get("manifest_id")
        or manifest.get("article_id") != job.get("article_id")
        or manifest.get("release_root") != raw_release
        or manifest.get("rollback_release_root") != job.get("base_release_root")
    ):
        raise RuntimeError("post_deploy_resume_operation_binding_invalid")
    deployment_id = deployment.get("deployment_id")
    release_tree = manifest.get("release_tree_sha256")
    manifest_sha = sha(manifest_path)
    if (
        deployment.get("status") != "PASS_LIVE"
        or not isinstance(deployment_id, str)
        or not deployment_id
        or deployment.get("release_tree_sha256") != release_tree
        or deployment.get("manifest_sha256") not in (None, manifest_sha)
        or live.get("status") != "PASS_LIVE"
        or live.get("deployment_id") != deployment_id
        or live.get("release_tree_sha256") != release_tree
        or purge.get("status") != "PASS_PURGED_TOKEN_REVOKED"
        or purge.get("deployment_id") != deployment_id
        or purge.get("manifest_sha256") != manifest_sha
    ):
        raise RuntimeError("post_deploy_resume_chain_invalid")
    return (report / "release-job.json").resolve()


def current_context() -> tuple[dict[str, Any], Path, set[str], set[str]]:
    config = load_config(CONFIG)
    resolution = config["_current_content_base_resolution"]
    release = Path(resolution["pointer"]["release_root"]).resolve(strict=True)
    routes, topics = current_publication_truth(
        release_root=release,
        catalog_relpaths=CATALOGS,
        published_ledger=PUBLISHED_LEDGER,
    )
    return resolution, release, routes, topics


def current_article_routes(release: Path) -> set[str]:
    """Catalog routes with a regular served page the builder can actually mutate."""
    source_routes: set[str] = set()
    for relative in CATALOGS:
        payload = json.loads((release / relative).read_text(encoding="utf-8"))
        rows = payload.get("articles")
        if not isinstance(rows, list):
            raise RuntimeError(f"buffer_article_catalog_invalid:{relative}")
        for row in rows:
            route = str(row.get("canonical_path") or "") if isinstance(row, dict) else ""
            if ROUTE_PATTERN.fullmatch(route) is None:
                continue
            public_path = release / "public" / route.strip("/") / "index.html"
            if public_path.is_file() and not public_path.is_symlink():
                source_routes.add(route)
    return source_routes


def inbound_source_rejection(
    packet_work: Path,
    release: Path,
    entry: dict[str, Any],
) -> dict[str, str] | None:
    """Name admitted packets whose planned source cannot exist in this build."""
    missing = missing_inbound_source_routes(
        packet_work,
        current_article_routes(release),
        expected_article_id=str(entry.get("article_id") or ""),
    )
    if not missing:
        return None
    return {
        "reason": f"inbound_link_source_missing:{missing[0]}",
        "article_id": str(entry["article_id"]),
        "diagnostic": "inbound_link_sources_absent_from_current_article_catalogs:" + ",".join(missing),
    }


def json_from_output(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"child_json_missing:{output[-1200:]}")


def run_child(argv: list[str], *, timeout: int, env: dict[str, str] | None = None,
              pass_fd: int | None = None) -> dict[str, Any]:
    result = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
        pass_fds=(pass_fd,) if pass_fd is not None else (),
    )
    if result.returncode:
        raise ChildProcessFailure(
            argv,
            result.returncode,
            result.stdout,
            result.stderr,
        )
    return json_from_output(result.stdout)


def permanent_candidate_rejection(
    error: ChildProcessFailure,
    materialized: dict[str, Any],
) -> PermanentArticleRejection | None:
    """Classify only failures proven local to one immutable article packet."""
    diagnostic = "\n".join((error.stderr, error.stdout, str(error)))
    match = ARTICLE_GATE_PATTERN.search(diagnostic)
    if match is not None:
        return PermanentArticleRejection(
            match.group(2) or "blog_article_gate_rejected_without_reason",
            article_id=match.group(1),
            diagnostic=diagnostic,
        )

    source_match = INBOUND_LINK_SOURCE_PATTERN.search(diagnostic)
    if source_match is None:
        return None
    template_path = Path(str((materialized.get("apply_template") or {}).get("path") or ""))
    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    article_id = template.get("article_id")
    if not isinstance(article_id, str) or not article_id:
        return None
    return PermanentArticleRejection(
        f"inbound_link_source_missing:{source_match.group(1)}",
        article_id=article_id,
        diagnostic=diagnostic,
    )


def recycle_cli(args: argparse.Namespace) -> dict[str, Any]:
    """Retire prior-PT-day claims that never produced a consumption.

    This is a WRITE lane and is deliberately separate from ``status``: the dry
    run must stay read-only. ``scripts/article_carrier_run.py`` calls this
    before its pre-flight, because the pre-flight itself is what turned a stale
    claim into ``BLOCKED_BUFFER_EMPTY`` on 2026-08-20 — consume was never even
    reached, so an in-consume recycle alone would never have fired.
    """
    result = recycle_stale_claims(
        root=args.root,
        work_root=args.work_root,
        base_state=POINTER,
        today_pt=args.pt_date,
    )
    return {**result, "pool": status(args.root)["pool"]}


def status(root: Path) -> dict[str, Any]:
    resolution, release, routes, topics = current_context()
    mutable_routes = current_article_routes(release)
    pool = pool_status(
        root,
        current_routes=routes,
        current_topic_ids=topics,
        mutable_article_routes=mutable_routes,
    )
    refill_trigger = refill_base_status(DEFAULT_WORK_ROOT, POINTER)
    pool["refill_trigger"] = refill_trigger
    held = stale_claims(root, today_pt=pt_date_now())
    raw_entries = ready_entries(root, current_routes=routes, current_topic_ids=topics)
    valid_entries = ready_entries(
        root,
        current_routes=routes,
        current_topic_ids=topics,
        mutable_article_routes=mutable_routes,
    )
    valid_ids = {entry["entry_id"] for _path, entry in valid_entries}
    invalid_entries = []
    for path, entry in raw_entries:
        if entry["entry_id"] in valid_ids:
            continue
        missing = missing_inbound_source_routes(
            path.parent / "packet",
            mutable_routes,
            expected_article_id=str(entry["article_id"]),
        )
        invalid_entries.append({
            "entry_id": entry["entry_id"],
            "topic_id": entry["topic_id"],
            "target_route": entry["target_route"],
            "reason": (
                f"inbound_link_source_missing:{missing[0]}"
                if missing else "admission_precheck_failed"
            ),
        })
    entries = [
        {
            "manifest": str(path),
            "entry_id": entry["entry_id"],
            "topic_id": entry["topic_id"],
            "target_route": entry["target_route"],
            "admitted_at_utc": entry["admitted_at_utc"],
        }
        for path, entry in valid_entries
    ]
    return {
        "status": "PASS_BUFFER_STATUS" if pool["at_or_above_minimum"] else "BUFFER_CAPACITY_DEFICIT",
        "pool": pool,
        "entries": entries,
        "raw_ready": len(raw_entries),
        "invalid_entries": invalid_entries,
        # Read-only visibility: entries a dead prior-day run is still holding.
        # `recycle` retires these; status only reports them.
        "stale_claims": held,
        "stale_claim_count": len(held),
        "current_content_base": resolution["state"],
        "refill_trigger": refill_trigger,
        "release_root": str(release),
    }


def admit_cli(args: argparse.Namespace) -> dict[str, Any]:
    _resolution, release, routes, topics = current_context()
    timestamp = args.admitted_at_utc or dt.datetime.now(dt.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    manifest = admit(
        root=args.root,
        packet=args.packet,
        pass_receipt=args.pass_receipt,
        control_files=control_pins(),
        current_routes=routes,
        current_topic_ids=topics,
        mutable_article_routes=current_article_routes(release),
        admitted_at_utc=timestamp,
    )
    return {"status": "PASS_BUFFER_ADMITTED", "manifest": str(manifest), "pool": status(args.root)["pool"]}


def consume_cli(args: argparse.Namespace) -> dict[str, Any]:
    _resolution, release, routes, topics = current_context()
    pt_date = args.pt_date or dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    operation_digest = hashlib.sha256(args.operation_id.encode()).hexdigest()[:16]

    def truth() -> tuple[set[str], set[str]]:
        _r, release, live_routes, live_topics = current_context()
        if not release.is_dir():
            raise RuntimeError("buffer_current_release_missing")
        return live_routes, live_topics

    def rematerialize(
        packet_work: Path,
        entry: dict[str, Any],
        base_spec: dict[str, str],
        operation_id: str,
        generation: int = 0,
    ) -> dict[str, Any]:
        # Generation 0 keeps the historical names byte-for-byte. A regeneration
        # (control-pin drift on the resume path) must NOT reuse them: the
        # materializer refuses to rewrite an existing apply template
        # (materialized_job_drift) and refuses an existing release root
        # (release_root_exists). A suffixed generation gives the new plan its
        # own run id, report dir, and candidate root.
        suffix = "" if not generation else f"-r{int(generation)}"
        run_id = f"buffer-{operation_digest}{suffix}"
        release_root = RELEASES / f"glp-buffer-{pt_date}-{operation_digest}{suffix}"
        rejection = inbound_source_rejection(packet_work, release, entry)
        if rejection is not None:
            return {
                "operation": "new_article",
                "base_state_sha256": base_spec["sha256"],
                "pass_receipt": entry["pass_receipt"],
                "permanent_rejection": rejection,
            }
        result = run_child([
            str(PYTHON), str(SYSTEM / "scripts/materialize_opus_run.py"),
            "--packet", str(packet_work),
            "--topic-id", entry["topic_id"],
            "--route", entry["target_route"],
            "--run-id", run_id,
            "--release-root", str(release_root),
            "--publication-date", pt_date,
            "--selection-receipt", entry["selection_receipt"]["path"],
            "--topic-authority-snapshot", entry["topic_authority"]["path"],
            "--materialization-reason", "remint",
            "--pt-date", pt_date,
        ], timeout=900)
        if result.get("status") != "PASS_OPUS_RUN_MATERIALIZED":
            raise RuntimeError("buffer_rematerialization_not_pass")
        template_path = Path(result["apply_template"]).resolve(strict=True)
        template = json.loads(template_path.read_text(encoding="utf-8"))
        if template.get("base_state_sha256") != base_spec["sha256"]:
            raise RuntimeError("buffer_rematerialization_current_base_mismatch")
        return {
            "operation": template.get("operation", "new_article"),
            "base_state_sha256": template["base_state_sha256"],
            "apply_template": {"path": str(template_path), "sha256": sha(template_path)},
            "review_job": {"path": result["review_job"], "sha256": result["review_job_sha256"]},
            "release_root": str(release_root),
            "pass_receipt": entry["pass_receipt"],
        }

    def publish(materialized: dict[str, Any], operation_id: str, lock_fd: int) -> dict[str, Any]:
        env = dict(os.environ)
        env["SEO_OPUS_PASS_RECEIPT"] = materialized["pass_receipt"]["path"]
        resume_job = post_deploy_resume_job(materialized)
        argv = (
            [
                str(PYTHON), str(SYSTEM / "scripts/publish_opus_article_release.py"),
                "--job", str(resume_job),
            ]
            if resume_job is not None
            else [
                str(PYTHON), str(SYSTEM / "scripts/apply_opus_article_release.py"),
                "--template", materialized["apply_template"]["path"],
            ]
        )
        try:
            result = run_child(argv, timeout=7200, env=env, pass_fd=lock_fd)
        except ChildProcessFailure as error:
            rejection = (
                permanent_candidate_rejection(error, materialized)
                if resume_job is None
                else None
            )
            if rejection is not None:
                raise rejection from error
            raise
        if result.get("status") != "PASS_LIVE_GSC_READBACK_PASS":
            raise RuntimeError("buffer_publish_not_terminal_pass")
        receipt = Path(result["receipt"]).resolve(strict=True)
        result["receipt_sha256"] = sha(receipt)
        result["status"] = "PASS_BUFFER_PUBLISHED"
        return result

    result = consume_oldest(
        root=args.root,
        operation_id=args.operation_id,
        current_routes=routes,
        current_topic_ids=topics,
        base_state=POINTER,
        work_root=args.work_root,
        rematerialize=rematerialize,
        publish=publish,
        current_truth=truth,
    )
    return result


def reconcile_cli(args: argparse.Namespace) -> dict[str, Any]:
    """Close a completed, still-live claim without invoking any publish step."""
    import article_buffer as buffer
    import publish_opus_article_release as publisher
    from carrier_lock import carrier_lock
    operation_hash = hashlib.sha256(args.operation_id.encode()).hexdigest()
    claim_path = args.root.resolve() / "claims" / (operation_hash + ".json")
    claim = buffer._read_sealed(claim_path, buffer.CLAIM_SCHEMA, "claim_sha256")
    if claim.get("operation_id") != args.operation_id:
        raise RuntimeError("buffer_claim_operation_mismatch")
    entry_path = buffer._verify_spec(claim["entry_manifest"], "buffer_claim_entry")
    entry = buffer.validate_entry(entry_path, current_routes=set(), current_topic_ids=set())
    if any(claim.get(k) != entry.get(k) for k in ("entry_id", "topic_id", "target_route")):
        raise RuntimeError("buffer_completed_claim_entry_drift")
    workspace = args.work_root.resolve() / operation_hash
    lineage = buffer.validate_completed_generation_lineage(workspace, claim)
    materialized = lineage[-1]["materialized"]
    template_path = buffer._verify_spec(materialized["apply_template"], "buffer_completed_apply_template")
    template = json.loads(template_path.read_text())
    report = Path(template["report_dir"]).resolve()
    if not report.is_relative_to(workspace) or report.is_symlink():
        raise RuntimeError("buffer_completed_report_outside_workspace")
    job = json.loads((report / "release-job.json").read_text())
    if any(job.get(k) != template.get(k) for k in (
        "manifest_id", "topic_id", "article_id", "target_route", "release_root", "base_state_sha256",
        "base_state_path", "published_ledger_path", "published_ledger_sha256")):
        raise RuntimeError("buffer_completed_job_template_drift")
    if job["base_state_path"] != str(POINTER) or job["published_ledger_path"] != str(PUBLISHED_LEDGER):
        raise RuntimeError("buffer_completed_writer_target_invalid")
    with carrier_lock():
        result = publisher.reconcile_final_job(job, verify_only=args.verify_only)
        if not args.verify_only:
            consumed_path = args.root.resolve() / "consumed" / (operation_hash + ".json")
            if consumed_path.exists():
                consumed = buffer._read_sealed(consumed_path, buffer.CONSUME_SCHEMA, "consumption_sha256")
            else:
                consumed = buffer._recover_completed_consumption(
                    root=args.root, operation_id=args.operation_id, base_state=POINTER,
                    work_root=args.work_root, consumed_path=consumed_path)
            if not consumed or consumed.get("operation_id") != args.operation_id:
                raise RuntimeError("buffer_completed_consumption_not_recovered")
            result["consumption"] = buffer._spec(consumed_path)
    result.update(operation_id=args.operation_id, generation=lineage[-1].get("generation", 0),
                  generation_count=len(lineage), target_route=entry["target_route"])
    return result


def build_parser() -> argparse.ArgumentParser:
    result = JsonReceiptArgumentParser()
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("status", "admit", "consume", "recycle", "reconcile"):
        item = sub.add_parser(name)
        item.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    admit_parser = sub.choices["admit"]
    admit_parser.add_argument("--packet", type=Path, required=True)
    admit_parser.add_argument("--pass-receipt", type=Path, required=True)
    admit_parser.add_argument("--admitted-at-utc")
    consume_parser = sub.choices["consume"]
    consume_parser.add_argument("--operation-id", required=True)
    consume_parser.add_argument("--pt-date")
    consume_parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    recycle_parser = sub.choices["recycle"]
    recycle_parser.add_argument("--pt-date")
    recycle_parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    reconcile_parser = sub.choices["reconcile"]
    reconcile_parser.add_argument("--operation-id", required=True)
    reconcile_parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    reconcile_parser.add_argument("--verify-only", action="store_true")
    return result


def main() -> int:
    args = None
    try:
        args = build_parser().parse_args()
        if args.command == "status":
            result = status(args.root)
        elif args.command == "admit":
            result = admit_cli(args)
        elif args.command == "recycle":
            result = recycle_cli(args)
        elif args.command == "reconcile":
            result = reconcile_cli(args)
        else:
            result = consume_cli(args)
        print(json.dumps({"schema": CLI_RECEIPT_SCHEMA, **result}, sort_keys=True))
        return 0
    except BufferEmptyError as error:
        print(json.dumps({
            "schema": CLI_RECEIPT_SCHEMA,
            "status": "NOTHING_DUE_BUFFER_EMPTY",
            "command": args.command,
            "operation_id": getattr(args, "operation_id", None),
            "reason": str(error),
            "quarantined": getattr(error, "quarantined", []),
        }, sort_keys=True))
        return 0
    except Exception as error:  # noqa: BLE001 - every exit needs a machine receipt
        traceback.print_exc(file=sys.stderr)
        exception_traceback = traceback.format_exc()
        command = getattr(args, "command", None)
        if command is None and len(sys.argv) > 1 and sys.argv[1] in {
            "status", "admit", "consume", "recycle",
        }:
            command = sys.argv[1]
        failure = {
            "schema": CLI_RECEIPT_SCHEMA,
            "status": (
                "BLOCKED_CONSUME_FAILED"
                if command == "consume"
                else "BLOCKED_CLI_FAILED"
            ),
            "command": command,
            "operation_id": getattr(args, "operation_id", None),
            "error_class": type(error).__name__,
            "reason": str(error)[:4000],
            "exception": {
                "class": type(error).__name__,
                "message": str(error),
                "traceback": exception_traceback,
            },
        }
        if isinstance(error, ChildProcessFailure):
            failure["child"] = {
                "argv": error.argv,
                "exit_code": error.returncode,
                "stdout": error.stdout,
                "stderr": error.stderr,
                "stdout_sha256": hashlib.sha256(error.stdout.encode()).hexdigest(),
                "stderr_sha256": hashlib.sha256(error.stderr.encode()).hexdigest(),
            }
        print(json.dumps(failure, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
