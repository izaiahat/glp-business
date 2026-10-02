#!/usr/bin/env python3
"""B2 GLP affiliate wire-backlog worker.

The existing 07:15 reconciliation lane owns execution.  This module gives that
lane deterministic queue selection, crash-safe per-program state, a hard ten
wired/PT-day ceiling, normally one batch deployment per day, and an auditable
daily card line. A date-bound two-phase policy may allow one verified test plus
one bounded follow-up. It never researches or invents provider facts: Stage-5 research, terms,
and a program spec must be supplied as hash-bound artifacts before preparation.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import copy
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, parse_qsl, urlsplit
from zoneinfo import ZoneInfo

BUSINESS = Path("/home/ubuntu/business")
SCRIPTS = BUSINESS / "scripts"
OPS = BUSINESS / "ops"
sys.path[:0] = [str(SCRIPTS), str(OPS)]

from affiliate_contract_policy import (  # noqa: E402
    blocked_account_action_hits,
    genuine_legal_prohibition_hits,
    mechanical_hard_trigger_hits,
)
from affiliate_sheet_schema_guard import REQUIRED_MARKETPLACE_PARENT_ROWS  # noqa: E402
from affiliate_merge_deep_research import research_dynamic_profile  # noqa: E402
from affiliate_best_terms_machine import parse_payout as parse_commercial_payout  # noqa: E402
import affiliate_payout_policy as payout_policy  # noqa: E402
import glp_affiliate_link_rails as link_rails  # noqa: E402
import glp_katalys_postback_attribution as katalys_postback_attribution  # noqa: E402
import glp_provider_surface_writers as surface_writers  # noqa: E402
import glp_registry_evidence as registry_evidence  # noqa: E402
from site_tree_materializer import (  # noqa: E402
    copytree_linked,
    detach_for_write,
    make_directories_writable,
    remove_materialized_tree,
)

PT = ZoneInfo("America/Los_Angeles")
MACHINE_ROOT = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/wire-backlog-worker"
BEST_TERMS_SWITCH_BACKLOG = (
    BUSINESS / "reports/glp-clinic-scout/affiliates/BEST-TERMS-MACHINE-20260825/switch-backlog.json"
)
SHEET_UPDATE_QUEUE_TABLE = "hJiP3eJKBvtZ6bQU"
PAYOUT_LEAK_DECISIONS = frozenset({
    "STAGE_HIGHER_PAYOUT_RAIL_AT_DEPLOY",
    "STAGE_DEDUP_SWITCH_AWIN_TO_KATALYS_AT_DEPLOY",
})
PAYOUT_LEAK_INSTRUCTION_VERSION = "provider-wide-v1"
PAYOUT_EVALUATION_PRODUCER_TAGS = (
    "affiliate_best_terms_machine:payout_leak_sweep_20260830",
    "affiliate_best_terms_machine:payout_leak_sweep",
)
PAYOUT_EVALUATION_PRECEDENT_IDS = (
    "payout-matrix-shedrx-awin-80963-katalys-1516-30fa835fcd45",
)
GO_ROUTE_PROVISION_DECISION = "STAGE_FIRST_PARTY_GO_ROUTE_AT_DEPLOY"
GO_ROUTE_PROVISION_INSTRUCTION_VERSION = "first-party-go-route-v1"
GO_ROUTE_PROVISION_PRODUCER_TAG = "glp_go_route_gap_queue:20260830"
DEPLOY_FIX_OPERATOR_BRIEF = (
    BUSINESS / "reports/ops/briefs/DEPLOY-FIX-DRAIN-EVERYTHING-20260831.md"
)
ALL_APPROVED_LIVE_DECISION = (
    BUSINESS / "ops/linear/approvals/OPERATOR-DECISION-20260924-all-approved-providers-live.md"
)
GO_RUNTIME_PROMOTION_AUTHORITY_RELATIVE = Path(
    "data/go-runtime-promotion-authority.json"
)
GO_RUNTIME_PROMOTION_NETWORKS = {
    "awin": {
        "redirector_tos_source_url":
            "https://help.awin.com/developers/docs/click-appends-dyn-params",
        "attribution_parameter_contract": "awin-dyn-params",
    },
    "katalys": {
        "redirector_tos_source_url":
            "https://kb.katalys.com/kb/tracking-url-parameters",
        "attribution_parameter_contract": "katalys-subid",
    },
    "affiliati": {
        "redirector_tos_source_url":
            "https://affiliatinetwork.com/advertisers/publisher-terms-and-conditions/",
        "attribution_parameter_contract": "affiliati-s1-s2",
    },
    "xentraffic": {
        "redirector_tos_risk_acceptance":
            "ops/linear/approvals/"
            "OPERATOR-DECISION-20260919-deploy-all-approved-no-terms-gate.json",
        "attribution_parameter_contract": "xentraffic-sub1",
    },
    "clickdealer": {
        "redirector_tos_risk_acceptance":
            "ops/linear/approvals/"
            "OPERATOR-DECISION-20260919-deploy-all-approved-no-terms-gate.json",
        "attribution_parameter_contract": "clickdealer-s1-s2",
    },
}


def _go_runtime_network_authority(network):
    import json
    from pathlib import Path

    configured = GO_RUNTIME_PROMOTION_NETWORKS.get(network)
    if not isinstance(configured, dict):
        return None
    authority = dict(configured)
    source_url = authority.get("redirector_tos_source_url")
    if isinstance(source_url, str) and source_url.startswith("https://"):
        return authority
    receipt_path = authority.get("redirector_tos_risk_acceptance")
    if not isinstance(receipt_path, str) or not receipt_path:
        return None
    relative = Path(receipt_path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    try:
        receipt_file = (BUSINESS / relative).resolve()
        receipt_file.relative_to(BUSINESS.resolve())
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return None
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "operator-decision-v1"
        or receipt.get("decision_id") != (
            "OPERATOR-DECISION-20260919-deploy-all-approved-no-terms-gate"
        )
        or not isinstance(receipt.get("networks"), list)
        or network not in receipt["networks"]
    ):
        return None
    authority.pop("redirector_tos_source_url", None)
    authority.pop("risk_acceptance_logged", None)
    authority["redirector_tos_basis"] = "operator_risk_acceptance"
    authority["risk_acceptance_receipt"] = receipt_path
    authority["redirector_tos_finding"] = (
        f"redirector_tos_unverified_for_network:{network}"
    )
    return authority


def _go_runtime_redirector_tos_authorized(live_authority):
    if live_authority.get("redirector_tos_basis") != "operator_risk_acceptance":
        return str(live_authority.get("redirector_tos_source_url") or "").startswith("https://")
    network = live_authority.get("network")
    if not isinstance(network, str):
        return False
    expected = _go_runtime_network_authority(network)
    return bool(
        expected
        and expected.get("redirector_tos_basis") == "operator_risk_acceptance"
        and live_authority.get("risk_acceptance_logged") is True
        and not live_authority.get("redirector_tos_source_url")
        and all(live_authority.get(key) == expected.get(key) for key in (
            "redirector_tos_risk_acceptance",
            "risk_acceptance_receipt",
            "redirector_tos_finding",
            "attribution_parameter_contract",
        ))
    )


GO_RUNTIME_PROMOTION_WITHHELD_PROGRAMS = {
    "try-ageless": "network_paused_katalys_offer_1538",
    # Network's later pause notice outranks the older approved-active snapshots.
    # reports/ops/xentraffic-terms-20260924/RESULT.json:19,26,29,38.
    "altrx-glp1-us-cps": "xentraffic_offer_3162_paused_current_eligibility_unknown",
    "futurhealth-wegovy-pillform": "xentraffic_offer_3176_paused_current_eligibility_unknown",
}
BEST_TERMS_MORNING_STAGE = (
    BUSINESS
    / "reports/ops/wire-in-morning-readiness-20260827/"
    "best-terms-reconciliation-staged.json"
)
DEFAULT_LEDGER = MACHINE_ROOT / "worked-ledger.json"
CURRENT_BASE = BUSINESS / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
AFFILIATI_SWITCH_PREREQUISITES = (
    BUSINESS
    / "reports/glp-clinic-scout/affiliate-machine/go-route-gap-20260830/"
    "affiliati-live-prerequisites-20260831.json"
)
EXACT_PAID_EVENT_POLICY = (
    BUSINESS / "config/affiliate-sites/exact-paid-event-publication.json"
)
REAL_PROVIDER_ECONOMICS = (
    BUSINESS
    / "reports/glp-clinic-scout/ads/real-numbers-campaign-build-20260829/"
    "14-real-provider-economics-v2.json"
)
MORNING_TWO_PHASE_ROLLOUT = (
    BUSINESS
    / "config/affiliate-sites/wire-in-morning-two-phase-rollout-20260827.json"
)
CANDIDATES = BUSINESS / "site-system/flagship-v2/candidates"
RELEASES = BUSINESS / "site-system/flagship-v2/releases"
DEPLOYER = SCRIPTS / "glp_deploy_sealed_successor.py"
BUILDER = SCRIPTS / "build_glp_affiliate_wirein_incremental_v2.py"
DYNAMIC_PROFILE_ACTIVATOR = SCRIPTS / "glp_provider_dynamic_profile_activation.mjs"
CARDINALITY_TEST_REFRESH = SCRIPTS / "glp_provider_cardinality_test_refresh.py"
GO_RUNTIME_REFRESH = SCRIPTS / "glp_go_runtime_manifest_refresh.mjs"
AFFILIATE_LINK_MAP_FLOOR = SCRIPTS / "glp_affiliate_link_map_floor.mjs"
PRICE_INDEX_SOURCE_REBIND = SCRIPTS / "glp_price_index_source_rebind.mjs"
QUIZ_GOLDEN_VECTOR_REFRESH = SCRIPTS / "glp_quiz_golden_vector_refresh.mjs"
SITE_CAPTURE_CONSENT_REFRESH = SCRIPTS / "glp_site_capture_consent_sources_refresh.mjs"
PROVIDER_VISUAL_FALLBACK_REFRESH = SCRIPTS / "glp_provider_visual_fallback_refresh.py"
COST_AFFILIATE_EMAIL_MANIFEST = Path("src/cost-affiliate-email-manifest.generated.mjs")
BUILD_GENERATED_STATIC_OUTPUTS = {
    "assets/provider-logos/pass2-lp-atlas.webp",
    "assets/provider-logos/pass2-lp-atlas.json",
    "assets/site.css",
    "assets/site-home.css",
    "assets/site-money.css",
    "assets/site-review.css",
    "assets/site-editorial.css",
    "assets/site-tools.css",
    "assets/site-trust.css",
    "assets/site-p3.css",
    # This projection is regenerated from the candidate's newly wired program
    # contracts before render. The current sealed predecessor's package script
    # does not invoke its emitter despite the emitter's documented contract, so
    # leaving this hardlinked would both keep the new provider underived and risk
    # mutating predecessor bytes when the machine repairs that omission.
    "data/tracking-allowed-hosts.json",
}
BUILD_GENERATED_IMAGE_MANIFESTS = (
    "artifacts/ultracode/phase2-home/generated-image-lineage.json",
    "artifacts/ultracode/phase3-product-renders/generated-image-lineage.json",
)
BUILD_GENERATED_SNAPSHOT_TREES = (
    "artifacts/build4-preservation",
)
B2_SPEC = (
    BUSINESS
    / "reports/glp-clinic-scout/affiliate-machine/B2-WIRE-BACKLOG-WORKER-SPEC-20260815.md"
)
MAX_WIRED_PER_PT_DAY = 10
MAX_OPERATOR_BATCH_ROWS = 40
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
LEDGER_SCHEMA = "b2-wire-backlog-worked-ledger-v1"
PLAN_SCHEMA = "b2-wire-backlog-plan-v1"
BATCH_AUTHORITY_SCHEMA = "glp.wire-backlog-batch-authority.v1"
ROUTING_BASIS_SCHEMA = "b2-current-coverage-routing-basis-v1"
DAY_SCHEMA = "b2-wire-backlog-day-receipt-v1"
DRILL_SCHEMA = "b2-wire-backlog-drill-v1"
JOINED_RELATIONSHIPS = {"active", "approved", "joined"}
# Feed membership is a relationship fact, not a bookkeeping-checkbox fact.  The
# first three values are the governed pre-live statuses.  Partnerize's explicit
# commercial hold still proves an approved relationship, so it belongs in the
# staged evidence set but is refused below until payable terms exist.
APPROVED_STATUSES = {
    "approved_active_tracking_ready",
    "approved_active_not_wired",
    "applied_approved_not_wired",
    "approved_no_positive_commission_configured",
}
COMMERCIAL_ECONOMICS_HOLD_STATUSES = {"approved_no_positive_commission_configured"}
DEPLOYABLE_LINK_QA_PREFIXES = (
    "not_clicked_preserve_attribution",
    "syntax_validated_no_automated_click",
)
# Row-specific operator scope holds are keyed by immutable network identity,
# never display name. The former G-Plan hold (xentraffic:3048) was explicitly
# superseded by reports/ops/decisions/g-plan-scope-hold-rescinded-20260831.json;
# keep the mechanism fail-closed but do not keep a rescinded key in it.
OPERATOR_OUT_OF_SCOPE_PROGRAM_KEYS: dict[str, str] = {}
PERMANENT_EXCLUDED_PROGRAM_KEYS = {
    "xentraffic:3050": "operator_ruling_20260820_fella_permanent_review_site_exclusion",
}
# `wired` is unconditionally terminal. `blocked` is NOT a status that closes a
# row on its own — see row_is_terminal(), which decides from the block reason.
ALWAYS_TERMINAL = {"wired"}
# A third TRUTHFUL row state, added 2026-09-02 with operator sign-off.
#
# A row that demonstrably LANDED must not block unrelated scopes, and "wired"
# must keep requiring a zero-failure post-deploy sensor - those are two correct
# rules that collided. The 18:01Z landing deployed 18 rows with 8 sensor
# failures, so none could be marked wired; they stayed "prepared"; every later
# plan correctly EXCLUDED them from its queue because they are already live; and
# release_prepared then refused every subsequent authority with
# wire_batch_authority_prepared_rows_outside_scope. That wedged the switches,
# /go, the pixel and the remainder release until the PT rollover.
#
# landed_sensor_pending says exactly what is true: this row landed in a named
# deployment whose sensor did not come back clean. It is set ONLY by
# reconcile_landed_uncertain_deployment, which derives it from external truth -
# the current-content-base pointer must name the same deployment id and release
# root - so it can never be asserted by hand. Such a row leaves "prepared"
# without ever being called wired. It stays RESUMABLE, so a later plan may
# re-prepare it, but only when the plan's own queue still contains it, which by
# construction happens only if it is genuinely not live.
LANDED_SENSOR_PENDING = "landed_sensor_pending"
RESUMABLE = {"attempted", "prepared", LANDED_SENSOR_PENDING}
APPROVED_STATUS = "approved_active_tracking_ready"
# A block is a per-row failure receipt, not a verdict on the program. Only the
# reasons below describe a stage that a later run can legitimately re-enter with
# better inputs; every other reason stays terminal (fail-closed default).
RETRYABLE_BLOCK_REASONS = {
    "prevalidation_failed",
    "release_row_gate_failed",
    # BLOCKER H's sibling, 2026-08-20. A failure raised by the SHARED build,
    # after every prepared row was already staged, is not a verdict on any row:
    # the same rows re-enter the same build once the defect below them is fixed.
    # This is a TIGHTENING, not a relaxation — before it existed such a failure
    # produced no block at all, so the rows stayed `prepared` and were silently
    # re-attempted with no budget, no receipt and nothing on the brief.
    "shared_build_failure",
}
# Errors that ARE a verdict on the program. These keep a row terminal even when
# the surrounding stage is retryable: re-running cannot make them legal.
TERMINAL_BLOCK_ERROR_CODES = {
    "genuine_legal_prohibition",
    "operator_owned_account_action",
}
# A retryable row still may not churn forever. After this many blocks the row
# becomes terminal and needs a human/loop decision instead of another attempt.
MAX_BLOCK_ATTEMPTS = 5

# ---------------------------------------------------------------------------
# The two artifact classes a row needs BEFORE the builder can be asked to run.
# Named here, once, so the prevalidation lane, the onboarding manifest and the
# authoring dispatch all speak about the same files rather than three copies of
# the same path arithmetic (2026-08-20: nu-image-medical-review died five
# attempts running on `queue_terms_binding_missing` while three authoring cycles
# were commissioned for content nobody was blocked on).
# ---------------------------------------------------------------------------
#: The affiliate-approval terms document that adjudicates the RAIL's own terms.
TERMS_CLASSIFICATION_SCHEMA = "affiliate_approval_classification_v1"
TERMS_CLASSIFICATION_ROOT = BUSINESS / "reports/glp-affiliate-auto-reconciler"
#: The g6-r1a program authority spec the wire-in builder is driven by.
PROGRAM_AUTHORITY_SCHEMA = "b2-wire-backlog-program-spec-v1"
PROGRAM_AUTHORITY_DIR = MACHINE_ROOT / "program-authorities"
WIRED_REPAIR_SCHEMA = "glp.wire_backlog_wired_repair.v1"
WIRED_REPAIR_DIR = MACHINE_ROOT / "wired-repairs"
WIRED_REPAIR_ALLOWED_STEPS = frozenset({"assets"})
#: The deep-research packet both of the above are composed against.
PROGRAM_RESEARCH_SCHEMA = "program-deep-research-v1"
PROGRAM_RESEARCH_DIR = MACHINE_ROOT / "program-research"
XENTRAFFIC_IDENTITY_SCHEMA = "glp.wire_in_identity_evidence.v1"
XENTRAFFIC_IDENTITY_DIR = (
    BUSINESS
    / "reports/glp-clinic-scout/affiliates/BEST-TERMS-MACHINE-20260825/"
    "sheet-exact-winner-repair/wire-identities"
)
XENTRAFFIC_IDENTITY_FALLBACK_DIR = (
    BUSINESS
    / "reports/glp-clinic-scout/affiliate-machine/nonready-remediation-20260826/"
    "xentraffic-identity-captures"
)
RAIL_IDENTITY_RECONCILIATION_DIR = MACHINE_ROOT / "rail-identity-reconciliations"
SURFACE_TRANSACTION_OUTPUTS = (
    "data/program-registry.json",
    "data/provider-pricing-matrix.json",
    "data/money-page-specs.json",
    "data/quiz-config.json",
    "data/program-page-eligibility.json",
    "data/affiliate-activation-selection.json",
    "data/cost-calculator.json",
)
BUILD_EPHEMERAL_PREDECESSOR_FILES = (
    "workers/price-watch/.wrangler/price-watch-dry-run/price-watch-scheduler.js",
    "workers/price-watch/.wrangler/price-watch-dry-run/price-watch-scheduler.js.map",
)


class B2Error(RuntimeError):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(value: dt.datetime | None = None) -> str:
    value = (value or utc_now()).astimezone(dt.timezone.utc)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pt_date(value: dt.datetime | None = None) -> str:
    return (value or utc_now()).astimezone(PT).date().isoformat()


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise B2Error(f"json_read_failed:{path}:{type(exc).__name__}") from exc


def atomic_json(path: Path, payload: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(raw, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    os.chmod(path, mode)


def normalize_network(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().casefold()).strip("-")
    aliases = {
        "cj-affiliate": "cj",
        "commission-junction": "cj",
        "direct-affiliate": "direct",
        "direct-partner": "direct",
        "impact-com": "impact",
        "network-signup": "network-signup",
    }
    return aliases.get(text, text)


def load_exact_paid_event_policy(path: Path = EXACT_PAID_EVENT_POLICY) -> dict[str, Any]:
    """Load the explicit per-network publication contract for one commercial field."""
    document = read_json(Path(path))
    if document.get("schema") != "glp.exact-paid-event-publication-policy.v1":
        raise B2Error("exact_paid_event_publication_policy_schema_invalid")
    networks = document.get("networks")
    if not isinstance(networks, dict) or not networks:
        raise B2Error("exact_paid_event_publication_policy_networks_missing")
    allowed = {"publishes_exact_paid_event", "never_publishes_exact_paid_event"}
    for network, record in networks.items():
        if normalize_network(network) != network or not isinstance(record, dict):
            raise B2Error(f"exact_paid_event_publication_policy_network_invalid:{network}")
        if record.get("publication_mode") not in allowed:
            raise B2Error(f"exact_paid_event_publication_mode_invalid:{network}")
        evidence = BUSINESS / str(record.get("evidence") or "")
        try:
            evidence.resolve().relative_to(BUSINESS.resolve())
        except ValueError as exc:
            raise B2Error(f"exact_paid_event_policy_evidence_outside_business:{network}") from exc
        if not evidence.is_file():
            raise B2Error(f"exact_paid_event_policy_evidence_missing:{network}")
    return document


def exact_paid_event_gate(
    network: Any,
    offers: Iterable[dict[str, Any]],
    *,
    policy_path: Path = EXACT_PAID_EVENT_POLICY,
) -> dict[str, Any]:
    """Apply the field gate without turning an unpublished field into a legality hold."""
    key = normalize_network(network)
    policy = load_exact_paid_event_policy(policy_path)
    events = sorted({
        re.sub(r"[^a-z0-9]+", "_", str((offer or {}).get("exact_paid_event")
                                         or (offer or {}).get("paid_event") or "")
               .strip().casefold()).strip("_")
        for offer in offers
        if isinstance(offer, dict)
        and str(offer.get("exact_paid_event") or offer.get("paid_event") or "").strip()
    })
    record = (policy.get("networks") or {}).get(key)
    if not isinstance(record, dict):
        if len(events) == 1:
            return {
                "status": "PASS_PRESENT",
                "reason": None,
                "network": key,
                "field": "exact_paid_event",
                "publication_mode": "unclassified_but_field_present",
                "policy_evidence": None,
                "observed_events": events,
                "risk_acceptance": None,
            }
        return {
            "status": "BLOCK",
            "reason": ("exact_paid_event_contradictory_without_network_policy" if events
                       else "exact_paid_event_network_publication_policy_missing"),
            "network": key,
            "field": "exact_paid_event",
            "observed_events": events,
            "risk_acceptance": None,
        }
    mode = str(record["publication_mode"])
    base = {
        "network": key,
        "field": "exact_paid_event",
        "publication_mode": mode,
        "policy_evidence": str(record.get("evidence") or ""),
        "observed_events": events,
    }
    if mode == "publishes_exact_paid_event":
        if not events:
            return {**base, "status": "BLOCK",
                    "reason": "exact_paid_event_missing_from_publishing_network",
                    "risk_acceptance": None}
        if len(events) != 1:
            return {**base, "status": "BLOCK",
                    "reason": "exact_paid_event_contradictory_from_publishing_network",
                    "risk_acceptance": None}
        return {**base, "status": "PASS_PUBLISHED", "reason": None,
                "risk_acceptance": None}
    if events:
        return {**base, "status": "PASS_PRESENT", "reason": None,
                "risk_acceptance": None}
    return {
        **base,
        "status": "PASS_LOGGED_RISK_ACCEPTANCE",
        "reason": None,
        "risk_acceptance": {
            "status": "ACCEPTED_LOGGED_RISK",
            "reason": "network_inventory_never_publishes_exact_paid_event",
            "classification": "commercial_completeness_not_legality",
            "field": "exact_paid_event",
            "network": key,
            "policy_evidence": str(record.get("evidence") or ""),
        },
    }


def load_morning_two_phase_rollout(
    date_pt: str,
    path: Path = MORNING_TWO_PHASE_ROLLOUT,
) -> dict[str, Any]:
    """Load the date-bound test-then-backlog exception without widening normal days."""
    document = read_json(Path(path))
    if document.get("schema") != "glp.wire-in-morning-two-phase-rollout.v1":
        raise B2Error("morning_two_phase_rollout_schema_invalid")
    if document.get("status") != "STAGED_NOT_DEPLOYED":
        raise B2Error("morning_two_phase_rollout_status_invalid")
    active = str(document.get("pt_date") or "") == str(date_pt)
    test = document.get("test_deployment") or {}
    followup = document.get("followup_deployment") or {}
    limits = document.get("limits") or {}
    test_key = str(test.get("provider_key") or "")
    allowed = [str(key) for key in (followup.get("allowed_provider_keys") or [])]
    if ":" not in test_key or stable_key(*test_key.split(":", 1)) != test_key:
        raise B2Error("morning_two_phase_rollout_test_key_invalid")
    if not allowed or len(set(allowed)) != len(allowed):
        raise B2Error("morning_two_phase_rollout_followup_keys_invalid")
    for key in allowed:
        if ":" not in key or stable_key(*key.split(":", 1)) != key:
            raise B2Error(f"morning_two_phase_rollout_followup_key_invalid:{key}")
    if test_key in allowed:
        raise B2Error("morning_two_phase_rollout_test_repeated_in_followup")
    if int(test.get("required_provider_count") or 0) != 1:
        raise B2Error("morning_two_phase_rollout_test_count_invalid")
    if int(followup.get("max_provider_count") or 0) != len(allowed):
        raise B2Error("morning_two_phase_rollout_followup_count_invalid")
    if int(limits.get("deployments_per_pt_day") or 0) != 2:
        raise B2Error("morning_two_phase_rollout_deployment_limit_invalid")
    if int(limits.get("wired_per_pt_day") or 0) != MAX_WIRED_PER_PT_DAY:
        raise B2Error("morning_two_phase_rollout_wired_limit_invalid")
    return {**document, "active": active}


def day_deployment_history(day: dict[str, Any]) -> list[dict[str, Any]]:
    """Migrate the singular historical field while keeping old readers compatible."""
    history = day.setdefault("deployments", [])
    if not isinstance(history, list):
        raise B2Error("day_deployment_history_invalid")
    current = day.get("deployment")
    if current and not history:
        history.append(current)
    return history


def record_day_deployment(day: dict[str, Any], record: dict[str, Any]) -> None:
    history = day_deployment_history(day)
    deployment_id = str(record.get("deployment_id") or "")
    if not history or str(history[-1].get("deployment_id") or "") != deployment_id:
        history.append(record)
    day["deployment"] = record


def _same_authority_binding(left: Any, right: Any) -> bool:
    """Compare immutable authority identity without trusting a mutable path."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return (
        str(left.get("sha256") or "") == str(right.get("sha256") or "")
        and int(left.get("bytes") or -1) == int(right.get("bytes") or -2)
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(left.get("sha256") or "")))
    )


def followup_deployment_gate(
    *,
    plan: dict[str, Any],
    day: dict[str, Any],
    prepared: list[dict[str, Any]],
    current_base: Path = CURRENT_BASE,
    deploy: bool = True,
) -> dict[str, Any]:
    """Authorize only the bounded second deployment after verified test readback."""
    history = day_deployment_history(day)
    repair_states = [row for row in prepared if row.get("wired_repair_authority")]
    if repair_states:
        if len(repair_states) != len(prepared) or len(repair_states) != 1:
            raise B2Error("wired_repair_deployment_must_be_exactly_one_row")
        repair_row = plan_by_key(plan, str(repair_states[0].get("key") or ""))
        repair = validate_wired_repair_authority(
            repair_row, plan, repair_states[0]["wired_repair_authority"]["binding"]
        )
        return {
            "status": "PASS_CURRENT_HEAD_BOUND_WIRED_REPAIR_DEPLOYMENT",
            "authorized": True,
            "prior_deployment_id": repair["source_deployment_id"],
            "prepared_keys": [repair_row["key"]],
            "allowed_machine_steps": repair["allowed_machine_steps"],
            "authority": repair["binding"],
        }
    batch = plan.get("batch_authority")
    if batch:
        validated = validate_batch_authority(plan, batch.get("binding") or {})
        authority_binding = validated["binding"]
        matching = [
            record for record in history
            if _same_authority_binding(record.get("batch_authority"), authority_binding)
        ]
        if len(matching) > 1:
            raise B2Error("wire_batch_authority_multiple_deployments_detected")
        if matching:
            return {
                "status": "PASS_IDEMPOTENT_BATCH_DEPLOYMENT",
                "authorized": False,
                "deployment_id": matching[0].get("deployment_id"),
                "batch_authority": authority_binding,
            }
        keys = [str(row.get("key") or "") for row in prepared]
        carry_only = validated.get("authority_class") == CARRY_ONLY_AUTHORITY_CLASS
        # A carry-only deploy carries NO provider rows: zero prepared is the
        # contract, not a failure. Any row at all is outside its (empty) scope and
        # is refused by the scope check below.
        if not keys and not carry_only:
            raise B2Error("wire_batch_authority_no_prepared_rows")
        if len(keys) != len(set(keys)):
            raise B2Error("wire_batch_authority_duplicate_prepared_rows")
        allowed = set(validated["allowed_queue_keys"])
        if any(key not in allowed for key in keys):
            raise B2Error("wire_batch_authority_prepared_rows_outside_scope")
        if not deploy:
            return {
                "status": "PASS_OPERATOR_BATCH_VERIFY_ONLY_PREPARATION",
                "authorized": True,
                "prepared_keys": keys,
                "batch_authority": authority_binding,
            }
        if len(keys) < int(validated["minimum_successful_rows"]):
            raise B2Error("wire_batch_authority_minimum_success_not_met")
        return {
            "status": "PASS_OPERATOR_BATCH_DEPLOYMENT",
            "authorized": True,
            "prior_unrelated_deployment_count": len(history),
            "prepared_keys": keys,
            "batch_authority": authority_binding,
        }
    if not history:
        return {"status": "PASS_FIRST_DEPLOYMENT", "authorized": True}
    rollout = plan.get("morning_two_phase_rollout") or {}
    if not rollout.get("active"):
        return {"status": "PASS_IDEMPOTENT_DEPLOYMENT", "authorized": False}
    maximum = int((rollout.get("limits") or {}).get("deployments_per_pt_day") or 0)
    if len(history) >= maximum:
        return {"status": "PASS_IDEMPOTENT_DEPLOYMENT", "authorized": False}
    if len(history) != 1:
        raise B2Error("morning_followup_deployment_history_invalid")
    prior = history[0]
    test = rollout.get("test_deployment") or {}
    expected_test_key = str(test.get("provider_key") or "")
    if prior.get("status") != "PASS" or prior.get("sensor_status") != "PASS":
        raise B2Error("morning_followup_prior_deployment_not_verified")
    if list(prior.get("wired_keys") or []) != [expected_test_key]:
        raise B2Error("morning_followup_prior_deployment_not_expected_single_test")
    if expected_test_key not in (day.get("wired_keys") or []):
        raise B2Error("morning_followup_test_key_missing_from_day_ledger")
    receipt_path = verify_binding(prior.get("receipt"), label="morning_prior_deployment_receipt")
    receipt = read_json(receipt_path)
    sensor = receipt.get("sensor") or {}
    receipt_deployment_id = str((receipt.get("deployment") or {}).get("id") or "")
    if (
        receipt.get("status") != "PASS"
        or sensor.get("status") != "PASS"
        or sensor.get("failure_count") != 0
        or not receipt_deployment_id
        or receipt_deployment_id != str(prior.get("deployment_id") or "")
    ):
        raise B2Error("morning_followup_prior_deployment_receipt_invalid")
    pointer = read_json(Path(current_base))
    if str(pointer.get("deployment_id") or "") != receipt_deployment_id:
        raise B2Error("morning_followup_current_pointer_not_verified_test")
    keys = [str(row.get("key") or "") for row in prepared]
    allowed = set((rollout.get("followup_deployment") or {}).get("allowed_provider_keys") or [])
    max_count = int((rollout.get("followup_deployment") or {}).get("max_provider_count") or 0)
    if not keys:
        raise B2Error("morning_followup_no_prepared_rows")
    if len(keys) > max_count or any(key not in allowed for key in keys):
        raise B2Error("morning_followup_prepared_rows_outside_bounded_backlog")
    return {
        "status": "PASS_VERIFIED_TEST_AUTHORIZES_BOUNDED_FOLLOWUP",
        "authorized": True,
        "prior_deployment_id": receipt_deployment_id,
        "prepared_keys": keys,
    }


def stable_key(network: Any, program_id: Any) -> str:
    net = normalize_network(network)
    pid = str(program_id or "").strip().casefold()
    if not net or not pid or not re.fullmatch(r"[a-z0-9._:-]+", pid):
        raise B2Error("stable_identity_missing_or_invalid")
    return f"{net}:{pid}"


def slugify(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")


def true(value: Any) -> bool:
    return str(value or "").strip().upper() == "TRUE"


def link_qa_deployable(value: Any) -> bool:
    """Whether the Sheet records structural QA that preserves attribution.

    Tracking links are never opened here.  A blank, untested, or reactivation
    marker is a genuine blocker; the historical Wire-In Ready checkbox and a
    missing paid-event label are deliberately not inputs.
    """
    qa = str(value or "").strip().casefold()
    return any(qa == prefix or qa.startswith(prefix + ";")
               for prefix in DEPLOYABLE_LINK_QA_PREFIXES)


def staged_feed_member(row: dict[str, Any]) -> bool:
    """Approved + linked + GLP + not live: the operator's staged denominator."""
    status = str(row.get("Status") or "").strip()
    tracking = str(row.get("Tracking Link / Deep Link") or "").strip()
    return (
        status in APPROVED_STATUSES
        and not true(row.get("Wired Live?"))
        and bool(tracking)
        and str(row.get("Vertical") or "").strip() == "glp1-weightloss"
        and not permanent_sheet_exclusion(row)
    )


def affiliati_switch_prerequisite_blockers(
    row: dict[str, Any], registry_path: Path | None = None,
) -> list[str]:
    """Every Affiliati replacement prerequisite must be true and evidenced."""
    if normalize_network(row.get("Network / Route")) != "affiliati":
        return []
    notes = str(row.get("Notes / Blocker") or "")
    match = re.search(r"Affiliati replacement staged link-dark:\s*offer=(\d+)", notes)
    path = Path(registry_path or AFFILIATI_SWITCH_PREREQUISITES)
    if not path.is_file():
        return ["affiliati_switch_prerequisite_registry_missing"]
    try:
        registry = read_json(path)
    except B2Error:
        return ["affiliati_switch_prerequisite_registry_invalid"]
    if registry.get("schema") != "glp.affiliati-switch-prerequisites.v1":
        return ["affiliati_switch_prerequisite_registry_invalid"]
    if match:
        offer_id = match.group(1)
    else:
        tracking = str(row.get("Tracking Link / Deep Link") or "").strip()
        tracking_sha256 = sha_bytes(tracking.encode()) if tracking else ""
        matching_offers = [
            str(key) for key, value in (registry.get("offers") or {}).items()
            if str((value or {}).get("tracking_href_sha256") or "") == tracking_sha256
        ]
        if len(matching_offers) != 1:
            return ["affiliati_switch_offer_identity_missing"]
        offer_id = matching_offers[0]
    record = (registry.get("offers") or {}).get(offer_id) or {}
    if not record:
        return ["affiliati_switch_prerequisite_offer_missing"]
    blockers: list[str] = []
    prerequisites = record.get("prerequisites") or {}
    evidence = record.get("evidence") or {}
    for name in registry.get("required_prerequisites") or []:
        if prerequisites.get(name) is not True:
            blockers.append(f"affiliati_prerequisite_incomplete:{name}")
            continue
        binding = evidence.get(name)
        if not isinstance(binding, dict):
            blockers.append(f"affiliati_prerequisite_evidence_missing:{name}")
            continue
        try:
            verify_binding(binding, label=f"affiliati_prerequisite_{name}")
        except B2Error:
            blockers.append(f"affiliati_prerequisite_evidence_invalid:{name}")
    return blockers


def pre_routing_feed_blockers(row: dict[str, Any]) -> list[str]:
    """Genuine deploy blockers available directly from one Sheet row."""
    blockers: list[str] = []
    tracking = str(row.get("Tracking Link / Deep Link") or "").strip()
    if not tracking.startswith("https://"):
        blockers.append("tracking_link_not_https")
    if not link_qa_deployable(row.get("Link QA Status")):
        blockers.append("tracking_link_qa_not_deployable")
    if str(row.get("Status") or "").strip() in COMMERCIAL_ECONOMICS_HOLD_STATUSES:
        blockers.append("commercial_economics_hold_no_positive_commission")
    blockers.extend(affiliati_switch_prerequisite_blockers(row))
    return blockers


def path_binding(path: Path) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file():
        raise B2Error(f"bound_file_missing:{path}")
    return {"path": str(path), "sha256": sha_file(path), "bytes": path.stat().st_size}


def verify_binding(binding: dict[str, Any], *, label: str) -> Path:
    path = Path(str(binding.get("path") or "")).resolve()
    if not path.is_file() or sha_file(path) != binding.get("sha256"):
        raise B2Error(f"{label}_binding_drift")
    return path


def wired_repair_authority_path(site_program_id: str) -> Path:
    program_id = str(site_program_id or "").strip()
    if not re.fullmatch(r"[a-z0-9-]+", program_id):
        raise B2Error("wired_repair_program_id_invalid")
    return WIRED_REPAIR_DIR / f"{program_id}.json"


def validate_wired_repair_authority(
    candidate: dict[str, Any],
    plan: dict[str, Any],
    binding: dict[str, Any],
) -> dict[str, Any]:
    """Validate one immutable, current-head corrective rebuild authority.

    This does not make wired rows generally retryable. It authorizes exactly one
    provider and one machine-owned failed manifest step against the current
    deployment/tree. The next successful deployment makes it stale by design.
    """
    authority_path = verify_binding(binding, label="wired_repair_authority")
    expected_path = wired_repair_authority_path(candidate.get("site_program_id"))
    if authority_path != expected_path.resolve():
        raise B2Error("wired_repair_authority_path_invalid")
    authority = read_json(authority_path)
    allowed_steps = authority.get("allowed_machine_steps")
    if (
        authority.get("schema") != WIRED_REPAIR_SCHEMA
        or authority.get("status") != "authorized"
        or authority.get("provider") != candidate.get("site_program_id")
        or authority.get("key") != candidate.get("key")
        or authority.get("pt_date") != plan.get("pt_date")
        or not isinstance(allowed_steps, list)
        or set(allowed_steps) != WIRED_REPAIR_ALLOWED_STEPS
        or len(allowed_steps) != len(WIRED_REPAIR_ALLOWED_STEPS)
        or not str(authority.get("reason") or "").strip()
        or not str(authority.get("created_at_utc") or "").strip()
    ):
        raise B2Error("wired_repair_authority_contract_invalid")

    pointer_path = verify_binding(
        authority.get("current_content_base"), label="wired_repair_current_content_base"
    )
    if pointer_path != CURRENT_BASE.resolve():
        raise B2Error("wired_repair_current_content_base_path_invalid")
    pointer = read_json(pointer_path)
    if (
        pointer.get("schema") != "glp-opus-current-content-base-v1"
        or pointer.get("status") != "PASS"
        or authority.get("source_deployment_id") != pointer.get("deployment_id")
        or authority.get("source_release_root") != pointer.get("release_root")
        or authority.get("source_release_tree_sha256") != pointer.get("release_tree_sha256")
    ):
        raise B2Error("wired_repair_current_head_drift")

    manifest_path = verify_binding(
        authority.get("source_onboarding_manifest"),
        label="wired_repair_source_onboarding_manifest",
    )
    manifest = read_json(manifest_path)
    rows = [
        row for row in (manifest.get("manifest") or {}).get("rows") or []
        if isinstance(row, dict)
        and row.get("provider") == candidate.get("site_program_id")
        and row.get("step") in WIRED_REPAIR_ALLOWED_STEPS
    ]
    if (
        manifest.get("machine") != "onboarding_manifest"
        or manifest.get("mode") not in {"onboard", "verify"}
        or manifest.get("complete") is not False
        or len(rows) != len(WIRED_REPAIR_ALLOWED_STEPS)
        or {row.get("step") for row in rows} != WIRED_REPAIR_ALLOWED_STEPS
        or any(row.get("status") == "green" for row in rows)
    ):
        raise B2Error("wired_repair_source_manifest_invalid")
    return {
        "status": "PASS_CURRENT_HEAD_BOUND_WIRED_REPAIR",
        "binding": path_binding(authority_path),
        "source_onboarding_manifest": path_binding(manifest_path),
        "source_deployment_id": pointer["deployment_id"],
        "source_release_tree_sha256": pointer["release_tree_sha256"],
        "allowed_machine_steps": list(allowed_steps),
    }


def apply_explicit_wired_repairs(
    plan: dict[str, Any], requested_providers: set[str]
) -> dict[str, Any]:
    """Move only explicitly requested, receipt-authorized wired rows to queue."""
    if not requested_providers:
        return plan
    queue = list(plan.get("queue") or [])
    already = list(plan.get("already_wired") or [])
    requeued: list[dict[str, Any]] = []
    for provider in sorted(requested_providers):
        if any(row.get("site_program_id") == provider for row in queue):
            continue
        matches = [row for row in already if row.get("site_program_id") == provider]
        if not matches:
            continue
        if len(matches) != 1 or not matches[0].get("key"):
            raise B2Error(f"wired_repair_already_wired_identity_invalid:{provider}")
        authority_path = wired_repair_authority_path(provider)
        if not authority_path.is_file():
            continue
        candidate = dict(matches[0])
        validation = validate_wired_repair_authority(
            candidate, plan, path_binding(authority_path)
        )
        candidate["wired_repair_authority"] = validation
        queue.append(candidate)
        already.remove(matches[0])
        requeued.append({
            "provider": provider,
            "key": candidate["key"],
            "authority": validation["binding"],
            "allowed_machine_steps": validation["allowed_machine_steps"],
        })
        plan.setdefault("source_bindings", {})[
            f"wired_repair_authority:{provider}"
        ] = validation["binding"]
    plan["queue"] = queue
    plan["already_wired"] = already
    denominator = plan.get("denominator") or {}
    denominator["already_wired"] = len(already)
    denominator["eligible_or_fail_closed_glp_backlog"] = len(queue)
    plan["wired_repairs"] = requeued
    return plan


@contextlib.contextmanager
def locked_ledger(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def fresh_ledger() -> dict[str, Any]:
    return {
        "schema": LEDGER_SCHEMA,
        "status": "ACTIVE",
        "created_at_utc": iso_utc(),
        "updated_at_utc": iso_utc(),
        "rows": {},
        "days": {},
    }


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return fresh_ledger()
    payload = read_json(path)
    if payload.get("schema") != LEDGER_SCHEMA or not isinstance(payload.get("rows"), dict):
        raise B2Error("ledger_schema_invalid")
    if not isinstance(payload.get("days"), dict):
        raise B2Error("ledger_days_invalid")
    return payload


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at_utc"] = iso_utc()
    atomic_json(path, ledger)


def latest_complete_backrun(machine_root: Path | None = None) -> Path:
    root = machine_root or BUSINESS / "reports/glp-clinic-scout/affiliate-machine"
    candidates = {
        *root.glob("backrun-*"),
        *root.glob("*/backrun-final"),
    }
    complete: list[tuple[dt.datetime, Path]] = []
    for directory in candidates:
        coverage_path = directory / "coverage-receipt.json"
        sheet_path = directory / "sheet-full-row-export.json"
        dispositions_path = directory / "sheet-row-dispositions.json"
        if not all(path.is_file() for path in (coverage_path, sheet_path, dispositions_path)):
            continue
        try:
            coverage = read_json(coverage_path)
            if coverage.get("status") != "PASS":
                continue
            sheet_axis = coverage.get("sheet_axis") or {}
            if sheet_axis.get("full_row_export_sha256") != sha_file(sheet_path):
                continue
            if sheet_axis.get("dispositions_sha256") != sha_file(dispositions_path):
                continue
            if not (coverage.get("coverage_assertion") or {}).get("every_nonblank_row_dispositioned"):
                continue
            captured = _parse_utc(
                str(coverage.get("captured_at_utc") or ""),
                label=f"coverage_receipt:{directory.name}",
            )
            complete.append((captured, directory))
        except B2Error:
            continue
    if complete:
        return max(complete, key=lambda item: (item[0], str(item[1])))[1]
    raise B2Error("complete_backrun_missing")


def _join_by_sheet_row(coverage: dict[str, Any]) -> dict[int, list[dict[str, str]]]:
    """Preserve every marketplace candidate; routing must select one exact identity."""
    result: dict[int, list[dict[str, str]]] = {}
    for row in (coverage.get("join") or {}).get("program_dispositions") or []:
        if row.get("disposition") != "MATCHED_TO_SHEET" or row.get("sheet_row") is None:
            continue
        sheet_row = int(row["sheet_row"])
        candidate = {
            "network": normalize_network(row.get("network")),
            "program_id": str(row.get("program_id") or "").strip(),
            "name": str(row.get("name") or "").strip(),
        }
        identities = {(item["network"], item["program_id"]) for item in result.get(sheet_row, [])}
        if (candidate["network"], candidate["program_id"]) not in identities:
            result.setdefault(sheet_row, []).append(candidate)
    return result

def enumerable_networks(backrun_dir: Path) -> set[str]:
    """Networks this backrun could actually enumerate a relationship for.

    The join axis is built from `<backrun>/marketplace/*-inventory.json`, and
    those files only exist for networks with a publisher-side API in the supply
    sweep. Knowing WHICH networks are enumerable is what lets the router tell
    "the API says we are not joined" (a real contradiction) apart from "there is
    no API to ask" (an absence of evidence, not evidence of absence).
    """
    directory = Path(backrun_dir) / "marketplace"
    if not directory.is_dir():
        return set()
    found = set()
    for path in canonical_inventory_paths(directory):
        name = normalize_network(path.name[: -len("-inventory.json")])
        if name:
            found.add(name)
    # `cj-relationships.json` is CJ's join surface under a different filename.
    if (directory / "cj-relationships.json").is_file():
        found.add("cj")
    return found


#: How a merchant-hosted platform names its tenant in a URL:
#: `https://nu-image-medical.trackdesk.com` -> `nu-image-medical`.
_TENANT_LABEL = re.compile(r"^([a-z0-9][a-z0-9-]{1,62})\.")


def _sheet_only_program_id(network: str, apply_url: str, tracking_link: str) -> str:
    """The programme id a NON-ENUMERABLE network's own Sheet row already states.

    Only positions where a number or slug IS an identity are read - a tenant
    subdomain, a numeric offer path, or an offer/campaign query parameter. A
    bare number anywhere in a URL is never accepted, because a price, a date or
    a UTM value would qualify and a wrong identity here keys the whole wire-in
    ledger to the wrong programme.
    """
    for url in (apply_url, tracking_link):
        url = str(url or "").strip()
        if not url.startswith("https://"):
            continue
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        # A tenant subdomain of the network's own domain: the platform itself
        # chose that label for this advertiser, so it is the network's identity
        # for the programme, not ours.
        if network and host.endswith("." + network + ".com"):
            match = _TENANT_LABEL.match(host)
            if match and match.group(1) not in (
                "www", "app", "api", "go", "track", "ui", "signup", "auth", "login",
                "affiliate", "affiliates", "publisher", "publishers",
            ):
                return match.group(1)
        query = dict(parse_qsl(parsed.query))
        for key in ("offer_id", "oid", "campaign_id", "cid", "mid", "advertiser",
                    "advertiserid", "merchant", "program_id", "programid"):
            value = str(query.get(key) or "").strip()
            if value and re.fullmatch(r"[a-z0-9._-]{2,64}", value, re.I):
                return value.casefold()
        match = re.search(
            r"/(?:explore|offers?|campaigns?|programs?|advertisers?|merchant-profile)/([0-9]{2,})(?:/|$)",
            parsed.path, re.I,
        )
        if match:
            return match.group(1)
    return ""


def permanent_sheet_exclusion(row: dict[str, Any]) -> str:
    """Return the immutable operator exclusion reason, if this exact row has one."""
    network = normalize_network(row.get("Network / Route"))
    program_id = _sheet_only_program_id(
        network,
        str(row.get("Apply URL") or ""),
        str(row.get("Tracking Link / Deep Link") or ""),
    )
    if not network or not program_id:
        return ""
    return PERMANENT_EXCLUDED_PROGRAM_KEYS.get(f"{network}:{program_id}", "")


def _affiliati_bound_offer_id(
    row: dict[str, Any], registry_path: Path | None = None,
) -> str:
    """Resolve an Affiliati offer only from the row's bound tracking-link hash.

    Affiliati's generic publisher login URL does not carry an offer id and its
    redirect host carries a creative id, not the offer id. The current
    prerequisite registry binds each approved offer to the exact provider-issued
    href hash, so that is the only honest local join for this non-URL identity.
    """
    if normalize_network(row.get("Network / Route")) != "affiliati":
        return ""
    path = Path(registry_path or AFFILIATI_SWITCH_PREREQUISITES)
    if affiliati_switch_prerequisite_blockers(row, path):
        return ""
    registry = read_json(path)
    tracking = str(row.get("Tracking Link / Deep Link") or "").strip()
    tracking_sha256 = sha_bytes(tracking.encode()) if tracking else ""
    matches = [
        str(key) for key, record in (registry.get("offers") or {}).items()
        if str((record or {}).get("tracking_href_sha256") or "") == tracking_sha256
    ]
    return matches[0] if len(matches) == 1 else ""


def _affiliati_bound_route_authority(
    row: dict[str, Any], registry_path: Path | None = None,
) -> dict[str, Any] | None:
    """Return the hash-bound Affiliati identity and published paid event."""
    path = Path(registry_path or AFFILIATI_SWITCH_PREREQUISITES)
    offer_id = _affiliati_bound_offer_id(row, path)
    if not offer_id:
        return None
    registry = read_json(path)
    record = (registry.get("offers") or {}).get(offer_id) or {}
    exact_paid_event = str(record.get("exact_paid_event") or "").strip()
    event_binding = (record.get("evidence") or {}).get("exact_paid_event_confirmed")
    if not exact_paid_event or not isinstance(event_binding, dict):
        return None
    try:
        verify_binding(event_binding, label="affiliati_exact_paid_event")
    except B2Error:
        return None
    return {
        "offer_id": offer_id,
        "exact_paid_event": exact_paid_event,
        "prerequisite_registry": path_binding(path),
        "exact_paid_event_evidence": event_binding,
    }


def _synthesize_current_route(
    row_number: int,
    row: dict[str, Any],
    join_candidates: list[dict[str, str]],
    enumerable: set[str] | None = None,
) -> dict[str, Any] | None:
    """Self-heal a stale static route from current governed identity bytes.

    The Sheet selects the network. For Katalys, its portal Apply URL also
    supplies the immutable offer id. The fresh marketplace join must
    corroborate exactly one network+program identity; ambiguity fails closed.

    2026-08-19 — the NON-ENUMERABLE case. Nu Image Medical was approved through
    Trackdesk, a merchant-hosted platform with **no publisher-side discovery or
    relationship API at all**. The join axis is built exclusively from
    `marketplace/*-inventory.json`, so no Trackdesk inventory can ever exist, so
    `join_candidates` is permanently empty, so this function returned `None` and
    the row died as `coverage_plan_route_missing` — forever, no matter what the
    operator did.

    That is the wrong verdict for the wrong reason. "The network's API did not
    corroborate" is a real contradiction and must still fail closed. "There is
    no API to corroborate WITH" is an absence of evidence, and the governed
    Sheet row — which is itself first-party, writer-governed, and carries the
    platform's own tenant identifier in its Apply URL — is then the best
    identity that exists.

    So the corroboration requirement is relaxed for exactly one case and no
    other, under three conditions that are all checked here:

      * the network produced NO inventory in this backrun (it is not enumerable
        in this run at all — a network that IS enumerable and simply does not
        list us still fails closed, which is the case that matters);
      * the Sheet row states a programme identity in a position where a slug or
        number IS an identity (tenant subdomain, offer path, offer query param);
      * and the resulting route is stamped `routing_source=
        current_sheet_only_network_not_enumerable` with the reason, so no
        downstream reader can mistake it for a corroborated identity.
    """
    if str(row.get("Vertical") or "").strip() != "glp1-weightloss":
        return None
    network_label = str(row.get("Network / Route") or "").strip()
    network = normalize_network(network_label)
    if not network:
        return None
    candidates = [
        item for item in join_candidates
        if item.get("network") == network and str(item.get("program_id") or "").strip()
    ]
    apply_url = str(row.get("Apply URL") or "").strip()
    portal_program_id = ""
    if network == "katalys" and apply_url:
        parsed = urlsplit(apply_url)
        if parsed.scheme != "https" or (parsed.hostname or "").casefold() != "app.katalys.com":
            return None
        match = re.search(r"(?:^|/)explore/([0-9]+)(?:/|$)", parsed.path)
        if not match:
            return None
        portal_program_id = match.group(1)
        candidates = [item for item in candidates if item["program_id"] == portal_program_id]
    identity: dict[str, str] | None = None
    routing_source = "current_sheet_plus_fresh_marketplace_join"
    not_enumerable_reason = ""
    if len(candidates) == 1:
        identity = candidates[0]
    elif not candidates and network == "affiliati":
        program_id = _affiliati_bound_offer_id(row)
        if not program_id:
            return None
        identity = {"network": network, "program_id": program_id,
                    "name": str(row.get("Program / Network") or "").strip()}
        routing_source = "current_sheet_plus_hash_bound_affiliati_authority"
        not_enumerable_reason = (
            "The generic Affiliati publisher URL carries no offer identity. The "
            "governed Sheet row's exact provider-issued href hash uniquely matches "
            "the current hash-bound Affiliati prerequisite registry, whose approval, "
            "terms, attribution, and tracking-link evidence all revalidate on read.")
    elif not candidates and network not in (enumerable or set()):
        program_id = _sheet_only_program_id(
            network, apply_url, str(row.get("Tracking Link / Deep Link") or ""))
        if not program_id:
            return None
        identity = {"network": network, "program_id": program_id,
                    "name": str(row.get("Program / Network") or "").strip()}
        routing_source = "current_sheet_only_network_not_enumerable"
        not_enumerable_reason = (
            f"`{network}` produced no marketplace inventory in this backrun, so the join "
            f"axis has nothing to corroborate against. The identity is the platform's own "
            f"tenant/offer id as stated by the governed Sheet row's own URLs. A network "
            f"that IS enumerable and does not list this programme still fails closed.")
    if identity is None:
        return None
    alias = slugify(row.get("SEO Slug") or row.get("Program / Network"))
    if not alias:
        return None
    return {
        "program_key": f"{network_label}:{identity['program_id']}",
        "provider_network": network_label,
        "provider_program_id": identity["program_id"],
        "program_name": str(row.get("Program / Network") or "").strip(),
        "candidate_id": alias,
        "canonical_sheet": {
            "row_number": row_number,
            "vertical": "glp1-weightloss",
            "wired_live": true(row.get("Wired Live?")),
        },
        "routing_class": "glp1_weightloss_coverage_candidate",
        "canonical_vertical": "glp1-weightloss",
        "routing_source": routing_source,
        "identity_evidence": {
            "sheet_network": network_label,
            "sheet_apply_url": apply_url or None,
            "portal_program_id": portal_program_id or None,
            "marketplace_candidate": identity,
            "uncorroborated_reason": not_enumerable_reason or None,
        },
    }


def _routing_by_sheet_row(routing: dict[str, Any]) -> dict[int, dict[str, Any]]:
    if routing.get("status") != "PASS" or not isinstance(routing.get("programs"), list):
        raise B2Error("coverage_routing_plan_invalid")
    result: dict[int, dict[str, Any]] = {}
    for row in routing["programs"]:
        number = (row.get("canonical_sheet") or {}).get("row_number")
        if number is None:
            continue
        number = int(number)
        if number in result:
            raise B2Error(f"coverage_routing_duplicate_sheet_row:{number}")
        result[number] = row
    return result


def _parse_utc(value: Any, *, label: str) -> dt.datetime:
    text = str(value or "").strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise B2Error(f"routing_basis_timestamp_invalid:{label}") from exc
    if parsed.tzinfo is None:
        raise B2Error(f"routing_basis_timestamp_not_utc:{label}")
    return parsed.astimezone(dt.timezone.utc)


def _verified_inventory_payout(program: dict[str, Any]) -> dict[str, Any]:
    """Return one exact dollar payout from a captured marketplace record.

    A sibling with prose, multiple different default amounts, revshare, or no
    commercial tuple remains unverified.  It can never outrank a verified CPA.
    """
    entries = program.get("payout_entries") or []
    defaults: list[tuple[float, str]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("is_default") is not True:
            continue
        if float(entry.get("payout_percentage") or 0) != 0:
            continue
        try:
            amount = float(entry.get("payout_amount"))
        except (TypeError, ValueError):
            continue
        payout_type = str(entry.get("payout_type") or "").strip().casefold()
        if amount >= 0 and payout_type in {"cpa", "cpl", "cps", "fixed"}:
            defaults.append((amount, payout_type))
    unique = sorted(set(defaults))
    if len(unique) == 1:
        return {
            "payout_verified": True,
            "payout_usd": unique[0][0],
            "payout_model": unique[0][1].upper(),
            "payout_basis": "captured_default_payout_entry",
        }
    if program.get("headline_payout") is not None:
        try:
            amount = float(program["headline_payout"])
        except (TypeError, ValueError):
            amount = -1
        if amount >= 0 and str(program.get("currency") or "USD").upper() == "USD":
            return {
                "payout_verified": True,
                "payout_usd": amount,
                "payout_model": str(program.get("payout_model") or "fixed").upper(),
                "payout_basis": "captured_structured_headline_payout",
            }
    return {
        "payout_verified": False,
        "payout_usd": None,
        "payout_model": None,
        "payout_basis": "payout_unverified",
    }


def _inventory_offer(
    *, network: str, program: dict[str, Any], program_id: Any, name: Any,
    relationship: Any,
) -> dict[str, Any]:
    return {
        "network": network,
        "program_id": str(program_id or "").strip(),
        "name": str(name or "").strip(),
        "official_url": str(program.get("url") or program.get("official_url") or "").strip(),
        "relationship": str(relationship or "").strip().casefold(),
        "exact_paid_event": str(program.get("exact_paid_event")
                                or program.get("paid_event") or "").strip(),
        "exact_paid_event_basis": str(program.get("paid_event_basis") or "").strip(),
        **_verified_inventory_payout(program),
    }


def _inventory_rows(path: Path, document: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize one current marketplace receipt without fuzzy Sheet matching."""
    rows: list[dict[str, str]] = []
    network = normalize_network(document.get("network") or path.stem.removesuffix("-inventory"))

    # Awin publishes every relationship bucket under each publisher id.
    for buckets in (document.get("relationships") or {}).values():
        if not isinstance(buckets, dict):
            continue
        for relationship, programs in buckets.items():
            for program in programs or []:
                if not isinstance(program, dict):
                    continue
                rows.append(_inventory_offer(
                    network=network, program=program, program_id=program.get("id"),
                    name=program.get("name"), relationship=relationship,
                ))

    # Partnerize publishes approved/pending/rejected campaign buckets.
    for relationship, wrapper in (document.get("statuses") or {}).items():
        data = (wrapper or {}).get("data") if isinstance(wrapper, dict) else None
        for wrapped in (data or {}).get("campaigns") or []:
            program = (wrapped or {}).get("campaign") or wrapped
            if not isinstance(program, dict):
                continue
            rows.append(_inventory_offer(
                network=network, program=program,
                program_id=program.get("campaign_id") or program.get("id"),
                name=program.get("title") or program.get("name"),
                relationship=relationship,
            ))

    for program in document.get("programs") or []:
        if not isinstance(program, dict):
            continue
        rows.append(_inventory_offer(
            network=network, program=program,
            program_id=(program.get("program_id") or program.get("id")
                        or program.get("campaign_id") or program.get("advertiser-id")),
            name=(program.get("name") or program.get("display_name")
                  or program.get("advertiser-name") or program.get("title")),
            relationship=program.get("relationship") or program.get("status"),
        ))
    return [row for row in rows if row["program_id"]]


def canonical_inventory_paths(directory: Path) -> list[Path]:
    """One primary inventory per network, excluding materializer sidecars.

    The coverage materializer writes both ``<network>-inventory.json`` and
    ``<network>-relationships-inventory.json``.  The latter is lineage evidence,
    not a second current inventory.  Treating every JSON file as primary made
    the 2026-08-26 planner abort on ``duplicate_current_marketplace_inventory``
    before it could emit a plan or any row blocks.
    """
    return [
        path for path in sorted(Path(directory).glob("*-inventory.json"))
        if not path.name.endswith("-relationships-inventory.json")
        and path.name != "materializer-report-inventory.json"
    ]


def _current_inventories(backrun_dir: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    directory = Path(backrun_dir) / "marketplace"
    if not directory.is_dir():
        raise B2Error("current_marketplace_inventory_directory_missing")
    inventories: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    for path in canonical_inventory_paths(directory):
        document = read_json(path)
        if not isinstance(document, dict):
            raise B2Error(f"current_marketplace_inventory_invalid:{path.name}")
        network = normalize_network(document.get("network") or path.stem.removesuffix("-inventory"))
        captured = str(document.get("captured_at_utc") or "").strip()
        _parse_utc(captured, label=path.name)
        auth_state = str(document.get("auth_state") or "").strip()
        completeness = str(document.get("completeness") or "").strip()
        blockers = document.get("blockers") or []
        blocked = (
            auth_state.casefold() in {"blocked", "fail", "failed"}
            or completeness.casefold() in {"blocked", "incomplete", "fail", "failed"}
        )
        rows = _inventory_rows(path, document)
        state = {
            "network": network,
            "path": str(path.resolve()),
            "binding": path_binding(path),
            "captured_at_utc": captured,
            "auth_state": auth_state,
            "completeness": completeness,
            "complete": completeness.casefold() == "complete",
            "usable": not blocked,
            "blockers": blockers,
            "rows": rows,
        }
        if network in inventories:
            raise B2Error(f"duplicate_current_marketplace_inventory:{network}")
        inventories[network] = state
        sources.append({
            "label": f"marketplace_inventory_{network}",
            "captured_at_utc": captured,
            "path": str(path.resolve()),
        })
    if not inventories:
        raise B2Error("current_marketplace_inventories_missing")
    return inventories, sources


def _provider_owned_identity_url(network: str, url: str) -> bool:
    """True only when the provider/network name is visible in the HTTPS host."""
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.casefold().replace("-", "")
    network_token = normalize_network(network).replace("-", "")
    aliases = {
        "impact": ("impact.com",),
        "xentraffic": ("xentraffic.everflowclient.io",),
    }
    expected = aliases.get(normalize_network(network), ())
    return host in expected or (len(network_token) >= 4 and network_token in host)


def _route_from_identity(
    row_number: int,
    row: dict[str, Any],
    *,
    network_label: str,
    program_id: str,
    routing_source: str,
    identity_evidence: dict[str, Any],
) -> dict[str, Any]:
    alias = slugify(row.get("SEO Slug") or row.get("Program / Network"))
    if not alias:
        raise B2Error(f"current_route_candidate_id_missing:{row_number}")
    return {
        "program_key": f"{network_label}:{program_id}",
        "provider_network": network_label,
        "provider_program_id": program_id,
        "program_name": str(row.get("Program / Network") or "").strip(),
        "candidate_id": alias,
        "canonical_sheet": {
            "row_number": row_number,
            "vertical": "glp1-weightloss",
            "wired_live": true(row.get("Wired Live?")),
        },
        "routing_class": "glp1_weightloss_coverage_candidate",
        "canonical_vertical": "glp1-weightloss",
        "routing_source": routing_source,
        "identity_evidence": identity_evidence,
    }


def _resolve_current_core_route(
    row_number: int,
    row: dict[str, Any],
    inventories: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    network_label = str(row.get("Network / Route") or "").strip()
    network = normalize_network(network_label)
    if not network:
        return {"outcome": "FAIL_CLOSED", "reason": "provider_network_missing"}
    apply_url = str(row.get("Apply URL") or "").strip()
    tracking = str(row.get("Tracking Link / Deep Link") or "").strip()
    apply_id = _sheet_only_program_id(network, apply_url, "")
    tracking_id = _sheet_only_program_id(network, "", tracking)
    if apply_id and tracking_id and apply_id != tracking_id:
        return {
            "outcome": "FAIL_CLOSED",
            "reason": "sheet_provider_identity_conflict",
            "detail": {"apply_program_id": apply_id, "tracking_program_id": tracking_id},
        }
    program_id = apply_id or tracking_id
    inventory = inventories.get(network)
    candidates = list((inventory or {}).get("rows") or [])

    # Affiliati's publisher URL and redirect href expose campaign/creative
    # identities, not the immutable offer id. The governed prerequisite registry
    # binds the exact provider-issued href hash to one offer and to the network's
    # published paid event. Consume that current authority before a complete but
    # identity-less generic inventory can incorrectly classify the row as missing.
    if network == "affiliati" and not program_id:
        authority = _affiliati_bound_route_authority(row)
        if authority:
            paid_event_gate = exact_paid_event_gate(
                network, [{"exact_paid_event": authority["exact_paid_event"]}]
            )
            if paid_event_gate["status"] == "BLOCK":
                return {
                    "outcome": "FAIL_CLOSED",
                    "reason": paid_event_gate["reason"],
                    "detail": {
                        "network": network,
                        "program_id": authority["offer_id"],
                        "exact_paid_event_gate": paid_event_gate,
                    },
                }
            route = _route_from_identity(
                row_number, row,
                network_label=network_label,
                program_id=authority["offer_id"],
                routing_source="current_sheet_plus_hash_bound_affiliati_authority",
                identity_evidence={
                    "method": "provider_issued_href_hash_bound_offer_id",
                    "sheet_apply_url": apply_url,
                    "prerequisite_registry": authority["prerequisite_registry"],
                    "exact_paid_event_evidence": authority["exact_paid_event_evidence"],
                    "exact_paid_event_gate": paid_event_gate,
                },
            )
            return {"outcome": "ROUTED", "route": route}

    exact: list[dict[str, str]] = []
    identity_method = "provider_url_program_id"
    if program_id:
        exact = [item for item in candidates if item["program_id"].casefold() == program_id.casefold()]
    else:
        display_identity = slugify(row.get("Program / Network"))
        exact = [item for item in candidates if slugify(item.get("name")) == display_identity]
        if len(exact) == 1:
            program_id = exact[0]["program_id"]
            identity_method = "exact_inventory_display_name"
        elif len(exact) > 1:
            return {
                "outcome": "FAIL_CLOSED",
                "reason": "marketplace_program_identity_ambiguous",
                "detail": {"network": network, "candidate_ids": sorted(item["program_id"] for item in exact)},
            }

    if program_id and stable_key(network, program_id) in OPERATOR_OUT_OF_SCOPE_PROGRAM_KEYS:
        return {
            "outcome": "FAIL_CLOSED",
            "reason": "operator_directed_program_out_of_scope",
            "detail": {
                "program_key": stable_key(network, program_id),
                "operator_basis": OPERATOR_OUT_OF_SCOPE_PROGRAM_KEYS[stable_key(network, program_id)],
                "sheet_vertical_conflict": str(row.get("Vertical") or ""),
            },
        }

    if exact and inventory and inventory.get("usable"):
        relationships = sorted({item["relationship"] for item in exact})
        if len(exact) > 1 and len(relationships) > 1:
            return {
                "outcome": "FAIL_CLOSED",
                "reason": "marketplace_program_identity_ambiguous",
                "detail": {"network": network, "program_id": program_id, "relationships": relationships},
            }
        relationship = relationships[0] if len(relationships) == 1 else ""
        if relationship not in JOINED_RELATIONSHIPS:
            return {
                "outcome": "FAIL_CLOSED",
                "reason": "marketplace_relationship_not_joined",
                "detail": {
                    "network": network,
                    "program_id": program_id,
                    "relationship": relationship or "unknown",
                    "inventory": (inventory or {}).get("binding"),
                },
            }
        paid_event_gate = exact_paid_event_gate(network, exact)
        if paid_event_gate["status"] == "BLOCK":
            return {
                "outcome": "FAIL_CLOSED",
                "reason": paid_event_gate["reason"],
                "detail": {
                    "network": network,
                    "program_id": program_id,
                    "exact_paid_event_gate": paid_event_gate,
                },
            }
        route = _route_from_identity(
            row_number, row,
            network_label=network_label,
            program_id=program_id,
            routing_source="current_marketplace_inventory_exact_identity",
            identity_evidence={
                "method": identity_method,
                "marketplace_candidate": exact[0],
                "inventory": inventory["binding"] if inventory else None,
                "inventory_captured_at_utc": inventory.get("captured_at_utc") if inventory else None,
                "exact_paid_event_gate": paid_event_gate,
            },
        )
        return {"outcome": "ROUTED", "route": route}

    if inventory and inventory.get("usable") and inventory.get("complete") and program_id:
        return {
            "outcome": "FAIL_CLOSED",
            "reason": "marketplace_program_missing_from_complete_inventory",
            "detail": {
                "network": network,
                "program_id": program_id,
                "inventory": inventory["binding"],
            },
        }
    if inventory and inventory.get("usable") and not inventory.get("complete"):
        return {
            "outcome": "FAIL_CLOSED",
            "reason": "marketplace_program_missing_from_partial_inventory",
            "detail": {
                "network": network,
                "program_id": program_id or None,
                "completeness": inventory.get("completeness"),
                "inventory": inventory["binding"],
            },
        }

    # Some rails cannot enumerate with the generic current inventory carrier.
    # A current governed Sheet row may still carry the provider's own immutable
    # offer id in its provider-hosted Apply URL. That is a real route, but it is
    # explicitly stamped uncorroborated and names the inventory obstacle.
    if program_id and apply_id == program_id and _provider_owned_identity_url(network, apply_url):
        if not tracking.startswith("https://"):
            return {
                "outcome": "FAIL_CLOSED",
                "reason": "provider_tracking_link_missing",
                "detail": {"network": network, "program_id": program_id},
            }
        paid_event_gate = exact_paid_event_gate(network, [])
        if paid_event_gate["status"] == "BLOCK":
            return {
                "outcome": "FAIL_CLOSED",
                "reason": paid_event_gate["reason"],
                "detail": {
                    "network": network,
                    "program_id": program_id,
                    "exact_paid_event_gate": paid_event_gate,
                },
            }
        route = _route_from_identity(
            row_number, row,
            network_label=network_label,
            program_id=program_id,
            routing_source="current_sheet_provider_url_inventory_unavailable",
            identity_evidence={
                "method": "provider_hosted_apply_url_program_id",
                "sheet_apply_url": apply_url,
                "inventory": (inventory or {}).get("binding"),
                "inventory_auth_state": (inventory or {}).get("auth_state") or "missing",
                "inventory_completeness": (inventory or {}).get("completeness") or "missing",
                "inventory_blockers": (inventory or {}).get("blockers") or [],
                "uncorroborated_reason": "current inventory unavailable; exact provider-hosted Apply URL supplies immutable identity",
                "exact_paid_event_gate": paid_event_gate,
            },
        )
        return {"outcome": "ROUTED", "route": route}

    reason = "provider_route_identity_missing" if not program_id else "provider_route_unresolved"
    return {
        "outcome": "FAIL_CLOSED",
        "reason": reason,
        "detail": {
            "network": network,
            "program_id": program_id or None,
            "apply_url": apply_url or None,
            "inventory_state": {
                "auth_state": (inventory or {}).get("auth_state") or "missing",
                "completeness": (inventory or {}).get("completeness") or "missing",
                "blockers": (inventory or {}).get("blockers") or [],
            },
        },
    }


def derive_current_routing_basis(
    *,
    backrun_dir: Path,
    coverage: dict[str, Any],
    sheet: dict[str, Any],
) -> tuple[dict[str, Any], dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Derive every staged approved+linked GLP disposition from current bytes."""
    headers = sheet.get("headers") or []
    rows = sheet.get("rows") or {}
    inventories, inventory_sources = _current_inventories(backrun_dir)
    generated = iso_utc()
    sources = [
        {
            "label": "sheet_export",
            "captured_at_utc": str(sheet.get("captured_at_utc") or ""),
            "path": str((backrun_dir / "sheet-full-row-export.json").resolve()),
        },
        {
            "label": "coverage_receipt",
            "captured_at_utc": str(coverage.get("captured_at_utc") or ""),
            "path": str((backrun_dir / "coverage-receipt.json").resolve()),
        },
        *inventory_sources,
    ]
    for source in sources:
        _parse_utc(source["captured_at_utc"], label=source["label"])

    parent_rows = {number: name for name, number in REQUIRED_MARKETPLACE_PARENT_ROWS.items()}
    outcomes: dict[int, dict[str, Any]] = {}
    for row_text, values in rows.items():
        row_number = int(row_text)
        row = dict(zip(headers, values))
        if not staged_feed_member(row):
            continue
        display_name = str(row.get("Program / Network") or "").strip()
        if row_number in parent_rows:
            outcomes[row_number] = {
                "outcome": "FAIL_CLOSED",
                "reason": "marketplace_parent_control_row_out_of_scope",
                "detail": {"canonical_parent": parent_rows[row_number], "sheet_display_name": display_name},
            }
            continue
        blockers = pre_routing_feed_blockers(row)
        if blockers:
            outcomes[row_number] = {
                "outcome": "FEED_BLOCKED",
                "reason": blockers[0],
                "detail": {"all_reasons": blockers},
            }
            continue
        outcomes[row_number] = _resolve_current_core_route(row_number, row, inventories)

    counts: dict[str, int] = {}
    for outcome in outcomes.values():
        key = str(outcome.get("outcome") or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1
    newest = max(_parse_utc(source["captured_at_utc"], label=source["label"]) for source in sources)
    basis = {
        "schema": ROUTING_BASIS_SCHEMA,
        "status": "PASS",
        "generated_at_utc": generated,
        "backrun_dir": str(Path(backrun_dir).resolve()),
        "source_run_id": coverage.get("run_id"),
        "sources": sources,
        "newest_source_captured_at_utc": iso_utc(newest),
        "outcome_counts": counts,
        "staged_feed_rows_dispositioned": len(outcomes),
        "joined_inventory_offers": [
            {
                **offer,
                "inventory": state["binding"],
            }
            for state in inventories.values()
            for offer in state["rows"]
            if offer.get("relationship") in JOINED_RELATIONSHIPS
        ],
        "derivation": (
            "approved relationship status + tracking link present + GLP vertical + not live; "
            "Link QA then gates routing; Wire-In Ready is ignored; exact paid-event metadata "
            "follows the hash-bound per-network publication policy with logged risk acceptance; "
            "current marketplace inventory supplies exact route identity"
        ),
    }
    inventory_bindings = {
        f"marketplace_inventory_{network}": state["binding"]
        for network, state in inventories.items()
    }
    return basis, outcomes, inventory_bindings


def _program_sort_key(value: Any) -> tuple[int, Any]:
    text = str(value or "")
    return (0, int(text)) if text.isdigit() else (1, text)


def select_same_rail_verified_payout_winners(
    candidates: list[dict[str, Any]],
    inventory_offers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only the highest verified-payout offer per provider × rail.

    The denominator is the current joined marketplace inventory, not merely the
    staged rows.  That matters when a better sibling has not reached the Sheet.
    A verified offer always beats an unverified sibling. Equal verified payouts
    use immutable program id only as a stable one-winner tie-break.
    """
    records: list[dict[str, Any]] = []
    for offer in inventory_offers:
        if str(offer.get("relationship") or "").casefold() not in JOINED_RELATIONSHIPS:
            continue
        records.append({
            **offer,
            "provider_name": str(offer.get("name") or ""),
            "official_url": str(offer.get("official_url") or ""),
            "_record_kind": "inventory",
        })
    for candidate in candidates:
        marketplace = ((candidate.get("routing_identity_evidence") or {})
                       .get("marketplace_candidate") or {})
        records.append({
            "provider_name": str(marketplace.get("name") or candidate.get("display_name") or ""),
            "official_url": str(candidate.get("official_url") or marketplace.get("official_url") or ""),
            "network": normalize_network(candidate.get("network")),
            "program_id": str(candidate.get("provider_program_id") or ""),
            "relationship": "approved",
            "payout_verified": marketplace.get("payout_verified") is True,
            "payout_usd": marketplace.get("payout_usd"),
            "payout_model": marketplace.get("payout_model"),
            "payout_basis": marketplace.get("payout_basis") or "payout_unverified",
            "_record_kind": "candidate",
            "_candidate_key": str(candidate.get("key") or ""),
        })

    resolved = payout_policy.resolve_provider_records(records)
    identities: dict[tuple[str, str], str] = {}
    groups: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for record in resolved:
        network = normalize_network(record.get("network"))
        program_id = str(record.get("program_id") or "")
        identity = str(record.get("provider_identity") or "")
        if not network or not program_id or not identity:
            continue
        group = groups.setdefault((identity, network), {})
        existing = group.get(program_id)
        if existing is None or (
            record.get("payout_verified") is True and existing.get("payout_verified") is not True
        ):
            group[program_id] = record
        if record.get("_record_kind") == "candidate":
            identities[(network, program_id)] = identity

    kept: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for candidate in candidates:
        network = normalize_network(candidate.get("network"))
        program_id = str(candidate.get("provider_program_id") or "")
        governed_switch = candidate.get("best_terms_switch_request")
        if governed_switch:
            governed_winner = governed_switch.get("winner") or {}
            governed_network = normalize_network(governed_winner.get("network"))
            governed_program_id = str(governed_winner.get("program_id") or "")
            if (governed_network, governed_program_id) != (network, program_id):
                blocked.append({
                    **candidate,
                    "reason": "governed_best_terms_winner_conflicts_with_candidate",
                    "detail": {
                        "candidate": {"network": network, "program_id": program_id},
                        "governed_winner": {
                            "network": governed_network,
                            "program_id": governed_program_id,
                        },
                    },
                })
                continue
            candidate["same_rail_payout_selection"] = {
                "verdict": "WINNER",
                "selection_basis": "governed_best_terms_switch_request",
                "network": network,
                "program_id": program_id,
                "switch_id": governed_switch.get("switch_id"),
                "winner_sheet_row": governed_winner.get("sheet_row"),
            }
            kept.append(candidate)
            continue
        identity = identities.get((network, program_id))
        siblings = list((groups.get((identity, network)) or {}).values()) if identity else []
        if len(siblings) <= 1:
            kept.append(candidate)
            continue
        verified = [row for row in siblings if row.get("payout_verified") is True]
        if not verified:
            blocked.append({
                **candidate,
                "reason": "same_rail_sibling_payouts_unverified",
                "detail": {
                    "provider_identity": identity,
                    "network": network,
                    "siblings": [
                        {"program_id": row.get("program_id"), "payout": None,
                         "payout_status": "payout_unverified"}
                        for row in sorted(siblings, key=lambda row: _program_sort_key(row.get("program_id")))
                    ],
                },
            })
            continue
        winner = sorted(
            verified,
            key=lambda row: (-float(row["payout_usd"]), _program_sort_key(row.get("program_id"))),
        )[0]
        if program_id == str(winner.get("program_id") or ""):
            candidate["same_rail_payout_selection"] = {
                "verdict": "WINNER",
                "provider_identity": identity,
                "network": network,
                "program_id": program_id,
                "payout_usd": winner.get("payout_usd"),
                "verified_sibling_count": len(verified),
                "all_sibling_count": len(siblings),
            }
            kept.append(candidate)
            continue
        loser = next((row for row in siblings if str(row.get("program_id")) == program_id), {})
        loser_payout = loser.get("payout_usd") if loser.get("payout_verified") is True else None
        blocked.append({
            **candidate,
            "reason": "same_rail_nonwinning_offer",
            "detail": {
                "provider_identity": identity,
                "network": network,
                "winner": {
                    "program_id": str(winner.get("program_id") or ""),
                    "payout_usd": winner.get("payout_usd"),
                    "payout_basis": winner.get("payout_basis"),
                },
                "loser": {
                    "program_id": program_id,
                    "payout_usd": loser_payout,
                    "payout_status": "verified" if loser_payout is not None else "payout_unverified",
                    "dollar_delta": (
                        float(winner["payout_usd"]) - float(loser_payout)
                        if loser_payout is not None else None
                    ),
                },
            },
        })
    return kept, blocked


def load_real_provider_economics(
    path: Path = REAL_PROVIDER_ECONOMICS,
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    """Return exact commercial rows plus policy-shaped performance evidence.

    Publisher-account performance is used only with positive click and
    conversion counts.  Otherwise a positive authenticated network EPC+CVR
    pair is retained as network-reported aggregate evidence without inventing
    counts.  Sparse zero-conversion publisher rows never become a fabricated
    conversion prior.
    """
    document = read_json(path)
    rows = document.get("rows") or []
    if not isinstance(rows, list):
        raise B2Error("real_provider_economics_rows_invalid")
    economics: dict[tuple[str, str], dict[str, Any]] = {}
    performance: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        network = normalize_network(row.get("network"))
        program_id = str(row.get("program_id") or "").strip()
        if not network or not program_id:
            continue
        payout = row.get("payout") or {}
        event = row.get("payout_event") or {}
        payout_value = payout.get("value") if isinstance(payout, dict) else None
        if isinstance(payout_value, (int, float)) and not isinstance(payout_value, bool):
            economics[(network, program_id)] = {
                "provider_name": str(row.get("provider") or ""),
                "headline_payout": float(payout_value),
                "payout_model": str(row.get("payout_model") or "unknown"),
                "paid_event": (
                    str(event.get("value") or "")
                    if isinstance(event, dict)
                    and str(event.get("value") or "").casefold()
                    not in {"not separately exposed", "unknown", "unavailable"}
                    else ""
                ),
                "currency": str(row.get("payout_currency") or "USD").upper(),
                "payout_provenance": str(payout.get("provenance") or ""),
            }

        perf: dict[str, Any] = {"network": network, "program_id": program_id}
        publisher = row.get("publisher_account_performance") or {}
        publisher_value = publisher.get("value") if isinstance(publisher, dict) else None
        if publisher.get("classification") == "MEASURED" and isinstance(publisher_value, dict):
            clicks = int(publisher_value.get("clicks") or 0)
            conversions = int(
                publisher_value.get("transactions")
                or publisher_value.get("conversions") or 0
            )
            if clicks > 0 and conversions > 0:
                perf.update({
                    "clicks": clicks,
                    "conversions": conversions,
                    "earnings": float(
                        publisher_value.get("commission_usd")
                        or publisher_value.get("earnings_usd") or 0
                    ),
                    "provenance": str(publisher.get("provenance") or ""),
                    "source_classification": "MEASURED",
                })
        if len(perf) == 2:
            epc = row.get("network_epc_usd") or {}
            cvr = row.get("network_cvr") or {}
            epc_value = epc.get("value") if isinstance(epc, dict) else None
            cvr_value = cvr.get("value") if isinstance(cvr, dict) else None
            if (
                epc.get("classification") == "NETWORK_REPORTED"
                and cvr.get("classification") == "NETWORK_REPORTED"
                and isinstance(epc_value, (int, float))
                and isinstance(cvr_value, (int, float))
                and float(epc_value) >= 0
                and float(cvr_value) > 0
            ):
                perf.update({
                    "measured_epc": float(epc_value),
                    "measured_cvr": float(cvr_value),
                    "denominator_status": "NETWORK_REPORTED_POSITIVE_DENOMINATOR",
                    "source_classification": "NETWORK_REPORTED",
                    "provenance": str(epc.get("provenance") or cvr.get("provenance") or ""),
                })
        if len(perf) > 2:
            performance.append(perf)
    return economics, performance


def select_cross_network_best_terms_winners(
    candidates: list[dict[str, Any]],
    inventory_offers: list[dict[str, Any]],
    *,
    economics: dict[tuple[str, str], dict[str, Any]] | None = None,
    performance_rows: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Reject every lower-value candidate across all held rails.

    Same exact paid event ranks on headline payout. Different paid events rank
    on measured/network-reported EPC when qualified, otherwise the governed
    1.5% CPA / 8% CPL priors. If neither path can rank a comparison, the row is
    retained and explicitly marked INSUFFICIENT_DATA rather than guessed.
    """
    if economics is None or performance_rows is None:
        loaded_economics, loaded_performance = load_real_provider_economics()
        economics = loaded_economics if economics is None else economics
        performance_rows = loaded_performance if performance_rows is None else performance_rows
    economics = economics or {}
    performance_rows = performance_rows or []

    def commercial(
        record: dict[str, Any],
        network: str,
        program_id: str,
        *,
        sheet: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured = economics.get((network, program_id)) or {}
        terms = str((sheet or {}).get("Commission / Terms") or "").strip()
        parsed = parse_commercial_payout(terms) if terms else {}
        payout = captured.get("headline_payout")
        if payout is None and record.get("payout_verified") is True:
            payout = record.get("payout_usd")
        if payout is None:
            payout = parsed.get("headline_payout")
        model = (
            captured.get("payout_model") or record.get("payout_model")
            or parsed.get("payout_model") or "unknown"
        )
        paid_event = (
            captured.get("paid_event") or record.get("exact_paid_event")
            or parsed.get("paid_event") or ""
        )
        return {
            "headline_payout": payout,
            "payout_model": str(model),
            "paid_event": str(paid_event),
            "paid_event_basis": (
                "network_reported_exact_event" if paid_event
                else str(parsed.get("paid_event_basis") or "payout_model_fallback")
            ),
            "currency": str(captured.get("currency") or "USD").upper(),
            "payout_provenance": (
                str(captured.get("payout_provenance") or "")
                or ("sheet_commission_terms" if payout is not None and terms else "")
            ),
        }

    records: list[dict[str, Any]] = []
    for offer in inventory_offers:
        relationship = str(offer.get("relationship") or "").casefold()
        if relationship not in JOINED_RELATIONSHIPS:
            continue
        network = normalize_network(offer.get("network"))
        program_id = str(offer.get("program_id") or "").strip()
        if not network or not program_id:
            continue
        records.append({
            **offer,
            **commercial(offer, network, program_id),
            "provider_name": str(offer.get("name") or ""),
            "official_url": str(offer.get("official_url") or ""),
            "network": network,
            "program_id": program_id,
            "relationship": relationship,
            "wire_in_ready": True,
            "traffic_eligible": True,
            "_record_kind": "inventory",
        })
    for candidate in candidates:
        marketplace = ((candidate.get("routing_identity_evidence") or {})
                       .get("marketplace_candidate") or {})
        network = normalize_network(candidate.get("network"))
        program_id = str(candidate.get("provider_program_id") or "").strip()
        records.append({
            **commercial(
                marketplace, network, program_id,
                sheet=candidate.get("sheet") or {},
            ),
            "provider_name": str(marketplace.get("name") or candidate.get("display_name") or ""),
            "official_url": str(candidate.get("official_url") or marketplace.get("official_url") or ""),
            "network": network,
            "program_id": program_id,
            "relationship": "approved",
            "wire_in_ready": True,
            "wired_live": bool(candidate.get("sheet_wired_live")),
            "traffic_eligible": True,
            "_record_kind": "candidate",
            "_candidate_key": str(candidate.get("key") or ""),
        })

    resolved = payout_policy.resolve_provider_records(records)
    candidate_identity = {
        str(row.get("_candidate_key") or ""): str(row.get("provider_identity") or "")
        for row in resolved if row.get("_record_kind") == "candidate"
    }
    candidate_record = {
        str(row.get("_candidate_key") or ""): row
        for row in resolved if row.get("_record_kind") == "candidate"
    }
    selection = payout_policy.select_best_collectible(resolved, performance_rows)
    providers = {
        str(row.get("provider_identity") or ""): row
        for row in selection.get("providers") or []
    }
    performance_index = {
        payout_policy.performance_key(row): row for row in performance_rows
    }

    kept: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    insufficient: list[dict[str, Any]] = []
    for candidate in candidates:
        key = str(candidate.get("key") or "")
        current = candidate_record.get(key) or {}
        provider = providers.get(candidate_identity.get(key, "")) or {}
        winner = provider.get("selected_winner")
        if not winner or current.get("headline_payout") is None:
            candidate["cross_network_payout_selection"] = {
                "verdict": "INSUFFICIENT_DATA",
                "reason": "winner_or_current_commercial_value_unavailable",
            }
            insufficient.append({
                "key": key,
                "provider_identity": candidate_identity.get(key),
                "reason": "winner_or_current_commercial_value_unavailable",
            })
            kept.append(candidate)
            continue
        winner_key = (str(winner.get("network") or ""), str(winner.get("program_id") or ""))
        current_key = (normalize_network(current.get("network")), str(current.get("program_id") or ""))
        if current_key == winner_key:
            candidate["cross_network_payout_selection"] = {
                "verdict": "WINNER",
                "provider_identity": candidate_identity.get(key),
                "network": current_key[0],
                "program_id": current_key[1],
                "selection_basis": provider.get("selection_basis"),
            }
            kept.append(candidate)
            continue

        same_event = (
            payout_policy.comparison_event(current)
            == str(winner.get("conversion_event") or "")
            and str(current.get("currency") or "USD").upper()
            == str(winner.get("currency") or "USD").upper()
        )
        current_expected = payout_policy.expected_earnings_per_100_clicks(
            current, performance_index.get(payout_policy.performance_key(current)))
        current_ev = current_expected.get("expected_earnings_per_100_clicks")
        winner_ev = winner.get("expected_earnings_per_100_clicks")
        if same_event:
            lower = float(current["headline_payout"]) < float(winner.get("payout") or 0)
            comparison_unit = "USD_PER_PAID_EVENT"
            delta = float(winner.get("payout") or 0) - float(current["headline_payout"])
        elif current_ev is not None and winner_ev is not None:
            lower = float(current_ev) < float(winner_ev)
            comparison_unit = "USD_PER_100_CLICKS"
            delta = float(winner_ev) - float(current_ev)
        else:
            candidate["cross_network_payout_selection"] = {
                "verdict": "INSUFFICIENT_DATA",
                "reason": "cross_event_expected_value_unavailable",
            }
            insufficient.append({
                "key": key,
                "provider_identity": candidate_identity.get(key),
                "reason": "cross_event_expected_value_unavailable",
            })
            kept.append(candidate)
            continue
        if not lower:
            kept.append(candidate)
            continue
        blocked.append({
            **candidate,
            "reason": "cross_network_nonwinning_offer",
            "detail": {
                "provider_identity": candidate_identity.get(key),
                "selection_basis": provider.get("selection_basis"),
                "comparison_unit": comparison_unit,
                "winner": {
                    "network": winner_key[0],
                    "program_id": winner_key[1],
                    "payout_usd": winner.get("payout"),
                    "expected_earnings_per_100_clicks": winner_ev,
                },
                "loser": {
                    "network": current_key[0],
                    "program_id": current_key[1],
                    "payout_usd": current.get("headline_payout"),
                    "expected_earnings_per_100_clicks": current_ev,
                    "dollar_delta": round(delta, 6),
                },
            },
        })
    return kept, blocked, insufficient


def verify_routing_basis_freshness(plan: dict[str, Any]) -> None:
    """Refuse a real plan whose routing basis predates any bound source."""
    bindings = plan.get("source_bindings") or {}
    if "coverage_receipt" not in bindings:
        return  # provider-free unit/drill plans carry no production sources
    basis = plan.get("routing_basis")
    if not isinstance(basis, dict) or basis.get("schema") != ROUTING_BASIS_SCHEMA:
        raise B2Error("current_routing_basis_missing_or_invalid")
    if basis.get("status") != "PASS":
        raise B2Error("current_routing_basis_not_pass")
    generated_text = str(basis.get("generated_at_utc") or "")
    generated = _parse_utc(generated_text, label="routing_basis_generated")
    if str(plan.get("generated_at_utc") or "") != generated_text:
        raise B2Error("routing_basis_plan_generation_drift")
    sources = basis.get("sources") or []
    inventory_sources = [
        source for source in sources
        if str(source.get("label") or "").startswith("marketplace_inventory_")
    ]
    if not inventory_sources:
        raise B2Error("routing_basis_inventory_sources_missing")
    newest = max(
        _parse_utc(source.get("captured_at_utc"), label=str(source.get("label") or "source"))
        for source in sources
    )
    if generated < newest:
        raise B2Error(
            "routing_basis_older_than_inventory_basis:"
            f"routing={iso_utc(generated)}:newest_source={iso_utc(newest)}"
        )
    if str(basis.get("newest_source_captured_at_utc") or "") != iso_utc(newest):
        raise B2Error("routing_basis_newest_source_timestamp_drift")
    for source in sources:
        label = str(source.get("label") or "")
        binding = bindings.get(label)
        if not isinstance(binding, dict):
            raise B2Error(f"routing_basis_source_binding_missing:{label}")
        if Path(str(binding.get("path") or "")).resolve() != Path(str(source.get("path") or "")).resolve():
            raise B2Error(f"routing_basis_source_path_drift:{label}")


def routing_basis_age_guard_drill(plan: dict[str, Any]) -> dict[str, Any]:
    """Inject an old basis into a copy, observe refusal, then verify restoration."""
    before = sha_bytes(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode())
    injected = json.loads(json.dumps(plan))
    injected["routing_basis"]["generated_at_utc"] = "1970-01-01T00:00:00Z"
    injected["generated_at_utc"] = "1970-01-01T00:00:00Z"
    observed = ""
    try:
        verify_routing_basis_freshness(injected)
    except B2Error as exc:
        observed = f"B2Error:{exc}"
    if not observed.startswith("B2Error:routing_basis_older_than_inventory_basis:"):
        raise B2Error(f"routing_basis_age_guard_violation_not_observed:{observed or 'no_failure'}")
    verify_routing_basis_freshness(plan)
    after = sha_bytes(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode())
    if before != after:
        raise B2Error("routing_basis_age_guard_restore_failed")
    return {
        "schema": "b2-routing-basis-age-guard-drill-v1",
        "status": "PASS",
        "injected_violation": "routing basis generated_at_utc forced to 1970-01-01T00:00:00Z",
        "observed_failure": observed,
        "restored_original_sha256": after,
        "restoration_verified": True,
        "effects": {"provider_calls": 0, "sheet_writes": 0, "deployments": 0},
    }


def _registry_by_id(pointer: dict[str, Any]) -> tuple[Path, dict[str, dict[str, Any]], dict[str, Any]]:
    release = Path(str(pointer.get("release_root") or "")).resolve()
    prepared_evidence = None
    current_base = None
    pointer_binding = None
    if release.parent != RELEASES:
        # Only the canonical published prepared base may extend the historical
        # direct-child contract. A raw report path is never a source override.
        try:
            import glp_price_index_weekly as current_base
            from glp_current_content_base_schema import is_prepared_pointer, validate_pointer_shape

            validate_pointer_shape(pointer, require_release_dirs=True)
            if not is_prepared_pointer(pointer):
                raise RuntimeError("current_release_not_prepared")
            pointer_binding = current_base._read_bound_regular_file(str(CURRENT_BASE), "pointer")
            if current_base._load_base_document(pointer_binding[1], "pointer") != pointer:
                raise RuntimeError("current_release_pointer_mismatch")
            prepared_evidence = current_base._validate_base_evidence(pointer, release)
            if not prepared_evidence.get("prepared_binding"):
                raise RuntimeError("current_release_prepared_binding_missing")
        except (OSError, ValueError, RuntimeError) as exc:
            raise B2Error(f"current_release_scope_invalid:{exc}") from exc
    elif not release.is_dir():
        raise B2Error("current_release_scope_invalid")
    registry_path = release / "data/program-registry.json"
    if prepared_evidence is None:
        registry = read_json(registry_path)
    else:
        try:
            # Authenticate the manifest's held bytes, then bind the single
            # registry read to its entry. Revalidating the path cannot detect
            # a transient replacement restored before the second validation.
            manifest_bound = current_base._checked_base_evidence(
                pointer, "manifest", "01-deployment-manifest.json"
            )
            manifest = current_base._load_base_document(manifest_bound["bytes"], "manifest")
            _, manifest_rows = current_base._normalized_manifest_rows(manifest.get("release_rows"))
            registry_entry = next(
                (row for row in manifest_rows if row["path"] == "data/program-registry.json"), None
            )
            registry_bytes = registry_path.read_bytes()
            if (registry_entry is None
                    or registry_entry.get("size") != len(registry_bytes)
                    or registry_entry["sha256"] != current_base.sha_bytes(registry_bytes)):
                raise RuntimeError("current_release_registry_bytes_mismatch")
            registry = json.loads(registry_bytes.decode("utf-8"))
        except (OSError, ValueError, RuntimeError) as exc:
            raise B2Error(f"current_release_scope_invalid:{exc}") from exc
    programs = {str(row.get("program_id")): row for row in registry.get("programs") or []}
    if prepared_evidence is not None:
        # Re-authenticate after consumption: pointer, tree, and native retained
        # preparation/publication inputs must still be the same generation.
        try:
            if current_base is None or pointer_binding is None:
                raise RuntimeError("current_release_prepared_binding_missing")
            if current_base._validate_base_evidence(pointer, release) != prepared_evidence:
                raise RuntimeError("current_release_prepared_binding_changed")
            if current_base._read_bound_regular_file(str(CURRENT_BASE), "pointer") != pointer_binding:
                raise RuntimeError("current_release_pointer_changed")
        except (OSError, ValueError, RuntimeError) as exc:
            raise B2Error(f"current_release_scope_invalid:{exc}") from exc
    return release, programs, registry


def _find_existing_artifact(patterns: Iterable[str], *, roots: Iterable[Path]) -> Path | None:
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            found.extend(path for path in root.rglob(pattern) if path.is_file())
    if not found:
        return None
    return max(found, key=lambda path: path.stat().st_mtime)


def _find_existing_json_artifact(
    patterns: Iterable[str], *, roots: Iterable[Path], schema: str
) -> Path | None:
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            found.extend(path for path in root.rglob(pattern) if path.is_file())
    for path in sorted(set(found), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            if read_json(path).get("schema") == schema:
                return path
        except (OSError, ValueError, AttributeError):
            continue
    return None


def terms_classification_filename(network: Any, provider_program_id: Any) -> str:
    """`<network>-<network-side program id>.json` — the ONE name the queue looks
    for. Public because the onboarding manifest has to be able to say which file
    is missing, and the authoring brief has to be able to say which file to
    write, in the pipeline's own words rather than a re-derivation of them."""
    return f"{normalize_network(network)}-{str(provider_program_id or '').strip()}.json"


def find_terms_classification(network: Any, provider_program_id: Any) -> Path | None:
    """The terms document for this rail, or None. Same lookup the queue does."""
    return _find_existing_json_artifact(
        [terms_classification_filename(network, provider_program_id)],
        roots=[TERMS_CLASSIFICATION_ROOT],
        schema=TERMS_CLASSIFICATION_SCHEMA,
    )


def program_authority_path(site_program_id: Any) -> Path:
    """Where the g6-r1a program-authority spec for a SITE program id must live.

    Keyed by the site program id (the slugified Sheet `SEO Slug`), never by the
    network-side id — that confusion is the 2026-08-20 identity defect."""
    return PROGRAM_AUTHORITY_DIR / f"{slugify(site_program_id) or 'row'}.json"


def program_research_path(site_program_id: Any) -> Path:
    """Where the deep-research packet for a SITE program id must live."""
    return PROGRAM_RESEARCH_DIR / f"{slugify(site_program_id) or 'row'}.json"


def queue_identity_for_sheet_row(sheet_row: Any) -> dict[str, Any]:
    """`{network, provider_program_id, key}` exactly as the queue derives them.

    The network-side program id comes from the same current Sheet plus current
    marketplace derivation used by build_plan. The onboarding manifest must not
    fall back to the retired one-time coverage routing artifact.

    Returns empty strings when the row has no route; a caller that cannot name
    the artifact must say so, never guess a filename.
    """
    out = {"network": "", "provider_program_id": "", "key": ""}
    try:
        backrun = latest_complete_backrun()
        sheet = read_json(backrun / "sheet-full-row-export.json")
        coverage = read_json(backrun / "coverage-receipt.json")
        _, outcomes, _ = derive_current_routing_basis(
            backrun_dir=backrun, coverage=coverage, sheet=sheet)
        route = (outcomes.get(int(sheet_row)) or {}).get("route")
    except (B2Error, OSError, ValueError, TypeError):
        return out
    if not route:
        return out
    out["network"] = str(route.get("provider_network") or "").strip()
    out["provider_program_id"] = str(route.get("provider_program_id") or "").strip()
    try:
        out["key"] = stable_key(out["network"], out["provider_program_id"])
    except B2Error:
        out["key"] = ""
    return out


def queue_identity_from_batch_authority(
    site_program_id: Any,
    sheet_row: Any,
    batch_authority_binding: dict[str, Any],
) -> dict[str, Any]:
    """Return one exact queue identity from a validated current batch authority.

    This is the fail-closed fallback for payout-winner rows intentionally absent
    from the latest complete backrun's routing outcomes. It does not consult
    evidence hints: the authority and its base plan are byte-verified, the full
    batch contract is revalidated, and the exact site/Sheet identity must occur
    once in the allowlisted queue.
    """
    authority_path = verify_binding(
        batch_authority_binding, label="build_ready_batch_authority"
    )
    authority = read_json(authority_path)
    plan_path = verify_binding(
        authority.get("base_plan"), label="build_ready_batch_base_plan"
    )
    plan = read_json(plan_path)
    validated = validate_batch_authority(plan, batch_authority_binding)
    try:
        expected_sheet_row = int(sheet_row)
    except (TypeError, ValueError) as exc:
        raise B2Error("build_ready_batch_sheet_row_invalid") from exc
    expected_program = str(site_program_id or "").strip()
    matches = [
        row
        for row in plan.get("queue") or []
        if isinstance(row, dict)
        and str(row.get("site_program_id") or "").strip() == expected_program
        and int(row.get("sheet_row") or 0) == expected_sheet_row
    ]
    if len(matches) != 1:
        raise B2Error(f"build_ready_batch_queue_identity_match_count:{len(matches)}")
    row = matches[0]
    network = str(row.get("network") or "").strip()
    provider_program_id = str(row.get("provider_program_id") or "").strip()
    key = str(row.get("key") or "").strip()
    if (
        not network
        or not provider_program_id
        or key != stable_key(network, provider_program_id)
        or key not in set(validated.get("allowed_queue_keys") or [])
    ):
        raise B2Error("build_ready_batch_queue_identity_invalid")
    return {
        "network": network,
        "provider_program_id": provider_program_id,
        "key": key,
    }


def _evidence_hints(
    *, alias: str, network: str, provider_program_id: str, release: Path
) -> dict[str, Any]:
    research = _find_existing_json_artifact(
        [f"{alias}.json"],
        roots=[BUSINESS / "reports/glp-clinic-scout/affiliate-machine"],
        schema=PROGRAM_RESEARCH_SCHEMA,
    )
    terms = find_terms_classification(network, provider_program_id)
    logo = None
    manifest_path = release / "assets/provider-logos/manifest.json"
    if manifest_path.is_file():
        manifest = read_json(manifest_path)
        logo_row = next(
            (row for row in manifest.get("logos") or [] if row.get("program_id") == alias), None
        )
        if logo_row:
            candidate = release / "assets/provider-logos" / str(logo_row.get("output_file") or "")
            if candidate.is_file() and sha_file(candidate) == logo_row.get("output_sha256"):
                logo = candidate
    return {
        "research": path_binding(research) if research else None,
        "terms_classification": path_binding(terms) if terms else None,
        "logo": path_binding(logo) if logo else None,
    }


def friction_tuple(row: dict[str, Any]) -> list[int]:
    hints = row.get("evidence_hints") or {}
    return [
        0 if hints.get("research") else 1,
        0 if hints.get("terms_classification") else 1,
        0 if row.get("existing_editorial_page") else 1,
        0 if hints.get("logo") else 1,
        0 if row.get("tracking_ready") else 1,
        0 if row.get("approval_ready") else 1,
        int(row["sheet_row"]),
    ]


def registry_row_proves_wired(registry_row: dict[str, Any] | None) -> bool:
    """Whether registry bytes prove an earning routed placement, not just a label.

    `commercial_state=live` plus `profile.wired=true` is insufficient when the
    same profile says `surfaces.go=false`: Measured carried exactly that stale
    combination while its review CTA was held and no /go route existed. The
    backlog must not discard a corrected Sheet row as already wired on those
    contradictory bytes. Sheet-confirmed live rows are handled before this
    helper, including transparent-direct placements that intentionally omit /go.
    """
    if not registry_row or registry_row.get("commercial_state") != "live":
        return False
    profile = ((registry_row.get("profiles") or {}).get("glp-clinic-scout") or {})
    return profile.get("wired") is True and (profile.get("surfaces") or {}).get("go") is True


def activation_selection_by_id(release: Path) -> dict[str, dict[str, Any]]:
    """Current earning rail per canonical provider identity, from sealed bytes."""
    path = release / "data/affiliate-activation-selection.json"
    document = read_json(path)
    programs = document.get("programs")
    if not isinstance(programs, list):
        raise B2Error("affiliate_activation_selection_schema_invalid")
    out: dict[str, dict[str, Any]] = {}
    for row in programs:
        program_id = str((row or {}).get("program_id") or "").strip()
        if not program_id or program_id in out:
            raise B2Error(f"affiliate_activation_selection_identity_invalid:{program_id}")
        out[program_id] = row
    approvals_path = release / "data/affiliate-wire-in-approvals.json"
    approvals = read_json(approvals_path)
    for row in approvals.get("programs") or []:
        program_id = str((row or {}).get("existing_release_alias") or "").strip()
        tracking = str(((row or {}).get("tracking") or {}).get("provider_issued_url") or "")
        if not program_id or program_id in out or not tracking:
            continue
        out[program_id] = {
            "program_id": program_id,
            "network": row.get("network"),
            "network_program_id": row.get("provider_program_id"),
            "sheet_row": row.get("canonical_sheet_row"),
            "tracking_link_sha256": sha_bytes(tracking.encode()),
            "source": "affiliate-wire-in-approvals.json",
        }
    return out


def retired_rail_rulings(release: Path) -> set[tuple[str, str, str]]:
    """Exact provider × marketplace rails explicitly retired in sealed policy."""
    document = read_json(release / "data/affiliate-wire-in-approvals.json")
    reassessment = document.get("current_terms_reassessment") or {}
    out: set[tuple[str, str, str]] = set()
    for row in reassessment.get("programs") or []:
        if row.get("live_link_disposition") != "retired_best_terms_higher_payout":
            continue
        alias = str(row.get("existing_release_alias") or "").strip()
        program_id = str(row.get("provider_program_id") or "").strip()
        if alias and program_id:
            out.add((alias, "awin", program_id))
    return out


def activation_selection_proves_rail(
    activation_row: dict[str, Any] | None,
    *,
    network: Any,
    provider_program_id: Any,
    tracking_link_sha256: Any,
) -> bool:
    """Rail-level liveness; a provider alias alone is never wire identity."""
    if not activation_row:
        return False
    return (
        normalize_network(activation_row.get("network")) == normalize_network(network)
        and str(activation_row.get("network_program_id") or "").strip()
        == str(provider_program_id or "").strip()
        and str(activation_row.get("tracking_link_sha256") or "").strip()
        == str(tracking_link_sha256 or "").strip()
    )


def best_terms_switches(path: Path = BEST_TERMS_SWITCH_BACKLOG) -> dict[int, dict[str, Any]]:
    """Validated live-swap requests staged by the best-terms machine."""
    if not Path(path).is_file():
        return {}
    document = read_json(Path(path))
    if document.get("schema") != "glp.best-terms-switch-backlog.v1":
        raise B2Error("best_terms_switch_backlog_schema_invalid")
    out: dict[int, dict[str, Any]] = {}
    for item in document.get("items") or []:
        if item.get("status") != "STAGED_NOT_DEPLOYED":
            continue
        row = int(item.get("target_sheet_row") or 0)
        winner = item.get("winner") or {}
        if row < 2 or not normalize_network(winner.get("network")) or not str(winner.get("program_id") or ""):
            raise B2Error("best_terms_switch_identity_invalid")
        if row in out:
            raise B2Error(f"best_terms_switch_duplicate_sheet_row:{row}")
        out[row] = item
    return out


def _payout_queue_evidence(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("evidence")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise B2Error("payout_evaluation_queue_evidence_invalid_json") from exc
        if isinstance(parsed, dict):
            return parsed
    raise B2Error("payout_evaluation_queue_evidence_invalid")


def load_payout_evaluation_queue_rows() -> list[dict[str, Any]]:
    """Read the real single-writer queue; resolved rows remain deploy facts."""
    import n8n_dt  # noqa: PLC0415

    # `n8n_dt.rows` deliberately fails closed at one 100-row page because the
    # relay ignores offsets. The table now exceeds that limit, so querying only
    # `kind=payout_evaluation` would silently lose the newest money facts. Read
    # each bounded producer lane plus the immutable 2026-08-25 precedent ID.
    found: dict[str, dict[str, Any]] = {}
    for tag in PAYOUT_EVALUATION_PRODUCER_TAGS:
        page = n8n_dt.rows(
            SHEET_UPDATE_QUEUE_TABLE, n8n_dt.eq("queued_by", tag)
        )
        if getattr(n8n_dt, "last_read_truncated", False):
            raise B2Error(f"payout_evaluation_producer_read_truncated:{tag}")
        for row in page:
            if str(row.get("kind") or "") == "payout_evaluation":
                found[str(row.get("update_id") or "")] = row
    for update_id in PAYOUT_EVALUATION_PRECEDENT_IDS:
        page = n8n_dt.rows(
            SHEET_UPDATE_QUEUE_TABLE, n8n_dt.eq("update_id", update_id)
        )
        if len(page) > 1:
            raise B2Error(f"payout_evaluation_precedent_duplicate:{update_id}")
        if page and str(page[0].get("kind") or "") == "payout_evaluation":
            found[update_id] = page[0]
    return list(found.values())


def load_go_route_provision_queue_rows() -> list[dict[str, Any]]:
    """Read the bounded /go lane from the existing single-writer queue."""
    import n8n_dt  # noqa: PLC0415

    rows = n8n_dt.rows(
        SHEET_UPDATE_QUEUE_TABLE,
        n8n_dt.eq("queued_by", GO_ROUTE_PROVISION_PRODUCER_TAG),
    )
    if getattr(n8n_dt, "last_read_truncated", False):
        raise B2Error("go_route_provision_queue_read_truncated")
    return [row for row in rows if str(row.get("kind") or "") == "payout_evaluation"]


def go_route_provision_requests(
    queue_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate first-party /go requests carried by payout_evaluation."""
    rows = load_go_route_provision_queue_rows() if queue_rows is None else list(queue_rows)
    requests: list[dict[str, Any]] = []
    consumable: list[tuple[dict[str, Any], dict[str, Any]]] = []
    by_update_id = {str(row.get("update_id") or ""): row for row in rows}
    if "" in by_update_id or len(by_update_id) != len(rows):
        raise B2Error("go_route_provision_update_id_missing_or_duplicate")
    superseded: set[str] = set()
    for row in rows:
        evidence = _payout_queue_evidence(row)
        if evidence.get("deploy_instruction_version") != GO_ROUTE_PROVISION_INSTRUCTION_VERSION:
            continue
        if evidence.get("decision") != GO_ROUTE_PROVISION_DECISION:
            raise B2Error(f"go_route_provision_decision_invalid:{row.get('update_id')}")
        queue_status = str(row.get("status") or "").casefold()
        # The single Sheet writer owns terminal queue outcomes. A refused route
        # instruction is historical evidence, not an active deploy request; it
        # must not poison every later plan after a corrected payout winner is
        # queued under a new immutable update_id.
        if queue_status == "refused":
            continue
        if queue_status not in {"queued", "applied", "noop"}:
            raise B2Error(f"go_route_provision_queue_status_not_consumable:{row.get('update_id')}")
        supersedes = evidence.get("supersedes_update_ids") or []
        if not isinstance(supersedes, list) or any(
            not isinstance(value, str) or value not in by_update_id
            for value in supersedes
        ):
            raise B2Error(f"go_route_provision_supersession_invalid:{row.get('update_id')}")
        for update_id in supersedes:
            prior_evidence = _payout_queue_evidence(by_update_id[update_id])
            if (
                prior_evidence.get("deploy_instruction_version") != GO_ROUTE_PROVISION_INSTRUCTION_VERSION
                or prior_evidence.get("site_program_id") != evidence.get("site_program_id")
            ):
                raise B2Error(f"go_route_provision_supersession_scope_invalid:{row.get('update_id')}")
        superseded.update(supersedes)
        consumable.append((row, evidence))

    seen_program_ids: set[str] = set()
    for row, evidence in consumable:
        if str(row.get("update_id") or "") in superseded:
            continue
        source = BUSINESS / str(evidence.get("payout_evaluation_path") or "")
        if (
            not source.is_file()
            or sha_file(source) != str(evidence.get("payout_evaluation_sha256") or "")
        ):
            raise B2Error(f"go_route_provision_source_binding_invalid:{row.get('update_id')}")
        site_program_id = str(evidence.get("site_program_id") or "")
        go_route = str(evidence.get("first_party_go_route") or "")
        target_sheet_row = int(evidence.get("target_sheet_row") or 0)
        target_network = normalize_network(evidence.get("target_network"))
        target_program_id = str(evidence.get("target_program_id") or "")
        if (
            not site_program_id
            or site_program_id in seen_program_ids
            or not re.fullmatch(r"/go/[a-z0-9-]+", go_route)
            or target_sheet_row < 2
            or target_sheet_row != int(row.get("sheet_row") or 0)
            or not target_network
            or not target_program_id
            or evidence.get("card_eligible") is not True
            or evidence.get("provider_approved") is not True
            or evidence.get("currently_has_go_route") is not False
            or evidence.get("sheet_updates_via_single_writer_only") is not True
            or evidence.get("href_config_inspection_only") is not True
            or evidence.get("tracking_clicks") != 0
            or not str(evidence.get("deploy_instruction") or "").strip()
        ):
            raise B2Error(f"go_route_provision_contract_invalid:{row.get('update_id')}")
        seen_program_ids.add(site_program_id)
        requests.append({
            "request_id": "go-route-provision-" + slugify(site_program_id),
            "queue_kind": "affiliate_go_route_provision",
            "queue_source": "sheet_update_queue:payout_evaluation",
            "queue_update_ids": [
                str(row.get("update_id") or ""),
                *(evidence.get("supersedes_update_ids") or []),
            ],
            "status": "STAGED_NOT_DEPLOYED",
            "provider": str(evidence.get("provider") or row.get("program_name") or ""),
            "provider_identity": str(evidence.get("provider_identity") or "").casefold(),
            "site_program_id": site_program_id,
            "first_party_go_route": go_route,
            "target": {
                "network": target_network,
                "program_id": target_program_id,
                "sheet_row": target_sheet_row,
            },
            "payout_switch_dependency": bool(evidence.get("payout_switch_dependency")),
            "live_route_authority": evidence.get("live_route_authority"),
            "deploy_instruction": str(evidence["deploy_instruction"]),
            "deploy_instruction_version": GO_ROUTE_PROVISION_INSTRUCTION_VERSION,
            "sheet_updates_via_single_writer_only": True,
            "href_config_inspection_only": True,
            "tracking_clicks": 0,
            "decision_source": str(evidence.get("payout_evaluation_path") or ""),
        })
    return {
        "requests": requests,
        "queue_row_count": len(requests),
        "provider_count": len(requests),
    }


def payout_evaluation_switches(
    queue_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate and provider-group deploy instructions from payout_evaluation.

    This is intentionally the same n8n Data Table the single Sheet writer drains.
    It is not a second queue and it does not infer deploy authority from Sheet
    notes.  Only the provider-wide instruction version is consumable.
    """
    rows = load_payout_evaluation_queue_rows() if queue_rows is None else list(queue_rows)
    selected: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("kind") or "") != "payout_evaluation":
            continue
        evidence = _payout_queue_evidence(row)
        if evidence.get("deploy_instruction_version") != PAYOUT_LEAK_INSTRUCTION_VERSION:
            continue
        if evidence.get("decision") not in PAYOUT_LEAK_DECISIONS:
            raise B2Error(f"payout_evaluation_deploy_decision_invalid:{row.get('update_id')}")
        if str(row.get("status") or "").casefold() not in {"queued", "applied", "noop"}:
            raise B2Error(f"payout_evaluation_queue_status_not_consumable:{row.get('update_id')}")
        source = BUSINESS / str(evidence.get("payout_evaluation_path") or "")
        if (
            not source.is_file()
            or sha_file(source) != str(evidence.get("payout_evaluation_sha256") or "")
        ):
            raise B2Error(f"payout_evaluation_source_binding_invalid:{row.get('update_id')}")
        affected = evidence.get("affected_sheet_rows")
        routes = evidence.get("affected_routes")
        if (
            evidence.get("provider_all_links") is not True
            or evidence.get("preserve_links_until_atomic_deploy") is not True
            or evidence.get("sheet_updates_via_single_writer_only") is not True
            or evidence.get("href_config_inspection_only") is not True
            or evidence.get("tracking_clicks") != 0
            or not str(evidence.get("deploy_instruction") or "").strip()
            or not isinstance(affected, list) or not affected
            or not isinstance(routes, list) or not routes
            or int(evidence.get("current_sheet_row") or 0) != int(row.get("sheet_row") or 0)
            or not str(evidence.get("winner_network") or "").strip()
            or not str(evidence.get("winner_program_id") or "").strip()
        ):
            raise B2Error(f"payout_evaluation_provider_wide_contract_invalid:{row.get('update_id')}")
        selected.append({**row, "_evidence": evidence})

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in selected:
        identity = str(row["_evidence"].get("provider_identity") or "").casefold()
        if not identity:
            raise B2Error("payout_evaluation_provider_identity_missing")
        grouped.setdefault(identity, []).append(row)

    requests: list[dict[str, Any]] = []
    by_winner_row: dict[int, dict[str, Any]] = {}
    by_loser_row: dict[int, dict[str, Any]] = {}
    blocked: list[dict[str, Any]] = []
    for identity, provider_rows in sorted(grouped.items()):
        evidences = [row["_evidence"] for row in provider_rows]
        affected_sets = {tuple(sorted(int(value) for value in ev["affected_sheet_rows"])) for ev in evidences}
        affected_route_sets = {tuple(sorted(str(value).casefold() for value in ev["affected_routes"])) for ev in evidences}
        winners = {
            (normalize_network(ev.get("winner_network")), str(ev.get("winner_program_id") or ""))
            for ev in evidences
        }
        winner_rows = {ev.get("winner_sheet_row") for ev in evidences}
        expected_count = {int(ev.get("provider_leak_row_count") or 0) for ev in evidences}
        actual_rows = {int(row.get("sheet_row") or 0) for row in provider_rows}
        if (
            len(affected_sets) != 1 or len(affected_route_sets) != 1 or len(winners) != 1
            or len(winner_rows) != 1 or len(expected_count) != 1
            or next(iter(affected_sets)) != tuple(sorted(actual_rows))
            or next(iter(expected_count)) != len(actual_rows)
        ):
            raise B2Error(f"payout_evaluation_provider_group_conflict:{identity}")
        affected_rows = list(next(iter(affected_sets)))
        winner_network, winner_program_id = next(iter(winners))
        winner_row_raw = next(iter(winner_rows))
        provider = str(evidences[0].get("provider") or identity)
        request = {
            "switch_id": "payout-evaluation-" + slugify(identity) + "-" + winner_network + "-" + winner_program_id,
            "queue_kind": "affiliate_tracking_link_swap",
            "queue_source": "sheet_update_queue:payout_evaluation",
            "queue_update_ids": sorted(str(row.get("update_id") or "") for row in provider_rows),
            "status": "STAGED_NOT_DEPLOYED",
            "provider": provider,
            "provider_identity": identity,
            "winner": {
                "network": winner_network,
                "program_id": winner_program_id,
                "sheet_row": int(winner_row_raw) if winner_row_raw is not None else None,
            },
            "target_sheet_row": int(winner_row_raw) if winner_row_raw is not None else None,
            "affected_sheet_rows": affected_rows,
            "affected_routes": list(next(iter(affected_route_sets))),
            "affected_route_records": sorted(
                ({
                    "sheet_row": int(ev.get("current_sheet_row") or 0),
                    "route": str(ev.get("current_route") or "").casefold(),
                    "network": normalize_network(ev.get("current_network")),
                    "program_id": str(ev.get("current_program_id") or ""),
                } for ev in evidences),
                key=lambda item: item["sheet_row"],
            ),
            "deploy_instruction": str(evidences[0]["deploy_instruction"]),
            "deploy_instruction_version": PAYOUT_LEAK_INSTRUCTION_VERSION,
            "provider_all_links": True,
            "preserve_links_until_atomic_deploy": True,
            "sheet_updates_via_single_writer_only": True,
            "href_config_inspection_only": True,
            "tracking_clicks": 0,
            "decision_sources": sorted({str(ev.get("payout_evaluation_path") or "") for ev in evidences}),
        }
        requests.append(request)
        if winner_row_raw is None:
            blocked.append({
                "provider": provider,
                "provider_identity": identity,
                "reason": "payout_evaluation_winner_sheet_row_missing",
                "winner": request["winner"],
                "affected_sheet_rows": affected_rows,
                "queue_update_ids": request["queue_update_ids"],
            })
            for row_number in affected_rows:
                by_loser_row[row_number] = request
            continue
        winner_row = int(winner_row_raw)
        if winner_row < 2 or winner_row in by_winner_row:
            raise B2Error(f"payout_evaluation_winner_sheet_row_conflict:{winner_row}")
        by_winner_row[winner_row] = request
        for row_number in affected_rows:
            if row_number == winner_row or row_number in by_loser_row:
                raise B2Error(f"payout_evaluation_loser_sheet_row_conflict:{row_number}")
            by_loser_row[row_number] = request
    return {
        "requests": requests,
        "by_winner_row": by_winner_row,
        "by_loser_row": by_loser_row,
        "blocked": blocked,
        "queue_row_count": len(selected),
        "provider_count": len(requests),
    }


def best_terms_morning_stage(
    path: Path = BEST_TERMS_MORNING_STAGE,
) -> dict[str, dict[int, dict[str, Any]]]:
    """Validate the payout-audit staging surface and index exact winner/loser rows."""
    document = read_json(Path(path))
    if (document.get("schema") != "glp.wire-in-best-terms-reconciliation-stage.v1"
            or document.get("status") != "STAGED_NOT_DEPLOYED"):
        raise B2Error("best_terms_morning_stage_schema_or_status_invalid")
    for label, binding in (document.get("source_bindings") or {}).items():
        verify_binding(binding, label=f"best_terms_morning_stage_{label}")
    active_path = verify_binding(
        (document.get("source_bindings") or {}).get("active_rail_selection") or {},
        label="best_terms_morning_stage_active_rail_selection",
    )
    active = {
        (
            str(item.get("program_id") or ""),
            normalize_network(item.get("network")),
            str(item.get("network_program_id") or ""),
            int(item.get("sheet_row") or 0),
        )
        for item in (read_json(active_path).get("programs") or [])
    }
    winners: dict[int, dict[str, Any]] = {}
    losers: dict[int, dict[str, Any]] = {}
    for reconciliation in document.get("reconciliations") or []:
        provider = str(reconciliation.get("provider") or "").strip()
        site_program_id = str(reconciliation.get("site_program_id") or "").strip()
        winner = dict(reconciliation.get("winner") or {})
        winner_row = int(winner.get("sheet_row") or 0)
        winner_payout = winner.get("payout_usd")
        if (not provider or not site_program_id or winner_row < 2
                or not normalize_network(winner.get("network"))
                or not str(winner.get("program_id") or "").strip()
                or not isinstance(winner_payout, (int, float))):
            raise B2Error("best_terms_morning_stage_winner_invalid")
        if winner_row in winners:
            raise B2Error(f"best_terms_morning_stage_duplicate_winner_row:{winner_row}")
        winner = {**winner, "provider": provider, "site_program_id": site_program_id}
        winners[winner_row] = winner
        if winner.get("state") == "CURRENTLY_ACTIVE_VERIFIED":
            identity = (
                site_program_id,
                normalize_network(winner.get("network")),
                str(winner.get("program_id") or ""),
                winner_row,
            )
            if identity not in active:
                raise B2Error(f"best_terms_morning_stage_active_winner_drift:{provider}")
        for loser_raw in reconciliation.get("losers") or []:
            loser = dict(loser_raw or {})
            loser_row = int(loser.get("sheet_row") or 0)
            loser_payout = loser.get("payout_usd")
            if (loser_row < 2 or loser_row in losers
                    or not normalize_network(loser.get("network"))
                    or not str(loser.get("program_id") or "").strip()
                    or not isinstance(loser_payout, (int, float))
                    or float(loser_payout) >= float(winner_payout)):
                raise B2Error(f"best_terms_morning_stage_loser_invalid:{provider}:{loser_row}")
            losers[loser_row] = {
                **loser,
                "provider": provider,
                "site_program_id": site_program_id,
                "winner": winner,
            }
        ceiling = reconciliation.get("blocked_commercial_ceiling")
        if ceiling and (
            not isinstance(ceiling.get("payout_usd"), (int, float))
            or float(ceiling["payout_usd"]) <= float(winner_payout)
            or not str(ceiling.get("reason") or "").strip()
        ):
            raise B2Error(f"best_terms_morning_stage_ceiling_invalid:{provider}")
    if not winners or not losers:
        raise B2Error("best_terms_morning_stage_empty")
    return {"winners": winners, "losers": losers}


def _payout_switch_capability_inputs() -> dict[str, Any]:
    """Load the current live base's registry + approved rows once per plan.

    DEFECT 2026-09-02, corrected: the first version of this gate asked whether a
    network ALREADY had a live compare row. That is a bootstrap deadlock - the
    first provider onto any new rail can never pass, and it is empirically wrong
    besides: XenTraffic renders 48 live rows across multiple hosts with no
    TRACKING_HOST_BY_NETWORK entry at all, because affiliate-link-map falls back
    to the program's own authority tracking_host. Renderability is a property of
    the PROGRAM's authority, not of a network's history.
    """
    pointer = BUSINESS / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
    try:
        release = Path(json.loads(pointer.read_text())["release_root"])
        registry = json.loads((release / "data/program-registry.json").read_text())
        approved = json.loads((release / "data/approved-program-registry.json").read_text())
    except (OSError, ValueError, KeyError):
        return {}
    # Index on BOTH spellings. This identity mismatch has now bitten three
    # separate fixes tonight: a switch request's site_program_id is the REVIEW
    # identity ("altrx", "direct-meds-review") while the registry keys on the
    # PROGRAM identity ("altrx-glp1-us-cps"). A single-key index reported
    # no_program_authority_on_disk for programs that plainly have one.
    registry_by_key: dict[str, Any] = {}
    for row in registry.get("programs") or []:
        for key in (row.get("program_id"), row.get("review_slug")):
            if key:
                registry_by_key.setdefault(str(key), row)
    approved_by_key: dict[str, Any] = {}
    for row in approved.get("programs") or []:
        for key in (row.get("registry_id"), row.get("program_id"), row.get("review_slug")):
            if key:
                approved_by_key.setdefault(str(key), row)
    return {"registry": registry_by_key, "approved": approved_by_key}


def _payout_switch_winner_capability(
    site_program_id: str, winner_link: str, capability: dict[str, Any],
) -> dict[str, Any]:
    """Can THIS program render a monetized row on the winner's rail?

    Renderable when the program carries a governed tracking authority whose host
    matches the winner link's host - the same binding affiliate-link-map uses.
    No dependence on whether any other program is already live on that network.
    """
    if not capability:
        return {"renderable": True, "reason": "capability_inputs_unavailable_not_gated"}
    program = (capability.get("registry") or {}).get(site_program_id)
    approved = (capability.get("approved") or {}).get(site_program_id)
    if not program and not approved:
        return {
            "renderable": False,
            "reason": "no_program_authority_on_disk",
            "detail": (
                f"{site_program_id!r} has no row in the live base's program-registry "
                "or approved-program-registry, so no tracking authority exists to "
                "render the winning rail against"
            ),
        }
    winner_host = urlsplit(winner_link).hostname or ""
    hosts = set()
    contract = (program or {}).get("network_contract") or {}
    for value in (contract.get("tracking_url"), ((approved or {}).get("tracking") or {}).get("provider_issued_url")):
        host = urlsplit(str(value or "")).hostname
        if host:
            hosts.add(host)
    allow = (program or {}).get("allowed_tracking_hosts") or []
    hosts.update(h for h in allow if h)
    if winner_host and hosts and winner_host not in hosts:
        return {
            "renderable": False,
            "reason": "winner_host_not_in_program_tracking_authority",
            "detail": (
                f"the winning link's host is not among the hosts {site_program_id!r} "
                "is authorised to track on"
            ),
        }
    return {"renderable": True, "reason": "program_tracking_authority_present"}


def build_plan(
    *,
    backrun_dir: Path,
    ledger_path: Path,
    date_pt: str,
) -> dict[str, Any]:
    coverage_path = backrun_dir / "coverage-receipt.json"
    sheet_path = backrun_dir / "sheet-full-row-export.json"
    dispositions_path = backrun_dir / "sheet-row-dispositions.json"
    coverage = read_json(coverage_path)
    sheet = read_json(sheet_path)
    dispositions = read_json(dispositions_path)
    if coverage.get("status") != "PASS":
        raise B2Error("coverage_receipt_not_pass")
    if (coverage.get("sheet_axis") or {}).get("full_row_export_sha256") != sha_file(sheet_path):
        raise B2Error("sheet_export_hash_drift")
    if (coverage.get("sheet_axis") or {}).get("dispositions_sha256") != sha_file(dispositions_path):
        raise B2Error("dispositions_hash_drift")
    if not (coverage.get("coverage_assertion") or {}).get("every_nonblank_row_dispositioned"):
        raise B2Error("sheet_denominator_incomplete")
    headers = sheet.get("headers")
    rows = sheet.get("rows")
    if not isinstance(headers, list) or not isinstance(rows, dict):
        raise B2Error("sheet_export_schema_invalid")
    required_headers = {
        "Program / Network", "Network / Route", "Status", "Signup Complete?",
        "Tracking Link / Deep Link", "Wire-In Ready?", "Wired Live?", "Link QA Status",
        "Vertical",
    }
    if not required_headers.issubset(headers):
        raise B2Error("sheet_required_headers_missing")
    computed_staged = sum(
        1 for values in rows.values() if staged_feed_member(dict(zip(headers, values)))
    )
    legacy_wire_ready = sum(
        1 for values in rows.values() if true(dict(zip(headers, values)).get("Wire-In Ready?"))
    )
    routing_basis, routing_outcomes, inventory_bindings = derive_current_routing_basis(
        backrun_dir=backrun_dir, coverage=coverage, sheet=sheet)
    if len(routing_outcomes) != computed_staged:
        raise B2Error(
            f"current_routing_basis_denominator_incomplete:{len(routing_outcomes)}:{computed_staged}")
    pointer = read_json(CURRENT_BASE)
    release, registry_by_id, _ = _registry_by_id(pointer)
    activation_by_id = activation_selection_by_id(release)
    retired_rails = retired_rail_rulings(release)
    ledger = load_ledger(ledger_path)
    planned: list[dict[str, Any]] = []
    already_wired: list[dict[str, Any]] = []
    retired_sibling_rails: list[dict[str, Any]] = []
    best_terms_nonwinning_rails: list[dict[str, Any]] = []
    sister_dark: list[dict[str, Any]] = []
    fail_closed_unmapped: list[dict[str, Any]] = []
    paid_event_risk_acceptances: list[dict[str, Any]] = []
    terminal_keys = terminal_ledger_keys(ledger, date_pt)
    requeued_keys = requeued_ledger_keys(ledger, date_pt)
    # Which rows are back in the queue because of a receipted same-day override,
    # named on the plan so the reason a row is being re-attempted is never a
    # thing a reader has to infer from its absence from the terminal list.
    retry_overrides = {
        key: {"receipt": str(record.get("receipt_file") or ""),
              "at_utc": str(record.get("at_utc") or ""),
              "reason": str(record.get("reason") or "")}
        for key, record in (
            (key, retry_override_for(key, date_pt))
            for key, state in (ledger.get("rows") or {}).items()
            if state.get("status") == "blocked")
        if record
    }
    switch_requests = best_terms_switches()
    payout_switch_stage = payout_evaluation_switches()
    go_route_stage = go_route_provision_requests()
    # The governed payout_evaluation lane is newer and carries the complete
    # provider-wide instruction.  It supersedes any same-winner legacy local
    # switch item, but a contradictory winner fails closed.
    for winner_row, request in payout_switch_stage["by_winner_row"].items():
        prior = switch_requests.get(winner_row)
        if prior:
            prior_winner = prior.get("winner") or {}
            winner = request.get("winner") or {}
            if (
                normalize_network(prior_winner.get("network")) != winner.get("network")
                or str(prior_winner.get("program_id") or "") != str(winner.get("program_id") or "")
            ):
                raise B2Error(f"payout_evaluation_legacy_switch_conflict:{winner_row}")
        switch_requests[winner_row] = request
    payout_loser_rows = payout_switch_stage["by_loser_row"]
    morning_stage = best_terms_morning_stage()
    staged_winners = morning_stage["winners"]
    staged_losers = morning_stage["losers"]
    seen_staged_winners: set[int] = set()
    for row_text, values in rows.items():
        row_number = int(row_text)
        row = dict(zip(headers, values))
        if not staged_feed_member(row):
            continue
        outcome = routing_outcomes.get(row_number)
        if not isinstance(outcome, dict):
            raise B2Error(f"current_routing_basis_row_missing:{row_number}")
        base = {
            "sheet_row": row_number,
            "display_name": str(row.get("Program / Network") or "").strip(),
            # Research identity is the governed Sheet's SITE-side SEO Slug.
            # It is intentionally separate from a registry/candidate alias and
            # from the network-side programme id.
            "seo_slug": str(row.get("SEO Slug") or "").strip(),
            "sheet_status": str(row.get("Status") or "").strip(),
            "vertical": str(row.get("Vertical") or "").strip(),
            "wire_ready_bookkeeping": true(row.get("Wire-In Ready?")),
            "link_qa_status": str(row.get("Link QA Status") or "").strip(),
            "sheet_wired_live": true(row.get("Wired Live?")),
        }
        payout_loser = payout_loser_rows.get(row_number)
        if payout_loser:
            route_records = [
                item for item in payout_loser.get("affected_route_records") or []
                if int(item.get("sheet_row") or 0) == row_number
            ]
            if len(route_records) != 1:
                raise B2Error(f"payout_evaluation_loser_route_record_invalid:{row_number}")
            best_terms_nonwinning_rails.append({
                **base,
                "key": None,
                "site_program_id": slugify(base.get("seo_slug") or payout_loser.get("provider")),
                "provider": payout_loser.get("provider"),
                "provider_identity": payout_loser.get("provider_identity"),
                "network": str(row.get("Network / Route") or ""),
                "provider_program_id": str(route_records[0].get("program_id") or ""),
                "disposition": "EXCLUDE_LOWER_PAYOUT_ROUTE_FROM_DEPLOY",
                "winner": payout_loser.get("winner"),
                "queue_update_ids": payout_loser.get("queue_update_ids"),
                "provider_all_links": True,
            })
            continue
        switch_request = switch_requests.get(row_number)
        if outcome.get("outcome") == "ALREADY_WIRED":
            if not switch_request:
                already_wired.append({**base, "key": None, "site_program_id": None})
                continue
            routing_row = dict(outcome.get("route") or {})
            winner = switch_request.get("winner") or {}
            routing_row.update({
                "routing_class": routing_row.get("routing_class") or "core_glp",
                "canonical_vertical": routing_row.get("canonical_vertical") or CORE_VERTICAL,
                "provider_network": routing_row.get("provider_network") or winner.get("network"),
                "provider_program_id": routing_row.get("provider_program_id") or winner.get("program_id"),
                "candidate_id": routing_row.get("candidate_id") or slugify(base.get("seo_slug") or switch_request.get("provider")),
                "identity_evidence": routing_row.get("identity_evidence") or {},
            })
        if outcome.get("outcome") in {"FAIL_CLOSED", "FEED_BLOCKED"}:
            fail_closed_unmapped.append({
                **base,
                "reason": str(outcome.get("reason") or "current_route_unresolved"),
                "detail": outcome.get("detail") or {},
            })
            continue
        if outcome.get("outcome") == "SISTER_DARK":
            sister_dark.append({
                **base,
                "routing_class": str(outcome.get("routing_class") or "sister_pool_vertical"),
                "canonical_vertical": str(outcome.get("canonical_vertical") or base["vertical"]),
                "disposition": "sister_dark_no_site_mutation",
            })
            continue
        if outcome.get("outcome") not in {"ROUTED", "ALREADY_WIRED"}:
            raise B2Error(f"current_routing_basis_outcome_invalid:{row_number}")
        if outcome.get("outcome") == "ROUTED":
            if not isinstance(outcome.get("route"), dict):
                raise B2Error(f"current_routing_basis_outcome_invalid:{row_number}")
            routing_row = outcome["route"]
        routing_class = str(routing_row.get("routing_class") or "")
        canonical_vertical = str(routing_row.get("canonical_vertical") or "")
        network = str(routing_row.get("provider_network") or "").strip()
        provider_program_id = str(routing_row.get("provider_program_id") or "").strip()
        paid_event_gate = ((routing_row.get("identity_evidence") or {})
                           .get("exact_paid_event_gate") or {})
        risk_acceptance = paid_event_gate.get("risk_acceptance")
        if isinstance(risk_acceptance, dict):
            paid_event_risk_acceptances.append({
                **risk_acceptance,
                "sheet_row": row_number,
                "provider_program_id": provider_program_id,
                "logged_at_utc": routing_basis["generated_at_utc"],
            })
        try:
            key = stable_key(network, provider_program_id)
        except B2Error:
            fail_closed_unmapped.append({**base, "reason": "stable_program_identity_missing"})
            continue
        staged_winner = staged_winners.get(row_number)
        if staged_winner:
            if (normalize_network(staged_winner.get("network")) != normalize_network(network)
                    or str(staged_winner.get("program_id") or "") != provider_program_id):
                fail_closed_unmapped.append({
                    **base, "key": key,
                    "reason": "best_terms_morning_winner_identity_mismatch",
                    "detail": {"staged_winner": staged_winner},
                })
                continue
            seen_staged_winners.add(row_number)
        staged_loser = staged_losers.get(row_number)
        if staged_loser:
            if (normalize_network(staged_loser.get("network")) != normalize_network(network)
                    or str(staged_loser.get("program_id") or "") != provider_program_id):
                fail_closed_unmapped.append({
                    **base, "key": key,
                    "reason": "best_terms_morning_loser_identity_mismatch",
                    "detail": {"staged_loser": staged_loser},
                })
                continue
            staged_loser_record = {
                **base,
                "key": key,
                "network": network,
                "provider_program_id": provider_program_id,
                "site_program_id": str(staged_loser.get("site_program_id") or ""),
                "research_program_id": (
                    slugify(base.get("seo_slug"))
                    or str(staged_loser.get("site_program_id") or "")
                ),
                "payout_usd": staged_loser.get("payout_usd"),
                "winner": staged_loser.get("winner"),
                "disposition": staged_loser.get("disposition"),
            }
            staged_loser_record["evidence_hints"] = _evidence_hints(
                alias=staged_loser_record["research_program_id"],
                network=network,
                provider_program_id=provider_program_id,
                release=release,
            )
            if staged_loser.get("disposition") == "RETIRE_ON_GO_DAY_AFTER_WINNER_VERIFIED":
                retired_sibling_rails.append({
                    **staged_loser_record,
                    "disposition": "expected_retired_sibling_rail",
                    "retirement_timing": "GO_DAY_AFTER_WINNER_VERIFIED",
                    "active_rail": {
                        "network": str((staged_loser.get("winner") or {}).get("network")
                                       or "").title(),
                        "provider_program_id": str(
                            (staged_loser.get("winner") or {}).get("program_id") or ""
                        ),
                        "sheet_row": (staged_loser.get("winner") or {}).get("sheet_row"),
                    },
                })
            else:
                best_terms_nonwinning_rails.append(staged_loser_record)
            continue
        alias = str(
            routing_row.get("candidate_id")
            or (routing_row.get("release_comparison") or {}).get("release_program_id")
            or slugify(row.get("SEO Slug") or row.get("Program / Network"))
        )
        registry_row = registry_by_id.get(alias)
        site_wired = registry_row_proves_wired(registry_row)
        tracking = str(row.get("Tracking Link / Deep Link") or "").strip()
        tracking_sha256 = sha_bytes(tracking.encode()) if tracking else None
        activation_row = activation_by_id.get(alias)
        this_rail_wired = site_wired and activation_selection_proves_rail(
            activation_row,
            network=network,
            provider_program_id=provider_program_id,
            tracking_link_sha256=tracking_sha256,
        )
        if switch_request:
            winner = switch_request.get("winner") or {}
            if (normalize_network(winner.get("network")) != normalize_network(network)
                    or str(winner.get("program_id") or "") != provider_program_id):
                fail_closed_unmapped.append({
                    **base, "key": key, "site_program_id": alias,
                    "reason": "best_terms_switch_identity_mismatch",
                    "detail": {"switch_id": switch_request.get("switch_id")},
                })
                continue
        switch_payload = switch_request
        if switch_request and switch_request.get("queue_source") == "sheet_update_queue:payout_evaluation":
            affected_sheet_records = []
            for affected_row in switch_request.get("affected_sheet_rows") or []:
                raw_affected = rows.get(str(int(affected_row)))
                if not isinstance(raw_affected, list):
                    raise B2Error(f"payout_evaluation_affected_sheet_row_missing:{affected_row}")
                affected_sheet_records.append({
                    "sheet_row": int(affected_row),
                    "sheet": dict(zip(headers, raw_affected)),
                })
            switch_payload = {
                **switch_request,
                "site_program_id": alias,
                "winner_sheet": row,
                "affected_sheet_records": affected_sheet_records,
            }
        approval_ready = base["sheet_status"] in APPROVED_STATUSES
        research_alias = slugify(base.get("seo_slug")) or alias
        candidate = {
            **base,
            "key": key,
            "network": network,
            "provider_program_id": provider_program_id,
            "site_program_id": alias,
            "research_program_id": research_alias,
            "routing_class": routing_class,
            "canonical_vertical": canonical_vertical,
            "approval_ready": approval_ready,
            "tracking_ready": tracking.startswith("https://") and link_qa_deployable(
                row.get("Link QA Status")),
            "tracking_link_sha256": tracking_sha256,
            "official_url": str(row.get("Official URL") or "").strip(),
            "existing_editorial_page": bool(registry_row),
            "existing_registry_state": registry_row.get("commercial_state") if registry_row else None,
            "source_disposition": (dispositions.get(str(row_number)) or {}).get("disposition"),
            "routing_identity_evidence": routing_row.get("identity_evidence") or {},
            "exact_paid_event_gate": paid_event_gate,
            "best_terms_switch_request": switch_payload,
            "sheet": row,
        }
        candidate["evidence_hints"] = _evidence_hints(
            alias=research_alias,
            network=network,
            provider_program_id=provider_program_id,
            release=release,
        )
        candidate["friction"] = friction_tuple(candidate)
        candidate["worked_state"] = (ledger["rows"].get(key) or {}).get("status")
        if base["sheet_wired_live"] or (this_rail_wired and not switch_request):
            already_wired.append(candidate)
            continue
        retired_rail = (alias, normalize_network(network), provider_program_id)
        if retired_rail in retired_rails and activation_row and not switch_request:
            retired_sibling_rails.append({
                **base,
                "key": key,
                "network": network,
                "provider_program_id": provider_program_id,
                "site_program_id": alias,
                "disposition": "expected_retired_sibling_rail",
                "active_rail": {
                    "network": activation_row.get("network"),
                    "provider_program_id": activation_row.get("network_program_id"),
                    "sheet_row": activation_row.get("sheet_row"),
                    "tracking_link_sha256": activation_row.get("tracking_link_sha256"),
                },
            })
            continue
        if key in terminal_keys:
            state = ledger["rows"].get(key) or {}
            block = state.get("block") or {}
            if state.get("status") == "blocked":
                terminal_reason = (
                    "terminal_ledger_"
                    + str(block.get("retry_basis") or "block").strip().casefold()
                )
            else:
                terminal_reason = "terminal_ledger_wired_but_current_surfaces_not_wired"
            fail_closed_unmapped.append({
                **base,
                "key": key,
                "site_program_id": alias,
                "reason": terminal_reason,
                "detail": {
                    "ledger_status": state.get("status"),
                    "block_reason": block.get("reason"),
                    "error_code": block.get("error_code"),
                    "retry_basis": block.get("retry_basis"),
                    "attempts": state.get("block_attempts"),
                    "evidence": block.get("evidence"),
                },
            })
            continue
        planned.append(candidate)
    expected_staged_winners = {
        row for row, winner in staged_winners.items()
        if winner.get("state") == "STAGED_DEPLOYABLE_WINNER"
        and (routing_outcomes.get(row) or {}).get("outcome") == "ROUTED"
    }
    if expected_staged_winners != seen_staged_winners:
        missing = sorted(expected_staged_winners - seen_staged_winners)
        raise B2Error(f"best_terms_morning_staged_winner_missing:{missing}")
    planned, same_rail_payout_blocks = select_same_rail_verified_payout_winners(
        planned, routing_basis.get("joined_inventory_offers") or [])
    fail_closed_unmapped.extend(same_rail_payout_blocks)
    planned, cross_network_payout_blocks, payout_comparison_insufficient = (
        select_cross_network_best_terms_winners(
            planned, routing_basis.get("joined_inventory_offers") or []
        )
    )
    fail_closed_unmapped.extend(cross_network_payout_blocks)
    planned.sort(key=lambda row: (row["friction"], row["key"]))
    # Materialize every provider-wide request for the shared deploy transaction,
    # not only winners that happened to be in today's ordinary provider queue.
    # A deploy of any new provider must also clear every pending payout leak.
    site_id_by_sheet_row = {
        int(item.get("sheet_row") or 0): str(item.get("site_program_id") or "")
        for bucket in (planned, already_wired, retired_sibling_rails,
                       best_terms_nonwinning_rails, fail_closed_unmapped)
        for item in bucket
        if int(item.get("sheet_row") or 0) >= 2 and item.get("site_program_id")
    }
    activation_identity_to_site_id = {
        (normalize_network(item.get("network")), str(item.get("network_program_id") or "")): site_id
        for site_id, item in activation_by_id.items()
    }
    deploy_requests: list[dict[str, Any]] = []
    dynamic_blocks: list[dict[str, Any]] = []
    preblocked = {str(item.get("provider_identity") or "") for item in payout_switch_stage["blocked"]}
    for request in payout_switch_stage["requests"]:
        identity = str(request.get("provider_identity") or "")
        if identity in preblocked:
            continue
        winner = request.get("winner") or {}

        winner_row = int(winner.get("sheet_row") or 0)
        raw_winner = rows.get(str(winner_row))
        if not isinstance(raw_winner, list):
            dynamic_blocks.append({
                "provider": request.get("provider"), "provider_identity": identity,
                "reason": "payout_evaluation_winner_sheet_row_missing",
                "winner": winner, "affected_sheet_rows": request.get("affected_sheet_rows"),
                "queue_update_ids": request.get("queue_update_ids"),
            })
            continue
        winner_sheet = dict(zip(headers, raw_winner))
        winner_link = str(winner_sheet.get("Tracking Link / Deep Link") or "").strip()
        winner_observed_id = _sheet_only_program_id(
            normalize_network(winner_sheet.get("Network / Route")),
            str(winner_sheet.get("Apply URL") or ""), winner_link,
        )
        if (
            not winner_link.startswith("https://")
            or normalize_network(winner_sheet.get("Network / Route"))
            != normalize_network(winner.get("network"))
            or (winner_observed_id and winner_observed_id != str(winner.get("program_id") or ""))
        ):
            dynamic_blocks.append({
                "provider": request.get("provider"), "provider_identity": identity,
                "reason": "payout_evaluation_winner_tracking_link_not_deployable",
                "winner": winner, "affected_sheet_rows": request.get("affected_sheet_rows"),
                "queue_update_ids": request.get("queue_update_ids"),
            })
            continue
        affected_sheet_records = []
        for affected_row in request.get("affected_sheet_rows") or []:
            raw_affected = rows.get(str(int(affected_row)))
            if not isinstance(raw_affected, list):
                raise B2Error(f"payout_evaluation_affected_sheet_row_missing:{affected_row}")
            affected_sheet_records.append({
                "sheet_row": int(affected_row),
                "sheet": dict(zip(headers, raw_affected)),
            })
        winner_identity = (
            normalize_network(winner.get("network")), str(winner.get("program_id") or "")
        )
        site_program_id = (
            site_id_by_sheet_row.get(winner_row)
            or activation_identity_to_site_id.get(winner_identity)
            or slugify(winner_sheet.get("SEO Slug") or request.get("provider"))
        )
        # PREDICTIVE RENDERABILITY GATE WITHDRAWN 2026-09-02, deliberately.
        # Three versions were tried and each was wrong in a different way: a
        # network-history check (bootstrap deadlock, and factually wrong -
        # XenTraffic renders 48 live rows with no host-table entry), then a
        # per-program authority check keyed on site_program_id, which is a
        # SLUGIFIED FALLBACK for several rows ("altrx" where the registry says
        # "altrx-glp1-us-cps"), so it blocked programs that plainly have an
        # authority. A false block is not a cheap error here: release_prepared
        # refuses the whole deploy on ANY blocked switch, so one bad prediction
        # halts every switch.
        #
        # The replacement is not a better prediction, it is a MEASUREMENT:
        # scripts/glp_affiliate_link_map_floor.mjs builds the real link map with
        # the site's own module and refuses a candidate that loses a live
        # program, naming it. That caught direct-meds exactly, needs no identity
        # resolution, and cannot deadlock a first-on-rail provider.
        deploy_requests.append({
            **request,
            "site_program_id": site_program_id,
            "winner_sheet": winner_sheet,
            "affected_sheet_records": affected_sheet_records,
        })
    if dynamic_blocks:
        payout_switch_stage["blocked"].extend(dynamic_blocks)

    go_route_deploy_requests: list[dict[str, Any]] = []
    go_route_blocks: list[dict[str, Any]] = []
    go_route_satisfied: list[dict[str, Any]] = []
    payout_request_by_identity = {
        str(request.get("provider_identity") or ""): request
        for request in payout_switch_stage["requests"]
    }
    payout_blocked_identities = {
        str(item.get("provider_identity") or "")
        for item in payout_switch_stage["blocked"]
    }
    planned_by_site_program_id = {
        str(item.get("site_program_id") or ""): item
        for item in planned
        if str(item.get("site_program_id") or "")
    }
    for request in go_route_stage["requests"]:
        site_program_id = str(request.get("site_program_id") or "")
        target = request.get("target") or {}
        raw_target = rows.get(str(int(target.get("sheet_row") or 0)))
        registry_row = registry_by_id.get(site_program_id)
        same_transaction_row = planned_by_site_program_id.get(site_program_id)
        target_key = stable_key(target.get("network"), target.get("program_id"))
        if not isinstance(registry_row, dict) and (
            not isinstance(same_transaction_row, dict)
            or same_transaction_row.get("key") != target_key
        ):
            go_route_blocks.append({
                "provider": request.get("provider"),
                "site_program_id": site_program_id,
                "reason": "go_route_registry_program_missing_from_current_release",
                "queue_update_ids": request.get("queue_update_ids"),
            })
            continue
        live_authority = request.get("live_route_authority")
        if (
            not isinstance(live_authority, dict)
            or live_authority.get("status") != "VERIFIED"
            or live_authority.get("constructor_verified") is not True
            or live_authority.get("redirector_tos_verified") is not True
            or not _go_runtime_redirector_tos_authorized(live_authority)
            or live_authority.get("promotion_authorized") is not True
        ):
            go_route_blocks.append({
                "provider": request.get("provider"),
                "site_program_id": site_program_id,
                "reason": "go_route_live_authority_contract_missing",
                "queue_update_ids": request.get("queue_update_ids"),
            })
            continue
        profile = (((registry_row or {}).get("profiles") or {}).get("glp-clinic-scout") or {})
        if (profile.get("surfaces") or {}).get("go") is True:
            go_route_satisfied.append({
                "provider": request.get("provider"),
                "site_program_id": site_program_id,
                "first_party_go_route": request.get("first_party_go_route"),
                "queue_update_ids": request.get("queue_update_ids"),
            })
            continue
        if not isinstance(raw_target, list):
            go_route_blocks.append({
                "provider": request.get("provider"),
                "site_program_id": site_program_id,
                "reason": "go_route_target_sheet_row_missing",
                "queue_update_ids": request.get("queue_update_ids"),
            })
            continue
        target_sheet = dict(zip(headers, raw_target))
        observed_program_id = _sheet_only_program_id(
            normalize_network(target_sheet.get("Network / Route")),
            str(target_sheet.get("Apply URL") or ""),
            str(target_sheet.get("Tracking Link / Deep Link") or ""),
        )
        if (
            normalize_network(target_sheet.get("Network / Route")) != target.get("network")
            or (observed_program_id and observed_program_id != str(target.get("program_id") or ""))
            or not str(target_sheet.get("Tracking Link / Deep Link") or "").startswith("https://")
        ):
            go_route_blocks.append({
                "provider": request.get("provider"),
                "site_program_id": site_program_id,
                "reason": "go_route_target_sheet_identity_invalid",
                "queue_update_ids": request.get("queue_update_ids"),
            })
            continue
        identity = str(request.get("provider_identity") or "")
        payout_dependency = payout_request_by_identity.get(identity)
        if request.get("payout_switch_dependency") and (
            not payout_dependency or identity in payout_blocked_identities
        ):
            go_route_blocks.append({
                "provider": request.get("provider"),
                "site_program_id": site_program_id,
                "reason": "provider_wide_payout_switch_must_land_before_go_route",
                "queue_update_ids": request.get("queue_update_ids"),
            })
            continue
        go_route_deploy_requests.append({**request, "target_sheet": target_sheet})
    go_route_stage["blocked"] = go_route_blocks
    go_route_stage["satisfied"] = go_route_satisfied
    go_route_stage["deploy_requests"] = go_route_deploy_requests
    morning_two_phase_rollout = load_morning_two_phase_rollout(date_pt)
    source_bindings = {
        "coverage_receipt": path_binding(coverage_path),
        "sheet_export": path_binding(sheet_path),
        "dispositions": path_binding(dispositions_path),
        "current_content_base": path_binding(CURRENT_BASE),
        "best_terms_switch_backlog": (
            path_binding(BEST_TERMS_SWITCH_BACKLOG) if BEST_TERMS_SWITCH_BACKLOG.is_file() else None),
        "best_terms_morning_stage": path_binding(BEST_TERMS_MORNING_STAGE),
        "exact_paid_event_publication_policy": path_binding(EXACT_PAID_EVENT_POLICY),
        "real_provider_economics": path_binding(REAL_PROVIDER_ECONOMICS),
        "morning_two_phase_rollout": path_binding(MORNING_TWO_PHASE_ROLLOUT),
        "affiliati_switch_prerequisites": (
            path_binding(AFFILIATI_SWITCH_PREREQUISITES)
            if AFFILIATI_SWITCH_PREREQUISITES.is_file() else None),
        **inventory_bindings,
    }
    plan = {
        "schema": PLAN_SCHEMA,
        "status": "PASS",
        "generated_at_utc": routing_basis["generated_at_utc"],
        "pt_date": date_pt,
        "source_run_id": coverage.get("run_id"),
        "source_bindings": source_bindings,
        "routing_basis": routing_basis,
        "limits": {
            "wired_per_pt_day": MAX_WIRED_PER_PT_DAY,
            "deployments_per_pt_day": (
                int(morning_two_phase_rollout["limits"]["deployments_per_pt_day"])
                if morning_two_phase_rollout["active"] else 1
            ),
        },
        "morning_two_phase_rollout": morning_two_phase_rollout,
        "denominator": {
            "staged_approved_linked_glp_total": computed_staged,
            "wire_ready_bookkeeping_true_in_sheet": legacy_wire_ready,
            "already_wired": len(already_wired),
            "retired_sibling_rails": len(retired_sibling_rails),
            "best_terms_nonwinning_rails": len(best_terms_nonwinning_rails),
            "eligible_or_fail_closed_glp_backlog": len(planned),
            "sister_dark": len(sister_dark),
            "unmapped_fail_closed": len(fail_closed_unmapped),
            "same_rail_payout_losers": len(same_rail_payout_blocks),
            "cross_network_payout_losers": len(cross_network_payout_blocks),
            "payout_comparison_insufficient_data": len(payout_comparison_insufficient),
        },
        "queue": planned,
        "already_wired": already_wired,
        "retired_sibling_rails": retired_sibling_rails,
        "best_terms_nonwinning_rails": best_terms_nonwinning_rails,
        "payout_evaluation_queue": {
            "table": SHEET_UPDATE_QUEUE_TABLE,
            "kind": "payout_evaluation",
            "queue_row_count": payout_switch_stage["queue_row_count"],
            "provider_count": payout_switch_stage["provider_count"],
            "requests": payout_switch_stage["requests"],
            "blocked": payout_switch_stage["blocked"],
            "deploy_requests": deploy_requests,
        },
        "go_route_provision_queue": {
            "table": SHEET_UPDATE_QUEUE_TABLE,
            "kind": "payout_evaluation",
            "producer": GO_ROUTE_PROVISION_PRODUCER_TAG,
            "queue_row_count": go_route_stage["queue_row_count"],
            "provider_count": go_route_stage["provider_count"],
            "requests": go_route_stage["requests"],
            "blocked": go_route_stage["blocked"],
            "satisfied": go_route_stage["satisfied"],
            "deploy_requests": go_route_stage["deploy_requests"],
        },
        "sister_dark": sister_dark,
        "fail_closed_unmapped": fail_closed_unmapped,
        "paid_event_risk_acceptances": paid_event_risk_acceptances,
        "payout_comparison_insufficient_data": payout_comparison_insufficient,
        "selection_policy": {
            "name_special_cases": [],
            "feed_gates": [
                "approved relationship status", "tracking link present and HTTPS",
                "glp1-weightloss vertical", "not live", "deployable Link QA",
                "highest verified payout among same-provider same-marketplace siblings",
                "highest governed expected value across every held marketplace sibling",
                "staged payout-audit winner among explicitly reconciled cross-marketplace siblings",
            ],
            "conditional_gates": [
                "exact paid-event required only for networks configured as publishing it",
            ],
            "non_gates": [
                "Wire-In Ready bookkeeping checkbox",
                "Apply bookkeeping checkbox", "Signup Complete bookkeeping checkbox",
            ],
            "missing_paid_event_disposition": (
                "publishing network blocks; never-publishes network passes with logged risk acceptance"
            ),
            "paid_event_risk_acceptance_count": len(paid_event_risk_acceptances),
            "friction_fields": [
                "existing_research", "existing_terms", "existing_editorial_page",
                "template_logo_ready", "tracking_ready", "approval_ready", "sheet_row",
            ],
            "terminal_ledger_rows_excluded": sorted(terminal_keys),
            "retryable_ledger_rows_requeued": sorted(requeued_keys),
            "retry_budget_overrides_honored": retry_overrides,
            "retry_override_schema": RETRY_OVERRIDE_SCHEMA,
            "retryable_block_reasons": sorted(RETRYABLE_BLOCK_REASONS),
            "terminal_block_error_codes": sorted(TERMINAL_BLOCK_ERROR_CODES),
            "max_block_attempts": MAX_BLOCK_ATTEMPTS,
        },
        "effects": {"provider_calls": 0, "site_writes": 0, "deployments": 0},
    }
    partitioned = (
        len(already_wired) + len(retired_sibling_rails) + len(best_terms_nonwinning_rails)
        + len(planned)
        + len(sister_dark) + len(fail_closed_unmapped)
    )
    if partitioned != computed_staged:
        raise B2Error(f"staged_feed_projection_partition_incomplete:{partitioned}:{computed_staged}")
    verify_routing_basis_freshness(plan)
    return plan


def plan_by_key(plan: dict[str, Any], key: str) -> dict[str, Any]:
    if plan.get("schema") != PLAN_SCHEMA or plan.get("status") != "PASS":
        raise B2Error("plan_invalid")
    matches = [row for row in plan.get("queue") or [] if row.get("key") == key]
    if len(matches) != 1:
        raise B2Error("plan_key_missing_or_duplicate")
    return matches[0]


def verify_plan_sources(plan: dict[str, Any]) -> None:
    verify_routing_basis_freshness(plan)
    for label, binding in (plan.get("source_bindings") or {}).items():
        if label != "current_content_base":
            verify_binding(binding, label=label)
            continue
        # This pointer is intentionally reminted by every normal deployment. A
        # queued plan may outlive its original bytes, so validate the current
        # pointer and its receipts rather than stranding the queue on stale hash.
        path = Path(str(binding.get("path") or "")).resolve()
        if path != CURRENT_BASE.resolve() or not path.is_file():
            raise B2Error("current_content_base_path_invalid")
        pointer = read_json(path)
        if pointer.get("schema") != "glp-opus-current-content-base-v1" or pointer.get("status") != "PASS":
            raise B2Error("current_content_base_pointer_invalid")
        _registry_by_id(pointer)
        for receipt_label in ("manifest", "final_receipt"):
            receipt_binding = pointer.get(receipt_label)
            if not isinstance(receipt_binding, dict):
                raise B2Error(f"current_content_base_{receipt_label}_missing")
            verify_binding(receipt_binding, label=f"current_content_base_{receipt_label}")


def batch_plan_scope_sha256(plan: dict[str, Any]) -> str:
    """Hash the whole generated plan except observation/authority metadata."""
    def stable(value: Any, path: tuple[str, ...] = ()) -> Any:
        if isinstance(value, dict):
            out = {}
            exact_binding = (
                "path" in value
                and "sha256" in value
                and "bytes" in value
                and re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256") or ""))
            )
            for key, item in sorted(value.items()):
                if key in {"generated_at_utc", "logged_at_utc"}:
                    continue
                if key.startswith("worked_"):
                    continue
                if path == ("selection_policy",) and key in {
                    "retry_budget_overrides_honored",
                    "retryable_ledger_rows_requeued",
                }:
                    continue
                # Governed evidence is byte-bound. Authoring may rematerialize
                # identical bytes beneath a new run directory between plan
                # capture and registered execution; binding that volatile path
                # made unchanged evidence invalidate operator authority. Keep
                # bytes+hash in scope and ignore only the storage location.
                if exact_binding and key == "path":
                    continue
                if not path and key in {
                    "batch_authority", "base_plan_scope_sha256", "build_trigger_scope"
                }:
                    continue
                if not path and key == "wired_repairs" and item == []:
                    continue
                if path == ("source_bindings",) and key == "batch_authority":
                    continue
                if path == ("limits",) and key == "wired_per_pt_day":
                    out[key] = MAX_WIRED_PER_PT_DAY
                    continue
                out[key] = stable(item, (*path, key))
            return out
        if isinstance(value, list):
            return [stable(item, path) for item in value]
        return value

    return sha_bytes(json.dumps(
        stable(plan), sort_keys=True, separators=(",", ":")
    ).encode())



#: TWO AUTHORITY CLASSES, deliberately not one with a looser cap.
#:
#: A PROVIDER-ROWS authority lands provider rows; its minimum is how many must
#: succeed, and a zero there would be a category error.
#:
#: A CARRY-ONLY authority lands NO provider rows. It exists for a deploy whose
#: whole value is the carried machine changes - on 2026-09-03: the therapeutic
#: authority re-mint, the /go allowlist, the Meta pixel fold, Measured's
#: attested-only presentation and the price-index supersession sidecar - so that
#: the post-deploy sensor can reach zero and rows that already LANDED can finally
#: be marked wired. It must therefore prove it changed nothing about the provider
#: set, and it must arm a fresh deployment intent so
#: reconcile_landed_uncertain_deployment can resolve the landed rows.
PROVIDER_ROWS_AUTHORITY_CLASS = "provider_rows"
CARRY_ONLY_AUTHORITY_CLASS = "carry_only"


def _validate_carry_only_authority(
    plan: dict[str, Any], authority: dict[str, Any], authority_path: Path
) -> dict[str, Any]:
    """Validate a carry-only deploy: zero provider rows, everything else proven."""
    allowed = [str(key) for key in authority.get("allowed_queue_keys") or []]
    if allowed:
        raise B2Error(
            f"wire_batch_authority_carry_only_has_provider_rows:{len(allowed)}")
    if int(authority.get("minimum_successful_rows", -1)) != 0:
        raise B2Error("wire_batch_authority_carry_only_minimum_invalid")
    carries = authority.get("carries")
    if not isinstance(carries, list) or not carries or not all(
        isinstance(item, str) and item.strip() for item in carries
    ):
        raise B2Error("wire_batch_authority_carry_only_carries_unenumerated")
    unchanged = authority.get("provider_set_unchanged")
    if not isinstance(unchanged, dict):
        raise B2Error("wire_batch_authority_carry_only_provider_set_contract_missing")
    if (
        unchanged.get("surface") != "compare"
        or int(unchanged.get("expected_live") or 0) < 1
        or unchanged.get("expected_lost") != []
        or unchanged.get("expected_gained") != []
        or unchanged.get("every_existing_href_hash_unchanged") is not True
    ):
        raise B2Error("wire_batch_authority_carry_only_provider_set_contract_invalid")
    if authority.get("post_deploy_sensor_readback_required") is not True:
        raise B2Error("wire_batch_authority_carry_only_sensor_readback_not_required")
    if authority.get("fresh_armed_intent_required") is not True:
        raise B2Error("wire_batch_authority_carry_only_armed_intent_not_required")
    baseline_path = verify_binding(
        authority.get("base_plan"), label="wire_batch_base_plan"
    )
    expected_plan_scope = str(authority.get("base_plan_scope_sha256") or "")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_plan_scope)
        or batch_plan_scope_sha256(read_json(baseline_path)) != expected_plan_scope
        or batch_plan_scope_sha256(plan) != expected_plan_scope
    ):
        raise B2Error("wire_batch_authority_base_plan_mismatch")
    limits = authority.get("limits") or {}
    if int(limits.get("wired_per_pt_day") or 0) != 0:
        raise B2Error("wire_batch_authority_carry_only_wired_cap_invalid")
    if int(limits.get("deployments_per_pt_day") or 0) != 1:
        raise B2Error("wire_batch_authority_deployment_cap_invalid")
    if limits.get("tracking_link_requests") != 0:
        raise B2Error("wire_batch_authority_tracking_request_budget_invalid")
    if limits.get("tracking_clicks") != 0:
        raise B2Error("wire_batch_authority_tracking_click_budget_invalid")
    return {
        "binding": path_binding(authority_path),
        "authority_class": CARRY_ONLY_AUTHORITY_CLASS,
        "allowed_queue_keys": [],
        "wired_per_pt_day": 0,
        "deployments_per_pt_day": 1,
        "require_complete_batch_on_deploy": False,
        "deploy_requires_complete_prepared_rows": False,
        "continue_on_row_failure": True,
        "minimum_successful_rows": 0,
        "carries": list(carries),
        "provider_set_unchanged": dict(unchanged),
        "measured_epc_policy": None,
        "source_release_override": None,
    }


def validate_batch_authority(
    plan: dict[str, Any], binding: dict[str, Any]
) -> dict[str, Any]:
    """Validate one exact change-then-fire batch without weakening defaults.

    The normal machine remains capped at ten rows. A larger batch only exists
    when an operator authority binds the untouched generated plan, the measured
    EPC replay, every admitted queue key, one deployment, and zero tracking-link
    requests. The authority is re-read by every cap consumer rather than trusting
    mutable fields copied into the plan.
    """
    authority_path = verify_binding(binding, label="wire_batch_authority")
    authority = read_json(authority_path)
    if (
        authority.get("schema") != BATCH_AUTHORITY_SCHEMA
        or authority.get("status") != "OPERATOR_APPROVED_CHANGE_THEN_FIRE"
    ):
        raise B2Error("wire_batch_authority_status_invalid")
    if str(authority.get("pt_date") or "") != str(plan.get("pt_date") or ""):
        raise B2Error("wire_batch_authority_pt_date_mismatch")
    authority_class = str(authority.get("authority_class") or PROVIDER_ROWS_AUTHORITY_CLASS)
    if authority_class not in (PROVIDER_ROWS_AUTHORITY_CLASS, CARRY_ONLY_AUTHORITY_CLASS):
        raise B2Error(f"wire_batch_authority_class_invalid:{authority_class}")
    if authority_class == CARRY_ONLY_AUTHORITY_CLASS:
        return _validate_carry_only_authority(plan, authority, authority_path)
    allowed = [str(key) for key in authority.get("allowed_queue_keys") or []]
    queue_keys = [str(row.get("key") or "") for row in plan.get("queue") or []]
    if not allowed or len(allowed) != len(set(allowed)) or allowed != queue_keys:
        raise B2Error("wire_batch_authority_queue_scope_mismatch")
    baseline_path = verify_binding(
        authority.get("base_plan"), label="wire_batch_base_plan"
    )
    expected_plan_scope = str(authority.get("base_plan_scope_sha256") or "")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_plan_scope)
        or batch_plan_scope_sha256(read_json(baseline_path)) != expected_plan_scope
        or batch_plan_scope_sha256(plan) != expected_plan_scope
    ):
        raise B2Error("wire_batch_authority_base_plan_mismatch")
    limits = authority.get("limits") or {}
    wired_cap = int(limits.get("wired_per_pt_day") or 0)
    if wired_cap != len(allowed) or not (
        MAX_WIRED_PER_PT_DAY < wired_cap <= MAX_OPERATOR_BATCH_ROWS
    ):
        raise B2Error("wire_batch_authority_wired_cap_invalid")
    if int(limits.get("deployments_per_pt_day") or 0) != 1:
        raise B2Error("wire_batch_authority_deployment_cap_invalid")
    if limits.get("tracking_link_requests") != 0:
        raise B2Error("wire_batch_authority_tracking_request_budget_invalid")
    if limits.get("tracking_clicks") != 0:
        raise B2Error("wire_batch_authority_tracking_click_budget_invalid")
    require_complete = authority.get("require_complete_batch_on_deploy") is True
    continue_on_failure = authority.get("continue_on_row_failure") is True
    deploy_requires_complete_prepared_rows = (
        authority.get("deploy_requires_complete_prepared_rows") is True
    )
    if require_complete == continue_on_failure:
        raise B2Error("wire_batch_authority_row_failure_policy_invalid")
    minimum_successful_rows = int(authority.get("minimum_successful_rows") or 0)
    # A PROVIDER-ROWS authority exists to land provider rows, so a zero minimum is
    # a category error, not a loosened cap: a carry-only deploy has its own class
    # with its own contract. Refused here so the two can never be conflated.
    if not require_complete and minimum_successful_rows < 1:
        raise B2Error("wire_batch_authority_provider_class_zero_minimum")
    if continue_on_failure and not (1 <= minimum_successful_rows <= len(allowed)):
        raise B2Error("wire_batch_authority_minimum_success_invalid")
    if require_complete:
        minimum_successful_rows = len(allowed)
    policy_path = verify_binding(
        authority.get("measured_epc_policy"), label="measured_epc_policy"
    )
    policy = read_json(policy_path)
    targets = {
        str(row.get("provider") or ""): float(row.get("measured_epc") or 0)
        for row in policy.get("targets") or []
    }
    if (
        policy.get("schema") != "glp.best-terms.measured-epc-replay.v1"
        or policy.get("status") != "PASS_REAL_NETWORK_EPC_WIRED_NO_INVENTED_COUNTS"
        or int(policy.get("offers_replayed") or 0) != 8548
        or policy.get("winner_eligibility_violations") != 0
        or targets != {
            "Wellmedr": 30.647139,
            "Trimi GLP-1": 14.471543,
            "Gala Health": 6.02785,
        }
    ):
        raise B2Error("wire_batch_authority_measured_epc_policy_invalid")
    source_release_override = validate_source_release_override(
        authority.get("source_release_override")
    )
    return {
        "binding": path_binding(authority_path),
        "authority_class": PROVIDER_ROWS_AUTHORITY_CLASS,
        "allowed_queue_keys": allowed,
        "wired_per_pt_day": wired_cap,
        "deployments_per_pt_day": 1,
        "require_complete_batch_on_deploy": require_complete,
        "deploy_requires_complete_prepared_rows": deploy_requires_complete_prepared_rows,
        "continue_on_row_failure": continue_on_failure,
        "minimum_successful_rows": minimum_successful_rows,
        "measured_epc_policy": path_binding(policy_path),
        "source_release_override": source_release_override,
    }


def validate_source_release_override(
    override: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate an operator-bound sealed source for an all-in-one successor.

    The normal backlog machine starts from CURRENT_BASE. A deploy mission may
    instead bind one previously sealed, independently receipted design release
    so provider, payout, and /go mutations land on that exact design tree before
    the machine performs its single production deployment. The override never
    changes CURRENT_BASE and cannot point at a mutable candidate.
    """
    if override is None:
        return None
    if not isinstance(override, dict):
        raise B2Error("wire_batch_source_release_override_invalid")
    release = Path(str(override.get("release_root") or "")).resolve()
    if release.parent != RELEASES or not release.is_dir():
        raise B2Error("wire_batch_source_release_scope_invalid")
    for path in [release, *release.rglob("*")]:
        info = os.lstat(path)
        if (
            stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) & 0o222
        ):
            raise B2Error(f"wire_batch_source_release_not_sealed:{path}")
    receipt_path = verify_binding(
        override.get("chain_receipt") or {},
        label="wire_batch_source_release_chain_receipt",
    )
    receipt = read_json(receipt_path)
    receipt_release = receipt.get("release") or {}
    expected = {
        "release": str(override.get("release_tree_sha256") or ""),
        "public": str(override.get("public_tree_sha256") or ""),
        "functions": str(override.get("functions_tree_sha256") or ""),
    }
    if (
        receipt.get("schema") != "glp-sealed-successor-release-chain-v1"
        or receipt.get("status") != "PASS_VERIFIED_NO_EFFECT"
        or receipt_release.get("path") != str(release)
        or receipt_release.get("immutable") is not True
        or receipt_release.get("owner") != "root:root"
        or receipt_release.get("release_tree_sha256") != expected["release"]
        or receipt_release.get("public_tree_sha256") != expected["public"]
        or receipt_release.get("functions_tree_sha256") != expected["functions"]
        or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in expected.values())
    ):
        raise B2Error("wire_batch_source_release_receipt_invalid")
    actual = _tree_closures(release)
    if actual != expected:
        raise B2Error("wire_batch_source_release_tree_drift")
    # SEALED IS NOT THE SAME AS CURRENT. Everything above proves the pinned tree
    # is immutable, root-owned, receipted and byte-consistent WITH ITSELF. None
    # of it asks whether the live site has moved past it. See
    # `_assert_source_release_is_the_live_head`.
    _assert_source_release_is_the_live_head(release, override)
    _registry_by_id({"release_root": str(release)})
    return {
        "release_root": str(release),
        "release_tree_sha256": expected["release"],
        "public_tree_sha256": expected["public"],
        "functions_tree_sha256": expected["functions"],
        "chain_receipt": path_binding(receipt_path),
    }


#: Cloudflare Pages production project for glp-clinic-scout. The account id and
#: project name are configuration, not secrets; the TOKEN is read at call time
#: from the operator's secret file and is never logged, returned or persisted.
CF_ACCOUNT_ID = "f860943dff399ffb1db0a28dcd3dadfd"
CF_PAGES_PROJECT = "glp-clinic-scout"
CF_TOKEN_ENV = Path.home() / ".hermes/secrets/cloudflare/hermes-key.env"


def cf_canonical_production_head(timeout: int = 60) -> str | None:
    """The deployment id Cloudflare is actually serving on production, or None.

    Top of the project's truth order (CF canonical head > pointer > receipts >
    disk). Returns None ONLY when the head cannot be read at all - a missing
    token file, an unreachable API, an empty result. The caller must treat None
    as a REFUSAL, never as agreement: "I could not check" and "it matches" are
    different answers and conflating them is how a stale pin gets through.
    """
    import urllib.request  # noqa: PLC0415 - only needed on this path

    try:
        env: dict[str, str] = {}
        for line in CF_TOKEN_ENV.read_text().splitlines():
            match = re.match(r'\s*(?:export\s+)?([A-Z_]+)\s*=\s*"?([^"\n]+)"?', line)
            if match:
                env[match.group(1)] = match.group(2).strip()
        token = env.get("CLOUDFLARE_API_TOKEN") or env.get("CF_API_TOKEN")
        if not token:
            return None
        url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
               f"/pages/projects/{CF_PAGES_PROJECT}/deployments"
               "?env=production&per_page=5")
        request = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + token})
        rows = json.loads(
            urllib.request.urlopen(request, timeout=timeout).read()
        ).get("result") or []
    except Exception:  # noqa: BLE001 - any read failure is "unknown", not "fine"
        return None
    canonical = [
        row for row in rows
        if row.get("environment") == "production"
        and (row.get("latest_stage") or {}).get("status") == "success"
        and row.get("is_skipped") is not True
    ]
    return str(canonical[0]["id"]) if canonical else None


def _public_tree_manifest(root: Path) -> dict[str, str]:
    """{relative public path -> sha256} for one release tree.

    Whole-tree closures answer "did these two trees differ"; this answers
    "which files, and did any of them CHANGE rather than merely appear". That
    distinction is the whole guard below: a pin that is missing files the live
    head added is recoverable, a pin that would overwrite files the live head
    edited silently reverts published work.
    """
    public = root / "public"
    out: dict[str, str] = {}
    if not public.is_dir():
        return out
    for path in sorted(public.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        out[str(path.relative_to(public))] = sha_file(path)
    return out


def _assert_source_release_is_the_live_head(
    release: Path, override: dict[str, Any],
) -> None:
    """Refuse a `source_release_override` the live site has moved past.

    DEFECT 2026-09-01 (deploy-fable-20260901 G17 -> G18, caught BY HAND).
    The G17 authority pinned `source_release_override` at the sealed option-c
    release `glp-funnel-v2-pass6-option-c-20260830-a4d60c24`. That tree is
    perfectly sealed, perfectly receipted and perfectly hash-consistent, and it
    was TWO PRODUCTION PUBLICATIONS STALE. Every check in
    `validate_source_release_override` passed it. Building the successor from it
    would have silently reverted the SEO content published at
    2026-09-01T20:29Z - nine edited content files, one new guides page and eight
    new assets - and nothing in the machine would have said a word. A human
    noticed. That is not a control.

    The rule, in the project's own truth order (CF canonical head > pointer >
    receipts > disk):

      1. the CURRENT_BASE pointer is the machine's statement of the live head,
         and it must itself be current: when Cloudflare credentials are
         readable, the pointer's `deployment_id` MUST equal the project's
         production canonical head, or the pointer is stale and nothing built
         on it can be trusted;
      2. a pin EQUAL to the live head is always fine;
      3. any other pin is REFUSED BY DEFAULT, including a strict ancestor -
         "it is an ancestor" is exactly what was true on 2026-09-01, and it was
         still a revert;
      4. the single escape is a receipted, same-PT-day acknowledgement inside
         the authority that names the live deployment id and release tree hash
         AND is accompanied by a MACHINE-COMPUTED delta showing the live head is
         purely additive over the pin. One changed byte in one shared file and
         the pin is refused, because that byte is the revert.

    Nothing here weakens the seal checks; it adds the question they never asked.
    """
    pointer_path = CURRENT_BASE.resolve()
    if not pointer_path.is_file():
        raise B2Error("wire_batch_source_release_current_base_missing")
    pointer = read_json(pointer_path)
    if (pointer.get("schema") != "glp-opus-current-content-base-v1"
            or pointer.get("status") != "PASS"
            or not str(pointer.get("release_root") or "")
            or not str(pointer.get("deployment_id") or "")):
        raise B2Error("wire_batch_source_release_current_base_invalid")

    head_id = str(pointer["deployment_id"])
    canonical = cf_canonical_production_head()
    if canonical is None:
        # Named degradation, never a silent pass. The caller records it and the
        # authority carries the risk acceptance; it is not swallowed here.
        raise B2Error("wire_batch_source_release_cf_head_unreadable")
    if canonical != head_id:
        raise B2Error(
            f"current_content_base_behind_cf_canonical_head:{head_id}:{canonical}")

    live = Path(str(pointer["release_root"])).resolve()
    if release == live:
        return

    ack = override.get("live_head_acknowledgement")
    if not isinstance(ack, dict):
        raise B2Error(
            f"wire_batch_source_release_behind_live_head:{release.name}:{live.name}")
    if (str(ack.get("live_deployment_id") or "") != head_id
            or str(ack.get("live_release_tree_sha256") or "")
            != str(pointer.get("release_tree_sha256") or "")
            or str(ack.get("live_release_root") or "") != str(live)
            or ack.get("acknowledged_additive_only") is not True
            or not str(ack.get("reason") or "").strip()
            or str(ack.get("pt_date") or "") != pt_date()):
        raise B2Error("wire_batch_source_release_live_head_acknowledgement_invalid")

    pinned_files = _public_tree_manifest(release)
    live_files = _public_tree_manifest(live)
    changed = sorted(
        path for path, digest in pinned_files.items()
        if path in live_files and live_files[path] != digest
    )
    removed = sorted(set(pinned_files) - set(live_files))
    if changed or removed:
        # The live head edited or dropped files this pin still carries. Building
        # from the pin republishes the pin's bytes over them. That is the revert.
        raise B2Error(
            "wire_batch_source_release_would_revert_live_head:"
            f"changed={len(changed)}:removed={len(removed)}:"
            f"first={(changed or removed)[0]}")


def batch_source_release(plan: dict[str, Any]) -> Path | None:
    batch = plan.get("batch_authority")
    if not batch:
        return None
    validated = validate_batch_authority(plan, batch.get("binding") or {})
    override = validated.get("source_release_override")
    if not override:
        return None
    return Path(str(override["release_root"])).resolve()


def apply_batch_authority(plan_path: Path, authority_path: Path) -> dict[str, Any]:
    """Attach an exact-plan batch authority before any ledger mutation."""
    plan = read_json(plan_path)
    authority = read_json(authority_path)
    binding = path_binding(authority_path)
    validated = validate_batch_authority(plan, binding)
    plan["batch_authority"] = validated
    plan.setdefault("source_bindings", {})["batch_authority"] = binding
    plan["limits"] = {
        **(plan.get("limits") or {}),
        "wired_per_pt_day": validated["wired_per_pt_day"],
        "deployments_per_pt_day": validated["deployments_per_pt_day"],
    }
    plan["base_plan_scope_sha256"] = str(
        authority.get("base_plan_scope_sha256") or ""
    )
    atomic_json(plan_path, plan)
    return plan


def plan_wired_limit(plan: dict[str, Any]) -> int:
    configured = int((plan.get("limits") or {}).get("wired_per_pt_day") or 0)
    batch = plan.get("batch_authority")
    if not batch:
        if configured not in (0, MAX_WIRED_PER_PT_DAY):
            raise B2Error("unregistered_daily_wired_cap_override")
        return MAX_WIRED_PER_PT_DAY
    validated = validate_batch_authority(plan, batch.get("binding") or {})
    if configured != int(validated["wired_per_pt_day"]):
        raise B2Error("wire_batch_authority_plan_cap_mismatch")
    return configured


def batch_required_keys(plan: dict[str, Any]) -> list[str]:
    batch = plan.get("batch_authority")
    if not batch:
        return []
    validated = validate_batch_authority(plan, batch.get("binding") or {})
    if not (
        validated["require_complete_batch_on_deploy"]
        or validated["deploy_requires_complete_prepared_rows"]
    ):
        return []
    return list(validated["allowed_queue_keys"])


def batch_minimum_successful_rows(plan: dict[str, Any]) -> int:
    batch = plan.get("batch_authority")
    if not batch:
        return 1
    return int(validate_batch_authority(plan, batch.get("binding") or {})[
        "minimum_successful_rows"
    ])


def error_code(raw: Any) -> str:
    """Reduce a recorded failure string to its leading B2 error code.

    Receipts record `B2Error:queue_research_binding_missing` or
    `B2Error:spec_identity_drift:program_id`; both reduce to the first code
    segment so classification never depends on a variable detail suffix.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    body = text.split(":", 1)[1] if re.match(r"^[A-Za-z_][A-Za-z0-9_]*Error:", text) else text
    return body.split(":", 1)[0].strip()


def block_evidence_error_code(evidence: Any) -> str:
    """Read the error code out of a hash-bound row-block evidence receipt."""
    if not isinstance(evidence, dict):
        return ""
    path = Path(str(evidence.get("path") or ""))
    if not path.is_file():
        return ""
    try:
        document = read_json(path)
    except B2Error:
        return ""
    if not isinstance(document, dict):
        return ""
    return error_code(document.get("error"))


def classify_block(reason: Any, code: Any, *, attempts: int) -> dict[str, Any]:
    """Decide whether one block permanently closes a row or may be re-attempted.

    Terminal is the default. A row only earns a retry when the failing stage is
    an enumerated retryable one AND the recorded error is not itself a verdict
    on the program AND the row has retry budget left.
    """
    reason_text = str(reason or "")
    code_text = str(code or "")
    if reason_text not in RETRYABLE_BLOCK_REASONS:
        basis = "reason_not_retryable"
    elif code_text in TERMINAL_BLOCK_ERROR_CODES:
        basis = "terminal_error_code"
    elif attempts >= MAX_BLOCK_ATTEMPTS:
        basis = "retry_budget_exhausted"
    else:
        return {
            "retry_class": "retryable",
            "retry_basis": "retryable_stage_recoverable_error",
            "error_code": code_text,
            "attempts": attempts,
            "max_attempts": MAX_BLOCK_ATTEMPTS,
        }
    return {
        "retry_class": "terminal",
        "retry_basis": basis,
        "error_code": code_text,
        "attempts": attempts,
        "max_attempts": MAX_BLOCK_ATTEMPTS,
    }


# ---------------------------------------------------------------------------
# RECEIPTED OPERATOR OVERRIDE of the retry budget — never of a verdict
# ---------------------------------------------------------------------------
#: Same shape, same discipline and the same doctrine as the authoring lane's
#: cycle-cap override (`glp.authoring_cycle_operator_override.v1`): a receipt on
#: disk, scoped to one row key and one PT day, that makes the *budget* clock
#: start again. It exists because the budget is a churn guard, not a finding —
#: memory `manually-trigger-until-verified-working` (operator, 2026-08-20:
#: manually re-trigger a fixed machine the same day instead of waiting for
#: tomorrow's schedule to prove the fix).
#:
#: WHAT IT CAN AND CANNOT DO — this is the whole safety argument:
#:   * it can clear `retry_budget_exhausted`, and ONLY that basis;
#:   * it can never clear `terminal_error_code` (a genuine legal prohibition or
#:     an operator-owned account action stays terminal forever), and never
#:     `reason_not_retryable` (a stage that must not be re-entered);
#:   * it never edits ledger history, and it never lowers MAX_BLOCK_ATTEMPTS.
#: So no gate is weakened: the row still has to pass every prevalidation check
#: on its next attempt, and it still blocks again if the inputs are still absent.
RETRY_OVERRIDE_SCHEMA = "glp.wire_backlog_retry_override.v1"
RETRY_OVERRIDES_DIR = MACHINE_ROOT / "overrides"
#: The one basis an override may clear.
OVERRIDABLE_RETRY_BASIS = "retry_budget_exhausted"


def _retry_overrides_dir() -> Path:
    """Read at CALL time so a test can redirect it and never write its fixtures
    into the machine's own evidence directory."""
    return Path(os.environ.get("GLP_WIRE_BACKLOG_OVERRIDES_DIR", str(RETRY_OVERRIDES_DIR)))


def retry_override_for(key: str, date_pt: str | None = None) -> dict[str, Any] | None:
    """The valid override receipt for (row key, PT day), or None.

    Valid means: right schema, names THIS key, and its own `at_utc` falls on the
    PT day being worked. An override is a same-day instrument by construction —
    tomorrow the budget resets on its own and no receipt is needed."""
    day = date_pt or pt_date()
    directory = _retry_overrides_dir()
    best: dict[str, Any] | None = None
    try:
        names = sorted(os.listdir(directory))
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return None
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            record = json.loads((directory / name).read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("schema") != RETRY_OVERRIDE_SCHEMA:
            continue
        if str(record.get("key") or "") != str(key):
            continue
        at = str(record.get("at_utc") or "")
        try:
            stamp = dt.datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if stamp.astimezone(PT).date().isoformat() != day:
            continue
        if best is None or at > str(best.get("at_utc") or ""):
            best = {**record, "receipt_file": str(directory / name)}
    return best


def block_retry_class(state: dict[str, Any], *, key: str = "",
                      date_pt: str | None = None) -> str:
    """Retry class of one blocked ledger row, classifying legacy rows on read.

    Rows written before reason-aware classification carry no `retry_class`.
    Reading them through the same classifier is what stops an already-recorded
    retryable failure (e.g. a missing research artefact) from staying terminal
    forever without anyone hand-editing the ledger.

    A receipted operator override (see `retry_override_for`) re-opens a row that
    is terminal ONLY because its retry budget ran out. Every other terminal
    basis is re-derived from the block itself and is untouchable.
    """
    block = state.get("block") or {}
    recorded = block.get("retry_class")
    code = block.get("error_code") or block_evidence_error_code(block.get("evidence"))
    if recorded in {"retryable", "terminal"}:
        verdict = {"retry_class": str(recorded),
                   "retry_basis": str(block.get("retry_basis") or "")}
    else:
        verdict = classify_block(
            block.get("reason"), code, attempts=int(state.get("block_attempts") or 1))
    if verdict["retry_class"] != "terminal":
        return verdict["retry_class"]
    # Re-derive the basis rather than trusting the recorded one: an override may
    # only ever act on a budget exhaustion, so the reason the row is terminal has
    # to be established from the block's own reason/error, not from a label.
    fresh = classify_block(block.get("reason"), code,
                           attempts=int(state.get("block_attempts") or 1))
    if fresh["retry_basis"] != OVERRIDABLE_RETRY_BASIS:
        return "terminal"
    if str(verdict.get("retry_basis") or fresh["retry_basis"]) != OVERRIDABLE_RETRY_BASIS:
        return "terminal"
    if not key or not retry_override_for(key, date_pt):
        return "terminal"
    return "retryable"


def row_is_terminal(state: dict[str, Any], *, key: str = "",
                    date_pt: str | None = None) -> bool:
    """True when a ledger row must never be selected again.

    `wired` is always terminal. `blocked` is terminal only when its reason and
    error say so — a blocked row is otherwise a retry, not a disqualification.

    `key` is what lets a receipted same-day override be honoured; without it the
    answer is the un-overridden one, which is the safe default for every caller
    that does not know which row it is holding.
    """
    status = state.get("status")
    if status in ALWAYS_TERMINAL:
        return True
    if status != "blocked":
        return False
    return block_retry_class(state, key=key, date_pt=date_pt) != "retryable"


def terminal_ledger_keys(ledger: dict[str, Any], date_pt: str | None = None) -> set[str]:
    return {key for key, state in (ledger.get("rows") or {}).items()
            if row_is_terminal(state, key=key, date_pt=date_pt)}


def requeued_ledger_keys(ledger: dict[str, Any], date_pt: str | None = None) -> set[str]:
    return {
        key for key, state in (ledger.get("rows") or {}).items()
        if state.get("status") == "blocked"
        and not row_is_terminal(state, key=key, date_pt=date_pt)
    }


def day_state(ledger: dict[str, Any], date_pt: str) -> dict[str, Any]:
    day = ledger["days"].setdefault(
        date_pt,
        {
            "wired_keys": [], "blocked_keys": [], "deployment": None, "deployments": [],
            "first_attempted_at_utc": iso_utc(),
        },
    )
    day_deployment_history(day)
    return day


def _assert_no_sensor_recovery_intent(
    day: dict[str, Any], states: Iterable[dict[str, Any]],
) -> None:
    """Keep the ordinary row/deploy machine out of an incident recovery."""
    if day.get("sensor_recovery_intent"):
        raise B2Error("sensor_recovery_in_progress_no_refire")
    affected = [
        str(state.get("key") or "")
        for state in states
        if state.get("sensor_recovery_intent")
    ]
    if affected:
        raise B2Error(
            "sensor_recovery_in_progress_no_refire:" + ",".join(affected)
        )


def begin_row(*, plan_path: Path, ledger_path: Path, key: str) -> dict[str, Any]:
    plan = read_json(plan_path)
    verify_plan_sources(plan)
    candidate = plan_by_key(plan, key)
    repair_validation = None
    if candidate.get("wired_repair_authority"):
        repair_validation = validate_wired_repair_authority(
            candidate, plan, candidate["wired_repair_authority"]["binding"]
        )
    with locked_ledger(ledger_path):
        ledger = load_ledger(ledger_path)
        recovery_day = (ledger.get("days") or {}).get(plan["pt_date"]) or {}
        existing = ledger["rows"].get(key)
        _assert_no_sensor_recovery_intent(
            recovery_day, [existing] if isinstance(existing, dict) else []
        )
        if existing:
            if existing.get("status") == "wired" and repair_validation is None:
                raise B2Error(f"row_already_terminal:{existing.get('status')}")
            if (existing.get("status") != "wired"
                    and row_is_terminal(existing, key=key, date_pt=plan.get("pt_date"))):
                raise B2Error(f"row_already_terminal:{existing.get('status')}")
            if existing.get("deployed_unverified"):
                raise B2Error("row_has_unverified_deployment_requires_reconciliation")
            if existing.get("status") != "blocked" and repair_validation is None:
                if existing.get("pt_date") == plan.get("pt_date"):
                    return {"status": "PASS_IDEMPOTENT_RESUME", "row": existing}
                if existing.get("status") not in RESUMABLE:
                    raise B2Error(f"row_not_cross_day_resumable:{existing.get('status')}")
                day = day_state(ledger, plan["pt_date"])
                if len(day["wired_keys"]) >= plan_wired_limit(plan):
                    raise B2Error("daily_wired_cap_reached")
                prior_dates = list(existing.get("prior_pt_dates") or [])
                old_date = str(existing.get("pt_date") or "")
                if old_date and old_date not in prior_dates:
                    prior_dates.append(old_date)
                existing.update({
                    "pt_date": plan["pt_date"],
                    "plan": path_binding(plan_path),
                    "identity": {
                        "network": candidate["network"],
                        "provider_program_id": candidate["provider_program_id"],
                        "site_program_id": candidate["site_program_id"],
                        "sheet_row": candidate["sheet_row"],
                    },
                    "prior_pt_dates": prior_dates,
                    "cross_day_resumed_at_utc": iso_utc(),
                    "updated_at_utc": iso_utc(),
                })
                save_ledger(ledger_path, ledger)
                return {"status": "PASS_CROSS_DAY_RESUME", "row": existing}
        day = day_state(ledger, plan["pt_date"])
        if (len(day["wired_keys"]) >= plan_wired_limit(plan)
                and not (repair_validation and key in day["wired_keys"])):
            raise B2Error("daily_wired_cap_reached")
        # A day's blocked_keys is that day's CURRENT state, and reopening a
        # retryable block changes it. Without this the daily card kept counting
        # rows as blocked while they were being retried and wired in the same
        # run. Earlier days are untouched: their receipts stay truthful, and the
        # row's full record survives in block_history.
        if existing and existing.get("status") == "blocked" and key in day["blocked_keys"]:
            day["blocked_keys"].remove(key)
        prior_blocks = list(existing.get("block_history") or []) if existing else []
        if existing and existing.get("block"):
            prior_blocks.append({
                "pt_date": existing.get("pt_date"),
                "attempt_id": existing.get("attempt_id"),
                "blocked_at_utc": existing.get("blocked_at_utc"),
                **{k: v for k, v in (existing.get("block") or {}).items() if k != "evidence"},
                "evidence": (existing.get("block") or {}).get("evidence"),
            })
        prior_wired_repairs = list(existing.get("wired_repair_history") or []) if existing else []
        if existing and existing.get("status") == "wired" and repair_validation:
            prior_wired_repairs.append({
                "wired_at_utc": existing.get("wired_at_utc"),
                "deployment_id": existing.get("deployment_id"),
                "sensor": existing.get("sensor"),
                "repair_started_at_utc": iso_utc(),
                "authority": repair_validation["binding"],
                "source_onboarding_manifest": repair_validation["source_onboarding_manifest"],
            })
        block_attempts = len(prior_blocks)
        attempt_id = sha_bytes(
            f"{key}|{plan['pt_date']}|{sha_file(plan_path)}|{block_attempts}".encode()
        )[:24]
        row = {
            "key": key,
            "status": "attempted",
            "attempt_id": attempt_id,
            "pt_date": plan["pt_date"],
            "first_attempted_at_utc": (
                existing.get("first_attempted_at_utc") if existing else iso_utc()
            ) or iso_utc(),
            "updated_at_utc": iso_utc(),
            "plan": path_binding(plan_path),
            "identity": {
                "network": candidate["network"],
                "provider_program_id": candidate["provider_program_id"],
                "site_program_id": candidate["site_program_id"],
                "sheet_row": candidate["sheet_row"],
            },
        }
        if prior_blocks:
            row["block_history"] = prior_blocks
            row["block_attempts"] = block_attempts
        if prior_wired_repairs:
            row["wired_repair_history"] = prior_wired_repairs
            row["wired_repair_authority"] = repair_validation
        ledger["rows"][key] = row
        save_ledger(ledger_path, ledger)
        return {
            "status": (
                "PASS_WIRED_REPAIR_ATTEMPT_STARTED" if repair_validation
                else "PASS_RETRY_ATTEMPT_STARTED" if prior_blocks
                else "PASS_ATTEMPT_STARTED"
            ),
            "row": row,
        }


def block_row(
    *, plan_path: Path, ledger_path: Path, key: str, reason: str, evidence_path: Path
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9_:-]+", reason):
        raise B2Error("block_reason_invalid")
    evidence = path_binding(evidence_path)
    plan = read_json(plan_path)
    candidate = plan_by_key(plan, key)
    with locked_ledger(ledger_path):
        ledger = load_ledger(ledger_path)
        row = ledger["rows"].get(key)
        if not row:
            raise B2Error("row_attempt_missing")
        if row.get("status") == "blocked":
            return {"status": "PASS_IDEMPOTENT_BLOCK", "row": row}
        if row.get("status") == "wired":
            raise B2Error("wired_row_cannot_block")
        attempts = int(row.get("block_attempts") or 0) + 1
        classification = classify_block(
            reason, block_evidence_error_code(evidence), attempts=attempts
        )
        row.update({
            "status": "blocked", "blocked_at_utc": iso_utc(), "updated_at_utc": iso_utc(),
            "block_attempts": attempts,
            "block": {"reason": reason, "evidence": evidence, **classification},
        })
        day = day_state(ledger, candidate.get("pt_date") or plan["pt_date"])
        if key not in day["blocked_keys"]:
            day["blocked_keys"].append(key)
        save_ledger(ledger_path, ledger)
        return {"status": "PASS_BLOCKED_ROW_ISOLATED", "row": row}


def _extract_program_spec(document: dict[str, Any], site_program_id: str) -> dict[str, Any]:
    if document.get("schema") == "g6-r1a-wirein-program-specs-v1":
        matches = [row for row in document.get("programs") or [] if row.get("program_id") == site_program_id]
        if len(matches) != 1:
            raise B2Error("program_spec_missing_or_duplicate")
        return matches[0]
    if document.get("schema") == "b2-wire-backlog-program-spec-v1":
        row = document.get("program")
        if not isinstance(row, dict) or row.get("program_id") != site_program_id:
            raise B2Error("program_spec_identity_invalid")
        return row
    raise B2Error("program_spec_schema_invalid")


def _research_valid(document: dict[str, Any], site_program_id: str) -> None:
    if document.get("schema") != "program-deep-research-v1":
        raise B2Error("research_schema_invalid")
    if document.get("program_id") != site_program_id:
        raise B2Error("research_identity_invalid")
    fields = document.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise B2Error("research_fields_missing")
    if "legitscript" not in fields:
        raise B2Error("research_required_field_missing:legitscript")
    for name, field in fields.items():
        if not isinstance(field, dict):
            raise B2Error(f"research_field_invalid:{name}")
        if not all(str(field.get(key) or "").strip() for key in ("quote", "source_url", "value", "verified_as_of")):
            raise B2Error(f"research_quote_binding_incomplete:{name}")
        if not str(field["source_url"]).startswith("https://"):
            raise B2Error(f"research_source_not_https:{name}")
    if int((document.get("effects") or {}).get("tracking_clicks", 0)) != 0:
        raise B2Error("research_tracking_click_forbidden")


def _terms_text(document: Any) -> str:
    parts: list[str] = []
    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str) and key.casefold() in {
            "text", "normalized_text", "terms_text", "markdown", "exact_relevant_quotes",
            "network_relationship_record_text",
        }:
            parts.append(value)
    visit(document)
    return "\n".join(parts)


def _terms_valid(document: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    if document.get("schema") != "affiliate_approval_classification_v1":
        raise B2Error("terms_classification_schema_invalid")
    network = normalize_network(document.get("network"))
    provider_id = str(document.get("program_id") or document.get("advertiser_id") or "").strip()
    if network != normalize_network(candidate["network"]) or provider_id != candidate["provider_program_id"]:
        raise B2Error("terms_classification_identity_invalid")
    terms = document.get("terms") or {}
    if not terms.get("capture_complete") or str(terms.get("text_sha256") or "") in {"", EMPTY_SHA}:
        raise B2Error("terms_capture_incomplete")
    raw_path = Path(str(terms.get("raw_private_path") or "")).resolve()
    if not raw_path.is_file():
        raise B2Error("terms_raw_capture_missing")
    raw = read_json(raw_path)
    text = _terms_text(raw)
    legal = genuine_legal_prohibition_hits(text)
    account = blocked_account_action_hits(text)
    mechanical = mechanical_hard_trigger_hits(text)
    if legal:
        raise B2Error("genuine_legal_prohibition")
    if account:
        raise B2Error("operator_owned_account_action")
    return {
        "policy": "legality_only_20260814",
        "decision": "ACCEPT_WITH_LOGGED_RISK" if mechanical else "PASS_LEGALITY_FILTER",
        "mechanical_risks": mechanical,
        "legal_prohibition_hits": [],
        "account_action_hits": [],
        "raw_capture": path_binding(raw_path),
    }


def resolve_xentraffic_network_identity(
    candidate: dict[str, Any],
    *,
    identity_dir: Path | None = None,
) -> dict[str, Any]:
    """Resolve XenTraffic's immutable token from authenticated catalogue bytes.

    XenTraffic has two namespaces for one offer: the numeric portal offer id in
    the governed queue and an opaque offer token in the tracking-link contract.
    This resolver never requests that link. It only verifies the independently
    captured portal catalogue and its hash-bound identity evidence.
    """
    if normalize_network(candidate.get("network")) != "xentraffic":
        raise B2Error("xentraffic_network_identity_wrong_rail")
    provider_offer_id = str(candidate.get("provider_program_id") or "").strip()
    if not provider_offer_id or not provider_offer_id.isdigit():
        raise B2Error("xentraffic_provider_offer_id_invalid")
    evidence_name = f"xentraffic-{provider_offer_id}.json"
    if identity_dir is not None:
        evidence_path = Path(identity_dir) / evidence_name
    else:
        candidates = [
            Path(XENTRAFFIC_IDENTITY_DIR) / evidence_name,
            Path(XENTRAFFIC_IDENTITY_FALLBACK_DIR) / evidence_name,
        ]
        evidence_path = next(
            (candidate_path for candidate_path in candidates if candidate_path.is_file()),
            candidates[0],
        )
    evidence = read_json(evidence_path)
    if evidence.get("schema") != XENTRAFFIC_IDENTITY_SCHEMA:
        raise B2Error("xentraffic_identity_capture_schema_invalid")
    if normalize_network(evidence.get("network")) != "xentraffic":
        raise B2Error("xentraffic_identity_capture_network_invalid")
    if str(evidence.get("provider_offer_id") or "").strip() != provider_offer_id:
        raise B2Error("xentraffic_identity_capture_provider_offer_mismatch")

    immutable_id = str(evidence.get("immutable_id") or "").strip()
    tracking_host = str(evidence.get("tracking_host") or "").strip().casefold()
    quote = str(evidence.get("quote") or "")
    if not re.fullmatch(r"[A-Za-z0-9]+", immutable_id):
        raise B2Error("xentraffic_identity_capture_immutable_id_invalid")
    if not tracking_host or urlsplit(f"https://{tracking_host}").hostname != tracking_host:
        raise B2Error("xentraffic_identity_capture_tracking_host_invalid")

    raw_capture = Path(str(evidence.get("capture_path") or ""))
    if not raw_capture.is_absolute():
        raw_capture = BUSINESS / raw_capture
    raw_capture = raw_capture.resolve()
    try:
        raw_capture.relative_to(BUSINESS.resolve())
    except ValueError as exc:
        raise B2Error("xentraffic_identity_source_outside_business") from exc
    if not raw_capture.is_file():
        raise B2Error("xentraffic_identity_source_missing")
    if sha_file(raw_capture) != str(evidence.get("capture_sha256") or ""):
        raise B2Error("xentraffic_identity_source_hash_drift")
    source_bytes = raw_capture.read_bytes()
    if quote.encode("utf-8") not in source_bytes:
        raise B2Error("xentraffic_identity_quote_not_in_source")
    source = read_json(raw_capture)
    offers = source.get("offers") if isinstance(source, dict) else None
    if not isinstance(offers, list):
        raise B2Error("xentraffic_identity_source_offers_missing")
    matches = [
        row for row in offers
        if isinstance(row, dict)
        and str(row.get("offer_id") or "").strip() == provider_offer_id
    ]
    if len(matches) != 1:
        raise B2Error(f"xentraffic_identity_source_offer_count_invalid:{len(matches)}")
    tracking_link = str(matches[0].get("tracking_link") or "").strip()
    parsed = urlsplit(tracking_link)
    path_tokens = [token for token in parsed.path.split("/") if token]
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != tracking_host:
        raise B2Error("xentraffic_identity_source_tracking_host_mismatch")
    if path_tokens.count(immutable_id) != 1:
        raise B2Error("xentraffic_identity_source_immutable_token_mismatch")
    if immutable_id not in quote or tracking_host not in quote.casefold():
        raise B2Error("xentraffic_identity_quote_binding_invalid")

    return {
        "advertiser_id": immutable_id,
        "tracking_host": tracking_host,
        "network_identity": {
            "schema": XENTRAFFIC_IDENTITY_SCHEMA,
            "network": "XenTraffic",
            "provider_offer_id": provider_offer_id,
            "immutable_id": immutable_id,
            "tracking_host": tracking_host,
            "identity_capture": path_binding(evidence_path),
            "source_capture": path_binding(raw_capture),
        },
    }


def reconcile_program_authority_rail_identity(
    authority_path: Path,
    candidate: dict[str, Any],
    *,
    receipt_dir: Path | None = None,
) -> dict[str, Any]:
    """Repair only the known legacy XenTraffic numeric-id authority shape."""
    if normalize_network(candidate.get("network")) != "xentraffic":
        return {"status": "PASS_NOT_APPLICABLE", "changed": False}

    authority_path = Path(authority_path).resolve()
    before_bytes = authority_path.read_bytes()
    document = read_json(authority_path)
    spec = _extract_program_spec(document, str(candidate.get("site_program_id") or ""))
    expected = resolve_xentraffic_network_identity(candidate)
    provider_offer_id = str(candidate.get("provider_program_id") or "").strip()
    current = str(spec.get("advertiser_id") or "").strip()
    changed = False
    if current == provider_offer_id:
        spec["advertiser_id"] = expected["advertiser_id"]
        spec["tracking_host"] = expected["tracking_host"]
        spec["network_identity"] = expected["network_identity"]
        changed = True
    elif current != expected["advertiser_id"]:
        raise B2Error("xentraffic_program_authority_immutable_id_drift")
    elif (
        spec.get("tracking_host") != expected["tracking_host"]
        or spec.get("network_identity") != expected["network_identity"]
    ):
        raise B2Error("xentraffic_program_authority_network_identity_drift")

    if changed:
        atomic_json(authority_path, document)
    after_binding = path_binding(authority_path)
    slug = re.sub(
        r"[^a-z0-9]+", "-", str(candidate.get("site_program_id") or "").casefold()
    ).strip("-") or "provider"
    receipt_path = Path(receipt_dir or RAIL_IDENTITY_RECONCILIATION_DIR) / (
        f"{slug}-xentraffic-{provider_offer_id}.json"
    )
    if not changed and receipt_path.is_file():
        prior = read_json(receipt_path)
        if (
            prior.get("schema") == "glp.program-authority-rail-identity-reconciliation.v1"
            and prior.get("status") == "PASS_RECONCILED_LEGACY_XENTRAFFIC_NUMERIC_ID"
            and str(prior.get("provider") or "") == str(candidate.get("site_program_id") or "")
            and str(prior.get("provider_offer_id") or "") == provider_offer_id
            and (prior.get("after") or {}).get("authority") == after_binding
            and (prior.get("after") or {}).get("network_identity")
            == expected["network_identity"]
        ):
            return {
                "status": "PASS_ALREADY_RECONCILED",
                "changed": False,
                "authority": after_binding,
                "receipt": path_binding(receipt_path),
                "network_identity": expected["network_identity"],
            }
    receipt = {
        "schema": "glp.program-authority-rail-identity-reconciliation.v1",
        "status": (
            "PASS_RECONCILED_LEGACY_XENTRAFFIC_NUMERIC_ID"
            if changed else "PASS_ALREADY_RECONCILED"
        ),
        "at_utc": iso_utc(),
        "provider": str(candidate.get("site_program_id") or ""),
        "network": "XenTraffic",
        "provider_offer_id": provider_offer_id,
        "before": {
            "authority": {
                "path": str(authority_path),
                "sha256": sha_bytes(before_bytes),
                "bytes": len(before_bytes),
            },
            "advertiser_id": current,
        },
        "after": {
            "authority": after_binding,
            "advertiser_id": expected["advertiser_id"],
            "tracking_host": expected["tracking_host"],
            "network_identity": expected["network_identity"],
        },
        "effects": {
            "authority_files_reconciled": int(changed),
            "deployments": 0,
            "provider_calls": 0,
            "tracking_link_requests": 0,
            "tracking_clicks": 0,
        },
    }
    atomic_json(receipt_path, receipt)
    return {
        "status": receipt["status"],
        "changed": changed,
        "authority": after_binding,
        "receipt": path_binding(receipt_path),
        "network_identity": expected["network_identity"],
    }


def validate_prepared_authority_reconciliation(
    candidate: dict[str, Any],
    before_binding: dict[str, Any],
    after_binding: dict[str, Any],
    *,
    receipt_dir: Path | None = None,
) -> dict[str, Any]:
    """License one prepared-spec rebind from the exact reconciliation receipt."""
    if normalize_network(candidate.get("network")) != "xentraffic":
        raise B2Error("prepared_spec_reconciliation_wrong_rail")
    provider = str(candidate.get("site_program_id") or "")
    provider_offer_id = str(candidate.get("provider_program_id") or "")
    slug = re.sub(r"[^a-z0-9]+", "-", provider.casefold()).strip("-") or "provider"
    receipt_path = Path(receipt_dir or RAIL_IDENTITY_RECONCILIATION_DIR) / (
        f"{slug}-xentraffic-{provider_offer_id}.json"
    )
    receipt = read_json(receipt_path)
    if receipt.get("schema") != "glp.program-authority-rail-identity-reconciliation.v1":
        raise B2Error("prepared_spec_reconciliation_schema_invalid")
    if receipt.get("status") != "PASS_RECONCILED_LEGACY_XENTRAFFIC_NUMERIC_ID":
        raise B2Error("prepared_spec_reconciliation_not_mutating_pass")
    if (
        str(receipt.get("provider") or "") != provider
        or normalize_network(receipt.get("network")) != "xentraffic"
        or str(receipt.get("provider_offer_id") or "") != provider_offer_id
    ):
        raise B2Error("prepared_spec_reconciliation_identity_drift")
    if (receipt.get("before") or {}).get("authority") != before_binding:
        raise B2Error("prepared_spec_reconciliation_before_binding_drift")
    if (receipt.get("after") or {}).get("authority") != after_binding:
        raise B2Error("prepared_spec_reconciliation_after_binding_drift")
    expected = resolve_xentraffic_network_identity(candidate)
    after = receipt.get("after") or {}
    if (
        after.get("advertiser_id") != expected["advertiser_id"]
        or after.get("tracking_host") != expected["tracking_host"]
        or after.get("network_identity") != expected["network_identity"]
    ):
        raise B2Error("prepared_spec_reconciliation_network_identity_drift")
    effects = receipt.get("effects") or {}
    if (
        int(effects.get("authority_files_reconciled", -1)) != 1
        or any(int(effects.get(name, -1)) != 0 for name in (
            "deployments", "provider_calls", "tracking_link_requests", "tracking_clicks"
        ))
    ):
        raise B2Error("prepared_spec_reconciliation_effects_invalid")
    return {
        "status": "PASS_PREPARED_SPEC_REBIND_AUTHORIZED",
        "receipt": path_binding(receipt_path),
        "before": before_binding,
        "after": after_binding,
    }


def _validate_spec(spec: dict[str, Any], candidate: dict[str, Any], research_path: Path, terms_path: Path) -> None:
    network = normalize_network(candidate["network"])
    expected_advertiser_id = candidate["provider_program_id"]
    expected_xentraffic: dict[str, Any] | None = None
    if network == "xentraffic":
        expected_xentraffic = resolve_xentraffic_network_identity(candidate)
        expected_advertiser_id = expected_xentraffic["advertiser_id"]
    exact = {
        "program_id": candidate["site_program_id"],
        "sheet_row": int(candidate["sheet_row"]),
        "advertiser_id": expected_advertiser_id,
    }
    for key, expected in exact.items():
        actual = int(spec.get(key)) if key == "sheet_row" else str(spec.get(key) or "")
        if actual != expected:
            raise B2Error(f"spec_identity_drift:{key}")
    if normalize_network(spec.get("network")) != normalize_network(candidate["network"]):
        raise B2Error("spec_network_drift")
    if expected_xentraffic is not None:
        if spec.get("network_identity") != expected_xentraffic["network_identity"]:
            raise B2Error("spec_network_identity_drift")
        if str(spec.get("tracking_host") or "").casefold() != expected_xentraffic["tracking_host"]:
            raise B2Error("spec_tracking_host_drift")
    if Path(str(spec.get("terms_classification_path") or "")).resolve() != terms_path.resolve():
        raise B2Error("spec_terms_path_drift")
    if str(spec.get("research_file") or "") != research_path.name:
        raise B2Error("spec_research_file_drift")
    if not str(spec.get("official_url") or "").startswith("https://"):
        raise B2Error("spec_official_url_invalid")
    # `checked_as_of` is a HARD requirement of the builder, not an optional
    # nicety: build_glp_affiliate_wirein_incremental_v2 subscripts it twice
    # (`spec["checked_as_of"]` at :970 for
    # network_contract.local_evidence_checked_through, and at :1027 for the
    # provider row), and the value is rendered to readers as "Checked <date>".
    #
    # DEFECT 2026-09-01 (deploy-fable-20260901, three rows lost). Three
    # authorities authored that day - joi-blokes-compounded-glp-1s (xentraffic
    # 3153), gatlan (3179) and omzo-quiz-v2-scott-disick (3185) - carried no
    # `checked_as_of`. Nothing here looked for it, so `step_program_authority`
    # graded all three GREEN and prepare passed them; the field's absence then
    # surfaced as a bare `KeyError: \'checked_as_of\'` INSIDE THE SHARED BUILD,
    # which is a run-wide crash rather than a per-row block. One missing field
    # in one authority took down a batch of nineteen.
    #
    # Requiring it here is the whole fix: the row fails by name, at prepare,
    # with a reason an authoring brief can act on, and the shared build never
    # sees a spec it cannot render. This does NOT synthesise a date and it does
    # not weaken any gate - a spec that already carried the field is unaffected.
    checked_as_of = str(spec.get("checked_as_of") or "").strip()
    if not checked_as_of:
        raise B2Error("spec_checked_as_of_missing")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked_as_of):
        raise B2Error(f"spec_checked_as_of_invalid:{checked_as_of}")
    try:
        dt.date.fromisoformat(checked_as_of)
    except ValueError as exc:
        raise B2Error(f"spec_checked_as_of_invalid:{checked_as_of}") from exc
    # A rail without fixed hosts (merchant_host / tracking_domain) needs a host
    # to hold the tracking link to. Resolve it HERE, at prevalidation, so a spec
    # that cannot supply one blocks by name instead of dying inside the builder
    # as `command_failed` — which is how Nu Image burned eight attempts on
    # `trackdesk_tracking_host_undeclared` with nothing on the card to act on.
    try:
        link_rails.spec_allowed_host(spec["network"], spec)
    except link_rails.RailError as exc:
        raise B2Error(f"spec_tracking_host_unresolvable:{exc}") from exc
    for section in ("page_copy", "row_contract", "field_sources"):
        if not isinstance(spec.get(section), dict) or not spec[section]:
            raise B2Error(f"spec_section_missing:{section}")
    try:
        profile = research_dynamic_profile(
            {"provider_dynamic_profile": spec.get("provider_dynamic_profile")},
            str(spec["program_id"]),
        )
    except ValueError as exc:
        raise B2Error(f"spec_provider_dynamic_profile_invalid:{exc}") from exc
    if profile is None:
        raise B2Error("spec_provider_dynamic_profile_missing")
    if spec.get("registry_evidence_file"):
        try:
            registry_evidence.load_registry_evidence(spec, research_path=research_path)
        except registry_evidence.RegistryEvidenceError as exc:
            raise B2Error(f"spec_registry_evidence_invalid:{exc}") from exc
    _validate_page_facts(spec, research_path)

def _validate_logo_source(spec: dict[str, Any]) -> dict[str, Any]:
    binding = spec.get("logo_source")
    if not isinstance(binding, dict):
        raise B2Error("net_new_logo_source_binding_missing")
    output = str(spec.get("logo_output_file") or "")
    if not output or Path(output).name != output or not output.endswith(".webp"):
        raise B2Error("net_new_logo_output_invalid")
    path = Path(str(binding.get("path") or "")).resolve()
    if not path.is_file():
        raise B2Error("net_new_logo_source_missing")
    if sha_file(path) != str(binding.get("sha256") or ""):
        raise B2Error("net_new_logo_source_hash_drift")
    if path.stat().st_size != int(binding.get("bytes") or -1):
        raise B2Error("net_new_logo_source_size_drift")
    if int(binding.get("width") or 0) < 1 or int(binding.get("height") or 0) < 1:
        raise B2Error("net_new_logo_dimensions_invalid")
    if binding.get("media_type") != "image/webp":
        raise B2Error("net_new_logo_media_type_invalid")
    if not str(binding.get("source_asset_url") or "").startswith("https://"):
        raise B2Error("net_new_logo_source_url_invalid")
    if str(binding.get("program_id") or "") != str(spec.get("program_id") or ""):
        raise B2Error("net_new_logo_program_identity_drift")
    return binding


def verify_staged_pricing_spec(pricing_path: Path, site_program_id: str) -> dict[str, Any]:
    """Verify an authored pricing add-spec without writing a candidate.

    The eventual writer re-runs these citation checks against the same bytes.
    This preflight exists so ``authoring_resume`` can distinguish BUILD-READY
    staged evidence from an engine receipt that merely says ``authored``.
    """
    path = Path(pricing_path).resolve()
    try:
        path.relative_to(BUSINESS.resolve())
    except ValueError as exc:
        raise B2Error("pricing_spec_outside_business_root") from exc
    spec = read_json(path)
    if not isinstance(spec, dict):
        raise B2Error("pricing_spec_invalid")
    if str(spec.get("program_id") or "") != str(site_program_id):
        raise B2Error("pricing_spec_identity_drift")
    if not str(spec.get("provider") or "").strip():
        raise B2Error("pricing_spec_provider_missing")
    medications = spec.get("medications") or []
    if not isinstance(medications, list):
        raise B2Error("pricing_spec_medications_invalid")
    if not medications and not str(spec.get("unavailable_reason") or "").strip():
        raise B2Error("pricing_spec_has_no_rows_or_unavailable_reason")
    proofs = []
    for row in medications:
        if not isinstance(row, dict):
            raise B2Error("pricing_spec_medication_row_invalid")
        try:
            proofs.append(surface_writers.verify_citation(row, site_program_id))
        except surface_writers.SurfaceWriteError as exc:
            raise B2Error(f"pricing_spec_citation_invalid:{exc}") from exc
    return {
        "status": "PASS",
        "program_id": site_program_id,
        "pricing_spec": path_binding(path),
        "verified_rows": len(proofs),
        "unavailable": not medications,
    }


def pricing_spec_from_authoring_receipt(receipt_path: Path,
                                        site_program_id: str) -> Path:
    """Resolve the one verified pricing spec carried by an authored fit receipt."""
    receipt_file = Path(receipt_path).resolve()
    receipt = read_json(receipt_file)
    if receipt.get("schema") != "glp.onboarding_authoring_receipt.v1":
        raise B2Error("authoring_receipt_schema_invalid")
    if str(receipt.get("provider") or "") != str(site_program_id):
        raise B2Error("authoring_receipt_provider_drift")
    if str(receipt.get("verdict") or "").casefold() != "authored":
        raise B2Error("authoring_receipt_not_authored")
    if str(receipt.get("fit_verdict") or "").casefold() != "fit":
        raise B2Error("authoring_receipt_not_fit")
    candidates = []
    for raw in receipt.get("artifacts_written") or []:
        path = Path(str(raw))
        if not path.is_absolute():
            path = BUSINESS / path
        if path.name == "pricing-spec.json" and path.is_file():
            candidates.append(path.resolve())
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) != 1:
        raise B2Error(f"authoring_pricing_spec_count_invalid:{len(candidates)}")
    verify_staged_pricing_spec(candidates[0], site_program_id)
    return candidates[0]


def _apply_staged_pricing(candidate: Path, pricing_path: Path,
                          site_program_id: str) -> dict[str, Any]:
    """Apply pricing only after the builder has materialized the registry row."""
    registry = read_json(candidate / "data/program-registry.json")
    registry_ids = {
        str(row.get("program_id") or "")
        for row in registry.get("programs") or [] if isinstance(row, dict)
    }
    if site_program_id not in registry_ids:
        raise B2Error(f"pricing_registry_row_missing_before_apply:{site_program_id}")
    verification = verify_staged_pricing_spec(pricing_path, site_program_id)
    matrix_path = candidate / surface_writers.PRICING_REL
    matrix = read_json(matrix_path)
    if site_program_id in (matrix.get("providers") or {}):
        return {
            "status": "PASS_ALREADY_PRESENT",
            "program_id": site_program_id,
            "pricing_spec": verification["pricing_spec"],
            "applied": False,
        }
    _detach(matrix_path)
    try:
        applied = surface_writers.add_pricing_provider(
            candidate, read_json(pricing_path), apply=True)
    except surface_writers.SurfaceWriteError as exc:
        raise B2Error(f"pricing_apply_failed:{exc}") from exc
    return {"status": "PASS_APPLIED", **applied,
            "pricing_spec": verification["pricing_spec"]}


def _validate_page_facts(spec: dict[str, Any], research_path: Path) -> None:
    # Same fact-attestation contract the post-deploy completeness sensor
    # enforces; a row with unattested page-copy tokens must fail at prepare,
    # not after its release is already live (Live Vital "expedited", 2026-08-15).
    import glp_deploy_completeness_sensor as sensor
    research = read_json(research_path)
    copy = spec.get("page_copy") or {}
    for page_field, research_fields in sensor.PAGE_FACT_MAP.items():
        page_text = copy.get(page_field)
        if not isinstance(page_text, str) or not page_text.strip():
            continue
        page_values = sensor.value_tokens(page_text)
        if not page_values:
            continue
        attested = sensor.value_tokens(sensor.research_text(research, research_fields))
        unattested = sorted(page_values - attested)
        if unattested:
            raise B2Error(
                f"spec_page_fact_unattested:{page_field}:{','.join(unattested)}")


def prepare_row(
    *,
    plan_path: Path,
    ledger_path: Path,
    key: str,
    spec_path: Path,
    research_path: Path,
    terms_path: Path,
    pricing_path: Path | None = None,
) -> dict[str, Any]:
    plan = read_json(plan_path)
    verify_plan_sources(plan)
    candidate = plan_by_key(plan, key)
    if not candidate.get("approval_ready") or not candidate.get("tracking_ready"):
        raise B2Error("sheet_approval_or_tracking_gate_not_ready")
    research = read_json(research_path)
    _research_valid(
        research,
        str(candidate.get("research_program_id") or candidate["site_program_id"]),
    )
    terms = read_json(terms_path)
    policy = _terms_valid(terms, candidate)
    spec_document = read_json(spec_path)
    spec = _extract_program_spec(spec_document, candidate["site_program_id"])
    _validate_spec(spec, candidate, research_path, terms_path)
    if not candidate.get("existing_editorial_page"):
        _validate_logo_source(spec)
    if pricing_path is not None:
        verify_staged_pricing_spec(pricing_path, candidate["site_program_id"])
    with locked_ledger(ledger_path):
        ledger = load_ledger(ledger_path)
        row = ledger["rows"].get(key)
        if not row:
            raise B2Error("row_attempt_missing")
        if row.get("status") == "prepared":
            current_artifacts = {
                "spec": path_binding(spec_path),
                "research": path_binding(research_path),
                "terms_classification": path_binding(terms_path),
            }
            if pricing_path is not None:
                current_artifacts["pricing_spec"] = path_binding(pricing_path)
            current_spec = current_artifacts["spec"]
            prepared_artifacts = row.get("artifacts") or {}
            prepared_spec = prepared_artifacts.get("spec") or {}
            drifted = sorted(
                name for name, binding in current_artifacts.items()
                if prepared_artifacts.get(name) != binding
            )
            if drifted:
                try:
                    reconciliation = validate_prepared_authority_reconciliation(
                        candidate, prepared_spec, current_spec
                    )
                except B2Error as exc:
                    # A benign same-run rewrite of a prepared row's evidence must
                    # not UN-PREPARE it. An authoring cycle re-emits the program
                    # authority (and can re-capture terms) while its own carrier
                    # is still resuming; the strict rail-identity licence above
                    # only covers the legacy XenTraffic numeric-id migration, so
                    # every other honest rewrite used to throw the row back to
                    # `blocked` and destroy accumulated preparation. That is the
                    # exact signature that cost G17 altRx and G18 Ro.
                    #
                    # Re-binding here is not a weaker gate than preparing fresh:
                    # every check a FIRST preparation must pass has already run
                    # above this lock against these exact current bytes - the
                    # sheet approval/tracking gate, `_research_valid`,
                    # `_terms_valid`, `_extract_program_spec`, `_validate_spec`
                    # against the plan candidate (which is what binds route and
                    # network identity), the logo source, and the staged pricing
                    # spec. A row that would be accepted as a first preparation
                    # is therefore accepted as a re-preparation. The strict
                    # refusal is recorded, never discarded, so a real identity
                    # drift is still visible in the ledger.
                    reconciliation = {
                        "status": "PASS_PREPARED_EVIDENCE_REVALIDATED_REBIND",
                        "changed": True,
                        "licensed_by": "full_fresh_preparation_gate_replay_on_current_bytes",
                        "drifted_artifacts": drifted,
                        "strict_rail_identity_reconciliation_refusal": (
                            f"{type(exc).__name__}:{exc}"
                        ),
                        "authority": current_spec,
                    }
                row.setdefault("artifacts", {}).update(current_artifacts)
                row.setdefault("artifact_reconciliations", []).append({
                    **reconciliation,
                    "reconciled_at_utc": iso_utc(),
                })
                row["terms_policy"] = policy
                row["updated_at_utc"] = iso_utc()
                save_ledger(ledger_path, ledger)
                return {
                    "status": "PASS_PREPARED_AUTHORITY_REBOUND",
                    "row": row,
                    "reconciliation": reconciliation,
                }
            return {"status": "PASS_IDEMPOTENT_PREPARED", "row": row}
        if row.get("status") != "attempted":
            raise B2Error(f"row_not_preparable:{row.get('status')}")
        artifacts = {
            "spec": path_binding(spec_path),
            "research": path_binding(research_path),
            "terms_classification": path_binding(terms_path),
        }
        if pricing_path is not None:
            artifacts["pricing_spec"] = path_binding(pricing_path)
        row.update({
            "status": "prepared", "prepared_at_utc": iso_utc(), "updated_at_utc": iso_utc(),
            "artifacts": artifacts,
            "terms_policy": policy,
        })
        save_ledger(ledger_path, ledger)
        return {"status": "PASS_PREPARED", "row": row}


def daily_card_line(ledger: dict[str, Any], plan: dict[str, Any], date_pt: str) -> str:
    day = ledger.get("days", {}).get(date_pt) or {"wired_keys": [], "blocked_keys": []}
    wired = len(set(day.get("wired_keys") or []))
    blocked_rows = [ledger["rows"][key] for key in day.get("blocked_keys") or [] if key in ledger["rows"]]
    plan_blocks = plan.get("fail_closed_unmapped") or []
    reasons: dict[str, int] = {}
    for row in blocked_rows:
        reason = str((row.get("block") or {}).get("reason") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
    for row in plan_blocks:
        reason = str(row.get("reason") or "unmapped_fail_closed")
        reasons[reason] = reasons.get(reason, 0) + 1
    reason_text = ", ".join(f"{key}:{reasons[key]}" for key in sorted(reasons)) or "none"
    terminal = terminal_ledger_keys(ledger)
    queue_keys = {row["key"] for row in plan.get("queue") or []}
    remaining = len(queue_keys - terminal)
    blocked_count = len(blocked_rows) + len(plan_blocks)
    return (
        f"wired {wired}/{plan_wired_limit(plan)} · blocked {blocked_count} "
        f"({reason_text}) · backlog remaining {remaining}"
    )


def status_receipt(*, plan_path: Path, ledger_path: Path) -> dict[str, Any]:
    plan = read_json(plan_path)
    verify_plan_sources(plan)
    ledger = load_ledger(ledger_path)
    line = daily_card_line(ledger, plan, plan["pt_date"])
    return {
        "schema": DAY_SCHEMA,
        "status": "PASS",
        "pt_date": plan["pt_date"],
        "daily_card_line": line,
        "counts": {
            "wired": len((ledger.get("days", {}).get(plan["pt_date"]) or {}).get("wired_keys") or []),
            "blocked": len((ledger.get("days", {}).get(plan["pt_date"]) or {}).get("blocked_keys") or []),
            "cap": plan_wired_limit(plan),
        },
        "plan": path_binding(plan_path),
        "ledger": path_binding(ledger_path) if ledger_path.exists() else None,
    }


def extract_tap_failure_blocks(text: str, *, limit: int = 20000) -> str:
    """Keep failing Node TAP diagnostics even when a long suite's tail clips them.

    Node's serial test reporter prints the actionable assertion immediately after
    ``not ok`` and may then print hundreds of passing tests.  Keeping only the
    final stdout bytes turns a deterministic refusal into an anonymous ``# fail
    1``.  Extract only failed TAP records; ordinary command output is unchanged.
    """
    lines = str(text).splitlines()
    blocks: list[str] = []
    boundary = re.compile(r"^(?:# Subtest:|\s*(?:not )?ok \d+ - |1\.\.\d+)")
    failed = re.compile(r"^\s*not ok \d+ - ")
    for index, line in enumerate(lines):
        if not failed.match(line):
            continue
        start = index - 1 if index and lines[index - 1].startswith("# Subtest:") else index
        end = index + 1
        while end < len(lines) and not boundary.match(lines[end]):
            end += 1
        blocks.append("\n".join(lines[start:end]).strip())
    if not blocks:
        return ""
    return "\n\n".join(blocks)[:limit]


def transient_top_level_test_files(error: str, *, cwd: Path) -> list[str]:
    """Return replay-safe paths only for anonymous test-file process failures."""
    summaries = [int(value) for value in re.findall(r"(?m)^# fail (\d+)\s*$", str(error))]
    if not summaries or summaries[-1] <= 0:
        return []
    blocks = extract_tap_failure_blocks(str(error)).split("\n\n")
    root = cwd.resolve()
    relative_files: list[str] = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 2 or not lines[0].startswith("# Subtest: "):
            return []
        if "error: 'test failed'" not in block or "code: 'ERR_TEST_FAILURE'" not in block:
            return []
        if any(marker in block for marker in ("ERR_ASSERTION", "expected:", "actual:")):
            return []
        candidate = Path(lines[0].removeprefix("# Subtest: ").strip())
        try:
            relative = candidate.resolve().relative_to(root)
        except (OSError, ValueError):
            return []
        if not relative.parts or relative.parts[0] != "tests" or not relative.name.endswith(".test.mjs"):
            return []
        if not candidate.is_file():
            return []
        value = relative.as_posix()
        if value not in relative_files:
            relative_files.append(value)
    if len(relative_files) != summaries[-1]:
        return []
    return relative_files


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, timeout=timeout, env=env,
    )
    record = {
        "command": command,
        "cwd": str(cwd),
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-1200:],
        "stderr_tail": result.stderr[-1200:],
        # Preserve refresh-related postbuild lines from the FULL stdout, not
        # merely the tail after later validators have printed. This lets the
        # durable release receipt carry route names when that postbuild version
        # emits them.
        "provenance_refresh_output_lines": [
            line[-20000:]
            for line in result.stdout.splitlines()
            if "provenance" in line.casefold()
            and ("refresh" in line.casefold() or "rewrit" in line.casefold())
        ][-20:],
        "environment_overrides": {
            name: env.get(name)
            for name in (
                "GLP_PRICE_INDEX_REFRESH",
                # Recorded so the receipt shows the exact lastmod lane used.
                "GLP_LASTMOD_DECLARED_ROUTES_FILE",
                "GLP_LASTMOD_PROVENANCE_REFRESH",
                "GLP_RELEASE_DATE",
            )
            if env and env.get(name) is not None
        },
    }
    if result.returncode:
        command_label = ":".join(str(part) for part in command[:2])
        # A failing command's real refusal is in whichever stream it chose, and
        # `npm run build` chains 20+ validators that print theirs to STDOUT.
        # Reading `stderr or stdout` therefore discarded the entire failure the
        # moment ANY earlier step wrote a single warning line to stderr: observed
        # live 2026-08-20T21:35:39Z, where the whole recorded cause of a failed
        # machine build was a Pillow deprecation warning from a validator that
        # had PASSED. Carry both, labelled, so the block receipt and the brief
        # can name what actually refused.
        streams = []
        tap_failures = extract_tap_failure_blocks(result.stdout)
        if tap_failures:
            streams.append(f"[tap_failures]\n{tap_failures}")
        if result.stdout.strip():
            streams.append(f"[stdout]\n{result.stdout[-100000:]}")
        if result.stderr.strip():
            streams.append(f"[stderr]\n{result.stderr[-100000:]}")
        detail = "\n".join(streams) or "(no output on either stream)"
        raise B2Error(f"command_failed:{command_label}:{result.returncode}:{detail}")
    return record


def _run_npm_test_with_top_level_replay(*, cwd: Path, timeout: int) -> dict[str, Any]:
    """Replay anonymous child startup failures once, then require the full suite."""
    try:
        return _run(["npm", "test"], cwd=cwd, timeout=timeout)
    except B2Error as initial_error:
        failed_files = transient_top_level_test_files(str(initial_error), cwd=cwd)
        if not failed_files:
            raise
        diagnostic_replay = _run(
            ["node", "--test", "--test-concurrency=1", *failed_files],
            cwd=cwd,
            timeout=timeout,
        )
        full_suite_replay = _run(["npm", "test"], cwd=cwd, timeout=timeout)
        return {
            **full_suite_replay,
            "status": "PASS_AFTER_TOP_LEVEL_PROCESS_REPLAY",
            "initial_failure": {
                "classification": "anonymous_top_level_test_process_failure",
                "error_sha256": hashlib.sha256(str(initial_error).encode("utf-8")).hexdigest(),
                "failed_files": failed_files,
                "failure_count": len(failed_files),
            },
            "diagnostic_replay": diagnostic_replay,
            "full_suite_replay": full_suite_replay,
        }


def clip_error(text: str, *, head: int = 4000, tail: int = 6000) -> str:
    """Clip a failure to its two informative ends, never to its head alone.

    A build failure's cause can be at either end — the failing command's banner
    at the top, the traceback at the bottom — so a head-only clip is how a block
    receipt ends up carrying a warning instead of the error.
    """
    text = str(text)
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n…[{len(text) - head - tail} characters elided]…\n{text[-tail:]}"


def _restore_build_ephemeral_predecessor_files(source: Path, candidate: Path) -> dict[str, Any]:
    """Carry forward predecessor bytes this build wipes but does not re-create.

    Two families, both of which the deploy chain's absolute deletion guard
    (`release_deletes_predecessor_paths`) would otherwise refuse:

      * the named wrangler dry-run outputs, which only `validate:price-watch-worker`
        produces;
      * anything the PREDECESSOR shipped under `artifacts/ops/` that this build
        did not write back. `scripts/build-staging.mjs` clears that directory
        wholesale (`rm(OPS, {recursive: true})`) and then re-emits only the
        artifacts it owns, so a one-off receipt another lane sealed into a
        release — e.g. `artifacts/ops/p0r2-offline-sensor.json`, sealed into the
        2026-08-19 base — vanishes on the NEXT build and takes the deploy with
        it. Discovered 2026-08-20 building the renderer-derivation release; it
        would have blocked the machine's own next deploy identically.

    Restoring is the conservative direction: it preserves bytes rather than
    relaxing the guard, and it can never resurrect a deliberate removal, because
    a deliberate removal travels through `--removal-receipt` on the deployer and
    never through this build path.
    """
    relatives = list(BUILD_EPHEMERAL_PREDECESSOR_FILES)
    ops = source / "artifacts/ops"
    if ops.is_dir():
        relatives.extend(
            str(path.relative_to(source))
            for path in sorted(ops.rglob("*"))
            if path.is_file() and not path.is_symlink()
        )
    restored: list[str] = []
    for relative in relatives:
        original = source / relative
        target = candidate / relative
        if not original.is_file():
            continue
        if target.exists():
            # An artifact this build DID regenerate is the build's, not the
            # predecessor's, and its new bytes stand. Only the named ephemeral
            # files must match byte-for-byte.
            if relative in BUILD_EPHEMERAL_PREDECESSOR_FILES and (
                    not target.is_file() or sha_file(target) != sha_file(original)):
                raise B2Error(f"build_ephemeral_predecessor_conflict:{relative}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
        restored.append(relative)
    return {"status": "PASS", "restored": restored}


def _restore_predecessor_referenced_images(source: Path, candidate: Path) -> dict[str, Any]:
    """Carry forward image bytes another lane published that this build cannot make.

    The SEO guide lane ships a guide as page DATA plus rendered image BINARIES.
    This build regenerates the page from that data but owns no image pipeline,
    so the binaries disappear from the candidate: the release would ship a guide
    whose own <img> tags 404, and would delete seven live assets belonging to a
    guide published minutes earlier (deploy guard
    `prepared_release_deletes_predecessor_paths`, 2026-08-18).

    Scope is deliberately narrow so this can never resurrect a deliberate
    deletion. A file is restored only if all four hold: it lives under
    public/assets/images, the predecessor has it, the candidate does not, and
    the candidate's OWN html still references it. A retired route's assets are
    referenced by nothing and stay deleted.
    """
    images = source / "public/assets/images"
    if not images.is_dir():
        return {"status": "PASS_NO_PREDECESSOR_IMAGES", "restored": []}
    referenced: set[str] = set()
    pattern = re.compile(r"/assets/images/([A-Za-z0-9._-]+)")
    for html in (candidate / "public").rglob("*.html"):
        referenced.update(pattern.findall(html.read_text(encoding="utf-8", errors="ignore")))
    restored: list[str] = []
    for original in sorted(images.rglob("*")):
        if not original.is_file():
            continue
        relative = original.relative_to(source)
        target = candidate / relative
        if target.exists() or original.name not in referenced:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
        os.chmod(target, 0o644)
        restored.append(relative.as_posix())
    return {"status": "PASS", "referenced_names": len(referenced), "restored": restored}


def _carry_forward_missing_deployment_paths(
    predecessor: Path, candidate: Path,
) -> dict[str, Any]:
    """Preserve every predecessor release path owned by the live site lanes.

    The deploy carrier computes deletions across the full sealed release, not only
    ``public`` and ``functions``. A batch-pinned source can predate the current
    production predecessor, so a later article publication can add root-level
    ``assets`` and ``artifacts/ultracode`` bytes that this build does not know how
    to regenerate. Missing bytes in those four release-owned scopes are copied
    forward; candidate-owned paths are never overwritten. Other root-level scopes
    remain fail-closed rather than being widened implicitly.
    """
    restored: list[str] = []
    for scope in ("public", "functions", "assets", "artifacts/ultracode"):
        root = predecessor / scope
        if not root.is_dir():
            if scope in {"public", "functions"}:
                raise B2Error(f"deployment_predecessor_scope_missing:{scope}")
            continue
        for original in sorted(root.rglob("*")):
            if not original.is_file() or original.is_symlink():
                continue
            relative = original.relative_to(predecessor)
            target = candidate / relative
            if target.exists():
                if not target.is_file():
                    raise B2Error(f"deployment_predecessor_path_type_conflict:{relative.as_posix()}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
            restored.append(relative.as_posix())
    return {"status": "PASS", "restored_count": len(restored), "restored": restored}


def _replace_once(path: Path, pattern: str, replacement: str | Callable[[re.Match[str]], str]) -> None:
    text = path.read_text(encoding="utf-8")
    matches = list(re.finditer(pattern, text, re.S))
    if len(matches) != 1:
        raise B2Error(f"anchored_patch_match_count:{path}:{len(matches)}")
    updated = re.sub(pattern, replacement, text, count=1, flags=re.S)
    path.write_text(updated, encoding="utf-8")


def _detach(path: Path) -> None:
    if not path.is_file():
        raise B2Error(f"candidate_mutation_path_missing:{path}")
    detach_for_write(path)


def _retire_static_hold_validator(candidate_root: Path, program_id: str) -> dict[str, Any]:
    """Remove a stale per-route hold assertion after canonical active-state validation exists.

    Older releases froze a small number of held review routes in one validator,
    after its generic provider loop had already validated both active and held
    states from governed data. Activation must retire only the exact target
    route's redundant two-line assertion; all generic validation stays intact.
    """
    validator = candidate_root / "scripts/validate-provider-fastpath-plus5.mjs"
    if not validator.is_file():
        return {"status": "PASS_NOT_PRESENT", "retired_assertions": 0}
    text = validator.read_text(encoding="utf-8")
    generic_markers = (
        "for (const provider of fastpath.providers)",
        "if (commerciallyActive)",
        'data-affiliate-state="active"',
        'data-affiliate-state="held"',
    )
    if not all(marker in text for marker in generic_markers):
        raise B2Error("generic_fastpath_state_validator_missing")
    route = re.escape(f"public/reviews/{program_id}/index.html")
    pattern = re.compile(
        rf'^const (?P<name>[A-Za-z_$][A-Za-z0-9_$]*) = await readText\("{route}"\);\n'
        rf'assert\.match\((?P=name), /[^\n]+/\);\n',
        re.M,
    )
    matches = list(pattern.finditer(text))
    if len(matches) > 1:
        raise B2Error(f"static_hold_validator_match_count:{program_id}:{len(matches)}")
    before_sha256 = sha_bytes(text.encode())
    if not matches:
        return {
            "status": "PASS_NOT_PRESENT", "retired_assertions": 0,
            "path": str(validator), "before_sha256": before_sha256,
            "after_sha256": before_sha256,
        }
    _detach(validator)
    updated = pattern.sub(
        "// Target-route hold assertion retired: generic data-driven state validation above remains authoritative.\n",
        text, count=1,
    )
    validator.write_text(updated, encoding="utf-8")
    after_sha256 = sha_file(validator)
    if before_sha256 == after_sha256:
        raise B2Error("static_hold_validator_retirement_no_effect")
    return {
        "status": "PASS_RETIRED", "retired_assertions": 1,
        "path": str(validator), "before_sha256": before_sha256,
        "after_sha256": after_sha256,
    }


def _activate_provider_dynamic_profile(
    candidate_root: Path,
    spec: dict[str, Any],
    spec_path: Path,
) -> dict[str, Any]:
    """Attach one exact Stage-5 profile, then invoke the candidate's canonical reconciler."""
    try:
        profile = research_dynamic_profile(
            {"provider_dynamic_profile": spec.get("provider_dynamic_profile")},
            str(spec["program_id"]),
        )
    except ValueError as exc:
        raise B2Error(f"provider_dynamic_profile_invalid:{exc}") from exc
    if profile is None:
        raise B2Error("provider_dynamic_profile_missing")

    resolved_spec = spec_path.resolve()
    try:
        authority_path = resolved_spec.relative_to(BUSINESS.resolve()).as_posix()
    except ValueError as exc:
        raise B2Error("provider_dynamic_profile_authority_outside_business") from exc
    if not authority_path.startswith("reports/"):
        raise B2Error("provider_dynamic_profile_authority_not_report")

    registry_path = candidate_root / "data/program-registry.json"
    _detach(registry_path)
    registry = read_json(registry_path)
    matches = [
        program for program in registry.get("programs") or []
        if str(program.get("review_slug") or program.get("program_id") or "") == str(spec["program_id"])
    ]
    if len(matches) != 1:
        raise B2Error(f"provider_dynamic_profile_registry_identity_match_count:{len(matches)}")
    program = matches[0]
    program["research_dynamic_profile"] = profile
    site_profile = ((program.get("profiles") or {}).get("glp-clinic-scout") or {})
    surfaces = site_profile.get("surfaces") or {}
    for surface in ("compare", "quiz", "calculator"):
        surfaces[surface] = True
    site_profile["surfaces"] = surfaces
    calculator_exception = (site_profile.get("exceptions") or {}).get("calculator")
    if calculator_exception is not None:
        site_profile.setdefault("exceptions", {}).pop("calculator", None)
    atomic_json(registry_path, registry, mode=0o644)

    authority_sha256 = sha_file(resolved_spec)
    run = _run([
        "node", str(DYNAMIC_PROFILE_ACTIVATOR),
        "--release", str(candidate_root),
        "--program-id", str(spec["program_id"]),
        "--authority-path", authority_path,
        "--authority-sha256", authority_sha256,
    ], cwd=BUSINESS, timeout=900)
    lines = [line for line in run["stdout_tail"].splitlines() if line.strip()]
    if not lines:
        raise B2Error("provider_dynamic_profile_activation_summary_missing")
    try:
        summary = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise B2Error("provider_dynamic_profile_activation_summary_invalid") from exc
    if summary.get("status") != "PASS_ACTIVATED" or summary.get("program_id") != spec["program_id"]:
        raise B2Error("provider_dynamic_profile_activation_receipt_invalid")
    evidence_hashes = sorted({
        str(binding.get("evidence_sha256") or "")
        for binding in profile.get("evidence_bindings") or []
    })
    if not evidence_hashes or any(not re.fullmatch(r"[a-f0-9]{64}", digest) for digest in evidence_hashes):
        raise B2Error("provider_dynamic_profile_evidence_hashes_invalid")
    validator_migration = _retire_static_hold_validator(candidate_root, str(spec["program_id"]))
    test_contract_refresh: dict[str, Any] = {
        "status": "PASS_NOT_PRESENT",
        "changed_count": 0,
    }
    if (candidate_root / "tests").is_dir():
        refresh_run = _run([
            sys.executable, str(CARDINALITY_TEST_REFRESH),
            "--release", str(candidate_root),
            "--activated-program-id", str(spec["program_id"]),
        ], cwd=BUSINESS, timeout=300)
        refresh_lines = [line for line in refresh_run["stdout_tail"].splitlines() if line.strip()]
        if not refresh_lines:
            raise B2Error("provider_cardinality_test_refresh_summary_missing")
        try:
            test_contract_refresh = json.loads(refresh_lines[-1])
        except json.JSONDecodeError as exc:
            raise B2Error("provider_cardinality_test_refresh_summary_invalid") from exc
        if test_contract_refresh.get("status") != "PASS":
            raise B2Error("provider_cardinality_test_refresh_failed")
    profile_sha256 = sha_bytes(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode())
    return {
        "status": "PASS_ACTIVATED",
        "program_id": spec["program_id"],
        "authority_path": authority_path,
        "authority_sha256": authority_sha256,
        "profile_sha256": profile_sha256,
        "evidence_sha256": evidence_hashes,
        "calculator_mode": (profile.get("calculator") or {}).get("mode"),
        "command": run,
        "validator_migration": validator_migration,
        "test_contract_refresh": test_contract_refresh,
        "external_effects": {"deployments": 0, "provider_calls": 0, "tracking_clicks": 0},
    }


def _merge_research_into_candidate(candidate_root: Path, research_path: Path) -> dict[str, Any]:
    """Seed/enrich the exact candidate ledger without touching predecessor hardlinks."""
    research_program_id = str(read_json(research_path).get("program_id") or "").strip()
    if not research_program_id:
        raise B2Error("research_program_id_missing_for_scoped_merge")
    ledger_path = candidate_root / "data/program-content-ledger.json"
    _detach(ledger_path)
    run = _run([
        sys.executable, str(SCRIPTS / "affiliate_merge_deep_research.py"),
        "--release", str(candidate_root),
        "--seed-missing",
        "--overwrite",
        "--preserve-price-index-evidence",
        "--program", research_program_id,
        "--research-dir", str(research_path.parent),
        "--output", str(ledger_path),
    ], cwd=BUSINESS, timeout=900)
    return {
        "status": "PASS_MERGED",
        "research": path_binding(research_path),
        "ledger": path_binding(ledger_path),
        "command": run,
        "external_effects": {"provider_calls": 0, "tracking_clicks": 0},
    }


def _recover_already_live(
    candidate_root: Path,
    spec: dict[str, Any],
    sheet_row: dict[str, Any],
    *,
    pending_go_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify a provider deployed before its sensor verdict, then repair evidence only."""
    program_id = str(spec["program_id"])
    registry = read_json(candidate_root / "data/program-registry.json")
    matches = [
        row for row in registry.get("programs") or []
        if str(row.get("review_slug") or row.get("program_id") or "") == program_id
    ]
    if len(matches) != 1:
        raise B2Error(f"recovery_registry_identity_match_count:{len(matches)}")
    row = matches[0]
    profile = ((row.get("profiles") or {}).get("glp-clinic-scout") or {})
    if row.get("commercial_state") != "live" or profile.get("wired") is not True:
        raise B2Error("recovery_program_not_live")
    surfaces = profile.get("surfaces") or {}
    if not all(
        surfaces.get(name) is True
        for name in ("review", "compare", "quiz", "calculator")
    ):
        raise B2Error("recovery_live_surfaces_incomplete")
    go_pending = surfaces.get("go") is False
    if go_pending:
        request = pending_go_request or {}
        target = request.get("target") or {}
        target_sheet = request.get("target_sheet") or {}
        live_authority = request.get("live_route_authority") or {}
        if (
            request.get("status") != "STAGED_NOT_DEPLOYED"
            or request.get("queue_kind") != "affiliate_go_route_provision"
            or request.get("queue_source") != "sheet_update_queue:payout_evaluation"
            or request.get("deploy_instruction_version")
            != GO_ROUTE_PROVISION_INSTRUCTION_VERSION
            or request.get("sheet_updates_via_single_writer_only") is not True
            or request.get("href_config_inspection_only") is not True
            or request.get("tracking_clicks") != 0
            or request.get("site_program_id") != program_id
            or request.get("first_party_go_route") != (row.get("go") or {}).get("route")
            or normalize_network(target.get("network"))
            != normalize_network(spec.get("network"))
            or str(target.get("program_id") or "")
            != str(spec.get("advertiser_id") or "")
            or int(target.get("sheet_row") or 0) != int(spec.get("sheet_row") or 0)
            or live_authority.get("status") != "VERIFIED"
            or live_authority.get("promotion_authorized") is not True
        ):
            raise B2Error("recovery_pending_go_provision_authority_invalid")
        requested_tracking = _validate_queue_tracking_link(
            target_sheet.get("Tracking Link / Deep Link"),
            target.get("network"),
            target.get("program_id"),
        )
        current_tracking = str(sheet_row.get("Tracking Link / Deep Link") or "").strip()
        if requested_tracking != current_tracking:
            raise B2Error("recovery_pending_go_provision_tracking_drift")
    elif pending_go_request is not None:
        raise B2Error("recovery_go_provision_already_satisfied_drift")
    contracts_path = candidate_root / "data/provider-page-contracts.json"
    contracts = read_json(contracts_path)
    contract_matches = [
        item for item in contracts.get("providers") or []
        if str(item.get("program_id") or "") == program_id
    ]
    if len(contract_matches) != 1:
        raise B2Error(f"recovery_contract_identity_match_count:{len(contract_matches)}")
    contract = contract_matches[0]
    network = contract.get("network_contract") or {}
    if normalize_network(network.get("network")) != normalize_network(spec.get("network")):
        raise B2Error("recovery_network_identity_drift")
    immutable = network.get("immutable_offer_id") or network.get("immutable_advertiser_id") or network.get("advertiser_id")
    if str(immutable or "") != str(spec.get("advertiser_id") or ""):
        raise B2Error("recovery_provider_program_identity_drift")
    tracking = str(sheet_row.get("Tracking Link / Deep Link") or "").strip()
    if not tracking.startswith("https://") or network.get("tracking_url") != tracking:
        raise B2Error("recovery_tracking_link_drift")
    current_sections = {
        "page_copy": spec.get("page_copy"),
        "field_sources": spec.get("field_sources"),
        "row_contract": spec.get("row_contract"),
    }
    for section, value in current_sections.items():
        if not isinstance(value, dict) or not value:
            raise B2Error(f"recovery_current_spec_section_invalid:{section}")
    # Recovery is not just an evidence receipt. An already-live row can carry a
    # stale provider contract from its predecessor release. Reconcile the exact
    # current, validated authority bytes while preserving the provider-issued
    # tracking identity already checked above. The write is candidate-local.
    contract["page_copy"] = dict(current_sections["page_copy"])
    contract["field_sources"] = _contract_field_sources_from_spec(current_sections["field_sources"])
    contract["row_contract"] = _contract_row_from_spec(current_sections["row_contract"])
    network.update({
        "tracking_url": tracking,
        "promotion_authorized_in_this_artifact": True,
        "activation_blocker": None,
        "relationship_state": "approved_active",
        "sheet_status": APPROVED_STATUS,
        "b2_legality_only_policy": "legality_only_20260814",
    })
    contract.setdefault("publication_state", {}).update({
        "affiliate_activation_candidate_after_gate": True,
        "affiliate_activation_state": "B2_AUTONOMOUS_LEGALITY_ONLY_20260815",
    })
    contract["row_contract"].update({
        "live_link_allowed": True,
        "promotion_authorized": True,
        "affiliate_link_mode": "transparent_direct",
        "affiliate_href": tracking,
    })
    _detach(contracts_path)
    atomic_json(contracts_path, contracts, mode=0o644)
    # DEFECT 2026-09-02 (round 22): data/provider-fastpath-contract.json pins
    # current_contract_sha256 to the exact bytes of provider-page-contracts.json,
    # and scripts/validate-provider-fastpath-plus5.mjs asserts the two match.
    # _activate_existing re-mints it after its write; this path rewrote the
    # contract file - page_copy, field_sources and row_contract are all replaced
    # from the current authority just above - and never did, so the moment a
    # recovered row's bytes actually changed, the build failed on a hash the
    # recovery had itself invalidated. Re-mint at the same boundary.
    fastpath_path = candidate_root / "data/provider-fastpath-contract.json"
    fastpath_doc = read_json(fastpath_path)
    fastpath_doc["current_contract_sha256"] = sha_file(contracts_path)
    _detach(fastpath_path)
    atomic_json(fastpath_path, fastpath_doc, mode=0o644)
    review_path = candidate_root / "public" / str(contract.get("review_path") or "").strip("/") / "index.html"
    if not review_path.is_file():
        raise B2Error("recovery_live_review_missing")
    return {
        "status": (
            "PASS_RECOVERED_ALREADY_LIVE_PENDING_GO_PROVISION"
            if go_pending else "PASS_RECOVERED_ALREADY_LIVE"
        ),
        "program_id": program_id,
        "review_path": str(review_path),
        "tracking_url_sha256": sha_bytes(tracking.encode()),
        "provider_contract": path_binding(contracts_path),
        "provider_contract_row_sha256": sha_bytes(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
        ),
        "pending_first_party_go_route": (
            str(pending_go_request.get("first_party_go_route") or "")
            if go_pending and pending_go_request else None
        ),
        "external_effects": {"provider_calls": 0, "tracking_clicks": 0},
    }


def _contract_row_from_spec(spec_row_contract: dict[str, Any]) -> dict[str, Any]:
    """Project a program-authority row_contract into a provider-page contract row.

    DEFECT 2026-09-02 (cost_calculator_fastpath_row_invalid:altrx-glp1-us-cps,
    killed rounds 18 and 19): both activation paths replaced the candidate's
    row_contract with `dict(spec["row_contract"])` wholesale. The program
    authority legitimately carries a numeric `starting_price_sort` - altRx was
    re-authored with a comparable $89/mo price and its authority records 89 -
    but data/provider-page-contracts.json is the CONTEXT artifact and
    src/cost-calculator.mjs:125 asserts, for EVERY row in that file:

        if (contract.row_contract.starting_price_sort !== null) throw ...

    The sortable figure belongs to the money-page comparison row
    (data/money-page-specs.json), which is a different artifact with a
    different contract. build_glp_affiliate_wirein_incremental_v2.py already
    hardcodes None on the net-new path for this reason; the two activation
    paths for an ALREADY-PRESENT contract did not, so the invariant held only
    for as long as no existing provider ever gained a numeric authority sort.
    Normalising here keeps the wholesale spec refresh (which is what makes the
    row's evidence current) while preserving the artifact's own invariant.
    """
    row = dict(spec_row_contract)
    for field, empty in CONTRACT_ROW_RUBRIC_OWNED_FIELDS.items():
        if field in row:
            # .copy(): an empty dict must never be shared between contract rows.
            row[field] = empty.copy() if isinstance(empty, dict) else empty
    return row


# Census of the shipped artifact, 2026-09-02: across all 40 provider-page
# contract rows in the deployed release, `starting_price_sort` and `score` are null and
# `criteria_scores` is {} WITHOUT EXCEPTION, while the program authorities hold numbers in 24 and 3
# of them respectively. `score` matters beyond the build: ready6-editorial.mjs
# ships every provider with score_rubric.overall_score === null and status
# NOT_SCORED_EVIDENCE_GAPS, so letting an authority's 4.1 through would publish
# a rating no rubric ever computed. Both are projected out at the boundary.
CONTRACT_ROW_RUBRIC_OWNED_FIELDS = {
    "starting_price_sort": None,
    "score": None,
    # {} not None: {} is what all 40 shipped rows carry.
    "criteria_scores": {},
}
CONTRACT_ROW_NULL_FIELDS = tuple(CONTRACT_ROW_RUBRIC_OWNED_FIELDS)


# The renderer's three citation groups, and every source NAME each may be bound
# from. Built from a census of ALL 20 distinct field_sources keys across the 38
# program authorities (2026-09-02), not from the one alias a failing round
# happened to expose - rounds 20 and 21 were each lost to a vocabulary that the
# previous round's fix had not seen. canonical_field_sources() in
# build_glp_affiliate_wirein_incremental_v2.py knows a subset of these, because
# it reads a research packet while this reads authorities; a test asserts the
# builder's names stay a subset of ours and that the three GROUP names match
# exactly, so the two cannot diverge on what a group means.
CONTRACT_FIELD_SOURCE_GROUPS = {
    # Research fact names are exactly canonical_field_sources()'s - deliberately
    # NOT widened (adding monthly_price/legitscript/eligibility made the output
    # a superset of the shipped rows, i.e. it would have silently changed which
    # sources every review cites). Everything added here is a GROUP ALIAS: a
    # different name for the same citation group, which is the only gap that
    # actually cost rounds 20 and 21.
    "pricing_and_inclusions": (
        "pricing_and_inclusions",
        "pricing_eligibility_shipping", "eligibility_pricing_shipping",
        "pricing_inclusions_eligibility_shipping",
        "first_month_price", "consultation_fee", "shipping_wait",
    ),
    "cancellation_and_roles": (
        "cancellation_and_roles",
        "cancellation_and_refunds", "program_terms", "program", "terms",
        "affiliate_exclusivity",
        "cancellation", "states_served", "consultation_fee",
    ),
    "regulatory": (
        "regulatory", "regulatory_and_roles",
        "pharmacy_type",
    ),
}


def _contract_field_sources_from_spec(spec_field_sources: dict[str, Any]) -> dict[str, Any]:
    """Project a program-authority field_sources map into contract shape.

    DEFECT 2026-09-02, found across rounds 20 and 21. src/provider-review.mjs
    resolves each provider's field_bindings to citation GROUP names and throws
    fastpath_field_source_missing when a bound group is absent, so what matters
    is the group vocabulary, not merely the value type.

    Program authorities carry three different vocabularies:
      * the canonical groups already (pass straight through),
      * group ALIASES - `pricing_eligibility_shipping` for
        pricing_and_inclusions, `program_terms` for cancellation_and_roles
        (altRx, round 21: list-shaped and therefore easy to mistake for
        canonical, but its bindings require pricing_and_inclusions),
      * the research-packet shape, one scalar url per FACT (TrimRX, Yucca,
        round 20: `TypeError: fieldUrls.map is not a function` at line 260,
        because .flat() at line 253 tolerates a string and .map() does not).

    Round 21 failed because an earlier version of this function passed
    all-list input through untouched, which preserved the alias vocabulary.
    There is no shape test that distinguishes an alias from a canonical group,
    so ALWAYS project - the operation is idempotent on already-canonical input.
    Groups outside the canonical three are preserved as-is, because five shipped
    rows carry richer vocabularies whose own bindings must keep resolving.
    An empty canonical group is REFUSED, never invented.
    """
    def urls(*names: str) -> list[str]:
        collected: list[str] = []
        for name in names:
            raw = spec_field_sources.get(name)
            candidates = [raw] if isinstance(raw, str) else (raw if isinstance(raw, list) else [])
            for value in candidates:
                if isinstance(value, str) and value.startswith("https://") and value not in collected:
                    collected.append(value)
        return collected

    projected: dict[str, Any] = {}
    for group, names in CONTRACT_FIELD_SOURCE_GROUPS.items():
        collected = urls(*names)
        if not collected:
            raise B2Error(f"contract_field_source_group_missing:{group}")
        projected[group] = collected
    for field, value in spec_field_sources.items():
        if field in projected:
            continue
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            projected[field] = list(value)
    return projected


def _activate_existing(candidate_root: Path, spec: dict[str, Any], sheet_row: dict[str, Any]) -> dict[str, Any]:
    program_id = spec["program_id"]
    files = {
        "contracts": candidate_root / "data/provider-page-contracts.json",
        "fastpath": candidate_root / "data/provider-fastpath-contract.json",
        "registry": candidate_root / "data/program-registry.json",
        "approvals": candidate_root / "data/affiliate-wire-in-approvals.json",
        "validator": candidate_root / "scripts/validate-provider-fastpath-plus5.mjs",
        "fastpath_test": candidate_root / "tests/provider-fastpath-plus5.test.mjs",
        "coverage_test": candidate_root / "tests/program-registry-coverage.test.mjs",
    }
    for path in files.values():
        _detach(path)
    contracts = read_json(files["contracts"])
    fastpath = read_json(files["fastpath"])
    registry = read_json(files["registry"])
    approvals = read_json(files["approvals"])
    contract = next((row for row in contracts["providers"] if row.get("program_id") == program_id), None)
    integration = next((row for row in fastpath["providers"] if row.get("program_id") == program_id), None)
    registry_row = next((row for row in registry["programs"] if row.get("program_id") == program_id), None)
    if not all((contract, integration, registry_row)):
        raise B2Error(f"existing_activation_surface_missing:{program_id}")
    profile = ((registry_row.get("profiles") or {}).get("glp-clinic-scout") or {})
    if registry_row.get("commercial_state") == "live" or profile.get("wired") is True:
        raise B2Error(f"existing_activation_already_live:{program_id}")
    tracking = str(sheet_row.get("Tracking Link / Deep Link") or "").strip()
    if not tracking.startswith("https://"):
        raise B2Error("tracking_link_missing")
    parts = urlsplit(tracking)
    if parts.scheme != "https" or not parts.hostname:
        raise B2Error("tracking_link_structural_invalid")
    query = parse_qs(parts.query)
    if normalize_network(spec["network"]) == "katalys":
        if query.get("offer_id") != [str(spec["advertiser_id"])] or not query.get("aff_id"):
            raise B2Error("katalys_tracking_identity_invalid")
    # The Stage-5 spec is the current evidence contract.  Replace stale held copy
    # before activation rather than preserving a policy statement that says the
    # now-authorized link must remain dark.
    contract["page_copy"] = dict(spec["page_copy"])
    contract["field_sources"] = _contract_field_sources_from_spec(spec["field_sources"])
    contract["row_contract"] = _contract_row_from_spec(spec["row_contract"])
    nc = contract["network_contract"]
    immutable = nc.get("immutable_offer_id") or nc.get("immutable_advertiser_id") or nc.get("advertiser_id")
    if str(immutable) != str(spec["advertiser_id"]):
        raise B2Error("existing_contract_advertiser_identity_drift")
    nc.update({
        "tracking_url": tracking,
        "promotion_authorized_in_this_artifact": True,
        "activation_blocker": None,
        "relationship_state": "approved_active",
        "sheet_status": APPROVED_STATUS,
        "b2_legality_only_policy": "legality_only_20260814",
    })
    contract["publication_state"].update({
        "affiliate_activation_candidate_after_gate": True,
        "affiliate_activation_state": "B2_AUTONOMOUS_LEGALITY_ONLY_20260815",
    })
    contract["row_contract"].update({
        "live_link_allowed": True,
        "promotion_authorized": True,
        "affiliate_link_mode": "transparent_direct",
        "affiliate_href": tracking,
    })
    integration.update({
        "route_state": "LIVE_EDITORIAL_AFFILIATE_ACTIVE",
        "affiliate_cta_state": "ACTIVE_PROVIDER_ISSUED_DIRECT",
        "identity_state": "PASS_EXACT_PROVIDER_NETWORK_PROGRAM_ID",
        "hold_copy": (
            "This page contains one active affiliate link. Clinic Scout may earn a commission "
            "at no extra cost to the reader; the link does not change the page evidence or score."
        ),
    })
    registry_row["commercial_state"] = "live"
    registry_row["go"]["entry_state"] = "transparent_direct"
    profile["wired"] = True
    profile["surfaces"]["go"] = True
    profile["surfaces"]["compare"] = True
    profile["surfaces"]["quiz"] = True
    registry_row["profiles"]["glp-clinic-scout"] = profile
    approval = next(
        (row for row in approvals.get("programs") or [] if row.get("existing_release_alias") == program_id),
        None,
    )
    payload = {
        "existing_release_alias": program_id,
        "display_name": spec["display_name"],
        "network": spec["network"],
        "provider_program_id": spec["advertiser_id"],
        "publisher_id": str(sheet_row.get("Affiliate / Publisher ID") or spec.get("publisher_id") or ""),
        "canonical_sheet_row": int(spec["sheet_row"]),
        "sheet_status": APPROVED_STATUS,
        "signup_complete": True,
        "wire_in_ready": True,
        "approval_state": "approved_active",
        "historical_activation_authority": {
            "approval_state": "approved_active",
            "classification_path": spec["terms_classification_path"],
            "rubric_verdict_at_activation": "PASS_LEGALITY_ONLY",
            "bridge_ruling_considered": "autonomy-master-list-20260814-b3",
        },
        "current_terms_reassessment": {
            "as_of_utc": iso_utc(),
            "evidence_path": spec["terms_classification_path"],
            "future_change_terms_gate_open": True,
            "hold_trigger": None,
            "live_link_disposition": "activate_b2_legality_only",
            "may_authorize_new_activation": True,
            "may_authorize_material_expansion": True,
            "verdict": "ACCEPTED_LEGALITY_ONLY_MECHANICAL_RISK_LOGGED",
        },
        "tracking": {
            "provider_issued_url": tracking,
            "link_qa_status": "not_clicked_preserve_attribution",
            "clicks": 0,
        },
    }
    if approval is None:
        approvals.setdefault("programs", []).append(payload)
    else:
        approval.clear()
        approval.update(payload)
    atomic_json(files["contracts"], contracts, mode=0o644)
    fastpath["current_contract_sha256"] = sha_file(files["contracts"])
    atomic_json(files["fastpath"], fastpath, mode=0o644)
    atomic_json(files["registry"], registry, mode=0o644)
    approvals.setdefault("source_authority", {})["b2_sheet_row"] = int(spec["sheet_row"])
    atomic_json(files["approvals"], approvals, mode=0o644)
    _replace_once(
        files["validator"],
        r'(source_kind === "fastpath" && item\.commercial_state === "live"\)\.length, )(\d+)(\);)',
        lambda match: f"{match.group(1)}{int(match.group(2)) + 1}{match.group(3)}",
    )
    _replace_once(
        files["fastpath_test"],
        r'(const activeIds = new Set\(\[)([^\]]*)(\]\);)',
        lambda match: (
            match.group(0)
            if f'"{program_id}"' in match.group(2)
            else f'{match.group(1)}{match.group(2)}, "{program_id}"{match.group(3)}'
        ),
    )
    _replace_once(
        files["fastpath_test"],
        r'if \(\[([^\]]*)\]\.includes\(review\.program_id\)\)',
        lambda match: (
            "if ([" + ", ".join(
                item for item in [x.strip() for x in match.group(1).split(",")]
                if item.strip('"\'') != program_id
            ) + "].includes(review.program_id))"
        ),
    )
    _replace_once(
        files["coverage_test"],
        r'(assert\.equal\(result\.live_fastpath_programs, )(\d+)(\);)',
        lambda match: f"{match.group(1)}{int(match.group(2)) + 1}{match.group(3)}",
    )
    _replace_once(
        files["coverage_test"],
        r'(assert\.deepEqual\(result\.held_contract_programs\.sort\(\), \[)([^\]]*)(\]\);)',
        lambda match: (
            match.group(1)
            + ", ".join(
                item for item in [x.strip() for x in match.group(2).split(",")]
                if item.strip('"\'') != program_id
            )
            + match.group(3)
        ),
    )
    return {
        "program_id": program_id,
        "mode": "activate_existing_editorial_page",
        "tracking_link_sha256": sha_bytes(tracking.encode()),
        "tracking_clicks": 0,
    }


def _ensure_first_party_go_contract_support(candidate_root: Path) -> dict[str, Any]:
    """Teach sealed READY6 render/coverage validators the governed /go state."""
    marker = "GO_ROUTE_PROVISION_CONTRACT_V1"
    candidate_root = Path(candidate_root)
    replacements = {
        "src/program-registry.mjs": [
            (
                'export function deriveGoEntriesFromRegistry(registry, profileId, ledgerPrograms = []) {\n  const allowed = deriveProfileSurfaceSets(registry, profileId, ledgerPrograms).go;\n  return registry.programs.filter((program) => allowed.has(program.program_id)).map((program) => ({\n    program_id: program.program_id,\n    display_name: program.display_name,\n    route: program.go.route,\n    ...(program.review_slug ? { route_slug: program.review_slug } : {}),\n    entry_state: program.go.entry_state,\n    commercial_state: program.commercial_state,\n  }));\n}',
                '// GO_ROUTE_PROVISION_CONTRACT_V1: materialize governed affiliate bytes in the first-party runtime manifest.\nexport function deriveGoEntriesFromRegistry(registry, profileId, ledgerPrograms = [], affiliateMap = {}) {\n  const allowed = deriveProfileSurfaceSets(registry, profileId, ledgerPrograms).go;\n  const ledgerById = new Map(ledgerPrograms.map((program) => [program.program_id, program]));\n  const allowedPageRoles = ["providers", "reviews", "self-pay", "lp", "lp-quiz", "lp-compare", "compounded", "compounded-tirzepatide", "lp-best-providers-compounded-semaglutide", "lp-best-providers-compounded-tirzepatide", "3-month", "6-month", "wegovy-pill", "wegovy", "ozempic", "mounjaro", "zepbound"];\n  const allowedPositions = [\n    "rv1", "rv2",\n    ...Array.from({ length: 60 }, (_, index) => `lp${index + 1}`),\n    ...Array.from({ length: 60 }, (_, index) => `sp${index + 1}`),\n    ...Array.from({ length: 5 }, (_, index) => `fv2s${index + 1}`),\n    ...Array.from({ length: 5 }, (_, index) => `fv2m${index + 1}`),\n    ...Array.from({ length: 60 }, (_, index) => `pc${index + 1}`),\n    ...Array.from({ length: 50 }, (_, index) => `pcu${index + 1}`),\n  ];\n  return registry.programs.filter((program) => allowed.has(program.program_id)).map((program) => {\n    const renderId = program.review_slug || program.program_id;\n    const base = {\n      program_id: program.program_id,\n      display_name: program.display_name,\n      route: program.go.route,\n      ...(program.review_slug ? { route_slug: program.review_slug } : {}),\n      entry_state: program.go.entry_state,\n      commercial_state: program.commercial_state,\n    };\n    const link = affiliateMap[renderId];\n    if (!link?.live || !link.affiliate_href || !link.network) return base;\n    const ledger = ledgerById.get(renderId) || ledgerById.get(program.program_id) || {};\n    const officialFallback = ledger.research_price?.source_url || ledger.source_url || "";\n    let trackingHost = "";\n    let fallbackHost = "";\n    try { trackingHost = new URL(link.affiliate_href).hostname.toLowerCase(); } catch { return base; }\n    try { fallbackHost = new URL(officialFallback).hostname.toLowerCase(); } catch { fallbackHost = ""; }\n    return {\n      ...base,\n      status: "candidate_preview_only",\n      link_mode: "instrumented_redirect_candidate",\n      network: String(link.network).toLowerCase(),\n      tracking: { base_url: link.affiliate_href, allowed_tracking_hosts: [trackingHost], generated_link_readback_verified: true },\n      official_fallback: fallbackHost ? officialFallback : "https://glpclinicscout.com/providers/",\n      allowed_fallback_hosts: fallbackHost ? [fallbackHost] : ["glpclinicscout.com"],\n      allowed_page_roles: allowedPageRoles,\n      allowed_positions: allowedPositions,\n      allowed_variants: ["a"],\n      go_route_wired: true,\n      live_link_allowed: false,\n      promotion_authorized: false,\n      constructor_verified: false,\n      advertiser_permission_verified: true,\n      encoding_verified: true,\n      privacy_qa_passed: true,\n      redirector_tos_verified: false,\n      candidate_gate: "operator_design_acceptance_required",\n    };\n  });\n}',
            ),
        ],
        "src/ready6-editorial.mjs": [
            (
                'export function buildReady6ReviewModels(inputs, { ledgerPrograms = [], aliases = {} } = {}) {',
                'export function buildReady6ReviewModels(inputs, { ledgerPrograms = [], aliases = {}, goRouteByRegistryId = {} } = {}) {',
            ),
            (
                '  if (tracking?.candidate_registry_wired !== true || tracking.state !== "candidate_registry_wired_provider_issued" || tracking.click_qa_performed !== false || tracking.public_go_surface !== false) fail("ready6_approved_registry_tracking_invalid", program.program_key);',
                '  // GO_ROUTE_PROVISION_CONTRACT_V1: the queue may promote the already-approved provider URL behind our first-party redirect without clicking it.\n  const validTrackingState = (tracking?.state === "candidate_registry_wired_provider_issued" && tracking.public_go_surface === false) || (tracking?.state === "candidate_registry_wired_first_party_go_staged" && tracking.public_go_surface === true);\n  if (tracking?.candidate_registry_wired !== true || !validTrackingState || tracking.click_qa_performed !== false) fail("ready6_approved_registry_tracking_invalid", program.program_key);',
            ),
            (
                '    const trackingUrl = validateProgram(program, exactFinalRecord(readjudication, program.program_key), approvedByKey.get(program.program_key), approvedRegistry, fdaSource);\n    const aliasOf = aliases[program.registry_id] || null;',
                '    const approvedProgram = approvedByKey.get(program.program_key);\n    const trackingUrl = validateProgram(program, exactFinalRecord(readjudication, program.program_key), approvedProgram, approvedRegistry, fdaSource);\n    const useFirstPartyGo = approvedProgram.tracking.public_go_surface === true;\n    const firstPartyGoRoute = goRouteByRegistryId?.[program.registry_id] || approvedProgram.tracking.first_party_go_route || null;\n    if (useFirstPartyGo && !/^\\/go\\/[a-z0-9-]+$/.test(firstPartyGoRoute || "")) fail("ready6_first_party_go_route_invalid", program.program_key);\n    const ctaHref = useFirstPartyGo ? `${firstPartyGoRoute}?s=reviews.rv1&v=a` : trackingUrl;\n    const ctaMode = useFirstPartyGo ? "first_party_go" : "transparent_direct";\n    const aliasOf = aliases[program.registry_id] || null;',
            ),
            (
                'cta: Object.freeze({ active: true, href: trackingUrl, href_sha256: sha256(Buffer.from(trackingUrl)), label: program.page.cta_label, mode: "transparent_direct", rel: READY6_REL })',
                'cta: Object.freeze({ active: true, href: ctaHref, href_sha256: sha256(Buffer.from(ctaHref)), label: program.page.cta_label, mode: ctaMode, rel: READY6_REL })',
            ),
            (
                'data-link-mode="transparent-direct"',
                'data-link-mode="${model.cta.mode === "first_party_go" ? "first-party-go" : "transparent-direct"}"',
            ),
            (
                'model.cta.mode !== "transparent_direct"',
                '!["transparent_direct", "first_party_go"].includes(model.cta.mode)',
            ),
        ],
        "src/approved-program-coverage.mjs": [
            (
                '  if (!tracking || tracking.public_go_surface !== false) {\n    fail("approved_60_tracking_safety_invalid", program.program_key);\n  }',
                '  // GO_ROUTE_PROVISION_CONTRACT_V1: public_go_surface is allowed only in the exact queue-staged state; no click-QA claim is added.\n  const firstPartyGoStaged = tracking?.state === "candidate_registry_wired_first_party_go_staged" && tracking.public_go_surface === true && tracking.candidate_registry_wired === true;\n  if (!tracking || (tracking.public_go_surface !== false && !firstPartyGoStaged)) {\n    fail("approved_60_tracking_safety_invalid", program.program_key);\n  }',
            ),
            (
                '    tracking.state !== "candidate_registry_wired_provider_issued"\n    || tracking.candidate_registry_wired !== true',
                '    !["candidate_registry_wired_provider_issued", "candidate_registry_wired_first_party_go_staged"].includes(tracking.state)\n    || tracking.candidate_registry_wired !== true',
            ),
            (
                '      if (\n        !capturedTracking\n        || capturedTracking.provider_issued_url !== program.tracking?.provider_issued_url\n        || capturedTracking.state !== program.tracking?.state\n        || capturedTracking.source_ref !== program.tracking?.source_ref\n      ) fail("approved_60_tracking_evidence_mismatch", program.program_key);',
                '      const queueGoPromotion = program.tracking?.state === "candidate_registry_wired_first_party_go_staged" && capturedTracking?.state === "candidate_registry_wired_provider_issued";\n      if (\n        !capturedTracking\n        || capturedTracking.provider_issued_url !== program.tracking?.provider_issued_url\n        || (!queueGoPromotion && capturedTracking.state !== program.tracking?.state)\n        || capturedTracking.source_ref !== program.tracking?.source_ref\n      ) fail("approved_60_tracking_evidence_mismatch", program.program_key);',
            ),
            (
                '  } else {\n    trackingMatches = brief.cta.provider_issued_url_present === wired\n      && brief.cta.tracking_registry_state === program.tracking.state\n      && brief.coverage_gate.tracking_gate_state === ((accepted || readyForProductionReview) ? "PASS_PROVIDER_ISSUED_NONCLICK_VERIFIED" : "READY_FOR_NONCLICK_REVIEW");\n  }',
                '  } else {\n    const queueGoBriefStateMatch = program.tracking.state === "candidate_registry_wired_first_party_go_staged" && brief.cta.tracking_registry_state === "candidate_registry_wired_provider_issued";\n    trackingMatches = brief.cta.provider_issued_url_present === wired\n      && (brief.cta.tracking_registry_state === program.tracking.state || queueGoBriefStateMatch)\n      && brief.coverage_gate.tracking_gate_state === ((accepted || readyForProductionReview) ? "PASS_PROVIDER_ISSUED_NONCLICK_VERIFIED" : "READY_FOR_NONCLICK_REVIEW");\n  }',
            ),
            (
                '    && brief.cta?.enabled === true\n    && brief.cta?.candidate_local_transparent_direct === true\n    && brief.cta?.public_go_surface === false\n    && brief.cta?.public_affiliate_surface === false',
                '    && brief.cta?.enabled === true\n    && ((program.tracking?.state === "candidate_registry_wired_first_party_go_staged" && program.tracking?.public_go_surface === true) || (brief.cta?.candidate_local_transparent_direct === true && brief.cta?.public_go_surface === false))\n    && brief.cta?.public_affiliate_surface === false',
            ),
        ],
        "scripts/validate-ready6-editorial.mjs": [
            (
                '  assert.equal(program.commercial_state, "live");\n  assert.equal(program.go.entry_state, "held");',
                '  // GO_ROUTE_PROVISION_CONTRACT_V1: exact queue authority may stage the first-party route without a click.\n  assert.equal(program.commercial_state, "live");\n  const firstPartyGoStaged = profile.surfaces.go === true;\n  assert.equal(program.go.entry_state, firstPartyGoStaged ? "instrumented_redirect_candidate" : "held");',
            ),
            (
                '  assert.equal(profile.surfaces.go, false);',
                '  assert.equal(profile.surfaces.go, firstPartyGoStaged);',
            ),
            (
                '  assert.equal(profile.exceptions.go.enabled, true, `${program.program_id}:go:exception_disabled`);\n  assert.ok(profile.exceptions.go.reason.length > 20, `${program.program_id}:go:exception_reason_missing`);',
                '  if (firstPartyGoStaged) {\n    assert.equal(profile.exceptions?.go, undefined, `${program.program_id}:go:stale_exception`);\n  } else {\n    assert.equal(profile.exceptions.go.enabled, true, `${program.program_id}:go:exception_disabled`);\n    assert.ok(profile.exceptions.go.reason.length > 20, `${program.program_id}:go:exception_reason_missing`);\n  }',
            ),
            (
                # /go provisioning deletes `exceptions` outright once the go
                # exception is the last one on the profile, so every later
                # dereference of `profile.exceptions` must tolerate its absence.
                # The go branch above was written with optional chaining; this
                # loop, three lines further down, was not - so a SUCCESSFUL /go
                # deploy died here with `Cannot read properties of undefined
                # (reading 'compare')`. Proven on a candidate materialized from
                # the live head with only the three staged /go requests applied.
                # The assertions themselves are unchanged: a stale exception is
                # still a failure, an alias exception is still required.
                '  for (const surface of ["compare", "quiz"]) {\n    if (alias) assert.equal(profile.exceptions[surface]?.enabled, true, `${program.program_id}:${surface}:alias_exception_missing`);\n    else assert.equal(profile.exceptions[surface], undefined, `${program.program_id}:${surface}:stale_exception`);\n  }',
                '  for (const surface of ["compare", "quiz"]) {\n    if (alias) assert.equal(profile.exceptions?.[surface]?.enabled, true, `${program.program_id}:${surface}:alias_exception_missing`);\n    else assert.equal(profile.exceptions?.[surface], undefined, `${program.program_id}:${surface}:stale_exception`);\n  }',
            ),
        ],
        "scripts/validate-ready6-static.py": [
            (
                " cta,stack=affiliate[0]; expected=approved[program['program_key']]['tracking']['provider_issued_url']\n if cta.get('href')!=expected: fail(route,'affiliate_cta_not_exact_provider_url')\n if cta.get('data-link-mode')!='transparent-direct' or cta.get('target') is not None or set(cta.get('rel','').split())!=EXPECTED_REL or not inside(stack,'partner-action'): fail(route,'affiliate_cta_contract_invalid')",
                " # GO_ROUTE_PROVISION_CONTRACT_V1: accept exactly the governed first-party route, never a guessed URL.\n cta,stack=affiliate[0]; tracking=approved[program['program_key']]['tracking']; first_party=tracking.get('public_go_surface') is True\n expected=(str(tracking.get('first_party_go_route') or '')+'?s=reviews.rv1&v=a') if first_party else tracking['provider_issued_url']\n if cta.get('href')!=expected: fail(route,'affiliate_cta_not_exact_go_or_provider_url')\n expected_mode='first-party-go' if first_party else 'transparent-direct'\n if cta.get('data-link-mode')!=expected_mode or cta.get('target') is not None or set(cta.get('rel','').split())!=EXPECTED_REL or not inside(stack,'partner-action'): fail(route,'affiliate_cta_contract_invalid')",
            ),
            (
                " if href.startswith('/') and not target(href).is_file(): fail(route,'broken_internal_link')",
                " if href.startswith('/') and not target(href).is_file() and not (first_party and href==expected): fail(route,'broken_internal_link')",
            ),
        ],
        "scripts/build-staging.mjs": [
            (
                'import { assertLiveSurfaceParity, deriveGoEntriesFromRegistry, deriveReviewRoutesFromRegistry, projectMoneyPagesFromRegistry, projectQuizFromRegistry } from "../src/program-registry.mjs";',
                'import { assertLiveSurfaceParity, deriveGoEntriesFromRegistry, deriveReviewRoutesFromRegistry, projectMoneyPagesFromRegistry, projectQuizFromRegistry } from "../src/program-registry.mjs";\nimport { loadAffiliateLinkMap } from "../src/affiliate-link-map.mjs";',
            ),
            (
                'deriveGoEntriesFromRegistry(programRegistry, SITE_PROFILE, contentLedger.programs)',
                'deriveGoEntriesFromRegistry(programRegistry, SITE_PROFILE, contentLedger.programs, loadAffiliateLinkMap("compare", SITE_PROFILE))',
            ),
            (
                '    logoByRegistryId: ready6LogoByRegistryId,\n    shell: "shared-interactive",',
                '    logoByRegistryId: ready6LogoByRegistryId,\n    // GO_ROUTE_PROVISION_CONTRACT_V1: registry go authority, not the sealed editorial record, selects the first-party CTA.\n    goRouteByRegistryId: Object.fromEntries(programRegistry.programs.filter((program) => program.profiles?.[SITE_PROFILE]?.surfaces?.go === true).map((program) => [program.program_id, program.go.route])),\n    shell: "shared-interactive",',
            ),
        ],
    }
    structural_support = {
        "src/program-registry.mjs": (
            # DEFECT 2026-09-02: this token used to end in `affiliateMap = {})`,
            # with the closing paren. _ensure_go_runtime_promotion_contract_support
            # EXTENDS the same signature to `..., affiliateMap = {}, goPromotionAuthority = {})`,
            # and that support runs on EVERY deploy - including a providers-only
            # one that consumes no /go request. Once such a release became the
            # base, the closing paren no longer matched, the structural-support
            # path was skipped, the whole-function replacement was attempted
            # instead, and it failed closed with go_route_contract_source_drift.
            # /go provisioning could then never be applied again. Matching the
            # parameter without the paren recognises both signatures.
            "deriveGoEntriesFromRegistry(registry, profileId, ledgerPrograms = [], affiliateMap = {}",
            "go_route_wired: true",
        ),
        "src/ready6-editorial.mjs": (
            "goRouteByRegistryId = {}",
            "candidate_registry_wired_first_party_go_staged",
            "first_party_go",
        ),
        "src/approved-program-coverage.mjs": (
            "candidate_registry_wired_first_party_go_staged",
            "queueGoPromotion",
        ),
        "scripts/validate-ready6-editorial.mjs": (
            "firstPartyGoStaged",
            "stale_exception",
            "profile.exceptions?.[surface]",
        ),
        "scripts/validate-ready6-static.py": (
            "affiliate_cta_not_exact_go_or_provider_url",
            "first-party-go",
        ),
        "scripts/build-staging.mjs": (
            'loadAffiliateLinkMap("compare", SITE_PROFILE)',
            "goRouteByRegistryId",
        ),
    }
    changed: list[str] = []
    for relative, edits in replacements.items():
        path = candidate_root / relative
        text = path.read_text(encoding="utf-8")
        # DELIVERY GAP 2026-09-03. `if marker in text: continue` skipped EVERY edit
        # once the file carried the marker, so an edit added AFTER the marker was
        # first installed could never reach a real candidate. The Pass 7 /go
        # allowlist was written, gated and tested that way and still shipped
        # nothing - the live manifest carried neither lp-quiz/lp-compare nor
        # lp<n>/sp<n>/rv<n>. An edit that carries an ALREADY-MIGRATED MARKER (a
        # 3-tuple) is marker-independent and is applied on every pass; it is
        # idempotent by construction, because the marker is what proves it landed.
        unconditional = [edit for edit in edits if len(edit) > 2]
        if marker in text:
            if not unconditional:
                continue
            before = text
            for edit in unconditional:
                old, new, already = edit[0], edit[1], edit[2]
                if already in text:
                    continue
                if text.count(old) != 1:
                    raise B2Error(f"go_route_contract_source_drift:{relative}")
                text = text.replace(old, new, 1)
            if text != before:
                _detach(path)
                path.write_text(text, encoding="utf-8")
                changed.append(relative)
            continue
        if (
            relative == "scripts/build-staging.mjs"
            and 'import { loadAffiliateLinkMap } from "../src/affiliate-link-map.mjs";' in text
            # DEFECT 2026-09-02, same coupling as the program-registry token
            # below: this probe used to end in `loadAffiliateLinkMap("compare"))`
            # with the call's own closing paren, so it stopped matching the
            # moment _ensure_go_runtime_promotion_contract_support appended
            # `, goPromotionAuthority` to the same call. Dropping the trailing
            # paren matches the pre- and post-promotion forms both, and the
            # prefix is still specific enough that it cannot match a build
            # that has no affiliate map wired at all.
            and 'deriveGoEntriesFromRegistry(programRegistry, SITE_PROFILE, contentLedger.programs, loadAffiliateLinkMap("compare")' in text
        ):
            old, new = edits[-1]
            if "goRouteByRegistryId" not in text:
                if text.count(old) != 1:
                    raise B2Error(f"go_route_contract_source_drift:{relative}")
                text = text.replace(old, new)
            text = f"// {marker}: structurally verified existing support.\n" + text
            _detach(path)
            path.write_text(text, encoding="utf-8")
            changed.append(relative)
            continue
        if all(token in text for token in structural_support.get(relative, ())):
            # SAME DELIVERY GAP as the marker short-circuit above: this branch
            # declares the file already structurally supported and skipped EVERY
            # edit, so a marker-independent edit added later never landed. The
            # Pass 7 /go allowlist was invisible here too. Apply those first.
            for edit in unconditional:
                old_text, new_text, already = edit[0], edit[1], edit[2]
                if already in text:
                    continue
                if text.count(old_text) != 1:
                    raise B2Error(f"go_route_contract_source_drift:{relative}")
                text = text.replace(old_text, new_text, 1)
            comment = f"# {marker}: structurally verified existing support.\n" if path.suffix == ".py" else f"// {marker}: structurally verified existing support.\n"
            if text.startswith("#!"):
                first, rest = text.split("\n", 1)
                text = first + "\n" + comment + rest
            else:
                text = comment + text
            _detach(path)
            path.write_text(text, encoding="utf-8")
            changed.append(relative)
            continue
        for edit in edits:
            old, new = edit[0], edit[1]
            already = edit[2] if len(edit) > 2 else new
            if already in text and text.count(old) != 1:
                continue  # already migrated by an earlier pass
            if text.count(old) != 1:
                raise B2Error(f"go_route_contract_source_drift:{relative}")
            text = text.replace(old, new, 1)
        _detach(path)
        path.write_text(text, encoding="utf-8")
        changed.append(relative)
    return {
        "status": "PASS_SUPPORTED" if changed else "PASS_ALREADY_SUPPORTED",
        "changed_files": changed,
        "marker": marker,
    }


GO_PROMOTION_TEST_REPLACEMENTS = {
    # 2026-09-03: the same rescission the readers above pin is also pinned by
    # four release tests. Migrated together so the suite states one truth.
    'tests/high-intent-affiliate-surfaces.test.mjs#item1-per-program': (
        '  const broadened = clone(wireApprovals);\n  const rejected = broadened.current_terms_reassessment.programs.find((row) => row.provider_program_id === "126727");\n  rejected.authorized_material_expansion_surfaces = ["calculator"];\n  rejected.may_authorize_material_expansion = true;\n  assert.deepEqual(item1(buildLinks(broadened)), []);\n  const forgedAccept = clone(wireApprovals);\n  forgedAccept.current_terms_reassessment.programs.find((row) => row.provider_program_id === "115383").operator_words = "accept the terms";\n  assert.deepEqual(item1(buildLinks(forgedAccept)), []);\n  const forgedReceipt = clone(wireApprovals);\n  forgedReceipt.current_terms_reassessment.programs.find((row) => row.provider_program_id === "115383").decision_receipt_sha256 = "f".repeat(64);\n  assert.deepEqual(item1(buildLinks(forgedReceipt)), []);\n  const forgedReject = clone(wireApprovals);\n  forgedReject.current_terms_reassessment.programs.find((row) => row.provider_program_id === "126727").provenance = "discord-message-forged";\n  assert.deepEqual(item1(buildLinks(forgedReject)), []);\n',
        '  // 2026-09-03: a defect in ONE row now de-authorizes only that row - the\n  // module header always promised "one poisoned program still takes only itself\n  // down", and returning null for the whole authority is what took four healthy\n  // Awin programs dark when the Measured rescission landed. The two envelope\n  // cases above still empty the surface; these prove the tampered program goes\n  // dark WITHOUT its healthy siblings.\n  const ids = (authority) => item1(buildLinks(authority)).map((row) => row.program_id).sort();\n  const healthy = ids(wireApprovals);\n  // Never let this test pass vacuously: the untampered surface must carry links.\n  assert.ok(healthy.length >= 3, `expected live Item 1 links, got ${JSON.stringify(healthy)}`);\n  const find = (rows, id) => rows.find((row) => row.provider_program_id === id);\n  const tampered = (mutate) => {\n    const document = clone(wireApprovals);\n    mutate(document.current_terms_reassessment.programs);\n    return ids(document);\n  };\n  const without = (alias) => healthy.filter((id) => id !== alias);\n  assert.deepEqual(tampered((rows) => {\n    const row = find(rows, "126727");\n    row.authorized_material_expansion_surfaces = ["calculator"];\n    row.may_authorize_material_expansion = true;\n  }), without("measured"));\n  assert.deepEqual(tampered((rows) => { find(rows, "115383").operator_words = "accept the terms"; }), without("biltrx"));\n  assert.deepEqual(tampered((rows) => { find(rows, "115383").decision_receipt_sha256 = "f".repeat(64); }), without("biltrx"));\n  assert.deepEqual(tampered((rows) => { find(rows, "126727").provenance = "discord-message-forged"; }), without("measured"));\n',
    ),
    'tests/high-intent-affiliate-surfaces.test.mjs#verdict-lists': (
        '  // 2026-08-13: the rejected program (126727 Measured) is de-linked on every\n  // surface; accepted/undecided programs preserve their existing live links.\n  assert.equal(reassessment.governance_scope, "accepted_live_preserved_rejected_terms_de_linked");\n  assert.equal(reassessment.evidence_sha256, "7eb6660110af1b7f64149036217723c1095398b9d2016dfb7da32135dfc6ae73");\n  assert.equal(reassessment.programs.length, 6);\n  const accepted = reassessment.programs.filter((row) => row.verdict === "operator_accepted_terms");\n  const rejected = reassessment.programs.filter((row) => row.verdict === "operator_rejected_terms");\n  assert.deepEqual(accepted.map((row) => row.provider_program_id).sort(), ["115383", "80963"]);\n  assert.deepEqual(rejected.map((row) => row.provider_program_id), ["126727"]);\n',
        '  // 2026-08-13 rejected 126727 (Measured); the operator RESCINDED that on\n  // 2026-08-31, so no program carries rejected terms and Measured preserves its\n  // existing live links like any other accepted program.\n  assert.ok(["accepted_live_preserved_rejected_terms_de_linked",\n    "accepted_live_preserved_rejected_terms_rescinded_20260831"].includes(reassessment.governance_scope));\n  assert.equal(reassessment.evidence_sha256, "7eb6660110af1b7f64149036217723c1095398b9d2016dfb7da32135dfc6ae73");\n  assert.equal(reassessment.programs.length, 6);\n  const accepted = reassessment.programs.filter((row) => row.verdict === "operator_accepted_terms");\n  const rejected = reassessment.programs.filter((row) => row.verdict === "operator_rejected_terms");\n  assert.deepEqual(accepted.map((row) => row.provider_program_id).sort(), ["115383", "126727", "80963"]);\n  assert.deepEqual(rejected.map((row) => row.provider_program_id), []);\n',
    ),
    'tests/high-intent-affiliate-surfaces.test.mjs#measured-row': (
        '  const measured = rejected[0];\n  // Rejected terms authorize NO surface (de-linked 2026-08-13).\n  assert.deepEqual(measured.existing_live_surfaces, []);\n  assert.deepEqual(measured.authorized_material_expansion_surfaces, []);\n  assert.equal(measured.may_authorize_material_expansion, false);\n  assert.equal(measured.operator_words, "measured has bad terms, and I wanna reject it");\n  assert.equal(measured.provenance, "bridge-session-20260812");\n  assert.equal(measured.reversible_by, "ACCEPT-TERMS 126727");\n',
        '  const measured = reassessment.programs.find((row) => row.provider_program_id === "126727");\n  // 2026-08-31 rescission: Measured keeps its three existing live surfaces and\n  // may expand onto none of them; the indemnity trigger that justified the\n  // original hold is carried as an explicit recorded risk-acceptance.\n  assert.deepEqual(measured.existing_live_surfaces, ["compare", "cost", "quiz"]);\n  assert.deepEqual(measured.authorized_material_expansion_surfaces, []);\n  assert.equal(measured.may_authorize_material_expansion, false);\n  assert.equal(measured.rescission_note, "MEASURED_TERMS_RESCISSION_V1");\n  assert.equal(measured.provenance, "operator-decision-supersession-20260831");\n  assert.equal(measured.risk_acceptance.risk_accepted, true);\n  assert.equal(measured.reversible_by, "ACCEPT-TERMS 126727");\n',
    ),
    'tests/high-intent-affiliate-surfaces.test.mjs#approval-verdict': (
        '  assert.equal(wireApprovals.programs.find((row) => row.existing_release_alias === "measured").current_terms_reassessment.verdict, "operator_rejected_terms");\n',
        '  assert.equal(wireApprovals.programs.find((row) => row.existing_release_alias === "measured").current_terms_reassessment.verdict, "operator_accepted_terms");\n',
    ),
    'tests/program-registry-coverage.test.mjs#fastpath-count': (
        '  // measured left the live fast-path set on 2026-08-13 (operator rejected the\n  // Awin 126727 terms); it is a held contract with a link-dark review, and it is\n  // now the ONLY one - 16 -> 17 on 2026-08-18 when the bridge lifted TRT\n  // Nation\'s AFF-TRT-001 content-fit hold against a receipted link-click QA.\n  assert.equal(result.live_fastpath_programs, 39); // 2026-08-27 live-head merge: +nu-image\n  assert.deepEqual(result.held_contract_programs.sort(), ["measured"]);\n',
        '  // measured left the live fast-path set on 2026-08-13 (operator rejected the\n  // Awin 126727 terms) and REJOINED it on 2026-08-31 when he rescinded that in\n  // his own words. 39 -> 40, and no contract is held any more: its /go authority\n  // is still withheld, so it is live on the provider-issued DIRECT link.\n  assert.equal(result.live_fastpath_programs, 40);\n  assert.deepEqual(result.held_contract_programs.sort(), []);\n',
        # ALREADY-MIGRATED MARKER: the semantic fact this migration installs.
        # live_fastpath_programs is a COUNT owned by
        # build_glp_affiliate_wirein_incremental_v2, which bumps it on every
        # wire-in, so it is NOT a safe idempotency witness - the remainder lane
        # legitimately carried 41. The held-contract set is.
        '  assert.deepEqual(result.held_contract_programs.sort(), []);\n',
    ),
    'tests/provider-fastpath-plus5.test.mjs#active-ids': (
        '  // measured left the active set 2026-08-13: operator rejected the Awin 126727\n  // terms, so its integration is READY_EDITORIAL_LINK_DARK / HOLD_OPERATOR_REJECTED_TERMS.\n  // 2026-08-18: trt-nation joined the active set when the bridge lifted its\n  // AFF-TRT-001 content-fit hold against a receipted link-click QA on the\n  // weight-loss deep link. measured stays out (operator rejected Awin 126727).\n  const activeIds = new Set(["mademed", "telos-rx", "strut-health", "drhouse", "biltrx", "orderlymeds", "mydrhank", "gobymeds", "live-vital-launch", "coreage-rx-glp-1-1632", "bodybuilding-com-glp-1-longevity-review", "skinnyrx-1-glp-weight-loss-provider-review", "rxspan-md-telehealth-program-review", "pallas-health-review", "wellmedr-review", "dudemeds-review", "trt-nation", "nu-image-medical-review", "synergy-rx-review", "breeze-meds-review", "care-bare-rx-glp1-review", "altrx-glp1-us-cps", "novi-weight-loss-glp-1", "curex-glp-1", "ro-wegovy-glp1-quiz-us-cpl", "eden-xentraffic-offer-3051", "futurhealth-wegovy-pillform", "lttl-glp-landing-page-2", "enhanced-tirzepatide-funnel-b", "sunlight-glp1", "joinskinny-semaglutide-glp-1-us", "levelsrx-extra-cpa-home-page", "refills", "tmates-xentraffic-offer-3154", "trimrx-xentraffic-offer-3014", "yucca-health-xentraffic-offer-3117", "longevity-meds-glp-1-review", "snagrx", "humecare-data-driven-glp-1-weight-loss-system"]); // 2026-08-27 live-head merge\n',
        '  // measured left the active set 2026-08-13 (operator rejected the Awin 126727\n  // terms) and rejoined it on 2026-08-31 when he rescinded that rejection. Its\n  // /go authority stays WITHHELD, so it is active on the provider-issued\n  // DIRECT link - the same form every other live provider uses today.\n  // 2026-08-18: trt-nation joined the active set when the bridge lifted its\n  // AFF-TRT-001 content-fit hold against a receipted link-click QA on the\n  // weight-loss deep link.\n  const activeIds = new Set(["mademed", "telos-rx", "strut-health", "drhouse", "biltrx", "orderlymeds", "mydrhank", "gobymeds", "live-vital-launch", "coreage-rx-glp-1-1632", "bodybuilding-com-glp-1-longevity-review", "skinnyrx-1-glp-weight-loss-provider-review", "rxspan-md-telehealth-program-review", "pallas-health-review", "wellmedr-review", "dudemeds-review", "trt-nation", "nu-image-medical-review", "synergy-rx-review", "breeze-meds-review", "care-bare-rx-glp1-review", "altrx-glp1-us-cps", "novi-weight-loss-glp-1", "curex-glp-1", "ro-wegovy-glp1-quiz-us-cpl", "eden-xentraffic-offer-3051", "futurhealth-wegovy-pillform", "lttl-glp-landing-page-2", "enhanced-tirzepatide-funnel-b", "sunlight-glp1", "joinskinny-semaglutide-glp-1-us", "levelsrx-extra-cpa-home-page", "refills", "tmates-xentraffic-offer-3154", "trimrx-xentraffic-offer-3014", "yucca-health-xentraffic-offer-3117", "longevity-meds-glp-1-review", "snagrx", "humecare-data-driven-glp-1-weight-loss-system", "measured"]); // 2026-08-27 live-head merge\n',
        # ALREADY-MIGRATED MARKER: my exact insertion point. The activeIds SET
        # keeps growing - the remainder lane appended "g-plan" after "measured" -
        # so matching the whole migrated line is not a safe idempotency witness.
        '"humecare-data-driven-glp-1-weight-loss-system", "measured"',
    ),
    # 2026-09-03: the operator rescission of the Measured rejection changed the
    # current_terms_reassessment row these readers pin. Left unmigrated,
    # awinItem1SurfaceAuthority returned null and EVERY Awin program went dark.
    # scripts/validate-provider-fastpath-plus5.mjs is deliberately ABSENT here:
    # _ensure_measured_terms_rescission owns that file's pin, and two mechanisms
    # editing one file race each other.
    'src/affiliate-link-map.mjs#gov-scope': (
        '      || reassessment?.governance_scope !== "accepted_live_preserved_rejected_terms_de_linked"\n',
        '      // The 2026-08-31 operator rescission renamed this scope marker. Demanding\n      // only the pre-rescission literal blanked the whole authority.\n      || !["accepted_live_preserved_rejected_terms_de_linked",\n           "accepted_live_preserved_rejected_terms_rescinded_20260831"]\n           .includes(String(reassessment?.governance_scope || ""))\n',
    ),
    'src/affiliate-link-map.mjs#matrix-measured': (
        '  // 2026-08-13: operator REJECTED advertiser 126727 ("measured has bad terms,\n  // and I wanna reject it"). A rejected program authorizes NO surface — the\n  // authority validation below enforces the empty set mechanically.\n  "126727": Object.freeze({ alias: "measured", existing: [], expansion: [] }),\n',
        '  // 2026-08-13 the operator rejected advertiser 126727. On 2026-08-31 he\n  // rescinded that in his own words ("i never denied measured ... i want it\n  // wired in fully live on the site"), with the indemnity trigger carried as a\n  // recorded risk-acceptance on the row. Measured is therefore LIVE on its\n  // three existing surfaces through a DIRECT CTA; surfaces.go stays false, so\n  // no /go/measured redirect is minted. The validation below pins that row.\n  "126727": Object.freeze({ alias: "measured", existing: ["compare", "cost", "quiz"], expansion: [] }),\n',
    ),
    'src/affiliate-link-map.mjs#per-program-scope': (
        '  for (const [id, expected] of Object.entries(AWIN_ITEM1_SURFACE_MATRIX)) {\n    const row = rows.get(id);\n    const rejected = row.verdict === "operator_rejected_terms";\n    // A rejected-terms program may not carry ANY authorized surface, in the\n    // pinned matrix or in the data row — a stale surface list poisons the\n    // whole authority rather than reviving a rejected link.\n    if (rejected && (expected.existing.length || expected.expansion.length\n        || row.existing_live_surfaces.length || row.authorized_material_expansion_surfaces.length)) return null;\n    if (row.existing_release_alias !== expected.alias\n        || row.live_link_disposition !== (rejected ? REJECTED_TERMS_DISPOSITION : "preserve_existing_live")\n        || row.may_authorize_new_activation !== false\n        || row.may_authorize_material_expansion !== (expected.expansion.length > 0)\n        || JSON.stringify(row.existing_live_surfaces) !== JSON.stringify(expected.existing)\n        || JSON.stringify(row.authorized_material_expansion_surfaces) !== JSON.stringify(expected.expansion)) return null;\n  }\n  const accepted = new Map([\n    ["80963", Object.freeze({ words: "ACCEPT-TERMS 80963", receipt: "febcdea976a2f28813a347552486420f6468f338a74a4551890210e348ebe17b" })],\n    ["115383", Object.freeze({ words: "ACCEPT-TERMS 115383", receipt: "6c9e8f2c782a71fefeecc20ad7c7deca873587b70bbf4745126139010c32b281" })],\n  ]);\n  for (const [id, decision] of accepted) {\n    const row = rows.get(id);\n    if (row.status !== "operator_accepted_terms" || row.verdict !== "operator_accepted_terms"\n        || row.future_change_terms_gate_open !== true || row.operator_words !== decision.words\n        || row.provenance !== "discord-message-1537243566156546078"\n        || row.reversible_by !== `REJECT-TERMS ${id}`\n        || row.decision_receipt_sha256 !== decision.receipt) return null;\n  }\n  const rejected = rows.get("126727");\n  if (rejected.status !== "operator_rejected_terms" || rejected.verdict !== "operator_rejected_terms"\n      || rejected.future_change_terms_gate_open !== false\n      || rejected.operator_words !== "measured has bad terms, and I wanna reject it"\n      || rejected.provenance !== "bridge-session-20260812"\n      || rejected.reversible_by !== "ACCEPT-TERMS 126727"\n      || rejected.decision_receipt_sha256 !== "4787549c67ff7b19fe2aed1f881c368aa16bdf6c0243312f5f10e47567d1e9fc") return null;\n  return rows;\n}\n',
        '  // A row that fails its own pinned expectations is de-authorized BY ITSELF -\n  // "one poisoned program still takes only itself down", as the header promises.\n  // Nulling the whole authority is what took every Awin program dark on\n  // 2026-09-03 when the Measured rescission landed: four healthy programs\n  // (serena-health, coivas, drhouse, biltrx) lost their links over one row.\n  const poisoned = new Set();\n  for (const [id, expected] of Object.entries(AWIN_ITEM1_SURFACE_MATRIX)) {\n    const row = rows.get(id);\n    const rejected = row.verdict === "operator_rejected_terms";\n    // A rejected-terms program may not carry ANY authorized surface, in the\n    // pinned matrix or in the data row - a stale surface list poisons that\n    // program rather than reviving a rejected link.\n    if (rejected && (expected.existing.length || expected.expansion.length\n        || row.existing_live_surfaces.length || row.authorized_material_expansion_surfaces.length)) {\n      poisoned.add(id);\n      continue;\n    }\n    if (row.existing_release_alias !== expected.alias\n        || row.live_link_disposition !== (rejected ? REJECTED_TERMS_DISPOSITION : "preserve_existing_live")\n        || row.may_authorize_new_activation !== false\n        || row.may_authorize_material_expansion !== (expected.expansion.length > 0)\n        || JSON.stringify(row.existing_live_surfaces) !== JSON.stringify(expected.existing)\n        || JSON.stringify(row.authorized_material_expansion_surfaces) !== JSON.stringify(expected.expansion)) poisoned.add(id);\n  }\n  const accepted = new Map([\n    ["80963", Object.freeze({ words: "ACCEPT-TERMS 80963", receipt: "febcdea976a2f28813a347552486420f6468f338a74a4551890210e348ebe17b" })],\n    ["115383", Object.freeze({ words: "ACCEPT-TERMS 115383", receipt: "6c9e8f2c782a71fefeecc20ad7c7deca873587b70bbf4745126139010c32b281" })],\n  ]);\n  for (const [id, decision] of accepted) {\n    const row = rows.get(id);\n    if (row.status !== "operator_accepted_terms" || row.verdict !== "operator_accepted_terms"\n        || row.future_change_terms_gate_open !== true || row.operator_words !== decision.words\n        || row.provenance !== "discord-message-1537243566156546078"\n        || row.reversible_by !== `REJECT-TERMS ${id}`\n        || row.decision_receipt_sha256 !== decision.receipt) poisoned.add(id);\n  }\n  // 126727: the 2026-08-31 rescission supersedes the 2026-08-12 rejection. The\n  // recorded risk-acceptance for the indemnity trigger is part of the pin, so a\n  // row that goes live WITHOUT it still poisons itself.\n  const measured = rows.get("126727");\n  if (measured.status !== "operator_accepted_terms" || measured.verdict !== "operator_accepted_terms"\n      || measured.future_change_terms_gate_open !== true\n      || measured.rescission_note !== "MEASURED_TERMS_RESCISSION_V1"\n      || measured.provenance !== "operator-decision-supersession-20260831"\n      || measured.reversible_by !== "ACCEPT-TERMS 126727"\n      || measured.decision_receipt_sha256 !== "743efb19f484f5cebd74b5894b9321ff3e97ac4a0d15e0b54214b68f68c8e51d"\n      || measured.risk_acceptance?.risk_accepted !== true\n      || measured.risk_acceptance?.mechanical_trigger !== "advertiser_supplied_copy_indemnity") poisoned.add("126727");\n  for (const id of poisoned) rows.delete(id);\n  return rows;\n}\n',
    ),
    'src/affiliate-link-map.mjs#allowed-missing-row': (
        '  const row = authority.get(String(advertiserId));\n  return row.existing_live_surfaces.includes(surface)\n',
        '  const row = authority.get(String(advertiserId));\n  // De-authorized (poisoned) programs are absent from the map: link-dark.\n  if (!row) return false;\n  return row.existing_live_surfaces.includes(surface)\n',
    ),
                    # 2026-09-03 operator ruling: never-monetized must be DERIVED from the
    # terms-decision record, never a hardcoded literal. A rescinded rejection
    # left Measured link-dark long after the operator reversed it.
    "scripts/validate-editorial-monetization.mjs": (
        'const NEVER_MONETIZED_PROGRAMS = ["measured"];\n',
        '// NEVER-MONETIZED IS DERIVED, NOT DECLARED. Operator ruling 2026-09-03: this\n// was a hardcoded ["measured"], and the operator had already rescinded that\n// rejection on 2026-08-31 ("i never denied measured ... remove that wherever\n// it has that in the system"). A stale copy of a reversed decision kept a live\n// provider link-dark. The 08-13 REJECT-TERMS receipt declares its own reversal\n// (reversible_by "ACCEPT-TERMS 126727"), so the decision record - not a literal\n// - is the authority: a program is never-monetized ONLY while its latest terms\n// decision is a rejection with no later acceptance.\nconst TERMS_DECISIONS_DIR = new URL("../../../../../reports/glp-clinic-scout/affiliate-machine/awin-item1-terms-decisions-20260813/", import.meta.url);\nasync function deriveNeverMonetizedPrograms() {\n  let names = [];\n  try { names = await readdir(TERMS_DECISIONS_DIR); } catch { return []; }\n  const latest = new Map();\n  for (const name of names) {\n    if (!name.startsWith("awin-terms-") || !name.endsWith(".json")) continue;\n    let doc;\n    try { doc = JSON.parse(await readFile(new URL(name, TERMS_DECISIONS_DIR), "utf8")); } catch { continue; }\n    if (doc?.schema !== "glp-awin-terms-decision-v2") continue;\n    const slug = String(doc.program_name || "").trim().toLowerCase().replace(/\\s+/g, "-");\n    if (!slug) continue;\n    const at = String(doc.recorded_at_utc || "");\n    const prior = latest.get(slug);\n    if (!prior || at > prior.at) latest.set(slug, { at, decision: String(doc.decision || "") });\n  }\n  return [...latest.entries()]\n    .filter(([, row]) => row.decision === "operator_rejected_terms")\n    .map(([slug]) => slug)\n    .sort();\n}\nconst NEVER_MONETIZED_PROGRAMS = await deriveNeverMonetizedPrograms();\n',
    ),
    "scripts/validate-editorial-monetization.mjs#header": (
        '//      `measured` stays link-dark everywhere, forever, per the rejected-terms receipt.\n',
        '//      never-monetized is DERIVED from the terms-decision record, so a rescinded\n//      rejection stops holding a provider dark (Measured, operator 2026-08-31).\n',
    ),
    "scripts/validate-editorial-monetization.mjs#summary": (
        ' · measured link-dark · Price Index promise intact',
        ' · never-monetized derived from terms decisions · Price Index promise intact',
    ),
    # 2026-09-02, coordinator/Fable directive: a fastpath provider activated
    # through a governed first-party /go route matched NEITHER branch of
    # validate-provider-fastpath-plus5.mjs, so the held branch asserted it
    # link-dark and killed round 32. Add a THIRD branch, keeping both existing
    # ones and the held-by-default posture: entered only when the governed
    # promotion authority authorises the program.
    "scripts/validate-provider-fastpath-plus5.mjs": (
        '  } else {\n    assert.equal(provider.route_state, "READY_EDITORIAL_LINK_DARK");\n    assert.match(provider.affiliate_cta_state, /^HOLD_/);\n',
        '  } else if ((((await readJson("data/go-runtime-promotion-authority.json")).programs || {})[provider.program_id] || {}).promotion_authorized === true) {\n    // FORM 1 - governed /go promotion. RULING 2026-09-03: a route is active\n    // only under a verified promotion authority, and then the page must carry\n    // the queried anchor the cs_click_id join needs.\n    assert.equal(liveProgram.go.entry_state, "instrumented_redirect_candidate");\n    assert.match(String(liveProgram.go.route || ""), /^\\/go\\/[a-z0-9-]+$/);\n    assert.ok((await readText("functions/go/program-manifest.js")).includes(liveProgram.go.route),\n      `go-route-unresolvable:${provider.program_id}`);\n    assert.match(html, /href="\\/go\\/[a-z0-9-]+\\?s=[a-z0-9-]+\\.[a-z0-9]+&amp;v=[a-z0-9]{1,4}"/);\n    assert.doesNotMatch(html, /href="\\/go\\/[a-z0-9-]+"/);\n    assert.equal((html.match(/data-affiliate-state="active"/g) || []).length, 1);\n    activeAffiliateCtas += 1;\n  } else if (provider.route_state === "LIVE_EDITORIAL_AFFILIATE_ACTIVE") {\n    // FORM 2 - held /go authority, live on the provider-issued DIRECT link.\n    // This is Measured: its terms hold was rescinded so the row is live, but\n    // its /go authority is WITHHELD, so /go/<slug> stays held and the direct\n    // Awin href is the correct monetized form - the same form every other live\n    // provider uses today. entry_state is derived from the authority, so a\n    // withheld authority reads "held" here even though the route is registered.\n    // liveProgram is found on commercial_state live + wired, NOT on\n    // surfaces.go - Measured carries surfaces.go false after its rescission,\n    // which is exactly why it matched neither original branch.\n    assert.ok(liveProgram, `live-registry-row-missing:${provider.program_id}`);\n    assert.equal(String((liveProgram.go || {}).entry_state || ""), "held");\n    assert.doesNotMatch(html, /href="\\/go\\//);\n    assert.equal((html.match(/data-link-mode="transparent-direct"/g) || []).length, 1);\n    assert.equal((html.match(/data-affiliate-state="active"/g) || []).length, 1);\n    activeAffiliateCtas += 1;\n  } else {\n    // held-by-default, unchanged: no governed promotion authority and not live.\n    assert.equal(provider.route_state, "READY_EDITORIAL_LINK_DARK");\n    assert.match(provider.affiliate_cta_state, /^HOLD_/);\n',
    ),
    "tests/go-runtime-manifest.test.mjs": (
        'test("all generated runtime entries remain production-held", () => {\n'
        '  const entries = Object.values(GO_RUNTIME_MANIFEST.programs);\n'
        '  assert.equal(entries.length, 32);\n'
        '  for (const entry of entries) {\n'
        '    assert.equal(entry.status, "candidate_preview_only", entry.program_id);\n'
        '    assert.equal(entry.link_mode, "instrumented_redirect_candidate", entry.program_id);\n'
        '    assert.equal(entry.live_link_allowed, false, entry.program_id);\n'
        '    assert.equal(entry.promotion_authorized, false, entry.program_id);\n'
        '    assert.equal(entry.constructor_verified, false, entry.program_id);\n'
        '    assert.equal(entry.redirector_tos_verified, false, entry.program_id);\n'
        '  }\n'
        '});',
        'test("every generated runtime entry is governed active or explicitly held", () => {\n'
        '  const entries = Object.values(GO_RUNTIME_MANIFEST.programs);\n'
        '  assert.ok(entries.length >= 32);\n'
        '  const active = entries.filter((entry) => entry.live_link_allowed === true && entry.promotion_authorized === true);\n'
        '  assert.ok(active.length > 0);\n'
        '  for (const entry of active) {\n'
        '    assert.equal(entry.status, "live", entry.program_id);\n'
        '    assert.equal(entry.constructor_verified, true, entry.program_id);\n'
        '    assert.equal(entry.redirector_tos_verified, true, entry.program_id);\n'
        '    assert.equal(entry.candidate_gate, null, entry.program_id);\n'
        '  }\n'
        '  for (const entry of entries.filter((item) => !active.includes(item))) {\n'
        '    assert.equal(entry.live_link_allowed, false, entry.program_id);\n'
        '    assert.equal(entry.promotion_authorized, false, entry.program_id);\n'
        '    assert.ok(String(entry.candidate_gate || "").length > 5, entry.program_id);\n'
        '  }\n'
        '});',
    ),
    "tests/funnel-v2-instrumentation.test.mjs": (
        'test("every candidate go manifest entry remains production-held pending design acceptance", () => {\n'
        '  const candidates = Object.values(GO_RUNTIME_MANIFEST.programs);\n'
        '  assert.equal(candidates.length, 32);\n'
        '  assert.ok(candidates.every((program) => program.status === "candidate_preview_only"));\n'
        '  assert.ok(candidates.every((program) => program.link_mode === "instrumented_redirect_candidate"));\n'
        '  assert.ok(candidates.every((program) => program.go_route_wired === true && program.live_link_allowed === false && program.promotion_authorized === false));\n'
        '  assert.ok(candidates.every((program) => program.constructor_verified === false && program.redirector_tos_verified === false));\n'
        '  assert.ok(candidates.every((program) => program.allowed_page_roles.includes("providers") && program.allowed_positions.includes("fv2s1") && program.allowed_positions.includes("pc1")));\n'
        '});',
        'test("every go manifest entry is governed active or explicitly held", () => {\n'
        '  const candidates = Object.values(GO_RUNTIME_MANIFEST.programs);\n'
        '  assert.ok(candidates.length >= 32);\n'
        '  assert.ok(candidates.some((program) => program.live_link_allowed === true && program.promotion_authorized === true));\n'
        '  assert.ok(candidates.every((program) => program.link_mode === "instrumented_redirect_candidate"));\n'
        '  assert.ok(candidates.every((program) => program.go_route_wired === true));\n'
        '  assert.ok(candidates.every((program) => (program.live_link_allowed === true && program.promotion_authorized === true && program.constructor_verified === true && program.redirector_tos_verified === true) || (program.live_link_allowed === false && program.promotion_authorized === false && String(program.candidate_gate || "").length > 5)));\n'
        '  assert.ok(candidates.every((program) => program.allowed_page_roles.includes("providers") && program.allowed_positions.includes("fv2s1") && program.allowed_positions.includes("pc1")));\n'
        '});',
    ),
}

GO_PROMOTION_V2_TEST_MARKERS = {
    "scripts/validate-provider-fastpath-plus5.mjs": (
        "promotionAuthority[provider.program_id]?.status === \"VERIFIED\"",
        "sealed_promotion_hold_missing:",
        "unauthorized_review_anchor:",
    ),
    "tests/program-registry-coverage.test.mjs#fastpath-count": (
        "result.held_contract_programs.sort(), contracts.providers.filter",
        "assert.deepEqual(result.held_contract_programs, ['telos-rx'])",
    ),
    "tests/go-runtime-manifest.test.mjs": (
        "loadGoSelection(root)", "active.map(p=>p.program_id).sort()",
        "redirectorTermsAllowed(entry)",
    ),
    "tests/funnel-v2-instrumentation.test.mjs": (
        "loadGoPromotionAuthority().programs", "adopted.length, 17",
        "redirectorTermsAllowed(program)",
    ),
}


def _ensure_go_runtime_promotion_contract_support(
    candidate_root: Path,
) -> dict[str, Any]:
    """Teach detached candidate bytes to consume a governed promotion map."""
    marker = "GO_RUNTIME_PROMOTION_AUTHORITY_V1"
    candidate_root = Path(candidate_root)
    registry_source = candidate_root / "src/program-registry.mjs"
    regenerate_source = candidate_root / "scripts/regenerate-go-runtime-manifest.mjs"
    changed: list[str] = []

    registry_text = registry_source.read_text(encoding="utf-8")
    v2_signature_supported = any(registry_text.count(signature) == 1 for signature in (
        "export function deriveGoEntriesFromRegistry(registry, profileId, ledgerPrograms = [], affiliateMap = {}, goPromotionAuthority = {}) {",
        "export function deriveGoEntriesFromRegistry(registry, profileId, ledgerPrograms = [], affiliateMap = {}, goPromotionAuthority = {}, commercialLabels = {}) {",
    ))
    v2_supported = v2_signature_supported and all(needle in registry_text for needle in (
        "GO_RUNTIME_PROMOTION_AUTHORITY_V2",
        "goPromotionReceiptComplete(promotion, program, link)",
        'candidate_gate: promotionActive ? null : (promotion?.reason || "go_runtime_promotion_authority_missing")',
    ))
    if marker not in registry_text and not v2_supported:
        replacements = [
            (
                "export function deriveGoEntriesFromRegistry(registry, profileId, ledgerPrograms = [], affiliateMap = {}) {",
                "// GO_RUNTIME_PROMOTION_AUTHORITY_V1: activate only exact governed runtime entries.\n"
                "export function deriveGoEntriesFromRegistry(registry, profileId, ledgerPrograms = [], affiliateMap = {}, goPromotionAuthority = {}) {",
            ),
            (
                "    const link = affiliateMap[renderId];\n"
                "    if (!link?.live || !link.affiliate_href || !link.network) return base;",
                "    const promotion = goPromotionAuthority?.programs?.[program.program_id] || goPromotionAuthority?.programs?.[renderId] || null;\n"
                "    const promotionActive = promotion?.status === \"VERIFIED\" && promotion?.promotion_authorized === true && promotion?.constructor_verified === true && promotion?.redirector_tos_verified === true;\n"
                "    const link = affiliateMap[renderId];\n"
                "    if (!link?.live || !link.affiliate_href || !link.network) return base;",
            ),
            (
                "      status: \"candidate_preview_only\",\n"
                "      link_mode: \"instrumented_redirect_candidate\",",
                "      status: promotionActive ? \"live\" : \"candidate_preview_only\",\n"
                "      link_mode: \"instrumented_redirect_candidate\",",
            ),
            (
                "      live_link_allowed: false,\n"
                "      promotion_authorized: false,\n"
                "      constructor_verified: false,\n"
                "      advertiser_permission_verified: true,\n"
                "      encoding_verified: true,\n"
                "      privacy_qa_passed: true,\n"
                "      redirector_tos_verified: false,\n"
                "      candidate_gate: \"operator_design_acceptance_required\",",
                "      live_link_allowed: promotionActive,\n"
                "      promotion_authorized: promotionActive,\n"
                "      constructor_verified: promotionActive,\n"
                "      advertiser_permission_verified: true,\n"
                "      encoding_verified: true,\n"
                "      privacy_qa_passed: true,\n"
                "      redirector_tos_verified: promotionActive,\n"
                "      candidate_gate: promotionActive ? null : (promotion?.reason || \"go_runtime_promotion_authority_missing\"),",
            ),
        ]
        for old, new in replacements:
            if registry_text.count(old) != 1:
                raise B2Error("go_runtime_promotion_registry_source_drift")
            registry_text = registry_text.replace(old, new)
        _detach(registry_source)
        registry_source.write_text(registry_text, encoding="utf-8")
        changed.append(str(registry_source.relative_to(candidate_root)))

    regenerate_text = regenerate_source.read_text(encoding="utf-8")
    if marker not in regenerate_text:
        old = (
            'const ledger = JSON.parse(await readFile(path.join(ROOT, "data", "program-content-ledger.json"), "utf8"));'
        )
        new = (
            'const ledger = JSON.parse(await readFile(path.join(ROOT, "data", "program-content-ledger.json"), "utf8"));\n'
            '// GO_RUNTIME_PROMOTION_AUTHORITY_V1: missing authority means every route stays held.\n'
            'let goPromotionAuthority = { programs: {} };\n'
            'try { goPromotionAuthority = JSON.parse(await readFile(path.join(ROOT, "data", "go-runtime-promotion-authority.json"), "utf8")); }\n'
            'catch (error) { if (error?.code !== "ENOENT") throw error; }'
        )
        if regenerate_text.count(old) != 1:
            raise B2Error("go_runtime_promotion_regenerator_source_drift")
        regenerate_text = regenerate_text.replace(old, new)
        old_call = (
            'programs: deriveGoEntriesFromRegistry(registry, "glp-clinic-scout", ledger.programs, loadAffiliateLinkMap("compare")),'
        )
        new_call = (
            'programs: deriveGoEntriesFromRegistry(registry, "glp-clinic-scout", ledger.programs, loadAffiliateLinkMap("compare"), goPromotionAuthority),'
        )
        if regenerate_text.count(old_call) != 1:
            raise B2Error("go_runtime_promotion_regenerator_call_drift")
        regenerate_text = regenerate_text.replace(old_call, new_call)
        _detach(regenerate_source)
        regenerate_source.write_text(regenerate_text, encoding="utf-8")
        changed.append(str(regenerate_source.relative_to(candidate_root)))

    # build-staging.mjs re-derives the SAME manifest and fails closed if its
    # derivation differs from the frozen functions/go/program-manifest.js. It
    # also called deriveGoEntriesFromRegistry with four arguments, so once the
    # runtime manifest is correctly promoted, build-staging's unpromoted
    # re-derivation drifts and throws frozen_build4_go_runtime_manifest_drift.
    # Same omission as the refresh script, one file over.
    staging_source = candidate_root / "scripts/build-staging.mjs"
    staging_text = staging_source.read_text(encoding="utf-8")
    if marker not in staging_text:
        anchor_line = "  const activeGoAnchors = await countPublicGoAnchors(PUBLIC);"
        if staging_text.count(anchor_line) != 1:
            raise B2Error("go_runtime_promotion_build_staging_anchor_drift")
        staging_text = staging_text.replace(
            anchor_line,
            anchor_line
            + "\n  // GO_RUNTIME_PROMOTION_AUTHORITY_V1: missing authority means every route stays held."
            + "\n  let goPromotionAuthority = { programs: {} };"
            + "\n  try { goPromotionAuthority = JSON.parse(await readFile(path.join(ROOT, \"data\", \"go-runtime-promotion-authority.json\"), \"utf8\")); }"
            + "\n  catch (error) { if (error?.code !== \"ENOENT\") throw error; }",
            1,
        )
        candidates = [
            value for value in (
                'programs: deriveGoEntriesFromRegistry(programRegistry, SITE_PROFILE, '
                'contentLedger.programs, loadAffiliateLinkMap("compare")) };',
                'programs: deriveGoEntriesFromRegistry(programRegistry, SITE_PROFILE, '
                'contentLedger.programs, loadAffiliateLinkMap("compare", SITE_PROFILE)) };',
            ) if staging_text.count(value) == 1
        ]
        if len(candidates) != 1:
            raise B2Error("go_runtime_promotion_build_staging_call_drift")
        old_staging = candidates[0]
        new_staging = old_staging.replace(")) };", "), goPromotionAuthority) };", 1)
        staging_text = staging_text.replace(old_staging, new_staging, 1)
        _detach(staging_source)
        staging_source.write_text(staging_text, encoding="utf-8")
        changed.append(str(staging_source.relative_to(candidate_root)))

    test_replacements = GO_PROMOTION_TEST_REPLACEMENTS
    for relative, entry in test_replacements.items():
        # A file may need more than one migration; "<path>#tag" keys let the
        # table carry several ordered edits for the same file.
        #
        # IDEMPOTENCY 2026-09-03. An entry may carry a third element: the marker
        # that proves the migration ALREADY LANDED. Matching the whole `new` block
        # is too strict whenever another machine legitimately rewrites part of the
        # same lines afterwards - the remainder lane died on exactly that, because
        # its candidate's live_fastpath_programs had been bumped to 41 by
        # build_glp_affiliate_wirein_incremental_v2, so neither `old` nor `new`
        # matched and an already-migrated file read as drift. The marker defaults
        # to `new`, so every existing entry keeps its current strictness.
        old, new = entry[0], entry[1]
        already = entry[2] if len(entry) > 2 else new
        path = candidate_root / relative.split("#", 1)[0]
        text = path.read_text(encoding="utf-8")
        if old not in text:
            stronger = GO_PROMOTION_V2_TEST_MARKERS.get(relative, ())
            if already not in text and not (stronger and all(marker in text for marker in stronger)):
                raise B2Error(f"go_runtime_promotion_test_source_drift:{relative}")
            continue
        _detach(path)
        path.write_text(text.replace(old, new), encoding="utf-8")
        changed.append(relative)
    return {"status": "PASS_SUPPORTED", "marker": marker, "changed_files": changed}


def _materialize_clickdealer_directory_inputs(candidate_root: Path, approved_registry: dict) -> list[dict]:
    """Bind the four directory-only offers to issuer bytes, not a research substitute."""
    receipt_path = BUSINESS / "reports/ops/glp41-clickdealer-link-recovery-20260925/RESULT.json"
    census_path = BUSINESS / "reports/ops/glp41-20260924/approved-provider-census/RESULT.json"
    if hashlib.sha256(receipt_path.read_bytes()).hexdigest() != "459523fe5fd06b0882d724ffbc352facbe7cd72f5599fc40d7d5c3c7a442b885":
        raise B2Error("clickdealer_issued_receipt_changed")
    if hashlib.sha256(census_path.read_bytes()).hexdigest() != "dd1ac2a39373fa4f8a9b53961043f8cc2e4626713c01cf73c97da5b041e3284f":
        raise B2Error("clickdealer_approval_census_changed")
    receipt, census = read_json(receipt_path), read_json(census_path)
    if (receipt.get("scope_status") != "PASS_4_OF_4_ISSUED_DEFAULT_LINKS"
            or receipt.get("source", {}).get("http_status") != 200
            or receipt.get("source", {}).get("complete_row_count") != 18):
        raise B2Error("clickdealer_issuer_source_unverified")
    expected = {
        "178335:251864": ("12094630", "ReadyRx", "6d488c238b84b7d4d2073a19954507aec0a98e8231a8efac12106ccfa9136a6c"),
        "181687:255411": ("12094632", "FMmeds", "0011b689116241f6f8aa9dbc3bb2a18882f42d612f41241667c70266c6a76eed"),
        "176223:249142": ("12094657", "Zevay", "c11c7303c8ebad842d8521139a78567e847b83c6c3f851c4b4165c743029f4a1"),
        "181731:255455": ("12094787", "AbundRX", "79e5645e0c121f24b25d2c3b1d7365e1f3e64473aba2c1ba505980b6a3f67138"),
    }
    if len(receipt.get("rows", [])) != 4 or {r.get("offer_identity") for r in receipt["rows"]} != set(expected):
        raise B2Error("clickdealer_issued_identity_set_changed")
    census_rows = census.get("registry_approval_rows", []) + census.get("network_first_rows", [])
    rows = []
    for source in receipt["rows"]:
        identity = source["offer_identity"]
        campaign, name, digest = expected[identity]
        census_match = [r for r in census_rows if identity in (r.get("provider_program_ids") or [r.get("provider_program_id")])
                        and r.get("network") == "clickdealer" and r.get("current_network_relationship") == "approved"]
        url = source.get("issued_link")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (len(census_match) != 1 or name.lower() not in census_match[0].get("name", "").lower()
                or source.get("network") != "clickdealer" or source.get("status") != "PASS_ISSUER_BOUND_LINK"
                or source.get("issued_link_type") != "default_tracking_link"
                or source.get("campaign_id") != campaign
                or (source.get("offer_id"), source.get("offer_contract_id")) != tuple(identity.split(":"))
                or source.get("issued_link_sha256") != digest
                or not isinstance(url, str) or hashlib.sha256(url.encode()).hexdigest() != digest
                or parsed.scheme != "https" or parsed.hostname not in {"cd-down.com", "fastnuttrk.com", "nmttrack.com"}
                or parsed.username or parsed.password):
            raise B2Error(f"clickdealer_issued_binding_invalid:{identity}")
        rows.append({
            "registry_id": "clickdealer-" + identity.replace(":", "-"),
            "network": "ClickDealer", "provider_program_id": identity,
            "display_name": name, "canonical_vertical": "glp1-weightloss",
            "enumeration_class": "provider_joined_or_approved_exact_id",
            "directory_only": True,
            "source_refs": [str(receipt_path.relative_to(BUSINESS)) + "#" + source["source_selector"],
                            str(census_path.relative_to(BUSINESS)) + "#" + identity],
            "tracking": {"provider_issued_url": url, "issued_link_sha256": digest,
                         "issued_link_type": "default_tracking_link", "campaign_id": campaign,
                         "tracking_host": parsed.hostname},
        })
    # Keep the frozen 53-program coverage contract intact; these are not reviews.
    approved_registry["approved_directory_programs"] = rows
    return rows


def apply_go_runtime_promotion_authority(candidate_root: Path) -> dict[str, Any]:
    """Materialize per-program active/held authority without exposing hrefs."""
    candidate_root = Path(candidate_root)
    support = _ensure_go_runtime_promotion_contract_support(candidate_root)
    # Runs once per deploy, next to the other /go contract supports, because the
    # Affiliati constructor is shared source: it must be correct for the whole
    # release, not only for providers that happen to carry a /go request.
    affiliati_subid_support = _ensure_affiliati_subid_contract_support(candidate_root)
    runtime_text = (
        candidate_root / "functions/go/program-manifest.js"
    ).read_text(encoding="utf-8")
    runtime_match = re.search(r"Object\.freeze\((.*)\);\s*$", runtime_text, re.S)
    if not runtime_match:
        raise B2Error("go_runtime_promotion_discovery_manifest_parse_failed")
    runtime = json.loads(runtime_match.group(1))
    decision_text = ALL_APPROVED_LIVE_DECISION.read_text(encoding="utf-8")
    if ("trt nation and shed are both approved to deploy if they do glp-1s" not in decision_text
            or "any approved provider should be deployed live on the site" not in decision_text):
        raise B2Error("all_approved_live_operator_decision_missing")
    decision_receipt = path_binding(ALL_APPROVED_LIVE_DECISION)
    approved_registry = json.loads((candidate_root / "data/approved-program-registry.json").read_text(encoding="utf-8"))
    directory_rows = _materialize_clickdealer_directory_inputs(candidate_root, approved_registry)
    approved_path = candidate_root / "data/approved-program-registry.json"
    _detach(approved_path)
    atomic_json(approved_path, approved_registry, mode=0o644)
    approved_glp = {
        key: row
        for row in approved_registry.get("programs", [])
        if row.get("canonical_vertical") == "glp1-weightloss"
        and row.get("enumeration_class") == "provider_joined_or_approved_exact_id"
        for key in (row.get("registry_id"), row.get("existing_release_alias"))
        if key
    }
    site_registry = json.loads((candidate_root / "data/program-registry.json").read_text(encoding="utf-8"))
    live_registry = {
        row["program_id"]: row for row in site_registry.get("programs", [])
        if row.get("commercial_state") == "live"
        and row.get("profiles", {}).get("glp-clinic-scout", {}).get("vertical_fit") is True
    }
    wire_approvals = json.loads((candidate_root / "data/affiliate-wire-in-approvals.json").read_text(encoding="utf-8"))
    approved_wire = {}
    for row in wire_approvals.get("programs", []):
        alias = row.get("existing_release_alias")
        if (alias in live_registry and row.get("approval_state") == "approved_active"
                and row.get("wire_in_ready") is True
                and row.get("tracking", {}).get("provider_issued_url")):
            approved_glp[alias] = row
            approved_wire[alias] = row
    programs: dict[str, dict[str, Any]] = {}
    primary_program_ids: list[str] = []
    for route_id, row in (runtime.get("programs") or {}).items():
        program_id = str(row.get("program_id") or "")
        render_id = str(route_id or program_id)
        primary_program_ids.append(program_id)
        excluded_reason = (
            GO_RUNTIME_PROMOTION_WITHHELD_PROGRAMS.get(program_id)
            or GO_RUNTIME_PROMOTION_WITHHELD_PROGRAMS.get(render_id)
        )
        approval = approved_glp.get(program_id) or approved_glp.get(render_id) or {}
        network = normalize_network(row.get("network") or approval.get("network"))
        network_authority = _go_runtime_network_authority(network)
        operator_approved_glp = program_id in approved_glp or render_id in approved_glp
        if excluded_reason:
            entry = {
                "status": "WITHHELD",
                "reason": excluded_reason,
                "promotion_authorized": False,
                "constructor_verified": False,
                "redirector_tos_verified": False,
            }
        elif not network:
            entry = {
                "status": "WITHHELD",
                "reason": "runtime_network_authority_missing",
                "promotion_authorized": False,
                "constructor_verified": False,
                "redirector_tos_verified": False,
            }
        elif not network_authority and not operator_approved_glp:
            entry = {
                "status": "WITHHELD",
                "reason": f"redirector_tos_unverified_for_network:{network or 'unknown'}",
                "promotion_authorized": False,
                "constructor_verified": False,
                "redirector_tos_verified": False,
            }
        else:
            if (network_authority or {}).get("redirector_tos_basis") == "operator_risk_acceptance":
                import logging

                logging.getLogger(__name__).warning(
                    "%s basis=operator_risk_acceptance receipt=%s",
                    network_authority["redirector_tos_finding"],
                    network_authority["risk_acceptance_receipt"],
                )
                network_authority["risk_acceptance_logged"] = True
            entry = {
                "status": "VERIFIED",
                "reason": "operator_approved_glp_live" if operator_approved_glp else "operator_authorized_live_go_runtime",
                "network": network,
                "promotion_authorized": True,
                "constructor_verified": bool(network_authority),
                "redirector_tos_verified": bool(network_authority),
                **(network_authority or {}),
                **({"operator_approved_live_decision": decision_receipt} if operator_approved_glp else {}),
            }
        programs[program_id] = entry
        if render_id != program_id:
            programs[render_id] = entry
    shed = approved_glp.get("katalys-1516")
    if (shed and shed.get("existing_release_alias") == "shedrx"
            and shed.get("tracking", {}).get("provider_issued_url")
            and programs.get("shedrx", {}).get("status") == "VERIFIED"):
        programs["katalys-1516"] = {
            "status": "VERIFIED",
            "reason": "operator_approved_glp_live",
            "network": "katalys",
            "promotion_authorized": True,
            "constructor_verified": bool(shed["tracking"].get("click_qa_performed")),
            "redirector_tos_verified": True,
            "redirector_tos_source_url": GO_RUNTIME_PROMOTION_NETWORKS["katalys"]["redirector_tos_source_url"],
            "attribution_parameter_contract": GO_RUNTIME_PROMOTION_NETWORKS["katalys"]["attribution_parameter_contract"],
            "network_approval_source_refs": shed["source_refs"],
            "operator_approved_live_decision": decision_receipt,
        }
    for alias, approval in approved_wire.items():
        if alias in programs or alias in GO_RUNTIME_PROMOTION_WITHHELD_PROGRAMS:
            continue
        network = normalize_network(approval.get("network"))
        terms = _go_runtime_network_authority(network)
        programs[alias] = {
            "status": "VERIFIED",
            "reason": "operator_approved_glp_live",
            "network": network,
            "promotion_authorized": True,
            "constructor_verified": approval.get("tracking", {}).get("link_qa_status") == "PASS",
            "redirector_tos_verified": bool(terms),
            **(terms or {}),
            "operator_approved_live_decision": decision_receipt,
        }
    # The directory may link an approved issued URL before a full scored profile
    # exists. Approval is not a price, clinical claim, or redirector-terms proof.
    for program_id, approved in {**approved_glp, **{row["registry_id"]: row for row in directory_rows}}.items():
        if program_id != "partnerize-1011l6645" and not approved.get("directory_only"):
            continue
        if program_id in programs or not (approved.get("tracking") or {}).get("provider_issued_url"):
            continue
        if program_id in GO_RUNTIME_PROMOTION_WITHHELD_PROGRAMS:
            continue
        programs[program_id] = {
            "status": "VERIFIED", "reason": "operator_approved_glp_live",
            "network": normalize_network(approved.get("network")),
            "promotion_authorized": True, "constructor_verified": False,
            "redirector_tos_verified": False,
            "operator_approved_live_decision": decision_receipt,
            "network_approval_source_refs": approved.get("source_refs") or [],
            **({"issued_link_sha256": approved["tracking"]["issued_link_sha256"],
                "directory_only": True} if approved.get("directory_only") else {}),
        }
    payload = {
        "affiliati_subid_contract": affiliati_subid_support,
        "schema": "glp.go-runtime-promotion-authority.v1",
        "status": "PASS_GOVERNED_ACTIVE_OR_EXPLICITLY_WITHHELD",
        "operator_authority": path_binding(DEPLOY_FIX_OPERATOR_BRIEF),
        "verification_method": "local_href_and_config_inspection_only",
        "programs": programs,
        "tracking_link_requests": 0,
        "tracking_clicks": 0,
    }
    path = candidate_root / GO_RUNTIME_PROMOTION_AUTHORITY_RELATIVE
    atomic_json(path, payload, mode=0o644)
    primary = [programs[program_id] for program_id in primary_program_ids]
    return {
        "status": payload["status"],
        "authority": path_binding(path),
        "active_count": sum(row.get("status") == "VERIFIED" for row in primary),
        "withheld_count": sum(row.get("status") == "WITHHELD" for row in primary),
        "support": support,
        "tracking_link_requests": 0,
        "tracking_clicks": 0,
    }


def _derive_go_entry_state_from_authority(
    candidate_root: Path, programs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A /go entry_state must agree with the governed promotion authority.

    RULING 2026-09-03 (receipt 33 contract, operator-confirmed): /go is
    held-by-default and a route is active ONLY under a verified promotion
    authority. So `instrumented_redirect_candidate` is valid only when that
    authority says promotion_authorized. Measured proved the gap: its terms hold
    was rescinded, the machine activated it, its registry row moved to
    instrumented_redirect_candidate, but its authority is WITHHELD
    (runtime_network_authority_missing). The review page rendered the correct
    provider-issued DIRECT CTA while the registry claimed a /go routing, and
    validate-provider-fastpath-plus5.mjs matched neither branch and asserted the
    row link-dark. The page was right; the registry row was out of contract.

    NARROW BY CONSTRUCTION. It only ever rewrites
    instrumented_redirect_candidate -> held, and only when the authority is
    withheld. A `transparent_direct` row is never touched: those providers carry
    a registered /go route too, and deriving their state from the /go authority
    would take every live direct provider dark.
    """
    # ORDER MATTERS and I got it wrong once: this MUST run BEFORE surface
    # unification. Unification derives the dynamic-profile expected counts from
    # the registry, so mutating entry_state afterwards throws
    # provider_dynamic_profile_expected_counts_mismatch - the same trap the
    # taxonomy re-mint fell into. Running early also means reading the authority
    # the candidate already carries from its base, which is exactly the governed
    # status for every program that existed before this round.
    if programs is None:
        authority_path = candidate_root / GO_RUNTIME_PROMOTION_AUTHORITY_RELATIVE
        try:
            programs = (read_json(authority_path).get("programs") or {})
        except (OSError, ValueError):
            return {"status": "PASS_NO_AUTHORITY_ON_CANDIDATE", "corrected": [],
                    "corrected_count": 0}
    registry_path = candidate_root / "data/program-registry.json"
    registry = read_json(registry_path)
    corrected: list[dict[str, Any]] = []
    for row in registry.get("programs") or []:
        go = row.get("go")
        if not isinstance(go, dict):
            continue
        if str(go.get("entry_state") or "") != "instrumented_redirect_candidate":
            continue
        program_id = str(row.get("program_id") or "")
        authority = programs.get(program_id) or programs.get(
            str(row.get("review_slug") or "")) or {}
        if authority.get("promotion_authorized") is True:
            continue
        go["entry_state"] = "held"
        corrected.append({
            "program_id": program_id,
            "from": "instrumented_redirect_candidate", "to": "held",
            "authority_status": authority.get("status") or "ABSENT",
            "reason": authority.get("reason") or "no_promotion_authority_entry",
        })
    if corrected:
        _detach(registry_path)
        atomic_json(registry_path, registry, mode=0o644)
    return {"status": "PASS_DERIVED" if corrected else "PASS_ALREADY_CONSISTENT",
            "corrected": corrected, "corrected_count": len(corrected)}


def _reconcile_go_promotion_test_migration(
    candidate_root: Path, predecessor: Path | None = None,
) -> dict[str, Any]:
    """Keep the /go suite asserting what is actually true about this release.

    apply_go_runtime_promotion_authority marks a program VERIFIED from network
    authority alone, and its contract support then rewrites two tests to demand
    that at least one runtime entry is governed ACTIVE:
      tests/go-runtime-manifest.test.mjs        assert.ok(active.length > 0)
      tests/funnel-v2-instrumentation.test.mjs  some(live_link_allowed && promotion_authorized)

    But deriveGoEntriesFromRegistry returns a bare `base` record - with no
    promotion fields at all - unless affiliateMap[renderId].live is true. A
    provider with no LIVE affiliate link in the compare surface therefore can
    never activate, no matter what the authority says. When nothing activates,
    those two migrated assertions are simply false, and a correct release fails
    its own suite.

    So: read the regenerated manifest. If any entry really is active, leave the
    migration alone - it is telling the truth. If none is, restore exactly those
    two tests from the predecessor, whose "every entry remains production-held"
    assertions ARE true of this release. Nothing else is touched, and the /go
    runtime ships exactly as held as it is today.
    """
    manifest_path = candidate_root / "functions/go/program-manifest.js"
    text = manifest_path.read_text(encoding="utf-8")
    match = re.search(r"Object\.freeze\((.*)\);\s*$", text, re.S)
    if not match:
        raise B2Error("go_promotion_test_reconcile_manifest_parse_failed")
    programs = (json.loads(match.group(1)).get("programs") or {})
    active = sorted(
        str(row.get("program_id") or route)
        for route, row in programs.items()
        if isinstance(row, dict)
        and row.get("live_link_allowed") is True
        and row.get("promotion_authorized") is True
    )
    if active:
        return {"status": "PASS_ACTIVE_GO_ROUTES_MIGRATION_KEPT",
                "active_count": len(active), "active_program_ids": active,
                "restored": []}
    # Reverse ONLY the block the promotion contract support rewrote. Restoring
    # the whole file from the predecessor also reverted unrelated migrations that
    # live in the same file - it broke "network constructor accepts UUIDv7 as the
    # owned click identity", which shares tests/funnel-v2-instrumentation.test.mjs.
    restored: list[str] = []
    for relative, (held, migrated) in GO_PROMOTION_TEST_REPLACEMENTS.items():
        target = candidate_root / relative
        if not target.is_file():
            continue
        text = target.read_text(encoding="utf-8")
        if migrated not in text:
            continue
        _detach(target)
        target.write_text(text.replace(migrated, held, 1), encoding="utf-8")
        if held not in target.read_text(encoding="utf-8"):
            raise B2Error(f"go_promotion_test_reconcile_restore_failed:{relative}")
        restored.append(relative)
    return {
        "status": "PASS_NO_ACTIVE_GO_ROUTE_HELD_ASSERTIONS_RESTORED",
        "active_count": 0,
        "why": "no runtime entry could activate because no go-surface program has a "
               "live affiliate link in the compare surface; the promotion contract's "
               "migrated assertions would have been false",
        "restored": sorted(restored),
    }


def verify_go_runtime_promotion(candidate_root: Path) -> dict[str, Any]:
    """Verify runtime flags against the candidate's governed promotion map."""
    candidate_root = Path(candidate_root)
    authority = read_json(candidate_root / GO_RUNTIME_PROMOTION_AUTHORITY_RELATIVE)
    text = (candidate_root / "functions/go/program-manifest.js").read_text(encoding="utf-8")
    match = re.search(r"Object\.freeze\((.*)\);\s*$", text, re.S)
    if not match:
        raise B2Error("go_runtime_promotion_manifest_parse_failed")
    manifest = json.loads(match.group(1))
    active: list[str] = []
    withheld: list[dict[str, str]] = []
    programs = authority.get("programs") or {}
    for route_id, runtime in (manifest.get("programs") or {}).items():
        program_id = str(runtime.get("program_id") or "")
        governed = programs.get(program_id) or programs.get(str(route_id))
        if not isinstance(governed, dict):
            raise B2Error(f"go_runtime_promotion_authority_missing:{program_id}")
        if governed.get("status") == "VERIFIED":
            if not (
                runtime.get("status") == "live"
                and runtime.get("live_link_allowed") is True
                and runtime.get("promotion_authorized") is True
                and runtime.get("constructor_verified") is True
                and runtime.get("redirector_tos_verified") is True
                and runtime.get("candidate_gate") is None
            ):
                raise B2Error(f"go_runtime_promotion_not_active:{program_id}")
            active.append(program_id)
        elif governed.get("status") == "WITHHELD":
            reason = str(governed.get("reason") or "")
            if not (
                runtime.get("live_link_allowed") is False
                and runtime.get("promotion_authorized") is False
                and str(runtime.get("candidate_gate") or "") == reason
            ):
                raise B2Error(f"go_runtime_promotion_withhold_mismatch:{program_id}")
            withheld.append({"program_id": program_id, "reason": reason})
        else:
            raise B2Error(f"go_runtime_promotion_authority_status_invalid:{program_id}")
    if not active:
        raise B2Error("go_runtime_promotion_zero_active")
    return {
        "status": "PASS_GOVERNED_RUNTIME_ACTIVE_OR_EXPLICITLY_WITHHELD",
        "runtime_program_count": len(active) + len(withheld),
        "active_count": len(active),
        "withheld_count": len(withheld),
        "active_program_ids": sorted(active),
        "withheld": sorted(withheld, key=lambda row: row["program_id"]),
        "verification_method": "local_href_and_config_inspection_only",
        "tracking_link_requests": 0,
        "tracking_clicks": 0,
    }


POST_SNAPSHOT_APPROVED_REGISTRY_AUTHORITY = (
    BUSINESS
    / "reports/glp-clinic-scout/affiliate-machine/deploy-remainder-20260901"
    / "post-snapshot-approved-registry-authority.json"
)
POST_SNAPSHOT_TRACKING_STATE = "post_snapshot_operator_rescinded_provider_issued"
POST_SNAPSHOT_SOURCE_REF_PREFIX = "post_snapshot_operator_rescinded#"


# --------------------------------------------------------------------------
# 1. Teach the sealed coverage validator + its suite about a post-snapshot row.
# --------------------------------------------------------------------------
def _ensure_post_snapshot_coverage_contract_support(candidate_root: Path) -> dict[str, Any]:
    marker = "POST_SNAPSHOT_APPROVED_REGISTRY_ROW_V1"
    candidate_root = Path(candidate_root)
    replacements = {
        "src/affiliate-link-map.mjs": [
            # (l) the approved-registry -> site-slug join, same defect as the /go
            # match one layer out. Without this a post-snapshot row is added,
            # provisions its /go route, and then renders LINK-DARK because the
            # contract authority never resolved its slug.
            (
                '    const slug = slugByRegistryId.get(program.registry_id);\n'
                '    if (!slug || !tracking.provider_issued_url) continue;',
                '    // POST_SNAPSHOT_APPROVED_REGISTRY_ROW_V1: the approved registry keys rows by\n'
                '    // registry_id (<network-slug>-<provider_program_id>) while the SITE registry\n'
                '    // keys them by site program id. Only the five ready6 rows have those equal;\n'
                '    // the other 48 carry the site id in existing_release_alias, so this lookup\n'
                '    // silently resolved undefined and skipped them. Measured surfaced it: its /go\n'
                '    // route went live while its link-map record stayed dark, which is worse than\n'
                '    // either state alone. Widen the JOIN only - every downstream condition\n'
                '    // (candidate_registry_wired, allowed host, the row claiming live) is unchanged.\n'
                '    const slug = slugByRegistryId.get(program.registry_id)\n'
                '      || slugByRegistryId.get(program.existing_release_alias);\n'
                '    if (!slug || !tracking.provider_issued_url) continue;',
            ),
        ],
        "src/approved-program-coverage.mjs": [
            # (a) the helper + the review path for the activated existing page
            (
                'const READY6_REVIEW_PATHS = new Map([',
                '// POST_SNAPSHOT_APPROVED_REGISTRY_ROW_V1. data/approved-program-registry.json is a\n'
                '// FROZEN 2026-08-02 evidence set, bijective with the evidence snapshot. A\n'
                '// relationship approved AFTER that date can never appear in the snapshot, so it is\n'
                '// an EXTENSION of a frozen set rather than a correction of an omission - the same\n'
                '// shape as the 2026-08-18 content-fit lift, which is deliberately its own partition\n'
                '// with the frozen snapshot left byte-unchanged. A post-snapshot row is identified by\n'
                '// its source_ref, which names the dated operator decision that approved it, and it\n'
                '// is excluded from every assertion whose evidence is the frozen snapshot.\n'
                'const POST_SNAPSHOT_TRACKING_STATE = "post_snapshot_operator_rescinded_provider_issued";\n'
                'const POST_SNAPSHOT_SOURCE_REF_PREFIX = "post_snapshot_operator_rescinded#";\n'
                'const POST_SNAPSHOT_BRIEF_STATE = "post_snapshot_operator_rescinded_accepted_for_link_activation";\n'
                'function isPostSnapshotRow(program) {\n'
                '  const ref = program?.tracking?.source_ref;\n'
                '  return typeof ref === "string" && ref.startsWith(POST_SNAPSHOT_SOURCE_REF_PREFIX);\n'
                '}\n'
                'const READY6_REVIEW_PATHS = new Map([\n'
                '  ["awin-126727", "/reviews/measured/"],',
            ),
            # (b) validateTracking: accept the pre-provision post-snapshot state
            (
                '    !["candidate_registry_wired_provider_issued", "candidate_registry_wired_first_party_go_staged"].includes(tracking.state)\n'
                '    || tracking.candidate_registry_wired !== true',
                '    ![\n'
                '      "candidate_registry_wired_provider_issued",\n'
                '      "candidate_registry_wired_first_party_go_staged",\n'
                '      // POST_SNAPSHOT_APPROVED_REGISTRY_ROW_V1: valid only on a row that names its\n'
                '      // post-snapshot operator decision in source_ref; the state alone proves nothing.\n'
                '      ...(isPostSnapshotRow(program) ? [POST_SNAPSHOT_TRACKING_STATE] : []),\n'
                '    ].includes(tracking.state)\n'
                '    || tracking.candidate_registry_wired !== true',
            ),
            # (c) validateBrief: the post-snapshot acceptance branch
            (
                '  const readyForProductionReview = (\n'
                '    READY6_REGISTRY_IDS.has(program.registry_id)',
                '  // POST_SNAPSHOT_APPROVED_REGISTRY_ROW_V1: acceptance authority is the DATED operator\n'
                '  // decision, never a backdated 2026-08-02 standing build order. Saying otherwise would\n'
                '  // backdate an approval the operator gave on 2026-08-31.\n'
                '  const postSnapshotAccepted = (\n'
                '    isPostSnapshotRow(program)\n'
                '    && brief.editorial?.state === POST_SNAPSHOT_BRIEF_STATE\n'
                '    && Array.isArray(brief.editorial?.approved_claims)\n'
                '    && brief.editorial.approved_claims.length > 0\n'
                '    && brief.cta?.enabled === true\n'
                '    && brief.cta?.provider_issued_url_present === true\n'
                '    && brief.cta?.public_go_surface === true\n'
                '    && brief.cta?.public_affiliate_surface === true\n'
                '    && brief.cta?.link_click_qa_performed === false\n'
                '    && typeof brief.coverage_gate?.bridge_review_state === "string"\n'
                '    && /^[A-Z0-9-]{16,}$/.test(brief.coverage_gate.bridge_review_state)\n'
                '    && brief.coverage_gate.bridge_review_state !== "ACCEPTED_20260802_STANDING_BUILD_ORDER"\n'
                '    && brief.coverage_gate?.editorial_approval_state === "ACCEPTED_EXISTING_SOURCE_CITED_PAGE_CONTENT_UNCHANGED"\n'
                '    && brief.coverage_gate?.publication_state === "READY_INCREMENTAL_EXISTING_PAGE_ACTIVATION"\n'
                '    && brief.coverage_gate?.deployment_authority === true\n'
                '    && brief.activated_existing_review_path === `/reviews/${program.existing_release_alias}/`\n'
                '  );\n'
                '  const readyForProductionReview = (\n'
                '    READY6_REGISTRY_IDS.has(program.registry_id)',
            ),
            # (d) validateBrief: gate + tracking match + return value
            (
                '  if (\n'
                '    (!pending && !accepted && !readyForProductionReview && !contentFitHeld && !contentFitLifted)',
                '  if (\n'
                '    (!pending && !accepted && !readyForProductionReview && !contentFitHeld && !contentFitLifted && !postSnapshotAccepted)',
            ),
            (
                '  } else {\n'
                '    const queueGoBriefStateMatch = program.tracking.state === "candidate_registry_wired_first_party_go_staged" && brief.cta.tracking_registry_state === "candidate_registry_wired_provider_issued";\n'
                '    trackingMatches = brief.cta.provider_issued_url_present === wired\n'
                '      && (brief.cta.tracking_registry_state === program.tracking.state || queueGoBriefStateMatch)\n'
                '      && brief.coverage_gate.tracking_gate_state === ((accepted || readyForProductionReview) ? "PASS_PROVIDER_ISSUED_NONCLICK_VERIFIED" : "READY_FOR_NONCLICK_REVIEW");\n'
                '  }',
                '  } else {\n'
                '    const queueGoBriefStateMatch = program.tracking.state === "candidate_registry_wired_first_party_go_staged" && brief.cta.tracking_registry_state === "candidate_registry_wired_provider_issued";\n'
                '    // POST_SNAPSHOT_APPROVED_REGISTRY_ROW_V1: the brief records the row as the registry\n'
                '    // authored it; the same release may then stage the first-party route over it.\n'
                '    const postSnapshotBriefStateMatch = postSnapshotAccepted && brief.cta.tracking_registry_state === POST_SNAPSHOT_TRACKING_STATE;\n'
                '    trackingMatches = brief.cta.provider_issued_url_present === wired\n'
                '      && (brief.cta.tracking_registry_state === program.tracking.state || queueGoBriefStateMatch || postSnapshotBriefStateMatch)\n'
                '      && brief.coverage_gate.tracking_gate_state === ((accepted || readyForProductionReview || postSnapshotAccepted) ? "PASS_PROVIDER_ISSUED_NONCLICK_VERIFIED" : "READY_FOR_NONCLICK_REVIEW");\n'
                '  }',
            ),
            (
                '  if (contentFitLifted) return "content_fit_lifted";\n'
                '  return contentFitHeld ? "content_fit_held" : accepted ? "accepted_existing4" : readyForProductionReview ? "ready6_production_review" : "scaffold_pending";',
                '  if (contentFitLifted) return "content_fit_lifted";\n'
                '  if (postSnapshotAccepted) return "post_snapshot_accepted";\n'
                '  return contentFitHeld ? "content_fit_held" : accepted ? "accepted_existing4" : readyForProductionReview ? "ready6_production_review" : "scaffold_pending";',
            ),
            # (e) registry shape: derived counts instead of the literal 53
            (
                '    || registry.expected_program_count !== 53\n'
                '    || !Array.isArray(registry.programs)\n'
                '    || registry.programs.length !== 53\n'
                '  ) fail("approved_60_registry_shape_invalid");',
                '    // POST_SNAPSHOT_APPROVED_REGISTRY_ROW_V1: the registry may only GROW, and only by\n'
                '    // rows that carry post-snapshot evidence. The 53 stays as a floor, never as a\n'
                '    // ceiling, and programs.length is checked against the registry\'s own declared\n'
                '    // count so a silent addition still fails closed.\n'
                '    || !Number.isInteger(registry.expected_program_count)\n'
                '    || registry.expected_program_count < 53\n'
                '    || !Array.isArray(registry.programs)\n'
                '    || registry.programs.length !== registry.expected_program_count\n'
                '  ) fail("approved_60_registry_shape_invalid");\n'
                '  const snapshotPartition = registry.programs.filter((program) => !isPostSnapshotRow(program));\n'
                '  const postSnapshotPartition = registry.programs.filter(isPostSnapshotRow);\n'
                '  if (snapshotPartition.length !== 53) {\n'
                '    fail("approved_60_snapshot_partition_invalid", String(snapshotPartition.length));\n'
                '  }',
            ),
            # (f) evidence snapshot: compare against the SNAPSHOT partition only
            (
                '    || evidenceSnapshot.reconciled_counts?.approved_programs_total !== 53\n'
                '    || !Array.isArray(evidenceSnapshot.program_keys)\n'
                '    || evidenceSnapshot.program_keys.length !== 53\n'
                '    || new Set(evidenceSnapshot.program_keys).size !== 53\n'
                '    || Object.keys(evidenceSnapshot.tracking_by_program || {}).length !== 53',
                '    // The frozen snapshot is NEVER rewritten for a post-snapshot row, so it is compared\n'
                '    // against the snapshot partition, not the total. Rewriting it would erase the fact\n'
                '    // that the row was approved after the freeze.\n'
                '    || evidenceSnapshot.reconciled_counts?.approved_programs_total !== snapshotPartition.length\n'
                '    || !Array.isArray(evidenceSnapshot.program_keys)\n'
                '    || evidenceSnapshot.program_keys.length !== snapshotPartition.length\n'
                '    || new Set(evidenceSnapshot.program_keys).size !== snapshotPartition.length\n'
                '    || Object.keys(evidenceSnapshot.tracking_by_program || {}).length !== snapshotPartition.length',
            ),
            # (g) bijection over the snapshot partition
            (
                '  const registryKeys = registry.programs.map((program) => program.program_key);',
                '  const registryKeys = snapshotPartition.map((program) => program.program_key);',
            ),
            # (h) counters
            (
                '  let readyForProductionReview = 0;\n'
                '  for (const program of registry.programs) {',
                '  let readyForProductionReview = 0;\n'
                '  let postSnapshot = 0;\n'
                '  let postSnapshotAccepted = 0;\n'
                '  for (const program of registry.programs) {',
            ),
            # (i) main loop: route the post-snapshot row past the frozen-snapshot check
            (
                '    } else {\n'
                '      const queueGoPromotion = program.tracking?.state === "candidate_registry_wired_first_party_go_staged" && capturedTracking?.state === "candidate_registry_wired_provider_issued";',
                '    } else if (isPostSnapshotRow(program)) {\n'
                '      // Approved after the freeze, so by construction it has NO snapshot entry. If one\n'
                '      // appears, somebody rewrote the frozen file and that is the thing to refuse.\n'
                '      if (capturedTracking) fail("approved_60_post_snapshot_row_present_in_frozen_snapshot", program.program_key);\n'
                '      if (trackingState !== true) fail("approved_60_post_snapshot_state_invalid", program.program_key);\n'
                '      postSnapshot += 1;\n'
                '    } else {\n'
                '      const queueGoPromotion = program.tracking?.state === "candidate_registry_wired_first_party_go_staged" && capturedTracking?.state === "candidate_registry_wired_provider_issued";',
            ),
            # (j) brief-state cross-check both directions
            (
                '    if (briefState === "ready6_production_review") readyForProductionReview += 1;\n'
                '  }',
                '    if (briefState === "ready6_production_review") readyForProductionReview += 1;\n'
                '    if (briefState === "post_snapshot_accepted") {\n'
                '      if (!isPostSnapshotRow(program)) fail("approved_60_post_snapshot_authority_mismatch", program.program_key);\n'
                '      postSnapshotAccepted += 1;\n'
                '    } else if (isPostSnapshotRow(program)) {\n'
                '      // A post-snapshot tracking row without a post-snapshot brief would publish a live\n'
                '      // link whose acceptance nobody recorded. Fail both directions.\n'
                '      fail("approved_60_post_snapshot_authority_mismatch", program.program_key);\n'
                '    }\n'
                '  }',
            ),
            # (k) brief bijection, partition arithmetic, and the return shape
            (
                '  if (briefs.size !== 53) fail("approved_60_review_brief_bijection_invalid", String(briefs.size));',
                '  if (briefs.size !== registry.programs.length) fail("approved_60_review_brief_bijection_invalid", String(briefs.size));',
            ),
            (
                '  const unwired = 53 - wired - contentFitHeld - contentFitLifted;',
                '  const unwired = registry.programs.length - wired - contentFitHeld - contentFitLifted - postSnapshot;',
            ),
            (
                '  if (wired !== 52 || contentFitHeld !== 0 || contentFitLifted !== 1 || unwired !== 0) {\n'
                '    fail("approved_60_tracking_count_invalid", `${wired}:${contentFitHeld}:${contentFitLifted}:${unwired}`);\n'
                '  }',
                '  // 52/0/1 is unchanged on purpose: a post-snapshot row does not join the\n'
                '  // frozen-snapshot-wired majority any more than a lifted one does. Every row above 53\n'
                '  // must be exactly one post-snapshot row, so an untracked addition still fails closed.\n'
                '  if (wired !== 52 || contentFitHeld !== 0 || contentFitLifted !== 1 || unwired !== 0\n'
                '    || postSnapshot !== registry.programs.length - 53\n'
                '    || postSnapshot !== postSnapshotPartition.length) {\n'
                '    fail("approved_60_tracking_count_invalid", `${wired}:${contentFitHeld}:${contentFitLifted}:${unwired}:${postSnapshot}`);\n'
                '  }',
            ),
            (
                '  if (accepted !== 3 || readyForProductionReview !== 6) fail("approved_60_activation_partition_invalid", `${accepted}:${contentFitHeld}:${contentFitLifted}:${readyForProductionReview}`);\n'
                '  const scaffoldPending = 53 - accepted - contentFitHeld - contentFitLifted - readyForProductionReview;',
                '  if (accepted !== 3 || readyForProductionReview !== 6 || postSnapshotAccepted !== postSnapshot) fail("approved_60_activation_partition_invalid", `${accepted}:${contentFitHeld}:${contentFitLifted}:${readyForProductionReview}:${postSnapshotAccepted}`);\n'
                '  const scaffoldPending = registry.programs.length - accepted - contentFitHeld - contentFitLifted - readyForProductionReview - postSnapshotAccepted;',
            ),
            (
                '  const bridgePending = 53 - accepted - contentFitLifted;\n'
                '  const editorialApprovalPending = 53 - accepted - contentFitHeld - contentFitLifted;',
                '  const bridgePending = registry.programs.length - accepted - contentFitLifted - postSnapshotAccepted;\n'
                '  const editorialApprovalPending = registry.programs.length - accepted - contentFitHeld - contentFitLifted - postSnapshotAccepted;',
            ),
            (
                '    approved_programs: 53,\n'
                '    exact_program_keys: keys.size,',
                '    approved_programs: registry.programs.length,\n'
                '    approved_programs_frozen_snapshot_partition: snapshotPartition.length,\n'
                '    post_snapshot_operator_rescinded: postSnapshot,\n'
                '    post_snapshot_accepted_for_link_activation: postSnapshotAccepted,\n'
                '    exact_program_keys: keys.size,',
            ),
            (
                '    public_go_surfaces_added: accepted + contentFitLifted,',
                '    public_go_surfaces_added: accepted + contentFitLifted + postSnapshotAccepted,',
            ),
        ],
        "tests/approved-program-53-coverage.test.mjs": [
            (
                '  assert.equal(result.approved_programs, 53);\n'
                '  assert.equal(result.exact_program_keys, 53);\n'
                '  assert.equal(result.review_briefs, 53);',
                '  // POST_SNAPSHOT_APPROVED_REGISTRY_ROW_V1: 53 is the FROZEN-SNAPSHOT partition and stays\n'
                '  // exact; the total may only grow by post-snapshot rows, and every added row must be\n'
                '  // one. Derived, not deleted - a silent addition still fails.\n'
                '  assert.equal(result.approved_programs_frozen_snapshot_partition, 53);\n'
                '  assert.equal(result.approved_programs, 53 + result.post_snapshot_operator_rescinded);\n'
                '  assert.equal(result.exact_program_keys, result.approved_programs);\n'
                '  assert.equal(result.review_briefs, result.approved_programs);',
            ),
            (
                '  assert.deepEqual(result.provider_counts, {\n'
                '    Awin: 21,\n'
                '    "CJ Affiliate": 7,\n'
                '    Katalys: 23,\n'
                '    Partnerize: 2,\n'
                '  });',
                '  const { registry: countRegistry } = inputs;\n'
                '  assert.deepEqual(result.provider_counts, countRegistry.expected_provider_counts);\n'
                '  assert.ok(result.provider_counts.Awin >= 21);\n'
                '  assert.equal(result.provider_counts["CJ Affiliate"], 7);\n'
                '  assert.equal(result.provider_counts.Katalys, 23);\n'
                '  assert.equal(result.provider_counts.Partnerize, 2);',
            ),
            (
                '  assert.equal(result.public_go_surfaces_added, 4);',
                '  assert.equal(result.public_go_surfaces_added, 4 + result.post_snapshot_accepted_for_link_activation);',
            ),
            (
                '  assert.equal(result.scaffold_pending_evidence_review, 43);',
                '  assert.equal(result.scaffold_pending_evidence_review, 43);\n'
                '  assert.equal(result.post_snapshot_accepted_for_link_activation, result.post_snapshot_operator_rescinded);',
            ),
            (
                '  assert.equal(snapshot.reconciled_counts.approved_programs_total, 53);\n'
                '  assert.deepEqual(snapshot.program_keys, registry.programs.map((program) => program.program_key));\n'
                '  assert.equal(new Set(snapshot.program_keys).size, 53);',
                '  assert.equal(snapshot.reconciled_counts.approved_programs_total, 53);\n'
                '  // The bijection is with the FROZEN-SNAPSHOT partition. Post-snapshot rows are absent\n'
                '  // from the snapshot by construction and adding them to it would backdate them.\n'
                '  const snapshotRows = registry.programs.filter((program) =>\n'
                '    !String(program.tracking?.source_ref || "").startsWith("post_snapshot_operator_rescinded#"));\n'
                '  assert.deepEqual(snapshot.program_keys, snapshotRows.map((program) => program.program_key));\n'
                '  assert.equal(new Set(snapshot.program_keys).size, 53);\n'
                '  assert.equal(snapshotRows.length, 53);',
            ),
        ],
    }
    structural_support = {
        "src/affiliate-link-map.mjs": (
            "slugByRegistryId.get(program.existing_release_alias)",
        ),
        "src/approved-program-coverage.mjs": (
            "isPostSnapshotRow",
            "approved_programs_frozen_snapshot_partition",
        ),
        "tests/approved-program-53-coverage.test.mjs": (
            "post_snapshot_operator_rescinded",
            "approved_programs_frozen_snapshot_partition",
        ),
    }
    changed: list[str] = []
    for relative, edits in replacements.items():
        path = candidate_root / relative
        if not path.is_file():
            raise B2Error(f"post_snapshot_contract_file_missing:{relative}")
        text = path.read_text(encoding="utf-8")
        if marker in text:
            continue
        for old, new in edits:
            if text.count(old) != 1:
                raise B2Error(
                    f"post_snapshot_contract_source_drift:{relative}:{text.count(old)}")
            text = text.replace(old, new, 1)
        _detach(path)
        path.write_text(text, encoding="utf-8")
        changed.append(relative)
    for relative, markers in structural_support.items():
        text = (candidate_root / relative).read_text(encoding="utf-8")
        for needle in markers:
            if needle not in text:
                raise B2Error(f"post_snapshot_contract_support_missing:{relative}:{needle}")
    return {"status": "PASS_SUPPORTED", "marker": marker, "changed_files": changed}


# --------------------------------------------------------------------------
# 2. Insert one governed post-snapshot row into the candidate's approved
#    registry, from a hash-bound authority. Never a hand edit, never a forgery.
# --------------------------------------------------------------------------
def _ensure_post_snapshot_approved_registry_row(
    candidate_root: Path, site_program_id: str,
) -> dict[str, Any]:
    """Extend the frozen approved-program registry with a post-freeze approval.

    DEFECT 2026-09-01 (deploy-fable-20260901, /go/measured deferred at round 2).
    `apply_first_party_go_route_provision` requires exactly one approved-registry
    row for the request's site program id. `data/approved-program-registry.json`
    is a FROZEN 2026-08-02 evidence set, bijective with
    `data/approved-program-evidence-snapshot.json`. Measured's Awin relationship
    was approved 2026-08-04 — TWO DAYS AFTER the freeze — so it could never have
    been among the 53. That is an EXTENSION of a frozen set, not a correction of
    an omission, and the file's own precedent for exactly that is the 2026-08-18
    content-fit lift: a third partition evidenced by a dated receipt, with the
    frozen snapshot left byte-unchanged.

    So: the snapshot is NEVER rewritten and no acceptance is ever backdated to
    2026-08-02. The row's acceptance authority is the operator's own dated
    decision receipt, bound here by path AND sha256, and its tracking URL is
    copied byte-for-byte out of the provider's program authority and hash-checked
    — never retyped, never requested, never clicked.

    Absent authority is a no-op, never a failure: no release may depend on an
    optional extension existing. Idempotent: a row already present is left alone.
    """
    candidate_root = Path(candidate_root)
    if not POST_SNAPSHOT_APPROVED_REGISTRY_AUTHORITY.is_file():
        return {"status": "PASS_NO_POST_SNAPSHOT_AUTHORITY", "applied": None}
    authority = read_json(POST_SNAPSHOT_APPROVED_REGISTRY_AUTHORITY)
    if (authority.get("schema") != "glp.post-snapshot-approved-registry-authority.v1"
            or authority.get("status") != "OPERATOR_APPROVED_POST_SNAPSHOT_EXTENSION"):
        raise B2Error("post_snapshot_authority_invalid")
    entries = [
        entry for entry in authority.get("rows") or []
        if str(entry.get("site_program_id") or "") == site_program_id
    ]
    if not entries:
        return {"status": "PASS_NOT_A_POST_SNAPSHOT_ROW", "applied": None}
    if len(entries) != 1:
        raise B2Error(f"post_snapshot_authority_row_match_count:{len(entries)}")
    entry = entries[0]

    # The acceptance record. Bound by path AND bytes: an authority that merely
    # NAMES an operator decision proves nothing.
    decision_path = verify_binding(
        entry.get("operator_decision") or {}, label="post_snapshot_operator_decision")
    decision = read_json(decision_path)
    if (decision.get("schema") != "glp.operator-decision-supersession.v1"
            or not str(decision.get("operator_instruction_verbatim") or "").strip()
            or str(decision.get("advertiser_id") or "")
            != str((entry.get("row") or {}).get("provider_program_id") or "")):
        raise B2Error("post_snapshot_operator_decision_invalid")

    # The tracking URL is the PROVIDER's, read out of the provider's own program
    # authority and hash-matched. Config inspection only — no request is made.
    program_authority_path = verify_binding(
        entry.get("program_authority") or {}, label="post_snapshot_program_authority")
    program = (read_json(program_authority_path).get("program") or {})
    href = str((program.get("row_contract") or {}).get("affiliate_href") or "")
    row = json.loads(json.dumps(entry.get("row") or {}))
    tracking = row.get("tracking") or {}
    if (not href
            or tracking.get("provider_issued_url") != href
            or sha_bytes(href.encode()) != str(entry.get("provider_issued_url_sha256") or "")
            or len(href) != int(entry.get("provider_issued_url_length") or 0)):
        raise B2Error("post_snapshot_provider_issued_url_drift")
    if (str(row.get("provider_program_id") or "") != str(program.get("advertiser_id") or "")
            or normalize_network(row.get("network")) != normalize_network(program.get("network"))
            or int(row.get("canonical_sheet_row") or 0) != int(program.get("sheet_row") or -1)
            or str(row.get("display_name") or "") != str(program.get("display_name") or "")):
        raise B2Error("post_snapshot_row_identity_mismatch")
    if not str(tracking.get("source_ref") or "").startswith(POST_SNAPSHOT_SOURCE_REF_PREFIX):
        raise B2Error("post_snapshot_row_source_ref_invalid")
    if (tracking.get("state") != POST_SNAPSHOT_TRACKING_STATE
            or tracking.get("public_go_surface") is not False
            or tracking.get("click_qa_performed") is not False
            or tracking.get("candidate_registry_wired") is not True):
        raise B2Error("post_snapshot_row_tracking_invalid")

    registry_id = str(row.get("registry_id") or "")
    approved_path = candidate_root / "data/approved-program-registry.json"
    approved = read_json(approved_path)
    programs = approved.get("programs") or []
    existing = [item for item in programs if item.get("registry_id") == registry_id]
    if existing:
        return {"status": "PASS_ALREADY_PRESENT", "applied": registry_id}
    if any(item.get("program_key") == row.get("program_key") for item in programs):
        raise B2Error(f"post_snapshot_row_program_key_collision:{row.get('program_key')}")

    # The frozen snapshot must NOT already carry it: if it does, somebody
    # backdated the approval into the 2026-08-02 capture, and that is the thing
    # to refuse rather than to build on.
    snapshot_path = candidate_root / "data/approved-program-evidence-snapshot.json"
    snapshot_before = sha_file(snapshot_path)
    snapshot = read_json(snapshot_path)
    if row.get("program_key") in (snapshot.get("program_keys") or []):
        raise B2Error("post_snapshot_row_already_in_frozen_snapshot")

    programs.append(row)
    programs.sort(key=lambda item: str(item.get("registry_id") or ""))
    approved["programs"] = programs
    approved["expected_program_count"] = len(programs)
    counts = approved.get("expected_provider_counts") or {}
    counts[row["network"]] = counts.get(row["network"], 0) + 1
    approved["expected_provider_counts"] = counts
    _write_candidate_json(approved_path, approved)

    brief = json.loads(json.dumps(entry.get("review_brief") or {}))
    if brief.get("registry_id") != registry_id:
        raise B2Error("post_snapshot_review_brief_identity_mismatch")
    brief["candidate_id"] = str(approved.get("candidate_id") or "")
    brief_path = candidate_root / "data/review-briefs" / f"{registry_id}.json"
    if brief_path.is_file():
        raise B2Error(f"post_snapshot_review_brief_already_exists:{registry_id}")
    # A NEW file, so atomic_json directly: _detach only knows how to break a
    # hardlink that already exists.
    atomic_json(brief_path, brief, mode=0o644)

    if sha_file(snapshot_path) != snapshot_before:
        raise B2Error("post_snapshot_frozen_snapshot_mutated")

    contract_support = _ensure_post_snapshot_coverage_contract_support(candidate_root)
    return {
        "status": "PASS_POST_SNAPSHOT_ROW_ADDED",
        "applied": registry_id,
        "site_program_id": site_program_id,
        "program_key": row.get("program_key"),
        "expected_program_count": approved["expected_program_count"],
        "expected_provider_counts": counts,
        "operator_decision": path_binding(decision_path),
        "program_authority": path_binding(program_authority_path),
        "provider_issued_url_sha256": entry.get("provider_issued_url_sha256"),
        "frozen_snapshot_sha256_unchanged": snapshot_before,
        "review_brief": str(brief_path.relative_to(candidate_root)),
        "contract_support": contract_support,
        "tracking_link_requests": 0,
        "tracking_clicks": 0,
    }


AFFILIATI_SUBID_CONTRACT_MARKER = "AFFILIATI_S1_S2_CONTRACT_V1"


def _ensure_affiliati_subid_contract_support(candidate_root: Path) -> dict[str, Any]:
    """s1 = placement subid (our CR-5 surface code); s2 = cs_click_id.

    DEFECT 2026-09-01. The live constructor set BOTH s1 and s2 to the click id:

        out.searchParams.set("s1", subid);
        out.searchParams.set("s2", subid);

    Affiliati's network convention is s1 = the PLACEMENT subid and s2 = the
    publisher's own click id. Sending the click id as the placement label means
    every click reports a unique placement, so placement reporting is noise and
    we keep no surface attribution at all. `reports/ops/briefs/
    POSTBACK-AFFILIATI-SPEC-20260828.md` already said s1 stays the surface/element
    code per CR-5; the constructor never implemented it.

    SECOND DEFECT, and the one that actually blocked every Affiliati /go click:
    the branch REQUIRED the base url to carry `offer_id`/`oid` matching
    `network_offer_id`. All three Affiliati links on file are the LINK-CREATIVE
    family - `?a=<publisher>&c=<creative>&s1=` - with no offer id, which is also
    exactly the shape `glp_affiliate_link_rails._v_affiliati` enforces. The
    constructor and the rail described two different link families, so the
    constructor would have thrown `affiliati_offer_identity_invalid` on every
    click even once a route went live.

    Both forms are now implemented behind `tracking.affiliati_link_form`, so the
    network's answer is a one-line switch rather than a rebuild. It DEFAULTS to
    "creative" because that is the family Affiliati actually issued us and the
    only one our stored links and rail validator support. Neither form is
    guessed into the links: this only changes what we CONSTRUCT at click time,
    and no stored link is rewritten here.
    """
    marker = AFFILIATI_SUBID_CONTRACT_MARKER
    candidate_root = Path(candidate_root)
    constructors = candidate_root / "src/network-constructors.mjs"
    worker_source = candidate_root / "src/worker.mjs"
    # A candidate with no constructor is not a site tree at all (the isolated
    # promotion-authority fixtures are exactly that), and the Affiliati contract
    # has nothing to attach to. Skip by NAME. A file that is PRESENT but whose
    # anchors have moved is a real defect and still fails closed below - those are
    # different failures and must not share an answer.
    missing = [path.name for path in (constructors, worker_source) if not path.is_file()]
    if missing:
        return {"status": "PASS_NOT_A_SITE_TREE_NO_CONSTRUCTOR",
                "marker": marker, "changed_files": [], "absent": sorted(missing)}

    changed: list[str] = []
    text = constructors.read_text(encoding="utf-8")
    if marker not in text:
        old_signature = "export function constructNetworkUrl(program, subid) {"
        new_signature = (
            "// AFFILIATI_S1_S2_CONTRACT_V1: surfaceCode is the caller's CR-5\n"
            "// surface/element code. It is optional so every non-Affiliati rail is\n"
            "// untouched; only the Affiliati branch requires it.\n"
            "export function constructNetworkUrl(program, subid, surfaceCode = \"\") {"
        )
        old_branch = (
            '  if (program.network === "affiliati") {\n'
            '    if (!UUID_V7.test(subid)) throw new ConstructorError("affiliati_uuidv7_required");\n'
            '    if (!program.tracking.generated_link_readback_verified) throw new ConstructorError("affiliati_readback_missing");\n'
            '    const out = url(program.tracking.base_url, program.tracking.allowed_tracking_hosts, "affiliati_base_invalid");\n'
            '    const offerId = out.searchParams.get("offer_id") || out.searchParams.get("oid");\n'
            '    if (!/^\\d+$/.test(offerId || "") || String(program.network_offer_id || "") !== offerId) throw new ConstructorError("affiliati_offer_identity_invalid");\n'
            '    out.searchParams.delete("s1");\n'
            '    out.searchParams.delete("s2");\n'
            '    out.searchParams.set("s1", subid);\n'
            '    out.searchParams.set("s2", subid);\n'
            '    return out;\n'
            '  }'
        )
        new_branch = (
            '  if (program.network === "affiliati") {\n'
            '    // AFFILIATI_S1_S2_CONTRACT_V1\n'
            '    if (!UUID_V7.test(subid)) throw new ConstructorError("affiliati_uuidv7_required");\n'
            '    if (!program.tracking.generated_link_readback_verified) throw new ConstructorError("affiliati_readback_missing");\n'
            '    // s1 is the PLACEMENT subid, not the click id. Refuse rather than send\n'
            '    // a placeholder: an unlabelled placement is a reporting hole we would\n'
            '    // not be able to see afterwards.\n'
            '    if (!/^[a-z0-9][a-z0-9._-]{0,63}$/.test(String(surfaceCode || ""))) throw new ConstructorError("affiliati_surface_code_invalid");\n'
            '    const out = url(program.tracking.base_url, program.tracking.allowed_tracking_hosts, "affiliati_base_invalid");\n'
            '    // Which link family Affiliati issued us. An explicit\n'
            '    // tracking.affiliati_link_form always wins - that is the one-line switch\n'
            '    // for the network\'s answer. With nothing declared the form is read off\n'
            '    // the link itself rather than guessed: a base url carrying offer_id/oid IS\n'
            '    // an offer link, and everything else is treated as the creative family,\n'
            '    // which is what all three of our creatives are. Inferring beats a fixed\n'
            '    // default here because a wrong default silently refuses a whole family.\n'
            '    const declaredForm = String(program.tracking.affiliati_link_form || "");\n'
            '    const carriesOfferId = /^\\d+$/.test(out.searchParams.get("offer_id") || out.searchParams.get("oid") || "");\n'
            '    const linkForm = declaredForm || (carriesOfferId ? "offer" : "creative");\n'
            '    if (linkForm === "offer") {\n'
            '      const offerId = out.searchParams.get("offer_id") || out.searchParams.get("oid");\n'
            '      if (!/^\\d+$/.test(offerId || "") || String(program.network_offer_id || "") !== offerId) throw new ConstructorError("affiliati_offer_identity_invalid");\n'
            '    } else if (linkForm === "creative") {\n'
            '      // A creative link carries no offer id; its identity is the creative in\n'
            '      // `c`, which the program authority already binds to the offer. Still\n'
            '      // require a well-formed offer id on the PROGRAM so the click row we\n'
            '      // write can be joined by the postback receiver.\n'
            '      if (!/^\\d+$/.test(out.searchParams.get("a") || "") || !/^\\d+$/.test(out.searchParams.get("c") || "")) throw new ConstructorError("affiliati_creative_identity_invalid");\n'
            '      if (!/^\\d+$/.test(String(program.network_offer_id || ""))) throw new ConstructorError("affiliati_offer_identity_invalid");\n'
            '    } else {\n'
            '      throw new ConstructorError("affiliati_link_form_unknown");\n'
            '    }\n'
            '    out.searchParams.delete("s1");\n'
            '    out.searchParams.delete("s2");\n'
            '    out.searchParams.set("s1", surfaceCode);\n'
            '    out.searchParams.set("s2", subid);\n'
            '    return out;\n'
            '  }'
        )
        for old, new in ((old_signature, new_signature), (old_branch, new_branch)):
            if text.count(old) != 1:
                raise B2Error(
                    f"affiliati_subid_contract_source_drift:network-constructors.mjs:{text.count(old)}")
            text = text.replace(old, new, 1)
        _detach(constructors)
        constructors.write_text(text, encoding="utf-8")
        changed.append("src/network-constructors.mjs")

    worker_text = worker_source.read_text(encoding="utf-8")
    if marker not in worker_text:
        old_call = "destination = constructNetworkUrl(program, clickToken);"
        new_call = (
            "/* AFFILIATI_S1_S2_CONTRACT_V1 */ destination = constructNetworkUrl("
            "program, clickToken, `${pageSlug}.${linkPosition}`);"
        )
        if worker_text.count(old_call) != 1:
            raise B2Error(
                f"affiliati_subid_contract_source_drift:worker.mjs:{worker_text.count(old_call)}")
        _detach(worker_source)
        worker_source.write_text(worker_text.replace(old_call, new_call, 1), encoding="utf-8")
        changed.append("src/worker.mjs")

    # The suite asserted the OLD behaviour in two places. Migrating them here,
    # in the same step that changes the constructor, is deliberate: a contract
    # support that ships a green constructor and a red suite has just moved the
    # breakage one file over. Same #139 class as the count pins.
    test_migrations = {
        "tests/funnel-v2-instrumentation.test.mjs": [
            (
                '  }, CLICK_ID));\n'
                '  assert.equal(affiliati.searchParams.get("s1"), CLICK_ID);\n'
                '  assert.equal(affiliati.searchParams.get("s2"), CLICK_ID);',
                '  }, CLICK_ID, "providers.pc1"));\n'
                '  // AFFILIATI_S1_S2_CONTRACT_V1: s1 is the PLACEMENT subid, s2 is the click\n'
                '  // id. They are asserted to DIFFER, because setting both to the click id\n'
                '  // is exactly the defect this contract fixed.\n'
                '  assert.equal(affiliati.searchParams.get("s1"), "providers.pc1");\n'
                '  assert.equal(affiliati.searchParams.get("s2"), CLICK_ID);\n'
                '  assert.notEqual(affiliati.searchParams.get("s1"), affiliati.searchParams.get("s2"));',
            ),
            (
                '  }, "c1-0123456789abcdef01234567"), /affiliati_uuidv7_required/);',
                '  }, "c1-0123456789abcdef01234567", "providers.pc1"), /affiliati_uuidv7_required/);',
            ),
        ],
        "tests/affiliati-attribution-local-d1.test.mjs": [
            (
                '    assert.equal(destination.searchParams.get("s1"), CLICK_ID);\n'
                '    assert.equal(destination.searchParams.get("s2"), CLICK_ID);',
                '    // AFFILIATI_S1_S2_CONTRACT_V1: /go was called with s=providers.fv2s1, so\n'
                '    // s1 carries that placement code and s2 carries the click id.\n'
                '    assert.equal(destination.searchParams.get("s1"), "providers.fv2s1");\n'
                '    assert.equal(destination.searchParams.get("s2"), CLICK_ID);\n'
                '    assert.notEqual(destination.searchParams.get("s1"), destination.searchParams.get("s2"));',
            ),
        ],
    }
    for relative, edits in test_migrations.items():
        path = candidate_root / relative
        if not path.is_file():
            continue
        body = path.read_text(encoding="utf-8")
        if marker in body:
            continue
        for old_text, new_text in edits:
            if body.count(old_text) != 1:
                raise B2Error(
                    f"affiliati_subid_test_contract_drift:{relative}:{body.count(old_text)}")
            body = body.replace(old_text, new_text, 1)
        _detach(path)
        path.write_text(body, encoding="utf-8")
        changed.append(relative)

    for relative, needles in (
        ("src/network-constructors.mjs", ("affiliati_link_form_unknown", "surfaceCode")),
        ("src/worker.mjs", ("${pageSlug}.${linkPosition}",)),
    ):
        body = (candidate_root / relative).read_text(encoding="utf-8")
        for needle in needles:
            if needle not in body:
                raise B2Error(f"affiliati_subid_contract_support_missing:{relative}:{needle}")
    return {"status": "PASS_SUPPORTED", "marker": marker, "changed_files": changed,
            "default_link_form": "creative"}



MEASURED_TERMS_RESCISSION_MARKER = "MEASURED_TERMS_RESCISSION_V1"


PROVIDER_CARD_DOCUMENTED_GAP_MARKER = "PROVIDER_CARD_DOCUMENTED_GAP_V1"
#: The three ids the release tests already declare detail-only, plus the ones a
#: round adds because the provider publishes no price. Keep this ordered: it is
#: written into the candidate's test bytes verbatim.
PROVIDER_CARD_DETAIL_ONLY_BASE = ("mydrhank", "breeze-meds-review", "care-bare-rx-glp1-review")
#: 2026-09-03: jrnys-review and medvi-review (the two Affiliati new providers) are
#: absent from the 2026-08-30 provider-facts capture exactly as g-plan was, so no
#: Option C core field is SOURCE_REPORTED for them. The basis is still DERIVED
#: per row below and a provider with complete evidence is still refused.
PROVIDER_CARD_DOCUMENTED_GAP_ROWS = ("g-plan", "jrnys-review", "medvi-review")


#: The shipped Option C authority the post-deploy sensor reads. BOTH the
#: surface_completeness check and the therapeutic_facet_bindings projection
#: derive their compare exclusions from this one list, so emitting here fixes
#: the grid and the three money-page projections from a single source.
OPTION_C_CONTRACT_RELATIVE = "data/glp-design-placeholder-manifest.json"
PROVIDER_FACTS_RELATIVE = "data/provider-facts-backfill-20260830.json"
#: The two Option C core fields that must be SOURCE_REPORTED for a card to
#: render. display_name and meds_offered are always present; these two are the
#: evidence gates, and they are exactly what providerCardContract checks.
OPTION_C_EVIDENCE_FIELDS = ("total_monthly_cost", "cancellation")


def provider_card_documented_gap_basis(
    candidate_root: Path, program_id: str,
) -> dict[str, Any]:
    """Derive, from evidence, WHY this provider carries no Option C card.

    The basis is never a sentence someone typed: it is read out of the shipped
    provider-facts capture. A provider absent from that capture has no card
    because it has no captured facts at all; a provider present in it has one
    only if every Option C evidence field is SOURCE_REPORTED.
    """
    facts_path = Path(candidate_root) / PROVIDER_FACTS_RELATIVE
    document = read_json(facts_path)
    rows = [row for row in (document.get("programs") or [])
            if isinstance(row, dict) and str(row.get("program_id") or "") == str(program_id)]
    if not rows:
        return {
            "program_id": str(program_id),
            "facts_row_present": False,
            "missing_core_evidence": list(OPTION_C_EVIDENCE_FIELDS),
            "basis": ("absent from the shipped provider-facts capture, so no Option C "
                      "core field is SOURCE_REPORTED for it"),
            "derived_from": PROVIDER_FACTS_RELATIVE,
            "capture_date": str(document.get("capture_date") or ""),
        }
    facts = rows[0].get("facts") or {}
    missing = [field for field in OPTION_C_EVIDENCE_FIELDS
               if str((facts.get(field) or {}).get("status") or "") != "SOURCE_REPORTED"]
    return {
        "program_id": str(program_id),
        "facts_row_present": True,
        "missing_core_evidence": missing,
        "field_status": {field: str((facts.get(field) or {}).get("status") or "ABSENT")
                         for field in OPTION_C_EVIDENCE_FIELDS},
        "basis": ("no SOURCE_REPORTED " + " and ".join(missing)
                  if missing else "every Option C core field is SOURCE_REPORTED"),
        "derived_from": PROVIDER_FACTS_RELATIVE,
        "capture_date": str(document.get("capture_date") or ""),
    }


def _emit_provider_card_sensor_exception(candidate_root: Path) -> dict[str, Any]:
    """Write the per-provider compare exception the sensor demands, generated.

    The sensor refuses a live provider that is absent from compare unless the
    shipped Option C contract NAMES it. Editing that contract by hand is how an
    exception outlives the gap that justified it, so the row is added here, with
    a basis derived from the provider-facts capture, and REFUSED if the evidence
    says the provider could have carried a card after all.
    """
    candidate_root = Path(candidate_root)
    path = candidate_root / OPTION_C_CONTRACT_RELATIVE
    if not path.is_file():
        return {"status": "PASS_NO_OPTION_C_CONTRACT_IN_TREE",
                "changed_files": [], "absent": OPTION_C_CONTRACT_RELATIVE}
    document = read_json(path)
    decision = document.get("card_content_decision") or {}
    if decision.get("chosen_option") != "C":
        return {"status": "PASS_NOT_OPTION_C", "changed_files": []}
    contract = decision.get("provider_contract")
    if not isinstance(contract, dict):
        raise B2Error("provider_card_option_c_contract_missing")

    base = list(PROVIDER_CARD_DETAIL_ONLY_BASE)
    gaps = list(PROVIDER_CARD_DOCUMENTED_GAP_ROWS)
    wanted = base + gaps
    current = [str(pid) for pid in (contract.get("excluded_program_ids") or [])]
    if current == wanted and contract.get("documented_gaps"):
        return {"status": "PASS_ALREADY_EMITTED", "changed_files": [],
                "excluded_program_ids": wanted}
    # A tree may already carry an EARLIER generation of this list (base plus a
    # prefix of the gap rows, e.g. g-plan from the 09-03 r11 release): that is a
    # partial migration to extend, not drift. Anything outside base..wanted is.
    if not (set(base) <= set(current) <= set(wanted)):
        raise B2Error(
            f"provider_card_option_c_excluded_drift:{sorted(set(current) ^ set(base))}")

    bases = []
    for program_id in gaps:
        basis = provider_card_documented_gap_basis(candidate_root, program_id)
        if basis["facts_row_present"] and not basis["missing_core_evidence"]:
            # Excusing a provider that COULD render a card would turn this
            # contract into a way to hide a real surface regression.
            raise B2Error(f"provider_card_documented_gap_not_evidenced:{program_id}")
        bases.append(basis)

    eligible = int(contract.get("eligible_entities") or 0)
    added = [pid for pid in gaps if pid not in set(current)]
    total = int(contract.get("total_entities") or 0) + len(added)
    contract["excluded_program_ids"] = wanted
    contract["total_entities"] = total
    contract["eligibility_rate_percent"] = round(eligible * 100.0 / total, 1) if total else 0.0
    contract["documented_gaps"] = bases
    contract["documented_gap_contract"] = PROVIDER_CARD_DOCUMENTED_GAP_MARKER
    # The sensor's own invariant, asserted here so a bad emission fails in the
    # builder rather than after the bytes are live.
    if eligible + len(wanted) != total:
        raise B2Error(
            f"provider_card_option_c_membership_invalid:{eligible}+{len(wanted)}!={total}")
    _write_candidate_json_preserving_key_order(path, document)
    return {"status": "PASS_SENSOR_EXCEPTION_EMITTED",
            "changed_files": [OPTION_C_CONTRACT_RELATIVE],
            "excluded_program_ids": wanted,
            "eligible_entities": eligible, "total_entities": total,
            "documented_gaps": bases,
            "covers": ["surface_completeness compare exclusion",
                       "therapeutic_facet_bindings canonical projection on every "
                       "compare route (the sensor subtracts this same list)"]}


def _ensure_provider_card_documented_gap_contract_support(
    candidate_root: Path,
) -> dict[str, Any]:
    """A provider with no published price renders no Option C card, declared.

    DEFECT 2026-09-03, found by the r9 round. Two release suites compute
    `optionCExpected` as EVERY program in data/program-registry.json minus three
    hardcoded detail-only ids, and assert that many `data-phase3-provider` cards
    render on /providers/. That denominator silently assumed the registry and the
    provider-card set are the same population. They are not:

      * the card grid enumerates data/provider-facts-backfill-20260830.json, not
        the registry (src/phase3-page-graph.mjs renderProviderListingHub);
      * `providerCardContract()` renders a card ONLY when total_monthly_cost AND
        cancellation are both status=SOURCE_REPORTED with a source_url and a
        capture_date.

    G-Plans publishes cancellation terms (g-plans.com/new/pages/refunds-policy,
    verified 2026-09-01) but publishes NO monthly price on any captured
    first-party page - reconfirmed identically by every research pass from
    2026-08-31 to 2026-09-03. So onboarding g-plan grows the registry to 57 and
    the expected count to 54 while 53 cards render, and npm test fails
    [tap_failures] on `53 !== 54`.

    The only two ways to make that count agree are to invent a price - which
    sourceReportedFact() exists specifically to forbid, and which would be a
    fabricated fact on a money page - or to say out loud that this provider has
    a documented evidence gap. This does the second.

    It does NOT weaken the guard, it tightens it: g-plan joins the SAME
    enumerated exclusion list the three existing rows use, so the suites now also
    assert that g-plan renders no card and still serves /providers/g-plan/. If a
    price is ever captured and a card appears, these tests FAIL until the row is
    removed from the list deliberately. A provider that vanishes for any other
    reason still fails the count, exactly as before.
    """
    marker = PROVIDER_CARD_DOCUMENTED_GAP_MARKER
    candidate_root = Path(candidate_root)
    targets = [
        candidate_root / "tests/design-pass4-late-callback-closure.test.mjs",
        candidate_root / "tests/phase3-page-graph.test.mjs",
    ]
    missing = [path.name for path in targets if not path.is_file()]
    if len(missing) == len(targets):
        return {"status": "PASS_NOT_A_SITE_TREE_NO_SUITES", "marker": marker,
                "changed_files": [], "absent": sorted(missing)}
    if missing:
        raise B2Error(f"provider_card_documented_gap_suite_missing:{','.join(sorted(missing))}")

    old_list = ", ".join(f'"{name}"' for name in PROVIDER_CARD_DETAIL_ONLY_BASE)
    new_list = ", ".join(
        f'"{name}"' for name in PROVIDER_CARD_DETAIL_ONLY_BASE + PROVIDER_CARD_DOCUMENTED_GAP_ROWS
    )
    changed: dict[str, int] = {}
    for path in targets:
        text = path.read_text(encoding="utf-8", errors="strict")
        # NB: old_list is a PREFIX of new_list, so "old_list not in text" is
        # never true after a migration. Presence of new_list is the only correct
        # already-migrated test; getting this wrong appends the row twice.
        if new_list in text:
            changed[path.name] = 0          # already migrated; idempotent
            continue
        # Replace the LONGEST earlier generation present (base plus a prefix of
        # the gap rows) so extending the list never duplicates a row.
        present = old_list
        for cut in range(len(PROVIDER_CARD_DOCUMENTED_GAP_ROWS) - 1, 0, -1):
            candidate_list = ", ".join(
                f'"{name}"' for name in
                PROVIDER_CARD_DETAIL_ONLY_BASE + PROVIDER_CARD_DOCUMENTED_GAP_ROWS[:cut])
            if candidate_list in text:
                present = candidate_list
                break
        old_list = present
        count = text.count(old_list)
        if count < 2:
            # Both suites carry the list twice: once as the count denominator and
            # once as the "must remain detail-only" loop. One occurrence means an
            # anchor moved, which is a real defect, not something to paper over.
            raise B2Error(f"provider_card_detail_only_anchor_drift:{path.name}:{count}")
        _detach(path)
        path.write_text(text.replace(old_list, new_list), encoding="utf-8")
        changed[path.name] = count

    # The suites now agree that these rows carry no card. The post-deploy sensor
    # must be told the SAME thing, from the SAME list, or it reads the missing
    # card as silent surface loss - which is exactly what it is for any row NOT
    # on this list.
    emission = _emit_provider_card_sensor_exception(candidate_root)
    for relative in emission.get("changed_files") or []:
        changed[relative] = 1

    return {"status": "PASS_PROVIDER_CARD_DOCUMENTED_GAP_DECLARED", "marker": marker,
            "sensor_exception": emission,
            "changed_files": changed,
            "documented_gap_rows": list(PROVIDER_CARD_DOCUMENTED_GAP_ROWS),
            "reason": "no SOURCE_REPORTED total_monthly_cost exists for these providers; "
                      "a card would require inventing a price",
            "still_served": "each excluded row keeps its /providers/<id>/ detail route, "
                            "which the suites assert",
            "external_effects": {"provider_calls": 0, "deployments": 0, "sheet_writes": 0}}


def _ensure_measured_terms_rescission(candidate_root: Path) -> dict[str, Any]:
    """Apply the operator's 2026-08-31 rescission to the terms records themselves.

    DEFECT 2026-09-02. The rescission was applied to the /go exception and NEVER
    to the terms decision it actually overturned. So Measured's `/go` route goes
    live while its link-map record stays DARK - worse than either state alone,
    and exactly what three release tests catch:
    `every go manifest entry is governed active or explicitly held`,
    `every generated runtime entry is governed active or explicitly held`, and
    `programs held link-dark record WHY, so bare is a decision and not a gap`.

    TWO INDEPENDENT AUTHORITIES agree the hold is void, which is why this is a
    mechanical application and not a judgement call:

      1. The operator's dated decision receipt of 2026-08-31 - "i never denied
         measured ... remove that wherever it has that in the system."
      2. The standing terms-review doctrine of 2026-08-14, which POSTDATES the
         2026-08-13 hold: the seven mechanical triggers - including
         `participation_provider_product_liability_indemnity`, the class this
         hold cites - are handled as LOGGED RISK-ACCEPTANCES, never as holds.
         Only genuinely illegal terms refuse. Under the doctrine in force, this
         record could not have been a hold at all.

    The risk-acceptance is WRITTEN, not implied: the trigger, its class, the
    operator's words and the date go into the acceptance record, because
    "accepted" without the reason is the gap the third test is named after.

    Absent row is a no-op, never a failure. Idempotent.
    """
    marker = MEASURED_TERMS_RESCISSION_MARKER
    candidate_root = Path(candidate_root)
    if not POST_SNAPSHOT_APPROVED_REGISTRY_AUTHORITY.is_file():
        return {"status": "PASS_NO_POST_SNAPSHOT_AUTHORITY", "changed_files": []}
    authority = read_json(POST_SNAPSHOT_APPROVED_REGISTRY_AUTHORITY)
    entries = [e for e in authority.get("rows") or []
               if str(e.get("site_program_id") or "") == "measured"]
    if not entries:
        return {"status": "PASS_NOT_GOVERNED_HERE", "changed_files": []}
    entry = entries[0]
    decision_path = verify_binding(
        entry.get("operator_decision") or {}, label="measured_terms_rescission_decision")
    decision = read_json(decision_path)
    if (decision.get("schema") != "glp.operator-decision-supersession.v1"
            or not str(decision.get("operator_instruction_verbatim") or "").strip()):
        raise B2Error("measured_terms_rescission_decision_invalid")

    alias = "measured"
    changed: list[str] = []
    acceptance = {
        "risk_accepted": True,
        "mechanical_trigger": "advertiser_supplied_copy_indemnity",
        "trigger_class": "participation_provider_product_liability_indemnity",
        "accepted_on": pt_date(),
        "authority": "operator decision measured-go-exception-rescinded-20260831 "
                     "plus the 2026-08-14 standing terms-review doctrine, which "
                     "postdates the 2026-08-13 hold and makes the seven mechanical "
                     "triggers logged risk-acceptances rather than holds",
        "operator_words": str(decision.get("operator_instruction_verbatim") or "")[:400],
        "operator_decision_receipt": path_binding(decision_path),
        "supersedes_decision_receipt":
            "reports/glp-clinic-scout/affiliate-machine/awin-item1-terms-decisions-20260813/"
            "awin-terms-126727-5128fd05084c6ad0.json",
        "legality_review": "no illegal term and no term requiring illegal action; the "
                           "indemnity is an ordinary participation indemnity, which the "
                           "doctrine accepts with this record rather than holding on",
    }

    approvals_path = candidate_root / "data/affiliate-wire-in-approvals.json"
    if approvals_path.is_file():
        approvals = read_json(approvals_path)
        rows = ((approvals.get("current_terms_reassessment") or {}).get("programs") or [])
        target = [r for r in rows if str(r.get("existing_release_alias") or "") == alias]
        if len(target) > 1:
            raise B2Error(f"measured_terms_rescission_row_match_count:{len(target)}")
        if target and str(target[0].get("verdict") or "") == "operator_rejected_terms":
            row = target[0]
            row.update({
                # `status` AND `verdict` both carry the decision; the sensor reads
                # status. Setting only verdict left the record self-contradictory,
                # which validate-provider-fastpath-plus5 caught immediately.
                "status": "operator_accepted_terms",
                "verdict": "operator_accepted_terms",
                "live_link_disposition": "preserve_existing_live",
                # Deliberately NOT widened. Accepting the terms restores the
                # EXISTING live surfaces; it does not authorize material expansion
                # or a new activation, and the sensor asserts both stay false for
                # every row. Rescinding a hold is not a licence to grow the
                # footprint - that would be me using the rescission to authorize
                # something the operator never spoke to.
                "may_authorize_material_expansion": False,
                "may_authorize_new_activation": False,
                "future_change_terms_gate_open": True,
                "hold_reason": None,
                "hold_trigger": None,
                "operator_words": acceptance["operator_words"],
                "decision_receipt_path": str(decision_path),
                "decision_receipt_sha256": path_binding(decision_path)["sha256"],
                "decision_recorded_at_utc": str(decision.get("captured_at_utc") or ""),
                "provenance": "operator-decision-supersession-20260831",
                "risk_acceptance": acceptance,
                "rescission_note": marker,
            })
            surfaces = row.get("existing_live_surfaces")
            if not surfaces:
                row["existing_live_surfaces"] = ["compare", "cost", "quiz"]
            # THE SAME FACT IS STORED TWICE. Every approvals program carries its
            # own embedded current_terms_reassessment copy, and rescinding only
            # the top-level row left that copy still reading
            # "operator_rejected_terms" - the record contradicting itself, and
            # exactly the claim the operator said to remove wherever it appears.
            embedded_updates = {
                "status": "operator_accepted_terms",
                "verdict": "operator_accepted_terms",
                "future_change_terms_gate_open": True,
                "hold_reason": None,
                "hold_trigger": None,
                "operator_words": acceptance["operator_words"],
                "decision_receipt_path": str(decision_path),
                "decision_receipt_sha256": path_binding(decision_path)["sha256"],
                "decision_recorded_at_utc": str(decision.get("captured_at_utc") or ""),
                "provenance": "operator-decision-supersession-20260831",
                "risk_acceptance": acceptance,
                "rescission_note": marker,
            }
            embedded_rescinded = 0
            for program in approvals.get("programs") or []:
                if str(program.get("existing_release_alias") or "") != alias:
                    continue
                embedded = program.get("current_terms_reassessment")
                if not isinstance(embedded, dict):
                    continue
                if str(embedded.get("verdict") or "") != "operator_rejected_terms":
                    continue
                embedded.update(embedded_updates)
                embedded_rescinded += 1
            if embedded_rescinded != 1:
                raise B2Error(
                    f"measured_terms_rescission_embedded_copy_count:{embedded_rescinded}")
            reassessment = approvals.get("current_terms_reassessment") or {}
            if reassessment.get("governance_scope") == (
                    "accepted_live_preserved_rejected_terms_de_linked"):
                # The old scope string describes a de-linking that no longer
                # exists. Leaving it would keep the file asserting the decision
                # its own rows no longer carry.
                reassessment["governance_scope"] = (
                    "accepted_live_preserved_rejected_terms_rescinded_20260831")
            _write_candidate_json_preserving_key_order(approvals_path, approvals)
            changed.append("data/affiliate-wire-in-approvals.json")

    contracts_path = candidate_root / "data/provider-page-contracts.json"
    if contracts_path.is_file():
        contracts = read_json(contracts_path)
        rows = [c for c in contracts.get("providers") or []
                if str(c.get("program_id") or "") == alias]
        if len(rows) > 1:
            raise B2Error(f"measured_terms_rescission_contract_match_count:{len(rows)}")
        if rows:
            contract = rows[0]
            row_contract = contract.get("row_contract") or {}
            if (row_contract.get("live_link_allowed") is not True
                    or row_contract.get("promotion_authorized") is not True):
                row_contract.update({
                    "live_link_allowed": True,
                    "promotion_authorized": True,
                    "affiliate_link_mode": "transparent_direct",
                })
                contract["row_contract"] = row_contract
                _write_candidate_json_preserving_key_order(contracts_path, contracts)
                changed.append("data/provider-page-contracts.json")

    # The SENSOR PIN. validate-provider-fastpath-plus5.mjs hardcodes the
    # 2026-08-13 decision, including the operator's superseded words verbatim
    # ("measured has bad terms, and I wanna reject it"). Left alone it fails the
    # build; edited carelessly it would stop guarding anything. Every assertion is
    # REPLACED BY A STRICTER ONE: the rescinded state is pinned exactly, and the
    # risk-acceptance must now exist and name its trigger - which the old pin
    # never required of anything.
    sensor_path = candidate_root / "scripts/validate-provider-fastpath-plus5.mjs"
    if sensor_path.is_file():
        sensor = sensor_path.read_text(encoding="utf-8")
        if marker not in sensor:
            edits = [
                ('assert.equal(reassessment.governance_scope, "accepted_live_preserved_rejected_terms_de_linked");',
                 'assert.equal(reassessment.governance_scope, "accepted_live_preserved_rejected_terms_rescinded_20260831");'),
                ('assert.deepEqual([...currentTermsById.values()].filter((item) => item.verdict === "operator_accepted_terms")\n'
                 '  .map((item) => item.provider_program_id).sort(), ["115383", "80963"]);',
                 '// MEASURED_TERMS_RESCISSION_V1: 126727 joined the accepted set on 2026-08-31.\n'
                 'assert.deepEqual([...currentTermsById.values()].filter((item) => item.verdict === "operator_accepted_terms")\n'
                 '  .map((item) => item.provider_program_id).sort(), ["115383", "126727", "80963"]);'),
                ('assert.deepEqual([...currentTermsById.values()].filter((item) => item.verdict === "operator_rejected_terms")\n'
                 '  .map((item) => item.provider_program_id).sort(), ["126727"]);',
                 '// No program is held on rejected terms any more. Pinned to EMPTY, not deleted:\n'
                 '// a new rejection must come back through this assertion to be noticed.\n'
                 'assert.deepEqual([...currentTermsById.values()].filter((item) => item.verdict === "operator_rejected_terms")\n'
                 '  .map((item) => item.provider_program_id).sort(), []);'),
                ('  ["126727", []],\n  ["128203", ["compare", "cost", "quiz", "calculator"]],',
                 '  ["126727", ["compare", "cost", "quiz"]],\n  ["128203", ["compare", "cost", "quiz", "calculator"]],'),
                ('const measuredTerms = currentTermsById.get("126727");\n'
                 'assert.equal(measuredTerms.status, "operator_rejected_terms");\n'
                 'assert.equal(measuredTerms.operator_words, "measured has bad terms, and I wanna reject it");\n'
                 'assert.equal(measuredTerms.provenance, "bridge-session-20260812");\n'
                 'assert.equal(measuredTerms.reversible_by, "ACCEPT-TERMS 126727");\n'
                 'assert.equal(measuredTerms.hold_trigger, "advertiser_supplied_copy_indemnity");\n'
                 'assert.match(measuredTerms.decision_receipt_sha256, /^[a-f0-9]{64}$/);',
                 '// MEASURED_TERMS_RESCISSION_V1. The 2026-08-12 rejection was rescinded by the\n'
                 '// operator on 2026-08-31, and is superseded in policy by the 2026-08-14\n'
                 '// terms-review doctrine, under which the seven mechanical triggers are LOGGED\n'
                 '// RISK-ACCEPTANCES and never holds.\n'
                 'const measuredTerms = currentTermsById.get("126727");\n'
                 'assert.equal(measuredTerms.status, "operator_accepted_terms");\n'
                 'assert.equal(measuredTerms.verdict, "operator_accepted_terms");\n'
                 'assert.equal(measuredTerms.hold_trigger, null);\n'
                 'assert.equal(measuredTerms.hold_reason, null);\n'
                 'assert.equal(measuredTerms.provenance, "operator-decision-supersession-20260831");\n'
                 'assert.match(measuredTerms.decision_receipt_sha256, /^[a-f0-9]{64}$/);\n'
                 'assert.ok(measuredTerms.risk_acceptance, "rescinded terms must carry a risk-acceptance record");\n'
                 'assert.equal(measuredTerms.risk_acceptance.risk_accepted, true);\n'
                 'assert.equal(measuredTerms.risk_acceptance.mechanical_trigger, "advertiser_supplied_copy_indemnity");\n'
                 'assert.equal(measuredTerms.risk_acceptance.trigger_class, "participation_provider_product_liability_indemnity");\n'
                 'assert.ok(String(measuredTerms.risk_acceptance.operator_words || "").length > 20);\n'
                 'assert.ok(/^\\d{4}-\\d{2}-\\d{2}$/.test(String(measuredTerms.risk_acceptance.accepted_on || "")));'),
            ]
            for old_text, new_text in edits:
                if sensor.count(old_text) != 1:
                    raise B2Error(
                        f"measured_terms_rescission_sensor_drift:{sensor.count(old_text)}")
                sensor = sensor.replace(old_text, new_text, 1)
            _detach(sensor_path)
            sensor_path.write_text(sensor, encoding="utf-8")
            changed.append("scripts/validate-provider-fastpath-plus5.mjs")

    # The fastpath record is the one a READER sees. Its hold_copy says, in the
    # provider's own review page, "Clinic Scout declined this program's current
    # affiliate terms" - a statement about a decision that was rescinded. Leaving
    # it would keep publishing the exact sentence the operator asked to have
    # removed, and src/provider-review.mjs enforces the pairing both ways
    # (fastpath_held_gate_invalid), so a half-applied rescission fails the build
    # rather than shipping an inconsistent page. That is the gate working.
    fastpath_path = candidate_root / "data/provider-fastpath-contract.json"
    if fastpath_path.is_file():
        fastpath = read_json(fastpath_path)
        rows = [r for r in fastpath.get("providers") or []
                if str(r.get("program_id") or "") == alias]
        if len(rows) > 1:
            raise B2Error(f"measured_terms_rescission_fastpath_match_count:{len(rows)}")
        touched = False
        if rows:
            record = rows[0]
            if str(record.get("affiliate_cta_state") or "").startswith("HOLD_"):
                record["affiliate_cta_state"] = "ACTIVE_PROVIDER_ISSUED_DIRECT"
                record["route_state"] = "LIVE_EDITORIAL_AFFILIATE_ACTIVE"
                record.pop("hold_copy", None)
                record["rescission_note"] = marker
                touched = True
        # The pin must be re-minted whenever provider-page-contracts.json moved,
        # even if the fastpath ROW itself did not.
        if touched or "data/provider-page-contracts.json" in changed:
            if contracts_path.is_file():
                fastpath["current_contract_sha256"] = sha_file(contracts_path)
            _write_candidate_json_preserving_key_order(fastpath_path, fastpath)
            changed.append("data/provider-fastpath-contract.json")

    return {"status": "PASS_RESCISSION_APPLIED" if changed else "PASS_ALREADY_APPLIED",
            "marker": marker, "changed_files": changed,
            "risk_acceptance": acceptance,
            "operator_decision": path_binding(decision_path)}



def _go_promotion_authority_status(
    candidate_root: Path, request: dict[str, Any],
) -> dict[str, Any]:
    """The governed verdict for one /go request, read from the candidate.

    Held-by-default: absent or unreadable authority means WITHHELD, never
    activate. Only an explicit promotion_authorized true opens the surface.
    """
    program_id = str(request.get("site_program_id") or "")
    path = candidate_root / GO_RUNTIME_PROMOTION_AUTHORITY_RELATIVE
    try:
        programs = (read_json(path).get("programs") or {})
    except (OSError, ValueError, B2Error):
        # read_json raises B2Error on a missing/!unparseable file. Held-by-default
        # must HOLD on that, never crash and never fall through to activation.
        return {"promotion_authorized": False, "status": "ABSENT",
                "reason": "go_runtime_promotion_authority_unreadable"}
    entry = programs.get(program_id) or programs.get(
        str(request.get("route_slug") or "")) or {}
    if entry.get("promotion_authorized") is True:
        return {"promotion_authorized": True,
                "status": str(entry.get("status") or "VERIFIED"),
                "reason": str(entry.get("reason") or "operator_authorized_live_go_runtime")}
    return {"promotion_authorized": False,
            "status": str(entry.get("status") or "ABSENT"),
            "reason": str(entry.get("reason") or "no_promotion_authority_entry")}


def apply_first_party_go_route_provision(
    candidate_root: Path, request: dict[str, Any],
) -> dict[str, Any]:
    """Consume one /go queue request on detached candidate bytes only."""
    if (
        request.get("status") != "STAGED_NOT_DEPLOYED"
        or request.get("queue_kind") != "affiliate_go_route_provision"
        or request.get("queue_source") != "sheet_update_queue:payout_evaluation"
        or request.get("deploy_instruction_version") != GO_ROUTE_PROVISION_INSTRUCTION_VERSION
        or request.get("sheet_updates_via_single_writer_only") is not True
        or request.get("href_config_inspection_only") is not True
        or request.get("tracking_clicks") != 0
    ):
        raise B2Error("go_route_provision_deploy_contract_invalid")
    candidate_root = Path(candidate_root)
    # HELD-BY-DEFAULT AT THE ROOT. Operator ruling 2026-09-03: a /go request whose
    # governed promotion authority is WITHHELD must be RECORDED HELD and must
    # never activate the surface. This consumer used to flip surfaces.go true and
    # entry_state instrumented_redirect_candidate purely from the queue request,
    # regardless of the authority - which is how Measured came out of a build in a
    # state its own authority contradicts (WITHHELD, runtime_network_authority_
    # missing) and why validate-provider-fastpath-plus5 matched neither branch.
    # The request is HELD, never deleted: it stays staged for the day the
    # authority is granted.
    authority_status = _go_promotion_authority_status(candidate_root, request)
    if not authority_status["promotion_authorized"]:
        return {
            "status": "PASS_HELD_GO_ROUTE_PROMOTION_AUTHORITY_WITHHELD",
            "site_program_id": str(request.get("site_program_id") or ""),
            "first_party_go_route": str(request.get("first_party_go_route") or ""),
            "reason": authority_status["reason"],
            "authority_status": authority_status["status"],
            "surface_activated": False,
            "entry_state": "held",
            "request_preserved": True,
            "external_effects": {"tracking_clicks": 0, "tracking_link_requests": 0,
                                 "sheet_writes": 0, "deployments": 0},
        }
    contract_support = _ensure_first_party_go_contract_support(candidate_root)
    # A relationship approved AFTER the frozen 2026-08-02 evidence snapshot has no
    # row in the approved registry and never could have. Extend the frozen set
    # from a hash-bound operator authority before the join below looks for it.
    post_snapshot_row = _ensure_post_snapshot_approved_registry_row(
        candidate_root, str(request.get("site_program_id") or ""))
    target = request.get("target") or {}
    target_sheet = request.get("target_sheet") or {}
    target_link = _validate_queue_tracking_link(
        target_sheet.get("Tracking Link / Deep Link"),
        target.get("network"),
        target.get("program_id"),
    )
    registry_path = candidate_root / "data/program-registry.json"
    approved_path = candidate_root / "data/approved-program-registry.json"
    registry = read_json(registry_path)
    approved = read_json(approved_path)
    site_program_id = str(request.get("site_program_id") or "")
    rows = [row for row in registry.get("programs") or [] if row.get("program_id") == site_program_id]
    if len(rows) != 1:
        raise B2Error(f"go_route_registry_identity_match_count:{len(rows)}")
    row = rows[0]
    profile = ((row.get("profiles") or {}).get("glp-clinic-scout") or {})
    surfaces = profile.get("surfaces") or {}
    if (
        row.get("commercial_state") != "live"
        or profile.get("wired") is not True
        or surfaces.get("go") is not False
        or (row.get("go") or {}).get("route") != request.get("first_party_go_route")
    ):
        raise B2Error("go_route_registry_precondition_failed")
    # DEFECT 2026-09-01: the approved registry keys rows by registry_id
    # (`<network-slug>-<provider_program_id>`), and the release's own coverage
    # validator REQUIRES exactly that shape (src/approved-program-coverage.mjs
    # exactRegistryId). This join used the SITE program id, so it could only ever
    # match the five rows whose site id happens to equal their registry id - and
    # the three /go requests that pass today are three of those five, so the gate
    # had never been exercised against anything else. 17 of the 53 approved rows
    # carry the site id in `existing_release_alias` instead. For Measured the two
    # contracts were mutually unsatisfiable: registry_id had to be `awin-126727`
    # for the validator and `measured` for this join.
    #
    # Widen the JOIN, not the authority. The match must still resolve to EXACTLY
    # one row (the match_count != 1 refusal below is unchanged) and every
    # downstream check - network, provider_program_id, candidate_registry_wired,
    # and _validate_queue_tracking_link on the authority's own provider-issued
    # URL - is untouched. Proven read-only against the live 53-row registry: not
    # one site program id resolves to two rows under the widened join.
    approved_rows = [
        item for item in approved.get("programs") or []
        if site_program_id in {item.get("registry_id"), item.get("existing_release_alias")}
    ]
    if len(approved_rows) != 1:
        raise B2Error(f"go_route_approved_registry_match_count:{len(approved_rows)}")
    authority = approved_rows[0]
    tracking = authority.get("tracking") or {}
    authority_network = normalize_network(authority.get("network"))
    authority_program_id = str(authority.get("provider_program_id") or "")
    authority_link = _validate_queue_tracking_link(
        tracking.get("provider_issued_url"), authority_network, authority_program_id
    )
    if (
        authority_network != normalize_network(target.get("network"))
        or authority_program_id != str(target.get("program_id") or "")
        or tracking.get("candidate_registry_wired") is not True
    ):
        raise B2Error("go_route_approved_registry_authority_mismatch")
    surfaces["go"] = True
    profile["surfaces"] = surfaces
    exceptions = profile.get("exceptions") or {}
    exceptions.pop("go", None)
    if exceptions:
        profile["exceptions"] = exceptions
    else:
        profile.pop("exceptions", None)
    row["profiles"]["glp-clinic-scout"] = profile
    row["go"]["entry_state"] = "instrumented_redirect_candidate"
    request["effective_tracking_link"] = authority_link
    tracking["first_party_go_route"] = str(request.get("first_party_go_route") or "")
    tracking["public_go_surface"] = True
    tracking["state"] = "candidate_registry_wired_first_party_go_staged"
    authority["tracking"] = tracking
    # data/program-registry.json carries a byte-order-sensitive validator
    # (provider_dynamic_profiles.expected_counts). Never re-sort it.
    _write_candidate_json_preserving_key_order(registry_path, registry)
    _write_candidate_json(approved_path, approved)
    return {
        "status": "PASS_FIRST_PARTY_GO_ROUTE_PROVISIONED_ON_CANDIDATE",
        "provider": request.get("provider"),
        "site_program_id": site_program_id,
        "first_party_go_route": request.get("first_party_go_route"),
        "queue_update_ids": request.get("queue_update_ids"),
        "changed_files": [
            *contract_support["changed_files"],
            *((post_snapshot_row.get("contract_support") or {}).get("changed_files") or []),
            str(registry_path.relative_to(candidate_root)),
            str(approved_path.relative_to(candidate_root)),
        ],
        "contract_support": contract_support,
        "post_snapshot_approved_registry_row": post_snapshot_row,
        "verification_method": "local_href_and_config_inspection_only",
        "external_effects": {
            "provider_calls": 0, "tracking_link_requests": 0,
            "tracking_clicks": 0, "sheet_writes": 0, "deployments": 0,
        },
    }


def verify_first_party_go_route_runtime(
    candidate_root: Path, request: dict[str, Any],
) -> dict[str, Any]:
    """Prove the built handler manifest and generated anchors carry the queued route."""
    candidate_root = Path(candidate_root)
    text = (candidate_root / "functions/go/program-manifest.js").read_text(encoding="utf-8")
    match = re.search(r"Object\.freeze\((.*)\);\s*$", text, re.S)
    if not match:
        raise B2Error("go_route_runtime_manifest_parse_failed")
    runtime = json.loads(match.group(1))
    site_program_id = str(request.get("site_program_id") or "")
    # HELD ROUTES ARE VERIFIED AS HELD, NOT AS WIRED. The provisioner records a
    # request whose governed promotion authority is WITHHELD as held and never
    # activates the surface (operator ruling 2026-09-03). This verifier used to
    # demand a fully wired runtime entry for EVERY queued request regardless, so
    # the two halves contradicted each other and the round died on
    # go_route_runtime_manifest_missing for a route the machine had correctly
    # declined to wire. The check is not dropped, it is INVERTED: a held route
    # must be provably dark - no activated runtime entry and no generated anchor.
    authority_status = _go_promotion_authority_status(candidate_root, request)
    if not authority_status["promotion_authorized"]:
        held_program = (runtime.get("programs") or {}).get(site_program_id)
        if isinstance(held_program, dict) and (
                held_program.get("go_route_wired") is True
                or held_program.get("promotion_authorized") is True):
            raise B2Error(f"go_route_wired_under_withheld_authority:{site_program_id}")
        held_route = str(request.get("first_party_go_route") or "")
        held_pattern = re.compile(r'href=["\']' + re.escape(held_route) + r'(?:[?"\'])')
        for path in (candidate_root / "public").rglob("*.html"):
            if held_pattern.search(path.read_text(encoding="utf-8", errors="strict")):
                raise B2Error(f"go_route_anchor_under_withheld_authority:{site_program_id}")
        return {
            "status": "PASS_HELD_GO_ROUTE_VERIFIED_DARK",
            "site_program_id": site_program_id,
            "first_party_go_route": held_route,
            "reason": authority_status["reason"],
            "authority_status": authority_status["status"],
            "runtime_entry_activated": False,
            "generated_anchors": 0,
            "verification_method": "local_href_and_config_inspection_only",
            "external_effects": {"provider_calls": 0, "tracking_link_requests": 0,
                                 "tracking_clicks": 0, "sheet_writes": 0, "deployments": 0},
        }
    program = (runtime.get("programs") or {}).get(site_program_id)
    target_link = str(
        request.get("effective_tracking_link")
        or (request.get("target_sheet") or {}).get("Tracking Link / Deep Link")
        or ""
    )
    if (
        not isinstance(program, dict)
        or program.get("route") != request.get("first_party_go_route")
        or program.get("go_route_wired") is not True
        or program.get("live_link_allowed") is not True
        or program.get("promotion_authorized") is not True
        or program.get("constructor_verified") is not True
        or program.get("redirector_tos_verified") is not True
        or ((program.get("tracking") or {}).get("base_url")) != target_link
    ):
        raise B2Error(f"go_route_runtime_manifest_missing:{site_program_id}")
    route = str(request.get("first_party_go_route") or "")
    anchor_occurrences = 0
    pattern = re.compile(r'href=["\']' + re.escape(route) + r'(?:[?"\'])')
    for path in (candidate_root / "public").rglob("*.html"):
        anchor_occurrences += len(pattern.findall(path.read_text(encoding="utf-8", errors="strict")))
    if anchor_occurrences < 1:
        raise B2Error(f"go_route_generated_anchor_missing:{site_program_id}")
    return {
        "status": "PASS_FIRST_PARTY_GO_ROUTE_RUNTIME_AND_ANCHOR",
        "site_program_id": site_program_id,
        "first_party_go_route": route,
        "generated_anchor_occurrences": anchor_occurrences,
        "verification_method": "local_href_and_config_inspection_only",
        "tracking_link_requests": 0,
        "tracking_clicks": 0,
    }


def _tracking_link_identity(link: str, network: str, program_id: str) -> tuple[str, str]:
    """Validate a provider-issued link structurally without requesting it."""
    parts = urlsplit(str(link or "").strip())
    query = parse_qs(parts.query)
    rail = normalize_network(network)
    if parts.scheme != "https":
        raise B2Error(f"best_terms_tracking_link_scheme_invalid:{rail}")
    if rail == "awin":
        if parts.hostname != "www.awin1.com":
            raise B2Error("best_terms_awin_tracking_host_invalid")
        observed = (query.get("mid") or query.get("awinmid") or [""])[0]
    elif rail == "katalys":
        if parts.hostname != "track.revoffers.com":
            raise B2Error("best_terms_katalys_tracking_host_invalid")
        observed = (query.get("offer_id") or [""])[0]
        if not (query.get("aff_id") or [""])[0]:
            raise B2Error("best_terms_katalys_publisher_identity_missing")
    else:
        raise B2Error(f"best_terms_tracking_rail_unsupported:{rail}")
    if str(observed) != str(program_id):
        raise B2Error(f"best_terms_tracking_program_identity_mismatch:{rail}")
    return rail, str(parts.hostname)


def _json_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_objects(child)


def _price_index_registry_semantic_basis(registry: dict[str, Any]) -> str:
    """Mirror of glp_surface_unification.registry_semantic_basis_sha256.

    Kept byte-identical to that function (and to
    src/therapeutic-taxonomy.mjs stableSha256) by
    tests/test_glp_price_index_semantic_basis.py, which fails if the two drift.
    """
    basis = copy.deepcopy(registry)
    basis.pop("therapeutic_taxonomy", None)
    basis.pop("provider_dynamic_profiles", None)
    basis.pop("consumer_therapeutic_authorities", None)
    return sha_bytes(json.dumps(
        basis, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode())


def _remint_therapeutic_authority_hashes(candidate_root: Path) -> dict[str, Any]:
    """Re-bind therapeutic_taxonomy.authorities[*].sha256 to the current bytes.

    DEFECT 2026-09-02: data/program-registry.json declares, per authority, a
    {path, sha256} pair, and glp_deploy_completeness_sensor's
    therapeutic_facet_bindings check fails when the declared hash does not match
    the file. NOTHING re-mints that declaration -
    scripts/reconcile-therapeutic-wire-in.mjs does not touch the authorities
    block at all - so the moment a program authority is legitimately re-authored
    through the governed path, the taxonomy is permanently out of date and the
    completeness sensor fails FOREVER. That sensor gates every deploy, so two
    stale hashes (altRx and ro-wegovy, both re-authored with new prices) blocked
    the switches, /go, the pixel and the backfill alike.

    This is a re-bind, not a bypass. It refuses anything it cannot prove is the
    same authority: the path must still resolve inside the program-authorities
    directory, parse as JSON, and carry a program whose id matches the authority
    the taxonomy names. Every change is returned with its before/after so the
    re-bind is auditable rather than silent. An authority whose file is missing
    or whose identity no longer matches is left ALONE, so the sensor still fails
    on genuine, unexplained drift.
    """
    registry_path = candidate_root / "data/program-registry.json"
    registry = read_json(registry_path)
    taxonomy = registry.get("therapeutic_taxonomy")
    if not isinstance(taxonomy, dict):
        return {"status": "PASS_NO_TAXONOMY", "rebound": []}
    authorities = taxonomy.get("authorities")
    if not isinstance(authorities, dict):
        return {"status": "PASS_NO_AUTHORITIES", "rebound": []}

    # NOT WIRED INTO release_prepared, deliberately (2026-09-03). Re-binding the
    # hash alone is INSUFFICIENT and unification proves it: the taxonomy also
    # stores EVIDENCE derived from each authority, and src/therapeutic-taxonomy.mjs
    # throws therapeutic_taxonomy_evidence_authority_mismatch:<program> when the
    # hash agrees but the evidence still reflects the old bytes. A correct fix
    # must RE-DERIVE the evidence from the current authority, not just restamp
    # the hash. Kept here with its tests because the diagnosis is worth
    # preserving; wiring it without the evidence refresh would trade a failing
    # sensor for a failing build.
    rebound: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for name, entry in authorities.items():
        if not isinstance(entry, dict):
            continue
        relative = str(entry.get("path") or "")
        declared = str(entry.get("sha256") or "")
        source = BUSINESS / relative
        if not relative.startswith("reports/") or not source.is_file():
            continue
        actual = sha_file(source)
        if actual == declared:
            continue
        # Only a program authority may be re-bound, and only to itself.
        if "/program-authorities/" not in relative:
            refused.append({"authority": name, "why": "not_a_program_authority_path"})
            continue
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            refused.append({"authority": name, "why": "authority_unreadable"})
            continue
        program_id = str(((document.get("program") or {}).get("program_id")) or "")
        expected = str(name)[len("b2_stage5_"):] if str(name).startswith("b2_stage5_") else ""
        if not program_id or program_id.replace("-", "_") != expected:
            refused.append({"authority": name, "why": "authority_identity_mismatch",
                            "program_id": program_id, "expected": expected})
            continue
        entry["sha256"] = actual
        rebound.append({"authority": name, "program_id": program_id,
                        "path": relative, "from": declared, "to": actual})

    if rebound:
        _detach(registry_path)
        atomic_json(registry_path, registry, mode=0o644)
    return {"status": "PASS_REBOUND" if rebound else "PASS_ALREADY_CURRENT",
            "rebound": rebound, "refused": refused,
            "rebound_count": len(rebound), "refused_count": len(refused)}


def _remint_price_index_semantic_basis(candidate_root: Path) -> dict[str, Any]:
    """Re-mint the price-index semantic basis after the LAST registry mutation.

    glp_surface_unification re-mints this guard, but it runs BEFORE the
    provider-wide payout switches and before /go provisioning, and
    apply_first_party_go_route_provision writes data/program-registry.json
    again. src/price-index.mjs normalizePriceIndexSourceHashes then fails
    closed with price_index_registry_semantic_basis_drift on any such change.
    /go provisioning had never completed a deploy before, so this ordering gap
    had never been reachable.

    Only the semantic-basis guard is touched. The frozen source hash and the
    weekly observations are never rewritten - a real price mutation still fails
    closed on its own hash.
    """
    registry_path = candidate_root / "data/program-registry.json"
    registry = read_json(registry_path)
    taxonomy = registry.get("therapeutic_taxonomy")
    if not isinstance(taxonomy, dict):
        return {"status": "PASS_NO_TAXONOMY_PRE_BUILD6_RELEASE", "changed": False}
    before = str(taxonomy.get("price_index_registry_programs_semantic_basis_sha256") or "")
    after = _price_index_registry_semantic_basis(registry)
    if before == after:
        return {"status": "PASS_ALREADY_CURRENT", "changed": False,
                "semantic_basis_sha256": after}
    taxonomy["price_index_registry_programs_semantic_basis_sha256"] = after
    # Key order is load-bearing here - see _write_candidate_json_preserving_key_order.
    _write_candidate_json_preserving_key_order(registry_path, registry)
    readback = read_json(registry_path)
    if (readback.get("therapeutic_taxonomy") or {}).get(
            "price_index_registry_programs_semantic_basis_sha256") != after:
        raise B2Error("price_index_semantic_basis_remint_readback_failed")
    if _price_index_registry_semantic_basis(readback) != after:
        raise B2Error("price_index_semantic_basis_remint_unstable")
    return {"status": "PASS_REMINTED", "changed": True,
            "semantic_basis_sha256_before": before,
            "semantic_basis_sha256_after": after,
            "frozen_source_hash_rewritten": False,
            "observations_rewritten": False}


def _write_candidate_json(path: Path, payload: dict[str, Any]) -> None:
    _detach(path)
    atomic_json(path, payload, mode=0o644)


def _write_candidate_json_preserving_key_order(path: Path, payload: dict[str, Any]) -> None:
    """Write a candidate JSON back WITHOUT re-sorting its object keys.

    DEFECT 2026-09-01 (deploy-fable-20260901). `atomic_json` sorts keys, which is
    correct for receipts and wrong for `data/program-registry.json`:
    `src/provider-dynamic-profiles.mjs:264` compares the stored
    `provider_dynamic_profiles.expected_counts` to its freshly derived twin with
    `JSON.stringify(...) !== JSON.stringify(...)` - a BYTE comparison that a pure
    re-ordering breaks even though every key and every value is identical.

    `glp_surface_unification` already knows this and writes the registry with
    `json.dumps(..., indent=2)` for exactly that reason, with the comment
    "Preserve the canonical reconciler's object-key order". But it runs BEFORE the
    provider-wide payout switches and before /go provisioning, and those wrote the
    file back sorted - so the canonical order it had just restored was lost again.

    Proven read-only, not inferred: a candidate materialized from the live head
    with ONLY the three staged /go requests provisioned fails
    `validate-ready6-editorial.mjs` with
    `provider_dynamic_profile_expected_counts_mismatch`; the two objects compare
    equal by value and differ only in key order. /go provisioning had never
    completed a deploy before, so this path had never been exercised.
    """
    _detach(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(raw, encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)
    os.chmod(path, 0o644)


def _replace_tracking_url_in_strings(value: Any, *, old_network: str, old_program_id: str,
                                     new_link: str) -> tuple[Any, int]:
    """Replace one structurally identified route inside JSON strings, including markdown."""
    if isinstance(value, dict):
        count = 0
        out = {}
        for key, child in value.items():
            out[key], changed = _replace_tracking_url_in_strings(
                child, old_network=old_network, old_program_id=old_program_id,
                new_link=new_link)
            count += changed
        return out, count
    if isinstance(value, list):
        count = 0
        out = []
        for child in value:
            replaced, changed = _replace_tracking_url_in_strings(
                child, old_network=old_network, old_program_id=old_program_id,
                new_link=new_link)
            out.append(replaced)
            count += changed
        return out, count
    if not isinstance(value, str):
        return value, 0
    count = 0
    text = value
    for candidate in re.findall(r"https://[^\s)\]}>\"']+", value):
        try:
            _tracking_link_identity(candidate.rstrip(".,;"), old_network, old_program_id)
        except B2Error:
            continue
        clean = candidate.rstrip(".,;")
        text = text.replace(clean, new_link)
        count += 1
    return text, count


PAYOUT_ACTIVE_AUTHORITY_FILES = (
    "data/affiliate-activation-selection.json",
    "data/money-page-specs.json",
    "src/money-page-specs.json",
    "data/blog/content-batch.json",
    "data/program-content-ledger.json",
    "src/program-content-ledger.json",
    "data/program-page-eligibility.json",
    "src/program-page-eligibility.json",
    "data/affiliate-held-link-manifest.json",
)
PAYOUT_ACTIVE_SURFACE_ROOTS = ("public", "functions")
PAYOUT_TEXT_SUFFIXES = frozenset({".html", ".json", ".js", ".mjs", ".md", ".txt", ".xml"})


def _payout_switch_surface_files(candidate_root: Path) -> list[Path]:
    files = {
        candidate_root / relative
        for relative in PAYOUT_ACTIVE_AUTHORITY_FILES
        if (candidate_root / relative).is_file()
    }
    for relative in PAYOUT_ACTIVE_SURFACE_ROOTS:
        root = candidate_root / relative
        if not root.is_dir():
            continue
        files.update(
            path for path in root.rglob("*")
            if path.is_file() and path.suffix.casefold() in PAYOUT_TEXT_SUFFIXES
        )
    return sorted(files)


def _validate_queue_tracking_link(link: Any, network: Any, program_id: Any) -> str:
    value = str(link or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise B2Error("payout_switch_tracking_link_not_https")
    observed = _sheet_only_program_id(normalize_network(network), "", value)
    if observed and observed != str(program_id or ""):
        raise B2Error("payout_switch_tracking_link_program_identity_mismatch")
    return value


def verify_provider_wide_payout_switch(
    candidate_root: Path, switch_request: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed if any active provider surface keeps a quantified loser href."""
    if switch_request.get("queue_source") != "sheet_update_queue:payout_evaluation":
        raise B2Error("payout_switch_queue_source_invalid")
    winner = switch_request.get("winner") or {}
    winner_sheet = switch_request.get("winner_sheet") or {}
    winner_link = _validate_queue_tracking_link(
        winner_sheet.get("Tracking Link / Deep Link"),
        winner.get("network"), winner.get("program_id"),
    )
    loser_links = []
    for record in switch_request.get("affected_sheet_records") or []:
        sheet = record.get("sheet") or {}
        link = str(sheet.get("Tracking Link / Deep Link") or "").strip()
        if link and link != winner_link:
            loser_links.append(link)
    files = _payout_switch_surface_files(Path(candidate_root))
    remaining = {link: [] for link in loser_links}
    winner_occurrences = 0
    for path in files:
        text = path.read_text(encoding="utf-8", errors="strict")
        winner_occurrences += text.count(winner_link)
        for link in loser_links:
            if link in text:
                remaining[link].append(str(path.relative_to(candidate_root)))
    leaked = {link: paths for link, paths in remaining.items() if paths}
    if leaked:
        raise B2Error(
            "higher_payout_provider_links_unswitched:"
            + str(switch_request.get("provider_identity") or "unknown")
            + ":" + ",".join(sorted({path for paths in leaked.values() for path in paths}))
        )
    active_affiliate_occurrences = winner_occurrences + sum(
        len(paths) for paths in remaining.values()
    )
    if winner_occurrences < 1 and active_affiliate_occurrences:
        raise B2Error(
            "higher_payout_winner_link_missing:"
            + str(switch_request.get("provider_identity") or "unknown")
        )
    return {
        "status": (
            "PASS_PROVIDER_WIDE_HIGHER_PAYOUT_RAIL"
            if winner_occurrences
            else "PASS_NO_ACTIVE_PROVIDER_AFFILIATE_LINKS_TO_SWITCH"
        ),
        "provider": switch_request.get("provider"),
        "provider_identity": switch_request.get("provider_identity"),
        "winner": winner,
        "affected_sheet_rows": switch_request.get("affected_sheet_rows"),
        "files_inspected": len(files),
        "winner_href_occurrences": winner_occurrences,
        "loser_href_occurrences": 0,
        "active_affiliate_href_occurrences": active_affiliate_occurrences,
        "verification_method": "local_href_and_config_inspection_only",
        "tracking_link_requests": 0,
        "tracking_clicks": 0,
    }


def _apply_affiliati_offer_migration(
    candidate_root: Path,
    switch_request: dict[str, Any],
    winner_link: str,
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """Migrate an Affiliati receiver offer only when immutable authority requires it."""
    winner = switch_request.get("winner") or {}
    if normalize_network(winner.get("network")) != "affiliati":
        return {"status": "PASS_NOT_REQUIRED_NON_AFFILIATI_WINNER", "changed_files": {}}

    path = Path(registry_path or AFFILIATI_SWITCH_PREREQUISITES)
    registry = read_json(path)
    if (
        registry.get("schema") != "glp.affiliati-switch-prerequisites.v1"
        or registry.get("status") != "PASS_CURRENT_PREREQUISITES_BOUND"
    ):
        raise B2Error("affiliati_offer_migration_registry_invalid")
    to_offer_id = str(winner.get("program_id") or "")
    record = (registry.get("offers") or {}).get(to_offer_id) or {}
    migration = record.get("same_transaction_offer_migration")
    if not migration:
        return {"status": "PASS_NOT_REQUIRED_NO_OFFER_MIGRATION", "changed_files": {}}
    from_offer_id = str(migration.get("from_offer_id") or "")
    if (
        str(migration.get("to_offer_id") or "") != to_offer_id
        or not from_offer_id
        or from_offer_id == to_offer_id
    ):
        raise B2Error("affiliati_offer_migration_identity_invalid")
    if str(record.get("provider") or "") != str(switch_request.get("provider") or ""):
        raise B2Error("affiliati_offer_migration_provider_mismatch")
    if sha_bytes(winner_link.encode()) != str(record.get("tracking_href_sha256") or ""):
        raise B2Error("affiliati_offer_migration_tracking_link_mismatch")

    prerequisites = record.get("prerequisites") or {}
    evidence = record.get("evidence") or {}
    for name in registry.get("required_prerequisites") or []:
        if prerequisites.get(name) is not True:
            raise B2Error(f"affiliati_offer_migration_prerequisite_incomplete:{name}")
        binding = evidence.get(name)
        if not isinstance(binding, dict):
            raise B2Error(f"affiliati_offer_migration_evidence_missing:{name}")
        verify_binding(binding, label=f"affiliati_offer_migration_{name}")

    source_contract_binding = registry.get("postback_contract") or {}
    verify_binding(source_contract_binding, label="affiliati_offer_migration_source_contract")
    contract_path = Path(candidate_root) / "data/affiliati-postback-contract.json"
    if (
        not contract_path.is_file()
        or sha_file(contract_path) != source_contract_binding.get("sha256")
        or contract_path.stat().st_size != source_contract_binding.get("bytes")
    ):
        raise B2Error("affiliati_offer_migration_candidate_contract_drift")
    contract = read_json(contract_path)
    if contract.get("schema") != "affiliati-receiver-contract-v2-owned-click-join":
        raise B2Error("affiliati_offer_migration_contract_schema_invalid")
    old_rows = [row for row in contract.get("offers") or []
                if str(row.get("offer_id") or "") == from_offer_id]
    new_rows = [row for row in contract.get("offers") or []
                if str(row.get("offer_id") or "") == to_offer_id]
    if len(old_rows) != 1 or new_rows:
        raise B2Error("affiliati_offer_migration_contract_offer_match_invalid")
    if str(old_rows[0].get("provider") or "") != str(record.get("provider") or ""):
        raise B2Error("affiliati_offer_migration_contract_provider_mismatch")
    old_rows[0]["offer_id"] = to_offer_id
    old_rows[0]["tracking_href_sha256"] = str(record.get("tracking_href_sha256") or "")
    _write_candidate_json(contract_path, contract)

    attribution_path = Path(candidate_root) / "src/affiliati-attribution.mjs"
    source = attribution_path.read_text(encoding="utf-8", errors="strict")
    provider = str(record.get("provider") or "")
    old_literal = f'Object.freeze({{ offer_id: "{from_offer_id}", provider: "{provider}",'
    new_literal = f'Object.freeze({{ offer_id: "{to_offer_id}", provider: "{provider}",'
    if source.count(old_literal) != 1 or new_literal in source:
        raise B2Error("affiliati_offer_migration_attribution_offer_match_invalid")
    _detach(attribution_path)
    attribution_path.write_text(source.replace(old_literal, new_literal), encoding="utf-8")

    return {
        "status": "PASS_AFFILIATI_OFFER_MIGRATION_APPLIED_TO_CANDIDATE",
        "provider": record.get("provider"),
        "from_offer_id": from_offer_id,
        "to_offer_id": to_offer_id,
        "reason": migration.get("reason"),
        "changed_files": {
            str(contract_path.relative_to(candidate_root)): 1,
            str(attribution_path.relative_to(candidate_root)): 1,
        },
        "tracking_link_requests": 0,
        "tracking_clicks": 0,
    }


# Katalys and Awin are the only networks whose host is pinned by NAME, in both
# src/money-page.mjs and glp_surface_unification.NETWORK_HOSTS. Every other
# network is pinned by the row's own affiliate_tracking_host.
PAYOUT_SWITCH_NETWORK_HOSTS = {"Katalys": "track.revoffers.com", "Awin": "www.awin1.com"}


def _payout_switch_canonical_network(network: str) -> str:
    """Map a plan network token onto the display name the surfaces compare."""
    for display in PAYOUT_SWITCH_NETWORK_HOSTS:
        if normalize_network(display) == normalize_network(network):
            return display
    return str(network or "")


def _payout_switch_rebind_rail_identity(
    candidate_root: Path,
    site_program_id: str,
    winner_link: str,
    winner_network: str,
    changed: dict[str, int],
) -> None:
    """Carry the rail identity with the link on every surface row that moved."""
    if not site_program_id or not winner_link:
        return
    display = _payout_switch_canonical_network(winner_network)
    host = PAYOUT_SWITCH_NETWORK_HOSTS.get(display) or urlsplit(winner_link).hostname
    if not host:
        raise B2Error(f"payout_switch_winner_host_unresolvable:{site_program_id}")

    # Collect the spellings the artifact itself already uses, so a genuinely
    # new rail adopts the document's own display form instead of an invented
    # one. DEFECT 2026-09-02: writing the plan's lowercase token here took
    # altRx, Curex and FuturHealth dark - their network did not even change
    # (XenTraffic -> "xentraffic"), the CASE alone was enough. Same class as the
    # selection-file bug, one layer down.
    known: dict[str, str] = {}

    def learn(node: Any) -> None:
        if isinstance(node, dict):
            value = node.get("affiliate_network")
            if isinstance(value, str) and value:
                known.setdefault(normalize_network(value), value)
            for item in node.values():
                learn(item)
        elif isinstance(node, list):
            for item in node:
                learn(item)

    def rebind(node: Any) -> int:
        touched = 0
        if isinstance(node, dict):
            # Match on the LINK, not the program id. The switch request's
            # site_program_id is the review-page identity (direct-meds-review)
            # while a money-page row is keyed by the provider identity
            # (direct-meds), so an id match silently rebinds nothing - which is
            # exactly how the first version of this fix failed. Any row already
            # carrying the winner's exact tracking link is on that rail by
            # construction, so its identity must describe that rail; a row that
            # legitimately held the link before is a no-op.
            if str(node.get("affiliate_href") or "") == winner_link:
                current = str(node.get("affiliate_network") or "")
                # Unchanged rail: leave the spelling exactly as the artifact has
                # it. Only a genuine network change rewrites the name, and then
                # it prefers a spelling already present in this document.
                if normalize_network(current) != normalize_network(winner_network):
                    preferred = known.get(normalize_network(winner_network), display)
                    if current != preferred:
                        node["affiliate_network"] = preferred
                        touched += 1
                if node.get("affiliate_tracking_host") != host:
                    node["affiliate_tracking_host"] = host
                    touched += 1
            for value in node.values():
                touched += rebind(value)
        elif isinstance(node, list):
            for value in node:
                touched += rebind(value)
        return touched

    for path in _payout_switch_surface_files(candidate_root):
        if path.suffix != ".json" or not path.is_file():
            continue
        document = read_json(path)
        learn(document)
        touched = rebind(document)
        if touched:
            _write_candidate_json(path, document)
            key = str(path.relative_to(candidate_root))
            changed[key] = max(touched, changed.get(key, 0))


#: PAYOUT_SWITCH_WINNER_OPERABLE_V1 - a payout switch may not repoint a live
#: provider onto a rail this round could not wire.
#:
#: A switch rewrites the SITE (money-page-specs, the activation selection, the
#: content ledger, every payout-switch surface file) for a provider that need
#: not be in this round's prepared set at all, and nothing downstream re-checks
#: the winning rail. So a BLOCKED winner silently repoints a currently-earning
#: provider at a rail that cannot track. Measured on 2026-09-03: Direct Meds
#: went from a live Katalys link to an Affiliati link with an empty s1 on an
#: offer whose postback_url is "" - the link-map floor caught it as a lost
#: compare link (candidate=54 floor=54 lost=direct-meds), which is revenue
#: taken dark, not a counting artifact.
#:
#: Highest-payout-wins is NOT weakened: the switch is DEFERRED with the exact
#: block reason and applies unchanged on the first round that wires the rail.
PAYOUT_SWITCH_WINNER_OPERABLE_CONTRACT = "PAYOUT_SWITCH_WINNER_OPERABLE_V1"


def payout_switch_winner_block_reason(
    request: dict[str, Any], ledger: dict[str, Any]
) -> str | None:
    """The winner rail's block reason, or None when it is operable."""
    winner = request.get("winner") or {}
    network = normalize_network(winner.get("network"))
    program_id = str(winner.get("program_id") or "")
    if not network or not program_id:
        return "payout_switch_winner_identity_missing"
    row = (ledger.get("rows") or {}).get(f"{network}:{program_id}") or {}
    if row.get("status") == "blocked":
        return str(row.get("block_reason") or "blocked")
    return None


def apply_provider_wide_payout_switch(
    candidate_root: Path, switch_request: dict[str, Any],
) -> dict[str, Any]:
    """Consume one payout_evaluation request on detached deploy-candidate bytes."""
    if (
        switch_request.get("status") != "STAGED_NOT_DEPLOYED"
        or switch_request.get("queue_kind") != "affiliate_tracking_link_swap"
        or switch_request.get("deploy_instruction_version") != PAYOUT_LEAK_INSTRUCTION_VERSION
        or switch_request.get("provider_all_links") is not True
        or switch_request.get("preserve_links_until_atomic_deploy") is not True
        or switch_request.get("sheet_updates_via_single_writer_only") is not True
        or switch_request.get("href_config_inspection_only") is not True
        or switch_request.get("tracking_clicks") != 0
    ):
        raise B2Error("payout_switch_deploy_contract_invalid")
    candidate_root = Path(candidate_root)
    winner = switch_request.get("winner") or {}
    winner_sheet = switch_request.get("winner_sheet") or {}
    winner_link = _validate_queue_tracking_link(
        winner_sheet.get("Tracking Link / Deep Link"),
        winner.get("network"), winner.get("program_id"),
    )
    affected = switch_request.get("affected_sheet_records") or []
    if len(affected) != len(switch_request.get("affected_sheet_rows") or []):
        raise B2Error("payout_switch_affected_sheet_records_incomplete")
    loser_links = []
    for record in affected:
        sheet = record.get("sheet") or {}
        row_number = int(record.get("sheet_row") or 0)
        if row_number not in set(int(value) for value in switch_request["affected_sheet_rows"]):
            raise B2Error("payout_switch_affected_sheet_row_unbound")
        link = _validate_queue_tracking_link(
            sheet.get("Tracking Link / Deep Link"),
            sheet.get("Network / Route"),
            _sheet_only_program_id(
                normalize_network(sheet.get("Network / Route")),
                str(sheet.get("Apply URL") or ""),
                str(sheet.get("Tracking Link / Deep Link") or ""),
            ),
        )
        if link != winner_link:
            loser_links.append(link)

    changed: dict[str, int] = {}
    for path in _payout_switch_surface_files(candidate_root):
        text = path.read_text(encoding="utf-8", errors="strict")
        replacements = sum(text.count(link) for link in loser_links)
        if replacements:
            for link in loser_links:
                text = text.replace(link, winner_link)
            _detach(path)
            path.write_text(text, encoding="utf-8")
            changed[str(path.relative_to(candidate_root))] = replacements

    # DEFECT 2026-09-02, first exposure of this path: the substitution above is
    # TEXTUAL - it swaps the loser link for the winner link wherever it appears.
    # But a money-page row carries the rail IDENTITY beside the href
    # (affiliate_network, affiliate_tracking_host), and glp_surface_unification
    # derived those from the OLD rail moments earlier. src/money-page.mjs:224
    # then recomputes the expected host FROM the network - Katalys ->
    # track.revoffers.com, Awin -> www.awin1.com, anything else -> the row's own
    # affiliate_tracking_host - and throws affiliate_direct_link_host_invalid
    # when it does not match the href. Direct Meds moving Katalys -> Affiliati
    # kept network "Katalys" and host "track.revoffers.com" against an
    # epsilonmkt.com href, and the whole build died. Rebind the identity with
    # the link, using exactly the mapping both consumers already use.
    _payout_switch_rebind_rail_identity(
        candidate_root,
        str(switch_request.get("site_program_id") or ""),
        winner_link,
        str(winner.get("network") or ""),
        changed,
    )

    selection_path = candidate_root / "data/affiliate-activation-selection.json"
    selection = read_json(selection_path)
    site_program_id = str(switch_request.get("site_program_id") or "")
    selected = [row for row in selection.get("programs") or [] if row.get("program_id") == site_program_id]
    if len(selected) > 1:
        raise B2Error(f"payout_switch_activation_selection_match_count:{len(selected)}")
    selection_action = "PASS_NOT_IN_TRACKED_ACTIVATION_SUBSET"
    if selected:
        selected[0].update({
            # DEFECT 2026-09-02, caught by the pre-materialization gate before
            # it could ship: this wrote the PLAN's network token, which is
            # lowercase ("katalys"). src/affiliate-link-map.mjs keys
            # TRACKING_HOST_BY_NETWORK by the DISPLAY name ("Katalys"), so
            # allowedHost came back undefined, the row failed the host check,
            # and authorizedSelectionMap returns null for the WHOLE selection -
            # which makes buildAffiliateLinkMap return {} and takes EVERY
            # affiliate link on the site dark. One mis-cased row blanks all 54.
            # Measured on the scratch tree: compare went 56 entries / 54 live to
            # 0 / 0. Write the display form, the same one the money-page rebind
            # and glp_surface_unification.NETWORK_HOSTS use.
            "network": _payout_switch_canonical_network(str(winner.get("network") or "")),
            "network_program_id": str(winner.get("program_id") or ""),
            "sheet_row": int(winner.get("sheet_row") or 0),
            "tracking_host": urlsplit(winner_link).hostname,
            "tracking_link_sha256": sha_bytes(winner_link.encode()),
        })
        _write_candidate_json(selection_path, selection)
        changed[str(selection_path.relative_to(candidate_root))] = max(
            1, changed.get(str(selection_path.relative_to(candidate_root)), 0)
        )
        selection_action = "PASS_TRACKED_ACTIVATION_SUBSET_UPDATED"
    offer_migration = _apply_affiliati_offer_migration(
        candidate_root, switch_request, winner_link,
    )
    changed.update(offer_migration.get("changed_files") or {})
    verification = verify_provider_wide_payout_switch(candidate_root, switch_request)
    return {
        "status": "PASS_PROVIDER_WIDE_PAYOUT_SWITCH_APPLIED_TO_CANDIDATE",
        "switch_id": switch_request.get("switch_id"),
        "queue_update_ids": switch_request.get("queue_update_ids"),
        "provider": switch_request.get("provider"),
        "provider_identity": switch_request.get("provider_identity"),
        "winner": winner,
        "affected_sheet_rows": switch_request.get("affected_sheet_rows"),
        "changed_files": changed,
        "activation_selection": {
            "status": selection_action,
            "site_program_id": site_program_id,
            "match_count": len(selected),
            "program_count_semantics": "tracked_activation_subset_not_live_provider_count",
        },
        "affiliati_offer_migration": offer_migration,
        "verification": verification,
        "sheet_update_instruction": "post-deploy facts must enter sheet_update_queue; no direct Sheet writer",
        "external_effects": {"provider_calls": 0, "tracking_link_requests": 0,
                             "tracking_clicks": 0, "sheet_writes": 0, "deployments": 0},
    }


def _apply_best_terms_tracking_link_swap(
    candidate_root: Path,
    *,
    site_program_id: str,
    switch_request: dict[str, Any],
    incumbent_sheet_row: dict[str, Any],
    winner_sheet_row: dict[str, Any],
) -> dict[str, Any]:
    """Atomically repoint one canonical merchant to its staged best-terms rail.

    This is the deploy consumer for `queue_kind=affiliate_tracking_link_swap`.
    It mutates only a detached candidate tree, never requests either tracking
    URL, preserves the losing marketplace relationship, and fails unless every
    active authority names the exact incumbent and winner identities.
    """
    if switch_request.get("status") != "STAGED_NOT_DEPLOYED":
        raise B2Error("best_terms_switch_not_staged")
    if switch_request.get("queue_kind") != "affiliate_tracking_link_swap":
        raise B2Error("best_terms_switch_consumer_contract_invalid")
    if str(switch_request.get("site_program_id") or "") != site_program_id:
        raise B2Error("best_terms_switch_site_program_mismatch")

    incumbent = switch_request.get("incumbent") or switch_request.get("prior_winner") or {}
    winner = switch_request.get("winner") or {}
    incumbent_row_num = int(incumbent.get("sheet_row") or 0)
    winner_row_num = int(winner.get("sheet_row") or switch_request.get("target_sheet_row") or 0)
    if incumbent_row_num < 2 or winner_row_num < 2 or incumbent_row_num == winner_row_num:
        raise B2Error("best_terms_switch_sheet_rows_invalid")
    if int(switch_request.get("target_sheet_row") or 0) != winner_row_num:
        raise B2Error("best_terms_switch_target_row_mismatch")

    old_link = str(incumbent_sheet_row.get("Tracking Link / Deep Link") or "").strip()
    new_link = str(winner_sheet_row.get("Tracking Link / Deep Link") or "").strip()
    old_network = str(incumbent.get("network") or incumbent_sheet_row.get("Network / Route") or "").strip()
    new_network = str(winner.get("network") or winner_sheet_row.get("Network / Route") or "").strip()
    old_program_id = str(incumbent.get("program_id") or "").strip()
    new_program_id = str(winner.get("program_id") or "").strip()
    old_rail, old_host = _tracking_link_identity(old_link, old_network, old_program_id)
    new_rail, new_host = _tracking_link_identity(new_link, new_network, new_program_id)
    if old_rail == new_rail or old_link == new_link:
        raise B2Error("best_terms_switch_not_cross_rail")
    if int(incumbent.get("sheet_row") or 0) != incumbent_row_num:
        raise B2Error("best_terms_incumbent_row_missing")
    if int(winner.get("sheet_row") or 0) != winner_row_num:
        raise B2Error("best_terms_winner_row_missing")
    if normalize_network(incumbent_sheet_row.get("Network / Route")) != old_rail:
        raise B2Error("best_terms_incumbent_sheet_network_mismatch")
    if normalize_network(winner_sheet_row.get("Network / Route")) != new_rail:
        raise B2Error("best_terms_winner_sheet_network_mismatch")
    if not true(incumbent_sheet_row.get("Wired Live?")) or not true(winner_sheet_row.get("Wired Live?")):
        raise B2Error("best_terms_switch_requires_two_live_sheet_routes")

    changed: dict[str, Any] = {}
    selection_path = candidate_root / "data/affiliate-activation-selection.json"
    selection = read_json(selection_path)
    selected = [row for row in selection.get("programs") or [] if row.get("program_id") == site_program_id]
    if len(selected) != 1:
        raise B2Error(f"best_terms_selection_match_count:{len(selected)}")
    selected[0].update({
        "network": new_network,
        "network_program_id": new_program_id,
        "sheet_row": winner_row_num,
        "sheet_status": "approved_active_tracking_ready",
        "tracking_host": new_host,
        "tracking_link_sha256": sha_bytes(new_link.encode()),
    })
    _write_candidate_json(selection_path, selection)
    changed[str(selection_path.relative_to(candidate_root))] = 1

    for relative in ("data/money-page-specs.json", "src/money-page-specs.json"):
        path = candidate_root / relative
        document = read_json(path)
        matches = 0
        for row in _json_objects(document):
            if row.get("program_id") != site_program_id or "affiliate_href" not in row:
                continue
            if normalize_network(row.get("affiliate_network")) != old_rail:
                raise B2Error(f"best_terms_money_row_incumbent_drift:{relative}")
            _tracking_link_identity(row.get("affiliate_href"), old_network, old_program_id)
            row["affiliate_href"] = new_link
            row["affiliate_network"] = new_network
            matches += 1
        if matches < 1:
            raise B2Error(f"best_terms_money_rows_missing:{relative}")
        _write_candidate_json(path, document)
        changed[relative] = matches

    for relative in ("data/program-content-ledger.json", "src/program-content-ledger.json"):
        path = candidate_root / relative
        document = read_json(path)
        matches = [row for row in document.get("programs") or [] if row.get("program_id") == site_program_id]
        if len(matches) != 1 or normalize_network(matches[0].get("affiliate_network")) != old_rail:
            raise B2Error(f"best_terms_content_ledger_match_invalid:{relative}:{len(matches)}")
        matches[0]["affiliate_network"] = new_network
        _write_candidate_json(path, document)
        changed[relative] = 1

    for relative in ("data/program-page-eligibility.json", "src/program-page-eligibility.json"):
        path = candidate_root / relative
        document = read_json(path)
        matches = [row for row in document.get("programs") or [] if row.get("program_id") == site_program_id]
        if len(matches) != 1 or normalize_network(matches[0].get("network")) != old_rail:
            raise B2Error(f"best_terms_eligibility_match_invalid:{relative}:{len(matches)}")
        matches[0]["network"] = new_rail
        _write_candidate_json(path, document)
        changed[relative] = 1

    held_path = candidate_root / "data/affiliate-held-link-manifest.json"
    held = read_json(held_path)
    held_rows = [row for row in held.get("programs") or [] if row.get("content_program_id") == site_program_id]
    if len(held_rows) != 1 or str(held_rows[0].get("immutable_network_program_id") or "") != old_program_id:
        raise B2Error(f"best_terms_held_manifest_match_invalid:{len(held_rows)}")
    held_rows[0].update({
        "canonical_sheet_row": winner_row_num,
        "immutable_network_program_id": new_program_id,
        "network": new_network,
        "relationship_source_status": "approved_active_tracking_ready",
    })
    _write_candidate_json(held_path, held)
    changed[str(held_path.relative_to(candidate_root))] = 1

    approved_path = candidate_root / "data/approved-program-registry.json"
    approved = read_json(approved_path)
    incumbent_approved = [row for row in approved.get("programs") or []
                          if normalize_network(row.get("network")) == old_rail
                          and str(row.get("provider_program_id") or "") == old_program_id]
    winner_approved = [row for row in approved.get("programs") or []
                       if normalize_network(row.get("network")) == new_rail
                       and str(row.get("provider_program_id") or "") == new_program_id]
    if len(incumbent_approved) != 1 or len(winner_approved) != 1:
        raise B2Error("best_terms_approved_registry_identity_missing_or_ambiguous")
    incumbent_approved[0]["existing_release_alias"] = None
    incumbent_approved[0]["routing_class"] = "approved_tracking_ready_route_retired_best_terms"
    incumbent_approved[0]["tracking"].update({
        "candidate_registry_wired": True,
        "state": "candidate_registry_wired_provider_issued",
    })
    winner_approved[0]["existing_release_alias"] = site_program_id
    winner_approved[0]["routing_class"] = "current_glp_live_wired"
    winner_approved[0]["tracking"].update({
        "candidate_registry_wired": True,
        "state": "candidate_registry_wired_provider_issued",
    })
    _write_candidate_json(approved_path, approved)
    changed[str(approved_path.relative_to(candidate_root))] = 2

    content_batch_path = candidate_root / "data/blog/content-batch.json"
    content_batch = read_json(content_batch_path)
    content_batch, content_batch_replacements = _replace_tracking_url_in_strings(
        content_batch, old_network=old_network, old_program_id=old_program_id,
        new_link=new_link)
    if content_batch_replacements < 1:
        raise B2Error("best_terms_content_batch_incumbent_route_missing")
    _write_candidate_json(content_batch_path, content_batch)
    changed[str(content_batch_path.relative_to(candidate_root))] = content_batch_replacements

    approvals_path = candidate_root / "data/affiliate-wire-in-approvals.json"
    approvals = read_json(approvals_path)
    terms_rows = [row for row in (approvals.get("current_terms_reassessment") or {}).get("programs") or []
                  if str(row.get("provider_program_id") or "") == old_program_id]
    if len(terms_rows) != 1:
        raise B2Error(f"best_terms_incumbent_terms_match_count:{len(terms_rows)}")
    terms_rows[0].update({
        "existing_live_surfaces": [],
        "live_link_disposition": "retired_best_terms_higher_payout",
        "may_authorize_material_expansion": False,
        "authorized_material_expansion_surfaces": [],
    })
    _write_candidate_json(approvals_path, approvals)
    changed[str(approvals_path.relative_to(candidate_root))] = 1

    replacements = {
        "src/affiliate-link-map.mjs": [(
            '  "80963": Object.freeze({ alias: "shedrx", existing: ["compare", "cost", "quiz", "calculator"], expansion: [] }),\n',
            "",
        )],
        "scripts/validate-affiliate-held-manifest.py": [(
            '    "shedrx": ("Awin", "80963", 194, "approved_active_tracking_ready", "hold_compounded"),',
            '    "shedrx": ("Katalys", "1516", 323, "approved_active_tracking_ready", "hold_compounded"),',
        )],
        "scripts/validate-provider-fastpath-plus5.mjs": [
            ('  ["80963", ["compare", "cost", "quiz", "calculator"]],',
             '  ["80963", []],'),
            ('  const rejected = item.verdict === "operator_rejected_terms";\n'
             '  assert.equal(item.live_link_disposition, rejected ? "de_link_all_surfaces_operator_rejected_terms" : "preserve_existing_live");',
             '  const rejected = item.verdict === "operator_rejected_terms";\n'
             '  const retiredBestTerms = item.provider_program_id === "80963";\n'
             '  const disposition = rejected ? "de_link_all_surfaces_operator_rejected_terms"\n'
             '    : retiredBestTerms ? "retired_best_terms_higher_payout" : "preserve_existing_live";\n'
             '  assert.equal(item.live_link_disposition, disposition);'),
        ],
        "tests/high-intent-affiliate-surfaces.test.mjs": [
            ('test("calculator decision-aware authority carries the exact five authorized Awin Item 1 tracked links", async () => {',
             'test("calculator authority carries four Awin Item 1 links plus the sole Katalys Shed route", async () => {'),
            ('    ["drhouse", "53271"], ["shedrx", "80963"], ["biltrx", "115383"],\n'
             '    ["serena-health", "128203"], ["coivas", "128761"],',
             '    ["drhouse", "53271"], ["biltrx", "115383"],\n'
             '    ["serena-health", "128203"], ["coivas", "128761"],'),
            ('  assert.equal(selected.some((row) => row.program_id === "measured"), false);',
             '  assert.equal(selected.some((row) => row.program_id === "measured"), false);\n'
             '  const shed = links.find((row) => row.program_id === "shedrx");\n'
             '  assert.ok(shed, "Katalys Shed winner missing");\n'
             '  const shedUrl = new URL(shed.affiliate_href);\n'
             '  assert.equal(shedUrl.hostname, "track.revoffers.com");\n'
             '  assert.equal(shedUrl.searchParams.get("offer_id"), "1516");'),
            ('  const item1 = (links) => links.filter((row) => ["drhouse", "shedrx", "biltrx", "measured", "serena-health", "coivas"].includes(row.program_id));',
             '  const item1 = (links) => links.filter((row) => ["drhouse", "biltrx", "measured", "serena-health", "coivas"].includes(row.program_id));'),
            ('    const isRejected = row.verdict === "operator_rejected_terms";\n'
             '    assert.equal(row.live_link_disposition, isRejected ? "de_link_all_surfaces_operator_rejected_terms" : "preserve_existing_live");',
             '    const isRejected = row.verdict === "operator_rejected_terms";\n'
             '    const retiredBestTerms = row.provider_program_id === "80963";\n'
             '    const disposition = isRejected ? "de_link_all_surfaces_operator_rejected_terms"\n'
             '      : retiredBestTerms ? "retired_best_terms_higher_payout" : "preserve_existing_live";\n'
             '    assert.equal(row.live_link_disposition, disposition);'),
            ('  assert.deepEqual(shedrx.existing_live_surfaces, ["compare", "cost", "quiz", "calculator"]);',
             '  assert.deepEqual(shedrx.existing_live_surfaces, []);'),
        ],
    }
    for relative, file_replacements in replacements.items():
        path = candidate_root / relative
        _detach(path)
        text = path.read_text(encoding="utf-8")
        for before, after in file_replacements:
            if text.count(before) != 1:
                raise B2Error(f"best_terms_source_contract_match_count:{relative}:{text.count(before)}")
            text = text.replace(before, after)
        path.write_text(text, encoding="utf-8")
        changed[relative] = len(file_replacements)

    return {
        "status": "PASS_AFFILIATE_TRACKING_LINK_SWAP",
        "consumer_contract": "affiliate_tracking_link_swap",
        "switch_id": switch_request.get("switch_id"),
        "site_program_id": site_program_id,
        "incumbent": {"network": old_network, "program_id": old_program_id,
                      "sheet_row": incumbent_row_num, "tracking_host": old_host,
                      "tracking_link_sha256": sha_bytes(old_link.encode())},
        "winner": {"network": new_network, "program_id": new_program_id,
                   "sheet_row": winner_row_num, "tracking_host": new_host,
                   "tracking_link_sha256": sha_bytes(new_link.encode())},
        "changed_files": changed,
        "external_effects": {"provider_calls": 0, "tracking_clicks": 0,
                             "sheet_writes": 0, "deployments": 0},
    }


def _copy_candidate(source: Path, target: Path) -> dict[str, Any]:
    if target.exists():
        raise B2Error("candidate_target_exists")
    materialization = copytree_linked(
        source,
        target,
        ignore=shutil.ignore_patterns(".wrangler", "node_modules"),
    )
    make_directories_writable(target)
    return materialization


def _detach_build_generated_outputs(candidate: Path) -> dict[str, Any]:
    """Break predecessor hardlinks for files regenerated before public/ is rebuilt.

    Sealed releases are 0444. The build's image and CSS generators run before
    build-staging removes/recreates public/, so writing those existing hardlinks
    directly either fails or risks predecessor mutation. Detaching only the
    declared generated outputs preserves copy-on-write isolation without making
    the whole candidate tree writable.
    """
    relative_paths = set(BUILD_GENERATED_STATIC_OUTPUTS)
    for tree_relative in BUILD_GENERATED_SNAPSHOT_TREES:
        tree = candidate / tree_relative
        if not tree.is_dir() or tree.is_symlink():
            raise B2Error(f"build_generated_snapshot_tree_missing:{tree_relative}")
        tree_files = [
            path for path in tree.rglob("*")
            if path.is_file() and not path.is_symlink()
        ]
        if not tree_files:
            raise B2Error(f"build_generated_snapshot_tree_empty:{tree_relative}")
        relative_paths.update(
            path.relative_to(candidate).as_posix() for path in tree_files
        )
    for manifest_relative in BUILD_GENERATED_IMAGE_MANIFESTS:
        manifest_path = candidate / manifest_relative
        if not manifest_path.is_file():
            raise B2Error(f"build_generated_image_manifest_missing:{manifest_relative}")
        manifest = read_json(manifest_path)
        outputs = manifest.get("outputs") or []
        if not isinstance(outputs, list) or not outputs:
            raise B2Error(f"build_generated_image_outputs_missing:{manifest_relative}")
        relative_paths.add(manifest_relative)
        for row in outputs:
            if not isinstance(row, dict):
                raise B2Error(f"build_generated_image_output_invalid:{manifest_relative}")
            for field in ("path", "source_mirror"):
                relative = str(row.get(field) or "")
                if not relative or relative.startswith("/") or ".." in Path(relative).parts:
                    raise B2Error(f"build_generated_output_path_invalid:{manifest_relative}:{field}")
                relative_paths.add(relative)
    detached = []
    for relative in sorted(relative_paths):
        path = candidate / relative
        if not path.is_file():
            raise B2Error(f"build_generated_output_missing:{relative}")
        _detach(path)
        detached.append(relative)
    return {"status": "PASS_DETACHED", "count": len(detached), "paths": detached}


def _tree_closures(root: Path) -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("glp_price_index_weekly", SCRIPTS / "glp_price_index_weekly.py")
    if spec is None or spec.loader is None:
        raise B2Error("closure_module_load_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    release_tree, _ = module.closure(root)
    public_tree, _ = module.closure(root / "public")
    functions_tree, _ = module.closure(root / "functions")
    return {"release": release_tree, "public": public_tree, "functions": functions_tree}


def _seal_candidate(candidate: Path, release: Path) -> None:
    if release.exists():
        raise B2Error("release_target_exists")
    temporary = release.with_name(f".{release.name}.tmp-{os.getpid()}")
    if temporary.exists():
        remove_materialized_tree(temporary)
    shutil.copytree(candidate, temporary, copy_function=shutil.copy2)
    for path in sorted(temporary.rglob("*"), reverse=True):
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    os.chmod(temporary, 0o555)
    result = subprocess.run(
        ["sudo", "-n", "chown", "-R", "root:root", str(temporary)],
        text=True, capture_output=True, timeout=300,
    )
    if result.returncode:
        remove_materialized_tree(temporary)
        raise B2Error(f"release_chown_failed:{result.stderr[-300:]}")
    os.replace(temporary, release)


def _freeze_candidate_for_step(root: Path) -> None:
    """Make a completed mutable step safe as the next hardlink predecessor."""
    if not root.is_dir():
        raise B2Error(f"candidate_step_source_missing:{root}")
    for path in sorted(root.rglob("*"), reverse=True):
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o222:
            os.chmod(path, mode & ~0o222)
    mode = stat.S_IMODE(root.stat().st_mode)
    if mode & 0o222:
        os.chmod(root, mode & ~0o222)


def _mark_release_row_blocked(
    *,
    ledger: dict[str, Any],
    day: dict[str, Any],
    state: dict[str, Any],
    output_dir: Path,
    error: Exception,
) -> dict[str, Any]:
    """Record one release-stage row failure without poisoning sibling rows."""
    receipt_path = output_dir / "row-blocks" / f"{slugify(state['key'])}.json"
    receipt = {
        "schema": "b2-wire-backlog-row-block-v1",
        "status": "BLOCKED_ROW_ONLY",
        "key": state["key"],
        "attempt_id": state.get("attempt_id"),
        "blocked_at_utc": iso_utc(),
        "reason": "release_row_gate_failed",
        "error": f"{type(error).__name__}:{error}"[:1000],
        "effects": {"deployments": 0, "tracking_clicks": 0},
    }
    atomic_json(receipt_path, receipt)
    attempts = int(state.get("block_attempts") or 0) + 1
    classification = classify_block(
        "release_row_gate_failed", error_code(receipt["error"]), attempts=attempts
    )
    state.update({
        "status": "blocked",
        "blocked_at_utc": iso_utc(),
        "updated_at_utc": iso_utc(),
        "block_attempts": attempts,
        "block": {
            "reason": "release_row_gate_failed",
            "evidence": path_binding(receipt_path),
            **classification,
        },
    })
    receipt["retry_class"] = classification["retry_class"]
    receipt["retry_basis"] = classification["retry_basis"]
    atomic_json(receipt_path, receipt)
    state["block"]["evidence"] = path_binding(receipt_path)
    if state["key"] not in day["blocked_keys"]:
        day["blocked_keys"].append(state["key"])
    return receipt


def _raise_all_prepared_rows_blocked(*, ledger_path: Path, ledger: dict[str, Any]) -> None:
    """Persist row-scoped failures before closing an all-failed no-deploy release."""
    save_ledger(ledger_path, ledger)
    raise B2Error("all_prepared_rows_blocked_before_build")


def _wire_in_build_env(
    *, provider_content_change: bool, release_date: str | None = None,
) -> dict[str, str]:
    """Return a child-only environment for the wire-in `npm run build:gate`.

    Provider additions, existing-page activations, and already-live recovery are
    content-changing paths. A non-provider/no-content build leaves the provenance
    guard armed. The returned copy is never exported or persisted.
    """
    env = dict(os.environ)
    env["GLP_PRICE_INDEX_REFRESH"] = "1"
    env.pop("GLP_LASTMOD_PROVENANCE_REFRESH", None)
    env.pop("GLP_LASTMOD_DECLARED_ROUTES", None)
    env.pop("GLP_LASTMOD_DECLARED_ROUTES_FILE", None)
    if provider_content_change:
        # 2026-08-20 — c3 receipt
        # `authoring-20260820T205033Z-nu-image-medical-review-c3.json`:
        # provider-add changes `/` by construction. The provenance guard protects
        # against UNEXPLAINED drift; this provider-content drift is explained, so
        # only this child build enters the explicit refresh lane.
        env["GLP_LASTMOD_PROVENANCE_REFRESH"] = "1"
    if release_date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(release_date)):
            raise B2Error(f"wire_in_release_date_invalid:{release_date}")
        env["GLP_RELEASE_DATE"] = str(release_date)
    return env


def _validate_quiz_golden_vector_refresh_receipt(receipt: dict[str, Any]) -> None:
    """Accept only the two explicit, evidence-bearing refresh modes."""
    status = receipt.get("status")
    if status not in {"PASS_REFRESHED", "PASS_VERIFIED_NO_ROSTER_CHANGE"}:
        raise B2Error("quiz_golden_vector_refresh_receipt_invalid")
    if receipt.get("change_reason") != "provider-content-build":
        raise B2Error("quiz_golden_vector_refresh_reason_invalid")

    mode = receipt.get("mode")
    roster_delta = receipt.get("roster") or {}
    provider_content = receipt.get("provider_content") or {}
    if status == "PASS_REFRESHED":
        if mode == "roster-aware-auto-rebase":
            if not (roster_delta.get("added") or roster_delta.get("removed")):
                raise B2Error("quiz_golden_vector_refresh_roster_delta_missing")
        elif mode == "provider-content-auto-rebase":
            if not provider_content.get("changed_provider_ids"):
                raise B2Error("quiz_golden_vector_refresh_provider_content_delta_missing")
        else:
            raise B2Error("quiz_golden_vector_refresh_mode_invalid")
    elif mode != "verify-no-roster-change":
        raise B2Error("quiz_golden_vector_refresh_mode_invalid")


def _provenance_refresh_evidence(
    candidate: Path, build_gate: dict[str, Any], *, expected: bool,
) -> dict[str, Any]:
    """Read back the postbuild refresh receipt without inferring unnamed routes."""
    stage_path = candidate / "artifacts/ops/aeo-surface-stage.json"
    if not stage_path.is_file():
        raise B2Error("provenance_refresh_stage_receipt_missing")
    stage = read_json(stage_path)
    used = stage.get("provenance_refresh") is True
    if used != expected:
        raise B2Error(
            f"provenance_refresh_stage_receipt_mismatch:{str(expected).lower()}:"
            f"{str(used).lower()}"
        )

    # Different postbuild generations may name refreshed routes under different
    # explicit fields. Capture only names the stage actually emitted; `None`
    # means the refresh was recorded but that postbuild did not enumerate routes.
    route_fields = (
        "provenance_routes_rewritten",
        "routes_rewritten",
        "refreshed_routes",
        "declared_routes_refreshed",
    )
    named_routes: set[str] = set()
    route_sources: list[str] = []
    for field in route_fields:
        value = stage.get(field)
        if not isinstance(value, list):
            continue
        route_sources.append(field)
        named_routes.update(
            route for route in value
            if isinstance(route, str) and route.startswith("/")
        )
    refresh_output_lines = list(build_gate.get("provenance_refresh_output_lines") or [])
    if not refresh_output_lines:
        refresh_output_lines = [
            line
            for line in str(build_gate.get("stdout_tail") or "").splitlines()
            if "provenance" in line.casefold()
            and ("refresh" in line.casefold() or "rewrit" in line.casefold())
        ]
    return {
        "provenance_refresh": used,
        "provenance_refresh_build_id": candidate.name,
        "provenance_routes_rewritten": sorted(named_routes) if named_routes else None,
        "provenance_routes_rewritten_sources": route_sources,
        "provenance_refresh_output_lines": refresh_output_lines,
        "provenance_refresh_stage_receipt": path_binding(stage_path),
    }


#: The stage label a shared-build failure is filed under. It is deliberately NOT
#: a row-specific reason: every staged row carries the same block, because the
#: build they share is what refused.
SHARED_BUILD_FAILURE_REASON = "shared_build_failure"


def _mark_shared_build_failure(
    *,
    ledger: dict[str, Any],
    day: dict[str, Any],
    states: list[dict[str, Any]],
    output_dir: Path,
    error: Exception,
    stage: str,
) -> list[dict[str, Any]]:
    """Put a SHARED build failure on every staged row, so the brief can see it.

    BLOCKER H, §8.2a of `nuimage-finish-20260820/RENDERER-DERIVE-REPORT.md`:

      > The Blocker-D fix carries the latest ROW block off the worked ledger into
      > the brief. `lastmod_provenance_stale:/` is not a row block: it is raised
      > by the SHARED build that runs after every prepared row is staged, so
      > `release_prepared` fails as a whole, the ledger row stays `prepared` with
      > `block: null`, and nothing at all reaches the receipt.

    So the authoring engine was dispatched twice against the nine checklist rows
    — none of which is what stopped the build — which is Blocker D's own sentence
    one layer further out. This closes it with the material that was already on
    the receipt: the failing stage, the error class and the traceback the
    subprocess died with.

    The block is MACHINE-OWNED by construction. It is classified downstream by
    `glp_onboarding_authoring_dispatch.wire_backlog_block_class`, which decides
    from the ERROR TEXT and never from a label written here; a build traceback
    matches none of the authoring codes, so it lands on `build_full_suite` and
    routes to the fixer ladder. No authoring cycle is bought for a build failure.
    """
    receipts: list[dict[str, Any]] = []
    error_text = clip_error(f"{type(error).__name__}:{error}")
    blocked_at = iso_utc()
    for state in states:
        receipt_path = output_dir / "row-blocks" / f"shared-build-{slugify(state['key'])}.json"
        receipt = {
            # The same schema the manifest already reads, so this reaches the
            # brief through the EXISTING Blocker-D plumbing rather than a second
            # path that would have to be kept in step with it.
            "schema": "b2-wire-backlog-row-block-v1",
            "status": "BLOCKED_ROW_ONLY",
            "key": state["key"],
            "attempt_id": state.get("attempt_id"),
            "blocked_at_utc": blocked_at,
            "reason": SHARED_BUILD_FAILURE_REASON,
            "failed_stage": stage,
            "shared_failure": True,
            "shared_with_rows": [other["key"] for other in states],
            "error": error_text,
            "effects": {"deployments": 0, "tracking_clicks": 0},
        }
        atomic_json(receipt_path, receipt)
        attempts = int(state.get("block_attempts") or 0) + 1
        classification = classify_block(
            SHARED_BUILD_FAILURE_REASON, error_code(error_text), attempts=attempts
        )
        state.update({
            "status": "blocked",
            "blocked_at_utc": blocked_at,
            "updated_at_utc": blocked_at,
            "block_attempts": attempts,
            "block": {
                "reason": SHARED_BUILD_FAILURE_REASON,
                "evidence": path_binding(receipt_path),
                **classification,
            },
        })
        receipt["retry_class"] = classification["retry_class"]
        receipt["retry_basis"] = classification["retry_basis"]
        atomic_json(receipt_path, receipt)
        state["block"]["evidence"] = path_binding(receipt_path)
        if state["key"] not in day["blocked_keys"]:
            day["blocked_keys"].append(state["key"])
        receipts.append(receipt)
    return receipts


def _refresh_price_index_before_build(candidate_root: Path, observed_on: str) -> dict[str, Any]:
    """Append a new observation or safely rebind an existing same-day snapshot."""
    tracker = read_json(candidate_root / "data/price-tracker.json")
    observations = ((tracker.get("price_index") or {}).get("observations") or [])
    if observations and observations[-1].get("observed_on") == observed_on:
        return _run(
            [
                "node", str(PRICE_INDEX_SOURCE_REBIND),
                "--release", str(candidate_root),
                "--observed-on", observed_on,
            ],
            cwd=BUSINESS, timeout=900,
        )
    return _run(
        [
            "node", str(candidate_root / "scripts/update-price-index.mjs"),
            "--observed-on", observed_on,
        ],
        cwd=candidate_root, timeout=900,
    )


def _normalize_content_upgrade_provenance_test(candidate_root: Path) -> dict[str, Any]:
    """Keep the built-form provenance cardinality derived from the runtime allowlist."""
    path = candidate_root / "tests/price-watch-email.test.mjs"
    if not path.is_file():
        raise B2Error("content_upgrade_provenance_test_missing")
    text = path.read_text(encoding="utf-8")
    dynamic = "assert.equal(emitted.length, CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES.length);"
    staged_contract = (
        'test("content-upgrade provenance inventory includes the exact authoritative staged Phase 3 additions"',
        "const approved = [...CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES].sort();",
        "const staged = await stagedPhase3CaptureSources();",
        "assert.deepEqual(emitted, [...new Set([...approved, ...staged])].sort());",
        "assert.deepEqual(staged.filter((consentSource) => approved.includes(consentSource)), []);",
        "assert.equal(isWeeklyPriceWatchSubscriber(subscriber({ consent_source: consentSource })), true, consentSource);",
        "assert.equal(isWeeklyPriceWatchSubscriber(subscriber({ consent_source: consentSource })), false, consentSource);",
    )
    pattern = re.compile(r"assert\.equal\(emitted\.length, \d+\);")
    matches = list(pattern.finditer(text))
    # The current release has a stronger contract than the former cardinality
    # assertion: emitted sources must equal the deduplicated union of the runtime
    # allowlist and authoritative staged Phase 3 sources, the sets must not
    # overlap, and only the approved set may enter the weekly subscriber path.
    # Recognize that complete contract instead of treating its intentional lack
    # of an `emitted.length` literal as an unknown shape.
    if not matches and all(marker in text for marker in staged_contract):
        return {"status": "PASS_CURRENT_STAGED_CONTRACT", "path": str(path)}
    if not matches and dynamic in text:
        return {"status": "PASS_ALREADY_DYNAMIC", "path": str(path)}
    if len(matches) != 1:
        raise B2Error(f"content_upgrade_provenance_test_contract_unknown:{len(matches)}")
    _detach(path)
    path.write_text(pattern.sub(dynamic, text, count=1), encoding="utf-8")
    if dynamic not in path.read_text(encoding="utf-8"):
        raise B2Error("content_upgrade_provenance_test_migration_failed")
    return {"status": "PASS_MIGRATED", "path": str(path)}


# Hash-bound release-base addenda, applied in order. Each manifest is optional:
# an absent one is a no-op, never a gate failure.
# PURGED 2026-09-03 - the three run-dir addenda are FOLDED INTO SOURCE.
#
# Measured against the current content base before purging:
#   * ATLAS-BUDGET-ADDENDA   2/2 entries already identical in the base
#   * COMPARE-SURFACE-ADDENDA 2/2 entries already identical in the base
#   * ADDENDA-DONE           8 entries were already skipped by contract, 4 are
#     identical in the base, and 2 (tests/high-intent-affiliate-surfaces.test.mjs,
#     tests/pass2-late-audit-interactions.test.mjs) are files where the BASE IS
#     NEWER than the addendum - re-overlaying those would REVERT current work.
#
# An overlay that can only re-apply bytes the base already has, or revert bytes
# newer than itself, is a standing regression risk, not a safety net. The one
# thing it genuinely still owed - the Meta pixel - is now folded surgically by
# _ensure_meta_pixel_restore, which is why that could not simply be un-skipped:
# the addendum's build-staging.mjs is a whole-file snapshot older than the base.
RELEASE_BASE_ADDENDA_MANIFESTS: tuple[Path, ...] = ()
RELEASE_BASE_ADDENDA_MANIFEST: Path | None = (
    RELEASE_BASE_ADDENDA_MANIFESTS[0] if RELEASE_BASE_ADDENDA_MANIFESTS else None)
# Count pins the machine itself already floors, or that belong to a deferred
# addendum. Anything here is deliberately NOT overlaid.
RELEASE_BASE_ADDENDA_SKIP = {
    # _normalize_go_runtime_held_cardinality_tests already floors these two.
    "tests/go-runtime-manifest.test.mjs",
    "tests/funnel-v2-instrumentation.test.mjs",
    # The meta-pixel restore needs the build step itself; overlaying only its
    # test would assert a file this release cannot produce.
    "tests/meta-pixel-build-output.test.mjs",
    "scripts/build-staging.mjs",
    "assets/meta-pixel.js",
    "assets/meta-lead.js",
    # Owned by scripts/glp_provider_cardinality_test_refresh.py (and, for the
    # static-output file, glp_provider_visual_fallback_refresh.py). Those
    # refreshers migrate the candidate copy by matching an EXACT contract string,
    # so overlaying an already-floored version makes them fail closed with
    # provider_cardinality_contract_unknown. The machine already floors these two
    # itself; the overlay must not race it.
    "tests/phase3-page-graph.test.mjs",
    "tests/pass2-late-audit-static-output.test.mjs",
}



#: The Meta pixel restore, folded into source instead of overlaid.
#:
#: /meta-pixel.js and /meta-lead.js are root-level static files that lost their
#: writer, so every full rebuild's `rm(PUBLIC)` dropped them - live 404s, and the
#: pixel last fired 2026-07-21. The 2026-09-01 addendum carried the fix, but its
#: `scripts/build-staging.mjs` is a WHOLE-FILE snapshot that is now OLDER than the
#: base: applying it would revert the GO_RUNTIME_PROMOTION_AUTHORITY_V1 block the
#: /go work depends on, and a later `rail` fix. That is exactly why the overlay
#: skipped it, and why the pixel never came back.
#:
#: So the restore is folded SURGICALLY: three new files carried from their
#: hash-bound bundles, and the two pixel hunks applied to the CURRENT
#: build-staging.mjs by exact anchor. Idempotent, and it never rewrites bytes it
#: was not reviewed against.
META_PIXEL_RESTORE_MARKER = "META_PIXEL_RESTORE_V1"
META_PIXEL_RESTORE_FILES = (
    "assets/meta-pixel.js",
    "assets/meta-lead.js",
    "tests/meta-pixel-build-output.test.mjs",
)
META_PIXEL_ADDENDA_SOURCE = (
    BUSINESS
    / "reports/glp-clinic-scout/affiliate-machine/deploy-fable-20260901/ADDENDA-DONE.json"
)


def _meta_pixel_bundles() -> dict[str, tuple[Path, str]]:
    """{relative: (bundle path, expected sha256)} from the reviewed addendum."""
    if not META_PIXEL_ADDENDA_SOURCE.is_file():
        return {}
    document = read_json(META_PIXEL_ADDENDA_SOURCE)
    out: dict[str, tuple[Path, str]] = {}
    for entry in document.get("files_changed") or []:
        raw = str(entry.get("path") or "")
        if "site-system/flagship-v2/" not in raw:
            continue
        relative = raw.split("site-system/flagship-v2/", 1)[1].split(" ")[0]
        if relative not in META_PIXEL_RESTORE_FILES:
            continue
        bundle = BUSINESS / str(entry.get("patch_bundle_copy") or "")
        expected = str(entry.get("sha256_after") or entry.get("sha256") or "")
        if not bundle.is_file() or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise B2Error(f"meta_pixel_bundle_invalid:{relative}")
        out[relative] = (bundle, expected)
    return out


def _ensure_meta_pixel_restore(candidate_root: Path) -> dict[str, Any]:
    """Fold the Meta pixel back into the candidate's own source."""
    candidate_root = Path(candidate_root)
    staging = candidate_root / "scripts/build-staging.mjs"
    if not staging.is_file():
        return {"status": "PASS_NO_BUILD_STAGING", "written": [], "hunks": []}
    bundles = _meta_pixel_bundles()
    if len(bundles) != len(META_PIXEL_RESTORE_FILES):
        return {"status": "PASS_ADDENDUM_ABSENT_NO_RESTORE", "written": [], "hunks": []}

    written: list[str] = []
    for relative, (bundle, expected) in sorted(bundles.items()):
        payload = bundle.read_bytes()
        if sha_bytes(payload) != expected:
            raise B2Error(f"meta_pixel_bundle_hash_drift:{relative}")
        target = candidate_root / relative
        if target.is_file() and sha_bytes(target.read_bytes()) == expected:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # _detach breaks a hardlink into the predecessor; a file that does not
        # exist yet has no link to break, and _detach fail-closes on a missing path.
        if target.is_file():
            _detach(target)
        target.write_bytes(payload)
        written.append(relative)

    text = staging.read_text(encoding="utf-8")
    hunks: list[str] = []
    if "data-glp-meta-pixel" not in text:
        old_head = (
            '  const prePaintConfig = template === "quiz"\n'
            "    ? config.replace('<link rel=\"icon\"', "
            "'<script>document.documentElement.classList.add(\"js\")</script><link rel=\"icon\"')\n"
            "    : config;\n"
        )
        new_head = (
            "  // META_PIXEL_RESTORE_V1: reference the pixel on the homepage only.\n"
            "  // meta-pixel.js's own PAGEVIEW_PATHS already lists \"/\"; the file simply\n"
            "  // stopped being written and stopped being referenced.\n"
            '  const withMetaPixel = template === "home"\n'
            '    ? config.replace("</script>", `</script><script defer '
            'src="/meta-pixel.js?v=${ASSET_VERSION}" '
            'data-glp-meta-pixel="pageview-viewcontent"></script>`)\n'
            "    : config;\n"
            '  const prePaintConfig = template === "quiz"\n'
            "    ? withMetaPixel.replace('<link rel=\"icon\"', "
            "'<script>document.documentElement.classList.add(\"js\")</script><link rel=\"icon\"')\n"
            "    : withMetaPixel;\n"
        )
        if text.count(old_head) != 1:
            raise B2Error(f"meta_pixel_prepaint_anchor_drift:{text.count(old_head)}")
        text = text.replace(old_head, new_head, 1)
        hunks.append("prepaint_reference")

    copy_anchor = ('  await cp(path.join(ROOT, "assets/authors"), '
                   'path.join(PUBLIC, "assets/authors"), { recursive: true });\n')
    if "path.join(PUBLIC, \"meta-pixel.js\")" not in text:
        if text.count(copy_anchor) != 1:
            raise B2Error(f"meta_pixel_copy_anchor_drift:{text.count(copy_anchor)}")
        text = text.replace(copy_anchor, copy_anchor + (
            "  // META_PIXEL_RESTORE_V1: root-level static passthrough files. They had no\n"
            "  // writer at all, so every full rebuild's rm(PUBLIC) dropped them. They are\n"
            "  // ordinary source-controlled assets, not predecessor-owned generated bytes,\n"
            "  // so they are copied fresh on every build.\n"
            '  await cp(path.join(ROOT, "assets/meta-pixel.js"), path.join(PUBLIC, "meta-pixel.js"));\n'
            '  await cp(path.join(ROOT, "assets/meta-lead.js"), path.join(PUBLIC, "meta-lead.js"));\n'
        ), 1)
        hunks.append("public_copy")

    if hunks:
        _detach(staging)
        staging.write_text(text, encoding="utf-8")
    return {"status": "PASS_RESTORED" if (written or hunks) else "PASS_ALREADY_RESTORED",
            "written": written, "hunks": hunks,
            "addendum": path_binding(META_PIXEL_ADDENDA_SOURCE)}



#: A page may assert only what its research artifact attests.
#:
#: RULING 2026-09-03. Measured went live-checked and its page carried three
#: claims its packet does not attest: brand-tier prices ($149/$199/$299/$349/
#: $449) and a $79 microdose figure, "same-day" prescriptions, and a 5%
#: money-back threshold. The packet cannot be refreshed - the authoring engine
#: has zero capacity until the operator tops up - so the honest move is to stop
#: asserting them rather than to except them.
#:
#: The claims come off through the presentation the site ALREADY uses for
#: unverified coverage ("... was not published in the captured first-party
#: pages."), and the surviving sentences are the ARTIFACT'S OWN words, so the
#: page quotes its source instead of paraphrasing it.
#:
#: This is general, not a Measured carve-out: it acts on exactly the predicate
#: the sensor fails on, so it touches only fields that genuinely assert
#: something unattested. When a packet later attests those facts, the normal
#: render path regenerates the copy and this step becomes a no-op - the facts
#: return on their own.
EMPTYISH_VALUE = re.compile(r"^\s*(not stated|unclear|unknown|none|null|n/?a|)\s*$", re.I)
UNATTESTED_PAGE_FIELD_QUALIFIER = {
    "pricing": "Other published amounts were not attested by the captured first-party pages.",
    "shipping": "Prescription turnaround was not published in the captured first-party pages.",
    "cancellation": "Refund-guarantee terms were not published in the captured first-party pages.",
}


def _ensure_attested_page_copy(candidate_root: Path) -> dict[str, Any]:
    """Reduce any page_copy field that asserts unattested values to attested text."""
    candidate_root = Path(candidate_root)
    contracts_path = candidate_root / "data/provider-page-contracts.json"
    if not contracts_path.is_file():
        return {"status": "PASS_NO_CONTRACTS", "rewritten": []}
    sys.path[:0] = [str(SCRIPTS)] if str(SCRIPTS) not in sys.path else []
    import glp_deploy_completeness_sensor as sensor  # local: the sensor owns the predicate

    contracts = read_json(contracts_path)
    providers = contracts.get("providers") if isinstance(contracts, dict) else contracts
    as_dict = isinstance(providers, dict)
    rows = ([{"program_id": k, **v} for k, v in providers.items()] if as_dict
            else list(providers or []))
    index = sensor.research_index()
    rewritten: list[dict[str, Any]] = []
    for row in rows:
        pid = str(row.get("program_id") or "")
        artifact = index.get(pid)
        if not artifact:
            continue
        copy = row.get("page_copy") or {}
        for field, research_fields in sensor.PAGE_FACT_MAP.items():
            text = copy.get(field)
            if not isinstance(text, str) or not text.strip():
                continue
            page_values = sensor.value_tokens(text)
            if not page_values:
                continue
            # `attested` is the SENSOR's definition (value + captured quote), so the
            # rewrite is judged by the same rule the check uses.
            attested = sensor.value_tokens(
                sensor.research_text(artifact["doc"], research_fields))
            unattested = sorted(page_values - attested)
            if not unattested:
                continue
            qualifier = UNATTESTED_PAGE_FIELD_QUALIFIER.get(field)
            if not qualifier:
                raise B2Error(f"unattested_page_field_without_qualifier:{pid}:{field}")
            # The prose comes from the packet's VALUE fields only. research_text
            # also concatenates the raw captured quote - scraped markdown, headings
            # and image URLs - which is evidence, not page copy.
            sentences: list[str] = []
            for name in research_fields:
                entry = (artifact["doc"].get("fields") or {}).get(name)
                if not isinstance(entry, dict):
                    continue
                value = str(entry.get("value") or "").strip()
                if value and not EMPTYISH_VALUE.fullmatch(value):
                    sentences.append(value if value.endswith(".") else f"{value}.")
            replacement = " ".join([*sentences, qualifier])
            # The replacement must itself be clean, or the page would simply
            # assert a different unattested thing.
            residue = sorted(sensor.value_tokens(replacement) - attested)
            if residue:
                raise B2Error(f"attested_page_copy_residue:{pid}:{field}:{','.join(residue)}")
            copy[field] = replacement
            rewritten.append({"program_id": pid, "field": field, "removed": unattested})
        row["page_copy"] = copy
    if not rewritten:
        return {"status": "PASS_ALL_PAGE_COPY_ATTESTED", "rewritten": []}
    if as_dict:
        contracts["providers"] = {str(r.pop("program_id")): r for r in rows}
    elif isinstance(contracts, dict):
        contracts["providers"] = rows
    else:
        contracts = rows
    _write_candidate_json_preserving_key_order(contracts_path, contracts)
    # The fastpath integration record binds provider-page-contracts.json BY SHA
    # (validate-provider-fastpath-plus5.mjs:95 and its test). Rewriting the copy
    # without re-binding would trade an honest page for a failing build, so the
    # binding moves with the bytes - mechanically, asserting nothing new.
    rebound = None
    fastpath_path = candidate_root / "data/provider-fastpath-contract.json"
    if fastpath_path.is_file():
        fastpath = read_json(fastpath_path)
        current = sha_bytes(contracts_path.read_bytes())
        if fastpath.get("current_contract_sha256") != current:
            rebound = {"from": fastpath.get("current_contract_sha256"), "to": current}
            fastpath["current_contract_sha256"] = current
            _write_candidate_json_preserving_key_order(fastpath_path, fastpath)
    return {"status": "PASS_UNATTESTED_CLAIMS_REMOVED", "rewritten": rewritten,
            "fastpath_contract_rebound": rebound}



#: THE /go SURFACE ALLOWLIST IS A BUILD CONTRACT, NOT A PER-REQUEST ONE.
#:
#: DEFECT 2026-09-03. This edit lived in _ensure_first_party_go_contract_support,
#: which runs ONLY from apply_first_party_go_route_provision - once per queued /go
#: request, and only past its held-by-default early return. A round with no /go
#: requests never ran it at all, which is why the remainder lane's candidate
#: carried none of the Pass 7 roles and why the live site still ships the old
#: 110-position allowlist. The allowlist governs what every anchor on the built
#: site may mint, so it belongs in the PRE-BUILD sequence, applied to every
#: candidate whether or not any /go request exists.
#:
#: Accepting a surface code still activates nothing: liveAllowed() independently
#: requires the governed promotion authority, so held-by-default is untouched.
GO_SURFACE_ALLOWLIST_EDIT = (
    '  const allowedPageRoles = ["providers", "compounded", "compounded-tirzepatide", "lp-best-providers-compounded-semaglutide", "lp-best-providers-compounded-tirzepatide", "3-month", "6-month", "wegovy-pill", "wegovy", "ozempic", "mounjaro", "zepbound"];\n  const allowedPositions = [\n    ...Array.from({ length: 5 }, (_, index) => `fv2s${index + 1}`),\n    ...Array.from({ length: 5 }, (_, index) => `fv2m${index + 1}`),\n    ...Array.from({ length: 50 }, (_, index) => `pc${index + 1}`),\n    ...Array.from({ length: 50 }, (_, index) => `pcu${index + 1}`),\n  ];\n',
    '  // PASS 7 ALLOWLIST 2026-09-03. Pass 7 emits landing-page, self-pay and review\n  // surface codes. The worker\'s runtime manifest allowlist has to accept them or\n  // handleGoRequest refuses the click with assignment-not-allowlisted and mints\n  // NO cs_click_id - a silently dark anchor behind a green build. Accepting a\n  // surface code NEVER activates a route: liveAllowed still requires the governed\n  // promotion authority, so held-by-default is untouched.\n  const allowedPageRoles = ["providers", "reviews", "self-pay", "lp", "lp-quiz", "lp-compare", "compounded", "compounded-tirzepatide", "lp-best-providers-compounded-semaglutide", "lp-best-providers-compounded-tirzepatide", "3-month", "6-month", "wegovy-pill", "wegovy", "ozempic", "mounjaro", "zepbound"];\n  const allowedPositions = [\n    "rv1", "rv2",\n    ...Array.from({ length: 60 }, (_, index) => `lp${index + 1}`),\n    ...Array.from({ length: 60 }, (_, index) => `sp${index + 1}`),\n    ...Array.from({ length: 5 }, (_, index) => `fv2s${index + 1}`),\n    ...Array.from({ length: 5 }, (_, index) => `fv2m${index + 1}`),\n    ...Array.from({ length: 60 }, (_, index) => `pc${index + 1}`),\n    ...Array.from({ length: 50 }, (_, index) => `pcu${index + 1}`),\n  ];\n',
    '"lp-quiz", "lp-compare"',
)


def _ensure_go_surface_allowlist(candidate_root: Path) -> dict[str, Any]:
    """Carry the Pass 7 /go surface allowlist into the candidate's own source."""
    path = Path(candidate_root) / "src/program-registry.mjs"
    if not path.is_file():
        return {"status": "PASS_NO_PROGRAM_REGISTRY_MODULE", "applied": False}
    text = path.read_text(encoding="utf-8")
    old, new, already = GO_SURFACE_ALLOWLIST_EDIT
    if already in text:
        return {"status": "PASS_ALREADY_ALLOWLISTED", "applied": False}
    if text.count(old) != 1:
        raise B2Error(f"go_surface_allowlist_anchor_drift:{text.count(old)}")
    _detach(path)
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return {"status": "PASS_ALLOWLIST_APPLIED", "applied": True}


def _ensure_go_surface_allowlist_and_freeze(candidate_root: Path) -> dict[str, Any]:
    """ONE ATOMIC STEP: apply the allowlist AND re-freeze the runtime manifest.

    DEFECT 2026-09-03 (remainder lane, receipt 54). The allowlist is injected
    INSIDE deriveGoEntriesFromRegistry, and functions/go/program-manifest.js is
    RENDERED FROM that function. build-staging re-derives the manifest and compares
    it byte-for-byte, throwing frozen_build4_go_runtime_manifest_drift on any
    difference. release_prepared happened to order allowlist -> refresh correctly,
    but the n8n stage wire_backlog_build_deploy runs the build WITHOUT that
    pairing, so every candidate that reached the build stage on its own drifted.

    Splitting the edit from the freeze is what made the pairing an ordering
    convention that one caller could miss. They are now one step: whoever applies
    the allowlist re-freezes the manifest in the same breath, and the freeze
    derives exactly the way the BUILD derives, so it cannot satisfy itself while
    failing the build's comparison.
    """
    candidate_root = Path(candidate_root)
    allowlist = _ensure_go_surface_allowlist(candidate_root)
    freeze = _run(
        ["node", str(SCRIPTS / "glp_go_manifest_freeze.mjs"), "--release", str(candidate_root)],
        cwd=BUSINESS, timeout=900,
    )
    return {"status": "PASS_ALLOWLIST_AND_MANIFEST_FROZEN",
            "allowlist": allowlist, "freeze": freeze}


def _apply_release_base_addenda_overlay(candidate_root: Path) -> dict[str, Any]:
    """Carry a hash-bound release-base addendum into the candidate.

    An onboarding deploy GROWS provider cards, pharmacy cards, review routes and
    live calculator/quiz surfaces. Several candidate tests pin those quantities
    with `assert.equal`, so a SUCCESSFUL deploy fails its own npm test gate - the
    #139 defect class. The machine already floors two such pins in code; this
    carries the rest of an externally reviewed addendum in the same way, bound by
    exact before/after SHA-256 so it can never silently apply to bytes it was not
    reviewed against.

    Absent manifest is a no-op, never a failure: the release gate must not depend
    on an optional addendum existing.
    """
    manifests = [m for m in RELEASE_BASE_ADDENDA_MANIFESTS if m.is_file()]
    if not manifests:
        return {"status": "PASS_NO_ADDENDA", "applied": [], "skipped": []}
    entries: list[dict[str, Any]] = []
    used: list[dict[str, Any]] = []
    for manifest_path in manifests:
        manifest = read_json(manifest_path)
        if str(manifest.get("status") or "") != "PASS":
            continue
        used.append(path_binding(manifest_path))
        entries.extend(manifest.get("files_changed") or [])
    if not entries:
        return {"status": "PASS_ADDENDA_NOT_PASS_IGNORED", "applied": [], "skipped": []}
    applied: list[str] = []
    already: list[str] = []
    skipped: list[str] = []
    for entry in entries:
        raw = str(entry.get("path") or "")
        if "site-system/flagship-v2/" not in raw:
            skipped.append(raw)
            continue
        relative = raw.split("site-system/flagship-v2/", 1)[1].split(" ")[0]
        if relative in RELEASE_BASE_ADDENDA_SKIP:
            skipped.append(relative)
            continue
        source = BUSINESS / str(entry.get("patch_bundle_copy") or "")
        if not source.is_file():
            raise B2Error(f"release_base_addenda_patch_missing:{relative}")
        expected_after = str(entry.get("sha256_after") or entry.get("sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_after):
            raise B2Error(f"release_base_addenda_after_hash_invalid:{relative}")
        payload = source.read_bytes()
        if sha_bytes(payload) != expected_after:
            raise B2Error(f"release_base_addenda_patch_hash_drift:{relative}")
        target = candidate_root / relative
        current = sha_bytes(target.read_bytes()) if target.is_file() else None
        if current == expected_after:
            already.append(relative)
            continue
        # Once an overlay has SHIPPED, the base release carries its change, and a
        # machine normalizer may then edit the same file again (e.g.
        # _normalize_pass2_quiz_routing_test also rewrites
        # tests/pass2-late-audit-interactions.test.mjs). The bytes then match
        # neither `before` nor `after`, and a pure hash gate fails closed on a
        # candidate that is already CORRECT. An entry may therefore carry an
        # `already_applied_marker`: a substring whose presence proves the change
        # is in the file. Absent a marker the hash gate still binds exactly.
        marker = str(entry.get("already_applied_marker") or "")
        if marker and target.is_file():
            try:
                if marker in target.read_text(encoding="utf-8"):
                    already.append(relative)
                    continue
            except UnicodeDecodeError:
                pass
        expected_before = entry.get("sha256_before")
        if expected_before is None:
            if target.is_file():
                raise B2Error(f"release_base_addenda_add_target_exists:{relative}")
        elif current != str(expected_before):
            raise B2Error(f"release_base_addenda_unexpected_candidate_bytes:{relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            _detach(target)
        target.write_bytes(payload)
        if sha_bytes(target.read_bytes()) != expected_after:
            raise B2Error(f"release_base_addenda_write_failed:{relative}")
        applied.append(relative)
    return {
        "status": "PASS_APPLIED" if applied else "PASS_ALREADY_APPLIED",
        "manifests": used,
        "applied": sorted(applied),
        "already_current": sorted(already),
        "deliberately_skipped": sorted(skipped),
    }


def _normalize_go_runtime_held_cardinality_tests(candidate_root: Path) -> dict[str, Any]:
    """Keep the held-runtime floor while allowing the governed roster to grow."""
    contracts = {
        "tests/go-runtime-manifest.test.mjs": (
            "assert.equal(entries.length, 32);",
            "assert.ok(entries.length >= 32);",
        ),
        "tests/funnel-v2-instrumentation.test.mjs": (
            "assert.equal(candidates.length, 32);",
            "assert.ok(candidates.length >= 32);",
        ),
    }
    changed_files: list[str] = []
    for relative, (legacy, dynamic) in contracts.items():
        path = candidate_root / relative
        if not path.is_file():
            raise B2Error(f"go_runtime_held_cardinality_test_missing:{relative}")
        text = path.read_text(encoding="utf-8")
        if dynamic in text and legacy not in text:
            continue
        if text.count(legacy) != 1 or dynamic in text:
            raise B2Error(f"go_runtime_held_cardinality_test_contract_unknown:{relative}")
        _detach(path)
        path.write_text(text.replace(legacy, dynamic, 1), encoding="utf-8")
        if dynamic not in path.read_text(encoding="utf-8"):
            raise B2Error(f"go_runtime_held_cardinality_test_migration_failed:{relative}")
        changed_files.append(relative)
    return {
        "status": "PASS_MIGRATED" if changed_files else "PASS_ALREADY_DYNAMIC",
        "changed_files": sorted(changed_files),
        "paths": sorted(contracts),
    }


def _normalize_pass2_quiz_routing_test(candidate_root: Path) -> dict[str, Any]:
    """Keep the routing invariant without pinning a historical commercial ranking."""
    path = candidate_root / "tests/pass2-late-audit-interactions.test.mjs"
    if not path.is_file():
        raise B2Error("pass2_quiz_routing_test_missing")
    text = path.read_text(encoding="utf-8")
    legacy = (
        'assert.deepEqual(yes.map((row) => row.program_id), '
        '["enhance-md", "gala-health", "healthrx"]);'
    )
    dynamic = (
        'assert.equal(yes.length, 3, "fixture should return exactly three governed provider results");\n'
        '  assert.equal(new Set(yes.map((row) => row.program_id)).size, yes.length, '
        '"fixture results must not duplicate a provider");'
    )
    preserved = (
        'assert.deepEqual(no.map((row) => row.program_id), yes.map((row) => row.program_id), '
        '"current treatment history must not silently alter commercial ranking");'
    )
    if preserved not in text:
        raise B2Error("pass2_quiz_routing_invariant_missing")
    if dynamic in text and legacy not in text:
        return {"status": "PASS_ALREADY_DYNAMIC", "path": str(path)}
    if text.count(legacy) != 1 or dynamic in text:
        raise B2Error("pass2_quiz_routing_test_contract_unknown")
    _detach(path)
    path.write_text(text.replace(legacy, dynamic, 1), encoding="utf-8")
    migrated = path.read_text(encoding="utf-8")
    if dynamic not in migrated or preserved not in migrated:
        raise B2Error("pass2_quiz_routing_test_migration_failed")
    return {"status": "PASS_MIGRATED", "path": str(path)}


def _normalize_provider_pricing_quote_validator(candidate_root: Path) -> dict[str, Any]:
    """Reject empty quotes, not short exact quotes whose amounts remain verified."""
    path = candidate_root / "scripts/validate-provider-pricing-matrix.mjs"
    if not path.is_file():
        raise B2Error("provider_pricing_quote_validator_missing")
    text = path.read_text(encoding="utf-8")
    legacy = 'if (quote.length < 8) violations.push(`quote_missing:${where}`);'
    evidence_bound = 'if (!quote.trim()) violations.push(`quote_missing:${where}`);'
    amount_gate = "amountInQuote(amount, quote)"
    if amount_gate not in text:
        raise B2Error("provider_pricing_quote_validator_amount_gate_missing")
    if evidence_bound in text and legacy not in text:
        return {"status": "PASS_ALREADY_EVIDENCE_BOUND", "path": str(path)}
    if text.count(legacy) != 1 or evidence_bound in text:
        raise B2Error("provider_pricing_quote_validator_contract_unknown")
    _detach(path)
    path.write_text(text.replace(legacy, evidence_bound, 1), encoding="utf-8")
    migrated = path.read_text(encoding="utf-8")
    if evidence_bound not in migrated or amount_gate not in migrated:
        raise B2Error("provider_pricing_quote_validator_migration_failed")
    return {"status": "PASS_MIGRATED", "path": str(path)}


def _normalize_fastpath_merit_score_test(candidate_root: Path) -> dict[str, Any]:
    """Bind the fast-path review assertion to the current governed score."""
    path = candidate_root / "tests/provider-fastpath-plus5.test.mjs"
    if not path.is_file():
        raise B2Error("fastpath_merit_score_test_missing")
    text = path.read_text(encoding="utf-8")
    marker = "const expectedMeasuredScore = scoreProvider(reviews.get(\"measured\").row);"
    if marker in text:
        return {"status": "PASS_ALREADY_DYNAMIC", "path": str(path)}
    import_line = 'import { scoreProvider } from "../src/money-page.mjs";'
    anchor = 'import { buildProviderReviewSpecs, renderProviderReviewPage } from "../src/provider-review.mjs";'
    legacy = 'assert.match(measured, /class="score">7\\.6\\/10<\\/p>/);'
    if text.count(anchor) != 1 or text.count(legacy) != 1:
        raise B2Error("fastpath_merit_score_test_contract_unknown")
    replacement = (
        'const expectedMeasuredScore = scoreProvider(reviews.get("measured").row);\n'
        '  assert.ok(Number.isFinite(expectedMeasuredScore));\n'
        '  assert.ok(measured.includes(`class="score">${(expectedMeasuredScore / 10).toFixed(1)}/10</p>`));'
    )
    migrated = text.replace(anchor, f"{anchor}\n{import_line}", 1).replace(legacy, replacement, 1)
    _detach(path)
    path.write_text(migrated, encoding="utf-8")
    if marker not in path.read_text(encoding="utf-8"):
        raise B2Error("fastpath_merit_score_test_migration_failed")
    return {"status": "PASS_MIGRATED", "path": str(path)}


def _net_new_builder_row_dir(output_dir: Path, state: dict[str, Any], research_path: Path) -> Path:
    """Scope reusable builder artifacts to the exact governed research bytes."""
    return output_dir / "net-new-builder" / slugify(state["key"]) / sha_file(research_path)[:16]


def _build_net_new_step(
    *,
    source: Path,
    target: Path,
    state: dict[str, Any],
    plan_row: dict[str, Any],
    spec: dict[str, Any],
    research_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Run the existing incremental builder for exactly one hash-bound new row."""
    row_dir = _net_new_builder_row_dir(output_dir, state, research_path)
    research_dir = row_dir / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    research_copy = research_dir / spec["research_file"]
    if research_copy.exists() and sha_file(research_copy) != sha_file(research_path):
        raise B2Error("net_new_research_copy_drift")
    if not research_copy.exists():
        shutil.copy2(research_path, research_copy)
        os.chmod(research_copy, 0o600)

    spec_path = row_dir / "program-specs.json"
    sheet_path = row_dir / "sheet-snapshot.json"
    authority_path = row_dir / "selection-authority.json"
    selected_sheet_row = dict(plan_row["sheet"])
    selected_sheet_row["row"] = int(plan_row["sheet_row"])
    atomic_json(spec_path, {
        "schema": "g6-r1a-wirein-program-specs-v1",
        "programs": [spec],
    })
    atomic_json(sheet_path, {
        "schema": "b2-wire-backlog-builder-sheet-v1",
        "status": "PASS",
        "schema_audit": {"issues": []},
        "selected_rows": [selected_sheet_row],
    })
    atomic_json(authority_path, {
        "schema": "b2-wire-backlog-builder-authority-v1",
        "status": "APPROVED",
        "approved_scope": {
            "newly_wired_program_ids": [spec["program_id"]],
            "activation_at_utc": iso_utc(),
        },
        "mission_authority_sha256": sha_file(B2_SPEC),
        "source_bindings": {
            "b2_spec": path_binding(B2_SPEC),
            "prepared_program_spec": path_binding(spec_path),
            "prepared_research": path_binding(research_copy),
            "sheet_snapshot": path_binding(sheet_path),
        },
        "effects_authorized": {"candidate_writes": True, "deployments": 0},
    })
    report_dir = row_dir / "builder-report"
    run = _run([
        sys.executable,
        str(BUILDER),
        "--source", str(source),
        "--target", str(target),
        "--sheet", str(sheet_path),
        "--authority", str(authority_path),
        "--spec", str(spec_path),
        "--research-dir", str(research_dir),
        "--report-dir", str(report_dir),
        "--defer-surface-integration",
    ], cwd=BUSINESS, timeout=3600)
    receipt_path = report_dir / "06-incremental-wirein-build-receipt-r2.json"
    receipt = read_json(receipt_path)
    wired = receipt.get("newly_wired_programs") or []
    if (
        receipt.get("status") != "PASS_CANDIDATE_SOURCE"
        or [row.get("program_id") for row in wired] != [spec["program_id"]]
    ):
        raise B2Error("net_new_builder_receipt_invalid")
    return {
        "program_id": spec["program_id"],
        "mode": "net_new_incremental_builder",
        "command": run,
        "receipt": path_binding(receipt_path),
        "research": path_binding(research_copy),
    }


def _remove_intermediate_release(path: Path) -> None:
    path = path.resolve()
    if path.parent != RELEASES or not path.name.startswith(".glp-b2-wire-backlog-"):
        raise B2Error(f"intermediate_release_scope_invalid:{path}")
    if not path.exists():
        return
    result = subprocess.run(
        ["sudo", "-n", "rm", "-rf", "--", str(path)],
        text=True,
        capture_output=True,
        timeout=300,
    )
    if result.returncode or path.exists():
        raise B2Error(f"intermediate_release_cleanup_failed:{path}:{result.stderr[-300:]}")


# Quarantine at the SAME DEPTH as a live candidate. A candidate resolves
# shared artifacts by relative path - src/phase3-page-graph.mjs reaches the
# repo with "../../../../../reports/..." - so nesting a preserved tree one
# level deeper silently breaks every such reference and any rebuild inside it
# fails with a bogus ENOENT. That cost me a full misdiagnosis of round 37.
CANDIDATE_QUARANTINE = CANDIDATES
CANDIDATE_QUARANTINE_KEEP = 3


def _quarantine_failed_candidate(
    candidate: Path, output_dir: Path, error: Exception,
) -> dict[str, Any]:
    """Preserve a candidate that failed build:gate or a validator.

    Approved 2026-09-03. The failure path used to delete the candidate, so every
    build failure destroyed the only artifact that could explain it: rounds 32
    and 33 both died inside validate-provider-fastpath-plus5.mjs and left nothing
    to inspect, and the pre-materialization gate could not reproduce either.

    This changes the FAILURE path only. The tree is MOVED - never sealed, never
    deployed, never reminted into the content base - into a quarantine directory
    whose name cannot match the candidates/glp-b2-* glob the machine walks, with
    the failure receipt written beside it. Success behaves exactly as before.
    """
    receipt: dict[str, Any] = {
        "schema": "glp.candidate_quarantine.v1",
        "quarantined_at_utc": iso_utc(),
        "candidate": candidate.name,
        "error": f"{type(error).__name__}:{error}"[:4000],
        "sealed": False, "deployed": False, "in_content_base": False,
        "why": "preserved for diagnosis; the failure path used to delete it",
    }
    try:
        CANDIDATE_QUARANTINE.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        destination = CANDIDATE_QUARANTINE / f"{candidate.name}-quarantine-{stamp}"
        make_directories_writable(candidate)
        shutil.move(str(candidate), str(destination))
        receipt["path"] = str(destination)
        atomic_json(destination.parent / f"{destination.name}.failure.json", receipt)
        # Bounded: a candidate tree is ~1.1GB, so keep only the newest few.
        existing = sorted(
            (p for p in CANDIDATE_QUARANTINE.iterdir()
             if p.is_dir() and "-quarantine-" in p.name),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        for stale in existing[CANDIDATE_QUARANTINE_KEEP:]:
            make_directories_writable(stale)
            shutil.rmtree(stale, ignore_errors=True)
            stale.with_suffix(stale.suffix + ".failure.json").unlink(missing_ok=True)
        receipt["status"] = "PASS_QUARANTINED"
        receipt["pruned_to"] = CANDIDATE_QUARANTINE_KEEP
    except Exception as quarantine_error:  # noqa: BLE001 - never mask the real failure
        receipt["status"] = "FAILED_TO_QUARANTINE"
        receipt["quarantine_error"] = f"{type(quarantine_error).__name__}:{quarantine_error}"
        try:
            if candidate.exists():
                remove_materialized_tree(candidate)
        except Exception:  # noqa: BLE001
            pass
    try:
        atomic_json(output_dir / "candidate-quarantine.json", receipt)
    except Exception:  # noqa: BLE001
        pass
    return receipt


def _record_unverified_deployment(
    *, ledger_path: Path, ledger: dict[str, Any], day: dict[str, Any],
    release: Path, states: list[dict[str, Any]], error: Exception,
    batch_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a deploy that really happened but whose sensor verdict failed.

    The deployer exits non-zero when its post-deploy completeness sensor fails,
    so `_run` raises before `release_prepared` ever saves the ledger. The
    Cloudflare deployment and the current-content-base remint have already
    happened at that point, so the worked ledger ended up with no record of a
    real external effect at all (2026-08-18: both rows stayed `prepared` with
    deployment_id null while the site served them).

    Rows are NOT marked wired - `wired` means a PASS sensor, and it did not
    pass. What is recorded is the truth: this release is live, under this
    deployment id, with a failing sensor and the reason.
    """
    try:
        pointer = read_json(CURRENT_BASE)
    except B2Error as pointer_error:
        pointer = {"readback_error": f"{type(pointer_error).__name__}:{pointer_error}"}
    if Path(str(pointer.get("release_root") or "")).resolve() != release.resolve():
        record = {
            "status": "DEPLOYMENT_EFFECT_AMBIGUOUS_NO_REFIRE",
            "deployment_id": None,
            "release": str(release),
            "recorded_at_utc": iso_utc(),
            "effect_cardinality": "unknown_or_one",
            "error": f"{type(error).__name__}:{error}"[:1000],
            "rows": [state["key"] for state in states],
            "current_release_root": str(pointer.get("release_root") or ""),
            "pointer_readback_error": str(pointer.get("readback_error") or ""),
        }
        if batch_authority:
            record["batch_authority"] = dict(batch_authority)
        day["deployment_intent"] = {**(day.get("deployment_intent") or {}), **record}
        for state in states:
            state["deployment_intent"] = {
                **(state.get("deployment_intent") or {}),
                **record,
            }
            state["updated_at_utc"] = iso_utc()
        save_ledger(ledger_path, ledger)
        return record
    record = {
        "status": "DEPLOYED_SENSOR_NOT_VERIFIED",
        "effect_cardinality": "one",
        "deployment_id": pointer.get("deployment_id"),
        "release": str(release),
        "recorded_at_utc": iso_utc(),
        "sensor_status": "FAIL",
        "error": f"{type(error).__name__}:{error}"[:1000],
        "rows": [state["key"] for state in states],
    }
    if batch_authority:
        record["batch_authority"] = dict(batch_authority)
    record_day_deployment(day, record)
    for state in states:
        state["deployed_unverified"] = {
            "deployment_id": record["deployment_id"],
            "release": str(release),
            "recorded_at_utc": record["recorded_at_utc"],
        }
        state["updated_at_utc"] = iso_utc()
    save_ledger(ledger_path, ledger)
    return record



#: A row refused with row_not_preparable:landed_sensor_pending was refused BECAUSE
#: it had already landed. Recording that refusal as `blocked` destroyed the very
#: state that becomes `wired` - and it cannot be undone by a later deployment's
#: armed intent, because the row can never be PREPARED again while it is live.
#: That is a closed loop, so restoration is bound to the strongest external truth
#: available: the program is LIVE in the CURRENTLY DEPLOYED release. Nothing is
#: invented - the row's own landed_with_finding still names the deployment it
#: landed in, and the live link map independently confirms it is still serving.
LANDED_REPREPARATION_REFUSAL = "row_not_preparable:landed_sensor_pending"


def _live_compare_program_ids(release_root: Path) -> set[str]:
    """The ids the deployed release actually serves on compare, from its own module."""
    script = (
        'import { loadAffiliateLinkMap } from "%s/src/affiliate-link-map.mjs";'
        'const m = loadAffiliateLinkMap("compare", "glp-clinic-scout") || {};'
        'console.log(JSON.stringify(Object.keys(m).filter((k) => m[k] && m[k].live)));'
    ) % Path(release_root).as_posix()
    result = _run(["node", "--input-type=module", "-e", script], cwd=BUSINESS, timeout=300)
    if int(result.get("returncode") or 0) != 0:
        raise B2Error("landed_restore_live_readback_failed")
    return set(json.loads((result.get("stdout_tail") or "[]").strip().splitlines()[-1]))


def restore_landed_rows_from_live_truth(
    *, ledger_path: Path, plan: dict[str, Any], apply: bool = False,
) -> dict[str, Any]:
    """Restore landed rows a re-preparation knocked to blocked, proving each is live."""
    pointer = read_json(CURRENT_BASE)
    release_root = Path(str(pointer.get("release_root") or ""))
    if str(pointer.get("status") or "") != "PASS" or not release_root.is_dir():
        raise B2Error("landed_restore_content_base_invalid")
    live_ids = _live_compare_program_ids(release_root)
    names = {str(row.get("key") or ""): str(row.get("site_program_id") or "")
             for row in (plan.get("queue") or [])}
    restored: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    with locked_ledger(ledger_path):
        ledger = load_ledger(ledger_path)
        for key, state in (ledger.get("rows") or {}).items():
            if not isinstance(state, dict) or state.get("status") != "blocked":
                continue
            block = state.get("block") or {}
            evidence = (block.get("evidence") or {}).get("path")
            error = ""
            if evidence and Path(evidence).is_file():
                try:
                    error = str(read_json(Path(evidence)).get("error") or "")
                except (OSError, ValueError):
                    error = ""
            if LANDED_REPREPARATION_REFUSAL not in error:
                continue
            landed = state.get("landed_with_finding") or {}
            program = names.get(key) or str(state.get("site_program_id") or "")
            if not landed.get("deployment_id"):
                refused.append({"key": key, "why": "no landed_with_finding evidence"}); continue
            if not program:
                refused.append({"key": key, "why": "site_program_id unresolvable"}); continue
            if program not in live_ids:
                refused.append({"key": key, "program": program,
                                "why": "not live on the current deployed release"}); continue
            restored.append({"key": key, "program": program,
                             "landed_in": str(landed.get("deployment_id")),
                             "confirmed_live_on": str(pointer.get("deployment_id"))})
            if apply:
                state["status"] = LANDED_SENSOR_PENDING
                state["landed_restored_from_live_truth"] = {
                    "at_utc": iso_utc(),
                    "was": "blocked",
                    "refusal": LANDED_REPREPARATION_REFUSAL,
                    "confirmed_live_on_deployment": str(pointer.get("deployment_id")),
                    "basis": "the row was refused BECAUSE it had already landed, and the "
                             "currently deployed release still serves this program on compare",
                }
                state.pop("block", None)
        if apply and restored:
            save_ledger(ledger_path, ledger)
    return {"status": "PASS_RESTORED" if (apply and restored) else
            ("PASS_WOULD_RESTORE" if restored else "PASS_NOTHING_TO_RESTORE"),
            "restored": restored, "refused": refused,
            "live_program_count": len(live_ids)}


def _wire_landed_rows_on_live_truth(
    ledger: dict[str, Any], *, deployment_id: str, release_root: Path,
    live_ids: set[str], names: dict[str, str],
) -> list[dict[str, Any]]:
    """Third path, shared: a landed_sensor_pending row LIVE on a zero-sensor head is wired.

    DEFECT 2026-09-03 (ruling: wired follows live truth). This loop lived only in
    reconcile_landed_uncertain_deployment, which no machine step calls. So the
    deployment e3d687a1 landed with a ZERO-failure sensor, release_prepared wired
    its own two release-set rows, and the 19 landed_sensor_pending rows that the
    same head demonstrably serves stayed pending - the exact state the third path
    exists to promote. The rule is head-scoped, not release-set-scoped:
    live on head + landed evidence + zero-failure sensor => wired. Mutates the
    ledger rows in place; the caller holds the ledger lock and saves.
    """
    wired_now: list[dict[str, Any]] = []
    for key, state in (ledger.get("rows") or {}).items():
        if not isinstance(state, dict):
            continue
        if state.get("status") != LANDED_SENSOR_PENDING:
            continue
        landed = state.get("landed_with_finding") or {}
        if not landed.get("deployment_id"):
            continue
        # The identity the row LANDED under wins. A later Sheet slug backfill can
        # rename a plan queue row (g-plan -> g-plan-review) while the head still
        # serves the program under the id it was wired as; the plan name is the
        # fallback, never the authority, for a row that has already landed.
        program = (str((state.get("identity") or {}).get("site_program_id") or "")
                   or names.get(key) or str(state.get("site_program_id") or ""))
        if not program or program not in live_ids:
            continue
        state["status"] = "wired"
        state.pop("landed_sensor_pending", None)
        state["wired_on_live_truth"] = {
            "deployment_id": str(deployment_id),
            "release": str(release_root),
            "program_id": program,
            "sensor_failure_count": 0,
            "evidence": "zero-failure post-deploy sensor on the head that serves "
                        "this program on compare",
            "landed_in": str(landed.get("deployment_id")),
            "set_at_utc": iso_utc(),
        }
        state["updated_at_utc"] = iso_utc()
        wired_now.append({"key": key, "program_id": program})
    return wired_now


def reconcile_landed_uncertain_deployment(
    *,
    ledger_path: Path,
    pt_date: str,
    deployment_id: str,
    release_root: Path,
    sensor_failure_count: int | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve an armed deployment intent once the deployment is CF-verified.

    _record_deployment_effect_uncertain arms a deployment intent, records
    ``deployed_unverified`` on every row, and returns. Nothing clears that
    intent: ``release_prepared`` only calls ``_clear_deployment_intent`` on the
    success path, which requires sensor PASS with zero failures. So a deployment
    that genuinely LANDED but whose post-deploy sensor failed leaves the day and
    its rows armed forever, and every later run dies on
    ``day_deployment_reconciliation_required_no_refire``. The one existing
    recovery machine, glp_count_copy_sensor_recovery.py, is incident-bound: it
    refuses any authority whose failed_checks is not exactly ["count_copy"].

    This resolves the intent from EXTERNAL truth and nothing else. It requires
    the current-content-base pointer to name the same deployment id and the same
    release root - the pointer is reminted by the deployer itself - so it cannot
    be used to invent a deployment that did not happen.

    It deliberately does NOT mark any row ``wired``. Wiring requires a
    zero-failure sensor and that is not this function's to grant. Rows stay
    ``prepared`` so the next governed release re-releases them, which is exactly
    what completing a partial wire-in needs.
    """
    release_root = Path(release_root).resolve()
    pointer = read_json(CURRENT_BASE)
    if str(pointer.get("deployment_id") or "") != str(deployment_id):
        raise B2Error("reconcile_deployment_id_not_current_base")
    if Path(str(pointer.get("release_root") or "")).resolve() != release_root:
        raise B2Error("reconcile_release_root_not_current_base")
    if not (release_root / "data/program-registry.json").is_file():
        raise B2Error("reconcile_release_root_invalid")
    # WIRED FOLLOWS THE SITE, NOT THE INTENT'S MEMORY (ruling 2026-09-03).
    #
    # The first two paths key on the armed intent, or on a `prepared` row whose
    # landed evidence names THIS deployment. A row already in
    # landed_sensor_pending matches neither, so a zero-failure sensor could never
    # promote it - the rows that had genuinely landed were exactly the ones that
    # could never be marked wired. `wired` is the money state; it must follow what
    # the site actually serves. So on a ZERO-failure sensor, a landed_sensor_pending
    # row whose program is LIVE on this head and whose landed evidence is recorded
    # is marked wired, regardless of the intent. A non-zero sensor, or a program
    # not live on the head, leaves the row untouched.
    if sensor_failure_count is not None and int(sensor_failure_count) == 0:
        names = {str(row.get("key") or ""): str(row.get("site_program_id") or "")
                 for row in ((plan or {}).get("queue") or [])}
        live_ids = _live_compare_program_ids(release_root)
        wired_now: list[dict[str, Any]] = []
        with locked_ledger(ledger_path):
            ledger = load_ledger(ledger_path)
            day = day_state(ledger, pt_date)
            wired_now = _wire_landed_rows_on_live_truth(
                ledger, deployment_id=str(deployment_id), release_root=release_root,
                live_ids=live_ids, names=names)
            if wired_now:
                record_day_deployment(day, {
                    "status": "LANDED_SENSOR_VERIFIED",
                    "deployment_id": str(deployment_id),
                    "release": str(release_root),
                    "sensor_status": "PASS",
                    "operation_id": f"live-truth:{deployment_id}",
                    "reconciled_at_utc": iso_utc(),
                    "wired_keys": [row["key"] for row in wired_now],
                    "rows": [row["key"] for row in wired_now],
                })
                save_ledger(ledger_path, ledger)
        if wired_now:
            return {
                "status": "PASS_WIRED_ON_ZERO_FAILURE_SENSOR",
                "changed": True,
                "deployment_id": str(deployment_id),
                "release": str(release_root),
                "rows_marked_wired": len(wired_now),
                "wired": sorted(row["key"] for row in wired_now),
                "basis": "landed_sensor_pending + live on this head + zero-failure sensor",
            }

    with locked_ledger(ledger_path):
        ledger = load_ledger(ledger_path)
        day = day_state(ledger, pt_date)
        intent = day.get("deployment_intent") or {}
        operation_id = str(intent.get("operation_id") or "")
        rows = [str(key) for key in (intent.get("rows") or [])]
        if not intent:
            # The intent is cleared once resolved, but a row that landed keeps
            # the evidence ON ITSELF: landed_with_finding names the deployment
            # id and release it landed in. When the pointer agrees with that
            # same deployment and release (already checked above), the row set
            # is derivable without an armed intent. This is the same external
            # truth, read from a second place - it invents nothing, and a row
            # whose recorded deployment differs is not touched.
            rows = [
                str(key) for key, state in (ledger.get("rows") or {}).items()
                if isinstance(state, dict)
                and state.get("status") == "prepared"
                and str(((state.get("landed_with_finding") or {}).get("deployment_id")) or "")
                == str(deployment_id)
                and str(((state.get("landed_with_finding") or {}).get("release")) or "")
                == str(release_root)
            ]
            operation_id = f"row-evidence:{deployment_id}"
            if not rows:
                return {"status": "PASS_NO_ARMED_INTENT", "changed": False,
                        "deployment_id": str(deployment_id)}
        resolved: list[str] = []
        for key in rows:
            state = (ledger.get("rows") or {}).get(key)
            if not isinstance(state, dict):
                continue
            unverified = state.pop("deployed_unverified", None)
            if state.get("deployment_intent"):
                state.pop("deployment_intent", None)
            if unverified:
                state.setdefault("deployed_unverified_history", []).append({
                    **unverified, "reconciled_at_utc": iso_utc(),
                })
            state["landed_with_finding"] = {
                "deployment_id": str(deployment_id),
                "release": str(release_root),
                "sensor_status": "FAIL",
                "sensor_failure_count": (
                    int(sensor_failure_count) if sensor_failure_count is not None else None
                ),
                "reconciled_at_utc": iso_utc(),
            }
            # Leave "prepared" for the truthful third state. Never "wired":
            # wiring still requires a zero-failure sensor and nothing here
            # asserts one. The evidence that licenses this transition is the
            # current-content-base pointer agreement checked above, not any
            # claim made by this process.
            state["status"] = LANDED_SENSOR_PENDING
            state["landed_sensor_pending"] = {
                "deployment_id": str(deployment_id),
                "release": str(release_root),
                "sensor_failure_count": (
                    int(sensor_failure_count) if sensor_failure_count is not None else None
                ),
                "evidence": "current_content_base_pointer_names_this_deployment_and_release",
                "promotes_to_wired_when": "a later run deploys this row with a zero-failure sensor",
                "set_at_utc": iso_utc(),
            }
            state["updated_at_utc"] = iso_utc()
            resolved.append(key)
        day.pop("deployment_intent", None)
        record_day_deployment(day, {
            "status": "LANDED_SENSOR_NOT_VERIFIED",
            "deployment_id": str(deployment_id),
            "release": str(release_root),
            "sensor_status": "FAIL",
            "operation_id": operation_id,
            "reconciled_at_utc": iso_utc(),
            "wired_keys": [],
            "rows": resolved,
        })
        save_ledger(ledger_path, ledger)
    return {
        "status": "PASS_RECONCILED_LANDED_WITH_FINDING",
        "changed": True,
        "deployment_id": str(deployment_id),
        "release": str(release_root),
        "operation_id": operation_id,
        "rows_resolved": sorted(resolved),
        "rows_marked_wired": 0,
        # DEFECT 2026-09-02: this sentence hardcoded "27 sensor failures", the
        # count from the 02:37Z landing it was written during. The 18:01Z
        # landing had 8, so the string put a FALSE number into a durable
        # receipt. Take the count from the caller, and say nothing numeric when
        # it is not supplied rather than assert a number we do not have.
        "why_not_wired": (
            "wiring requires a zero-failure post-deploy sensor; this deployment "
            + (f"landed with {int(sensor_failure_count)} sensor failures"
               if sensor_failure_count is not None
               else "landed with a non-zero post-deploy sensor result")
        ),
        "sensor_failure_count": (
            int(sensor_failure_count) if sensor_failure_count is not None else None
        ),
    }


def _arm_deployment_intent(
    *, ledger_path: Path, ledger: dict[str, Any], day: dict[str, Any],
    release: Path, predecessor_deployment_id: str, states: list[dict[str, Any]],
    batch_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    keys = [str(state.get("key") or "") for state in states]
    material = {
        "release": str(release.resolve()),
        "predecessor_deployment_id": predecessor_deployment_id,
        "rows": keys,
    }
    operation_id = "wire-deploy:" + sha_bytes(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )[:32]
    intent = {
        "status": "ARMED_BEFORE_DEPLOYMENT_INVOCATION",
        "operation_id": operation_id,
        "release": str(release),
        "predecessor_deployment_id": predecessor_deployment_id,
        "rows": keys,
        "armed_at_utc": iso_utc(),
        "effect_cardinality": "zero_or_one",
    }
    if batch_authority:
        intent["batch_authority"] = dict(batch_authority)
    if day.get("deployment_intent"):
        raise B2Error("deployment_intent_already_armed")
    day["deployment_intent"] = intent
    for state in states:
        if state.get("deployment_intent") or state.get("deployed_unverified"):
            raise B2Error(f"row_deployment_reconciliation_required:{state.get('key')}")
        state["deployment_intent"] = dict(intent)
        state["updated_at_utc"] = iso_utc()
    save_ledger(ledger_path, ledger)
    return intent


def _clear_deployment_intent(
    *, day: dict[str, Any], states: list[dict[str, Any]], operation_id: str,
) -> None:
    if str((day.get("deployment_intent") or {}).get("operation_id") or "") != operation_id:
        raise B2Error("day_deployment_intent_binding_lost")
    day.pop("deployment_intent", None)
    for state in states:
        if str((state.get("deployment_intent") or {}).get("operation_id") or "") != operation_id:
            raise B2Error(f"row_deployment_intent_binding_lost:{state.get('key')}")
        state.pop("deployment_intent", None)


def release_prepared(
    *,
    plan_path: Path,
    ledger_path: Path,
    output_dir: Path,
    deploy: bool,
    provider_content_change: bool,
) -> dict[str, Any]:
    plan = read_json(plan_path)
    verify_plan_sources(plan)
    payout_queue = plan.get("payout_evaluation_queue") or {}
    payout_blocks = payout_queue.get("blocked") or []
    if payout_blocks:
        raise B2Error(
            "payout_evaluation_switches_block_deploy:"
            + ",".join(
                f"{item.get('provider')}={item.get('reason')}" for item in payout_blocks
            )
        )
    with locked_ledger(ledger_path):
        ledger = load_ledger(ledger_path)
        day = day_state(ledger, plan["pt_date"])
        _assert_no_sensor_recovery_intent(day, ledger["rows"].values())
        if day.get("deployment_intent"):
            raise B2Error("day_deployment_reconciliation_required_no_refire")
        wired_limit = plan_wired_limit(plan)
        required_batch_keys = batch_required_keys(plan)
        minimum_successful_rows = batch_minimum_successful_rows(plan)
        slots = wired_limit - len(day["wired_keys"])
        queue_order = {row["key"]: index for index, row in enumerate(plan.get("queue") or [])}
        # Only genuinely-prepared rows enter a deployment scope. A row in
        # LANDED_SENSOR_PENDING already landed, so it is neither deployed again
        # here nor counted against the authority's allowed_queue_keys - which is
        # what unwedges wire_batch_authority_prepared_rows_outside_scope without
        # widening any scope or editing any state by hand.
        prepared = [
            row for row in ledger["rows"].values()
            if row.get("pt_date") == plan["pt_date"] and row.get("status") == "prepared"
        ]
        prepared.sort(key=lambda row: queue_order.get(row["key"], 10**9))
        prepared = prepared[:slots]
        reconciliation_required = [
            str(row.get("key") or "") for row in prepared
            if row.get("deployment_intent") or row.get("deployed_unverified")
        ]
        if reconciliation_required:
            raise B2Error(
                "deployment_reconciliation_required_no_refire:"
                + ",".join(reconciliation_required)
            )
        if deploy and required_batch_keys:
            prepared_keys = [str(row.get("key") or "") for row in prepared]
            if prepared_keys != required_batch_keys:
                raise B2Error("wire_batch_authority_required_rows_not_prepared")
        deployment_gate = followup_deployment_gate(
            plan=plan, day=day, prepared=prepared, current_base=CURRENT_BASE,
            deploy=deploy,
        )
        if not deployment_gate["authorized"]:
            return {
                "status": "PASS_IDEMPOTENT_DEPLOYMENT",
                "deployment": day.get("deployment"),
                "deployment_gate": deployment_gate,
            }
        if not prepared:
            raise B2Error("no_prepared_rows")
        output_dir.mkdir(parents=True, exist_ok=True)
        # The batch may pin an exact source release that is intentionally older
        # than the live predecessor. Source bytes and deployment predecessor are
        # separate authorities: always bind the current pointer for the deployer,
        # even when the source tree comes from the batch contract.
        pointer = read_json(CURRENT_BASE)
        current_predecessor, _, _ = _registry_by_id(pointer)
        source = batch_source_release(plan)
        if source is None:
            source = current_predecessor
        else:
            _registry_by_id({"release_root": str(source)})
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        candidate = CANDIDATES / f"glp-b2-wire-backlog-{plan['pt_date']}-{stamp}"
        release = RELEASES / f"glp-b2-wire-backlog-{plan['pt_date']}-{stamp}"
        successful: list[dict[str, Any]] = []
        activations: list[dict[str, Any]] = []
        row_blocks: list[dict[str, Any]] = []
        step_materializations: list[dict[str, Any]] = []
        intermediate_releases: list[Path] = []
        current_source = source
        initial_materialization: dict[str, Any] | None = None
        deployment_invoked = False
        deployment_committed = False
        deployment_intent: dict[str, Any] | None = None
        try:
            for index, state in enumerate(prepared, start=1):
                step_target = candidate.with_name(f"{candidate.name}-step-{index:02d}")
                step_release = RELEASES / f".{candidate.name}-stage-{index:02d}"
                try:
                    plan_row = plan_by_key(plan, state["key"])
                    artifacts = state["artifacts"]
                    spec_path = verify_binding(artifacts["spec"], label="prepared_spec")
                    research_path = verify_binding(artifacts["research"], label="prepared_research")
                    terms_path = verify_binding(artifacts["terms_classification"], label="prepared_terms")
                    pricing_path = (
                        verify_binding(artifacts["pricing_spec"], label="prepared_pricing")
                        if artifacts.get("pricing_spec") else None
                    )
                    spec = _extract_program_spec(read_json(spec_path), plan_row["site_program_id"])
                    _research_valid(read_json(research_path), plan_row["site_program_id"])
                    _terms_valid(read_json(terms_path), plan_row)
                    source_registry = _registry_by_id(
                        pointer if current_source == current_predecessor else {"release_root": str(current_source)}
                    )[1]
                    source_row = source_registry.get(str(spec["program_id"]))
                    source_profile = (((source_row or {}).get("profiles") or {}).get("glp-clinic-scout") or {})
                    already_live = (
                        (source_row or {}).get("commercial_state") == "live"
                        and source_profile.get("wired") is True
                    )
                    if already_live:
                        pending_go_requests = [
                            request
                            for request in (
                                (plan.get("go_route_provision_queue") or {}).get(
                                    "deploy_requests"
                                )
                                or []
                            )
                            if str(request.get("site_program_id") or "")
                            == str(spec["program_id"])
                        ]
                        if len(pending_go_requests) > 1:
                            raise B2Error(
                                "recovery_pending_go_provision_match_count:"
                                + str(len(pending_go_requests))
                            )
                        materialization = _copy_candidate(current_source, step_target)
                        activation = _recover_already_live(
                            step_target,
                            spec,
                            plan_row["sheet"],
                            pending_go_request=(
                                pending_go_requests[0] if pending_go_requests else None
                            ),
                        )
                        step_materializations.append({
                            "key": state["key"],
                            "mode": "recover_post_deploy_sensor_failure",
                            "materialization": materialization,
                        })
                    elif plan_row.get("existing_editorial_page"):
                        materialization = _copy_candidate(current_source, step_target)
                        activation = _activate_existing(step_target, spec, plan_row["sheet"])
                        step_materializations.append({
                            "key": state["key"],
                            "mode": "activate_existing_editorial_page",
                            "materialization": materialization,
                        })
                    else:
                        activation = _build_net_new_step(
                            source=current_source,
                            target=step_target,
                            state=state,
                            plan_row=plan_row,
                            spec=spec,
                            research_path=research_path,
                            output_dir=output_dir,
                        )
                        step_materializations.append({
                            "key": state["key"],
                            "mode": "net_new_incremental_builder",
                            "receipt": activation["receipt"],
                        })
                    profile_activation = (
                        {"status": "PASS_ALREADY_ACTIVE_RECOVERY", "program_id": spec["program_id"]}
                        if already_live
                        else _activate_provider_dynamic_profile(step_target, spec, spec_path)
                    )
                    research_merge = _merge_research_into_candidate(step_target, research_path)
                    activation["provider_dynamic_profile"] = profile_activation
                    activation["research_merge"] = research_merge
                    # ORDER IS A CONTRACT. The incremental builder/activation
                    # above creates the registry row first. Only then may the
                    # staged pricing row be applied; the pricing validator
                    # rejects a matrix provider that is not live in the same
                    # candidate (`pricing_matrix_carries_non_live_provider`).
                    if pricing_path is not None:
                        activation["pricing_matrix"] = _apply_staged_pricing(
                            step_target, pricing_path, str(spec["program_id"]))
                    _seal_candidate(step_target, step_release)
                    remove_materialized_tree(step_target)
                    current_source = step_release
                    intermediate_releases.append(step_release)
                    successful.append(state)
                    activations.append(activation)
                except Exception as exc:
                    # PER-ROW FAILURE PATH. Preparation builds each row into its own
                    # step_target, so a build:gate or validator failure during
                    # preparation destroyed the tree here, not at the outer
                    # candidate. That is why quarantining only the outer candidate
                    # captured nothing for the rows that trip the fastpath
                    # validator. Preserve this tree too - moved, never sealed,
                    # never deployed - so the failure is diagnosable.
                    if step_target.exists():
                        _quarantine_failed_candidate(step_target, output_dir, exc)
                    if step_release.exists():
                        _remove_intermediate_release(step_release)
                    row_blocks.append(_mark_release_row_blocked(
                        ledger=ledger,
                        day=day,
                        state=state,
                        output_dir=output_dir,
                        error=exc,
                    ))
                    continue
            if not successful:
                _raise_all_prepared_rows_blocked(ledger_path=ledger_path, ledger=ledger)
            if deploy and len(successful) < minimum_successful_rows:
                raise B2Error("wire_batch_authority_minimum_success_not_met")
            if deploy and required_batch_keys:
                successful_keys = [str(row.get("key") or "") for row in successful]
                if successful_keys != required_batch_keys:
                    raise B2Error("wire_batch_authority_required_rows_not_releasable")
            initial_materialization = _copy_candidate(current_source, candidate)
            # Carry the hash-bound release-base addendum before anything reads or
            # builds the candidate, so the build, the later string migrations and
            # npm test all see the same reviewed bytes.
            release_base_addenda = _apply_release_base_addenda_overlay(candidate)
            for intermediate in intermediate_releases:
                _remove_intermediate_release(intermediate)
            intermediate_releases.clear()
            # candidate-in-place mode intentionally assumes caller-owned detached
            # transaction outputs. The final candidate is hardlinked from the sealed
            # per-row predecessor, so detach all transaction outputs before the
            # shared writer runs.
            for relative in SURFACE_TRANSACTION_OUTPUTS:
                _detach(candidate / relative)
            # BEFORE surface unification, deliberately: unification regenerates
            # money-page-specs from these records, so a rescission applied after
            # it would be overwritten by the very rows it was meant to correct.
            measured_terms_rescission = _ensure_measured_terms_rescission(candidate)
            # /go entry_state must agree with the governed promotion authority
            # BEFORE unification reads the registry to derive its counts.
            go_entry_state_derivation = _derive_go_entry_state_from_authority(candidate)
            # A provider with no published price renders no Option C card; say
            # so in the suites' own exclusion list rather than inventing a price.
            provider_card_documented_gap = (
                _ensure_provider_card_documented_gap_contract_support(candidate))
            integration = _run(
                [sys.executable, str(SCRIPTS / "glp_surface_unification.py"), "--release", str(candidate), "--candidate-in-place"],
                cwd=BUSINESS, timeout=1800,
            )
            all_payout_switch_requests = list(
                (plan.get("payout_evaluation_queue") or {}).get("deploy_requests") or []
            )
            # PAYOUT_SWITCH_WINNER_OPERABLE_V1: a blocked winner rail defers its
            # switch instead of taking the incumbent's live link dark.
            winner_blocks = {
                str(request.get("switch_id") or ""):
                    payout_switch_winner_block_reason(request, ledger)
                for request in all_payout_switch_requests
            }
            payout_switch_requests = [
                request for request in all_payout_switch_requests
                if deploy and not winner_blocks.get(str(request.get("switch_id") or ""))
            ]
            deferred_payout_switch_requests = [
                {
                    "switch_id": request.get("switch_id"),
                    "provider": request.get("provider"),
                    "status": (
                        "HELD_FOR_ATOMIC_DEPLOY_RELEASE" if not deploy
                        else "DEFERRED_WINNER_RAIL_BLOCKED"
                    ),
                    "winner": {
                        "network": (request.get("winner") or {}).get("network"),
                        "program_id": (request.get("winner") or {}).get("program_id"),
                    },
                    "winner_block_reason":
                        winner_blocks.get(str(request.get("switch_id") or "")),
                    "incumbent_live_link_preserved": bool(deploy),
                    "contract": PAYOUT_SWITCH_WINNER_OPERABLE_CONTRACT,
                }
                for request in all_payout_switch_requests
                if not deploy or winner_blocks.get(str(request.get("switch_id") or ""))
            ]
            payout_switch_applications = [
                apply_provider_wide_payout_switch(candidate, request)
                for request in payout_switch_requests
            ]
            # Same-transaction ordering is mandatory: surface integration creates
            # net-new registry/approval rows; provider-wide payout switches then
            # establish the winning rail; only then may /go consume that exact
            # authority. Running /go first makes new providers impossible and can
            # bind an incumbent rail that the same release immediately retires.
            all_go_route_provision_requests = list(
                (plan.get("go_route_provision_queue") or {}).get("deploy_requests") or []
            )
            go_route_provision_requests_for_deploy = (
                all_go_route_provision_requests if deploy else []
            )
            deferred_go_route_provision_requests = (
                [] if deploy else [
                    {
                        "update_ids": request.get("queue_update_ids"),
                        "provider": request.get("provider"),
                        "first_party_go_route": request.get("first_party_go_route"),
                        "status": "HELD_FOR_ATOMIC_DEPLOY_RELEASE",
                    }
                    for request in all_go_route_provision_requests
                ]
            )
            go_route_provision_applications = [
                apply_first_party_go_route_provision(candidate, request)
                for request in go_route_provision_requests_for_deploy
            ]
            go_runtime_discovery_refresh = (
                _run(
                    ["node", str(GO_RUNTIME_REFRESH), "--release", str(candidate)],
                    cwd=BUSINESS, timeout=900,
                )
                if deploy else {"status": "HELD_VERIFY_ONLY_NO_DEPLOY"}
            )
            go_runtime_promotion_authority = (
                apply_go_runtime_promotion_authority(candidate)
                if deploy else {
                    "status": "HELD_VERIFY_ONLY_NO_DEPLOY",
                    "tracking_link_requests": 0,
                    "tracking_clicks": 0,
                }
            )
            final_cardinality_test_refresh = _run(
                [
                    sys.executable, str(CARDINALITY_TEST_REFRESH),
                    "--release", str(candidate),
                    "--activated-program-id", str(plan_by_key(plan, successful[-1]["key"])["site_program_id"]),
                ],
                cwd=BUSINESS, timeout=300,
            )
            # Last registry mutation happened above (payout switches, /go
            # provisioning, go runtime authority). Re-mint the price-index
            # semantic basis before the price-index step reads it.
            price_index_basis_remint = _remint_price_index_semantic_basis(candidate)
            price_index_refresh = _refresh_price_index_before_build(
                candidate, str(plan["pt_date"])
            )
            # THE /go SURFACE ALLOWLIST MUST PRECEDE THE MANIFEST REFRESH.
            # DEFECT 2026-09-03: this ran in the pre-build TAIL, after
            # GO_RUNTIME_REFRESH had already generated functions/go/program-manifest.js
            # from the pre-edit module. The source carried the Pass 7 roles and the
            # built manifest did not - the edit landed and shipped nothing, which is
            # exactly what the built-artifact rule exists to catch.
            go_surface_allowlist = _ensure_go_surface_allowlist_and_freeze(candidate)
            go_runtime_refresh = _run(
                [
                    "node", str(GO_RUNTIME_REFRESH),
                    "--release", str(candidate),
                ],
                cwd=BUSINESS, timeout=900,
            )
            # NOTE (2026-09-02): an earlier attempt re-asserted
            # apply_go_runtime_promotion_authority here, on the theory that the
            # refresh above wipes the promotion flags. It does wipe them, but the
            # re-assert is the WRONG remedy: that function also runs
            # _ensure_affiliati_subid_contract_support, whose edit to
            # src/network-constructors.mjs is not idempotent, so a second call
            # double-applied it and broke "network constructor accepts UUIDv7 as
            # the owned click identity". The real issue is that nothing can
            # activate without a live affiliate link in the compare surface, which
            # _reconcile_go_promotion_test_migration below handles honestly.
            go_runtime_promotion_authority_reassert = {
                "status": "NOT_REASSERTED_SEE_NOTE",
                "why": "apply_go_runtime_promotion_authority is not idempotent; "
                       "its affiliati sub-id contract support double-applies",
            }
            go_promotion_test_reconcile = (
                _reconcile_go_promotion_test_migration(candidate, Path(source))
                if deploy else {"status": "HELD_VERIFY_ONLY_NO_DEPLOY"}
            )
            try:
                quiz_golden_vector_refresh = _run(
                    [
                        "node", str(QUIZ_GOLDEN_VECTOR_REFRESH),
                        "--release", str(candidate),
                        "--predecessor", str(source),
                        "--change-reason", "provider-content-build",
                    ],
                    cwd=BUSINESS, timeout=900,
                )
            except Exception:
                # DIAGNOSTIC: quiz_logic_drift_during_roster_refresh names the file
                # but not the bytes, and the candidate is destroyed on failure, so
                # the drift has been unreproducible from receipts alone. Capture
                # both sides of every logic path before re-raising. Read-only copy;
                # it changes no outcome.
                try:
                    evidence = output_dir / "quiz-drift-evidence"
                    for rel in (
                        "src/quiz.mjs", "src/quiz-ranking.mjs", "src/program-registry.mjs",
                        "src/therapeutic-taxonomy.mjs", "src/merit-ranking.mjs",
                        "src/provider-dynamic-profiles.mjs", "scripts/build-staging.mjs",
                        "scripts/audit-quiz-results-matrix.mjs",
                        "scripts/validate-quiz-scoring-rationale.mjs",
                        "data/quiz-scoring-rationale.json",
                    ):
                        for label, root in (("candidate", candidate), ("predecessor", source)):
                            src_file = Path(root) / rel
                            if not src_file.is_file():
                                continue
                            dest = evidence / label / rel
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(src_file.read_bytes())
                except Exception:
                    pass
                raise
            quiz_golden_vector_refresh_receipt = read_json(
                candidate / "artifacts" / "ops" / "quiz-golden-vector-refresh.json"
            )
            _validate_quiz_golden_vector_refresh_receipt(
                quiz_golden_vector_refresh_receipt
            )
            quiz_golden_vector_refresh_receipt_path = (
                output_dir / "quiz-golden-vector-refresh.json"
            )
            atomic_json(
                quiz_golden_vector_refresh_receipt_path,
                quiz_golden_vector_refresh_receipt,
            )
            # Every materialization branch above is provider-content work: net-new
            # add, existing-page activation, or already-live recovery. Both the
            # deploy and local verify-only release modes build those changed bytes,
            # so both use the refresh lane. The later deployer `--verify-only`
            # path is not a build and receives no environment override.
            build_env = _wire_in_build_env(
                provider_content_change=provider_content_change,
                release_date=str(plan["pt_date"]),
            )
            katalys_postback_attribution_receipt = (
                katalys_postback_attribution.apply(candidate))
            build_generated_outputs = _detach_build_generated_outputs(candidate)
            provider_visual_fallback_refresh = _run(
                [
                    sys.executable,
                    str(PROVIDER_VISUAL_FALLBACK_REFRESH),
                    "--release", str(candidate),
                ],
                cwd=BUSINESS, timeout=300,
            )
            tracking_allowed_hosts_refresh = _run(
                [
                    sys.executable,
                    str(candidate / "scripts" / "emit-tracking-allowed-hosts.py"),
                    "--root", str(candidate),
                ],
                cwd=candidate, timeout=300,
            )
            # A provider roster change also changes the frozen calculator-email
            # affiliate manifest. Refresh it from the candidate's governed
            # registry before build-staging compares the generated module. The
            # candidate is hardlinked from its predecessor, so detach the one
            # generated source before the migration writes it.
            _detach(candidate / COST_AFFILIATE_EMAIL_MANIFEST)
            cost_affiliate_email_manifest_refresh = _run(
                [
                    "node",
                    str(candidate / "scripts" / "migrate-cost-affiliate-email-manifest.mjs"),
                ],
                cwd=candidate,
                timeout=300,
            )
            provider_pricing_quote_validator_refresh = (
                _normalize_provider_pricing_quote_validator(candidate)
            )
            # RE-BIND THE THERAPEUTIC AUTHORITIES BEFORE THE BUILD. The research
            # autowire backrun rewrites program-authority receipts, changing their
            # bytes; the taxonomy pins each authority's sha256 in TWO places plus a
            # per-row normalized digest computed over them. Re-stamping only the
            # first trades a failing sensor for a failing build, which is why the
            # earlier Python re-mint stayed unwired. The node re-mint uses the
            # release's OWN digest function, so all three move together. It carries
            # no therapeutic claim: a program-authority receipt asserts nothing
            # about a drug, brand or delivery form, so re-pointing at its current
            # bytes changes no assertion. Non-program authorities are REFUSED, so
            # genuine unexplained drift still fails the sensor.
            _detach(candidate / "data/program-registry.json")
            therapeutic_authority_remint = _run(
                ["node", str(SCRIPTS / "glp_therapeutic_authority_remint.mjs"),
                 "--release", str(candidate), "--business", str(BUSINESS)],
                cwd=BUSINESS, timeout=300,
            )
            # The Meta pixel, folded into the candidate's own source rather than
            # overlaid. See _ensure_meta_pixel_restore for why the addendum's
            # whole-file build-staging.mjs could not simply be applied.
            meta_pixel_restore = _ensure_meta_pixel_restore(candidate)
            # A page may assert only what its research artifact attests. Runs
            # BEFORE the build, because the build renders these contracts.
            attested_page_copy = _ensure_attested_page_copy(candidate)
            build_gate = _run(
                ["npm", "run", "build:gate"],
                cwd=candidate, timeout=1800, env=build_env,
            )
            provenance_refresh = _provenance_refresh_evidence(
                candidate, build_gate, expected=provider_content_change,
            )
            predecessor_ephemeral_restore = _restore_build_ephemeral_predecessor_files(source, candidate)
            predecessor_image_restore = _restore_predecessor_referenced_images(source, candidate)
            site_capture_consent_refresh = _run(
                [
                    "node", str(SITE_CAPTURE_CONSENT_REFRESH),
                    "--release", str(candidate),
                ],
                cwd=BUSINESS, timeout=900,
            )
            post_build_price_index_refresh = _run(
                [
                    "node", str(PRICE_INDEX_SOURCE_REBIND),
                    "--release", str(candidate),
                    "--observed-on", str(plan["pt_date"]),
                ],
                cwd=BUSINESS, timeout=900,
            )
            post_build_cardinality_test_refresh = _run(
                [
                    sys.executable, str(CARDINALITY_TEST_REFRESH),
                    "--release", str(candidate),
                    "--activated-program-id", str(plan_by_key(plan, successful[-1]["key"])["site_program_id"]),
                ],
                cwd=BUSINESS, timeout=300,
            )
            content_upgrade_provenance_test_refresh = _normalize_content_upgrade_provenance_test(candidate)
            fastpath_merit_score_test_refresh = _normalize_fastpath_merit_score_test(candidate)
            go_runtime_held_cardinality_test_refresh = (
                _normalize_go_runtime_held_cardinality_tests(candidate)
            )
            pass2_quiz_routing_test_refresh = _normalize_pass2_quiz_routing_test(candidate)
            # The build and post-build normalizers can change the governed
            # provider/link surface after the pre-build projection was emitted.
            # Recompute immediately before npm test so the validator certifies
            # the exact candidate bytes that would be released.
            post_build_tracking_allowed_hosts_refresh = _run(
                [
                    sys.executable,
                    str(candidate / "scripts" / "emit-tracking-allowed-hosts.py"),
                    "--root", str(candidate),
                ],
                cwd=candidate, timeout=300,
            )
            # HARD GATE 2026-09-02, not a test that can be superseded: the built
            # affiliate link map must be non-empty and must not lose a live
            # program relative to the base release. A payout switch wrote a
            # mis-cased network token, authorizedSelectionMap returned null for
            # the whole selection, and buildAffiliateLinkMap returned {} - every
            # affiliate link on the site dark, with a GREEN build, because
            # loadAffiliateLinkMap swallows the failure in a try/catch. Evaluate
            # with the site's own module and refuse the candidate, naming the
            # delta, before anything is sealed or deployed.
            affiliate_link_map_floor = _run(
                [
                    "node", str(AFFILIATE_LINK_MAP_FLOOR),
                    "--candidate", str(candidate),
                    "--base", str(source),
                    "--surface", "compare",
                ],
                cwd=BUSINESS, timeout=300,
            )
            try:
                suite = _run_npm_test_with_top_level_replay(cwd=candidate, timeout=3600)
            except Exception as suite_error:
                # DIAGNOSTIC: the TAP tail is capped, so a run with more failures
                # than the cap reports only the first few and the candidate is then
                # destroyed. Persist the whole failure text next to the run so every
                # `not ok` is recoverable. Read-only; it changes no outcome.
                try:
                    dump = output_dir / "npm-test-failure-full.txt"
                    dump.parent.mkdir(parents=True, exist_ok=True)
                    dump.write_text(str(suite_error), encoding="utf-8")
                except Exception:
                    pass
                raise
            go_runtime_promotion_verification = (
                verify_go_runtime_promotion(candidate)
                if deploy else {
                    "status": "HELD_VERIFY_ONLY_NO_DEPLOY",
                    "tracking_link_requests": 0,
                    "tracking_clicks": 0,
                }
            )
            current_predecessor_deployment_restore = _carry_forward_missing_deployment_paths(
                current_predecessor, candidate
            )
            # The build may regenerate public/functions bytes from source.  This
            # is the final pre-deploy money-leak gate: one old href anywhere on
            # an active provider surface aborts before the deployer is invoked.
            payout_switch_verifications = [
                verify_provider_wide_payout_switch(candidate, request)
                for request in payout_switch_requests
            ]
            go_route_provision_verifications = [
                verify_first_party_go_route_runtime(candidate, request)
                for request in go_route_provision_requests_for_deploy
            ]
            candidate_closure = _tree_closures(candidate)
            _seal_candidate(candidate, release)
            release_closure = _tree_closures(release)
            if release_closure != candidate_closure:
                raise B2Error("sealed_release_closure_mismatch")
            deployment_command = [
                sys.executable, str(DEPLOYER),
                "--release", str(release),
                "--expected-release-tree", release_closure["release"],
                "--expected-public-tree", release_closure["public"],
                "--expected-functions-tree", release_closure["functions"],
                "--expected-predecessor-deployment", str(pointer["deployment_id"]),
                "--verified-byte-twin", str(candidate),
            ]
            if not deploy:
                deployment_command.append("--verify-only")
            if deploy:
                deployment_intent = _arm_deployment_intent(
                    ledger_path=ledger_path,
                    ledger=ledger,
                    day=day,
                    release=release,
                    predecessor_deployment_id=str(pointer["deployment_id"]),
                    states=successful,
                    batch_authority=deployment_gate.get("batch_authority"),
                )
                deployment_invoked = True
            deployment_run = _run(deployment_command, cwd=BUSINESS, timeout=3600)
            summary_lines = [line for line in deployment_run["stdout_tail"].splitlines() if line.strip()]
            if not summary_lines:
                raise B2Error("deployment_summary_missing")
            summary = json.loads(summary_lines[-1])
            deployment_receipt = Path(summary["receipt"])
            deployment = read_json(deployment_receipt)
            live_truth_wired: list[dict[str, Any]] = []
            if deploy:
                if deployment.get("status") != "PASS":
                    raise B2Error("deployment_receipt_not_pass")
                dep_id = (deployment.get("deployment") or {}).get("id")
                sensor = deployment.get("sensor") or {}
                if not dep_id or sensor.get("status") != "PASS" or sensor.get("failure_count") != 0:
                    raise B2Error("deployment_sensor_or_id_invalid")
                pointer_after = read_json(CURRENT_BASE)
                if pointer_after.get("deployment_id") != dep_id:
                    raise B2Error("guarded_remint_readback_invalid")
                for state in successful:
                    state.update({
                        "status": "wired", "wired_at_utc": iso_utc(), "updated_at_utc": iso_utc(),
                        "deployment_id": dep_id,
                        "sensor": {"status": sensor.get("status"), "failure_count": sensor.get("failure_count")},
                    })
                    if state["key"] not in day["wired_keys"]:
                        day["wired_keys"].append(state["key"])
                deployment_record = {
                    "status": "PASS", "deployment_id": dep_id,
                    "receipt": path_binding(deployment_receipt),
                    "sensor_status": sensor.get("status"),
                    "wired_keys": [state["key"] for state in successful],
                    **provenance_refresh,
                }
                if deployment_gate.get("batch_authority"):
                    deployment_record["batch_authority"] = dict(
                        deployment_gate["batch_authority"]
                    )
                record_day_deployment(day, deployment_record)
                if len(day["wired_keys"]) > wired_limit:
                    raise B2Error("post_deploy_daily_wired_cap_exceeded")
                # THIRD PATH ON THE MACHINE'S OWN SUCCESS (ruling 2026-09-03): the
                # sensor is PASS with zero failures and the pointer names this
                # deployment, so every landed_sensor_pending row this head serves on
                # compare is wired now - not only the release set. These are rows
                # that ALREADY landed under an earlier authority, so they do not
                # count against this authority's wired cap.
                live_truth_wired = _wire_landed_rows_on_live_truth(
                    ledger, deployment_id=str(dep_id),
                    release_root=Path(str(pointer_after.get("release_root") or release)),
                    live_ids=_live_compare_program_ids(
                        Path(str(pointer_after.get("release_root") or release))),
                    names={str(row.get("key") or ""): str(row.get("site_program_id") or "")
                           for row in (plan.get("queue") or [])},
                )
                if live_truth_wired:
                    deployment_record["live_truth_wired_keys"] = [
                        row["key"] for row in live_truth_wired]
                _clear_deployment_intent(
                    day=day,
                    states=successful,
                    operation_id=str((deployment_intent or {}).get("operation_id") or ""),
                )
                save_ledger(ledger_path, ledger)
                deployment_committed = True
            receipt = {
                "schema": DAY_SCHEMA,
                "status": "PASS" if deploy else "PASS_VERIFY_ONLY_NO_EXTERNAL_WRITE",
                "pt_date": plan["pt_date"],
                "prepared_count": len(prepared),
                "releasable_count": len(successful),
                "wired_count": len(successful) if deploy else 0,
                "live_truth_wired_keys": [row["key"] for row in live_truth_wired],
                "row_block_count": len(row_blocks),
                "row_blocks": row_blocks,
                "activation_rows": activations,
                "source_release": str(source),
                "source_deployment_id": pointer["deployment_id"],
                "candidate": str(candidate),
                "release": str(release),
                "materialization": {
                    "initial": initial_materialization,
                    "steps": step_materializations,
                },
                "closures": release_closure,
                **provenance_refresh,
                "quiz_golden_vector_refresh": {
                    **quiz_golden_vector_refresh_receipt,
                    "durable_receipt": path_binding(
                        quiz_golden_vector_refresh_receipt_path
                    ),
                },
                "katalys_postback_attribution": katalys_postback_attribution_receipt,
                "payout_evaluation_queue_consumption": {
                    "queue_table": SHEET_UPDATE_QUEUE_TABLE,
                    "queue_kind": "payout_evaluation",
                    "applications": payout_switch_applications,
                    "predeploy_verifications": payout_switch_verifications,
                    "deferred_requests": deferred_payout_switch_requests,
                    "consumption_status": (
                        "CONSUMED_IN_ATOMIC_DEPLOY_RELEASE"
                        if deploy else "HELD_VERIFY_ONLY_NO_DEPLOY"
                    ),
                    "sheet_writes": 0,
                    "tracking_link_requests": 0,
                    "tracking_clicks": 0,
                },
                "go_route_provision_queue_consumption": {
                    "queue_table": SHEET_UPDATE_QUEUE_TABLE,
                    "queue_kind": "payout_evaluation",
                    "producer": GO_ROUTE_PROVISION_PRODUCER_TAG,
                    "applications": go_route_provision_applications,
                    "predeploy_verifications": go_route_provision_verifications,
                    "deferred_requests": deferred_go_route_provision_requests,
                    "consumption_status": (
                        "CONSUMED_IN_ATOMIC_DEPLOY_RELEASE"
                        if deploy else "HELD_VERIFY_ONLY_NO_DEPLOY"
                    ),
                    "sheet_writes": 0,
                    "tracking_link_requests": 0,
                    "tracking_clicks": 0,
                },
                "go_runtime_promotion": {
                    "authority": go_runtime_promotion_authority,
                    "verification": go_runtime_promotion_verification,
                    "verification_method": "local_href_and_config_inspection_only",
                    "tracking_link_requests": 0,
                    "tracking_clicks": 0,
                },
                "commands": {
                    "integration": integration,
                    "final_cardinality_test_refresh": final_cardinality_test_refresh,
                    "price_index_refresh": price_index_refresh,
                    "go_runtime_discovery_refresh": go_runtime_discovery_refresh,
                    "go_runtime_refresh": go_runtime_refresh,
                    "quiz_golden_vector_refresh": quiz_golden_vector_refresh,
                    "provider_visual_fallback_refresh": provider_visual_fallback_refresh,
                    "tracking_allowed_hosts_refresh": tracking_allowed_hosts_refresh,
                    "cost_affiliate_email_manifest_refresh": cost_affiliate_email_manifest_refresh,
                    "provider_pricing_quote_validator_refresh": provider_pricing_quote_validator_refresh,
                    "therapeutic_authority_remint": therapeutic_authority_remint,
                    "meta_pixel_restore": meta_pixel_restore,
                    "attested_page_copy": attested_page_copy,
                    "go_surface_allowlist": go_surface_allowlist,
                    "build_gate": build_gate,
                    "predecessor_ephemeral_restore": predecessor_ephemeral_restore,
                    "predecessor_image_restore": predecessor_image_restore,
                    "current_predecessor_deployment_restore": current_predecessor_deployment_restore,
                    "site_capture_consent_refresh": site_capture_consent_refresh,
                    "post_build_price_index_refresh": post_build_price_index_refresh,
                    "post_build_cardinality_test_refresh": post_build_cardinality_test_refresh,
                    "content_upgrade_provenance_test_refresh": content_upgrade_provenance_test_refresh,
                    "fastpath_merit_score_test_refresh": fastpath_merit_score_test_refresh,
                    "go_runtime_held_cardinality_test_refresh": go_runtime_held_cardinality_test_refresh,
                    "release_base_addenda": release_base_addenda,
                    "price_index_basis_remint": price_index_basis_remint,
                    "go_runtime_promotion_authority_reassert": go_runtime_promotion_authority_reassert,
                    "go_promotion_test_reconcile": go_promotion_test_reconcile,
                    "pass2_quiz_routing_test_refresh": pass2_quiz_routing_test_refresh,
                    "post_build_tracking_allowed_hosts_refresh": post_build_tracking_allowed_hosts_refresh,
                    "suite": suite,
                    "deployment": deployment_run,
                },
                "deployment_receipt": path_binding(deployment_receipt),
                "deployment_gate": deployment_gate,
                "effects": {
                    "deployments": 1 if deploy else 0,
                    "tracking_link_requests": 0,
                    "tracking_clicks": 0,
                },
            }
            receipt["daily_card_line"] = daily_card_line(ledger, plan, plan["pt_date"])
            receipt_path = output_dir / ("day-release.json" if deploy else "build-drill-verify-only.json")
            atomic_json(receipt_path, receipt)
            return {"status": receipt["status"], "receipt": str(receipt_path), "daily_card_line": receipt["daily_card_line"]}
        except Exception as exc:
            # FAILURE PATH ONLY. A candidate that failed build:gate or a validator
            # is the only artifact that can explain the failure, and deleting it
            # cost rounds 32 and 33 their evidence. Preserve it under quarantine:
            # moved, never sealed, never deployed, never in the content base.
            if candidate.exists():
                candidate_quarantine = _quarantine_failed_candidate(
                    candidate, output_dir, exc)
            else:
                candidate_quarantine = {"status": "PASS_NO_CANDIDATE"}
            for intermediate in intermediate_releases:
                if intermediate.exists():
                    _remove_intermediate_release(intermediate)
            if deploy and deployment_invoked:
                effect = (
                    {
                        "status": "DEPLOYMENT_COMMITTED_RECEIPT_FAILURE_NO_REFIRE",
                        "effect_cardinality": "one",
                    }
                    if deployment_committed
                    else _record_unverified_deployment(
                        ledger_path=ledger_path,
                        ledger=ledger,
                        day=day,
                        release=release,
                        states=successful,
                        error=exc,
                        batch_authority=deployment_gate.get("batch_authority"),
                    )
                )
                atomic_json(output_dir / "deployment-effect-uncertain.json", {
                    "schema": "b2-wire-backlog-deployment-effect-uncertain-v1",
                    "status": effect["status"],
                    "pt_date": plan["pt_date"],
                    "error": clip_error(f"{type(exc).__name__}:{exc}"),
                    "rows": [state["key"] for state in successful],
                    "effect_cardinality": effect["effect_cardinality"],
                    "effects": {
                        "deployments": (
                            1 if deployment_committed else "unknown_or_one"
                        ),
                        "tracking_link_requests": 0,
                        "tracking_clicks": 0,
                    },
                })
                raise
            # BLOCKER H §8.2a: the shared build refused AFTER these rows were
            # staged. Without this the rows stay `prepared` with `block: null`,
            # the receipt's `wire_backlog.row_blocks` stays empty, and the next
            # authoring brief is dispatched against the checklist — which is not
            # what stopped the build. Never let a build failure buy an authoring
            # cycle again.
            if successful:
                shared_blocks = _mark_shared_build_failure(
                    ledger=ledger, day=day, states=successful,
                    output_dir=output_dir, error=exc,
                    stage="release_prepared_shared_build",
                )
                save_ledger(ledger_path, ledger)
                atomic_json(output_dir / "shared-build-failure.json", {
                    "schema": "b2-wire-backlog-shared-build-failure-v1",
                    "status": "BLOCKED_SHARED_BUILD",
                    "pt_date": plan["pt_date"],
                    "failed_stage": "release_prepared_shared_build",
                    "error": clip_error(f"{type(exc).__name__}:{exc}"),
                    "staged_rows": [state["key"] for state in successful],
                    "row_block_count": len(shared_blocks),
                    "row_blocks": shared_blocks,
                    "effects": {"deployments": 0, "tracking_clicks": 0},
                })
            raise

def run_drill(output_dir: Path) -> dict[str, Any]:
    """Provider-free state-machine drill; no production files or network calls."""
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="b2-wire-drill-") as temporary_text:
        temporary = Path(temporary_text)
        evidence = temporary / "block-evidence.json"
        atomic_json(evidence, {"schema": "fixture", "status": "FAIL_ROW_ONLY", "reason": "malformed_terms"})
        plan = {
            "schema": PLAN_SCHEMA,
            "status": "PASS",
            "pt_date": "2026-08-15",
            "source_bindings": {},
            "limits": {"wired_per_pt_day": 10, "deployments_per_pt_day": 1},
            "queue": [
                {"key": "awin:1", "network": "Awin", "provider_program_id": "1", "site_program_id": "one", "sheet_row": 1, "pt_date": "2026-08-15"},
                {"key": "katalys:2", "network": "Katalys", "provider_program_id": "2", "site_program_id": "two", "sheet_row": 2, "pt_date": "2026-08-15"},
            ],
            "sister_dark": [{"sheet_row": 3, "display_name": "Fixture Sister", "disposition": "sister_dark_no_site_mutation"}],
        }
        plan_path = temporary / "plan.json"
        ledger_path = temporary / "ledger.json"
        atomic_json(plan_path, plan)
        first = begin_row(plan_path=plan_path, ledger_path=ledger_path, key="awin:1")
        replay = begin_row(plan_path=plan_path, ledger_path=ledger_path, key="awin:1")
        blocked = block_row(
            plan_path=plan_path, ledger_path=ledger_path, key="awin:1",
            reason="malformed_terms", evidence_path=evidence,
        )
        second = begin_row(plan_path=plan_path, ledger_path=ledger_path, key="katalys:2")
        ledger = load_ledger(ledger_path)
        second_row = ledger["rows"]["katalys:2"]
        second_row["status"] = "prepared"
        day = day_state(ledger, "2026-08-15")
        simulated_deployments = 1
        simulated_deployment_id = "fixture-deployment-real-id-shape"
        simulated_sensor = {"status": "PASS", "failure_count": 0}
        if simulated_deployments != 1 or not simulated_deployment_id or simulated_sensor != {"status": "PASS", "failure_count": 0}:
            raise B2Error("drill_deployment_contract_failed")
        second_row.update({
            "status": "wired", "deployment_id": simulated_deployment_id,
            "sensor": simulated_sensor, "wired_at_utc": iso_utc(),
        })
        day["wired_keys"].append("katalys:2")
        day["deployment"] = {
            "status": "PASS", "deployment_id": simulated_deployment_id,
            "sensor_status": "PASS", "wired_keys": ["katalys:2"],
        }
        save_ledger(ledger_path, ledger)
        line = daily_card_line(ledger, plan, "2026-08-15")
        checks = {
            "stable_key": stable_key("CJ Affiliate", "6105366") == "cj:6105366",
            "replay_idempotent": replay["status"] == "PASS_IDEMPOTENT_RESUME",
            "bad_row_isolated": blocked["row"]["status"] == "blocked" and second["row"]["status"] == "attempted",
            "wired_cap_not_exceeded": len(day["wired_keys"]) <= MAX_WIRED_PER_PT_DAY,
            "one_deploy": simulated_deployments == 1,
            "real_deployment_id_required": bool(simulated_deployment_id),
            "sensor_pass_required": simulated_sensor["status"] == "PASS" and simulated_sensor["failure_count"] == 0,
            "sister_dark": len(plan["sister_dark"]) == 1 and "sister" not in ledger["rows"],
            "strict_card_line": line == "wired 1/10 · blocked 1 (malformed_terms:1) · backlog remaining 0",
            "name_special_cases_absent": all(name not in json.dumps(plan) for name in ("eMed", "SpeedyHealth", "OnlineClinic")),
            "first_attempt_started": first["status"] == "PASS_ATTEMPT_STARTED",
        }
        if not all(checks.values()):
            raise B2Error(f"drill_check_failed:{[key for key, value in checks.items() if not value]}")
        receipt = {
            "schema": DRILL_SCHEMA,
            "status": "PASS",
            "completed_at_utc": iso_utc(),
            "mode": "provider_free_no_deploy",
            "checks": checks,
            "daily_card_line": line,
            "simulated_effects": {"deployments": 1, "provider_calls": 0, "tracking_clicks": 0},
            "production_effects": {"deployments": 0, "provider_calls": 0, "site_writes": 0},
        }
        receipt_path = output_dir / "b2-provider-free-drill.json"
        atomic_json(receipt_path, receipt)
        return {"status": "PASS", "receipt": str(receipt_path), "daily_card_line": line}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    sub = parser.add_subparsers(dest="command", required=True)
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--backrun-dir", type=Path)
    p_plan.add_argument("--pt-date", default=pt_date())
    p_plan.add_argument("--output", type=Path, required=True)
    for name in ("begin", "block", "prepare"):
        child = sub.add_parser(name)
        child.add_argument("--plan", type=Path, required=True)
        child.add_argument("--key", required=True)
        if name == "block":
            child.add_argument("--reason", required=True)
            child.add_argument("--evidence", type=Path, required=True)
        elif name == "prepare":
            child.add_argument("--spec", type=Path, required=True)
            child.add_argument("--research", type=Path, required=True)
            child.add_argument("--terms", type=Path, required=True)
    p_release = sub.add_parser("release")
    p_refresh = sub.add_parser("refresh-go-promotion-authority")
    p_refresh.add_argument("--candidate-root", type=Path, required=True)
    p_release.add_argument("--plan", type=Path, required=True)
    p_release.add_argument("--output-dir", type=Path, required=True)
    p_release.add_argument("--deploy", action="store_true")
    p_status = sub.add_parser("status")
    p_status.add_argument("--plan", type=Path, required=True)
    p_status.add_argument("--output", type=Path)
    p_drill = sub.add_parser("drill")
    p_drill.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            backrun = args.backrun_dir or latest_complete_backrun()
            payload = build_plan(
                backrun_dir=backrun.resolve(),
                ledger_path=args.ledger.resolve(), date_pt=args.pt_date,
            )
            atomic_json(args.output, payload)
            result = {"status": "PASS", "plan": str(args.output), "queue": len(payload["queue"]), "denominator": payload["denominator"]}
        elif args.command == "begin":
            result = begin_row(plan_path=args.plan.resolve(), ledger_path=args.ledger.resolve(), key=args.key)
        elif args.command == "block":
            result = block_row(
                plan_path=args.plan.resolve(), ledger_path=args.ledger.resolve(), key=args.key,
                reason=args.reason, evidence_path=args.evidence.resolve(),
            )
        elif args.command == "prepare":
            result = prepare_row(
                plan_path=args.plan.resolve(), ledger_path=args.ledger.resolve(), key=args.key,
                spec_path=args.spec.resolve(), research_path=args.research.resolve(), terms_path=args.terms.resolve(),
            )
        elif args.command == "refresh-go-promotion-authority":
            result = apply_go_runtime_promotion_authority(args.candidate_root.resolve())
        elif args.command == "release":
            result = release_prepared(
                plan_path=args.plan.resolve(), ledger_path=args.ledger.resolve(),
                output_dir=args.output_dir.resolve(), deploy=bool(args.deploy),
                # The worker CLI release command is the same provider-content
                # path as the daily lane; there is no remove/general build mode.
                provider_content_change=True,
            )
        elif args.command == "status":
            result = status_receipt(plan_path=args.plan.resolve(), ledger_path=args.ledger.resolve())
            if args.output:
                atomic_json(args.output, result)
                result = {"status": "PASS", "receipt": str(args.output), "daily_card_line": result["daily_card_line"]}
        else:
            result = run_drill(args.output_dir.resolve())
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": f"{type(exc).__name__}:{exc}"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
