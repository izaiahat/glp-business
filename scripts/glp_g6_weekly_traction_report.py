#!/usr/bin/env python3
"""Build one honest GLP directory traction report from current durable receipts.

Traction inputs are the newest completed Sunday Weekly Strategy run (its bound
GSC 28d, GA4, Cogny and Bing snapshots) and the current analytics route matrix.
Each input's date is recorded; an input older than eight days is refused,
never silently reused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, '/home/ubuntu/business/ops')
from discord_fmt import kvline, lint, meaning, render, subtext, title

B = Path('/home/ubuntu/business')
ROOT = B / 'reports/glp-directory-flywheel-2026-07-07'
MANIFEST = ROOT / 'approved-programs-manifest.json'
LEDGER = ROOT / 'affiliate-application-ledger.json'
OUTPUT_DIR = ROOT / 'weekly-traction-reports'
# Written by scripts/glp_weekly_strategy_report.py (Sunday Weekly Strategy).
WEEKLY_ROOT = B / 'reports/glp-clinic-scout/weekly-strategy'
# Written by systems/analytics-brain (route-instrumentation-matrix.json).
ROUTE_MATRIX = B / 'reports/glp-clinic-scout/analytics/current/route-instrumentation-matrix.json'
MAX_INPUT_AGE = timedelta(days=8)
MAX_CLOCK_SKEW = timedelta(hours=1)


class InputRefused(Exception):
    """A traction input is missing, unbound or older than MAX_INPUT_AGE."""


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def read_bound(binding: dict, name: str) -> dict:
    try:
        raw = Path(binding['path']).read_bytes()
    except (OSError, KeyError, TypeError) as exc:
        raise InputRefused(f'{name}: bound input unreadable ({type(exc).__name__})') from exc
    if hashlib.sha256(raw).hexdigest() != binding.get('sha256'):
        raise InputRefused(f'{name}: bound input hash changed at {binding["path"]}')
    return json.loads(raw)


def parse_utc(value, name: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError as exc:
        raise InputRefused(f'{name}: timestamp missing or invalid ({value!r})') from exc
    if stamp.tzinfo is None:
        raise InputRefused(f'{name}: timestamp has no timezone ({value!r})')
    return stamp


def latest_weekly_run(weekly_root: Path) -> tuple[Path, dict, datetime]:
    """Newest completed weekly review run (REPORT.json written) by build time."""
    runs = []
    for report in weekly_root.glob('run-*/REPORT.json'):
        manifest_path = report.parent / 'collection/MODULE-INPUTS.json'
        try:
            manifest = load(manifest_path)
            built = parse_utc(manifest['built_at_utc'], str(manifest_path))
        except (OSError, ValueError, KeyError, TypeError, InputRefused):
            continue
        runs.append((built, report.parent, manifest))
    if not runs:
        raise InputRefused(f'weekly review: no completed run under {weekly_root}')
    built, run, manifest = max(runs, key=lambda row: row[0])
    return run, manifest, built


def select_inputs(now: datetime, weekly_root: Path = None, route_matrix: Path = None) -> dict:
    weekly_root = WEEKLY_ROOT if weekly_root is None else weekly_root
    route_matrix = ROUTE_MATRIX if route_matrix is None else route_matrix
    run, manifest, built = latest_weekly_run(weekly_root)
    bindings = manifest.get('inputs', {})
    for key in ('gsc-28', 'ga4', 'cogny-weekly-evidence'):
        if key not in bindings:
            raise InputRefused(f'weekly review {run.name}: input {key} not bound')
    docs = {key: read_bound(bindings[key], key) for key in ('gsc-28', 'ga4', 'cogny-weekly-evidence')}
    cogny = docs['cogny-weekly-evidence']
    for key in ('bing-page', 'bing-query'):
        docs[key] = read_bound(cogny.get('snapshots', {}).get(key, {}), key)
    try:
        docs['route-matrix'] = load(route_matrix)
    except (OSError, ValueError) as exc:
        raise InputRefused(f'route matrix: unreadable at {route_matrix} ({type(exc).__name__})') from exc
    stamps = {
        'weekly-review': built,
        'gsc-28': parse_utc(docs['gsc-28'].get('captured_at_utc'), 'gsc-28'),
        'ga4': parse_utc(docs['ga4'].get('captured_at_utc'), 'ga4'),
        # Cogny evidence carries only its report date; read it as that day's start.
        'cogny-weekly-evidence': parse_utc(f"{cogny.get('report_date')}T00:00:00+00:00", 'cogny-weekly-evidence'),
        'bing-page': parse_utc(docs['bing-page'].get('captured_at_utc'), 'bing-page'),
        'bing-query': parse_utc(docs['bing-query'].get('captured_at_utc'), 'bing-query'),
        'route-matrix': parse_utc(docs['route-matrix'].get('generated_at_utc'), 'route-matrix'),
    }
    paths = {'weekly-review': str(run), 'route-matrix': str(route_matrix)}
    paths.update({key: bindings[key]['path'] for key in ('gsc-28', 'ga4', 'cogny-weekly-evidence')})
    paths.update({key: cogny['snapshots'][key]['path'] for key in ('bing-page', 'bing-query')})
    freshness = {}
    for key, stamp in stamps.items():
        age = now - stamp
        if age > MAX_INPUT_AGE:
            raise InputRefused(f'{key}: input dated {stamp.date()} is {age.days} days old '
                               f'(limit {MAX_INPUT_AGE.days}) at {paths[key]}')
        if age < -MAX_CLOCK_SKEW:
            raise InputRefused(f'{key}: input dated {stamp.isoformat()} is in the future at {paths[key]}')
        freshness[key] = {'date': stamp.date().isoformat(), 'observed_at_utc': stamp.isoformat(),
                          'age_hours': round(age.total_seconds() / 3600, 1), 'path': paths[key]}
    return {'docs': docs, 'freshness': freshness}


def complete_rows(doc: dict):
    result = doc.get('result', {})
    if result.get('ok') is not True or result.get('complete') is not True or not isinstance(result.get('rows'), list):
        return None  # Unknown is not zero.
    return result['rows']


def build(as_of: str, now: datetime = None) -> dict:
    now = now or datetime.now(timezone.utc)
    selected = select_inputs(now)
    docs = selected['docs']
    manifest = load(MANIFEST)
    ledger = load(LEDGER)
    programs = manifest.get('programs', [])
    applications = ledger.get('applications', [])
    approved = [row for row in applications if str(row.get('status', '')).startswith('approved')]
    legal_review = [row for row in applications if row.get('status') == 'legal_review_pending']
    live_links = [row for row in programs if row.get('live_link_allowed')]
    gsc_rows = complete_rows(docs['gsc-28'])
    ga4_rows = complete_rows(docs['ga4'])
    bing_page, bing_query = complete_rows(docs['bing-page']), complete_rows(docs['bing-query'])
    routes = docs['route-matrix'].get('routes', [])
    cogny_queries = docs['cogny-weekly-evidence'].get('queries', [])
    report = {
        'schema': 'glp_directory_weekly_traction_v2',
        'generated_at_utc': now.isoformat(),
        'as_of': as_of,
        'source_freshness': selected['freshness'],
        'max_input_age_days': MAX_INPUT_AGE.days,
        'gsc_window': {k: docs['gsc-28'].get('context', {}).get(k) for k in ('start_date', 'end_date')},
        'ga4_window': {k: docs['ga4'].get('context', {}).get(k) for k in ('start_date', 'end_date')},
        'metrics': {
            'sitemap_routes': docs['route-matrix'].get('sitemap_route_count'),
            'sitemap_routes_live_200': sum(r.get('in_sitemap') is True and r.get('http_status') == 200 for r in routes),
            'gsc_clicks_28d': None if gsc_rows is None else sum(float(r.get('clicks') or 0) for r in gsc_rows),
            'gsc_impressions_28d': None if gsc_rows is None else sum(float(r.get('impressions') or 0) for r in gsc_rows),
            'ga4_neutral_route_page_views_28d': None if ga4_rows is None else sum(r['metrics'][0] for r in ga4_rows),
            'bing_page_rows': None if bing_page is None else len(bing_page),
            'bing_query_rows': None if bing_query is None else len(bing_query),
            'cogny_queries': len(cogny_queries),
            'cogny_queries_returned': sum(q.get('state') == 'RETURNED' for q in cogny_queries),
            'approved_programs': len(approved),
            'legal_review_pending_programs': len(legal_review),
            'live_affiliate_links': len(live_links),
            'affiliate_revenue_usd': 0,
            'compounded_live_link_share': manifest.get('compounded_seller_policy', {}).get('current_live_link_share', 0.0),
        },
        'not_reported': {'url_inspection_samples': 'not collected by the weekly review; no stale sample reused'},
        'program_state': {
            'gobymeds': next((row.get('status') for row in applications if row.get('program_id') == 'gobymeds'), 'missing'),
            'telewellnessmd': next((row.get('status') for row in applications if row.get('program_id') == 'telewellnessmd'), 'missing'),
        },
        'day_90_gate': 'HOLD — no affiliate conversion and fewer than 1,000 measured monthly organic visits; do not scale from sample indexation alone.',
        'external_sends': 0,
        'secret_printed': False,
    }
    return report


def shown(value) -> str:
    if value is None:
        return 'UNKNOWN'
    return f'{value:g}' if isinstance(value, float) else str(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--as-of', default=date.today().isoformat())
    args = parser.parse_args()
    try:
        report = build(args.as_of)
    except InputRefused as exc:
        print(f'G6_INPUT_REFUSED: {exc}; no traction report written', file=sys.stderr)
        return 2
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / f'{args.as_of}.json'
    out.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    m, f = report['metrics'], report['source_freshness']
    message = render(
        title('report', 'GLP G-6', f"traction remains HOLD as of {args.as_of}"),
        kvline(
            ('live routes', f"{m['sitemap_routes_live_200']}/{shown(m['sitemap_routes'])}"),
            ('GSC 28d', f"{shown(m['gsc_clicks_28d'])} clicks / {shown(m['gsc_impressions_28d'])} impressions"),
        ),
        kvline(
            ('GA4 28d', shown(m['ga4_neutral_route_page_views_28d'])),
            ('Bing rows', f"{shown(m['bing_page_rows'])} pages / {shown(m['bing_query_rows'])} queries"),
            ('approved', m['approved_programs']),
            ('live links', m['live_affiliate_links']),
            ('revenue', '$0'),
        ),
        kvline(
            ('weekly review', f['weekly-review']['date']),
            ('route matrix', f['route-matrix']['date']),
        ),
        meaning('hold scale-up; continue approval-gated onboarding and measured indexation'),
        subtext(f"report: {out}"),
        output_class='report',
    )
    check = lint(message, 'report')
    if check['status'] != 'pass':
        raise RuntimeError(f'Discord house-style lint failed: {check["errors"]}')
    print(message)
    return 0 if report['external_sends'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
