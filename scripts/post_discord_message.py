#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path('/home/ubuntu/.hermes/.env')


def load_token() -> str:
    text = ENV_PATH.read_text()
    m = re.search(r'^DISCORD_BOT_TOKEN=(.+)$', text, re.M)
    if not m:
        raise RuntimeError('DISCORD_BOT_TOKEN missing from ~/.hermes/.env')
    return m.group(1).strip().strip('"').strip("'")


def send(channel_id: str, content: str) -> dict:
    # This notification-only helper suppresses structured mentions. Registered
    # reviews must use bridge_authority_guard.py send-successor, never this path.
    if 'TRUE-BRIDGE FABLE REVIEW REQUEST' in content.upper():
        raise RuntimeError('registered_bridge_sender_required')
    token = load_token()
    req = urllib.request.Request(
        f'https://discord.com/api/v10/channels/{channel_id}/messages',
        data=json.dumps({'content': content, 'allowed_mentions': {'parse': []}}).encode('utf-8'),
        headers={
            'Authorization': f'Bot {token}',
            'Content-Type': 'application/json',
            'User-Agent': 'HermesDiscordPoster/1.0',
        },
        method='POST',
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            # A 429 explicitly rejects the POST; unlike a timeout/5xx it is
            # not an ambiguous effect. Never retry any other failure.
            if exc.code != 429 or attempt == 3:
                raise
            payload = json.loads(exc.read().decode('utf-8'))
            delay = payload.get('retry_after')
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 30:
                raise
            print('DISCORD_POST_RATE_REJECTED: explicit HTTP 429; bounded retry', file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError('discord_rate_retry_exhausted')


def readback(channel_id: str, message_id: str) -> dict:
    token = load_token()
    req = urllib.request.Request(
        f'https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}',
        headers={
            'Authorization': f'Bot {token}',
            'User-Agent': 'HermesDiscordPoster/1.0',
        },
        method='GET',
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--message')
    group.add_argument('--file')
    args = parser.parse_args()

    if args.file:
        content = Path(args.file).read_text()
    else:
        content = args.message or ''
    if not content.strip():
        raise RuntimeError('empty message')

    sent_content = content[:1900].rstrip('\r\n')
    payload = send(args.channel, sent_content)
    message_id = str(payload.get('id') or '')
    if not message_id:
        raise RuntimeError('Discord POST response missing message ID')
    fetched = readback(args.channel, message_id)
    exact_readback = fetched.get('channel_id') == args.channel and fetched.get('content') == sent_content
    if not exact_readback:
        raise RuntimeError(json.dumps({
            'error': 'Discord exact readback failed',
            'message_id': message_id,
            'channel_match': fetched.get('channel_id') == args.channel,
            'sent_length': len(sent_content),
            'readback_length': len(str(fetched.get('content') or '')),
            'sent_sha256': hashlib.sha256(sent_content.encode()).hexdigest(),
            'readback_sha256': hashlib.sha256(str(fetched.get('content') or '').encode()).hexdigest(),
            'secret_printed': False,
        }, sort_keys=True))
    print(json.dumps({
        'ok': True,
        'message_id': message_id,
        'channel_id': args.channel,
        'exact_readback': True,
        'allowed_mentions_parse': [],
    }, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
