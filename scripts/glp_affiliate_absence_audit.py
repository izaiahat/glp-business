#!/usr/bin/env python3
"""Account for every adverse Sheet offer not selected by the site, without clicks."""
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl
from glp_affiliate_registry_sync import selected_relationships

class Anchors(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.hrefs=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('a','form'):
            a=dict(attrs)
            if a.get('href') or a.get('action'):self.hrefs.append(a.get('href') or a['action'])

def urls_in_documents(public):
    links=[];files=[]
    def visit(value,file):
        if isinstance(value,str):
            if '<a' in value or '<form' in value:
                parser=Anchors();parser.feed(value)
                links.extend((file,href) for href in parser.hrefs)
        elif isinstance(value,dict):
            for v in value.values():visit(v,file)
        elif isinstance(value,list):
            for v in value:visit(v,file)
    for p in sorted(public.rglob('*')):
        if p.is_file() and p.suffix in ('.html','.json'):
            raw=p.read_bytes();rel=str(p.relative_to(public));files.append({'file':rel,'sha256':hashlib.sha256(raw).hexdigest()})
            if p.suffix=='.json':visit(json.loads(raw),rel)
            else:visit(raw.decode(),rel)
    if not files:raise ValueError('absence_audit_public_empty')
    return links,files

def same_issued(a,b):
    if not a or not b:return False
    left,right=urlsplit(html.unescape(a)),urlsplit(html.unescape(b))
    return (left.scheme in ('http','https') and left.hostname==right.hostname and left.path==right.path
            and set(parse_qsl(left.query)).issubset(set(parse_qsl(right.query))))

def audit(site,registry_report,snapshot,*,public=None):
    public=public or site/'public';links,files=urls_in_documents(public)
    rows={r['sheet_row']:r['cells'] for r in snapshot['rows']}
    selected=selected_relationships(site);registry=json.loads((site/'data/program-registry.json').read_text())
    ids={p['program_id'] for p in registry['programs']}
    source=site/'functions/go/program-manifest.js'
    raw=source.read_text();runtime=json.loads(raw.split('Object.freeze(',1)[1].rsplit(');',1)[0])
    decisions=[]
    for missing in registry_report['unmapped_adverse']:
        key=(missing['network'],missing['program_id']);cells=rows[missing['sheet_row']]
        matched=[pid for pid,pair in selected.items() if pair==key and pid in ids]
        runtime_matches=[]
        for slug,program in runtime['programs'].items():
            if program.get('live_link_allowed') is not True:continue
            if (str(program.get('network','')).casefold()==key[0] and str(program.get('network_offer_id',''))==key[1]) or same_issued(cells[14],program.get('tracking',{}).get('base_url','')):
                runtime_matches.append(slug)
        linked=[file for file,href in links if same_issued(cells[14],href)] if cells[14] else []
        if matched or runtime_matches or linked:
            raise ValueError('unmapped_adverse_has_site_presence:'+json.dumps({'offer':key,'mapped':matched,'runtime':runtime_matches,'files':sorted(set(linked))}))
        decisions.append({**missing,'program_name':cells[2],
            'disposition':'NO_LIVE_EXACT_OFFER_PRESENCE',
            'reason':'No selected registry identity, no live runtime offer, and no retained issued-link anchor in complete rendered HTML/lazy JSON. A different rail for the same brand is not withdrawn by name.',
            'selected_registry_matches':matched,'active_runtime_matches':runtime_matches,
            'issued_link_present_in_sheet':bool(cells[14]),'issued_link_sha256':hashlib.sha256(str(cells[14]).encode()).hexdigest() if cells[14] else None,
            'rendered_issued_link_matches':[],
            'sheet_row_sha256':hashlib.sha256(json.dumps(cells,separators=(',',':')).encode()).hexdigest()})
    if len(decisions)!=len(registry_report['unmapped_adverse']):raise ValueError('absence_audit_denominator_mismatch')
    return {'schema':'affiliate-unmapped-adverse-audit-v1','status':'PASS','remaining_unmapped':len(decisions),
            'evidenced_no_live_offer_presence':len(decisions),'unaccounted':0,'rows':decisions,
            'html_json_documents':len(files),'anchor_count':len(links),'documents':files,
            'registry_sha256':hashlib.sha256((site/'data/program-registry.json').read_bytes()).hexdigest(),
            'runtime_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'tracking_clicks':0}
