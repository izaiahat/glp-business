#!/usr/bin/env python3
"""Mint immutable daily R2 update authorities from weekly GSC learning truth.

This script selects only. It has no network, review, publication, deployment, or
messaging capability. The resulting update selection receipt is consumed by the
standard packet -> deterministic QA -> Sol review -> apply/publish chain.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from current_content_base import verify_resolution
from existing_article_updates import (
    DAILY_UPDATE_QUOTAS,
    build_selection_receipt,
    canonical_route,
    daily_plan,
    load_observability,
    load_release_articles,
    select_update_candidates,
    validate_r2_execution_contract,
)
from machine_config import load_config
from weekly_strategy_consumer import load_strategy as load_cogny_strategy, preference as cogny_preference, preference_score as cogny_preference_score
from selection_receipt import write_immutable_receipt

BUSINESS = Path("/home/ubuntu/business")
SYSTEM = BUSINESS / "systems/seo-blog-machine"
DEFAULT_CONFIG = SYSTEM / "config/verticals/glp-clinic-scout.json"
DEFAULT_DATABASE = BUSINESS / "reports/glp-clinic-scout/operator-national-wow-20260723/data/glp-observability.sqlite"
DEFAULT_OUTPUT_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/r2-update-carrier"
DEFAULT_CLAIM_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/r2-update-operation-claims"
DEFAULT_WEEKLY_ACTIONS = BUSINESS / "reports/glp-clinic-scout/seo-machine/weekly-learning-actions/latest.json"
TOPIC_QUEUE = BUSINESS / "reports/glp-clinic-scout/seo-blog-machine-20260730/topic-queues/latest.json"
STRATEGY_QUEUE = BUSINESS / "reports/glp-clinic-scout/seo-blog-machine-20260804/topic-queues/20260804T120000Z-cluster-rotation.json"
PUBLISHED_LEDGER = BUSINESS / "reports/glp-clinic-scout/seo-machine/published-topic-ledger.json"
HOLD_LEDGER = BUSINESS / "reports/glp-clinic-scout/seo-machine/article-hold-ledger.json"
BASE_STATE = BUSINESS / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
UPDATE_LEDGER = BUSINESS / "reports/glp-clinic-scout/seo-machine/article-update-event-ledger.json"


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def payload_hash(value: dict[str, Any], field: str) -> str:
    unsigned = dict(value)
    unsigned.pop(field, None)
    return hashlib.sha256(canonical(unsigned)).hexdigest()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def spec(path: Path) -> dict[str, str]:
    source = path.resolve(strict=True)
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"r2_input_invalid:{source}")
    return {"path": str(source), "sha256": sha(source)}


def write_sealed(path: Path, payload: dict[str, Any], field: str) -> Path:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    value = dict(payload)
    value[field] = payload_hash(value, field)
    rendered = canonical(value)
    if target.exists():
        if target.read_bytes() != rendered:
            raise RuntimeError(f"r2_immutable_conflict:{target}")
        os.chmod(target, 0o444)
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


def topic_id(candidate: dict[str, Any], as_of: dt.date) -> str:
    closure = hashlib.sha256(
        f"{candidate['route']}\0{candidate['article_id']}\0{candidate['priority']}\0{as_of.isoformat()}".encode()
    ).hexdigest()[:20]
    return f"r2-{closure}"


def primary_query(candidate: dict[str, Any]) -> str:
    rows = candidate.get("gsc_queries") or []
    values = [" ".join(str((row or {}).get("query") or "").split()) for row in rows]
    values = [value for value in values if value]
    if not values:
        raise RuntimeError(f"r2_gsc_query_missing:{candidate.get('route')}")
    return values[0]


def object_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def compact_payload_hash(value: dict[str, Any], field: str) -> str:
    """Match updater/publisher self hashes, whose canonical bytes omit newline."""
    unsigned = dict(value)
    unsigned.pop(field, None)
    rendered = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label}_invalid:{path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label}_not_object:{path}")
    return value


def load_learning_work_orders(pointer_path: Path, *, as_of: dt.date) -> tuple[dict[str, Any], dict[str, str]]:
    """Load the sealed Monday R2 plan emitted by the autonomous learning loop."""
    source = pointer_path.resolve(strict=True)
    if not source.is_file() or source.is_symlink():
        raise RuntimeError("r2_learning_actions_pointer_invalid")
    pointer = read_object(source, "r2_learning_actions_pointer")
    if pointer.get("schema") != "glp-seo-weekly-learning-actions-pointer-v1" or pointer.get("status") != "PASS_CURRENT_WEEKLY_ACTIONS":
        raise RuntimeError("r2_learning_actions_contract_invalid")
    if pointer.get("pointer_sha256") != payload_hash(pointer, "pointer_sha256"):
        raise RuntimeError("r2_learning_actions_self_hash_invalid")
    expected_week = (as_of - dt.timedelta(days=as_of.weekday())).isoformat()
    if pointer.get("week_start") != expected_week:
        raise RuntimeError("r2_learning_actions_week_stale")
    work_order_spec = pointer.get("r2_work_orders") or {}
    work_order_path = Path(str(work_order_spec.get("path") or "")).resolve(strict=True)
    if not work_order_path.is_file() or work_order_path.is_symlink() or sha(work_order_path) != work_order_spec.get("sha256"):
        raise RuntimeError("r2_learning_actions_work_order_drift")
    selection = read_object(work_order_path, "r2_learning_r2_work_orders")
    if selection.get("schema") != "glp-seo-existing-article-update-selection-v1":
        raise RuntimeError("r2_learning_actions_selection_schema_invalid")
    if selection.get("receipt_sha256") != payload_hash(selection, "receipt_sha256"):
        raise RuntimeError("r2_learning_actions_selection_self_hash_invalid")
    return selection, spec(source)


def current_learning_work_orders(
    pointer_path: Path,
    *,
    database: Path,
    release_root: Path,
    as_of: dt.date,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """Rebuild weekly selection from current release plus current DFS/GSC truth.

    The weekly pointer remains the action authority and lineage anchor. Its
    selected bytes are not allowed to freeze Monday's ranking for the rest of
    the week: each daily mint rebuilds the same governed P1-P4 contract against
    the latest saved keyword-gap artifact and observability mirror.
    """
    selection, pointer = load_learning_work_orders(pointer_path, as_of=as_of)
    declared = Path(str((selection.get("current_release") or {}).get("path") or "")).resolve()
    current = release_root.resolve()
    rebuilt = build_selection_receipt(database=database, release_root=current, as_of=as_of)
    unchanged = rebuilt.get("receipt_sha256") == selection.get("receipt_sha256")
    if declared == current and unchanged:
        return selection, pointer, {
            "status": "BOUND_TO_CURRENT_RELEASE_AND_ANALYTICS",
            "previous_release": str(declared),
            "current_release": str(current),
            "rebuilt": False,
        }
    release_changed = declared != current
    return rebuilt, pointer, {
        "status": (
            "PASS_REBOUND_TO_CURRENT_RELEASE"
            if release_changed else "PASS_REBUILT_CURRENT_ANALYTICS"
        ),
        "reason": (
            "normal_deploy_reminted_current_content_base"
            if release_changed else "current_dataforseo_and_gsc_selection_rebuilt"
        ),
        "previous_release": str(declared),
        "current_release": str(current),
        "previous_selection_receipt_sha256": selection.get("receipt_sha256"),
        "rebuilt_selection_receipt_sha256": rebuilt.get("receipt_sha256"),
        "rebuilt": True,
    }


def nested_records(payload: dict[str, Any], records_key: str) -> list[dict[str, Any]]:
    records: Any = payload
    for part in records_key.split("."):
        if part:
            records = records.get(part) if isinstance(records, dict) else None
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise RuntimeError("r2_catalog_records_invalid")
    return records


def exact_catalog_identity(config: dict[str, Any], route: str, article_id: str) -> dict[str, Any]:
    matches: list[tuple[dict[str, Any], int, dict[str, Any]]] = []
    article_id_routes: list[str] = []
    for catalog in config["paths"]["content_catalogs"]:
        catalog_path = Path(catalog["_resolved_path"]).resolve(strict=True)
        rows = nested_records(read_object(catalog_path, f"r2_catalog:{catalog['catalog_id']}"), catalog["records_key"])
        for index, article in enumerate(rows):
            if article.get("article_id") == article_id:
                article_id_routes.append(str(article.get("canonical_path") or ""))
            if article.get("canonical_path") == route:
                matches.append((catalog, index, article))
    if len(matches) != 1:
        raise RuntimeError(f"r2_current_route_match_count_invalid:{len(matches)}")
    catalog, index, article = matches[0]
    if article.get("article_id") != article_id or article_id_routes != [route]:
        raise RuntimeError("r2_current_article_identity_invalid")
    return {
        "catalog_id": catalog["catalog_id"],
        "catalog_kind": catalog["kind"],
        "release_relative_path": catalog["release_relative_path"],
        "records_key": catalog["records_key"],
        "article_index": index,
        "canonical_path": route,
        "article_id": article_id,
        "article_sha256": object_hash(article),
    }


def public_page_spec(release_root: Path, route: str) -> dict[str, str]:
    public_root = (release_root / "public").resolve(strict=True)
    relative = Path(*[part for part in route.strip("/").split("/") if part]) / "index.html"
    candidate = public_root / relative
    if candidate.is_symlink():
        raise RuntimeError("r2_public_page_symlink_invalid")
    page = candidate.resolve(strict=True)
    if public_root not in page.parents or not page.is_file():
        raise RuntimeError("r2_public_page_invalid")
    return {"release_relative_path": f"public/{relative.as_posix()}", **spec(page)}


def selected_weekly_row(weekly: dict[str, Any], route: str, article_id: str) -> dict[str, Any]:
    if weekly.get("schema") != "glp-seo-existing-article-update-selection-v1":
        raise RuntimeError("r2_weekly_selection_schema_invalid")
    unsigned = dict(weekly)
    expected = unsigned.pop("receipt_sha256", None)
    if expected != hashlib.sha256(canonical(unsigned)).hexdigest():
        raise RuntimeError("r2_weekly_selection_self_hash_invalid")
    rows = (weekly.get("weekly_plan") or {}).get("selected") or []
    matches = [row for row in rows if isinstance(row, dict) and row.get("route") == route and row.get("article_id") == article_id]
    if len(matches) != 1 or matches[0].get("refresh_tier") != "R2" or matches[0].get("priority") not in {"P1", "P2", "P3", "P4"}:
        raise RuntimeError(f"r2_weekly_selection_match_invalid:{len(matches)}")
    selected = dict(matches[0])
    selected["target_query"] = primary_query(selected)
    return selected


def authority_source_inputs(
    *,
    config_path: Path,
    database: Path,
    weekly_selection: Path,
    resolution: dict[str, Any],
) -> dict[str, Any]:
    weekly = read_object(weekly_selection, "r2_weekly_selection")
    return {
        "config": spec(config_path),
        "observability_database": spec(database),
        "weekly_gsc_selection": {**spec(weekly_selection), "receipt_sha256": weekly.get("receipt_sha256")},
        "current_content_base_state": dict(resolution["state"]),
        "current_content_base_resolution": json.loads(json.dumps(resolution, sort_keys=True)),
        "current_content_base_resolution_sha256": object_hash(resolution),
        "catalogs": json.loads(json.dumps(resolution["catalogs"], sort_keys=True)),
        "program_registry": json.loads(json.dumps(resolution["program_registry"], sort_keys=True)),
    }


def validate_update_authority(path: Path, *, config_path: Path) -> dict[str, Any]:
    source = path.resolve(strict=True)
    if not source.is_file() or source.is_symlink():
        raise RuntimeError("r2_authority_file_invalid")
    if stat.S_IMODE(source.stat().st_mode) & 0o222:
        raise RuntimeError("r2_authority_not_immutable")
    authority = read_object(source, "r2_authority")
    if authority.get("schema") != "glp-seo-r2-update-authority-v1" or authority.get("status") != "AUTHORIZED_R2_REFRESH" or authority.get("operation") != "r2_refresh":
        raise RuntimeError("r2_authority_contract_invalid")
    if authority.get("authority_sha256") != payload_hash(authority, "authority_sha256"):
        raise RuntimeError("r2_authority_self_hash_invalid")
    inputs = authority.get("inputs") or {}
    for key in ("config", "weekly_gsc_selection"):
        source_spec = inputs.get(key) or {}
        source_path = Path(str(source_spec.get("path") or "")).resolve(strict=True)
        if source_path.is_symlink() or not source_path.is_file() or sha(source_path) != source_spec.get("sha256"):
            raise RuntimeError(f"r2_authority_input_drift:{key}")
    # The observability database is a live append-only input. Its digest is
    # provenance for the bytes used to mint the sealed weekly selection, not a
    # control-file pin: later GSC ingestion must not strand an already-minted
    # authority. The immutable weekly selection and exact selected row remain
    # hash-bound below.
    database_spec = inputs.get("observability_database") or {}
    database_path = Path(str(database_spec.get("path") or "")).resolve(strict=True)
    database_digest = database_spec.get("sha256")
    if (
        database_path.is_symlink()
        or not database_path.is_file()
        or not isinstance(database_digest, str)
        or len(database_digest) != 64
        or any(character not in "0123456789abcdef" for character in database_digest)
    ):
        raise RuntimeError("r2_authority_input_invalid:observability_database")
    if Path(inputs["config"]["path"]).resolve(strict=True) != config_path.resolve(strict=True):
        raise RuntimeError("r2_authority_config_path_mismatch")
    weekly = read_object(Path(inputs["weekly_gsc_selection"]["path"]), "r2_weekly_selection")
    if weekly.get("receipt_sha256") != inputs["weekly_gsc_selection"].get("receipt_sha256"):
        raise RuntimeError("r2_authority_weekly_receipt_drift")
    selected = selected_weekly_row(weekly, authority["target_route"], authority["article_id"])
    if selected != authority.get("selected_r2") or object_hash(selected) != authority.get("selected_r2_sha256"):
        raise RuntimeError("r2_authority_weekly_selected_drift")
    config = load_config(config_path)
    resolution = config.get("_current_content_base_resolution")
    if not isinstance(resolution, dict):
        raise RuntimeError("r2_authority_current_resolution_missing")
    verify_resolution(resolution)
    if object_hash(resolution) != inputs.get("current_content_base_resolution_sha256"):
        raise RuntimeError("r2_authority_current_resolution_drift")
    if resolution.get("state") != inputs.get("current_content_base_state") or resolution.get("catalogs") != inputs.get("catalogs") or resolution.get("program_registry") != inputs.get("program_registry"):
        raise RuntimeError("r2_authority_current_inputs_drift")
    current_release = authority.get("current_release") or {}
    for key in ("deployment_id", "release_root", "release_tree_sha256", "public_tree_sha256", "manifest", "final_receipt"):
        if current_release.get(key) != resolution["pointer"].get(key):
            raise RuntimeError(f"r2_authority_current_release_drift:{key}")
    identity = exact_catalog_identity(config, authority["target_route"], authority["article_id"])
    stored_identity = authority.get("existing_article") or {}
    if any(stored_identity.get(key) != value for key, value in identity.items()):
        raise RuntimeError("r2_authority_existing_article_drift")
    if stored_identity.get("public_page") != public_page_spec(Path(current_release["release_root"]), authority["target_route"]):
        raise RuntimeError("r2_authority_public_page_drift")
    validate_r2_execution_contract(
        authority.get("r2_contract"),
        route=authority["target_route"],
    )
    return authority


def validate_r2_packet_contract(authority: dict[str, Any], article: dict[str, Any], update_authority: dict[str, Any]) -> None:
    exact = {
        "operation": "r2_refresh",
        "topic_id": update_authority["topic_id"],
        "article_id": update_authority["article_id"],
        "target_query": update_authority["target_query"],
        "target_route": update_authority["target_route"],
        "refresh_tier": "R2",
        "priority": update_authority["priority"],
    }
    for key, value in exact.items():
        observed = (authority.get("target_route") or authority.get("route")) if key == "target_route" else authority.get(key)
        if observed != value:
            raise RuntimeError(f"r2_packet_authority_mismatch:{key}")
    if authority.get("r2_contract") != update_authority.get("r2_contract"):
        raise RuntimeError("r2_packet_authority_mismatch:r2_contract")
    if article.get("canonical_path") != update_authority["target_route"] or article.get("article_id") != update_authority["article_id"]:
        raise RuntimeError("r2_packet_article_identity_mismatch")


def current_input_specs(resolution: dict[str, Any], update_authority: Path,
                        strategy_paths: tuple[Path, Path] | None = None) -> dict[str, Any]:
    strategy_queue = strategy_paths[0] if strategy_paths else STRATEGY_QUEUE
    strategy = json.loads(strategy_queue.read_text(encoding="utf-8"))
    strategy_map = (strategy_paths[1] if strategy_paths else Path(str(strategy.get("map_path") or ""))).resolve(strict=True)
    release_root = Path(resolution["pointer"]["release_root"])
    sitemap = release_root / "public/sitemap.xml"
    return {
        "topic_queue": spec(TOPIC_QUEUE),
        "strategy_queue": spec(strategy_queue),
        "strategy_map": spec(strategy_map),
        "published_ledger": spec(PUBLISHED_LEDGER),
        "hold_ledger": spec(HOLD_LEDGER),
        "current_content_base_state": spec(BASE_STATE),
        "current_content_base_resolution": resolution,
        "sitemap_readback": spec(sitemap),
        "update_authority": spec(update_authority),
        "update_event_ledger": spec(UPDATE_LEDGER),
    }


def mint_candidate(
    candidate: dict[str, Any],
    *,
    output_dir: Path,
    config: dict[str, Any],
    config_path: Path,
    database: Path,
    weekly_selection: Path,
    resolution: dict[str, Any],
    current_routes: set[str],
    striking_routes: set[str],
    as_of: dt.date,
    slot: int,
    strategy_paths: tuple[Path, Path] | None = None,
) -> dict[str, Any]:
    route = canonical_route(candidate["route"])
    article_id = str(candidate.get("article_id") or "")
    if not article_id or route not in current_routes:
        raise RuntimeError("r2_target_identity_invalid")
    # The refreshed route is itself the authorized destination for every inbound
    # link in this packet.  Cadence-reserve P4 pages may not be in the global
    # position-5..20 set; omitting the selected route made the authority
    # impossible to satisfy and stranded publication after review.
    usable_striking = sorted((striking_routes | {route}) & current_routes)
    if not usable_striking:
        raise RuntimeError("r2_striking_distance_capacity_empty")
    selected_r2 = selected_weekly_row(read_object(weekly_selection, "r2_weekly_selection"), route, article_id)
    query = selected_r2["target_query"]
    operation_topic_id = topic_id(candidate, as_of)
    contract = {
        **candidate,
        "priority": candidate["priority"],
        "position": float(candidate["position"]),
        "gsc_queries": candidate["gsc_queries"],
        "current_routes": sorted(current_routes),
        "striking_distance_routes": usable_striking,
        "required_inbound_links": 2 if candidate["priority"] == "P1" else 1,
        "r2_edit_forbidden": False,
    }
    release_root = Path(resolution["pointer"]["release_root"]).resolve(strict=True)
    existing_article = exact_catalog_identity(config, route, article_id)
    existing_article["public_page"] = public_page_spec(release_root, route)
    authority = {
        "schema": "glp-seo-r2-update-authority-v1",
        "status": "AUTHORIZED_R2_REFRESH",
        "operation": "r2_refresh",
        "strategy_version": "glp-content-mix-v1",
        "refresh_tier": "R2",
        "priority": candidate["priority"],
        "topic_id": operation_topic_id,
        "article_id": article_id,
        "target_query": query,
        "target_route": route,
        "as_of": as_of.isoformat(),
        "daily_slot": slot,
        "selected_r2": selected_r2,
        "selected_r2_sha256": object_hash(selected_r2),
        "existing_article": existing_article,
        "current_release": {
            key: json.loads(json.dumps(resolution["pointer"][key], sort_keys=True))
            for key in ("deployment_id", "release_root", "release_tree_sha256", "public_tree_sha256", "manifest", "final_receipt")
        },
        "inputs": authority_source_inputs(
            config_path=config_path,
            database=database,
            weekly_selection=weekly_selection,
            resolution=resolution,
        ),
        "r2_contract": contract,
        "constraints": {
            "same_route": True,
            "same_article_id": True,
            "minimum_substantive_delta": 0.30,
            "new_gsc_answering_section": 1,
            "new_primary_source": 1,
            "new_outbound_internal_link": 1,
            "inbound_link_minimum": contract["required_inbound_links"],
            "deploy_lock": "/home/ubuntu/.hermes/state/glp-seo-carrier.lock",
        },
        "effects": {"page_mutations": 0, "deployments": 0, "redirects": 0},
    }
    authority_path = write_sealed(output_dir / f"{slot:02d}-{operation_topic_id}-authority.json", authority, "authority_sha256")
    authority_payload = validate_update_authority(authority_path, config_path=config_path)
    authority_digest = sha(authority_path)
    selected = {
        "topic_id": operation_topic_id,
        "article_id": article_id,
        "target_query": query,
        "target_route": route,
        "operator_directive_id": f"glp-content-mix-v1:{authority_digest}",
        "queue_position": slot,
        "cluster_id": "existing-article-refresh",
        "depth_stage": "refresh",
        "queue_sha256": authority_digest,
        "strategy_safe": True,
        "predecessor_proof": {"required": True, "satisfied": True, "basis": "current_content_base_exact_route_and_article_id"},
        "refresh_tier": "R2",
        "priority": candidate["priority"],
        "r2_contract": authority_payload["r2_contract"],
    }
    selection = {
        "schema": "glp-opus-content-update-selection-receipt-v1",
        "status": "PASS_R2_UPDATE_SELECTION",
        "operation": "r2_refresh",
        "generated_at_utc": f"{as_of.isoformat()}T00:00:00Z",
        "selection_mode": "weekly_gsc_learning_loop_p1_p4",
        "eligible_count": 1,
        "selected": [selected],
        "eligible": [selected],
        "rules": {
            "strategy_version": "glp-content-mix-v1",
            "priority_order": ["P1", "P2", "P3", "P4"],
            "deep_loss_routes_become_p5_spokes": True,
            "r3_non_executable": True,
        },
        "inputs": current_input_specs(resolution, authority_path, strategy_paths),
    }
    selection_path = write_immutable_receipt(
        output_dir / f"{slot:02d}-{operation_topic_id}-selection.json",
        selection,
    )
    return {
        "slot": slot,
        "topic_id": operation_topic_id,
        "article_id": article_id,
        "target_query": query,
        "target_route": route,
        "priority": candidate["priority"],
        "authority": spec(authority_path),
        "selection": spec(selection_path),
        "operation": "r2_refresh",
    }


def current_release_weekly_selection_path(
    week_dir: Path,
    resolution: dict[str, Any],
    selection: dict[str, Any] | None = None,
) -> Path:
    """Version immutable weekly truth by release and analytics selection."""
    digest = str((resolution.get("pointer") or {}).get("release_tree_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError("r2_current_release_tree_hash_invalid")
    suffix = digest[:16]
    if selection is not None:
        selection_digest = str(selection.get("receipt_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", selection_digest):
            raise RuntimeError("r2_current_selection_hash_invalid")
        suffix += f"-{selection_digest[:12]}"
    return week_dir / f"weekly-gsc-selection-{suffix}.json"


def current_release_generation(resolution: dict[str, Any]) -> dict[str, str]:
    """Return the exact live generation every mutable R2 decision depends on."""
    pointer = resolution.get("pointer") or {}
    deployment_id = str(pointer.get("deployment_id") or "")
    digest = str(pointer.get("release_tree_sha256") or "")
    if not deployment_id:
        raise RuntimeError("r2_current_deployment_id_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError("r2_current_release_tree_hash_invalid")
    return {
        "deployment_id": deployment_id,
        "release_tree_sha256": digest,
        "generation_id": digest[:16],
    }


def current_release_day_directory(
    week_dir: Path,
    update_day_index: int,
    resolution: dict[str, Any],
    selection: dict[str, Any] | None = None,
) -> Path:
    """Keep each release/analytics generation beside prior immutable plans."""
    generation = current_release_generation(resolution)
    suffix = generation["generation_id"]
    if selection is not None:
        selection_digest = str(selection.get("receipt_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", selection_digest):
            raise RuntimeError("r2_current_selection_hash_invalid")
        suffix += f"-{selection_digest[:12]}"
    return week_dir / f"day-{update_day_index}-{suffix}"


def superseded_day_plans(
    week_dir: Path,
    update_day_index: int,
    current_day_dir: Path,
) -> list[dict[str, str]]:
    """Bind the new plan to every older immutable plan it supersedes."""
    candidates = [week_dir / f"day-{update_day_index}" / "daily-plan.json"]
    candidates.extend(sorted(week_dir.glob(f"day-{update_day_index}-*/daily-plan.json")))
    return [
        spec(path)
        for path in candidates
        if path.is_file() and path.parent.resolve() != current_day_dir.resolve()
    ]


def carried_completed_authorities(
    superseded_plans: list[dict[str, str]],
    *,
    as_of: dt.date,
    claim_root: Path = DEFAULT_CLAIM_ROOT,
) -> list[dict[str, Any]]:
    """Carry exact same-day PASS publications across release-generation remints."""
    carried: dict[str, dict[str, Any]] = {}
    claim_root_resolved = claim_root.resolve(strict=True) if claim_root.exists() else claim_root.resolve()
    for prior_spec in sorted(superseded_plans, key=lambda row: str(row.get("path") or "")):
        raw_plan_path = str(prior_spec.get("path") or "")
        if not raw_plan_path or not Path(raw_plan_path).is_absolute():
            raise RuntimeError("r2_superseded_plan_spec_invalid")
        plan_path = Path(raw_plan_path).resolve(strict=True)
        if (
            plan_path.is_symlink()
            or not plan_path.is_file()
            or str(plan_path) != raw_plan_path
            or sha(plan_path) != prior_spec.get("sha256")
            or plan_path.parent.parent.name != as_of.isoformat()
        ):
            raise RuntimeError("r2_superseded_plan_binding_invalid")
        prior_plan = read_object(plan_path, "r2_superseded_plan")
        if (
            prior_plan.get("schema") != "glp-seo-r2-daily-authority-plan-v1"
            or prior_plan.get("as_of") != as_of.isoformat()
            or prior_plan.get("plan_sha256") != payload_hash(prior_plan, "plan_sha256")
        ):
            raise RuntimeError("r2_superseded_plan_contract_invalid")
        day_path = plan_path.parent / "r2-day-receipt.json"
        if not day_path.is_file():
            continue
        if day_path.is_symlink():
            raise RuntimeError("r2_superseded_day_receipt_invalid")
        day = read_object(day_path, "r2_superseded_day_receipt")
        daily_plan = day.get("daily_plan") or {}
        if (
            day.get("schema") != "glp-seo-r2-day-receipt-v1"
            or day.get("pt_date") != as_of.isoformat()
            or day.get("receipt_sha256") != compact_payload_hash(day, "receipt_sha256")
            or daily_plan.get("path") != str(plan_path)
            or daily_plan.get("sha256") != sha(plan_path)
        ):
            raise RuntimeError("r2_superseded_day_receipt_binding_invalid")
        for row in day.get("candidates") or []:
            if not isinstance(row, dict) or row.get("status") != "PASS_REFRESHED":
                continue
            operation = str(row.get("operation_id") or "")
            topic = str(row.get("topic_id") or "")
            route = canonical_route(str(row.get("target_route") or ""))
            claim_spec = row.get("claim") or {}
            expected_claim = claim_root_resolved / f"{hashlib.sha256(operation.encode()).hexdigest()}.json"
            raw_claim_path = str(claim_spec.get("path") or "")
            if not raw_claim_path or Path(raw_claim_path).resolve(strict=True) != expected_claim:
                raise RuntimeError("r2_carried_claim_path_binding_invalid")
            claim_path = Path(raw_claim_path)
            if (
                claim_path.is_symlink()
                or not claim_path.is_file()
                or sha(claim_path) != claim_spec.get("sha256")
            ):
                raise RuntimeError("r2_carried_claim_file_binding_invalid")
            claim = read_object(claim_path, "r2_carried_claim")
            publication = claim.get("publication_receipt") or {}
            raw_final_path = str(publication.get("path") or "")
            if not raw_final_path or not Path(raw_final_path).is_absolute():
                raise RuntimeError("r2_carried_publication_path_invalid")
            final_path = Path(raw_final_path).resolve(strict=True)
            if final_path.is_symlink() or not final_path.is_file() or sha(final_path) != publication.get("sha256"):
                raise RuntimeError("r2_carried_publication_file_binding_invalid")
            final = read_object(final_path, "r2_carried_publication")
            if (
                claim.get("claim_sha256") != compact_payload_hash(claim, "claim_sha256")
                or claim.get("status") != "PASS_REFRESHED"
                or claim.get("quota_consumed") is not True
                or claim.get("operation_id") != operation
                or claim.get("topic_id") != topic
                or canonical_route(str(claim.get("target_route") or "")) != route
                or final.get("receipt_sha256") != compact_payload_hash(final, "receipt_sha256")
                or final.get("schema") != "glp-opus-article-final-publication-v1"
                or final.get("status") != "PASS_LIVE_GSC_READBACK_PASS"
                or final.get("operation") != "r2_refresh"
                or final.get("topic_id") != topic
                or final.get("article_id") != claim.get("article_id")
                or final.get("live_url") != f"https://glpclinicscout.com{route}"
                or not re.fullmatch(r"[0-9a-f]{64}", str(final.get("target_sha256") or ""))
            ):
                raise RuntimeError("r2_carried_completion_contract_invalid")
            carried_row = {
                "status": "PASS_REFRESHED",
                "operation_id": operation,
                "topic_id": topic,
                "target_route": route,
                "article_id": claim.get("article_id"),
                "completed_at_utc": claim.get("completed_at_utc"),
                "source_plan": spec(plan_path),
                "source_day_receipt": spec(day_path),
                "claim": spec(claim_path),
                "publication_receipt": spec(final_path),
                "target_sha256": final.get("target_sha256"),
            }
            prior = carried.get(route)
            if prior is not None:
                stable_fields = (
                    "status",
                    "operation_id",
                    "topic_id",
                    "target_route",
                    "article_id",
                    "claim",
                    "publication_receipt",
                    "target_sha256",
                )
                if any(prior.get(field) != carried_row.get(field) for field in stable_fields):
                    raise RuntimeError("r2_carried_completion_route_conflict")
                # A later generation can repeat the same exact carried proof in
                # its day receipt. Keep the first deterministic provenance row
                # rather than treating identical terminal truth as a conflict.
                continue
            carried[route] = carried_row
    return sorted(carried.values(), key=lambda row: (str(row.get("completed_at_utc") or ""), row["target_route"]))


def daily_authority_candidates(
    candidates: list[dict[str, Any]],
    *,
    update_day_index: int,
    quota: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return scheduled authorities plus an ordered same-day substitute pool."""
    start = sum(DAILY_UPDATE_QUOTAS[:update_day_index])
    primary = candidates[start:start + quota]
    primary_keys = {(row.get("route"), row.get("article_id")) for row in primary}
    ordered_remainder = [*candidates[start + quota:], *candidates[:start]]
    substitutes = [
        row for row in ordered_remainder
        if (row.get("route"), row.get("article_id")) not in primary_keys
    ]
    return primary, substitutes


def current_striking_distance_routes(
    metrics: dict[str, dict[str, Any]],
    current_routes: set[str],
    selected_candidates: list[dict[str, Any]],
) -> set[str]:
    """Fuse lagged GSC route truth with selected current class-A DFS truth."""
    observed = {
        canonical_route(route)
        for route, metric in metrics.items()
        if (
            (11 <= float(metric.get("position") or 0) <= 40 and int(metric.get("impressions") or 0) > 0)
            or any(
                11 <= float(query.get("position") or 0) <= 40
                and int(query.get("impressions") or 0) > 0
                for query in metric.get("queries") or []
                if isinstance(query, dict)
            )
        )
        and route in current_routes
    }
    selected_class_a = {
        canonical_route(row.get("route"))
        for row in selected_candidates
        if row.get("opportunity_class") == "A" and row.get("route") in current_routes
    }
    return observed | selected_class_a


def execute_authority_pool(
    primary: list[dict[str, Any]],
    substitutes: list[dict[str, Any]],
    *,
    quota: int,
    runner: Any,
) -> dict[str, Any]:
    """Exercise an authority pool while preserving the final-validator gate."""
    attempts: list[dict[str, Any]] = []
    completed = 0
    substitution_fired = False
    pool = [("primary", row) for row in primary] + [("substitute", row) for row in substitutes]
    for role, authority in pool:
        if completed >= quota:
            break
        outcome = runner(authority)
        passed = isinstance(outcome, dict) and outcome.get("all_checks_passed") is True
        if role == "substitute":
            substitution_fired = True
        attempts.append({
            "role": role,
            "topic_id": authority.get("topic_id"),
            "target_route": authority.get("target_route") or authority.get("route"),
            "passed": passed,
            "outcome": outcome,
        })
        if passed:
            completed += 1
    genuinely_exhausted = completed < quota and len(attempts) == len(pool)
    return {
        "status": (
            "PASS_R2_DAILY_QUOTA" if completed >= quota
            else "PARTIAL_R2_DAILY_QUOTA" if completed
            else "BLOCKED_R2_DAILY_QUOTA"
        ),
        "quota": quota,
        "completed": completed,
        "attempts": attempts,
        "substitution_fired": substitution_fired,
        "genuinely_exhausted": genuinely_exhausted,
        "all_checks_required": True,
        "gate_bypass_used": False,
    }


HEAD_CHANGE_FAILURE_CODES = (
    "r2_authority_current_resolution_drift",
    "r2_authority_current_inputs_drift",
    "r2_authority_current_release_drift",
    "production_head_changed",
    "base_state_drift",
    "base_state_invalid",
)


def authority_plan_matches_resolution(
    plan: dict[str, Any],
    resolution: dict[str, Any],
) -> bool:
    """Compare a sealed plan's generation with the freshly resolved live head."""
    return plan.get("release_generation") == current_release_generation(resolution)


def outcome_reports_head_change(outcome: Any) -> bool:
    """Recognize only explicit fail-closed head/pin drift outcomes."""
    try:
        rendered = json.dumps(outcome, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(outcome)
    return any(code in rendered for code in HEAD_CHANGE_FAILURE_CODES)


def execute_generation_bound_authority_plan(
    plan: dict[str, Any],
    *,
    runner: Any,
    resolve_current: Any,
    remint_current: Any,
    max_head_changes: int = 8,
) -> dict[str, Any]:
    """Run R2 while replacing stale generations instead of consuming them.

    The publication chain still owns the final current-head CAS. If that guard
    refuses because the head moved after this coordinator's preflight, the
    refusal is a remint signal, never permission to publish stale bytes and
    never an exhausted content authority.
    """
    active = plan
    quota = int(active.get("quota") or 0)
    if quota <= 0:
        raise RuntimeError("r2_authority_plan_quota_invalid")
    attempts: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []
    completed_keys: set[tuple[str, str]] = set()
    cursor = 0

    def pool(value: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        return [
            *(("primary", row) for row in value.get("authorities") or []),
            *(("substitute", row) for row in value.get("substitute_authorities") or []),
        ]

    def key(authority: dict[str, Any]) -> tuple[str, str]:
        return (
            str(authority.get("target_route") or authority.get("route") or ""),
            str(authority.get("article_id") or ""),
        )

    def rebind(resolution: dict[str, Any], reason: str, outcome: Any = None) -> None:
        nonlocal active, cursor
        if len(recoveries) >= max_head_changes:
            raise RuntimeError("r2_head_change_remint_budget_exhausted")
        previous = active.get("release_generation")
        fresh = remint_current(resolution)
        latest = resolve_current()
        if not authority_plan_matches_resolution(fresh, latest):
            raise RuntimeError("r2_reminted_plan_not_current")
        active = fresh
        cursor = 0
        recoveries.append({
            "reason": reason,
            "previous_release_generation": previous,
            "current_release_generation": fresh["release_generation"],
            "refusal_outcome": outcome,
            "stale_publish_permitted": False,
        })

    while len(completed_keys) < quota:
        current = resolve_current()
        if not authority_plan_matches_resolution(active, current):
            rebind(current, "live_release_pointer_changed_before_authority")
            continue
        active_pool = pool(active)
        while cursor < len(active_pool) and key(active_pool[cursor][1]) in completed_keys:
            cursor += 1
        if cursor >= len(active_pool):
            break
        role, authority = active_pool[cursor]
        cursor += 1
        outcome = runner(authority)
        if outcome_reports_head_change(outcome):
            latest = resolve_current()
            if not authority_plan_matches_resolution(active, latest):
                rebind(latest, "live_release_pointer_changed_during_authority", outcome)
                continue
        passed = isinstance(outcome, dict) and outcome.get("all_checks_passed") is True
        attempts.append({
            "role": role,
            "topic_id": authority.get("topic_id"),
            "target_route": authority.get("target_route") or authority.get("route"),
            "passed": passed,
            "outcome": outcome,
            "release_generation": active["release_generation"],
        })
        if passed:
            completed_keys.add(key(authority))

    completed = len(completed_keys)
    return {
        "status": (
            "PASS_R2_DAILY_QUOTA" if completed >= quota
            else "PARTIAL_R2_DAILY_QUOTA" if completed
            else "BLOCKED_R2_DAILY_QUOTA"
        ),
        "quota": quota,
        "completed": completed,
        "attempts": attempts,
        "head_change_recoveries": recoveries,
        "release_generation": active["release_generation"],
        "stale_publish_permitted": False,
        "all_checks_required": True,
        "gate_bypass_used": False,
    }


def load_continuation_candidate(plan_path: Path, operation: str, *, as_of: dt.date,
                                update_day_index: int) -> dict[str, Any]:
    """Keep an existing operation's sealed selection; never rerank/substitute it."""
    def sealed(path, field, expected=None):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o222:
            raise RuntimeError('r2_continuation_not_immutable')
        if expected is not None and sha(path) != expected:
            raise RuntimeError('r2_continuation_input_drift')
        value = read_object(path, 'r2_continuation')
        if value.get(field) != payload_hash(value, field):
            raise RuntimeError('r2_continuation_self_hash')
        return value

    plan = sealed(plan_path, 'plan_sha256')
    if (plan.get('schema') != 'glp-seo-r2-daily-authority-plan-v1'
            or plan.get('as_of') != as_of.isoformat()
            or plan.get('update_day_index') != update_day_index or plan.get('quota') != 1):
        raise RuntimeError('r2_continuation_day_or_quota_mismatch')
    matches = [row for row in [*plan.get('authorities', []), *plan.get('substitute_authorities', [])]
               if f"r2-refresh:{row.get('topic_id')}:{row.get('target_route')}" == operation]
    if len(matches) != 1:
        raise RuntimeError('r2_continuation_operation_mismatch')
    row = matches[0]
    authority = sealed(row['authority']['path'], 'authority_sha256', row['authority']['sha256'])
    selection = sealed(row['selection']['path'], 'receipt_sha256', row['selection']['sha256'])
    if (authority.get('schema') != 'glp-seo-r2-update-authority-v1'
            or authority.get('status') != 'AUTHORIZED_R2_REFRESH'
            or authority.get('operation') != 'r2_refresh'
            or authority.get('as_of') != as_of.isoformat()
            or selection.get('operation') != 'r2_refresh'
            or selection.get('inputs', {}).get('update_authority') != row['authority']
            or len(selection.get('selected', [])) != 1):
        raise RuntimeError('r2_continuation_authority_contract')
    for key in ('topic_id', 'article_id', 'target_route', 'target_query', 'priority'):
        if row.get(key) != authority.get(key) or selection['selected'][0].get(key) != authority.get(key):
            raise RuntimeError('r2_continuation_identity_mismatch:' + key)
    weekly_spec = authority['inputs']['weekly_gsc_selection']
    weekly_path = Path(weekly_spec['path'])
    weekly = sealed(weekly_path, 'receipt_sha256', weekly_spec['sha256'])
    candidate = selected_weekly_row(weekly, row['target_route'], row['article_id'])
    # selected_weekly_row adds the normalized target_query; do not change the
    # original P1-P4 identity, source selection or original plan day.
    if (candidate != authority.get('selected_r2')
            or object_hash(candidate) != authority.get('selected_r2_sha256')
            or topic_id(candidate, as_of) != row['topic_id']):
        raise RuntimeError('r2_continuation_selection_mismatch')
    return {'candidate': candidate, 'weekly_selection': weekly_path,
            'operation_id': operation, 'plan': plan, 'row': row,
            'strategy_inputs': {key: selection['inputs'][key] for key in ('strategy_queue', 'strategy_map')}}


def run_continuation(*, plan_path: Path, operation: str, config_path: Path,
                     database: Path, output_root: Path, as_of: dt.date,
                     update_day_index: int, strategy_paths: tuple[Path, Path] | None = None) -> dict[str, Any]:
    """No apply: mint one current-generation continuation via mint_candidate.

    The immutable producer AUTHORIZED status is selection authority, not public
    release approval. The original pending journal/claim is never modified.
    """
    retained = load_continuation_candidate(plan_path, operation, as_of=as_of,
                                           update_day_index=update_day_index)
    claim_path = DEFAULT_CLAIM_ROOT / (hashlib.sha256(operation.encode()).hexdigest() + '.json')
    claim = read_object(claim_path, 'r2_continuation_claim')
    if (claim.get('claim_sha256') != compact_payload_hash(claim, 'claim_sha256')
            or claim.get('operation_id') != operation or claim.get('status') != 'FAILED_RETRYABLE'
            or claim.get('quota_consumed') is not False):
        raise RuntimeError('r2_continuation_claim_not_retryable')
    paths = strategy_paths or tuple(Path(retained['strategy_inputs'][key]['path'])
                                    for key in ('strategy_queue', 'strategy_map'))
    for key, path in zip(('strategy_queue', 'strategy_map'), paths):
        if path.is_symlink() or spec(path)['sha256'] != retained['strategy_inputs'][key]['sha256']:
            raise RuntimeError('r2_continuation_strategy_drift:' + key)
    config = load_config(config_path)
    resolution = config['_current_content_base_resolution']
    verify_resolution(resolution)
    articles, _ = load_release_articles(Path(resolution['pointer']['release_root']))
    routes = {canonical_route(row['canonical_path']) for row in articles}
    generation = current_release_generation(resolution)
    day_dir = output_root.resolve()/as_of.isoformat()/f"day-{update_day_index}-{generation['generation_id']}-continuation-{sha(plan_path)[:12]}"
    # The original source selection is retained byte-for-byte. All publication
    # identity/catalog/registry/page bindings are freshly verified by the usual
    # authority producer, not hand-authored by this continuation path.
    old_authority = read_object(Path(retained['row']['authority']['path']), 'r2_continuation_authority')
    row = mint_candidate(retained['candidate'], output_dir=day_dir, config=config,
                         config_path=config_path.resolve(strict=True), database=database.resolve(strict=True),
                         weekly_selection=retained['weekly_selection'], resolution=resolution,
                         current_routes=routes,
                         striking_routes=set(old_authority['r2_contract']['striking_distance_routes']),
                         as_of=as_of, slot=1, strategy_paths=paths)
    row['authority_role'] = 'primary'
    payload = {
        'schema':'glp-seo-r2-daily-authority-plan-v1', 'status':'PASS_R2_DAILY_AUTHORITIES',
        'strategy_version':'glp-content-mix-v1', 'as_of':as_of.isoformat(),
        'update_day_index':update_day_index, 'release_generation':generation,
        'supersedes_plans':[spec(plan_path)], 'quota':1, 'carried_completed_authorities':[],
        'carried_completed':0, 'remaining_quota':1, 'authorized':1, 'substitute_authorized':0,
        'total_authorized':1, 'total_capacity':1, 'capacity_deficit':0,
        'weekly_gsc_selection':spec(retained['weekly_selection']),
        'authorities':[row], 'substitute_authorities':[], 'failures':[],
        'continuation':{'operation_id':operation, 'original_plan':spec(plan_path),
                        'claim_read_only':spec(claim_path), 'public_release_authorized':False},
        'execution_contract':{'stop_after_completed':1,'primary_then_substitute':False,
                              'all_checks_required':True,'gate_bypass_permitted':False},
        'effects':{'page_mutations':0,'deployments':0,'redirects':0},
    }
    receipt = write_sealed(day_dir/'daily-plan.json', payload, 'plan_sha256')
    return {**payload, 'receipt':spec(receipt)}


def run(
    *,
    database: Path,
    config_path: Path,
    output_root: Path,
    as_of: dt.date,
    update_day_index: int,
    weekly_actions: Path | None = None,
    continuation_plan: Path | None = None,
    continuation_operation: str | None = None,
    strategy_paths: tuple[Path, Path] | None = None,
) -> dict[str, Any]:
    if continuation_plan is not None or continuation_operation is not None:
        if continuation_plan is None or not continuation_operation:
            raise RuntimeError('r2_continuation_pair_required')
        return run_continuation(plan_path=continuation_plan, operation=continuation_operation,
                                config_path=config_path, database=database, output_root=output_root,
                                as_of=as_of, update_day_index=update_day_index, strategy_paths=strategy_paths)
    if strategy_paths is not None:
        raise RuntimeError('r2_strategy_relocation_requires_continuation')
    if not 0 <= update_day_index < len(DAILY_UPDATE_QUOTAS):
        raise RuntimeError("update_day_index_invalid")
    config = load_config(config_path)
    resolution = config["_current_content_base_resolution"]
    verify_resolution(resolution)
    release_root = Path(resolution["pointer"]["release_root"])
    articles, _catalogs = load_release_articles(release_root)
    current_routes = {canonical_route(row["canonical_path"]) for row in articles}
    metrics, evidence = load_observability(database, window_end=as_of)
    learning_action_pointer = None
    learning_action_rebind = None
    if weekly_actions is not None:
        weekly_payload, learning_action_pointer, learning_action_rebind = current_learning_work_orders(
            weekly_actions,
            database=database,
            release_root=release_root,
            as_of=as_of,
        )
    else:
        weekly_payload = build_selection_receipt(
            database=database,
            release_root=release_root,
            as_of=as_of,
        )
    cogny_strategy, cogny_meta = load_cogny_strategy(as_of=as_of)
    r2_candidates = list((weekly_payload.get("weekly_plan") or {}).get("selected") or [])
    # Reorder only the already governed P1-P4 R2 pool. No new route/identity or
    # quota is minted from a recommendation, including low-volume page2 advice.
    r2_candidates = sorted(r2_candidates, key=lambda row: (
        str(row.get("priority") or ""),
        -cogny_preference_score(cogny_strategy, row.get("target_query") or row.get("query"), row.get("route"), "r2_refresh"),
    ))
    r2_candidates = [{**row, "cogny_strategy_preference": cogny_preference(
        cogny_strategy, row.get("target_query") or row.get("query"), row.get("route"), "r2_refresh")}
        for row in r2_candidates]
    weekly_payload = {**weekly_payload, "cogny_weekly_strategy": cogny_meta,
                      "weekly_plan": {**(weekly_payload.get("weekly_plan") or {}), "selected": r2_candidates}}
    weekly_payload["receipt_sha256"] = payload_hash(weekly_payload, "receipt_sha256")
    week_dir = output_root.resolve() / as_of.isoformat()
    week_dir.mkdir(parents=True, exist_ok=True)
    weekly_selection = write_sealed(
        current_release_weekly_selection_path(week_dir, resolution, weekly_payload),
        weekly_payload,
        "receipt_sha256",
    )
    quota = DAILY_UPDATE_QUOTAS[update_day_index]
    release_generation = current_release_generation(resolution)
    day_dir = current_release_day_directory(
        week_dir, update_day_index, resolution, weekly_payload,
    )
    supersedes_plans = superseded_day_plans(
        week_dir,
        update_day_index,
        day_dir,
    )
    carried = carried_completed_authorities(supersedes_plans, as_of=as_of)
    if len(carried) > quota:
        raise RuntimeError("r2_carried_completion_exceeds_daily_quota")
    carried_routes = {row["target_route"] for row in carried}
    eligible_candidates = [
        row for row in r2_candidates
        if canonical_route(str(row.get("route") or "")) not in carried_routes
    ]
    remaining_quota = quota - len(carried)
    if remaining_quota:
        chosen, substitute_candidates = daily_authority_candidates(
            eligible_candidates,
            update_day_index=update_day_index,
            quota=remaining_quota,
        )
    else:
        chosen, substitute_candidates = [], []
    daily = {
        "schema": "glp-seo-r2-daily-plan-v1",
        "status": "PASS_DAILY_R2_PLAN" if len(chosen) == remaining_quota else "R2_CAPACITY_DEFICIT",
        "update_day_index": update_day_index,
        "quota": quota,
        "carried_completed": len(carried),
        "remaining_quota": remaining_quota,
        "selected_count": len(chosen),
        "capacity_deficit": remaining_quota - len(chosen),
        "selected": chosen,
        "substitutes": substitute_candidates,
        "weekly_capacity": weekly_payload.get("weekly_plan") or {},
    }
    striking_routes = current_striking_distance_routes(metrics, current_routes, r2_candidates)
    day_dir.mkdir(parents=True, exist_ok=True)
    minted: list[dict[str, Any]] = []
    substitute_minted: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    authority_pool = [
        *(("primary", candidate) for candidate in daily["selected"]),
        *(("substitute", candidate) for candidate in daily["substitutes"]),
    ]
    for slot, (role, candidate) in enumerate(authority_pool, 1):
        try:
            authority = mint_candidate(
                candidate,
                output_dir=day_dir,
                config=config,
                config_path=config_path.resolve(strict=True),
                database=database.resolve(strict=True),
                weekly_selection=weekly_selection,
                resolution=resolution,
                current_routes=current_routes,
                striking_routes=striking_routes,
                as_of=as_of,
                slot=slot,
            )
            authority["authority_role"] = role
            if role == "primary":
                minted.append(authority)
            else:
                substitute_minted.append(authority)
        except Exception as error:
            failures.append({
                "authority_role": role,
                "route": str(candidate.get("route") or ""),
                "reason": f"{type(error).__name__}:{error}",
            })
    total_authorized = len(minted) + len(substitute_minted)
    total_capacity = len(carried) + total_authorized
    status = "PASS_R2_DAILY_AUTHORITIES" if total_capacity >= quota else "R2_CAPACITY_DEFICIT"
    payload = {
        "schema": "glp-seo-r2-daily-authority-plan-v1",
        "status": status,
        "strategy_version": "glp-content-mix-v1",
        "as_of": as_of.isoformat(),
        "update_day_index": update_day_index,
        "release_generation": release_generation,
        "supersedes_plans": supersedes_plans,
        "quota": quota,
        "carried_completed_authorities": carried,
        "carried_completed": len(carried),
        "remaining_quota": remaining_quota,
        "authorized": len(minted),
        "substitute_authorized": len(substitute_minted),
        "total_authorized": total_authorized,
        "total_capacity": total_capacity,
        "capacity_deficit": max(0, quota - total_capacity),
        "weekly_capacity": daily["weekly_capacity"],
        "observability": evidence,
        "striking_distance_routes": sorted(striking_routes),
        "weekly_gsc_selection": spec(weekly_selection),
        "weekly_learning_actions": learning_action_pointer,
        "weekly_learning_actions_rebind": learning_action_rebind,
        "authorities": minted,
        "substitute_authorities": substitute_minted,
        "execution_contract": {
            "stop_after_completed": remaining_quota,
            "primary_then_substitute": True,
            "repair_each_authority_before_substitution": True,
            "all_checks_required": True,
            "gate_bypass_permitted": False,
            "blocked_only_when_every_authority_failed_repair": True,
        },
        "failures": failures,
        "effects": {"page_mutations": 0, "deployments": 0, "redirects": 0},
    }
    receipt = write_sealed(day_dir / "daily-plan.json", payload, "plan_sha256")
    return {**payload, "receipt": spec(receipt)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--as-of", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--update-day-index", type=int, required=True)
    parser.add_argument("--weekly-actions", type=Path, default=DEFAULT_WEEKLY_ACTIONS)
    parser.add_argument("--continuation-plan", type=Path)
    parser.add_argument("--continuation-operation")
    parser.add_argument("--retained-strategy-queue", type=Path)
    parser.add_argument("--retained-strategy-map", type=Path)
    args = parser.parse_args()
    if bool(args.retained_strategy_queue) != bool(args.retained_strategy_map):
        parser.error('retained strategy queue and map must be supplied together')
    result = run(
        database=args.database,
        config_path=args.config,
        output_root=args.output_root,
        as_of=args.as_of,
        update_day_index=args.update_day_index,
        weekly_actions=args.weekly_actions,
        continuation_plan=args.continuation_plan,
        continuation_operation=args.continuation_operation,
        strategy_paths=(args.retained_strategy_queue, args.retained_strategy_map) if args.retained_strategy_queue else None,
    )
    print(json.dumps({
        "status": result["status"],
        "quota": result["quota"],
        "authorized": result["authorized"],
        "capacity_deficit": result["capacity_deficit"],
        "receipt": result["receipt"],
        "authorities": result["authorities"],
        "substitute_authorities": result["substitute_authorities"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
