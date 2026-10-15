"""Discovery supplies research targets only; all commercial gates stay downstream."""
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import glp_sponsor_discovery as discovery


def test_query_pack_covers_existing_categories_and_rotates_geography():
    rows = discovery.query_pack()
    assert {r['category'] for r in rows} == {'nutrition_protein', 'meals', 'fitness', 'cgm', 'weight_coaching', 'telehealth_glp1', 'pharmacy'}
    assert len({r['query'] for r in rows}) == len(rows) > 300
    assert len({r['category'] for r in rows[:4]}) == 4
    assert any('California' in r['query'] for r in rows)


def test_free_directory_uses_only_literal_https_official_sites_and_retains_provenance(tmp_path):
    calls = []
    def fetch(api, params):
        calls.append((api, params))
        if params['action'] == 'query':
            return {'query': {'pages': {'1': {'title': 'Fixture Fitness', 'pageprops': {'wikibase_item': 'Q123'}}}}}
        return {'entities': {'Q123': {'claims': {'P856': [
            {'mainsnak': {'datavalue': {'value': 'https://www.fresh.example/fitness'}}},
            {'mainsnak': {'datavalue': {'value': 'https://fresh.ca/'}}},
            {'mainsnak': {'datavalue': {'value': 'https://alternate.example/'}}},
            {'mainsnak': {'datavalue': {'value': 'http://insecure.example/'}}},
            {'mainsnak': {'datavalue': {'value': 'https://bad.example/?aff=1'}}},
        ]}}}}
    d = discovery.Discovery(tmp_path, fetch=fetch)
    result = d.run(free_only=True, directory_batches=1)
    assert result['new_hosts'] == ['fresh.example']
    row = d.load()['candidates']['fresh.example']
    assert row['source'] == 'wikidata_P856' and row['entity'] == 'Q123'
    assert row['url'] == 'https://www.fresh.example/fitness'
    assert (tmp_path / row['receipt']).is_file()
    import hashlib
    assert hashlib.sha256((tmp_path / row['receipt']).read_bytes()).hexdigest() == row['receipt_sha256']
    assert len(calls) == 2
    assert 'email' not in row and result['verified_contacts'] == 0


def test_directory_cursor_continues_then_advances_and_keeps_backlog(tmp_path):
    tokens = []
    def fetch(api, params):
        tokens.append(params.get('gcmcontinue'))
        if len(tokens) == 1:
            return {'continue': {'gcmcontinue': 'next'}, 'query': {'pages': {}}}
        return {'query': {'pages': {}}}
    d = discovery.Discovery(tmp_path, fetch=fetch)
    d.run(free_only=True, directory_batches=1)
    d.run(free_only=True, directory_batches=1)
    assert tokens == [None, 'next']
    state = d.load()
    assert state['directory_index'] == 1 and state['directory_continue'] is None


def test_search_persists_receipt_before_cursor_and_does_not_refilter_stored_backlog(tmp_path):
    queries = []
    def search(payload):
        queries.append(payload)
        return {'success': True, 'data': {'web': [{'source': 'wikidata_P856', 'url': 'https://fresh.example/partners', 'title': 'Fresh', 'description': 'Fixture'}]}}
    d = discovery.Discovery(tmp_path, search=search)
    r = d.run(directory_batches=0, search_batches=2)
    assert len(queries) == 2 and queries[0] != queries[1]
    assert all(q['limit'] == 100 and q['sources'] == ['web'] and 'scrapeOptions' not in q for q in queries)
    assert r['new_hosts'] == ['fresh.example'] and d.load()['query_index'] == 2
    assert d.load()['candidates']['fresh.example']['source'] == 'firecrawl_search'
    # No transient 'seen' filter may consume a host before it reaches capture.
    r = d.run(free_only=True, directory_batches=0)
    assert r['candidate_hosts'] == ['fresh.example'] and r['new_hosts'] == []


def test_budget_refusal_does_not_advance_or_pay_and_unknown_does_not_replay(tmp_path):
    called = []
    def refused(payload):
        called.append(payload)
        raise discovery.AdmissionRefused('sponsor_ledger_credit_cost_unknown')
    d = discovery.Discovery(tmp_path, search=refused)
    r = d.run(directory_batches=0)
    assert len(called) == 1 and r['search_status'] == 'sponsor_ledger_credit_cost_unknown'
    assert d.load()['query_index'] == 0
    d.run(directory_batches=0)
    assert len(called) == 1  # day-local admission refusal is not hammered
    # Unknown transport after admission stays pending, never silently replayed.
    other = discovery.Discovery(tmp_path / 'unknown', search=lambda p: (_ for _ in ()).throw(TimeoutError()))
    r = other.run(directory_batches=0)
    assert r['search_status'] == 'pending_reconciliation'
    other.search = lambda p: pytest.fail('uncertain paid query replayed')
    assert other.run(directory_batches=0)['search_status'] == 'pending_reconciliation'


@pytest.mark.parametrize('url', ['http://a.example', 'https://127.0.0.1/', 'https://localhost/', 'https://a.example/?utm_source=x', 'https://user:pass@a.example/', 'https://a.example:9443/', 'https://foo.wikipedia.org/a', 'https://a.example/go/track'])
def test_untrusted_directory_urls_not_promoted(url):
    assert discovery.research_host(url) is None


def test_no_paid_client_or_live_source_is_created_by_free_only(tmp_path):
    d = discovery.Discovery(tmp_path, fetch=lambda *a: {'query': {'pages': {}}}, search=lambda p: pytest.fail('paid'))
    assert d.run(free_only=True, directory_batches=1)['paid_search_attempts'] == 0


def test_post_transport_budget_error_is_ambiguous_not_a_safe_admission_refusal(tmp_path):
    d = discovery.Discovery(tmp_path, search=lambda _: (_ for _ in ()).throw(RuntimeError('sponsor_ledger_credit_cost_unknown')))
    assert d.run(directory_batches=0)['search_status'] == 'pending_reconciliation'
    assert d.load()['pending_query'] == 0


def test_real_concurrent_discovery_refuses_before_effects(tmp_path):
    import fcntl
    tmp_path.mkdir(exist_ok=True)
    with (tmp_path / 'discovery.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        d = discovery.Discovery(tmp_path, search=lambda _: pytest.fail('concurrent paid search'))
        with pytest.raises(BlockingIOError):
            d.run(directory_batches=0, search_batches=1)
    assert not d.path.exists()


def test_unparseable_paid_response_is_retained_and_never_replayed(tmp_path):
    d = discovery.Discovery(tmp_path, search=lambda _: {'success': False, 'error': 'upstream failure'})
    with pytest.raises(ValueError, match='firecrawl_search_failed'):
        d.run(directory_batches=0, search_batches=1)
    assert len(list((tmp_path / 'receipts').glob('*.json'))) == 1
    assert d.load()['pending_query'] == 0 and d.load()['query_index'] == 0


@pytest.mark.parametrize('status', [429, 503])
@pytest.mark.parametrize('retry_header', ['120', 'Thu, 15 Oct 2026 00:01:59 GMT'])
@pytest.mark.parametrize('throttled_api', [discovery.WIKIPEDIA, discovery.WIKIDATA])
def test_retry_after_survives_restart_and_midnight_then_resumes(tmp_path, monkeypatch, status, retry_header, throttled_api):
    import datetime
    import email.message
    import urllib.error
    from types import SimpleNamespace
    real_datetime = datetime.datetime
    class Clock(real_datetime):
        current = real_datetime(2026, 10, 14, 23, 59, 59, tzinfo=datetime.timezone.utc)
        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)
    monkeypatch.setattr(discovery.dt, 'datetime', Clock)
    calls = []
    def open_request(req, timeout):
        calls.append(req.full_url)
        if req.full_url.startswith(throttled_api) and not resumed[0]:
            headers = email.message.Message()
            headers['Retry-After'] = retry_header
            raise urllib.error.HTTPError(req.full_url, status, 'throttled', headers, None)
        payload = ({'query': {'pages': {'1': {'title': 'Fixture', 'pageprops': {'wikibase_item': 'Q1'}}}}}
                   if req.full_url.startswith(discovery.WIKIPEDIA) else {'entities': {}})
        import io
        class Response(io.BytesIO):
            status = 200
        return Response(json.dumps(payload).encode())
    resumed = [False]
    monkeypatch.setattr(discovery.urllib.request, 'build_opener', lambda *a: SimpleNamespace(open=open_request))
    d = discovery.Discovery(tmp_path)
    state = d.load()
    state.update(directory_continue='retained-token', pending_query=3, query_index=3)
    d.save(state)
    assert d.run(directory_batches=1)['directory_status'] == 'failed:HTTPError'
    frozen = d.path.read_bytes()
    count = len(calls)
    for seconds in (1, 118):
        Clock.current += datetime.timedelta(seconds=seconds)
        report = discovery.Discovery(tmp_path).run(directory_batches=1)
        assert report['directory_status'] == 'retry_after'
        assert len(calls) == count and d.path.read_bytes() == frozen
        assert report['search_status'] == 'pending_reconciliation'
    Clock.current += datetime.timedelta(seconds=1)  # exact expiry, not one second early
    resumed[0] = True
    report = discovery.Discovery(tmp_path).run(directory_batches=1)
    assert report['directory_status'] == 'ok' and len(calls) == count + 2
    assert d.load()['directory_index'] == 1 and d.load()['directory_continue'] is None
    assert d.load()['pending_query'] == 3 and d.load()['query_index'] == 3


def test_receipt_write_failure_does_not_advance_cursor(tmp_path, monkeypatch):
    d = discovery.Discovery(tmp_path, search=lambda p: {'success': True, 'data': {'web': []}})
    monkeypatch.setattr(d, 'receipt', lambda *a: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError):
        d.run(directory_batches=0, search_batches=1)
    assert d.load()['query_index'] == 0 and d.load()['pending_query'] == 0
