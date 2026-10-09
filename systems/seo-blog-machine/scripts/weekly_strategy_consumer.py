#!/usr/bin/env python3
"""Consume GLP44's exact Cogny-readback strategy as preferences, not authority.

No queue/source/network writes. Missing, stale or tampered strategy is UNKNOWN;
existing measured-demand and publication guards remain the sole admission path.
"""
import datetime as dt
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo


def review_week_start(day):
    """Sunday review governs the upcoming Monday-through-Saturday supply week.

    Before Sunday's review is ready, an absent new input means no dispatch.
    Never silently keep using last week's topics after the Sunday rollover.
    """
    return day + dt.timedelta(days=1) if day.weekday() == 6 else day - dt.timedelta(days=day.weekday())

DEFAULT_PATH=Path('/home/ubuntu/business/reports/glp-clinic-scout/weekly-strategy/current-strategy.json')

def bound(spec):
    path=Path(spec['path'])
    if not path.is_absolute() or path.is_symlink() or not path.is_file(): raise ValueError('strategy_bound_path_invalid')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=spec['sha256']: raise ValueError('strategy_bound_hash_invalid')
    return json.loads(raw),raw.decode()

def load_strategy(path=DEFAULT_PATH, *, as_of=None):
    path=Path(path);today=as_of or dt.datetime.now(ZoneInfo('America/Los_Angeles')).date()
    meta={'path':str(path),'status':'UNKNOWN_COGNY_STRATEGY','applied':False,'keyword_gap_count':None,
          'publication_authorized':False,'own_site_analytics_gates_content':False}
    try:
        if path.is_symlink():raise ValueError('strategy_pointer_symlink')
        raw=path.read_bytes();pointer=json.loads(raw)
        if pointer.get('schema')!='glp-weekly-strategy-current-v1' or pointer.get('publication_authorized') is not False:
            raise ValueError('strategy_pointer_contract')
        doc,text=bound(pointer['updates'])
        week=dt.date.fromisoformat(doc['week_start'])
        if pointer['week_start']!=doc['week_start'] or week != review_week_start(today): raise ValueError('strategy_week_stale_or_future')
        if (doc.get('schema')!='glp-weekly-strategy-updates-v1' or doc.get('publication_authorized') is not False
                or doc.get('strategy_input_write_authorized') is not True or doc.get('unknown_is_not_zero') is not True):
            raise ValueError('strategy_updates_contract')
        cogny=pointer['cogny']
        if cogny.get('status')!='COGNY_EXACT_CONTENT_READBACK' or cogny.get('updates')!=pointer['updates']:
            raise ValueError('strategy_cogny_readback_missing')
        remote,_=bound(cogny['readback']);response=remote['response']
        node=response.get('node',response) if isinstance(response,dict) else response
        content=node.get('content') if isinstance(node,dict) else node
        if not isinstance(content,str) or text not in content: raise ValueError('strategy_cogny_content_mismatch')
        for field in ['topic_priorities','page2_targets','refresh_list']:
            if not isinstance(doc.get(field),list) or not all(isinstance(x,dict) for x in doc[field]):raise ValueError('strategy_rows_invalid:'+field)
        gaps=doc.get('keyword_gaps')
        if gaps is not None and (not isinstance(gaps,list) or not all(isinstance(x,dict) for x in gaps)):raise ValueError('strategy_gaps_invalid')
        # Read every source binding supplied by the producer. Null/UNKNOWN
        # results remain in the source payload and are not converted to metrics.
        for evidence in doc.get('evidence_bindings',{}).values():bound(evidence)
        meta.update(status='VALID_COGNY_STRATEGY_INPUT',applied=True,week_start=doc['week_start'],
                    pointer_sha256=hashlib.sha256(raw).hexdigest(),updates=pointer['updates'],
                    cogny_readback=cogny['readback'],keyword_gap_count=None if gaps is None else len(gaps),
                    source_observations=doc.get('source_observations',{}),
                    field_counts={key:len(doc[key]) for key in ['topic_priorities','page2_targets','refresh_list']})
        return doc,meta
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as error:
        meta['reason']=str(error)
        return None,meta

def normalize(value):return ' '.join(str(value or '').lower().split())

def preference(doc, query, route, lane):
    """Exact joins only. Existing-page advice never authorizes a second route."""
    if doc is None or 'fella' in (normalize(query)+' '+str(route).lower()):return None
    if lane not in {'new_article','r2_refresh'}:raise ValueError('strategy_consumer_lane_invalid')
    matches=[]
    fields=['keyword_gaps'] if lane=='new_article' else ['topic_priorities','page2_targets','refresh_list']
    for field in fields:
        for index,row in enumerate(doc.get(field) or []):
            if field=='keyword_gaps':
                matched=normalize(row.get('keyword'))==normalize(query) and bool(normalize(query))
            else:
                matched=row.get('route')==route and (field=='refresh_list' or normalize(row.get('query'))==normalize(query))
            if not matched:continue
            priority=row.get('priority',index+1)
            if type(priority) not in (int,float) or not 0 < priority < 100000:continue
            weight=0.25 if row.get('evidence_class')=='LOW_VOLUME_CONTEXT' else 1.0
            matches.append({'source':field,'priority':priority,'score':weight/priority,
                            'evidence_class':row.get('evidence_class'), 'publication_authorized':False})
    return max(matches,key=lambda x:x['score']) if matches else None

def preference_score(doc, query, route, lane):
    value=preference(doc,query,route,lane)
    return value['score'] if value else 0.0  # Sorting neutral; not a zero metric.
