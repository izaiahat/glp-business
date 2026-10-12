"""Run the actual FDA provenance script with sandbox paths and transport."""
import builtins
import hashlib
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import firecrawl_client as fc


def test_gov_onboarding_does_not_grant_clearance():
    source = (ROOT / 'scripts/research_glp_batch2_fda_warning_provenance.py').read_text()
    assert '/v2/search/gov' in source and 'NO_MATCH_BOUNDED' in source
    assert 'DISCOVERY_REQUIRES_JURISDICTION_REVIEW_NOT_CLEARANCE' in source
    assert "--spec" in (ROOT / 'scripts/g6_r1a_stage5_research_fetch.py').read_text()


@pytest.fixture
def offline_fda(monkeypatch, tmp_path):
    business = tmp_path / 'business'
    home = tmp_path / 'home'
    (home / '.hermes').mkdir(parents=True)
    (home / '.hermes/.env').write_text('FIRECRAWL_API_KEY=fixture-only\n')
    output = business / 'reports' / 'fixture'
    spec = tmp_path / 'spec.json'
    spec.write_text(json.dumps({'programs': [
        {'program_id': 'fixture-clinic', 'display_name': 'Fixture Clinic',
         'urls': ['https://fixture.example.invalid/']},
        {'program_id': 'fella-health', 'display_name': 'Fella Health', 'urls': []},
    ]}))
    calls = []
    monkeypatch.setattr(fc, 'LEDGER', tmp_path / 'operations.jsonl')
    monkeypatch.setattr(fc.urllib.request, 'urlopen',
                        lambda *a, **kw: pytest.fail('unexpected urllib transport'))

    # Redirect the script's fixed business root and home only during execution.
    # The maintained script is run unmodified; all receipts stay in tmp_path.
    class SandboxPath(type(tmp_path)):
        def __new__(cls, *parts):
            if parts == ('/home/ubuntu/business',):
                return business
            return super().__new__(cls, *parts)

        @classmethod
        def home(cls):
            return home

    def sandbox_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'pathlib' and fromlist == ('Path',):
            return SimpleNamespace(Path=SandboxPath)
        return builtins.__import__(name, globals, locals, fromlist, level)

    def execute(responses, relative_output=False):
        pending = iter(responses)
        output_arg = str(output)
        if relative_output:
            business.mkdir(parents=True, exist_ok=True)
            monkeypatch.chdir(business)
            output_arg = str(output.relative_to(business))

        def post(url, **kwargs):
            calls.append((url, kwargs))
            try:
                status, payload = next(pending)
            except StopIteration:
                pytest.fail('unexpected extra provider request')

            def response_json():
                if isinstance(payload, Exception):
                    raise payload
                return payload
            return SimpleNamespace(status_code=status, json=response_json)

        monkeypatch.setattr(requests, 'post', post)
        with monkeypatch.context() as execution:
            execution.setattr(sys, 'argv', [str(ROOT / 'scripts/research_glp_batch2_fda_warning_provenance.py'),
                                          '--spec', str(spec), '--output', output_arg, '--limit', '1'])
            execution.setattr('time.sleep', lambda seconds: None)
            runpy.run_path(str(ROOT / 'scripts/research_glp_batch2_fda_warning_provenance.py'),
                           run_name='__main__',
                           init_globals={'__builtins__': dict(vars(builtins), __import__=sandbox_import)})

    return execute, output, business, calls


@pytest.mark.parametrize('case', ['brand_match', 'domain_match', 'unrelated', 'empty'])
def test_fda_execution_grouped_results_and_receipt_provenance(offline_fda, capsys, case):
    execute, output, business, calls = offline_fda
    hit = {'url': 'https://www.fda.gov/fixture-warning', 'title': 'Fixture Clinic warning letter',
           'description': 'Offline provenance fixture'}
    if case == 'domain_match':
        hit['title'] = 'Warning letter mentioning fixture.example.invalid'
    elif case == 'unrelated':
        hit['title'] = 'Unrelated company warning letter'
    matched = case in ('brand_match', 'domain_match')
    warning = {'success': True, 'data': {
        'web': [hit, {'title': 'Missing URL'}] if case != 'empty' else [],
        'news': [hit], 'images': [hit],
    }}
    state = {'success': True, 'data': {'web': []}}
    execute([(200, warning), (200, state)])
    summary = json.loads(capsys.readouterr().out)
    assert summary == {'status': 'PASS', 'programs': 1,
                       'matches': ['fixture-clinic'] if matched else [],
                       'ledger': str(output / 'fda-warning-bounded-search-ledger.json')}
    ledger = json.loads((output / 'fda-warning-bounded-search-ledger.json').read_text())
    assert len(ledger['programs']) == 1  # Fella stays excluded.
    row = ledger['programs'][0]
    assert row['status'] == ('MATCH_MANUAL_REVIEW' if matched else 'NO_MATCH_BOUNDED')
    assert row['result_count'] == int(case != 'empty')
    assert row['exact_brand_or_domain_result_count'] == int(matched)
    assert row['matched_results'] == ([hit] if matched else [])
    assert 'not legal clearance' in row['search_scope']
    raw_path = business / row['raw_receipt_path']
    raw_bytes = raw_path.read_bytes()
    assert row['raw_receipt_sha256'] == hashlib.sha256(raw_bytes).hexdigest()
    raw = json.loads(raw_bytes)
    assert raw['response'] == warning
    assert raw['searched_at_utc'] == row['searched_at_utc'] == ledger['generated_at_utc']
    assert raw['query'] == row['firecrawl_query'] == calls[0][1]['json']['query']
    state_raw = json.loads(Path(row['state_rules_receipt_path']).read_text())
    assert state_raw['response'] == state
    assert state_raw['status'] == 'DISCOVERY_REQUIRES_JURISDICTION_REVIEW_NOT_CLEARANCE'
    assert len(calls) == 2
    assert all(url == 'https://api.firecrawl.dev/v2/search/gov' for url, _ in calls)
    assert all(options['json']['k'] == 10 and options['timeout'] == 60 for _, options in calls)
    assert 'site:fda.gov' in calls[0][1]['json']['query']
    assert 'fixture.example.invalid' in calls[0][1]['json']['query']
    assert 'fella' not in json.dumps(calls).lower()
    operations = [json.loads(line) for line in fc.LEDGER.read_text().splitlines()]
    assert [op['caller'] for op in operations] == [
        'research_glp_batch2_fda_warning_provenance', 'research_glp_batch2_fda_warning_provenance',
        'research_glp_batch2_fda_warning_provenance.states', 'research_glp_batch2_fda_warning_provenance.states']
    assert [op['outcome'] for op in operations] == ['started', 'success', 'started', 'success']
    assert 'fixture-only' not in fc.LEDGER.read_text()


def test_relative_output_does_not_fail_after_two_paid_calls(offline_fda):
    execute, output, _, calls = offline_fda
    execute([(200, {'success': True, 'data': {'web': []}})] * 2, relative_output=True)
    assert (output / 'fda-warning-bounded-search-ledger.json').exists()
    assert len(calls) == 2


@pytest.mark.parametrize('data', [None, [], {}, {'news': []}, {'web': None},
                                       {'web': {}}, {'web': [None]}, {'web': ['not a row']}])
def test_fda_execution_malformed_warning_never_becomes_no_match(offline_fda, capsys, data):
    execute, output, _, calls = offline_fda
    with pytest.raises(ValueError, match='firecrawl_search_grouped_data_'):
        execute([(200, {'success': True, 'data': data})])
    assert len(calls) == 1
    assert not (output / 'fda-warning-bounded-search-ledger.json').exists()
    assert not list(output.rglob('*-state-rules.json'))
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('response', [
    (503, {'success': True, 'data': {'web': []}}),
    (200, {'success': False, 'data': {'web': []}}),
    (200, ValueError('offline non-JSON response')),
])
def test_fda_execution_unavailable_warning_never_reports_pass(offline_fda, capsys, response):
    execute, output, _, calls = offline_fda
    with pytest.raises(RuntimeError, match='fda_search_unavailable_not_no_match'):
        execute([response])
    assert len(calls) == 1
    assert not (output / 'fda-warning-bounded-search-ledger.json').exists()
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('status,payload', [
    (503, {'success': True, 'data': {'web': []}}),
    (200, {'success': False, 'data': {'web': []}}),
])
def test_fda_execution_unavailable_state_never_grants_clearance(offline_fda, capsys, status, payload):
    execute, output, _, calls = offline_fda
    with pytest.raises(RuntimeError, match='state_rules_unavailable_not_clearance'):
        execute([(200, {'success': True, 'data': {'web': []}}), (status, payload)])
    assert len(calls) == 2
    assert not (output / 'fda-warning-bounded-search-ledger.json').exists()
    assert not list(output.rglob('*-state-rules.json'))
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('payload', [
    {'success': True},
    {'success': True, 'data': None},
    {'success': True, 'data': []},
    {'success': True, 'data': {}},
    {'success': True, 'data': {'news': []}},
    {'success': True, 'data': {'web': None}},
    {'success': True, 'data': {'web': {}}},
    {'success': True, 'data': {'web': 'not a list'}},
    {'success': True, 'data': {'web': [None]}},
])
def test_fda_execution_malformed_state_never_writes_pass_provenance(offline_fda, capsys, payload):
    execute, output, _, calls = offline_fda
    with pytest.raises(ValueError, match='firecrawl_search_grouped_data_'):
        execute([(200, {'success': True, 'data': {'web': []}}), (200, payload)])
    assert len(calls) == 2  # One warning search, one state search; no replay.
    assert (output / 'fda-warning-search-raw/fixture-clinic.json').exists()
    assert not (output / 'fda-warning-bounded-search-ledger.json').exists()
    assert not list(output.rglob('*-state-rules.json'))
    assert capsys.readouterr().out == ''
