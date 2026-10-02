"""Bounded weekly commission-day observations, never reconciled Sovrn totals.

Official contract: https://developer.sovrn.com/reference/get_reports-transactions
and /docs/authorization. The example and flat OpenAPI revenue schemas disagree;
only the nested example is retained, with all financial completeness unproved.
See ops/glp/reporting-v2/SOVRN.md for the runner's 240s execution conflict.
"""
from datetime import date, datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import stat
import threading
import time

from affiliate_network_access import AccessResult, ReadTransport, ProviderResponseError, _window
from affiliate_reporting_partnerstack import _amount, _identifier
from marketplace_adapters import _base

API = 'https://viglink.io/v1/reports/transactions'
COLLECTOR_BUDGET_SECONDS = 600
REQUEST_SPACING_SECONDS = 61
REQUEST_TIMEOUT_SECONDS = 30
MINIMUM_WEEK_BUDGET_SECONDS = 6 * REQUEST_SPACING_SECONDS + 7 * REQUEST_TIMEOUT_SECONDS
BINDING = 'CONFIGURED_PER_SITE_SECRET_NOT_RESPONSE_VERIFIED_SITE_OR_ACCOUNT'
LIMITER_STATE_PATH = (_base.BUSINESS / 'reports/glp-clinic-scout/affiliate-machine/'
                     'weekly-reporting/.sovrn-reporting-rate-limit.json')


class ProtectionUnavailable(Exception):
    """Fixed reason codes only; no error or private state is serialized."""


def _boot_id():
    # Monotonic timestamps are comparable across local processes within a boot.
    # A changed/unavailable boot identity requires reviewed recovery, not reset.
    import re
    value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not re.fullmatch(r'[0-9a-f-]{36}', value):
        raise ValueError('boot_identity_unavailable')
    return value


class WallBudget:
    """Main-thread process deadline; never replace another owner's alarm."""
    def __init__(self, deadline):
        self.deadline = deadline

    def __enter__(self):
        changed = False
        try:
            if threading.current_thread() is not threading.main_thread():
                raise ValueError('main_thread_required')
            if any(signal.getitimer(signal.ITIMER_REAL)):
                raise ValueError('existing_alarm_owned_elsewhere')
            if signal.SIGALRM in signal.pthread_sigmask(signal.SIG_BLOCK, []):
                raise ValueError('wall_alarm_signal_blocked')
            remaining = self.deadline-time.monotonic()
            if remaining <= 0:
                raise ValueError('deadline_elapsed')
            self.previous_handler = signal.getsignal(signal.SIGALRM)
            def expired(signum, frame):
                raise TimeoutError('sovrn_collector_wall_deadline')
            signal.signal(signal.SIGALRM, expired)
            changed = True
            signal.setitimer(signal.ITIMER_REAL, remaining)
            return self
        except Exception:
            if changed:
                signal.signal(signal.SIGALRM, self.previous_handler)
            raise ProtectionUnavailable('WALL_TIME_PROTECTION_UNAVAILABLE') from None

    def __exit__(self, *args):
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        finally:
            signal.signal(signal.SIGALRM, self.previous_handler)


class SharedReportingLimiter:
    """Network-wide conservative gate shared across runs and all site keys.

    The same private inode is locked for the whole week. A durable in-flight
    marker blocks reuse after an uncertain read/crash. Never unlink or replace
    this file to recover a lock; failed/corrupt/old-boot state needs review.
    """
    def __enter__(self):
        self.handle = None
        fd = None
        try:
            boot = _boot_id()
            path = Path(LIMITER_STATE_PATH)
            self.path = path
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                fd = os.open(path, flags)
                created = False
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise ValueError('private_regular_state_required')
            self.handle = os.fdopen(fd, 'r+', encoding='utf-8')
            fd = None
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if created:
                self.state = {'schema':'sovrn-reporting-limiter-v1', 'boot_id':boot,
                              'not_before':time.monotonic(), 'in_flight':False}
                self._save()
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            else:
                raw = self.handle.read(4097)
                if len(raw) > 4096:
                    raise ValueError('state_size_unproven')
                self.state = json.loads(raw)
            state = self.state
            if (not isinstance(state, dict) or set(state) != {'schema','boot_id','not_before','in_flight'}
                    or state['schema'] != 'sovrn-reporting-limiter-v1' or state['boot_id'] != boot
                    or type(state['not_before']) not in (int, float)
                    or not math.isfinite(state['not_before']) or state['not_before'] < 0
                    or type(state['in_flight']) is not bool or state['in_flight']):
                raise ValueError('state_or_uncertain_read_requires_review')
            return self
        except Exception:
            if self.handle is not None:
                self.handle.close()
            elif fd is not None:
                os.close(fd)
            raise ProtectionUnavailable('CONCURRENCY_PROTECTION_UNAVAILABLE') from None

    def _save(self):
        # Keep the locked inode: atomic replacement would bypass its flock.
        self.handle.seek(0)
        self.handle.truncate()
        json.dump(self.state, self.handle, allow_nan=False, sort_keys=True)
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def reserve(self):
        linked = os.stat(self.path, follow_symlinks=False)
        locked = os.fstat(self.handle.fileno())
        if (linked.st_dev, linked.st_ino, linked.st_nlink) != (locked.st_dev, locked.st_ino, 1):
            raise ProtectionUnavailable('CONCURRENCY_PROTECTION_UNAVAILABLE')
        if time.monotonic() < self.state['not_before']:
            raise ValueError('cooldown_not_elapsed')
        self.state['in_flight'] = True
        self._save()

    def response_completed(self):
        self.state.update(in_flight=False, not_before=time.monotonic()+REQUEST_SPACING_SECONDS)
        self._save()

    def __exit__(self, *args):
        self.handle.close()  # releases flock; no deletion or state reset


def _result(reason, *, coverage='UNKNOWN', records=(), receipts=()):
    return AccessResult('sovrn', 'conversions_earnings', 'SUPPORTED', API,
        coverage=coverage, records=tuple(records), receipts=tuple(receipts),
        account_binding=BINDING, reason=reason)


def _headers():
    key = _base.secret_text('affiliate-marketplaces', 'sovrn', 'secret_key')
    if not isinstance(key, str) or not key.strip():
        raise FileNotFoundError('credential_missing')
    return {'Authorization': 'secret ' + key.strip()}


def _calendar_date(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError('date_shape_unproven')
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('date_shape_unproven')
    return value


def _project_page(document, day):
    # Unknown root metadata could be truncation/pagination: never ignore it.
    if (not isinstance(document, dict) or set(document) != {'transactions'}
            or not isinstance(document['transactions'], list)):
        raise ValueError('response_shape_unproven')
    projected = []
    for row in document['transactions']:
        if (not isinstance(row, dict) or not set(row) <= {'account', 'commission', 'click', 'merchant', 'product'}
                or not isinstance(row.get('commission'), dict)
                or not isinstance(row.get('account'), dict)
                or not isinstance(row.get('merchant'), dict)):
            raise ValueError('nested_example_shape_unproven')
        commission = row['commission']
        if not set(commission) <= {'revenueId', 'commissionId', 'commissionDate', 'updateDate',
                                  'orderValue', 'publisherNetRevenue', 'programType'}:
            raise ValueError('revenue_schema_conflict_or_unknown')
        if _calendar_date(commission.get('commissionDate')) != day:
            raise ValueError('commission_day_mismatch')
        updated = commission.get('updateDate')
        if updated is not None:
            updated = _calendar_date(updated)
        account, merchant = row['account'], row['merchant']
        if any('fella' in str(value).casefold() for value in (
                account.get('campaignName', ''), merchant.get('merchantGroupName', ''))):
            continue
        projected.append({
            'source_revenue_id': _identifier(commission.get('revenueId')),
            'source_commission_id': _identifier(commission.get('commissionId')),
            'source_campaign_id': _identifier(account.get('campaignId')),
            'source_account_id': _identifier(account.get('accountId')),
            'source_merchant_group_id': _identifier(merchant.get('merchantGroupId')),
            'source_commission_date': day, 'source_update_date': updated,
            'source_timezone': 'UNKNOWN', 'source_status': 'unknown',
            'reported_amount': _amount(commission.get('publisherNetRevenue')),
            'source_amount_field': 'commission.publisherNetRevenue',
            'amount_semantics': 'reported_publisher_net_revenue_not_verified_commission_or_cash',
            'amount_unit': 'UNKNOWN', 'currency': None,
            'identity_revision_semantics': 'UNKNOWN',
            'classification': 'UNKNOWN', 'attribution': 'UNKNOWN'})
    # A bad later row rejects the entire page, including earlier projected rows.
    return projected


def collect(transport, *, start, end, deadline=None, max_requests=32):
    began = time.monotonic()
    local_deadline = began + COLLECTOR_BUDGET_SECONDS
    effective_deadline = min(local_deadline, deadline) if deadline is not None else local_deadline
    try:
        a, b = _window(start, end)
    except Exception:
        return _result('WEEKLY_ONLY_RATE_LIMITED')
    if (b-a != timedelta(days=7) or a.weekday() != 0 or
            any((a.hour, a.minute, a.second, b.hour, b.minute, b.second)) or
            b > datetime.now(timezone.utc)):
        return _result('WEEKLY_ONLY_RATE_LIMITED')
    if (type(max_requests) is not int or max_requests < 7 or
            effective_deadline - began < MINIMUM_WEEK_BUDGET_SECONDS):
        return _result('RUNNER_BUDGET_INSUFFICIENT')
    t = None
    records, accepted_days = [], 0
    try:
        with WallBudget(effective_deadline), SharedReportingLimiter() as limiter:
            now = time.monotonic()
            cooldown = max(0, limiter.state['not_before']-now)
            if effective_deadline-now < cooldown+MINIMUM_WEEK_BUDGET_SECONDS:
                return _result('RATE_LIMIT_COOLDOWN_EXCEEDS_BUDGET')
            try:
                headers = _headers()
            except Exception:
                return _result('CREDENTIAL_MISSING')
            t = ReadTransport(transport, deadline=effective_deadline, max_requests=7)
            for offset in range(7):
                # One delay calculation cannot race into a negative sleep.
                next_request_at = limiter.state['not_before']
                while True:
                    delay = next_request_at-time.monotonic()
                    if delay <= 0:
                        break
                    if next_request_at + REQUEST_TIMEOUT_SECONDS > effective_deadline:
                        raise TimeoutError('collector_budget_exhausted')
                    time.sleep(delay)
                if time.monotonic() + REQUEST_TIMEOUT_SECONDS > effective_deadline:
                    raise TimeoutError('collector_budget_exhausted')
                day = (a + timedelta(days=offset)).date().isoformat()
                limiter.reserve()
                before = len(t.receipts)
                try:
                    response = t.request('GET', API, headers=headers, params={'commissionDate': day})
                finally:
                    # A receipt proves a response returned, even when HTTP or
                    # provider parsing failed. Without one, preserve in-flight.
                    if len(t.receipts) > before:
                        limiter.response_completed()
                t.receipts[-1].update(date_axis='commissionDate', requested_date=day,
                                      reporting_timezone='UNKNOWN')
                if time.monotonic() > effective_deadline:
                    raise TimeoutError('collector_budget_exhausted')
                if response.status != 200:
                    raise ProviderResponseError('response_status_unproven')
                page = _project_page(response.json(), day)
                records.extend(page)
                accepted_days += 1
        return _result('DAILY_OBSERVATIONS_ONLY_SCHEMA_TIMEZONE_CURRENCY_REVISIONS_PAGINATION_UNPROVEN',
                       coverage='PARTIAL', records=records, receipts=t.receipts)
    except ProtectionUnavailable as error:
        return _result(str(error), coverage='PARTIAL' if accepted_days else 'UNKNOWN',
                       records=records, receipts=t.receipts if t is not None else [])
    except Exception as error:
        # Valid earlier days remain observations even after a later read fails.
        # JSON/provider/HTTP failures are UNKNOWN if no day was accepted. A
        # decoded unrecognized shape is explicit PARTIAL evidence, never data.
        receipts = t.receipts if t is not None else []
        failed_read = (isinstance(error, (ProviderResponseError, json.JSONDecodeError))
                       or not receipts or any(r['http_status'] != 200 for r in receipts))
        coverage = 'PARTIAL' if accepted_days or not failed_read else 'UNKNOWN'
        return _result('READ_FAILED_OR_SCHEMA_BUDGET_UNPROVEN_NO_RETRY', coverage=coverage,
                       records=records, receipts=receipts)
