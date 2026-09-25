#!/usr/bin/env python3
"""Build one honest GLP directory traction report from current durable receipts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/ubuntu/business/ops')
from discord_fmt import kvline, lint, meaning, render, subtext, title

ROOT = Path('/home/ubuntu/business/reports/glp-directory-flywheel-2026-07-07')
PAGE_AUDIT = ROOT / 'page-audit.json'
GSC = ROOT / 'gsc-indexation-readback.json'
MANIFEST = ROOT / 'approved-programs-manifest.json'
LEDGER = ROOT / 'affiliate-application-ledger.json'
OUTPUT_DIR = ROOT / 'weekly-traction-reports'


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def build(as_of: str) -> dict:
    page_audit = load(PAGE_AUDIT)
    gsc = load(GSC)
    manifest = load(MANIFEST)
    ledger = load(LEDGER)
    programs = manifest.get('programs', [])
    applications = ledger.get('applications', [])
    approved = [row for row in applications if str(row.get('status', '')).startswith('approved')]
    legal_review = [row for row in applications if row.get('status') == 'legal_review_pending']
    live_links = [row for row in programs if row.get('live_link_allowed')]
    inspection = gsc.get('url_inspection_samples', [])
    indexed = sum(row.get('verdict') == 'PASS' for row in inspection)
    report = {
        'schema': 'glp_directory_weekly_traction_v1',
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'as_of': as_of,
        'source_freshness': {
            'page_audit_generated_at_utc': page_audit.get('generated_at_utc'),
            'gsc_generated_at_utc': gsc.get('generated_at_utc'),
        },
        'metrics': {
            'pages_live_batch0': len(page_audit.get('pages', [])),
            'indexed_url_inspection_samples': indexed,
            'gsc_batch0_clicks_28d': gsc.get('search_analytics', {}).get('batch0_clicks'),
            'gsc_batch0_impressions_28d': gsc.get('search_analytics', {}).get('batch0_impressions'),
            'ga4_batch0_page_views_28d': gsc.get('ga4', {}).get('batch0_page_views_28d'),
            'approved_programs': len(approved),
            'legal_review_pending_programs': len(legal_review),
            'live_affiliate_links': len(live_links),
            'affiliate_revenue_usd': 0,
            'compounded_live_link_share': manifest.get('compounded_seller_policy', {}).get('current_live_link_share', 0.0),
        },
        'program_state': {
            'gobymeds': next((row.get('status') for row in applications if row.get('program_id') == 'gobymeds'), 'missing'),
            'telewellnessmd': next((row.get('status') for row in applications if row.get('program_id') == 'telewellnessmd'), 'missing'),
        },
        'day_90_gate': 'HOLD — no affiliate conversion and fewer than 1,000 measured monthly organic visits; do not scale from sample indexation alone.',
        'external_sends': 0,
        'secret_printed': False,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--as-of', default=date.today().isoformat())
    args = parser.parse_args()
    report = build(args.as_of)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / f'{args.as_of}.json'
    out.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    m = report['metrics']
    message = render(
        title('report', 'GLP G-6', f"traction remains HOLD as of {args.as_of}"),
        kvline(
            ('pages', m['pages_live_batch0']),
            ('indexed samples', m['indexed_url_inspection_samples']),
            ('GSC 28d', f"{m['gsc_batch0_clicks_28d']} clicks / {m['gsc_batch0_impressions_28d']} impressions"),
        ),
        kvline(
            ('GA4 28d', m['ga4_batch0_page_views_28d']),
            ('approved', m['approved_programs']),
            ('live links', m['live_affiliate_links']),
            ('revenue', '$0'),
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
