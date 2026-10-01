import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import learning_loop_actions as actions  # noqa: E402
import r2_update_carrier as r2  # noqa: E402


def put(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def authority() -> dict:
    value = {
        "schema": "glp-seo-a7-learning-loop-action-authority-v1",
        "status": "AUTHORIZED_AUTONOMOUS_ACTION",
        "authorized_actions": {name: True for name in actions.REQUIRED_ACTIONS},
        "execution": {
            "operator_approval_required": False,
            "weekly_counts_report_only": True,
            "network_or_deployment_capability": False,
        },
    }
    value["authority_sha256"] = actions.payload_hash(value, "authority_sha256")
    return value


def selection(release: Path) -> dict:
    r2_row = {
        "route": "/guides/a/",
        "article_id": "article-a",
        "priority": "P2",
        "refresh_tier": "R2",
        "action": "substantive_refresh",
        "position": 8.0,
        "impressions": 100,
        "gsc_queries": [{"query": "query a", "clicks": 3, "impressions": 100, "position": 8.0}],
    }
    value = {
        "schema": "glp-seo-existing-article-update-selection-v1",
        "status": "R2_CAPACITY_DEFICIT",
        "as_of": "2026-08-17",
        "observability": {},
        "current_release": {"path": str(release.resolve()), "articles": 3},
        "weekly_plan": {
            "schema": "glp-seo-r2-weekly-plan-v1",
            "status": "R2_CAPACITY_DEFICIT",
            "required": 9,
            "selected_count": 1,
            "capacity_deficit": 8,
            "tier_capacity": {},
            "selected": [r2_row],
            "daily_quotas": [2, 1, 2, 1, 2, 1],
            "net_new_weekly_target": 3,
        },
        "p5_net_new_spokes": [{
            "route": "/guides/c/",
            "article_id": "article-c",
            "priority": "P5",
            "action": "net_new_spoke",
            "r2_edit_forbidden": True,
            "gsc_queries": [{"query": "new supporting query", "impressions": 10}],
        }],
        "effects": {"page_mutations": 0, "deployments": 0, "redirects": 0},
    }
    value["receipt_sha256"] = actions.payload_hash(value, "receipt_sha256")
    return value


def test_authority_is_self_hashed_and_complete(tmp_path):
    path = put(tmp_path / "authority.json", authority())
    assert actions.validate_authority(path)["status"] == "AUTHORIZED_AUTONOMOUS_ACTION"
    broken = authority()
    broken["authorized_actions"]["held_city_pages_in_scope"] = False
    broken["authority_sha256"] = actions.payload_hash(broken, "authority_sha256")
    put(tmp_path / "broken.json", broken)
    with pytest.raises(RuntimeError, match="scope_incomplete"):
        actions.validate_authority(tmp_path / "broken.json")


def test_p6_duplicate_query_chooses_strongest_target():
    metrics = {
        "/guides/a/": {"queries": [{"query": "GLP-1 cost", "clicks": 5, "impressions": 90, "position": 7}]},
        "/guides/b/": {"queries": [{"query": "glp 1 cost", "clicks": 1, "impressions": 100, "position": 5}]},
    }
    rows = actions.p6_duplicate_query_tasks(
        metrics,
        {"/guides/a/": "article-a", "/guides/b/": "article-b"},
        as_of=dt.date(2026, 8, 17),
    )
    assert len(rows) == 1
    assert rows[0]["source_route"] == "/guides/b/"
    assert rows[0]["target_route"] == "/guides/a/"
    assert rows[0]["state"] == "READY_FOR_CONSOLIDATION_CARRIER"


def test_emitter_writes_r2_buffer_and_consolidation_intakes(tmp_path, monkeypatch):
    as_of = dt.date(2026, 8, 17)
    release = tmp_path / "release"
    release.mkdir()
    config = put(tmp_path / "config.json", {})
    database = tmp_path / "observability.sqlite"
    database.write_bytes(b"db")
    authority_path = put(tmp_path / "authority.json", authority())
    loops = {
        "schema": "seo-machine-improvement-proposals-v1",
        "status": "PASS_PROPOSAL_ONLY",
        "proposals": [{
            "proposal_id": "thin-city",
            "loop": "E_PRUNE_CONSOLIDATE_QUARTERLY_MONTH6_PLUS",
            "page": "/guides/thin-city/",
            "action": "CONSOLIDATE_INTO_DEFINITIVE_GUIDE_THEN_301",
            "evidence": {"target": "/guides/a/", "clicks": 0},
        }],
    }
    loops["receipt_sha256"] = hashlib.sha256(
        json.dumps(loops, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    loops_path = put(tmp_path / "loops.json", loops)
    queue_path = put(tmp_path / "queue.json", {
        "schema": "seo-blog-machine-topic-queue-v1",
        "queue": [{"analytics_feedback": {"applied": True}}],
    })
    selected = selection(release)
    metrics = {
        "/guides/a/": {"queries": [{"query": "same query", "clicks": 2, "impressions": 30, "position": 6}]},
        "/guides/b/": {"queries": [{"query": "same query", "clicks": 0, "impressions": 20, "position": 9}]},
    }
    articles = [
        {"canonical_path": "/guides/a/", "article_id": "article-a"},
        {"canonical_path": "/guides/b/", "article_id": "article-b"},
    ]
    monkeypatch.setattr(actions, "load_config", lambda _path: {"_current_content_base_resolution": {"pointer": {"release_root": str(release)}}})
    monkeypatch.setattr(actions, "build_selection_receipt", lambda **_kwargs: selected)
    monkeypatch.setattr(actions, "load_observability", lambda *_args, **_kwargs: (metrics, {"database": {"path": str(database), "sha256": hashlib.sha256(database.read_bytes()).hexdigest()}}))
    monkeypatch.setattr(actions, "load_release_articles", lambda _root: (articles, {}))

    result = actions.emit(
        config_path=config,
        database=database,
        authority_path=authority_path,
        loops_path=loops_path,
        topic_queue_path=queue_path,
        run_dir=tmp_path / "run",
        action_root=tmp_path / "actions",
        buffer_intake=tmp_path / "buffer-intake.json",
        consolidation_intake=tmp_path / "consolidation-intake.json",
        as_of=as_of,
    )
    assert result["status"] == "PASS_AUTONOMOUS_ACTIONS_EMITTED"
    assert result["counts"] == {"P1": 0, "P2": 1, "P3": 0, "P4": 0, "P5": 1, "P6": 2}
    assert json.loads((tmp_path / "buffer-intake.json").read_text())["status"] == "PASS_CURRENT_BUFFER_LEARNING_INTAKE"
    assert json.loads((tmp_path / "consolidation-intake.json").read_text())["status"] == "PASS_CURRENT_CONSOLIDATION_INTAKE"
    assert json.loads((tmp_path / "actions/latest.json").read_text())["week_start"] == "2026-08-17"


def test_r2_carrier_loads_current_week_action_pointer(tmp_path):
    work_orders = selection(tmp_path)
    work_path = put(tmp_path / "r2-work-orders.json", work_orders)
    pointer = {
        "schema": "glp-seo-weekly-learning-actions-pointer-v1",
        "status": "PASS_CURRENT_WEEKLY_ACTIONS",
        "week_start": "2026-08-17",
        "r2_work_orders": {"path": str(work_path), "sha256": hashlib.sha256(work_path.read_bytes()).hexdigest()},
    }
    pointer["pointer_sha256"] = r2.payload_hash(pointer, "pointer_sha256")
    pointer_path = put(tmp_path / "latest.json", pointer)
    loaded, source = r2.load_learning_work_orders(pointer_path, as_of=dt.date(2026, 8, 20))
    assert loaded["receipt_sha256"] == work_orders["receipt_sha256"]
    assert source["path"] == str(pointer_path.resolve())
    with pytest.raises(RuntimeError, match="week_stale"):
        r2.load_learning_work_orders(pointer_path, as_of=dt.date(2026, 8, 24))


def test_r2_carrier_rebinds_normal_current_release_drift(tmp_path, monkeypatch):
    old_release = tmp_path / "old-release"
    current_release = tmp_path / "current-release"
    old_release.mkdir()
    current_release.mkdir()
    database = tmp_path / "observability.sqlite"
    database.write_bytes(b"observability")
    work_orders = selection(old_release)
    work_path = put(tmp_path / "r2-work-orders.json", work_orders)
    pointer = {
        "schema": "glp-seo-weekly-learning-actions-pointer-v1",
        "status": "PASS_CURRENT_WEEKLY_ACTIONS",
        "week_start": "2026-08-10",
        "r2_work_orders": {"path": str(work_path), "sha256": hashlib.sha256(work_path.read_bytes()).hexdigest()},
    }
    pointer["pointer_sha256"] = r2.payload_hash(pointer, "pointer_sha256")
    pointer_path = put(tmp_path / "latest.json", pointer)
    rebuilt = selection(current_release)
    monkeypatch.setattr(r2, "build_selection_receipt", lambda **kwargs: rebuilt)

    loaded, source, rebind = r2.current_learning_work_orders(
        pointer_path,
        database=database,
        release_root=current_release,
        as_of=dt.date(2026, 8, 15),
    )

    assert loaded["current_release"]["path"] == str(current_release.resolve())
    assert source["path"] == str(pointer_path.resolve())
    assert rebind["status"] == "PASS_REBOUND_TO_CURRENT_RELEASE"
    assert rebind["previous_release"] == str(old_release.resolve())
    assert rebind["current_release"] == str(current_release.resolve())
    assert rebind["rebuilt"] is True


def test_r2_carrier_rebuilds_same_release_from_current_dataforseo_and_gsc(tmp_path, monkeypatch):
    current_release = tmp_path / "current-release"
    current_release.mkdir()
    database = tmp_path / "observability.sqlite"
    database.write_bytes(b"observability")
    work_orders = selection(current_release)
    work_path = put(tmp_path / "r2-work-orders.json", work_orders)
    pointer = {
        "schema": "glp-seo-weekly-learning-actions-pointer-v1",
        "status": "PASS_CURRENT_WEEKLY_ACTIONS",
        "week_start": "2026-08-24",
        "r2_work_orders": {"path": str(work_path), "sha256": hashlib.sha256(work_path.read_bytes()).hexdigest()},
    }
    pointer["pointer_sha256"] = r2.payload_hash(pointer, "pointer_sha256")
    pointer_path = put(tmp_path / "latest.json", pointer)
    rebuilt = selection(current_release)
    rebuilt["weekly_plan"] = {**rebuilt["weekly_plan"], "selected_count": 8}
    rebuilt["receipt_sha256"] = r2.payload_hash(rebuilt, "receipt_sha256")
    monkeypatch.setattr(r2, "build_selection_receipt", lambda **kwargs: rebuilt)

    loaded, _source, rebind = r2.current_learning_work_orders(
        pointer_path,
        database=database,
        release_root=current_release,
        as_of=dt.date(2026, 8, 27),
    )

    assert loaded["receipt_sha256"] == rebuilt["receipt_sha256"]
    assert rebind["status"] == "PASS_REBUILT_CURRENT_ANALYTICS"
    assert rebind["reason"] == "current_dataforseo_and_gsc_selection_rebuilt"
    assert rebind["rebuilt"] is True

