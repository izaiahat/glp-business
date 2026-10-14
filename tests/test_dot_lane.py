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


def attachment_ticket(root):
    """Offline authored custody, not a live Cloud/Discord execution receipt."""
    import dot_dispatch as d
    import native_reply_consumer as native
    ticket = 'finch-glp-359-offline'
    directory = root/'reports/ops/dot-tickets'/ticket
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    config = dict(parent='DOT-CLOUD-FINAL', receipt_state_dir=str(directory/'receipts'),
        registrations=[dict(correlation='discord-100', channel=native.FINCH_CHANNEL,
            state_dir=str(root/'native'), issuer='issuer', audience='audience', subject='subject',
            task_owner='DOT-CLOUD-FINAL', action='Inspect retained evidence only',
            result_key=ticket, reply_id='101', content_sha256='a'*64,
            terminal_attachment=dict(id='102', filename='evidence.txt', content_type='text/plain',
                byte_length=23, sha256=hashlib.sha256(b'Original evidence copy\n').hexdigest()))])
    # Use actual payload length, never attachment metadata alone.
    payload = 'Original evidence copy\n'
    config['registrations'][0]['terminal_attachment']['byte_length'] = len(payload.encode())
    native_path = directory/'NATIVE-CONFIG.json'
    native_path.write_text(json.dumps(config)); native_path.chmod(0o600)
    _, entries = native.load_registrations(native_path)
    registration = entries[0]
    custody = dict(config['registrations'][0]['terminal_attachment'], url_sha256='b'*64)
    row = dict(status='staged', outcome='result', correlation='discord-100',
        author_id=native.FINCH_BOT, trust=d.TRUST, operator_approval=False,
        text_truncated=False, text='RESULT '+ticket+' retained evidence', reply_id='101',
        result_key=ticket, content_sha256='a'*64, channel_id=native.FINCH_CHANNEL,
        receipt_id=registration.receipt_id, registration_sha256=registration.registration_sha256,
        parent='DOT-CLOUD-FINAL', task_owner='DOT-CLOUD-FINAL', action=registration.action,
        attachment=dict(custody, text=payload, provenance_kind='assistant-copy'),
        native_attachment_custody=custody)
    state = dict(ticket=ticket, dot='finch', issue='GLP-359', task_owner=row['task_owner'],
        parent=row['parent'], state='sent', request_id='100', correlation='discord-100', sent_at=10)
    d.publish_reply(directory, state, {'results':[row]})
    return directory, state, row, registration


def test_terminal_accepts_shared_attachment_renderer_without_weakening_custody(tmp_path):
    import dot_lane as lane
    directory, state, row, registration = attachment_ticket(tmp_path)
    assert lane.terminal(directory, state) == row
    import copy
    import dot_dispatch as d
    completion = (directory/'COMPLETION.json').read_bytes()
    reply = (directory/'REPLY.md').read_bytes()
    for case in ('text', 'attachment-text', 'custody', 'wire-hash', 'full-hash', 'reply-bytes', 'author', 'provenance'):
        bad = copy.deepcopy(row)
        altered_state = dict(state)
        if case == 'text': bad['text'] += ' altered'
        if case == 'attachment-text': bad['attachment']['text'] += ' altered'
        if case == 'custody': bad['native_attachment_custody']['sha256'] = '0'*64
        if case == 'wire-hash': bad['content_sha256'] = '0'*64
        if case == 'author': bad['author_id'] = '999'
        if case == 'provenance': bad['attachment']['provenance_kind'] = 'direct-tool-export'
        # Reseal the outer document to prove inner attachment custody still refuses.
        d.atomic_json(directory/'COMPLETION.json', {'results':[bad]})
        rendered = d.reply_document(altered_state, bad).encode()
        (directory/'REPLY.md').write_bytes(rendered)
        altered_state['reply_sha256'] = hashlib.sha256(rendered).hexdigest()
        if case == 'full-hash': altered_state['reply_sha256'] = '0'*64
        if case == 'reply-bytes': (directory/'REPLY.md').write_bytes(rendered.replace(b'\n', b'\r\n'))
        # A self-consistent text alteration cannot rewrite the original expected full SHA.
        if case == 'text': altered_state['reply_sha256'] = state['reply_sha256']
        with pytest.raises(ValueError): lane.terminal(directory, altered_state)
    (directory/'COMPLETION.json').write_bytes(completion)
    (directory/'REPLY.md').write_bytes(reply)
    assert lane.terminal(directory, state) == row


def test_exact_ticket_owner_handoff_disposes_staged_receipt_without_impersonation(tmp_path, capsys):
    import native_reply_consumer as native
    import dot_lane as lane
    directory, state, row, registration = attachment_ticket(tmp_path)
    receipts_dir = directory/'receipts'; receipts_dir.mkdir(mode=0o700)
    plan = dict(schema='DOT_RECEIPT_OWNER_HANDOFF_V1', ticket=str(directory.resolve()),
        ticket_sha256=hashlib.sha256((directory/'TICKET.json').read_bytes()).hexdigest(),
        native_config_sha256=hashlib.sha256((directory/'NATIVE-CONFIG.json').read_bytes()).hexdigest(),
        completion_sha256=hashlib.sha256((directory/'COMPLETION.json').read_bytes()).hexdigest(),
        reply_sha256=state['reply_sha256'], receipt_preimage_sha256=hashlib.sha256(native.canonical(row).encode()).hexdigest(),
        from_parent='DOT-CLOUD-FINAL', from_task_owner='DOT-CLOUD-FINAL',
        receiving_owner='DOT-OWNER', reason='Existing owner receives retained historical evidence only')
    original = {p:p.read_bytes() for p in directory.iterdir() if p.is_file()}
    with native.Receipts(receipts_dir) as receipts:
        receipts.put(row)
        with pytest.raises(ValueError, match='owner_or_disposition_refused'):
            receipts.record(registration.receipt_id, 'DOT-OWNER', 'received', 'Read original output')
        assert lane.ingest(tmp_path, effects=lambda *_:pytest.fail('foreign-owner send')) == []
        before = (receipts_dir/'reply-consumers.sqlite3').read_bytes()
        for key, value in [(key, '0'*64) for key in plan if key.endswith('_sha256')] + [
                ('from_parent','FOREIGN'), ('from_task_owner','FOREIGN'),
                ('receiving_owner','FOREIGN'), ('ticket',str(directory.parent/'foreign')),
                ('unexpected',True)]:
            bad_plan = dict(plan, **{key:value})
            with pytest.raises((ValueError, FileNotFoundError)):
                receipts.record(registration.receipt_id, 'DOT-OWNER', 'received', 'No false ACK', handoff_plan=bad_plan)
            assert receipts.get(registration.receipt_id) == row
            assert (receipts_dir/'reply-consumers.sqlite3').read_bytes() == before
        received = receipts.record(registration.receipt_id, 'DOT-OWNER', 'received',
            'Read original output', handoff_plan=plan)
        assert received['status'] == 'received'
        assert (received['parent'], received['task_owner']) == ('DOT-CLOUD-FINAL', 'DOT-CLOUD-FINAL')
        assert received['recorded_by'] == 'DOT-OWNER'
        assert receipts.record(registration.receipt_id, 'DOT-OWNER', 'received', 'Read original output') == received
        rejected = receipts.record(registration.receipt_id, 'DOT-OWNER', 'rejected',
            'Historical evidence; missing nonce and attributable denial')
        assert rejected['recorded_by'] == 'DOT-OWNER'
        before = (receipts_dir/'reply-consumers.sqlite3').read_bytes()
        assert receipts.record(registration.receipt_id, 'DOT-OWNER', 'rejected',
            'Historical evidence; missing nonce and attributable denial') == rejected
        assert (receipts_dir/'reply-consumers.sqlite3').read_bytes() == before
        with pytest.raises(ValueError, match='owner_or_disposition_refused'):
            receipts.record(registration.receipt_id, 'DOT-CLOUD-FINAL', 'rejected', 'Impersonation')
    assert all(p.read_bytes() == data for p, data in original.items())
    plan_path = tmp_path/'HANDOFF.json'
    plan_path.write_text(json.dumps(plan)); plan_path.chmod(0o600)
    argv = ['--config', str(directory/'NATIVE-CONFIG.json'), '--receipt-id', registration.receipt_id,
        '--owner', 'DOT-OWNER', '--record', 'rejected', '--evidence',
        'Historical evidence; missing nonce and attributable denial', '--owner-handoff-plan', str(plan_path)]
    before = (receipts_dir/'reply-consumers.sqlite3').read_bytes()
    assert native.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['recorded_by'] == 'DOT-OWNER'
    assert (receipts_dir/'reply-consumers.sqlite3').read_bytes() == before
    assert native.main(['--config', str(directory/'NATIVE-CONFIG.json'), '--owner-handoff-plan', str(plan_path)]) == 2
    capsys.readouterr()


def test_refill_plan_order_once_per_scope_and_idle_only(tmp_path):
    import dot_lane as lane
    policy = dict(scopes=[
        dict(id='later', issue='GLP-328', dots=['rook','finch'], effect='public_research',
             coordinator_authorized=True, brief='Public sources only; no sends.', files=[]),
        dict(id='first', issue='GLP-253', dots=['rook','finch'], effect='public_research',
             coordinator_authorized=True, brief='Public sources only; no sends.', files=[])])
    issues = [dict(identifier='GLP-328', priority=1, state={'type':'started'}),
              dict(identifier='GLP-253', priority=0, state={'type':'unstarted'})]
    plan = {'issues':[{'issue':'GLP-253','order':6}, {'issue':'GLP-328','order':17}]}
    assert lane.select('rook', issues, plan, policy, {}, [])[0]['id'] == 'first'
    assert lane.select('rook', issues, plan, policy, {'first':{}}, [])[0]['id'] == 'later'
    assert lane.select('rook', issues, plan, policy, {}, [dict(dot='rook', state='sent', issue='GLP-93')]) is None
    assert lane.select('finch', issues, plan, policy, {}, [dict(dot='rook', state='sent', issue='GLP-253')])[0]['id'] == 'later'
    issues[1]['state']['type'] = 'backlog'
    assert lane.select('rook',issues,plan,policy,{},[])[0]['id'] == 'later'
    policy['scopes'][0]['effect'] = 'send'
    assert lane.select('finch', issues, plan, policy, {'first':{}}, []) is None
    policy['scopes'][0]['effect'] = 'independent_pr'
    policy['scopes'][0]['files'] = ['scripts/shared.py']
    policy['scopes'][0]['coordinator_authorized'] = False
    assert lane.select('finch', issues, plan, policy, {'first':{}}, []) is None


def test_cron_loop_handoff_then_refill_keeps_success_stdout_silent():
    root = Path(__file__).resolve().parents[1]
    source = (root/'ops/hermes-cron-scripts/fleet_ingest_cron.sh').read_text()
    assert source.index('dot_lane.py ingest') < source.index('dot_lane.py refill')
    assert source.count('>>reports/ops/dot-refill/FLEET-CRON.log') == 2
    assert 'mkdir -p -m 700 reports/ops/dot-refill' in source
    assert 'umask 077' in source


def test_new_acceptance_versions_replenish_without_repeating_same_scope():
    import dot_lane as lane
    policy = dict(scopes=[],continuous_public_research_issues=['GLP-93'])
    issue = dict(identifier='GLP-93',title='Provider source evidence',description='One unresolved public terms gap',priority=1,state={'type':'started'})
    plan = {'issues':[{'issue':'GLP-93','order':1}]}
    first = lane.select('rook',[issue],plan,policy,{},[])[0]
    assert lane.select('rook',[issue],plan,policy,{first['id']:{}},[]) is None
    issue['description'] += '; newly admitted official terms question'
    assert lane.select('rook',[issue],plan,policy,{first['id']:{}},[])[0]['id'] != first['id']


def test_supply_ignores_execution_owner_metadata_changes():
    import dot_lane as lane
    policy = dict(scopes=[],continuous_public_research_issues=['GLP-93'])
    description = '<!-- execution-owner:start -->\nOld owner and handoff\n<!-- execution-owner:end -->\nAcceptance: one official-source terms gap.'
    issue = dict(identifier='GLP-93',description=description,state={'type':'started'})
    plan = {'issues':[{'issue':'GLP-93','order':1}]}
    first = lane.select('rook',[issue],plan,policy,{},[])[0]
    assert 'Old owner' not in first['brief']
    issue['description'] = description.replace('Old owner and handoff','New coordinator metadata')
    assert lane.select('rook',[issue],plan,policy,{first['id']:{}},[]) is None


def test_legacy_raw_description_claim_does_not_repeat_after_supply_cleanup():
    import dot_lane as lane
    issue = dict(identifier='GLP-93',description='<!-- execution-owner:start -->old owner<!-- execution-owner:end -->Public acceptance',state={'type':'started'})
    old = 'public-gap-glp-93-'+hashlib.sha256(issue['description'].encode()).hexdigest()[:16]
    assert lane.select('rook',[issue],{'issues':[{'issue':'GLP-93','order':1}]},dict(scopes=[],continuous_public_research_issues=['GLP-93']),{old:{}},[]) is None


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


def test_rook_cloud_terminal_owned_by_r1_is_still_historical_evidence(tmp_path):
    import dot_lane as lane
    import dot_dispatch as d
    message = dict(id='123456789',channel_id=d.DOTS['rook']['channel'],author={'id':d.DOTS['rook']['user']},content='RESULT rook-glp-359-test Codex Cloud proof for R1, not approval.')
    api = lambda method,route: [message] if '?limit=' in route else message
    events = []
    def effects(kind,issue,body,marker):
        events.append(kind)
        return {'id':'receipt'}
    lane.cloud_updates(tmp_path,api=api,effects=effects)
    assert events == ['linear','discord']


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


def test_one_refill_comment_hold_does_not_stall_the_other_dot(tmp_path,monkeypatch):
    import dot_lane as lane
    test_full_refill_cloud_handoff_precedes_rook_and_busy_is_silent(tmp_path,monkeypatch)
    ledger = tmp_path/'reports/ops/dot-refill/CLAIMS.json'
    claims = json.loads(ledger.read_text())
    claims['two']['status'] = 'dispatched'
    ledger.write_text(json.dumps(claims))
    def effects(kind,*args):
        assert kind == 'linear_reconcile'
        raise ValueError('uncertain_linear_effect_absent_coordinator_reconcile_required')
    issues = [dict(identifier='GLP-253',state={'type':'started'}),dict(identifier='GLP-328',state={'type':'started'})]
    out = lane.refill(tmp_path,issues=issues,effects=effects,cloud_reader=lambda root:[])
    assert [r['status'] for r in out] == ['busy','awaiting_coordinator_refill_comment']


def test_backlogged_reply_is_handed_to_coordinator_without_role_escalation(tmp_path):
    import dot_lane as lane
    import dot_dispatch as d
    test_ingestion_writes_coordinator_receipt_and_is_idempotent(tmp_path)
    directory = tmp_path/'reports/ops/dot-tickets/rook-glp-354-test'
    completion = json.loads((directory/'COMPLETION.json').read_text())
    completion['results'][0]['author_id'] = d.DOTS['rook']['user']
    (directory/'COMPLETION.json').write_text(json.dumps(completion))
    target = tmp_path/'reports/ops/fleet-ingest/rook-glp-354-test.json'
    receipt = json.loads(target.read_text())
    receipt['effects'] = {}
    target.write_text(json.dumps(receipt))
    class LinearError(Exception):
        pass
    events = []
    def effects(kind,issue,body,marker):
        events.append(kind)
        if kind == 'linear':
            raise LinearError('Fleet target must be an admitted active GLP issue')
        if kind == 'linear_reconcile':
            raise ValueError('uncertain_linear_effect_absent_coordinator_reconcile_required')
        return {'id':'notification'}
    lane.ingest(tmp_path,since=0,effects=effects)
    assert json.loads(target.read_text())['status'] == 'awaiting_coordinator_comment'
    assert events == ['linear','discord']
    lane.ingest(tmp_path,since=0,effects=effects)
    assert events == ['linear','discord','linear_reconcile']


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
