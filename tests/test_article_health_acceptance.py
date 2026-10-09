"""Offline packet/reviewer custody; never publishes or closes a live issue."""
import hashlib
import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import article_health_acceptance as aha
import linear_close as lc


def test_health_completion_cannot_relabel_money_or_send(tmp_path, monkeypatch):
    worker = tmp_path / 'RESULT.json'
    worker.write_text(json.dumps({'status': 'DONE_READY_TO_CLOSE', 'issue': 'GLP-320'}))
    for title in ['Send health guide email to subscribers', 'Charge card for health guide']:
        with pytest.raises(lc.Refused, match='protected effect'):
            lc.build('GLP-320', worker, {'title': title, 'description': '## Acceptance\nPublish new health claims.'})


def test_natural_due_slot_requires_same_local_day_and_scheduled():
    run = {'action': 'article_carrier_consume', 'status': 'PASS', 'rc': 0,
           'invocation': 'scheduled', 'due_slot': '2026-10-09T09:45:America/Los_Angeles',
           'started_utc': '2026-10-09T16:45:01Z',
           'effects': {'machine_status': 'PASS_PUBLISHED', 'output_count': 1, 'live_status': 200}}
    aha.validate_carrier(run, natural=True)
    for patch in [{'invocation': 'manual_registered'}, {'started_utc': '2026-10-10T16:45:00Z'},
                  {'effects': {'machine_status': 'PASS_NOTHING_DUE', 'output_count': 0}},
                  {'started_utc': '2026-10-09T16:40:00Z'}]:
        with pytest.raises(ValueError):
            aha.validate_carrier({**run, **patch}, natural=True)


def test_author_must_have_authenticated_distinct_model_and_completed_before_review(tmp_path):
    import sqlite3
    db = tmp_path / 'state.db'
    conn = sqlite3.connect(db)
    conn.execute('create table sessions(id text, model text, started_at real, ended_at real)')
    conn.execute('insert into sessions values(?,?,?,?)', ('writer', 'gpt-6.1-sol', 10, 20))
    conn.commit()
    raw = {'model': 'gpt-6-astra-900k', 'runtime': {'native_session_id': 'reviewer'}}
    assert aha.author_session(db, 'writer', raw, 30)['model'] == 'gpt-6.1-sol'
    for session, review, when in [('absent', raw, 30), ('writer', {**raw, 'model': 'gpt-6.1-sol'}, 30),
                                  ('writer', {**raw, 'runtime': {'native_session_id': 'writer'}}, 30),
                                  ('writer', raw, 15)]:
        with pytest.raises(ValueError):
            aha.author_session(db, session, review, when)


def test_packet_writer_custody(tmp_path):
    import sqlite3
    db = tmp_path / 'state.db'
    draft = tmp_path / 'packet/04-draft.md'
    with sqlite3.connect(db) as c:
        c.execute('create table sessions(id text, model text)')
        c.execute('create table messages(id int,session_id text,role text,tool_name text,content text,timestamp real)')
        c.execute('insert into sessions values("writer","sol")')
        c.execute('insert into messages values(1,"writer","tool","write_file",?,10)',
                  (json.dumps({'verified': True, 'files_modified': [str(draft)]}),))
    raw = {'model': 'astra', 'runtime': {'native_session_id': 'reviewer'}}
    assert aha.packet_authors(db, [draft], raw, 20)[0]['model'] == 'sol'
    with pytest.raises(ValueError, match='after review seal'):
        aha.packet_authors(db, [draft], raw, 5)
    raw['model'] = 'sol'
    with pytest.raises(ValueError, match='same model'):
        aha.packet_authors(db, [draft], raw, 20)


def test_publication_receipt_retains_source_and_deployment():
    import article_source_carrier as carrier
    row = {'entry_id': 'a' * 24, 'article': {'sha256': 'b' * 64}}
    result = {'deployment_id': 'dep', 'status': 'PASS_PRODUCTION_TERMINAL'}
    proof = carrier.publication_receipt({}, result, {}, row)
    assert proof['source_entry'] == row and proof['deployment_id'] == 'dep'
    with pytest.raises(RuntimeError, match='deployment_identity_missing'):
        carrier.publication_receipt({}, {}, {}, row)


def test_health_build_reuses_receipt_without_model_or_human_call(tmp_path, monkeypatch):
    worker = tmp_path / 'RESULT.json'
    worker.write_text(json.dumps({'status': 'DONE_READY_TO_CLOSE', 'issue': 'GLP-320', 'receipt': 'carrier.json'}))
    seen = []
    def accept(path, **kwargs):
        seen.append((path, kwargs['natural']))
        return {'schema': aha.SCHEMA, 'status': 'PASS'}
    monkeypatch.setattr(aha, 'acceptance', accept)
    monkeypatch.setattr(lc, 'resolve_pr', lambda *a: {'url': 'https://github.com/izaiahat/glp-site-candidate/pull/1', 'merge_sha': 'a'*40, 'checks': [{'name': 'ci'}]})
    monkeypatch.setattr(lc, 'collect_evidence', lambda *a: [{'observed': ['guide-title']}])
    data = lc.build('GLP-320', worker, {'title': 'Publish articles on a natural slot', 'description': '## Acceptance\nPublish articles.'})
    assert seen == [('carrier.json', True)]
    assert data['schema'] == 'linear-reviewed-health-completion-v1'
    assert data['protected_effects'] == ['health-claim-publish']
    assert 'Independent article acceptance SHA256:' in lc.acceptance_body({**data, 'evidence': []})


def test_unbound_health_proof_cannot_be_an_ordinary_completion(tmp_path, monkeypatch):
    data = {'schema': 'linear-reviewed-health-completion-v1', 'issue': 'GLP-320', 'status': 'COMPLETE',
            'scope': 'reviewed-health-runtime', 'protected_effects': ['health-claim-publish'],
            'description_sha256': hashlib.sha256(b'## Acceptance\nPublish a health guide.').hexdigest(),
            'independent_health_review': {'status': 'PASS'}}
    result = tmp_path / 'RESULT.json'
    result.write_text(json.dumps(data))
    issue = {'identifier': 'GLP-320', 'title': 'Publish a health guide',
             'description': '## Acceptance\nPublish a health guide.', 'team': {'id': lc.lo.TEAM, 'key': 'GLP'}}
    with pytest.raises(SystemExit, match='independent|health'):
        lc.lo._lint_ordinary_result(result, issue)
