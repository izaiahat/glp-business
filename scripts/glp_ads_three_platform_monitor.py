#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path('/home/ubuntu/business')
CONTROL = ROOT / 'ventures/glp-clinic-scout/ads-control-plane'
REPORT_DIR = ROOT / 'reports/glp-clinic-scout/ads/final-launch-pass-20260831/monitor'
LATEST = REPORT_DIR / 'latest.json'
HISTORY = REPORT_DIR / 'history'
BRIDGE_RECEIPTS = ROOT / 'reports/ops/n8n-bridge-jobs'
DEPLOY_CLOSEOUT = ROOT / 'reports/glp-clinic-scout/affiliate-machine/deploy-final-20260831/FINAL-DEPLOY-CLOSEOUT.json'
N8N_KEY_PATH = Path('/home/ubuntu/.hermes/secrets/n8n/native-api-key.txt')
META_TOKEN_PATH = Path('/home/ubuntu/.hermes/secrets/meta/glp-clinic-scout-system-user-token.txt')
HERMES_ENV = Path('/home/ubuntu/.hermes/.env')
GOOGLE_CUSTOMER = '1177599039'
GOOGLE_LOGIN = '2847481998'
META_REQUESTED = 'act_243816027637970'
META_MATCHING = 'act_243816027637970'
MEASUREMENT_CONFIG = ROOT / 'ops/glp/ads-monitor-measurement.json'
CURRENT_CONTENT_BASE = ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
CLICK_PARITY = ROOT / 'reports/ops/orchestration-restore-20260907/PLAN/D1/CLICK-PARITY.md'
MICROSOFT_ACCOUNT = '189346863'
MICROSOFT_CAMPAIGN = '571604457'
NOTIFICATION_PROTOCOL = 'legacy'
DISCORD_OPS = '1487300132054765629'
DISCORD_UA = 'DiscordBot (https://discordapp.com, 10)'
N8N_BASE = 'https://hermesops.app.n8n.cloud/api/v1'
POSTBACK_WORKFLOWS = {
    'receiver': 'dG2y4DqpoHEMCLfD',
    'ship': 'ubpIUcu56h97S1Sf',
}
# No account-native approved-sale/export binding has been evidenced. An arbitrary
# environment ID is not evidence and must not turn platform actions into sales.
META_APPROVED_SALE_CUSTOM_CONVERSION_ID = None
META_APPROVED_SALE_ACTION_TYPE = None


def now() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def safe_read(path: Path) -> str | None:
    try:
        value = path.read_text(encoding='utf-8').strip()
    except (FileNotFoundError, PermissionError):
        return None
    return value or None


def redacted_error_tail(exc: Exception) -> str:
    # Provider exception strings are untrusted and can embed credentials in
    # arbitrary formats (headers, query strings, JSON, or free text). Keep
    # only the exception class; semantic details belong in typed status fields.
    return f'{exc.__class__.__name__}: [REDACTED]'


def canonical_policy_reason_sha256(value: Any) -> str:
    """Hash policy-reason semantics without serializing provider text."""

    def normalize(item: Any) -> Any:
        if item is None or isinstance(item, (bool, int, float, str)):
            return item
        if isinstance(item, bytes):
            return {'bytes_sha256': hashlib.sha256(item).hexdigest()}
        if isinstance(item, dict):
            return {
                str(key): normalize(item[key])
                for key in sorted(item, key=lambda key: str(key))
            }
        if isinstance(item, (list, tuple, set)):
            normalized = [normalize(child) for child in item]
            return sorted(
                normalized,
                key=lambda child: json.dumps(
                    child, sort_keys=True, separators=(',', ':'), ensure_ascii=True
                ),
            )
        enum_name = getattr(item, 'name', None)
        if isinstance(enum_name, str):
            return enum_name
        protobuf = getattr(item, '_pb', item)
        if getattr(protobuf, 'DESCRIPTOR', None) is not None:
            try:
                from google.protobuf.json_format import MessageToDict

                return normalize(MessageToDict(
                    protobuf,
                    preserving_proto_field_name=True,
                    use_integers_for_enums=False,
                ))
            except Exception:
                pass
        return str(item)

    canonical = json.dumps(
        normalize(value), sort_keys=True, separators=(',', ':'), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def policy_state_fingerprint(identity: str, statuses: list[str], reasons: Any) -> str:
    payload = {
        'identity': identity,
        'statuses': statuses,
        'policy_reasons_sha256': canonical_policy_reason_sha256(reasons),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def http_json(url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None,
              timeout: int = 45) -> tuple[int, dict[str, Any]]:
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode('utf-8'))
        except Exception:
            payload = {'error_class': 'HTTPError', 'http_status': exc.code}
        return exc.code, payload


def collect_google() -> dict[str, Any]:
    sys.path.insert(0, str(CONTROL))
    os.environ.setdefault('GLP_GOOGLE_ADS_LOGIN_CUSTOMER_ID', GOOGLE_LOGIN)
    try:
        from adsctl.google_ads_client import _build_google_ads_client_config
        from adsctl.google_ads_version import resolve_google_ads_api_version
        from google.ads.googleads.client import GoogleAdsClient

        client = GoogleAdsClient.load_from_dict(
            _build_google_ads_client_config(),
            version=resolve_google_ads_api_version(),
        )
        service = client.get_service('GoogleAdsService')
        customer_rows = list(service.search(
            customer_id=GOOGLE_CUSTOMER,
            query=(
                'SELECT customer.id, customer.descriptive_name, customer.currency_code, '
                'customer.time_zone, customer.status FROM customer'
            ),
        ))
        customer = customer_rows[0].customer if customer_rows else None
        campaigns = []
        query = (
            'SELECT campaign.id, campaign.name, campaign.status, '
            'campaign.advertising_channel_type, campaign_budget.amount_micros '
            'FROM campaign ORDER BY campaign.id'
        )
        for row in service.search(customer_id=GOOGLE_CUSTOMER, query=query):
            campaigns.append({
                'id': str(row.campaign.id),
                'name': row.campaign.name,
                'status': row.campaign.status.name,
                'channel': row.campaign.advertising_channel_type.name,
                'budget_micros': int(row.campaign_budget.amount_micros or 0),
            })
        def metric_window(date_range: str) -> dict[str, Any]:
            metrics = None
            metric_query = (
                'SELECT metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions '
                f'FROM customer WHERE segments.date DURING {date_range}'
            )
            for row in service.search(customer_id=GOOGLE_CUSTOMER, query=metric_query):
                metrics = {
                    'impressions': int(row.metrics.impressions or 0),
                    'clicks': int(row.metrics.clicks or 0),
                    'cost_micros': int(row.metrics.cost_micros or 0),
                    'conversions': float(row.metrics.conversions or 0),
                }
            return {**metrics, 'report_status': 'SUCCESS', 'rows': 1} if metrics is not None else {
                'report_status': 'SUCCESS', 'rows': 0,
                'impressions': None, 'clicks': None, 'cost_micros': None, 'conversions': None,
            }

        today = metric_window('TODAY')
        last_30d = metric_window('LAST_30_DAYS')

        inventory: dict[str, Any] = {
            'ad_groups': {'total': 0, 'by_status': {}},
            'keywords': {'total': 0, 'negative': 0, 'by_status': {}, 'by_match_type': {}},
            'ads': {
                'total': 0,
                'with_final_urls': 0,
                'by_status': {},
                'by_approval_status': {},
                'policy_readback_available': True,
                'policy_state_sha256': None,
                'policy_issue_item_refs': [],
            },
            'assets': {'total': 0, 'with_final_urls': 0, 'by_type': {}},
            'campaign_asset_associations': 0,
            'ad_group_asset_associations': 0,
            'conversion_actions': {'total': 0, 'enabled': 0, 'primary': 0},
        }

        def bump(node: dict[str, int], key: str) -> None:
            node[key] = node.get(key, 0) + 1

        for row in service.search(
            customer_id=GOOGLE_CUSTOMER,
            query='SELECT ad_group.id, ad_group.status FROM ad_group ORDER BY ad_group.id',
        ):
            inventory['ad_groups']['total'] += 1
            bump(inventory['ad_groups']['by_status'], row.ad_group.status.name)

        for row in service.search(
            customer_id=GOOGLE_CUSTOMER,
            query=(
                'SELECT ad_group_criterion.criterion_id, ad_group_criterion.status, '
                'ad_group_criterion.negative, ad_group_criterion.keyword.match_type '
                'FROM keyword_view ORDER BY ad_group_criterion.criterion_id'
            ),
        ):
            inventory['keywords']['total'] += 1
            inventory['keywords']['negative'] += int(bool(row.ad_group_criterion.negative))
            bump(inventory['keywords']['by_status'], row.ad_group_criterion.status.name)
            bump(inventory['keywords']['by_match_type'], row.ad_group_criterion.keyword.match_type.name)

        google_policy_states: list[str] = []
        google_policy_issue_refs: list[str] = []
        for row in service.search(
            customer_id=GOOGLE_CUSTOMER,
            query=(
                'SELECT ad_group_ad.ad.id, ad_group_ad.status, ad_group_ad.ad.final_urls, '
                'ad_group_ad.policy_summary.approval_status, '
                'ad_group_ad.policy_summary.policy_topic_entries '
                'FROM ad_group_ad ORDER BY ad_group_ad.ad.id'
            ),
        ):
            ad_id = str(row.ad_group_ad.ad.id)
            approval_status = row.ad_group_ad.policy_summary.approval_status.name
            inventory['ads']['total'] += 1
            inventory['ads']['with_final_urls'] += int(bool(list(row.ad_group_ad.ad.final_urls)))
            bump(inventory['ads']['by_status'], row.ad_group_ad.status.name)
            bump(inventory['ads']['by_approval_status'], approval_status)
            google_policy_states.append(policy_state_fingerprint(
                ad_id,
                [approval_status],
                list(row.ad_group_ad.policy_summary.policy_topic_entries),
            ))
            if approval_status == 'DISAPPROVED':
                google_policy_issue_refs.append(
                    'ad_sha256_' + hashlib.sha256(ad_id.encode('utf-8')).hexdigest()[:16]
                )
        inventory['ads']['policy_state_sha256'] = hashlib.sha256(
            '\n'.join(sorted(google_policy_states)).encode('utf-8')
        ).hexdigest()
        inventory['ads']['policy_issue_item_refs'] = sorted(google_policy_issue_refs)

        for row in service.search(
            customer_id=GOOGLE_CUSTOMER,
            query='SELECT asset.id, asset.type, asset.final_urls FROM asset ORDER BY asset.id',
        ):
            inventory['assets']['total'] += 1
            inventory['assets']['with_final_urls'] += int(bool(list(row.asset.final_urls)))
            bump(inventory['assets']['by_type'], row.asset.type_.name)

        for _ in service.search(
            customer_id=GOOGLE_CUSTOMER,
            query='SELECT campaign_asset.resource_name FROM campaign_asset',
        ):
            inventory['campaign_asset_associations'] += 1

        for _ in service.search(
            customer_id=GOOGLE_CUSTOMER,
            query='SELECT ad_group_asset.resource_name FROM ad_group_asset',
        ):
            inventory['ad_group_asset_associations'] += 1

        for row in service.search(
            customer_id=GOOGLE_CUSTOMER,
            query=(
                'SELECT conversion_action.id, conversion_action.status, conversion_action.primary_for_goal '
                'FROM conversion_action ORDER BY conversion_action.id'
            ),
        ):
            inventory['conversion_actions']['total'] += 1
            inventory['conversion_actions']['enabled'] += int(row.conversion_action.status.name == 'ENABLED')
            inventory['conversion_actions']['primary'] += int(bool(row.conversion_action.primary_for_goal))

        return {
            'status': 'PASS',
            'account_id': GOOGLE_CUSTOMER,
            'identity': {
                'id': str(customer.id) if customer else None,
                'descriptive_name': customer.descriptive_name if customer else None,
                'currency': customer.currency_code if customer else None,
                'time_zone': customer.time_zone if customer else None,
                'status': customer.status.name if customer else None,
            },
            'campaigns': campaigns,
            'inventory': inventory,
            'configured_enabled_campaigns': sum(row['status'] == 'ENABLED' for row in campaigns),
            'paused_campaigns': sum(row['status'] == 'PAUSED' for row in campaigns),
            'removed_campaigns': sum(row['status'] == 'REMOVED' for row in campaigns),
            'today': today,
            'last_30d': last_30d,
            'today_spend_usd': today['cost_micros'] / 1_000_000 if today['cost_micros'] is not None else None,
            'last_30d_spend_usd': last_30d['cost_micros'] / 1_000_000 if last_30d['cost_micros'] is not None else None,
            'secret_values_printed': False,
        }
    except Exception as exc:
        return {
            'status': 'BLOCKED',
            'account_id': GOOGLE_CUSTOMER,
            'error_class': exc.__class__.__name__,
            'error_tail_redacted': redacted_error_tail(exc),
            'secret_values_printed': False,
        }


def collect_meta_account(account_id: str, token: str) -> dict[str, Any]:
    base = 'https://graph.facebook.com/v25.0'
    account_status, account = http_json(
        f'{base}/{account_id}',
        params={'access_token': token, 'fields': 'id,name,account_status,currency,timezone_name,business_name,disable_reason'},
    )
    campaign_status, campaigns_payload = meta_collection(
        f'{base}/{account_id}/campaigns',
        params={'access_token': token, 'fields': 'id,name,status,effective_status,start_time,stop_time,budget_remaining', 'limit': 500},
    )
    ads_status, ads_payload = meta_collection(
        f'{base}/{account_id}/ads',
        params={'access_token': token, 'fields': 'id,status,effective_status,issues_info', 'limit': 500},
    )
    insights_status, insights_payload = meta_collection(
        f'{base}/{account_id}/insights',
        params={
            'access_token': token,
            'fields': 'spend,impressions,clicks,actions',
            'date_preset': 'today',
            'level': 'account',
        },
    )
    adset_status, adsets_payload = meta_collection(
        f'{base}/{account_id}/adsets',
        params={'access_token': token, 'fields': 'id,campaign_id,status,effective_status', 'limit': 500},
    )
    adsets_readable = adset_status == 200
    adsets = adsets_payload.get('data', []) if adsets_readable else []
    campaigns_readable = campaign_status == 200
    ads_readable = ads_status == 200
    insights_readable = insights_status == 200
    campaigns = campaigns_payload.get('data', []) if campaigns_readable and isinstance(campaigns_payload, dict) else []
    ads = ads_payload.get('data', []) if ads_readable and isinstance(ads_payload, dict) else []
    insight_rows = insights_payload.get('data', []) if insights_readable and isinstance(insights_payload, dict) else []
    spend = sum(float(row['spend']) for row in insight_rows) if insight_rows and all(row.get('spend') is not None for row in insight_rows) else None
    clicks = sum(int(row['clicks']) for row in insight_rows) if insight_rows and all(row.get('clicks') is not None for row in insight_rows) else None
    impressions = sum(int(row['impressions']) for row in insight_rows) if insight_rows and all(row.get('impressions') is not None for row in insight_rows) else None
    action_values: dict[str, float] = {}
    if insights_readable:
        for insight in insight_rows:
            for action in insight.get('actions') or []:
                action_type = str(action.get('action_type') or '')
                if not META_APPROVED_SALE_ACTION_TYPE or action_type != META_APPROVED_SALE_ACTION_TYPE:
                    continue
                action_values[action_type] = action_values.get(action_type, 0.0) + float(
                    action.get('value') or 0
                )
    normalized_conversions = (
        action_values.get(META_APPROVED_SALE_ACTION_TYPE, 0.0)
        if insights_readable and META_APPROVED_SALE_ACTION_TYPE else None
    )
    errors = [status for status in (account_status, campaign_status, adset_status, ads_status, insights_status) if status != 200]
    meta_policy_issue_statuses = {'DISAPPROVED', 'WITH_ISSUES', 'PENDING_REVIEW', 'PREAPPROVED'}
    policy_issue_count = sum(
        bool(row.get('issues_info')) or str(row.get('effective_status') or '').upper() in meta_policy_issue_statuses
        for row in ads
    ) if ads_readable else None
    meta_policy_states = [
        policy_state_fingerprint(
            str(row.get('id')),
            [str(row.get('status')), str(row.get('effective_status'))],
            row.get('issues_info'),
        )
        for row in ads
    ]
    meta_policy_issue_refs = sorted(
        'ad_sha256_' + hashlib.sha256(str(row.get('id')).encode('utf-8')).hexdigest()[:16]
        for row in ads
        if bool(row.get('issues_info'))
        or str(row.get('effective_status') or '').upper() in meta_policy_issue_statuses
    ) if ads_readable else None
    return {
        'status': 'PASS' if not errors else 'BLOCKED',
        'account_id': account_id,
        'name': account.get('name') if isinstance(account, dict) else None,
        'business_name': account.get('business_name') if isinstance(account, dict) else None,
        'account_status': account.get('account_status') if isinstance(account, dict) else None,
        'disable_reason': account.get('disable_reason') if isinstance(account, dict) else None,
        'state_readback_available': not errors,
        'campaign_readback_available': campaigns_readable,
        'ad_policy_readback_available': ads_readable,
        'spend_readback_available': spend is not None,
        'adset_readback_available': adsets_readable,
        'adsets': [{key: row.get(key) for key in ('id', 'campaign_id', 'status', 'effective_status')} for row in adsets],
        'configured_active_adsets': sum(row.get('status') == 'ACTIVE' for row in adsets) if adsets_readable else None,
        'configured_active_ads': sum(row.get('status') == 'ACTIVE' for row in ads) if ads_readable else None,
        'object_statuses_known': not errors and all(
            row.get('status') in ('ACTIVE', 'PAUSED', 'DELETED', 'ARCHIVED')
            for row in campaigns + adsets + ads
        ),
        'campaigns': [
            {
                'id_ref': 'campaign_sha256_' + hashlib.sha256(str(row.get('id')).encode()).hexdigest()[:12],
                'name': row.get('name'),
                'status': row.get('status'),
                'effective_status': row.get('effective_status'),
                'stop_time': row.get('stop_time'),
                'budget_remaining': row.get('budget_remaining'),
            }
            for row in campaigns
        ],
        'configured_active_campaigns': sum(row.get('status') == 'ACTIVE' for row in campaigns) if campaigns_readable else None,
        'paused_campaigns': sum(row.get('status') == 'PAUSED' for row in campaigns) if campaigns_readable else None,
        'ad_policy': {
            'ads_total': len(ads) if ads_readable else None,
            'issue_count': policy_issue_count,
            'policy_state_sha256': hashlib.sha256(
                '\n'.join(sorted(meta_policy_states)).encode('utf-8')
            ).hexdigest() if ads_readable else None,
            'policy_issue_item_refs': meta_policy_issue_refs,
            'effective_status_counts': {
                status: sum(str(row.get('effective_status') or '').upper() == status for row in ads)
                for status in sorted({str(row.get('effective_status') or '').upper() for row in ads if row.get('effective_status')})
            } if ads_readable else None,
        },
        'today': {
            'spend_usd': spend,
            'clicks': clicks,
            'impressions': impressions,
            'insight_rows': len(insight_rows) if insights_readable else None,
            'normalized_conversions': normalized_conversions,
            'conversion_readback_available': bool(insights_readable and META_APPROVED_SALE_ACTION_TYPE),
            'conversion_definition_status': (
                'PASS_ACCOUNT_NATIVE_CUSTOM_CONVERSION_ID_BOUND'
                if META_APPROVED_SALE_ACTION_TYPE
                else 'NOT_APPLICABLE_UNTIL_SPEND' if spend is None or spend == 0
                else 'FIRST_PARTY_NETWORK_APPROVAL_JOIN_REQUIRED'
            ),
            'conversion_action_type': META_APPROVED_SALE_ACTION_TYPE,
            'conversion_action_values': (
                action_values if insights_readable and META_APPROVED_SALE_ACTION_TYPE else None
            ),
        },
        'http_statuses': {
            'account': account_status, 'campaigns': campaign_status, 'ads': ads_status,
            'insights': insights_status, 'adsets': adset_status,
        },
        'secret_values_printed': False,
    }


def collect_meta() -> dict[str, Any]:
    token = safe_read(META_TOKEN_PATH)
    if not token:
        return {'status': 'BLOCKED', 'reason': 'meta_token_unavailable', 'secret_values_printed': False}
    try:
        requested = collect_meta_account(META_REQUESTED, token)
    except Exception as exc:
        requested = {'status': 'BLOCKED', 'account_id': META_REQUESTED, 'error_class': type(exc).__name__}
    identity_match = requested.get('account_id') == META_MATCHING and requested.get('name') == 'GLP Clinic Scout'
    return {
        'status': 'PASS' if requested['status'] == 'PASS' and identity_match else 'BLOCKED',
        'requested': requested,
        'known_matching_not_mutated': requested,
        'requested_identity_match': identity_match,
        'identity_evidence': 'A2 Cogny GLP account/campaign/adset readback; personal account is not requested scope',
        'secret_values_printed': False,
    }


def collect_microsoft() -> dict[str, Any]:
    result = {
        'status': 'BLOCKED',
        'reason': 'UNKNOWN_MICROSOFT_API_READ_REQUIRED',
        'account_id': MICROSOFT_ACCOUNT,
        'required_artifact': 'scripts/microsoft_ads_api.py runtime plus private developer_token and google_oauth_token.json under ~/.hermes/secrets/microsoft-ads; account 189346863 and completed account-wide campaign performance report',
        'bing_webmaster_is_not_microsoft_advertising': True,
        'spend_readback_available': False,
        'campaign_readback_available': False,
        'configured_enabled_campaigns': None,
        'today_spend_usd': None,
        'today_clicks': None,
        'today_conversions': None,
        'secret_values_printed': False,
    }
    try:
        client = microsoft_client()
        account = client.get_account()['Account']
        if str(account.get('Id')) != MICROSOFT_ACCOUNT:
            raise ValueError('microsoft_account_identity_mismatch')
        campaigns = client.get_campaigns()['Campaigns']
        if not campaigns or len(campaigns) > 20:
            raise ValueError('microsoft_campaign_denominator_unproven')
        if not any(str(row.get('Id')) == MICROSOFT_CAMPAIGN for row in campaigns):
            raise ValueError('microsoft_expected_campaign_missing')
        result.update({
            'campaign_readback_available': True,
            'api_status': 'OK',
            'campaigns': [{key: row.get(key) for key in ('Id', 'Status', 'CampaignType', 'DailyBudget')} for row in campaigns],
            'configured_enabled_campaigns': sum(row.get('Status') == 'Active' for row in campaigns),
            'known_campaign_present': any(str(row.get('Id')) == MICROSOFT_CAMPAIGN for row in campaigns),
        })
        if any(row.get('Status') not in ('Active', 'Paused', 'BudgetPaused', 'BudgetAndManualPaused', 'Deleted', 'Suspended') for row in campaigns):
            result['configured_enabled_campaigns'] = None
            raise ValueError('microsoft_campaign_status_unknown')
        today = datetime.now(ZoneInfo('America/Los_Angeles')).date()
        date = {'Day': today.day, 'Month': today.month, 'Year': today.year}
        receipts = []
        result['report_receipts'] = receipts
        for campaign in campaigns:
            response = client.submit_campaign_performance_report(str(campaign['Id']), date, date)
            request_id = response['ReportRequestId']
            receipt = {'campaign_id': str(campaign['Id']), 'request_id': request_id, 'status': 'Pending', 'metrics': None}
            receipts.append(receipt)
            for attempt in range(4):
                state = client.poll_generate_report(request_id)['ReportRequestStatus']
                receipt['status'] = state['Status']
                if receipt['status'] == 'Success':
                    if 'ReportDownloadUrl' not in state:
                        raise ValueError('microsoft_report_download_field_missing')
                    url = state['ReportDownloadUrl']
                    if url is not None and (not isinstance(url, str) or not url.strip()):
                        raise ValueError('microsoft_report_download_field_invalid')
                receipt['download_url_present'] = bool(state.get('ReportDownloadUrl'))
                if state.get('Status') != 'Pending':
                    break
                if attempt < 3:
                    time.sleep(2)
            if receipt['status'] == 'Success' and state.get('ReportDownloadUrl'):
                blob = client.download_generated_report(state['ReportDownloadUrl'], maximum_bytes=5 * 1024 * 1024)
                receipt['download_sha256'] = hashlib.sha256(blob).hexdigest()
                receipt['metrics'] = microsoft_report_metrics(blob, str(campaign['Id']))
        result['window'] = {'date': str(today), 'timezone': 'America/Los_Angeles', 'partial_day': True}
        result['status'] = 'PASS' if all(row['status'] == 'Success' for row in receipts) else 'UNKNOWN'
        result['api_status'] = 'OK'
        result['data_status'] = 'ROWS' if all(row['metrics'] is not None for row in receipts) else (
            # Microsoft documents Success + nil URL as no data for the parameters.
            # https://learn.microsoft.com/en-us/advertising/reporting-service/reportrequeststatus?view=bingads-13
            'PROVIDER_SUCCESS_NO_DATA' if all(row['status'] == 'Success' and not row['download_url_present'] for row in receipts)
            else 'EMPTY_CSV' if all(row['status'] == 'Success' and row['download_url_present'] and row['metrics'] is None for row in receipts)
            else 'UNKNOWN_PARTIAL_OR_PENDING'
        )
        if not all(row['metrics'] is not None for row in receipts):
            result['status'] = 'UNKNOWN'
            result['reason'] = 'UNKNOWN_MICROSOFT_SPEND_' + result['data_status']
            result['required_artifact'] = 'Explicit numeric CampaignPerformance rows for account 189346863 and the dated report request IDs in report_receipts; provider no-data and empty CSV preserve null spend, never numeric zero'
            return result
        result.update({
            'status': 'PASS', 'reason': None, 'spend_readback_available': True,
            'today_spend_usd': sum(row['metrics']['spend'] for row in receipts),
            'today_clicks': sum(row['metrics']['clicks'] for row in receipts),
            'today_impressions': sum(row['metrics']['impressions'] for row in receipts),
            'conversion_readback_available': False,
            'conversion_status': 'UNKNOWN_REPORT_HAS_NO_APPROVED_SALE_CONVERSION_COLUMN',
        })
    except Exception as exc:
        result.update({'status': 'UNKNOWN' if isinstance(exc, (ValueError, KeyError, TypeError)) else 'BLOCKED',
                       'api_status': result.get('api_status', 'UNKNOWN') if isinstance(exc, (ValueError, KeyError, TypeError)) else 'BLOCKED',
                       'data_status': 'UNKNOWN_READ_FAILED',
                       'reason': 'UNKNOWN_MICROSOFT_READ_FAILED', 'error_class': type(exc).__name__})
    return result


def microsoft_client():
    sys.path.insert(0, str(ROOT / 'scripts'))
    from microsoft_ads_api import MicrosoftAdsClient

    class ReadOnlyCredentialClient(MicrosoftAdsClient):
        def _persist_refreshed_credentials(self, prior_refresh_token):
            # Monitoring refreshes access in memory, never secret files.
            self._token_doc = json.loads(self._creds.to_json())

    return ReadOnlyCredentialClient(timeout=30)


def microsoft_report_metrics(blob: bytes, campaign_id: str) -> dict[str, Any] | None:
    if zipfile.is_zipfile(io.BytesIO(blob)):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith('.csv')]
            if len(names) != 1 or archive.getinfo(names[0]).file_size > 5 * 1024 * 1024:
                raise ValueError('microsoft_report_archive_shape')
            blob = archive.read(names[0])
    reader = csv.DictReader(io.StringIO(blob.decode('utf-8-sig')))
    if not {'AccountId', 'CampaignId', 'Spend', 'Clicks', 'Impressions'}.issubset(reader.fieldnames or []):
        raise ValueError('microsoft_report_header_unknown')
    rows = list(reader)
    if not rows:
        return None
    if any(str(row.get('AccountId')) != MICROSOFT_ACCOUNT or str(row.get('CampaignId')) != campaign_id for row in rows):
        raise ValueError('microsoft_report_identity_mismatch')
    return {
        'spend': sum(float(row['Spend'].replace(',', '')) for row in rows),
        'clicks': sum(int(row['Clicks'].replace(',', '')) for row in rows),
        'impressions': sum(int(row['Impressions'].replace(',', '')) for row in rows),
        'rows': len(rows),
    }


def meta_collection(url: str, *, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    rows = []
    query = dict(params)
    cursors = set()
    for _ in range(10):
        status, payload = http_json(url, params=query)
        if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            return status if status != 200 else 502, {'data': [], 'reason': 'collection_read_unknown'}
        rows.extend(payload['data'])
        paging = payload.get('paging') or {}
        if not paging.get('next'):
            return 200, {'data': rows}
        cursor = (paging.get('cursors') or {}).get('after')
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            break
        cursors.add(cursor)
        query['after'] = cursor
    # Never follow provider-supplied next URLs or claim a partial denominator.
    return 502, {'data': [], 'reason': 'collection_pagination_incomplete'}


def collect_n8n_postbacks() -> dict[str, Any]:
    key = safe_read(N8N_KEY_PATH)
    if not key:
        return {'status': 'BLOCKED', 'reason': 'n8n_native_api_key_unavailable'}
    rows = {}
    for label, workflow_id in POSTBACK_WORKFLOWS.items():
        status, payload = http_json(
            f'{N8N_BASE}/workflows/{workflow_id}',
            headers={'X-N8N-API-KEY': key},
        )
        rows[label] = {
            'http_status': status,
            'name': payload.get('name') if isinstance(payload, dict) else None,
            'active': bool(payload.get('active')) if isinstance(payload, dict) else False,
            'active_version_id_present': bool(payload.get('activeVersionId')) if isinstance(payload, dict) else False,
        }
    passed = all(row['http_status'] == 200 and row['active'] for row in rows.values())
    return {'status': 'PASS' if passed else 'BLOCKED', 'workflows': rows, 'secret_values_printed': False}


def collect_go_runtime() -> dict[str, Any]:
    if not DEPLOY_CLOSEOUT.is_file():
        return {'status': 'UNKNOWN_CURRENT_RELEASE_MONETIZATION_RECEIPT_REQUIRED',
                'label': 'awaiting release train 1', 'reason': 'deploy_closeout_missing'}
    payload = json.loads(DEPLOY_CLOSEOUT.read_text(encoding='utf-8'))
    go = payload.get('go_runtime', {})
    result = {
        'status': go.get('monetized_runtime_activation_status', 'BLOCKED'),
        'instrumentation_status': go.get('instrumentation_status'),
        'program_count': go.get('program_count'),
        'candidate_preview_only': go.get('candidate_preview_only'),
        'live_link_allowed_false': go.get('live_link_allowed_false'),
        'promotion_authorized_false': go.get('promotion_authorized_false'),
        'cs_click_id_minting_present': go.get('analytics_mints_cs_click_id_at_runtime'),
        'tracking_links_requested': 0,
    }
    result['evidence_age_status'] = 'HISTORICAL_NOT_CURRENT_RELEASE_PROOF'
    result['required_artifact'] = 'D2 current deployment-bound monetized route/owned click receipt plus PLAN/D1/CLICK-PARITY.md; collector repair is not route activation'
    result['click_parity_artifact_present'] = CLICK_PARITY.is_file()
    if CURRENT_CONTENT_BASE.is_file():
        current = json.loads(CURRENT_CONTENT_BASE.read_text())
        result['current_content_base_sha256'] = hashlib.sha256(CURRENT_CONTENT_BASE.read_bytes()).hexdigest()
        result['current_content_base'] = {key: current.get(key) for key in ('deployment_id', 'release_root', 'public_dir')}
    result['status'] = 'UNKNOWN_CURRENT_RELEASE_MONETIZATION_RECEIPT_REQUIRED'
    result['label'] = 'awaiting release train 1'
    return result


def posthog_client():
    sys.path.insert(0, str(ROOT / 'ops/glp'))
    from seo_data_rails import PostHog
    return PostHog()


def d1_client():
    sys.path.insert(0, str(ROOT / 'scripts'))
    from glp_network_revenue_import import _remote_client
    return _remote_client()


def collect_posthog() -> dict[str, Any]:
    start_utc = datetime.now(ZoneInfo('America/Los_Angeles')).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')
    aggregate_query = f"""
        SELECT
          count() AS events,
          countIf(notEmpty(toString(properties.cs_click_id))) AS with_cs_click_id,
          countIf(notEmpty(toString(properties.gclid))) AS with_gclid,
          countIf(notEmpty(toString(properties.msclkid))) AS with_msclkid,
          countIf(notEmpty(toString(properties.fbclid))) AS with_fbclid,
          max(timestamp) AS latest_event_utc
        FROM events
        WHERE event = 'affiliate_click'
          AND domain(properties.$current_url) = 'glpclinicscout.com'
          AND ifNull(toString(properties.is_test), '') NOT IN ('true', '1')
          AND timestamp >= toDateTime('{start_utc}')
    """
    identity_query = f"""
        SELECT DISTINCT
          toString(properties.cs_click_id) AS cs_click_id,
          toString(properties.gclid) AS gclid,
          toString(properties.msclkid) AS msclkid,
          toString(properties.fbclid) AS fbclid
        FROM events
        WHERE event = 'affiliate_click'
          AND domain(properties.$current_url) = 'glpclinicscout.com'
          AND timestamp >= toDateTime('{start_utc}')
          AND notEmpty(toString(properties.cs_click_id))
        LIMIT 10000
    """
    try:
        posthog = posthog_client()
        aggregate = posthog.hogql(aggregate_query)
        identities = posthog.hogql(identity_query)
        if not aggregate.get('ok') or not identities.get('ok'):
            raise RuntimeError('posthog_hogql_failed')
        if not aggregate.get('rows'):
            raise ValueError('posthog_aggregate_rows_missing')
        row = aggregate['rows'][0]
        identity_rows = [item for item in (identities.get('rows') or []) if item and str(item[0]).strip()]
        if len(identities.get('rows') or []) >= 10000:
            raise ValueError('posthog_identity_denominator_truncated')
        click_ids = {str(item[0]).strip() for item in identity_rows}
        sink_rows = d1_client().query(
            "SELECT click_token AS cs_click_id FROM affiliate_click_map "
            "WHERE analytics_consent = 'granted' "
            f"AND created_at >= '{start_utc.replace(' ', 'T')}.000Z' LIMIT 10001"
        )
        if len(sink_rows) >= 10001:
            raise ValueError('d1_sink_denominator_truncated')
        sink_ids = {
            str(item.get('cs_click_id') or '').strip() for item in sink_rows
            if str(item.get('cs_click_id') or '').strip()
        }
        joined = click_ids & sink_ids
        joined_by_platform = {
            'google': len({
                str(item[0]).strip() for item in identity_rows
                if str(item[0]).strip() in joined and len(item) > 1 and str(item[1] or '').strip()
            }),
            'microsoft': len({
                str(item[0]).strip() for item in identity_rows
                if str(item[0]).strip() in joined and len(item) > 2 and str(item[2] or '').strip()
            }),
            'meta': len({
                str(item[0]).strip() for item in identity_rows
                if str(item[0]).strip() in joined and len(item) > 3 and str(item[3] or '').strip()
            }),
        }
        events = int(row[0] or 0)
        with_cs_click_id = int(row[1] or 0)
        canary_status = (
            'MEASURED_ZERO_IDLE' if events == 0
            else 'DARK' if with_cs_click_id == 0
            else 'PASS'
        )
        return {
            'status': 'PASS',
            'source': 'PostHog project 508172 plus read-only affiliate_click_map runtime sink; never revenue-ledger membership',
            'host_filter': 'glpclinicscout.com',
            'window': 'today America/Los_Angeles',
            'window_start_utc': start_utc,
            'affiliate_click_events_today': events,
            'events_with_cs_click_id': with_cs_click_id,
            'events_missing_cs_click_id': max(events - with_cs_click_id, 0),
            'platform_click_id_counts': {
                'google_gclid': int(row[2] or 0),
                'microsoft_msclkid': int(row[3] or 0),
                'meta_fbclid': int(row[4] or 0),
            },
            'latest_event_utc': None if str(row[5] or '').startswith('1970-01-01') else row[5],
            'cs_click_id_live_query_available': True,
            'distinct_cs_click_ids_today': len(click_ids),
            'joined_clicks_today': len(joined),
            'joined_clicks_by_platform': joined_by_platform,
            'd1_runtime_rows_with_identity': len(sink_ids),
            'join_definition': 'Observed affiliate_click IDs intersect consent-granted owned runtime click_token IDs in the same PT day; not sales or proven REAL traffic',
            'real_non_test_classification': 'UNKNOWN_EVENT_AND_RUNTIME_TEST_PROVENANCE_REQUIRED',
            'approved_sale_count': None,
            'click_identity_set_sha256': hashlib.sha256(
                '\n'.join(sorted(click_ids)).encode('utf-8')
            ).hexdigest(),
            'raw_identifiers_serialized': False,
            'canary_status': canary_status,
            'secrets_printed': False,
        }
    except Exception as exc:
        return {
            'status': 'BLOCKED',
            'reason': 'posthog_or_d1_read_only_join_unavailable',
            'error_class': exc.__class__.__name__,
            'cs_click_id_live_query_available': False,
            'joined_clicks_today': None,
            'joined_clicks_by_platform': {'google': None, 'microsoft': None, 'meta': None},
            'platform_click_id_counts': {
                'google_gclid': None,
                'microsoft_msclkid': None,
                'meta_fbclid': None,
            },
            'canary_status': 'UNKNOWN_SOURCE_UNAVAILABLE',
            'raw_identifiers_serialized': False,
            'secrets_printed': False,
        }


def _postback_stdout(receipt: dict[str, Any]) -> str:
    flag = receipt.get('stdout_truncated')
    expected = receipt.get('stdout_bytes')
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        raise ValueError('stdout_size_unknown')
    if flag is True:
        path = Path(str(receipt.get('stdout_file') or ''))
        job_id = receipt.get('job_id')
        if (not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{16}', job_id)
                or path.resolve().parent != BRIDGE_RECEIPTS.resolve()
                or not path.name.endswith('-' + job_id + '.stdout') or path.is_symlink()):
            raise ValueError('stdout_artifact_unbound')
        with path.open('rb') as handle:
            raw = handle.read(8_000_001)
        if len(raw) > 8_000_000 or len(raw) != expected:
            raise ValueError('stdout_artifact_incomplete')
        return raw.decode('utf-8')
    if flag is not False or not isinstance(receipt.get('stdout_tail'), str):
        raise ValueError('stdout_completeness_unknown')
    text = receipt['stdout_tail']
    if len(text.encode('utf-8')) != expected:
        raise ValueError('stdout_tail_incomplete')
    return text


def collect_postback_receipts() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    counts = {'scanned': 0, 'unreadable': 0, 'rejected': 0, 'receiver_failed': 0, 'unrelated': 0}
    available = BRIDGE_RECEIPTS.is_dir()
    try:
        paths = list(BRIDGE_RECEIPTS.glob('*.json')) if available else []
    except OSError:
        paths, available = [], False
    for path in paths:
        counts['scanned'] += 1
        try:
            with path.open('rb') as handle:
                raw = handle.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError('bridge_receipt_over_budget')
            receipt = json.loads(raw)
            if not isinstance(receipt, dict):
                raise ValueError('bridge_receipt_schema')
        except (OSError, ValueError):
            counts['unreadable'] += 1
            continue
        if receipt.get('name') != 'affiliate_postback_receive':
            counts['unrelated'] += 1
            continue
        if receipt.get('rc') != 0:
            counts['receiver_failed'] += 1
            continue
        try:
            # Last terminal JSON line is parsed only after the complete output is bound.
            lines = _postback_stdout(receipt).strip().splitlines()
            payload = json.loads(lines[-1])
            if (not isinstance(payload, dict) or any(not isinstance(payload.get(k), str) or not payload[k]
                    for k in ('event_status', 'event_type', 'status', 'network'))):
                raise ValueError('receiver_receipt_schema')
            if payload['status'].startswith(('FAIL', 'BLOCK', 'UNKNOWN', 'PARTIAL')):
                raise ValueError('receiver_did_not_accept')
        except (OSError, ValueError, IndexError):
            counts['rejected'] += 1
            continue
        rows.append({k: receipt.get(k) if k == 'finished_utc' else payload.get(k)
                     for k in ('finished_utc', 'event_status', 'event_type', 'status', 'network', 'replay_safe_effects', 'is_test')})
    tests = [row for row in rows if row['event_status'] == 'test' or row['network'] == '__receiver_selftest__' or row['is_test'] is True]
    real = [row for row in rows if row not in tests and row['is_test'] is False]
    unclassified = len(rows) - len(tests) - len(real)
    incomplete = counts['unreadable'] + counts['rejected'] + counts['receiver_failed']
    return {
        'status': 'UNKNOWN_RECEIPT_SOURCE' if not available else 'PARTIAL_RECEIPT_SCAN' if incomplete else 'PASS_READ_ONLY_RECEIPT_SCAN',
        'source': str(BRIDGE_RECEIPTS), 'coverage': counts,
        'receiver_receipts_total': len(rows), 'selftest_receipts': len(tests),
        'unclassified_receipts': unclassified,
        'real_postback_receipts': None if unclassified else len(real),
        'real_classification_status': 'UNKNOWN_MISSING_IS_TEST_PROVENANCE' if unclassified else 'EXPLICIT_FLAGS_OR_RECEIVER_SELFTEST',
        'latest_real_finished_utc': max((str(row.get('finished_utc') or '') for row in real), default=None),
        'tracking_links_opened': 0, 'secret_values_printed': False,
    }


def build_stop_loss(report: dict[str, Any]) -> dict[str, Any]:
    google = report['platforms']['google_ads']
    meta = report['platforms']['meta_ads'].get('requested', {})
    microsoft = report['platforms']['microsoft_advertising']
    posthog = report['revenue_chain']['posthog']
    platform_rows = {
        'google': {
            'spend_usd': google.get('today_spend_usd'),
            'joined_clicks': (posthog.get('joined_clicks_by_platform') or {}).get('google'),
        },
        'meta': {
            'spend_usd': meta.get('today', {}).get('spend_usd'),
            'joined_clicks': (posthog.get('joined_clicks_by_platform') or {}).get('meta'),
        },
        'microsoft': {
            'spend_usd': microsoft.get('today_spend_usd'),
            'joined_clicks': (posthog.get('joined_clicks_by_platform') or {}).get('microsoft'),
        },
    }
    known_spend = [row['spend_usd'] for row in platform_rows.values()
                   if isinstance(row['spend_usd'], (int, float))]
    spend_usd_known_total = float(sum(known_spend)) if known_spend else None
    joined_clicks = posthog.get('joined_clicks_today')
    google_ads_policy = (google.get('inventory') or {}).get('ads') or {}
    google_policy_readable = (
        google.get('status') == 'PASS'
        and google_ads_policy.get('policy_readback_available') is True
    )
    google_disapproved = (
        int((google_ads_policy.get('by_approval_status') or {}).get('DISAPPROVED', 0))
        if google_policy_readable else None
    )
    google_policy_issue_refs = (
        google_ads_policy.get('policy_issue_item_refs') if google_policy_readable else None
    )
    google_policy_state_sha256 = (
        google_ads_policy.get('policy_state_sha256') if google_policy_readable else None
    )
    meta_policy = meta.get('ad_policy') or {}
    meta_policy_readable = meta.get('ad_policy_readback_available') is True
    meta_policy_issues = meta_policy.get('issue_count') if meta_policy_readable else None
    meta_policy_issue_refs = meta_policy.get('policy_issue_item_refs') if meta_policy_readable else None
    meta_policy_state_sha256 = meta_policy.get('policy_state_sha256') if meta_policy_readable else None

    previous_stop_loss = report.get('previous_stop_loss') or {}
    previous_disapproved = previous_stop_loss.get('google_disapproved_ads_baseline')
    previous_meta_policy_issues = previous_stop_loss.get('meta_policy_issue_ads_baseline')
    previous_google_issue_refs = previous_stop_loss.get('google_policy_issue_refs_baseline')
    previous_meta_issue_refs = previous_stop_loss.get('meta_policy_issue_refs_baseline')
    previous_google_policy_state_sha256 = previous_stop_loss.get(
        'google_policy_state_sha256_baseline'
    )
    previous_meta_policy_state_sha256 = previous_stop_loss.get(
        'meta_policy_state_sha256_baseline'
    )
    if LATEST.is_file() and not (
        isinstance(previous_disapproved, int)
        or isinstance(previous_meta_policy_issues, int)
        or isinstance(previous_google_issue_refs, list)
        or isinstance(previous_meta_issue_refs, list)
    ):
        try:
            previous = json.loads(LATEST.read_text(encoding='utf-8'))
            prior_stop_loss = previous.get('stop_loss') or {}
            previous_disapproved = prior_stop_loss.get('google_disapproved_ads_baseline')
            if not isinstance(previous_disapproved, int):
                previous_disapproved = prior_stop_loss.get('google_disapproved_ads')
            previous_meta_policy_issues = prior_stop_loss.get('meta_policy_issue_ads_baseline')
            if not isinstance(previous_meta_policy_issues, int):
                previous_meta_policy_issues = prior_stop_loss.get('meta_policy_issue_ads')
            previous_google_issue_refs = prior_stop_loss.get('google_policy_issue_refs_baseline')
            previous_meta_issue_refs = prior_stop_loss.get('meta_policy_issue_refs_baseline')
            previous_google_policy_state_sha256 = prior_stop_loss.get(
                'google_policy_state_sha256_baseline'
            )
            previous_meta_policy_state_sha256 = prior_stop_loss.get(
                'meta_policy_state_sha256_baseline'
            )
        except Exception:
            previous_disapproved = None
            previous_meta_policy_issues = None
            previous_google_issue_refs = None
            previous_meta_issue_refs = None
            previous_google_policy_state_sha256 = None
            previous_meta_policy_state_sha256 = None

    google_new_policy_refs = (
        sorted(set(google_policy_issue_refs) - set(previous_google_issue_refs))
        if isinstance(google_policy_issue_refs, list) and isinstance(previous_google_issue_refs, list)
        else []
    )
    meta_new_policy_refs = (
        sorted(set(meta_policy_issue_refs) - set(previous_meta_issue_refs))
        if isinstance(meta_policy_issue_refs, list) and isinstance(previous_meta_issue_refs, list)
        else []
    )
    policy_state_changes = [
        platform
        for platform, current, previous in (
            ('google', google_policy_state_sha256, previous_google_policy_state_sha256),
            ('meta', meta_policy_state_sha256, previous_meta_policy_state_sha256),
        )
        if isinstance(current, str) and isinstance(previous, str) and current != previous
    ]
    google_account_status = (google.get('identity') or {}).get('status')
    meta_account_status = meta.get('account_status')
    meta_disable_reason = meta.get('disable_reason')
    account_restrictions = []
    if google_account_status not in (None, 'ENABLED'):
        account_restrictions.append('google')
    if meta_account_status not in (None, 1, '1', 'ACTIVE') or meta_disable_reason not in (None, 0, '0', '', 'NONE'):
        account_restrictions.append('meta')
    spend_without_join = [
        platform for platform, row in platform_rows.items()
        if isinstance(row['spend_usd'], (int, float)) and row['spend_usd'] > 0
        and row['joined_clicks'] in (None, 0)
    ]
    join_source_blockers = [
        platform for platform, row in platform_rows.items()
        if not isinstance(row['joined_clicks'], (int, float))
    ]
    rules = [
        {
            'code': 'JOIN_SOURCE_UNAVAILABLE',
            'armed': True,
            'triggered': bool(join_source_blockers),
            'triggered_platforms': join_source_blockers,
            'evaluation': 'PARTIAL_OR_TOTAL_JOIN_SOURCE_UNAVAILABLE',
        },
        {
            'code': 'SPEND_WITH_ZERO_JOINED_CLICKS',
            'armed': True,
            'triggered': bool(spend_without_join),
            'triggered_platforms': spend_without_join,
            'evaluation': 'UNKNOWN_JOIN_SOURCE' if (
                posthog.get('status') != 'PASS'
                or any(row['joined_clicks'] is None for row in platform_rows.values())
            ) else 'EVALUATED_PER_PLATFORM',
        },
        {
            'code': 'DISAPPROVAL_APPEARED',
            'armed': True,
            'triggered': bool(google_new_policy_refs or meta_new_policy_refs or policy_state_changes),
            'evaluation': (
                'SOURCE_UNAVAILABLE' if not google_policy_readable or not meta_policy_readable
                else 'COMPARED_TO_PREVIOUS_READBACK' if (
                    isinstance(previous_google_issue_refs, list)
                    and isinstance(previous_meta_issue_refs, list)
                ) else 'BASELINE_ESTABLISHED'
            ),
            'google': {'baseline_count': previous_disapproved, 'current_count': google_disapproved},
            'meta': {'baseline_count': previous_meta_policy_issues, 'current_count': meta_policy_issues},
            'new_issue_item_refs': {
                'google': google_new_policy_refs,
                'meta': meta_new_policy_refs,
            },
            'policy_state_sha256_changed_platforms': policy_state_changes,
            'policy_state_sha256': {
                'google': {
                    'baseline': previous_google_policy_state_sha256,
                    'current': google_policy_state_sha256,
                },
                'meta': {
                    'baseline': previous_meta_policy_state_sha256,
                    'current': meta_policy_state_sha256,
                },
            },
        },
        {
            'code': 'ACCOUNT_RESTRICTION_APPEARED',
            'armed': True,
            'triggered': bool(account_restrictions),
            'triggered_platforms': account_restrictions,
            'evaluation': 'CURRENT_ACCOUNT_STATUS_AND_DISABLE_REASON',
        },
        {
            'code': 'CS_CLICK_ID_CANARY_DARK',
            'armed': True,
            'triggered': posthog.get('canary_status') == 'DARK',
            'evaluation': posthog.get('canary_status'),
        },
    ]
    return {
        'status': 'ARMED_WITH_SOURCE_BLOCKERS' if (
            joined_clicks is None
            or join_source_blockers
            or any(not isinstance(row['spend_usd'], (int, float)) for row in platform_rows.values())
        ) else 'ARMED',
        'spend_usd_known_total': spend_usd_known_total,
        'spend_readback_complete_all_platforms': all(
            isinstance(row['spend_usd'], (int, float)) for row in platform_rows.values()
        ),
        'platform_breakdown': platform_rows,
        'joined_clicks_today': joined_clicks,
        'google_disapproved_ads': google_disapproved,
        'google_disapproved_ads_baseline': (
            google_disapproved if isinstance(google_disapproved, int) else previous_disapproved
        ),
        'google_policy_issue_refs_baseline': (
            google_policy_issue_refs
            if isinstance(google_policy_issue_refs, list) else previous_google_issue_refs
        ),
        'google_policy_state_sha256_baseline': (
            google_policy_state_sha256
            if isinstance(google_policy_state_sha256, str)
            else previous_google_policy_state_sha256
        ),
        'meta_policy_issue_ads': meta_policy_issues,
        'meta_policy_issue_ads_baseline': (
            meta_policy_issues if isinstance(meta_policy_issues, int) else previous_meta_policy_issues
        ),
        'meta_policy_issue_refs_baseline': (
            meta_policy_issue_refs
            if isinstance(meta_policy_issue_refs, list) else previous_meta_issue_refs
        ),
        'meta_policy_state_sha256_baseline': (
            meta_policy_state_sha256
            if isinstance(meta_policy_state_sha256, str)
            else previous_meta_policy_state_sha256
        ),
        'rules': rules,
        'budget_or_bid_mutations_allowed': False,
    }


def paused_state(platform: str, row: dict[str, Any]) -> bool:
    """Require explicit inactive object state; an ENABLED account is not an ad."""
    if platform == 'microsoft':
        # Campaign pause observation is independent of numeric spend observation.
        if (row.get('campaign_readback_available') is not True
                or row.get('account_id') != MICROSOFT_ACCOUNT
                or row.get('known_campaign_present') is not True):
            return False
    elif row.get('status') != 'PASS':
        return False
    if platform == 'meta':
        return (
            row.get('object_statuses_known') is True
            and all(row.get(key) == 0 for key in (
                'configured_active_campaigns', 'configured_active_adsets'))
            and not row.get('ad_policy', {}).get('effective_status_counts', {}).get('ACTIVE', 0)
            and not any(c.get('effective_status') == 'ACTIVE' for c in row.get('campaigns', []) + row.get('adsets', []))
        )
    campaigns = row.get('campaigns')
    if not isinstance(campaigns, list) or not campaigns:
        return False
    key = 'Status' if platform == 'microsoft' else 'status'
    inactive = ('Paused', 'BudgetAndManualPaused', 'Deleted') if platform == 'microsoft' else ('PAUSED', 'REMOVED')
    if row.get('configured_enabled_campaigns') != 0 or not all(c.get(key) in inactive for c in campaigns):
        return False
    return True


def enabled_objects(platform: str, row: dict[str, Any]) -> bool:
    if platform == 'meta':
        return (
            any((row.get(key) or 0) > 0 for key in ('configured_active_campaigns', 'configured_active_adsets'))
            or (row.get('ad_policy', {}).get('effective_status_counts', {}).get('ACTIVE') or 0) > 0
            or any(c.get('status') == 'ACTIVE' or c.get('effective_status') == 'ACTIVE'
                   for c in row.get('campaigns', []) + row.get('adsets', []))
        )
    return (row.get('configured_enabled_campaigns') or 0) > 0 or any(
        c.get('Status' if platform == 'microsoft' else 'status') in ('Active', 'ENABLED')
        for c in row.get('campaigns', [])
    )


def expected_empty_spend(platform: str, row: dict[str, Any]) -> bool:
    if not paused_state(platform, row):
        return False
    if platform == 'google':
        return row.get('today', {}).get('report_status') == 'SUCCESS' and row.get('today', {}).get('rows') == 0
    if platform == 'meta':
        return row.get('http_statuses', {}).get('insights') == 200 and row.get('today', {}).get('insight_rows') == 0
    # A successful Microsoft report without numeric rows is still unknown spend,
    # including documented nil-download no-data and a downloaded empty CSV.
    return False


def spend_label(platform: str, row: dict[str, Any]) -> str:
    if expected_empty_spend(platform, row):
        return 'PAUSED · no spend rows (expected)'
    value = row.get('today', {}).get('spend_usd') if platform == 'meta' else row.get('today_spend_usd')
    return 'UNKNOWN · spend unreadable' if value is None else f'${value:.2f}'


def microsoft_api_label(row: dict[str, Any]) -> str:
    if expected_empty_spend('microsoft', row):
        return 'API OK · paused · no rows'
    if row.get('status') == 'BLOCKED':
        return 'BLOCKED · auth/credential/transport failure'
    return 'API OK · ' + str(row.get('data_status', 'UNKNOWN')) if row.get('api_status') == 'OK' else str(row.get('status', 'UNKNOWN'))


def build_alerts(report: dict[str, Any]) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    google = report['platforms']['google_ads']
    meta = report['platforms']['meta_ads']
    microsoft = report['platforms']['microsoft_advertising']
    go = report['revenue_chain']['go_runtime']
    posthog = report['revenue_chain']['posthog']
    if google.get('status') != 'PASS':
        alerts.append({'severity': 'P1', 'code': 'GOOGLE_STATE_UNREADABLE', 'detail': google.get('error_class', '')})
    if isinstance(google.get('today_spend_usd'), (int, float)) and google['today_spend_usd'] > 0:
        alerts.append({'severity': 'P0', 'code': 'GOOGLE_SPEND_NONZERO', 'detail': str(google['today_spend_usd'])})
    requested = meta.get('requested', {})
    if requested.get('status') != 'PASS':
        alerts.append({'severity': 'P1', 'code': 'META_REQUESTED_STATE_UNREADABLE', 'detail': META_REQUESTED})
    requested_spend = requested.get('today', {}).get('spend_usd')
    if isinstance(requested_spend, (int, float)) and requested_spend > 0:
        alerts.append({'severity': 'P0', 'code': 'META_SPEND_NONZERO', 'detail': str(requested['today']['spend_usd'])})
    if isinstance(requested_spend, (int, float)) and requested_spend > 0 and (
        approved_sale_definition().get('definition_binding_status') != 'BOUND_FIRST_PARTY_NETWORK_APPROVAL_JOIN'
    ):
        alerts.append({
            'severity': 'P1',
            'code': 'META_APPROVED_SALE_CONVERSION_DEFINITION_UNBOUND',
            'detail': 'first_party_network_approval_join_definition_unbound; no Pixel/CAPI conversion required',
        })
    if isinstance(google.get('configured_enabled_campaigns'), int) and google['configured_enabled_campaigns'] > 0:
        alerts.append({'severity': 'P1', 'code': 'GOOGLE_CAMPAIGN_ENABLED', 'detail': str(google['configured_enabled_campaigns'])})
    if isinstance(requested.get('configured_active_adsets'), int) and requested['configured_active_adsets'] > 0:
        alerts.append({'severity': 'P1', 'code': 'REQUESTED_META_ADSET_CONFIGURED_ACTIVE', 'detail': str(requested['configured_active_adsets'])})
    if isinstance(requested.get('configured_active_campaigns'), int) and requested['configured_active_campaigns'] > 0:
        alerts.append({'severity': 'P1', 'code': 'REQUESTED_META_CAMPAIGN_CONFIGURED_ACTIVE', 'detail': str(requested['configured_active_campaigns'])})
    if (requested.get('ad_policy', {}).get('effective_status_counts', {}).get('ACTIVE') or 0) > 0:
        alerts.append({'severity': 'P1', 'code': 'REQUESTED_META_AD_EFFECTIVE_ACTIVE', 'detail': 'effective_active'})
    if any(c.get('effective_status') == 'ACTIVE' for c in requested.get('campaigns', []) + requested.get('adsets', [])):
        alerts.append({'severity': 'P1', 'code': 'REQUESTED_META_OBJECT_EFFECTIVE_ACTIVE', 'detail': 'campaign_or_adset'})

    if microsoft.get('status') == 'BLOCKED':
        alerts.append({'severity': 'P1', 'code': 'MICROSOFT_ADS_UNMONITORED', 'detail': microsoft.get('reason', '')})
    elif microsoft.get('status') != 'PASS':
        alerts.append({'severity': 'P1', 'code': 'MICROSOFT_REPORT_STATE_UNREADABLE',
                       'detail': microsoft.get('reason', 'UNKNOWN'), 'disposition': 'UNKNOWN'})
    if isinstance(microsoft.get('today_spend_usd'), (int, float)) and microsoft['today_spend_usd'] > 0:
        alerts.append({
            'severity': 'P0',
            'code': 'MICROSOFT_SPEND_NONZERO',
            'detail': str(microsoft['today_spend_usd']),
        })
    if (
        isinstance(microsoft.get('configured_enabled_campaigns'), int)
        and microsoft['configured_enabled_campaigns'] > 0
    ):
        alerts.append({
            'severity': 'P1',
            'code': 'MICROSOFT_CAMPAIGN_ENABLED',
            'detail': str(microsoft['configured_enabled_campaigns']),
        })
    if go.get('status') != 'PASS':
        alerts.append({'severity': 'INFO', 'code': 'GO_RUNTIME_MONETIZATION_BLOCKED',
                       'detail': 'awaiting release train 1', 'label': 'awaiting release train 1'})
    if posthog.get('status') != 'PASS':
        alerts.append({'severity': 'P1', 'code': 'POSTHOG_CS_CLICK_ID_QUERY_BLOCKED', 'detail': posthog.get('reason', '')})
    if not meta.get('requested_identity_match', False):
        alerts.append({'severity': 'P1', 'code': 'META_ACCOUNT_IDENTITY_MISMATCH', 'detail': META_REQUESTED})
    for rule in report.get('stop_loss', {}).get('rules', []):
        if rule.get('triggered'):
            alerts.append({'severity': 'P1', 'code': str(rule['code']), 'detail': str(rule.get('evaluation'))})
    for platform, row, value in (
        ('google', google, google.get('today_spend_usd')),
        ('meta', requested, requested.get('today', {}).get('spend_usd')),
        ('microsoft', microsoft, microsoft.get('today_spend_usd')),
    ):
        if not isinstance(value, (int, float)) and not expected_empty_spend(platform, row):
            if enabled_objects(platform, row):
                alerts.append({'severity': 'P1', 'code': 'SPEND_SOURCE_UNAVAILABLE', 'detail': platform})
            elif row.get('status') == 'PASS':
                alerts.append({'severity': 'P1', 'code': 'SPEND_REPORT_STATE_UNREADABLE',
                               'detail': platform, 'disposition': 'UNKNOWN',
                               'required_artifact': 'Successful report and explicit complete paused object readback; no numeric zero inferred'})
    artifacts = {
        'META_REQUESTED_STATE_UNREADABLE': 'Cogny/Graph account act_243816027637970 plus complete campaigns, adsets, ads and insights readback; private Meta system-user token access',
        'META_ACCOUNT_IDENTITY_MISMATCH': 'Fresh account act_243816027637970 identity named GLP Clinic Scout; never substitute personal act_126299047720199',
        'META_APPROVED_SALE_CONVERSION_DEFINITION_UNBOUND': str(MEASUREMENT_CONFIG) + ': approved_sale.required_artifact; no Pixel/CAPI export authorized',
        'MICROSOFT_ADS_UNMONITORED': microsoft.get('required_artifact', 'MicrosoftAdsClient authenticated account-wide campaign performance readback'),
        'GO_RUNTIME_MONETIZATION_BLOCKED': go.get('required_artifact', str(CLICK_PARITY) + ' and D2 deployment-bound current route receipt'),
        'POSTHOG_CS_CLICK_ID_QUERY_BLOCKED': 'PostHog project 508172 read permission and D1 click/revenue schema readback',
        'SPEND_SOURCE_UNAVAILABLE': 'Successful current-day report plus complete paused object readback, or explicit numeric performance rows; unknown spend is never zero',
        'JOIN_SOURCE_UNAVAILABLE': 'PostHog project 508172 exact cohort and SELECT-only affiliate_click_map schema/readback with created_at, click_token and analytics_consent',
    }
    for alert in alerts:
        if alert['code'] in artifacts:
            alert.update({'disposition': 'UNKNOWN', 'required_artifact': artifacts[alert['code']]})
    return alerts


def approved_sale_definition() -> dict[str, Any]:
    try:
        config = json.loads(MEASUREMENT_CONFIG.read_text())
        if config.get('schema') != 'glpcs.ads-monitor-measurement.v1' or config.get('meta_account_id') != META_REQUESTED:
            raise ValueError('measurement_config_identity')
        sale = config['approved_sale']
        if sale.get('meta_export_allowed') is not False or sale.get('consumer_health_export_allowed') is not False:
            raise ValueError('measurement_export_hold_required')
        if (sale.get('definition_binding_status') != 'BOUND_FIRST_PARTY_NETWORK_APPROVAL_JOIN'
                or sale.get('click_event') != 'affiliate_click'
                or sale.get('measurement_method') != 'POSTHOG_AFFILIATE_CLICK_TO_NETWORK_APPROVAL_JOIN'
                or sale.get('meta_custom_conversion_id') is not None):
            raise ValueError('first_party_approval_definition_required')
        return {**sale, 'config_sha256': hashlib.sha256(MEASUREMENT_CONFIG.read_bytes()).hexdigest()}
    except Exception as exc:
        return {'join_contract_status': 'UNKNOWN_CONFIG_UNAVAILABLE', 'error_class': type(exc).__name__, 'required_artifact': str(MEASUREMENT_CONFIG)}


def discord_token() -> str | None:
    key_name = 'DISCORD' + '_BOT' + '_TOKEN'
    raw = safe_read(HERMES_ENV)
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith(key_name + '='):
            return line.split('=', 1)[1].strip()
    return None


def _post_discord_once(report: dict[str, Any], *, force_digest: bool) -> dict[str, Any]:
    if not report['alerts'] and not force_digest:
        return {'status': 'SILENT_HEALTHY', 'sent': False}
    token = discord_token()
    if not token:
        return {'status': 'BLOCKED', 'sent': False, 'reason': 'discord_token_unavailable'}
    google = report['platforms']['google_ads']
    meta = report['platforms']['meta_ads'].get('requested', {})
    lines = [
        '**GLP ads monitor — spend remains held**',
        f"Verdict: `{report['status']}` · alerts `{len(report['alerts'])}`",
        f"Google: enabled `{google.get('configured_enabled_campaigns', 'unknown')}` · today `{spend_label('google', google)}`",
        f"Meta requested: active `{meta.get('configured_active_campaigns', 'unknown')}` · today `{spend_label('meta', meta)}` · identity match `{report['platforms']['meta_ads'].get('requested_identity_match')}`",
        f"Microsoft Ads: `{microsoft_api_label(report['platforms']['microsoft_advertising'])}`",
        'Approved-sale measurement: first-party affiliate_click → network approval join · actual join UNKNOWN · ' + (
            'not applicable until spend' if meta.get('today', {}).get('spend_usd') in (None, 0)
            else 'network approval evidence required'
        ),
        f"/go monetization: `{report['revenue_chain']['go_runtime']['status']}` · awaiting release train 1 · PostHog query: `{report['revenue_chain']['posthog']['status']}`",
    ]
    for alert in report['alerts'][:8]:
        disposition = f" / {alert['disposition']}" if alert.get('disposition') else ''
        lines.append(f"• `{alert['severity']}{disposition}` `{alert['code']}`")
    if len(report['alerts']) > 8:
        lines.append(f"+ {len(report['alerts']) - 8} additional alerts in the evidence report")
    lines.append(f"Evidence and exact missing artifacts: {LATEST}")
    if len('\n'.join(lines)) > 2000:
        return {'status': 'BLOCKED_CARD_LENGTH', 'sent': False, 'receipt': str(LATEST)}
    body = json.dumps({'content': '\n'.join(lines), 'allowed_mentions': {'parse': []}}).encode('utf-8')
    request = urllib.request.Request(
        f'https://discord.com/api/v10/channels/{DISCORD_OPS}/messages',
        data=body,
        headers={
            'Authorization': f'Bot {token}',
            'Content-Type': 'application/json',
            'User-Agent': DISCORD_UA,
        },
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = json.loads(response.read().decode('utf-8'))
    readback_status, readback = http_json(
        f'https://discord.com/api/v10/channels/{DISCORD_OPS}/messages/{payload["id"]}',
        headers={'Authorization': f'Bot {token}', 'User-Agent': DISCORD_UA},
    )
    return {
        'status': 'POSTED',
        'sent': True,
        'message_id': payload['id'],
        'readback_http_status': readback_status,
        'readback_verified': readback_status == 200 and readback.get('content') == '\n'.join(lines),
    }


def notice_fingerprint(report: dict[str, Any]) -> str:
    platforms = report['platforms']
    requested = platforms['meta_ads'].get('requested', {})
    state = {
        'status': report.get('status'),
        'alerts': sorted(report.get('alerts', []), key=lambda row: (row['code'], row.get('detail', ''))),
        'google': {key: platforms['google_ads'].get(key) for key in ('status', 'today_spend_usd', 'configured_enabled_campaigns')},
        'meta': {key: requested.get(key) for key in ('status', 'configured_active_campaigns', 'configured_active_adsets', 'today', 'ad_policy')},
        'microsoft': {key: platforms['microsoft_advertising'].get(key) for key in ('status', 'reason', 'today_spend_usd', 'configured_enabled_campaigns')},
        'go_status': report['revenue_chain']['go_runtime'].get('status'),
        'posthog_status': report['revenue_chain']['posthog'].get('status'),
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def atomic_notice_state(path: Path, state: dict[str, Any]) -> None:
    if path.is_symlink():
        raise OSError('notification_state_symlink')
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def post_discord(report: dict[str, Any], *, force_digest: bool) -> dict[str, Any]:
    # The current published n8n parser requires a send for every nonempty alert
    # report. Silent v1 is an opt-in protocol, not a forged legacy sent receipt.
    if NOTIFICATION_PROTOCOL == 'legacy':
        return _post_discord_once(report, force_digest=force_digest)
    if not report['alerts'] and not force_digest:
        return {'status': 'SILENT_HEALTHY', 'sent': False}
    path = REPORT_DIR / 'notification-state.json'
    digest = notice_fingerprint(report)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.is_symlink():
                raise OSError('notification_state_symlink')
            try:
                previous = json.loads(path.read_text())
            except FileNotFoundError:
                previous = {}
            if previous.get('status') == 'PENDING_AMBIGUOUS':
                return {'status': 'BLOCKED_AMBIGUOUS_PREVIOUS_SEND', 'sent': False, 'receipt': str(path)}
            timestamp = time.time()
            elapsed = timestamp - previous.get('posted_at', 0)
            if previous.get('fingerprint') == digest and previous.get('status') == 'POSTED_VERIFIED' and 0 <= elapsed < 86400:
                return {
                    'status': 'SILENT_UNCHANGED', 'sent': False, 'receipt': str(path),
                    'protocol': 'silent-v1', 'fingerprint': digest,
                    'prior_verified_message_id': previous.get('message_id'),
                    'prior_verified_posted_at': previous.get('posted_at'),
                    'elapsed_seconds': elapsed,
                }
            atomic_notice_state(path, {'status': 'PENDING_AMBIGUOUS', 'fingerprint': digest, 'attempted_at': timestamp})
            outcome = _post_discord_once(report, force_digest=force_digest)
            if outcome.get('sent') and outcome.get('readback_verified'):
                atomic_notice_state(path, {'status': 'POSTED_VERIFIED', 'fingerprint': digest, 'posted_at': timestamp, 'message_id': outcome['message_id']})
            elif not outcome.get('sent'):
                atomic_notice_state(path, previous)
            else:
                atomic_notice_state(path, {'status': 'PENDING_AMBIGUOUS', 'fingerprint': digest, 'message_id': outcome.get('message_id')})
            return outcome
    except Exception as exc:
        return {'status': 'BLOCKED_NOTIFICATION_STATE_OR_SEND', 'sent': False, 'effect': 'UNKNOWN_IF_PENDING_RECEIPT', 'error_class': type(exc).__name__, 'receipt': str(path)}


def run(*, post_digest: bool, post_alerts: bool) -> dict[str, Any]:
    posthog = collect_posthog()
    report = {
        'schema': 'glpcs.ads-monitor.v2',
        'captured_at_utc': now(),
        'mode': 'READ_ONLY',
        'notification_protocol': NOTIFICATION_PROTOCOL,
        'effects': {
            'ad_platform_writes': 0,
            'campaign_deletions': 0,
            'destination_urls_locked': 0,
            'review_submissions': 0,
            'spend_usd': 0,
            'tracking_links_opened': 0,
            'tracking_links_clicked': 0,
            'posthog_events_written': 0,
            'd1_rows_written': 0,
            'budget_or_bid_mutations': 0,
        },
        'platforms': {
            'google_ads': collect_google(),
            'meta_ads': collect_meta(),
            'microsoft_advertising': collect_microsoft(),
        },
        'revenue_chain': {
            'approved_sale_definition': approved_sale_definition(),
            'go_runtime': collect_go_runtime(),
            'n8n_postback_workflows': collect_n8n_postbacks(),
            'postback_receipts': collect_postback_receipts(),
            'posthog': posthog,
            'go_click_join': {
                'status': posthog.get('status'),
                'joined_clicks_today': posthog.get('joined_clicks_today'),
                'raw_identifiers_serialized': False,
            },
        },
    }
    report['spend_labels'] = {
        platform: spend_label(platform, row) for platform, row in (
            ('google', report['platforms']['google_ads']),
            ('meta', report['platforms']['meta_ads'].get('requested', {})),
            ('microsoft', report['platforms']['microsoft_advertising']),
        )
    }
    report['platforms']['microsoft_advertising']['label'] = microsoft_api_label(report['platforms']['microsoft_advertising'])
    report['stop_loss'] = build_stop_loss(report)
    findings = build_alerts(report)
    report['notices'] = [row for row in findings if row['severity'] == 'INFO']
    report['alerts'] = [row for row in findings if row['severity'] != 'INFO']
    report['status'] = (
        'PAUSED_WITH_UNKNOWNS' if not report['alerts'] and (
            report['notices'] or not report['stop_loss']['spend_readback_complete_all_platforms']
        ) else 'PASS_PAUSED_ZERO_SPEND' if not report['alerts'] else 'BLOCKED_WITH_ALERTS'
    )
    report['discord'] = post_discord(report, force_digest=post_digest) if (post_digest or post_alerts) else {'status': 'NOT_REQUESTED', 'sent': False}
    return report


def main() -> int:
    global REPORT_DIR, HISTORY, LATEST, NOTIFICATION_PROTOCOL
    parser = argparse.ArgumentParser()
    parser.add_argument('--post-digest', action='store_true')
    parser.add_argument('--post-alerts', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--notification-protocol', choices=('legacy', 'silent-v1'), default='legacy')
    args = parser.parse_args()
    NOTIFICATION_PROTOCOL = args.notification_protocol
    if args.dry_run and (args.post_digest or args.post_alerts or not args.output_dir):
        parser.error('--dry-run requires --output-dir and forbids post flags')
    if args.output_dir:
        REPORT_DIR = args.output_dir.resolve()
        HISTORY = REPORT_DIR / 'history'
        LATEST = REPORT_DIR / 'latest.json'
    report = run(post_digest=args.post_digest, post_alerts=args.post_alerts)
    report['execution_kind'] = 'MANUAL_NO_POST_DRY_RUN' if args.dry_run else 'EXPLICIT_INVOCATION_NOT_NATURAL_PROOF'
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY.mkdir(parents=True, exist_ok=True)
    stamp = report['captured_at_utc'].replace(':', '').replace('-', '')
    history_path = HISTORY / f'{stamp}.json'
    encoded = json.dumps(report, indent=2, sort_keys=True) + '\n'
    history_path.write_text(encoded, encoding='utf-8')
    LATEST.write_text(encoded, encoding='utf-8')
    print(json.dumps({
        'status': report['status'],
        'notification_protocol': NOTIFICATION_PROTOCOL,
        'alerts': len(report['alerts']),
        'latest': str(LATEST),
        'history': str(history_path),
        'discord': report['discord'],
    }, sort_keys=True))
    if NOTIFICATION_PROTOCOL == 'silent-v1' and report['discord'].get('status') in ('SILENT_UNCHANGED', 'SILENT_HEALTHY'):
        print('[SILENT]')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
