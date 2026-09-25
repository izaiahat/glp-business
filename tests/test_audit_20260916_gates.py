"""Regressions for the 2026-09-16 ultracode-audit repairs: each test names the defect class it closes."""
import json, os, sys, time, subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import result_lint, protected_oracle_gate as POG, brief_clause_gate as BCG, candidate_build_lock as CBL


# ---------------- Class 5: grounding (result_lint never executes; it reads) ----------------
def _complete(**over):
    d = {'issue': 'GLP-1', 'status': 'COMPLETE', 'changed_files': [], 'artifacts_created': [],
         'acceptance_checks': [{'check': 'c', 'status': 'PASS', 'exit_code': 0,
                                'observable': over.pop('observable', '/home/ubuntu/business/CLAUDE.md:1 holds')}],
         'acceptance_clauses': [{'clause': 'x', 'verdict': 'PASS', 'evidence': 'e'}], 'unverified': 'none'}
    d.update(over)
    return d


def test_grounding_rejects_a_cited_path_that_does_not_exist():
    p = result_lint.grounding_problems(_complete(observable='/home/ubuntu/business/does/not/exist.mjs:12 now reports `ok`'))
    assert any('does not exist' in x for x in p), p


def test_grounding_rejects_a_line_past_end_of_file():
    p = result_lint.grounding_problems(_complete(observable='/home/ubuntu/business/CLAUDE.md:999999 holds'))
    assert any('past end of file' in x for x in p), p


def test_grounding_rejects_a_now_reports_literal_absent_from_the_file():
    p = result_lint.grounding_problems(_complete(observable='/home/ubuntu/business/CLAUDE.md now reports `tracker_freshness_ok_definitely_absent`'))
    assert any('does not contain' in x for x in p), p


def test_two_citation_readback_binds_each_literal_to_its_own_path():
    """H7: one literal was demanded of EVERY cited file in the text."""
    ob = ('/home/ubuntu/business/CLAUDE.md now reports `Claude Code`; '
          '/home/ubuntu/business/ops/linear/WORKER-STANDARD.md now reports `Delegate`')
    assert result_lint.grounding_problems(_complete(observable=ob)) == []


def test_grounding_accepts_a_real_citation_and_ignores_non_closure_results():
    assert result_lint.grounding_problems(_complete(observable='/home/ubuntu/business/CLAUDE.md:1 now reports `Claude Code`')) == []
    blocked = _complete(status='BLOCKED', observable='/home/ubuntu/business/nope.py:1')
    assert result_lint.grounding_problems(blocked) == []


# ---------------- Class 5b: a declared brief is always linted ----------------
def test_declared_but_unreadable_brief_is_a_defect(tmp_path, monkeypatch):
    monkeypatch.setattr(result_lint, '_issue_acceptance_text', lambda issue, **kw: ('x', 'linear'))
    r = tmp_path / 'RESULT.json'
    r.write_text(json.dumps(_complete(brief=str(tmp_path / 'missing-brief.md'))))
    probs = result_lint.lint(str(r), None)
    assert any('declares brief' in x and 'not a readable file' in x for x in probs), probs


# ---------------- Class 2: producer registry ----------------
def test_registered_artifact_without_a_producer_is_refused(monkeypatch):
    d = _complete(changed_files=['ops/evidence-freshness/adoptions.jsonl'])
    p = result_lint.producer_problems(d)
    assert any('registered artifact' in x for x in p), p


def test_registered_artifact_with_its_registered_producer_passes():
    d = _complete(changed_files=['ops/evidence-freshness/adoptions.jsonl'],
                  producer={'registered_command': 'python3 scripts/glp_evidence_refresh_cycle.py adopt --x'})
    assert result_lint.producer_problems(d) == []


def test_one_off_program_writing_a_registered_artifact_is_refused():
    d = _complete(changed_files=['ops/evidence-freshness/adoptions.jsonl'],
                  producer={'registered_command': 'python3 scripts/glp_evidence_batch_adopt.py'})
    p = result_lint.producer_problems(d)
    assert any('registered producer' in x and 'none of which' in x for x in p), p


def test_no_producer_artifact_needs_an_operator_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(result_lint, 'APPROVALS_DIR', tmp_path)  # no receipts here
    d = _complete(changed_files=['site-system/flagship-v2/candidates/glp34-provider-families-20260912/data/coupon-offers.json'],
                  producer={'registered_command': 'python3 scripts/anything.py'})
    p = result_lint.producer_problems(d)
    assert any('no program is registered' in x for x in p), p


# ---------------- Class 1b: the reviewer is bound ----------------
def _decision(tmp_path, name, sha, *, author='Fable (visible fable-operator TUI)', review=None, old=False):
    dec = tmp_path / 'OPERATOR-DECISION-x.json'
    body = {'schema': 'glp-operator-decision-v1', 'author': author, 'approved_sha256': {name: sha},
            'independent_review': {'result': str(review)} if review else None}
    dec.write_text(json.dumps(body))
    if old:
        # B2: an OLD mtime must buy nothing now that grandfathering is by receipt content.
        os.utime(dec, (0, 0))
    return dec, body


def test_unsigned_approve_is_unbound(tmp_path):
    rev = tmp_path / 'REVIEW.json'; rev.write_text(json.dumps({'verdict': 'APPROVE'}))
    dec, body = _decision(tmp_path, 'scripts/x.py', 'abc', review=rev)
    why = POG.reviewer_binding_problem(dec, body, json.loads(rev.read_text()), 'scripts/x.py', 'abc')
    assert why and 'no `reviewer`' in why


def test_self_review_is_unbound(tmp_path):
    rev = tmp_path / 'REVIEW.json'
    rev.write_text(json.dumps({'verdict': 'APPROVE', 'reviewer': 'Fable', 'reviewed_sha256': {'scripts/x.py': 'abc'}}))
    dec, body = _decision(tmp_path, 'scripts/x.py', 'abc', review=rev)
    why = POG.reviewer_binding_problem(dec, body, json.loads(rev.read_text()), 'scripts/x.py', 'abc')
    assert why and 'author' in why


def test_review_of_other_bytes_is_unbound_and_bound_review_passes(tmp_path):
    rev = tmp_path / 'REVIEW.json'
    rev.write_text(json.dumps({'verdict': 'APPROVE', 'reviewer': 'independent-review-agent-7', 'reviewed_sha256': {'scripts/x.py': 'OTHER'}}))
    dec, body = _decision(tmp_path, 'scripts/x.py', 'abc', review=rev)
    assert POG.reviewer_binding_problem(dec, body, json.loads(rev.read_text()), 'scripts/x.py', 'abc')
    rev.write_text(json.dumps({'verdict': 'APPROVE', 'reviewer': 'independent-review-agent-7', 'reviewed_sha256': {'scripts/x.py': 'abc'}}))
    assert POG.reviewer_binding_problem(dec, body, json.loads(rev.read_text()), 'scripts/x.py', 'abc') is None


def test_grandfathering_is_by_receipt_content_not_mtime(tmp_path):
    """B2: mtime was author-writable (os.utime) and the cutoff constant decoded to 2025."""
    import hashlib
    rev = tmp_path / 'REVIEW.json'; rev.write_text(json.dumps({'verdict': 'APPROVE'}))
    dec, body = _decision(tmp_path, 'scripts/x.py', 'abc', review=rev, old=True)   # old mtime buys nothing
    assert POG.reviewer_binding_problem(dec, body, json.loads(rev.read_text()), 'scripts/x.py', 'abc')
    for f in sorted((ROOT / 'ops/linear/approvals').glob('OPERATOR-DECISION-20260916-*.json')):
        assert hashlib.sha256(f.read_bytes()).hexdigest() in POG.GRANDFATHERED_DECISION_SHA256, f.name
    assert len(POG.GRANDFATHERED_DECISION_SHA256) == 3


def test_decision_without_an_author_cannot_be_reviewed_independently(tmp_path):
    """B3: all three live receipts had no author, so the self-review check was inert."""
    rev = tmp_path / 'REVIEW.json'
    rev.write_text(json.dumps({'verdict': 'APPROVE', 'reviewer': 'Fable', 'reviewed_sha256': {'scripts/x.py': 'abc'}}))
    dec, body = _decision(tmp_path, 'scripts/x.py', 'abc', author='', review=rev)
    why = POG.reviewer_binding_problem(dec, body, json.loads(rev.read_text()), 'scripts/x.py', 'abc')
    assert why and 'no `author`' in why


def test_reviewer_merely_containing_a_self_token_is_refused(tmp_path):
    """L1: the policy is that a same-seat name anywhere in the reviewer string is not independent."""
    rev = tmp_path / 'REVIEW.json'
    rev.write_text(json.dumps({'verdict': 'APPROVE', 'reviewer': 'fable-subagent-9', 'reviewed_sha256': {'scripts/x.py': 'abc'}}))
    dec, body = _decision(tmp_path, 'scripts/x.py', 'abc', review=rev)
    assert POG.reviewer_binding_problem(dec, body, json.loads(rev.read_text()), 'scripts/x.py', 'abc')


# ---------------- Class 1d: npm run build is the chokepoint ----------------
def _live_lock(lock, seat='glp-scout', age=0):
    lock.write_text(json.dumps({'seat': seat, 'reason': 'r', 'at': time.time() - age}))


def test_require_held_refuses_without_a_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(CBL, 'LOCK', tmp_path / 'lock.json')
    monkeypatch.setattr(CBL, '_is_real_lock_dir', lambda: True)
    monkeypatch.setattr(CBL, '_oracle_gate', lambda: 0)
    assert CBL.require_held('glp-scout') == 1 and 'REFUSED_NO_LOCK' in capsys.readouterr().out


def test_require_held_refuses_a_test_lock_dir_before_anything_else(tmp_path, monkeypatch, capsys):
    """B1: BUILD_LOCK_DIR was a one-variable bypass of both the lock and the oracle gate at the chokepoint."""
    lock = tmp_path / 'lock.json'; monkeypatch.setattr(CBL, 'LOCK', lock); _live_lock(lock)
    monkeypatch.setattr(CBL, '_is_real_lock_dir', lambda: False)
    monkeypatch.setattr(CBL, '_oracle_gate', lambda: pytest.fail('gate must not even be consulted'))
    assert CBL.require_held('glp-scout') == 1 and 'REFUSED_TEST_LOCK_DIR' in capsys.readouterr().out


def test_require_held_passes_only_the_holder_and_refuses_stale_or_other(tmp_path, monkeypatch, capsys):
    """H2: any live lock used to pass, so seat B could build on seat A's lock."""
    lock = tmp_path / 'lock.json'; monkeypatch.setattr(CBL, 'LOCK', lock)
    monkeypatch.setattr(CBL, '_is_real_lock_dir', lambda: True)
    monkeypatch.setattr(CBL, '_oracle_gate', lambda: 0)
    _live_lock(lock, 'glp-scout')
    assert CBL.require_held('glp-scout') == 0
    assert CBL.require_held('kael-sol') == 1 and 'REFUSED_NOT_HOLDER' in capsys.readouterr().out
    assert CBL.require_held(None) == 1 and 'REFUSED_NO_SEAT' in capsys.readouterr().out
    _live_lock(lock, 'glp-scout', age=CBL.STALE_S + 1)
    assert CBL.require_held('glp-scout') == 1 and 'STALE' in capsys.readouterr().out


def test_require_held_refuses_when_the_oracle_gate_refuses(tmp_path, monkeypatch):
    lock = tmp_path / 'lock.json'; monkeypatch.setattr(CBL, 'LOCK', lock); _live_lock(lock)
    monkeypatch.setattr(CBL, '_is_real_lock_dir', lambda: True)
    monkeypatch.setattr(CBL, '_oracle_gate', lambda: 1)
    assert CBL.require_held('glp-scout') == 1


def test_stale_ceiling_is_capped_on_the_real_lock_dir():
    """L2: BUILD_LOCK_STALE_S could be raised without bound, making any stale lock look live."""
    src = (ROOT / 'scripts/candidate_build_lock.py').read_text()
    assert 'min(max(300, _STALE_ENV), 2700) if _is_real_lock_dir()' in src


def test_candidate_build_string_carries_the_gate_where_ignore_scripts_cannot_strip_it():
    """H1: `npm run build --ignore-scripts` skipped prebuild; the gate now lives inside `build` itself."""
    s = json.loads((ROOT / 'site-system/flagship-v2/candidates/glp34-provider-families-20260912/package.json').read_text())['scripts']
    assert 'prebuild' not in s
    assert s['build'].startswith('python3 /home/ubuntu/business/scripts/candidate_build_lock.py require-held --seat')
    assert 'CANDIDATE_BUILD_SEAT' in s['build']


# ---------------- Class 3: Fable-actor clauses need an ENABLING declaration ----------------
def _brief(tmp_path, issue, clause, decl=None):
    b = tmp_path / 'b.md'
    text = f'LINEAR: {issue} - x\nGOAL\nServes: "{clause}"\n' + (f'{decl}\n' if decl else '') + 'NOT IN SCOPE\n- none\n'
    b.write_text(text)
    return b


def test_worker_seat_handed_only_fable_clauses_is_refused_without_a_declaration(tmp_path, monkeypatch):
    cache = tmp_path / 'cache.json'
    cache.write_text(json.dumps({'GLP-5': {'acceptance': 'Fable reads the candidate and signs the matrix.'}}))
    monkeypatch.setattr(BCG, 'CACHE', cache)
    monkeypatch.setattr(BCG, 'fable_obligation_rows', lambda *a, **k: [])
    code, rep = BCG.check(_brief(tmp_path, 'GLP-5', 'Fable reads the candidate and signs the matrix.'), 'GLP-5', 'glp-scout')
    assert code == 2 and 'ENABLING WORK FOR' in rep['how_to_clear']
    code, rep = BCG.check(_brief(tmp_path, 'GLP-5', 'Fable reads the candidate and signs the matrix.',
                                 decl='ENABLING WORK FOR: a b c d e f g'), 'GLP-5', 'glp-scout')
    assert code == 2, 'L3: six meaningless words must not satisfy the declaration'
    code, rep = BCG.check(_brief(tmp_path, 'GLP-5', 'Fable reads the candidate and signs the matrix.',
                                 decl='ENABLING WORK FOR: "reads the candidate and signs" - this seat emits the candidate Fable reads'), 'GLP-5', 'glp-scout')
    assert code == 0 and rep.get('enabling_work')
    code, rep = BCG.check(_brief(tmp_path, 'GLP-5', 'Fable reads the candidate and signs the matrix.'), 'GLP-5', 'fable-operator')
    assert code == 0


# ---------------- cycle-2 pins (N1/N2/N3/N4/N8/N9/N12) ----------------
CAND = 'site-system/flagship-v2/candidates/glp34-provider-families-20260912'


def test_npm_run_build_is_a_registered_producer_for_the_pricing_matrix():
    d = _complete(changed_files=[f'{CAND}/data/provider-pricing-matrix.json'],
                  producer={'registered_command': 'npm run build'})
    assert result_lint.producer_problems(d) == []
    d = _complete(changed_files=[f'{CAND}/data/provider-pricing-matrix.json'],
                  producer={'registered_command': f'cd /home/ubuntu/business/{CAND} && npm run build'})
    assert result_lint.producer_problems(d) == []


def test_relocated_copy_of_a_registered_producer_is_refused():
    """N1: a suffix rule accepted `/tmp/x/scripts/glp_evidence_refresh_cycle.py` - the registry's own motivating one-off."""
    d = _complete(changed_files=['ops/evidence-freshness/adoptions.jsonl'],
                  producer={'registered_command': 'python3 /tmp/x/scripts/glp_evidence_refresh_cycle.py adopt'})
    assert result_lint.producer_problems(d)
    d = _complete(changed_files=[f'{CAND}/data/provider-pricing-matrix.json'],
                  producer={'registered_command': 'cd /tmp/evil && node scripts/build-staging.mjs'})
    assert result_lint.producer_problems(d)


def test_another_candidates_builder_is_not_this_candidates_producer():
    """N12."""
    other = 'site-system/flagship-v2/candidates/glp208-source-refresh'
    d = _complete(changed_files=[f'{CAND}/data/provider-pricing-matrix.json'],
                  producer={'registered_command': f'cd /home/ubuntu/business/{other} && node scripts/build-staging.mjs'})
    assert result_lint.producer_problems(d)


def test_unnormalised_and_candidate_relative_paths_are_caught_without_a_producer():
    """N2/H5: `ops//...`, `./`, `~`, and a candidate-relative spelling all still hit the registry."""
    for path in ('ops//evidence-freshness/adoptions.jsonl', './ops/evidence-freshness/adoptions.jsonl',
                 '~/business/ops/evidence-freshness/adoptions.jsonl', 'data/provider-pricing-matrix.json',
                 './data/provider-pricing-matrix.json'):
        assert result_lint.producer_problems(_complete(changed_files=[path])), path


def test_a_receipt_that_merely_mentions_the_path_does_not_authorize(tmp_path, monkeypatch):
    """H6/N9: substring grep authorized a 'do NOT touch <path>' note."""
    (tmp_path / 'FABLE-DECISION-x.json').write_text(json.dumps({'note': f'do NOT touch {CAND}/data/coupon-offers.json'}))
    monkeypatch.setattr(result_lint, 'APPROVALS_DIR', tmp_path)
    assert result_lint.producer_problems(_complete(changed_files=[f'{CAND}/data/coupon-offers.json']))


def test_router_unavailable_is_recorded_not_silently_narrowed(monkeypatch):
    """L4/N9."""
    import builtins
    real = builtins.__import__
    def fake(name, *a, **k):
        if name == 'skill_router':
            raise ImportError('boom')
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', fake)
    p = result_lint.producer_problems(_complete(changed_files=['ops/evidence-freshness/adoptions.jsonl']))
    assert any('surface router unavailable' in x for x in p), p


def test_repeated_citation_still_has_its_literal_checked():
    """N3: dedup by (path,line) skipped the literal window on the second citation - the routine honest shape."""
    d = _complete(observable='/home/ubuntu/business/CLAUDE.md:1 holds')
    d['acceptance_clauses'] = [{'clause': 'x', 'verdict': 'PASS',
                                'evidence': '/home/ubuntu/business/CLAUDE.md:1 now reports `definitely_not_in_claude_md_zz`'}]
    p = result_lint.grounding_problems(d)
    assert any('does not contain' in x for x in p), p


def test_literal_window_runs_to_the_next_citation_not_160_chars():
    """N4: padding past 160 chars evaded the literal check."""
    ob = '/home/ubuntu/business/CLAUDE.md ' + ('pad ' * 60) + 'now reports `definitely_not_in_claude_md_zz`'
    assert result_lint.grounding_problems(_complete(observable=ob))


def test_enabling_declaration_echo_is_punctuation_insensitive(tmp_path, monkeypatch):
    """N8."""
    cache = tmp_path / 'cache.json'
    cache.write_text(json.dumps({'GLP-5': {'acceptance': 'Fable reads the candidate, then signs the matrix.'}}))
    monkeypatch.setattr(BCG, 'CACHE', cache)
    monkeypatch.setattr(BCG, 'fable_obligation_rows', lambda *a, **k: [])
    b = _brief(tmp_path, 'GLP-5', 'Fable reads the candidate, then signs the matrix.',
               decl='ENABLING WORK FOR: "reads the candidate then signs" - this seat emits what Fable reads')
    code, rep = BCG.check(b, 'GLP-5', 'glp-scout')
    assert code == 0, rep


# ---------------- cycle-3 pins (D1/D3/D5/D6) ----------------
def test_a_builder_copied_inside_the_candidate_tree_is_not_its_producer(tmp_path):
    """D1: fnmatch's `*` spanned `/`, so scratch/ and node_modules/.cache copies matched the registry glob."""
    for cmd in (f'cd /home/ubuntu/business/{CAND}/scratch && node scripts/build-staging.mjs',
                f'cd /home/ubuntu/business/{CAND} && node scratch/scripts/build-staging.mjs',
                f'cd /home/ubuntu/business/{CAND} && node node_modules/.cache/scripts/update-price-index.mjs'):
        d = _complete(changed_files=[f'{CAND}/data/provider-pricing-matrix.json'], producer={'registered_command': cmd})
        assert result_lint.producer_problems(d), cmd


def test_candidate_relative_matrix_with_the_correct_producer_passes():
    """D3: the common spelling (29 corpus RESULTs) with the right producer was refused with a false message."""
    d = _complete(changed_files=['data/provider-pricing-matrix.json'],
                  producer={'registered_command': f'cd /home/ubuntu/business/{CAND} && npm run build'})
    assert result_lint.producer_problems(d) == []


def test_symlinked_spelling_of_a_registered_artifact_is_caught(tmp_path):
    """D5: only resolve() (not normpath) sees through a symlink."""
    link = tmp_path / 'ef'
    link.symlink_to('/home/ubuntu/business/ops/evidence-freshness')
    assert result_lint.producer_problems(_complete(changed_files=[str(link / 'adoptions.jsonl')]))


def test_backup_copy_of_a_registered_artifact_is_not_the_artifact():
    """D2/D5: `reports/ops/backups/ops/evidence-freshness/adoptions.jsonl` must not hit the registry."""
    assert result_lint.producer_problems(_complete(changed_files=['reports/ops/backups/ops/evidence-freshness/adoptions.jsonl'])) == []


def test_receipt_sha_binding_is_pinned_with_a_schema_bearing_fixture(tmp_path, monkeypatch):
    """N9/D5: the earlier fixture had no `schema`, so restoring the substring rule left it green."""
    art = f'{CAND}/data/coupon-offers.json'
    (tmp_path / 'OPERATOR-DECISION-x.json').write_text(json.dumps({
        'schema': 'glp-operator-decision-v1', 'authorizes_writes': [art], 'approved_sha256': {art: 'not-the-live-sha'}}))
    monkeypatch.setattr(result_lint, 'APPROVALS_DIR', tmp_path)
    assert result_lint.producer_problems(_complete(changed_files=[art]))   # named but wrong bytes -> refused
    import hashlib
    live = hashlib.sha256((ROOT / art).read_bytes()).hexdigest()
    (tmp_path / 'OPERATOR-DECISION-x.json').write_text(json.dumps({
        'schema': 'glp-operator-decision-v1', 'authorizes_writes': [art], 'approved_sha256': {art: live}}))
    assert result_lint.producer_problems(_complete(changed_files=[art])) == []


def test_a_missing_literal_cited_twice_is_exactly_one_problem():
    """D6."""
    ob = '/home/ubuntu/business/CLAUDE.md now reports `definitely_not_in_claude_md_zz`'
    d = _complete(observable=ob)
    d['acceptance_clauses'] = [{'clause': 'x', 'verdict': 'PASS', 'evidence': ob}]
    p = result_lint.grounding_problems(d)
    assert len([x for x in p if 'does not contain' in x]) == 1, p


def test_a_stale_later_sorting_receipt_never_clears_a_bytes_bound_approval(tmp_path, monkeypatch):
    """Re-baseline 2026-09-16: an older receipt approving OTHER bytes sorted after the valid one and reset the
    verdict it had just found, refusing four legitimate re-baselines. Fail-closed, but it made the gate
    unusable for any file with a historical receipt."""
    name, live = 'scripts/x.py', 'abc'
    rev = tmp_path / 'REVIEW.json'
    rev.write_text(json.dumps({'verdict': 'APPROVE', 'reviewer': 'independent-review-agent-7', 'reviewed_sha256': {name: live}}))
    old_rev = tmp_path / 'OLD-REVIEW.json'; old_rev.write_text(json.dumps({'verdict': 'APPROVE'}))
    (tmp_path / 'A-valid.json').write_text(json.dumps({'schema': 'glp-operator-decision-v1', 'author': 'Fable',
                                                        'approved_sha256': {name: live},
                                                        'independent_review': {'result': str(rev)}}))
    (tmp_path / 'Z-stale.json').write_text(json.dumps({'schema': 'glp-operator-decision-v1', 'author': 'Fable',
                                                        'approved_sha256': {name: 'OTHERBYTES'},
                                                        'independent_review': {'result': str(old_rev)}}))
    monkeypatch.setattr(POG, 'APPROVALS', tmp_path)
    found = POG.receipts_for(name, live)
    assert found['review_verdict'] == 'APPROVE', found
    # and a stale receipt whose review was DEFECTS still sticks
    old_rev.write_text(json.dumps({'verdict': 'DEFECTS'}))
    assert POG.receipts_for(name, live)['review_verdict'] != 'APPROVE'
