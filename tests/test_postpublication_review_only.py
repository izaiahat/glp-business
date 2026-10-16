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
    import glp_release_main
    monkeypatch.setattr(glp_release_main, 'RUNS', tmp_path/'private-release-runs')


def competing_cli_refused(job_path, packet, history, tmp_path, actual_run):
    """Separate-process actual controller; real private run flock, no effects."""
    import glp_release_main
    controller = ROOT/'systems/seo-blog-machine/scripts/opus_review_loop.py'
    marker = tmp_path/'contender-reached-effect'
    before = {p: p.read_bytes() for d in (packet, history) for p in d.rglob('*') if p.is_file()}
    code = '''import sys,pathlib,runpy,subprocess
sys.dont_write_bytecode=True
sys.path[:0]=%r
import glp_release_main,review_transports
glp_release_main.RUNS=pathlib.Path(%r)
review_transports.AUTHORIZED_TRANSPORTS['hermes-openai-codex-sol61-independent']['wrapper']=pathlib.Path(%r)
def trace(frame,event,arg):
 if event=='call' and frame.f_code.co_name=='main':
  frame.f_globals.update(RUBRIC=pathlib.Path(%r),REVIEW_SCHEMA=pathlib.Path(%r),validate_topic_authority=lambda job:{'fixture':True})
 return trace
sys.settrace(trace)
def denied(*a,**kw):
 pathlib.Path(%r).write_text('QA/transport was reached')
 raise RuntimeError('CONTENDER_EFFECT_REACHED')
subprocess.run=denied
sys.argv=[%r,'--job',%r,'--review-only-once']
runpy.run_path(%r,run_name='__main__')
''' % ([str(ROOT/'scripts'), str(controller.parent)], str(glp_release_main.RUNS),
       str(__import__('review_transports').transport('hermes-openai-codex-sol61-independent')['wrapper']),
       str(loop.RUBRIC), str(loop.REVIEW_SCHEMA), str(marker), str(controller), str(job_path), str(controller))
    proc = actual_run([sys.executable, '-c', code], capture_output=True, text=True, timeout=15,
                      env={**__import__('os').environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert proc.returncode == 2 and 'Resource temporarily unavailable' in proc.stderr, proc.stderr
    assert not marker.exists()
    assert before == {p: p.read_bytes() for d in (packet, history) for p in d.rglob('*') if p.is_file()}


def assert_private_run_lock_released():
    import fcntl
    import glp_release_main
    with (glp_release_main.RUNS/'run.lock').open('a') as contender:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender, fcntl.LOCK_UN)


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


def test_registered_standard_route_not_fast_only(tmp_path, monkeypatch):
    import review_transports
    root, entry, original, output, db = producer_fixture(tmp_path, monkeypatch)
    route = review_transports.transport('hermes-openai-codex-astra-independent')
    # Registry calls standard speed 'normal'; do not mutate it to Fast.
    assert route['speed'] == 'normal' and route['service_tier'] == 'default'
    monkeypatch.setitem(route, 'wrapper', root/'systems/seo-blog-machine/scripts/review-wrapper.py')
    path = recovery.prepare_review_only(entry, original, output, route['transport_id'], root=root, session_db=db)
    recovery.validate_review_only_binding(recovery.read(path), route, root=root, session_db=db)
    with pytest.raises(ValueError, match='registered_native_route'):
        recovery.validate_review_only_binding(recovery.read(path), {**route, 'speed': 'fast'}, root=root, session_db=db)


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


def successor_fixture(tmp_path, monkeypatch):
    import sqlite3
    import compile_review_packet_article as compiler
    root, entry, original, output, db = producer_fixture(tmp_path, monkeypatch)
    successor = root/'reports/corrected'; successor.mkdir()
    origin = Path(recovery.read(original)['packet_dir'])
    for name in recovery.CORE[:6]:
        (successor/name).write_bytes((origin/name).read_bytes())
    (successor/'04-draft.md').write_text('corrected successor draft, not the published draft')
    candidate = root/'reports/corrected-candidate.html'
    candidate.write_text('<html>corrected successor, not deployed</html>')
    blocked = root/'reports/original-BLOCKED.json'
    blocked.write_text(json.dumps({'status': 'BLOCKED_OPUS_SEO_DRAFT'}))
    with __import__('sqlite3').connect(db) as c:
        c.execute('insert into sessions values(?,?,?,?)', ('successor-writer', 'astra', 4, 6))
        c.execute('insert into messages values(2,?,?,?,?,?)', ('successor-writer','tool','patch',
            json.dumps({'verified': True, 'files_modified': [str(successor/'04-draft.md')]}),5))
    # Explicit deterministic fixture compiler/QA, not clinical evidence.
    monkeypatch.setattr(compiler, 'compile_packet', lambda p: {
        'draft': (p/'04-draft.md').read_text(),
        'render_contract': {'schema': 'glp-accepted-guide-render-v1'}})
    validator = root/'systems/seo-blog-machine/scripts/validate_opus_article_packet.py'
    validator.write_text('''import json,sys,hashlib
from pathlib import Path
p=Path(sys.argv[sys.argv.index('--packet')+1])
a={'draft':(p/'04-draft.md').read_text(),'render_contract':{'schema':'glp-accepted-guide-render-v1'}}
(p/'06-article.json').write_text(json.dumps(a,indent=2,sort_keys=True)+'\\n')
arts={n:{'sha256':hashlib.sha256((p/n).read_bytes()).hexdigest()} for n in %r}
(p/'07-qa-receipt.json').write_text(json.dumps({'status':'PASS','failures':[], 'checks':{'fixture':True},'checked_at_utc':'2026-01-01T00:00:00Z','artifacts':arts}))
''' % list(recovery.CORE[:-1]))
    options = dict(successor_packet=successor, successor_candidate=candidate,
                   candidate_sha256=recovery.sha(candidate), draft_sha256=recovery.sha(successor/'04-draft.md'),
                   blocked_review=blocked)
    return root, entry, original, output, db, options


def test_preparation_binds_corrected_successor_not_deployed_subject(tmp_path, monkeypatch):
    import review_transports
    root, entry, original, output, db, options = successor_fixture(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in (root/'reports').rglob('*') if p.is_file()}
    route = review_transports.transport('hermes-openai-codex-sol61-independent')
    path = recovery.prepare_review_only(entry, original, output, route['transport_id'], root=root, session_db=db, **options)
    job = recovery.read(path); packet = Path(job['packet_dir']); binding = job['postpublication_binding']
    assert (packet/recovery.CANDIDATE).read_bytes() == options['successor_candidate'].read_bytes()
    assert (packet/'04-draft.md').read_bytes() == (options['successor_packet']/'04-draft.md').read_bytes()
    assert recovery.sha(packet/recovery.CANDIDATE) != binding['deployed_candidate']['sha256']
    assert binding['review_subject'] == 'corrected_successor'
    assert binding['successor']['blocked_review'] == recovery.spec(options['blocked_review'])
    assert set(binding['successor']['original_aliases']) == set(recovery.CORE[:6]) - {'04-draft.md'}
    assert 'corrected successor' in job['prompt_intro'] and 'byte-exact deployed page' not in job['prompt_intro']
    recovery.validate_review_only_binding(job, route, root=root, session_db=db, for_invocation=True)
    assert not (output/'history').exists()
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize('fault,reason', [
    ('candidate_sha', 'exact_bytes'), ('draft_sha', 'exact_bytes'),
    ('partial', 'successor_inputs'), ('original_author', 'same model'),
    ('successor_model', 'same model'), ('successor_session', 'same model or session'),
    ('incomplete_author', 'not completed'), ('unauthenticated_author', 'writer edits missing'),
    ('identity', 'identity_subject_changed')])
def test_successor_fail_closed_before_preparation(tmp_path, monkeypatch, fault, reason):
    import sqlite3
    import review_transports
    root, entry, original, output, db, options = successor_fixture(tmp_path, monkeypatch)
    route = review_transports.transport('hermes-openai-codex-sol61-independent')
    if fault == 'candidate_sha': options['candidate_sha256'] = '0'*64
    if fault == 'draft_sha': options['draft_sha256'] = '0'*64
    if fault == 'partial': options.pop('blocked_review')
    if fault == 'identity': (options['successor_packet']/'00-authority.json').write_text('different identity')
    with sqlite3.connect(db) as c:
        if fault == 'original_author':
            c.execute('update sessions set model=? where id=?', (route['model'], 'writer'))
        if fault == 'successor_model':
            c.execute('update sessions set model=? where id=?', (route['model'], 'successor-writer'))
        if fault == 'successor_session':
            c.execute('update sessions set id=? where id=?', ('NOT_YET_INVOKED', 'successor-writer'))
            c.execute('update messages set session_id=? where session_id=?', ('NOT_YET_INVOKED', 'successor-writer'))
        if fault == 'incomplete_author':
            c.execute('update sessions set ended_at=null where id=?', ('successor-writer',))
        if fault == 'unauthenticated_author': c.execute('delete from messages where id=2')
    with pytest.raises(ValueError, match=reason):
        recovery.prepare_review_only(entry, original, output, route['transport_id'], root=root, session_db=db, **options)
    assert not output.exists()


@pytest.mark.parametrize('target', ['draft', 'candidate', 'alias', 'original', 'qa', 'blocked'])
def test_successor_rechecked_at_admission(tmp_path, monkeypatch, target):
    import review_transports
    root, entry, original, output, db, options = successor_fixture(tmp_path, monkeypatch)
    route = review_transports.transport('hermes-openai-codex-sol61-independent')
    path = recovery.prepare_review_only(entry, original, output, route['transport_id'], root=root, session_db=db, **options)
    job = recovery.read(path); packet = Path(job['packet_dir'])
    changed = {'draft': packet/'04-draft.md', 'candidate': packet/recovery.CANDIDATE,
               'alias': options['successor_packet']/'03-editorial-brief.md',
               'original': Path(recovery.read(original)['packet_dir'])/'03-editorial-brief.md',
               'qa': packet/'06-article.json', 'blocked': options['blocked_review']}[target]
    changed.write_text('{}')
    with pytest.raises(ValueError):
        recovery.validate_review_only_binding(job, route, root=root, session_db=db, for_invocation=True)
    assert not (output/'history').exists()


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


@pytest.mark.parametrize('successor,transport_id', [
    (False, 'hermes-openai-codex-sol61-independent'),
    (True, 'hermes-openai-codex-sol61-independent'),
    (True, 'hermes-openai-codex-astra-independent')])
def test_prepared_role_cli_reaches_offline_boundary_once(tmp_path, monkeypatch, capsys, successor, transport_id):
    """Actual producer/controller __main__; synthetic inputs, no native invocation.

    Only fixture paths/DB/authority and the external transport boundary differ.
    The role-specific import, native author queries, input binding, pinned QA,
    mode checks and durable intent run unchanged. A refused transport is never
    retried; this fixture cannot emit a clinical review or recovery receipt.
    """
    import runpy
    import subprocess
    import review_transports
    if successor:
        root, entry, original, output, db, options = successor_fixture(tmp_path, monkeypatch)
    else:
        root, entry, original, output, db = producer_fixture(tmp_path, monkeypatch)
        options = {}
    monkeypatch.setitem(review_transports.transport(transport_id), 'wrapper',
                        root/'systems/seo-blog-machine/scripts/review-wrapper.py')
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

    successor_args = [part for k, v in options.items() for part in ('--'+k.replace('_', '-'), v)]
    cli(preparer, ['--prepare-review-only', '--entry', entry,
                  '--original-job', original, '--output', output, '--review-transport',
                  transport_id, *successor_args], exits=False)
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
        competing_cli_refused(job_path, Path(job['packet_dir']), history, tmp_path, actual_run)
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 73, '', 'OFFLINE_TRANSPORT_BLOCKED_NO_NATIVE_CALL')

    monkeypatch.setattr(subprocess, 'run', offline_transport)
    # Mode policy must refuse before QA, binding or the offline boundary.
    assert cli(controller, ['--job', job_path, '--review-only-once', '--apply']) == 2
    assert 'review_only_cannot_apply' in capsys.readouterr().err
    assert not history.exists() and not calls and not admitted
    assert cli(controller, ['--job', job_path, '--review-only-once']) == 2
    assert 'opus_review_failed:73' in capsys.readouterr().err
    assert_private_run_lock_released()
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


@pytest.mark.parametrize('consumed', [False, True])
def test_actual_qa_retry_preserves_every_packet_history_and_intent_byte(tmp_path, monkeypatch, capsys, consumed):
    """Actual pinned QA/compiler and controller CLI; deliberately BLOCKED inputs.

    No QA PASS or clinical result is fabricated. The consumed variant adds only
    explicit private sealed-input/intent history, never a native review receipt.
    """
    import contextlib
    import io
    import runpy
    import subprocess
    monkeypatch.syspath_prepend(str(ROOT/'systems/seo-blog-machine/tests'))
    monkeypatch.syspath_prepend(str(ROOT/'systems/analytics-brain'))
    from test_publication_date_binding import packet as fixture_packet
    import validate_opus_article_packet as qa
    # Earlier producer fixtures intentionally import their own compiler. Load
    # this regression's compiler by exact candidate file, never that cached seam.
    import importlib.util
    spec = importlib.util.spec_from_file_location('actual_retry_compiler',
        ROOT/'systems/seo-blog-machine/scripts/compile_review_packet_article.py')
    compiler = importlib.util.module_from_spec(spec); spec.loader.exec_module(compiler)
    import review_transports
    root, entry, original, output, db = producer_fixture(tmp_path, monkeypatch)
    packet = tmp_path/'real-qa-packet'; packet.mkdir(); fixture_packet(packet)
    (packet/'03-editorial-brief.md').write_text('Explicit offline fixture brief')
    draft = packet/'04-draft.md'
    draft.write_text(draft.read_text().replace('---\n> **Quick',
                     'schemaRequirements: [Article, FAQPage]\n---\n> **Quick'))
    (packet/recovery.CANDIDATE).write_text('<html>private synthetic deployed candidate</html>')
    sitemap = tmp_path/'sitemap.xml'; sitemap.write_text('<urlset/>')
    selected = {'topic_id': 'date-fixture', 'target_query': 'fixture guide',
                'target_route': '/guides/date-binding-fixture/'}
    monkeypatch.setattr(qa, 'load_selection_receipt', lambda path: (
        {'inputs': {'sitemap_readback': recovery.spec(sitemap)}}, selected))
    monkeypatch.setattr(qa, 'validate_authority_selection_binding', lambda *a: None)
    monkeypatch.setattr(qa, 'packet_research_valid', lambda *a: False)
    monkeypatch.setattr(qa, 'current_article_catalog_routes', lambda *a: set())
    controls = ROOT/'systems/seo-blog-machine/scripts'
    argv = [sys.executable, str(controls/'validate_opus_article_packet.py'), '--packet', str(packet),
            '--selection-receipt', str(root/'reports/selection.json')]
    validator = {'argv': argv, 'executable': sys.executable,
                 'executable_sha256': recovery.sha(sys.executable),
                 'control_files': {str(controls/n): recovery.sha(controls/n) for n in (
                     'validate_opus_article_packet.py', 'compile_review_packet_article.py',
                     'selection_receipt.py', 'r2_update_carrier.py')}}
    route = review_transports.transport('hermes-openai-codex-sol61-independent')
    history = tmp_path/'real-qa-history'
    job = recovery.review_only_job(recovery.read(original), packet, history, route,
                                   validator=validator, binding={'fixture': 'not clinical acceptance'})
    job_path = tmp_path/'real-qa-job.json'; job_path.write_text(json.dumps(job))
    controller = controls/'opus_review_loop.py'
    calls = []
    actual_run = subprocess.run

    def actual_validator(command, **kwargs):
        assert command == loop.validate_executable(validator, 'validator') == argv
        competing_cli_refused(job_path, packet, history, tmp_path, actual_run)
        calls.append(command)
        monkeypatch.setattr(sys, 'argv', command[1:])
        out = io.StringIO(); err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = qa.main()
        return subprocess.CompletedProcess(command, rc, out.getvalue(), err.getvalue())

    monkeypatch.setattr(subprocess, 'run', actual_validator)

    def trace(frame, event, arg):
        if event == 'call' and frame.f_code.co_name == 'main' and frame.f_code.co_filename == str(controller):
            frame.f_globals.update(RUBRIC=loop.RUBRIC, REVIEW_SCHEMA=loop.REVIEW_SCHEMA,
                                   validate_topic_authority=loop.validate_topic_authority)
        return trace

    def cli():
        monkeypatch.setattr(sys, 'argv', [str(controller), '--job', str(job_path), '--review-only-once'])
        prior = sys.gettrace(); sys.settrace(trace)
        try:
            with pytest.raises(SystemExit) as exc:
                runpy.run_path(str(controller), run_name='__main__')
            assert exc.value.code == 2
        finally:
            sys.settrace(prior)
        return capsys.readouterr().err

    assert 'deterministic_validator_failed' in cli()
    assert_private_run_lock_released()
    assert len(calls) == 1 and not (history/'native-review-intent.json').exists()
    observed = recovery.read(packet/'07-qa-receipt.json')
    assert observed['status'] == 'BLOCKED' and observed['failures']
    assert (packet/'06-article.json').read_bytes() == (
        json.dumps(compiler.compile_packet(packet), indent=2, sort_keys=True)+'\n').encode()
    if consumed:
        # A prior consumed one-shot is synthetic state, not a claimed model PASS.
        _, manifest_path = loop.seal_subjects(job, 1, history)
        loop.reserve_review_only_intent(history, job, loop.sha_file(manifest_path))
    before = {p: p.read_bytes() for d in (packet, history) for p in d.rglob('*') if p.is_file()}
    assert 'receipt_exists:' in cli()
    after = {p: p.read_bytes() for d in (packet, history) for p in d.rglob('*') if p.is_file()}
    assert after == before, 'refused retry mutated actual QA/compiled subjects or sealed history'
    assert len(calls) == 1, 'refused retry executed mutating QA or transport'
    assert not list(history.glob('*sol-raw*')) and not recovery.recovery_path(entry, root).exists()
    assert all(Path(m.__file__).resolve().is_relative_to(ROOT) for m in (qa, compiler, loop, recovery))
