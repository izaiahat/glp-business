#!/usr/bin/env python3
"""Operator-approved included-allowance reset policy. No paid-credit fallback.

GET-only dry-run is the default. Only account-2 reaching 0% can trigger one
reset on account-2/3; prefer the exhausted account resetting furthest away.
An uncertain POST is latched permanently for reconciliation, never retried.
"""
from datetime import datetime, timezone

ALLOWED = ('codex-account-2', 'codex-account-3')


def plan_reset(rows, now):
    by_label = {r['label']: r for r in rows}
    if len(by_label) != len(rows):
        return {'status': 'unsafe_identity'}
    trigger = by_label.get(ALLOWED[0], {})
    if not trigger.get('identity_safe'):
        return {'status': 'unsafe_identity'}
    if trigger.get('remaining_pct') != 0:
        return {'status': 'not_exhausted', 'trigger_remaining_pct': trigger.get('remaining_pct')}
    candidates = []
    for label in ALLOWED:
        r = by_label.get(label, {})
        try:
            reset = datetime.fromisoformat(r['reset_at_utc'].replace('Z', '+00:00'))
            if (r.get('identity_safe') and r.get('remaining_pct') == 0 and
                    type(r.get('banked_resets')) is int and r['banked_resets'] > 0 and reset > now):
                candidates.append((reset, label))
        except (KeyError, TypeError, ValueError):
            pass
    if not candidates:
        return {'status': 'no_eligible_reset'}
    reset, label = max(candidates)
    return {'status': 'eligible', 'selected_account': label, 'reset_at_utc': reset.isoformat(),
            'banked_resets': by_label[label]['banked_resets'],
            'selection_reason': 'exhausted allowlisted account with furthest natural reset'}


def read_live():
    from agent.account_usage import _codex_headers, _codex_backend_urls
    from account_capacity_collector import _read_pool_rows, _default_usage_getter, _record_from_payload
    pool = _read_pool_rows()
    identities = [_codex_headers(r.get('access_token', ''), None).get('ChatGPT-Account-ID') for r in pool]
    rows, handles = [], {}
    now = datetime.now(timezone.utc).isoformat()
    for r, identity in zip(pool, identities):
        label = r.get('label', '')
        # Read only known pool labels, never serialize arbitrary upstream strings.
        if label not in (*ALLOWED, 'openai-codex-oauth-4'):
            continue
        safe = {'label': label, 'identity_safe': bool(identity and identities.count(identity) == 1)}
        try:
            payload = _default_usage_getter(_codex_backend_urls(r.get('base_url', ''))[0],
                                          _codex_headers(r['access_token'], None))
            rec = _record_from_payload(provider='openai-codex', label=label, payload=payload, fetched_at_utc=now)
            values = [v for v in (rec.session_remaining_pct, rec.weekly_remaining_pct) if v is not None]
            resets = [v for v in (rec.session_reset_at_utc, rec.weekly_reset_at_utc) if v]
            safe.update(remaining_pct=min(values) if values and not rec.error_class else None,
                        reset_at_utc=max(resets) if resets else None,
                        banked_resets=rec.operator_only.get('banked_reset_count'),
                        weekly_remaining_pct=rec.weekly_remaining_pct,
                        session_remaining_pct=rec.session_remaining_pct)
            handles[label] = r
        except Exception as exc:
            safe['error_class'] = type(exc).__name__
        rows.append(safe)
    return rows, handles


def _save(path, data):
    import json, os
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _consume(row, request_id):
    import httpx
    from agent.account_usage import _codex_headers, _codex_backend_urls
    response = httpx.post(_codex_backend_urls(row.get('base_url', ''))[2],
                          headers=_codex_headers(row['access_token'], None),
                          json={'redeem_request_id': request_id}, timeout=15)
    response.raise_for_status()
    return response.json()


def reset_tick(directory, *, now=None, dry_run=True, reader=read_live, consumer=_consume, clear=None):
    import fcntl, json, uuid
    from pathlib import Path
    now = now or datetime.now(timezone.utc)
    rows, handles = reader()
    result = dict(plan_reset(rows, now), dry_run=dry_run, accounts=rows, observed_at=now.isoformat())
    if dry_run:
        return result
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.reset-lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / 'reset-redemption.json'
        previous = json.loads(path.read_text()) if path.exists() else {}
        if previous.get('status') in ('intent', 'uncertain', 'reconciliation_required'):
            return dict(result, status='reconciliation_required', request_id=previous['request_id'])
        if result['status'] != 'eligible':
            return result
        # Re-read under lock before spending; recovery or a reset-date change cancels intent.
        rows, handles = reader()
        fresh = plan_reset(rows, now)
        if fresh != plan_reset(result['accounts'], now):
            return dict(result, status='changed_before_redemption')
        label = result['selected_account']
        episode = label + ':' + result['reset_at_utc']
        if previous.get('episode') == episode:
            return dict(result, status='episode_already_attempted')
        intent = {'status': 'intent', 'episode': episode, 'selected_account': label,
                  'request_id': str(uuid.uuid4()), 'before': rows, 'created_at': now.isoformat()}
        _save(path, intent)
        try:
            body = consumer(handles[label], intent['request_id'])
            code = body.get('code')
            intent['backend_code'] = code if code in ('reset', 'nothing_to_reset', 'no_credit', 'already_redeemed') else 'unknown'
            after, _ = reader()
            observed = next((r for r in after if r['label'] == label), {})
            intent['after'] = after
            verified = (code in ('reset', 'already_redeemed') and observed.get('identity_safe') and
                        observed.get('remaining_pct') is not None and observed['remaining_pct'] > 0 and
                        observed.get('banked_resets') == result['banked_resets'] - 1)
            if verified:
                if clear is None:
                    from hermes_cli.auth import clear_codex_pool_quota_cooldowns
                    clear = clear_codex_pool_quota_cooldowns
                clear(handles[label]['access_token'])
                intent['status'] = 'redeemed_verified'
            elif code in ('nothing_to_reset', 'no_credit'):
                intent['status'] = 'backend_refused'
            else:
                intent['status'] = 'uncertain'
        except Exception as exc:
            intent.update(status='uncertain', error_class=type(exc).__name__)
        _save(path, intent)
        return dict(result, status=intent['status'], request_id=intent['request_id'])


if __name__ == '__main__':
    import argparse, json
    p = argparse.ArgumentParser()
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    print(json.dumps(reset_tick('/home/ubuntu/business/ops/subscription-capacity', dry_run=not args.apply), indent=2))
