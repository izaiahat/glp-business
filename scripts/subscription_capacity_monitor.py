#!/usr/bin/env python3
"""Subscription telemetry, approved included resets and verified Discord alerts.

No LLM calls, OAuth refresh, paid-credit fallback or business work. Automatic
banked redemption is scoped to the operator's Oct 9 account-2-at-zero decision.
The default run notifies on meaningful changes or the daily Pacific digest.
"""
# Direct Discord poster: discord.com/api/v10/channels (channel is verified before POST).
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Subscription capacity","output_class":"report","composer_type":"deterministic","privacy_level":"internal","fixture":"📊 **Subscription capacity** — Usage and reset ledger\n\nOpenAI primary: available; backup: depleted.\nClaude: depleted in CLI snapshot.\n→ continue useful OpenAI work; banked resets remain operator-only\n-# ledger: ops/subscription-capacity/latest.json","fixture_output_class":"report","fixture_test":"tests/test_subscription_capacity_monitor.py::test_report_labels_unverified_claude_and_renders_pacific_reset","route":"discord:1487300132054765629","detail_policy":"complete_summary_in_discord"}
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from zoneinfo import ZoneInfo

from account_capacity_collector import collect_capacity, claude_statusline_record

ROOT = Path('/home/ubuntu/business')
STATE_DIR = ROOT / 'ops/subscription-capacity'
CHANNEL = '1487300132054765629'
TOKEN_PATH = Path('/home/ubuntu/.hermes/secrets/boring-ai/discord_bot_token')
PACIFIC = ZoneInfo('America/Los_Angeles')
sys.path.insert(0, str(ROOT))
from ops import discord_fmt


def now_utc():
    return datetime.now(timezone.utc)


def claude_usage_snapshot():
    """Read the persisted OAuth credential and GET usage without token refresh."""
    from hermes_cli.auth import read_credential_pool
    from agent.anthropic_credentials import read_claude_code_credentials, is_claude_code_token_valid
    from agent.account_usage import _usage_windows, _snapshot
    from account_capacity_collector import _default_usage_getter
    rows = read_credential_pool('anthropic')
    credentials = read_claude_code_credentials()
    tokens = [row.get('access_token') for row in rows]
    if credentials and is_claude_code_token_valid(credentials):
        tokens.append(credentials.get('accessToken'))
    for raw_token in tokens:
        token = str(raw_token or '').strip()
        if not token or not token.startswith('sk-ant-oat'):
            continue
        payload = _default_usage_getter('https://api.anthropic.com/api/oauth/usage', {
            'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
            'Content-Type': 'application/json', 'anthropic-beta': 'oauth-2025-04-20',
            'User-Agent': 'claude-code/2.1.0'})
        windows = _usage_windows(payload, (('five_hour', 'Current session'),
            ('seven_day', 'Current week'), ('seven_day_opus', 'Opus week'),
            ('seven_day_sonnet', 'Sonnet week')), 'utilization', 'resets_at', fraction=True)
        return _snapshot('anthropic', 'oauth_usage_api', windows, [])
    return None


def collect_monitored_capacity(*, now):
    from dataclasses import replace
    report = collect_capacity(now=now, claude_snapshot_getter=claude_usage_snapshot)
    if any(r.provider == 'anthropic' and r.freshness == 'live' for r in report.records):
        return report
    cached = claude_statusline_record(now=now)
    if cached is not None:
        report = replace(report, records=tuple(r for r in report.records if r.provider != 'anthropic') + (cached,))
    return report


def stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.tmp.{os.getpid()}')
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    temporary.chmod(0o600)
    os.replace(temporary, path)


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def threshold_bucket(remaining, thresholds):
    if remaining is None:
        return 'unknown'
    return next((str(n) for n in thresholds if remaining <= n), 'normal')


def alert_state(report, now):
    result = []
    for record in report['records']:
        if record.get('duplicate_of'):
            continue  # retain the alias in latest.json, never alert twice for one account
        provider = record.get('provider')
        weekly_thresholds = (0, 10, 25, 40, 55, 70, 85) if provider == 'anthropic' else (0, 10, 20)
        windows = record.get('windows') or {
            'five_hour': {'remaining_pct': record.get('session_remaining_pct'),
                          'reset_at_utc': record.get('session_reset_at_utc')},
            'seven_day': {'remaining_pct': record.get('weekly_remaining_pct'),
                          'reset_at_utc': record.get('weekly_reset_at_utc')}}
        for scope, window in sorted(windows.items()):
            # Five-hour resets are routine; alert only at >=90% used. Claude's
            # seven-day/model windows retain their 15-point steps.
            thresholds = (0, 10) if scope == 'five_hour' else weekly_thresholds
            result.append({'label': record['label'], 'scope': scope,
                           'bucket': threshold_bucket(window.get('remaining_pct'), thresholds),
                           'reset': window.get('reset_at_utc'), 'freshness': record.get('freshness'),
                           'status': record.get('capacity_status'), 'error': record.get('error_class'),
                           'banked_api': (record.get('operator_only') or {}).get('banked_reset_count')})
    for pro in report.get('pro_chat_ledger', []):
        remaining = pro.get('local_guard_remaining')
        guard = pro.get('local_weekly_guard')
        result.append({'label': pro.get('label'), 'scope': 'local_submissions_7d',
                       'bucket': threshold_bucket(100 * remaining / guard if remaining is not None and guard else None, (0, 10)),
                       'freshness': pro.get('freshness', 'local'), 'provider_verified': False})
    banked = report.get('banked_resets_operator') or {}
    result.append({'label': 'operator-reported-banked-resets', 'scope': 'dated_report',
                   'confirmed': banked.get('count'), 'pending': banked.get('pending_rollout_count'),
                   'reported_on': banked.get('reported_on')})
    result.append({'label': 'collection', 'scope': 'health',
                   'errors': sorted(report.get('collection_errors') or []),
                   'account_count_ok': report.get('account_count_ok')})
    if 'auto_reset' in report:
        result.append({'label': 's7-policy', 'scope': 'reset_api_meter',
                       'reset_status': report['auto_reset']['status'],
                       'api_allowed': report['claude_api'].get('dispatch_allowed'),
                       'meter_status': report['n8n_meter']['status']})
    return sorted(result, key=lambda x: (str(x.get('label')), x['scope']))


RESERVE_TARGET_PCT = 0.0
ACTIVE_POOL_PATH = STATE_DIR / 'ACTIVE-POOL.json'


def openai_roster(path=ACTIVE_POOL_PATH):
    """The operator's own declared OpenAI account roster.

    ops/subscription-capacity/ACTIVE-POOL.json carries `expected_order`: the accounts the
    operator says are pooled, in drain order. It is a DECLARED expectation, so a missing
    account keeps failing account_count_ok instead of quietly becoming the new baseline
    the way `expected = len(labels)` did (that comparison ratified whatever it saw).
    """
    try:
        order = json.loads(Path(path).read_text()).get('expected_order')
    except Exception:
        return []
    return [x for x in order if isinstance(x, str)] if isinstance(order, list) else []


def account_identity(record):
    """Signature proving two credential rows describe the SAME upstream account.

    A weekly window opens when the account's plan does, so its reset instant is unique to
    that account to the second; two rows reporting the same reset instant AND the same
    remaining percentage are one account read twice, not two accounts.
    """
    reset = record.get('weekly_reset_at_utc')
    left = record.get('weekly_remaining_pct')
    return (reset, left) if reset and left is not None else None


def mark_duplicate_labels(records, roster):
    """Tag credential-pool rows that ALIAS an account already counted. Pure over `records`.

    2026-09-16: hermes_cli.auth.read_credential_pool('openai-codex') returns FOUR rows for
    THREE accounts - `codex-account-2` and `codex-account-2-real` hold two credentials for
    one upstream account and report identical weekly_remaining_pct and identical
    weekly_reset_at_utc to the second across every sample. Counting labels therefore
    reported four accounts, summed one account's balance twice into the pool total, and
    let the banked-reset plan bind to whichever label sorted first.

    The alias is TAGGED, never dropped: the row stays in latest.json as evidence, carries
    `duplicate_of`, and every consumer (count, totals, routing, pacing) skips it.

    Only a label OUTSIDE the operator's declared roster may be folded into a roster label:
    the roster is the authority on which labels are accounts, so two declared accounts are
    never collapsed on a signature coincidence - that is returned as a collision for the
    caller to raise. With no roster declared nothing is folded at all; a matching signature
    is reported as a collision so the duplicate is visible without anything being guessed.
    """
    aliases, collisions = {}, []
    openai = [r for r in records if r.get('provider') == 'openai-codex' and not r.get('error_class')]
    order = {label: index for index, label in enumerate(roster)}
    seen = {}
    for record in sorted(openai, key=lambda r: (r['label'] not in order, order.get(r['label'], 0), r['label'])):
        signature = account_identity(record)
        if signature is None:
            continue
        canonical = seen.get(signature)
        if canonical is None:
            seen[signature] = record['label']
            continue
        if record['label'] in order or canonical not in order:
            collisions.append([canonical, record['label']])
            continue
        record['duplicate_of'] = canonical
        record['duplicate_reason'] = (
            f"same weekly reset instant and same weekly remaining as {canonical}, and not in the "
            'declared roster: one upstream account pooled under two credential labels')
        aliases[record['label']] = canonical
    return aliases, collisions


def openai_accounts(records):
    """OpenAI records that stand for a distinct account (aliases excluded)."""
    return [r for r in records if r.get('provider') == 'openai-codex' and not r.get('duplicate_of')]


def routing_advice(report, now, pacing=None):
    """The one line the fleet reads before delegating. ONE selector is authoritative.

    capacity_pacing.py owns ACTIVE-pool selection and writes it to PACING.json. This
    function used to re-derive it from ACTIVE-POOL.json's raw operator label with no
    capacity test at all, so on 2026-09-16 it broadcast "prioritize codex-account-1" at
    1.0% weekly left while the pacing ledger correctly named the 100% pool. When the
    ledger is missing or stale, fall back to the same reserve-target filter the ledger
    applies - never to the raw label.
    """
    live = [r for r in openai_accounts(report['records'])
            if r['freshness'] == 'live' and r['capacity_status'] in ('available', 'limited')
            and not r.get('error_class')]
    live.sort(key=lambda r: r.get('weekly_reset_at_utc') or '9999')
    if not live:
        return 'No measured OpenAI capacity: pause new discretionary delegates; verify telemetry. Never redeem a reset automatically.'
    pacing = _pacing_doc() if pacing is None else pacing
    ledger_active = pacing.get('active_openai_pool')
    selection = pacing.get('openai_selection') or {}
    if ledger_active is None and selection.get('status') == 'NO_OPENAI_CAPACITY':
        return ('No OpenAI pool has measured remaining capacity: pause new discretionary delegates and ask the operator '
                'about a banked reset. Never redeem a reset automatically.')
    if ledger_active == 'shared':
        labels = set((pacing.get('combined_openai') or {}).get('labels') or [r['label'] for r in live])
        window = [r for r in live if r['label'] in labels] or live
        total = sum(r.get('weekly_remaining_pct') or 0 for r in window)
        days = (parse_time(window[0].get('weekly_reset_at_utc')) - now).total_seconds() / 86400 if window[0].get('weekly_reset_at_utc') else None
        pace = f'; about {total / days:.1f} combined percentage points/day to the common reset' if days and days > 0 else ''
        who = ', '.join(r['label'] for r in window)
        return (f"Share the load across {who} until their common reset{pace}. "
                'Redeem banked resets only when the line above says REDEEM NOW.')
    preferred = None
    basis = ''
    # The ledger's own rule is that a pool at or below the reserve can never be ACTIVE.
    # Re-check it here rather than trust the name: a stale PACING.json must not be able to
    # broadcast a drained pool, which is the exact 2026-09-16 defect in its other form.
    match = [r for r in live if r['label'] == ledger_active
             and (r.get('weekly_remaining_pct') or 0) > RESERVE_TARGET_PCT] if ledger_active else []
    if match:
        preferred, basis = match[0], 'active pool in the pacing ledger; the rest idle until it depletes'
    else:
        # Ledger missing, stale or naming a pool this sample cannot see: re-apply its rule here.
        usable = [r for r in live if (r.get('weekly_remaining_pct') or 0) > RESERVE_TARGET_PCT]
        if not usable:
            return ('No OpenAI pool has measured remaining capacity: pause new discretionary delegates and ask the operator '
                    'about a banked reset. Never redeem a reset automatically.')
        preferred = usable[0]
        basis = 'earliest measured reset with remaining capacity (pacing ledger unavailable)'
    reset = parse_time(preferred.get('weekly_reset_at_utc'))
    days = (reset - now).total_seconds() / 86400 if reset else None
    remaining = preferred.get('weekly_remaining_pct')
    pace = f'; about {remaining / days:.1f} percentage points/day to reset' if days and days > 0 and remaining is not None else ''
    return (f"Prioritize useful work on {preferred['label']} ({basis}){pace}. "
            'Native fill_first remains unchanged; no forced account switch. Banked resets are operator-only.')


def reset_text(record):
    reset = parse_time(record.get('weekly_reset_at_utc'))
    return reset.astimezone(PACIFIC).strftime('%a %b %d %I:%M%p %Z') if reset else 'unknown'


def _pacing_doc():
    try:
        return json.load(open(os.path.expanduser('~/business/ops/subscription-capacity/PACING.json')))
    except Exception:
        return {}


def _redeem_line(pacing, records):
    """One explicit operator-facing line: redeem a banked reset or hold, and why."""
    plan = (pacing.get('banked_resets_plan') or {}).get('accounts') or []
    if not plan:
        return None
    total = sum(a.get('banked', 0) for a in plan)
    first_exp = min((a['expiries_utc'][0] for a in plan if a.get('expiries_utc')), default=None)
    urgent = [a for a in plan if a.get('advice', 'hold') != 'hold']
    left = {r['label']: r.get('weekly_remaining_pct') for r in records if r.get('provider') == 'openai-codex'}
    if urgent:
        who = ', '.join(f"{a['label']} ({left.get(a['label'], '?')}% left)" for a in urgent)
        # Use the plan's OWN verb. A hardcoded "REDEEM NOW" once headed a RECONCILE advice
        # whose reason said "confirm before redeeming" - the line contradicted itself.
        verb = urgent[0]['advice'].split(' (operator)', 1)[0].split(':', 1)[0].strip()
        api = [a for a in urgent if a.get('api_disagreement')]
        count = (f"{total} unused per the readings file, {sum(a.get('banked_reset_count_api') or 0 for a in plan)} available per the usage API"
                 if api else f"{total} unused")
        return f"Banked resets: {count} · {verb} → {who}. Reason: {urgent[0]['advice'].split(':', 1)[-1].strip()}"
    pct = ' / '.join(f"{lbl.replace('codex-', '')} {v:g}%" for lbl, v in left.items() if v is not None)
    exp = f"first expiry {first_exp[:10]}" if first_exp else 'expiry unknown'
    return f"Banked resets: {total} unused ({exp}) · no redeem needed now — hold; redeem when an account is ≤5% with more than a day to its reset ({pct})"


WINDOW_TOLERANCE_HOURS = 6.0


def _reset_windows(records):
    """Group records whose weekly resets fall within WINDOW_TOLERANCE_HOURS, soonest first.

    Accounts bought at different times sit on genuinely different reset windows, so one
    reset time can never stand for the whole pool - the 2026-09-16 report attached a
    Sep 19 reset to a total whose only non-zero component reset Sep 23.
    """
    ordered = sorted((r for r in records if parse_time(r.get('weekly_reset_at_utc'))),
                     key=lambda r: parse_time(r['weekly_reset_at_utc']))
    undated = [r for r in records if not parse_time(r.get('weekly_reset_at_utc'))]
    groups = []
    for r in ordered:
        head = parse_time(groups[-1][0]['weekly_reset_at_utc']) if groups else None
        if head and (parse_time(r['weekly_reset_at_utc']) - head).total_seconds() / 3600 <= WINDOW_TOLERANCE_HOURS:
            groups[-1].append(r)
        else:
            groups.append([r])
    if undated:
        groups.append(undated)
    return groups


def legacy_render_report(report, now, pacing=None):
    pacing = _pacing_doc() if pacing is None else pacing
    body = []
    openai = [r for r in openai_accounts(report['records']) if r.get('weekly_remaining_pct') is not None]
    co = pacing.get('combined_openai') or {}
    if not openai:
        # Never drop the pool silently: a report with no OpenAI section reads as "nothing to say"
        # when it actually means the telemetry read failed (observed 2026-09-16 on a bad cwd).
        bad = [r for r in report['records'] if r.get('provider') in ('openai-codex', None) and r.get('error_class')]
        why = ', '.join(sorted({str(r.get('error_class')) for r in bad})) or 'no OpenAI records returned'
        body.append(f"OpenAI: telemetry unavailable this sample ({why}) — capacity is UNKNOWN, not zero.")
    if openai:
        total_left = sum(r['weekly_remaining_pct'] for r in openai)
        active = pacing.get('active_openai_pool')
        mode = 'shared' if active == 'shared' else 'sequential'
        head = f"OpenAI ({mode}): {total_left:g} of {100 * len(openai)} pts left across {len(openai)} accounts"
        if active and active != 'shared':
            head += f" · ACTIVE {active}" if any(r['label'] == active for r in openai) else \
                    f" · pacing ledger names {active}, which this sample does not see"
        elif active is None:
            head += ' · no pool above the reserve target'
        body.append(head)
        by_label = {p.get('label'): p for p in pacing.get('pools', [])}
        for window in _reset_windows(openai):
            parts = ' · '.join(f"{r['label'].replace('codex-', '')} {r['weekly_remaining_pct']:g}%" for r in window)
            left = sum(r['weekly_remaining_pct'] for r in window)
            pace = ''
            if active == 'shared' and co.get('required_burn_pp_per_day') is not None and \
                    co.get('labels') and {r['label'] for r in window} <= set(co['labels']):
                pace = f" · need {co['required_burn_pp_per_day']} pts/day · pace {co['status']}"
            elif active and any(r['label'] == active for r in window):
                ap = by_label.get(active) or {}
                if ap.get('required_burn_pp_per_day') is not None:
                    pace = f" · need {ap['required_burn_pp_per_day']} pts/day · pace {ap.get('status', 'UNKNOWN')}"
            body.append(f"· resets {reset_text(window[0])}: {parts} ({left:g} of {100 * len(window)} pts){pace}")
    if report.get('openai_alias_labels'):
        # Never bury this: the pool is carrying duplicate credentials for one account, and
        # whoever owns ~/.hermes auth has to retire the alias. Counting is already correct.
        pairs = ', '.join(f'{alias} = {canonical}' for alias, canonical in sorted(report['openai_alias_labels'].items()))
        body.append(f"Credential pool: {pairs} — one account under two labels; counted once. Retire the duplicate credential.")
    if report.get('openai_duplicate_account_signature'):
        pairs = ', '.join(' = '.join(pair) for pair in report['openai_duplicate_account_signature'])
        body.append(f"Credential pool: {pairs} report the same reset instant and the same balance — "
                    'counted separately, but check whether one account is pooled twice.')
    for r in report['records']:
        if r.get('provider') == 'openai-codex':
            continue
        value = r.get('weekly_remaining_pct')
        pct = f'{value:g}%' if value is not None else 'unknown'
        label = r['label'].replace('claude-user-reported', 'Claude (fallback, no live read)').replace('claude-code', 'Claude all-models (CLI snapshot)')
        body.append(f"{label}: {pct} weekly left · resets {reset_text(r)} · {r['freshness']}")
    try:  # operator-reported Fable-only weekly (the CLI snapshot above is all-models, which under-reads the binding pool)
        rd = json.load(open(os.path.expanduser('~/business/ops/subscription-capacity/OPERATOR-READINGS.json')))
        fo = rd.get('claude_fable_only_weekly') or {}
        if fo.get('used_pct') is not None:
            fp = next((p for p in pacing.get('pools', []) if 'fable-only' in p.get('label', '')), {})
            if fp.get('status') == 'STALE_OPERATOR_READING':
                # An expired reading is not a live pool. Say so instead of publishing a rate.
                body.append(f"Claude Fable-only: last operator reading is stale — its weekly window "
                            f"(reset {reset_text({'weekly_reset_at_utc': fo.get('resets_at_utc')})}) has closed. "
                            'A fresh Fable-only reading is needed; no burn target can be computed.')
            else:
                need = f" · would need {fp['required_burn_pp_per_day']} pts/day to reach 5% by reset" if fp.get('required_burn_pp_per_day') is not None else ''
                body.append(f"Claude Fable-only (your report): {100 - float(fo['used_pct']):g}% weekly left · resets {reset_text({'weekly_reset_at_utc': fo.get('resets_at_utc')})}{need}")
    except Exception:
        pass
    rl = _redeem_line(pacing, report['records'])
    if rl:
        body.append(rl)
    digest = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()[:12]
    message = discord_fmt.render(
        discord_fmt.title('report', 'Subscription capacity', 'Usage and reset ledger'),
        *body, discord_fmt.meaning(routing_advice(report, now, pacing)),
        discord_fmt.subtext(f'ledger: ops/subscription-capacity/latest.json | sample {digest} | no resets or auth writes'),
        output_class='report',
    )
    errors = discord_fmt.lint(message, 'report')
    if errors['status'] != 'pass':
        raise RuntimeError('discord_house_style_invalid')
    return message


def pro_ledger(now):
    """Local relay submissions, not provider usage or an inferred Codex mapping."""
    config_path = ROOT / 'ops/gpt6pro-relay/config.json'
    try:
        config = json.loads(config_path.read_text())
        ledger = ROOT / config['ledger']
        rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
        result = []
        for account in config['accounts']:
            count = sum(1 for row in rows if row.get('account') == account['label']
                        and row.get('event') == 'submitted'
                        and 0 <= (now - parse_time(row['ts'])).total_seconds() < 7 * 86400)
            guard = account['weekly_messages']
            result.append({'label': account['profile'], 'plan': account['plan'],
                           'submitted_7d': count, 'local_weekly_guard': guard,
                           'local_guard_remaining': max(0, guard - count),
                           'metric': 'local_submissions_7d', 'provider_remaining_pct': None,
                           'provider_reset_at_utc': None, 'provider_verified': False,
                           'reserve_pct': config.get('budget_reserve_pct', 10),
                           'source': str(ledger.relative_to(ROOT)),
                           'observed_at': stamp(now),
                           'ledger_modified_at': stamp(datetime.fromtimestamp(ledger.stat().st_mtime, timezone.utc)),
                           'codex_mapping': 'unknown'})
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return [{'label': 'Pro relay', 'submitted_7d': None, 'local_weekly_guard': None,
                 'local_guard_remaining': None, 'metric': 'local_submissions_7d',
                 'provider_remaining_pct': None, 'provider_reset_at_utc': None,
                 'provider_verified': False, 'freshness': 'unavailable'}]


def s7_reset_tick(*args, **kwargs):
    from capacity_reset_policy import reset_tick
    return reset_tick(*args, **kwargs)


def s7_credit_snapshot():
    from agent.anthropic_credit_guard import credit_snapshot
    return credit_snapshot()


def s7_meter_read(**kwargs):
    from ops.n8n_execution_budget import health_check
    return health_check(**kwargs)


def s7_extensions(directory, now, *, dry_run_reset=False):
    reset = s7_reset_tick(directory, now=now, dry_run=dry_run_reset)
    credit = s7_credit_snapshot()
    due = datetime(2026, 10, 10, tzinfo=timezone.utc)
    meter = {'status': 'WAITING_OCT10', 'not_before_utc': stamp(due), 'billing_headroom': None}
    if now >= due:
        try:
            snapshot = s7_meter_read(state_path=directory / 'n8n-demand.json', now=now)['snapshot']
            meter.update(snapshot, billing_headroom=None)
        except Exception as exc:
            meter.update(status='UNKNOWN', error_class=type(exc).__name__)
    return {'auto_reset': reset, 'claude_api': credit, 'n8n_meter': meter,
            'n8n_executions_month': meter.get('month_execution_high_water'),
            'n8n_projected_month_end': meter.get('projected_month_end')}


def render_s7_report(report, now):
    accounts = report['auto_reset'].get('accounts', [])
    pieces = []
    for row in accounts:
        pct = row.get('remaining_pct')
        pieces.append(f"{row['label']} {pct if pct is not None else 'UNKNOWN'}% included left"
                      f" reset {row.get('reset_at_utc') or 'UNKNOWN'} banked {row.get('banked_resets', 'UNKNOWN')}")
    credit = report['claude_api']
    usd = credit.get('remaining_usd')
    balance = 'UNKNOWN' if usd is None else f'${usd:.2f}'
    line = ' · '.join(pieces) + f" · Claude API {balance} estimated grant left reset {credit.get('expires') or 'UNKNOWN'} floor $5"
    meter = report['n8n_meter']
    if meter['status'] == 'WAITING_OCT10':
        demand = 'GLP-338 readback held until Oct 10 00:00Z'
    else:
        demand = (f"GLP-338 n8n_executions_month={report.get('n8n_executions_month')}"
                  f" forecast={report.get('n8n_projected_month_end')} cap=10000 status={meter['status']}"
                  f" sampled={meter.get('generated_at_utc', 'UNKNOWN')}; retained demand, billing headroom UNKNOWN")
    return discord_fmt.render(discord_fmt.title('report', 'Subscription capacity', 'Included allowance'),
        line, f"Automatic reset {report['auto_reset']['status']} (account-2 at 0% only, never oauth-4)", demand,
        discord_fmt.subtext(f"sample {stamp(now)} | ops/subscription-capacity/latest.json"), output_class='report')


def render_report(report, now, pacing=None, alerts=()):
    if 'auto_reset' in report:
        return render_s7_report(report, now)
    body = list(alerts)
    if report.get('account_count_ok') is False:
        body.append('⚠️ Account coverage needs reconciliation: ' + ', '.join(report.get('collection_errors', [])))
    for record in openai_accounts(report['records']):
        value = record.get('weekly_remaining_pct')
        left = 'unknown' if value is None else f'{value:g}% left'
        api = (record.get('operator_only') or {}).get('banked_reset_count')
        body.append(f"**Codex {record['label']}**: {left} · reset {reset_text(record)} · {record['freshness']} · banked API {api if api is not None else 'unknown'}")
    for record in report['records']:
        if record.get('provider') == 'anthropic':
            value = record.get('weekly_remaining_pct')
            left = 'unknown' if value is None else f'{value:g}% left'
            body.append(f"**Anthropic** (one subscription, all-models): {left} · reset {reset_text(record)} · {record['freshness']}")
            extras = []
            for scope, title in (('five_hour', '5h'), ('seven_day_opus', 'Opus'),
                                 ('seven_day_sonnet', 'Sonnet')):
                window = (record.get('windows') or {}).get(scope) or {}
                value = window.get('remaining_pct')
                extras.append(f"{title} {f'{value:g}%' if value is not None else '?'} reset {reset_text({'weekly_reset_at_utc': window.get('reset_at_utc')})}")
            body.append('Claude scopes: ' + ' · '.join(extras))
    pro_parts = []
    for record in report.get('pro_chat_ledger', []):
        count = record.get('submitted_7d')
        usage = 'unknown (unavailable)' if count is None else f'{count} submitted in 7d'
        pro_parts.append(f"{record['label']} {usage}/{record.get('local_weekly_guard', '?')} local guard ({record.get('local_guard_remaining', '?')} slots)")
    if pro_parts:
        body.append('**Pro-chat local ledger**: ' + '; '.join(pro_parts) + ' · provider remaining/reset unknown')
    banked = report.get('banked_resets_operator', {})
    count = banked.get('count')
    pending = banked.get('pending_rollout_count')
    rollout = f" + {pending} pending rollout per dated operator report (live Codex API counts above; not additive)" if pending else ''
    observed = openai_accounts(report['records'])
    live_banked = [(r.get('operator_only') or {}).get('banked_reset_count') for r in observed]
    if observed and all(r.get('freshness') == 'live' and not r.get('error_class') for r in observed) and all(v is not None for v in live_banked):
        body.append(f"Measured Codex banked resets: {sum(live_banked)} across {len(observed)} observed accounts (not additional usage). Pro ledger is not provider quota; Pro/Codex mapping unknown; aliases counted once.")
    else:
        body.append(f"Banked resets: {count if count is not None else 'unknown'}{rollout} (operator {banked.get('reported_on') or 'unknown'}). Pro ledger is not provider quota; Codex mapping unknown; aliases counted once.")
    usable = [r for r in openai_accounts(report['records']) if r.get('freshness') == 'live'
              and r.get('capacity_status') in ('available', 'limited')
              and not r.get('error_class') and (r.get('weekly_remaining_pct') or 0) > RESERVE_TARGET_PCT]
    usable.sort(key=lambda r: r.get('weekly_reset_at_utc') or '9999')
    advice = (f"Prioritize useful work on {usable[0]['label']} before its reset; no quota reserve."
              if usable else 'No measured Codex capacity remaining; verify telemetry before discretionary work.')
    advice += ' No forced switching, quota-filling busywork or reset redemption.'
    return discord_fmt.render(discord_fmt.title('report', 'Subscription capacity', 'Per-account usage'),
        *body, discord_fmt.meaning(advice),
        discord_fmt.subtext(f"sample {report.get('collected_at_utc', stamp(now))} | ops/subscription-capacity/latest.json"),
        output_class='report')


def secure_token():
    descriptor = os.open(TOKEN_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) % 64 or info.st_uid != os.geteuid():
            raise RuntimeError('discord_token_file_not_secure')
        value = os.read(descriptor, 8192).decode().strip()
    finally:
        os.close(descriptor)
    if not value:
        raise RuntimeError('discord_token_empty')
    return value


def verified_send(message, directory, request=None):
    """Persist POST intent/ID and reconcile exact readback before dedupe ACK."""
    import httpx
    token = secure_token() if request is None else None
    def api(method, path, payload=None):
        if request is not None:
            return request(method, path, payload)
        response = httpx.request(method, 'https://discord.com/api/v10' + path,
            headers={'Authorization': 'Bot ' + token, 'User-Agent': 'HermesCapacityMonitor/1.0'},
            json=payload, timeout=20)
        if response.status_code >= 400:
            raise RuntimeError('discord_http_' + str(response.status_code))
        return response.json()
    pending_path = directory / 'pending-delivery.json'
    pending = load_json(pending_path, None)
    if pending:
        # Never blind-repeat an uncertain POST. Reconcile its exact content first.
        message = pending['content']
        if pending.get('message_id'):
            observed = api('GET', f"/channels/{CHANNEL}/messages/{pending['message_id']}")
        else:
            recent = api('GET', f'/channels/{CHANNEL}/messages?limit=50')
            matches = [m for m in recent if m.get('content') == message and m.get('author', {}).get('bot')]
            if len(matches) != 1:
                raise RuntimeError('ambiguous_discord_post_requires_reconciliation')
            observed = matches[0]
        if observed.get('content') != message or observed.get('channel_id') != CHANNEL:
            raise RuntimeError('discord_readback_mismatch')
    else:
        channel = api('GET', f'/channels/{CHANNEL}')
        channels = api('GET', f"/guilds/{channel['guild_id']}/channels")
        if not any(c.get('id') == CHANNEL and 'ops-alerts' in c.get('name', '') for c in channels):
            raise RuntimeError('discord_target_mismatch')
        atomic_json(pending_path, {'content': message, 'created_at': stamp(now_utc())})
        posted = api('POST', f'/channels/{CHANNEL}/messages', {'content': message, 'allowed_mentions': {'parse': []}})
        atomic_json(pending_path, {'content': message, 'message_id': posted['id']})
        observed = api('GET', f"/channels/{CHANNEL}/messages/{posted['id']}")
        if observed.get('content') != message or observed.get('channel_id') != CHANNEL:
            raise RuntimeError('discord_readback_mismatch')
    receipt = {'message_id': observed['id'], 'channel_id': CHANNEL, 'content': message,
               'verified_at': stamp(now_utc()), 'exact_readback': True}
    atomic_json(directory / 'last-delivery.json', receipt)
    pending_path.unlink()
    return receipt


# How many OpenAI accounts SHOULD be pooled. Set HERMES_EXPECTED_OPENAI_ACCOUNTS to pin it
# deliberately; unset means "whatever is actually pooled", so a plan change never raises a
# false mismatch. The constant was hardcoded 2, then moved to 3 - each move re-armed the same
# alarm for the next plan change, so it is data-driven now.
EXPECTED_OPENAI_ACCOUNTS = (
    int(os.environ['HERMES_EXPECTED_OPENAI_ACCOUNTS'])
    if os.environ.get('HERMES_EXPECTED_OPENAI_ACCOUNTS', '').isdigit() else None)


def tick(directory=STATE_DIR, *, notify=True, force=False, collector=collect_monitored_capacity, sender=verified_send, now=None, extension_reader=None):
    now = now or now_utc()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = collector(now=now).to_dict(include_operator_only=True)
        if extension_reader is not None:
            report.update(extension_reader(directory, now))
        report['pro_chat_ledger'] = pro_ledger(now)
        readings = load_json(STATE_DIR / 'OPERATOR-READINGS.json', {})
        banked = readings.get('banked_resets', {})
        report['banked_resets_operator'] = {'count': banked.get('total_remaining'),
                                           'reported_on': banked.get('reported_on'),
                                           'pending_rollout_count': banked.get('pending_rollout_total'),
                                           'expected_after_rollout': banked.get('total_expected_after_rollout'),
                                           'accounts': banked.get('accounts', {})}
        if banked.get('accounts'):
            # Current operator readings supersede the collector's dated fallback slots;
            # the measured records and their live API counts remain untouched.
            report['operator_only'] = [dict(info, label=label, reported_on=banked.get('reported_on'))
                                       for label, info in banked['accounts'].items()]
        # Collapse credential ALIASES onto their account before anything counts, totals or
        # routes off the record list.
        roster = openai_roster(directory / 'ACTIVE-POOL.json')
        aliases, collisions = mark_duplicate_labels(report['records'], roster)
        report['openai_account_roster'] = roster
        if aliases:
            report['openai_alias_labels'] = dict(sorted(aliases.items()))
        report['routing_advice'] = 'Prioritize useful work before each measured account reset; no quota reserve. No cross-plan quota conversion, forced switching or reset redemption.'
        # Operator added a third OpenAI 5x plan 2026-09-16 while account-1 sat at 5% and
        # account-2-real at 0%. A hardcoded 2 made every run report
        # openai_account_count_mismatch and would do so again on the next plan change, so
        # the expectation is configurable and defaults to whatever is actually pooled.
        actual = openai_accounts(report['records'])
        # A count alone let a dead 401 credential row stand in for a missing account
        # (2026-09-16 00:46Z: codex-account-1 absent, a phantom codex-account-2 row present,
        # account_count_ok True, and the Discord report understated the pool by 100 points).
        # Count only records that actually reported capacity, and watch the label SET.
        healthy = [r for r in actual if not r.get('error_class') and r.get('weekly_remaining_pct') is not None]
        labels = sorted({r['label'] for r in healthy})
        if collisions:
            # Two labels that both stand for accounts read as ONE upstream account. Publish
            # the evidence and keep counting them apart: an identical reset instant is strong
            # evidence of duplication, not proof, and no read actually failed here.
            report['openai_duplicate_account_signature'] = collisions
        expected = EXPECTED_OPENAI_ACCOUNTS
        if expected is None:
            # The operator's declared roster, not the sample itself. `len(labels)` made the
            # check self-ratifying: an account could vanish, alarm for exactly one sample,
            # and the reduced pool became the expectation from the next sample on.
            expected = len(roster) or len(labels) or 1
        report['expected_openai_accounts'] = expected
        report['openai_account_labels'] = labels
        previous = load_json(directory / 'latest.json', {}).get('openai_account_labels')
        report['account_count_ok'] = (
            len(healthy) == expected and len(labels) == expected
            and not report['collection_errors'])
        if not report['account_count_ok']:
            report['collection_errors'].append('openai_account_count_mismatch')
        # Membership drift is a different defect from a count change and must be visible even
        # when the count happens to match. Measure it against the declared roster when there
        # is one, so the alarm PERSISTS; a previous-sample baseline was one-shot. Compare only
        # two samples that both actually read the pool when there is no roster.
        baseline = roster if roster else previous
        if baseline and labels and sorted(baseline) != labels:
            report['collection_errors'].append('openai_account_set_changed')
            report['account_count_ok'] = False
        atomic_json(directory / 'latest.json', report)
        with (directory / 'history.jsonl').open('a') as stream:
            stream.write(json.dumps(report, sort_keys=True) + '\n')
        state = alert_state(report, now)
        local = now.astimezone(PACIFIC)
        day = local.date().isoformat()
        acknowledged = load_json(directory / 'notification-state.json', {})
        daily = local.hour >= 9 and acknowledged.get('daily_date') != day
        changed = state != acknowledged.get('alert_state') or acknowledged.get('report_version') != 2
        if notify and (force or daily or changed or (directory / 'pending-delivery.json').exists()):
            previous_state = {(s.get('label'), s.get('scope')): s for s in acknowledged.get('alert_state', [])}
            crossings = []
            for current in state:
                previous = previous_state.get((current.get('label'), current.get('scope')))
                if previous and previous.get('bucket') != current.get('bucket') and current.get('bucket') not in (None, 'unknown', 'normal'):
                    kind = 'local guard' if current.get('scope') == 'local_submissions_7d' else 'provider remaining'
                    crossings.append(f"{current['label']} {current['scope']} {kind} ≤{current['bucket']}%")
            alert_line = ('⚠️ **Capacity threshold**: ' + '; '.join(crossings)) if crossings else None
            message = render_report(report, now, alerts=[alert_line] if alert_line else ())
            receipt = sender(message, directory)
            # A recovered old pending receipt doesn't acknowledge newer data.
            if receipt.get('content') == message:
                atomic_json(directory / 'notification-state.json', {'alert_state': state, 'daily_date': day,
                    'message_id': receipt['message_id'], 'acknowledged_at': stamp(now), 'report_version': 2})
        return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-notify', action='store_true')
    parser.add_argument('--force-report', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--dry-run-reset', action='store_true', help='GET-only reset decision; never consume a reset')
    args = parser.parse_args()
    try:
        report = tick(notify=not args.no_notify, force=args.force_report,
            extension_reader=lambda directory, now: s7_extensions(directory, now, dry_run_reset=args.dry_run_reset))
        if args.json:
            print(json.dumps(report, indent=2))
        return 0
    except Exception as exc:
        # No exception text: an HTTP adapter may carry token-bearing headers.
        safe = {'status': 'ERROR', 'error_class': type(exc).__name__, 'at': stamp(now_utc())}
        atomic_json(STATE_DIR / 'last-error.json', safe)
        print(json.dumps(safe), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
