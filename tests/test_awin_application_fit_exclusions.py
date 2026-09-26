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
    verdict, classification = fit.evaluate_application(candidate, rulings={})
    assert verdict.decision != "APPLY_OK"
    assert not classification.fit


@pytest.mark.parametrize("name", [
    "Fella Health", "FellaHealth - GLP-1", "Fella - GLP-1",
    "Telos Rx", "telos rx - GLP-1", "TelosRx",
    "MadeMed", "Made Med", "Breeze Meds", "BreezeMeds",
    "Direct Meds", "DirectMeds", "Strut Health", "StrutHealth", "eMed.com",
])
def test_registry_holds_are_terminal_even_with_fit_override(name):
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


def test_registry_unavailable_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(fit, "APPLICATION_EXCLUSIONS_PATH", tmp_path / "absent.json")
    decision, _ = fit.evaluate_application(fit.Candidate(name="Example Clinic", region="US"), rulings={})
    assert decision.decision == fit.EXCLUDED_PROVIDER_DECISION
    assert "registry_unavailable" in str(decision.reasons)


def test_real_medication_delivery_still_fits():
    c = fit.Candidate(name="Example GLP-1 Clinic",
                      description="Clinicians prescribe GLP-1 medications via telehealth", region="US")
    decision, verdict = fit.evaluate_application(c, rulings={})
    assert (decision.decision, verdict.tier) == ("APPLY_OK", "explicit_glp1")


def test_unrelated_brand_is_not_alias_match():
    assert fit.application_exclusion("Fellabella GLP-1 clinic") == ("", "")
