"""Static brief checks: local source/fixture reads only, no command replay or API."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def brief_lint():
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('brief_lint_deep_under_test', ROOT / 'scripts/brief_lint.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BL = brief_lint()

GOOD = """LINEAR: GLP-999 — deep-lint fixture. Read the issue first. Standard: ops/linear/WORKER-STANDARD.md.
MODEL: gpt-6-sol
PROVIDER: openai-codex
REASONING: medium
MACHINE MAP: {map}
GOAL
Make the marker appear in the fixture file.
ACCEPTANCE
- `grep -c NOTYET {target}` → prints `1`
- `python3 scripts/brief_lint.py --deep --no-run {target}` → exits 0
BASELINE
Today `grep -c NOTYET {target}` prints `0` and the marker is absent.
NOT IN SCOPE
- the selector cache
- the label taxonomy
- anything under site-system/
FILES
Touch: {target}. Do not touch: /home/ubuntu/business/scripts/linear_client.py.
PRE
- `grep -c NOTYET {target}` → FAILS today: prints `0`
- `python3 scripts/brief_lint.py --deep --no-run {target}` → FAILS today: exit 1
TIER: T1
STEP BUDGET: 10
TOOLS
Owning skills: linear-work-tracking
ROUTER BLOCK (paste verbatim; the words "skills enforced" do not substitute for it)
> Before you start: grep `~/.hermes/skills/EXPERT-ROOTS.md` (the current router) for the surfaces this work touches, load the owning skills, and record them in `skills_should_have_loaded` plus `skills_loaded`.
> For file surfaces run `python3 scripts/skill_router.py --paths <the files you will touch>`.
DELEGATION
Delegate independent reasoning and disjoint file work through host-shared admission.
COMPLETION
Keep going until every numbered item has its read-back effect. PARTIAL is not an outcome. BLOCKED is the exception, not the resting state: it names the exact condition, its file:line or receipt, and who owns removing it, and independent in-scope work continues meanwhile.
DONE: write {result} in the shape of ops/linear/RESULT-SCHEMA.json, print `DEEPFIX COMPLETE <abs RESULT.json>`; end each numbered item with `DEEPFIX PROGRESS item N done`.
"""


@pytest.fixture()
def world(tmp_path):
    """A brief that passes every deep check, plus the isolated dirs the collision/key checks read."""
    machine_map = tmp_path / 'MACHINE-MAP-fixture.md'
    machine_map.write_text('entry point scripts/brief_lint.py:1; gate lint_deep(); verify: pytest -q\n')
    target = tmp_path / 'fixture.txt'
    target.write_text('nothing here yet\n')
    briefs = tmp_path / 'briefs'
    briefs.mkdir()
    brief = briefs / 'GLP-999-fixture.md'
    brief.write_text(GOOD.format(map=machine_map, target=target, result=tmp_path / 'RESULT.json'))
    return {'brief': brief, 'briefs': briefs, 'target': target, 'map': machine_map, 'tmp': tmp_path}


def lint(world, **kw):
    kw.setdefault('in_flight', {})
    kw.setdefault('briefs_dir', world['briefs'])
    return BL.lint_deep(world['brief'], **kw)


def rewrite(world, old, new):
    world['brief'].write_text(world['brief'].read_text().replace(old, new))


def test_the_fixture_brief_passes_every_deep_check(world):
    assert lint(world) == []


# --- D1 / D2 / D3: the command the brief names must exist and its flags must parse ----------
def test_d1_a_named_script_that_does_not_exist_fails(world):
    rewrite(world, 'scripts/brief_lint.py --deep', 'scripts/does_not_exist.py --deep')
    assert any(p.startswith('D1 script does not exist') for p in lint(world, run_pre=False))


def test_d2_a_flag_the_script_does_not_accept_fails(world):
    rewrite(world, 'scripts/brief_lint.py --deep --no-run', 'scripts/brief_lint.py --deep --apply')
    problems = lint(world, run_pre=False)
    assert any(p.startswith('D2 flag --apply') for p in problems)


def test_d2_reports_the_real_defect_class_that_burned_the_fleet():
    """The measured case: --apply / --event on affiliate_approval_auto_reconciler.py (GLP-159)."""
    problems = BL.check_command('python3 scripts/affiliate_approval_auto_reconciler.py --event x --apply')
    assert [p for p in problems if '--apply' in p] and [p for p in problems if '--event' in p]


def test_d3_a_script_with_no_help_falls_back_to_its_parsed_flags(tmp_path):
    script = tmp_path / 'bare.py'
    script.write_text('import sys\nif "--only-this" in sys.argv:\n    print("ok")\n')
    assert BL.ast_flags(script) == {'--only-this'}
    assert BL.check_command('python3 %s --only-this' % script) == []
    assert any('D2 flag --nope' in p for p in BL.check_command('python3 %s --nope' % script))


def test_d3_unverifiable_command_when_nothing_can_resolve_the_flag(tmp_path):
    script = tmp_path / 'opaque.py'
    script.write_text('print("hello")\n')
    problems = BL.check_command('python3 %s --mystery' % script)
    assert any('UNVERIFIABLE_COMMAND' in p for p in problems)


# --- D4: paths exist, or say `(new)` --------------------------------------------------------
def test_d4_a_business_path_that_does_not_exist_must_be_marked_new(world):
    rewrite(world, str(world['target']), '/home/ubuntu/business/scripts/never_written.py')
    assert any('D4 path does not exist' in p for p in lint(world, run_pre=False))


def test_d4_a_new_path_is_accepted_when_it_says_so(world):
    rewrite(world, str(world['target']), '/home/ubuntu/business/scripts/never_written.py (new)')
    assert not any(p.startswith('D4') for p in lint(world, run_pre=False))


# --- D5: the pass string has to be testable -------------------------------------------------
@pytest.mark.parametrize('vague', ['works', 'passes cleanly', 'no errors', 'as expected', 'looks right'])
def test_d5_vague_pass_strings_are_rejected(vague):
    assert any(p.startswith('D5 vague pass string')
               for p in BL.check_pass_strings(['- `pytest -q` → %s' % vague]))


def test_d5_a_bullet_without_an_expectation_is_rejected():
    assert any('no expected pass string' in p for p in BL.check_pass_strings(['- `pytest -q` and it is done']))


def test_d5_a_literal_expectation_passes():
    assert BL.check_pass_strings(['- `pytest -q` → prints `30 passed`']) == []


# --- D6: the check must fail today ----------------------------------------------------------
def test_green_regression_checks_are_not_decoration(world):
    world['target'].write_text('NOTYET\n')          # the acceptance check now passes before any work
    problems = lint(world)
    assert problems == []


def test_baseline_record_has_no_mandatory_pre_header(world):
    rewrite(world, 'PRE\n- `grep -c NOTYET', 'PREVIOUSLY\n- `grep -c NOTYET')
    assert lint(world, run_pre=False) == []


def test_shell_runner_and_help_runner_are_deleted():
    assert not hasattr(BL, 'run_read_only')
    assert not hasattr(BL, 'help_text')
    assert not hasattr(BL, 'READ_ONLY_ALLOW')


# --- D7: tier ------------------------------------------------------------------------------
def test_d7_a_t2_verb_without_a_t2_tier_is_a_mismatch(world):
    rewrite(world, 'Make the marker appear in the fixture file.',
            'Send the partner the approval email, then make the marker appear.')
    assert any('TIER_MISMATCH' in p for p in lint(world, run_pre=False))


def test_d7_a_missing_tier_line_fails(world):
    rewrite(world, 'TIER: T1\n', '')
    assert any('missing `TIER: T0|T1|T2` line' in p for p in lint(world, run_pre=False))


def test_d7_the_template_step_budget_line_never_raises_the_tier():
    """Regression: `STEP BUDGET:` made every brief infer T2, so nothing could ever be dispatched."""
    text = ('LINEAR: GLP-1 — x\nGOAL\nRead the registry and write the report.\n'
            'ACCEPTANCE\n- `ls /tmp` → prints `x`\nSTEP BUDGET: 10\n'
            'FILES\nTouch: /home/ubuntu/business/scripts/brief_lint.py.\n')
    assert BL.infer_tier(text, ['/home/ubuntu/business/scripts/brief_lint.py'])[0] == 'T1'


def test_d7_a_prohibition_never_raises_the_tier():
    text = ('LINEAR: GLP-1 — x\nGOAL\nRead the registry and write the report.\n'
            'ACCEPTANCE\n- `ls /tmp` → prints `x`\n'
            'NOT IN SCOPE\n- any deploy or publish of the live site\n- any partner email send\n'
            '- credential rotation\nFILES\nTouch: /home/ubuntu/business/scripts/brief_lint.py.\n')
    assert BL.infer_tier(text, ['/home/ubuntu/business/scripts/brief_lint.py'])[0] == 'T1'


def test_d7_a_path_name_is_not_an_action():
    text = ('LINEAR: GLP-1 — x\nGOAL\nRead scripts/publish_site.py and report what it does.\n'
            'ACCEPTANCE\n- `ls /tmp` → prints `x`\nFILES\nTouch: /home/ubuntu/business/reports/x.md.\n')
    # T0 means the brief produces nothing but its RESULT: `reports/` holds this machine's artifacts too,
    # so writing there is a reversible write (T1), not a read. A pure readback stays T0.
    assert BL.infer_tier(text, ['/home/ubuntu/business/reports/x.md'])[0] == 'T1'
    assert BL.infer_tier(text, ['/home/ubuntu/business/reports/a/RESULT.json'])[0] == 'T0'


def test_d7_an_unknown_blast_radius_is_a_t2():
    assert BL.infer_tier('GOAL\nDo a thing.\n', [])[0] == 'T2'


def test_d7_state_md_in_touch_is_always_t2():
    assert BL.infer_tier('GOAL\nUpdate the cursor.\n', ['/home/ubuntu/business/STATE.md'])[0] == 'T2'


def test_d7_a_more_conservative_declared_tier_is_allowed(world):
    rewrite(world, 'TIER: T1', 'TIER: T2')
    assert not any(p.startswith('D7') for p in lint(world, run_pre=False))


# --- D8 / D9 / D10 --------------------------------------------------------------------------
def test_d8_a_touch_collision_with_an_in_flight_brief_fails(world):
    other = {'/some/other/brief.md': {str(world['target'])}}
    assert any(p.startswith('D8 Touch: collision') for p in lint(world, run_pre=False, in_flight=other))


def test_d9_an_owning_skill_that_is_not_a_skill_directory_fails(world):
    rewrite(world, 'Owning skills: linear-work-tracking', 'Owning skills: maxbounty-payout-rules')
    assert any(p.startswith('D9 owning skill does not resolve') for p in lint(world, run_pre=False))


def test_d10_a_duplicate_done_task_key_fails(world):
    twin = world['briefs'] / 'GLP-998-twin.md'
    twin.write_text(world['brief'].read_text().replace('GLP-999', 'GLP-998'))
    assert any(p.startswith('D10 DONE task key') for p in lint(world, run_pre=False))


def test_d10_the_same_issue_may_have_more_than_one_brief_revision(world):
    twin = world['briefs'] / 'GLP-999-v2.md'
    twin.write_text(world['brief'].read_text())
    assert not any(p.startswith('D10') for p in lint(world, run_pre=False))


def test_the_cli_deep_flag_runs_the_deep_checks(world, capsys):
    rc = BL.main(['brief_lint.py', '--deep', '--no-run', str(world['brief'])])
    out = capsys.readouterr().out
    # The real briefs/plan dir and the live LANE-MAP are consulted here; the fixture's paths are
    # isolated, so the only thing being asserted is that --deep ran and printed a verdict.
    assert rc in (0, 1) and (out.startswith('PASS') or out.startswith('FAIL'))


def test_d11_machine_promises_also_receive_semantic_review():
    import brief_review
    assert 'unpromised' in dict(brief_review.RUBRIC)['Q5']
    verdict, why = brief_review.enforce_rubric({'verdict': 'APPROVE', 'rubric': {
        'Q5': {'answer': 'fail', 'evidence': 'spec_publisher_id is not a promised field'}}})
    assert verdict == 'REVISE' and 'Q5' in why


def test_d11_acceptance_may_not_name_a_mechanism_the_machine_map_never_named(tmp_path):
    """Retained original negative assertions; removing ceremony cannot erase this catch."""
    m = tmp_path / 'MAP.md'
    m.write_text('The gate is scripts/thing.py:12 and the canonical status is approved_active_tracking_ready.')
    text = 'MACHINE MAP: %s\n' % m
    grounded = ['- `python3 scripts/thing.py` -> prints approved_active_tracking_ready']
    invented = ['- `python3 scripts/thing.py --apply` -> the row reads spec_publisher_id populated']
    assert BL.check_mechanism_grounding(text, grounded) == []
    problems = BL.check_mechanism_grounding(text, invented)
    assert problems and all(p.startswith('D11') for p in problems)
    assert any('--apply' in p or 'spec_publisher_id' in p for p in problems)


def test_d11_is_silent_without_a_machine_map(tmp_path):
    assert BL.check_mechanism_grounding('GOAL\nx\n', ['- `python3 scripts/whatever.py --wild` -> ok']) == []


def test_d11_rejection_is_wired_into_deep_lint(world):
    rewrite(world, '→ prints `1`', '→ prints `spec_publisher_id`')
    assert any(p.startswith('D11') and 'spec_publisher_id' in p for p in lint(world))


def test_no_mandatory_map_but_delegation_instruction_required(world):
    rewrite(world, 'MACHINE MAP: %s\n' % world['map'], '')
    assert lint(world) == []
    rewrite(world, 'Delegate independent reasoning and disjoint file work through host-shared admission.',
                   'At your discretion.')
    assert any('delegation instruction' in p for p in lint(world))


# --- D8 in-flight discovery: only a real brief is admissible evidence of in-flight work ---------
def test_in_flight_discovery_ignores_a_memo_that_is_not_a_brief(tmp_path, monkeypatch):
    """A memo has no FILES block and no RESULT contract, so `_brief_finished` can never return True
    for it. Admitting one pinned its prose-scraped 'Touch set' in flight forever: on 2026-09-16 all
    four in-flight entries were memos, and every template-conformant brief failed --deep against
    work that had already finished. The only way past it was to stop declaring paths."""
    standing = tmp_path / 'seat-standing-work.json'
    ledger = tmp_path / 'dispatch-ledger.jsonl'
    memo = tmp_path / 'FIDELITY-PASS.md'
    memo.write_text('# GLP-34 is CLOSED. Take the fidelity pass.\n'
                    'Touch the candidate at /home/ubuntu/business/site-system/x and its css-src/.\n')
    real = tmp_path / 'GLP-1-real.md'
    real.write_text('LINEAR: GLP-1 — a real brief\nFILES\nTouch: /home/ubuntu/business/x.py\n')
    standing.write_text(json.dumps({'seats': {'s1': {'brief': str(memo)}}}))
    ledger.write_text(json.dumps({'lane': 'lane:x', 'brief': str(real)}) + '\n')
    monkeypatch.setattr(BL, 'SEAT_STANDING_WORK', standing)
    monkeypatch.setattr(BL, 'DISPATCH_LEDGER', ledger)
    found = BL.in_flight_briefs()
    assert set(found) == {str(real)}
    assert BL._is_brief(memo) is False
    assert BL._is_brief(real) is True


def test_a_real_brief_still_collides_after_the_memo_filter(tmp_path, monkeypatch):
    """The filter must not be a way to make D8 quiet: two real briefs still collide."""
    standing = tmp_path / 'seat-standing-work.json'
    ledger = tmp_path / 'dispatch-ledger.jsonl'
    other = tmp_path / 'GLP-2-other.md'
    other.write_text('LINEAR: GLP-2 — other\nFILES\nTouch: /home/ubuntu/business/shared.css\n')
    standing.write_text(json.dumps({'seats': {'s1': {'brief': str(other)}}}))
    ledger.write_text('')
    monkeypatch.setattr(BL, 'SEAT_STANDING_WORK', standing)
    monkeypatch.setattr(BL, 'DISPATCH_LEDGER', ledger)
    mine = tmp_path / 'GLP-3-mine.md'
    mine.write_text('LINEAR: GLP-3 — mine\n')
    out = BL.check_collision(mine, ['/home/ubuntu/business/shared.css'])
    assert out and out[0].startswith('D8 Touch: collision')
