#!/usr/bin/env python3
"""Silent watchdog for the OpenAI Codex adaptive request gate.

No stdout on healthy state. Non-empty stdout becomes the Discord alert body when
run through Hermes cron no_agent mode.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/ubuntu/business/ops')
from discord_fmt import (
    title as fmt_title, kv as fmt_kv, kvline as fmt_kvline, section as fmt_section,
    meaning as fmt_meaning, subtext as fmt_subtext, render as fmt_render,
)

HOME = Path.home()
STATE_PATH = HOME / ".hermes" / "codex_gate" / "state.json"
ENV_PATH = HOME / ".hermes" / "codex-throttle.env"
WD_STATE_PATH = HOME / ".hermes" / "codex_gate" / "watchdog_state.json"
WINDOW_SECONDS = 15 * 60
DEDUP_SECONDS = 60 * 60


def iso(ts: float | int | None) -> str:
    if not isinstance(ts, (int, float)):
        return "n/a"
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def parse_env(path: Path) -> dict[str, str]:
    vals: dict[str, str] = {}
    if not path.exists():
        return vals
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(errors="replace"))
    except Exception:
        return {}


def recent_gateway_events() -> list[str]:
    cmd = [
        "journalctl",
        "--user",
        "-u",
        "hermes-gateway.service",
        "-u",
        "hermes-dashboard-remote.service",
        "--since",
        "15 minutes ago",
        "--no-pager",
    ]
    try:
        cp = subprocess.run(cmd, text=True, capture_output=True, timeout=10, check=False)
    except Exception as exc:
        return [f"journalctl unavailable: {exc}"]
    text = (cp.stdout or "") + "\n" + (cp.stderr or "")
    pat = re.compile(r"(HTTP 429|HTTP 503|HTTP 529|RateLimitError|upstream-connect-reset|global cooldown|concurrency permit)", re.I)
    lines = [ln for ln in text.splitlines() if pat.search(ln)]
    return lines[-8:]


def should_emit(alert_key: str, now: float) -> bool:
    prev = load_json(WD_STATE_PATH)
    if prev.get("last_alert_key") == alert_key and now - float(prev.get("last_alert_ts", 0)) < DEDUP_SECONDS:
        return False
    WD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    WD_STATE_PATH.write_text(json.dumps({"last_alert_key": alert_key, "last_alert_ts": now}, indent=2))
    return True


def clear_alert_if_healthy() -> None:
    if WD_STATE_PATH.exists():
        try:
            WD_STATE_PATH.unlink()
        except OSError:
            pass


def main() -> int:
    now = time.time()
    env = parse_env(ENV_PATH)
    state = load_json(STATE_PATH)
    alerts: list[str] = []

    if not ENV_PATH.exists():
        alerts.append(f"missing env file: {ENV_PATH}")
    if not STATE_PATH.exists():
        alerts.append(f"missing state file: {STATE_PATH}")

    try:
        max_c = int(env.get("HERMES_CODEX_MAX_CONCURRENCY", "0"))
        max_d = int(env.get("HERMES_CODEX_MAX_DELEGATES", "0"))
        min_c = int(env.get("HERMES_CODEX_MIN_CONCURRENCY", "1"))
    except ValueError:
        max_c, max_d, min_c = 0, 0, 1
    adaptive = env.get("HERMES_CODEX_ADAPTIVE_CONCURRENCY", "")

    delegate_snapshot = {}
    try:
        sys.path.insert(0, str(HOME / ".hermes" / "hermes-agent"))
        from agent.codex_throttle import codex_delegate_runtime_snapshot

        delegate_snapshot = codex_delegate_runtime_snapshot()
    except Exception as exc:
        alerts.append(f"delegate gate readback unavailable: {type(exc).__name__}")
    delegate_active = delegate_snapshot.get("active_delegates")
    # Reviewed ceiling. 2026-08-22 (bridge): env was raised 7 -> 8 for the Sol-worker
    # era but this pin was not, so the watchdog false-positived every 15m for hours
    # with fail_count 0 (the exact "same alert twice = defect" class). Update this
    # constant WITH any deliberate env change; drift from it still alerts.
    REVIEWED_CEILING = 8
    if max_c != REVIEWED_CEILING or max_d != REVIEWED_CEILING:
        alerts.append(
            f"root ceilings drifted from reviewed value {REVIEWED_CEILING}: "
            f"requests={max_c}, delegates={max_d}")
    if delegate_active is None:
        alerts.append("shared delegate live-count unavailable")
    elif int(delegate_active) > max_d:
        alerts.append(f"shared delegate live-count exceeds ceiling: active={delegate_active}, max={max_d}")

    permit = state.get("permit")
    fail_count = int(state.get("fail_count") or 0)
    cooldown_until = float(state.get("cooldown_until") or 0)
    last_fail_ts = float(state.get("last_fail_ts") or 0)
    last_increase_ts = float(state.get("last_increase_ts") or 0)

    events = recent_gateway_events()
    recent_pushbacks = [e for e in events if re.search(r"HTTP 429|HTTP 503|HTTP 529|RateLimitError|upstream-connect-reset|global cooldown", e, re.I)]

    if max_c <= 1 and adaptive == "1":
        alerts.append(f"gate ceiling is effectively serialized while adaptive is enabled: MAX={max_c}")

    if cooldown_until > now:
        alerts.append(f"active coordinated cooldown until {iso(cooldown_until)}")

    recent_fail = last_fail_ts > now - WINDOW_SECONDS
    if isinstance(permit, int) and max_c > 1 and permit <= min_c and (recent_fail or len(recent_pushbacks) >= 2):
        alerts.append(f"permit at floor under recent pushback: permit={permit}, min={min_c}, max={max_c}")

    if len(recent_pushbacks) >= 3:
        alerts.append(f"{len(recent_pushbacks)} recent Codex pushback events in the last 15m")

    # 2026-08-13 (bridge): a mid-range permit with zero failures and active
    # delegates is HEALTHY adaptive steady-state, not a pathology — alerting
    # on it spammed the operator. Only alert when permit is pinned near the
    # floor for >45m under sustained pushback.
    if (isinstance(permit, int) and max_c > 1 and permit <= min_c + 1
            and last_increase_ts and now - last_increase_ts > 45 * 60
            and len(recent_pushbacks) >= 3):
        alerts.append(f"permit pinned near floor for >45m under sustained pushback: permit={permit}, min={min_c}, max={max_c}, last_increase={iso(last_increase_ts)}")

    if not alerts:
        clear_alert_if_healthy()
        return 0

    alert_key = "|".join(sorted(set(alerts)))
    if not should_emit(alert_key, now):
        return 0

    extra = []
    if events:
        extra.append(f'recent pushback hits: **{len(events[-5:])}** (journal tail kept local)')
    print(fmt_render(
        fmt_title('warn', 'Codex Gate', f'{len(alerts)} gate condition(s) need attention'),
        *extra,
        fmt_kvline(('permit', permit), ('request min', min_c), ('request max', max_c), ('delegates active', delegate_active), ('delegates max', max_d), ('adaptive', adaptive), ('fail count', fail_count)),
        fmt_meaning('nothing unless alerts repeat — then lower START/BACKOFF/MAX or inspect the local gate state'),
        fmt_subtext(f'state: {STATE_PATH} · env: {ENV_PATH}'),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
