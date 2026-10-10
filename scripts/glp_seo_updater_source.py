#!/usr/bin/env python3
"""Weekly-governed updater: review -> site-source PR; NEVER deploy here.

The hourly main-release job is the sole release owner. A staged PR is not live.
The old n8n/direct publication path is not reachable through this entry point.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path('/home/ubuntu/business')
SCRIPTS = Path(__file__).resolve().parents[1] / 'systems/seo-blog-machine/scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from weekly_strategy_consumer import load_strategy
from seo_weekly_governance import CADENCE
import existing_article_updates as updates
import r2_update_carrier as r2
from carrier_lock import carrier_lock
import glp_seo_updater_run as legacy
import article_source_carrier as source

OUT = ROOT / 'reports/glp-clinic-scout/seo-machine/source-updater'

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + '\n')
    return path

def today():
    return dt.datetime.now(ZoneInfo('America/Los_Angeles')).date()

def verify_staged_retry(prior, current, predecessor, route, week):
    """A source-only retry cannot replace an already changed published base."""
    if (prior.get('status') != 'PASS_SOURCE_UPDATE_STAGED'
            or prior.get('route') != route or prior.get('week_start') != week
            or prior.get('published') is not False
            or prior.get('review', {}).get('status') != 'PASS_OPUS_SEO_DRAFT'
            or prior.get('review', {}).get('publication_invoked') is not False
            or current != predecessor):
        raise RuntimeError('staged_retry_scope_or_published_predecessor_changed')


def plan(retry_route=None):
    strategy, meta = load_strategy(as_of=today())
    config = legacy.load_config(r2.DEFAULT_CONFIG)
    resolution = config['_current_content_base_resolution']
    legacy.verify_resolution(resolution)
    selection = updates.build_selection_receipt(database=r2.DEFAULT_DATABASE,
        release_root=Path(resolution['pointer']['release_root']), as_of=today())
    rows = selection['weekly_plan']['selected'] if strategy is not None else []
    # Count staged or live units across strategy revisions within the same week.
    week = meta.get('week_start')
    prior = [json.loads(p.read_text()) for p in OUT.glob('*/stage.json')]
    active = [r for r in prior if r.get('week_start') == week and r.get('status') == 'PASS_SOURCE_UPDATE_STAGED']
    staged = {r['route'] for r in active}
    rows = [r for r in rows if r['route'] not in staged or r['route'] == retry_route]
    if len(staged) >= CADENCE['meaningful_updates_max_per_week']:
        rows = [r for r in rows if r['route'] == retry_route and retry_route in staged]
    result = {'schema':'glp-weekly-governed-updater-plan-v1',
        'status':'PASS_GOVERNED_UPDATE_PLAN' if rows else 'PASS_UPDATE_NOOP',
        'strategy':meta, 'cadence':CADENCE, 'selected':rows,
        'staged_this_week':len(staged), 'published':False,
        'release_owner':'415ec5eba194', 'legacy_direct_path':False,
        'selection':selection}
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = save(OUT / ('plan-' + stamp) / 'plan.json', result)
    result['plan_path'] = str(target)
    return result

def review_target(operation):
    # Dormant legacy target required by materializer, never executed in this lane.
    return ROOT / 'site-system/flagship-v2/releases' / ('source-review-' + operation)


def stage(packet, route, replace_staged=None):
    with carrier_lock():
        prior = None
        predecessor = None
        if replace_staged:
            if len(replace_staged) != 24 or any(c not in '0123456789abcdef' for c in replace_staged):
                raise RuntimeError('staged_retry_operation_invalid')
            folder = OUT / replace_staged
            for name in ('stage.json', 'before-article.json'):
                path = folder / name
                if folder.is_symlink() or path.is_symlink() or not path.is_file():
                    raise RuntimeError('staged_retry_input_not_regular')
            prior = json.loads((folder/'stage.json').read_text())
            predecessor = json.loads((folder/'before-article.json').read_text())
            stored = prior.get('source_pr') or {}
            url = str(stored.get('url') or '')
            prefix = 'https://github.com/izaiahat/glp-site-candidate/pull/'
            if not url.startswith(prefix) or not url[len(prefix):].isdigit():
                raise RuntimeError('staged_retry_source_pr_invalid')
            remote = source.gh('pr','view',url,'--json','state,headRefOid,mergeCommit')
            if remote.get('state') != 'MERGED' or any(remote.get(k) != stored.get(k) for k in ('headRefOid','mergeCommit')):
                raise RuntimeError('staged_retry_source_pr_binding_changed')
        governed = plan(retry_route=route if prior else None)
        choices = [r for r in governed['selected'] if r['route'] == route]
        if len(choices) != 1:
            raise RuntimeError('route_not_current_week_governed_or_already_staged')
        assert_noop = governed['strategy'].get('applied') is not True
        if assert_noop:
            raise RuntimeError('current_week_strategy_unavailable')
        strategy_sha = governed['strategy']['updates']['sha256']
        revision = hashlib.sha256(b''.join((packet/name).read_bytes() for name in
            ('00-authority.json','01-evidence-ledger.json','02-serp-brief.json','03-editorial-brief.md','04-draft.md','05-claim-source-map.json'))).hexdigest()
        operation = hashlib.sha256((strategy_sha + route + revision).encode()).hexdigest()[:24]
        work = OUT / operation
        terminal = work / 'stage.json'
        if terminal.exists():
            return json.loads(terminal.read_text())
        if work.exists():
            raise RuntimeError('source_update_prior_attempt_requires_exact_readback:' + str(work))
        work.mkdir(parents=True)
        save(work / 'intent.json', {'route':route, 'strategy':governed['strategy'], 'published':False})
        legacy.assert_no_article_carrier_collision(route, work)
        before_live = legacy.live_snapshot(route, work / 'live', 'before')
        config = legacy.load_config(r2.DEFAULT_CONFIG)
        resolution = config['_current_content_base_resolution']
        release = Path(resolution['pointer']['release_root'])
        articles, _ = updates.load_release_articles(release)
        before = next(a for a in articles if a['canonical_path'] == route)
        if prior is not None:
            verify_staged_retry(prior, before, predecessor, route, governed['strategy']['week_start'])
            save(work/'staged-retry-binding.json', {'operation':replace_staged, 'source_pr':prior['source_pr'],
                'predecessor_unchanged':True, 'new_independent_review_required':True})
        save(work / 'before-article.json', before)
        weekly = r2.write_sealed(work / 'weekly-selection.json', governed['selection'], 'receipt_sha256')
        minted = r2.mint_candidate(choices[0], output_dir=work / 'authority', config=config,
            config_path=r2.DEFAULT_CONFIG, database=r2.DEFAULT_DATABASE, weekly_selection=weekly,
            resolution=resolution, current_routes={a['canonical_path'] for a in articles},
            striking_routes={route}, as_of=today(), slot=1)
        authpath = Path(minted['authority']['path'])
        selpath = Path(minted['selection']['path'])
        auth = json.loads(authpath.read_text())
        sel = json.loads(selpath.read_text())
        target = work / 'packet'
        legacy.rebind_packet(packet, target, auth, authpath, sel, selpath, work)
        from quote_display_repair import normalize_zepbound_display
        normalize_zepbound_display(target)
        run_id = 'source-update-' + operation
        _, materialized, _ = legacy.run_child('materialize-source-update',
            [str(legacy.PYTHON), str(legacy.MATERIALIZER), '--packet', str(target),
             '--topic-id', minted['topic_id'], '--route', route, '--run-id', run_id,
             '--release-root', str(review_target(operation)),
             '--publication-date', today().isoformat(), '--selection-receipt', str(selpath),
             '--r2-authority', str(authpath), '--operation', 'r2_refresh',
             '--materialization-reason', 'r2_refresh', '--review-transport', legacy.REVIEW_TRANSPORT,
             '--pt-date', today().isoformat()], cwd=ROOT, output_dir=work / 'children', timeout=600)
        article = json.loads((target / '06-article.json').read_text())
        if not updates.semantic_materiality(before, article)['material']:
            result={'schema':'glp-source-update-stage-v1','status':'PASS_NO_MATERIAL_UPDATE',
                    'route':route,'week_start':governed['strategy']['week_start'],
                    'strategy':governed['strategy'],'no_op':True,'published':False,
                    'date_modified_changed':False,'source_pr':None,
                    'effects':{'publishes':0,'deployments':0,'source_prs':0}}
            save(terminal,result)
            return result
        job = Path(materialized['review_job'])
        # Explicitly WITHOUT --apply: the independently reviewed subject cannot
        # invoke its legacy publisher. Source commit and hourly release follow.
        _, reviewed, _ = legacy.run_child('independent-review-no-publish',
            [str(legacy.PYTHON), str(legacy.REVIEW_LOOP), '--job', str(job)],
            cwd=ROOT, output_dir=work / 'children', timeout=7200)
        if reviewed.get('status') != 'PASS_OPUS_SEO_DRAFT' or reviewed.get('publication_invoked') is not False:
            raise RuntimeError('independent_review_not_pass')
        article = json.loads((target / '06-article.json').read_text())
        contract = updates.validate_r2_refresh(before, article,
            gsc_queries=auth['r2_contract']['gsc_queries'],
            current_routes=set(auth['r2_contract']['current_routes']),
            striking_distance_routes=set(auth['r2_contract']['striking_distance_routes']),
            minimum_inbound_links=auth['r2_contract']['required_inbound_links'], priority=auth['priority'])
        if article.get('updated_at') != today().isoformat():
            raise RuntimeError('material_update_editorial_date_not_today')
        pass_receipt = Path(reviewed['rounds'][-1]['receipt'])
        entry = {'entry_id':operation, 'target_route':route,
            'pass_receipt':{'path':str(pass_receipt)}, 'before_sha256':updates.sha(work / 'before-article.json'),
            'weekly_strategy_sha256':strategy_sha, 'semantic_materiality':contract['semantic_materiality']}
        pr = stage_source(work / 'source', target, entry, before)
        result = {'schema':'glp-source-update-stage-v1','status':'PASS_SOURCE_UPDATE_STAGED',
            'route':route, 'week_start':governed['strategy']['week_start'], 'strategy':governed['strategy'],
            'source_pr':pr, 'review':reviewed, 'materiality':contract, 'before_live':before_live,
            'published':False, 'release_owner':'415ec5eba194', 'packet':str(target)}
        save(terminal, result)
        return result

def stage_source(workspace, packet, entry, before):
    branch = 'updater-' + entry['entry_id']
    source.run(['git','clone','git@github.com:izaiahat/glp-site-candidate.git',workspace], timeout=300)
    source.run(['git','checkout','-b',branch], cwd=workspace)
    manifest_path = workspace / 'data/blog/accepted-guides/updates.json'
    manifest = json.loads(manifest_path.read_text())
    route = entry['target_route']
    slug = route.strip('/').split('/')[-1] + '-update-' + entry['entry_id'][:8]
    rel = Path('data/blog/accepted-guides') / slug
    specs = {}
    for key, original in {'article':packet/'06-article.json', 'qa':packet/'07-qa-receipt.json',
                          'review':Path(entry['pass_receipt']['path'])}.items():
        target = workspace / rel / (key + '.json')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original.read_bytes())
        specs[key] = {'path':str(rel / (key + '.json')), 'sha256':source.digest(target)}
    # Bind the immutable site catalog predecessor, not a generated HTML tree.
    prior = next((r for r in manifest['entries'] if r['route'] == route), None)
    if prior:
        predecessor_sha = prior['before_sha256']
    else:
        originals = []
        for name in ('content-batch.json','content-national-wow-additions.json'):
            originals += json.loads((workspace/'data/blog'/name).read_text())['articles']
        originals += [json.loads((workspace/r['article']['path']).read_text())
                      for r in source.source_rows(workspace)]
        matching = [a for a in originals if a['canonical_path'] == route and a['article_id'] == before['article_id']]
        if len(matching) != 1:
            raise RuntimeError('source_predecessor_identity_invalid')
        predecessor_sha = updates.payload_hash(matching[0], '__unused')
    row = {'entry_id':entry['entry_id'], 'route':route, 'before_sha256':predecessor_sha,
           'weekly_strategy_sha256':entry['weekly_strategy_sha256'],
           'semantic_materiality':entry['semantic_materiality'], **specs}
    manifest['entries'] = [r for r in manifest['entries'] if r['route'] != route] + [row]
    save(manifest_path, manifest)
    source.run(['git','add','data/blog/accepted-guides'], cwd=workspace)
    source.run(['git','commit','-m','Reviewed meaningful update ' + route], cwd=workspace)
    source.run(['git','push','origin','HEAD'], cwd=workspace)
    source.run(['gh','pr','create','--repo',source.REPO,'--head',branch,'--title','Updater: '+route,
        '--body','Current weekly strategy '+entry['weekly_strategy_sha256']+'. Exact QA/five-gate PASS; semantic materiality; no direct release. Hourly carrier owns publication.'], timeout=90)
    # Reuse the existing exact-head CI wait/merge protocol without releasing.
    pr = source.gh('pr','view',branch,'--repo',source.REPO,'--json','url,headRefOid')
    import time
    for _ in range(24):
        state = source.gh('pr','view',pr['url'],'--json','headRefOid,statusCheckRollup')
        if state['headRefOid'] != pr['headRefOid']:
            raise RuntimeError('source_head_changed')
        if state.get('statusCheckRollup'):
            break
        time.sleep(5)
    source.run(['gh','pr','checks',pr['url'],'--watch','--interval','10'], timeout=1800)
    state = source.gh('pr','view',pr['url'],'--json','headRefOid,statusCheckRollup')
    if state['headRefOid'] != pr['headRefOid'] or not state.get('statusCheckRollup') or any(c.get('conclusion') != 'SUCCESS' for c in state['statusCheckRollup']):
        raise RuntimeError('source_exact_head_ci_not_green')
    source.run(['gh','pr','merge',pr['url'],'--merge','--match-head-commit',pr['headRefOid']], timeout=90)
    return source.gh('pr','view',pr['url'],'--json','url,state,headRefOid,mergeCommit')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['plan','stage'])
    parser.add_argument('--packet', type=Path)
    parser.add_argument('--route')
    parser.add_argument('--replace-staged', help='Exact unpublished staged operation; requires unchanged published predecessor and a fresh review')
    args = parser.parse_args()
    if args.mode == 'stage' and (not args.packet or not args.route):
        parser.error('stage requires --packet and --route')
    result = plan() if args.mode == 'plan' else stage(args.packet.resolve(), args.route, args.replace_staged)
    if args.mode == 'plan':
        result = {key:value for key,value in result.items() if key != 'selection'}
        result['strategy'] = {key:value for key,value in result['strategy'].items() if key != 'source_observations'}
    print(json.dumps(result, sort_keys=True))

if __name__ == '__main__':
    main()
