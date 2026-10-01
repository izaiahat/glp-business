"""Synthetic Sunday/Monday counterexample from the retained RED reproduction.

Portable function-level coverage: execute explicit AST definitions from this
checkout; omit eager host-only imports and all CLI/emitter/selection branches.
No host dependency source, live data, authority, database or jobs are loaded.
"""
import ast
import datetime as dt
import hashlib
import json
import types
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _functions(filename, names):
    source = SCRIPTS / filename
    tree = ast.parse(source.read_text(), filename=str(source))
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == set(names)
    module = types.ModuleType("workweek_fixture_" + source.stem)
    module.__dict__.update(dt=dt, hashlib=hashlib, json=json, Path=Path, Any=Any)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), module.__dict__)
    return module


actions = _functions("learning_loop_actions.py", ("canonical", "payload_hash", "week_start"))
r2 = _functions("r2_update_carrier.py", (
    "canonical", "payload_hash", "sha", "spec", "read_object", "load_learning_work_orders",
))


@pytest.mark.parametrize('selection_day,consumer_day', [
    ('2026-10-04', '2026-10-05'),
    ('2026-10-05', '2026-10-05'),
    ('2026-10-06', '2026-10-06'),
    ('2026-10-07', '2026-10-07'),
    ('2026-10-08', '2026-10-08'),
    ('2026-10-09', '2026-10-09'),
    ('2026-10-10', '2026-10-10'),
])
def test_learning_workweek_consumption(tmp_path, selection_day, consumer_day):
    selection_date = dt.date.fromisoformat(selection_day)
    consumer_date = dt.date.fromisoformat(consumer_day)
    # This fixture exercises the real loader, without collecting SEO evidence.
    selection = {
        'schema': 'glp-seo-existing-article-update-selection-v1',
        'as_of': selection_day,
        'observability': {'fixture_observation_date': '2026-10-01'},
    }
    original_selection = dict(selection)
    selection['receipt_sha256'] = actions.payload_hash(selection, 'receipt_sha256')
    work_path = tmp_path / 'r2-work-orders.json'
    work_path.write_text(json.dumps(selection) + '\n')
    pointer = {
        'schema': 'glp-seo-weekly-learning-actions-pointer-v1',
        'status': 'PASS_CURRENT_WEEKLY_ACTIONS',
        'as_of': selection_day,
        'week_start': actions.week_start(selection_date),
        'r2_work_orders': {
            'path': str(work_path),
            'sha256': hashlib.sha256(work_path.read_bytes()).hexdigest(),
        },
    }
    selection_bytes = work_path.read_bytes()
    pointer_path = tmp_path / 'latest.json'

    def seal(value):
        value = dict(value)
        value['pointer_sha256'] = actions.payload_hash(value, 'pointer_sha256')
        pointer_path.write_text(json.dumps(value) + '\n')

    # The unchanged freshness gate must reject both older and future weeks.
    for stale_week in ('2026-09-28', '2026-10-12'):
        seal({**pointer, 'week_start': stale_week})
        with pytest.raises(RuntimeError, match='^r2_learning_actions_week_stale$'):
            r2.load_learning_work_orders(pointer_path, as_of=consumer_date)

    seal(pointer)
    try:
        loaded, _ = r2.load_learning_work_orders(pointer_path, as_of=consumer_date)
    except RuntimeError as error:
        outcome = str(error)
    else:
        outcome = 'ACCEPTED'
        assert loaded == selection
        assert {k: v for k, v in loaded.items() if k != 'receipt_sha256'} == original_selection
        assert work_path.read_bytes() == selection_bytes
        assert loaded['as_of'] == selection_day
        assert loaded['observability']['fixture_observation_date'] == '2026-10-01'
    assert selection['as_of'] == selection_day
    assert pointer['as_of'] == selection_day
    print(json.dumps({
        'selection_day': selection_day,
        'consumer_day': consumer_day,
        'emitted_week_start': pointer['week_start'],
        'expected_week_start': '2026-10-05',
        'consumer_outcome': outcome,
        'older_and_future_week_rejected': True,
    }, sort_keys=True))
    assert (pointer['week_start'], outcome) == ('2026-10-05', 'ACCEPTED')
