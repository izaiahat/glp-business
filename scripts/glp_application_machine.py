#!/usr/bin/env python3
"""GLP Clinic Scout — operator-queued affiliate application drain.

Applications are never discovered or selected by this worker. The operator names
the network and exact program, supplies application instructions, and creates a
row in n8n's ``affiliate_application_queue``. This worker drains only those rows,
re-checks fit, dedupe and legality gates, then queues verified Sheet facts through
the governed single writer. An empty queue is healthy and completely silent.

What this file is NOT
---------------------
It is not a second browser stack and not a second application executor.  The
proven machinery already exists:

* ``scripts/marketplace_adapters/portal_apply.py`` — per-network executors
  (direct merchant forms, CJ, PartnerStack, Rakuten, Impact, Digistore24,
  GoAffPro, ClickBank, UpPromote, Awin, Katalys) riding the authenticated
  Chrome/CDP lane, with a fail-closed identity gate and a durable ledger.
* ``scripts/affiliate_niche_geo_filter.py`` — the niche/geo/GLP-fit choke.
* ``scripts/affiliate_sheet_batch_writer.py`` — the Sheet read path.

This machine is the part that was missing: it assembles a *candidate pool that
is actually GLP-fit and actually un-applied*, hands exactly those rows to the
existing executor, and turns each real submission into a `sheet_update_queue`
row so the one governed writer can record it.

Truthfulness gate
-----------------
Every application form gets only true statements, taken from
``config/affiliate-sites/glp-clinic-scout.json`` (Tilton Group LLC,
glpclinicscout.com, ops@glpclinicscout.com, organic website traffic, FTC
disclosure).  No traffic number, revenue figure or audience size is ever
invented; the executor's form mapper is fail-closed on any field it cannot
answer honestly.

Two routes, both counted
------------------------
`portal` is the browser form/Join executor above.  `email` is the second real
route, and it exists because a provider whose published intake IS "email us"
will fail a form filler forever with `application_form_not_found`.  Those rows
are stamped `application_route_next: email` by `portal_apply.next_route()`, and
this machine now CONSUMES them in the same run (see "The EMAIL route" below):
address resolved from the provider's own published pages, copy drafted and
truth-gated, alias re-verified, `.eml` receipt per send, one queue row each.
Before 2026-08-19 the routing existed and the consumption did not, so the daily
card read `paths: portal=0 email=0 api=0` while 12 providers sat routed and
un-emailed.  Queued is not sent.

Usage
-----
    python3 scripts/glp_application_machine.py --mode drain
    python3 scripts/glp_application_machine.py --mode plan

Exit code is 0 whenever the machine ran honestly, including an empty queue; a
non-zero code means the machine itself failed and should page.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BUSINESS = Path("/home/ubuntu/business")
SCRIPTS = BUSINESS / "scripts"
MACHINE_DIR = BUSINESS / "reports/glp-clinic-scout/affiliate-machine"
APPLICATIONS_DIR = MACHINE_DIR / "applications"
LEDGER_PATH = MACHINE_DIR / "applications-ledger.json"
QUEUED_INDEX = APPLICATIONS_DIR / "queued-index.json"
APPLICATION_QUEUE_TABLE = "wuDGU2Xg1LvwXuk6"
SHEET_UPDATE_QUEUE_TABLE = "hJiP3eJKBvtZ6bQU"
APPLICATION_MACHINE_LOCK = APPLICATIONS_DIR / ".application-machine.lock"
# The outer schema is the stable machine-output contract consumed by fleet
# liveness. The contract_schema records the current queue-drain payload shape so
# the 2026-08-24 queue-only conversion remains explicit instead of masquerading
# as the retired discovery/applier implementation.
RUN_RECEIPT_SCHEMA = "glp-affiliate-application-machine-run.v1"
RUN_RECEIPT_CONTRACT_SCHEMA = "glp-application-queue-drain-run-v1"

if str(BUSINESS) not in sys.path:
    sys.path.insert(0, str(BUSINESS))
from ops import affiliate_apply_gate as apply_gate  # noqa: E402


#: The interpreter that actually has Playwright.  The default python3 on this
#: box does not, and the Sheet client is not installed in that one - so the
#: machine reads the Sheet here and shells the submit phase over there.
RUNTIME_PYTHON = Path("/home/ubuntu/.hermes/venvs/glp-awin-runtime-r2/bin/python")

#: Networks this machine is allowed to submit through today.
#: Katalys is excluded by operator direction (portal login broken 2026-08-18).
#: Awin is excluded from THIS executor because Awin's Join is a modal on the
#: authenticated publisher portal, not a third-party application form - a
#: different mechanism, owned by `scripts/glp_awin_portal_join.py`. It is NOT
#: excluded because of TOTP: that blocker is disproven and cleared (2026-08-18,
#: two programmes joined live). Awin supply reaches the daily card through the
#: portal path, and the daily card breaks the counts out per path.
DEFAULT_NETWORKS = ("direct", "cj", "partnerstack", "rakuten", "impact",
                    "digistore24", "clickbank", "uppromote", "goaffpro")
EXCLUDED_NETWORKS = ("katalys", "awin")

#: Hosts that are a closed door regardless of how the candidate was labelled.
#: A Sheet row can name a direct provider while its Apply URL actually points at
#: a network we cannot use - applying there would burn a real submission on a
#: door we already know is shut, and would look like a success in the ledger.
BLOCKED_APPLICATION_HOSTS = {
    # operator direction 2026-08-18: Katalys portal login is broken, skip it
    "katalys.com": "katalys_portal_login_broken",
    # our publisher application was declined; re-submitting is not honest supply
    "flexoffers.com": "flexoffers_publisher_application_declined",
    "www.flexoffers.com": "flexoffers_publisher_application_declined",
}
# NOTE (2026-08-18): `ui.awin.com` / `www.awin.com` were in this table on the
# premise that Awin's Join control sat behind app-based TOTP. That premise is
# DISPROVEN. The operator removed Awin 2FA, `operator-blockers.json` already
# carried BLK-awin-totp as `verified_cleared`, and tonight the portal session
# was re-established and two GLP-fit programmes (HealthRX 129105, TempraMed
# 94359) were joined through the real Join control with the provider's own
# relationship flipping notjoined -> pending. Receipt:
# reports/glp-clinic-scout/affiliate-machine/awin-portal-join/apply-20260818/join.json
# Awin Joins run on the dedicated `scripts/glp_awin_portal_join.py` lane, which
# is why `awin` remains in EXCLUDED_NETWORKS for the FORM executor: that
# executor fills third-party application forms and has no Join-modal chain.

#: Sheet statuses that mean "this program has NOT been applied to yet".  Every
#: other status - applied, pending, approved, rejected, live, paused, blocked,
#: suppressed - is a dedupe hit.
UNAPPLIED_SHEET_STATUSES = {
    "", "ready_to_apply", "verify_affiliate_route",
    "provider_not_joined_reapply_decision_required",
}

#: Sheet `Vertical` values that are inside the operator's niche.  Sister
#: verticals (supplements, fitness equipment, skincare) get their own job.
GLP_VERTICALS = {"glp1-weightloss"}

#: Column indexes in the canonical Affiliate Applications tab.
COL_APPLY = 1
COL_PROGRAM = 2
COL_CATEGORY = 3
COL_NETWORK = 4
COL_APPLY_URL = 5
COL_OFFICIAL_URL = 6
COL_FIT_REASON = 7
COL_STATUS = 10
COL_PROGRAM_ID = 13
COL_NOTES = 23
COL_VERTICAL = 25


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_ACTIVE_RECEIPT_PATH: Path | None = None
_ACTIVE_RECEIPT_CONTEXT: dict[str, Any] = {}
_ACTIVE_RECEIPT_PHASE = "startup"
_ACTIVE_RECEIPT_TERMINAL = False


def _atomic_json_receipt(path: Path, payload: dict[str, Any]) -> None:
    """Replace one run receipt without exposing a half-written signal marker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _begin_run_receipt(path: Path, *, run_id: str, mode: str, phase: str) -> None:
    """Publish the active phase and retain enough context for a fatal handler."""
    global _ACTIVE_RECEIPT_PATH, _ACTIVE_RECEIPT_CONTEXT
    global _ACTIVE_RECEIPT_PHASE, _ACTIVE_RECEIPT_TERMINAL
    _ACTIVE_RECEIPT_PATH = path
    _ACTIVE_RECEIPT_CONTEXT = {
        "schema": RUN_RECEIPT_SCHEMA,
        "contract_schema": RUN_RECEIPT_CONTRACT_SCHEMA,
        "run_id": run_id,
        "mode": mode,
        "pid": os.getpid(),
    }
    _ACTIVE_RECEIPT_PHASE = phase
    _ACTIVE_RECEIPT_TERMINAL = False
    _atomic_json_receipt(path, {
        **_ACTIVE_RECEIPT_CONTEXT,
        "captured_at_utc": utc_now(),
        "status": "started",
        "phase": phase,
        "note": "non-terminal marker; a receipt left in this state means the "
                "process ended without a catchable signal or Python exception",
    })


def _finish_run_receipt(path: Path, payload: dict[str, Any]) -> None:
    global _ACTIVE_RECEIPT_TERMINAL
    _atomic_json_receipt(path, payload)
    if path == _ACTIVE_RECEIPT_PATH:
        _ACTIVE_RECEIPT_TERMINAL = True


def _stamp_termination(kind: str, **detail: Any) -> None:
    """Turn a STARTED receipt into evidence before the process exits."""
    global _ACTIVE_RECEIPT_TERMINAL
    if _ACTIVE_RECEIPT_PATH is None or _ACTIVE_RECEIPT_TERMINAL:
        return
    payload = {
        **_ACTIVE_RECEIPT_CONTEXT,
        "captured_at_utc": utc_now(),
        "status": "terminated" if kind == "signal" else "failed",
        "phase": _ACTIVE_RECEIPT_PHASE,
        "termination": {"kind": kind, **detail},
    }
    if _LAST_QUEUE_READ_OBSERVATIONS:
        payload["queue_read_observations"] = _LAST_QUEUE_READ_OBSERVATIONS
    try:
        _atomic_json_receipt(_ACTIVE_RECEIPT_PATH, payload)
        _ACTIVE_RECEIPT_TERMINAL = True
    except OSError as exc:
        # The bridge captures stderr and records negative signal return codes. If
        # disk instrumentation itself fails, leave a second independent witness.
        try:
            os.write(2, (f"application_machine termination_receipt_failed "
                         f"kind={kind} phase={_ACTIVE_RECEIPT_PHASE} "
                         f"error={type(exc).__name__}\n").encode())
        except OSError:
            pass


def _handle_termination_signal(signum: int, _frame: Any) -> None:
    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = f"SIGNAL_{signum}"
    exit_code = 128 + signum
    _stamp_termination(
        "signal", signal_number=signum, signal_name=signal_name, exit_code=exit_code)
    raise SystemExit(exit_code)


def _install_termination_handlers() -> dict[int, Any]:
    previous: dict[int, Any] = {}
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, _handle_termination_signal)
    return previous


def _restore_termination_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def authorized_lanes() -> set[str]:
    """The lane vocabulary the executor's operator-signed authority accepts.

    Read from the executor rather than restated here, so a lane this machine
    proposes can never drift away from the lane the gate will honour.
    """
    for path in (str(BUSINESS), str(SCRIPTS)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from scripts.marketplace_adapters.portal_apply import LANE_MAP  # noqa: E402
    return set(LANE_MAP)


AUTHORIZED_LANES: set[str] = set()


def _import_repo_modules():
    global AUTHORIZED_LANES
    AUTHORIZED_LANES = authorized_lanes()
    """Import the proven modules, from the repo root, once."""
    for path in (str(BUSINESS), str(SCRIPTS)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from scripts import affiliate_niche_geo_filter as nfilter  # noqa: E402
    return nfilter


def repo_relative(path: Path | str) -> str:
    text = str(path)
    root = str(BUSINESS) + "/"
    return text[len(root):] if text.startswith(root) else text


# ---------------------------------------------------------------------------
# Sheet: dedupe index + sheet-resident candidates
# ---------------------------------------------------------------------------


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def read_sheet() -> list[list[Any]]:
    """Live read of the governed Sheet through the existing auth path."""
    for path in (str(BUSINESS), str(SCRIPTS)):
        if path not in sys.path:
            sys.path.insert(0, path)
    import affiliate_sheet_batch_writer as writer  # noqa: E402
    from affiliate_sheet_schema_guard import EXPECTED_RANGE  # noqa: E402
    from googleapiclient.discovery import build  # noqa: E402

    svc = build("sheets", "v4", credentials=writer.creds(), cache_discovery=False)
    values = svc.spreadsheets().values().get(
        spreadsheetId=writer.SHEET_ID,
        range=f"'{writer.TAB}'!{EXPECTED_RANGE}",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute().get("values", [])
    if not values:
        raise RuntimeError("sheet_read_returned_no_rows")
    header = [str(cell).strip() for cell in values[0]]
    if header[:3] != ["Priority", "Apply?", "Program / Network"]:
        raise RuntimeError(f"sheet_header_unexpected:{header[:3]}")
    return values


def pad(row: list[Any], width: int = 27) -> list[Any]:
    row = list(row)
    return row + [""] * (width - len(row)) if len(row) < width else row


def sheet_index(values: list[list[Any]]) -> dict[str, Any]:
    """Split the Sheet into (a) the dedupe set and (b) un-applied candidates."""
    engaged_names: set[str] = set()
    engaged_ids: set[tuple[str, str]] = set()
    #: The full identity of every engaged row, for the CROSS-NETWORK legs of the
    #: shared dedupe (brand name, merchant domain, and the row's own relationship
    #: state). Name+network alone is what let an Awin application go out for
    #: HealthRX while the same brand was live on Katalys.
    engaged_rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}

    for row_number, raw in enumerate(values[1:], start=2):
        row = pad(raw)
        program = str(row[COL_PROGRAM]).strip()
        if not program:
            continue
        status = str(row[COL_STATUS]).strip()
        status_counts[status or "(blank)"] = status_counts.get(status or "(blank)", 0) + 1
        network = str(row[COL_NETWORK]).strip()
        program_id = str(row[COL_PROGRAM_ID]).strip()

        if status not in UNAPPLIED_SHEET_STATUSES:
            engaged_names.add(norm(program))
            if program_id:
                engaged_ids.add((norm(network), program_id.casefold()))
            engaged_rows.append({
                "name": program, "network": network, "status": status,
                "program_id": program_id, "sheet_row": row_number,
                "official_url": str(row[COL_OFFICIAL_URL]).strip(),
                "apply_url": str(row[COL_APPLY_URL]).strip(),
            })
            continue

        vertical = str(row[COL_VERTICAL]).strip().lower()
        apply_url = str(row[COL_APPLY_URL]).strip()
        candidates.append({
            "source": "sheet",
            "sheet_row": row_number,
            "name": program,
            "network_label": network,
            "sheet_vertical": vertical,
            "sheet_status": status,
            "sheet_apply_flag": str(row[COL_APPLY]).strip().upper() in ("TRUE", "1", "YES"),
            "application_url": apply_url,
            "official_url": str(row[COL_OFFICIAL_URL]).strip(),
            "category": str(row[COL_CATEGORY]).strip(),
            "fit_reason": str(row[COL_FIT_REASON]).strip(),
            "notes": str(row[COL_NOTES]).strip(),
        })
    return {
        "engaged_names": engaged_names,
        "engaged_ids": engaged_ids,
        "engaged_rows": engaged_rows,
        "candidates": candidates,
        "status_counts": status_counts,
        "row_count": len(values) - 1,
    }


def sheet_row_lookup(values: list[list[Any]]) -> dict[str, int]:
    """normalized program name -> sheet row, for queue-row addressing."""
    out: dict[str, int] = {}
    for row_number, raw in enumerate(values[1:], start=2):
        program = str(pad(raw)[COL_PROGRAM]).strip()
        if program:
            out.setdefault(norm(program), row_number)
    return out


# ---------------------------------------------------------------------------
# Candidate pools
# ---------------------------------------------------------------------------


def newest(pattern: str) -> Path | None:
    hits = sorted(MACHINE_DIR.glob(pattern), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def direct_supply_candidates() -> list[dict[str, Any]]:
    """Direct GLP telehealth providers that are on no network at all.

    The supply-discovery lane already harvests these with first-party evidence
    (`glp_evidence_tier`, the page the claim came from).  They are the pool the
    operator means by "direct GLP telehealth providers not on any network".
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    supply = newest("enumeration-supply-*/direct-supply.json")
    if supply is not None:
        try:
            doc = json.loads(supply.read_text())
        except (OSError, ValueError):
            doc = {}
        for program in doc.get("programs") or []:
            name = str(program.get("name") or "").strip()
            url = str(program.get("url") or "").strip()
            if not name or not url or norm(name) in seen:
                continue
            if str(program.get("relationship") or "").lower() not in ("", "notjoined"):
                continue
            seen.add(norm(name))
            tier = str(program.get("glp_evidence_tier") or "")
            out.append({
                "source": f"direct-supply:{repo_relative(supply)}",
                "name": name,
                "network": "direct",
                "program_id": str(program.get("program_id") or name),
                "application_url": url,
                "official_url": url,
                "region": str(program.get("region") or "US"),
                "currency": str(program.get("currency") or "USD"),
                "description": str(program.get("description") or "").strip() or tier,
                "evidence_tier": tier,
                "evidence_source": str(program.get("evidence_source") or ""),
            })

    programs = newest("supply-discovery/run-*/direct-programs.json")
    if programs is not None:
        try:
            doc = json.loads(programs.read_text())
        except (OSError, ValueError):
            doc = {}
        for program in doc.get("accepted") or []:
            name = str(program.get("provider") or program.get("title") or "").strip()
            url = str(program.get("program_url") or "").strip()
            if not name or not url or norm(name) in seen:
                continue
            seen.add(norm(name))
            evidence = program.get("glp_evidence") or {}
            out.append({
                "source": f"supply-discovery:{repo_relative(programs)}",
                "name": name,
                "network": "direct",
                "program_id": name,
                "application_url": url,
                "official_url": str(program.get("official_url") or url),
                "region": "US",
                "currency": "USD",
                "description": str(program.get("evidence_excerpt")
                                   or program.get("snippet") or "").strip()
                               or str(evidence.get("tier") or ""),
                "evidence_tier": str(evidence.get("tier") or ""),
                "evidence_source": str(program.get("evidence_source") or ""),
            })
    return out


def marketplace_candidates(networks: set[str]) -> list[dict[str, Any]]:
    """Un-joined programs from the freshest per-network API enumerations.

    The enumeration machine (`glp/marketplace-enumeration`) refreshes these
    daily; this reads the newest snapshot per network rather than re-hitting
    every API, so a marketplace outage degrades supply instead of failing the
    run.
    """
    for path in (str(BUSINESS), str(SCRIPTS)):
        if path not in sys.path:
            sys.path.insert(0, path)
    import affiliate_auto_apply as planner  # noqa: E402

    out: list[dict[str, Any]] = []
    for inventory in planner.freshest_inventories():
        network = inventory.stem.split("-")[0].lower()
        if network in EXCLUDED_NETWORKS or (networks and network not in networks):
            continue
        try:
            doc = json.loads(inventory.read_text())
        except (OSError, ValueError):
            continue
        rows = (planner.candidates_from_awin(doc) if doc.get("relationships")
                else planner.candidates_from_generic(doc, network))
        for row in rows:
            if not str(row.get("program_id") or "").strip():
                continue
            row["source"] = f"marketplace:{repo_relative(inventory)}"
            row.setdefault("application_url", row.get("url") or "")
            out.append(row)
    return out


def sheet_candidates(index: dict[str, Any]) -> list[dict[str, Any]]:
    """Sheet rows that are researched, GLP-vertical, and not yet applied to.

    These are the highest-value candidates in the whole system: they already
    carry a researched row, so a successful submission maps cleanly onto the
    writer's `application_pending` mode.
    """
    out: list[dict[str, Any]] = []
    for row in index["candidates"]:
        if row["sheet_vertical"] not in GLP_VERTICALS:
            continue
        if not row["application_url"]:
            continue
        network_label = row["network_label"].lower()
        network = "direct"
        for known in ("cj", "partnerstack", "rakuten", "impact", "digistore24",
                      "clickbank", "goaffpro", "uppromote", "partnerize"):
            if known in network_label.replace(" ", ""):
                network = known
                break
        if "cj affiliate" in network_label:
            network = "cj"
        out.append({
            "source": f"sheet:row{row['sheet_row']}",
            "sheet_row": row["sheet_row"],
            "name": row["name"],
            "network": network,
            "program_id": row["name"],
            "application_url": row["application_url"],
            "official_url": row["official_url"] or row["application_url"],
            "region": "US",
            "currency": "USD",
            "description": " ".join(part for part in (
                row["category"], row["fit_reason"]) if part).strip(),
            "evidence_tier": "sheet_vertical_glp1",
            "evidence_source": "sheet_row",
            "sheet_apply_flag": row["sheet_apply_flag"],
            "sheet_notes": row["notes"],
            "sheet_vertical": row["sheet_vertical"],
        })
    return out


#: Phrases in a Sheet row's Notes/Blocker that record a deliberate decision not
#: to re-apply.  A machine that re-applies over one of these is overruling a
#: recorded judgement, so it stops and says so instead.
NO_REPLAY_MARKERS = (
    "no automatic replay",
    "reapply decision required",
    "do not reapply",
    "do not re-apply",
)


# ---------------------------------------------------------------------------
# Fit gate + dedupe
# ---------------------------------------------------------------------------


#: URL path tokens that mark a page as an actual affiliate/partner application
#: surface rather than a marketing homepage.  Ranking on this is not a fit
#: judgement - every candidate here already passed the niche gate - it just
#: spends the day's cap on the programs where a form actually exists, instead
#: of burning it on homepages the executor will correctly refuse.
APPLICATION_PAGE_TOKENS = (
    "affiliate", "partner", "ambassador", "creator", "referral",
    "influencer", "publisher", "become-a", "join-",
)


def application_page_rank(candidate: dict[str, Any]) -> tuple[int, int, str]:
    url = str(candidate.get("application_url") or "").lower()
    path = url.split("//", 1)[-1]
    path = path[path.find("/"):] if "/" in path else ""
    on_form_page = any(token in path for token in APPLICATION_PAGE_TOKENS)
    tier_rank = {"explicit_glp1": 0, "clinical_weight_loss": 1,
                 "compounding_pharmacy": 1, "telehealth_clinical": 2}.get(
        str(candidate.get("fit_tier") or ""), 3)
    return (0 if on_form_page else 1, tier_rank, str(candidate.get("name") or ""))


def load_queued_index() -> dict[str, Any]:
    if QUEUED_INDEX.is_file():
        try:
            return json.loads(QUEUED_INDEX.read_text())
        except (OSError, ValueError):
            pass
    return {"schema": "glp-application-queued-index-v1", "entries": {}}


def save_queued_index(document: dict[str, Any]) -> None:
    QUEUED_INDEX.parent.mkdir(parents=True, exist_ok=True)
    QUEUED_INDEX.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    os.chmod(QUEUED_INDEX, 0o600)


#: Blockers that mean we DID submit their form and their own side then failed.
#: Only these license the sentence "your form does not work" — that is a claim
#: about the provider, and the truth gate cannot catch a lie about someone else.
POST_SUBMIT_FAILURES = (
    "provider_404_after_submit", "provider_server_error_after_submit",
    "provider_generic_error_after_submit", "provider_bot_wall_after_submit",
)


#: The executor's LANE vocabulary is not the Sheet's VERTICAL vocabulary, and
#: the first real run proved it the expensive way: three applications were sent,
#: their queue rows carried `vertical: telehealth-general` straight off the
#: ledger's lane, and the writer refused all three with
#: `affiliate_sheet_vertical_invalid:telehealth-general`. The emails were out and
#: the Sheet did not know. Translation happens here, from an explicit table.
SHEET_VERTICAL_BY_LANE = {
    "glp1-weightloss": "glp1-weightloss",
    "supplements-vitamins": "supplements-nutrition",
    "supplements-nutrition": "supplements-nutrition",
    "peptides-longevity": "peptides-longevity",
    "diagnostics-testing": "diagnostics-testing",
    "skincare-aesthetics": "skincare-aesthetics",
    "fitness-equipment": "fitness-equipment",
    "womens-health": "womens-health",
}

#: Fit-evidence tokens that make `glp1-weightloss` a READING of the evidence
#: rather than a guess.  `telehealth-general` is a lane the classifier uses for
#: broad telehealth brands; when that same row also carries explicit GLP-1 fit
#: evidence, the Sheet vertical it belongs in is not in doubt.
GLP_FIT_EVIDENCE_TOKENS = ("glp_fit:explicit_glp1", "glp_fit:clinical_weight_loss",
                           "glp1_token")


def sheet_vertical_for(row: dict[str, Any]) -> str:
    """The Sheet Vertical this program belongs in, or "" if underivable.

    Read against the schema guard's own allowlist rather than a restated copy,
    so a vocabulary change there can never leave this silently minting invalid
    rows.  Underivable returns "" and the caller skips the provider by name —
    an application we cannot record is not an application we should send.
    """
    for path in (str(BUSINESS), str(SCRIPTS)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from affiliate_sheet_schema_guard import VERTICAL_ALLOWED_VALUES  # noqa: E402

    lane = str(row.get("lane") or "").strip().lower()
    mapped = SHEET_VERTICAL_BY_LANE.get(lane, "")
    if mapped in VERTICAL_ALLOWED_VALUES:
        return mapped
    evidence = " ".join(str(row.get(key) or "") for key in
                        ("fit_reason", "description", "evidence_tier")).lower()
    if any(token in evidence for token in GLP_FIT_EVIDENCE_TOKENS):
        return "glp1-weightloss"
    return ""


def _email_framing(provider: str, url: str, blocker: str, detail: str = "") -> str:
    """What we may honestly tell the provider about why we are emailing.

    Three different facts hide behind the routing:

    * we submitted and their endpoint errored — Mediclab's real 404. "Your form
      is broken" is TRUE, and telling them is a favour: every application they
      ever received through it was silently lost.
    * their affiliate page exists and describes the program but publishes no
      form we could complete — Inception Telehealth's ambassador page carries
      zero `<form>` elements. Saying "your form is broken" here would be a claim
      about a form that does not exist.
    * the routed URL is a homepage or an article — we know only that we could
      not find an application route at all.

    Getting this wrong is cheap to do and expensive to send: an email that opens
    by describing a page the recipient does not have is the fastest way to look
    like a bot.
    """
    path = url.split("//", 1)[-1]
    path = path[path.find("/"):].lower() if "/" in path else ""
    on_application_page = any(token in path for token in APPLICATION_PAGE_TOKENS)
    if blocker in POST_SUBMIT_FAILURES:
        return (f"We submitted their published affiliate application form at {url} and "
                f"their own endpoint failed: {detail or blocker}. Write this as an application to "
                f"their affiliate program, and state plainly and helpfully that their "
                f"form does not work, which is why we are emailing instead.")
    if blocker == "application_page_unreachable":
        return (f"Their affiliate application page at {url} did not load for us "
                f"({blocker}). Write this as an application to their affiliate program, "
                f"say in one clause that the page would not load so we are applying by "
                f"email, and ask for the correct route if we have the wrong URL.")
    if on_application_page and blocker == "application_form_not_found":
        return (f"Their page at {url} describes their affiliate/ambassador program but "
                f"publishes no application form we could complete — there is no form on "
                f"the page. Write this as an application to that program, referring to "
                f"what the page itself says, and ask how they would like applications "
                f"submitted. Do NOT say their form is broken: there is no form.")
    return (f"We could not find a published affiliate application form anywhere on "
            f"their site (the automated attempt returned {blocker}). Do NOT claim to "
            f"have seen their affiliate program and do NOT claim their form is broken. "
            f"Write this as a direct application asking whether they run an affiliate "
            f"or partner arrangement, offering to set one up if they do not, and ask "
            f"that the note be routed to whoever owns partnerships if this is a general "
            f"inbox.")


def email_fallback_targets(ledger: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Programs whose PORTAL application failed for a PROVIDER-SIDE reason.

    Operator ruling 2026-08-18, after reproducing Mediclab's 404 by hand:

      "mediclab returned 404 for me too - affiliate application job will need to
       add them to the list to email directly."

    A broken provider form is a routing fact, not operator work. `portal_apply`
    stamps every failed row with `outcome_class` + `application_route_next`, and
    this turns the `email`-routed ones into targets for the proven email lane.
    Nothing is invented: the personalization and fit reason come from the row's
    own captured evidence, and a row with no usable contact route is skipped by
    the email lane itself rather than being guessed at here.
    """
    if ledger is None:
        try:
            ledger = json.loads(LEDGER_PATH.read_text())
        except (OSError, ValueError):
            return []
    # The email lane's own ledger is the authority on what has already been sent
    # (6 of these were emailed on 2026-08-18). Reading it here keeps the target
    # list honest instead of leaning on the lane to skip duplicates silently.
    already: set[str] = set()
    try:
        index = json.loads(
            (MACHINE_DIR / "email-applications/email-applied-index.json").read_text())
        already = {norm(key) for key in (index.get("applied") or {})}
    except (OSError, ValueError):
        pass

    out: list[dict[str, Any]] = []
    for row in ledger.get("applications") or []:
        if str(row.get("application_route_next") or "") != "email":
            continue
        if row.get("email_applied_at_utc") or norm(row.get("name")) in already:
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        detail = str(row.get("blocker_detail") or "").strip()
        url = str(row.get("application_url") or row.get("url") or "")
        out.append({
            "provider": name,
            "contact_email": str(row.get("partnerships_email") or "").strip(),
            "evidence_url": str(row.get("application_url") or row.get("url") or ""),
            "official_url": str(row.get("url") or row.get("application_url") or ""),
            "category": str(row.get("category") or "GLP-1 telehealth"),
            "fit_reason": str(row.get("fit_reason") or row.get("description") or ""),
            "vertical": sheet_vertical_for(row),
            "personalization": str(row.get("description") or "")[:400],
            # The email lane must SAY why we are emailing rather than applying,
            # and it must say something TRUE. "Your form is broken" is only true
            # when the row we routed actually names an application surface; for
            # a row whose URL is a homepage or an article, the honest statement
            # is that no application form is published at all.
            # The blocker CODE decides what we may claim; the human-readable
            # detail only colours the sentence. Passing the detail as the code
            # made every framing fall through to the generic branch.
            "framing": _email_framing(name, url, str(row.get("last_blocker") or ""),
                                      detail=detail),
            "routed_from": {
                "network": row.get("network"), "program_id": row.get("program_id"),
                "blocker": row.get("last_blocker"), "outcome_class": row.get("outcome_class"),
            },
        })
    return sorted(out, key=lambda row: row["provider"])


# ---------------------------------------------------------------------------
# The INSIDE-ACCOUNT routes (Impact per-advertiser, Everflow brand portals)
# ---------------------------------------------------------------------------
#
# Until 2026-08-19 these were operator work, not machine work: canonical Sheet
# row 777 records that the account-level Impact Marketplace application is
# Declined and demanded explicit operator approval before a broad per-advertiser
# run. The operator granted exactly that approval on 2026-08-18, and it is now
# recorded as `OA-20260818-impact-everflow-per-advertiser` so the machine reads
# it at run time and never asks again.
#
# The routes are DISCOVERED here so the daily card can count them and so a
# scheduled run reports what is waiting even when it cannot execute (the Impact
# lane needs an authenticated browser session that only the operator can
# establish). Execution lives in the two dedicated lanes, for the same reason
# Awin's Join does: these are not third-party application forms and the generic
# form filler cannot drive them.

INSIDE_ACCOUNT_ROUTES = ("impact_per_advertiser", "everflow_brand_portal")
INSIDE_ACCOUNT_LANES = {
    "impact_per_advertiser": "scripts/glp_impact_program_apply.py",
    "everflow_brand_portal": "scripts/glp_everflow_portal_apply.py",
}


def inside_account_routes() -> dict[str, Any]:
    """What the inside-account routes have available, and whether we may use it.

    Returns a receipt-shaped dict rather than raising: a missing authorization is
    a REFUSAL the daily card should say out loud, not a crash. The default is
    refusal - without an active authorization naming the route, the counts are
    zero and the reason is named.
    """
    out: dict[str, Any] = {"routes": {}}
    try:
        import glp_operator_authorizations as auth  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"routes": {}, "error": f"authorization_loader_unavailable:{exc!r}"[:200]}

    for route in INSIDE_ACCOUNT_ROUTES:
        record = auth.authorization_for(route)
        entry: dict[str, Any] = {"lane": INSIDE_ACCOUNT_LANES[route],
                                 "authorized": record is not None}
        if record is None:
            entry["refusal"] = auth.refusal_for(route)
            entry["doors_available"] = 0
            out["routes"][route] = entry
            continue
        entry["authorization"] = auth.cite(record)
        try:
            if route == "everflow_brand_portal":
                import glp_everflow_portal_apply as lane  # noqa: PLC0415
                entry["doors_available"] = len(lane.manifest_doors())
            else:
                import glp_impact_program_apply as lane  # noqa: PLC0415
                doors = lane.manifest_doors()
                entry["doors_available"] = len(doors)
                entry["doors_in_niche"] = sum(
                    1 for door in doors if lane.fit_verdict(door)[0])
        except Exception as exc:  # noqa: BLE001
            entry["doors_available"] = 0
            entry["error"] = f"{type(exc).__name__}:{exc}"[:160]
        out["routes"][route] = entry
    return out


def build_dedupe_index(index: dict[str, Any], queued: dict[str, Any]):
    """The shared dedupe index, seeded from this run's live Sheet read.

    Also refreshes the on-disk Sheet mirror so the lanes that cannot reach
    Google (the Playwright submit venv, `portal_apply.drain`) still get the
    governed-Sheet leg on their next run.
    """
    for path in (str(BUSINESS), str(SCRIPTS)):
        if path not in sys.path:
            sys.path.insert(0, path)
    import glp_application_dedupe as dedupe  # noqa: E402

    try:
        dedupe.write_sheet_mirror(index.get("engaged_names") or (),
                                  index.get("engaged_ids") or (),
                                  row_count=int(index.get("row_count") or 0),
                                  engaged_rows=index.get("engaged_rows") or ())
    except OSError:
        pass  # the mirror is a convenience for other lanes, never this run's truth
    return dedupe.build(sheet_index=index,
                        queue_rows=[{"program_name": entry.get("name") if isinstance(entry, dict) else "",
                                     "network": str(key).split(":", 1)[0],
                                     "program_id": str(key).split(":", 1)[-1],
                                     "update_id": key, "kind": "queued_index"}
                                    for key, entry in (queued.get("entries") or {}).items()],
                        check_live_manifest=True)


def ledger_identities() -> set[str]:
    """Every program the executor ledger has ever touched, in any state."""
    try:
        doc = json.loads(LEDGER_PATH.read_text())
    except (OSError, ValueError):
        return set()
    out: set[str] = set()
    for row in doc.get("applications") or []:
        out.add(f"{str(row.get('network') or '').lower()}:{row.get('program_id')}")
        out.add("name:" + norm(row.get("name")))
    return out


def gate_and_dedupe(pool: list[dict[str, Any]], index: dict[str, Any],
                    queued: dict[str, Any], nfilter,
                    dedupe_index=None) -> dict[str, Any]:
    """One decision per candidate, with the reason recorded either way.

    Dedupe is NOT decided here. It is delegated to `glp_application_dedupe`,
    the single source of truth every application-causing lane shares, because a
    private dedupe is how the machine applied to Found — a Katalys program we
    already earn from and serve at /go/found-health — off a stale
    `verify_affiliate_route` Sheet row spelled "Found" while the live row is
    spelled "Found Health". See that module's docstring for the full post-mortem.
    """
    dedupe = dedupe_index if dedupe_index is not None else build_dedupe_index(index, queued)
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()

    for candidate in pool:
        name = str(candidate.get("name") or "").strip()
        network = str(candidate.get("network") or "direct").strip().lower()
        program_id = str(candidate.get("program_id") or name).strip()
        key = f"{network}:{program_id}"

        def skip(reason: str, detail: str = "") -> None:
            skipped.append({"name": name, "network": network, "program_id": program_id,
                            "reason": reason, "detail": detail,
                            "source": candidate.get("source", "")})

        if not name or not program_id:
            skip("identity_incomplete")
            continue
        if key in seen or f"name:{norm(name)}" in seen:
            skip("duplicate_within_run")
            continue
        if network in EXCLUDED_NETWORKS:
            skip("network_excluded", network)
            continue
        host = str(candidate.get("application_url") or "").split("//", 1)[-1].split("/", 1)[0].lower()
        blocked = BLOCKED_APPLICATION_HOSTS.get(host)
        if blocked:
            skip("application_host_blocked", f"{host}:{blocked}")
            continue

        # --- dedupe (single source of truth: glp_application_dedupe) -------
        # The candidate's Sheet network label is an alias for the same program:
        # a direct research stub and its live network row are the same brand.
        verdict = dedupe.verdict(
            name, candidate.get("network_label") or network, program_id,
            aliases=(candidate.get("display_name") or "",
                     candidate.get("canonical_display_name") or ""),
            urls=(candidate.get("official_url") or "",
                  candidate.get("application_url") or "",
                  candidate.get("url") or ""))
        if verdict is None and network != norm(candidate.get("network_label") or network):
            verdict = dedupe.verdict(name, network, program_id)
        if verdict is not None:
            skip(verdict.reason, f"{verdict.leg}: {verdict.get('evidence', '')}"[:300])
            continue
        notes = str(candidate.get("sheet_notes") or "").lower()
        marker = next((m for m in NO_REPLAY_MARKERS if m in notes), "")
        if marker:
            skip("sheet_row_marked_no_replay", marker)
            continue

        # --- fit gate -----------------------------------------------------
        mapping = {
            "display_name": name,
            "canonical_display_name": name,
            "network": network,
            "program_id": program_id,
            "description": candidate.get("description") or candidate.get("evidence_tier") or "",
            "categories": candidate.get("categories") or [],
            "region": candidate.get("region") or "",
            "application_url": candidate.get("application_url") or "",
        }
        verdict = nfilter.choke_candidate(
            mapping, require_application_url=True, require_fit_evidence=True,
            operation="glp_application_machine.gate", persist=False)
        if not verdict["operational"]:
            skip(verdict["decision"], ";".join(verdict.get("reason_codes") or [])[:200])
            continue
        fit_tier = str((verdict.get("evidence") or {}).get("fit_tier") or "")
        lane = ""
        for reason in verdict.get("reason_codes") or []:
            if str(reason).startswith("lane:"):
                lane = str(reason).split(":", 1)[1]
                break
        # The classifier usually names the lane; when it does not (it can reach
        # APPLY_OK off an operator ruling or a Sheet-assigned vertical without
        # emitting a lane token), fall back to the row's own vertical rather
        # than dropping a genuinely fit program on a bookkeeping detail.
        if not lane:
            lane = str(candidate.get("lane") or candidate.get("sheet_vertical") or "").strip().lower()
        if not lane or lane not in AUTHORIZED_LANES:
            skip("no_authorized_lane", lane)
            continue

        seen.add(key)
        seen.add(f"name:{norm(name)}")
        accepted.append({
            **candidate,
            "network": network,
            "program_id": program_id,
            "lane": lane,
            "fit_tier": fit_tier,
            "fit_reason_codes": list(verdict.get("reason_codes") or []),
            "route": "portal",
        })
    return {"accepted": accepted, "skipped": skipped}


# ---------------------------------------------------------------------------
# Ledger enqueue + submission
# ---------------------------------------------------------------------------


def enqueue_ledger(rows: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    """Put the accepted rows into the executor's queue, atomically.

    The executor drains `status == queued_for_portal_execution` from the
    canonical ledger; this is the handoff, and the ledger is also the durable
    replay guard that stops a second run double-applying.
    """
    for path in (str(BUSINESS), str(SCRIPTS)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from scripts.marketplace_adapters import portal_apply  # noqa: E402

    entries = []
    for row in rows:
        entries.append({
            "network": row["network"],
            "program_id": str(row["program_id"]),
            "name": row["name"],
            "lane": row["lane"],
            "route": "portal",
            "status": "queued_for_portal_execution",
            "planned_at_utc": utc_now(),
            "url": row.get("official_url") or row.get("application_url") or "",
            "application_url": row.get("application_url") or "",
            "description": row.get("description") or "",
            "fit_reason": ";".join(row.get("fit_reason_codes") or [])[:400],
            "region": row.get("region") or "US",
            "currency": row.get("currency") or "USD",
            "categories": row.get("categories") or [],
            "queued_by": f"glp_application_machine:{run_id}",
        })
    if entries:
        portal_apply.update_ledger(entries)
    return entries


def submit(rows: list[dict[str, Any]], cap: int, run_id: str,
           run_dir: Path) -> dict[str, Any]:
    """Run the proven executor, on EXACTLY today's rows, in the Playwright venv.

    The row set is handed over explicitly rather than left to the executor's own
    ledger scan: the machine has already spent its fit gate and its dedupe on
    choosing these, and the day's cap belongs to them.
    """
    if not RUNTIME_PYTHON.is_file():
        return {"status": "BLOCKED", "blocker": "runtime_python_missing",
                "detail": str(RUNTIME_PYTHON)}
    rows_path = run_dir / "submit-rows.json"
    rows_path.write_text(json.dumps(rows, indent=2, sort_keys=True, default=str) + "\n")
    os.chmod(rows_path, 0o600)
    argv = [str(RUNTIME_PYTHON), "-B", "scripts/glp_application_submit_child.py",
            "--rows", str(rows_path), "--run-id", run_id, "--daily-cap", str(cap)]
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{BUSINESS}:{SCRIPTS}"
    try:
        proc = subprocess.run(argv, cwd=str(BUSINESS), env=env, capture_output=True,
                              text=True, timeout=3000)
    except subprocess.TimeoutExpired:
        return {"status": "BLOCKED", "blocker": "executor_timeout"}
    stdout = (proc.stdout or "").strip().splitlines()
    summary: dict[str, Any] = {}
    for line in reversed(stdout):
        try:
            summary = json.loads(line)
            break
        except ValueError:
            continue
    if not summary:
        return {"status": "BLOCKED", "blocker": "executor_no_json_summary",
                "returncode": proc.returncode,
                "stderr_tail": (proc.stderr or "")[-1500:]}
    summary["status"] = "RAN"
    summary["returncode"] = proc.returncode
    summary["stderr_tail"] = (proc.stderr or "")[-800:]
    return summary


def collect_evidence(run_dir: Path, evidence_dir: Path) -> dict[str, str]:
    """Copy the executor's per-submission artifacts under applications/.

    The Sheet cites these paths, so they have to live somewhere a human can
    open forever - not inside a per-run scratch directory nobody looks at.
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    if not run_dir.is_dir():
        return copied
    for source in sorted(run_dir.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(run_dir)
        target = evidence_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied[str(relative)] = repo_relative(target)
    return copied


# ---------------------------------------------------------------------------
# Queue rows for the one sheet writer
# ---------------------------------------------------------------------------


def queue_rows_for(results: list[dict[str, Any]], accepted_by_key: dict[str, dict[str, Any]],
                   rows_by_name: dict[str, int], receipt_path: str,
                   run_id: str) -> list[dict[str, Any]]:
    """One `application_pending` queue row per REAL, verified submission.

    Only `SUBMITTED_VERIFIED` produces a queue row.  An unverified submit means
    the lane lost track of the portal's state; claiming "applied" from that
    would put a guess into the source of truth.
    """
    out: list[dict[str, Any]] = []
    for result in results:
        if str(result.get("status")) != "SUBMITTED_VERIFIED":
            continue
        network = str(result.get("network") or "")
        program_id = str(result.get("program_id") or "")
        name = str(result.get("name") or "")
        candidate = accepted_by_key.get(f"{network}:{program_id}", {})
        submitted_at = str(result.get("completed_at_utc") or utc_now())
        evidence = {
            "source": f"glp_application_machine:{run_id}",
            "submitted_at_utc": submitted_at,
            "submission_receipt_path": receipt_path,
            "network": network,
            "program_id": program_id,
            "application_url": candidate.get("application_url", ""),
            "confirmation_state": str(result.get("confirmation_state") or ""),
            "fit_tier": candidate.get("fit_tier", ""),
        }
        for key in ("first_party_evidence_path", "screenshot_path", "payload_path"):
            if result.get(key):
                evidence[key] = str(result[key])
        out.append({
            "update_id": f"application-{run_id}-{network}-{re.sub(r'[^A-Za-z0-9]+', '-', program_id)[:40]}",
            "kind": "application_pending",
            "network": candidate.get("network_label") or network,
            "program_id": program_id,
            "program_name": name,
            "sheet_row": rows_by_name.get(norm(name), ""),
            # This is the QUEUE row's own lifecycle status, not the Sheet
            # status. The writer drains only rows whose status is blank or
            # "queued" - anything else it treats as already resolved and skips
            # in silence, which is how a submission would vanish before it ever
            # reached the Sheet. The Sheet status is decided by the kind.
            "status": "queued",
            "queued_by": "glp_application_machine",
            "queued_at_utc": utc_now(),
            "note": (f"Applied via {network} on {submitted_at}; "
                     f"fit={candidate.get('fit_tier', 'glp')}; awaiting brand decision."),
            "evidence": json.dumps(evidence, sort_keys=True),
        })
    return out


# ---------------------------------------------------------------------------
# The EMAIL route, consumed by the main flow
# ---------------------------------------------------------------------------
#
# Until 2026-08-18 this machine could ROUTE a provider to email and then do
# nothing with it. `email_fallback_targets()` existed, 12 providers carried
# `application_route_next: email`, and the daily 09:40 PT run still reported
# `paths: portal=0 email=0 api=0` — because nothing in the main flow consumed
# the targets. Queued is not sent. This section is the consumption.
#
# Three things had to be true before a scheduled run could send:
#   1. a contact address must be RESOLVED, from the provider's own published
#      pages, by `glp_provider_contact_discovery` — never guessed or bought;
#   2. the send must be PACED, because a young sending domain that fires a
#      dozen cold emails in one minute damages its own reputation;
#   3. the day's cap must be SHARED with the portal route, so the two lanes
#      cannot together exceed the operator's 25/day bar.

EMAIL_LANE_SCRIPT = SCRIPTS / "glp_application_email_apply.py"

#: Per-run email cap. Deliberately well under the 25/day application bar:
#: glpclinicscout.com is a young sending domain, cold volume is what gets a
#: domain filtered, and 4-6 a day compounds to a real pipeline inside a week
#: without ever looking like a blast. Overridable via --email-cap so the bridge
#: action (and a proof run) can lower it; it is capped by the day's remaining
#: application budget either way.
DEFAULT_EMAIL_CAP = 5

#: Seconds between sends inside one run. Sends land minutes apart instead of
#: milliseconds apart.
DEFAULT_EMAIL_PACE_S = 45

#: If the portal phase has already burned this much of the run, the email lane
#: is skipped WITH A NAMED REASON rather than being started and killed by the
#: bridge's 3300s timeout mid-send. A half-sent lane is the one state that can
#: produce an email with no receipt.
EMAIL_LANE_DEADLINE_S = 2100

#: Ceiling for the email lane subprocess itself (drafting + gate + paced sends).
EMAIL_LANE_TIMEOUT_S = 1500


#: Whole-run ceiling for contact discovery. Twelve providers × a dozen page
#: fetches each is minutes of wall clock inside a machine that also has a
#: browser lane to run; past this, the rest are skipped with a reason that says
#: so, rather than the run being killed with sends half-made.
CONTACT_DISCOVERY_BUDGET_S = 600.0


def _with_sheet_row(target: dict[str, Any],
                    sheet_rows: dict[str, int] | None) -> dict[str, Any]:
    """Address an EXISTING Sheet row when the program already has one.

    Without this the queue row carries a blank `sheet_row`, the drain's identity
    match is network-scoped, and a program filed under `Direct/contact verify`
    gets a SECOND row created next to it — the Calibrate 20/85 duplicate, again.
    The drain re-verifies the row's program name before writing, so naming a row
    here can only ever address the right one or be refused.
    """
    row_number = (sheet_rows or {}).get(norm(target.get("provider")))
    if row_number:
        target["sheet_row"] = row_number
    return target


def resolve_email_contacts(targets: list[dict[str, Any]], budget: int,
                           run_dir: Path,
                           deadline_s: float = CONTACT_DISCOVERY_BUDGET_S,
                           sheet_rows: dict[str, int] | None = None) -> dict[str, Any]:
    """Turn routed targets into SENDABLE targets, or into named skips.

    A target with no published address is not a target. It is skipped by name
    and reported, exactly as Lasara and FuturHealth were on 2026-08-18 — the
    alternative is guessing an address, and a guessed address is how a machine
    starts emailing strangers in a provider's name.
    """
    for path in (str(BUSINESS), str(SCRIPTS)):
        if path not in sys.path:
            sys.path.insert(0, path)
    import glp_provider_contact_discovery as contacts  # noqa: E402

    resolved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    receipts: dict[str, Any] = {}
    started = time.monotonic()
    for target in targets:
        provider = str(target.get("provider") or "")
        if len(resolved) >= max(budget, 0):
            skipped.append({"provider": provider, "reason": "email_cap_reached_this_run",
                            "detail": f"cap={budget}", "pages_checked": 0})
            continue
        if not str(target.get("vertical") or "").strip():
            # An application the governed Sheet cannot record is not one we
            # should send: the email would go out and nothing would know.
            skipped.append({"provider": provider,
                            "reason": "no_sheet_vertical_derivable_from_evidence",
                            "detail": f"lane={target.get('vertical_lane', '')}",
                            "pages_checked": 0})
            continue
        if str(target.get("contact_email") or "").strip():
            resolved.append(_with_sheet_row(dict(target), sheet_rows))
            continue
        if time.monotonic() - started > deadline_s:
            skipped.append({"provider": provider,
                            "reason": "contact_discovery_run_budget_exhausted",
                            "detail": f"budget={int(deadline_s)}s", "pages_checked": 0})
            continue
        try:
            found = contacts.discover_with_personalization(
                provider, [target.get("official_url"), target.get("evidence_url")])
        except Exception as exc:  # noqa: BLE001 — a discovery crash is a skip, not a run failure
            skipped.append({"provider": provider, "reason": "contact_discovery_failed",
                            "detail": f"{type(exc).__name__}: {exc}"[:200],
                            "pages_checked": 0})
            continue
        receipts[provider] = found
        if not found.get("contact_email"):
            skipped.append({"provider": provider,
                            "reason": "no_verified_published_contact_address",
                            "detail": str(found.get("skip_reason") or "")[:240],
                            "pages_checked": found.get("pages_fetched", 0)})
            continue
        row = dict(target)
        row["contact_email"] = found["contact_email"]
        row["contact_tier"] = found.get("contact_tier", "")
        row["contact_evidence"] = found.get("contact_evidence", {})
        excerpt = (found.get("personalization") or {}).get("text") or ""
        if excerpt:
            # Verbatim from their own page, with the URL, so the opening line is
            # checkable rather than a model's impression of the brand.
            row["personalization"] = (
                f"Quoted verbatim from {(found.get('personalization') or {}).get('source_url')} "
                f"on {utc_now()[:10]}: \"{excerpt}\"")
        else:
            # No quotable line was captured. The engine must NOT fill that gap
            # from its own knowledge of the brand — a plausible-sounding detail
            # is the one kind of falsehood the truth gate cannot see, because it
            # is a claim about them.
            row["personalization"] = (
                "No verbatim line was captured from their own pages today. Do NOT state "
                "any specific fact about their program, pricing, medications or audience. "
                "Open on what we do and what a partnership would look like instead.")
        resolved.append(_with_sheet_row(row, sheet_rows))

    receipt_path = run_dir / "email-contact-discovery.json"
    receipt_path.write_text(json.dumps(
        {"captured_at_utc": utc_now(), "resolved": [r["provider"] for r in resolved],
         "skipped": skipped, "discovery": receipts}, indent=2, sort_keys=True,
        default=str) + "\n")
    os.chmod(receipt_path, 0o600)
    return {"resolved": resolved, "skipped": skipped,
            "receipt": repo_relative(receipt_path)}


def run_email_lane(cap: int, run_id: str, run_dir: Path, engine: str = "claude_p",
                   pace_s: int = DEFAULT_EMAIL_PACE_S, plan_only: bool = False,
                   targets: list[dict[str, Any]] | None = None,
                   sheet_rows: dict[str, int] | None = None) -> dict[str, Any]:
    """Send today's email-route applications through the PROVEN email lane.

    This shells `glp_application_email_apply.py` rather than importing it: that
    script owns the alias re-verification, the truth gate, the `.eml` receipt,
    the per-send ledger write and the queue-row shape, and every one of those is
    proven by real sends. Re-implementing any of it here would be a second,
    unproven copy of the only part of this machine that touches a stranger's
    inbox.
    """
    targets = email_fallback_targets() if targets is None else targets
    if not targets:
        return {"status": "NO_TARGETS", "sent": 0, "queue_rows": [],
                "targets": 0, "skipped": []}
    if cap <= 0:
        return {"status": "NO_BUDGET", "sent": 0, "queue_rows": [],
                "targets": len(targets),
                "skipped": [{"provider": t["provider"],
                             "reason": "daily_application_cap_exhausted_by_portal_route"}
                            for t in targets]}

    contacts = resolve_email_contacts(targets, cap, run_dir, sheet_rows=sheet_rows)
    sendable = contacts["resolved"]
    targets_path = run_dir / "email-targets.json"
    targets_path.write_text(json.dumps(
        {"schema": "glp_email_application_targets_v1", "captured_at_utc": utc_now(),
         "run_id": run_id, "targets": sendable}, indent=2, sort_keys=True,
        default=str) + "\n")
    os.chmod(targets_path, 0o600)

    base = {"status": "", "targets": len(targets), "sendable": len(sendable),
            "skipped": contacts["skipped"], "contact_receipt": contacts["receipt"],
            "targets_file": repo_relative(targets_path), "cap": cap,
            "pace_seconds": pace_s, "sent": 0, "queue_rows": []}
    if plan_only:
        return {**base, "status": "PLAN_ONLY"}
    if not sendable:
        return {**base, "status": "NO_VERIFIED_CONTACTS"}

    argv = [sys.executable, "-B", str(EMAIL_LANE_SCRIPT),
            "--targets", str(targets_path), "--send", "--cap", str(cap),
            "--engine", engine, "--pace-seconds", str(pace_s),
            "--run-id", f"emailapply-{run_id}"]
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{BUSINESS}:{SCRIPTS}"
    try:
        proc = subprocess.run(argv, cwd=str(BUSINESS), env=env, capture_output=True,
                              text=True, timeout=EMAIL_LANE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # The lane writes its ledger and queue rows after EVERY send, so a
        # timeout loses the summary, never a receipt. Read what landed on disk.
        return {**base, "status": "TIMEOUT",
                **_email_lane_disk_state(f"emailapply-{run_id}")}
    summary: dict[str, Any] = {}
    for line in (proc.stdout or "").splitlines():
        if line.startswith("EMAIL_APPLY_SUMMARY "):
            try:
                summary = json.loads(line.split(" ", 1)[1])
            except ValueError:
                summary = {}
    disk = _email_lane_disk_state(f"emailapply-{run_id}")
    return {**base, "status": "RAN" if proc.returncode == 0 else "LANE_FAILED",
            "returncode": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-600:],
            "engine": engine, "lane_summary": summary, **disk}


def _email_lane_disk_state(lane_run_id: str) -> dict[str, Any]:
    """The lane's own on-disk receipts are the truth about what was sent."""
    lane_dir = MACHINE_DIR / "email-applications" / lane_run_id
    out: dict[str, Any] = {"sent": 0, "queue_rows": [], "results": [],
                           "lane_run_dir": repo_relative(lane_dir)}
    try:
        rows = json.loads((lane_dir / "queue-rows.json").read_text())
        out["queue_rows"] = rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        pass
    try:
        doc = json.loads((lane_dir / "run.json").read_text())
        out["results"] = doc.get("results") or []
        out["sender_alias"] = doc.get("sender_alias")
    except (OSError, ValueError):
        pass
    out["sent"] = sum(1 for row in out["results"]
                      if str(row.get("status")) == "SENT_VERIFIED") or len(out["queue_rows"])
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


#: The three real application routes this machine can produce a submission on.
#: A queue row states its own channel in `evidence.application_channel`; rows
#: written before that field existed are portal submissions, which is what the
#: form executor has always been.
#: `inside_account` is the route added 2026-08-19: doors that live INSIDE an
#: account or on a brand-hosted tenant, rather than on a network's own
#: application form. Two lanes carry it - Impact per-advertiser signup and the
#: Everflow white-label brand portals - and both are gated on the standing
#: operator authorization rather than on a per-run card.
APPLICATION_PATHS = ("portal", "email", "api", "inside_account")


def path_counts(queue_rows: list[dict[str, Any]], run_id: str = "") -> dict[str, int]:
    """Break today's submissions out by the route that produced them."""
    counts = {path: 0 for path in APPLICATION_PATHS}
    counts["unknown"] = 0
    for row in queue_rows:
        evidence = row.get("evidence")
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except ValueError:
                evidence = {}
        channel = str((evidence or {}).get("application_channel") or "portal").lower()
        counts[channel if channel in counts else "unknown"] += 1
    return counts


def _application_queue_client():
    ops = str(BUSINESS / "ops")
    if ops not in sys.path:
        sys.path.insert(0, ops)
    import n8n_dt  # noqa: E402
    return n8n_dt


QUEUE_READ_MAX_ATTEMPTS = 3
QUEUE_READ_BACKOFF_S = (0.25, 1.0)
_LAST_QUEUE_READ_OBSERVATIONS: list[dict[str, Any]] = []


def read_application_queue(client=None, *, sleep=time.sleep) -> list[dict[str, Any]]:
    """Read only operator-queued work, stabilizing a suspicious full page.

    Transport retries already live in ``n8n_dt``. This is a different integrity
    boundary: on 2026-08-25 the relay returned a successful 100-row page for an
    empty filtered queue, then the same filter returned zero rows one minute
    later. A full page is therefore re-read before it is accepted as a stable
    truncation. A legitimate 100+ row queue remains fail-closed after three
    consistent observations; no row is ever silently dropped.
    """
    global _LAST_QUEUE_READ_OBSERVATIONS
    client = client or _application_queue_client()
    observations: list[dict[str, Any]] = []
    for attempt in range(1, QUEUE_READ_MAX_ATTEMPTS + 1):
        rows = client.rows(APPLICATION_QUEUE_TABLE, client.eq("status", "queued"))
        truncated = bool(client.last_read_truncated)
        statuses: dict[str, int] = {}
        keys: set[str] = set()
        for row in rows:
            status = str(row.get("status") or "<empty>")
            statuses[status] = statuses.get(status, 0) + 1
            keys.update(str(key) for key in row)
        observations.append({
            "attempt": attempt,
            "row_count": len(rows),
            "truncated": truncated,
            "status_counts": dict(sorted(statuses.items())),
            "row_keys": sorted(keys),
        })
        _LAST_QUEUE_READ_OBSERVATIONS = observations
        if not truncated:
            return sorted(rows, key=lambda row: (str(row.get("queued_at_utc") or ""),
                                                 str(row.get("queue_id") or "")))
        if attempt < QUEUE_READ_MAX_ATTEMPTS:
            sleep(QUEUE_READ_BACKOFF_S[attempt - 1])
    raise RuntimeError(
        f"application_queue_read_truncated_after_{QUEUE_READ_MAX_ATTEMPTS}_attempts:"
        f"row_count={observations[-1]['row_count']}")


SUBMISSION_PACKET_SCHEMA = "glp.affiliate-submission-packet.v1"
SUBMISSION_MECHANISMS = frozenset({"ui_portal", "documented_api", "published_email"})


def validate_submission_packet(packet: Any, *, network: str, program_id: str,
                               program_name: str) -> dict[str, Any]:
    """Require submit-time mechanics to be fully staged and evidence-bound."""
    if not isinstance(packet, dict):
        raise ValueError("submission_packet_missing_or_not_object")
    if packet.get("schema") != SUBMISSION_PACKET_SCHEMA:
        raise ValueError("submission_packet_schema_invalid")
    identities = (str(packet.get("network") or "").strip().lower(),
                  str(packet.get("program_id") or "").strip(),
                  str(packet.get("program_name") or "").strip())
    if identities != (network, program_id, program_name):
        raise ValueError("submission_packet_program_identity_mismatch")
    mechanism = packet.get("mechanism")
    if (not isinstance(mechanism, dict)
            or mechanism.get("type") not in SUBMISSION_MECHANISMS
            or not str(mechanism.get("entry_surface") or "").strip()):
        raise ValueError("submission_packet_mechanism_invalid")
    fields = packet.get("form_fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("submission_packet_form_fields_missing")
    for index, field in enumerate(fields):
        if not isinstance(field, dict) or not all(
                str(field.get(key) or "").strip()
                for key in ("field", "value", "verified_from")):
            raise ValueError(f"submission_packet_form_field_invalid:{index}")
    identity = packet.get("identity")
    if not isinstance(identity, dict) or not all(
            str(identity.get(key) or "").strip()
            for key in ("profile_key", "publisher_identity", "website", "contact_identity")):
        raise ValueError("submission_packet_identity_incomplete")
    fit = packet.get("fit_assessment")
    if not isinstance(fit, dict) or fit.get("decision") != "eligible" \
            or fit.get("us_verified") is not True:
        raise ValueError("submission_packet_fit_assessment_refused")
    evidence = packet.get("verification_evidence")
    if not isinstance(evidence, dict):
        raise ValueError("submission_packet_verification_evidence_missing")
    raw_path = str(evidence.get("path") or "").strip()
    digest = str(evidence.get("sha256") or "").strip()
    if not raw_path.startswith("reports/") or ".." in raw_path:
        raise ValueError("submission_packet_evidence_path_invalid")
    path = BUSINESS / raw_path
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("submission_packet_evidence_hash_mismatch")
    if not str(evidence.get("verified_at_utc") or "").strip():
        raise ValueError("submission_packet_verified_at_missing")
    if network == "maxbounty":
        by_name = {str(field["field"]): str(field["value"]) for field in fields}
        required = {"leads_per_day", "creative_type", "traffic_type",
                    "campaign_description", "advertising_image"}
        if set(by_name) != required:
            raise ValueError("maxbounty_submission_packet_five_fields_required")
        match = re.match(r"\s*(\d+)", by_name["leads_per_day"])
        if not match or int(match.group(1)) < 25:
            raise ValueError("maxbounty_leads_per_day_below_25_forbidden")
        if by_name["advertising_image"] != "approved_asset:maxbounty_advertising_image":
            raise ValueError("maxbounty_advertising_image_asset_not_staged")
        snapshot = packet.get("program_snapshot")
        if not isinstance(snapshot, dict) or str(snapshot.get("program_id") or "") != program_id:
            raise ValueError("maxbounty_program_snapshot_missing")
        # Staging must enforce every field the submit path hard-requires, or a packet that
        # can never be applied still reaches the queue. 20 rows staged 2026-08-26 with an
        # empty snapshot application_url and each would have blocked identically at submit.
        if not str(snapshot.get("application_url") or "").startswith("https://"):
            raise ValueError("maxbounty_snapshot_application_url_missing")
    return packet


def queued_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Turn one explicit queue row into one candidate; never infer its identity."""
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise ValueError("queue_payload_invalid_json") from exc
    if not isinstance(payload, dict):
        raise ValueError("queue_payload_not_object")
    network = str(row.get("network") or payload.get("network") or "").strip().lower()
    program_id = str(row.get("program_id") or payload.get("program_id") or "").strip()
    name = str(row.get("program_name") or payload.get("program_name") or "").strip()
    instructions = " ".join(str(row.get("instructions") or "").split())
    authority = str(row.get("operator_authority") or "").strip()
    queue_id = str(row.get("queue_id") or "").strip()
    missing = [key for key, value in (("queue_id", queue_id), ("network", network),
               ("program_id", program_id), ("program_name", name),
               ("instructions", instructions), ("operator_authority", authority)) if not value]
    if missing:
        raise ValueError("queue_fields_missing:" + ",".join(missing))
    submission_packet = validate_submission_packet(
        payload.get("submission_packet"), network=network,
        program_id=program_id, program_name=name)
    return {
        **payload,
        "queue_id": queue_id,
        "network": network,
        "network_label": str(payload.get("network_label") or network),
        "program_id": program_id,
        "name": name,
        "instructions": instructions,
        "operator_authority": authority,
        "submission_packet": submission_packet,
        "application_url": str(payload.get("application_url") or ""),
        "official_url": str(payload.get("official_url") or ""),
        "description": str(payload.get("description") or ""),
        "categories": payload.get("categories") or [],
        "region": str(payload.get("region") or "US"),
        "currency": str(payload.get("currency") or "USD"),
        "lane": str(payload.get("lane") or "glp1-weightloss"),
        "source": "operator_application_queue",
    }


def stop_after_scoped_application_outcome(*, status: str, provider_attempts: int,
                                          attempts_used: int, attempt_cap: int) -> bool:
    """Only provider-terminal/readback-proven zero-attempt rows may advance."""
    if attempts_used >= attempt_cap:
        return True
    return not (
        provider_attempts == 0
        and status in {
            "TERMINAL_PROVIDER_UNAVAILABLE",
            "TERMINAL_ALREADY_APPROVED",
            "PROVIDER_STATE_PENDING_NO_ATTEMPT",
        }
    )


def _executor_results(submission: dict[str, Any], run_dir: Path) -> tuple[list[dict[str, Any]],
                                                                           dict[str, str]]:
    executor_dir = Path(submission.get("run_dir") or "")
    results: list[dict[str, Any]] = []
    if not executor_dir.is_dir():
        return results, {}
    detail_path = executor_dir / "portal-apply-run.json"
    if detail_path.is_file():
        try:
            results = json.loads(detail_path.read_text()).get("applications") or []
        except (OSError, ValueError):
            results = []
    if not results:
        for receipt_file in sorted((executor_dir / "applications").glob("*.json")):
            try:
                results.append(json.loads(receipt_file.read_text()))
            except (OSError, ValueError):
                continue
    return results, collect_evidence(executor_dir, run_dir / "evidence")


def _queue_update(client, queue_id: str, **data: Any) -> None:
    client.update(APPLICATION_QUEUE_TABLE, client.eq("queue_id", queue_id), data)


def _validated_maxbounty_outbox(path_value: str) -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(str(path_value or "").strip())
    if not path.is_absolute():
        path = BUSINESS / path
    path = path.resolve()
    try:
        path.relative_to(APPLICATIONS_DIR.resolve())
    except ValueError as exc:
        raise ValueError("maxbounty_sheet_outbox_outside_applications_root") from exc
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "glp.maxbounty.sheet-queue-outbox.v1":
        raise ValueError("maxbounty_sheet_outbox_schema_invalid")
    row = data.get("sheet_queue_row")
    if not isinstance(row, dict) or row.get("kind") != "application_pending":
        raise ValueError("maxbounty_sheet_outbox_row_invalid")
    if str(row.get("network") or "").strip().casefold() != "maxbounty":
        raise ValueError("maxbounty_sheet_outbox_network_invalid")
    if str(row.get("program_id") or "") != str(data.get("program_id") or ""):
        raise ValueError("maxbounty_sheet_outbox_program_identity_mismatch")
    if str(row.get("update_id") or "") != str(data.get("update_id") or ""):
        raise ValueError("maxbounty_sheet_outbox_update_identity_mismatch")
    action_path = Path(str(data.get("source_action_receipt_path") or ""))
    if not action_path.is_absolute():
        action_path = BUSINESS / action_path
    action_path = action_path.resolve()
    action_path.relative_to(APPLICATIONS_DIR.resolve())
    expected_sha = str(data.get("source_action_receipt_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("maxbounty_sheet_outbox_action_sha_invalid")
    actual_sha = hashlib.sha256(action_path.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        raise ValueError("maxbounty_sheet_outbox_action_sha_drift")
    action = json.loads(action_path.read_text(encoding="utf-8"))
    action_status = str(action.get("status") or "")
    if (action.get("schema") != "glp.maxbounty.application-action.v1"
            and not (action_status == "PROVIDER_STATE_PENDING_NO_ATTEMPT"
                     and action.get("schema")
                     == "glp.maxbounty.provider-application-attempt.v1")):
        raise ValueError("maxbounty_sheet_outbox_action_schema_invalid")
    if action_status not in {
            "SUBMITTED_VERIFIED", "PROVIDER_STATE_PENDING_NO_ATTEMPT"}:
        raise ValueError("maxbounty_sheet_outbox_action_not_verified")
    if str(action.get("program_id") or "") != str(row.get("program_id") or ""):
        raise ValueError("maxbounty_sheet_outbox_action_program_mismatch")
    evidence = json.loads(str(row.get("evidence") or "{}"))
    if (evidence.get("submission_receipt_path") != data.get("source_action_receipt_path")
            or evidence.get("submission_receipt_sha256") != expected_sha):
        raise ValueError("maxbounty_sheet_outbox_evidence_binding_invalid")
    if action_status == "SUBMITTED_VERIFIED":
        if int(action.get("provider_application_attempts") or 0) != 1:
            raise ValueError("maxbounty_sheet_outbox_action_attempt_count_invalid")
    else:
        if (int(action.get("provider_application_attempts") or 0) != 0
                or int(action.get("provider_mutations") or 0) != 0):
            raise ValueError("maxbounty_sheet_outbox_pending_observation_effects_invalid")
        observation_path = Path(str(evidence.get("provider_observation_receipt_path") or ""))
        if not observation_path.is_absolute():
            observation_path = BUSINESS / observation_path
        observation_path = observation_path.resolve()
        observation_path.relative_to(APPLICATIONS_DIR.resolve())
        observation_sha = str(
            evidence.get("provider_observation_receipt_sha256") or "").lower()
        if (not re.fullmatch(r"[0-9a-f]{64}", observation_sha)
                or hashlib.sha256(observation_path.read_bytes()).hexdigest() != observation_sha):
            raise ValueError("maxbounty_sheet_outbox_pending_observation_sha_invalid")
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        detail = observation.get("campaign_detail_state") or {}
        if (observation.get("status") != "PROVIDER_STATE_PENDING_NO_ATTEMPT"
                or str(observation.get("program_id") or "") != str(row.get("program_id") or "")
                or int(observation.get("provider_application_attempts") or 0) != 0
                or int(observation.get("provider_mutations") or 0) != 0
                or detail.get("classification") != "application_pending"
                or not detail.get("pending_markers_seen")):
            raise ValueError("maxbounty_sheet_outbox_pending_observation_invalid")
        if (evidence.get("submitted_at_utc") != action.get("observed_at_utc")
                or "not a submit receipt" not in str(row.get("note") or "")):
            raise ValueError("maxbounty_sheet_outbox_pending_observation_honesty_invalid")
    return data, row


_MAXBOUNTY_APPLIED_FIT_PATH = (
    "reports/ops/orchestration-restore-20260907/PLAN/"
    "MARKETPLACE-ACCESS/M2b/APPLY-LIST.json")
_MAXBOUNTY_APPLIED_FIT_SHA = (
    "339a4c59d278a3bc6b565e49fcd0321c1da88aee8d7320957b51fa9c53d249dc")
_MAXBOUNTY_APPLIED_FIT_IDS = frozenset({
    "24682", "25152", "27296", "29865", "29938", "30641", "30832", "31362",
})
_MAXBOUNTY_SISTER_FIT_REASON = (
    "M2b SISTER fit from frozen captured evidence; provider submission "
    "verified pending. No GLP placement approval is implied.")


def _verified_maxbounty_applied_enrichment(
    outbox: dict[str, Any], original: dict[str, Any], observed: dict[str, Any],
) -> bool:
    """Recognize only the eight hash-bound, already-applied M2b fit enrichments.

    The governed writer may add fit provenance and amend three researched cells;
    it must not silently rewrite the source action or any other outbox evidence.
    This is a readback exception, never permission to insert enriched bytes.
    """
    if (str(observed.get("status") or "").strip().casefold() != "applied"
            or str(original.get("program_id")) not in _MAXBOUNTY_APPLIED_FIT_IDS):
        return False
    try:
        source = json.loads(original["evidence"])
        enriched = json.loads(observed["evidence"])
    except (KeyError, TypeError, ValueError):
        return False
    if not isinstance(source, dict) or not isinstance(enriched, dict):
        return False
    if (enriched.get("fit_assessment_path") != _MAXBOUNTY_APPLIED_FIT_PATH
            or enriched.get("fit_assessment_sha256") != _MAXBOUNTY_APPLIED_FIT_SHA):
        return False
    added = {"fit_assessment_path", "fit_assessment_sha256"}
    if set(enriched) != set(source) | added or added & set(source):
        return False
    if (enriched.get("submission_receipt_path")
            != outbox.get("source_action_receipt_path")
            or enriched.get("submission_receipt_sha256")
            != outbox.get("source_action_receipt_sha256")):
        return False
    for key in source.keys() - {"row_cells"}:
        if enriched[key] != source[key]:
            return False
    cells = source.get("row_cells")
    observed_cells = enriched.get("row_cells")
    if not isinstance(cells, dict) or not isinstance(observed_cells, dict):
        return False
    expected_cells = dict(cells)
    if original["program_id"] in {"24682", "25152", "27296"}:
        if expected_cells.get("1") is not True or "7" not in expected_cells:
            return False
        expected_cells.update({"1": False, "7": _MAXBOUNTY_SISTER_FIT_REASON})
    if original["program_id"] == "27296":
        if expected_cells.get("25") != "out-of-scope":
            return False
        expected_cells["25"] = "supplements-nutrition"
    if observed_cells != expected_cells:
        return False
    # Reuse the writer-side operator binding, not merely a plausible path/hash.
    try:
        import glp_sheet_sync_drain as drain  # noqa: PLC0415
        binding = drain.require_fit_assessment_binding(
            original["network"], original["program_id"], enriched)
    except (OSError, ValueError, KeyError):
        return False
    return binding["verdict"] == (
        "CORE" if original["program_id"] == "30832" else "SISTER")


def persist_maxbounty_sheet_outbox(client, path_value: str) -> dict[str, Any]:
    """Idempotently insert one durable outbox row and independently read it back."""
    data, row = _validated_maxbounty_outbox(path_value)
    existing = client.rows(
        SHEET_UPDATE_QUEUE_TABLE, client.eq("update_id", row["update_id"]), limit=3)
    if len(existing) > 1:
        raise RuntimeError("maxbounty_sheet_queue_duplicate_update_id")
    immutable = (
        "update_id", "kind", "network", "program_id", "program_name", "evidence",
        "queued_by", "queued_at_utc",
    )
    if existing:
        if (any(existing[0].get(key) != row.get(key)
                for key in immutable if key != "evidence")
                or (existing[0].get("evidence") != row.get("evidence")
                    and not _verified_maxbounty_applied_enrichment(
                        data, row, existing[0]))):
            raise RuntimeError("maxbounty_sheet_queue_immutable_readback_drift")
        write = {"write_status": "lifecycle_already_present", "transport": "readback"}
        readback = existing
    else:
        write = client.insert(SHEET_UPDATE_QUEUE_TABLE, row)
        readback = client.rows(
            SHEET_UPDATE_QUEUE_TABLE, client.eq("update_id", row["update_id"]), limit=3)
    if len(readback) != 1:
        raise RuntimeError("maxbounty_sheet_queue_exact_readback_failed")
    if str(readback[0].get("status") or "").strip().casefold() in {"", "queued"}:
        if any(readback[0].get(key) != value for key, value in row.items()):
            raise RuntimeError("maxbounty_sheet_queue_exact_readback_failed")
    return {
        "schema": "glp.maxbounty.sheet-queue-outbox-readback.v1",
        "outbox_path": str(path_value),
        "outbox_sha256": hashlib.sha256(
            (BUSINESS / path_value).read_bytes() if not Path(path_value).is_absolute()
            else Path(path_value).read_bytes()).hexdigest(),
        "update_id": row["update_id"],
        "program_id": row["program_id"],
        "write_status": str(write.get("write_status") or ""),
        "transport": str(write.get("transport") or ""),
        "queue_status": str(readback[0].get("status") or ""),
        "exact_readback_rows": 1,
        "sheet_direct_writes": 0,
    }


def recover_maxbounty_sheet_outboxes(client) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    if not APPLICATIONS_DIR.is_dir():
        return receipts
    # A prebound handoff manifest exists before any provider submit. If the
    # provider attempt reached SUBMITTED_VERIFIED but the process died before
    # writing the action/outbox, reconstruct those exact bytes without replaying
    # the provider mutation.
    import glp_maxbounty_lane as maxbounty  # noqa: PLC0415
    for path in sorted(APPLICATIONS_DIR.glob("**/provider-reconciliation-*.json")):
        maxbounty.reconcile_application_handoff(path)
    for path in sorted(APPLICATIONS_DIR.glob("**/application-handoff-*.json")):
        maxbounty.materialize_application_handoff(path)
    for path in sorted(APPLICATIONS_DIR.glob("**/sheet-queue-outbox-*.json")):
        receipts.append(persist_maxbounty_sheet_outbox(client, repo_relative(path)))
    return receipts


def main(argv: list[str] | None = None, *, queue_client=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("drain", "plan"), default="drain")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    plan_only = args.mode == "plan"
    run_id = args.run_id or dt.datetime.now(dt.timezone.utc).strftime(
        "application-queue-%Y%m%dT%H%M%SZ")
    run_dir = APPLICATIONS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = run_dir / "run.json"

    APPLICATION_MACHINE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = APPLICATION_MACHINE_LOCK.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        receipt = {
            "schema": RUN_RECEIPT_SCHEMA,
            "contract_schema": RUN_RECEIPT_CONTRACT_SCHEMA,
            "run_id": run_id, "captured_at_utc": utc_now(),
            "status": "LOCK_CONTENDED_NO_EFFECT", "mode": args.mode,
            "queue_items": 0, "results": [], "sheet_queue_rows": [],
            "effects": {"applications_submitted": 0, "emails_sent": 0,
                        "provider_deploys": 0, "sheet_direct_writes": 0,
                        "provider_application_attempts": 0,
                        "maxbounty_application_attempts": 0,
                        "other_network_application_attempts": 0},
        }
        _finish_run_receipt(receipt_path, receipt)
        print(json.dumps({
            "schema": "glp-application-queue-drain-summary-v1", "run_id": run_id,
            "status": receipt["status"], "queue_items": 0, "processed": 0,
            "submitted": 0, "discord_text": "", "queue_rows": [],
            "receipt": repo_relative(receipt_path),
        }, sort_keys=True))
        return 0

    # Write STARTED before touching the queue client. The CLI boundary replaces
    # this marker on a catchable signal or uncaught Python exception; only SIGKILL,
    # native interpreter failure, or a receipt I/O failure can leave it STARTED.
    def _mark(phase: str) -> None:
        try:
            _begin_run_receipt(
                receipt_path, run_id=run_id, mode=args.mode, phase=phase)
        except OSError:
            pass  # never let instrumentation break the run

    _mark("queue_client_connect")
    client = queue_client or _application_queue_client()
    sheet_outbox_receipts = []
    if not plan_only:
        _mark("sheet_outbox_recovery")
        sheet_outbox_receipts = recover_maxbounty_sheet_outboxes(client)
    _mark("queue_read")
    queued_rows = read_application_queue(client)

    if not queued_rows:
        receipt = {
            "schema": RUN_RECEIPT_SCHEMA,
            "contract_schema": RUN_RECEIPT_CONTRACT_SCHEMA, "run_id": run_id,
            "captured_at_utc": utc_now(), "status": "HEALTHY_EMPTY",
            "mode": args.mode, "queue_items": 0, "results": [], "sheet_queue_rows": [],
            "sheet_queue_outbox_receipts": sheet_outbox_receipts,
            "queue_read_observations": _LAST_QUEUE_READ_OBSERVATIONS,
            "effects": {"applications_submitted": 0, "emails_sent": 0,
                        "provider_deploys": 0, "sheet_direct_writes": 0},
        }
        _finish_run_receipt(receipt_path, receipt)
        print(json.dumps({
            "schema": "glp-application-queue-drain-summary-v1", "run_id": run_id,
            "status": "HEALTHY_EMPTY", "queue_items": 0, "processed": 0,
            "discord_text": "", "queue_rows": [],
            "sheet_queue_outboxes_reconciled": len(sheet_outbox_receipts),
            "receipt": repo_relative(receipt_path),
        }, sort_keys=True))
        return 0

    # The apply gate. Checked AFTER the queue read so a held run reports how
    # much is waiting, and reported as its own status so "forbidden to act"
    # never again looks identical to "nothing to do" -- that ambiguity is how a
    # supposedly paused machine went unnoticed as still-armed for a week.
    # Nothing is claimed, submitted, emailed or written while the gate is shut.
    _mark("apply_gate")
    gate_block = apply_gate.blocking_reason()
    if gate_block:
        held = [
            {"queue_id": str(row.get("queue_id") or ""),
             "program": str(row.get("program_name") or row.get("program") or ""),
             "network": str(row.get("network") or "")}
            for row in queued_rows
        ]
        receipt = {
            "schema": RUN_RECEIPT_SCHEMA,
            "contract_schema": RUN_RECEIPT_CONTRACT_SCHEMA, "run_id": run_id,
            "captured_at_utc": utc_now(), "status": "PAUSED_BY_APPLY_GATE",
            "mode": args.mode, "queue_items": len(queued_rows), "results": [],
            "sheet_queue_rows": [], "gate_reason": gate_block,
            "sheet_queue_outbox_receipts": sheet_outbox_receipts,
            "held_without_applying": held,
            "effects": {"applications_submitted": 0, "emails_sent": 0,
                        "provider_deploys": 0, "sheet_direct_writes": 0},
        }
        _finish_run_receipt(receipt_path, receipt)
        print(json.dumps({
            "schema": "glp-application-queue-drain-summary-v1", "run_id": run_id,
            "status": "PAUSED_BY_APPLY_GATE", "queue_items": len(queued_rows),
            "processed": 0, "gate_reason": gate_block,
            "discord_text": "", "queue_rows": [],
            "sheet_queue_outboxes_reconciled": len(sheet_outbox_receipts),
            "receipt": repo_relative(receipt_path),
        }, sort_keys=True))
        return 0

    gate_state = apply_gate.gate_state()
    scope = gate_state.get("scope") or {}
    allowed_networks = set(scope.get("networks_allowed") or [])
    allowed_queue_ids = set(scope.get("queue_ids_allowed") or [])
    max_applications = int(scope.get("max_applications") or 0)
    consumed_before_run = apply_gate.application_attempts_consumed()
    remaining_applications = max(0, max_applications - consumed_before_run)
    scoped_rows = [
        row for row in queued_rows
        if (str(row.get("network") or "").strip().lower() in allowed_networks
            and (not allowed_queue_ids
                 or str(row.get("queue_id") or "").strip() in allowed_queue_ids))
    ]
    # The cap governs provider ATTEMPTS, not queue rows. A provider-terminal row
    # proven without a submit (for example, MaxBounty's campaign API returns 404)
    # must be resolved and skipped without consuming the one allowed attempt.
    # Generic zero-attempt failures still stop the run below, so defects cannot
    # burn through the backlog.
    selected_rows = scoped_rows if remaining_applications else []
    scope_held = [
        {"queue_id": str(row.get("queue_id") or ""),
         "program": str(row.get("program_name") or row.get("program") or ""),
         "network": str(row.get("network") or "")}
        for row in queued_rows if row not in scoped_rows
    ]
    cap_held = [
        {"queue_id": str(row.get("queue_id") or ""),
         "program": str(row.get("program_name") or row.get("program") or ""),
         "network": str(row.get("network") or "")}
        for row in scoped_rows if not remaining_applications
    ]
    if not selected_rows:
        receipt = {
            "schema": RUN_RECEIPT_SCHEMA,
            "contract_schema": RUN_RECEIPT_CONTRACT_SCHEMA, "run_id": run_id,
            "captured_at_utc": utc_now(), "status": "NO_ITEMS_IN_OPERATOR_SCOPE",
            "mode": args.mode, "queue_items": len(queued_rows), "results": [],
            "sheet_queue_rows": [], "gate_scope": scope,
            "sheet_queue_outbox_receipts": sheet_outbox_receipts,
            "provider_attempts_consumed_before_run": consumed_before_run,
            "provider_attempts_remaining_before_run": remaining_applications,
            "scope_held_without_applying": scope_held,
            "cap_held_without_applying": cap_held,
            "effects": {"applications_submitted": 0, "emails_sent": 0,
                        "provider_deploys": 0, "sheet_direct_writes": 0,
                        "provider_application_attempts": 0,
                        "maxbounty_application_attempts": 0,
                        "other_network_application_attempts": 0},
        }
        _finish_run_receipt(receipt_path, receipt)
        print(json.dumps({
            "schema": "glp-application-queue-drain-summary-v1", "run_id": run_id,
            "status": receipt["status"], "queue_items": len(queued_rows),
            "processed": 0, "submitted": 0, "discord_text": "", "queue_rows": [],
            "sheet_queue_outboxes_reconciled": len(sheet_outbox_receipts),
            "receipt": repo_relative(receipt_path),
        }, sort_keys=True))
        return 0

    nfilter = _import_repo_modules()
    values = read_sheet()
    index = sheet_index(values)
    rows_by_name = sheet_row_lookup(values)
    replay_index = load_queued_index()
    outcomes: list[dict[str, Any]] = []
    sheet_queue_rows: list[dict[str, Any]] = []
    workflow_queue_rows: list[dict[str, Any]] = []
    total_effects = {"applications_submitted": 0, "emails_sent": 0,
                     "provider_deploys": 0, "sheet_direct_writes": 0,
                     "provider_application_attempts": 0,
                     "maxbounty_application_attempts": 0,
                     "other_network_application_attempts": 0}
    processed_scoped = 0
    unprocessed_after_block: list[dict[str, Any]] = []

    for raw_row in selected_rows:
        processed_scoped += 1
        stop_after_current = True
        queue_id = str(raw_row.get("queue_id") or "").strip()
        item_dir = run_dir / re.sub(r"[^A-Za-z0-9_.-]+", "-", queue_id or "invalid")
        item_dir.mkdir(parents=True, exist_ok=True)
        try:
            candidate = queued_candidate(raw_row)
            queue_id = candidate["queue_id"]
            if not plan_only:
                _queue_update(client, queue_id, status="claimed", claimed_at_utc=utc_now(),
                              receipt=repo_relative(receipt_path))
            if candidate["network"] == "maxbounty":
                from scripts import glp_maxbounty_lane as maxbounty  # noqa: E402
                if plan_only:
                    result = {"status": "PLAN_ONLY", "queue_rows": [],
                              "effects": {"applications_submitted": 0,
                                          "provider_deploys": 0}}
                else:
                    result = maxbounty.execute_queued_application(
                        candidate["program_id"], queue_id=queue_id,
                        instructions=candidate["instructions"], run_dir=item_dir,
                        submission_packet=candidate["submission_packet"])
                status = str(result.get("status") or "BLOCKED")
                item_sheet_rows = result.get("queue_rows") or []
            else:
                verdicts = gate_and_dedupe([candidate], index, replay_index, nfilter)
                if not verdicts["accepted"]:
                    reason = (verdicts["skipped"] or [{}])[0].get("reason") or "fit_or_dedupe_refused"
                    result = {"status": "BLOCKED", "blocker": reason,
                              "queue_rows": [], "effects": {"applications_submitted": 0}}
                    status = "BLOCKED"
                    item_sheet_rows = []
                elif plan_only:
                    result = {"status": "PLAN_ONLY", "queue_rows": [],
                              "effects": {"applications_submitted": 0}}
                    status = "PLAN_ONLY"
                    item_sheet_rows = []
                else:
                    accepted = verdicts["accepted"][0]
                    submission = submit(enqueue_ledger([accepted], run_id), 1, run_id, item_dir)
                    results, evidence = _executor_results(submission, item_dir)
                    accepted_key = f"{accepted['network']}:{accepted['program_id']}"
                    item_sheet_rows = queue_rows_for(
                        results, {accepted_key: accepted}, rows_by_name,
                        repo_relative(receipt_path), run_id)
                    result = {**submission, "results": results,
                              "evidence_files": evidence, "queue_rows": item_sheet_rows,
                              "effects": {"applications_submitted": len(item_sheet_rows)}}
                    status = ("SUBMITTED_VERIFIED" if item_sheet_rows else
                              str(submission.get("blocker") or "BLOCKED"))
            effects = result.get("effects") or {}
            for key in ("applications_submitted", "emails_sent",
                        "provider_deploys", "sheet_direct_writes"):
                total_effects[key] += int(effects.get(key) or 0)
            provider_attempts = int(effects.get("provider_application_attempts") or 0)
            total_effects["provider_application_attempts"] += provider_attempts
            if candidate["network"] == "maxbounty":
                total_effects["maxbounty_application_attempts"] += provider_attempts
            else:
                total_effects["other_network_application_attempts"] += (
                    provider_attempts or int(effects.get("applications_submitted") or 0))
            sheet_handoff_error = ""
            outbox_readback = None
            provider_pending_observed = status == "PROVIDER_STATE_PENDING_NO_ATTEMPT"
            if candidate["network"] == "maxbounty" and item_sheet_rows:
                outbox = result.get("sheet_queue_outbox") or {}
                try:
                    outbox_readback = persist_maxbounty_sheet_outbox(
                        client, str(outbox.get("path") or ""))
                    sheet_outbox_receipts.append(outbox_readback)
                except Exception as exc:  # provider attempt is already durable; never replay it
                    sheet_handoff_error = (
                        f"{type(exc).__name__}:{str(exc)[:400]}")
                    status = ("PROVIDER_PENDING_SHEET_QUEUE_BLOCKED"
                              if provider_pending_observed
                              else "PROVIDER_SUBMITTED_SHEET_QUEUE_BLOCKED")
                    result["blocker"] = sheet_handoff_error
            elif item_sheet_rows:
                workflow_queue_rows.extend(item_sheet_rows)
            sheet_queue_rows.extend(item_sheet_rows)
            outcome = {"queue_id": queue_id, "network": candidate["network"],
                       "program_id": candidate["program_id"], "program_name": candidate["name"],
                       "status": status, "blocker": result.get("blocker", ""),
                       "sheet_queue_rows": len(item_sheet_rows),
                       "effects": effects,
                       "provider_application_attempts": provider_attempts,
                       "provider_receipt": result.get("receipt")
                           or result.get("attempt_receipt", ""),
                       "provider_receipt_sha256": result.get("sha256")
                           or result.get("attempt_receipt_sha256", ""),
                       "sheet_queue_outbox_readback": outbox_readback}
            outcomes.append(outcome)
            if not plan_only:
                if int(effects.get("applications_submitted") or 0):
                    final = ("provider_submitted_sheet_queue_blocked" if sheet_handoff_error
                             else "provider_submitted_sheet_pending")
                elif provider_pending_observed and not sheet_handoff_error:
                    final = "provider_observed_sheet_pending"
                else:
                    final = "blocked"
                _queue_update(client, queue_id, status=final, resolved_at_utc=utc_now(),
                              receipt=repo_relative(receipt_path), note=status[:500])
            stop_after_current = stop_after_scoped_application_outcome(
                status=status,
                provider_attempts=provider_attempts,
                attempts_used=total_effects["provider_application_attempts"],
                attempt_cap=remaining_applications,
            )
        except Exception as exc:  # one queued item cannot strand the rest
            blocker = f"{type(exc).__name__}:{str(exc)[:500]}"
            outcomes.append({"queue_id": queue_id, "status": "BLOCKED", "blocker": blocker,
                             "sheet_queue_rows": 0,
                             "effects": {"applications_submitted": 0}})
            if queue_id and not plan_only:
                _queue_update(client, queue_id, status="blocked", resolved_at_utc=utc_now(),
                              receipt=repo_relative(receipt_path), note=blocker)
            stop_after_current = True
        if stop_after_current:
            break

    remaining_scoped_rows = scoped_rows[processed_scoped:]
    if total_effects["provider_application_attempts"] >= remaining_applications:
        cap_held = [
            {"queue_id": str(row.get("queue_id") or ""),
             "program": str(row.get("program_name") or row.get("program") or ""),
             "network": str(row.get("network") or "")}
            for row in remaining_scoped_rows
        ]
    else:
        unprocessed_after_block = [
            {"queue_id": str(row.get("queue_id") or ""),
             "program": str(row.get("program_name") or row.get("program") or ""),
             "network": str(row.get("network") or "")}
            for row in remaining_scoped_rows
        ]

    receipt = {
        "schema": RUN_RECEIPT_SCHEMA,
        "contract_schema": RUN_RECEIPT_CONTRACT_SCHEMA, "run_id": run_id,
        "captured_at_utc": utc_now(),
        "status": ("PLAN_ONLY" if plan_only else
                   ("COMPLETE_WITH_BLOCKERS" if any(
                       item.get("blocker") and item.get("status") not in {
                           "TERMINAL_PROVIDER_UNAVAILABLE", "TERMINAL_ALREADY_APPROVED"}
                       for item in outcomes)
                    else "COMPLETE")),
        "mode": args.mode, "queue_items": len(queued_rows), "results": outcomes,
        "sheet_queue_rows": sheet_queue_rows, "effects": total_effects,
        "sheet_queue_outbox_receipts": sheet_outbox_receipts,
        "gate_scope": scope,
        "provider_attempts_consumed_before_run": consumed_before_run,
        "provider_attempts_remaining_before_run": remaining_applications,
        "selected_queue_items": processed_scoped,
        "scope_held_without_applying": scope_held,
        "cap_held_without_applying": cap_held,
        "unprocessed_after_block": unprocessed_after_block,
        "queue_read_observations": _LAST_QUEUE_READ_OBSERVATIONS,
    }
    _finish_run_receipt(receipt_path, receipt)
    discord_lines = [f"**applications queue** — {len(outcomes)} operator-queued item(s) processed"]
    for item in outcomes[:12]:
        line = f"• {item.get('program_name') or item.get('queue_id')}: {item['status']}"
        if item.get("blocker"):
            line += f" ({item['blocker']})"
        discord_lines.append(line)
    discord_lines.append(f"receipt: {repo_relative(receipt_path)}")
    summary = {
        "schema": "glp-application-queue-drain-summary-v1", "run_id": run_id,
        "status": receipt["status"], "queue_items": len(queued_rows),
        "processed": len(outcomes), "submitted": total_effects["applications_submitted"],
        "discord_text": "\n".join(discord_lines)[:1900],
        "queue_rows": workflow_queue_rows,
        "sheet_queue_outboxes_reconciled": len(sheet_outbox_receipts),
        "receipt": repo_relative(receipt_path),
    }
    print(json.dumps(summary, sort_keys=True, default=str))
    return 0


def run_cli(argv: list[str] | None = None, *, queue_client=None) -> int:
    """CLI boundary that receipts catchable signals and uncaught exceptions."""
    previous = _install_termination_handlers()
    try:
        return main(argv, queue_client=queue_client)
    except SystemExit:
        # SIGTERM/SIGINT/SIGHUP are stamped by their handler before SystemExit.
        raise
    except BaseException as exc:
        _stamp_termination(
            "exception", exception_type=type(exc).__name__, message=str(exc)[:500],
            exit_code=1)
        raise
    finally:
        _restore_termination_handlers(previous)


if __name__ == "__main__":
    raise SystemExit(run_cli())
