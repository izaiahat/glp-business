#!/usr/bin/env python3
"""A candidate that fails the build must be PRESERVED, not deleted.

Approved 2026-09-03. release_prepared's failure path deleted the candidate, so
every build failure destroyed the only artifact that could explain it. Rounds 32
and 33 both died inside validate-provider-fastpath-plus5.mjs and left nothing to
inspect, and the pre-materialization gate could not reproduce either.

This changes the FAILURE path only: the tree is moved into a quarantine
directory, never sealed, never deployed, never reminted into the content base,
with the failure receipt beside it. Success is untouched.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import glp_affiliate_wire_backlog_worker as worker  # noqa: E402

SOURCE = (ROOT / "scripts/glp_affiliate_wire_backlog_worker.py").read_text()


def _candidate(tmp_path, name="glp-b2-wire-backlog-2026-09-03-TEST"):
    tree = tmp_path / "candidates" / name
    (tree / "data").mkdir(parents=True)
    (tree / "data/program-registry.json").write_text('{"programs": []}')
    return tree


def test_failed_candidate_is_moved_not_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CANDIDATES", tmp_path / "candidates", raising=False)
    monkeypatch.setattr(worker, "CANDIDATE_QUARANTINE",
                        tmp_path / "candidates" / "quarantine", raising=False)
    tree = _candidate(tmp_path)
    out = tmp_path / "out"; out.mkdir()
    receipt = worker._quarantine_failed_candidate(tree, out, RuntimeError("build_gate_failed"))
    assert receipt["status"] == "PASS_QUARANTINED"
    assert not tree.exists(), "the candidate must be MOVED, not left in place"
    kept = Path(receipt["path"])
    assert kept.is_dir(), "the tree must survive for diagnosis"
    assert (kept / "data/program-registry.json").is_file()
    assert receipt["sealed"] is False and receipt["deployed"] is False
    assert receipt["in_content_base"] is False
    assert "build_gate_failed" in receipt["error"]


def test_failure_receipt_is_written_beside_the_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CANDIDATES", tmp_path / "candidates", raising=False)
    monkeypatch.setattr(worker, "CANDIDATE_QUARANTINE",
                        tmp_path / "candidates" / "quarantine", raising=False)
    tree = _candidate(tmp_path)
    out = tmp_path / "out"; out.mkdir()
    receipt = worker._quarantine_failed_candidate(tree, out, RuntimeError("validator_failed"))
    kept = Path(receipt["path"])
    beside = kept.parent / f"{kept.name}.failure.json"
    assert beside.is_file(), "the failure receipt must sit beside the tree"
    assert json.loads(beside.read_text())["candidate"] == tree.name
    # and a copy lands in the run's own output dir
    assert (out / "candidate-quarantine.json").is_file()


def test_quarantine_is_bounded(tmp_path, monkeypatch):
    """A candidate tree is ~1.1GB; quarantine must not fill the disk."""
    monkeypatch.setattr(worker, "CANDIDATES", tmp_path / "candidates", raising=False)
    monkeypatch.setattr(worker, "CANDIDATE_QUARANTINE",
                        tmp_path / "candidates" / "quarantine", raising=False)
    out = tmp_path / "out"; out.mkdir()
    for index in range(worker.CANDIDATE_QUARANTINE_KEEP + 2):
        worker._quarantine_failed_candidate(
            _candidate(tmp_path, f"glp-b2-wire-backlog-2026-09-03-T{index}"),
            out, RuntimeError("boom"))
    kept = [p for p in (tmp_path / "candidates" / "quarantine").iterdir() if p.is_dir()]
    assert len(kept) <= worker.CANDIDATE_QUARANTINE_KEEP, kept


def test_quarantine_directory_cannot_be_walked_as_a_candidate():
    """It must not match the candidates/glp-b2-* glob the machine walks."""
    assert worker.CANDIDATE_QUARANTINE.name == "quarantine"
    assert not worker.CANDIDATE_QUARANTINE.name.startswith("glp-b2")


def test_only_the_failure_path_quarantines_and_success_is_untouched():
    """Structural: the success path must not call quarantine, and the failure
    path must no longer delete the candidate."""
    # def + BOTH failure paths: the per-row step tree (preparation builds each
    # row into its own step_target) and the outer candidate.
    assert SOURCE.count("_quarantine_failed_candidate(") == 3
    assert "if step_target.exists():\n                        _quarantine_failed_candidate(" in SOURCE
    assert "remove_materialized_tree(step_target)" not in SOURCE.split("except Exception as exc:")[1][:600]
    failure = SOURCE[SOURCE.index("        except Exception as exc:\n            # FAILURE PATH ONLY"):]
    failure = failure[:2000]
    assert "_quarantine_failed_candidate(" in failure
    assert "remove_materialized_tree(candidate)" not in failure, (
        "the failure path must preserve the candidate, not delete it")
    # The success path still returns its receipt.
    success = SOURCE[:SOURCE.index("        except Exception as exc:\n            # FAILURE PATH ONLY")]
    assert 'return {"status": receipt["status"]' in success
    # EVERY call site must sit inside an except block. Quarantining is a failure
    # behaviour only; a call on a success path would preserve trees that should
    # have been sealed and cleaned up.
    body = SOURCE[SOURCE.index("def release_prepared("):]
    for index in range(len(body)):
        if not body.startswith("_quarantine_failed_candidate(", index):
            continue
        preceding = body[:index]
        last_except = preceding.rfind("except Exception as exc:")
        last_return = preceding.rfind("\n            return {")
        assert last_except > last_return, (
            "a _quarantine_failed_candidate call is not inside a failure path")


def test_quarantine_failure_never_masks_the_real_error(tmp_path, monkeypatch):
    """If quarantining itself fails, it is reported, not raised over the build error."""
    monkeypatch.setattr(worker, "CANDIDATE_QUARANTINE", Path("/proc/cannot/write"), raising=False)
    tree = _candidate(tmp_path)
    out = tmp_path / "out"; out.mkdir()
    receipt = worker._quarantine_failed_candidate(tree, out, RuntimeError("real_build_error"))
    assert receipt["status"] == "FAILED_TO_QUARANTINE"
    assert "real_build_error" in receipt["error"]
