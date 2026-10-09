#!/usr/bin/env python3
"""One-command scheduled release of merged site `main` (operator-approved-main-release).

  glp_release_main.py [--sha SITE_MAIN_SHA] [--prepare-only]

Authority: ops/linear/approvals/OPERATOR-DECISION-20261009-workflow-v3.md — merged
`main` is published by this scheduled command, followed by a live readback.

It wraps the existing registered path (glp_affiliate_sync_job.execute(..., publish=True)):
exact merged PR + green CI on its head, native build, raw held-provider/ended-program
census (Fella included), adverse-absence audit, package, prepare, Cloudflare publish with
rollback, and the pointer CAS. Nothing here relaxes those gates. It adds only:

* idempotency: the current pointer already at the merged head -> PASS_ALREADY_CURRENT;
* source selection: the merged PR whose merge commit is the requested main commit and
  whose head tree equals that commit's tree (the carrier builds the exact PR head);
* a secrets scan of the built public tree before any effect;
* cache convergence: before the live route matrix, purge-by-URL (never whole-zone) of the
  bare, non-content-addressed URLs the matrix probes on the zone. Cloudflare serves those
  from a one-year immutable edge cache, so without the purge a correct release rolled back.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = Path('/home/ubuntu/business')
SCOPE = 'operator-approved-main-release'
AUTHORITY = 'ops/linear/approvals/OPERATOR-DECISION-20261009-workflow-v3.md'
RUNS = ROOT / 'reports/glp-clinic-scout/affiliate-machine/registry-site-sync'
ZONE_NAME = 'glpclinicscout.com'
PURGE_BATCH = 30
SECRET_PATTERNS = [re.compile(p) for p in (
    rb'-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----',
    rb'\bsk_live_[0-9A-Za-z]{16,}', rb'\brk_live_[0-9A-Za-z]{16,}',
    rb'\bghp_[0-9A-Za-z]{30,}', rb'\bgithub_pat_[0-9A-Za-z_]{40,}',
    rb'\bxox[baprs]-[0-9A-Za-z-]{10,}', rb'\bAKIA[0-9A-Z]{16}\b',
    rb'\bAIza[0-9A-Za-z_-]{35}\b(?![^<]{0,40}(?:maps|places))',
    rb'\blin_api_[0-9A-Za-z]{30,}', rb'CLOUDFLARE_API_TOKEN\s*=',
)]


def gh_json(*args):
    return json.loads(subprocess.check_output(['gh', *args], text=True, timeout=120))


def site_main_sha():
    return gh_json('api', 'repos/izaiahat/glp-site-candidate/commits/main')['sha']


def merged_pr_for(main_sha, gh=gh_json):
    """The merged PR whose merge commit is main_sha and whose head tree equals it."""
    prs = gh('pr', 'list', '--repo', 'izaiahat/glp-site-candidate', '--state', 'merged', '--limit', '30',
             '--json', 'number,url,headRefOid,mergeCommit')
    for pr in prs:
        if (pr.get('mergeCommit') or {}).get('oid') == main_sha:
            tree = lambda ref: gh('api', f'repos/izaiahat/glp-site-candidate/commits/{ref}')['commit']['tree']['sha']
            if tree(pr['headRefOid']) != tree(main_sha):
                raise RuntimeError('main_tree_differs_from_pr_head_rebase_required')
            return pr
    raise RuntimeError('main_head_not_a_merged_pr:' + main_sha)


def secrets_scan(public: Path):
    hits = []
    for path in public.rglob('*'):
        if path.is_file() and not path.is_symlink() and path.suffix in ('.html', '.js', '.json', '.css', '.txt', '.xml', ''):
            raw = path.read_bytes()
            for pattern in SECRET_PATTERNS:
                if pattern.search(raw):
                    hits.append({'file': str(path.relative_to(public)), 'pattern': pattern.pattern.decode()[:40]})
    if hits:
        raise RuntimeError('secrets_scan_failed:' + json.dumps(hits[:5]))
    return {'status': 'PASS', 'patterns': len(SECRET_PATTERNS)}


def purge_urls(route_specs, site):
    """Bare (non-content-addressed) assets plus HTML/XML routes the live matrix probes."""
    urls = []
    for item in route_specs:
        path = item['url_path']
        if path.startswith('/assets/') and re.search(r'\.[0-9a-f]{16}\.(?:js|css)$', path):
            continue  # content-addressed names cannot hold stale bytes
        urls.append(site + path)
    return sorted(set(urls))


def make_purger(read_token, log):
    state = {'zone': None}

    def request(method, path, body=None):
        req = urllib.request.Request('https://api.cloudflare.com/client/v4' + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'Authorization': 'Bearer ' + read_token(),
                                              'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=45) as response:
            payload = json.loads(response.read())
        if not payload.get('success'):
            raise RuntimeError(f'cloudflare_purge_api_unsuccessful:{method}:{path.split("?")[0]}')
        return payload['result']

    def purge(urls):
        if not urls:
            return []
        if any('*' in u or not u.startswith('https://' + ZONE_NAME + '/') for u in urls):
            raise RuntimeError('purge_url_outside_zone_or_wildcard')
        if state['zone'] is None:
            zones = request('GET', '/zones?name=' + ZONE_NAME)
            if len(zones) != 1 or zones[0]['name'] != ZONE_NAME:
                raise RuntimeError('purge_zone_identity_invalid')
            state['zone'] = zones[0]['id']
        batches = []
        for i in range(0, len(urls), PURGE_BATCH):
            chunk = urls[i:i + PURGE_BATCH]
            request('POST', f"/zones/{state['zone']}/purge_cache", {'files': chunk})  # by URL; never purge_everything
            batches.append(len(chunk))
        log({'purged_urls': urls, 'batches': batches})
        return batches
    return purge


def install_cache_convergence(carrier, purge, log):
    """Purge-by-URL exactly once per deployment before the unchanged live route matrix."""
    original = carrier.current_route_matrix
    done = set()

    def current_route_matrix(client, deployment, route_specs, public, receipt_path, receipt, **kw):
        key = str(deployment.get('id', ''))
        if key not in done:
            done.add(key)
            urls = purge_urls(route_specs, carrier.SITE)
            purge(urls)
            receipt.setdefault('stages', {})['cache_purge_by_url'] = {'deployment_id': key, 'count': len(urls)}
            carrier.checkpoint(receipt_path, receipt)
        return original(client, deployment, route_specs, public, receipt_path, receipt, **kw)
    carrier.current_route_matrix = current_route_matrix
    return original


def pointer_head():
    pointer = json.loads((ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json').read_bytes())
    return pointer.get('site_head'), pointer.get('deployment_id')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sha', help='exact site main commit (default: current origin/main)')
    p.add_argument('--prepare-only', action='store_true')
    a = p.parse_args(argv)
    import glp_affiliate_sync_job as job
    import glp_deploy_glp34_authority_v1 as carrier
    RUNS.mkdir(parents=True, exist_ok=True)
    with (RUNS / 'run.lock').open('a') as lock:  # same lock as the :17 sync job
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'DEFERRED_BUSY', 'scope': SCOPE}))
            return 0
        for intent in RUNS.glob('*/publish-intent.json'):
            if not (intent.parent / 'PUBLISH.json').exists():
                raise RuntimeError('prior_publication_uncertain:' + str(intent))
        main_sha = a.sha or site_main_sha()
        pr = merged_pr_for(main_sha)
        current_head, current_deployment = pointer_head()
        pointer = json.loads((ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json').read_text())
        if current_head in (main_sha, pr['headRefOid']) and job.rebuild_inputs_complete(Path(pointer['release_root'])):
            print(json.dumps({'status': 'PASS_ALREADY_CURRENT', 'scope': SCOPE, 'site_main': main_sha,
                              'deployment_id': current_deployment}))
            return 0
        report = RUNS / ('main-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        report.mkdir(parents=False, exist_ok=False)
        events = []
        log = lambda row: (events.append(row), job.save(report / 'MAIN-RELEASE-EVENTS.json', events))
        job.save(report / 'MAIN-RELEASE-INTENT.json', {'scope': SCOPE, 'authority': AUTHORITY, 'site_main': main_sha,
                                                       'source_pr': pr['url'], 'source_head': pr['headRefOid'],
                                                       'predecessor_deployment_id': current_deployment})
        original_gate = job.build_gate

        def gated(source, rep, config, head):
            original_gate(source, rep, config, head)
            log({'secrets_scan': secrets_scan(Path(source) / 'public')})
        job.build_gate = gated
        import glp_affiliate_release as release
        # Workflow v3: fresh build from main; the stale PREPARED-chain re-authentication is skipped,
        # terminal-pointer identity + live-head check + rollback + pointer CAS remain.
        release.authenticated_current_base = release.main_release_base
        carrier.MAIN_RELEASE_FRESH = True
        install_cache_convergence(carrier, make_purger(carrier.read_token, log), log)
        try:
            result = job.execute(job.read(job.CONFIG), report, source_ref=pr['headRefOid'], source_pr=pr['url'],
                                 publish=not a.prepare_only)
        except Exception as exc:
            job.save(report / 'FAILURE.json', {'status': 'FAIL', 'scope': SCOPE, 'error': str(exc)[:2000],
                                               'run_type': os.environ.get('GLP_RUN_TYPE', 'manual')})
            raise
        out = {'status': result.get('status'), 'scope': SCOPE, 'site_main': main_sha, 'source_pr': pr['url'],
               'deployment_id': pointer_head()[1], 'predecessor_deployment_id': current_deployment, 'report': str(report)}
        job.save(report / 'MAIN-RELEASE-RESULT.json', out)
        print(json.dumps(out))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
