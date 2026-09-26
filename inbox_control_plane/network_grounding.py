"""Read-only network-wide grounding; approvals, site presence and revenue stay distinct."""
from __future__ import annotations
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
import re

BUS = Path('/home/ubuntu/business')
SHEET_ROOT = BUS/'reports/glp-clinic-scout/affiliate-machine'
POINTER = BUS/'reports/glp-clinic-scout/seo-machine/current-content-base.json'
REPORT_ROOT = SHEET_ROOT/'weekly-reporting'


def load(path):
    raw=path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def norm(value):
    return re.sub('[^a-z0-9]','',str(value or '').casefold())


def context(network, *, sheet_root=SHEET_ROOT, pointer_path=POINTER, reporting_root=REPORT_ROOT, now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    facts=[]; sources={}; network=norm(network)
    facts.append('BRAND_VOICE | Confident, strengths first, concise and human. Never apologetic or self deprecating. Lead with evidenced approved relationships and live publication capabilities. Unknown performance is not evidence of no business. Do not invent traffic, revenue or active campaigns.')
    if not network:
        return {'facts':facts+['NETWORK_GROUNDING | UNKNOWN sender network; no network-wide claims'], 'sources':sources}
    # Chronological captures, not lexical backrun-research names; stale fails closed.
    valid=[]
    for p in sheet_root.glob('backrun-*/sheet-full-row-export.json'):
        try:
            d,h=load(p)
            at=dt.datetime.fromisoformat(d['captured_at_utc'].replace('Z','+00:00'))
            if 0 <= (now-at).total_seconds() <= 86400:
                valid.append((at,p,d,h))
        except (OSError,ValueError,KeyError,TypeError):
            continue
    if not valid:
        return {'facts':facts+[f'NETWORK_GROUNDING | {network}: UNKNOWN fresh Sheet snapshot; no zero approval or no live claims'], 'sources':sources}
    at,path,sheet,h=max(valid,key=lambda x:(x[0],str(x[1])))
    sources['network_sheet']={'path':str(path),'sha256':h,'captured_at_utc':at.isoformat()}
    headers=sheet['headers']; required=['Network / Route','Program / Network','Status','SEO Slug','Vertical','Wired Live?']
    if any(x not in headers for x in required) or not isinstance(sheet['rows'],dict):
        raise ValueError('network_sheet_schema_invalid')
    rows=[]
    for key,values in sheet['rows'].items():
        if not isinstance(values,list): continue
        row=dict(zip(headers,values))
        if norm(row.get('Network / Route'))==network and row.get('Vertical')=='glp1-weightloss':
            row['sheet_row']=key; rows.append(row)
    def state(row):
        status=str(row.get('Status','')).lower()
        if status=='approved' or status.startswith('approved_') or status in {'active','joined','live'}: return 'approved'
        if 'applied' in status or 'submitted' in status: return 'applied'
        if 'pending' in status: return 'pending'
        return 'other'
    counts=Counter(map(state,rows))
    eligible=[r for r in rows if 'fella' not in norm(r['Program / Network'])]
    excluded=len(rows)-len(eligible)
    facts.append('NETWORK_APPROVAL_SUMMARY_JSON '+json.dumps({'network':network,'approved':counts['approved'],'pending':counts['pending'],'applied':counts['applied'],'glp_rows':len(rows),'excluded_rows':excluded,'approval_is_not_site_or_campaign_live':True},separators=(',',':')))
    names=[str(r['Program / Network']) for r in eligible if state(r)=='approved']
    chunk='NETWORK_APPROVED_PROGRAMS | '
    for name in names:
        if len(chunk)+len(name)+3>490:
            facts.append(chunk.rstrip('; ')); chunk='NETWORK_APPROVED_PROGRAMS | '
        chunk+=name+'; '
    if names: facts.append(chunk.rstrip('; '))
    try:
        pointer,ph=load(pointer_path)
        root=Path(pointer['release_root'])
        if pointer.get('status')!='PASS' or not root.is_relative_to(BUS/'site-system/flagship-v2/releases'):
            raise ValueError('unverified_content_base')
        reg,rh=load(root/'data/program-registry.json')
        sources['registry']={'path':str(root/'data/program-registry.json'),'sha256':rh,'pointer_sha256':ph,'deployment_id':pointer.get('deployment_id')}
        live={}
        for p in reg['programs']:
            profile=p.get('profiles',{}).get('glp-clinic-scout',{})
            if p.get('commercial_state')=='live' and profile.get('vertical_fit') is True and profile.get('wired') is True and profile.get('surfaces',{}).get('review') is True:
                live[p['program_id']]=p['display_name']
        matched=set()
        for r in eligible:
            slug=str(r.get('SEO Slug',''))
            candidates={slug,slug.removesuffix('-review')}
            matched.update(live[x] for x in candidates if x in live)
        facts.append('NETWORK_SITE_PRESENCE | Exact Sheet slug/current registry joins confirm these providers have live site reviews: '+('; '.join(sorted(matched)) if matched else 'UNKNOWN exact joins')+'. This does NOT prove this network supplies the active affiliate link or has running campaigns.')
    except (OSError,ValueError,KeyError,TypeError):
        facts.append('NETWORK_SITE_PRESENCE | UNKNOWN registry binding. Approved relationships still exist independently; never infer nothing live from missing identity joins.')
    # Read the standing reporting output; cross-network records never become this network's performance.
    candidates=sorted(reporting_root.glob('*/RESULT.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    if candidates:
        rp=candidates[0]; report,hh=load(rp)
        sources['reporting']={'path':str(rp),'sha256':hh}
        facts.append('NETWORK_REPORTING | '+network+': performance UNKNOWN unless a current authenticated account-and-period-bound report is present. Latest fleet reporting receipt status '+str(report.get('status','UNKNOWN'))+'; other-network results, approvals and clicks do not prove this network revenue or campaigns.')
    else:
        facts.append('NETWORK_REPORTING | '+network+': authenticated reporting unavailable; revenue and campaigns UNKNOWN, not zero.')
    if any(len(x)>500 for x in facts):
        raise ValueError('network_grounding_fact_too_large')
    return {'facts':facts,'sources':sources}


def enforce_body(body, facts):
    summary=None
    for fact in facts:
        if fact.startswith('NETWORK_APPROVAL_SUMMARY_JSON '):
            summary=json.loads(fact.split(' ',1)[1])
    if summary and summary.get('approved',0)>0:
        if re.search(r"nothing.{0,70}live|do not have anything.{0,100}live|no (?:live|approved) (?:programs|offers|relationships)|starting point rather than.{0,30}scale",body,re.I|re.S):
            raise ValueError('network_grounding_contradiction_no_live_blanket_claim')
    if re.search(r'\b(?:we are|we.re|our company is) (?:just |still )?(?:early stage|small|new)\b',body,re.I):
        raise ValueError('brand_voice_self_deprecating')
