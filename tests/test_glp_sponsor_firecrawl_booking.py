"""Conservative accounting only: no operation replay, settlement, or spend authority."""
import datetime as dt
import hashlib
import io
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import firecrawl_client as fc
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


def test_exact_round1_decision_extends_only_approved_terminal_operation(tmp_path):
    ledger, state, row, client = fixture(tmp_path)
    row.update(operation_id='224ef323-f3b6-4780-8553-fbf191c3c598',
               at_utc='2026-10-15T03:53:23.394211+00:00',
               http_status=408, outcome='http_error', run='sponsor-supply-20261015T0343')
    ledger.write_text(json.dumps(row) + '\n')
    before = ledger.read_bytes()
    reservations = (state / 'firecrawl-supply-budget.json').read_bytes()
    with pytest.raises(RuntimeError, match='outside_decision_scope'):
        booking.book(state, ledger, client, tmp_path / 'old.json')
    approved_sha = 'f24d941b78b9612439102b541e1ed805c8025471af685894b1ab4d93202fa432'
    with pytest.raises(RuntimeError, match='exact_operation_decision_invalid'):
        booking.book(state, ledger, client, tmp_path / 'forged.json',
                     exact_operation_decision_sha='0' * 64)
    plan = booking.book(state, ledger, client, tmp_path / 'plan.json',
                        exact_operation_decision_sha=approved_sha)
    assert ledger.read_bytes() == before and plan['booked_operations'] == 1
    result = booking.book(state, ledger, client, tmp_path / 'apply.json', apply=True,
                          expected_sha=plan['pre_ledger_sha256'],
                          exact_operation_decision_sha=approved_sha)
    assert result['unknown_remaining'] == 0
    latest = json.loads(ledger.read_text().splitlines()[-1])
    assert latest['creditsUsed'] == 13 and latest['outcome'] == 'http_error'
    assert latest['cost_booking']['exact_operation_decision_sha256'] == approved_sha
    assert ledger.read_bytes().startswith(before)
    assert (state / 'firecrawl-supply-budget.json').read_bytes() == reservations
    for key, value in [('operation_id', 'unapproved'), ('http_status', 500),
                       ('at_utc', '2026-10-15T04:00:00Z'), ('outcome', 'started')]:
        altered = dict(row, **{key: value})
        ledger.write_text(json.dumps(altered) + '\n')
        with pytest.raises(RuntimeError, match='outside_decision_scope'):
            booking.book(state, ledger, client, tmp_path / (key + '.json'),
                         exact_operation_decision_sha=approved_sha)


def test_same_budget_and_run_locks_refuse_before_api(tmp_path):
    import fcntl
    ledger, state, row, client = fixture(tmp_path)
    client.request = lambda *a, **k: pytest.fail('API after lock refusal')
    for name in ('run.lock', 'firecrawl-supply-budget.lock'):
        with (state / name).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(BlockingIOError):
                booking.book(state, ledger, client, tmp_path / (name + '.json'), apply=False)


def real_client(ledger, monkeypatch, calls, on_get=lambda: None):
    """Actual append-only logger; only urllib responses are synthetic/offline."""
    def urlopen(req, timeout):
        assert req.get_method() == 'GET'
        calls.append(req.full_url)
        on_get()
        if req.full_url == fc.API + 'team/credit-usage':
            payload = {'success': True, 'data': {'planCredits': 100000, 'remainingCredits': 54038,
                'billingPeriodStart': '2026-09-19T23:59:59Z', 'billingPeriodEnd': '2026-10-19T23:59:59Z'}}
        else:
            assert req.full_url == fc.API + 'team/credit-usage/historical'
            payload = {'success': True, 'periods': [{'startDate': '2026-10-01T00:00:00Z',
                'endDate': None, 'creditsUsed': 13}]}
        response = io.BytesIO(json.dumps(payload).encode())
        response.status = 200
        return response
    monkeypatch.setattr(fc.urllib.request, 'urlopen', urlopen)
    return fc.FirecrawlClient('sponsor_full_reconciliation',
        api_key='synthetic-offline-not-a-secret', ledger_path=ledger)


def test_documented_plan_apply_with_actual_shared_ledger_client(tmp_path, monkeypatch):
    # Retained independent RV521-522 regression: real client supplied the SAME ledger.
    ledger, state, original, _ = fixture(tmp_path)
    original['outcome'] = 'ambiguous_transport_error'
    ledger.write_text(json.dumps(original) + '\n')
    before = ledger.read_bytes()
    reservations = (state / 'firecrawl-supply-budget.json').read_bytes()
    calls = []
    client = real_client(ledger, monkeypatch, calls)
    plan = booking.book(state, ledger, client, tmp_path / 'plan.json')
    assert plan['pre_ledger_sha256'] == hashlib.sha256(ledger.read_bytes()).hexdigest()
    assert len(calls) == 2
    result = booking.book(state, ledger, client, tmp_path / 'apply.json', apply=True,
                          expected_sha=plan['pre_ledger_sha256'])
    assert result['status'] == 'applied' and result['unknown_remaining'] == 0
    assert len(calls) == 4 and ledger.read_bytes().startswith(before)
    latest = {r['operation_id']: r for r in map(json.loads, ledger.read_text().splitlines())}
    assert latest[original['operation_id']]['creditsUsed'] == 13
    assert latest[original['operation_id']]['outcome'] == original['outcome']
    assert (state / 'firecrawl-supply-budget.json').read_bytes() == reservations
    assert client.ledger_path == ledger  # Never mutate the caller's client.
    for receipt in (tmp_path / 'plan.json', tmp_path / 'apply.json'):
        audit = Path(str(receipt) + '.usage.jsonl')
        bound = json.loads(receipt.read_text())['usage_audit']
        assert bound == {'path': str(audit.resolve()), 'sha256': hashlib.sha256(audit.read_bytes()).hexdigest()}
        assert audit.stat().st_mode & 0o777 == 0o600
        rows = [json.loads(line) for line in audit.read_text().splitlines()]
        assert len(rows) == 4
        assert [r['outcome'] for r in rows] == ['started', 'success', 'started', 'success']
        assert {r['endpoint'] for r in rows} == {'/v2/team/credit-usage', '/v2/team/credit-usage/historical'}
    producer = supply.Producer(state_dir=state, import_only=True)
    producer.operations = lambda: list(map(json.loads, ledger.read_text().splitlines()))
    with pytest.raises(RuntimeError, match='ambiguous_capture_effect_no_replay'):
        producer.capture_admissible('scrape', 'target')


def test_exact_round1_real_client_plan_apply_preserves_null_receipt(tmp_path, monkeypatch):
    ledger, state, row, _ = fixture(tmp_path)
    row.update(operation_id='224ef323-f3b6-4780-8553-fbf191c3c598',
               at_utc='2026-10-15T03:53:23.394211+00:00',
               http_status=408, outcome='http_error', run='sponsor-supply-20261015T0343')
    ledger.write_text(json.dumps(row) + '\n')
    before = ledger.read_bytes()
    calls = []
    client = real_client(ledger, monkeypatch, calls)
    kw = {'exact_operation_decision_sha': booking.EXACT_OPERATION_DECISION_SHA256}
    plan = booking.book(state, ledger, client, tmp_path / 'plan.json', **kw)
    result = booking.book(state, ledger, client, tmp_path / 'apply.json', apply=True,
                          expected_sha=plan['pre_ledger_sha256'], **kw)
    assert result['status'] == 'applied' and result['unknown_remaining'] == 0
    assert ledger.read_bytes().startswith(before) and len(calls) == 4
    latest = json.loads(ledger.read_text().splitlines()[-1])
    assert latest['creditsUsed'] == 13 and latest['http_status'] == 408
    assert latest['outcome'] == 'http_error'
    assert result['exact_operation_decision_sha256'] == booking.EXACT_OPERATION_DECISION_SHA256
    assert all('/team/credit-usage' in url for url in calls)
    with pytest.raises(RuntimeError, match='ambiguous_capture_effect_no_replay'):
        producer = supply.Producer(state_dir=state, import_only=True)
        producer.operations = lambda: list(map(json.loads, ledger.read_text().splitlines()))
        producer.capture_admissible('scrape', 'target')


def test_exact_round1_mixed_ledger_books_only_approved_operation(tmp_path, monkeypatch):
    # RV537's full-ledger failing probe, with synthetic equivalents of its
    # 640 existing bookings and 16 unrelated post-cutoff null map operations.
    # Unlike the review projection, retain every unrelated row in plan/apply.
    ledger, state, original, _ = fixture(tmp_path)
    exact = dict(original, **booking.EXACT_OPERATION)
    prior_bookings = [dict(original, operation_id=f'booked-{i}', creditsUsed=13,
                           cost_booking={'reason': 'retained historical booking'})
                      for i in range(640)]
    unrelated = [dict(original, operation_id=f'127c41f3-unapproved-map-{i}',
                      endpoint='/v2/map', at_utc='2026-10-15T03:46:39.365573+00:00',
                      outcome='success', http_status=200)
                 for i in range(16)]
    # A still-unbooked historical null is also outside this supplemental run.
    rows = prior_bookings + unrelated + [original, exact]
    before = b''.join(booking.encoded(row) for row in rows)
    ledger.write_bytes(before)
    reservation = (state / 'firecrawl-supply-budget.json').read_bytes()
    calls = []
    client = real_client(ledger, monkeypatch, calls)
    kw = {'exact_operation_decision_sha': booking.EXACT_OPERATION_DECISION_SHA256}
    plan = booking.book(state, ledger, client, tmp_path / 'plan.json', **kw)
    assert plan['booked_operations'] == 1
    assert [c['operation_id'] for c in plan['charges']] == [exact['operation_id']]
    assert ledger.read_bytes() == before
    result = booking.book(state, ledger, client, tmp_path / 'apply.json', apply=True,
                          expected_sha=plan['pre_ledger_sha256'], **kw)
    after = ledger.read_bytes()
    assert after.startswith(before)
    additions = list(map(json.loads, after[len(before):].splitlines()))
    assert len(additions) == 1
    added = additions[0]
    assert added['operation_id'] == exact['operation_id'] and added['creditsUsed'] == 13
    assert added['http_status'] == 408 and added['outcome'] == 'http_error'
    assert result['status'] == 'applied' and result['unknown_remaining'] == 0  # scoped
    latest = {r['operation_id']: r for r in map(json.loads, after.splitlines())}
    assert [r for r in map(json.loads, after.splitlines()) if r.get('cost_booking')][:-1] == prior_bookings
    assert all(latest[r['operation_id']] == r for r in unrelated + [original])
    assert len([r for r in latest.values() if r['creditsUsed'] is None]) == 17
    assert (state / 'firecrawl-supply-budget.json').read_bytes() == reservation
    assert len(calls) == 4 and all('/team/credit-usage' in url for url in calls)
    repeated = booking.book(state, ledger, client, tmp_path / 'repeat.json', **kw)
    assert repeated['booked_operations'] == 0 and ledger.read_bytes() == after
    # A later unapproved ambiguous scrape is held, not booked by this decision.
    future = dict(exact, operation_id='new-unapproved-null')
    ledger.write_bytes(after + booking.encoded(future))
    retained = ledger.read_bytes()
    no_op = booking.book(state, ledger, client, tmp_path / 'future.json', apply=True,
                         expected_sha=hashlib.sha256(retained).hexdigest(), **kw)
    assert no_op['booked_operations'] == 0 and ledger.read_bytes() == retained
    producer = supply.Producer(state_dir=state, import_only=True)
    producer.operations = lambda: list(map(json.loads, ledger.read_text().splitlines()))
    with pytest.raises(RuntimeError, match='sponsor_ledger_credit_cost_unknown'):
        producer.daily_accounted('2026-10-15')
    with pytest.raises(RuntimeError, match='ambiguous_capture_effect_no_replay'):
        producer.capture_admissible('scrape', 'target')
    assert (state / 'firecrawl-supply-budget.json').read_bytes() == reservation


def test_exact_round1_cli_rejects_unapproved_decision_before_client(tmp_path, monkeypatch):
    old, exact = tmp_path / 'old.md', tmp_path / 'round1.md'
    old.write_bytes(b'old approved decision fixture')
    exact.write_bytes(b'unapproved extension')
    monkeypatch.setattr(booking, 'DECISION_SHA256', hashlib.sha256(old.read_bytes()).hexdigest())
    monkeypatch.setattr(sys, 'argv', ['booking', '--decision', str(old),
        '--exact-operation-decision', str(exact), '--receipt', str(tmp_path / 'receipt.json')])
    monkeypatch.setattr(booking, 'FirecrawlClient', lambda *a, **k: pytest.fail('client before authority'))
    with pytest.raises(SystemExit) as rejected:
        booking.main()
    assert rejected.value.code == 2
    assert not (tmp_path / 'receipt.json').exists()


@pytest.mark.parametrize('when', ['between_plan_apply', 'during_apply_get'])
@pytest.mark.parametrize('endpoint', ['/v2/scrape', '/v2/team/credit-usage'])
def test_real_client_still_refuses_unrelated_ledger_writes(tmp_path, monkeypatch, when, endpoint):
    ledger, state, _, _ = fixture(tmp_path)
    calls = []
    def foreign_write():
        fc.record_operation('another_owner', endpoint, outcome='started',
                            operation_id='unrelated', ledger_path=ledger)
    def on_get():
        if when == 'during_apply_get' and len(calls) == 3:
            foreign_write()
    client = real_client(ledger, monkeypatch, calls, on_get)
    plan = booking.book(state, ledger, client, tmp_path / 'plan.json')
    if when == 'between_plan_apply':
        foreign_write()
    with pytest.raises(RuntimeError, match='ledger_changed_replan_required'):
        booking.book(state, ledger, client, tmp_path / 'apply.json', apply=True,
                     expected_sha=plan['pre_ledger_sha256'])
    assert not (tmp_path / 'apply.json').exists()
    rows = list(map(json.loads, ledger.read_text().splitlines()))
    assert all('cost_booking' not in row for row in rows)
    assert any(row['operation_id'] == 'unrelated' for row in rows)


@pytest.mark.parametrize('symlink', [False, True])
def test_existing_usage_audit_refuses_before_api(tmp_path, monkeypatch, symlink):
    ledger, state, _, _ = fixture(tmp_path)
    before = ledger.read_bytes()
    receipt = tmp_path / 'plan.json'
    audit = Path(str(receipt) + '.usage.jsonl')
    if symlink:
        audit.symlink_to(ledger)
    else:
        audit.write_bytes(b'retained failed attempt\n')
    retained = audit.read_bytes()
    calls = []
    client = real_client(ledger, monkeypatch, calls)
    with pytest.raises(FileExistsError):
        booking.book(state, ledger, client, receipt)
    assert calls == [] and ledger.read_bytes() == before
    assert audit.read_bytes() == retained and not receipt.exists()
