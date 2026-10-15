#!/usr/bin/env python3
"""One-command scheduled release of merged site `main` (operator-approved-main-release).

  glp_release_main.py [--sha SITE_MAIN_SHA] [--prepare-only]

Authority: ops/linear/approvals/OPERATOR-DECISION-20261009-workflow-v3.md — merged
`main` is published by this scheduled command, followed by a live readback.

It wraps the shared gated release library (execute(..., publish=True, source_only=False)):
exact merged PR + green CI on its head, native build, raw held-provider/ended-program
census (Fella included), adverse-absence audit, package, prepare, Cloudflare publish with
rollback, and the pointer CAS. Nothing here relaxes those gates. It adds only:

* idempotency: exact successful input closure + fresh validators/sensor ->
  SKIPPED_IDENTICAL_INPUTS (no clone, native build, package or deployment);
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


def ensure_main_acceptance(main_sha, gh=gh_json):
    """Make stale-base integration a normal exact-head CI subject, not a bypass.

    Recovery only creates/reuses its own deterministic marker PR. It never
    selects any unrelated open PR, and never substitutes main for an accepted
    head. The existing exact-tree check is rerun after a guarded merge.
    """
    try:
        accepted = merged_pr_for(main_sha) if gh is gh_json else merged_pr_for(main_sha, gh=gh)
        return {'status': 'ACCEPTED', 'main_sha': main_sha, 'pr': accepted}
    except RuntimeError as exc:
        if str(exc) != 'main_tree_differs_from_pr_head_rebase_required':
            raise
    api = 'repos/izaiahat/glp-site-candidate'
    prs = gh('pr', 'list', '--repo', 'izaiahat/glp-site-candidate', '--state', 'merged',
             '--limit', '30', '--json', 'number,url,headRefOid,mergeCommit')
    previous = next(p for p in prs if (p.get('mergeCommit') or {}).get('oid') == main_sha)
    tree = gh('api', f'{api}/commits/{main_sha}')['commit']['tree']['sha']
    branch = 'release/accepted-main-' + main_sha
    # data/** is deliberately enrolled in both existing exact-head CI workflows.
    marker_path = 'data/main-release-acceptance.json'
    marker = json.dumps({'schema': 'glp-main-integration-acceptance-v1',
                         'source_main_sha': main_sha, 'source_main_tree': tree,
                         'previous_pr': previous['url'],
                         'previous_head': previous['headRefOid']}, sort_keys=True, indent=2) + '\n'
    result = {'status': 'DEFERRED_MAIN_ACCEPTANCE', 'main_sha': main_sha,
              'main_tree': tree, 'previous_head': previous['headRefOid']}
    proposals = gh('pr', 'list', '--repo', 'izaiahat/glp-site-candidate', '--state', 'all',
                   '--base', 'main', '--head', branch,
                   '--json', 'number,url,headRefOid,state,statusCheckRollup')
    if proposals:
        if len(proposals) != 1 or proposals[0]['state'] != 'OPEN':
            raise RuntimeError('main_acceptance_pr_ambiguous_or_closed_reconcile_required')
        pr = proposals[0]
        head = pr['headRefOid']
        commit = gh('api', f'{api}/git/commits/{head}')
        if [p['sha'] for p in commit['parents']] != [main_sha]:
            raise RuntimeError('main_acceptance_head_parent_drift')
        import base64
        content = gh('api', f'{api}/contents/{marker_path}?ref={head}')
        if base64.b64decode(content['content']).decode() != marker:
            raise RuntimeError('main_acceptance_marker_drift')
        comparison = gh('api', f'{api}/compare/{main_sha}...{head}')
        if [f['filename'] for f in comparison['files']] != [marker_path]:
            raise RuntimeError('main_acceptance_source_drift')
        result['acceptance_pr'] = pr['url']
        checks = pr.get('statusCheckRollup') or []
        if not checks or any(c.get('status') != 'COMPLETED' or c.get('conclusion') != 'SUCCESS' for c in checks):
            return {**result, 'reason': 'required_exact_head_ci_not_green'}
        if gh('api', f'{api}/commits/main')['sha'] != main_sha:
            raise RuntimeError('main_acceptance_base_advanced_retry_next_run')
        response = gh('api', f'{api}/pulls/{pr["number"]}/merge', '--method', 'PUT',
                      '-f', f'sha={head}', '-f', 'merge_method=merge')
        if not response.get('merged'):
            raise RuntimeError('main_acceptance_merge_refused')
        merged = gh('api', f'{api}/commits/main')['sha']
        accepted = merged_pr_for(merged, gh=gh)
        if accepted['headRefOid'] != head:
            raise RuntimeError('main_acceptance_merge_readback_drift')
        return {'status': 'ACCEPTED', 'main_sha': merged, 'pr': accepted}
    new_tree = gh('api', f'{api}/git/trees', '--method', 'POST',
                  '-f', f'base_tree={tree}', '-f', f'tree[][path]={marker_path}',
                  '-f', 'tree[][mode]=100644', '-f', 'tree[][type]=blob',
                  '-f', f'tree[][content]={marker}')['sha']
    head = gh('api', f'{api}/git/commits', '--method', 'POST',
              '-f', 'message=chore: accept integrated main through exact-head CI',
              '-f', f'tree={new_tree}', '-f', f'parents[]={main_sha}')['sha']
    # Reconcile a prior uncertain ref write instead of pushing/overwriting it.
    refs = gh('api', f'{api}/git/matching-refs/heads/{branch}')
    if refs:
        if len(refs) != 1:
            raise RuntimeError('main_acceptance_ref_uncertain_reconcile_required')
        head = refs[0]['object']['sha']
        resumed = gh('api', f'{api}/git/commits/{head}')
        if (resumed['tree']['sha'] != new_tree
                or [p['sha'] for p in resumed['parents']] != [main_sha]):
            raise RuntimeError('main_acceptance_ref_uncertain_reconcile_required')
    else:
        gh('api', f'{api}/git/refs', '--method', 'POST',
           '-f', f'ref=refs/heads/{branch}', '-f', f'sha={head}')
    proposal = gh('api', f'{api}/pulls', '--method', 'POST', '-f', 'base=main', '-f', f'head={branch}',
                  '-f', 'title=chore: accept integrated main through exact-head CI',
                  '-f', f'body=fleet-ticket:GLP-349\n\nMarker-only integration acceptance for {main_sha}. '
                  'The predecessor PR had a stale base. Normal required exact-head CI must pass '
                  'before this PR can merge; production exact-head tree equality stays mandatory. '
                  'No held or unrelated PR is merged.')
    return {**result, 'acceptance_pr': proposal['html_url'], 'reason': 'proposed_fresh_exact_head_ci'}


def fresh_release_inputs(config, report, pr):
    """Read exact-head registry source and the live Sheet without a site clone."""
    import base64
    import glp_affiliate_sync_job as job
    from glp_affiliate_registry_sync import sync
    from glp_release_inputs import capture
    source = report / 'input-source'
    for name in ('program-registry.json', 'approved-program-registry.json',
                 'affiliate-wire-in-approvals.json', 'affiliate-activation-selection.json',
                 'go-selection-receipt.json'):
        obj = gh_json('api', f'repos/izaiahat/glp-site-candidate/contents/data/{name}?ref={pr["headRefOid"]}')
        path = source / 'data' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(obj['content']))
    outcome = sync(source, report / 'registry', apply=False)
    if outcome['changed']:
        return None  # Full producer must propagate the fresh denial/terms delta.
    tree = gh_json('api', f'repos/izaiahat/glp-site-candidate/commits/{pr["headRefOid"]}')['commit']['tree']['sha']
    snapshot = capture(config, source_tree=tree, sheet_sha256=job.sha(report / 'registry/sheet-snapshot.json'))
    job.save(report / 'CURRENT-INPUTS.json', snapshot)
    return snapshot


def validate_cached_release(config, report, root, pr):
    """Rerun current-time validation and adverse-absence gates, never npm build."""
    import shutil
    import glp_affiliate_sync_job as job
    from glp_affiliate_absence_audit import audit
    job.ci(pr['url'], report)
    argv = job.namespace(config, root, report)
    for i, token in enumerate(argv):
        if token == '--bind' and argv[i + 2] == config['namespace']:
            argv[i] = '--ro-bind'
    # Some validators emit reports. Overlay only their artifact directory, not
    # source/data/public, so validation cannot mutate the deployed release.
    artifacts = report / 'cached-validation-artifacts'
    shutil.copytree(root / 'artifacts', artifacts, symlinks=True)
    argv += ['--bind', str(artifacts), config['namespace'] + '/artifacts']
    job.run(argv + ['npm', 'run', 'validate'], log=report / 'cached-native-validation.log')
    code = """import{readFileSync,readdirSync}from'node:fs';import{join,relative}from'node:path';
import{rawHeldInputs,rawHeldOutputCensus,assertRawHeldOutput}from'./src/served-affiliate-identity.mjs';
import{loadGoPromotionAuthority}from'./src/go-runtime-selection.mjs';import{GO_RUNTIME_MANIFEST}from'./functions/go/program-manifest.js';
const inputs=rawHeldInputs(process.cwd()),authority=loadGoPromotionAuthority(process.cwd());
for(const[id,p]of Object.entries(authority.programs))if(p.status==='WITHHELD'&&!inputs.held.some(r=>r.id===id))inputs.held.push({id,sources:['effective current authority']});
const docs=[];function walk(p){for(const d of readdirSync(p,{withFileTypes:true})){const f=join(p,d.name);if(d.isDirectory())walk(f);else if(/\\.(html|json)$/.test(f))docs.push({file:relative('public',f),text:readFileSync(f,'utf8')})}}walk('public');
console.log(JSON.stringify(assertRawHeldOutput(rawHeldOutputCensus(docs,inputs,GO_RUNTIME_MANIFEST.programs))));"""
    census = json.loads(job.run(argv + ['node', '--input-type=module', '-e', code]))
    job.save(report / 'RAW-HELD-CENSUS.json', census)
    absence = audit(root, job.read(report / 'registry/registry-sync.json'),
                    job.read(report / 'registry/sheet-snapshot.json'))
    job.save(report / 'ADVERSE-ABSENCE.json', absence)
    secrets_scan(root / 'public')


def try_identical_inputs(config, report, pointer, pr):
    """Reuse only a successful, intact release with a complete equal input key."""
    import glp_affiliate_sync_job as job
    from glp_release_inputs import path_digest
    from glp_deploy_sensor_hook import attach_sensor_result
    root = Path(pointer['release_root'])
    baseline_path = root.parents[1] / 'SUCCESSFUL-INPUTS.json'
    def miss(reason):
        job.save(report / 'INPUT-CACHE-DECISION.json', {'status':'BUILD_REQUIRED', 'reason':reason})
        return None
    if not baseline_path.is_file() or baseline_path.is_symlink():
        return miss('last_success_has_no_recorded_input_closure')
    baseline = job.read(baseline_path)
    if baseline.get('status') != 'PASS_PRODUCTION_TERMINAL' or baseline.get('pointer') != pointer:
        return miss('last_success_pointer_or_terminal_binding_differs')
    current = fresh_release_inputs(config, report, pr)
    if not current or current != baseline.get('inputs'):
        return miss('source_sheet_environment_dependency_mount_or_clock_inputs_differ')
    if path_digest(root) != baseline['release_tree_sha256']:
        raise ValueError('identical_inputs_release_tree_drift')
    try:
        validate_cached_release(config, report, root, pr)
        out = {'status': 'CHECKING_IDENTICAL_INPUTS', 'scope': SCOPE,
               'source_pr': pr['url'], 'source_head': pr['headRefOid'],
               'deployment_id': pointer['deployment_id'], 'release_root': str(root),
               'inputs_sha256': current['sha256'], 'baseline': str(baseline_path),
               'build_executed': False, 'deployed': False, 'report': str(report),
               'run_type': os.environ.get('GLP_RUN_TYPE', 'manual')}
        receipt = report / 'MAIN-RELEASE-RESULT.json'
        job.save(receipt, out)
        sensor = attach_sensor_result(receipt, root, post_alert=False)
        if sensor.get('status') != 'PASS' or sensor.get('failure_count') != 0:
            raise ValueError('identical_inputs_deploy_completeness_sensor_nonpass')
        if path_digest(root) != baseline['release_tree_sha256']:
            raise ValueError('identical_inputs_release_tree_drift_during_validation')
        recheck = report / 'post-validation-inputs'
        recheck.mkdir(exist_ok=True)
        if fresh_release_inputs(config, recheck, pr) != current:
            raise ValueError('identical_inputs_changed_during_validation')
        out.update(status='SKIPPED_IDENTICAL_INPUTS', sensor_result=sensor)
        job.save(receipt, out)
        return out
    except Exception as exc:
        job.save(report / 'MAIN-RELEASE-RESULT.json', {'status': 'FAIL', 'scope': SCOPE,
                 'phase': 'identical_inputs_validation', 'error': str(exc)[:2000]})
        raise


def record_successful_inputs(report, result, pointer):
    """A prepared/failed run can never become the skip baseline."""
    import glp_affiliate_sync_job as job
    from glp_release_inputs import path_digest
    snapshot = report / 'BUILD-INPUTS.json'
    if result.get('status') != 'PASS_PRODUCTION_TERMINAL' or not snapshot.is_file():
        return None
    root = Path(pointer['release_root'])
    if root != report / 'prepared/release':
        raise ValueError('successful_input_release_root_mismatch')
    body = {'status': 'PASS_PRODUCTION_TERMINAL', 'inputs': job.read(snapshot),
            'pointer': pointer, 'release_tree_sha256': path_digest(root)}
    # Retain concrete derived date/taxonomy provenance as well as its full
    # pre-build source/data/environment closure, never backfill old receipts.
    body['derived_build_provenance'] = [
        {'file': str(p.relative_to(root)), 'sha256': job.sha(p)}
        for p in (root / 'artifacts/ops/public-date-provenance.json',
                  root / 'artifacts/ops/build-report.json', root / 'data/page-content-provenance.json')
        if p.is_file()]
    job.save(report / 'SUCCESSFUL-INPUTS.json', body)
    return body


def build_storage_status():
    import shutil
    usage = shutil.disk_usage('/')
    return {'total_bytes': usage.total, 'used_bytes': usage.used, 'available_bytes': usage.free,
            'blocked': usage.used * 100 >= usage.total * 92, 'policy': 'STOP92'}


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


def main_pre_effect_refusal(report):
    """Recognize only the native consumed-head refusal before client creation."""
    try:
        failure = json.loads((report / 'FAILURE.json').read_text())
        intent = json.loads((report / 'publish-intent.json').read_text())
        prepared_path = report / 'prepared/PREPARED.json'
        if (failure.get('status') != 'FAIL' or failure.get('scope') != SCOPE
                or failure.get('error') != 'current_release_consumed_or_binding_changed'
                or intent['prepared']['path'] != str(prepared_path)):
            return False
        import hashlib
        if hashlib.sha256(prepared_path.read_bytes()).hexdigest() != intent['prepared']['sha256']:
            return False
        prepared = json.loads(prepared_path.read_text())
        receipt = json.loads((ROOT / 'reports/glp-clinic-scout/seo-machine' /
                              ('current-release-' + prepared['site_head']) / 'CURRENT-RECEIPT.json').read_text())
        # This branch is before Cloudflare client construction. A matching ACTIVE
        # operation can have effects and is never a recovery grant.
        return (receipt.get('status') == 'PASS_PRODUCTION_TERMINAL'
                and receipt['binding']['prepared_manifest_sha256'] != intent['prepared']['sha256'])
    except (OSError, ValueError, KeyError, TypeError):
        return False


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
            if not (intent.parent / 'PUBLISH.json').exists() and not main_pre_effect_refusal(intent.parent):
                raise RuntimeError('prior_publication_uncertain:' + str(intent))
        main_sha = a.sha or site_main_sha()
        report = RUNS / ('main-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        report.mkdir(parents=False, exist_ok=False)
        if a.prepare_only:
            pr = merged_pr_for(main_sha)
        else:
            acceptance = ensure_main_acceptance(main_sha)
            job.save(report / 'MAIN-ACCEPTANCE.json', acceptance)
            if acceptance['status'] != 'ACCEPTED':
                out = {**acceptance, 'scope': SCOPE, 'report': str(report),
                       'run_type': os.environ.get('GLP_RUN_TYPE', 'manual')}
                job.save(report / 'MAIN-RELEASE-RESULT.json', out)
                print(json.dumps(out))
                return 0
            main_sha, pr = acceptance['main_sha'], acceptance['pr']
        current_head, current_deployment = pointer_head()
        pointer = json.loads((ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json').read_text())
        base_root = Path(pointer['release_root'])
        rebuildable = job.rebuild_inputs_complete(base_root) or bool(job.equivalent_main_rebuild_inputs(base_root))
        if a.prepare_only and current_head in (main_sha, pr['headRefOid']) and rebuildable:
            # Prepare-only retains its no-external-effects contract.
            out = {'status': 'PASS_ALREADY_CURRENT', 'scope': SCOPE, 'site_main': main_sha,
                   'deployment_id': current_deployment, 'search_submission': None, 'prepare_only': True}
            job.save(report / 'MAIN-RELEASE-RESULT.json', out)
            print(json.dumps(out))
            return 0
        config = job.read(job.CONFIG)
        if not a.prepare_only:
            skipped = try_identical_inputs(config, report, pointer, pr)
            if skipped:
                from glp_release_search_notify import submit_deployment
                skipped['search_submission'] = submit_deployment(pointer, Path(pointer['supersedes']['release_root']))
                job.save(report / 'MAIN-RELEASE-RESULT.json', skipped)
                # Keep the registered shell's existing success-silence protocol;
                # the authoritative receipt above retains the exact skip status.
                print(json.dumps({**skipped, 'status': 'PASS_ALREADY_CURRENT',
                                  'receipt_status': skipped['status']}))
                return 0
        storage = build_storage_status()
        if storage['blocked']:
            out = {'status': 'DEFERRED_STOP92', 'scope': SCOPE, 'site_main': main_sha,
                   'storage': storage, 'report': str(report), 'build_executed': False,
                   'run_type': os.environ.get('GLP_RUN_TYPE', 'manual')}
            job.save(report / 'MAIN-RELEASE-RESULT.json', out)
            print(json.dumps(out))
            return 0
        events = []
        log = lambda row: (events.append(row), job.save(report / 'MAIN-RELEASE-EVENTS.json', events))
        job.save(report / 'MAIN-RELEASE-INTENT.json', {'scope': SCOPE, 'authority': AUTHORITY, 'site_main': main_sha,
                                                       'source_pr': pr['url'], 'source_head': pr['headRefOid'],
                                                       'predecessor_deployment_id': current_deployment})
        original_gate = job.build_gate

        def gated(source, rep, config, head):
            from glp_release_inputs import capture
            # A fresh clone must have no hidden ignored/untracked inputs at the
            # build boundary. Its entire tracked source is the accepted Git tree.
            if job.run(['git', 'status', '--porcelain', '--untracked-files=all', '--ignored'], cwd=source):
                raise ValueError('main_build_unsealed_source_inputs')
            tree = job.run(['git', 'rev-parse', 'HEAD^{tree}'], cwd=source)
            before = capture(config, source_tree=tree,
                             sheet_sha256=job.sha(rep / 'registry/sheet-snapshot.json'))
            job.save(rep / 'BUILD-INPUTS.json', before)
            original_gate(source, rep, config, head)
            after = capture(config, source_tree=tree,
                            sheet_sha256=job.sha(rep / 'registry/sheet-snapshot.json'))
            if before != after:
                raise ValueError('main_build_inputs_changed_during_build')
            log({'secrets_scan': secrets_scan(Path(source) / 'public')})
        job.build_gate = gated
        import glp_affiliate_release as release
        # Workflow v3: fresh build from main; the stale PREPARED-chain re-authentication is skipped,
        # terminal-pointer identity + live-head check + rollback + pointer CAS remain.
        release.authenticated_current_base = release.main_release_base
        carrier.MAIN_RELEASE_FRESH = True
        install_cache_convergence(carrier, make_purger(carrier.read_token, log), log)
        try:
            result = job.execute(config, report, source_ref=pr['headRefOid'], source_pr=pr['url'],
                                 publish=not a.prepare_only, source_only=False)
        except Exception as exc:
            job.save(report / 'FAILURE.json', {'status': 'FAIL', 'scope': SCOPE, 'error': str(exc)[:2000],
                                               'run_type': os.environ.get('GLP_RUN_TYPE', 'manual')})
            raise
        out = {'status': result.get('status'), 'scope': SCOPE, 'site_main': main_sha, 'source_pr': pr['url'],
               'deployment_id': pointer_head()[1], 'predecessor_deployment_id': current_deployment, 'report': str(report)}
        notification_failed = False
        if result.get('status') == 'PASS_PRODUCTION_TERMINAL' and not a.prepare_only:
            record_successful_inputs(report, result, job.read(
                ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'))
            # Publication is already terminal; never roll it back or redispatch
            # an article because an indexing notification failed.
            from glp_release_search_notify import submit_deployment, current_pointer
            try:
                out['search_submission'] = submit_deployment(current_pointer(), base_root)
            except Exception as exc:
                notification_failed = True
                out['search_submission'] = {'status': 'BLOCKED_SEARCH_NOTIFICATION', 'error': str(exc)[:1000]}
        job.save(report / 'MAIN-RELEASE-RESULT.json', out)
        print(json.dumps(out))
        return 2 if notification_failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
