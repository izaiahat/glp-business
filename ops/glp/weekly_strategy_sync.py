"""Cogny-backed weekly strategy inputs, separate from the article queue writer.

Updates are operator-authorized planning inputs, never publication authority.
Unknown keyword gaps remain null. Only exact Cogny readback publishes a pointer.
"""
import hashlib
import json
import os
from pathlib import Path
import uuid


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic(path, doc):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    raw=(json.dumps(doc,indent=2,ensure_ascii=False,allow_nan=False)+'\n').encode()
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp.open('xb') as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path)
    return {'path':str(path.resolve()),'sha256':sha(raw)}


def build_updates(topics, modules, bindings, *, source_observations):
    rows=[];refresh={}
    for original in topics['recommendations']:
        row={k:v for k,v in original.items() if k!='queue_eligible'}
        row['action']='refresh_existing_page'
        row['evidence_class']='MONTHLY_PROXY_AT_LEAST_100' if (row.get('impressions') or 0)>=100 else 'LOW_VOLUME_CONTEXT'
        row['consumer_review_required']=True
        rows.append(row)
        entry=refresh.setdefault(row['route'],{'route':row['route'],'queries':[],
            'priority':row['priority'],'action':'refresh_existing_page',
            'consumer_review_required':True,'new_article_authorized':False})
        entry['queries'].append(row['query'])
    gap=modules['modules']['keyword_gap']
    extension=gap.get('output',{}).get('ws_s_extension',{})
    gaps=extension.get('new_observations') if gap['status']=='PASS_ANALYSIS_ONLY' else None
    return {'schema':'glp-weekly-strategy-updates-v1','week_start':topics['week_start'],
        'topic_priorities':rows,'page2_targets':rows,'refresh_list':list(refresh.values()),
        'keyword_gaps':gaps,'keyword_gap_status':gap['status'],
        'keyword_gap_required_artifact':gap.get('required_artifact'),
        'source_observations':source_observations,'evidence_bindings':bindings,
        'consumer':'GLP-73 existing topic/refill single writer',
        'strategy_input_write_authorized':True,'publication_authorized':False,
        'own_site_analytics_gates_content':False,'unknown_is_not_zero':True,
        'consumer_constraints':['Preserve existing provider holds and Fella exclusion',
            'Group query synonyms into one existing-page refresh, not duplicate articles',
            'Require existing exact-query demand, identity, source and route checks for new topics',
            'Do not mistake low-volume GSC candidates for qualified 100-impression page-two wins']}


def sync_updates(client, updates, output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    binding=atomic(output/'STRATEGY-UPDATES.json',updates)
    raw=Path(binding['path']).read_text()
    path='glp-clinic-scout/seo/weekly/'+updates['week_start']+'/'+binding['sha256'][:24]
    content='# Evidence-bound GLP weekly strategy\n\nSHA256: '+binding['sha256']+'\n\n```json\n'+raw+'```\n'
    payload={'path':path,'title':'GLP weekly SEO strategy '+updates['week_start'],
        'content':content,'metadata':{'schema':updates['schema'],'sha256':binding['sha256']}}
    atomic(output/'COGNY-WRITE-INTENT.json',{'tool':'write_context_node','arguments':payload,'replay':'Read exact target before retrying any ambiguous effect'})
    response=client.call('write_context_node',payload)
    atomic(output/'COGNY-WRITE-RESPONSE.json',{'response':response})
    remote=client.call('read_context_node',{'path':path})
    atomic(output/'COGNY-READBACK.json',{'tool':'read_context_node','arguments':{'path':path},'response':remote})
    node=remote.get('node',remote) if isinstance(remote,dict) else remote
    returned=node.get('content') if isinstance(node,dict) else node
    if not isinstance(returned,str) or content not in returned:
        raise ValueError('cogny_strategy_readback_mismatch')
    return {'status':'COGNY_EXACT_CONTENT_READBACK','context_path':path,
        'updates':binding,'readback':{'path':str((output/'COGNY-READBACK.json').resolve()),
            'sha256':sha((output/'COGNY-READBACK.json').read_bytes())}}


def publish_current(path, updates, receipt):
    if receipt['status']!='COGNY_EXACT_CONTENT_READBACK':raise ValueError('cogny_readback_required')
    if json.loads(Path(receipt['updates']['path']).read_text())!=updates:raise ValueError('strategy_input_changed')
    pointer={'schema':'glp-weekly-strategy-current-v1','week_start':updates['week_start'],
        'updates':receipt['updates'],'cogny':receipt,'consumer_adoption':'PENDING_GLP73_RECEIPT',
        'publication_authorized':False}
    atomic(path,pointer)
    return pointer


def read_current(path):
    pointer=json.loads(Path(path).read_text());binding=pointer['updates']
    raw=Path(binding['path']).read_bytes()
    if sha(raw)!=binding['sha256']:raise ValueError('strategy_input_changed')
    readback=pointer['cogny']['readback'];raw_readback=Path(readback['path']).read_bytes()
    if sha(raw_readback)!=readback['sha256']:raise ValueError('cogny_readback_changed')
    return {'pointer':pointer,'updates':json.loads(raw)}
