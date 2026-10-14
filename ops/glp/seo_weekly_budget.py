"""GLP-44 weekly SEO collection spend ledger: hard $25/week across metered sources.

Operator authority: ops/linear/approvals/OPERATOR-DECISION-20261011-grilling-round1.md
("GLP-44 keyword/strategy collection"): Cogny + GSC + GA4 + DataForSEO, hard cap
USD 25 per week total. Over-cap or unknown cost => refuse and alert ops-alerts,
never overspend.

Every metered call is reserved BEFORE it is sent with a published-price ceiling
estimate. A reservation is refused when the week's committed spend plus the
estimate would exceed the cap, when the price is unknown, or when any earlier
spend this week is unknown (unknown is not zero). Committed spend counts this
ledger plus the shared DataForSEO rail journal for the same week, so other
DataForSEO jobs can never push the combined week over the cap. Actual provider
cost is settled per call; an unsettled or unmeasured call blocks the week.

Week = the supply week governed by the Sunday review: Sunday's calls count
toward the following Monday's week (Sunday..Saturday, America/Los_Angeles),
matching weekly_strategy_consumer.review_week_start.
Ordinary spend reduction reads no credentials. Exact recovery delegates only to
existing account-authenticated, zero-cost SERP metadata readback; no paid replay.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

B = Path('/home/ubuntu/business')
LEDGER_ROOT = B / 'reports/glp-clinic-scout/seo-machine/weekly-spend'
RAIL_JOURNAL = B / 'reports/ops/orchestration-restore-20260907/WS-C/dataforseo-ledger.jsonl'
WEEKLY_CAP_USD = Decimal('25')
AUTHORITY = 'ops/linear/approvals/OPERATOR-DECISION-20261011-grilling-round1.md#GLP-44 keyword/strategy collection'
OPS_ALERTS_CHANNEL = '1487300132054765629'
PT = ZoneInfo('America/Los_Angeles')

# Flat-rate or free rails: zero marginal cost with a recorded basis. Anything
# else is metered and must have a published-price estimator below.
ZERO_MARGINAL = {
    'cogny': 'Cogny Solo plan: cogny_status reports Unlimited credits (flat subscription, no per-call charge)',
    'gsc': 'Google Search Console API: no per-request charge',
    'ga4': 'GA4 Data API via Cogny connector: no per-request charge',
    'posthog': 'PostHog query API within existing plan: no per-request charge',
}


def _dfs_labs_items(payload: list[dict[str, Any]]) -> Decimal:
    # https://dataforseo.com/pricing/dataforseo-labs/dataforseo-google-api (2026-10-12):
    # all other Labs endpoints USD 0.012/task + 0.00012/item, clickstream doubles.
    total = Decimal('0')
    for task in payload:
        limit = task.get('limit')
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise BudgetRefused('unknown_cost:labs_limit_missing_or_invalid')
        price = Decimal('0.012') + Decimal('0.00012') * limit
        total += price * (2 if task.get('include_clickstream_data') else 1)
    return total


def _dfs_serp_live(payload: list[dict[str, Any]]) -> Decimal:
    # https://dataforseo.com/pricing/serp/google-organic-serp-api (2026-10-12):
    # Live mode USD 0.002 per SERP of 10 results; depth multiplies per 10 results.
    total = Decimal('0')
    for task in payload:
        depth = task.get('depth', 10)
        if type(depth) is not int or not 1 <= depth <= 700:
            raise BudgetRefused('unknown_cost:serp_depth_invalid')
        if any(task.get(k) for k in ('calculate_rectangles', 'load_async_overview', 'people_also_ask_click_depth', 'max_crawl_pages')):
            raise BudgetRefused('unknown_cost:serp_cost_multiplier_parameter')
        total += Decimal('0.002') * math.ceil(depth / 10)
    return total


def _dfs_ads_volume_live(payload: list[dict[str, Any]]) -> Decimal:
    # https://dataforseo.com/pricing/keywords-data/google-ads (2026-10-12):
    # Live mode USD 0.09 per task of up to 1,000 keywords.
    for task in payload:
        words = task.get('keywords')
        if not isinstance(words, list) or not 1 <= len(words) <= 1000:
            raise BudgetRefused('unknown_cost:search_volume_keywords_invalid')
    return Decimal('0.09') * len(payload)


ESTIMATORS: dict[tuple[str, str], Callable[[list[dict[str, Any]]], Decimal]] = {
    ('dataforseo', '/dataforseo_labs/google/ranked_keywords/live'): _dfs_labs_items,
    ('dataforseo', '/serp/google/organic/live/advanced'): _dfs_serp_live,
    ('dataforseo', '/keywords_data/google_ads/search_volume/live'): _dfs_ads_volume_live,
}


class BudgetRefused(RuntimeError):
    """Raised before a metered call is sent. Nothing was spent by this call."""


def week_start(moment: dt.datetime) -> dt.date:
    local = moment.astimezone(PT).date()
    if local.weekday() == 6:
        return local + dt.timedelta(days=1)
    return local - dt.timedelta(days=local.weekday())


def estimate(source: str, endpoint: str, payload: Any) -> Decimal:
    if source in ZERO_MARGINAL:
        return Decimal('0')
    estimator = ESTIMATORS.get((source, endpoint))
    if estimator is None:
        raise BudgetRefused(f'unknown_cost:{source}:{endpoint}')
    if not isinstance(payload, list) or not payload or not all(isinstance(t, dict) for t in payload):
        raise BudgetRefused('unknown_cost:payload_shape')
    return estimator(payload)


def _alert_ops(message: str) -> dict[str, Any]:
    from scripts.post_discord_message import send, readback
    sent = send(OPS_ALERTS_CHANNEL, message[:1900])
    message_id = str(sent.get('id') or '')
    fetched = readback(OPS_ALERTS_CHANNEL, message_id) if message_id else {}
    return {'sent': bool(message_id), 'message_id': message_id,
            'readback_exact': fetched.get('content') == message[:1900]}


class WeeklyBudget:
    def __init__(self, *, ledger_root: Path = LEDGER_ROOT, rail_journal: Path | None = RAIL_JOURNAL,
                 cap_usd: Decimal | float | str = WEEKLY_CAP_USD,
                 alert: Callable[[str], dict[str, Any]] | None = _alert_ops,
                 clock: Callable[[], dt.datetime] | None = None) -> None:
        self.ledger_root = Path(ledger_root)
        self.rail_journal = Path(rail_journal) if rail_journal else None
        self.cap = Decimal(str(cap_usd))
        if not self.cap > 0 or self.cap > WEEKLY_CAP_USD:
            raise ValueError('weekly_cap_outside_operator_authority')
        self.alert = alert
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def path(self, week: dt.date) -> Path:
        return self.ledger_root / f'{week.isoformat()}.jsonl'

    @staticmethod
    def _rows(handle) -> list[dict[str, Any]]:
        handle.seek(0)
        return [json.loads(line) for line in handle if line.strip()]

    @staticmethod
    def _rail_cost(value: Any) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            cost = Decimal(str(value))
            return cost if cost.is_finite() and cost >= 0 else None
        except ArithmeticError:
            return None

    @classmethod
    def _provider_task(cls, account: str, attempt_at: float, ambiguous_at: float,
                       endpoint: str, expected: dict[str, Any]) -> dict[str, Any] | None:
        """Read free metadata through the unchanged native account rail."""
        from ops.glp.seo_data_rails import DataForSEO, _http
        try:
            rail = DataForSEO()
            selected = rail._accounts[account]
            if not selected['auth']:
                return None
            start = dt.datetime.fromtimestamp(math.floor(attempt_at), dt.timezone.utc)
            end = dt.datetime.fromtimestamp(math.floor(ambiguous_at) + 1, dt.timezone.utc)
            payload = [{'datetime_from': start.strftime('%Y-%m-%d %H:%M:%S +00:00'),
                        'datetime_to': end.strftime('%Y-%m-%d %H:%M:%S +00:00'),
                        'limit': 1000, 'offset': 0, 'include_metadata': True}]
            doc = _http(rail.BASE + '/serp/id_list', method='POST', body=payload,
                        headers={'Authorization': 'Basic ' + selected['auth']}, timeout=60, retries=1)
            return cls._metadata_record(doc, payload, endpoint, expected)
        except Exception:
            return None

    @staticmethod
    def _echo_matches(echo: Any, expected: dict[str, Any]) -> bool:
        if not isinstance(echo, dict) or not set(expected).issubset(echo):
            return False
        defaults = {'device': 'desktop', 'os': 'windows', 'postback_data': 'advanced'}
        if any(k not in defaults or v != defaults[k] for k, v in echo.items() if k not in expected):
            return False
        return json.dumps({k: echo[k] for k in expected}, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False)

    @classmethod
    def _metadata_record(cls, doc, payload, endpoint, expected):
        if not isinstance(doc, dict) or doc.get('status_code') != 20000 or cls._rail_cost(doc.get('cost')) != 0:
            return None
        tasks = doc.get('tasks')
        if not isinstance(tasks, list) or len(tasks) != 1:
            return None
        task = tasks[0]
        if (task.get('status_code') != 20000 or cls._rail_cost(task.get('cost')) != 0
                or task.get('path') != ['v3', 'serp', 'id_list']
                or any(task.get('data', {}).get(k) != v for k, v in payload[0].items())):
            return None
        records = task.get('result')
        if not isinstance(records, list) or len(records) >= 1000 or not all(isinstance(r, dict) for r in records):
            return None
        matches = [r for r in records if r.get('url') == 'v3' + endpoint
                   and cls._echo_matches(r.get('metadata'), expected)]
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _recovery_cost(cls, result: dict[str, Any], attempt: dict[str, Any],
                       ambiguous: list[dict[str, Any]]) -> tuple[Decimal, str, str] | None:
        """Local response bytes are hints, not authentic evidence.

        Bind the original native account/run/attempt/hash/endpoint and interval
        to one unique authenticated provider task matching the complete payload,
        task ID and measured charge. An unavailable or incomplete provider record
        remains UNKNOWN. Supported recovery is one organic Live SERP task.
        No journal append or paid request is performed here.
        """
        try:
            proof = result['provider_evidence']
            if (proof['kind'] != 'dataforseo_response' or result.get('state') != 'MEASURED'
                    or attempt.get('endpoint') != '/serp/google/organic/live/advanced'
                    or not ambiguous):
                return None
            native_ids = ambiguous[0].get('provider_task_ids')
            if any(r.get('provider_task_ids') != native_ids for r in ambiguous):
                return None
            raw = Path(proof['path']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != proof['sha256']:
                return None
            doc = json.loads(raw)
            for key in ('account', 'run_id', 'attempt_id', 'request_hash', 'endpoint'):
                if not isinstance(attempt.get(key), str) or not attempt[key] or doc.get(key) != attempt[key]:
                    return None
            payload = doc['payload']
            if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict) or not payload[0]:
                return None
            digest = hashlib.sha256(json.dumps([doc['endpoint'], payload], sort_keys=True,
                                               allow_nan=False).encode()).hexdigest()
            if digest != attempt['request_hash']:
                return None
            response = doc['response']
            tasks = response['tasks']
            if response.get('status_code') != 20000 or not isinstance(tasks, list) or len(tasks) != 1:
                return None
            task = tasks[0]
            task_id = task.get('id')
            uuid.UUID(task_id)
            if native_ids is not None and native_ids != [task_id]:
                return None
            measured = cls._rail_cost(response.get('cost'))
            expected = dict(payload[0], api='serp', function='live', se='google', se_type='organic')
            if (task.get('status_code') != 20000
                    or measured is None or measured != cls._rail_cost(result.get('cost'))
                    or measured != cls._rail_cost(task.get('cost'))
                    or not cls._echo_matches(task.get('data'), expected)):
                return None
            end_at = min(r['at'] for r in ambiguous)
            record = cls._provider_task(attempt['account'], attempt['at'], end_at, attempt['endpoint'], expected)
            if (not isinstance(record, dict) or record.get('id') != task_id
                    or record.get('url') != 'v3' + attempt['endpoint']
                    or record.get('status') != '20000'
                    or cls._rail_cost(record.get('cost')) != measured
                    or not cls._echo_matches(record.get('metadata'), expected)):
                return None
            posted, done = (dt.datetime.strptime(record[k], '%Y-%m-%d %H:%M:%S %z').timestamp()
                            for k in ('datetime_posted', 'datetime_done'))
            # Provider timestamps have second precision, unlike the local journal.
            if not math.floor(attempt['at']) <= posted <= done <= math.floor(end_at) + 1:
                return None
            fingerprint = hashlib.sha256(json.dumps(response, sort_keys=True, allow_nan=False).encode()).hexdigest()
            return measured, task_id, fingerprint
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            pass
        return None

    def _rail_spend(self, week: dt.date) -> tuple[Decimal, bool]:
        """Reduce append-only exact identities; charge once in the ATTEMPT's PT week."""
        if self.rail_journal is None or not self.rail_journal.is_file():
            return Decimal('0'), False
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        total, unknown = Decimal('0'), False
        for line in self.rail_journal.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                unknown = True
                continue
            if not isinstance(row, dict):
                unknown = True
                continue
            if row.get('event') in ('ATTEMPT', 'AMBIGUOUS', 'RESULT'):
                identity = tuple(row.get(k) for k in ('account', 'run_id', 'attempt_id', 'request_hash'))
                if any(k is not None and not isinstance(k, str) for k in identity):
                    unknown = True
                    continue
                groups.setdefault(identity, []).append(row)
        for identity, rows in groups.items():
            attempts = [r for r in rows if r['event'] == 'ATTEMPT']
            anchor = attempts[0] if attempts else rows[0]
            try:
                at = anchor['at']
                if isinstance(at, bool) or not isinstance(at, (int, float)):
                    raise ValueError('invalid attempt time')
                if week_start(dt.datetime.fromtimestamp(at, dt.timezone.utc)) != week:
                    continue
            except (ValueError, KeyError, OverflowError, OSError):
                unknown = True  # undatable spend must not open any week
                continue
            if (not attempts or not anchor.get('attempt_id')
                    or any((r.get('at'), r.get('endpoint')) != (at, anchor.get('endpoint')) for r in attempts)):
                unknown = True
                continue
            results = [r for r in rows if r['event'] == 'RESULT']
            ambiguous = [r for r in rows if r['event'] == 'AMBIGUOUS']
            costs = [self._rail_cost(r.get('cost')) for r in results]
            valid = bool(results) and all(c is not None for c in costs) and len(set(costs)) == 1
            valid = valid and all(r.get('state') in (None, 'MEASURED', 'EXHAUSTED', 'PROVIDER_ERROR') for r in results)
            valid = valid and rows[0]['event'] == 'ATTEMPT'
            valid = valid and all(not isinstance(r.get('at'), bool)
                                  and isinstance(r.get('at'), (int, float))
                                  and math.isfinite(r['at']) and r['at'] >= at for r in rows)
            # Omitted legacy endpoint is compatible; an explicit conflict is not.
            valid = valid and all('endpoint' not in r or r['endpoint'] == anchor.get('endpoint') for r in rows)
            if ambiguous or any('provider_evidence' in r for r in results):
                valid = valid and all(isinstance(k, str) and bool(k) for k in identity)
                valid = valid and rows[-1]['event'] == 'RESULT'
                valid = valid and all(r['at'] >= a['at'] for r in results for a in ambiguous)
                if valid:
                    # The journal lacks competing payloads: different raw hashes
                    # cannot exclude provider-default-equivalent queries.
                    end = min((a['at'] for a in ambiguous), default=at)
                    for other_id, other_rows in groups.items():
                        if (other_id == identity or other_id[0] != identity[0]
                                or other_rows[0].get('endpoint') not in (None, anchor.get('endpoint'))):
                            continue
                        other_start = other_rows[0].get('at', float('nan'))
                        times = [r.get('at', float('nan')) for r in other_rows if r['event'] in ('RESULT', 'AMBIGUOUS')]
                        if (type(other_start) not in (int, float) or not math.isfinite(other_start)
                                or any(type(t) not in (int, float) or not math.isfinite(t) for t in times)):
                            valid = False
                            continue
                        other_end = math.floor(min(times)) + 1 if times else float('inf')
                        # Use the same whole-second uncertainty as provider
                        # metadata admission, not disjoint subsecond local times.
                        if math.floor(other_start) <= math.floor(end) + 1 and other_end >= math.floor(at):
                            valid = False
                    recovered = {}
                    for r in results:
                        if not valid:
                            break
                        key = json.dumps(r, sort_keys=True)
                        if key not in recovered:
                            recovered[key] = self._recovery_cost(r, anchor, ambiguous)
                    identities = list(recovered.values())
                    valid = valid and (all(i is not None and i[0] == costs[0] for i in identities)
                                       and len(set(identities)) == 1)
            if valid and costs[0] is not None:
                total += costs[0]
            else:
                unknown = True
        return total, unknown

    def _state(self, rows: list[dict[str, Any]], week: dt.date) -> dict[str, Any]:
        reserved, actual, unknown = {}, {}, []
        for row in rows:
            rid = row.get('reservation_id')
            if row['event'] == 'RESERVE':
                reserved[rid] = Decimal(row['estimate_usd'])
            elif row['event'] == 'SETTLE':
                if row.get('actual_usd') is None:
                    unknown.append(rid)
                else:
                    actual[rid] = Decimal(row['actual_usd'])
        open_ids = [r for r in reserved if r not in actual and r not in unknown]
        ledger = sum(actual.values(), Decimal('0')) + sum((reserved[r] for r in open_ids), Decimal('0'))
        rail, rail_unknown = self._rail_spend(week)
        return {'week_start': week.isoformat(), 'cap_usd': str(self.cap),
                'ledger_committed_usd': str(ledger), 'rail_journal_usd': str(rail),
                'committed_usd': str(ledger + rail), 'open_reservations': open_ids,
                'unknown_cost_reservations': unknown, 'rail_journal_unknown': rail_unknown,
                'calls': len(reserved)}

    def _append(self, handle, row: dict[str, Any]) -> None:
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(row, sort_keys=True, allow_nan=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())

    def _refuse(self, handle, week: dt.date, reason: str, row: dict[str, Any]) -> None:
        self._append(handle, {**row, 'event': 'REFUSE', 'reason': reason})
        key = hashlib.sha256(f'{week}:{reason.split(":")[0]}'.encode()).hexdigest()[:16]
        alerted = any(r.get('event') == 'ALERT' and r.get('key') == key for r in self._rows(handle))
        if not alerted and self.alert is not None:
            message = (f"⚠️ **GLP-44 SEO spend guard** — refused metered call ({reason}). "
                       f"Week {week} committed ${row.get('committed_usd')} of ${self.cap} cap; "
                       f"estimate ${row.get('estimate_usd')}. No call was sent. "
                       f"→ No action unless the cap should change.\n-# ledger: {self.path(week)}")
            try:
                receipt = self.alert(message)
            except Exception as exc:  # noqa: BLE001 - refusal stands even if alert fails
                receipt = {'sent': False, 'error_type': type(exc).__name__}
            self._append(handle, {'event': 'ALERT', 'key': key, 'at_utc': self.clock().isoformat(),
                                  'reason': reason, 'receipt': receipt})
        raise BudgetRefused(reason)

    def reserve(self, source: str, endpoint: str, payload: Any, *, run_id: str) -> dict[str, Any]:
        now = self.clock()
        week = week_start(now)
        path = self.path(week)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+', encoding='utf-8') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            state = self._state(self._rows(handle), week)
            base = {'at_utc': now.isoformat(), 'source': source, 'endpoint': endpoint,
                    'run_id': run_id, 'committed_usd': state['committed_usd'], 'authority': AUTHORITY,
                    'request_sha256': hashlib.sha256(json.dumps([endpoint, payload], sort_keys=True, default=str).encode()).hexdigest()}
            try:
                cost = estimate(source, endpoint, payload)
            except BudgetRefused as exc:
                self._refuse(handle, week, str(exc), {**base, 'estimate_usd': None})
            if cost and (state['unknown_cost_reservations'] or state['open_reservations'] or state['rail_journal_unknown']):
                self._refuse(handle, week, 'unknown_prior_spend_this_week', {**base, 'estimate_usd': str(cost)})
            if Decimal(state['committed_usd']) + cost > self.cap:
                self._refuse(handle, week, f'weekly_cap_exceeded:{Decimal(state["committed_usd"]) + cost}>{self.cap}',
                             {**base, 'estimate_usd': str(cost)})
            reservation = {**base, 'event': 'RESERVE', 'reservation_id': str(uuid.uuid4()),
                           'estimate_usd': str(cost), 'week_start': week.isoformat()}
            self._append(handle, reservation)
            return reservation

    def settle(self, reservation: dict[str, Any], actual_usd: Any) -> None:
        value = None
        if not isinstance(actual_usd, bool) and isinstance(actual_usd, (int, float, str, Decimal)):
            try:
                number = Decimal(str(actual_usd))
                value = str(number) if number.is_finite() and number >= 0 else None
            except ArithmeticError:
                value = None
        path = self.path(dt.date.fromisoformat(reservation['week_start']))
        with path.open('a+', encoding='utf-8') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self._append(handle, {'event': 'SETTLE', 'reservation_id': reservation['reservation_id'],
                                  'at_utc': self.clock().isoformat(), 'source': reservation['source'],
                                  'endpoint': reservation['endpoint'], 'estimate_usd': reservation['estimate_usd'],
                                  'actual_usd': value})

    def guarded(self, source: str, endpoint: str, payload: Any, send: Callable[[], Any], *,
                run_id: str, cost_of: Callable[[Any], Any]) -> Any:
        """Reserve, send once, settle the provider-reported cost (None => unknown)."""
        reservation = self.reserve(source, endpoint, payload, run_id=run_id)
        try:
            response = send()
        except Exception:
            self.settle(reservation, None)
            raise
        self.settle(reservation, cost_of(response))
        return response

    def receipt(self, week: dt.date | None = None) -> dict[str, Any]:
        week = week or week_start(self.clock())
        path = self.path(week)
        rows: list[dict[str, Any]] = []
        if path.is_file():
            with path.open('r', encoding='utf-8') as handle:
                fcntl.flock(handle, fcntl.LOCK_SH)
                rows = self._rows(handle)
        state = self._state(rows, week)
        by_source: dict[str, Decimal] = {}
        for row in rows:
            if row['event'] == 'SETTLE' and row.get('actual_usd') is not None:
                by_source[row['source']] = by_source.get(row['source'], Decimal('0')) + Decimal(row['actual_usd'])
        within = (not state['unknown_cost_reservations'] and not state['rail_journal_unknown']
                  and Decimal(state['committed_usd']) <= self.cap)
        return {'schema': 'glp-weekly-seo-spend-receipt-v1', **state,
                'actual_by_source_usd': {k: str(v) for k, v in sorted(by_source.items())},
                'zero_marginal_sources': ZERO_MARGINAL,
                'refusals': [r for r in rows if r['event'] == 'REFUSE'],
                'alerts': [r for r in rows if r['event'] == 'ALERT'],
                'within_cap': within, 'authority': AUTHORITY,
                'ledger': {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}}
