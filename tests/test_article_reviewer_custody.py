"""Offline runtime-owned reviewer custody; no model call or publication."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import hermes_sol_article_review as review


@pytest.mark.parametrize('text', ['Actual native line.\nSecond line.\n',
                                '{"quote":"before\u2028after\u0085end\u2029"}\n', ''])
def test_fixture_matches_native_registered_context_builder(tmp_path, text):
    import ast, os
    source = ROOT / 'systems/seo-blog-machine/scripts/opus_review_loop.py'
    tree = ast.parse(source.read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name in {'physical_source_lines', 'build_context_bundle'}]
    namespace = {'Path': Path, 'os': os, 'Any': object,
                 'physical_lines': review.physical_lines,
                 'subject_paths': lambda job: [Path(job['packet_dir'])/'04-draft.md']}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    subject = tmp_path/'04-draft.md'
    subject.write_bytes(text if isinstance(text, bytes) else text.encode())
    manifest = {'manifest_sha256': 'a'*64}
    result = namespace['build_context_bundle']({'packet_dir':str(tmp_path)}, manifest, 1, tmp_path)
    assert result.read_text() == native_context(subject, manifest)



def native_context(subject, manifest):
    # Retain the current registered physical-line envelope, not PR441's older
    # transport spelling. Independent legacy reviews require their original code.
    chunks = ['SEALED ARTICLE SUBJECTS — READ ONLY',
              f"manifest_sha256={manifest['manifest_sha256']}", '',
              f'===== {subject.name} =====']
    text = subject.read_bytes().decode('utf-8').replace('\r\n', '\n')
    lines = text.split('\n') if text else []
    if lines and lines[-1] == '':
        lines.pop()
    chunks.extend(f'{n:04d}|{s}' for n, s in enumerate(lines, 1))
    chunks.append('')
    return '\n'.join(chunks) + '\n'


def fixture(tmp_path, monkeypatch, text='Source-backed draft.\n', envelope='current'):
    packet = tmp_path / 'packet'
    packet.mkdir()
    subject = packet / '04-draft.md'
    subject.write_bytes(text if isinstance(text, bytes) else text.encode())
    manifest = {'manifest_sha256': 'a' * 64, 'subject_files': [{'path': subject.name}]}
    context = tmp_path / 'context.txt'
    content = native_context(subject, manifest)
    if envelope == 'pr441':
        parts = ['# SEALED PACKET CONTENT — ALREADY IN BAND', '',
                 f"Subject manifest SHA-256: {manifest['manifest_sha256']}",
                 'Every source line is prefixed with its original 1-based line number.',
                 '', f'## FILE: {subject.name}', '']
        parts.extend(f'{i}|{line}' for i, line in enumerate(review.physical_lines(subject.read_bytes()), 1))
        content = '\n'.join(parts) + '\n'
    context.write_text(content)
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('Review all five gates against manifest ' + 'a' * 64)
    args = SimpleNamespace(context_file=[str(context)], prompt_file=str(prompt), prompt=None,
                           json_schema_file=None, model='gpt-6-astra-900k', provider='openai-codex',
                           effort='high', disable_tools=True, timeout=60)
    args.max_context_chars = review.DEFAULT_MAX_CONTEXT_CHARS
    args.max_total_chars = review.DEFAULT_MAX_TOTAL_CHARS
    captured = {}
    payload = review.build_payload(args, captured)
    inputs = review.review_input(args, payload, captured)
    response = review.ADVISORY_HEADER + '\n\n' + json.dumps({'summary': 'Runtime reviewer decision'})
    db = tmp_path / 'state.db'
    with sqlite3.connect(db) as c:
        c.execute('create table sessions(id text, model text, model_config text, system_prompt text, '
                  'started_at real, ended_at real, billing_provider text, api_call_count int, tool_call_count int)')
        c.execute('create table messages(id integer,session_id text,role text,content text,tool_calls text,timestamp real)')
        c.execute('insert into sessions values(?,?,?,?,?,?,?,?,?)',
                  ('reviewer', args.model, json.dumps({'max_iterations': 1, 'reasoning_config': {'effort': 'high'}}),
                   review.SYSTEM_BRIEF, 10, 20, args.provider, 1, 0))
        c.executemany('insert into messages values(?,?,?,?,?,?)',
                      [(1, 'reviewer', 'user', payload, None, 11),
                       (2, 'reviewer', 'assistant', response, None, 19)])
    monkeypatch.setattr(review, 'SESSION_DB', db, raising=False)
    monkeypatch.setattr(review, 'ensure_hermes_python', lambda: None)
    monkeypatch.setitem(sys.modules, 'agent.reasoning_effort', SimpleNamespace(
        codex_supported_efforts=lambda model: ['high']))
    monkeypatch.setitem(sys.modules, 'hermes_cli.runtime_provider', SimpleNamespace(
        resolve_runtime_provider=lambda **kw: {'provider': args.provider, 'requested_provider': args.provider,
                                               'api_mode': 'codex_responses'}))
    handle = object()
    closed = []

    class Agent:
        def __init__(self, **kwargs):
            assert kwargs['session_db'] is handle, 'reviewer_must_use_native_sessiondb'
            self.kwargs = kwargs
            self.session_id = 'reviewer'
            self.provider, self.model = args.provider, args.model
            self.api_mode = 'codex_responses'
            self.tools, self.valid_tool_names, self.fallback_providers = [], [], []
            self.tool_calls_made = 0
            self._compression_summary_calls = 0
            self.base_url = 'https://chatgpt.com/backend-api/codex'
            self.input_tokens, self.output_tokens = 100, 10
            self.closed = False
            self.started = False
            self._cached_system_prompt = None
        def _build_system_prompt(self, system_message):
            return system_message
        def _ensure_db_session(self):
            self.started = True
        def run_conversation(self, user_message, **kwargs):
            assert self.started
            assert kwargs['system_message'] == review.SYSTEM_BRIEF
            assert user_message == payload
            return {'final_response': response, 'api_calls': 1, 'completed': True,
                    'provider': args.provider, 'model': args.model, 'session_id': 'reviewer',
                    'messages': [{'role': 'assistant', 'content': response}]}
        def close(self):
            self.closed = True
            closed.append(self.kwargs.get('session_db'))

    monkeypatch.setitem(sys.modules, 'run_agent', SimpleNamespace(AIAgent=Agent))
    monkeypatch.setitem(sys.modules, 'hermes_state_registry', SimpleNamespace(
        acquire=lambda path: handle, release_or_close=lambda value: closed.append(value)))
    return args, inputs, db, packet, manifest, handle, closed


@pytest.mark.parametrize('source,lines', [
    ('a\u2028b\u2029c\x85d\x0be\x0cf\n'.encode(), ['a\u2028b\u2029c\x85d\x0be\x0cf']),
    (b'{\r"quote": "valid JSON whitespace"}\r', ['{\r"quote": "valid JSON whitespace"}\r']),
    (b'a\r\nb\nc\r', ['a', 'b', 'c\r']),
    (b'', []),
])
def test_custodian_replays_physical_lines_without_backfilling(tmp_path, monkeypatch, source, lines):
    args, _, db, packet, manifest, _, _ = fixture(tmp_path, monkeypatch)
    (packet / '04-draft.md').write_bytes(source)
    context = ('SEALED ARTICLE SUBJECTS — READ ONLY\nmanifest_sha256=' + 'a' * 64 +
               '\n\n===== 04-draft.md =====\n' +
               ''.join(f'{n:04d}|{line}\n' for n, line in enumerate(lines, 1)) + '\n')
    Path(args.context_file[0]).write_bytes(context.encode())
    captured = {}
    payload = review.build_payload(args, captured)
    with sqlite3.connect(db) as c:
        c.execute('update messages set content=? where id=1', (payload,))
        response = c.execute('select content from messages where id=2').fetchone()[0]
    custody = review.authenticate_result('reviewer', payload, response, model=args.model,
                                         provider=args.provider, effort=args.effort, session_db=db)
    runtime = {'session_id': 'reviewer', 'model': args.model, 'provider': args.provider,
               'effort': args.effort, 'result_custody': custody}
    raw = {'session_id': 'reviewer', 'result': response, 'hermes_runtime': runtime,
           'review_input': review.review_input(args, payload, captured, runtime)}
    before = db.read_bytes()
    assert review.validate_result_custody(raw, manifest=manifest, packet=packet, session_db=db) == custody
    historical = copy.deepcopy(raw)
    historical['hermes_runtime'].pop('result_custody')
    with pytest.raises(RuntimeError, match='custody'):
        review.validate_result_custody(historical, manifest=manifest, packet=packet, session_db=db)
    assert 'result_custody' not in historical['hermes_runtime']
    assert db.read_bytes() == before


@pytest.mark.parametrize('text', ['Source-backed draft.\n', '{"quote":"a\u2028b\u0085c\u2029d"}\n',
                                b'{\r"a":1}\n', b'first\rsecond\nthird\r\nfourth\r',
                                b'', b'\n', b'first\nsecond', b'first\nsecond\n',
                                b'first\r\nsecond\r\n', b'\r'])
@pytest.mark.parametrize('envelope', ['current', 'pr441'])
def test_producer_retains_runtime_result_custody(tmp_path, monkeypatch, text, envelope):
    args, inputs, db, packet, manifest, handle, closed = fixture(tmp_path, monkeypatch, text, envelope)
    captured = {}
    payload = review.build_payload(args, captured)
    response, usage, execution = review.run_hermes(payload, args)
    assert handle in closed, 'maintained producer must supply native runtime persistence'
    assert execution['result_custody']['response_message_id'] == 2
    raw = {'session_id': 'reviewer', 'result': response, 'review_input': inputs,
           'hermes_runtime': execution}
    assert review.validate_result_custody(raw, manifest=manifest, packet=packet) == execution['result_custody']
    # Ordinary receipt self-hashes are not authority: changing the session,
    # decision or reviewed bytes cannot change fixed runtime-owned DB messages.
    for tamper in ('missing', 'session', 'response', 'input', 'manifest', 'model', 'message'):
        forged = copy.deepcopy(raw)
        expected_manifest = copy.deepcopy(manifest)
        if tamper == 'missing':
            forged['hermes_runtime'].pop('result_custody')
        elif tamper == 'session':
            forged['session_id'] = forged['hermes_runtime']['session_id'] = 'NEVER_RAN'
            forged['hermes_runtime']['result_custody']['session_id'] = 'NEVER_RAN'
        elif tamper == 'response':
            forged['result'] = review.ADVISORY_HEADER + '\n\n' + json.dumps({'summary': 'Test-authored PASS'})
            forged['hermes_runtime']['result_custody']['response_sha256'] = review.sha_bytes(forged['result'].encode())
        elif tamper == 'input':
            Path(args.prompt_file).write_text('Changed prompt')
            captured = {}
            changed = review.build_payload(args, captured)
            forged['review_input'] = review.review_input(args, changed, captured, execution)
            forged['hermes_runtime']['result_custody']['payload_sha256'] = review.sha_bytes(changed.encode())
        elif tamper == 'manifest':
            expected_manifest['manifest_sha256'] = 'b' * 64
        elif tamper == 'model':
            forged['hermes_runtime']['model'] = 'gpt-6.1-sol'
        else:
            forged['hermes_runtime']['result_custody']['response_message_id'] = 99
        forged['record_sha256'] = hashlib.sha256(json.dumps(forged, sort_keys=True).encode()).hexdigest()
        with pytest.raises(RuntimeError, match='custody'):
            review.validate_result_custody(forged, manifest=expected_manifest, packet=packet)
        if tamper == 'input':
            Path(args.prompt_file).write_text('Review all five gates against manifest ' + 'a' * 64)


def test_registered_sol61_route_preserves_author_exclusions(tmp_path, monkeypatch, capsys):
    """Registered caller → wrapper → fixture native DB; NOT health acceptance."""
    import article_health_acceptance as health
    import socket
    sys.path.insert(0, str(ROOT / 'systems/seo-blog-machine/scripts'))
    import opus_review_loop as loop
    transports = loop.review_transports
    route_id = 'hermes-openai-codex-sol61-independent'
    route = transports.transport(route_id)
    assert (route['provider'], route['model'], route['effort'], route['speed'], route['service_tier']) == (
        'openai-codex', 'gpt-6.1-sol', 'high', 'fast', 'priority')
    assert transports.DEFAULT_TRANSPORT_ID == transports.ASTRA_TRANSPORT['transport_id']
    assert transports.transport(None) == transports.SOL_TRANSPORT  # historical identity unchanged
    monkeypatch.setitem(route, 'wrapper', ROOT / 'scripts/hermes_sol_article_review.py')
    args, _, db, packet, _, handle, closed = fixture(tmp_path, monkeypatch)
    for name in ('00-source.md', '01-brief.md', '02-evidence.md', '03-plan.md'):
        (packet / name).write_text('Synthetic sealed subject, no clinical claim.\n')
    with sqlite3.connect(db) as c:
        c.execute('alter table messages add column tool_name text')
        for sid, model in [('original', 'gpt-6-astra-900k'), ('successor', 'claude-opus-5-5')]:
            c.execute('insert into sessions values(?,?,?,?,?,?,?,?,?)',
                      (sid, model, '{}', '', 1, 8, 'fixture', 0, 0))
        for mid, sid, name in [(3, 'original', '00-source.md'), (4, 'successor', '04-draft.md')]:
            c.execute('insert into messages values(?,?,?,?,?,?,?)',
                      (mid, sid, 'tool', json.dumps({'verified': True, 'files_modified': [str(packet / name)]}),
                       None, 7, 'write_file'))
    calls = []

    class NativeFixtureAgent:
        # Only this isolated fixture represents native runtime-owned writes.
        def __init__(self, **kwargs):
            assert kwargs['session_db'] is handle
            assert kwargs['model'] == route['model']
            assert kwargs['request_overrides']['service_tier'] == 'priority'
            assert kwargs['reasoning_config']['effort'] == 'high'
            assert kwargs['enabled_toolsets'] == [] and kwargs['fallback_model'] is None
            assert kwargs['skip_context_files'] and kwargs['skip_memory']
            self.tools, self.valid_tool_names = [], []
        def _build_system_prompt(self, brief):
            return brief
        def _ensure_db_session(self):
            self.started = True
        def run_conversation(self, payload, **kw):
            assert self.started and kw['system_message'] == review.SYSTEM_BRIEF
            manifest = json.loads((tmp_path / 'history/round-01-subject-manifest.json').read_text())
            response = json.dumps({'summary': 'Synthetic offline route fixture; no health acceptance.',
                                  'verdict': 'PASS_OPUS_SEO_DRAFT', 'round': 1,
                                  'subject_manifest_sha256': manifest['manifest_sha256'],
                                  'gates': [{'gate': gate, 'status': 'PASS', 'findings': []} for gate in 'ABCDE'],
                                  'findings': []})
            with sqlite3.connect(db) as c:
                c.execute('update sessions set model=? where id="reviewer"', (route['model'],))
                c.execute('update messages set content=? where id=1', (payload,))
                c.execute('update messages set content=? where id=2', (response,))
            return {'final_response': response, 'api_calls': 1, 'completed': True,
                    'provider': route['provider'], 'model': route['model'], 'session_id': 'reviewer',
                    'output_tokens': 50, 'messages': [{'role': 'assistant', 'content': response}]}
        def close(self):
            closed.append(handle)

    monkeypatch.setitem(sys.modules, 'run_agent', SimpleNamespace(AIAgent=NativeFixtureAgent))
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('network forbidden'))
    monkeypatch.setattr(loop, 'RUBRIC', tmp_path / 'rubric.md')
    loop.RUBRIC.write_text('Synthetic rubric: all five gates, zero findings required.\n')
    schema_path = tmp_path / 'offline-schema.json'
    schema_path.write_text(json.dumps({'type': 'object', 'required': ['verdict', 'round', 'subject_manifest_sha256', 'gates', 'findings']}))
    monkeypatch.setattr(loop, 'REVIEW_SCHEMA', schema_path)
    monkeypatch.setattr(loop, 'validate_topic_authority', lambda job: {'type': 'synthetic fixture'})
    monkeypatch.setattr(loop, 'run_validator', lambda *a: None)  # no unrelated compiler/QA effects
    monkeypatch.setattr(loop, 'run_publish', lambda *a: pytest.fail('publication forbidden'))
    monkeypatch.setattr(loop, 'run_repair', lambda *a: pytest.fail('repair forbidden'))

    def registered_wrapper(argv, **kw):
        assert Path(argv[1]).name == 'hermes_sol_article_review.py'
        calls.append(argv)
        monkeypatch.setattr(sys, 'argv', argv[1:])
        rc = review.main()  # real parser, route, emission and fixed-DB authentication
        captured = capsys.readouterr()
        return SimpleNamespace(returncode=rc, stdout=captured.out, stderr=captured.err)

    monkeypatch.setattr(loop.subprocess, 'run', registered_wrapper)
    job = {'schema': 'glp-opus-seo-review-job-v1', 'max_rounds': 5,
           'job_id': 'offline-fresh-sol61', 'topic_id': 'offline-fixture',
           'packet_dir': str(packet), 'history_dir': str(tmp_path / 'history'),
           'subject_files': sorted(p.name for p in packet.iterdir()), 'review_transport': route_id,
           'review_wrapper_sha256': review.sha_file(ROOT / 'scripts/hermes_sol_article_review.py'),
           'rubric_sha256': review.sha_file(loop.RUBRIC), 'review_schema_sha256': review.sha_file(loop.REVIEW_SCHEMA)}
    jobpath = tmp_path / 'job.json'
    jobpath.write_text(json.dumps(job))
    result = loop.run(jobpath, False, review_only_once=True)
    assert len(calls) == 1 and handle in closed
    assert result['status'] == 'PASS_OPUS_SEO_DRAFT' and result['publication_invoked'] is False
    raw = json.loads((tmp_path / 'history/round-01-sol-raw.json').read_text())
    manifest = json.loads((tmp_path / 'history/round-01-subject-manifest.json').read_text())
    custody = raw['hermes_runtime']['result_custody']
    assert list(raw['modelUsage']) == [route['model']]
    assert transports.validate_raw_review_runtime(raw) == route
    before = db.read_bytes()
    assert review.validate_result_custody(raw, manifest=manifest, packet=packet) == custody
    reviewer = {'model': route['model'], 'runtime': {'native_session_id': custody['session_id']}}
    paths = [packet / name for name in job['subject_files']]
    assert {r['session_id'] for r in health.packet_authors(db, paths, reviewer, 30)} == {'original', 'successor'}
    for sid in ('original', 'successor'):
        assert health.author_session(db, sid, reviewer, 30)['session_id'] == sid
    # Every original AND successor author retains both exclusions independently.
    for sid, model in [('original', 'gpt-6-astra-900k'), ('successor', 'claude-opus-5-5')]:
        for excluded in ({**reviewer, 'model': model},
                         {**reviewer, 'runtime': {'native_session_id': sid}}):
            with pytest.raises(ValueError, match='same model or session'):
                health.packet_authors(db, paths, excluded, 30)
            with pytest.raises(ValueError, match='same model or session'):
                health.author_session(db, sid, excluded, 30)
    for tamper in ('model', 'response', 'custody', 'session', 'route', 'tier', 'usage'):
        forged = copy.deepcopy(raw)
        if tamper == 'model':
            for block in ('review_input', 'hermes_runtime'):
                forged[block].update(model='gpt-6-astra-900k', transport_id=transports.DEFAULT_TRANSPORT_ID,
                                     speed='normal', service_tier='default')
            forged['modelUsage'] = {'gpt-6-astra-900k': {'canonicalModel': 'gpt-6-astra-900k', 'outputTokens': 50}}
        elif tamper == 'response':
            forged['result'] = review.advisory_output('{"summary":"forged PASS"}')
            forged['hermes_runtime']['result_custody']['response_sha256'] = review.sha_bytes(forged['result'].encode())
        elif tamper == 'custody':
            forged['hermes_runtime'].pop('result_custody')
        elif tamper == 'session':
            forged['session_id'] = forged['hermes_runtime']['session_id'] = 'NEVER_RAN'
        elif tamper == 'route':
            forged['review_input']['transport_id'] = transports.DEFAULT_TRANSPORT_ID
        elif tamper == 'tier':
            forged['hermes_runtime']['service_tier'] = 'default'
        else:
            forged['modelUsage'] = {'gpt-6-astra-900k': {'canonicalModel': 'gpt-6-astra-900k', 'outputTokens': 50}}
        forged.pop('record_sha256', None)
        forged['record_sha256'] = review.sha_bytes(json.dumps(forged, sort_keys=True, separators=(',', ':'),
                                                            ensure_ascii=False).encode())
        with pytest.raises((RuntimeError, transports.TransportError)):
            transports.validate_raw_review_runtime(forged)
            review.validate_result_custody(forged, manifest=manifest, packet=packet)
    assert db.read_bytes() == before  # consumers never backfill native custody
    assert 'priority' in (tmp_path / 'history/round-01-review-prompt.md').read_text()
    for legacy in (None, *[r['transport_id'] for r in (transports.SOL_TRANSPORT, transports.OPUS_TRANSPORT,
                                                    transports.POLICY_OPUS_TRANSPORT, transports.POLICY_OPUS55_TRANSPORT)]):
        job['review_transport'] = legacy
        jobpath.write_text(json.dumps(job))
        with pytest.raises(RuntimeError, match='astra_only|current_article_review'):
            loop.run(jobpath, False, review_only_once=True)
    assert len(calls) == 1
