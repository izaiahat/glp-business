"""Invariants the 2026-09-16 linter audit restored or repaired. Offline: no Linear, no network.

Each test names the defect it locks down. Nothing here weakens an existing rule; every case either
restores an operator standing order that was deleted, or removes a false positive that parked lanes.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'ops/linear/DISPATCH-TEMPLATE.md'


def _load(name, rel):
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BL = _load('bl_invariants', 'scripts/brief_lint.py')
RL = _load('rl_invariants', 'scripts/result_lint.py')
SR = _load('sr_invariants', 'scripts/skill_router.py')

BRIEF = """LINEAR: GLP-99 — restore the invariants. Read the issue first. Standard: ops/linear/WORKER-STANDARD.md.
GOAL
Make the audit report only real findings.
ACCEPTANCE
- `python3 scripts/linear_audit.py --fixture f.json` → prints exit 0 with zero findings
BASELINE
Today the same command prints 3 findings and exits 0.
NOT IN SCOPE
- the label taxonomy another lane is creating
FILES
Touch: /home/ubuntu/business/scripts/linear_audit.py. Do not touch: /home/ubuntu/business/STATE.md.
TIER: T1
TOOLS
Owning skills: linear-work-tracking
ROUTER BLOCK (paste verbatim; the words "skills enforced" do not substitute for it)
> Before you start: grep `~/.hermes/skills/EXPERT-ROOTS.md` (the current router) for the surfaces this work touches.
COMPLETION
Keep going until every numbered item has its read-back effect. PARTIAL is not an outcome. BLOCKED is the exception, not the resting state.
DONE: write /home/ubuntu/business/reports/x/RESULT.json in the shape of ops/linear/RESULT-SCHEMA.json, then print `GLP-99 COMPLETE <path>`.
"""


def probs(text):
    return BL.lint_short(text)


# --- the shipped contract must pass its own validator, plainly and deeply ---------------
def test_shipped_template_passes_normal_and_deep_lint():
    assert BL.lint(TEMPLATE) == []
    assert BL.lint_deep(TEMPLATE) == []


def test_the_reference_brief_passes():
    assert probs(BRIEF) == []


# --- operator standing orders that had been deleted from the lint -----------------------
def test_a_brief_without_the_completion_line_fails():
    text = BRIEF.replace('Keep going until every numbered item has its read-back effect. PARTIAL is not an outcome. ', '')
    assert any('COMPLETION line' in p for p in probs(text))


def test_a_time_box_is_forbidden():
    for clause in ('TIME BOX: 5 hours\n', 'Hard time box of 5 hours.\n', 'Work inside a timebox.\n'):
        assert any('time box' in p for p in probs(BRIEF + clause)), clause


def test_a_time_box_can_never_satisfy_a_completion_criterion():
    """The retired long-form rule offered `time box` as an alternative to a completion criterion."""
    text = BRIEF.replace('Keep going until every numbered item has its read-back effect. PARTIAL is not an outcome. ',
                         '') + 'STOP\nStop when the hard time box expires.\n'
    out = probs(text)
    assert any('COMPLETION line' in p for p in out)
    assert any('time box' in p and 'forbidden' in p for p in out)
    assert not any('or time box' in p for p in out)


def test_the_router_block_is_required_not_the_words_skills_enforced():
    text = BRIEF.replace('ROUTER BLOCK (paste verbatim; the words "skills enforced" do not substitute for it)\n', '')
    text = text.replace('> Before you start: grep `~/.hermes/skills/EXPERT-ROOTS.md` (the current router) for the surfaces this work touches.\n',
                        'Skills enforced.\n')
    assert any('ROUTER BLOCK' in p for p in probs(text))


def test_routing_to_the_superseded_capability_map_alone_is_rejected():
    text = BRIEF.replace('grep `~/.hermes/skills/EXPERT-ROOTS.md` (the current router)',
                         'consult `~/.hermes/skills/CAPABILITY-MAP.md`')
    assert any('CAPABILITY-MAP.md alone' in p for p in probs(text))


def test_a_brief_with_touch_paths_needs_a_baseline():
    text = BRIEF.replace('BASELINE\nToday the same command prints 3 findings and exits 0.\n', '')
    assert any('BASELINE' in p for p in probs(text))


def test_an_orchestrator_build_gate_is_rejected():
    for clause in ('Do not run a full build. Tell me when your source work is done and I sequence the next one.\n',
                   'Do not run a build yourself.\n'):
        assert any('build gate' in p for p in probs(BRIEF + clause)), clause


def test_a_credential_value_in_a_short_brief_still_fails():
    assert any('credential' in p for p in probs(BRIEF + 'password: hunter2xyz\n'))


# --- the retired long-form grammar gives one clear error, not eight obsolete demands -----
def test_a_file_without_the_issue_binding_gets_one_clear_error(tmp_path):
    memo = tmp_path / 'memo.md'
    memo.write_text('## Scope change\nSome prose the orchestrator wrote.\n')
    out = BL.lint(memo)
    assert len(out) == 1 and 'line 1 must be' in out[0]
    assert not any('CONTEXT' in p or 'AUTONOMY' in p or 'VERIFY' in p for p in out)


# --- Touch parsing ---------------------------------------------------------------------
def test_a_lowercase_do_not_touch_still_excludes_the_path():
    text = BRIEF.replace('Touch: /home/ubuntu/business/scripts/linear_audit.py. Do not touch: /home/ubuntu/business/STATE.md.',
                         'Touch: /home/ubuntu/business/scripts/linear_audit.py, do not touch /home/ubuntu/business/STATE.md.')
    assert BL._touch_tokens(text) == ['/home/ubuntu/business/scripts/linear_audit.py']


def test_trailing_prose_naming_a_read_only_file_is_not_a_touch_declaration():
    text = BRIEF.replace('Do not touch: /home/ubuntu/business/STATE.md.',
                         'No other writes; upstream inputs, cards, STATE.md and siblings stay read-only.')
    assert BL._touch_tokens(text) == ['/home/ubuntu/business/scripts/linear_audit.py']


def test_a_markdown_heading_ends_the_files_block():
    text = ('LINEAR: GLP-99 — x\nFILES\nTouch: reports/ops/x/, tests/browser/\n\n'
            '## Why this is landing on you\nthe desktop/mobile gap at 58/19/35/12\n')
    assert BL._touch_tokens(text) == ['reports/ops/x/', 'tests/browser/']


def test_relative_touch_paths_are_normalised_to_absolute():
    assert BL.abs_touch(['reports/ops/x/', '/home/ubuntu/business/a.py', '<absolute paths>']) == {
        '/home/ubuntu/business/reports/ops/x', '/home/ubuntu/business/a.py'}


def test_a_relative_write_is_no_longer_inferred_as_produces_nothing():
    text = 'GOAL\nEdit the handler.\nACCEPTANCE\n- `x` → ok\n'
    assert BL.infer_tier(text, ['src/handler.mjs'])[0] == 'T1'


# --- D8 collisions ---------------------------------------------------------------------
def test_d8_in_flight_discovery_reads_the_sources_that_are_actually_written(tmp_path, monkeypatch):
    """The retired 2026-09-07 plan dir, LANE-MAP.json and autodispatch-ledger.json are gone."""
    assert not hasattr(BL, 'LANE_MAP_PATH')
    assert BL.SEAT_STANDING_WORK.name == 'seat-standing-work.json'
    assert BL.DISPATCH_LEDGER.name == 'dispatch-ledger.jsonl'
    standing = tmp_path / 'seat-standing-work.json'
    ledger = tmp_path / 'dispatch-ledger.jsonl'
    a, b = tmp_path / 'a.md', tmp_path / 'b.md'
    a.write_text('LINEAR: GLP-1 — a\nFILES\nTouch: /home/ubuntu/business/x.py\n')
    b.write_text('LINEAR: GLP-2 — b\nFILES\nTouch: /home/ubuntu/business/y.py\n')
    standing.write_text(json.dumps({'seats': {'s1': {'brief': str(a)}}}))
    ledger.write_text(json.dumps({'lane': 'lane:x', 'brief': str(b)}) + '\n')
    monkeypatch.setattr(BL, 'SEAT_STANDING_WORK', standing)
    monkeypatch.setattr(BL, 'DISPATCH_LEDGER', ledger)
    found = BL.in_flight_briefs()
    assert set(found) == {str(a), str(b)}
    assert found[str(a)] == {'/home/ubuntu/business/x.py'}


def test_d8_collides_on_directory_containment(tmp_path):
    other = tmp_path / 'other.md'
    other.write_text('x')
    in_flight = {str(other): {'/home/ubuntu/business/site-system/flagship-v2/candidates/c1'}}
    out = BL.check_collision(tmp_path / 'mine.md',
                             ['/home/ubuntu/business/site-system/flagship-v2/candidates/c1/css-src/10-home.css'],
                             in_flight=in_flight)
    assert out and out[0].startswith('D8 Touch: collision')


def test_a_blocked_brief_no_longer_pins_its_paths_in_flight_forever(tmp_path):
    result = tmp_path / 'RESULT.json'
    result.write_text(json.dumps({'status': 'BLOCKED'}))
    brief = tmp_path / 'b.md'
    brief.write_text('LINEAR: GLP-1 — x\nDONE: write %s in the shape of ops/linear/RESULT-SCHEMA.json.\n' % result)
    assert BL._brief_finished(brief) is True


def test_a_result_style_brief_contract_is_recognised(tmp_path):
    result = tmp_path / 'RESULT.json'
    result.write_text(json.dumps({'status': 'COMPLETE'}))
    brief = tmp_path / 'b.md'
    brief.write_text('LINEAR: GLP-1 — x\nRESULT: %s\n' % result)
    assert BL._brief_finished(brief) is True


# --- D7 tier: the noun is not the act ---------------------------------------------------
@pytest.mark.parametrize('goal, tier', [
    ('Make the deploy sensor fail on an unwired provider.', 'T1'),
    ('Fix the publish carrier retry log.', 'T1'),
    ('Deploy the candidate to the live site.', 'T2'),
    ('Publish the release to Cloudflare Pages.', 'T2'),
    ('Run the deploy and read back the live head.', 'T2'),
    ('Send the partner reply.', 'T2'),
])
def test_tier_distinguishes_the_noun_from_the_act(goal, tier):
    text = 'GOAL\n%s\nACCEPTANCE\n- `x` → ok\n' % goal
    assert BL.infer_tier(text, ['/home/ubuntu/business/scripts/a.py'])[0] == tier


def test_state_md_and_a_missing_touch_still_force_t2():
    assert BL.infer_tier('GOAL\nEdit a file.\n', ['/home/ubuntu/business/STATE.md'])[0] == 'T2'
    assert BL.infer_tier('GOAL\nEdit a file.\n', [])[0] == 'T2'


# --- result_lint -----------------------------------------------------------------------
QUIZ = '/home/ubuntu/business/site-system/flagship-v2/src/quiz-page.mjs'
BASE = {'issue': 'GLP-9999', 'status': 'BLOCKED', 'unverified': 'none', 'changed_files': [],
        'acceptance_checks': [{'check': 'x', 'verify_cmd': 'true', 'verify_output': 'ok',
                               'exit_code': 0, 'status': 'PASS'}],
        'acceptance_clauses': [{'clause': 'c', 'verdict': 'BLOCKED', 'evidence': 'e'}],
        'skills_should_have_loaded': ['site-design'], 'skills_loaded': ['site-design']}


@pytest.fixture()
def rlint(tmp_path, monkeypatch):
    monkeypatch.setattr(RL, '_issue_acceptance_text', lambda issue, **kw: ('c', 'linear'))

    def run(extra, path=None):
        d = dict(BASE)
        d.update(extra)
        f = tmp_path / 'RESULT.json'
        f.write_text(json.dumps(d))
        return RL.lint_report(str(f), str(path) if path else None)['problems']
    return run


def test_a_mapped_surface_in_artifacts_created_alone_is_rejected(rlint):
    assert any('mapped surface' in p for p in rlint({'artifacts_created': [QUIZ]}))
    assert any('mapped surface' in p for p in rlint({'changed_files': [QUIZ]}))
    assert rlint({'changed_files': ['/home/ubuntu/business/unmapped.txt']}) == []


def test_a_one_character_rationale_no_longer_satisfies_the_skills_gate(rlint):
    thin = rlint({'skills_should_have_loaded': [], 'skills_loaded': [],
                  'skills_should_have_loaded_rationale': 'x'})
    assert any('too thin' in p for p in thin)
    real = rlint({'skills_should_have_loaded': [], 'skills_loaded': [],
                  'skills_should_have_loaded_rationale':
                      'Ran skill_router on scripts/foo.py and ops/bar.json; neither path is mapped '
                      'and no EXPERT-ROOTS track owns fleet queue plumbing.'})
    assert real == []


def test_an_observable_only_complete_check_needs_a_concrete_readback(rlint):
    complete = {'status': 'COMPLETE',
                'acceptance_clauses': [{'clause': 'c', 'verdict': 'PASS', 'evidence': 'e'}]}
    prose = rlint({**complete, 'acceptance_checks': [
        {'check': 'looks right', 'observable': 'the page renders', 'output': 'yes', 'status': 'PASS'}]})
    assert any('concrete readback' in p for p in prose)
    concrete = rlint({**complete, 'acceptance_checks': [
        {'check': 'renders', 'observable': 'https://x.test/quiz shows `Step 1 of 5`',
         'output': '`Step 1 of 5` present', 'status': 'PASS'}]})
    assert concrete == []


def test_a_declared_test_count_must_not_contradict_the_run_output(rlint):
    complete = {'status': 'COMPLETE',
                'acceptance_clauses': [{'clause': 'c', 'verdict': 'PASS', 'evidence': 'e'}]}
    bad = rlint({**complete, 'acceptance_checks': [
        {'check': 't', 'verify_cmd': 'pytest -q tests/', 'verify_output': 'collected 0 items',
         'exit_code': 0, 'status': 'PASS', 'selected_test_count': 99}]})
    assert any('contradicts the run output' in p for p in bad)
    good = rlint({**complete, 'acceptance_checks': [
        {'check': 't', 'verify_cmd': 'pytest -q tests/',
         'verify_output': 'collected 53 items\n53 passed', 'exit_code': 0, 'status': 'PASS',
         'selected_test_count': 53}]})
    assert good == []


def test_a_lowercase_do_not_touch_keeps_the_excluded_path_out_of_the_allow_set():
    brief = ('FILES\nTouch: /home/ubuntu/business/a.py, do not touch /home/ubuntu/business/STATE.md.\n'
             'TIER: T1\n')
    assert RL.touch_list(brief) == ['/home/ubuntu/business/a.py']
    assert RL.denied_list(brief) == ['/home/ubuntu/business/STATE.md']


def test_the_deny_set_is_scoped_to_the_files_block():
    assert RL.denied_list(TEMPLATE.read_text()) == []


def test_an_unrevalidated_acceptance_cache_is_flagged_not_trusted(tmp_path, monkeypatch):
    cache = tmp_path / 'acceptance-cache.json'
    cache.write_text(json.dumps({'GLP-9999': {'acceptance': 'The thing is done.'}}))
    monkeypatch.setattr(RL, 'ACCEPTANCE_CACHE', cache)
    monkeypatch.setattr(RL, '_fetch_issue', lambda issue: (None, None))
    text, source = RL._issue_acceptance_text('GLP-9999')
    assert text == 'The thing is done.' and source == 'cache (unrevalidated)'
    d = dict(BASE)
    d['acceptance_clauses'] = [{'clause': 'The thing is done.', 'verdict': 'BLOCKED', 'evidence': 'e'}]
    f = tmp_path / 'RESULT.json'
    f.write_text(json.dumps(d))
    assert any('could NOT be revalidated' in p for p in RL.lint_report(str(f))['problems'])


def test_a_revalidated_cache_hit_is_silent(tmp_path, monkeypatch):
    cache = tmp_path / 'acceptance-cache.json'
    cache.write_text(json.dumps({'GLP-9999': {'acceptance': 'The thing is done.', 'updated_at': 'T1'}}))
    monkeypatch.setattr(RL, 'ACCEPTANCE_CACHE', cache)
    monkeypatch.setattr(RL, '_fetch_issue', lambda issue: ('The thing is done.', 'T1'))
    assert RL._issue_acceptance_text('GLP-9999') == ('The thing is done.', 'cache')


def test_an_edited_acceptance_replaces_the_cached_text(tmp_path, monkeypatch):
    cache = tmp_path / 'acceptance-cache.json'
    cache.write_text(json.dumps({'GLP-9999': {'acceptance': 'Old text.', 'updated_at': 'T1'}}))
    monkeypatch.setattr(RL, 'ACCEPTANCE_CACHE', cache)
    monkeypatch.setattr(RL, '_fetch_issue', lambda issue: ('New text.', 'T2'))
    assert RL._issue_acceptance_text('GLP-9999') == ('New text.', 'linear')
    assert json.loads(cache.read_text())['GLP-9999']['updated_at'] == 'T2'


# --- skill_router ----------------------------------------------------------------------
def test_the_queue_machinery_now_routes_to_its_owning_skill():
    need = SR.required_for(['scripts/linear_orchestrator.py', 'ops/linear/WORKER-STANDARD.md'])
    assert 'linear-work-tracking' in need


def test_every_router_skill_resolves_to_a_live_skill_directory():
    rules = json.loads((ROOT / 'ops/skills/surface-router.json').read_text())['rules']
    names = {s for r in rules for s in r['skills']}
    live = BL.skill_dirs()
    assert live, 'skills catalog unreadable'
    assert sorted(n for n in names if n not in live) == []


def test_a_shared_reports_directory_is_not_a_shared_file_collision(tmp_path):
    other = tmp_path / 'other.md'
    other.write_text('x')
    in_flight = {str(other): {'/home/ubuntu/business/reports/ops/glp38-20260915'}}
    assert BL.check_collision(tmp_path / 'mine.md',
                              ['/home/ubuntu/business/reports/ops/glp38-20260915/RESULT-a.json'],
                              in_flight=in_flight) == []
    # a named file two briefs both declare still collides, even under reports/
    in_flight = {str(other): {'/home/ubuntu/business/reports/ops/glp38-20260915/register.json'}}
    assert BL.check_collision(tmp_path / 'mine.md',
                              ['/home/ubuntu/business/reports/ops/glp38-20260915/register.json'],
                              in_flight=in_flight)
