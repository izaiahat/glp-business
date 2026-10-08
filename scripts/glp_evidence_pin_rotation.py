#!/usr/bin/env python3
"""Rotate ONE capture's independent pin to its current authenticated identity, on a named decision.

WHY THIS EXISTS
Three pinned captures (ro, plushcare, ftc) were legitimately re-fetched, so their ledger rows carry
new scrape ids while ACCEPTED_CAPTURE_PINS still names the superseded fetches. The build refuses at
`accepted_capture_pin_invalid`. Nothing else about those rows is wrong: their quote hashes, urls and
status all still match their pins, and their capture clocks are hours old.

WHAT WENT WRONG THE FIRST TIME (all closed below, from ROTATION-REVIEW-RESULT.json)
- T1: a fixed 12-line lookahead with no block-end guard rewrote the WRONG capture's pin and logged it
  as the right one. Now bounded by the block's own closing brace, and the matched block is re-checked
  to belong to the named capture.
- T2: the "decision receipt" was any JSON with seven truthy keys. Now schema-checked, must live under
  ops/linear/approvals/, and must carry the evidence fields.
- T3: status_code was never compared despite being named. Now compared, with every other pinned field.
- T6: the id was shape-checked only. Now the strongest available OUTSIDE-the-chain check is applied:
  a Firecrawl id is UUIDv7 and embeds its issue time, so it must agree with the capture's own
  fetch_started_at. The forged id the prior review planted was a v4 reading year 9728.
- The "exactly one line changed" proof compared two in-memory strings and could never fire. It now
  re-reads the file from disk and diffs it.

WHAT IT STILL WILL NOT DO
Move a quote, a url or a status; rotate a capture the decision does not name; rotate to an id the
authenticated chain does not carry; or run without a decision receipt. Identity only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import sys

BUSINESS = Path("/home/ubuntu/business")
APPROVALS = BUSINESS / "ops/linear/approvals"
CANDIDATES = ("site-system/flagship-v2/candidates/glp34-provider-families-20260912",
              "site-system/flagship-v2/candidates/glp208-source-refresh")
RECEIPTS = BUSINESS / "ops/evidence-freshness/pin-rotations.jsonl"
UUID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-7[a-f0-9]{3}-[a-f0-9]{4}-[a-f0-9]{12}$")
REQUIRED = ("schema", "capture_id", "new_scrape_id", "authority", "operator_words_verbatim",
            "evidence_reviewed", "why_this_capture_must_move")
SCHEMA = "glp-pin-rotation-decision-v1"
MAX_ID_SKEW_S = 120


def firecrawl_attests(scrape_id: str, expect_url: str) -> dict:
    """Ask FIRECRAWL whether this scrape exists and what it fetched. First-party, outside the chain.

    This is the check the 2026-09-16 rotation review said was missing. Every other signal available
    to this tool reads out of capture.json, whose only authentication is its own sha256 -- so it
    cannot distinguish an authentic capture from a forged one. GET /v2/scrape/{id} can: the three
    real ids return 200 with their pinned source URL, and the forged id that review planted
    (deadbeef-dead-4ead-8ead-deaddeaddead) returns 404.
    """
    import urllib.error
    import urllib.request
    sys.path.insert(0, str(BUSINESS / "scripts"))
    from affiliate_direct_evidence_capture import _firecrawl_key
    request = urllib.request.Request(f"https://api.firecrawl.dev/v2/scrape/{scrape_id}",
                                     headers={"Authorization": "Bearer " + _firecrawl_key()})
    try:
        from firecrawl_client import firecrawl_urlopen
        with firecrawl_urlopen(request, caller='glp_evidence_pin_rotation', timeout=45) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        return {"attested": False, "http_status": exc.code,
                "reason": "Firecrawl does not know this scrape id"}
    except Exception as exc:                 # noqa: BLE001 - an unverifiable id is not a rotatable id
        return {"attested": False, "reason": f"attestation unreachable: {type(exc).__name__}"}
    data = body.get("data") or {}
    meta = data.get("metadata") or {}
    source = meta.get("sourceURL")
    if source != expect_url:
        return {"attested": False, "http_status": 200, "source_url": source,
                "reason": "Firecrawl attests a DIFFERENT url than this capture's pinned requested_url"}
    if meta.get("statusCode") != 200:
        return {"attested": False, "http_status": 200, "attested_status": meta.get("statusCode"),
                "reason": "Firecrawl attests a non-200 fetch"}
    body_text = data.get("markdown")
    digest = hashlib.sha256(body_text.encode()).hexdigest() if isinstance(body_text, str) else None
    return {"attested": True, "http_status": 200, "source_url": source,
            "attested_status_code": meta.get("statusCode"), "attested_content_sha256": digest}


def uuid7_time(value: str) -> datetime:
    """A UUIDv7's first 48 bits are its issue time in milliseconds.

    CORROBORATION ONLY. Both compared values live in capture.json, which the same author writes, so
    agreement here proves self-consistency and nothing more. The BASIS is the Firecrawl attestation.
    """
    return datetime.fromtimestamp(int(value.replace("-", "")[:12], 16) / 1000, timezone.utc)


def chain_state(capture_id: str) -> tuple[dict, dict]:
    sys.path.insert(0, str(BUSINESS / "scripts"))
    import glp_evidence_refresh_cycle as cycle
    inputs = cycle.load_inputs()
    ledger = json.loads(cycle.bound(inputs, "ledger"))
    row = next((r for r in ledger["records"] if r["capture_id"] == capture_id), None)
    if row is None:
        raise SystemExit(json.dumps({"status": "REFUSED", "reason": "capture_id not in the ledger"}))
    binding = (inputs.get("captures") or {}).get(capture_id)
    if not binding:
        raise SystemExit(json.dumps({"status": "REFUSED",
                                     "reason": "capture has no adopted capture in the chain"}))
    return row, json.loads(cycle.bound(binding, "capture"))


def pin_block(text: str, capture_id: str) -> tuple[int, dict]:
    """Locate the capture's pin block, bounded by its OWN closing brace, and read its fields."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines)
                  if f'"{capture_id}"' in l and "Object.freeze" in l), None)
    if start is None:
        raise SystemExit(json.dumps({"status": "REFUSED", "reason": f"no pin block for {capture_id}"}))
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\}\)", lines[i])), None)
    if end is None:
        raise SystemExit(json.dumps({"status": "REFUSED", "reason": "pin block is not closed"}))
    fields, scrape_line = {}, None
    for i in range(start + 1, end):
        found = re.match(r'\s*([a-z_]+):\s*(?:"([^"]*)"|(\d+)),\s*$', lines[i])
        if not found:
            continue
        fields[found.group(1)] = found.group(2) if found.group(2) is not None else int(found.group(3))
        if found.group(1) == "scrape_id":
            scrape_line = i
    if scrape_line is None:
        raise SystemExit(json.dumps({"status": "REFUSED", "reason": "pin block has no scrape_id"}))
    return scrape_line, fields


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture-id", required=True)
    ap.add_argument("--decision", required=True, type=Path)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    decision_path = args.decision.resolve()
    if decision_path.parent != APPROVALS.resolve():
        return say({"status": "REFUSED", "reason": f"decision must live in {APPROVALS}"})
    decision = json.loads(decision_path.read_text())
    if decision.get("schema") != SCHEMA:
        return say({"status": "REFUSED", "reason": f"decision schema must be {SCHEMA}"})
    missing = [k for k in REQUIRED if not decision.get(k)]
    if missing:
        return say({"status": "REFUSED", "reason": "decision incomplete", "missing": missing})
    if decision["capture_id"] != args.capture_id:
        return say({"status": "REFUSED", "reason": "decision names a different capture"})
    new_id = str(decision["new_scrape_id"])
    if not UUID_RE.match(new_id):
        return say({"status": "REFUSED", "reason": "new_scrape_id is not a UUIDv7"})

    row, capture = chain_state(args.capture_id)
    if row["scrape_id"] != new_id:
        return say({"status": "REFUSED", "reason": "decision blesses an id the ledger does not carry",
                    "ledger": row["scrape_id"], "decision": new_id})
    started = datetime.fromisoformat(capture["fetch_started_at"].replace("Z", "+00:00"))
    skew = abs((uuid7_time(new_id) - started).total_seconds())
    if skew > MAX_ID_SKEW_S:
        return say({"status": "REFUSED",
                    "reason": "the id's embedded issue time does not match the fetch it claims",
                    "id_time": uuid7_time(new_id).isoformat(), "fetch_started_at": capture["fetch_started_at"],
                    "skew_seconds": round(skew, 1)})

    attest = firecrawl_attests(new_id, row["requested_url"])
    if attest.get("attested") and not (attest.get("attested_content_sha256") and capture.get("content_sha256")):
        attest = {"attested": False,
                  "reason": "no comparable content hash on one side; a rotation is never allowed on an "
                            "UNBOUND body",
                  "attested_content_sha256": attest.get("attested_content_sha256"),
                  "capture_content_sha256": capture.get("content_sha256")}
    elif attest.get("attested") and attest["attested_content_sha256"] != capture["content_sha256"]:
        # A real scrape id for the right url is not enough: the DOCUMENT must be the one this capture
        # claims. Without this, an authentic id could carry a capture whose body was swapped.
        attest = {"attested": False, "reason": "Firecrawl attests a DIFFERENT document than this capture",
                  "attested_content_sha256": attest["attested_content_sha256"],
                  "capture_content_sha256": capture["content_sha256"]}
    if not attest.get("attested"):
        return say({"status": "REFUSED",
                    "reason": "Firecrawl does not attest this scrape id for this url",
                    "attestation": attest})

    plan = []
    for candidate in CANDIDATES:
        target = BUSINESS / candidate / "src/mystery-shop.mjs"
        if not target.exists():
            continue
        text = target.read_text()
        line, fields = pin_block(text, args.capture_id)
        for field in ("requested_url", "resolved_url", "exact_quote_sha256", "status_code"):
            if field in fields and fields[field] != row.get(field):
                return say({"status": "REFUSED", "path": str(target),
                            "reason": f"{field} differs from the live row; that is a MATERIAL change, "
                                      "not an identity rotation", "pin": fields[field], "row": row.get(field)})
        plan.append({"path": str(target), "line": line + 1, "from": fields["scrape_id"], "to": new_id})

    if not args.apply:
        return say({"status": "DRY_RUN", "capture_id": args.capture_id,
                    "id_time_matches_fetch_within_s": round(skew, 1),
                    "firecrawl_attestation": attest, "plan": plan})

    changed = []
    for item in plan:
        target = Path(item["path"])
        before = target.read_text()
        lines = before.splitlines(keepends=True)
        lines[item["line"] - 1] = lines[item["line"] - 1].replace(f'"{item["from"]}"', f'"{item["to"]}"')
        target.write_text("".join(lines))
        after = target.read_text()          # re-read from DISK; the old proof compared memory to memory
        a, b = before.splitlines(), after.splitlines()
        diff = [i for i in range(max(len(a), len(b))) if (a[i:i+1] or [None])[0] != (b[i:i+1] or [None])[0]]
        if diff != [item["line"] - 1] or len(a) != len(b):
            target.write_text(before)
            return say({"status": "REFUSED", "reason": "edit touched more than the scrape_id line; reverted",
                        "lines": diff})
        changed.append({**item, "sha256": hashlib.sha256(after.encode()).hexdigest()})

    receipt = {"schema": "glp-pin-rotation-v1", "capture_id": args.capture_id,
               "decision_receipt": str(decision_path),
               "decision_sha256": hashlib.sha256(decision_path.read_bytes()).hexdigest(),
               "new_scrape_id": new_id, "id_time_skew_seconds": round(skew, 1),
               "firecrawl_attestation": attest,
               "authority": decision["authority"], "changed": changed}
    RECEIPTS.parent.mkdir(parents=True, exist_ok=True)
    with RECEIPTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, sort_keys=True) + "\n")
    return say({"status": "ROTATED", **receipt})


def say(payload: dict) -> int:
    print(json.dumps(payload, indent=1))
    return 0 if payload.get("status") in ("DRY_RUN", "ROTATED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
