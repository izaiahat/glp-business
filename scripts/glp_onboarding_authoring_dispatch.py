#!/usr/bin/env python3
"""AUTHORING DISPATCH — the onboarding machine dispatches its own authoring
engine, instead of naming one and waiting for a human to launch it.

OPERATOR, 2026-08-20: *"NO HOT FIXES EVER!!! the machine has to work E2E that's
the only way the system can be reliable."*

THE DEFECT THIS CLOSES
----------------------
`glp/onboarding-manifest` assessed nu-image-medical correctly — 4/24 green — and
its receipt said, in as many words, *"authoring routes to engine `claude`"*.
Nothing on this box read that sentence. A bridge seat read it and launched an
authoring agent by hand. That hand-launch is the hot fix: the machine produced a
correct verdict and then stopped, and a provider only moved because a human was
watching at 2am. A machine that needs a human between two of its own steps is
not a machine.

So the routing decision the manifest already makes becomes a DISPATCH the
manifest itself performs, on exactly the infrastructure that was proven tonight
for the tier-2 fixer ladder:

    onboard cycle ends with authoring-class blocks
        │
        ▼
    ONE bridge `hermes_chat` job on the resolved primary model at xhigh
    (45-minute hard budget), carrying: the provider
    slug + governed Sheet row, the EXACT blocked steps off this machine's own
    receipt, the capture step, the artifact contract, and the guardrail contract
        │  async — job id recorded, the workflow resumes on the bridge callback
        ▼
    authoring done → the machine RE-RUNS its own assessment
        │  clear?  -> build -> deploy -> sensor -> deployed_live queue row + HB
        │  not clear? -> next cycle, max 3 per provider per PT day
        ▼
    cap reached -> ops/error-handler (`error_cases`), where the tier-2 ladder
    catches it, plus an honest red card. Never a silent stop.

THE FIT GATE STAYS THE MACHINE'S
--------------------------------
The authoring engine gathers evidence; it does not get to decide whether a
provider ships. If its evidence lands `not_fit`, this module records the verdict
as a PERSISTENT stop for that provider and no further cycle is dispatched — a
correct refusal, with nothing deployed. An engine that returns `fit` still only
un-blocks the manifest's own `fit_legality_gate` step, which re-verifies against
the registry and the Sheet exactly as before. No gate is weakened anywhere.

CLEAN-ROOM ENFORCEMENT
----------------------
A capable agent handed "produce a research packet for nu-image-medical" and a
disk that already contains a hand-authored packet from a killed manual run will
reuse it, and the E2E proof becomes a proof of nothing. So a dispatch snapshots
the sha256 of every pre-existing artifact that mentions the provider
(`prior_fingerprints`), and the resume path REFUSES a packet whose capture
hashes are drawn from that set. First-party captures made by THIS job, or the
cycle does not count.

  BLOCKER B, fixed 2026-08-20 — the guard used to be SELF-POISONING. Its
  fingerprint root contained every previous cycle's capture directory, so a
  genuine fresh firecrawl of an UNCHANGED page hashed into its own history and
  every honest cycle failed `reused_prior_artifacts`. Three cycles were recorded
  `failed / evidence_gap` in one day for doing the job correctly.

  The fix is a DISCRIMINATION, not a weakening, in three parts:
    1. the capture step writes a PER-CYCLE FETCH RECEIPT for every URL it
       fetches (cycle id, timestamp, route, HTTP status, a hash of the request,
       and a hash of response evidence the network returned). A capture with a
       valid this-cycle receipt is legitimate REGARDLESS of matching historical
       bytes — an unchanged page is the expected steady state.
    2. `prior_fingerprints` skips the MACHINE's own prior authoring output
       (`is_machine_authoring_path`). The quarantine tree, hand-run directories
       and every other non-machine path stay fingerprinted.
    3. a capture that matches history WITHOUT a valid this-cycle receipt still
       FAILS — that is the actual reuse case, and it is what a hand-planted
       artifact will always be.

I/O is injectable (`bridge_post`, `job_status`, `assess`) so the whole lane is
unit-testable without a bridge, a model, or n8n.
"""
from __future__ import annotations

import datetime as dt
import argparse
import fcntl
import signal
import subprocess
from contextlib import contextmanager
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import sys
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

try:                                    # normal: every caller puts scripts/ on the path
    import glp_capture_fetch_receipts as fetch_receipts
except ImportError:                     # standalone import of this file
    _SCRIPTS = str(Path(__file__).resolve().parent)
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    import glp_capture_fetch_receipts as fetch_receipts  # noqa: E402

BUSINESS = Path(os.environ.get("GLP_AUTHORING_REPO", "/home/ubuntu/business"))
HOME = Path(os.path.expanduser("~"))
PT = ZoneInfo("America/Los_Angeles")

SCHEMA = "glp.onboarding_authoring_dispatch.v1"
RECEIPT_SCHEMA = "glp.onboarding_authoring_receipt.v1"
MACHINE = "onboarding_manifest"

ROOT = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/onboarding-manifest"
STATE_PATH = Path(os.environ.get("GLP_AUTHORING_STATE",
                                 str(ROOT / "authoring-dispatches.json")))
RECEIPT_DIR = Path(os.environ.get("GLP_AUTHORING_RECEIPT_DIR",
                                  str(ROOT / "authoring")))


def receipt_dir() -> Path:
    """Where cycle receipts live, read at CALL time, not at import time.

    Import-time capture meant a test could not redirect it, so the suite wrote
    its fixtures into the PRODUCTION receipt directory (that is how an empty
    `authoring/` came to exist at 04:42 while the real cycles at 04:06-04:10 had
    written nothing). Reading the env per call keeps test bytes out of the
    machine's evidence directory."""
    return Path(os.environ.get("GLP_AUTHORING_RECEIPT_DIR", str(RECEIPT_DIR)))

BRIDGE_BASE = os.environ.get("GLP_AUTHORING_BRIDGE_BASE", "https://bridge.allocant.ai")
BRIDGE_TOKEN_FILE = HOME / ".hermes/secrets/n8n-bridge/token"

#: 45 minutes, the operator's number and the same one the tier-2 lane runs. The
#: bridge caps the managed job independently, and the deadline is enforced on
#: our side too — a bridge that loses
#: a job can never leave a provider mid-cycle forever.
BUDGET_S = int(os.environ.get("GLP_AUTHORING_BUDGET_S", "2700"))
# Managed turns for a Sol authoring cycle.
#
# DEFECT 2026-08-20 #6 — the receipts, not a preference:
#   * 04:06-04:10 (wellmedr): the bridge defaulted `max_turns` to 1. Three
#     cycles, three `Reached maximum iterations (1)`, 39-52s each. Fixed
#     upstream that morning.
#   * 06:52-07:32 (nu-image, WITH `max_turns: 12` honoured): every Sol cycle
#     still ended `Reached maximum iterations (12)` — c1 305s, c1' 471s, c3
#     426s. None was near the 2700s budget. Turns were the binding constraint,
#     not the clock, and 12 was simply not enough to read, capture 12 pages,
#     quote-check and write.
#   * The two `claude_p` opus cycles between them completed CLEAN in 377s and
#     677s — under a contract with NO turn cap at all, only the wall clock.
#
# So the ceiling goes to the bridge's own maximum. It is the highest defensible
# number: `bridge.py` rejects anything over 20 (`max_turns must be 1..20`), and
# a payload the bridge rejects is a dispatch that never happens. The 45-minute
# budget stays authoritative — the runs above show it binds long before turns
# would, which is exactly the shape the opus contract already has.
MAX_TURNS = int(os.environ.get("GLP_AUTHORING_MAX_TURNS", "20"))
#: The bridge's hard ceiling. Pinned here so a raise past it fails a test rather
#: than every live dispatch. 60 since 2026-08-20 ~21:50Z: `bridge.py` allows 60
#: (operator routing order — Sol carries research with a heavy turn budget
#: instead of an opus contract) and `n8n-bridge.service` was restarted onto
#: that code at a verified zero-child moment, so the running process and this
#: pin agree.
BRIDGE_MAX_TURNS = int(os.environ.get("GLP_BRIDGE_MAX_TURNS", "60"))
#: Max authoring cycles per provider per PT day. Cycle 4 is not a longer try, it
#: is evidence that authoring is not the blocker — that goes to the ladder.
MAX_CYCLES_PER_DAY = int(os.environ.get("GLP_AUTHORING_MAX_CYCLES", "3"))
#: The bridge carries three concurrent jobs and the manifest run is one of them.
MAX_IN_FLIGHT = int(os.environ.get("GLP_AUTHORING_MAX_IN_FLIGHT", "1"))
# ENGINE POLICY — bridge decision recorded 2026-08-20 in
# AUTHORING-DISPATCH-DEFECTS-20260820.md #3, under the operator's heavy-Sol
# directive:
#
#   * Sol xhigh (`hermes_chat`) is the PRIMARY authoring engine. Not a fallback,
#     not a cost compromise — the primary.
#   * Whatever ran is recorded HONESTLY in the ledger record AND in the cycle's
#     authoring receipt. The lane never says `opus` and run Sol, or the reverse.
#   * A Sol cycle that fails for CAPABILITY reasons (it could not do the work:
#     turn ceiling, crash, no receipt, lost budget) is recorded as exactly that.
#     There is no bigger AUTHORING contract behind it any more — see the
#     operator routing order below — so a repeat is owned by the daily cap and
#     the error ladder, never by a silent model swap. A Sol cycle that fails
#     for EVIDENCE reasons (the provider publishes no price; the page is gone)
#     is not a capability problem at all — a bigger budget cannot invent
#     evidence, and pretending it can is how invented facts get onto a money
#     page.
# ENGINE POLICY, FOURTH ITERATION — operator conserve ruling, 2026-09-03.
#
# OpenAI credits drained to 1,280 of the 5,000 bought that day, and from
# ~08:00Z EVERY authoring dispatch died the same way:
#
#     API call failed after 3 retries: HTTP 429: The usage limit has been reached
#         measured-review c1 08:08:36Z, c2 08:20:01Z  → failure_class=capability
#         omzo-quiz-v2 c3 08:16:25Z (receipt stdout_tail, verbatim above)
#
# That is a CAPABILITY failure by this file's own definition ("rate limit" is in
# CAPABILITY_MARKERS) — the run learned nothing about the provider — and because
# research packets gate every affiliate wire, a fleet-wide 429 blocks revenue.
#
# So the MODEL is now config-driven, exactly the way ops/error-fixer's engine is:
#
#     GLP_AUTHORING_MODEL (env)  >  ops/control/authoring-engine.json  >  Sol
#
# Under the conserve ruling that file names `gpt-5.6-luna`; the prior value is
# recorded in ops/control/openai-routing-manifest.json and
# `scripts/openai_routing_switch.py --apply sol-restore` puts Sol back after the
# OpenAI reset. What did NOT change:
#
#   * The ENGINE stays `hermes_chat`. Luna is a different model on the same
#     transport, not a different lane, and not `claude_p`. The routing order
#     below still holds: opus never authors.
#   * Reasoning stays `xhigh`. Judged deliberately, not copied: an authoring
#     cycle is a BOUNDED run (12-turn observed ceiling historically, 60 turns
#     now, under a 45-minute wall budget), and `max` is the documented 40+
#     minute single-turn trap. On a bounded multi-tool run, one max turn can eat
#     the whole wall budget and produce the exact `reached maximum iterations`
#     capability failure this policy was written to stop. xhigh is the ceiling
#     here for the same reason it is the ceiling for the fixer engine.
#   * Whatever actually ran is still recorded HONESTLY in the ledger record and
#     the authoring receipt. A config-driven model must never make the receipt
#     say one thing and the dispatch do another — `primary_engine()` is the one
#     function every consumer reads, so they cannot disagree.

#: Models this lane will accept from config. A control file is operator-written,
#: but it feeds a bridge payload, so it is validated rather than trusted: a typo
#: must fall back to the pinned default, not dispatch a garbage model id (or
#: worse, a shell-shaped string) to every provider in the queue.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ALLOWED_REASONING = frozenset({"low", "medium", "high", "xhigh"})
#: The pinned fallback. Config can move the lane off this; nothing can make the
#: lane run with no model at all.
DEFAULT_ENGINE = "hermes_chat"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING = "xhigh"
AUTHORING_ENGINE_CONTROL = os.environ.get(
    "GLP_AUTHORING_ENGINE_CONTROL",
    "/home/ubuntu/business/ops/control/authoring-engine.json")


def authoring_engine_control(path: str = None) -> dict:
    """The routing control file's contents, or {} when it says nothing usable.

    Fail-open by design: an unreadable, malformed, or invalid control file must
    leave the lane on its pinned default, never stop authoring.
    """
    try:
        with open(path or AUTHORING_ENGINE_CONTROL, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_engine_field(env_var: str, key: str, default: str,
                          validator, control: dict = None) -> str:
    """env var > control file > pinned default, each validated the same way."""
    control = authoring_engine_control() if control is None else control
    for candidate in (os.environ.get(env_var), control.get(key)):
        # A non-str from a hand-written JSON file (a number, a list, a dict) is
        # a typo, not a model id. str()-ing it would smuggle "5" or "{'a': 1}"
        # into a bridge payload sent to every provider in the queue.
        if not isinstance(candidate, str):
            continue
        value = candidate.strip()
        if value and validator(value):
            return value
    return default


def resolve_authoring_engine(control: dict = None) -> tuple:
    """`(engine, model, reasoning)` for this process, resolved once at import.

    Resolved at import rather than per dispatch so that one long-running
    dispatcher process cannot change models halfway through its own queue and
    write receipts that disagree with each other.
    """
    control = authoring_engine_control() if control is None else control
    engine = _resolve_engine_field(
        "GLP_AUTHORING_ENGINE", "engine", DEFAULT_ENGINE,
        lambda v: v in {"hermes_chat", "claude_p"}, control)
    model = _resolve_engine_field(
        "GLP_AUTHORING_MODEL", "model", DEFAULT_MODEL,
        lambda v: bool(_MODEL_ID_RE.match(v)), control)
    reasoning = _resolve_engine_field(
        "GLP_AUTHORING_REASONING", "reasoning", DEFAULT_REASONING,
        lambda v: v in _ALLOWED_REASONING, control)
    return engine, model, reasoning


PRIMARY_ENGINE, PRIMARY_MODEL, PRIMARY_REASONING = resolve_authoring_engine()
#: Back-compat aliases: the primary engine's model/reasoning, unchanged.
MODEL = PRIMARY_MODEL
REASONING = PRIMARY_REASONING
TOOLSET_PROFILE = "authoring"

# ENGINE POLICY, THIRD ITERATION — operator routing order, 2026-08-20 ~2:30pm PT.
#
# "For any recurring jobs that need research or stuff like that, we should use
# Sol. Opus should only be the independent reviewer... OpenAI has more liberal
# limits weekly so we want to conserve [Fable] and Opus for orchestration and
# independent reviews." Sol is therefore the ONLY authoring engine, for every
# class. Opus never authors; it reviews finished runs.
#
# The second iteration (2026-08-20 defect #6) had routed research-packet-class
# work to `claude_p` opus as primary, on these receipts (nu-image, alternating
# cycles, same provider, same blocked steps):
#     c1  06:52Z  hermes_chat/gpt-5.6-sol  305s  → Reached maximum iterations (12)
#     c2  06:58Z  claude_p/opus            377s  → AUTHORED, prices corrected
#     c1' 07:05Z  hermes_chat/gpt-5.6-sol  472s  → Reached maximum iterations (12)
#     c2' 07:13Z  claude_p/opus            677s  → AUTHORED, fresh capture verified
#     c3  07:25Z  hermes_chat/gpt-5.6-sol  427s  → Reached maximum iterations (12)
#
# Re-read under the routing order, those receipts show a BUDGET asymmetry, not
# a model verdict: every Sol cycle died at a 12-TURN ceiling while the opus
# contract ran with NO turn cap at all — only the wall clock. Sol was never
# given the budget the job needs. So the class distinction survives, but what
# it selects changed:
#
#   * A cycle whose authoring blocks include a RESEARCH-PACKET-class step runs
#     Sol xhigh with the HEAVY turn budget (`RESEARCH_MAX_TURNS`, clamped to
#     the bridge's own ceiling). The 45-minute wall budget stays authoritative.
#   * Every lighter authoring class (registry rows, pricing-matrix rows, the
#     taxonomy, the fit gate, surface declarations) keeps Sol xhigh with the
#     standard `MAX_TURNS` budget.
#   * A capability failure no longer buys the next cycle a different model —
#     it is recorded honestly, and the daily cap plus the error ladder own the
#     repeat (which is where a human or the fixer decides what changes).
#
# `research_packet` is the heavy class because the packet is where the capture
# and the evidence live: the other steps mostly transcribe what the packet
# already proved. `fit_legality_gate` rides with it — it is adjudicated off the
# same capture in the same cycle, and splitting them would put the fit verdict
# on a smaller budget than the evidence it reads.
# 2026-08-20, second addition: `terms_classification` and `program_authority`
# ride with the research packet, and for the same reason. The terms document is
# an adjudication of the RAIL's own captured contract text, and the program
# authority is composed out of the packet's already-double-verified evidence —
# both are quote-checked, hash-bound, legality-adjacent work on the same
# capture set. Splitting them onto a lighter budget would put a legal read and
# the review copy that ships on a smaller contract than the evidence they cite.
RESEARCH_PACKET_CLASS = frozenset({
    "research_packet", "fit_legality_gate", "terms_classification",
    "program_authority"})
#: The heavy class's turn budget. 60 is deliberate headroom over the observed
#: need (opus finished the same job in well under the wall budget with no turn
#: cap); the 45-minute wall budget stays the authoritative bound. Clamped to
#: `BRIDGE_MAX_TURNS` at dispatch, so raising this alone can never produce a
#: payload the bridge rejects.
RESEARCH_MAX_TURNS = int(os.environ.get("GLP_AUTHORING_RESEARCH_MAX_TURNS", "60"))
#: Names for the two policies, recorded verbatim in the ledger, the receipt and
#: the status view so "which budget, and why" is never inferred from a model id.
POLICY_RESEARCH_PACKET = "research_packet_class_sol_heavy_turns"
POLICY_SOL_PRIMARY = "sol_primary_no_model_escalation"

# ---------------------------------------------------------------------------
# HTTP bounds for the bridge — defect 2026-08-20 #5
# ---------------------------------------------------------------------------
#: Every bridge call is bounded three ways, for the same reason `ops/n8n_dt` is:
#: a socket timeout answers "is this socket idle?", never "has this call taken
#: too long?" Exec 330 proved the difference costs hours.
HTTP_CONNECT_TIMEOUT_S = 15
HTTP_READ_TIMEOUT_S = 45          # per socket operation, and ≤60s by contract
HTTP_DEADLINE_S = 60              # WALL CLOCK for one attempt, body included
HTTP_MAX_ATTEMPTS = 3
HTTP_BACKOFF_S = (2.0, 5.0)
HTTP_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
HTTP_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

#: DISPATCH-ACK bound (2026-08-20): the bridge's sync window is 25s, but under
#: load (concurrent finder/fixer/manifest jobs) the ack for a claude_p spawn has
#: been observed to exceed the strict 45s read bound — killing a legitimate
#: dispatch whose retry is forbidden (attempts=1 by design). The ack gets a
#: roomier, still-FINITE bound. Everything else keeps the strict bounds.
HTTP_DISPATCH_ACK_TIMEOUT_S = 180

#: Why a cycle failed. Only `capability` buys the next cycle a bigger contract.
CAPABILITY, EVIDENCE_GAP, NO_FAILURE = "capability", "evidence_gap", "none"

#: Substrings that mean "the engine could not do the work", checked against the
#: bridge job's own tail and the engine's notes. Every one of these was either
#: OBSERVED on this lane or is a hard runtime failure:
#:   * "reached maximum iterations" — OBSERVED 2026-08-20 on all three wellmedr
#:     cycles: the bridge defaulted `max_turns` to 1, so Sol burned three cycles
#:     writing a checklist and never got a second tool call.
#:   * turn/tool ceilings and context limits — the same class, different wording.
#:   * timeouts, kills and rate limits — the run did not finish, so nothing it
#:     did or did not find is evidence about the provider.
CAPABILITY_MARKERS = (
    "reached maximum iterations",
    "maximum iterations",
    "max turns",
    "max_turns",
    "tool-call limit",
    "tool call limit",
    "tool-call ceiling",
    "tool call ceiling",
    "iteration cap",
    "context window",
    "context length",
    "rate limit",
    "429 too many requests",
    #: OBSERVED 2026-09-03 08:08-08:20Z, fleet-wide, verbatim from the bridge
    #: job's own stdout_tail: "API call failed after 3 retries: HTTP 429: The
    #: usage limit has been reached". The list above matched none of it -- the
    #: cycles were only caught as capability failures by the no-receipt path,
    #: which names the symptom instead of the cause.
    "usage limit has been reached",
    "http 429",
    "timed out",
    "timeout",
    "killed",
    "out of memory",
)
ENABLED = os.environ.get("GLP_AUTHORING_ENABLED", "1").lower() not in (
    "0", "false", "no", "off")

#: Cloudflare fronts bridge.allocant.ai and answers 403 (`error code: 1010`) to
#: the default `Python-urllib/3.x` User-Agent — PROVEN by the tier-2 kill test
#: 2026-08-19. Without this header every dispatch dies at the POST and the whole
#: lane degrades silently to "named the block and stopped", i.e. back to the hot
#: fix. Never remove it.
BRIDGE_UA = os.environ.get("GLP_AUTHORING_HTTP_UA",
                           "tilton-onboarding-authoring/1.0 (+https://bridge.allocant.ai)")

BRIEFING_DOC = "reports/ops/N8N-BUILDER-BRIEFING.md"
CHARTER_DOC = "ops/error-fixer/FIXER-CHARTER.md"
CAPTURE_STEP = "scripts/g6_r1a_stage5_research_fetch.py"
SURFACE_WRITERS = "scripts/glp_provider_surface_writers.py"
RESEARCH_REL = "reports/glp-clinic-scout/affiliate-machine/wire-backlog-worker/program-research"
RESEARCH_SCHEMA = "program-deep-research-v1"

# ---------------------------------------------------------------------------
# The two artifact classes added to this contract on 2026-08-20
# ---------------------------------------------------------------------------
#: 1. THE RAIL'S TERMS DOCUMENT. `glp_affiliate_wire_backlog_daily.run_day`
#: refuses to prepare a row without it (`queue_terms_binding_missing`), and
#: `wire_worker._terms_valid` is what judges it.
TERMS_REL = "reports/glp-affiliate-auto-reconciler"
TERMS_SCHEMA = "affiliate_approval_classification_v1"
TERMS_RAW_REL = "reports/glp-affiliate-auto-reconciler/private-provider-terms"
#: The MACHINE's own writer for that document. The engine supplies captured
#: evidence; this module's deterministic classifier + legality screen fill the
#: cells. An engine hand-writing the JSON is an engine deciding a legal question.
TERMS_WRITER = "scripts/affiliate_approval_policy.py"
#: 2. THE PROGRAM AUTHORITY. The wire-in builder is driven by this file, not by
#: the authoring lane's candidate tree.
AUTHORITY_REL = ("reports/glp-clinic-scout/affiliate-machine/wire-backlog-worker"
                 "/program-authorities")
AUTHORITY_SCHEMA = "b2-wire-backlog-program-spec-v1"
AUTHORITY_LEGACY_SCHEMA = "g6-r1a-wirein-program-specs-v1"

#: SCHEMA PARITY, stated as key sets rather than as "go and look".
#:
#: Every one of these is the INTERSECTION of the keys carried by every real
#: artifact of that class already on disk (36 live classification documents, 8
#: live program authorities, 2026-08-20) — not a wish list. A test re-derives
#: them from those same artifacts, so the brief can never ask an engine for a
#: shape the pipeline does not actually use, and a future artifact that drops a
#: key breaks a test instead of a deploy.
TERMS_DOC_KEYS: tuple = (
    "advertiser_id", "affiliate_redirect_clicked", "authoritative_relationship",
    "classification", "destination_url", "event_id", "network", "program_id",
    "program_name", "public_wiring_created", "recorded_at_utc", "schema", "status",
    "terms", "tracking_link_generated", "tracking_link_present")
TERMS_BLOCK_KEYS: tuple = (
    "capture_complete", "hard_trigger_codes", "hard_trigger_count",
    "incomplete_nonblocking", "normalized_chars", "raw_private_path",
    "rubric_verdict", "text_sha256")
AUTHORITY_DOC_KEYS: tuple = ("effects", "program", "schema", "status")
#: DEFECT 2026-09-01 (deploy-fable-20260901). `checked_as_of` was absent from
#: this tuple, so no brief ever asked an engine for it, and three authorities
#: authored that day (joi-blokes-compounded-glp-1s, gatlan,
#: omzo-quiz-v2-scott-disick — plus live-vital-launch outside the batch) came
#: back without it. The builder subscripts it twice
#: (build_glp_affiliate_wirein_incremental_v2.py:970 and :1027), so its absence
#: surfaced as a bare KeyError inside the SHARED build and took a nineteen-row
#: batch down. The "intersection of what is already on disk" rule above can only
#: SHRINK this set; it never pulls a builder-required key back in. Added here.
AUTHORITY_PROGRAM_KEYS: tuple = (
    "advertiser_id", "checked_as_of", "display_name", "field_sources",
    "logo_output_file", "network",
    "network_identity", "official_url", "page_copy", "program_id", "provider_dynamic_profile",
    "publisher_id", "regulatory_banner_required", "research_file", "row_contract",
    "sheet_row", "terms_classification_path", "tracking_host")
AUTHORITY_PAGE_COPY_KEYS: tuple = (
    "affiliate_constraint", "bottom_line", "cancellation", "description",
    "eligibility", "pricing", "regulatory", "shipping", "title")
AUTHORITY_ROW_CONTRACT_KEYS: tuple = (
    "affiliate_href", "affiliate_link_mode", "best_for", "cons", "criteria_scores",
    "insurance", "live_link_allowed", "logo", "medications", "program_id",
    "promotion_authorized", "pros", "provider", "review_path", "score", "source_url",
    "starting_price", "starting_price_sort")

IN_FLIGHT, RESOLVED, FAILED, DISPATCH_FAILED, REFUSED = (
    "in_flight", "resolved", "failed", "dispatch_failed", "refused_not_fit")

#: The steps whose blocker is AUTHORED EVIDENCE — research, the registry profile
#: that evidence binds, and priced rows whose quotes must be captured and hashed.
#: That is the whole authoring class: RESEARCH · REGISTRY · PRICING-EVIDENCE.
#:
#: DEFECT 2026-08-20 (observed live, AUTHORING-DISPATCH-DEFECTS-20260820.md #1):
#: `logo_pipeline` was in this set, and wellmedr-review burned all 3/3 daily
#: cycles on it. Its blocked detail is *"logo asset is present and rendered, but
#: provider-logo-sources.json carries no capture/luminance declaration"* — the
#: asset is already on disk and the missing row is written by
#: `scripts/glp_provider_surface_writers.py::add_logo_source`, which measures the
#: chip treatment off the artwork itself. No amount of research moves it. Buying
#: a 45-minute engine for a step the engine cannot move is how a machine spends
#: its whole day proving nothing, so logo provenance is MACHINE-OWNED and, when
#: the machine cannot move it either, it goes to the error ladder as itself.
#: DEFECT 2026-08-20 (`nuimage-finish-20260820/FINISH-REPORT.md`): this contract
#: commissioned the research packet, the registry profile and the priced rows —
#: and NOT the two artifacts the wire-backlog queue actually blocks on first.
#: `nu-image-medical-review` therefore burned its whole retry budget on
#: `queue_terms_binding_missing` while three authoring cycles worked on content
#: nothing was waiting for. Every earlier provider inherited both files from a
#: retired backfill lane; the first provider to arrive on a new rail inherited
#: neither, and no cycle could clear a block that was never in its brief.
AUTHORING_STEPS: frozenset = frozenset({
    "research_packet",          # program-deep-research-v1 artifact
    "fit_legality_gate",        # registry profile must assert vertical_fit
    "terms_classification",     # affiliate_approval_classification_v1 for the RAIL
    "program_authority",        # b2-wire-backlog-program-spec-v1 (g6-r1a) spec
    "program_registry",         # the release registry profile itself
    "pricing_matrix_row",       # governed pricing rows (evidence-checked writer)
    "taxonomy",                 # therapeutic taxonomy tags (registry-borne)
})

#: Checklist rows whose truth is materialized by the B2 build. Before a valid
#: authoring receipt exists these can still describe missing authored INPUTS
#: (for example the pricing evidence behind ``pricing_matrix_row``), so they
#: remain in ``AUTHORING_STEPS`` where applicable. Once the receipt is
#: ``authored`` + ``fit`` and the staged inputs verify, however, these rows are
#: BUILD-READY: regrading the sealed predecessor is expected to show them red
#: until the builder creates the successor. They must never buy another engine.
#:
#: This is deliberately a separate stateful class rather than moving the five
#: overlapping rows permanently into ``MACHINE_OWNED_STEPS``. Doing the latter
#: would strand a genuinely missing research/price input before authoring ran.
BUILD_READY_STEPS: frozenset = frozenset({
    "research_packet", "fit_legality_gate", "tracking_link_verified",
    "pricing_matrix_row", "review_page", "program_registry",
    "quiz_matrix_entry", "calculator_entry", "compare_pages", "money_tables",
    "provider_count_copy", "taxonomy", "logo_pipeline", "assets",
    "internal_links", "sitemap", "build_full_suite", "deploy",
    "completeness_sensor_pass", "live_url_checks", "indexnow",
    "sheet_status_update", "surface_inventory_grade",
})

AUTHORING_PATH = "authoring"
BUILD_READY_PATH = "build_ready"
MACHINE_PATH = "machine_owned"
AUTHORING_BUILD_GATE_SOURCE = "authoring_build_gate"

#: Every remaining manifest step, named. Union with AUTHORING_STEPS is the whole
#: manifest and the two sets are disjoint — pinned by a test, so a step added to
#: `glp_onboarding_manifest.STEPS` must be classified DELIBERATELY instead of
#: defaulting into (or out of) an engine dispatch by silence.
MACHINE_OWNED_STEPS: frozenset = frozenset({
    "payout_evaluation_queue", "tracking_link_verified", "postback_configuration",
    "wire_in_gate", "review_page",
    "quiz_matrix_entry", "calculator_entry", "compare_pages",
    "money_tables", "provider_count_copy", "logo_pipeline", "assets",
    "internal_links", "sitemap", "build_full_suite", "deploy",
    "completeness_sensor_pass", "live_url_checks", "indexnow",
    "sheet_status_update", "surface_inventory_grade",
})

#: The steps `scripts/glp_provider_surface_writers.py` owns end-to-end. Only one
#: of them is authoring-class, and only for its EVIDENCE half: a price needs a
#: verbatim quote from a first-party capture, which is research. A logo needs an
#: asset the build machine already stages and a measurement the writer already
#: takes. Written down so the distinction is a fact in the code, not a memory.
SURFACE_WRITER_STEPS: frozenset = frozenset({"pricing_matrix_row", "logo_pipeline"})

#: Surface steps that are machine-owned in general but authoring-class in ONE
#: state: when the registry profile does not declare the surface at all. That
#: declaration lives in the registry profile the engine authors, and
#: `step_program_registry` does not check it — so without this rule a provider
#: whose registry exists but omits a surface would block forever with nobody
#: owning the block. The marker below is the manifest's OWN wording; a wiring
#: test pins the two together so a re-phrase breaks a test, not production.
REGISTRY_DECLARATION_STEPS: frozenset = frozenset({
    "quiz_matrix_entry", "calculator_entry", "compare_pages", "sitemap"})
REGISTRY_DECLARATION_MARKER = "registry profile does not declare"


# ---------------------------------------------------------------------------
# WIRE-BACKLOG ROW BLOCKS — the BUILD's own failure, carried into the brief
# ---------------------------------------------------------------------------
#: BLOCKER D, 2026-08-20 — `nuimage-finish-20260820/CLEANROOM-FIX-REPORT.md` §7,
#: and the reason three consecutive authoring cycles could not fix a one-sentence
#: evidence gap:
#:
#:   > The blocked-step list in the brief is derived from the manifest checklist.
#:   > `provider_dynamic_profile_taxonomy_evidence_semantics_missing` exists only
#:   > inside the wire-backlog ROW BLOCK, which the manifest receipt does not
#:   > carry (`receipt["queue"] == {"queued": 0}`; there is no `wire_backlog`
#:   > key). So all three cycles were asked to fix `research_packet,
#:   > fit_legality_gate, pricing_matrix_row, …` and NONE was ever told what
#:   > actually stopped the build.
#:
#: The manifest now reads the row block and puts it on the receipt as a
#: first-class blocked-step row (`glp_onboarding_manifest.wire_backlog_row_blocks`),
#: carrying the block's error class AND its full error text. This half decides
#: WHOSE row it is, and it decides from the ERROR — never from a step label a
#: caller attached — so a builder traceback can never buy a 45-minute engine by
#: arriving with an authoring step name on it, and an authoring evidence gap can
#: never be filed as an infra crash nobody is asked to fix.
WIRE_BACKLOG_BLOCK_SOURCE = "wire_backlog_row_block"

#: EXACT error codes whose artifact is authored, mapped to the manifest step that
#: names that artifact. These are the wire-backlog queue's own prevalidation
#: refusals — every one of them is a file an authoring cycle writes.
WIRE_BACKLOG_AUTHORING_ERROR_CODES: tuple = (
    ("queue_research_binding_missing", "research_packet"),
    ("queue_terms_binding_missing", "terms_classification"),
    ("stage5_program_authority_missing", "program_authority"),
    ("spec_identity_drift", "program_authority"),
    # `checked_as_of` is AUTHORED EVIDENCE, not a build artifact: it is the date
    # of the newest capture the bound research packet cites, it is rendered to
    # readers as "Checked <date>", and it is copied into
    # network_contract.local_evidence_checked_through. Only the engine holding
    # the packet can supply it, so a row refused for it must buy a
    # program_authority cycle rather than land on the machine-owned fixer ladder,
    # where nothing can move it. Added 2026-09-01 alongside the _validate_spec
    # requirement and the AUTHORITY_PROGRAM_KEYS contract change; without this
    # line the three rows lost to KeyError:'checked_as_of' would block by name
    # and then sit there.
    ("spec_checked_as_of_missing", "program_authority"),
    ("spec_checked_as_of_invalid", "program_authority"),
    ("everflow_client_offer_token_mismatch", "program_authority"),
)

#: Error-code PREFIXES from `src/provider-dynamic-profiles.mjs`, whose `fail()`
#: stamps every code `provider_dynamic_profile_<code>`. The three families below
#: are all validations of the `provider_dynamic_profile` block INSIDE the program
#: authority the engine authors — evidence bindings, their taxonomy semantics and
#: the calculator's evidence — so a refusal from any of them is an authoring
#: evidence gap, not a build defect. Everything else the validator can raise
#: (schema/membership/expected-count/normalized-hash/registry-surface) is derived
#: by the builder and stays MACHINE-OWNED.
WIRE_BACKLOG_AUTHORING_ERROR_PREFIXES: tuple = (
    ("provider_dynamic_profile_taxonomy_", "taxonomy"),
    ("provider_dynamic_profile_evidence_", "program_authority"),
    ("provider_dynamic_profile_calculator_", "program_authority"),
)

#: Where a row block lands when nothing above matches: the build step, which is
#: MACHINE-OWNED. Fail-closed in the direction that costs nothing — an unclassified
#: block is reported and fixed by the machine/fixer ladder, never turned into
#: engine spend on a step no engine can move (the `logo_pipeline` lesson).
WIRE_BACKLOG_MACHINE_STEP = "build_full_suite"

#: One colon-joined error token, e.g.
#: `provider_dynamic_profile_taxonomy_evidence_semantics_missing:nu-image-medical-review:delivery_form_tags:injectable`
_WIRE_BACKLOG_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]*")


def _token_at(text: str, index: int) -> str:
    match = _WIRE_BACKLOG_TOKEN.match(text, index)
    return match.group(0).rstrip(":.") if match else ""


def wire_backlog_block_class(error_text: Any) -> dict:
    """Whose row is this wire-backlog block, decided by its own error text.

    Returns `{step, authoring, matched, error_token}`. `matched` is the code (or
    prefix) that decided it, so a receipt can cite the reason rather than assert
    a verdict, and `error_token` is the machine's own refusal string — the thing
    the brief has to name for the engine to be able to fix it.
    """
    text = str(error_text or "")
    for code, step in WIRE_BACKLOG_AUTHORING_ERROR_CODES:
        index = text.find(code)
        if index >= 0:
            return {"step": step, "authoring": True, "matched": code,
                    "error_token": _token_at(text, index) or code}
    for prefix, step in WIRE_BACKLOG_AUTHORING_ERROR_PREFIXES:
        index = text.find(prefix)
        if index >= 0:
            return {"step": step, "authoring": True, "matched": prefix,
                    "error_token": _token_at(text, index)}
    # Machine-owned — but still NAME it. A row block reported as `command_failed`
    # is exactly as uninformative as the checklist row Blocker D hid behind, and
    # the operator card and the fixer ladder both read this. The innermost named
    # error is what a `command_failed` envelope is actually wrapping: BLOCKER E
    # arrived as `B2Error:command_failed:npm:run:1:…Error:
    # provider_logo_treatment_missing:nu-image-medical-review`.
    return {"step": WIRE_BACKLOG_MACHINE_STEP, "authoring": False,
            "matched": "", "error_token": _innermost_named_error(text)}


#: `Error: <token>` / `RuntimeError: <token>` — the line a node or python
#: traceback actually died on, as opposed to the subprocess wrapper around it.
_NAMED_ERROR = re.compile(r"(?:^|\n)\s*(?:[A-Za-z_][A-Za-z0-9_]*)?Error:\s*"
                          r"([a-z0-9_]+(?::[^\s\"'`]+)*)")


def _innermost_named_error(text: str) -> str:
    matches = _NAMED_ERROR.findall(text or "")
    return matches[-1].rstrip(":.") if matches else ""


def is_wire_backlog_row_block(row: Any) -> bool:
    return isinstance(row, dict) and row.get("source") == WIRE_BACKLOG_BLOCK_SOURCE


def is_authoring_row(row: Any) -> bool:
    """Is THIS blocked row authoring's to move? Row-level, not step-level."""
    if not isinstance(row, dict):
        return False
    if is_wire_backlog_row_block(row):
        # Re-derived here from the error text the row carries, deliberately: the
        # dispatcher must not inherit a classification it cannot check.
        return bool(wire_backlog_block_class(
            row.get("error_text") or row.get("detail") or "")["authoring"])
    # ``path`` is written by the manifest only after it has verified an authored
    # fit receipt and all staged build inputs. It is checked after the wire-row
    # branch because a builder traceback is always classified from its own error
    # text, never from a caller-provided label.
    if row.get("path") == BUILD_READY_PATH:
        return False
    if row.get("path") == MACHINE_PATH:
        return False
    if row.get("path") == AUTHORING_PATH:
        return True
    step = row.get("step")
    if step in AUTHORING_STEPS:
        return True
    if step in REGISTRY_DECLARATION_STEPS:
        return REGISTRY_DECLARATION_MARKER in str(row.get("detail") or "").casefold()
    return False


def route_step(row: Any, *, build_ready: bool = False) -> str:
    """Return the honest owner/path for one manifest row.

    ``build_ready`` means the machine has already verified the authoring receipt
    and staged artifacts for this provider. A wire-backlog row remains governed
    by its own error: a real evidence gap can still route to authoring, while a
    build failure routes to the machine/error ladder.
    """
    if not isinstance(row, dict):
        return MACHINE_PATH
    if is_wire_backlog_row_block(row):
        return (AUTHORING_PATH if wire_backlog_block_class(
            row.get("error_text") or row.get("detail") or "")["authoring"]
                else MACHINE_PATH)
    if build_ready and row.get("step") in BUILD_READY_STEPS:
        return BUILD_READY_PATH
    return AUTHORING_PATH if is_authoring_row(row) else MACHINE_PATH


def route_steps(not_green: list, build_ready_providers: set[str] | frozenset[str],
                build_blocked_providers: set[str] | frozenset[str] = frozenset()) -> list:
    """Copy blocked rows and stamp the path taken on every one.

    The returned rows are suitable for ``receipt['not_green']``. Keeping the
    path on the row makes dispatch, receipts and tests consume one decision
    rather than independently re-deriving it.
    """
    ready = set(build_ready_providers or ())
    blocked = set(build_blocked_providers or ())
    routed = []
    for original in not_green or []:
        if not isinstance(original, dict):
            continue
        row = dict(original)
        if row.get("provider") in blocked and not row.get("authoring_class"):
            # An authored+fit handoff whose staged verifier failed is no longer
            # an authoring job. The exact verifier failure is a first-class row
            # written by the manifest; every sibling row stays with the machine
            # so the same authored set cannot silently buy another cycle.
            #
            # DEFECT 2026-09-03: this used to stamp MACHINE_PATH on EVERY row of
            # a blocked provider, including the one row that names an
            # AUTHORING-owned refusal. `spec_checked_as_of_missing` is an
            # authored-evidence gap - only an authoring cycle can supply the
            # date - so filing it machine-owned meant it could never be fixed:
            # refused at the build gate, escalated to the error ladder, refused
            # again next run. Three rows sat there.
            #
            # The sibling rule is preserved exactly: a row is only rescued if it
            # carries its OWN derived authoring_class. A build crash still stamps
            # machine-owned across the provider and still cannot buy a cycle.
            row["path"] = MACHINE_PATH
        else:
            row["path"] = route_step(row, build_ready=row.get("provider") in ready)
        routed.append(row)
    return routed


# ---------------------------------------------------------------------------
# time / io helpers
# ---------------------------------------------------------------------------


def utc_now(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pt_day(now: dt.datetime | None = None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(PT).date().isoformat()


def parse_utc(value: Any) -> float:
    if not value:
        return 0.0
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def load_state(path: str | Path | None = None) -> dict:
    path = Path(path or STATE_PATH)
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and isinstance(data.get("providers"), dict):
            data.setdefault("not_fit", {})
            return data
    except (OSError, ValueError):
        pass
    return {"schema": SCHEMA, "providers": {}, "not_fit": {}}


def save_state(state: dict, path: str | Path | None = None) -> None:
    path = Path(path or STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, default=str) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def provider_record(state: dict, provider: str) -> dict:
    return (state.setdefault("providers", {})
            .setdefault(provider, {"provider": provider, "cycles": []}))


#: Operator cycle-override receipts (schema glp.authoring_cycle_operator_override.v1)
#: live here. An override for (provider, pt_day) makes cycles dispatched BEFORE its
#: at_utc stop counting toward MAX_CYCLES_PER_DAY — a receipted, deterministic reset
#: that never mutates cycle history and never changes the cap mechanism itself.
#: Doctrine: memory `manually-trigger-until-verified-working` (operator 2026-08-20).
OVERRIDES_DIR = os.environ.get(
    "GLP_AUTHORING_OVERRIDES_DIR",
    "reports/glp-clinic-scout/affiliate-machine/onboarding-manifest/overrides")
OVERRIDE_REQUIRED_FIELDS = (
    "operator_order_verbatim", "why", "state_before", "scope_limits", "action")


def operator_override_at(provider: str, now: dt.datetime | None = None) -> str | None:
    """Return the at_utc of a valid override receipt for (provider, today), else None."""
    day = pt_day(now)
    try:
        names = sorted(os.listdir(OVERRIDES_DIR))
    except FileNotFoundError:
        return None
    best = None
    for name in names:
        if provider not in name or not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(OVERRIDES_DIR, name)) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if rec.get("schema") != "glp.authoring_cycle_operator_override.v1":
            continue
        if rec.get("provider") != provider:
            continue
        # A timestamp-shaped JSON file is not operator authority. Ignore partial
        # override receipts so a newer malformed write cannot shadow the latest
        # complete, auditable reset receipt.
        if not all(rec.get(field) for field in OVERRIDE_REQUIRED_FIELDS):
            continue
        at = rec.get("amended_at_utc") or rec.get("at_utc") or ""
        # The receipt applies to the PT day of its own timestamp.
        try:
            stamp = dt.datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if pt_day(stamp) != day:
            continue
        if best is None or at > best:
            best = at
    return best


def cycles_today(state: dict, provider: str, now: dt.datetime | None = None) -> list:
    day = pt_day(now)
    rows = [c for c in (provider_record(state, provider).get("cycles") or [])
            if c.get("pt_day") == day]
    override_at = operator_override_at(provider, now)
    if override_at:
        # DEFECT 2026-08-20 (observed live, this lane, within minutes of the first
        # real override): this read `dispatched_at_utc`, and `dispatch()` writes
        # `dispatched_utc`. Every cycle therefore compared `"" > override_at` ->
        # False and was filtered out, so ANY valid override did not *reset* the
        # daily cap, it DISABLED it for the rest of the PT day. The machine spent
        # three full opus cycles and started a fourth before this was caught.
        #
        # An override is a reset, never an off switch. Read the field the ledger
        # actually writes (keeping the old name as a fallback so a pre-existing
        # record is still read correctly), and treat a cycle with NO timestamp as
        # counting — an unstamped cycle must never be silently forgiven.
        def _dispatched(cycle: dict) -> str:
            return str(cycle.get("dispatched_utc")
                       or cycle.get("dispatched_at_utc") or "")

        rows = [c for c in rows
                if not _dispatched(c) or _dispatched(c) > override_at]
    return rows


def in_flight_cycles(state: dict) -> list:
    out = []
    for rec in (state.get("providers") or {}).values():
        out.extend(c for c in (rec.get("cycles") or [])
                   if c.get("status") == IN_FLIGHT)
    return out


# ---------------------------------------------------------------------------
# What is blocking, and is it authoring's problem
# ---------------------------------------------------------------------------


def authoring_blocks(not_green: list, provider: str = "") -> list:
    """The rows from the machine's OWN receipt that authoring can move.

    Verbatim rows, not a re-derivation: the brief must carry the machine's own
    words, or the agent is fixing a paraphrase of a problem."""
    out = []
    for row in not_green or []:
        if not is_authoring_row(row):
            continue
        if provider and row.get("provider") not in (provider, "", None):
            continue
        out.append(row)
    return sorted(out, key=lambda r: int(r.get("step_index") or 0))


def provider_identity_gate(provider: str, receipt: dict) -> dict:
    """Admit only the canonical provider resolved by the manifest.

    Historical receipts predate ``provider_identity`` and remain readable. New
    manifest receipts carry it, so a network-side alias cannot become a second
    state key if it reaches this module by mistake.
    """
    if "provider_identity" not in receipt:
        return {"ok": True, "why": "legacy receipt has no provider identity binding"}
    bindings = receipt.get("provider_identity")
    if not isinstance(bindings, list):
        return {"ok": False, "why": "provider identity binding is malformed",
                "reason_code": "provider_identity_malformed"}
    canonical = sorted({
        str(item.get("resolved") or "").strip()
        for item in bindings if isinstance(item, dict) and item.get("resolved")
    })
    if provider in canonical:
        return {"ok": True, "why": "provider matches manifest canonical identity",
                "canonical_providers": canonical}
    supplied = [
        item for item in bindings if isinstance(item, dict)
        and str(item.get("supplied") or "").strip() == provider
    ]
    reasons = sorted({str(item.get("reason") or "identity mismatch") for item in supplied})
    return {
        "ok": False,
        "why": (f"provider identity mismatch: {provider} is not a canonical provider in "
                f"this receipt ({', '.join(canonical) or 'none'}); "
                f"{'; '.join(reasons) or 'no matching supplied identity'}"),
        "reason_code": "provider_identity_mismatch",
        "canonical_providers": canonical,
    }


def non_authoring_blocks(not_green: list, provider: str = "") -> list:
    out = []
    for row in not_green or []:
        if not isinstance(row, dict) or is_authoring_row(row):
            continue
        if provider and row.get("provider") not in (provider, "", None):
            continue
        out.append(row)
    return sorted(out, key=lambda r: int(r.get("step_index") or 0))


# ---------------------------------------------------------------------------
# Clean room: fingerprints of everything that already mentions this provider
# ---------------------------------------------------------------------------

_SLUG_SAFE = re.compile(r"[^a-z0-9-]+")

#: Where a previous HAND-RUN could plausibly have left an artifact the agent
#: might reuse instead of capturing first-party. Bounded on purpose: a repo-wide
#: walk at dispatch time is minutes of I/O for a guard that only needs the paths
#: an authoring job actually writes or reads.
#:
#: The quarantine root is listed FIRST and deliberately: it holds the
#: KILLED-MANUAL-RUN class this guard was built for
#: (`nuimage-phase2b-20260820-KILLED-MANUAL-RUN`, 15 hand-made captures), and
#: walking it first means the file cap can never starve the very artifacts the
#: guard exists to catch.
FINGERPRINT_ROOTS = (
    "archives/cleanroom-quarantine",
    "reports/glp-clinic-scout/affiliate-machine",
)

#: THE MACHINE'S OWN PRIOR AUTHORING OUTPUT — excluded from the fingerprint set,
#: and this is the correction of a real defect, not a relaxation.
#:
#: BLOCKER B, 2026-08-20, proven byte-for-byte: `prior_fingerprints()` walked
#: `reports/glp-clinic-scout/affiliate-machine`, which CONTAINS every previous
#: cycle's capture directory. A genuine fresh firecrawl of an unchanged
#: marketing page therefore produced a hash already in the set —
#:
#:   fresh capture nuimagemedical.com          sha 5a2df055a6be7b31…
#:   runs/20260820T160431Z/authoring/…-c2/captures/nuimagemedical.com.md  IDENTICAL
#:
#: — so every honest cycle on a stable-page provider failed
#: `reused_prior_artifacts`, three times in one day. The guard's own output was
#: its next input.
#:
#: WHAT IS EXCLUDED is only what the MACHINE itself authored under its own run
#: tree: prior cycles' capture roots and the dispatcher's cycle receipts. What
#: STAYS IN is everything a human could have planted — the quarantine tree, the
#: hand-run directories (`nuimage-onboard-20260819/`, `phase2-gate-20260820/`),
#: the wire-backlog worker's inputs, and every other non-machine artifact path.
#: And exclusion alone never legitimises anything: a capture is credited only
#: when it also carries a valid THIS-CYCLE fetch receipt (below), so a prior
#: cycle's file copied forward without a re-fetch still fails.
MACHINE_AUTHORING_MARKERS = (
    "reports/glp-clinic-scout/affiliate-machine/onboarding-manifest/authoring/",
    "reports/glp-clinic-scout/affiliate-machine/onboarding-manifest/runs/",
)
#: Within a machine run directory, only the AUTHORING subtree is the machine's
#: own authoring output. A run's wire-backlog/wire-in artifacts are a different
#: lane's records and stay fingerprinted.
MACHINE_AUTHORING_RUN_SEGMENT = "/authoring/"

FINGERPRINT_MAX_FILES = int(os.environ.get("GLP_AUTHORING_FP_MAX", "400"))


def is_machine_authoring_path(rel: str) -> bool:
    """True for the machine's OWN prior authoring output.

    `rel` is repo-relative and forward-slashed. Kept as a named predicate so the
    exclusion is one testable rule rather than a condition buried in a walk."""
    path = str(rel or "").replace("\\", "/").lstrip("./")
    for marker in MACHINE_AUTHORING_MARKERS:
        if not path.startswith(marker):
            continue
        if marker.endswith("/runs/"):
            # runs/<run_id>/authoring/** only
            return MACHINE_AUTHORING_RUN_SEGMENT in path[len(marker):]
        return True
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def provider_needles(provider: str, min_len: int = 5) -> set:
    """The name-fragments a hand-run would plausibly have used for this provider.

    PROVEN necessary 2026-08-20: matching only the exact slug missed
    `nuimage-onboard-20260819/captures/semaglutide.html` — a whole directory of
    hand-made captures for `nu-image-medical`, sitting one token short of the
    filter. A clean-room guard that misses the actual pollution is decoration."""
    slug = _SLUG_SAFE.sub("", (provider or "").casefold().replace("_", "-"))
    if not slug:
        return set()
    tokens = [t for t in slug.split("-") if t]
    needles = {slug, slug.replace("-", "")}
    for size in range(2, len(tokens) + 1):
        needles.add("".join(tokens[:size]))
        needles.add("-".join(tokens[:size]))
    return {n for n in needles if len(n) >= min_len}


def prior_fingerprints(provider: str, roots: tuple = FINGERPRINT_ROOTS,
                       base: Path | None = None) -> dict:
    """sha256 -> repo-relative path, for pre-existing provider artifacts.

    A capture the agent 'produces' whose hash is already in here, AND for which
    it holds no this-cycle fetch receipt, was not captured by this job. That is
    the whole test. The WHOLE relative path is searched, not just the filename:
    hand-runs name the DIRECTORY after the provider and the files inside after
    the page.

    The machine's own prior authoring output is skipped (see
    `is_machine_authoring_path`): re-fetching a page this machine already
    fetched is the expected steady state, not a reuse."""
    base = Path(base or BUSINESS)
    needles = provider_needles(provider)
    found: dict[str, str] = {}
    if not needles:
        return found
    seen = 0
    for root in roots:
        top = base / root
        if not top.is_dir():
            continue
        for path in sorted(top.rglob("*")):
            if seen >= FINGERPRINT_MAX_FILES:
                break
            if not path.is_file():
                continue
            relative = str(path.relative_to(base))
            if is_machine_authoring_path(relative):
                continue
            rel = relative.casefold().replace("_", "-")
            squashed = rel.replace("-", "").replace("/", "")
            if not any(n in rel or n.replace("-", "") in squashed for n in needles):
                continue
            try:
                found.setdefault(_sha256(path), relative)
            except OSError:
                continue
            seen += 1
    return found


def reused_prior_artifacts(receipt: dict, prior: dict,
                           fetched_this_cycle: dict | None = None) -> list:
    """Capture hashes the agent claims that we already had on disk AND cannot
    show a this-cycle fetch for.

    THE DISCRIMINATION (Blocker B, 2026-08-20). Bytes alone cannot separate two
    opposite facts:

      * the agent copied an artifact that was already on disk        — CHEATING
      * the agent re-fetched a page that has not changed since       — HONEST

    An unchanged marketing page is the EXPECTED steady state; NuImage's `/about`
    was byte-identical across three separate captures. So the question the guard
    asks is no longer "are these bytes new?" — which is unanswerable and was
    failing every honest cycle — but "did THIS cycle fetch them?", which the
    capture step now answers with a receipt carrying a request hash and response
    evidence the network returned (`scripts/glp_capture_fetch_receipts.py`).

    Nothing is loosened. A capture that matches history and has NO valid
    this-cycle receipt is still a violation — that is the actual reuse case, and
    it is what a hand-planted quarantine artifact will always be, because no
    fetch of it ever happened. A forged receipt missing its fetch evidence is
    rejected before it gets here, by `validate_receipt`.

    `fetched_this_cycle` maps content sha256 -> the valid receipt that produced
    it. Absent (None or empty), the guard behaves exactly as it did before.
    """
    prior = prior or {}
    fetched = fetched_this_cycle or {}
    hits = []
    for entry in (receipt or {}).get("captures") or []:
        sha = str((entry or {}).get("capture_sha256") or "").strip().casefold()
        if not sha or sha not in prior:
            continue
        proof = fetched.get(sha)
        if proof:
            # Fetched by this cycle. Matching historical bytes is the page being
            # unchanged, which is evidence FOR the capture, not against it.
            continue
        hits.append({"capture_sha256": sha, "prior_path": prior[sha],
                     "claimed_path": (entry or {}).get("capture_path") or "",
                     "source_url": (entry or {}).get("source_url") or "",
                     "no_fetch_receipt_this_cycle": True})
    return hits


def cycle_fetch_receipts(record: dict) -> dict:
    """The valid fetch receipts THIS cycle produced, keyed by content sha256.

    Read from the cycle's own capture root — the directory the brief pointed the
    capture step's `--out` at — so the guard reads evidence the machine itself
    told the engine where to write."""
    capture_root = str((record or {}).get("capture_root") or "")
    if not capture_root:
        return {}
    cycle_id = (str((record or {}).get("cycle_id") or "")
                or fetch_receipts.cycle_id_for_capture_root(capture_root))
    try:
        return fetch_receipts.valid_receipts_by_sha(
            capture_root, cycle_id, str((record or {}).get("dispatched_utc") or ""))
    except Exception:  # noqa: BLE001 - a guard must never take the run down
        return {}


# ---------------------------------------------------------------------------
# The authoring contract — inherited from the fixer charter, never negotiated
# ---------------------------------------------------------------------------

AUTHORING_CONTRACT = """\
## GUARDRAIL CONTRACT (inherited in full — you do not get a lighter version)

Everything in `ops/error-fixer/FIXER-CHARTER.md` binds you, and every
prohibition the tier-2 fix lane carries binds you identically. You were
dispatched because you have hands and a bigger context, NOT because the rules
are looser for an authoring job.

YOU MAY:
  * read anything in the repo; run the machines' own CLIs and test suites
  * capture first-party pages with `%(capture_step)s`
  * WRITE the artifacts named in the ARTIFACT CONTRACT below, and only those
  * add or extend tests; write receipts and reports under `reports/`

YOU MUST NOT — any one of these means you have FAILED the attempt and must stop
and report `verdict: "not_authored"` instead:
  1. Loosen, disable, delete, shorten or "temporarily" bypass ANY guard, gate,
     validator, floor, required-input list, preserve-list, assertion or
     invariant so that a manifest step goes green. A step that is blocked is
     RIGHT until the EVIDENCE changes. You change the evidence; you never
     change the gate.
  2. Lower or edit a scoring floor, or touch anything certified or sealed
     (production-v13 / v14, the scorer, sealed artifacts, certified releases).
  3. Rotate, rewrite, or print any credential or secret. Reading a path is fine.
     A secret value never appears in a card, a receipt, a log or a commit.
  4. Touch payments, invoices, or legality-adjacent copy (FTC/FDA/CAN-SPAM/
     claims surfaces) beyond quoting the provider's own published words.
  5. Write to the governed Google Sheet. It has ONE writer machine and you are
     not it. If a Sheet change is owed, say so in the receipt; do not make it.
  6. Insert a row into the `heartbeats` datatable, at any tier, for any reason.
  7. Delete data of any kind — rows, files, workflows, executions.
  8. Weaken, skip, xfail or delete a test to make a suite go green.
  9. DEPLOY, build a release, seal a release, or run the wire-backlog pipeline.
     The onboarding-manifest machine does ALL deploys. You produce inputs; it
     ships them. `--apply` into a CANDIDATE tree via `%(surface_writers)s` is
     the furthest you go, and only where this brief says so.
 10. Edit `ops/error-fixer/**`, `scripts/glp_onboarding_manifest.py`,
     `scripts/glp_onboarding_authoring_dispatch.py`, or any file another lane is
     actively working in this hour. The machine that dispatched you does not get
     rewritten by the job it dispatched.

EVIDENCE RULES — these are the ones an authoring job is actually tempted by:
  a. FIRST-PARTY CAPTURES ONLY. Every PROVIDER fact comes from a page you fetched
     in THIS job with `%(capture_step)s`, written under `%(capture_root)s`. Not a
     search result, not a summary, not a competitor's table, not a capture some
     earlier run left on disk. Pre-existing artifacts for this provider are
     fingerprinted by sha256 before you start; a capture you list under
     `captures` whose hash matches one of them AND for which this cycle holds no
     FETCH RECEIPT is REJECTED and the cycle failed.
     A fetch receipt is written automatically, for every URL, by
     `%(capture_step)s` into `%(capture_root)s/fetch-receipts.json`. You do not
     write it and you must not edit it. Its consequence for you is simple and
     freeing: **an unchanged page is fine.** If the provider's pricing page is
     byte-identical to last cycle's, that is evidence your capture is honest,
     not a violation — so capture the real page and never "freshen" bytes to
     look new. What is still fatal is citing a file you did not fetch this
     cycle: copying a prior run's capture, or a hand-made one, produces no
     receipt and fails exactly as before.
     ONE bounded exception, and it is a scoping rule rather than a loophole: the
     RAIL's authenticated records (portal offer pages, the governed Sheet row)
     are records the machine already holds and cannot re-fetch without clicking
     through a partner portal. Cite those under `terms_evidence`, with path and
     sha256, NOT under `captures`. Provider facts stay first-party-this-cycle;
     nothing about the clean-room rule for them is relaxed.
  b. EVERY PRICE QUOTE carries a verbatim `quote`, its `source_url`, its
     `capture_path` and its `capture_sha256`. `%(surface_writers)s` re-reads the
     capture, re-hashes it and checks the quote is genuinely inside those bytes;
     a claim whose quote is not in its own cited capture is refused by name and
     nothing is written. Do not try to satisfy that check by editing a capture.
  c. NO INVENTED FACTS. A provider with no published price gets an honest
     `unavailable_reason`, not a number. A provider with no capturable brand
     mark gets `no_verified_mark` and NO manifest entry. Drawing something
     logo-shaped for a brand you never fetched is inventing a provider fact.
  d. SCHEMA PARITY. Every artifact you write must carry the same key set as an
     existing LIVE provider's artifact of that type — read one first and match
     it. A packet with fewer fields is a packet that silently drops a surface.

PROOF (this is the whole job):
  Your cycle counts ONLY if the onboarding machine's OWN re-assessment moves the
  blocked steps. Do not re-run the machine yourself and do not deploy — it
  re-assesses automatically when your job ends. A claimed artifact that does not
  move a manifest row closes nothing.

RECEIPT (mandatory, machine-read):
  Write `%(receipt)s` as JSON, exactly this shape:

  {
    "schema": "%(receipt_schema)s",
    "provider": "%(provider)s",
    "cycle": %(cycle)d,
    "engine": "%(engine)s",
    "model": "%(model)s",
    "engine_policy": "%(engine_policy)s",
    "dispatch_class": "%(dispatch_class)s",
    "verdict": "authored" | "not_authored",
    "failure_class": "evidence_gap" | "capability" | "none",
    "fit_verdict": "fit" | "not_fit" | "unknown",
    "fit_evidence": ["verbatim quote + source_url, one line each"],
    "artifacts_written": ["repo-relative path, one line each"],
    "captures": [{"source_url": "...", "capture_path": "...",
                  "capture_sha256": "..."}],
    "terms_evidence": [{"what": "...", "path": "...", "sha256": "..."}],
    "steps_addressed": ["the manifest step names you moved"],
    "steps_not_addressed": [{"step": "...", "why": "one line"}],
    "tests_run": ["command -> pass/fail"],
    "guardrails_touched": false,
    "operator_card": {
      "what_happened": "one sentence a non-technical owner understands",
      "what_it_means": "what earns or does not earn, concretely",
      "ask": "ONE non-technical thing the operator must do, or \\"\\" if none"
    },
    "notes": "anything the next cycle needs"
  }

  `engine` and `model` are pre-filled above with what actually ran — copy them
  verbatim, never a guess at what "should" have run. `failure_class` is how you
  say WHY a `not_authored` cycle failed: `evidence_gap` when the evidence simply
  is not published (a bigger model changes nothing), `capability` when YOU ran
  out of turns, context or time before you could look. The machine escalates the
  next cycle to a full-capability contract on `capability` and does not on
  `evidence_gap`, so an honest answer here is what stops the fleet from buying
  an expensive re-run of a question that has no answer.

  `fit_verdict` is EVIDENCE, not a decision: the machine reads it and decides.
  If the provider genuinely does not fit this site's vertical or fails a
  legality read, set `"not_fit"` with the quotes that prove it — that is a
  CORRECT outcome, it stops the provider cleanly, and nothing about it counts
  against you. Never guess `"fit"` to be helpful.
  `operator_card` is not optional and is not a place for jargon.
"""


def authoring_contract(provider: str, cycle: int, receipt_path: str,
                       capture_root: str, engine: str = PRIMARY_ENGINE,
                       model: str = PRIMARY_MODEL,
                       engine_policy: str = POLICY_SOL_PRIMARY,
                       dispatch_class_name: str = "light") -> str:
    return AUTHORING_CONTRACT % {
        "provider": provider, "cycle": int(cycle), "receipt": receipt_path,
        "receipt_schema": RECEIPT_SCHEMA, "capture_step": CAPTURE_STEP,
        "surface_writers": SURFACE_WRITERS, "capture_root": capture_root,
        "engine": engine, "model": model, "engine_policy": engine_policy,
        "dispatch_class": dispatch_class_name}


# ---------------------------------------------------------------------------
# The dispatch brief
# ---------------------------------------------------------------------------


def receipt_path_for(provider: str, cycle: int, stamp: str = "") -> str:
    stamp = stamp or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return str(receipt_dir() / f"authoring-{stamp}-{provider}-c{int(cycle)}.json")


def receipt_glob_for(provider: str, cycle: int) -> str:
    return "authoring-*-%s-c%d.json" % (provider, int(cycle))


def capture_root_for(provider: str, cycle: int, run_id: str) -> str:
    return str(ROOT / "runs" / (run_id or "adhoc") / "authoring"
               / f"{provider}-c{int(cycle)}" / "captures")


#: How much of a wire-backlog row block's raw error goes into the brief. The
#: refusal string is the first line of it; the rest is the node traceback, which
#: is what tells an engine WHICH validator refused and where. Generous on
#: purpose — a truncated error is how Blocker D survived three cycles.
ERROR_TEXT_CHARS = 2400


def _row_line(row: dict) -> str:
    ev = "; ".join(str(e) for e in (row.get("evidence") or [])[:4])
    error = str(row.get("error_text") or "").strip()
    if len(error) > ERROR_TEXT_CHARS:
        error = error[:ERROR_TEXT_CHARS] + "\n…(truncated)"
    return "* `%s` (step %s) — %s: %s%s%s" % (
        row.get("step"), row.get("step_index"), str(row.get("status")).upper(),
        str(row.get("detail") or "").strip(),
        ("\n    THE BUILD'S OWN ERROR, verbatim:\n    ```\n%s\n    ```" % error)
        if error else "",
        ("\n    evidence: " + ev) if ev else "")


def build_brief(*, provider: str, cycle: int, run_id: str, receipt: dict,
                sheet_row: dict | None, release: str, receipt_path: str,
                capture_root: str, prior: dict | None = None,
                engine: dict | None = None) -> str:
    """Everything the engine needs, in one prompt. No secrets — paths only."""
    not_green = receipt.get("not_green") or []
    mine = authoring_blocks(not_green, provider)
    theirs = non_authoring_blocks(not_green, provider)
    row = sheet_row or {}
    identity = row.get("identity") or {}
    sheet_line = "(no governed Sheet row bound — say so in the receipt)"
    if row:
        sheet_line = (
            "row %s · %s · network=%s · status=%s · vertical=%s · "
            "program_id=%s · fit_reason=%s" % (
                row.get("row"), identity.get("program_name") or row.get("program"),
                identity.get("network"), row.get("status"), row.get("vertical"),
                identity.get("program_id"), str(row.get("fit_reason") or "")[:200]))
    eng = engine or primary_engine()
    engine_line = "engine running this cycle: `%s` · model `%s`%s\nengine policy: `%s` " \
                  "(authoring class `%s`) — %s%s" % (
                      eng.get("engine"), eng.get("model"),
                      (" · reasoning `%s`" % eng["reasoning"]) if eng.get("reasoning") else "",
                      eng.get("policy") or POLICY_SOL_PRIMARY,
                      eng.get("dispatch_class") or "light", str(eng.get("why") or ""),
                      ("\nESCALATED: " + str(eng.get("why"))) if eng.get("escalated") else "")
    # BLOCKER D: when one of the blocked rows above is the BUILD's own row block,
    # say so explicitly. The rule the engine needs is not "a step is red" — it is
    # "the builder refused these exact bytes, by this exact name".
    wire_rows = [r for r in mine if is_wire_backlog_row_block(r)]
    wire_backlog_note = ""
    if wire_rows:
        tokens = [wire_backlog_block_class(r.get("error_text") or r.get("detail"))
                  ["error_token"] or "(unnamed)" for r in wire_rows]
        wire_backlog_note = """
THE ROW(S) ABOVE MARKED `%(source)s` ARE THE BUILD'S OWN FAILURE, not a checklist
gap. The wire-backlog builder RAN, got as far as this validator and refused by
name:
%(tokens)s
Read the refusal literally — the last colon-separated segments are
`<provider>:<field>:<value>`, i.e. exactly which value of which field the
validator could not bind. Fix THAT, in the artifact that carries it (the program
authority's `provider_dynamic_profile`, and the research packet the profile is
derived from), and re-verify the artifact against the rule the refusal names.
Everything else on the checklist is downstream of this row: the review page, the
registry, the taxonomy and the pricing surfaces are all written BY the build that
this refusal stops, so a cycle that does not move this row moves nothing.
""" % {"source": WIRE_BACKLOG_BLOCK_SOURCE,
       "tokens": "\n".join("  * `%s`" % t for t in tokens)}

    prior = prior or {}
    shown = sorted(prior.items(), key=lambda kv: kv[1])[:40]
    prior_lines = "\n".join("* %s  sha256=%s" % (path, sha[:16]) for sha, path in shown)
    if not prior_lines:
        prior_lines = "(none on disk — a genuinely clean room)"
    elif len(prior) > len(shown):
        prior_lines += ("\n* …and %d more; the full set is enforced by hash whether or "
                        "not it is printed here." % (len(prior) - len(shown)))

    return """\
# AUTHORING CYCLE %(cycle)d/%(max_cycles)d — dispatched BY the onboarding machine

You are not a person who was asked to help. You are the authoring engine that
`glp/onboarding-manifest` routes to when its own checklist blocks on content it
cannot produce. The machine ran, assessed this provider against its whole
checklist (%(step_count)s steps), and
NAMED the steps below as blocked on authoring. It re-assesses automatically the
moment your job ends, and it — not you — decides what ships.

## THE PROVIDER
slug: `%(provider)s`
governed Sheet: %(sheet_line)s
release under assessment: %(release)s
machine receipt this brief is built from: `%(machine_receipt)s`
%(engine_line)s

## THE BLOCKED STEPS — verbatim from the machine's own receipt
%(mine)s
%(wire_backlog_note)s
## NOT YOURS (the machine owns these; they follow once the inputs above exist)
%(theirs)s

`logo_pipeline` is on that list on purpose: the brand asset is staged by the
build machine and its capture/luminance provenance row is written and MEASURED
by `%(surface_writers)s` itself. Do not write, draw, or declare a logo. If a
logo row is what is missing, that is the machine's defect to fix, not yours.

## THE JOB, IN ORDER
1. CAPTURE first-party. `python3 %(capture_step)s --spec <spec.json> --out %(capture_root)s`
   Capture the provider's own pages: home, the GLP-1/medication pages that carry
   prices, eligibility, fees, cancellation/refund, telehealth notice, terms, and
   the brand mark. Record every capture's sha256.
   That command also writes `%(capture_root)s/fetch-receipts.json` — this
   cycle's proof of fetch, keyed by content sha256, read by the clean-room
   guard. Run the capture step through `--out %(capture_root)s` exactly as
   written (it derives the cycle id from that path) and leave the receipts file
   alone. If you must re-capture, run the step again: it MERGES receipts.
2. RESEARCH PACKET — write `%(research_rel)s/%(provider)s.json`, schema
   `%(research_schema)s`. Read an existing LIVE provider's packet in that same
   directory FIRST and match its key set exactly (`fields`,
   `first_party_source_captures`, `provider_dynamic_profile`, …). Every field
   carries `quote` + `source_url` + `verified_as_of`.
   2a. TAXONOMY TAGS ARE BOUND BY THE TAG WORD ITSELF. In
   `provider_dynamic_profile.taxonomy`, every value of `molecule_tags`,
   `brand_name_tags`, `delivery_form_tags` and `treatment_type_tags` must appear
   AS THAT LITERAL WORD inside the `evidence_text` of a binding whose `fields`
   include `taxonomy.<that field>` (underscores may be spaces or hyphens; nothing
   else is accepted). `src/provider-dynamic-profiles.mjs ::
   validatePositiveTaxonomySemantics` refuses the whole build otherwise, by name:
   `provider_dynamic_profile_taxonomy_evidence_semantics_missing:<provider>:<field>:<value>`.
   OBSERVED 2026-08-20 on this provider: the tag `injectable` was bound to an
   excerpt reading "Semaglutide **Injection** As low as $99/mo". "Injection" is
   not "injectable", and the build died. The word WAS on the same captured page
   thirty characters later — "**Injectable** option designed to help support
   appetite awareness" — so the fix is always to WIDEN THE EXCERPT to include the
   provider's own use of the tag word, never to drop the tag and never to
   paraphrase one into the text. If the provider genuinely never writes the word,
   the honest answer is to omit that tag: an unbound tag is an invented fact.
3. THE RAIL'S TERMS DOCUMENT (step `terms_classification`) — required BEFORE any
   build can run. The wire-backlog queue refuses the row as
   `queue_terms_binding_missing` without a `%(terms_schema)s` document at
   `%(terms_rel)s/**/<network>-<network-side program id>.json`.
   This is NOT the provider's consumer terms. It is the terms of the AFFILIATE
   PROGRAM you were approved into, on the rail named in the Sheet row.
   a. EVIDENCE — the machine already holds authenticated records for this rail.
      Read the Sheet row's `Commission / Terms`, `Compliance / Disclosure Notes`
      and `API Capabilities` cells (they were transcribed from the authenticated
      portal), and the portal captures under
      `reports/glp-clinic-scout/affiliate-machine/` that the row's Notes cite.
      Portal credentials, when a fresh capture is genuinely needed, are at
      `~/.hermes/secrets/affiliate-marketplaces/<network>/` — read the path,
      never print a value, and never click an affiliate tracking link.
   b. SCOPE, and this one has bitten before: the raw capture is the PROGRAM's
      own offer terms (offer/general/payouts/creatives/landing-page pages). It
      is NOT the network's generic partner agreement and NOT the advertiser's
      patient-facing terms of sale. Screening those through the operator-account
      filters is a category error, and it produces a FALSE
      `operator_owned_account_action` — which is a TERMINAL verdict that would
      kill this provider permanently. Existing raw captures under
      `%(terms_raw_rel)s` show the scope; read one first.
   c. WRITE the raw capture as JSON under `%(terms_raw_rel)s` carrying verbatim
      `terms_text` plus `source_bindings` (path + sha256 of every portal capture
      it was transcribed from). Verbatim means verbatim — no paraphrase.
   d. THEN let the MACHINE classify it. Call
      `%(terms_writer)s :: record_approval_classification(...)`, passing the
      screened terms from `screen_provider_terms({"terms": <text>})` and
      `raw_terms_path`. The portfolio, vertical, wire-eligibility and legality
      cells are the classifier's to fill, never yours. If that writer REFUSES
      (hard triggers, or a relationship that is not approved), that refusal is a
      real finding: report `not_authored` with the refusal verbatim. Do not
      hand-write the JSON around a writer that said no.
   e. The document must carry every key a live one carries:
      top level `%(terms_doc_keys)s`; `terms` block `%(terms_block_keys)s`.
4. THE PROGRAM AUTHORITY (step `program_authority`) — `%(authority_rel)s/%(provider)s.json`,
   schema `%(authority_schema)s` (the g6-r1a spec; the legacy multi-program form
   `%(authority_legacy_schema)s` is also accepted). This is what DRIVES the
   wire-in builder — the builder materialises its own tree from the sealed base
   and reads THIS file, so a candidate tree you edited by hand is not an input.
   COMPOSE it from artifacts that are already double-verified — the research
   packet from step 2 and the registry profile from step 5 — never from a fresh
   opinion:
     * `program` keys: `%(authority_program_keys)s`
     * `page_copy` keys: `%(authority_page_copy_keys)s` — this is the review page
       a customer reads. Brand voice: confident, strengths-first, never
       self-deprecating. Honesty rules bind absolutely: every number and claim
       must already be attested in the research packet (the prepare-time check
       `spec_page_fact_unattested` re-derives the value tokens and refuses copy
       that carries a figure the packet does not evidence). Compounded-drug and
       telehealth regulatory language is legality-adjacent: quote the provider
       and the FDA, never characterise.
     * `checked_as_of` — the ISO date (YYYY-MM-DD) through which the LOCAL
       evidence in this authority is current. It is the date of the newest
       capture the research packet actually cites, not today's date and not the
       date you are running. It is rendered to readers as "Checked <date>" and
       it is copied verbatim into `network_contract.local_evidence_checked_through`,
       so a date not backed by a capture is a false claim on a customer-facing
       page. Derive it from the bound research packet's own evidence: the date
       part of its `captured_at_utc`, or a later date carried by a
       `first_party_source_captures` entry if one is newer. (The live
       comparator: `novi-weight-loss-glp-1` was captured
       2026-08-30T19:16:47Z and its authority carries `checked_as_of`
       2026-08-30.) If the packet carries no dated capture at all, report
       `not_authored` with that as the reason rather than inventing one.
     * `row_contract` keys: `%(authority_row_contract_keys)s`. `affiliate_href`
       is the Sheet row's OWN tracking link, unmodified.
     * `terms_classification_path` must be the absolute path of the document you
       wrote in step 3, and `research_file` the packet's filename.
     * `tracking_host` — a rail without fixed hosts needs a host to hold the
       tracking link to, and `glp_affiliate_link_rails.spec_allowed_host()` owns
       the resolution order. Ask it, do not reason it out:
       `requires_declared_host(<network>)` True means you MUST supply the field
       (a `tracking_domain` rail rotates its domain per tenant and nothing can
       derive it). False means you must NOT: a fixed-host rail (Katalys, Awin)
       already knows its host, and a `merchant_host` rail (Trackdesk
       direct-linking, Refersion) DERIVES it from your `official_url`.
       That derivation is the point. `official_url` is authored from first-party
       research captures, independently of the tracking link, so checking the
       link against it is a real check. **Never take `tracking_host` from the
       tracking link's own hostname** — that is a tautology and it defeats the
       gate. Get `official_url` right and the host follows.
       OBSERVED 2026-08-20: an unresolvable host is what stopped the first
       Trackdesk build, one step after the terms and authority landed; it now
       blocks by name at prevalidation (`spec_tracking_host_unresolvable`)
       instead of dying inside the builder as `command_failed`.
     * XENTRAFFIC HAS TWO ID NAMESPACES. The governed network program id is the
       numeric portal offer id from `/offers/<id>`. `advertiser_id` is NOT that
       number: it is the opaque immutable offer token from the independently
       verified `glp.wire_in_identity_evidence.v1` capture under the canonical
       wire-identities directory. Copy that capture's exact binding into
       `network_identity` and its independently captured host into
       `tracking_host`. Never fetch, open, or click the tracking URL; validate
       only the captured bytes, hash, host, and path token.
     * LOGO: `logo_output_file` (`<program_id>.webp`) plus a `logo_source`
       binding — absolute `path` to a webp you produced from the provider's own
       published brand mark, with `sha256`, `bytes`, `width`, `height`,
       `media_type: "image/webp"`, `source_asset_url` (https, first-party) and
       `program_id`. That binding is the INPUT the luminance pipeline measures.
       You still do NOT write `provider-logo-sources.json` and do NOT run
       `--logo-spec`; the machine owns the provenance row and the measurement.
       No capturable mark means `no_verified_mark` reported honestly in your
       receipt — never a drawn or imagined logo.
   VALIDATE YOUR OWN OUTPUT before you finish, with the pipeline's real
   validator rather than by eye:
     `python3 -c "import sys;sys.path.insert(0,'scripts');import
      glp_affiliate_wire_backlog_worker as w;
      spec=w._extract_program_spec(w.read_json(<authority>), '%(provider)s');
      w._validate_spec(spec, {'site_program_id':'%(provider)s','sheet_row':<row>,
      'provider_program_id':'<network program id>','network':'<network>'},
      <research path>, <terms path>); print('SPEC OK')"`
   A refusal names its own reason; fix the artifact, never the validator.
5. REGISTRY PROFILE — the release registry profile for this provider under the
   `%(site_profile)s` profile must assert `vertical_fit`, carry the therapeutic
   `taxonomy` tags, and DECLARE the surfaces the manifest checks (quiz,
   calculator, compare, sitemap). Match the shape an existing live provider
   uses; do not invent a new one. The registry's `research_dynamic_profile`
   needs hash-bound `evidence_bindings` (`evidence_text` + `source_url` +
   `evidence_sha256`) or `research_packet` stays blocked.
6. PRICING — write the spec and run
   `python3 %(surface_writers)s --release <CANDIDATE> --pricing-spec … --apply`.
   That writer re-reads and re-hashes every capture and refuses a quote that is
   not inside its own cited bytes. If it refuses, FIX THE CLAIM, never the check.
   `--logo-spec` is NOT yours — see the machine-owned list above.
7. FIT — read the provider's own words on what it sells and to whom, and set
   `fit_verdict` with the quotes that prove it. `not_fit` is a correct answer.
8. RECEIPT — write the JSON below. Then STOP. Do not build, do not deploy, do
   not re-run the onboarding machine.

Do only the steps your BLOCKED STEPS list names. Steps 3 and 4 are upstream of
all content: while they are blocked, nothing downstream of them can be built, so
they come first in the same cycle they appear.

## CLEAN ROOM — pre-existing artifacts for this provider (fingerprinted)
A killed manual run and earlier hand-work left files on disk. They are NOT your
output and reusing them makes this whole lane a theatre. Every capture you cite
is re-checked against these hashes; a match FAILS the cycle.
%(prior_lines)s

## READ THESE FIRST
* `%(briefing)s` — the fleet briefing (webhook auth, workflow/datatable ids,
  bridge contract, the hard-won gotchas).
* `%(charter)s` — the fixer charter, whose tier rules bind you.
* `%(machine_receipt)s` — this machine's own receipt, in full.

## BUDGET
%(budget)d minutes, one shot. If you are not going to make it, stop early and
write the receipt with `verdict: "not_authored"` and honest
`steps_not_addressed` — a receipt that arrives beats an artifact that does not.
Cycle %(cycle)d of at most %(max_cycles)d today; after that the machine routes
this to the error ladder instead of asking you again.

%(contract)s
""" % {
        "cycle": int(cycle), "max_cycles": MAX_CYCLES_PER_DAY,
        # Derived, never restated: the checklist grew by two steps on 2026-08-20
        # and a brief that still said "24" would be teaching a stale fact.
        "step_count": len(AUTHORING_STEPS | MACHINE_OWNED_STEPS),
        "provider": provider, "sheet_line": sheet_line, "release": release or "(unknown)",
        "machine_receipt": receipt.get("receipt_path") or "(not recorded)",
        "mine": "\n".join(_row_line(r) for r in mine) or "(none — nothing to author)",
        "theirs": "\n".join(_row_line(r) for r in theirs) or "(none)",
        "wire_backlog_note": wire_backlog_note,
        "capture_step": CAPTURE_STEP, "capture_root": capture_root,
        "research_rel": RESEARCH_REL, "research_schema": RESEARCH_SCHEMA,
        "terms_rel": TERMS_REL, "terms_schema": TERMS_SCHEMA,
        "terms_raw_rel": TERMS_RAW_REL, "terms_writer": TERMS_WRITER,
        "terms_doc_keys": ", ".join(TERMS_DOC_KEYS),
        "terms_block_keys": ", ".join(TERMS_BLOCK_KEYS),
        "authority_rel": AUTHORITY_REL, "authority_schema": AUTHORITY_SCHEMA,
        "authority_legacy_schema": AUTHORITY_LEGACY_SCHEMA,
        "authority_program_keys": ", ".join(AUTHORITY_PROGRAM_KEYS),
        "authority_page_copy_keys": ", ".join(AUTHORITY_PAGE_COPY_KEYS),
        "authority_row_contract_keys": ", ".join(AUTHORITY_ROW_CONTRACT_KEYS),
        "site_profile": "glp-clinic-scout", "surface_writers": SURFACE_WRITERS,
        "prior_lines": prior_lines, "briefing": BRIEFING_DOC, "charter": CHARTER_DOC,
        "budget": BUDGET_S // 60, "engine_line": engine_line,
        "contract": authoring_contract(
            provider, cycle, receipt_path, capture_root,
            engine=str(eng.get("engine") or PRIMARY_ENGINE),
            model=str(eng.get("model") or PRIMARY_MODEL),
            engine_policy=str(eng.get("policy") or POLICY_SOL_PRIMARY),
            dispatch_class_name=str(eng.get("dispatch_class") or "light")),
    }


# ---------------------------------------------------------------------------
# Engine policy — one config-resolved primary engine, no model escalation
# ---------------------------------------------------------------------------


def dispatch_class(blocked_steps: Any = ()) -> str:
    """`"research_packet"` or `"light"` — which authoring class this cycle is.

    One function, so the ledger, the brief, the receipt, the card and the status
    view cannot disagree about which policy applied."""
    return ("research_packet"
            if set(str(s) for s in (blocked_steps or ())) & RESEARCH_PACKET_CLASS
            else "light")


def primary_engine(blocked_steps: Any = ()) -> dict:
    """The engine a cycle runs on — the resolved primary, always (operator
    routing order 2026-08-20: recurring research runs on the OpenAI lane; opus
    only reviews). The MODEL is config-resolved since the 2026-09-03 conserve
    ruling; the policy — one primary, no escalation — is unchanged.

    What the class distinction selects now is the TURN BUDGET, not the model:
    research-packet-class work gets `RESEARCH_MAX_TURNS`, everything lighter
    gets `MAX_TURNS`. Both are clamped to the bridge's own ceiling — a payload
    the bridge rejects is a dispatch that silently never happens."""
    if dispatch_class(blocked_steps) == "research_packet":
        turns = max(1, min(BRIDGE_MAX_TURNS, RESEARCH_MAX_TURNS))
        return {"engine": PRIMARY_ENGINE, "model": PRIMARY_MODEL,
                "reasoning": PRIMARY_REASONING, "escalated": False,
                "policy": POLICY_RESEARCH_PACKET,
                "dispatch_class": "research_packet",
                "max_turns": turns,
                "why": ("research-packet-class authoring (blocked: %s) runs "
                        "%s %s with the heavy %d-turn budget — operator routing "
                        "2026-08-20: this lane carries recurring research, opus "
                        "only reviews; the 2026-08-20 failures were a 12-turn "
                        "ceiling, not a model verdict"
                        % (", ".join(sorted(set(str(s) for s in blocked_steps or ())
                                            & RESEARCH_PACKET_CLASS)),
                           PRIMARY_MODEL, PRIMARY_REASONING, turns))}
    return {"engine": PRIMARY_ENGINE, "model": PRIMARY_MODEL,
            "reasoning": PRIMARY_REASONING, "escalated": False,
            "policy": POLICY_SOL_PRIMARY, "dispatch_class": "light",
            "max_turns": max(1, min(BRIDGE_MAX_TURNS, MAX_TURNS)),
            "why": ("%s %s is the primary engine for light authoring classes"
                    % (PRIMARY_MODEL, PRIMARY_REASONING))}


def _capability_marker(text: Any) -> str:
    haystack = str(text or "").casefold()
    for marker in CAPABILITY_MARKERS:
        if marker in haystack:
            return marker
    return ""


def classify_failure(record: dict, agent: dict | None = None,
                     job: dict | None = None) -> dict:
    """WHY did this cycle fail — capability, or an evidence gap?

    Nothing escalates to a bigger model any more, but the distinction still
    matters everywhere the failure is read: a capability failure says the
    CONTRACT (budget, tooling, bridge) needs fixing and belongs to the error
    ladder; an evidence gap says the PROVIDER cannot support the claim, and no
    contract change may be spent trying to make that answer different — that
    temptation is how invented facts get onto a money page.
    """
    record = record or {}
    agent = agent or {}
    job = job or record.get("job") or {}

    if record.get("status") == DISPATCH_FAILED or record.get("dispatch_error"):
        return {"failure_class": CAPABILITY,
                "why": "the engine never started: %s"
                       % (record.get("dispatch_error") or "the bridge refused the job")}

    rc = job.get("rc")
    if rc not in (None, "", 0):
        return {"failure_class": CAPABILITY,
                "why": "the bridge job exited rc=%s — the run did not finish, so it "
                       "learned nothing about the provider" % rc}
    if str(job.get("status") or "").casefold() in ("error", "failed", "timeout"):
        return {"failure_class": CAPABILITY,
                "why": "the bridge reported job status %r" % job.get("status")}

    marker = ""
    for text in (job.get("stdout_tail"), job.get("error"), agent.get("notes"),
                 json.dumps(agent.get("steps_not_addressed") or [], default=str)):
        marker = marker or _capability_marker(text)
    if marker:
        return {"failure_class": CAPABILITY,
                "why": "the run hit a capability ceiling (%r in its own output)" % marker}

    if not agent:
        return {"failure_class": CAPABILITY,
                "why": "the engine finished without writing its mandatory receipt — a run "
                       "that cannot report is a run that could not work"}

    declared = str(agent.get("failure_class") or "").strip().casefold()
    if declared == EVIDENCE_GAP:
        return {"failure_class": EVIDENCE_GAP,
                "why": "the engine reported an evidence gap: %s"
                       % (str(agent.get("notes") or "")[:200] or "no further detail")}
    if str(agent.get("verdict") or "").strip().casefold() == "not_authored":
        return {"failure_class": EVIDENCE_GAP,
                "why": "the engine ran to completion and reported `not_authored` without a "
                       "capability ceiling — that is an evidence answer, not a bigger-model "
                       "problem"}
    return {"failure_class": NO_FAILURE, "why": ""}


def engine_for(provider: str, state: dict, now: dt.datetime | None = None,
               blocked_steps: Any = ()) -> dict:
    """Which engine this cycle runs on — Sol, unconditionally.

    There is no model-escalation ladder any more (operator routing order,
    2026-08-20: opus only reviews, never authors). What remains of the ladder
    is HONESTY: when the last terminal cycle failed for capability reasons on
    this same Sol contract, the record says so out loud instead of implying a
    bigger contract is still available — the daily cap and the error ladder
    own the repeat, and that is where a human or the fixer decides what
    changes.

    This deliberately looks at the last TERMINAL cycle, not the last cycle
    today: a capability failure at 23:59 is still a capability failure at
    00:01, and a resolved cycle in between clears it."""
    base = primary_engine(blocked_steps)
    cycles = provider_record(state, provider).get("cycles") or []
    prev = None
    for cycle in reversed(cycles):
        if cycle.get("status") == IN_FLIGHT:
            continue
        prev = cycle
        break
    if (prev and prev.get("status") in (FAILED, DISPATCH_FAILED)
            and str(prev.get("failure_class") or "") == CAPABILITY):
        base = dict(base)
        base["why"] += (" (cycle %s already failed for capability reasons — %s — and "
                        "there is no model escalation behind this contract; the "
                        "daily cap and the error ladder take it from here)"
                        % (prev.get("cycle"),
                           prev.get("failure_reason") or prev.get("outcome_note")
                           or "no reason recorded"))
    return base


# ---------------------------------------------------------------------------
# Eligibility — every bound in one place
# ---------------------------------------------------------------------------


def may_dispatch(provider: str, receipt: dict, state: dict,
                 now: dt.datetime | None = None, *, entry: str = "bridge") -> dict:
    """{'ok': bool, 'why': str, ...}. Nothing dispatches around this."""
    if not ENABLED:
        return {"ok": False, "why": "authoring dispatch disabled by GLP_AUTHORING_ENABLED",
                "reason_code": "disabled"}

    identity = provider_identity_gate(provider, receipt)
    if not identity["ok"]:
        return identity

    stop = (state.get("not_fit") or {}).get(provider)
    if stop:
        return {"ok": False, "reason_code": "not_fit",
                "why": ("`%s` was recorded not_fit on %s — the machine STOPPED this "
                        "provider; nothing deploys and no further authoring is bought"
                        % (provider, stop.get("recorded_utc") or "an earlier cycle")),
                "not_fit": stop}

    # DEFECT 2026-09-03, the third place this same mistake lived: the SOURCE of a
    # block was being read as its CLASSIFICATION. Every build-handoff refusal was
    # treated as machine-owned, so a spec the builder refused for missing AUTHORED
    # EVIDENCE could never buy the very cycle that would fix it - it was refused
    # here, escalated to the error handler, and re-refused on the next run.
    # `spec_checked_as_of_missing` on joi-blokes-compounded-glp-1s, gatlan and
    # omzo-quiz-v2-scott-disick is exactly that shape.
    #
    # Honour the row's own derived `authoring_class`. A handoff block that is
    # authoring-owned falls through to the normal authoring path below; anything
    # machine-owned or unclassified still refuses here, so the fail-closed
    # direction is unchanged.
    handoff_blocks = [
        row for row in receipt.get("not_green") or []
        if isinstance(row, dict)
        and row.get("provider") in (provider, "", None)
        and row.get("source") == AUTHORING_BUILD_GATE_SOURCE
        and not row.get("authoring_class")
    ]
    if handoff_blocks:
        return {
            "ok": False,
            "reason_code": "build_handoff_blocked_machine_owned",
            "why": ("authored fit handoff reached the staged-input gate, but the "
                    "machine refused it: %s" %
                    str(handoff_blocks[0].get("detail") or "unnamed build gate failure")),
            "build_handoff_blocks": [row.get("detail") for row in handoff_blocks],
        }

    blocks = authoring_blocks(receipt.get("not_green") or [], provider)
    if not blocks:
        build_ready = sorted({
            str(row.get("step") or "")
            for row in receipt.get("not_green") or []
            if isinstance(row, dict)
            and row.get("provider") in (provider, "", None)
            and row.get("path") == BUILD_READY_PATH
        })
        if build_ready:
            return {
                "ok": False,
                "reason_code": "build_ready_machine_owned",
                "build_ready_steps": build_ready,
                "why": ("authored fit inputs verify; %d remaining step(s) are "
                        "BUILD-READY machine work, so another authoring cycle is forbidden"
                        % len(build_ready)),
            }
        return {"ok": False, "reason_code": "no_authoring_blocks",
                "why": "no authoring-class step is blocked for %s" % provider}

    running = in_flight_cycles(state)
    cap = max(1, min(4, int(os.environ.get("GLP_AUTHORING_NATIVE_MAX_IN_FLIGHT", "4")))) if entry == "native" else MAX_IN_FLIGHT
    if any(c.get("provider") == provider for c in running) or len(running) >= cap:
        return {"ok": False, "reason_code": "in_flight",
                "why": ("an authoring job is already in flight (%s) — the bridge "
                        "carries 3 jobs and the fleet needs the slots"
                        % ", ".join("%s c%s" % (c.get("provider"), c.get("cycle"))
                                    for c in running))}

    today = cycles_today(state, provider, now)
    if len(today) >= MAX_CYCLES_PER_DAY:
        return {"ok": False, "reason_code": "cycle_cap",
                "why": ("%s already used %d/%d authoring cycles today — the cap is the "
                        "signal that authoring is not the blocker; routing to the error "
                        "ladder" % (provider, len(today), MAX_CYCLES_PER_DAY)),
                "cycles_today": len(today)}

    return {"ok": True, "reason_code": "eligible", "cycle": len(today) + 1,
            "blocks": [b.get("step") for b in blocks],
            "why": "%d authoring-class step(s) blocked; cycle %d/%d"
                   % (len(blocks), len(today) + 1, MAX_CYCLES_PER_DAY)}


# ---------------------------------------------------------------------------
# Bridge I/O (injectable)
# ---------------------------------------------------------------------------


def sink() -> str:
    """`GLP_AUTHORING_SINK=<path>` writes the dispatch to a FILE and never calls
    the bridge. Same discipline as the fixer's tier-2 sink: a test or a
    rehearsal must be unable to spawn a 45-minute run by construction, not by
    somebody remembering to stub the right function."""
    return os.environ.get("GLP_AUTHORING_SINK", "").strip()


def _sink_write(brief: str, model: str, timeout_s: int, provider: str,
                cycle: int, callback_url: str = "", engine: dict | None = None) -> dict:
    eng = engine or primary_engine()
    path = Path(sink())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"written_utc": utc_now(), "provider": provider,
                             "cycle": cycle, "engine": eng.get("engine"), "model": model,
                             "reasoning": eng.get("reasoning"),
                             "escalated": bool(eng.get("escalated")),
                             "engine_policy": eng.get("policy") or POLICY_SOL_PRIMARY,
                             "dispatch_class": eng.get("dispatch_class") or "light",
                             "timeout_s": timeout_s,
                             "callback_url": callback_url,
                             "brief_chars": len(brief), "brief": brief},
                            ensure_ascii=False) + "\n")
    return {"job_id": "sink-%s-c%d" % (provider, cycle), "status": "running",
            "sink": str(path), "posted_to_bridge": False}


def _bridge_headers() -> dict:
    token = BRIDGE_TOKEN_FILE.read_text().strip()
    return {"Content-Type": "application/json",
            "Authorization": "Bearer " + token,
            "User-Agent": BRIDGE_UA}


def bridge_hermes_chat(prompt: str, model: str | None = None,
                       timeout_s: int | None = None, callback_url: str = "",
                       http_timeout: int = HTTP_DISPATCH_ACK_TIMEOUT_S,
                       max_turns: int | None = None) -> dict:
    """Fire a Hermes-managed authoring job on the bridge and DO NOT WAIT.

    200 with the finished record if the job beats the bridge's 25s sync window,
    else 202 {job_id, status: running}. Either way only the job id is kept — a
    45-minute agent must never be held inside the manifest run (whose own bridge
    action is capped at 7800s and whose n8n Wait is finite)."""
    body: dict[str, Any] = {"action": "hermes_chat", "prompt": prompt,
                            "model": model or MODEL,
                            "reasoning": REASONING,
                            # Clamped to the bridge's own ceiling: a payload it
                            # rejects (`max_turns must be 1..N`) is a dispatch
                            # that silently never happens.
                            "max_turns": max(1, min(BRIDGE_MAX_TURNS,
                                                    int(max_turns or MAX_TURNS))),
                            "toolset_profile": TOOLSET_PROFILE,
                            "timeout_s": int(timeout_s or BUDGET_S)}
    if callback_url:
        body["callback_url"] = callback_url
    return _bridge_run(body, http_timeout)


def _read_bounded(resp, deadline: float, what: str) -> bytes:
    """Read a bridge response body under a WALL-CLOCK bound.

    DEFECT 2026-08-20 #5, same root cause as the datatable client: a socket
    timeout fires when one `recv` is idle too long, so a peer that dribbles
    bytes — or a half-open connection nothing reaps — can hold a plain
    `resp.read()` for hours without ever tripping it. The deadline is checked
    before every chunk, so the worst case is bounded by the clock, not by the
    peer's manners."""
    # `read1`, never `read(n)`: `read(n)` blocks until it HAS n bytes, so a peer
    # dribbling bytes keeps the interpreter inside one call and the deadline is
    # never re-checked. `read1` returns what one raw read produced, which is
    # what makes the deadline an actual bound.
    reader = getattr(resp, "read1", None) or resp.read
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("%s exceeded its %ds wall-clock deadline while reading "
                               "the response (%d bytes so far)"
                               % (what, HTTP_DEADLINE_S, total))
        chunk = reader(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > HTTP_MAX_RESPONSE_BYTES:
            raise ValueError("%s returned more than %d bytes — refusing to buffer an "
                             "unbounded response" % (what, HTTP_MAX_RESPONSE_BYTES))
        chunks.append(chunk)
    return b"".join(chunks)


def _http_json(req: urllib.request.Request, what: str,
               http_timeout: int, attempts: int, sleep=time.sleep) -> dict:
    """One bounded bridge round trip, reported as data and never as a hang.

    `attempts` is the retry bound and it is NOT the same for both callers:
      * `/job/<id>` is a read — retried, because asking twice costs nothing;
      * `/run` is `attempts=1` DELIBERATELY. A retry there could buy a second
        45-minute agent for the same cycle, which is the one thing this lane
        may never do (`test_a_second_dispatch_while_one_is_in_flight_is_refused`).
        The retry for a dispatch is the next CYCLE, under the 3/day cap.
    """
    last = {"error": "no attempt was made"}
    for attempt in range(1, max(1, attempts) + 1):
        # Wall deadline scales with the caller's read bound (dispatch acks pass
        # 180s) but never below the strict default. Always FINITE.
        deadline = time.monotonic() + max(HTTP_DEADLINE_S, http_timeout + 15)
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                raw = _read_bounded(resp, deadline, what)
            return json.loads(raw.decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            last = {"error": "http %s" % exc.code, "detail": detail}
            if exc.code not in HTTP_RETRY_STATUS:
                return last
        except Exception as exc:  # noqa: BLE001
            last = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:260])}
        if attempt < max(1, attempts):
            sleep(HTTP_BACKOFF_S[min(attempt - 1, len(HTTP_BACKOFF_S) - 1)])
    return last


def _bridge_run(body: dict, http_timeout: int = HTTP_READ_TIMEOUT_S) -> dict:
    req = urllib.request.Request(
        BRIDGE_BASE + "/run", data=json.dumps(body).encode("utf-8"),
        method="POST", headers=_bridge_headers())
    return _http_json(req, "bridge POST /run", http_timeout, attempts=1)


def poster_for(engine: dict) -> Callable:
    """The live bridge function for an engine choice. One place, so a new engine
    cannot be recorded in the ledger while a different one is actually posted.

    Sol is the ONLY authoring engine (operator routing order 2026-08-20 — the
    `claude_p` opus authoring lane is deleted, opus only reviews), so what this
    binds now is the TURN BUDGET the engine choice carries: the returned poster
    posts `hermes_chat` with exactly the `max_turns` the recorded engine dict
    named."""
    turns = int((engine or {}).get("max_turns") or MAX_TURNS)

    def _post(prompt: str, model: str | None = None,
              timeout_s: int | None = None, callback_url: str = "") -> dict:
        return bridge_hermes_chat(prompt, model, timeout_s, callback_url,
                                  max_turns=turns)
    return _post


def bridge_job_status(job_id: str, http_timeout: int = HTTP_READ_TIMEOUT_S) -> dict:
    """Read one bridge job record. Idempotent, so it gets the full retry ladder."""
    req = urllib.request.Request(BRIDGE_BASE + "/job/" + str(job_id),
                                 headers=_bridge_headers())
    return _http_json(req, "bridge GET /job/%s" % job_id, http_timeout,
                      attempts=HTTP_MAX_ATTEMPTS)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@contextmanager
def ledger_lock(state_path=None):
    path = Path(state_path or STATE_PATH).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def commissioning_context():
    """Read the caller's actual Linear child and its existing reviewed brief."""
    from lane_autodispatch import review_record, brief_sha256, brief_tier, t2_approved
    from linear_client import Client

    identifier = os.environ.get("GLP_AUTHORING_COMMISSIONING_ISSUE", "").strip()
    if not re.fullmatch(r"GLP-\d+", identifier):
        raise ValueError("commissioning_issue_missing")
    issue = Client().gql(
        "query($id:String!){issue(id:$id){id identifier title description "
        "parent{id identifier} labels{nodes{name}} state{name type} assignee{id name} "
        "delegate{id name} comments(first:5){nodes{createdAt body}} "
        "relations{nodes{type relatedIssue{identifier}}} "
        "inverseRelations{nodes{type issue{identifier}}}}}", {"id": identifier})["issue"]
    review = review_record(identifier)
    if review.get("verdict") != "APPROVE" or review.get("issue") != issue["identifier"]:
        raise ValueError("BRIEF_UNREVIEWED")
    path = Path(review["brief_path"])
    if review.get("brief_sha256") != brief_sha256(path):
        raise ValueError("BRIEF_STALE")
    text = path.read_text()
    tier = brief_tier(text, review)
    if tier == "T2" and not t2_approved(identifier, text):
        raise ValueError("T2_NEEDS_OPERATOR")
    return {"issue": issue, "review": review, "brief_path": str(path),
            "brief_sha256": review["brief_sha256"], "tier": tier, "brief_text": text}


def dispatch(*, entry="bridge", native_runner=None, **kwargs):
    """Reserve under the ledger lock; native execution never holds that lock.

    Native uses the same cycle allocation, brief, paths and receipt adjudicator.
    An interrupted owner leaves its reservation in flight, never silently retries.
    """
    if entry not in {"bridge", "native"}:
        raise ValueError("unknown authoring entry")
    if entry == "bridge":
        with ledger_lock(kwargs.get("state_path")):
            return _dispatch(**kwargs)
    if kwargs.get("state") is not None or kwargs.get("bridge_post") is not None:
        raise ValueError("native entry owns its fresh ledger and transport")
    if native_runner is None:
        kwargs["commissioning"] = commissioning_context()
    prompt = []

    def reserve(brief, model=None, timeout_s=None, callback_url=""):
        prompt.append(brief + "\nNATIVE RESEARCH-ONLY ENTRY: Do not build, deploy, "
                      "publish, run assessment, send messages, or dispatch another cycle. "
                      "Write only the commissioned evidence/artifacts and exact receipt.\n")
        return {"job_id": "native-" + hashlib.sha256(brief.encode()).hexdigest()[:24],
                "status": "running"}

    kwargs["callback_url"] = ""
    with ledger_lock(kwargs.get("state_path")):
        record = _dispatch(**kwargs, bridge_post=reserve, entry="native")
    if not record.get("dispatched"):
        return record
    if sink():
        _sink_write(prompt[0], record["model"], record["budget_s"],
                    record["provider"], record["cycle"])
        return record
    terminal = (native_runner or native_hermes_chat)(prompt[0], record)
    # Only this exact terminal process receipt may release the reservation.
    if (terminal.get("job_id") != record["job_id"]
            or terminal.get("status") not in {"done", "error"}
            or not terminal.get("finished_utc") or terminal.get("rc") is None):
        raise ValueError("native terminal receipt missing or mismatched; cycle remains in flight")
    path = native_job_path(record)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(terminal, indent=2) + "\n")
    os.replace(tmp, path)
    result = ingest(record["provider"], state_path=kwargs.get("state_path"))
    with ledger_lock(kwargs.get("state_path")):
        current = latest_cycle(load_state(kwargs.get("state_path")), record["provider"])
        return {**record, **current, "ingest": result}


def native_job_path(record):
    return Path(record["authoring_receipt"] + ".native-job.json")


def native_hermes_chat(prompt, record):
    """Run once, enforce the hard budget on the whole process group, reap first."""
    query = Path(record["authoring_receipt"] + ".prompt.txt")
    query.write_text(prompt)
    log = Path(record["authoring_receipt"] + ".native.log")
    budget = min(2700, int(record["budget_s"]))
    command = ["hermes", "chat", "--query-file", str(query), "--oneshot", "--cli",
               "--model", record["model"], "--reasoning", record["reasoning"],
               "--max-turns", str(record["max_turns"]), "--run-budget", str(budget)]
    terminal = {"job_id": record["job_id"], "timed_out": False,
                "receipt_path": str(native_job_path(record))}
    with log.open("w+b") as output:
        try:
            process = subprocess.Popen(command, cwd=BUSINESS, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                rc = process.wait(timeout=budget)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                rc = process.wait()
                terminal["timed_out"] = True
        except OSError as exc:
            rc = 127
            terminal["error"] = str(exc)
        output.flush()
        output.seek(max(0, output.tell() - 12000))
        terminal["stdout_tail"] = output.read().decode("utf-8", "replace")
    terminal.update(status="done" if rc == 0 else "error", rc=rc, finished_utc=utc_now())
    return terminal


def _dispatch(*, provider: str, receipt: dict, sheet_row: dict | None = None,
             release: str = "", run_id: str = "", callback_url: str = "",
             state: dict | None = None, bridge_post: Callable | None = None,
             now: dt.datetime | None = None, state_path: str | Path | None = None,
             fingerprints: dict | None = None, engine: dict | None = None,
             entry: str = "bridge", commissioning: dict | None = None) -> dict:
    """Send ONE authoring job. Returns the cycle record (never raises).

    A bridge that is down must degrade to an honest blocked receipt, not take
    the onboarding machine down with it."""
    state = load_state(state_path) if state is None else state
    verdict = may_dispatch(provider, receipt, state, now, entry=entry)
    if not verdict.get("ok"):
        return {"dispatched": False, "provider": provider, **verdict}

    cycle = int(verdict["cycle"])
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    # The engine is TOLD to write its receipt here, so the directory has to
    # exist before it is told. (Observed 2026-08-20: the ledger cited three
    # receipt paths under a directory nothing had created.) A filesystem that
    # refuses must not take the onboarding run down with it — the cycle still
    # dispatches and the missing receipt is then named honestly at ingest.
    try:
        receipt_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    receipt_file = receipt_path_for(provider, cycle, stamp)
    capture_root = capture_root_for(provider, cycle, run_id)
    prior = (prior_fingerprints(provider) if fingerprints is None else fingerprints)
    # The engine is chosen from THIS cycle's blocked steps, before the brief is
    # built, so the brief, the ledger record and the posted job can never name
    # three different contracts (defect 2026-08-20 #6).
    # De-duplicated in FIRST-SEEN (manifest) order: one step can now be named by
    # two rows — the checklist's own verdict and the wire-backlog row block — and
    # a ledger that says `taxonomy, taxonomy` reads like a bug in the ledger.
    blocked_steps = list(dict.fromkeys(
        b.get("step") for b in
        authoring_blocks(receipt.get("not_green") or [], provider)))
    eng = engine or engine_for(provider, state, now, blocked_steps=blocked_steps)

    brief = build_brief(provider=provider, cycle=cycle, run_id=run_id,
                        receipt=receipt, sheet_row=sheet_row, release=release,
                        receipt_path=receipt_file, capture_root=capture_root,
                        prior=prior, engine=eng)

    if commissioning is not None:
        brief += (
            "\nCOMMISSIONING CONTEXT (verified Linear child and existing reviewed brief):\n"
            + json.dumps({k: v for k, v in commissioning.items() if k != "brief_text"},
                         indent=2, ensure_ascii=False)
            + "\nREVIEWED COMMISSIONING BRIEF:\n" + commissioning["brief_text"]
            + "\nThis cycle is the commissioning child's delegated authoring-only continuation "
              "for the ONE provider and blocked steps above. The parent owns code repair, "
              "other providers, reconciliation and issue closure. Do not execute those parts "
              "of the enclosing brief or invent a separate provider issue. The enclosing "
              "brief is included for scope/review provenance, not permission to widen this cycle.\n"
        )

    poster = bridge_post
    if poster is None:
        poster = ((lambda prompt, model=None, timeout_s=None, callback_url="":
                   _sink_write(prompt, model or eng["model"], timeout_s or BUDGET_S,
                               provider, cycle, callback_url, engine=eng))
                  if sink() else poster_for(eng))
    try:
        res = poster(brief, eng["model"], BUDGET_S, callback_url) or {}
    except Exception as exc:  # noqa: BLE001
        res = {"error": str(exc)[:300]}

    started = utc_now(now)
    record = {
        "provider": provider,
        "commissioning": commissioning,
        "cycle": cycle,
        "pt_day": pt_day(now),
        "run_id": run_id,
        "release": release,
        "engine": eng["engine"],
        "model": eng["model"],
        "reasoning": eng.get("reasoning") or "",
        "engine_escalated": bool(eng.get("escalated")),
        "engine_reason": eng.get("why") or "",
        # WHICH POLICY chose this engine, recorded next to the engine itself.
        # The model is always Sol now, so the policy is what tells a reader
        # whether this cycle carried the heavy research turn budget or the
        # standard one — and those two facts mean different things about what
        # a failure here proves.
        "engine_policy": eng.get("policy") or POLICY_SOL_PRIMARY,
        "dispatch_class": eng.get("dispatch_class") or dispatch_class(blocked_steps),
        "max_turns": int(eng.get("max_turns") or MAX_TURNS),
        "toolset_profile": TOOLSET_PROFILE,
        "dispatched_utc": started,
        "deadline_utc": utc_now((now or dt.datetime.now(dt.timezone.utc))
                                + dt.timedelta(seconds=BUDGET_S)),
        "budget_s": BUDGET_S,
        "job_id": res.get("job_id"),
        "bridge_status": res.get("status") or ("error" if res.get("error") else ""),
        "callback_url_given": bool(callback_url),
        "authoring_receipt": receipt_file,
        "capture_root": capture_root,
        # The id the capture step stamps on every fetch receipt it writes. Both
        # sides derive it from the capture-root path, so it can never be a third
        # value nobody agreed to (asserted by test).
        "cycle_id": fetch_receipts.cycle_id_for_capture_root(capture_root),
        "machine_receipt": receipt.get("receipt_path") or "",
        "blocked_steps": blocked_steps,
        "prior_fingerprints": prior,
        "brief_chars": len(brief),
        "status": IN_FLIGHT if res.get("job_id") else DISPATCH_FAILED,
        "dispatch_error": res.get("error") or "",
        "checks": 0,
    }
    if record["status"] == DISPATCH_FAILED:
        # The cited receipt path must never be a path nothing will ever write.
        # A dispatch that never reached an engine has its OWN receipt, written
        # by the machine, saying exactly that.
        verdict_failure = classify_failure(record)
        record["failure_class"] = verdict_failure["failure_class"]
        record["failure_reason"] = verdict_failure["why"]
        persist_machine_receipt(record, reason=verdict_failure["why"], now=now)
    provider_record(state, provider).setdefault("cycles", []).append(record)
    save_state(state, state_path)
    return {"dispatched": bool(res.get("job_id")), **record,
            "why": verdict.get("why", ""), "reason_code": "dispatched"
            if res.get("job_id") else "dispatch_failed"}


# ---------------------------------------------------------------------------
# Ingest — what the engine produced, judged by the machine
# ---------------------------------------------------------------------------


def read_authoring_receipt(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def locate_authoring_receipt(record: dict) -> tuple[str, str]:
    """(path, how_found). The ledger's cited path first, then a bounded recovery glob.

    DEFECT 2026-08-20 #2: the ledger cited three receipts and `authoring/` was
    empty. The proximate cause was upstream (the bridge defaulted `max_turns` to
    1, so no engine cycle ever got far enough to write one), but the lane also
    had NO answer for "the engine wrote it somewhere near, or not at all". Both
    halves are closed here: a receipt for this provider+cycle sitting under a
    different stamp is adopted and re-cited, and a cycle with no engine receipt
    at all gets a machine-written one at the exact cited path (below), so the
    ledger can never again name a file that does not exist.

    Recovery is time-bound to this dispatch. Provider+cycle numbers repeat across
    runs, so an unbounded glob can adopt an older run's authored receipt and make
    the new cycle appear resolved without its newly written artifacts. JRNYS hit
    that exact failure: an 00:45 receipt was adopted for an 08:17 dispatch while
    the current cycle's pricing spec remained uncited."""
    dispatched_at = parse_utc(record.get("dispatched_utc")
                              or record.get("dispatched_at_utc"))
    cited = str(record.get("authoring_receipt") or "")
    if cited and Path(cited).is_file():
        try:
            cited_is_current = (
                not dispatched_at or Path(cited).stat().st_mtime >= dispatched_at
            )
        except OSError:
            cited_is_current = False
        if cited_is_current:
            return cited, "cited_path"
    provider = str(record.get("provider") or "")
    cycle = int(record.get("cycle") or 0)
    if not provider or not cycle:
        return "", "absent"
    if not dispatched_at:
        return "", "absent"
    try:
        matches = sorted((
            p for p in receipt_dir().glob(receipt_glob_for(provider, cycle))
            if p.is_file() and p.stat().st_mtime >= dispatched_at
        ), key=lambda p: p.stat().st_mtime)
    except OSError:
        matches = []
    if matches:
        return str(matches[-1]), "recovered_by_glob"
    return "", "absent"


def machine_receipt(record: dict, reason: str, now: dt.datetime | None = None) -> dict:
    """The receipt the MACHINE writes when the engine wrote none.

    It is labelled `authored_by: "machine"` and carries `verdict:
    "not_authored"` — it is a record of a cycle that produced nothing, never a
    stand-in for engine work. `ingest` refuses to read one as engine output."""
    blocked = list(record.get("blocked_steps") or [])
    engine = record.get("engine") or PRIMARY_ENGINE
    model = record.get("model") or PRIMARY_MODEL
    return {
        "schema": RECEIPT_SCHEMA,
        "authored_by": "machine",
        "written_by": "glp_onboarding_authoring_dispatch",
        "written_utc": utc_now(now),
        "provider": record.get("provider") or "",
        "cycle": record.get("cycle"),
        "pt_day": record.get("pt_day") or "",
        "run_id": record.get("run_id") or "",
        "engine": engine,
        "model": model,
        "reasoning": record.get("reasoning") or "",
        "engine_escalated": bool(record.get("engine_escalated")),
        # The policy that chose the engine, on the receipt itself — so a receipt
        # read on its own says whether opus was the primary for a research
        # packet or an escalation after Sol could not finish (defect #6).
        "engine_policy": record.get("engine_policy") or POLICY_SOL_PRIMARY,
        "dispatch_class": record.get("dispatch_class")
        or dispatch_class(record.get("blocked_steps") or ()),
        "max_turns": record.get("max_turns"),
        "job_id": record.get("job_id") or "",
        "job": record.get("job") or {},
        "budget_s": record.get("budget_s") or BUDGET_S,
        "dispatched_utc": record.get("dispatched_utc") or "",
        "verdict": "not_authored",
        "failure_class": record.get("failure_class") or "",
        "fit_verdict": "unknown",
        "fit_evidence": [],
        "artifacts_written": [],
        "captures": [],
        "steps_addressed": [],
        "steps_not_addressed": [{"step": s, "why": reason} for s in blocked],
        "tests_run": [],
        "guardrails_touched": False,
        "operator_card": {
            "what_happened": ("The site's onboarding machine asked its authoring engine "
                              "(%s / %s) to research %s and the engine came back with "
                              "nothing usable." % (engine, model,
                                                   record.get("provider") or "a provider")),
            "what_it_means": ("That provider still cannot be published, so it still earns "
                              "nothing. The machine recorded why and moved on — it did not "
                              "publish anything unverified."),
            "ask": "",
        },
        "notes": reason,
    }


def persist_machine_receipt(record: dict, reason: str,
                            now: dt.datetime | None = None) -> str:
    """Write the machine's own receipt at the ledger-cited path. Never
    overwrites — an engine receipt always wins, and a re-ingest never rewrites
    history. Returns the path written, or "" if nothing was written."""
    path = Path(str(record.get("authoring_receipt") or ""))
    if not str(path) or str(path) == ".":
        return ""
    if str(record.get("job_id") or "").startswith("native-"):
        path = Path(str(path) + ".machine.json")
    if path.exists():
        return ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(machine_receipt(record, reason, now),
                                  indent=2, sort_keys=True, default=str) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        record["authoring_receipt_write_error"] = "%s: %s" % (type(exc).__name__, exc)
        return ""
    if str(record.get("job_id") or "").startswith("native-"):
        record["machine_receipt"] = str(path)
    else:
        record["authoring_receipt_written_by"] = "machine"
    return str(path)


def latest_cycle(state: dict, provider: str) -> dict | None:
    cycles = provider_record(state, provider).get("cycles") or []
    return cycles[-1] if cycles else None


def resolved_authoring_handoff(provider: str, state: dict | None = None,
                                state_path: str | Path | None = None) -> dict:
    """Read the newest usable completed authoring receipt without mutating state.

    ``onboard`` is a separate workflow entry from ``authoring_resume``. The
    latter adjudicates an in-flight callback with :func:`ingest`; the former
    must be able to consume that already-resolved result on a later fire. This
    adapter returns the same fields the build-ready verifier consumes. A newer
    terminal capability/dispatch failure may not erase an older resolved
    authored+fit receipt: the downstream verifier rebinds every current artifact
    before build. In-flight, refused-not-fit, or authored-but-failed latest
    cycles still fail closed and are never bypassed.
    """
    state = load_state(state_path) if state is None else state
    provider_state = ((state.get("providers") or {}).get(provider) or {})
    cycles = provider_state.get("cycles") or []
    if not cycles:
        return {
            "provider": provider,
            "ingested": False,
            "handoff_candidate": False,
            "reason_code": "no_cycle",
            "why": "no prior authoring cycle recorded for %s" % provider,
        }

    def inspect(record: dict) -> dict:
        found, how = locate_authoring_receipt(record)
        agent = read_authoring_receipt(found) if found else {}
        if str(agent.get("authored_by") or "").casefold() == "machine":
            agent = {}
        verdict = str(agent.get("verdict") or record.get("agent_verdict") or "")
        fit_verdict = str(agent.get("fit_verdict")
                          or record.get("agent_fit_verdict") or "unknown")
        return {
            "record": record,
            "found": found,
            "how": how,
            "agent": agent,
            "verdict": verdict,
            "fit_verdict": fit_verdict,
            "candidate": (
                verdict.casefold() == "authored"
                and fit_verdict.casefold() == "fit"
            ),
            "status": str(record.get("status") or ""),
            "readable": bool(agent),
        }

    latest = inspect(cycles[-1])
    selected = latest
    newer_terminal_failure = None
    latest_is_unusable_terminal_failure = (
        latest["status"] in {FAILED, DISPATCH_FAILED}
        and not latest["candidate"]
    )
    if latest_is_unusable_terminal_failure:
        for older_record in reversed(cycles[:-1]):
            older = inspect(older_record)
            if (
                older["status"] == RESOLVED
                and older["candidate"]
                and older["readable"]
            ):
                selected = older
                newer_terminal_failure = {
                    "cycle": latest["record"].get("cycle"),
                    "status": latest["status"],
                    "failure_class": latest["record"].get("failure_class"),
                    "authoring_receipt": (
                        latest["found"]
                        or str(latest["record"].get("authoring_receipt") or "")
                    ),
                }
                break

    record = selected["record"]
    found = selected["found"]
    how = selected["how"]
    agent = selected["agent"]
    verdict = selected["verdict"]
    fit_verdict = selected["fit_verdict"]
    candidate = selected["candidate"]
    status = selected["status"]
    readable = selected["readable"]
    ingested = candidate and readable and status == RESOLVED
    cited = found or str(record.get("authoring_receipt") or "")

    if not candidate:
        why = ("latest authoring cycle is not an authored+fit handoff "
               "(status=%s, verdict=%s, fit=%s)" %
               (status or "unknown", verdict or "missing", fit_verdict or "missing"))
    elif status != RESOLVED:
        why = "latest authored+fit cycle is %s, not resolved" % (status or "unknown")
    elif not readable:
        why = "latest authored+fit cycle cites no readable engine receipt: %s" % (
            cited or "(unset)")
    elif newer_terminal_failure:
        why = (
            "prior resolved authored+fit receipt recovered after a newer terminal "
            "capability/dispatch failure; current staged-input verification remains mandatory"
        )
    else:
        why = "latest resolved authored+fit receipt recovered for onboard build handoff"

    return {
        "provider": provider,
        "cycle": record.get("cycle"),
        "status": status,
        "ingested": ingested,
        "handoff_candidate": ingested,
        "reason_code": "resolved_receipt" if ingested else "handoff_unavailable",
        "agent_verdict": verdict,
        "agent_fit_verdict": fit_verdict,
        "artifacts_written": list(agent.get("artifacts_written")
                                  or record.get("artifacts_written") or [])[:20],
        "steps_addressed": list(agent.get("steps_addressed") or [])[:20],
        "steps_not_addressed": list(agent.get("steps_not_addressed") or [])[:20],
        "authored_block_set": list(record.get("blocked_steps") or []),
        "authoring_receipt": cited,
        "resolved_from": how,
        "why": why,
        **({"newer_terminal_failure": newer_terminal_failure}
           if newer_terminal_failure else {}),
    }


def ingest(provider: str, state: dict | None = None,
           now: dt.datetime | None = None, job_status: Callable | None = None,
           state_path: str | Path | None = None) -> dict:
    with ledger_lock(state_path):
        state = load_state(state_path) if state is None else state
        record = latest_cycle(state, provider)
        if record and str(record.get("job_id") or "").startswith("native-"):
            try:
                job = json.loads(native_job_path(record).read_text())
            except (OSError, ValueError):
                job = {}
            if (job.get("job_id") != record["job_id"]
                    or job.get("status") not in {"done", "error"}
                    or not job.get("finished_utc") or job.get("rc") is None):
                return {"ingested": False, "reason_code": "in_flight",
                        "status": IN_FLIGHT, "why": "exact native terminal receipt absent"}
            job_status = lambda job_id: job
        return _ingest(provider, state, now, job_status, state_path)


def _ingest(provider: str, state: dict | None = None,
           now: dt.datetime | None = None, job_status: Callable | None = None,
           state_path: str | Path | None = None) -> dict:
    """Adjudicate the in-flight cycle for one provider, BEFORE re-assessment.

    Three outcomes, and only three:
      * `not_fit`   — the engine's evidence says this provider does not belong.
                      Recorded as a persistent stop. The machine refuses. Correct.
      * `reused`    — the engine cited a capture that already existed on disk.
                      The cycle FAILS: a clean-room violation is not an artifact.
      * `ingested`  — artifacts exist (or do not); either way the machine now
                      re-assesses and the manifest decides. The engine's own
                      `verdict: "authored"` proves nothing on its own.
    """
    state = load_state(state_path) if state is None else state
    record = latest_cycle(state, provider)
    if not record:
        return {"ingested": False, "reason_code": "no_cycle",
                "why": "no authoring cycle recorded for %s" % provider}
    if record.get("status") != IN_FLIGHT:
        return {"ingested": False, "reason_code": "not_in_flight",
                "why": "latest cycle for %s is %s" % (provider, record.get("status")),
                "cycle": record.get("cycle"), "status": record.get("status")}

    job: dict = {}
    if record.get("job_id"):
        try:
            job = (job_status or bridge_job_status)(record["job_id"]) or {}
        except Exception as exc:  # noqa: BLE001
            job = {"error": str(exc)[:200]}

    found, how = locate_authoring_receipt(record)
    agent = read_authoring_receipt(found) if found else {}
    if str(agent.get("authored_by") or "").casefold() == "machine":
        # The machine's own stub from an earlier ingest is NOT engine output.
        agent = {}
    # A sibling callback is not proof that this job finished. Missing output,
    # even after the local deadline, cannot release a live bridge reservation.
    terminal = bool(record.get("job_id")) and job.get("job_id", record.get("job_id")) == record.get("job_id") and (
        (job.get("finished_utc") not in (None, "") and job.get("rc") not in (None, ""))
        or str(job.get("status") or "").casefold() in {"error", "failed", "timeout"}
        or (str(job.get("status") or "").casefold() in {"done", "finished"} and job.get("rc") not in (None, ""))
    )
    if not agent and not terminal:
        return {"ingested": False, "reason_code": "still_running", "status": IN_FLIGHT,
                "provider": provider, "cycle": record.get("cycle"),
                "why": "exact terminal bridge receipt absent; cycle remains in flight"}

    record["checks"] = int(record.get("checks") or 0) + 1
    record["last_check_utc"] = utc_now(now)
    record["job"] = {k: job.get(k) for k in
                     ("status", "rc", "finished_utc", "receipt_path", "error",
                      "stdout_tail", "timed_out") if k in job}
    if agent and how == "recovered_by_glob":
        # Re-cite what actually exists: a ledger that points at a file nobody
        # wrote is the defect this closes.
        record["authoring_receipt_dispatched"] = record.get("authoring_receipt")
        record["authoring_receipt"] = found
        record["authoring_receipt_resolved_from"] = how
    record["agent_verdict"] = agent.get("verdict") or ""
    record["agent_fit_verdict"] = (agent.get("fit_verdict") or "unknown")
    record["agent_card"] = agent.get("operator_card") or {}
    record["guardrails_touched"] = bool(agent.get("guardrails_touched"))
    record["artifacts_written"] = list(agent.get("artifacts_written") or [])[:20]

    expired = parse_utc(utc_now(now)) >= parse_utc(record.get("deadline_utc"))

    # --- the fit gate stays the MACHINE's ---------------------------------
    if str(record["agent_fit_verdict"]).strip().casefold() == "not_fit":
        record["status"] = REFUSED
        record["outcome_note"] = (
            "authoring evidence landed not_fit — the machine records the verdict and "
            "STOPS this provider; nothing is deployed")
        state.setdefault("not_fit", {})[provider] = {
            "provider": provider,
            "recorded_utc": utc_now(now),
            "cycle": record.get("cycle"),
            "evidence": list(agent.get("fit_evidence") or [])[:8],
            "authoring_receipt": record.get("authoring_receipt"),
            "operator_card": agent.get("operator_card") or {},
        }
        save_state(state, state_path)
        return {"ingested": True, "reason_code": "not_fit", "stop_provider": True,
                "provider": provider, "cycle": record.get("cycle"),
                "why": record["outcome_note"],
                "fit_evidence": list(agent.get("fit_evidence") or [])[:8],
                "operator_card": agent.get("operator_card") or {}}

    # --- clean room -------------------------------------------------------
    # The receipts THIS cycle's own capture step wrote. A capture that carries
    # one is first-party output of this cycle even when the page it fetched has
    # not changed since the last cycle — which is the normal case for a stable
    # provider and used to fail every honest cycle (Blocker B).
    fetched = cycle_fetch_receipts(record)
    record["fetch_receipts_this_cycle"] = len(fetched)
    rejected = []
    try:
        rejected = fetch_receipts.receipt_rejections(
            str(record.get("capture_root") or ""),
            str(record.get("cycle_id") or ""),
            str(record.get("dispatched_utc") or ""))
    except Exception:  # noqa: BLE001
        rejected = []
    if rejected:
        # An invalid receipt is never silently ignored: it is the shape a forged
        # one takes, and the reason it was refused belongs in the ledger.
        record["fetch_receipts_rejected"] = rejected[:20]
    reused = reused_prior_artifacts(agent, record.get("prior_fingerprints") or {},
                                    fetched)
    if reused:
        record["status"] = FAILED
        record["reused_prior_artifacts"] = reused
        record["outcome_note"] = (
            "clean-room violation: %d cited capture(s) already existed on disk before "
            "this job started AND carry no valid fetch receipt from this cycle, so "
            "they are not first-party output of this cycle" % len(reused))
        # A clean-room violation is an EVIDENCE failure, not a capability one:
        # the engine was perfectly able to work, it just did not capture. A
        # bigger contract buys nothing and the next cycle stays on the primary.
        record["failure_class"] = EVIDENCE_GAP
        record["failure_reason"] = record["outcome_note"]
        save_state(state, state_path)
        return {"ingested": True, "reason_code": "reused_prior_artifacts",
                "provider": provider, "cycle": record.get("cycle"),
                "reused": reused, "why": record["outcome_note"]}

    if not agent and expired:
        record["status"] = FAILED
        record["outcome_note"] = (
            "the %d-minute authoring budget expired and no receipt was written"
            % (BUDGET_S // 60))
        _record_failure(record, agent, job, now)
        save_state(state, state_path)
        return {"ingested": True, "reason_code": "budget_expired",
                "provider": provider, "cycle": record.get("cycle"),
                "failure_class": record.get("failure_class"),
                "authoring_receipt": record.get("authoring_receipt"),
                "why": record["outcome_note"]}

    record["status"] = RESOLVED if agent else FAILED
    record["outcome_note"] = (
        "authoring receipt read (%s); the machine re-assesses now — the engine's own "
        "verdict proves nothing" % (agent.get("verdict") or "no verdict")
        if agent else
        "the authoring job produced no receipt at `%s`; nothing to ingest"
        % (record.get("authoring_receipt") or "(unset)"))
    if agent:
        # Even a RESOLVED cycle records why it failed to author, if it did:
        # `not_authored` with an evidence gap must not escalate, and a
        # `not_authored` that hit a turn ceiling must.
        outcome = classify_failure(record, agent, job)
        record["failure_class"] = outcome["failure_class"]
        record["failure_reason"] = outcome["why"]
        record["engine_recorded_in_receipt"] = agent.get("engine") or ""
    else:
        _record_failure(record, agent, job, now)
    save_state(state, state_path)
    return {"ingested": bool(agent), "reason_code": "receipt" if agent else "no_receipt",
            "provider": provider, "cycle": record.get("cycle"),
            "agent_verdict": agent.get("verdict") or "",
            "agent_fit_verdict": agent.get("fit_verdict") or "unknown",
            "artifacts_written": record["artifacts_written"],
            "steps_addressed": list(agent.get("steps_addressed") or [])[:20],
            "steps_not_addressed": list(agent.get("steps_not_addressed") or [])[:20],
            # S is the exact block set that commissioned this cycle. The
            # manifest compares it with the post-build regrade so an authored
            # S -> same builder-owned S transition can never dispatch S again.
            "authored_block_set": list(record.get("blocked_steps") or []),
            "operator_card": (agent.get("operator_card") or {}) if agent else
                             machine_receipt(record, record["outcome_note"],
                                             now)["operator_card"],
            "failure_class": record.get("failure_class"),
            "authoring_receipt": record.get("authoring_receipt"),
            "why": record["outcome_note"]}


def _record_failure(record: dict, agent: dict, job: dict,
                    now: dt.datetime | None) -> None:
    """Classify a failed cycle and make sure its cited receipt exists on disk."""
    outcome = classify_failure(record, agent, job)
    record["failure_class"] = outcome["failure_class"]
    record["failure_reason"] = outcome["why"]
    if not agent:
        persist_machine_receipt(
            record,
            reason="%s — %s" % (record.get("outcome_note") or "the cycle failed",
                                outcome["why"]),
            now=now)


# ---------------------------------------------------------------------------
# Escalation — the ladder, never a silent stop
# ---------------------------------------------------------------------------

ESCALATION_WORKFLOW = "8lnjTRUBrG7zCgPn"          # glp/onboarding-manifest
ERROR_CASES_TABLE = os.environ.get("GLP_AUTHORING_CASES_TABLE", "jwrG1kHGYUIcOPWG")
ESCALATION_NODE = "authoring_dispatch"

#: The `error_cases` datatable's REAL columns. n8n rejects an insert carrying a
#: key that is not one of them, and `ops/n8n_dt.insert` returns that rejection
#: as a normal dict — it does not raise. So the cap escalation reported
#: `routed: True` while the relay had answered
#: `{"success": false, "error": "unknown column name 'node'"}` and NOTHING was
#: written (VERIFIED 2026-08-20: wellmedr-review's cap case was absent from all
#: 41 rows of the table). Two fixes, both here: project the row onto these
#: columns, and VERIFY the row by reading it back before claiming it landed.
CASE_COLUMNS = ("case_id", "signature", "source", "workflow_id", "execution_id",
                "error_text", "status", "attempts_today", "first_seen_utc",
                "last_update_utc", "receipt")


def _fnv1a64(raw: str) -> str:
    """FNV-1a 64, byte-identical to `ops/error-fixer/fixer_runtime.fnv1a64` and
    to ops/error-handler's Format node. Copied rather than imported so this lane
    does not drag the fixer runtime into every onboarding run; a test asserts
    the two implementations agree."""
    h = 0xCBF29CE484222325
    for byte in raw.encode("utf-8"):
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h


#: Byte-identical to `fixer_runtime._NOISE`, in the same order — the collapse is
#: half of the signature, so a divergence here is a silent dedupe failure.
#: Pinned by `test_case_signature_matches_the_fixer_runtime`.
_CASE_NOISE = (
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<ts>"),
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d{3,}\b"), "<n>"),
    (re.compile(r"0x[0-9a-f]+", re.I), "<addr>"),
    (re.compile(r"/tmp/\S+"), "<tmp>"),
)


def case_signature(workflow_id: str, node: str, error_text: str) -> str:
    """Stable id for "this defect", blind to counts and timestamps, so the same
    provider capping out twice collides in the fixer's attempt ledger instead of
    minting a fresh case every day."""
    head = (error_text or "").strip().splitlines()
    head = head[0] if head else ""
    for pattern, repl in _CASE_NOISE:
        head = pattern.sub(repl, head)
    head = " ".join(head.split())[:200].lower()
    return _fnv1a64("|".join([(workflow_id or "").strip(), (node or "").strip(), head]))


def case_row(case: dict) -> dict:
    """The insertable projection of a case. Anything not a real column is
    metadata for us, not for n8n."""
    return {k: case[k] for k in CASE_COLUMNS if k in case}


def escalation_case(provider: str, receipt: dict, state: dict,
                    now: dt.datetime | None = None) -> dict:
    """The `error_cases` row that hands this to ops/error-handler's ladder.

    Written in the fixer's own vocabulary so its tier logic reads it normally:
    the tier-2 rung then dispatches a full-capability agent at the REAL blocker,
    which by construction is no longer 'nobody authored the content'."""
    cycles = cycles_today(state, provider, now)
    not_green = receipt.get("not_green") or []
    blocks = authoring_blocks(not_green, provider)
    machine_blocks = non_authoring_blocks(not_green, provider)
    build_ready = [
        row for row in not_green
        if isinstance(row, dict)
        and row.get("provider") in (provider, "", None)
        and row.get("path") == BUILD_READY_PATH
    ]
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%d%H%M%S")
    # The engine AND the policy that chose it. The ladder's next rung needs to
    # know which TURN BUDGET each cycle already ran under (heavy research vs
    # standard) — a failure on the heavy budget means the defect is not "give
    # it more turns", and the fixer must not spend a rung rediscovering that.
    engines = ", ".join(
        "c%s %s/%s [%s] -> %s%s" % (
            c.get("cycle"), c.get("engine") or "?", c.get("model") or "?",
            c.get("engine_policy") or POLICY_SOL_PRIMARY,
            c.get("status") or "?",
            (" (%s)" % c["failure_class"]) if c.get("failure_class") else "")
        for c in cycles[-MAX_CYCLES_PER_DAY:]) or "(no cycles recorded)"
    # The blocks are re-read at CAP time, not at dispatch time, and they can have
    # changed owner in between (they did on 2026-08-20, when logo_pipeline was
    # reclassified machine-owned). Name both sets, so the ladder is handed the
    # provider's real remaining blockers rather than an empty authoring list.
    if build_ready:
        error_text = (
            "onboarding-manifest: %s returned authored + fit and its staged build inputs "
            "verified, but the B2 build trigger did not clear these BUILD-READY steps: %s. "
            "The sealed predecessor was not mistaken for missing authoring and no new "
            "authoring cycle was dispatched. Machine-owned build errors/steps: %s. "
            "Authoring receipt(s): %s. Nothing was deployed and no gate was loosened."
            % (provider,
               ", ".join(b.get("step") or "?" for b in build_ready),
               ", ".join(b.get("step") or "?" for b in machine_blocks)
               or "(none named)",
               ", ".join(str(c.get("authoring_receipt") or "")
                         for c in cycles[-3:])))
    else:
        error_text = (
            "onboarding-manifest: %s is still not deployable after %d/%d authoring "
            "cycles today. Blocked authoring steps: %s. Blocked MACHINE-OWNED steps: %s. "
            "The machine dispatched its own authoring engine each cycle (%s; receipts: %s) "
            "and the blocks did not clear, so authoring is NOT the blocker — the defect is "
            "upstream of content. Nothing was deployed and no gate was loosened."
            % (provider, len(cycles), MAX_CYCLES_PER_DAY,
               ", ".join(b.get("step") or "?" for b in blocks) or "(none named)",
               ", ".join(b.get("step") or "?" for b in machine_blocks)
               or "(none named)", engines,
               ", ".join(str(c.get("authoring_receipt") or "")
                         for c in cycles[-3:])))
    text = error_text[:1800]
    return {
        "case_id": "ec-%s-authoring-%s" % (stamp, provider[:24]),
        "signature": case_signature(ESCALATION_WORKFLOW, ESCALATION_NODE, text),
        "source": ("onboarding_manifest_build_ready"
                   if build_ready else "onboarding_manifest_authoring_cap"),
        "workflow_id": ESCALATION_WORKFLOW,
        "execution_id": "",
        "node": ESCALATION_NODE,          # metadata for us; NOT a table column
        "error_text": text,
        "status": "new",
        "attempts_today": 0,
        "first_seen_utc": utc_now(now),
        "last_update_utc": utc_now(now),
        "receipt": str(receipt.get("receipt_path") or "")[:400],
    }


def _default_case_insert(table_id: str, row: dict) -> dict:
    import sys  # noqa: PLC0415
    sys.path.insert(0, str(BUSINESS / "ops"))
    import n8n_dt  # noqa: PLC0415
    # ALLOWLIST ESCAPE (ops/n8n_dt.REFUSAL_INSPECTORS). A `success: false` RAISES
    # for every other caller, because a refusal returned as data is how this lane
    # reported a queued case that never existed. Here the refusal is the thing we
    # need to READ: `route_to_error_handler` turns it into `routed: false` with
    # the relay's own words attached, which is a better red card than a traceback.
    return n8n_dt.insert(table_id, row, allow_refusal=True)


def _default_case_verify(table_id: str, case_id: str) -> bool:
    import sys  # noqa: PLC0415
    sys.path.insert(0, str(BUSINESS / "ops"))
    import n8n_dt  # noqa: PLC0415
    return bool(n8n_dt.rows(table_id, n8n_dt.eq("case_id", case_id), limit=5))


def route_to_error_handler(provider: str, receipt: dict, state: dict,
                           now: dt.datetime | None = None,
                           insert: Callable | None = None,
                           verify: Callable | None = None) -> dict:
    """Insert the case AND prove it landed. Never raises — a datatable that is
    down must not swallow the red card, so the failure itself is returned and
    named.

    `routed: True` now means a row with this `case_id` was read back out of the
    table. It used to mean "the insert call returned", which is how a rejected
    write got reported as a queued case.
    """
    case = escalation_case(provider, receipt, state, now)
    row = case_row(case)
    live = insert is None
    if insert is None:
        insert = _default_case_insert
    if verify is None and live:
        verify = _default_case_verify

    def out(routed: bool, **extra) -> dict:
        return {"routed": routed, "case_id": case["case_id"],
                "table": ERROR_CASES_TABLE, "signature": case["signature"],
                "error_text": case["error_text"], "row_keys": sorted(row),
                **extra}

    try:
        res = insert(ERROR_CASES_TABLE, row)
    except Exception as exc:  # noqa: BLE001
        return out(False, error="%s: %s" % (type(exc).__name__, str(exc)[:200]))

    if isinstance(res, dict) and res.get("success") is False:
        return out(False, verified=False,
                   error="datatable refused the insert: %s"
                         % str(res.get("error") or json.dumps(res)[:200]))
    if verify is None:
        return out(True, verified=None)
    try:
        found = bool(verify(ERROR_CASES_TABLE, case["case_id"]))
    except Exception as exc:  # noqa: BLE001
        return out(False, verified=False,
                   error="insert reported success but could not be verified: %s: %s"
                         % (type(exc).__name__, str(exc)[:200]))
    if not found:
        return out(False, verified=False,
                   error="insert reported success but no row with case_id %s is in "
                         "table %s" % (case["case_id"], ERROR_CASES_TABLE))
    return out(True, verified=True)


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


def dispatch_card_line(record: dict) -> str:
    if record.get("dispatched"):
        return ("🤖 Authoring engine dispatched by the machine (cycle %s/%s on `%s`/`%s`%s, "
                "%d-minute budget, job `%s`) for `%s` — blocked steps: %s. Engine policy: "
                "`%s` (%s class). The machine re-assesses automatically when it returns; "
                "no human is in this loop."
                % (record.get("cycle"), MAX_CYCLES_PER_DAY,
                   record.get("engine") or PRIMARY_ENGINE,
                   record.get("model") or PRIMARY_MODEL,
                   " ESCALATED" if record.get("engine_escalated") else "",
                   BUDGET_S // 60,
                   record.get("job_id"), record.get("provider"),
                   ", ".join(record.get("blocked_steps") or []) or "(none)",
                   record.get("engine_policy") or POLICY_SOL_PRIMARY,
                   record.get("dispatch_class") or "light"))
    code = record.get("reason_code") or "not_dispatched"
    return "🤖 Authoring NOT dispatched (%s): %s" % (code, record.get("why") or "")


def refusal_card(provider: str, stop: dict) -> str:
    evidence = "\n".join("  • " + str(e)[:200] for e in (stop.get("evidence") or [])[:5])
    card = (stop.get("operator_card") or {})
    return ("⛔ **onboarding-manifest** — `%s` STOPPED as not-fit.\n"
            "The authoring engine's own evidence says this provider does not belong on "
            "this site, so the machine recorded the verdict and refused to deploy it. "
            "That is the gate working, not a failure.\n%s%s"
            % (provider, (evidence + "\n") if evidence else "",
               ("What it means: " + str(card.get("what_it_means"))[:300])
               if card.get("what_it_means") else ""))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Commission one authoring-only cycle; no deployment")
    parser.add_argument("--entry", choices=("bridge", "native"), default="bridge")
    parser.add_argument("--provider", required=True, help="Exact governed Sheet SEO Slug")
    parser.add_argument("--sheet-row", type=int, help="Disambiguate a provider carried on multiple rails")
    parser.add_argument("--step", required=True, choices=sorted(RESEARCH_PACKET_CLASS))
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.provider):
        parser.error("provider must be a canonical SEO slug")
    # Read-only identity binding. Never run autowire assessment or deployment.
    import glp_research_autowire as autowire
    values = autowire.read_sheet_values()
    matches = [autowire.event_from_row(i, row) for i, row in enumerate(values[1:], 2)
               if str(dict(zip(autowire.sheet_writer.HEADERS,
                               autowire.sheet_writer.pad(row))).get("SEO Slug") or "").strip()
               == args.provider and (args.sheet_row is None or i == args.sheet_row)]
    if len(matches) != 1:
        parser.error("provider must bind exactly one governed Sheet row")
    event = matches[0]
    identity = autowire.research_network_program_identity(event["sheet_row"], values[event["sheet_row"] - 1])
    event.update(identity)
    receipt = {"not_green": [{"provider": args.provider, "step": args.step,
                              "status": "blocked", "detail": "operator-commissioned authoring evidence"}]}
    result = dispatch(entry=args.entry, provider=args.provider, receipt=receipt,
                      sheet_row=autowire.authoring_sheet_row(event),
                      release="research-only-no-release",
                      run_id=f"research-autowire-{event['approval_event_id'][:24]}")
    print(json.dumps({"ledger_path": str(STATE_PATH),
                      "cycle_receipt": result.get("authoring_receipt"),
                      "ledger_entry": result}, indent=2, default=str))
    return 0 if result.get("dispatched") and result.get("status") != FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
