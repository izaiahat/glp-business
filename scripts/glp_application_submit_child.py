#!/usr/bin/env python3
"""Submit an EXACT, pre-gated set of affiliate applications. Child process only.

Why this exists: `portal_apply`'s own CLI drains every row the ledger has in
`queued_for_portal_execution`, in ledger order.  The applications machine has
already decided which programs today's cap belongs to (fit-gated, deduped
against the Sheet, ranked by whether a real application surface exists), so it
needs the executor to act on *those* rows and nothing else - otherwise a day's
cap gets spent on whatever happened to be queued first months ago.

It also exists because Playwright lives in a different interpreter than the
Google Sheets client, so the submit phase has to be a separate process anyway.

This file adds no policy.  It calls the same `portal_apply.drain()` the pinned
cron calls, with the same gate, the same ledger lock and the same evidence
capture; it only chooses the row set.  Awin and Katalys are refused here as
well as upstream, because Awin execution requires a separately reviewed exact
manifest and Katalys' portal login is broken.

    <runtime-venv>/bin/python -B scripts/glp_application_submit_child.py \
        --rows rows.json --run-id <id> --daily-cap 3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")
for _path in (str(BUSINESS), str(BUSINESS / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from scripts.marketplace_adapters import portal_apply  # noqa: E402

REFUSED_NETWORKS = {"awin", "katalys"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", required=True, help="JSON list of queue rows")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--daily-cap", type=int, default=25)
    args = parser.parse_args(argv)

    rows = json.loads(Path(args.rows).read_text())
    if not isinstance(rows, list):
        raise SystemExit("rows_file_must_contain_a_list")
    refused = [row for row in rows
               if str(row.get("network") or "").lower() in REFUSED_NETWORKS]
    if refused:
        raise SystemExit(
            "refused_networks_present:"
            + ",".join(sorted({str(row.get("network")) for row in refused})))

    receipt = portal_apply.drain(
        rows, run_id=args.run_id, daily_cap=args.daily_cap, execution_mode=True)
    receipt["plan"] = "glp_application_machine"
    portal_apply.write_private(
        Path(receipt["run_dir"]) / "portal-apply-run.json", receipt)
    print(json.dumps({
        "run_id": receipt["run_id"],
        "run_dir": receipt["run_dir"],
        "queued_count": receipt["queued_count"],
        "submitted_verified": receipt["submitted_verified"],
        "submission_unverified": receipt["submission_unverified"],
        "already_relationship": receipt["already_relationship"],
        "blocked": receipt["blocked"],
        "deferred": receipt["deferred"],
        "provider_write_actions": receipt["provider_write_actions"],
        "unblock_cards": receipt["unblock_cards"],
    }, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
