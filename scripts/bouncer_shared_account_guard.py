#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

REGISTRY = Path('/home/ubuntu/.hermes/secrets/bouncer/venture_registry.json')
LEDGER = Path('/home/ubuntu/business/reports/bouncer-shared-credit-ledger-2026-06-25.jsonl')
API_BASE = 'https://api.usebouncer.com'


def load_env(path: Path) -> dict[str, str]:
    vals: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.strip().startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        vals[k.strip()] = v.strip()
    return vals


def load_registry() -> dict:
    return json.loads(REGISTRY.read_text())


def bouncer_get(path: str, key: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        API_BASE + path,
        headers={
            'x-api-key': key,
            'Accept': 'application/json',
            'User-Agent': 'Hermes shared Bouncer guard',
        },
        method='GET',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8', 'replace')
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', 'replace')
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {'error': type(exc).__name__}
        return exc.code, body


def normalize_email(value: str) -> str:
    value = (parseaddr(value or '')[1] or value or '').strip().lower()
    return value.strip('.,;:()[]<>"\'')


def email_hash(value: str) -> str:
    return hashlib.sha256(normalize_email(value).encode()).hexdigest()


def email_domain(value: str) -> str:
    value = normalize_email(value)
    return value.rsplit('@', 1)[1] if '@' in value else ''


def load_shared_key(registry: dict) -> str:
    shared_env = Path(registry['shared_env'])
    env = load_env(shared_env)
    key = env.get('BOUNCER_API_KEY')
    if not key:
        raise RuntimeError('missing_bouncer_api_key')
    return key


def bouncer_verify_email(key: str, email: str, timeout: int = 10) -> tuple[int, dict]:
    qs = urllib.parse.urlencode({'email': normalize_email(email), 'timeout': str(timeout)})
    return bouncer_get('/v1.1/email/verify?' + qs, key)


def send_decision(body: dict) -> dict:
    status = str(body.get('status') or '').lower()
    score = body.get('score')
    domain = body.get('domain') if isinstance(body.get('domain'), dict) else {}
    account = body.get('account') if isinstance(body.get('account'), dict) else {}
    toxicity = body.get('toxicity')
    blockers: list[str] = []
    if status != 'deliverable':
        blockers.append('bouncer_status_not_deliverable:' + (status or 'missing'))
    if isinstance(score, (int, float)) and score < 80:
        blockers.append(f'bouncer_score_below_80:{score}')
    if str(domain.get('disposable') or '').lower() == 'yes':
        blockers.append('bouncer_disposable_domain')
    if str(account.get('disabled') or '').lower() == 'yes':
        blockers.append('bouncer_disabled_account')
    if str(account.get('fullMailbox') or '').lower() == 'yes':
        blockers.append('bouncer_full_mailbox')
    if isinstance(toxicity, (int, float)) and toxicity >= 4:
        blockers.append(f'bouncer_toxicity_ge_4:{toxicity}')
    return {
        'send_eligible': not blockers,
        'decision': 'send_eligible' if not blockers else 'do_not_send',
        'blockers': blockers,
    }


def get_credits(registry: dict) -> tuple[int, dict]:
    try:
        key = load_shared_key(registry)
    except RuntimeError:
        return 0, {'error': 'missing_bouncer_api_key'}
    return bouncer_get('/v1.1/credits', key)


def cmd_status(args: argparse.Namespace) -> int:
    registry = load_registry()
    code, body = get_credits(registry)
    credits = body.get('credits') if isinstance(body, dict) else None
    print(json.dumps({
        'status': 'pass' if code == 200 else 'fail',
        'http_status': code,
        'account_alias': registry.get('account_alias'),
        'credits': credits,
        'global_policy': registry.get('global_policy', {}),
        'ventures': sorted((registry.get('ventures') or {}).keys()),
        'registry_mode': oct(stat.S_IMODE(REGISTRY.stat().st_mode)),
        'shared_env_mode': oct(stat.S_IMODE(Path(registry['shared_env']).stat().st_mode)),
    }, indent=2, sort_keys=True))
    return 0 if code == 200 else 1


def cmd_preflight(args: argparse.Namespace) -> int:
    registry = load_registry()
    ventures = registry.get('ventures') or {}
    if args.venture not in ventures:
        print(json.dumps({'status': 'blocked', 'reason': 'unknown_venture', 'venture': args.venture}, sort_keys=True))
        return 2
    code, body = get_credits(registry)
    credits = body.get('credits') if isinstance(body, dict) else None
    requested = int(args.requested)
    reasons = []
    if code != 200:
        reasons.append(f'credits_endpoint_http_{code}')
    if not isinstance(credits, int):
        reasons.append('credits_unknown')
    elif requested > credits:
        reasons.append('insufficient_bouncer_credits')
    status = 'pass' if not reasons else 'blocked'
    print(json.dumps({
        'status': status,
        'venture': args.venture,
        'requested_verifications': requested,
        'credits': credits,
        'account_alias': registry.get('account_alias'),
        'reasons': reasons,
        'no_secret_output': True,
    }, indent=2, sort_keys=True))
    return 0 if status == 'pass' else 3


def cmd_verify_email(args: argparse.Namespace) -> int:
    registry = load_registry()
    ventures = registry.get('ventures') or {}
    venture = args.venture
    email = normalize_email(args.email)
    if venture not in ventures:
        print(json.dumps({'status': 'blocked', 'reason': 'unknown_venture', 'venture': venture}, sort_keys=True))
        return 2
    if '@' not in email or not email_domain(email):
        print(json.dumps({'status': 'blocked', 'reason': 'invalid_email_shape', 'venture': venture}, sort_keys=True))
        return 3
    key = load_shared_key(registry)
    # Per-email sends must not depend on the credits endpoint. That endpoint is
    # rate-limited separately and caused safe sends to fail closed with
    # `credits_endpoint_http_429`. The real-time verify endpoint remains the
    # authoritative fail-closed send/no-send gate; status/preflight commands
    # still use credits for planning.
    credits_before = None
    verify_code, verify_body = bouncer_verify_email(key, email, timeout=max(1, min(int(args.timeout), 30)))
    credits_after = None
    if verify_code == 200 and isinstance(verify_body, dict):
        decision = send_decision(verify_body)
    else:
        decision = {'send_eligible': False, 'decision': 'do_not_send', 'blockers': [f'bouncer_http_{verify_code}']}
    entry = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'venture': venture,
        'event': 'verify_email',
        'ref': args.ref,
        'email_hash': email_hash(email),
        'email_domain': email_domain(email),
        'http_status': verify_code,
        'provider_status': verify_body.get('status') if isinstance(verify_body, dict) else None,
        'provider_reason': verify_body.get('reason') if isinstance(verify_body, dict) else None,
        'score': verify_body.get('score') if isinstance(verify_body, dict) else None,
        'toxicity': verify_body.get('toxicity') if isinstance(verify_body, dict) else None,
        'decision': decision['decision'],
        'send_eligible': decision['send_eligible'],
        'blockers': decision['blockers'],
        'credits_before': credits_before,
        'credits_after': credits_after,
        'delta': None if not isinstance(credits_before, int) or not isinstance(credits_after, int) else credits_after - credits_before,
        'credits_endpoint_skipped_for_rate_limit_safety': True,
        'secret': 'redacted',
    }
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open('a') as fh:
        fh.write(json.dumps(entry, sort_keys=True) + '\n')
    out = {k: v for k, v in entry.items() if k != 'secret'}
    out['status'] = 'pass' if decision['send_eligible'] else 'blocked'
    out['no_secret_output'] = True
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if decision['send_eligible'] else 3


def cmd_record(args: argparse.Namespace) -> int:
    entry = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'venture': args.venture,
        'event': args.event,
        'credits_before': args.credits_before,
        'credits_after': args.credits_after,
        'delta': None if args.credits_before is None or args.credits_after is None else args.credits_after - args.credits_before,
        'report': args.report,
        'secret': 'redacted',
    }
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open('a') as fh:
        fh.write(json.dumps(entry, sort_keys=True) + '\n')
    print(json.dumps({'status': 'recorded', 'ledger': str(LEDGER), 'entry': entry}, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Shared Bouncer account guard for Hermes ventures')
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status')
    pre = sub.add_parser('preflight')
    pre.add_argument('--venture', required=True)
    pre.add_argument('--requested', type=int, required=True)
    rec = sub.add_parser('record')
    rec.add_argument('--venture', required=True)
    rec.add_argument('--event', required=True)
    rec.add_argument('--credits-before', type=int)
    rec.add_argument('--credits-after', type=int)
    rec.add_argument('--report', required=True)
    ver = sub.add_parser('verify-email')
    ver.add_argument('--venture', required=True)
    ver.add_argument('--email', required=True)
    ver.add_argument('--ref', default='')
    ver.add_argument('--timeout', type=int, default=10)
    args = parser.parse_args()
    if args.cmd == 'status':
        return cmd_status(args)
    if args.cmd == 'preflight':
        return cmd_preflight(args)
    if args.cmd == 'record':
        return cmd_record(args)
    if args.cmd == 'verify-email':
        return cmd_verify_email(args)
    raise AssertionError(args.cmd)


if __name__ == '__main__':
    raise SystemExit(main())
