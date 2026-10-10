"""Offline contract tests; never send to Linear or Discord."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import subprocess


def test_real_cli_silent_clean_minimized_violation_and_sanitized_error(tmp_path):
    auditor()
    source=tmp_path/'fixture.json'; evidence=tmp_path/'evidence.json'
    command=[sys.executable,str(ROOT/'scripts/linear_audit.py'),'--fixture',str(source),'--orchestrator-id',OP,'--now',NOW,'--evidence',str(evidence)]
    source.write_text(json.dumps([issue()]))
    run=subprocess.run(command,capture_output=True,text=True)
    assert run.returncode == 0 and run.stdout == run.stderr == ''
    assert json.loads(evidence.read_text())['status'] == 'CLEAN'
    source.write_text(json.dumps([issue(project=None,description='PHI SECRET')]))
    run=subprocess.run(command,capture_output=True,text=True)
    assert run.returncode == 1
    assert 'MISSING_PROJECT' in run.stdout
    assert 'PHI SECRET' not in run.stdout+run.stderr+evidence.read_text()
    source.write_text('{broken SECRET')
    run=subprocess.run(command,capture_output=True,text=True)
    assert run.returncode == 2 and run.stdout == '' and 'SECRET' not in run.stderr
    assert json.loads(evidence.read_text())['unresolved_count'] is None

def test_obsolete_pending_delivery_does_not_poison_distinct_audit(tmp_path):
    import sqlite3, hashlib
    a = auditor(); d = FakeDiscord(); state = tmp_path / 'delivery.sqlite'
    report = a.audit([issue(project=None)], OP, NOW)
    assert a.notify(report, state, d) == 'CONFIRMED'
    current_content = a.notice_chunks(report)[0]
    assert current_content.startswith('Linear audit v2 ')
    old_content = current_content.replace('Linear audit v2 ', 'Linear audit ', 1)
    digest = hashlib.sha256(old_content.encode()).hexdigest()
    with sqlite3.connect(state) as db:
        db.execute('INSERT INTO delivery VALUES (?,?,?,?,?)', (digest, a.CHANNEL, old_content, None, 'PENDING'))
    assert a.notify(report, state, d) == 'DUPLICATE_CONFIRMED'
    assert len(d.posts) == 1
    with sqlite3.connect(state) as db:
        assert db.execute('SELECT message_id,status FROM delivery WHERE digest=?', (digest,)).fetchone() == (None, 'PENDING')
    # If those exact bytes become required again, never resend or claim success.
    original = a.notice_chunks
    a.notice_chunks = lambda report: [old_content]
    try:
        assert a.notify(report, state, d) == 'DELIVERY_UNKNOWN'
        assert len(d.posts) == 1
    finally:
        a.notice_chunks = original


class FakeDiscord:
    def __init__(self): self.posts=[]; self.messages={}; self.fail_send=False; self.fail_read=False
    def send(self,channel,content):
        self.posts.append(content)
        if self.fail_send: raise RuntimeError('SECRET ambiguous')
        mid=str(len(self.posts)); self.messages[mid]={'id':mid,'channel_id':channel,'content':content}
        return self.messages[mid]
    def readback(self,channel,mid):
        if self.fail_read: raise RuntimeError('SECRET lost readback')
        return self.messages[mid]

def test_notification_confirmed_duplicate_and_missing_receipt_no_resend(tmp_path):
    a=auditor(); report=a.audit([issue(project=None)],OP,NOW)
    d=FakeDiscord(); state=tmp_path/'delivery.sqlite'
    assert a.notify(report,state,d) == 'CONFIRMED'
    assert a.notify(report,state,d) == 'DUPLICATE_CONFIRMED'
    assert len(d.posts) == 1
    d=FakeDiscord(); d.fail_send=True; state=tmp_path/'unknown.sqlite'
    assert a.notify(report,state,d) == 'DELIVERY_UNKNOWN'
    d.fail_send=False
    assert a.notify(report,state,d) == 'DELIVERY_UNKNOWN'
    assert len(d.posts) == 1

def test_notification_readback_failure_reconciles_exact_id_without_resending(tmp_path):
    a=auditor(); report=a.audit([issue(project=None)],OP,NOW)
    d=FakeDiscord(); d.fail_read=True; state=tmp_path/'state.sqlite'
    assert a.notify(report,state,d) == 'DELIVERY_UNKNOWN'
    d.fail_read=False
    assert a.notify(report,state,d) == 'DUPLICATE_CONFIRMED'
    assert len(d.posts) == 1

def test_notification_chunks_without_truncation_and_no_clean_send(tmp_path):
    a=auditor(); d=FakeDiscord(); state=tmp_path/'state.sqlite'
    rows=[issue(id='i'+str(n),identifier='GLP-'+str(n),url='https://linear.app/tilton-group/issue/GLP-'+str(n),project=None) for n in range(1,61)]
    report=a.audit(rows,OP,NOW)
    assert a.notify(report,state,d) == 'CONFIRMED'
    assert all(len(c)<=1900 for c in d.posts)
    assert all(f['url'] in '\n'.join(d.posts) for f in report['findings'])
    count=len(d.posts)
    assert a.notify(a.audit([],OP,NOW),state,d) == 'NOT_NEEDED'
    assert len(d.posts) == count

import pytest

@pytest.mark.parametrize('labels,reason', [([], 'INVALID_LANE'),
    ([{'id':'a','name':'lane:ads'}, {'id':'b','name':'lane:alien'}, {'id':'c','name':'class:ops'}], 'INVALID_LANE'),
    ([{'id':'a','name':'lane:ads'}, {'id':'b','name':'class:alien'}], 'INVALID_CLASS')])
def test_exactly_one_valid_namespace(labels, reason):
    assert reason in {f['reason'] for f in auditor().audit([issue(labels=labels)], OP, NOW)['findings']}

def test_done_result_requires_comment_path_not_description_or_bare_word():
    a = auditor()
    row = issue(state={'id':'done','name':'Done','type':'completed'}, parent={'id':'parent'},
                description='/home/ubuntu/business/reports/private/RESULT.json')
    assert 'DONE_WITHOUT_RESULT_PATH' in {f['reason'] for f in a.audit([row], OP, NOW)['findings']}
    row['comments'] = [comment('RESULT.json')]
    assert a.audit([row], OP, NOW)['findings']
    row['comments'] = [comment('/home/ubuntu/business/reports/test/RESULT.json')]
    assert a.audit([row], OP, NOW)['findings'] == []

def comment(body, when='2026-09-11T10:59:00Z', author='86ceaadc-3c24-4810-ac3a-985cfc561a05', **extra):
    return dict(id='c1', body=body, createdAt=when, editedAt=None, archivedAt=None, user={'id':author} if author else None, **extra)

def test_triage_aged_needs_explicit_orchestrator_disposition_not_ack():
    a = auditor()
    row = issue(createdAt='2026-09-09T11:00:00Z', comments=[comment('ACK seen')])
    assert 'STALE_TRIAGE_NO_DISPOSITION' in {f['reason'] for f in a.audit([row], OP, NOW)['findings']}
    row['comments'] = [comment('FLEET_TRIAGE_DISPOSITION_V1\n```json\n'+json.dumps({'issue_id':'i1','disposition':'deferred','reason':'await scoped review'})+'\n```')]
    # Even an authenticated legacy disposition cannot excuse remaining in Triage.
    assert 'STALE_TRIAGE_NO_DISPOSITION' in reasons(row)
    row['comments'][0]['user'] = None
    assert 'STALE_TRIAGE_NO_DISPOSITION' in reasons(row)
    row['comments'] = [comment('await scoped review')]
    row['state'] = {'id':'s2', 'name':'Backlog', 'type':'backlog'}
    row['history'] = [event(toState={'id':'s2','name':'Backlog'})]
    assert row['state']['name'] != 'Triage'
    assert a.audit([row], OP, NOW)['findings'] == []
    row['history'][0]['actor'] = None
    assert 'TRANSITION_ACTOR_UNKNOWN:h1' in reasons(row)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
NOW = '2026-09-11T12:00:00Z'
OP = '86ceaadc-3c24-4810-ac3a-985cfc561a05'

def auditor():
    path = ROOT / 'scripts/linear_audit.py'
    assert path.exists(), 'read-only audit implementation missing'
    spec = importlib.util.spec_from_file_location('audit_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def issue(**updates):
    row = dict(id='i1', identifier='GLP-1', url='https://linear.app/tilton-group/issue/GLP-1/private-title',
               createdAt='2026-09-11T11:00:00Z', updatedAt='2026-09-11T11:00:00Z',
               archivedAt=None, trashed=False,
               parent=None, project={'id':'p1'}, state={'id':'s1','name':'Triage','type':'triage'},
               labels=[{'id':'l1','name':'lane:ads'},{'id':'l2','name':'class:ops'}],
               description=None, history=[], comments=[])
    row.update(updates)
    return row

def event(**updates):
    row = dict(id='h1', createdAt='2026-09-11T11:00:00Z', fromStateId='s1', toStateId='s2',
               fromState={'id':'s1','name':'Triage'}, toState={'id':'s2','name':'Todo'}, actor={'id':OP},
               fromParentId=None, toParentId=None)
    row.update(updates)
    return row

def transition_receipt(**updates):
    data = dict(issue_id='i1', from_state_id='s1', to_state_id='s2', result_path='/home/ubuntu/business/reports/test/RESULT.json')
    data.update(updates)
    return 'FLEET_TRANSITION_V1\n```json\n'+json.dumps(data)+'\n```'

def reasons(row):
    return {f['reason'] for f in auditor().audit([row], OP, NOW)['findings']}

def test_native_non_done_transition_needs_attributed_history_not_comment():
    row = issue(history=[event()])
    assert not reasons(row)
    row['comments'] = [comment(transition_receipt())]
    assert not reasons(row)
    row['history'][0]['actor'] = {'id':'fleet'}
    assert 'TRANSITION_NON_ORCHESTRATOR:h1' in reasons(row)

@pytest.mark.parametrize('when', ['2026-09-11T10:59:59Z', '2026-09-11T12:00:01Z'])
def test_native_history_outside_issue_lifetime_or_audit_clock_refuses(when):
    assert 'TRANSITION_TIME_UNKNOWN:h1' in reasons(issue(history=[event(createdAt=when)]))

@pytest.mark.parametrize('marker', ['FLEET_RESULT_V1', 'FLEET_TRANSITION_V1'])
def test_done_retains_exact_result_or_legacy_closure_proof(marker):
    data = dict(issue_id='i1', outcome='PASS', result_path='/home/ubuntu/business/reports/test/RESULT.json', result_sha256='a'*64)
    body = marker+'\n```json\n'+json.dumps(data)+'\n```' if marker == 'FLEET_RESULT_V1' else transition_receipt()
    row = issue(history=[event(toState={'id':'s2','name':'Done'})], comments=[comment(body)])
    assert not reasons(row)
    row['comments'] = []
    assert 'TRANSITION_WITHOUT_RECEIPT:h1' in reasons(row)

@pytest.mark.parametrize('change', [dict(issue_id='other'), dict(outcome='FAIL'), dict(result_path='RESULT.json'), dict(result_sha256='bad')])
def test_done_result_binding_negatives(change):
    data = dict(issue_id='i1', outcome='PASS', result_path='/home/ubuntu/business/reports/test/RESULT.json', result_sha256='a'*64)
    data.update(change)
    body = 'FLEET_RESULT_V1\n```json\n'+json.dumps(data)+'\n```'
    assert 'TRANSITION_WITHOUT_RECEIPT:h1' in reasons(issue(history=[event(toState={'id':'s2','name':'Done'})], comments=[comment(body)]))

@pytest.mark.parametrize('change', [dict(user=None), dict(user={'id':'fleet'}),
    dict(createdAt='2026-09-11T09:00:00Z'), dict(editedAt='2026-09-11T11:01:00Z'),
    dict(body=transition_receipt(issue_id='other')), dict(body=transition_receipt(to_state_id='other'))])
def test_bad_receipts_do_not_cover_transition(change):
    c = comment(transition_receipt()); c.update(change)
    assert 'TRANSITION_WITHOUT_RECEIPT:h1' in reasons(issue(history=[event(toState={'id':'s2','name':'Done'})], comments=[c]))

def test_initial_triage_not_confused_with_actual_transition_or_unknown_actor():
    initial = event(fromStateId=None, fromState=None, toStateId='s1', toState={'id':'s1','name':'Triage'}, actor={'id':'fleet'})
    assert not reasons(issue(history=[initial]))
    initial['createdAt'] = '2026-09-11T11:30:00Z'
    assert 'TRANSITION_ORIGIN_UNKNOWN:h1' in reasons(issue(history=[initial]))
    assert 'TRANSITION_ACTOR_UNKNOWN:h1' in reasons(issue(history=[event(actor=None)]))

def test_receipt_cannot_cover_repeated_history_and_child_history_is_not_main():
    row = issue(history=[event(toState={'id':'s2','name':'Done'}), event(id='h2', createdAt='2026-09-11T11:02:00Z', toState={'id':'s2','name':'Done'})], comments=[comment(transition_receipt())])
    assert 'TRANSITION_WITHOUT_RECEIPT:h2' in reasons(row)
    row['parent'] = {'id':'p'}
    assert not reasons(row)

def test_schema_unknown_not_clean_or_zero():
    a = auditor()
    for update in [dict(labels=None), dict(state=None), dict(comments=None), dict(history=None), dict(createdAt='nonsense')]:
        result = a.audit([issue(**update)], OP, NOW)
        assert result['status'] == 'ERROR'
        assert result['complete'] is False
        assert result['unresolved_count'] is None
        assert result['unknown_count'] == 1
    row = issue(); del row['parent']
    assert a.audit([row], OP, NOW)['status'] == 'ERROR'


def test_triage_uses_latest_entry_and_disposition_not_previous_visit():
    row = issue(createdAt='2026-09-01T11:00:00Z', parent={'id':'p'}, history=[event(toStateId='s1',fromStateId='s2',toState={'id':'s1','name':'Triage'},fromState={'id':'s2','name':'Todo'})])
    assert not reasons(row)
    row['history'][0]['createdAt'] = '2026-09-09T11:00:00Z'
    row['comments'] = [comment('FLEET_TRIAGE_DISPOSITION_V1\n```json\n'+json.dumps({'issue_id':'i1','disposition':'deferred','reason':'review'})+'\n```',when='2026-09-02T11:00:00Z')]
    assert 'STALE_TRIAGE_NO_DISPOSITION' in reasons(row)


def page(nodes, more=False, cursor=None):
    return {'nodes':nodes,'pageInfo':{'hasNextPage':more,'endCursor':cursor}}

class SequenceClient:
    def __init__(self, responses):
        self.responses = iter(responses); self.calls=[]
    def gql(self, query, variables):
        assert query.lstrip().startswith('query'), 'no mutations'
        self.calls.append((query, copy.deepcopy(variables)))
        value = next(self.responses)
        if isinstance(value, Exception): raise value
        return value

def test_pagination_rejects_partial_duplicate_and_cursor_cycle():
    a = auditor()
    client = SequenceClient([{'issues':page([{'id':'a'}], True, 'x')},{'issues':page([{'id':'b'}])}])
    assert a.pages(client, 'query {}', {}, ('issues',)) == [{'id':'a'},{'id':'b'}]
    assert client.calls[1][1]['after'] == 'x'
    for responses in [[{'issues':page([],True,None)}],
                      [{'issues':page([{'id':'a'}],True,'x')},{'issues':page([{'id':'a'}])}],
                      [{'issues':page([],True,'x')},{'issues':page([],True,'x')}],
                      [{'issues':{'nodes':[]}}], [{'issues':None}],
                      [{'issues':page([],True,'x')}, RuntimeError('secret API error')]]:
        with pytest.raises(a.AuditError): a.pages(SequenceClient(responses), 'query {}', {}, ('issues',))

def test_fetch_paginates_embedded_tails_and_checks_snapshot():
    a = auditor()
    row = issue(); row.pop('description')
    raw = dict(row, labels=page(row['labels'],True,'labels1'), history=page([],True,'history1'), comments=page([],True,'comments1'))
    client=SequenceClient([{'issues':page([raw])}, {'issue':{'labels':page([{'id':'l3','name':'source:manual'}])}},
                           {'issue':{'comments':page([comment('safe')])}}, {'issue':{'history':page([event()])}},
                           {'issues':page([{'id':'i1','updatedAt':row['updatedAt']}])}])
    rows=a.fetch(client,'team-id')
    assert len(rows[0]['labels']) == 3
    assert len(rows[0]['history']) == len(rows[0]['comments']) == 1
    assert [v['after'] for q,v in client.calls[1:4]] == ['labels1','comments1','history1']
    bad=SequenceClient([{'issues':page([dict(row,labels=page(row['labels']),comments=page([]),history=page([]))])},
                        {'issues':page([{'id':'i1','updatedAt':'changed'}])}])
    with pytest.raises(a.AuditError): a.fetch(bad,'team-id')

def test_null_state_reference_and_initial_actor_remain_unknown():
    assert 'TRANSITION_STATE_UNKNOWN:h1' in reasons(issue(history=[event(toState=None)],comments=[comment(transition_receipt())]))
    initial=event(fromStateId=None,fromState=None,toStateId='s1',toState={'id':'s1','name':'Triage'},actor=None)
    assert 'INITIAL_ACTOR_UNKNOWN:h1' in reasons(issue(history=[initial]))

def test_historical_top_level_detected_after_reparenting():
    row=issue(parent={'id':'p'},history=[event(actor={'id':'fleet'}),event(id='hp',createdAt='2026-09-11T11:30:00Z',fromStateId=None,toStateId=None,fromState=None,toState=None,fromParentId=None,toParentId='p')])
    assert 'TRANSITION_NON_ORCHESTRATOR:h1' in reasons(row)


def test_lock_conflict_never_overwrites_inflight_evidence(tmp_path):
    a=auditor(); target=tmp_path/'report.json'; target.write_text('{"in_flight":true}')
    with a.exclusive(str(target)+'.lock'):
        result=a.main(['--orchestrator-id',OP,'--evidence',str(target),'--fixture',str(tmp_path/'absent.json')])
    assert result == 2
    assert target.read_text() == '{"in_flight":true}'


def test_lost_or_corrupt_notification_state_and_wrong_readback_never_resend(tmp_path):
    a=auditor(); d=FakeDiscord(); state=tmp_path/'delivery.sqlite'; report=a.audit([issue(project=None)],OP,NOW)
    assert a.notify(report,state,d) == 'CONFIRMED'
    state.unlink()
    assert a.notify(report,state,d) == 'DELIVERY_UNKNOWN'
    assert len(d.posts) == 1
    state=tmp_path/'empty.sqlite'; state.touch()
    assert a.notify(report,state,d) == 'DELIVERY_UNKNOWN'
    assert len(d.posts) == 1
    d=FakeDiscord(); d.fail_read=True; state=tmp_path/'wrong.sqlite'
    assert a.notify(report,state,d) == 'DELIVERY_UNKNOWN'
    d.fail_read=False; d.messages['1']['channel_id']='wrong'
    assert a.notify(report,state,d) == 'DELIVERY_UNKNOWN'
    assert len(d.posts) == 1


def test_default_read_only_never_calls_notification(tmp_path, monkeypatch, capsys):
    a=auditor(); fixture=tmp_path/'fixture.json'; fixture.write_text(json.dumps([issue(project=None)]))
    monkeypatch.setattr(a,'notify',lambda *args: pytest.fail('implicit send'))
    assert a.main(['--orchestrator-id',OP,'--evidence',str(tmp_path/'report.json'),'--fixture',str(fixture),'--now',NOW]) == 1
    assert 'MISSING_PROJECT' in capsys.readouterr().out


def test_existing_sqlite_missing_delivery_table_cannot_be_reinitialized(tmp_path):
    import sqlite3
    a=auditor(); d=FakeDiscord(); state=tmp_path/'delivery.sqlite'; report=a.audit([issue(project=None)],OP,NOW)
    assert a.notify(report,state,d) == 'CONFIRMED'
    with sqlite3.connect(state) as db: db.execute('DROP TABLE delivery')
    assert a.notify(report,state,d) == 'DELIVERY_UNKNOWN'
    assert len(d.posts) == 1


def test_known_findings_preserved_alongside_unknown_schema():
    a=auditor(); good=issue(project=None); bad=issue(id='i2',identifier='GLP-2',url='https://linear.app/tilton-group/issue/GLP-2',history=None)
    result=a.audit([good,bad],OP,NOW)
    assert result['complete'] is False and result['unresolved_count'] is None
    assert any(f['reason']=='MISSING_PROJECT' for f in result['findings'])
    assert any(f['reason']=='SCHEMA_UNKNOWN' and f['identifier']=='GLP-2' for f in result['findings'])


def test_clean_and_missing_project_have_minimal_findings():
    a = auditor()
    assert a.audit([issue()], OP, NOW)['findings'] == []
    result = a.audit([issue(project=None)], OP, NOW)
    assert result['status'] == 'FINDINGS'
    assert result['unresolved_count'] == 1
    assert result['findings'] == [{'id':'i1','identifier':'GLP-1','reason':'MISSING_PROJECT',
                                  'url':'https://linear.app/tilton-group/issue/GLP-1'}]
    assert 'private-title' not in json.dumps(result)
