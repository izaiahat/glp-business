import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pytest
from linear_client import LinearError
from linear_project_dashboard import build_status, sync_projects


def issue(identifier, project='One', state='Todo', title='Work', parent=None, labels=()):
    return {'id': identifier, 'identifier': identifier, 'title': title,
            'project': {'id': project, 'name': project}, 'parent': parent,
            'state': {'name': state, 'type': {'Done': 'completed', 'Canceled': 'canceled',
                                              'Backlog': 'backlog', 'Todo': 'unstarted',
                                              'In Progress': 'started'}.get(state, 'started')},
            'labels': {'nodes': [{'name': x} for x in labels]},
            'assignee': None, 'delegate': None}


class Fake:
    role = 'orchestrator'
    def __init__(self, rows):
        self.rows = rows
        self.updates = {}
        self.posts = []
        self.project_rows = [{'id': name, 'name': name, 'description': None,
                              'state': 'backlog', 'status': {'id': 'b', 'type': 'backlog'},
                              'archivedAt': None} for name in ('One', 'Two')]
    def pages(self, query, variables, path):
        if path == ('projects',):
            return self.project_rows
        assert path == ('issues',)
        return self.rows
    def gql(self, query, variables=None):
        if 'projectStatuses' in query:
            return {'projectStatuses': {'nodes': [{'id': 'started-id', 'name': 'In Progress',
                                                   'type': 'started'}],
                                        'pageInfo': {'hasNextPage': False}}}
        if 'projectUpdate(id:' in query:
            p = next(p for p in self.project_rows if p['id'] == variables['id'])
            p['state'] = 'started'
            p['status'] = {'id': variables['i']['statusId'], 'type': 'started'}
            return {'projectUpdate': {'success': True, 'project': {'id': p['id']}}}
        if 'project(id:' in query and 'projectUpdates' not in query:
            p = next(p for p in self.project_rows if p['id'] == variables['id'])
            return {'project': p}
        if 'projectUpdates' in query:
            return {'project': {'projectUpdates': {'nodes': self.updates.get(variables['id'], [])[:2]}}}
        raise AssertionError('unexpected query')


def test_queued_not_inferred_active():
    body, health, _ = build_status({'id': 'One', 'name': 'One', 'state': 'backlog'},
                                    [issue('GLP-1')], {}, '2026-09-25T01:00:00Z')
    assert 'Queued' in body and 'Next: admission' in body and 'active: 0' in body
    assert health != 'onTrack'


def test_blocked_active_closed_and_next_dependency():
    body, health, _ = build_status({'id': 'One', 'name': 'One', 'state': 'backlog'}, [
        issue('GLP-1', state='In Progress'), issue('GLP-2', state='Blocked', title='Need approval'),
        issue('GLP-3', state='Done')], {'GLP-1': 'seat-1'}, '2026-09-25T01:00:00Z')
    assert 'active: 1' in body and 'blocked: 1' in body and 'closed: 1' in body
    assert 'GLP-2' in body and 'Need approval' in body and 'seat-1' in body
    assert health == 'offTrack'


def test_sync_exact_remote_readback_and_unchanged_second_run(monkeypatch):
    import linear_project_dashboard as d
    f = Fake([issue('GLP-1', state='In Progress')])
    def post(c, name, body, health):
        row = {'id': f'u{len(f.posts)+1}', 'body': body, 'health': health,
               'project': {'id': name, 'name': name}, 'url': 'https://linear.app/test'}
        f.updates[name] = [row] + f.updates.get(name, [])
        f.posts.append(row)
        return row
    monkeypatch.setattr(d, 'post_project_update', post)
    now = datetime(2026, 9, 25, 1, tzinfo=timezone.utc)
    first = sync_projects(f, {}, now)
    assert len(first['created']) == 2 and len(f.posts) == 2
    assert {x['project'] for x in first['created']} == {'One', 'Two'}
    second = sync_projects(f, {}, now)
    assert second['created'] == [] and len(f.posts) == 2
    f.rows.append(issue('GLP-2', state='Blocked'))
    third = sync_projects(f, {}, now)
    assert len(third['created']) == 1 and third['created'][0]['project'] == 'One'


def test_fail_closed_on_fleet_role_and_incomplete_snapshot():
    f = Fake([issue('GLP-1')]); f.role = 'fleet'
    with pytest.raises(LinearError):
        sync_projects(f, {}, datetime.now(timezone.utc))
    f.role = 'orchestrator'; f.rows = [{'id': 'GLP-1', 'project': {'id': 'One'}}]
    with pytest.raises(LinearError):
        sync_projects(f, {}, datetime.now(timezone.utc))


def test_daily_refresh_and_human_override(monkeypatch):
    import linear_project_dashboard as d
    f = Fake([issue('GLP-1')])
    def post(c, name, body, health):
        row = {'id': f'u{len(f.posts)+1}', 'body': body, 'health': health,
               'project': {'id': name, 'name': name}}
        f.updates[name] = [row] + f.updates.get(name, [])
        f.posts.append(row)
        return row
    monkeypatch.setattr(d, 'post_project_update', post)
    a = datetime(2026, 9, 25, 1, tzinfo=timezone.utc)
    b = datetime(2026, 9, 26, 1, tzinfo=timezone.utc)
    assert len(sync_projects(f, {}, a)['created']) == 2
    assert len(sync_projects(f, {}, b)['created']) == 2
    f.updates['One'].insert(0, {'id': 'human', 'body': 'Human status', 'health': 'atRisk'})
    result = sync_projects(f, {}, b)
    assert result['created'] == []
    assert any(x['reason'] == 'newer_human_update' for x in result['skipped'])
    assert len(f.posts) == 4


def test_registered_reconciler_syncs_even_without_live_owners(monkeypatch, capsys):
    import linear_state_reconciler as registered
    monkeypatch.setattr(registered, 'live_owners', lambda: {})
    called = []
    assert registered.reconcile(object(), lambda c, owners: called.append(owners) or {'created': []}) == 0
    assert called == [{}]
    assert '"dashboard"' in capsys.readouterr().out


def test_blocking_label_is_not_a_blocked_state():
    p = {'id': 'One', 'name': 'One', 'state': 'backlog'}
    body, health, _ = build_status(p, [issue('GLP-1', state='In Progress', labels=('blocking',))],
                                    {}, '2026-09-25T01:00:00Z')
    assert 'active: 1; blocked: 0' in body
    assert health == 'atRisk'


def test_only_admitted_started_roots_start_their_projects(monkeypatch):
    import linear_project_dashboard as d
    rows = [issue('GLP-7', project='One', state='In Progress'),
            issue('GLP-44', project='Two', state='In Progress', parent={'id': 'root'}),
            issue('GLP-99', project='Triage', state='In Progress', labels=('triage',))]
    f = Fake(rows)
    f.project_rows = [
        {'id': 'One', 'name': 'One', 'description': None, 'state': 'backlog',
         'status': {'id': 'backlog-id', 'type': 'backlog'}, 'archivedAt': None},
        {'id': 'Two', 'name': 'Two', 'description': None, 'state': 'backlog',
         'status': {'id': 'backlog-id', 'type': 'backlog'}, 'archivedAt': None},
        {'id': 'Triage', 'name': 'Triage', 'description': None, 'state': 'backlog',
         'status': {'id': 'backlog-id', 'type': 'backlog'}, 'archivedAt': None}]
    f.mutations = []
    def gql(query, variables=None):
        if 'projectUpdate(id:' in query:
            f.mutations.append(variables)
        return Fake.gql(f, query, variables)
    f.gql = gql
    monkeypatch.setattr(d, 'post_project_update', lambda c, name, body, health: (
        f.updates.setdefault(name, []).insert(0, {'id': 'u-'+name, 'body': body, 'health': health}) or
        {'id': 'u-'+name, 'body': body, 'health': health, 'project': {'id': name}, 'url': 'https://linear.app/test'}))
    first = sync_projects(f, {}, datetime(2026, 9, 25, 1, tzinfo=timezone.utc))
    assert first['project_status_changed'][0]['project'] == 'One'
    assert f.mutations == [{'id': 'One', 'i': {'statusId': 'started-id'}}]
    assert all('Project state: `started`' in x['body'] for x in first['created'] if x['project'] == 'One')
    second = sync_projects(f, {}, datetime(2026, 9, 25, 1, tzinfo=timezone.utc))
    assert second['project_status_changed'] == [] and len(f.mutations) == 1


def test_project_lifecycle_refuses_terminal_project_with_active_root():
    f = Fake([issue('GLP-7', state='In Progress')])
    f.project_rows[0]['state'] = 'completed'
    f.project_rows[0]['status'] = {'id': 'closed-id', 'type': 'completed'}
    with pytest.raises(LinearError):
        sync_projects(f, {}, datetime(2026, 9, 25, 1, tzinfo=timezone.utc))
    assert f.posts == []


def test_no_green_from_closed_only_or_empty():
    p = {'id': 'One', 'name': 'One', 'state': 'backlog'}
    for rows in ([], [issue('GLP-1', state='Done')]):
        body, health, _ = build_status(p, rows, {}, '2026-09-25T01:00:00Z')
        assert health != 'onTrack'
        assert 'not proof of business outcome' in body
