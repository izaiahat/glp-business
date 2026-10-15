"""Offline producer safety; fixtures are NOT clinical or native acceptance."""
import copy
import json
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'systems/seo-blog-machine/scripts')]
import article_review_recovery as recovery
import opus_review_loop as loop


def test_postpublication_job_requires_single_effect_free_mode(tmp_path):
    job = {'schema': 'glp-opus-seo-review-job-v1',
           'artifact_role': 'postpublication_review_only_job',
           'max_rounds': 1}
    for apply, once in ((True, False), (False, False), (True, True)):
        path = tmp_path / 'job.json'; path.write_text(json.dumps(job))
        with pytest.raises(RuntimeError, match='review_only'):
            loop.run(path, apply, review_only_once=once)
    for field in ('publish', 'repair'):
        changed = {**job, field: {'argv': ['forbidden']}}
        with pytest.raises(RuntimeError, match='review_only_effect_spec_forbidden'):
            loop.validate_review_only_job(changed, apply=False, review_only_once=True)
    assert loop.validate_review_only_job(job, apply=False, review_only_once=True) is None


def test_review_only_intent_is_exclusive_and_never_replayed(tmp_path):
    loop.reserve_review_only_intent(tmp_path, {'job_id': 'fixture'}, 'sealed-hash')
    path = tmp_path / 'native-review-intent.json'
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        loop.reserve_review_only_intent(tmp_path, {'job_id': 'fixture'}, 'sealed-hash')
    assert path.read_bytes() == before


def test_review_only_blocked_is_not_an_autonomous_retry():
    result = loop.review_only_refusal_policy()
    assert result['autonomous_retry_required'] is False
    assert result['queue_may_advance'] is False
    assert result['retry_policy'] == 'consumed_intent_no_model_retry'


def test_review_only_job_data_never_contains_effect_executables(tmp_path, monkeypatch):
    import review_transports
    packet = tmp_path / 'packet'; packet.mkdir()
    for name in [*recovery.CORE, recovery.CANDIDATE]:
        (packet / name).write_text('fixture subject\n')
    original = {'schema': 'glp-opus-seo-review-job-v1', 'job_id': 'original',
                'packet_dir': '/immutable/original', 'history_dir': '/immutable/history',
                'max_rounds': 5, 'review_timeout': 1200,
                'subject_files': list(recovery.CORE),
                'validator': {'argv': ['old']}, 'repair': {'argv': ['forbidden']},
                'publish': {'argv': ['forbidden']}}
    before = copy.deepcopy(original)
    route = review_transports.transport('hermes-openai-codex-sol61-independent')
    wrapper = tmp_path / 'wrapper.py'; wrapper.write_text('# fixture control\n')
    route['wrapper'] = wrapper
    monkeypatch.setattr(loop, 'validate_topic_authority', lambda job: {'fixture': True})
    job = recovery.review_only_job(original, packet, tmp_path/'history', route,
                                   validator={'argv': ['fixture-validator']},
                                   binding={'fixture': 'not acceptance'})
    assert original == before
    assert 'publish' not in job and 'repair' not in job
    assert job['max_rounds'] == 1
    assert job['artifact_role'] == 'postpublication_review_only_job'
    assert job['review_transport'] == route['transport_id']
    assert job['subject_files'] == [*recovery.CORE, recovery.CANDIDATE]
    assert job['review_wrapper_sha256'] == loop.sha_file(Path(route['wrapper']))
    assert job['postpublication_binding'] == {'fixture': 'not acceptance'}
    assert job['validator'] == {'argv': ['fixture-validator']}


def test_review_only_intent_concurrent_reservation_has_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    def reserve(_):
        try:
            loop.reserve_review_only_intent(tmp_path, {'job_id': 'fixture'}, 'hash')
            return 'reserved'
        except FileExistsError:
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, range(2))) == ['refused', 'reserved']


def producer_fixture(tmp_path, monkeypatch, fault=None):
    """Real fixture filesystem/native-read queries/validator process; no model doubles."""
    import sqlite3
    import weekly_strategy_consumer
    root = tmp_path/'business'; root.mkdir()
    reports = root/'reports'; reports.mkdir()
    entry_dir = reports/'glp-clinic-scout/seo-machine/article-buffer/entries'/('a'*24)
    packet = entry_dir/'packet'; packet.mkdir(parents=True)
    origin = reports/'original-packet'; origin.mkdir()
    for name in recovery.CORE:
        (packet/name).write_text(json.dumps({'fixture_subject': name}))
        (origin/name).write_bytes((packet/name).read_bytes())
    selection = reports/'selection.json'; selection.write_text('{"fixture":true}')
    review = reports/'review.json'
    review.write_text(json.dumps({'job_id': 'fixture', 'authority': {
        'selection_binding': {'target_query': 'fixture query'}}}))
    entry = {'entry_id': entry_dir.name, 'topic_id': 'fixture-topic',
             'target_route': '/guides/fixture/', 'selection_receipt': recovery.spec(selection),
             'pass_receipt': recovery.spec(review),
             'packet_files': [dict(path=n, sha256=recovery.sha(packet/n)) for n in recovery.CORE]}
    entry_path = entry_dir/'entry.json'; entry_path.write_text(json.dumps(entry))
    original = {'schema': 'glp-opus-seo-review-job-v1', 'job_id': 'fixture',
                'operation': 'new_article', 'packet_dir': str(origin),
                'topic_id': entry['topic_id'], 'target_route': entry['target_route'],
                'target_query': 'fixture query', 'selection_receipt': recovery.spec(selection),
                'publish': {'argv': ['FORBIDDEN']}, 'repair': {'argv': ['FORBIDDEN']}}
    original_path = reports/'original-job.json'; original_path.write_text(json.dumps(original))
    release = reports/'release'; release.mkdir()
    from article_source_carrier import SOURCE_REL
    source = release/SOURCE_REL; source.mkdir(parents=True)
    row = {'entry_id': entry_dir.name, 'route': entry['target_route']}
    for key, path in (('article', packet/'06-article.json'), ('qa', packet/'07-qa-receipt.json'), ('review', review)):
        dest = source/(key+'.json'); dest.write_bytes(path.read_bytes())
        row[key] = {'path': dest.relative_to(release).as_posix(), 'sha256': recovery.sha(dest)}
    (source/'manifest.json').write_text(json.dumps({'schema': 'glp-accepted-guides-source-v1', 'entries': [row]}))
    candidate = release/'public/guides/fixture/index.html'; candidate.parent.mkdir(parents=True)
    candidate.write_text('<html>fixture candidate only</html>')
    pointer = reports/'glp-clinic-scout/seo-machine/current-content-base.json'
    pointer.write_text(json.dumps({'release_root': str(release)}))
    controls = root/'systems/seo-blog-machine/scripts'; controls.mkdir(parents=True)
    validator = controls/'validate_opus_article_packet.py'
    validator.write_text("import json,sys\nfrom pathlib import Path\np=Path(sys.argv[sys.argv.index('--packet')+1])\n(p/'06-article.json').write_text(json.dumps({'fixture':'compiled'}))\n(p/'07-qa-receipt.json').write_text(json.dumps({'status': '" + ('BLOCKED' if fault == 'qa' else 'PASS') + "'}))\n")
    for name in ('compile_review_packet_article.py', 'selection_receipt.py', 'r2_update_carrier.py'):
        (controls/name).write_text('# fixture pinned control\n')
    import review_transports
    wrapper = controls/'review-wrapper.py'; wrapper.write_text('# fixture not invoked\n')
    monkeypatch.setitem(review_transports.AUTHORIZED_TRANSPORTS['hermes-openai-codex-sol61-independent'],
                        'wrapper', wrapper)
    db = tmp_path/'native.db'
    with sqlite3.connect(db) as c:
        c.execute('create table sessions(id text,model text,started_at real,ended_at real)')
        c.execute('create table messages(id int,session_id text,role text,tool_name text,content text,timestamp real)')
        c.execute('insert into sessions values(?,?,?,?)', ('writer', 'gpt-6.1-sol' if fault == 'author' else 'astra', 1, 3))
        c.execute('insert into messages values(1,?,?,?,?,?)', ('writer','tool','write_file',
            json.dumps({'verified': True, 'files_modified': [str(origin/'04-draft.md')]}),2))
    strategy = {'applied': fault != 'strategy', 'reason': 'fixture-stale' if fault == 'strategy' else None}
    monkeypatch.setattr(weekly_strategy_consumer, 'load_strategy', lambda *a, **kw: ({}, strategy))
    monkeypatch.setattr(loop, 'validate_topic_authority', lambda job: {'fixture': True})
    if fault == 'source':
        (source/'article.json').write_text('fixture drift')
    return root, entry_path, original_path, reports/'prepared', db


@pytest.mark.parametrize('fault,reason', [(None, None), ('author', 'same model'),
    ('strategy', 'current_weekly_strategy'), ('source', 'published_original_article'),
    ('qa', 'current_deterministic_qa_failed')])
def test_producer_runs_only_qa_and_refuses_unsafe_admission(tmp_path, monkeypatch, fault, reason):
    root, entry, original, output, db = producer_fixture(tmp_path, monkeypatch, fault)
    before = {p: p.read_bytes() for p in entry.parent.rglob('*') if p.is_file()}
    invoke = lambda: recovery.prepare_review_only(entry, original, output,
        'hermes-openai-codex-sol61-independent', root=root, session_db=db)
    if reason:
        with pytest.raises(ValueError, match=reason):
            invoke()
        assert not (output/'job.json').exists()
    else:
        job_path = invoke(); job = json.loads(job_path.read_text())
        assert job_path == output/'job.json'
        assert 'publish' not in job and 'repair' not in job
        assert not (output/'history').exists()
        import review_transports
        recovery.validate_review_only_binding(job, review_transports.transport(job['review_transport']),
                                             root=root, session_db=db, for_invocation=True)
        with pytest.raises(ValueError, match='unused_report_directory'):
            invoke()
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize('fault', [None, 'body', 'published-source', 'entry'])
def test_supplement_keeps_historical_source_and_full_deployed_bytes(tmp_path, monkeypatch, fault):
    root, entry, original, output, db = producer_fixture(tmp_path, monkeypatch)
    job_path = recovery.prepare_review_only(entry, original, output,
        'hermes-openai-codex-sol61-independent', root=root, session_db=db)
    job = json.loads(job_path.read_text())
    doc = {'review_only_job': recovery.spec(job_path)}
    release = Path(json.loads((output/'current-pointer.json').read_text())['release_root'])
    fresh = output/'packet'
    if fault == 'body':
        (release/'public/guides/fixture/index.html').write_text('changed fixture body')
    elif fault == 'published-source':
        row = job['postpublication_binding']['deployed_source']
        (release/row['qa']['path']).write_text('changed historical fixture QA')
    elif fault == 'entry':
        from article_source_carrier import SOURCE_REL
        manifest = release/SOURCE_REL/'manifest.json'
        changed = json.loads(manifest.read_text()); changed['entries'][0]['entry_id'] = 'b'*24
        manifest.write_text(json.dumps(changed))
    if fault:
        with pytest.raises(ValueError, match='supplement_'):
            recovery.validate_supplement_deployed_source(entry, doc, release, fresh)
    else:
        recovery.validate_supplement_deployed_source(entry, doc, release, fresh)
        # Preparation is not custody: no receipt is manufactured when no native review ran.
        (output/'history').mkdir()
        with pytest.raises(ValueError, match='exact_single_review_required'):
            recovery.successor_documents(entry, output/'history', fresh, root=root,
                                         review_only_job_path=job_path)
        assert not recovery.recovery_path(entry, root).exists()
