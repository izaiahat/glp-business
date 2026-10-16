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


def test_host_capacity_refusal_preserves_exact_request_for_later_handoff(case):
    """Synthetic regression for an observed pre-claim host-admission refusal.

    A truthful top-stakes daemon deferral is not a missing dispatch producer.
    Neither a depleted host slot nor its later availability permits a new issue,
    a cursor rewind, a reviewer substitution, or fabricated invocation attempts.
    """
    from contextlib import contextmanager

    before = case.state_path.read_bytes()
    issue_path = case.issue_dir / (prior.ISSUE + '.json')
    original_issue = issue_path.read_bytes()

    @contextmanager
    def full_host():
        raise RuntimeError('parent_handoff_host_capacity')
        yield  # pragma: no cover - contextmanager contract, no acquired lease

    with patch.object(case, 'host', full_host):
        with pytest.raises(RuntimeError, match='parent_handoff_host_capacity'):
            invoke(case)

    assert case.state_path.read_bytes() == before
    assert issue_path.read_bytes() == original_issue
    case.inferences.assert_not_called()
    assert not case.posts
    assert not case.host_events

    # Same immutable fixture/request, after real admission would become available.
    assert invoke(case)['status'] == 'posted'
    case.inferences.assert_called_once()
    state = prior.bridge.load_state()
    assert state['last_id'] == json.loads(before)['last_id']
    receipt = state['reviewer_receipts'][prior.REQUEST]
    assert receipt['mode'] == 'deferred' and receipt['attempts'] == []
    assert receipt['parent_handoff']['status'] == 'posted'
    assert case.posts[0]['message_reference']['message_id'] == prior.REQUEST
    assert issue_path.read_bytes() == original_issue


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
def test_initial_canonical_send_is_claimed_once(initial_send,uncertain):
    c=initial_send
    if uncertain:c.net.side_effect=TimeoutError('ambiguous fixture')
    if uncertain:
        with pytest.raises(TimeoutError):c.send()
    else:assert c.send()['status']=='BOUND'
    assert c.path.with_suffix('.send-claim.json').exists()
    payload=json.loads(c.net.call_args.args[0].data)
    assert payload['allowed_mentions']=={'parse':[],'users':[prior.SIGNER]}
    assert payload['enforce_nonce'] is True
    with pytest.raises(Exception):c.send()
    assert c.net.call_count==1


@pytest.fixture
def initial_send():
    """Real sender/guard, private filesystem, synthetic Discord/capacity only."""
    from contextlib import ExitStack
    from datetime import datetime, timezone
    import fable_deferred_handoff as module
    import test_fable_mention_recovery as sender
    c=sender.RegisteredFixture(methodName='runTest');c.setUp()
    try:
        for k in ('predecessor_request_message_id','predecessor_issue_id','predecessor_reply_message_id','succession_reason'):
            c.issue.pop(k)
        old=str(c.artifact);c.artifact=c.artifact.rename(c.artifact.with_name('production-manifest.json'))
        c.issue['review_artifact_path']=str(c.artifact)
        c.issue['request_content']=c.issue['request_content'].replace(old,str(c.artifact))
        c.issue['request_content_sha256']=sender.guard.sha256_text(c.issue['request_content'])
        c.message['content']=c.issue['request_content'];c.write_issue()
        c.state_path=c.root/'state.json'
        c.state_path.write_text(json.dumps({'last_id':'1','reviewer_receipts':{},'successor_issues':{}}))
        c.capacity={'collected_at_utc':datetime.now(timezone.utc).isoformat(),'records':[
            dict(provider='anthropic',source='oauth_usage_api',freshness='live',capacity_status='available',
                 session_remaining_pct=50,weekly_remaining_pct=60)]}
        # Realistic snowflakes around issue creation; history covers that floor.
        issued=sender.guard._timestamp(c.issue['issued_at_utc'],'issue').timestamp()
        floor=int((issued*1000-1420070400000))*2**22
        c.message['id']=str(floor+2)
        c.history=[dict(id=str(floor-1),content='unrelated')]
        with ExitStack() as s:
            for obj,name,value in [(module,'require_installed',lambda *a:None),
                (module,'authorize',lambda *a:{'fixture':True}),
                (prior.bridge,'ISSUE_DIR',c.issues),(prior.bridge,'STATE',c.state_path),
                (prior.bridge,'SELF_ID',prior.SIGNER),
                (prior.bridge,'_authority_guard',lambda:sender.guard),
                (prior.bridge,'refresh_included_capacity',lambda:c.capacity),
                (prior.bridge,'api',lambda method,path: c.history if method=='GET' and '?before=' in path else (_ for _ in ()).throw(AssertionError('unexpected API'))),
                (sender.guard,'_discord_bot_token',lambda:'fixture')]:
                s.enter_context(patch.object(obj,name,value))
            net=s.enter_context(patch.object(sender.guard.urllib.request,'urlopen'))
            net.return_value.__enter__.return_value.read.side_effect=lambda:json.dumps(c.message).encode()
            c.net=net
            c.send=lambda:module.send_request(prior.bridge,c.issue['issue_id'],c.digest,'fixture')
            yield c
    finally:c.doCleanups()


@pytest.mark.parametrize('damage',['unknown','depleted','expired-during-token'])
def test_initial_send_requires_positive_capacity_at_post_boundary(initial_send,damage):
    from datetime import datetime, timezone, timedelta
    import test_fable_mention_recovery as sender
    c=initial_send
    before=c.path.read_bytes()
    if damage=='unknown':c.capacity['records']=[]
    elif damage=='depleted':c.capacity['records'][0]['weekly_remaining_pct']=0
    def token():
        if damage=='expired-during-token':
            c.capacity['collected_at_utc']=(datetime.now(timezone.utc)-timedelta(seconds=601)).isoformat()
        return 'fixture'
    with patch.object(sender.guard,'_discord_bot_token',side_effect=token):
        with pytest.raises(RuntimeError,match='fresh_positive_included_capacity_required'):c.send()
    c.net.assert_not_called()
    assert c.path.read_bytes()==before


@pytest.mark.parametrize('stage',['before','capacity','token'])
@pytest.mark.parametrize('activity',['request','suppressed-request','reply','receipt','artifact'])
def test_initial_send_reconciles_intervening_activity(initial_send,stage,activity):
    import copy
    import test_fable_mention_recovery as sender
    c=initial_send
    def intervene():
        if activity=='receipt':
            c.state_path.write_text(json.dumps({'last_id':'1','reviewer_receipts':{
                'alias':{'issue_id':c.issue['issue_id'],'tuple_sha256':c.digest,'attempts':[{'invoked':True}]}}}))
        elif activity=='artifact':c.artifact.write_text('changed')
        else:
            message=copy.deepcopy(c.message)
            if activity=='suppressed-request':message['mentions']=[]
            if activity=='reply':
                message.update(content='TRUE-BRIDGE FABLE — REJECT\nissue_id: '+c.issue['issue_id'],
                    author={'id':prior.SIGNER,'bot':True},type=19,
                    message_reference={'message_id':c.message['id']})
            c.history.insert(0,message)
    if stage=='before':intervene()
    def collect():
        if stage=='capacity':intervene()
        return c.capacity
    def token():
        if stage=='token':intervene()
        return 'fixture'
    with patch.object(prior.bridge,'refresh_included_capacity',side_effect=collect),patch.object(sender.guard,'_discord_bot_token',side_effect=token):
        with pytest.raises(RuntimeError):c.send()
    c.net.assert_not_called()


def test_initial_send_holds_existing_state_lock_through_post(initial_send):
    import fcntl
    c=initial_send
    def post(*args,**kwargs):
        with c.state_path.with_suffix('.json.lock').open('a+') as handle:
            with pytest.raises(BlockingIOError):fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return c.net.return_value
    c.net.side_effect=post
    assert c.send()['status']=='BOUND'


@pytest.mark.parametrize('damage',['none','request-page-two','truncated','nonmonotonic','unavailable'])
def test_initial_send_newest_first_paginated_history(initial_send,damage):
    import copy
    c=initial_send
    floor=int(c.message['id'])-2
    first=[{'id':str(floor+n),'content':'unrelated'} for n in range(200,100,-1)]
    second=[{'id':str(floor-1),'content':'unrelated'}]
    if damage=='request-page-two':
        message=copy.deepcopy(c.message);message['id']=str(floor+50);second.insert(0,message)
    elif damage=='truncated':second=[]
    elif damage=='nonmonotonic':first.reverse()
    elif damage=='unavailable':first={'error':'unavailable'}
    calls=[]
    def history(method,path):
        calls.append(path)
        return second if 'before='+str(floor+101)+'&' in path else first
    with patch.object(prior.bridge,'api',side_effect=history):
        if damage=='none':assert c.send()['status']=='BOUND'
        else:
            with pytest.raises(RuntimeError):c.send()
            c.net.assert_not_called()
    if damage in ('none','request-page-two','truncated'):
        assert any('before='+str(floor+101)+'&' in path for path in calls)


@pytest.mark.parametrize('damage',['none','mentions','edited','webhook','reply','wrong-author','hidden-request'])
def test_initial_send_only_explicit_nonintake_status_is_harmless(initial_send,damage):
    c=initial_send
    notice=dict(id=c.message['id'],content='Routing status — NOT reviewer intake or acceptance.\n'
        +c.issue['issue_id']+'\n'+c.digest,author={'id':prior.bridge.KAEL_BOT_ID,'bot':True},mentions=[])
    if damage=='mentions':notice['mentions']=[{'id':prior.SIGNER}]
    elif damage=='edited':notice['edited_timestamp']='changed'
    elif damage=='webhook':notice['webhook_id']='1'
    elif damage=='reply':notice['message_reference']={'message_id':'1'}
    elif damage=='wrong-author':notice['author']['id']=prior.SIGNER
    elif damage=='hidden-request':notice['content']+='\n'+c.issue['request_content']
    c.history.insert(0,notice)
    before=c.state_path.read_bytes()
    if damage=='none':assert c.send()['status']=='BOUND'
    else:
        with pytest.raises(RuntimeError):c.send()
        c.net.assert_not_called()
    assert c.state_path.read_bytes()==before


@pytest.mark.parametrize('change',['capacity','issue','state'])
def test_initial_send_final_history_latency_cannot_leave_stale_bindings(initial_send,change):
    from datetime import datetime, timezone, timedelta
    c=initial_send
    calls=[]
    def history(method,path):
        calls.append(path)
        if len(calls)==2:
            if change=='capacity':c.capacity['collected_at_utc']=(datetime.now(timezone.utc)-timedelta(seconds=601)).isoformat()
            elif change=='issue':
                issue=json.loads(c.path.read_text());issue['status']='BOUND';c.path.write_text(json.dumps(issue))
            elif change=='state':c.state_path.write_text('broken JSON')
        return c.history
    with patch.object(prior.bridge,'api',side_effect=history):
        with pytest.raises((RuntimeError,ValueError)):c.send()
    c.net.assert_not_called()
    assert c.path.with_suffix('.send-claim.json').exists()
    # Final-interlock refusal is terminal under the existing durable claim.
    with pytest.raises(Exception):c.send()
    c.net.assert_not_called()
