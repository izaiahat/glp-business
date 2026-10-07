#!/usr/bin/env python3
"""Weekly Strategy report producer; read-only providers, report-local outputs.

No metered collection, queue admission, finding mutations, site writes or sends.
The registered wake gate calls this before its scheduler-owned summary delivery.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import uuid

B=Path('/home/ubuntu/business')
sys.path.insert(0,str(B))

def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None: raise ValueError('producer_unavailable')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def save(path,doc):
    path.write_text(json.dumps(doc,indent=2,ensure_ascii=False)+'\n')

def run(output):
    from ops.glp.weekly_strategy_inputs import topic_recommendations
    from ops.glp.seo_weekly_modules import read_bound
    collector=load(B/'scripts/glp_weekly_seo_collect.py','weekly_strategy_collector')
    analysis=load(B/'scripts/glp_weekly_seo_analysis.py','weekly_strategy_analysis')
    analysis.verify_pins()
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    collected=collector.run(output/'collection',allow_metered=False)
    from ops.glp.seo_weekly_continuation import run_week
    receipt=run_week(Path(collected['manifest']['path']),output/'analysis',registered=True)
    manifest=json.loads(Path(collected['manifest']['path']).read_text())
    inputs=manifest['inputs'];estate=read_bound(inputs['estate'])
    gsc=read_bound(inputs['gsc-28'])
    module_report=json.loads((output/'analysis/modules/receipt.json').read_text())
    money=module_report['modules']['money_pages'].get('output',{})
    input_observations={}
    for name in ('gsc-28','gsc-90','gsc-prior-28','ga4','posthog','cogny-weekly-evidence'):
        doc=read_bound(inputs[name]); result=doc.get('result',{})
        input_observations[name]={'binding':inputs[name],'captured_at_utc':doc.get('captured_at_utc'),
                                 'context':doc.get('context'),'state':result.get('state'),
                                 'ok':result.get('ok'),'complete':result.get('complete')}
    ranking=None
    from ops.glp.weekly_strategy_inputs import read_evidence
    rank_root=B/'reports/glp-clinic-scout/seo-machine/weekly-ranking-report'
    for path in sorted(rank_root.glob('????-??-??.json'),reverse=True):
        try:
            doc,binding=read_evidence(path,max_age_hours=192)
            ranking={'binding':binding,'headline':doc['headline'],'verdict':doc['verdict'],'report_date':doc['report_date']}
            break
        except (OSError,ValueError,TypeError,KeyError): continue
    held=[r['review_path'] for r in money.get('programs',[]) if r.get('permission',{}).get('held_preserved')]
    topics=topic_recommendations(gsc,held_routes=held)
    topics['evidence_binding']=inputs['gsc-28'];topics['week_start']=receipt['week_start']
    topics['module_binding']={'path':str(output/'analysis/modules/receipt.json'),'sha256':hashlib.sha256((output/'analysis/modules/receipt.json').read_bytes()).hexdigest()}
    save(output/'TOPIC-RECOMMENDATIONS.json',topics)
    current={c['component']:c['live_effect'] for c in estate['result']['components']}
    sources=current.get('analytics',{}).get('sources',[])
    unknowns=[{'subject':s['source'],'reason':s.get('reason') or s['state'],'owner':'GLP-44 source/import owner'} for s in sources if s['state'] not in ('healthy','measured_zero')]
    unknowns += [{'subject':k,'reason':module_report['modules'][k].get('required_artifact',v),'owner':'operator: explicit recurring authority and per-run ceiling' if k=='keyword_gap' else 'GLP-44'} for k,v in receipt['module_statuses'].items() if v!='PASS_ANALYSIS_ONLY']
    unknowns += [{'subject':name,'reason':'Weekly neutral snapshot incomplete; see bound result, not cached source success','owner':'GLP-44'} for name in ('ga4','posthog') if input_observations[name]['ok'] is not True or input_observations[name]['complete'] is False]
    if ranking is None: unknowns.append({'subject':'weekly ranking verdict','reason':'No ranking receipt captured within eight days','owner':'registered weekly ranking producer'})
    unknowns += [{'subject':'affiliate payable revenue / ROAS','reason':'Network join incomplete; unmatched D1 clicks are not lost or payable sales','owner':'network reporting/import owner'},
                 {'subject':'article/click identity in GA4/PostHog','reason':'UNMEASURED_BY_DESIGN: approved neutral-route contract only; do not widen health-data export','owner':'privacy boundary retained'},
                 {'subject':'causal SEO lift','reason':'Returned-row movement is descriptive; GSC omissions and search latency persist','owner':'SEO observation'}]
    report={'schema':'glp-weekly-strategy-report-v1','generated_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
            'status':'COMPLETE_WITH_EXPLICIT_UNKNOWNS' if estate['result']['ok'] and all(receipt['module_statuses'].get(k)=='PASS_ANALYSIS_ONLY' for k in ('page2','money_pages','estate_health')) and all(input_observations[k]['ok'] is True and input_observations[k]['complete'] is not False for k in ('ga4','posthog')) else 'PARTIAL_INPUT_UNAVAILABLE',
            'ranking':ranking,'input_observations':input_observations,
            'analytics_mode':current.get('analytics',{}).get('mode'),'source_rows':{s['source']:s.get('actual_rows') for s in sources},
            'module_statuses':receipt['module_statuses'],'gsc_rows':collected['gsc_rows'],
            'cogny_query_count':collected['cogny_query_count'],'recommendations':len(topics['recommendations']),
            'non_article_operations':receipt['source_operations'],'affiliate_join':current.get('affiliate_join'),
            'unknowns':unknowns,'metered_cost_usd':collected['actual_cost_usd'],
            'sends':0,'queue_writes':0,'site_writes':0,'publication_authorized':False}
    save(output/'REPORT.json',report)
    lines=['# Weekly Strategy — GLP Clinic Scout','',f"Status: {report['status']} · analytics: {report['analytics_mode']}",'',
           '## Ranking outcome', json.dumps(ranking,ensure_ascii=False) if ranking else 'UNKNOWN: no eligible recent ranking receipt.', '',
           '## Source readbacks',*['- '+s['source']+': '+str(s['state'])+'; rows '+str(s.get('actual_rows'))+'; watermark '+str(s.get('watermark')) for s in sources],
           '',f"Cogny calls: {collected['cogny_query_count']}; GSC query/page windows: {collected['gsc_rows']}; new metered spend: $0.",'',
           '## Concrete SEO action list (GLP-73 handoff, not queue writes)',
           *[f"{r['priority']}. {r['route']} — query ‘{r['query']}’; {r['impressions']} impressions, {r['clicks']} clicks, avg position {r['average_position']:.2f}. {r['decision_value']}" for r in topics['recommendations']],
           '',f"Non-article source operations: {receipt['source_operations']}; use analysis/NON-ARTICLE-INTAKE.json via the existing governed updater, not article identities.",'',
           '## Remaining UNKNOWN',*['- '+r['subject']+': '+r['reason']+' (owner: '+r['owner']+').' for r in unknowns],
           '', '## Detailed comparative SEO strategy', (output/'analysis/WEEKLY-STRATEGY-NOTE.md').read_text()]
    (output/'WEEKLY-REVIEW.md').write_text('\n'.join(lines)+'\n')
    summary='\n'.join(['📊 **Weekly Strategy** — '+str(report['recommendations'])+' evidence-bound SEO actions',
      f"Status: **{report['analytics_mode']}** · estate **{receipt['module_statuses'].get('estate_health')}**",
      'Shipped: Cogny + GSC + GA4 + PostHog + Bing source readbacks consumed.',
      f"Blocked: network revenue **UNKNOWN**; keyword gap **held**; new metered spend **$0**.",
      f"Next GLP: review **{report['recommendations']}** existing-page opportunities; **{receipt['source_operations']}** source patches.",
      '→ Keep ads paused; explicit recurring DataForSEO authority/ceiling needed only for the held module.',
      '-# artifact: '+str(output/'WEEKLY-REVIEW.md')])
    (output/'DISCORD.txt').write_text(summary+'\n')
    return {'report':str(output/'REPORT.json'),'review':str(output/'WEEKLY-REVIEW.md'),
            'topic_recommendations':str(output/'TOPIC-RECOMMENDATIONS.json'),'summary':summary,'status':report['status']}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    output=args.output or B/'reports/glp-clinic-scout/weekly-strategy'/('run-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    lock=B/'ops/glp/.weekly-strategy-report.lock'
    with lock.open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        print(json.dumps(run(output),ensure_ascii=False))

if __name__=='__main__': main()
