#!/usr/bin/env python3
"""A /go request with a WITHHELD authority is HELD, never activated.

Operator ruling 2026-09-03. apply_first_party_go_route_provision used to flip
profiles.surfaces.go = true and go.entry_state = "instrumented_redirect_candidate"
from the queue request alone, without consulting the governed promotion
authority. Measured came out of a build in a state its own authority
contradicts - authority WITHHELD (runtime_network_authority_missing) while the
registry claimed a /go routing - and validate-provider-fastpath-plus5.mjs
matched neither branch and asserted the row link-dark.

Held-by-default now lives at the root: the consumer records the request HELD
with a named reason and never touches the surface. The request is PRESERVED,
never deleted, so it activates the day the authority is granted.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import glp_affiliate_wire_backlog_worker as worker  # noqa: E402

QUARANTINE = ROOT / "site-system/flagship-v2/candidates/quarantine"


def _request(program_id="measured"):
    return {
        "status": "STAGED_NOT_DEPLOYED",
        "queue_kind": "affiliate_go_route_provision",
        "queue_source": "sheet_update_queue:payout_evaluation",
        "deploy_instruction_version": worker.GO_ROUTE_PROVISION_INSTRUCTION_VERSION,
        "sheet_updates_via_single_writer_only": True,
        "href_config_inspection_only": True,
        "tracking_clicks": 0,
        "site_program_id": program_id,
        "first_party_go_route": f"/go/{program_id}",
    }


def _tree(tmp_path, programs):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / worker.GO_RUNTIME_PROMOTION_AUTHORITY_RELATIVE).write_text(
        json.dumps({"schema": "glp.go-runtime-promotion-authority.v1",
                    "programs": programs}))
    return tmp_path


def test_withheld_authority_holds_the_request_and_activates_nothing(tmp_path):
    tree = _tree(tmp_path, {"measured": {
        "status": "WITHHELD", "promotion_authorized": False,
        "reason": "runtime_network_authority_missing"}})
    request = _request()
    result = worker.apply_first_party_go_route_provision(tree, request)
    assert result["status"] == "PASS_HELD_GO_ROUTE_PROMOTION_AUTHORITY_WITHHELD"
    assert result["surface_activated"] is False
    assert result["entry_state"] == "held"
    assert result["reason"] == "runtime_network_authority_missing"
    assert result["request_preserved"] is True
    # the request itself is untouched - it activates when the authority is granted
    assert request["status"] == "STAGED_NOT_DEPLOYED"
    assert result["external_effects"]["tracking_clicks"] == 0


def test_absent_authority_is_also_held(tmp_path):
    """Held-by-default means ABSENT is withheld, not permitted."""
    tree = _tree(tmp_path, {})
    result = worker.apply_first_party_go_route_provision(tree, _request())
    assert result["status"] == "PASS_HELD_GO_ROUTE_PROMOTION_AUTHORITY_WITHHELD"
    assert result["authority_status"] == "ABSENT"
    assert result["surface_activated"] is False


def test_unreadable_authority_is_held_not_permitted(tmp_path):
    (tmp_path / "data").mkdir(parents=True)
    result = worker.apply_first_party_go_route_provision(tmp_path, _request())
    assert result["surface_activated"] is False
    assert result["reason"] == "go_runtime_promotion_authority_unreadable"


def test_measured_preserved_tree_is_the_real_fixture():
    """Measured's own quarantined authority must resolve to WITHHELD."""
    trees = sorted(QUARANTINE.glob("glp-b2-wire-backlog-*/data/go-runtime-promotion-authority.json"))
    if not trees:
        pytest.skip("no quarantined tree on disk")
    programs = json.loads(trees[-1].read_text()).get("programs") or {}
    measured = programs.get("measured")
    if measured is None:
        pytest.skip("measured absent from this tree's authority")
    assert measured.get("promotion_authorized") is False
    status = worker._go_promotion_authority_status(
        trees[-1].parent.parent, _request())
    assert status["promotion_authorized"] is False
    assert status["reason"] == measured.get("reason")


def test_the_consumer_consults_the_authority_before_touching_the_surface():
    """Structural: the guard must precede every surface write."""
    source = (ROOT / "scripts/glp_affiliate_wire_backlog_worker.py").read_text()
    body = source[source.index("def apply_first_party_go_route_provision("):]
    body = body[:body.index("\ndef ", 10)]
    guard = body.index("_go_promotion_authority_status(")
    for writer in ('surfaces["go"] = True', 'row["go"]["entry_state"] = "instrumented_redirect_candidate"'):
        assert guard < body.index(writer), f"{writer} runs before the authority guard"
