"""Read-only provider capacity collector candidate.

This module deliberately does *not* use ``load_pool().select()`` or
``resolve_codex_runtime_credentials()``.  Both are runtime paths that may
refresh OAuth state, clear cooldowns, or persist pool changes.  The collector
reads the already-persisted pool through Hermes' supported
``read_credential_pool`` helper, then makes only GET requests to the existing
Codex usage endpoint, once per pool row.

The module is a candidate for parent integration.  It is not a scheduler,
router, reset redeemer, or credential manager.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
import json
from pathlib import Path
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

UTC = timezone.utc
CLAUDE_TZ = ZoneInfo("America/Los_Angeles")
CLAUDE_RESET_WEEKDAY = 2  # datetime.weekday(): Monday=0, Wednesday=2
CLAUDE_RESET_LOCAL_TIME = time(4, 0)
DEFAULT_OPERATOR_REPORT_DATE = "2026-09-21"
CLAUDE_REPORTED_RESET_DATE = date(2026, 9, 9)

# User-reported on 2026-09-04: each of the two OpenAI subscriptions has one
# banked reset.  These are deliberately *unbound* metadata records.  Do not
# attach slot 1/2 to a pool label by order; only a provider usage response may
# establish that mapping.  They are operator-only and excluded by default from
# public serialization.
DEFAULT_OPERATOR_BANKED_RESET_METADATA: tuple[Mapping[str, Any], ...] = (
    {
        "slot": 1,
        "provider": "openai-codex",
        "reported_count": 0,
        "reported_on": DEFAULT_OPERATOR_REPORT_DATE,
        "label_binding": "deferred_until_usage_evidence",
    },
    {
        "slot": 2,
        "provider": "openai-codex",
        "reported_count": 0,
        "reported_on": DEFAULT_OPERATOR_REPORT_DATE,
        "label_binding": "deferred_until_usage_evidence",
    },
)

_USAGE_SOURCE = "usage_api"
_CLAUDE_FALLBACK_SOURCE = "operator_report:fallback"
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_ACCOUNT_ID_RE = re.compile(r"(?:^|[_.:-])(acct|account|user|org|team)[_.:-]?[A-Za-z0-9-]+$", re.I)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)


@dataclass(frozen=True)
class CapacityRecord:
    """Safe, serializable capacity facts for one provider/account label."""

    provider: str
    label: str
    session_remaining_pct: Optional[float]
    weekly_remaining_pct: Optional[float]
    session_reset_at_utc: Optional[str]
    weekly_reset_at_utc: Optional[str]
    capacity_status: str  # available | limited | depleted | unknown | unavailable
    freshness: str  # live | reported | unavailable
    source: str
    fetched_at_utc: str
    reset_scope: Optional[str] = None
    error_class: Optional[str] = None
    # Kept separate so callers cannot accidentally present operator metadata as
    # ordinary capacity.  It contains counts/status only, never credentials or
    # account identity.
    operator_only: Mapping[str, Any] = field(default_factory=dict, repr=False)
    windows: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def to_dict(self, *, include_operator_only: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.provider,
            "label": self.label,
            "session_remaining_pct": self.session_remaining_pct,
            "weekly_remaining_pct": self.weekly_remaining_pct,
            "session_reset_at_utc": self.session_reset_at_utc,
            "weekly_reset_at_utc": self.weekly_reset_at_utc,
            "capacity_status": self.capacity_status,
            "freshness": self.freshness,
            "source": self.source,
            "fetched_at_utc": self.fetched_at_utc,
            "reset_scope": self.reset_scope,
            "error_class": self.error_class,
            "windows": {key: dict(value) for key, value in self.windows.items()},
            "metric": "provider_quota",
        }
        if include_operator_only:
            result["operator_only"] = dict(self.operator_only)
        return result


@dataclass(frozen=True)
class CapacityReport:
    records: tuple[CapacityRecord, ...]
    collected_at_utc: str
    source: str = "account_capacity_collector"
    operator_only: tuple[Mapping[str, Any], ...] = DEFAULT_OPERATOR_BANKED_RESET_METADATA
    collection_errors: tuple[str, ...] = ()

    def to_dict(self, *, include_operator_only: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "records": [
                record.to_dict(include_operator_only=include_operator_only)
                for record in self.records
            ],
            "collected_at_utc": self.collected_at_utc,
            "source": self.source,
            "collection_errors": list(self.collection_errors),
        }
        if include_operator_only:
            result["operator_only"] = [dict(item) for item in self.operator_only]
        return result


class UsageReadError(Exception):
    """Internal safe error carrying only an HTTP/status class."""

    def __init__(self, error_class: str):
        super().__init__(error_class)
        self.error_class = error_class


def _require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return dt


def _utc_now_iso(now: Optional[datetime] = None) -> str:
    value = _require_aware(now or datetime.now(UTC)).astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_reset_at(value: Any) -> Optional[datetime]:
    """Parse provider epoch/ISO reset data and normalize it to UTC."""

    if value in (None, ""):
        return None
    try:
        if isinstance(value, datetime):
            return _require_aware(value).astimezone(UTC)
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                return None
            numeric = float(value)
            # Codex currently returns epoch seconds, but accepting milliseconds
            # keeps the collector tolerant without changing the wire call.
            if numeric > 1_000_000_000_000:
                numeric /= 1000.0
            return datetime.fromtimestamp(numeric, tz=UTC)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                numeric = float(text)
            except ValueError:
                numeric = None
            if numeric is not None:
                return _parse_reset_at(numeric)
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return _require_aware(parsed).astimezone(UTC)
    except (OverflowError, TypeError, ValueError):
        return None
    return None


def _format_utc(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return _require_aware(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_label(raw: Any, ordinal: int) -> str:
    """Return an operator-safe label without emitting identity-like values."""

    label = str(raw or "").strip()
    if not label:
        return f"pool-entry-{ordinal}"
    # ``codex-account-1`` / ``codex-account-2`` are explicit operator-safe
    # labels used for rotation.  Preserve them; reject generic identity-like
    # account prefixes instead.
    account_like = _ACCOUNT_ID_RE.search(label) and not label.lower().startswith("codex-account-")
    if "@" in label or _EMAIL_RE.fullmatch(label) or _UUID_RE.fullmatch(label) or account_like:
        return f"pool-entry-{ordinal}"
    # A long all-numeric label is more likely an account/customer identifier
    # than an intentional human label.  Preserve short numeric labels only.
    if label.isdigit() and len(label) > 8:
        return f"pool-entry-{ordinal}"
    if len(label) > 80:
        return f"pool-entry-{ordinal}"
    return label


def _safe_http_error_class(status_code: Any) -> str:
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        return "http_error"
    return f"http_{status}" if 100 <= status <= 599 else "http_error"


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _remaining(used_percent: Any) -> Optional[float]:
    used = _number(used_percent)
    if used is None:
        return None
    # Provider values are percentages. Clamp malformed over/under-shoots rather
    # than inventing a capacity above 100% or below zero.
    return round(max(0.0, min(100.0, 100.0 - used)), 4)


def _window(payload: Mapping[str, Any], key: str) -> tuple[Optional[float], Optional[str]]:
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, Mapping):
        return None, None
    raw_window = rate_limit.get(key)
    if not isinstance(raw_window, Mapping):
        return None, None
    return _remaining(raw_window.get("used_percent")), _format_utc(
        _parse_reset_at(raw_window.get("reset_at"))
    )


def _status(session_remaining: Optional[float], weekly_remaining: Optional[float]) -> str:
    values = [value for value in (session_remaining, weekly_remaining) if value is not None]
    if not values:
        return "unknown"
    if any(value <= 0.0 for value in values):
        return "depleted"
    if any(value <= 20.0 for value in values):
        return "limited"
    # Some Pro plans expose only a weekly window. Absence of a second window
    # is not missing data when the provider describes one actual limit.
    return "available"


def _codex_usage_url(base_url: Optional[str]) -> str:
    """Reuse Hermes' existing endpoint resolver; no auth or state mutation."""

    from agent.account_usage import _codex_backend_urls
    from hermes_cli.auth import DEFAULT_CODEX_BASE_URL

    configured = str(base_url or "").strip()
    return _codex_backend_urls(configured or os.getenv("HERMES_CODEX_BASE_URL", "") or DEFAULT_CODEX_BASE_URL)[0]


def _read_pool_rows() -> list[Mapping[str, Any]]:
    """Read persisted pool rows without invoking selection or refresh."""

    from hermes_cli.auth import read_credential_pool

    rows = read_credential_pool("openai-codex")
    return [row for row in rows if isinstance(row, Mapping)]


def _default_usage_getter(url: str, headers: Mapping[str, str]) -> Mapping[str, Any]:
    """Perform exactly one read-only GET and return JSON, value-blindly."""

    import httpx

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(url, headers=dict(headers))
        status_code = getattr(response, "status_code", 200)
        if int(status_code) >= 400:
            raise UsageReadError(_safe_http_error_class(status_code))
        payload = response.json()
    except UsageReadError:
        raise
    except Exception as exc:
        # Do not retain or report exception text: wrappers can include request
        # headers, URLs, or provider response bodies.
        raise UsageReadError(type(exc).__name__.lower()) from None
    if not isinstance(payload, Mapping):
        raise UsageReadError("invalid_json_shape")
    return payload


def _record_from_payload(
    *,
    provider: str,
    label: str,
    payload: Mapping[str, Any],
    fetched_at_utc: str,
) -> CapacityRecord:
    session_remaining = weekly_remaining = None
    session_reset = weekly_reset = None
    rate_limit = payload.get("rate_limit") or {}
    if not isinstance(rate_limit, Mapping):
        rate_limit = {}
    unclassified = False
    for key in ("primary_window", "secondary_window"):
        window = rate_limit.get(key)
        if not isinstance(window, Mapping):
            continue
        duration = _number(window.get("limit_window_seconds"))
        # The live Pro20x API returns a WEEKLY primary_window (604800s) and
        # no secondary_window. Positional Session/Weekly mapping is wrong.
        if duration == 604800:
            weekly_remaining, weekly_reset = _window(payload, key)
        elif duration == 18000:
            session_remaining, session_reset = _window(payload, key)
        else:
            unclassified = True
    status = _status(session_remaining, weekly_remaining)
    if rate_limit.get("allowed") is False or rate_limit.get("limit_reached") is True:
        status = "depleted"
    elif unclassified:
        status = "unknown"
    scope = (
        "session_and_weekly" if session_remaining is not None and weekly_remaining is not None
        else "weekly" if weekly_remaining is not None
        else "session" if session_remaining is not None
        else None
    )
    reset_credits = payload.get("rate_limit_reset_credits")
    banked_count: Optional[int] = None
    if isinstance(reset_credits, Mapping):
        raw_count = reset_credits.get("available_count")
        if isinstance(raw_count, (int, float)) and not isinstance(raw_count, bool):
            banked_count = max(0, int(raw_count))
    operator_only: dict[str, Any] = {"banked_reset_count": banked_count}
    return CapacityRecord(
        provider=provider,
        label=label,
        session_remaining_pct=session_remaining,
        weekly_remaining_pct=weekly_remaining,
        session_reset_at_utc=session_reset,
        weekly_reset_at_utc=weekly_reset,
        capacity_status=status,
        freshness="live",
        source=_USAGE_SOURCE,
        fetched_at_utc=fetched_at_utc,
        reset_scope=scope,
        error_class="unclassified_usage_window" if unclassified else None,
        operator_only=operator_only,
        windows={scope: {"remaining_pct": remaining, "reset_at_utc": reset,
                         "source": _USAGE_SOURCE, "fetched_at_utc": fetched_at_utc}
                 for scope, remaining, reset in (("five_hour", session_remaining, session_reset),
                                                  ("seven_day", weekly_remaining, weekly_reset))
                 if remaining is not None or reset is not None},
    )


def _unavailable_record(
    *,
    label: str,
    fetched_at_utc: str,
    error_class: str,
) -> CapacityRecord:
    return CapacityRecord(
        provider="openai-codex",
        label=label,
        session_remaining_pct=None,
        weekly_remaining_pct=None,
        session_reset_at_utc=None,
        weekly_reset_at_utc=None,
        capacity_status="unavailable",
        freshness="unavailable",
        source=_USAGE_SOURCE,
        fetched_at_utc=fetched_at_utc,
        error_class=error_class,
        operator_only={"banked_reset_count": None},
    )


def _claude_record_from_snapshot(
    snapshot: Any,
    *,
    fetched_at_utc: str,
) -> Optional[CapacityRecord]:
    """Map an existing ``agent.account_usage`` snapshot without a new call.

    The parent may pass a snapshot obtained from Hermes' already-proven
    non-generative Claude usage endpoint.  This adapter only reads its public
    window fields; it never resolves credentials itself.
    """

    if snapshot is None or getattr(snapshot, "unavailable_reason", None):
        return None
    session_remaining: Optional[float] = None
    weekly_remaining: Optional[float] = None
    session_reset: Optional[str] = None
    weekly_reset: Optional[str] = None
    scopes = {"current session": "five_hour", "five_hour": "five_hour",
              "current week": "seven_day", "week": "seven_day", "weekly": "seven_day",
              "seven_day": "seven_day", "opus week": "seven_day_opus",
              "seven_day_opus": "seven_day_opus", "sonnet week": "seven_day_sonnet",
              "seven_day_sonnet": "seven_day_sonnet"}
    windows: dict[str, dict[str, Any]] = {}
    for window in getattr(snapshot, "windows", ()) or ():
        label = str(getattr(window, "label", "") or "").lower()
        scope = scopes.get(label)
        if scope is None:
            continue  # Never serialize arbitrary upstream labels/identity strings.
        remaining = _remaining(getattr(window, "used_percent", None))
        reset = _format_utc(_parse_reset_at(getattr(window, "reset_at", None)))
        windows[scope] = {"remaining_pct": remaining, "reset_at_utc": reset,
                          "source": str(getattr(snapshot, "source", "usage_api") or "usage_api"),
                          "fetched_at_utc": fetched_at_utc}
        if scope == "five_hour":
            session_remaining, session_reset = remaining, reset
        elif scope == "seven_day":
            weekly_remaining, weekly_reset = remaining, reset
    if not windows:
        return None
    for scope in ('five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet'):
        windows.setdefault(scope, {"remaining_pct": None, "reset_at_utc": None,
                                   "source": "not_returned_by_usage_api", "fetched_at_utc": fetched_at_utc})
    return CapacityRecord(
        provider="anthropic",
        label="claude-live",
        session_remaining_pct=session_remaining,
        weekly_remaining_pct=weekly_remaining,
        session_reset_at_utc=session_reset,
        weekly_reset_at_utc=weekly_reset,
        capacity_status=_status(session_remaining, weekly_remaining),
        freshness="live",
        source=str(getattr(snapshot, "source", "usage_api") or "usage_api"),
        fetched_at_utc=fetched_at_utc,
        reset_scope="session_and_weekly",
        windows=windows,
    )


def next_claude_reset_utc(now: Optional[datetime] = None) -> datetime:
    """Return the next Wednesday 04:00 America/Los_Angeles as aware UTC."""

    current = _require_aware(now or datetime.now(UTC)).astimezone(CLAUDE_TZ)
    days_ahead = (CLAUDE_RESET_WEEKDAY - current.weekday()) % 7
    candidate_date = current.date() + timedelta(days=days_ahead)
    candidate = datetime.combine(candidate_date, CLAUDE_RESET_LOCAL_TIME, tzinfo=CLAUDE_TZ)
    if candidate <= current:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


def _reported_claude_reset_utc() -> datetime:
    """Return the fixed reset implied by the 2026-09-04 operator report."""

    return datetime.combine(
        CLAUDE_REPORTED_RESET_DATE,
        CLAUDE_RESET_LOCAL_TIME,
        tzinfo=CLAUDE_TZ,
    ).astimezone(UTC)


def claude_statusline_record(*, now: Optional[datetime] = None,
                            path: Path = Path('/home/ubuntu/.claude/usage-latest.json')) -> Optional[CapacityRecord]:
    """Read the existing Claude Code statusline snapshot, never credentials.

    File freshness is not an upstream API-fetch timestamp: label it cached.
    A past reset or old snapshot cannot prove that capacity has recovered.
    """
    try:
        data = json.loads(path.read_text())
        observed = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        limits = data.get('rate_limits') or {}
        values = {}
        for key, label in (('five_hour', 'session'), ('seven_day', 'weekly'),
                           ('seven_day_opus', 'opus'), ('seven_day_sonnet', 'sonnet')):
            window = limits.get(key)
            if not isinstance(window, Mapping):
                continue
            used = _number(window.get('used_percentage'))
            if used is None:
                continue
            # Claude Code's explicit used_percentage is already 0..100.
            values[label] = (_remaining(used), _format_utc(_parse_reset_at(window.get('resets_at'))))
        if not values:
            return None
        current = _require_aware(now or datetime.now(UTC)).astimezone(UTC)
        stale = (current - observed).total_seconds() > 3600
        due = any(reset and _parse_reset_at(reset) <= current for _, reset in values.values())
        session, session_reset = values.get('session', (None, None))
        weekly, weekly_reset = values.get('weekly', (None, None))
        return CapacityRecord(provider='anthropic', label='claude-code',
            session_remaining_pct=None if stale or due else session,
            weekly_remaining_pct=None if stale or due else weekly,
            session_reset_at_utc=session_reset, weekly_reset_at_utc=weekly_reset,
            capacity_status='unknown' if stale or due else _status(session, weekly),
            freshness='stale' if stale else 'cached', source='claude_code_statusline',
            fetched_at_utc=_format_utc(observed),
            reset_scope='session_and_weekly' if session is not None and weekly is not None else 'weekly',
            error_class='cached_reset_due_confirmation' if due else 'stale_statusline' if stale else None,
            windows={scope: {'remaining_pct': None if stale or due or (reset and _parse_reset_at(reset) <= current) else remaining,
                             'reset_at_utc': reset, 'source': 'claude_code_statusline',
                             'fetched_at_utc': _format_utc(observed)}
                     for label, (remaining, reset) in values.items()
                     for scope in ({'session': 'five_hour', 'weekly': 'seven_day',
                                    'opus': 'seven_day_opus', 'sonnet': 'seven_day_sonnet'}[label],)})
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def claude_user_reported_fallback(*, now: Optional[datetime] = None) -> CapacityRecord:
    """Represent the Claude pool when no live statusline read is available.

    Unknown is not zero: without a live read this record never asserts depletion.
    It carries only the next projected weekly reset (Wednesday 04:00 America/Los_Angeles)
    so the ledger can still pace, and an error class naming the missing live read.
    """

    fetched_at = _utc_now_iso(now)
    current = _require_aware(now or datetime.now(UTC)).astimezone(UTC)
    reset_at = None  # Historical operator schedule is not current telemetry.
    return CapacityRecord(
        provider="anthropic",
        label="claude-user-reported",
        session_remaining_pct=None,
        weekly_remaining_pct=None,
        session_reset_at_utc=None,
        weekly_reset_at_utc=_format_utc(reset_at),
        capacity_status="unknown",
        freshness="reported",
        source=_CLAUDE_FALLBACK_SOURCE,
        fetched_at_utc=fetched_at,
        reset_scope="weekly",
        error_class="no_live_claude_read",
        operator_only={
            "timezone": CLAUDE_TZ.key,
            "reset_local": "Wednesday 04:00",
            "note": "operator-reported Fable-only readings live in ops/subscription-capacity/OPERATOR-READINGS.json",
        },
    )


def collect_capacity(
    *,
    now: Optional[datetime] = None,
    pool_reader: Optional[Callable[[], Sequence[Mapping[str, Any]]]] = None,
    usage_getter: Optional[Callable[[str, Mapping[str, str]], Mapping[str, Any]]] = None,
    claude_snapshot_getter: Optional[Callable[[], Any]] = None,
    include_claude_fallback: bool = True,
    operator_bank_reset_metadata: Sequence[Mapping[str, Any]] = DEFAULT_OPERATOR_BANKED_RESET_METADATA,
) -> CapacityReport:
    """Collect one live usage snapshot per OpenAI pool row plus Claude fallback.

    ``pool_reader`` and ``usage_getter`` are injectable only for hermetic tests
    and controlled parent integration.  The default path uses the existing
    Hermes read-only pool reader and one GET per row.  If the parent supplies
    ``claude_snapshot_getter``, it must return an existing non-generative
    ``agent.account_usage`` snapshot; otherwise the user-reported Claude
    fallback is emitted.  This function never calls a refresh, pool selector,
    reset redeemer, scheduler, Discord, or config writer.
    """

    collected_at = _utc_now_iso(now)
    reader = pool_reader or _read_pool_rows
    getter = usage_getter or _default_usage_getter
    records: list[CapacityRecord] = []
    errors: list[str] = []
    try:
        rows = list(reader())
    except Exception as exc:
        rows = []
        errors.append(f"pool_read_{type(exc).__name__.lower()}")

    if not rows and errors:
        records.append(
            _unavailable_record(
                label="openai-pool-unavailable",
                fetched_at_utc=collected_at,
                error_class="pool_unavailable",
            )
        )

    for ordinal, row in enumerate(rows, start=1):
        label = _safe_label(row.get("label"), ordinal)
        token = str(row.get("access_token") or "").strip()
        base_url = row.get("base_url")
        if not token:
            records.append(
                _unavailable_record(
                    label=label,
                    fetched_at_utc=collected_at,
                    error_class="credential_unavailable",
                )
            )
            errors.append(f"{label}:credential_unavailable")
            continue
        try:
            payload = getter(
                _codex_usage_url(base_url if isinstance(base_url, str) else None),
                {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": "codex-cli",
                },
            )
            records.append(
                _record_from_payload(
                    provider="openai-codex",
                    label=label,
                    payload=payload,
                    fetched_at_utc=collected_at,
                )
            )
        except UsageReadError as exc:
            records.append(
                _unavailable_record(
                    label=label,
                    fetched_at_utc=collected_at,
                    error_class=exc.error_class,
                )
            )
            errors.append(f"{label}:{exc.error_class}")
        except Exception as exc:
            # A custom adapter must not be able to leak a token-bearing error
            # into the report.  Keep only its exception type.
            error_class = type(exc).__name__.lower()
            records.append(
                _unavailable_record(
                    label=label,
                    fetched_at_utc=collected_at,
                    error_class=error_class,
                )
            )
            errors.append(f"{label}:{error_class}")

    if claude_snapshot_getter is not None:
        try:
            live_claude = _claude_record_from_snapshot(
                claude_snapshot_getter(),
                fetched_at_utc=collected_at,
            )
        except Exception as exc:
            live_claude = None
            errors.append(f"claude_live_{type(exc).__name__.lower()}")
        if live_claude is not None:
            records.append(live_claude)
        elif include_claude_fallback:
            records.append(claude_user_reported_fallback(now=now))
    elif include_claude_fallback:
        records.append(claude_user_reported_fallback(now=now))

    return CapacityReport(
        records=tuple(records),
        collected_at_utc=collected_at,
        operator_only=tuple(dict(item) for item in operator_bank_reset_metadata),
        collection_errors=tuple(errors),
    )


__all__ = [
    "CapacityRecord",
    "CapacityReport",
    "DEFAULT_OPERATOR_BANKED_RESET_METADATA",
    "claude_user_reported_fallback",
    "collect_capacity",
    "next_claude_reset_utc",
]
