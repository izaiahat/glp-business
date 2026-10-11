"""Offline DOT1 custody, effect idempotency and safe refill contracts."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


def test_dispatch_import_and_subscription_check_are_real_and_read_only(tmp_path):
    import dot_dispatch as d
    from rook_mcp_events import FinchIdentity, OPERATOR, BOT, ROOK_BOT
    for name, original in d.DOTS.items():
        directory = tmp_path / name
        directory.mkdir()
        dot = dict(original, state_dir=str(directory))
        policy = {'issuer': d.ISSUER, 'audience': dot['audience'], 'subject': d.SUBJECT}
        if name == 'finch':
            policy['identity'] = FinchIdentity(d.DOTS['finch']['channel'], '1555387660644782121',
                dot['user'], Path('/home/ubuntu/.hermes/secrets/dot-bots/finch.token'),
                (OPERATOR, BOT, ROOK_BOT)).policy_fields()
        row = dict(id='sub_test', policy=d.digest(d.canonical(policy)), subject=d.SUBJECT,
                   expires=time.time()+600, channel_id=dot['channel'], guild_id=d.GUILD)
        database = directory / 'rook.sqlite3'
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE subscriptions(value TEXT)')
            db.execute('CREATE TABLE requests(value TEXT)')
            db.execute('INSERT INTO subscriptions VALUES (?)', (json.dumps(row),))
        before = database.read_bytes()
        assert d.subscription_ready(dot)['id'] == 'sub_test'
        assert database.read_bytes() == before
    requirements = Path(__file__).resolve().parents[1] / 'ops/dots/requirements.txt'
    assert 'standardwebhooks==1.0.0' in requirements.read_text()


def test_ingestion_writes_coordinator_receipt_and_is_idempotent(tmp_path):
    import dot_lane as lane
    import dot_dispatch as d
    ticket = 'rook-glp-354-test'
    directory = tmp_path / 'reports/ops/dot-tickets' / ticket
    directory.mkdir(parents=True)
    text = 'RESULT '+ticket+' PR https://github.com/izaiahat/glp-business/pull/999 head '+('a'*40)
    reply = '# '+ticket+'\n\n'+d.TRUST+'\n\n'+text+'\n'
    row = dict(status='staged', outcome='result', correlation='discord-123',
               author_id=d.DOTS['rook']['user'], trust=d.TRUST, operator_approval=False,
               text_truncated=False, text=text, reply_id='456', result_key=ticket)
    state = dict(ticket=ticket, dot='rook', issue='GLP-354', task_owner='coordinator',
                 parent='fable-plan-2-20261009', state='complete', request_id='123',
                 reply_id='456', correlation='discord-123', sent_at=10,
                 reply_sha256=hashlib.sha256(reply.encode()).hexdigest())
    for name, value in [('TICKET.json', state), ('COMPLETION.json', {'results':[row]})]:
        (directory/name).write_text(json.dumps(value))
    (directory/'REPLY.md').write_text(reply)
    calls = []
    effects = lambda kind, issue, body, marker: calls.append(kind) or {'id':kind+'1'}
    pr = lambda url: dict(url=url, head_sha='a'*40, author='rook', state='OPEN')
    assert lane.ingest(tmp_path, since=0, effects=effects, pr_lookup=pr)[0]['status'] == 'ingested'
    assert lane.ingest(tmp_path, since=0, effects=effects, pr_lookup=pr)[0]['status'] == 'ingested'
    assert calls == ['linear', 'discord']
    receipt = json.loads((tmp_path/'reports/ops/fleet-ingest'/ (ticket+'.json')).read_text())
    assert receipt['review']['status'] == 'pending_coordinator_non_author_review'
    assert receipt['prs'][0]['head_sha'] == 'a'*40
    inbox = tmp_path/'reports/ops/fable-plan-2-20261009/lanes/DOTS/INBOX.jsonl'
    assert len(inbox.read_text().splitlines()) == 1
    row['author_id'] = 'unexpected'
    (directory/'COMPLETION.json').write_text(json.dumps({'results':[row]}))
    with pytest.raises(ValueError, match='custody'):
        lane.ingest(tmp_path, since=0, effects=effects, pr_lookup=pr)


def test_refill_plan_order_once_per_scope_and_idle_only(tmp_path):
    import dot_lane as lane
    policy = dict(scopes=[
        dict(id='later', issue='GLP-328', dots=['rook','finch'], effect='public_research',
             coordinator_authorized=True, brief='Public sources only; no sends.', files=[]),
        dict(id='first', issue='GLP-253', dots=['rook','finch'], effect='public_research',
             coordinator_authorized=True, brief='Public sources only; no sends.', files=[])])
    issues = [dict(identifier='GLP-328', priority=1, state={'type':'started'}),
              dict(identifier='GLP-253', priority=0, state={'type':'backlog'})]
    plan = {'issues':[{'issue':'GLP-253','order':6}, {'issue':'GLP-328','order':17}]}
    assert lane.select('rook', issues, plan, policy, {}, [])[0]['id'] == 'first'
    assert lane.select('rook', issues, plan, policy, {'first':{}}, [])[0]['id'] == 'later'
    assert lane.select('rook', issues, plan, policy, {}, [dict(dot='rook', state='sent', issue='GLP-93')]) is None
    assert lane.select('finch', issues, plan, policy, {}, [dict(dot='rook', state='sent', issue='GLP-253')])[0]['id'] == 'later'
    policy['scopes'][0]['effect'] = 'send'
    assert lane.select('finch', issues, plan, policy, {'first':{}}, []) is None
    policy['scopes'][0]['effect'] = 'independent_pr'
    policy['scopes'][0]['files'] = ['scripts/shared.py']
    policy['scopes'][0]['coordinator_authorized'] = False
    assert lane.select('finch', issues, plan, policy, {'first':{}}, []) is None


def test_new_acceptance_versions_replenish_without_repeating_same_scope():
    import dot_lane as lane
    policy = dict(scopes=[],continuous_public_research_issues=['GLP-93'])
    issue = dict(identifier='GLP-93',title='Provider source evidence',description='One unresolved public terms gap',priority=1,state={'type':'started'})
    plan = {'issues':[{'issue':'GLP-93','order':1}]}
    first = lane.select('rook',[issue],plan,policy,{},[])[0]
    assert lane.select('rook',[issue],plan,policy,{first['id']:{}},[]) is None
    issue['description'] += '; newly admitted official terms question'
    assert lane.select('rook',[issue],plan,policy,{first['id']:{}},[])[0]['id'] != first['id']


def test_cloud_brief_requires_both_renewed_environment_confirmations(tmp_path):
    import dot_lane as lane
    p = tmp_path/'reports/ops/fable-plan-2-20261009/lanes/R1/RESULT.json'
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'status':'IN_PROGRESS','cloud_ready_for_dots':False}))
    assert lane.cloud_brief(tmp_path, 'rook') == ''
    p.write_text(json.dumps({'status':'DONE_READY_TO_CLOSE','cloud_ready_for_dots':True}))
    assert 'hermes-evidence-account1' in lane.cloud_brief(tmp_path,'rook')
    assert 'hermes-evidence-account2' in lane.cloud_brief(tmp_path,'finch')


def test_rook_cloud_updates_exact_readback_and_no_replay(tmp_path):
    import dot_lane as lane
    import dot_dispatch as d
    message = dict(id='123456789',channel_id=d.DOTS['rook']['channel'],author={'id':d.DOTS['rook']['user']},
                   content='Codex Cloud Tailscale public configuration receipt; private key not present.')
    def api(method, route):
        return [message] if '?limit=' in route else message
    events = []
    def effects(kind, issue, body, marker):
        events.append((kind,issue))
        return {'id':str(len(events))}
    lane.cloud_updates(tmp_path,api=api,effects=effects)
    lane.cloud_updates(tmp_path,api=api,effects=effects)
    assert events == [('linear','GLP-359'),('discord','GLP-359')]
    inbox = tmp_path/'reports/ops/fable-plan-2-20261009/lanes/DOTS/INBOX.jsonl'
    receipt = json.loads(inbox.read_text())
    assert receipt['schema'] == 'DOT_CLOUD_UPDATE_V1'
    assert receipt['operator_approval'] is False


def test_full_refill_cloud_handoff_precedes_rook_and_busy_is_silent(tmp_path,monkeypatch):
    import dot_lane as lane
    import dot_dispatch as d
    policy = dict(ingest_since=0,scopes=[
        dict(id='one',issue='GLP-253',dots=['rook'],effect='public_research',coordinator_authorized=True,files=[],brief='Public official source question.'),
        dict(id='two',issue='GLP-328',dots=['finch'],effect='public_research',coordinator_authorized=True,files=[],brief='Public official source question.')])
    plan = {'issues':[{'issue':'GLP-253','order':1},{'issue':'GLP-328','order':2}]}
    p = tmp_path/'ops/dots/lane-policy.json'
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(policy))
    p = tmp_path/'reports/ops/fable-plan-2-20261009/PLAN.json'
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(plan))
    issues = [dict(identifier='GLP-253',state={'type':'started'}),dict(identifier='GLP-328',state={'type':'started'})]
    events = []
    monkeypatch.setattr(d,'subscription_ready',lambda dot:{'id':'ready'})
    def cloud(root):
        events.append('cloud')
    def dispatch(dot,issue,brief,root,parent,owner):
        events.append(dot)
        ticket = dot+'-'+issue.lower()+'-test'
        p = Path(root)/ticket/'TICKET.json'
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(dict(ticket=ticket,dot=dot,issue=issue,state='sent')))
        return dict(ticket=ticket,request_id='123456789',exact_readback=True)
    effects = lambda *args: {'id':'comment'}
    out = lane.refill(tmp_path,issues=issues,dispatch=dispatch,effects=effects,cloud_reader=cloud)
    assert [r['status'] for r in out] == ['dispatched','dispatched']
    assert events == ['cloud','rook','finch']
    out = lane.refill(tmp_path,issues=issues,dispatch=dispatch,effects=effects,cloud_reader=cloud)
    assert [r['status'] for r in out] == ['busy','busy']
    assert events == ['cloud','rook','finch']


def test_uncertain_ingest_queries_but_does_not_repeat_external_write(tmp_path):
    # The already exercised receipt fixture provides a real staged terminal pair.
    import dot_lane as lane
    test_ingestion_writes_coordinator_receipt_and_is_idempotent(tmp_path)
    directory = tmp_path/'reports/ops/dot-tickets/rook-glp-354-test'
    row = json.loads((directory/'COMPLETION.json').read_text())
    import dot_dispatch as d
    row['results'][0]['author_id'] = d.DOTS['rook']['user']
    (directory/'COMPLETION.json').write_text(json.dumps(row))
    receipt_path = tmp_path/'reports/ops/fleet-ingest/rook-glp-354-test.json'
    receipt = json.loads(receipt_path.read_text())
    receipt['effects']['linear'] = {'status':'posting'}
    receipt_path.write_text(json.dumps(receipt))
    calls = []
    def recover(kind, issue, body, marker):
        calls.append(kind)
        raise ValueError('uncertain_linear_effect_absent_coordinator_reconcile_required')
    with pytest.raises(ValueError, match='uncertain_linear'):
        lane.ingest(tmp_path, since=0, effects=recover)
    assert calls == ['linear_reconcile']
