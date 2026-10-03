#!/usr/bin/env python3
"""Read-only F12 consumer: current receiver ledger + latest closed network report.

Never replays a callback, writes money/Sheets, or promotes missing provenance.
The existing PR160 reconciler owns joins; this module owns fresh input custody.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import types
from datetime import datetime, timezone, timedelta
import urllib.request
import uuid

ROOT = Path('/home/ubuntu/business')
REPORTS = ROOT / 'reports/glp-clinic-scout/affiliate-machine'
ROSTER = ROOT / 'reports/ops/orchestration-restore-20260907/WS-F/NETWORK-REPORTING-ALL/MARKETPLACES.json'
REGISTRY = ROOT / 'ops/glp/reporting-v2/identity-registry.json'
D1_ENDPOINT = 'https://api.cloudflare.com/client/v4/accounts/f860943dff399ffb1db0a28dcd3dadfd/d1/database/bcb91d2d-c3f1-4c55-93e3-070587e86d60/query'
D1_SECRET = Path('/home/ubuntu/.hermes/secrets/cloudflare/glp-observability-d1.env')
# Execute the exact verified bytes, not a second import-time read.
DEPENDENCIES = {
    'glp_network_revenue_v2': 'cbb68056ffe1988ebd3fb6ae2b158f027d1cbdfffdb4b0e79e80ddd1581763e1',
    'glp_affiliate_weekly_reporting': 'ddfabbe7f1a53b65257a803a5a48b460699b4b712548d83731526c1ba1f550e0',
}
MAX_ROWS = 10000
COUNT_SQL = 'SELECT count(*) AS n FROM analytics_revenue_ledger_v1'
EVENT_SQL = """SELECT revenue_event_id,network,network_account_ref,program_id,
network_click_id,cs_click_id,conversion_id,event_type,event_status,currency,
commission_minor,occurred_at,imported_at,raw_row_sha256,
CASE WHEN json_valid(attributes_json) THEN json_object(
'is_test',json_extract(attributes_json,'$.is_test'),
'is_probe',json_extract(attributes_json,'$.is_probe'),
'probe_kind',json_extract(attributes_json,'$.probe_kind')) ELSE NULL END AS attributes_json
FROM analytics_revenue_ledger_v1 ORDER BY revenue_event_id LIMIT 10001"""
CLICK_SQL = """SELECT c.click_token,c.network,c.program_id,c.network_offer_id,
c.network_account_ref,c.is_test,
CASE WHEN c.probe_ref IS NOT NULL AND c.probe_ref<>'' THEN 1 ELSE 0 END AS is_probe
FROM affiliate_click_map c WHERE EXISTS(SELECT 1 FROM analytics_revenue_ledger_v1 l
WHERE l.cs_click_id=c.click_token AND l.network=c.network AND l.program_id=c.program_id)
ORDER BY c.click_token LIMIT 10001"""


class InputError(ValueError):
    """Only fixed nonsecret reason codes may be used here."""


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load_reconciler():
    modules = {}
    for name, expected in DEPENDENCIES.items():
        path = ROOT / 'scripts' / (name + '.py')
        raw = path.read_bytes()
        if sha(raw) != expected:
            raise InputError('RECONCILER_SOURCE_DRIFT')
        module = types.ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        exec(compile(raw, str(path), 'exec'), module.__dict__)
        modules[name] = module
    return modules['glp_affiliate_weekly_reporting'], modules['glp_network_revenue_v2'].TEST_ID


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InputError('D1_REDIRECT_REFUSED')


class ReceiverReader:
    def __init__(self):
        values = {}
        for line in D1_SECRET.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            values[key.removeprefix('export ').strip()] = value.strip().strip('\"\'')
        self.secret = values.get('CLOUDFLARE_API_TOKEN') or values.get('CF_API_TOKEN')
        if not self.secret:
            raise InputError('D1_CREDENTIAL_UNAVAILABLE')
        self.opener = urllib.request.build_opener(NoRedirect())
        self.receipts = []

    def query(self, sql):
        if sql not in (COUNT_SQL, EVENT_SQL, CLICK_SQL):
            raise InputError('NON_ALLOWLISTED_READ_REFUSED')
        request = urllib.request.Request(D1_ENDPOINT,
            data=json.dumps({'sql': sql, 'params': []}).encode(), method='POST',
            headers={'Authorization': 'Bearer ' + self.secret, 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=30) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise InputError('D1_RESPONSE_BOUND_EXCEEDED')
            doc = json.loads(raw)
        boxes = doc.get('result')
        if (doc.get('success') is not True or not isinstance(boxes, list) or len(boxes) != 1
                or boxes[0].get('success') is not True or not isinstance(boxes[0].get('results'), list)):
            raise InputError('D1_READ_FAILED')
        rows = boxes[0]['results']
        self.receipts.append({'sql_sha256': sha(sql.encode()), 'response_sha256': sha(raw),
                              'returned_rows': len(rows), 'http_status': 200})
        return rows


def capture(reader, out, test_id):
    started = datetime.now(timezone.utc).isoformat()
    before = reader.query(COUNT_SQL)[0]['n']
    if type(before) is not int or not 0 <= before <= MAX_ROWS:
        raise InputError('RECEIVER_POPULATION_BOUND_EXCEEDED')
    events = reader.query(EVENT_SQL)
    clicks = reader.query(CLICK_SQL)
    after = reader.query(COUNT_SQL)[0]['n']
    if before != after or after != len(events) or len(clicks) > MAX_ROWS:
        raise InputError('RECEIVER_POPULATION_CHANGED_OR_TRUNCATED')
    retained_count = len(events)
    events = [e for e in events if not any('fella' in str(e.get(k, '')).casefold()
                                         for k in ('network', 'program_id'))]
    for event in events:
        if event.get('conversion_id') == test_id or event.get('cs_click_id') == test_id:
            event['classification'] = 'TEST'
        # No inferred REAL/account/tenant/program mapping from untyped callbacks.
    return {'schema': 'glp-f12-reconciliation-input-v1', 'evidence_ref': str(out / 'receiver-input.json'),
            'captured_at': started, 'completed_at': datetime.now(timezone.utc).isoformat(),
            'complete': False, 'receiver_row_count': retained_count, 'fella_excluded_count': retained_count - len(events), 'non_atomic_observation': True,
            'population_scope': 'CURRENT_D1_RECEIVER_READ_NOT_COMPLETE_FINANCIAL_POPULATION',
            'source': 'analytics_revenue_ledger_v1: registered n8n receiver and Pages receiver',
            'read_receipts': reader.receipts, 'conversions': events, 'clicks': clicks, 'financial_records': []}


def weekly_input(report_root, now):
    paths = sorted((p for p in report_root.glob('*/RESULT.json')
                    if re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{12}', p.parent.name)), reverse=True)
    if not paths:
        raise InputError('WEEKLY_REPORT_MISSING')
    path = paths[0]
    if path.stat().st_size > 32 * 1024 * 1024:
        raise InputError('WEEKLY_REPORT_BOUND_EXCEEDED')
    raw = path.read_bytes()
    doc = json.loads(raw)
    captured = datetime.fromisoformat(doc['captured_at'])
    completed = datetime.fromisoformat(doc['completed_at'])
    if not captured <= completed <= now or now - captured > timedelta(days=8):
        raise InputError('WEEKLY_REPORT_STALE_OR_CLOCK_INVALID')
    if (doc.get('schema') != 'glp-affiliate-weekly-report-v1'
            or doc.get('invocation') != 'MANUAL_READ_ONLY' or not doc.get('source_pins_sha256')
            or not isinstance(doc.get('observations'), list)):
        raise InputError('WEEKLY_REPORT_NOT_COMPLETED_LIVE_READ')
    return doc, {'path': str(path), 'sha256': sha(raw), 'captured_at': doc['captured_at'],
                 'window': doc['window'], 'freshness': 'LATEST_RETAINED_CLOSED_REPORT_NOT_CURRENT_EVENT_WINDOW'}


def network_rows(names, observations, snapshot, reconciled, reporter):
    """Keep each period separate: overlapping week/month/lookback are not additive."""
    rows = []
    for name in sorted(names):
        obs = [o for o in observations if o.get('network') == name]
        events = [e for e in (snapshot or {}).get('conversions', []) if e.get('network') == name]
        event_ids = {e.get('revenue_event_id') for e in events}
        joined = [r for r in reconciled['rows'] if r['conversion_event_id'] in event_ids]
        rows.append({'network': name, 'status': 'PARTIAL' if any(r['earnings_state'] == 'JOINED' for r in joined) else 'UNKNOWN',
                     'receiver_event_count': len(events) if snapshot is not None else None,
                     'commission_minor': None, 'cash_minor': None,
                     'gaps': sorted(set(g for r in joined for g in r['gaps']) |
                                    ({'NO_ELIGIBLE_CONVERSION_LINEAGE'} if not joined else set()) |
                                    ({'NETWORK_REPORT_UNAVAILABLE'} if not obs else set())),
                     'periods': [{k: o.get(k) for k in ('period_purpose', 'start_inclusive', 'end_exclusive',
                                  'status', 'coverage', 'record_count', 'reason', 'account_binding', 'cash_minor', 'commission_minor')}
                                 | {'observation_sha256': reporter.digest(o)} for o in obs],
                     'reconciliation_rows': joined})
    return rows


def main():
    os.umask(0o077)
    reporter, test_id = load_reconciler()
    root = REPORTS / 'conversion-reconciliation'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        now = datetime.now(timezone.utc)
        out = root / (now.strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12])
        out.mkdir(mode=0o700)
        roster = json.loads(ROSTER.read_bytes())
        registry = json.loads(REGISTRY.read_bytes())
        if registry.get('schema') != 'reporting-identity-registry-v1':
            raise InputError('IDENTITY_REGISTRY_SCHEMA_INVALID')
        names = {m['marketplace'] for m in roster['marketplaces']}
        if len(names) != roster['marketplace_count']:
            raise InputError('ROSTER_COUNT_INVALID')
        gaps, snapshot, observations, weekly_ref = [], None, [], None
        try:
            snapshot = capture(ReceiverReader(), out, test_id)
            reporter.save(out / 'receiver-input.json', snapshot)
        except Exception as exc:
            gaps.append({'source': 'receiver', 'state': 'UNKNOWN', 'error_class': type(exc).__name__, 'reason': str(exc) if isinstance(exc, InputError) else 'SAFE_INPUT_ERROR'})
        try:
            weekly, weekly_ref = weekly_input(REPORTS / 'weekly-reporting', now)
            if weekly.get('roster_sha256') != reporter.digest(roster):
                raise InputError('WEEKLY_ROSTER_MISMATCH')
            observations = weekly['observations']
            reporter.save(out / 'network-input.json', weekly)
        except Exception as exc:
            gaps.append({'source': 'network_report', 'state': 'UNKNOWN', 'error_class': type(exc).__name__, 'reason': str(exc) if isinstance(exc, InputError) else 'SAFE_INPUT_ERROR'})
        reconciliation = reporter.reconcile_conversions(snapshot, observations, registry.get('identities', []))
        # Include unexpected observed namespaces rather than silently dropping them.
        names |= {o['network'] for o in observations}
        names |= {e['network'] for e in (snapshot or {}).get('conversions', []) if e['network'] != '__receiver_selftest__'}
        names = {n for n in names if 'fella' not in n.casefold()}
        rows = network_rows(names, observations, snapshot, reconciliation, reporter)
        result = {'schema': 'glp334-live-reconciliation-v1', 'status': 'PARTIAL' if not gaps else 'UNKNOWN',
                  'run_type': 'REGISTERED_ENTRY_EXECUTION_CLOCK_TYPE_IN_NATIVE_LEDGER',
                  'captured_at': now.isoformat(), 'completed_at': datetime.now(timezone.utc).isoformat(),
                  'network_count': len(rows), 'network_scope': 'FULL_REPORTING_ROSTER_SUPERSET_NOT_PROOF_OF_CURRENT_APPROVAL',
                  'roster_sha256': reporter.digest(roster), 'identity_registry_sha256': reporter.digest(registry),
                  'weekly_report': weekly_ref, 'source_pins': DEPENDENCIES,
                  'coverage_complete': False, 'commission_minor': None, 'cash_minor': None,
                  'gaps': gaps, 'conversion_reconciliation': reconciliation, 'networks': rows,
                  'effects': {'provider_writes': 0, 'sheet_writes': 0, 'tracking_requests': 0, 'callback_replays': 0}}
        reporter.save(out / 'RESULT.json', result)
        print(json.dumps({'status': result['status'], 'networks': len(rows), 'result': str(out / 'RESULT.json')}))
        return 0 if not gaps else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status': 'BLOCKED', 'error_class': type(exc).__name__, 'reason': str(exc) if isinstance(exc, InputError) else 'SAFE_INPUT_ERROR'}))
        raise SystemExit(2)
