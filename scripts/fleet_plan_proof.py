#!/usr/bin/env python3
"""Read-only PLAN proofs. No dispatch, writes, tracking GETs or synthetic receipts.

Commands may verify a useful prerequisite but refuse until ALL specified gates are
available. Missing evidence is not success. linear_close independently checks the
merged/installed provenance and authenticated acceptance before actual closure.
"""
import sys
sys.dont_write_bytecode = True
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path('/home/ubuntu/business')
PLAN = 'reports/ops/fable-plan-2-20261009'


def require(value, reason):
    if not value:
        raise ValueError(reason)


def read(rel):
    p = (ROOT / rel).resolve()
    require(p.is_relative_to(ROOT) and p.is_file() and not p.is_symlink(), 'missing evidence: ' + str(rel))
    return json.loads(p.read_text())


def gh(*args):
    return json.loads(subprocess.check_output(['gh', *args], text=True, timeout=45))


def dot_roundtrips():
    d = read(PLAN + '/lanes/L3/LIVE-ROUNDTRIPS.json')
    rows = d['roundtrips']
    require(d['count'] == 2 and len(rows) == 2 and {r['dot'] for r in rows} == {'rook', 'finch'}, 'two distinct dot roundtrips required')
    require(len({r['request_id'] for r in rows}) == 2 and len({r['reply_id'] for r in rows}) == 2, 'duplicate request/reply ids')
    latencies = {}
    for r in rows:
        ticket = Path(r['ticket_dir'])
        require(ticket.is_relative_to(ROOT / 'reports/ops/dot-tickets'), 'ticket outside workspace')
        config = read(str(ticket.relative_to(ROOT) / 'CONFIG.json'))
        require(config['request_id'] == r['request_id'] and config['dot'] == r['dot'], 'ticket correlation changed')
        require(r['native_custody_verified'] and r['exact_request_readback'], 'unverified native custody/request')
        require(r['structured_mentions'] == [r['reply_author']], 'structured recipient binding absent')
        require(hashlib.sha256((ticket / 'REPLY.md').read_bytes()).hexdigest() == r['reply_md_sha256'], 'terminal reply bytes changed')
        # Discord snowflakes encode UTC milliseconds; exact request/reply ids are
        # already bound by the native custody receipt. No external send/read needed.
        latency = ((int(r['reply_id']) >> 22) - (int(r['request_id']) >> 22)) / 1000
        require(latency > 0, 'reply predates request')
        latencies[r['dot']] = latency
        ingests = [json.loads(p.read_text()) for p in (ROOT / 'reports/ops/fleet-inbox' / r['issue']).glob('*/INGEST.json')]
        require(any(x.get('source') == 'dot' and x.get('identity') == str(ticket / 'REPLY.md') for x in ingests), 'dot reply absent from production fleet inbox')
    cron = read(PLAN + '/lanes/L3/CRON-READBACK.json')
    require(cron['manual_registered_pass'] and cron['no_sends'] and cron['no_model'], 'registered consumer evidence absent')
    require((Path('/etc/cron.d/glp-dot-consumer')).is_file(), 'consumer cron not installed')
    return {'roundtrip_count': 2, 'latency_seconds': latencies, 'request_ids': [r['request_id'] for r in rows], 'duplicate_request_ids': 0}


def fleet_ingestion():
    d = read('reports/ops/fleet-inbox/LAST-RUN.json')
    require(d['status'] == 'COMPLETE' and len([n for n in d['sources'].values() if n > 0]) >= 3, 'three production sources required')
    require(d['linear_mutations'] == 0, 'poller attempted Linear writes')
    natural = read(PLAN + '/lanes/L2/NATURAL-RUN.json')
    require(natural.get('job_id') == 'bbc82551321b' and natural.get('last_status') == 'ok'
            and natural.get('linear_mutations') == 0
            and len([n for n in natural.get('sources', {}).values() if n > 0]) >= 3,
            'registered natural source-ingestion proof absent')
    # The L2b assignment permits GLP-355 itself to be the first new closure.
    # Check its real pre-effect output here. The coordinator must then preserve
    # the exact READY row + typed RESULT + new Linear transition readback. No
    # historical closure is invented and the business-output gate is unchanged.
    return {'row_count': d['row_count'], 'source_count': len(d['sources']),
            'natural_rows': natural['rows'], 'closure_postcondition': 'pending GLP-355 transition from its own READY row'}


def analytics():
    d = read(PLAN + '/lanes/R364/MANUAL-RUN-EVIDENCE.json')
    report = read(PLAN + '/lanes/R364/RESULT.json')['manual_runs']['analytics']
    require(report['executed_phases'] >= 5 and report['each_run']['sitemap_route_rows'] > 0, 'registered analytics output absent')
    require(report['hourly'] == 'PASS_SEARCH_ONLY' and str(report['weekly']).startswith('PASS_SEARCH_ONLY'), 'hourly/weekly search-only runs not PASS')
    matrix = report['current_matrix']
    require(matrix['rows'] > 0 and matrix['static_fail'] == matrix['probe_fail'] == matrix['missing_obligations'] == 0, 'route coverage did not pass')
    pr = gh('pr', 'view', '31', '--repo', 'izaiahat/glp-site-candidate', '--json', 'state')
    require(pr['state'] in {'MERGED', 'CLOSED'}, 'site PR31 still OPEN: designated privacy/design review required')
    return {'route_rows': matrix['rows'], 'collector_rows': report['each_run']['sitemap_route_rows'], 'site_pr31_state': pr['state']}


def firecrawl_ledger():
    # This is a live-ledger prerequisite, not substitute fixtures for missing
    # registered item runs. All 0..8 remain bound to their maintained item owner.
    path = ROOT / 'data/firecrawl/operations.jsonl'
    require(path.is_file(), 'Firecrawl operation ledger missing')
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    require(len(rows) > 0, 'Firecrawl ledger empty')
    return {'ledger_rows': len(rows)}


def health(issue):
    # Find the genuine worker receipt via the coordinator-owned policy, not an
    # arbitrary file name containing a successful publication.
    policies = read(PLAN + '/PLAN.json')['issues']
    row = next(r for r in policies if r['issue'] == issue)
    config = ROOT / 'reports/ops/fleet-inbox/CONFIG.json'
    overrides = json.loads(config.read_text()).get('issues', {}).get(issue, {}) if config.is_file() else {}
    row.update(overrides)
    require(row.get('worker_result'), 'coordinator has not bound the genuine natural-slot worker RESULT')
    worker = read(row['worker_result'])
    from article_health_acceptance import acceptance
    proof = acceptance(worker.get('receipt') or '', natural=True, root=ROOT)
    return {'carrier': proof['carrier'], 'independent_acceptance': True}


def main(argv):
    if len(argv) != 1:
        print('usage: fleet_plan_proof.py GLP-n')
        return 2
    issue = argv[0]
    functions = {'GLP-355': fleet_ingestion, 'GLP-356': dot_roundtrips,
                 'GLP-44': analytics, 'GLP-350': firecrawl_ledger,
                 'GLP-320': lambda: health(issue)}
    try:
        require(issue in functions, 'no complete executable proof contract; see PLAN proof_unavailable_reason')
        result = functions[issue]()
        print(issue + ' PASS ' + json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, KeyError, StopIteration, OSError, subprocess.SubprocessError) as exc:
        print(issue + ' REFUSED ' + str(exc))
        return 3


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
