#!/usr/bin/env python3
"""Authenticated host custody for Affiliati release intents (not conversions).

Loopback ingress is for registered host callers. No unauthenticated public listener,
new credentials, n8n waits or arbitrary commands. ACK follows SQLite FULL commit.
An uncertain bridge POST stays reserved forever until exact operator reconciliation.
"""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request

ROOT = Path('/home/ubuntu/business')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
STATE = Path.home() / '.hermes/state/glp-host-ingress'
TOKEN = Path.home() / '.hermes/secrets/n8n-bridge/token'
BASE = 'http://127.0.0.1:8791'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def connect(path=None):
    p = Path(path or STATE / 'intents.sqlite3')
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    c = sqlite3.connect(p, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA synchronous=FULL')
    c.execute('CREATE TABLE IF NOT EXISTS intents (intent_id TEXT PRIMARY KEY, digest TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, job_id TEXT, receipt TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
    c.commit()
    return c


def accept(c, body):
    if not isinstance(body, dict) or set(body) != {'intent_id', 'kind', 'payload'}:
        raise ValueError('invalid_envelope')
    key, kind, payload = body['intent_id'], body['kind'], body['payload']
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,160}', key):
        raise ValueError('invalid_intent_id')
    if kind not in {'affiliati_release', 'self_test', 'cogny_completion'} or not isinstance(payload, dict):
        raise ValueError('invalid_kind')
    if kind in {'affiliati_release', 'self_test'} and payload != {}:
        raise ValueError('no_release_argument_overrides')
    if kind == 'cogny_completion' and (set(payload) != {'job_id', 'task_id'} or any(
            not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', v) for v in payload.values())):
        raise ValueError('invalid_callback_binding')
    raw = json.dumps(body, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with c:
        c.execute('INSERT OR IGNORE INTO intents VALUES (?,?,?,?,?,NULL,NULL,?,?)',
                  (key, digest, kind, json.dumps(payload), 'acknowledged', now(), now()))
        row = c.execute('SELECT * FROM intents WHERE intent_id=?', (key,)).fetchone()
        if row['digest'] != digest:
            raise ValueError('intent_conflict')
    return public(row)


def public(row):
    return {k: row[k] for k in ('intent_id', 'kind', 'status', 'job_id', 'created_at', 'updated_at')}


def bridge(path, payload=None):
    req = urllib.request.Request(BASE + path, headers={
        'Authorization': 'Bearer ' + TOKEN.read_text().strip(),
        'Content-Type': 'application/json'},
        data=None if payload is None else json.dumps(payload).encode())
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.load(response)


def update(c, key, **values):
    values['updated_at'] = now()
    with c:
        c.execute('UPDATE intents SET ' + ','.join(k+'=?' for k in values) + ' WHERE intent_id=?',
                  (*values.values(), key))


def release_receipt(job):
    if job.get('status') != 'done' or type(job.get('rc')) is not int or job['rc'] != 0:
        raise ValueError('release_job_failed')
    for line in reversed(str(job.get('stdout_tail') or '').splitlines()):
        try:
            result = json.loads(line)
        except ValueError:
            continue
        if isinstance(result, dict) and result.get('receipt') and result.get('status') in {
                'PASS', 'PASS_VERSIONED_ASSET_LIVE', 'SKIP_ALREADY_INSTALLED'}:
            return result
    raise ValueError('release_terminal_receipt_missing')


def drain(c, call=bridge):
    # Caller holds the stable drain flock, including through external POST/GET.
    rows = c.execute("SELECT * FROM intents WHERE status IN ('acknowledged','running') ORDER BY created_at").fetchall()
    results = []
    for row in rows:
        key, kind = row['intent_id'], row['kind']
        if kind == 'self_test':
            update(c, key, status='completed', receipt=json.dumps({'self_test': True, 'deploys': 0}))
        elif kind == 'cogny_completion':
            from scripts import glp_cogny_queue_drain as cogny
            from ops.glp.cogny_client import connect as cogny_connect
            payload = json.loads(row['payload'])
            result = cogny.mode_resume(cogny_connect(retries=1), payload['job_id'], payload['task_id'])
            update(c, key, status='completed' if result.get('ok') else 'held', receipt=json.dumps(result))
        else:
            job = None
            if row['status'] == 'acknowledged':
                # Reserve before the only effectful POST. Crash/lost ACK never replays.
                update(c, key, status='post_uncertain')
                job = call('/run', {'action': 'script', 'name': 'affiliati_postback_ship', 'args': {}})
                if not isinstance(job, dict) or not job.get('job_id'):
                    raise ValueError('bridge_ack_unknown_no_replay')
                update(c, key, status='running', job_id=job['job_id'])
            else:
                job = call('/job/' + row['job_id'])
                if job.get('job_id') != row['job_id']:
                    raise ValueError('bridge_readback_identity_mismatch')
            if job.get('status') in {'done', 'error'}:
                try:
                    receipt = release_receipt(job)
                except ValueError as exc:
                    update(c, key, status='held', receipt=json.dumps({'reason': str(exc), 'job_id': job['job_id']}))
                else:
                    update(c, key, status='completed', receipt=json.dumps(receipt))
        results.append(public(c.execute('SELECT * FROM intents WHERE intent_id=?', (key,)).fetchone()))
    return {'schema': 'glp.host-ingress-drain.v1', 'at': now(), 'results': results,
            'unresolved': c.execute("SELECT count(*) FROM intents WHERE status NOT IN ('completed')").fetchone()[0]}


def handler(db_path=None, token_file=TOKEN):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No request bodies, authorization or callback capabilities in logs.

        def reply(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def authenticated(self):
            token = token_file.read_text().strip()
            return bool(token) and hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)

        def do_GET(self):
            if not self.authenticated():
                return self.reply(401, {'error': 'unauthorized'})
            if self.path == '/health':
                return self.reply(200, {'status': 'ready', 'service': 'glp-host-ingress'})
            if not self.path.startswith('/intents/'):
                return self.reply(404, {'error': 'not_found'})
            with connect(db_path) as c:
                row = c.execute('SELECT * FROM intents WHERE intent_id=?', (self.path[len('/intents/'):],)).fetchone()
                self.reply(200 if row else 404, public(row) if row else {'error': 'not_found'})

        def do_POST(self):
            if not self.authenticated():
                return self.reply(401, {'error': 'unauthorized'})
            if self.path != '/intents':
                return self.reply(404, {'error': 'not_found'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 8192:
                    raise ValueError('invalid_body_size')
                body = json.loads(self.rfile.read(size))
                with connect(db_path) as c:
                    result = accept(c, body)
            except ValueError:
                return self.reply(409, {'error': 'invalid_or_conflicting_intent'})
            self.reply(202, result)
    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('mode', choices=['serve', 'drain'])
    ap.add_argument('--port', type=int, default=8790)
    ns = ap.parse_args()
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    if ns.mode == 'serve':
        if not TOKEN.read_text().strip():
            raise ValueError('existing_auth_token_missing')
        ThreadingHTTPServer(('127.0.0.1', ns.port), handler()).serve_forever()
    else:
        with (STATE / 'drain.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({'status': 'busy'}))
                return 0
            with connect() as c:
                result = drain(c)
            from scripts import glp_cogny_queue_drain as cogny
            from ops.glp.cogny_client import connect as cogny_connect
            result['cogny'] = cogny.mode_tick(cogny_connect(retries=1))
            temp = STATE / 'latest-drain.tmp'
            temp.write_text(json.dumps(result, indent=2))
            os.replace(temp, STATE / 'latest-drain.json')
            print(json.dumps(result))
            return 0 if not result['unresolved'] and result['cogny'].get('ok') else 2
    return 0


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
