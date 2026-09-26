#!/usr/bin/env python3
"""GPT-6 Pro relay: drive the operator's ChatGPT web session (GPT-6 Pro) from files.

Authority: ops/linear/approvals/OPERATOR-DECISION-20260916-gpt6pro-browser-relay.json (option A, ToS risk
accepted in the operator's own words). Config: ops/gpt6pro-relay/config.json.

What it is: a read-think-write channel. A job = one prompt file (+ optional attachments) sent as ONE message to a
NEW GPT-6 Pro chat; its hash-bound reply lands in reports/ops/gpt6pro-relay/jobs/<job>/RESPONSE.md.
Inputs go to Drive inbox/<job>; Drive outbox artifacts require a final-chat-bound OUTPUT-MANIFEST.json.

What it is not: it never builds, tests, reads back a site, or causes a live effect. Every RESPONSE is UNTRUSTED
seat output and passes the same gates (result_lint grounding, protected-oracle review) before anything is applied.

Login: the browser profile (~/.hermes/browser-profiles/chatgpt-izaiah, CDP 127.0.0.1:9340) is signed in by the
OPERATOR himself over a VNC handoff. This script never enters, stores, prints or resets any credential. If the
session is logged out it stops with LOGGED_OUT and the operator repeats the handoff.

Budget: per-account weekly message count from the ledger; refuses at weekly_messages * (1 - reserve%).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")
CONFIG = BUSINESS / "ops/gpt6pro-relay/config.json"
JOBS = BUSINESS / "reports/ops/gpt6pro-relay/jobs"
UVX = Path(os.path.expanduser("~/.hermes/bin/uvx"))
SA_KEY = Path(os.path.expanduser("~/.hermes/secrets/glp-clinic-scout/google_workspace_dwd_service_account.json"))
BU_ENV = {"BU_NAME": "gpt6", "BU_CDP_URL": "http://127.0.0.1:9340", "BU_TAB_MARKER": "0"}  # default = account 1


def use_account(acct: dict) -> None:
    """Point the browser-use daemon at this account's Chrome (one profile + CDP port + daemon name per account)."""
    BU_ENV.update({"BU_NAME": acct.get("bu_name", "gpt6"), "BU_CDP_URL": f"http://127.0.0.1:{acct.get('cdp_port', 9340)}"})
MODEL_LABEL = re.compile(r"^\s*6\s*Pro\s*$")
MAX_MESSAGE_CHARS = 180_000
IPC_CHUNK = 6_000  # browser-harness IPC line limit (~64KB) and its 5s per-call response timeout: small chunks keep each insert fast
ATTACH_SEP = "\n\n===== ATTACHMENT: {name} =====\n"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config() -> dict:
    return json.loads(CONFIG.read_text())


def account_of(cfg: dict, label: str | None) -> dict:
    accts = cfg["accounts"]
    if not label:
        return accts[0]
    for a in accts:
        if a["label"] == label:
            return a
    raise SystemExit(f"REFUSED_UNKNOWN_ACCOUNT {label}")


# ---------------------------------------------------------------- ledger / budget
def ledger_path(cfg: dict) -> Path:
    return BUSINESS / cfg["ledger"]


def ledger_append(cfg: dict, rec: dict) -> None:
    p = ledger_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": now_iso(), **rec}
    with p.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
        fh.flush(); os.fsync(fh.fileno())


def ledger_rows(cfg: dict) -> list[dict]:
    p = ledger_path(cfg)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def messages_used_7d(cfg: dict, account: str) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    n = 0
    for r in ledger_rows(cfg):
        if r.get("account") == account and r.get("event") == "submitted":
            try:
                if datetime.fromisoformat(r["ts"]) >= cutoff:
                    n += 1
            except (KeyError, ValueError):
                continue
    return n


def budget_check(cfg: dict, acct: dict) -> tuple[int, int]:
    """Return (used, limit); raise SystemExit when the next message would exceed the reserve line."""
    used = messages_used_7d(cfg, acct["label"])
    limit = int(acct["weekly_messages"] * (1 - cfg.get("budget_reserve_pct", 10) / 100))
    if used + 1 > limit:
        raise SystemExit(f"REFUSED_BUDGET account={acct['label']} used_7d={used} limit={limit} "
                         f"(weekly={acct['weekly_messages']} reserve={cfg.get('budget_reserve_pct', 10)}%)")
    return used, limit


# One lock per Chrome endpoint (account aliases must not get independent locks). The CLI is single-threaded.
_HELD_LOCKS = set()


@contextmanager
def account_lock():
    key = hashlib.sha256(BU_ENV["BU_CDP_URL"].encode()).hexdigest()
    if key in _HELD_LOCKS:
        yield
        return
    locks = BUSINESS / "ops/gpt6pro-relay/locks"
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / f"{key}.lock").open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        _HELD_LOCKS.add(key)
        try:
            yield
        finally:
            _HELD_LOCKS.remove(key)
            fcntl.flock(fh, fcntl.LOCK_UN)


# Shared verbatim with the browser-use script; compare complete ids, not URL prefixes.
BINDING_URL_CODE = r'''
def _conversation(url):
    match = re.fullmatch(r"https://chatgpt[.]com/c/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?(?:[?#].*)?", url or "")
    return "https://chatgpt.com/c/" + match[1] if match else None
'''
exec(BINDING_URL_CODE)


PROVENANCE_FIELDS = ('dispatcher_profile', 'hermes_session_id', 'linear_child', 'request_id')
ACTIVE_STATES = {'submitting', 'running', 'followup_submitting'}


@contextmanager
def global_lock():
    path = BUSINESS / 'ops/gpt6pro-relay/locks/admission-ip.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _legacy_live_jobs():
    """Read-only process evidence. Unknown wave ownership refuses reconciliation."""
    try:
        from scripts import pro_relay_landing_monitor as monitor
    except ImportError:
        import pro_relay_landing_monitor as monitor
    listing = monitor.read_ps()
    live = set()
    for runner in monitor.runners(listing):
        live.update(monitor.wave_jobs(runner))
    for line in listing.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        executable = Path(parts[1].split(None, 1)[0]).name
        if not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', executable):
            continue
        try:
            argv = shlex.split(parts[1])
        except ValueError:
            # An unrelated Python worker may have a free-form, unbalanced
            # prompt in argv. Never ignore a malformed possible relay runner.
            if 'gpt6pro_relay.py' in parts[1] or 'gpt6pro_wave.py' in parts[1] or 'scripts.gpt6pro_relay' in parts[1]:
                raise ValueError('unparseable possible Python relay process')
            continue
        if not argv:
            continue
        executable = Path(argv[0]).name
        if not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', executable):
            continue
        args = argv[1:]
        while args and args[0] in ('-B', '-u', '-E', '-s', '-S', '-I', '-O', '-OO'):
            args.pop(0)
        if args[:2] == ['-m', 'scripts.gpt6pro_relay']:
            args = args[2:]
        elif args and Path(args[0]).name == 'gpt6pro_relay.py':
            args = args[1:]
        else:
            continue
        if args and args[0] in ('poll', 'followup', 'fetch-outbox', 'submit'):
            job = monitor.option(args, '--job')
            if not job:
                raise ValueError('active relay command without job')
            live.add(job)
    return live


def _legacy_tabs(account):
    """Read Chrome's existing target listing; never attach, navigate or create a tab."""
    url = f"http://127.0.0.1:{int(account['cdp_port'])}/json/list"
    with urllib.request.urlopen(url, timeout=3) as response:
        tabs = json.load(response)
    if not isinstance(tabs, list) or any(not isinstance(t, dict) or
                                           not isinstance(t.get('id'), str) or
                                           not isinstance(t.get('url'), str) for t in tabs):
        raise ValueError('invalid CDP target listing')
    return tabs


def reconcile_legacy(*, apply=False, live_jobs_reader=_legacy_live_jobs, tabs_reader=_legacy_tabs):
    """Quarantine only v1 running rows with no live runner AND no matching tab.

    Global lock serializes against admission. Account locks serialize against relay
    actions; neither a missing CDP endpoint nor an unreadable runner is absence proof.
    """
    cfg = load_config()
    accounts = {a['label']: a for a in cfg['accounts']}
    candidates = sorted(JOBS.glob('*/JOB.json'))
    results = []
    # Use one account lock at a time (the usual lock order is account then IP).
    for path in candidates:
        try:
            original = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise SystemExit(f'RECONCILE_UNREADABLE {path}: {type(exc).__name__}') from exc
        if original.get('status') != 'running' or original.get('binding_version') == 2:
            continue
        label = original.get('account')
        if label not in accounts:
            results.append({'job': path.parent.name, 'decision': 'UNKNOWN_ACCOUNT'})
            continue
        use_account(accounts[label])
        with account_lock(), global_lock():
            preimage = path.read_bytes()
            st = json.loads(preimage)
            if st.get('status') != 'running' or st.get('binding_version') == 2:
                results.append({'job': path.parent.name, 'decision': 'CHANGED_SKIP'})
                continue
            if st.get('job') != path.parent.name or st.get('account') != label or st.get('retry_blocked'):
                results.append({'job': path.parent.name, 'decision': 'IDENTITY_OR_RETRY_UNKNOWN'})
                continue
            try:
                live = live_jobs_reader()
                tabs = tabs_reader(accounts[label])
            except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
                results.append({'job': path.parent.name, 'decision': 'EVIDENCE_UNKNOWN', 'error': type(exc).__name__})
                continue
            want = _conversation(st.get('conversation_url') or st.get('pending_conversation_url'))
            tab_live = any(t.get('type') == 'page' and
                           (t['id'] == st.get('tab') or (want and _conversation(t['url']) == want))
                           for t in tabs)
            if path.parent.name in live or tab_live:
                results.append({'job': path.parent.name, 'decision': 'LIVE_EVIDENCE',
                                'runner': path.parent.name in live, 'tab': tab_live})
                continue
            if not apply:
                results.append({'job': path.parent.name, 'decision': 'WOULD_ORPHAN'})
                continue
            digest = hashlib.sha256(preimage).hexdigest()
            backup = path.with_name(f'JOB.pre-reconcile-{digest}.json')
            try:
                with backup.open('xb') as fh:
                    fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
            except FileExistsError:
                if backup.read_bytes() != preimage:
                    raise SystemExit(f'RECONCILE_PREIMAGE_CONFLICT {backup}')
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
            meta = {'event': 'legacy_reconcile', 'job': path.parent.name, 'account': label,
                    'from_status': 'running', 'to_status': 'legacy_orphaned',
                    'preimage_sha256': digest, 'preimage': str(backup)}
            ledger_append(cfg, {**meta, 'phase': 'STARTED'})
            st.update(status='legacy_orphaned', legacy_reconciliation={
                'at': now_iso(), 'reason': 'v1_unbound_no_live_runner_or_tab',
                'preimage_sha256': digest, 'preimage': str(backup)})
            job_write(path.parent.name, st)
            ledger_append(cfg, {**meta, 'phase': 'APPLIED'})
            if job_state(path.parent.name) != st or backup.read_bytes() != preimage:
                raise SystemExit(f'RECONCILE_READBACK_FAILED {path}')
            results.append({'job': path.parent.name, 'decision': 'ORPHANED', 'preimage_sha256': digest})
    print(json.dumps({'mode': 'apply' if apply else 'dry-run', 'results': results,
                      'active': active_registry()}, sort_keys=True))
    return 0


def _legacy_reply_complete(result: dict, tab: str, conversation: str) -> bool:
    """Use the poller's stop/text rule, but require the existing exact tab."""
    if result.get('error') or result.get('tab') != tab:
        return False
    state = result.get('state') or {}
    return (not state.get('stop') and not state.get('logged_out') and
            not state.get('rate_limited') and not state.get('drive_reconnect') and
            _conversation(state.get('conversation_url_now')) == conversation and
            bool(result.get('text')) and state.get('last_len') == len(result['text']))


def terminalize_legacy(*, apply=False, live_jobs_reader=_legacy_live_jobs, tabs_reader=_legacy_tabs,
                       reply_reader=None, close_reader=None):
    """Inspect a v1 row's already-open tab; never create, reload or submit.

    No legacy Drive outbox is fetched: v1 lacks the input hash required by the
    manifest-bound fetcher. Existing outbox files are never changed. RESPONSE.md
    is the local final-reply artifact and is written only when absent.
    """
    cfg = load_config()
    reply_reader = reply_reader or read_reply
    close_reader = close_reader or close_tab_quiet
    accounts = {a['label']: a for a in cfg['accounts']}
    results = []
    for path in sorted(JOBS.glob('*/JOB.json')):
        initial = json.loads(path.read_bytes())
        if initial.get('status') != 'running' or initial.get('binding_version') == 2:
            continue
        job = path.parent.name
        label = initial.get('account')
        if label not in accounts:
            results.append({'job': job, 'decision': 'UNKNOWN_ACCOUNT'}); continue
        use_account(accounts[label])
        with account_lock(), global_lock():
            preimage = path.read_bytes()
            st = json.loads(preimage)
            if st.get('status') != 'running' or st.get('binding_version') == 2:
                results.append({'job': job, 'decision': 'CHANGED_SKIP'}); continue
            tab, want = st.get('tab'), _conversation(st.get('conversation_url'))
            if (st.get('job') != job or st.get('account') != label or st.get('retry_blocked') or
                    not tab or not want or st.get('pending_conversation_url')):
                results.append({'job': job, 'decision': 'IDENTITY_OR_RETRY_UNKNOWN'}); continue
            try:
                live = live_jobs_reader()
                tabs = tabs_reader(accounts[label])
                matching = [t for t in tabs if t.get('type') == 'page' and
                            _conversation(t.get('url')) == want]
                if job in live or len(matching) != 1:
                    results.append({'job': job, 'decision': 'RUNNER_OR_TAB_AMBIGUOUS',
                                    'runner': job in live, 'matching_tabs': len(matching)}); continue
                # A moved tab is acceptable only if this is the sole target for
                # the exact conversation and no active sibling owns that target.
                tab = matching[0]['id']
                # A URL-only match is not a safe binding. A historical terminal
                # row may retain a reused tab id; only its exact conversation
                # claim or an active sibling's tab claim prevents reconciliation.
                claimed = any(p != path and (lambda other: other.get('account') == label and
                    (_conversation(other.get('conversation_url')) == want or
                     ((other.get('status') in ACTIVE_STATES or other.get('retry_blocked')) and
                      other.get('tab') == tab)))(
                        json.loads(p.read_text())) for p in JOBS.glob('*/JOB.json'))
                if claimed:
                    results.append({'job': job, 'decision': 'RUNNER_OR_TAB_AMBIGUOUS',
                                    'runner': False, 'matching_tabs': len(matching), 'claimed': claimed}); continue
                first = reply_reader(want, tab, reload=False, existing_only=True)
                if not _legacy_reply_complete(first, tab, want):
                    state = first.get('state') or {}
                    decision = ('STILL_GENERATING' if state.get('stop') else
                                'EMPTY_REPLY' if not first.get('text') else 'REPLY_UNVERIFIED')
                    results.append({'job': job, 'decision': decision,
                                    'stop': state.get('stop'), 'last_len': state.get('last_len')}); continue
                # The poller needs the same hash on successive polls. Two reads
                # here establish stable visible text without mutating JOB.json.
                time.sleep(2)
                second = reply_reader(want, tab, reload=False, existing_only=True)
                if (not _legacy_reply_complete(second, tab, want) or first['text'] != second['text']):
                    results.append({'job': job, 'decision': 'STILL_GENERATING_OR_UNSTABLE'}); continue
                text = second['text']
                response = path.with_name('RESPONSE.md')
                expected = (text + '\n').encode()
                if response.exists() and response.read_bytes() != expected:
                    results.append({'job': job, 'decision': 'RESPONSE_CONFLICT'}); continue
                if not apply:
                    results.append({'job': job, 'decision': 'WOULD_COMPLETE', 'chars_out': len(text),
                                    'response_present': response.exists()}); continue
                # Read back the target before writing. A browser-use probe cannot
                # silently switch to a sibling tab on the same conversation URL.
                tabs = tabs_reader(accounts[label])
                if not any(t.get('id') == tab and t.get('type') == 'page' and
                           _conversation(t.get('url')) == want for t in tabs):
                    results.append({'job': job, 'decision': 'TAB_CHANGED'}); continue
                digest = hashlib.sha256(preimage).hexdigest()
                backup = path.with_name(f'JOB.pre-reconcile-{digest}.json')
                try:
                    with backup.open('xb') as fh:
                        fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
                except FileExistsError:
                    if backup.read_bytes() != preimage:
                        raise SystemExit(f'RECONCILE_PREIMAGE_CONFLICT {backup}')
                fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(fd)
                finally: os.close(fd)
                meta = {'event': 'legacy_terminalize', 'job': job, 'account': label,
                        'from_status': 'running', 'to_status': 'legacy_completed',
                        'preimage_sha256': digest, 'preimage': str(backup),
                        'conversation_url': want, 'reply_sha256': hashlib.sha256(text.encode()).hexdigest()}
                ledger_append(cfg, {**meta, 'phase': 'STARTED'})
                if not response.exists():
                    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.response-', delete=False) as fh:
                        fh.write(expected); fh.flush(); os.fsync(fh.fileno())
                        temporary = fh.name
                    # Account lock excludes relay writers; do not replace a
                    # response appearing unexpectedly under a separate owner.
                    if response.exists():
                        Path(temporary).unlink()
                        raise SystemExit(f'RECONCILE_RESPONSE_RACE {response}')
                    os.link(temporary, response)
                    Path(temporary).unlink()
                st.update(status='legacy_completed', tab=tab, finished=now_iso(), chars_out=len(text),
                          source_reply_sha256=meta['reply_sha256'], legacy_reconciliation={
                              'at': now_iso(), 'reason': 'v1_exact_existing_tab_final_reply',
                              'preimage_sha256': digest, 'preimage': str(backup)})
                job_write(job, st)
                ledger_append(cfg, {**meta, 'phase': 'APPLIED'})
                if job_state(job) != st or response.read_bytes() != expected or backup.read_bytes() != preimage:
                    raise SystemExit(f'RECONCILE_READBACK_FAILED {path}')
                closed = close_reader(tab, want)
                remaining = tabs_reader(accounts[label])
                closed = bool(closed and not any(t.get('id') == tab for t in remaining))
                results.append({'job': job, 'decision': 'COMPLETED' if closed else 'COMPLETED_CLOSE_UNVERIFIED',
                                'chars_out': len(text), 'preimage_sha256': digest, 'tab_closed': closed})
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
                results.append({'job': job, 'decision': 'EVIDENCE_UNKNOWN', 'error': type(exc).__name__})
    print(json.dumps({'mode': 'apply' if apply else 'dry-run', 'results': results,
                      'active': active_registry()}, sort_keys=True))
    return 0 if all(r['decision'] not in ('COMPLETED_CLOSE_UNVERIFIED', 'EVIDENCE_UNKNOWN') for r in results) else 1


def abandon_empty_legacy(*, jobs: list[str], apply=False, live_jobs_reader=_legacy_live_jobs,
                         tabs_reader=_legacy_tabs, reply_reader=None, close_reader=None):
    """Abandon explicitly named, old v1 jobs only after an existing-tab empty read.

    Authority: Fable's 2026-09-23 ruling for finished GLP-30 and superseded
    GLP-38 final-pass work. This is not a generic age-based completion path.
    """
    approved = {'glp30-final-npm-test-119-classification', 'glp38-final-dw-row-readback'}
    if not jobs or len(jobs) != len(set(jobs)) or any(j not in approved for j in jobs):
        raise SystemExit('ABANDON_REQUIRES_EXPLICIT_RULING_JOBS')
    cfg = load_config()
    accounts = {a['label']: a for a in cfg['accounts']}
    reply_reader = reply_reader or read_reply
    close_reader = close_reader or close_tab_quiet
    results = []
    for job in jobs:
        path = JOBS / job / 'JOB.json'
        if not path.is_file():
            results.append({'job': job, 'decision': 'NO_SUCH_JOB'}); continue
        initial = json.loads(path.read_bytes())
        label = initial.get('account')
        if label not in accounts:
            results.append({'job': job, 'decision': 'UNKNOWN_ACCOUNT'}); continue
        use_account(accounts[label])
        with account_lock(), global_lock():
            preimage = path.read_bytes()
            st = json.loads(preimage)
            if st.get('status') != 'running' or st.get('binding_version') is not None:
                results.append({'job': job, 'decision': 'NOT_RUNNING_V1'}); continue
            want = _conversation(st.get('conversation_url'))
            if (st.get('job') != job or st.get('account') != label or st.get('retry_blocked') or
                    not want or not st.get('tab') or st.get('pending_conversation_url')):
                results.append({'job': job, 'decision': 'IDENTITY_OR_RETRY_UNKNOWN'}); continue
            try:
                submitted = datetime.fromisoformat(st['submitted'])
                if submitted.tzinfo is None or datetime.now(timezone.utc) - submitted <= timedelta(hours=72):
                    results.append({'job': job, 'decision': 'TOO_NEW_OR_UNKNOWN_AGE'}); continue
            except (KeyError, TypeError, ValueError):
                results.append({'job': job, 'decision': 'TOO_NEW_OR_UNKNOWN_AGE'}); continue
            if path.with_name('RESPONSE.md').exists():
                results.append({'job': job, 'decision': 'RESPONSE_PRESENT'}); continue
            try:
                live = live_jobs_reader()
                tabs = tabs_reader(accounts[label])
                matching = [t for t in tabs if t.get('type') == 'page' and
                            _conversation(t.get('url')) == want]
                if job in live or len(matching) != 1:
                    results.append({'job': job, 'decision': 'RUNNER_OR_TAB_AMBIGUOUS',
                                    'runner': job in live, 'matching_tabs': len(matching)}); continue
                tab = matching[0]['id']
                siblings = sibling_bindings(label, job)
                if any(binding_url(s) == want or
                       ((s.get('status') in ACTIVE_STATES or s.get('retry_blocked')) and s.get('tab') == tab)
                       for s in siblings):
                    results.append({'job': job, 'decision': 'SIBLING_CLAIM'}); continue
                def empty_read():
                    r = reply_reader(want, tab, reload=False, existing_only=True)
                    state = r.get('state') or {}
                    return (not r.get('error') and r.get('tab') == tab and
                            _conversation(state.get('conversation_url_now')) == want and
                            state.get('stop') is False and not state.get('logged_out') and
                            not state.get('rate_limited') and not state.get('drive_reconnect') and
                            r.get('text') == '' and state.get('last_len') == 0)
                if not empty_read():
                    results.append({'job': job, 'decision': 'NOT_VERIFIED_EMPTY_IDLE'}); continue
                if not apply:
                    results.append({'job': job, 'decision': 'WOULD_ABANDON'}); continue
                # The tab and page may change between the first probe and commit.
                tabs = tabs_reader(accounts[label])
                if not any(t.get('id') == tab and t.get('type') == 'page' and
                           _conversation(t.get('url')) == want for t in tabs) or not empty_read():
                    results.append({'job': job, 'decision': 'TAB_OR_PAGE_CHANGED'}); continue
                if path.read_bytes() != preimage or path.with_name('RESPONSE.md').exists():
                    results.append({'job': job, 'decision': 'ROW_OR_RESPONSE_CHANGED'}); continue
                digest = hashlib.sha256(preimage).hexdigest()
                backup = path.with_name(f'JOB.pre-reconcile-{digest}.json')
                try:
                    with backup.open('xb') as fh:
                        fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
                except FileExistsError:
                    if backup.read_bytes() != preimage:
                        raise SystemExit(f'RECONCILE_PREIMAGE_CONFLICT {backup}')
                fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(fd)
                finally: os.close(fd)
                ruling = 'Fable 2026-09-23: GLP-30 closed; GLP-38 final pass superseded'
                meta = {'event': 'legacy_abandon_empty', 'job': job, 'account': label,
                        'from_status': 'running', 'to_status': 'legacy_abandoned',
                        'ruling': ruling, 'preimage_sha256': digest, 'preimage': str(backup),
                        'conversation_url': want}
                ledger_append(cfg, {**meta, 'phase': 'STARTED'})
                st.update(status='legacy_abandoned', tab=tab, finished=now_iso(),
                          legacy_reconciliation={'at': now_iso(), 'reason': 'v1_stale_empty_idle_closed_work',
                                                 'ruling': ruling, 'preimage_sha256': digest,
                                                 'preimage': str(backup)})
                job_write(job, st)
                ledger_append(cfg, {**meta, 'phase': 'APPLIED'})
                if job_state(job) != st or backup.read_bytes() != preimage:
                    raise SystemExit(f'RECONCILE_READBACK_FAILED {path}')
                closed = close_reader(tab, want)
                remaining = tabs_reader(accounts[label])
                closed = bool(closed and not any(t.get('id') == tab for t in remaining))
                results.append({'job': job, 'decision': 'ABANDONED' if closed else 'ABANDONED_CLOSE_UNVERIFIED',
                                'preimage_sha256': digest, 'tab_closed': closed})
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
                results.append({'job': job, 'decision': 'EVIDENCE_UNKNOWN', 'error': type(exc).__name__})
    print(json.dumps({'mode': 'apply' if apply else 'dry-run', 'results': results,
                      'active': active_registry()}, sort_keys=True))
    return 0 if all(r['decision'] in ('WOULD_ABANDON', 'ABANDONED', 'NOT_RUNNING_V1') for r in results) else 1

def active_registry():
    """JOB.json is the account-global reservation, not a wave or live PID count."""
    rows = []
    for path in sorted(JOBS.glob('*/JOB.json')):
        st = json.loads(path.read_text())
        if st.get('status') in ACTIVE_STATES or (st.get('retry_blocked') and
                not (st.get('status') == 'failed' and st.get('reason') == 'THINKING_FAILED')):
            rows.append({'job': path.parent.name, 'account': st.get('account'),
                         'status': st.get('status'), 'conversation_id': st.get('conversation_id'),
                         'retry_blocked': bool(st.get('retry_blocked'))})
    return rows


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with tmp.open('w') as fh:
        json.dump(value, fh, indent=1); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def admit(job, account, cfg, state, followup=False):
    """Account lock then shared IP lock; reserve BEFORE the non-idempotent send."""
    envelope = cfg['envelope']
    with global_lock():
        rows = [r for r in active_registry() if not (followup and r['job'] == job)]
        if len(rows) >= min(6, envelope['max_concurrent_chats_per_ip']):
            raise SystemExit('CAPACITY_IP: active registry full')
        if sum(r['account'] == account['label'] for r in rows) >= min(3, envelope['max_concurrent_chats_per_account']):
            raise SystemExit('CAPACITY_ACCOUNT: active registry full')
        budget_check(cfg, account)
        path = BUSINESS / 'ops/gpt6pro-relay/admission.json'
        clock = json.loads(path.read_text()) if path.exists() else {}
        gap = max(60, envelope['min_seconds_between_submits'])
        if time.time() < float(clock.get('last_submit', 0)) + gap:
            raise SystemExit('PACING_SUBMIT: wait for shared IP submit interval')
        job_write(job, state)
        clock['last_submit'] = time.time()
        _atomic_json(path, clock)
        ledger_append(cfg, {'event': 'submitted', 'job': job, 'account': account['label'],
                           'kind': 'followup' if followup else 'initial', 'certainty': 'reserved_may_send',
                           **{key: state[key] for key in PROVENANCE_FIELDS}})
        _atomic_json(BUSINESS / 'ops/gpt6pro-relay/active-registry.json', {'snapshot_at': now_iso(), 'authority': 'JOB.json', 'jobs': active_registry()})


def poll_slot(job, cfg):
    with global_lock():
        path = BUSINESS / 'ops/gpt6pro-relay/poll-clock.json'
        clock = json.loads(path.read_text()) if path.exists() else {}
        now = time.time(); envelope = cfg['envelope']
        if now < clock.get(job, 0) + max(150, envelope['poll_interval_s']) or now < clock.get('_ip', 0) + max(20, envelope['poll_stagger_s']):
            return False
        clock[job] = clock['_ip'] = now
        _atomic_json(path, clock)
        return True


def provenance(a):
    path = JOBS / a.job / 'MANIFEST.json'
    manifest = json.loads(path.read_text()) if path.exists() else {}
    values = {key: getattr(a, key, None) or manifest.get(key) for key in PROVENANCE_FIELDS}
    if any(not isinstance(v, str) or not v.strip() for v in values.values()):
        raise SystemExit('PROVENANCE_REQUIRED: ' + ','.join(PROVENANCE_FIELDS))
    return values


def sibling_bindings(account: str, job: str) -> list[dict]:
    rows = []
    for path in sorted(JOBS.glob("*/JOB.json")):
        if path.parent.name == job:
            continue
        try:
            row = json.loads(path.read_text())
            if not isinstance(row, dict) or not isinstance(row.get("account"), str):
                raise ValueError("expected an object with an account")
        except (OSError, ValueError) as e:
            raise SystemExit(f"BINDING_REGISTRY_UNREADABLE {path}: {e}")
        if row.get("account") == account:
            rows.append({**row, "job": path.parent.name})
    return rows


def binding_url(st: dict) -> str | None:
    return _conversation(st.get("conversation_url") or st.get("pending_conversation_url"))


def require_binding(st: dict, result: dict | None = None) -> None:
    want = binding_url(st)
    if not want:
        raise SystemExit("BINDING_UNVERIFIED persistent conversation id required; operator recovery needed")
    if st.get('binding_version') != 2 or st.get('conversation_id') != want.rsplit('/', 1)[1]:
        raise SystemExit('BINDING_UNVERIFIED legacy or inconsistent identity requires reconciliation')
    for sibling in sibling_bindings(st["account"], st["job"]):
        if binding_url(sibling) == want:
            raise SystemExit(f"CONVERSATION_COLLISION job={st['job']} sibling={sibling.get('job')}")
        if result is not None and result.get("tab") and result["tab"] == sibling.get("tab"):
            raise SystemExit(f"TAB_COLLISION job={st['job']} sibling={sibling.get('job')}")
    if result is not None:
        state = result.get("state") or {}
        if (result.get("error") or not result.get("tab") or
                _conversation(state.get("url")) != want or
                _conversation(state.get("conversation_url_now")) != want):
            raise SystemExit(f"CONVERSATION_MISMATCH job={st['job']}")


def protected_tabs(st: dict) -> list[str]:
    return [s["tab"] for s in sibling_bindings(st["account"], st["job"]) if s.get("tab")]


# ---------------------------------------------------------------- browser
@account_lock()
def bu(script: str, timeout: int = 300) -> str:
    env = {**os.environ, **BU_ENV}
    r = subprocess.run([str(UVX), "browser-use"], input=script, capture_output=True, text=True, env=env,
                       timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"browser-use exit {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return r.stdout


# Shared page-load token bucket. Embedded in the browser subprocess so reopened
# chats and reloads consume the same durable IP token, not a wave-local sleep.
PAGE_PACING_CODE = r'''
import fcntl, os
from pathlib import Path

def _page_load_slot():
    path = Path(_pace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        last = float(fh.read() or 0)
        delay = last + _page_gap - time.time()
        if delay > 0: time.sleep(delay)
        fh.seek(0); fh.truncate(); fh.write(str(time.time()))
        fh.flush(); os.fsync(fh.fileno())

def _paced_send(callback):
    path = Path(_pace_path).with_name('send-ip.clock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0); last = float(fh.read() or 0)
        delay = last + _submit_gap - time.time()
        if delay > 0: time.sleep(delay)
        # Reserve before click, finish timestamp after click, even on an ambiguous error.
        fh.seek(0); fh.truncate(); fh.write(str(time.time())); fh.flush(); os.fsync(fh.fileno())
        try:
            return callback()
        finally:
            fh.seek(0); fh.truncate(); fh.write(str(time.time())); fh.flush(); os.fsync(fh.fileno())
'''


def bu_json(script: str, timeout: int = 300) -> dict:
    envelope = load_config().get('envelope', {})
    cap = min(3, envelope.get('max_concurrent_chats_per_account', 3)) + 2
    preamble = ('import time\n_pace_path=' + repr(str(BUSINESS / 'ops/gpt6pro-relay/locks/page-load-ip.clock')) +
                '\n_page_gap=' + repr(max(60, 60 / max(0.01, envelope.get('max_page_loads_per_min_per_ip', 1)))) + '\n')
    preamble = ('\n_tab_cap=' + repr(cap) + '\n_submit_gap=' +
                repr(max(60, envelope.get('min_seconds_between_submits', 60))) + '\n' + preamble)
    out = bu(preamble + PAGE_PACING_CODE + script, timeout=timeout)
    lines = [l for l in out.splitlines() if l.startswith("RELAY_JSON:")]
    if not lines:
        raise RuntimeError(f"no RELAY_JSON line in browser-use output:\n{out[-2000:]}")
    return json.loads(lines[-1][len("RELAY_JSON:"):])


BU_PRELUDE = 'import json, re, time\n' + BINDING_URL_CODE + r'''
# These helpers never infer ownership from current_tab() or a URL-matching sibling tab.
_typed = False
_sent = False

def _binding_error(reason, **details):
    print("RELAY_JSON:"+json.dumps({"error":reason,"typed":_typed,"sent":_sent,**details}))
    raise SystemExit(0)

_sessions = {}

def _eval(tab, expression):
    # An explicit session disables the daemon's stale-session fallback to another page.
    result = cdp("Runtime.evaluate", session_id=_sessions[tab], expression=expression,
                 returnByValue=True, awaitPromise=False)
    if result.get("exceptionDetails"):
        _binding_error("page_evaluation_failed", tab=tab)
    return result.get("result", {}).get("value")

def _target(tab):
    # Target.* is ALWAYS browser-level in browser_harness.daemon. A targetId-less
    # getTargetInfo reports the browser, not the attached page (the P71 failure).
    # switch_tab returns the attachToTarget session; prove wrapper js and explicitly
    # addressed Runtime/Input share that page using a fresh per-check nonce.
    if tab not in _sessions or cdp("Target.getTargetInfo", targetId=tab)["targetInfo"]["targetId"] != tab:
        _binding_error("tab_mismatch", tab=tab)
    nonce = __import__('uuid').uuid4().hex
    _eval(tab, "window.__relay_session_nonce=" + json.dumps(nonce))
    if js("window.__relay_session_nonce") != nonce:
        _binding_error("tab_mismatch", tab=tab)

def _select(tab):
    sid = switch_tab(tab)
    if not isinstance(sid, str) or not sid:
        _binding_error("page_session_unverified", tab=tab)
    _sessions[tab] = sid
    # Independently attach the named target. This proves the switch_tab-returned
    # session addresses that target, even if wrapper metadata/default session both
    # drifted together. Target.getTargetInfo alone cannot establish this link.
    witness = cdp('Target.attachToTarget', targetId=tab, flatten=True)['sessionId']
    nonce = __import__('uuid').uuid4().hex
    try:
        cdp('Runtime.evaluate', session_id=witness,
            expression='window.__relay_session_nonce=' + json.dumps(nonce), returnByValue=True)
        if _eval(tab, 'window.__relay_session_nonce') != nonce:
            _binding_error('tab_mismatch', tab=tab)
    finally:
        cdp('Target.detachFromTarget', sessionId=witness)
    _target(tab)

def _new_tab(url):
    before = {t["targetId"] for t in list_tabs()}
    if len(before) >= _tab_cap:
        _binding_error('TAB_CAP', count=len(before), cap=_tab_cap)
    _page_load_slot()
    # Never use new_tab's blank-tab reuse; attach before navigating to avoid its load race.
    tab = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
    if not tab or tab in before:
        _binding_error("tab_collision", tab=tab)
    try:
        _select(tab)
        cdp("Page.navigate", session_id=_sessions[tab], url=url)
        for _ in range(50):
            _target(tab)
            if _eval(tab, "document.readyState") == "complete": break
            time.sleep(0.3)
        _target(tab)
    except BaseException:
        close_tab(tab)
        raise
    return tab

def _here(tab):
    _target(tab)
    return _eval(tab, "location.href")

def _bound_js(tab, want, expression):
    _target(tab)
    # Location and the read/click execute in the SAME JS evaluation, not in two racing snapshots.
    result = _eval(tab, "(()=>{const here=location.href;"
                "if(here.split(/[?#]/)[0].replace(/\\/$/,'')!=="+json.dumps(want.rstrip("/"))+
                ")return {binding_error:true,url:here};return {url:here,value:("+expression+")};})()")
    if result.get("binding_error"):
        reason = "conversation_collision" if _conversation(result.get("url")) in globals().get("forbidden", ()) else "conversation_mismatch"
        _binding_error(reason, expected=want, observed_url=result.get("url"), tab=tab)
    return result["value"]

def _bound_tab(want, tab, protected):
    if not _conversation(want):
        _binding_error("binding_unverified", expected=want)
    if tab and tab not in protected and tab in {t["targetId"] for t in list_tabs()}:
        _select(tab)
        if _conversation(_here(tab)) == want:
            return tab
    tab = _new_tab(want)
    time.sleep(4)
    here = _here(tab)
    if _conversation(here) != want:
        close_tab(tab)
        _binding_error("conversation_mismatch", expected=want, observed_url=here, tab=tab)
    return tab
def _tab_for(substr):
    for t in list_tabs():
        if substr in (t.get("url") or ""):
            return t["targetId"]
    return None
def _goto(url):
    tid = _tab_for("chatgpt.com")
    if tid:
        switch_tab(tid); goto_url(url)
    else:
        _new_tab(url)
    wait_for_load()
def _state(probe=js):
    return probe("""(()=>{
      const mb=[...document.querySelectorAll('button')].map(b=>b.innerText.trim()).find(t=>/^\\s*\\d+\\s*Pro\\s*$/.test(t)||/^\\s*\\d+(\\.\\d+)?\\s*(Sol)?\\s*$/.test(t));
      const login=!!document.querySelector('button[data-testid="login-button"]')||[...document.querySelectorAll('button')].some(b=>/^Log in$/.test(b.innerText.trim()));
      const stop=!!document.querySelector('button[data-testid="stop-button"]');
      const send=document.querySelector('button[data-testid="send-button"]')||document.querySelector('button[type="submit"][aria-label="Send"]');
      const drive_reconnect=/Reconnect Google Drive/.test(document.body.innerText);
      const rate_limited=/too many requests/i.test(document.body.innerText.slice(0,2000))||/too many requests/i.test(document.title);
      const msgs=[...document.querySelectorAll('[data-message-author-role="assistant"]')];
      const last=msgs.length?msgs[msgs.length-1].innerText:'';
      // Match the exact generation label in either UI variant, not quoted prompt text.
      const main=document.querySelector('main');
      const thinking_failed=!!main&&[...main.querySelectorAll('div[class*="group/activity-header"] span, button')].some(e=>e.textContent.trim()==='Thinking failed'&&(e.matches('button')||e.children.length===0));
      return {url:location.href,title:document.title,model:mb||null,logged_out:login,stop:stop,drive_reconnect:drive_reconnect,rate_limited:rate_limited,send:send?!send.disabled:null,assistant_count:msgs.length,last_len:last.length,thinking_failed:thinking_failed};
    })()""")
'''


def browser_state(url: str | None = None) -> dict:
    # Read the state of a chatgpt.com tab, never the daemon's own about:blank tab: on 2026-09-17 `status` reported
    # account 3 as logged_out=False while its only ChatGPT tab was the logged-out landing page (the daemon's current
    # tab was about:blank, where the login-marker probe finds nothing and reads as "logged in").
    focus = ('tid=next((t["targetId"] for t in list_tabs() if (t.get("url") or "").startswith("https://chatgpt.com")),None)\n'
             'if tid: switch_tab(tid)\n')
    script = BU_PRELUDE + (f'_goto({url!r})\ntime.sleep(2)\n' if url else focus) + 'print("RELAY_JSON:"+json.dumps(_state()))\n'
    return bu_json(script)


def send_message(text: str, dry: bool = False, forbidden: list[str] | tuple[str, ...] = ()) -> dict:
    """Open a NEW tab (one per job, so jobs run in parallel), verify the model, insert, send, return tab + URL."""
    payload = json.dumps(text)
    script = BU_PRELUDE + f'''
CHUNK={IPC_CHUNK}
forbidden=set({list(forbidden)!r})
forbidden.update(_conversation(t.get("url")) for t in list_tabs())
forbidden.discard(None)
tab={{"targetId":_new_tab("https://chatgpt.com/")}}
def _fresh():
    here=_here(tab["targetId"])
    if _conversation(here) in forbidden:
        _binding_error("conversation_collision", observed_url=here, tab=tab["targetId"])
    if here!="https://chatgpt.com/" and not _conversation(here):
        _binding_error("binding_unverified", observed_url=here, tab=tab["targetId"])
    if _bound_js(tab["targetId"], here, "!!document.querySelector('[data-message-author-role]')"):
        _binding_error("conversation_not_new", observed_url=here, tab=tab["targetId"])
    return here
fresh=_fresh()
def _input(method, **params):
    global _typed
    if _fresh()!=fresh:
        _binding_error("conversation_mismatch", tab=tab["targetId"])
    if method=="Input.insertText": _typed=True
    return cdp(method, session_id=_sessions[tab["targetId"]], **params)
# Chrome throttles BACKGROUND renderers; a 60k-char composer insert then blows the daemon's 5s CDP timeout
# (wave 1, 2026-09-16: five submits failed with zero messages sent). Foreground the tab while we type.
activate_tab(tab["targetId"])
time.sleep(3)
st=_state(lambda expression: _bound_js(tab["targetId"], fresh, expression))
for _ in range(10):  # the power/model button renders late on a fresh tab; give it up to ~20 s
    if st["model"] or st["logged_out"] or st["rate_limited"]: break
    time.sleep(2); st=_state(lambda expression: _bound_js(tab["targetId"], fresh, expression))
if st["logged_out"]:
    print("RELAY_JSON:"+json.dumps({{"error":"LOGGED_OUT","state":st}})); raise SystemExit(0)
# On a fresh chat the trigger may display the selected effort (e.g. Medium),
# not the model. Select maximum power only on the observed five-step picker;
# never infer GPT-6 from a bare Pro trigger or from a slider click alone.
if not st["model"]:
    trigger="button[aria-label='Select ChatGPT model']"
    label=_bound_js(tab['targetId'], fresh, "(()=>{{const b=document.querySelector("+json.dumps(trigger)+");return b?b.innerText.trim():null;}})()")
    if label in ('Medium', 'Pro'):
        opened=_bound_js(tab['targetId'], fresh, "(()=>{{const b=document.querySelector("+json.dumps(trigger)+");if(!b||b.innerText.trim()!=="+json.dumps(label)+")return false;b.click();return true;}})()")
        if opened and label == 'Medium':
            # The slider is visible only in the simple view. Its fifth tick was
            # observed to show the full '6 Pro' model/effort row on acct2.
            for _ in range(10):
                geometry=_bound_js(tab['targetId'], fresh, "(()=>{{const b=document.querySelector("+json.dumps(trigger)+");const v=document.querySelector('[data-model-picker-view=simple]');const s=v&&v.querySelector('[data-reasoning-slider] [role=slider]');const rail=v&&v.querySelector('[data-model-picker-power-slider]');if(!b||b.getAttribute('aria-expanded')!=='true'||!v||!s||!rail||s.getAttribute('aria-valuemin')!=='0'||s.getAttribute('aria-valuemax')!=='4'||s.getAttribute('aria-valuenow')!=='1')return null;const r=rail.getBoundingClientRect();return r.width>100&&r.height>10?{{x:r.right-13,y:r.top+r.height/2}}:null;}})()")
                if geometry: break
                time.sleep(0.3)
            if geometry:
                _fresh()
                for typ in ('mousePressed','mouseReleased'):
                    cdp('Input.dispatchMouseEvent', session_id=_sessions[tab['targetId']], type=typ,
                        x=geometry['x'], y=geometry['y'], button='left', clickCount=1)
        selected=False
        if opened:
            for _ in range(10):
                time.sleep(0.3)
                selected=_bound_js(tab['targetId'], fresh, "(()=>{{const b=document.querySelector("+json.dumps(trigger)+");const v=document.querySelector('[data-model-picker-view=simple]');const rows=v?[...v.querySelectorAll('[role=menuitem][data-model-picker-view-toggle=true]')]:[];const slider=v&&v.querySelector('[data-reasoning-slider] [role=slider]');return !!b&&b.getAttribute('aria-expanded')==='true'&&rows.length===1&&rows[0].getAttribute('aria-hidden')==='false'&&rows[0].innerText.trim().replaceAll(String.fromCharCode(10),' ')==='6 Pro'&&!!rows[0].querySelector('[data-maximum=true]')&&!!slider&&slider.getAttribute('aria-valuenow')==='4'&&slider.getAttribute('aria-valuemax')==='4';}})()")
                if selected: break
            _bound_js(tab['targetId'], fresh, "(()=>{{const b=document.querySelector("+json.dumps(trigger)+");if(b&&b.getAttribute('aria-expanded')==='true')b.click();return true;}})()")
            if selected: st['model']='6 Pro'
if not st["model"] or not __import__("re").match(r"^\\s*6\\s*Pro\\s*$", st["model"]):
    _binding_error("WRONG_MODEL", state=st)
# Settle: ChatGPT re-mounts the composer a few seconds after load (measured 2026-09-16), which drops focus and
# loses inserted text. Wait until the composer node identity has been stable for 8s before typing.
def _cid():
    return _bound_js(tab["targetId"], fresh, "(()=>{{const e=(document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]'));if(!e)return null;if(!e.dataset.relayId)e.dataset.relayId=String(Math.random());return e.dataset.relayId;}})()")
stable_since=None; last=None; stable=False
# Fresh ChatGPT hydrates the editor independently of the model trigger; the
# markdown editor may appear after the old 40s window. Require a stable node,
# not merely a non-null last sample at the deadline.
for i in range(90):
    cid=_cid()
    if cid and cid==last:
        if stable_since is None: stable_since=time.time()
        if time.time()-stable_since>=8:
            stable=True; break
    else:
        stable_since=None
    last=cid; time.sleep(1)
if not stable:
    print("RELAY_JSON:"+json.dumps({{"error":"NO_COMPOSER","state":_state(lambda expression: _bound_js(tab["targetId"], fresh, expression))}})); raise SystemExit(0)
def _len():
    return _bound_js(tab["targetId"], fresh, "((document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]'))||{{innerText:''}}).innerText.length")
def _focus():
    _fresh()
    # focus() is undefined; a bound JS object omits an undefined `value` key.
    # Return a concrete boolean, without weakening _bound_js's required-value guard.
    if not _bound_js(tab["targetId"], fresh, "(()=>{{const e=(document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]'));if(!e)return false;e.focus();return document.activeElement===e;}})()"):
        _binding_error('FOCUS_FAILED', tab=tab["targetId"])
_focus()
# ProseMirror renders an empty paragraph as a newline in innerText. Keep the
# guard against any real draft while allowing only whitespace-only hydration.
if _bound_js(tab["targetId"], fresh, "!!((document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]'))||{{innerText:''}}).innerText.trim().length"):
    _binding_error('composer_not_empty', tab=tab['targetId'])
TEXT={payload}
pos=0; retries=0
while pos<len(TEXT):
    chunk=TEXT[pos:pos+CHUNK]
    before=_len()
    _focus()
    _input("Input.insertText", text=chunk)
    time.sleep(0.25)
    after=_len()
    if after>=before+len(chunk)-max(5,len(chunk)*0.02):
        pos+=len(chunk); retries=0
    else:
        _binding_error("INSERT_STALLED", pos=pos, before=before, after=after)
time.sleep(2)
got=_len()
if abs(got-len(TEXT))>max(50,len(TEXT)*0.02):
    print("RELAY_JSON:"+json.dumps({{"error":"INSERT_MISMATCH","expected":len(TEXT),"got":got}})); raise SystemExit(0)
if {dry!r}:
    _input("Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA", modifiers=2, windowsVirtualKeyCode=65)
    _input("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", modifiers=2, windowsVirtualKeyCode=65)
    _input("Input.dispatchKeyEvent", type="keyDown", key="Backspace", code="Backspace", windowsVirtualKeyCode=8)
    _input("Input.dispatchKeyEvent", type="keyUp", key="Backspace", code="Backspace", windowsVirtualKeyCode=8)
    time.sleep(1); left=_len(); close_tab(tab["targetId"])
    print("RELAY_JSON:"+json.dumps({{"dry_run":True,"inserted":got,"left_after_clear":left}})); raise SystemExit(0)
_fresh()
_sent='UNKNOWN'
ok=_paced_send(lambda: _bound_js(tab["targetId"], fresh, "(()=>{{const b=document.querySelector('button[data-testid=\\"send-button\\"]')||document.querySelector('button[type=\\"submit\\"][aria-label=\\"Send\\"]');if(!b||b.disabled)return false;b.click();return true;}})()"))
if not ok:
    print("RELAY_JSON:"+json.dumps({{"error":"SEND_DISABLED"}})); raise SystemExit(0)
_sent=True
conv=None
for i in range(60):
    time.sleep(1.5)
    u=_here(tab["targetId"])
    if _conversation(u) in forbidden:
        _binding_error("conversation_collision", observed_url=u, tab=tab["targetId"])
    conv=_conversation(u)
    if conv:
        st=_state(lambda expression: _bound_js(tab["targetId"], conv, expression))
        st["conversation_url_now"]=_bound_js(tab["targetId"], conv, "location.href")
        # Reserve the id immediately so siblings can submit while this job is thinking. Only promote the
        # reservation to conversation_url after the first assistant text; poll performs the same promotion.
        key="conversation_url" if st["last_len"]>0 else "pending_conversation_url"
        print("RELAY_JSON:"+json.dumps({{key:conv,"tab":tab["targetId"],"state":st,"typed":_typed,"sent":_sent}}))
        break
else:
    _binding_error("binding_unverified", observed_url=_here(tab["targetId"]), tab=tab["targetId"])
'''
    return bu_json(script, timeout=900)


def read_reply(conversation_url: str, tab: str | None = None, reload: bool = False,
               protected: list[str] | tuple[str, ...] = (), existing_only: bool = False,
               expected_identity: str | None = None, capture_mode: str | None = None) -> dict:
    """Read the last assistant message; nonterminal callers must use existing_only.

    Reopening/reloading is reserved for independently confirmed terminal fetches.
    Missing Stop/assistant nodes or elapsed time do not prove completion.
    """
    if existing_only and (reload or not tab or not _conversation(conversation_url)):
        raise ValueError('existing reply requires an exact tab and canonical conversation without reload')
    if capture_mode not in (None, 'role', 'selection_markdown'):
        raise ValueError('unknown reply capture mode')
    script = BU_PRELUDE + f'''
RELOAD={reload!r}
EXISTING_ONLY={existing_only!r}
CAPTURE_MODE={capture_mode!r}
want=_conversation({conversation_url!r})
if EXISTING_ONLY:
    matches=[t for t in list_tabs() if t['targetId']=={tab!r} and _conversation(t.get('url'))==want]
    if len(matches)!=1: _binding_error('legacy_tab_mismatch', expected=want, tab={tab!r})
    tab={tab!r}
    _select(tab)
    if _conversation(_here(tab))!=want: _binding_error('legacy_tab_mismatch', expected=want, tab=tab)
else:
    tab=_bound_tab(want, {tab!r}, {list(protected)!r})
probe=lambda expression: _bound_js(tab, want, expression)
st=_state(probe)
if RELOAD and not st["stop"] and not st["thinking_failed"]:
    _page_load_slot()
    # Generation may resume or fail while waiting for the global page token.
    if not (lambda state: state["stop"] or state["thinking_failed"])(_state(probe)):
        probe("location.href="+json.dumps(want))
        wait_for_load(); time.sleep(8)
    st=_state(probe)
st["conversation_url_now"]=probe("location.href")
if {expected_identity is not None!r}:
    identity={expected_identity!r}
    st["user_identity_matched"]=probe("(()=>{{const m=document.querySelector('main');return !!m&&[...m.querySelectorAll('[data-message-author-role=user]')].some(e=>e.innerText.includes("+json.dumps(identity)+"));}})()")
n=probe("""(()=>{{const m=[...document.querySelectorAll('[data-message-author-role=assistant]')];return m.length?m[m.length-1].innerText.length:0;}})()""")
mode=CAPTURE_MODE or 'role'
if mode=='selection_markdown':
    n=0
if not n and CAPTURE_MODE!='role':
    # A forced capture uses the original DOM source; never substitute the other
    # renderer when it cannot reproduce the saved representation.
    n=probe("""(()=>{{const m=[...document.querySelectorAll('[data-chatgpt-selection-message-id] [data-markdown-text-style]')];return m.length?m[m.length-1].innerText.length:0;}})()""") or 0
    if n: mode='selection_markdown'
parts=[]
for i in range(0, n, {IPC_CHUNK}):
    if mode=='selection_markdown':
        parts.append(probe("(()=>{{const m=[...document.querySelectorAll('[data-chatgpt-selection-message-id] [data-markdown-text-style]')];return m[m.length-1].innerText.slice(%d,%d);}})()" % (i, i+{IPC_CHUNK})))
    else:
        parts.append(probe("(()=>{{const m=[...document.querySelectorAll('[data-message-author-role=assistant]')];return m[m.length-1].innerText.slice(%d,%d);}})()" % (i, i+{IPC_CHUNK})))
text="".join(parts)
st['render_mode']=mode
st["conversation_url_now"]=probe("location.href")
print("RELAY_JSON:"+json.dumps({{"state":st,"text":text,"tab":tab}}))
'''
    return bu_json(script)


def cleanup_tabs() -> int:
    """Keep bound active tabs; remove completed chats and old blank probes."""
    cfg = load_config()
    account = next(a['label'] for a in cfg['accounts'] if f"http://127.0.0.1:{a.get('cdp_port', 9340)}" == BU_ENV['BU_CDP_URL'])
    with account_lock():
        rows = [json.loads(p.read_text()) for p in JOBS.glob('*/JOB.json')]
        live = [r for r in rows if r.get('account') == account and
                (r.get('status') in ACTIVE_STATES or
                 (r.get('retry_blocked') and not (r.get('status') == 'failed' and r.get('reason') == 'THINKING_FAILED')))]
        if any(r.get('status') == 'running' and not r.get('tab') for r in live):
            raise SystemExit('CLEANUP_REFUSED unbound generating job')
        protected = [tab for r in live for tab in (r.get('tab'), (r.get('error') or {}).get('tab')) if tab]
        out = bu_json(BU_PRELUDE + f'''
protected=set({protected!r})
tabs=list_tabs()
# Chrome needs one page for subsequent browser-use calls; reuse a blank keeper.
keeper=next((x['targetId'] for x in tabs if x['targetId'] not in protected and x.get('url')=='about:blank'),None) if not protected else None
if not protected and not keeper:
    keeper=cdp('Target.createTarget', url='about:blank', background=True)['targetId']
n=0
for x in tabs:
    if x['targetId'] in protected or x['targetId']==keeper: continue
    close_tab(x['targetId']); n+=1
print('RELAY_JSON:'+json.dumps({{'closed':n,'remaining':len(list_tabs())}}))
''', timeout=120)
        return out['closed']


def drive_reconnect(tab: str) -> dict:
    """Connector authentication belongs to the authorized AI browser/vault path."""
    return {'reconnect': 'STOP_CREDENTIAL_STEP', 'reason': 'use authorized AI browser; no ambient connector actions'}


def close_tab_quiet(tab: str | None, conversation_url: str | None = None) -> bool:
    if not tab or not _conversation(conversation_url):
        return False
    # Never close/re-navigate a reused sibling tab. Keep Chrome alive with a
    # separately-created blank keeper, not by navigating the bound conversation.
    try:
        result = bu_json(BU_PRELUDE + f'''
want=_conversation({conversation_url!r})
tabs=list_tabs()
match=[t for t in tabs if t['targetId']=={tab!r} and _conversation(t.get('url'))==want]
if match:
    _select({tab!r})
    _bound_js({tab!r}, want, "location.href")
    if len(tabs)==1:
        cdp('Target.createTarget', url='about:blank', background=True)
    _bound_js({tab!r}, want, "location.href")
    close_tab({tab!r})
print('RELAY_JSON:'+json.dumps({{'closed':bool(match)}}))
''', timeout=60)
        return result.get('closed') is True
    except Exception:
        return False


# ---------------------------------------------------------------- drive
def drive():
    """Drive client acting AS the workspace user in config.drive.impersonate (DWD).

    A bare service account has no Drive storage quota, so it cannot create files even in a folder shared to it
    (403 storageQuotaExceeded, 2026-09-16). ops@ has quota; the operator's relay folder is shared to ops@ as writer.
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    subject = load_config()["drive"]["impersonate"]
    creds = service_account.Credentials.from_service_account_file(
        str(SA_KEY), scopes=["https://www.googleapis.com/auth/drive"]).with_subject(subject)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def drive_subfolder(svc, parent_id: str, name: str) -> str:
    q = f"'{parent_id}' in parents and name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    r = svc.files().list(q=q, fields="files(id)").execute()
    if r["files"]:
        return r["files"][0]["id"]
    f = svc.files().create(body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}, fields="id").execute()
    return f["id"]


def drive_upload(svc, folder_id: str, path: Path) -> str:
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(path), mimetype="text/plain", resumable=False)
    # Upsert by name: a retried submit updates the record copy instead of leaving duplicates.
    q = f"'{folder_id}' in parents and name = '{path.name}' and trashed = false"
    existing = svc.files().list(q=q, fields="files(id)").execute()["files"]
    if existing:
        f = svc.files().update(fileId=existing[0]["id"], media_body=media, fields="id,webViewLink").execute()
    else:
        f = svc.files().create(body={"name": path.name, "parents": [folder_id]}, media_body=media, fields="id,webViewLink").execute()
    return f.get("webViewLink") or f["id"]


# ---------------------------------------------------------------- jobs
def job_dir(job: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", job):
        raise SystemExit("REFUSED_JOB_NAME use [A-Za-z0-9._-]")
    d = JOBS / job
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_state(job: str) -> dict:
    p = JOBS / job / "JOB.json"
    return json.loads(p.read_text()) if p.exists() else {}


def job_write(job: str, st: dict) -> None:
    _atomic_json(JOBS / job / 'JOB.json', st)


POINTER = """# {title}

Your full instructions for this job are in the file `PROMPT.md` in my Google Drive, folder
"GLP Fable-GPT6Pro Relay" > "inbox" > "{job}". Open it with the Drive connector and read it in full before doing
anything else; it names the other files in that folder you must read (every file in the folder is an input).
Then carry out PROMPT.md exactly, in the order it gives, and answer here in the deliverable format it specifies.
If you cannot open any file, say so at the top of your answer, name the file, and do not guess at its contents.
Files in the folder:
{listing}
"""


DRIVE_SECTION = """

===== FILES ON GOOGLE DRIVE (read them before answering) =====
Open these files from my Google Drive using the Drive connector. They are in the folder
"GLP Fable-GPT6Pro Relay" > "inbox" > "{job}". Read every file in full before you begin; if you cannot open one,
say so explicitly at the top of your answer instead of guessing at its contents.
{listing}
"""


def assemble(prompt_path: Path, attachments: list[Path], drive_files: list[Path] | None = None, job: str = "") -> str:
    text = prompt_path.read_text()
    for a in attachments:
        text += ATTACH_SEP.format(name=a.name) + a.read_text()
    if drive_files:
        listing = "\n".join(f"- {p.name} ({p.stat().st_size:,} bytes)" for p in drive_files)
        text += DRIVE_SECTION.format(job=job, listing=listing)
    if len(text) > MAX_MESSAGE_CHARS:
        raise SystemExit(f"REFUSED_TOO_LONG {len(text)} chars > {MAX_MESSAGE_CHARS}; split the job or trim attachments")
    return text


def send_followup(conversation_url: str, tab: str | None, text: str, protected: list[str] | tuple[str, ...] = ()) -> dict:
    payload = json.dumps(text)
    script = BU_PRELUDE + f'''
want=_conversation({conversation_url!r})
tab=_bound_tab(want, {tab!r}, {list(protected)!r})
probe=lambda expression: _bound_js(tab, want, expression)
activate_tab(tab)
time.sleep(10)
st=_state(probe)
if st["rate_limited"]:
    print("RELAY_JSON:"+json.dumps({{"error":"RATE_LIMITED"}})); raise SystemExit(0)
if st["stop"]:
    print("RELAY_JSON:"+json.dumps({{"error":"STILL_GENERATING"}})); raise SystemExit(0)
if not probe("(()=>{{const e=(document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]'));if(!e)return false;e.focus();return document.activeElement===e;}})()"):
    _binding_error('FOCUS_FAILED', tab=tab)
probe("location.href")
_typed=True
cdp("Input.insertText", session_id=_sessions[tab], text={payload})
time.sleep(1.5)
_sent='UNKNOWN'
ok=_paced_send(lambda: probe("(()=>{{const b=document.querySelector('button[data-testid=\\"send-button\\"]')||document.querySelector('button[type=\\"submit\\"][aria-label=\\"Send\\"]');if(!b||b.disabled)return false;b.click();return true;}})()"))
_sent=bool(ok)
time.sleep(3)
st=_state(probe)
st["conversation_url_now"]=probe("location.href")
print("RELAY_JSON:"+json.dumps({{"sent":ok,"tab":tab,"state":st}}))
'''
    return bu_json(script, timeout=300)


FOLLOWUP_RESUME = """Your previous answer in this chat was cut off by a client-side disconnect on my end (not by me choosing to stop). Please resume the job from PROMPT.md in the Drive folder for this job and deliver the COMPLETE output in the deliverable format it specifies. You do not need to repeat any file-reading you already did if you still have the contents; if not, re-read the files. Do not summarise or shorten because of the interruption."""


def cmd_followup(a) -> int:
    cfg = load_config()
    initial = job_state(a.job)
    if not initial:
        raise SystemExit(f"NO_RUNNING_JOB {a.job}")
    use_account(account_of(cfg, initial["account"]))
    with account_lock():
        return _cmd_followup(a)


def _cmd_followup(a) -> int:
    cfg = load_config()
    st = job_state(a.job)
    if not st or st.get("status") not in ("running", "done"):
        raise SystemExit(f"NO_RUNNING_JOB {a.job}")
    acct = account_of(cfg, st["account"])
    use_account(acct)
    used, limit = budget_check(cfg, acct)
    text = Path(a.text_file).read_text() if a.text_file else FOLLOWUP_RESUME
    require_binding(st)
    if not st.get("conversation_url"):
        raise SystemExit("BINDING_PENDING poll before followup")
    st.update(status='followup_submitting', retry_blocked=True, typed='UNKNOWN', sent='UNKNOWN')
    if any(not st.get(k) for k in PROVENANCE_FIELDS):
        raise SystemExit('PROVENANCE_REQUIRED followup needs dispatcher identity')
    admit(a.job, acct, cfg, st, followup=True)
    res = send_followup(st["conversation_url"], st.get("tab"), text, protected=protected_tabs(st))
    if "error" in res or not res.get("sent"):
        raise SystemExit(f"FOLLOWUP_FAILED {json.dumps(res)[:300]}")
    require_binding(st, res)
    st.update({"status": "running", "retry_blocked": False, "typed": True, "sent": True,
               "tab": res["tab"], "followups": st.get("followups", 0) + 1, "last_text_sha": None,
               "finished_unrendered_polls": 0, "reloaded_once": False, "followup_sent": now_iso()})
    job_write(a.job, st)
    ledger_append(cfg, {"event": "followup_bound", **{k: st[k] for k in PROVENANCE_FIELDS}, "kind": "followup", "account": acct["label"], "job": a.job,
                        "conversation_url": st["conversation_url"], "chars_in": len(text), "used_7d_after": used + 1, "limit": limit})
    print(f"FOLLOWUP_SENT job={a.job} budget={used + 1}/{limit}")
    return 0


def cmd_fetch_outbox(a) -> int:
    cfg = load_config()
    initial = job_state(a.job)
    if not initial:
        raise SystemExit(f"NO_SUCH_JOB {a.job}")
    use_account(account_of(cfg, initial["account"]))
    with account_lock():
        return _cmd_fetch_outbox(a)


def drive_list_all(svc, query):
    rows, token, seen = [], None, set()
    while True:
        page = svc.files().list(q=query, fields='nextPageToken,files(id,name,mimeType)',
                                pageSize=1000, **({'pageToken': token} if token else {})).execute()
        rows.extend(page.get('files', []))
        token = page.get('nextPageToken')
        if not token: return rows
        if token in seen: raise ValueError('repeated Drive page token')
        seen.add(token)


def _cmd_fetch_outbox(a) -> int:
    """Folder names locate data; only a final-chat-bound manifest authorizes it."""
    cfg = load_config(); st = job_state(a.job)
    require_binding(st)
    if not st.get('conversation_url'):
        raise SystemExit('BINDING_PENDING poll before fetch-outbox')
    if st.get('status') == 'done':
        cleanup_tabs()
    r = read_reply(binding_url(st), st.get('tab'), protected=protected_tabs(st),
                   existing_only=st.get('status') != 'done',
                   capture_mode=st.get('reply_render_mode'))
    require_binding(st, r)  # no Drive call or destination writes before exact session verification
    # Poll closes a completed tab. A later reopened, non-generating page can
    # initially render no assistant text; reload only that exact finished chat.
    # Never reload a generating chat or accept a changed reply hash.
    if st.get('status') == 'done' and not r['state'].get('stop') and not r.get('text'):
        r = read_reply(binding_url(st), r['tab'], reload=True, protected=protected_tabs(st),
                       capture_mode=st.get('reply_render_mode'))
        require_binding(st, r)
    try:
        def need(ok, reason):
            if not ok: raise ValueError(reason)
        text = r['text']; sha = hashlib.sha256(text.encode()).hexdigest()
        need(st.get('status') == 'done' and not r['state']['stop'], 'not final')
        local = (JOBS / a.job / 'RESPONSE.md').read_bytes()
        # The original hash is of the captured DOM text, not a normalized
        # Markdown rendering. A missing original renderer is UNKNOWN.
        need(st.get('input_sha256') and st.get('last_text_sha') == sha, 'final reply hash')
        # Old completed jobs appended exactly one LF after hashing the DOM text.
        # New jobs record their raw-byte format; neither branch strips whitespace.
        expected = text.encode() if st.get('reply_serialization') == 'raw_utf8' else (text + '\n').encode()
        need(local == expected, 'local reply differs')
        marker = re.fullmatch(r'(.*\n)RELAY_OUTPUT_MANIFEST_SHA256=([0-9a-f]{64})\s*', text, re.S)
        svc = drive()
        folders = drive_list_all(svc, f"'{cfg['drive']['outbox_id']}' in parents and name = '{a.job}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
        if not folders:
            # Exact-parent, fully paginated zero folders proves absence even if
            # Pro never emitted a manifest marker. It does NOT authorize payload
            # recovery; any nonzero folder still requires the final-chat receipt.
            st['fetch_verification'] = {'state': 'ABSENT', 'reason': 'zero folders under configured outbox',
                                        'source_reply_sha256': sha, 'at': now_iso()}
            st['tab'] = r['tab']
            job_write(a.job, st)
            print(f'OUTBOX_ABSENT_VERIFIED job={a.job} advisory_response={JOBS / a.job / "RESPONSE.md"}')
            return 10
        need(marker, 'missing final-chat manifest receipt')
        need(len(folders) == 1, 'ambiguous/missing folder')
        files = drive_list_all(svc, f"'{folders[0]['id']}' in parents and trashed = false")
        names = [f['name'] for f in files]
        need(len(names) == len(set(names)), 'duplicate filenames')
        need(all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', n) for n in names), 'unsafe filename')
        by_name = {f['name']: f for f in files}
        need('OUTPUT-MANIFEST.json' in by_name, 'missing manifest')
        raw = svc.files().get_media(fileId=by_name['OUTPUT-MANIFEST.json']['id']).execute()
        need(hashlib.sha256(raw).hexdigest() == marker[2], 'manifest receipt hash')
        manifest = json.loads(raw)
        need(manifest.get('job') == a.job, 'job/conversation')
        if manifest.get('schema') == 'gpt6pro-output-manifest-v2':
            # ChatGPT cannot know its URL before the first message is sent, or hash
            # the browser's rendered final reply. A per-job pre-send secret binds
            # the manifest to this exact reservation; the exact-URL browser read,
            # captured reply hash and final-chat manifest digest bind the rest.
            need(st.get('reply_token') and manifest.get('reply_token') == st['reply_token'], 'reply token')
            need('conversation_id' not in manifest or manifest['conversation_id'] == st['conversation_id'], 'job/conversation')
            need('source_reply_sha256' not in manifest or
                 manifest['source_reply_sha256'] == hashlib.sha256(marker[1].encode()).hexdigest(), 'source reply hash')
        else:
            need(manifest.get('conversation_id') == st['conversation_id'], 'job/conversation')
            need(manifest.get('source_reply_sha256') == hashlib.sha256(marker[1].encode()).hexdigest(), 'source reply hash')
        need(manifest.get('input_sha256') == st['input_sha256'], 'input hash')
        declared = manifest.get('files')
        need(isinstance(declared, dict) and bool(declared), 'empty file set')
        need(set(names) == set(declared) | {'OUTPUT-MANIFEST.json'}, 'undeclared/extra file set')
        need('OUTPUT-MANIFEST.json' not in declared, 'recursive manifest')
        blobs = {'OUTPUT-MANIFEST.json': raw}
        for name, digest in declared.items():
            need(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest), 'invalid hash')
            need(not by_name[name]['mimeType'].startswith('application/vnd.google-apps'), 'native document')
            data = svc.files().get_media(fileId=by_name[name]['id']).execute()
            need(hashlib.sha256(data).hexdigest() == digest, 'file hash ' + name)
            blobs[name] = data
        # Recheck final chat after downloads; never promote a stale/sibling result.
        final = read_reply(binding_url(st), r['tab'], protected=protected_tabs(st),
                           capture_mode=st.get('reply_render_mode'))
        require_binding(st, final)
        need(not final['state']['stop'] and final['text'] == text, 'reply changed during fetch')
        dest = JOBS / a.job / 'outbox'
        if dest.exists():
            need(dest.is_dir() and set(p.name for p in dest.iterdir()) == set(blobs), 'legacy/conflicting destination')
            need(all((dest / n).read_bytes() == b for n, b in blobs.items()), 'conflicting destination bytes')
        else:
            with tempfile.TemporaryDirectory(prefix='.outbox-stage-', dir=dest.parent) as staging:
                stage = Path(staging) / 'verified'; stage.mkdir()
                for name, data in blobs.items():
                    with (stage / name).open('wb') as fh:
                        fh.write(data); fh.flush(); os.fsync(fh.fileno())
                os.replace(stage, dest)
        st.update(tab=final['tab'], outbox_files=sorted(blobs), fetch_verification={
            'state': 'VERIFIED', 'conversation_id': st['conversation_id'], 'source_reply_sha256': sha,
            'manifest_sha256': marker[2], 'verified_at': now_iso()})
        job_write(a.job, st)
        print(f'FETCHED_VERIFIED job={a.job} conversation={st["conversation_id"]} files={len(blobs)} -> {dest}')
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        st['fetch_verification'] = {'state': 'UNKNOWN', 'reason': str(exc), 'at': now_iso()}
        st['tab'] = r['tab']
        job_write(a.job, st)
        raise SystemExit(f'OUTBOX_UNVERIFIED job={a.job}: {exc}')
    finally:
        if st.get('status') == 'done':
            close_tab_quiet(r.get('tab'), binding_url(st))


def cmd_status(a) -> int:
    cfg = load_config()
    out = []
    for x in cfg["accounts"]:
        row = {"label": x["label"], "cdp_port": x.get("cdp_port"), "used_7d": messages_used_7d(cfg, x["label"]),
               "limit": int(x["weekly_messages"] * (1 - cfg.get("budget_reserve_pct", 10) / 100))}
        try:
            import socket
            with socket.create_connection(("127.0.0.1", int(x.get("cdp_port", 9340))), timeout=2):
                pass
            use_account(x); b = browser_state()
            row["browser"] = {k: b.get(k) for k in ("url", "model", "logged_out", "rate_limited")}
        except OSError:
            row["browser"] = "chrome not running"
        except Exception as e:
            row["browser"] = f"error: {str(e)[:80]}"
        out.append(row)
    print(json.dumps({"accounts": out}, indent=1))
    return 0


def reconcile_unsent_focus(job: str, failure_log: Path, apply: bool = False) -> int:
    """Release only these three observed pre-insert undefined-focus aborts."""
    incident_logs = {
        'glp7-20260923-carrier-fix-draft': 'acct1.log',
        'glp40-20260923-unresolved-disposition': 'acct2.log',
        'glp44-20260923-ga4-gsc-posthog-measurement-plan': 'acct3.log',
    }
    if job not in incident_logs:
        raise SystemExit('FOCUS_RECONCILE_REFUSED_UNLISTED ' + job)
    st = job_state(job)
    if not st:
        raise SystemExit('NO_SUCH_JOB ' + job)
    cfg = load_config()
    use_account(account_of(cfg, st['account']))
    with account_lock(), global_lock():
        st = job_state(job)
        if (st.get('status') != 'submitting' or st.get('retry_blocked') is not True or
            st.get('typed') != 'UNKNOWN' or st.get('sent') != 'UNKNOWN' or
            any(st.get(k) for k in ('tab', 'conversation_url', 'pending_conversation_url'))):
            raise SystemExit('FOCUS_RECONCILE_REFUSED_STATE ' + job)
        if job in _legacy_live_jobs():
            raise SystemExit('FOCUS_RECONCILE_REFUSED_LIVE_RUNNER ' + job)
        log = failure_log.resolve()
        if log.parent != BUSINESS / 'reports/ops/pro-wave-20260923' or log.name != incident_logs[job]:
            raise SystemExit('FOCUS_RECONCILE_REFUSED_LOG_PATH')
        text = log.read_text()
        if (f"submit {job}: rc=1 KeyError: 'value'" not in text or
            "SUBMIT_REFUSED KeyError: 'value'" not in text):
            raise SystemExit('FOCUS_RECONCILE_REFUSED_SIGNATURE ' + job)
        events = [row['event'] for row in ledger_rows(cfg) if row.get('job') == job]
        if events != ['submitted'] or not (JOBS / job / 'MESSAGE.md').is_file():
            raise SystemExit('FOCUS_RECONCILE_REFUSED_LEDGER ' + job)
        if not apply:
            print('WOULD_RECONCILE_UNSENT_FOCUS job=' + job + ' evidence=' + str(log))
            return 0
        path = JOBS / job / 'JOB.json'
        preimage = path.read_bytes()
        digest = hashlib.sha256(preimage).hexdigest()
        backup = path.with_name('JOB.pre-focus-reconcile-' + digest + '.json')
        if backup.exists() and backup.read_bytes() != preimage:
            raise SystemExit('FOCUS_RECONCILE_REFUSED_BACKUP_CONFLICT ' + job)
        if not backup.exists():
            backup.write_bytes(preimage)
        if path.read_bytes() != preimage:
            raise SystemExit('FOCUS_RECONCILE_REFUSED_STATE_DRIFT ' + job)
        job_write(job, {**st, 'status': 'unsent_reconciled', 'retry_blocked': False,
                        'typed': False, 'sent': False, 'reconciled_at': now_iso(),
                        'reconcile_evidence': str(log), 'preimage_sha256': digest})
        ledger_append(cfg, {'event': 'focus_undefined_unsent_reconciled',
                            'job': job, 'account': st['account'], 'preimage_sha256': digest,
                            'evidence': str(log)})
        cleanup_tabs()
        print('RECONCILED_UNSENT_FOCUS job=' + job + ' preimage_sha256=' + digest)
        return 0


def _reconcile_unsent_composer(job: str, cfg: dict, st: dict, path: Path,
                               preimage: bytes, apply: bool) -> int:
    """Exact incident: NO_COMPOSER exits before _focus/_input; prove all live tabs idle."""
    error = st.get('error', {})
    observed = error.get('state', {}) if isinstance(error, dict) else {}
    if (st.get('job') != job or st.get('status') != 'failed' or
        st.get('reason') != 'NO_COMPOSER' or st.get('retry_blocked') is not True or
        st.get('typed') != 'UNKNOWN' or st.get('sent') != 'UNKNOWN' or
        any(st.get(k) for k in ('tab', 'conversation_url', 'pending_conversation_url')) or
        error.get('error') != 'NO_COMPOSER' or
        observed.get('url') != 'https://chatgpt.com/' or observed.get('stop') is not False or
        observed.get('assistant_count') != 0 or observed.get('last_len') != 0 or
        (JOBS / job / 'RESPONSE.md').exists() or not (JOBS / job / 'MESSAGE.md').is_file()):
        raise SystemExit('COMPOSER_RECONCILE_REFUSED_STATE ' + job)
    if job in _legacy_live_jobs():
        raise SystemExit('COMPOSER_RECONCILE_REFUSED_LIVE_RUNNER ' + job)
    rows = [row for row in ledger_rows(cfg) if row.get('job') == job]
    expected = [('submitted', None)]
    for _ in range(2):
        expected += [('model_unsent_reconcile', 'STARTED'),
                     ('model_unsent_reconcile', 'APPLIED'), ('submitted', None)]
    if [(row.get('event'), row.get('phase')) for row in rows] != expected:
        raise SystemExit('COMPOSER_RECONCILE_REFUSED_LEDGER ' + job)
    for previous in (rows[2], rows[5]):
        digest = previous.get('preimage_sha256')
        backup = path.with_name('JOB.pre-model-reconcile-' + str(digest) + '.json')
        if (not re.fullmatch(r'[0-9a-f]{64}', str(digest)) or not backup.is_file() or
            hashlib.sha256(backup.read_bytes()).hexdigest() != digest):
            raise SystemExit('COMPOSER_RECONCILE_REFUSED_HISTORY ' + job)
    # Existing account Chrome only: no navigation, reload, new tab, composer write or send.
    # The NO_COMPOSER branch in send_message is before _focus and Input.insertText.
    tabs = bu_json(BU_PRELUDE + '''
results=[]
for t in list_tabs():
    if not (t.get('url') or '').startswith('https://chatgpt.com/'): continue
    tab=t['targetId']; _select(tab); here=_here(tab)
    info=_bound_js(tab, here, "(()=>{const e=document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]');const u=[...document.querySelectorAll('[data-message-author-role=user]')];const a=[...document.querySelectorAll('[data-message-author-role=assistant]')];return {composer_empty:!e||!e.innerText.trim(),messages_empty:u.length===0&&a.length===0,job_in_user:u.some(x=>x.innerText.includes('glp270-20260924d-taxonomy-all-expiring-rows')),job_in_composer:!!e&&e.innerText.includes('glp270-20260924d-taxonomy-all-expiring-rows')};})()")
    results.append({'url':here,**info})
print('RELAY_JSON:'+json.dumps({'tabs':results}))
''')
    pages = tabs.get('tabs', [])
    home = [p for p in pages if p.get('url') == 'https://chatgpt.com/']
    if (not home or any(not p.get('composer_empty') or not p.get('messages_empty') for p in home) or
        any(p.get('job_in_user') or p.get('job_in_composer') for p in pages) or
        any(not p.get('url', '').startswith('https://chatgpt.com/') for p in pages)):
        raise SystemExit('COMPOSER_RECONCILE_REFUSED_TAB_EVIDENCE ' + job)
    if not apply:
        print('WOULD_RECONCILE_UNSENT_COMPOSER job=' + job + ' account=acct1 home_tabs=' + str(len(home)))
        return 0
    digest = hashlib.sha256(preimage).hexdigest()
    backup = path.with_name('JOB.pre-composer-reconcile-' + digest + '.json')
    try:
        with backup.open('xb') as fh:
            fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
    except FileExistsError:
        if backup.read_bytes() != preimage:
            raise SystemExit('COMPOSER_RECONCILE_REFUSED_BACKUP_CONFLICT ' + job)
    if path.read_bytes() != preimage:
        raise SystemExit('COMPOSER_RECONCILE_REFUSED_STATE_DRIFT ' + job)
    ledger_append(cfg, {'event': 'composer_unsent_reconcile', 'phase': 'STARTED',
                        'job': job, 'account': st['account'], 'preimage_sha256': digest})
    job_write(job, {**st, 'status': 'unsent_reconciled', 'retry_blocked': False,
                    'typed': False, 'sent': False, 'reconciled_at': now_iso(),
                    'preimage_sha256': digest})
    ledger_append(cfg, {'event': 'composer_unsent_reconcile', 'phase': 'APPLIED',
                        'job': job, 'account': st['account'], 'preimage_sha256': digest})
    cleanup_tabs()
    print('RECONCILED_UNSENT_COMPOSER job=' + job + ' account=acct1 preimage_sha256=' + digest)
    return 0


def _reconcile_unsent_nonempty_composer(job: str, cfg: dict, st: dict, path: Path,
                                        preimage: bytes, apply: bool) -> int:
    """Release only the exact pre-insert 20260924d composer guard refusal."""
    error = st.get('error', {})
    incident_tab = error.get('tab') if isinstance(error, dict) else None
    if (st.get('job') != job or st.get('status') != 'failed' or
        st.get('reason') != 'composer_not_empty' or st.get('retry_blocked') is not True or
        st.get('typed') is not False or st.get('sent') is not False or
        not isinstance(incident_tab, str) or not incident_tab or
        error.get('error') != 'composer_not_empty' or
        error.get('typed') is not False or error.get('sent') is not False or
        any(st.get(k) for k in ('tab', 'conversation_url', 'pending_conversation_url')) or
        (JOBS / job / 'RESPONSE.md').exists() or not (JOBS / job / 'MESSAGE.md').is_file()):
        raise SystemExit('NONEMPTY_COMPOSER_RECONCILE_REFUSED_STATE ' + job)
    if job in _legacy_live_jobs():
        raise SystemExit('NONEMPTY_COMPOSER_RECONCILE_REFUSED_LIVE_RUNNER ' + job)
    rows = [row for row in ledger_rows(cfg) if row.get('job') == job]
    expected = [('submitted', None)]
    for _ in range(2):
        expected += [('model_unsent_reconcile', 'STARTED'),
                     ('model_unsent_reconcile', 'APPLIED'), ('submitted', None)]
    expected += [('composer_unsent_reconcile', 'STARTED'),
                 ('composer_unsent_reconcile', 'APPLIED'), ('submitted', None)]
    if [(row.get('event'), row.get('phase')) for row in rows] != expected:
        raise SystemExit('NONEMPTY_COMPOSER_RECONCILE_REFUSED_LEDGER ' + job)
    for index, prefix in ((2, 'model'), (5, 'model'), (8, 'composer')):
        digest = rows[index].get('preimage_sha256')
        backup = path.with_name('JOB.pre-' + prefix + '-reconcile-' + str(digest) + '.json')
        if (not re.fullmatch(r'[0-9a-f]{64}', str(digest)) or
            rows[index - 1].get('preimage_sha256') != digest or
            not backup.is_file() or hashlib.sha256(backup.read_bytes()).hexdigest() != digest):
            raise SystemExit('NONEMPTY_COMPOSER_RECONCILE_REFUSED_HISTORY ' + job)
    # The composer guard at send_message:924-925 precedes TEXT and Input.insertText.
    # Inspect only existing account tabs; never navigate, reload, type or send.
    tabs = bu_json(BU_PRELUDE + '''
results=[]
for t in list_tabs():
    if not (t.get('url') or '').startswith('https://chatgpt.com/'): continue
    tab=t['targetId']; _select(tab); here=_here(tab)
    info=_bound_js(tab, here, "(()=>{const e=document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]');const u=[...document.querySelectorAll('[data-message-author-role=user]')];const a=[...document.querySelectorAll('[data-message-author-role=assistant]')];return {composer_empty:!e||!e.innerText.trim(),messages_empty:u.length===0&&a.length===0,job_in_user:u.some(x=>x.innerText.includes('glp270-20260924d-taxonomy-all-expiring-rows')),job_in_composer:!!e&&e.innerText.includes('glp270-20260924d-taxonomy-all-expiring-rows')};})()")
    results.append({'tab':tab,'url':here,**info})
print('RELAY_JSON:'+json.dumps({'tabs':results}))
''')
    pages = tabs.get('tabs', [])
    incident = [p for p in pages if p.get('tab') == incident_tab]
    home = [p for p in pages if p.get('url') == 'https://chatgpt.com/']
    if (len(incident) != 1 or incident[0].get('url') != 'https://chatgpt.com/' or
        not incident[0].get('composer_empty') or not incident[0].get('messages_empty') or
        not home or any(not p.get('composer_empty') or not p.get('messages_empty') for p in home) or
        any(p.get('job_in_user') or p.get('job_in_composer') for p in pages) or
        any(not p.get('url', '').startswith('https://chatgpt.com/') for p in pages)):
        raise SystemExit('NONEMPTY_COMPOSER_RECONCILE_REFUSED_TAB_EVIDENCE ' + job)
    if not apply:
        print('WOULD_RECONCILE_UNSENT_NONEMPTY_COMPOSER job=' + job +
              ' account=acct1 home_tabs=' + str(len(home)))
        return 0
    digest = hashlib.sha256(preimage).hexdigest()
    backup = path.with_name('JOB.pre-nonempty-composer-reconcile-' + digest + '.json')
    try:
        with backup.open('xb') as fh:
            fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
    except FileExistsError:
        if backup.read_bytes() != preimage:
            raise SystemExit('NONEMPTY_COMPOSER_RECONCILE_REFUSED_BACKUP_CONFLICT ' + job)
    if path.read_bytes() != preimage:
        raise SystemExit('NONEMPTY_COMPOSER_RECONCILE_REFUSED_STATE_DRIFT ' + job)
    ledger_append(cfg, {'event': 'nonempty_composer_unsent_reconcile', 'phase': 'STARTED',
                        'job': job, 'account': st['account'], 'preimage_sha256': digest})
    job_write(job, {**st, 'status': 'unsent_reconciled', 'retry_blocked': False,
                    'typed': False, 'sent': False, 'reconciled_at': now_iso(),
                    'preimage_sha256': digest})
    ledger_append(cfg, {'event': 'nonempty_composer_unsent_reconcile', 'phase': 'APPLIED',
                        'job': job, 'account': st['account'], 'preimage_sha256': digest})
    cleanup_tabs()
    print('RECONCILED_UNSENT_NONEMPTY_COMPOSER job=' + job +
          ' account=acct1 preimage_sha256=' + digest)
    return 0


def reconcile_unsent_send_disabled(job: str, apply: bool = False) -> int:
    """Release only the 20260924d SEND_DISABLED reservation with its intact draft.

    send_message exits before b.click() on this error. Independently prove no
    job-marker user message across every bound account tab and exactly one
    intact, idle home-tab draft. Clear that draft via the bound relay session
    before marking the reservation unsent. An interrupted clear stays blocked.
    """
    incident = 'glp270-20260924d-taxonomy-all-expiring-rows'
    if job != incident:
        raise SystemExit('SEND_DISABLED_RECONCILE_REFUSED_UNLISTED ' + job)
    cfg = load_config()
    st = job_state(job)
    if not st:
        raise SystemExit('SEND_DISABLED_RECONCILE_REFUSED_MISSING ' + job)
    use_account(account_of(cfg, st['account']))
    with account_lock(), global_lock():
        path = JOBS / job / 'JOB.json'
        preimage = path.read_bytes()
        st = json.loads(preimage)
        error = st.get('error', {})
        message_path = JOBS / job / 'MESSAGE.md'
        if (st.get('job') != job or st.get('status') != 'failed' or
            st.get('reason') != 'SEND_DISABLED' or st.get('retry_blocked') is not True or
            st.get('typed') != 'UNKNOWN' or st.get('sent') != 'UNKNOWN' or
            any(st.get(k) for k in ('tab', 'conversation_url', 'pending_conversation_url')) or
            not isinstance(error, dict) or error.get('error') != 'SEND_DISABLED' or
            (JOBS / job / 'RESPONSE.md').exists() or not message_path.is_file() or
            not st.get('reply_token') or not st.get('input_sha256')):
            raise SystemExit('SEND_DISABLED_RECONCILE_REFUSED_STATE ' + job)
        message = message_path.read_text()
        if not all(value in message for value in (job, st['reply_token'], st['input_sha256'])):
            raise SystemExit('SEND_DISABLED_RECONCILE_REFUSED_MESSAGE ' + job)
        if job in _legacy_live_jobs():
            raise SystemExit('SEND_DISABLED_RECONCILE_REFUSED_LIVE_RUNNER ' + job)
        rows = [row for row in ledger_rows(cfg) if row.get('job') == job]
        expected = [('submitted', None)]
        for _ in range(2):
            expected += [('model_unsent_reconcile', 'STARTED'),
                         ('model_unsent_reconcile', 'APPLIED'), ('submitted', None)]
        expected += [('composer_unsent_reconcile', 'STARTED'),
                     ('composer_unsent_reconcile', 'APPLIED'), ('submitted', None),
                     ('nonempty_composer_unsent_reconcile', 'STARTED'),
                     ('nonempty_composer_unsent_reconcile', 'APPLIED'), ('submitted', None)]
        if [(r.get('event'), r.get('phase')) for r in rows] != expected or any(
            r.get('event') == 'submission_bound' for r in rows):
            raise SystemExit('SEND_DISABLED_RECONCILE_REFUSED_LEDGER ' + job)
        for index, prefix in ((2, 'model'), (5, 'model'), (8, 'composer'),
                              (11, 'nonempty-composer')):
            digest = rows[index].get('preimage_sha256')
            backup = path.with_name('JOB.pre-' + prefix + '-reconcile-' + str(digest) + '.json')
            if (not re.fullmatch(r'[0-9a-f]{64}', str(digest)) or
                rows[index - 1].get('preimage_sha256') != digest or not backup.is_file() or
                hashlib.sha256(backup.read_bytes()).hexdigest() != digest):
                raise SystemExit('SEND_DISABLED_RECONCILE_REFUSED_HISTORY ' + job)
        # No navigation, new tab, reload, click or composer write in this census.
        tabs = bu_json(BU_PRELUDE + '''
results=[]
for t in list_tabs():
    if not (t.get('url') or '').startswith('https://chatgpt.com/'): continue
    tab=t['targetId']; _select(tab); here=_here(tab)
    info=_bound_js(tab, here, "(()=>{const e=document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]');const u=[...document.querySelectorAll('[data-message-author-role=user]')];return {composer:e?e.innerText:null,user_count:u.length,assistant_count:document.querySelectorAll('[data-message-author-role=assistant]').length,job_in_user:u.some(x=>x.innerText.includes("+json.dumps(JOB)+"))};})()")
    results.append({'tab':tab,'url':here,**info})
print('RELAY_JSON:'+json.dumps({'tabs':results,'target_count':len([t for t in list_tabs() if (t.get('url') or '').startswith('https://chatgpt.com/')])}))
'''.replace('JOB', repr(job)))
        pages = tabs.get('tabs')
        normalize = lambda text: ' '.join(text.split())
        if (not isinstance(pages, list) or not pages or
            tabs.get('target_count') != len(pages) or
            len({p.get('tab') for p in pages}) != len(pages) or
            any(p.get('job_in_user') is not False or not isinstance(p.get('url'), str) or
                not p['url'].startswith('https://chatgpt.com/') or
                not isinstance(p.get('user_count'), int) or
                not isinstance(p.get('assistant_count'), int) or
                p.get('composer') is not None and not isinstance(p['composer'], str)
                for p in pages)):
            raise SystemExit('SEND_DISABLED_RECONCILE_REFUSED_TAB_EVIDENCE ' + job)
        matching = [p for p in pages if isinstance(p['composer'], str) and
                    normalize(p['composer']) == normalize(message)]
        if (len(matching) != 1 or matching[0]['url'] != 'https://chatgpt.com/' or
            matching[0]['user_count'] != 0 or matching[0]['assistant_count'] != 0 or
            any(job in (p['composer'] or '') for p in pages if p is not matching[0])):
            raise SystemExit('SEND_DISABLED_RECONCILE_REFUSED_TAB_EVIDENCE ' + job)
        tab = matching[0]['tab']
        if not apply:
            print('WOULD_RECONCILE_UNSENT_SEND_DISABLED job=' + job + ' tab=' + tab +
                  ' inspected_tabs=' + str(len(pages)))
            return 0
        digest = hashlib.sha256(preimage).hexdigest()
        backup = path.with_name('JOB.pre-send-disabled-reconcile-' + digest + '.json')
        try:
            with backup.open('xb') as fh:
                fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
        except FileExistsError:
            if backup.read_bytes() != preimage:
                raise SystemExit('SEND_DISABLED_RECONCILE_REFUSED_BACKUP_CONFLICT ' + job)
        if path.read_bytes() != preimage:
            raise SystemExit('SEND_DISABLED_RECONCILE_REFUSED_STATE_DRIFT ' + job)
        ledger_append(cfg, {'event': 'send_disabled_unsent_reconcile', 'phase': 'STARTED',
                            'job': job, 'account': st['account'], 'preimage_sha256': digest,
                            'draft_tab': tab, 'inspected_tabs': len(pages)})
        # Atomically compare the intact draft in its exact bound page before key events.
        cleared = bu_json(BU_PRELUDE + '''
tab=TAB
matches=[t for t in list_tabs() if t['targetId']==tab and t.get('url')=='https://chatgpt.com/']
if len(matches)!=1: _binding_error('draft_target_mismatch', tab=tab)
_select(tab)
want='https://chatgpt.com/'
check="(()=>{const e=document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]');const u=document.querySelectorAll('[data-message-author-role=user]');const a=document.querySelectorAll('[data-message-author-role=assistant]');return {text:e?e.innerText:null,user_count:u.length,assistant_count:a.length};})()"
value=_bound_js(tab,want,check)
if not isinstance(value['text'],str) or ' '.join(value['text'].split())!=EXPECTED or value['user_count']!=0 or value['assistant_count']!=0:
    _binding_error('draft_changed',tab=tab)
_focus=_bound_js(tab,want,"(()=>{const e=document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]');if(!e)return false;e.focus();return document.activeElement===e;})()")
if not _focus: _binding_error('draft_focus_failed',tab=tab)
value=_bound_js(tab,want,check)
if not isinstance(value['text'],str) or ' '.join(value['text'].split())!=EXPECTED or value['user_count']!=0 or value['assistant_count']!=0:
    _binding_error('draft_changed_after_focus',tab=tab)
cdp('Input.dispatchKeyEvent',session_id=_sessions[tab],type='keyDown',key='a',code='KeyA',modifiers=2,windowsVirtualKeyCode=65)
cdp('Input.dispatchKeyEvent',session_id=_sessions[tab],type='keyUp',key='a',code='KeyA',modifiers=2,windowsVirtualKeyCode=65)
cdp('Input.dispatchKeyEvent',session_id=_sessions[tab],type='keyDown',key='Backspace',code='Backspace',windowsVirtualKeyCode=8)
cdp('Input.dispatchKeyEvent',session_id=_sessions[tab],type='keyUp',key='Backspace',code='Backspace',windowsVirtualKeyCode=8)
time.sleep(0.5)
left=_bound_js(tab,want,check)
print('RELAY_JSON:'+json.dumps({'cleared':isinstance(left['text'],str) and not left['text'].strip() and left['user_count']==0 and left['assistant_count']==0,'tab':tab,'url':_here(tab),'user_count':left['user_count'],'assistant_count':left['assistant_count']}))
'''.replace('TAB', repr(tab)).replace('EXPECTED', repr(normalize(message))))
        if (cleared.get('cleared') is not True or cleared.get('tab') != tab or
            cleared.get('url') != 'https://chatgpt.com/' or cleared.get('user_count') != 0 or
            cleared.get('assistant_count') != 0):
            raise SystemExit('SEND_DISABLED_RECONCILE_REFUSED_CLEAR_UNVERIFIED ' + job)
        if path.read_bytes() != preimage:
            raise SystemExit('SEND_DISABLED_RECONCILE_REFUSED_STATE_DRIFT ' + job)
        job_write(job, {**st, 'status': 'unsent_reconciled', 'retry_blocked': False,
                        'typed': False, 'sent': False, 'reconciled_at': now_iso(),
                        'preimage_sha256': digest, 'cleared_tab': tab})
        ledger_append(cfg, {'event': 'send_disabled_unsent_reconcile', 'phase': 'APPLIED',
                            'job': job, 'account': st['account'], 'preimage_sha256': digest,
                            'draft_tab': tab})
        cleanup_tabs()
        print('RECONCILED_UNSENT_SEND_DISABLED job=' + job + ' tab=' + tab)
        return 0


def reconcile_unsent_model(job: str, apply: bool = False, report: str | None = None) -> int:
    """Release a pre-insert model refusal, never a possibly-sent job.

    Jobs are either allowlisted below or named with an explicit ``report``
    (a path relative to the business root) whose row must prove the job unsent;
    the account alias then comes from the job's own JOB.json.
    """
    incidents = {
        'glp270-20260924d-taxonomy-all-expiring-rows':
            ('reports/ops/pro-wave-20260924d/RESULT.json', 'acct1'),
        'glp41-20260925-postreset-r3-browser-node-blockers':
            ('reports/ops/pro-postreset-wave-20260925/RESULT.json', 'acct2'),
    }
    listed = job in incidents
    if listed:
        report_path, account_alias = incidents[job]
        if report is not None and report != report_path:
            raise SystemExit('MODEL_RECONCILE_REFUSED_REPORT_MISMATCH ' + job)
    elif report is None:
        raise SystemExit('MODEL_RECONCILE_REFUSED_UNLISTED ' + job)
    else:
        report_path, account_alias = report, None
        resolved = (BUSINESS / report_path).resolve()
        if (Path(report_path).is_absolute() or
                not resolved.is_relative_to(BUSINESS.resolve()) or not resolved.is_file()):
            raise SystemExit('MODEL_RECONCILE_REFUSED_REPORT_PATH ' + job)
    cfg = load_config()
    st = job_state(job)
    if not st:
        raise SystemExit('MODEL_RECONCILE_REFUSED_MISSING ' + job)
    use_account(account_of(cfg, st['account']))
    with account_lock(), global_lock():
        path = JOBS / job / 'JOB.json'
        preimage = path.read_bytes()
        st = json.loads(preimage)
        if listed and st.get('reason') == 'NO_COMPOSER':
            return _reconcile_unsent_composer(job, cfg, st, path, preimage, apply)
        if listed and st.get('reason') == 'composer_not_empty':
            return _reconcile_unsent_nonempty_composer(job, cfg, st, path, preimage, apply)
        if account_alias is None:
            account_alias = str(st.get('account'))
        error = st.get('error', {})
        observed = error.get('state', {}) if isinstance(error, dict) else {}
        if (st.get('job') != job or st.get('status') != 'failed' or
            st.get('reason') != 'WRONG_MODEL' or st.get('retry_blocked') is not True or
            st.get('typed') is not False or st.get('sent') is not False or
            any(st.get(k) for k in ('tab', 'conversation_url', 'pending_conversation_url')) or
            error.get('error') != 'WRONG_MODEL' or error.get('typed') is not False or
            error.get('sent') is not False or observed.get('url') != 'https://chatgpt.com/' or
            observed.get('model') is not None or observed.get('assistant_count') != 0 or
            observed.get('last_len') != 0):
            raise SystemExit('MODEL_RECONCILE_REFUSED_STATE ' + job)
        report = json.loads((BUSINESS / report_path).read_text())
        matching = [row for row in report.get('jobs', [])
                    if row.get('job', row.get('job_id')) == job and
                    (row.get('job_id', job) == job)]
        if (len(matching) != 1 or matching[0].get('submission_bound') is not False or
            matching[0].get('typed') is not False or matching[0].get('sent') is not False):
            raise SystemExit('MODEL_RECONCILE_REFUSED_REPORT ' + job)
        if job in _legacy_live_jobs():
            raise SystemExit('MODEL_RECONCILE_REFUSED_LIVE_RUNNER ' + job)
        tabs = _legacy_tabs(account_of(cfg, st['account']))
        home_tab = any(t.get('type') == 'page' and t.get('url') == 'https://chatgpt.com/' for t in tabs)
        # This incident's pre-insert tab was already closed. Require *no* ChatGPT
        # page target in its account, rather than manufacturing a home-tab witness.
        no_chatgpt_page = not any(t.get('type') == 'page' and
                                   t.get('url', '').startswith('https://chatgpt.com') for t in tabs)
        if not (home_tab or (job == 'glp41-20260925-postreset-r3-browser-node-blockers' and no_chatgpt_page)):
            raise SystemExit('MODEL_RECONCILE_REFUSED_TAB_EVIDENCE ' + job)
        rows = [row for row in ledger_rows(cfg) if row.get('job') == job]
        events = [(row['event'], row.get('phase')) for row in rows]
        first = events == [('submitted', None)]
        second = events == [('submitted', None), ('model_unsent_reconcile', 'STARTED'),
                            ('model_unsent_reconcile', 'APPLIED'), ('submitted', None)]
        if second:
            old_hash = rows[2].get('preimage_sha256')
            old_backup = path.with_name('JOB.pre-model-reconcile-' + str(old_hash) + '.json')
            if (not re.fullmatch(r'[0-9a-f]{64}', str(old_hash)) or
                rows[1].get('preimage_sha256') != old_hash or not old_backup.is_file() or
                hashlib.sha256(old_backup.read_bytes()).hexdigest() != old_hash or
                st.get('reply_token') == json.loads(old_backup.read_bytes()).get('reply_token')):
                raise SystemExit('MODEL_RECONCILE_REFUSED_PREVIOUS ' + job)
        if not (first or second) or not (JOBS / job / 'MESSAGE.md').is_file():
            raise SystemExit('MODEL_RECONCILE_REFUSED_LEDGER ' + job)
        if not apply:
            print('WOULD_RECONCILE_UNSENT_MODEL job=' + job + ' account=' + account_alias)
            return 0
        digest = hashlib.sha256(preimage).hexdigest()
        backup = path.with_name('JOB.pre-model-reconcile-' + digest + '.json')
        try:
            with backup.open('xb') as fh:
                fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
        except FileExistsError:
            if backup.read_bytes() != preimage:
                raise SystemExit('MODEL_RECONCILE_REFUSED_BACKUP_CONFLICT ' + job)
        if path.read_bytes() != preimage:
            raise SystemExit('MODEL_RECONCILE_REFUSED_STATE_DRIFT ' + job)
        ledger_append(cfg, {'event': 'model_unsent_reconcile', 'phase': 'STARTED',
                            'job': job, 'account': st['account'], 'preimage_sha256': digest})
        job_write(job, {**st, 'status': 'unsent_reconciled', 'retry_blocked': False,
                        'reconciled_at': now_iso(), 'preimage_sha256': digest})
        ledger_append(cfg, {'event': 'model_unsent_reconcile', 'phase': 'APPLIED',
                            'job': job, 'account': st['account'], 'preimage_sha256': digest})
        cleanup_tabs()
        print('RECONCILED_UNSENT_MODEL job=' + job + ' account=' + account_alias + ' preimage_sha256=' + digest)
        return 0


def cmd_submit(a) -> int:
    if not a.account:
        raise SystemExit('ACCOUNT_REQUIRED')
    cfg = load_config()
    use_account(account_of(cfg, a.account))
    with account_lock():
        rc = _cmd_submit(a)
    if rc == 0 and a.wait and not a.dry_run:
        return poll(a.job, wait=True, interval=a.interval, max_wait=a.max_wait)
    return rc


def _cmd_submit(a) -> int:
    cfg = load_config()
    acct = account_of(cfg, a.account)
    use_account(acct)
    if a.job_class not in cfg["job_classes_allowed"]:
        raise SystemExit(f"REFUSED_JOB_CLASS {a.job_class} not in {cfg['job_classes_allowed']}")
    d = job_dir(a.job)
    previous = job_state(a.job)
    if previous.get("status") in ("submitting", "running", "done") or previous.get("retry_blocked"):
        raise SystemExit(f"REFUSED_DUPLICATE job {a.job} already submitted or uncertain (use poll/operator recovery)")
    siblings = sibling_bindings(acct["label"], a.job)
    if any((s.get("status") in ("submitting", "running") or s.get("retry_blocked")) and not binding_url(s) for s in siblings):
        raise SystemExit("BINDING_UNVERIFIED sibling has no persistent id; operator recovery needed")
    forbidden = [binding_url(s) for s in siblings if binding_url(s)]
    used, limit = (0, 0) if a.dry_run else budget_check(cfg, acct)
    prompt = Path(a.prompt).resolve()
    attachments = [Path(x).resolve() for x in (a.attach or [])]
    drive_files = [Path(x).resolve() for x in (a.drive_attach or [])]
    if a.prompt_on_drive:
        # The composer chokes on long code-heavy text (2026-09-16 measurements): the whole packet lives on Drive
        # and the message is a short pointer. Small inline attachments are folded into the Drive prompt copy; when
        # the folded prompt would exceed the verified single-file read (~150k chars), every attachment goes up as
        # its own Drive file instead and the prompt names them (no MAX_MESSAGE_CHARS cap applies on Drive).
        base = prompt.read_text()
        folded = sum(len(x.read_text()) for x in attachments) + len(base)
        prompt = d / "drive-input" / "PROMPT.md"
        prompt.parent.mkdir(exist_ok=True)
        if folded <= 150_000:
            full = base
            for x in attachments:
                full += ATTACH_SEP.format(name=x.name) + x.read_text()
            if prompt.resolve() != Path(a.prompt).resolve() or attachments:
                prompt.write_text(full)
            extra = []
        else:
            names = "\n".join(f"- {x.name}" for x in attachments)
            full = base + ("\n\n===== ATTACHMENTS ARE SEPARATE FILES IN THIS DRIVE FOLDER (read each in full) =====\n" + names + "\n")
            if prompt.resolve() != Path(a.prompt).resolve() or attachments:
                prompt.write_text(full)
            extra = list(attachments)
        drive_files = [prompt] + extra + [p for p in drive_files if p.resolve() != prompt.resolve()]
        title = full.splitlines()[0].lstrip("# ").strip() if full.strip() else a.job
        listing = "\n".join(f"- {p.name} ({p.stat().st_size:,} bytes)" for p in drive_files)
        text = POINTER.format(title=title, job=a.job, listing=listing)
        attachments = []
    else:
        text = assemble(prompt, attachments, drive_files, a.job)
    meta = provenance(a)
    input_sha = hashlib.sha256(json.dumps([(p.name, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in [Path(a.prompt).resolve(), *[Path(x).resolve() for x in (a.attach or [])], *[Path(x).resolve() for x in (a.drive_attach or [])]]],
        separators=(',', ':')).encode()).hexdigest()
    reply_token = secrets.token_hex(32)
    text += (f'\n\nRELAY_IDENTITY job={a.job} input_sha256={input_sha} reply_token={reply_token}\n'
             'For Drive files supply OUTPUT-MANIFEST.json with schema="gpt6pro-output-manifest-v2", '
             'job, input_sha256, reply_token (copy the exact values above), and files (object mapping '
             'payload basename to SHA256 of the exact saved file bytes; exclude OUTPUT-MANIFEST.json itself). '
             'Do NOT include conversation_id: the relay verifies the actual chat URL, which you cannot know '
             'from the prompt. Do NOT include source_reply_sha256: the browser-rendered final reply may differ '
             'from the text you composed; the relay verifies its own exact captured reply hash. '
             'Append RELAY_OUTPUT_MANIFEST_SHA256=<SHA256 of exact uploaded OUTPUT-MANIFEST.json bytes> '
             'as the LAST line of your reply. Never invent hashes or save a manifest whose bytes you did not hash.\n')
    (d / "MESSAGE.md").write_text(text)
    reservation = {'job': a.job, 'job_class': a.job_class, 'account': acct['label'],
                   'status': 'submitting', 'retry_blocked': True, 'input_sha256': input_sha,
                   'reply_token': reply_token,
                   'typed': 'UNKNOWN', 'sent': 'UNKNOWN', **meta}
    if not a.dry_run:
        admit(a.job, acct, cfg, reservation)
        cleanup_tabs()
    # Reservation and quota precede upload and send; failures are quarantined.
    links = {}
    if not a.dry_run:
        svc = drive()
        inbox = drive_subfolder(svc, cfg["drive"]["inbox_id"], a.job)
        links = {p.name: drive_upload(svc, inbox, p) for p in [d / "MESSAGE.md", *drive_files]}
    if a.dry_run:
        res = send_message(text, dry=True, forbidden=forbidden)
        print(f"DRY_RUN job={a.job} {json.dumps(res)}"); return 0 if res.get("dry_run") else 1
    res = send_message(text, forbidden=forbidden)
    if "error" not in res and (not binding_url(res) or binding_url(res) in forbidden):
        res = {"error": "conversation_collision" if binding_url(res) in forbidden else "binding_unverified",
               "typed": True, "sent": True, "result": res}
    if "error" in res:
        job_write(a.job, {**reservation, "status": "failed", "reason": res["error"], "error": res,
                          "retry_blocked": True, "typed": res.get('typed', 'UNKNOWN'),
                          "sent": res.get('sent', 'UNKNOWN'), "ts": now_iso()})
        raise SystemExit(f"SEND_FAILED {res['error']} {json.dumps(res.get('state') or res)[:400]}")
    st = {**reservation, "status": "running", "retry_blocked": False, "typed": True, "sent": True,
          "conversation_id": binding_url(res).rsplit('/', 1)[1], "binding_version": 2,
          "tab": res.get("tab"), "submitted": now_iso(), "chars_in": len(text),
          "drive_files": [str(x) for x in drive_files],
          "drive_inbox": links, "prompt": str(prompt), "attachments": [str(x) for x in attachments]}
    key = "conversation_url" if res.get("conversation_url") else "pending_conversation_url"
    st[key] = binding_url(res)
    require_binding(st, res)
    job_write(a.job, st)
    ledger_append(cfg, {"event": "submission_bound", **meta, "account": acct["label"], "job": a.job, "job_class": a.job_class,
                        "conversation_url": binding_url(res), "chars_in": len(text), "used_7d_after": used + 1, "limit": limit})
    print(f"SUBMITTED job={a.job} account={acct['label']} conv={binding_url(res)} binding={key} chars_in={len(text)} budget={used + 1}/{limit}")
    return 0


def poll(job: str, wait: bool, interval: int, max_wait: int) -> int:
    cfg = load_config()
    initial = job_state(job)
    if not initial:
        raise SystemExit(f"NO_SUCH_JOB {job}")
    use_account(account_of(cfg, initial.get("account")))
    t0 = time.time()
    while True:
        # Release between polls/backoffs: three chats can keep thinking while another is submitted.
        with account_lock():
            rc, pause = _poll_once(job, cfg, interval)
        if rc in (0, 5) or not wait:
            return rc
        if int(time.time() - t0) > max_wait:
            raise SystemExit(f"TIMEOUT after {max_wait}s (job still running; poll again later)")
        time.sleep(pause)


def _failed_generation_identity(st: dict) -> str:
    return (f"RELAY_IDENTITY job={st.get('job')} input_sha256={st.get('input_sha256')} "
            f"reply_token={st.get('reply_token')}")


def _record_failed_generation(job: str, cfg: dict, st: dict, r: dict) -> bool:
    """Terminalize only the sent, exact-tab, exact-user-message failed-generation UI.

    Called under the account lock; unknown/empty/other-conversation reads do nothing.
    A retry stays blocked, but this terminal failure no longer consumes a live slot.
    """
    s = r.get('state') or {}
    marker = _failed_generation_identity(st)
    message = JOBS / job / 'MESSAGE.md'
    path = JOBS / job / 'JOB.json'
    bound = [row for row in ledger_rows(cfg) if row.get('event') == 'submission_bound' and
             row.get('job') == job and row.get('account') == st.get('account') and
             _conversation(row.get('conversation_url')) == binding_url(st) and
             all(row.get(k) == st.get(k) for k in PROVENANCE_FIELDS)]
    if (len(bound) != 1 or st.get('status') != 'running' or st.get('job') != job or st.get('sent') is not True or
            st.get('retry_blocked') or st.get('binding_version') != 2 or
            not all(st.get(k) for k in PROVENANCE_FIELDS) or
            not re.fullmatch(r'[0-9a-f]{64}', st.get('input_sha256') or '') or
            not re.fullmatch(r'[0-9a-f]{64}', st.get('reply_token') or '') or
            not message.is_file() or marker not in message.read_text() or
            (JOBS / job / 'RESPONSE.md').exists() or (JOBS / job / 'outbox').exists() or
            r.get('tab') != st.get('tab') or s.get('stop') is not False or
            s.get('logged_out') or s.get('rate_limited') or s.get('drive_reconnect') or
            s.get('thinking_failed') is not True or s.get('user_identity_matched') is not True or
            s.get('assistant_count') != 0 or s.get('last_len') != 0 or r.get('text') != ''):
        return False
    require_binding(st, r)
    with global_lock():
        preimage = path.read_bytes()
        if json.loads(preimage) != st:
            return False
        digest = hashlib.sha256(preimage).hexdigest()
        backup = path.with_name(f'JOB.pre-thinking-failed-{digest}.json')
        try:
            with backup.open('xb') as fh:
                fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
        except FileExistsError:
            if backup.read_bytes() != preimage:
                raise SystemExit('THINKING_FAILED_PREIMAGE_CONFLICT ' + job)
        meta = {'event': 'generation_failed', 'job': job, 'account': st['account'],
                'conversation_url': binding_url(st), 'reason': 'THINKING_FAILED',
                'preimage_sha256': digest, 'preimage': str(backup),
                **{k: st[k] for k in PROVENANCE_FIELDS}}
        ledger_append(cfg, {**meta, 'phase': 'STARTED'})
        updated = {**st, 'status': 'failed', 'reason': 'THINKING_FAILED',
                   'retry_blocked': True, 'finished': now_iso(),
                   'failed_generation': {'ui': 'Thinking failed', 'preimage_sha256': digest,
                                         'preimage': str(backup), 'observed_at': now_iso()}}
        job_write(job, updated)
        ledger_append(cfg, {**meta, 'phase': 'APPLIED'})
        if job_state(job) != updated or backup.read_bytes() != preimage:
            raise SystemExit('THINKING_FAILED_READBACK_FAILED ' + job)
    close_tab_quiet(st.get('tab'), binding_url(st))
    return True


def reconcile_no_reply(job: str) -> int:
    """Inspect an exact bound tab without reloading or releasing an uncertain send."""
    cfg = load_config()
    st = job_state(job)
    if not st or st.get("status") != "running":
        raise SystemExit(f"JOB_NOT_RUNNING job={job}")
    require_binding(st)
    use_account(account_of(cfg, st["account"]))
    with account_lock():
        st = job_state(job)
        if st.get('status') != 'running':
            print(f"RECONCILED_STATE_CHANGED job={job} status={st.get('status')}; no resend")
            return 3
        require_binding(st)
        r = read_reply(binding_url(st), st.get("tab"), existing_only=True,
                       expected_identity=_failed_generation_identity(st))
        require_binding(st, r)
        if _record_failed_generation(job, cfg, st, r):
            print(f"RECONCILED_GENERATION_FAILED job={job} reason=THINKING_FAILED; no resend")
            return 5
    if r["state"]["stop"]:
        print(f"RECONCILED_GENERATING job={job}; reservation retained")
    elif r["text"]:
        print(f"RECONCILED_REPLY_READY job={job}; poll the bound response")
    else:
        print(f"RECONCILED_NO_REPLY_UNRESOLVED job={job}; reservation retained; no resend")
    return 3


def _poll_once(job: str, cfg: dict, interval: int) -> tuple[int, int]:
    st = job_state(job)
    if not st:
        raise SystemExit(f"NO_SUCH_JOB {job}")
    require_binding(st)
    if st.get("status") == "done":
        print(f"DONE job={job} response={JOBS / job / 'RESPONSE.md'}")
        return 0, 0
    if st.get("status") != "running":
        raise SystemExit(f"JOB_NOT_RUNNING status={st.get('status')}")
    interval = max(150, interval, cfg['envelope']['poll_interval_s'])
    if not poll_slot(job, cfg):
        print(f'PACING_POLL job={job}', flush=True)
        return 3, interval
    conv = binding_url(st)
    # Stability still survives separate poll invocations; never carry stale JOB.json across the lock.
    prev = st.get("last_text_sha")
    # Missing Stop/assistant nodes can be virtualized generation, not completion.
    # No poll count or elapsed time permits navigation of a nonterminal binding.
    r = read_reply(conv, st.get("tab"), existing_only=True, protected=protected_tabs(st),
                   expected_identity=_failed_generation_identity(st),
                   capture_mode=st.get('reply_render_mode'))
    require_binding(st, r)
    if _record_failed_generation(job, cfg, st, r):
        print(f"poll job={job} GENERATION_FAILED reason=THINKING_FAILED; no resend", flush=True)
        return 5, 0
    s = r["state"]
    st["tab"] = r["tab"]
    if s.get("rate_limited"):
        n = st.get("rate_limit_hits", 0) + 1; st["rate_limit_hits"] = n; job_write(job, st)
        print(f"poll job={job} RATE_LIMITED (429 page, hit {n}) — do not reload; back off", flush=True)
        return 4, 900 if n == 1 else 3600
    if s.get("drive_reconnect"):
        raise SystemExit('DRIVE_RECONNECT_NEEDS_OPERATOR: no unbound automatic connector clicks')
    if s.get("logged_out"):
        raise SystemExit("LOGGED_OUT operator must repeat the VNC login handoff")
    text = r["text"]
    streaming = s["stop"]
    if st.get("pending_conversation_url") and text:
        st["conversation_url"] = conv
        del st["pending_conversation_url"]
    sha = hashlib.sha256(text.encode()).hexdigest() if text else None
    if streaming or not text or prev != sha:
        st["last_text_sha"] = sha if not streaming else None
        st['reply_render_mode'] = s.get('render_mode') if not streaming and text else None
        st["finished_unrendered_polls"] = (st.get("finished_unrendered_polls", 0) + 1) if (not streaming and not text) else 0
        job_write(job, st)
        print(f"poll job={job} streaming={streaming} chars_out={len(text)}", flush=True)
        return 3, interval
    if st.get('reply_render_mode') and s.get('render_mode') != st['reply_render_mode']:
        raise SystemExit('REPLY_RENDER_MODE_CHANGED original capture unavailable')
    # The text was captured with a same-evaluation location guard; check the job registry again before writing.
    require_binding(st, r)
    d = JOBS / job
    with tempfile.NamedTemporaryFile(mode='w', dir=d, prefix='.response-', delete=False) as fh:
        fh.write(text); fh.flush(); os.fsync(fh.fileno())
        temporary = fh.name
    os.replace(temporary, d / 'RESPONSE.md')
    link = 'local-bound-response'
    st.update({"status": "done", "finished": now_iso(), "chars_out": len(text),
               "source_reply_sha256": sha, "reply_serialization": "raw_utf8",
               "reply_render_mode": s.get('render_mode')})
    job_write(job, st)
    close_tab_quiet(st.get("tab"), conv)
    try:
        elapsed_total = (datetime.fromisoformat(st["finished"]) - datetime.fromisoformat(st["submitted"])).total_seconds()
    except (KeyError, ValueError):
        elapsed_total = None
    ledger_append(cfg, {"event": "completed", "account": st["account"], "job": job, "chars_out": len(text),
                        "elapsed_s": elapsed_total, "conversation_url": conv})
    print(f"DONE job={job} chars_out={len(text)} elapsed_s={elapsed_total} response={d / 'RESPONSE.md'} drive={link}")
    return 0, 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    nr = sub.add_parser("reconcile-no-reply", help="read exact bound tab without reload or freeing uncertain capacity")
    nr.add_argument("--job", required=True)
    u = sub.add_parser("reconcile-unsent-focus", help="dry-run first; exact pre-insert focus() KeyError only")
    u.add_argument("--job", required=True)
    u.add_argument("--failure-log", type=Path, required=True)
    u.add_argument("--apply", action="store_true")
    m = sub.add_parser("reconcile-unsent-model", help="dry-run first; exact pre-insert WRONG_MODEL refusal only")
    m.add_argument("--job", required=True)
    m.add_argument("--report", help="unlisted job: report JSON (relative to business root) whose row proves it unsent")
    m.add_argument("--apply", action="store_true")
    sd = sub.add_parser("reconcile-unsent-send-disabled", help="dry-run first; exact intact unsent SEND_DISABLED draft")
    sd.add_argument("--job", required=True)
    sd.add_argument("--apply", action="store_true")
    r = sub.add_parser("reconcile-legacy", help="dry-run by default: quarantine or inspect and complete v1 rows")
    r.add_argument("--apply", action="store_true", help="preserve preimage, journal and apply verified changes")
    r.add_argument("--terminalize", action="store_true", help="inspect only existing exact tabs; complete stable final replies")
    r.add_argument("--abandon-empty", action="store_true", help="abandon ruled stale v1 empty/idle tabs only")
    r.add_argument("--job", action="append", help="explicit ruling job (repeat per job for --abandon-empty)")
    p = sub.add_parser("cleanup"); p.add_argument("--account", required=True, help="only this account; never sweep all accounts")
    s = sub.add_parser("submit")
    s.add_argument("--job", required=True)
    s.add_argument("--prompt", required=True)
    s.add_argument("--attach", nargs="*", help="inline into the message (small files)")
    s.add_argument("--drive-attach", nargs="*", help="upload to Drive inbox/<job> and reference by name (large files)")
    s.add_argument("--account", required=True)
    for field in PROVENANCE_FIELDS:
        s.add_argument('--' + field.replace('_', '-'))
    s.add_argument("--job-class", required=True)
    s.add_argument("--wait", action="store_true")
    s.add_argument("--prompt-on-drive", action="store_true", help="upload the prompt (+inline attachments) to Drive; the message is a short pointer")
    s.add_argument("--dry-run", action="store_true", help="insert the full message into a new chat, verify the length, clear and close; never sends")
    s.add_argument("--interval", type=int, default=30)
    s.add_argument("--max-wait", type=int, default=3600)
    fo = sub.add_parser("fetch-outbox"); fo.add_argument("--job", required=True)
    f = sub.add_parser("followup")
    f.add_argument("--job", required=True)
    f.add_argument("--text-file", help="message text; default = resume-after-disconnect")
    p = sub.add_parser("poll")
    p.add_argument("--job", required=True)
    p.add_argument("--wait", action="store_true")
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--max-wait", type=int, default=3600)
    a = ap.parse_args()
    if a.cmd == "status":
        return cmd_status(a)
    if a.cmd == "reconcile-no-reply":
        return reconcile_no_reply(a.job)
    if a.cmd == "reconcile-unsent-focus":
        return reconcile_unsent_focus(a.job, a.failure_log, apply=a.apply)
    if a.cmd == "reconcile-unsent-model":
        return reconcile_unsent_model(a.job, apply=a.apply, report=a.report)
    if a.cmd == "reconcile-unsent-send-disabled":
        return reconcile_unsent_send_disabled(a.job, apply=a.apply)
    if a.cmd == "reconcile-legacy":
        if a.terminalize and a.abandon_empty:
            ap.error('--terminalize and --abandon-empty are mutually exclusive')
        if a.abandon_empty:
            return abandon_empty_legacy(jobs=a.job or [], apply=a.apply)
        if a.job:
            ap.error('--job requires --abandon-empty')
        return terminalize_legacy(apply=a.apply) if a.terminalize else reconcile_legacy(apply=a.apply)
    if a.cmd == "cleanup":
        import socket
        # 2026-09-17: a wave runner's start-up cleanup iterated EVERY account and closed tabs on accounts whose own
        # waves were mid-flight (RELAY-INCIDENTS.md, first signature). Scope to one account when asked.
        for x in load_config()["accounts"]:
            if getattr(a, "account", None) and x["label"] != a.account:
                continue
            try:
                with socket.create_connection(("127.0.0.1", int(x.get("cdp_port", 9340))), timeout=2):
                    pass
            except OSError:
                continue
            use_account(x); print(f"{x['label']}: closed {cleanup_tabs()} stray tabs")
        return 0
    if a.cmd == "submit":
        return cmd_submit(a)
    if a.cmd == "fetch-outbox":
        return cmd_fetch_outbox(a)
    if a.cmd == "followup":
        return cmd_followup(a)
    if a.cmd == "poll":
        return poll(a.job, a.wait, a.interval, a.max_wait)
    return 2


if __name__ == "__main__":
    sys.exit(main())
