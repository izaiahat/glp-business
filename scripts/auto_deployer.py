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
import sqlite3
import subprocess
import tempfile

ROOT = Path('/home/ubuntu/business')
REPOS = ('izaiahat/glp-business', 'izaiahat/glp-site-candidate')
OPS_ALERTS = '1487300132054765629'
MANIFEST = 'ops/rebuild-source-manifest.json'
LOCK_FD = None
SHA = re.compile(r'[0-9a-f]{40}\Z')
NATIVE_REVIEW_DB = Path.home() / '.hermes/state.db'
NATIVE_REVIEW_ROOT = ROOT


def review_field(body, name):
    values = re.findall(r'^' + re.escape(name) + r': ([^\r\n]+)\r?$', body, re.M)
    if len(values) != 1:
        raise ValueError('missing/duplicate review field ' + name)
    return values[0]


def native_review(pr, review):
    """Authenticate runtime-owned writes, not worker names or author-owned files.

    Same SessionDB trust boundary as linear_orchestrator native review custody:
    author/reviewer cannot manufacture messages in their coordinator's session.
    No claim of OS isolation from an actor able to modify the runtime database.
    """
    def require(ok, why):
        if not ok:
            raise ValueError('native review custody: ' + why)

    def artifact(path, digest, reviewer=False):
        pp = PurePosixPath(path)
        require(not pp.is_absolute() and '..' not in pp.parts and str(pp) == path
                and pp.parts[0] == 'lanes' and pp.name.endswith('.json'), 'lanes-only artifact required')
        if reviewer:
            require(pp.name == 'RESULT.json' and pp.parent.name.startswith('RV'), 'RV*/RESULT.json required')
        source = NATIVE_REVIEW_ROOT / pp
        require(not source.is_symlink() and source.resolve().is_relative_to(NATIVE_REVIEW_ROOT.resolve()), 'artifact escape')
        raw = source.read_bytes()
        require(re.fullmatch(r'[a-f0-9]{64}', digest or '')
                and hashlib.sha256(raw).hexdigest() == digest, 'artifact digest changed')
        return source, raw, json.loads(raw)

    body = review['body']
    try:
        receipt_path = review_field(body, 'Receipt')
        receipt_digest = review_field(body, 'Receipt-SHA256')
        cs, cr, custody = artifact(review_field(body, 'Custody'), review_field(body, 'Custody-SHA256'))
        coordinator, author, reviewer = (custody[k] for k in ('coordinator_session_id', 'author_session_id', 'reviewer_session_id'))
        require(all(isinstance(v, str) and v for v in (coordinator, author, reviewer))
                and len({coordinator, author, reviewer}) == 3, 'distinct native sessions required')
        require(review_field(body, 'Reviewer') == reviewer and review_field(body, 'Source-author') == author,
                'body identities do not match authenticated sessions')
        require(custody['receipt'] == receipt_path and custody['receipt_sha256'] == receipt_digest, 'coordinator receipt binding missing')
        rs, rr, accepted = artifact(receipt_path, receipt_digest, reviewer=True)
        src, raw, authored = artifact(custody['author_result'], custody['author_result_sha256'])
        url = f'https://github.com/{pr["repo"]}/pull/{pr["number"]}'
        for record in (custody, accepted, authored):
            require(record['pr'] == url and record['head'] == pr['headRefOid'], 'repo/PR/head binding mismatch')
        require(accepted['verdict'] in {'ACCEPT', 'ACCEPT_SOURCE', 'APPROVE_SOURCE_ONLY'}, 'receipt is not an acceptance')
        with sqlite3.connect(NATIVE_REVIEW_DB.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            require(db.execute('SELECT id FROM sessions WHERE id=?', (coordinator,)).fetchone(), 'coordinator absent')
            for role, sid, source, content in [('coordinator', coordinator, cs, cr), ('author', author, src, raw), ('reviewer', reviewer, rs, rr)]:
                session = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
                require(session is not None, role + ' session missing')
                if role != 'coordinator':
                    require(session['parent_session_id'] == coordinator
                            and json.loads(session['model_config'])['_delegate_from'] == coordinator
                            and session['ended_at'] is not None and session['end_reason'] == 'agent_close',
                            role + ' not a completed delegated child')
                if role == 'reviewer':
                    require(session['model'] == 'gpt-6-astra-900k', 'designated Astra reviewer required')
                verified = False
                for message in db.execute("SELECT * FROM messages WHERE session_id=? AND role='assistant' AND tool_calls IS NOT NULL", (sid,)):
                    for call in json.loads(message['tool_calls']):
                        if call.get('function', {}).get('name') != 'write_file':
                            continue
                        args = json.loads(call['function']['arguments'])
                        if args.get('path') != str(source) or args.get('content', '').encode() != content:
                            continue
                        for result in db.execute("SELECT * FROM messages WHERE session_id=? AND role='tool' AND tool_name='write_file' AND tool_call_id=?", (sid, call['id'])):
                            proof = json.loads(result['content'])
                            if (message['timestamp'] <= result['timestamp']
                                    and (role == 'coordinator' or result['timestamp'] <= session['ended_at'])
                                    and proof.get('verified') is True and proof.get('resolved_path') == str(source.resolve())
                                    and proof.get('bytes_written') == len(content)):
                                verified = True
                require(verified and source.read_bytes() == content, role + ' successful exact-byte runtime write missing')
        return {'receipt': receipt_path, 'sha256': receipt_digest, 'custody': custody}
    except (sqlite3.Error, OSError, KeyError, TypeError, IndexError) as exc:
        raise ValueError('native review custody unavailable: ' + str(exc)) from None


def path_match(value, patterns):
    """GitHub's ordered include/!exclude subset; unknown glob syntax refuses."""
    matched = False
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern or any(c in pattern for c in '[]+{}'):
            raise ValueError('unsupported workflow glob; explicit review required')
        negative = pattern.startswith('!')
        pattern = pattern[1:] if negative else pattern
        pieces, i = [], 0
        while i < len(pattern):
            if pattern[i:i+3] == '**/':
                pieces.append('(?:.*/)?'); i += 3
            elif pattern[i:i+2] == '**':
                pieces.append('.*'); i += 2
            elif pattern[i] == '*':
                pieces.append('[^/]*'); i += 1
            elif pattern[i] == '?':
                pieces.append('[^/]'); i += 1
            else:
                pieces.append(re.escape(pattern[i])); i += 1
        if re.fullmatch(''.join(pieces), value):
            matched = not negative
    return matched


def workflow_jobs(text, files, branch):
    # PyYAML is already part of the registered Hermes interpreter. Missing parser
    # or unsupported dynamic jobs fail closed rather than weaken completeness.
    import yaml
    doc = yaml.safe_load(text)
    events = doc.get('on', doc.get(True))  # YAML 1.1 reads unquoted on as True.
    if isinstance(events, str):
        events = {events: None}
    elif isinstance(events, list):
        events = dict.fromkeys(events)
    if not isinstance(events, dict) or 'pull_request' not in events:
        return []
    trigger = events['pull_request'] or {}
    if set(trigger) - {'branches', 'branches-ignore', 'paths', 'paths-ignore'}:
        raise ValueError('unsupported PR workflow trigger')
    if 'branches' in trigger and not path_match(branch, trigger['branches']):
        return []
    if 'branches-ignore' in trigger and path_match(branch, trigger['branches-ignore']):
        return []
    if 'paths' in trigger and not any(path_match(f, trigger['paths']) for f in files):
        return []
    if 'paths-ignore' in trigger and all(path_match(f, trigger['paths-ignore']) for f in files):
        return []
    jobs = []
    for key, job in doc['jobs'].items():
        name = job.get('name', key)
        if 'strategy' in job or 'if' in job or 'uses' in job or not isinstance(name, str) or '${{' in name:
            raise ValueError('dynamic/conditional workflow job needs explicit CI policy')
        jobs.append(name)
    if not jobs or len(set(jobs)) != len(jobs):
        raise ValueError('empty/ambiguous workflow jobs')
    return jobs


def complete_ci(pr):
    head, checks = pr['headRefOid'], pr['checks']
    if not checks or any(c.get('head_sha') != head or c.get('status') != 'completed'
                         or c.get('conclusion') != 'success' for c in checks):
        raise ValueError('nonempty all-SUCCESS exact-head checks required')
    statuses = pr['statuses']
    status_url = f'https://api.github.com/repos/{pr["repo"]}/statuses/{head}'
    if any(s.get('url') != status_url or s.get('sha', head) != head or s.get('state') != 'success' for s in statuses):
        raise ValueError('all commit-status contexts must be exact-head SUCCESS')
    expected = pr['expected_workflows']
    if not expected:
        raise ValueError('nonempty expected workflow/job set required')
    for workflow in expected:
        runs = [r for r in pr['workflow_runs'] if r.get('path') == workflow['path']
                and r.get('head_sha') == head and r.get('event') == 'pull_request']
        if not runs or any(r.get('status') != 'completed' or r.get('conclusion') != 'success' for r in runs):
            raise ValueError('missing/pending/failed workflow: ' + workflow['path'])
        for run_record in runs:
            names = {c['name'] for c in checks if c.get('app', {}).get('slug') == 'github-actions'
                     and c.get('check_suite', {}).get('id') == run_record['check_suite_id']}
            if not workflow['jobs'] or not set(workflow['jobs']).issubset(names):
                raise ValueError('missing required jobs: ' + workflow['path'])
    # Branch-protection contexts may be checks or legacy commit statuses.
    successful = {c['name'] for c in checks} | {s['context'] for s in pr['statuses']}
    if not set(pr['required_contexts']).issubset(successful):
        raise ValueError('missing branch-required context')


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
    def __init__(self):
        self.workflow_cache = {}
        self.blob_cache = {}

    def expected_workflows(self, repo, base, head, files, branch):
        expected = {}
        for ref in (base, head):
            cache_key = (repo, ref)
            if cache_key not in self.workflow_cache:
                tree = self.api(f'repos/{repo}/git/trees/{ref}?recursive=1')
                if tree.get('truncated'):
                    raise ValueError('workflow tree truncated')
                sources = []
                for entry in tree['tree']:
                    if not re.fullmatch(r'\.github/workflows/[^/]+\.ya?ml', entry['path']):
                        continue
                    if entry['type'] != 'blob' or entry['mode'] != '100644':
                        raise ValueError('nonregular workflow source')
                    blob = (repo, entry['sha'])
                    if blob not in self.blob_cache:
                        self.blob_cache[blob] = base64.b64decode(self.api(f'repos/{repo}/git/blobs/{entry["sha"]}')['content']).decode()
                    sources.append((entry['path'], self.blob_cache[blob]))
                self.workflow_cache[cache_key] = sources
            for path, text in self.workflow_cache[cache_key]:
                jobs = workflow_jobs(text, files, branch)
                if jobs:
                    expected.setdefault(path, set()).update(jobs)
        return [{'path': path, 'jobs': sorted(jobs)} for path, jobs in sorted(expected.items())]

    def required_contexts(self, repo, branch):
        try:
            return self.api(f'repos/{repo}/branches/{branch}/protection/required_status_checks')['contexts']
        except subprocess.CalledProcessError as exc:
            # Only the known unprotected-branch response is absence of policy;
            # transport/auth/other errors must not silently drop required checks.
            try:
                absent = json.loads(exc.stdout)
            except (ValueError, TypeError):
                raise exc
            if absent.get('status') == '404' and absent.get('message') == 'Branch not protected':
                return []
            raise

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
        files = self.files(repo, number)
        if len(files) != p['changed_files']:
            raise ValueError('incomplete changed-file enumeration')
        changed = [f['filename'] for f in files] + [f['previous_filename'] for f in files if 'previous_filename' in f]
        expected = self.expected_workflows(repo, p['base']['sha'], head, changed, p['base']['ref'])
        return {'repo': repo, 'number': number, 'state': p['state'].upper(),
                'baseRefName': p['base']['ref'], 'isDraft': p['draft'],
                'headRefOid': head, 'body': p.get('body') or '', 'changed_files': p['changed_files'],
                'labels': p['labels'], 'mergeStateStatus': view['mergeStateStatus'],
                'headCommittedAt': self.api(f'repos/{repo}/commits/{head}')['commit']['committer']['date'],
                'reviews': self.pages(f'repos/{repo}/pulls/{number}/reviews?per_page=100'),
                'checks': self.pages(f'repos/{repo}/commits/{head}/check-runs?filter=all&per_page=100', 'check_runs'),
                'statuses': self.pages(f'repos/{repo}/commits/{head}/statuses?per_page=100'),
                'workflow_runs': self.pages(f'repos/{repo}/actions/runs?head_sha={head}&event=pull_request&per_page=100', 'workflow_runs'),
                'expected_workflows': expected,
                'required_contexts': self.required_contexts(repo, p['base']['ref'])}

    def files(self, repo, number):
        return self.pages(f'repos/{repo}/pulls/{number}/files?per_page=100')

    def manifest(self, repo, head):
        content = self.api(f'repos/{repo}/contents/{MANIFEST}?ref={head}')
        return json.loads(base64.b64decode(content['content']))

    def installed_hashes(self, repo, merge, paths):
        """Fetch regular blobs by immutable merge-tree OID, not mutable main."""
        tree = self.api(f'repos/{repo}/git/trees/{merge}?recursive=1')
        if tree.get('truncated') is not False:
            raise ValueError('recovery tree incomplete')
        entries = {entry['path']: entry for entry in tree['tree']}
        result = {}
        for path in paths:
            entry = entries.get(path)
            if not entry or entry['type'] != 'blob' or entry['mode'] not in {'100644', '100755'}:
                raise ValueError('recovery tree path missing/nonregular: ' + path)
            oid = entry['sha']
            if not SHA.fullmatch(oid):
                raise ValueError('invalid recovery blob OID')
            blob = self.api(f'repos/{repo}/git/blobs/{oid}')
            if blob['sha'] != oid or blob['encoding'] != 'base64':
                raise ValueError('invalid recovery blob response')
            raw = base64.b64decode(''.join(blob['content'].split()), validate=True)
            actual_oid = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
            if len(raw) != blob['size'] or actual_oid != oid:
                raise ValueError('incomplete/mismatched recovery blob')
            result[path] = hashlib.sha256(raw).hexdigest()
        return result

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
    """Require complete CI and runtime-authenticated non-author review custody."""
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
    complete_ci(pr)
    checks = pr['checks']
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
    if (review_field(body, 'Head') != head or review_field(body, 'Independent') != 'true'
            or review_field(body, 'Non-author') != 'true'):
        raise ValueError('independent non-author exact-head attestation required')
    custody = native_review(pr, review)
    return {'issue': tickets[0], 'review': review, 'checks': checks, 'custody': custody,
            'statuses': pr['statuses'], 'expected_workflows': pr['expected_workflows'],
            'workflow_runs': pr['workflow_runs'], 'required_contexts': pr['required_contexts']}


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

    def recovery_path(self, receipt):
        repo, number, head = receipt['repo'], receipt['pr'], receipt['head']
        if repo not in REPOS or type(number) is not int or number < 1 or not SHA.fullmatch(head):
            raise ValueError('invalid recovery identity')
        return self.base / '.state/recoveries' / f'{repo.split("/")[1]}-{number}-{head}.json'

    def recovery_bytes(self, path):
        """Only canonical, regular, in-root files; reject symlink parents too."""
        pp = PurePosixPath(path)
        if not pp.parts or pp.is_absolute() or '..' in pp.parts or str(pp) != path:
            raise ValueError('unsafe recovery path')
        target = self.root
        for part in pp.parts:
            target = target / part
            if target.is_symlink():
                raise ValueError('recovery symlink refused')
        if not target.resolve().is_relative_to(self.root.resolve()) or not target.is_file():
            raise ValueError('recovery file missing/nonregular')
        return target.read_bytes()

    def verify_recovery(self, path, receipt, recovery):
        """Read-only reconciliation, never merge/apply. All evidence is required."""
        def require(ok, why):
            if not ok:
                raise ValueError('recovery: ' + why)

        self.recovery_path(receipt)  # Validate identity before remote/path use.
        require(receipt['status'] == 'BLOCKED_UNCERTAIN', 'original is not blocked')
        require(recovery['schema'] == 'auto-deployer-recovery-v1', 'unknown schema')
        require(all(recovery[k] == receipt[k] for k in ('repo', 'pr', 'head')), 'identity mismatch')
        original_path = str(path.relative_to(self.root))
        require(recovery['original_receipt'] == original_path, 'original receipt path mismatch')
        raw = self.recovery_bytes(original_path)
        require(hashlib.sha256(raw).hexdigest() == recovery['original_receipt_sha256']
                and json.loads(raw) == receipt, 'original receipt digest/content mismatch')
        merge = recovery['merge_commit']
        require(isinstance(merge, str) and SHA.fullmatch(merge), 'invalid merge commit')
        recorded_merge = receipt.get('merge', receipt.get('inspection', {})).get('merge_commit')
        require(recorded_merge is None or recorded_merge == merge, 'original merge mismatch')
        authority_path = recovery['authority']
        require(isinstance(authority_path, str) and authority_path.startswith('lanes/')
                and authority_path.endswith('.json'), 'lanes authority file required')
        authority_raw = self.recovery_bytes(authority_path)
        require(hashlib.sha256(authority_raw).hexdigest() == recovery['authority_sha256'], 'authority digest mismatch')
        authority = json.loads(authority_raw)
        require(authority['original_receipt'] == original_path and authority['merge'] == merge
                and authority['authority'] and authority['scope'], 'authority binding missing')
        installed = recovery['installed_sha256']
        require(isinstance(installed, dict) and installed, 'nonempty installed map required')
        expected = receipt.get('deployment', {}).get('paths', receipt.get('runtime_paths'))
        # Receipts created before checkout/apply can get their scope from the
        # retained exact-head intent; absence never authorizes a partial map.
        if expected is None:
            intent = self.intent_path({'repo': receipt['repo'], 'number': receipt['pr'], 'headRefOid': receipt['head']})
            item = json.loads(self.recovery_bytes(str(intent.relative_to(self.root))))
            require(all(item[k] == receipt[k] for k in ('repo', 'pr', 'head')), 'intent identity mismatch')
            expected = item['runtime_paths']
        require(isinstance(expected, list) and len(expected) == len(set(expected))
                and set(installed) == set(expected), 'incomplete installed path scope')
        for name, digest in installed.items():
            require(isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest), 'invalid installed digest')
            require(hashlib.sha256(self.recovery_bytes(name)).hexdigest() == digest, 'live bytes drifted: ' + name)
        observed = self.remote.merged(receipt['repo'], receipt['pr'], receipt['head'])
        require(observed['merge_commit'] == merge and observed['head'] == receipt['head'], 'remote merge mismatch')
        require(self.remote.installed_hashes(receipt['repo'], merge, sorted(installed)) == installed,
                'merge-tree bytes mismatch')

    def record_recovery(self, proposal):
        """Coordinator-only bookkeeping under run.lock; no source/outbox effects."""
        proposal = Path(proposal)
        if not proposal.is_absolute():
            proposal = self.root / proposal
        recovery = json.loads(self.recovery_bytes(str(proposal.relative_to(self.root))))
        matches = [(path, receipt) for path, receipt in self.receipts()
                   if str(path.relative_to(self.root)) == recovery['original_receipt']]
        if len(matches) != 1:
            raise ValueError('recovery must name one retained historical receipt')
        path, receipt = matches[0]
        self.verify_recovery(path, receipt, recovery)
        destination = self.recovery_path(receipt)
        if any(p.is_symlink() for p in (destination, *destination.parents) if p != self.root.parent):
            raise ValueError('recovery destination symlink refused')
        immutable(destination, recovery)
        return {'status': 'RECOVERY_RECORDED', 'recovery': str(destination.relative_to(self.root))}

    def recovered(self, path, receipt):
        try:
            recovery_path = self.recovery_path(receipt)
            recovery = json.loads(self.recovery_bytes(str(recovery_path.relative_to(self.root))))
            self.verify_recovery(path, receipt, recovery)
            return True
        except Exception:
            # Missing, unreadable, malformed or stale local/remote evidence is
            # still uncertainty. Never alter the historical receipt to clear it.
            return False

    def reconcile(self, dry_run=False):
        """Inspect unfinished attempts, including PRs no longer open. No replay."""
        receipts = self.receipts()
        if not dry_run:
            for path, receipt in receipts:
                self.deliver(path, receipt)
        known = {(r['repo'], r['pr'], r['head']) for _, r in receipts}
        blocked = [r for path, r in receipts if r['status'] == 'BLOCKED_UNCERTAIN'
                   and not self.recovered(path, r)]
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Read-only decisions; no locks, receipts, install, merge, or sends')
    mode.add_argument('--record-recovery', metavar='JSON', help='Coordinator exact recovery only: verify and exclusively record; no merge/install/send')
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
            if args.record_recovery:
                print(json.dumps(deployer.record_recovery(args.record_recovery), sort_keys=True))
                return 0
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
