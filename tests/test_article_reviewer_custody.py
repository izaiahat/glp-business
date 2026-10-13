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
                 'physical_source_lines': review.physical_source_lines,
                 'subject_paths': lambda job: [Path(job['packet_dir'])/'04-draft.md']}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    subject = tmp_path/'04-draft.md'
    subject.write_bytes(text if isinstance(text, bytes) else text.encode())
    manifest = {'manifest_sha256': 'a'*64}
    result = namespace['build_context_bundle']({'packet_dir':str(tmp_path)}, manifest, 1, tmp_path)
    assert result.read_text() == native_context(subject, manifest)



def native_context(subject, manifest):
    chunks = ['# SEALED PACKET CONTENT \u2014 ALREADY IN BAND', '',
              f"Subject manifest SHA-256: {manifest['manifest_sha256']}",
              'Every source line is prefixed with its original 1-based line number.',
              '', f'## FILE: {subject.name}', '']
    lines = subject.read_bytes().decode('utf-8').replace('\r\n', '\n').split('\n')
    if lines[-1] == '':
        lines.pop()
    chunks.extend(f'{n}|{s}' for n, s in enumerate(lines, 1))
    return '\n'.join(chunks) + '\n'


def fixture(tmp_path, monkeypatch, text='Source-backed draft.\n'):
    packet = tmp_path / 'packet'
    packet.mkdir()
    subject = packet / '04-draft.md'
    subject.write_bytes(text if isinstance(text, bytes) else text.encode())
    manifest = {'manifest_sha256': 'a' * 64, 'subject_files': [{'path': subject.name}]}
    context = tmp_path / 'context.txt'
    context.write_text(native_context(subject, manifest))
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


@pytest.mark.parametrize('text', ['Source-backed draft.\n', '{"quote":"a\u2028b\u0085c\u2029d"}\n',
                                b'{\r"a":1}\n', b'first\rsecond\nthird\r\nfourth\r',
                                b'', b'\n', b'first\nsecond', b'first\nsecond\n',
                                b'first\r\nsecond\r\n', b'\r'])
def test_producer_retains_runtime_result_custody(tmp_path, monkeypatch, text):
    args, inputs, db, packet, manifest, handle, closed = fixture(tmp_path, monkeypatch, text)
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
