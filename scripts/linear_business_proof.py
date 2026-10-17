#!/usr/bin/env python3
"""Read-only issue business-output proofs; never dispatch, close, send or publish.

A worker's ``business_output`` contains *locators*, not verdicts. Locators name
native producer output, exact message/PR/route IDs and byte hashes. Predicates
below are code, not worker-selected assertions. Missing output refuses (exit 3).
A PASS is an observation, not acceptance, provenance or protected-effect authority;
fleet_ingest must still run the existing independent completion validator.
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import base64
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
from email.utils import parseaddr
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path('/home/ubuntu/business')
HERE = Path(__file__).resolve().parent
PLAN = 'ops/linear/PLAN.json'
FLOOR = 100_000_000_000


class Refused(ValueError):
    pass


def require(value, why):
    if not value:
        raise Refused(why)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def stamp(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(value.tzinfo is not None, 'timezone missing')
    return value.timestamp()


def confined(root, path):
    path = Path(path)
    path = path if path.is_absolute() else root / path
    require(path.is_absolute() and path.is_relative_to(root), 'path outside workspace')
    require(not any(p.is_symlink() for p in [path, *path.parents] if p != root.parent), 'symlink evidence')
    require(path.exists(), 'native output file absent')
    resolved = path.resolve(strict=True)
    require(resolved.is_relative_to(root) and resolved.is_file(), 'not a workspace file')
    return resolved


def read_json(root, path):
    return json.loads(confined(root, path).read_text())


def gh(*args):
    return json.loads(subprocess.check_output(['gh', *args], text=True, timeout=45))


def live_issue(issue):
    # The maintained client performs only the query below, never a transition.
    sys.path.insert(0, str(HERE))
    import linear_orchestrator as lo
    return lo.issue(issue)


def acceptance(description):
    match = re.search(r'^## Acceptance\s*\n+(.*?)(?=\n## |\Z)', description or '', re.S | re.M)
    require(match, 'current Linear Acceptance missing')
    # The old command is explanatory metadata, not the business criterion.
    text = match[1].strip().split(' Read-only proof:', 1)[0].strip()
    # Bind every clause when an authorized owner expands the acceptance (GLP-93).
    return ' '.join(text.split())


def http(route):
    # No redirects or third-party/tracking GETs, including through canonical site.
    require(isinstance(route, str) and re.fullmatch(r'/[A-Za-z0-9_./-]*', route)
            and '..' not in route and not route.startswith('//'), 'noncanonical site route')
    require(not route.startswith(('/go/', '/api/', '/out/', '/redirect/', '/r/')),
            'tracking/dynamic route refused')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise Refused('redirect refused; never follow a tracking/destination URL')
    url = 'https://glpclinicscout.com' + route
    with urllib.request.build_opener(NoRedirect()).open(
            urllib.request.Request(url, headers={'User-Agent': 'GLP-readonly-output-verifier'}), timeout=30) as response:
        require(response.status == 200 and response.url == url, 'live URL/status mismatch')
        body = response.read(8_000_001)
    require(len(body) <= 8_000_000, 'oversized live response')
    return body


def ops_mail():
    import glp_affiliate_inbox_mirror as mirror
    return mirror.gmail_service(next(b for b in mirror.MAILBOXES if b['key'] == 'ops'))


class Proof:
    def __init__(self, issue, row, root=ROOT):
        self.root, self.issue, self.row = root, issue, row
        self.worker = read_json(root, row['worker_result'])
        self.inputs = self.worker.get('business_output') or row.get('business_output') or {}
        require(isinstance(self.inputs, dict), 'business_output must contain named native-output locators')
        self.observed = {}

    def native(self, name, prefix):
        """Read a hash-bound real producer output, never an acceptance/worker claim."""
        spec = self.inputs.get(name)
        if not spec and self.issue == 'GLP-334' and name == 'reconciliation':
            # Maintained producer namespaces sort by UTC stamp, not report mtime.
            base = self.root / 'reports/glp-clinic-scout/affiliate-machine/conversion-reconciliation'
            paths = sorted((q for q in base.glob('*/RESULT.json')
                            if re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{12}', q.parent.name)), reverse=True)
            require(paths, 'registered conversion reconciliation output absent')
            spec = {'path': str(paths[0]), 'sha256': digest(confined(self.root, paths[0]).read_bytes())}
        require(isinstance(spec, dict), 'native output locator absent: ' + name)
        path = confined(self.root, spec['path'])
        relative = path.relative_to(self.root).as_posix()
        require(relative.startswith(prefix.rstrip('/') + '/'), 'unregistered producer output: ' + name)
        require(path.suffix == '.json' and not any(w in path.name.upper() for w in ('ACCEPT', 'REVIEW')), 'review is not business output')
        blob = path.read_bytes()
        require(re.fullmatch('[a-f0-9]{64}', str(spec.get('sha256', ''))) and digest(blob) == spec['sha256'], 'native output bytes changed: ' + name)
        self.observed[name] = {'path': relative, 'sha256': digest(blob)}
        return json.loads(blob)

    def pr(self, repo, number):
        require(repo in ('glp-business', 'glp-site-candidate') and type(number) is int and number > 0, 'invalid PR')
        p = gh('pr', 'view', str(number), '--repo', 'izaiahat/' + repo, '--json',
               'state,headRefOid,mergeCommit,statusCheckRollup')
        require(p['state'] == 'MERGED' and (p.get('mergeCommit') or {}).get('oid'), f'{repo} PR{number} not MERGED')
        checks = p.get('statusCheckRollup') or []
        require(checks and all(c.get('conclusion') == 'SUCCESS' or c.get('state') == 'SUCCESS' for c in checks), 'nonempty green exact-head CI required')
        # Check-runs are read by exact commit, not a mutable branch/check name.
        runs = gh('api', f'repos/izaiahat/{repo}/commits/{p["headRefOid"]}/check-runs', '--paginate')
        require(runs.get('check_runs') and all(r['head_sha'] == p['headRefOid'] and r['conclusion'] == 'success' for r in runs['check_runs']), 'CI not green at exact PR head')
        self.observed[f'{repo}-PR{number}'] = p['headRefOid']
        return p

    def page(self, spec):
        require(isinstance(spec, dict), 'live page locator missing')
        body = http(spec['route'])
        require(digest(body) == spec['sha256'], 'served content differs from registered output')
        text = body.decode('utf-8')
        title = re.search(r'<title[^>]*>(.*?)</title>', text, re.S | re.I)
        require(title and title[1].strip() == spec['title'] and spec['title'], 'live title mismatch')
        self.observed[spec['route']] = {'sha256': digest(body), 'title': title[1]}
        return text

    def cron(self, job):
        data = json.loads(Path('/home/ubuntu/.hermes/cron/jobs.json').read_text())
        rows = [r for r in data['jobs'] if r['id'] == job]
        require(len(rows) == 1, 'registered job identity missing')
        return rows[0]

    def reviews(self, scopes):
        # Independent acceptance is deliberately NOT inferred from a worker JSON
        # ACCEPT. The unchanged fleet/linear_close validator owns this gate. In
        # particular do not call result_lint here: it writes an acceptance cache.
        self.observed['external_acceptance_gates'] = list(scopes)

    def no_effects(self, report):
        effects = report.get('effects') or {}
        require(all(effects.get(k) == 0 for k in ('tracking_requests', 'provider_writes')), 'tracking/provider effects not excluded')

    def sheet(self):
        # Maintained sole-writer transport's read function only, never drain/apply.
        import glp_sponsor_campaign as campaign
        return campaign.read_values()

    def receiver(self):
        # Fixed SELECT-only Cloudflare reader, not the receiver POST/webhook.
        from glp334_live_reconciliation import ReceiverReader, COUNT_SQL, EVENT_SQL, CLICK_SQL, MAX_ROWS
        reader = ReceiverReader()
        before = reader.query(COUNT_SQL)[0]['n']
        require(type(before) is int and 0 <= before <= MAX_ROWS, 'receiver population bound exceeded')
        events, clicks = reader.query(EVENT_SQL), reader.query(CLICK_SQL)
        after = reader.query(COUNT_SQL)[0]['n']
        require(before == after == len(events) and len(clicks) <= MAX_ROWS, 'receiver read changed/truncated')
        self.observed['receiver_rows'] = len(events)
        return events, clicks

    def onboard_live(self):
        import glp_item_b_provider_readback_20260810 as api
        token = api.cloudflare_token()
        base = '/accounts/' + api.ACCOUNT_ID
        _, project = api.cf_json(token, base + '/pages/projects/glp-clinic-scout')
        mode = project['deployment_configs']['production'].get('env_vars', {}).get('NEWSLETTER_CAPTURE_MODE', {})
        mode = mode.get('value') if isinstance(mode, dict) else mode
        worker = base + '/workers/scripts/glp-clinic-scout-onboarding'
        _, settings = api.cf_json(token, worker + '/settings')
        flags = {r['name']: r.get('text') for r in settings['bindings'] if r.get('type') == 'plain_text'}
        _, schedules = api.cf_json(token, worker + '/schedules')
        deliveries = api.d1_query(token, "SELECT message_type,status,sequence_step,resend_email_id FROM email_deliveries WHERE subscriber_id IN (SELECT subscriber_id FROM subscribers WHERE email='izaiah@glpclinicscout.com' AND vertical='glp-clinic-scout')")
        self.observed['owned_onboarding_delivery_rows'] = len(deliveries)
        require(any(r['status'] == 'delivered' and r['resend_email_id'] for r in deliveries),
                'no provider-delivered owned onboarding message in live D1')
        require(mode == 'enabled' and flags.get('ONBOARDING_ENABLED') == 'true'
                and flags.get('ONBOARDING_TEST_ONLY') == 'false' and schedules,
                'approved live capture/onboarding automation not activated')
        return deliveries

    def resend_get(self, message_id):
        import glp_item_b_provider_readback_20260810 as api
        require(re.fullmatch(r'[a-f0-9-]{36}', message_id), 'provider message id invalid')
        _, _, _, data = api.request('GET', 'https://api.resend.com/emails/' + message_id,
            headers={'Authorization':'Bearer ' + api.resend_key(), 'User-Agent':api.USER_AGENT})
        return {'id':data['id'], 'status':data['last_event']}

    def owned_mailbox_message(self, message_id):
        from inbox_control_plane.gmail_client import GmailClient
        registry = read_json(self.root, 'inbox_control_plane/registry.json')
        rows = [r for r in registry['mailboxes'] if r.get('mailbox_id') == 'glp_izaiah_gmail']
        require(len(rows) == 1 and rows[0]['canonical_address'] == 'izaiah@glpclinicscout.com'
                and rows[0]['lifecycle'] == 'active', 'owned recipient mailbox binding invalid')
        client = GmailClient.from_mailbox(rows[0], mode='read')
        client.preflight()
        return client.get_message(message_id, format='raw')


def nuimage(p):
    import glp_sponsor_campaign as campaign
    import glp_sponsor_nuimage_reply as reply
    journal = read_json(p.root, reply.JOURNAL)
    queue = read_json(p.root, reply.QUEUE)
    client = campaign.service('read')
    row = client.get_message(journal['message_id'], format='raw')
    message, body = reply.parsed_body(reply.raw_message(row))
    require('SENT' in row.get('labelIds', []) and row['threadId'] == reply.THREAD,
            'exact reply not SENT in approved thread')
    require(parseaddr(str(message['From']))[1].lower() == campaign.SENDER
            and parseaddr(str(message['To']))[1].lower() == reply.TO
            and digest(body.encode()) == reply.BODY_SHA
            and str(message['In-Reply-To']).strip() == queue['source_rfc_message_id'], 'approved reply identity/body mismatch')
    require(digest(confined(p.root, reply.LANE / 'GLP125-NuImage-reply.txt').read_bytes()) == reply.ARTIFACT_SHA, 'approved proposal/rate-card artifact drift')
    matches = []
    for item in client.get_thread(reply.THREAD).get('messages', []):
        r = client.get_message(item['id'], format='raw')
        m, b = reply.parsed_body(reply.raw_message(r))
        if ('SENT' in r.get('labelIds', []) and parseaddr(str(m['To']))[1].lower() == reply.TO
                and (digest(b.encode()) == reply.BODY_SHA or 'Our proposed sponsor pilot is $750' in b)):
            matches.append(r['id'])
    require(matches == [row['id']], 'duplicate or unbound commercial reply')
    return {'message_id': row['id'], 'thread_id': row['threadId'], 'matching_sent_messages': len(matches), 'new_sends': 0}


def savings(p):
    p.pr('glp-site-candidate', 116)
    p.pr('glp-site-candidate', 123)
    release = p.native('publication', 'reports/glp-clinic-scout')
    require(release['publisher'] == 'host' and release['status'] == 'PASS_PRODUCTION_TERMINAL', 'sole-host terminal publication absent')
    text = p.page(release['article'])
    require('savings' in release['article']['route'] and 'savings' in text.lower(), 'not the corrected savings guide')
    site_map = http('/sitemap.xml').decode()
    require('https://glpclinicscout.com' + release['article']['route'] in site_map, 'published route absent from live sitemap')
    notifier = p.native('notifier', 'reports/glp-clinic-scout')
    require(notifier['submitted_urls'] and all(r['status'] in (200, 202) for r in notifier['submitted_urls'])
            and any(r['url'] == 'https://glpclinicscout.com' + release['article']['route'] for r in notifier['submitted_urls']), 'notifier exact-route readback missing')
    build = p.native('build', 'reports')
    require(build['head_sha'] == release['head_sha'] and build['full_build_rc'] == build['test_rc'] == 0
            and build['baseline_glp361'] == 'PASS', 'exact-head full build/baseline not verified')
    n8n = p.native('n8n_carrier', 'reports/ops')
    require(n8n['workflow_id'] and n8n['active'] is False, 'retired article carrier must remain inactive')
    p.reviews(('source', 'health', 'design'))
    return p.observed


def sponsor_site(p):
    current = p.worker.get('public_resources', {}).get('pr', '') if hasattr(p, 'worker') else ''
    match = re.fullmatch(r'https://github.com/izaiahat/glp-site-candidate/pull/([1-9][0-9]*)', current)
    if match:
        p.pr('glp-site-candidate', int(match[1]))
    output = p.native('site_output', 'reports/glp-clinic-scout')
    p.pr('glp-site-candidate', output['source_pr'])
    pages = output['pages']
    require({r['kind'] for r in pages} >= {'rate_card', 'example_placement'}, 'rate/example output missing')
    for page in pages:
        text = p.page(page)
        require('sponsor' in text.lower() and 'disclos' in text.lower(), 'visible sponsorship disclosure absent')
        if page['kind'] == 'rate_card':
            require(all(s in text for s in ('$750', '$250', '$500')), 'approved prices absent')
    require(output['terms'] == {'initial_usd': 750, 'setup_usd': 250, 'placement_usd': 500, 'affirmative_renewal_usd': 500}, 'approved economics changed')
    require({r['viewport'] for r in output['renders']} >= {'desktop', 'mobile'}
            and all(r['http'] == 200 and r['page_sha256'] in {x['sha256'] for x in pages} for r in output['renders']), 'rendered output binding missing')
    require(output['editorial_ranking_before_sha256'] == output['editorial_ranking_after_sha256']
            and output['holds_before_sha256'] == output['holds_after_sha256'], 'ranking/hold drift')
    p.reviews(('design',))
    return p.observed


def sponsor_outreach(p):
    job = p.cron('3940adcf89f6')
    require(job['enabled'] is True and job['state'] == 'scheduled', 'registered sponsor sender not active')
    import glp_sponsor_campaign as campaign
    campaign.approval_ready(read_json(p.root, campaign.CONFIG))
    report = p.native('pilot_report', 'reports/ops/sponsor-machine-20261003')
    require(report['sent_messages'], 'no genuine sponsor SENT output')
    client = campaign.service('read')
    counts, identities = {}, set()
    for item in report['sent_messages']:
        row = client.get_message(item['message_id'], format='raw')
        raw = base64.urlsafe_b64decode(row['raw'] + '===')
        m = BytesParser(policy=default).parsebytes(raw)
        require('SENT' in row.get('labelIds', []) and row['threadId'] == item['thread_id'] and digest(raw) == item['raw_sha256'], 'pilot SENT identity changed')
        require(parseaddr(str(m['From']))[1].lower() == campaign.SENDER, 'wrong sponsor sender')
        identity = (item['organization_id'], item['sequence_step'])
        require(identity not in identities and item['us_eligible'] is True, 'duplicate/ineligible outreach')
        identities.add(identity)
        at = datetime.fromtimestamp(int(row['internalDate']) / 1000, ZoneInfo('America/Los_Angeles'))
        require(at.weekday() < 5 and 6 <= at.hour < 17, 'outside approved send window')
        counts[str(at.date())] = counts.get(str(at.date()), 0) + 1
    require(max(counts.values()) <= 25, 'daily send cap exceeded')
    require(report['genuine_reply_stops'] and report['genuine_followup_stops'] and report['opt_out_stops'], 'genuine stop handling not observed')
    require(report['excluded_provider_sends'] == report['suppressed_sends'] == 0 and report['spend_used'] <= report['spend_cap'], 'exclusion/suppression/spend defect')
    return {'sent_messages': len(identities), 'daily_max': max(counts.values()), **p.observed}


def disk_floor(p):
    free = shutil.disk_usage(p.root).free
    p.observed['host_free_bytes'] = free
    require(free >= FLOOR, f'host_free_bytes={free} < {FLOOR}')
    return free


def pruner(p):
    disk_floor(p)
    first = p.native('manual_prune', 'reports/ops')
    natural = p.native('scheduled_prune', 'reports/ops')
    for report in (first, natural):
        require(report['invocation'] in ('manual', 'scheduled') and report['status'] == 'COMPLETE'
                and report['partial'] is False and report['protected_removed_count'] == report['active_removed_count'] == 0, 'incomplete/unsafe prune')
        require(report['registered_entrypoint'] and report['allowlist_sha256'] == digest(confined(p.root, report['allowlist_path']).read_bytes()), 'prune allowlist/current entry binding changed')
        require(report['free_bytes_after'] >= FLOOR and report['actual_freed_bytes'] >= 0, 'recorded floor not sustained')
    require(first['actual_freed_bytes'] > 0 and first['invocation'] == 'manual'
            and natural['invocation'] == 'scheduled' and stamp(natural['started_utc']) > stamp(first['completed_utc']), 'actual reclaim then subsequent scheduled cycle required')
    return {'freed_bytes': first['actual_freed_bytes'], **p.observed}


def host_safety(p):
    disk_floor(p)
    db = Path('/home/ubuntu/.hermes/state.db')
    require(db.stat().st_size < 8_000_000_000, f'state.db_bytes={db.stat().st_size} >= 8000000000')
    prune = p.native('scheduled_prune', 'reports/ops')
    require(prune['invocation'] == 'scheduled' and prune['status'] == 'COMPLETE' and prune['free_bytes_after'] >= FLOOR, 'scheduled floor not sustained')
    disposition = p.native('kael_disposition', 'reports/ops')
    require(disposition['disposition'] in ('SUCCESS', 'CONSUMER_REMOVED') and disposition['consumer_readback'], 'kael-audit disposition unverified')
    telemetry = p.native('capacity_telemetry', 'reports/ops')
    rows = telemetry['samples']
    require(len(rows) >= 2 and stamp(rows[-1]['at']) - stamp(rows[0]['at']) >= 86400, '24-hour telemetry absent')
    require(all(r['admission_safe'] is True and r['filler_workers'] == 0 and r['memory_swap_safe'] is True for r in rows)
            and any(r['real_worker_slots'] == 8 for r in rows), 'real safe eight-slot workload not observed')
    require(max(stamp(b['at']) - stamp(a['at']) for a, b in zip(rows, rows[1:])) <= 900, 'telemetry gaps exceed 15 minutes')
    return {'state_db_bytes': db.stat().st_size, 'telemetry_samples': len(rows), **p.observed}


def onboarding(p):
    if hasattr(p, 'onboard_live'):
        live_deliveries = p.onboard_live()
    else:
        live_deliveries = None  # isolated offline transport fixture only
    output = p.native('onboarding', 'reports/glp-clinic-scout')
    require(output['classification'] == 'TEST' and output['consented'] is True and output['owned_identity'] is True, 'owned consented TEST only')
    if live_deliveries is not None:
        require(any(r['resend_email_id'] == output['provider_message_id'] and r['status'] == 'delivered'
                    for r in live_deliveries), 'exact provider identity not delivered in live onboarding ledger')
    # Provider and mailbox observations are exact GETs, not sender exit status.
    provider = p.resend_get(output['provider_message_id']) if hasattr(p, 'resend_get') else p.native('provider_delivery', 'reports/glp-clinic-scout')
    require(provider['id'] == output['provider_message_id'] and provider['status'] == 'delivered', 'provider delivery not verified')
    mailbox = p.inputs['mailbox_message']
    row = p.owned_mailbox_message(mailbox['id'])
    require(digest(base64.urlsafe_b64decode(row['raw'] + '===')) == mailbox['raw_sha256'], 'owned mailbox content mismatch')
    require(output['duplicate_refusals'] > 0 and output['unconsented_refusals'] > 0 and output['suppressed_refusals'] > 0, 'required adverse refusals absent')
    require(output['copy_sha256'] == output['accepted_copy_sha256'] and output['retention_sha256'] == output['accepted_retention_sha256'], 'copy/retention drift')
    active = p.native('activation', 'reports/glp-clinic-scout')
    require(active['capture_enabled'] is True and active['onboarding_enabled'] is True
            and active['copy_sha256'] == output['copy_sha256'] and active['commercial_test_rows'] == 0, 'same approved automation not activated/TEST exclusion absent')
    p.reviews(('copy', 'retention'))
    return p.observed


def conversions(p):
    p.pr('glp-business', 560)
    report = p.native('reconciliation', 'reports/glp-clinic-scout/affiliate-machine/conversion-reconciliation')
    data = report['conversion_reconciliation']
    require(data['coverage_complete'] is True and data['input_completeness'] == 'COMPLETE'
            and data['provenance'] == 'AUTHENTICATED_PRODUCTION_DATA', 'financial population/coverage is UNKNOWN, not a zero')
    attempt = p.native('fresh_attempt', 'reports/ops')
    require(attempt['status'] == 'complete' and not attempt['attempt_sha256'].startswith('e4678e3d')
            and attempt['preclaim_source_pr'] == 560 and attempt['fresh_attempt_count'] == 1, 'exactly one fresh PR560 preplanned attempt required; old effect never replayed')
    require(stamp(report['completed_at']) > stamp(attempt['started_at']), 'report predates fresh attempt')
    for row in data['rows']:
        require(row['classification'] == 'REAL' and row['source_ids'] and row['conversion_id']
                and row['earnings_id'] and (row['payout_id'] or row['unmatched_owner'])
                and row['reversal_status'] in ('none', 'reversed', 'partial'), 'conversion/earnings/payout/reversal/unmatched lineage incomplete')
        require(stamp(row['period_end_exclusive']) <= datetime.now(timezone.utc).timestamp(), 'financial period not closed')
    require(data['rows'] or data.get('network_proven_zero') is True, 'no lineage or network-proven zero')
    require(data['test_rows_in_financial_totals'] == 0, 'TEST contamination')
    p.no_effects(report)
    return {'real_lineage_rows': len(data['rows']), **p.observed}


def supply(p):
    report = p.native('reconciliation', 'reports/glp-clinic-scout/affiliate-machine') if p.inputs.get('reconciliation') else read_json(
        p.root, 'lanes/F01-OWNER/glp93-69-20261016/DISPOSITIONS.json')
    frozen_path = '.worktrees/ultra-glp93-20261015/reports/glp-clinic-scout/affiliate-machine/reconciliation/20261015-glp93/DISPOSITIONS.json'
    raw = confined(p.root, frozen_path).read_bytes()
    require(digest(raw) == '11af2dbe4560880668f29cfd5cb5e18b18f0c89b292cbc7f1e586b912591d16f', 'operator-frozen cohort source bytes changed')
    frozen = json.loads(raw)
    identity = lambda r: (r['sheet_row'], r['network'], r['program_key'])
    require(report['denominator'] == 69 and len(report['rows']) == 69
            and {identity(r) for r in report['rows']} == {identity(r) for r in frozen['rows']},
            'frozen 69-row population changed; rows are not distinct programs')
    require(report['fully_eligible'] is not None
            and all(r['eligibility'] != 'NOT_ESTABLISHED' for r in report['rows']),
            'full cohort eligibility remains UNKNOWN; routing/wire-ready is not eligibility')
    rows = p.sheet()
    require(rows, 'fresh canonical Sheet read empty')
    require(report['sheet_sha256'] == digest(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()), 'reconciliation not bound to current authenticated Sheet')
    approved = [r for r in report['rows'] if r['eligibility'] == 'FULLY_ELIGIBLE_APPROVED_US']
    require(approved and all(r['adopted'] is True and r['newly_live'] is True
                            and 'fella' not in json.dumps(r).lower() and r['held'] is False for r in approved),
            'every eligible US cohort program must be adopted, including at least one newly live provider')
    for r in approved:
        registry = p.native(r['registry_locator'], 'reports/glp-clinic-scout')
        require(registry['program_id'] == r['program_id'] and registry['route'] == r['route'] and registry['status'] == 'live', 'governed registry/build adoption mismatch')
        p.page(r['live_page'])
        require(r['provider_identity'] in http(r['route']).decode(), 'served new-cohort provider identity mismatch')
    require(all(r['receipt_id'] and r['disposition'] in ('approved', 'rejected', 'withdrawn', 'held', 'pending') for r in report['queued_applications']), 'queued applications lack genuine disposition receipts')
    p.no_effects(report)
    return {'inventory_rows': 69, 'eligible_approved_adopted': len(approved), **p.observed}


def closure(p):
    job = p.cron('13d3fff8734a')
    require(job['enabled'] is True and job['state'] == 'scheduled', 'hourly coordinator is paused/not enabled')
    output = p.native('closure_readback', 'reports/ops/linear-close')
    require(output['poller_status'] == 'READY_TO_CLOSE' and output['proof_rc'] == output['validator_rc'] == 0
            and output['immutable_receipt'] and output['linear_before'] != 'Done', 'genuine proof/validator/receipt chain absent')
    require(live_issue(output['closed_issue'])['state']['name'] == 'Done', 'exact target is not Done live')
    require(stamp(output['done_at']) < stamp(output['hourly_enabled_at']), 'hourly enabled before genuine closure')
    runs = p.native('coordinator_runs', 'reports/ops')
    first, second = runs['runs']
    require(first['invocation'] == second['invocation'] == 'scheduled' and first['unattended_action_id']
            and first['tui_race_count'] == second['tui_race_count'] == 0
            and second['duplicate_effects'] == 0 and second['idempotent'] is True
            and stamp(second['started_at']) > stamp(first['completed_at']), 'natural action and idempotent second pass not proved')
    return p.observed


def cloud(p):
    report = p.native('cloud_results', 'reports/ops/dot-tickets')
    require({r['dot'] for r in report['results']} == {'rook', 'finch'} and len(report['results']) == 2, 'both dot Cloud tasks required')
    from post_discord_message import readback
    for r in report['results']:
        require(r['environment_id'] and r['task_id'] and r['result_id'] and r['environment_published'] is True
                and r['executor'] == 'codex-cloud' and r['approved_boundary_result'] == 'PASS', 'actual published Cloud execution/boundary output absent')
        message = readback(r['channel_id'], r['reply_id'])
        require(message['author']['id'] == r['author_id'] and message['message_reference']['message_id'] == r['request_id']
                and r['task_id'] in message['content'] and r['result_id'] in message['content'], 'exact authenticated Discord result/task readback mismatch')
        p.pr('glp-business', r['pr_number'])
    p.reviews(('readiness',))
    return p.observed


def permissions(p, ro=False):
    # Exact original threads only. Never messages.list, label mutation, replay or send.
    original = [('XenTraffic', '1a086bd56c0efba4', '1a1229a1484e341a'),
                ('BistroMD', '1a0a0ca5a8b8d54a', '1a1229aa7f912aa1')]
    if ro:
        original = [('Ro', '1a0e14c2f194fb53', '1a0e14c2f194fb53')]
    client = ops_mail()
    replies = {}
    for partner, thread_id, sent_id in original:
        thread = client.users().threads().get(userId='me', id=thread_id, format='full').execute(num_retries=0)
        messages = thread.get('messages', [])
        sent = next(r for r in messages if r['id'] == sent_id)
        inbound = [r for r in messages if int(r['internalDate']) > int(sent['internalDate'])
                   and not {'SENT', 'DRAFT'}.intersection(r.get('labelIds', []))]
        require(inbound, partner + ': no provider permission reply in exact original thread')
        replies[partner] = {r['id'] for r in inbound}
    output = p.native('permission_registry', 'reports/glp-clinic-scout')
    for r in output['answers']:
        require(r['partner'] in replies and r['message_id'] in replies[r['partner']]
                and r['decision'] in ('grant', 'refusal') and r['scope_quote'], 'unbound written permission decision')
        raw = client.users().messages().get(userId='me', id=r['message_id'], format='raw').execute(num_retries=0)
        wire = base64.urlsafe_b64decode(raw['raw'] + '===')
        require(digest(wire) == r['raw_sha256'], 'reply native bytes differ from governed answer')
        require(r['hold'] == ('released' if r['decision'] == 'grant' else 'kept'), 'hold contrary to written answer')
        require(r['registry_message_id'] == r['message_id'] and r['duplicate_requests'] == 0, 'registry identity/duplicate request defect')
    require({r['partner'] for r in output['answers']} == set(replies), 'missing network answer')
    return output


def partner_holds(p):
    permissions(p)
    return p.observed


def ro_approval(p):
    permissions(p, ro=True)
    output = p.native('sheet_adoption', 'reports/glp-clinic-scout/affiliate-machine')
    rows = p.sheet()
    require(output['tenant'] and output['offer_id'] and output['inbox_event_id'] and output['queue_id']
            and output['drain_mode'] == 'sole_writer' and output['sole_or_best_rail_evidence'], 'inbox/queue/sole-drain/rail chain absent')
    require(rows[output['sheet_row'] - 1] == output['sheet_postimage'], 'current Sheet row not the verified drain postimage')
    require(output['placement_permission'] and output['traffic_permission'] and output['event_terms_permission']
            and output['registry_status'] == 'live' and output['build_route'] == output['live_page']['route'], 'Ro permission/registry/build adoption incomplete')
    p.page(output['live_page'])
    require(output['partner_pixel_fires'] == output['tracking_requests'] == 0, 'pixel/tracking probe prohibited')
    return p.observed


def xentraffic(p):
    permissions(p)
    output = p.native('network_attribution', 'reports/glp-clinic-scout/affiliate-machine')
    require(output['network'] == 'xentraffic' and output['permission_eligible'] is True
            and output['classification'] == 'REAL' and output['origin'] == 'authenticated_network'
            and output['postback_id'] and output['registry_id'] and output['click_id']
            and output['join_count'] == 1 and output['duplicate_count'] == 0, 'genuine once-joined network attribution absent')
    events, clicks = p.receiver()
    events = [e for e in events if e['network'] == 'xentraffic' and e['revenue_event_id'] == output['postback_id']]
    require(len(events) == 1, 'exact network postback absent/duplicated in live receiver ledger')
    event = events[0]
    attributes = json.loads(event['attributes_json'] or '{}')
    require(attributes.get('is_test') in (False, 0) and attributes.get('is_probe') in (False, 0)
            and event['conversion_id'] and event['program_id'] == output['registry_id']
            and event['cs_click_id'] == output['click_id'], 'postback is TEST/PROBE/UNKNOWN or has wrong identity')
    joined = [c for c in clicks if c['click_token'] == event['cs_click_id']
              and c['network'] == 'xentraffic' and c['program_id'] == event['program_id']
              and c['is_test'] == c['is_probe'] == 0]
    require(len(joined) == 1, 'live governed non-TEST click join is not exactly one')
    p.page(output['live_page'])
    require(output['tracking_requests'] == 0 and output['fabricated_earnings'] == 0, 'tracking/fabricated earning prohibited')
    return p.observed


VERIFIERS = {'GLP-125': nuimage, 'GLP-320': savings, 'GLP-327': sponsor_site,
             'GLP-328': sponsor_outreach, 'GLP-349': host_safety, 'GLP-47': onboarding,
             'GLP-334': conversions, 'GLP-93': supply, 'GLP-354': pruner,
             'GLP-358': closure, 'GLP-359': cloud, 'GLP-88': xentraffic,
             'GLP-250': partner_holds, 'GLP-253': ro_approval}


def run(issue, root=ROOT, plan=None):
    require(issue in VERIFIERS, 'issue not in reviewed business-proof allowlist')
    rows = (plan if plan is not None else read_json(root, PLAN))['issues']
    matches = [r for r in rows if r['issue'] == issue]
    require(len(matches) == 1, 'issue policy missing/duplicate')
    row = matches[0]
    current = live_issue(issue)
    business = acceptance(current['description'])
    expected = ' '.join(row['acceptance_rewrite'].split(' Read-only proof:', 1)[0].split())
    require(business == expected, 'current Linear acceptance changed; review and update predicate before closure')
    result = VERIFIERS[issue](Proof(issue, row, root))
    return {'issue': issue, 'acceptance_sha256': digest(business.encode()), 'observed': result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('issue', choices=sorted(VERIFIERS))
    args = parser.parse_args(argv)
    try:
        print(json.dumps({'status': 'PASS', **run(args.issue)}, sort_keys=True))
        return 0
    except Exception as exc:
        # Do not print provider bodies, tokens, mail text or credential paths.
        reason = str(exc) if isinstance(exc, Refused) else type(exc).__name__ + ': native output unavailable or incompatible'
        print(json.dumps({'issue': args.issue, 'status': 'REFUSED', 'reason': reason}, sort_keys=True))
        return 3


if __name__ == '__main__':
    sys.exit(main())
