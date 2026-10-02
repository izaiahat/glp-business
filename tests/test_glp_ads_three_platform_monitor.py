from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/glp_ads_three_platform_monitor.py'


def load_module():
    spec = importlib.util.spec_from_file_location('glp_ads_monitor', SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MicrosoftReportClient:
    def __init__(self, state=None, blob=None, account='189346863', campaign='571604457', status='Paused'):
        self.state = state if state is not None else {'Status': 'Success', 'ReportDownloadUrl': None}
        self.blob = blob
        self.account = account
        self.campaign = campaign
        self.status = status
        self.downloads = 0

    def get_account(self):
        return {'Account': {'Id': self.account}}

    def get_campaigns(self):
        return {'Campaigns': [{'Id': self.campaign, 'Status': self.status, 'CampaignType': 'Search'}]}

    def submit_campaign_performance_report(self, campaign, start, end):
        assert campaign == self.campaign
        return {'ReportRequestId': 'fixture-report'}

    def poll_generate_report(self, request_id):
        return {'ReportRequestStatus': self.state}

    def download_generated_report(self, url, maximum_bytes):
        self.downloads += 1
        return self.blob


def test_microsoft_provider_nil_download_is_not_numeric_zero(monkeypatch):
    module = load_module()
    client = MicrosoftReportClient()
    monkeypatch.setattr(module, 'microsoft_client', lambda: client)
    row = module.collect_microsoft()
    assert row['data_status'] == 'PROVIDER_SUCCESS_NO_DATA'
    assert row['status'] == 'UNKNOWN'
    assert row['api_status'] == 'OK'
    assert row['today_spend_usd'] is None
    assert row['spend_readback_available'] is False
    assert row['report_receipts'][0]['download_url_present'] is False
    assert client.downloads == 0
    assert module.paused_state('microsoft', row) is True
    assert module.expected_empty_spend('microsoft', row) is False
    assert module.spend_label('microsoft', row) == 'UNKNOWN · spend unreadable'
    report = healthy_report(module)
    report['platforms']['microsoft_advertising'] = row
    assert 'MICROSOFT_REPORT_STATE_UNREADABLE' in {item['code'] for item in module.build_alerts(report)}
    guard = module.build_stop_loss(report)
    assert guard['status'] == 'ARMED_WITH_SOURCE_BLOCKERS'
    assert guard['spend_readback_complete_all_platforms'] is False
    assert guard['platform_breakdown']['microsoft']['spend_usd'] is None
    assert guard['budget_or_bid_mutations_allowed'] is False


@pytest.mark.parametrize('state,blob,data_status', [
    ({'Status': 'Success', 'ReportDownloadUrl': 'https://example.invalid/report'},
     b'AccountId,CampaignId,Spend,Clicks,Impressions\n', 'EMPTY_CSV'),
    ({'Status': 'Success'}, None, 'UNKNOWN_READ_FAILED'),
    ({'Status': 'Success', 'ReportDownloadUrl': ''}, None, 'UNKNOWN_READ_FAILED'),
    ({'Status': 'Success', 'ReportDownloadUrl': False}, None, 'UNKNOWN_READ_FAILED'),
    ({'Status': 'Pending'}, None, 'UNKNOWN_PARTIAL_OR_PENDING'),
    ({'Status': 'Error'}, None, 'UNKNOWN_PARTIAL_OR_PENDING'),
    ({'ReportDownloadUrl': None}, None, 'UNKNOWN_READ_FAILED'),
    ({'Status': 'Success', 'ReportDownloadUrl': 'https://example.invalid/report'},
     b'AccountId,CampaignId,Clicks,Impressions\n189346863,571604457,0,0\n', 'UNKNOWN_READ_FAILED'),
    ({'Status': 'Success', 'ReportDownloadUrl': 'https://example.invalid/report'},
     b'AccountId,CampaignId,Spend,Clicks,Impressions\n189346863,571604457,,0,0\n', 'UNKNOWN_READ_FAILED'),
    ({'Status': 'Success', 'ReportDownloadUrl': 'https://example.invalid/report'},
     b'AccountId,CampaignId,Spend,Clicks,Impressions\nother,571604457,0,0,0\n', 'UNKNOWN_READ_FAILED'),
])
def test_microsoft_absent_or_unproven_report_metrics_remain_unknown(monkeypatch, state, blob, data_status):
    module = load_module()
    monkeypatch.setattr(module, 'microsoft_client', lambda: MicrosoftReportClient(state, blob))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    row = module.collect_microsoft()
    assert row['status'] == 'UNKNOWN'
    assert row['data_status'] == data_status
    assert row['today_spend_usd'] is None
    assert row['spend_readback_available'] is False
    assert module.expected_empty_spend('microsoft', row) is False


@pytest.mark.parametrize('account,campaign', [('other', '571604457'), ('189346863', 'other')])
def test_microsoft_rejects_wrong_target_before_reporting(monkeypatch, account, campaign):
    module = load_module()
    client = MicrosoftReportClient(account=account, campaign=campaign)
    monkeypatch.setattr(client, 'submit_campaign_performance_report',
                        lambda campaign, start, end: pytest.fail('wrong identity report submitted'))
    monkeypatch.setattr(module, 'microsoft_client', lambda: client)
    row = module.collect_microsoft()
    assert row['status'] == 'UNKNOWN'
    assert row['today_spend_usd'] is None
    assert module.paused_state('microsoft', row) is False


@pytest.mark.parametrize('spend', ['0', '5.25'])
def test_microsoft_real_numeric_rows_are_the_only_spend_proof(monkeypatch, spend):
    module = load_module()
    blob = f'AccountId,CampaignId,Spend,Clicks,Impressions\n189346863,571604457,{spend},0,0\n'.encode()
    monkeypatch.setattr(module, 'microsoft_client', lambda: MicrosoftReportClient(
        {'Status': 'Success', 'ReportDownloadUrl': 'https://example.invalid/report'}, blob))
    row = module.collect_microsoft()
    assert row['status'] == 'PASS'
    assert row['today_spend_usd'] == float(spend)
    assert row['spend_readback_available'] is True
    assert row['data_status'] == 'ROWS'


def test_microsoft_active_campaign_does_not_inherit_pause_proof(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, 'microsoft_client', lambda: MicrosoftReportClient(status='Active'))
    row = module.collect_microsoft()
    assert row['status'] == 'UNKNOWN'
    assert row['configured_enabled_campaigns'] == 1
    assert module.paused_state('microsoft', row) is False
    assert module.expected_empty_spend('microsoft', row) is False


def test_microsoft_auth_failure_is_blocked_and_never_zero(monkeypatch):
    module = load_module()
    def failure():
        raise RuntimeError('private auth failure not to be serialized')
    monkeypatch.setattr(module, 'microsoft_client', failure)
    row = module.collect_microsoft()
    assert row['status'] == 'BLOCKED'
    assert row['api_status'] == 'BLOCKED'
    assert row['today_spend_usd'] is None
    assert module.paused_state('microsoft', row) is False
    assert 'private auth failure' not in json.dumps(row)


def test_provider_exception_tail_never_serializes_untrusted_details():
    module = load_module()
    hostile_messages = [
        'Authorization: Bearer top-secret-token',
        'client_secret="top secret value" refresh_token=another-secret',
        'https://example.invalid/?access_token=url-secret&api_key=other-secret',
    ]
    for message in hostile_messages:
        result = module.redacted_error_tail(RuntimeError(message))
        assert result == 'RuntimeError: [REDACTED]'
        assert message not in result


def healthy_report(module):
    return {
        'platforms': {
            'google_ads': {
                'status': 'PASS', 'today_spend_usd': 0, 'configured_enabled_campaigns': 0,
                'identity': {'status': 'ENABLED'},
                'inventory': {'ads': {
                    'policy_readback_available': True,
                    'by_approval_status': {'DISAPPROVED': 0},
                    'policy_state_sha256': 'google-policy-state-current',
                    'policy_issue_item_refs': [],
                }},
            },
            'meta_ads': {
                'requested': {
                    'status': 'PASS', 'today': {'spend_usd': 0}, 'configured_active_campaigns': 0,
                    'account_status': 1, 'disable_reason': 0,
                    'ad_policy_readback_available': True,
                    'ad_policy': {
                        'issue_count': 0,
                        'policy_state_sha256': 'meta-policy-state-current',
                        'policy_issue_item_refs': [],
                    },
                },
                'requested_identity_match': True,
            },
            'microsoft_advertising': {
                'status': 'PASS', 'today_spend_usd': 0, 'configured_enabled_campaigns': 0,
            },
        },
        'revenue_chain': {
            'go_runtime': {'status': 'PASS'},
            'posthog': {
                'status': 'PASS',
                'joined_clicks_today': 0,
                'joined_clicks_by_platform': {'google': 0, 'microsoft': 0, 'meta': 0},
                'canary_status': 'PASS',
                'platform_click_id_counts': {
                    'google_gclid': 0,
                    'microsoft_msclkid': 0,
                    'meta_fbclid': 0,
                },
            },
        },
        'stop_loss': {'rules': []},
    }


def test_alerts_are_empty_only_when_every_hard_rail_is_green():
    module = load_module()
    report = healthy_report(module)
    assert module.build_alerts(report) == []


def test_nonzero_spend_enabled_campaigns_and_missing_surfaces_alert():
    module = load_module()
    report = healthy_report(module)
    report['platforms']['google_ads'].update({'today_spend_usd': 1.25, 'configured_enabled_campaigns': 1})
    report['platforms']['meta_ads']['requested'].update({'today': {'spend_usd': 2}, 'configured_active_campaigns': 1})
    report['platforms']['meta_ads']['requested_identity_match'] = False
    report['platforms']['microsoft_advertising'] = {'status': 'BLOCKED', 'reason': 'missing'}
    report['revenue_chain']['go_runtime'] = {'status': 'BLOCKED'}
    report['revenue_chain']['posthog'] = {'status': 'BLOCKED', 'reason': 'missing'}
    codes = {row['code'] for row in module.build_alerts(report)}
    assert {
        'GOOGLE_SPEND_NONZERO',
        'META_SPEND_NONZERO',
        'GOOGLE_CAMPAIGN_ENABLED',
        'REQUESTED_META_CAMPAIGN_CONFIGURED_ACTIVE',
        'MICROSOFT_ADS_UNMONITORED',
        'GO_RUNTIME_MONETIZATION_BLOCKED',
        'POSTHOG_CS_CLICK_ID_QUERY_BLOCKED',
        'META_ACCOUNT_IDENTITY_MISMATCH',
    } == codes


def test_microsoft_nonzero_spend_and_enabled_campaign_alert_even_with_joined_click():
    module = load_module()
    report = healthy_report(module)
    report['platforms']['microsoft_advertising'].update({
        'today_spend_usd': 5,
        'configured_enabled_campaigns': 1,
    })
    report['revenue_chain']['posthog']['joined_clicks_by_platform']['microsoft'] = 1
    codes = {row['code'] for row in module.build_alerts(report)}
    assert 'MICROSOFT_SPEND_NONZERO' in codes
    assert 'MICROSOFT_CAMPAIGN_ENABLED' in codes


def test_failed_meta_reads_are_unknown_not_zero(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, 'http_json', lambda *args, **kwargs: (403, {'error': {'code': 200}}))
    row = module.collect_meta_account(module.META_REQUESTED, 'redacted-test-token')
    assert row['status'] == 'BLOCKED'
    assert row['campaign_readback_available'] is False
    assert row['spend_readback_available'] is False
    assert row['configured_active_campaigns'] is None
    assert row['paused_campaigns'] is None
    assert row['today']['spend_usd'] is None
    assert row['today']['clicks'] is None
    assert row['today']['normalized_conversions'] is None
    assert row['today']['conversion_readback_available'] is False
    assert row['ad_policy_readback_available'] is False


def test_unknown_meta_state_alerts_instead_of_claiming_zero():
    module = load_module()
    report = healthy_report(module)
    report['platforms']['meta_ads']['requested'] = {
        'status': 'BLOCKED',
        'configured_active_campaigns': None,
        'today': {'spend_usd': None},
    }
    codes = {row['code'] for row in module.build_alerts(report)}
    assert 'META_REQUESTED_STATE_UNREADABLE' in codes
    assert 'META_SPEND_NONZERO' not in codes


def test_unbound_meta_custom_conversion_is_not_a_paused_p1():
    module = load_module()
    report = healthy_report(module)
    report['platforms']['meta_ads']['requested']['today']['conversion_definition_status'] = (
        'BLOCKED_APPROVED_SALE_CUSTOM_CONVERSION_ID_ABSENT'
    )
    codes = {row['code'] for row in module.build_alerts(report)}
    assert 'META_APPROVED_SALE_CONVERSION_DEFINITION_UNBOUND' not in codes


def test_stop_loss_rules_are_armed_without_mutation_authority():
    module = load_module()
    report = healthy_report(module)
    report['platforms']['google_ads']['inventory'] = {'ads': {
        'policy_readback_available': True,
        'by_approval_status': {'DISAPPROVED': 1},
        'policy_issue_item_refs': ['ad_sha256_new'],
    }}
    report['revenue_chain']['posthog'] = {
        'status': 'BLOCKED',
        'joined_clicks_today': None,
        'canary_status': 'UNKNOWN_SOURCE_UNAVAILABLE',
        'platform_click_id_counts': {},
        'joined_clicks_by_platform': {'google': None, 'microsoft': None, 'meta': None},
    }
    report['previous_stop_loss'] = {
        'google_disapproved_ads_baseline': 0,
        'meta_policy_issue_ads_baseline': 0,
        'google_policy_issue_refs_baseline': [],
        'meta_policy_issue_refs_baseline': [],
    }
    stop_loss = module.build_stop_loss(report)
    assert stop_loss['status'] == 'ARMED_WITH_SOURCE_BLOCKERS'
    assert stop_loss['budget_or_bid_mutations_allowed'] is False
    assert all(rule['armed'] for rule in stop_loss['rules'])
    assert next(rule for rule in stop_loss['rules'] if rule['code'] == 'DISAPPROVAL_APPEARED')['triggered'] is True


def test_historical_disapprovals_do_not_retrigger_without_an_increase():
    module = load_module()
    report = healthy_report(module)
    report['platforms']['google_ads']['inventory'] = {'ads': {
        'policy_readback_available': True,
        'by_approval_status': {'DISAPPROVED': 8},
        'policy_issue_item_refs': ['ad_sha256_existing'],
    }}
    report['previous_stop_loss'] = {
        'google_disapproved_ads_baseline': 8,
        'meta_policy_issue_ads_baseline': 0,
        'google_policy_issue_refs_baseline': ['ad_sha256_existing'],
        'meta_policy_issue_refs_baseline': [],
    }
    stop_loss = module.build_stop_loss(report)
    rule = next(row for row in stop_loss['rules'] if row['code'] == 'DISAPPROVAL_APPEARED')
    assert rule['triggered'] is False
    assert rule['google']['baseline_count'] == 8


def test_policy_state_hash_transition_triggers_with_stable_issue_references():
    module = load_module()
    report = healthy_report(module)
    report['previous_stop_loss'] = {
        'google_disapproved_ads_baseline': 0,
        'meta_policy_issue_ads_baseline': 0,
        'google_policy_issue_refs_baseline': [],
        'meta_policy_issue_refs_baseline': [],
        'google_policy_state_sha256_baseline': 'google-policy-state-before',
        'meta_policy_state_sha256_baseline': 'meta-policy-state-before',
    }
    stop_loss = module.build_stop_loss(report)
    rule = next(row for row in stop_loss['rules'] if row['code'] == 'DISAPPROVAL_APPEARED')
    assert rule['triggered'] is True
    assert rule['policy_state_sha256_changed_platforms'] == ['google', 'meta']
    assert stop_loss['google_policy_state_sha256_baseline'] == 'google-policy-state-current'
    assert stop_loss['meta_policy_state_sha256_baseline'] == 'meta-policy-state-current'


def test_policy_reason_fingerprint_is_order_stable_and_semantic():
    module = load_module()
    reason_a = [{'reason': 'POLICY_REASON_A', 'details': {'level': 'AD', 'code': 7}}]
    reason_a_reordered = [{'details': {'code': 7, 'level': 'AD'}, 'reason': 'POLICY_REASON_A'}]
    reason_b = [{'reason': 'POLICY_REASON_B', 'details': {'level': 'AD', 'code': 7}}]
    google_a = module.policy_state_fingerprint('google-ad', ['DISAPPROVED'], reason_a)
    google_a_reordered = module.policy_state_fingerprint(
        'google-ad', ['DISAPPROVED'], reason_a_reordered)
    meta_b = module.policy_state_fingerprint('meta-ad', ['PAUSED', 'WITH_ISSUES'], reason_b)
    meta_a = module.policy_state_fingerprint('meta-ad', ['PAUSED', 'WITH_ISSUES'], reason_a)
    assert google_a == google_a_reordered
    assert google_a != module.policy_state_fingerprint('google-ad', ['DISAPPROVED'], reason_b)
    assert meta_a != meta_b


def test_meta_policy_reason_change_changes_state_hash_without_serializing_reason(monkeypatch):
    module = load_module()

    def collect(reason: str):
        responses = iter([
            (200, {'id': module.META_REQUESTED, 'name': 'GLP Clinic Scout', 'account_status': 1}),
            (200, {'data': []}),
            (200, {'data': [{
                'id': 'meta-ad-1',
                'status': 'PAUSED',
                'effective_status': 'WITH_ISSUES',
                'issues_info': [{'reason': reason}],
            }]}),
            (200, {'data': []}),
            (200, {'data': []}),  # requested ad-set state
        ])
        monkeypatch.setattr(module, 'http_json', lambda *args, **kwargs: next(responses))
        return module.collect_meta_account(module.META_REQUESTED, 'redacted-test-token')

    first = collect('POLICY_REASON_A')
    second = collect('POLICY_REASON_B')
    assert first['ad_policy']['policy_state_sha256'] != second['ad_policy']['policy_state_sha256']
    assert 'POLICY_REASON_A' not in json.dumps(first)
    assert 'POLICY_REASON_B' not in json.dumps(second)


def test_failed_policy_read_preserves_last_valid_baseline_without_false_trigger():
    module = load_module()
    report = healthy_report(module)
    report['platforms']['google_ads'] = {
        'status': 'BLOCKED',
        'today_spend_usd': None,
        'configured_enabled_campaigns': None,
        'identity': {'status': None},
        'inventory': {'ads': {
            'policy_readback_available': False,
            'by_approval_status': {},
            'policy_state_sha256': None,
            'policy_issue_item_refs': None,
        }},
    }
    report['previous_stop_loss'] = {
        'google_disapproved_ads_baseline': 8,
        'meta_policy_issue_ads_baseline': 0,
        'google_policy_issue_refs_baseline': ['ad_sha256_existing'],
        'meta_policy_issue_refs_baseline': [],
        'google_policy_state_sha256_baseline': 'google-policy-state-valid',
        'meta_policy_state_sha256_baseline': 'meta-policy-state-current',
    }
    stop_loss = module.build_stop_loss(report)
    rule = next(row for row in stop_loss['rules'] if row['code'] == 'DISAPPROVAL_APPEARED')
    assert rule['triggered'] is False
    assert rule['evaluation'] == 'SOURCE_UNAVAILABLE'
    assert rule['google']['current_count'] is None
    assert stop_loss['google_disapproved_ads_baseline'] == 8
    assert stop_loss['google_policy_issue_refs_baseline'] == ['ad_sha256_existing']
    assert stop_loss['google_policy_state_sha256_baseline'] == 'google-policy-state-valid'


def test_meta_normalizes_only_exact_approved_sale_action(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, 'META_APPROVED_SALE_ACTION_TYPE', 'offsite_conversion.custom.12345')
    responses = iter([
        (200, {'id': module.META_REQUESTED, 'name': 'GLP Clinic Scout', 'account_status': 1}),
        (200, {'data': []}),
        (200, {'data': []}),
        (200, {'data': [{
            'spend': '0', 'impressions': '0', 'clicks': '0',
            'actions': [
                {'action_type': module.META_APPROVED_SALE_ACTION_TYPE, 'value': '2'},
                {'action_type': 'purchase', 'value': '2'},
                {'action_type': 'omni_purchase', 'value': '2'},
            ],
        }]}),
        (200, {'data': []}),  # requested ad-set state
    ])
    monkeypatch.setattr(module, 'http_json', lambda *args, **kwargs: next(responses))
    row = module.collect_meta_account(module.META_REQUESTED, 'redacted-test-token')
    assert row['today']['normalized_conversions'] == 2
    assert row['today']['conversion_action_values'] == {
        module.META_APPROVED_SALE_ACTION_TYPE: 2,
    }


def test_meta_conversion_is_unknown_without_account_native_custom_conversion_id(monkeypatch):
    module = load_module()
    assert module.META_APPROVED_SALE_ACTION_TYPE is None
    responses = iter([
        (200, {'id': module.META_REQUESTED, 'name': 'GLP Clinic Scout', 'account_status': 1}),
        (200, {'data': []}),
        (200, {'data': []}),
        (200, {'data': [{
            'spend': '0', 'impressions': '0', 'clicks': '0',
            'actions': [{'action_type': 'offsite_conversion.custom.12345', 'value': '2'}],
        }]}),
        (200, {'data': []}),  # requested ad-set state
    ])
    monkeypatch.setattr(module, 'http_json', lambda *args, **kwargs: next(responses))
    row = module.collect_meta_account(module.META_REQUESTED, 'redacted-test-token')
    assert row['today']['normalized_conversions'] is None
    assert row['today']['conversion_readback_available'] is False
    assert row['today']['conversion_definition_status'] == (
        'NOT_APPLICABLE_UNTIL_SPEND'
    )
    assert row['today']['conversion_action_values'] is None


def test_account_restriction_rule_triggers_for_meta_disable_reason():
    module = load_module()
    report = healthy_report(module)
    report['platforms']['meta_ads']['requested']['disable_reason'] = 1
    stop_loss = module.build_stop_loss(report)
    rule = next(row for row in stop_loss['rules'] if row['code'] == 'ACCOUNT_RESTRICTION_APPEARED')
    assert rule['triggered'] is True
    assert rule['triggered_platforms'] == ['meta']


def test_posthog_measured_zero_is_healthy_and_does_not_serialize_ids(monkeypatch):
    module = load_module()

    class FakePostHog:
        def hogql(self, query):
            if 'SELECT DISTINCT' in query:
                return {'ok': True, 'rows': []}
            return {'ok': True, 'rows': [[0, 0, 0, 0, 0, '1970-01-01T00:00:00Z']]}

    class FakeD1:
        def query(self, sql):
            return []

    monkeypatch.setattr(module, 'posthog_client', lambda: FakePostHog())
    monkeypatch.setattr(module, 'd1_client', lambda: FakeD1())
    row = module.collect_posthog()
    assert row['status'] == 'PASS'
    assert row['canary_status'] == 'MEASURED_ZERO_IDLE'
    assert row['joined_clicks_today'] == 0
    assert row['raw_identifiers_serialized'] is False
    assert 'cs_click_ids' not in row


def test_posthog_counts_joined_clicks_per_platform_not_raw_click_ids(monkeypatch):
    module = load_module()

    class FakePostHog:
        def hogql(self, query):
            if 'SELECT DISTINCT' in query:
                return {'ok': True, 'rows': [
                    ['joined-google', 'g-1', '', ''],
                    ['unjoined-google', 'g-2', '', ''],
                    ['joined-meta', '', '', 'f-1'],
                ]}
            return {'ok': True, 'rows': [[3, 3, 2, 0, 1, '2026-09-01T00:00:00Z']]}

    class FakeD1:
        def query(self, sql):
            return [{'cs_click_id': 'joined-google'}, {'cs_click_id': 'joined-meta'}]

    monkeypatch.setattr(module, 'posthog_client', lambda: FakePostHog())
    monkeypatch.setattr(module, 'd1_client', lambda: FakeD1())
    row = module.collect_posthog()
    assert row['platform_click_id_counts']['google_gclid'] == 2
    assert row['joined_clicks_by_platform'] == {'google': 1, 'microsoft': 0, 'meta': 1}
    report = healthy_report(module)
    report['platforms']['google_ads']['today_spend_usd'] = 3
    report['revenue_chain']['posthog'] = row
    rule = next(
        item for item in module.build_stop_loss(report)['rules']
        if item['code'] == 'SPEND_WITH_ZERO_JOINED_CLICKS'
    )
    assert rule['triggered'] is False


def test_stop_loss_evaluates_spend_per_platform():
    module = load_module()
    report = healthy_report(module)
    report['platforms']['google_ads']['today_spend_usd'] = 3
    report['previous_stop_loss'] = {'google_disapproved_ads': 0}
    stop_loss = module.build_stop_loss(report)
    rule = next(row for row in stop_loss['rules'] if row['code'] == 'SPEND_WITH_ZERO_JOINED_CLICKS')
    assert rule['triggered'] is True
    assert rule['triggered_platforms'] == ['google']
    assert stop_loss['platform_breakdown']['google']['spend_usd'] == 3


def test_partial_join_source_unavailability_fails_loud_even_at_zero_spend():
    module = load_module()
    report = healthy_report(module)
    report['revenue_chain']['posthog']['joined_clicks_by_platform']['microsoft'] = None
    stop_loss = module.build_stop_loss(report)
    report['stop_loss'] = stop_loss
    rule = next(row for row in stop_loss['rules'] if row['code'] == 'JOIN_SOURCE_UNAVAILABLE')
    assert stop_loss['status'] == 'ARMED_WITH_SOURCE_BLOCKERS'
    assert rule['triggered'] is True
    assert rule['triggered_platforms'] == ['microsoft']
    assert 'JOIN_SOURCE_UNAVAILABLE' in {row['code'] for row in module.build_alerts(report)}


def test_discord_stays_silent_when_healthy_and_digest_not_forced():
    module = load_module()
    report = healthy_report(module)
    report['alerts'] = []
    assert module.post_discord(report, force_digest=False) == {'status': 'SILENT_HEALTHY', 'sent': False}


def test_discord_uses_cloudflare_safe_user_agent():
    module = load_module()
    assert module.DISCORD_UA == 'DiscordBot (https://discordapp.com, 10)'


def test_bridge_actions_are_exact_read_only_entrypoints():
    actions = json.loads(Path('/home/ubuntu/business/ops/n8n-bridge/actions.json').read_text())
    expected_python = '/home/ubuntu/business/ventures/glp-clinic-scout/ads-control-plane/.venv/bin/python'
    assert actions['glp_ads_monitor']['argv'] == [
        expected_python,
        'scripts/glp_ads_three_platform_monitor.py',
        '--post-alerts',
        '--notification-protocol=silent-v1',
    ]
    assert actions['glp_ads_monitor_daily_digest']['argv'] == [
        expected_python,
        'scripts/glp_ads_three_platform_monitor.py',
        '--post-digest',
        '--notification-protocol=silent-v1',
    ]
    assert actions['glp_ads_monitor']['allowed_args'] == []
    assert actions['glp_ads_monitor_daily_digest']['allowed_args'] == []
