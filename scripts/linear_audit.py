#!/usr/bin/env python3
"""Read-only Linear governance audit. No entity repairs or implicit sends."""
import json
import re
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / 'reports/ops/orchestration-restore-20260907/PLAN/L1/item9-audit'
CHANNEL = '1487300132054765629'


def durable_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temp): os.unlink(temp)


@contextmanager
def exclusive(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def notice_chunks(report):
    lines = [f"{f['identifier']} {f['id']} {f['reason']} {f['url']}" for f in report['findings']]
    review = report.get('triage_review')
    if review and review['candidates']:
        lines.append('Triage review ' + review['date'] + ': candidates are NOT admitted work.')
        lines.extend(f"{r['identifier']} review candidate {r['url']}" for r in review['candidates'])
    if report['status'] == 'ERROR':
        lines.insert(0, 'AUDIT_INCOMPLETE; unresolved total UNKNOWN; inspect local audit evidence')
    if not lines:
        return []
    digest = hashlib.sha256(json.dumps(lines, separators=(',',':')).encode()).hexdigest()
    header = 'Linear audit '+digest+'\n'
    chunks, current = [], header
    for line in lines:
        require(len(header)+len(line)+1 <= 1900)
        if len(current)+len(line)+1 > 1900:
            chunks.append(current.rstrip()); current=header
        current += line+'\n'
    chunks.append(current.rstrip())
    return chunks


def notify(report, state, transport=None):
    """Only called under explicit --notify; durable intent before one POST."""
    if transport is None:
        import post_discord_message as transport
    state = Path(state)
    try:
        with exclusive(str(state)+'.lock'):
            marker = Path(str(state)+'.initialized')
            if state.is_symlink() or (marker.exists() and not state.exists()) or (state.exists() and state.stat().st_size == 0):
                return 'DELIVERY_UNKNOWN'
            existing = state.exists()
            # Owner-only DB; retain it across runs. Never delete to retry a send.
            fd = os.open(state, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            db = sqlite3.connect(state)
            try:
                db.execute('PRAGMA synchronous=FULL')
                if existing:
                    require(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery'").fetchone() is not None)
                if marker.exists():
                    require(json.loads(marker.read_text()) == {'schema':'linear-audit-delivery-v1'})
                db.execute('CREATE TABLE IF NOT EXISTS delivery (digest TEXT PRIMARY KEY, channel TEXT NOT NULL, content TEXT NOT NULL, message_id TEXT, status TEXT NOT NULL)')
                db.commit()
                if not marker.exists():
                    durable_json(marker, {'schema':'linear-audit-delivery-v1'})
                chunks = notice_chunks(report)
                required_digests = {hashlib.sha256(content.encode()).hexdigest() for content in chunks}
                for digest, channel, content, mid, status in db.execute('SELECT * FROM delivery').fetchall():
                    require(channel == CHANNEL and status in {'PENDING','CONFIRMED'})
                    require(hashlib.sha256(content.encode()).hexdigest() == digest)
                    if status == 'CONFIRMED':
                        require(bool(mid)); continue
                    # Preserve old uncertain effects verbatim, but an unrelated
                    # historical report is not a delivery requirement today.
                    # Its exact digest remains fenced if needed again.
                    if digest not in required_digests:
                        continue
                    if not mid:
                        return 'DELIVERY_UNKNOWN'
                    fetched = transport.readback(channel, mid)
                    if fetched.get('id') != mid or fetched.get('channel_id') != channel or fetched.get('content') != content:
                        return 'DELIVERY_UNKNOWN'
                    db.execute('UPDATE delivery SET status=? WHERE digest=?', ('CONFIRMED',digest)); db.commit()
                if not chunks:
                    return 'NOT_NEEDED'
                sent = False
                for content in chunks:
                    digest = hashlib.sha256(content.encode()).hexdigest()
                    if db.execute('SELECT 1 FROM delivery WHERE digest=?', (digest,)).fetchone():
                        continue
                    db.execute('INSERT INTO delivery VALUES (?,?,?,?,?)', (digest,CHANNEL,content,None,'PENDING')); db.commit()
                    payload = transport.send(CHANNEL, content)
                    mid = payload.get('id')
                    if not isinstance(mid, str) or not re.fullmatch(r'[0-9]+', mid):
                        return 'DELIVERY_UNKNOWN'
                    db.execute('UPDATE delivery SET message_id=? WHERE digest=?', (mid,digest)); db.commit()
                    fetched = transport.readback(CHANNEL, mid)
                    if fetched.get('id') != mid or fetched.get('channel_id') != CHANNEL or fetched.get('content') != content:
                        return 'DELIVERY_UNKNOWN'
                    db.execute('UPDATE delivery SET status=? WHERE digest=?', ('CONFIRMED',digest)); db.commit()
                    sent = True
                return 'CONFIRMED' if sent else 'DUPLICATE_CONFIRMED'
            finally:
                db.close()
    except Exception:
        # Includes lock conflict, malformed state, API failure and lost receipts.
        return 'DELIVERY_UNKNOWN'


class AuditError(RuntimeError):
    """Fixed safe error codes only; never stringify upstream errors."""


def require(condition):
    if not condition:
        raise AuditError('SCHEMA_UNKNOWN')


def object_id(value, nullable=False):
    if value is None and nullable:
        return
    require(isinstance(value, dict) and isinstance(value.get('id'), str) and bool(value['id']))


def validate(row):
    require(isinstance(row, dict))
    for key in ('id', 'identifier', 'url', 'createdAt', 'updatedAt', 'archivedAt', 'trashed', 'parent', 'project', 'state', 'labels', 'comments', 'history'):
        require(key in row)
    require(isinstance(row['id'], str) and bool(re.fullmatch(r'[A-Za-z0-9-]{1,64}', row['id'])))
    require(isinstance(row['identifier'], str) and bool(re.fullmatch(r'GLP-[0-9]+', row['identifier'])))
    require(isinstance(row['url'], str) and bool(re.match(r'https://linear\.app/[a-zA-Z0-9_-]+/issue/'+re.escape(row['identifier'])+r'(?:/|$)', row['url'])))
    instant(row['createdAt']); instant(row['updatedAt'])
    object_id(row['parent'], True); object_id(row['project'], True); object_id(row['state'])
    require(row['state']['name'] in {'Triage','Backlog','Todo','In Progress','In Review','Blocked','Done','Canceled','Duplicate'})
    for kind in ('labels', 'comments', 'history'):
        require(isinstance(row[kind], list))
        ids = set()
        for item in row[kind]:
            object_id(item)
            require(item['id'] not in ids); ids.add(item['id'])
            if kind == 'labels':
                require(isinstance(item['name'], str))
            else:
                instant(item['createdAt'])
                if kind == 'comments':
                    require(isinstance(item['body'], str))
                    object_id(item['user'], True)
                    for key in ('editedAt','archivedAt'):
                        if item[key] is not None: instant(item[key])
                else:
                    object_id(item['actor'], True)
                    require(bool(re.fullmatch(r'[A-Za-z0-9-]{1,64}', item['id'])))
                    for key in ('fromParentId','toParentId','fromStateId','toStateId'):
                        require(item[key] is None or isinstance(item[key], str))
                    for prefix in ('from','to'):
                        value = item[prefix+'State']; object_id(value, True)
                        if value is not None:
                            require(value['id'] == item[prefix+'StateId'] and isinstance(value['name'], str))


def audit(issues, orchestrator_id, now):
    require(isinstance(issues, list) and isinstance(orchestrator_id, str) and bool(orchestrator_id))
    instant(now)
    from zoneinfo import ZoneInfo
    review = {'date': instant(now).astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat(), 'candidates': []}
    findings, unknown, ids = [], 0, set()
    for row in issues:
        if row.get('archivedAt') or row.get('trashed'): continue  # archived/trashed issues are out of audit scope
        try:
            validate(row)
            require(row['id'] not in ids); ids.add(row['id'])
            checked = _audit_valid([row], orchestrator_id, now)
            findings.extend(checked['findings'])
            review['candidates'].extend(checked['triage_review']['candidates'])
        except (AuditError, KeyError, TypeError, ValueError, AttributeError):
            unknown += 1
            # Only expose a malformed row's identity after independent validation.
            try:
                safe = {k:row[k] for k in ('id','identifier','url')}
                require(isinstance(safe['id'],str) and bool(re.fullmatch(r'[A-Za-z0-9-]{1,64}',safe['id'])))
                require(isinstance(safe['identifier'],str) and bool(re.fullmatch(r'GLP-[0-9]+',safe['identifier'])))
                require(isinstance(safe['url'],str) and bool(re.match(r'https://linear\.app/[a-zA-Z0-9_-]+/issue/'+re.escape(safe['identifier'])+r'(?:/|$)',safe['url'])))
                safe['url'] = safe['url'].split('/issue/')[0]+'/issue/'+safe['identifier']
                findings.append(dict(safe,reason='SCHEMA_UNKNOWN'))
            except (AuditError, KeyError, TypeError):
                pass
    findings.sort(key=lambda f:(f['identifier'], f['reason']))
    review['candidates'].sort(key=lambda r: r['identifier'])
    return {'status': 'ERROR' if unknown else ('FINDINGS' if findings else 'CLEAN'),
            'complete': not unknown, 'read_only': True, 'issue_count': len(issues),
            'unresolved_count': None if unknown else len(findings), 'known_finding_count': len(findings),
            'unknown_count': unknown + sum('_UNKNOWN:' in f['reason'] for f in findings),
            'errors': ['SCHEMA_UNKNOWN'] if unknown else [], 'findings': findings, 'triage_review': review}


PAGE = 'pageInfo { hasNextPage endCursor }'
CHILD_FIELDS = {
    'labels': 'id name',
    'comments': 'id body createdAt editedAt archivedAt user { id }',
    'history': 'id createdAt fromStateId toStateId fromState { id name } toState { id name } actor { id } fromParentId toParentId',
}
BASE_FIELDS = 'id identifier url createdAt updatedAt archivedAt trashed parent { id } project { id } state { id name type }'
ISSUES_QUERY = 'query($team:ID!,$after:String){issues(first:25,after:$after,includeArchived:true,filter:{team:{id:{eq:$team}}}){nodes{'+BASE_FIELDS+' '+ ' '.join(k+'(first:10){nodes{'+v+'}'+PAGE+'}' for k,v in CHILD_FIELDS.items())+'}'+PAGE+'}}'
STAMP_QUERY = 'query($team:ID!,$after:String){issues(first:100,after:$after,includeArchived:true,filter:{team:{id:{eq:$team}}}){nodes{id updatedAt}'+PAGE+'}}'


def pages(client, query, variables, path, first=None):
    """Strict full connection. An error invalidates all partial rows."""
    rows, ids, cursors = [], set(), set()
    variables = dict(variables, after=None)
    value = first
    while True:
        if value is None:
            try:
                value = client.gql(query, variables)
                for part in path:
                    value = value[part]
            except Exception:
                raise AuditError('READ_OR_CONNECTION_UNKNOWN') from None
        try:
            require(isinstance(value, dict) and isinstance(value['nodes'], list))
            page_info = value['pageInfo']
            require(type(page_info['hasNextPage']) is bool and 'endCursor' in page_info)
            for node in value['nodes']:
                object_id(node)
                require(node['id'] not in ids)
                ids.add(node['id']); rows.append(node)
            if not page_info['hasNextPage']:
                return rows
            cursor = page_info['endCursor']
            require(isinstance(cursor, str) and bool(cursor) and cursor not in cursors)
            cursors.add(cursor); variables['after'] = cursor
            value = None
        except (KeyError, TypeError, AuditError):
            raise AuditError('PAGINATION_UNKNOWN') from None


ISSUE_QUERY = 'query($id:String!){issue(id:$id){'+BASE_FIELDS+' '+ ' '.join(k+'(first:10){nodes{'+v+'}'+PAGE+'}' for k,v in CHILD_FIELDS.items())+'}}'


def hydrate(client, rows):
    """Replace each row's truncated child connections with the full paginated lists."""
    for row in rows:
        for kind, fields in CHILD_FIELDS.items():
            query = 'query($id:String!,$after:String){issue(id:$id){'+kind+'(first:100,after:$after){nodes{'+fields+'}'+PAGE+'}}}'
            try:
                first = row[kind]
                require(first is not None)
                row[kind] = pages(client, query, {'id':row['id']}, ('issue',kind), first=first)
            except (KeyError, TypeError):
                raise AuditError('CONNECTION_UNKNOWN') from None
    return rows


def reread(client, issue_id):
    """Re-read one issue whole, for the single snapshot retry."""
    try:
        node = client.gql(ISSUE_QUERY, {'id':issue_id})['issue']
    except Exception:
        raise AuditError('READ_OR_CONNECTION_UNKNOWN') from None
    try:
        object_id(node)
        require(node['id'] == issue_id)
    except (KeyError, TypeError, AuditError):
        raise AuditError('SNAPSHOT_CHANGED_OR_UNKNOWN') from None
    return hydrate(client, [node])[0]


def stamp_map(client, team_id):
    try:
        return {r['id']:r['updatedAt'] for r in pages(client, STAMP_QUERY, {'team':team_id}, ('issues',))}
    except (KeyError, TypeError, AuditError):
        raise AuditError('SNAPSHOT_CHANGED_OR_UNKNOWN') from None


def fetch(client, team_id):
    """Consistent census of the audited issues.

    Only the issues actually audited have to match: an issue created mid-run is simply not in this
    snapshot, and one that moved mid-run is re-read once. Comparing the whole team census verbatim
    turned every concurrent lane comment into status=ERROR with an empty findings list
    (reports/ops/efficiency-20260911/AUDIT-LINEAR-SETUP-VS-PRACTICE.md §5B)."""
    rows = hydrate(client, pages(client, ISSUES_QUERY, {'team':team_id}, ('issues',)))
    for attempt in (0, 1):
        stamps = stamp_map(client, team_id)
        try:
            moved = [r for r in rows if stamps.get(r['id']) != r['updatedAt']]
            gone = [r for r in rows if r['id'] not in stamps]
        except (KeyError, TypeError):
            raise AuditError('SNAPSHOT_CHANGED_OR_UNKNOWN') from None
        if not moved and not gone:
            return rows
        if attempt or gone:
            raise AuditError('SNAPSHOT_CHANGED_OR_UNKNOWN') from None
        fresh = {r['id']: r for r in (reread(client, m['id']) for m in moved)}
        rows = [fresh.get(r['id'], r) for r in rows]
    raise AuditError('SNAPSHOT_CHANGED_OR_UNKNOWN')

LANES = {'lane:'+v for v in ('kael', 'design', 'scout', 'ads', 'fable')}
CLASSES = {'class:'+v for v in ('release', 'machine', 'research', 'ads', 'seo', 'marketplace', 'ops', 'operator-decision')}

SOP_INSTALLED_AT = '2026-09-11T00:10:00.000Z'
ADJUDICATED_PATH = '/home/ubuntu/business/ops/linear/audit-adjudicated.json'  # {history_id: reason} adjudicated by the orchestrator (SOP: historical unstructured receipts are findings for adjudication)
try:
    import json as _j; ADJUDICATED = _j.load(open(ADJUDICATED_PATH))
except Exception:
    ADJUDICATED = {}  # transitions before the SOP install are pre-SOP: reported once as INFO, not findings
RESULT_PATH = re.compile(r'(?<![\w/])/(?:[A-Za-z0-9_.-]+/)+RESULT\.json(?![\w.])')


def instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone required')
    return parsed


def receipt(body, marker):
    match = re.fullmatch(re.escape(marker)+r'\s*```json\s*(\{.*\})\s*```\s*', body, re.S)
    if not match:
        return None
    try:
        value = json.loads(match[1])
        return value if isinstance(value, dict) else None
    except ValueError:
        return None


def _audit_valid(issues, orchestrator_id, now):
    findings = []
    now = instant(now)
    for row in issues:
        if row.get('archivedAt') or row.get('trashed'): continue  # archived/trashed issues are out of audit scope
        # A canceled issue is discontinued work: its shape (project, lane/class labels) no longer has to be
        # valid, and demanding it produces findings nobody can act on. Transition receipts are still audited.
        canceled = (row.get('state') or {}).get('type') == 'canceled'
        def add(reason):
            findings.append({'id': row['id'], 'identifier': row['identifier'], 'reason': reason,
                             'url': row['url'].split('/issue/')[0] + '/issue/' + row['identifier']})
        if row['project'] is None and not canceled:
            add('MISSING_PROJECT')
        for prefix, valid, reason in ([] if canceled else
                                      [('lane:', LANES, 'INVALID_LANE'), ('class:', CLASSES, 'INVALID_CLASS')]):
            values = [v['name'] for v in row['labels'] if v['name'].startswith(prefix)]
            if len(values) != 1 or values[0] not in valid:
                add(reason)
        if row.get('archivedAt') or row.get('trashed'): continue  # SKIP_ARCHIVED: archived/trashed issues are out of audit scope
        comments = [c for c in row['comments'] if c['archivedAt'] is None]
        used = set()
        parent = (row['parent'] or {}).get('id')
        events = sorted(row['history'], key=lambda h: (instant(h['createdAt']), h['id']), reverse=True)
        main_events = []
        for h in events:
            # A combined parent/state edit counts if either side is top-level.
            changed_parent = h['fromParentId'] != h['toParentId']
            if parent is None or (changed_parent and h['fromParentId'] is None):
                main_events.append(h)
            if changed_parent:
                parent = h['fromParentId']
        for h in reversed(main_events):
            if h['fromStateId'] == h['toStateId']:
                continue  # Both null = a non-state history event, not a transition.
            when = instant(h['createdAt'])
            if not instant(row['createdAt']) <= when <= now:
                add('TRANSITION_TIME_UNKNOWN:'+h['id'])
            actor = (h['actor'] or {}).get('id')
            initial = h['fromStateId'] is None and 0 <= (when-instant(row['createdAt'])).total_seconds() <= 2
            if initial and (h['toState'] or {}).get('name') == 'Triage':
                if actor is None:
                    add('INITIAL_ACTOR_UNKNOWN:'+h['id'])
                continue
            if initial:
                if actor != orchestrator_id:
                    add('INITIAL_STATE_NON_ORCHESTRATOR:'+h['id'])
                continue
            if any(h[p+'StateId'] is not None and h[p+'State'] is None for p in ('from','to')):
                add('TRANSITION_STATE_UNKNOWN:'+h['id'])
            if h['fromStateId'] is None or h['toStateId'] is None:
                add('TRANSITION_ORIGIN_UNKNOWN:'+h['id'])
            if actor is None:
                add('TRANSITION_ACTOR_UNKNOWN:'+h['id'])
            elif actor != orchestrator_id:
                add('TRANSITION_NON_ORCHESTRATOR:'+h['id'])
            # Native history is the metadata record; comments are not authorization.
            # Done alone still needs the correlated result proof (including legacy closures).
            if (h['toState'] or {}).get('name') != 'Done':
                continue
            matched = False
            for c in sorted(comments, key=lambda c: (instant(c['createdAt']), c['id'])):
                if c['id'] in used or (c['user'] or {}).get('id') != orchestrator_id:
                    continue
                age = (when-instant(c['createdAt'])).total_seconds()
                if not 0 <= age <= 900 or (c['editedAt'] and instant(c['editedAt']) > when):
                    continue
                value = receipt(c['body'], 'FLEET_TRANSITION_V1')
                evidence = value and RESULT_PATH.fullmatch(str(value.get('result_path', '')))
                valid = value and value.get('issue_id') == row['id'] and value.get('from_state_id') == h['fromStateId'] and value.get('to_state_id') == h['toStateId'] and evidence
                if not valid and (h['toState'] or {}).get('name') == 'Done':
                    value = receipt(c['body'], 'FLEET_RESULT_V1')
                    valid = value and value.get('issue_id') == row['id'] and value.get('outcome') == 'PASS' and RESULT_PATH.fullmatch(str(value.get('result_path', ''))) and re.fullmatch(r'[0-9a-f]{64}', str(value.get('result_sha256', '')))
                if valid:
                    used.add(c['id'])
                    matched = True
                    break
            if not matched:
                if h.get('createdAt','') < SOP_INSTALLED_AT or h['id'] in ADJUDICATED: continue
                add('TRANSITION_WITHOUT_RECEIPT:'+h['id'])
        if row['state']['name'] == 'Done' and not any(RESULT_PATH.search(c['body']) for c in comments):
            add('DONE_WITHOUT_RESULT_PATH')
        triage_since = max([instant(h['createdAt']) for h in row['history'] if (h['toState'] or {}).get('name') == 'Triage'] or [instant(row['createdAt'])])
        if row['state']['name'] == 'Triage' and (now-triage_since).total_seconds() > 86400:
            add('STALE_TRIAGE_NO_DISPOSITION')
    from zoneinfo import ZoneInfo
    review = {'date': now.astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat(),
              'candidates': [{'id': row['id'], 'identifier': row['identifier'], 'url': row['url'].split('/issue/')[0]+'/issue/'+row['identifier']}
                             for row in issues if row['state']['name'] == 'Triage'
                             and not row.get('archivedAt') and not row.get('trashed')]}
    return {'status': 'FINDINGS' if findings else 'CLEAN', 'complete': True,
            'issue_count': len(issues), 'unresolved_count': len(findings),
            'unknown_count': 0, 'findings': findings, 'triage_review': review}


def error_report():
    return {'status':'ERROR', 'complete':False, 'read_only':True, 'issue_count':None,
            'unresolved_count':None, 'known_finding_count':None, 'unknown_count':None,
            'errors':['AUDIT_READ_SCHEMA_OR_INPUT_UNKNOWN'], 'findings':[]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--orchestrator-id', required=True, help='Exact verified orchestrator UUID; never inferred from reading actor')
    parser.add_argument('--team-id', default=None, help='Verified GLP team UUID; otherwise resolve GLP with full pagination')
    parser.add_argument('--role', choices=('fleet','orchestrator'), default='fleet')
    parser.add_argument('--fixture', type=Path, help='Offline normalized rows; incompatible with --notify')
    parser.add_argument('--now', help='ISO8601 fixture clock only')
    parser.add_argument('--notify', action='store_true', help='Explicit ops-alerts delivery, durable no-blind-resend state')
    parser.add_argument('--evidence', type=Path, default=ARTIFACTS/'runtime/latest.json')
    parser.add_argument('--notification-state', type=Path, default=ARTIFACTS/'runtime/delivery.sqlite')
    args = parser.parse_args(argv)
    # Semantic argument validation is inside evidence boundary; no raw values echoed.
    report = error_report()
    try:
        from linear_client import uuid_arg
        uuid_arg(args.orchestrator_id)
        if args.team_id is not None: uuid_arg(args.team_id)
        require(not (args.fixture and args.notify))
        require(not args.now or args.fixture)
        with exclusive(str(args.evidence)+'.lock'):
            try:
                if args.fixture:
                    rows = json.loads(args.fixture.read_text())
                else:
                    from linear_client import Client
                    client = Client(role=args.role)
                    team_id = args.team_id or client.team('GLP')['id']
                    rows = fetch(client, team_id)
                report = audit(rows, args.orchestrator_id, args.now or datetime.now(timezone.utc).isoformat())
            except Exception:
                report = error_report()
            report['checked_at'] = datetime.now(timezone.utc).isoformat()
            report['source'] = 'fixture' if args.fixture else 'live'
            report['delivery'] = 'DISABLED'
            durable_json(args.evidence, report)  # Evidence exists before delivery intent.
            if args.notify:
                report['delivery'] = notify(report, args.notification_state)
                durable_json(args.evidence, report)
    except BlockingIOError:
        # The other invocation owns the evidence; do not clobber its receipt.
        print('LINEAR_AUDIT_ERROR: another invocation owns the evidence lock', file=sys.stderr)
        return 2
    except Exception:
        report = error_report()
        try: durable_json(args.evidence, report)
        except Exception: pass
    if report['status'] == 'ERROR' or report.get('delivery') == 'DELIVERY_UNKNOWN':
        print('LINEAR_AUDIT_ERROR: audit or delivery incomplete; inspect local evidence', file=sys.stderr)
        return 2
    if report['findings']:
        for finding in report['findings']:
            print(json.dumps(finding, sort_keys=True))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
