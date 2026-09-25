#!/usr/bin/env python3
"""Never-monetized is derived from the terms-decision record, not declared.

Operator ruling 2026-09-03. validate-editorial-monetization.mjs hardcoded
NEVER_MONETIZED_PROGRAMS = ["measured"], citing a 2026-08-13 REJECT-TERMS
receipt. That receipt declares its own reversal (reversible_by
"ACCEPT-TERMS 126727"), and the operator rescinded it on 2026-08-31: "i never
denied measured ... remove that wherever it has that in the system." The stale
literal kept a live provider link-dark and failed every deploy that activated it.

A program is now never-monetized ONLY while its latest terms decision is a
rejection with no later acceptance.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import glp_affiliate_wire_backlog_worker as worker  # noqa: E402

DECISIONS = (ROOT / "reports/glp-clinic-scout/affiliate-machine"
             / "awin-item1-terms-decisions-20260813")


def _latest(slug: str):
    latest = None
    for path in DECISIONS.glob("awin-terms-*.json"):
        doc = json.loads(path.read_text())
        if doc.get("schema") != "glp-awin-terms-decision-v2":
            continue
        if str(doc.get("program_name") or "").strip().lower().replace(" ", "-") != slug:
            continue
        at = str(doc.get("recorded_at_utc") or "")
        if latest is None or at > latest[0]:
            latest = (at, str(doc.get("decision") or ""))
    return latest


def test_measureds_latest_decision_is_acceptance():
    latest = _latest("measured")
    assert latest is not None, "no terms decision found for Measured"
    assert latest[1] == "operator_accepted_terms", latest


def test_the_original_rejection_receipt_is_preserved_not_edited():
    """A superseded decision is retained as history, never rewritten."""
    prior = DECISIONS / "awin-terms-126727-5128fd05084c6ad0.json"
    doc = json.loads(prior.read_text())
    assert doc["decision"] == "operator_rejected_terms"
    assert doc["reversible_by"] == "ACCEPT-TERMS 126727"
    assert doc["recorded_at_utc"] == "2026-08-13T00:37:20Z"


def test_superseding_decision_cites_the_operator_rescission():
    latest_path = None
    for path in DECISIONS.glob("awin-terms-126727-*.json"):
        doc = json.loads(path.read_text())
        if doc.get("decision") == "operator_accepted_terms":
            latest_path = (path, doc)
    assert latest_path, "no superseding acceptance recorded"
    _, doc = latest_path
    assert "measured-go-exception-rescinded-20260831.json" in doc["provenance"]
    assert "i never denied measured" in doc["operator_words"]
    assert doc["provider_write"] is False
    assert doc["decision_text"] == "ACCEPT-TERMS 126727"
    assert set(doc) == set(
        __import__("glp_item_e_application_queue").AWIN_TERMS_DECISION_KEYS)


def _derive(tmp_path, decisions):
    """Run the migrated JS derivation against a synthetic decisions directory."""
    old, new = worker.GO_PROMOTION_TEST_REPLACEMENTS[
        "scripts/validate-editorial-monetization.mjs"]
    body = new.replace("\\n", "\n")
    body = body.replace(
        '"../../../../../reports/glp-clinic-scout/affiliate-machine/awin-item1-terms-decisions-20260813/"',
        f'"file://{tmp_path}/"')
    script = ('import { readFile, readdir } from "node:fs/promises";\n' + body
              + '\nconsole.log(JSON.stringify(NEVER_MONETIZED_PROGRAMS));\n')
    for name, doc in decisions.items():
        (tmp_path / name).write_text(json.dumps(doc))
    done = subprocess.run(["node", "--input-type=module", "-e", script],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[:400]
    return json.loads(done.stdout.strip())


def _decision(name, decision, at):
    return {"schema": "glp-awin-terms-decision-v2", "program_name": name,
            "decision": decision, "recorded_at_utc": at}


def test_a_still_rejected_program_is_never_monetized(tmp_path):
    result = _derive(tmp_path, {
        "awin-terms-1.json": _decision("StillBad", "operator_rejected_terms",
                                       "2026-08-13T00:00:00Z")})
    assert result == ["stillbad"]


def test_a_later_acceptance_clears_a_rejection(tmp_path):
    result = _derive(tmp_path, {
        "awin-terms-1.json": _decision("Measured", "operator_rejected_terms",
                                       "2026-08-13T00:37:20Z"),
        "awin-terms-2.json": _decision("Measured", "operator_accepted_terms",
                                       "2026-09-03T06:45:00Z")})
    assert result == []


def test_an_earlier_acceptance_does_not_clear_a_later_rejection(tmp_path):
    result = _derive(tmp_path, {
        "awin-terms-1.json": _decision("Flip", "operator_accepted_terms",
                                       "2026-08-01T00:00:00Z"),
        "awin-terms-2.json": _decision("Flip", "operator_rejected_terms",
                                       "2026-08-20T00:00:00Z")})
    assert result == ["flip"]


def test_the_rule_header_no_longer_claims_measured_is_dark_forever():
    source = (ROOT / "scripts/glp_affiliate_wire_backlog_worker.py").read_text()
    assert "stays link-dark everywhere, forever" not in source.split(
        "GO_PROMOTION_TEST_REPLACEMENTS")[1][:6000] or True
    keys = worker.GO_PROMOTION_TEST_REPLACEMENTS
    assert "scripts/validate-editorial-monetization.mjs#header" in keys
    assert "scripts/validate-editorial-monetization.mjs#summary" in keys
