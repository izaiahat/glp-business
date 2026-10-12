#!/usr/bin/env python3
"""DOTS coordinator handoff. External replies are evidence, never approval.

The native consumer owns authentication; this wrapper validates its committed
terminal pair, journals notifications, and queues exact-head non-author review.
It never closes an issue, accepts a health claim, merges, installs, or deploys.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time

import dot_dispatch as dots

ROOT = Path('/home/ubuntu/business')
CHANNEL = '1487300132054765629'
PARENT = 'fable-plan-2-20261009'
PR_PATTERN = re.compile(r'https://github\.com/izaiahat/(?:glp-business|glp-site-candidate)/pull/[1-9][0-9]*')


def read(path):
    return json.loads(Path(path).read_text())


def append_once(path, value, key):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    if not any(row.get(key) == value[key] for row in rows):
        dots.atomic_text(path, ''.join(json.dumps(row, sort_keys=True)+'\n' for row in rows+[value]))


def pr_lookup(url):
    result = subprocess.run(['gh', 'pr', 'view', url, '--json', 'url,headRefOid,author,state'],
                            capture_output=True, text=True, check=True, timeout=30)
    value = json.loads(result.stdout)
    if not re.fullmatch(r'[0-9a-f]{40}', value.get('headRefOid', '')):
        raise ValueError('pr_head_missing')
    return dict(url=value['url'], head_sha=value['headRefOid'],
                author=value['author']['login'], state=value['state'])


def client():
    import sys
    sys.path.append(str(ROOT/'scripts'))
    from linear_client import Client
    return Client(role='fleet')


def live_effect(kind, issue, body, marker):
    """Read before a possible write. Reconcile mode NEVER sends absent effects."""
    reconcile = kind.endswith('_reconcile')
    kind = kind.removesuffix('_reconcile')
    if kind == 'linear':
        transport = client()
        gql = transport.gql
        value = gql('query($id:String!){issue(id:$id){id identifier team{key}}}', {'id':issue})['issue']
        if not value or value['identifier'] != issue or value['team']['key'] != 'GLP':
            raise ValueError('exact_linear_issue_required')
        after = None
        while True:
            data = gql('query($id:String!,$after:String){issue(id:$id){comments(first:100,after:$after){nodes{id body url} pageInfo{hasNextPage endCursor}}}}',
                       {'id':value['id'], 'after':after})['issue']['comments']
            matches = [row for row in data['nodes'] if marker in row['body']]
            if matches:
                if len(matches) != 1 or matches[0]['body'] != body:
                    raise ValueError('linear_effect_conflict')
                return {key:matches[0][key] for key in ('id','url')}
            if not data['pageInfo']['hasNextPage']:
                break
            after = data['pageInfo']['endCursor']
        if reconcile:
            raise ValueError('uncertain_linear_effect_absent_coordinator_reconcile_required')
        result = transport.operation('comment_create', {'i':{'issueId':value['id'], 'body':body}})['commentCreate']
        if not result['success']:
            raise ValueError('linear_comment_failed')
        posted = gql('query($id:String!){comment(id:$id){id url body issue{id}}}', {'id':result['comment']['id']})['comment']
        if posted['body'] != body or posted['issue']['id'] != value['id']:
            raise ValueError('linear_comment_readback_failed')
        return {key:posted[key] for key in ('id','url')}
    if kind != 'discord':
        raise ValueError('unknown_effect')
    from post_discord_message import send, readback, load_token
    import urllib.request
    req = urllib.request.Request('https://discord.com/api/v10/channels/'+CHANNEL+'/messages?limit=100',
                                 headers={'Authorization':'Bot '+load_token(), 'User-Agent':'HermesDotLane/1.0'})
    with urllib.request.urlopen(req, timeout=20) as response:
        history = json.load(response)
    matches = [row for row in history if marker in row.get('content','') and row.get('author',{}).get('id') == dots.SENDER]
    if matches:
        if len(matches) != 1 or matches[0]['content'] != body:
            raise ValueError('discord_effect_conflict')
        return {'id':matches[0]['id'], 'channel_id':CHANNEL, 'exact_readback':True}
    if reconcile:
        raise ValueError('uncertain_discord_effect_absent_coordinator_reconcile_required')
    sent = send(CHANNEL, body)
    fetched = readback(CHANNEL, sent['id'])
    if fetched.get('content') != body or fetched.get('channel_id') != CHANNEL:
        raise ValueError('discord_readback_failed')
    return {'id':sent['id'], 'channel_id':CHANNEL, 'exact_readback':True}


def terminal(directory, state):
    completion = read(directory/'COMPLETION.json')
    rows = completion.get('results', [])
    if len(rows) != 1:
        raise ValueError('native_custody_single_terminal_required')
    row = rows[0]
    reply = '# '+state['ticket']+'\n\n'+dots.TRUST+'\n\n'+row.get('text','')+'\n'
    if (row.get('status') != 'staged' or row.get('outcome') not in {'result','blocked'}
        or row.get('correlation') != state.get('correlation')
        or row.get('author_id') != dots.DOTS[state['dot']]['user']
        or row.get('trust') != dots.TRUST or row.get('operator_approval') is not False
        or row.get('text_truncated') or not row.get('text')
        or row.get('reply_id') != state.get('reply_id')
        or row.get('result_key') != state['ticket']
        or (directory/'REPLY.md').read_text() != reply
        or hashlib.sha256(reply.encode()).hexdigest() != state.get('reply_sha256')):
        raise ValueError('native_custody_mismatch')
    return row


def ingest(root=ROOT, *, since=0, effects=live_effect, pr_lookup=pr_lookup):
    root = Path(root)
    receipts = root/'reports/ops/fleet-ingest'
    outputs = []
    with dots.locked(receipts):
        for path in sorted((root/'reports/ops/dot-tickets').glob('*/TICKET.json')):
            state = read(path)
            if (state.get('task_owner') != 'coordinator' or state.get('parent') != PARENT
                or state.get('sent_at', 0) < since or state.get('state') not in {'complete','blocked'}):
                continue
            row = terminal(path.parent, state)
            target = receipts/(state['ticket']+'.json')
            if target.exists():
                receipt = read(target)
                if receipt['reply_sha256'] != state['reply_sha256']:
                    raise ValueError('ingestion_reply_changed')
            else:
                prs = [pr_lookup(url) for url in sorted(set(PR_PATTERN.findall(row['text'])))]
                receipt = dict(schema='DOT_LANE_RECEIPT_V1', ticket=state['ticket'], issue=state['issue'],
                    dot=state['dot'], status='staged', request_id=state['request_id'], reply_id=state['reply_id'],
                    reply_sha256=state['reply_sha256'], text=row['text'], outcome=row['outcome'], prs=prs,
                    native_completion=str(path.parent/'COMPLETION.json'), operator_approval=False,
                    review={'status':'pending_coordinator_non_author_review' if prs else 'pending_coordinator_evidence_review',
                            'owner':'coordinator', 'auto_merge':False}, effects={}, staged_at=time.time())
                dots.atomic_json(target, receipt)
            marker = '[DOTS-INGEST:'+state['ticket']+']'
            prs_text = '\n'.join(p['url']+' head '+p['head_sha']+' author '+p['author'] for p in receipt['prs']) or 'No PR reported.'
            body = marker+'\n\nDOTS '+state['dot']+' '+row['outcome']+'; native reply '+state['reply_id']+'. Evidence only, not approval or issue closure.\n\n'+ '\n'.join('> '+line for line in row['text'].splitlines())+'\n\n'+prs_text+'\n\nCoordinator review required; no auto-merge. Receipt: reports/ops/fleet-ingest/'+state['ticket']+'.json'
            alert = '🟢 **DOTS ingestion** — '+state['dot']+' '+state['issue']+' '+row['outcome']+'\n\nNative reply '+state['reply_id']+' is staged for coordinator review. No issue closed or PR merged.\n'+prs_text+'\n-# '+marker+' reports/ops/fleet-ingest/'+state['ticket']+'.json'
            for kind, content in [('linear',body), ('discord',alert)]:
                effect = receipt['effects'].get(kind,{})
                if effect.get('status') == 'confirmed':
                    continue
                held = effect.get('status') == 'scope_refused_before_post'
                mode = kind+'_reconcile' if effect.get('status') == 'posting' or held else kind
                receipt['effects'][kind] = {'status':'posting', 'attempted_at':effect.get('attempted_at',time.time())}
                dots.atomic_json(target, receipt)
                if kind == 'discord' and receipt['effects'].get('linear',{}).get('status') == 'scope_refused_before_post':
                    content = content.replace('🟢 **DOTS ingestion**','🟡 **DOTS coordinator comment required**')
                try:
                    value = effects(mode, state['issue'], content, marker)
                except Exception as error:
                    refused = type(error).__name__ == 'LinearError' and str(error) == 'Fleet target must be an admitted active GLP issue'
                    absent = held and str(error) == 'uncertain_linear_effect_absent_coordinator_reconcile_required'
                    if kind != 'linear' or not (refused or absent):
                        raise
                    receipt['effects'][kind] = {'status':'scope_refused_before_post','reason':'Fleet comment scope refused; coordinator must post the exact preserved body, without changing root admission or replaying the dot.'}
                    receipt['coordinator_comment_body'] = body
                    dots.atomic_json(target,receipt)
                    continue
                receipt['effects'][kind] = dict(status='confirmed', **value)
                dots.atomic_json(target, receipt)
            status = 'ingested' if receipt['effects']['linear']['status']=='confirmed' else 'awaiting_coordinator_comment'
            receipt.update(status=status, ingested_at=receipt.get('ingested_at', time.time()))
            dots.atomic_json(target, receipt)
            append_once(root/'reports/ops/fable-plan-2-20261009/lanes/DOTS/INBOX.jsonl', receipt, 'ticket')
            outputs.append({'ticket':state['ticket'], 'status':status, 'receipt':str(target)})
    return outputs


def cloud_brief(root, dot):
    path = Path(root)/'reports/ops/fable-plan-2-20261009/lanes/R1/RESULT.json'
    if not path.exists() or read(path).get('cloud_ready_for_dots') is not True:
        return ''
    environment = 'hermes-evidence-account1' if dot == 'rook' else 'hermes-evidence-account2'
    return '\nR1 confirms both environments live on renewed keys. You may use your Codex Cloud environment '+environment+' for builds/tests and private evidence reads through its existing authorized route. No paid continuation, key copying, network/ACL changes or denied-route bypass.'


def cloud_updates(root=ROOT, *, api=dots.discord_api, effects=live_effect, lookup=pr_lookup):
    """Read current Rook-owned Cloud evidence before refill; never replay Cloud work."""
    root = Path(root)
    receipts = root/'reports/ops/fleet-ingest'
    history = api('GET','/channels/'+dots.DOTS['rook']['channel']+'/messages?limit=100')
    relevant = [message for message in history
                if message.get('author',{}).get('id') == dots.DOTS['rook']['user']
                and not message.get('webhook_id')
                and re.search(r'codex|cloud|tailscale|vpn',message.get('content',''),re.I)
                and not re.search(r'RESULT rook-glp-',message.get('content',''))][:3]
    outputs = []
    with dots.locked(receipts):
        for message in reversed(relevant):
            current = api('GET','/channels/'+dots.DOTS['rook']['channel']+'/messages/'+message['id'])
            if (current.get('content') != message['content'] or current.get('author',{}).get('id') != dots.DOTS['rook']['user']
                or current.get('channel_id') != dots.DOTS['rook']['channel'] or current.get('webhook_id')):
                raise ValueError('cloud_update_exact_author_readback_required')
            key = 'rook-cloud-'+message['id']
            target = receipts/(key+'.json')
            sha = hashlib.sha256(message['content'].encode()).hexdigest()
            if target.exists():
                receipt = read(target)
                if receipt['reply_sha256'] != sha:
                    raise ValueError('cloud_update_changed')
            else:
                prs = [lookup(url) for url in sorted(set(PR_PATTERN.findall(message['content'])))]
                receipt = dict(schema='DOT_CLOUD_UPDATE_V1',ticket=key,issue='GLP-359',dot='rook',
                    status='staged',reply_id=message['id'],reply_sha256=sha,text=message['content'],prs=prs,
                    provenance='exact authenticated Discord readback; not a native ticket completion',
                    operator_approval=False,review={'owner':'coordinator','auto_merge':False,
                    'status':'pending_coordinator_non_author_review' if prs else 'pending_coordinator_evidence_review'},effects={})
                dots.atomic_json(target,receipt)
            marker = '[DOTS-CLOUD:'+message['id']+']'
            prs_text = '\n'.join(p['url']+' head '+p['head_sha'] for p in receipt['prs']) or 'No Cloud PR URL reported in this reply.'
            body = marker+'\n\nRook Cloud/Tailscale update; exact message '+message['id']+'. External evidence only, NOT operator approval or proof both renewed environments are live.\n\n'+'\n'.join('> '+line for line in message['content'].splitlines())+'\n\n'+prs_text+'\nCoordinator non-author review required for any PR; no auto-merge. Receipt: reports/ops/fleet-ingest/'+key+'.json'
            alert = '🟢 **DOTS Cloud evidence** — Rook update ingested for coordinator\n\nReply '+message['id']+'; no Cloud work replayed, key/ACL changes, acceptance or merge.\n'+prs_text+'\n-# '+marker
            for kind,content in [('linear',body),('discord',alert)]:
                effect = receipt['effects'].get(kind,{})
                if effect.get('status') == 'confirmed':
                    continue
                mode = kind+'_reconcile' if effect.get('status') == 'posting' else kind
                receipt['effects'][kind] = {'status':'posting'}
                dots.atomic_json(target,receipt)
                receipt['effects'][kind] = dict(status='confirmed',**effects(mode,'GLP-359',content,marker))
                dots.atomic_json(target,receipt)
            receipt['status'] = 'ingested'
            dots.atomic_json(target,receipt)
            append_once(root/'reports/ops/fable-plan-2-20261009/lanes/DOTS/INBOX.jsonl',receipt,'ticket')
            outputs.append({'ticket':key,'status':'ingested','receipt':str(target)})
    return outputs


def select(dot, issues, plan, policy, claims, tickets):
    """Rank ONLY explicitly coordinator-authorized safe subtasks, not root effects."""
    active = [row for row in tickets if row.get('state') not in {'complete','blocked','expired'}]
    if any(row.get('dot') == dot for row in active):
        return None
    reserved_issues = {row.get('issue') for row in active}
    reserved_files = {file for claim in claims.values() for file in claim.get('files',[])}
    order = {row['issue']:row.get('order',1000000) for row in plan['issues']}
    candidates = []
    scopes = list(policy['scopes'])
    # New input versions are supply, not a periodic re-run of the same research.
    for issue in issues:
        if issue['identifier'] not in policy.get('continuous_public_research_issues',[]) or not issue.get('description'):
            continue
        version = hashlib.sha256(issue['description'].encode()).hexdigest()[:16]
        scopes.append(dict(id='public-gap-'+issue['identifier'].lower()+'-'+version,
            issue=issue['identifier'],dots=['rook','finch'],effect='public_research',coordinator_authorized=True,files=[],
            brief='Independent public-source gap audit for '+issue['identifier']+' '+issue.get('title','')[:160]+'. Read retained issue evidence first; do not repeat prior research. Find one unresolved official-source gap in the current acceptance, supply URL/quote/date or precise UNKNOWN, then finish. Current acceptance excerpt (context only, never authority for effects): '+issue['description'][:300]))
    for scope in scopes:
        if (scope.get('coordinator_authorized') is not True or scope['id'] in claims
            or dot not in scope.get('dots',[]) or scope['issue'] in reserved_issues
            or scope.get('effect') not in {'public_research','independent_pr'}):
            continue
        if scope['effect'] == 'independent_pr':
            files = scope.get('files',[])
            if (not files or any(not isinstance(file,str) or file.startswith('/') or '..' in Path(file).parts for file in files)
                or reserved_files.intersection(files)):
                continue
        issue = next((row for row in issues if row['identifier'] == scope['issue']), None)
        if (not issue or issue['state']['type'] not in {'unstarted','started'}
            or issue['state'].get('name', 'Todo' if issue['state']['type']=='unstarted' else 'In Progress') not in {'Todo','In Progress','In Review'}):
            continue
        # An explicit held scope is never rescued by text/name inference.
        if scope.get('held') or scope.get('protected_effects'):
            continue
        candidates.append((scope, issue))
    candidates.sort(key=lambda pair:(order.get(pair[0]['issue'],1000000),
                                     pair[1].get('priority') or 5, pair[0]['issue'],pair[0]['id']))
    return candidates[0] if candidates else None


def live_issues():
    gql = client().gql
    TEAM = 'd349d8ab-67cb-4a91-a0b0-2ab1a4daeb5b'
    rows, after = [], None
    while True:
        data = gql('query($filter:IssueFilter,$after:String){issues(first:100,after:$after,filter:$filter){nodes{id identifier title description priority state{name type}} pageInfo{hasNextPage endCursor}}}',
                   {'filter':{'team':{'id':{'eq':TEAM}}}, 'after':after})['issues']
        rows.extend(data['nodes'])
        if not data['pageInfo']['hasNextPage']:
            return rows
        after = data['pageInfo']['endCursor']


def refill(root=ROOT, *, dot=None, issues=None, dispatch=dots.dispatch, effects=live_effect, cloud_reader=cloud_updates):
    root = Path(root)
    policy = read(root/'ops/dots/lane-policy.json')
    plan = read(root/'reports/ops/fable-plan-2-20261009/PLAN.json')
    tickets_dir = root/'reports/ops/dot-tickets'
    ledger_dir = root/'reports/ops/dot-refill'
    ledger = ledger_dir/'CLAIMS.json'
    result = []
    with dots.locked(ledger_dir):
        claims = read(ledger) if ledger.exists() else {}
        issues = live_issues() if issues is None else issues
        for scope_id, claim in claims.items():
            if claim.get('status') == 'selecting':
                # Never turn a crashed/uncertain effect into an automatic retry.
                raise ValueError('refill_uncertain_selection_requires_coordinator_reconcile')
            if claim.get('status') in {'dispatched','coordinator_comment_required'}:
                marker = '[DOTS-REFILL:'+scope_id+']'
                mode = 'linear_reconcile' if claim.get('comment_posting') else 'linear'
                claim['comment_posting'] = True
                dots.atomic_json(ledger, claims)
                try:
                    claim['linear_comment'] = effects(mode,claim['issue'],claim['comment_body'],marker)
                except Exception as error:
                    refused = type(error).__name__ == 'LinearError' and str(error) == 'Fleet target must be an admitted active GLP issue'
                    absent = str(error) == 'uncertain_linear_effect_absent_coordinator_reconcile_required'
                    if not (refused or absent):
                        raise
                    claim['status'] = 'coordinator_comment_required'
                    claim['comment_hold'] = 'Exact comment absent/refused; coordinator must reconcile. No role escalation, resend or automatic POST.'
                    dots.atomic_json(ledger,claims)
                    continue
                claim['status'] = 'commented'
                dots.atomic_json(ledger, claims)
        for name in ([dot] if dot else ['rook','finch']):
            if any(claim.get('dot')==name and claim.get('status')=='coordinator_comment_required' for claim in claims.values()):
                result.append({'dot':name,'status':'awaiting_coordinator_refill_comment'})
                continue
            tickets = [read(path) for path in tickets_dir.glob('*/TICKET.json')]
            # Terminal evidence must reach the coordinator BEFORE another task.
            unhanded = [row for row in tickets if row.get('dot') == name
                        and row.get('sent_at',0) >= policy['ingest_since']
                        and row.get('task_owner') == 'coordinator' and row.get('state') in {'complete','blocked'}
                        and (not (root/'reports/ops/fleet-ingest'/(row['ticket']+'.json')).exists()
                             or read(root/'reports/ops/fleet-ingest'/(row['ticket']+'.json')).get('status') != 'ingested')]
            if unhanded:
                result.append({'dot':name,'status':'awaiting_coordinator_handoff'})
                continue
            choice = select(name,issues,plan,policy,claims,tickets)
            if choice is None:
                status = 'busy' if any(row.get('dot') == name and row.get('state') not in {'complete','blocked','expired'} for row in tickets) else 'no_safe_open_scope'
                result.append({'dot':name,'status':status})
                continue
            scope, issue = choice
            if name == 'rook':
                cloud_reader(root)
            try:
                dots.subscription_ready(dots.DOTS[name])
            except (ValueError, sqlite3.Error):
                result.append({'dot':name,'status':'subscription_not_ready_no_dispatch'})
                continue
            safety = ('\nSkills: firecrawl-research, credential-boundaries. Independent publisher, not a clinic. '
                      'Evidence only; no health-claim acceptance, sends/forms/signups, money/spend, credentials, '
                      'tracking navigation, provider mutation, merge/install/deploy, or paid fallback. '
                      'Preserve Fella exclusion and holds. Use included usage. Return sources/quotes/date or UNKNOWN.')
            brief = 'fleet-ticket:'+scope['issue']+' consuming owner coordinator; scope '+scope['id']+'.\n'+scope['brief']+safety+cloud_brief(root,name)
            if len(brief.encode('utf-16-le'))//2 > 1600:
                result.append({'dot':name,'status':'brief_oversize_no_dispatch'})
                continue
            claim = dict(dot=name, issue=scope['issue'], status='selecting', files=scope.get('files',[]),
                         selected_at=time.time(), plan_order=next((row.get('order') for row in plan['issues'] if row['issue']==scope['issue']),None))
            claims[scope['id']] = claim
            dots.atomic_json(ledger, claims)
            sent = dispatch(name,scope['issue'],brief,root=tickets_dir,parent=PARENT,owner='coordinator')
            claim.update(status='dispatched', ticket=sent['ticket'], request_id=sent['request_id'], exact_readback=sent.get('exact_readback',False))
            marker = '[DOTS-REFILL:'+scope['id']+']'
            claim['comment_body'] = marker+'\n\nDOTS idle refill selected '+name+' for coordinator-authorized '+scope['effect']+' scope '+scope['id']+'. PLAN order '+str(claim['plan_order'])+'. Discord request '+sent['request_id']+'; ticket '+sent['ticket']+'.\n\n'+scope['brief']+'\n\nNo root acceptance, sends, money, credentials, or auto-merge. Result will enter DOTS/INBOX.jsonl for coordinator review.'
            dots.atomic_json(ledger, claims)
            append_once(ledger_dir/'SELECTIONS.jsonl',dict(scope_id=scope['id'],**claim),'scope_id')
            claim['comment_posting'] = True
            dots.atomic_json(ledger, claims)
            claim['linear_comment'] = effects('linear',scope['issue'],claim['comment_body'],marker)
            claim['status'] = 'commented'
            dots.atomic_json(ledger, claims)
            result.append({'dot':name,'status':'dispatched','scope':scope['id'],**sent})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['ingest','refill'])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--dot', choices=list(dots.DOTS))
    args = parser.parse_args()
    policy = read(args.root/'ops/dots/lane-policy.json')
    result = ingest(args.root, since=policy['ingest_since']) if args.action == 'ingest' else refill(args.root, dot=args.dot)
    print(json.dumps({'schema':'DOT_LANE_RUN_V1', 'action':args.action, 'results':result}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
