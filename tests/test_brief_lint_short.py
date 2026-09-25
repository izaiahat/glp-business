"""Short-form Definition-of-Ready rules R1-R8 (audit 2026-09-11 §2). Offline; no Linear, no network."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'ops/linear/DISPATCH-TEMPLATE.md'


def brief_lint():
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('brief_lint_under_test', ROOT / 'scripts/brief_lint.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GOOD = """LINEAR: GLP-99 — harden the queue tooling. Read the issue first. Standard: ops/linear/WORKER-STANDARD.md.
MODEL: gpt-6-sol
PROVIDER: openai-codex
REASONING: medium
MACHINE MAP: {machine_map}
GOAL
Make the hourly audit report only real findings.
ACCEPTANCE
- `python3 scripts/linear_audit.py --fixture f.json` → prints exit 0 with zero findings
- `python3 -m pytest tests/test_linear_audit.py -q` → expect `30 passed`
BASELINE
Today `python3 scripts/linear_audit.py --fixture f.json` prints 3 findings and exits 0.
NOT IN SCOPE
- the label taxonomy another lane is creating
- the selector cache
- anything under site-system/
FILES
Touch: /home/ubuntu/business/scripts/linear_audit.py, /home/ubuntu/business/tests/test_linear_audit.py. Do not touch: /home/ubuntu/business/scripts/linear_client.py.
STEP BUDGET: 10
TOOLS
Owning skills: linear-work-tracking
ROUTER BLOCK (paste verbatim; the words "skills enforced" do not substitute for it)
> Before you start: grep `~/.hermes/skills/EXPERT-ROOTS.md` (the current router) for the surfaces this work touches, load the owning skills, and record them in `skills_should_have_loaded` plus `skills_loaded`.
> For file surfaces run `python3 scripts/skill_router.py --paths <the files you will touch>`.
DELEGATION
Delegate independent reasoning and disjoint file work through host-shared admission; verify effective routes.
COMPLETION
Keep going until every numbered item has its read-back effect. PARTIAL is not an outcome. BLOCKED is the exception, not the resting state: it names the exact condition, its file:line or receipt, and who owns removing it, and independent in-scope work continues meanwhile.
DONE: write /home/ubuntu/business/reports/x/RESULT.json in the shape of ops/linear/RESULT-SCHEMA.json, print `K COMPLETE <path>`.
"""


@pytest.fixture()
def good(tmp_path):
    machine_map = tmp_path / 'map.md'
    machine_map.write_text('entry points, gates, verify commands\n')
    return GOOD.format(machine_map=machine_map)


def problems(text, path=None):
    return brief_lint().lint_short(text, path)


def test_the_shipped_template_passes_its_own_lint():
    assert brief_lint().lint(TEMPLATE) == []


def test_a_well_formed_short_brief_passes(good):
    assert problems(good) == []


def test_r1_files_touch_line_is_required(good):
    text = good.replace('FILES\nTouch: /home/ubuntu/business/scripts/linear_audit.py, /home/ubuntu/business/tests/test_linear_audit.py. Do not touch: /home/ubuntu/business/scripts/linear_client.py.\n', '')
    assert any(p.startswith('R1:') for p in problems(text))


def test_r1_touch_line_with_no_path_is_rejected(good):
    text = good.replace('Touch: /home/ubuntu/business/scripts/linear_audit.py, /home/ubuntu/business/tests/test_linear_audit.py.', 'Touch: whatever you need.')
    assert any(p.startswith('R1:') for p in problems(text))


def test_real_exclusions_have_no_three_bullet_quota(good):
    text = good.replace('- the selector cache\n', '')
    assert problems(text) == []
    assert problems(good.replace('NOT IN SCOPE\n', 'NOTES\n')) != []


def test_step_budget_is_optional_not_a_veto(good):
    assert problems(good.replace('STEP BUDGET: 10\n', '')) == []
    assert problems(good.replace('STEP BUDGET: 10', 'STEP BUDGET: 40')) == []
    assert problems(good.replace('STEP BUDGET: 10', 'STEP BUDGET: 20')) == []


def test_r4_acceptance_bullet_needs_a_command_and_a_pass_string(good):
    no_cmd = good.replace('- `python3 scripts/linear_audit.py --fixture f.json` → prints exit 0 with zero findings',
                          '- the audit is clean afterwards')
    assert any(p.startswith('D5') and 'no `command`' in p for p in problems(no_cmd))
    no_pass = good.replace('- `python3 scripts/linear_audit.py --fixture f.json` → prints exit 0 with zero findings',
                           '- `python3 scripts/linear_audit.py --fixture f.json`')
    assert any(p.startswith('D5') and 'pass string' in p for p in problems(no_pass))


@pytest.mark.parametrize('marker', ['→ `PASS`', '-> `PASS`', 'expect `PASS`', 'prints `PASS`', 'exit 0'])
def test_r4_accepts_every_documented_pass_marker(good, marker):
    text = good.replace('→ prints exit 0 with zero findings', marker)
    assert problems(text) == []


def test_reasoning_header_is_a_brief_gate(good):
    assert any('REASONING:' in p for p in problems(good.replace('REASONING: medium\n', '')))
    assert any('invalid or ambiguous' in p for p in problems(good.replace('REASONING: medium', 'REASONING: high.')))
    assert problems(good.replace('REASONING: medium', 'REASONING: xhigh')) == []


def test_delegation_needs_an_instruction_not_just_a_header(good):
    text = good.replace('Delegate independent reasoning and disjoint file work through host-shared admission; verify effective routes.',
                        'Children may work at your choice.')
    assert any('delegation instruction' in p for p in problems(text))
    assert any('delegation instruction' in p for p in problems(good.replace(
        'Delegate independent reasoning and disjoint file work through host-shared admission; verify effective routes.',
        'Do not delegate independent reasoning or file work.')))
    assert problems(good.replace('DELEGATION\n', '')) == []


def test_delegate_no_independent_or_disjoint_work_is_not_affirmative(good):
    text = good.replace('Delegate independent reasoning and disjoint file work through host-shared admission; verify effective routes.',
                        'Delegate no independent or disjoint work.')
    assert any('delegation instruction' in p for p in problems(text))


def test_prohibited_router_and_skill_loading_are_not_instructions(good):
    text = good.replace('For file surfaces run `python3 scripts/skill_router.py --paths <the files you will touch>`.',
                        'Never run `python3 scripts/skill_router.py --paths <the files you will touch>`.')
    text = text.replace('load the owning skills', 'do not load the owning skills')
    assert any('missing skill_router.py --paths instruction' in p for p in problems(text))


def test_more_than_five_real_acceptance_checks_are_allowed(good):
    extra = '\n'.join(f'- `cmd{i}` → exit 0' for i in range(4))
    text = good.replace('- `python3 -m pytest tests/test_linear_audit.py -q` → expect `30 passed`',
                        '- `python3 -m pytest tests/test_linear_audit.py -q` → expect `30 passed`\n' + extra)
    assert problems(text) == []


def test_forty_line_cap_is_removed(good):
    text = good + '\n'.join(f'note {i}' for i in range(40))
    assert problems(text) == []


def test_machine_map_placeholder_allowed_but_a_real_path_must_exist(good):
    assert problems(good.replace(next(line for line in good.splitlines() if line.startswith('MACHINE MAP:')),
                                 'MACHINE MAP: <abs path to the map>')) == []
    bad = good.replace(next(line for line in good.splitlines() if line.startswith('MACHINE MAP:')),
                       'MACHINE MAP: /home/ubuntu/business/does-not-exist-map.md')
    assert any('MACHINE MAP path does not exist' in p for p in problems(bad))


# --- the operator's COMPLETION invariant (2026-09-10, memory: no-partial-results-complete-in-full) ---
# These are the rules that vanished in the 2026-09-14 simplification. With nothing enforcing them,
# 2 of 39 live briefs carried the invariant and 37 of 39 RESULTs came back BLOCKED with zero
# COMPLETE. They are asserted here so the deletion cannot repeat silently.

COMPLETION_BODY = (
    'Keep going until every numbered item has its read-back effect. PARTIAL is not an outcome. '
    'BLOCKED is the exception, not the resting state: it names the exact condition, its file:line '
    'or receipt, and who owns removing it, and independent in-scope work continues meanwhile.'
)


def test_a_brief_without_the_completion_line_fails_lint_short(good):
    """The rule the operator asked for: no COMPLETION block, no dispatch."""
    text = good.replace('COMPLETION\n' + COMPLETION_BODY + '\n', '')
    assert 'COMPLETION' not in text
    assert any('COMPLETION line' in p for p in problems(text))


def test_a_completion_block_that_omits_either_half_fails(good):
    """Both halves are the instruction: keep going, and PARTIAL is not an outcome."""
    no_keep_going = good.replace('Keep going until every numbered item has its read-back effect. ', '')
    assert any('COMPLETION line' in p for p in problems(no_keep_going))
    no_partial = good.replace('PARTIAL is not an outcome. ', '')
    assert any('COMPLETION line' in p for p in problems(no_partial))


def test_incidental_prose_elsewhere_does_not_satisfy_the_completion_rule(good):
    """A whole-file substring search let a brief with NO COMPLETION block pass on a GOAL remark."""
    text = good.replace('COMPLETION\n' + COMPLETION_BODY + '\n', '')
    text = text.replace('Make the hourly audit report only real findings.',
                        'Make the hourly audit report only real findings. (Historically we would '
                        'keep going until the cows come home; PARTIAL is not an outcome there either.)')
    assert any('COMPLETION line' in p for p in problems(text))


def test_a_brief_containing_a_time_box_fails_lint_short(good):
    """No time boxes in briefs, ever (operator 2026-09-10): completion, not clocks."""
    assert any('time box' in p and 'forbidden' in p
               for p in problems(good + '\nHard time box: 5 hours.\n'))


@pytest.mark.parametrize('clause', [
    'This work is time-boxed.',
    'TIME BOX: 5 hours',
    'Work inside a timebox.',
    'Work a 3-hour budget then stop.',
    'Hard stop at 17:00.',
    'Keep the lane open until 03:00Z.',
])
def test_every_spelling_of_a_clock_is_a_time_box(good, clause):
    assert any('time box' in p for p in problems(good + '\n' + clause + '\n')), clause


@pytest.mark.parametrize('fact', [
    'the unchanged 45-day limit crossed at 17:13Z today',
    'the CDN holds a 30-minute cache window',
])
def test_a_duration_the_work_is_about_is_not_a_time_box(good, fact):
    """A lint that called a LifeMD offer fact a time box would teach the orchestrator to delete
    true evidence to get green. The ban is on a clock the WORK runs against."""
    assert problems(good + '\n' + fact + '\n') == []


def test_a_completion_block_may_not_license_a_partial_result(good):
    """The exact bypass found in the pass-2 audit: a COMPLETION block ordering the banned behaviour."""
    text = good.replace(COMPLETION_BODY,
                        'Keep going until 03:00Z, then stop where you are and report a PARTIAL '
                        'result. Do not finish the remaining items.')
    out = problems(text)
    assert any('COMPLETION line' in p for p in out)
    assert any('licenses a partial result' in p for p in out)
    assert any('time box' in p for p in out)


def test_blocked_is_the_exception_not_a_licence_to_stop(good):
    """Naming a real blocker stays legal; telling the worker to stop half-done does not."""
    legal = good.replace('BLOCKED is the exception, not the resting state:',
                         'BLOCKED names the exact condition, its file:line or receipt, and its owner:')
    assert problems(legal) == []
    assert any('licenses a partial result' in p
               for p in problems(good + '\nIf you run long, deliver whatever you have when the '
                                        'session ends.\n'))


def test_the_invariant_may_ride_the_done_contract_line(good):
    """The carrier must be a DIRECTIVE section, not the COMPLETION header specifically. Nine live
    briefs state the invariant as the closing paragraph under DONE:, and the worker reads it there."""
    text = good.replace('COMPLETION\n' + COMPLETION_BODY + '\n', '')
    text = text.replace('print `K COMPLETE <path>`.', 'print `K COMPLETE <path>`.\n' + COMPLETION_BODY)
    assert problems(text) == []


def test_a_descriptive_section_is_never_the_carrier(good):
    """GOAL and BASELINE describe the world; they do not order the work."""
    stripped = good.replace('COMPLETION\n' + COMPLETION_BODY + '\n', '')
    for section, anchor in (('GOAL', 'Make the hourly audit report only real findings.'),
                            ('BASELINE', 'Today `python3 scripts/linear_audit.py --fixture f.json` '
                                         'prints 3 findings and exits 0.')):
        text = stripped.replace(anchor, anchor + ' ' + COMPLETION_BODY)
        assert any('COMPLETION line' in p for p in problems(text)), section
