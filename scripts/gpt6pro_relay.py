#!/usr/bin/env python3
"""GPT-6 Pro relay: drive the operator's ChatGPT web session (GPT-6 Pro) from files.

Authority: ops/linear/approvals/OPERATOR-DECISION-20260916-gpt6pro-browser-relay.json (option A, ToS risk
accepted in the operator's own words). Config: ops/gpt6pro-relay/config.json.

What it is: a read-think-write channel. A job = one prompt file (+ optional attachments) sent as ONE message to a
NEW GPT-6 Pro chat; its hash-bound reply lands in reports/ops/gpt6pro-relay/jobs/<job>/RESPONSE.md.
Inputs go to Drive inbox/<job>; Drive outbox artifacts require a final-chat-bound OUTPUT-MANIFEST.json.
A separate hash-authorized, one-shot `resume-unsent-readiness` path may continue an exact
proven-unsent SECURITY_INTERSTITIAL job on its original retained tab, or the single
source-diagnosed consumed six-PR first-evaluation failure with fresh current custody.
Default is read-only; neither path reuploads, opens or navigates a tab, or clears
retry_blocked merely on readiness.

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
from typing import NoReturn

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

# Only committed, independently reviewed rulings can authorize sent-job disposition.
# A new ruling requires a source PR, never a caller-authored runtime allowlist.
SUPERSESSION_RULINGS = {
    'ops/linear/approvals/OPERATOR-DECISION-20261013-pro-research-supersession.json': '4ad6e16fd15e8734ce965f4ebd42a339cc893f6e53ae8f938cfbab02920f764d',
}


def _supersession_entries(ruling):
    """Authenticate the pinned identities independently of mutable queue links."""
    if ruling not in SUPERSESSION_RULINGS:
        raise ValueError('RULING_NOT_REGISTERED')
    path = BUSINESS / ruling
    raw = path.read_bytes()
    if path.is_symlink() or hashlib.sha256(raw).hexdigest() != SUPERSESSION_RULINGS[ruling]:
        raise ValueError('RULING_HASH')
    facts = json.loads(raw)
    entries = facts.get('jobs')
    if (facts.get('schema') != 'pro-research-supersession-ruling-v1' or
            facts.get('authority') != 'operator' or facts.get('research_only') is not True or
            facts.get('no_side_effects') is not True or not facts.get('verbatim') or
            not facts.get('decision_date') or not isinstance(entries, list) or not entries):
        raise ValueError('RULING_SCOPE')
    originals = [r['job'] for r in entries]
    replacements = [r['replacement_job'] for r in entries]
    if (len(set(originals)) != len(entries) or len(set(replacements)) != len(entries) or
            set(originals) & set(replacements) or any(not isinstance(j, str) or
            not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', j) or j in ('.', '..')
            for j in originals + replacements)):
        raise ValueError('RULING_IDENTITIES')
    return entries, raw


def _supersession_ruling(ruling, job):
    entries, raw = _supersession_entries(ruling)
    matching = [r for r in entries if r['job'] == job]
    if len(matching) != 1 or matching[0].get('linear_child') == 'GLP-336':
        raise ValueError('RULING_JOB')
    return matching[0], raw


def _supersession_evidence(cfg, st, preimage, ruling):
    """Authenticate the complete sent preimage, unchanged packet and original journal."""
    entry, raw = _supersession_ruling(ruling, st['job'])
    alias = entry['account_alias']
    if not re.fullmatch(r'acct[123]', alias):
        raise ValueError('ACCOUNT_ALIAS')
    account = cfg['accounts'][int(alias[4:]) - 1]['label']
    if (json.loads(preimage) != st or hashlib.sha256(preimage).hexdigest() != entry['job_sha256'] or
            st.get('status') != 'running' or st.get('job_class') != 'research-synthesis' or
            st.get('typed') is not True or st.get('sent') is not True or
            st.get('retry_blocked') is not False or st.get('account') != account or
            st.get('linear_child') != entry['linear_child'] or st.get('linear_child') == 'GLP-336' or
            st.get('input_sha256') != entry['input_sha256'] or
            st.get('binding_version') != 2 or not binding_url(st) or
            st.get('conversation_id') != binding_url(st).rsplit('/', 1)[1] or
            not isinstance(st.get('tab'), str) or not st['tab'] or
            not all(isinstance(st.get(k), str) and st[k] for k in PROVENANCE_FIELDS)):
        raise ValueError('SENT_BOUND_RESEARCH_STATE')
    d = JOBS / st['job']
    if (d / 'RESPONSE.md').exists() or (d / 'outbox').exists():
        raise ValueError('OUTPUT_PRESENT')
    files = entry['files']
    if not isinstance(files, dict) or not files:
        raise ValueError('PACKET_PINS')
    custody = {}
    for name, digest in files.items():
        p = BUSINESS / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or
                p.resolve() != p.absolute() or not p.resolve().is_relative_to(BUSINESS.resolve()) or
                not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError('PACKET_PATH')
        content = p.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError('PACKET_HASH')
        custody[p] = content
    required = [d / n for n in ('PROMPT.md', 'MANIFEST.json', 'MESSAGE.md')]
    if not all(p in custody for p in required):
        raise ValueError('PACKET_INCOMPLETE')
    if _failed_generation_identity(st) not in custody[d / 'MESSAGE.md'].decode():
        raise ValueError('MESSAGE_IDENTITY')
    manifest = json.loads(custody[d / 'MANIFEST.json'])
    paths = [d / 'PROMPT.md', *map(Path, manifest['attach']), *map(Path, manifest['drive_attach'])]
    if any(p not in custody for p in paths):
        raise ValueError('PACKET_INCOMPLETE')
    packet = [(p.name, hashlib.sha256(custody[p]).hexdigest()) for p in paths]
    if hashlib.sha256(json.dumps(packet, separators=(',', ':')).encode()).hexdigest() != st['input_sha256']:
        raise ValueError('INPUT_HASH')
    ledger = ledger_path(cfg)
    custody[ledger] = ledger.read_bytes()
    all_rows = [(line, json.loads(line)) for line in custody[ledger].splitlines() if line.strip()]
    job_lines = [line for line, row in all_rows if row.get('job') == st['job']]
    n = len(entry['ledger_lines_sha256'])
    if not n or [hashlib.sha256(line).hexdigest() for line in job_lines[:n]] != entry['ledger_lines_sha256']:
        raise ValueError('JOURNAL_PREIMAGE')
    rows = [json.loads(line) for line in job_lines]
    bound = [r for r in rows[:n] if r.get('event') == 'submission_bound']
    if (len(bound) != 1 or bound[0].get('account') != st['account'] or
            _conversation(bound[0].get('conversation_url')) != binding_url(st) or
            any(bound[0].get(k) != st[k] for k in PROVENANCE_FIELDS)):
        raise ValueError('JOURNAL_BINDING')
    custody[BUSINESS / ruling] = raw
    return entry, raw, custody, rows, job_lines


def _supersession_observations(st, rows):
    """Only exact native read-only point observations may follow the ruling prefix."""
    fields = ('stop', 'logged_out', 'security_interstitial', 'human_challenge',
              'rate_limited', 'drive_reconnect', 'thinking_failed', 'user_identity_matched')
    for row in rows:
        observed = row.get('observed', {})
        if set(observed) != set(fields) or any(type(v) is not bool and v is not None for v in observed.values()):
            raise ValueError('JOURNAL_OBSERVATION')
        generating = (False if any(v is not None and v != (k in ('stop', 'user_identity_matched'))
                                  for k, v in observed.items()) else
                      None if any(v is None for v in observed.values()) else True)
        expected = {'ts': row['ts'], 'event': 'generation_observation',
                    'schema': 'gpt6pro-generation-observation-v1', 'coverage': 'single_bound_read_only',
                    'read_started_at': row['read_started_at'], 'read_finished_at': row['read_finished_at'],
                    'job': st['job'], 'account': st['account'], 'conversation_url': binding_url(st),
                    'conversation_id': st['conversation_id'], 'binding_version': 2, 'tab': st['tab'],
                    'observed': observed, 'authenticated_generation_observed': generating,
                    'reply_token_sha256': hashlib.sha256(st['reply_token'].encode()).hexdigest(),
                    **{key: st[key] for key in (*PROVENANCE_FIELDS, 'job_class', 'input_sha256')}}
        times = [datetime.fromisoformat(row[key]) for key in ('read_started_at', 'read_finished_at', 'ts')]
        if (row != expected or type(row.get('authenticated_generation_observed')) not in (bool, type(None)) or
                not all(t.tzinfo for t in times) or times != sorted(times)):
            raise ValueError('JOURNAL_OBSERVATION')


def _supersession_event(st, entry, ruling, raw, at, journal_lines):
    return {'event': 'research_supersession', 'job': st['job'], 'account': st['account'],
            'from_status': st['status'], 'to_status': 'SUPERSEDED',
            'replacement_job': entry['replacement_job'], 'ruling': ruling,
            'ruling_sha256': hashlib.sha256(raw).hexdigest(), 'preimage_sha256': entry['job_sha256'],
            'journal_preimage_rows': len(journal_lines),
            'journal_preimage_sha256': hashlib.sha256(b'\n'.join(journal_lines)).hexdigest(),
            'conversation_url': binding_url(st), 'retained_tab': st['tab'], 'observed_at': at}


def _verified_supersession(st):
    """An interrupted or tampered disposition NEVER frees active capacity."""
    try:
        if st.get('status') != 'SUPERSEDED' or job_state(st['job']) != st:
            return False
        proof = st['supersession']
        preimage = (JOBS / st['job'] / ('JOB.pre-supersession-' + proof['preimage_sha256'] + '.json')).read_bytes()
        original = json.loads(preimage)
        entry, raw, _, rows, lines = _supersession_evidence(load_config(), original, preimage, proof['ruling'])
        n = len(rows) - 2
        if n < len(entry['ledger_lines_sha256']):
            return False
        _supersession_observations(original, rows[len(entry['ledger_lines_sha256']):n])
        expected = _supersession_event(original, entry, proof['ruling'], raw, proof['observed_at'], lines[:n])
        return (datetime.fromisoformat(proof['observed_at']).tzinfo is not None and proof == expected and
                st == {**original, 'status': 'SUPERSEDED', 'retry_blocked': True, 'supersession': proof} and
                len(rows) == n + 2 and all(r == {'ts': r.get('ts'), **proof, 'phase': phase}
                                         for r, phase in zip(rows[n:], ('STARTED', 'APPLIED'))))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return False


def supersede_research(job, ruling, *, apply=False, authorize_preimage=None):
    """Forward-only ruled disposition. Never stop, close, reload, send, fetch or erase.

    Old tabs remain protected and the original ID is permanently retry-blocked.
    --apply requires the exact original JOB digest; dry-run changes no custody.
    Coordination lock files may be created by the existing lock helpers.
    Replacement creation/submission is a separate installed dispatcher action.
    """
    def refuse(reason):
        raise SystemExit('SUPERSESSION_REFUSED_' + reason + ' ' + job)
    try:
        entry, _ = _supersession_ruling(ruling, job)
        if (apply and authorize_preimage != entry['job_sha256']) or (not apply and authorize_preimage is not None):
            refuse('AUTHORITY')
        cfg = load_config()
        st = job_state(job)
        use_account(account_of(cfg, st.get('account')))
        with account_lock(), global_lock():
            path = JOBS / job / 'JOB.json'
            preimage = path.read_bytes(); st = json.loads(preimage)
            if st.get('status') == 'SUPERSEDED':
                if not _verified_supersession(st) or st['supersession']['ruling'] != ruling:
                    refuse('INCOMPLETE_OR_CHANGED_HISTORY')
                print('ALREADY_SUPERSEDED job=' + job)
                return 0
            if st.get('job') != job:
                refuse('JOB_IDENTITY')
            entry, raw, custody, rows, lines = _supersession_evidence(cfg, st, preimage, ruling)
            _supersession_observations(st, rows[len(entry['ledger_lines_sha256']):])
            replacement = JOBS / entry['replacement_job']
            if replacement.exists() or any(r.get('job') == entry['replacement_job'] for r in ledger_rows(cfg)):
                refuse('REPLACEMENT_ALREADY_EXISTS')
            require_binding(st)
            if _legacy_live_jobs():
                refuse('LIVE_RUNNER')
            backup = path.with_name('JOB.pre-supersession-' + entry['job_sha256'] + '.json')
            if backup.exists():
                refuse('PREIMAGE_ALREADY_EXISTS')
            def recheck():
                if (path.read_bytes() != preimage or any(p.read_bytes() != b for p, b in custody.items()) or
                        replacement.exists() or (path.parent / 'RESPONSE.md').exists() or
                        (path.parent / 'outbox').exists() or _legacy_live_jobs()):
                    refuse('DRIFT')
            recheck()
            if not apply:
                print('WOULD_SUPERSEDE job=' + job + ' replacement=' + entry['replacement_job'])
                return 0
            with backup.open('xb') as fh:
                fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
            recheck()
            event = _supersession_event(st, entry, ruling, raw, now_iso(), lines)
            ledger_append(cfg, {**event, 'phase': 'STARTED'})
            job_write(job, {**st, 'status': 'SUPERSEDED', 'retry_blocked': True, 'supersession': event})
            ledger_append(cfg, {**event, 'phase': 'APPLIED'})
            if not _verified_supersession(job_state(job)):
                refuse('READBACK')
            print('SUPERSEDED job=' + job + ' replacement=' + entry['replacement_job'] + '; retained tab protected')
            return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        refuse(type(exc).__name__ + '_' + str(exc))


def active_registry():
    """JOB.json is the account-global reservation, not a wave or live PID count."""
    rows = []
    for path in sorted(JOBS.glob('*/JOB.json')):
        st = json.loads(path.read_text())
        if ((st.get('status') == 'SUPERSEDED' and
                (st.get('job') != path.parent.name or not _verified_supersession(st))) or
                st.get('status') in ACTIVE_STATES or (st.get('retry_blocked') and
                st.get('status') != 'SUPERSEDED' and
                not (st.get('status') == 'failed' and st.get('reason') == 'THINKING_FAILED') and
                not (st.get('job') == path.parent.name and _verified_terminal_unsent(st)))):
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
        if sum(r['account'] == account['label'] for r in rows) >= min(2, envelope['max_concurrent_chats_per_account']):
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
        if result is not None and result.get("tab") and result["tab"] in custody_tabs(sibling):
            raise SystemExit(f"TAB_COLLISION job={st['job']} sibling={sibling.get('job')}")
    if result is not None:
        state = result.get("state") or {}
        if (result.get("error") or not result.get("tab") or
                _conversation(state.get("url")) != want or
                _conversation(state.get("conversation_url_now")) != want):
            raise SystemExit(f"CONVERSATION_MISMATCH job={st['job']}")


def custody_tabs(st: dict) -> list[str]:
    """Conserve every attributed target, independently of admission-proof validity."""
    tabs = [st.get('tab')]
    for key, field in (('error', 'tab'), ('resume_error', 'tab'),
                       ('terminal_unsent', 'retained_tab'), ('resume_attempt', 'retained_tab')):
        row = st.get(key)
        if isinstance(row, dict):
            tabs.append(row.get(field))
    history = st.get('resume_history')
    if isinstance(history, list):
        tabs.extend(row.get('retained_tab') for row in history if isinstance(row, dict))
    # Conflicting attribution protects the union; it never selects a winner or
    # proves a reservation terminal. Malformed target values fail closed.
    if any(tab is not None and (not isinstance(tab, str) or not tab) for tab in tabs):
        raise SystemExit('BINDING_REGISTRY_INVALID_TARGET ' + str(st.get('job')))
    return sorted({tab for tab in tabs if tab is not None})


def protected_tabs(st: dict) -> list[str]:
    return sorted({tab for s in sibling_bindings(st['account'], st['job']) for tab in custody_tabs(s)})


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
    cfg = load_config()
    envelope = cfg.get('envelope', {})
    cap = min(3, envelope.get('max_concurrent_chats_per_account', 3)) + 2
    # Terminal dispositions release active capacity but must retain their exact
    # evidence tabs. Account for only byte/journal-verified terminal custody;
    # ordinary uncertain targets get NO extra allowance. Active admission stays
    # three/account and six/IP under the existing global reservation lock.
    accounts = [a for a in cfg.get('accounts', [])
                if f"http://127.0.0.1:{a.get('cdp_port', 9340)}" == BU_ENV['BU_CDP_URL']]
    if len(accounts) == 1:
        retained = {t for s in sibling_bindings(accounts[0]['label'], '')
                    if _verified_terminal_unsent(s) or _verified_supersession(s) for t in custody_tabs(s)}
        cap += len(retained)
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
      const trigger=document.querySelector('button[aria-label="Select ChatGPT model"]');
      const composer=document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]');
      const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).display!=='none'&&getComputedStyle(e).visibility!=='hidden';
      const security_text=document.title+'\\n'+document.body.innerText.slice(0,2000);
      const security_interstitial=/just a moment|checking your browser|performing security verification/i.test(security_text);
      // Conservatively preserve visible CAPTCHA frames; never inspect/solve inside them.
      const human_challenge=/verify (that )?you are human|confirm (that )?you are human|complete the security check|verify you'?re human/i.test(security_text)||
        [...document.querySelectorAll('iframe')].some(e=>visible(e)&&/challenges\\.cloudflare\\.com|recaptcha|hcaptcha/i.test(e.src+' '+e.title));
      return {url:location.href,title:document.title,model:mb||null,logged_out:login,stop:stop,drive_reconnect:drive_reconnect,rate_limited:rate_limited,send:send?!send.disabled:null,assistant_count:msgs.length,last_len:last.length,thinking_failed:thinking_failed,
        ready_state:document.readyState,composer_present:visible(composer),model_trigger:visible(trigger)?trigger.innerText.trim():null,security_interstitial:security_interstitial,human_challenge:human_challenge};
    })()""")
'''


# Home is the supported app-shell transition out of Codex Cloud. Use it only
# on the relay's newly created, empty literal root; never on retained documents.
ROOT_CHAT_HOME_JS = r'''(()=>{
    if(location.href!=='https://chatgpt.com/'||
       document.querySelector('button[aria-label="Select ChatGPT model"]')||
       document.querySelector('button[data-testid="stop-button"]')||
       document.querySelector('[data-message-author-role], [data-chatgpt-selection-message-id]'))return null;
    const mode=document.querySelector('button[aria-label="Work in"]');
    if(!mode||mode.innerText.trim()!=='Cloud'||!document.body.innerText.includes('GPT-6.1 Sol'))return null;
    const editor=document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]');
    if(!editor||editor.textContent.trim()||editor.innerText.trim())return null;
    const homes=[...document.querySelectorAll('button')].filter(e=>e.innerText.trim()==='Home'&&
        !e.disabled&&e.getClientRects().length&&getComputedStyle(e).display!=='none'&&getComputedStyle(e).visibility!=='hidden');
    if(homes.length!==1)return null;
    const rect=homes[0].getBoundingClientRect();
    return {x:rect.x+rect.width/2,y:rect.y+rect.height/2};
})()'''


def browser_state(url: str | None = None) -> dict:
    # Read the state of a chatgpt.com tab, never the daemon's own about:blank tab: on 2026-09-17 `status` reported
    # account 3 as logged_out=False while its only ChatGPT tab was the logged-out landing page (the daemon's current
    # tab was about:blank, where the login-marker probe finds nothing and reads as "logged in").
    focus = ('tid=next((t["targetId"] for t in list_tabs() if (t.get("url") or "").startswith("https://chatgpt.com")),None)\n'
             'if tid: switch_tab(tid)\n')
    script = BU_PRELUDE + (f'_goto({url!r})\ntime.sleep(2)\n' if url else focus) + 'print("RELAY_JSON:"+json.dumps(_state()))\n'
    return bu_json(script)


# The observed plain ProseMirror editor is DIV > P > text, or a sole
# trailingBreak placeholder for an empty P. innerText inserts presentation LFs;
# textContent discards paragraph boundaries. Neither is the logical message.
# Reject unobserved rich structures; do not trim or normalize any codepoint.
COMPOSER_TEXT_JS = r'''function composerText(e){
    if(!e||e.tagName!=='DIV'||e.getAttribute('contenteditable')!=='true'||
       !e.classList.contains('ProseMirror'))return null;
    const lines=[];
    for(const p of e.childNodes){
        if(p.nodeType!==1||p.tagName!=='P')return null;
        const kids=[...p.childNodes];
        if(kids.length===1&&kids[0].nodeType===1&&kids[0].tagName==='BR'&&
           kids[0].className==='ProseMirror-trailingBreak'&&kids[0].attributes.length===1){
            // The captured initial editor has this exact placeholder only on
            // its sole P. Do not generalize decorations to message paragraphs.
            const initialPlaceholder=e.childNodes.length===1&&p.attributes.length===3&&
                p.getAttribute('data-empty-paragraph')==='true'&&
                p.getAttribute('data-placeholder')==='Ask ChatGPT'&&
                p.getAttribute('class')==='placeholder';
            // The earlier full-draft capture records marker presence only.
            if(!initialPlaceholder&&p.attributes.length!==0&&!(p.attributes.length===1&&
               p.hasAttribute('data-empty-paragraph')))return null;
            lines.push('');continue;
        }
        if(p.attributes.length!==0||!kids.length||kids.some(n=>n.nodeType!==3))return null;
        lines.push(kids.map(n=>n.data).join(''));
    }
    return lines.length?lines.join('\n'):null;
}'''


# Chat/Work is independent of selectedModel. The sender verifies only; idle
# mode correction is a separately authorized UI action, never a send fallback.
PRO_CHAT_SURFACE_JS = r'''function ensureProChatSurface(diagnose=true){
    const visible=e=>{
        if(!e.getClientRects().length)return false;
        for(let p=e;p;p=p.parentElement){
            const s=getComputedStyle(p);
            if(p.hidden||p.inert||p.disabled||p.getAttribute('aria-hidden')==='true'||
               p.getAttribute('aria-disabled')==='true'||s.display==='none'||s.visibility==='hidden')return false;
        }return true;
    };
    const main=document.querySelector('main');
    let groups=[],scope=main;
    // Capture only structural facts from the deciding evaluation. Never retain
    // draft text, arbitrary attributes, HTML or account/conversation metadata.
    // Diagnostics are emitted on refusal only; bounded samples never limit the
    // full uniqueness/visibility predicate used for the decision.
    const refuse=reason=>{
        const result={error:'CHAT_SURFACE_REFUSED',reason:reason};
        if(!diagnose)return result;
        const excluded=e=>{
            if(!e.getClientRects().length)return 'no_rect';
            for(let p=e;p;p=p.parentElement){
                const s=getComputedStyle(p);
                if(p.hidden)return 'hidden';if(p.inert)return 'inert';if(p.disabled)return 'disabled';
                if(p.getAttribute('aria-hidden')==='true')return 'aria_hidden';
                if(p.getAttribute('aria-disabled')==='true')return 'aria_disabled';
                if(s.display==='none')return 'display_none';if(s.visibility==='hidden')return 'visibility_hidden';
            }return null;
        };
        const buttons=scope?[...scope.querySelectorAll('button')]:[];
        const candidates=label=>{
            const raw=buttons.filter(e=>(e.textContent||e.innerText).trim()===label);
            return {raw_count:raw.length,filtered_count:raw.filter(visible).length,
                overflow:raw.length>8,candidates:raw.slice(0,8).map(e=>{
                    const p=e.getAttribute('aria-pressed');
                    return {pressed:p===null?'missing':['true','false','mixed'].includes(p)?p:'other',
                        excluded:excluded(e)};
                })};
        };
        result.diagnostics={main_present:!!main,
            main_count:typeof document.querySelectorAll==='function'?document.querySelectorAll('main').length:null,
            group_count:groups.length,scope:groups.length===1?'composer_mode':main?'first_main':'none',
            group_excluded:groups.length===1?excluded(groups[0]):null,
            chat:candidates('Chat'),work:candidates('Work')};
        return result;
    };
    if(!main)return refuse('mode_missing');
    // The captured app shell identifies the toggle independently of other
    // Chat/Work-labelled buttons. Retain the old main-only contract if absent.
    groups=typeof main.querySelectorAll==='function'?
        [...main.querySelectorAll('[role="group"][aria-label="Composer mode"]')]
            .filter(e=>e.getAttribute('aria-label')==='Composer mode'):[];
    if(groups.length===1)scope=groups[0];
    if(groups.length>1)return refuse('mode_ambiguous');
    if(groups.length===1&&!visible(groups[0]))return refuse('mode_unverified');
    const buttons=[...scope.querySelectorAll('button')].filter(visible);
    const label=e=>(e.textContent||e.innerText).trim();
    const chat=buttons.filter(e=>label(e)==='Chat');
    const work=buttons.filter(e=>label(e)==='Work');
    if(!chat.length&&!work.length)return refuse('mode_missing');
    if(chat.length!==1||work.length!==1)return refuse('mode_ambiguous');
    if(chat[0].getAttribute('aria-pressed')==='true'&&work[0].getAttribute('aria-pressed')==='false')return {switched:false};
    if(work[0].getAttribute('aria-pressed')==='true')return refuse('work_selected');
    return refuse('mode_unverified');
}'''


# Current picker visibility belongs to its active view ancestor, not the row.
# Keep the complete label, maximum badge, exact scale and actionable menu proof.
MODEL_PICKER_JS = PRO_CHAT_SURFACE_JS + r'''function pickerActionable(e,visualThumb=false){
        if(!e||!e.getClientRects().length)return false;
        for(let p=e;p;p=p.parentElement){
            const s=getComputedStyle(p);
            const hidden=p.getAttribute('aria-hidden')==='true'&&!(visualThumb&&p===e&&p.getAttribute('role')==='slider');
            if(p.hidden||p.inert||hidden||
               p.getAttribute('aria-disabled')==='true'||p.disabled===true||
               p.getAttribute('data-active')==='false'||
               s.display==='none'||s.visibility==='hidden')return false;
        }return true;
}
function pickerTrigger(){
    const triggers=[...document.querySelectorAll('button[aria-label="Select ChatGPT model"]')].filter(e=>pickerActionable(e));
    return triggers.length===1?triggers[0]:null;
}
function pickerMenu(){
    const trigger=pickerTrigger();
    if(!trigger||trigger.getAttribute('aria-expanded')!=='true')return null;
    const menus=[...document.querySelectorAll('[id]')].filter(e=>e.id===trigger.getAttribute('aria-controls'));
    return menus.length===1&&pickerActionable(menus[0])&&menus[0].getAttribute('role')==='menu'?menus[0]:null;
}
function committed6ProMaximum(){
    // Revalidate Chat at every existing initial/chunk/Send admission, including
    // retained atomic Send. A stale Pro selection never authorizes Work quota.
    if(ensureProChatSurface(false).error)return false;
    const trigger=pickerTrigger();
    // The trigger's candidate effort also labels staged state. Only the closed
    // selection owner's incoming selectedModel is a parent-acceptance witness.
    // This follows the supported maximum action + full 6 Pro/maximum/4-of-4
    // proof below; null is the observed Pro model effort, NOT medium => maximum.
    if(!trigger||trigger.getAttribute('aria-expanded')!=='false'||
       trigger.getAttribute('data-codex-intelligence-trigger')!=='true'||
       trigger.getAttribute('data-composer-navigation-target')!=='reasoning'||
       !['Pro','Thinking effort','6 Pro'].includes(trigger.innerText.trim()))return false;
    const own=(o,k)=>{
        if(!o||(typeof o!=='object'&&typeof o!=='function'))return undefined;
        const d=Object.getOwnPropertyDescriptor(o,k);
        if(d&&!('value'in d))throw new Error('accessor');
        return d?.value;
    };
    const contract=['selectedModel','onModelChange','models','modelLimits',
        'isLoading','isError','disabled','xHighExperimentResetContextKey'];
    const sourceContract=['selectedModel:','onModelChange:','onThinkingEffortChange:',
        'onCommitPower:','onOpenChange:','CHATGPT_MODEL_PICKER_SURFACE_COMPOSER'];
    function selected(p){
        if(!p||!contract.every(k=>{
            const d=Object.getOwnPropertyDescriptor(p,k);return d&&'value'in d;
        }))return false;
        const model=own(p,'selectedModel');
        return own(p,'disabled')===false&&own(p,'isLoading')===false&&own(p,'isError')===false&&
            typeof own(p,'onModelChange')==='function'&&
            own(model,'slug')==='gpt-6-pro'&&own(model,'thinkingEffort')===null;
    }
    try{
        const keys=Object.getOwnPropertyNames(trigger).filter(k=>k.startsWith('__reactFiber$'));
        if(keys.length!==1)return false;
        let f=own(trigger,keys[0]),owners=0,accepted=false;
        const seen=new Set();
        for(let depth=0;f&&depth<80;depth++,f=own(f,'return')){
            if(seen.has(f))return false;seen.add(f);
            const type=own(f,'type');
            if(typeof type!=='function')continue;
            const source=Function.prototype.toString.call(type);
            if(!sourceContract.every(s=>source.includes(s)))continue;
            // Recognize before schema validation: malformed owners must not
            // disappear from uniqueness, even when their getters are unread.
            if(++owners!==1)return false;
            const p=own(f,'memoizedProps');
            // A DOM fiber can reference either React buffer. Never guess that
            // stale Pro props are current: both buffers must agree if present.
            const alternate=own(f,'alternate');
            accepted=selected(p)&&(alternate==null||(own(alternate,'type')===type&&
                selected(own(alternate,'memoizedProps'))));
        }
        return owners===1&&accepted;
    }catch(_){return false;}
}
function picker6ProLabel(row,view){
    const label=row.innerText.trim().replace(/\s+/g,' ');
    if(label==='6 Pro')return true;
    // Observed default-family UI abbreviates 6 Pro as Pro. Never trust
    // Pro alone: require the same picker to name exactly one selected GPT-6
    // family, the explicit default-family/effort-only schema, and the existing
    // maximum/4-of-4 gate below. The CLOSED incoming-parent gpt-6-pro/null
    // witness remains mandatory before typing and again at Send.
    if(label!=='Pro')return false;
    const owner=row.closest('[data-explicit-model]');
    const badge=row.querySelector('[data-maximum=true][data-effort-only=true]');
    if(!owner||owner.getAttribute('data-explicit-model')!=='false'||
       !badge||badge.getAttribute('data-effort-only')!=='true')return false;
    const selected=[...view.querySelectorAll('[role=menuitemradio][aria-checked=true]')];
    return selected.length===1&&selected[0].innerText.trim()==='GPT-6'&&
        selected[0].getAttribute('aria-checked')==='true'&&
        selected[0].getAttribute('data-model-selected')==='true';
}
function full6ProMaximum(){
    const actionable=pickerActionable,menu=pickerMenu();
    if(!menu)return false;
    const views=[...menu.querySelectorAll('[data-model-picker-view=simple]')].filter(e=>actionable(e));
    if(views.length!==1)return false;
    const view=views[0];
    const rows=[...view.querySelectorAll('[role=menuitem][data-model-picker-view-toggle=true]')].filter(e=>actionable(e));
    if(rows.length!==1)return false;
    const row=rows[0],owner=row.closest('[aria-hidden]');
    if(!owner||owner.getAttribute('aria-hidden')!=='false'||
       !picker6ProLabel(row,view))return false;
    const badge=row.querySelector('[data-maximum=true]');
    // The observed thumb is aria-hidden=true; its active Power menuitem is
    // the accessible control. Never ignore hidden/inert ancestry or disablement.
    const powers=[...view.querySelectorAll('[data-reasoning-slider][role=menuitem][aria-label=Power]')].filter(e=>actionable(e));
    if(powers.length!==1)return false;
    const sliders=[...powers[0].querySelectorAll('[role=slider]')].filter(e=>actionable(e,true));
    if(!actionable(badge)||sliders.length!==1)return false;
    const slider=sliders[0];
    return slider.getAttribute('aria-valuemin')==='0'&&
           slider.getAttribute('aria-valuenow')==='4'&&slider.getAttribute('aria-valuemax')==='4';
}'''


def send_message(text: str, dry: bool = False, forbidden: list[str] | tuple[str, ...] = (),
                 retained_tab: str | None = None, time_origin: float | None = None) -> dict:
    """Verify full 6 Pro/maximum gate and send; only explicit resume uses an existing target."""
    if retained_tab and (dry or type(time_origin) not in (int, float) or time_origin <= 0):
        raise ValueError('retained submission requires a bound document and effectful authority')
    payload = json.dumps(text)
    full_picker_probe = '(()=>{' + MODEL_PICKER_JS + 'return full6ProMaximum();})()'
    committed_probe = '(()=>{' + MODEL_PICKER_JS + 'return committed6ProMaximum();})()'
    open_picker = '(()=>{' + MODEL_PICKER_JS + "const b=pickerTrigger();if(!b)return false;if(b.getAttribute('aria-expanded')!=='true')b.click();return true;})()"
    maximum_geometry = '(()=>{' + MODEL_PICKER_JS + r'''const menu=pickerMenu();if(!menu)return null;
        const views=[...menu.querySelectorAll('[data-model-picker-view=simple]')].filter(pickerActionable);
        if(views.length!==1)return null;
        const powers=[...views[0].querySelectorAll('[data-reasoning-slider][role=menuitem][aria-label=Power]')].filter(pickerActionable);
        if(powers.length!==1)return null;
        const sliders=[...powers[0].querySelectorAll('[role=slider]')].filter(e=>pickerActionable(e,true));
        const rails=[...views[0].querySelectorAll('[data-model-picker-power-slider]')].filter(pickerActionable);
        if(sliders.length!==1||rails.length!==1||sliders[0].getAttribute('aria-valuemin')!=='0'||
           sliders[0].getAttribute('aria-valuemax')!=='4')return null;
        const r=rails[0].getBoundingClientRect();return r.width>100&&r.height>10?{x:r.right-13,y:r.top+r.height/2}:null;})()'''
    # The captured trigger toggles on pointerdown, not HTMLElement.click().
    # Geometry is only an input target; unchanged closed/parent readback below
    # still proves acceptance after the supported CDP press/release pair.
    close_picker = '(()=>{' + COMPOSER_TEXT_JS + MODEL_PICKER_JS + r'''
        if(composerText(document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]'))!==''||
           document.querySelectorAll('[data-message-author-role]').length)return null;
        const b=pickerTrigger();if(!b||!pickerMenu()||!full6ProMaximum())return null;
        const r=b.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
        return r.width>0&&r.height>0&&Number.isFinite(x)&&Number.isFinite(y)?{x,y}:null;})()'''
    closed_probe = '(()=>{' + MODEL_PICKER_JS + "const b=pickerTrigger();return !!b&&b.getAttribute('aria-expanded')==='false'&&committed6ProMaximum();})()"
    fresh_click = '(()=>{' + MODEL_PICKER_JS + '''if(!committed6ProMaximum())return false;
        const b=document.querySelector('button[data-testid="send-button"]')||document.querySelector('button[type="submit"][aria-label="Send"]');
        if(!b||b.disabled)return false;b.click();return true;})()'''
    if retained_tab:
        guard = (COMPOSER_TEXT_JS + 'if(performance.timeOrigin!==' + json.dumps(time_origin) +
            "||composerText(document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]'))!=='')return false;")
        open_picker = open_picker.replace('(()=>{', '(()=>{' + guard, 1)
        close_picker = close_picker.replace('(()=>{', '(()=>{' + guard, 1)
    # Retained sends must re-prove document, exact original draft and full
    # model/readiness in the synchronous evaluation that clicks Send.
    retained_click = ("(()=>{const retainedOrigin=" + json.dumps(time_origin) +
        ";const intended=" + payload + ";" + COMPOSER_TEXT_JS + MODEL_PICKER_JS + r'''
        if(performance.timeOrigin!==retainedOrigin)return {clicked:false,reason:'document'};
        const e=document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]');
        if(composerText(e)!==intended)return {clicked:false,reason:'composer'};
        const visible=x=>!!x&&x.getClientRects().length>0&&getComputedStyle(x).display!=='none'&&getComputedStyle(x).visibility!=='hidden';
        const body=document.body.innerText.slice(0,2000);
        const security=document.title+'\n'+body;
        if(document.readyState!=='complete'||!visible(e)||
           document.querySelector('[data-message-author-role]')||
           /just a moment|checking your browser|performing security verification|verify (that )?you are human|confirm (that )?you are human|complete the security check|verify you'?re human|too many requests/i.test(security)||
           [...document.querySelectorAll('iframe')].some(f=>visible(f)&&/challenges\.cloudflare\.com|recaptcha|hcaptcha/i.test(f.src+' '+f.title))||
           document.querySelector('button[data-testid="login-button"]')||
           [...document.querySelectorAll('button')].some(b=>/^Log in$/.test(b.innerText.trim()))||
           document.querySelector('button[data-testid="stop-button"]')||
           /Reconnect Google Drive/.test(body)||
           (()=>{const main=document.querySelector('main');return !!main&&[...main.querySelectorAll('div[class*="group/activity-header"] span, button')].some(x=>x.textContent.trim()==='Thinking failed'&&(x.matches('button')||x.children.length===0));})())return {clicked:false,reason:'readiness'};
        if(!committed6ProMaximum())return {clicked:false,reason:'model'};
        const b=document.querySelector('button[data-testid="send-button"]')||document.querySelector('button[type="submit"][aria-label="Send"]');
        if(!b||b.disabled)return {clicked:false,reason:'send_disabled'};
        b.click();return {clicked:true};})()''')
    # Serialize this JS as a Python string literal once, instead of relying on
    # escaping through both the outer f-string and the generated Python script.
    retained_evidence = r'''(()=>{const e=document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]');const body=document.body.innerText.slice(0,2000);return {origin:performance.timeOrigin,users:document.querySelectorAll('[data-message-author-role=user]').length,assistants:document.querySelectorAll('[data-message-author-role=assistant]').length,empty:!!e&&!e.innerText.trim(),challenge:/verify (that )?you are human|confirm (that )?you are human|complete the security check|verify you'?re human/i.test(document.title+'\n'+body)||[...document.querySelectorAll('iframe')].some(f=>f.getClientRects().length>0&&/challenges\.cloudflare\.com|recaptcha|hcaptcha/i.test(f.src+' '+f.title)),interstitial:/just a moment|checking your browser|performing security verification/i.test(document.title+'\n'+body),login:!!document.querySelector('button[data-testid="login-button"]'),rate:/too many requests/i.test(document.title+'\n'+body),stop:!!document.querySelector('button[data-testid="stop-button"]'),reconnect:/Reconnect Google Drive/.test(body)};})()'''
    prepare_js = COMPOSER_TEXT_JS + r'''
        const e=document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]');
        if(composerText(e)!==prefix)return false;
        e.focus();
        const p=e.lastChild,last=p.lastChild;
        const node=last.nodeType===3?last:p,offset=last.nodeType===3?last.data.length:0;
        const range=document.createRange(),selection=window.getSelection();
        if(!selection)return false;
        range.setStart(node,offset);range.collapse(true);
        selection.removeAllRanges();selection.addRange(range);
        return document.activeElement===e&&selection.isCollapsed&&
            selection.anchorNode===node&&selection.anchorOffset===offset&&
            composerText(e)===prefix;
    '''
    exact_js = COMPOSER_TEXT_JS + r'''
        const e=document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]');
        return composerText(e)===expected;
    '''
    script = BU_PRELUDE + f'''
CHUNK={IPC_CHUNK}
RETAINED={retained_tab!r}
ORIGIN={time_origin!r}
forbidden=set({list(forbidden)!r})
forbidden.update(_conversation(t.get("url")) for t in list_tabs())
forbidden.discard(None)
if RETAINED:
    before_tabs=[t['targetId'] for t in list_tabs()]
    if RETAINED not in before_tabs: _binding_error('retained_target_missing', tab=RETAINED)
    _select(RETAINED)
    tab={{'targetId':RETAINED}}
else:
    tab={{"targetId":_new_tab("https://chatgpt.com/")}}
    landed=_here(tab['targetId'])
    if landed!='https://chatgpt.com/' and not _conversation(landed):
        # Normalize only this newly created, unbound, non-generating target.
        from urllib.parse import urlsplit
        parsed=urlsplit(landed)
        state=_state()
        if state['stop'] or state['human_challenge'] or state['security_interstitial'] or state['logged_out']:
            _binding_error('NON_CHAT_ROUTE_HELD', state=state, tab=tab['targetId'])
        if parsed.scheme=='https' and parsed.netloc=='chatgpt.com':
            _page_load_slot()
            _target(tab['targetId'])
            cdp('Page.navigate', session_id=_sessions[tab['targetId']], url='https://chatgpt.com/')
            for _ in range(50):
                _target(tab['targetId'])
                if _eval(tab['targetId'], 'document.readyState')=='complete': break
                time.sleep(0.3)
def _fresh():
    here=_here(tab["targetId"])
    if RETAINED:
        evidence=_bound_js(tab['targetId'], 'https://chatgpt.com/', {json.dumps(retained_evidence)})
        if (here!='https://chatgpt.com/' or evidence['origin']!=ORIGIN or evidence['users']!=0 or evidence['assistants']!=0 or
                evidence['challenge'] or evidence['interstitial'] or evidence['login'] or evidence['rate'] or evidence['stop'] or evidence['reconnect'] or
                (not _typed and not evidence['empty']) or sorted(before_tabs)!=sorted(t['targetId'] for t in list_tabs())):
            _binding_error('retained_document_changed', tab=tab['targetId'])
        current=_state(lambda expression: _bound_js(tab['targetId'], here, expression))
        if (current['human_challenge'] or current['security_interstitial'] or current['logged_out'] or
                current['rate_limited'] or current['drive_reconnect'] or current['stop'] or
                (current['model'] and not re.match(r'^\\s*6\\s*Pro\\s*$', current['model']))):
            _binding_error('retained_readiness_changed', tab=tab['targetId'])
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
# Readiness is NOT model approval. Only the observed empty Codex root may get
# one supported Home transition; security surfaces get observation only.
# The full model/maximum gate below is unchanged.
ready_started=time.monotonic()
home_repaired=False
for readiness_observations in range(1, 47):
    if _fresh()!=fresh:
        _binding_error("conversation_mismatch", tab=tab["targetId"])
    st=_state(lambda expression: _bound_js(tab["targetId"], fresh, expression))
    readiness={{"elapsed_s":round(time.monotonic()-ready_started, 3),
                "observations":readiness_observations,"limit_s":90}}
    if st["human_challenge"]:
        # Never click, solve, reload or close a human-security surface.
        _binding_error("HUMAN_CHALLENGE", state=st, tab=tab["targetId"], readiness=readiness)
    if st["logged_out"]:
        _binding_error("LOGGED_OUT", state=st, tab=tab["targetId"], readiness=readiness)
    if st["rate_limited"]:
        _binding_error("RATE_LIMITED", state=st, tab=tab["targetId"], readiness=readiness)
    if not RETAINED and not home_repaired and not st['model_trigger'] and not st['security_interstitial'] and st['ready_state']=='complete':
        home=_bound_js(tab['targetId'], fresh, {ROOT_CHAT_HOME_JS!r})
        if home:
            home_repaired=True  # one supported action, not a reload or cookie edit
            _fresh()
            for typ in ('mousePressed','mouseReleased'):
                cdp('Input.dispatchMouseEvent',session_id=_sessions[tab['targetId']],type=typ,
                    x=home['x'],y=home['y'],button='left',clickCount=1)
            time.sleep(1)
            continue
    remaining=90-(time.monotonic()-ready_started)
    if (remaining>=0 and not st["security_interstitial"] and st["ready_state"]=="complete" and
            st["composer_present"] and (st["model"] or st["model_trigger"])):
        break
    if remaining<=0 or readiness_observations==46:
        reason="SECURITY_INTERSTITIAL" if st["security_interstitial"] else "APP_NOT_READY"
        _binding_error(reason, state=st, tab=tab["targetId"], readiness=readiness)
    time.sleep(min(2, remaining))
# Do not touch a model picker on a replacement document after the readiness wait.
if RETAINED: _fresh()
# Verify the independent composer mode before every path's picker. The sender
# never changes Work to Chat; unverified/ambiguous mode refuses before input.
chat_surface=None
# A new empty root can hydrate the composer before its independent mode toggle.
# Observe ONLY missing controls under the same freshness/security checks; do not
# switch Work, retry retained documents, or insert before a positive Chat proof.
for mode_observation in range(11):
    if _fresh()!=fresh:
        _binding_error('conversation_mismatch', tab=tab['targetId'])
    chat_surface=_bound_js(tab['targetId'],fresh,{json.dumps('(()=>{' + PRO_CHAT_SURFACE_JS + 'return ensureProChatSurface();})()')})
    if (not isinstance(chat_surface,dict) or not chat_surface.get('error') or
            chat_surface.get('reason')!='mode_missing' or RETAINED or mode_observation==10):
        break
    current=_state(lambda expression: _bound_js(tab['targetId'],fresh,expression))
    if (current['human_challenge'] or current['security_interstitial'] or current['logged_out'] or
            current['rate_limited'] or current['drive_reconnect'] or current['stop']):
        _binding_error('mode_readiness_changed', tab=tab['targetId'], state=current)
    time.sleep(0.3)
if not isinstance(chat_surface,dict) or chat_surface.get('error'):
    print('RELAY_JSON:'+json.dumps({{'error':'CHAT_SURFACE_REFUSED','typed':False,'sent':False,'tab':tab['targetId'],
        'mode_observation':mode_observation+1,'mode_evidence':chat_surface}})); raise SystemExit(0)
if chat_surface.get('switched') is not False:
    _binding_error('CHAT_SURFACE_REFUSED', tab=tab['targetId'])
# All paths must commit supported 6 Pro maximum explicitly, even when
# button text or an open preview already says 6 Pro/maximum. Those are not
# authoritative selected effort and must never skip this same gate.
if RETAINED: _fresh()
opened=_bound_js(tab['targetId'], fresh, {open_picker!r})
geometry=None
if opened:
    for _ in range(10):
        geometry=_bound_js(tab['targetId'], fresh, {maximum_geometry!r})
        if geometry: break
        time.sleep(0.3)
if not geometry:
    _binding_error('WRONG_MODEL', state=st, tab=tab['targetId'])
_fresh()
for typ in ('mousePressed','mouseReleased'):
    cdp('Input.dispatchMouseEvent', session_id=_sessions[tab['targetId']], type=typ,
        x=geometry['x'], y=geometry['y'], button='left', clickCount=1)
selected=False
for _ in range(10):
    time.sleep(0.3)
    # Some supported controls close on selection. Reopen only for the full
    # 6 Pro/maximum/4-of-4 proof; staged selection is not parent acceptance.
    if not _bound_js(tab['targetId'], fresh, {open_picker!r}): break
    selected=_bound_js(tab['targetId'], fresh, {full_picker_probe!r})
    if selected: break
closed=False
_fresh()
close_geometry=_bound_js(tab['targetId'], fresh, {close_picker!r}) if selected else None
if close_geometry:
    for typ in ('mousePressed','mouseReleased'):
        cdp('Input.dispatchMouseEvent', session_id=_sessions[tab['targetId']], type=typ,
            x=close_geometry['x'], y=close_geometry['y'], button='left', clickCount=1)
    for _ in range(10):
        closed=_bound_js(tab['targetId'], fresh, {closed_probe!r})
        if closed: break
        time.sleep(0.3)
if not selected or not closed or not _bound_js(tab['targetId'], fresh, {committed_probe!r}):
    _binding_error('WRONG_MODEL', state=st, tab=tab['targetId'])
st['model']='6 Pro'
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
def _exact(expected):
    expression="(()=>{{const expected="+json.dumps(expected)+";"+{exact_js!r}+"}})()"
    return _bound_js(tab['targetId'], fresh, expression)
def _prepare(prefix):
    _fresh()
    if not _bound_js(tab['targetId'], fresh, {committed_probe!r}):
        _binding_error('WRONG_MODEL', tab=tab['targetId'])
    # Focus alone does not prove a caret position. Restore and verify the exact
    # logical end before EVERY chunk, while refusing any changed prefix.
    expression="(()=>{{const prefix="+json.dumps(prefix)+";"+{prepare_js!r}+"}})()"
    if not _bound_js(tab['targetId'], fresh, expression):
        _binding_error('INSERT_PREFIX_OR_CARET', pos=len(prefix), tab=tab['targetId'])
if not _exact(''):
    _binding_error('composer_not_empty', tab=tab['targetId'])
TEXT={text!r}
pos=0
while pos<len(TEXT):
    chunk=TEXT[pos:pos+CHUNK]
    _prepare(TEXT[:pos])
    _input("Input.insertText", text=chunk)
    time.sleep(0.25)
    if not _exact(TEXT[:pos+len(chunk)]):
        _binding_error('INSERT_MISMATCH', pos=pos, tab=tab['targetId'])
    pos+=len(chunk)
time.sleep(2)
if not _exact(TEXT):
    _binding_error('INSERT_MISMATCH', pos=pos, tab=tab['targetId'])
got=len(TEXT)
if {dry!r}:
    _input("Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA", modifiers=2, windowsVirtualKeyCode=65)
    _input("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", modifiers=2, windowsVirtualKeyCode=65)
    _input("Input.dispatchKeyEvent", type="keyDown", key="Backspace", code="Backspace", windowsVirtualKeyCode=8)
    _input("Input.dispatchKeyEvent", type="keyUp", key="Backspace", code="Backspace", windowsVirtualKeyCode=8)
    time.sleep(1); left=_len(); close_tab(tab["targetId"])
    print("RELAY_JSON:"+json.dumps({{"dry_run":True,"inserted":got,"left_after_clear":left}})); raise SystemExit(0)
_fresh()
_sent='UNKNOWN'
if RETAINED:
    # _paced_send can wait after _fresh; no Python/CDP round-trip may separate
    # the final document/message/readiness/model guard from the DOM click.
    ok=_paced_send(lambda: _bound_js(tab['targetId'], fresh, {retained_click!r}))
    if not ok['clicked']:
        _binding_error('retained_send_guard_failed', guard=ok['reason'], tab=tab['targetId'])
else:
    ok=_paced_send(lambda: _bound_js(tab["targetId"], fresh, {fresh_click!r}))
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


# Activity/commentary uses the same markdown style as final output. Only a
# positively complete latest turn and its typed final assistant unit supply bytes.
CURRENT_REPLY_JS = r'''function currentFinalReply(start,end,renderer='turn_markdown'){
    const main=document.querySelector('main');
    const turns=main?[...main.querySelectorAll('[data-turn-key]')]:[];
    const states=main?[...main.querySelectorAll('[data-talvt-turn-state]')]:[];
    if(!turns.length&&!states.length)return {present:false};
    const refuse=reason=>({present:true,complete:false,reason:reason,n:0});
    if(main.querySelector('button[data-testid="stop-button"], button[aria-label="Stop generating"]'))
        return refuse('generation_active');
    if(!turns.length)return refuse('turn_missing');
    const turn=turns[turns.length-1];
    const owned=[...turn.querySelectorAll('[data-talvt-turn-state]')]
        .filter(e=>e.closest('[data-turn-key]')===turn);
    if(owned.length!==1||owned[0].getAttribute('data-talvt-turn-state')!=='complete')
        return refuse('turn_incomplete');
    const units=[...owned[0].querySelectorAll('[data-content-search-unit-key$=":assistant"]')];
    if(units.length!==1||!units[0].querySelector('[data-conversation-role="assistant"]'))
        return refuse('final_assistant_unverified');
    const nodes=[...units[0].querySelectorAll('[data-markdown-text-style="assistant-message"]')]
        .filter(e=>!e.closest('[class*="group/activity-header"]')&&
                   e.closest('[data-content-search-unit-key]')===units[0]);
    if(nodes.length!==1||!nodes[0].innerText)return refuse('final_markdown_unverified');
    let node=nodes[0];
    if(renderer==='role')node=node.closest('[data-message-author-role=assistant]');
    if(renderer==='selection_markdown'&&!node.closest('[data-chatgpt-selection-message-id]'))
        return refuse('saved_renderer_unavailable');
    if(!node)return refuse('saved_renderer_unavailable');
    if(start!==undefined)return node.innerText.slice(start,end);
    return {present:true,complete:true,n:node.innerText.length,
        turn_key:turn.getAttribute('data-turn-key'),
        unit_key:units[0].getAttribute('data-content-search-unit-key')};
}'''


def read_reply(conversation_url: str, tab: str | None = None, reload: bool = False,
               protected: list[str] | tuple[str, ...] = (), existing_only: bool = False,
               expected_identity: str | None = None, capture_mode: str | None = None) -> dict:
    """Read the last assistant message; nonterminal callers must use existing_only.

    Reopening/reloading is reserved for independently confirmed terminal fetches.
    Missing Stop/assistant nodes or elapsed time do not prove completion.
    """
    if existing_only and (reload or not tab or not _conversation(conversation_url)):
        raise ValueError('existing reply requires an exact tab and canonical conversation without reload')
    if capture_mode not in (None, 'role', 'selection_markdown', 'turn_markdown'):
        raise ValueError('unknown reply capture mode')
    turn_probe = '(()=>{' + CURRENT_REPLY_JS + 'return currentFinalReply();})()'
    turn_slice = '(()=>{' + CURRENT_REPLY_JS + 'return currentFinalReply(%d,%d,%s);})()'
    turn_render = '(()=>{' + CURRENT_REPLY_JS + 'return currentFinalReply(undefined,undefined,%s);})()'
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
# Chrome virtualizes background transcripts. Activate only the already proved
# exact target; never navigate or reload a nonterminal chat to obtain text.
activate_tab(tab)
time.sleep(1)
probe=lambda expression: _bound_js(tab, want, expression)
st=_state(probe)
turn=probe({turn_probe!r})
def _incomplete(value):
    return value.get('present') and value.get('complete') is not True
if RELOAD and not st["stop"] and not st["thinking_failed"] and not _incomplete(turn):
    _page_load_slot()
    # Recheck both generation representations after the shared pacing wait.
    if not (lambda state: state["stop"] or state["thinking_failed"])(_state(probe)) and not _incomplete(probe({turn_probe!r})):
        probe("location.href="+json.dumps(want))
        wait_for_load(); time.sleep(8)
    st=_state(probe)
turn=probe({turn_probe!r})
if _incomplete(turn):
    st['stop']=True
    st['reply_incomplete_reason']=turn.get('reason')
st["conversation_url_now"]=probe("location.href")
if {expected_identity is not None!r}:
    identity={expected_identity!r}
    st["user_identity_matched"]=probe("(()=>{{const m=document.querySelector('main');return !!m&&[...m.querySelectorAll('[data-message-author-role=user]')].some(e=>e.innerText.includes("+json.dumps(identity)+"));}})()")
mode=CAPTURE_MODE or 'role'
n=0
if turn.get('present'):
    mode=CAPTURE_MODE or 'turn_markdown'
    rendered=probe({turn_render!r} % json.dumps(mode))
    if rendered.get('complete') is True:
        n=rendered['n']
    else:
        st['stop']=True
        st['reply_incomplete_reason']=rendered.get('reason')
elif not _incomplete(turn):
    if mode not in ('selection_markdown','turn_markdown'):
        n=probe("""(()=>{{const m=[...document.querySelectorAll('[data-message-author-role=assistant]')];return m.length?m[m.length-1].innerText.length:0;}})()""")
    if not n and CAPTURE_MODE not in ('role','turn_markdown'):
        # Preserve the exact saved renderer; no approximate equivalence.
        n=probe("""(()=>{{const m=[...document.querySelectorAll('[data-chatgpt-selection-message-id] [data-markdown-text-style]')];return m.length?m[m.length-1].innerText.length:0;}})()""") or 0
        if n: mode='selection_markdown'
parts=[]
for i in range(0, n, {IPC_CHUNK}):
    if turn.get('present'):
        part=probe({turn_slice!r} % (i,i+{IPC_CHUNK},json.dumps(mode)))
        if not isinstance(part,str):_binding_error('reply_became_incomplete',tab=tab)
        parts.append(part)
    elif mode=='selection_markdown':
        parts.append(probe("(()=>{{const m=[...document.querySelectorAll('[data-chatgpt-selection-message-id] [data-markdown-text-style]')];return m[m.length-1].innerText.slice(%d,%d);}})()" % (i, i+{IPC_CHUNK})))
    else:
        parts.append(probe("(()=>{{const m=[...document.querySelectorAll('[data-message-author-role=assistant]')];return m[m.length-1].innerText.slice(%d,%d);}})()" % (i, i+{IPC_CHUNK})))
text="".join(parts)
if turn.get('present') and probe({turn_probe!r})!=turn:
    _binding_error('reply_turn_changed',tab=tab)
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
        # A proof-attributed target may be absent from the original error. Protect
        # it conservatively even if later evidence drift invalidates admission.
        protected = sorted({tab for row in live for tab in custody_tabs(row)})
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


def read_terminal_reply(st: dict) -> dict:
    """Hydration is not content drift. Wait boundedly for the saved exact hash.

    No renderer substitution, rehash, navigation or reload. All observations keep
    exact conversation custody; any resumed generation returns to the final gate.
    """
    if st.get('status') != 'done':
        raise ValueError('terminal reply requires done state')
    tab = st.get('tab')
    url = binding_url(st)
    if url is None:
        raise ValueError('terminal reply requires exact conversation binding')
    reply: dict = {}
    for attempt in range(11):
        reply = read_reply(url, tab, protected=protected_tabs(st),
                           capture_mode=st.get('reply_render_mode'))
        require_binding(st, reply)
        tab = reply['tab']
        if (reply['state']['stop'] or
                hashlib.sha256(reply['text'].encode()).hexdigest() == st.get('last_text_sha')):
            return reply
        if attempt < 10:
            time.sleep(2)
    return reply  # unchanged final-hash gate must still reject a genuinely changed reply


def _cmd_fetch_outbox(a) -> int:
    """Folder names locate data; only a final-chat-bound manifest authorizes it."""
    cfg = load_config(); st = job_state(a.job)
    require_binding(st)
    if not st.get('conversation_url'):
        raise SystemExit('BINDING_PENDING poll before fetch-outbox')
    if st.get('status') == 'done':
        cleanup_tabs()
    if st.get('status') == 'done':
        r = read_terminal_reply(st)
    else:
        r = read_reply(binding_url(st), st.get('tab'), protected=protected_tabs(st),
                       existing_only=True, capture_mode=st.get('reply_render_mode'))
    require_binding(st, r)  # no Drive call or destination writes before exact session verification
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
        download_dir = getattr(a, 'download_dir', None)
        if download_dir is not None:
            need(marker, 'missing final-chat manifest receipt')
            directory = Path(download_dir)
            need(not any(p.is_symlink() for p in (directory, *directory.parents)), 'download symlink')
            need(directory.is_dir(), 'missing download directory')
            by_name = {p.name: p for p in directory.iterdir()}
            need(all(p.is_file() and not p.is_symlink() for p in by_name.values()), 'download non-file')
            names = list(by_name)
            read_artifact = lambda name: by_name[name].read_bytes()
        else:
            svc = drive()
            folders = drive_list_all(svc, f"'{cfg['drive']['outbox_id']}' in parents and name = '{a.job}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
            if not folders:
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
            by_name = {f['name']: f for f in files}
            def read_artifact(name):
                need(not by_name[name]['mimeType'].startswith('application/vnd.google-apps'), 'native document')
                return svc.files().get_media(fileId=by_name[name]['id']).execute()
        need(len(names) == len(set(names)), 'duplicate filenames')
        need(all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', n) for n in names), 'unsafe filename')
        need('OUTPUT-MANIFEST.json' in by_name, 'missing manifest')
        raw = read_artifact('OUTPUT-MANIFEST.json')
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
            data = read_artifact(name)
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
            'manifest_sha256': marker[2], 'verified_at': now_iso(),
            'artifact_source': 'native_downloads' if download_dir is not None else 'drive'})
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


def _retained_empty_witness(tab, time_origin, *, disposition_only=False):
    """Read the existing document only; never activate, navigate or modify its page.

    Terminal disposition is NOT send readiness: an owned, never-inserted job
    can be retired on the captured empty Codex root. Keep the sender's reader
    unchanged; only the disposition caller recognizes this exact placeholder.
    Complete custody/ledger proofs are checked before this passive observation.
    """
    reader = COMPOSER_TEXT_JS
    if disposition_only:
        reader = reader.replace("p.getAttribute('data-placeholder')==='Ask ChatGPT'",
            "['Ask ChatGPT','Work with ChatGPT'].includes(p.getAttribute('data-placeholder'))")
    extra = (r'''(()=>{const e=document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]');''' + reader + r'''return {url:location.href,time_origin:performance.timeOrigin,composer_empty:composerText(e)==='',user_count:document.querySelectorAll('[data-message-author-role=user]').length,transcript_count:document.querySelectorAll('[data-message-author-role], [data-chatgpt-selection-message-id]').length,stop:!!document.querySelector('button[data-testid="stop-button"],button[aria-label="Stop generating"]')};})()''')
    witness = bu_json(BU_PRELUDE + f'\ntab={tab!r}\nextra={extra!r}\n' + '''
before=[t['targetId'] for t in list_tabs()]
if tab not in before: _binding_error('retained_target_missing', tab=tab)
_select(tab)
want='https://chatgpt.com/'
probe=lambda expression: _bound_js(tab,want,expression)
first=probe(extra)
state=_state(probe)
last=probe(extra)
after=[t['targetId'] for t in list_tabs()]
print('RELAY_JSON:'+json.dumps({'tab':tab,'state':state,'first':first,'last':last,'same_tabs':sorted(before)==sorted(after)}))
''')
    current = witness.get('state', {})
    first, last = witness.get('first', {}), witness.get('last', {})
    return (not witness.get('error') and witness.get('tab') == tab and
            witness.get('same_tabs') is True and
            current.get('url') == 'https://chatgpt.com/' and current.get('ready_state') == 'complete' and
            current.get('composer_present') is True and current.get('assistant_count') == 0 and
            current.get('last_len') == 0 and all(current.get(k) is False for k in
                ('security_interstitial', 'human_challenge', 'logged_out', 'rate_limited',
                 'stop', 'drive_reconnect', 'thinking_failed')) and
            first == last and first.get('url') == 'https://chatgpt.com/' and
            first.get('time_origin') == time_origin and
            first.get('composer_empty') is True and first.get('user_count') == 0 and
            first.get('transcript_count') == 0 and first.get('stop') is False)


def _reconcile_unsent_readiness(job, cfg, st, path, preimage, report_path, apply, account, inspect_only=False):
    """Reconcile only a proven pre-insertion SECURITY_INTERSTITIAL failure.

    Called by the registered model reconciler under account + global locks.
    Requires normal heavy admission (CANDIDATE_BUILD_SEAT), explicit hash-bound
    report evidence, and the same existing empty idle document. No navigation,
    cleanup, insertion, or model selection. This does NOT grant replay permission:
    retry_blocked stays true, and the original reason/error/packet are retained.
    """
    prefix = 'READINESS_RECONCILE_REFUSED_'
    def refuse(reason):
        raise SystemExit(prefix + reason + ' ' + job)
    error = st.get('error')
    observed = error.get('state') if isinstance(error, dict) else None
    if (st.get('job') != job or st.get('account') != account or st.get('status') != 'failed' or
            st.get('reason') != 'SECURITY_INTERSTITIAL' or st.get('retry_blocked') is not True or
            st.get('typed') is not False or st.get('sent') is not False or
            any(st.get(k) for k in ('tab', 'conversation_url', 'pending_conversation_url',
                                  'conversation_id', 'binding_version')) or
            not isinstance(error, dict) or error.get('error') != 'SECURITY_INTERSTITIAL' or
            error.get('typed') is not False or error.get('sent') is not False or
            not isinstance(error.get('tab'), str) or not error['tab'] or
            not isinstance(observed, dict) or observed.get('url') != 'https://chatgpt.com/' or
            observed.get('security_interstitial') is not True or
            observed.get('composer_present') is not False or observed.get('model') is not None or
            observed.get('assistant_count') != 0 or observed.get('last_len') != 0 or
            any(observed.get(k) is not False for k in ('human_challenge', 'logged_out',
                'rate_limited', 'stop', 'drive_reconnect', 'thinking_failed')) or
            any((path.parent / name).exists() for name in ('RESPONSE.md', 'outbox')) or
            not all(st.get(k) for k in PROVENANCE_FIELDS) or
            not re.fullmatch(r'[0-9a-f]{64}', st.get('input_sha256') or '') or
            not re.fullmatch(r'[0-9a-f]{64}', st.get('reply_token') or '')):
        refuse('STATE')
    message_path = path.parent / 'MESSAGE.md'
    if not message_path.is_file():
        refuse('MESSAGE')
    message = message_path.read_bytes()
    if _failed_generation_identity(st).encode() not in message:
        refuse('MESSAGE')
    # Strict decoding here: the legacy permissive ledger reader must not hide
    # a malformed row that could contain a binding or interrupted reconciliation.
    ledger = ledger_path(cfg)
    ledger_preimage = ledger.read_bytes()
    rows = []
    try:
        all_rows = [json.loads(line) for line in ledger_preimage.splitlines() if line.strip()]
        rows = [row for row in all_rows if row.get('job') == job]
    except (ValueError, AttributeError):
        refuse('LEDGER')
    if (len(rows) != 1 or rows[0].get('event') != 'submitted' or rows[0].get('phase') is not None or
            rows[0].get('account') != account or rows[0].get('kind') != 'initial' or
            rows[0].get('certainty') != 'reserved_may_send' or
            any(rows[0].get(k) != st[k] for k in PROVENANCE_FIELDS)):
        refuse('LEDGER')
    digest = hashlib.sha256(preimage).hexdigest()
    report_file = BUSINESS / report_path
    report_preimage = report_file.read_bytes()
    report = json.loads(report_preimage)
    matching = [row for row in report.get('jobs', []) if row.get('job', row.get('job_id')) == job]
    if len(matching) != 1:
        refuse('REPORT')
    row = matching[0]
    if (row.get('job_id', job) != job or row.get('reason') != st['reason'] or
            any(row.get(k) is not False for k in ('submission_bound', 'typed', 'sent')) or
            row.get('job_sha256') != digest or row.get('error_tab') != error['tab'] or
            row.get('message_sha256') != hashlib.sha256(message).hexdigest() or
            row.get('ledger_rows_sha256') != hashlib.sha256(json.dumps(rows,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest() or
            type(row.get('time_origin')) not in (int, float) or not row['time_origin'] > 0):
        refuse('REPORT')
    if job in _legacy_live_jobs():
        refuse('LIVE_RUNNER')
    # Require, never acquire/reclaim, the caller's ordinary host-heavy admission.
    admission = subprocess.run([sys.executable, str(BUSINESS / 'scripts/candidate_build_lock.py'),
                                'require-held'], capture_output=True, text=True)
    if admission.returncode:
        refuse('ADMISSION')
    tab = error['tab']
    if not _retained_empty_witness(tab, row['time_origin']):
        refuse('TAB_EVIDENCE')
    # Recheck all local custody after the browser read, including default dry-run.
    if (path.read_bytes() != preimage or message_path.read_bytes() != message or
            ledger.read_bytes() != ledger_preimage or report_file.read_bytes() != report_preimage):
        refuse('STATE_DRIFT')
    if job in _legacy_live_jobs():
        refuse('LIVE_RUNNER')
    if inspect_only:
        if apply:
            refuse('MODE')
        return {'tab': tab, 'time_origin': row['time_origin'], 'job_sha256': digest,
                'message_sha256': row['message_sha256'], 'report_sha256': hashlib.sha256(report_preimage).hexdigest(),
                'ledger_sha256': hashlib.sha256(ledger_preimage).hexdigest()}
    if not apply:
        print('WOULD_RECONCILE_UNSENT_READINESS job=' + job + ' tab=' + tab + ' preimage_sha256=' + digest)
        return 0
    backup = path.with_name('JOB.pre-readiness-reconcile-' + digest + '.json')
    try:
        with backup.open('xb') as fh:
            fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
    except FileExistsError:
        if backup.read_bytes() != preimage:
            refuse('BACKUP_CONFLICT')
    if (path.read_bytes() != preimage or message_path.read_bytes() != message or
            ledger.read_bytes() != ledger_preimage or report_file.read_bytes() != report_preimage):
        refuse('STATE_DRIFT')
    event = {'event': 'readiness_unsent_reconcile', 'job': job, 'account': account,
             'preimage_sha256': digest, 'reason': st['reason'], 'retained_tab': tab,
             'message_sha256': row['message_sha256'], 'time_origin': row['time_origin']}
    ledger_append(cfg, {**event, 'phase': 'STARTED'})
    job_write(job, {**st, 'status': 'unsent_reconciled', 'retry_blocked': True,
                    'reconciled_at': now_iso(), 'preimage_sha256': digest})
    ledger_append(cfg, {**event, 'phase': 'APPLIED'})
    # Deliberately no cleanup_tabs(): the retained target is not a disposable tab.
    print('RECONCILED_UNSENT_READINESS job=' + job + ' tab=' + tab + ' preimage_sha256=' + digest)
    return 0


# Fixed source incident, not an admission rule for other uncertain failures.
CONSUMED_CANARY = {
    'job': 'pro-source-bundle-six-pr56-pr57-pr58-site17-pr59-pr60-20260927',
    'held': '88041b25bf3cd6f2388c1b84aaa3f4f23ea659a9237ca3e58bed7d4878fc15c6',
    'backup': 'ef0ddbcb8e8921544cae919ff6ddbe74d33a151ec92adf570eab0a694a69d7e4',
    'message': '0e84873bcd5fe7e4ad5eac8efd7366fe21481cdc23b983c8aa0d3b8fa9a71ea0',
    'manifest': '739af62e396b50637f721ffc9a8da25067a633b985d8ef955f963542d6efe968',
    'drive_prompt': '4f83a600306fa493295c7206378d7e471b9fabfa3f442cbcdb2222f264c85707',
    'ledger_lines': ('456e7ab3712416432cb049a2b202b049b1a37e21fbe429887a42bef9e0262935',
                     '948358a0bb196ed6a44d861e54ecd8f1f6a13dad6cb46d40c43a8fa373de11af'),
    'witness_sha256': '36033bb2572dfddd7c8f5358f89c6ca581ce16cd5e01755de1000e29d7ad02d0',
    'tab': '096B6BD311B590EA7895CBD6E103BA3C',
    'time_origin': 1790498286675.7,
}


def _prove_consumed_canary(job, cfg, st, path, preimage, report_path, account):
    """Exact failed first-evaluation canary only; source barrier is not a live-page claim."""
    c = CONSUMED_CANARY
    def refuse(reason):
        raise SystemExit('CANARY_RECOVERY_REFUSED_' + reason + ' ' + job)
    digest = hashlib.sha256(preimage).hexdigest()
    original_path = path.with_name('JOB.pre-retained-resume-' + c['backup'] + '.json')
    message_path = path.with_name('MESSAGE.md')
    manifest_path = path.with_name('MANIFEST.json')
    drive_prompt_path = path.parent / 'drive-input/PROMPT.md'
    if (job != c['job'] or digest != c['held'] or st.get('job') != job or
            st.get('account') != account or st.get('status') != 'failed' or
            st.get('reason') != 'RETAINED_RESUME_UNCERTAIN' or st.get('retry_blocked') is not True or
            st.get('typed') is not False or st.get('sent') is not False or
            any(st.get(k) for k in ('tab', 'conversation_url', 'pending_conversation_url',
                                     'conversation_id', 'binding_version')) or
            st.get('resume_error') != {'error': 'page_evaluation_failed', 'typed': False,
                                       'sent': False, 'tab': c['tab']} or
            any((path.parent / name).exists() for name in ('RESPONSE.md', 'outbox')) or
            not all(st.get(k) for k in PROVENANCE_FIELDS) or
            not re.fullmatch(r'[0-9a-f]{64}', st.get('input_sha256') or '') or
            not re.fullmatch(r'[0-9a-f]{64}', st.get('reply_token') or '')):
        refuse('STATE')
    try:
        original_bytes = original_path.read_bytes()
        message = message_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        drive_prompt = drive_prompt_path.read_bytes()
        original = json.loads(original_bytes)
        manifest = json.loads(manifest_bytes)
    except (OSError, ValueError):
        refuse('PACKET')
    attempt = st.get('resume_attempt')
    if (hashlib.sha256(original_bytes).hexdigest() != c['backup'] or
            hashlib.sha256(message).hexdigest() != c['message'] or
            hashlib.sha256(manifest_bytes).hexdigest() != c['manifest'] or
            hashlib.sha256(drive_prompt).hexdigest() != c['drive_prompt'] or
            not isinstance(attempt, dict) or
            attempt != {'job': job, 'account': account, 'preimage_sha256': c['backup'],
                        'message_sha256': c['message'], 'report_sha256': attempt.get('report_sha256'),
                        'retained_tab': c['tab'], 'attempt_at': attempt.get('attempt_at')} or
            not re.fullmatch(r'[0-9a-f]{64}', attempt['report_sha256'] or '') or
            not isinstance(attempt['attempt_at'], str) or
            original.get('status') != 'failed' or original.get('reason') != 'SECURITY_INTERSTITIAL' or
            original.get('retry_blocked') is not True or original.get('typed') is not False or
            original.get('sent') is not False or original.get('account') != account or
            original.get('error', {}).get('tab') != c['tab'] or
            original.get('error', {}).get('error') != 'SECURITY_INTERSTITIAL' or
            {k: v for k, v in st.items() if k not in ('status', 'reason', 'resume_attempt', 'resume_error')}
            != {k: v for k, v in original.items() if k != 'status' and k != 'reason'} or
            _failed_generation_identity(st).encode() not in message or
            any(st.get(k) != original.get(k) for k in PROVENANCE_FIELDS) or
            any(manifest.get(k) != st.get(k) for k in (*PROVENANCE_FIELDS, 'job', 'job_class'))):
        refuse('CUSTODY')
    # Recompute the registered input identity from the original packet in its
    # original order; a matching manifest alone does not prove attachment bytes.
    paths = [path.parent / 'PROMPT.md', *manifest.get('attach', []), *manifest.get('drive_attach', [])]
    try:
        if (not isinstance(manifest.get('attach'), list) or
                not isinstance(manifest.get('drive_attach'), list) or
                any(not isinstance(p, (str, Path)) or not Path(p).resolve().is_relative_to(path.parent.resolve())
                    for p in paths)):
            refuse('PACKET')
        packet = [(Path(p).name, hashlib.sha256(Path(p).read_bytes()).hexdigest()) for p in paths]
    except OSError:
        refuse('PACKET')
    if hashlib.sha256(json.dumps(packet, separators=(',', ':')).encode()).hexdigest() != st['input_sha256']:
        refuse('PACKET')
    ledger = ledger_path(cfg)
    ledger_preimage = ledger.read_bytes()
    try:
        lines = [line for line in ledger_preimage.splitlines() if line.strip() and
                 json.loads(line).get('job') == job]
        rows = [json.loads(line) for line in lines]
        # Parse every line, even unrelated rows: never overlook a malformed binding.
        [json.loads(line) for line in ledger_preimage.splitlines() if line.strip()]
    except (ValueError, AttributeError):
        refuse('LEDGER')
    if (tuple(hashlib.sha256(line).hexdigest() for line in lines) != c['ledger_lines'] or
            [(r.get('event'), r.get('phase')) for r in rows] !=
            [('submitted', None), ('retained_resume', 'STARTED')] or
            rows[0].get('account') != account or rows[0].get('kind') != 'initial' or
            rows[0].get('certainty') != 'reserved_may_send' or
            any(rows[0].get(k) != st[k] for k in PROVENANCE_FIELDS) or
            any(rows[1].get(k) != attempt.get(k) for k in attempt)):
        refuse('LEDGER')
    report_file = BUSINESS / report_path
    report_preimage = report_file.read_bytes()
    if hashlib.sha256(report_preimage).hexdigest() != c['witness_sha256']:
        refuse('WITNESS_CUSTODY')
    report = json.loads(report_preimage)
    custody = report['custody_after']
    if (report.get('schema') != 'pro-consumed-canary-existing-target-witness-v1' or
            report.get('status') != 'EXACT_TARGET_PASSIVE_WITNESS_HELD_NO_RECOVERY' or
            report.get('job') != job or report.get('target') != c['tab'] or
            report['witness']['first'] != report['witness']['last'] or
            report['witness']['first']['time_origin'] != c['time_origin'] or
            custody['files']['JOB.json']['sha256'] != digest or
            custody['files']['MESSAGE.md']['sha256'] != c['message'] or
            custody['files'][original_path.name]['sha256'] != c['backup'] or
            [r['sha256'] for r in custody['ledger_rows']] != list(c['ledger_lines']) or
            custody['ordered_input_sha256'] != st['input_sha256']):
        refuse('WITNESS_EVIDENCE')
    if job in _legacy_live_jobs():
        refuse('LIVE_RUNNER')
    admission = subprocess.run([sys.executable, str(BUSINESS / 'scripts/candidate_build_lock.py'),
                                'require-held'], capture_output=True, text=True)
    if admission.returncode:
        refuse('ADMISSION')
    if not _retained_empty_witness(c['tab'], c['time_origin']):
        refuse('TAB_EVIDENCE')
    def recheck():
        if (path.read_bytes() != preimage or original_path.read_bytes() != original_bytes or
                message_path.read_bytes() != message or manifest_path.read_bytes() != manifest_bytes or
                drive_prompt_path.read_bytes() != drive_prompt or
                any(hashlib.sha256(Path(p).read_bytes()).hexdigest() != item[1]
                    for p, item in zip(paths, packet)) or
                ledger.read_bytes() != ledger_preimage or report_file.read_bytes() != report_preimage or
                job in _legacy_live_jobs()):
            refuse('DRIFT')
    recheck()
    return {'tab': c['tab'], 'time_origin': c['time_origin'], 'job_sha256': digest,
            'message_sha256': c['message'], 'report_sha256': hashlib.sha256(report_preimage).hexdigest(),
            'ledger_sha256': hashlib.sha256(ledger_preimage).hexdigest(),
            'recheck': recheck, 'original_attempt': attempt}


def resume_unsent_readiness(job: str, report: str, authorize_preimage: str | None = None) -> int:
    """One-shot original-message send on the exact proven-unsent retained document.

    No reconciliation apply, reupload, cleanup or second reservation. An absent
    authority hash is read-only. An interrupted attempt remains retry-blocked.
    """
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', job):
        raise SystemExit('RESUME_REFUSED_JOB')
    resolved = (BUSINESS / report).resolve()
    if Path(report).is_absolute() or not resolved.is_relative_to(BUSINESS.resolve()) or not resolved.is_file():
        raise SystemExit('RESUME_REFUSED_REPORT_PATH')
    cfg = load_config()
    path = JOBS / job / 'JOB.json'
    if not path.is_file():
        raise SystemExit('RESUME_REFUSED_MISSING')
    initial = json.loads(path.read_bytes())
    acct = account_of(cfg, initial.get('account'))
    use_account(acct)
    with account_lock(), global_lock():
        preimage = path.read_bytes()
        st = json.loads(preimage)
        if st.get('job') != job or st.get('account') != acct['label']:
            raise SystemExit('RESUME_REFUSED_IDENTITY')
        recovery = job == CONSUMED_CANARY['job'] and st.get('reason') == 'RETAINED_RESUME_UNCERTAIN'
        proof = (_prove_consumed_canary(job, cfg, st, path, preimage, report, acct['label'])
                 if recovery else
                 _reconcile_unsent_readiness(job, cfg, st, path, preimage, report, False,
                                             acct['label'], inspect_only=True))
        if _legacy_live_jobs():
            raise SystemExit('RESUME_REFUSED_LIVE_RUNNER')
        if any(s.get('tab') == proof['tab'] or
               (s.get('status') in ACTIVE_STATES and not binding_url(s))
               for s in sibling_bindings(acct['label'], job)):
            raise SystemExit('RESUME_REFUSED_SIBLING')
        active = active_registry()
        envelope = cfg['envelope']
        if (len(active) > min(6, envelope['max_concurrent_chats_per_ip']) or
                sum(r['account'] == acct['label'] for r in active) >
                min(2, envelope['max_concurrent_chats_per_account'])):
            raise SystemExit('RESUME_REFUSED_CAPACITY')
        used = messages_used_7d(cfg, acct['label'])
        limit = int(acct['weekly_messages'] * (1 - cfg.get('budget_reserve_pct', 10) / 100))
        if used > limit:
            raise SystemExit('RESUME_REFUSED_BUDGET')
        message_path = path.with_name('MESSAGE.md')
        ledger = ledger_path(cfg)
        message_bytes = message_path.read_bytes()
        if (path.read_bytes() != preimage or
                hashlib.sha256(message_bytes).hexdigest() != proof['message_sha256'] or
                hashlib.sha256(ledger.read_bytes()).hexdigest() != proof['ledger_sha256'] or
                hashlib.sha256(resolved.read_bytes()).hexdigest() != proof['report_sha256']):
            raise SystemExit('RESUME_REFUSED_DRIFT')
        if authorize_preimage is None:
            print('WOULD_RESUME_RETAINED job=' + job + ' tab=' + proof['tab'] +
                  ' preimage_sha256=' + proof['job_sha256'] + '; retry_blocked retained')
            return 0
        if authorize_preimage != proof['job_sha256']:
            raise SystemExit('RESUME_REFUSED_AUTHORITY')
        if recovery:
            proof['recheck']()
        backup = path.with_name(('JOB.pre-canary-recovery-' if recovery else
                                 'JOB.pre-retained-resume-') + proof['job_sha256'] + '.json')
        try:
            with backup.open('xb') as fh:
                fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
        except FileExistsError:
            raise SystemExit('RESUME_REFUSED_ALREADY_ATTEMPTED')
        attempt = now_iso()
        meta = {'job': job, 'account': acct['label'], 'preimage_sha256': proof['job_sha256'],
                'message_sha256': proof['message_sha256'], 'report_sha256': proof['report_sha256'],
                'retained_tab': proof['tab'], 'attempt_at': attempt}
        if recovery:
            meta['original_preimage_sha256'] = CONSUMED_CANARY['backup']
        ledger_append(cfg, {'event': 'canary_forward_recovery' if recovery else 'retained_resume',
                            'phase': 'STARTED', **meta})
        reservation = {**st, 'status': 'resume_submitting', 'retry_blocked': True,
                       'typed': 'UNKNOWN', 'sent': 'UNKNOWN',
                       'resume_attempt': meta,
                       **({'resume_history': [proof['original_attempt']]} if recovery else {})}
        job_write(job, reservation)
        # Keep both locks until exact binding is committed. No sibling admission
        # can steal the tab between the last proof and the one-shot send.
        result = send_message(message_bytes.decode('utf-8'), retained_tab=proof['tab'],
                              time_origin=proof['time_origin'],
                              forbidden=[url for s in sibling_bindings(acct['label'], job)
                                         if (url := binding_url(s))])
        if result.get('error'):
            job_write(job, {**reservation, 'status': 'failed', 'reason': 'RETAINED_RESUME_UNCERTAIN',
                            'resume_error': result, 'typed': result.get('typed', 'UNKNOWN'),
                            'sent': result.get('sent', 'UNKNOWN')})
            raise SystemExit('RESUME_HELD ' + str(result['error']))
        conv = binding_url(result)
        if (result.get('tab') != proof['tab'] or not conv or
                result.get('typed') is not True or result.get('sent') is not True):
            raise SystemExit('RESUME_HELD_BINDING_UNVERIFIED')
        bound = {**reservation, 'status': 'running', 'retry_blocked': False,
                 'typed': True, 'sent': True, 'binding_version': 2,
                 'conversation_id': conv.rsplit('/', 1)[1],
                 'tab': proof['tab'], 'submitted': now_iso(), 'chars_in': len(message_bytes.decode('utf-8'))}
        key = 'conversation_url' if result.get('conversation_url') else 'pending_conversation_url'
        bound[key] = conv
        require_binding(bound, result)
        job_write(job, bound)
        ledger_append(cfg, {'event': 'submission_bound', 'job': job, 'account': acct['label'],
                            'conversation_url': conv, 'chars_in': bound['chars_in'],
                            **{k: st[k] for k in PROVENANCE_FIELDS}})
        print('RESUMED_RETAINED job=' + job + ' conv=' + conv)
        return 0


# Obsolete PR52/44f52bba: source-proven initial _exact('') refusal BEFORE TEXT.
# These immutable incident bytes are NOT a generic cancellation or retry policy.
TERMINAL_UNSENT_PR52 = {
    'job': 'pro-glp318-pr52-44f52bba-integration-review-20260927',
    'held': '1547740d1c1093b628e40050db6f313a30db90ba89c02336940ccaffe5d86ac5',
    'backup': '349f843d584f11cc5a5e6f03a092b347aa0b3676b75051ccbe282de261146f17',
    'message': '2cc8c1c8d5b73218d3a1e0f08e038c5938e584c4d84d38de8879932f02978166',
    'manifest': 'cb5ac9ed06e1a5c67fd6cd0118735366466db183d461391f3346e62806f67618',
    'drive_prompt': '2aa146621777338096f0da54bdeed721d060242d986312709b8bee3b4c762a0d',
    'ledger_lines': ('0283006ad6de6df682e6fc57a9e932b3040a024b79a6133cd028d0379299bb96',
                     'f344dbec7d98913be323552a7aa6baf76a9313b57dbc91a91060bec983b07223'),
    'tab': 'D3DD42837C1937487F27C4C6870EF9D9',
    'time_origin': 1790488400308.6,
}


# Current source-proven pre-insert refusals only. No retained send was attempted.
# Acct2's consumed UNKNOWN job is deliberately absent; stale error flags cannot
# establish unsent custody. These pins do not broaden PR52/canary exceptions.
TERMINAL_UNSENT_CURRENT = {
    # Exact L4 pre-insert APP_NOT_READY refusal on a restored Codex root.
    # Preserve retry_blocked and error.tab; only NEW jobs gain admission.
    'route-glp-47-4edd8591b79590e9fdf5b3e5': {
        'job': 'route-glp-47-4edd8591b79590e9fdf5b3e5',
        'held': 'ff39c24573c0f21466d2d7ad27d11165028bf87894c204728e64e12dcad9384c',
        'status': 'failed', 'reason': 'APP_NOT_READY', 'backup': None,
        'message': 'eb9bbe2d25537562d5499b3af76f286afa198b40909d7591fdaaacb63053d503',
        'manifest': '8bd6bb3a064b43b0f3a4f29e78b986c2a8bea2dabcf250777c1e4afbddb78f20',
        'drive_prompt': 'a63f1d906c8618a05391d2ec7b5e3e4ec25c2d4bd94113a3b3350acf6b0bb2f8',
        'ledger_lines': ('0a13f638dc191e91de0ae5b051bfade8e50708ae6a317f32407bfb59b305feb5',),
        'tab': 'FA0A5B8FA98BC31CF4BE7B0A623DB702', 'time_origin': 1791583174138.6,
    },
    # Exact first-attempt refusal at _new_tab, before creation/typing; no retry.
    'route-glp-80-0165c8c734145a8044438815': {
        'job': 'route-glp-80-0165c8c734145a8044438815',
        'held': 'ca026a94c263e6f45054353f8a1e8b6e35e5e23598d6c841ae58cb1cd508af8f',
        'status': 'failed', 'reason': 'TAB_CAP', 'backup': None,
        'message': '7589e8921ddfaddcafeb70455662cdb81127ffeccfc9219ec348e85dbe6a7a5e',
        'manifest': '2c21a666b44ef1f713e068d16032024bb23984588104bf212bdf8ee38a7b21d3',
        'drive_prompt': '2abe3e55eb9ff67175ad183df94b25641eafbce260dad3bc3fca81419fc97838',
        'ledger_lines': ('06b6e63b2ecebb6a370dff8af6d227378a04564807d9927ccb33d38df80596c6',),
        'tab': None, 'time_origin': None, 'pretarget': True,
        'attempt_log': 'ops/gpt6pro-relay/route/logs/route-glp-80-0165c8c734145a8044438815-submit-1791412306196753224.log',
        'attempt_log_sha256': 'b03698bdca2fcf66eed6a716acf7dcfb3faf99b0aac9363d70fec1334164c0e9',
    },
    'glp44-canonical-sensor-partition-20261006': {
        'job': 'glp44-canonical-sensor-partition-20261006',
        'held': 'a9217c279914d3a72be56525e3b01215fc9424a5ebcf9dd7a190f247bf436575',
        'status': 'failed', 'reason': 'WRONG_MODEL', 'backup': None,
        'message': 'dd16dabaee15ffb8a58fce3edd81444fa133dac49bf6982ee39e2a6713655505',
        'manifest': 'cd6bb067f72fd10c839e9c24c812712e2d7b3d40fbbc0ecb284a5bdc6a877981',
        'drive_prompt': 'dfe4a416c40c891b4dcaba3d0da2d1d3106fce655271a929acd71a547c5e1ceb',
        'ledger_lines': ('b0625dd0a4ed821b0491f7163b666ff330c0263c6137ab41de9759a033701870',),
        'tab': None, 'time_origin': None,
    },
    'pro-glp96-intake-design-closer-20261003': {
        'job': 'pro-glp96-intake-design-closer-20261003',
        'held': '9a49105d13b15892836e64c42ade9c4376fc411dabac11813ad182c5913cbb8d',
        'status': 'unsent_reconciled', 'reason': 'SECURITY_INTERSTITIAL',
        'backup': '5b34fffc0ba2573074f8e45092e43e4af8b3ceef7b7d6b8c8c801c649da82b0e',
        'message': '24d8e13f21295e9cdbbd71e10bebd5c422b9965512f240714e36dd5b0f65a8a9',
        'manifest': 'c9d04fbf6bf1542bf3b02ba8925f054e90156b9fdd12674fea261a3d34a8b527',
        'drive_prompt': '39a2fe3edac5fd160ea93401abee616023e9d048fabba56351f3206a940a5589',
        'ledger_lines': ('606843ce0562eb04cd73c57ecfa70edab13759c98a57eb08bc6b4ea27a27cebf',
                         '93890a3862731339fb2bb3079f1c5720c7eee6fd1e5d39ad3c93683d8cdf95bc',
                         '55e34b66977981a3d2036e1a15002627f82c9438c2eb1fe8bc6df6147053f7da'),
        'tab': 'AE132746A45F57BA0367D39D3B0320EF', 'time_origin': 1790988433154.6,
    },
}


# One historical atomic composer refusal AFTER insertion but BEFORE its click.
# Original true/UNKNOWN flags and the nonmatching draft are never rewritten.
# These reviewed-source/custody pins do not cover generic errors or timeouts.
TERMINAL_GUARD_REFUSED = {
    **CONSUMED_CANARY,
    'held': '9063d40a522736092508f974e1d6f477cccdc5c708d4cb2ade73b9fc71da9c0e',
    'postinsert_guard': True,
    'draft_sha256': '40c661b5b3d7b6005f75974e9ada44ba6829679b354ccd0bf0ebbb58f9bbdc9a',
    'ledger_lines': (*CONSUMED_CANARY['ledger_lines'],
        '63a54f9bc6f132b314a153677e5065bc92efa67b24e5e4602a3792982a1e3b29'),
    'backups': {
        'JOB.pre-retained-resume-ef0ddbcb8e8921544cae919ff6ddbe74d33a151ec92adf570eab0a694a69d7e4.json':
            'ef0ddbcb8e8921544cae919ff6ddbe74d33a151ec92adf570eab0a694a69d7e4',
        'JOB.pre-canary-recovery-88041b25bf3cd6f2388c1b84aaa3f4f23ea659a9237ca3e58bed7d4878fc15c6.json':
            '88041b25bf3cd6f2388c1b84aaa3f4f23ea659a9237ca3e58bed7d4878fc15c6'},
    'proof_files': {
        'reports/ops/pro-retained-generated-js-review-20260927/INDEPENDENT-REVIEW.json':
            '438b50df6352d6cce90dd4aba70cf1881cd6bc563bc20c1adf244e99f5c343c3',
        'reports/ops/pro-consumed-canary-source-recovery-review-20260927/INDEPENDENT-REVIEW.json':
            '69eddab33757986db27362a5277b8701f7388e4dafd80a3c4db138666dab3af3',
        'reports/ops/pro-six-forward-recovery-execution-20260927/INDEPENDENT-REVIEW.json':
            'a602400cb61d11b82369e1d1bfce8be69546344aeae9dbbb25fca5757b5e483e',
        'reports/ops/pro-consumed-canary-existing-target-witness-20260927/RESULT.json':
            '36033bb2572dfddd7c8f5358f89c6ca581ce16cd5e01755de1000e29d7ad02d0',
        'pro-consumed-canary-source-recovery-review-worktree/scripts/gpt6pro_relay.py':
            'baf90d2d37a6b69cec56c5f6fc1c0c3b31ce6cf40eca42d9e59a0c089ff23569',
        'reports/ops/system-resume-20261006/persistent-three-pro-20261007/ACCT2-RAW-DRAFT.json':
            '9a4149f6291ce87c08bcaf1a4948a212143b1a0515ecea8b4449c0c037a441cd'},
}


def _guard_refusal_custody(cfg, st, preimage, report):
    """Exact historical source barriers + unchanged raw custody; never flag-only."""
    c = TERMINAL_GUARD_REFUSED
    sha = lambda b: hashlib.sha256(b).hexdigest()
    if (st.get('job') != c['job'] or sha(preimage) != c['held'] or json.loads(preimage) != st or
            st.get('status') != 'failed' or st.get('reason') != 'RETAINED_RESUME_UNCERTAIN' or
            st.get('typed') is not True or st.get('sent') != 'UNKNOWN' or st.get('retry_blocked') is not True or
            st.get('resume_error') != {'error': 'retained_send_guard_failed', 'guard': 'composer',
                'typed': True, 'sent': 'UNKNOWN', 'tab': c['tab']} or
            any(st.get(k) for k in ('tab', 'conversation_id', 'conversation_url', 'pending_conversation_url', 'binding_version'))):
        raise ValueError('STATE')
    d = JOBS / c['job']
    if any((d / name).exists() for name in ('RESPONSE.md', 'outbox')):
        raise ValueError('OUTPUT')
    pins = {'MESSAGE.md': c['message'], 'MANIFEST.json': c['manifest'],
            'drive-input/PROMPT.md': c['drive_prompt'], **c['backups']}
    custody = {}
    for root, items in ((d, pins), (BUSINESS, c['proof_files'])):
        for rel, digest in items.items():
            p = (root / rel).resolve()
            if Path(rel).is_absolute() or not p.is_relative_to(root.resolve()):
                raise ValueError('CUSTODY_PATH')
            custody[p] = p.read_bytes()
            if sha(custody[p]) != digest:
                raise ValueError('CUSTODY')
    manifest = json.loads(custody[(d / 'MANIFEST.json').resolve()])
    if (any(not st.get(k) or manifest.get(k) != st[k] for k in (*PROVENANCE_FIELDS, 'job', 'job_class')) or
            _failed_generation_identity(st).encode() not in custody[(d / 'MESSAGE.md').resolve()]):
        raise ValueError('IDENTITY')
    paths = [d / 'PROMPT.md', *map(Path, manifest['attach']), *map(Path, manifest['drive_attach'])]
    if any(not p.resolve().is_relative_to(d.resolve()) for p in paths):
        raise ValueError('PACKET_PATH')
    custody.update({p: p.read_bytes() for p in paths})
    ordered = [(p.name, sha(custody[p])) for p in paths]
    if sha(json.dumps(ordered, separators=(',', ':')).encode()) != st['input_sha256']:
        raise ValueError('PACKET')
    lp = ledger_path(cfg); custody[lp] = lp.read_bytes()
    all_rows = [(line, json.loads(line)) for line in custody[lp].splitlines() if line.strip()]
    lines = [line for line, row in all_rows if row.get('job') == c['job']]
    if tuple(map(sha, lines[:3])) != c['ledger_lines']:
        raise ValueError('LEDGER')
    rows = list(map(json.loads, lines))
    rp = (BUSINESS / report).resolve()
    if Path(report).is_absolute() or not rp.is_relative_to(BUSINESS.resolve()):
        raise ValueError('REPORT_PATH')
    custody[rp] = rp.read_bytes()
    matches = [r for r in json.loads(custody[rp]).get('jobs', []) if r.get('job') == c['job']]
    if len(matches) != 1:
        raise ValueError('REPORT')
    row = matches[0]
    expected = {'job': c['job'], 'disposition': 'terminal_unsent_postinsert_guard',
        'job_sha256': c['held'], 'typed': True, 'sent': 'UNKNOWN', 'submission_bound': False,
        'draft_sha256': c['draft_sha256']}
    if any(row.get(k) != v or type(row.get(k)) is not type(v) for k, v in expected.items()):
        raise ValueError('REPORT')
    n = row.get('ledger_preimage_bytes')
    if type(n) is not int or n <= 0 or sha(custody[lp][:n]) != row.get('ledger_preimage_sha256'):
        raise ValueError('LEDGER_PREIMAGE')
    if len(rows) == 3 and len(custody[lp]) != n:
        raise ValueError('LEDGER_PREIMAGE')
    return custody, rows


def _retained_draft_witness(tab, time_origin, digest):
    """Passive stable exact populated draft; never treat UNKNOWN as empty/ready."""
    expression = '(()=>{' + COMPOSER_TEXT_JS + r'''const e=document.querySelector('#prompt-textarea')||document.querySelector('[data-composer-markdown][contenteditable=true]');return {url:location.href,origin:performance.timeOrigin,logical:composerText(e),html:e?.outerHTML,innerText:e?.innerText,users:document.querySelectorAll('[data-message-author-role=user]').length,assistants:document.querySelectorAll('[data-message-author-role=assistant]').length};})()'''
    result = bu_json(BU_PRELUDE + f'''
tab={tab!r}
before=sorted(t['targetId'] for t in list_tabs())
if tab not in before: _binding_error('retained_target_missing')
_select(tab)
probe=lambda e:_bound_js(tab,'https://chatgpt.com/',e)
a=probe({expression!r}); state=_state(probe); b=probe({expression!r})
valid=(a==b and before==sorted(t['targetId'] for t in list_tabs()) and
 a['url']=='https://chatgpt.com/' and a['origin']=={time_origin!r} and
 isinstance(a['logical'],str) and len(a['logical'])==13323 and a['users']==a['assistants']==0 and
 __import__('hashlib').sha256(a['logical'].encode()).hexdigest()=={digest!r} and
 state['ready_state']=='complete' and state['composer_present'] is True and
 all(state[k] is False for k in ('logged_out','stop','drive_reconnect','rate_limited','security_interstitial','human_challenge','thinking_failed')))
print('RELAY_JSON:'+json.dumps({{'valid':valid}}))
''')
    return result == {'valid': True}


def _terminal_unsent_pin(job, report=None):
    if job == TERMINAL_GUARD_REFUSED['job']:
        return TERMINAL_GUARD_REFUSED
    if job == TERMINAL_UNSENT_PR52['job']:
        return TERMINAL_UNSENT_PR52
    if job in TERMINAL_UNSENT_CURRENT:
        return TERMINAL_UNSENT_CURRENT[job]
    if not isinstance(report, str):
        raise ValueError('REPORT_PATH')
    rp = (BUSINESS / report).resolve()
    if Path(report).is_absolute() or not rp.is_relative_to(BUSINESS.resolve()):
        raise ValueError('REPORT_PATH')
    matching = [r for r in json.loads(rp.read_bytes())['jobs'] if r.get('job') == job]
    if len(matching) != 1:
        raise ValueError('REPORT')
    row = matching[0]
    if row.get('disposition') != 'terminal_unsent_preinsert' or row.get('reason') not in ('WRONG_MODEL', 'SECURITY_INTERSTITIAL', 'CHAT_SURFACE_REFUSED'):
        raise ValueError('DISPOSITION')
    c = {'job': job, 'held': row['job_sha256'], 'message': row['message_sha256'],
         'manifest': row['manifest_sha256'], 'drive_prompt': row['drive_prompt_sha256'],
         'ledger_lines': tuple(row['ledger_line_sha256']), 'tab': row['error_tab'],
         'time_origin': row['time_origin'], 'status': 'failed', 'reason': row['reason'],
         'backup': None, 'generic': True, 'target_evidence': row['target_evidence'],
         'ledger_preimage_sha256': row['ledger_preimage_sha256'], 'ledger_preimage_bytes': row['ledger_preimage_bytes']}
    if (any(not re.fullmatch(r'[0-9a-f]{64}', v) for v in
            (c['held'], c['message'], c['manifest'], c['drive_prompt'], c['ledger_preimage_sha256'], *c['ledger_lines'])) or
            type(c['ledger_preimage_bytes']) is not int or c['ledger_preimage_bytes'] <= 0 or
            len(c['ledger_lines']) != 1 or not isinstance(c['tab'], str) or not c['tab'] or
            type(c['time_origin']) not in (int, float) or c['time_origin'] <= 0):
        raise ValueError('PINS')
    return c


def _terminal_mode_receipt_valid(error, tab):
    """Closed legacy/current pretyping schemas; never normalize factual receipts."""
    base = {'error', 'typed', 'sent', 'tab'}
    if (not isinstance(error, dict) or error.get('error') != 'CHAT_SURFACE_REFUSED' or
            error.get('typed') is not False or error.get('sent') is not False or
            not isinstance(tab, str) or not tab or error.get('tab') != tab):
        return False
    if set(error) == base:
        return True  # Historical four-field gate, still bound to full custody.
    if set(error) != base | {'mode_observation', 'mode_evidence'}:
        return False
    observation = error['mode_observation']
    if type(observation) is not int or not 1 <= observation <= 11:
        return False
    evidence = error['mode_evidence']
    reasons = {'mode_missing', 'mode_ambiguous', 'mode_unverified', 'work_selected'}
    if (not isinstance(evidence, dict) or
            set(evidence) not in ({'error', 'reason'}, {'error', 'reason', 'diagnostics'}) or
            evidence.get('error') != 'CHAT_SURFACE_REFUSED' or
            not isinstance(evidence.get('reason'), str) or evidence['reason'] not in reasons):
        return False
    if 'diagnostics' not in evidence:
        return True  # Earlier current gate before diagnostic sampling.
    d = evidence['diagnostics']
    excluded = {None, 'no_rect', 'hidden', 'inert', 'disabled', 'aria_hidden',
                'aria_disabled', 'display_none', 'visibility_hidden'}
    def enum(value, choices):
        return (value is None or isinstance(value, str)) and value in choices
    def count(value):
        return type(value) is int and 0 <= value <= 9007199254740991
    if (not isinstance(d, dict) or set(d) != {'main_present', 'main_count', 'group_count',
            'scope', 'group_excluded', 'chat', 'work'} or
            type(d['main_present']) is not bool or
            not (d['main_count'] is None or count(d['main_count'])) or
            not count(d['group_count']) or
            not enum(d['scope'], {'composer_mode', 'first_main', 'none'}) or
            not enum(d['group_excluded'], excluded)):
        return False
    if (d['scope'] != ('composer_mode' if d['group_count'] == 1 else
                      'first_main' if d['main_present'] else 'none') or
            (d['main_count'] is not None and d['main_present'] != (d['main_count'] > 0)) or
            (not d['main_present'] and d['group_count'] != 0) or
            (d['group_count'] != 1 and d['group_excluded'] is not None)):
        return False
    selected = {}
    for label in ('chat', 'work'):
        part = d[label]
        if (not isinstance(part, dict) or set(part) != {'raw_count', 'filtered_count', 'overflow', 'candidates'} or
                not count(part['raw_count']) or not count(part['filtered_count']) or
                part['filtered_count'] > part['raw_count'] or type(part['overflow']) is not bool or
                part['overflow'] != (part['raw_count'] > 8) or
                not isinstance(part['candidates'], list) or
                len(part['candidates']) != min(8, part['raw_count'])):
            return False
        visible = []
        for candidate in part['candidates']:
            if (not isinstance(candidate, dict) or set(candidate) != {'pressed', 'excluded'} or
                    not enum(candidate['pressed'], {'true', 'false', 'mixed', 'missing', 'other'}) or
                    candidate['pressed'] is None or not enum(candidate['excluded'], excluded)):
                return False
            if candidate['excluded'] is None:
                visible.append(candidate['pressed'])
        if (len(visible) > part['filtered_count'] or
                (not part['overflow'] and len(visible) != part['filtered_count'])):
            return False
        selected[label] = visible
    # Reject a success-shaped diagnostic snapshot, not just a success error tag.
    if (d['main_present'] and d['group_count'] <= 1 and d['group_excluded'] is None and
            d['chat']['filtered_count'] == d['work']['filtered_count'] == 1 and
            selected['chat'] == ['true'] and selected['work'] == ['false']):
        return False
    return True



def _current_terminal_unsent_custody(cfg, st, preimage, report, c=None):
    """Exact no-resume incidents; no inference from an error's stale sent flag."""
    c = c or TERMINAL_UNSENT_CURRENT[st['job']]
    sha = lambda b: hashlib.sha256(b).hexdigest()
    error = st.get('error') or {}
    observed = error.get('state') or {}
    attempt_custody = {}
    if c.get('pretarget'):
        # Closed pinned incident schema, no target or stale readiness inference.
        if (c['job'] != 'route-glp-80-0165c8c734145a8044438815' or c.get('generic') or
                c['tab'] is not None or c['time_origin'] is not None or
                error != {'error': 'TAB_CAP', 'typed': False, 'sent': False, 'count': 5, 'cap': 5}):
            raise ValueError('PRETARGET')
        lp = (BUSINESS / c['attempt_log']).resolve()
        if Path(c['attempt_log']).is_absolute() or not lp.is_relative_to(BUSINESS.resolve()):
            raise ValueError('LOG_PATH')
        log_bytes = lp.read_bytes()
        if (sha(log_bytes) != c['attempt_log_sha256'] or
                log_bytes != ('SEND_FAILED TAB_CAP ' + json.dumps(error) + '\n').encode()):
            raise ValueError('ATTEMPT_LOG')
        attempt_custody[lp] = log_bytes
    mode_refusal = c.get('generic') and c['reason'] == 'CHAT_SURFACE_REFUSED'
    if mode_refusal and not _terminal_mode_receipt_valid(error, c['tab']):
        raise ValueError('MODE_RECEIPT')
    # This gate's exact pre-input receipt carries its own target, but no state.
    # Never borrow this exception for readiness/model failures or missing tabs.
    readiness_invalid = (not c.get('pretarget') and not mode_refusal and
        (observed.get('url') != 'https://chatgpt.com/' or observed.get('stop') is not False or
         observed.get('assistant_count') != 0 or observed.get('last_len') != 0))
    if (sha(preimage) != c['held'] or json.loads(preimage) != st or
            st.get('status') != c['status'] or st.get('reason') != c['reason'] or
            st.get('retry_blocked') is not True or st.get('typed') is not False or st.get('sent') is not False or
            error.get('error') != c['reason'] or error.get('typed') is not False or error.get('sent') is not False or
            (error.get('tab') != c['tab'] and not (c.get('generic') and error.get('tab') is None)) or readiness_invalid or
            any(k in st for k in ('resume_attempt', 'resume_history', 'resume_error', 'terminal_unsent')) or
            any(st.get(k) for k in ('tab', 'conversation_id', 'conversation_url', 'pending_conversation_url', 'binding_version'))):
        raise ValueError('STATE')
    d = JOBS / c['job']
    if any((d / name).exists() for name in ('RESPONSE.md', 'outbox')):
        raise ValueError('OUTPUT')
    pins = {'MESSAGE.md': c['message'], 'MANIFEST.json': c['manifest'], 'drive-input/PROMPT.md': c['drive_prompt']}
    if c['backup']:
        pins['JOB.pre-readiness-reconcile-' + c['backup'] + '.json'] = c['backup']
    custody = {d / name: (d / name).read_bytes() for name in pins}
    custody.update(attempt_custody)
    if any(sha(custody[d / name]) != digest for name, digest in pins.items()):
        raise ValueError('CUSTODY')
    if c['backup']:
        original = json.loads(custody[d / ('JOB.pre-readiness-reconcile-' + c['backup'] + '.json')])
        if (st.get('preimage_sha256') != c['backup'] or original.get('status') != 'failed' or
                original.get('typed') is not False or original.get('sent') is not False or
                st != {**original, 'status': c['status'], 'preimage_sha256': c['backup'],
                       'reconciled_at': st.get('reconciled_at')}):
            raise ValueError('PREIMAGE')
    manifest = json.loads(custody[d / 'MANIFEST.json'])
    if (any(manifest.get(k) != st.get(k) or not st.get(k) for k in (*PROVENANCE_FIELDS, 'job', 'job_class')) or
            not isinstance(manifest.get('attach'), list) or not isinstance(manifest.get('drive_attach'), list) or
            _failed_generation_identity(st).encode() not in custody[d / 'MESSAGE.md']):
        raise ValueError('IDENTITY')
    paths = [d / 'PROMPT.md', *manifest['attach'], *manifest['drive_attach']]
    if any(not Path(p).resolve().is_relative_to(d.resolve()) for p in paths):
        raise ValueError('PACKET_PATH')
    custody.update({Path(p): Path(p).read_bytes() for p in paths})
    packet = [(Path(p).name, sha(custody[Path(p)])) for p in paths]
    if sha(json.dumps(packet, separators=(',', ':')).encode()) != st['input_sha256']:
        raise ValueError('PACKET')
    ledger = ledger_path(cfg)
    custody[ledger] = ledger.read_bytes()
    if c.get('generic') and sha(custody[ledger][:c['ledger_preimage_bytes']]) != c['ledger_preimage_sha256']:
        raise ValueError('LEDGER_PREIMAGE')
    all_rows = [(line, json.loads(line)) for line in custody[ledger].splitlines() if line.strip()]
    lines = [line for line, row in all_rows if row.get('job') == c['job']]
    n = len(c['ledger_lines'])
    if tuple(map(sha, lines[:n])) != c['ledger_lines']:
        raise ValueError('LEDGER')
    rows = [json.loads(line) for line in lines]
    if c.get('pretarget') and (len(c['ledger_lines']) != 1 or
            rows[0].get('event') != 'submitted' or rows[0].get('kind') != 'initial' or
            rows[0].get('certainty') != 'reserved_may_send' or rows[0].get('phase') is not None or
            rows[0].get('account') != st['account'] or
            any(rows[0].get(k) != st[k] for k in PROVENANCE_FIELDS)):
        raise ValueError('RESERVATION')
    rp = (BUSINESS / report).resolve()
    if Path(report).is_absolute() or not rp.is_relative_to(BUSINESS.resolve()):
        raise ValueError('REPORT_PATH')
    custody[rp] = rp.read_bytes()
    matching = [r for r in json.loads(custody[rp]).get('jobs', []) if r.get('job') == c['job']]
    expected = {'job': c['job'], 'disposition': 'terminal_unsent_preinsert' if c.get('generic') else 'terminal_unsent_obsolete', 'reason': c['reason'],
                'submission_bound': False, 'typed': False, 'sent': False, 'job_sha256': c['held'],
                'message_sha256': c['message'], 'error_tab': c['tab'], 'time_origin': c['time_origin'],
                'ledger_rows_sha256': sha(json.dumps(rows[:n], sort_keys=True, separators=(',', ':')).encode())}
    if (len(matching) != 1 or any(matching[0].get(k) != v for k, v in expected.items()) or
            any(matching[0].get(k) is not False for k in ('typed', 'sent', 'submission_bound'))):
        raise ValueError('REPORT')
    return custody, rows


def _generic_terminal_unsent_evidence(st, c, rows, custody):
    """Prove one initial preinsert refusal, including a missing error.tab's attribution.

    Historical evidence identifies the original document; it never substitutes for
    the fresh locked strict-empty witness required by the registered apply path.
    """
    sha = lambda b: hashlib.sha256(b).hexdigest()
    initial = rows[0]
    if (initial.get('event') != 'submitted' or initial.get('phase') is not None or
            initial.get('account') != st['account'] or initial.get('kind') != 'initial' or
            initial.get('certainty') != 'reserved_may_send' or
            any(initial.get(k) != st[k] for k in PROVENANCE_FIELDS)):
        raise ValueError('LEDGER')
    start = datetime.fromisoformat(initial['ts'])
    end = datetime.fromisoformat(st['ts'])
    if (start.tzinfo is None or end.tzinfo is None or
            not start.timestamp()*1000 <= c['time_origin'] <= end.timestamp()*1000):
        raise ValueError('DOCUMENT_INTERVAL')
    records = {}
    explicit_mode_target = c['reason'] == 'CHAT_SURFACE_REFUSED'
    for key in (('observation',) if explicit_mode_target else ('preflight', 'observation')):
        pin = c['target_evidence'][key]
        ep = (BUSINESS / pin['path']).resolve()
        if Path(pin['path']).is_absolute() or not ep.is_relative_to(BUSINESS.resolve()):
            raise ValueError('EVIDENCE_PATH')
        custody[ep] = ep.read_bytes()
        if sha(custody[ep]) != pin['sha256']:
            raise ValueError('EVIDENCE')
        records[key] = json.loads(custody[ep])
    before = set() if explicit_mode_target else {t['target_id'] for t in records['preflight']['targets']}
    observed = records['observation']['browser']
    candidates = [t for t in observed['metadata'] if
        (t['target_id'] == st['error']['tab'] if explicit_mode_target else t['target_id'] not in before) and
        t['url'] == 'https://chatgpt.com/' and start.timestamp()*1000 <= t['time_origin'] <= end.timestamp()*1000]
    if (len(candidates) != 1 or candidates[0]['target_id'] != c['tab'] or
            candidates[0]['time_origin'] != c['time_origin'] or
            sum(t['target_id'] == c['tab'] and t['url'] == 'https://chatgpt.com/'
                for t in observed['targets_before']) != 1):
        raise ValueError('TARGET_ATTRIBUTION')


def _terminal_unsent_custody(cfg, st, preimage, report):
    """Offline exact-byte proof shared by disposition and admission readback."""
    if st.get('job') == TERMINAL_GUARD_REFUSED['job']:
        return _guard_refusal_custody(cfg, st, preimage, report)
    if st.get('job') not in (TERMINAL_UNSENT_PR52['job'], *TERMINAL_UNSENT_CURRENT):
        c = _terminal_unsent_pin(st['job'], report)
        custody, rows = _current_terminal_unsent_custody(cfg, st, preimage, report, c)
        _generic_terminal_unsent_evidence(st, c, rows, custody)
        return custody, rows
    if st.get('job') in TERMINAL_UNSENT_CURRENT:
        return _current_terminal_unsent_custody(cfg, st, preimage, report)
    c = TERMINAL_UNSENT_PR52
    sha = lambda b: hashlib.sha256(b).hexdigest()
    def refuse(reason):
        raise ValueError(reason)
    if (sha(preimage) != c['held'] or json.loads(preimage) != st or st.get('job') != c['job'] or
            st.get('status') != 'failed' or st.get('reason') != 'RETAINED_RESUME_UNCERTAIN' or
            st.get('typed') is not False or st.get('sent') is not False or st.get('retry_blocked') is not True or
            st.get('resume_error') != {'error': 'composer_not_empty', 'typed': False,
                                     'sent': False, 'tab': c['tab']}):
        refuse('STATE')
    d = JOBS / c['job']
    if any((d / name).exists() for name in ('RESPONSE.md', 'outbox')):
        refuse('OUTPUT')
    pins = {'JOB.pre-retained-resume-' + c['backup'] + '.json': c['backup'],
            'MESSAGE.md': c['message'], 'MANIFEST.json': c['manifest'],
            'drive-input/PROMPT.md': c['drive_prompt']}
    custody = {d / name: (d / name).read_bytes() for name in pins}
    if any(sha(custody[d / name]) != digest for name, digest in pins.items()):
        refuse('CUSTODY')
    manifest = json.loads(custody[d / 'MANIFEST.json'])
    paths = [d / 'PROMPT.md', *manifest['attach'], *manifest['drive_attach']]
    if any(not Path(p).resolve().is_relative_to(d.resolve()) for p in paths):
        refuse('PACKET_PATH')
    custody.update({Path(p): Path(p).read_bytes() for p in paths})
    packet = [(Path(p).name, sha(custody[Path(p)])) for p in paths]
    if sha(json.dumps(packet, separators=(',', ':')).encode()) != st['input_sha256']:
        refuse('PACKET')
    ledger = ledger_path(cfg)
    custody[ledger] = ledger.read_bytes()
    # Parse every row strictly, including unrelated ones; never hide a binding.
    all_rows = [(line, json.loads(line)) for line in custody[ledger].splitlines() if line.strip()]
    lines = [line for line, row in all_rows if row.get('job') == c['job']]
    rows = [json.loads(line) for line in lines]
    if tuple(map(sha, lines[:2])) != c['ledger_lines']:
        refuse('LEDGER')
    rp = (BUSINESS / report).resolve()
    if Path(report).is_absolute() or not rp.is_relative_to(BUSINESS.resolve()):
        refuse('REPORT_PATH')
    custody[rp] = rp.read_bytes()
    matching = [r for r in json.loads(custody[rp]).get('jobs', []) if r.get('job') == c['job']]
    expected = {'job': c['job'], 'disposition': 'terminal_unsent_obsolete', 'reason': st['reason'],
                'submission_bound': False, 'typed': False, 'sent': False, 'job_sha256': c['held'],
                'message_sha256': c['message'], 'error_tab': c['tab'], 'time_origin': c['time_origin'],
                'ledger_rows_sha256': sha(json.dumps(rows[:2], sort_keys=True, separators=(',', ':')).encode())}
    if (len(matching) != 1 or any(matching[0].get(k) != v for k, v in expected.items()) or
            any(matching[0].get(k) is not False for k in ('typed', 'sent', 'submission_bound'))):
        refuse('REPORT')
    return custody, rows


def _terminal_unsent_event(st, report, report_bytes, observed_at):
    c = _terminal_unsent_pin(st['job'], report)
    return {'event': 'terminal_unsent', 'job': c['job'], 'account': st['account'],
            'preimage_sha256': c['held'], 'message_sha256': c['message'],
            'retained_tab': c['tab'], 'time_origin': c['time_origin'],
            'report': report, 'report_sha256': hashlib.sha256(report_bytes).hexdigest(),
            'observed_at': observed_at}


def _verified_terminal_unsent(st):
    """No flag-based release: require exact preimage + preserved packet + APPLIED journal.

    Fail closed after any interrupted apply or changed evidence. Cleanup deliberately
    does NOT use this admission exception: retry_blocked still protects error.tab.
    """
    if not isinstance(st.get('terminal_unsent'), dict):
        return False
    try:
        # Sibling records project the directory name over job; that projection is
        # not evidence. Both admission callers must match the raw persisted JOB.
        if json.loads((JOBS / st['job'] / 'JOB.json').read_bytes()) != st:
            return False
        proof = st['terminal_unsent']
        c = _terminal_unsent_pin(st['job'], proof['report'])
        n = len(c['ledger_lines'])
        backup = JOBS / st['job'] / ('JOB.pre-terminal-unsent-' + c['held'] + '.json')
        preimage = backup.read_bytes()
        original = json.loads(preimage)
        custody, rows = _terminal_unsent_custody(load_config(), original, preimage, proof['report'])
        expected = _terminal_unsent_event(original, proof['report'],
            custody[(BUSINESS / proof['report']).resolve()], proof['observed_at'])
        return (datetime.fromisoformat(proof['observed_at']).tzinfo is not None and
                proof == expected and st == {**original, 'terminal_unsent': proof} and len(rows) == n + 2 and
                all(row == {'ts': row.get('ts'), **proof, 'phase': phase}
                    for row, phase in zip(rows[n:], ('STARTED', 'APPLIED'))))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _reconcile_terminal_unsent(cfg, st, path, preimage, report, apply, authorize_preimage, account):
    """Registered, forward-only disposition; NEVER restores, clears or resends."""
    try:
        c = _terminal_unsent_pin(path.parent.name, report)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise SystemExit('TERMINAL_UNSENT_REFUSED_REPORT ' + path.parent.name)
    def refuse(reason) -> NoReturn:
        raise SystemExit('TERMINAL_UNSENT_REFUSED_' + reason + ' ' + c['job'])
    if (st.get('job') != c['job'] or hashlib.sha256(preimage).hexdigest() != c['held'] or
            st.get('account') != account or (apply and authorize_preimage != c['held']) or
            (not apply and authorize_preimage is not None)):
        refuse('AUTHORITY')
    try:
        custody, rows = _terminal_unsent_custody(cfg, st, preimage, report)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        refuse('CUSTODY')
    backup = path.with_name('JOB.pre-terminal-unsent-' + c['held'] + '.json')
    if len(rows) != len(c['ledger_lines']) or backup.exists():
        refuse('HISTORY')
    if c.get('generic') and len(custody[ledger_path(cfg)]) != c['ledger_preimage_bytes']:
        refuse('LEDGER_PREIMAGE')
    if _legacy_live_jobs():
        refuse('LIVE_RUNNER')
    admission = subprocess.run([sys.executable, str(BUSINESS / 'scripts/candidate_build_lock.py'),
                                'require-held'], capture_output=True, text=True)
    if admission.returncode:
        refuse('ADMISSION')
    # Pre-insert incidents retain strict-empty proof. The one post-insertion
    # guard refusal instead preserves the exact populated draft, never clears it.
    def tab_recheck():
        if c.get('postinsert_guard'):
            if not _retained_draft_witness(c['tab'], c['time_origin'], c['draft_sha256']):
                refuse('TAB_EVIDENCE')
        elif c['tab'] is not None and not _retained_empty_witness(c['tab'], c['time_origin'], disposition_only=True):
            refuse('TAB_EVIDENCE')
    tab_recheck()
    def recheck():
        if (path.read_bytes() != preimage or any(p.read_bytes() != b for p, b in custody.items()) or
                any((path.parent / name).exists() for name in ('RESPONSE.md', 'outbox')) or _legacy_live_jobs()):
            refuse('DRIFT')
    recheck()
    if not apply:
        print('WOULD_TERMINALIZE_UNSENT job=' + c['job'] + ' preimage_sha256=' + c['held'])
        return 0
    # Every original attempt/failure survives in the unchanged preimage and old rows.
    with backup.open('xb') as fh:
        fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
    tab_recheck()
    recheck()
    event = _terminal_unsent_event(st, report, custody[(BUSINESS / report).resolve()], now_iso())
    ledger_append(cfg, {**event, 'phase': 'STARTED'})
    job_write(c['job'], {**st, 'terminal_unsent': event})
    ledger_append(cfg, {**event, 'phase': 'APPLIED'})
    if not _verified_terminal_unsent(job_state(c['job'])):
        refuse('READBACK')
    # No cleanup, page close, status reset, or retry permission.
    print('TERMINALIZED_UNSENT job=' + c['job'] + '; permanently retry-blocked; retained tab protected')
    return 0


def reconcile_unsent_model(job: str, apply: bool = False, report: str | None = None,
                           authorize_preimage: str | None = None) -> int:
    """Reconcile pre-insert model/readiness refusals, never a possibly-sent job.

    Jobs are either allowlisted below or named with an explicit ``report``
    (a path relative to the business root) whose row must prove the job unsent;
    the account alias then comes from the job's own JOB.json.

    SECURITY_INTERSTITIAL additionally requires an explicit report row with
    reason, error_tab, job_sha256, message_sha256, ledger_rows_sha256 (SHA256
    of the exact job rows serialized with sorted keys and compact separators),
    and time_origin from an admitted read of the retained challenge-free tab.
    It preserves retry_blocked and the retained tab; it is not a resume API.
    Exact pinned obsolete PR52/current pre-insert jobs append terminal_unsent proof.
    The explicit report row must say disposition=terminal_unsent_obsolete;
    --apply also requires --authorize-preimage for the exact current JOB.
    Reusable disposition=terminal_unsent_preinsert binds complete current JOB,
    packet/message, whole-ledger preimage plus exact job row, frozen target
    attribution and fresh same-document strict-empty proof. It appends only
    STARTED/APPLIED and a terminal proof, never clears retry_blocked or cleans up.
    Only a DIFFERENT job can benefit from its verified terminal admission case.
    The separately pinned post-insertion composer-refusal incident additionally
    binds historical source/attempt evidence and exact populated-draft custody.
    Its original typed=true/sent=UNKNOWN/retry_blocked fields are preserved;
    generic possibly-sent jobs, retries and draft edits remain forbidden.
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
    selected_account = st['account']
    use_account(account_of(cfg, selected_account))
    with account_lock(), global_lock():
        path = JOBS / job / 'JOB.json'
        preimage = path.read_bytes()
        st = json.loads(preimage)
        facts = json.loads((BUSINESS / report_path).read_bytes()) if report is not None else {}
        terminal_requested = any(r.get('job') == job and r.get('disposition') == 'terminal_unsent_preinsert'
                                 for r in facts.get('jobs', []))
        if job in (TERMINAL_UNSENT_PR52['job'], TERMINAL_GUARD_REFUSED['job']) or job in TERMINAL_UNSENT_CURRENT or terminal_requested:
            return _reconcile_terminal_unsent(cfg, st, path, preimage, report_path,
                                              apply, authorize_preimage, selected_account)
        if authorize_preimage is not None:
            raise SystemExit('MODEL_RECONCILE_REFUSED_AUTHORITY_SCOPE ' + job)
        if st.get('reason') == 'SECURITY_INTERSTITIAL' and report is not None:
            return _reconcile_unsent_readiness(job, cfg, st, path, preimage,
                                              report_path, apply, selected_account)
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


# Only the PR60 reservation whose folder-create traceback and empty-folder
# readback were retained. This is not an UNKNOWN-as-unsent policy.
DRIVE_TIMEOUT_PR60 = {
    'job': 'pro-glp96-pr60-8b6b1b8-held-intake-union-review-20260928',
    'job_sha256': 'ef90a0be255a39c2c8b7c2e04662a7cddf54d0fdf0eb1ba4ac2de00494b2e2c4',
    'message_sha256': 'ed588ade8680e8675007fa7da986cc97cd79c13b639881c9aebe877af1bd73b4',
    'manifest_sha256': 'd4abdf3b18e62cf69f3b6cdf442d19e304921fff12c35323ca7255d8ab368c15',
    'prompt_sha256': '82ad8c037ed945cb32a9a6ff2fe5ecd223ae2d3a64c60c0c3c4ed0f8562238cd',
    'incident_sha256': 'ad825788d91be035210e5192306bff5d472adf2a534ef2384c8803323d8c8d93',
    'submit_sha256': '2c7d6978b3b6954d31a00a45b7f9447bfb857739d2dd8b11240b6a0b96e7a361',
    'folder_id_sha256': '504a34c4803f3a8c6348740fc48b84d525925285634370142b77ebfd3dc72832',
    'reservation_ledger_line': 667,
    'input_sha256': 'a5fe0ae77f68aaf5736ba265af21c0bfdcbe6c5bf1fa82878d1bb8e5cce4647a',
    'account': 'izaiahtilton1@gmail.com',
}


def resume_drive_timeout(job: str, authorize_preimage: str | None = None) -> int:
    """Inspect the exact stranded reservation; explicit hash authorizes one continuation.

    Never call drive_subfolder: a timed-out create may already have landed.
    The original UNKNOWN fields remain until the existing send guards bind a chat.
    """
    c = DRIVE_TIMEOUT_PR60
    if job != c['job']:
        raise SystemExit('DRIVE_RESUME_REFUSED_JOB')
    cfg = load_config()
    acct = account_of(cfg, c['account'])
    use_account(acct)
    d = JOBS / job
    path = d / 'JOB.json'
    def refuse(reason: str) -> NoReturn:
        raise SystemExit('DRIVE_RESUME_REFUSED_' + reason)
    with account_lock(), global_lock():
        try:
            preimage = path.read_bytes()
            st = json.loads(preimage)
            message = (d / 'MESSAGE.md').read_bytes()
            manifest_bytes = (d / 'MANIFEST.json').read_bytes()
            manifest = json.loads(manifest_bytes)
            prompt_bytes = (d / 'PROMPT.md').read_bytes()
            incident_bytes = (BUSINESS / 'reports/ops/glp96-current-integration-pro-review-20260928/SUBMISSION-INCIDENT.json').read_bytes()
            submit_bytes = (BUSINESS / 'reports/ops/glp96-current-integration-pro-review-20260928/20260928T011108Z-submit.json').read_bytes()
            incident = json.loads(incident_bytes)
            submit = json.loads(submit_bytes)
        except (OSError, ValueError, TypeError):
            refuse('CUSTODY')
        sha = lambda data: hashlib.sha256(data).hexdigest()
        if (sha(preimage) != c['job_sha256'] or sha(message) != c['message_sha256'] or
                sha(manifest_bytes) != c['manifest_sha256'] or sha(prompt_bytes) != c['prompt_sha256'] or
                sha(incident_bytes) != c['incident_sha256'] or sha(submit_bytes) != c['submit_sha256'] or
                st.get('job') != job or st.get('account') != acct['label'] or
                st.get('job_class') != 'independent-review' or st.get('status') != 'submitting' or
                st.get('retry_blocked') is not True or st.get('typed') != 'UNKNOWN' or
                st.get('sent') != 'UNKNOWN' or st.get('input_sha256') != c['input_sha256'] or
                any(st.get(k) for k in ('tab', 'conversation_url', 'pending_conversation_url', 'binding_version')) or
                not all(st.get(k) and manifest.get(k) == st[k] for k in PROVENANCE_FIELDS) or
                manifest.get('job') != job or manifest.get('job_class') != st['job_class'] or
                manifest.get('attach') != [] or manifest.get('kind') != 'freeform' or
                not isinstance(manifest.get('drive_attach'), list) or
                len(manifest['drive_attach']) != 27 or
                _failed_generation_identity(st).encode() not in message or
                (d / 'RESPONSE.md').exists() or (d / 'outbox').exists()):
            refuse('STATE_OR_PACKET')
        # The original manifest has absolute installed paths. Resolve only their
        # basenames within this job; the pinned manifest and packet hash bind order.
        names = [Path(p).name for p in manifest['drive_attach']]
        if (len(set(names)) != len(names) or any(Path(p).parent !=
                Path('/home/ubuntu/business/reports/ops/gpt6pro-relay/jobs') / job
                for p in manifest['drive_attach']) or
                any(not n.startswith('DRIVE-') for n in names)):
            refuse('PATHS')
        paths = [d / 'PROMPT.md', *(d / n for n in names)]
        uploads = [d / 'MESSAGE.md', d / 'drive-input/PROMPT.md', *(d / n for n in names)]
        try:
            packet = [(p.name, sha(p.read_bytes())) for p in paths]
            upload_bytes = [p.read_bytes() for p in uploads]
            ledger_bytes = ledger_path(cfg).read_bytes()
            rows = [json.loads(line) for line in ledger_bytes.splitlines() if line.strip()]
        except (OSError, ValueError, TypeError):
            refuse('PACKET_OR_LEDGER')
        if (sha(json.dumps(packet, separators=(',', ':')).encode()) != c['input_sha256'] or
                sha(upload_bytes[0]) != c['message_sha256'] or
                len(set(p.name for p in uploads)) != len(uploads) or
                upload_bytes[1] != prompt_bytes or
                any(r.get('job') == job for r in rows if r.get('event') != 'submitted') or
                len([r for r in rows if r.get('job') == job]) != 1 or
                len(rows) < c['reservation_ledger_line'] or
                rows[c['reservation_ledger_line'] - 1].get('job') != job):
            refuse('PACKET_OR_RESERVATION')
        reservation = next(r for r in rows if r.get('job') == job)
        if (reservation.get('event') != 'submitted' or reservation.get('kind') != 'initial' or
                reservation.get('certainty') != 'reserved_may_send' or
                reservation.get('account') != acct['label'] or
                any(reservation.get(k) != st[k] for k in PROVENANCE_FIELDS) or
                incident.get('job_sha256') != c['job_sha256'] or
                incident.get('message_sha256') != c['message_sha256'] or
                incident.get('reservation_ledger_line') != c['reservation_ledger_line'] or
                incident.get('submission_bound_count') != 0 or
                incident.get('drive_inbox_folders') != [{'folder_id_sha256': c['folder_id_sha256'],
                                                           'file_count': 0, 'media': []}] or
                incident.get('fully_paginated_exact_inbox_query') is not True or
                submit.get('returncode') != 1 or
                'in _cmd_submit' not in submit.get('stderr', '') or
                'in drive_subfolder' not in submit.get('stderr', '') or
                'TimeoutError' not in submit.get('stderr', '')):
            refuse('INCIDENT')
        if _legacy_live_jobs() or any(
                (s.get('status') in ACTIVE_STATES or s.get('retry_blocked')) and
                not binding_url(s) and not _verified_terminal_unsent(s)
                for s in sibling_bindings(acct['label'], job)):
            refuse('SIBLING_OR_RUNNER')
        active = active_registry()
        if (len(active) > min(6, cfg['envelope']['max_concurrent_chats_per_ip']) or
                sum(r['account'] == acct['label'] for r in active) >
                min(2, cfg['envelope']['max_concurrent_chats_per_account'])):
            refuse('CAPACITY')
        svc = drive()
        folder_query = (f"'{cfg['drive']['inbox_id']}' in parents and name = '{job}' "
                        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
        try:
            folders = drive_list_all(svc, folder_query)
            if (len(folders) != 1 or sha(folders[0]['id'].encode()) != c['folder_id_sha256'] or
                    folders[0].get('name') != job or
                    folders[0].get('mimeType') != 'application/vnd.google-apps.folder' or
                    drive_list_all(svc, f"'{folders[0]['id']}' in parents and trashed = false")):
                refuse('FOLDER')
        except (KeyError, ValueError, TypeError):
            refuse('FOLDER')
        if authorize_preimage is None:
            print('WOULD_RESUME_DRIVE_FOLDER job=' + job + ' preimage_sha256=' + sha(preimage) +
                  '; retry_blocked and UNKNOWN retained')
            return 0
        if authorize_preimage != sha(preimage):
            refuse('AUTHORITY')
        # Fresh effect authority must also pass the existing heavy-seat admission.
        admission = subprocess.run([sys.executable, str(BUSINESS / 'scripts/candidate_build_lock.py'),
                                    'require-held'], capture_output=True, text=True)
        if admission.returncode:
            refuse('ADMISSION')
        clock_path = BUSINESS / 'ops/gpt6pro-relay/admission.json'
        clock = json.loads(clock_path.read_text()) if clock_path.exists() else {}
        if time.time() < float(clock.get('last_submit', 0)) + max(60, cfg['envelope']['min_seconds_between_submits']):
            refuse('PACING')
        used = messages_used_7d(cfg, acct['label'])
        limit = int(acct['weekly_messages'] * (1 - cfg.get('budget_reserve_pct', 10) / 100))
        if used > limit:
            refuse('BUDGET')
        if (path.read_bytes() != preimage or (d / 'MESSAGE.md').read_bytes() != message or
                (d / 'MANIFEST.json').read_bytes() != manifest_bytes or
                (d / 'PROMPT.md').read_bytes() != prompt_bytes or
                ledger_path(cfg).read_bytes() != ledger_bytes or
                any(p.read_bytes() != data for p, data in zip(uploads, upload_bytes)) or
                drive_list_all(svc, folder_query) != folders or
                drive_list_all(svc, f"'{folders[0]['id']}' in parents and trashed = false")):
            refuse('DRIFT')
        backup = d / ('JOB.pre-drive-timeout-' + sha(preimage) + '.json')
        try:
            with backup.open('xb') as fh:
                fh.write(preimage); fh.flush(); os.fsync(fh.fileno())
        except FileExistsError:
            refuse('ALREADY_ATTEMPTED')
        attempt = {'job': job, 'account': acct['label'], 'preimage_sha256': sha(preimage),
                   'message_sha256': sha(message), 'incident_sha256': sha(incident_bytes),
                   'folder_id_sha256': c['folder_id_sha256'], 'attempt_at': now_iso()}
        ledger_append(cfg, {'event': 'drive_timeout_resume', 'phase': 'STARTED', **attempt})
        pending = {**st, 'status': 'drive_resume_submitting', 'drive_resume_attempt': attempt}
        job_write(job, pending)
        clock['last_submit'] = time.time()
        _atomic_json(clock_path, clock)
        # No second folder create and no new reservation: original upload set,
        # original message, then the installed Pro/model/atomic-send guards.
        links = {p.name: drive_upload(svc, folders[0]['id'], p) for p in uploads}
        forbidden = [url for s in sibling_bindings(acct['label'], job)
                     if (url := binding_url(s))]
        result = send_message(message.decode('utf-8'), forbidden=forbidden)
        if 'error' not in result and (not binding_url(result) or binding_url(result) in forbidden):
            result = {'error': 'conversation_collision' if binding_url(result) in forbidden else 'binding_unverified',
                      'typed': True, 'sent': True, 'result': result}
        if 'error' in result:
            job_write(job, {**pending, 'status': 'failed', 'reason': result['error'],
                            'error': result, 'typed': result.get('typed', 'UNKNOWN'),
                            'sent': result.get('sent', 'UNKNOWN')})
            raise SystemExit('DRIVE_RESUME_HELD ' + str(result['error']))
        bound = {**pending, 'status': 'running', 'retry_blocked': False,
                 'typed': True, 'sent': True, 'binding_version': 2,
                 'conversation_id': binding_url(result).rsplit('/', 1)[1],
                 'tab': result.get('tab'), 'submitted': now_iso(),
                 'chars_in': len(message.decode('utf-8')),
                 'drive_files': [str(p) for p in uploads[1:]], 'drive_inbox': links,
                 'prompt': str(d / 'drive-input/PROMPT.md'), 'attachments': []}
        key = 'conversation_url' if result.get('conversation_url') else 'pending_conversation_url'
        bound[key] = binding_url(result)
        require_binding(bound, result)
        job_write(job, bound)
        ledger_append(cfg, {'event': 'submission_bound', 'job': job, 'account': acct['label'],
                            'job_class': st['job_class'], 'conversation_url': binding_url(result),
                            'chars_in': bound['chars_in'], 'used_7d_after': used,
                            'limit': limit, **{k: st[k] for k in PROVENANCE_FIELDS}})
        print('RESUMED_DRIVE_FOLDER job=' + job + ' conv=' + binding_url(result))
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
    if previous.get("status") in ("submitting", "running", "done", "SUPERSEDED") or previous.get("retry_blocked"):
        raise SystemExit(f"REFUSED_DUPLICATE job {a.job} already submitted or uncertain (use poll/operator recovery)")
    siblings = sibling_bindings(acct["label"], a.job)
    if any((s.get("status") in ("submitting", "running") or s.get("retry_blocked")) and
           not binding_url(s) and not _verified_terminal_unsent(s) for s in siblings):
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
             'For output files in either channel supply OUTPUT-MANIFEST.json with schema="gpt6pro-output-manifest-v2", '
             'job, input_sha256, reply_token (copy the exact values above), and files (object mapping '
             'payload basename to SHA256 of the exact saved file bytes; exclude OUTPUT-MANIFEST.json itself). '
             'Do NOT include conversation_id: the relay verifies the actual chat URL, which you cannot know '
             'from the prompt. Do NOT include source_reply_sha256: the browser-rendered final reply may differ '
             'from the text you composed; the relay verifies its own exact captured reply hash. '
             'Choose one authoritative output channel: Drive or native downloads. Never produce a second revised '
             'copy after hashing the first. Read back and hash the exact final manifest and payload bytes in '
             'the chosen channel; identify the channel in the reply. The host must not reserialize a manifest '
             'or reconstruct payload bytes from rendered chat. Earlier unbound copies remain unauthenticated. '
             'Append RELAY_OUTPUT_MANIFEST_SHA256=<SHA256 of exact final authoritative OUTPUT-MANIFEST.json bytes> '
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


def _record_generation_observation(job: str, cfg: dict, st: dict, result: dict,
                                   started: str, finished: str) -> None:
    """Append v1 point evidence to the existing ledger, never interval qualification.

    Caller has validated the exact bound read. UI authentication evidence means
    an explicit non-login/non-challenge state and the full registered user marker;
    it does not identify the signed-in email or prove continuous generation.
    Missing booleans remain null. This receipt changes no admission/terminal gate.
    Provenance covers this assigned job only, not the existence of fresh backlog.
    """
    state = result['state']
    fields = ('stop', 'logged_out', 'security_interstitial', 'human_challenge',
              'rate_limited', 'drive_reconnect', 'thinking_failed', 'user_identity_matched')
    observed = {key: state.get(key) if type(state.get(key)) is bool else None
                for key in fields}
    expected = {key: key in ('stop', 'user_identity_matched') for key in fields}
    if any(observed[key] is not None and observed[key] != value
           for key, value in expected.items()):
        generating = False
    elif any(value is None for value in observed.values()):
        generating = None
    else:
        generating = True
    # Legacy bound jobs can lack a token; never fabricate its identity hash.
    reply_token = st.get('reply_token')
    reply_token_sha256 = (hashlib.sha256(reply_token.encode()).hexdigest()
                          if isinstance(reply_token, str) and reply_token else None)
    ledger_append(cfg, {'event': 'generation_observation',
        'schema': 'gpt6pro-generation-observation-v1',
        'coverage': 'single_bound_read_only', 'read_started_at': started,
        'read_finished_at': finished, 'job': job, 'account': st['account'],
        'conversation_url': binding_url(st), 'conversation_id': st['conversation_id'],
        'binding_version': st['binding_version'], 'tab': result['tab'],
        'observed': observed, 'authenticated_generation_observed': generating,
        'reply_token_sha256': reply_token_sha256,
        **{key: st.get(key) for key in (*PROVENANCE_FIELDS, 'job_class', 'input_sha256')}})


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
    read_started = now_iso()
    r = read_reply(conv, st.get("tab"), existing_only=True, protected=protected_tabs(st),
                   expected_identity=_failed_generation_identity(st),
                   capture_mode=st.get('reply_render_mode'))
    read_finished = now_iso()
    require_binding(st, r)
    _record_generation_observation(job, cfg, st, r, read_started, read_finished)
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
    ss = sub.add_parser('supersede-research', help='dry-run by default; registered operator ruling for sent/bound research-only jobs')
    ss.add_argument('--job', required=True)
    ss.add_argument('--ruling', required=True, help='registered approval path relative to business root')
    ss.add_argument('--apply', action='store_true')
    ss.add_argument('--authorize-preimage', help='exact original JOB.json SHA256, required only with --apply')
    nr = sub.add_parser("reconcile-no-reply", help="read exact bound tab without reload or freeing uncertain capacity")
    nr.add_argument("--job", required=True)
    u = sub.add_parser("reconcile-unsent-focus", help="dry-run first; exact pre-insert focus() KeyError only")
    u.add_argument("--job", required=True)
    u.add_argument("--failure-log", type=Path, required=True)
    u.add_argument("--apply", action="store_true")
    df = sub.add_parser('resume-drive-timeout', help='read-only by default; exact PR60 empty-folder reservation only')
    df.add_argument('--job', required=True)
    df.add_argument('--authorize-preimage', help='exact current JOB.json SHA256; one effectful continuation')
    rr = sub.add_parser('resume-unsent-readiness', help='read-only by default; explicit one-shot authority for exact unsent retained tab')
    rr.add_argument('--job', required=True)
    rr.add_argument('--report', required=True)
    rr.add_argument('--authorize-preimage', help='exact original JOB.json SHA256; authorizes one effectful continuation')
    m = sub.add_parser("reconcile-unsent-model", help="dry-run first; pre-insert WRONG_MODEL or hash-bound SECURITY_INTERSTITIAL only")
    m.add_argument("--job", required=True)
    m.add_argument("--report", help="unlisted job: report JSON (relative to business root) whose row proves it unsent")
    m.add_argument("--apply", action="store_true")
    m.add_argument('--authorize-preimage', help='exact current JOB SHA256; required with --apply only for pinned obsolete terminal-unsent disposition')
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
    fo.add_argument('--download-dir', type=Path, help='exact original native downloads; same final-chat hash/token/payload checks, never rewrite Drive artifacts')
    f = sub.add_parser("followup")
    f.add_argument("--job", required=True)
    f.add_argument("--text-file", help="message text; default = resume-after-disconnect")
    p = sub.add_parser("poll")
    p.add_argument("--job", required=True)
    p.add_argument("--wait", action="store_true")
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--max-wait", type=int, default=3600)
    a = ap.parse_args()
    if a.cmd == 'supersede-research':
        return supersede_research(a.job, a.ruling, apply=a.apply, authorize_preimage=a.authorize_preimage)
    if a.cmd == "status":
        return cmd_status(a)
    if a.cmd == "reconcile-no-reply":
        return reconcile_no_reply(a.job)
    if a.cmd == "reconcile-unsent-focus":
        return reconcile_unsent_focus(a.job, a.failure_log, apply=a.apply)
    if a.cmd == 'resume-drive-timeout':
        return resume_drive_timeout(a.job, a.authorize_preimage)
    if a.cmd == 'resume-unsent-readiness':
        return resume_unsent_readiness(a.job, a.report, a.authorize_preimage)
    if a.cmd == "reconcile-unsent-model":
        return reconcile_unsent_model(a.job, apply=a.apply, report=a.report,
                                      authorize_preimage=a.authorize_preimage)
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
