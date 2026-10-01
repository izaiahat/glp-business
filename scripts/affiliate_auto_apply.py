#!/usr/bin/env python3
"""Affiliate discovery + auto-apply engine (daily phase 4 of the machine).

Operator authority 2026-08-06: *"the affiliate machine should also find new
programs to apply for as part of the process that runs every day, and
automatically apply with the standard info... we need to fully leverage
[our marketplaces] and apply for more as well."*

Two modes:
  --plan     (read-only) Build today's application plan: pull every not-joined
             program from the freshest network inventories, run the operator's
             US+niche filter, drop anything already on the Sheet or already
             applied to (append-only applications ledger), rank, cap.
  --execute  Apply. Networks with a PROVEN programmatic join adapter execute
             directly; everything else lands on the portal/native task list the
             lane executes the same day with the standard application profile.
             Every attempt is appended to the ledger BEFORE the attempt so a
             crash can never double-apply.

The filter decision gates every application — a program outside the US/niche
policy cannot be applied to by any route. Receipts 0600 under the machine dir.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")
CONFIG_PATH = BUSINESS / "config/affiliate-sites/glp-clinic-scout.json"
MACHINE_DIR = BUSINESS / "reports/glp-clinic-scout/affiliate-machine"
LEDGER_PATH = MACHINE_DIR / "applications-ledger.json"

# Sheet `Vertical` values the GLP site may file applications for. Everything
# else on the Sheet is a sister-vertical row waiting for its own job.
GLP_SHEET_VERTICALS = {"glp1-weightloss", "glp-1", "glp1", "glp1-weight-loss"}

# Programmatic join adapters, keyed by network. A network only lands here when
# its adapter targets a DOCUMENTED provider mutation endpoint — guessing
# mutation endpoints is how accounts get banned. Portal execution covers the
# gap for every other network. Populated from the adapter registry
# (scripts/marketplace_adapters), which today proves exactly one:
#   partnerize -> POST /v2/publishers/{pid}/campaign-requests
# The import is defensive: a broken adapter package must degrade this engine to
# portal-only, never crash the daily run.
try:
    sys.path.insert(0, str(BUSINESS))
    from scripts import marketplace_adapters as _adapters

    JOIN_ADAPTERS: dict = _adapters.join_adapters()
except Exception:  # noqa: BLE001 - portal route is the safe fallback
    JOIN_ADAPTERS = {}

# API-key-first (operator standing rule 2026-08-06: "the affiliate machine never
# hits a blocker"). Every successful application/signup to a marketplace we hold
# no API credential for owes the portal lane a key-minting followup, so the next
# run reaches that network over an API instead of a browser. The import is
# defensive for the same reason as above: no queue must never mean no apply.
try:
    from scripts.marketplace_adapters import portal_lane as _portal_lane
except Exception:  # noqa: BLE001
    _portal_lane = None


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


nfilter = _load("affiliate_niche_geo_filter", BUSINESS / "scripts/affiliate_niche_geo_filter.py")
rulings = _load("affiliate_operator_rulings", BUSINESS / "scripts/affiliate_operator_rulings.py")
# Cached advertiser site evidence. Import is pure — `candidate_site()` reads the
# cache the harvest job (scripts/affiliate_site_evidence.py) fills, so planning
# never touches the network.
site_evidence = _load("affiliate_site_evidence", BUSINESS / "scripts/affiliate_site_evidence.py")
contract_policy = _load("affiliate_contract_policy", BUSINESS / "scripts/affiliate_contract_policy.py")


def bind_direct_application_evidence(candidate: dict, config: dict) -> dict:
    """Attach only an explicitly configured immutable evidence descriptor."""
    candidate = dict(candidate)
    if str(candidate.get("network") or "").lower() != "direct":
        return candidate
    key = f"direct:{candidate.get('program_id', '')}"
    bindings = ((config.get("auto_apply") or {}).get("direct_application_evidence") or {})
    binding = bindings.get(key)
    if isinstance(binding, dict):
        candidate["direct_application_evidence"] = dict(binding)
    return candidate


def direct_application_gate(candidate: dict, *, operation: str) -> dict:
    """Shared class-level direct surface/terms gate; provider-free and fail-closed."""
    return contract_policy.direct_application_eligibility(candidate, operation=operation)


def compact_direct_gate_receipt(gate: dict) -> dict:
    return {key: gate.get(key) for key in (
        "schema", "decision", "operational", "operation", "candidate", "reason_codes",
        "evidence", "mechanical_trigger_hits", "legal_prohibition_hits",
        "blocked_account_action_hits",
    )}


def gate_application_candidate(candidate: dict, *, operation: str,
                               persist: bool = False) -> dict:
    """Apply the shared choke before a plan or portal/API candidate escapes."""
    candidate = dict(candidate)
    # Inventory adapters use `url` for the application surface. Direct Sheet
    # candidates already carry the explicit Apply URL. This mapping is local to
    # the application lane; the shared choke never guesses from an official URL.
    candidate.setdefault("application_url", candidate.get("apply_url") or candidate.get("url") or "")
    return nfilter.choke_candidate(candidate, require_application_url=True,
                                   operation=operation, persist=persist)


class PlanDeadlineExceeded(RuntimeError):
    """The local planning budget expired before provider execution began."""


class ExecutionDeadlineExceeded(RuntimeError):
    """The execution budget expired; committed effects may already exist."""


def require_deadline(deadline_monotonic: float | None, phase: str) -> None:
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise PlanDeadlineExceeded(f"deadline_exceeded:{phase}")


def require_execution_deadline(deadline_monotonic: float | None, phase: str) -> None:
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise ExecutionDeadlineExceeded(f"deadline_exceeded:{phase}")


def new_execution_effects() -> dict[str, int]:
    """Mutable, caller-owned accounting that survives execution exceptions."""
    return {
        "provider_mutation_attempts": 0,
        "provider_mutations": 0,
        "provider_mutations_ambiguous": 0,
        "applications_submitted": 0,
        "portal_tasks_queued": 0,
        "ledger_rows_committed": 0,
        "ledger_writes": 0,
        "api_key_followups_queued": 0,
    }


def execution_effect_snapshot(effects: dict[str, int]) -> dict[str, int]:
    """Return the stable public effect vocabulary with integer counts only."""
    return {key: int(effects.get(key) or 0) for key in new_execution_effects()}


def compact_gate_receipt(gate: dict) -> dict:
    """Keep gate proof without duplicating the complete candidate in the plan."""
    return {key: gate.get(key) for key in (
        "schema", "decision", "parking", "operational", "fingerprint",
        "reason_codes", "evidence",
    )}


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def private_json(path: Path, payload) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    path.write_text(body)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return hashlib.sha256(body.encode()).hexdigest()


def load_ledger() -> dict:
    if LEDGER_PATH.exists():
        return json.loads(LEDGER_PATH.read_text())
    return {"schema": "affiliate-applications-ledger-v1", "applications": []}


def _canonical_ledger_writer_guard(path: Path | None = None):
    """Return the shared Item E writer guard for the selected ledger path."""
    from scripts import glp_item_e_operator_intake as intake_mod

    return intake_mod.canonical_writer_guard([Path(path or LEDGER_PATH)])


# A ledger row is a double-apply guard only for an attempt that actually reached
# a provider verdict. The row is written BEFORE the attempt, so an adapter that
# raised leaves `status: "failed"` with NO `provider_receipt` — a recorded
# NON-verdict. Re-attempting one cannot submit twice, because every adapter in
# JOIN_ADAPTERS reads provider state first and returns ALREADY_RELATIONSHIP /
# NOOP_ALREADY_RELATED with write_actions == 0 when a relationship exists
# (clickdealer.apply, partnerize.apply, affiliati.apply, healthtrader.apply):
# the pre-read, not the ledger row, is the real guard for a retry. Rows that did
# reach a verdict keep their receipt and stay permanently blocked.
# Before 2026-09-17 these rows were terminal, so an adapter AttributeError on
# ClickDealer's scalar `allowed_media_types` stranded five GLP-1 telehealth
# offers (ShedRX + four TrimRX) with no registered route back.
UNREACHED_ATTEMPT_STATUSES = frozenset({"failed"})
#: A recorded non-verdict earns a bounded number of re-attempts and then goes
#: terminal on its own. Unbounded retries are how a permanently inert row (an
#: adapter whose request body this engine cannot assemble) becomes daily churn:
#: each pass appends another unreached row that is itself a retry candidate.
#: WORKER-STANDARD 7 - on a repeated identical failure, name the condition and
#: owner rather than repeating the same patch.
MAX_UNREACHED_ATTEMPTS = 2


def is_unreached_attempt(row: dict) -> bool:
    """True when this row records an API attempt that never reached a verdict.

    A hard kill leaves `status: "attempting"`, which is deliberately NOT in
    UNREACHED_ATTEMPT_STATUSES: that row may have dispatched, so it stays
    terminal. Never add it here.
    """
    return (str(row.get("route") or "") == "api"
            and str(row.get("status") or "") in UNREACHED_ATTEMPT_STATUSES
            and not row.get("provider_receipt")
            and not row.get("exclude_from_application_execution"))


def retryable_unreached_keys(rows: list[dict]) -> set[str]:
    """Keys whose ONLY ledger history is fewer than MAX_UNREACHED_ATTEMPTS non-verdicts."""
    reached: set[str] = set()
    counts: dict[str, int] = {}
    for row in rows:
        key = f"{row.get('network')}:{row.get('program_id')}"
        if is_unreached_attempt(row):
            counts[key] = counts.get(key, 0) + 1
        else:
            reached.add(key)
    return {key for key, n in counts.items()
            if key not in reached and n < MAX_UNREACHED_ATTEMPTS}


def ledger_keys(ledger: dict) -> set[str]:
    rows = ledger.get("applications", [])
    return ({f"{a['network']}:{a['program_id']}" for a in rows}
            - retryable_unreached_keys(rows))


# ---------------------------------------------------------------------------
# Per-network operator scope (config-driven, NARROWING ONLY)
# ---------------------------------------------------------------------------
# Operator decision 2026-09-16T23:58:57Z, addendum 2026-09-17T00:30:43Z
# (ops/linear/approvals/OPERATOR-DECISION-20260917-apply-scope-glp1-telehealth-only-clickdealer.json):
# apply ONLY to explicit GLP-1 / weight-loss telehealth MEDICATION programs, and
# the daily cap does not apply to ClickDealer.
#
# This gate runs AFTER the shared niche/GLP choke and can only ever REFUSE a
# candidate the choke already admitted - it can never admit one the choke
# rejected, so it cannot widen scope and the classifier stays untouched. Marketplace
# vertical labels alone are not sufficient in either direction, which is why the
# rule needs all three legs: ClickDealer files Hers, Inc. under "Health supplements
# (Ecommerce)" and Willow under "Health services (Ecommerce)" though both prescribe
# GLP-1s, while it files MIC-B12 lipotropic injections and Sermorelin peptides under
# "Telehealth - GLP-1" though neither is a GLP-1 medication.


def network_scope_for(config: dict, network: str) -> dict:
    scopes = (config.get("auto_apply") or {}).get("network_scope") or {}
    scope = scopes.get(str(network or "").strip().lower())
    return dict(scope) if isinstance(scope, dict) else {}


def _any_match(patterns, text: str) -> str:
    for pattern in patterns or []:
        try:
            if re.search(str(pattern), text, re.I):
                return str(pattern)
        except re.error:
            continue
    return ""


def network_scope_verdict(candidate: dict, scope: dict, fit: dict | None = None) -> dict:
    """Admit/refuse one already-choke-approved candidate under the operator scope.

    No configured scope means no narrowing: admit, so every other network keeps
    its existing behaviour byte for byte.
    """
    if not scope:
        return {"admit": True, "reason": "no_network_scope_configured", "scoped": False}
    program_id = str(candidate.get("program_id") or "")
    for exception in scope.get("program_exceptions") or []:
        if isinstance(exception, dict) and str(exception.get("program_id") or "") == program_id:
            return {"admit": True, "scoped": True,
                    "reason": f"operator_program_exception:{exception.get('why') or 'operator named'}"}
    tiers = scope.get("require_fit_tiers")
    tier = str((fit or {}).get("tier") or "")
    if tiers and tier not in tiers:
        return {"admit": False, "scoped": True,
                "reason": f"operator_scope_tier_not_admitted:{tier or 'none'}"}
    name = str(candidate.get("name") or "")
    verticals = [str(v) for v in (candidate.get("categories") or [])]
    vertical = " | ".join(verticals)
    # The offer's OWN words. The network's vertical label is deliberately NOT
    # folded in here: it is a separate admit path below, and mixing it in made
    # the medication-evidence leg vacuous for every "Telehealth - GLP-1" row.
    own_words = " ".join([name, str(candidate.get("description") or "")])
    text = " ".join([own_words, vertical])
    for vertical_value in verticals:
        if vertical_value in (scope.get("refuse_network_verticals") or []):
            return {"admit": False, "scoped": True,
                    "reason": f"operator_scope_network_vertical_refused:{vertical_value}"}
    hit = _any_match(scope.get("refuse_name_patterns"), name)
    if hit:
        return {"admit": False, "scoped": True,
                "reason": f"operator_scope_modality_refused_in_name:{hit}"}
    hit = _any_match(scope.get("refuse_text_patterns"), text)
    if hit:
        return {"admit": False, "scoped": True,
                "reason": f"operator_scope_modality_refused_in_text:{hit}"}
    # Two independent, network-published admit paths, because ClickDealer's
    # vertical label is wrong in BOTH directions: the offer's own words say it is
    # a GLP-1 medication programme, or the network files it under a vertical the
    # operator trusts for that. Neither path is an inference of ours.
    trusted = [v for v in verticals if v in (scope.get("admit_network_verticals") or [])]
    hit = _any_match(scope.get("require_text_patterns"), own_words)
    if hit:
        return {"admit": True, "scoped": True, "network_vertical": vertical,
                "reason": f"operator_scope_admitted_offer_text:{hit}"}
    if trusted:
        return {"admit": True, "scoped": True, "network_vertical": vertical,
                "reason": f"operator_scope_admitted_network_vertical:{trusted[0]}"}
    if scope.get("require_text_patterns") or scope.get("admit_network_verticals"):
        return {"admit": False, "scoped": True,
                "reason": "operator_scope_no_glp1_medication_evidence"}
    return {"admit": True, "scoped": True, "reason": "operator_scope_admitted",
            "network_vertical": vertical}


# ---------------------------------------------------------------------------
# Bound fit assessment (the proof the single Sheet writer requires)
# ---------------------------------------------------------------------------
# GLP-285. `affiliate_program_relevance.create_verdict` refuses to CREATE a Sheet
# row for a program whose NAME carries no niche token, because the only thing
# that separated the 2026-08-26 junk mirror from real providers was PROVENANCE.
# Its own docstring says the name is not the test: real GLP-1 providers are
# brandable (Hers, Lttl, LevelsRx, FeelGoodMeds), and a name-only rule rejected
# 8 of 18 real providers when measured. The drain therefore accepts an assessment
# instead - but until 2026-09-17 the ONLY list it would accept was one an
# operator had pinned by hand in ops/control/affiliate-apply-gate.json, so the
# 41 provider-verified ClickDealer applications submitted under the operator's
# own scope receipt could not all be recorded: 10 had no route to the Sheet.
#
# Operator 2026-09-17, in his own words: "yes add the 10 rows and fix the sheet
# job or whatever prevented this so it never happens again."
#
# So THIS program - the machine that actually submitted the applications - is now
# the registered producer of the bound list. Every run restricted to a network
# whose operator scope is receipted emits one, covering the applications this run
# submitted AND every SUBMITTED_VERIFIED application already in the ledger for
# that network (the replay/backfill path is this same entry point, never a
# separate script). The list names the operator receipt and records its sha256,
# so the drain can verify the binding without a hand-minted amendment.
FIT_LIST_SCHEMA = "glp-apply-fit-assessment-v1"
OPERATOR_DECISION_SCHEMA = "glp-operator-decision-v1"
APPROVALS_DIR = BUSINESS / "ops/linear/approvals"
FIT_LIST_NAME = "fit-assessment.json"

#: Tier -> the classifier vocabulary the Sheet writer's fit gate admits. Only the
#: tiers that are DIRECT GLP-1 evidence are CORE; an admitted adjacent tier is
#: SISTER. A tier the scope does not admit never reaches this map at all.
FIT_VERDICT_BY_TIER = {
    "explicit_glp1": "CORE",
    "site_glp1": "CORE",
    "sheet_vertical_glp1": "CORE",
    "bridge_ruling": "CORE",
}


def apply_queue_id(network: str, program_id: str) -> str:
    """The queue id this producer authorizes for one application.

    Derived from the identity itself and re-derived by the drain, so a list can
    never carry an authorization minted for a different program.
    """
    return f"apply-{str(network or '').strip().lower()}-{program_id}"


def scope_receipt_for(config: dict) -> dict | None:
    """The operator scope receipt the per-network apply scope cites, plus its sha.

    Returns None when the config cites nothing readable - in which case this
    producer emits no list and every network keeps its previous behaviour.
    """
    scopes = (config.get("auto_apply") or {}).get("network_scope") or {}
    ref = str(scopes.get("_authority") or "").strip()
    if not ref:
        return None
    path = BUSINESS / ref
    if not path.is_file() or path.resolve().parent != APPROVALS_DIR.resolve():
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if str(doc.get("schema") or "") != OPERATOR_DECISION_SCHEMA:
        return None
    return {"path": ref,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "operator_words": str(doc.get("operator_words") or ""),
            "when": str(doc.get("when") or "")}


def receipted_scope_networks(config: dict, networks: set[str] | None) -> list[str]:
    """Networks in this run that carry a configured, receipted apply scope."""
    scopes = (config.get("auto_apply") or {}).get("network_scope") or {}
    named = {name for name, scope in scopes.items()
             if not name.startswith("_") and isinstance(scope, dict) and scope}
    if networks:
        named &= {str(n).strip().lower() for n in networks}
    return sorted(named)


def assess_submitted_application(row: dict, config: dict, *, policy_snapshot=None,
                                 evidence_cache=None) -> dict:
    """Re-run the real classifier over one already-submitted application.

    The verdict is the CLASSIFIER'S, from the offer's own network-published
    words the ledger preserved - never this function's opinion and never the
    program's name. A submitted application the current scope no longer admits is
    reported as refused with its reason instead of being quietly admitted.
    """
    candidate = {
        "name": row.get("name", ""), "network": row.get("network", ""),
        "program_id": row.get("program_id", ""),
        "description": row.get("description", ""), "region": row.get("region", ""),
        "categories": list(row.get("categories") or []),
        "currency": row.get("currency", ""), "url": row.get("url", ""),
        "application_url": row.get("application_url", ""),
    }
    decision, verdict = nfilter.evaluate_application(
        nfilter.Candidate(
            name=candidate["name"], network=candidate["network"],
            program_id=candidate["program_id"], description=candidate["description"],
            region=candidate["region"], categories=tuple(candidate["categories"]),
            network_region=str(row.get("network_region") or ""),
            currency=candidate["currency"], url=candidate["url"],
            site=(site_evidence.candidate_site(candidate, evidence_cache)
                  if evidence_cache is not None else None)),
        policy_snapshot=policy_snapshot)
    fit = {"fit": verdict.fit, "tier": verdict.tier, "reasons": verdict.reasons}
    # THE CLASSIFIER IS THE NICHE AUTHORITY, and its refusal is terminal. It
    # carries the permanent exclusions (Fella) and the operator's standing
    # program rulings: ColonBroom is SKIPPED_OPERATOR_RULING here - "Fiber
    # supplement in the Diet & Weight Loss vertical, not a GLP-1 telehealth
    # medication program" - so a held program can never be listed as a fit.
    if not decision.apply_ok:
        return {"admit": False, "reason": f"classifier:{decision.decision}",
                "tier": verdict.tier, "reasons": verdict.reasons[:4]}
    # The per-network operator scope is an APPLY-TIME narrowing: which offers the
    # machine may submit to today. It is recorded here as evidence and is
    # deliberately NOT a veto, because every row in this list has already been
    # submitted and provider-verified - there is no application left for it to
    # gate, and the question the Sheet's writer actually asks is "does this
    # program belong to our niche?", not "would we apply to it again today?".
    # Making it a veto reintroduced the exact failure this work exists to end:
    # Goby Meds (clickdealer 178044:251543 and 178299:251824) is a GLP-1
    # telehealth clinic filed under "Telehealth - GLP-1" whose blurb lists NAD+
    # among the other treatments it offers, so the scope's `\bnad\+` modality
    # pattern refused a real, already-applied-to GLP-1 provider on its TEXT.
    scope_verdict = network_scope_verdict(
        candidate, network_scope_for(config, candidate["network"]), fit)
    return {"admit": True, "tier": verdict.tier,
            "verdict": FIT_VERDICT_BY_TIER.get(verdict.tier, "SISTER"),
            "lane": decision.lane, "reasons": verdict.reasons[:4],
            "operator_scope_admit": bool(scope_verdict["admit"]),
            "operator_scope_reason": scope_verdict["reason"],
            "network_vertical": scope_verdict.get("network_vertical", "")}


def fit_assessment_payload(config: dict, ledger: dict, networks: list[str],
                           receipt: dict, *, policy_snapshot=None,
                           evidence_cache=None) -> dict:
    """Build the bound fit-assessment list for one run's receipted networks."""
    programs, refused = [], []
    for row in ledger.get("applications") or []:
        if not isinstance(row, dict):
            continue
        network = str(row.get("network") or "").strip().lower()
        if network not in networks:
            continue
        provider_receipt = row.get("provider_receipt")
        if (str(row.get("status") or "") != "SUBMITTED_VERIFIED"
                or not isinstance(provider_receipt, dict)
                or str(provider_receipt.get("status") or "") != "SUBMITTED_VERIFIED"
                or not str(provider_receipt.get("campaign_id") or "")):
            continue
        program_id = str(row.get("program_id") or "")
        assessment = assess_submitted_application(
            row, config, policy_snapshot=policy_snapshot, evidence_cache=evidence_cache)
        identity = {"network": network, "program_id": program_id,
                    "program_name": str(row.get("name") or "")}
        if not assessment["admit"]:
            refused.append({**identity, "reason": assessment["reason"],
                            "tier": assessment.get("tier", ""),
                            "classifier_reasons": assessment.get("reasons") or []})
            continue
        programs.append({
            **identity,
            "queue_id": apply_queue_id(network, program_id),
            "classifier": {
                "verdict": assessment["verdict"],
                "tier": assessment["tier"],
                "lane": assessment.get("lane") or "",
                "reasons": assessment.get("reasons") or [],
                "operator_scope_admit": assessment.get("operator_scope_admit"),
                "operator_scope_reason": assessment.get("operator_scope_reason", ""),
                "network_vertical": assessment.get("network_vertical", ""),
                "assessed_by": "scripts/affiliate_auto_apply.py"
                               " (affiliate_niche_geo_filter.evaluate_application"
                               " + network_scope_verdict)",
            },
            "application_receipt": {
                "campaign_id": str((row.get("provider_receipt") or {}).get("campaign_id") or ""),
                "submitted_at_utc": str(
                    (row.get("provider_receipt") or {}).get("submitted_at_utc") or ""),
                "status": "SUBMITTED_VERIFIED",
            },
        })
    return {
        "schema": FIT_LIST_SCHEMA,
        "assessed_at_utc": utc(),
        "network": networks[0] if len(networks) == 1 else "",
        "networks": networks,
        "scope_receipt": receipt["path"],
        "scope_receipt_sha256": receipt["sha256"],
        "scope_receipt_when": receipt["when"],
        "producer": "scripts/affiliate_auto_apply.py",
        "count": len(programs),
        "queue_ids_authorized": sorted({row["queue_id"] for row in programs}),
        "programs": programs,
        "refused": refused,
        "refused_count": len(refused),
        "note": "One row per provider-verified application this machine submitted under "
                "the named operator scope receipt. The verdict is the classifier's, "
                "re-derived from the offer's own network-published words; a program the "
                "classifier refuses - a permanent exclusion or a standing operator HOLD - "
                "is listed under `refused` and is NOT authorized. Each row also carries "
                "today's apply-scope verdict as evidence; that scope gates APPLYING, not "
                "niche fit, and never vetoes a program already submitted and "
                "provider-verified. Consumed by scripts/glp_sheet_sync_drain.py, which re-checks "
                "this file's sha256, the receipt's sha256, the exactly-one-row match, the "
                "CORE/SISTER verdict and the derived queue id before it may set "
                "fit_verified on a Sheet row.",
        "secret_printed": False,
    }


def emit_fit_assessment(config: dict, out_dir: Path, networks: set[str] | None) -> dict:
    """Write this run's bound fit list and bind each covered ledger row to it.

    Idempotent and provider-free: it reads the ledger, writes one immutable list
    under the run's own evidence directory, and records `fit_assessment` on each
    covered application so the Sheet queue producer can carry the reference
    without re-deriving anything.
    """
    scoped = receipted_scope_networks(config, networks)
    if not scoped:
        return {"status": "NO_RECEIPTED_SCOPE_NETWORK_IN_RUN",
                "networks_requested": sorted(networks or [])}
    receipt = scope_receipt_for(config)
    if receipt is None:
        return {"status": "NO_READABLE_OPERATOR_SCOPE_RECEIPT", "networks": scoped}
    evidence_cache = site_evidence.load_cache()
    policy_snapshot = rulings.load_snapshot()
    with _canonical_ledger_writer_guard(LEDGER_PATH):
        ledger = load_ledger()
        payload = fit_assessment_payload(
            config, ledger, scoped, receipt,
            policy_snapshot=policy_snapshot, evidence_cache=evidence_cache)
        out_dir.mkdir(parents=True, exist_ok=True)
        sha = private_json(out_dir / FIT_LIST_NAME, payload)
        rel = str((out_dir / FIT_LIST_NAME).resolve().relative_to(BUSINESS))
        authorized = {f"{row['network']}:{row['program_id']}": row for row in payload["programs"]}
        bound = cleared = 0
        for row in ledger.get("applications") or []:
            if not isinstance(row, dict):
                continue
            network = str(row.get("network") or "").strip().lower()
            if network not in scoped:
                continue
            key = f"{network}:{row.get('program_id')}"
            listed = authorized.get(key)
            if listed is None or str(row.get("status") or "") != "SUBMITTED_VERIFIED":
                # A binding this run did NOT re-issue is STALE and is removed.
                # Without this a program the operator holds tomorrow would keep
                # yesterday's reference to a list that still calls it a fit, and
                # the hold would never reach the Sheet's writer. A fit proof is a
                # current fact, not an append-only log.
                if row.pop("fit_assessment", None) is not None:
                    cleared += 1
                continue
            row["fit_assessment"] = {
                "fit_assessment_path": rel, "fit_assessment_sha256": sha,
                "queue_id": listed["queue_id"],
                "verdict": listed["classifier"]["verdict"],
                "assessed_at_utc": payload["assessed_at_utc"],
                "scope_receipt": receipt["path"],
            }
            bound += 1
        if bound or cleared:
            private_json(LEDGER_PATH, ledger)
    return {"status": "EMITTED", "path": rel, "sha256": sha,
            "networks": scoped, "scope_receipt": receipt["path"],
            "scope_receipt_sha256": receipt["sha256"],
            "programs": payload["count"], "refused": payload["refused_count"],
            "refused_rows": payload["refused"], "ledger_rows_bound": bound,
            "stale_bindings_cleared": cleared}


def uncapped_networks(config: dict) -> set[str]:
    """Networks whose per-network operator receipt removes the daily cap."""
    scopes = (config.get("auto_apply") or {}).get("network_scope") or {}
    return {name for name, scope in scopes.items()
            if isinstance(scope, dict) and scope.get("no_daily_cap")}


def unreached_attempt_candidates(ledger: dict,
                                 networks: set[str] | None = None,
                                 limit: int | None = None) -> list[dict]:
    """The ledger's recorded NON-verdicts, ready to be re-attempted.

    A key is eligible only while no row for it ever reached a provider verdict
    and it has fewer than MAX_UNREACHED_ATTEMPTS non-verdicts, so a success, a
    refusal or repetition all retire it. `limit` bounds one invocation to the
    daily cap: a retry re-attempts work already counted, but it must not become
    an unmetered second application channel.
    """
    rows = ledger.get("applications", [])
    retryable = retryable_unreached_keys(rows)
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        key = f"{row.get('network')}:{row.get('program_id')}"
        if key not in retryable or key in seen:
            continue
        if networks and str(row.get("network")) not in networks:
            continue
        seen.add(key)
        out.append({k: v for k, v in row.items()
                    if k not in {"route", "status", "error", "planned_at_utc"}})
        if limit is not None and len(out) >= limit:
            break
    return out


def freshest_inventories() -> list[Path]:
    """Newest inventory file per network across machine + enumeration dirs."""
    best: dict[str, Path] = {}
    for pattern in ("backrun-*/marketplace/*.json", "enumeration-*/*.json",
                    # unified adapter readbacks (scripts/marketplace_adapters):
                    # <net>-readback.json in a marketplace-adapters-<stamp> dir
                    "marketplace-adapters-*/*-readback.json"):
        for p in MACHINE_DIR.glob(pattern):
            if p.name.startswith("_"):
                continue
            net = p.stem.split("-")[0]
            if net not in best or p.stat().st_mtime > best[net].stat().st_mtime:
                best[net] = p
    return list(best.values())


def candidates_from_awin(doc: dict) -> list[dict]:
    out = []
    for pub_id, buckets in (doc.get("relationships") or {}).items():
        for prog in (buckets.get("notjoined") or []):
            region = (prog.get("primaryRegion") or {})
            out.append({
                "network": "awin", "program_id": str(prog.get("id") or ""),
                "name": str(prog.get("name") or ""),
                "description": str(prog.get("description") or "")[:500],
                "region": str(region.get("countryCode") or ""),
                "currency": str(prog.get("currencyCode") or ""),
                "url": str(prog.get("displayUrl") or ""),
                # Awin publishes its own sector taxonomy per programme and the
                # filter was never shown it, so 3,477 "Health & Beauty" rows
                # were classified from the blurb alone.
                "categories": [c for c in [str(prog.get("primarySector") or "")] if c],
                "apply_route": "portal",
            })
    return out


def candidates_from_generic(doc: dict, network: str) -> list[dict]:
    out = []
    for prog in (doc.get("programs") or []):
        rel = str(prog.get("relationship") or "").lower()
        if rel in {"joined", "approved", "active"}:
            continue
        # never re-apply into a closed door
        if rel in {"rejected", "declined", "suspended"}:
            continue
        # ...nor into an open one. A pending/invited relationship IS an
        # application already in flight; only the local ledger used to catch
        # these, and it does not know about the 66 Katalys applications filed
        # before the ledger existed.
        if rel in {"pending", "invited", "applied", "requested"}:
            continue
        # Open-marketplace adapters (ClickBank, Digistore24) report a single
        # account-level pseudo-program: there is nothing to apply to.
        if str(prog.get("program_id") or "") == "network-account" or rel.startswith("active_account"):
            continue
        out.append({
            "network": network, "program_id": str(prog.get("program_id") or prog.get("id") or ""),
            "name": str(prog.get("name") or ""),
            "description": str(prog.get("description") or "")[:500],
            "region": str(prog.get("region") or ""),
            # The network's own country assertion, when the program record has
            # no per-program region (Katalys tags every offer, Rakuten does not).
            "network_region": str(prog.get("network_region") or doc.get("network_region") or ""),
            "categories": [str(x) for x in (prog.get("categories") or []) if str(x).strip()],
            "currency": str(prog.get("currency") or ""),
            "url": str(prog.get("url") or ""),
            # carried through so a documented join API (partnerize campaign
            # requests) has the advertiser identity its request body requires
            "advertiser_id": str(prog.get("advertiser_id") or ""),
            "group_slug": str(prog.get("group_slug") or ""),
            "application_meta": (dict(prog.get("application_meta"))
                                 if isinstance(prog.get("application_meta"), dict) else {}),
            "application_url": str(prog.get("application_url") or prog.get("url") or ""),
            "apply_route": "api" if network in JOIN_ADAPTERS else "portal",
        })
    return out


def discover(config: dict, networks: set[str] | None = None) -> tuple[list[dict], dict]:
    counts: dict[str, int] = {}
    pool: list[dict] = []
    for path in freshest_inventories():
        if networks and path.stem.split("-")[0] not in networks:
            continue
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        net = path.stem.split("-")[0]
        # Dispatch on document SHAPE, not on network name: awin appears both as
        # the legacy relationship-bucketed inventory and as the unified adapter
        # readback (a flat programs list). Keying on the name silently zeroed
        # awin the moment an adapter readback became the freshest awin file.
        rows = (candidates_from_awin(doc) if doc.get("relationships")
                else candidates_from_generic(doc, net))
        for source_index, row in enumerate(rows):
            row = bind_direct_application_evidence(row, config)
            rows[source_index] = row
            row["_source_path"] = str(path)
            row["_source_index"] = source_index
        counts[net] = len(rows)
        pool.extend(rows)
    return pool, counts


def direct_candidates_from_sheet(exclude_keys: set[str], policy_snapshot=None,
                                 config: dict | None = None) -> tuple[list[dict], list[dict]]:
    """candidate_apply_ok rows from the freshest backrun dispositions, with
    their Apply URLs, as direct-application candidates.

    The GLP application-scope gate applies here too: the depletion fallback is
    a different SOURCE, not a different policy. Returns (accepted, rejected) so
    the rejected rows keep their per-decision receipt.
    """
    out: list[dict] = []
    rejected: list[dict] = []
    disp_files = sorted(MACHINE_DIR.glob("backrun-*/sheet-row-dispositions.json"))
    exports = sorted(MACHINE_DIR.glob("backrun-*/sheet-full-row-export.json"))
    if not disp_files or not exports:
        return out, rejected
    disps = json.loads(disp_files[-1].read_text())
    export = json.loads(exports[-1].read_text())
    headers = export.get("headers") or []
    idx = {h: i for i, h in enumerate(headers)}
    for row_n, d in disps.items():
        if d.get("disposition") != "candidate_apply_ok":
            continue
        row = (export.get("rows") or {}).get(row_n) or []
        def cell(h):
            i = idx.get(h, -1)
            return str(row[i]).strip() if 0 <= i < len(row) else ""
        key = f"direct:{cell('Program / Network')}"
        if key in exclude_keys or not cell("Apply URL"):
            continue
        name, category = cell("Program / Network"), cell("Category")
        vertical = cell("Vertical").strip().lower()
        # The Sheet's `Vertical` IS the sister-vertical assignment, and it is the
        # only per-row classification on this route that is trustworthy: the
        # `Category` column carries a bulk-fill artifact ("GLP-1 / metabolic
        # health" is stamped on backup software, pet insurance and Zumba), so it
        # can create GLP evidence that no provider ever claimed. A row assigned
        # to another vertical belongs to a job that does not exist yet.
        if vertical and vertical not in GLP_SHEET_VERTICALS:
            rejected.append({
                "network": "direct", "program_id": name, "name": name,
                "description": category, "sheet_row": row_n,
                "glp_fit": {"fit": False, "tier": "none",
                            "reasons": [f"sister_vertical:{vertical}",
                                        "sheet_row_assigned_to_another_vertical"],
                            "evidence_source": "sheet_row"},
            })
            continue
        # NAME only: on this route the free text is Sheet-authored, and the
        # `Category` bulk-fill would manufacture GLP evidence out of nothing.
        decision, verdict = nfilter.evaluate_application(nfilter.Candidate(
            name=name, network="direct", program_id=name,
            region="US", url=cell("Apply URL")), policy_snapshot=policy_snapshot)
        fit, tier, reasons = verdict.fit, verdict.tier, list(verdict.reasons)
        # A row the Sheet already assigned to the GLP vertical counts as fit even
        # when its free-text Category says nothing ("direct provider"): the
        # column that is trusted to exclude sister verticals is the same column,
        # and refusing to read it in the other direction would strand real GLP
        # clinics. The brand-identity gate still overrides it.
        # A no-rescue terminal (an operator hold, a permanently excluded
        # provider) is never overturned by the Sheet's own Vertical column.
        if (decision.decision not in nfilter.NO_RESCUE_TERMINALS and not fit
                and vertical in GLP_SHEET_VERTICALS and not verdict.off_verticals_in_name):
            fit, tier = True, "sheet_vertical_glp1"
            reasons = ["glp_fit:sheet_vertical_glp1", f"sheet_vertical:{vertical}", *reasons]
        row = {
            "network": "direct", "program_id": name,
            "name": name, "description": category,
            "region": "US", "currency": "", "url": cell("Apply URL"),
            "application_url": cell("Apply URL"),
            "apply_route": "native_form", "lane": cell("Vertical") or "unclassified",
            "filter_reasons": ["sheet_candidate_apply_ok", f"row:{row_n}"] + reasons[:3],
            "glp_fit": {"fit": fit, "tier": tier, "reasons": reasons,
                        "evidence_source": "sheet_row"},
            "sheet_row": row_n,
        }
        row = bind_direct_application_evidence(row, config or {})
        # Planning already emits one immutable disposition per candidate. The
        # global parking ledger is an execution ledger, not a per-run planning
        # sink; persisting here made each read-only plan scan+fsync it per row.
        gate = gate_application_candidate(row, operation="affiliate_auto_apply.direct_sheet", persist=False)
        (out if fit and gate["operational"] else rejected).append(
            row if gate["operational"] else {**row, "gate": gate}
        )
    return out, rejected


def plan(config: dict, out_dir: Path, *, deadline_monotonic: float | None = None,
         networks: set[str] | None = None) -> dict:
    # One immutable local policy view for direct and marketplace candidates.
    policy_snapshot = rulings.load_snapshot()
    ledger = load_ledger()
    applied = ledger_keys(ledger)
    pool, source_counts = discover(config, networks) if networks else discover(config)
    evidence_cache = site_evidence.load_cache()
    seen: set[str] = set()
    evaluated_candidates: list[dict] = []
    evaluated_by_id: dict[str, dict] = {}
    decisions = {"APPLY_OK": [], "SKIPPED_NICHE": 0, "SKIPPED_GEO": 0,
                 "SKIPPED_NOT_GLP_FIT": 0, "NEEDS_SITE_EVIDENCE": 0,
                 "SKIPPED_NETWORK_SCOPE": 0, "already_applied": 0, "dup": 0}
    # candidate -> fit verdict + reason, for every candidate that reached the
    # GLP gate. This is the auditable application-scope receipt.
    fit_decisions: list[dict] = []

    def coverage_identity(candidate: dict) -> dict:
        candidate_id = f"{candidate['network']}:{candidate['program_id']}"
        source_path = str(candidate.get("_source_path") or "")
        source_index = str(candidate.get("_source_index") or "")
        evaluation_id = hashlib.sha256(
            f"{candidate_id}|{source_path}|{source_index}".encode()
        ).hexdigest()
        return {
            "evaluation_id": evaluation_id,
            "candidate_id": candidate_id,
            "network": candidate["network"],
            "program_id": candidate["program_id"],
            "name": candidate["name"],
            "source_path": source_path,
            "source_index": candidate.get("_source_index"),
        }

    def record_coverage(candidate: dict, disposition: str, reason: str, **extra) -> dict:
        row = {**coverage_identity(candidate), "disposition": disposition, "reason": reason}
        row.update(extra)
        evaluated_candidates.append(row)
        evaluated_by_id[row["evaluation_id"]] = row
        return row

    for c in pool:
        require_deadline(deadline_monotonic, "marketplace_candidate_evaluation")
        c = bind_direct_application_evidence(c, config)
        key = f"{c['network']}:{c['program_id']}"
        meta = coverage_identity(c)
        if key in seen:
            decisions["dup"] += 1
            record_coverage(c, "DUPLICATE", "duplicate_candidate_identity")
            continue
        seen.add(key)
        if key in applied:
            decisions["already_applied"] += 1
            record_coverage(c, "ALREADY_APPLIED", "application_ledger_contains_candidate")
            continue
        d, verdict = nfilter.evaluate_application(nfilter.Candidate(
            name=c["name"], network=c["network"], program_id=c["program_id"],
            description=c["description"], region=c["region"],
            categories=tuple(c.get("categories") or ()),
            network_region=str(c.get("network_region") or ""),
            currency=c["currency"], url=c["url"],
            site=site_evidence.candidate_site(c, evidence_cache)), policy_snapshot=policy_snapshot)
        # Provider-free planning must not append to the global parking ledger.
        # The application plan itself is the complete immutable scope ledger;
        # execution rechecks only the shortlist with persist=True.
        gate = gate_application_candidate(c, operation="affiliate_auto_apply.plan", persist=False)
        direct_gate = direct_application_gate(c, operation="affiliate_auto_apply.plan")
        fit = {"fit": verdict.fit, "tier": verdict.tier, "reasons": verdict.reasons,
               "off_verticals": verdict.off_verticals_in_name or verdict.off_verticals_in_text}
        if verdict.tier != "none" or d.decision == "SKIPPED_NOT_GLP_FIT":
            fit_decisions.append({"candidate_id": meta["candidate_id"], "name": c["name"],
                                  "network": c["network"], "verdict": "FIT" if verdict.fit else "NOT_FIT",
                                  "tier": verdict.tier, "reason": ";".join(verdict.reasons)})
        if d.apply_ok and gate["operational"] and direct_gate["operational"]:
            # NARROWING ONLY, and only over candidates the shared choke admitted.
            scope_verdict = network_scope_verdict(c, network_scope_for(config, c["network"]), fit)
            if not scope_verdict["admit"]:
                decisions["SKIPPED_NETWORK_SCOPE"] += 1
                record_coverage(c, "SKIPPED_NETWORK_SCOPE", scope_verdict["reason"],
                                lane=d.lane, filter_reasons=d.reasons[:4], glp_fit=fit,
                                network_scope=scope_verdict)
                continue
            decisions["APPLY_OK"].append({
                **c, **meta, "lane": d.lane, "filter_reasons": d.reasons[:4],
                "glp_fit": fit, "network_scope": scope_verdict,
                "direct_application_gate": compact_direct_gate_receipt(direct_gate),
            })
            record_coverage(c, "FILTER_APPLY_OK", "filter_passed", lane=d.lane,
                            filter_reasons=d.reasons[:4], glp_fit=fit,
                            network_scope=scope_verdict)
        else:
            # The filter owns its own terminal vocabulary and grows it (bridge
            # rulings, NEEDS_SITE_EVIDENCE). A terminal this dict has never seen
            # must count, not crash the daily run — the seeded keys below keep
            # the receipt shape stable either way.
            terminal = (d.decision if not d.apply_ok else
                        (str(gate["decision"]).upper() if not gate["operational"]
                         else str(direct_gate["decision"])))
            decisions[terminal] = decisions.get(terminal, 0) + 1
            reason = ";".join(d.reasons[:4]) or ";".join(gate["reason_codes"][:4]) or "filter_rejected"
            record_coverage(c, terminal, reason, filter_reasons=d.reasons[:4], glp_fit=fit,
                            gate=compact_gate_receipt(gate),
                            direct_application_gate=compact_direct_gate_receipt(direct_gate))
    cap = int((config.get("auto_apply") or {}).get("daily_cap") or 25)
    # CAP OVERRIDE (operator 2026-09-17T00:30:43Z, receipt
    # ops/linear/approvals/OPERATOR-DECISION-20260917-apply-scope-glp1-telehealth-only-clickdealer.json:
    # "why dont we just apply to all clickdealer offers that fit? the 25 cap is not
    # needed here."). Only honoured when the RUN is restricted to networks that all
    # carry no_daily_cap in config, so a mixed or unrestricted run keeps the cap.
    uncapped = uncapped_networks(config)
    cap_override = bool(networks) and bool(uncapped) and set(networks) <= uncapped
    # Strong/weak split: single weak keyword hits are where junk lives (a
    # fashion brand's fragrance line matching "beauty"). Auto-apply takes only
    # STRONG fits — 2+ lane term hits, or a lane term in the NAME itself, and
    # no region contradiction between the name and the region field. Weak
    # matches go to a review queue for the lane, never auto-applied.
    LANE_PRIORITY = ["glp1-weightloss", "peptides", "telehealth-general", "cpap-sleep",
                     "hair-loss", "supplements-vitamins", "beauty-skincare", "business-saas"]
    NON_US_NAME = ("apac", "emea", " eu)", "(uk", " uk)", "(au", "australia", "canada only")

    def strength(c):
        hits = 0
        for r in c["filter_reasons"]:
            if r.startswith("term_hits:"):
                hits = int(r.split(":")[1])
        name_l = c["name"].lower()
        name_lane_hit = any(t in name_l for t in nfilter.NICHE_LANES.get(c["lane"], ()))
        region_conflict = any(t in name_l for t in NON_US_NAME)
        return hits, name_lane_hit, region_conflict

    # The GLP fit gate has already answered the question the term-hit heuristic
    # was a proxy for ("is this a real fit or a stray keyword?"), and it answers
    # it better: a multi-service telehealth clinic lands on lane `hair-loss`
    # with one term hit and would have been buried by the old rule. What the
    # heuristic still catches uniquely is a name that contradicts the region.
    strong, review = [], []
    for c in decisions["APPLY_OK"]:
        _hits, _name_hit, conflict = strength(c)
        if conflict:
            c["review_reason"] = "name_region_contradiction"
            review.append(c)
        else:
            strong.append(c)
    strong.sort(key=lambda c: (LANE_PRIORITY.index(c["lane"]) if c["lane"] in LANE_PRIORITY else 99,
                               -next((int(r.split(":")[1]) for r in c["filter_reasons"] if r.startswith("term_hits:")), 0)))
    effective_cap = len(strong) if cap_override else cap
    shortlist = strong[:effective_cap]
    shortlist_ids = {c.get("evaluation_id") for c in shortlist}
    for c in strong:
        row = evaluated_by_id[c["evaluation_id"]]
        if c["evaluation_id"] in shortlist_ids:
            row.update(disposition="SHORTLISTED", reason="strong_fit_within_daily_cap")
        else:
            row.update(disposition="DEFERRED_DAILY_CAP", reason="strong_fit_daily_cap_reached")
    for c in review:
        evaluated_by_id[c["evaluation_id"]].update(
            disposition="REVIEW_QUEUE", reason=c["review_reason"]
        )
    # DEPLETION FALLBACK (operator 2026-08-06): when the marketplaces run dry
    # of strong GLP-fit programs, top up from the Sheet's direct-application
    # candidates (candidate_apply_ok rows with an Apply URL) so the daily cap
    # is spent on real opportunities either way. Same GLP gate applies.
    direct_used, direct_rejected = 0, 0
    if len(shortlist) < effective_cap and not networks:
        require_deadline(deadline_monotonic, "direct_candidate_discovery")
        direct, rejected_direct = direct_candidates_from_sheet(
            applied | {f"{c['network']}:{c['program_id']}" for c in shortlist})
        policy_rejected = []
        allowed_direct = []
        for candidate in direct:
            candidate = bind_direct_application_evidence(candidate, config)
            direct_gate = direct_application_gate(
                candidate, operation="affiliate_auto_apply.plan")
            candidate["direct_application_gate"] = compact_direct_gate_receipt(direct_gate)
            if not direct_gate["operational"]:
                policy_rejected.append(candidate)
                continue
            policy = policy_snapshot.evaluate_program(candidate.get("name"))
            if policy is not None and policy.verdict == "HOLD":
                candidate = {**candidate, "filter_reasons": [f"operator_ruling:{policy.instruction_key}"],
                             "glp_fit": {"fit": False, "tier": "operator_ruling",
                                         "reasons": [policy.reason]}}
                policy_rejected.append(candidate)
            else:
                allowed_direct.append(candidate)
        direct = allowed_direct
        rejected_direct = rejected_direct + policy_rejected
        take = direct[: effective_cap - len(shortlist)]
        shortlist.extend(take)
        direct_used, direct_rejected = len(take), len(rejected_direct)
        for row in take + rejected_direct:
            fit_decisions.append({"candidate_id": f"direct:{row['program_id']}",
                                  "name": row["name"], "network": "direct",
                                  "verdict": "FIT" if row["glp_fit"]["fit"] else "NOT_FIT",
                                  "tier": row["glp_fit"]["tier"],
                                  "reason": ";".join(row["glp_fit"]["reasons"])})

    # QUOTA SHORTFALL (operator 2026-08-06): a run that cannot fill its cap with
    # GLP-fit candidates has exhausted every inventory it can see, and that is an
    # operator-actionable fact — more marketplaces or more discovery routes.
    shortlisted_nets = {c["network"] for c in shortlist}
    exhausted = sorted(n for n in source_counts if n not in shortlisted_nets)
    if "direct" not in shortlisted_nets:
        exhausted.append("sheet-direct")
    quota = {
        "daily_cap": cap,
        "effective_cap": effective_cap,
        "cap_override": cap_override,
        "cap_override_authority": (
            "ops/linear/approvals/OPERATOR-DECISION-20260917-apply-scope-glp1-telehealth-only-clickdealer.json"
            if cap_override else None),
        "uncapped_networks": sorted(uncapped),
        "filled": len(shortlist),
        "shortfall": max(0, effective_cap - len(shortlist)),
        "quota_filled": len(shortlist) >= effective_cap,
        "glp_fit_marketplace_candidates": len(strong),
        "direct_topups_used": direct_used,
        "direct_topups_rejected_not_glp_fit": direct_rejected,
        "sources_evaluated": sorted(source_counts) + ["sheet-direct"],
        "exhausted_sources": exhausted,
        "operator_ask": ("need more marketplaces or discovery routes"
                         if len(shortlist) < effective_cap else None),
    }
    require_deadline(deadline_monotonic, "plan_receipt_write")
    payload = {
        "schema": "affiliate-application-plan-v1",
        "captured_at_utc": utc(),
        "source_counts": source_counts,
        "pool_size": len(pool),
        "filter_summary": {k: (len(v) if isinstance(v, list) else v) for k, v in decisions.items()},
        "daily_cap": cap,
        "quota": quota,
        "application_scope": {
            "schema": "affiliate-application-scope-v1",
            "site": "glpclinicscout.com",
            "policy": "GLP-1 / weight-loss telehealth and directly adjacent ONLY; "
                      "sister verticals get their own jobs later",
            "authority": "operator 2026-08-06",
            "terminal_added": "SKIPPED_NOT_GLP_FIT",
            "fit_decision_count": len(fit_decisions),
            "fit_decisions": fit_decisions,
        },
        "operator_rulings": rulings.policy_receipt(policy_snapshot),
        "strong_candidates": len(strong),
        "review_queue_count": len(review),
        "review_queue": review[:100],
        "shortlist": shortlist,
        "evaluated_candidate_count": len(evaluated_candidates),
        "evaluated_candidates": evaluated_candidates,
        "coverage_contract": {
            "schema": "affiliate-application-plan-coverage-v1",
            "one_disposition_per_candidate": True,
            "one_reason_per_candidate": True,
            "stable_identity": "sha256(network:program_id|source_path|source_index)",
            "provider_free": True,
        },
        "application_profile": config.get("application_profile"),
        "effects": {"provider_mutations": 0, "applications_submitted": 0},
        "secret_printed": False,
    }
    sha = private_json(out_dir / "application-plan.json", payload)
    # Standalone application-scope receipt: the plan carries everything, but the
    # `candidate -> fit verdict + reason` ledger is what an auditor reads.
    private_json(out_dir / "application-scope-receipt.json", {
        "schema": "affiliate-application-scope-receipt-v1",
        "captured_at_utc": payload["captured_at_utc"],
        **payload["application_scope"],
        "quota": quota,
    })
    payload["_sha256"] = sha
    return payload


def queue_api_key_followups(entries: list[dict], run_id: str, portal_lane=None) -> dict:
    """Append a key-minting task per NEW marketplace we just transacted with.

    Only networks that produced a real application/signup are queued, and
    `portal_lane.queue_api_key_minting` drops any network that already holds a
    working API credential — so a network is queued once, minted once, and then
    permanently served over its API.
    """
    lane = portal_lane if portal_lane is not None else _portal_lane
    if lane is None:
        return {"queued": False, "reason": "portal_lane_unavailable"}
    seen, followups = set(), []
    for entry in entries:
        network = str(entry.get("network") or "").lower()
        if not network or network in seen:
            continue
        seen.add(network)
        followups.append({"network": network, "program_id": entry.get("program_id"),
                          "trigger": f"application_{entry.get('status', 'unknown')}"})
    if not followups:
        return {"queued": False, "reason": "no_successful_applications"}
    result = lane.queue_api_key_minting(followups, run_id=run_id)
    result["queued"] = True
    return result


def execute(config: dict, out_dir: Path, plan_payload: dict,
            *, deadline_monotonic: float | None = None,
            effects: dict[str, int] | None = None) -> dict:
    effects = effects if effects is not None else new_execution_effects()
    # Serialize the complete ledger mutation / provider-execution / final-commit
    # cycle. In particular, no ledger read may happen before lock acquisition:
    # an Item E promotion can otherwise land between the stale read and commit.
    with _canonical_ledger_writer_guard(LEDGER_PATH):
        ledger = load_ledger()
        known_ledger_keys = ledger_keys(ledger)
        policy_snapshot = rulings.load_snapshot()
        portal_tasks, api_applied, failures, policy_holds = [], [], [], []
        for c in plan_payload["shortlist"]:
            require_execution_deadline(deadline_monotonic, "application_execution")
            key = f"{c['network']}:{c['program_id']}"
            # Planning normally removes these rows. Re-check under the writer
            # lock so a concurrent writer or exact replay cannot duplicate an
            # application or re-run a provider mutation.
            if key in known_ledger_keys:
                continue
            direct_gate = direct_application_gate(
                c, operation="affiliate_auto_apply.execute")
            if not direct_gate["operational"]:
                failures.append({
                    "network": c.get("network"), "program_id": c.get("program_id"),
                    "name": c.get("name"), "status": "machine_owned_non_executable",
                    "direct_application_gate": compact_direct_gate_receipt(direct_gate),
                })
                continue
            gate = gate_application_candidate(c, operation="affiliate_auto_apply.execute", persist=True)
            if not gate["operational"]:
                failures.append({"network": c.get("network"), "program_id": c.get("program_id"),
                                 "name": c.get("name"), "status": "parked", "gate": gate})
                continue
            policy = policy_snapshot.evaluate_program(c.get("name"))
            if policy is not None and policy.verdict == "HOLD":
                policy_holds.append({"network": c["network"], "program_id": c["program_id"],
                                     "name": c["name"], "reason": policy.reason,
                                     "instruction_key": policy.instruction_key})
                continue
            # Preserve the complete screened projection. The later portal run
            # must apply the exact candidate the niche/geo choke admitted, not a
            # lossy three-field reconstruction.
            entry = {
                "network": c["network"], "program_id": c["program_id"], "name": c["name"],
                "lane": c["lane"], "planned_at_utc": utc(), "route": None, "status": None,
                "url": c.get("url", ""),
                "application_url": c.get("application_url") or c.get("url", ""),
                "description": c.get("description", ""), "fit_reason": c.get("fit_reason", ""),
                "categories": c.get("categories") or [], "region": c.get("region", ""),
                "currency": c.get("currency", ""), "advertiser_id": c.get("advertiser_id", ""),
                "group_slug": c.get("group_slug", ""),
                "application_meta": c.get("application_meta") or {},
            }
            adapter = JOIN_ADAPTERS.get(c["network"])
            # Ledger-first: record the attempt before making it (no double-apply).
            if adapter:
                entry.update(route="api", status="attempting")
                ledger["applications"].append(entry)
                known_ledger_keys.add(key)
                private_json(LEDGER_PATH, ledger)
                effects["ledger_rows_committed"] += 1
                effects["ledger_writes"] += 1
                try:
                    result = adapter(config, c)
                except Exception as exc:  # noqa: BLE001
                    # A provider exception after request dispatch is not proof of
                    # zero mutation. Preserve the ambiguity for reconciliation.
                    effects["provider_mutation_attempts"] += 1
                    effects["provider_mutations_ambiguous"] += 1
                    entry.update(status="failed", error=type(exc).__name__)
                    failures.append(entry)
                    private_json(LEDGER_PATH, ledger)
                    effects["ledger_writes"] += 1
                else:
                    provider_status = str(result.get("status") or "")
                    write_actions = int(result.get("write_actions") or 0)
                    effects["provider_mutation_attempts"] += write_actions
                    if provider_status == "SUBMITTED_VERIFIED" and write_actions == 1:
                        effects["provider_mutations"] += 1
                        effects["applications_submitted"] += 1
                        entry.update(status="SUBMITTED_VERIFIED", provider_receipt=result)
                        api_applied.append(entry)
                    elif provider_status == "ALREADY_RELATIONSHIP" and write_actions == 0:
                        entry.update(status="ALREADY_RELATIONSHIP", provider_receipt=result)
                        api_applied.append(entry)
                    else:
                        if write_actions:
                            effects["provider_mutations_ambiguous"] += write_actions
                        entry.update(status=provider_status or "SUBMISSION_UNVERIFIED",
                                     provider_receipt=result)
                        failures.append(entry)
                    # Commit the terminal row immediately. A later deadline must
                    # never leave a confirmed provider action recorded only in RAM.
                    private_json(LEDGER_PATH, ledger)
                    effects["ledger_writes"] += 1
            else:
                entry.update(route="portal", status="queued_for_portal_execution")
                ledger["applications"].append(entry)
                known_ledger_keys.add(key)
                portal_tasks.append(entry)
                private_json(LEDGER_PATH, ledger)
                effects["ledger_rows_committed"] += 1
                effects["ledger_writes"] += 1
                effects["portal_tasks_queued"] += 1
        minting = queue_api_key_followups(api_applied + portal_tasks, run_id=out_dir.name)
        effects["api_key_followups_queued"] = int(minting.get("appended_count") or 0)
        receipt = {
            "schema": "affiliate-application-execution-v1",
            "captured_at_utc": utc(),
            "api_applied": len(api_applied), "portal_queued": len(portal_tasks),
            "failures": len(failures), "policy_holds": policy_holds,
            "api_application_results": api_applied,
            "failure_rows": failures,
            "operator_rulings": rulings.policy_receipt(policy_snapshot),
            "api_key_minting_followups": minting,
            "portal_task_list": portal_tasks,
            "effects": execution_effect_snapshot(effects),
            "note": "portal tasks are executed same-day by the activation lane with the standard application profile; ledger prevents re-application",
            "secret_printed": False,
        }
        private_json(out_dir / "application-execution.json", receipt)
        return receipt


def attach_fit_assessment(summary: dict, config: dict, out_dir: Path,
                          networks: set[str] | None) -> dict:
    """Emit the bound fit list for this run and record it on the run summary.

    Called on EVERY completed run - plan-only included - because the Sheet's
    single writer needs the proof for applications that are already submitted,
    not only for ones submitted a moment ago. A run that emits no list says so
    with a reason instead of leaving the field absent.
    """
    fit = emit_fit_assessment(config, out_dir, networks)
    compact = {key: value for key, value in fit.items() if key != "refused_rows"}
    if fit.get("refused_rows"):
        compact["refused_rows"] = fit["refused_rows"][:10]
    summary["fit_assessment"] = compact
    return fit


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--retry-unreached", action="store_true",
                    help="re-attempt ONLY ledger rows whose API attempt never reached a "
                         "provider verdict (adapter raised); no discovery, no cap spend")
    ap.add_argument("--output-dir")
    ap.add_argument("--network", action="append", choices=_adapters.NETWORKS,
                    help="limit this registered run to named marketplace(s); no direct topups")
    ap.add_argument("--deadline-seconds", type=float, default=480.0,
                    help="inner monotonic budget; must stay below the scheduler timeout")
    ns = ap.parse_args()
    config = json.loads(CONFIG_PATH.read_text())
    if not (config.get("auto_apply") or {}).get("enabled"):
        print(json.dumps({"status": "disabled_in_config"}))
        return 1
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(ns.output_dir) if ns.output_dir else MACHINE_DIR / f"apply-{stamp}"
    deadline = time.monotonic() + ns.deadline_seconds if ns.deadline_seconds > 0 else None
    if ns.retry_unreached:
        cap = int((config.get("auto_apply") or {}).get("daily_cap") or 25)
        retry = unreached_attempt_candidates(
            load_ledger(), set(ns.network) if ns.network else None, limit=cap)
        p = {"schema": "affiliate-application-retry-plan-v1", "captured_at_utc": utc(),
             "source": str(LEDGER_PATH), "networks": sorted(ns.network or []),
             "pool_size": len(retry), "shortlist": retry,
             "filter_summary": {"UNREACHED_ATTEMPT": len(retry)},
             "quota": {"daily_cap": cap, "cap_spent_by_this_run": 0,
                       "max_unreached_attempts_per_key": MAX_UNREACHED_ATTEMPTS,
                       "note": "re-attempt of rows already counted by the run that planned "
                               "them; bounded by the daily cap per invocation"},
             "secret_printed": False}
        private_json(out_dir / "application-retry-plan.json", p)
        summary = {"status": "RETRY_PLANNED", "pool": p["pool_size"],
                   "filter_summary": p["filter_summary"], "shortlist": len(retry),
                   "quota": p["quota"], "plan": str(out_dir / "application-retry-plan.json")}
        if not ns.execute:
            attach_fit_assessment(summary, config, out_dir, set(ns.network) if ns.network else None)
            print(json.dumps(summary, indent=2))
            return 0
        if not retry:
            summary.update(status="RETRY_NO_TARGETS")
            attach_fit_assessment(summary, config, out_dir, set(ns.network) if ns.network else None)
            print(json.dumps(summary, indent=2))
            return 0
        e = execute(config, out_dir, p, deadline_monotonic=deadline,
                    effects=new_execution_effects())
        summary.update(status="RETRY_EXECUTED", api_applied=e["api_applied"],
                       portal_queued=e["portal_queued"], failures=e["failures"],
                       execution=str(out_dir / "application-execution.json"))
        attach_fit_assessment(summary, config, out_dir, set(ns.network) if ns.network else None)
        print(json.dumps(summary, indent=2))
        return 0
    try:
        p = plan(config, out_dir, deadline_monotonic=deadline,
                 networks=set(ns.network) if ns.network else None)
    except PlanDeadlineExceeded as exc:
        receipt = {"schema": "affiliate-auto-apply-deadline-v1", "status": "BLOCKED_DEADLINE",
                   "phase": "planning", "reason": str(exc), "captured_at_utc": utc(),
                   "effects": execution_effect_snapshot(new_execution_effects())}
        private_json(out_dir / "application-deadline.json", receipt)
        print(json.dumps(receipt, indent=2))
        return 124
    summary = {"status": "PLANNED", "pool": p["pool_size"],
               "filter_summary": p["filter_summary"], "shortlist": len(p["shortlist"]),
               "quota": p["quota"], "plan": str(out_dir / "application-plan.json")}
    if ns.execute:
        execution_effects = new_execution_effects()
        try:
            require_execution_deadline(deadline, "before_application_execution")
            e = execute(config, out_dir, p, deadline_monotonic=deadline,
                        effects=execution_effects)
        except ExecutionDeadlineExceeded as exc:
            receipt = {"schema": "affiliate-auto-apply-deadline-v1", "status": "BLOCKED_DEADLINE",
                       "phase": "execution", "reason": str(exc), "captured_at_utc": utc(),
                       "effects": execution_effect_snapshot(execution_effects),
                       "plan": str(out_dir / "application-plan.json")}
            private_json(out_dir / "application-deadline.json", receipt)
            print(json.dumps(receipt, indent=2))
            return 124
        except Exception as exc:  # noqa: BLE001 - terminal accounting must survive errors
            receipt = {"schema": "affiliate-auto-apply-execution-error-v1",
                       "status": "BLOCKED_EXECUTION_ERROR", "phase": "execution",
                       "reason": f"execution_error:{type(exc).__name__}",
                       "captured_at_utc": utc(),
                       "effects": execution_effect_snapshot(execution_effects),
                       "plan": str(out_dir / "application-plan.json")}
            private_json(out_dir / "application-execution-error.json", receipt)
            print(json.dumps(receipt, indent=2))
            return 1
        summary.update(status="EXECUTED", api_applied=e["api_applied"],
                       portal_queued=e["portal_queued"], failures=e["failures"],
                       execution=str(out_dir / "application-execution.json"))
    attach_fit_assessment(summary, config, out_dir, set(ns.network) if ns.network else None)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
