"""GLP-213: a deadline watcher that passed its deadline without acting.

Observed 2026-09-12: scripts/fastmode_revert.py ran as a systemd user service with
--deadline 2026-09-12T07:00:00Z. The deadline passed (--check reported
seconds_to_deadline = -39 while the service was still active) and it never reverted.
The revert only happened because the orchestrator ran --now by hand, and that manual
run was then killed by its own 300s timeout mid pane-loop.

Two defects, both pinned here:
  1. The deadline was checked AFTER reset_detected(pool_samples()). A sampling call
     that hangs or raises therefore cost the deadline entirely.
  2. wait_ready was spent PER PANE, so four busy panes could block for 4x the budget
     and blow any caller timeout, losing the remaining panes AND the receipt.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "fastmode_revert.py"
TARGET = ['%99999', '$99999', 'disposable-unit', '/tmp/unit-instructions.md', 'GLP-213']


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_deadline_fires_even_when_the_pool_sampler_raises(monkeypatch):
    """THE regression. A broken sampler must never cost us the deadline."""
    module = load("fmr_deadline_raises")
    reverted = {}

    def exploding_sampler(tail=6):
        raise RuntimeError("pacing ledger unreadable")

    monkeypatch.setattr(module, "pool_samples", exploding_sampler)
    monkeypatch.setattr(module, "revert", lambda panes, why, **kw: reverted.setdefault("why", why))

    past = (datetime.now(timezone.utc) - timedelta(seconds=39)).isoformat()
    monkeypatch.setattr(sys, "argv", ["fastmode_revert.py", "--deadline", past, "--poll", "1", '--target', *TARGET])

    assert module.main() == 3
    assert "deadline" in reverted.get("why", ""), "watcher sat past its deadline doing nothing"


def test_deadline_still_fires_on_a_healthy_sampler(monkeypatch):
    """The fix must not have traded one failure for another."""
    module = load("fmr_deadline_healthy")
    reverted = {}
    monkeypatch.setattr(module, "pool_samples",
                        lambda tail=6: [{"codex-account-1": 50.0}, {"codex-account-1": 51.0}])
    monkeypatch.setattr(module, "revert", lambda panes, why, **kw: reverted.setdefault("why", why))

    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    monkeypatch.setattr(sys, "argv", ["fastmode_revert.py", "--deadline", past, "--poll", "1", '--target', *TARGET])

    assert module.main() == 3
    assert "deadline" in reverted.get("why", "")


def test_wait_ready_is_a_total_budget_not_a_per_pane_one(monkeypatch):
    """Four busy panes must not cost four times the budget."""
    module = load("fmr_budget")
    monkeypatch.setattr(module, "RECEIPTS", pathlib.Path(tempfile.mkdtemp()))
    monkeypatch.setattr(module, "ready", lambda pane: False)      # every pane busy
    monkeypatch.setattr(module, "send", lambda target: pytest.fail('busy target must not be sent'))
    monkeypatch.setattr(module, "revert_config_tier", lambda: "stub")
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    budget = 2
    started = time.time()
    record = module.revert([TARGET] * 4, "test", wait_ready=budget)
    elapsed = time.time() - started

    # Per-pane spending would be ~4x the budget here.
    assert elapsed < budget * 2, f"budget spent per pane, not in total ({elapsed:.1f}s)"
    assert record["panes_done"] == 4, "a busy pane must not cost the others their revert"
    assert all(row['transport_exit'] == 2 for row in record['panes'])


def test_a_receipt_is_written_even_when_panes_are_unreachable(monkeypatch):
    """The receipt is evidence of what was actually done; a kill must not erase it."""
    module = load("fmr_receipt")
    receipts = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(module, "RECEIPTS", receipts)
    monkeypatch.setattr(module, "ready", lambda pane: False)
    monkeypatch.setattr(module, "send", lambda target: pytest.fail('unreachable target must not be sent'))
    monkeypatch.setattr(module, "revert_config_tier", lambda: "stub")
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    module.revert([TARGET], "test", wait_ready=1)
    written = list(receipts.glob("*.json"))
    assert len(written) == 1


def test_every_poll_logs_its_decision(monkeypatch, capsys):
    """A silent no-op is what hid this for hours; each poll must say what it decided."""
    module = load("fmr_logging")
    monkeypatch.setattr(module, "pool_samples", lambda tail=6: [])
    monkeypatch.setattr(module, "revert", lambda panes, why, **kw: None)

    soon = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    monkeypatch.setattr(sys, "argv", ["fastmode_revert.py", "--deadline", soon, "--poll", "1", '--target', *TARGET])
    module.main()

    out = capsys.readouterr().out
    assert "POLL" in out and "seconds_to_deadline" in out
