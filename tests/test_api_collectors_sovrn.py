"""Offline Sovrn reporting checks; seven daily reads never prove reconciliation."""
import copy
import fcntl
import importlib
import json
import os
import signal
import sys
import threading
import time
import types
from datetime import datetime, timedelta

import pytest

import affiliate_network_access as access
from marketplace_adapters import _base

API = 'https://viglink.io/v1/reports/transactions'
START = '2026-09-21T00:00:00+00:00'
END = '2026-09-28T00:00:00+00:00'
SECRET = 'synthetic-sovrn-secret-never-persist'


class Clock:
    def __init__(self, undershoot=False):
        self.now = 1000.0
        self.sleeps = []
        self.undershoot = undershoot

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        if self.undershoot:
            self.now += seconds / 2
            self.undershoot = False
        else:
            self.now += seconds


class Transport:
    def __init__(self, clock, documents, *, statuses=None, duration=0, error=None):
        self.clock = clock
        self.documents = list(documents)
        self.statuses = list(statuses) if statuses is not None else None
        self.duration = duration
        self.error = error
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((self.clock.monotonic(), method, url, kwargs))
        self.clock.now += self.duration
        if self.error is not None:
            raise self.error
        assert self.documents, 'unexpected request'
        document = self.documents.pop(0)
        status = self.statuses.pop(0) if self.statuses is not None else 200
        body = document if isinstance(document, bytes) else json.dumps(document).encode()
        return _base.Response(status, body, {'Authorization': SECRET},
                              url='https://sensitive-response-url.invalid/customer')


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(time, 'sleep', clock.sleep)
    return clock


@pytest.fixture(autouse=True)
def synthetic_secrets(monkeypatch):
    monkeypatch.setattr(_base, 'secret_text', lambda *parts: SECRET)


@pytest.fixture(autouse=True)
def isolated_limiter_state(monkeypatch, tmp_path):
    path = tmp_path / 'sovrn-rate-limit.json'
    if importlib.util.find_spec('affiliate_reporting_sovrn') is not None:
        module = collector()
        monkeypatch.setattr(module, 'LIMITER_STATE_PATH', path, raising=False)
        monkeypatch.setattr(module, '_boot_id', lambda: 'synthetic-boot', raising=False)
    return path


def collector():
    return importlib.import_module('affiliate_reporting_sovrn')


def row(day='2026-09-21', *, revenue='r1', commission='c1', amount=-1.25):
    return {
        'account': {'accountId': 123, 'campaignId': 1, 'campaignName': 'safe'},
        'commission': {'revenueId': revenue, 'commissionId': commission,
                       'commissionDate': day, 'updateDate': '2026-10-01',
                       'publisherNetRevenue': amount, 'programType': 'CPA'},
        'merchant': {'merchantGroupId': 5, 'merchantGroupName': 'safe'},
    }


def page(rows=()):
    return {'transactions': list(rows)}


def week_pages(*, empty=False):
    first = datetime.fromisoformat(START)
    return [page() if empty else page([row((first + timedelta(days=i)).date().isoformat(),
                                          revenue='r' + str(i), commission='c' + str(i))])
            for i in range(7)]


def assert_unknown_totals(got):
    assert got.record_count is None
    assert got.commission_minor is None
    assert got.cash_minor is None
    assert not got.report_complete_period


def assert_safe(got):
    saved = json.dumps(got.to_dict())
    assert SECRET not in saved
    assert 'Authorization' not in saved
    assert 'sensitive-response-url' not in saved
    assert 'private-customer' not in saved
    assert 'private-error' not in saved


def test_sovrn_dispatch_through_access_contract(monkeypatch):
    module = types.ModuleType('affiliate_reporting_sovrn')
    expected = access.AccessResult('sovrn', 'conversions_earnings', reason='SYNTHETIC_COLLECTOR_REACHED')
    calls = []

    def collect(transport, **kwargs):
        calls.append(kwargs)
        return expected

    module.collect = collect
    monkeypatch.setitem(sys.modules, module.__name__, module)
    got = access.conversions_earnings('sovrn', object(), start=START, end=END,
                                     deadline=9000, max_requests=7)
    assert got is expected
    assert calls == [{'start': START, 'end': END, 'deadline': 9000, 'max_requests': 7}]


def test_sovrn_exact_read_route_reaches_injected_transport(clock):
    transport = Transport(clock, [page()])
    wrapped = access.ReadTransport(transport, deadline=1600)
    assert wrapped.request('GET', API).status == 200
    assert len(transport.calls) == 1
    assert wrapped.receipts[0]['endpoint'] == API


@pytest.mark.parametrize('method,url', [
    ('POST', API), ('GET', API + '/extra'), ('GET', API + '?commissionDate=2026-09-21'),
    ('GET', 'http://viglink.io/v1/reports/transactions'),
    ('GET', 'https://viglink.io:443/v1/reports/transactions'),
    ('GET', 'https://viglink.io/v1/reports/clicks'),
])
def test_sovrn_route_allowlist_stays_exact(clock, method, url):
    transport = Transport(clock, [])
    with pytest.raises(ValueError, match='route_not_read_allowlisted'):
        access.ReadTransport(transport).request(method, url)
    assert not transport.calls


@pytest.mark.parametrize('undershoot,duration', [(False, 0), (True, 0), (False, 30)])
def test_sovrn_seven_reads_respect_spacing_and_local_budget(clock, undershoot, duration, monkeypatch):
    clock.undershoot = undershoot
    key_paths = []
    monkeypatch.setattr(_base, 'secret_text', lambda *parts: key_paths.append(parts) or SECRET)
    transport = Transport(clock, week_pages(), duration=duration)
    got = collector().collect(transport, start=START, end=END)
    assert collector().COLLECTOR_BUDGET_SECONDS == 600
    assert collector().REQUEST_SPACING_SECONDS == 61
    assert collector().API == API
    assert key_paths == [('affiliate-marketplaces', 'sovrn', 'secret_key')]
    assert len(transport.calls) == len(got.receipts) == len(got.records) == 7
    assert got.coverage == 'PARTIAL'
    assert all(right[0] - left[0] >= 61
               for left, right in zip(transport.calls, transport.calls[1:]))
    assert clock.monotonic() <= 1600
    for i, (_, method, url, kwargs) in enumerate(transport.calls):
        assert method == 'GET' and url == API
        assert kwargs['params'] == {'commissionDate': '2026-09-' + str(21 + i)}
        assert kwargs['headers'] == {'Authorization': 'secret ' + SECRET}
        assert 0 < kwargs['timeout'] <= 30
    assert_unknown_totals(got)
    assert_safe(got)


@pytest.mark.parametrize('start,end', [
    ('2026-09-01T00:00:00Z', '2026-10-01T00:00:00Z'),
    ('2026-07-01T00:00:00Z', '2026-09-29T00:00:00Z'),
    (START, '2026-09-22T00:00:00Z'),
    (START, '2026-09-29T00:00:00Z'),
    ('2026-09-21T01:00:00Z', '2026-09-28T01:00:00Z'),
    ('2100-09-21T00:00:00Z', '2100-09-28T00:00:00Z'),
])
def test_sovrn_nonweekly_or_unclosed_windows_never_read_key(clock, monkeypatch, start, end):
    def forbidden(*parts):
        pytest.fail('credential read before weekly-only window preflight')
    monkeypatch.setattr(_base, 'secret_text', forbidden)
    transport = Transport(clock, [])
    got = collector().collect(transport, start=start, end=end)
    assert got.coverage == 'UNKNOWN'
    assert got.reason == 'WEEKLY_ONLY_RATE_LIMITED'
    assert not transport.calls and not clock.sleeps
    assert_unknown_totals(got)


def test_sovrn_offset_window_normalizes_to_utc_dates(clock):
    transport = Transport(clock, week_pages(empty=True))
    got = collector().collect(transport, start='2026-09-21T02:00:00+02:00',
                              end='2026-09-28T02:00:00+02:00')
    assert len(transport.calls) == 7
    assert transport.calls[0][3]['params'] == {'commissionDate': '2026-09-21'}
    assert transport.calls[-1][3]['params'] == {'commissionDate': '2026-09-27'}
    assert got.coverage == 'PARTIAL'


@pytest.mark.parametrize('deadline,max_requests', [(1240, 32), (999, 32), (1575.99, 32), (1600, 6)])
def test_sovrn_insufficient_preflight_budget_never_reads_key(clock, monkeypatch, deadline, max_requests):
    def forbidden(*parts):
        pytest.fail('credential read before runner/request budget preflight')
    monkeypatch.setattr(_base, 'secret_text', forbidden)
    transport = Transport(clock, [])
    got = collector().collect(transport, start=START, end=END,
                              deadline=deadline, max_requests=max_requests)
    assert got.coverage == 'UNKNOWN'
    assert got.reason == 'RUNNER_BUDGET_INSUFFICIENT'
    assert not transport.calls and not clock.sleeps
    assert_unknown_totals(got)


def test_sovrn_exact_minimum_budget_can_finish(clock):
    transport = Transport(clock, week_pages(empty=True), duration=30)
    got = collector().collect(transport, start=START, end=END, deadline=1576, max_requests=7)
    assert len(transport.calls) == 7
    assert clock.monotonic() <= 1576
    assert got.coverage == 'PARTIAL'
    assert_unknown_totals(got)


def test_sovrn_collector_deadline_does_not_expand_parent_or_local_budget(clock):
    transport = Transport(clock, week_pages(empty=True))
    got = collector().collect(transport, start=START, end=END, deadline=9000)
    assert len(transport.calls) == 7
    assert got.coverage == 'PARTIAL'
    assert clock.monotonic() == 1366
    assert all(call[3]['timeout'] <= 30 for call in transport.calls)


def test_sovrn_local_deadline_expires_during_sleep_without_another_request(clock, monkeypatch):
    def late_wakeup(seconds):
        clock.sleeps.append(seconds)
        clock.now += 601
    monkeypatch.setattr(time, 'sleep', late_wakeup)
    transport = Transport(clock, [page([row()])])
    got = collector().collect(transport, start=START, end=END, deadline=9000)
    assert got.coverage == 'PARTIAL' and len(got.records) == 1
    assert len(transport.calls) == 1
    assert_unknown_totals(got)


@pytest.mark.parametrize('key', ['', '   ', None])
def test_sovrn_missing_key_is_unknown_without_provider_read(clock, monkeypatch, key):
    monkeypatch.setattr(_base, 'secret_text', lambda *parts: key)
    transport = Transport(clock, [])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'UNKNOWN' and got.reason == 'CREDENTIAL_MISSING'
    assert not transport.calls and not clock.sleeps
    assert_unknown_totals(got)


def test_sovrn_secret_read_failure_does_not_leak_error(clock, monkeypatch):
    def missing(*parts):
        raise FileNotFoundError(SECRET + ' private-error')
    monkeypatch.setattr(_base, 'secret_text', missing)
    transport = Transport(clock, [])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'UNKNOWN' and got.reason == 'CREDENTIAL_MISSING'
    assert not transport.calls
    assert_safe(got)


@pytest.mark.parametrize('status,document', [
    (302, {}), (403, {'private-error': SECRET}), (429, {'error': SECRET}),
    (200, {'error': 'private-error ' + SECRET}), (200, b'not-json private-error'),
])
def test_sovrn_first_failure_is_unknown_and_never_retries(clock, status, document):
    transport = Transport(clock, [document], statuses=[status])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'UNKNOWN'
    assert not got.records
    assert len(transport.calls) == 1
    assert_unknown_totals(got)
    assert_safe(got)


@pytest.mark.parametrize('status,document', [(429, {'error': SECRET}), (200, {'error': SECRET}), (200, b'invalid')])
def test_sovrn_later_failure_preserves_prior_observations(clock, status, document):
    transport = Transport(clock, [page([row()]), document], statuses=[200, status])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'PARTIAL'
    assert len(got.records) == 1 and len(transport.calls) == 2
    assert got.records[0]['source_revenue_id'] == 'r1'
    assert_unknown_totals(got)
    assert_safe(got)


def test_sovrn_transport_exception_is_safe_unknown_without_retry(clock, caplog, capsys):
    transport = Transport(clock, [], error=RuntimeError(SECRET + ' private-error'))
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'UNKNOWN' and len(transport.calls) == 1
    assert_unknown_totals(got)
    assert_safe(got)
    captured = capsys.readouterr()
    logs = caplog.text + captured.out + captured.err
    assert SECRET not in logs and 'private-error' not in logs


def test_sovrn_seven_empty_days_cannot_fabricate_zero(clock):
    transport = Transport(clock, week_pages(empty=True))
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'PARTIAL' and not got.records
    assert len(transport.calls) == 7
    assert_unknown_totals(got)


def test_sovrn_nested_example_keeps_negative_adjustments_and_uncertainty(clock):
    transport = Transport(clock, [page([row()])] + [page()] * 6)
    got = collector().collect(transport, start=START, end=END)
    record = got.records[0]
    assert record['reported_amount'] == '-1.25'
    assert record['source_revenue_id'] == 'r1'
    assert record['source_commission_id'] == 'c1'
    assert 'provider_event_id' not in record
    assert record['currency'] is None and record['classification'] == 'UNKNOWN'
    assert got.coverage == 'PARTIAL'
    assert_unknown_totals(got)


@pytest.mark.parametrize('amount', [None, True, False, float('inf'), float('nan'), 'malformed'])
def test_sovrn_unknown_amounts_survive_without_zero(clock, amount):
    transport = Transport(clock, [page([row(amount=amount)])] + [page()] * 6)
    got = collector().collect(transport, start=START, end=END)
    assert len(got.records) == 1
    assert got.records[0]['reported_amount'] is None
    assert_unknown_totals(got)


def test_sovrn_missing_amount_survives_without_zero(clock):
    record = row()
    del record['commission']['publisherNetRevenue']
    transport = Transport(clock, [page([record])] + [page()] * 6)
    got = collector().collect(transport, start=START, end=END)
    assert len(got.records) == 1
    assert got.records[0]['reported_amount'] is None


def test_sovrn_repeated_identifiers_are_not_deduplicated_as_stable_events(clock):
    transport = Transport(clock, [page([row(), row(amount=-2)])] + [page()] * 6)
    got = collector().collect(transport, start=START, end=END)
    assert [r['reported_amount'] for r in got.records] == ['-1.25', '-2']
    assert all(r['source_revenue_id'] == 'r1' for r in got.records)
    assert_unknown_totals(got)


@pytest.mark.parametrize('document', [
    [], {}, {'transactions': {}}, {'transactions': [None]},
    {'transactions': [{'publisherRevenue': -1.25, 'commissionDate': '2026-09-21',
                       'revenueId': 'r1', 'commissionId': 'c1'}]},
    page([{'commission': {'commissionDate': '2026-09-21', 'publisherRevenue': -1.25}}]),
])
def test_sovrn_unknown_or_conflicting_shapes_fail_closed(clock, document):
    transport = Transport(clock, [document])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'PARTIAL' and not got.records
    assert len(transport.calls) == 1
    assert_unknown_totals(got)


@pytest.mark.parametrize('field,value', [
    ('commissionDate', '2026-09-20'), ('commissionDate', '2026-09-22'),
    ('commissionDate', None), ('commissionDate', '2026-09-21T00:00:00Z'),
    ('commissionDate', '2026-02-30'), ('updateDate', 'invalid'),
    ('updateDate', '2026-02-30'),
])
def test_sovrn_invalid_or_unmatched_dates_reject_entire_page(clock, field, value):
    malformed = row()
    malformed['commission'][field] = value
    transport = Transport(clock, [page([row(), malformed])])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'PARTIAL' and not got.records
    assert len(transport.calls) == 1
    assert_unknown_totals(got)


def test_sovrn_missing_commission_date_rejects_entire_page(clock):
    malformed = row()
    del malformed['commission']['commissionDate']
    transport = Transport(clock, [page([row(), malformed])])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'PARTIAL' and not got.records


def test_sovrn_optional_update_date_is_not_invented(clock):
    record = row()
    del record['commission']['updateDate']
    transport = Transport(clock, [page([record])] + [page()] * 6)
    got = collector().collect(transport, start=START, end=END)
    assert len(got.records) == 1
    assert '2026-10-01' not in json.dumps(got.records)


@pytest.mark.parametrize('section,field', [('account', 'campaignName'), ('merchant', 'merchantGroupName')])
def test_sovrn_fella_names_and_sensitive_fields_are_not_projected(clock, section, field):
    forbidden = row()
    forbidden[section][field] = 'Fella Health'
    allowed = copy.deepcopy(row(revenue='allowed'))
    allowed['click'] = {'pageUrl': 'https://private-customer.invalid/page',
                        'linkUrl': 'https://private-customer.invalid/click',
                        'cuid': 'private-customer'}
    allowed['product'] = []
    transport = Transport(clock, [page([forbidden, allowed])] + [page()] * 6)
    got = collector().collect(transport, start=START, end=END)
    assert len(got.records) == 1
    assert got.records[0]['source_revenue_id'] == 'allowed'
    assert got.records[0]['currency'] is None
    assert 'USD' not in json.dumps(got.records)
    assert 'click' not in got.records[0] and 'product' not in got.records[0]
    assert_safe(got)


@pytest.mark.parametrize('section,field,value', [
    (None, 'customer', {'email': 'private-customer'}),
    (None, 'trackingUrl', 'https://private-customer.invalid/click'),
    ('commission', 'currency', 'USD'),
])
def test_sovrn_unreviewed_row_fields_reject_entire_page(clock, section, field, value):
    unreviewed = row()
    target = unreviewed if section is None else unreviewed[section]
    target[field] = value
    transport = Transport(clock, [page([row(), unreviewed])])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'PARTIAL' and not got.records
    assert len(transport.calls) == 1
    assert_unknown_totals(got)
    assert_safe(got)


def limiter_state(**extra):
    return {'schema': 'sovrn-reporting-limiter-v1', 'boot_id': 'synthetic-boot',
            'not_before': 1000.0, 'in_flight': False, **extra}


def write_limiter(path, state):
    path.write_text(json.dumps(state))
    os.chmod(path, 0o600)


def test_sovrn_limiter_durable_state_is_private_and_contains_no_credentials(clock, isolated_limiter_state):
    transport = Transport(clock, week_pages(empty=True))
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'PARTIAL' and len(transport.calls) == 7
    state = json.loads(isolated_limiter_state.read_text())
    assert state == limiter_state(not_before=clock.monotonic() + 61)
    assert isolated_limiter_state.stat().st_mode & 0o777 == 0o600
    assert SECRET not in isolated_limiter_state.read_text()


def test_sovrn_cross_run_cooldown_cannot_expand_local_budget(clock, isolated_limiter_state):
    first = Transport(clock, week_pages(empty=True))
    assert collector().collect(first, start=START, end=END).coverage == 'PARTIAL'
    second = Transport(clock, [])
    got = collector().collect(second, start=START, end=END)
    assert got.coverage == 'UNKNOWN'
    assert got.reason == 'RATE_LIMIT_COOLDOWN_EXCEEDS_BUDGET'
    assert not second.calls
    assert_unknown_totals(got)
    clock.now += 61
    later = Transport(clock, week_pages(empty=True))
    assert collector().collect(later, start=START, end=END).coverage == 'PARTIAL'
    assert len(later.calls) == 7
    assert later.calls[0][0] - first.calls[-1][0] >= 61


def test_sovrn_cross_run_cooldown_within_budget_is_observed(clock, isolated_limiter_state):
    write_limiter(isolated_limiter_state, limiter_state(not_before=1024.0))
    transport = Transport(clock, week_pages(empty=True), duration=30)
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'PARTIAL' and len(transport.calls) == 7
    assert transport.calls[0][0] == 1024.0
    assert clock.monotonic() <= 1600


def test_sovrn_actual_kernel_lock_conflict_fails_before_provider_read(clock, isolated_limiter_state):
    write_limiter(isolated_limiter_state, limiter_state())
    with isolated_limiter_state.open('r+') as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            transport = Transport(clock, [])
            got = collector().collect(transport, start=START, end=END)
        finally:
            fcntl.flock(owner.fileno(), fcntl.LOCK_UN)
    assert got.coverage == 'UNKNOWN'
    assert got.reason == 'CONCURRENCY_PROTECTION_UNAVAILABLE'
    assert not transport.calls
    assert_unknown_totals(got)


def test_sovrn_kernel_lock_is_held_during_every_read_and_spacing_wait(clock, monkeypatch, isolated_limiter_state):
    probes = []

    def assert_held():
        with isolated_limiter_state.open('r+') as competitor:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        probes.append(clock.monotonic())

    def observed_sleep(seconds):
        assert_held()
        clock.sleep(seconds)

    class ObservedTransport(Transport):
        def request(self, method, url, **kwargs):
            assert_held()
            return super().request(method, url, **kwargs)

    monkeypatch.setattr(time, 'sleep', observed_sleep)
    transport = ObservedTransport(clock, week_pages(empty=True))
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'PARTIAL' and len(transport.calls) == 7
    assert len(probes) == 13
    # A normal completed week releases the kernel lock for the next run.
    with isolated_limiter_state.open('r+') as released:
        fcntl.flock(released.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(released.fileno(), fcntl.LOCK_UN)


@pytest.mark.parametrize('state', [
    {}, limiter_state(schema='unknown'), limiter_state(boot_id='another-boot'),
    limiter_state(in_flight=True), limiter_state(in_flight='false'),
    limiter_state(not_before='1000'), limiter_state(not_before=True),
    limiter_state(not_before=float('nan')),
    limiter_state(not_before=float('inf')),
])
def test_sovrn_invalid_or_crash_limiter_state_fails_closed(clock, isolated_limiter_state, state):
    write_limiter(isolated_limiter_state, state)
    transport = Transport(clock, [])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'UNKNOWN'
    assert got.reason == 'CONCURRENCY_PROTECTION_UNAVAILABLE'
    assert not transport.calls
    assert_unknown_totals(got)
    assert_safe(got)


def test_sovrn_corrupt_limiter_state_does_not_leak_its_payload(clock, isolated_limiter_state, caplog, capsys):
    isolated_limiter_state.write_text('invalid-json ' + SECRET + ' private-error')
    os.chmod(isolated_limiter_state, 0o600)
    transport = Transport(clock, [])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'UNKNOWN'
    assert got.reason == 'CONCURRENCY_PROTECTION_UNAVAILABLE'
    assert not transport.calls
    assert_safe(got)
    captured = capsys.readouterr()
    assert SECRET not in caplog.text + captured.out + captured.err


def test_sovrn_unusable_limiter_path_fails_before_provider_read(clock, isolated_limiter_state):
    isolated_limiter_state.mkdir()
    transport = Transport(clock, [])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'UNKNOWN'
    assert got.reason == 'CONCURRENCY_PROTECTION_UNAVAILABLE'
    assert not transport.calls


def test_sovrn_transport_failure_leaves_durable_reservation_and_blocks_next_run(clock, isolated_limiter_state):
    transport = Transport(clock, [], error=RuntimeError(SECRET + ' private-error'))
    first = collector().collect(transport, start=START, end=END)
    assert first.coverage == 'UNKNOWN' and len(transport.calls) == 1
    assert json.loads(isolated_limiter_state.read_text())['in_flight'] is True
    later = Transport(clock, [])
    got = collector().collect(later, start=START, end=END)
    assert got.coverage == 'UNKNOWN'
    assert got.reason == 'CONCURRENCY_PROTECTION_UNAVAILABLE'
    assert not later.calls
    assert_safe(got)


def test_sovrn_http_failure_still_persists_completed_request_cooldown(clock, isolated_limiter_state):
    transport = Transport(clock, [{}], statuses=[429])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'UNKNOWN' and len(transport.calls) == 1
    assert json.loads(isolated_limiter_state.read_text()) == limiter_state(not_before=1061.0)


def test_sovrn_existing_wall_timer_is_not_hijacked(clock, monkeypatch, isolated_limiter_state):
    monkeypatch.setattr(signal, 'getitimer', lambda timer: (1.0, 0.0))
    transport = Transport(clock, [])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'UNKNOWN'
    assert got.reason == 'WALL_TIME_PROTECTION_UNAVAILABLE'
    assert not transport.calls
    assert_unknown_totals(got)


def test_sovrn_wall_timer_setup_failure_is_unknown_before_provider_read(clock, monkeypatch):
    def unavailable(*args):
        raise OSError('private-error ' + SECRET)
    monkeypatch.setattr(signal, 'setitimer', unavailable)
    transport = Transport(clock, [])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'UNKNOWN'
    assert got.reason == 'WALL_TIME_PROTECTION_UNAVAILABLE'
    assert not transport.calls
    assert_safe(got)


def test_sovrn_non_main_thread_cannot_issue_without_wall_protection(clock, monkeypatch):
    monkeypatch.setattr(threading, 'current_thread', lambda: object())
    transport = Transport(clock, [])
    got = collector().collect(transport, start=START, end=END)
    assert got.coverage == 'UNKNOWN'
    assert got.reason == 'WALL_TIME_PROTECTION_UNAVAILABLE'
    assert not transport.calls
    assert_unknown_totals(got)


def test_sovrn_short_runner_budget_does_not_touch_lock_timer_or_credentials(clock, monkeypatch, isolated_limiter_state):
    def forbidden(*args):
        pytest.fail('short runner must fail before credential or timer access')
    monkeypatch.setattr(_base, 'secret_text', forbidden)
    monkeypatch.setattr(signal, 'getitimer', forbidden)
    monkeypatch.setattr(signal, 'setitimer', forbidden)
    transport = Transport(clock, [])
    got = collector().collect(transport, start=START, end=END, deadline=1240)
    assert got.coverage == 'UNKNOWN' and got.reason == 'RUNNER_BUDGET_INSUFFICIENT'
    assert not transport.calls
    assert not isolated_limiter_state.exists()
