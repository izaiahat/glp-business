#!/usr/bin/env python3
"""Read-only view of the onboarding machine's authoring-dispatch ledger.

Nothing here dispatches, writes, or calls an engine. It exists so the state that
decides whether a provider gets another 45-minute authoring cycle is INSPECTABLE
from the bridge and from a card, rather than only from a JSON file somebody has
to remember to open. A budget you cannot see is a budget you will overspend.

Exit 0 always when the ledger is readable; the JSON says what is true.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import glp_onboarding_authoring_dispatch as ad  # noqa: E402


def summarize(provider_filter: str = "") -> dict:
    state = ad.load_state()
    wanted = {p.strip() for p in provider_filter.split(",") if p.strip()}
    providers = []
    for name, record in sorted((state.get("providers") or {}).items()):
        if wanted and name not in wanted:
            continue
        cycles = record.get("cycles") or []
        today = ad.cycles_today(state, name)
        last = cycles[-1] if cycles else {}
        providers.append({
            "provider": name,
            "cycles_today": len(today),
            "cycles_remaining_today": max(0, ad.MAX_CYCLES_PER_DAY - len(today)),
            "cycles_all_time": len(cycles),
            "last_status": last.get("status") or "",
            "last_job_id": last.get("job_id") or "",
            "last_dispatched_utc": last.get("dispatched_utc") or "",
            "last_deadline_utc": last.get("deadline_utc") or "",
            "last_receipt": last.get("authoring_receipt") or "",
            # A receipt path is only evidence if the file is there. The ledger
            # cited three that nobody had written (defect 2026-08-20 #2), and a
            # status view that repeats the path without checking is how that
            # went unnoticed for four hours.
            "last_receipt_on_disk": bool(last.get("authoring_receipt")
                                         and Path(last["authoring_receipt"]).is_file()),
            "last_blocked_steps": last.get("blocked_steps") or [],
            "last_engine": "%s/%s" % (last.get("engine") or "",
                                      last.get("model") or "") if last else "",
            "last_engine_escalated": bool(last.get("engine_escalated")),
            # WHICH POLICY chose that engine, and for which authoring class.
            # The model is always Sol now (operator routing 2026-08-20), so the
            # policy is what tells a reader which TURN BUDGET the cycle ran
            # under — a failure on the heavy research budget and one on the
            # standard budget mean different things. The status view must not
            # make a reader guess. (`claude_p/opus` can still appear in OLD
            # cycle records; that is history, not the live policy.)
            "last_engine_policy": last.get("engine_policy") or "",
            "last_dispatch_class": last.get("dispatch_class") or "",
            "last_max_turns": last.get("max_turns"),
            "last_failure_class": last.get("failure_class") or "",
            # The next engine is only knowable together with what is blocked —
            # the same provider routes to opus for a research packet and to Sol
            # for a taxonomy row. Asked with the last cycle's blocks, which is
            # the best evidence available before the next manifest run.
            "next_engine": ad.engine_for(name, state,
                                         blocked_steps=last.get("blocked_steps") or []),
            "agent_verdict": last.get("agent_verdict") or "",
            "fit_verdict": last.get("agent_fit_verdict") or "",
        })
    stops = [{"provider": p, "recorded_utc": s.get("recorded_utc"),
              "evidence": (s.get("evidence") or [])[:3],
              "authoring_receipt": s.get("authoring_receipt") or ""}
             for p, s in sorted((state.get("not_fit") or {}).items())
             if not wanted or p in wanted]
    in_flight = [{"provider": c.get("provider"), "cycle": c.get("cycle"),
                  "job_id": c.get("job_id"), "deadline_utc": c.get("deadline_utc")}
                 for c in ad.in_flight_cycles(state)]
    return {
        "status": "PASS",
        "schema": "glp.onboarding_authoring_status.v1",
        "machine": ad.MACHINE,
        "state_path": str(ad.STATE_PATH),
        "enabled": ad.ENABLED,
        "budget_minutes": ad.BUDGET_S // 60,
        "max_cycles_per_provider_per_day": ad.MAX_CYCLES_PER_DAY,
        "max_in_flight": ad.MAX_IN_FLIGHT,
        # ENGINE POLICY, stated rather than implied. Sol is the only authoring
        # engine (operator routing order 2026-08-20: opus only reviews); the
        # classes differ by TURN BUDGET, not by model.
        "engine_policy": {
            "research_packet_class": {
                "name": ad.POLICY_RESEARCH_PACKET,
                "steps": sorted(ad.RESEARCH_PACKET_CLASS),
                "primary": "%s/%s (%s)" % (ad.PRIMARY_ENGINE, ad.PRIMARY_MODEL,
                                           ad.PRIMARY_REASONING),
                "max_turns": max(1, min(ad.BRIDGE_MAX_TURNS,
                                        ad.RESEARCH_MAX_TURNS)),
                "why": ("operator routing 2026-08-20: recurring research runs "
                        "on Sol with the heavy turn budget; the 3/3 nu-image "
                        "Sol failures were 12-turn ceilings while opus ran "
                        "uncapped — a budget asymmetry, not a model verdict"),
            },
            "light_class": {
                "name": ad.POLICY_SOL_PRIMARY,
                "primary": "%s/%s (%s)" % (ad.PRIMARY_ENGINE, ad.PRIMARY_MODEL,
                                           ad.PRIMARY_REASONING),
                "max_turns": ad.MAX_TURNS,
                "bridge_max_turns": ad.BRIDGE_MAX_TURNS,
            },
            "model_escalation": "none — capability failures are recorded and "
                                "owned by the daily cap and the error ladder; "
                                "opus is the independent reviewer, never the "
                                "authoring engine",
        },
        "primary_engine": "%s/%s (%s)" % (ad.PRIMARY_ENGINE, ad.PRIMARY_MODEL,
                                          ad.PRIMARY_REASONING),
        "model": ad.MODEL,
        "max_turns": ad.MAX_TURNS,
        # Every bridge call is bounded three ways (defect 2026-08-20 #5).
        "http_bounds": {
            "connect_timeout_s": ad.HTTP_CONNECT_TIMEOUT_S,
            "read_timeout_s": ad.HTTP_READ_TIMEOUT_S,
            "wall_clock_deadline_s": ad.HTTP_DEADLINE_S,
            "max_attempts_read": ad.HTTP_MAX_ATTEMPTS,
            "max_attempts_dispatch": 1,
        },
        "in_flight": in_flight,
        "stopped_not_fit": stops,
        "providers": providers,
        "output_count": len(providers),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="",
                        help="comma-separated program_id(s) to narrow the view")
    args = parser.parse_args(argv)
    print(json.dumps(summarize(args.provider), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
