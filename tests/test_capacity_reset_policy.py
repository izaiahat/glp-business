from datetime import datetime, timezone
import importlib.util
from pathlib import Path


def test_account2_must_hit_zero_before_selecting_later_reset_account():
    source = Path(__file__).resolve().parents[1] / 'scripts/capacity_reset_policy.py'
    assert source.exists(), 'automatic reset policy missing'
    spec = importlib.util.spec_from_file_location('reset_policy', source)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    now = datetime(2026, 10, 9, 22, tzinfo=timezone.utc)
    rows = [dict(label=label, remaining_pct=left, reset_at_utc=reset, banked_resets=bank,
                 identity_safe=True) for label, left, reset, bank in [
        ('codex-account-2', 50, '2026-10-15T00:00:00Z', 2),
        ('codex-account-3', 0, '2026-10-14T00:00:00Z', 2),
        ('openai-codex-oauth-4', 0, '2026-10-20T00:00:00Z', 1)]]
    assert m.plan_reset(rows, now)['status'] == 'not_exhausted'
    rows[0]['remaining_pct'] = 0
    assert m.plan_reset(rows, now)['selected_account'] == 'codex-account-2'
    rows[1]['reset_at_utc'] = '2026-10-16T00:00:00Z'
    assert m.plan_reset(rows, now)['selected_account'] == 'codex-account-3'
    rows[0]['banked_resets'] = rows[1]['banked_resets'] = 0
    assert m.plan_reset(rows, now)['status'] == 'no_eligible_reset'


def test_transaction_dry_run_no_post_uncertain_post_never_retried(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import capacity_reset_policy as m
    assert hasattr(m, 'reset_tick'), 'durable reset transaction missing'
    now = datetime(2026, 10, 9, 22, tzinfo=timezone.utc)
    rows = [dict(label=label, remaining_pct=0, reset_at_utc=reset, banked_resets=2,
                 identity_safe=True) for label, reset in [
        ('codex-account-2', '2026-10-15T00:00:00Z'),
        ('codex-account-3', '2026-10-14T00:00:00Z')]]
    reads, posts = [], []
    def read():
        reads.append(True)
        return rows, {'codex-account-2': {'access_token': 'fixture', 'base_url': ''}}
    def post(row, request_id):
        posts.append(request_id)
        raise TimeoutError('never serialize a token-bearing exception')
    preview = m.reset_tick(tmp_path, now=now, reader=read, consumer=post)
    assert preview['status'] == 'eligible' and preview['dry_run'] is True
    assert not posts and not list(tmp_path.iterdir())
    result = m.reset_tick(tmp_path, now=now, dry_run=False, reader=read, consumer=post)
    assert result['status'] == 'uncertain' and len(posts) == 1
    assert m.reset_tick(tmp_path, now=now, dry_run=False, reader=read, consumer=post)['status'] == 'reconciliation_required'
    assert len(posts) == 1 and len(reads) >= 3
    assert 'token-bearing' not in (tmp_path / 'reset-redemption.json').read_text()


def test_meter_is_not_read_before_oct10_and_digest_contains_fields(monkeypatch, tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import subscription_capacity_monitor as m
    assert hasattr(m, 's7_extensions'), 'S7 digest integration missing'
    monkeypatch.setattr(m, 's7_reset_tick', lambda *a, **kw: {'status': 'not_exhausted', 'accounts': []})
    monkeypatch.setattr(m, 's7_credit_snapshot', lambda: {'remaining_usd': 8.74, 'expires': '2026-10-11T00:00:00Z'})
    calls = []
    def meter(**kw):
        calls.append(kw)
        return {'snapshot': {'month_execution_high_water': 10, 'projected_month_end': 100,
                             'generated_at_utc': kw['now'].isoformat(), 'status': 'PASS'}}
    monkeypatch.setattr(m, 's7_meter_read', meter)
    before = m.s7_extensions(tmp_path, datetime(2026, 10, 9, 23, tzinfo=timezone.utc))
    assert not calls and before['n8n_meter']['status'] == 'WAITING_OCT10'
    after = m.s7_extensions(tmp_path, datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert len(calls) == 1 and after['n8n_executions_month'] == 10
    assert after['n8n_projected_month_end'] == 100
    assert after['n8n_meter']['billing_headroom'] is None


def test_reset_verified_clears_only_selected_account(tmp_path):
    import capacity_reset_policy as m
    now = datetime(2026, 10, 9, 22, tzinfo=timezone.utc)
    rows = [dict(label='codex-account-2', remaining_pct=0, reset_at_utc='2026-10-15T00:00:00Z',
                 banked_resets=2, identity_safe=True)]
    posts, cleared = [], []
    def read():
        return rows, {'codex-account-2': {'access_token': 'selected-fixture'}}
    def post(row, request):
        posts.append(request)
        rows[0].update(remaining_pct=100, banked_resets=1)
        return {'code': 'reset'}
    got = m.reset_tick(tmp_path, now=now, dry_run=False, reader=read, consumer=post, clear=cleared.append)
    assert got['status'] == 'redeemed_verified'
    assert len(posts) == 1 and cleared == ['selected-fixture']
    m.reset_tick(tmp_path, now=now, dry_run=False, reader=read, consumer=post, clear=cleared.append)
    assert len(posts) == 1


def test_account3_redemption_does_not_spend_again_for_same_trigger(tmp_path):
    import copy
    import capacity_reset_policy as m
    now = datetime(2026, 10, 9, 22, tzinfo=timezone.utc)
    rows = [dict(label=label, remaining_pct=0, reset_at_utc=reset, banked_resets=2,
                 identity_safe=True) for label, reset in [
        ('codex-account-2', '2026-10-15T00:00:00Z'),
        ('codex-account-3', '2026-10-16T00:00:00Z')]]
    posts = []
    def read():
        return copy.deepcopy(rows), {r['label']: {'label': r['label'], 'access_token': 'fixture'} for r in rows}
    def post(handle, request):
        posts.append(handle['label'])
        selected = next(r for r in rows if r['label'] == handle['label'])
        selected.update(remaining_pct=100, banked_resets=1)
        return {'code': 'reset'}
    assert m.reset_tick(tmp_path, now=now, dry_run=False, reader=read,
                        consumer=post, clear=lambda _: None)['status'] == 'redeemed_verified'
    assert m.reset_tick(tmp_path, now=now, dry_run=False, reader=read,
                        consumer=post, clear=lambda _: None)['status'] == 'episode_already_attempted'
    assert posts == ['codex-account-3']


def test_duplicate_identity_and_stale_reset_fail_closed():
    import capacity_reset_policy as m
    now = datetime(2026, 10, 9, 22, tzinfo=timezone.utc)
    row = dict(label='codex-account-2', remaining_pct=0, reset_at_utc='2026-10-15T00:00:00Z',
               banked_resets=2, identity_safe=False)
    assert m.plan_reset([row], now)['status'] == 'unsafe_identity'
    row.update(identity_safe=True, reset_at_utc='2026-10-09T00:00:00Z')
    assert m.plan_reset([row], now)['status'] == 'no_eligible_reset'


def test_s7_render_preserves_live_coverage_and_banked_facts():
    import subscription_capacity_monitor as m
    now = datetime(2026, 10, 9, 22, tzinfo=timezone.utc)
    report = {'auto_reset': {'status': 'not_exhausted', 'accounts': [
        dict(label=l, remaining_pct=p, reset_at_utc='2026-10-15T00:00:00Z',
             banked_resets=b, identity_safe=True)
        for l,p,b in [('codex-account-2',50,2),('codex-account-3',0,2),('openai-codex-oauth-4',0,1)]]},
        'account_count_ok': False, 'collection_errors': ['openai_account_set_changed'],
        'claude_api': {'remaining_usd': 8.74, 'expires': '2026-10-11T00:00:00Z'},
        'n8n_meter': {'status': 'WAITING_OCT10'}}
    text = m.render_report(report, now)
    assert 'openai_account_set_changed' in text
    assert '5 across 3 observed accounts' in text
    assert m.discord_fmt.lint(text, 'report')['status'] == 'pass'
    assert len(text) <= 2000


def test_scoped_reconciliation_retains_edits_and_refuses_drift(tmp_path, monkeypatch):
    import hashlib
    import json
    import subprocess
    spec = importlib.util.spec_from_file_location('r5_sync', Path(__file__).resolve().parents[1] / 'scripts/sync_from_repo.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert hasattr(m, 'CAPACITY_RECONCILIATION'), 'fixed live-edit reconciliation missing'
    repo, live = tmp_path / 'repo', tmp_path / 'live'
    repo.mkdir(); live.mkdir(); (live / 'ops').mkdir(); (repo / 'ops').mkdir()
    paths = ('scripts/subscription_capacity_monitor.py', 'tests/test_subscription_capacity_monitor.py')
    digest = lambda b: hashlib.sha256(b).hexdigest()
    pins, files, baselines = {}, {}, {'unselected': 'leave-alone'}
    for rel in paths:
        old, before, after = b'old\n', b'preserved live edit\n', b'preserved live edit\nnew feature\n'
        (repo / rel).parent.mkdir(exist_ok=True); (live / rel).parent.mkdir(exist_ok=True)
        (repo / rel).write_bytes(after); (live / rel).write_bytes(before)
        pins[rel] = (digest(old), digest(before), digest(after))
        files[rel] = {'sha256': digest(after)}; baselines[rel] = digest(old)
    (repo / m.MANIFEST).write_text(json.dumps({'schema': 'GLP_BUSINESS_SOURCE_V1', 'files': files, 'tracked_count': 3}))
    (live / m.STATE).write_text(json.dumps({'hashes': baselines}))
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    monkeypatch.setattr(m, 'CAPACITY_RECONCILIATION', pins)
    assert m.sync(live, repo, False, only=paths) == 2
    state = (live / m.STATE).read_bytes()
    assert m.sync(live, repo, False, only=paths, capacity_reconciliation=True) == 0
    assert (live / m.STATE).read_bytes() == state
    (live / paths[0]).write_bytes(b'unreviewed new edits')
    assert m.sync(live, repo, True, only=paths, capacity_reconciliation=True) == 2
    assert (live / m.STATE).read_bytes() == state
    (live / paths[0]).write_bytes(b'preserved live edit\n')
    missing_baseline = dict(baselines)
    del missing_baseline[paths[0]]
    (live / m.STATE).write_text(json.dumps({'hashes': missing_baseline}))
    assert m.sync(live, repo, True, only=paths, capacity_reconciliation=True) == 2
    (live / m.STATE).write_bytes(state)
    assert m.sync(live, repo, True, only=paths, capacity_reconciliation=True) == 0
    assert all((live / rel).read_bytes() == (repo / rel).read_bytes() for rel in paths)
    assert json.loads((live / m.STATE).read_text())['hashes']['unselected'] == 'leave-alone'
    assert m.sync(live, repo, False, only=paths) == 0


def test_s7_render_includes_three_accounts_and_balance():
    import subscription_capacity_monitor as m
    now = datetime(2026, 10, 9, 22, tzinfo=timezone.utc)
    report = {'auto_reset': {'status': 'not_exhausted', 'accounts': [
        dict(label=l, remaining_pct=p, reset_at_utc='2026-10-15T00:00:00Z', banked_resets=b)
        for l,p,b in [('codex-account-2',50,2),('codex-account-3',0,2),('openai-codex-oauth-4',0,1)]]},
        'claude_api': {'remaining_usd': 8.74, 'expires': '2026-10-11T00:00:00Z'},
        'n8n_meter': {'status': 'WAITING_OCT10'}}
    text = m.render_s7_report(report, now)
    assert '50% included left' in text and '$8.74' in text
    assert 'codex-account-3' in text and 'oauth-4' in text and len(text) < 2000
