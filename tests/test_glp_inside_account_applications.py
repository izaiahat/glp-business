"""The inside-account routes: the authorization gate, the fit gate, the dedupe.

These cover the 2026-08-19 lane that applies to Impact per-advertiser doors and
Everflow white-label brand portals under standing operator authorization
`OA-20260818-impact-everflow-per-advertiser`.

The property that matters most is the FIRST one tested: without an active,
route-naming authorization record on disk, both lanes refuse. A standing grant
that is not load-bearing is decoration.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path("/home/ubuntu/business")
for path in (str(ROOT), str(ROOT / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import glp_application_dedupe as dedupe  # noqa: E402
import glp_operator_authorizations as auth  # noqa: E402
import glp_everflow_portal_apply as everflow  # noqa: E402
import glp_impact_program_apply as impact  # noqa: E402
import glp_application_machine as machine  # noqa: E402

AUTH_ID = "OA-20260818-impact-everflow-per-advertiser"


# --------------------------------------------------------------- the gate

def test_the_live_authorization_covers_both_routes_and_is_active():
    for route in ("impact_per_advertiser", "everflow_brand_portal"):
        record = auth.authorization_for(route)
        assert record is not None, route
        assert record["authorization_id"] == AUTH_ID
        assert record["state"] == "active"
        assert record["revoked_at_utc"] is None


def test_the_authorization_carries_the_operators_verbatim_words():
    record = auth.authorization_for("impact_per_advertiser")
    verbatim = record["verbatim"]
    assert "i fully approve all applications going out" in verbatim
    assert "even tho we were rejected on the main advertiser portal" in verbatim


def test_the_authorization_states_what_it_does_NOT_permit():
    """An authorization with no stated boundary is an assumption, not a grant."""
    record = auth.authorization_for("impact_per_advertiser")
    forbidden = " ".join(record["does_not_permit"]).lower()
    for phrase in ("tax identity", "interview", "id upload", "banking",
                   "credentials we do not already hold", "outside the glp"):
        assert phrase in forbidden or phrase.split()[0] in forbidden, phrase


def test_an_unknown_route_is_refused_with_a_named_reason():
    assert auth.authorization_for("some_route_nobody_granted") is None
    assert auth.refusal_for("some_route_nobody_granted") == "operator_authorization_absent"


def test_a_revoked_record_stops_authorizing(tmp_path):
    record = json.loads((ROOT / "reports/glp-clinic-scout/affiliate-machine"
                         / "operator-authorizations"
                         / "2026-08-18-impact-everflow-per-advertiser.json").read_text())
    record["state"] = "revoked"
    record["revoked_at_utc"] = "2026-09-01T00:00:00Z"
    (tmp_path / "x.json").write_text(json.dumps(record))
    assert auth.authorization_for("impact_per_advertiser", tmp_path) is None
    assert "revoked" in auth.refusal_for("impact_per_advertiser", tmp_path)


def test_a_record_missing_its_boundary_is_not_honoured(tmp_path):
    record = json.loads((ROOT / "reports/glp-clinic-scout/affiliate-machine"
                         / "operator-authorizations"
                         / "2026-08-18-impact-everflow-per-advertiser.json").read_text())
    record.pop("does_not_permit")
    (tmp_path / "x.json").write_text(json.dumps(record))
    assert auth.authorization_for("impact_per_advertiser", tmp_path) is None


def test_an_empty_authorization_directory_refuses_both_routes(tmp_path):
    for route in ("impact_per_advertiser", "everflow_brand_portal"):
        assert auth.authorization_for(route, tmp_path) is None
        assert auth.refusal_for(route, tmp_path) == "operator_authorization_absent"


# ----------------------------------------------------------- the fit gate

@pytest.mark.parametrize("tier,expected", [
    ("first-tier GLP-1", True),
    ("GLP-1", True),
    ("telehealth", True),
    ("GLP-1 adjacent + Noom Med", True),
    ("mental health (sister)", False),
    ("diagnostics (sister)", False),
    ("womens health", False),
    ("weight-loss app (sister)", False),
    ("", False),
])
def test_impact_fit_gate_keeps_the_niche(tier, expected):
    fit, _ = impact.fit_verdict({"glp_tier": tier})
    assert fit is expected


def test_the_operator_authorized_volume_within_scope_not_indiscriminately():
    """Off-niche doors are SKIPPED AND NAMED, never applied to."""
    doors = impact.manifest_doors()
    off = [d["brand"] for d in doors if not impact.fit_verdict(d)[0]]
    assert set(off) == {"Everlywell", "BetterHelp", "Willow", "Simple App"}


# ------------------------------------------------- never re-apply to a live one

def test_everflow_manifest_refuses_every_relationship_we_already_earn_from():
    """Orderly Meds, ShedRx, Direct Meds, GobyMeds and Found are already wired."""
    by_brand = {d["brand"]: d for d in everflow.manifest_doors()}
    for brand in ("Orderly Meds", "ShedRx", "Direct Meds", "GobyMeds", "Found"):
        assert everflow.already_live(by_brand[brand]), brand


def test_impact_manifest_refuses_the_already_live_and_already_declined():
    by_brand = {d["brand"]: d for d in impact.manifest_doors()}
    assert impact.manifest_refusal(by_brand["Found"])          # live on Katalys
    assert impact.manifest_refusal(by_brand["Willow"])         # already declined on Impact


# ------------------------------------------------------------- the dedupe fix

def test_a_whitelabel_tenant_host_keys_on_the_TENANT_not_the_platform():
    """`eden.` and `ro.` must not collapse into one `everflowclient.io` identity.

    This is the bug that made Reverse Health and Wisp - which have no Sheet row
    at all - look `sheet_already_engaged`, because Sheet row 410 (Digital Media
    Solutions) signs up at `digitalmediasolutions.everflowclient.io`.
    """
    assert dedupe.domain_key("https://eden.everflowclient.io/affiliate/signup") \
        == "eden.everflowclient.io"
    assert dedupe.domain_key("https://ro.everflowclient.io/affiliate/signup") \
        == "ro.everflowclient.io"
    assert dedupe.domain_key("https://eden.everflowclient.io/x") \
        != dedupe.domain_key("https://ro.everflowclient.io/x")


def test_the_platform_apex_itself_names_no_merchant():
    assert dedupe.domain_key("https://everflowclient.io/") == ""
    assert dedupe.domain_key("https://www.everflowclient.io") == ""


def test_ordinary_merchant_domains_are_unchanged_by_the_tenant_rule():
    assert dedupe.domain_key("https://www.healthrx.com/affiliates") == "healthrx.com"
    assert dedupe.domain_key("https://my.gobymeds.com/affiliate/register") == "gobymeds.com"
    assert dedupe.domain_key("https://track.revoffers.com/aff_c") == ""


# --------------------------------------------------- follow-up, narrowly scoped

def _index_with(rows):
    index = dedupe.DedupeIndex()
    names, pairs, domains, brands, collisions, follow = {}, {}, {}, {}, {}, {}
    dedupe._engaged_row_identities(rows, "test", names, pairs, domains, brands,
                                   collisions, follow)
    index.add(dedupe.LEG_SHEET, names, pairs, {"ok": True}, domains, brands, follow)
    return index


def test_unanswered_outreach_can_be_followed_up_through_a_different_door():
    index = _index_with([{"name": "Eden", "network": "Direct/contact verify",
                          "status": "partnership_form_submitted_awaiting_response",
                          "sheet_row": 32}])
    assert index.verdict("Eden", "everflow", "Eden") is not None
    assert index.verdict("Eden", "everflow", "Eden", allow_follow_up=True) is None


def test_a_real_relationship_is_never_released_by_allow_follow_up():
    for status in ("applied_pending_brand_approval", "approved_active_wired_live",
                   "live_wired_verified", "provider_rejected", "approved_active_not_wired"):
        index = _index_with([{"name": "Acme Health", "network": "Katalys",
                              "status": status, "sheet_row": 1}])
        assert index.verdict("Acme Health", "everflow", "Acme Health",
                             allow_follow_up=True) is not None, status


def test_one_real_relationship_anywhere_on_the_key_blocks_the_whole_key():
    index = _index_with([
        {"name": "Eden", "network": "Direct", "sheet_row": 32,
         "status": "partnership_form_submitted_awaiting_response"},
        {"name": "Eden", "network": "Katalys", "sheet_row": 999,
         "status": "approved_active_wired_live"},
    ])
    assert index.verdict("Eden", "everflow", "Eden", allow_follow_up=True) is not None


def test_the_sheet_citation_names_the_row_and_the_status():
    """"an engaged row carries this program name" is not actionable; this is."""
    index = _index_with([{"name": "Eden", "network": "Direct/contact verify",
                          "status": "partnership_form_submitted_awaiting_response",
                          "sheet_row": 32}])
    verdict = index.verdict("Eden", "everflow", "Eden")
    assert "row 32" in verdict["evidence"]
    assert "partnership_form_submitted_awaiting_response" in verdict["evidence"]


# ---------------------------------------------------------------- truth gate

def test_the_form_answers_we_actually_send_pass_the_truth_gate():
    profile = everflow.site_profile()
    answers = {label: everflow.fill_template(value, profile)
               for label, value in everflow.ANSWERS_STATIC.items()}
    assert everflow.truth_check(answers) == []


def test_the_form_truth_gate_blocks_a_fabricated_traffic_figure():
    assert any("fabricated_metric" in fail for fail in
               everflow.truth_check({"Describe": "We send 40,000 visitors a month."}))


def test_the_form_truth_gate_blocks_a_ranking_claim():
    assert any("unverifiable_ranking" in fail for fail in
               everflow.truth_check({"Describe": "We are the #1 GLP comparison site."}))


def test_the_form_truth_gate_blocks_the_forbidden_voice():
    assert any("forbidden_voice" in fail for fail in
               everflow.truth_check({"Describe": "We are early stage."}))


def test_the_form_gate_is_not_the_email_gate_and_does_not_import_its_shape():
    """An honest one-word form answer must not fail for having no subject line."""
    assert everflow.truth_check({"City": "Sacramento"}) == []


# ------------------------------------------------- required-field solvability

def test_a_required_tax_identity_field_refuses_the_whole_door():
    """Fella Health makes `Tax iD/VAT or SSN` mandatory - outside the grant."""
    plan = everflow.plan_answers(
        [{"label": "Tax iD/VAT or SSN", "required": True}], everflow.site_profile())
    assert plan["refusals"] == ["Tax iD/VAT or SSN:requires_operator_tax_identity"]


def test_the_same_field_optional_is_simply_left_blank():
    plan = everflow.plan_answers(
        [{"label": "Tax ID / VAT or SSN", "required": False}], everflow.site_profile())
    assert plan["refusals"] == []
    assert "Tax ID / VAT or SSN" in plan["blanks"]


def test_a_required_field_with_no_truthful_answer_is_unanswerable_not_guessed():
    plan = everflow.plan_answers(
        [{"label": "Your Dun & Bradstreet number", "required": True}],
        everflow.site_profile())
    assert plan["unanswerable"] == ["Your Dun & Bradstreet number"]
    assert plan["answers"] == {}


def test_a_required_social_following_field_refuses_rather_than_states_a_number():
    plan = everflow.plan_answers(
        [{"label": "What is your total social media following?", "required": True}],
        everflow.site_profile())
    assert plan["refusals"] == [
        "What is your total social media following?:would_require_stating_an_audience_figure"]


def test_every_answer_is_a_published_business_fact():
    profile = everflow.site_profile()
    assert profile["business_name"] == "Tilton Group LLC"
    assert profile["website"] == "https://glpclinicscout.com"
    assert everflow.PUBLISHED_ADDRESS["city"] == "Sacramento"
    assert everflow.PUBLISHED_ADDRESS["zip"] == "95816"


# ------------------------------------------------------- daily machine wiring

def test_the_daily_machine_knows_the_inside_account_path():
    assert "inside_account" in machine.APPLICATION_PATHS
    assert machine.path_counts([])["inside_account"] == 0


def test_the_daily_machine_reports_both_routes_as_authorized_with_their_lanes():
    routes = machine.inside_account_routes()["routes"]
    assert set(routes) == {"impact_per_advertiser", "everflow_brand_portal"}
    for route, entry in routes.items():
        assert entry["authorized"] is True
        assert entry["authorization"]["authorization_id"] == AUTH_ID
        assert Path(ROOT / entry["lane"]).is_file()
        assert entry["doors_available"] > 0


def test_the_daily_machine_counts_the_impact_doors_that_pass_the_fit_gate():
    entry = machine.inside_account_routes()["routes"]["impact_per_advertiser"]
    assert entry["doors_available"] == 19
    assert entry["doors_in_niche"] == 15


def test_the_lane_scripts_refuse_outright_without_an_authorization(monkeypatch, tmp_path):
    """Both CLIs exit non-zero and say why, rather than opening a browser."""
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path)
    assert everflow.main(["--plan"]) == 2
    assert impact.main(["--plan"]) == 2


# ----------------------------------------------------------- queue-row shape

def test_only_verified_submissions_become_queue_rows(tmp_path):
    import glp_everflow_apply_queue as queue  # noqa: PLC0415

    receipt = tmp_path / "everflow-apply-run.json"
    receipt.write_text(json.dumps({
        "run_id": "t",
        "authorization": {"authorization_id": AUTH_ID},
        "plan": [{"brand": "A", "sheet_rows": []}, {"brand": "B", "sheet_rows": []},
                 {"brand": "C", "sheet_rows": []}],
        "results": [
            {"brand": "A", "outcome": "submitted_verified", "url": "https://a/x"},
            {"brand": "B", "outcome": "blocked", "url": "https://b/x"},
            {"brand": "C", "outcome": "submission_unverified", "url": "https://c/x"},
        ],
    }))
    rows = queue.rows_from(receipt)
    assert [row["program_id"] for row in rows] == ["A"]
    assert rows[0]["kind"] == "application_pending"
    assert rows[0]["status"] == "queued"


def test_a_created_row_carries_the_sheet_vertical_not_a_lane_name(tmp_path):
    import glp_everflow_apply_queue as queue  # noqa: PLC0415

    receipt = tmp_path / "everflow-apply-run.json"
    receipt.write_text(json.dumps({
        "run_id": "t", "authorization": {"authorization_id": AUTH_ID},
        "plan": [{"brand": "A", "sheet_rows": []}],
        "results": [{"brand": "A", "outcome": "submitted_verified", "url": "https://a/x"}],
    }))
    cells = json.loads(queue.rows_from(receipt)[0]["evidence"])["row_cells"]
    assert cells["25"] == "glp1-weightloss"
    assert cells["4"] == "Everflow"


def test_the_queue_row_never_carries_a_column_the_datatable_does_not_have(tmp_path):
    """An unknown top-level column makes n8n reject the whole insert."""
    import glp_everflow_apply_queue as queue  # noqa: PLC0415

    receipt = tmp_path / "everflow-apply-run.json"
    receipt.write_text(json.dumps({
        "run_id": "t", "authorization": {"authorization_id": AUTH_ID},
        "plan": [{"brand": "A", "sheet_rows": []}],
        "results": [{"brand": "A", "outcome": "submitted_verified", "url": "https://a/x"}],
    }))
    allowed = {"update_id", "kind", "network", "program_id", "program_name",
               "sheet_row", "status", "queued_by", "queued_at_utc",
               "resolved_at_utc", "note", "evidence"}
    assert set(queue.rows_from(receipt)[0]) <= allowed
