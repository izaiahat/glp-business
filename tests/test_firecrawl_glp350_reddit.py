"""Exercise the maintained Reddit search caller without provider effects."""
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import firecrawl_client as fc
import glp_reddit_daily_scout as scout


@pytest.fixture
def offline_search(monkeypatch, tmp_path):
    env = tmp_path / 'fixture.env'
    env.write_text('FIRECRAWL_API_KEY=fixture-only\n')
    monkeypatch.setattr(scout, 'HERMES_ENV', env)
    monkeypatch.setattr(fc, 'LEDGER', tmp_path / 'operations.jsonl')
    calls = []

    def install(payload):
        def urlopen(request, *, timeout):
            calls.append((request, timeout))
            return io.BytesIO(json.dumps(payload).encode())
        monkeypatch.setattr(fc.urllib.request, 'urlopen', urlopen)
        return scout.live_search_fn()

    return install, calls


def test_reddit_live_search_consumes_web_group_only(offline_search):
    install, calls = offline_search
    search = install({'success': True, 'data': {
        'web': [
            {'url': 'https://www.reddit.com/r/GLP1/comments/fixture/question/',
             'title': 'Fixture question', 'description': 'Fixture snippet', 'extra': 'ignored'},
            {'url': 'https://www.reddit.com/r/GLP1/comments/second/'},
            {'title': 'No URL'},
        ],
        'news': [{'url': 'https://news.example.invalid/fixture'}],
        'images': [{'url': 'https://images.example.invalid/fixture'}],
    }})
    assert search('fixture query', tbs='qdr:w', limit=7) == [
        {'url': 'https://www.reddit.com/r/GLP1/comments/fixture/question/',
         'title': 'Fixture question', 'description': 'Fixture snippet'},
        {'url': 'https://www.reddit.com/r/GLP1/comments/second/', 'title': '', 'description': ''},
    ]
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == 'https://api.firecrawl.dev/v2/search'
    assert request.get_method() == 'POST'
    assert json.loads(request.data) == {'query': 'fixture query', 'limit': 7, 'tbs': 'qdr:w'}
    assert timeout == 120
    rows = [json.loads(line) for line in fc.LEDGER.read_text().splitlines()]
    assert [row['outcome'] for row in rows] == ['started', 'success']
    assert all(row['caller'] == 'glp_reddit_daily_scout' for row in rows)
    assert 'fixture-only' not in fc.LEDGER.read_text()


def test_reddit_live_search_accepts_genuine_empty_web_group(offline_search):
    install, calls = offline_search
    search = install({'success': True, 'data': {'web': [], 'news': [{'url': 'https://news.example.invalid'}]}})
    assert search('fixture query') == []
    assert len(calls) == 1
    assert json.loads(calls[0][0].data) == {'query': 'fixture query', 'limit': 20}


@pytest.mark.parametrize('payload', [
    {'success': False, 'data': {'web': []}},
    {'data': {'web': []}},
    {'success': True},
    {'success': True, 'data': None},
    {'success': True, 'data': []},
    {'success': True, 'data': {}},
    {'success': True, 'data': {'news': []}},
    {'success': True, 'data': {'web': None}},
    {'success': True, 'data': {'web': {}}},
    {'success': True, 'data': {'web': ['not a row']}},
    {'success': True, 'data': {'web': [{'url': 'https://example.invalid'}, None]}},
])
def test_reddit_live_search_refuses_malformed_as_empty(offline_search, payload):
    install, calls = offline_search
    with pytest.raises((ValueError, RuntimeError), match='firecrawl_search_'):
        install(payload)('fixture query')
    assert len(calls) == 1
