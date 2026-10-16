#!/usr/bin/env python3
"""Receive independently accepted exact-head PRs. Never reviews or closes Linear.

See ops/linear/AUTO-DEPLOYER.md for review format and recovery contract.
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

ROOT = Path('/home/ubuntu/business')
REPOS = ('izaiahat/glp-business', 'izaiahat/glp-site-candidate')
OPS_ALERTS = '1487300132054765629'
MANIFEST = 'ops/rebuild-source-manifest.json'
LOCK_FD = None
SHA = re.compile(r'[0-9a-f]{40}\Z')


def utc():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')


def immutable(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as out:
        json.dump(data, out, indent=2, sort_keys=True)
        out.write('\n')
        out.flush()
        os.fsync(out.fileno())
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run(argv, cwd=ROOT, timeout=120):
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True,
                          timeout=timeout, check=True,
                          pass_fds=(LOCK_FD,) if LOCK_FD is not None else ()).stdout


class GitHub:
    def api(self, endpoint):
        return json.loads(run(['gh', 'api', endpoint]))

    def pages(self, endpoint, field=None):
        # Host gh 2.45 has --paginate but NOT --slurp. Decode concatenated
        # JSON documents without dropping later pages or trusting line layout.
        payload = run(['gh', 'api', '--paginate', endpoint]).lstrip()
        decoder, result = json.JSONDecoder(), []
        while payload:
            page, offset = decoder.raw_decode(payload)
            rows = page[field] if field else page
            if not isinstance(rows, list):
                raise ValueError('paginated GitHub response is not a list')
            result.extend(rows)
            payload = payload[offset:].lstrip()
        return result

    def open_prs(self, repo):
        return [int(p['number']) for p in self.pages(f'repos/{repo}/pulls?state=open&per_page=100')]

    def snapshot(self, repo, number):
        p = self.api(f'repos/{repo}/pulls/{number}')
        head = p['head']['sha']
        view = json.loads(run(['gh', 'pr', 'view', str(number), '--repo', repo,
                              '--json', 'mergeStateStatus,headRefOid']))
        if view['headRefOid'] != head:
            raise ValueError('head changed during GitHub observation')
        return {'repo': repo, 'number': number, 'state': p['state'].upper(),
                'baseRefName': p['base']['ref'], 'isDraft': p['draft'],
                'headRefOid': head, 'body': p.get('body') or '', 'changed_files': p['changed_files'],
                'labels': p['labels'], 'mergeStateStatus': view['mergeStateStatus'],
                'headCommittedAt': self.api(f'repos/{repo}/commits/{head}')['commit']['committer']['date'],
                'reviews': self.pages(f'repos/{repo}/pulls/{number}/reviews?per_page=100'),
                'checks': self.pages(f'repos/{repo}/commits/{head}/check-runs?filter=all&per_page=100', 'check_runs')}

    def files(self, repo, number):
        return self.pages(f'repos/{repo}/pulls/{number}/files?per_page=100')

    def manifest(self, repo, head):
        content = self.api(f'repos/{repo}/contents/{MANIFEST}?ref={head}')
        return json.loads(base64.b64decode(content['content']))

    def merge(self, repo, number, head):
        run(['gh', 'pr', 'merge', str(number), '--repo', repo, '--merge',
             '--match-head-commit', head])

    def merged(self, repo, number, head):
        p = self.api(f'repos/{repo}/pulls/{number}')
        merge = p.get('merge_commit_sha')
        if not (p.get('merged') is True and p['head']['sha'] == head
                and SHA.fullmatch(merge or '') and p.get('merged_at')):
            raise ValueError('merge readback not exact; inspect target, do not retry')
        commit = self.api(f'repos/{repo}/commits/{merge}')
        if head not in [c['sha'] for c in commit['parents']]:
            raise ValueError('merge does not directly contain accepted head')
        return {'head': head, 'merge_commit': merge, 'merged_at': p['merged_at']}


def admission(pr):
    """Fail closed on machine-readable reviewer attestation, never GH identity."""
    if (pr.get('repo') not in REPOS or pr.get('state') != 'OPEN'
            or pr.get('baseRefName') != 'main' or pr.get('isDraft') is not False
            or pr.get('mergeStateStatus') != 'CLEAN'):
        raise ValueError('not an open, ready, CLEAN allowlisted main PR')
    head = pr.get('headRefOid', '')
    if not SHA.fullmatch(head):
        raise ValueError('invalid head')
    if any(label['name'].lower() in {'hold', 'do-not-merge'} for label in pr['labels']):
        raise ValueError('held label')
    tickets = re.findall(r'^fleet-ticket:(GLP-[1-9][0-9]*)\s*$', pr['body'], re.M)
    if len(tickets) != 1:
        raise ValueError('require exactly one fleet-ticket:GLP-N line')
    checks = pr['checks']
    if not checks or any(c.get('head_sha') != head or c.get('status') != 'completed'
                         or c.get('conclusion') != 'success' for c in checks):
        raise ValueError('nonempty all-SUCCESS exact-head checks required')
    committed = datetime.fromisoformat(pr['headCommittedAt'].replace('Z', '+00:00'))
    reviews = [r for r in pr['reviews'] if r.get('commit_id') == head
               and r.get('state') not in {'PENDING', 'DISMISSED'}]
    if not reviews:
        raise ValueError('missing exact-head review')
    review = max(reviews, key=lambda r: (r.get('submitted_at') or '', r['id']))
    if (review['state'] != 'COMMENTED'
            or datetime.fromisoformat(review['submitted_at'].replace('Z', '+00:00')) <= committed):
        raise ValueError('require COMMENT review strictly after head commit')
    body = review['body']
    if not re.match(r'^(?:APPROVE_SOURCE_ONLY|ACCEPT_SOURCE|ACCEPT)(?=\s|$|[,:.])', body):
        raise ValueError('review must start with accepted verdict')
    def field(name):
        values = re.findall(r'^' + re.escape(name) + r': ([^\r\n]+)\r?$', body, re.M)
        if len(values) != 1:
            raise ValueError('missing/duplicate review field ' + name)
        return values[0]
    if (field('Head') != head or field('Independent') != 'true' or field('Non-author') != 'true'
            or field('Reviewer').casefold() == field('Source-author').casefold()):
        raise ValueError('independent non-author exact-head attestation required')
    return {'issue': tickets[0], 'review': review, 'checks': checks}


def runtime_paths(files, manifest):
    selected = set()
    for change in files:
        path = change['filename']
        pp = PurePosixPath(path)
        if pp.is_absolute() or '..' in pp.parts or str(pp) != path:
            raise ValueError('unsafe changed path')
        # Docs/tests/reports/control metadata are not live runtime payloads.
        if (path == MANIFEST or pp.parts[0] in {'tests', 'reports', '.github', '.codex-task'}
                or 'tests' in pp.parts or pp.suffix.lower() in {'.md', '.rst'}):
            continue
        if change['status'] in {'removed', 'renamed'}:
            raise ValueError('runtime deletion/rename needs an explicit receiver: ' + path)
        if path not in manifest['files']:
            raise ValueError('uncurated runtime path: ' + path)
        selected.add(path)
    return sorted(selected)


def inspect_paths(root, paths):
    result = {}
    for path in paths:
        target = root / path
        try:
            if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
                result[path] = {'error': 'symlink/escape refused'}
            else:
                result[path] = {'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                                'mode': oct(target.stat().st_mode & 0o777)}
        except OSError as exc:
            result[path] = {'error': str(exc)}
    return result


class Deployer:
    def __init__(self, root, remote, notify=None):
        self.root, self.remote = Path(root), remote
        self.base = self.root / 'reports/ops/auto-deployer'
        self.notify = notify or self.discord

    def discord(self, text):
        return run([sys.executable, '-B', str(self.root / 'scripts/post_discord_message.py'),
                    '--channel', OPS_ALERTS, '--message', text], self.root)

    def install(self, checkout, paths, runner=run):
        if not paths:
            return {'status': 'NO_RUNTIME_PATHS', 'paths': []}
        argv = [sys.executable, '-B', str(self.root / 'scripts/sync_from_repo.py'),
                '--root', str(self.root), '--checkout', str(checkout)]
        for path in paths:
            argv += ['--only', path]
        result = {'status': 'BLOCKED_UNCERTAIN', 'paths': paths, 'steps': [], 'checkout': str(checkout)}
        try:
            for flag in ('--dry-run', '--apply', '--dry-run'):
                cmd = argv[:3] + [flag] + argv[3:]
                output = runner(cmd, self.root, timeout=300)
                result['steps'].append({'argv': cmd, 'stdout': output})
            if not re.search(r'^DRY_RUN diffs=0 conflicts=0 tracked=[0-9]+$', output, re.M):
                raise ValueError('post-install dry run did not prove zero differences')
            result['status'] = 'INSTALLED_SOURCE'
        except Exception as exc:
            # Do not run apply again, even after explicit refusal. Inspect target.
            result['reason'] = str(exc)[:1000]
        result['target_inspection'] = inspect_paths(self.root, paths)
        return result

    def checkout(self, repo, commit, *, reviewed_head, paths):
        # Full genuine ancestry is kept for the installer's baseline validator.
        scratch = Path(os.environ.get('TMPDIR', '/home/ubuntu/.hermes/cache/scratch'))
        scratch.mkdir(parents=True, exist_ok=True)
        target = Path(tempfile.mkdtemp(prefix='auto-deployer-', dir=scratch)) / 'repo'
        run(['git', 'clone', '--quiet', '--no-checkout', f'git@github.com:{repo}.git', str(target)], self.root, timeout=300)
        run(['git', '-C', str(target), 'checkout', '--quiet', '--detach', commit], self.root)
        if run(['git', '-C', str(target), 'rev-parse', 'HEAD'], self.root).strip() != commit:
            raise ValueError('checkout not exact merge commit')
        # A concurrent main change must not silently widen the reviewed payload.
        run(['git', '-C', str(target), 'diff', '--quiet', reviewed_head, commit, '--', *paths], self.root)
        return target

    def receipts(self):
        # Corrupt history refuses, never becomes permission to repeat an effect.
        return [(p, json.loads(p.read_text())) for p in self.base.glob('[0-9]*/*.json')]

    def intent_path(self, pr):
        return self.base / '.state/intents' / f'{pr["repo"].split("/")[1]}-{pr["number"]}-{pr["headRefOid"]}.json'

    def reconcile(self, dry_run=False):
        """Inspect unfinished attempts, including PRs no longer open. No replay."""
        receipts = self.receipts()
        if not dry_run:
            for path, receipt in receipts:
                self.deliver(path, receipt)
        known = {(r['repo'], r['pr'], r['head']) for _, r in receipts}
        blocked = [r for _, r in receipts if r['status'] == 'BLOCKED_UNCERTAIN']
        for path in sorted((self.base / '.state/intents').glob('*.json')):
            item = json.loads(path.read_text())
            if (item['repo'], item['pr'], item['head']) in known:
                continue
            observation = self.inspect_merge(item['repo'], item['pr'], item['head'])
            receipt = dict(item, schema='auto-deployer-v1', status='BLOCKED_UNCERTAIN', inspection=observation,
                           live_output_proven=False,
                           target_inspection=inspect_paths(self.root, item.get('runtime_paths', [])),
                           reason='unfinished intent; owner must reconcile; never automatically replay')
            if not dry_run:
                self.publish(receipt)
            blocked.append(receipt)
        return blocked

    def inspect_merge(self, repo, number, head):
        try:
            return self.remote.merged(repo, number, head)
        except Exception as exc:
            return {'error': type(exc).__name__, 'reason': str(exc)[:1000]}

    def publish(self, receipt):
        path = self.base / utc() / f'{receipt["repo"].split("/")[1]}-{receipt["pr"]}.json'
        immutable(path, receipt)
        self.deliver(path, receipt)
        return receipt

    def deliver(self, path, receipt):
        """Recover only absent outbox stages, never source effects or a claimed send."""
        inbox = self.root / 'reports/ops/fleet-inbox'
        inbox.mkdir(parents=True, exist_ok=True)
        with (inbox / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            journal = inbox / 'auto-deployer.jsonl'
            row = {'receipt': str(path.relative_to(self.root)),
                   'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            rows = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
            matching = [r for r in rows if r['receipt'] == row['receipt']]
            if matching and matching != [row]:
                raise ValueError('conflicting/duplicated inbox receipt; inspect, never append blindly')
            if not matching:
                with journal.open('a') as out:
                    out.write(json.dumps(row) + '\n')
                    out.flush()
                    os.fsync(out.fileno())
        # Durable claim BEFORE the send. Lost acknowledgement is never resent.
        notification = self.base / '.state/notifications' / (path.parent.name + '.json')
        if notification.exists():
            return
        immutable(notification, {'receipt': str(path), 'status': 'SEND_INTENT'})
        text = f'{receipt["issue"]} {receipt["repo"]}#{receipt["pr"]} {receipt["status"]}; head {receipt["head"][:12]}; live output not yet proven'
        try:
            response = self.notify(text)
            immutable(notification.with_suffix('.result.json'), {'response': response})
        except Exception as exc:
            immutable(notification.with_suffix('.error.json'), {'error': str(exc), 'no_retry': True})

    def process(self, pr, dry_run=False):
        repo, number, head = pr['repo'], pr['number'], pr['headRefOid']
        for _, prior in self.receipts():
            if (prior['repo'], prior['pr'], prior['head']) == (repo, number, head):
                return {'repo': repo, 'pr': number, 'head': head, 'status': 'SKIP_RECEIPTED'}
        if self.intent_path(pr).exists():
            return {'repo': repo, 'pr': number, 'status': 'BLOCKED_UNCERTAIN',
                    'inspection': self.inspect_merge(repo, number, head)}
        try:
            accepted = admission(pr)
            fresh = self.remote.snapshot(repo, number)
            if fresh['headRefOid'] != head:
                raise ValueError('head changed during admission')
            accepted = admission(fresh)
            paths = []
            if repo == REPOS[0]:
                files = self.remote.files(repo, number)
                if len(files) != fresh['changed_files']:
                    raise ValueError('changed-file API incomplete or PR drifted')
                paths = runtime_paths(files, self.remote.manifest(repo, head))
                final = self.remote.snapshot(repo, number)
                if final['headRefOid'] != head or final['changed_files'] != len(files):
                    raise ValueError('head changed during runtime selection')
                accepted = admission(final)
        except (ValueError, KeyError, TypeError) as exc:
            return {'repo': repo, 'pr': number, 'status': 'REFUSED', 'reason': str(exc)}
        if dry_run:
            return {'repo': repo, 'pr': number, 'status': 'WOULD_MERGE', 'head': head, 'runtime_paths': paths}
        intent = self.intent_path(pr)
        immutable(intent, {'repo': repo, 'pr': number, 'head': head, 'started': utc(),
                           'issue': accepted['issue'], 'acceptance': accepted, 'runtime_paths': paths})
        receipt = {'schema': 'auto-deployer-v1', 'repo': repo, 'pr': number,
                   'head': head, 'issue': accepted['issue'], 'acceptance': accepted,
                   'status': 'BLOCKED_UNCERTAIN', 'live_output_proven': False}
        try:
            try:
                self.remote.merge(repo, number, head)
            except Exception as exc:
                receipt['merge_ack_error'] = str(exc)[:1000]
            # Whether transport succeeded or failed, inspect exactly once.
            receipt['merge'] = self.remote.merged(repo, number, head)
            receipt['deployment'] = {'status': 'WAITING_REGISTERED_RELEASE', 'release_job': '415ec5eba194'}
            if repo == REPOS[0]:
                checkout = self.checkout(repo, receipt['merge']['merge_commit'], reviewed_head=head, paths=paths) if paths else None
                receipt['deployment'] = self.install(checkout, paths)
            if receipt['deployment']['status'] != 'BLOCKED_UNCERTAIN':
                receipt['status'] = 'WAITING_LIVE_OUTPUT'
        except Exception as exc:
            receipt['reason'] = str(exc)[:1000]
        return self.publish(receipt)


def main(argv=None, *, remote=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Read-only decisions; no locks, receipts, install, merge, or sends')
    args = parser.parse_args(argv)
    remote = remote or GitHub()
    deployer = Deployer(root, remote)

    def cycle():
        blocked = deployer.reconcile(dry_run=args.dry_run)
        if blocked:
            print(json.dumps({'status': 'BLOCKED_UNCERTAIN', 'attempts': blocked}, sort_keys=True))
            return 2
        failure = False
        for repo in REPOS:
            for number in remote.open_prs(repo):
                try:
                    decision = deployer.process(remote.snapshot(repo, number), dry_run=args.dry_run)
                except Exception as exc:
                    decision = {'repo': repo, 'pr': number, 'status': 'ERROR', 'reason': str(exc)[:1000]}
                print(json.dumps(decision, sort_keys=True))
                if decision['status'] in {'ERROR', 'BLOCKED_UNCERTAIN'}:
                    failure = True
                    # Do not install later work across an uncertain effect.
                    break
            if failure:
                break
        return 2 if failure else 0

    if args.dry_run:
        return cycle()
    state = deployer.base / '.state'
    state.mkdir(parents=True, exist_ok=True)
    global LOCK_FD
    with (state / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'BUSY', 'effects': 0}))
            return 0
        LOCK_FD = lock.fileno()
        try:
            return cycle()
        finally:
            LOCK_FD = None


if __name__ == '__main__':
    os.umask(0o077)
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status': 'ERROR', 'error': type(exc).__name__, 'reason': str(exc)[:1000]}))
        raise SystemExit(2)
