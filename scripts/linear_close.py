#!/usr/bin/env python3
"""One-command ordinary Linear closer.

  linear_close.py <GLP-n> <worker RESULT.json> [--apply | --check-only]

--check-only evaluates the same gate in memory, prints PASS/REFUSED and performs
no filesystem or Linear writes. PASS means prevalidated for coordinator acceptance,
not an authenticated acceptance or permission to close. Already-Done issues refuse.
The legacy default prepares/posts acceptance; it is NOT a no-write dry run.

Turns a worker's simple RESULT (status DONE_READY_TO_CLOSE, pr, install, live_run, receipt)
into a typed linear-ordinary-completion-v1 RESULT at <worker dir>/<GLP-n>/RESULT.json,
pre-validates it with the unmodified linear_orchestrator ordinary validator, posts the
coordinator acceptance comment, runs `pass --ordinary --check-only` and, with --apply,
the real `pass --ordinary`. No comment is posted for an issue the validator would refuse.
Prints one line: issue, final state, typed RESULT path.

Protected outcomes retain the typed protected validator. For shared Linear accounts,
capture authenticated operator Discord acceptance with linear_operator_acceptance.py
and use `linear_orchestrator.py pass ... --protected-noncode|--protected-runtime`.
See ops/linear/OPERATOR-DISCORD-ACCEPTANCE.md. This ordinary closer never infers it.
"""
import sys
# --check-only includes import-time artifacts: do not create __pycache__ files.
sys.dont_write_bytecode = True
import hashlib
import json
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import linear_orchestrator as lo  # noqa: E402

ROOT = HERE.parent
REPORTS = ROOT / 'reports'
PR_RE = re.compile(r'(https://github\.com/izaiahat/glp-(?:business|site-candidate)/pull/[1-9]\d*)|\bPR\s*#?([1-9]\d*)', re.I)
PATH_RE = re.compile(r'(?:/home/ubuntu/business/)?reports/[^\s"\'`;,)\]]+')
SOURCE_RE = re.compile(r'(?<![\w/.-])(?:/home/ubuntu/business/)?((?:scripts|systems|ops)/[A-Za-z0-9_./-]*[A-Za-z0-9_])')
STALE_SECONDS = 3000  # re-post acceptance well inside the validator's same-hour window
TOKEN_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_:.-]{3,}[A-Za-z0-9]')
COUNT_RE = re.compile(r'"[a-z0-9_]*(?:rows|count|pages|urls|programs|offers|published|written|updated|'
                      r'created|records|items|guides|articles)[a-z0-9_]*": ?[1-9]\d*')


class Refused(Exception):
    pass


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


def gh_pr(url):
    return json.loads(subprocess.check_output(
        ['gh', 'pr', 'view', url, '--json', 'state,headRefOid,mergeCommit,statusCheckRollup,files'],
        text=True, timeout=60))


def resolve_pr(worker, gh=gh_pr):
    raw = worker.get('pr')
    if not raw or str(raw).strip().lower() in ('none', 'n/a', 'null'):
        return None
    match = PR_RE.search(str(raw))
    if not match:
        raise Refused('worker pr unresolvable: ' + str(raw)[:80])
    url = match.group(1) or 'https://github.com/izaiahat/glp-business/pull/' + match.group(2)
    remote = gh(url)
    if remote.get('state') != 'MERGED' or not (remote.get('mergeCommit') or {}).get('oid'):
        raise Refused(url + ' is not merged')
    checks = [{'name': c['name'], 'url': c['detailsUrl']} for c in remote.get('statusCheckRollup') or []
              if c.get('name') and c.get('detailsUrl')]
    return {'url': url, 'head_sha': remote['headRefOid'], 'merge_sha': remote['mergeCommit']['oid'],
            'checks': checks, 'files': [f['path'] for f in remote.get('files') or [] if f.get('path')]}


def installed_files(worker, pr_files, pr=None, gh=gh_pr):
    """R2 inputs: changed paths, or unchanged blobs carried from a merged source PR.

    A zero-file carry can name install={source_pr: URL, paths: [...]}.
    Authenticate that source's merged ancestry and each selected Git blob at the
    original source head, CI head and receiving merge; prose is not provenance.
    The ordinary validator still enforces exact-head CI and live merge bytes.
    """
    text = '\n'.join(strings({k: worker.get(k) for k in ('install', 'install_readback')}))
    source = None
    install = worker.get('install')
    if not pr_files and pr and isinstance(install, dict) and install.get('source_pr'):
        source = resolve_pr({'pr': install['source_pr']}, gh)
        if not source or source['url'].rsplit('/pull/', 1)[0] != pr['url'].rsplit('/pull/', 1)[0]:
            raise Refused('unchanged installed source requires a merged source PR in the same repository')
        repo = pr['url'].split('github.com/', 1)[1].split('/pull/', 1)[0]

        def api(endpoint):
            return json.loads(subprocess.check_output(
                ['gh', 'api', 'repos/' + repo + '/' + endpoint], text=True, timeout=60))

        # The source was actually merged into the tested head, and that exact
        # head was received by the merge. Later main changes need not share its
        # whole tree, but may not alter any selected installed blob.
        for base, head in ((source['merge_sha'], pr['head_sha']), (pr['head_sha'], pr['merge_sha'])):
            if not all(re.fullmatch(r'[a-f0-9]{40}', sha) for sha in (base, head)) or \
                    (api('compare/' + base + '...' + head).get('merge_base_commit') or {}).get('sha') != base:
                raise Refused('unchanged installed source lacks authenticated merged source ancestry')
        pr_files = source['files']
    out = []
    for rel in dict.fromkeys(SOURCE_RE.findall(text)):
        path = ROOT / rel
        if source and rel not in pr_files:
            raise Refused('merged source PR did not change installed path: ' + rel)
        if rel in pr_files and path.is_file() and not path.is_symlink():
            if source:
                raw = path.read_bytes()
                blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
                for ref in (source['head_sha'], pr['head_sha'], pr['merge_sha']):
                    remote = api('contents/' + rel + '?ref=' + ref)
                    if remote.get('type') != 'file' or remote.get('sha') != blob:
                        raise Refused('installed Git blob differs from source/CI head/merge: ' + rel)
            out.append({'path': str(path.resolve()), 'sha256': lo.sha(path)})
    return out


def first_output(data):
    """Observed runtime values that are business output, not exit/HTTP/ok tokens or field names."""
    values = [value for entry in data['evidence'] for value in entry['observed'] if not lo.is_bare_output(value)]
    return ', '.join(dict.fromkeys(values)) or None


def collect_evidence(worker, worker_path):
    text = '\n'.join(strings(worker))
    tokens = [t for t in dict.fromkeys(TOKEN_RE.findall(text)) if re.search(r'\d|_', t)]
    evidence = []
    for ref in dict.fromkeys(PATH_RE.findall(text)):
        path = (ROOT / ref.replace('/home/ubuntu/business/', '', 1)).resolve()
        if not path.is_file() or path == Path(worker_path).resolve() or not path.is_relative_to(REPORTS.resolve()):
            continue
        raw = path.read_bytes()
        decoded = raw.decode(errors='replace')
        observed = [t for t in tokens if t in decoded and t not in ref][:8]
        # Business counts in the evidence itself ("provider_rows": 58) are output the worker
        # text rarely repeats verbatim; observe them exactly as written.
        observed += [m for m in dict.fromkeys(COUNT_RE.findall(decoded)) if m not in observed][:8]
        if not observed:
            raise Refused('no worker-cited identifier found in ' + ref)
        evidence.append({'kind': 'runtime', 'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
                         'observed': observed})
    if not evidence:
        raise Refused('worker RESULT cites no existing reports evidence file')
    return evidence


def acceptance_body(data):
    lines = []
    if 'pr' in data:
        pr = data['pr']
        lines.append('Coordinator acceptance verified: %s merged at %s; exact-head checks passed: %s.'
                     % (pr['url'], pr['merge_sha'], ', '.join(sorted({c['name'] for c in pr['checks']}))))
    else:
        lines.append('Coordinator acceptance verified: read-only runtime outcome.')
    lines.append('Accepted description SHA256: ' + data['description_sha256'])
    for entry in data.get('installed_files') or []:
        lines.append('Installed: %s %s' % (Path(entry['path']).relative_to(ROOT.resolve()).as_posix(),
                                           entry['sha256']))
    if 'independent_health_review' in data:
        lines.append('Independent article acceptance SHA256: ' + digest(json.dumps(
            data['independent_health_review'], sort_keys=True, separators=(',', ':'))))
    for entry in data['evidence']:
        ref = 'reports/' + Path(entry['path']).relative_to(REPORTS.resolve()).as_posix()
        lines.append('Evidence: %s observed %s' % (ref, ', '.join(entry['observed'])))
    output = first_output(data)
    if output:
        lines.append('Output: ' + output)
    return '\n'.join(lines)


def build(ident, worker_path, iss, gh=gh_pr):
    """Typed RESULT without review; raises Refused when the worker claim cannot be closed."""
    worker = json.loads(Path(worker_path).read_text())
    if worker.get('status') != 'DONE_READY_TO_CLOSE':
        raise Refused('worker status is %r' % worker.get('status'))
    named = worker.get('issues') or [worker.get('issue')]
    if ident not in named:
        raise Refused('worker RESULT does not name ' + ident)
    for field in ('named_waits', 'unverified', 'blocked'):
        if re.search(r'\b' + re.escape(ident) + r'\b', json.dumps(worker.get(field, ''))):
            raise Refused('worker RESULT %s names %s as still open' % (field, ident))
    effect = lo.protected_effect((iss.get('title') or '') + '\n' + lo.acceptance_section(iss.get('description')))
    independent = None
    if effect == 'health-claim-publish':
        from article_health_acceptance import acceptance
        try:
            independent = acceptance(worker.get('receipt') or '',
                natural=bool(re.search(r'\bnatural\b', (iss.get('title') or '') + '\n' +
                    lo.acceptance_section(iss.get('description')), re.I)), root=ROOT)
        except (ValueError, KeyError, OSError, RuntimeError) as exc:
            raise Refused(str(exc)) from None
    elif effect:
        raise Refused('protected effect %s: needs independent acceptance via '
                      'linear_orchestrator.py pass %s <RESULT> --protected-runtime '
                      '(authenticated operator Discord path: ops/linear/OPERATOR-DISCORD-ACCEPTANCE.md)' % (effect, ident))
    pr = resolve_pr(worker, gh)
    runtime = any(worker.get(k) for k in ('install', 'live_run', 'receipt'))
    data = {'schema': 'linear-ordinary-completion-v1', 'issue': ident, 'status': 'COMPLETE',
            'scope': 'ordinary-runtime' if pr and runtime else 'ordinary-code' if pr else 'ordinary-readonly-runtime',
            'protected_effects': [], 'description_sha256': digest(iss.get('description') or ''),
            'worker_result': {'path': str(Path(worker_path).resolve()), 'sha256': lo.sha(worker_path)},
            'evidence': collect_evidence(worker, worker_path)}
    if independent is not None:
        if not pr:
            raise Refused('published health completion requires merged source provenance')
        data.update(schema='linear-reviewed-health-completion-v1', scope='reviewed-health-runtime',
                    protected_effects=['health-claim-publish'], independent_health_review=independent)
    if pr:
        files = pr.pop('files', [])
        data['pr'] = pr
        installed = installed_files(worker, files, pr, gh)
        if installed:
            data['installed_files'] = installed
        elif data['scope'] in ('ordinary-runtime', 'reviewed-health-runtime') and pr['url'].startswith('https://github.com/izaiahat/glp-business/'):
            raise Refused('runtime scope but no installed scripts/systems/ops path from the merged PR is named '
                          'in worker install/install_readback')
    else:
        data['changed_files'] = []
    if not first_output(data):
        raise Refused('evidence carries only exit/HTTP/ok tokens; no business output observed')
    return data


def preflight(typed_path, iss, body, viewer_id, data=None):
    """Run the unmodified validator against the planned comment before posting it."""
    real = lo.gql

    def planned(query, variables=None):
        if 'comment(id:' in query:
            from datetime import datetime, timezone
            return {'comment': {'id': variables['id'], 'body': body, 'archivedAt': None,
                                'createdAt': datetime.now(timezone.utc).isoformat(),
                                'user': {'id': viewer_id}, 'issue': {'id': iss['id']}}}
        return real(query, variables)
    lo.gql = planned
    try:
        lo._lint_ordinary_result(str(typed_path), iss, result_data=data)
    except SystemExit as exc:
        raise Refused(str(exc)) from None
    finally:
        lo.gql = real


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1) + '\n')


def orchestrate(ident, typed_path, *extra):
    run = subprocess.run([sys.executable, str(HERE / 'linear_orchestrator.py'), 'pass', ident, str(typed_path),
                          '--ordinary', *extra], text=True, capture_output=True, timeout=300)
    if run.returncode:
        raise Refused(((run.stderr or run.stdout).strip().splitlines() or ['pass failed'])[-1])
    return run.stdout


def typed_destination(ident, worker_path):
    """One stable reports-only destination per exact issue and physical worker.

    Identity is the workspace-relative worker path, not mutable worker bytes.
    Resolve and check even during preflight, without creating directories/files.
    """
    if not isinstance(ident, str) or not re.fullmatch(r'GLP-[0-9]+', ident):
        raise Refused('exact GLP issue required')
    worker = Path(worker_path).resolve()
    if not worker.is_relative_to(ROOT.resolve()):
        raise Refused('worker RESULT outside workspace')
    identity = hashlib.sha256(str(worker.relative_to(ROOT.resolve())).encode()).hexdigest()
    destination = REPORTS.resolve() / 'ops/linear-close' / ident / identity / 'RESULT.json'
    if destination.resolve() != destination or destination == worker:
        raise Refused('typed RESULT destination must stay under reports and not overwrite worker')
    return destination


def close(ident, worker_path, apply=False, check_only=False):
    if apply and check_only:
        raise Refused('--apply and --check-only are mutually exclusive')
    worker_path = Path(worker_path).resolve()
    typed_path = typed_destination(ident, worker_path)
    iss = lo.issue(ident)
    if check_only and iss['state']['name'] == 'Done':
        raise Refused('issue is already Done; not an OPEN completion')
    if iss['state']['name'] == 'Done':
        return 'Done', typed_path
    data = build(ident, worker_path, iss)
    body = acceptance_body(data)
    if check_only:
        data['review'] = {'comment_id': 'planned', 'body_sha256': digest(body)}
        preflight(typed_path, iss, body, lo.gql('query{viewer{id}}')['viewer']['id'], data=data)
        return 'PASS', typed_path
    previous = json.loads(typed_path.read_text()) if typed_path.is_file() else {}
    review = previous.get('review') or {}
    if review.get('body_sha256') == digest(body) and review.get('comment_id') not in (None, 'planned'):
        row = lo.gql('query($id:String!){comment(id:$id){createdAt}}', {'id': review['comment_id']})['comment']
        from datetime import datetime, timezone
        created = datetime.fromisoformat(((row or {}).get('createdAt') or '1970-01-01T00:00:00+00:00')
                                         .replace('Z', '+00:00'))
        if (datetime.now(timezone.utc) - created).total_seconds() > STALE_SECONDS:
            review = {}  # Done must follow the coordinator check within the hour; check again.
    if review.get('body_sha256') != digest(body) or review.get('comment_id') in (None, 'planned'):
        data['review'] = {'comment_id': 'planned', 'body_sha256': digest(body)}
        write(typed_path, data)
        preflight(typed_path, iss, body, lo.gql('query{viewer{id}}')['viewer']['id'])
        posted = lo.comment(iss['id'], body)
        if not posted.get('success') or not (posted.get('comment') or {}).get('id'):
            raise Refused('acceptance comment unconfirmed; inspect the issue before any retry')
        review = {'comment_id': posted['comment']['id'], 'body_sha256': digest(body)}
    data['review'] = review
    write(typed_path, data)
    orchestrate(ident, typed_path, '--check-only')
    if apply:
        orchestrate(ident, typed_path)
    return lo.issue(ident)['state']['name'], typed_path


def main(argv):
    args = [a for a in argv if a not in ('--apply', '--check-only')]
    if len(args) != 2 or any(a.startswith('--') for a in args):
        print(__doc__)
        return 2
    ident = args[0]
    try:
        state, typed_path = close(ident, args[1], apply='--apply' in argv, check_only='--check-only' in argv)
    except (Refused, OSError, ValueError) as exc:
        print('%s REFUSED %s' % (ident, exc))
        return 3
    print('%s PASS gate=prevalidated coordinator-acceptance=required writes=0' % ident
          if state == 'PASS' else '%s %s %s' % (ident, state, typed_path))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
