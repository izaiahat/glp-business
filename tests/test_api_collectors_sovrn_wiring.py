"""Offline scheduled Sovrn bounds, three-period coverage and source custody."""
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import types

import affiliate_network_access as access
from glp_affiliate_weekly_reporting import run
from marketplace_adapters import _base

ROOT = Path(__file__).resolve().parents[1]
API = 'https://viglink.io/v1/reports/transactions'


def test_scheduler_refuses_sovrn_before_credential_or_transport_read(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError('no credential or provider read within short runner budget')
    monkeypatch.setattr(_base, 'secret_text', forbidden)
    transport = types.SimpleNamespace(request=forbidden)
    got = run(tmp_path/'report', {'marketplace_count':1,'marketplaces':[{'marketplace':'sovrn'}]}, [],
              as_of=datetime(2026,10,2,tzinfo=timezone.utc),
              collectors=access.weekly_collectors(['sovrn'], transport))
    assert len(got['observations']) == 3
    weekly, lookback, month = got['observations']
    assert weekly['reason'] == 'RUNNER_BUDGET_INSUFFICIENT'
    assert [r['reason'] for r in (lookback, month)] == ['WEEKLY_ONLY_RATE_LIMITED']*2
    assert [r['period_purpose'] for r in got['observations']] == ['closed_week','lookback','closed_month']
    assert all(r['coverage'] == 'UNKNOWN' and r['reporting_timezone'] == 'UNKNOWN' for r in got['observations'])
    assert all(r['record_count'] is None and not r['payload_complete'] for r in got['observations'])
    assert got['fleet_commission_minor'] is None and got['fleet_cash_minor'] is None
    assert len(list((tmp_path/'report').glob('sovrn-page-*.json'))) == 3
    assert json.loads((tmp_path/'report'/'RESULT.json').read_text())['observations'] == json.loads(json.dumps(got['observations']))


def test_sovrn_auth_failure_retains_no_read_wider_periods(monkeypatch, tmp_path):
    module = types.ModuleType('affiliate_reporting_sovrn')
    calls = []
    def collect(transport, **kwargs):
        calls.append(kwargs)
        return access.AccessResult('sovrn','conversions_earnings','SUPPORTED',API,
            receipts=({'method':'GET','endpoint':API,'http_status':401},),
            reason='HTTP_ERROR_NO_RETRY')
    module.collect = collect
    monkeypatch.setitem(sys.modules, module.__name__, module)
    got = run(tmp_path/'report', {'marketplace_count':1,'marketplaces':[{'marketplace':'sovrn'}]}, [],
        as_of=datetime(2026,10,2,tzinfo=timezone.utc), lookback_days=7,
        collectors=access.weekly_collectors(['sovrn'], object()))
    assert len(calls) == 1
    assert len(got['observations']) == 3
    assert got['observations'][0]['reason'] == 'HTTP_ERROR_NO_RETRY'
    assert [r['reason'] for r in got['observations'][1:]] == ['WEEKLY_ONLY_RATE_LIMITED']*2


def test_sovrn_runner_limits_are_unchanged():
    wrapper=(ROOT/'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting.sh').read_text()
    runner=(ROOT/'scripts/glp_affiliate_weekly_reporting.py').read_text()
    job=(ROOT/'scripts/glp_affiliate_weekly_reporting_job.py').read_text()
    assert '--lookback-days 90 --max-requests 32 --budget-seconds 240' in wrapper
    assert "p.add_argument('--budget-seconds',type=int,default=240)" in runner
    assert 'signal.alarm(args.budget_seconds+5)' in runner
    assert "parser.add_argument('--budget-seconds',type=int,default=240)" in job


def test_sovrn_moving_clock_cannot_turn_short_sleep_into_negative_delay(monkeypatch, tmp_path):
    import time
    import affiliate_reporting_sovrn as sovrn
    from test_api_collectors_sovrn import Clock, Transport, week_pages, START, END
    clock = Clock()
    monkeypatch.setattr(sovrn, 'LIMITER_STATE_PATH', tmp_path/'limiter.json')
    monkeypatch.setattr(sovrn, '_boot_id', lambda: 'synthetic-boot')
    def advancing_monotonic():
        now = clock.now
        clock.now += 1
        return now
    def short_sleep(seconds):
        assert seconds >= 0
        clock.now += max(0, seconds-1.5)
    monkeypatch.setattr(time, 'monotonic', advancing_monotonic)
    monkeypatch.setattr(time, 'sleep', short_sleep)
    monkeypatch.setattr(_base, 'secret_text', lambda *parts: 'synthetic-only')
    transport = Transport(clock, week_pages(empty=True))
    got = sovrn.collect(transport, start=START, end=END)
    assert len(transport.calls) == 7
    assert got.coverage == 'PARTIAL' and got.record_count is None
    assert all(b[0]-a[0] >= 61 for a,b in zip(transport.calls, transport.calls[1:]))


def test_sovrn_blocked_alarm_signal_refuses_provider_io(monkeypatch, tmp_path):
    import signal
    import affiliate_reporting_sovrn as sovrn
    from test_api_collectors_sovrn import Clock, Transport, START, END
    clock = Clock()
    monkeypatch.setattr(sovrn.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(sovrn, 'LIMITER_STATE_PATH', tmp_path/'limiter.json')
    monkeypatch.setattr(sovrn, '_boot_id', lambda: 'synthetic-boot')
    monkeypatch.setattr(_base, 'secret_text', lambda *parts: 'synthetic-only')
    monkeypatch.setattr(signal, 'pthread_sigmask', lambda *args: {signal.SIGALRM})
    transport = Transport(clock, [])
    got = sovrn.collect(transport, start=START, end=END)
    assert got.reason == 'WALL_TIME_PROTECTION_UNAVAILABLE'
    assert got.coverage == 'UNKNOWN' and not transport.calls
    assert not (tmp_path/'limiter.json').exists()


def test_sovrn_wall_deadline_restores_handler_and_retains_uncertain_read(monkeypatch, tmp_path):
    import signal
    import affiliate_reporting_sovrn as sovrn
    from test_api_collectors_sovrn import Clock, START, END
    clock = Clock()
    monkeypatch.setattr(sovrn.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(sovrn, 'LIMITER_STATE_PATH', tmp_path/'limiter.json')
    monkeypatch.setattr(sovrn, '_boot_id', lambda: 'synthetic-boot')
    monkeypatch.setattr(_base, 'secret_text', lambda *parts: 'synthetic-only')
    previous_handler = signal.getsignal(signal.SIGALRM)
    class DeadlineTransport:
        calls = 0
        def request(self, *args, **kwargs):
            self.calls += 1
            remaining, interval = signal.getitimer(signal.ITIMER_REAL)
            assert 0 < remaining <= 600 and interval == 0
            signal.getsignal(signal.SIGALRM)(signal.SIGALRM, None)
    transport = DeadlineTransport()
    got = sovrn.collect(transport, start=START, end=END)
    assert got.coverage == 'UNKNOWN' and transport.calls == 1
    assert json.loads((tmp_path/'limiter.json').read_text())['in_flight'] is True
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)
    assert signal.getsignal(signal.SIGALRM) == previous_handler
