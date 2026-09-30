#!/usr/bin/env python3
"""Turn weekly P1-P6 learning conclusions into sealed carrier work orders.

The emitter has no network, provider, publication, deployment, redirect, or
messaging capability. It writes immutable evidence-bound work orders and atomic
latest pointers for the existing R2, article-buffer, and consolidation lanes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from existing_article_updates import (
    build_selection_receipt,
    canonical_route,
    load_observability,
    load_release_articles,
)
from machine_config import load_config

BUSINESS = Path("/home/ubuntu/business")
SYSTEM = BUSINESS / "systems/seo-blog-machine"
DEFAULT_CONFIG = SYSTEM / "config/verticals/glp-clinic-scout.json"
DEFAULT_DATABASE = BUSINESS / "reports/glp-clinic-scout/operator-national-wow-20260723/data/glp-observability.sqlite"
DEFAULT_AUTHORITY = BUSINESS / "reports/glp-clinic-scout/seo-machine/A7-LEARNING-LOOP-ACTION-AUTHORITY-20260815.json"
DEFAULT_ACTION_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/weekly-learning-actions"
DEFAULT_BUFFER_INTAKE = BUSINESS / "reports/glp-clinic-scout/seo-machine/article-buffer/action-intake/latest-learning-work-orders.json"
DEFAULT_CONSOLIDATION_INTAKE = BUSINESS / "reports/glp-clinic-scout/seo-machine/consolidation-carrier/inbox/latest-learning-tasks.json"
REQUIRED_ACTIONS = {
    "retarget_keywords",
    "deprioritize_losers",
    "refresh_or_rewrite_underperformers",
    "merge_or_retire_thin_pages",
    "held_city_pages_in_scope",
    "emit_r2_refresh_work_orders",
    "emit_net_new_spoke_work_orders",
    "emit_consolidation_tasks",
}
SAFE_ROUTE = re.compile(r"^/[a-z0-9][a-z0-9/-]*/$")


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def payload_hash(value: dict[str, Any], field: str) -> str:
    unsigned = dict(value)
    unsigned.pop(field, None)
    return hashlib.sha256(canonical(unsigned)).hexdigest()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label}_invalid:{path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label}_not_object:{path}")
    return value


def file_spec(path: Path) -> dict[str, str]:
    source = path.resolve(strict=True)
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"learning_action_input_invalid:{source}")
    return {"path": str(source), "sha256": sha(source)}


def validate_authority(path: Path) -> dict[str, Any]:
    source = path.resolve(strict=True)
    authority = read_object(source, "learning_action_authority")
    if authority.get("schema") != "glp-seo-a7-learning-loop-action-authority-v1":
        raise RuntimeError("learning_action_authority_schema_invalid")
    if authority.get("status") != "AUTHORIZED_AUTONOMOUS_ACTION":
        raise RuntimeError("learning_action_authority_status_invalid")
    if authority.get("authority_sha256") != payload_hash(authority, "authority_sha256"):
        raise RuntimeError("learning_action_authority_self_hash_invalid")
    actions = authority.get("authorized_actions") or {}
    if any(actions.get(name) is not True for name in REQUIRED_ACTIONS):
        raise RuntimeError("learning_action_authority_scope_incomplete")
    execution = authority.get("execution") or {}
    if execution.get("operator_approval_required") is not False or execution.get("weekly_counts_report_only") is not True:
        raise RuntimeError("learning_action_authority_execution_invalid")
    if execution.get("network_or_deployment_capability") is not False:
        raise RuntimeError("learning_action_authority_effects_invalid")
    return authority


def write_immutable(path: Path, payload: dict[str, Any]) -> Path:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
    if target.exists():
        if target.read_bytes() != rendered:
            raise RuntimeError(f"learning_action_immutable_conflict:{target}")
        return target
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o444)
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def write_pointer(path: Path, payload: dict[str, Any]) -> Path:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    value = dict(payload)
    value["pointer_sha256"] = payload_hash(value, "pointer_sha256")
    rendered = json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def normalize_query(value: Any) -> str:
    text = re.sub(r"\bglp\s*-?\s*1\b", "glp1", str(value or "").lower())
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _route(value: Any) -> str:
    route = str(value or "")
    if not SAFE_ROUTE.fullmatch(route) or "//" in route or "/../" in route:
        raise RuntimeError(f"learning_action_route_invalid:{route}")
    return route


def p6_duplicate_query_tasks(
    metrics: dict[str, dict[str, Any]],
    article_ids: dict[str, str],
    *,
    as_of: dt.date,
) -> list[dict[str, Any]]:
    by_query: dict[str, list[dict[str, Any]]] = {}
    for route, metric in sorted(metrics.items()):
        if route not in article_ids:
            continue
        for row in metric.get("queries") or []:
            query = normalize_query(row.get("query"))
            if not query:
                continue
            by_query.setdefault(query, []).append({
                "route": route,
                "article_id": article_ids[route],
                "clicks": int(row.get("clicks") or 0),
                "impressions": int(row.get("impressions") or 0),
                "position": float(row.get("position") or 999.0),
            })
    tasks: list[dict[str, Any]] = []
    for query, raw_rows in sorted(by_query.items()):
        rows_by_route: dict[str, dict[str, Any]] = {}
        for row in raw_rows:
            current = rows_by_route.setdefault(row["route"], {**row, "clicks": 0, "impressions": 0})
            current["clicks"] += row["clicks"]
            current["impressions"] += row["impressions"]
            current["position"] = min(current["position"], row["position"])
        rows = list(rows_by_route.values())
        if len(rows) < 2:
            continue
        rows.sort(key=lambda row: (-row["clicks"], -row["impressions"], row["position"], row["route"]))
        target = rows[0]
        for source in sorted(rows[1:], key=lambda row: row["route"]):
            evidence = {"query": query, "target": target, "source": source, "as_of": as_of.isoformat()}
            tasks.append({
                "task_id": "p6-" + hashlib.sha256(canonical(evidence)).hexdigest()[:20],
                "priority": "P6",
                "action": "merge_unique_value_then_301",
                "source_route": source["route"],
                "source_article_id": source["article_id"],
                "target_route": target["route"],
                "target_article_id": target["article_id"],
                "duplicate_query": query,
                "evidence": evidence,
                "held_city_page_allowed": True,
                "delete_without_redirect_forbidden": True,
                "state": "READY_FOR_CONSOLIDATION_CARRIER",
            })
    return tasks


def loop_consolidation_tasks(loops: dict[str, Any], *, as_of: dt.date) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for row in loops.get("proposals") or []:
        if not isinstance(row, dict) or not str(row.get("loop") or "").startswith("E_"):
            continue
        if row.get("action") != "CONSOLIDATE_INTO_DEFINITIVE_GUIDE_THEN_301":
            continue
        evidence = row.get("evidence") or {}
        source = _route(row.get("page"))
        target = _route(evidence.get("target"))
        if source == target:
            raise RuntimeError("learning_action_consolidation_self_target")
        identity = {"proposal_id": row.get("proposal_id"), "source": source, "target": target, "as_of": as_of.isoformat()}
        tasks.append({
            "task_id": "p6-thin-" + hashlib.sha256(canonical(identity)).hexdigest()[:20],
            "priority": "P6",
            "action": "merge_unique_value_then_301",
            "source_route": source,
            "target_route": target,
            "evidence": evidence,
            "source_proposal_id": row.get("proposal_id"),
            "held_city_page_allowed": True,
            "delete_without_redirect_forbidden": True,
            "state": "READY_FOR_CONSOLIDATION_CARRIER",
        })
    return tasks


def week_start(day: dt.date) -> str:
    return (day - dt.timedelta(days=day.weekday())).isoformat()


def emit(
    *,
    config_path: Path,
    database: Path,
    authority_path: Path,
    loops_path: Path,
    topic_queue_path: Path,
    run_dir: Path,
    action_root: Path,
    buffer_intake: Path,
    consolidation_intake: Path,
    as_of: dt.date,
) -> dict[str, Any]:
    authority = validate_authority(authority_path)
    loops = read_object(loops_path.resolve(strict=True), "learning_loops")
    if loops.get("schema") != "seo-machine-improvement-proposals-v1":
        raise RuntimeError("learning_loops_schema_invalid")
    unsigned_loops = dict(loops)
    expected_loops_hash = unsigned_loops.pop("receipt_sha256", None)
    actual_loops_hash = hashlib.sha256(
        json.dumps(unsigned_loops, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if expected_loops_hash != actual_loops_hash:
        raise RuntimeError("learning_loops_self_hash_invalid")
    topic_queue = read_object(topic_queue_path.resolve(strict=True), "learning_topic_queue")
    if topic_queue.get("schema") != "seo-blog-machine-topic-queue-v1":
        raise RuntimeError("learning_topic_queue_schema_invalid")

    config = load_config(config_path)
    release_root = Path(config["_current_content_base_resolution"]["pointer"]["release_root"]).resolve(strict=True)
    selection = build_selection_receipt(database=database, release_root=release_root, as_of=as_of)
    metrics, observability = load_observability(database, window_end=as_of)
    articles, _catalogs = load_release_articles(release_root)
    article_ids = {
        canonical_route(row.get("canonical_path")): str(row.get("article_id") or "")
        for row in articles
        if row.get("canonical_path") and row.get("article_id")
    }

    p5_rows = list(selection.get("p5_net_new_spokes") or [])
    p6_rows = p6_duplicate_query_tasks(metrics, article_ids, as_of=as_of)
    p6_rows.extend(loop_consolidation_tasks(loops, as_of=as_of))
    unique_p6 = {row["task_id"]: row for row in p6_rows}
    p6_rows = [unique_p6[key] for key in sorted(unique_p6)]

    authority_spec = file_spec(authority_path)
    selection_path = write_immutable(run_dir / "r2-work-orders.json", selection)
    p5_payload = {
        "schema": "glp-seo-p5-buffer-work-orders-v1",
        "status": "PASS_P5_BUFFER_WORK_ORDERS",
        "as_of": as_of.isoformat(),
        "week_start": week_start(as_of),
        "authority": authority_spec,
        "work_orders": p5_rows,
        "effects": {"page_mutations": 0, "publishes": 0, "deployments": 0},
    }
    p5_payload["receipt_sha256"] = payload_hash(p5_payload, "receipt_sha256")
    p5_path = write_immutable(run_dir / "p5-buffer-work-orders.json", p5_payload)
    p6_payload = {
        "schema": "glp-seo-p6-consolidation-tasks-v1",
        "status": "PASS_P6_CONSOLIDATION_TASKS",
        "as_of": as_of.isoformat(),
        "week_start": week_start(as_of),
        "authority": authority_spec,
        "tasks": p6_rows,
        "effects": {"page_mutations": 0, "redirects": 0, "deployments": 0},
    }
    p6_payload["receipt_sha256"] = payload_hash(p6_payload, "receipt_sha256")
    p6_path = write_immutable(run_dir / "p6-consolidation-tasks.json", p6_payload)

    selected = (selection.get("weekly_plan") or {}).get("selected") or []
    counts = {name: sum(1 for row in selected if row.get("priority") == name) for name in ("P1", "P2", "P3", "P4")}
    counts.update({"P5": len(p5_rows), "P6": len(p6_rows)})
    queue_rows = topic_queue.get("queue") or []
    retargeted = sum(1 for row in queue_rows if ((row.get("analytics_feedback") or {}).get("applied") is True))
    manifest = {
        "schema": "glp-seo-weekly-learning-actions-v1",
        "status": "PASS_AUTONOMOUS_ACTIONS_EMITTED",
        "as_of": as_of.isoformat(),
        "week_start": week_start(as_of),
        "authority": authority_spec,
        "inputs": {
            "config": file_spec(config_path),
            "database": observability.get("database"),
            "loops": file_spec(loops_path),
            "topic_queue": file_spec(topic_queue_path),
            "current_release": {"path": str(release_root)},
        },
        "work_orders": {
            "r2": file_spec(selection_path),
            "buffer": file_spec(p5_path),
            "consolidation": file_spec(p6_path),
        },
        "counts": counts,
        "queue_actions": {"retargeted_or_reprioritized": retargeted, "total_ranked": len(queue_rows)},
        "reporting": "weekly_counts_only",
        "effects": {"page_mutations": 0, "redirects": 0, "publishes": 0, "deployments": 0},
    }
    manifest["receipt_sha256"] = payload_hash(manifest, "receipt_sha256")
    manifest_path = write_immutable(run_dir / "action-manifest.json", manifest)

    latest = {
        "schema": "glp-seo-weekly-learning-actions-pointer-v1",
        "status": "PASS_CURRENT_WEEKLY_ACTIONS",
        "as_of": as_of.isoformat(),
        "week_start": week_start(as_of),
        "manifest": file_spec(manifest_path),
        "r2_work_orders": file_spec(selection_path),
        "buffer_work_orders": file_spec(p5_path),
        "consolidation_tasks": file_spec(p6_path),
        "authority": authority_spec,
    }
    latest_path = write_pointer(action_root / "latest.json", latest)
    write_pointer(buffer_intake, {
        "schema": "glp-seo-article-buffer-learning-intake-pointer-v1",
        "status": "PASS_CURRENT_BUFFER_LEARNING_INTAKE",
        "as_of": as_of.isoformat(),
        "week_start": week_start(as_of),
        "work_orders": file_spec(p5_path),
        "authority": authority_spec,
    })
    write_pointer(consolidation_intake, {
        "schema": "glp-seo-consolidation-carrier-intake-pointer-v1",
        "status": "PASS_CURRENT_CONSOLIDATION_INTAKE",
        "as_of": as_of.isoformat(),
        "week_start": week_start(as_of),
        "tasks": file_spec(p6_path),
        "authority": authority_spec,
    })
    return {**manifest, "latest": file_spec(latest_path), "manifest": file_spec(manifest_path)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--authority", type=Path, default=DEFAULT_AUTHORITY)
    parser.add_argument("--loops", type=Path, required=True)
    parser.add_argument("--topic-queue", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--action-root", type=Path, default=DEFAULT_ACTION_ROOT)
    parser.add_argument("--buffer-intake", type=Path, default=DEFAULT_BUFFER_INTAKE)
    parser.add_argument("--consolidation-intake", type=Path, default=DEFAULT_CONSOLIDATION_INTAKE)
    parser.add_argument("--as-of", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args()
    result = emit(
        config_path=args.config,
        database=args.database,
        authority_path=args.authority,
        loops_path=args.loops,
        topic_queue_path=args.topic_queue,
        run_dir=args.run_dir,
        action_root=args.action_root,
        buffer_intake=args.buffer_intake,
        consolidation_intake=args.consolidation_intake,
        as_of=args.as_of,
    )
    print(json.dumps({"status": result["status"], "counts": result["counts"], "latest": result["latest"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
