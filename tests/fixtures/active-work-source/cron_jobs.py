ONESHOT_RUN_CLAIM_TTL_SECONDS = 1800

_DEFAULT_CRON_INACTIVITY_TIMEOUT = 600.0

def _oneshot_run_claim_ttl_seconds() -> float:
    """One-shot running-claim TTL from ``HERMES_CRON_TIMEOUT``: unset/invalid → 600s → 1800s;
    ``0`` (unlimited) → the fixed floor; positive N → ``max(N * headroom, floor)``."""
    raw = cron_env_setting("HERMES_CRON_TIMEOUT").strip()
    try:
        timeout = float(raw) if raw else _DEFAULT_CRON_INACTIVITY_TIMEOUT
    except (ValueError, TypeError):
        timeout = _DEFAULT_CRON_INACTIVITY_TIMEOUT
    if timeout <= 0:
        return float(ONESHOT_RUN_CLAIM_TTL_SECONDS)
    return max(timeout * CLAIM_TTL_INACTIVITY_HEADROOM, float(ONESHOT_RUN_CLAIM_TTL_SECONDS))
