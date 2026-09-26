#!/usr/bin/env python3
"""Evidence-limited project updates on the existing Linear reconciler cadence.

Remote project updates are the deduplication ledger. No issue state, project description,
or human-authored update is changed. Issue state is not business-outcome acceptance.
"""
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone

from linear_client import Client, LinearError, post_project_update

PROJECTS = '''query($after:String){projects(first:50,after:$after){nodes{
 id name description state status{id name type} archivedAt}pageInfo{hasNextPage endCursor}}}'''
ISSUES = '''query($filter:IssueFilter,$after:String){issues(first:50,after:$after,filter:$filter){nodes{
 id identifier title parent{id} state{name type} project{id name}
 assignee{name} delegate{name} labels(first:50){nodes{name}pageInfo{hasNextPage endCursor}}
 }pageInfo{hasNextPage endCursor}}}'''
LATEST = '''query($id:String!){project(id:$id){projectUpdates(first:2){nodes{
 id body health createdAt}pageInfo{hasNextPage endCursor}}}}'''
MARKER = '<!-- glp-dashboard-sync-v1 sha256:{digest} date:{day} -->'
ACTIVE = ('In Progress', 'In Review')
CLOSED = ('Done', 'Canceled', 'Duplicate')


def _clean(text, limit=140):
    return ' '.join(str(text).split())[:limit].replace('`', "'")


def _validate(row):
    if (not isinstance(row, dict) or not row.get('id') or not row.get('identifier')
            or not isinstance(row.get('title'), str)
            or not isinstance(row.get('state'), dict) or not row['state'].get('name')
            or not isinstance(row.get('project'), (dict, type(None)))
            or not isinstance(row.get('labels'), dict)
            or not isinstance(row['labels'].get('nodes'), list)
            or row['labels'].get('pageInfo', {}).get('hasNextPage')):
        raise LinearError('Incomplete Linear issue snapshot; no project status posted')
    return row


def build_status(project, rows, owners, sampled_at):
    """Return body, conservative health and stable digest of meaningful issue facts."""
    facts = sorted(({'id': r['identifier'], 'title': r['title'],
                     'state': r['state']['name'],
                     'owner': owners.get(r['identifier']),
                     'labels': sorted(x['name'] for x in r['labels']['nodes']
                                      if x['name'] == 'blocking' or x['name'].startswith('lane:')),
                     'assignee': (r.get('assignee') or {}).get('name'),
                     'delegate': (r.get('delegate') or {}).get('name')}
                    for r in rows), key=lambda x: x['id'])
    digest = hashlib.sha256(json.dumps({'project': project['id'], 'state': project['state'],
                                          'issues': facts}, sort_keys=True).encode()).hexdigest()
    blocked = [r for r in facts if r['state'] == 'Blocked']
    active = [r for r in facts if r['state'] in ACTIVE and r not in blocked]
    closed = [r for r in facts if r['state'] == 'Done']
    canceled = [r for r in facts if r['state'] in ('Canceled', 'Duplicate')]
    queued = [r for r in facts if r['state'] not in ACTIVE + CLOSED + ('Blocked',) and r not in blocked]
    verified_active = any(r['owner'] for r in active)
    snapshot = 'Active dispatch observed' if verified_active else (
        'Queued / live seat unconfirmed' if active or queued or blocked else 'No open issues')
    lines = [f'## {_clean(project["name"])} — {snapshot}',
             f'Sampled {sampled_at} from paginated Linear GLP issues. Project state: `{project["state"]}`.',
             f'Issue states (not proof of business outcome): active: {len(active)}; blocked: {len(blocked)}; '
             f'queued: {len(queued)}; closed: {len(closed)}; canceled: {len(canceled)}; total: {len(facts)}.']
    for r in (active + blocked)[:12]:
        ownership = f' — last dispatch {_clean(r["owner"])} (pane busy)' if r['owner'] else (
            f' — Linear owner {_clean(r["delegate"] or r["assignee"])}; live seat unconfirmed'
            if r['delegate'] or r['assignee'] else ' — live seat unconfirmed')
        lines.append(f'- {r["id"]} [{r["state"]}]: {_clean(r["title"])}{ownership}')
    if len(active + blocked) > 12:
        lines.append(f'- {len(active + blocked)-12} more active/blocked issues (counted above).')
    if blocked:
        r = blocked[0]
        lines.append(f'Next: resolve/check blocker {r["id"]} ({_clean(r["title"])}); prerequisite not inferred from title.')
    elif active:
        lines.append(f'Next: {active[0]["id"]} needs a verified result/acceptance; state alone is not completion.')
    elif queued:
        lines.append(f'Next: admission/owner dispatch for {queued[0]["id"]}; no execution inferred.')
    else:
        lines.append('Next: no open issue in this project; monitor new intake. Closed is a Linear state, not release proof.')
    # A transport/state snapshot cannot establish business health as green.
    health = 'offTrack' if blocked else 'atRisk'
    return '\n'.join(lines), health, digest


def reconcile_project_lifecycle(client, projects, rows):
    """Start only real projects containing a started, admitted GLP top-level root."""
    active = defaultdict(list)
    for row in rows:
        if (row['state'].get('type') == 'started' and not row.get('parent') and row.get('project')
                and not any(label['name'] == 'triage' for label in row['labels']['nodes'])
                and row['project']['name'] != 'Triage'):
            active[row['project']['id']].append(row['identifier'])
    known = {p['id']: p for p in projects}
    if set(active) - set(known):
        raise LinearError('Active root references an unknown project; no lifecycle mutation')
    pending = []
    for pid, root_ids in active.items():
        p = known[pid]
        status = p.get('status') or {}
        if p.get('archivedAt') or status.get('type') in ('completed', 'canceled'):
            raise LinearError('Active root in archived/terminal project; no lifecycle mutation')
        if not status.get('id') or status.get('type') != p['state']:
            raise LinearError('Incomplete/inconsistent project status snapshot; no lifecycle mutation')
        if status['type'] == 'started':
            continue
        if status['type'] not in ('backlog', 'planned'):
            raise LinearError('Unexpected lifecycle state for active root project')
        pending.append((p, root_ids))
    if not pending:
        return []
    statuses = client.gql('query{projectStatuses(first:50){nodes{id name type} '
                          'pageInfo{hasNextPage}}}')['projectStatuses']
    started = [s for s in statuses['nodes'] if s['type'] == 'started']
    if statuses['pageInfo']['hasNextPage'] or len(started) != 1 or not started[0]['id']:
        raise LinearError('Unique live started project status not established')
    changed = []
    for p, roots in pending:
        result = client.gql('mutation($id:String!,$i:ProjectUpdateInput!){'
                            'projectUpdate(id:$id,input:$i){success project{id}}}',
                            {'id': p['id'], 'i': {'statusId': started[0]['id']}})['projectUpdate']
        if not result['success'] or (result.get('project') or {}).get('id') != p['id']:
            raise LinearError('Project lifecycle mutation ambiguous; reconcile exact ID', ambiguous=True)
        back = client.gql('query($id:String!){project(id:$id){id name description state '
                          'status{id name type}}}', {'id': p['id']})['project']
        if (not back or back['id'] != p['id'] or back['name'] != p['name']
                or back['description'] != p['description'] or back['state'] != 'started'
                or back['status']['id'] != started[0]['id'] or back['status']['type'] != 'started'):
            raise LinearError('Project lifecycle exact readback failed; reconcile before replay', ambiguous=True)
        p['state'], p['status'] = back['state'], back['status']
        changed.append({'project': p['name'], 'project_id': p['id'], 'roots': sorted(roots),
                        'from': status['type'], 'to': 'started'})
    return changed


def sync_projects(client: Client, owners: dict, now=None):
    if client.role != 'orchestrator':
        raise LinearError('Dashboard sync requires explicit orchestrator role')
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    day = stamp[:10]
    projects = client.pages(PROJECTS, {}, ('projects',))
    rows = client.pages(ISSUES, {'filter': {'team': {'key': {'eq': 'GLP'}}}}, ('issues',))
    groups = defaultdict(list)
    for row in rows:
        _validate(row)
        if row['project']:
            groups[row['project']['id']].append(row)
    project_status_changed = reconcile_project_lifecycle(client, projects, rows)
    created, skipped = [], []
    for p in projects:
        if p.get('archivedAt') or p.get('state') in ('canceled', 'completed'):
            continue
        if not p.get('id') or not p.get('name') or not p.get('state'):
            raise LinearError('Incomplete Linear project snapshot')
        body, health, digest = build_status(p, groups[p['id']], owners, stamp)
        previous = client.gql(LATEST, {'id': p['id']})['project']
        if not previous or not previous.get('projectUpdates'):
            raise LinearError('Missing project updates snapshot')
        updates = previous['projectUpdates']['nodes']
        marker = MARKER.format(digest=digest, day=day)
        if updates and marker in updates[0]['body'] and updates[0]['health'] == health \
                and body.splitlines()[2] in updates[0]['body']:
            skipped.append({'project': p['name'], 'reason': 'unchanged_today'})
            continue
        # Do not supersede a human update placed after our last automated post.
        if len(updates) > 1 and 'glp-dashboard-sync-v1' not in updates[0]['body'] \
                and 'glp-dashboard-sync-v1' in updates[1]['body']:
            skipped.append({'project': p['name'], 'reason': 'newer_human_update'})
            continue
        body += '\n\n' + marker
        result = post_project_update(client, p['name'], body, health)
        if ((result.get('project') or {}).get('id') != p['id'] or result.get('health') != health
                or result.get('body') != body or not result.get('id')):
            raise LinearError('Project update exact readback failed; reconcile before replay', ambiguous=True)
        created.append({'id': result['id'], 'project': p['name'], 'project_id': p['id'],
                        'health': health, 'body': body, 'url': result.get('url')})
    return {'sampled_at': stamp, 'projects': len(projects), 'issues': len(rows),
            'project_status_changed': project_status_changed, 'created': created, 'skipped': skipped}
