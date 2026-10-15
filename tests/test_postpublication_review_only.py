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


@pytest.fixture(autouse=True)
def isolated_controller_controls(tmp_path, monkeypatch):
    # These tests exercise producer admission, never a clinical review. Do not
    # accidentally depend on the operator host's absolute rubric/schema files.
    rubric = tmp_path/'fixture-rubric.md'; rubric.write_text('# fixture only; no review\n')
    schema = tmp_path/'fixture-schema.json'; schema.write_text('{}\n')
    monkeypatch.setattr(loop, 'RUBRIC', rubric)
    monkeypatch.setattr(loop, 'REVIEW_SCHEMA', schema)


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


def test_prepared_role_cli_reaches_offline_boundary_once(tmp_path, monkeypatch, capsys):
    """Actual producer/controller __main__; synthetic inputs, no native invocation.

    Only fixture paths/DB/authority and the external transport boundary differ.
    The role-specific import, native author queries, input binding, pinned QA,
    mode checks and durable intent run unchanged. A refused transport is never
    retried; this fixture cannot emit a clinical review or recovery receipt.
    """
    import runpy
    import subprocess
    root, entry, original, output, db = producer_fixture(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in entry.parent.rglob('*') if p.is_file()}
    preparer = ROOT/'scripts/article_review_recovery.py'
    controller = ROOT/'systems/seo-blog-machine/scripts/opus_review_loop.py'
    actual_prepare = recovery.prepare_review_only
    actual_binding = recovery.validate_review_only_binding
    admitted = []

    def prepare(*args, **kwargs):
        return actual_prepare(*args, root=root, session_db=db, **kwargs)

    def binding(job, route, **kwargs):
        actual_binding(job, route, root=root, session_db=db, **kwargs)
        assert kwargs == {'for_invocation': True}
        admitted.append(job['job_id'])

    monkeypatch.setattr(recovery, 'validate_review_only_binding', binding)

    def trace(frame, event, arg):
        if event == 'call' and frame.f_code.co_name == 'main':
            if frame.f_code.co_filename == str(preparer):
                frame.f_globals.update(ROOT=root, prepare_review_only=prepare)
            elif frame.f_code.co_filename == str(controller):
                frame.f_globals.update(RUBRIC=loop.RUBRIC, REVIEW_SCHEMA=loop.REVIEW_SCHEMA,
                                       validate_topic_authority=loop.validate_topic_authority)
        return trace

    def cli(script, args, exits=True):
        monkeypatch.setattr(sys, 'argv', [str(script), *map(str, args)])
        prior = sys.gettrace()
        sys.settrace(trace)
        try:
            if exits:
                with pytest.raises(SystemExit) as exc:
                    runpy.run_path(str(script), run_name='__main__')
                return exc.value.code
            runpy.run_path(str(script), run_name='__main__')
        finally:
            sys.settrace(prior)

    cli(preparer, ['--prepare-review-only', '--entry', entry,
                  '--original-job', original, '--output', output, '--review-transport',
                  'hermes-openai-codex-sol61-independent'], exits=False)
    prepared = json.loads(capsys.readouterr().out)
    assert prepared['status'] == 'PREPARED_REVIEW_ONLY_NOT_INVOKED'
    assert prepared['native_calls'] == 0 and prepared['publication_authorized'] is False
    job_path = output/'job.json'; job = json.loads(job_path.read_text())
    assert job['artifact_role'] == 'postpublication_review_only_job'
    assert job['max_rounds'] == 1 and not {'repair', 'publish'} & job.keys()
    history = Path(job['history_dir'])
    calls = []
    actual_run = subprocess.run

    def offline_transport(argv, **kwargs):
        if Path(argv[1]).name == 'validate_opus_article_packet.py':
            return actual_run(argv, **kwargs)  # Real bounded fixture QA subprocess.
        assert admitted == [job['job_id']]
        assert argv[1] == str(__import__('review_transports').transport(job['review_transport'])['wrapper'])
        assert '--disable-tools' in argv and kwargs['cwd'] == Path(job['packet_dir'])
        intent = json.loads((history/'native-review-intent.json').read_text())
        assert intent['job_id'] == job['job_id']
        assert intent['subject_manifest_sha256'] == loop.sha_file(history/'round-01-subject-manifest.json')
        assert intent['retry_allowed'] is False and intent['publication_authorized'] is False
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 73, '', 'OFFLINE_TRANSPORT_BLOCKED_NO_NATIVE_CALL')

    monkeypatch.setattr(subprocess, 'run', offline_transport)
    # Mode policy must refuse before QA, binding or the offline boundary.
    assert cli(controller, ['--job', job_path, '--review-only-once', '--apply']) == 2
    assert 'review_only_cannot_apply' in capsys.readouterr().err
    assert not history.exists() and not calls and not admitted
    assert cli(controller, ['--job', job_path, '--review-only-once']) == 2
    assert 'opus_review_failed:73' in capsys.readouterr().err
    assert len(calls) == 1 and admitted == [job['job_id']]
    intent_bytes = (history/'native-review-intent.json').read_bytes()
    assert cli(controller, ['--job', job_path, '--review-only-once']) == 2
    assert 'receipt_exists:' in capsys.readouterr().err
    assert len(calls) == 1 and (history/'native-review-intent.json').read_bytes() == intent_bytes
    assert loop.review_only_refusal_policy() == {
        'autonomous_retry_required': False, 'queue_may_advance': False,
        'retry_policy': 'consumed_intent_no_model_retry'}
    assert not (history/'round-01-sol-raw.json').exists()
    assert not (history/'loop-closeout.json').exists()
    assert not list(history.glob('*repair*')) and not list(history.glob('*publication*'))
    assert not recovery.recovery_path(entry, root).exists()
    assert all(p.read_bytes() == data for p, data in before.items())
    assert Path(recovery.__file__).resolve() == preparer
    assert Path(loop.__file__).resolve() == controller
