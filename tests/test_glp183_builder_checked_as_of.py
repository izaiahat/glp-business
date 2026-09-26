"""Offline direct-builder regressions; no live authorities or tracking visits."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_glp_affiliate_wirein_incremental_v2 as builder


def inputs():
    """Complete synthetic contract inputs, independent of private provider data."""
    spec = {
        "program_id": "fixture-provider", "display_name": "Fixture Provider",
        "official_url": "https://provider.example/", "advertiser_id": "fixture-offer",
        "network": "Awin", "logo_output_file": "fixture.png",
        "checked_as_of": "2026-08-31", "regulatory_banner_required": False,
        "page_copy": {"title": "Fixture", "description": "Fixture description",
                      "bottom_line": "Fixture bottom line"},
        "row_contract": {"best_for": "Fixture", "cons": [], "insurance": "Unknown",
                         "medications": [], "pros": [], "starting_price": "Unknown"},
    }
    research = {"fields": {
        name: {"value": "Fixture source text", "source_url": "https://provider.example/terms",
               "verified_as_of": "2026-08-31"}
        for name in ("first_month_price", "states_served", "consultation_fee",
                     "cancellation", "pharmacy_type", "shipping_wait")
    }}
    logo = {"path": "/fixture.png", "source_asset_url": "https://provider.example/logo.png",
            "sha256": "0" * 64}
    return spec, research, logo


def build(spec, research, logo):
    return builder.build_contract(spec, research, "https://tracking.example/fixture", logo,
                                  "fixture-publisher")


def test_missing_date_is_named_refusal_not_keyerror():
    spec, research, logo = inputs()
    del spec["checked_as_of"]
    with pytest.raises(RuntimeError, match="^spec_checked_as_of_missing$"):
        build(spec, research, logo)


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_date_is_named_refusal(blank):
    spec, research, logo = inputs()
    spec["checked_as_of"] = blank
    with pytest.raises(RuntimeError, match="^spec_checked_as_of_missing$"):
        build(spec, research, logo)


def test_valid_source_date_is_preserved_without_mutating_inputs():
    spec, research, logo = inputs()
    before = deepcopy((spec, research, logo))
    result = build(spec, research, logo)
    assert result["network_contract"]["local_evidence_checked_through"] == "2026-08-31"
    assert result["program_id"] == spec["program_id"]
    assert result["page_copy"]["pricing"] == research["fields"]["first_month_price"]["value"]
    assert (spec, research, logo) == before
