from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BUSINESS = Path("/home/ubuntu/business")
sys.path.insert(0, str(BUSINESS))
sys.path.insert(0, str(BUSINESS / "scripts"))

import affiliate_best_terms_machine as machine  # noqa: E402
import glp_new_approval_research_queue as new_approval_research  # noqa: E402
from inbox_control_plane import control_plane as cp  # noqa: E402

DRAFTER_PATH = BUSINESS / "tools/fable/email_drafter.py"
spec = importlib.util.spec_from_file_location("best_terms_fable_drafter_test", DRAFTER_PATH)
drafter = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(drafter)


def test_ro_whitelabel_row_enters_comparison_without_wire_readiness(monkeypatch):
    monkeypatch.setattr(machine, 'payout_matrix_evidence', lambda: {})
    row = machine.sheet_writer.pad([])
    row[2] = 'Roman Health Ventures Inc.'
    row[4] = 'Ro Everflow whitelabel (tenant 4131)'
    row[5] = 'https://ro.everflowclient.io/offers/1'
    row[6] = 'https://ro.co/'
    row[10] = 'approved_active_not_wired'
    row[16] = '$30 USD CPA; paid conversion event: AddToCart'
    offers = machine.sheet_offers([machine.sheet_writer.HEADERS, row])
    assert len(offers) == 1
    assert offers[0]['network'] == 'roeverflowwhitelabeltenant4131'
    assert offers[0]['program_id'] == '1'
    assert offers[0]['headline_payout'] == 30
    assert offers[0]['wire_in_ready'] is False
    row[5] = 'https://another.everflowclient.io/offers/1'
    assert machine.program_id_from_row(row) == ''


def test_ro_sheet_approval_triggers_once_without_legacy_receipt(tmp_path):
    row = machine.sheet_writer.pad([])
    row[2] = 'Roman Health Ventures Inc.'
    row[4] = 'Ro Everflow whitelabel (tenant 4131)'
    row[5] = 'https://ro.everflowclient.io/offers/1'
    row[10] = 'approved_active_not_wired'
    row[16] = '$30 USD CPA; paid conversion event: AddToCart'
    values = [machine.sheet_writer.HEADERS, row]
    found = machine.approval_receipts_since_cursor({}, tmp_path, values=values)
    assert len(found) == 1
    assert found[0]['payload']['row_number'] == 2
    assert found[0]['payload']['source'] == 'governed_sheet_ro_tenant_approval'
    cursor = {'processed_receipts': [found[0]['path']]}
    assert machine.approval_receipts_since_cursor(cursor, tmp_path, values=values) == []
    row[10] = 'cancelled_by_network'
    assert machine.approval_receipts_since_cursor({}, tmp_path, values=values) == []


def test_approval_rerank_uses_current_catalog_and_rejects_stale_sheet_offer(monkeypatch, tmp_path):
    # The approval producer has a durable live failure ledger; keep this
    # isolated test independent of a concurrent natural cron run.
    monkeypatch.setattr(machine, 'FAILURE_STATE', tmp_path / 'failure.json')
    row = machine.sheet_writer.pad([])
    row[2], row[4] = 'Roman Health Ventures Inc.', 'Ro Everflow whitelabel (tenant 4131)'
    row[5], row[10] = 'https://ro.everflowclient.io/offers/1', 'approved_active_not_wired'
    row[16] = '$30 USD CPA; paid conversion event: AddToCart'
    monkeypatch.setattr(machine, 'RUNS_ROOT', tmp_path / 'runs')
    monkeypatch.setattr(machine, 'read_live_sheet', lambda: [machine.sheet_writer.HEADERS, row])
    monkeypatch.setattr(machine, 'APPROVAL_CURSOR', tmp_path / 'cursor.json')
    monkeypatch.setattr(machine, 'approval_receipts_since_cursor', lambda *a, **k: [{'payload': {'row_number': 2}}])
    catalog = tmp_path / 'offers.json'
    catalog.write_text(json.dumps({'offers': [{'offer_id': '999', 'name': 'Other Clinic',
        'payout_type': 'CPL', 'payout_amount': 5, 'tracking_url': 'https://example.test/click'}],
        'completeness': {'verdict': 'COMPLETE', 'enumerated': 1, 'reported_total': 1}}))
    calls = []
    def current(_):
        calls.append('current')
        return catalog, {'completeness': 'COMPLETE', 'enumerated': 1, 'reported_total': 1}
    monkeypatch.setattr(machine, 'run_xentraffic_census', current)
    monkeypatch.setattr(machine, 'latest_xentraffic_catalog', lambda *a: pytest.fail('historical fallback'))
    monkeypatch.setattr(machine, 'sheet_offers', lambda _: [{'provider_name': 'Ro',
        'network': 'xentraffic', 'program_id': '3164', 'headline_payout': 20,
        'payout_model': 'CPL', 'relationship': 'approved', 'wire_in_ready': True,
        'source': 'live_sheet', 'terms_source': 'sheet'}])
    monkeypatch.setattr(machine, 'application_ledger_offers', lambda: [])
    monkeypatch.setattr(machine, 'inventory_programs', lambda _: [])
    class Compared(Exception):
        pass
    def compare(offers):
        stale = next(o for o in offers if o['program_id'] == '3164')
        assert stale['headline_payout'] == 20
        assert stale['relationship'] == 'unknown'
        assert stale['wire_in_ready'] is False
        assert stale['availability'] == 'not_in_current_authenticated_catalog'
        assert next(o for o in offers if o['program_id'] == '999')['wire_in_ready'] is True
        raise Compared()
    monkeypatch.setattr(machine, 'selection_with_offers', compare)
    with pytest.raises(Compared):
        machine.run_machine(first_run=False, approval_events=True, stage=False,
                            send_alert=False, census_dir=tmp_path)
    assert calls == ['current']


def winner(network: str, program_id: str) -> dict:
    return {
        "network": network,
        "program_id": program_id,
        "payout": 200,
        "payout_model": "CPA",
        "wire_in_ready": True,
    }


def test_single_best_terms_config_has_weekly_priors_performance_and_runtime() -> None:
    config = machine.SELECTION_CONFIG
    assert machine.BEST_TERMS_CONFIG == (
        BUSINESS / "config/affiliate-sites/best-terms-selection.json")
    assert config["rerank"] == {
        "cadence": "weekly", "window": "iso_week", "one_run_per_window": True,
    }
    assert config["priors"]["cpa_sale"]["conversion_rate_per_click"] == "0.015"
    assert config["priors"]["cpl_lead"]["conversion_rate_per_click"] == "0.08"
    assert config["performance_path"] == "data/affiliate-payout-performance.json"
    assert config["collector_runtime"]["interpreter"] == (
        "/home/ubuntu/.hermes/venvs/glp-maxbounty-runtime/bin/python")
    assert set(config["collector_runtime"]["required_imports"]) == {
        "requests", "playwright.sync_api", "google.oauth2.credentials",
        "googleapiclient.discovery",
    }


def test_collector_dependency_preflight_fails_loud(tmp_path: Path) -> None:
    interpreter = tmp_path / "python"
    interpreter.write_text("#!/bin/sh\nexit 1\n")
    interpreter.chmod(0o700)
    calls = []

    def failed(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=1, stderr="No module named playwright", stdout="")

    with pytest.raises(RuntimeError, match="collector_dependency_preflight_failed"):
        machine.collector_runtime_preflight(
            interpreter, ["requests", "playwright.sync_api"], runner=failed)
    assert len(calls) == 1
    assert calls[0][0][0] == str(interpreter)
    assert json.loads(calls[0][0][3]) == ["requests", "playwright.sync_api"]


def test_collector_launch_failure_is_not_downgraded_to_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        machine, "collector_runtime_preflight",
        lambda: {"status": "PASS", "interpreter": str(machine.COLLECTOR_INTERPRETER)},
    )
    monkeypatch.setattr(
        machine.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=2, stderr="collector import exploded", stdout=""),
    )
    with pytest.raises(RuntimeError, match="marketplace_supply_sweep_failed_loud"):
        machine.run_supply_census(tmp_path)


def test_weekly_window_guard_rejects_duplicate_run(tmp_path: Path) -> None:
    moment = dt.datetime(2026, 8, 26, 12, tzinfo=dt.timezone.utc)
    first = machine.claim_weekly_window(tmp_path, moment=moment)
    duplicate = machine.claim_weekly_window(tmp_path, moment=moment + dt.timedelta(days=2))
    next_window = machine.claim_weekly_window(tmp_path, moment=moment + dt.timedelta(days=7))
    assert first["claimed"] is True
    assert duplicate["duplicate"] is True
    assert duplicate["window_id"] == first["window_id"]
    assert next_window["claimed"] is True
    assert next_window["window_id"] != first["window_id"]


def test_duplicate_weekly_window_exits_before_collectors_or_sheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine, "claim_weekly_window", lambda: {
        "claimed": False, "duplicate": True, "window_id": "2026-W35",
        "marker": "fixture", "existing": {"window_id": "2026-W35"},
    })
    monkeypatch.setattr(
        machine, "read_live_sheet",
        lambda: pytest.fail("duplicate window must exit before Sheet read"),
    )
    result = machine.run_machine(
        first_run=False, approval_events=False, stage=False, send_alert=False)
    assert result["status"] == "PASS_DUPLICATE_WINDOW"
    assert result["effects"] == {
        "deploys": 0, "live_link_changes": 0, "repoints": 0,
    }


def test_baseline_xentraffic_catalog_preserves_all_41_offers_and_28_brands() -> None:
    offers = machine.xentraffic_offers(machine.CLINIC_FITS)
    assert len(offers) == 41
    assert len({machine.slug(row["provider_name"]) for row in offers}) == 28
    assert all(row["relationship"] == "active" for row in offers)
    assert all(row["wire_in_ready"] is True for row in offers)


def test_full_xentraffic_catalog_excludes_sister_vertical_false_matches() -> None:
    full = machine.XENTRAFFIC_BASELINE / "catalog-normalized.json"
    offers = machine.xentraffic_offers(full)
    assert len(offers) == 41
    assert all(row["program_id"] != "3249" for row in offers)
    ro = next(row for row in offers if row["program_id"] == "3164")
    assert ro["payout_model"] == "CPL"


def test_supplied_xentraffic_slice_proves_the_full_authenticated_denominator() -> None:
    summary = machine.supplied_xentraffic_summary(machine.CLINIC_FITS)
    assert summary["completeness"] == "COMPLETE"
    assert summary["enumerated"] == 381
    assert summary["reported_total"] == 381
    assert summary["selection_receipt"].endswith("clinic-fits.json")


def test_all_18_duplicate_decisions_have_exact_winners() -> None:
    decisions = machine.baseline_comparison_decisions()
    assert len(decisions) == 18
    assert all(row["winner"]["network"] and row["winner"]["program_id"] for row in decisions)
    medvi = next(row for row in decisions if row["provider"] == "MEDVI")
    assert medvi["winner"]["program_id"] == "2862"
    assert medvi["winner"]["payout"] == 260


def test_comparison_decision_uses_current_policy_winner_and_live_state() -> None:
    selection = {"providers": [{
        "provider_name": "Breeze Meds",
        "selected_winner": {
            "network": "xentraffic",
            "program_id": "3151",
            "payout": 190,
            "payout_model": "CPA",
            "wire_in_ready": True,
            "wired_live": False,
            "sheet_row": 104,
        },
    }]}
    decisions = machine.baseline_comparison_decisions(selection)
    breeze = next(row for row in decisions if row["provider"] == "Breeze Meds")
    assert breeze["winner"]["program_id"] == "3151"
    assert breeze["switch_staged"] is True


def test_newly_observed_already_live_winner_does_not_stage_redundant_switch() -> None:
    current_winner = winner("katalys", "live")
    current_winner["wired_live"] = True
    current = {"providers": [{
        "provider_name": "Already Live Health",
        "selected_winner": current_winner,
    }]}
    assert machine.changed_winners({"providers": []}, current) == []


def test_broad_inventory_cannot_promote_a_pending_application_to_approved() -> None:
    merged = machine.merge_offers([
        {
            "provider_name": "Sesame Care", "network": "impact", "program_id": "13292",
            "relationship": "pending", "wire_in_ready": False,
            "source": "applications-ledger.json",
        },
        {
            "provider_name": "Sesame Care", "network": "impact", "program_id": "13292",
            "relationship": "approved", "wire_in_ready": False,
            "source": "impact-inventory.json",
        },
    ])
    assert len(merged) == 1
    assert merged[0]["relationship"] == "pending"


def test_api_terms_outrank_portal_and_email_with_48h_risk_recheck() -> None:
    base = {
        "provider_name": "Fixture Health", "official_url": "https://fixture.example",
        "network": "impact", "program_id": "123", "relationship": "pending",
        "wire_in_ready": False, "payout_model": "CPA", "currency": "USD",
    }
    merged = machine.merge_offers([
        {**base, "headline_payout": 100, "terms_source": "portal", "source": "portal"},
        {**base, "headline_payout": 120, "terms_source": "email", "source": "email"},
        {**base, "headline_payout": 200, "terms_source": "api", "source": "api"},
    ])
    assert len(merged) == 1
    row = merged[0]
    assert row["headline_payout"] == 200
    assert row["terms_authority"] == "api"
    assert row["relationship"] == "pending"
    assert any(
        item["field"] == "headline_payout"
        and item["authoritative_source"] == "api"
        and item["authoritative_value"] == 200
        for item in row["terms_contradictions"]
    )
    risk = row["risk_acceptance"]
    assert risk["status"] == "ACCEPT_API_TRUTH_PENDING_RECHECK"
    assert risk["authority_rule"] == "api_out_ranks_portal_and_email"
    accepted = dt.datetime.fromisoformat(risk["accepted_at_utc"].replace("Z", "+00:00"))
    recheck = dt.datetime.fromisoformat(risk["recheck_after_utc"].replace("Z", "+00:00"))
    assert recheck - accepted == dt.timedelta(hours=48)


def test_marketplace_suffix_does_not_split_hume_health_cross_network_offers() -> None:
    offers = machine.merge_offers([
        {
            "provider_name": "Hume Health US", "official_url": "https://humehealth.com/",
            "network": "awin", "program_id": "102781", "relationship": "approved",
            "wire_in_ready": True, "payout_model": "CPA", "headline_payout": 40,
        },
        {
            "provider_name": "Hume Health", "official_url": "https://humehealth.com/",
            "network": "cj", "program_id": "7865389", "relationship": "approved",
            "wire_in_ready": True, "payout_model": "CPA", "headline_payout": 30,
        },
    ])

    selection = machine.selection_with_offers(offers)

    assert machine.clean_provider_name("Hume Health US") == "Hume Health"
    assert len(selection["providers"]) == 1
    provider = selection["providers"][0]
    assert provider["provider_name"] == "Hume Health"
    assert {(row["network"], row["program_id"]) for row in provider["offers"]} == {
        ("awin", "102781"), ("cj", "7865389"),
    }
    assert provider["selected_winner"]["network"] == "awin"
    assert provider["selected_winner"]["program_id"] == "102781"


def test_authenticated_program_identity_groups_all_three_gala_rails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine, "payout_matrix_evidence", lambda: {
        ("katalys", "1576"): {
            "provider": "Gala Health", "payout_model": "CPA", "headline_value": 349,
        },
        ("maxbounty", "32072"): {
            "provider": "Gala Health", "payout_model": "CPA/sale", "headline_value": 225,
        },
    })
    headers = [""] * 27
    katalys = [""] * 27
    katalys[2] = "Gala Health"
    katalys[4] = "Katalys"
    katalys[5] = "https://app.katalys.com/explore/1576"
    katalys[10] = "live_wired_verified"
    katalys[14] = "https://tracking.example/katalys-gala"
    katalys[16] = "Default: $349; HRT: $164; Microdosing: $182"
    katalys[19] = True
    katalys[20] = True
    katalys[21] = "live_direct_link_verified"
    maxbounty = [""] * 27
    maxbounty[2] = "Gala - GLP-1 - CPS (US)"
    maxbounty[4] = "MaxBounty"
    maxbounty[5] = "https://affiliates.maxbounty.com/campaign/32072"
    maxbounty[10] = "approved_active_not_wired"
    maxbounty[16] = "Sale Fixed $225.00 per lead"
    xen = {
        "provider_name": "Gala Health", "network": "xentraffic", "program_id": "3178",
        "relationship": "active", "wire_in_ready": True, "wired_live": False,
        "tracking_link_present": True, "headline_payout": 200, "payout_model": "CPA",
        "payout_display": "$200 CPA", "currency": "USD", "traffic_eligible": True,
        "paid_event": "", "paid_event_basis": "payout_model_fallback",
    }

    sheet = machine.sheet_offers([headers, katalys, maxbounty])
    max_offer = next(row for row in sheet if row["network"] == "maxbounty")
    assert max_offer["provider_name"] == "Gala Health"

    provider = machine.selection_with_offers([*sheet, xen])["providers"][0]
    assert provider["provider_name"] == "Gala Health"
    assert {(row["network"], row["program_id"]) for row in provider["offers"]} == {
        ("katalys", "1576"), ("maxbounty", "32072"), ("xentraffic", "3178"),
    }
    assert provider["selection_basis"] == "highest_expected_earnings_per_100_clicks"
    assert provider["selected_winner"]["network"] == "katalys"
    assert provider["selected_winner"]["expected_earnings_per_100_clicks"] == 602.785
    assert provider["selected_winner"]["performance_basis"] \
        == "network_reported_epc_cvr_positive_denominator"
    assert all(row["comparable"] for row in provider["cross_rail_comparisons"])


def test_sesame_different_paid_events_are_compared_on_expected_earnings() -> None:
    impact = machine.parse_payout("GLP-1 CPA: $260")
    maxbounty = machine.parse_payout(
        "Weight loss medication subscription signup Fixed $200.00 per lead")
    offers = [
        {"provider_name": "Sesame Care", "network": "impact", "program_id": "13292",
         "relationship": "approved", "wire_in_ready": True, "currency": "USD", **impact},
        {"provider_name": "Sesame Care", "network": "maxbounty", "program_id": "725",
         "relationship": "approved", "wire_in_ready": False, "currency": "USD", **maxbounty},
    ]
    provider = machine.selection_with_offers(offers)["providers"][0]
    comparison = provider["cross_rail_comparisons"][0]
    assert impact["paid_event"] == "glp1_acquisition"
    assert maxbounty["paid_event"] == "medication_subscription_signup"
    assert comparison["comparable"] is True
    assert comparison["decision"] == "COMPARABLE_EXPECTED_EARNINGS_PER_100_CLICKS"
    assert provider["selected_winner"]["network"] == "impact"


def test_affiliati_sheet_terms_preserve_exact_paid_event_without_counting_purchase_price() -> None:
    payout = machine.parse_payout(
        "$300 CPA; paid conversion event: Converts on $89 CTC purchase by customer "
        "to the weight loss membership. cc submit"
    )

    assert payout["headline_payout"] == 300
    assert payout["payout_model"] == "CPA"
    assert payout["paid_event"] == (
        "Converts on $89 CTC purchase by customer to the weight loss membership. cc submit"
    )
    assert payout["paid_event_basis"] == "explicit_paid_conversion_event"


def test_duplicate_provider_names_collapse_across_affiliati_and_other_rails() -> None:
    assert machine.clean_provider_name("JRNYS | Affiliati") == "JRNYS"
    assert machine.clean_provider_name("TrimRX - Telehealth Weight Management - CPS (US)") == "TrimRX"
    assert machine.clean_provider_name("ShedRX - GLP-1 Microdosing - CPS (US)") == "ShedRX"


def test_sheet_offers_exposes_merchant_url_to_identity_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine, "payout_matrix_evidence", lambda: {})
    headers = [""] * 27
    row = [""] * 27
    row[2] = "Gala - GLP-1 - CPS (US)"
    row[4] = "MaxBounty"
    row[5] = "https://affiliates.maxbounty.com/campaigns/32072"
    row[6] = "https://galaglp1.com/"
    row[10] = "approved_active_not_wired"
    row[16] = "$225 CPA"
    offer = machine.sheet_offers([headers, row])[0]
    assert offer["official_url"] == "https://galaglp1.com/"
    assert machine.policy.provider_identity(offer) == "domain:galaglp1.com"


def test_merge_offers_groups_gala_by_resolved_identity_without_merging_rails() -> None:
    offers = machine.merge_offers([
        {"provider_name": "Gala Health", "official_url": "https://galaglp1.com/lp/glp1",
         "network": "Katalys", "program_id": "1576", "source": "sheet"},
        {"provider_name": "Gala - GLP-1 - CPS (US)", "official_url": "https://galaglp1.com/",
         "network": "MaxBounty", "program_id": "32072", "source": "sheet"},
        {"provider_name": "Gala Health | XenTraffic offer 3178", "official_url": "",
         "network": "XenTraffic", "program_id": "3178", "source": "sheet"},
    ])
    assert len(offers) == 3
    assert {offer["provider_identity"] for offer in offers} == {"domain:galaglp1.com"}


def test_parse_payout_marks_per_sale_exact_and_sale_fixed_per_lead_unknown() -> None:
    curex = machine.parse_payout("$20 per sale")
    gala = machine.parse_payout("Sale Fixed $225.00 per lead")
    trim = machine.parse_payout("Converts on Purchase Fixed $187.50 per lead")
    assert (curex["paid_event"], curex["paid_event_basis"]) == ("sale", "explicit_per_sale")
    assert gala["paid_event"] == ""
    assert gala["paid_event_basis"] == "payout_model_fallback"
    assert (trim["paid_event"], trim["paid_event_basis"]) == (
        "purchase", "explicit_converts_on_purchase",
    )


def test_sheet_offers_reads_explicit_conversion_event_from_compliance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine, "payout_matrix_evidence", lambda: {})
    headers = [""] * 27
    row = [""] * 27
    row[2] = "Breeze Meds"
    row[4] = "Katalys"
    row[5] = "https://app.katalys.com/explore/1518"
    row[6] = "https://breezemeds.com/"
    row[10] = "approved_active_tracking_ready"
    row[16] = "Default: $300"
    row[22] = "Additional terms: Conversion Event: Purchase"
    offer = machine.sheet_offers([headers, row])[0]
    assert (offer["paid_event"], offer["paid_event_basis"]) == (
        "purchase", "explicit_sheet_conversion_event",
    )


def test_xentraffic_does_not_inherit_katalys_paid_event_from_stale_compliance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine, "payout_matrix_evidence", lambda: {})
    headers = [""] * 27
    row = [""] * 27
    row[2] = "TrimRX | XenTraffic offer 3014"
    row[4] = "XenTraffic"
    row[5] = "https://xentraffic.everflowclient.io/offers/3014"
    row[6] = "https://trimrx.com/"
    row[10] = "approved_active_tracking_ready"
    row[16] = "CPA: $275"
    row[22] = "Katalys prohibited: Misleading Claims | Additional terms: Conversion Event: Purchase"
    offer = machine.sheet_offers([headers, row])[0]
    assert offer["paid_event"] == ""
    assert offer["paid_event_basis"] == "payout_model_fallback"


def test_not_applied_relationship_does_not_collapse_to_pending() -> None:
    assert machine.sheet_relationship("not_applied") == "notjoined"
    assert machine.sheet_relationship("not joined") == "notjoined"
    assert machine.sheet_relationship("applied_pending_brand_approval") == "pending"


def test_live_swap_uses_same_event_value_or_cross_event_expected_earnings() -> None:
    previous = {"providers": [{"provider_name": "Fixture", "selected_winner": {
        "network": "katalys", "program_id": "1", "paid_event": "glp1_acquisition",
        "currency": "USD", "effective_value": 100,
        "expected_earnings_per_100_clicks": 100, "wired_live": True,
    }}]}
    better = {"providers": [{"provider_name": "Fixture", "selected_winner": {
        "network": "awin", "program_id": "2", "paid_event": "glp1_acquisition",
        "currency": "USD", "effective_value": 120, "wired_live": False, "sheet_row": 216,
    }}]}
    different_event = {"providers": [{"provider_name": "Fixture", "selected_winner": {
        "network": "awin", "program_id": "2", "paid_event": "medication_subscription_signup",
        "currency": "USD", "effective_value": 300,
        "expected_earnings_per_100_clicks": 300,
        "wired_live": False, "sheet_row": 216,
    }}]}
    assert machine.changed_winners(previous, better)[0]["target_sheet_row"] == 216
    assert machine.changed_winners(previous, different_event)[0]["target_sheet_row"] == 216


def test_impact_tracking_link_keeps_campaign_id_after_wire_note_rewrite() -> None:
    row = [""] * 27
    row[4] = "Impact.com"
    row[14] = "https://sesame-care.pxf.io/c/7467025/1116240/13292"
    row[23] = "Wire-in gate OPENED for the Impact.com rail (immutable id 13292)."
    assert machine.program_id_from_row(row) == "13292"


def test_affiliati_terms_capture_keeps_offer_id_after_wire_note_rewrite() -> None:
    row = [""] * 27
    row[4] = "Affiliati"
    row[5] = "https://login.affiliatinetwork.com/affiliates/"
    row[22] = (
        "Terms captured verbatim at reports/glp-clinic-scout/affiliate-machine/"
        "PAYOUT-AUDIT-20260827/affiliati-43194-terms-verbatim.json."
    )
    row[23] = "Wire-in gate OPENED for the Affiliati rail (immutable id 178786)."
    assert machine.program_id_from_row(row) == "43194"


def test_selection_prefers_live_sheet_provider_label_over_catalog_mojibake() -> None:
    offers = [
        {
            "provider_name": "Gaâla", "provider_identity": "domain:galaglp1.com",
            "network": "rakuten", "program_id": "50824", "relationship": "pending",
            "wire_in_ready": False, "wired_live": False, "headline_payout": None,
            "payout_model": "unknown", "currency": "USD", "traffic_eligible": True,
        },
        {
            "provider_name": "Gala Health", "provider_identity": "domain:galaglp1.com",
            "official_url": "https://galaglp1.com/lp/glp1", "network": "katalys",
            "program_id": "1576", "relationship": "approved", "wire_in_ready": True,
            "wired_live": True, "headline_payout": 349, "payout_model": "CPA",
            "currency": "USD", "traffic_eligible": True, "sheet_row": 88,
        },
    ]
    selection = machine.selection_with_offers(offers, performance_rows=[])
    assert selection["providers"][0]["provider_name"] == "Gala Health"


def test_payout_queue_row_uses_numeric_sheet_row_for_exact_n8n_readback(
    tmp_path: Path,
) -> None:
    values = [[""] * 27, [""] * 27]
    values[1][2] = "Longevity Meds - GLP-1"
    values[1][4] = "XenTraffic"
    values[1][5] = "https://xentraffic.everflowclient.io/offers/3277"
    values[1][16] = "CPA: $200"
    evidence_path = tmp_path / "switches-staged.json"
    evidence_path.write_text("{}")
    switches = [{
        "provider": "Longevity Meds - GLP-1",
        "target_sheet_row": 2,
        "decision": "STAGE_BEST_COLLECTIBLE_WINNER_CHANGE",
        "winner": {
            "network": "xentraffic",
            "program_id": "3277",
            "effective_value": 200,
        },
    }]

    rows = machine.payout_queue_rows(values, switches, evidence_path)

    assert len(rows) == 1
    assert rows[0]["update_id"] == "best-terms-longevity-meds-glp-1-xentraffic-3277"
    assert rows[0]["sheet_row"] == 2
    assert isinstance(rows[0]["sheet_row"], int)


def test_weekly_no_change_is_silent_but_partial_census_fails_loud() -> None:
    no_change = {
        "census": {"status": "PASS", "failures": []},
        "reconciliation": {"winner_recording_gaps": []},
        "switch_backlog": {"added_count": 0},
    }
    assert machine.discord_message(no_change) == ""
    partial = {
        "census": {"status": "PARTIAL", "failures": [{"network": "fixture"}]},
        "reconciliation": {"winner_recording_gaps": []},
        "switch_backlog": {"added_count": 0},
    }
    message = machine.discord_message(partial)
    assert "census PARTIAL" in message
    assert "winners changed: **0**" in message


def test_sheet_reactivation_flag_cannot_be_overridden_by_stale_tracking_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine, "payout_matrix_evidence", lambda: {
        ("katalys", "1518"): {
            "tracking_ready": True,
            "payout_model": "CPA",
            "headline_value": 300,
        },
    })
    headers = [""] * 27
    row = [""] * 27
    row[2] = "Breeze Meds"
    row[4] = "Katalys"
    row[5] = "https://app.katalys.com/programs/1518"
    row[10] = "approved_active_tracking_ready"
    row[14] = "https://tracking.example/retained"
    row[16] = "CPA: $300"
    row[19] = False
    row[20] = False
    row[21] = "link_revalidation_required_after_reactivation"

    offer = machine.sheet_offers([headers, row])[0]
    assert offer["relationship"] == "approved"
    assert offer["wire_in_ready"] is False


def test_sheet_winner_reconciliation_requires_exact_approved_wire_ready_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine, "read_json", lambda path, default=None: {
        "providers": [{"provider": "Fixture Health"}],
        "duplicate_providers": [],
    })
    headers = [""] * 27
    pending = [""] * 27
    pending[2] = "Fixture Health"
    pending[4] = "Katalys"
    pending[5] = "https://auth.katalys.com/explore/11"
    pending[10] = "applied_pending_brand_approval"
    pending[16] = "CPA: $300"
    selection = {"providers": [{
        "provider_name": "Fixture Health",
        "selected_winner": winner("xentraffic", "22"),
    }]}
    offers = [{
        "provider_name": "Fixture Health", "network": "xentraffic",
        "program_id": "22", "payout_display": "CPA: $200",
        "relationship": "active", "wire_in_ready": True,
    }]
    missing = machine.expected_28_reconciliation([headers, pending], offers, selection)
    assert missing["status"] == "PARTIAL"
    assert missing["winner_recording_gaps"][0]["issues"] == ["exact_winner_row_missing"]

    exact = [""] * 27
    exact[2] = "Fixture Health | XenTraffic offer 22"
    exact[4] = "XenTraffic"
    exact[5] = "https://xentraffic.everflowclient.io/offers/22"
    exact[10] = "approved_active_tracking_ready"
    exact[16] = "CPA: $200"
    exact[19] = True
    complete = machine.expected_28_reconciliation([headers, pending, exact], offers, selection)
    assert complete["status"] == "PASS"
    assert complete["exact_winner_rows_recorded"] == 1
    assert complete["winner_recording_gaps"] == []


def test_sheet_winner_reconciliation_compares_paid_events_not_model_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine, "read_json", lambda path, default=None: {
        "providers": [{"provider": "Fixture Health"}],
        "duplicate_providers": [],
    })
    headers = [""] * 27
    exact = [""] * 27
    exact[2] = "Fixture Health | XenTraffic offer 22"
    exact[4] = "XenTraffic"
    exact[5] = "https://xentraffic.everflowclient.io/offers/22"
    exact[10] = "approved_active_tracking_ready"
    exact[16] = "CPA: $200"
    exact[19] = True
    selection = {"providers": [{
        "provider_name": "Fixture Health",
        "selected_winner": {**winner("xentraffic", "22"), "payout_model": "CPA/sale"},
    }]}
    result = machine.expected_28_reconciliation([headers, exact], [], selection)
    assert result["winner_recording_gaps"] == []


def test_sheet_reconciliation_distinguishes_selection_block_from_handoff_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine, "read_json", lambda path, default=None: {
        "providers": [{"provider": "Fixture Health"}],
        "duplicate_providers": [],
    })
    headers = [""] * 27
    first = [""] * 27
    first[2] = "Fixture Health | XenTraffic offer 22"
    first[4] = "XenTraffic"
    first[5] = "https://xentraffic.everflowclient.io/offers/22"
    first[10] = "approved_active_tracking_ready"
    first[16] = "CPL: $20"
    first[19] = True
    second = [""] * 27
    second[2] = "Fixture Health | XenTraffic offer 23"
    second[4] = "XenTraffic"
    second[5] = "https://xentraffic.everflowclient.io/offers/23"
    second[10] = "approved_active_tracking_ready"
    second[16] = "CPA: $200"
    second[19] = True
    selection = {"providers": [{
        "provider_name": "Fixture Health",
        "selected_winner": None,
        "selection_basis": "no_wire_in_ready_offer",
        "wire_in_ready_offer_count": 0,
    }]}

    result = machine.expected_28_reconciliation([headers, first, second], [], selection)

    assert result["status"] == "PARTIAL"
    assert result["winner_recording_gaps"] == []
    assert result["exact_winner_rows_recorded"] == 0
    assert result["winner_selection_blocks"] == [{
        "provider": "Fixture Health",
        "selection_basis": "no_wire_in_ready_offer",
        "wire_in_ready_offer_count": 0,
    }]


def test_discord_names_selection_blocks_without_calling_them_sheet_gaps() -> None:
    result = {
        "census": {"status": "PASS", "failures": []},
        "reconciliation": {
            "winner_recording_gaps": [],
            "winner_selection_blocks": [{"provider": "Fixture Health"}],
        },
        "switch_backlog": {"added_count": 0},
    }

    message = machine.discord_message(result)

    assert "1 winner selection block" in message
    assert "Sheet winner gap" not in message


def test_approval_receipt_re_evaluates_only_affected_provider(tmp_path: Path) -> None:
    receipt = tmp_path / "approval.json"
    receipt.write_text(json.dumps({
        "status": "PASS",
        "approval_processing": True,
        "provider_relationship": "approved",
        "row_number": 2,
    }))
    receipts = machine.approval_receipts_since_cursor({"processed_receipts": []}, tmp_path)
    values = [["header"] * 25, ["", "", "Sesame Care", *([""] * 22)]]
    scope = machine.approval_affected_providers(receipts, values)
    assert scope == {"sesame-care"}

    previous = {"providers": [
        {"provider_name": "Sesame Care", "selected_winner": None},
        {"provider_name": "Other Health", "selected_winner": winner("xentraffic", "old")},
    ]}
    current = {"providers": [
        {"provider_name": "Sesame Care", "selected_winner": winner("maxbounty", "27300")},
        {"provider_name": "Other Health", "selected_winner": winner("katalys", "new")},
    ]}
    changes = machine.changed_winners(previous, current, scope)
    assert len(changes) == 1
    assert changes[0]["provider"] == "Sesame Care"
    assert changes[0]["winner"]["program_id"] == "27300"


def sesame_matrix() -> dict:
    return {
        "schema": "glp.best-collectible-offer-selection.v1",
        "providers": [{
            "provider_name": "Sesame Care",
            "selected_winner": None,
            "offers": [
                {
                    "network": "maxbounty",
                    "program_id": "27300",
                    "sheet_row": 725,
                    "relationship": "approved",
                    "status": "approved_active_not_wired",
                    "wire_in_ready": False,
                    "wired_live": False,
                    "headline_payout": 200,
                    "payout_display": "$200 fixed per lead",
                    "paid_event": "medication_subscription_signup",
                    "standing_gate": "site deployment plus operator approved advertiser display image",
                },
                {
                    "network": "impact",
                    "program_id": "13292",
                    "sheet_row": 740,
                    "relationship": "approved",
                    "status": "approved_active_tracking_ready",
                    "wire_in_ready": True,
                    "wired_live": False,
                    "headline_payout": 20,
                    "payout_display": "$20 default Appointment Booked; variable payout groups",
                    "paid_event": "glp1_acquisition",
                },
                {
                    "network": "katalys",
                    "program_id": "239",
                    "sheet_row": 116,
                    "relationship": "rejected",
                    "status": "rejected",
                    "wire_in_ready": False,
                    "wired_live": False,
                    "headline_payout": 175,
                    "payout_display": "$175 CPA",
                },
                {
                    "network": "direct",
                    "program_id": "direct",
                    "sheet_row": 38,
                    "relationship": "pending",
                    "status": "pending",
                    "wire_in_ready": False,
                    "wired_live": False,
                    "headline_payout": None,
                    "payout_display": "not recorded",
                },
            ],
        }],
    }


def test_sesame_context_selects_exact_impact_row_and_marks_other_rails_awareness_only(tmp_path: Path) -> None:
    matrix_path = tmp_path / "latest-matrix.json"
    matrix_path.write_text(json.dumps(sesame_matrix()))
    context = cp.load_affiliate_best_terms_context({
        "sender": "Mandy Pitera <mandy.pitera@sesamecare.com>",
        "subject": "Re: Affiliate application",
        "body_text": "Thanks, Izaiah! I updated your contract terms and increased the GLP 1 CPA to $260.",
    }, path=matrix_path, marketplace="impact")
    joined = "\n".join(context["facts"])
    assert context["provider"] == "Sesame Care"
    assert context["marketplace"] == "impact"
    relevant = next(fact for fact in context["facts"] if fact.startswith("AFFILIATE_RELEVANT_RAIL_JSON "))
    assert '"marketplace": "impact"' in relevant
    assert '"program_id": "13292"' in relevant
    assert '"sheet_row": 740' in relevant
    assert '"relationship": "approved"' in relevant
    others = [fact for fact in context["facts"] if fact.startswith("AFFILIATE_OTHER_RAIL_JSON ")]
    assert len(others) == 3
    assert any('"marketplace": "maxbounty"' in fact and '"sheet_row": 725' in fact for fact in others)
    assert any('"marketplace": "katalys"' in fact and '"sheet_row": 116' in fact for fact in others)
    assert any('"marketplace": "direct"' in fact and '"sheet_row": 38' in fact for fact in others)
    assert "AFFILIATE_PRODUCTIVE_PATH_JSON" not in joined
    assert "Sesame Place" in joined


def test_sesame_single_writer_receipt_overrides_stale_impact_terms(tmp_path: Path) -> None:
    matrix_path = tmp_path / "latest-matrix.json"
    matrix_path.write_text(json.dumps(sesame_matrix()))
    receipt_root = tmp_path / "sheet-sync"
    receipt_dir = receipt_root / "drain-20260825T210129Z"
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "sesame-receipt.json").write_text(json.dumps({
        "status": "PASS",
        "dry_run": False,
        "captured_at_utc": "2026-08-25T21:01:30Z",
        "post_write_values_sha256": "a" * 64,
        "readback_mismatches": [],
        "evidence": [{
            "row": 740,
            "network_program_id": "13292",
            "program": "Sesame Care",
            "commission_terms": "GLP-1 CPA: $260",
            "fetched_at_utc": "2026-08-25T21:01:28Z",
        }],
    }))
    context = cp.load_affiliate_best_terms_context({
        "sender": "Mandy Pitera <mandy.pitera@sesamecare.com>",
        "subject": "Re: Affiliate application",
        "body_text": "Thanks, Izaiah! I updated your contract terms and increased the GLP 1 CPA to $260.",
    }, path=matrix_path, marketplace="impact", receipt_root=receipt_root)
    relevant = next(fact for fact in context["facts"] if fact.startswith("AFFILIATE_RELEVANT_RAIL_JSON "))
    assert '"current_terms": "GLP-1 CPA: $260"' in relevant
    assert '"terms_source": "single_writer_verified_sheet_readback"' in relevant


def test_completed_impact_update_cannot_be_redirected_to_maxbounty(tmp_path: Path) -> None:
    matrix_path = tmp_path / "latest-matrix.json"
    matrix_path.write_text(json.dumps(sesame_matrix()))
    source = "Thanks, Izaiah! I updated your contract terms and increased the GLP 1 CPA to $260."
    context = cp.load_affiliate_best_terms_context({
        "sender": "Mandy Pitera <mandy.pitera@sesamecare.com>",
        "subject": "Re: Affiliate application",
        "body_text": source,
    }, path=matrix_path, marketplace="impact")
    generated = {
        "body": "Please apply the increase to our MaxBounty campaign relationship.",
        "grounded_facts": ["Stale cross rail fixture"],
        "rationale": "Fixture.",
    }
    repaired = drafter.enforce_affiliate_business_path(
        generated, context["facts"], source, "mandy.pitera@sesamecare.com", "impact",
    )
    assert "$260" in repaired["body"]
    assert "Impact" in repaired["body"]
    assert "MaxBounty" not in repaired["body"]
    assert "Please apply" not in repaired["body"]
    assert not any(dash in repaired["body"] for dash in drafter.FORBIDDEN_DASHES)


def test_unresolved_marketplace_forbids_action_on_awareness_only_rail() -> None:
    context = [
        "AFFILIATE_RELEVANT_RAIL_UNRESOLVED | no exact marketplace row was verified",
        'AFFILIATE_OTHER_RAIL_JSON {"marketplace":"maxbounty","program_id":"27300","provider":"Sesame Care"}',
    ]
    with pytest.raises(ValueError, match="affiliate_cross_rail_action_forbidden"):
        drafter.enforce_affiliate_business_path({
            "body": "Please apply the increase to MaxBounty.",
            "grounded_facts": ["fixture"],
            "rationale": "fixture",
        }, context, "Please review the account.", "mandy.pitera@sesamecare.com")


def test_unverified_affiliate_identifier_is_rejected() -> None:
    context = [
        'AFFILIATE_RELEVANT_RAIL_JSON {"marketplace":"maxbounty","program_id":"27300","provider":"Sesame Care","relationship":"approved"}',
    ]
    with pytest.raises(ValueError, match="affiliate_identifier_not_verified"):
        drafter.enforce_affiliate_business_path({
            "body": "Use partner ID 999999 for the increase.",
            "grounded_facts": ["fixture"],
            "rationale": "fixture",
        }, context, "Please send the correct identifier.", "partner@example.test", "maxbounty")


def test_newly_materialized_winner_row_is_resolved_by_exact_route() -> None:
    header = ["" for _ in range(len(machine.sheet_writer.HEADERS))]
    winner = ["" for _ in range(len(machine.sheet_writer.HEADERS))]
    winner[4] = "Affiliati"
    winner[23] = "Affiliati offer 100131; approved winner row."
    assert machine.live_winner_sheet_row(
        [header, winner], winner_network="affiliati", winner_program_id="100131",
    ) == 2


def test_newly_materialized_winner_row_ambiguity_fails_closed() -> None:
    header = ["" for _ in range(len(machine.sheet_writer.HEADERS))]
    winner = ["" for _ in range(len(machine.sheet_writer.HEADERS))]
    winner[4] = "XenTraffic"
    winner[5] = "https://xentraffic.example/offers/3249"
    with pytest.raises(ValueError, match="payout_leak_winner_sheet_row_ambiguous"):
        machine.live_winner_sheet_row(
            [header, winner, list(winner)],
            winner_network="xentraffic", winner_program_id="3249",
        )


def test_researched_row_with_held_link_records_non_click_qa() -> None:
    cells = new_approval_research.build_cells({
        "network": "XenTraffic",
        "display_name": "Fixture Provider",
        "provider_program_id": "3249",
        "tracking_link": "https://tracking.example/provider-issued",
    }, {
        "portal_capture_path": "reports/fixture.json",
        "captured_at_utc": "2026-08-30T23:00:00Z",
    }, "2026-08-30T23:01:00Z")
    assert cells["21"] == "not_clicked_preserve_attribution"
    assert cells["19"] == cells["20"] == "FALSE"


def test_hash_bound_program_contract_mapping_allows_required_separate_shared_link_row(
        tmp_path: Path) -> None:
    source = tmp_path / "authenticated-economics.json"
    source.write_text(json.dumps({"rows": [{
        "immutable_ids": {"value": {
            "program_id": "100131", "offer_contract_id": "40044"}},
        "approval_status": {"value": {"relationship_status": "Approved"}},
    }]}))
    link = "https://tracking.example/held-link"
    header = ["" for _ in range(len(machine.sheet_writer.HEADERS))]
    incumbent = ["" for _ in range(len(machine.sheet_writer.HEADERS))]
    incumbent[2] = "Direct Meds | Affiliati"
    incumbent[4] = "Affiliati"
    incumbent[14] = link
    incumbent[23] = "Affiliati offer 40044"
    entry = {
        "network": "Affiliati",
        "provider_program_id": "100131",
        "program": "Direct Meds - GLP-1 Weight Loss CPA $500",
        "cells": {"14": link},
        "evidence": {"shared_tracking_link_identity": {
            "rule": "operator_separate_program_contract_row",
            "network": "affiliati",
            "winner_program_id": "100131",
            "incumbent_program_id": "40044",
            "incumbent_sheet_row": 2,
            "source_path": str(source),
            "source_sha256": machine.sha256_file(source),
        }},
    }
    assert machine.sheet_writer.add_approved_row_existing_matches(
        [header, incumbent], entry) == []
    entry["evidence"] = {}
    matches = machine.sheet_writer.add_approved_row_existing_matches(
        [header, incumbent], entry)
    assert matches[0]["axes"] == ["tracking_link"]
