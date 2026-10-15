"""Conservative accounting only: no operation replay, settlement, or spend authority."""
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import glp_sponsor_firecrawl_booking as booking
import glp_sponsor_firecrawl_supply as supply


def fixture(tmp_path):
    ledger = tmp_path / 'operations.jsonl'
    row = {'schema': 'firecrawl-operation-v1', 'operation_id': 'unknown',
           'caller': supply.CALLER, 'endpoint': '/v2/scrape', 'at_utc': '2026-10-13T23:00:00+00:00',
           'creditsUsed': None, 'outcome': 'started', 'url_hash': [hashlib.sha256(b'target').hexdigest()], 'run': 'old'}
    ledger.write_text(json.dumps(row) + '\n')
    state = tmp_path / 'state'; state.mkdir()
    (state / 'firecrawl-supply-budget.json').write_text('{"day":"2026-10-14","reserved":103,"pending_by_day":{"2026-10-14":103}}')
    class Client:
        def request(self, endpoint, *, method):
            assert method == 'GET'
            if endpoint == 'team/credit-usage':
                return {'success': True, 'data': {'planCredits': 100000, 'remainingCredits': 54038,
                        'billingPeriodStart': '2026-09-19T23:59:59Z', 'billingPeriodEnd': '2026-10-19T23:59:59Z'}}
            assert endpoint == 'team/credit-usage/historical'
            return {'success': True, 'periods': [{'startDate': '2026-10-01T00:00:00Z', 'endDate': None, 'creditsUsed': 13}]}
    return ledger, state, row, Client()


def test_booking_is_append_only_nonzero_cas_and_preserves_ambiguity(tmp_path):
    ledger, state, row, client = fixture(tmp_path)
    before = ledger.read_bytes()
    reservation = (state / 'firecrawl-supply-budget.json').read_bytes()
    plan = booking.book(state, ledger, client, tmp_path / 'dry.json', apply=False)
    assert ledger.read_bytes() == before and plan['booked_operations'] == 1
    with pytest.raises(RuntimeError, match='ledger_changed'):
        booking.book(state, ledger, client, tmp_path / 'stale.json', apply=True, expected_sha='0'*64)
    result = booking.book(state, ledger, client, tmp_path / 'apply.json', apply=True, expected_sha=hashlib.sha256(before).hexdigest())
    assert result['status'] == 'applied' and result['unknown_remaining'] == 0
    assert ledger.read_bytes().startswith(before)
    latest = json.loads(ledger.read_text().splitlines()[-1])
    assert latest['creditsUsed'] == 13 and latest['outcome'] == 'started'
    for k, v in row.items():
        if k != 'creditsUsed': assert latest[k] == v
    audit = latest['cost_booking']
    assert audit['reason'] and audit['pre_ledger_sha256'] == hashlib.sha256(before).hexdigest()
    assert result['post_ledger_sha256'] == hashlib.sha256(ledger.read_bytes()).hexdigest()
    assert (state / 'firecrawl-supply-budget.json').read_bytes() == reservation
    again = booking.book(state, ledger, client, tmp_path / 'again.json', apply=False)
    assert again['booked_operations'] == 0
    p = supply.Producer(state_dir=state, import_only=True)
    p.operations = lambda: [json.loads(x) for x in ledger.read_text().splitlines()]
    with pytest.raises(RuntimeError, match='ambiguous_capture_effect_no_replay'):
        p.capture_admissible('scrape', 'target')


@pytest.mark.parametrize('delta', [0, 1, 15])
def test_whole_authenticated_window_delta_never_zero(tmp_path, delta):
    ledger, state, row, client = fixture(tmp_path)
    original = client.request
    def request(endpoint, **kw):
        value = original(endpoint, **kw)
        if endpoint.endswith('historical'): value['periods'][0]['creditsUsed'] = delta
        return value
    client.request = request
    result = booking.book(state, ledger, client, tmp_path / 'dry.json', apply=False)
    assert result['charges'][0]['credits'] == max(1, delta)


def test_future_null_guard_and_uncovered_history_fail_closed(tmp_path):
    ledger, state, row, client = fixture(tmp_path)
    row['at_utc'] = '2026-10-15T01:05:00+00:00'
    ledger.write_text(json.dumps(row) + '\n')
    with pytest.raises(RuntimeError, match='outside_decision_scope'):
        booking.book(state, ledger, client, tmp_path / 'future.json', apply=False)
    row['at_utc'] = '2026-09-01T00:00:00Z'
    ledger.write_text(json.dumps(row) + '\n')
    with pytest.raises(RuntimeError, match='usage_window_missing'):
        booking.book(state, ledger, client, tmp_path / 'uncovered.json', apply=False)


def test_same_budget_and_run_locks_refuse_before_api(tmp_path):
    import fcntl
    ledger, state, row, client = fixture(tmp_path)
    client.request = lambda *a, **k: pytest.fail('API after lock refusal')
    for name in ('run.lock', 'firecrawl-supply-budget.lock'):
        with (state / name).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(BlockingIOError):
                booking.book(state, ledger, client, tmp_path / (name + '.json'), apply=False)
