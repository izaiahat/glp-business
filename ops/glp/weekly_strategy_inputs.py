"""Read-only weekly report inputs. Unknown business effects never become zero."""
import datetime as dt
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_evidence(path, max_age_hours=30):
    raw = path.read_bytes(); doc = json.loads(raw)
    stamp = doc.get('generated_at_utc') or doc.get('captured_at_utc')
    if not isinstance(stamp,str): raise ValueError('estate_artifact_timestamp_missing')
    captured = dt.datetime.fromisoformat(stamp.replace('Z','+00:00'))
    age = (dt.datetime.now(dt.timezone.utc)-captured).total_seconds()/3600
    if captured.tzinfo is None or not -0.1 <= age <= max_age_hours:
        raise ValueError('estate_artifact_stale_or_future')
    return doc, {'path':str(path.resolve()),'sha256':hashlib.sha256(raw).hexdigest(),
                 'observed_at_utc':stamp}


def estate_snapshot(business, *, jobs=None):
    business=Path(business); current=business/'reports/glp-clinic-scout/analytics/current'
    bindings={}; components=[]; errors=[]
    if jobs is None:
        try:
            registry=json.loads((Path.home()/'.hermes/cron/jobs.json').read_text())
            jobs=registry if isinstance(registry,list) else registry['jobs']
        except (OSError,ValueError,KeyError,TypeError):
            jobs=[];errors.append({'component':'scheduler','reason':'estate_registry_unavailable'})
    schedules=[{'id':j['id'],'name':j.get('name'),'enabled':j.get('enabled',True),
                'schedule':j.get('schedule_display'),'next_run_at':j.get('next_run_at'),
                'last_run_at':j.get('last_run_at'),'last_status':j.get('last_status'),
                'dispatch':j.get('last_dispatch')}
               for j in jobs if any(t in j.get('name','').lower()
               for t in ('weekly strategy','seo weekly','weekly seo','analytics'))
               and 'cbs' not in j.get('name','').lower()]
    for name,filename in [('analytics','analytics-source-health.json'),('affiliate_join','affiliate-reconciliation.json')]:
        try:
            doc,binding=read_evidence(current/filename); bindings[name]=binding
            if name=='analytics':
                if doc.get('schema')!='analytics-source-health-v1' or not isinstance(doc.get('sources'),list):
                    raise ValueError('estate_analytics_schema_invalid')
                effect={'mode':doc['mode'],'sources':doc['sources']}
            else:
                if not isinstance(doc.get('counts'),dict) or not isinstance(doc.get('network'),dict):
                    raise ValueError('estate_join_schema_invalid')
                effect={**doc['counts'],'network':doc['network']}
            components.append({'owner':'GLP-44','component':name,
                'natural_run':{'state':'NOT_INFERRED_FROM_OUTPUT','registry_observations':schedules},
                'manual_run':{'state':'OUTPUT_OBSERVED_TRIGGER_NOT_INFERRED','artifact':binding},
                'live_effect':effect,'required_artifact':'Completed network report/import with click identity for cash attribution' if name=='affiliate_join' else 'Fresh source receipts; schedule success alone does not prove business effect'})
        except (OSError,ValueError,TypeError,KeyError) as exc:
            errors.append({'component':name,'reason':str(exc)})
    return {'captured_at_utc':now(),'context':{'source':'installed analytics outputs and cron metadata, read-only','business_success_not_inferred':True},
            'bindings':bindings,'result':{'ok':not errors,'complete':not errors,'components':components,'errors':errors}}


def topic_recommendations(gsc, *, held_routes):
    result=gsc.get('result',{}); recommendations=[]; seen=set()
    if result.get('ok') is not True or result.get('complete') is False:
        status='UNKNOWN_GSC_INCOMPLETE'
    else:
        status='OBSERVED_REVIEW_CANDIDATES'
        rows=sorted(result.get('rows',[]),key=lambda r:(-float(r.get('impressions') or 0),str(r.get('keys'))))
        for row in rows:
            keys=row.get('keys',[])
            if len(keys)!=2: continue
            query,url=keys; parsed=urlsplit(url); route=parsed.path or '/'
            if (parsed.scheme!='https' or parsed.netloc!='glpclinicscout.com' or parsed.query or parsed.fragment
                or route in held_routes or route=='/go' or route.startswith('/go/')
                or 'fella' in (query+' '+route).lower()): continue
            pos=row.get('position')
            if not isinstance(pos,(int,float)) or not 11<=pos<=20 or (query,route) in seen: continue
            seen.add((query,route))
            recommendations.append({'priority':len(recommendations)+1,'query':query,'route':route,
                'average_position':pos,'impressions':row.get('impressions'),'clicks':row.get('clicks'),
                'action':'improve_existing_page','owner':'GLP-73 SEO refill owner',
                'decision_value':'Answer this distinct comparison decision with sourced benefits, drawbacks and costs; inspect the existing page before proposing a separate article.',
                'required_review':'Current identity/route dedupe, provider holds and source evidence; average position is not a stable page-two rank',
                'queue_eligible':False})
            if len(recommendations)>=20:break
    return {'schema':'glp-weekly-topic-recommendations-v1','status':status,'recommendations':recommendations,
            'operational_queue_writes':0,'publication_authorized':False,
            'consumer':'GLP-73 existing SEO refill owner; non-executable recommendations only',
            'absence_is_not_zero':True}
