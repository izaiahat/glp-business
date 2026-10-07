"""Non-article source CAS producer. Pure preview transform, never an R2 article carrier.

Q24 installs the operational consumer. This module emits executable source changes
against the already-consumed clinic-scout-page-seo-v1 contract; it grants no review,
queue or release authority. Source bytes are never modified by this module.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

SCHEMA='glp-seo-non-article-intake-v1'
SOURCE='data/page-seo-copy.json'
ROUTE=re.compile(r'^/(?:providers|reviews|pharmacies)/(?:[a-z0-9-]+/)?$')

def digest(value):
    if not isinstance(value,bytes):value=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    return hashlib.sha256(value).hexdigest()

def valid_copy(copy):
    return isinstance(copy,dict) and set(copy)=={'title','h1','meta'} and all(isinstance(v,str) and v.strip() and len(v)<=4096 and not re.search(r'[<>\r\n]|\$\s*\d|\b\d+\s+(?:providers|clinics)\b',v,re.I) for v in copy.values())

def pointer(route):return '/routes/'+route.replace('~','~0').replace('/','~1')

def emit_bundle(intakes, source_path, *, week_start, evidence_bindings, generation):
    source_path=Path(source_path).resolve();raw=source_path.read_bytes();doc=json.loads(raw)
    if doc.get('schema')!='clinic-scout-page-seo-v1' or not isinstance(doc.get('routes'),dict):raise ValueError('source_schema')
    if source_path!=Path(generation['release_root']).resolve()/SOURCE:raise ValueError('source_generation_mismatch')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',week_start):raise ValueError('week_start')
    rows=[];operations=[];seen=set()
    for row in sorted(intakes['rows'],key=lambda x:str(x.get('route'))):
        route=row.get('route'); copy={k:v for k,v in row.get('exact_new_copy',{}).items() if k!='h1_text_segments'}
        state=row.get('state');reason=None
        if not isinstance(route,str) or not ROUTE.fullmatch(route):reason='HELD_UNSUPPORTED_ROUTE'
        elif route in seen:raise ValueError('duplicate_route')
        elif state!='STAGED_SOURCE_OWNER_REVIEW_REQUIRED':reason=state or 'HELD_MISSING_DISPOSITION'
        elif not valid_copy(copy):reason='HELD_UNSUPPORTED_COPY'
        elif 'fella' in route:reason='HELD_PERMANENT_EXCLUSION'
        seen.add(route)
        if reason:rows.append({'route':route,'state':reason});continue
        before=doc['routes'].get(route)
        if before==copy:rows.append({'route':route,'state':'NOOP_SOURCE_ALREADY_MATCHES'});continue
        op={'op':'replace' if route in doc['routes'] else 'add','path':pointer(route),'route':route,'before':before,'value':copy,'evidence_module':row['evidence_module']}
        op['patch_id']=digest({'week_start':week_start,'source_sha256':digest(raw),'operation':op})
        operations.append(op);rows.append({'route':route,'state':'READY_FOR_Q24_SOURCE_REVIEW','patch_id':op['patch_id']})
    envelope={'schema':SCHEMA,'kind':'NON_ARTICLE_SOURCE_PATCH','week_start':week_start,'generation':generation,'source':{'path':str(source_path),'relative_path':SOURCE,'sha256':digest(raw),'schema':doc['schema'],'owner':'src/page-seo-copy.mjs:pageSeoCopy'},'evidence_bindings':evidence_bindings,'operations':operations,'dispositions':rows,'operation_count':len(operations),'review_required':True,'publication_authorized':False,'operational_queue_entry':False,'next_consumer':'Kael Q24 non-article source-stage intake; not replace_article'}
    envelope['intake_id']=digest(envelope)
    return envelope

def apply_operations(raw, bundle):
    """Return a changed in-memory source document for an isolated dry run only."""
    if bundle.get('schema')!=SCHEMA or bundle.get('kind')!='NON_ARTICLE_SOURCE_PATCH':raise ValueError('nonarticle_schema')
    if bundle.get('publication_authorized') is not False or bundle.get('operational_queue_entry') is not False or bundle.get('review_required') is not True:raise ValueError('authority_not_granted')
    if digest({k:v for k,v in bundle.items() if k!='intake_id'})!=bundle.get('intake_id'):raise ValueError('intake_digest_changed')
    if digest(raw)!=bundle['source']['sha256']:raise ValueError('source_cas_mismatch')
    doc=deepcopy(json.loads(raw))
    if doc.get('schema')!='clinic-scout-page-seo-v1':raise ValueError('source_schema')
    if len(bundle['operations'])!=bundle['operation_count']:raise ValueError('operation_count')
    seen=set()
    for op in bundle['operations']:
        expected_id=digest({'week_start':bundle['week_start'],'source_sha256':bundle['source']['sha256'],'operation':{k:v for k,v in op.items() if k!='patch_id'}})
        if op.get('patch_id')!=expected_id:raise ValueError('patch_identity_changed')
        route=op['route']
        if not ROUTE.fullmatch(route) or route in seen or 'fella' in route:raise ValueError('route_identity')
        seen.add(route)
        if op['path']!=pointer(route) or not valid_copy(op['value']):raise ValueError('invalid_field_patch')
        if op['op']=='add' and (route in doc['routes'] or op['before'] is not None):raise ValueError('add_precondition')
        if op['op']=='replace' and (route not in doc['routes'] or doc['routes'][route]!=op['before']):raise ValueError('replace_precondition')
        if op['op'] not in ('add','replace'):raise ValueError('operation_not_supported')
        doc['routes'][route]=op['value']
    return doc
