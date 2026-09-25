#!/usr/bin/env python3
"""Operator alert when a Codex (OpenAI) account nears depletion (operator 2026-09-10: 'alert me at ~5% and I'll redeem a banked reset').
Reads ops/subscription-capacity/latest.json (collector every 30 min); once per reset cycle per account, when weekly_remaining_pct <= THRESHOLD,
raises an operator-urgent Pushover alert through ops/operator-console/red_alert_hook (live) and records the state so it never repeats
until the account's remaining climbs back above 20 (a redemption). Cron: every 10 minutes."""
import json, sys, datetime
from pathlib import Path
sys.path.insert(0, '/home/ubuntu/business/ops/operator-console'); sys.path.insert(0, '/home/ubuntu/business/ops')
THRESHOLD = 5.0
LATEST = Path('/home/ubuntu/business/ops/subscription-capacity/latest.json')
STATE = Path('/home/ubuntu/business/ops/subscription-capacity/depletion-alert-state.json')
def main():
    d = json.loads(LATEST.read_text()); st = json.loads(STATE.read_text()) if STATE.exists() else {}
    fired = []
    codex = [r for r in d.get('records', []) if 'codex' in str(r.get('label', '')) and r.get('weekly_remaining_pct') is not None]
    # The pool is the ACTIVE account: alert when the best remaining Codex account is at or below the threshold
    # (a depleted sibling with banked resets is the one the operator redeems, not the trigger).
    best = max(codex, key=lambda r: r['weekly_remaining_pct']) if codex else None
    for r in ([best] if best else []):
        label = 'codex-pool(' + str(r.get('label')) + ')'; pct = r['weekly_remaining_pct']
        if pct > 20: st[label] = {'alerted': False}; continue
        if pct <= THRESHOLD and not st.get(label, {}).get('alerted'):
            import red_alert_hook
            spec = {'severity': 'RED', 'source': 'Fable', 'impact': 'Codex pool nearly empty',
                    'what': f'The active Codex account is at {pct:.0f} percent, at or below the {THRESHOLD:.0f} percent line. Redeem a banked reset now or every GLP lane stalls at zero.',
                    'action': 'Policy (b), GLP-71: redeem the banked Codex reset NOW; if none remain, this is a hard stop until the weekly reset. Then tell the Fable TUI so the ledger updates.',
                    'where': f'hermes-01 Codex pool {label}', 'evidence_codes': ['codex_capacity_le_5pct'],
                    'emergency': True, 'operator_urgent': True, 'recovery': False, 'url': None, 'url_title': None, 'dedup_key': f'codex-depletion-{label}'}
            res = red_alert_hook.raise_alert(spec, live=True)
            st[label] = {'alerted': True, 'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'pct': pct, 'delivery': res.get('delivery', res)}
            fired.append((label, pct, res.get('status')))
    STATE.write_text(json.dumps(st, indent=1))
    print('fired:', fired if fired else 'none', '| records:', [(r.get('label'), r.get('weekly_remaining_pct')) for r in d.get('records', [])])
if __name__ == '__main__': main()
