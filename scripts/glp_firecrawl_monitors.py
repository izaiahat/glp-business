#!/usr/bin/env python3
"""Daily provider terms/pricing monitor and local evidence-review intake."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit
from firecrawl_client import FirecrawlClient, record_operation

ROOT = Path('/home/ubuntu/business')
STATE = ROOT / 'data/firecrawl/monitor.json'
QUEUE = ROOT / 'data/firecrawl/evidence-review-queue.jsonl'
NAME = 'GLP provider terms and pricing daily'


def targets(contracts):
    urls = []
    for row in contracts['providers']:
        if 'fella' in (str(row.get('display_name')) + str(row.get('official_url'))).lower():
            continue
        for field in ('pricing_and_inclusions', 'cancellation_and_refunds', 'eligibility_and_states'):
            urls.extend(row.get('field_sources', {}).get(field, []))
    urls = list(dict.fromkeys(urls))[:50]
    if not urls or any(urlsplit(u).scheme != 'https' or any(x in u.lower() for x in ('aff_c', 'revoffers', 'awin1', 'fella')) for u in urls):
        raise ValueError('monitor_public_targets_invalid')
    return urls


def intake(detail, queue=QUEUE):
    queue.parent.mkdir(parents=True, exist_ok=True)
    existing = {json.loads(line)['id'] for line in queue.read_text().splitlines()} if queue.exists() else set()
    added = 0
    with queue.open('a') as out:
        for page in detail.get('pages', []):
            identity = detail['id'] + ':' + page['id']
            if page['status'] not in ('new', 'changed', 'removed') or identity in existing:
                continue
            out.write(json.dumps({'id': identity, 'monitor_id': detail['monitorId'], 'check_id': detail['id'],
                'source_url': page['url'], 'detected_status': page['status'], 'diff': page.get('diff'),
                'created_at': page.get('createdAt'), 'status': 'REVIEW_REQUIRED_NOT_ADOPTED',
                'initial_baseline': page['status'] == 'new'}) + '\n')
            added += 1
    queue.chmod(0o600)
    return added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contracts', type=Path, default=ROOT / 'site-system/flagship-v2/candidates/glp34-provider-families-20260912/data/provider-page-contracts.json')
    parser.add_argument('--create', action='store_true')
    parser.add_argument('--run-now', action='store_true')
    args = parser.parse_args()
    client = FirecrawlClient('glp_firecrawl_monitors')
    usage = client.request('team/credit-usage', method='GET')['data']
    used = usage['planCredits'] - usage['remainingCredits']
    if STATE.exists():
        identity = json.loads(STATE.read_text())['id']
    else:
        found = [x for x in client.request('monitor', method='GET')['data'] if x['name'] == NAME and x['status'] != 'deleted']
        if found:
            identity = found[0]['id']
        elif args.create:
            urls = targets(json.loads(args.contracts.read_text()))
            # Reserve 10 credits/page/day for the entire remaining billing period;
            # markdown-only basic monitors are cheaper. No email/webhook sends.
            end = datetime.fromisoformat(usage['billingPeriodEnd'].replace('Z', '+00:00'))
            days = max(1, (end - datetime.now(timezone.utc)).days + 1)
            if used + len(urls) * 10 * days >= 45000:
                raise RuntimeError('monitor_credit_budget_would_exceed_45000')
            created = client.request('monitor', {'name': NAME, 'schedule': {'cron': '0 8 * * *', 'timezone': 'UTC'},
                'targets': [{'type': 'scrape', 'urls': urls, 'scrapeOptions': {'formats': ['markdown'], 'onlyMainContent': True, 'proxy': 'basic'}}],
                'judgeEnabled': False, 'retentionDays': 30})['data']
            identity = created['id']
        else:
            raise RuntimeError('monitor_not_created')
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({'id': identity, 'name': NAME}) + '\n'); STATE.chmod(0o600)
    monitor = client.request('monitor/' + identity, method='GET')['data']
    url_count = sum(len(t.get('urls') or [t.get('url')]) for t in monitor['targets'])
    end = datetime.fromisoformat(usage['billingPeriodEnd'].replace('Z', '+00:00'))
    days = max(1, (end - datetime.now(timezone.utc)).days + 1)
    if used + url_count * 10 * days >= 45000:
        client.request('monitor/' + identity, {'status': 'paused'}, method='PATCH')
        print(json.dumps({'status': 'PAUSED_CREDIT_CAP', 'used': used})); return 0
    if args.run_now and not monitor.get('currentCheckId'):
        client.request('monitor/' + identity + '/run', {})
    checks = client.request('monitor/' + identity + '/checks?limit=30', method='GET')['data']
    added = 0
    for check in checks:
        if check['status'] not in ('completed', 'partial'):
            continue
        detail = client.request('monitor/' + identity + '/checks/' + check['id'] + '?limit=100', method='GET')['data']
        added += intake(detail)
        record_operation('firecrawl.monitor.check', '/v2/monitor/check', {'limit': url_count},
            payload={'id': check['id'], 'creditsUsed': check.get('actualCredits')}, outcome=check['status'],
            operation_id='monitor-check:' + check['id'])
    print(json.dumps({'status': monitor['status'], 'monitor_id': identity, 'urls': url_count,
        'period_used': used, 'review_rows_added': added, 'queue': str(QUEUE),
        'checks': [{'id': x['id'], 'status': x['status'], 'summary': x.get('summary')} for x in checks]}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
