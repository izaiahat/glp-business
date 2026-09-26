#!/usr/bin/env python3
"""Deterministic pre-application filter: US-only + niche allowlist.

Operator ruling 2026-08-05: *"you should only ever apply to affiliate programs
within the United States … only apply to affiliate programs within the niches
that fit those categories that we already established. Anything else is just a
waste of time."*

This module is pure — no I/O, no network — so the coordinator, the backrun, and
the tests all share one implementation. Every candidate gets a terminal
decision with recorded reasons; nothing is silently dropped:

    APPLY_OK        US-serving and inside an allowed niche lane
    SKIPPED_NICHE   outside the niche allowlist (blocklist hit or no lane match)
    SKIPPED_GEO     provably non-US, or region unverifiable (fail closed:
                    "within the United States" means confirmed, not assumed)

The classifier decision gates APPLICATIONS and the shared candidate choke additionally gates every operational Sheet boundary. Rejected and incomplete candidates remain auditable in the local parking ledger; they are never emitted as operational Sheet cells.

`evaluate()` above is the SHARED classifier (backrun + auto-apply). The daily
auto-apply job calls `evaluate_application()` at the bottom of this file, which
adds one further terminal — SKIPPED_NOT_GLP_FIT — because applications are for
glpclinicscout.com only (operator 2026-08-06). See the APPLICATION SCOPE block.

APPLY-BREADTH CONTRACT (operator 2026-08-13, durable ruling at
reports/glp-clinic-scout/affiliate-machine/operator-rulings/
operator-rulings-bridge-session-20260813-apply-breadth.json):

    Within GLP-relevant fit, the ONLY reason not to apply is a program
    operating ILLEGALLY. Fit means GLP-RELEVANT — GLP/weight-loss telehealth,
    compounding pharmacies, GLP-adjacent labs/diagnostics (PlexusDx / CoreAge /
    T2D and metabolic/hormone testing), diabetes-metabolic care — and
    domestic-US-serving. Everything non-fit PARKS in the NOT_FIT ledger with a
    reason; nothing is silently dropped. Terms-consent holds stay at the
    ACCEPT step, never at application submission.

Every application decision therefore carries a `contract:<bucket>:<reason>`
tag from the CLOSED enum in `CONTRACT_REASONS`: bucket APPLY, PARK_NOT_FIT, or
REJECT_LEGALITY. Every non-legality reason maps to APPLY or PARK_NOT_FIT.
"""
from __future__ import annotations

import fcntl
import glob as _glob
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from dataclasses import dataclass, field

# --- niche lanes (operator's established categories) -------------------------
NICHE_LANES: dict[str, tuple[str, ...]] = {
    "glp1-weightloss": (
        "glp-1", "glp1", "semaglutide", "tirzepatide", "wegovy", "ozempic",
        "zepbound", "mounjaro", "weight loss", "weight-loss", "weightloss",
        "obesity", "medical weight",
    ),
    "peptides": ("peptide", "bpc-157", "bpc157", "nad+", "sermorelin", "ipamorelin"),
    "hair-loss": ("hair loss", "hair-loss", "hair growth", "minoxidil",
                  "finasteride", "hair restoration", "hair regrowth"),
    "cpap-sleep": ("cpap", "sleep apnea", "sleep apnoea", "bipap", "sleep therapy",
                   "sleep study", "insomnia"),
    "telehealth-general": ("telehealth", "telemedicine", "online doctor",
                           "online clinic", "online pharmacy", "virtual care",
                           "online prescription", "mens health", "men's health",
                           "womens health", "women's health", "testosterone", "trt"),
    "beauty-skincare": ("skincare", "skin care", "beauty", "cosmetic", "aesthetic",
                        "dermatolog", "anti-aging", "anti aging", "botox", "serum"),
    "supplements-vitamins": ("supplement", "vitamin", "nutraceutical", "creatine",
                             "protein powder", "probiotic", "collagen", "wellness",
                             "nutrition"),
    "business-saas": ("saas", "b2b software", "crm", "productivity software",
                      "project management", "invoicing", "accounting software",
                      "email marketing", "automation platform", "business tool"),
}

# Hard blocklist: a hit here is terminal SKIPPED_NICHE even if an allow term
# also appears ("wellness travel retreats" is travel, not wellness).
NICHE_BLOCKLIST: tuple[tuple[str, str], ...] = (
    ("travel", r"\b(travel|airline|airlines|flight|flights|hotel|hotels|resort|cruise|vacation|tour operator|car rental|booking platform)\b"),
    ("gambling", r"\b(casino|gambling|betting|sportsbook|poker|lottery)\b"),
    ("fashion", r"\b(fashion|apparel|clothing|footwear|sneaker|handbag|jewelry|jewellery)\b"),
    ("electronics", r"\b(electronics|smartphone|laptop|gaming pc|console|camera gear)\b"),
    ("finance-retail", r"\b(forex|crypto exchange|trading platform|payday loan|credit card offer)\b"),
    ("adult", r"\b(adult content|dating site|hookup|sexual wellness|intimacy products|pleasure products|satisfyer)\b"),
    ("general-retail", r"\b(marketplace superstore|department store|general merchandise)\b"),
)

# --- geo --------------------------------------------------------------------
US_POSITIVE = re.compile(
    r"\b(united states|usa|u\.s\.a?\.?|us only|us-based|all 50 states|"
    r"ships? (?:to|within) the (?:us|united states)|nationwide \(us\))\b", re.I)
US_REGION_CODES = {"US", "USA", "U.S.", "U.S.A.", "UNITED STATES", "UNITED STATES OF AMERICA",
                   "US/CA", "NORTH AMERICA", "NA", "GLOBAL", "WORLDWIDE"}
NON_US_REGION = re.compile(
    r"\b(uk only|united kingdom|great britain|\bgbp\b|\beur\b|\baud\b|\bcad\b(?! ?/)|"
    r"europe only|eu only|australia only|canada only|germany|france|spain|italy|"
    r"netherlands|india only|singapore|brazil|mexico only|poland|sweden)\b", re.I)


@dataclass
class SiteEvidence:
    """What the advertiser's OWN site says it offers.

    Operator ruling 2026-08-06: *"a telehealth/clinic brand whose NAME doesn't
    say weight-loss but which OFFERS GLP-1/semaglutide/tirzepatide care is a
    fit"*. A marketplace record carries at most a 255-char marketing blurb, and
    Katalys carries no blurb at all — so the name+description alone can never
    answer "what does this provider actually sell?". This is that answer,
    harvested once per advertiser by scripts/affiliate_site_evidence.py and
    cached, so the filter itself stays pure.

    Structured (not free text) on purpose: a raw site dump fed back into the
    body-copy lanes would trip every off-vertical veto on the page footer.
    """

    url: str = ""
    fetched: bool = False
    glp1_tokens: tuple[str, ...] = field(default_factory=tuple)
    weight_loss: bool = False
    clinical: bool = False
    error: str = ""

    @property
    def proves_glp1(self) -> bool:
        """A GLP-1 token AND a weight-loss framing AND a clinical marker.

        All three, and the middle one is what separates a provider from a
        mention. Verified against the live harvest on 2026-08-06:

            HealthRX          glp=8 tokens  wl=True   clinical=True   -> provider
            Border Free       glp=4 tokens  wl=True   clinical=True   -> provider
            Brightmeds        glp=8 tokens  wl=True   clinical=True   -> provider
            SingleCare        glp=4 tokens  wl=False  clinical=True   -> drug-price
                                                                         listings
            Love Wellness     glp=1 token   wl=False  clinical=True   -> supplement
                                                                         brand's blog

        A real GLP-1 practice always frames the drug as weight care. A discount
        card listing Ozempic prices and a supplement brand ranking for "GLP-1"
        never do, and both cleared a tokens+clinical bar.
        """
        return bool(self.fetched and self.glp1_tokens and self.weight_loss and self.clinical)


@dataclass
class Candidate:
    name: str
    network: str = ""
    program_id: str = ""
    description: str = ""
    categories: tuple[str, ...] = field(default_factory=tuple)
    region: str = ""          # network-reported region/country field, verbatim
    currency: str = ""        # network-reported payout currency
    url: str = ""
    # The NETWORK's own country assertion for this program, when the per-program
    # region field is absent. Katalys tags all 405 marketplace offers "United
    # States of America" in its `country` taxonomy; stripping that tag was what
    # made every Katalys row fail geo as `region_unverified`.
    network_region: str = ""
    site: SiteEvidence | None = None
    network_status: str = ""  # network-reported offer state; paused is terminal


@dataclass
class Decision:
    decision: str             # APPLY_OK | SKIPPED_NICHE | SKIPPED_GEO
    lane: str | None
    reasons: list[str]

    @property
    def apply_ok(self) -> bool:
        return self.decision == "APPLY_OK"


def _text(c: Candidate) -> str:
    return " ".join((c.name, c.description, " ".join(c.categories))).lower()


def classify_niche(c: Candidate) -> tuple[str | None, list[str]]:
    text = _text(c)
    for label, pattern in NICHE_BLOCKLIST:
        if re.search(pattern, text, re.I):
            return None, [f"blocklist:{label}"]
    best, best_hits = None, 0
    for lane, terms in NICHE_LANES.items():
        hits = sum(1 for t in terms if t in text)
        if hits > best_hits:
            best, best_hits = lane, hits
    if best is None:
        return None, ["no_lane_match"]
    return best, [f"lane:{best}", f"term_hits:{best_hits}"]


def classify_geo(c: Candidate) -> tuple[bool, list[str]]:
    # A blank per-program region is not evidence of anything. When the network
    # itself asserts the program's country, that assertion IS the region — it is
    # the same provider field, one level up. Fail-closed still applies when both
    # are blank.
    region = ((c.region or "").strip() or (c.network_region or "").strip()).upper()
    text = _text(c) + " " + (c.url or "").lower()
    if NON_US_REGION.search(c.region or "") or NON_US_REGION.search(text):
        return False, [f"non_us_signal:{(c.region or 'text-match')[:40]}"]
    if region in US_REGION_CODES:
        return True, [f"region_code:{region}"]
    if (c.currency or "").strip().upper() == "USD":
        return True, ["currency:USD"]
    if US_POSITIVE.search(text):
        return True, ["us_text_signal"]
    if c.url and re.search(r"\.(co\.uk|com\.au|\.ca|\.de|\.fr|\.in|\.eu)(/|$)", c.url):
        return False, ["non_us_tld"]
    # Fail closed: "within the United States" means confirmed, not assumed.
    return False, ["region_unverified"]


def evaluate(c: Candidate) -> Decision:
    lane, niche_reasons = classify_niche(c)
    if lane is None:
        return Decision("SKIPPED_NICHE", None, niche_reasons)
    us_ok, geo_reasons = classify_geo(c)
    if not us_ok:
        return Decision("SKIPPED_GEO", lane, niche_reasons + geo_reasons)
    return Decision("APPLY_OK", lane, niche_reasons + geo_reasons)


# =============================================================================
# APPLICATION SCOPE — glpclinicscout.com ONLY
# =============================================================================
# Operator ruling 2026-08-06: *"applications are for the GLP site ONLY"*. The
# lanes above stay as they are: they classify EVERY program the machine sees so
# the Sheet, the research lane and the future sister-vertical jobs keep their
# vocabulary. This layer is narrower and applies to ONE question — may the daily
# auto-apply job file an application for this program?
#
# The answer is yes only for GLP-1 / weight-loss telehealth and what is directly
# adjacent to it. Men's-health/TRT, hair, peptides, diagnostics, fitness
# equipment and general supplements are real verticals with real programs — they
# simply belong to sister sites that do not exist yet, and an application filed
# from glpclinicscout.com against them burns a relationship we cannot use.
#
# Two gates, in order:
#   1. BRAND IDENTITY. A program whose own NAME declares another vertical
#      ("TRT Nation", "Circle DNA", "Shed Supplements") is that vertical's
#      program. Only its own name declaring GLP-1/weight-loss overturns this —
#      a body-copy mention does not, because every telehealth brand's body copy
#      mentions everything it has ever sold.
#   2. GLP EVIDENCE. Something must actually say GLP-1 or weight-loss:
#        explicit_glp1               a GLP-1 drug/class token anywhere
#        weight_loss_in_name         the NAME is a weight-loss identity
#        clinical_weight_loss        weight-loss body copy + a clinical marker,
#                                    and no other vertical in the body copy
#      Anything else is NOT_GLP_FIT. Fail closed, exactly like geo.

# Longest form first: regex alternation is first-match-wins, so `glp` ahead of
# `glp-1` made every captured token read "glp" and truncated the evidence
# recorded in receipts. Membership tests were unaffected; the receipts were not.
GLP1_EXPLICIT_RE = re.compile(
    r"\b(glp[- ]?1s|glp[- ]?1|glp|semaglutide|tirzepatide|liraglutide|retatrutide|"
    r"dulaglutide|wegovy|ozempic|zepbound|mounjaro|saxenda|victoza|trulicity)\b", re.I)

WEIGHT_LOSS_RE = re.compile(
    r"\b(weight[- ]?loss|weightloss|weight management|medical weight|weight care|"
    r"obesity|bariatric|lose weight|losing weight|fat loss|metabolic health|"
    r"appetite suppressant|appetite suppression|"
    # Compound brand constructions the space-separated terms above miss.
    # "WeightWatchers US" and "SkinnyRx" are weight-loss identities that read as
    # one token; "Trim Rx"/"TrimRx" is the same construction with a space.
    r"weight[- ]?watchers|(?:slim|trim|skinny|lean)[- ]?(?:rx|md|meds))\b", re.I)

# A weight-loss claim only counts from the body copy when the program is
# clinically delivered — that is the difference between a GLP-1 clinic and a
# fat-burner supplement that says "supports weight loss" on the label.
CLINICAL_CONTEXT_RE = re.compile(
    # `prescribe`/`prescribes` were missing from this alternation; harmless while
    # the tier only needed a weight-loss claim, load-bearing from 2026-09-17 when
    # clinical delivery became the medication test ("GobyMeds PRESCRIBES
    # semaglutide"). A supplement label never prescribes, so this cannot widen
    # scope to the product classes the ruling excluded.
    r"\b(telehealth|telemedicine|prescription|prescriptions|prescribe|prescribes|"
    r"prescribed|prescribing|"
    r"online doctor|online clinic|online pharmacy|compounded|compounding|clinician|"
    r"clinicians|physician|physicians|licensed provider|licensed providers|"
    r"board[- ]certified|medically supervised|medical provider|injection|injections|"
    # "medical weight loss" is the clinic category's own name for the service
    # ("Center for Medical Weight Loss ... with semaglutide", live 2026-08-06).
    # Added 2026-09-17 with the medication test, for the same reason as
    # prescribe/prescribes: without it a real clinic reads as a product.
    r"medical weight[- ]loss|"
    r"injectable|injectables|patient|patients|clinic|clinics|rx)\b", re.I)

MEDICATION_OFFER_RE = re.compile(
    r"\b(?:glp[- ]?1|semaglutide|tirzepatide|wegovy|ozempic|zepbound|mounjaro)\b"
    r".{0,110}\b(?:medications?|prescriptions?|prescrib\w*|compounded|injections?|"
    r"injectables?)\b|"
    r"\b(?:medications?|prescriptions?|prescrib\w*|compounded|injections?|"
    r"injectables?)\b.{0,110}"
    r"\b(?:glp[- ]?1|semaglutide|tirzepatide|wegovy|ozempic|zepbound|mounjaro)\b", re.I)
DELIVERY_SERVICE_RE = re.compile(
    r"\b(?:telehealth|telemedicine|online clinic|online doctor|clinicians?|"
    r"physicians?|medical provider|prescrib\w*|prescription|patient|clinic)\b", re.I)

# Other verticals, for APPLICATION purposes only. A hit here never changes how a
# program is recorded or researched — only whether glpclinicscout.com applies.
OFF_VERTICAL_FOR_APPLICATION: tuple[tuple[str, str], ...] = (
    ("mens-health-trt", r"\b(trt|testosterone|erectile|men'?s health|low[- ]t|"
                        r"hormone replacement|hrt|sexual health|libido)\b"),
    ("hair-loss", r"\b(hair|minoxidil|finasteride|regrowth|balding|alopecia)\b"),
    ("peptides-longevity", r"\b(peptide|peptides|bpc[- ]?157|nad\+|nad|sermorelin|"
                           r"ipamorelin|longevity|anti[- ]aging)\b"),
    ("diagnostics-testing", r"\b(dna|genomic|genomics|genetic|lab test|lab tests|"
                            r"blood test|blood tests|biomarker|biomarkers|diagnostic|"
                            r"diagnostics|microbiome|paternity|test kit|testing kit|"
                            r"at[- ]home test)\b"),
    ("fitness-equipment", r"\b(treadmill|dumbbell|dumbbells|home gym|gym equipment|"
                          r"fitness equipment|exercise bike|rowing machine|weight bench|"
                          r"barbell|kettlebell)\b"),
    ("supplements-general", r"\b(supplement|supplements|vitamin|vitamins|multivitamin|"
                            r"nutraceutical|nutraceuticals|protein powder|creatine|"
                            r"collagen|probiotic|probiotics|sports nutrition|gummies|"
                            r"herbal|nutrition)\b"),
    ("cpap-sleep", r"\b(cpap|bipap|sleep apnea|sleep apnoea|sleep therapy)\b"),
    ("beauty-skincare", r"\b(skincare|skin care|botox|serum|serums|cosmetic|cosmetics|"
                        r"dermatology|dermatologist)\b"),
    ("business-saas", r"\b(saas|crm|invoicing|project management|email marketing)\b"),
    ("pets", r"\b(pet|pets|dog|dogs|cat|cats|canine|feline|puppy)\b"),
)

# A telehealth clinic that also treats hair loss or low testosterone is still a
# clinic — that is what "directly adjacent" means, and it is where GLP-1 lives.
# A supplement store or a lab-test brand that merely mentions weight loss is a
# different business. So a sibling CLINICAL vertical in the body copy does not
# veto a weight-loss offering; a PRODUCT vertical does.
CLINIC_SIBLING_VERTICALS = frozenset({
    "mens-health-trt", "hair-loss", "peptides-longevity", "cpap-sleep",
})

# =============================================================================
# APPLY-SCOPE RULING (operator 2026-09-17) — GLP-1 TELEHEALTH MEDICATION ONLY
# =============================================================================
# Receipt: ops/linear/approvals/
# OPERATOR-DECISION-20260917-apply-scope-glp1-telehealth-only-clickdealer.json
#
#   "Affiliate auto-apply scope: apply ONLY to explicit GLP-1 / weight-loss
#    telehealth MEDICATION programs (the explicit_glp1 tier); diabetes/
#    blood-sugar supplements, general weight-loss supplements, gummies, detox,
#    peptides, ED/other telehealth are NOT fits."
#
# This SUPERSEDES the 2026-08-13 breadth tiers as APPLY fits. They remain
# classifier vocabulary — a candidate still classifies as `diabetes_metabolic`
# or `telehealth_clinical`, and that tier is still recorded as evidence — but
# the CONTRACT_REASONS map above now buckets those reasons PARK_NOT_FIT, and
# LANE_BY_TIER below binds them to a lane the signed authority manifest does
# not activate, so scripts/affiliate_application_executor_gate.py fails closed
# on any row that somehow reaches it.
#
# Two mechanical consequences inside the explicit_glp1 tier itself, because the
# ruling says telehealth MEDICATION and names the non-fit product classes:
#   * a GLP-1 token with no clinical-delivery evidence is a consumable product
#     ("Soda Lean bundle 1 - 2 bottles", vertical "Nutra - Weight Loss GLP1"),
#     parked as park_glp1_without_medication_delivery;
#   * a PRODUCT vertical in the program's own NAME stays terminal even when the
#     name also says GLP-1 ("Trimora - GLP-1 Skincare"), the same rule the site
#     -evidence rescue has always applied. A sibling CLINICAL vertical in the
#     body copy still does not veto a clinic (CLINIC_SIBLING_VERTICALS).
APPLY_SCOPE_RULING_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ops", "linear", "approvals",
    "OPERATOR-DECISION-20260917-apply-scope-glp1-telehealth-only-clickdealer.json")

# Permanently excluded providers are never an application fit, in any tier, by
# any route — not even a bridge FIT ruling or a GLP-1 Sheet vertical. Fella
# Health's own terms prohibit review sites, so the whole business excludes it
# (see scripts/affiliate_payout_policy.py PERMANENT_EXCLUSIONS and
# scripts/affiliate_sheet_batch_writer.py). Its two ClickDealer offers read
# "[WEB+MOB] Fella Health - GLP-1 Medication CTC ...", i.e. they land squarely
# in the explicit_glp1 tier and would otherwise apply.
APPLICATION_EXCLUSIONS_PATH = Path(__file__).resolve().parents[1] / "ops/affiliate-application-exclusions.json"
EXCLUDED_PROVIDER_DECISION = "SKIPPED_EXCLUDED_PROVIDER"


def application_exclusion(name: str) -> tuple[str, str]:
    """Read the curated held/excluded provider registry; never assume an unreadable
    registry means no holds. Match aliases as complete tokens, not substrings.
    """
    try:
        registry = json.loads(APPLICATION_EXCLUSIONS_PATH.read_text(encoding="utf-8"))
        if registry.get("schema") != "affiliate-application-exclusions-v1":
            raise ValueError("exclusion registry schema")
        providers = registry["providers"]
        if not isinstance(providers, list) or not providers:
            raise ValueError("exclusion registry empty")
    except (OSError, ValueError, KeyError, TypeError):
        return "registry_unavailable", "application_exclusion_registry_unavailable"
    normalized = re.sub(r"[^a-z0-9]+", "", str(name or "").lower())
    for provider in providers:
        for alias in (provider["name"], *provider.get("aliases", [])):
            # Preserve boundaries around decorated affiliate program titles.
            pieces = re.findall(r"[a-z0-9]+", alias.lower())
            expression = r"(?<![a-z0-9])" + r"[\W_]*".join(map(re.escape, pieces)) + r"(?![a-z0-9])"
            if re.search(expression, str(name or ""), re.I):
                return provider["name"], provider["reason"]
        # Joined brand spellings (e.g. TelosRx), only for a complete name.
        if normalized == re.sub(r"[^a-z0-9]+", "", provider["name"].lower()):
            return provider["name"], provider["reason"]
    return "", ""
# Terminals no downstream rescue may overturn (the direct-Sheet
# `sheet_vertical_glp1` rescue in scripts/affiliate_auto_apply.py reads this).
NO_RESCUE_TERMINALS = frozenset({"SKIPPED_OPERATOR_RULING", EXCLUDED_PROVIDER_DECISION,
                                 "SKIPPED_NETWORK_PAUSED"})


def permanently_excluded_provider(name: str) -> str:
    """The matched exclusion token for `name`, or "" — never a fit, any route."""
    name, _reason = application_exclusion(name)
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") if name else ""


# =============================================================================
# APPLY-BREADTH CONTRACT (operator 2026-08-13) — widened GLP-RELEVANT fit
# =============================================================================
# The durable operator ruling (see module docstring) widens application fit
# from "says GLP-1/weight-loss" to GLP-RELEVANT: clinical telehealth delivery
# platforms, compounding pharmacies, GLP-adjacent labs/diagnostics and
# diabetes-metabolic care are all fit — they prescribe, compound, or measure
# what glpclinicscout.com covers. Rejection (as opposed to parking) is reserved
# for programs operating illegally.
APPLY_BREADTH_RULING_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "reports", "glp-clinic-scout", "affiliate-machine", "operator-rulings",
    "operator-rulings-bridge-session-20260813-apply-breadth.json")

# Clinical telehealth DELIVERY platforms — a brand that can consult and
# prescribe. Deliberately platform-shaped tokens: "prescription discount card"
# and a retail pharmacy aisle must not match.
TELEHEALTH_PLATFORM_RE = re.compile(
    r"\b(telehealth|telemedicine|tele-health|"
    r"online (?:doctor|doctors|clinic|pharmacy|consultations?)|"
    r"virtual (?:care|clinic|visits?)|prescription delivery|"
    r"online prescription (?:services?|referral services?|referrals?))\b", re.I)

COMPOUNDING_PHARMACY_RE = re.compile(
    r"\b(compounding pharmac(?:y|ies)|"
    r"compounded (?:medications?|prescriptions?|meds|semaglutide|tirzepatide)|"
    r"503[ab] pharmac(?:y|ies))\b", re.I)

# Diabetes-metabolic care splits into the CONDITION (a disease mention proves
# nothing — cord-blood banks quote diabetes statistics) and MANAGEMENT (what a
# diabetes business actually sells). The tier needs management evidence, or the
# condition plus clinical delivery context.
DIABETES_CONDITION_RE = re.compile(
    r"\b(diabetes|diabetic|t2d|type[- ]2 diabetes|prediabetes)\b", re.I)
DIABETES_MANAGEMENT_RE = re.compile(
    r"\b(blood sugar|glucose monitor(?:s|ing)?|a1c|insulin(?: resistance)?|cgm|"
    r"continuous glucose|glycemic|metabolic health|cardiometabolic|"
    r"diabetes (?:supplies|management|care|test(?:ing)?))\b", re.I)

# A lab/diagnostics record is GLP-adjacent only when it measures the metabolic/
# hormonal ground GLP care stands on — "like PlexusDx/CoreAge/T2D" (operator's
# words). Ancestry DNA, paternity and generic at-home panels stay parked.
GLP_ADJACENT_LAB_CONTEXT_RE = re.compile(
    r"\b(metabolic|metabolism|hormones?|glucose|a1c|insulin|thyroid|diabetes|"
    r"diabetic|weight|obesity|glp|cardiometabolic)\b", re.I)

OPERATOR_NAMED_GLP_ADJACENT_RE = re.compile(r"\b(plexusdx|coreage|t2d)\b", re.I)

# Clinical-delivery specialties that are NOT GLP-relevant: a fertility kit, a
# lens-replacement "online prescription" store or a cannabis-card platform is a
# clinic-shaped business in a different vertical. Parked, never rejected.
NON_GLP_SPECIALTY_RE = re.compile(
    r"\b(fertility|ivf|prenatal|dental|dentistry|orthodont\w*|vision|eyewear|"
    r"glasses|contact lens(?:es)?|lasik|hearing aids?|audiolog\w*|veterinar\w*|"
    r"cannabis|marijuana|cbd|thc|psychedelic|ketamine|cord blood|stem cell)\b", re.I)

# The ONLY rejection class: a program whose own record advertises operating
# outside the law (prescription drugs without prescriptions, "research only"
# compounds marketed for use). The phrase alone is NOT enough — an eyewear
# store legitimately sells sunglasses "without a prescription" ("A Sight For
# Sport Eyes", real 2026-08-12 awin record) — so the signal only fires when the
# record also talks about prescription drugs. Cross-border importation calls
# are judgement calls and stay with the bridge-fit-rulings hook, which
# outranks this.
ILLEGAL_OPERATION_RE = re.compile(
    r"\b(no (?:prescription|rx|script) (?:needed|required)|"
    r"without (?:a )?(?:valid )?prescription|"
    r"not for human consumption|research (?:use|purposes) only)\b", re.I)
RX_DRUG_CONTEXT_RE = re.compile(
    r"\b(medications?|meds|drugs?|pharmacy|pharmacies|pharmaceutical(?:s)?|"
    r"peptides?|semaglutide|tirzepatide|ozempic|wegovy|zepbound|mounjaro|"
    r"glp[- ]?1s?|injectables?)\b", re.I)

# Classifies a human ruling's REASON text as legality-based (REJECT bucket)
# versus a plain fit judgement (PARK bucket). Decision names are unchanged.
LEGALITY_RULING_RE = re.compile(
    r"\b(illegal|illegally|legality|legality-adjacent|importation|unlicensed|"
    r"counterfeit|regulatory gray|gray zone|dea|fda (?:action|warning))\b", re.I)

# --- closed decision/reason enum -------------------------------------------
# Every application decision carries exactly one reason from this enum, tagged
# `contract:<bucket>:<reason>` in its reasons list. The mapping is CLOSED:
# `contract_tag()` raises on an unknown reason, so a new reason cannot ship
# without declaring its bucket, and every non-legality reason maps to APPLY or
# PARK_NOT_FIT — never a silent drop.
CONTRACT_APPLY = "APPLY"
CONTRACT_PARK = "PARK_NOT_FIT"
CONTRACT_REJECT = "REJECT_LEGALITY"

CONTRACT_REASONS: dict[str, str] = {
    # APPLY — explicit GLP-1 / weight-loss telehealth MEDICATION, US-serving,
    # lawful. This is the whole apply scope after the 2026-09-17 ruling.
    "fit_glp_explicit": CONTRACT_APPLY,
    "fit_site_glp1_evidence": CONTRACT_APPLY,
    "fit_bridge_ruling": CONTRACT_APPLY,
    # PARK_NOT_FIT — the 2026-08-13 breadth tiers keep their classification as
    # evidence vocabulary, but they are NOT application fits (2026-09-17).
    "fit_weight_loss_identity": CONTRACT_PARK,
    "fit_clinical_weight_loss": CONTRACT_PARK,
    "fit_telehealth_clinical": CONTRACT_PARK,
    "fit_compounding_pharmacy": CONTRACT_PARK,
    "fit_glp_adjacent_labs": CONTRACT_PARK,
    "fit_diabetes_metabolic": CONTRACT_PARK,
    # PARK_NOT_FIT — auditable in the parking/NOT_FIT ledger, never silent
    "park_blocklisted_category": CONTRACT_PARK,
    "park_off_vertical_brand": CONTRACT_PARK,
    "park_product_vertical_body_copy": CONTRACT_PARK,
    "park_specialty_non_glp": CONTRACT_PARK,
    "park_weight_claim_without_clinical_context": CONTRACT_PARK,
    "park_glp1_without_medication_delivery": CONTRACT_PARK,
    "park_permanently_excluded_provider": CONTRACT_PARK,
    "park_network_paused_offer": CONTRACT_PARK,
    "park_not_glp_relevant": CONTRACT_PARK,
    "park_non_us": CONTRACT_PARK,
    "park_us_unverified": CONTRACT_PARK,
    "park_needs_site_evidence": CONTRACT_PARK,
    "park_bridge_ruling_not_fit": CONTRACT_PARK,
    "park_operator_ruling_hold": CONTRACT_PARK,
    # REJECT_LEGALITY — the only true rejection under the 2026-08-13 ruling
    "reject_illegal_operation_signal": CONTRACT_REJECT,
    "reject_bridge_ruling_legality": CONTRACT_REJECT,
    "reject_operator_ruling_legality": CONTRACT_REJECT,
}


def contract_tag(reason: str) -> str:
    """`contract:<bucket>:<reason>` for a CLOSED-enum reason; raises otherwise."""
    return f"contract:{CONTRACT_REASONS[reason]}:{reason}"


def apply_breadth_provenance() -> dict:
    """The durable apply-breadth ruling document's provenance, for receipts.

    Advisory only — the contract above is wired in code; this reports which
    ruling bytes it implements (or that the file is absent, loudly)."""
    try:
        with open(APPLY_BREADTH_RULING_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        return {"present": True, "path": APPLY_BREADTH_RULING_PATH,
                "ruled_at_utc": str(doc.get("ruled_at_utc") or ""),
                "source_message_id": str(doc.get("source_message_id") or "")}
    except (OSError, ValueError) as exc:
        return {"present": False, "path": APPLY_BREADTH_RULING_PATH,
                "error": f"{type(exc).__name__}: {exc}"}


# The lane an APPLICATION rides. Only the three ruled fit tiers bind to a lane
# the signed authority manifest activates; every de-scoped tier binds to
# UNAUTHORIZED_APPLY_LANE, which is in no authority manifest and in no
# portal_apply.LANE_MAP entry, so affiliate_application_executor_gate fails
# closed (`fit_lane_not_authorized`) on anything that reaches it (2026-09-17).
UNAUTHORIZED_APPLY_LANE = "apply-scope-not-authorized"
LANE_BY_TIER: dict[str, str] = {
    "explicit_glp1": "glp1-weightloss",
    "site_glp1": "glp1-weightloss",
    "sheet_vertical_glp1": "glp1-weightloss",
    "bridge_ruling": "glp1-weightloss",
    "weight_loss_in_name": UNAUTHORIZED_APPLY_LANE,
    "clinical_weight_loss": UNAUTHORIZED_APPLY_LANE,
    "telehealth_clinical": UNAUTHORIZED_APPLY_LANE,
    "compounding_pharmacy": UNAUTHORIZED_APPLY_LANE,
    "glp_adjacent_labs": UNAUTHORIZED_APPLY_LANE,
    "diabetes_metabolic": UNAUTHORIZED_APPLY_LANE,
}

# The whole application scope, derived from the CLOSED contract above so the map
# and the scope can never disagree: exactly the reasons bucketed APPLY, and
# exactly the tiers they come from. `sheet_vertical_glp1` is the direct-Sheet
# route's own operator-ruled fit (scripts/affiliate_auto_apply.py), kept here so
# the lane binding and the scope lists stay in one place.
APPLY_SCOPE_FIT_REASONS = frozenset(
    reason for reason, bucket in CONTRACT_REASONS.items() if bucket == CONTRACT_APPLY)
APPLY_SCOPE_FIT_TIERS = frozenset(
    {"explicit_glp1", "site_glp1", "sheet_vertical_glp1", "bridge_ruling"})
assert all(LANE_BY_TIER[tier] != UNAUTHORIZED_APPLY_LANE for tier in APPLY_SCOPE_FIT_TIERS)
assert all(lane == UNAUTHORIZED_APPLY_LANE
           for tier, lane in LANE_BY_TIER.items() if tier not in APPLY_SCOPE_FIT_TIERS)


@dataclass
class FitVerdict:
    """Why the application lane did or did not accept a program.

    `contract_decision`/`contract_reason` carry the closed-enum apply-breadth
    verdict (APPLY | PARK_NOT_FIT | REJECT_LEGALITY); tiers keep the historical
    vocabulary plus the 2026-08-13 breadth tiers (telehealth_clinical,
    compounding_pharmacy, glp_adjacent_labs, diabetes_metabolic)."""

    fit: bool
    tier: str                  # explicit_glp1 | weight_loss_in_name | clinical_weight_loss | none | ...
    reasons: list[str]
    off_verticals_in_name: list[str] = field(default_factory=list)
    off_verticals_in_text: list[str] = field(default_factory=list)
    contract_decision: str = ""
    contract_reason: str = ""


def _off_verticals(text: str) -> list[str]:
    return [label for label, pattern in OFF_VERTICAL_FOR_APPLICATION
            if re.search(pattern, text, re.I)]


def glp_application_fit(c: Candidate) -> FitVerdict:
    """Is this program a GLP-RELEVANT offering we may apply to?

    Widened per the 2026-08-13 apply-breadth ruling: beyond explicit GLP-1 /
    weight-loss evidence, clinical telehealth platforms, compounding pharmacies,
    GLP-adjacent labs/diagnostics and diabetes-metabolic care are fit. Non-fit
    is PARKED with a closed-enum reason, never silently dropped."""
    name = (c.name or "").lower()
    text = _text(c)
    off_name = _off_verticals(name)
    off_text = _off_verticals(text)

    glp_in_name = bool(GLP1_EXPLICIT_RE.search(name))
    glp_in_text = bool(GLP1_EXPLICIT_RE.search(text))
    wl_in_name = bool(WEIGHT_LOSS_RE.search(name))
    wl_in_text = bool(WEIGHT_LOSS_RE.search(text))
    clinical = bool(CLINICAL_CONTEXT_RE.search(text))
    comp_in_name = bool(COMPOUNDING_PHARMACY_RE.search(name))
    compounding = bool(COMPOUNDING_PHARMACY_RE.search(text))
    telehealth = bool(TELEHEALTH_PLATFORM_RE.search(text))
    diabetes = bool(DIABETES_MANAGEMENT_RE.search(text)
                    or (DIABETES_CONDITION_RE.search(text)
                        and CLINICAL_CONTEXT_RE.search(text)))
    named_adjacent = bool(OPERATOR_NAMED_GLP_ADJACENT_RE.search(name))
    specialty = NON_GLP_SPECIALTY_RE.search(name) or NON_GLP_SPECIALTY_RE.search(text)
    site = c.site
    site_proves = bool(site and site.proves_glp1)
    site_reason = (f"site_glp1_evidence:{','.join(site.glp1_tokens[:4])}@{site.url}"
                   if site_proves else "")
    product_verticals = [v for v in off_text if v not in CLINIC_SIBLING_VERTICALS]
    # Labs are GLP-adjacent only alongside metabolic/hormonal context.
    labs_adjacent = ("diagnostics-testing" in off_text
                     and bool(GLP_ADJACENT_LAB_CONTEXT_RE.search(text)))

    def parked(reason_key: str, reasons: list[str], tier: str = "none") -> FitVerdict:
        return FitVerdict(False, tier, [*reasons, contract_tag(reason_key)],
                          off_name, off_text,
                          contract_decision=CONTRACT_PARK, contract_reason=reason_key)

    def fit(tier: str, reason_key: str, reasons: list[str]) -> FitVerdict:
        """A classified tier, admitted as an application fit only if the closed
        contract still buckets its reason APPLY (operator 2026-09-17).

        A de-scoped breadth tier KEEPS its classification — the tier travels in
        the verdict and in the receipt vocabulary — but it is not a fit, so no
        route can file an application for it.
        """
        if CONTRACT_REASONS[reason_key] != CONTRACT_APPLY:
            return parked(reason_key,
                          [f"glp_tier:{tier}", *reasons,
                           "apply_scope_20260917:not_glp1_telehealth_medication"],
                          tier=tier)
        return FitVerdict(True, tier,
                          [f"glp_fit:{tier}", *reasons, contract_tag(reason_key)],
                          off_name, off_text,
                          contract_decision=CONTRACT_APPLY, contract_reason=reason_key)

    # Gate 0 — a permanently excluded provider is never a fit, in any tier.
    excluded = permanently_excluded_provider(c.name)
    if excluded:
        return parked("park_permanently_excluded_provider",
                      [f"permanently_excluded_provider:{excluded}",
                       "permanent_business_wide_exclusion_review_site_prohibition"])

    # Gate 1 — the brand's own name says which vertical it is.
    #
    # ONE thing overturns a name: the provider's own site proving it delivers
    # GLP-1 care. That is the operator's rule — a telehealth provider doing GLPs
    # is a fit however its program is titled ("Brightmeds - NAD+ Injections" is
    # a service line of a clinic that prescribes semaglutide). It applies only
    # to SIBLING CLINICAL verticals, so a PRODUCT vertical in the name stays
    # terminal no matter what the site says. (Until 2026-09-17 a GLP-1 /
    # weight-loss / compounding / operator-named-lab token in the name also
    # overturned it; the apply-scope ruling below removed that bypass.)
    #
    # 2026-09-17: an off-vertical in the name is terminal even when the name ALSO
    # says GLP-1 or weight-loss. "Trimora - GLP-1 Skincare" is a skincare
    # product, "Research Labs - GLP-3 RT - Peptides Weight Loss" is a peptide
    # product, and the ruling names supplements, gummies, detox, peptides and
    # ED/other telehealth as NOT fits — so a GLP token can no longer pull a
    # program that titled itself another vertical back into the apply scope. The
    # site-evidence rescue is unchanged, and still only for sibling CLINICAL
    # verticals: a supplement store, pet brand, lab-test company or SaaS whose
    # blog mentions Ozempic is still not a GLP-1 clinic.
    if off_name:
        rescuable = site_proves and all(v in CLINIC_SIBLING_VERTICALS for v in off_name)
        if not rescuable:
            return parked("park_off_vertical_brand",
                          [f"off_vertical_brand:{','.join(off_name)}",
                           "off_vertical_in_name_is_terminal",
                           "apply_scope_20260917:not_glp1_telehealth_medication"]
                          + ([f"site_evidence_insufficient_for:{','.join(off_name)}"]
                             if site and site.fetched else []))

    # Gate 2 — evidence ladder, strongest first. Historical tiers unchanged;
    # the 2026-08-13 breadth tiers extend the ladder below them.
    if glp_in_text:
        # The broad clinical matcher includes informational wording; require a
        # medication-specific offer tied to a delivery service, not advice about
        # supplements or a category label carrying an incidental GLP token.
        offer_text = " ".join((c.name or "", c.description or ""))
        if product_verticals:
            return parked("park_product_vertical_body_copy",
                          [f"off_vertical_text:{','.join(product_verticals)}",
                           "glp_token_vetoed_by_product_vertical"], tier="explicit_glp1")
        if (clinical and MEDICATION_OFFER_RE.search(offer_text)
                and DELIVERY_SERVICE_RE.search(offer_text)):
            return fit("explicit_glp1", "fit_glp_explicit",
                       ["glp1_token_present", "medication_delivery_evidence"])
        # …unless the provider's OWN site carries the proof the record lacks.
        # SiteEvidence.proves_glp1 already requires a GLP-1 token AND
        # weight-loss framing AND a clinical marker, which is exactly medication
        # -delivery evidence, and site_glp1 stays an operator-ruled fit. So a
        # name-only clinic ("Embody GLP1" on Katalys, "Trim Rx") keeps a
        # registered route in — through the site-evidence harvest, never through
        # an assumption. Handled by the site tier below; park only if it is not
        # reached.
        if not site_proves:
            return parked("park_glp1_without_medication_delivery",
                          ["glp1_token_present", "no_clinical_delivery_evidence",
                           "apply_scope_20260917:not_glp1_telehealth_medication"],
                          tier="explicit_glp1")
    # The site tier is now consulted BEFORE the de-scoped name/label tiers
    # (2026-09-17): a site-proven GLP-1 clinic is a ruled fit, and letting
    # `weight_loss_in_name` claim it first would park a real clinic ("Trim Rx"
    # with a harvested site is the audit's own false-negative case).
    if site_proves:
        # The record's own text says nothing, but the provider's site does.
        # Site evidence is still only evidence about a CLINIC. If the record's
        # own body copy sells a product vertical, the GLP-1 words on that site
        # belong to a store's content marketing, not to a care offering — the
        # same veto the body-copy tier below applies, for the same reason.
        if product_verticals:
            return parked("park_product_vertical_body_copy",
                          [f"off_vertical_text:{','.join(product_verticals)}",
                           "site_glp1_evidence_vetoed_by_product_vertical"])
        reasons = [site_reason, "site_clinical_context"]
        if off_name:
            reasons.append(f"site_evidence_overrides_brand_identity:{','.join(off_name)}")
        return fit("site_glp1", "fit_site_glp1_evidence", reasons)
    # De-scoped by the 2026-09-17 ruling but still CLASSIFIED: `fit()` parks a
    # tier whose contract reason no longer buckets APPLY.
    if wl_in_name:
        return fit("weight_loss_in_name", "fit_weight_loss_identity",
                   ["weight_loss_identity_in_name"])
    if named_adjacent:
        return fit("glp_adjacent_labs", "fit_glp_adjacent_labs",
                   ["operator_named_glp_adjacent_program"])
    if wl_in_text and clinical:
        # Body-copy-only evidence is the weakest historical tier, so it does
        # not survive a body that is also selling a PRODUCT vertical
        # (supplements, lab tests, gym gear). Sibling clinical services are
        # fine — see above.
        if product_verticals:
            return parked("park_product_vertical_body_copy",
                          [f"off_vertical_text:{','.join(product_verticals)}",
                           "body_copy_weight_claim_without_glp1_token"])
        return fit("clinical_weight_loss", "fit_clinical_weight_loss",
                   ["weight_loss_body_copy", "clinical_context"])

    # --- 2026-08-13 breadth tiers ------------------------------------------
    if compounding:
        if specialty:
            return parked("park_specialty_non_glp",
                          [f"specialty_non_glp:{specialty.group(0).lower()}"])
        return fit("compounding_pharmacy", "fit_compounding_pharmacy",
                   ["compounding_pharmacy_offering"])
    if telehealth:
        if specialty:
            return parked("park_specialty_non_glp",
                          [f"specialty_non_glp:{specialty.group(0).lower()}",
                           "telehealth_specialty_outside_glp"])
        if product_verticals:
            return parked("park_product_vertical_body_copy",
                          [f"off_vertical_text:{','.join(product_verticals)}",
                           "telehealth_signal_inside_product_vertical_body_copy"])
        return fit("telehealth_clinical", "fit_telehealth_clinical",
                   ["clinical_telehealth_platform"])
    if labs_adjacent:
        lab_products = [v for v in product_verticals if v != "diagnostics-testing"]
        if specialty:
            return parked("park_specialty_non_glp",
                          [f"specialty_non_glp:{specialty.group(0).lower()}"])
        if lab_products:
            return parked("park_product_vertical_body_copy",
                          [f"off_vertical_text:{','.join(lab_products)}",
                           "lab_signal_inside_product_vertical_body_copy"])
        return fit("glp_adjacent_labs", "fit_glp_adjacent_labs",
                   ["glp_adjacent_lab_diagnostics"])
    if diabetes:
        if specialty:
            return parked("park_specialty_non_glp",
                          [f"specialty_non_glp:{specialty.group(0).lower()}"])
        if product_verticals:
            return parked("park_product_vertical_body_copy",
                          [f"off_vertical_text:{','.join(product_verticals)}",
                           "diabetes_signal_inside_product_vertical_body_copy"])
        return fit("diabetes_metabolic", "fit_diabetes_metabolic",
                   ["diabetes_metabolic_care_offering"])

    if wl_in_text:
        return parked("park_weight_claim_without_clinical_context",
                      ["weight_loss_body_copy", "no_clinical_context"])
    return parked("park_not_glp_relevant", ["no_glp1_or_weight_loss_offering"])


def is_metadata_poor(c: Candidate) -> bool:
    """True when the record carries no offering text at all — only a name.

    Katalys emits `{name, program_id, relationship}` and nothing else, so every
    one of its 405 marketplace offers was being judged on its brand name alone.
    "Ro.co", "Hims, Inc." and "PlushCare" are three of the largest GLP-1
    telehealth providers in the US and all three read as `no_lane_match`. A
    record with nothing to read is not a rejection — it is an ENRICHMENT
    request, and conflating the two is how a whole network disappears.
    """
    return not (c.description or "").strip() and not any(
        str(x).strip() for x in (c.categories or ()))


# =============================================================================
# BRIDGE FIT RULINGS — human override, consulted BEFORE the automated gates
# =============================================================================
# The bridge rules individual programs by hand when the classifier cannot see
# what a human can (GoodRx Care is a real telehealth arm; Canadian Insulin is a
# cross-border importation risk). Those rulings are terminal for the named
# program, in both directions, and they are FILE-driven so a new ruling ships by
# writing JSON — not by editing this module (persistence doctrine).
#
# Schema `bridge-fit-rulings-v1`:
#   {"schema": "bridge-fit-rulings-v1", "ruled_at_utc": "...",
#    "rulings": [{"program": "...", "verdict": "FIT_APPLY|NOT_FIT_PARKED",
#                 "reason": "..."}]}
#
# Files are read newest-last (by `ruled_at_utc`, then filename), so the newest
# ruling for a program name wins. Loading is lazy and cached: the classifier
# functions above stay pure, and `evaluate_application` can always be handed an
# explicit rulings mapping instead of touching the disk at all.
BRIDGE_RULINGS_GLOB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "reports", "glp-clinic-scout", "affiliate-machine",
    "bridge-fit-rulings-*.json")

RULING_FIT = "FIT_APPLY"
RULING_NOT_FIT = "NOT_FIT_PARKED"

_RULINGS_CACHE: dict[str, dict[str, tuple[str, str]]] = {}


def load_bridge_rulings(pattern: str | None = None,
                        use_cache: bool = True) -> dict[str, tuple[str, str]]:
    """Map lowercased program name -> (verdict, reason). Newest ruling wins.

    Malformed files and unknown verdicts are skipped rather than raised: a
    typo in one ruling file must not take the daily apply job down.
    """
    pattern = pattern or BRIDGE_RULINGS_GLOB
    if use_cache and pattern in _RULINGS_CACHE:
        return _RULINGS_CACHE[pattern]

    loaded: list[tuple[str, str, list]] = []
    for path in _glob.glob(pattern):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if doc.get("schema") != "bridge-fit-rulings-v1":
            continue
        rulings = doc.get("rulings")
        if isinstance(rulings, list):
            loaded.append((str(doc.get("ruled_at_utc") or ""), path, rulings))

    out: dict[str, tuple[str, str]] = {}
    for _stamp, _path, rulings in sorted(loaded, key=lambda r: (r[0], r[1])):
        for r in rulings:
            if not isinstance(r, dict):
                continue
            program = str(r.get("program") or "").strip()
            verdict = str(r.get("verdict") or "").strip()
            if not program or verdict not in (RULING_FIT, RULING_NOT_FIT):
                continue
            out[program.lower()] = (verdict, str(r.get("reason") or "").strip())

    if use_cache:
        _RULINGS_CACHE[pattern] = out
    return out


def _word_bounded_contains(hay: str, needle: str) -> bool:
    """`needle` appears in `hay` on token boundaries (not inside a word)."""
    if not hay or not needle:
        return False
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", hay))


def match_bridge_ruling(name: str,
                        rulings: dict[str, tuple[str, str]]
                        ) -> tuple[str, str, str] | None:
    """(program, verdict, reason) for `name`, or None.

    Case-insensitive WORD-BOUNDED containment in either direction — a ruling on
    "GoodRx (GoodRx Care)" has to catch the candidate row "GoodRx Care", and a
    ruling on "Canadian Insulin" has to catch "Canadian Insulin Affiliate
    Program". The boundary requirement is load-bearing: raw substring matching
    handed the sunglasses brand "goodr" the GoodRx FIT ruling on the real
    2026-08-12 awin enumeration, which would have filed an application to an
    eyewear company. The longest matching program name wins so a specific
    ruling is never shadowed by a shorter one.
    """
    n = (name or "").strip().lower()
    if not n:
        return None
    hits = [(p, v, r) for p, (v, r) in rulings.items()
            if _word_bounded_contains(n, p) or _word_bounded_contains(p, n)]
    if not hits:
        return None
    program, verdict, reason = max(hits, key=lambda h: len(h[0]))
    return program, verdict, reason


def evaluate_application(c: Candidate,
                         rulings: dict[str, tuple[str, str]] | None = None,
                         policy_snapshot=None,
                         ) -> tuple[Decision, FitVerdict]:
    """The terminal decision for the DAILY AUTO-APPLY job (GLP site only).

    Returns the decision plus the fit verdict so every candidate carries an
    auditable `candidate -> verdict + reason` receipt, including the ones that
    never reach the GLP gate because niche/geo already rejected them.

    Terminals: APPLY_OK | SKIPPED_NICHE | SKIPPED_GEO | SKIPPED_NOT_GLP_FIT |
    SKIPPED_BRIDGE_RULING | SKIPPED_OPERATOR_RULING | NEEDS_SITE_EVIDENCE
    (name-only record, site not harvested yet) | REJECTED_LEGALITY (the only
    true rejection under the 2026-08-13 apply-breadth ruling — every other
    non-APPLY terminal is a PARK, auditable in the NOT_FIT ledger).

    A bridge fit ruling on the program name is consulted FIRST and is terminal
    in both directions. Pass `rulings` explicitly to bypass disk entirely.
    """
    # Registry and network-state exclusions are terminal at the application
    # boundary itself, before operator snapshots and bridge FIT overrides.
    excluded, exclusion_reason = application_exclusion(c.name)
    if excluded:
        why = [f"application_exclusion:{excluded}", f"exclusion_reason:{exclusion_reason}",
               contract_tag("park_permanently_excluded_provider")]
        return (Decision(EXCLUDED_PROVIDER_DECISION, None, why),
                FitVerdict(False, "excluded_provider", why,
                           contract_decision=CONTRACT_PARK,
                           contract_reason="park_permanently_excluded_provider"))
    if re.search(r"\b(?:paused|suspended|inactive|disabled)\b", c.network_status or "", re.I):
        why = [f"network_offer_state:{c.network_status}",
               contract_tag("park_network_paused_offer")]
        return (Decision("SKIPPED_NETWORK_PAUSED", None, why),
                FitVerdict(False, "network_paused", why,
                           contract_decision=CONTRACT_PARK,
                           contract_reason="park_network_paused_offer"))

    # 0. General operator policy is an exact program identity gate. Coordinators
    # pass one frozen snapshot through a run; a standalone caller gets a fresh
    # inert-or-current local snapshot without any provider access.
    if policy_snapshot is None:
        try:
            from scripts import affiliate_operator_rulings as _operator_rulings
            policy_snapshot = _operator_rulings.load_snapshot()
        except Exception:  # malformed/unavailable policy is inert, never broadens a route
            policy_snapshot = None
    if policy_snapshot is not None:
        policy = policy_snapshot.evaluate_program(c.name)
        if policy is not None and policy.verdict == "HOLD":
            hold_reason = ("reject_operator_ruling_legality"
                           if LEGALITY_RULING_RE.search(policy.reason or "")
                           else "park_operator_ruling_hold")
            why = [
                f"operator_ruling:{policy.instruction_key}",
                f"operator_ruling_reason:{policy.reason}",
                f"operator_ruling_version:{policy.snapshot_version}",
                contract_tag(hold_reason),
            ]
            return (Decision("SKIPPED_OPERATOR_RULING", None, why),
                    FitVerdict(False, "operator_ruling", why,
                               contract_decision=CONTRACT_REASONS[hold_reason],
                               contract_reason=hold_reason))

    # 0a-. A permanently excluded provider is terminal, ahead of every automated
    # gate AND ahead of a human bridge FIT ruling: Fella Health's own terms
    # prohibit review sites and the whole business excludes it, forever. The
    # canonical operator-rulings snapshot above already holds it by name and its
    # terminal is preserved; this is the fail-safe for the paths that have no
    # snapshot (an unreadable policy file degrades to inert, and callers may pass
    # `rulings=` directly), because "permanently excluded" cannot depend on a
    # file being readable.
    excluded = permanently_excluded_provider(c.name)
    if excluded:
        why = [f"permanently_excluded_provider:{excluded}",
               "permanent_business_wide_exclusion_review_site_prohibition",
               contract_tag("park_permanently_excluded_provider")]
        return (Decision(EXCLUDED_PROVIDER_DECISION, None, why),
                FitVerdict(False, "permanently_excluded", why,
                           contract_decision=CONTRACT_PARK,
                           contract_reason="park_permanently_excluded_provider"))

    # 0a. Bridge ruling — a human verdict outranks every automated gate below
    # it. A frozen general-policy snapshot carries a compatible legacy mapping
    # so every candidate in one job sees the same ruling bytes.
    bridge_rulings = (dict(policy_snapshot.bridge_rulings)
                      if policy_snapshot is not None and rulings is None
                      else (load_bridge_rulings() if rulings is None else rulings))
    ruled = match_bridge_ruling(c.name, bridge_rulings)
    if ruled:
        program, verdict, reason = ruled
        if verdict == RULING_FIT:
            why = [f"bridge_ruling:{reason}", f"bridge_ruling_program:{program}",
                   contract_tag("fit_bridge_ruling")]
            return (Decision("APPLY_OK", "glp1-weightloss", why),
                    FitVerdict(True, "bridge_ruling", why,
                               contract_decision=CONTRACT_APPLY,
                               contract_reason="fit_bridge_ruling"))
        ruling_reason = ("reject_bridge_ruling_legality"
                         if LEGALITY_RULING_RE.search(reason or "")
                         else "park_bridge_ruling_not_fit")
        why = [f"bridge_ruling:{reason}", f"bridge_ruling_program:{program}",
               contract_tag(ruling_reason)]
        return (Decision("SKIPPED_BRIDGE_RULING", None, why),
                FitVerdict(False, "bridge_ruling", why,
                           contract_decision=CONTRACT_REASONS[ruling_reason],
                           contract_reason=ruling_reason))

    text = _text(c)

    # 0b. Legality — the ONLY rejection class within fit (operator 2026-08-13).
    # A record advertising prescription drugs without prescriptions, or
    # "research only" compounds marketed for use, is rejected outright. Both
    # halves are required: the phrase AND prescription-drug context.
    illegal = (ILLEGAL_OPERATION_RE.search(text)
               if RX_DRUG_CONTEXT_RE.search(text) else None)
    if illegal:
        why = [f"illegal_operation_signal:{illegal.group(0).lower()}",
               contract_tag("reject_illegal_operation_signal")]
        return (Decision("REJECTED_LEGALITY", None, why),
                FitVerdict(False, "legality", why,
                           contract_decision=CONTRACT_REJECT,
                           contract_reason="reject_illegal_operation_signal"))

    # 1. Hard blocklist categories are outside GLP-relevant fit by definition
    # (travel, gambling, fashion, ...) — parked under the historical
    # SKIPPED_NICHE terminal, even when a stray GLP token appears in the copy
    # ("semaglutide retreat" is a resort, not a clinic).
    for label, pattern in NICHE_BLOCKLIST:
        if re.search(pattern, text, re.I):
            why = [f"blocklist:{label}", contract_tag("park_blocklisted_category")]
            return (Decision("SKIPPED_NICHE", None, why),
                    FitVerdict(False, "none", ["not_evaluated:skipped_niche"],
                               contract_decision=CONTRACT_PARK,
                               contract_reason="park_blocklisted_category"))

    site = c.site
    site_fetched = bool(site and site.fetched)

    # 2. Nothing to read and nothing harvested: do not pretend this is a
    #    verdict. A name that DECLARES another vertical is the exception — that
    #    is real evidence, and gate 1 answers it without a fetch (operator's
    #    named case: "TRT Nation" is NOT FIT, full stop). Only the genuine
    #    unknowns — the "Ro.co"/"PlushCare"/"Trim Rx" class, a bare brand name
    #    and no text — become an enrichment request instead of a rejection.
    #    An operator-NAMED GLP-adjacent program (PlexusDx/CoreAge/T2D) is
    #    likewise already decided evidence and needs no fetch.
    name_lower = (c.name or "").lower()
    if (is_metadata_poor(c) and not site_fetched and not _off_verticals(name_lower)
            and not OPERATOR_NAMED_GLP_ADJACENT_RE.search(name_lower)):
        reasons = ["metadata_poor:name_only_record", "site_evidence_not_harvested",
                   contract_tag("park_needs_site_evidence")]
        return (Decision("NEEDS_SITE_EVIDENCE", None, reasons),
                FitVerdict(False, "none", ["not_evaluated:needs_site_evidence"],
                           contract_decision=CONTRACT_PARK,
                           contract_reason="park_needs_site_evidence"))

    # 3. GLP-relevant fit is now the PRIMARY gate (2026-08-13): the widened
    #    ladder in `glp_application_fit` decides relevance; the legacy niche
    #    lanes only supply the lane label and receipts vocabulary.
    verdict = glp_application_fit(c)

    if verdict.fit:
        niche_lane, niche_reasons = classify_niche(c)
        us_ok, geo_reasons = classify_geo(c)
        base_reasons = list(niche_reasons)
        # 2026-09-17: an APPLICATION rides its RULED lane, not whichever niche
        # lane the vocabulary matched first. "Enhanced Tirzepatide" scoring one
        # `peptides` term hit must not carry a peptides-longevity authority lane
        # into the executor gate. The niche lane stays in the reasons as
        # evidence; an unruled tier gets a lane no manifest activates.
        lane = LANE_BY_TIER.get(verdict.tier, UNAUTHORIZED_APPLY_LANE)
        if niche_lane and niche_lane != lane:
            # Consumers that re-derive the lane from the `lane:` reason token
            # rather than from Decision.lane (scripts/glp_application_machine.py
            # :988-1000) must see the RULED lane too, or an explicit_glp1 fit
            # whose vocabulary happened to match `peptides` would still ride a
            # peptides-longevity authority lane.
            base_reasons = [r for r in base_reasons if not r.startswith("lane:")]
            base_reasons.insert(0, f"lane:{lane}")
            base_reasons.append(f"niche_lane_evidence:{niche_lane}")
        if niche_lane is None:
            if verdict.tier == "site_glp1" and site:
                # The record's own text gave the lanes nothing; the provider's
                # site is what overrode them (legacy rescue vocabulary).
                base_reasons += ["site_evidence_overrides_niche", f"site:{site.url}"]
        if not us_ok:
            geo_reason = ("park_us_unverified" if "region_unverified" in geo_reasons
                          else "park_non_us")
            return (Decision("SKIPPED_GEO", lane,
                             base_reasons + geo_reasons + [contract_tag(geo_reason)]),
                    verdict)
        return (Decision("APPLY_OK", lane,
                         base_reasons + geo_reasons + verdict.reasons), verdict)

    # 4. Not GLP-relevant: park. The shared classifier still names the terminal
    #    the way every downstream consumer knows it (SKIPPED_NICHE for
    #    no-lane/off-lane text, SKIPPED_GEO when geo already fails, and
    #    SKIPPED_NOT_GLP_FIT for in-lane programs outside GLP fit).
    base = evaluate(c)
    if base.decision != "APPLY_OK":
        park_reason = (("park_us_unverified" if "region_unverified" in base.reasons
                        else "park_non_us") if base.decision == "SKIPPED_GEO"
                       else (verdict.contract_reason or "park_not_glp_relevant"))
        return (Decision(base.decision, base.lane,
                         base.reasons + [contract_tag(park_reason)]), verdict)
    return (Decision("SKIPPED_NOT_GLP_FIT", base.lane,
                     base.reasons + verdict.reasons), verdict)


# =============================================================================
# SHARED CANDIDATE CHOKE / PARKING LEDGER
# =============================================================================
# This is deliberately in the same module as the old classifier.  Callers that
# used `evaluate()` or `evaluate_application()` keep their old return contracts,
# while every output boundary can now call one deterministic, serializable gate.
# The gate is provider-free and has no dependency on Sheets, Gmail, or browser
# state.  Rejected and incomplete records are history, not Sheet candidates.
OPERATIONAL_DECISION = "eligible_operational"
HISTORY_DECISION = "out_of_scope_history"
INCOMPLETE_DECISION = "incomplete_awaiting_external"
PARKING_OPERATIONAL = "operational"
PARKING_HISTORY = "history_parking"
PARKING_INCOMPLETE = "awaiting_external_parking"
DEFAULT_PARKING_PATH = Path(
    "/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/affiliate-history-parking.jsonl"
)

# The existing classifier's blocklist is intentionally conservative, but these
# terms are the portfolio-wide hard boundary.  In particular, a bare "Qatar"
# name must not become operational merely because another field mentions GLP-1.
TRAVEL_LUXURY_RETAIL_TERMS = (
    "travel", "airline", "airlines", "airways", "flight", "flights", "hotel",
    "hotels", "resort", "cruise", "vacation", "holiday", "holidays", "tour",
    "tourism", "car rental", "luxury", "harrods", "etihad", "qatar",
    "capital holidays", "fashion", "apparel", "clothing", "footwear", "sneaker",
    "handbag", "jewelry", "jewellery", "retail", "department store",
    "general merchandise", "shopping mall",
)
TRAVEL_LUXURY_RETAIL_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(term) for term in TRAVEL_LUXURY_RETAIL_TERMS) + r")\b",
    re.I,
)
SECRET_KEY_RE = re.compile(
    r"(?:secret|token|password|passwd|credential|api[_-]?key|access[_-]?key|private[_-]?key|authorization|cookie)",
    re.I,
)
SECRET_VALUE_RE = re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9+/=_\-.]{8,}", re.I)


def _candidate_mapping(candidate: object) -> dict:
    """Return a shallow, JSON-friendly mapping without mutating the input."""
    if isinstance(candidate, dict):
        return dict(candidate)
    if hasattr(candidate, "__dict__"):
        return dict(vars(candidate))
    return {"name": str(candidate or "")}


def _first(mapping: dict, *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (list, tuple, set)):
            value = ", ".join(str(item) for item in value)
        if str(value or "").strip():
            return str(value).strip()
    return ""


def _candidate_name(mapping: dict) -> str:
    return _first(mapping, "display_name", "name", "provider", "advertiser_name", "program_name", "program", "title")


def _candidate_categories(mapping: dict) -> tuple[str, ...]:
    raw = mapping.get("categories", mapping.get("category", mapping.get("vertical", mapping.get("tags", ()))))
    if isinstance(raw, str):
        return tuple(part.strip() for part in re.split(r"[,|;/]", raw) if part.strip())
    if isinstance(raw, (list, tuple, set)):
        return tuple(str(part).strip() for part in raw if str(part).strip())
    return (str(raw).strip(),) if str(raw or "").strip() else ()


def _candidate_url(mapping: dict) -> str:
    return _first(mapping, "application_url", "apply_url", "applyUrl", "applicationUrl", "signup_url")


def is_https_application_url(value: object) -> bool:
    """Only an explicit HTTPS application URL can make a row operational."""
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    return parsed.scheme.lower() == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


def has_fit_evidence(mapping: dict) -> bool:
    """Whether a candidate has record-level evidence for an application decision.

    A queue lane is routing metadata, not evidence. Callers that require this
    invariant must retain a source excerpt/category before they invoke the
    candidate choke.
    """
    if _first(mapping, "description", "evidence_excerpt", "snippet", "fit_reason", "reason"):
        return True
    if _candidate_categories(mapping):
        return True
    site = mapping.get("site")
    return bool(isinstance(site, SiteEvidence) and site.fetched)


def _redact_secrets(value: object, key: str = "") -> object:
    if SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact_secrets(v, str(k)) for k, v in value.items() if not SECRET_KEY_RE.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [_redact_secrets(item, key) for item in value]
    if isinstance(value, str) and SECRET_VALUE_RE.search(value):
        return SECRET_VALUE_RE.sub("[REDACTED]", value)
    return value


def non_secret_row(candidate: object) -> dict:
    """Canonical complete row for the local ledger; credentials never persist."""
    return _redact_secrets(_candidate_mapping(candidate))  # type: ignore[return-value]


def candidate_fingerprint(candidate: object) -> str:
    payload = non_secret_row(candidate)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _scope_candidate(mapping: dict) -> tuple[Decision, FitVerdict]:
    name = _candidate_name(mapping)
    description = _first(mapping, "description", "evidence_excerpt", "snippet", "fit_reason", "reason")
    url = _first(mapping, "official_url", "destination_url", "url", "site_url")
    region = _first(mapping, "region", "geo", "destination", "countries", "country")
    network_region = _first(mapping, "network_region", "network_geo")
    currency = _first(mapping, "currency", "payout_currency")
    site_raw = mapping.get("site")
    site = site_raw if isinstance(site_raw, SiteEvidence) else None
    return evaluate_application(
        Candidate(
            name=name,
            network=_first(mapping, "network", "hosted_network", "network_route"),
            program_id=_first(mapping, "program_id", "offer_id", "id"),
            description=description,
            categories=_candidate_categories(mapping),
            region=region,
            currency=currency,
            url=url,
            network_region=network_region,
            site=site,
        )
    )


def _out_of_scope_reasons(mapping: dict) -> list[str]:
    haystack = " ".join(
        (_candidate_name(mapping), _first(mapping, "description", "evidence_excerpt", "snippet", "fit_reason"),
         " ".join(_candidate_categories(mapping)), _first(mapping, "destination", "geo", "region", "countries"))
    ).lower()
    hits = sorted({term for term in TRAVEL_LUXURY_RETAIL_TERMS if re.search(r"\b" + re.escape(term) + r"\b", haystack, re.I)})
    return ["out_of_scope:travel_luxury_retail", "out_of_scope_terms:" + ",".join(hits)] if hits else []


def _parent_step(mapping: dict) -> str:
    name = _candidate_name(mapping).lower()
    network = _first(mapping, "network", "hosted_network").lower()
    program_id = _first(mapping, "program_id", "offer_id", "id")
    if "plexusdx" in name:
        return "Parent: find and complete the PlexusDx affiliate application by hand, or add the exact application URL to the direct supply inventory."
    if "t2d" in name:
        return "Parent: find and complete the T2D affiliate application by hand, or add the exact application URL to the direct supply inventory."
    if "coreage" in name or network == "katalys":
        return f"Parent: use the authenticated Katalys GraphQL/UI application fallback for program {program_id or '[program_id]'} and capture the exact application URL."
    return "Parent: research and record the exact provider application URL in the local supply evidence before operational admission."


def _requires_local_program_evidence(mapping: dict) -> bool:
    name = _candidate_name(mapping).lower()
    return any(token in name for token in ("plexusdx", "coreage", "t2d"))


def _has_local_program_evidence(mapping: dict) -> bool:
    return bool(
        mapping.get("evidence")
        or mapping.get("evidence_source")
        or mapping.get("provider_evidence")
        or mapping.get("program_evidence")
        or mapping.get("local_evidence")
    )


def choke_candidate(candidate: object, *, require_application_url: bool = True,
                    require_fit_evidence: bool = False, operation: str = "", persist: bool = False,
                    parking_path: str | os.PathLike[str] | None = None) -> dict:
    """The only candidate gate allowed before Sheet or application output.

    The result contains no non-serializable objects and is safe to put in a
    receipt.  `persist=True` is reserved for output boundaries; it records the
    complete non-secret input exactly once when the result is parked.
    """
    mapping = _candidate_mapping(candidate)
    name = _candidate_name(mapping)
    fingerprint = candidate_fingerprint(mapping)
    reasons = _out_of_scope_reasons(mapping)
    evidence = {"operation": operation, "name": name, "network": _first(mapping, "network", "hosted_network"),
                "program_id": _first(mapping, "program_id", "offer_id", "id")}
    if reasons:
        result = {
            "schema": "affiliate_candidate_choke_result_v1", "decision": HISTORY_DECISION,
            "parking": PARKING_HISTORY, "operational": False, "fingerprint": fingerprint,
            "reason_codes": reasons, "evidence": evidence, "candidate": non_secret_row(mapping),
        }
    elif require_fit_evidence and not has_fit_evidence(mapping):
        result = {
            "schema": "affiliate_candidate_choke_result_v1", "decision": INCOMPLETE_DECISION,
            "parking": PARKING_INCOMPLETE, "operational": False, "fingerprint": fingerprint,
            "reason_codes": ["missing_fit_evidence", "awaiting_external_fit_evidence",
                             "parent_step:" + _parent_step(mapping)],
            "evidence": {**evidence, "required_fit_evidence": True},
            "candidate": non_secret_row(mapping),
        }
    else:
        decision, fit = _scope_candidate(mapping)
        if not decision.apply_ok:
            result = {
                "schema": "affiliate_candidate_choke_result_v1", "decision": HISTORY_DECISION,
                "parking": PARKING_HISTORY, "operational": False, "fingerprint": fingerprint,
                "reason_codes": list(decision.reasons) or [decision.decision.lower()],
                "evidence": {**evidence, "classifier_decision": decision.decision, "fit_tier": fit.tier},
                "candidate": non_secret_row(mapping),
            }
        else:
            application_url = _candidate_url(mapping)
            if _requires_local_program_evidence(mapping) and not _has_local_program_evidence(mapping):
                result = {
                    "schema": "affiliate_candidate_choke_result_v1", "decision": INCOMPLETE_DECISION,
                    "parking": PARKING_INCOMPLETE, "operational": False, "fingerprint": fingerprint,
                    "reason_codes": ["missing_local_provider_program_evidence",
                                     "parent_step:" + _parent_step(mapping)],
                    "evidence": {**evidence, "fit_tier": fit.tier},
                    "candidate": non_secret_row(mapping),
                }
            elif require_application_url and not is_https_application_url(application_url):
                result = {
                    "schema": "affiliate_candidate_choke_result_v1", "decision": INCOMPLETE_DECISION,
                    "parking": PARKING_INCOMPLETE, "operational": False, "fingerprint": fingerprint,
                    "reason_codes": ["missing_application_url", "awaiting_external_application_route",
                                     "parent_step:" + _parent_step(mapping)],
                    "evidence": {**evidence, "fit_tier": fit.tier, "classifier_reasons": list(decision.reasons)},
                    "candidate": non_secret_row(mapping),
                }
            else:
                result = {
                    "schema": "affiliate_candidate_choke_result_v1", "decision": OPERATIONAL_DECISION,
                    "parking": PARKING_OPERATIONAL, "operational": True, "fingerprint": fingerprint,
                    "reason_codes": list(decision.reasons),
                    "evidence": {**evidence, "fit_tier": fit.tier, "classifier_reasons": list(decision.reasons)},
                    "candidate": non_secret_row(mapping),
                }
    if persist and not result["operational"]:
        append_parking_record(result, parking_path=parking_path)
    return result


def append_parking_record(result: dict, *, parking_path: str | os.PathLike[str] | None = None) -> dict:
    """Locked, fsynced, fingerprint-idempotent local JSONL append."""
    if result.get("operational"):
        raise ValueError("operational_candidate_cannot_be_parked")
    path = Path(parking_path or DEFAULT_PARKING_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "affiliate_candidate_parking_record_v1",
        "fingerprint": str(result["fingerprint"]),
        "decision": result["decision"], "parking": result["parking"],
        "reason_codes": list(result.get("reason_codes") or []),
        "evidence": _redact_secrets(result.get("evidence") or {}),
        "candidate": non_secret_row(result.get("candidate") or {}),
    }
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()
    with path.open("a+", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        for line in handle:
            try:
                if json.loads(line).get("fingerprint") == record["fingerprint"]:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    return {"path": str(path), "fingerprint": record["fingerprint"], "inserted": False}
            except (ValueError, TypeError):
                continue
        handle.seek(0, os.SEEK_END)
        handle.write(encoded.decode())
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle, fcntl.LOCK_UN)
    return {"path": str(path), "fingerprint": record["fingerprint"], "inserted": True}


def choke_candidates(candidates: list[object], *, require_application_url: bool = True,
                     require_fit_evidence: bool = False, operation: str = "", persist: bool = False,
                     parking_path: str | os.PathLike[str] | None = None) -> tuple[list[object], list[dict]]:
    """Filter a batch while retaining an auditable decision for every input."""
    eligible, parked = [], []
    for candidate in candidates:
        result = choke_candidate(candidate, require_application_url=require_application_url,
                                  require_fit_evidence=require_fit_evidence, operation=operation,
                                  persist=persist, parking_path=parking_path)
        (eligible if result["operational"] else parked).append(candidate if result["operational"] else result)
    return eligible, parked
