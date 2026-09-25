#!/usr/bin/env python3
"""Burn-down pacing for subscription capacity (read-only; no account/auth/reset writes).

Reads ops/subscription-capacity/latest.json (+ history.jsonl for burn rate) and the
Claude status-line usage file, and writes ops/subscription-capacity/PACING.json:
for every pool -> hours to reset, % left, required burn rate (pp/h) to land at
TARGET_LEFT_PCT at reset, observed burn rate over the last window, and a status:
  UNDERUSING  observed burn < 60% of required (capacity will expire unused)
  ON_TRACK    within band
  OVERPACE    observed burn > 140% of required (will hit the cap early)
  EXPIRED_WINDOW / UNKNOWN when data is missing.

Operator rule (2026-09-07): capacity that expires unused is a defect, the same as an
idle machine. Banked resets remain operator-only and are never touched here.
Prints a one-line summary; exit 0 always (alerting is a separate concern).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/ubuntu/business")
LEDGER = ROOT / "ops/subscription-capacity/latest.json"
HISTORY = ROOT / "ops/subscription-capacity/history.jsonl"
CLAUDE_USAGE = Path(os.path.expanduser("~/.claude/usage-latest.json"))
OUT = ROOT / "ops/subscription-capacity/PACING.json"
TARGET_LEFT_PCT = 0.0
WINDOW_HOURS = 3.0
# Two OpenAI pools share a RESET WINDOW when their weekly resets fall this close together.
# Accounts bought at different times do not: observed 2026-09-16, account-1/-2-real reset
# ~80 h out and account-3 ~168 h out. Credit in one window cannot be spent against the
# other window's deadline, so pacing must never pool balances across windows.
WINDOW_TOLERANCE_HOURS = 6.0


def _iso(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _status(required: float | None, observed: float | None) -> str:
    if required is None:
        return "UNKNOWN"
    if required <= 0:
        return "ON_TRACK"
    if observed is None:
        return "UNKNOWN"
    ratio = observed / required
    if ratio < 0.6:
        return "UNDERUSING"
    if ratio > 1.4:
        return "OVERPACE"
    return "ON_TRACK"


LEDGER_MAX_AGE_MIN = 75.0  # > 30m collector cadence plus dispatch jitter
CLAUDE_TARGET_PCT = 15.0  # preserve orchestration continuity


def ledger_age_minutes(doc: dict, now: float) -> float | None:
    """How old the collector sample is. A dead collector must not read as live capacity."""
    collected = doc.get("collected_at_utc")
    if not collected:
        return None
    try:
        return max(0.0, (now - _iso(collected)) / 60.0)
    except Exception:
        return None


def pools_from_ledger(now: float | None = None) -> list[dict]:
    d = json.load(open(LEDGER))
    age = ledger_age_minutes(d, now if now is not None else time.time())
    rows = []
    for r in d.get("records", []):
        # An ALIAS row is one upstream account's second credential label (the monitor tags
        # it with duplicate_of). Pacing it as a pool double-counted that account's balance
        # in every window total and let the banked-reset plan bind to the alias.
        if r.get("duplicate_of"):
            continue
        rows.append({
            "label": r.get("label"),
            "provider": r.get("provider"),
            "weekly_left_pct": r.get("weekly_remaining_pct"),
            "weekly_reset_at": r.get("weekly_reset_at_utc"),
            "session_left_pct": r.get("session_remaining_pct"),
            "session_reset_at": r.get("session_reset_at_utc"),
            # The collector's own freshness word describes ITS read, not the age of the file
            # we are reading now. With no ledger-age rule a dead collector kept publishing
            # 'live' pools with hours_to_weekly_reset recomputed against a fresh clock.
            "freshness": (r.get("freshness") if age is not None and age <= LEDGER_MAX_AGE_MIN
                          else f"stale (ledger sample {age:.0f} min old)" if age is not None
                          else 'unknown (ledger timestamp missing)'),
            "fetched_at": r.get("fetched_at_utc"),
            # Live usage-API count of unredeemed banked resets; reconciled against the
            # hand-maintained OPERATOR-READINGS.json below (API truth > report).
            "banked_reset_count_api": (r.get("operator_only") or {}).get("banked_reset_count"),
            "windows": r.get("windows") or {},
            "metric": "provider_quota",
        })
    return rows


def group_reset_windows(pools: list[dict], tolerance: float = WINDOW_TOLERANCE_HOURS) -> list[list[dict]]:
    """Group pools into reset windows, soonest window first.

    A pool joins the current window when its reset is within `tolerance` hours of that
    window's earliest member; otherwise it opens a new window. Pure: no I/O.
    """
    groups: list[list[dict]] = []
    for p in sorted(pools, key=lambda x: x["hours_to_weekly_reset"]):
        if groups and p["hours_to_weekly_reset"] - groups[-1][0]["hours_to_weekly_reset"] <= tolerance:
            groups[-1].append(p)
        else:
            groups.append([p])
    return groups


def select_active(pools: list[dict], pref: str | None = None, target: float = TARGET_LEFT_PCT) -> dict:
    """Which OpenAI pool carries load now, and at what rate. Pure — no ledger, no Discord.

    Rules, in order:
      1. A pool at or below the reserve target can NEVER be ACTIVE: it has nothing to give.
         (2026-09-16: soonest-reset-first named codex-account-2-real ACTIVE at 0.0% left.)
      2. Drain the SOONEST-EXPIRING reset window that still holds capacity. Credit in a
         later window keeps its own deadline and waits.
      3. The ACTIVE-POOL.json operator preference decides the order INSIDE the selected
         window only — there the order is a free choice. It never overrides an earlier
         deadline, and a preference naming a drained or later-window pool is recorded as
         not applied rather than silently dropped.
      4. Nothing above target anywhere -> active is None with NO_OPENAI_CAPACITY, so
         downstream consumers read an explicit no-capacity signal, not a dead label.

    The required burn rate covers only the SELECTED WINDOW's remaining credit over that
    window's own hours. Summing across windows produced physically unreachable targets
    that read UNDERUSING forever.
    """
    out = {"active": None, "status": "NO_OPENAI_CAPACITY", "window_labels": [],
           "window_hours": None, "window_left_pp": 0.0, "required_burn_pp_per_hour": None,
           "forced": [], "preference": pref, "pref_applied": False, "pref_note": None}
    sized = [p for p in pools if p.get("hours_to_weekly_reset") is not None]
    if not sized:
        out["status"] = "NO_MEASURED_OPENAI_POOLS"
        out["pref_note"] = "no OpenAI pool carries a measured reset time"
        return out
    groups = group_reset_windows(sized)
    chosen = next((g for g in groups if any((p.get("weekly_left_pct") or 0) > target for p in g)), None)
    if chosen is None:
        out["pref_note"] = f"every measured OpenAI pool is at or below the {target:g}% reserve target"
        return out
    usable = [p for p in chosen if (p.get("weekly_left_pct") or 0) > target]
    window_labels = [p["label"] for p in chosen]
    usable_labels = {p["label"] for p in usable}
    first = usable[0]
    if pref:
        named = next((p for p in sized if p["label"] == pref), None)
        if named is None:
            out["pref_note"] = f"preference '{pref}' matches no measured pool"
        elif pref in usable_labels:
            first = named
            out["pref_applied"] = True
            out["pref_note"] = f"operator preference '{pref}' applied inside the expiring window"
        elif (named.get("weekly_left_pct") or 0) <= target:
            out["pref_note"] = (f"preference '{pref}' is at or below the {target:g}% reserve target "
                                f"({named.get('weekly_left_pct')}% left) and cannot carry load")
        else:
            out["pref_note"] = (f"preference '{pref}' resets in a different window "
                                f"({named['hours_to_weekly_reset']:.0f} h) than the expiring one being drained "
                                f"({first['hours_to_weekly_reset']:.0f} h)")
    hours = first["hours_to_weekly_reset"]
    window_left = max(0.0, (first.get("weekly_left_pct") or 0) - target)
    required = None if not hours else round(window_left / hours, 2)
    # Cross-account percentage rates do not establish comparable work capacity.
    forced = []
    out.update({"active": first["label"], "status": "ACTIVE_SELECTED", "window_labels": window_labels,
                "window_hours": round(hours, 2), "window_left_pp": round(window_left, 1),
                "required_burn_pp_per_hour": required, "forced": forced})
    return out


def observed_burn(label: str, now: float) -> float | None:
    """pp/hour of weekly capacity consumed over the last WINDOW_HOURS, from history."""
    if not HISTORY.exists():
        return None
    pts: list[tuple[float, float, str]] = []
    with open(HISTORY) as f:
        for line in f:
            try:
                snap = json.loads(line)
            except Exception:
                continue
            t = snap.get("collected_at_utc")
            if not t:
                continue
            ts = _iso(t)
            if now - ts > WINDOW_HOURS * 3600:
                continue
            for r in snap.get("records", []):
                if (r.get("label") == label and r.get("weekly_remaining_pct") is not None
                        and not r.get('duplicate_of') and r.get('freshness') == 'live'
                        and r.get('weekly_reset_at_utc')):
                    pts.append((ts, float(r["weekly_remaining_pct"]), r['weekly_reset_at_utc']))
    if len(pts) < 2:
        return None
    pts.sort()
    reset = pts[-1][2]
    same_window = [p for p in pts if p[2] == reset]
    if len(same_window) < 2:
        return None
    (t0, p0, _), (t1, p1, _) = same_window[0], same_window[-1]
    if t1 - t0 < 600:
        return None
    burned = p0 - p1  # remaining drops as we burn; a reset makes this negative
    if burned < 0:
        return None  # a changed allowance within the same reset must not look like zero burn
    return burned / ((t1 - t0) / 3600)


def live_claude(now: float) -> dict | None:
    from account_capacity_collector import claude_statusline_record
    record = claude_statusline_record(now=datetime.fromtimestamp(now, timezone.utc))
    if record is None:
        return None
    return {'label': record.label, 'provider': record.provider,
            'weekly_left_pct': record.weekly_remaining_pct, 'weekly_reset_at': record.weekly_reset_at_utc,
            'session_left_pct': record.session_remaining_pct, 'session_reset_at': record.session_reset_at_utc,
            'freshness': record.freshness, 'fetched_at': record.fetched_at_utc}


def legacy_live_claude(now: float) -> dict | None:
    try:
        d = json.load(open(CLAUDE_USAGE))
    except Exception:
        return None
    rl = d.get("rate_limits") or {}
    def pct(k):
        b = rl.get(k) or {}
        if b.get("used_percentage") is not None:
            return float(b["used_percentage"])
        if b.get("utilization") is not None:
            return float(b["utilization"]) * 100
        return None
    def reset(k):
        r = (rl.get(k) or {}).get("resets_at")
        if not r:
            return None
        t = float(r); t = t / 1000 if t > 1e12 else t
        return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
    w = pct("seven_day"); f = pct("five_hour")
    return {
        "label": "claude-code-live",
        "provider": "anthropic",
        "weekly_left_pct": None if w is None else round(100 - w, 1),
        "weekly_reset_at": reset("seven_day"),
        "session_left_pct": None if f is None else round(100 - f, 1),
        "session_reset_at": reset("five_hour"),
        "freshness": "live" if now - CLAUDE_USAGE.stat().st_mtime < 900 else "stale",
        "fetched_at": datetime.fromtimestamp(CLAUDE_USAGE.stat().st_mtime, tz=timezone.utc).isoformat(),
    }


def main() -> int:
    now = time.time()
    rows = pools_from_ledger(now)
    lc = None if any(r.get("provider") == "anthropic" and r.get("freshness") == "live" for r in rows) else live_claude(now)
    if lc:
        rows = [r for r in rows if r.get("provider") != "anthropic"] + [lc]
    out_rows = []
    for r in rows:
        left = r.get("weekly_left_pct")
        reset_at = r.get("weekly_reset_at")
        hours = None
        if reset_at:
            hours = max(0.0, (_iso(reset_at) - now) / 3600)
        required = None
        target = CLAUDE_TARGET_PCT if r.get('provider') == 'anthropic' else TARGET_LEFT_PCT
        fresh = r.get('freshness') == 'live'
        if left is not None and hours is not None and fresh:
            required = 0.0 if hours <= 0 else max(0.0, (float(left) - target) / hours)
        observed = observed_burn(r.get("label"), now) if fresh else None
        if r.get('provider') == 'anthropic':
            observed = None  # all-models and model-specific history are not interchangeable
        status = "EXPIRED_WINDOW" if hours is not None and hours <= 0 else _status(required, observed)
        scoped = {}
        for scope, window in (r.get('windows') or {}).items():
            remaining = window.get('remaining_pct')
            reset = window.get('reset_at_utc')
            days = max(0.0, (_iso(reset) - now) / 86400) if reset else None
            scoped[scope] = {**window, 'days_to_reset': None if days is None else round(days, 4),
                             'target_pct': target,
                             'required_pp_per_day': (round(max(0.0, remaining - target) / days, 2)
                                                     if fresh and remaining is not None and days and days > 0 else None)}
        out_rows.append({
            **r,
            "window_pacing": scoped,
            "target_pct": target,
            "days_to_reset": None if hours is None else round(hours / 24, 4),
            "required_pp_per_day": None if required is None else round(required * 24, 2),
            "hours_to_weekly_reset": None if hours is None else round(hours, 2),
            "required_burn_pp_per_hour": None if required is None else round(required, 2),
            "observed_burn_pp_per_hour_3h": None if observed is None else round(observed, 2),
            "status": status,
        })
    # Standing order (operator 2026-09-08): one OpenAI account at a time, soonest reset drained
    # first; the other stays idle unless its own deadline can no longer be met by waiting.
    openai = [p for p in out_rows if p.get("provider") == "openai-codex" and p.get("hours_to_weekly_reset") is not None and p.get('freshness') == 'live' and p.get('weekly_left_pct') is not None]
    active = None
    selection = None
    if openai:
        # Operator preference file: ops/subscription-capacity/ACTIVE-POOL.json {"label": ...}.
        pref = None
        try:
            pref = json.load(open(ROOT / "ops/subscription-capacity/ACTIVE-POOL.json")).get("label")
        except Exception:
            pref = None
        selection = select_active(openai, pref)
        active = selection["active"]
        window = set(selection["window_labels"])
        forced = set(selection["forced"])
        for p in out_rows:
            if p.get("provider") != "openai-codex":
                continue
            if p["label"] == active:
                p["role"] = "ACTIVE"
            elif p["label"] in forced:
                p["role"] = "ALSO_ACTIVE_DEADLINE"
            elif (p.get("weekly_left_pct") or 0) <= TARGET_LEFT_PCT:
                p["role"] = "DRAINED_BELOW_TARGET"
            elif p["label"] in window:
                p["role"] = "IDLE_UNTIL_ACTIVE_DEPLETES"
            else:
                p["role"] = "IDLE_LATER_RESET_WINDOW"
            if p["label"] == active and selection["required_burn_pp_per_hour"] is not None:
                # Preserve this account's own rate; plan percentages are not comparable.
                p["status"] = _status(p["required_burn_pp_per_hour"], p.get("observed_burn_pp_per_hour_3h"))
                p["sequential_note"] = (
                    "Per-account pacing only; subscription allowances are not comparable units.")
            p["required_burn_pp_per_day"] = None if p.get("required_burn_pp_per_hour") is None else round(24 * p["required_burn_pp_per_hour"], 1)
    for p in out_rows:
        if p.get("provider") == "anthropic":
            p["role"] = "ANTHROPIC_SUBSCRIPTION"
            p["required_burn_pp_per_day"] = None if p.get("required_burn_pp_per_hour") is None else round(24 * p["required_burn_pp_per_hour"], 1)
    doc = {
        "schema": "capacity-pacing.v2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "target_left_pct_at_reset": TARGET_LEFT_PCT,
        "window_hours_for_observed_burn": WINDOW_HOURS,
        "active_openai_pool": active,
        "openai_selection": selection,
        "policy": "Prioritize useful work before each account's reset, with no quota reserve; percentages are per-account, not comparable quota units. No forced switching or busywork.",
        "pools": out_rows,
        "notes": "Read-only pacing. Banked resets are operator-only. Anthropic observed burn is not computed (statusline file has no history).",
    }
    try:
        local_pro = json.loads(LEDGER.read_text()).get('pro_chat_ledger', [])
        for pro in local_pro:
            doc['pools'].append({'label': pro.get('label'), 'provider': 'pro-chat',
                'metric': 'local_submissions_7d', 'submitted_7d': pro.get('submitted_7d'),
                'local_weekly_guard': pro.get('local_weekly_guard'),
                'local_guard_remaining': pro.get('local_guard_remaining'),
                'provider_remaining_pct': None, 'provider_reset_at_utc': None,
                'fetched_at': pro.get('observed_at'), 'ledger_modified_at': pro.get('ledger_modified_at'),
                'status': 'PACE_UNKNOWN_PROVIDER_UNVERIFIED', 'required_pp_per_day': None})
    except (OSError, ValueError, TypeError):
        pass
    try:
        _age = ledger_age_minutes(json.load(open(LEDGER)), now)
    except Exception:
        _age = None
    doc["ledger_age_minutes"] = None if _age is None else round(_age, 1)
    doc["ledger_stale"] = bool(_age is not None and _age > LEDGER_MAX_AGE_MIN)
    if doc["ledger_stale"]:
        doc["ledger_stale_note"] = (
            f"the collector sample is {_age:.0f} min old (> {LEDGER_MAX_AGE_MIN:g} min): OpenAI balances "
            "below describe that sample, not this moment")
    # Shared mode (operator 2026-09-08): when ACTIVE-POOL.json says mode=shared, both OpenAI pools carry the load
    # until their common reset; roles and status are computed on the combined budget instead of one-at-a-time.
    try:
        ap = json.load(open(ROOT / "ops/subscription-capacity/ACTIVE-POOL.json"))
        if ap.get("mode") == "shared":
            doc["active_openai_pool"] = "shared"
            doc["policy"] = "both OpenAI accounts share the load until their common weekly reset; pace to the combined budget; Fable pool always active; never leave credits at reset; banked resets are operator-only"
            oa = [p for p in doc["pools"] if p.get("provider") == "openai-codex"
                  and p.get("weekly_left_pct") is not None and p.get("hours_to_weekly_reset") is not None]
            # Only pools that actually SHARE a reset window may be combined: "shared mode" was set
            # 2026-09-08 on the premise of a common reset, and a third account on its own window
            # made that premise false. Combine the soonest window that still holds capacity.
            if oa:
                groups = group_reset_windows(oa)
                oa = next((g for g in groups
                           if any((p.get("weekly_left_pct") or 0) > doc.get("target_left_pct_at_reset", 5.0) for p in g)),
                          groups[0])
            if oa:
                hours = max(0.01, min(p.get("hours_to_weekly_reset") or 0.01 for p in oa))
                combined_left = sum(p["weekly_left_pct"] for p in oa)
                target = doc.get("target_left_pct_at_reset", 5.0) * len(oa)
                need = max(0.0, combined_left - target) / hours
                seen = [p.get("observed_burn_pp_per_hour_3h") for p in oa]
                seen_total = sum(x for x in seen if isinstance(x, (int, float))) if any(isinstance(x, (int, float)) for x in seen) else None
                for p in oa:
                    p["role"] = "ACTIVE_SHARED"
                    p["status"] = "SHARED"
                doc["combined_openai"] = {"labels": [p["label"] for p in oa],
                                          "weekly_left_pp": combined_left, "hours_to_common_reset": round(hours, 2),
                                          "required_burn_pp_per_hour": round(need, 2), "required_burn_pp_per_day": round(need * 24, 1),
                                          "observed_burn_pp_per_hour_3h": (round(seen_total, 2) if seen_total is not None else None),
                                          "status": ("UNKNOWN" if seen_total is None else "ON_TRACK" if 0.8 * need <= seen_total <= 1.5 * need else "UNDERUSING" if seen_total < 0.8 * need else "OVERPACE")}
    except Exception as exc:  # never let the mode file break the ledger
        doc["shared_mode_error"] = str(exc)[:120]
    # Banked resets (operator-only redemptions): plan the timing so none expire unused.
    try:
        rd = json.loads((ROOT / "ops/subscription-capacity/OPERATOR-READINGS.json").read_text())
        br = rd.get("banked_resets") or {}
        now_dt = datetime.now(timezone.utc)
        # The readings file is hand-maintained and its account keys drift from the live pool
        # labels (2026-09-16: key 'codex-account-2' vs live label 'codex-account-2-real'), which
        # silently HID a banked reset from the plan, the expiry logic and the Discord line.
        # Resolve keys to live labels, and surface anything that cannot be matched.
        live_labels = [p["label"] for p in doc["pools"] if p.get("provider") == "openai-codex"]
        def _resolve_banked_key(key: str) -> str | None:
            if key in live_labels:
                return key
            cands = [l for l in live_labels if l.startswith(key + "-") or key.startswith(l + "-")]
            return cands[0] if len(cands) == 1 else None
        by_label: dict[str, dict] = {}
        unmatched: list[str] = []
        for key, info in br.items():
            if not isinstance(info, dict) or not (info.get("items") or info.get("expires_utc") or info.get("count")):
                continue  # schema/rule/source/total_remaining scalars, not account entries
            lbl = _resolve_banked_key(key)
            if lbl is None:
                unmatched.append(key)
            else:
                by_label.setdefault(lbl, info)
        plan = []
        for p in doc["pools"]:
            if p.get("provider") != "openai-codex":
                continue
            info = by_label.get(p["label"]) or {}
            items = info.get("items") or ([{"expires_utc": info["expires_utc"]}] * int(info.get("count", 0)) if info.get("expires_utc") else [])
            items = [i for i in items if not i.get("redeemed_at_utc")]
            if not items:
                continue
            exps = sorted(datetime.fromisoformat(i["expires_utc"].replace("Z", "+00:00")) for i in items)
            days_to_first = (exps[0] - now_dt).total_seconds() / 86400.0
            left = p.get("weekly_left_pct")
            hrs = p.get("hours_to_weekly_reset") or 0
            advice = "hold"
            if left is not None and left <= 5.0 and hrs > 24:
                advice = "REDEEM NOW (operator): account near empty with >24 h to its natural reset"
            elif days_to_first <= 2:
                advice = "REDEEM BEFORE EXPIRY (operator): an unredeemed banked reset expires in <2 days"
            # API truth > report (standing rule): the live usage API publishes how many banked
            # resets are actually available. When it disagrees with the hand-maintained readings
            # file, emit the discrepancy instead of issuing REDEEM NOW off the stale side.
            api_count = p.get("banked_reset_count_api")
            entry = {"label": p["label"], "banked": len(items), "expiries_utc": [e.isoformat().replace("+00:00", "Z") for e in exps],
                     "days_to_first_expiry": round(days_to_first, 1), "banked_reset_count_api": api_count}
            if api_count is not None and api_count != len(items):
                entry["api_disagreement"] = (f"usage API reports {api_count} banked reset(s) available; "
                                             f"OPERATOR-READINGS.json records {len(items)} unredeemed")
                advice = ("RECONCILE (operator): the usage API and the readings file disagree on banked resets "
                          "— confirm in the account before redeeming")
            entry["advice"] = advice
            plan.append(entry)
        if unmatched:
            doc["banked_resets_label_unmatched"] = unmatched
        if plan or unmatched:
            # COMPUTED, never a literal. The old note hardcoded "4 redemptions total (2 per
            # account) ... natural weekly resets 09-15, 09-22, 09-29 01:27Z": by 2026-09-16
            # not one of those dates or that time matched any live reset, and the sentence
            # contradicted the banked counts printed directly above it.
            total_banked = sum(a["banked"] for a in plan)
            last_expiry = max((a["expiries_utc"][-1] for a in plan if a.get("expiries_utc")), default=None)
            resets = sorted({p["weekly_reset_at"] for p in doc["pools"]
                             if p.get("provider") == "openai-codex" and p.get("weekly_reset_at")})
            note = (f"{total_banked} unredeemed banked reset(s) across {len(plan)} account(s)"
                    + (f"; last expiry {last_expiry[:10]}" if last_expiry else "")
                    + (f"; next natural weekly resets {', '.join(r.replace('T', ' ')[:16] + 'Z' for r in resets)}"
                       if resets else "")
                    + ". The useful windows are the depletions before each of those; redemption is operator-only.")
            doc["banked_resets_plan"] = {"rule": br.get("rule"), "accounts": plan,
                                         "label_unmatched": unmatched,
                                         "schedule_note": note}
    except Exception as exc:
        doc["banked_resets_error"] = str(exc)[:120]
    # Operator-reported readings (the statusline payload carries only all-models five_hour/seven_day;
    # the Fable-only weekly pool, which binds this seat, is visible only in the Claude app). Side file is
    # written by the orchestrator when the operator reports a reading; never auto-fetched.
    try:
        readings_path = ROOT / "ops/subscription-capacity/OPERATOR-READINGS.json"
        if readings_path.exists():
            readings = json.loads(readings_path.read_text())
            doc["operator_readings"] = readings
            fo = readings.get("claude_fable_only_weekly")
            if fo and fo.get("used_pct") is not None and fo.get("resets_at_utc"):
                left = 100.0 - float(fo["used_pct"])
                reset = datetime.fromisoformat(fo["resets_at_utc"].replace("Z", "+00:00"))
                seconds_left = (reset - datetime.now(timezone.utc)).total_seconds()
                pool = {
                    "label": "claude-fable-only (operator-reported)",
                    "provider": "anthropic",
                    "weekly_left_pct": round(left, 1),
                    "weekly_reset_at": fo["resets_at_utc"],
                    "freshness": "operator-reported " + str(fo.get("reported_at_utc")),
                    "session_left_pct": None,
                    "session_reset_at": None,
                    "fetched_at": fo.get("reported_at_utc"),
                    "observed_burn_pp_per_hour_3h": None,
                    "role": "BINDING_POOL_FOR_FABLE_SEAT",
                    "note": "Not machine-observable; the all-models seven_day figure above under-reads this pool.",
                }
                if seconds_left <= 0:
                    # The reading's own weekly window has closed, so the balance describes a pool
                    # that has since reset. The old code clamped hours to 0.01 and published a
                    # live-looking 31200 pp/day off an 8-day-old screenshot. Unknown is not a
                    # number: suppress the rate and say a fresh reading is needed.
                    pool.update({
                        "hours_to_weekly_reset": None,
                        "required_burn_pp_per_hour": None,
                        "required_burn_pp_per_day": None,
                        "status": "STALE_OPERATOR_READING",
                        "stale_note": "this reading's weekly window has already closed — a fresh Fable-only reading is needed",
                    })
                else:
                    hours = seconds_left / 3600.0
                    need = max(0.0, left - CLAUDE_TARGET_PCT) / hours
                    pool.update({
                        "hours_to_weekly_reset": round(hours, 2),
                        "required_burn_pp_per_hour": round(need, 2),
                        "required_burn_pp_per_day": round(24.0 * need, 1),
                        "status": "OPERATOR_REPORTED",
                    })
                doc["pools"].append(pool)
    except Exception as exc:  # never let a side file break the ledger
        doc["operator_readings_error"] = str(exc)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Status-change line to ops-alerts (WS-U): only when a pool's status or the active pool changes.
    prev = {}
    try:
        prev = json.load(open(OUT))
    except Exception:
        prev = {}
    prev_states = {p.get("label"): p.get("status") for p in prev.get("pools", [])}
    prev_active = prev.get("active_openai_pool")
    # Alert only on meaningful transitions between measured pacing states; UNKNOWN/OPERATOR_REPORTED flips are
    # data-availability noise (observed-burn window empty), not a pacing change (operator 2026-09-08 11:40 PT).
    MEASURED = {"ON_TRACK", "UNDERUSING", "OVERPACE"}
    shared = doc.get("active_openai_pool") == "shared"
    changes = [f"{p['label']}: {prev_states.get(p['label'])} → {p['status']}" for p in out_rows
               if (not shared or p.get("provider") != "openai-codex")
               and prev_states.get(p["label"]) in MEASURED and p["status"] in MEASURED and prev_states.get(p["label"]) != p["status"]]
    if shared:
        cur = (doc.get("combined_openai") or {}).get("status")
        prv = (prev.get("combined_openai") or {}).get("status")
        if prv in MEASURED and cur in MEASURED and prv != cur:
            co = doc["combined_openai"]
            changes.append(f"OpenAI combined: {prv} → {cur} ({co['weekly_left_pp']:.0f} pp left, {co['hours_to_common_reset']:.0f} h to the common reset; need {co['required_burn_pp_per_hour']} pp/h, seen {co['observed_burn_pp_per_hour_3h']})")
        if prev_active and prev_active != "shared":
            changes.append("mode: one-account-at-a-time → SHARED (both accounts carry the load until the common reset; operator 2026-09-08)")
    elif prev_active and active and prev_active != active:
        changes.append(f"active OpenAI pool: {prev_active} → {active}")
    try:
        prev_adv = {a["label"]: a["advice"] for a in (prev.get("banked_resets_plan") or {}).get("accounts", [])}
        for a in (doc.get("banked_resets_plan") or {}).get("accounts", []):
            if prev_adv.get(a["label"]) != a["advice"] and a["advice"] != "hold":
                changes.append(f"banked reset — {a['label']}: {a['advice']} (banked {a['banked']}, first expiry {a['expiries_utc'][0][:10]})")
    except Exception:
        pass
    # The subscription monitor owns all Discord notifications through verified_send();
    # pacing records transitions locally and never creates a second unverified sender.
    if changes:
        doc["pacing_transitions"] = changes
    OUT.write_text(json.dumps(doc, indent=2) + "\n")
    print(" | ".join(
        f"{p['label']}: {p.get('weekly_left_pct')}% left, {p.get('hours_to_weekly_reset')}h, need {p.get('required_burn_pp_per_hour')}pp/h, seen {p.get('observed_burn_pp_per_hour_3h')} -> {p['status']}"
        for p in out_rows if p.get('provider') != 'pro-chat'))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
