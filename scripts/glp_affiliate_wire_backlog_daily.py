#!/usr/bin/env python3
"""Run one friction-ordered B2 wire-backlog slice inside the 07:15 lane."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")
SCRIPTS = BUSINESS / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(BUSINESS / "ops"))
import glp_affiliate_wire_backlog_worker as worker  # noqa: E402
from marketplace_adapters import maxbounty  # noqa: E402

#: Kept as an alias: the directory is now named once, in the worker, so the
#: onboarding manifest and the authoring brief cite the same constant this lane
#: reads (2026-08-20 — three copies of one path is how a provider ends up
#: blocked on a file nobody could name).
AUTHORITY_DIR = worker.PROGRAM_AUTHORITY_DIR
BUILD_READY_HANDOFF_SCHEMA = "glp.onboarding_build_ready_handoff.v1"
MAXBOUNTY_ROLLOUT_STATE = (
    BUSINESS / "reports/glp-clinic-scout/affiliate-machine/maxbounty/rollout-state.json")


def load_maxbounty_rollout_state() -> dict[str, object]:
    if not MAXBOUNTY_ROLLOUT_STATE.is_file():
        return {"schema": "glp.maxbounty.rollout-state.v1"}
    try:
        payload = json.loads(MAXBOUNTY_ROLLOUT_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"schema": "glp.maxbounty.rollout-state.v1", "invalid": True}
    return payload if isinstance(payload, dict) else {
        "schema": "glp.maxbounty.rollout-state.v1", "invalid": True}


def apply_maxbounty_rollout(plan: dict[str, object]) -> dict[str, object]:
    """Encode site-live -> one test -> operator verdict -> backlog release."""
    policy = maxbounty.load_policy()
    runtime = load_maxbounty_rollout_state()
    rollout = maxbounty.rollout_status(policy, runtime)
    queue = list(plan.get("queue") or [])
    max_rows = [row for row in queue
                if worker.normalize_network(row.get("network")) == "maxbounty"]
    allowed = set(rollout.get("allowed_providers") or [])
    if "*" in allowed:
        kept = queue
        blocked: list[dict[str, object]] = []
    else:
        def permitted(row: dict[str, object]) -> bool:
            if worker.normalize_network(row.get("network")) != "maxbounty":
                return True
            identities = {str(row.get("site_program_id") or ""),
                          str(row.get("provider_program_id") or "")}
            return bool(identities & allowed)
        kept = [row for row in queue if permitted(row)]
        blocked = [
            {"key": row.get("key"), "site_program_id": row.get("site_program_id"),
             "provider_program_id": row.get("provider_program_id"),
             "reason": rollout["phase"]}
            for row in max_rows if not permitted(row)
        ]
    # The test phase must be exactly one provider even if stale routing produced
    # two MaxBounty identities for the same configured id.
    if rollout["phase"] == "ALLOW_ONE_TEST_PROVIDER":
        allowed_rows = [row for row in kept
                        if worker.normalize_network(row.get("network")) == "maxbounty"]
        if len(allowed_rows) > 1:
            raise worker.B2Error("maxbounty_rollout_test_provider_not_unique")
    plan["queue"] = kept
    plan["maxbounty_rollout"] = {
        "phase": rollout["phase"],
        "gate": rollout.get("gate"),
        "maxbounty_rows_seen": len(max_rows),
        "maxbounty_rows_allowed": sum(
            worker.normalize_network(row.get("network")) == "maxbounty" for row in kept),
        "blocked": blocked,
    }
    return rollout


def record_maxbounty_test_deploy(provider_id: str, key: str, release: dict[str, object]) -> None:
    receipt = MAXBOUNTY_ROLLOUT_STATE.with_name("test-provider-deploy-receipt.json")
    worker.atomic_json(receipt, {
        "schema": "glp.maxbounty.test-provider-deploy.v1",
        "status": "PASS",
        "provider_id": provider_id,
        "ledger_key": key,
        "recorded_at_utc": worker.iso_utc(),
        "release": release,
    })
    worker.atomic_json(MAXBOUNTY_ROLLOUT_STATE, {
        "schema": "glp.maxbounty.rollout-state.v1",
        "test_deploy": {
            "provider_id": provider_id,
            "receipt_path": str(receipt),
            "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        },
    })


def safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "row"


def binding_path(binding: object, label: str) -> Path:
    if not isinstance(binding, dict):
        raise worker.B2Error(f"{label}_binding_missing")
    return worker.verify_binding(binding, label=label)


def receipt_providers(receipts: list[Path] | None, *, label: str) -> dict[str, Path]:
    """Validate one unique authored+fit receipt per exact provider identity."""
    out: dict[str, Path] = {}
    for receipt_path in receipts or []:
        resolved = Path(receipt_path).resolve()
        receipt = worker.read_json(resolved)
        if receipt.get("schema") != "glp.onboarding_authoring_receipt.v1":
            raise worker.B2Error(f"{label}_schema_invalid")
        provider = str(receipt.get("provider") or "")
        if not provider:
            raise worker.B2Error(f"{label}_provider_missing")
        if str(receipt.get("verdict") or "").casefold() != "authored":
            raise worker.B2Error(f"{label}_not_authored:{provider}")
        if str(receipt.get("fit_verdict") or "").casefold() != "fit":
            raise worker.B2Error(f"{label}_not_fit:{provider}")
        if provider in out:
            raise worker.B2Error(f"duplicate_{label}_for_provider:{provider}")
        out[provider] = resolved
    return out


def pricing_by_provider(
    authoring_receipts: list[Path] | None,
    pricing_source_receipts: list[Path] | None = None,
) -> dict[str, Path]:
    """Verify the staged-pricing subset of separately bound authoring receipts.

    Existing providers may preserve a pricing-matrix row that the onboarding
    build-ready gate independently re-validated. Those providers intentionally
    have no pricing-source receipt in this call. A supplied pricing source must
    still belong to an exact latest authoring provider; foreign sources fail
    closed.
    """
    latest = receipt_providers(authoring_receipts, label="authoring_receipt")
    sources = receipt_providers(
        pricing_source_receipts if pricing_source_receipts is not None
        else authoring_receipts,
        label="pricing_source_receipt",
    )
    if sources and not latest:
        raise worker.B2Error("pricing_sources_without_authoring_receipts")
    if not set(sources).issubset(set(latest)):
        raise worker.B2Error("pricing_source_provider_set_mismatch")
    return {
        provider: worker.pricing_spec_from_authoring_receipt(source, provider)
        for provider, source in sources.items()
    }


def build_ready_inputs_by_provider(
    handoff_receipts: list[Path] | None,
    authoring_receipts: list[Path] | None,
) -> dict[str, dict[str, Path]]:
    """Verify exact authored BUILD-READY bindings without changing the base plan.

    The batch authority remains bound to the original plan and queue denominator.
    This overlay only replaces stale/null evidence hints for a provider whose
    authored+fit receipt and canonical B2 inputs were independently bound by the
    onboarding build-ready gate.
    """
    if not handoff_receipts:
        return {}
    authored = receipt_providers(authoring_receipts, label="authoring_receipt")
    out: dict[str, dict[str, Path]] = {}
    for handoff_path in handoff_receipts:
        handoff_file = Path(handoff_path).resolve()
        handoff = worker.read_json(handoff_file)
        if handoff.get("schema") != BUILD_READY_HANDOFF_SCHEMA:
            raise worker.B2Error("build_ready_handoff_schema_invalid")
        if str(handoff.get("status") or "") != "PASS":
            raise worker.B2Error("build_ready_handoff_status_invalid")
        provider = str(handoff.get("provider") or "")
        if not provider:
            raise worker.B2Error("build_ready_handoff_provider_missing")
        if provider in out:
            raise worker.B2Error(f"duplicate_build_ready_handoff:{provider}")
        if provider not in authored:
            raise worker.B2Error(f"build_ready_handoff_authoring_receipt_missing:{provider}")
        artifacts = handoff.get("artifacts")
        if not isinstance(artifacts, dict):
            raise worker.B2Error(f"build_ready_handoff_artifacts_missing:{provider}")
        verified = {
            name: worker.verify_binding(artifacts.get(name) or {}, label=f"build_ready_{name}")
            for name in ("authoring_receipt", "program_authority", "research", "terms_classification")
        }
        if verified["authoring_receipt"] != authored[provider]:
            raise worker.B2Error(f"build_ready_authoring_receipt_drift:{provider}")
        canonical_authority = (AUTHORITY_DIR / f"{safe_name(provider)}.json").resolve()
        if verified["program_authority"] != canonical_authority:
            raise worker.B2Error(f"build_ready_program_authority_path_drift:{provider}")
        canonical_research = worker.program_research_path(provider).resolve()
        if verified["research"] != canonical_research:
            raise worker.B2Error(f"build_ready_research_path_drift:{provider}")
        worker._research_valid(worker.read_json(verified["research"]), provider)
        authority = worker.read_json(verified["program_authority"])
        worker._extract_program_spec(authority, provider)
        out[provider] = verified
    return out


def row_evidence_paths(
    row: dict[str, object],
    build_ready_inputs: dict[str, dict[str, Path]],
) -> tuple[Path, Path, Path]:
    """Resolve one row's evidence, preferring only its exact validated handoff."""
    provider = str(row.get("site_program_id") or "")
    staged = build_ready_inputs.get(provider)
    if not staged:
        hints = row.get("evidence_hints") or {}
        if not isinstance(hints, dict):
            raise worker.B2Error("queue_evidence_hints_invalid")
        if hints.get("terms_classification") is None:
            terms_path = worker.find_terms_classification(
                row.get("network"), row.get("provider_program_id")
            )
            if terms_path is not None:
                hints = {**hints, "terms_classification": worker.path_binding(terms_path)}
                row["evidence_hints"] = hints
        research = binding_path(hints.get("research"), "queue_research")
        terms = binding_path(hints.get("terms_classification"), "queue_terms")
        authority = (AUTHORITY_DIR / f"{safe_name(provider)}.json").resolve()
        if not authority.is_file():
            raise worker.B2Error("stage5_program_authority_missing")
        return research, terms, authority

    research = staged["research"]
    terms = staged["terms_classification"]
    authority = staged["program_authority"]
    candidate = dict(row)
    worker._research_valid(worker.read_json(research), provider)
    worker._terms_valid(worker.read_json(terms), candidate)
    spec = worker._extract_program_spec(worker.read_json(authority), provider)
    worker._validate_spec(spec, candidate, research, terms)
    return research, terms, authority


def authority_bound_plan(authority_path: Path) -> dict[str, object]:
    """Load the immutable plan the operator approved for change-then-fire.

    Authoring intentionally adds new evidence after the plan is captured. A
    fresh planner can therefore reorder the same queue by friction and no longer
    byte-match the authority. The authority already binds the exact denominator,
    order, source release and semantic scope, so registered execution must use
    that plan rather than silently substitute a newly observed one.
    """
    resolved = Path(authority_path).resolve()
    binding = worker.path_binding(resolved)
    authority = worker.read_json(resolved)
    baseline = worker.verify_binding(
        authority.get("base_plan") or {}, label="wire_batch_base_plan"
    )
    plan = worker.read_json(baseline)
    worker.validate_batch_authority(plan, binding)
    return plan


def authority_bound_maxbounty_rollout(plan: dict[str, object]) -> dict[str, object]:
    """Describe MaxBounty disposition without mutating an approved plan.

    A change-then-fire authority binds the base plan before this daily wrapper
    runs. Adding a newly observed rollout block would invalidate that exact
    semantic scope. The operator-approved batch is therefore the rollout gate
    for that plan; ordinary, non-batch plans still use the live rollout policy.
    """
    recorded = plan.get("maxbounty_rollout")
    if isinstance(recorded, dict):
        return recorded
    return {
        "phase": "OPERATOR_APPROVED_EXACT_BATCH",
        "gate": "wire_batch_authority",
        "maxbounty_rows_seen": sum(
            worker.normalize_network(row.get("network")) == "maxbounty"
            for row in plan.get("queue") or []
        ),
        "maxbounty_rows_allowed": sum(
            worker.normalize_network(row.get("network")) == "maxbounty"
            for row in plan.get("queue") or []
        ),
        "blocked": [],
    }


def run_day(*, output_dir: Path, ledger: Path, backrun_dir: Path | None,
            date_pt: str, deploy: bool, providers: list[str] | None = None,
            authoring_receipts: list[Path] | None = None,
            pricing_source_receipts: list[Path] | None = None,
            build_ready_handoffs: list[Path] | None = None,
            plan_only: bool = False,
            batch_authority: Path | None = None) -> dict[str, object]:
    if plan_only and deploy:
        raise worker.B2Error("plan_only_cannot_deploy")
    output_dir.mkdir(parents=True, exist_ok=True)
    selected_backrun = (backrun_dir or worker.latest_complete_backrun()).resolve()
    plan_path = output_dir / "plan.json"
    if batch_authority is not None:
        plan = authority_bound_plan(batch_authority)
        maxbounty_rollout = authority_bound_maxbounty_rollout(plan)
    else:
        plan = worker.build_plan(
            backrun_dir=selected_backrun,
            ledger_path=ledger.resolve(),
            date_pt=date_pt,
        )
        maxbounty_rollout = apply_maxbounty_rollout(plan)
    worker.atomic_json(plan_path, plan)
    if batch_authority is not None:
        plan = worker.apply_batch_authority(
            plan_path, batch_authority.resolve()
        )
    requested = {str(provider) for provider in providers or [] if str(provider)}
    attempt_rows = list(plan.get("queue") or [])
    if requested:
        # A wired row stays terminal unless an explicit provider request is
        # paired with a current-head, source-manifest-bound corrective receipt.
        # This narrow transform happens before filtering so normal wired rows
        # remain absent and unrelated rows can never hitchhike into the build.
        plan = worker.apply_explicit_wired_repairs(plan, requested)
        attempt_rows = [
            row for row in attempt_rows
            if str(row.get("site_program_id") or "") in requested
        ]
        if len(attempt_rows) != len(requested):
            found = {str(row.get("site_program_id") or "") for row in attempt_rows}
            raise worker.B2Error(
                "explicit_provider_scope_missing:" + ",".join(sorted(requested - found))
            )
        # Ordinary single-slice runs retain the old narrow plan. A registered
        # change-then-fire batch keeps its full authority-bound queue so each
        # no-deploy authoring slice accumulates staged rows and the final slice
        # can make one exact all-rows COW release.
        if not plan.get("batch_authority"):
            plan["queue"] = list(attempt_rows)
        plan["build_trigger_scope"] = {
            "mode": "explicit_providers",
            "providers": sorted(requested),
        }
    worker.atomic_json(plan_path, plan)
    if plan_only:
        guard_drill = worker.routing_basis_age_guard_drill(plan)
        guard_path = output_dir / "routing-age-guard-drill.json"
        worker.atomic_json(guard_path, guard_drill)
        current = worker.status_receipt(plan_path=plan_path, ledger_path=ledger.resolve())
        result = {
            "schema": "b2-wire-backlog-daily-run-v1",
            "status": "PASS_PLAN_ONLY_NO_EXTERNAL_WRITE",
            "pt_date": date_pt,
            "prepared": 0,
            "isolated_blocks": 0,
            "build_trigger_providers": sorted(requested),
            "staged_pricing_providers": [],
            "maxbounty_rollout": maxbounty_rollout,
            "release": None,
            "plan": worker.path_binding(plan_path),
            "routing_age_guard_drill": worker.path_binding(guard_path),
            "daily_card_line": current["daily_card_line"],
            "effects": {
                "provider_calls": 0, "sheet_writes": 0, "site_writes": 0,
                "deployments": 0, "ledger_writes": 0,
            },
        }
        worker.atomic_json(output_dir / "plan-only-run.json", result)
        return result
    staged_pricing = pricing_by_provider(
        authoring_receipts, pricing_source_receipts
    )
    staged_build_ready = build_ready_inputs_by_provider(
        build_ready_handoffs, authoring_receipts
    )
    prepared = 0
    isolated_blocks = 0
    prepared_rows: dict[str, str] = {}
    for row in attempt_rows:
        if prepared >= worker.plan_wired_limit(plan):
            break
        key = str(row["key"])
        try:
            worker.begin_row(plan_path=plan_path, ledger_path=ledger.resolve(), key=key)
            research, terms, authority = row_evidence_paths(row, staged_build_ready)
            worker.prepare_row(
                plan_path=plan_path,
                ledger_path=ledger.resolve(),
                key=key,
                spec_path=authority.resolve(),
                research_path=research,
                terms_path=terms,
                pricing_path=staged_pricing.get(str(row["site_program_id"])),
            )
            prepared += 1
            # The rollout policy may register either the site's canonical id or
            # MaxBounty's own campaign id. Bind both to the same ledger key so a
            # real test deployment can always be found and receipted.
            prepared_rows[str(row["site_program_id"])] = key
            prepared_rows[str(row.get("provider_program_id") or "")] = key
        except Exception as exc:
            isolated_blocks += 1
            evidence = output_dir / "row-blocks" / f"{safe_name(key)}.json"
            worker.atomic_json(evidence, {
                "schema": "b2-wire-backlog-row-prevalidation-block-v1",
                "status": "BLOCKED_ROW_ONLY",
                "key": key,
                "error": f"{type(exc).__name__}:{exc}",
            })
            try:
                worker.block_row(
                    plan_path=plan_path,
                    ledger_path=ledger.resolve(),
                    key=key,
                    reason="prevalidation_failed",
                    evidence_path=evidence,
                )
            except Exception:
                pass
            continue
    # A registered operator batch separates serial row preparation from the one
    # atomic release. Running a complete verify-only build after every bounded
    # provider slice replays every previously prepared row from the immutable
    # base release; any later row/build refusal then rewrites those accumulated
    # rows back to blocked. Keep the registered webhook and every row gate, but
    # stop at durable ledger preparation. The separately registered deploy
    # webhook below is the only path that may consume the full prepared set.
    batch_preparation_only = bool(
        batch_authority is not None and requested and not deploy
    )
    if batch_preparation_only:
        ledger_after = worker.load_ledger(ledger.resolve())
        target_keys = [str(row["key"]) for row in attempt_rows]
        prepared_target_keys = [
            key for key in target_keys
            if (
                ((ledger_after.get("rows") or {}).get(key) or {}).get("status")
                == "prepared"
                and ((ledger_after.get("rows") or {}).get(key) or {}).get("pt_date")
                == plan["pt_date"]
            )
        ]
        allowed_keys = list(
            ((plan.get("batch_authority") or {}).get("allowed_queue_keys") or [])
        )
        globally_prepared_keys = [
            key for key in allowed_keys
            if (
                ((ledger_after.get("rows") or {}).get(key) or {}).get("status")
                == "prepared"
                and ((ledger_after.get("rows") or {}).get(key) or {}).get("pt_date")
                == plan["pt_date"]
            )
        ]
        preparation_status = (
            "PASS_BATCH_PREPARATION_ONLY_NO_EXTERNAL_WRITE"
            if prepared_target_keys == target_keys
            else "PARTIAL_BATCH_PREPARATION_ONLY_NO_EXTERNAL_WRITE"
        )
        preparation_path = output_dir / "batch-preparation-only.json"
        preparation = {
            "schema": "b2-wire-backlog-batch-preparation-only-v1",
            "status": preparation_status,
            "pt_date": plan["pt_date"],
            "target_keys": target_keys,
            "prepared_target_keys": prepared_target_keys,
            "target_count": len(target_keys),
            "prepared_target_count": len(prepared_target_keys),
            "globally_prepared_keys": globally_prepared_keys,
            "globally_prepared_count": len(globally_prepared_keys),
            "authority": worker.path_binding(batch_authority.resolve()),
            "ledger": worker.path_binding(ledger.resolve()),
            "effects": {
                "deployments": 0,
                "site_writes": 0,
                "sheet_writes": 0,
                "tracking_link_requests": 0,
                "tracking_clicks": 0,
            },
        }
        worker.atomic_json(preparation_path, preparation)
        preparation = {
            **preparation,
            "receipt": str(preparation_path),
        }
        release = None
        status = preparation_status
    elif prepared:
        release = worker.release_prepared(
            plan_path=plan_path,
            ledger_path=ledger.resolve(),
            output_dir=output_dir,
            deploy=deploy,
            # This daily lane only wires provider additions/activations. Its
            # shared build therefore changes provider-derived route content in
            # both deploy and local verify-only runs. The later deployer
            # `--verify-only` call receives no build environment of its own.
            provider_content_change=True,
        )
        status = str(release["status"])
        if deploy and maxbounty_rollout["phase"] == "ALLOW_ONE_TEST_PROVIDER":
            test_provider = str((maxbounty.load_policy().get("rollout") or {}).get(
                "test_provider_id") or "")
            test_key = prepared_rows.get(test_provider)
            ledger_after = worker.load_ledger(ledger.resolve())
            test_state = (ledger_after.get("rows") or {}).get(test_key or "") or {}
            if test_key and test_state.get("status") == "wired":
                record_maxbounty_test_deploy(test_provider, test_key, release)
    else:
        release = None
        status = "PASS_NO_PREPARED_ROWS"
    current = worker.status_receipt(plan_path=plan_path, ledger_path=ledger.resolve())
    result = {
        "schema": "b2-wire-backlog-daily-run-v1",
        "status": status,
        "pt_date": date_pt,
        "prepared": prepared,
        "isolated_blocks": isolated_blocks,
        "build_trigger_providers": sorted(requested),
        "staged_pricing_providers": sorted(staged_pricing),
        "staged_build_ready_providers": sorted(staged_build_ready),
        "maxbounty_rollout": maxbounty_rollout,
        "release": release,
        "daily_card_line": current["daily_card_line"],
    }
    if batch_preparation_only:
        result["preparation"] = preparation
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, default=worker.DEFAULT_LEDGER)
    parser.add_argument("--backrun-dir", type=Path)
    parser.add_argument("--pt-date", default=worker.pt_date())
    parser.add_argument("--deploy", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument("--authoring-receipt", action="append", type=Path, default=[])
    parser.add_argument("--pricing-source-receipt", action="append", type=Path,
                        default=None)
    parser.add_argument("--pricing-sources-explicit", action="store_true")
    parser.add_argument("--authoring-build-ready-handoff", action="append", type=Path,
                        default=[])
    parser.add_argument("--batch-authority", type=Path)
    args = parser.parse_args()
    try:
        result = run_day(
            output_dir=args.output_dir.resolve(),
            ledger=args.ledger.resolve(),
            backrun_dir=args.backrun_dir.resolve() if args.backrun_dir else None,
            date_pt=args.pt_date,
            deploy=bool(args.deploy),
            providers=args.provider,
            authoring_receipts=args.authoring_receipt,
            pricing_source_receipts=(
                args.pricing_source_receipt or []
                if args.pricing_sources_explicit
                else args.pricing_source_receipt
            ),
            build_ready_handoffs=args.authoring_build_ready_handoff,
            plan_only=args.plan_only,
            batch_authority=args.batch_authority,
        )
    except Exception as exc:
        print(json.dumps({"schema": "b2-wire-backlog-daily-run-v1", "status": "FAIL", "error": f"{type(exc).__name__}:{exc}"}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
