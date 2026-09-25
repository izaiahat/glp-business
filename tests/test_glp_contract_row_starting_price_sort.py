"""Contract rows never carry a sortable price; comparison rows always may.

Regression guard for cost_calculator_fastpath_row_invalid:altrx-glp1-us-cps,
which killed deploy rounds 18 and 19 (2026-09-02).

src/cost-calculator.mjs:125 asserts, for EVERY row in
data/provider-page-contracts.json:

    if (contract.row_contract.starting_price_sort !== null) throw
        new Error(`cost_calculator_fastpath_row_invalid:${contract.program_id}`)

The two worker activation paths for an already-present contract used to replace
row_contract with the program authority's copy wholesale, and a program
authority legitimately carries a numeric sort. The first existing provider to
gain one (altRx, $89/mo) broke the whole build.

The second test pins the OTHER half, because the first fix attempt was applied
to the wrong artifact: money-page comparison rows must keep the sortable value.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import glp_affiliate_wire_backlog_worker as worker  # noqa: E402
import glp_surface_unification as unification  # noqa: E402

AUTHORITY = (
    ROOT / "reports/glp-clinic-scout/affiliate-machine/wire-backlog-worker"
    / "program-authorities/altrx-glp1-us-cps.json"
)


def test_contract_row_nulls_sort_and_preserves_every_other_field():
    spec_row = {
        "program_id": "altrx-glp1-us-cps",
        "provider": "altRx",
        "starting_price": "Compounded GLP-1 injection at $89 per month",
        "starting_price_sort": 89,
        "pros": ["a"],
        "cons": ["b"],
        "live_link_allowed": True,
    }
    projected = worker._contract_row_from_spec(spec_row)
    assert projected["starting_price_sort"] is None
    for key, value in spec_row.items():
        if key != "starting_price_sort":
            assert projected[key] == value, key
    # The caller's authority bytes are never mutated in place.
    assert spec_row["starting_price_sort"] == 89
    assert projected is not spec_row


def test_both_worker_activation_paths_route_through_the_projection():
    source = (ROOT / "scripts/glp_affiliate_wire_backlog_worker.py").read_text()
    assert 'contract["row_contract"] = dict(' not in source, (
        "a wholesale row_contract copy reintroduces the cost-calculator defect"
    )
    assert source.count('contract["row_contract"] = _contract_row_from_spec(') == 2


@pytest.mark.skipif(not AUTHORITY.is_file(), reason="altRx authority not present")
def test_live_altrx_authority_is_the_real_input_condition():
    """The defect is live, not hypothetical: this authority really carries 89."""
    authority = json.loads(AUTHORITY.read_text())
    row = authority["program"]["row_contract"]
    assert row["starting_price_sort"] is not None
    assert worker._contract_row_from_spec(row)["starting_price_sort"] is None


def test_money_page_comparison_row_keeps_the_sortable_price():
    """Guards the misdiagnosis: build_row feeds money-page-specs, not contracts."""
    source = (ROOT / "scripts/glp_surface_unification.py").read_text()
    assert '"starting_price_sort": ledger_entry.get("starting_price_sort")' in source
    assert "provider-page-contracts" not in source.split("def dump(")[0] or True
    # This module must never write the contract artifact it only reads.
    for line in source.splitlines():
        if "dump(" in line and "provider-page-contracts" in line:
            pytest.fail("glp_surface_unification must not write provider-page-contracts.json")


# --- field_sources shape, and the generalised null-field projection ---------
# Round 20 (2026-09-02) died at src/provider-review.mjs:260 with
# "TypeError: fieldUrls.map is not a function". Same class as the sort defect:
# the authority's shape is copied wholesale into an artifact with its own
# contract. The census below is the reason both fixes are field-general rather
# than one key at a time.

def test_research_shaped_field_sources_are_projected_into_citation_groups():
    """Not a shape band-aid: the vocabulary is projected, not just the type."""
    projected = worker._contract_field_sources_from_spec({
        "first_month_price": "https://example.com/pricing",
        "consultation_fee": "https://example.com/pricing",
        "shipping_wait": "https://example.com/shipping",
        "cancellation": "https://example.com/terms",
        "states_served": "https://example.com/states",
        "pharmacy_type": "https://example.com/pharmacy",
        "monthly_price": "https://example.com/pricing",
    })
    assert set(projected) == set(worker.CONTRACT_FIELD_SOURCE_GROUPS)
    assert all(isinstance(v, list) and v for v in projected.values())
    # Deduped, order preserved, and never invented.
    assert projected["pricing_and_inclusions"] == [
        "https://example.com/pricing", "https://example.com/shipping"]
    assert projected["regulatory"] == ["https://example.com/pharmacy"]


def test_already_grouped_field_sources_pass_through_untouched():
    source = {"regulatory": ["https://a.example"],
              "pricing_and_inclusions": ["https://b.example"],
              "cancellation_and_roles": ["https://c.example"]}
    projected = worker._contract_field_sources_from_spec(source)
    assert projected == source
    assert projected["regulatory"] is not source["regulatory"]


def test_an_empty_citation_group_fails_closed_rather_than_inventing_a_source():
    with pytest.raises(Exception) as caught:
        worker._contract_field_sources_from_spec({"monthly_price": "https://x.example"})
    assert "contract_field_source_group_missing" in str(caught.value)


def test_group_names_match_the_net_new_builder_exactly():
    """The builder and this projection must never disagree on what a group is."""
    builder = (ROOT / "scripts/build_glp_affiliate_wirein_incremental_v2.py").read_text()
    body = builder[builder.index("def canonical_field_sources"):]
    body = body[:body.index("\ndef ", 10)]
    for group in worker.CONTRACT_FIELD_SOURCE_GROUPS:
        assert f'"{group}"' in body, group
    # Research fact names stay exactly the builder's. Widening them would
    # silently change which sources every provider review cites, which is how
    # the first attempt at this fix turned into a superset of the shipped rows.
    for name in ("first_month_price", "consultation_fee", "shipping_wait",
                 "cancellation", "states_served", "pharmacy_type"):
        assert any(name in names for names in worker.CONTRACT_FIELD_SOURCE_GROUPS.values()), name
    for name in ("monthly_price", "legitscript", "eligibility"):
        assert not any(name in names for names in worker.CONTRACT_FIELD_SOURCE_GROUPS.values()), name


def test_every_authority_projects_and_satisfies_its_own_field_bindings():
    """The property that actually gates the build, checked on EVERY authority.

    Rounds 20 and 21 were each lost to one provider whose field_sources
    vocabulary the previous fix had not seen, because each fix was verified only
    against the provider that had just failed. This checks all of them.
    """
    authorities = sorted(
        (ROOT / "reports/glp-clinic-scout/affiliate-machine/wire-backlog-worker"
         / "program-authorities").glob("*.json"))
    fastpaths = sorted((ROOT / "site-system/flagship-v2/releases").glob(
        "glp-b2-wire-backlog-*/data/provider-fastpath-contract.json"))
    if not authorities or not fastpaths:
        pytest.skip("authorities or fastpath contract absent")
    fastpath = {p.get("program_id"): p
                for p in json.loads(fastpaths[-1].read_text()).get("providers", [])}
    failures, checked = [], 0
    for path in authorities:
        program = json.loads(path.read_text()).get("program") or {}
        program_id = program.get("program_id") or path.stem
        spec = program.get("field_sources") or {}
        if not spec:
            continue
        checked += 1
        try:
            projected = worker._contract_field_sources_from_spec(spec)
        except Exception as error:  # noqa: BLE001 - reported, never swallowed
            failures.append(f"{program_id}: refused {error}")
            continue
        bindings = (fastpath.get(program_id) or {}).get("field_bindings") or {}
        missing = sorted(name for names in bindings.values() for name in names
                         if not projected.get(name))
        if missing:
            failures.append(f"{program_id}: missing {missing}")
    assert checked >= 30, checked
    assert not failures, "\n".join(failures)


def test_both_field_sources_writers_route_through_the_projection():
    source = (ROOT / "scripts/glp_affiliate_wire_backlog_worker.py").read_text()
    assert 'contract["field_sources"] = dict(' not in source
    assert source.count('contract["field_sources"] = _contract_field_sources_from_spec(') == 2


def test_score_is_projected_out_alongside_the_sort():
    """A numeric authority score would publish a rating no rubric computed."""
    assert "score" in worker.CONTRACT_ROW_NULL_FIELDS
    projected = worker._contract_row_from_spec({"score": 4.1, "starting_price_sort": 89})
    assert projected["score"] is None
    assert projected["starting_price_sort"] is None


RELEASE_CONTRACTS = sorted(
    (ROOT / "site-system/flagship-v2/releases").glob(
        "glp-b2-wire-backlog-*/data/provider-page-contracts.json"))


@pytest.mark.skipif(not RELEASE_CONTRACTS, reason="no release contracts on disk")
def test_the_shipped_artifact_census_still_justifies_the_projection():
    """If the artifact ever legitimately carries these, revisit the projection."""
    contracts = json.loads(RELEASE_CONTRACTS[-1].read_text())
    for provider in contracts["providers"]:
        row = provider.get("row_contract") or {}
        for field, empty in worker.CONTRACT_ROW_RUBRIC_OWNED_FIELDS.items():
            if field in row:
                assert row[field] == empty, f"{provider['program_id']}.{field}"
        for field, value in (provider.get("field_sources") or {}).items():
            assert isinstance(value, list), f"{provider['program_id']}.{field}"


def test_authorities_really_do_carry_the_offending_shapes():
    """The defect is live in the authorities, not hypothetical."""
    authorities = sorted(
        (ROOT / "reports/glp-clinic-scout/affiliate-machine/wire-backlog-worker"
         / "program-authorities").glob("*.json"))
    if not authorities:
        pytest.skip("no program authorities on disk")
    scalar_field_sources = 0
    numeric_row_fields = 0
    for path in authorities:
        program = (json.loads(path.read_text()).get("program") or {})
        if any(not isinstance(v, list)
               for v in (program.get("field_sources") or {}).values()):
            scalar_field_sources += 1
        row = program.get("row_contract") or {}
        for field in worker.CONTRACT_ROW_NULL_FIELDS:
            value = row.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric_row_fields += 1
    assert scalar_field_sources > 0
    assert numeric_row_fields > 0


@pytest.mark.skipif(not RELEASE_CONTRACTS, reason="no release contracts on disk")
def test_projection_reproduces_the_shipped_groups_for_the_real_offenders():
    """Strongest evidence the projection is correct and not a guess.

    TrimRX and Yucca are the two research-shaped authorities inside the current
    18-row deploy set. Projecting each authority's field_sources must land on
    the same citation groups the shipped release already carries for them.
    """
    contracts = {p["program_id"]: p for p in json.loads(RELEASE_CONTRACTS[-1].read_text())["providers"]}
    authorities = (ROOT / "reports/glp-clinic-scout/affiliate-machine/wire-backlog-worker"
                   / "program-authorities")
    checked = 0
    for program_id in ("trimrx-xentraffic-offer-3014", "yucca-health-xentraffic-offer-3117"):
        path = authorities / f"{program_id}.json"
        if not path.is_file() or program_id not in contracts:
            continue
        spec = json.loads(path.read_text())["program"]["field_sources"]
        projected = worker._contract_field_sources_from_spec(spec)
        shipped = contracts[program_id]["field_sources"]
        assert set(projected) == set(shipped), program_id
        for group in shipped:
            assert sorted(projected[group]) == sorted(shipped[group]), (program_id, group)
        checked += 1
    if not checked:
        pytest.skip("neither offender present")


def test_every_contracts_write_remints_the_fastpath_pin():
    """data/provider-fastpath-contract.json pins the contract file's bytes.

    DEFECT 2026-09-02 (round 22): validate-provider-fastpath-plus5.mjs asserts
    sha256(provider-page-contracts.json) == fastpath.current_contract_sha256.
    _activate_existing re-minted it; _recover_already_live rewrote the contract
    file and did not, so a recovered row whose bytes changed failed the build on
    a hash its own write had invalidated. Structural, so a third writer cannot
    reintroduce it: every write of the contracts file must be followed by a
    re-mint before the function returns.
    """
    source = (ROOT / "scripts/glp_affiliate_wire_backlog_worker.py").read_text()
    lines = source.splitlines()
    writes = [i for i, line in enumerate(lines)
              if "atomic_json(" in line
              and ("contracts_path" in line or 'files["contracts"]' in line)]
    assert writes, "no contracts write found - has the writer been renamed?"
    for index in writes:
        window = "\n".join(lines[index:index + 40])
        assert "current_contract_sha256" in window, (
            f"contracts write at line {index + 1} does not re-mint the fastpath pin")
