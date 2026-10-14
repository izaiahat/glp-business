"""Issue-bound deferred actual-output review; no generic top-stakes bypass."""
import argparse,hashlib,json,re
from pathlib import Path
import fable_parent_handoff as shared
ROOT=Path('/home/ubuntu/business')
SCOPE='DEFERRED_ACTUAL_OUTPUT_REVIEW_ONLY'
SOURCES=('scripts/fable_bridge.py','scripts/fable_parent_handoff.py',
 'scripts/fable_deferred_handoff.py',
 'ventures/boring-ai/holistic-machine/scripts/bridge_authority_guard.py')

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def require_installed(bridge):
    if Path(bridge.__file__).resolve()!=ROOT/'scripts/fable_bridge.py' or Path(__file__).resolve()!=ROOT/'scripts/fable_deferred_handoff.py':
        raise RuntimeError('deferred_handoff_source_not_installed')
    if bridge.SELF_ID!=bridge.expected_author_id() or bridge.SELF_ID==bridge.KAEL_BOT_ID:
        raise RuntimeError('independent_signer_mismatch')

def checked(spec):
    p=Path(spec['path'])
    if p.is_symlink() or not p.resolve().is_relative_to(ROOT/'reports') or sha(p)!=spec['sha256']:
        raise RuntimeError('deferred_admission_artifact_drift')
    return p


def authorize(bridge,issue,admission_path,action):
    """Parent supplies native independent boundary review; hashes alone do not grant."""
    require_installed(bridge)
    p=Path(admission_path)
    if p.is_symlink() or not p.resolve().is_relative_to(ROOT/'reports'):
        raise RuntimeError('deferred_admission_path')
    data=json.loads(p.read_text())
    if (data.get('schema')!='fable-deferred-handoff-admission-v1' or data.get('scope')!=SCOPE
        or data.get('issue_id')!=issue['issue_id'] or data.get('tuple_sha256')!=issue['tuple_sha256']
        or action not in data.get('allowed_actions',[])
        or not set(data['allowed_actions'])<= {'send_request','review_deferred'}):
        raise RuntimeError('deferred_admission_scope')
    sources={name:sha(ROOT/name) for name in SOURCES}
    if data.get('sources')!=sources or not re.fullmatch('[0-9a-f]{40}',data.get('source_head','')):
        raise RuntimeError('deferred_admission_source')
    raw=json.loads(checked(data['review']).read_text())
    authenticate_admission(raw,{k:v for k,v in data.items() if k not in ('schema','review')})
    return {'admission_path':str(p),'admission_sha256':sha(p),'native_boundary_review':data['review']}


def authenticate_admission(raw,expected):
    import hermes_sol_article_review as transport
    inputs,runtime=raw['review_input'],raw['hermes_runtime']
    args=argparse.Namespace(prompt=inputs.get('inline_prompt'),prompt_file=(inputs.get('prompt_file') or {}).get('path'),
        context_file=[v['path'] for v in inputs['context_files']],json_schema_file=(inputs.get('json_schema_file') or {}).get('path'),
        max_context_chars=inputs['max_context_chars'],max_total_chars=inputs['max_total_chars'],
        model=runtime['model'],provider=runtime['provider'],effort=runtime['effort'],disable_tools=True)
    captured={}
    payload=transport.build_payload(args,captured,transport.load_schema(args,captured))
    if transport.review_input(args,payload,captured,runtime)!=inputs:
        raise RuntimeError('deferred_admission_native_input')
    custody=transport.authenticate_result(runtime['session_id'],payload,raw['result'],
        model=runtime['model'],provider=runtime['provider'],effort=runtime['effort'])
    if custody!=runtime.get('result_custody'):
        raise RuntimeError('deferred_admission_native_custody')
    text=raw['result'];verdict=json.loads(text[text.index('{'):text.rindex('}')+1])
    if verdict!=dict(expected,status='ACCEPT'):
        raise RuntimeError('deferred_admission_independent_verdict')


def load_issue(bridge,issue_id,tuple_sha256,*,bound):
    if not re.fullmatch('bridge-issue-[a-f0-9]{32}',issue_id) or not re.fullmatch('[a-f0-9]{64}',tuple_sha256):
        raise RuntimeError('deferred_handoff_identity')
    guard=bridge._authority_guard();path=bridge.ISSUE_DIR/(issue_id+'.json')
    first=json.loads(path.read_text())
    path,issue=guard.load_issue_record(path,expected_subject_id=first['subject_id'],expected_tuple_sha256=tuple_sha256,require_bound=bound)
    if issue.get('reviewer_designation') is not None:
        raise RuntimeError('deferred_handoff_designation_changed')
    return guard,path,issue


def send_request(bridge,issue_id,tuple_sha256,admission_path):
    require_installed(bridge)
    guard,path,issue=load_issue(bridge,issue_id,tuple_sha256,bound=False)
    if issue.get('schema')!='boring_ai_bridge_request_issue_v2' or any(k in issue for k in ('predecessor_issue_id','request_message_id')):
        raise RuntimeError('deferred_handoff_initial_issue_required')
    if bridge.top_stakes_category('review_request',issue['request_content'])!='production_or_high_impact_change':
        raise RuntimeError('deferred_handoff_truthful_production_scope_required')
    permit=authorize(bridge,issue,admission_path,'send_request')
    def revalidate():
        if authorize(bridge,issue,admission_path,'send_request')!=permit:
            raise RuntimeError('deferred_admission_changed')
    path,record=guard._send_registered_issue(path,issue,before_post=revalidate)
    return {'issue_record_path':str(path),'request_message_id':record['request_message_id'],'status':'BOUND'}


def dispatch(bridge,source_id,issue_id,tuple_sha256,admission_path):
    require_installed(bridge)
    if not re.fullmatch('[0-9]{17,20}',source_id):raise RuntimeError('deferred_handoff_source_id')
    _,_,issue=load_issue(bridge,issue_id,tuple_sha256,bound=True)
    if issue.get('request_message_id')!=source_id:raise RuntimeError('deferred_handoff_source_binding')
    state=json.loads(bridge.STATE.read_text())
    receipt=state.get('reviewer_receipts',{}).get(source_id,{})
    deferred_id=str(receipt.get('posted_message_id',''))
    if not re.fullmatch('[0-9]{17,20}',deferred_id):raise RuntimeError('deferred_handoff_deferral_id')
    return shared.dispatch_bound(bridge,source_id,issue_id,tuple_sha256,issue['subject_id'],deferred_id,
        lambda:authorize(bridge,issue,admission_path,'review_deferred'))
