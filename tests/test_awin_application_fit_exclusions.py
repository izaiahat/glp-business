"""Offline application-gate regressions; no portal, Sheet or send side effects."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("affiliate_fit", ROOT / "scripts/affiliate_niche_geo_filter.py")
assert spec is not None and spec.loader is not None
fit = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = fit
spec.loader.exec_module(fit)


@pytest.mark.parametrize("name,description,categories", [
    ("GLP-1 Care", "GLP-1 weight-loss gummies and supplements, with telehealth advice", ("Nutra - Weight Loss GLP1",)),
    ("GLP-1 Weight Care", "GLP-1 supplements with medical weight loss information", ()),
    ("GLP-1 Offer", "GLP-1 peptides injectable for research use, weight loss", ()),
    ("GLP-1 Advice", "GLP-1 medical weight loss information and telehealth advice", ()),
    ("GLP-1 Wellness", "GLP-1 gummies and supplements, clinician approved", ()),
    ("GLP-1 Labs", "Semaglutide research peptides not for human consumption", ()),
])
def test_product_and_research_probes_never_apply(name, description, categories):
    candidate = fit.Candidate(name=name, description=description, categories=categories, region="US")
    verdict, classification = fit.evaluate_application(candidate, rulings={name.lower(): (fit.RULING_FIT, "override")})
    assert verdict.decision != "APPLY_OK"
    assert not classification.fit


@pytest.mark.parametrize("name", ["Fella Health", "FellaHealth - GLP-1", "Fella - GLP-1"])
def test_permanent_exclusion_is_terminal_even_with_fit_override(name):
    c = fit.Candidate(name=name, description="GLP-1 medication prescribed via telehealth", region="US")
    decision, verdict = fit.evaluate_application(c, rulings={name.lower(): (fit.RULING_FIT, "fit")})
    assert decision.decision == fit.EXCLUDED_PROVIDER_DECISION
    assert not verdict.fit
    assert decision.decision in fit.NO_RESCUE_TERMINALS


@pytest.mark.parametrize("state", ["paused", "Network Paused", "suspended", "inactive", "disabled"])
def test_network_paused_offer_cannot_apply_or_be_rescued(state):
    c = fit.Candidate(name="Example GLP Clinic", description="GLP-1 medication prescribed via telehealth",
                      region="US", network_status=state)
    decision, verdict = fit.evaluate_application(c, rulings={c.name.lower(): (fit.RULING_FIT, "fit")})
    assert decision.decision == "SKIPPED_NETWORK_PAUSED"
    assert not verdict.fit
    assert decision.decision in fit.NO_RESCUE_TERMINALS


def test_permanent_policy_unavailable_fails_closed(monkeypatch):
    monkeypatch.setattr(fit.affiliate_payout_policy, "PERMANENT_EXCLUSIONS", frozenset())
    decision, _ = fit.evaluate_application(fit.Candidate(name="Example Clinic", region="US"), rulings={})
    assert decision.decision == fit.EXCLUDED_PROVIDER_DECISION
    assert "registry_unavailable" in str(decision.reasons)


def test_real_medication_delivery_still_fits():
    c = fit.Candidate(name="Example GLP-1 Clinic",
                      description="Clinicians prescribe GLP-1 medications via telehealth", region="US",
                      network_status="active")
    decision, verdict = fit.evaluate_application(c, rulings={})
    assert (decision.decision, verdict.tier) == ("APPLY_OK", "explicit_glp1")


def test_unrelated_brand_is_not_alias_match():
    assert fit.application_exclusion("Fellabella GLP-1 clinic") == ("", "")


def test_reproduced_research_grade_peptide_is_terminal_before_fit():
    candidate = fit.Candidate(name='Semaglutide Research Grade',
                              description='Research-grade semaglutide peptides injectable; online clinician information',
                              region='US')
    for rulings in ({}, {candidate.name.lower(): (fit.RULING_FIT, 'fit')}):
        decision, verdict = fit.evaluate_application(candidate, rulings=rulings)
        assert decision.decision == 'SKIPPED_NOT_GLP_FIT'
        assert not verdict.fit
        assert not fit.glp_application_fit(candidate).fit
    clinical = fit.Candidate('Example GLP Clinic',
                             description='Clinicians prescribe GLP-1 medications via telehealth',
                             region='US', network_status='active')
    assert fit.evaluate_application(clinical, rulings={})[0].decision == 'APPLY_OK'


@pytest.mark.parametrize('field', ['network_status', 'status', 'offer_status'])
@pytest.mark.parametrize('paused', ['paused', 'Network Paused', 'suspended', 'inactive', 'disabled'])
def test_shared_choke_preserves_adverse_network_state(field, paused):
    row = {'name': 'Example GLP Clinic', 'description': 'GLP-1 medication prescribed via telehealth',
           'region': 'US', 'application_url': 'https://example.org/apply',
           'network_status': 'active', 'status': 'active', 'offer_status': 'active'}
    row[field] = paused
    result = fit.choke_candidate(row)
    assert not result['operational']
    assert result['evidence']['classifier_decision'] == 'SKIPPED_NETWORK_PAUSED'
    assert fit.choke_candidate({key: value for key, value in row.items()
                                if key not in ('network_status', 'status', 'offer_status')})['operational'] is False
    assert fit.choke_candidate({**row, field: 'active'})['operational'] is True


def test_permanent_authority_propagates_and_editorial_holds_keep_scope(monkeypatch):
    original = fit.affiliate_payout_policy.PERMANENT_EXCLUSIONS
    assert {'fella', 'fella health'} <= original
    monkeypatch.setattr(fit.affiliate_payout_policy, 'PERMANENT_EXCLUSIONS',
                        original | {'new held clinic'})
    assert fit.application_exclusion('New Held Clinic - GLP-1')[0] == 'new held clinic'
    assert fit.evaluate_application(fit.Candidate('New Held Clinic', region='US'), rulings={})[0].decision == fit.EXCLUDED_PROVIDER_DECISION
    monkeypatch.setattr(fit.affiliate_payout_policy, 'PERMANENT_EXCLUSIONS', original)
    assert fit.application_exclusion('New Held Clinic') == ('', '')
    # These source-thin / product-status / FDA-warning / economics holds govern
    # editorial or monetizable outputs, not a blanket application prohibition.
    for name in ('MadeMed', 'Telos RX', 'Breeze Meds', 'Direct Meds', 'Strut Health', 'eMed'):
        assert fit.application_exclusion(name) == ('', '')
