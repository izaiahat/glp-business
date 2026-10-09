#!/usr/bin/env python3
"""Publish sealed, independently accepted packets as reviewed site main source.

Never edits frozen catalogs/image manifests. The native main release retains
secrets, held-provider census, claim freshness, pointer CAS and rollback gates.
A deterministic source branch and immutable consumption permit crash resume.
"""
from __future__ import annotations
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path('/home/ubuntu/business')
SCRIPTS = Path(__file__).resolve().parents[1] / 'systems/seo-blog-machine/scripts'
sys.path.insert(0, str(SCRIPTS))
import article_buffer as buffer
from carrier_lock import carrier_lock

SOURCE_REL = Path('data/blog/accepted-guides')
REPO = 'izaiahat/glp-site-candidate'
SITE = 'https://glpclinicscout.com'


def run(argv, *, cwd=None, timeout=900, lock_fd=None):
    proc = subprocess.run([str(x) for x in argv], cwd=cwd, capture_output=True,
                          text=True, timeout=timeout,
                          pass_fds=(lock_fd,) if lock_fd is not None else ())
    if proc.returncode:
        raise RuntimeError(f'source_child_failed:{argv}:rc={proc.returncode}\n{proc.stdout[-16000:]}\n{proc.stderr[-16000:]}')
    return proc.stdout


def gh(*args):
    return json.loads(run(['gh', *args]))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_rows(source):
    manifest = json.loads((source / SOURCE_REL / 'manifest.json').read_text())
    if manifest.get('schema') != 'glp-accepted-guides-source-v1':
        raise RuntimeError('source_manifest_invalid')
    return manifest['entries']


def land_packet(source, packet, entry):
    route = entry['target_route']
    if not re.fullmatch(r'/guides/[a-z0-9-]+/', route):
        raise RuntimeError('source_route_invalid')
    rel = SOURCE_REL / route.strip('/').split('/')[-1]
    specs = {}
    inputs = {'article':packet / '06-article.json', 'qa':packet / '07-qa-receipt.json',
              'review':Path(entry['pass_receipt']['path'])}
    for key, path in inputs.items():
        specs[key] = {'path':(rel / f'{key}.json').as_posix(), 'sha256':digest(path)}
    row = {'entry_id':entry['entry_id'], 'route':route,
           'decorative_image_policy':'existing-approved-editorial-evidence-hero; no new generation or shared-manifest mutation', **specs}
    manifest_path = source / SOURCE_REL / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    existing = [r for r in manifest['entries'] if r['route'] == route or r['entry_id'] == entry['entry_id']]
    if existing:
        if existing != [row] or any(digest(source / spec['path']) != spec['sha256'] for spec in specs.values()):
            raise RuntimeError('source_identity_drift')
        return False
    for key, path in inputs.items():
        target = source / specs[key]['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    manifest['entries'].append(row)
    manifest_path.write_text(json.dumps(manifest,indent=2) + '\n')
    return True


def accepted_entry(root, source, row):
    manifest = root / 'entries' / row['entry_id'] / 'entry.json'
    entry = buffer.validate_entry(manifest,current_routes=set(),current_topic_ids=set())
    packet = manifest.parent / 'packet'
    article = json.loads((packet / '06-article.json').read_text())
    for key, path in {'article':packet / '06-article.json', 'qa':packet / '07-qa-receipt.json',
                      'review':Path(entry['pass_receipt']['path'])}.items():
        spec = row[key]
        if digest(path) != spec['sha256'] or digest(source / spec['path']) != spec['sha256']:
            raise RuntimeError('source_packet_identity_drift')
    if entry['target_route'] != row['route'] or article['canonical_path'] != row['route']:
        raise RuntimeError('source_route_identity_drift')
    return manifest, entry, article


def fetch(url):
    # Exact publisher route only; never fetch tracking/affiliate links.
    out = run(['curl','--fail-with-body','--silent','--show-error','--max-time','60',
               '--write-out','\n%{http_code}',url],timeout=90)
    body, code = out.rsplit('\n',1)
    return int(code), body


def live_readback(route,title):
    url = SITE + route
    code, body = fetch(url)
    match = re.search(r'<title>(.*?)</title>',body,re.S|re.I)
    actual = html.unescape(match[1]).strip() if match else None
    if code != 200 or actual != title:
        raise RuntimeError(f'live_title_mismatch:{code}:{actual!r}:{title!r}')
    robots = re.findall(r'<meta\b[^>]*name=[\"\']robots[\"\'][^>]*>',body,re.I)
    if not robots or any(re.search(r'noindex|nofollow',tag,re.I) for tag in robots):
        raise RuntimeError('live_indexability_invalid')
    if f'href="{url}"' not in body:
        raise RuntimeError('live_canonical_missing')
    return {'url':url,'http':code,'title':actual,'html_sha256':hashlib.sha256(body.encode()).hexdigest()}


def consumed_path(root,operation_id):
    return root / 'consumed' / (hashlib.sha256(operation_id.encode()).hexdigest() + '.json')


def record_consumption(root, entry, operation_id, publication, claim=None):
    target = consumed_path(root,operation_id)
    if target.exists():
        return buffer._read_sealed(target,buffer.CONSUME_SCHEMA,'consumption_sha256')
    receipt = publication.get('receipt')
    if not receipt and (publication.get('release') or {}).get('report'):
        receipt = str(Path(publication['release']['report']) / 'RESULT.json')
    if not receipt:
        pointer = json.loads((ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json').read_text())
        receipt = pointer['final_receipt']['path']
    publication = {**publication, 'receipt': receipt}
    payload = {'schema':buffer.CONSUME_SCHEMA,'status':'PASS_BUFFER_PUBLISHED',
               'operation_id':operation_id,'entry_id':entry['entry_id'],
               'topic_id':entry['topic_id'],'target_route':entry['target_route'],
               'publication':publication,'publication_lane':'accepted-site-main-source'}
    if claim:
        payload['claim'] = buffer._spec(claim)
    buffer._write_once(target,payload,'consumption_sha256')
    return buffer._read_sealed(target,buffer.CONSUME_SCHEMA,'consumption_sha256')


def reconcile_published(root, release):
    """Seal only source-bound, live exact titles; never publish during recovery."""
    reconciled = []
    for row in source_rows(release):
        if not (root / 'entries' / row['entry_id'] / 'entry.json').is_file():
            continue
        manifest, entry, article = accepted_entry(root,release,row)
        live = live_readback(entry['target_route'],article['title'])
        pointer = json.loads((ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json').read_text())
        publication = {'status':'PASS_BUFFER_PUBLISHED','source_reconciled':True,
                       'source_entry':row,'current_base':buffer._spec(ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'),
                       'deployment_id':pointer.get('deployment_id'),'live':live}
        record_consumption(root,entry,'source-reconciled-' + entry['entry_id'],publication)
        for claim_path, claim in buffer._claim_rows(root):
            if claim['entry_id'] == entry['entry_id']:
                record_consumption(root,entry,claim['operation_id'],publication,claim_path)
        reconciled.append({'entry_id':entry['entry_id'],**live})
    return {'status':'PASS_SOURCE_RECONCILED','entries':reconciled}


def validate_next(root, release, routes, topics):
    rows = buffer.ready_entries(root,current_routes=routes,current_topic_ids=topics)
    if not rows:
        raise buffer.BufferEmptyError('article_buffer_empty_or_invalid')
    manifest, entry = rows[0]
    # Admission pins are historical; authenticate the complete accepted subject
    # against the currently installed validation chain before the source effect.
    controls = {p:digest(Path(p)) for p in entry['control_files']}
    evidence = buffer.validate_admission_evidence(manifest.parent / 'packet',Path(entry['pass_receipt']['path']),controls)
    if any(evidence[k] != entry[k] for k in ('topic_id','article_id','target_route')):
        raise RuntimeError('source_acceptance_identity_invalid')
    return manifest, entry


def preflight():
    import article_buffer_cli as cli
    _, release, routes, topics = cli.current_context()
    manifest, entry = validate_next(cli.DEFAULT_ROOT,release,routes,topics)
    return {'status':'PASS_SOURCE_MAIN_COMPATIBILITY','entry_id':entry['entry_id'],
            'route':entry['target_route'],'manifest':str(manifest),'release_root':str(release)}


def ensure_source_main(workspace, packet, entry):
    branch = 'carrier/accepted-' + entry['entry_id']
    if not workspace.exists():
        run(['git','clone','--quiet','--branch','main','--single-branch',f'git@github.com:{REPO}.git',workspace])
        run(['git','checkout','-b',branch],cwd=workspace)
    run(['git','fetch','origin','main'],cwd=workspace)
    # Read exact published branch first after any uncertain GitHub operation.
    prs = gh('pr','list','--repo',REPO,'--head',branch,'--state','all','--json','number,state,headRefOid,mergeCommit,url')
    if len(prs) > 1:
        raise RuntimeError('source_pr_identity_ambiguous')
    if prs and prs[0]['state'] == 'MERGED':
        merge = prs[0]['mergeCommit']['oid']
        run(['git','checkout','--detach',merge],cwd=workspace)
        land_packet(workspace,packet,entry)  # exact identity only, no second commit
        return prs[0]
    if prs and prs[0]['state'] == 'CLOSED':
        raise RuntimeError('source_pr_closed_without_merge')
    if not prs:
        run(['git','merge','--ff-only','origin/main'],cwd=workspace)
    changed = land_packet(workspace,packet,entry)
    if changed:
        run(['git','add',SOURCE_REL],cwd=workspace)
        run(['git','commit','-m',f'Publish accepted guide {entry["target_route"]}'],cwd=workspace)
    run(['git','push','origin',f'HEAD:refs/heads/{branch}'],cwd=workspace)
    if not prs:
        run(['gh','pr','create','--repo',REPO,'--base','main','--head',branch,
             '--title',f'Carrier: accepted guide {entry["target_route"]}',
             '--body',f'Registered host carrier. Exact independently accepted packet {entry["entry_id"]}; prose/QA/five-gate bytes unchanged. Native main release preserves secrets, Fella/provider holds, claim freshness, idempotency and rollback. No sends or n8n activation.'],timeout=90)
    pr = gh('pr','view',branch,'--repo',REPO,'--json','url,headRefOid')
    # GitHub may not have registered any checks when a new PR is returned.
    # Empty is UNKNOWN, not failure or green; wait boundedly on the exact head
    # before asking --watch (which exits immediately on an empty check set).
    deadline = time.monotonic() + 120
    while True:
        registered = gh('pr','view',pr['url'],'--json','headRefOid,statusCheckRollup')
        if registered.get('headRefOid') != pr['headRefOid']:
            raise RuntimeError('source_exact_head_ci_not_green')
        if registered.get('statusCheckRollup'):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('source_ci_registration_timeout')
        time.sleep(5)
    # Required existing site CI includes accepted-source contracts. The native
    # build and release safety gates run separately in glp_release_main.py.
    run(['gh','pr','checks',pr['url'],'--watch','--interval','10'],timeout=1800)
    readback = gh('pr','view',pr['url'],'--json','headRefOid,statusCheckRollup')
    checks = readback.get('statusCheckRollup') or []
    if readback['headRefOid'] != pr['headRefOid'] or not checks or any(c.get('conclusion') != 'SUCCESS' for c in checks):
        raise RuntimeError('source_exact_head_ci_not_green')
    run(['gh','pr','merge',pr['url'],'--merge','--match-head-commit',pr['headRefOid']],timeout=90)
    return gh('pr','view',pr['url'],'--json','url,state,headRefOid,mergeCommit')


def consume(args):
    import article_buffer_cli as cli
    with carrier_lock() as lock_fd:
        path = consumed_path(args.root,args.operation_id)
        if path.exists():
            return buffer._read_sealed(path,buffer.CONSUME_SCHEMA,'consumption_sha256')
        _, release, routes, topics = cli.current_context()
        reconcile_published(args.root,release)
        if path.exists():
            return buffer._read_sealed(path,buffer.CONSUME_SCHEMA,'consumption_sha256')
        claim_name = hashlib.sha256(args.operation_id.encode()).hexdigest() + '.json'
        claim = args.root / 'claims' / claim_name
        if claim.exists():
            claimed = buffer._read_sealed(claim,buffer.CLAIM_SCHEMA,'claim_sha256')
            manifest = buffer._verify_spec(claimed['entry_manifest'],'source_claim_entry')
            entry = buffer.validate_entry(manifest,current_routes=routes,current_topic_ids=topics)
        else:
            manifest, entry = validate_next(args.root,release,routes,topics)
            claim = buffer._claim_oldest_locked(root=args.root,operation_id=args.operation_id,
                         current_routes=routes,current_topic_ids=topics,base_state=cli.POINTER)
        controls = {p:digest(Path(p)) for p in entry['control_files']}
        evidence = buffer.validate_admission_evidence(manifest.parent / 'packet',Path(entry['pass_receipt']['path']),controls)
        if any(evidence[k] != entry[k] for k in ('topic_id','article_id','target_route')):
            raise RuntimeError('source_acceptance_identity_invalid')
        workspace = args.work_root / ('source-' + entry['entry_id'])
        workspace.parent.mkdir(parents=True,exist_ok=True)
        pr = ensure_source_main(workspace,manifest.parent / 'packet',entry)
        if pr.get('state') != 'MERGED':
            raise RuntimeError('source_merge_not_observed')
        output = run([sys.executable,ROOT / 'scripts/glp_release_main.py','--sha',pr['mergeCommit']['oid']],timeout=3600,lock_fd=lock_fd)
        result = cli.json_from_output(output)
        if result.get('status') not in {'PASS_PRODUCTION_TERMINAL','PASS_ALREADY_CURRENT'}:
            raise RuntimeError('source_release_not_terminal:' + json.dumps(result))
        article = json.loads((manifest.parent / 'packet/06-article.json').read_text())
        live = live_readback(entry['target_route'],article['title'])
        _, current, current_routes, _ = cli.current_context()
        if entry['target_route'] not in current_routes:
            raise RuntimeError('source_release_sitemap_missing')
        row = next((r for r in source_rows(current) if r['entry_id'] == entry['entry_id']),None)
        if row is None:
            raise RuntimeError('source_release_identity_missing')
        accepted_entry(args.root,current,row)
        publication = {'status':'PASS_BUFFER_PUBLISHED','source_pr':pr,'main_release':result,'live':live}
        return record_consumption(args.root,entry,args.operation_id,publication,claim)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['preflight','reconcile'])
    args = p.parse_args()
    if args.mode == 'preflight':
        result = preflight()
    else:
        import article_buffer_cli as cli
        with carrier_lock():
            _,release,_,_ = cli.current_context()
            result = reconcile_published(cli.DEFAULT_ROOT,release)
    print(json.dumps(result,sort_keys=True))

if __name__ == '__main__':
    main()
