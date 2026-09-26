"""linear_cycle_time derives its metrics from issue.history, not from hand-typed comments.

Offline: the Linear client is replaced with a fake whose gql/pages return fixture rows
(audit 2026-09-11 §3 and Top-10 item 9; RESEARCH-FLEET-QUEUE-PRACTICE.md §4 M1-M7)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def cycle_time():
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('cycle_time_under_test', ROOT / 'scripts/linear_cycle_time.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def child(identifier, **updates):
    row = dict(identifier=identifier, title=identifier + ' title', state={'name': 'Done'},
               createdAt='2026-09-11T10:00:00Z', startedAt='2026-09-11T10:30:00Z', completedAt='2026-09-11T12:00:00Z',
               description=None, parent={'identifier': 'GLP-25', 'startedAt': '2026-09-11T09:00:00Z'},
               labels={'nodes': []}, comments={'nodes': []},
               history={'nodes': [{'createdAt': '2026-09-11T10:30:00Z', 'fromState': {'name': 'Todo'}, 'toState': {'name': 'In Progress'}},
                                  {'createdAt': '2026-09-11T12:00:00Z', 'fromState': {'name': 'In Progress'}, 'toState': {'name': 'Done'}}]})
    row.update(updates)
    return row


NOW = __import__('datetime').datetime(2026, 9, 11, 13, 0, tzinfo=__import__('datetime').timezone.utc)


def test_queue_time_and_cycle_time_come_from_the_timestamps():
    row = cycle_time().compute([child('GLP-100')], {}, NOW)[0]
    assert row['queue_minutes'] == 30 and row['minutes'] == 90


def test_blockers_blocked_duration_and_flow_efficiency_come_from_history():
    blocked = child('GLP-101', history={'nodes': [
        {'createdAt': '2026-09-11T10:30:00Z', 'fromState': {'name': 'Todo'}, 'toState': {'name': 'In Progress'}},
        {'createdAt': '2026-09-11T11:00:00Z', 'fromState': {'name': 'In Progress'}, 'toState': {'name': 'Blocked'}},
        {'createdAt': '2026-09-11T11:30:00Z', 'fromState': {'name': 'Blocked'}, 'toState': {'name': 'In Review'}},
        {'createdAt': '2026-09-11T11:45:00Z', 'fromState': {'name': 'In Review'}, 'toState': {'name': 'In Progress'}},
        {'createdAt': '2026-09-11T12:00:00Z', 'fromState': {'name': 'In Progress'}, 'toState': {'name': 'Done'}}]})
    row = cycle_time().compute([blocked], {}, NOW)[0]
    assert row['blockers'] == 1
    assert row['blocked_minutes'] == 45           # 30 min Blocked + 15 min In Review
    assert row['flow_efficiency'] == 0.5          # (90 - 45) / 90
    assert 'blocked_writes' not in row            # the suppressed-log-line proxy is gone


def test_first_pass_yield_is_false_after_a_done_to_non_done_transition():
    reopened = child('GLP-102', history={'nodes': [
        {'createdAt': '2026-09-11T11:00:00Z', 'fromState': {'name': 'In Progress'}, 'toState': {'name': 'Done'}},
        {'createdAt': '2026-09-11T11:10:00Z', 'fromState': {'name': 'Done'}, 'toState': {'name': 'In Progress'}},
        {'createdAt': '2026-09-11T12:00:00Z', 'fromState': {'name': 'In Progress'}, 'toState': {'name': 'Done'}}]})
    rows = cycle_time().compute([child('GLP-100'), reopened], {}, NOW)
    assert {r['issue']: r['first_pass'] for r in rows} == {'GLP-100': True, 'GLP-102': False}
    assert cycle_time().summarize(rows, 'x')['first_pass_yield'] == 0.5


def test_rework_counts_both_labels_and_legacy_comments():
    module = cycle_time()
    labelled = child('GLP-103', labels={'nodes': [{'name': 'rework:brief'}, {'name': 'lane:ads'}]})
    commented = child('GLP-104', comments={'nodes': [{'body': '**REWORK[machine]** the carrier died'}]})
    both = child('GLP-105', labels={'nodes': [{'name': 'rework:supply'}]},
                 comments={'nodes': [{'body': '**REWORK[supply]** no research packet'}]})
    rows = module.compute([labelled, commented, both], {}, NOW)
    assert [r['rework'] for r in rows] == [{'brief': 1, 'machine': 0, 'supply': 0},
                                           {'brief': 0, 'machine': 1, 'supply': 0},
                                           {'brief': 0, 'machine': 0, 'supply': 2}]
    assert rows[0]['lane'] == 'ads'


def test_weekly_summary_reports_the_full_metric_set():
    module = cycle_time()
    rows = module.compute([
        child('GLP-100'),
        child('GLP-103', labels={'nodes': [{'name': 'rework:brief'}]}),
        child('GLP-106', createdAt='2026-09-11T10:20:00Z', labels={'nodes': [{'name': 'escaped'}]}),
        child('GLP-107', parent={'identifier': 'GLP-21', 'startedAt': '2026-09-11T11:00:00Z'}),
    ], {}, NOW)
    summary = module.summarize(rows, '2026-09-04T00:00:00Z')
    assert summary['done_children'] == 4
    assert summary['p50_minutes'] == 90 and summary['p85_minutes'] == 90
    assert summary['brief_defect_rate'] == 0.25                  # 1 of 4 started
    assert summary['escaped_defects'] == 1
    assert summary['root_inflation'] == {'GLP-21': 0.0, 'GLP-25': 1.0}   # all GLP-25 children created after 09:00
    assert set(summary['blocker_cause_mix']) == {'brief', 'machine', 'supply', 'unattributed'}
    assert summary['first_pass_yield'] == 1.0


def test_monitor_events_log_is_read_once(tmp_path, monkeypatch):
    module = cycle_time()
    log = tmp_path / 'monitor-events.log'
    log.write_text('2026-09-11 key=A11 STALL no output\n2026-09-11 key=A11 NUDGED\n2026-09-11 key=A13 RE-RESUMED\n')
    opened = []
    real_open = module.open if hasattr(module, 'open') else open
    import builtins
    def counting_open(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, 'open', counting_open)
    counts = module.monitor_counts(str(log))
    assert counts['A11'] == {'stalls': 1, 'nudges': 1, 'resumes': 0}
    assert counts['A13']['resumes'] == 1
    assert opened.count(str(log)) == 1


def test_main_runs_against_a_faked_gql(monkeypatch, capsys):
    module = cycle_time()
    calls = []

    class FakeClient:
        def __init__(self, role=None):
            self.role = role
        def gql(self, query, variables=None):
            calls.append(('gql', query))
            return {'issues': {'nodes': [child('GLP-100')], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}
        def pages(self, query, variables, path):
            calls.append(('pages', path))
            return [child('GLP-100'), child('GLP-101', state={'name': 'Backlog'})]

    monkeypatch.setattr(module.lc, 'Client', FakeClient)
    monkeypatch.setattr(sys, 'argv', ['linear_cycle_time.py', '--since', '2026-09-04T00:00:00Z'])
    assert module.main() == 0
    out = capsys.readouterr().out
    assert 'WEEKLY METRICS' in out and 'GLP-100' in out
    assert 'GLP-101' not in out                       # Backlog children are not in-flight work
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary['done_children'] == 1 and summary['since'] == '2026-09-04T00:00:00Z'
    assert calls and calls[0][0] == 'pages'
