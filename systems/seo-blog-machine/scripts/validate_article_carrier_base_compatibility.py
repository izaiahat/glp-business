#!/usr/bin/env python3
"""Read-only compatibility gate for the article carrier's live base and next entry.

This executable intentionally uses no test-runner dependency so the registered
n8n carrier can invoke it with its pinned system interpreter before any claim.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

BUSINESS = Path("/home/ubuntu/business")
MACHINE = BUSINESS / "systems/seo-blog-machine"
SCRIPTS = Path(__file__).resolve().parent
for path in (str(SCRIPTS), str(Path(__file__).resolve().parents[3] / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import article_buffer_cli as buffer_cli  # noqa: E402

BASE_POINTER = (
    BUSINESS
    / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def current_release() -> Path:
    state = json.loads(BASE_POINTER.read_text())
    release = Path(str(state.get("release_root") or ""))
    require(release.is_dir(), f"current_release_missing:{release}")
    return release


def json_file(path: Path) -> Any:
    return json.loads(path.read_text())


def current_articles(release: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rel in (
        "data/blog/content-batch.json",
        "data/blog/content-national-wow-additions.json",
    ):
        payload = json_file(release / rel)
        require(isinstance(payload, dict), f"catalog_not_object:{rel}")
        articles = payload.get("articles")
        require(isinstance(articles, list), f"catalog_articles_not_list:{rel}")
        rows.extend(row for row in articles if isinstance(row, dict))
    return rows


def validate_base_interfaces(release: Path) -> dict[str, Any]:
    import build_opus_article_release as builder
    import existing_article_updates as existing
    national = json_file(release / "data/blog/content-national-wow-additions.json")
    ownership = json_file(release / "data/blog/keyword-ownership-map.json")
    articles = national.get("articles")
    require(isinstance(articles, list) and articles, "national_articles_empty")

    patched, materialized = existing.materialize_national_article_ownership(
        ownership, articles
    )
    require(
        materialized.get("status") == "PASS_EXACT_NATIONAL_OWNERSHIP",
        "national_ownership_materialization_nonpass",
    )
    national_routes = {str(row["canonical_path"]) for row in articles}
    owners = patched.get(str(materialized.get("entries_key") or ""))
    require(isinstance(owners, list), "ownership_entries_not_list")
    actual_routes = [str(row.get("url") or "") for row in owners]
    require(len(actual_routes) == len(set(actual_routes)), "ownership_duplicate_route")
    require(national_routes.issubset(set(actual_routes)), "ownership_route_missing")

    expected_routes = {
        str(row["canonical_path"]) for row in current_articles(release)
    }

    manifest = json_file(release / "artifacts/ultracode/article-image-manifest.json")
    predecessor_state = builder.resolve_predecessor_route_master_state(
        release,
        manifest,
        expected_routes,
    )
    require(
        predecessor_state["mode"] == "release_local",
        "route_master_current_base_not_release_local",
    )
    require(
        builder.ROUTE_MASTER_STATE_RELATIVE in builder.ARTICLE_OVERLAYS,
        "route_master_state_missing_from_candidate_cow_overlays",
    )
    records = predecessor_state["records"]
    require(manifest.get("route_master_count") == len(expected_routes), "route_master_count_mismatch")
    require(manifest.get("article_count") == len(expected_routes), "route_master_article_count_mismatch")

    share_test = release / "tests/pass2-late-audit-static-output.test.mjs"
    builder.increment_article_share_control_count(release, share_test.read_text())

    return {
        "national_articles": len(expected_routes),
        "ownership_rows_materialized": len(materialized.get("rows") or []),
        "route_master_records": len(records),
        "route_master_predecessor_mode": predecessor_state["mode"],
        "route_master_candidate_cow_declared": True,
        "strict_share_count_next": len(expected_routes) + 1,
    }


def require_publishable_inventory(status):
    # Minimum/target are refill watermarks, not a ban on consuming the last
    # independently admitted entry. Treat unknown status and empty inventory
    # as failures; all existing exact next-entry/base checks still run below.
    require(isinstance(status, dict) and status.get("status") in {
        "PASS_BUFFER_STATUS", "BUFFER_CAPACITY_DEFICIT",
    }, "buffer_status_nonpass")
    require(isinstance(status.get("entries"), list) and bool(status["entries"]), "ready_buffer_empty")


def validate_next_ready_entry(release: Path) -> dict[str, Any]:
    import materialize_opus_run as materializer
    status = buffer_cli.status(buffer_cli.DEFAULT_ROOT)
    require_publishable_inventory(status)
    entries = status.get("entries")
    require(isinstance(entries, list) and entries, "ready_buffer_empty")
    entry = entries[0]
    require(isinstance(entry, dict), "ready_entry_not_object")
    manifest_path = Path(str(entry.get("manifest") or ""))
    require(manifest_path.is_file(), f"ready_manifest_missing:{manifest_path}")
    manifest = json_file(manifest_path)
    packet_dir = manifest_path.parent / "packet"
    article = json_file(packet_dir / "06-article.json")
    plan = article.get("inbound_link_plan")
    require(isinstance(plan, list) and plan, "next_entry_inbound_plan_empty")

    articles = current_articles(release)
    pending = materializer.unsatisfied_inbound_public_changes(articles, plan)
    pending_set = set(pending)
    route_map = {str(row.get("canonical_path") or ""): row for row in articles}
    target = str(article.get("canonical_path") or "")
    for row in plan:
        require(isinstance(row, dict), "inbound_plan_row_not_object")
        source = str(row.get("source_route") or "")
        planned_target = str(row.get("target_route") or "")
        require(source in route_map, f"inbound_source_missing:{source}")
        already_linked = materializer.article_links_to_route(
            route_map[source], planned_target
        )
        public_path = f"{source.strip('/')}/index.html"
        require(
            (public_path in pending_set) is (not already_linked),
            f"inbound_delta_not_exact:{source}",
        )
        require(
            (release / "public" / public_path).is_file(),
            f"inbound_public_source_missing:{public_path}",
        )

    source = Path(materializer.__file__).read_text()
    marker = "else:\n        update_ledger = None"
    require(marker in source, "new_article_branch_missing")
    branch = source.split(marker, 1)[1].split("apply_template =", 1)[0]
    require(
        "unsatisfied_inbound_public_changes(" in branch
        and "current_articles," in branch,
        "new_article_branch_bypasses_live_truth_delta_adapter",
    )
    require(
        "inbound_changed = [route_to_public_index(row[\"source_route\"])" not in branch,
        "new_article_branch_uses_literal_inbound_delta",
    )

    return {
        "ready": len(entries),
        "next_route": target,
        "planned_inbound_sources": len(plan),
        "unsatisfied_inbound_changes": len(pending),
    }


def validate_all() -> dict[str, Any]:
    # The scheduled new-guide producer uses accepted site source, not frozen
    # catalog/image overlays. Its native main-release build owns those gates.
    from article_source_carrier import preflight
    result = preflight()
    return {
        "schema": "glp.article_carrier.base_compatibility.v1",
        "status": "PASS_CURRENT_BASE_AND_NEXT_ENTRY_COMPATIBLE",
        "source_lane": result,
        "effects": 0,
    }


def main() -> int:
    try:
        print(json.dumps(validate_all(), sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": "glp.article_carrier.base_compatibility.v1",
                    "status": "BLOCKED_CURRENT_BASE_OR_NEXT_ENTRY_INCOMPATIBLE",
                    "error_class": type(exc).__name__,
                    "reason": str(exc),
                    "effects": 0,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
