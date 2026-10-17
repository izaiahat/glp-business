#!/usr/bin/env python3
"""Orchestrator-only Linear transitions with native history (operator key). Usage:
  linear_orchestrator.py transition <GLP-n> <StateName> <abs RESULT.json path>
  linear_orchestrator.py pass <GLP-n> <abs RESULT.json path> --brief <scope brief>  # legacy/protected contract
  linear_orchestrator.py pass <GLP-n> <abs RESULT.json path> --protected-noncode [--check-only]
        Existing typed RESULT, scope protected-noncode, changed_files/protected_effects [], no pr/brief.
        Retained runtime evidence uses {kind,path,sha256,observed}; artifacts_created must be
        RESULT or that evidence. review/protected_review bind authenticated comments on exact issue.
        Both bodies bind Accepted description SHA256, Accepted protected evidence SHA256
        (canonical JSON sort_keys=True,separators=(',',':'), excluding review/protected_review),
        and Execution author Linear ID. Parent additionally binds Independent protected acceptance:
        <comment_id> <body_sha256>. Independent user differs from parent and attested author.
        Prefixes: Acceptance verified by parent: (or Coordinator acceptance verified:) and
        Independent protected acceptance verified:. No new effects or weakened REAL/send gates.
        Quote complete nested Acceptance/Relevant tool wiring requirements in acceptance_clauses
        or retained_acceptance_clauses, each PASS with evidence; typed first-section lint remains.
        PR placeholders and Authority/Plan path metadata are not acceptance clauses.
        Installed-source/deployment acceptance requires --protected-runtime provenance.
  linear_orchestrator.py pass <GLP-n> <abs RESULT.json path> --protected-runtime [--check-only]
        Same authenticated/full-scope bindings, plus ordinary PR/CI format and
        installed_files [{path,sha256}] matching current live bytes AND authenticated merged
        glp-business source. Evidence may be check/runtime, with at least one runtime receipt.
        No source authoring, reinstallation, job/send execution or effect approval occurs here.
        Approved GLP109 two-account exception: protected_review.native binds coordinator_session_id
        and author/reviewer {session_id,path,sha256,write_message_id,tool_result_message_id}.
        Runtime SessionDB parent/_delegate_from and successful exact write_file bytes authenticate
        distinct completed author/reviewer/coordinator sessions and designated Astra acceptance.
        The non-author operator adopts that review in one comment and accepts in a different comment;
        both bind Native independent review SHA256: <canonical native object SHA256>. No agent
        impersonation, third account, file-only identity or native fallback on missing rows.
  linear_orchestrator.py pass <GLP-n> <abs RESULT.json path> --ordinary [--check-only]
        Ordinary completion: linear-ordinary-completion-v1, COMPLETE, exact issue,
        scope ordinary-code|ordinary-runtime|ordinary-readonly-runtime, protected_effects [],
        description_sha256, review {comment_id,body_sha256},
        evidence [{kind:check|runtime,path,sha256,observed:[exact identifiers/output]}].
        Code/installed runtime additionally requires pr {url,head_sha,merge_sha,checks:[{name,url}]}.
        Read-only non-code runtime instead requires explicit read-only issue acceptance,
        no pr or changed_files, and only runtime artifacts named in the existing authenticated
        coordinator comment (Coordinator acceptance verified: or Acceptance verified by parent:).
        It proves no source change, installation, publication, application or send.
        Uses current scope and physical evidence, not a fabricated PR or retrospective brief.
        --check-only writes nothing.
        No release, outward design, financial REAL or third-party-send approval is granted.
  linear_orchestrator.py triage <GLP-n> admitted "<reason/evidence>" --root <GLP-current-root> --basis <operator-priority|escaped-safety-revenue-defect|current-acceptance-dependency> --project "<name>" --type <task|defect|incident|decision|idea> [--priority N] [--blocks GLP-m]
        Native Triage candidates become direct Todo children. Dependency basis requires --blocks.
        Already-planned Backlog placement cleanup still accepts --project/--type without --root/--basis.
  linear_orchestrator.py triage <GLP-n> <deferred|rejected|duplicate|needs-input> "<reason>" [--state Canceled]
        Deferred/needs-input candidates remain Triage, regardless of project/labels.
        (accept|decline|defer still work; they are normalised to admitted|rejected|deferred)
  linear_orchestrator.py rework <GLP-n> <brief|machine|supply> "<note>"   # REWORK comment + the rework:<cause> label
  linear_orchestrator.py assign <GLP-n> <operator|lane:<name>|none> "<why>"  # native assignee change
  linear_orchestrator.py promote <GLP-n> "<why>" --root <GLP-active-root> --blocks GLP-m  # one shared-cause repair
        The original promote <GLP-n> <GLP-active-root> "<why>" form is also accepted.
        New active roots require owner-surface:<surface>; at most 3 may be active.
        Owners keep surfaces disjoint and use the existing candidate build lock.
  linear_orchestrator.py resolve-request <GLP-n>            # close the fable-owned REQUEST bound to a dispositioned triage issue
  linear_orchestrator.py relabel <GLP-n> lane:<name> "<why>"  # native lane-label change
  linear_orchestrator.py rule <GLP-n> "<ruling>"              # one plain reason + closes the bound fable-owned REQUEST
Done retains its exact RESULT receipt before the state change; metadata uses native history.

Ordinary ideas stay in the existing triage journal. Admission is an orchestrator judgment:
explicit operator priority, an escaped safety/revenue defect needing independent work, or
an actually failing acceptance command whose repair is outside the current named ownership.
Group a shared cause into one repair, not one issue per symptom. Legal/privacy/security
incidents must never be suppressed to meet throughput targets. No text classifier grants admission.

An admission is ONE operation: it strips the `triage` label, sets the real project, adds the `type:` label and
sets a priority. Half-admitting (state moved, project still "Triage", `triage` label kept) is what produced
GLP-148, GLP-161 and GLP-163; project "Triage" and the `triage` label are legal only while the state is Triage."""
from contextlib import contextmanager, nullcontext
import fcntl
import json, hashlib, os, sys, hashlib, urllib.request
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT_ACTIVATION_LOCK = Path(__file__).resolve().parents[1] / 'ops/linear/locks/root-activation.lock'

@contextmanager
def root_activation_lock():
    """Serialize fresh root-count check with the activating state mutation."""
    ROOT_ACTIVATION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(ROOT_ACTIVATION_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
REPIN = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'linear_config_apply.py'), '--views-only']
def inflation_warning(root_identifier=None):
    """Rule 55: a root above 20% inflation is absorbing its discoveries instead of closing.

    Printed after every top-level transition so the orchestrator sees it at the moment it can act
    (re-cut and move the absorbed children to the roots that own their surface)."""
    try:
        q = ('{issues(first:100,filter:{parent:{id:{eq:"%s"}}}){nodes{identifier state{name} createdAt}}}'
             % root_identifier)
        rows = gql(q)['issues']['nodes']
        if len(rows) < 5:
            return
        started = [r for r in rows if r['state']['name'] not in ('Todo', 'Backlog', 'Triage')]
        if not started:
            return
        first = min(r['createdAt'] for r in rows)
        late = [r for r in rows if r['createdAt'] > first and r['state']['name'] not in ('Done', 'Canceled')]
        ratio = len(late) / max(1, len(rows))
        if ratio > 0.20:
            print('ROOT-INFLATION %.0f%% (%d of %d children filed after this root opened and still open) — '
                  'rule 55: promote only what breaks THIS root\'s verify; move the rest to the root that owns '
                  'the surface' % (ratio * 100, len(late), len(rows)), file=sys.stderr)
    except Exception:
        pass


def repin_views():
    """Re-pin the operator dashboard to the live active root. Best effort: never blocks a transition."""
    import subprocess
    try: subprocess.run(REPIN, env=dict(os.environ, LINEAR_ROLE='orchestrator'), capture_output=True, timeout=180)
    except Exception as error: print('views not repinned: %s' % error, file=sys.stderr)
def receipt_validates(issue_id, result_path):
    """True when linear_next.pass_receipt() would accept this closure - the same reader the selector uses.

    Checking the reader directly, instead of guessing at one failure mode, catches both a result the lane
    rewrote after close and a receipt posted outside the window because the issue was already Done.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import linear_next as ln
        c = ln.ReadOnlyClient()
        row = c.gql('query($id:String!){issue(id:$id){id identifier createdAt updatedAt trashed state{name type}}}',
                    {'id': issue_id})['issue']
        comments = c.pages(ln.COMMENTS_QUERY, {'id': issue_id}, ('issue', 'comments'))
        history = [dict(h, actorId=(h['actor'] or {}).get('id'))
                   for h in c.pages(ln.HISTORY_QUERY, {'id': issue_id}, ('issue', 'history'))]
        plan = json.loads((Path(__file__).resolve().parents[1] / 'ops/linear/approved-plan.json').read_text())
        root = Path(__file__).resolve().parents[1] / 'reports'
        receipt = ln.pass_receipt(row, comments, history, plan['orchestrator_id'], root)
        return bool(receipt and Path(receipt['result_path']).resolve() == Path(result_path).resolve()
                    and receipt['result_sha256'] == sha(result_path))
    except Exception as exc:          # a reader failure must never silently pass the issue
        print(json.dumps({'receipt_check': 'unavailable', 'why': str(exc)[:160]}))
        return None


def sync_wrapper_pins(result_path):
    """Re-pin the reconciliation wrapper for any covered file THIS issue changed.

    The wrapper refuses to run when a file it pins by sha has moved. That gate is right and stays. But a lane that
    legitimately repairs one of those files leaves its pin stale, and the next deploy dies at preflight publishing
    nothing — three deploy attempts were lost that way on 2026-09-11/12. Re-pinning at PASS, from the RESULT's own
    changed_files, makes the next run clean by construction. It never adds, removes or weakens a pin, and a file
    nobody claimed to change is left drifted so the gate still catches it.
    """
    try:
        import wrapper_pin_sync
        changed = json.loads(Path(result_path).read_text()).get('changed_files') or []
        paths = [str(Path(c)) for c in changed if str(c).endswith(('.py', '.mjs'))]
        if not paths:
            return
        receipt = wrapper_pin_sync.sync(paths=paths)
        if receipt.get('repinned'):
            print(json.dumps({'wrapper_pins_repinned': [r['path'].split('/')[-1] for r in receipt['repinned']]}))
    except Exception as error:
        print('wrapper pins not synced: %s' % error, file=sys.stderr)


def attach_evidence(identifier, path):
    """Attach the RESULT.json + named artifacts to the issue (WP6 linear_evidence). Idempotent; never fails a PASS."""
    try:
        import linear_evidence
        out = linear_evidence.attach(linear_evidence.Client(role='orchestrator'), identifier, path)
        print(json.dumps({k: v for k, v in out.items() if k in ('issue', 'status', 'comment_id')}))
    except Exception as error: print('evidence not attached: %s' % error, file=sys.stderr)
TEAM = 'd349d8ab-67cb-4a91-a0b0-2ab1a4daeb5b'
_KEY = None
def key():
    """Lazy so the module imports (and its pure helpers can be tested) without touching the secret."""
    global _KEY
    if _KEY is None: _KEY = open(os.path.expanduser('~/.hermes/secrets/linear/api_key')).read().strip()
    return _KEY
def gql(q, v=None):
    req = urllib.request.Request('https://api.linear.app/graphql', data=json.dumps({'query': q, 'variables': v or {}}).encode(), headers={'Authorization': key(), 'Content-Type': 'application/json', 'User-Agent': 'glp-orchestrator'})
    d = json.load(urllib.request.urlopen(req, timeout=60))
    if d.get('errors'): raise SystemExit('linear error: ' + json.dumps(d['errors'])[:300])
    return d['data']
def states(): return {s['name']: s['id'] for s in gql('query($id:String!){ team(id:$id){ states{ nodes{ id name } } } }', {'id': TEAM})['team']['states']['nodes']}
ISSUE_FIELDS = 'id identifier title description parent{ id } team{ id key } state{ id name } project{ id name } priority labels{ nodes{ id name } }'
def issue(ident): return gql('query($id:String!){ issue(id:$id){ ' + ISSUE_FIELDS + ' } }', {'id': ident})['issue']
def projects(): return {p['name']: p['id'] for p in gql('query($id:String!){ team(id:$id){ projects(first:250){ nodes{ id name } } } }', {'id': TEAM})['team']['projects']['nodes']}

ISSUE_TYPES = ('task', 'defect', 'incident', 'decision', 'idea')

def label_names(iss): return sorted(l['name'] for l in (iss.get('labels') or {}).get('nodes', []))

# Triage's own shape is legal while the item is in Triage and while it is dead (a declined or duplicate
# item never entered the executable queue). It is illegal in every state in between.
TRIAGE_SHAPE_OK_STATES = {'Triage', 'Canceled', 'Duplicate'}

def triage_placement_problem(state_name, project_name, labels):
    """Invariant: project "Triage" and the `triage` label are legal ONLY while the state is Triage
    (or the item is Canceled/Duplicate, i.e. never admitted).

    Pure, so the contract is testable without Linear. Returns None when the placement is legal."""
    if state_name in TRIAGE_SHAPE_OK_STATES: return None
    if project_name == 'Triage':
        return f'project "Triage" with state {state_name!r} — an admitted issue must carry its real project'
    if 'triage' in set(labels):
        return f'`triage` label with state {state_name!r} — an admitted issue must drop it'
    return None

# Dispositions select native states; legacy CLI aliases remain accepted.
TRIAGE_DISPOSITIONS = {'admitted', 'deferred', 'rejected', 'duplicate', 'needs-input'}
TRIAGE_ALIASES = {'accept': 'admitted', 'decline': 'rejected', 'defer': 'deferred'}
TRIAGE_TARGET_STATE = {'admitted': 'Backlog', 'deferred': 'Backlog', 'needs-input': 'Blocked',
                       'rejected': 'Canceled', 'duplicate': 'Duplicate'}

def team_label_id(name):
    """Resolve a label by exact name (team labels first, then workspace). None when it does not exist yet."""
    q = 'query($id:String!){ team(id:$id){ labels(first:250){ nodes{ id name } } } }'
    for row in gql(q, {'id': TEAM})['team']['labels']['nodes']:
        if row['name'] == name: return row['id']
    for row in gql('query{ issueLabels(first:250){ nodes{ id name } } }')['issueLabels']['nodes']:
        if row['name'] == name: return row['id']
    return None

def add_label(iss, name):
    """Union the label onto the issue. Returns True when applied, False when the label does not exist."""
    have = {l['name']: l['id'] for l in (iss.get('labels') or {}).get('nodes', [])}
    if name in have: return True
    lid = team_label_id(name)
    if lid is None: return False
    gql('mutation($id:String!,$i:IssueUpdateInput!){ issueUpdate(id:$id,input:$i){ success } }',
        {'id': iss['id'], 'i': {'labelIds': sorted(set(have.values()) | {lid})}})
    return True
def _lint_result(path, brief=None, expected_issue=None):
    """One typed completion validator; no force bypass for invalid evidence."""
    import result_lint
    probs = result_lint.lint(path, brief, expected_issue=expected_issue, require_complete=True)
    if probs:
        raise SystemExit('RESULT validation failed:\n  - ' + '\n  - '.join(probs))
    return probs

def comment(iid, body): return gql('mutation($i:CommentCreateInput!){ commentCreate(input:$i){ success comment{ id } } }', {'i': {'issueId': iid, 'body': body}})['commentCreate']
def guard_root_activation(iss, target):
    """Bound root activation; the candidate build lock owns surface exclusion."""
    from linear_client import ACTIVE_STATES, MAX_ACTIVE_ROOTS, Client, LinearError
    if target not in ACTIVE_STATES or iss.get('parent'):
        return
    if (iss.get('team') or {}).get('key') != 'GLP' or iss['team'].get('id') != TEAM:
        raise SystemExit('active roots must belong to GLP')
    try:
        roots = Client(role='orchestrator')._active_roots()
    except LinearError as error:
        raise SystemExit(str(error)) from None
    if iss['id'] in {root['id'] for root in roots}:
        return  # Active -> active consumes no additional slot.
    if len(roots) >= MAX_ACTIVE_ROOTS:
        raise SystemExit('refusing activation: at most %s active GLP roots' % MAX_ACTIVE_ROOTS)
    if not any(name.startswith('owner-surface:') and name.split(':', 1)[1].strip()
               for name in label_names(iss)):
        raise SystemExit('root activation requires an owner-surface:<surface> label')


def set_state(iid, sid):
    target = next((name for name, state_id in states().items() if state_id == sid), None)
    if target is None:
        raise SystemExit('unknown target state for GLP')
    from linear_client import ACTIVE_STATES
    if target in ACTIVE_STATES:
        with root_activation_lock():
            guard_root_activation(issue(iid), target)
            return gql('mutation($id:String!,$i:IssueUpdateInput!){ issueUpdate(id:$id,input:$i){ success issue{ identifier state{name} } } }', {'id': iid, 'i': {'stateId': sid}})['issueUpdate']['issue']
    return gql('mutation($id:String!,$i:IssueUpdateInput!){ issueUpdate(id:$id,input:$i){ success issue{ identifier state{name} } } }', {'id': iid, 'i': {'stateId': sid}})['issueUpdate']['issue']
OPERATOR_USER_ID = '86ceaadc-3c24-4810-ac3a-985cfc561a05'
def set_assignee(iid, uid):
    return gql('mutation($id:String!,$i:IssueUpdateInput!){ issueUpdate(id:$id,input:$i){ success issue{ identifier assignee{ name } } } }',
               {'id': iid, 'i': {'assigneeId': uid}})['issueUpdate']['issue']
def set_parent(iid, pid):
    return gql('mutation($id:String!,$i:IssueUpdateInput!){ issueUpdate(id:$id,input:$i){ success issue{ identifier parent{ identifier } } } }',
               {'id': iid, 'i': {'parentId': pid}})['issueUpdate']['issue']
def add_blocks(blocker_id, blocked_id, client=None):
    from linear_client import Client, link_blocks
    return link_blocks(client or Client(role='orchestrator'), blocker_id, blocked_id)


def require_active_root(client, root):
    """Validate an explicitly named root against the fresh fleet-wide set."""
    from linear_client import ACTIVE_STATES
    matches = [r for r in client._active_roots() if r['id'] == root['id']]
    if (len(matches) != 1 or root.get('parent')
            or (root.get('state') or {}).get('name') not in ACTIVE_STATES
            or (root.get('team') or {}).get('key') != 'GLP'
            or root['team'] != matches[0]['team']):
        raise SystemExit('admission must name an active root in GLP')
    return root


def repair_admission_client(repair, parent, blocked):
    """Structural admission only; the existing orchestrator owns the evidence judgment."""
    from linear_client import Client
    problem = triage_placement_problem('Todo', (repair.get('project') or {}).get('name'), label_names(repair))
    if problem:
        raise SystemExit('repair needs existing atomic triage admission before promotion: ' + problem)
    client = Client(role='orchestrator')
    root = require_active_root(client, parent)
    if (repair.get('team') != root['team'] or blocked.get('team') != root['team']
            or repair['id'] in (root['id'], blocked['id']) or
            (blocked['id'] != root['id'] and (blocked.get('parent') or {}).get('id') != root['id'])):
        raise SystemExit('repair --blocks target must be the named active root or its GLP execution child')
    if repair['state']['name'] in ('Done', 'Canceled', 'Duplicate'):
        raise SystemExit('closed repair cannot be admitted again')
    if not team_label_id('blocking'):
        raise SystemExit('missing structural blocking label; repair admission not performed')
    matching = 0
    for edge in client.connection('incoming_relations', {'id': blocked['id']}):
        if edge['type'] != 'blocks': continue
        other_id = edge['issue']['id']
        if other_id == repair['id']:
            matching += 1
            continue
        other = client.operation('issue_get', {'id': other_id})['issue']
        if other['state']['name'] not in ('Done', 'Canceled', 'Duplicate'):
            labels = client.connection('issue_labels', {'id': other_id})
            if any(label['name'] == 'blocking' for label in labels):
                raise SystemExit('existing blocking repair ' + other['identifier'] + '; reuse it before admitting another')
    if matching > 1:
        raise SystemExit('duplicate repair blocks edges; reconcile without replay')
    return client
def resolve_bound_request(iss, reason, disposition_comment_id=None):
    """Close the OPEN fable-owned REQUEST that linear_triage raised for this triage issue.

    The request's target is the active root (subissue None), so the FLEET_REQUEST_RESPONSE_V1 envelope is
    posted there and points at the plain reason comment on the triage issue. Prints one line; never raises."""
    try:
        import datetime, sqlite3
        import linear_requests, linear_client
        store = linear_requests.RequestStore()
        db = sqlite3.connect(str(linear_requests.DEFAULT_PATH)); db.row_factory = sqlite3.Row
        # versions keeps every revision: take the LATEST per request or a superseded OPEN row reopens a
        # request that is already resolved.
        rows = [dict(r) for r in db.execute(
            "SELECT request_id, record FROM versions v WHERE revision = "
            "(SELECT MAX(revision) FROM versions w WHERE w.request_id = v.request_id)")]
        db.close()
        open_ids = []
        for r in rows:
            try: rec = json.loads(r['record'])
            except Exception: continue
            # Match by binding first (the lane names the subissue, not always the identifier in prose),
            # then fall back to the identifier appearing in the question text.
            bound = rec.get('subissue_id') == iss['id'] or iss['identifier'] in str(rec.get('question', ''))
            if rec.get('status') == 'OPEN' and rec.get('owner_role') == 'fable' and bound:
                open_ids.append((rec['request_id'], rec['revision'], rec['root_issue_id'], rec.get('subissue_id')))
        if not open_ids: print(json.dumps({'resolve_request': 'none_open', 'issue': iss['identifier']})); return
        if disposition_comment_id is None:
            disposition_comment_id = comment(iss['id'], reason)['comment']['id']
        client = linear_client.Client(role='orchestrator')
        for request_id, revision, root_id, sub_id in open_ids:
            env = fenced('FLEET_REQUEST_RESPONSE_V1', {'request_id': request_id, 'revision': revision, 'action': 'RESOLVE_FABLE',
                         'disposition_issue_id': iss['id'], 'disposition_comment_id': disposition_comment_id, 'reason': reason})
            cid = comment(sub_id or root_id, env)['comment']['id']
            try:
                rec = store.receive_response(client, request_id, cid, now=datetime.datetime.now(datetime.timezone.utc))
                # receive_response returns {'request': record, 'mirror': ..., 'response_comment_id': ...}; reading
                # 'status' off the envelope printed a bare null on every successful resolve and read as a failure
                # (2026-09-12). Report the resolved request's own status.
                status = ((rec or {}).get('request') or {}).get('status') or 'RESOLVED'
                print(json.dumps({'resolve_request': status, 'request_id': request_id, 'issue': iss['identifier']}))
            except Exception as one:
                # One request failing (already resolved, stale revision) must not swallow the others or the ruling.
                print(json.dumps({'resolve_request': 'skipped', 'request_id': request_id, 'why': str(one)[:120]}))
    except Exception as error:
        print('request not resolved: %s' % error, file=sys.stderr)
def sha(p):
    with open(p, 'rb') as f: return hashlib.sha256(f.read()).hexdigest()
def fenced(marker, obj): return marker + '\n```json\n' + json.dumps(obj, indent=1) + '\n```'


# Workflow v3 Done gate (ops/linear/approvals/OPERATOR-DECISION-20261009-workflow-v3.md).
# Independent acceptance applies only to these three effects, classified over the
# issue title plus its Acceptance section(s); footers, labels and tool-wiring
# sections never trigger it. Release/deploy/design gates stay pre-merge PR gates.
PROTECTED_EFFECTS = {
    'financial-write': (
        r'\b(?:make|makes|made|issu\w*|process\w*|record\w*|book\w*|initiat\w*|execut\w*|trigger\w*|'
        r'disburs\w*|transfer\w*|collect\w*|pay|pays|paid|send\w*)\b[^\n.]{0,40}'
        r'(?<!#)\b(?:payments?|payouts?|refunds?|invoices?|funds?|money)\b|'
        r'\b(?:refund\w*|charg(?:e|es|ed|ing)|invoic(?:e|es|ed|ing)|purchas\w*|ad[- ]?spend|spend\w*|'
        r'budget\w*)\b|(?-i:\bREAL\b)|'
        r'\b(?:record|mark|claim|book|reconcile|verify|confirm|establish)\w*[^\n.]{0,40}'
        r'\b(?:earning\w*|commission\w*|settlement\w*|cash|payout\w*)\b'),
    'third-party-send': (
        r'\b(?:send\w*|sent|e-?mail\w*|messag\w*|deliver\w*|submit\w*|appl(?:y|ies|ied|ying|ication\w*)|'
        r'repl(?:y|ies|ied|ying))\b'
        r'[^\n.]{0,60}\b(?:partners?|providers?|customers?|sponsors?|users?|subscribers?|clinics?|'
        r'networks?|advertisers?|recipients?)\b|\boutreach\b|'
        r'\b(?:partners?|providers?|customers?|sponsors?|subscribers?|recipients?)\s+repl(?:y|ies)\b(?![./])|'
        r'\brepl(?:y|ies|ied|ying)\s+to\b|'
        r'\b(?:send\w*|sent|e-?mail\w*|messag\w*|deliver\w*)\b[^\n.]{0,60}'
        r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|'
        r'\b(?:send|sent)\s+(?:(?:a|the)\s+)?repl(?:y|ies)\b(?![./])|'
        r'(?<!no )(?<!never )(?<!zero )(?<!without )\b(?:a send|send occurs)\b'),
    'health-claim-publish': (
        r'\b(?:publish(?!ers?\b)\w*|publicat\w*)\b[^\n.]{0,60}\b(?:claims?|articles?|pages?|copy|content)\b|'
        r'\b(?:articles?|claims?)\b[^\n.]{0,20}\bpublish(?:ing|es|ed)?\b|'
        r'\b(?:new|health|medical|clinical)\s+claims?\b'),
}
# Exit/HTTP/status tokens alone are not business output (R3).
BARE_OUTPUT = (r'(?i)\s*(?:(?:rc|exit(?:[ _]?code)?|returncode|status|http)\s*[=:]?\s*)?'
               r'(?:0|ok|okay|success|succeeded|200|201|202|204|active|running|ready|true|'
               r'pass|passed|done|complete|completed|healthy|green)\s*|'
               # Field names, hash labels and bare timestamps name where output would be, not output.
               r'\s*(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)*|sha(?:1|256|512)?|'
               r'\d{4}-\d{2}-\d{2}(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?)\s*')


def acceptance_section(description):
    """Bodies of every Acceptance heading, each up to the next heading of the same or higher level."""
    import re
    description = description or ''
    headings = list(re.finditer(r'^(#{1,6})[ \t]*([^\n]*)$', description, re.M))
    parts = []
    for index, heading in enumerate(headings):
        if not re.match(r'Acceptance\b', heading[2].strip(), re.I):
            continue
        end = next((h.start() for h in headings[index + 1:] if len(h[1]) <= len(heading[1])), len(description))
        parts.append(description[heading.end():end])
    return '\n'.join(parts)


def _preserved_no_effect_boundary(text, match):
    """Recognize only charge/send tokens in an explicit preserved no-* boundary list."""
    import re
    prefix = {'charge': 'no-', 'send': 'no-third-party-'}.get(match[0].lower())
    return bool(prefix and re.search(
        r'(?:^|[.!?\n])[ \t]*Preserve[ \t]+(?:the[ \t]+)?(?:original[ \t]+)?'
        r'(?:no-[a-z]+(?:-[a-z]+)*,[ \t]+)*'
        r'(?:no-[a-z]+(?:-[a-z]+)*[ \t]+and[ \t]+)?' + prefix + r'$',
        text[:match.start()], re.I)
        and re.match(r'(?:,[ \t]+no-[a-z]+(?:-[a-z]+)*)*'
                     r'(?:[ \t]+and[ \t]+no-[a-z]+(?:-[a-z]+)*)?'
                     r'[ \t]+boundaries\b', text[match.end():], re.I))


def protected_effect(text):
    """Classify effect semantics, not internal reply artifacts or a negative guard.

    Do not discard whole clauses: a protected action beside file evidence or a
    no-duplicate constraint still needs independent acceptance.
    """
    import re
    # Remove only artifact tokens, never an entire clause/backtick span. A verb
    # or third-party recipient beside a filename must still classify protected.
    text = re.sub(r'(?<![\w/.-])/?(?:[\w.-]+/)*[\w.-]+\.'
                  r'(?:md|jsonl?|py|sh|txt|csv|ya?ml|log)\b',
                  lambda m: ' ' * len(m[0]), text or '', flags=re.I)
    for name, pattern in PROTECTED_EFFECTS.items():
        for match in re.finditer(pattern, text, re.I):
            # This is the retained SEO no-duplicate guard, not a spend action.
            # Exempt only its exact spend token; later/adjacent financial effects
            # (and all ambiguous negations) remain protected.
            if (name == 'financial-write' and match[0].lower() == 'spend'
                    and re.search(r'\bavoid\s+duplicate\s+finding\s*/\s*publication\s*/\s*$',
                                  text[:match.start()], re.I)):
                continue
            # An explicit preserved no-charge boundary is a prohibition, not a
            # charge. Exempt that token only in a sentence-start Preserve list
            # of no-* boundaries; bare/ambiguous negations still fail closed.
            # Never mask the clause or stop checking later financial matches.
            if name == 'financial-write' and _preserved_no_effect_boundary(text, match):
                continue
            return name
    return None


def is_bare_output(value):
    import re
    # Hex identifiers (deployment ids, commit shas) are output even when they start with a-f,
    # which the field-name branch of BARE_OUTPUT would otherwise swallow.
    if re.fullmatch(r'[0-9a-f]{7,64}', value or '') and re.search(r'\d', value):
        return False
    return bool(re.fullmatch(BARE_OUTPUT, value or ''))


def _verify_installed(data, repo, require):
    """R2: every installed file's bytes equal the authenticated merge-commit bytes."""
    import base64
    import subprocess
    root = Path(__file__).resolve().parents[1]
    installed = data.get('installed_files')
    require(isinstance(installed, list) and installed, 'current installed source evidence required')
    for entry in installed:
        source = Path(entry['path'])
        require(source.is_absolute() and source.is_file() and not source.is_symlink() and
                source.resolve().is_relative_to(root.resolve()) and
                source.resolve().relative_to(root.resolve()).parts[0] in ('scripts', 'systems', 'ops'),
                'installed maintained source path required')
        rel = source.resolve().relative_to(root.resolve()).as_posix()
        remote_file = json.loads(subprocess.check_output(
            ['gh', 'api', f'repos/{repo}/contents/{rel}?ref={data["pr"]["merge_sha"]}'],
            text=True, timeout=60))
        require(remote_file.get('encoding') == 'base64' and
                hashlib.sha256(base64.b64decode(remote_file['content'])).hexdigest() == entry['sha256'] and
                sha(source) == entry['sha256'], 'installed bytes do not match authenticated merged source')
    return installed


def _lint_ordinary_result(path, iss, *, result_data=None):
    """Read-only ordinary outcome check, not authority for any protected effect.

    The coordinator supplies a current description binding, existing acceptance
    comment and hash-bound observed evidence. Code/installed runtime also needs a
    merged PR/exact-head CI; explicit read-only non-code work uses the same evidence
    structures without inventing a PR. No brief, historical packet rewrite,
    command execution or pins. Protected scopes retain independent acceptance.

    Read-only non-code acceptance must include exactly one standalone line:
    Accepted description SHA256: <64 lowercase hex characters>
    Hash the exact issue description's UTF-8 bytes, without normalization. The
    authenticated coordinator comment must also retain its acceptance prefix,
    artifact paths and observed identifiers. A retained unbound comment requires
    a NEW scoped coordinator acceptance after review, never a historical edit.
    RESULT binds that new comment's id/body hash and the same description hash.
    This additional binding does not change the ordinary code/runtime contract.
    """
    import re
    import subprocess

    def require(condition, message):
        if not condition:
            raise SystemExit('ordinary completion refused: ' + message)

    try:
        # Preflight supplies the typed object in memory; all gates below are
        # identical to the persisted-result path, including business output.
        data = json.loads(Path(path).read_text()) if result_data is None else result_data
        require(isinstance(data, dict), 'result must be an object')
        health = data.get('schema') == 'linear-reviewed-health-completion-v1'
        require(health or data.get('schema') == 'linear-ordinary-completion-v1', 'unknown schema')
        require(data.get('issue') == iss['identifier'] and data.get('status') == 'COMPLETE',
                'exact issue and COMPLETE required')
        require(iss.get('team') == {'id': TEAM, 'key': 'GLP'}, 'issue must belong to GLP')
        readonly = data.get('scope') == 'ordinary-readonly-runtime'
        require((health and data.get('scope') == 'reviewed-health-runtime'
                 and data.get('protected_effects') == ['health-claim-publish']) or
                (not health and data.get('scope') in ('ordinary-code', 'ordinary-runtime', 'ordinary-readonly-runtime')
                 and data.get('protected_effects') == []), 'protected scope requires independent acceptance')
        description = iss.get('description') or ''
        require(re.search(r'##\s*Acceptance\s*\n\s*\S', description, re.I), 'current acceptance missing')
        require(data.get('description_sha256') == hashlib.sha256(description.encode()).hexdigest(),
                'current issue acceptance/scope changed')
        # Workflow v3 R4: only financial writes/charges, third-party sends and publishing
        # new health claims need independent acceptance (the protected arms). The
        # classifier reads the title and Acceptance section(s) only.
        effect = protected_effect((iss.get('title') or '') + '\n' + acceptance_section(description))
        if health:
            require(effect == 'health-claim-publish', 'health receipt cannot waive money/send or ordinary scope')
            from article_health_acceptance import validate
            try:
                validate(data.get('independent_health_review'),
                         natural=bool(re.search(r'\bnatural\b', (iss.get('title') or '') + '\n' +
                             acceptance_section(description), re.I)), root=Path(__file__).resolve().parents[1])
            except (ValueError, KeyError, OSError, RuntimeError) as exc:
                require(False, str(exc))
        else:
            require(effect is None, '%s requires --protected-runtime independent acceptance '
                    '(protected effect: %s)' % (effect, effect))
        if readonly:
            require('pr' not in data and data.get('changed_files', []) == [],
                    'read-only non-code work cannot claim a PR or source changes')
            # Keep nested headings and requirements after metadata-shaped lines.
            # Only a complete skill-name list is metadata, never a scope terminator.
            acceptance_match = re.search(r'##\s*Acceptance\s*\n(.*)', description, re.I | re.S)
            acceptance = acceptance_match.group(1) if acceptance_match else ''
            skill_name = r'[a-z][a-z0-9]*(?:-[a-z0-9]+)+'
            acceptance = '\n'.join(line for line in acceptance.splitlines() if not re.fullmatch(
                r'Skills:[ \t]*' + skill_name + r'(?:,[ \t]*' + skill_name + r')*\.?[ \t]*', line))
            require(re.search(r'\bread[- ]only\b', acceptance, re.I),
                    'explicit read-only acceptance required for non-code runtime')
            readonly_scope = (iss.get('title') or '') + '\n' + acceptance
            # A preserved no-third-party-send boundary is not an instruction to
            # send. Check every other token; do not discard its clause or scope.
            require(not any(not _preserved_no_effect_boundary(readonly_scope, match)
                            for match in re.finditer(
                                r'\b(send\w*|deliver\w*|submit\w*|appl(?:y|ies|ied|ying))\b',
                                readonly_scope, re.I)),
                    'protected send/application scope is not read-only')
            require(not re.search(r'\b(release|deploy\w*|publish(?!ers?\b)\w*|publicat\w*|production|design|'
                                  r'Fable|commission\w*|earning\w*|settlement|cash)\b',
                                  (iss.get('title') or '') + '\n' + acceptance, re.I),
                    'protected release/design/financial scope is not read-only')
        else:
            pr = data['pr']
            require(re.fullmatch(r'https://github\.com/izaiahat/glp-(?:business|site-candidate)/pull/[1-9]\d*',
                                 pr['url']), 'exact authorized repository PR URL required')
            require(all(re.fullmatch(r'[a-f0-9]{40}', pr[key]) for key in ('head_sha', 'merge_sha')),
                    'full head and merge SHA required')
            remote = json.loads(subprocess.check_output(
                ['gh', 'pr', 'view', pr['url'], '--json', 'state,headRefOid,mergeCommit,statusCheckRollup'],
                text=True, timeout=60))
            require(remote['state'] == 'MERGED' and remote['headRefOid'] == pr['head_sha'] and
                    (remote.get('mergeCommit') or {}).get('oid') == pr['merge_sha'], 'PR identity/merge mismatch')
            checks = remote.get('statusCheckRollup') or []
            require(checks and all(c.get('status') == 'COMPLETED' and c.get('conclusion') == 'SUCCESS'
                                   for c in checks), 'exact-head CI is not all successful')
            require(isinstance(pr.get('checks'), list) and pr['checks'] and all(
                any(c.get('name') == expected['name'] and c.get('detailsUrl') == expected['url'] for c in checks)
                for expected in pr['checks']), 'exact CI receipts missing or changed')
            # R2: installed bytes equal the merge for runtime outcomes.
            # Site-candidate PRs are deployed by the release service; their live readback is
            # the runtime evidence. Business-repo runtime work must prove installed bytes.
            repo = re.fullmatch(r'https://github\.com/(izaiahat/glp-[a-z-]+)/pull/\d+', pr['url'])[1]
            if (data['scope'] in ('ordinary-runtime', 'reviewed-health-runtime') and repo == 'izaiahat/glp-business') or data.get('installed_files'):
                _verify_installed(data, repo, require)
        review = data['review']
        comment_row = gql('query($id:String!){comment(id:$id){id body archivedAt createdAt user{id} issue{id}}}',
                          {'id': review['comment_id']})['comment']
        require(comment_row and comment_row['id'] == review['comment_id'] and
                comment_row['issue']['id'] == iss['id'] and not comment_row.get('archivedAt') and
                (comment_row.get('user') or {}).get('id') in (
                    OPERATOR_USER_ID, '858c38f1-aa44-4ff2-b2b7-04ea63fcf1e7'),
                'existing coordinator acceptance comment required on exact issue')
        # Negative custody only: self-declared distinct IDs cannot prove independence,
        # but an admitted ops coordinator declaring itself the author must refuse.
        reviewer_id = comment_row['user']['id']
        require(reviewer_id != '858c38f1-aa44-4ff2-b2b7-04ea63fcf1e7' or
                data.get('author_id') != reviewer_id,
                'ops execution author cannot accept its own ordinary outcome')
        body = comment_row['body']
        require(hashlib.sha256(body.encode()).hexdigest() == review['body_sha256'],
                'coordinator acceptance body changed')
        if readonly:
            require(body.startswith(('Coordinator acceptance verified:', 'Acceptance verified by parent:')),
                    'existing coordinator acceptance required')
            # Authenticated approval must name the accepted version, not just rely
            # on a worker refreshing RESULT's hash. No fallback for old comments.
            require(re.findall(r'^Accepted description SHA256: ([a-f0-9]{64})$', body, re.M) ==
                    [data['description_sha256']],
                    'coordinator accepted description binding missing, ambiguous or stale')
        else:
            require(body.startswith('Coordinator acceptance verified:') and data['pr']['merge_sha'] in body,
                    'coordinator acceptance body/merge binding missing or changed')
            # R2 bound to the authenticated comment: one Installed line per file.
            for entry in data.get('installed_files') or []:
                require(re.search(r'^Installed: (?:\S+ )?' + re.escape(entry['sha256']) + r'$', body, re.M),
                        'coordinator acceptance must bind Installed: <sha256> for each installed file')
        if health:
            acceptance_digest = hashlib.sha256(json.dumps(data['independent_health_review'],
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            require(re.findall(r'^Independent article acceptance SHA256: ([a-f0-9]{64})$', body, re.M)
                    == [acceptance_digest], 'coordinator independent article binding missing or stale')
        # Coordinator checked it and set Done the same hour.
        from datetime import datetime, timezone
        created = datetime.fromisoformat(comment_row['createdAt'].replace('Z', '+00:00'))
        require(abs((datetime.now(timezone.utc) - created).total_seconds()) <= 3600,
                'coordinator acceptance must be from the same hour as Done')
        evidence = data.get('evidence')
        require(isinstance(evidence, list) and evidence, 'acceptance evidence missing')
        runtime = readonly or data['scope'] == 'ordinary-runtime' or re.search(
            r'\b(installed|runtime|natural|read.?back|replayed|card|receipt|live)\b', description, re.I)
        require(not runtime or any(e.get('kind') == 'runtime' for e in evidence),
                'live acceptance evidence required; a merged PR does not prove runtime work')
        reports = Path(__file__).resolve().parents[1] / 'reports'
        for entry in evidence:
            source = Path(entry['path'])
            require(entry.get('kind') in ('check', 'runtime') and source.is_absolute() and
                    source.resolve().is_relative_to(reports.resolve()) and source.is_file() and
                    source.resolve() != Path(path).resolve(), 'existing reports evidence required')
            if readonly:
                reference = 'reports/' + source.resolve().relative_to(reports.resolve()).as_posix()
                require(entry.get('kind') == 'runtime' and re.search(
                    r'(?<![\w/.-])' + re.escape(reference) + r'(?=$|[\s#;:)`\]]|[.,](?=\s|$))', body),
                    'read-only runtime artifact must be named in coordinator acceptance')
            raw = source.read_bytes()
            require(hashlib.sha256(raw).hexdigest() == entry['sha256'], 'acceptance evidence hash changed')
            observed = entry.get('observed')
            require(isinstance(observed, list) and observed and all(
                isinstance(value, str) and value.strip() and value in raw.decode() for value in observed),
                'observed acceptance identifiers/output missing from evidence')
            if entry['kind'] == 'runtime':
                require(any(value in body for value in observed), 'runtime evidence absent from coordinator acceptance')
        # R3: one real run/readback carries business output, not only exit/HTTP/ok tokens,
        # and the coordinator comment names it on an Output: line.
        outputs = re.findall(r'^Output: (.+)$', body, re.M)
        require(any(not is_bare_output(value) and any(value in line for line in outputs)
                    for entry in evidence if entry.get('kind') == 'runtime' or not runtime
                    for value in entry['observed']),
                'business output required: rc=0/ok/200/active-only evidence is not output; '
                'coordinator acceptance needs Output: <observed business value>')
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit('ordinary completion evidence unavailable/invalid: ' + str(exc)) from None


NATIVE_REVIEW_DB = Path.home() / '.hermes' / 'state.db'


def _lint_glp109_native_review(custody, data, iss, result_path, parent, independent, payload):
    """Verify the retained GLP109 Astra review, not a third Linear identity.

    SessionDB sessions/messages are runtime-owned provenance at the existing local
    runtime/coordinator trust boundary, not an OS-isolated signature. Read only the
    named native rows. The shorter-lived async ledger is not the custody source;
    retained session parent/_delegate_from and successful write_file records are.
    Missing/pruned custody refuses; a filesystem report never substitutes for it.
    """
    import re
    import sqlite3

    def require(condition, message):
        if not condition:
            raise SystemExit('protected native review refused: ' + message)

    require(iss['identifier'] == 'GLP-109' and data['scope'] == 'protected-runtime',
            'native exception is only the approved GLP109 retained runtime')
    require(parent['user']['id'] == OPERATOR_USER_ID and
            independent['user']['id'] == OPERATOR_USER_ID and
            data['author_id'] != OPERATOR_USER_ID,
            'non-author operator must authenticate adoption and coordinator acceptance')
    require(set(custody) == {'coordinator_session_id', 'author', 'reviewer'}, 'exact native custody required')
    coordinator = custody['coordinator_session_id']
    author = custody['author']['session_id']
    reviewer = custody['reviewer']['session_id']
    require(all(isinstance(v, str) and v for v in (coordinator, author, reviewer)) and
            len({coordinator, author, reviewer}) == 3, 'distinct author, reviewer and coordinator sessions required')
    native_digest = hashlib.sha256(json.dumps(custody, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    for row in (parent, independent):
        require(re.findall(r'^Native independent review SHA256: (.+)$', row['body'], re.M) == [native_digest],
                'authenticated native custody binding missing, ambiguous or stale')
    reports = Path(__file__).resolve().parents[1] / 'reports'
    try:
        # mode=ro never creates a DB, changes schemas or uses native helpers which
        # open write transactions. Hold one read snapshot across all custody rows.
        with sqlite3.connect(NATIVE_REVIEW_DB.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            require(conn.execute('SELECT id FROM sessions WHERE id=?', (coordinator,)).fetchone(),
                    'native coordinator session missing')
            artifacts = {}
            for role in ('author', 'reviewer'):
                binding = custody[role]
                require(set(binding) == {'session_id', 'path', 'sha256', 'write_message_id', 'tool_result_message_id'},
                        'exact native artifact binding required')
                session = conn.execute('SELECT parent_session_id,model,model_config,ended_at,end_reason '
                                       'FROM sessions WHERE id=?', (binding['session_id'],)).fetchone()
                require(session and session['parent_session_id'] == coordinator and
                        json.loads(session['model_config'])['_delegate_from'] == coordinator and
                        session['ended_at'] is not None and session['end_reason'] == 'agent_close',
                        'completed native child linkage missing')
                if role == 'reviewer':
                    require(session['model'] == 'gpt-6-astra-900k', 'designated Astra review required')
                source = Path(binding['path'])
                require(source.is_absolute() and source.is_file() and not source.is_symlink() and
                        source.resolve().is_relative_to(reports.resolve()) and
                        re.fullmatch(r'[a-f0-9]{64}', binding['sha256']), 'reports-only native artifact required')
                content = source.read_bytes()
                require(hashlib.sha256(content).hexdigest() == binding['sha256'], 'native report bytes changed')
                require(type(binding['write_message_id']) is int and type(binding['tool_result_message_id']) is int,
                        'native message IDs required')
                write = conn.execute('SELECT * FROM messages WHERE id=?', (binding['write_message_id'],)).fetchone()
                result = conn.execute('SELECT * FROM messages WHERE id=?', (binding['tool_result_message_id'],)).fetchone()
                require(write and result and write['role'] == 'assistant' and result['role'] == 'tool' and
                        write['session_id'] == result['session_id'] == binding['session_id'] and
                        result['tool_name'] == 'write_file' and
                        write['timestamp'] <= result['timestamp'] <= session['ended_at'],
                        'native report write/result custody missing')
                calls = [call for call in json.loads(write['tool_calls'])
                         if call.get('id') == result['tool_call_id'] and
                         call.get('function', {}).get('name') == 'write_file']
                require(len(calls) == 1, 'unique native write_file call required')
                args = json.loads(calls[0]['function']['arguments'])
                receipt = json.loads(result['content'])
                require(args['path'] == str(source) and args['content'].encode() == content and
                        receipt.get('verified') is True and receipt.get('resolved_path') == str(source.resolve()) and
                        receipt.get('bytes_written') == len(content), 'native write bytes or successful receipt mismatch')
                require(source.read_bytes() == content, 'native artifact changed during validation')
                artifacts[role] = json.loads(content)
    except (sqlite3.Error, KeyError, TypeError, ValueError, OSError) as exc:
        raise SystemExit('protected native review custody unavailable/invalid: ' + str(exc)) from None
    authored = artifacts['author']
    source = authored['source']
    require(authored['issue'] == iss['identifier'] and source['pr'] == data['pr']['url'] and
            source['head'] == data['pr']['head_sha'] and source['merge'] == data['pr']['merge_sha'] and
            source['installed_sha256'] in [f['sha256'] for f in data['installed_files']],
            'native execution author source binding changed')
    accepted = artifacts['reviewer']
    require(accepted['verdict'] == 'ACCEPT_FULL_RETAINED_SCHEDULER_RUNTIME_EVIDENCE' and
            accepted['issue'] == iss['identifier'] and accepted['issue_id'] == iss['id'] and
            accepted['candidate'] == str(Path(result_path).resolve()) and
            accepted['description_sha256'] == data['description_sha256'] and
            accepted['protected_evidence_sha256'] == payload and
            accepted['author_id'] == data['author_id'] and accepted['parent_viewer_id'] == parent['user']['id'],
            'native independent acceptance is missing or stale for exact scope/evidence/author')


def _lint_protected_result(path, iss, *, runtime=False):
    """Verify retained protected receipts, never authorize a new effect.

    Uses the existing typed RESULT and runtime evidence formats. Both authenticated
    acceptances bind the exact description and complete evidence payload (canonical
    JSON, excluding only review/protected_review). Parent separately binds the exact
    independent comment/body. Execution identity is parent-attested, not inferred
    from shared GitHub accounts; the authenticated independent user must differ
    from both the attested author and parent. GLP109's explicitly approved native
    exception instead verifies distinct completed native child sessions and exact
    artifact-write custody; the non-author operator authenticates adoption and
    coordinator acceptance in separate bound comments. It never infers identity
    from an editable report label or missing runtime history.
    """
    import base64
    import re
    import subprocess

    def require(condition, message):
        if not condition:
            raise SystemExit('protected completion refused: ' + message)

    try:
        data = json.loads(Path(path).read_text())
        require(isinstance(data, dict), 'result must be an object')
        require(data.get('issue') == iss['identifier'] and data.get('status') == 'COMPLETE' and
                iss.get('team') == {'id': TEAM, 'key': 'GLP'}, 'exact GLP issue and COMPLETE required')
        require(data.get('scope') == ('protected-runtime' if runtime else 'protected-noncode') and
                data.get('protected_effects') == [] and data.get('changed_files') == [] and 'brief' not in data,
                'distinct retained non-code/runtime scope required; no brief, source edits or new effects')
        if not runtime:
            require('pr' not in data and 'installed_files' not in data,
                    'non-code arm cannot accept installed-code provenance')
        description = iss.get('description') or ''
        require(re.search(r'##\s*Acceptance\s*\n\s*\S', description, re.I) and
                data.get('description_sha256') == hashlib.sha256(description.encode()).hexdigest(),
                'current issue acceptance/scope changed')
        # Legacy lint reads only the first Acceptance. This arm additionally
        # requires complete bodies of ALL acceptance/tool-wiring sections,
        # including nested subheadings; it never reduces the description.
        clauses = data.get('acceptance_clauses', []) + data.get('retained_acceptance_clauses', [])
        require(clauses and all(c.get('verdict') == 'PASS' and c.get('evidence') for c in clauses),
                'all original/nested acceptance clauses need PASS with evidence')
        normalise = lambda text: re.sub(r'\s+', ' ', text).strip()
        answered = normalise('\n'.join(c['clause'] for c in clauses))
        # Ignore only known metadata forms, never stop at a metadata-looking line:
        # subsequent requirements and nested headings still belong to this section.
        metadata = (r'PR: pending or retain linked existing PR; no source reauthoring solely for consolidation\.|'
                    r'Authority: `ops/linear/approvals/[^`\n]+\.md`\.'
                    r'(?:[ \t]+Plan: `reports/[^`\n]+\.md`\.)?|Plan: `reports/[^`\n]+\.md`\.')
        scope = [iss.get('title') or '']
        headings = list(re.finditer(r'^(#{2,6})[ \t]+([^\n]+)\n', description, re.M))
        for index, heading in enumerate(headings):
            if not re.match(r'Acceptance\b|Relevant tool wiring\b', heading[2], re.I):
                continue
            end = next((h.start() for h in headings[index + 1:] if len(h[1]) <= len(heading[1])),
                       len(description))
            body = description[heading.end():end]
            scope.append(body)
            requirements = '\n'.join(line for line in body.splitlines()
                                     if not re.fullmatch(metadata, line.strip()))
            require(normalise(requirements) in answered,
                    'full original/nested acceptance or tool-wiring coverage missing')
        if not runtime:
            # The authenticated issue's contract, not a caller's scope label or
            # omission of pr/installed_files, determines whether provenance is needed.
            require(not re.search(r'\b(?:install\w*|deploy\w*|source[- ](?:code|bytes))\b',
                                  '\n'.join(scope), re.I),
                    'installed-source/deployment acceptance requires runtime provenance, not non-code')
        author = data.get('author_id')
        require(isinstance(author, str) and re.fullmatch(r'[a-f0-9-]{36}', author),
                'parent-attested execution author Linear ID required')
        payload = hashlib.sha256(json.dumps(
            {k: v for k, v in data.items() if k not in ('review', 'protected_review')},
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        protected_review = data['protected_review']
        discord = isinstance(protected_review, dict) and 'discord' in protected_review
        if discord:
            require(set(protected_review) == {'discord'}, 'Discord and Linear/native acceptance are distinct contracts')
        rows = []
        for key, prefix in (('review', ('Coordinator acceptance verified:', 'Acceptance verified by parent:')),
                            ('protected_review', ('Independent protected acceptance verified:',))):
            if discord and key == 'protected_review':
                continue
            binding = data[key]
            row = gql('query($id:String!){comment(id:$id){id body archivedAt createdAt user{id} issue{id}}}',
                      {'id': binding['comment_id']})['comment']
            require(row and row['id'] == binding['comment_id'] and
                    row['issue']['id'] == iss['id'] and not row.get('archivedAt') and
                    (row.get('user') or {}).get('id'), 'authenticated ' + key + ' required on exact issue')
            body = row['body']
            require(hashlib.sha256(body.encode()).hexdigest() == binding['body_sha256'] and
                    body.startswith(prefix), key + ' acceptance missing or body changed')
            for label, value in (('Accepted description SHA256', data['description_sha256']),
                                 ('Accepted protected evidence SHA256', payload),
                                 ('Execution author Linear ID', author)):
                require(re.findall(r'^' + re.escape(label) + r': (.+)$', body, re.M) == [value],
                        key + ' ' + label + ' binding missing, ambiguous or stale')
            rows.append(row)
        parent = rows[0]
        require(parent['user']['id'] in (OPERATOR_USER_ID, '858c38f1-aa44-4ff2-b2b7-04ea63fcf1e7'),
                'authenticated coordinator acceptance required')
        from datetime import datetime, timezone
        created = datetime.fromisoformat(parent['createdAt'].replace('Z', '+00:00'))
        require(abs((datetime.now(timezone.utc) - created).total_seconds()) <= 3600,
                'coordinator acceptance must be from the same hour as Done')
        if discord:
            from linear_operator_acceptance import verify
            binding_hash = verify(protected_review['discord'], iss['identifier'], data.get('evidence') or [])
            require(re.findall(r'^Independent operator Discord acceptance SHA256: (.+)$', parent['body'], re.M) ==
                    [binding_hash], 'parent must bind exact authenticated operator Discord acceptance')
        else:
            independent = rows[1]
            require(independent['id'] != parent['id'], 'separate adoption and coordinator comments required')
            if 'native' in protected_review:
                require(runtime, 'native review cannot bypass non-code independent acceptance')
                _lint_glp109_native_review(protected_review['native'], data, iss, path, parent, independent, payload)
            else:
                require(independent['user']['id'] not in (author, parent['user']['id']),
                        'separate non-author independent protected acceptance required')
            require(re.findall(r'^Independent protected acceptance: (.+)$', parent['body'], re.M) ==
                    [protected_review['comment_id'] + ' ' + protected_review['body_sha256']],
                    'parent must bind designated independent protected acceptance')
        if runtime:
            pr = data['pr']
            match = re.fullmatch(r'https://github\.com/(izaiahat/glp-business)/pull/[1-9]\d*', pr['url'])
            require(match and all(re.fullmatch(r'[a-f0-9]{40}', pr[key]) for key in ('head_sha', 'merge_sha')),
                    'exact maintained PR/full head and merge required')
            assert match is not None  # require above fails closed; narrows the authenticated repo match.
            remote = json.loads(subprocess.check_output(
                ['gh', 'pr', 'view', pr['url'], '--json', 'state,headRefOid,mergeCommit,statusCheckRollup'],
                text=True, timeout=60))
            checks = remote.get('statusCheckRollup') or []
            require(remote['state'] == 'MERGED' and remote['headRefOid'] == pr['head_sha'] and
                    (remote.get('mergeCommit') or {}).get('oid') == pr['merge_sha'], 'PR identity/merge mismatch')
            require(checks and all(c.get('status') == 'COMPLETED' and c.get('conclusion') == 'SUCCESS'
                                      for c in checks), 'exact-head CI is not all successful')
            require(isinstance(pr.get('checks'), list) and pr['checks'] and all(
                any(c.get('name') == e['name'] and c.get('detailsUrl') == e['url'] for c in checks)
                for e in pr['checks']), 'exact CI receipts missing or changed')
            _verify_installed(data, match[1], require)
        reports = Path(__file__).resolve().parents[1] / 'reports'
        evidence = data.get('evidence')
        require(isinstance(evidence, list) and evidence and any(e.get('kind') == 'runtime' for e in evidence),
                'runtime acceptance evidence missing')
        for entry in evidence:
            source = Path(entry['path'])
            require(entry.get('kind') in (('check', 'runtime') if runtime else ('runtime',)) and
                    source.is_absolute() and source.is_file() and
                    source.resolve().is_relative_to(reports.resolve()) and
                    source.resolve() != Path(path).resolve(), 'existing reports runtime evidence required')
            raw = source.read_bytes()
            require(hashlib.sha256(raw).hexdigest() == entry['sha256'], 'runtime evidence bytes changed')
            observed = entry.get('observed')
            require(isinstance(observed, list) and observed and all(
                isinstance(value, str) and value.strip() and value in raw.decode() for value in observed),
                'observed identifiers/output missing from runtime evidence')
        require(any(not is_bare_output(v) for e in evidence if e.get('kind') == 'runtime' for v in e['observed']),
                'business output required: rc=0/ok/200/active-only evidence is not output')
        # Replaces only legacy FILES/Touch for this explicit non-code arm. Every
        # reported artifact is a reports-only file bound by both acceptances.
        for artifact in data.get('artifacts_created', []):
            source = Path(artifact)
            if not source.is_absolute():
                source = reports.parent / source
            require(source.is_file() and source.resolve().is_relative_to(reports.resolve()) and
                    (source.resolve() == Path(path).resolve() or any(
                        source.resolve() == Path(e['path']).resolve() for e in evidence)),
                    'reported artifact must be RESULT or hash-bound runtime evidence')
        _lint_result(path, expected_issue=iss['identifier'])
        if discord:
            # Re-read after slower provenance/legacy checks, immediately before
            # returning to the single writer. Never rely on capture-time copies.
            verify(protected_review['discord'], iss['identifier'], data['evidence'])
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit('protected completion evidence unavailable/invalid: ' + str(exc)) from None


def pass_issue(iss, result_path, st, brief=None, *, ordinary=False, protected_noncode=False,
               protected_runtime=False, check_only=False):
    """The single Done writer. A failed readback requires inspection, not replay."""
    path = Path(result_path)
    reports = Path(__file__).resolve().parents[1] / 'reports'
    if (not path.is_absolute() or path.name != 'RESULT.json' or not path.is_file()
            or not path.resolve().is_relative_to(reports.resolve())):
        raise SystemExit('need an existing absolute RESULT.json path under reports')
    path = str(path.resolve())
    if iss['state']['name'] == 'Done':
        if not receipt_validates(iss['id'], path):
            raise SystemExit('already Done but exact receipt unverified; inspect before any action')
        print(json.dumps({'identifier': iss['identifier'], 'state': 'Done', 'already_verified': True}))
        return
    digest = sha(path)
    if protected_noncode or protected_runtime:
        if ordinary or brief is not None or (protected_noncode and protected_runtime):
            raise SystemExit('--protected-noncode, --protected-runtime, --ordinary and --brief are distinct contracts')
        _lint_protected_result(path, iss, runtime=protected_runtime)
    elif ordinary:
        if brief is not None:
            raise SystemExit('--ordinary and --brief are distinct contracts')
        _lint_ordinary_result(path, iss)
    else:
        if brief is None or not Path(brief).is_file():
            raise SystemExit('new closure needs its existing scope brief via --brief')
        _lint_result(path, brief, expected_issue=iss['identifier'])
    if sha(path) != digest:
        raise SystemExit('RESULT changed during validation; inspect before any action')
    if check_only:
        print(json.dumps({'identifier': iss['identifier'], 'validated': True, 'writes': 0,
                          'result_sha256': digest}))
        return
    if protected_noncode or protected_runtime:
        # Reserve exactly once before *any* external write, including startedAt.
        # An uncertain receipt/Done outcome must never make this reply reusable.
        retained = json.loads(Path(path).read_text()).get('protected_review') or {}
        if 'discord' in retained:
            from linear_operator_acceptance import consume
            try:
                consume(retained['discord'], iss['identifier'],
                        json.loads(Path(path).read_text())['evidence'], digest)
            except (ValueError, OSError) as exc:
                raise SystemExit('protected completion refused: ' + str(exc)) from None
        if sha(path) != digest:
            raise SystemExit('RESULT changed before closure attempt; inspect before any action')
    if iss['state']['name'] in ('Todo', 'Backlog', 'Triage'):
        # Retain the real startedAt needed by existing cycle-time readers.
        set_state(iss['id'], st['In Progress'])
        iss['state'] = {'id': st['In Progress'], 'name': 'In Progress'}
    # Existing Done readers accept the exact RESULT binding without a metadata echo.
    receipt_data = {'issue_id': iss['id'], 'outcome': 'PASS', 'result_path': path, 'result_sha256': digest}
    if protected_noncode or protected_runtime:
        retained = json.loads(Path(path).read_text()).get('protected_review') or {}
        if 'discord' in retained:
            # Inline the authenticated original snapshots, not just a mutable file
            # pointer. The unchanged RESULT hash also binds scope/author/evidence.
            receipt_data['independent_operator_acceptance'] = retained['discord']
        if sha(path) != digest:
            raise SystemExit('RESULT changed before receipt; inspect before any action')
    receipt = comment(iss['id'], fenced('FLEET_RESULT_V1', receipt_data))
    if not receipt.get('success') or not (receipt.get('comment') or {}).get('id'):
        raise SystemExit('RESULT comment unconfirmed; inspect before any action')
    if sha(path) != digest:
        raise SystemExit('RESULT changed after receipt; inspect before any action')
    result = set_state(iss['id'], st['Done'])
    if sha(path) != digest or not receipt_validates(iss['id'], path):
        raise SystemExit('closure readback unverified; inspect actual state/receipt before any action')
    print(json.dumps(result))
    if not ordinary and not (protected_noncode or protected_runtime):
        sync_wrapper_pins(path)


def main():
    a = sys.argv[1:]
    if not a: print(__doc__); return
    cmd = a[0]
    if cmd not in ('transition', 'pass', 'rework', 'triage', 'assign', 'promote', 'resolve-request', 'relabel', 'rule', 'reorder'): print(__doc__); return 2
    if len(a) < 2: raise SystemExit(cmd + ' needs an issue identifier, e.g. GLP-163')
    if any(flag in a for flag in ('--ordinary', '--protected-noncode', '--protected-runtime', '--check-only')) and not (
            cmd == 'pass' or (cmd == 'transition' and len(a) > 2 and a[2] == 'Done')):
        raise SystemExit('--ordinary/--protected-noncode/--protected-runtime/--check-only are for completion only')
    st = states(); i = issue(a[1])
    if cmd == 'transition':
        if a[2] == 'Done':
            brief = a[a.index('--brief') + 1] if '--brief' in a else None
            return pass_issue(i, a[3], st, brief, ordinary='--ordinary' in a,
                              protected_noncode='--protected-noncode' in a,
                              protected_runtime='--protected-runtime' in a, check_only='--check-only' in a)
        to = st[a[2]]; path = os.path.abspath(a[3]); assert path.endswith('RESULT.json') and os.path.exists(path), 'need an existing absolute RESULT.json path'

        print(json.dumps(set_state(i['id'], to)))
        if not i.get('parent'):
            inflation_warning(i['id'])
    elif cmd == 'pass':
        brief = a[a.index('--brief') + 1] if '--brief' in a else None
        return pass_issue(i, a[2], st, brief, ordinary='--ordinary' in a,
                          protected_noncode='--protected-noncode' in a,
                          protected_runtime='--protected-runtime' in a, check_only='--check-only' in a)
    elif cmd == 'assign':
        # The scoped client refuses metadata writes on children, so nothing sanctioned could change an
        # assignee; an issue parked on the operator by mistake stayed in his "Needs you" view forever.
        who = a[2]; why = a[3] if len(a) > 3 else ''
        assert why.strip(), 'assign needs a one-line reason'
        if who == 'operator': uid = OPERATOR_USER_ID
        elif who == 'none': uid = None
        elif who.startswith('lane:'):
            uid = None  # lanes are not Linear users: the lane label owns it, the assignee is cleared
        else: raise SystemExit('assign target must be operator | lane:<name> | none')
        print(json.dumps(set_assignee(i['id'], uid)))
    elif cmd == 'promote':
        # Reparenting an IN-FLIGHT issue breaks the lane working it: the scoped client only permits comments on
        # the active root's children, so its closing comment is refused and a finished issue reports BLOCKED
        # (GLP-184, 2026-09-12). Re-cut issues that are not dispatched, or accept that you post the comment.
        try:
            led = json.loads((Path(__file__).resolve().parents[1] / 'ops/linear/autodispatch-ledger.json').read_text())
            holder = next((lane for lane, v in (led.get('entries') or {}).items()
                           if isinstance(v, dict) and v.get('issue') == i['identifier']), None)
        except Exception:
            holder = None
        if holder and '--force-inflight' not in a:
            raise SystemExit('refusing to reparent %s: %s is working it right now (autodispatch ledger). '
                             'Re-cut it after it PASSes, or pass --force-inflight and post its closing comment '
                             'yourself from this seat.' % (i['identifier'], holder))
        # A triage/backlog defect that turns out to make the active root's verify fail. The scoped client
        # cannot reparent or relate a non-child, so without this verb the only options were leaving the
        # dependency undiscoverable or absorbing the work into the root without a record.
        if '--root' in a:
            at = a.index('--root')
            if at != 3 or at + 1 == len(a) or a[at + 1].startswith('--'):
                raise SystemExit('promote needs "<why>" --root <GLP-active-root>')
            parent = issue(a[at + 1]); why = a[2]
        else:
            # Retain the original explicit positional-root contract.
            parent = issue(a[2]); why = a[3] if len(a) > 3 else ''
        assert why.strip(), 'promote needs a one-line reason'
        if '--blocks' not in a or a.index('--blocks') + 1 == len(a):
            raise SystemExit('repair promotion requires --blocks GLP-n')
        blocked = issue(a[a.index('--blocks') + 1])
        client = repair_admission_client(i, parent, blocked)
        out = {'parent': i.get('parent')}
        if (i.get('parent') or {}).get('id') != parent['id']:
            out['parent'] = set_parent(i['id'], parent['id'])
        out['blocking_label'] = add_label(i, 'blocking')
        if i['state']['name'] in ('Backlog', 'Triage'):
            out['state'] = set_state(i['id'], st['Todo'])
        out['blocks'] = add_blocks(i['id'], blocked['id'], client)
        print(json.dumps(out))
    elif cmd == 'reorder':
        # Operator-directed change to the approved ROOT ORDER (WORK-ORDER §G: root/order/admission are the orchestrator's).
        # Usage: reorder GLP-n --after GLP-m --basis "<one line>" --receipt ops/linear/approvals/OPERATOR-DECISION-*.json
        # It never manufactures approval: the receipt must carry the operator's own words. It inserts (or moves) the
        # top-level issue right after --after, rewrites ops/linear/approved-plan.json, re-signs approved-plan.sha256 and
        # appends the basis to approval_reference. Found 2026-09-17: the K10-023 SEO root (GLP-7) was never in the 37-id
        # manifest, so the selector could never reach it and no article published for a week.
        for flag in ('--after', '--basis', '--receipt'):
            if flag not in a or a.index(flag) + 1 == len(a) or a[a.index(flag) + 1].startswith('--'):
                raise SystemExit('reorder needs ' + flag)
        after = issue(a[a.index('--after') + 1]); basis = a[a.index('--basis') + 1].strip()
        receipt = Path(a[a.index('--receipt') + 1])
        rec = json.loads(receipt.read_text())
        if not str(rec.get('operator_words') or '').strip() or not basis:
            raise SystemExit('reorder refused: receipt has no operator_words or basis is empty')
        if i.get('parent'):
            raise SystemExit('reorder refused: %s is a child of %s, only top-level roots are ordered' % (i['identifier'], i['parent'].get('identifier')))
        if i['state']['name'] in ('Done', 'Canceled', 'Duplicate'):
            raise SystemExit('reorder refused: %s is %s' % (i['identifier'], i['state']['name']))
        plan_path = Path(__file__).resolve().parents[1] / 'ops/linear/approved-plan.json'
        sha_path = Path(__file__).resolve().parents[1] / 'ops/linear/approved-plan.sha256'
        plan = json.loads(plan_path.read_text()); ids = list(plan['issue_ids'])
        if after['id'] not in ids:
            raise SystemExit('reorder refused: --after %s is not in the approved order' % after['identifier'])
        if after['id'] == i['id']:
            raise SystemExit('reorder refused: an issue cannot follow itself')
        before_pos = ids.index(i['id']) if i['id'] in ids else None
        if i['id'] in ids: ids.remove(i['id'])
        ids.insert(ids.index(after['id']) + 1, i['id'])
        if len(set(ids)) != len(ids): raise SystemExit('reorder refused: duplicate ids after insert')
        plan['issue_ids'] = ids
        plan['approval_reference'] = str(plan.get('approval_reference') or '') + '; ' + basis + ' (receipt ' + str(receipt) + ')'
        body = json.dumps(plan, indent=1) + '\n'
        plan_path.write_text(body)
        sha_path.write_text(hashlib.sha256(body.encode()).hexdigest() + '\n')
        pos = ids.index(i['id'])
        print(json.dumps({'identifier': i['identifier'], 'moved_from': before_pos, 'position': pos, 'after': after['identifier'],
                          'plan_sha256': sha_path.read_text().strip(), 'roots': len(ids)}))
    elif cmd == 'relabel':
        # Lane labels pin execution (the selector filters on them), and the scoped client refuses metadata
        # writes on children, so an idle lane could never take a sibling's child. One lane label at a time.
        lane = a[2]; why = a[3] if len(a) > 3 else ''
        assert lane.startswith('lane:') and why.strip(), 'relabel needs lane:<name> and a one-line reason'
        have = {l['name']: l['id'] for l in (i.get('labels') or {}).get('nodes', [])}
        new_id = team_label_id(lane); assert new_id, 'unknown label ' + lane
        keep = {lid for name, lid in have.items() if not name.startswith('lane:')} | {new_id}

        out = gql('mutation($id:String!,$i:IssueUpdateInput!){ issueUpdate(id:$id,input:$i){ success issue{ identifier labels{ nodes{ name } } } } }',
                  {'id': i['id'], 'i': {'labelIds': sorted(keep)}})['issueUpdate']['issue']
        print(json.dumps({'identifier': out['identifier'], 'labels': sorted(l['name'] for l in out['labels']['nodes'])}))
    elif cmd == 'rule':
        # A lane's genuine question (REQUEST, owner_role fable) is answered here: one reason on the issue,
        # then the bound request closes so the monitor stops re-emitting it.
        ruling = a[2] if len(a) > 2 else ''; assert ruling.strip(), 'rule needs the ruling text'
        blocked = (i.get('state') or {}).get('name') == 'Blocked'
        if blocked and not st.get('In Progress'):
            raise SystemExit('missing target state: In Progress')
        cid = comment(i['id'], ruling)['comment']['id']
        resolve_bound_request(i, ruling, cid)
        # A ruling is precisely what un-blocks a lane, so the state must not stay Blocked. Leaving it
        # showed the operator a stopped item while the lane was already working on the answer
        # (his dashboard, 2026-09-13). Answering and clearing are ONE operation, never two.
        moved = None
        if blocked:
            moved = set_state(i['id'], st['In Progress'])['state']['name']
        print(json.dumps({'identifier': i['identifier'], 'ruled': True, 'unblocked_to': moved}))
    elif cmd == 'resolve-request':
        resolve_bound_request(i, a[2] if len(a) > 2 else 'dispositioned by the orchestrator')
    elif cmd == 'rework':
        cause = a[2]; note = a[3]; assert cause in ('brief', 'machine', 'supply'), 'cause must be brief|machine|supply'
        comment(i['id'], f"**REWORK[{cause}]** {note}")
        # The label is the machine-readable half (linear_cycle_time.py counts both). A missing label
        # never suppresses the comment: it warns and the metric still has the comment to count.
        labelled = add_label(i, 'rework:' + cause)
        if not labelled:
            print(f"WARNING: label 'rework:{cause}' does not exist yet — comment posted, label NOT applied", file=sys.stderr)
        print(json.dumps({'identifier': i['identifier'], 'rework': cause, 'label_applied': labelled}))
    elif cmd == 'triage':
        disp = TRIAGE_ALIASES.get(a[2], a[2]); reason = a[3]
        if disp not in TRIAGE_DISPOSITIONS:
            raise SystemExit('disposition must be one of ' + '|'.join(sorted(TRIAGE_DISPOSITIONS)) + ' (or accept|decline|defer)')
        if not str(reason).strip() or str(reason).startswith('--'):
            raise SystemExit('a triage disposition needs an explicit non-empty reason')
        if '--state' in a and a.index('--state') + 1 == len(a):
            raise SystemExit('missing target state after --state')
        candidate = i['state']['name'] == 'Triage'
        candidate_admission = candidate and disp == 'admitted'
        for flag in ('--root', '--basis', '--blocks', '--project', '--type', '--priority'):
            if flag in a and (a.index(flag) + 1 == len(a) or a[a.index(flag) + 1].startswith('--')):
                raise SystemExit('missing value after ' + flag)
        if not candidate_admission and any(flag in a for flag in ('--root', '--basis', '--blocks')):
            raise SystemExit('--root/--basis/--blocks require an admitted Triage candidate; inspect before replay')
        default_target = ('Todo' if candidate_admission else
                          'Triage' if candidate and disp in ('deferred', 'needs-input') else
                          TRIAGE_TARGET_STATE.get(disp))
        target = a[a.index('--state') + 1] if '--state' in a else default_target
        if not target or target not in st or not st[target]:
            raise SystemExit('missing or unknown target state: ' + str(target))
        if target == 'Done':
            raise SystemExit('Done requires the pass path and a validated RESULT')
        if candidate and disp in ('deferred', 'needs-input') and target != 'Triage':
            raise SystemExit('pending candidates must remain Triage; use explicit admitted with --root and --basis')
        if candidate and disp != 'admitted' and target not in TRIAGE_SHAPE_OK_STATES:
            raise SystemExit('candidate execution requires explicit admitted with --root and --basis')
        if candidate_admission and target != 'Todo':
            raise SystemExit('candidate admission targets Todo under the active root')
        update, warnings = {}, []
        # ACCEPTANCE_REQUIRED_ON_ADMIT (2026-09-15): an issue with no written '## Acceptance'
        # cannot be lint-verified - result_lint fails closed on it - so it must not be admitted.
        _desc = i.get('description') or ''
        # Only an ADMISSION needs a written acceptance. Applying this to deferred/rejected/duplicate/
        # needs-input made every non-admit disposition impossible (audit 2026-09-16 H13: 38 candidates,
        # 27 older than a day, zero dispositions - the verb refused all of them), which is the opposite of
        # what a triage queue is for.
        if disp == 'admitted' and '## Acceptance' not in _desc:
            raise SystemExit(json.dumps({'error': 'admit refused: issue has no "## Acceptance" section; result_lint fails closed on unverifiable acceptance. Add the written acceptance first.'}))
        if disp == 'admitted' or triage_placement_problem(target, (i.get('project') or {}).get('name'), label_names(i)):
            # Refuse BEFORE any comment or write: a half-admission is the defect, not a recoverable state.
            if '--project' not in a or '--type' not in a:
                raise SystemExit(disp + ' needs --project "<name>" and --type <' + '|'.join(ISSUE_TYPES) + '>: '
                                 'an admission must leave project "Triage" and the `triage` label behind in one operation')
            project_name = a[a.index('--project') + 1]; type_name = a[a.index('--type') + 1]
            if type_name not in ISSUE_TYPES: raise SystemExit('--type must be one of ' + '|'.join(ISSUE_TYPES))
            if project_name == 'Triage': raise SystemExit('--project "Triage" is not an admission; name the real domain project')
            available = projects()
            if project_name not in available:
                raise SystemExit('no such project: ' + project_name + ' (have: ' + ', '.join(sorted(available)) + ')')
            have = {l['name']: l['id'] for l in (i.get('labels') or {}).get('nodes', [])}
            keep = {v for k, v in have.items() if k != 'triage' and not k.startswith('type:')}
            type_id = have.get('type:' + type_name) or team_label_id('type:' + type_name)
            if type_id is None: warnings.append("label 'type:" + type_name + "' does not exist yet — NOT applied")
            else: keep.add(type_id)
            update = {'projectId': available[project_name], 'labelIds': sorted(keep),
                      'priority': int(a[a.index('--priority') + 1]) if '--priority' in a else 3}
        root = blocked = admission_client = None
        if candidate_admission:
            basis = a[a.index('--basis') + 1] if '--basis' in a else None
            if basis not in ('operator-priority', 'escaped-safety-revenue-defect', 'current-acceptance-dependency'):
                raise SystemExit('--basis must be operator-priority, escaped-safety-revenue-defect, or current-acceptance-dependency; labels grant no authority')
            if '--root' not in a:
                raise SystemExit('candidate admission requires --root naming the current active root')
            if basis == 'current-acceptance-dependency' and '--blocks' not in a:
                raise SystemExit('current-acceptance-dependency requires --blocks naming the acceptance work')
            root = issue(a[a.index('--root') + 1])
            from linear_client import Client
            admission_client = Client()
            require_active_root(admission_client, root)
            if (i.get('team') != root['team'] or i['id'] == root['id']
                    or ((i.get('parent') or {}).get('id') not in (None, root['id']))):
                raise SystemExit('candidate belongs to a different root or is the active root')
            update['parentId'] = root['id']
            if '--blocks' in a:
                blocked = issue(a[a.index('--blocks') + 1])
                # Reuse repair scope and one-blocker guards on validated future placement.
                prospective = dict(i, project={'name': project_name}, state={'name': 'Todo'},
                                   labels={'nodes': [{'name': n} for n in have if n != 'triage']})
                admission_client = repair_admission_client(prospective, root, blocked)
                blocking_id = team_label_id('blocking')
                if not blocking_id:
                    raise SystemExit('blocking label missing; cannot admit repair')
                update['labelIds'] = sorted(set(update['labelIds']) | {blocking_id})
            reason = f'Admission basis: {basis}\nRoot: {root["identifier"]}\n{reason}'
        from linear_client import ACTIVE_STATES
        with root_activation_lock() if target in ACTIVE_STATES else nullcontext():
            guard_root_activation(dict(i, parent={'id': update['parentId']}) if 'parentId' in update else i, target)
            # Establish the optional edge before making work executable. On ambiguity,
            # native helpers refuse; do not blindly replay a partially applied admission.
            if blocked:
                add_blocks(i['id'], blocked['id'], admission_client)
            cid = comment(i['id'], reason)['comment']['id']
            if target != i['state']['name']:
                update['stateId'] = st[target]
            if update:
                changed = gql('mutation($id:String!,$i:IssueUpdateInput!){ issueUpdate(id:$id,input:$i){ success } }', {'id': i['id'], 'i': update})['issueUpdate']
                if changed.get('success') is not True:
                    raise SystemExit('disposition update unconfirmed; inspect before any further action')
        back = issue(i['identifier'])
        if (back.get('id') != i['id'] or (back.get('state') or {}).get('id') != st[target]
                or (back.get('state') or {}).get('name') != target):
            raise SystemExit('disposition state readback unconfirmed; inspect before any further action')
        if candidate_admission and (
                (back.get('parent') or {}).get('id') != root['id']
                or (back.get('project') or {}).get('id') != update['projectId']
                or back.get('priority') != update['priority']
                or {row['id'] for row in (back.get('labels') or {}).get('nodes', [])} != set(update['labelIds'])):
            raise SystemExit('candidate admission readback unconfirmed; inspect before any further action')
        out = {'identifier': back['identifier'], 'disposition': disp, 'state': back['state']['name'],
               'project': (back.get('project') or {}).get('name'), 'priority': back.get('priority'),
               'labels': label_names(back)}
        if candidate_admission:
            out.update(parent_id=(back.get('parent') or {}).get('id'), basis=basis,
                       blocks=blocked['identifier'] if blocked else None)
        problem = triage_placement_problem(out['state'], out['project'], out['labels'])
        if problem: warnings.append('PLACEMENT INVARIANT VIOLATED: ' + problem)
        for w in warnings: print('WARNING: ' + w, file=sys.stderr)
        out['warnings'] = warnings
        print(json.dumps(out))
        if problem: raise SystemExit(1)
        resolve_bound_request(back, reason, cid)
    else: print(__doc__)
if __name__ == '__main__': sys.exit(main() or 0)
