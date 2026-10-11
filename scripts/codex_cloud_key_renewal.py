#!/usr/bin/env python3
"""Secret-free VPN expiry monitor. This is a manual-renewal fallback, not auto-renew.

No documented public Codex environment VPN-secret API was found. Minting or
transferring secrets outside secure vault tools is prohibited. Never revoke a
working key on a reminder, before a new Cloud GET, or from stale metadata.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.request

ROOT = Path('/home/ubuntu/business')
CONFIG = ROOT / 'ops/codex-cloud/tailscale-renewal.json'
STATE = ROOT / 'reports/ops/codex-cloud-key-renewal/state.json'
DECISIONS = '1524254375453786294'
OPS_ALERTS = '1487300132054765629'
DOC = 'https://learn.chatgpt.com/docs/environments/cloud-environments#private-networking-vpn'


def timestamp(s):
    value = datetime.fromisoformat(s.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('expiry_must_have_timezone')
    return value.astimezone(timezone.utc)


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.renewal-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def operator_steps(env):
    return (f"⚠️ **Codex VPN renewal — {env['name']}**\n"
            f"Account: {env['account']} · expires **{env['expires']}**\n"
            "No documented public Codex VPN-secret update API found. This job is a reminder, NOT auto-renew.\n"
            "1. In Tailscale Admin → Settings → Keys, generate a 7-day auth key: "
            "Reusable ON, Ephemeral ON, Pre-approved ON, only `tag:codex-cloud`. Do not alter ACLs.\n"
            "2. Keep the key in a private password manager/secure browser path only; never Discord, chat, logs or command arguments. "
            "No new key has been generated or delivered by this job.\n"
            f"3. Signed in as {env['account']}, open Settings → Codex Cloud → Environments → "
            f"{env['name']} → Edit → Advanced → VPN. Replace the Tailscale key, Save draft, then Republish. "
            "Wait for Environment published.\n"
            "4. Check included allowance first (no credits/resets). Start a NEW task in that environment; "
            "run proxy-respecting GET `http://hermes-01.tail9e25df.ts.net:18909/v1/fleet-status.json`; "
            "require HTTP 200 and `codex-evidence-v1`. Existing tasks are not a fresh-key proof.\n"
            f"5. Only after that proof, revoke OLD key id `{env['key_id']}`. "
            "Update the maintained nonsecret expiry/key-id registry with live metadata; attach the Cloud task/GET receipt.\n"
            f"Docs: {DOC}")


class Discord:
    def __init__(self):
        import post_discord_message as transport
        self.transport = transport

    def send(self, channel, content):
        return self.transport.send(channel, content)

    def readback(self, channel, ident):
        return self.transport.readback(channel, ident)

    def recent(self, channel):
        req = urllib.request.Request(
            f'https://discord.com/api/v10/channels/{channel}/messages?limit=100',
            headers={'Authorization': f'Bot {self.transport.load_token()}',
                     'User-Agent': 'HermesCodexRenewal/1.0'})
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read())


def notify(state, state_path, client, channel, key, content):
    """Journal-before-POST. Reconcile uncertain sends; never blindly retry."""
    footer = '\n-# renewal-ref: ' + hashlib.sha256(key.encode()).hexdigest()[:20]
    wire = content + footer
    if len(wire.encode('utf-16-le')) // 2 > 1900:
        raise ValueError('discord_message_too_long')
    rows = state.setdefault('notifications', {})
    row = rows.get(key)
    if row and row.get('status') == 'VERIFIED':
        return row
    if row and row.get('message_id'):
        fetched = client.readback(channel, row['message_id'])
    elif row:
        found = [m for m in client.recent(channel) if m.get('content') == wire
                 and m.get('channel_id') == channel and m.get('author', {}).get('bot')]
        if len(found) != 1:
            raise RuntimeError('notification_uncertain_manual_reconcile')
        fetched = found[0]
        row['message_id'] = fetched['id']; atomic(state_path, state)
    else:
        row = {'status': 'INTENT', 'channel_id': channel,
               'content_sha256': hashlib.sha256(wire.encode()).hexdigest()}
        rows[key] = row; atomic(state_path, state)
        posted = client.send(channel, wire)
        if not posted.get('id'): raise RuntimeError('notification_missing_id')
        row['message_id'] = posted['id']; atomic(state_path, state)
        fetched = client.readback(channel, posted['id'])
    if fetched.get('channel_id') != channel or fetched.get('content') != wire:
        raise RuntimeError('notification_readback_mismatch')
    row['status'] = 'VERIFIED'; atomic(state_path, state)
    return row


def run(config, state_path, now, client):
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    outcomes = []
    # Hourly checking at 73h gives the operator >=72h even between cron slots.
    for env in config['environments']:
        remaining = timestamp(env['expires']) - now
        due = remaining <= timedelta(hours=73)
        row = {'name': env['name'], 'key_id': env['key_id'], 'expires': env['expires'],
               'status': 'MANUAL_RENEWAL_REQUIRED' if due else 'NOT_DUE',
               'renewed': False, 'revoked': False}
        if due:
            key = f"steps:{env['environment_id']}:{env['key_id']}:{env['expires']}"
            row['notification'] = notify(state, state_path, client, DECISIONS, key, operator_steps(env))
            day = now.date().isoformat()
            alert_key = f"alert:{env['key_id']}:{day}"
            row['failure_alert'] = notify(state, state_path, client, OPS_ALERTS, alert_key,
                f"⚠️ **Codex VPN auto-renew unavailable** — {env['name']} expires {env['expires']}. "
                "Manual replacement required; exact secure-handling steps posted in #decisions. "
                "Old key/ACL unchanged; no fresh Cloud roundtrip. Automation must not be marked complete.")
        outcomes.append(row)
    result = {'status': 'MANUAL_RENEWAL_REQUIRED' if any(x['status']=='MANUAL_RENEWAL_REQUIRED' for x in outcomes) else 'NOT_DUE',
              'checked_at': now.isoformat(), 'mode': 'manual_fallback', 'environments': outcomes,
              'secret_generated': False, 'cloud_secret_updated': False, 'acl_changed': False,
              'limit': 'No documented public Codex VPN-secret update API; no secure vault mint/transfer broker available'}
    state['last_check'] = result; atomic(state_path, state)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--state', type=Path, default=STATE)
    parser.add_argument('--notify-blocker', action='store_true', help='Post the current missing automation prerequisite once')
    args = parser.parse_args(argv)
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = datetime.now(timezone.utc)
        client = Discord()
        try:
            config = json.loads(args.config.read_text())
            if args.notify_blocker:
                state = json.loads(args.state.read_text()) if args.state.exists() else {}
                notify(state, args.state, client, OPS_ALERTS, 'automation-prerequisite-r1-v1',
                    '⚠️ **Codex key renewal blocked** — supported VPN update is Codex UI only; '
                    'no documented public environment-secret API or secure vault key-mint/transfer broker available. '
                    'Tailscale Google sign-in returned “Couldn’t sign you in”; no existing authorized tailnet tab remains. '
                    'Both original keys still expire Oct 17. Hourly expiry reminders are the fallback, NOT auto-renew. '
                    'No key was minted/revoked, ACL changed, or fresh Cloud GET claimed.')
            result = run(config, args.state, now, client)
        except Exception as exc:
            # Exceptions from credential/network clients must never be printed.
            state = json.loads(args.state.read_text()) if args.state.exists() else {}
            result = {'status': 'FAIL', 'error_class': type(exc).__name__, 'secret_printed': False}
            try:
                notify(state, args.state, client, OPS_ALERTS, 'monitor-failure:'+now.date().isoformat(),
                    '⚠️ **Codex VPN expiry monitor failed** — inspect private renewal state. '
                    'No key/ACL changes; do not blindly replay uncertain Discord sends.')
            except Exception:
                result['alert_delivery'] = 'FAILED_OR_UNCERTAIN'
            atomic(args.state.parent / 'FAILURE.json', result)
            print(json.dumps(result)); return 1
    print(json.dumps(result)); return 0


if __name__ == '__main__':
    sys.exit(main())
