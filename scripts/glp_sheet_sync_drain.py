#!/usr/bin/env python3
"""Drain the `sheet_update_queue` into the governed Sheet writer.

The 2026-08-18 affiliate architecture makes the governed Google Sheet the single
source of truth and allows exactly ONE writer. Every other machine - the inbound
detector, the applications machine, the deploy machine, the liveness audit, the
reconciler - emits rows into the n8n datatable `sheet_update_queue` and never
touches the Sheet. This is that one writer's runtime.

What it does, per queue row:

  1. maps the row's `kind` to exactly one governed narrow mode in
     scripts/affiliate_sheet_batch_writer.py (SHEET_SYNC_KIND_MODE). An unknown
     kind is ESCALATED, never guessed at;
  2. resolves the target Sheet row - the queue's `sheet_row` when given, else an
     identity lookup by network + provider program id + program name. An
     ambiguous or absent match is REFUSED with the ambiguity spelled out;
  3. projects the queue row into that mode's narrow cells, carrying the queue's
     evidence into the row's own Notes/Blocker cell so the Sheet holds the
     receipt;
  4. runs the mode, which enforces its own preconditions, CAS-checks the whole
     sheet, writes, reads back, scans for collateral change and re-derives
     idempotency;
  5. records the outcome. A mode that refuses produces `refused` + the mode's
     own error string - surfaced to Discord and left in the queue's own record,
     never silently dropped.

Re-running the same queue rows is a no-op: every mode is idempotent by
construction, so a replay reports `noop` with zero cells written.

Nothing here decides truth. Approval truth comes from the network adapters,
liveness truth from the probes, deploy truth from the deploy receipts. This
runtime only transcribes an evidenced fact into the one place that is allowed to
hold it.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import affiliate_sheet_batch_writer as writer  # noqa: E402
from affiliate_sheet_schema_guard import EXPECTED_RANGE  # noqa: E402

RECEIPT_ROOT = Path(
    "/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/sheet-sync"
)
BUSINESS = Path("/home/ubuntu/business")
SCHEMA = "glp.sheet_sync_drain.v1"

#: Queue statuses this runtime may assign. `applied`/`noop` are successes,
#: `refused` is a governed mode saying no, `escalated` is this runtime saying it
#: does not know how - both stay visible.
RESULT_APPLIED = "applied"
RESULT_NOOP = "noop"
RESULT_REFUSED = "refused"
RESULT_ESCALATED = "escalated"
#: A row the writer could not even attempt for an infrastructure reason stays
#: QUEUED. Proven on the first production run: Google returned a 502 mid-batch
#: and one governed correction was recorded `refused` - which is terminal, so
#: the queue would never have retried a change nothing had actually rejected.
#: A refusal must mean a MODE said no, never that the network hiccuped.
RESULT_DEFERRED = "queued"

#: Lock contention is a CONTROLLED state, not a crash.
#:
#: Exactly one writer may hold `affiliate_sheet_batch_writer.LOCK`. When a
#: scheduled drain collides with a concurrent manual/agent drain (proven
#: 2026-08-18: n8n execution 131 at 15:45Z died on
#: `BlockingIOError: [Errno 11]`), the correct answer is *not* to crash and not
#: to wait for the lock (the other writer may hold it for minutes and the caller
#: is a scheduled workflow, not a human). The correct answer is to stand down
#: immediately, acknowledge NOTHING, and let the next scheduled drain pick the
#: same queue rows up. The lock itself is never removed and parallel writers are
#: never permitted.
#:
#: `RC_LOCK_CONTENDED` is a dedicated exit code so the caller can distinguish
#: "another drain is already running" from a genuine failure. 75 is
#: EX_TEMPFAIL - the conventional "try again later" rc.
RC_LOCK_CONTENDED = 75
STATUS_LOCK_CONTENDED = "DEFERRED_LOCK_BUSY"
LOCK_CONTENDED_NOTE = (
    "another drain is already running - deferring; queue rows preserved "
    "(unacknowledged) for the next run"
)

#: Failure shapes that are transient by nature: retry is the correct response,
#: and the next scheduled drain is the retry.
TRANSIENT_MARKERS = (
    "HttpError", "TimeoutError", "ConnectionError", "SSLError", "socket.timeout",
    "ServerNotFoundError", "BrokenPipeError", "IncompleteRead", "RemoteDisconnected",
    "sheet_concurrency_drift", "portfolio_sheet_apply_lock_busy",
    "Bad Gateway", "Service Unavailable", "Internal Error", "backendError",
    "rateLimitExceeded", "quotaExceeded", "Deadline Exceeded",
    "katalys_tracking_ready_ambiguous_",
)


def is_transient(exc: Exception) -> bool:
    """True when the failure is infrastructure, not a governed mode's refusal."""
    blob = f"{type(exc).__name__}:{exc}"
    if any(marker in blob for marker in TRANSIENT_MARKERS):
        # A 4xx that is not a rate limit is a real, non-retryable answer.
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status is not None and 400 <= int(status) < 500 and int(status) != 429:
            return False
        return True
    return False


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Fit-assessment binding (the `application_pending` niche-fit proof)
# ---------------------------------------------------------------------------
#
# `affiliate_program_relevance.create_verdict` refuses a CREATE whose program
# NAME carries no niche token, because the 2026-08-26 Partnerize mirror wrote 32
# junk rows and the only thing that separated them from real providers was
# PROVENANCE. Its docstring is explicit that the name is not the test - real
# GLP-1 providers are brandable (Zealthy, Eden, Wisp, Hers) - so a producer may
# override the name rule by shipping an assessment the drain can verify.
#
# Until 2026-09-17 there was exactly ONE way to verify one: an
# `operator_amendments` entry in ops/control/affiliate-apply-gate.json naming the
# list path and sha256. That made a run under a receipted operator SCOPE
# unrecordable - 10 of 41 provider-verified ClickDealer applications could not
# reach the Sheet at all (HealthRX, LevelsRx x2, Hers Inc., Lttl, WeightLoss,
# FeelGoodMeds x2, Wellorithm Medvi-Like Page) because no amendment enumerated
# them and minting one by hand is an authority change no seat may make. Operator
# 2026-09-17, in his own words: "yes add the 10 rows and fix the sheet job or
# whatever prevented this so it never happens again."
# (ops/linear/approvals/OPERATOR-DECISION-20260917-clickdealer-fit-verified-10-rows-persistent-fix.json)
#
# So a list may now be bound by EITHER
#   * an operator_amendments entry (unchanged, byte for byte), or
#   * the operator SCOPE RECEIPT the list itself names: a
#     `glp-operator-decision-v1` document under ops/linear/approvals/, whose
#     sha256 the list records and whose own words name the rail.
#
# Both branches keep every existing guard: the queued sha256 must match the
# file's real bytes, the list must hold EXACTLY ONE row for this
# (network, program_id), that row's classifier verdict must be CORE or SISTER,
# and its queue id must be authorized. Neither branch ever accepts a bare
# boolean or a caller-authored verdict, and the receipt branch additionally
# refuses a queue id the (network, program_id) pair does not derive, so a list
# cannot borrow another program's authorization.
FIT_LIST_SCHEMA = "glp-apply-fit-assessment-v1"
OPERATOR_DECISION_SCHEMA = "glp-operator-decision-v1"
#: The filename prefix reserved to the OPERATOR'S OWN decisions. ops/linear/approvals
#: also holds model-authored documents - FABLE-DECISION-*, REVIEW-*, PIN-ROTATION-* -
#: and at least one of them (FABLE-DECISION-20260916-gate-batch-audit-repairs.json)
#: carries `glp-operator-decision-v1` itself. Accepting the schema alone would let a
#: MODEL mint its own scope receipt and then fit-verify against it, which is the one
#: thing a model may never do: manufacture human approval. Independent review
#: 2026-09-17 caught exactly this.
OPERATOR_DECISION_PREFIX = "OPERATOR-DECISION-"
#: Scope receipts whose sha256 is PINNED HERE, in code.
#:
#: The prefix, the schema and `operator_words` are all writable by the same seats
#: that write FABLE-DECISION-*, so on their own they only move the forgery from
#: the schema to the filename: a model could author a new OPERATOR-DECISION-*.json
#: and grant itself fit_verified. scripts/protected_oracle_gate.py:76-87 already
#: settled this class - "The author writes the decision receipt" - and answers it
#: by freezing the sha256 of each receipt in code rather than trusting the
#: directory and the name. The same answer is used here. An operator receipt is
#: immutable, so pinning it costs nothing; opening a NEW rail costs one reviewed
#: code change, which is exactly the gate a self-authorized receipt must not skip.
#: Independent review 2026-09-17 found both holes, in this order.
AUTHORIZED_SCOPE_RECEIPT_SHA256: dict[str, str] = {
    "2a1044cd5687e1ec4b757967703bc80a390a656a7743e3060bd2ca9762c722ed":
        "ops/linear/approvals/"
        "OPERATOR-DECISION-20260917-apply-scope-glp1-telehealth-only-clickdealer.json"
        " - operator 2026-09-16T23:58:57Z in his own words, addendum 2026-09-17T00:30:43Z;"
        " bound by OPERATOR-DECISION-20260917-clickdealer-fit-verified-10-rows-persistent-fix.json"
        " (GLP-285).",
}
APPLY_GATE_PATH = "ops/control/affiliate-apply-gate.json"
APPLICATIONS_LEDGER_PATH = "reports/glp-clinic-scout/affiliate-machine/applications-ledger.json"
OPERATOR_APPROVALS_DIR = Path("ops/linear/approvals")
FIT_VERDICTS_ADMITTED = frozenset({"CORE", "SISTER"})


def expected_apply_queue_id(network: str, provider_program_id: str) -> str:
    """The one queue id a scope-receipt-bound fit list may carry for a program.

    Derived, never accepted. The amendment branch authorizes queue ids the
    OPERATOR enumerated in the gate file; a scope receipt authorizes a scope
    instead, so the list carries its own `queue_ids_authorized` and the drain
    re-derives what each row's id must be. Without the derivation a list could
    authorize one program under an id the operator granted to another.
    """
    return f"apply-{writer.canonical_network(network)}-{provider_program_id}"


def _repo_file(ref: Any, error: str) -> Path:
    """Resolve a repo-relative reference to a real file inside the repo."""
    text = str(ref or "").strip()
    if not text:
        raise ValueError(error)
    rel = Path(text)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(error)
    resolved = (BUSINESS / rel).resolve()
    if not resolved.is_relative_to(BUSINESS.resolve()) or not resolved.is_file():
        raise ValueError(error)
    return resolved


def _amendment_binding(resolved: Path, actual_sha: str) -> dict[str, Any] | None:
    """The pre-2026-09-17 binding: an operator amendment naming path + sha256."""
    authority = json.loads((BUSINESS / APPLY_GATE_PATH).read_text())
    bindings = [a for a in authority.get("operator_amendments", [])
                if a.get("apply_list_path") == str(resolved)
                and a.get("apply_list_sha256") == actual_sha]
    if len(bindings) != 1:
        return None
    return {
        "kind": "operator_amendment",
        "authority": APPLY_GATE_PATH,
        "queue_ids_allowed": [
            str(q) for q in (authority.get("scope", {}).get("queue_ids_allowed") or [])
        ],
    }


def _scope_receipt_binding(source: dict[str, Any], canonical: str) -> dict[str, Any] | None:
    """The 2026-09-17 binding: the operator scope receipt the list itself names.

    Returns None when the list does not even claim this shape, so the caller can
    report `not_operator_bound` exactly as before. Once it DOES claim the shape,
    every defect is a named refusal rather than a silent fall-through: a list
    that says it is receipt-bound and is not must never degrade to unbound.
    """
    if str(source.get("schema") or "") != FIT_LIST_SCHEMA:
        return None
    receipt = _repo_file(source.get("scope_receipt"),
                         "application_pending_fit_scope_receipt_invalid")
    if receipt.relative_to(BUSINESS.resolve()).parent != OPERATOR_APPROVALS_DIR:
        raise ValueError("application_pending_fit_scope_receipt_not_an_approval")
    receipt_sha = hashlib.sha256(receipt.read_bytes()).hexdigest()
    if str(source.get("scope_receipt_sha256") or "") != receipt_sha:
        raise ValueError("application_pending_fit_scope_receipt_hash_mismatch")
    decision = json.loads(receipt.read_text())
    # Three legs, because the directory is shared with model-authored documents:
    # the reserved OPERATOR-DECISION- filename, the operator-decision schema, and
    # the operator's own words actually recorded. Every model-authored file in
    # ops/linear/approvals today fails at least two of the three.
    if (not receipt.name.startswith(OPERATOR_DECISION_PREFIX)
            or str(decision.get("schema") or "") != OPERATOR_DECISION_SCHEMA
            or not str(decision.get("operator_words") or "").strip()):
        raise ValueError("application_pending_fit_scope_receipt_not_an_operator_decision")
    # ...and the receipt must be one this code was changed to trust. Everything
    # above is author-writable; this is not.
    if receipt_sha not in AUTHORIZED_SCOPE_RECEIPT_SHA256:
        raise ValueError("application_pending_fit_scope_receipt_not_pinned")
    # The operator's own document must name the rail it opened. A receipt about
    # some other network can never authorize this one.
    blob = " ".join(
        str(decision.get(key) or "")
        for key in ("decision", "operator_words", "mechanism", "keeps", "evidence_of_problem")
    ).casefold()
    if canonical not in blob:
        raise ValueError("application_pending_fit_scope_receipt_does_not_name_network")
    if writer.canonical_network(source.get("network")) != canonical:
        raise ValueError("application_pending_fit_list_network_mismatch")
    return {
        "kind": "operator_scope_receipt",
        "authority": str(receipt.relative_to(BUSINESS.resolve())),
        "authority_sha256": receipt_sha,
        "queue_ids_allowed": [str(q) for q in (source.get("queue_ids_authorized") or [])],
        "derived_queue_id": True,
    }


def ledger_fit_reference(network: str, provider_program_id: str) -> dict[str, Any] | None:
    """The fit assessment the APPLY MACHINE recorded for this exact application.

    Resolved here, at the single writer, rather than carried by whichever
    producer happened to enqueue the row. That is the same argument
    `affiliate_program_relevance` makes for putting the relevance filter at the
    chokepoint: "a rule enforced at the chokepoint cannot be forgotten by the
    next producer someone adds". It also means the binding is LIVE - when the
    apply machine's next run drops a program (an operator HOLD, say) it clears
    the reference, and the drain stops fit-verifying that program immediately,
    where a reference pinned into a queue row at enqueue time would outlive the
    hold. A queue row that carries its own reference still wins, so a producer
    that pins one keeps working unchanged.

    Returns None - never raises - when the ledger has no binding for this
    application; the caller then leaves the row to the ordinary name rule
    exactly as before.
    """
    try:
        ledger = json.loads((BUSINESS / APPLICATIONS_LEDGER_PATH).read_text())
    except (OSError, ValueError):
        return None
    canonical = writer.canonical_network(network)
    for row in ledger.get("applications") or []:
        if not isinstance(row, dict):
            continue
        if (writer.canonical_network(row.get("network")) != canonical
                or str(row.get("program_id") or "") != provider_program_id):
            continue
        # Only a provider-VERIFIED application may carry a fit proof. A planned,
        # failed or unreached attempt proves nothing about the relationship.
        receipt = row.get("provider_receipt")
        if (str(row.get("status") or "") != "SUBMITTED_VERIFIED"
                or not isinstance(receipt, dict)
                or str(receipt.get("status") or "") != "SUBMITTED_VERIFIED"):
            continue
        fit = row.get("fit_assessment")
        if not isinstance(fit, dict):
            continue
        path = str(fit.get("fit_assessment_path") or "").strip()
        sha = str(fit.get("fit_assessment_sha256") or "").strip()
        if path and sha:
            return {"fit_assessment_path": path, "fit_assessment_sha256": sha,
                    "source": "applications_ledger"}
    return None


def require_fit_assessment_binding(
    network: str, provider_program_id: str, evidence: dict[str, Any],
) -> dict[str, Any]:
    """Prove the queued fit assessment is operator-bound, exact and admitted.

    Raises on every failure; a caller that gets a dict back may set
    `fit_verified` and nothing else may.
    """
    resolved = _repo_file(evidence.get("fit_assessment_path"),
                          "application_pending_fit_path_invalid")
    actual_sha = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if evidence.get("fit_assessment_sha256") != actual_sha:
        raise ValueError("application_pending_fit_hash_mismatch")
    source = json.loads(resolved.read_text())
    canonical = writer.canonical_network(network)
    binding = (_amendment_binding(resolved, actual_sha)
               or _scope_receipt_binding(source, canonical))
    if binding is None:
        raise ValueError("application_pending_fit_not_operator_bound")
    matches = [r for r in source.get("programs", [])
               if r.get("network") == canonical
               and r.get("program_id") == provider_program_id]
    if (len(matches) != 1
            or matches[0].get("classifier", {}).get("verdict") not in FIT_VERDICTS_ADMITTED):
        raise ValueError("application_pending_fit_not_admitted")
    queue_id = str(matches[0].get("queue_id") or "")
    if queue_id not in binding["queue_ids_allowed"]:
        raise ValueError("application_pending_fit_queue_not_authorized")
    if (binding.get("derived_queue_id")
            and queue_id != expected_apply_queue_id(canonical, provider_program_id)):
        raise ValueError("application_pending_fit_queue_not_authorized")
    return {
        "binding": binding["kind"],
        "authority": binding["authority"],
        "fit_assessment_path": str(resolved.relative_to(BUSINESS.resolve())),
        "fit_assessment_sha256": actual_sha,
        "queue_id": queue_id,
        "verdict": str(matches[0].get("classifier", {}).get("verdict")),
    }


REPO = Path("/home/ubuntu/business")


def repo_relative(path: Path) -> str:
    """Repo-relative path when possible; the absolute path otherwise."""
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return str(path)


#: Statuses `network_reactivated` writes. A row carrying one of these has ALREADY
#: recorded the later half of the pause/reactivate lifecycle.
REACTIVATED_STATUSES = frozenset({
    writer.NETWORK_REACTIVATION_STATUS_WITH_LINK,
    writer.NETWORK_REACTIVATION_STATUS_WITHOUT_LINK,
})


def parse_utc(text: Any) -> datetime | None:
    """Best-effort UTC datetime from the timestamp shapes this box writes.

    Deliberately lenient about shape and strict about ambiguity: anything it
    cannot read comes back None, and every caller treats None as "unknown", not
    as "old".
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    candidate = raw.replace("Z", "+00:00")
    for value in (candidate, candidate[:19], candidate[:10]):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def lifecycle_event_at(evidence: dict[str, Any]) -> datetime | None:
    """When the lifecycle event this queue row asserts actually HAPPENED.

    Not when it was queued and not when it was captured — a mailbox sweep run
    today routinely captures a notice the network sent days ago, and it is the
    notice's own date that orders the lifecycle. The inbox-mirror capture file
    carries `internal_date_ms` / `date_header`; API-side detectors carry
    `observed_at_utc`. Missing is missing.
    """
    for key in ("event_at_utc", "observed_at_utc", "paused_at_utc"):
        parsed = parse_utc(evidence.get(key))
        if parsed:
            return parsed
    path = str(evidence.get("path") or evidence.get("capture_path") or "").strip()
    if not path:
        return None
    candidate = Path(path) if Path(path).is_absolute() else REPO / path
    try:
        capture = json.loads(candidate.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(capture, dict):
        return None
    millis = capture.get("internal_date_ms")
    try:
        if millis not in (None, ""):
            return datetime.fromtimestamp(int(millis) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    header = str(capture.get("date_header") or "").strip()
    if header:
        try:
            from email.utils import parsedate_to_datetime

            parsed = parsedate_to_datetime(header)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    return parse_utc(capture.get("captured_at_utc"))


def lifecycle_supersession_reason(
    kind: str, evidence: dict[str, Any], row_values: list[Any] | None,
) -> str | None:
    """Refusal reason when an OLD pause notice would undo a RECORDED reactivation.

    `network_paused` and `network_reactivated` are the two halves of one
    lifecycle, and nothing in the queue orders them: a mailbox sweep enqueues by
    message id, and the 2026-08-17 Katalys pause notices sit in the same batch as
    the 2026-08-19 re-activations they were superseded by. Draining in queue
    order would therefore re-pause three programmes that the network has already
    turned back on — a silent revenue regression that no validator downstream can
    see, because a pause is a perfectly well-formed write.

    The rule: a pause may not be applied to a row that already reads re-activated
    unless the pause notice is NEWER than the last time that row was checked. A
    genuine second pause always is. A stale one never is. Evidence with no
    readable event time is refused rather than guessed at, because guessing here
    means guessing in the direction that costs money.
    """
    if kind != "network_paused" or not row_values:
        return None
    padded = writer.pad(row_values)
    current = str(padded[10] or "").strip()
    if current not in REACTIVATED_STATUSES:
        return None
    event = lifecycle_event_at(evidence)
    if event is None:
        return ("network_paused_superseded_reactivation_unorderable:"
                f"status={current} reason=evidence_carries_no_event_time")
    checked = parse_utc(padded[24])
    if checked is None:
        return ("network_paused_superseded_reactivation_unorderable:"
                f"status={current} reason=row_carries_no_checked_time")
    if event <= checked:
        return ("network_paused_superseded_by_recorded_reactivation:"
                f"status={current} pause_event_at={event.isoformat()} "
                f"row_checked_at={checked.isoformat()}")
    return None


#: Network label canonicalisation moved into affiliate_sheet_batch_writer on
#: 2026-08-25 so ONE definition serves both the supersession check here and
#: `sheet_sync_existing_matches` there. Two copies meant this module could see
#: that a programme was already on the Sheet while the writer could not find
#: the row it sat on, which is how Nootropics Depot (row 290, "Impact.com")
#: refused with `no_sheet_row_matches_program_identity`. Re-exported under the
#: original names so existing call sites and tests keep working unchanged.
NETWORK_LABEL_ALIASES = writer.NETWORK_LABEL_ALIASES
canonical_network = writer.canonical_network


def create_supersession_reason(
    values: list[list[Any]], queue_row: dict[str, Any],
) -> str | None:
    """A create-capable queue row whose fact the Sheet ALREADY carries.

    The failure this closes, proven live on 2026-08-19/20: a detector sees an
    approval, emits `new_approval`, and the drain escalates
    `new_approval_requires_researched_row_cells` — asking a research lane for
    cells that will never be written, because the programme is already on the
    Sheet by another route (the enumeration materializer, or a backfill). The
    escalation is permanent: nothing ever clears it, every drain re-reports it,
    and the enumerator re-queues it on every snapshot transition (Curex, Awin
    121970, did exactly this). Worse, the "fix" the escalation asks for is the
    dangerous one — handing `add_approved_row` researched cells for a programme
    we already track either burns its CAS reads on a refusal or, where the
    writer's identity axes miss a name variant, MINTS A DUPLICATE ROW.

    So this guard runs BEFORE the researched-cells demand and answers the prior
    question: is this fact new at all? It is deliberately narrow.

      * Identity is the writer's OWN index (`sheet_sync_existing_matches`) —
        re-implementing it here is how the drain and the writer end up with two
        opinions about what a duplicate is. The only thing widened is the
        network LABEL, via `NETWORK_LABEL_ALIASES`, and only to spellings of
        the same rail.
      * SAME-NETWORK ONLY. A brand approved on a second network is a genuinely
        new relationship and must get its own row; suppressing it would be
        losing an approval, which is far worse than an escalation. Cross-network
        candidates are left escalated for a governed identity ruling.
      * More than one candidate row = ambiguous = not answered here. An
        ambiguous identity must never be resolved by a guard whose whole job is
        to say "nothing to do".

    It can only ever produce a NOOP. It writes nothing and it cannot create,
    overwrite or suppress a cell.
    """
    program_name = str(queue_row.get("program_name") or "").strip()
    program_id = str(queue_row.get("program_id") or "").strip()
    if not program_name and not program_id:
        return None
    queue_network = canonical_network(queue_row.get("network"))
    if not queue_network:
        return None

    labels: set[str] = set()
    for row in values[1:]:
        label = str(writer.pad(row)[4] or "").strip()
        if label and canonical_network(label) == queue_network:
            labels.add(label)
    if not labels:
        return None

    hits: dict[int, set[str]] = {}
    separate_identity = (
        str(parse_evidence(queue_row).get("operator_rule") or "")
        == "separate_provider_marketplace_identity_row"
    )
    for label in sorted(labels):
        for match in writer.sheet_sync_existing_matches(values, {
            "network": label,
            "provider_program_id": program_id,
            "program": program_name,
        }):
            # `tracking_link_program_id` is the one axis in the writer's index
            # that is NOT network-scoped, so a hit on it alone says nothing
            # about this rail and is dropped here rather than widened there.
            axes = {axis for axis in match["axes"] if axis != "tracking_link_program_id"}
            # Payout-evaluation notes deliberately name the future winner route.
            # A notes-only hit is therefore not proof that this row already owns
            # that winner identity when the operator has explicitly required a
            # separate provider/marketplace row. URL/link identity still blocks.
            if separate_identity and axes == {"notes_program_id"}:
                axes = set()
            if axes:
                hits.setdefault(match["row"], set()).update(axes)
    if len(hits) != 1:
        return None
    row_num, axes = next(iter(hits.items()))
    sheet_row = writer.pad(values[row_num - 1])
    # A same-network pending row carries the application, not the later
    # approval fact. Let `new_approval` continue to the researched-row path so
    # add_approved_row can promote that exact row instead of suppressing the
    # highest-value transition as a duplicate. Network-account parent rows are
    # intentionally excluded: account approval is not campaign approval.
    status = str(sheet_row[10] or "").strip()
    if (str(queue_row.get("kind") or "") == "new_approval"
            and str(queue_row.get("program_id") or "") != "network-account"
            and not status.startswith(writer.SHEET_SYNC_APPROVAL_CLAIM_PREFIXES)):
        return None
    return (
        f"{str(queue_row.get('kind') or 'create')}_superseded_by_existing_sheet_row:"
        f"row={row_num} axes={','.join(sorted(axes))} "
        f"sheet_program={str(sheet_row[2]).strip()!r} "
        f"sheet_network={str(sheet_row[4]).strip()!r} "
        f"sheet_status={str(sheet_row[10]).strip()!r} "
        f"queue_program={program_name!r} queue_network={str(queue_row.get('network') or '').strip()!r}"
    )


def provider_brand_key(value: Any) -> str:
    """Brand identity for detecting, never merging, cross-marketplace rails."""
    text = str(value or "").casefold()
    text = re.sub(r"\b(affiliate|partner)\s+program\b", " ", text)
    text = re.sub(r"\((?:us|usa|united states)\)$", " ", text.strip())
    text = re.sub(r"\b(?:us|usa|united states)\b$", " ", text.strip())
    return re.sub(r"[^a-z0-9]+", "", text)


def cross_marketplace_existing_rows(
    values: list[list[Any]], queue_row: dict[str, Any],
) -> list[dict[str, Any]]:
    """Rows for the same brand on other rails; informational, never a merge."""
    brand = provider_brand_key(queue_row.get("program_name"))
    network = canonical_network(queue_row.get("network"))
    if not brand or not network:
        return []
    matches = []
    for row_num, raw in enumerate(values[1:], 2):
        row = writer.pad(raw)
        row_network = canonical_network(row[4])
        if row_network and row_network != network and provider_brand_key(row[2]) == brand:
            matches.append({
                "sheet_row": row_num,
                "program_name": str(row[2] or ""),
                "network": str(row[4] or ""),
                "status": str(row[10] or ""),
            })
    return matches


def route_new_approval_research(
    run_dir: Path, queue_row: dict[str, Any], evidence: dict[str, Any],
    cross_marketplace_rows: list[dict[str, Any]],
) -> str:
    """Persist the honest research request a missing provider×rail row needs."""
    update_id = str(queue_row.get("update_id") or "new-approval")
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in update_id)[:80]
    directory = (BUSINESS / "reports/glp-clinic-scout/affiliate-machine/"
                 "new-approval-research-requests/pending")
    path = directory / f"{slug}.json"
    record = {
        "schema": "glp.new_approval_research_request.v1",
        "status": "PENDING_RESEARCH",
        "requested_at_utc": utc_now(),
        "queue_update_id": update_id,
        "network": str(queue_row.get("network") or ""),
        "program_id": str(queue_row.get("program_id") or ""),
        "program_name": str(queue_row.get("program_name") or ""),
        "provider_marketplace_identity": {
            "provider_key": provider_brand_key(queue_row.get("program_name")),
            "marketplace": canonical_network(queue_row.get("network")),
        },
        "cross_marketplace_existing_rows": cross_marketplace_rows,
        "approval_evidence": evidence,
        "required_researched_fields": [
            "category", "fit_reason", "official_url", "vertical", "suggested_glp_page",
        ],
        "consumer": "scripts/glp_new_approval_research_queue.py",
        "rule": "research with first-party captures and verbatim citations; never invent cells",
    }
    directory.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        previous = json.loads(path.read_text())
        previous.pop("requested_at_utc", None)
        comparable = dict(record)
        comparable.pop("requested_at_utc", None)
        if previous == comparable:
            return repo_relative(path)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)
    return repo_relative(path)


def canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def authority_sha256(queue_row: dict[str, Any]) -> str:
    """Bind the plan to the exact queue row that authorised it."""
    return hashlib.sha256(canonical(queue_row).encode("utf-8")).hexdigest()


def parse_evidence(queue_row: dict[str, Any]) -> dict[str, Any]:
    """Queue evidence is a JSON string (n8n datatable columns are scalars)."""
    raw = queue_row.get("evidence")
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"evidence_not_json:{exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("evidence_not_an_object")
    return parsed


#: Evidence fields the drain may MATERIALIZE, per kind.
#:
#: n8n cloud has no filesystem on this box, so a detector running there can
#: observe a real state change (an API relationship flipping to paused, a
#: programme disappearing from a healthy network's enumeration) and still have
#: nowhere to put the evidence file every governed mode demands. Refusing those
#: rows forever would mean the API sweep - the one detector that is already
#: LIVE - could never feed the writer at all.
#:
#: So the drain, which DOES run on the box, persists the observation the queue
#: row asserts and cites that file. This is not inventing evidence: the file
#: contains the queue row verbatim, including who queued it, when, from which
#: snapshot, and the n8n execution that saw it. Anyone can open the citation in
#: the Sheet and read exactly what the machine acted on. What the drain will
#: never do is materialize a field the queuer left semantically empty - the
#: observation itself (relationship, verdict, source) must already be there.
#:
#: What is DELIBERATELY absent: every kind that talks a row UP.
#: `cross_network_duplicate_relationship` and `approval_confirmed` both write an
#: approval, and an approval is the one claim the drain may never manufacture
#: from a queue row's say-so - it costs money to be wrong about and it is the
#: gate the whole wire-in lane sits behind. Those kinds demand a first-party
#: capture that exists on disk, hash-matched, before the writer will look at
#: them. Two tests pin the absence.
#:
#: `commercial_economics_hold` is absent for the mirror-image reason: it talks a
#: row OUT of the wire-in path. The authenticated commission read IS the whole
#: content of that claim, so a drain that could manufacture it could park any
#: approved, PAYING program in an economics hold and quietly stop it earning.
#: Same rule, both directions: the drain may not manufacture the evidence that
#: decides whether a program is placeable.
MATERIALIZABLE = {
    "network_paused": ("path", ("message_id",)),
    "rejection": ("rejection_evidence_path", ("source",)),
    "application_pending": ("submission_receipt_path", ("source",)),
    "status_correction": ("portal_capture_path", ("portal_relationship", "captured_at_utc")),
}


def materialize_evidence(kind: str, queue_row: dict[str, Any], evidence: dict[str, Any],
                         run_dir: Path) -> dict[str, Any]:
    """Persist an API-observed queue row's evidence and cite the file."""
    spec = MATERIALIZABLE.get(kind)
    if not spec:
        return evidence
    field, required = spec
    if str(evidence.get(field) or "").strip():
        return evidence  # the queuer cited a real file; leave it alone
    for name in required:
        if not str(evidence.get(name) or "").strip():
            raise ValueError(f"{kind}_evidence_field_missing:{name}")
    update_id = str(queue_row.get("update_id") or "row")
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in update_id)[:80]
    directory = run_dir / "queued-observations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slug}.json"
    record = {
        "schema": "glp.sheet_sync_queued_observation.v1",
        "materialized_at_utc": utc_now(),
        "materialized_by": "glp_sheet_sync_drain.py",
        "reason": ("the queuing detector runs in n8n cloud and has no filesystem here; "
                   "this file is the observation it asserted, persisted verbatim so the "
                   "Sheet's citation resolves to something a human can open"),
        "queue_row": {key: value for key, value in queue_row.items() if key != "evidence"},
        "observation": evidence,
    }
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)
    enriched = dict(evidence)
    enriched[field] = repo_relative(path)
    if kind == "status_correction":
        enriched["portal_capture_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return enriched


def clip(text: str, limit: int = 4500) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def validate_maxbounty_application_pending_evidence(
        queue_row: dict[str, Any], evidence: dict[str, Any]) -> None:
    """Bind a MaxBounty Sheet update to the verified action and provider journal."""
    if str(queue_row.get("network") or "").strip().casefold() != "maxbounty":
        return
    relative = Path(str(evidence.get("submission_receipt_path") or ""))
    expected_sha = str(evidence.get("submission_receipt_sha256") or "").lower()
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("maxbounty_application_receipt_path_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("maxbounty_application_receipt_sha_invalid")
    action_path = (BUSINESS / relative).resolve()
    try:
        action_path.relative_to(
            (BUSINESS / "reports/glp-clinic-scout/affiliate-machine/applications").resolve())
    except ValueError as exc:
        raise ValueError("maxbounty_application_receipt_outside_application_root") from exc
    if hashlib.sha256(action_path.read_bytes()).hexdigest() != expected_sha:
        raise ValueError("maxbounty_application_receipt_sha_drift")
    action = json.loads(action_path.read_text(encoding="utf-8"))
    if action.get("schema") != "glp.maxbounty.application-action.v1":
        raise ValueError("maxbounty_application_receipt_schema_invalid")
    if str(action.get("program_id") or "") != str(queue_row.get("program_id") or ""):
        raise ValueError("maxbounty_application_receipt_program_mismatch")
    status = str(action.get("status") or "")
    submitted = status == "SUBMITTED_VERIFIED"
    provider_pending = status == "PROVIDER_STATE_PENDING_NO_ATTEMPT"
    if not submitted and not provider_pending:
        raise ValueError("maxbounty_application_receipt_status_invalid")
    if submitted and (
            int(action.get("provider_application_attempts") or 0) != 1
            or int(action.get("gate_slot_consumed") or 0) != 1):
        raise ValueError("maxbounty_application_receipt_attempt_boundary_invalid")
    if provider_pending:
        detail = action.get("campaign_detail_state") or {}
        note = str(queue_row.get("note") or "").casefold()
        if (int(action.get("provider_application_attempts") or 0) != 0
                or int(action.get("provider_mutations") or 0) != 0
                or detail.get("classification") != "application_pending"
                or not detail.get("pending_markers_seen")
                or "not a submit receipt" not in note):
            raise ValueError("maxbounty_provider_pending_action_contract_invalid")
    attempt_path = Path(str(action.get("attempt_receipt") or ""))
    if not attempt_path.is_absolute():
        attempt_path = BUSINESS / attempt_path
    attempt_path = attempt_path.resolve()
    try:
        attempt_path.relative_to(
            (BUSINESS / "reports/glp-clinic-scout/affiliate-machine/applications").resolve())
    except ValueError as exc:
        raise ValueError("maxbounty_provider_attempt_receipt_outside_application_root") from exc
    attempt_sha = str(action.get("attempt_receipt_sha256") or "").lower()
    if (not re.fullmatch(r"[0-9a-f]{64}", attempt_sha)
            or hashlib.sha256(attempt_path.read_bytes()).hexdigest() != attempt_sha):
        raise ValueError("maxbounty_provider_attempt_receipt_sha_invalid")
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    attempt_contract_valid = (
        attempt.get("schema") == "glp.maxbounty.provider-application-attempt.v1"
        and attempt.get("status") == status
        and str(attempt.get("program_id") or "") == str(queue_row.get("program_id") or "")
    )
    if submitted:
        attempt_contract_valid = attempt_contract_valid and (
            int(attempt.get("provider_application_attempts") or 0) == 1
            and int(attempt.get("gate_slot_consumed") or 0) == 1
        )
    else:
        detail = attempt.get("campaign_detail_state") or {}
        observed_attempt_path = Path(str(evidence.get("provider_observation_receipt_path") or ""))
        if not observed_attempt_path.is_absolute():
            observed_attempt_path = BUSINESS / observed_attempt_path
        attempt_contract_valid = attempt_contract_valid and (
            int(attempt.get("provider_application_attempts") or 0) == 0
            and int(attempt.get("provider_mutations") or 0) == 0
            and detail.get("classification") == "application_pending"
            and bool(detail.get("pending_markers_seen"))
            and observed_attempt_path.resolve() == attempt_path
            and str(evidence.get("provider_observation_receipt_sha256") or "").lower()
                == attempt_sha
        )
    if not attempt_contract_valid:
        raise ValueError("maxbounty_provider_attempt_receipt_contract_invalid")


# ---------------------------------------------------------------------------
# Row resolution
# ---------------------------------------------------------------------------


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def deployed_live_tuple_binding(
    queue_row: dict[str, Any], evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Validate and return the full deployed-live provider/rail identity."""
    evidence = parse_evidence(queue_row) if evidence is None else evidence
    network_raw = str(queue_row.get("network") or "").strip()
    network = canonical_network(network_raw)
    provider_program_id = str(queue_row.get("program_id") or "").strip()
    program_name = str(queue_row.get("program_name") or "").strip()
    # `sheet_update_queue` is a fixed 12-column n8n Data Table. Provider/site
    # slug and stable key belong to its hash-bound JSON evidence; inventing new
    # top-level columns makes an otherwise valid row fail at queue insertion.
    provider_slug = str(evidence.get("provider_slug") or "").strip()
    try:
        sheet_row = int(queue_row.get("sheet_row") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("deployed_live_sheet_row_invalid") from exc
    stable_key = str(evidence.get("stable_key") or "").strip()
    expected_stable_key = f"{network}:{provider_program_id}"
    if (
        not network
        or not provider_program_id
        or not program_name
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", provider_slug)
        or sheet_row < 2
        or stable_key != expected_stable_key
    ):
        raise ValueError("deployed_live_tuple_shape_invalid")
    registration = {
        "provider": provider_slug,
        "network": network_raw,
        "program_name": program_name,
        "program_id": provider_program_id,
        "sheet_row": sheet_row,
    }
    tuple_value = {
        **registration,
        "network": network,
        "stable_key": stable_key,
    }
    expected_evidence = {
        "network": network,
        "network_program_id": provider_program_id,
        "program_name": program_name,
        "provider_slug": provider_slug,
        "sheet_row": sheet_row,
        "stable_key": stable_key,
        "tuple_sha256": canonical_sha256(tuple_value),
        "program_registration_sha256": canonical_sha256(registration),
    }
    for key, expected in expected_evidence.items():
        actual = evidence.get(key)
        if key == "sheet_row":
            try:
                actual = int(actual)
            except (TypeError, ValueError):
                pass
        if actual != expected:
            raise ValueError(f"deployed_live_evidence_tuple_mismatch:{key}")
    return {**tuple_value, "tuple_sha256": expected_evidence["tuple_sha256"]}


def resolve_sheet_row(
    values: list[list[Any]], queue_row: dict[str, Any],
) -> tuple[int | None, str | None]:
    """Find the Sheet row this queue row is about.

    An explicit `sheet_row` wins and is verified against the queue's program
    name, because a stale row number pointing at some other program is exactly
    how a governed writer would corrupt an unrelated row.
    """
    if str(queue_row.get("kind") or "") == "katalys_tracking_ready":
        try:
            explicit = queue_row.get("sheet_row")
            if type(explicit) is not int or explicit < 2:
                raise ValueError("frozen_sheet_row_invalid")
            entry = build_entry("katalys_tracking_ready", queue_row,
                                parse_evidence(queue_row), explicit, None)
            writer.require_katalys_tracking_ready(entry, values)
            return explicit, None
        except (RuntimeError, ValueError, TypeError, KeyError, AttributeError) as exc:
            return None, f"sheet_row_katalys_identity_invalid:{exc}"
    program_name = str(queue_row.get("program_name") or "").strip()
    explicit = queue_row.get("sheet_row")
    if explicit not in (None, "", 0, "0"):
        try:
            row_num = int(explicit)
        except (TypeError, ValueError):
            return None, f"sheet_row_not_an_integer:{explicit!r}"
        if row_num < 2:
            return None, f"sheet_row_out_of_range:{row_num}"
        if len(values) < row_num or not writer.row_has_business_value(values[row_num - 1]):
            return None, f"sheet_row_blank:{row_num}"
        actual = writer.pad(values[row_num - 1])
        if program_name and writer.norm(actual[2]) != writer.norm(program_name):
            return None, (
                f"sheet_row_program_mismatch:{row_num}:"
                f"sheet={str(actual[2]).strip()!r} queue={program_name!r}"
            )
        if str(queue_row.get("kind") or "") == "deployed_live":
            try:
                binding = deployed_live_tuple_binding(queue_row)
            except ValueError as exc:
                return None, f"sheet_row_deployed_live_identity_invalid:{exc}"
            if canonical_network(actual[4]) != binding["network"]:
                return None, (
                    f"sheet_row_network_mismatch:{row_num}:"
                    f"sheet={str(actual[4]).strip()!r} queue={binding['network']!r}"
                )
            axes = writer.provider_program_identity_axes(
                actual, binding["network"], binding["program_id"]
            )
            if not axes:
                return None, (
                    f"sheet_row_program_id_mismatch:{row_num}:"
                    f"queue={binding['program_id']!r}"
                )
            if writer.norm(actual[9]) != writer.norm(binding["provider"]):
                return None, (
                    f"sheet_row_provider_slug_mismatch:{row_num}:"
                    f"sheet={str(actual[9]).strip()!r} queue={binding['provider']!r}"
                )
        return row_num, None

    matches = writer.sheet_sync_existing_matches(values, {
        "network": queue_row.get("network"),
        "provider_program_id": queue_row.get("program_id"),
        "program": program_name,
    })
    if not matches:
        return None, "no_sheet_row_matches_program_identity"
    distinct = sorted({item["row"] for item in matches})
    if len(distinct) > 1:
        return None, f"sheet_row_ambiguous:{distinct}"
    return distinct[0], None


def name_collisions(values: list[list[Any]], queue_row: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows naming the same program under a DIFFERENT network label.

    Not a refusal and not a merge — a flag. `sheet_sync_existing_matches` scopes
    every axis by network, which is correct (the same brand really can run on two
    networks), but it means a variant label silently permits a duplicate row.
    Surfacing the collision lets the reconciler decide, and keeps the duplicate
    from being invisible in the meantime.
    """
    program = writer.norm(queue_row.get("program_name"))
    network = writer.norm(queue_row.get("network"))
    if not program:
        return []
    out = []
    for row_num, row in enumerate(values[1:], 2):
        actual = writer.pad(row)
        if not writer.row_has_business_value(actual):
            continue
        if writer.norm(actual[2]) == program and writer.norm(actual[4]) != network:
            out.append({"row": row_num, "program": str(actual[2]).strip(),
                        "sheet_network": str(actual[4]).strip(),
                        "queue_network": str(queue_row.get("network") or "").strip(),
                        "sheet_status": str(actual[10]).strip()})
    return out


# ---------------------------------------------------------------------------
# Per-kind cell projection
# ---------------------------------------------------------------------------


def _note(queue_row: dict[str, Any], head: str) -> str:
    tail = str(queue_row.get("note") or "").strip()
    queued_by = str(queue_row.get("queued_by") or "sheet_sync_drain").strip()
    update_id = str(queue_row.get("update_id") or "").strip()
    parts = [head, f"queued_by={queued_by}"]
    if update_id:
        parts.append(f"queue_update_id={update_id}")
    if tail:
        parts.append(tail)
    return clip(" ".join(parts))


def build_entry(
    kind: str, queue_row: dict[str, Any], evidence: dict[str, Any],
    row_num: int | None, sheet_row_values: list[Any] | None,
) -> dict[str, Any]:
    """Project one queue row into its mode's plan entry.

    Raises ValueError with a machine-readable reason when the queue row does not
    carry what the mode needs. That reason is surfaced, not swallowed.
    """
    program = str(queue_row.get("program_name") or "").strip()
    checked = utc_now()

    if kind == "research_only_provider":
        if (queue_row.get("sheet_row") not in (None, "", 0, "0")
                or queue_row.get("network") != "Research-only provider"
                or str(queue_row.get("program_id") or "") != evidence.get("provider_id")
                or any(key in evidence for key in ("row_cells", "approval_date", "tracking_link",
                                                    "payout", "apply", "signup_complete"))):
            raise ValueError("research_only_provider_forbidden_queue_claim")
        entry = {"program": program, "network": "Research-only provider",
                 "provider_program_id": evidence.get("provider_id"), "evidence": evidence,
                 "create": row_num is None}
        if row_num is not None:
            entry["row"] = row_num
        projected = writer.research_only_provider_cells(entry)
        entry["cells"] = (projected if row_num is None else {
            key: value for key, value in projected.items()
            if int(key) in writer.SHEET_SYNC_MODES[kind].mutable_columns})
        return entry

    if kind == "maxbounty_publisher_id":
        return {"row": row_num, "program": program,
                "network": queue_row.get("network"),
                "evidence": evidence,
                "cells": {"13": evidence.get("publisher_id")}}

    if kind in writer.provider_metadata.KINDS:
        contract = writer.provider_metadata.load_manifest(kind, evidence, BUSINESS)
        source = contract["source_row"]
        if (row_num != contract["sheet_row"] or program != source[2]
                or queue_row.get("network") != source[4]):
            raise ValueError("provider_metadata_queue_identity_mismatch")
        return {"row": row_num, "program": program, "evidence": evidence,
                "cells": writer.provider_metadata.manifest_cells(kind, contract)}

    if kind == "katalys_tracking_ready":
        if row_num is None or queue_row.get("sheet_row") != row_num:
            raise ValueError("katalys_tracking_ready_requires_frozen_existing_row")
        return {
            "row": row_num, "program": queue_row.get("program_name"),
            "network": queue_row.get("network"),
            "provider_program_id": queue_row.get("program_id"), "evidence": evidence,
            "cells": {"10": "approved_active_tracking_ready",
                      "14": evidence.get("tracking_link"), "19": "TRUE",
                      "21": "not_clicked_preserve_attribution",
                      "24": evidence.get("checked_at_utc")},
        }

    if kind == "junk_row_purge":
        rows = evidence.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValueError("junk_row_purge_requires_evidence_rows")
        return {"rows": rows}

    if kind == "application_pending":
        validate_maxbounty_application_pending_evidence(queue_row, evidence)
        path = str(evidence.get("submission_receipt_path") or "").strip()
        when = str(evidence.get("submitted_at_utc") or "").strip()
        head = (f"Application submitted {when}; submission receipt={path}; "
                f"awaiting brand decision.")
        cells = {
            "10": "applied_pending_brand_approval",
            "11": "TRUE", "19": "FALSE", "20": "FALSE",
            "23": _note(queue_row, head), "24": checked,
        }
        entry: dict[str, Any] = {"program": program, "evidence": evidence, "cells": cells}
        network = str(queue_row.get("network") or "").strip()
        provider_program_id = str(queue_row.get("program_id") or "").strip()
        # The fit proof is validated for EVERY application_pending row, not only
        # for a create. The relevance gate that consumes `fit_verified` fires on
        # creates alone, but a row that already exists is just as entitled to
        # carry the assessment in its receipt - and a readback that can only
        # report the fit of rows born today cannot answer "is every receipted
        # application fit-verified?" at all. Validated here, above the update
        # return, so both paths prove the same binding with the same guards.
        fit_reference = evidence if evidence.get("fit_assessment_path") else (
            ledger_fit_reference(network, provider_program_id)
            if network and provider_program_id else None)
        if fit_reference:
            if not network or not provider_program_id:
                raise ValueError("application_pending_fit_requires_network_and_program_id")
            entry["fit_binding"] = require_fit_assessment_binding(
                network, provider_program_id, fit_reference)
            entry["fit_binding"]["reference_source"] = str(
                fit_reference.get("source") or "queue_evidence")
            entry["fit_verified"] = True
        if row_num is not None:
            entry["row"] = row_num
            return entry

        # ---- create path: a brand-new provider we just applied to.
        # Same bargain `new_approval` strikes: the drain will not INVENT the
        # researched columns (category, fit reason, official URL, vertical). The
        # queuer holds that evidence, so it must ship it as `evidence.row_cells`
        # or the row escalates visibly instead of appearing half-true.
        payload = evidence.get("row_cells")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("application_pending_row_creation_requires_researched_cells")
        if not network or not provider_program_id:
            raise ValueError("application_pending_create_requires_network_and_program_id")
        researched = {str(key): value for key, value in payload.items()}
        # The status/flag/note/timestamp cells are the DRAIN's to state, never the
        # queuer's — otherwise a caller could queue a create that claims approval
        # or a live wire. The projected cells therefore overwrite the payload, and
        # a tracking-link claim on a pending row is rejected outright (the writer
        # enforces this too; failing here names the caller instead of the engine).
        if str(researched.get("14") or "").strip():
            raise ValueError("application_pending_create_must_not_claim_tracking_link")
        researched.update(cells)
        # Preserve the producer's assessed niche fit (validated above, for both
        # the update and the create path) so the relevance gate sees provenance
        # rather than the program's name.
        entry = {**entry, "network": network,
                 "provider_program_id": provider_program_id,
                 "create": True, "cells": researched}
        return entry

    if kind == "rejection":
        path = str(evidence.get("rejection_evidence_path") or "").strip()
        when = str(evidence.get("observed_at_utc") or "").strip()
        by = str(evidence.get("decided_by") or "network").strip().lower()
        status = ("rejected_brand_declined_application" if by == "brand"
                  else "rejected_network_declined_application")
        head = (f"Rejection recorded {when} by {by}; evidence={path}; "
                f"source={str(evidence.get('source') or '').strip()}.")
        cells = {
            "10": status, "19": "FALSE", "20": "FALSE",
            "23": _note(queue_row, head), "24": checked,
        }
        entry = {"program": program, "evidence": evidence, "cells": cells}
        if row_num is not None:
            entry["row"] = row_num
            return entry

        # ---- create path: a program the network rejected before it ever
        # reached the Sheet (proven live 2026-08-18: Awin/MicroVitamin 122466).
        # Same bargain as `application_pending`: the researched columns are
        # SHIPPED by the queuer as `evidence.row_cells` or the row escalates
        # visibly. A rejection is worth recording precisely so the applications
        # machine does not re-apply next month.
        payload = evidence.get("row_cells")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("rejection_row_creation_requires_researched_cells")
        network = str(queue_row.get("network") or "").strip()
        provider_program_id = str(queue_row.get("program_id") or "").strip()
        if not network or not provider_program_id:
            raise ValueError("rejection_create_requires_network_and_program_id")
        researched = {str(key): value for key, value in payload.items()}
        if str(researched.get("14") or "").strip():
            raise ValueError("rejection_create_must_not_claim_tracking_link")
        researched.update(cells)
        return {"program": program, "network": network,
                "provider_program_id": provider_program_id,
                "create": True, "evidence": evidence, "cells": researched}

    if kind == "invitation_disposition":
        path = str(evidence.get("invitation_capture_path") or "").strip()
        when = str(evidence.get("observed_at_utc") or "").strip()
        network_program_id = str(evidence.get("network_program_id") or "").strip()
        fit_classification = str(evidence.get("fit_classification") or "").strip()
        disposition = str(evidence.get("disposition") or "").strip()
        head = (
            f"Authenticated invitation recorded {when}; provider relationship=notjoined; "
            f"network programme={network_program_id}; fit_classification={fit_classification}; "
            f"disposition={disposition}; invitation capture={path}. No invitation acceptance, "
            "application, tracking-link generation or public wiring was executed."
        )
        cells = {
            "0": "LOW", "1": "FALSE",
            "10": writer.INVITATION_DISPOSITION_STATUS,
            "11": "FALSE", "19": "FALSE", "20": "FALSE",
            "21": "not_tested_no_link",
            "23": _note(queue_row, head), "24": checked,
        }
        if row_num is not None:
            return {"row": row_num, "program": program, "evidence": evidence, "cells": cells}
        payload = evidence.get("row_cells")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("invitation_disposition_row_creation_requires_researched_cells")
        network = str(queue_row.get("network") or "").strip()
        provider_program_id = str(queue_row.get("program_id") or "").strip()
        if not network or not provider_program_id:
            raise ValueError("invitation_disposition_create_requires_network_and_program_id")
        researched = {str(key): value for key, value in payload.items()}
        if str(researched.get("14") or "").strip():
            raise ValueError("invitation_disposition_create_must_not_claim_tracking_link")
        researched.update(cells)
        return {"program": program, "network": network,
                "provider_program_id": provider_program_id,
                "create": True, "evidence": evidence, "cells": researched}

    if kind == "marketplace_candidate":
        # A researched network we have NO relationship with. Every cell that
        # carries a claim is stated HERE, from the evidence, so a queuer can
        # supply the researched columns without being able to imply an account,
        # an application or an approval. The writer re-proves all of it.
        path = str(evidence.get("research_capture_path") or "").strip()
        when = str(evidence.get("researched_at_utc") or "").strip()
        apply_url = str(evidence.get("application_url") or "").strip()
        network_type = str(evidence.get("network_type") or "").strip()
        priority = str(evidence.get("priority") or "").strip()
        api = str(evidence.get("api_availability") or "").strip()
        requirements = str(evidence.get("approval_requirements") or "").strip()
        relevance = str(evidence.get("glp_relevance_evidence") or "").strip()
        terms = str(evidence.get("payment_terms") or "").strip()
        if not apply_url or not priority:
            raise ValueError("marketplace_candidate_requires_application_url_and_priority")
        head = (f"CANDIDATE NETWORK — RESEARCHED {when}, NOT APPLIED TO. We hold no "
                f"account, no application and no relationship with this network. "
                f"Apply at {apply_url}. Type={network_type}. Priority={priority}. "
                f"API: {api} Approval requirements: {requirements} "
                + (f"Payment terms: {terms} " if terms else "")
                + f"GLP-1 relevance: {relevance} Research capture={path}.")
        cells = {
            "0": priority,
            "1": "FALSE" if priority == "LOW" else "TRUE",
            "10": "candidate_not_applied",
            "11": "FALSE", "19": "FALSE", "20": "FALSE",
            "23": _note(queue_row, head), "24": checked,
        }
        entry = {"program": program, "evidence": evidence, "cells": cells}
        if row_num is not None:
            entry["row"] = row_num
            return entry

        # ---- create path: the normal case. A network nobody has ever filed.
        # As with `application_pending` and `rejection`, the researched columns
        # are SHIPPED by the queuer as `evidence.row_cells` - the drain will not
        # invent a category, a fit reason, an official URL or a vertical.
        payload = evidence.get("row_cells")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("marketplace_candidate_row_creation_requires_researched_cells")
        network = str(queue_row.get("network") or "").strip()
        provider_program_id = str(queue_row.get("program_id") or "").strip()
        if not network or not provider_program_id:
            raise ValueError("marketplace_candidate_create_requires_network_and_program_id")
        researched = {str(key): value for key, value in payload.items()}
        if str(researched.get("14") or "").strip():
            raise ValueError("marketplace_candidate_create_must_not_claim_tracking_link")
        # The Apply URL cell is the one the operator clicks, so it is taken from
        # the evidence rather than from the payload.
        researched["5"] = apply_url
        researched.update(cells)
        return {"program": program, "network": network,
                "provider_program_id": provider_program_id,
                "create": True, "evidence": evidence, "cells": researched}

    if kind == "impact_program_candidate":
        # A KNOWN Impact merchant that the canonical account can resolve by id,
        # whose authenticated campaign detail says `Never Joined`. This is not a
        # marketplace-network candidate: the account exists. It is also not an
        # application, so every relationship/live flag stays false and the mode
        # has no provider mutation path.
        path = str(evidence.get("campaign_capture_path") or "").strip()
        when = str(evidence.get("observed_at_utc") or "").strip()
        program_id = str(evidence.get("network_program_id") or "").strip()
        relationship = str(evidence.get("provider_relationship") or "").strip()
        head = (
            f"Impact campaign {program_id} resolved by known-id API lookup {when}; "
            f"provider_relationship={relationship}; campaign capture={path}. "
            f"Public terms: {str(evidence.get('commission_terms') or '').strip()}; "
            f"cookie={str(evidence.get('cookie_window') or '').strip()}. Candidate only: "
            "NO application or join was submitted, no tracking link was created, and the "
            "programme is not wire-in ready. Impact exposes no partner application endpoint; "
            "a later operator-authorized application run must use the portal."
        )
        cells = {
            "0": "HIGH", "1": "TRUE",
            "5": evidence.get("application_url"), "6": evidence.get("official_url"),
            "7": evidence.get("fit_reason"),
            "10": writer.IMPACT_PROGRAM_CANDIDATE_STATUS,
            "11": "FALSE", "16": evidence.get("commission_terms"),
            "17": evidence.get("cookie_window"), "19": "FALSE", "20": "FALSE",
            "21": "not_tested_no_link", "23": _note(queue_row, head), "24": checked,
            "25": evidence.get("vertical"), "26": evidence.get("api_capabilities"),
        }
        base = {"program": program, "network": queue_row.get("network"),
                "provider_program_id": queue_row.get("program_id"),
                "evidence": evidence, "cells": cells}
        if row_num is not None:
            # Apply/Official URLs are identity cells and no update mode may
            # rewrite them. They remain required and evidence-bound on CREATE;
            # an existing row keeps its established identity bytes.
            existing_cells = {key: value for key, value in cells.items() if key not in {"5", "6"}}
            return {"row": row_num, **base, "cells": existing_cells}
        payload = evidence.get("row_cells")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("impact_program_candidate_row_creation_requires_researched_cells")
        researched = {str(key): value for key, value in payload.items()}
        if str(researched.get("14") or "").strip():
            raise ValueError("impact_program_candidate_must_not_claim_tracking_link")
        researched.update(cells)
        return {**base, "create": True, "cells": researched}

    if kind == "cross_network_duplicate_relationship":
        path = str(evidence.get("approval_evidence_path") or "").strip()
        when = str(evidence.get("approved_at_utc") or "").strip()
        relationship = str(evidence.get("network_relationship") or "").strip()
        other_network = str(evidence.get("existing_placement_network") or "").strip()
        other_program = str(evidence.get("existing_placement_program_id") or "").strip()
        other_row = str(evidence.get("existing_placement_sheet_row") or "").strip()
        clause = str(evidence.get("exclusivity_clause") or "").strip()
        if not other_network or not other_program:
            raise ValueError("cross_network_duplicate_requires_existing_placement_identity")
        head = (f"{str(queue_row.get('network') or 'this network').strip()} approved us "
                f"{when} (relationship={relationship}), but this brand is ALREADY LIVE and "
                f"earning via {other_network} program {other_program} (sheet row {other_row}), "
                f"so this relationship is SUPERSEDED and must not be deployed. "
                f"Approval evidence={path}."
                + (f" Network exclusivity: \"{clause}\"" if clause else "")
                + " Which network the brand runs on is the operator's decision; nothing "
                  "was deployed and the live placement is untouched.")
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": "approved_superseded_by_existing_placement", "19": "FALSE", "20": "FALSE",
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "approval_confirmed":
        # The transition the machine could not say until 2026-08-18: an EXISTING
        # pending/invited row whose network has now approved us. It writes the
        # weakest true approval sentence - approved, not wired - and nothing more.
        path = str(evidence.get("approval_capture_path") or "").strip()
        when = str(evidence.get("confirmed_at_utc") or "").strip()
        relationship = str(evidence.get("network_relationship") or "").strip()
        program_id = str(evidence.get("network_program_id") or "").strip()
        if not program_id:
            raise ValueError("approval_confirmed_requires_the_network_program_id")
        current = str(writer.pad(sheet_row_values or [])[10] or "")
        network = str(queue_row.get("network") or "the network").strip()
        head = (f"{network} programme {program_id} APPROVED us: "
                f"network_relationship={relationship}, confirmed {when} against "
                f"first-party capture approval_capture_path={path}. The sheet's prior "
                f"'{current or 'unknown'}' claim is superseded by the approval itself. "
                f"Approved and NOT wired: no tracking link has been issued or deployed "
                f"here, and nothing in this row claims wire-ready or live.")
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": "approved_active_not_wired", "19": "FALSE", "20": "FALSE",
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "partner_email_hold":
        return {"row": row_num, "program": program, "network": queue_row.get("network"),
                "provider_program_id": queue_row.get("program_id"), "evidence": evidence,
                "cells": writer.partner_email_hold_cells(evidence)}

    if kind == "status_correction":
        relationship = str(evidence.get("portal_relationship") or "").strip()
        status = writer.STATUS_CORRECTION_RELATIONSHIP_STATUS.get(relationship)
        if not status:
            raise ValueError(f"status_correction_relationship_unknown:{relationship!r}")
        path = str(evidence.get("portal_capture_path") or "").strip()
        when = str(evidence.get("captured_at_utc") or "").strip()
        current = str(writer.pad(sheet_row_values or [])[10] or "")
        head = (f"Status corrected against portal capture {path} ({when}): the network "
                f"reports relationship={relationship}, so the sheet's prior "
                f"{current or 'unknown'} claim is retracted.")
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": status, "19": "FALSE", "20": "FALSE",
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "deployed_live":
        binding = deployed_live_tuple_binding(queue_row, evidence)
        path = str(evidence.get("deploy_receipt_path") or "").strip()
        url = str(evidence.get("live_url") or "").strip()
        release = str(evidence.get("release") or "").strip()
        head = (f"Wired live and verified: {url}; release={release}; deploy receipt={path}; "
                f"completeness sensor={str(evidence.get('sensor_status') or '').strip()}.")
        return {
            "row": row_num,
            "program": program,
            "network": str(queue_row.get("network") or ""),
            "provider_program_id": binding["program_id"],
            "provider_slug": binding["provider"],
            "stable_key": binding["stable_key"],
            "tuple_sha256": binding["tuple_sha256"],
            "evidence": evidence,
            "cells": {
            "10": "live_wired_verified", "19": "TRUE", "20": "TRUE",
            "21": "not_clicked_preserve_attribution",
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind in {"best_terms_winner_confirmed", "best_terms_route_retired"}:
        receipt = str(evidence.get("route_reconcile_path") or "").strip()
        payout = str(evidence.get("payout_decision_path") or "").strip()
        when = str(evidence.get("checked_at_utc") or "").strip()
        release = str(evidence.get("release") or "").strip()
        winner_row = str(evidence.get("winner_sheet_row") or "").strip()
        decision = str(evidence.get("decision") or "").strip()
        if kind == "best_terms_winner_confirmed":
            head = (f"Best-terms winner confirmed {when} in release={release}: Sheet row "
                    f"{winner_row} remains the sole href-verified live route; "
                    f"decision={decision}. Production href-only verification={receipt}; "
                    f"payout decision={payout}. No tracking link was requested or clicked.")
            return {"row": row_num, "program": program, "evidence": evidence, "cells": {
                "10": "live_wired_verified", "19": "TRUE", "20": "TRUE",
                "21": "not_clicked_preserve_attribution",
                "23": _note(queue_row, head), "24": checked,
            }}
        head = (f"Route retired {when} in release={release}: the same provider's higher-paying "
                f"rail on Sheet row {winner_row} is the sole live route; decision={decision}. "
                "Production href-only "
                f"verification={receipt}; payout decision={payout}. This marketplace "
                "relationship remains approved and its provider-issued tracking link remains "
                "tracking-ready history; no tracking link was requested or clicked.")
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": "approved_active_tracking_ready", "19": "TRUE", "20": "FALSE",
            "21": "not_clicked_preserve_attribution",
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "removed":
        path = str(evidence.get("removal_receipt_path") or "").strip()
        release = str(evidence.get("release") or "").strip()
        when = str(evidence.get("removed_at_utc") or "").strip()
        # The Link-QA cell says WHY the link was retired, and that is a fact the
        # removal receipt owns. Before 2026-08-18 this line was hardcoded to
        # `link_retired_network_program_paused`, so an operator niche-fit removal
        # (no network paused anything, all three relationships still approved)
        # put a false fact on the Sheet. The reason class is now READ from the
        # receipt here, and re-derived independently by the writer mode, so
        # neither the queuer nor the drain can choose which sentence is told.
        try:
            reason_class = writer.removal_receipt_reason_class(REPO / path)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        link_qa = writer.REMOVAL_REASON_CLASS_LINK_QA[reason_class]
        why = ("the network paused the program" if reason_class == "network_paused"
               else "the program is not a GLP Clinic Scout niche fit; the network "
                    "relationship remains approved and the tracking link stays valid")
        head = (f"Site surfaces removed {when}; release={release}; removal receipt={path}; "
                f"reason_class={reason_class} - {why}. "
                f"Tracking link retained as history; no live claim remains.")
        return {"row": row_num, "program": program,
                "evidence": dict(evidence, removal_reason_class=reason_class), "cells": {
                    "10": "removed_from_site_surfaces_deleted", "19": "FALSE", "20": "FALSE",
                    "21": link_qa,
                    "23": _note(queue_row, head), "24": checked,
                }}

    if kind == "marketplace_rail_status":
        path = str(evidence.get("rail_access_evidence_path") or "").strip()
        when = str(evidence.get("observed_at_utc") or "").strip()
        basis = str(evidence.get("access_basis") or "").strip()
        head = (f"Marketplace ACCOUNT access recorded {when}: {basis}. "
                f"Evidence={path}. This is a network rail row - the status records "
                f"that we hold the account, not that a merchant approved us.")
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": "approved_marketplace_access",
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "fit_assessment":
        path = str(evidence.get("fit_assessment_path") or "").strip()
        when = str(evidence.get("assessed_at_utc") or "").strip()
        verdict = str(evidence.get("verdict") or "").strip()
        vertical = str(evidence.get("vertical") or "").strip()
        fit_reason = str(evidence.get("fit_reason") or "").strip()
        if not fit_reason or not vertical:
            raise ValueError("fit_assessment_requires_fit_reason_and_vertical")
        head = (f"GLP fit assessed {when}: verdict={verdict}, vertical={vertical}. "
                f"Assessment={path}.")
        actual = writer.pad(sheet_row_values or [])
        existing_note = str(actual[23] or "")
        marker = "queue_update_id=" + str(queue_row.get("update_id") or "")
        note = existing_note if marker in existing_note.split() else (
            existing_note + ("\n" if existing_note else "") + _note(queue_row, head))
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "7": fit_reason, "25": vertical,
            "23": note, "24": checked,
        }}

    if kind == "marketplace_parent_relabel":
        path = str(evidence.get("parent_authority_path") or "").strip()
        message_id = str(evidence.get("gmail_message_id") or "").strip()
        when = str(evidence.get("observed_at_utc") or "").strip()
        decision = str(evidence.get("decision") or "").strip()
        directive = str(evidence.get("operator_directive") or "").strip()
        if not decision:
            raise ValueError("marketplace_parent_relabel_requires_a_decision")
        head = (f"Network-level application decided {when}: {decision}. "
                f"Evidence={path} (gmail {message_id}). Operator directive: "
                f"\"{directive}\". The rail is closed; this row no longer reads as a "
                f"pending application.")
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": decision,
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "commercial_economics_hold":
        # Approved, in scope, research complete - and unplaceable, because the
        # ADVERTISER configured no payable commission. The note has to make the
        # distinction that the status alone cannot: this is not a fit problem, an
        # evidence problem or a rejection, and the row re-enters the wire-in path
        # the moment a positive rate is configured.
        path = str(evidence.get("commission_verification_path") or "").strip()
        when = str(evidence.get("verified_at_utc") or "").strip()
        program_id = str(evidence.get("network_program_id") or "").strip()
        advertiser = str(evidence.get("network_advertiser_id") or "").strip()
        sale_type = str(evidence.get("commission_sale_type") or "commission").strip()
        if not program_id or not advertiser:
            raise ValueError(
                "commercial_economics_hold_requires_the_campaign_and_advertiser_ids")
        current = str(writer.pad(sheet_row_values or [])[10] or "")
        network = str(queue_row.get("network") or "the network").strip()
        head = (f"APPROVED and HELD on economics, not on fit or evidence. {network} "
                f"campaign {program_id} (advertiser {advertiser}) exposes no payable "
                f"commission to our publisher account: the only active {sale_type} is "
                f"configured at commission_value=0 / commission_rate=null, read live off "
                f"the authenticated commission endpoint {when} "
                f"(commission_verification_path={path}). Placing this program would send "
                f"reader clicks to a provider that pays nothing, so it is deliberately "
                f"not wired. The prior '{current or 'unknown'}' claim is superseded. The "
                f"relationship stays approved and the tracking link is untouched; the "
                f"research is COMPLETE, so the moment a positive commission is configured "
                f"this row re-enters wire-in with no research left to do.")
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": writer.COMMERCIAL_ECONOMICS_HOLD_STATUS, "19": "FALSE", "20": "FALSE",
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "evidence_blocked":
        path = str(evidence.get("capture_attempts_path") or "").strip()
        blocker = str(evidence.get("blocker") or "").strip()
        when = str(evidence.get("attempted_at_utc") or "").strip()
        head = (f"Approved, but first-party evidence capture is BLOCKED: {blocker} "
                f"(last attempted {when}, {evidence.get('attempts')} attempts, "
                f"capture log={path}). No review page can be built without fabricating "
                f"evidence, so nothing claims this program is wired.")
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": "approved_active_first_party_evidence_blocked", "19": "FALSE", "20": "FALSE",
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "network_cancelled":
        if row_num is None:
            raise ValueError("network_cancelled_requires_existing_row")
        event_id = str(evidence.get("event_id") or "").strip()
        path = str(evidence.get("path") or "").strip()
        when = str(evidence.get("event_at_utc") or "").strip()
        if not event_id or not path or not when:
            raise ValueError("network_cancelled_evidence_requires_event_id_path_and_event_at_utc")
        head = (f"Network cancelled the program on {when}. Evidence event {event_id} "
                f"saved at {path}. Wire-In Ready and Wired Live claims retracted; "
                f"tracking link retained as historical evidence, not deployable.")
        note = _note(queue_row, head)
        previous = str(writer.pad(sheet_row_values or [])[23] or "")
        note = previous if note in previous else "\n".join(filter(None, (previous, note)))
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": "cancelled_by_network", "19": "FALSE", "20": "FALSE",
            "21": "link_retired_network_program_cancelled", "23": note,
            "24": when,
        }}

    if kind == "network_paused":
        message_id = str(evidence.get("message_id") or "").strip()
        path = str(evidence.get("path") or "").strip()
        if not message_id or not path:
            raise ValueError("network_paused_evidence_requires_message_id_and_path")
        head = (f"Network paused the program. Evidence {message_id} saved at {path}. "
                f"Live/wired claim retracted; tracking link retained as history.")
        return {"row": row_num, "program": program,
                "evidence": {"message_id": message_id, "path": path}, "cells": {
                    "10": "paused_network_program_paused", "19": "FALSE", "20": "FALSE",
                    "21": "link_retired_network_program_paused",
                    "23": _note(queue_row, head), "24": checked,
                }}

    if kind == "network_reactivated":
        # The other half of `network_paused`. The status is NOT chosen here: the
        # writer re-derives it from the row's own tracking-link cell and refuses
        # if this projection disagrees, so a queuer cannot talk a paused row up
        # into link-ready by asserting a link the Sheet does not hold.
        path = str(evidence.get("reactivation_capture_path") or "").strip()
        when = str(evidence.get("observed_at_utc") or "").strip()
        wording = str(evidence.get("activation_wording") or "").strip()
        advertiser = str(evidence.get("advertiser") or "").strip()
        row_cells = writer.pad(sheet_row_values or [])
        has_link = bool(str(row_cells[14] or "").strip())
        status = (writer.NETWORK_REACTIVATION_STATUS_WITH_LINK if has_link
                  else writer.NETWORK_REACTIVATION_STATUS_WITHOUT_LINK)
        head = (f"Network RE-ACTIVATED the programme {when} (advertiser {advertiser}; the "
                f"notice says \"{wording}\", which is the word this network actually uses "
                f"- a 'reactivated' keyword scores zero on these mails). Capture="
                f"{path}. The pause claim is retracted. NOT live and NOT wire-ready: the "
                f"site surfaces the pause removed are restored by the deploy machine "
                f"behind its own receipt, and the retained tracking link is unproven "
                f"until it is re-validated.")
        cells = {
            "10": status, "19": "FALSE", "20": "FALSE",
            "23": _note(queue_row, head), "24": checked,
        }
        if has_link:
            cells["21"] = writer.NETWORK_REACTIVATION_LINK_QA
        return {"row": row_num, "program": program, "evidence": evidence, "cells": cells}

    if kind == "provider_scope_retraction":
        if row_num is None:
            raise ValueError("provider_scope_retraction_requires_existing_row")
        path = str(evidence.get("scope_retraction_path") or "").strip()
        when = str(evidence.get("assessed_at_utc") or "").strip()
        if not path or not when:
            raise ValueError("provider_scope_retraction_evidence_incomplete")
        head = (f"Provider scope gate retracted at {when}; evidence={path}. "
                "Network approval is not provider fit. No relationship, link, live flag "
                "or historical row is deleted; no new activation authority.")
        actual = writer.pad(sheet_row_values or [])
        existing_note = str(actual[23] or "")
        marker = "queue_update_id=" + str(queue_row.get("update_id") or "")
        note = existing_note if marker in existing_note else (
            existing_note + ("\n" if existing_note else "") + _note(queue_row, head))
        cells = {"19": "FALSE", "23": note, "24": when}
        if evidence.get("clear_slug") is True:
            cells["9"] = ""
        return {"row": row_num, "program": program, "evidence": evidence, "cells": cells}

    if kind == "wire_in_ready":
        # The generalised replacement for the Katalys-only col-19 write. It says
        # ONE thing — "the builder can build from this row's own link" — and it
        # says it only after the writer has structurally re-validated that link
        # against the rail the Sheet itself names. No status, no live claim, and
        # `Link QA` stays at the never-clicked value because nothing here fetches.
        network = str(evidence.get("network") or "").strip()
        immutable_id = str(evidence.get("immutable_id") or "").strip()
        rail_evidence = str(evidence.get("rail_evidence") or "").strip()
        plan_path = str(evidence.get("wire_in_plan_path") or "").strip()
        when = str(evidence.get("link_validated_at_utc") or "").strip()
        if not network or not immutable_id or not plan_path or not when:
            raise ValueError("wire_in_ready_evidence_incomplete")
        head = (f"Wire-in gate OPENED for the {network} rail (immutable id {immutable_id}). "
                f"The row's own tracking link was structurally validated against that "
                f"rail at {when} — host, path shape, immutable id and our publisher "
                f"identity — with ZERO tracking clicks; the link was never fetched. "
                f"Rail evidence: {rail_evidence or 'see the rail table'}. "
                f"Plan={plan_path}. This makes NO status or live claim: the build and "
                f"the deploy still have to happen and prove themselves.")
        cells = {
            "19": "TRUE", "21": "not_clicked_preserve_attribution",
            "23": _note(queue_row, head), "24": checked,
        }
        provisioned = str(evidence.get("provisioned_seo_slug") or "").strip()
        if provisioned:
            # Only ever set when the producer found column 9 BLANK. An existing
            # slug is never overwritten, because the producer does not send one.
            cells["9"] = provisioned
            basis = str(evidence.get("provisioned_seo_slug_basis") or "").strip()
            cells["23"] = _note(queue_row, head + (
                f" SEO slug provisioned as `{provisioned}` on this same write "
                f"({basis or 'derived on the approval-to-wire-in chain'}); the "
                "cell was blank, and a blank slug stops the research packet and "
                "therefore the page. No existing slug is ever overwritten."))
        return {"row": row_num, "program": program, "evidence": evidence,
                "cells": cells}

    if kind == "everflow_white_label_inventory":
        # Complete one EXISTING approved white-label row from a hash-bound,
        # authenticated offer-detail capture. The writer re-derives relationship,
        # payout, event, publisher id and link from those bytes. This projection
        # keeps col-19 closed so the independent rail validator remains the only
        # thing that can add the row to the build queue.
        path = str(evidence.get("portal_capture_path") or "").strip()
        when = str(evidence.get("captured_at_utc") or "").strip()
        offer_id = str(evidence.get("network_program_id") or "").strip()
        paid_event = str(evidence.get("paid_event") or "").strip()
        fit_ledger = str(evidence.get("fit_ledger_path") or "").strip()
        head = (
            f"Authenticated Everflow white-label inventory captured {when}: tenant="
            f"{evidence.get('tenant_display_name')}; offer/program ID={offer_id}; offer="
            f"{evidence.get('offer_name')}; relationship={evidence.get('provider_relationship')}; "
            f"payout={evidence.get('payout_terms')} with exact paid event={paid_event}; "
            f"provider-issued tracking link stored but never clicked. Portal capture={path}. "
            f"Fit/official-domain evidence={fit_ledger}. The offer's default destination URL "
            "and cookie window are genuinely unavailable in the captured detail; the Official "
            "URL is the hash-verified first-party brand root, and the Cookie Window cell is left "
            "blank. No application, link generation, deploy, spend or provider write occurred. "
            "Wire-In Ready remains FALSE until the independent rail validator passes."
        )
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "3": evidence.get("category"),
            "5": evidence.get("offer_detail_url"),
            "6": evidence.get("official_url"),
            "7": evidence.get("fit_reason"),
            "8": evidence.get("suggested_glp_page"),
            "9": evidence.get("seo_slug"),
            "10": "approved_active_tracking_ready",
            "11": "TRUE",
            "12": evidence.get("portal_origin"),
            "13": evidence.get("publisher_id"),
            "14": evidence.get("tracking_link"),
            "16": evidence.get("payout_terms"),
            "17": "",
            "19": "FALSE", "20": "FALSE",
            "21": "not_clicked_preserve_attribution",
            "22": evidence.get("compliance_notes"),
            "23": _note(queue_row, head), "24": checked,
            "25": evidence.get("vertical"),
            "26": evidence.get("api_capabilities"),
        }}

    if kind == "commission_terms_changed":
        # Phase 1b. The rate is NOT taken from the notice email - Awin's mail
        # carries no number at all, it only links the commission manager. It is
        # taken from the authenticated fetch this queue row cites, and the mode
        # re-proves that capture names this programme before anything is
        # written. The note records the OLD cell verbatim because a rate
        # correction whose before-state is unrecoverable is unauditable.
        path = str(evidence.get("commission_capture_path") or "").strip()
        when = str(evidence.get("fetched_at_utc") or "").strip()
        method = str(evidence.get("fetch_method") or "").strip()
        terms = " ".join(str(evidence.get("commission_terms") or "").split())
        cookie = " ".join(str(evidence.get("cookie_window") or "").split())
        program_id = str(evidence.get("network_program_id") or "").strip()
        notice = str(evidence.get("notice_message_id") or "").strip()
        census_correction = notice.startswith("not_applicable_authenticated_census_correction")
        if not terms:
            raise ValueError("commission_terms_correction_requires_the_fetched_terms")
        if not program_id:
            raise ValueError("commission_terms_correction_requires_the_network_program_id")
        if method not in ("network_api", "authenticated_ui", "operator_confirmed_partner_email"):
            raise ValueError(f"commission_terms_correction_fetch_method_unknown:{method!r}")
        row_cells = writer.pad(sheet_row_values or [])
        previous = " ".join(str(row_cells[16] or "").split())
        live = row_cells[20] is True or str(row_cells[20]).strip().upper() == "TRUE"
        network = str(queue_row.get("network") or "the network").strip()
        partner_email = method == "operator_confirmed_partner_email"
        head = (f"Commission terms {'confirmed by the partner manager for' if partner_email else 're-read from'} "
                f"{network} programme {program_id} on {when} via fetch_method={method}: {terms}. "
                + (f"Cookie window: {cookie}. " if cookie else "")
                + f"The cell previously read \"{previous or 'blank'}\", which the "
                  f"network's own surface no longer supports. "
                + (f"Operator-confirmed partner email {notice} explicitly stated the new rate; "
                   "the original RFC822 message is the hash-bound capture. " if partner_email else
                   "No notice was involved; the weekly best-terms census found this "
                   "event-label mismatch, and the correction comes from the authenticated "
                   "network capture. " if census_correction else
                   f"Notice {notice or 'unstated'} announced the change and carried no "
                   "rate; this correction comes from the fetch, not the mail. ")
                + f"commission_capture_path={path}. "
                + ("This row IS wired live, so any rate quoted on the site must be "
                   "re-checked against this value before the next release."
                   if live else
                   "This row is not wired live; nothing published quotes this rate yet."))
        cells: dict[str, Any] = {
            "16": terms, "23": _note(queue_row, head), "24": checked,
        }
        if cookie:
            cells["17"] = cookie
        return {"row": row_num, "program": program, "evidence": evidence, "cells": cells}

    if kind == "maxbounty_inventory":
        button = str(evidence.get("relationship_button") or "").strip()
        fit_class = str(evidence.get("fit_classification") or "").strip()
        vertical = str(evidence.get("vertical") or "").strip()
        status = writer.MAXBOUNTY_INVENTORY_STATUS.get((button, fit_class))
        relationship = writer.MAXBOUNTY_INVENTORY_RELATIONSHIP.get(button)
        if not status or not relationship:
            raise ValueError(
                f"maxbounty_inventory_button_or_fit_unknown:{button!r}:{fit_class!r}")
        approved = relationship == "approved"
        site_slug = str(evidence.get("seo_slug") or "").strip()
        suggested = str(evidence.get("suggested_glp_page") or "").strip()
        identity_basis = str(evidence.get("site_identity_basis") or "").strip()
        if approved and fit_class == "core_glp1":
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", site_slug):
                raise ValueError(f"maxbounty_approved_core_site_slug_invalid:{site_slug!r}")
            if suggested != site_slug:
                raise ValueError(
                    f"maxbounty_approved_core_site_identity_mismatch:"
                    f"{suggested!r}:{site_slug!r}")
            if identity_basis not in {
                "operator_mission_explicit_site_slug",
                "same_official_domain_existing_site_slug",
                "deterministic_provider_review_slug",
            }:
                raise ValueError(
                    f"maxbounty_approved_core_site_identity_basis_invalid:{identity_basis!r}")
        row_cells = writer.pad(sheet_row_values or [])
        source_status = str(row_cells[10] or "").strip()
        source_tracking_ready = (
            source_status in writer.MAXBOUNTY_INVENTORY_PRESERVED_STATUS)
        if source_tracking_ready and not approved:
            raise ValueError(
                "maxbounty_inventory_relationship_regressed_requires_dedicated_deactivation")
        preserve_tracking_ready = source_tracking_ready and approved
        if preserve_tracking_ready:
            status = source_status
        preserved_wire_ready = (
            row_cells[19] is True or str(row_cells[19] or "").strip().upper() == "TRUE")
        preserved_wired_live = (
            row_cells[20] is True or str(row_cells[20] or "").strip().upper() == "TRUE")
        priority = "HIGH" if fit_class == "core_glp1" else "LOW"
        path = str(evidence.get("portal_capture_path") or "").strip()
        when = str(evidence.get("captured_at_utc") or "").strip()
        program_id = str(evidence.get("network_program_id") or "").strip()
        provider_status = str(
            evidence.get("relationship_status_evidence") or button).strip()
        head = (
            f"MaxBounty campaign {program_id} enumerated {when}; authenticated relationship "
            f"evidence was '{button}', therefore relationship={relationship}. "
            f"Provider status evidence: {provider_status}. "
            f"fit_classification={fit_class}; vertical={vertical}; status={status}. "
            + ("Existing tracking-ready status, earned wire-ready/wired-live flags, and link QA "
               "were byte-preserved. "
               if preserve_tracking_ready else "")
            + f"Coupons: {evidence.get('coupon_summary')}. Payout: {evidence.get('payout_terms')}. "
            f"Cookie: {evidence.get('cookie_window')}. Portal capture={path}. "
            "No application was submitted, no tracking link was minted, and no provider was "
            "deployed. Apply remains FALSE; inventory cannot manufacture earned rail state."
        )
        cells = {
            "0": priority, "1": "FALSE", "7": evidence.get("fit_reason"), "10": status,
            "11": "TRUE" if approved else "FALSE",
            "15": evidence.get("coupon_summary"), "16": evidence.get("payout_terms"),
            "17": evidence.get("cookie_window"),
            "19": "TRUE" if preserve_tracking_ready and preserved_wire_ready else "FALSE",
            "20": "TRUE" if preserve_tracking_ready and preserved_wired_live else "FALSE",
            "21": (str(row_cells[21] or "not_clicked_preserve_attribution")
                   if preserve_tracking_ready else "not_tested_no_link"),
            "23": _note(queue_row, head), "24": checked,
            "25": vertical, "26": "Authenticated UI automation; MaxBounty has no publisher API",
        }
        if suggested:
            cells["8"] = suggested
        if site_slug:
            cells["9"] = site_slug
        entry = {"program": program, "evidence": evidence, "cells": cells}
        if row_num is not None:
            entry["row"] = row_num
            return entry
        network = str(queue_row.get("network") or "").strip()
        if writer.norm(network) not in {"maxbounty", "maxbountynetwork"} or not program_id:
            raise ValueError("maxbounty_inventory_create_requires_exact_network_and_program_id")
        cells.update({
            "2": program, "3": "Affiliate program", "4": "MaxBounty",
            "5": evidence.get("detail_url"), "6": evidence.get("official_url"),
        })
        return {"program": program, "network": network,
                "provider_program_id": program_id, "create": True,
                "evidence": evidence, "cells": cells}

    if kind == "affiliati_inventory":
        relationship = str(evidence.get("provider_relationship") or "").strip()
        status = writer.AFFILIATI_INVENTORY_STATUS.get(relationship)
        if not status:
            raise ValueError(f"affiliati_inventory_relationship_unknown:{relationship!r}")
        program_id = str(evidence.get("network_program_id") or "").strip()
        path = str(evidence.get("api_capture_path") or "").strip()
        when = str(evidence.get("captured_at_utc") or "").strip()
        apply_needed = relationship == "notjoined"
        head = (
            f"Affiliati offer {program_id} enumerated from authenticated API {when}; "
            f"provider_relationship={relationship}; exact paid conversion event="
            f"{evidence.get('exact_paid_event')}; payout={evidence.get('payout_terms')}; "
            f"fit_classification=core_glp1; SEO allowed. API capture={path}. "
            f"Queued promotion notes: {evidence.get('promotion_notes')} Apply path for the later "
            "operator-authorized batch: Offers tab > search > provider > media type SEO > paste "
            "promotion notes > submit. NO application was submitted, no tracking link was "
            "generated, and no provider or site mutation occurred."
        )
        cells = {
            "0": "HIGH", "1": "TRUE" if apply_needed else "FALSE",
            "7": evidence.get("fit_reason"), "8": evidence.get("suggested_glp_page"),
            "9": evidence.get("seo_slug"), "10": status,
            "11": "FALSE" if apply_needed else "TRUE",
            "16": evidence.get("payout_terms"), "19": "FALSE", "20": "FALSE",
            "21": "not_tested_no_link", "22": evidence.get("compliance_notes"),
            "23": _note(queue_row, head), "24": checked,
            "25": evidence.get("vertical"), "26": evidence.get("api_capabilities"),
        }
        entry = {"program": program, "network": queue_row.get("network"),
                 "provider_program_id": program_id, "evidence": evidence, "cells": cells}
        if row_num is not None:
            entry["row"] = row_num
            return entry
        network = str(queue_row.get("network") or "").strip()
        if writer.canonical_network(network) != "affiliati" or not program_id:
            raise ValueError("affiliati_inventory_create_requires_exact_network_and_program_id")
        cells.update({
            "2": program, "3": evidence.get("category"), "4": "Affiliati",
            "5": evidence.get("application_url"), "6": evidence.get("official_url"),
        })
        return {**entry, "network": network, "create": True}

    if kind == "affiliati_switchover_stage":
        provider = str(evidence.get("provider_name") or "").strip()
        program_id = str(evidence.get("network_program_id") or "").strip()
        status = str(evidence.get("target_status") or "").strip()
        approval_required = status == "awaiting_network_approval"
        checked = str(evidence.get("captured_at_utc") or "").strip()
        head = (
            f"Affiliati replacement staged link-dark: offer={program_id}; provider={provider}; "
            f"payout={evidence.get('payout_terms')}; campaign_id={evidence.get('campaign_id')}; "
            f"tracking_host={evidence.get('tracking_host')}; status={status}. Blocking prerequisites: "
            "obtain the exact per-offer tracking link; populate outbound s1 placement subid and "
            "s2 cs_click_id; build the postback receiver; confirm network echo tokens. "
            f"Switch capture={evidence.get('switch_capture_path')}; prerequisite registry="
            f"{evidence.get('prerequisite_registry_path')}; canonical postback "
            f"contract={evidence.get('postback_contract_path')}; exact offer terms="
            f"{evidence.get('terms_capture_path')}. No application, link generation, provider write, "
            "site write, or deployment occurred."
        )
        cells = {
            "0": "HIGH", "1": "TRUE" if approval_required else "FALSE",
            "7": evidence.get("fit_reason"), "8": evidence.get("suggested_glp_page"),
            "9": evidence.get("seo_slug"), "10": status,
            "11": "FALSE" if approval_required else "TRUE",
            "16": evidence.get("payout_terms"), "19": "FALSE", "20": "FALSE",
            "21": "not_tested_no_link", "22": evidence.get("compliance_notes"),
            "23": _note(queue_row, head), "24": checked,
            "25": evidence.get("vertical"), "26": evidence.get("api_capabilities"),
        }
        entry = {"program": program, "network": queue_row.get("network"),
                 "provider_program_id": program_id, "evidence": evidence, "cells": cells,
                 "fit_verified": True}
        if row_num is not None:
            entry["row"] = row_num
            return entry
        network = str(queue_row.get("network") or "").strip()
        if writer.canonical_network(network) != "affiliati" or not program_id:
            raise ValueError("affiliati_switchover_create_requires_exact_network_and_program_id")
        cells.update({
            "2": program, "3": evidence.get("category"), "4": "Affiliati",
            "5": evidence.get("application_url"), "6": evidence.get("official_url"),
        })
        return {**entry, "network": network, "create": True}

    if kind == "higher_rail_pending_replacement":
        checked = str(evidence.get("checked_at_utc") or "").strip()
        prior = str(writer.pad(sheet_row_values or [])[23] or "").strip()
        # This is a current fact, not an append-only log. Re-running after a
        # corrected source-offer payout must replace the previous pending tail
        # instead of preserving a stale dollar claim in the Sheet forever.
        prior = prior.split("[HIGHER RAIL PENDING]", 1)[0].strip()
        pending = (
            f"[HIGHER RAIL PENDING] Keep the current XenTraffic link as an unretired fallback, "
            f"but do not wire it as a new placement. Affiliati offer "
            f"{evidence.get('replacement_offer_id')} pays ${evidence.get('replacement_payout_usd')} "
            f"versus ${evidence.get('current_payout_usd')} here (+${evidence.get('dollar_delta')}); "
            f"campaign_id={evidence.get('replacement_campaign_id')}; host="
            f"{evidence.get('replacement_host')}. Retirement is forbidden until the replacement "
            f"link and postback path are live. Contract={evidence.get('postback_contract_path')}; "
            f"capture={evidence.get('switch_capture_path')}."
        )
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": writer.HIGHER_RAIL_PENDING_STATUS,
            "23": _note(queue_row, f"{prior} {pending}".strip()), "24": checked,
        }}

    if kind == "payout_evaluation":
        path = str(evidence.get("payout_evaluation_path") or "").strip()
        when = str(evidence.get("evaluated_at_utc") or "").strip()
        basis = str(evidence.get("basis") or "").strip()
        incumbent = str(evidence.get("incumbent_effective_value") or "").strip()
        challenger = str(evidence.get("challenger_effective_value") or "").strip()
        decision = str(evidence.get("decision") or "").strip()
        prior_note = str(writer.pad(sheet_row_values or [])[23] or "").strip()
        # Replace the prior payout-policy tail instead of nesting the whole note
        # every day. Everything before the marker is preserved byte-for-byte.
        base_note = prior_note.split("[PAYOUT POLICY]", 1)[0].strip()
        latest = (
            f"Cross-network payout policy evaluated {when}; provider_identity="
            f"{evidence.get('provider_identity')}; basis={basis}; formula="
            f"{evidence.get('formula')}; incumbent_effective_value={incumbent}; "
            f"challenger_effective_value={challenger}; decision={decision}; "
            f"evaluation={path}. The highest payout per qualified lead wins. A rejected or lower "
            "challenger leaves the incumbent live; a winning switch still requires a validated "
            "challenger link and one atomic site release; "
            "this note itself changes no link, status, flag or site surface."
        )
        head = f"{base_note} [PAYOUT POLICY] {latest}" if base_note else f"[PAYOUT POLICY] {latest}"
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "live_claim_correction":
        path = str(evidence.get("site_reconcile_path") or "").strip()
        when = str(evidence.get("checked_at_utc") or "").strip()
        release = str(evidence.get("release") or "").strip()
        finding = str(evidence.get("finding") or "").strip()
        cited_row = str(evidence.get("sheet_row") or "").strip()
        if not path or not when or not release or not finding or not cited_row:
            raise ValueError("live_claim_correction_evidence_incomplete")
        if row_num is None or cited_row != str(row_num):
            raise ValueError("live_claim_correction_sheet_row_mismatch")
        head = (
            f"Stale live claim retracted {when}; finding={finding}; release={release}; "
            f"site reconcile={path}; Sheet row={cited_row}. The current release has no "
            "monetized affiliate CTA and no /go route for this provider. Only Wired Live? "
            "was set FALSE; approval, tracking link, Link QA and Wire-In Ready remain "
            "unchanged so the normal deployment backlog can restore monetization."
        )
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "20": "FALSE", "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "provider_relationship_withdrawn":
        network = str(evidence.get("network") or "").strip()
        program_id = str(evidence.get("network_program_id") or "").strip()
        path = str(evidence.get("readback_path") or "").strip()
        when = str(evidence.get("captured_at_utc") or "").strip()
        api_status = str(evidence.get("provider_api_status") or "").strip()
        head = (
            f"{network} withdrew campaign {program_id}: its own authenticated campaign "
            f"page answered HTTP {api_status} for this account when read {when} "
            f"(classification terminal_provider_unavailable). Readback={path}. We were "
            "approved on this campaign and it had reached Wire-In Ready, so the gate is "
            "closed here to stop a placement shipping against a link the network no "
            "longer honours. This is NOT a declined application: the approval was real "
            "and the issued tracking link is left in place as the record of it. "
            "Wired Live is untouched."
        )
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": "provider_withdrew_campaign_relationship_gone",
            "19": "FALSE", "21": "not_tested_no_link",
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "rail_tracking_link":
        # One projection for every registered rail. The per-rail shape is the
        # rail registry's job, not this note's — see the mode definition in
        # affiliate_sheet_batch_writer for why this is one mode and not six.
        network = str(evidence.get("network") or "").strip()
        immutable_id = str(evidence.get("immutable_id") or "").strip()
        program_id = str(evidence.get("network_program_id") or "").strip()
        path = str(evidence.get("capture_path") or "").strip()
        when = str(evidence.get("captured_at_utc") or "").strip()
        quote = str(evidence.get("capture_quote") or "").strip()
        host = str(evidence.get("allowed_host") or "").strip()
        # BOTH IDS IN THE NOTE. On several rails the network's programme id and
        # the id the link is validated against are different strings — an opaque
        # offer token on Everflow-client, a camref on Partnerize, a link id on
        # CJ. A reader checking this row against the network's own portal needs
        # the programme id; a reader checking the LINK needs the immutable id.
        # Printing only one sends somebody looking for a string the portal does
        # not have.
        identity = (f"programme {program_id}" if program_id else "programme")
        if immutable_id and immutable_id != program_id:
            identity += f" (link id {immutable_id})"
        elif immutable_id:
            identity += f" (link id {immutable_id})"
        head = (
            f"{network} {identity} tracking link recorded from a first-party "
            f"authenticated capture taken {when}; capture={path}"
            + (f"; issuing host={host}" if host else "")
            + (f"; capture quote={quote}" if quote else "")
            + ". The href and the programme id come from that same capture, which was "
            "re-hashed before this write, and the link was validated against this rail's "
            "own observed shape. The link was never requested, opened or clicked. Wire-In "
            "Ready and Wired Live remain FALSE: opening the gate is a separate decision "
            "the wire-in producer makes on its own evidence."
        )
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "14": evidence.get("tracking_link"),
            "19": "FALSE", "20": "FALSE", "21": "not_clicked_preserve_attribution",
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "maxbounty_tracking_link":
        path = str(evidence.get("mint_receipt_path") or "").strip()
        when = str(evidence.get("minted_at_utc") or "").strip()
        program_id = str(evidence.get("network_program_id") or "").strip()
        configuration = str(evidence.get("tracking_configuration") or "").strip()
        head = (
            f"MaxBounty campaign {program_id} tracking link minted {when} through the gated "
            f"four-option builder; full reproducible configuration={configuration}; "
            f"mint receipt={path}. The Copy-link popup was read, but the link was never "
            "clicked. Wire-In Ready and Wired Live remain FALSE until structural rail "
            "validation and the atomic rollout sequence independently pass."
        )
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": "approved_active_tracking_ready", "14": evidence.get("tracking_link"),
            "19": "FALSE", "20": "FALSE", "21": "not_clicked_preserve_attribution",
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "everflow_brand_portal_tracking_link":
        rail_path = str(evidence.get("rail_capture_path") or "").strip()
        readback_path = str(evidence.get("readback_capture_path") or "").strip()
        when = str(evidence.get("captured_at_utc") or "").strip()
        tenant = str(evidence.get("tenant_display_name") or "").strip()
        offer_id = str(evidence.get("network_program_id") or "").strip()
        offer_name = str(evidence.get("offer_name") or "").strip()
        immutable_id = str(evidence.get("immutable_id") or "").strip()
        host = str(evidence.get("tracking_host") or "").strip()
        paid_event = str(evidence.get("exact_paid_event") or "").strip()
        network = str(evidence.get("network") or "").strip()
        head = (
            f"{tenant} approved our application for offer {offer_id} ({offer_name}) on its own "
            f"Everflow white-label tenant, and the tenant's authenticated publisher API issued "
            f"the tracking href on rail {network}: host {host}, immutable id {immutable_id}, "
            f"exact paid event {paid_event}. Read {when} by field inspection only from "
            f"{readback_path}; rail-validated capture={rail_path}. The href was never "
            "requested, resolved, opened or clicked. Wire-In Ready and Wired Live remain "
            "FALSE until the independent structural rail gate accepts the row."
        )
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": "approved_active_tracking_ready", "11": "TRUE",
            "13": evidence.get("publisher_id"),
            "14": evidence.get("tracking_link"), "16": evidence.get("payout_terms"),
            "19": "FALSE", "20": "FALSE",
            "21": "not_clicked_preserve_attribution", "23": _note(queue_row, head),
            "24": checked,
        }}

    if kind == "affiliati_tracking_link":
        path = str(evidence.get("campaign_capture_path") or "").strip()
        when = str(evidence.get("captured_at_utc") or "").strip()
        program_id = str(evidence.get("network_program_id") or "").strip()
        campaign_id = str(evidence.get("campaign_id") or "").strip()
        research = str(evidence.get("research_packet_path") or "").strip()
        reconcile = str(evidence.get("reconciliation_path") or "").strip()
        slug = str(evidence.get("governed_seo_slug") or "").strip()
        head = (
            f"Affiliati program {program_id}, campaign {campaign_id}, approved Link creative "
            f"href copied {when} from authenticated campaign configuration by field inspection "
            f"only; capture={path}; governed SEO Slug={slug}; nine-field research={research}; "
            f"best-terms reconciliation={reconcile}. The tracking href was never requested, "
            "resolved, opened, or clicked. Wire-In Ready and Wired Live remain FALSE until "
            "the independent structural rail gate accepts the row."
        )
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": "approved_active_tracking_ready", "11": "TRUE",
            "14": evidence.get("tracking_link"), "16": evidence.get("payout_terms"),
            "17": evidence.get("cookie_window"), "19": "FALSE", "20": "FALSE",
            "21": "not_clicked_preserve_attribution", "23": _note(queue_row, head),
            "24": checked,
        }}

    if kind == "portal_readback_confirmed":
        path = str(evidence.get("readback_capture_path") or "").strip()
        when = str(evidence.get("captured_at_utc") or "").strip()
        verdict = str(evidence.get("readback_verdict") or "").strip()
        status = writer.PORTAL_READBACK_VERDICT_STATUS.get(verdict)
        if not status:
            raise ValueError(f"portal_readback_verdict_unknown:{verdict!r}")
        reference = str(evidence.get("provider_account_reference") or "").strip()
        current = str(writer.pad(sheet_row_values or [])[10] or "")
        explain = {
            "approved_active": ("the provider's own authenticated surface reports us "
                                "approved and active, so the readback this row was "
                                "waiting on is DONE"),
            "membership_confirmed_approval_unproven": (
                "our membership of the provider's programme is confirmed, and the "
                "provider exposes NO approval field anywhere - so approval is neither "
                "claimed nor denied here, it is unproven"),
            "api_ip_locked_to_host": (
                "the marketplace API authenticates and answers from the licensed host; "
                "the key is IP-locked, which is why off-host (n8n cloud) calls fail. "
                "This is not a UI auth wall"),
            "available_via_approved_network_account": (
                "this network has no per-offer relationship model at all - every offer "
                "is available through the approved network account"),
        }[verdict]
        extra = str(evidence.get("api_ip_lock_host") or evidence.get("membership_basis") or "")
        head = (f"Provider readback DONE {when}: readback_verdict={verdict} - {explain}. "
                f"Account reference={reference}. "
                + (f"{extra.strip()} " if extra.strip() else "")
                + f"Capture={path}. The sheet's prior '{current or 'unknown'}' claim is "
                  f"superseded. Nothing here claims wire-ready or live.")
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": status, "19": "FALSE", "20": "FALSE",
            "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "operator_status_ruling":
        path = str(evidence.get("ruling_capture_path") or "").strip()
        when = str(evidence.get("observed_at_utc") or "").strip()
        ruling = str(evidence.get("operator_ruling") or "").strip()
        quote = " ".join(str(evidence.get("operator_note_quote") or "").split())
        status = writer.OPERATOR_RULING_STATUS.get(ruling)
        if not status:
            raise ValueError(f"operator_ruling_unknown:{ruling!r}")
        if not quote:
            raise ValueError("operator_status_ruling_requires_the_operator_quote")
        # The quote is carried FORWARD into the note on purpose: the writer proves
        # the operator's words are in the live cell on every run, replay included,
        # so the note this mode writes has to keep them there.
        head = (f"Operator ruling recorded {when}: operator_ruling={ruling}. The operator "
                f"wrote, in this row's own notes cell: \"{quote}\". Capture={path}. That "
                f"ruling is now the canonical Status; the operator's words are preserved "
                f"above verbatim.")
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "10": status, "23": _note(queue_row, head), "24": checked,
        }}

    if kind == "operator_note_secret_scrub":
        store = str(evidence.get("secret_store_path") or "").strip()
        when = str(evidence.get("transferred_at_utc") or "").strip()
        fields = str(evidence.get("credential_fields") or "").strip()
        if not store or not fields:
            raise ValueError("secret_scrub_requires_store_path_and_field_names")
        # Deliberately NOT built with `_note()`: the queue metadata carries long
        # opaque ids, and the writer refuses any replacement note that still
        # contains a secret-shaped token. Traceability lives in the plan, the
        # receipt and the transfer receipt - never in the cell we are cleaning.
        head = (f"Credentials stored {when} - see secrets store: {store} "
                f"(fields: {fields}). The secret values that were pasted in this cell "
                f"have been moved to the 0600 store on the host and removed from the "
                f"Sheet; they were never printed to any log, card or report.")
        return {"row": row_num, "program": program, "evidence": evidence, "cells": {
            "23": clip(head, 380), "24": checked,
        }}

    if kind == "new_approval":
        # add_approved_row creates a whole researched row (category, fit reason,
        # official URL, vertical, portal capture + digest). A detector cannot
        # invent those, and inventing them is exactly the dishonesty this whole
        # architecture exists to prevent. So: if the queuer supplied a researched
        # row payload, use it; otherwise escalate, visibly.
        payload = evidence.get("row_cells")
        if not isinstance(payload, dict) or not payload:
            # This is a WAITING state, not a failure, and the card must read like
            # one. Before 2026-08-18 the operator saw a bare
            # `new_approval_requires_researched_row_cells` next to real refusals
            # and had no way to tell "we lost an approval" from "this program is
            # queued for research". The machine-readable token stays first so
            # every existing parser and test still matches it; the sentence after
            # it is what a human reads.
            raise ValueError(
                "new_approval_requires_researched_row_cells: "
                f"{str(queue_row.get('network') or 'the network')} approved "
                f"{program or 'this program'} and the approval is safely recorded in the "
                "queue with its evidence — nothing was lost. What is missing is the "
                "researched row `add_approved_row` requires (category, fit reason, "
                "official URL, vertical, suggested GLP page), which only the research "
                "lane can produce; a detector may not invent those cells. The program "
                "will be researched and this row will apply itself on a later drain. "
                "No operator action is needed.")
        return {
            "program": program,
            "network": queue_row.get("network"),
            "provider_program_id": queue_row.get("program_id"),
            "evidence": evidence,
            "cells": {str(key): value for key, value in payload.items()},
        }

    raise ValueError(f"kind_has_no_projection:{kind}")


# ---------------------------------------------------------------------------
# Mode invocation
# ---------------------------------------------------------------------------


def run_mode(mode: str, plan: dict[str, Any], plan_path: Path, receipt_path: Path,
             apply: bool) -> dict[str, Any]:
    """Write the plan and hand it to the governed writer mode."""
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    plan_path.chmod(0o600)
    # The writer modes print their own summaries. stdout here belongs to the one
    # machine-readable drain line n8n parses, so their chatter goes to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        if mode == "add_approved_row":
            rc = writer.run_add_approved_row(plan_path, receipt_path, apply)
        elif mode == "network_paused_disposition":
            rc = writer.run_network_paused_disposition(plan_path, receipt_path, apply)
        elif mode == "junk_row_purge":
            rc = writer.run_junk_row_purge(plan_path, receipt_path, apply)
        else:
            rc = writer.run_sheet_sync_mode(mode, plan_path, receipt_path, apply)
    receipt = json.loads(receipt_path.read_text())
    return {"rc": rc, "receipt": receipt}


def plan_for(mode: str, entry: dict[str, Any], queue_row: dict[str, Any]) -> dict[str, Any]:
    if mode == "junk_row_purge":
        return {
            "schema": writer.JUNK_ROW_PURGE_PLAN_SCHEMA,
            "authority_sha256": authority_sha256(queue_row),
            "tracking_clicks": 0, "provider_writes": 0,
            "queue_update_id": str(queue_row.get("update_id") or ""),
            "rows": entry["rows"],
        }
    schema = {
        "add_approved_row": writer.ADD_APPROVED_ROW_PLAN_SCHEMA,
        "network_paused_disposition": writer.NETWORK_PAUSED_DISPOSITION_PLAN_SCHEMA,
    }.get(mode) or writer.SHEET_SYNC_MODES[mode].plan_schema
    # `fit_binding` is the drain's own readback of the proof it verified, not a
    # writer input: the mode consumes `fit_verified` only. Keeping it out of the
    # plan body means the governed writer's row shape is unchanged by GLP-285.
    body = {key: value for key, value in entry.items() if key != "fit_binding"}
    if mode not in ("add_approved_row", "network_paused_disposition"):
        body["queue_update_id"] = str(queue_row.get("update_id") or "")
    return {
        "schema": schema,
        "authority_sha256": authority_sha256(queue_row),
        "tracking_clicks": 0,
        "provider_writes": 0,
        "queue_update_id": str(queue_row.get("update_id") or ""),
        "rows": [body],
    }


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------


def create_capable_kinds() -> frozenset[str]:
    """Queue kinds whose governed mode is allowed to CREATE a Sheet row.

    Derived from the writer's own specs so the drain can never believe a mode
    may create rows that the mode itself refuses to (or vice versa).
    """
    out = set()
    for kind, mode in writer.SHEET_SYNC_KIND_MODE.items():
        spec = writer.SHEET_SYNC_MODES.get(mode)
        if spec is not None and spec.may_create:
            out.add(kind)
    return frozenset(out)


#: Backoff between re-reads of the Sheet, in seconds. Google's per-user read
#: quota is 60/minute and this drain re-reads after EVERY applied row so later
#: rows see earlier writes — a 60-row queue therefore spends the whole run near
#: the ceiling. Proven live 2026-08-19 (n8n execution 250): a burst of HTTP 429s
#: deferred 22 rows mid-batch, which is a stall dressed up as a success.
READ_RETRY_BACKOFF_S = (5, 15, 30, 60)


def read_sheet(svc, sleep=None) -> list[list[Any]]:
    """The canonical read, with the rate limit treated as weather, not an answer.

    A 429 on the re-read is not information about the queue; it is the drain
    competing with itself (and with every other reader on the box). Waiting is
    the correct response and it is CHEAPER than deferring the row, because a
    deferred row costs a whole extra run.
    """
    sleeper = sleep or time.sleep
    last: Exception | None = None
    for attempt in range(len(READ_RETRY_BACKOFF_S) + 1):
        try:
            values = svc.spreadsheets().values().get(
                spreadsheetId=writer.SHEET_ID,
                range=f"'{writer.TAB}'!{EXPECTED_RANGE}",
                valueRenderOption="UNFORMATTED_VALUE",
            ).execute().get("values", [])
            break
        except Exception as exc:  # noqa: BLE001 - re-raised below when not transient
            if not is_transient(exc) or attempt == len(READ_RETRY_BACKOFF_S):
                raise
            last = exc
            sleeper(READ_RETRY_BACKOFF_S[attempt])
    if last is not None:
        sys.stderr.write("sheet_sync_drain: recovered a transient Sheet read after "
                         "%d retr(ies): %s\n" % (attempt, str(last)[:200]))
    if not values or writer.pad(values[0]) != writer.HEADERS:
        raise RuntimeError("canonical header mismatch; refusing to drain")
    return values


#: Retry ladder for a governed mode that failed for an infrastructure reason.
#: Same reasoning as READ_RETRY_BACKOFF_S, one level up: the mode does its own
#: CAS reads, so a rate limit surfaces as the MODE failing, and deferring the row
#: costs an entire extra run to say nothing new.
MODE_RETRY_BACKOFF_S = (8, 20, 45)

#: Wall-clock budget for starting work in one drain, in seconds. The bridge kills
#: the action at 900s, so the final 300s are carrier reserve for the in-flight
#: governed mode, terminal summary and bridge return. That return lets n8n perform
#: its downstream queue acknowledgements and heartbeat. The old
#: implementation passed this deadline only to retry sleeps; ordinary rows kept
#: starting after it and execution 4301 was killed at 900s after 79 receipted
#: writes. The loop below now checkpoints before every row and defers every
#: unstarted row once this budget is spent.
DRAIN_BUDGET_S = 600


def run_mode_with_retry(mode: str, plan: dict[str, Any], plan_path: Path,
                        receipt_path: Path, apply: bool, deadline: float | None = None,
                        sleep=None) -> dict[str, Any]:
    """`run_mode`, but a rate limit is waited out instead of deferring the row."""
    sleeper = sleep or time.sleep
    for attempt in range(len(MODE_RETRY_BACKOFF_S) + 1):
        try:
            return run_mode(mode, plan, plan_path, receipt_path, apply)
        except Exception as exc:  # noqa: BLE001 - re-raised when not worth retrying
            if attempt == len(MODE_RETRY_BACKOFF_S) or not is_transient(exc):
                raise
            wait = MODE_RETRY_BACKOFF_S[attempt]
            if deadline is not None and time.monotonic() + wait > deadline:
                raise
            sleeper(wait)
    raise AssertionError("unreachable")


#: Refusals whose cause is ALWAYS upstream of this writer: the queue row cites a
#: file that has changed or vanished since the row was written. Nothing the drain
#: can do fixes one - the producer has to stop citing mutable evidence - so the
#: reason line names the producer that wrote the row.
EVIDENCE_REFUSAL_MARKERS = ("_evidence_hash_drift", "_evidence_file_missing")


def refusal_reason(exc: Exception, transient: bool, queue_row: dict[str, Any]) -> str:
    """The reason string a refused/deferred row records.

    2026-09-01: 728 queued rows refused with
    `payout_evaluation_evidence_hash_drift:<program>:payout_evaluation_path` and
    nothing else. The producer was a chain that rewrote the very file its earlier
    rows cited, but neither the drain receipt nor the Discord line said WHO wrote
    the row, so the same refusal was re-read fifteen times without being routed.
    The writer now names the path and both digests; this names the producer.
    """
    reason = (
        f"{'deferred_transient' if transient else 'refused'}:"
        f"{type(exc).__name__}:{str(exc)[:600]}"
    )
    if any(marker in str(exc) for marker in EVIDENCE_REFUSAL_MARKERS):
        reason += (
            f":producer={str(queue_row.get('queued_by') or '').strip() or 'unknown'}"
            f":queued_at={str(queue_row.get('queued_at_utc') or '').strip() or 'unknown'}"
        )
    return reason


def result_base(queue_row: dict[str, Any], index: int) -> dict[str, Any]:
    """One stable result envelope, including rows deferred before inspection."""
    return {
        "update_id": str(queue_row.get("update_id") or f"row-{index + 1}"),
        "kind": str(queue_row.get("kind") or "").strip(),
        "network": str(queue_row.get("network") or ""),
        "program_id": str(queue_row.get("program_id") or ""),
        "program_name": str(queue_row.get("program_name") or ""),
        "queued_by": str(queue_row.get("queued_by") or ""),
        "queued_at_utc": str(queue_row.get("queued_at_utc") or ""),
        "provider_slug": "",
        "stable_key": "",
        "tuple_sha256": "",
        "sheet_row": None,
        "mode": None,
        "status": RESULT_ESCALATED,
        "reason": None,
        "cells_written": 0,
        "receipt": None,
        "resolved_at_utc": utc_now(),
    }


def drain(queue_rows: list[dict[str, Any]], run_dir: Path, apply: bool,
          sleep=None) -> dict[str, Any]:
    from googleapiclient.discovery import build as _build

    require_katalys_queue_id_consistency(queue_rows)
    deadline = time.monotonic() + DRAIN_BUDGET_S
    svc = _build("sheets", "v4", credentials=writer.creds(), cache_discovery=False)
    values = read_sheet(svc, sleep=sleep)
    results: list[dict[str, Any]] = []

    for index, queue_row in enumerate(queue_rows):
        if time.monotonic() >= deadline:
            for remaining_index, remaining_row in enumerate(
                    queue_rows[index:], start=index):
                deferred = result_base(remaining_row, remaining_index)
                deferred.update({
                    "status": RESULT_DEFERRED,
                    "deferred": True,
                    "reason": (
                        f"deferred_cycle_budget:budget_s={DRAIN_BUDGET_S}:"
                        "carrier_timeout_s=900"
                    ),
                })
                results.append(deferred)
            break

        base = result_base(queue_row, index)
        update_id = base["update_id"]
        kind = base["kind"]
        mode = writer.SHEET_SYNC_KIND_MODE.get(kind)
        if not mode:
            base["reason"] = f"unknown_kind:{kind or '(blank)'}"
            results.append(base)
            continue
        base["mode"] = mode

        try:
            evidence = materialize_evidence(
                kind, queue_row, parse_evidence(queue_row), run_dir)
        except ValueError as exc:
            base["status"] = RESULT_REFUSED
            base["reason"] = str(exc)
            results.append(base)
            continue
        if kind == "deployed_live":
            base["provider_slug"] = str(evidence.get("provider_slug") or "")
            base["stable_key"] = str(evidence.get("stable_key") or "")
            base["tuple_sha256"] = str(evidence.get("tuple_sha256") or "")

        row_num: int | None = None
        row_values: list[Any] | None = None
        if kind not in {"new_approval", "junk_row_purge"}:
            row_num, reason = resolve_sheet_row(values, queue_row)
            if row_num is None:
                # A submission to a provider that is not on the Sheet yet is a
                # REAL application, not a malformed queue row. The same is true
                # of a rejection that arrived before the program was ever filed.
                # So for kinds whose MODE declares `may_create` - read off the
                # writer, never hardcoded here, so the two can never drift - a
                # clean identity miss falls through to the create path, where
                # `build_entry` still demands researched cells + real evidence
                # before anything reaches the Sheet. Every other kind, and every
                # ambiguous match, refuses exactly as before — an ambiguous
                # identity must never mint a duplicate row.
                if not (kind in create_capable_kinds()
                        and reason == "no_sheet_row_matches_program_identity"):
                    base["status"] = RESULT_REFUSED
                    base["reason"] = reason
                    results.append(base)
                    continue
                base["creates_row"] = True
                # Every identity axis is scoped BY NETWORK, so a row filed under a
                # variant network label ("Direct/contact verify" vs "Direct") does
                # not match and a second row gets minted. Proven live 2026-08-18:
                # Calibrate created row 85 while row 20 already named it. Auto-
                # merging on name alone would be worse — a program legitimately
                # listed on two networks would have the WRONG row overwritten — so
                # the drain reports the collision instead of resolving it, and the
                # weekly `glp/sheet-reconciler` owns the merge decision.
                base["name_collisions"] = name_collisions(values, queue_row)
            else:
                base["sheet_row"] = row_num
                row_values = writer.pad(values[row_num - 1])

        # Before demanding the researched cells a row creation needs, answer the
        # prior question: is this fact new at all? A create path whose programme
        # the same rail already carries is a re-report, and the only two things
        # that can happen to it downstream are a wasted refusal or a duplicate
        # row. Neither is worth a permanent escalation.
        if (kind == "new_approval" or base.get("creates_row")) and kind != "research_only_provider":
            already = create_supersession_reason(values, queue_row)
            if already:
                base["status"] = RESULT_NOOP
                base["reason"] = already
                base["creates_row"] = False
                results.append(base)
                continue
            cross_rows = cross_marketplace_existing_rows(values, queue_row)
            if cross_rows:
                base["cross_marketplace_existing_rows"] = cross_rows

        # A stale pause notice must never undo a re-activation the Sheet already
        # records. The drain re-reads the Sheet after every applied write, so this
        # catches the in-batch ordering case (pause queued after reactivation) and
        # the cross-batch case (reactivation applied by an earlier run) alike.
        superseded = lifecycle_supersession_reason(kind, evidence, row_values)
        if superseded:
            base["status"] = RESULT_REFUSED
            base["reason"] = superseded
            results.append(base)
            continue

        try:
            entry = build_entry(kind, queue_row, evidence, row_num, row_values)
        except (ValueError, RuntimeError) as exc:
            base["status"] = RESULT_REFUSED if kind in {*writer.provider_metadata.KINDS, "partner_email_hold", "research_only_provider"} else RESULT_ESCALATED
            base["reason"] = str(exc)
            if kind == "new_approval" and str(exc).startswith(
                    "new_approval_requires_researched_row_cells"):
                base["research_request"] = route_new_approval_research(
                    run_dir, queue_row, evidence,
                    base.get("cross_marketplace_existing_rows") or [])
            results.append(base)
            continue

        if entry.get("fit_verified") and entry.get("fit_binding"):
            base["fit_verified"] = True
            base["fit_assessment"] = entry["fit_binding"]
        plan = plan_for(mode, entry, queue_row)
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in update_id)[:60]
        plan_path = run_dir / f"{index + 1:02d}-{slug}-plan.json"
        receipt_path = run_dir / f"{index + 1:02d}-{slug}-receipt.json"
        try:
            outcome = run_mode_with_retry(mode, plan, plan_path, receipt_path, apply,
                                          deadline=deadline, sleep=sleep)
        except Exception as exc:  # noqa: BLE001 - a refusal is the mode's answer
            transient = is_transient(exc)
            base["status"] = RESULT_DEFERRED if transient else RESULT_REFUSED
            base["deferred"] = transient
            base["reason"] = refusal_reason(exc, transient, queue_row)
            base["receipt"] = repo_relative(plan_path)
            results.append(base)
            continue

        receipt = outcome["receipt"]
        written = int(receipt.get("value_cells_written") or 0)
        base["cells_written"] = written
        base["receipt"] = repo_relative(receipt_path)
        # A created row's number is only knowable after the writer allocates it.
        # Without this the readback can name the row of every UPDATE and of no
        # CREATE - which is exactly the half a "which row did the previously
        # refused application land on?" question needs.
        if base["sheet_row"] is None:
            allocated = [int(r) for r in (receipt.get("created_rows") or [])]
            if len(allocated) == 1:
                base["sheet_row"] = allocated[0]
        if outcome["rc"] != 0 or receipt.get("status") != "PASS":
            base["status"] = RESULT_REFUSED
            base["reason"] = (
                f"writer_receipt_{receipt.get('status')}:"
                f"mismatches={len(receipt.get('readback_mismatches') or [])} "
                f"collateral={len(receipt.get('collateral_changes') or [])} "
                f"idempotent={receipt.get('idempotent_rerun')}"
            )
        elif not apply:
            base["status"] = RESULT_NOOP
            base["reason"] = "dry_run"
            base["cells_written"] = 0
            base["planned_cells"] = int(receipt.get("value_cells_planned") or 0)
        elif written == 0:
            base["status"] = RESULT_NOOP
            base["reason"] = "already_applied"
        else:
            base["status"] = RESULT_APPLIED
        if apply and base["status"] in (RESULT_APPLIED, RESULT_NOOP):
            values = read_sheet(svc, sleep=sleep)  # later rows must see earlier writes
            # Receipt capture remains available while the candidate/site fan-out
            # is OFF. A terminal Sheet update can therefore be replayed without
            # another Sheet write. Fan-out failure never changes Sheet truth.
            import affiliate_terms_fanout as terms_fanout
            if kind in terms_fanout.KINDS:
                try:
                    event_path = run_dir / f"{index + 1:02d}-{slug}-fanout.json"
                    base["terms_fanout_event"] = terms_fanout.capture_readback(
                        queue_row, base, plan_path, receipt_path, values, event_path)
                    if os.environ.get("GLP_AFFILIATE_TERMS_FANOUT_ENABLE") == "1":
                        base["terms_fanout"] = terms_fanout.consume(
                            event_path,
                            Path(os.environ["GLP_AFFILIATE_TERMS_REGISTRY"]),
                            Path(os.environ["GLP_AFFILIATE_TERMS_IDENTITY_MAP"]),
                            Path(os.environ["GLP_AFFILIATE_TERMS_STATE_DIR"]),
                            enabled=True, apply=True)
                    else:
                        base["terms_fanout"] = {"status": "DISABLED"}
                except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
                    base["terms_fanout"] = {"status": "REFUSED", "reason": str(exc)}
        results.append(base)

    counts = {status: sum(1 for r in results if r["status"] == status)
              for status in (RESULT_APPLIED, RESULT_NOOP, RESULT_REFUSED, RESULT_ESCALATED)}
    return {
        "schema": SCHEMA,
        "status": "PASS",
        "captured_at_utc": utc_now(),
        "apply": apply,
        "queue_rows": len(queue_rows),
        "applied": counts[RESULT_APPLIED],
        "noop": counts[RESULT_NOOP],
        "refused": counts[RESULT_REFUSED],
        "escalated": counts[RESULT_ESCALATED],
        "deferred": sum(1 for r in results if r.get("deferred")),
        "cells_written": sum(r["cells_written"] for r in results),
        "results": results,
    }


def lock_contended_summary(queue_rows: list[dict[str, Any]], apply: bool) -> dict[str, Any]:
    """The receipt for a run that stood down because another drain held the lock.

    Deliberately shaped like every other summary so the same n8n parser reads
    it, but with `status=DEFERRED_LOCK_BUSY` and `lock_contended=true` so the
    caller can tell a benign stand-down from a real failure. `results` is empty:
    nothing was attempted, so nothing is acknowledged and every queue row stays
    `queued`.
    """
    return {
        "schema": SCHEMA,
        "status": STATUS_LOCK_CONTENDED,
        "lock_contended": True,
        "captured_at_utc": utc_now(),
        "apply": apply,
        "queue_rows": len(queue_rows),
        "applied": 0,
        "noop": 0,
        "refused": 0,
        "escalated": 0,
        "deferred": len(queue_rows),
        "cells_written": 0,
        "results": [],
        "note": LOCK_CONTENDED_NOTE,
    }


def summary_line(summary: dict[str, Any], receipt_rel: str) -> dict[str, Any]:
    """The compact line n8n parses out of stdout (see the liveness lane)."""
    return {
        "schema": SCHEMA,
        "status": summary["status"],
        "lock_contended": bool(summary.get("lock_contended", False)),
        "note": summary.get("note"),
        "apply": summary["apply"],
        "queue_rows": summary["queue_rows"],
        "applied": summary["applied"],
        "noop": summary["noop"],
        "refused": summary["refused"],
        "escalated": summary["escalated"],
        "deferred": summary.get("deferred", 0),
        "cells_written": summary["cells_written"],
        "receipt": receipt_rel,
        "results": [
            {key: value for key, value in row.items()
             if key in ("update_id", "kind", "sheet_row", "program_name", "status",
                        "provider_slug", "stable_key", "tuple_sha256", "reason",
                        "cells_written", "deferred", "queued_by", "queued_at_utc")}
            for row in summary["results"]
        ],
    }


def load_queue(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.queue_json:
        payload = json.loads(args.queue_json)
    elif args.queue_file:
        payload = json.loads(Path(args.queue_file).read_text())
    else:
        payload = []
    if isinstance(payload, dict):
        payload = payload.get("rows") or payload.get("queue") or []
    if not isinstance(payload, list):
        raise RuntimeError("queue payload must be a JSON array of queue rows")
    rows = [row for row in payload if isinstance(row, dict)]
    # Only queued rows are the writer's business; anything already resolved is
    # left exactly as it is.
    rows = [row for row in rows
            if str(row.get("status") or "queued").strip().lower() in ("", "queued")]
    return dedupe_by_update_id(rows)


def require_katalys_queue_id_consistency(rows: list[dict[str, Any]]) -> None:
    """Refuse conflicting copies before dedupe or any transport is created."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get("update_id") or ""), []).append(row)
    for update_id, copies in groups.items():
        guarded = next((row.get("kind") for row in copies
                        if row.get("kind") in {"katalys_tracking_ready", "partner_email_hold"}), None)
        if guarded is None:
            continue
        if not update_id or update_id != update_id.strip():
            raise RuntimeError(f"{guarded}_update_id_missing_or_invalid")
        if len(copies) == 1:
            continue  # per-row evidence failures remain the drain's row result
        payloads = set()
        for row in copies:
            payload = {key: row.get(key) for key in
                       ("kind", "network", "program_id", "program_name", "sheet_row")}
            payload["evidence"] = parse_evidence(row)
            payloads.add(canonical(payload))
        if len(payloads) != 1:
            raise RuntimeError(f"{guarded}_duplicate_update_id_conflict:{update_id}")


def dedupe_by_update_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One `update_id` is one fact, however many datatable rows carry it.

    The queue accumulates duplicate `update_id`s because detectors re-run and
    `insertRows` does not upsert; and the workflow's acknowledgement node filters
    by `update_id`, so every copy gets the same status anyway. Draining the copies
    is therefore pure cost — and it is expensive cost, because each row triggers a
    governed mode's CAS reads against a 60/minute Sheets quota. Proven 2026-08-19
    (n8n execution 261): 42 queue rows, 10 of them second copies, and the run
    spent its quota re-deciding facts it had already decided in the same batch.
    """
    require_katalys_queue_id_consistency(rows)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        update_id = str(row.get("update_id") or "").strip()
        if update_id and update_id in seen:
            continue
        if update_id:
            seen.add(update_id)
        out.append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queue-json", help="JSON array of sheet_update_queue rows.")
    ap.add_argument("--queue-file", help="Path to a JSON array of sheet_update_queue rows.")
    ap.add_argument("--output", help="Run summary path (default: dated file under sheet-sync/).")
    ap.add_argument("--apply", default="false",
                    help="'true' performs the governed Sheet writes; anything else is a dry run.")
    args = ap.parse_args()
    apply = str(args.apply).strip().lower() in ("1", "true", "yes", "apply")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RECEIPT_ROOT / f"drain-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    out = Path(args.output) if args.output else run_dir / "run-summary.json"

    queue_rows = load_queue(args)
    rc = 0
    if not queue_rows:
        summary = {
            "schema": SCHEMA, "status": "PASS", "captured_at_utc": utc_now(), "apply": apply,
            "queue_rows": 0, "applied": 0, "noop": 0, "refused": 0, "escalated": 0,
            "deferred": 0, "cells_written": 0, "results": [],
            "note": "queue empty; no sheet write attempted",
        }
    else:
        lock_path = writer.LOCK
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # CONTROLLED stand-down, not a crash. The other writer keeps the
            # lock; we acknowledge nothing, so every queue row is still
            # `queued` when the next drain runs. See RC_LOCK_CONTENDED.
            handle.close()
            summary = lock_contended_summary(queue_rows, apply)
            rc = RC_LOCK_CONTENDED
        else:
            try:
                summary = drain(queue_rows, run_dir, apply)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    out.chmod(0o600)
    receipt_rel = repo_relative(out)
    print(json.dumps(summary_line(summary, receipt_rel), separators=(",", ":")))
    if rc:
        sys.stderr.write(
            "sheet_sync_drain: %s (rc=%d) - %d queue row(s) left untouched\n"
            % (STATUS_LOCK_CONTENDED, rc, len(queue_rows))
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
