#!/usr/bin/env python3
"""A4 report producer. No sends or object mutations; Q23 adoption not required."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path('/home/ubuntu/business')
sys.path.insert(0, str(ROOT / 'scripts'))
HERE = Path(__file__).resolve().parent
PT = ZoneInfo('America/Los_Angeles')
SOURCES = {
    'monitor': ROOT / 'scripts/glp_ads_three_platform_monitor.py',
    'economics': ROOT / 'reports/ops/orchestration-restore-20260907/WS-G/offline_extension.py',
    'formatter': ROOT / 'ops/discord_fmt.py',
    'microsoft': ROOT / 'scripts/microsoft_ads_api.py',
    'posthog': ROOT / 'ops/glp/seo_data_rails.py',
}
A3 = HERE.parent / 'A3/RESULT.json'
PLATFORMS = ('google', 'meta', 'microsoft')
CAMPAIGNS = {'google': '24204737927', 'meta': '120248533692790046', 'microsoft': '571604457'}


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dependencies():
    pins = json.loads((HERE / 'SOURCE-PINS.json').read_text())
    for name, path in SOURCES.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != pins[name]['sha256']:
            raise ValueError('source_drift_' + name)
    return {key: load_module('a4_' + key, SOURCES[key]) for key in ('monitor', 'economics', 'formatter')}


def windows(now):
    local = now.astimezone(PT)
    monday = local.date() - timedelta(days=local.weekday())
    result = {}
    for label, offset in [('current', 7), ('previous', 14)]:
        start = monday - timedelta(days=offset)
        end = start + timedelta(days=7)
        result[label] = {'start': str(start), 'end': str(end - timedelta(days=1)),
                         'end_exclusive': str(end), 'timezone': str(PT),
                         'start_utc': datetime.combine(start, datetime.min.time(), PT).astimezone(timezone.utc).isoformat(),
                         'end_utc': datetime.combine(end, datetime.min.time(), PT).astimezone(timezone.utc).isoformat()}
    return result


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return value


def unknown(reason):
    return {'status': 'UNKNOWN', 'reason': reason, 'rows': [], 'coverage': 'UNKNOWN'}


def guarded(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:
        # Never persist provider exception text, credentials or response bodies.
        return unknown(type(exc).__name__)


def google_week(m, w):
    sys.path.insert(0, str(m.CONTROL))
    os.environ.setdefault('GLP_GOOGLE_ADS_LOGIN_CUSTOMER_ID', m.GOOGLE_LOGIN)
    from adsctl.google_ads_client import _build_google_ads_client_config
    from adsctl.google_ads_version import resolve_google_ads_api_version
    from google.ads.googleads.client import GoogleAdsClient
    client = GoogleAdsClient.load_from_dict(_build_google_ads_client_config(), version=resolve_google_ads_api_version())
    service = client.get_service('GoogleAdsService')
    identity = list(service.search(customer_id=m.GOOGLE_CUSTOMER,
        query='SELECT customer.id, customer.currency_code, customer.time_zone FROM customer'))
    if len(identity) != 1 or str(identity[0].customer.id) != m.GOOGLE_CUSTOMER:
        raise ValueError('google_identity')
    if identity[0].customer.currency_code != 'USD' or identity[0].customer.time_zone != str(PT):
        raise ValueError('google_currency_or_timezone')
    query = ('SELECT campaign.id, ad_group.id, ad_group_ad.ad.id, metrics.impressions, metrics.clicks, '
             'metrics.cost_micros, metrics.conversions FROM ad_group_ad '
             f"WHERE campaign.id = {CAMPAIGNS['google']} AND segments.date BETWEEN '{w['start']}' AND '{w['end']}'")
    rows = []
    for r in service.search(customer_id=m.GOOGLE_CUSTOMER, query=query):
        if str(r.campaign.id) != CAMPAIGNS['google']:
            raise ValueError('google_campaign_scope')
        rows.append({'campaign_id': str(r.campaign.id), 'ad_group_id': str(r.ad_group.id),
                     'object_id': str(r.ad_group_ad.ad.id), 'impressions': int(r.metrics.impressions),
                     'clicks': int(r.metrics.clicks), 'spend': int(r.metrics.cost_micros) / 1e6,
                     'platform_conversions': float(r.metrics.conversions)})
    return {'status': 'PASS' if rows else 'UNKNOWN', 'reason': None if rows else 'NO_EXPLICIT_ROWS',
            'rows': rows, 'coverage': 'RETURNED_AD_ROWS_ONLY', 'window': w, 'currency': 'USD',
            'account_id': m.GOOGLE_CUSTOMER, 'query_sha256': hashlib.sha256(query.encode()).hexdigest(),
            'approved_sales': None, 'complete_mature_cohort': False}


def meta_week(m, w):
    token = m.safe_read(m.META_TOKEN_PATH)
    if not token:
        return unknown('CREDENTIAL_UNAVAILABLE')
    base = 'https://graph.facebook.com/v25.0/' + m.META_REQUESTED
    status, identity = m.http_json(base, params={'access_token': token, 'fields': 'id,name,currency,timezone_name'})
    if status != 200 or identity.get('id') != m.META_REQUESTED or identity.get('name') != 'GLP Clinic Scout':
        raise ValueError('meta_identity')
    if identity.get('currency') != 'USD' or identity.get('timezone_name') != str(PT):
        raise ValueError('meta_currency_or_timezone')
    status, payload = m.meta_collection(base + '/insights', params={
        'access_token': token, 'fields': 'campaign_id,adset_id,ad_id,spend,impressions,clicks',
        'time_range': json.dumps({'since': w['start'], 'until': w['end']}), 'level': 'ad', 'limit': 500,
        'filtering': json.dumps([{'field': 'campaign.id', 'operator': 'IN', 'value': [CAMPAIGNS['meta']]}])})
    if status != 200:
        return unknown('INSIGHTS_OR_PAGINATION_UNAVAILABLE')
    if any(str(r.get('campaign_id')) != CAMPAIGNS['meta'] for r in payload['data']):
        raise ValueError('meta_campaign_scope')
    rows = [{'campaign_id': str(r['campaign_id']), 'ad_group_id': str(r['adset_id']),
             'object_id': str(r['ad_id']), 'spend': float(r['spend']),
             'impressions': int(r['impressions']), 'clicks': int(r['clicks'])} for r in payload['data']]
    return {'status': 'PASS' if rows else 'UNKNOWN', 'reason': None if rows else 'NO_EXPLICIT_ROWS',
            'rows': rows, 'coverage': 'RETURNED_AD_ROWS_ONLY', 'window': w, 'currency': 'USD',
            'account_id': m.META_REQUESTED, 'approved_sales': None, 'complete_mature_cohort': False}


def microsoft_week(m, w):
    client = m.microsoft_client()
    account = client.get_account()['Account']
    if str(account.get('Id')) != m.MICROSOFT_ACCOUNT or account.get('CurrencyCode') != 'USD':
        raise ValueError('microsoft_identity_or_currency')
    campaigns = client.get_campaigns()['Campaigns']
    if not campaigns or len(campaigns) > 20:
        raise ValueError('microsoft_campaign_denominator')
    campaigns = [c for c in campaigns if str(c['Id']) == CAMPAIGNS['microsoft']]
    if len(campaigns) != 1:
        raise ValueError('microsoft_campaign_scope')
    def date_doc(s):
        d = datetime.fromisoformat(s)
        return {'Day': d.day, 'Month': d.month, 'Year': d.year}
    rows, receipts = [], []
    for campaign in campaigns:
        cid = str(campaign['Id'])
        request_id = client.submit_campaign_performance_report(cid, date_doc(w['start']), date_doc(w['end']))['ReportRequestId']
        receipt = {'campaign_id': cid, 'request_id': request_id, 'status': 'Pending'}
        receipts.append(receipt)
        state = {}
        for attempt in range(4):
            state = client.poll_generate_report(request_id)['ReportRequestStatus']
            receipt['status'] = state.get('Status')
            if state.get('Status') != 'Pending':
                break
            if attempt < 3:
                time.sleep(2)
        if state.get('Status') == 'Success' and state.get('ReportDownloadUrl'):
            blob = client.download_generated_report(state['ReportDownloadUrl'], maximum_bytes=5 * 1024 * 1024)
            receipt['download_sha256'] = hashlib.sha256(blob).hexdigest()
            metrics = m.microsoft_report_metrics(blob, cid)
            if metrics is not None:
                rows.append({'object_id': cid, 'campaign_id': cid, **metrics})
    complete = len(rows) == len(campaigns)
    return {'status': 'PASS' if complete else 'UNKNOWN', 'reason': None if complete else 'MISSING_CAMPAIGN_METRIC_ROWS',
            'rows': rows, 'coverage': 'ALL_CAMPAIGNS_RETURNED' if complete else 'PARTIAL_OR_EMPTY',
            'window': w, 'currency': 'USD', 'account_id': m.MICROSOFT_ACCOUNT,
            'report_receipts': receipts, 'granularity': 'campaign_not_creative',
            'complete_mature_cohort': False, 'provider_complete_data_required': False,
            'approved_sales': None}


def posthog_week(m, w):
    # Existing aggregates only. No people, raw identifiers, quiz answers or events emitted.
    start = datetime.fromisoformat(w['start_utc']).strftime('%Y-%m-%d %H:%M:%S')
    end = datetime.fromisoformat(w['end_utc']).strftime('%Y-%m-%d %H:%M:%S')
    query = f"""SELECT
      countIf(event = '$pageview' AND path(properties.$current_url) IN ('/lp/quiz/', '/lp/quiz')),
      countIf(event = 'quiz_start' AND path(properties.$current_url) IN ('/lp/quiz/', '/lp/quiz')),
      countIf(event = 'quiz_complete' AND path(properties.$current_url) IN ('/lp/quiz/', '/lp/quiz')),
      countIf(event = 'affiliate_click')
      FROM events WHERE domain(properties.$current_url) = 'glpclinicscout.com'
      AND timestamp >= toDateTime('{start}') AND timestamp < toDateTime('{end}')"""
    response = m.posthog_client().hogql(query)
    rows = response.get('rows')
    if not response.get('ok') or not isinstance(rows, list) or len(rows) != 1 or len(rows[0]) != 4:
        return unknown('AGGREGATE_QUERY_UNAVAILABLE')
    if any(finite(v) is None for v in rows[0]):
        raise ValueError('invalid_aggregate')
    return {'status': 'PASS', 'project_id': '508172', 'window': w,
            'observed_counts': dict(zip(('paid_lp_pageviews', 'paid_lp_quiz_start', 'paid_lp_quiz_complete', 'site_affiliate_click'), rows[0])),
            'query_sha256': hashlib.sha256(query.encode()).hexdigest(),
            'real_non_test': 'UNKNOWN', 'consent': 'UNKNOWN', 'joined_funnel': 'UNKNOWN',
            'approved_sales': None, 'raw_identifiers_serialized': False}


def read_a3_observation():
    """Read one frozen predecessor, not a new closeout or spend authority.

    result_contract validates today's RESULT closeouts. This historical BLOCKED
    receipt predates that schema and is used only as labelled report context.
    Authenticate the original bytes instead; never rewrite/re-lint the receipt
    or its campaign-authoring brief, and never infer a current serving state.
    """
    raw = A3.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != '257339ab71e99ebbf845b56b6e1414d2baef8ec9ee7eb014eaf555f5cff8ce94':
        raise ValueError('a3_historical_receipt_drift')
    a3 = json.loads(raw)
    return {'path': str(A3), 'sha256': digest,
            'captured_at_utc': a3['closed_at_utc'],
            'all_campaigns_configured_paused': a3['all_campaigns_configured_paused'],
            'spend_authorized': a3['spend_authorized'],
            'destination_status': a3['destination_status'],
            'source_status': a3['status'],
            'evidence_kind': 'PINNED_HISTORICAL_OBSERVATION_NOT_AUTHORITY'}


def collect(now, modules):
    a3 = read_a3_observation()
    m = modules['monitor']
    ws = windows(now)
    report = {'schema': 'glp.a4.capture.v1', 'captured_at_utc': now.isoformat(), 'windows': ws,
              'source_mode': 'LIVE_READ_ONLY', 'weekly': {}, 'daily': {}}
    tasks = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for platform, fn in [('google', google_week), ('meta', meta_week), ('microsoft', microsoft_week), ('posthog', posthog_week)]:
            # Each platform client runs its two weeks serially (avoid refresh/concurrency races).
            tasks[platform] = pool.submit(lambda f=fn: {key: guarded(f, m, w) for key, w in ws.items()})
        for platform, future in tasks.items():
            report['weekly'][platform] = future.result()
    # Do not call monitor.run/main, go probes, postbacks, notifications or persistence.
    for platform, fn in [('google_ads', m.collect_google), ('meta_ads', m.collect_meta),
                         ('microsoft_advertising', m.collect_microsoft), ('posthog', m.collect_posthog)]:
        report['daily'][platform] = guarded(fn)
    report['a3_evidence'] = a3
    report['effects'] = {'ad_object_writes': 0, 'sends': 0, 'events_emitted': 0, 'scheduler_changes': 0}
    return report


def summarize(source, economics):
    rows = source.get('rows')
    if source.get('status') != 'PASS' or not rows:
        return {'status': 'UNKNOWN', 'spend': None, 'clicks': None, 'impressions': None, 'ctr': None, 'cpc': None}
    totals = {}
    for key in ('spend', 'clicks', 'impressions'):
        values = [finite(r.get(key)) for r in rows]
        totals[key] = sum(values) if all(v is not None for v in values) else None
    return {'status': 'MEASURED_RETURNED_ROWS', **totals,
            'ctr': economics.ratio(totals['clicks'], totals['impressions']),
            'cpc': economics.ratio(totals['spend'], totals['clicks'])}


def creative_review(current, previous, economics):
    """Descriptive diagnostics, not significance, sale attribution or rotation authority."""
    if current.get('status') != 'PASS' or previous.get('status') != 'PASS':
        return []
    if current.get('granularity') == 'campaign_not_creative':
        return []
    prior = {r['object_id']: r for r in previous.get('rows', [])}
    reviews = []
    for row in current.get('rows', []):
        old = prior.get(row['object_id'], {})
        current_ctr = economics.ratio(row.get('clicks'), row.get('impressions'))
        prior_ctr = economics.ratio(old.get('clicks'), old.get('impressions'))
        if current_ctr is not None and prior_ctr is not None and current_ctr < prior_ctr:
            reviews.append({'object_id': row['object_id'], 'current_ctr': current_ctr, 'prior_ctr': prior_ctr,
                            'action': 'DIAGNOSE_ONLY', 'apply': False,
                            'reason': 'Observed CTR decline; audience, placement, sample and mature sales evidence required before rotation'})
    return reviews


def analyze(capture, modules, now, previous=None):
    e, m = modules['economics'], modules['monitor']
    age = (now - datetime.fromisoformat(capture['captured_at_utc'])).total_seconds()
    fresh = 0 <= age <= 86400 and capture['windows'] == windows(now)
    weekly = {}
    for p in PLATFORMS:
        summaries = {w: summarize(capture['weekly'][p][w], e) for w in ('current', 'previous')}
        # Preserve WS-G mature-cohort controls. This source set has no network proof.
        rec = e.recommendation({'gates': {'destination': None, 'policy': None,
            'program_permission': None, 'privacy': None, 'risk_authority': False},
            'coverage': 'UNKNOWN', 'mature': False})
        deltas = {k: summaries['current'][k] - summaries['previous'][k]
                  if fresh and all(summaries[w][k] is not None for w in summaries) else None
                  for k in ('spend', 'clicks', 'impressions')}
        weekly[p] = {'metrics': summaries, 'observed_deltas_only': deltas,
                     'creative_diagnostics': creative_review(capture['weekly'][p]['current'], capture['weekly'][p]['previous'], e) if fresh else [],
                     'economic_comparison': 'UNKNOWN_INCOMPLETE_IMMATURE_COHORT',
                     'recommendation': rec, 'approved_sale_cpa': None, 'retained_roas': None}
    daily = capture['daily']
    # Local fallback cannot consume or change the repaired monitor's production cursor.
    m.LATEST = HERE / 'uninstalled-no-baseline.json'
    stop = m.build_stop_loss({'platforms': {k: daily.get(k, {}) for k in
                            ('google_ads', 'meta_ads', 'microsoft_advertising')},
                            'revenue_chain': {'posthog': daily.get('posthog', {})},
                            'previous_stop_loss': previous or {}})
    active = {'google': daily.get('google_ads', {}).get('configured_enabled_campaigns'),
              'meta': daily.get('meta_ads', {}).get('requested', {}).get('configured_active_campaigns'),
              'microsoft': daily.get('microsoft_advertising', {}).get('configured_enabled_campaigns')}
    pause = [p for p, n in active.items() if finite(n) is not None and n > 0]
    return {'schema': 'glp.a4.analysis.v1', 'fresh': fresh, 'source_age_seconds': age,
            'weekly': weekly, 'daily_stop_loss': stop, 'active_campaign_counts': active,
            'pause_review': pause, 'apply': False, 'operations': [], 'scale': 'NONE_GATE_HELD',
            'creative_rotation': 'NONE: no qualified ad-level delivery or mature sales evidence; retain A3 assets PAUSED',
            'lp_hypotheses': [
                {'hypothesis': 'Clarify independent comparison and commission disclosure before the quiz CTA',
                 'measure': 'Consented non-TEST joined LP-to-quiz-start rate after exact release acceptance', 'status': 'STAGED_NOT_TESTED'},
                {'hypothesis': 'Make reported program terms and next steps clearer without altering quiz logic',
                 'measure': 'Consented non-TEST joined quiz completion and approved-sale cohort after measurement acceptance', 'status': 'STAGED_NOT_TESTED'}],
            'missing_artifacts': ['D2 exact paid-LP release and consent/egress acceptance',
                'Account/destination classification and program-channel permissions',
                'Complete non-TEST network approval/reversal cohort, attribution and margin bounds',
                'Operator spend decision; A3 proposal is not approval',
                'Ad-level Microsoft report and statistically qualified creative comparison',
                'Natural scheduler execution and exact Discord target readback']}


def card(capture, analysis, formatter, artifact):
    def value(v):
        return 'UNKNOWN' if v is None else f'{v:g}'
    rows = [formatter.kvline((p.title() + ' A3 week spend USD', value(analysis['weekly'][p]['metrics']['current']['spend'])),
                             ('clicks', value(analysis['weekly'][p]['metrics']['current']['clicks']))) for p in PLATFORMS]
    active = analysis['active_campaign_counts']
    state = 'PAUSED' if analysis['fresh'] and all(v == 0 for v in active.values()) else 'UNKNOWN / pause review required'
    text = formatter.render(
        formatter.title('report', 'GLP Weekly Ads', 'report-only; scale remains gate-held'),
        formatter.kvline(('campaign serving state', state), ('evidence freshness', 'current' if analysis['fresh'] else 'STALE')),
        *rows,
        formatter.kvline(('approved-sale CPA', 'UNKNOWN'), ('retained ROAS', 'UNKNOWN')),
        'Rotate: **none recommended**; keep A3 creatives PAUSED. LP hypotheses remain staged.',
        'PostHog: **observed aggregates only**; REAL consented funnel remains UNKNOWN.',
        formatter.meaning(('Pause review: ' + ', '.join(analysis['pause_review']) + '. ' if analysis['pause_review'] else '') +
                          'Keep spend held; resolve destination, classification and measurement. Spend decision stays with the operator.'),
        formatter.subtext('report-only / delivery not proven; report: ' + str(artifact)), output_class='report')
    if formatter.lint(text, 'report')['status'] != 'pass':
        raise ValueError('card_lint_failed')
    return text


def due(now):
    local = now.astimezone(PT)
    return local.weekday() == 0 and local.hour == 9 and local.minute == 30


def reserve_week(path, week, digest):
    """Reserve before stdout; ambiguous delivery is never automatically retried."""
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {}
        if week in state:
            return False
        state[week] = {'status': 'EMISSION_RESERVED_DELIVERY_UNVERIFIED', 'card_sha256': digest}
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(state, indent=2) + '\n')
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        return True


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    os.chmod(path, 0o600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['manual', 'daily', 'weekly'], default='manual')
    parser.add_argument('--capture', type=Path, help='Replay saved real capture, never imply a fresh source read')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, help='A4-owned stop-loss baseline, never the monitor cursor')
    args = parser.parse_args()
    args.output = args.output.resolve()
    if not args.output.is_relative_to(HERE):
        parser.error('output_must_remain_inside_A4')
    if args.baseline and not args.baseline.resolve().is_relative_to(HERE / 'runtime'):
        parser.error('baseline_must_remain_inside_A4_runtime')
    now = datetime.now(timezone.utc)
    scheduled_slot = due(now)
    off_schedule_manual = args.mode == 'weekly' and not scheduled_slot
    # Registered run-now still observes providers and writes the full report.
    # Off-slot acceptance never reserves/emits the scheduled card or resets
    # the daily stop-loss baseline; scheduler remains the sole delivery owner.
    # Pin verification is independent of monitor scheduler adoption, and fails closed.
    modules = dependencies()
    args.output.mkdir(parents=True, exist_ok=True)
    capture = json.loads(args.capture.read_text()) if args.capture else collect(now, modules)
    previous = json.loads(args.baseline.read_text()) if args.baseline and args.baseline.exists() else None
    analysis = analyze(capture, modules, now, previous)
    text = card(capture, analysis, modules['formatter'], args.output / 'ANALYSIS.json')
    save(args.output / 'CAPTURE.json', capture)
    save(args.output / 'ANALYSIS.json', analysis)
    daily_text = '\n'.join(['# A4 daily audit (report-only)',
        'Account-wide spend/policy guard; weekly performance is limited to the A3 campaign cohort.',
        'Fresh evidence: ' + str(analysis['fresh']),
        'Pause-review platforms: ' + (', '.join(analysis['pause_review']) or 'none observed'),
        'Campaign counts: ' + json.dumps(analysis['active_campaign_counts']),
        *[r['code'] + ': triggered=' + str(r['triggered']) + '; ' + str(r.get('evaluation'))
          for r in analysis['daily_stop_loss']['rules']],
        'Unknown is not zero; no platform changes executed. See ANALYSIS.json for full rule inputs.',
        'Policy-change baseline: ' + ('prior A4 receipt loaded' if previous else 'unavailable; no new-issue absence claimed')])
    (args.output / 'DAILY-AUDIT.md').write_text(daily_text + '\n')
    (args.output / 'WEEKLY-CARD.md').write_text(text + '\n')
    save(args.output / 'CARD-LINT.json', modules['formatter'].lint(text, 'report'))
    save(args.output / 'RUN.json', {'mode': args.mode, 'captured_live': not bool(args.capture),
         'off_schedule_manual': off_schedule_manual,
         'natural_execution_verified': False, 'delivery_verified': False,
         'platform_writes': 0, 'direct_sends': 0, 'finished_at_utc': datetime.now(timezone.utc).isoformat()})
    if args.baseline and args.capture is None and args.mode != 'manual' and not off_schedule_manual and analysis['fresh']:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        save(args.baseline, analysis['daily_stop_loss'])
    emit = args.mode == 'weekly' and args.capture is None and scheduled_slot
    if emit:
        emit = reserve_week(HERE / 'weekly-emissions.json', capture['windows']['current']['start'], hashlib.sha256(text.encode()).hexdigest())
    print(text if emit else '[SILENT]')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
