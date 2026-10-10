#!/usr/bin/env python3
"""Capture MaxBounty catalogue identity; never request a tracking URL."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

import glp_affiliate_link_rails as rails
import glp_wire_in_ready_queue as queue

API_ROOT = "https://api.maxbounty.com/affiliates/api"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "reports/glp-clinic-scout/affiliate-machine/maxbounty-identity/identity-captures"


def fetch_campaign(program_id: str) -> dict:
    from affiliate_approval_auto_reconciler import portal_password, portal_username

    if not re.fullmatch(r"[0-9]+", program_id):
        raise RuntimeError("maxbounty_campaign_id_invalid")
    username = portal_username("maxbounty")
    if username.strip().lower() != "ops@glpclinicscout.com":
        raise RuntimeError("maxbounty_account_mismatch")
    with requests.Session() as session:
        response = session.post(
            f"{API_ROOT}/authenticationApp",
            json={"email": username, "password": portal_password("maxbounty"), "ld": 0, "oid": 0},
            timeout=120, allow_redirects=False,
        )
        if response.status_code != 200:
            raise RuntimeError(f"maxbounty_authentication_http:{response.status_code}")
        login = response.json()
        if login.get("success") is not True or not login.get("mb-app-token"):
            raise RuntimeError("maxbounty_authentication_refused")
        session.headers.update({"x-app-token": login["mb-app-token"], "Accept": "application/json"})
        response = session.get(f"{API_ROOT}/campaign/{program_id}", timeout=120, allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f"maxbounty_campaign_http:{response.status_code}")
        record = response.json()
        if not isinstance(record, dict) or record.get("success") is not True:
            raise RuntimeError("maxbounty_campaign_readback_refused")
        return record


def write_capture(record: dict, *, program_id: str, sheet_row: int,
                  program: str, tracking_link: str, output_dir: Path) -> Path:
    identity = record.get("campaign_id")
    if isinstance(identity, bool) or not isinstance(identity, (str, int)) or str(identity) != str(program_id):
        raise RuntimeError("maxbounty_campaign_identity_missing_or_mismatch")
    output_dir = Path(output_dir).resolve()
    source = output_dir / f"_campaign-{program_id}.json"
    destination = output_dir / f"maxbounty-{program_id}.json"
    body = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    quote = '"campaign_id": ' + json.dumps(identity)
    if not queue.quote_binds_capture_bytes(quote, body):
        raise RuntimeError("maxbounty_campaign_quote_not_bound")
    capture = rails.bind_tracking_capture(
        "MaxBounty", str(identity), tracking_link, program=program,
        base={
            "sheet_row": sheet_row,
            "provider_offer_id": str(identity),
            "quote": quote,
            "capture_path": str(source),
            "capture_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "tracking_clicks": 0,
            "why_this_is_independent_of_the_sheet": (
                f"Authenticated GET {API_ROOT}/campaign/{program_id}; immutable_id is the response's "
                "top-level campaign_id, not an id parsed from a tracking link. "
                "The retained Sheet link is used only for string validation and hash indexing."
            ),
        },
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    source.write_text(body, encoding="utf-8")
    destination.write_text(json.dumps(capture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


def main() -> int:
    from glp_maxbounty_lane import read_sheet_snapshot

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program-id", required=True)
    parser.add_argument("--sheet-row", required=True, type=int)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    snapshot = read_sheet_snapshot()
    rows = [r for r in snapshot["rows"] if r["sheet_row"] == args.sheet_row]
    if len(rows) != 1:
        raise RuntimeError("maxbounty_sheet_row_missing_or_ambiguous")
    cells = rows[0]["cells"]
    if str(cells[4]).strip().casefold() != "maxbounty":
        raise RuntimeError("maxbounty_sheet_network_mismatch")
    record = fetch_campaign(args.program_id)
    path = write_capture(record, program_id=args.program_id, sheet_row=args.sheet_row,
                         program=str(cells[2]), tracking_link=str(cells[14]).strip(), output_dir=args.output_dir)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
