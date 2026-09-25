"""Regression detector for the ACTIVE-pool selector the whole fleet paces to.

capacity_pacing.py runs on cron every 30 minutes, picks the OpenAI pool that carries
load, and computes the burn rate workers read before delegating. It shipped with no
tests, so the 2026-09-16 defect it was patched for - naming a pool with nothing left
ACTIVE - had no detector. These cases pin the behaviour, not the patch.
"""
import capacity_pacing as cp

TARGET = cp.TARGET_LEFT_PCT


def pool(label, left, hours):
    return {"label": label, "provider": "openai-codex",
            "weekly_left_pct": left, "hours_to_weekly_reset": hours}


# The live shape at the time of the incident: two drained pools on the soonest window,
# one fresh pool on its own window ~88 h later.
LIVE = [pool("codex-account-1", 0.0, 79.70),
        pool("codex-account-2-real", 0.0, 79.68),
        pool("codex-account-3", 100.0, 167.74)]


def test_a_pool_at_or_below_target_is_never_active_while_another_has_capacity():
    for pref in (None, "codex-account-1", "codex-account-2-real"):
        got = cp.select_active(LIVE, pref)
        assert got["active"] == "codex-account-3", (pref, got)
    # With no reserve, the exact-target boundary is zero remaining.
    got = cp.select_active([pool("a", TARGET, 10.0), pool("b", 60.0, 200.0)])
    assert got["active"] == "b"


def test_required_rate_is_paced_against_the_selected_windows_own_capacity():
    """The rate must be reachable from the credit it is asked to burn."""
    got = cp.select_active(LIVE)
    assert got["window_labels"] == ["codex-account-3"]
    assert got["window_hours"] == 167.74
    assert got["window_left_pp"] == 100.0
    assert abs(got["required_burn_pp_per_hour"] * got["window_hours"] - 100.0) < 1.0
    # Finding's scenario B: one 40% pool on an 80 h window, one 100% pool on a 168 h window.
    # Summing both balances over the 80 h window demanded ~39 pp/day from a pool holding 35 pp.
    b = cp.select_active([pool("codex-account-1", 40.0, 80.0), pool("codex-account-3", 100.0, 168.0)])
    assert b["active"] == "codex-account-1"
    assert b["window_labels"] == ["codex-account-1"]
    assert b["required_burn_pp_per_hour"] * 24 <= 40.0


def test_balances_are_not_summed_even_when_accounts_share_a_window():
    """Same reset does not establish interchangeable plan allowances."""
    got = cp.select_active([pool("codex-account-2-real", 0.0, 80.0),
                            pool("codex-account-1", 100.0, 167.9),
                            pool("codex-account-3", 100.0, 167.74)],
                           "codex-account-1")
    assert got["active"] == "codex-account-1"
    assert sorted(got["window_labels"]) == ["codex-account-1", "codex-account-3"]
    assert got["window_left_pp"] == 100.0  # selected account only
    assert got["pref_applied"] is True


def test_cross_plan_rates_do_not_force_other_accounts():
    got = cp.select_active([pool('20x', 6, 10), pool('5x', 100, 20)])
    assert got['active'] == '20x'
    assert got['forced'] == []


def test_all_pools_drained_yields_an_explicit_no_capacity_signal():
    got = cp.select_active([pool("codex-account-1", 0.0, 79.7),
                            pool("codex-account-2-real", 0.0, 79.68),
                            pool("codex-account-3", 0.0, 167.74)])
    assert got["active"] is None
    assert got["status"] == "NO_OPENAI_CAPACITY"
    assert got["required_burn_pp_per_hour"] is None


def test_operator_preference_decides_order_inside_the_window_and_never_across_it():
    same = [pool("codex-account-1", 40.0, 79.7), pool("codex-account-2-real", 60.0, 79.68)]
    # soonest-reset alone would pick account-2-real; the preference is a free choice here
    assert cp.select_active(same)["active"] == "codex-account-2-real"
    got = cp.select_active(same, "codex-account-1")
    assert got["active"] == "codex-account-1" and got["pref_applied"] is True
    # a preference naming a LATER window must not strand the expiring credit
    across = cp.select_active([pool("codex-account-1", 40.0, 79.7), pool("codex-account-3", 100.0, 167.74)],
                              "codex-account-3")
    assert across["active"] == "codex-account-1"
    assert across["pref_applied"] is False
    assert "different window" in across["pref_note"]
    # a preference naming a drained pool is recorded, not silently dropped
    drained = cp.select_active(LIVE, "codex-account-1")
    assert drained["pref_applied"] is False
    assert "reserve target" in drained["pref_note"]
    # a preference naming no live pool at all
    ghost = cp.select_active(LIVE, "codex-account-9")
    assert ghost["active"] == "codex-account-3" and "matches no measured pool" in ghost["pref_note"]


def test_reset_windows_group_by_proximity_not_by_account():
    groups = cp.group_reset_windows(LIVE)
    assert [[p["label"] for p in g] for g in groups] == [
        ["codex-account-2-real", "codex-account-1"], ["codex-account-3"]]


def test_pools_without_a_measured_reset_are_not_selectable():
    got = cp.select_active([{"label": "codex-account-1", "weekly_left_pct": 90.0,
                             "hours_to_weekly_reset": None}])
    assert got["active"] is None and got["status"] == "NO_MEASURED_OPENAI_POOLS"


# --- 2026-09-16 pass 2: the ledger carries alias rows and can go stale ---

import json
from datetime import datetime, timezone


def _write_ledger(tmp_path, monkeypatch, records, collected_at):
    path = tmp_path / 'latest.json'
    path.write_text(json.dumps({'records': records, 'collected_at_utc': collected_at}))
    monkeypatch.setattr(cp, 'LEDGER', path)
    return path


LEDGER_ROWS = [
    {'provider': 'openai-codex', 'label': 'codex-account-2-real', 'weekly_remaining_pct': 0.0,
     'weekly_reset_at_utc': '2026-09-19T08:12:51Z', 'freshness': 'live'},
    {'provider': 'openai-codex', 'label': 'codex-account-2', 'weekly_remaining_pct': 0.0,
     'weekly_reset_at_utc': '2026-09-19T08:12:51Z', 'freshness': 'live',
     'duplicate_of': 'codex-account-2-real'},
    {'provider': 'openai-codex', 'label': 'codex-account-3', 'weekly_remaining_pct': 98.0,
     'weekly_reset_at_utc': '2026-09-23T00:56:47Z', 'freshness': 'live'},
]
COLLECTED = '2026-09-16T01:17:06Z'
COLLECTED_TS = datetime(2026, 9, 16, 1, 17, 6, tzinfo=timezone.utc).timestamp()


def test_alias_rows_are_not_paced_as_pools(tmp_path, monkeypatch):
    """One account under two credential labels was counted - and summed - twice."""
    _write_ledger(tmp_path, monkeypatch, LEDGER_ROWS, COLLECTED)
    labels = [p['label'] for p in cp.pools_from_ledger(COLLECTED_TS + 60)]
    assert labels == ['codex-account-2-real', 'codex-account-3']


def test_pacing_four_distinct_accounts_scoped_claude_and_local_pro(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    reset = datetime.fromtimestamp(now.timestamp() + 2 * 86400, timezone.utc).isoformat()
    records = [dict(provider='openai-codex', label=f'codex-account-{i}',
                    weekly_remaining_pct=v, weekly_reset_at_utc=reset, freshness='live',
                    windows={'seven_day': {'remaining_pct': v, 'reset_at_utc': reset}})
               for i, v in ((1, 40), (2, 0), (3, 80))]
    records.append(dict(records[1], label='alias', duplicate_of='codex-account-2'))
    records.append(dict(provider='anthropic', label='claude-live', weekly_remaining_pct=75,
                        weekly_reset_at_utc=reset, freshness='live',
                        windows={'seven_day': {'remaining_pct': 75, 'reset_at_utc': reset},
                                 'seven_day_opus': {'remaining_pct': 45, 'reset_at_utc': reset}}))
    ledger = _write_ledger(tmp_path, monkeypatch, records, now.isoformat())
    doc = json.loads(ledger.read_text())
    doc['pro_chat_ledger'] = [{'label': 'chat-profile', 'submitted_7d': 45,
                              'local_weekly_guard': 50, 'local_guard_remaining': 5}]
    ledger.write_text(json.dumps(doc))
    monkeypatch.setattr(cp, 'ROOT', tmp_path)
    monkeypatch.setattr(cp, 'OUT', tmp_path / 'PACING.json')
    monkeypatch.setattr(cp, 'HISTORY', tmp_path / 'missing-history')
    assert cp.main() == 0
    result = json.loads(cp.OUT.read_text())
    assert len([p for p in result['pools'] if p['metric'] == 'provider_quota']) == 4
    claude = next(p for p in result['pools'] if p['provider'] == 'anthropic')
    assert claude['target_pct'] == 15
    assert abs(claude['required_pp_per_day'] - 30) < 0.1
    assert abs(claude['window_pacing']['seven_day_opus']['required_pp_per_day'] - 15) < 0.1
    assert next(p for p in result['pools'] if p['provider'] == 'pro-chat')['required_pp_per_day'] is None
    assert result['ledger_stale'] is False


def test_stale_budget_exceeds_schedule_jitter():
    assert cp.LEDGER_MAX_AGE_MIN > 30


def test_a_dead_collector_never_reads_as_live_capacity(tmp_path, monkeypatch):
    """Pacing copied the collector's own 'live' word however old the sample was."""
    _write_ledger(tmp_path, monkeypatch, LEDGER_ROWS, COLLECTED)
    fresh = cp.pools_from_ledger(COLLECTED_TS + 60)
    assert {p['freshness'] for p in fresh} == {'live'}
    stale = cp.pools_from_ledger(COLLECTED_TS + 3 * 3600)
    assert all(p['freshness'].startswith('stale (ledger sample') for p in stale)
    assert cp.ledger_age_minutes({'collected_at_utc': COLLECTED}, COLLECTED_TS + 1800) == 30.0
