from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

ROOT = Path("/home/ubuntu/business")


def load_sensor(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts/business_output_health_sensor.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completed(payload: dict, returncode: int = 0):
    return types.SimpleNamespace(
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="",
    )


def test_seo_buffer_reads_validated_depth_not_raw_ready(monkeypatch):
    sensor = load_sensor("output_health_validated_depth")
    monkeypatch.setattr(
        sensor.subprocess,
        "run",
        lambda *_args, **_kwargs: completed({
            "status": "PASS_BUFFER_READY",
            "pool": {"ready": 3, "minimum": 3, "target": 4},
            "raw_ready": 7,
            "invalid_entries": [{"entry_id": "bad"}],
        }),
    )

    result = sensor.seo_buffer()

    assert result["ok"] is True
    assert "3 validated ready" in result["actual"]
    assert "raw_ready=7" in result["actual"]
    assert "invalid=1" in result["actual"]


def test_seo_buffer_observation_below_floor_dispatches_refill(monkeypatch):
    sensor = load_sensor("output_health_refill_dispatch")
    monkeypatch.setattr(
        sensor.subprocess,
        "run",
        lambda *_args, **_kwargs: completed({
            "status": "BUFFER_CAPACITY_DEFICIT",
            "pool": {"ready": 2, "minimum": 3, "target": 4},
            "raw_ready": 2,
            "invalid_entries": [],
        }),
    )
    calls = []
    fake_selfheal = types.ModuleType("glp_article_carrier_selfheal")

    def check_supply(pool, *, emit):
        calls.append((pool, emit))
        return {"status": "SUPPLY_BELOW_FLOOR", "refill_dispatch": {"status": "PASS_DISPATCHED"}}

    fake_selfheal.check_supply = check_supply
    monkeypatch.setitem(sys.modules, "glp_article_carrier_selfheal", fake_selfheal)

    result = sensor.seo_buffer()

    assert result["ok"] is False
    assert calls == [({"ready": 2, "minimum": 3, "target": 4}, False)]
    assert "refill=PASS_DISPATCHED" in result["actual"]


def test_seo_buffer_fails_closed_on_unreadable_pool(monkeypatch):
    sensor = load_sensor("output_health_unreadable_pool")
    monkeypatch.setattr(
        sensor.subprocess,
        "run",
        lambda *_args, **_kwargs: completed({"status": "BUFFER_CAPACITY_DEFICIT", "pool": {}}),
    )

    result = sensor.seo_buffer()

    assert result["ok"] is False
    assert result["actual"] == "BLOCKED_POOL_UNREADABLE"
