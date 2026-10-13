#!/usr/bin/env python3
"""PER-CYCLE FETCH RECEIPTS — the evidence that a capture was fetched by THIS
cycle, which is what the clean-room guard was always trying to ask.

THE DEFECT THIS CLOSES (Blocker B, nuimage-finish-20260820)
-----------------------------------------------------------
The clean-room guard fingerprinted every pre-existing artifact that mentions a
provider and refused any cited capture whose sha256 was already in that set.
It was built against a real quarantine class — a KILLED-MANUAL-RUN's
hand-planted captures that a capable agent would happily "produce" again — and
it caught exactly that.

But its fingerprint root, `reports/glp-clinic-scout/affiliate-machine`, also
contains **every previous cycle's own capture directory**. So:

    cycle 1 fetches nuimagemedical.com   -> writes captures/nuimagemedical.com.md
    cycle 2 fetches the SAME page again  -> byte-identical markdown
    guard: "sha 5a2df055… already existed on disk"  -> FAILED / evidence_gap

Two consequences, both proven byte-for-byte in that report: the guard's own
output is its next input, and **a provider whose pages are stable can never
satisfy it**. An unchanged marketing page is the expected steady state — it is
evidence the capture is HONEST, not evidence it was reused.

THE DISCRIMINATION (never a weakening)
--------------------------------------
Bytes cannot tell "re-fetched an unchanged page" from "copied a file". A
*receipt of the fetch itself* can:

  * the capture step records, for every URL it fetches, a receipt carrying the
    cycle id it was fetched under, the timestamp, the transport route, the HTTP
    status, a hash of the REQUEST it made (method/endpoint/url/header NAMES/
    body keys + a per-request nonce — never a header VALUE, so no key can leak)
    and a hash of the RESPONSE evidence (Firecrawl's own response headers, or
    the browser route's navigation status + raw-HTML hash).
  * a cited capture whose content hash carries a valid receipt from THIS cycle
    is legitimate **regardless of matching historical bytes**.
  * a cited capture that matches history and has NO valid this-cycle receipt is
    the actual reuse case, and still fails.

The response-evidence hash is the load-bearing half: it is derived from bytes
the network returned during this cycle and cannot be reconstructed from
anything on disk. A receipt without it is not a receipt, and a hand-forged
receipt that omits it is refused by `validate_receipt` — which is what keeps a
quarantined hand-artifact failing even when someone writes a receipt next to it.

This module is the ONE definition of the receipt shape and of the cycle id, so
the writer (`g6_r1a_stage5_research_fetch.py`) and the reader
(`glp_onboarding_authoring_dispatch.py`) can never drift apart.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

#: The file the capture step writes into the cycle's own capture root. The
#: authoring brief points the engine's `--out` at that directory, so the
#: receipts land where the guard already knows to look.
RECEIPTS_FILENAME = "fetch-receipts.json"

INDEX_SCHEMA = "glp.capture_fetch_receipts.v1"
RECEIPT_SCHEMA = "glp.capture_fetch_receipt.v1"

#: Routes that may produce a legitimate first-party capture.
VALID_ROUTES = ("firecrawl", "headful_chrome")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: A capture root looks like
#:   …/onboarding-manifest/runs/<run_id>/authoring/<provider>-c<N>/captures
#: and the cycle it belongs to is exactly (<run_id>, <provider>-c<N>).
_CAPTURE_ROOT = re.compile(
    r"runs/(?P<run_id>[^/]+)/authoring/(?P<cycle_dir>[^/]+)(?:/captures)?/?$")


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------
# The cycle id — one derivation, used by the writer and the reader
# ---------------------------------------------------------------------------


def cycle_id_for_capture_root(capture_root: str | Path) -> str:
    """Stable id for the authoring cycle that owns this capture directory.

    Derived from the PATH the machine itself minted in `capture_root_for()`, so
    the capture step needs no new argument threaded through the brief and the
    guard needs no new field in the ledger: both read the same directory name.
    A path that does not look like a machine capture root falls back to its last
    two components, which is still stable and still cycle-specific.
    """
    raw = str(capture_root or "").replace("\\", "/").rstrip("/")
    if not raw:
        return ""
    match = _CAPTURE_ROOT.search(raw)
    if match:
        return "%s:%s" % (match.group("run_id"), match.group("cycle_dir"))
    parts = [p for p in raw.split("/") if p]
    return ":".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "")


# ---------------------------------------------------------------------------
# Building a receipt (writer side)
# ---------------------------------------------------------------------------


def new_nonce() -> str:
    """Minted BEFORE the request and folded into the request hash, so a receipt
    cannot be reconstructed after the fact from the URL alone."""
    return secrets.token_hex(16)


def request_descriptor(*, route: str, method: str, endpoint: str, url: str,
                       header_names: list | tuple = (),
                       body_keys: list | tuple = (),
                       nonce: str = "", **extra: Any) -> dict:
    """What we asked for, with NOTHING secret in it.

    Header NAMES only — an `Authorization` value would put the Firecrawl key in
    a receipt that gets read into briefs, reports and n8n rows. The rule the
    charter states ("a secret value never appears in a receipt") is enforced by
    shape here, not by remembering."""
    desc = {
        "route": str(route),
        "method": str(method).upper(),
        "endpoint": str(endpoint),
        "url": str(url),
        "header_names": sorted(str(h).lower() for h in header_names),
        "body_keys": sorted(str(b) for b in body_keys),
        "nonce": str(nonce),
    }
    for key, value in extra.items():
        desc[str(key)] = value
    return desc


def request_evidence_sha256(descriptor: dict) -> str:
    return _sha256_text(canonical_json(descriptor))


def response_evidence_sha256(evidence: dict) -> str:
    """Hash of bytes the NETWORK returned this cycle.

    This is the half that cannot be reproduced from disk: response headers carry
    per-request values (date, request ids, cache markers) and the browser route
    carries the raw-HTML hash and the navigation status. Reconstructing it
    without having made the request is the thing we are making hard."""
    return _sha256_text(canonical_json(evidence))


def make_receipt(*, cycle_id: str, url: str, fetched_at_utc: str,
                 content_sha256: str, route: str, http_status: Any,
                 descriptor: dict, response_evidence: dict,
                 chars: int | None = None, final_url: str = "") -> dict:
    return {
        "schema": RECEIPT_SCHEMA,
        "cycle_id": str(cycle_id or ""),
        "url": str(url or ""),
        "final_url": str(final_url or ""),
        "fetched_at_utc": str(fetched_at_utc or ""),
        "capture_route": str(route or ""),
        "http_status": http_status,
        "chars": chars,
        "content_sha256": str(content_sha256 or "").strip().casefold(),
        "request_descriptor": descriptor,
        "request_evidence_sha256": request_evidence_sha256(descriptor),
        "response_evidence": response_evidence,
        "response_evidence_sha256": response_evidence_sha256(response_evidence),
    }


# ---------------------------------------------------------------------------
# Persisting and reading the index
# ---------------------------------------------------------------------------


def index_path(capture_root: str | Path) -> Path:
    return Path(capture_root) / RECEIPTS_FILENAME


def load_index(capture_root: str | Path) -> dict:
    try:
        data = json.loads(index_path(capture_root).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_index(capture_root: str | Path, cycle_id: str, receipts: list,
                *, merge: bool = True) -> Path:
    """Write (or extend) the cycle's receipt index.

    MERGE by default: a cycle may call the capture step more than once — a
    second spec, a retry, a late page — and a run that clobbered its own earlier
    receipts would hand the guard a half-empty cycle and re-create the very
    false positive this closes."""
    path = index_path(capture_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list = []
    if merge:
        prior = load_index(capture_root)
        if str(prior.get("cycle_id") or "") == str(cycle_id or ""):
            existing = [r for r in (prior.get("receipts") or [])
                        if isinstance(r, dict)]
    by_key: dict[tuple, dict] = {}
    for rec in list(existing) + [r for r in receipts if isinstance(r, dict)]:
        by_key[(str(rec.get("url") or ""),
                str(rec.get("content_sha256") or ""))] = rec
    payload = {
        "schema": INDEX_SCHEMA,
        "cycle_id": str(cycle_id or ""),
        "written_at_utc": now_utc(),
        "receipts": sorted(by_key.values(),
                           key=lambda r: (str(r.get("url") or ""),
                                          str(r.get("fetched_at_utc") or ""))),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Validating a receipt (reader / guard side)
# ---------------------------------------------------------------------------


def _parse_utc(stamp: str) -> dt.datetime | None:
    raw = str(stamp or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


#: A fetch that happened before the cycle was dispatched is not this cycle's
#: fetch. Small slack absorbs clock skew between the box and the bridge without
#: opening a window a previous cycle could fit through.
DISPATCH_SLACK_S = 300


def validate_receipt(receipt: Any, cycle_id: str = "",
                     dispatched_utc: str = "") -> tuple[bool, str]:
    """(ok, reason). Every failure reason is a named, reportable class.

    Deliberately strict about EVIDENCE and deliberately silent about content: a
    receipt proves a fetch happened, and whether the bytes match history is none
    of its business — that is exactly the discrimination this closes."""
    if not isinstance(receipt, dict):
        return False, "not_a_receipt"
    if str(receipt.get("schema") or "") != RECEIPT_SCHEMA:
        return False, "wrong_schema"
    if cycle_id and str(receipt.get("cycle_id") or "") != str(cycle_id):
        return False, "foreign_cycle"
    if not str(receipt.get("cycle_id") or "").strip():
        return False, "no_cycle_id"
    if not str(receipt.get("url") or "").strip():
        return False, "no_url"
    fetched = _parse_utc(receipt.get("fetched_at_utc"))
    if fetched is None:
        return False, "no_fetched_at"
    if str(receipt.get("capture_route") or "") not in VALID_ROUTES:
        return False, "unknown_route"
    for field in ("content_sha256", "request_evidence_sha256",
                  "response_evidence_sha256"):
        if not _HEX64.match(str(receipt.get(field) or "").strip().casefold()):
            return False, "missing_" + field
    descriptor = receipt.get("request_descriptor")
    if not isinstance(descriptor, dict) or not descriptor:
        return False, "no_request_descriptor"
    if request_evidence_sha256(descriptor) != str(
            receipt.get("request_evidence_sha256")).strip().casefold():
        return False, "request_evidence_mismatch"
    evidence = receipt.get("response_evidence")
    if not isinstance(evidence, dict) or not evidence:
        return False, "no_response_evidence"
    if response_evidence_sha256(evidence) != str(
            receipt.get("response_evidence_sha256")).strip().casefold():
        return False, "response_evidence_mismatch"
    # A secret must never reach a receipt. Belt-and-braces on the shape rule
    # above, because this file is read into briefs and operator cards.
    if any(k.lower() in ("authorization", "api_key", "apikey", "token", "bearer")
           for k in descriptor):
        return False, "secret_in_descriptor"
    started = _parse_utc(dispatched_utc)
    if started and fetched < started - dt.timedelta(seconds=DISPATCH_SLACK_S):
        return False, "fetched_before_dispatch"
    return True, "ok"


def valid_receipts_by_sha(capture_root: str | Path, cycle_id: str = "",
                          dispatched_utc: str = "") -> dict:
    """content_sha256 -> the valid this-cycle receipt that produced it."""
    index = load_index(capture_root)
    cycle = str(cycle_id or index.get("cycle_id") or "")
    out: dict[str, dict] = {}
    for receipt in index.get("receipts") or []:
        ok, _why = validate_receipt(receipt, cycle, dispatched_utc)
        if not ok:
            continue
        sha = str(receipt.get("content_sha256") or "").strip().casefold()
        if sha:
            out.setdefault(sha, receipt)
    return out


def receipt_rejections(capture_root: str | Path, cycle_id: str = "",
                       dispatched_utc: str = "") -> list:
    """Why each rejected receipt was rejected — an honest failure needs the
    reason, not just the count."""
    index = load_index(capture_root)
    cycle = str(cycle_id or index.get("cycle_id") or "")
    out = []
    for receipt in index.get("receipts") or []:
        ok, why = validate_receipt(receipt, cycle, dispatched_utc)
        if not ok:
            out.append({"url": (receipt or {}).get("url") if isinstance(receipt, dict) else "",
                        "content_sha256": (receipt or {}).get("content_sha256")
                        if isinstance(receipt, dict) else "",
                        "rejected_because": why})
    return out
