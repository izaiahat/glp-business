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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'systems/analytics-brain'))
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

def plan():
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
    rows = [r for r in rows if r['route'] not in staged]
    if len(active) >= CADENCE['meaningful_updates_max_per_week']:
        rows = []
    result = {'schema':'glp-weekly-governed-updater-plan-v1',
        'status':'PASS_GOVERNED_UPDATE_PLAN' if rows else 'PASS_UPDATE_NOOP',
        'strategy':meta, 'cadence':CADENCE, 'selected':rows,
        'staged_this_week':len(active), 'published':False,
        'release_owner':'415ec5eba194', 'legacy_direct_path':False,
        'selection':selection}
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = save(OUT / ('plan-' + stamp) / 'plan.json', result)
    result['plan_path'] = str(target)
    return result

def review_target(operation):
    # Dormant legacy target required by materializer, never executed in this lane.
    return ROOT / 'site-system/flagship-v2/releases' / ('source-review-' + operation)


def stage(packet, route):
    with carrier_lock():
        governed = plan()
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

# Independently accepted site PR77 / RV77 review5512946524, exact source head.
# Canonical complete oral-guide row (including all three actual site preimages),
# NOT the original buffer's article/QA/review hashes. No caller may repin it.
ACCEPTED_SUCCESSOR_SOURCE_HEAD = 'ea07aa33ea6167081b854de3372722988d9c3569'
ACCEPTED_SUCCESSOR_SOURCE_ROW_SHA256 = '089cdb8108b0706fde1d60a3cd15350fd5c455c5cb114b9346d6b4f2c3842c5d'


# Savings predecessor present at independently accepted RV116 source head
# (review5536530872). Pin its immutable manifest AND complete row, never caller
# history/rehashes. Other current manifest rows may legitimately have advanced.
ACCEPTED_SAVINGS_SOURCE_HEAD = '34703bc33b19dd625d5dcc8293f01b243e3b3d64'
ACCEPTED_SAVINGS_MANIFEST_SHA256 = '51aa0559ab8d22cd75c711d32f4fb6d99c4d5b3fe28341f6220a74c90d320900'
ACCEPTED_SAVINGS_SOURCE_ROW_SHA256 = '5a3ed72b028fc2952046dd754781145e81ea2ea6219a6d48293a85bd654fcd2f'


def adopt_successor(workspace, entry_path, packet, history, *, expected_head,
                    root=ROOT, source_author_packet=None, source_author_review=None,
                    review_only_job=None):
    # Same serialization as ordinary stage and every content-base mutation;
    # admission covers reads, native authentication, final recheck and writes.
    with carrier_lock():
        return _adopt_successor_locked(workspace, entry_path, packet, history,
            expected_head=expected_head, root=root, source_author_packet=source_author_packet,
            source_author_review=source_author_review, review_only_job=review_only_job)


def _adopt_successor_locked(workspace, entry_path, packet, history, *, expected_head,
                            root, source_author_packet, source_author_review, review_only_job=None):
    """Stage retained authenticated successor inputs in private Git source only.

    No review invocation, commit/PR/merge, build, live read or recovery seal.
    The full recovery consumer still requires exact deployed and live bytes.
    """
    import re
    import article_review_recovery as recovery
    workspace = workspace.resolve(strict=True)
    if (source.run(['git', 'rev-parse', '--show-toplevel'], cwd=workspace).strip() != str(workspace)
        or source.run(['git', 'rev-parse', 'HEAD'], cwd=workspace).strip() != expected_head
        or source.run(['git', 'status', '--porcelain'], cwd=workspace).strip()):
        raise RuntimeError('successor_source_checkout_drift')
    # Capture buffers before authentication, then authenticate those exact bytes.
    # Do not reread mutable entry/context to derive the adoption after handoff.
    entry_bytes = entry_path.read_bytes()
    context_path = packet / recovery.PUBLICATION_CONTEXT
    context_bytes = None if review_only_job else context_path.read_bytes()
    manifest_path = workspace / 'data/blog/accepted-guides' / ('manifest.json' if review_only_job else 'updates.json')
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if review_only_job:
        recovery.require(not source_author_packet and not source_author_review,
                         'corrected_successor_job_is_sole_author_binding')
        validated = recovery.corrected_successor_documents(entry_path, history, packet, review_only_job, root=root)
        doc = validated['document']
    else:
        doc = recovery.successor_documents(entry_path, history, packet, root=root,
            source_author_packet=source_author_packet, source_author_review=source_author_review)
        validated = recovery.validate_successor_documents(entry_path, doc, root=root)
    pins = {r['path']: r['sha256'] for r in validated['manifest']['subject_files']}
    recovery.require(hashlib.sha256(entry_bytes).hexdigest() == doc['entry']['sha256']
                     and (review_only_job or hashlib.sha256(context_bytes).hexdigest() == pins[recovery.PUBLICATION_CONTEXT]),
                     'successor_source_authenticated_input_drift')
    entry = json.loads(entry_bytes)
    context = None if review_only_job else json.loads(context_bytes)
    rows = [r for r in manifest['entries'] if r['route'] == entry['target_route']]
    schema = 'glp-accepted-guides-source-v1' if review_only_job else 'glp-accepted-guide-updates-v1'
    recovery.require(manifest['schema'] == schema and len(rows) == 1,
                     'successor_source_row_invalid')
    row = rows[0]
    row_digest = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(',', ':'),
                                         ensure_ascii=False).encode()).hexdigest()
    if review_only_job:
        accepted_bytes = source.run(['git', 'show', f'{ACCEPTED_SAVINGS_SOURCE_HEAD}:data/blog/accepted-guides/manifest.json'], cwd=workspace).encode()
        recovery.require(hashlib.sha256(accepted_bytes).hexdigest() == ACCEPTED_SAVINGS_MANIFEST_SHA256
                         and row_digest == ACCEPTED_SAVINGS_SOURCE_ROW_SHA256
                         and row in json.loads(accepted_bytes)['entries'],
                         'successor_source_accepted_preimage_drift')
        recovery.require(validated['job']['postpublication_binding']['deployed_source'] == row,
                         'successor_source_deployed_binding_drift')
    else:
        recovery.require(context['source_head'] == ACCEPTED_SUCCESSOR_SOURCE_HEAD
                         and row_digest == ACCEPTED_SUCCESSOR_SOURCE_ROW_SHA256,
                         'successor_source_accepted_preimage_drift')
    preimages = {manifest_path: manifest_bytes}
    for key in ('article', 'qa', 'review'):
        relative = row[key]['path']
        recovery.require(re.fullmatch(r'data/blog/accepted-guides/[a-z0-9-]+/[a-z0-9_.-]+\.json', relative)
                         and not any(p.is_symlink() for p in [(workspace / relative), *(workspace / relative).parents]
                                     if p.is_relative_to(workspace))
                         and (workspace / relative).resolve(strict=True).is_relative_to(workspace),
                         'successor_source_path_invalid')
        preimage_path = workspace / relative
        preimages[preimage_path] = preimage_path.read_bytes()
        recovery.require(hashlib.sha256(preimages[preimage_path]).hexdigest() == row[key]['sha256'],
                         'successor_source_preimage_drift')
    article_bytes = (packet / '06-article.json').read_bytes()
    before, article = json.loads(preimages[workspace / row['article']['path']]), json.loads(article_bytes)
    recovery.require(before['article_id'] == article['article_id'] == entry['article_id']
                     and before['canonical_path'] == article['canonical_path'] == row['route'],
                     'successor_source_identity_invalid')
    if not review_only_job:
        recovery.require(row.get('first_publication') == context['first_publication'],
                         'successor_source_publication_context_drift')
    originals = {'article': packet / '06-article.json', 'qa': packet / '07-qa-receipt.json',
                 'review': recovery.checked(doc['review'])}
    expected = {'article': pins['06-article.json'], 'qa': pins['07-qa-receipt.json'],
                'review': doc['review']['sha256']}
    reviewed_subject_specs = {packet/name: digest for name, digest in pins.items()}
    reviewed_subject_specs[originals['review']] = expected['review']
    content = {key: original.read_bytes() for key, original in originals.items()}
    recovery.require(all(hashlib.sha256(content[key]).hexdigest() == expected[key] for key in content),
                     'successor_source_reviewed_bytes_drift')
    rel = Path(row['article']['path']).parent
    specs = {key: {'path': (rel / (key + '-successor-' + expected[key] + '.json')).as_posix(),
                   'sha256': expected[key]} for key, original in originals.items()}
    # All refusal checks precede source writes; never overwrite predecessor bytes.
    for spec in specs.values():
        target = workspace / spec['path']
        recovery.require(not target.is_symlink() and (not target.exists() or source.digest(target) == spec['sha256']),
                         'successor_source_destination_drift')
    # Final pre-effect validation while the genuine carrier owner is held.
    recovery.require(entry_path.read_bytes() == entry_bytes
                     and (review_only_job or context_path.read_bytes() == context_bytes),
                     'successor_source_authenticated_input_drift')
    # Git reads are also handoff seams: check every authenticated dependency
    # AFTER them, immediately before entering the first source-write loop.
    if (source.run(['git', 'rev-parse', 'HEAD'], cwd=workspace).strip() != expected_head
        or source.run(['git', 'status', '--porcelain'], cwd=workspace).strip()):
        raise RuntimeError('successor_source_checkout_drift')
    recovery.require(entry_path.read_bytes() == entry_bytes
                     and (review_only_job or context_path.read_bytes() == context_bytes),
                     'successor_source_authenticated_input_drift')
    recovery.require(all(not path.is_symlink() and path.read_bytes() == data
                         for path, data in preimages.items()), 'successor_source_preimage_drift')
    recovery.require(all(not path.is_symlink() and path.is_file() and source.digest(path) == digest
                         for path, digest in reviewed_subject_specs.items()),
                     'successor_source_reviewed_bytes_drift')
    recovery.require(all(not Path(value['path']).is_symlink() and Path(value['path']).is_file()
                         and source.digest(Path(value['path'])) == value['sha256']
                         for value in validated.get('authenticated_inputs', [])),
                     'successor_source_authenticated_input_drift')
    for key, original in originals.items():
        target = workspace / specs[key]['path']
        if not target.exists():
            with target.open('xb') as stream:
                stream.write(content[key])
    row.update(specs)  # Preserve first-publication, materiality, predecessor and all unrelated rows.
    save(manifest_path, manifest)
    return {'status': 'SUCCESSOR_SOURCE_STAGED_NOT_ACCEPTED', 'route': row['route'],
            'source_head': expected_head, 'bindings': specs, 'published': False,
            'scope': 'retained_native_subjects_only_not_design_fullpackage_or_recovery_acceptance'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['plan','stage','adopt-successor'])
    parser.add_argument('--packet', type=Path)
    parser.add_argument('--route')
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--entry', type=Path)
    parser.add_argument('--history', type=Path)
    parser.add_argument('--expected-source-head')
    parser.add_argument('--source-author-packet', type=Path)
    parser.add_argument('--source-author-review', type=Path)
    parser.add_argument('--review-only-job', type=Path,
                        help='Existing consumed nine-subject corrected-successor job; no new review')
    args = parser.parse_args()
    if args.mode == 'stage' and (not args.packet or not args.route):
        parser.error('stage requires --packet and --route')
    if args.mode == 'adopt-successor':
        if not all((args.workspace, args.entry, args.packet, args.history, args.expected_source_head)):
            parser.error('adopt-successor requires --workspace --entry --packet --history --expected-source-head')
        result = adopt_successor(args.workspace, args.entry.resolve(strict=True),
            args.packet.resolve(strict=True), args.history.resolve(strict=True),
            expected_head=args.expected_source_head, source_author_packet=args.source_author_packet,
            source_author_review=args.source_author_review, review_only_job=args.review_only_job)
    else:
        result = plan() if args.mode == 'plan' else stage(args.packet.resolve(), args.route)
    if args.mode == 'plan':
        result = {key:value for key,value in result.items() if key != 'selection'}
        result['strategy'] = {key:value for key,value in result['strategy'].items() if key != 'source_observations'}
    print(json.dumps(result, sort_keys=True))

if __name__ == '__main__':
    main()
