#!/usr/bin/env python3
"""A payout switch must carry the rail IDENTITY with the link.

DEFECT 2026-09-02, found by the pre-materialization gate on the first run of a
path that had never landed a deploy. apply_provider_wide_payout_switch swaps the
loser tracking link for the winner's TEXTUALLY across the surface files. A
money-page row, though, carries the rail identity beside the href
(affiliate_network, affiliate_tracking_host), and glp_surface_unification
derived those from the OLD rail moments earlier in the same transaction.

src/money-page.mjs recomputes the expected host FROM the network - Katalys ->
track.revoffers.com, Awin -> www.awin1.com, anything else -> the row's own
affiliate_tracking_host - and throws affiliate_direct_link_host_invalid when it
disagrees with the href. Direct Meds moving Katalys -> Affiliati kept network
"Katalys" and host "track.revoffers.com" against an epsilonmkt.com href, and the
entire build died with no provider named in the error.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import glp_affiliate_wire_backlog_worker as worker  # noqa: E402


def test_named_networks_keep_their_pinned_hosts():
    assert worker.PAYOUT_SWITCH_NETWORK_HOSTS == {
        "Katalys": "track.revoffers.com", "Awin": "www.awin1.com"}


def test_canonical_network_matches_the_surface_display_names():
    for token in ("katalys", "Katalys", "KATALYS"):
        assert worker._payout_switch_canonical_network(token) == "Katalys"
    for token in ("awin", "Awin"):
        assert worker._payout_switch_canonical_network(token) == "Awin"
    # An unnamed network is passed through, and the host then comes from the link.
    assert worker._payout_switch_canonical_network("affiliati") == "affiliati"


def test_money_page_still_derives_the_host_from_the_network():
    """If the renderer stops doing this, the rebind must be revisited."""
    releases = sorted((ROOT / "site-system/flagship-v2/releases").glob(
        "glp-b2-wire-backlog-*/src/money-page.mjs"))
    if not releases:
        pytest.skip("no release money-page.mjs on disk")
    text = releases[-1].read_text()
    assert 'affiliate_network === "Katalys" ? "track.revoffers.com"' in text
    assert "affiliate_tracking_host" in text
    assert "affiliate_direct_link_host_invalid" in text


def _row(program_id, href, network, host):
    return {"program_id": program_id, "affiliate_href": href,
            "affiliate_network": network, "affiliate_tracking_host": host,
            "affiliate_link_mode": "transparent_direct"}


def test_rebind_moves_identity_only_for_the_switched_row(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    surface = data / "money-page-specs.json"
    surface.write_text(json.dumps({"pages": [{"path": "/x/", "rows": [
        _row("direct-meds", "https://epsilonmkt.com/aff_c?o=1",
             "Katalys", "track.revoffers.com"),
        _row("someone-else", "https://track.revoffers.com/click?o=2",
             "Katalys", "track.revoffers.com"),
    ]}]}))
    monkeypatch.setattr(worker, "_payout_switch_surface_files", lambda root: [surface])
    changed: dict = {}
    worker._payout_switch_rebind_rail_identity(
        tmp_path, "direct-meds", "https://epsilonmkt.com/aff_c?o=1", "affiliati", changed)
    rows = json.loads(surface.read_text())["pages"][0]["rows"]
    switched = next(r for r in rows if r["program_id"] == "direct-meds")
    untouched = next(r for r in rows if r["program_id"] == "someone-else")
    assert switched["affiliate_network"] == "affiliati"
    assert switched["affiliate_tracking_host"] == "epsilonmkt.com"
    # A row that did not move keeps its identity exactly.
    assert untouched["affiliate_network"] == "Katalys"
    assert untouched["affiliate_tracking_host"] == "track.revoffers.com"
    assert changed


def test_rebind_onto_a_named_network_uses_the_pinned_host(tmp_path, monkeypatch):
    data = tmp_path / "data"; data.mkdir()
    surface = data / "money-page-specs.json"
    surface.write_text(json.dumps({"rows": [
        _row("mover", "https://track.revoffers.com/click?o=9", "affiliati", "epsilonmkt.com")]}))
    monkeypatch.setattr(worker, "_payout_switch_surface_files", lambda root: [surface])
    worker._payout_switch_rebind_rail_identity(
        tmp_path, "mover", "https://track.revoffers.com/click?o=9", "katalys", {})
    row = json.loads(surface.read_text())["rows"][0]
    assert row["affiliate_network"] == "Katalys"
    assert row["affiliate_tracking_host"] == "track.revoffers.com"


def test_rebind_only_touches_rows_already_carrying_the_winner_link(tmp_path, monkeypatch):
    """The textual swap runs first; a row that did not take the link is not ours."""
    data = tmp_path / "data"; data.mkdir()
    surface = data / "money-page-specs.json"
    surface.write_text(json.dumps({"rows": [
        _row("direct-meds", "https://old-loser.example/x", "Katalys", "track.revoffers.com")]}))
    monkeypatch.setattr(worker, "_payout_switch_surface_files", lambda root: [surface])
    worker._payout_switch_rebind_rail_identity(
        tmp_path, "direct-meds", "https://epsilonmkt.com/aff_c?o=1", "affiliati", {})
    row = json.loads(surface.read_text())["rows"][0]
    assert row["affiliate_network"] == "Katalys"


def test_rebind_keys_on_the_link_not_the_program_id(tmp_path, monkeypatch):
    """The exact miss that made the first version of this fix a no-op.

    A switch request's site_program_id is the REVIEW identity
    (direct-meds-review); the money-page row is keyed by the PROVIDER identity
    (direct-meds). Matching on the id rebound nothing and the build still died.
    """
    data = tmp_path / "data"; data.mkdir()
    surface = data / "money-page-specs.json"
    winner = "https://epsilonmkt.com/aff_c?o=1"
    surface.write_text(json.dumps({"rows": [
        _row("direct-meds", winner, "Katalys", "track.revoffers.com")]}))
    monkeypatch.setattr(worker, "_payout_switch_surface_files", lambda root: [surface])
    worker._payout_switch_rebind_rail_identity(
        tmp_path, "direct-meds-review", winner, "affiliati", {})
    row = json.loads(surface.read_text())["rows"][0]
    assert row["affiliate_network"] == "affiliati"
    assert row["affiliate_tracking_host"] == "epsilonmkt.com"


# --- the catastrophic one: a mis-cased network blanks the ENTIRE site --------

def test_selection_write_uses_the_display_network_name():
    """DEFECT 2026-09-02, caught by the gate before it shipped.

    apply_provider_wide_payout_switch wrote the PLAN's network token into
    data/affiliate-activation-selection.json. Plan tokens are lowercase
    ("katalys"); src/affiliate-link-map.mjs keys TRACKING_HOST_BY_NETWORK by the
    DISPLAY name ("Katalys"). A mis-cased row makes allowedHost undefined, the
    row fails the tracking_host check, and authorizedSelectionMap returns null
    for the WHOLE selection - so buildAffiliateLinkMap returns {} and every
    affiliate link on the site goes dark. Measured on the scratch tree: the
    compare surface went from 56 entries / 54 live to 0 / 0 because three of
    eleven rows were mis-cased.
    """
    source = (ROOT / "scripts/glp_affiliate_wire_backlog_worker.py").read_text()
    assert '"network": str(winner.get("network") or ""),' not in source
    assert '"network": _payout_switch_canonical_network(' in source


def test_link_map_host_table_still_keys_on_the_display_name():
    """If the runtime ever keys on the lowercase token, revisit the write."""
    releases = sorted((ROOT / "site-system/flagship-v2/releases").glob(
        "glp-b2-wire-backlog-*/src/affiliate-link-map.mjs"))
    if not releases:
        pytest.skip("no release affiliate-link-map.mjs on disk")
    text = releases[-1].read_text()
    assert "TRACKING_HOST_BY_NETWORK" in text
    assert 'Awin: "www.awin1.com"' in text
    assert 'Katalys: "track.revoffers.com"' in text
    # And one bad row really does blank everything, which is why case matters.
    assert "if (!authorized) return {};" in text


def test_canonical_network_repairs_every_plan_token_spelling():
    for token in ("katalys", "Katalys", " katalys ", "KATALYS"):
        assert worker._payout_switch_canonical_network(token.strip()) == "Katalys"
    for token in ("awin", "Awin", "AWIN"):
        assert worker._payout_switch_canonical_network(token) == "Awin"
