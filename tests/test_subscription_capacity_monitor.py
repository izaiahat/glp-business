from datetime import datetime, timezone
from types import SimpleNamespace
import json

import pytest
import subscription_capacity_monitor as monitor

NOW = datetime(2026, 9, 4, 21, tzinfo=timezone.utc)


def test_codex_usage_binds_account_and_residency_without_auth_mutation():
    import base64
    from account_capacity_collector import collect_capacity

    claims = {'https://api.openai.com/auth': {
        'chatgpt_account_id': 'fixture-account', 'chatgpt_data_residency': 'us'}}
    token = 'fixture.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.fixture'
    rows = [{'label': 'codex-account-1', 'access_token': token}]
    before = json.dumps(rows)
    calls = []

    def getter(url, headers):
        calls.append(url)
        assert headers['ChatGPT-Account-ID'] == 'fixture-account'
        assert headers['x-openai-internal-codex-residency'] == 'us'
        return {'rate_limit': {'allowed': True, 'limit_reached': False,
            'primary_window': {'used_percent': 68, 'limit_window_seconds': 604800,
                               'reset_at': '2026-10-04T22:46:02Z'}},
            'rate_limit_reset_credits': {'available_count': 1}}

    report = collect_capacity(pool_reader=lambda: rows, usage_getter=getter,
                              include_claude_fallback=False)
    assert len(calls) == 1
    assert report.records[0].weekly_remaining_pct == 32
    assert report.records[0].operator_only['banked_reset_count'] == 1
    assert json.dumps(rows) == before
    serialized = json.dumps(report.to_dict(include_operator_only=True))
    assert token not in serialized and 'fixture-account' not in serialized


def test_report_prefers_live_banked_counts_and_discloses_roster_drift():
    records = [{'provider': 'openai-codex', 'label': f'codex-account-{i}',
        'weekly_remaining_pct': left, 'weekly_reset_at_utc': None,
        'freshness': 'live', 'capacity_status': 'available' if left else 'depleted',
        'operator_only': {'banked_reset_count': 1}} for i, left in enumerate((0, 0, 32), 1)]
    report = {'records': records, 'account_count_ok': False,
        'collection_errors': ['openai_account_set_changed'],
        'banked_resets_operator': {'count': 1, 'pending_rollout_count': 3,
                                   'reported_on': '2026-09-22'}}
    text = monitor.render_report(report, NOW)
    assert '3 across 3 observed accounts' in text
    assert 'pending rollout' not in text
    assert 'openai_account_set_changed' in text
    assert '32% left' in text


def test_claude_usage_reads_valid_persisted_token_without_refresh(monkeypatch):
    import hermes_cli.auth as auth
    import agent.anthropic_credentials as creds
    import account_capacity_collector as collector_module
    monkeypatch.setattr(auth, 'read_credential_pool', lambda provider: [])
    monkeypatch.setattr(creds, 'read_claude_code_credentials', lambda: {'accessToken': 'sk-ant-oat-fixture'})
    monkeypatch.setattr(creds, 'is_claude_code_token_valid', lambda c: True)
    calls = []
    def getter(url, headers):
        calls.append((url, headers['Authorization'].startswith('Bearer ')))
        return {'five_hour': {'utilization': 0.2, 'resets_at': NOW.isoformat()},
                'seven_day_opus': {'utilization': 0.5, 'resets_at': NOW.isoformat()}}
    monkeypatch.setattr(collector_module, '_default_usage_getter', getter)
    snapshot = monitor.claude_usage_snapshot()
    assert len(calls) == 1 and calls[0][0].endswith('/api/oauth/usage')
    assert {w.label for w in snapshot.windows} == {'Current session', 'Opus week'}


def test_model_doctrine_has_no_reserve_and_live_claude_wins(monkeypatch):
    import capacity_pacing as pacing
    from account_capacity_collector import CapacityReport, _claude_record_from_snapshot

    assert pacing.TARGET_LEFT_PCT == monitor.RESERVE_TARGET_PCT == 0
    assert pacing.select_active([{
        'label': 'small-remainder', 'weekly_left_pct': 1,
        'hours_to_weekly_reset': 2,
    }])['active'] == 'small-remainder'
    snap = SimpleNamespace(source='oauth_usage_api', unavailable_reason=None, windows=[
        SimpleNamespace(label='Current session', used_percent=0, reset_at=None),
        SimpleNamespace(label='Current week', used_percent=100, reset_at=NOW),
    ])
    def collect(**kwargs):
        assert kwargs['claude_snapshot_getter']() is snap
        row = _claude_record_from_snapshot(snap, fetched_at_utc=NOW.isoformat())
        return CapacityReport(records=(row,), collected_at_utc=NOW.isoformat())
    monkeypatch.setattr(monitor, 'claude_usage_snapshot', lambda: snap)
    monkeypatch.setattr(monitor, 'collect_capacity', collect)
    monkeypatch.setattr(monitor, 'claude_statusline_record', lambda **_: pytest.fail('stale cache must not replace live API'))
    record = monitor.collect_monitored_capacity(now=NOW).records[0]
    assert record.freshness == 'live' and record.weekly_remaining_pct == 0


def test_claude_all_model_week_is_not_overwritten_by_model_windows():
    from account_capacity_collector import _claude_record_from_snapshot
    snap = SimpleNamespace(source='oauth_usage_api', unavailable_reason=None, windows=[
        SimpleNamespace(label='Current week', used_percent=100, reset_at=NOW),
        SimpleNamespace(label='Opus week', used_percent=20, reset_at=NOW),
        SimpleNamespace(label='Sonnet week', used_percent=10, reset_at=NOW),
    ])
    assert _claude_record_from_snapshot(snap, fetched_at_utc=NOW.isoformat()).weekly_remaining_pct == 0


def test_scoped_claude_windows_survive_into_ledger_and_alerts():
    from account_capacity_collector import _claude_record_from_snapshot
    snap = SimpleNamespace(source='oauth_usage_api', unavailable_reason=None, windows=[
        SimpleNamespace(label='Current session', used_percent=14, reset_at=NOW),
        SimpleNamespace(label='Current week', used_percent=4, reset_at=NOW),
        SimpleNamespace(label='Opus week', used_percent=61, reset_at=NOW),
        SimpleNamespace(label='Sonnet week', used_percent=17, reset_at=NOW),
    ])
    row = _claude_record_from_snapshot(snap, fetched_at_utc=NOW.isoformat()).to_dict()
    assert set(row['windows']) == {'five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet'}
    assert row['windows']['seven_day_opus']['remaining_pct'] == 39
    assert row['weekly_remaining_pct'] == 96
    states = monitor.alert_state({'records': [row]}, NOW)
    assert next(x for x in states if x['scope'] == 'seven_day_opus')['bucket'] == '40'
    assert monitor.threshold_bucket(84, (0, 10, 25, 40, 55, 70, 85)) == '85'
    assert monitor.threshold_bucket(69, (0, 10, 25, 40, 55, 70, 85)) == '70'
    assert monitor.threshold_bucket(19, (0, 10, 20)) == '20'
    assert monitor.threshold_bucket(9, (0, 10, 20)) == '10'


def test_five_hour_crossings_only_at_ninety_percent_used_and_weekly_keeps_steps(tmp_path):
    sent = []
    def sender(message, _directory):
        sent.append(message)
        return {'content': message, 'message_id': str(len(sent))}

    def usage(session_left, week_left):
        return lambda **_: SimpleNamespace(to_dict=lambda **__: {
            'records': [{'provider': 'anthropic', 'label': 'claude-live', 'freshness': 'live',
                         'capacity_status': 'available', 'windows': {
                             'five_hour': {'remaining_pct': session_left},
                             'seven_day': {'remaining_pct': week_left}}}],
            'collection_errors': [], 'collected_at_utc': NOW.isoformat()})

    monitor.tick(tmp_path, collector=usage(100, 100), sender=sender, now=NOW)
    monitor.tick(tmp_path, collector=usage(70, 84), sender=sender, now=NOW)
    assert 'claude-live seven_day provider remaining ≤85%' in sent[-1]
    assert 'five_hour provider remaining' not in sent[-1]
    monitor.tick(tmp_path, collector=usage(11, 84), sender=sender, now=NOW)
    assert 'five_hour provider remaining' not in sent[-1]
    monitor.tick(tmp_path, collector=usage(10, 84), sender=sender, now=NOW)
    assert 'claude-live five_hour provider remaining ≤10%' in sent[-1]


def test_duplicate_account_remains_in_ledger_but_not_alert_state(tmp_path):
    report = monitor.tick(with_roster(tmp_path), notify=False, collector=pooled(LIVE_FOUR), now=NOW,
                          sender=lambda *_: pytest.fail('must not send'))
    assert any(r.get('duplicate_of') for r in report['records'])
    labels = {s['label'] for s in monitor.alert_state(report, NOW)}
    assert 'codex-account-2' not in labels
    assert 'codex-account-2-real' in labels


def test_pro_local_threshold_is_not_provider_remaining():
    report = {'records': [], 'pro_chat_ledger': [
        {'label': 'profile', 'submitted_7d': 44, 'local_weekly_guard': 50,
         'local_guard_remaining': 6, 'provider_remaining_pct': None}]}
    bucket = lambda: next(x['bucket'] for x in monitor.alert_state(report, NOW) if x['scope'] == 'local_submissions_7d')
    assert bucket() == 'normal'
    report['pro_chat_ledger'][0]['local_guard_remaining'] = 5
    assert bucket() == '10'
    report['pro_chat_ledger'][0]['local_guard_remaining'] = 0
    assert bucket() == '0'

def sample(remaining=91):
    records = []
    for label, pct, reset in [('codex-account-1', 0, '2026-09-07T02:27:58Z'),
                              ('codex-account-2', remaining, '2026-09-10T20:54:21Z')]:
        records.append({'provider': 'openai-codex', 'label': label,
            'weekly_remaining_pct': pct, 'weekly_reset_at_utc': reset,
            'capacity_status': 'depleted' if pct == 0 else 'available',
            'freshness': 'live', 'operator_only': {'banked_reset_count': 1}})
    records.append({'provider': 'anthropic', 'label': 'claude-user-reported',
        'weekly_remaining_pct': 0, 'weekly_reset_at_utc': '2026-09-09T11:00:00Z',
        'capacity_status': 'depleted', 'freshness': 'reported'})
    return {'records': records, 'collection_errors': [], 'collected_at_utc': NOW.isoformat()}


def collector(value):
    return lambda **_: SimpleNamespace(to_dict=lambda **__: sample(value))


def test_monitor_silences_unchanged_samples_and_delivers_threshold_change(tmp_path):
    sent = []
    def sender(message, directory):
        sent.append(message)
        return {'content': message, 'message_id': str(len(sent))}
    monitor.tick(tmp_path, collector=collector(91), sender=sender, now=NOW)
    monitor.tick(tmp_path, collector=collector(90), sender=sender, now=NOW)
    assert len(sent) == 1
    monitor.tick(tmp_path, collector=collector(10), sender=sender, now=NOW)
    assert len(sent) == 2
    assert '**Capacity threshold**' in sent[1] and '≤10%' in sent[1]
    assert len((tmp_path / 'history.jsonl').read_text().splitlines()) == 3
    assert json.loads((tmp_path / 'latest.json').read_text())['account_count_ok']


def test_failed_delivery_does_not_advance_dedupe_state(tmp_path):
    def failing(*_):
        raise RuntimeError('fixture_failure')
    with pytest.raises(RuntimeError):
        monitor.tick(tmp_path, collector=collector(91), sender=failing, now=NOW)
    assert not (tmp_path / 'notification-state.json').exists()
    assert (tmp_path / 'latest.json').exists()


def test_no_notify_writes_live_ledger_without_external_effect(tmp_path):
    result = monitor.tick(tmp_path, notify=False, collector=collector(91), now=NOW,
                          sender=lambda *_: pytest.fail('must not send'))
    assert result['records'][1]['weekly_remaining_pct'] == 91
    assert not (tmp_path / 'notification-state.json').exists()


def test_report_labels_unverified_claude_and_renders_pacific_reset():
    text = monitor.render_report(sample(), NOW, pacing={})
    assert '**Anthropic**' in text and 'reported' in text
    assert 'PDT' in text
    assert monitor.discord_fmt.lint(text, 'report')['status'] == 'pass'
    assert len(text) <= 1900


def test_report_names_the_mode_from_the_ledger_and_one_reset_per_window():
    """The mode word and the reset time were literals: a sequential pool was reported as
    'shared', and one account's reset was attached to a total it did not cover."""
    seq = monitor.render_report(sample(), NOW, pacing={'active_openai_pool': 'codex-account-2',
                                                       'pools': [{'label': 'codex-account-2',
                                                                  'required_burn_pp_per_day': 9.0,
                                                                  'status': 'ON_TRACK'}]})
    assert 'Prioritize useful work on codex-account-2' in seq
    # the fixture's two accounts reset 3 days apart: two window lines, each with its own reset
    assert len([l for l in seq.splitlines() if l.startswith('**Codex ') and 'reset ' in l]) == 2
    assert 'Sep 06' in seq and 'Sep 10' in seq
    assert 'pts/day' not in seq
    shared = monitor.render_report(sample(), NOW, pacing={'active_openai_pool': 'shared', 'pools': [],
                                                          'combined_openai': {'labels': ['codex-account-1', 'codex-account-2'],
                                                                              'required_burn_pp_per_day': 12.0, 'status': 'ON_TRACK'}})
    assert 'combined' not in shared
    assert monitor.discord_fmt.lint(shared, 'report')['status'] == 'pass'


def test_routing_advice_never_names_a_depleted_pool():
    """The fleet-facing line and the pacing ledger must agree. It once broadcast
    'prioritize codex-account-1' at 1.0% weekly left while the ledger named the 100% pool."""
    report = {'records': [
        {'provider': 'openai-codex', 'label': 'codex-account-1', 'weekly_remaining_pct': 0.0,
         'weekly_reset_at_utc': '2026-09-07T02:27:58Z', 'capacity_status': 'limited', 'freshness': 'live'},
        {'provider': 'openai-codex', 'label': 'codex-account-3', 'weekly_remaining_pct': 100.0,
         'weekly_reset_at_utc': '2026-09-11T02:27:58Z', 'capacity_status': 'available', 'freshness': 'live'}]}
    # ledger authoritative
    assert 'codex-account-3' in monitor.routing_advice(report, NOW, {'active_openai_pool': 'codex-account-3'})
    # ledger missing -> same positive-capacity filter, never the drained pool
    fallback = monitor.routing_advice(report, NOW, {})
    assert 'codex-account-3' in fallback and 'codex-account-1' not in fallback
    # Zero remaining -> an explicit no-capacity line, not a dead label.
    drained = {'records': [dict(report['records'][0]), dict(report['records'][1], weekly_remaining_pct=0.0)]}
    assert 'No OpenAI pool has measured remaining capacity' in monitor.routing_advice(drained, NOW, {})


def test_account_count_rejects_a_dead_credential_row_standing_in_for_an_account(tmp_path):
    """A 401 row once passed the count check while a real account was missing."""
    def collector_with(records):
        return lambda **_: SimpleNamespace(to_dict=lambda **__: {
            'records': records, 'collection_errors': [], 'collected_at_utc': NOW.isoformat()})
    good = [{'provider': 'openai-codex', 'label': f'codex-account-{n}', 'weekly_remaining_pct': 50,
             'weekly_reset_at_utc': '2026-09-10T20:54:21Z', 'capacity_status': 'available',
             'freshness': 'live', 'error_class': None} for n in (1, 2)]
    first = monitor.tick(tmp_path, notify=False, collector=collector_with(good), now=NOW)
    assert first['account_count_ok'] and first['openai_account_labels'] == ['codex-account-1', 'codex-account-2']
    dead = [good[0], {'provider': 'openai-codex', 'label': 'codex-account-9', 'weekly_remaining_pct': None,
                      'weekly_reset_at_utc': None, 'capacity_status': 'unavailable',
                      'freshness': 'unavailable', 'error_class': 'http_401'}]
    second = monitor.tick(tmp_path, notify=False, collector=collector_with(dead), now=NOW)
    assert not second['account_count_ok']
    assert 'openai_account_set_changed' in second['collection_errors']


def test_expected_account_count_follows_the_pool_not_a_magic_number(tmp_path):
    """A hardcoded expectation re-armed the same false alarm on every plan change."""
    assert monitor.EXPECTED_OPENAI_ACCOUNTS is None
    for count in (2, 3, 4):
        records = [{'provider': 'openai-codex', 'label': f'codex-account-{n}', 'weekly_remaining_pct': 50,
                    'weekly_reset_at_utc': '2026-09-10T20:54:21Z', 'capacity_status': 'available',
                    'freshness': 'live', 'error_class': None} for n in range(1, count + 1)]
        directory = tmp_path / f'run{count}'
        result = monitor.tick(directory, notify=False, now=NOW,
                              collector=lambda **_: SimpleNamespace(to_dict=lambda **__: {
                                  'records': records, 'collection_errors': [], 'collected_at_utc': NOW.isoformat()}))
        assert result['account_count_ok'] and result['expected_openai_accounts'] == count


def test_recovers_old_pending_receipt_without_acknowledging_new_state(tmp_path):
    receipt = {'content': 'old report', 'message_id': 'previous'}
    monitor.tick(tmp_path, collector=collector(91), now=NOW, sender=lambda *_: receipt)
    assert not (tmp_path / 'notification-state.json').exists()


def test_exact_send_readback_and_no_mentions(tmp_path):
    calls = []
    def request(method, path, payload):
        calls.append((method, path, payload))
        if path == '/channels/' + monitor.CHANNEL:
            return {'guild_id': 'guild'}
        if path == '/guilds/guild/channels':
            return [{'id': monitor.CHANNEL, 'name': 'ops-alerts'}]
        if method == 'POST':
            assert payload['allowed_mentions'] == {'parse': []}
            return {'id': 'msg1'}
        return {'id': 'msg1', 'channel_id': monitor.CHANNEL, 'content': 'fixture message'}
    receipt = monitor.verified_send('fixture message', tmp_path, request=request)
    assert receipt['exact_readback']
    assert not (tmp_path / 'pending-delivery.json').exists()
    assert len([x for x in calls if x[0] == 'POST']) == 1


def test_ambiguous_post_never_blind_retries(tmp_path):
    monitor.atomic_json(tmp_path / 'pending-delivery.json', {'content': 'uncertain'})
    def request(method, path, payload):
        assert method == 'GET'
        return []
    with pytest.raises(RuntimeError, match='ambiguous_discord_post'):
        monitor.verified_send('new', tmp_path, request=request)
    assert (tmp_path / 'pending-delivery.json').exists()


def test_wrong_readback_retains_post_id(tmp_path):
    def request(method, path, payload):
        if path == '/channels/' + monitor.CHANNEL:
            return {'guild_id': 'guild'}
        if path == '/guilds/guild/channels':
            return [{'id': monitor.CHANNEL, 'name': 'ops-alerts'}]
        if method == 'POST':
            return {'id': 'sent'}
        return {'id': 'sent', 'channel_id': monitor.CHANNEL, 'content': 'wrong'}
    with pytest.raises(RuntimeError, match='readback_mismatch'):
        monitor.verified_send('expected', tmp_path, request=request)
    assert json.loads((tmp_path / 'pending-delivery.json').read_text())['message_id'] == 'sent'


# --- 2026-09-16 pass 2: the pool carries four credential labels for three accounts ---

ROSTER = ['codex-account-1', 'codex-account-2-real', 'codex-account-3']


def test_glp308_per_account_usage_and_unknown(tmp_path):
    result = monitor.tick(with_roster(tmp_path), notify=False, collector=pooled(LIVE_FOUR), now=NOW)
    assert len(monitor.openai_accounts(result['records'])) == 3
    result['records'].append({'provider': 'anthropic', 'label': 'claude', 'freshness': 'unavailable',
                              'weekly_remaining_pct': None, 'weekly_reset_at_utc': None})
    text = monitor.render_report(result, NOW)
    assert 'unknown · reset unknown · unavailable' in text
    assert '300 pts' not in text
    assert 'Pro-chat local ledger' in text


def test_glp308_zero_resets_historical_source():
    readings = monitor.load_json(monitor.STATE_DIR / 'OPERATOR-READINGS.json', {})
    assert readings['historical_superseded_not_current']['banked_reset_reading_20260921']['total_remaining'] == 0
    assert 'claude_fable_only_weekly' not in readings
    from account_capacity_collector import DEFAULT_OPERATOR_BANKED_RESET_METADATA
    assert sum(r['reported_count'] for r in DEFAULT_OPERATOR_BANKED_RESET_METADATA) == 0


def test_reset_rollout_keeps_reports_separate_from_live_capacity(tmp_path, monkeypatch):
    accounts = {'claude-code': {'provider': 'anthropic', 'reported_count': 1,
                               'status': 'operator_reported_available', 'expires_utc': None}}
    accounts.update({label: {'provider': 'openai-codex', 'announced_count': 1,
                            'status': 'rollout_pending_account_verification', 'expires_utc': None}
                     for label in ['codex-account-1', 'codex-account-2-real', 'codex-account-3']})
    banked = {'total_remaining': 1, 'pending_rollout_total': 3,
              'total_expected_after_rollout': 4, 'reported_on': '2026-09-22', 'accounts': accounts}
    (tmp_path / 'OPERATOR-READINGS.json').write_text(json.dumps({'banked_resets': banked}))
    monkeypatch.setattr(monitor, 'STATE_DIR', tmp_path)
    report = monitor.tick(tmp_path, notify=False, collector=collector(91), now=NOW,
                          sender=lambda *_: pytest.fail('must not send'))
    assert report['banked_resets_operator']['accounts'] == accounts
    assert report['banked_resets_operator']['pending_rollout_count'] == 3
    assert report['banked_resets_operator']['count'] == 1
    assert {r['label'] for r in report['operator_only']} == set(accounts)
    assert report['records'] == sample(91)['records']
    text = monitor.render_report(report, NOW, pacing={})
    assert 'Measured Codex banked resets:' in text and '3 pending rollout' not in text


def test_session_depletion_blocks_weekly_capacity_recommendation():
    report = sample()
    for record in report['records']:
        record.update(session_remaining_pct=0, weekly_remaining_pct=90, capacity_status='depleted')
    assert 'No measured Codex capacity remaining' in monitor.render_report(report, NOW)


def test_missing_pro_ledger_and_operator_counts_are_unknown():
    report = sample()
    for record in report['records']:
        if record.get('provider') == 'openai-codex':
            record['operator_only']['banked_reset_count'] = None
    report['pro_chat_ledger'] = [{'label': 'Pro relay', 'submitted_7d': None}]
    report['banked_resets_operator'] = {'count': None, 'reported_on': None}
    text = monitor.render_report(report, NOW)
    assert 'unknown (unavailable)' in text and 'Banked resets: unknown' in text
    assert 'None' not in text


def test_glp308_digest_separates_local_guards():
    report = sample()
    report['pro_chat_ledger'] = monitor.pro_ledger(datetime.now(timezone.utc))
    report['banked_resets_operator'] = {'count': 0, 'reported_on': '2026-09-21'}
    text = monitor.render_report(report, NOW)
    assert len(report['pro_chat_ledger']) == 3
    assert sorted(r['local_weekly_guard'] for r in report['pro_chat_ledger']) == [50, 200, 200]
    assert 'Measured Codex banked resets:' in text and 'Fable-only' not in text
    assert 'not provider quota' in text and 'useful work' in text
    assert len(text) <= 2000
    assert monitor.discord_fmt.lint(text, 'report')['status'] == 'pass'


def pooled(labels_and_shape):
    """One collector sample from (label, weekly_remaining_pct, weekly_reset_at_utc) triples."""
    records = [{'provider': 'openai-codex', 'label': label, 'weekly_remaining_pct': pct,
                'weekly_reset_at_utc': reset, 'capacity_status': 'depleted' if pct == 0 else 'available',
                'freshness': 'live', 'error_class': None, 'operator_only': {'banked_reset_count': 0}}
               for label, pct, reset in labels_and_shape]
    return lambda **_: SimpleNamespace(to_dict=lambda **__: {
        'records': records, 'collection_errors': [], 'collected_at_utc': NOW.isoformat()})


def with_roster(tmp_path, roster=ROSTER):
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / 'ACTIVE-POOL.json').write_text(json.dumps({'mode': 'sequential', 'expected_order': roster}))
    return tmp_path


LIVE_FOUR = [('codex-account-1', 0.0, '2026-09-19T08:14:05Z'),
             ('codex-account-2', 0.0, '2026-09-19T08:12:51Z'),
             ('codex-account-2-real', 0.0, '2026-09-19T08:12:51Z'),
             ('codex-account-3', 98.0, '2026-09-23T00:56:47Z')]


def test_alias_credential_label_is_folded_onto_its_account(tmp_path):
    """Four pooled labels, three real accounts: count the accounts, not the credentials."""
    result = monitor.tick(with_roster(tmp_path), notify=False, collector=pooled(LIVE_FOUR), now=NOW)
    assert result['openai_account_labels'] == ROSTER
    assert result['expected_openai_accounts'] == 3
    assert result['account_count_ok'] and result['collection_errors'] == []
    assert result['openai_alias_labels'] == {'codex-account-2': 'codex-account-2-real'}
    alias = next(r for r in result['records'] if r['label'] == 'codex-account-2')
    assert alias['duplicate_of'] == 'codex-account-2-real'  # kept as evidence, never dropped
    assert [r['label'] for r in monitor.openai_accounts(result['records'])] == ROSTER


def test_alias_is_not_summed_into_the_pool_total_and_is_named_in_the_report(tmp_path):
    result = monitor.tick(with_roster(tmp_path), notify=False, collector=pooled(LIVE_FOUR), now=NOW)
    text = monitor.render_report(result, NOW, {'active_openai_pool': 'codex-account-3'})
    assert '300 pts' not in text
    assert '**Codex codex-account-3**: 98% left' in text
    assert 'aliases counted once' in text
    assert '**Codex codex-account-2**' not in text


def test_a_roster_account_that_vanishes_keeps_failing_the_count(tmp_path):
    """The old previous-sample baseline alarmed once and then ratified the smaller pool."""
    short = [t for t in LIVE_FOUR if t[0] != 'codex-account-3']
    directory = with_roster(tmp_path)
    first = monitor.tick(directory, notify=False, collector=pooled(short), now=NOW)
    second = monitor.tick(directory, notify=False, collector=pooled(short), now=NOW)
    for result in (first, second):
        assert not result['account_count_ok']
        assert 'openai_account_set_changed' in result['collection_errors']
        assert result['expected_openai_accounts'] == 3


def test_two_roster_accounts_are_never_collapsed_on_a_signature_match(tmp_path):
    """Only a label outside the declared roster may fold; declared accounts are reported."""
    same = [('codex-account-1', 0.0, '2026-09-19T08:14:05Z'),
            ('codex-account-2-real', 0.0, '2026-09-19T08:14:05Z'),
            ('codex-account-3', 98.0, '2026-09-23T00:56:47Z')]
    result = monitor.tick(with_roster(tmp_path), notify=False, collector=pooled(same), now=NOW)
    assert result['openai_account_labels'] == ROSTER
    assert 'openai_alias_labels' not in result
    assert result['openai_duplicate_account_signature'] == [['codex-account-1', 'codex-account-2-real']]


def test_routing_refuses_a_drained_pool_even_when_the_pacing_ledger_names_it(tmp_path):
    """Neither selector may broadcast a depleted pool while another has capacity."""
    result = monitor.tick(with_roster(tmp_path), notify=False, collector=pooled(
        [('codex-account-1', 0.0, '2026-09-19T08:14:05Z'),
         ('codex-account-3', 98.0, '2026-09-23T00:56:47Z')], ), now=NOW)
    advice = monitor.routing_advice(result, NOW, {'active_openai_pool': 'codex-account-1'})
    assert 'codex-account-3' in advice and 'codex-account-1' not in advice
