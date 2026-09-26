"""Paused preparation is hash-only, exact-review-gated and never activation."""
import copy
import json
from pathlib import Path

import pytest

import awin_pin_release as producer


def fixture(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    names = ["affiliate_portal_apply_daily.sh", "awin_auth_liveness_preflight.sh",
             "affiliate_multi_network_portal_apply_daily.sh"]
    ids = ["52e2014a9579", "7a269c665b12", "5a2cba16e057"]
    jobs, entries = [], []
    for job_id, name in zip(ids, names):
        wrapper = scripts / name
        wrapper.write_text("#!/bin/sh\nexit 0\n# " + name + "\n")
        jobs.append({"id": job_id, "script": name, "script_sha256": "0" * 64,
                     "enabled": False, "state": "paused", "schedule": {"expr": "5 13 * * *"},
                     "next_run_at": "2026-09-27T13:05:00Z", "paused_reason": "fresh-browser migration hold",
                     "paused_at": "2026-09-26T00:00:00Z", "last_status": "error"})
        entries.append({"path": str(wrapper), "reviewed_sha256": producer.sha_bytes(wrapper.read_bytes()),
                        "review_basis": "current_wrapper_review", "evidence": ["isolated fixture review"]})
    jobs.append({"id": "unrelated", "enabled": True, "state": "scheduled", "script_sha256": "f" * 64})
    registry_path = tmp_path / "jobs.json"
    registry_path.write_text(json.dumps({"jobs": jobs, "metadata": "preserve fixture envelope"}))
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps({"schema": "awin-pin-review-v1", "decision": "ACCEPT_EXACT_CURRENT_BYTES",
                                      "reviewer": "fixture reviewer", "reviewed_at_utc": "2026-01-01T00:00:00Z",
                                      "entries": entries}))
    monkeypatch.setattr(producer.report, "HERMES_SCRIPTS", scripts)
    return registry_path, review_path, copy.deepcopy(jobs)


def test_paused_release_changes_only_three_hashes_after_exact_review(tmp_path, monkeypatch):
    registry_path, review_path, before = fixture(tmp_path, monkeypatch)
    registry = producer.JsonRegistry(registry_path)
    original = registry_path.read_bytes()
    code, preview = producer.release_paused_registrations(review_path, registry, apply=False)
    assert code == 0 and not preview["applied"]
    assert len(preview["registry_updates"]) == 3
    assert registry_path.read_bytes() == original
    code, result = producer.release_paused_registrations(review_path, registry, apply=True)
    assert code == 0 and result["applied"]
    expected = copy.deepcopy(before)
    for update in preview["registry_updates"]:
        next(j for j in expected if j["id"] == update["job_id"])["script_sha256"] = update["script_sha256"]
    assert registry.jobs() == expected
    assert json.loads(registry_path.read_text())["metadata"] == "preserve fixture envelope"
    assert result["pause_state_preserved"] is True
    _, second = producer.release_paused_registrations(review_path, registry, apply=True)
    assert second["registry_updates"] == []


@pytest.mark.parametrize("mutation", ["enabled", "state", "missing", "duplicate", "script", "review", "bytes"])
def test_refuses_before_any_write(tmp_path, monkeypatch, mutation):
    path, review, _ = fixture(tmp_path, monkeypatch)
    document = json.loads(path.read_text())
    if mutation == "enabled":
        document["jobs"][0]["enabled"] = True
    elif mutation == "state":
        document["jobs"][0]["state"] = "scheduled"
    elif mutation == "missing":
        document["jobs"].pop(0)
    elif mutation == "duplicate":
        document["jobs"].append(copy.deepcopy(document["jobs"][0]))
    elif mutation == "script":
        document["jobs"][0]["script"] = "unexpected.sh"
    elif mutation == "review":
        receipt = json.loads(review.read_text())
        receipt["entries"].pop()
        review.write_text(json.dumps(receipt))
    elif mutation == "bytes":
        (producer.report.HERMES_SCRIPTS / document["jobs"][0]["script"]).write_text("changed\n")
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    with pytest.raises(RuntimeError):
        producer.release_paused_registrations(review, producer.JsonRegistry(path), apply=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize("race", ["schedule", "enabled", "hash", "wrapper"])
def test_compare_and_swap_refuses_intervening_change(tmp_path, monkeypatch, race):
    path, review, _ = fixture(tmp_path, monkeypatch)
    registry = producer.JsonRegistry(path)
    original_update = registry.update_paused_hashes
    captured = []

    def racing_update(expected, digests):
        document = json.loads(path.read_text())
        if race == "wrapper":
            (producer.report.HERMES_SCRIPTS / document["jobs"][0]["script"]).write_text("changed\n")
        else:
            field = {"schedule": "next_run_at", "enabled": "enabled", "hash": "script_sha256"}[race]
            document["jobs"][0][field] = True if race == "enabled" else "changed"
            path.write_text(json.dumps(document))
        captured.append(path.read_bytes())
        original_update(expected, digests)

    monkeypatch.setattr(registry, "update_paused_hashes", racing_update)
    with pytest.raises(RuntimeError):
        producer.release_paused_registrations(review, registry, apply=True)
    assert path.read_bytes() == captured[0]


def test_cli_exposes_explicit_mutually_exclusive_paused_mode(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["awin_pin_release.py", "--help"])
    with pytest.raises(SystemExit) as ended:
        producer.main()
    assert ended.value.code == 0
    assert "--paused-registrations-only" in capsys.readouterr().out


def test_fixture_adapter_cannot_write_live_registry(tmp_path, monkeypatch):
    path, review, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(producer, "CRON_REGISTRY", path)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="fixture_adapter_live_registry_refused"):
        producer.release_paused_registrations(review, producer.JsonRegistry(path), apply=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize("contended", [False, True])
def test_hermes_adapter_hash_only_save_and_fail_closed_lock(tmp_path, monkeypatch, contended):
    import contextlib
    import fcntl
    import sys
    import threading
    import types
    path, review, before = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(producer, "CRON_REGISTRY", path)
    calls = []
    scheduler = types.ModuleType("cron.jobs")
    scheduler._current_cron_store = lambda: types.SimpleNamespace(jobs_file=path)
    scheduler._jobs_file_lock = threading.RLock()
    scheduler._jobs_lock_file = lambda: tmp_path / ".jobs.lock"
    scheduler._record_load_stamp = lambda stamp: None
    scheduler._peek_jobs_unlocked = lambda: json.loads(path.read_text())["jobs"]
    def save(jobs):
        calls.append(copy.deepcopy(jobs))
        path.write_text(json.dumps({"jobs": jobs}))
    scheduler._save_jobs_unlocked = save
    cron = types.ModuleType("cron")
    cron.jobs = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron)
    monkeypatch.setitem(sys.modules, "cron.jobs", scheduler)
    registry = producer.HermesRegistry.__new__(producer.HermesRegistry)
    def forbidden_loader():
        raise AssertionError("Paused preparation must not use the auto-repairing loader")
    registry._load_jobs = forbidden_loader
    with contextlib.ExitStack() as stack:
        if contended:
            lock = stack.enter_context(scheduler._jobs_lock_file().open("a+"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(BlockingIOError):
                producer.release_paused_registrations(review, registry, apply=True)
            assert calls == []
            assert scheduler._peek_jobs_unlocked() == before
        else:
            _, result = producer.release_paused_registrations(review, registry, apply=True)
            assert len(calls) == 1
            for job in calls[0]:
                old = next(j for j in before if j["id"] == job["id"])
                assert {k:v for k,v in job.items() if k != "script_sha256"} == {k:v for k,v in old.items() if k != "script_sha256"}
            assert result["pause_state_preserved"]
