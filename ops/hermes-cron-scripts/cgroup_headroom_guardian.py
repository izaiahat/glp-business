#!/usr/bin/env python3
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Lane Memory Headroom","output_class":"alert","composer_type":"deterministic_script","privacy_level":"internal","fixture":"🔴 **Lane Memory Headroom** — 2 lane conditions\n\nglp-scout: real memory **11.2G** is **86%** of its **13G** throttle point\nkael-sol: ceiling re-applied max **12G**, throttle **10G**\n→ A throttled lane stalls silently and keeps reporting progress while producing nothing — shed load or raise the policy for that lane.\n-# state: /home/ubuntu/business/reports/ops/cgroup-headroom-state.json","fixture_output_class":"alert","fixture_test":"/home/ubuntu/business/tests/test_cgroup_headroom_guardian.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
"""Keep every TUI lane's memory ceiling generous, bounded, and SAFE.

Written 2026-08-25 after the glp-scout lane was OOM-killed.

WHAT HAPPENED
-------------
The glp-scout tmux scope carried ``memory.max=8G`` / ``memory.high=7G`` while the
host had 30 GiB of RAM and every parent slice was ``max``. The lane's TUI had
grown to 4.8 GB over 14 days of uptime; with a codex worker beside it the scope
sat at 7.34 GB, permanently over ``high``, so the kernel held its worker in
uninterruptible sleep doing reclaim instead of work. Ninety minutes produced
zero output while the pane cheerfully displayed "processing".

The bridge then "fixed" it by raising ``memory.high`` to 11 GiB -- ABOVE the
8 GiB ``memory.max``. That does not lift the ceiling; it removes the brake. A
soft limit above the hard limit can never trigger, so the scope grew unthrottled
straight into ``memory.max`` and the cgroup OOM killer took the largest process
in it: the lane's own TUI. The throttling had been protective, and disabling it
destroyed the thing it was protecting.

Two rules come out of that, and this script exists to enforce them forever:

  1. ``memory.high`` MUST stay below ``memory.max``. Always. No exceptions.
  2. Never set ``memory.max`` below what the scope is really using, or the write
     itself becomes the kill.

WHY A GUARDIAN RATHER THAN A CONFIG FILE
----------------------------------------
These are transient systemd scopes, recreated on every pane respawn. Whatever
limit the spawn path applies wins at creation time -- which is how an 8 GiB
fence reappeared on a 30 GiB box. A periodic enforcer survives respawns without
having to find and patch every spawn path.

It is also the pressure SENSOR. Before this, the first sign a lane was dying was
the lane being dead.

ANON vs CACHE
-------------
``memory.current`` includes reclaimable page cache and routinely reads 5x the
real number -- the bridge scope showed 15.72 GB of which only 2.97 GB was
anonymous. Cache evicts harmlessly under pressure. Every decision here uses
``anon`` from ``memory.stat``. Judging pressure by ``memory.current`` produces
false alarms and, worse, false confidence.

Silent when healthy. Cron delivers stdout, so printing IS the alert.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CGROOT = Path("/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service")
GIB = 1024 ** 3
STATE = Path("/home/ubuntu/business/reports/ops/cgroup-headroom-state.json")

# Ceilings are not reservations: the sum may exceed RAM, because `high`
# throttles long before anything competes for the last byte.
POLICY = {
    # marker found in the scope's processes -> (max_gib, high_gib)
    "--source glp-scout": (16, 13),
    "--source kael-sol": (12, 10),
    "claude --resume": (20, 17),
}
DEFAULT = (12, 10)

# Warn when real anonymous memory reaches this fraction of `high`.
WARN_FRACTION = 0.85


def read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    return None if raw == "max" else int(raw)


def anon_bytes(scope: Path) -> int:
    try:
        for line in (scope / "memory.stat").read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "anon":
                return int(value)
    except OSError:
        pass
    return 0


def scope_owner(scope: Path) -> tuple[str, str]:
    """(policy_marker, human label) for a scope, from the processes inside it."""
    try:
        pids = (scope / "cgroup.procs").read_text().split()
    except OSError:
        return "", "unreadable"
    for pid in pids[:40]:
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace")
        except OSError:
            continue
        for marker in POLICY:
            if marker in cmd:
                return marker, marker.replace("--source ", "").replace(" --resume", "")
    return "", "unclassified"


def write_limit(path: Path, value: int) -> bool:
    """cgroup files need root; the cron user holds passwordless sudo."""
    try:
        subprocess.run(["sudo", "-n", "tee", str(path)], input=f"{value}\n".encode(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def main() -> int:
    if not CGROOT.is_dir():
        print("cgroup root missing — not a cgroup v2 user slice")
        return 0

    messages: list[str] = []
    report: list[dict] = []

    for scope in sorted(CGROOT.glob("tmux-spawn-*.scope")):
        current = read_int(scope / "memory.current") or 0
        if current < 50 * 1024 * 1024:
            continue  # empty/dying scope, nothing to protect

        marker, label = scope_owner(scope)
        want_max_g, want_high_g = POLICY.get(marker, DEFAULT)
        want_max, want_high = want_max_g * GIB, want_high_g * GIB

        have_max = read_int(scope / "memory.max")
        have_high = read_int(scope / "memory.high")
        anon = anon_bytes(scope)

        # RULE 2 — never write a ceiling under real usage; that write is a kill.
        if want_max <= anon:
            messages.append(
                f"⚠️ {label}: policy max {want_max_g}G is at/below real usage "
                f"{anon/GIB:.1f}G — REFUSING to set it (that write would OOM the lane). "
                f"Raise the policy.")
            want_max = None

        acted = []
        # Order matters: raise the ceiling before the brake, lower the brake
        # before the ceiling. The invariant must hold at every instant, not
        # just at the end.
        if want_max is not None and have_max != want_max:
            if (have_max is None or want_max > have_max) and write_limit(
                    scope / "memory.max", want_max):
                acted.append(f"max→{want_max_g}G")
                have_max = want_max

        if have_high != want_high:
            ceiling = have_max if have_max is not None else want_max
            # RULE 1 — high must stay strictly below max. This is the exact
            # inversion that killed the lane on 2026-08-25.
            if ceiling is not None and want_high >= ceiling:
                messages.append(
                    f"🛑 {label}: refusing high={want_high_g}G — it is not below "
                    f"max={ceiling/GIB:.0f}G. A soft limit above the hard limit "
                    f"disables throttling entirely and invites the OOM killer.")
            elif write_limit(scope / "memory.high", want_high):
                acted.append(f"high→{want_high_g}G")
                have_high = want_high

        if want_max is not None and have_max is not None and have_max != want_max and \
                want_max < have_max and want_max > anon:
            if write_limit(scope / "memory.max", want_max):
                acted.append(f"max↓{want_max_g}G")
                have_max = want_max

        if acted:
            messages.append(f"✅ {label}: applied {', '.join(acted)} "
                            f"(real anon {anon/GIB:.1f}G)")

        # SENSOR — the thing that did not exist on 2026-08-25.
        if have_high and anon >= have_high * WARN_FRACTION:
            messages.append(
                f"🔴 {label}: real memory {anon/GIB:.1f}G is "
                f"{anon/have_high*100:.0f}% of its {have_high/GIB:.0f}G throttle "
                f"point. Throttling stalls the lane silently — it keeps printing "
                f"'processing' while doing nothing. Shed load or raise the policy.")

        report.append({
            "scope": scope.name, "label": label,
            "anon_gib": round(anon / GIB, 2),
            "current_gib": round(current / GIB, 2),
            "max_gib": None if have_max is None else round(have_max / GIB, 1),
            "high_gib": None if have_high is None else round(have_high / GIB, 1),
        })

    # Final invariant sweep — catches limits set by anything other than us.
    for row in report:
        if row["max_gib"] and row["high_gib"] and row["high_gib"] >= row["max_gib"]:
            messages.append(
                f"🛑 INVARIANT VIOLATED on {row['label']}: high {row['high_gib']}G >= "
                f"max {row['max_gib']}G. Throttling is disabled; this scope will OOM "
                f"rather than throttle.")

    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"scopes": report}, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass

    if not messages:
        return 0  # healthy is silence

    # House style CRON_DISCORD_HOUSE_STYLE_V1: title, blank line, <=3 body
    # lines, exactly one meaning line, one subtext line. Alert budget is 6
    # nonblank lines total.
    critical = any(m.startswith(("🔴", "🛑")) for m in messages)
    emoji = "🔴" if critical else "⚠️"
    body = [m.split(" ", 1)[1] if m[0] in "✅⚠️🔴🛑" else m for m in messages]
    shown, extra = body[:3], max(0, len(body) - 3)
    headline = (f"{len(body)} lane condition{'s' if len(body) != 1 else ''}"
                if critical else f"{len(body)} lane ceiling update"
                f"{'s' if len(body) != 1 else ''}")

    print(f"{emoji} **Lane Memory Headroom** — {headline}")
    print()
    for line in shown:
        print(line if not extra or line is not shown[-1] else f"{line} (+{extra} more)")
    if critical:
        print("→ A throttled lane stalls silently and keeps reporting progress while "
              "producing nothing — shed load or raise the policy for that lane.")
    else:
        print("→ Ceilings re-applied after a scope respawn; no action needed unless "
              "this repeats every run.")
    print(f"-# state: {STATE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
