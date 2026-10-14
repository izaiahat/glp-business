"""Synthetic deferred handoff; no live model/network."""
import json,sys
from pathlib import Path
from unittest.mock import patch
import pytest
import test_fable_parent_handoff as prior
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))

@pytest.fixture
def case():
    c=prior.ParentHandoffTests(methodName='runTest');c.setUp()
    try:yield c
    finally:c.doCleanups()

def invoke(case, admission=None):
    import fable_deferred_handoff as module
    with patch.object(module,'authorize',return_value={'native_review':'fixture'}), patch.object(module,'require_installed'), patch.object(module.shared,'_host_admission',case.host), patch.object(prior.bridge,'SESSION_ID',module.shared.LEGACY_SESSION_ID), patch.object(prior.bridge,'_native_session_id',return_value=module.shared.NATIVE_SESSION_ID):
        return module.dispatch(prior.bridge,prior.REQUEST,prior.ISSUE,case.digest,admission or 'fixture')

def test_once_exact_native_review_with_retained_deferral(case):
    before=json.loads(case.state_path.read_text())
    assert invoke(case)['status']=='posted'
    assert len(case.posts)==1
    assert case.posts[0]['message_reference']['message_id']==prior.REQUEST
    claim=prior.bridge.load_state()['reviewer_receipts'][prior.REQUEST]['parent_handoff']
    assert claim['status']=='posted'
    assert prior.bridge.load_state()['last_id']==before['last_id']
    with pytest.raises(RuntimeError,match='receipt_not_eligible'):invoke(case)
    case.inferences.assert_called_once()

@pytest.mark.parametrize('damage',['attempt','claim','model-mode','extra-history','artifact'])
def test_adverse_no_inference(case,damage):
    if damage=='artifact':case.artifact.write_text('changed')
    elif damage=='extra-history':case.extra.append(dict(case.refusal,id='1555580000000000005'))
    else:
        state=json.loads(case.state_path.read_text());r=state['reviewer_receipts'][prior.REQUEST]
        if damage=='attempt':r['attempts']=[{'invoked':True}]
        if damage=='claim':r['parent_handoff']={'status':'reserved'}
        if damage=='model-mode':r['mode']='model'
        case.state_path.write_text(json.dumps(state))
    with pytest.raises(Exception):invoke(case)
    case.inferences.assert_not_called();assert not case.posts


def test_native_boundary_verdict_must_match(monkeypatch):
    import types
    import fable_deferred_handoff as module
    expected={'scope':module.SCOPE,'issue_id':'fixture','sources':{}}
    inputs={'context_files':[],'max_context_chars':10,'max_total_chars':20}
    runtime={'session_id':'fixture','model':'fixture','provider':'fixture','effort':'high','result_custody':{'fixed':'native'}}
    raw={'review_input':inputs,'hermes_runtime':runtime,'result':json.dumps(dict(expected,status='ACCEPT'))}
    t=types.SimpleNamespace(load_schema=lambda *a:None,build_payload=lambda *a:'actual input',
        review_input=lambda *a:inputs,authenticate_result=lambda *a,**kw:{'fixed':'native'})
    monkeypatch.setitem(sys.modules,'hermes_sol_article_review',t)
    module.authenticate_admission(raw,expected)
    with pytest.raises(RuntimeError,match='independent_verdict'):
        module.authenticate_admission(raw,dict(expected,scope='DEPLOY'))

@pytest.mark.parametrize('uncertain',[False,True])
def test_initial_canonical_send_is_claimed_once(uncertain):
    import fable_deferred_handoff as module
    import test_fable_mention_recovery as sender
    c=sender.RegisteredFixture(methodName='runTest');c.setUp()
    try:
        for k in ('predecessor_request_message_id','predecessor_issue_id','predecessor_reply_message_id','succession_reason'):c.issue.pop(k)
        old=str(c.artifact);c.artifact=c.artifact.rename(c.artifact.with_name('production-manifest.json'))
        c.issue['review_artifact_path']=str(c.artifact)
        c.issue['request_content']=c.issue['request_content'].replace(old,str(c.artifact))
        c.issue['request_content_sha256']=sender.guard.sha256_text(c.issue['request_content'])
        c.message['content']=c.issue['request_content']
        c.write_issue()
        _initial_sender_check(module,sender,c,uncertain)
    finally:c.doCleanups()


def _initial_sender_check(module,sender,c,uncertain):
    guard=sender.guard
    with patch.object(module,'require_installed'),patch.object(module,'authorize',return_value={'fixture':True}),patch.object(prior.bridge,'ISSUE_DIR',c.issues),patch.object(prior.bridge,'_authority_guard',return_value=guard),patch.object(guard,'_discord_bot_token',return_value='fixture'),patch.object(guard.urllib.request,'urlopen') as net:
        _exercise_initial_send(module,guard,c,net,uncertain)


def _exercise_initial_send(module,guard,c,net,uncertain):
    if uncertain:net.side_effect=TimeoutError('ambiguous fixture')
    else:net.return_value.__enter__.return_value.read.return_value=json.dumps(c.message).encode()
    call=lambda:module.send_request(prior.bridge,c.issue['issue_id'],c.digest,'fixture')
    if uncertain:
        with pytest.raises(TimeoutError):call()
    else:assert call()['status']=='BOUND'
    assert c.path.with_suffix('.send-claim.json').exists()
    payload=json.loads(net.call_args.args[0].data)
    assert payload['allowed_mentions']=={'parse':[],'users':[prior.SIGNER]}
    assert payload['enforce_nonce'] is True
    with pytest.raises(Exception):call()
    assert net.call_count==1
