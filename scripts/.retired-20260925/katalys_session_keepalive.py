#!/usr/bin/env python3
"""Keep the Katalys portal session alive, and say so loudly when it isn't.

The interval is **measured, not guessed**. The session captured 2026-08-19 carries
`user_token` / `user_refresh_token` / `user_id_token` with **167.9h** of life
across 15 katalys.com cookies, none session-only. So the session lives ~7 days and
this refreshes at ~half that (`DEFAULT_MAX_AGE_HOURS = 84`), leaving a full margin
to notice and fix a failed refresh before anything goes dark.

What it does, in order:

  1. Ask the sentinel (`staleness_verdict`) whether the lane is healthy.
  2. If it is, re-read the marketplace with the stored bearer. That is both the
     freshness proof and the keepalive: a successful authenticated read is what
     "the session still works" MEANS. It rewrites the programme index, so
     `bind --name` never answers from a stale picture.
  3. If the read fails, the session is dead — post the exact recovery command to
     `#decisions` rather than dying quietly.
  4. Heartbeat either way, so the fleet sentinel can see Katalys auth health
     next to every other machine.

A silent keepalive is worse than none: the whole failure mode this guards against
is `bind --name` confidently answering from an index nobody noticed had frozen.

    katalys_session_keepalive.py            # refresh if due, heartbeat, alert on failure
    katalys_session_keepalive.py --force    # refresh regardless of age
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")
sys.path.insert(0, str(BUSINESS / "ops"))

MACHINE = "katalys_portal_session"
HEARTBEATS_TABLE = "9Gevk0AdC7J6UbFn"

RECOVERY = ("xvfb-run -a --server-args='-screen 0 1440x900x24' "
            "/home/ubuntu/.hermes/venvs/glp-awin-runtime-r2/bin/python "
            "/home/ubuntu/business/scripts/katalys_headful_login.py")


def _kps():
    spec = importlib.util.spec_from_file_location(
        "katalys_portal_session", BUSINESS / "scripts" / "katalys_portal_session.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def refresh(kps) -> dict:
    """Re-read the whole marketplace with the stored bearer and rewrite the index."""
    from playwright.async_api import async_playwright

    bearer = kps.BEARER_PATH.read_text().strip()
    rows: list[dict] = []
    total = None
    offset = 0
    async with async_playwright() as pw:
        ctx = await pw.request.new_context()
        try:
            while total is None or offset < total:
                response = await ctx.post(
                    kps.GRAPHQL,
                    headers={"authorization": bearer, "content-type": "application/json"},
                    data={"query": kps.MARKETPLACE_QUERY,
                          "variables": {"orgId": kps.ORG_ID, "limit": kps.PAGE_SIZE,
                                        "offset": offset, "search": "",
                                        "pubId": kps.PUB_ID}},
                    timeout=60000)
                if not response.ok:
                    raise RuntimeError(f"marketplace_http_{response.status}")
                payload = await response.json()
                if payload.get("errors"):
                    raise RuntimeError("marketplace_errors:"
                                       + json.dumps(kps.scrub(payload["errors"]))[:200])
                block = (payload.get("data") or {}).get("offerMarketplace") or {}
                total = block.get("total") if total is None else total
                results = block.get("results") or []
                if not results:
                    break
                for item in results:
                    rows.append({
                        "offer_id": item.get("offerId"),
                        "offer_name": item.get("offerName"),
                        "relationship_statuses": [r.get("status")
                                                  for r in (item.get("relationships") or [])],
                    })
                offset += len(results)
        finally:
            await ctx.dispose()

    if not rows:
        raise RuntimeError("marketplace_returned_no_rows")
    index = {"schema": "glp.katalys_program_index.v1", "captured_at_utc": kps.utc_now(),
             "org_id": kps.ORG_ID, "pub_id": kps.PUB_ID, "program_count": len(rows),
             "approved_count": len(kps.approved_programs(rows)), "programs": rows}
    kps.PROGRAM_CACHE.write_text(json.dumps(index, indent=2) + "\n")
    return index


def heartbeat(note: str, count: int) -> None:
    """Never let a telemetry outage mask the real result."""
    try:
        import n8n_dt

        n8n_dt.insert(HEARTBEATS_TABLE, {
            "machine": MACHINE,
            "last_success_utc": _kps().utc_now(),
            "output_count": count,
            "receipt": str(_kps().PROGRAM_CACHE),
            "note": note[:300],
        })
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"heartbeat_failed": f"{type(exc).__name__}: {str(exc)[:120]}"}))


def alert(reason: str) -> None:
    try:
        sys.path.insert(0, str(BUSINESS / "ops" / "error-fixer"))
        import fixer_runtime

        fixer_runtime.discord_post(
            "\U0001F534 **KATALYS AUTH IS DOWN** — the portal session no longer works "
            f"(`{reason}`). While it is down, Katalys approvals cannot be bound to a "
            "programme id, so they escalate instead of recording, and the Katalys leg "
            "of the liveness audit is dark.\n"
            f"➜ **DO:** re-run the login (no tap needed, the authenticator handles 2SV):\n"
            f"```\n{RECOVERY}\n```", channel="decisions")
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"alert_failed": f"{type(exc).__name__}: {str(exc)[:120]}"}))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="refresh regardless of index age")
    ap.add_argument("--max-age-hours", type=float, default=None)
    args = ap.parse_args(argv)

    kps = _kps()
    max_age = args.max_age_hours if args.max_age_hours is not None else kps.DEFAULT_MAX_AGE_HOURS
    verdict = kps.staleness_verdict(kps.load_program_index(), max_age)

    if verdict["healthy"] and not args.force:
        heartbeat(f"fresh, age {verdict['age_hours']}h of {max_age}h", verdict["program_count"])
        print(json.dumps({"action": "no_refresh_needed", **verdict}))
        return 0

    try:
        index = asyncio.run(refresh(kps))
    except Exception as exc:  # noqa: BLE001 - a dead session must be LOUD, not fatal-silent
        reason = f"{type(exc).__name__}: {str(exc)[:160]}"
        alert(reason)
        heartbeat(f"REFRESH FAILED: {reason}", 0)
        print(json.dumps({"action": "refresh_failed", "reason": reason,
                          "remedy": RECOVERY}))
        return 2

    note = (f"refreshed: {index['program_count']} programmes, "
            f"{index['approved_count']} approved")
    heartbeat(note, index["program_count"])
    print(json.dumps({"action": "refreshed", "program_count": index["program_count"],
                      "approved_count": index["approved_count"],
                      "captured_at_utc": index["captured_at_utc"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
