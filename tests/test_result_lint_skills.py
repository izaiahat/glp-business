"""Skill accounting and child counts are observations, not correctness vetoes."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import result_lint


ACCEPTANCE = 'The effect holds.'


@pytest.fixture(autouse=True)
def _acceptance_from_fixture(monkeypatch):
    """These tests exercise skill accounting. The 2026-09-15 acceptance_clauses gate needs the issue's
    acceptance text; supplying it in-process keeps the gate LIVE (verbatim + coverage still checked)
    without a Linear round-trip or the live cache."""
    monkeypatch.setattr(result_lint, '_issue_acceptance_text', lambda issue, **kw: (ACCEPTANCE, 'linear'))


def result(**over):
    base = {'issue': 'GLP-1', 'status': 'COMPLETE', 'children_spawned': 3, 'changed_files': [], 'artifacts_created': [],
            'acceptance_clauses': [{'clause': ACCEPTANCE, 'verdict': 'PASS', 'evidence': 'fixture'}],
            'acceptance_checks': [{'check': 'c', 'verify_cmd': 'true', 'verify_output': 'ok',
                                   'status': 'PASS', 'exit_code': 0}],
            'unverified': 'none', 'skills_loaded': ['linear-work-tracking'],
            'skills_should_have_loaded': ['linear-work-tracking'], 'skills_created': [],
            'effort': 'medium', 'completed_at': '2026-09-11T00:00:00Z'}
    base.update(over)
    return base


def lint(tmp_path, data):
    path = tmp_path / 'RESULT.json'
    path.write_text(json.dumps(data))
    return result_lint.lint_report(str(path), None, strict=False)['problems']


def test_loaded_what_it_should_have_passes(tmp_path):
    assert lint(tmp_path, result()) == []


def test_unloaded_owning_skill_is_a_closeout_veto(tmp_path):
    """Promoted from observational to BLOCKING 2026-09-15 (operator: "we should always be using all
    relevant skills on all runs"). The declaration is the worker's own, so failing on it raises no bar
    the worker did not set. This test asserted the pre-mandate behaviour and was stale."""
    problems = lint(tmp_path, result(skills_loaded=['enzyme'],
                                     skills_should_have_loaded=['linear-work-tracking', 'enzyme']))
    assert problems and any('linear-work-tracking' in p for p in problems), problems


def test_case_and_whitespace_do_not_create_a_false_failure(tmp_path):
    assert lint(tmp_path, result(skills_loaded=[' Linear-Work-Tracking '],
                                 skills_should_have_loaded=['linear-work-tracking'])) == []


def test_comma_string_fields_are_accepted(tmp_path):
    assert lint(tmp_path, result(skills_loaded='a, b', skills_should_have_loaded='b')) == []
    # comma parsing works in both directions: 'b' declared but not loaded is the (now blocking) veto
    problems = lint(tmp_path, result(skills_loaded='a', skills_should_have_loaded='a, b'))
    assert problems and any("'b'" in p or ' b' in p or 'b ' in p for p in problems), problems


def test_none_sentinel_is_not_a_missing_skill(tmp_path):
    """'none' is an empty declaration, not a missing skill - but an empty declaration must carry a
    real routing rationale (2026-09-15: loading a skill you happened to know about is not routing)."""
    rationale = ('checked scripts/linear_audit.py and tests/test_linear_audit.py against '
                 'ops/skills/surface-router.json; no expert track owns either surface')
    assert lint(tmp_path, result(skills_loaded=['none'], skills_should_have_loaded=['none'],
                                 skills_should_have_loaded_rationale=rationale)) == []


@pytest.mark.parametrize('loaded,should,missing', [
    ([], ['x'], ['x']),
    (['x'], ['x', 'y'], ['y']),
    (['x', 'y'], ['x'], []),
    (None, None, []),
])
def test_unloaded_skills_helper(loaded, should, missing):
    assert result_lint.unloaded_skills(
        {'skills_loaded': loaded, 'skills_should_have_loaded': should}) == missing


def test_child_count_is_not_a_correctness_gate(tmp_path):
    big = tmp_path / 'brief.md'; big.write_text('GOAL x\nSTEP BUDGET: 40\nFILES: a\n')
    small = tmp_path / 'b2.md'; small.write_text('GOAL x\nSTEP BUDGET: 8\nFILES: a\n')
    def probs(data, brief):
        path = tmp_path / 'RESULT.json'; path.write_text(json.dumps(data))
        return result_lint.lint_report(str(path), str(brief), strict=False)['problems']
    assert not any('rule 51' in p for p in probs(result(children_spawned=0), big))
    assert not any('rule 51' in p for p in probs(result(children_spawned=0, serial_reason='one indivisible registered run'), big))
    assert not any('rule 51' in p for p in probs(result(children_spawned=0), small))
    assert not any('rule 51' in p for p in probs(result(children_spawned=5), big))
