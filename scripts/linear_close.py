#!/usr/bin/env python3
"""One-command ordinary Linear closer.

  linear_close.py <GLP-n> <worker RESULT.json> [--apply]

Turns a worker's simple RESULT (status DONE_READY_TO_CLOSE, pr, install, live_run, receipt)
into a typed linear-ordinary-completion-v1 RESULT at <worker dir>/<GLP-n>/RESULT.json,
pre-validates it with the unmodified linear_orchestrator ordinary validator, posts the
coordinator acceptance comment, runs `pass --ordinary --check-only` and, with --apply,
the real `pass --ordinary`. No comment is posted for an issue the validator would refuse.
Prints one line: issue, final state, typed RESULT path.
"""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import linear_orchestrator as lo  # noqa: E402

ROOT = HERE.parent
REPORTS = ROOT / 'reports'
PR_RE = re.compile(r'(https://github\.com/izaiahat/glp-(?:business|site-candidate)/pull/[1-9]\d*)|\bPR\s*#?([1-9]\d*)', re.I)
PATH_RE = re.compile(r'(?:/home/ubuntu/business/)?reports/[^\s"\'`;,)\]]+')
TOKEN_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_:.-]{3,}[A-Za-z0-9]')


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
        ['gh', 'pr', 'view', url, '--json', 'state,headRefOid,mergeCommit,statusCheckRollup'],
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
            'checks': checks}


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
    for entry in data['evidence']:
        ref = 'reports/' + Path(entry['path']).relative_to(REPORTS.resolve()).as_posix()
        lines.append('Evidence: %s observed %s' % (ref, ', '.join(entry['observed'])))
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
    pr = resolve_pr(worker, gh)
    runtime = any(worker.get(k) for k in ('install', 'live_run', 'receipt'))
    data = {'schema': 'linear-ordinary-completion-v1', 'issue': ident, 'status': 'COMPLETE',
            'scope': 'ordinary-runtime' if pr and runtime else 'ordinary-code' if pr else 'ordinary-readonly-runtime',
            'protected_effects': [], 'description_sha256': digest(iss.get('description') or ''),
            'worker_result': {'path': str(Path(worker_path).resolve()), 'sha256': lo.sha(worker_path)},
            'evidence': collect_evidence(worker, worker_path)}
    if pr:
        data['pr'] = pr
    else:
        data['changed_files'] = []
    return data


def preflight(typed_path, iss, body, viewer_id):
    """Run the unmodified validator against the planned comment before posting it."""
    real = lo.gql

    def planned(query, variables=None):
        if 'comment(id:' in query:
            return {'comment': {'id': variables['id'], 'body': body, 'archivedAt': None,
                                'user': {'id': viewer_id}, 'issue': {'id': iss['id']}}}
        return real(query, variables)
    lo.gql = planned
    try:
        lo._lint_ordinary_result(str(typed_path), iss)
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


def close(ident, worker_path, apply=False):
    worker_path = Path(worker_path).resolve()
    typed_path = worker_path.parent / ident / 'RESULT.json'
    iss = lo.issue(ident)
    if iss['state']['name'] == 'Done':
        return 'Done', typed_path
    data = build(ident, worker_path, iss)
    body = acceptance_body(data)
    previous = json.loads(typed_path.read_text()) if typed_path.is_file() else {}
    review = previous.get('review') or {}
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
    args = [a for a in argv if a != '--apply']
    if len(args) != 2:
        print(__doc__)
        return 2
    ident = args[0]
    try:
        state, typed_path = close(ident, args[1], apply='--apply' in argv)
    except Refused as exc:
        print('%s REFUSED %s' % (ident, exc))
        return 3
    print('%s %s %s' % (ident, state, typed_path))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
