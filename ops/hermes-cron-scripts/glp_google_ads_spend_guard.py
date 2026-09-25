#!/usr/bin/env python3
"""Silent deterministic spend guard for the current GLP Google Search campaign.

Cron semantics:
- empty stdout = healthy/silent;
- stdout is emitted only when the guard takes action;
- non-zero exit alerts ops because a broken spend guard must not fail silently.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CONTROL_PLANE = Path('/home/ubuntu/business/ventures/glp-clinic-scout/ads-control-plane')
VENV_PY = CONTROL_PLANE / '.venv/bin/python'
OUT = Path('/home/ubuntu/business/reports/glp-ads-spend-guard')
CUSTOMER_ID = '1177599039'
CAMPAIGN_ID = '23967577167'
CAMPAIGN_RN = f'customers/{CUSTOMER_ID}/campaigns/{CAMPAIGN_ID}'
APPROVED_HARD_CAP_USD = 12.0
SOFT_PAUSE_USD = 8.0
PT = ZoneInfo('America/Los_Angeles')

sys.path.insert(0, str(CONTROL_PLANE))
sys.path.insert(0, '/home/ubuntu/business/ops')
sys.path.insert(0, '/home/ubuntu/business/ops/operator-console')
from discord_fmt import (
    title as fmt_title, kv as fmt_kv, kvline as fmt_kvline, section as fmt_section,
    meaning as fmt_meaning, subtext as fmt_subtext, render as fmt_render,
)
import red_alert_hook  # noqa: E402
from monitor_heartbeats import emit_heartbeat  # noqa: E402


if VENV_PY.exists() and Path(sys.prefix).resolve() != (CONTROL_PLANE / '.venv').resolve():
    os.execv(str(VENV_PY), [str(VENV_PY), __file__, *sys.argv[1:]])

from adsctl.google_optimizer_adapter import get_google_client  # noqa: E402


def usd_to_micros(value: float) -> int:
    return int(round(value * 1_000_000))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def enum_name(value) -> str:
    return getattr(value, 'name', str(value))


def write_latest(payload: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'latest.json').write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    day = datetime.now(timezone.utc).astimezone(PT).date().isoformat()
    (OUT / f'{day}.json').write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def raise_spend_red_alert(payload: dict, *, live: bool = True):
    campaign_id = str(payload.get('campaign_id') or 'spend-cap')
    alert = {
        'severity': 'RED', 'source': 'Spend Guard', 'impact': 'campaign paused at spend cap',
        'what': f"The Google Ads guard paused the campaign after same-day spend reached ${payload.get('same_day_spend_usd')}.",
        'action': 'Verify the campaign is paused; investigate only if its state is unexpected.',
        'where': f'{OUT / "latest.json"} · campaign {campaign_id}',
        'evidence_codes': [str(payload.get('action') or 'spend_guard_action')],
        'url': None, 'url_title': None,
        'dedup_key': f'Spend Guard:{campaign_id}',
        'emergency': False, 'recovery': False,
    }
    return red_alert_hook.raise_alert(alert, live=live)


def main() -> int:
    client = get_google_client()
    service = client.get_service('GoogleAdsService')
    day = datetime.now(timezone.utc).astimezone(PT).date().isoformat()
    config_query = f"""
      SELECT campaign.id, campaign.name, campaign.status, campaign.primary_status,
             campaign.serving_status
      FROM campaign
      WHERE campaign.id = {CAMPAIGN_ID}
      LIMIT 1
    """
    metrics_query = f"""
      SELECT campaign.id, campaign.name, campaign.status, campaign.primary_status,
             campaign.serving_status, metrics.cost_micros, metrics.clicks,
             metrics.impressions, metrics.conversions, segments.date
      FROM campaign
      WHERE campaign.id = {CAMPAIGN_ID} AND segments.date = '{day}'
      LIMIT 1
    """
    config_rows = list(service.search(customer_id=CUSTOMER_ID, query=config_query))
    if config_rows:
        row = config_rows[0]
        status = enum_name(row.campaign.status)
        primary_status = enum_name(row.campaign.primary_status)
        serving_status = enum_name(row.campaign.serving_status)
        campaign_name = row.campaign.name
    else:
        status = 'UNKNOWN'
        primary_status = 'UNKNOWN'
        serving_status = 'UNKNOWN'
        campaign_name = ''
    metrics_rows = list(service.search(customer_id=CUSTOMER_ID, query=metrics_query))
    if metrics_rows:
        row = metrics_rows[0]
        spend_micros = int(row.metrics.cost_micros or 0)
        clicks = int(row.metrics.clicks or 0)
        impressions = int(row.metrics.impressions or 0)
        conversions = float(row.metrics.conversions)
    else:
        spend_micros = 0
        clicks = 0
        impressions = 0
        conversions = 0.0
    spend_usd = round(spend_micros / 1_000_000, 6)
    payload = {
        'schema_version': 'glp_google_ads_spend_guard_v2',
        'checked_at_utc': utc_now(),
        'pt_date': day,
        'campaign_id': CAMPAIGN_ID,
        'campaign_name': campaign_name,
        'campaign_status_before': status,
        'campaign_primary_status_before': primary_status,
        'campaign_serving_status_before': serving_status,
        'same_day_spend_usd': spend_usd,
        'same_day_clicks': clicks,
        'same_day_impressions': impressions,
        'same_day_conversions': conversions,
        'approved_hard_cap_usd': APPROVED_HARD_CAP_USD,
        'soft_pause_usd': SOFT_PAUSE_USD,
        'action': 'none',
        'secret_values_printed': False,
    }
    if status == 'ENABLED' and spend_micros >= usd_to_micros(SOFT_PAUSE_USD):
        campaign_service = client.get_service('CampaignService')
        operation = client.get_type('CampaignOperation')
        campaign = operation.update
        campaign.resource_name = CAMPAIGN_RN
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        operation.update_mask.paths.append('status')
        resp = campaign_service.mutate_campaigns(request={'customer_id': CUSTOMER_ID, 'operations': [operation]})
        payload['action'] = 'paused_soft_cap_reached'
        payload['mutated_resources'] = [r.resource_name for r in resp.results]
        # Verify status after mutation.
        verify_rows = list(service.search(customer_id=CUSTOMER_ID, query=config_query))
        payload['campaign_status_after'] = enum_name(verify_rows[0].campaign.status) if verify_rows else 'UNKNOWN'
        payload['red_alert_hook_receipt'] = raise_spend_red_alert(payload, live=True)
    write_latest(payload)
    if payload['action'] != 'none':
        print(fmt_render(
            fmt_title('money', 'Spend Guard', f"${spend_usd} — {payload['action'].replace('_', ' ')}"),
            fmt_kvline(('spend', f'${spend_usd}'), ('soft-pause', f'${SOFT_PAUSE_USD}'),
                       ('hard cap', f'${APPROVED_HARD_CAP_USD}'), ('campaign ID shown', False),
                       ('status after', payload.get('campaign_status_after'))),
            fmt_meaning('guard already acted — verify campaign state in Google Ads only if unexpected'),
            fmt_subtext(f'artifact: {OUT / "latest.json"}'),
        ))
    return 0


if __name__ == '__main__':
    try:
        if '--s2-fixture-red-branch' in sys.argv:
            OUT.mkdir(parents=True, exist_ok=True)
            payload = {'action': 'paused_soft_cap_reached', 'same_day_spend_usd': 8.01, 'soft_pause_usd': SOFT_PAUSE_USD, 'approved_hard_cap_usd': APPROVED_HARD_CAP_USD, 'campaign_id': CAMPAIGN_ID, 'secret_values_printed': False}
            receipt = raise_spend_red_alert(payload, live=False)
            out = Path('/home/ubuntu/business/reports/claude-audit-20260707/operator-delivery-20260708/s2-glp-spend-guard-real-branch-fixture.json')
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({'status': 'pass', 'real_branch': 'glp_google_ads_spend_guard_soft_pause', 'live_send': False, 'hook_receipt': receipt, 'secret_printed': False}, indent=2) + '\n')
            print(json.dumps({'status': 'pass', 'receipt': str(out), 'live_send': False, 'secret_printed': False}, indent=2))
            raise SystemExit(0)
        rc = main()
        if rc == 0:
            emit_heartbeat('glp_spend_guard', upstream_ok=True, auth_valid=True, detail='wrapper_complete')
        raise SystemExit(rc)
    except Exception as exc:  # noqa: BLE001
        error = {
            'schema_version': 'glp_google_ads_spend_guard_v2',
            'checked_at_utc': utc_now(),
            'status': 'error',
            'error_class': type(exc).__name__,
            'error_tail_redacted': str(exc)[-1200:],
            'secret_values_printed': False,
        }
        write_latest(error)
        print(fmt_render(
            fmt_title('action', 'Spend Guard', f'guard crashed: {type(exc).__name__}'),
            fmt_meaning('spend guard is NOT protecting the cap — inspect and rerun before next spend window'),
            fmt_subtext(f'artifact: {OUT / "latest.json"}'),
        ))
        raise SystemExit(1)
