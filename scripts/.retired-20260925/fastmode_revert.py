#!/usr/bin/env python3
"""fastmode_revert.py — request /fast off from explicit recipients at reset or deadline.

Operator 2026-09-12: "i turned on fast mode on all tuis just turn it off after the reset hits or midnight pst
whichever comes first." /fast on an OpenAI model means Priority Processing (service_tier=priority), so it must not
outlive the window it was turned on for.

Reset signal: any codex pool's weekly-remaining jumps up versus the sample before it (a reset refills the pool);
codex-account-1 sitting at 0% rising at all is the clearest tell. Deadline: --deadline ISO-8601 (default 07:00Z,
which is midnight PDT). Sends only a pointer to an existing instruction file, once.
The recipient applies /fast off; transport never claims the setting changed.

Usage: scripts/fastmode_revert.py [--deadline ISO] [--check] [--now]
       --target %pane '$tmux-session' actual-hermes-session /absolute/instructions GLP-n
Repeat --target for explicitly authorized recipients. No default fleet broadcast.
"""
import argparse, json, os, re, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

B = Path(__file__).resolve().parents[1]
PACING = B / 'ops/subscription-capacity/pacing.log'
RECEIPTS = B / 'reports/ops/fastmode-20260912'


def pool_samples(tail=6):
    """[{account: percent_left}] oldest→newest, parsed from the pacing ledger."""
    if not PACING.exists(): return []
    lines = [l for l in PACING.read_text(errors='ignore').splitlines() if 'codex-account' in l][-tail:]
    out = []
    for l in lines:
        out.append({m.group(1): float(m.group(2))
                    for m in re.finditer(r'(codex-account-\d|claude-code-live):\s*([\d.]+)% left', l)})
    return out

def reset_detected(samples, jump=8.0):
    """True when a pool refilled: newest sample is more than `jump` points above the previous one."""
    if len(samples) < 2: return False, None
    prev, cur = samples[-2], samples[-1]
    for acct, now_left in cur.items():
        if acct.startswith('codex-account') and acct in prev and now_left - prev[acct] >= jump:
            return True, f'{acct} {prev[acct]}% -> {now_left}% left'
    return False, None

def ready(target):
    r = subprocess.run([str(B / 'scripts/tui_send.sh'), '--check', *target[:3]],
                       capture_output=True, text=True)
    return r.returncode == 0

def send(target):
    r = subprocess.run([str(B / 'scripts/tui_send.sh'), *target], capture_output=True, text=True)
    return r.returncode, (r.stdout or r.stderr).strip()

CONFIG = Path.home() / '.hermes/config.yaml'

def revert_config_tier():
    """agent.service_tier drives fast mode for every NEW hermes process (cli.py:5550), including background
    `hermes chat --oneshot` jobs. It persists, so it reverts on the same trigger as the TUI panes."""
    if not CONFIG.exists(): return 'config missing'
    lines = CONFIG.read_text().split('\n')
    changed = 0
    inside_agent = False
    for i, l in enumerate(lines):
        if l.startswith('agent:'): inside_agent = True; continue
        if inside_agent and l and not l.startswith((' ', '\t')): break
        if inside_agent and l.strip() == 'service_tier: fast':
            lines[i] = l.replace('service_tier: fast', 'service_tier: standard'); changed += 1
    if changed:
        CONFIG.write_text('\n'.join(lines))
    return f'agent.service_tier reverted ({changed} line)' if changed else 'agent.service_tier already standard'

def revert(panes, why, wait_ready=240):
    """Request /fast off via file pointers; never claim recipient consumption.

    GLP-213: wait_ready is a TOTAL budget across all panes, not per pane. It used to be
    per pane, so four busy panes could block for 4*240s and the caller's own timeout
    killed the run mid-loop -- losing both the remaining panes and the receipt.

    A busy pane must not cost the others their revert, so we make one pass over the
    panes that are already ready, then spend whatever budget is left on the stragglers.
    The receipt is written in a finally block so a kill still leaves evidence of what
    was actually done.
    """
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    rows = []
    started = time.time()
    cfg = 'not reached'
    try:
        # Ready panes first: they are free, and doing them up front means a hung pane
        # can never prevent a healthy one from reverting.
        order = sorted(panes, key=lambda p: not ready(p))
        for p in order:
            while not ready(p) and time.time() - started < wait_ready:
                time.sleep(10)
            if not ready(p):
                code, detail = 2, 'REFUSED: target not idle within total wait budget'
            else:
                code, detail = send(p)
            rows.append({'pane': p[0], 'target': p, 'transport_exit': code,
                         'consumption': 'unconfirmed', 'note': detail})
            print(f"FASTMODE-REQUEST {p[0]} transport_exit={code} {detail}", flush=True)
        # The config half is what governs every NEW hermes process, so it must happen
        # even if some pane never came ready.
        cfg = revert_config_tier()
        print('FASTMODE-OFF config: ' + cfg, flush=True)
    finally:
        rec = {'at': datetime.now(timezone.utc).isoformat(), 'why': why, 'panes': rows,
               'config': cfg, 'panes_requested': list(panes), 'panes_done': len(rows),
               'wait_ready_budget_s': wait_ready,
               'elapsed_s': round(time.time() - started, 1)}
        (RECEIPTS / f"revert-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json").write_text(json.dumps(rec, indent=1))
    return rec

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--deadline', default='2026-09-12T07:00:00+00:00')
    ap.add_argument('--target', nargs=5, action='append', default=[],
                    metavar=('PANE', 'TMUX_SESSION', 'HERMES_SESSION', 'ARTIFACT', 'ISSUE'))
    ap.add_argument('--poll', type=int, default=120)
    ap.add_argument('--check', action='store_true', help='report the signal state and exit')
    ap.add_argument('--now', action='store_true', help='revert immediately')
    a = ap.parse_args()
    panes = a.target
    deadline = datetime.fromisoformat(a.deadline)
    if not a.check and not panes:
        ap.error('explicit --target bindings required; no default fleet broadcast')

    if a.now:
        revert(panes, 'manual --now'); return 3
    if a.check:
        s = pool_samples(); hit, why = reset_detected(s)
        print(json.dumps({'samples': s[-2:], 'reset_detected': hit, 'why': why,
                          'deadline': a.deadline, 'seconds_to_deadline': int((deadline - datetime.now(timezone.utc)).total_seconds())}, indent=1))
        return 0

    # GLP-213. The deadline is checked FIRST, before anything that can block or raise.
    # The original order checked reset_detected(pool_samples()) first, so a hung or
    # throwing sample read meant the deadline line was never reached and the watcher
    # sat past its deadline having done nothing -- observed 2026-09-12, --check
    # reported seconds_to_deadline -39 while the service was still active.
    # Every poll now prints its decision, so a silent no-op is visible in the journal.
    while True:
        now = datetime.now(timezone.utc)
        if now >= deadline:
            print('DEADLINE reached ' + a.deadline, flush=True)
            revert(panes, 'deadline ' + a.deadline); return 3

        try:
            hit, why = reset_detected(pool_samples())
        except Exception as error:
            # A sampling failure must never cost us the deadline. Log it and keep the
            # loop alive; the deadline check above still fires on the next pass.
            hit, why = False, None
            print(f"POLL {now.isoformat()} sample_error={type(error).__name__}: {error} "
                  f"seconds_to_deadline={int((deadline - now).total_seconds())}", flush=True)
        else:
            print(f"POLL {now.isoformat()} reset_detected={hit} why={why or '-'} "
                  f"seconds_to_deadline={int((deadline - now).total_seconds())}", flush=True)

        if hit:
            print('RESET-DETECTED ' + why, flush=True)
            revert(panes, 'openai reset: ' + why); return 3

        # Never sleep past the deadline: a 120s poll must not overshoot a deadline 5s away.
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        time.sleep(max(0, min(a.poll, remaining)))

if __name__ == '__main__':
    raise SystemExit(main())
